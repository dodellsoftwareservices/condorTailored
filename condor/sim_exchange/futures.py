"""Simulated Kraken Futures (USD-margined perpetuals): real public data, simulated account.

Same idea as the spot simulator, one level up: prices, order books, the trade tape,
funding rates and contract rules all come from Kraken Futures' *unauthenticated* public
endpoints (no key, no account), and the account, fills, fees and funding are simulated
here. It adds what spot cannot do: shorting, leverage, funding, liquidation.

Modelled
--------
* **Market orders** walk the real book (after a sampled latency) and pay the taker fee.
* **Limit orders** rest with a queue position and fill only from real trades printed on the
  public tape, paying the maker fee; post-only is enforced.
* **Positions** are signed (long positive, short negative) with a volume-weighted entry.
  Realized P&L settles into the collateral balance; equity adds unrealized P&L at the mark.
* **Funding** accrues continuously on open positions at Kraken's published hourly relative
  funding rate (`relativeFundingRate` on the public ticker): positive means longs pay shorts.
  It uses the *current* rate for the whole time since the last touch, so it is an
  approximation while Condor is down. Verification status: Kraken's Coin-M contract page
  documents hourly settlement and a +/-0.25% hourly bound; the flexible (PF_) contracts we
  simulate list their own bound (`maxRelativeFundingRate`) but I could not find their
  funding page, so continuous accrual at the hourly rate is an assumption that matches
  hourly settlement on average, not a confirmed rule.
* **Liquidation** closes everything at the mark, minus a penalty, if equity falls under the
  maintenance margin. Margin levels use Kraken's more conservative EEA retail schedule
  (10% initial, 5% maintenance) even though the instrument list shows 1% / 0.5% for other
  regions: stricter is the safe side for a paper account, and the owner's leverage cap
  (3x) sits well inside either schedule.
* **Fees** default to Kraken's entry tier (0.02% maker, 0.05% taker; Kraken support page),
  with the same owner-only what-if override as the spot account.

Not modelled: cross-collateral across assets, auto-deleveraging, fee tiers by volume (the
higher tiers are unverified), the funding settlement schedule, our own market impact.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from condor.fsutil import atomic_write_json

from . import engine as E
from .kraken_public import KrakenPublicError

D = Decimal
HOST = "https://futures.kraken.com"
PREFIX = "/derivatives/api/v3/"
CHART_PREFIX = "/api/charts/v1/trade/"
PUBLIC_ENDPOINTS = frozenset({"tickers", "orderbook", "history", "instruments"})
CANDLE_MINUTES = (1, 5, 15, 30, 60, 240, 1440)

DEFAULT_MAKER = D("0.0002")
DEFAULT_TAKER = D("0.0005")
INITIAL_MARGIN = D("0.10")
MAINTENANCE_MARGIN = D("0.05")
LIQUIDATION_PENALTY = D("0.005")
MAX_FUNDING_HOURS = D(72)
DEFAULT_START_CASH = D("50000")  # per desk; the owner funds each desk with $50k
_ALIASES = {"BTC": "XBT", "DOGE": "XDG"}


def to_symbol(name: str) -> str:
    """BTC-USD, btc/usd, BTC or PF_XBTUSD all mean PF_XBTUSD."""
    s = name.strip().upper().replace("/", "-")
    if s.startswith("PF_"):
        return s
    base = s.split("-")[0]
    if not base.isalnum():
        raise KrakenPublicError(f"Not a perpetual name: {name}")
    return f"PF_{_ALIASES.get(base, base)}USD"


def _ts(value: str) -> float:
    head, _, tail = value.partition(".")
    frac = "".join(ch for ch in tail if ch.isdigit())[:6]
    text = f"{head}.{frac or '0'}+00:00"
    return datetime.fromisoformat(text).timestamp()


# ── public client ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class FutRules:
    symbol: str
    base: str
    tick_size: Decimal
    lot_decimals: int
    max_position: Decimal

    @property
    def min_qty(self) -> Decimal:
        return D(1).scaleb(-self.lot_decimals)


@dataclass(frozen=True)
class FutTrade:
    price: Decimal
    size: Decimal
    ts: float
    side: str  # taker side: buy | sell
    trade_id: str


class KrakenFuturesClient:
    """Read-only. The only paths it can build are Kraken Futures' public market data."""

    def __init__(
        self,
        transport: Callable[[str, dict], Awaitable[dict]] | None = None,
        *,
        min_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        ticker_ttl_s: float = 4.0,
    ) -> None:
        self._transport = transport or self._http
        self._min_interval = min_interval_s
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._rules: tuple[float, dict[str, FutRules]] | None = None
        self._tickers: tuple[float, dict[str, dict]] | None = None
        self._ticker_ttl = ticker_ttl_s

    async def _http(self, path: str, params: dict) -> dict:
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(
                    HOST + path, params=params, allow_redirects=False
                ) as r:
                    if r.status != 200:
                        raise KrakenPublicError(
                            f"Kraken Futures HTTP {r.status} for {path}"
                        )
                    return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise KrakenPublicError(
                f"Could not reach Kraken Futures: {e or type(e).__name__}"
            ) from None

    async def _call(self, path: str, **params) -> dict:
        async with self._lock:  # public API is rate limited; space calls out
            wait = self._min_interval - (self._clock() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._transport(
                    path, {k: v for k, v in params.items() if v is not None}
                )
            finally:
                self._last_call = self._clock()

    async def _get(self, endpoint: str, **params) -> dict:
        if endpoint not in PUBLIC_ENDPOINTS:
            raise KrakenPublicError("Refusing a non-public Kraken Futures endpoint")
        data = await self._call(PREFIX + endpoint, **params)
        if data.get("result") != "success":
            raise KrakenPublicError(str(data.get("error") or data)[:200])
        return data

    async def rules(self, symbol: str) -> FutRules:
        if not self._rules or self._clock() - self._rules[0] > 6 * 3600:
            data = await self._get("instruments")
            table: dict[str, FutRules] = {}
            for i in data.get("instruments", []):
                if (
                    i.get("type") != "flexible_futures"
                    or not i.get("tradeable")
                    or i.get("quote") != "USD"
                ):
                    continue
                table[i["symbol"]] = FutRules(
                    symbol=i["symbol"],
                    base=str(i.get("base", "")).upper().replace("XBT", "BTC"),
                    tick_size=D(str(i["tickSize"])),
                    lot_decimals=int(i.get("contractValueTradePrecision", 4)),
                    max_position=D(str(i.get("maxPositionSize", "0"))),
                )
            self._rules = (self._clock(), table)
        r = self._rules[1].get(symbol)
        if not r:
            raise KrakenPublicError(f"Unknown or untradeable perpetual {symbol}")
        return r

    async def ticker(self, symbol: str) -> dict:
        if not self._tickers or self._clock() - self._tickers[0] > self._ticker_ttl:
            data = await self._get("tickers")
            self._tickers = (
                self._clock(),
                {t["symbol"]: t for t in data.get("tickers", [])},
            )
        t = self._tickers[1].get(symbol)
        if not t:
            raise KrakenPublicError(f"No ticker for {symbol}")
        if t.get("suspended"):
            raise KrakenPublicError(f"{symbol} is suspended")
        return {
            "bid": D(str(t["bid"])),
            "ask": D(str(t["ask"])),
            "last": D(str(t["last"])),
            "mark": D(str(t["markPrice"])),
            "index": D(str(t.get("indexPrice", t["markPrice"]))),
            "funding_hourly": D(str(t.get("relativeFundingRate", 0))),
            "funding_predicted_hourly": D(
                str(t.get("relativeFundingRatePrediction", 0))
            ),
            "volume_24h": D(str(t.get("vol24h", 0))),
            "open_interest": D(str(t.get("openInterest", 0))),
            "high_24h": D(str(t.get("high24h", 0))),
            "low_24h": D(str(t.get("low24h", 0))),
        }

    async def orderbook(self, symbol: str) -> dict:
        data = await self._get("orderbook", symbol=symbol)
        book = data["orderBook"]
        bids = sorted(
            ((D(str(p)), D(str(s))) for p, s in book["bids"] if s), reverse=True
        )
        asks = sorted((D(str(p)), D(str(s))) for p, s in book["asks"] if s)
        return {"bids": bids, "asks": asks, "ts": time.time()}

    async def trades(self, symbol: str) -> list[FutTrade]:
        data = await self._get("history", symbol=symbol)
        out = [
            FutTrade(
                D(str(t["price"])),
                D(str(t["size"])),
                _ts(t["time"]),
                t["side"],
                str(t.get("uid") or t.get("trade_id")),
            )
            for t in data.get("history", [])
            if t.get("type", "fill") == "fill"
        ]
        return sorted(out, key=lambda t: t.ts)

    async def candles(self, symbol: str, minutes: int, count: int) -> list[dict]:
        if minutes not in CANDLE_MINUTES:
            raise KrakenPublicError(f"candle_minutes must be one of {CANDLE_MINUTES}")
        res = (
            f"{minutes}m"
            if minutes < 60
            else f"{minutes // 60}h" if minutes < 1440 else "1d"
        )
        since = int(time.time()) - minutes * 60 * (min(count, 200) + 1)
        data = await self._call(f"{CHART_PREFIX}{symbol}/{res}", **{"from": since})
        return [
            {
                "ts": c["time"] // 1000,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c["volume"],
            }
            for c in data.get("candles", [])
        ][-count:]


# ── account ─────────────────────────────────────────────────────────────


@dataclass
class FPos:
    qty: Decimal = D("0")  # signed: long > 0, short < 0
    entry: Decimal = D("0")


@dataclass
class FLimits:
    """Hard caps the model cannot raise: only the owner edits these."""

    max_order_notional: Decimal = D("2500")
    max_position_notional: Decimal = D("5000")
    max_leverage: Decimal = D("3")
    max_open_orders: int = 10


@dataclass
class FAccount:
    name: str
    created: float
    start_cash: Decimal
    cash: Decimal  # collateral balance: realized P&L, fees and funding settle here
    positions: dict[str, FPos] = field(default_factory=dict)
    orders: dict[str, E.Order] = field(default_factory=dict)
    fills: list[E.Fill] = field(default_factory=list)
    limits: FLimits = field(default_factory=FLimits)
    costs: E.CostConfig = field(default_factory=E.CostConfig)
    fees_paid: Decimal = D("0")
    slippage_cost: Decimal = D("0")
    realized_pnl: Decimal = D("0")  # gross trading P&L, before fees and funding
    funding_paid: Decimal = D("0")  # positive = we paid, negative = we were paid
    volume: Decimal = D("0")
    liquidations: int = 0
    last_funding_ts: float = 0.0
    client_ids: dict[str, str] = field(default_factory=dict)
    seen_trade_keys: dict[str, list[str]] = field(default_factory=dict)
    fee_override: dict | None = None

    @classmethod
    def new(cls, name: str, start_cash: Decimal, now: float) -> "FAccount":
        return cls(
            name=name,
            created=now,
            start_cash=start_cash,
            cash=start_cash,
            last_funding_ts=now,
        )


def account_to_dict(a: FAccount) -> dict:
    return E._enc(asdict(a))


def account_from_dict(raw: dict) -> FAccount:
    a = FAccount(
        name=raw["name"],
        created=raw["created"],
        start_cash=D(raw["start_cash"]),
        cash=D(raw["cash"]),
    )
    a.positions = {
        k: FPos(D(v["qty"]), D(v["entry"])) for k, v in raw.get("positions", {}).items()
    }
    a.orders = {k: E._build(E.Order, v) for k, v in raw.get("orders", {}).items()}
    a.fills = [E._build(E.Fill, v) for v in raw.get("fills", [])]
    a.limits = E._build(FLimits, raw.get("limits", {}))
    a.costs = E._build(E.CostConfig, raw.get("costs", {}))
    for name in (
        "fees_paid",
        "slippage_cost",
        "realized_pnl",
        "funding_paid",
        "volume",
    ):
        setattr(a, name, D(raw.get(name, "0")))
    a.liquidations = int(raw.get("liquidations", 0))
    a.last_funding_ts = float(raw.get("last_funding_ts", 0.0))
    a.client_ids = dict(raw.get("client_ids", {}))
    a.seen_trade_keys = {k: list(v) for k, v in raw.get("seen_trade_keys", {}).items()}
    a.fee_override = raw.get("fee_override")
    return a


# ── pure engine ─────────────────────────────────────────────────────────


def fee_rates(acct: FAccount) -> tuple[Decimal, Decimal]:
    ov = acct.fee_override
    if ov:
        return D(ov["maker"]) / 100, D(ov["taker"]) / 100
    return DEFAULT_MAKER, DEFAULT_TAKER


def unrealized(acct: FAccount, marks: dict[str, Decimal]) -> Decimal:
    return sum(
        (p.qty * (marks[s] - p.entry) for s, p in acct.positions.items() if p.qty),
        D("0"),
    )


def equity(acct: FAccount, marks: dict[str, Decimal]) -> Decimal:
    return acct.cash + unrealized(acct, marks)


def gross_exposure(acct: FAccount, marks: dict[str, Decimal]) -> Decimal:
    return sum(
        (abs(p.qty) * marks[s] for s, p in acct.positions.items() if p.qty), D("0")
    )


def _signed(side: str, qty: Decimal) -> Decimal:
    return qty if side == "buy" else -qty


def _pending_increase(acct: FAccount, symbol: str | None = None) -> Decimal:
    """Notional that resting orders could add if they all filled. Reduce-only ones add none."""
    total = D("0")
    for o in acct.orders.values():
        if o.status != "open" or (symbol and o.pair != symbol) or o.reduce_only:
            continue
        total += o.remaining * (o.price or o.arrival_mid)
    return total


def _check(
    acct: FAccount,
    rules: FutRules,
    marks: dict[str, Decimal],
    *,
    side: str,
    qty: Decimal,
    price: Decimal,
    reduce_only: bool,
    new_open_order: bool,
) -> None:
    lim = acct.limits
    notional = qty * price
    if notional > lim.max_order_notional:
        raise E.Rejected(
            f"Order notional ${notional:.2f} exceeds the ${lim.max_order_notional} per-order limit"
        )
    if new_open_order:
        open_n = sum(1 for o in acct.orders.values() if o.status == "open")
        if open_n >= lim.max_open_orders:
            raise E.Rejected(
                f"Already {open_n} open orders; the limit is {lim.max_open_orders}"
            )
    pos = acct.positions.get(rules.symbol, FPos())
    signed = _signed(side, qty)
    if reduce_only:
        if pos.qty == 0 or (pos.qty > 0) == (signed > 0) or qty > abs(pos.qty):
            raise E.Rejected(
                f"reduce_only: the order must shrink your {pos.qty} {rules.symbol} position, not grow or flip it"
            )
        return
    after = pos.qty + signed
    if abs(after) <= abs(pos.qty):
        return  # shrinking risk is always allowed
    mark = marks[rules.symbol]
    if abs(after) * mark > lim.max_position_notional:
        raise E.Rejected(
            f"{rules.symbol} position would be ${abs(after) * mark:.2f}, over the ${lim.max_position_notional} per-contract limit"
        )
    if abs(after) > rules.max_position:
        raise E.Rejected(
            f"Position {abs(after)} is over Kraken's {rules.max_position} maximum for {rules.symbol}"
        )
    eq = equity(acct, marks)
    gross_after = (
        gross_exposure(acct, marks)
        + (abs(after) - abs(pos.qty)) * mark
        + _pending_increase(acct)
    )
    if eq <= 0 or gross_after > eq * lim.max_leverage:
        raise E.Rejected(
            f"Exposure would be ${gross_after:.2f}, over {lim.max_leverage}x equity (${eq:.2f})"
        )
    if gross_after * INITIAL_MARGIN > eq:
        raise E.Rejected(
            f"Initial margin ${gross_after * INITIAL_MARGIN:.2f} exceeds equity ${eq:.2f}"
        )


def _apply_fill(
    acct: FAccount,
    order: E.Order,
    *,
    qty: Decimal,
    price: Decimal,
    liquidity: str,
    fee_rate: Decimal,
    ts: float,
    latency_ms: int,
) -> E.Fill:
    sym = order.pair
    pos = acct.positions.get(sym, FPos())
    signed = _signed(order.side, qty)
    realized = D("0")
    if pos.qty == 0 or (pos.qty > 0) == (signed > 0):
        total = abs(pos.qty) + qty
        entry = (abs(pos.qty) * pos.entry + qty * price) / total
        new = FPos(pos.qty + signed, entry)
    else:
        closing = min(qty, abs(pos.qty))
        realized = closing * (price - pos.entry) * (1 if pos.qty > 0 else -1)
        new_qty = pos.qty + signed
        if new_qty == 0:
            new = FPos()
        elif (new_qty > 0) != (pos.qty > 0):
            new = FPos(
                new_qty, price
            )  # flipped through zero: the remainder opens at this price
        else:
            new = FPos(new_qty, pos.entry)
    if new.qty == 0:
        acct.positions.pop(sym, None)
    else:
        acct.positions[sym] = new
    notional = E.q5(qty * price)
    fee = E.q5(notional * fee_rate)
    slip = E.q5(
        (price - order.arrival_mid) * qty
        if order.side == "buy"
        else (order.arrival_mid - price) * qty
    )
    acct.cash += realized - fee
    acct.fees_paid += fee
    acct.slippage_cost += slip
    acct.realized_pnl += realized
    acct.volume += notional
    order.remaining -= qty
    order.filled += qty
    fill = E.Fill(
        id=uuid.uuid4().hex[:12],
        order_id=order.id,
        ts=ts,
        pair=sym,
        base=order.base,
        side=order.side,
        price=price,
        qty=qty,
        notional=notional,
        fee=fee,
        liquidity=liquidity,
        arrival_mid=order.arrival_mid,
        slippage_cost=slip,
        realized_pnl=E.q5(realized),
        latency_ms=latency_ms,
    )
    acct.fills.append(fill)
    return fill


def _new_order(
    rules: FutRules,
    *,
    side,
    type_,
    qty,
    price,
    post_only,
    reduce_only,
    now,
    mid,
    client_id,
) -> E.Order:
    o = E.Order(
        id=uuid.uuid4().hex[:12],
        client_id=client_id,
        pair=rules.symbol,
        base=rules.base,
        side=side,
        type=type_,
        qty=qty,
        price=price,
        remaining=qty,
        post_only=post_only,
        status="open",
        created=now,
        arrival_mid=mid,
        reduce_only=reduce_only,
    )
    return o


def _validate(rules: FutRules, side: str, qty: Decimal) -> Decimal:
    if side not in ("buy", "sell"):
        raise E.Rejected("side must be buy or sell")
    want = E.floor_to(qty, rules.lot_decimals)
    if want < rules.min_qty:
        raise E.Rejected(
            f"Quantity {qty} is below the {rules.min_qty} {rules.base} minimum order size"
        )
    return want


def place_market(
    acct: FAccount,
    rules: FutRules,
    *,
    side: str,
    qty: Decimal,
    book: dict,
    marks: dict[str, Decimal],
    now: float,
    client_id: str = "",
    reduce_only: bool = False,
    latency_ms: int = 0,
):
    dup = E._dedupe(acct, client_id)
    if dup:
        return dup, [f for f in acct.fills if f.order_id == dup.id], True
    want = _validate(rules, side, qty)
    mid = E.mid_of(book)
    marks = {**marks, rules.symbol: marks.get(rules.symbol, mid)}
    levels = book["asks"] if side == "buy" else book["bids"]
    w = E.walk_book(levels, qty=want, limit_price=None, side=side)
    if w.qty <= 0:
        raise E.Rejected("No liquidity on the book to fill this order")
    _check(
        acct,
        rules,
        marks,
        side=side,
        qty=w.qty,
        price=w.avg,
        reduce_only=reduce_only,
        new_open_order=False,
    )
    order = _new_order(
        rules,
        side=side,
        type_="market",
        qty=want,
        price=None,
        post_only=False,
        reduce_only=reduce_only,
        now=now,
        mid=mid,
        client_id=client_id,
    )
    _, taker = fee_rates(acct)
    fill = _apply_fill(
        acct,
        order,
        qty=w.qty,
        price=E.q5(w.avg),
        liquidity="taker",
        fee_rate=taker,
        ts=now,
        latency_ms=latency_ms,
    )
    order.remaining = D("0")
    order.status = "filled" if w.qty == want else "partial_cancelled"
    acct.orders[order.id] = order
    if client_id:
        acct.client_ids[client_id] = order.id
    return order, [fill], False


def place_limit(
    acct: FAccount,
    rules: FutRules,
    *,
    side: str,
    qty: Decimal,
    price: Decimal,
    book: dict,
    marks: dict[str, Decimal],
    now: float,
    client_id: str = "",
    post_only: bool = False,
    reduce_only: bool = False,
    latency_ms: int = 0,
):
    dup = E._dedupe(acct, client_id)
    if dup:
        return dup, [f for f in acct.fills if f.order_id == dup.id], True
    want = _validate(rules, side, qty)
    if (price / rules.tick_size) != (price / rules.tick_size).to_integral_value():
        raise E.Rejected(
            f"Price {price} is not a multiple of the {rules.tick_size} tick size"
        )
    mid = E.mid_of(book)
    marks = {**marks, rules.symbol: marks.get(rules.symbol, mid)}
    levels = book["asks"] if side == "buy" else book["bids"]
    best = levels[0][0] if levels else None
    marketable = best is not None and (
        (side == "buy" and price >= best) or (side == "sell" and price <= best)
    )
    if marketable and post_only:
        raise E.Rejected("Post-only order would cross the book and take liquidity")
    _check(
        acct,
        rules,
        marks,
        side=side,
        qty=want,
        price=price,
        reduce_only=reduce_only,
        new_open_order=True,
    )
    order = _new_order(
        rules,
        side=side,
        type_="limit",
        qty=want,
        price=price,
        post_only=post_only,
        reduce_only=reduce_only,
        now=now,
        mid=mid,
        client_id=client_id,
    )
    fills: list[E.Fill] = []
    if marketable:
        w = E.walk_book(levels, qty=want, limit_price=price, side=side)
        if w.qty > 0:
            _, taker = fee_rates(acct)
            fills.append(
                _apply_fill(
                    acct,
                    order,
                    qty=w.qty,
                    price=E.q5(w.avg),
                    liquidity="taker",
                    fee_rate=taker,
                    ts=now,
                    latency_ms=latency_ms,
                )
            )
    if order.remaining > 0:
        own = book["bids"] if side == "buy" else book["asks"]
        order.queue_ahead = (
            next((s for p, s in own if p == price), D("0"))
            if not marketable
            else D("0")
        )
    else:
        order.status = "filled"
    acct.orders[order.id] = order
    if client_id:
        acct.client_ids[client_id] = order.id
    return order, fills, False


def on_trades(
    acct: FAccount, rules: FutRules, trades: list[FutTrade], now: float
) -> list[E.Fill]:
    """Feed the real public tape to resting orders, oldest trade first."""
    out: list[E.Fill] = []
    recent = acct.seen_trade_keys.setdefault(rules.symbol, [])
    seen = set(recent)
    maker, _ = fee_rates(acct)
    for t in sorted(trades, key=lambda t: t.ts):
        if t.trade_id in seen:
            continue
        seen.add(t.trade_id)
        recent.append(t.trade_id)
        rem_vol = t.size
        for o in sorted(
            (
                o
                for o in acct.orders.values()
                if o.status == "open" and o.pair == rules.symbol
            ),
            key=lambda o: o.created,
        ):
            if t.ts < o.created:
                continue
            if o.side == "buy" and not (t.side == "sell" and t.price <= o.price):
                continue
            if o.side == "sell" and not (t.side == "buy" and t.price >= o.price):
                continue
            if t.price == o.price:
                eaten = min(o.queue_ahead, rem_vol)
                o.queue_ahead -= eaten
                rem_vol -= eaten
                got = min(o.remaining, rem_vol)
                rem_vol -= got
            else:
                got = o.remaining
            if got <= 0:
                continue
            out.append(
                _apply_fill(
                    acct,
                    o,
                    qty=got,
                    price=o.price,
                    liquidity="maker",
                    fee_rate=maker,
                    ts=t.ts,
                    latency_ms=0,
                )
            )
            if o.remaining <= 0:
                o.status = "filled"
    if len(recent) > 4096:
        del recent[:-4096]
    return out


def accrue_funding(
    acct: FAccount, hourly: dict[str, Decimal], marks: dict[str, Decimal], now: float
) -> Decimal:
    """Charge or credit funding since the last touch. Returns what we paid (negative = received)."""
    hours = min(D(str(max(now - acct.last_funding_ts, 0))) / D(3600), MAX_FUNDING_HOURS)
    acct.last_funding_ts = now
    paid = D("0")
    for sym, p in acct.positions.items():
        if p.qty and sym in hourly and sym in marks:
            paid += (
                p.qty * marks[sym] * hourly[sym] * hours
            )  # long pays when the rate is positive
    paid = E.q5(paid)
    acct.cash -= paid
    acct.funding_paid += paid
    return paid


def liquidate_if_needed(
    acct: FAccount, marks: dict[str, Decimal], now: float
) -> list[str]:
    """Close everything at the mark, minus a penalty, if equity is under maintenance margin."""
    if not acct.positions:
        return []
    need = gross_exposure(acct, marks) * MAINTENANCE_MARGIN
    if equity(acct, marks) >= need:
        return []
    closed = []
    for o in acct.orders.values():
        if o.status == "open":
            o.status = "cancelled" if o.filled == 0 else "partial_cancelled"
    for sym, p in list(acct.positions.items()):
        side = "sell" if p.qty > 0 else "buy"
        mark = marks[sym]
        price = (
            mark * (1 - LIQUIDATION_PENALTY)
            if side == "sell"
            else mark * (1 + LIQUIDATION_PENALTY)
        )
        o = E.Order(
            id="liq-" + uuid.uuid4().hex[:8],
            client_id="",
            pair=sym,
            base=sym.removeprefix("PF_").removesuffix("USD").replace("XBT", "BTC"),
            side=side,
            type="liquidation",
            qty=abs(p.qty),
            price=None,
            remaining=abs(p.qty),
            post_only=False,
            status="open",
            created=now,
            arrival_mid=mark,
        )
        _apply_fill(
            acct,
            o,
            qty=abs(p.qty),
            price=E.q5(price),
            liquidity="taker",
            fee_rate=fee_rates(acct)[1],
            ts=now,
            latency_ms=0,
        )
        o.status = "filled"
        acct.orders[o.id] = o
        closed.append(sym)
    acct.liquidations += 1
    return closed


def portfolio(acct: FAccount, marks: dict[str, Decimal], now: float) -> dict:
    eq = equity(acct, marks)
    rows = []
    for sym, p in sorted(acct.positions.items()):
        if not p.qty:
            continue
        mark = marks[sym]
        rows.append(
            {
                "asset": sym.removeprefix("PF_")
                .removesuffix("USD")
                .replace("XBT", "BTC"),
                "symbol": sym,
                "side": "long" if p.qty > 0 else "short",
                "qty": str(p.qty),
                "avg_cost": str(E.q5(p.entry)),
                "price": str(E.q5(mark)),
                "value": str(E.q5(abs(p.qty) * mark)),
                "unrealized_pnl": str(E.q5(p.qty * (mark - p.entry))),
            }
        )
    gross = gross_exposure(acct, marks)
    return {
        "equity": str(E.q5(eq)),
        "cash": str(E.q5(acct.cash)),
        "start_cash": str(acct.start_cash),
        "total_return_pct": str(((eq / acct.start_cash - 1) * 100).quantize(D("0.01"))),
        "gross_exposure": str(E.q5(gross)),
        "leverage": str((gross / eq).quantize(D("0.01"))) if eq > 0 else "0",
        "maintenance_margin": str(E.q5(gross * MAINTENANCE_MARGIN)),
        "positions": rows,
        "open_orders": sum(1 for o in acct.orders.values() if o.status == "open"),
        "volume_30d": str(
            E.q5(
                sum(
                    (f.notional for f in acct.fills if f.ts >= now - E.THIRTY_DAYS),
                    D("0"),
                )
            )
        ),
        "funding_paid": str(E.q5(acct.funding_paid)),
        "liquidations": acct.liquidations,
    }


def cost_report(
    acct: FAccount, marks: dict[str, Decimal], now: float, llm_requests: int = 0
) -> dict:
    eq = equity(acct, marks)
    days = D(str(max(now - acct.created, 0))) / D(str(E.DAY))
    llm = acct.costs.llm_cost_per_request_usd * llm_requests
    fixed = acct.costs.fixed_daily_usd * days
    equity_pnl = eq - acct.start_cash
    net = equity_pnl - llm - fixed
    gross = equity_pnl + acct.fees_paid + acct.slippage_cost + acct.funding_paid
    vol = acct.volume
    bps = lambda x: str((x / vol * E.BPS).quantize(D("0.1"))) if vol else "0"
    return {
        "net_pnl_after_all_costs": str(E.q5(net)),
        "equity_pnl": str(E.q5(equity_pnl)),
        "gross_pnl_before_execution_costs": str(E.q5(gross)),
        "costs": {
            "exchange_fees": str(E.q5(acct.fees_paid)),
            "slippage_vs_arrival_mid": str(E.q5(acct.slippage_cost)),
            "funding_paid": str(E.q5(acct.funding_paid)),
            "llm_requests": llm_requests,
            "llm_cost": str(E.q5(llm)),
            "fixed_running_cost": str(E.q5(fixed)),
            "total_execution_and_operating": str(
                E.q5(
                    acct.fees_paid
                    + acct.slippage_cost
                    + acct.funding_paid
                    + llm
                    + fixed
                )
            ),
        },
        "traded_volume": str(E.q5(vol)),
        "fees_bps_of_volume": bps(acct.fees_paid),
        "fills": {
            "taker": sum(f.liquidity == "taker" for f in acct.fills),
            "maker": sum(f.liquidity == "maker" for f in acct.fills),
        },
        "realized_pnl": str(E.q5(acct.realized_pnl)),
        "note": "equity_pnl already includes fees, slippage and funding. LLM and fixed costs are not in equity, so they are subtracted for net.",
    }


# ── service ─────────────────────────────────────────────────────────────


def _order_dict(o: E.Order) -> dict:
    return {
        "id": o.id,
        "client_order_id": o.client_id,
        "symbol": o.pair,
        "side": o.side,
        "type": o.type,
        "qty": str(o.qty),
        "price": None if o.price is None else str(o.price),
        "filled": str(o.filled),
        "remaining": str(o.remaining),
        "status": o.status,
        "post_only": o.post_only,
        "reduce_only": o.reduce_only,
        "queue_ahead": str(o.queue_ahead),
        "created": o.created,
    }


def _fill_dict(f: E.Fill) -> dict:
    return {
        "id": f.id,
        "order_id": f.order_id,
        "ts": f.ts,
        "symbol": f.pair,
        "side": f.side,
        "price": str(f.price),
        "qty": str(f.qty),
        "notional": str(f.notional),
        "fee": str(f.fee),
        "liquidity": f.liquidity,
        "realized_pnl": str(f.realized_pnl),
        "slippage_cost": str(f.slippage_cost),
    }


class FuturesService:
    """One simulated futures account, persisted to one file, mutated under one lock."""

    def __init__(
        self,
        client: KrakenFuturesClient,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        start_cash: Decimal = DEFAULT_START_CASH,
        latency_ms: int = 250,
        rng: random.Random | None = None,
        ledger_dir: Path | None = None,
    ) -> None:
        self.client = client
        self.path = path
        self._clock = clock
        self._sleep = sleeper
        self.start_cash = start_cash
        self.latency_ms = latency_ms
        self._rng = rng or random.Random()
        self._ledger_dir = ledger_dir
        self._lock = asyncio.Lock()

    def _load(self) -> FAccount:
        if self.path.exists():
            return account_from_dict(json.loads(self.path.read_text()))
        a = FAccount.new("kraken_futures", self.start_cash, self._clock())
        self._save(a)
        return a

    def _save(self, a: FAccount) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, account_to_dict(a))

    def _latency(self) -> int:
        ms = self._rng.lognormvariate(math.log(max(self.latency_ms, 1)), 0.4)
        return int(min(max(ms, 20), 2000))

    async def _marks(
        self, acct: FAccount, known: dict[str, Decimal] | None = None
    ) -> tuple[dict, dict]:
        marks, hourly = dict(known or {}), {}
        for sym, p in acct.positions.items():
            if p.qty:
                t = await self.client.ticker(sym)
                marks.setdefault(sym, t["mark"])
                hourly[sym] = t["funding_hourly"]
        return marks, hourly

    async def _touch(
        self, acct: FAccount, known: dict[str, Decimal] | None = None
    ) -> dict:
        """Mark to market, accrue funding, liquidate if under margin. Returns the marks."""
        marks, hourly = await self._marks(acct, known)
        now = self._clock()
        accrue_funding(acct, hourly, marks, now)
        liquidate_if_needed(acct, marks, now)
        return marks

    # reads

    async def snapshot(
        self, symbol: str, candles: int = 0, candle_minutes: int = 15
    ) -> dict:
        sym = to_symbol(symbol)
        rules = await self.client.rules(sym)
        t = await self.client.ticker(sym)
        book = await self.client.orderbook(sym)
        mid = E.mid_of(book)
        async with self._lock:
            acct = self._load()
        maker, taker = fee_rates(acct)
        pos = acct.positions.get(sym, FPos())
        spread = book["asks"][0][0] - book["bids"][0][0]

        def depth_within(levels, side, bps):
            lim = (
                mid * (1 + D(bps) / E.BPS)
                if side == "ask"
                else mid * (1 - D(bps) / E.BPS)
            )
            tot = D("0")
            for p, s in levels:
                if (side == "ask" and p > lim) or (side == "bid" and p < lim):
                    break
                tot += p * s
            return str(E.q5(tot))

        fmt = lambda x: None if x is None else str(x.quantize(D("0.01")))
        out = {
            "symbol": sym,
            "base": rules.base,
            "bid": str(book["bids"][0][0]),
            "ask": str(book["asks"][0][0]),
            "mid": str(mid),
            "mark": str(t["mark"]),
            "index": str(t["index"]),
            "spread_bps": str((spread / mid * E.BPS).quantize(D("0.01"))),
            "funding": {
                "hourly_pct": str((t["funding_hourly"] * 100).quantize(D("0.00001"))),
                "annualized_pct": str(
                    (t["funding_hourly"] * 24 * 365 * 100).quantize(D("0.01"))
                ),
                "predicted_hourly_pct": str(
                    (t["funding_predicted_hourly"] * 100).quantize(D("0.00001"))
                ),
                "note": "Positive: longs pay shorts. It accrues on every open position, so a carry trade must beat fees too.",
            },
            "volume_24h": str(t["volume_24h"]),
            "open_interest": str(t["open_interest"]),
            "high_24h": str(t["high_24h"]),
            "low_24h": str(t["low_24h"]),
            "depth_usd_within_bps": {
                str(b): {
                    "bids": depth_within(book["bids"], "bid", b),
                    "asks": depth_within(book["asks"], "ask", b),
                }
                for b in (10, 25, 50)
            },
            "market_order_impact_bps": {
                str(n): {
                    "buy": fmt(E.impact_bps(book["asks"], D(n), mid, "buy")),
                    "sell": fmt(E.impact_bps(book["bids"], D(n), mid, "sell")),
                }
                for n in (500, 1000, 2500)
            },
            "your_fee_rates_pct": {
                "maker": str(maker * 100),
                "taker": str(taker * 100),
            },
            "round_trip_cost_bps": {
                "taker_both_ways_before_slippage": str(
                    ((taker * 2) * E.BPS).quantize(D("0.1"))
                ),
                "maker_both_ways_before_slippage": str(
                    ((maker * 2) * E.BPS).quantize(D("0.1"))
                ),
                "note": "An edge must beat this plus spread, impact and funding before a trade is worth taking.",
            },
            "your_position": {"qty": str(pos.qty), "entry": str(pos.entry)},
            "rules": {
                "tick_size": str(rules.tick_size),
                "min_order_qty": str(rules.min_qty),
                "lot_decimals": rules.lot_decimals,
                "shorting": "allowed: sell to open a short, buy to close it",
                "max_leverage_owner_cap": str(acct.limits.max_leverage),
            },
            "as_of": book["ts"],
        }
        if candles > 0:
            out["candles"] = {
                "minutes": candle_minutes,
                "rows": await self.client.candles(sym, candle_minutes, candles),
            }
        return out

    async def candles(self, symbol: str, candle_minutes: int, count: int) -> list[dict]:
        """Just the candles, without the ticker and order book a snapshot also pulls."""
        return await self.client.candles(to_symbol(symbol), candle_minutes, count)

    # orders

    async def place(
        self,
        *,
        symbol: str,
        side: str,
        type: str = "market",
        qty: Decimal | None = None,
        price: Decimal | None = None,
        post_only: bool = False,
        reduce_only: bool = False,
        client_order_id: str = "",
    ) -> dict:
        sym = to_symbol(symbol)
        async with self._lock:
            acct = self._load()
            rules = await self.client.rules(sym)
            if type not in ("market", "limit"):
                raise E.Rejected("type must be market or limit")
            if qty is None or (type == "limit" and price is None):
                raise E.Rejected("Give qty, and a price for a limit order")
            latency = (
                0
                if client_order_id and client_order_id in acct.client_ids
                else self._latency()
            )
            if latency:
                await self._sleep(latency / 1000)
            book = await self.client.orderbook(sym)
            marks = await self._touch(
                acct, {sym: (await self.client.ticker(sym))["mark"]}
            )
            if sym not in acct.positions and not marks.get(sym):
                marks[sym] = E.mid_of(book)
            now = self._clock()
            if type == "market":
                order, fills, dup = place_market(
                    acct,
                    rules,
                    side=side,
                    qty=qty,
                    book=book,
                    marks=marks,
                    now=now,
                    client_id=client_order_id,
                    reduce_only=reduce_only,
                    latency_ms=latency,
                )
            else:
                order, fills, dup = place_limit(
                    acct,
                    rules,
                    side=side,
                    qty=qty,
                    price=price,
                    book=book,
                    marks=marks,
                    now=now,
                    client_id=client_order_id,
                    post_only=post_only,
                    reduce_only=reduce_only,
                    latency_ms=latency,
                )
            self._save(acct)
            pos = acct.positions.get(sym, FPos())
            return {
                "duplicate": dup,
                "order": _order_dict(order),
                "fills": [_fill_dict(f) for f in fills],
                "cash": str(E.q5(acct.cash)),
                "position": {"qty": str(pos.qty), "entry": str(pos.entry)},
                "simulated_latency_ms": latency,
            }

    async def cancel(self, order_id: str) -> dict:
        async with self._lock:
            acct = self._load()
            o = E.cancel(acct, order_id)
            self._save(acct)
            return {"order": _order_dict(o)}

    async def advance(self) -> dict:
        async with self._lock:
            return await self._advance(self._load())

    async def _advance(self, acct: FAccount) -> dict:
        new = []
        for sym in sorted({o.pair for o in acct.orders.values() if o.status == "open"}):
            rules = await self.client.rules(sym)
            new += on_trades(acct, rules, await self.client.trades(sym), self._clock())
        await self._touch(acct)
        self._save(acct)
        return {
            "filled": [_fill_dict(f) for f in new],
            "open_orders": sum(1 for o in acct.orders.values() if o.status == "open"),
        }

    async def orders(self, limit: int = 20) -> dict:
        async with self._lock:
            acct = self._load()
            adv = (
                await self._advance(acct)
                if any(o.status == "open" for o in acct.orders.values())
                else {"filled": []}
            )
            rows = sorted(acct.orders.values(), key=lambda o: o.created, reverse=True)
            return {
                "open": [_order_dict(o) for o in rows if o.status == "open"],
                "recent": [_order_dict(o) for o in rows if o.status != "open"][:limit],
                "fills_just_processed": adv["filled"],
            }

    async def portfolio(self) -> dict:
        async with self._lock:
            acct = self._load()
            if any(o.status == "open" for o in acct.orders.values()):
                await self._advance(acct)
            marks = await self._touch(acct)
            self._save(acct)
            return portfolio(acct, marks, self._clock())

    async def dashboard(self) -> dict:
        """Equity, exposure and settled outcomes for the paper dashboard.

        Same shape as the spot ``SimService.dashboard`` so the dashboard route can
        treat every Kraken desk alike. Marks positions and accrues funding (so equity
        is not overstated) but never advances resting orders.
        """
        async with self._lock:
            acct = self._load()
            marks = await self._touch(acct)
            self._save(acct)
            report = portfolio(acct, marks, self._clock())
            # A fill with realized P&L closed (part of) a position. A close at exactly
            # the entry price books 0 and is not counted; that undercounts breakevens.
            closed: dict[str, dict] = {}
            for fill in acct.fills:
                if not fill.realized_pnl:
                    continue
                item = closed.setdefault(
                    fill.order_id, {"pnl": D("0"), "timestamp": fill.ts}
                )
                item["pnl"] += fill.realized_pnl
                item["timestamp"] = max(item["timestamp"], fill.ts)
            report.update(
                created=acct.created,
                max_order_notional=str(acct.limits.max_order_notional),
                volume=str(acct.volume),
                fees=str(acct.fees_paid),
                realized_pnl=str(acct.realized_pnl),
                outcomes=[
                    {"pnl": float(item["pnl"]), "timestamp": item["timestamp"]}
                    for item in closed.values()
                ],
                fills=len(acct.fills),
            )
            return report

    async def costs(self) -> dict:
        from .service import count_llm_requests

        async with self._lock:
            acct = self._load()
            marks = await self._touch(acct)
            self._save(acct)
            llm = (
                count_llm_requests(acct.created, self._ledger_dir)
                if self._ledger_dir
                else 0
            )
            return cost_report(acct, marks, self._clock(), llm_requests=llm)

    # owner controls (never exposed to the agent's tools)

    async def config(self) -> dict:
        async with self._lock:
            a = self._load()
            maker, taker = fee_rates(a)
            return {
                "limits": E._enc(asdict(a.limits)),
                "costs": E._enc(asdict(a.costs)),
                "start_cash": str(a.start_cash),
                "created": a.created,
                "fee_override": a.fee_override,
                "fee_rates_now_pct": {
                    "maker": str((maker * 100).quantize(D("0.0001"))),
                    "taker": str((taker * 100).quantize(D("0.0001"))),
                },
                "fee_source": "Kraken support: Fees for Derivatives trading, entry tier (0.02% maker / 0.05% taker)",
                "margin": {
                    "initial": str(INITIAL_MARGIN),
                    "maintenance": str(MAINTENANCE_MARGIN),
                    "liquidation_penalty": str(LIQUIDATION_PENALTY),
                },
            }

    async def set_config(
        self,
        limits: dict | None = None,
        costs: dict | None = None,
        fee_override: dict | None = None,
    ) -> dict:
        async with self._lock:
            a = self._load()
            if fee_override is not None:
                parsed = E.parse_fee_override(fee_override)
                if parsed and "volume_usd" in parsed:
                    raise E.Rejected(
                        "fee_override for futures takes maker and taker percents, not volume_usd"
                    )
                a.fee_override = parsed
            for k, v in (limits or {}).items():
                if not hasattr(a.limits, k):
                    raise E.Rejected(f"Unknown limit {k}")
                setattr(a.limits, k, int(v) if k == "max_open_orders" else D(str(v)))
            for k, v in (costs or {}).items():
                if not hasattr(a.costs, k):
                    raise E.Rejected(f"Unknown cost setting {k}")
                setattr(a.costs, k, D(str(v)))
            self._save(a)
        return await self.config()

    async def reset(self, start_cash: Decimal) -> dict:
        async with self._lock:
            old = self._load()
            fresh = FAccount.new("kraken_futures", start_cash, self._clock())
            fresh.limits, fresh.costs, fresh.fee_override = (
                old.limits,
                old.costs,
                old.fee_override,
            )
            if self.path.exists():
                self.path.rename(self.path.with_suffix(f".{int(self._clock())}.bak"))
            self._save(fresh)
            self.start_cash = start_cash
        return await self.config()
