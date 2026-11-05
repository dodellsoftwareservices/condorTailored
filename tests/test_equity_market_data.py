"""condor/equity_market_data.py: cache, single-flight, stale fallback, and
not-found mapping, with the real yfinance call replaced. No live network.
"""

import asyncio
import time

import pandas as pd
import pytest
from yfinance.exceptions import YFRateLimitError, YFTickerMissingError, YFTzMissingError

from condor import equity_market_data as emd
from condor.equity_observability import EquityMetrics


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """A fresh metrics instance and empty caches for every test.

    Mirrors test_alpaca_paper_transport.py's own ``monkeypatch.setattr(A,
    "equity_metrics", metrics)`` — the module imports the name directly, so
    replacing it here replaces what the module under test actually calls.
    """
    metrics = EquityMetrics()
    monkeypatch.setattr(emd, "equity_metrics", metrics)
    emd.reset_caches()
    yield metrics
    emd.reset_caches()


def _counts(metrics: EquityMetrics) -> dict:
    return metrics.snapshot()["counters"]


_GOOD_INFO = {
    "symbol": "AAPL",
    "longName": "Apple Inc.",
    "exchange": "NMS",
    "currency": "USD",
    "quoteType": "EQUITY",
}


# ── search_symbols ──


def test_search_filters_to_equity_and_etf(monkeypatch):
    raw = [
        {
            "symbol": "AAPL",
            "shortname": "Apple Inc.",
            "exchange": "NMS",
            "quoteType": "EQUITY",
        },
        {
            "symbol": "BTC-USD",
            "shortname": "Bitcoin",
            "exchange": "CCC",
            "quoteType": "CRYPTOCURRENCY",
        },
        {
            "symbol": "VOO",
            "shortname": "Vanguard S&P 500",
            "exchange": "PCX",
            "quoteType": "ETF",
        },
        {
            "symbol": "^GSPC",
            "shortname": "S&P 500",
            "exchange": "SNP",
            "quoteType": "INDEX",
        },
    ]
    monkeypatch.setattr(emd, "_search_sync", lambda q, n: raw)

    results = asyncio.run(emd.search_symbols("whatever"))

    assert [r.symbol for r in results] == ["AAPL", "VOO"]
    assert results[0] == emd.Instrument(
        symbol="AAPL",
        name="Apple Inc.",
        exchange="NMS",
        currency="USD",
        quote_type="EQUITY",
    )


def test_search_blank_query_short_circuits(monkeypatch):
    calls = []
    monkeypatch.setattr(emd, "_search_sync", lambda q, n: calls.append(1) or [])

    assert asyncio.run(emd.search_symbols("   ")) == []
    assert calls == []  # never reached the cache/fetch path at all


# ── caching: hit/miss, TTL expiry, single-flight ──


def test_cache_hit_avoids_a_second_fetch(monkeypatch, isolated):
    calls = []
    monkeypatch.setattr(
        emd, "_instrument_sync", lambda symbol: (calls.append(symbol), _GOOD_INFO)[1]
    )

    first = asyncio.run(emd.fetch_instrument("aapl"))
    second = asyncio.run(emd.fetch_instrument("AAPL"))  # symbol is normalized to upper

    assert first == second
    assert calls == ["AAPL"]  # second call served from cache, not re-fetched
    counts = _counts(isolated)
    assert counts["cache_hit"] == 1
    assert counts["cache_miss"] == 1
    assert counts["provider_ok"] == 1


def test_ttl_expiry_forces_a_refetch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        emd, "_instrument_sync", lambda symbol: (calls.append(symbol), _GOOD_INFO)[1]
    )

    asyncio.run(emd.fetch_instrument("AAPL"))
    assert len(calls) == 1

    # Backdate the cached entry past its TTL directly, rather than touching
    # the real clock (monkeypatching time.monotonic globally would also
    # perturb asyncio/SingleFlight, which use the same module).
    key = ("instrument", "AAPL")
    ts, value = emd._instrument_cache[key]
    emd._instrument_cache[key] = (ts - emd._INSTRUMENT_TTL - 1, value)

    asyncio.run(emd.fetch_instrument("AAPL"))
    assert len(calls) == 2


# ── _instrument_sync itself, not monkeypatched past ──
# Found in an independent review of this module: every test above replaces
# _instrument_sync wholesale, so none of them ever actually ran its own
# empty-info branch — which called YFTickerMissingError with one argument
# while the installed yfinance 1.7.0 requires two, raising TypeError instead
# of the intended exception. One level deeper (fake yf.Ticker itself) so the
# real function body executes.


class _FakeTicker:
    def __init__(self, info: dict):
        self.info = info


def test_instrument_sync_raises_ticker_missing_on_empty_info(monkeypatch):
    monkeypatch.setattr(emd.yf, "Ticker", lambda symbol, session=None: _FakeTicker({}))

    with pytest.raises(YFTickerMissingError):
        emd._instrument_sync("NOPE")


def test_fetch_instrument_end_to_end_through_the_real_sync_wrapper(monkeypatch):
    """The regression this is actually guarding: fetch_instrument on a symbol
    with no identifying info must come back as EquitySymbolNotFound (404 at
    the route layer), not EquityDataError (502) from an unrelated TypeError.
    """
    monkeypatch.setattr(emd.yf, "Ticker", lambda symbol, session=None: _FakeTicker({}))

    with pytest.raises(emd.EquitySymbolNotFound):
        asyncio.run(emd.fetch_instrument("NOPE"))


def test_single_flight_coalesces_concurrent_identical_calls(monkeypatch):
    # The real SingleFlight stays in place — that's what's under test. Only
    # the sync fetch underneath it is replaced, made slow enough (a real
    # time.sleep, since it runs in asyncio.to_thread's worker thread, not on
    # the event loop) that both search_symbols() calls are genuinely
    # in-flight at once, so a bug that stopped sharing the task would show up
    # as two calls instead of one.
    calls = 0

    def slow_search(query, max_results):
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return [
            {
                "symbol": "AAPL",
                "shortname": "Apple Inc.",
                "exchange": "NMS",
                "quoteType": "EQUITY",
            }
        ]

    monkeypatch.setattr(emd, "_search_sync", slow_search)

    async def both():
        # asyncio.gather needs a running loop to attach to, so it has to be
        # built inside the coroutine asyncio.run drives, not passed to
        # asyncio.run() directly as an already-constructed awaitable.
        return await asyncio.gather(
            emd.search_symbols("apple"), emd.search_symbols("apple")
        )

    results = asyncio.run(both())

    assert calls == 1  # both callers shared the one in-flight fetch
    assert results[0] == results[1]


# ── not-found mapping ──


@pytest.mark.parametrize(
    "make_exc",
    [
        lambda s: YFTickerMissingError(s, "possibly delisted"),
        lambda s: YFTzMissingError(s),  # a real subclass of YFTickerMissingError
    ],
    ids=["YFTickerMissingError", "YFTzMissingError"],
)
def test_ticker_missing_family_maps_to_symbol_not_found(monkeypatch, make_exc):
    def boom(symbol):
        raise make_exc(symbol)

    monkeypatch.setattr(emd, "_instrument_sync", boom)
    with pytest.raises(emd.EquitySymbolNotFound):
        asyncio.run(emd.fetch_instrument("NOPE"))


def test_blank_symbol_is_not_found_without_a_fetch(monkeypatch):
    calls = []
    monkeypatch.setattr(emd, "_instrument_sync", lambda s: calls.append(s))
    with pytest.raises(emd.EquitySymbolNotFound):
        asyncio.run(emd.fetch_instrument("  "))
    assert calls == []


# ── stale fallback on rate limit / other failure ──


def test_rate_limit_falls_back_to_the_stale_value(monkeypatch, isolated):
    monkeypatch.setattr(emd, "_instrument_sync", lambda s: _GOOD_INFO)
    first = asyncio.run(emd.fetch_instrument("AAPL"))

    # Expire only the live TTL entry; the stale copy is untouched.
    key = ("instrument", "AAPL")
    ts, value = emd._instrument_cache[key]
    emd._instrument_cache[key] = (ts - emd._INSTRUMENT_TTL - 1, value)

    def rate_limited(s):
        raise YFRateLimitError()  # takes no arguments

    monkeypatch.setattr(emd, "_instrument_sync", rate_limited)
    second = asyncio.run(emd.fetch_instrument("AAPL"))

    assert second == first
    assert _counts(isolated)["provider_rate_limited"] == 1


def test_rate_limit_with_no_stale_copy_raises(monkeypatch, isolated):
    def rate_limited(s):
        raise YFRateLimitError()

    monkeypatch.setattr(emd, "_instrument_sync", rate_limited)
    with pytest.raises(emd.EquityDataError):
        asyncio.run(emd.fetch_instrument("AAPL"))
    assert _counts(isolated)["provider_rate_limited"] == 1


def test_other_failure_also_falls_back_to_stale(monkeypatch, isolated):
    monkeypatch.setattr(emd, "_instrument_sync", lambda s: _GOOD_INFO)
    asyncio.run(emd.fetch_instrument("AAPL"))

    key = ("instrument", "AAPL")
    ts, value = emd._instrument_cache[key]
    emd._instrument_cache[key] = (ts - emd._INSTRUMENT_TTL - 1, value)

    def boom(s):
        raise ConnectionError("down")

    monkeypatch.setattr(emd, "_instrument_sync", boom)
    result = asyncio.run(emd.fetch_instrument("AAPL"))

    assert result.symbol == "AAPL"
    assert _counts(isolated)["provider_error"] == 1


# ── fetch_bars row shaping ──


def test_bars_are_shaped_like_candle_data(monkeypatch):
    index = pd.date_range("2026-01-02", periods=2, freq="D", tz="UTC")
    df = pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [101.0, 102.5],
            "Volume": [1_000_000, 900_000],
        },
        index=index,
    )
    monkeypatch.setattr(emd, "_history_sync", lambda symbol, period, interval: df)

    rows = asyncio.run(emd.fetch_bars("AAPL", period="5d", interval="1d"))

    assert len(rows) == 2
    assert set(rows[0]) == {"timestamp", "open", "high", "low", "close", "volume"}
    assert rows[0]["timestamp"] == pytest.approx(index[0].timestamp())
    assert rows[0]["open"] == 100.0
    assert rows[1]["close"] == 102.5


def test_empty_history_is_not_found(monkeypatch):
    monkeypatch.setattr(
        emd,
        "_history_sync",
        lambda symbol, period, interval: pd.DataFrame(
            columns=["Open", "High", "Low", "Close", "Volume"]
        ),
    )
    with pytest.raises(emd.EquitySymbolNotFound):
        asyncio.run(emd.fetch_bars("NOPE", period="5d", interval="1d"))
