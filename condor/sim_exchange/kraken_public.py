"""Read-only client for Kraken's public market-data API.

Paper-only by construction, like ``condor.alpaca_paper``: the host is pinned,
every path must sit under ``/0/public/`` and only GET is ever issued, so this
module cannot reach a private or trading endpoint and never holds an API key.
Redirects are refused outright.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable

import aiohttp

HOST = "https://api.kraken.com"
PUBLIC_PREFIX = "/0/public/"

Transport = Callable[[str, dict], Awaitable[dict]]

# Kraken's own ticker aliases for assets people usually write differently.
_ALIASES = {"BTC": "XBT", "DOGE": "XDG"}
_REVERSE = {v: k for k, v in _ALIASES.items()}


class KrakenPublicError(RuntimeError):
    """Kraken answered with an error, or could not be reached."""


@dataclass(frozen=True)
class PairRules:
    altname: str  # e.g. XBTUSD: what the public endpoints take
    base: str  # normalised display asset, e.g. BTC
    quote: str  # e.g. USD
    ordermin: Decimal
    costmin: Decimal
    tick_size: Decimal
    lot_decimals: int
    pair_decimals: int


def normalise_asset(sym: str) -> str:
    sym = sym.upper()
    return _REVERSE.get(sym, sym)


def to_kraken_pair(pair: str) -> str:
    """``BTC-USD`` / ``BTC/USD`` / ``XBTUSD`` -> ``XBTUSD``."""
    p = pair.strip().upper().replace("/", "-")
    if "-" in p:
        base, _, quote = p.partition("-")
        return f"{_ALIASES.get(base, base)}{_ALIASES.get(quote, quote)}"
    return p


@dataclass(frozen=True)
class Trade:
    price: Decimal
    volume: Decimal
    ts: float
    side: str  # "b" = buyer was the aggressor, "s" = seller was
    trade_id: str


class KrakenPublicClient:
    def __init__(
        self,
        transport: Transport | None = None,
        min_interval_s: float = 1.0,
        rules_ttl_s: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport or self._http
        self._min_interval = min_interval_s
        self._rules_ttl = rules_ttl_s
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._rules: dict[str, tuple[float, PairRules]] = {}

    async def _http(self, path: str, params: dict) -> dict:
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(
                    HOST + path, params=params, allow_redirects=False
                ) as r:
                    if r.status != 200:
                        raise KrakenPublicError(f"Kraken HTTP {r.status} for {path}")
                    return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            # A timeout is not a ClientError; without it one slow reply read as a bug.
            raise KrakenPublicError(
                f"Could not reach Kraken: {e or type(e).__name__}"
            ) from None

    async def _get(self, path: str, **params) -> dict:
        if not path.startswith(PUBLIC_PREFIX) or ".." in path:
            raise KrakenPublicError("Refusing a non-public Kraken path")
        async with self._lock:  # public API is rate limited; space calls out
            wait = self._min_interval - (self._clock() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                data = await self._transport(
                    path, {k: v for k, v in params.items() if v is not None}
                )
            finally:
                self._last_call = self._clock()
        errors = data.get("error") or []
        if errors:
            raise KrakenPublicError("; ".join(errors))
        return data["result"]

    async def pair_rules(self, pair: str) -> PairRules:
        alt = to_kraken_pair(pair)
        hit = self._rules.get(alt)
        if hit and self._clock() - hit[0] < self._rules_ttl:
            return hit[1]
        res = await self._get(PUBLIC_PREFIX + "AssetPairs", pair=alt)
        if not res:
            raise KrakenPublicError(f"Unknown pair {pair}")
        r = next(iter(res.values()))
        wsname = r.get("wsname") or ""
        base, _, quote = wsname.partition("/")
        if not quote:
            raise KrakenPublicError(f"Pair {pair} has no usable name")
        rules = PairRules(
            altname=r.get("altname", alt),
            base=normalise_asset(base),
            quote=normalise_asset(quote),
            ordermin=Decimal(str(r["ordermin"])),
            costmin=Decimal(str(r.get("costmin", "0"))),
            tick_size=Decimal(str(r["tick_size"])),
            lot_decimals=int(r["lot_decimals"]),
            pair_decimals=int(r["pair_decimals"]),
        )
        self._rules[alt] = (self._clock(), rules)
        return rules

    async def depth(self, altname: str, count: int = 500) -> dict:
        """``{"bids": [(price, size)...], "asks": [...], "ts": float}``, best first."""
        res = await self._get(PUBLIC_PREFIX + "Depth", pair=altname, count=count)
        book = next(iter(res.values()))
        conv = lambda rows: [(Decimal(str(p)), Decimal(str(v))) for p, v, *_ in rows]
        ts = max(
            [float(r[2]) for r in book["bids"][:1] + book["asks"][:1]] or [time.time()]
        )
        return {"bids": conv(book["bids"]), "asks": conv(book["asks"]), "ts": ts}

    async def ticker(self, altname: str) -> dict:
        res = await self._get(PUBLIC_PREFIX + "Ticker", pair=altname)
        t = next(iter(res.values()))
        return {
            "bid": Decimal(str(t["b"][0])),
            "ask": Decimal(str(t["a"][0])),
            "last": Decimal(str(t["c"][0])),
            "volume_24h": Decimal(str(t["v"][1])),
            "vwap_24h": Decimal(str(t["p"][1])),
            "trades_24h": int(t["t"][1]),
            "high_24h": Decimal(str(t["h"][1])),
            "low_24h": Decimal(str(t["l"][1])),
        }

    async def trades(
        self, altname: str, since: str | None = None
    ) -> tuple[list[Trade], str]:
        res = await self._get(PUBLIC_PREFIX + "Trades", pair=altname, since=since)
        last = str(res.pop("last"))
        rows = next(iter(res.values()))
        out = [
            Trade(
                Decimal(str(r[0])),
                Decimal(str(r[1])),
                float(r[2]),
                str(r[3]),
                str(r[6]) if len(r) > 6 else "",
            )
            for r in rows
        ]
        return out, last

    async def ohlc(self, altname: str, interval: int = 15) -> list[dict]:
        res = await self._get(PUBLIC_PREFIX + "OHLC", pair=altname, interval=interval)
        res.pop("last", None)
        rows = next(iter(res.values()))
        return [
            {
                "ts": int(r[0]),
                "open": r[1],
                "high": r[2],
                "low": r[3],
                "close": r[4],
                "vwap": r[5],
                "volume": r[6],
            }
            for r in rows
        ]
