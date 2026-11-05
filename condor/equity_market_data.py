"""Yahoo Finance-backed research data for US equities: symbol lookup,
instrument info, and historical bars.

Read-only — this module never submits an order and never talks to Alpaca.
``condor/alpaca_paper.py`` owns execution, and the one quote call a paper
account needs (execution-grade, priced by the broker itself rather than a
free research feed) stays on that client, not here.

Named after the provider, matching ``condor/pool_data.py`` (GeckoTerminal) —
not ``condor/fetchers/``, which is contractually scoped to the Hummingbot
Backend API client (every fetcher's first argument is that client), and not
a ``condor/venues/`` package, which would shadow the ``condor/venues.py``
module that already exists (see ``docs/paper-trading-stack.md``).

``yfinance`` is synchronous; calls use a dedicated pool with bounded admission
and an overall caller deadline. A timed-out worker retains its capacity slot
until it really finishes. Yahoo rate-limits datacenter IPs, so
each cache below has a stale-tolerant fallback in the same spirit as
``pool_data.py``'s own ``_stale_put``/``_stale_get``: a research chart going
blank on a transient 429 is a worse answer than a two-minute-old one.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Hashable

import requests
import yfinance as yf
from yfinance.exceptions import YFRateLimitError, YFTickerMissingError

from condor.asyncutil import SingleFlight
from condor.equity_observability import (
    EquityEvent,
    EquityMeasurement,
    equity_correlation_id,
    equity_metrics,
)

logger = logging.getLogger(__name__)


class EquityDataError(RuntimeError):
    """A research-data call failed for a reason that is not "symbol not found"."""


class EquitySymbolNotFound(EquityDataError):
    """The symbol does not exist, or Yahoo has no data for it."""


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    exchange: str
    currency: str
    quote_type: str  # "EQUITY" | "ETF" | ...


# ── Caching ──
# One TTL dict + one stale-tolerant dict + one SingleFlight per concept, the
# same shape as pool_data.py's token/pool caches and market.py's candle
# cache — not a shared abstraction, because the three existing copies of this
# idiom already disagree on eviction details in ways that would have to be
# parameterized away, and three thin instances are easier to read than one
# configurable one. Raw upstream values are cached (a yfinance quote dict, an
# info dict, a DataFrame); each public function re-shapes the cached value on
# every call, which is pure in-memory work, not worth caching separately.

_SEARCH_TTL = 300.0
_INSTRUMENT_TTL = 3600.0
_BARS_TTL = 30.0  # matches market.py's own candle TTL

# Serve a value this much past its TTL, on a rate limit or upstream failure,
# rather than fail outright. Separate from the TTL dicts so sweeping an
# expired live entry never takes the fallback copy with it.
_STALE_MAX_AGE = 15 * 60.0

_CACHE_MAX = 256  # generous for a research feature; bounds an unbounded symbol stream

_search_cache: dict[Hashable, tuple[float, Any]] = {}
_search_stale: dict[Hashable, tuple[float, Any]] = {}
_search_inflight = SingleFlight()

_instrument_cache: dict[Hashable, tuple[float, Any]] = {}
_instrument_stale: dict[Hashable, tuple[float, Any]] = {}
_instrument_inflight = SingleFlight()

_bars_cache: dict[Hashable, tuple[float, Any]] = {}
_bars_stale: dict[Hashable, tuple[float, Any]] = {}
_bars_inflight = SingleFlight()


def _ttl_get(cache: dict, key: Hashable, ttl: float) -> Any | None:
    entry = cache.get(key)
    if entry and (time.monotonic() - entry[0]) < ttl:
        return entry[1]
    return None


def _ttl_put(cache: dict, key: Hashable, value: Any, ttl: float) -> None:
    now = time.monotonic()
    for k in [k for k, (ts, _) in cache.items() if now - ts >= ttl]:
        cache.pop(k, None)
    cache[key] = (now, value)
    while len(cache) > _CACHE_MAX:
        cache.pop(next(iter(cache)))  # dicts preserve insertion order


def _stale_put(cache: dict, key: Hashable, value: Any) -> None:
    cache[key] = (time.monotonic(), value)
    now = time.monotonic()
    for k in [k for k, (ts, _) in cache.items() if now - ts >= _STALE_MAX_AGE]:
        cache.pop(k, None)
    while len(cache) > _CACHE_MAX:
        cache.pop(next(iter(cache)))


def _stale_get(cache: dict, key: Hashable) -> tuple[Any | None, float]:
    """The cached value regardless of freshness, with its age in seconds."""
    entry = cache.get(key)
    if not entry:
        return None, 0.0
    return entry[1], time.monotonic() - entry[0]


def _usable_stale(cache: dict, key: Hashable, concept: str) -> Any | None:
    """A stale value, but only inside ``_STALE_MAX_AGE`` — checked here, at
    read time, not only when a later success happens to sweep the dict.

    ``_stale_put`` only evicts on its own next write, so during a *sustained*
    outage (no write ever happens again) an entry would otherwise be served
    arbitrarily old: a chart could silently show a multi-day-old price as if
    it were merely the usual few-minutes-stale fallback.
    """
    value, age = _stale_get(cache, key)
    if value is None or age >= _STALE_MAX_AGE:
        return None
    logger.info(
        "Equity provider request source=yfinance correlation_id=%s "
        "concept=%s outcome=stale_fallback stale_age_seconds=%.1f",
        equity_correlation_id.get(),
        concept,
        age,
    )
    return value


async def _cached_fetch(
    *,
    concept: str,
    cache: dict,
    stale_cache: dict,
    inflight: SingleFlight,
    key: Hashable,
    ttl: float,
    sync_fetch: Callable[[], Any],
) -> Any:
    """TTL cache, single-flight coalescing, and a stale fallback — one call site
    for all three research endpoints below.

    Raises :class:`EquitySymbolNotFound` on a ``YFTickerMissingError`` (which
    also covers its ``YFPricesMissingError``/``YFTzMissingError`` subclasses)
    and :class:`EquityDataError` on anything else that has no usable stale
    copy to fall back to. A rate limit or any other upstream failure serves
    the last-known-good value, aged, when one exists.
    """
    cached = _ttl_get(cache, key, ttl)
    if cached is not None:
        equity_metrics.record(EquityEvent.CACHE_HIT)
        return cached
    equity_metrics.record(EquityEvent.CACHE_MISS)

    started = time.monotonic()
    outcome = "abandoned"
    try:
        value = await inflight.run(key, lambda: _bounded_sync_fetch(sync_fetch))
    except YFTickerMissingError:
        outcome = "not_found"
        equity_metrics.record(
            EquityEvent.PROVIDER_OK
        )  # a real answer: "no such symbol"
        raise EquitySymbolNotFound(f"No {concept} data for this symbol") from None
    except YFRateLimitError as e:
        outcome = "rate_limited"
        equity_metrics.record(EquityEvent.PROVIDER_RATE_LIMITED)
        stale = _usable_stale(stale_cache, key, concept)
        if stale is not None:
            outcome = "stale_fallback"
            return stale
        raise EquityDataError(f"{concept} data is rate limited upstream") from e
    except Exception as e:
        outcome = "error"
        equity_metrics.record(EquityEvent.PROVIDER_ERROR)
        stale = _usable_stale(stale_cache, key, concept)
        if stale is not None:
            outcome = "stale_fallback"
            return stale
        raise EquityDataError(f"Failed to fetch {concept} data") from e
    else:
        outcome = "ok"
        equity_metrics.record(EquityEvent.PROVIDER_OK)
        _ttl_put(cache, key, value, ttl)
        _stale_put(stale_cache, key, value)
        return value
    finally:
        elapsed = time.monotonic() - started
        if outcome != "abandoned":
            equity_metrics.observe(EquityMeasurement.PROVIDER_LATENCY_SECONDS, elapsed)
        # Never the query or symbol: equity_observability's whole contract is a
        # fixed, bounded label/line set with nothing a viewer typed in it.
        logger.info(
            "Equity provider request source=yfinance correlation_id=%s "
            "concept=%s outcome=%s latency_seconds=%.6f",
            equity_correlation_id.get(),
            concept,
            outcome,
            elapsed,
        )


# ── Sync wrappers ──
# The only functions that actually call yfinance. Kept separate and private so
# tests monkeypatch exactly these — one level above the real network call,
# the same principle test_alpaca_paper_transport.py uses mocking
# aiohttp.ClientSession rather than reaching into aiohttp's internals.

# Clamp yfinance's own explicit timeouts as well as calls that omit one.
# A separate bounded pool keeps a timed-out worker out of the shared executor.
# Slots are released only when workers finish, not when callers stop waiting.
_TIMEOUT_SECONDS = 20
_OPERATION_TIMEOUT_SECONDS = 60
_WORKER_COUNT = 4
_workers = ThreadPoolExecutor(
    max_workers=_WORKER_COUNT, thread_name_prefix="equity-data"
)
_worker_slots = threading.BoundedSemaphore(_WORKER_COUNT)


async def _bounded_sync_fetch(fetch: Callable[[], Any]) -> Any:
    if not _worker_slots.acquire(blocking=False):
        raise EquityDataError("Research data workers are busy; try again shortly")
    try:
        future = _workers.submit(fetch)
    except BaseException:
        _worker_slots.release()
        raise
    future.add_done_callback(lambda _: _worker_slots.release())
    # Cancellation cannot stop a running Python thread. The completion callback
    # above deliberately retains its slot until the real work has ended.
    return await asyncio.wait_for(
        asyncio.wrap_future(future), timeout=_OPERATION_TIMEOUT_SECONDS
    )


class _TimeoutSession(requests.Session):
    def request(self, *args, **kwargs):
        timeout = kwargs.get("timeout")

        def clamp(value):
            return _TIMEOUT_SECONDS if value is None else min(value, _TIMEOUT_SECONDS)

        kwargs["timeout"] = (
            tuple(clamp(value) for value in timeout)
            if isinstance(timeout, tuple)
            else clamp(timeout)
        )
        return super().request(*args, **kwargs)


_session = _TimeoutSession()


def _search_sync(query: str, max_results: int) -> list[dict]:
    return list(
        yf.Search(query, max_results=max_results, news_count=0, session=_session).quotes
    )


def _instrument_sync(symbol: str) -> dict:
    info = yf.Ticker(symbol, session=_session).info
    # Some yfinance versions answer an unknown ticker with a near-empty dict
    # rather than raising — treat "nothing identifying" the same as the
    # exception case rather than returning a blank instrument.
    #
    # YFTickerMissingError.__init__ requires (ticker, rationale) — installed
    # yfinance 1.7.0, verified by inspect.signature, not assumed. A one-arg
    # call here raised TypeError instead, which _cached_fetch's generic
    # except Exception branch still catches (so no crash reached a caller),
    # but it surfaced as EquityDataError/502 rather than the intended
    # EquitySymbolNotFound/404 — and every existing test monkeypatches this
    # whole function, so none of them actually ran this line to catch it.
    if not info or not (
        info.get("symbol") or info.get("shortName") or info.get("longName")
    ):
        raise YFTickerMissingError(symbol, "no identifying fields in Yahoo's response")
    return info


def _history_sync(symbol: str, period: str, interval: str):
    return yf.Ticker(symbol, session=_session).history(period=period, interval=interval)


# ── Public API ──

_SEARCH_QUOTE_TYPES = frozenset({"EQUITY", "ETF"})


async def search_symbols(query: str, limit: int = 8) -> list[Instrument]:
    """US equities/ETFs matching ``query``, ranked as Yahoo ranks them.

    Yahoo's search also returns crypto, indices and futures; "look up a
    stock" doesn't need those, so they're filtered out here rather than left
    for the caller to notice.
    """
    query = query.strip()
    if not query:
        return []
    raw = await _cached_fetch(
        concept="search",
        cache=_search_cache,
        stale_cache=_search_stale,
        inflight=_search_inflight,
        key=("search", query, limit),
        ttl=_SEARCH_TTL,
        sync_fetch=lambda: _search_sync(query, limit),
    )
    results = []
    for quote in raw:
        quote_type = str(quote.get("quoteType") or "").upper()
        symbol = str(quote.get("symbol") or "").strip()
        if quote_type not in _SEARCH_QUOTE_TYPES or not symbol:
            continue
        results.append(
            Instrument(
                symbol=symbol,
                name=str(quote.get("shortname") or quote.get("longname") or ""),
                exchange=str(quote.get("exchange") or ""),
                currency="USD",
                quote_type=quote_type,
            )
        )
    return results


async def fetch_instrument(symbol: str) -> Instrument:
    """Name, exchange and currency for one symbol. Raises :class:`EquitySymbolNotFound`."""
    symbol = symbol.strip().upper()
    if not symbol:
        raise EquitySymbolNotFound("No symbol given")
    info = await _cached_fetch(
        concept="instrument",
        cache=_instrument_cache,
        stale_cache=_instrument_stale,
        inflight=_instrument_inflight,
        key=("instrument", symbol),
        ttl=_INSTRUMENT_TTL,
        sync_fetch=lambda: _instrument_sync(symbol),
    )
    return Instrument(
        symbol=str(info.get("symbol") or symbol),
        name=str(info.get("longName") or info.get("shortName") or ""),
        exchange=str(info.get("exchange") or ""),
        currency=str(info.get("currency") or "USD"),
        quote_type=str(info.get("quoteType") or "").upper(),
    )


def reset_caches() -> None:
    """Forget every cached value and in-flight request. Tests only.

    Mirrors ``pool_data.reset_gecko_throttle()``: this module's caches are
    process-wide, so a test that populates one and a test that asserts a cold
    miss must not share state just because they ran in the same process.
    """
    for cache in (
        _search_cache,
        _search_stale,
        _instrument_cache,
        _instrument_stale,
        _bars_cache,
        _bars_stale,
    ):
        cache.clear()
    for inflight in (_search_inflight, _instrument_inflight, _bars_inflight):
        inflight.clear()


async def fetch_bars(symbol: str, period: str, interval: str) -> list[dict]:
    """OHLCV rows for ``symbol``, shaped exactly like ``CandleData``:
    ``timestamp`` (unix seconds), ``open``, ``high``, ``low``, ``close``,
    ``volume``. Raises :class:`EquitySymbolNotFound`.
    """
    symbol = symbol.strip().upper()
    if not symbol:
        raise EquitySymbolNotFound("No symbol given")
    df = await _cached_fetch(
        concept="bars",
        cache=_bars_cache,
        stale_cache=_bars_stale,
        inflight=_bars_inflight,
        key=("bars", symbol, period, interval),
        ttl=_BARS_TTL,
        sync_fetch=lambda: _history_sync(symbol, period, interval),
    )
    # An empty result is yfinance's usual "nothing here" signal for a bad
    # symbol/period/interval combination — it doesn't always raise. Checked
    # here rather than in _history_sync so a cached or stale-fallback empty
    # frame is re-evaluated the same way a fresh one is.
    if df is None or df.empty:
        raise EquitySymbolNotFound(f"No price history for {symbol}")
    rows = []
    for ts, row in df.iterrows():
        rows.append(
            {
                "timestamp": float(ts.timestamp()),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
            }
        )
    return rows
