"""Matching and accounting for the simulated Kraken spot exchange.

Pure: no network, no clock, no files. The service feeds it live order books and
trade tapes and persists the :class:`Account` it returns, so every rule below
can be checked by hand against a synthetic book.

What is modelled, and how faithfully
------------------------------------
* **Market orders** walk the real order book level by level, so spread and
  price impact come from actual depth. Depth that runs out gives a partial fill.
* **Limit orders** rest with a queue position. Only trades the real tape prints
  fill them: a trade *through* the price fills the order in full, a trade *at*
  the price first eats the volume queued ahead. Cancellations by other
  participants are not modelled, so the queue never shortens on its own. That
  errs toward fewer, later fills.
* **Fees** come from the Kraken tier table by this account's rolling 30-day
  volume, in the quote currency, maker or taker by how the fill happened.
* **Venue rules**: tick size, lot decimals, minimum order size and minimum cost
  are enforced exactly as the pair reports them.
* **Costs** are kept apart: exchange fees, slippage against the mid at arrival,
  the agent's own LLM requests and any fixed running cost.

Not modelled: margin and shorting, staking, other quote currencies than USD,
withdrawals, order-book changes caused by our own orders, and hidden or iceberg
liquidity. Our orders never move the real book, so market impact beyond what
the visible depth already shows is understated for large sizes.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path

D = Decimal
Q5 = D("0.00001")
DAY = 86400.0
THIRTY_DAYS = 30 * DAY
BPS = D(10000)

FEES_FILE = Path(__file__).with_name("kraken_fees.json")


class Rejected(ValueError):
    """The simulated exchange refused the order; the message says why."""


def q5(x: Decimal) -> Decimal:
    return x.quantize(Q5, rounding=ROUND_HALF_UP)


def floor_to(x: Decimal, decimals: int) -> Decimal:
    return x.quantize(D(1).scaleb(-decimals), rounding=ROUND_DOWN)


# ── fees ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FeeSchedule:
    spot: tuple[
        tuple[Decimal, Decimal, Decimal], ...
    ]  # (min_volume, maker, taker) as fractions
    stable: tuple[tuple[Decimal, Decimal, Decimal], ...]
    stable_bases: frozenset[str]
    source: str = ""
    verification: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "FeeSchedule":
        raw = json.loads((path or FEES_FILE).read_text())

        def tiers(rows):
            return tuple(
                sorted(
                    (
                        D(str(r["min_volume_usd"])),
                        D(r["maker"]) / 100,
                        D(r["taker"]) / 100,
                    )
                    for r in rows
                )
            )

        return cls(
            spot=tiers(raw["spot_crypto"]),
            stable=tiers(raw["stablecoin_fx"]),
            stable_bases=frozenset(raw["stablecoin_fx_bases"]),
            source=raw.get("source", ""),
            verification=raw.get("verification", ""),
        )

    def rates(self, volume_30d: Decimal, base: str) -> tuple[Decimal, Decimal]:
        """(maker, taker) as fractions for this 30-day volume."""
        table = self.stable if base.upper() in self.stable_bases else self.spot
        maker, taker = table[0][1], table[0][2]
        for min_vol, mk, tk in table:
            if volume_30d >= min_vol:
                maker, taker = mk, tk
        return maker, taker


# ── state ───────────────────────────────────────────────────────────────


@dataclass
class Limits:
    """Hard caps the model cannot raise: only the owner edits these."""

    max_order_notional: Decimal = D("2500")
    max_position_notional: Decimal = D("5000")
    max_gross_exposure_pct: Decimal = D("60")
    min_cash_reserve_pct: Decimal = D("20")
    max_open_orders: int = 10


@dataclass
class CostConfig:
    fixed_daily_usd: Decimal = D("0")
    llm_cost_per_request_usd: Decimal = D("0")


@dataclass
class Order:
    id: str
    client_id: str
    pair: str
    base: str
    side: str
    type: str
    qty: Decimal
    price: Decimal | None
    remaining: Decimal
    post_only: bool
    status: str  # open | filled | partial_cancelled | cancelled
    created: float
    arrival_mid: Decimal
    queue_ahead: Decimal = D("0")
    filled: Decimal = D("0")
    reduce_only: bool = (
        False  # futures only: the order may shrink a position, never grow or flip it
    )


@dataclass
class Fill:
    id: str
    order_id: str
    ts: float
    pair: str
    base: str
    side: str
    price: Decimal
    qty: Decimal
    notional: Decimal
    fee: Decimal
    liquidity: str  # taker | maker
    arrival_mid: Decimal
    slippage_cost: Decimal  # vs arrival mid; negative when we earned the spread
    realized_pnl: Decimal
    latency_ms: int = 0


@dataclass
class Account:
    name: str
    created: float
    start_cash: Decimal
    cash: Decimal
    holdings: dict[str, Decimal] = field(default_factory=dict)
    basis: dict[str, Decimal] = field(default_factory=dict)  # USD cost incl. fees
    orders: dict[str, Order] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)
    costs: CostConfig = field(default_factory=CostConfig)
    fees_paid: Decimal = D("0")
    slippage_cost: Decimal = D("0")
    realized_pnl: Decimal = D("0")
    volume: Decimal = D("0")
    cursors: dict[str, str] = field(default_factory=dict)
    client_ids: dict[str, str] = field(default_factory=dict)
    # Kraken can replay a trade at a cursor boundary. Retain recent source IDs
    # so a replay cannot consume queue volume or create a second simulated fill.
    seen_trade_keys: dict[str, list[str]] = field(default_factory=dict)
    # A labelled what-if for the fee schedule, e.g. {"maker": "0.25", "taker": "0.40", "label": ...}
    # in percent, or {"volume_usd": "10000000"} to price as if that 30-day volume tier were reached.
    # None means the real tier schedule by this account's own rolling volume (the honest default).
    fee_override: dict | None = None

    @classmethod
    def new(cls, name: str, start_cash: Decimal, now: float) -> "Account":
        return cls(name=name, created=now, start_cash=start_cash, cash=start_cash)


# JSON round-trip: Decimals travel as strings so no precision is lost.


def _enc(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, dict):
        return {k: _enc(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_enc(v) for v in o]
    return o


def account_to_dict(acct: Account) -> dict:
    return _enc(asdict(acct))


def _build(cls, raw: dict):
    kwargs = {}
    for f in fields(cls):
        if f.name not in raw:
            continue
        v = raw[f.name]
        t = str(f.type)
        if v is not None and "Decimal" in t and "dict" not in t:
            v = D(v)
        kwargs[f.name] = v
    return cls(**kwargs)


def account_from_dict(raw: dict) -> Account:
    acct = _build(
        Account,
        {
            k: v
            for k, v in raw.items()
            if k not in ("orders", "fills", "limits", "costs", "holdings", "basis")
        },
    )
    acct.holdings = {k: D(v) for k, v in raw.get("holdings", {}).items()}
    acct.basis = {k: D(v) for k, v in raw.get("basis", {}).items()}
    acct.orders = {k: _build(Order, v) for k, v in raw.get("orders", {}).items()}
    acct.fills = [_build(Fill, v) for v in raw.get("fills", [])]
    acct.limits = _build(Limits, raw.get("limits", {}))
    acct.costs = _build(CostConfig, raw.get("costs", {}))
    return acct


# ── helpers ─────────────────────────────────────────────────────────────


def volume_30d(acct: Account, now: float) -> Decimal:
    cutoff = now - THIRTY_DAYS
    return sum((f.notional for f in acct.fills if f.ts >= cutoff), D("0"))


def parse_fee_override(raw: dict | None) -> dict | None:
    """Validate an owner-supplied fee what-if. Empty or None clears it."""
    if not raw:
        return None
    try:
        if "volume_usd" in raw:
            vol = D(str(raw["volume_usd"]))
            if vol < 0:
                raise ValueError
            out = {"volume_usd": str(vol)}
        else:
            maker, taker = D(str(raw["maker"])), D(str(raw["taker"]))
            if not (D("0") <= maker <= D("2") and D("0") <= taker <= D("2")):
                raise ValueError
            out = {"maker": str(maker), "taker": str(taker)}
    except (KeyError, ArithmeticError, ValueError):
        raise Rejected(
            "fee_override needs maker and taker as percents between 0 and 2, or volume_usd"
        ) from None
    if raw.get("label"):
        out["label"] = str(raw["label"])[:80]
    return out


def rates_for(
    acct: Account, sched: FeeSchedule, base: str, now: float
) -> tuple[Decimal, Decimal]:
    ov = acct.fee_override
    if ov:
        if "volume_usd" in ov:
            return sched.rates(D(ov["volume_usd"]), base)
        return D(ov["maker"]) / 100, D(ov["taker"]) / 100
    return sched.rates(volume_30d(acct, now), base)


def mid_of(book: dict) -> Decimal:
    if not book["bids"] or not book["asks"]:
        raise Rejected("The order book is empty on one side; no price to trade against")
    return (book["bids"][0][0] + book["asks"][0][0]) / 2


def reserved_cash(acct: Account, taker_rate: Decimal) -> Decimal:
    return sum(
        (
            o.remaining * o.price * (1 + taker_rate)
            for o in acct.orders.values()
            if o.status == "open" and o.side == "buy"
        ),
        D("0"),
    )


def reserved_base(acct: Account, base: str) -> Decimal:
    return sum(
        (
            o.remaining
            for o in acct.orders.values()
            if o.status == "open" and o.side == "sell" and o.base == base
        ),
        D("0"),
    )


def equity(acct: Account, mids: dict[str, Decimal]) -> Decimal:
    total = acct.cash
    for base, qty in acct.holdings.items():
        if qty:
            if base not in mids:
                raise Rejected(f"No price for {base}; cannot value the account")
            total += qty * mids[base]
    return total


def gross_exposure(acct: Account, mids: dict[str, Decimal]) -> Decimal:
    return sum(
        (q * mids[b] for b, q in acct.holdings.items() if q and b in mids), D("0")
    )


@dataclass
class Walk:
    qty: Decimal
    notional: Decimal

    @property
    def avg(self) -> Decimal:
        return self.notional / self.qty if self.qty else D("0")


def walk_book(levels, *, qty: Decimal, limit_price: Decimal | None, side: str) -> Walk:
    """Consume ``qty`` from ``levels`` (best first), stopping at ``limit_price``."""
    left, got, cost = qty, D("0"), D("0")
    for price, size in levels:
        if limit_price is not None and (
            (side == "buy" and price > limit_price)
            or (side == "sell" and price < limit_price)
        ):
            break
        take = min(size, left)
        got += take
        cost += take * price
        left -= take
        if left <= 0:
            break
    return Walk(got, cost)


def qty_for_budget(levels, budget: Decimal, fee: Decimal, lot_decimals: int) -> Decimal:
    """Largest lot-rounded quantity whose cost plus taker fee fits ``budget``."""
    net = budget / (1 + fee)
    qty = D("0")
    for price, size in levels:
        afford = net / price
        take = min(size, afford)
        qty += take
        net -= take * price
        if net <= 0 or take < size:
            break
    return floor_to(qty, lot_decimals)


def impact_bps(levels, notional: Decimal, mid: Decimal, side: str) -> Decimal | None:
    """Average-fill vs mid, in bps, for a hypothetical market order of ``notional``.

    ``None`` when the visible depth cannot absorb it.
    """
    left, qty, cost = notional, D("0"), D("0")
    for price, size in levels:
        take = min(size, left / price)
        qty += take
        cost += take * price
        left -= take * price
        if left <= D("0.00000001"):
            break
    if left > D("0.01"):
        return None
    avg = cost / qty
    return ((avg - mid) / mid * BPS) if side == "buy" else ((mid - avg) / mid * BPS)


# ── checks ──────────────────────────────────────────────────────────────


def _check_limits(
    acct: Account,
    side: str,
    base: str,
    notional: Decimal,
    mids: dict[str, Decimal],
    *,
    new_open_order: bool,
) -> None:
    lim = acct.limits
    if notional > lim.max_order_notional:
        raise Rejected(
            f"Order notional ${notional:.2f} exceeds the ${lim.max_order_notional} per-order limit"
        )
    if new_open_order:
        open_n = sum(1 for o in acct.orders.values() if o.status == "open")
        if open_n >= lim.max_open_orders:
            raise Rejected(
                f"Already {open_n} open orders; the limit is {lim.max_open_orders}"
            )
    if side != "buy":
        return
    eq = equity(acct, mids)
    pos_after = acct.holdings.get(base, D("0")) * mids.get(base, D("0")) + notional
    if pos_after > lim.max_position_notional:
        raise Rejected(
            f"{base} position would be ${pos_after:.2f}, over the ${lim.max_position_notional} per-asset limit"
        )
    exp_after = gross_exposure(acct, mids) + notional
    if exp_after > eq * lim.max_gross_exposure_pct / 100:
        raise Rejected(
            f"Gross exposure would be ${exp_after:.2f}, over {lim.max_gross_exposure_pct}% of equity (${eq:.2f})"
        )
    cash_after = acct.cash - notional
    if cash_after < eq * lim.min_cash_reserve_pct / 100:
        raise Rejected(
            f"Cash after the order (${cash_after:.2f}) would fall under the {lim.min_cash_reserve_pct}% reserve"
        )


def _validate_size(rules, qty: Decimal, notional: Decimal) -> None:
    if qty < rules.ordermin:
        raise Rejected(
            f"Quantity {qty} is below the {rules.ordermin} {rules.base} minimum order size"
        )
    if rules.costmin and notional < rules.costmin:
        raise Rejected(
            f"Order value ${notional:.2f} is below the ${rules.costmin} minimum"
        )


def _require_usd(rules) -> None:
    if rules.quote != "USD":
        raise Rejected(
            f"Only USD-quoted pairs are simulated; {rules.altname} is quoted in {rules.quote}"
        )


# ── fills ───────────────────────────────────────────────────────────────


def _settle(
    acct: Account,
    order: Order,
    *,
    qty: Decimal,
    price: Decimal,
    liquidity: str,
    fee_rate: Decimal,
    ts: float,
    latency_ms: int,
) -> Fill:
    notional = q5(qty * price)
    fee = q5(notional * fee_rate)
    realized = D("0")
    if order.side == "buy":
        acct.cash -= notional + fee
        acct.holdings[order.base] = acct.holdings.get(order.base, D("0")) + qty
        acct.basis[order.base] = acct.basis.get(order.base, D("0")) + notional + fee
        slip = (price - order.arrival_mid) * qty
    else:
        held = acct.holdings.get(order.base, D("0"))
        avg_cost = acct.basis.get(order.base, D("0")) / held if held else D("0")
        cost_removed = avg_cost * qty
        proceeds = notional - fee
        realized = proceeds - cost_removed
        acct.cash += proceeds
        acct.holdings[order.base] = held - qty
        acct.basis[order.base] = acct.basis.get(order.base, D("0")) - cost_removed
        slip = (order.arrival_mid - price) * qty
    slip = q5(slip)
    order.remaining -= qty
    order.filled += qty
    acct.fees_paid += fee
    acct.slippage_cost += slip
    acct.realized_pnl += realized
    acct.volume += notional
    fill = Fill(
        id=uuid.uuid4().hex[:12],
        order_id=order.id,
        ts=ts,
        pair=order.pair,
        base=order.base,
        side=order.side,
        price=price,
        qty=qty,
        notional=notional,
        fee=fee,
        liquidity=liquidity,
        arrival_mid=order.arrival_mid,
        slippage_cost=slip,
        realized_pnl=q5(realized),
        latency_ms=latency_ms,
    )
    acct.fills.append(fill)
    return fill


def _dedupe(acct: Account, client_id: str):
    if client_id and client_id in acct.client_ids:
        o = acct.orders.get(acct.client_ids[client_id])
        if o:
            return o
    return None


def place_market(
    acct: Account,
    rules,
    sched: FeeSchedule,
    *,
    side: str,
    book: dict,
    now: float,
    client_id: str = "",
    qty: Decimal | None = None,
    quote_amount: Decimal | None = None,
    latency_ms: int = 0,
    mids: dict[str, Decimal] | None = None,
) -> tuple[Order, list[Fill], bool]:
    """Returns (order, fills, duplicate). Raises :class:`Rejected` with the reason."""
    dup = _dedupe(acct, client_id)
    if dup:
        return dup, [f for f in acct.fills if f.order_id == dup.id], True
    _require_usd(rules)
    if side not in ("buy", "sell"):
        raise Rejected("side must be buy or sell")
    if (qty is None) == (quote_amount is None):
        raise Rejected("Give exactly one of qty or quote_amount")
    if quote_amount is not None and side != "buy":
        raise Rejected("quote_amount is only for buys; give qty to sell")

    mid = mid_of(book)
    mids = {**(mids or {}), rules.base: mid}
    maker, taker = rates_for(acct, sched, rules.base, now)
    levels = book["asks"] if side == "buy" else book["bids"]

    want = (
        floor_to(qty, rules.lot_decimals)
        if qty is not None
        else qty_for_budget(levels, quote_amount, taker, rules.lot_decimals)
    )
    w = walk_book(levels, qty=want, limit_price=None, side=side)
    _validate_size(rules, want, w.notional if w.qty else want * mid)
    if w.qty <= 0:
        raise Rejected("No liquidity on the book to fill this order")
    fee_est = q5(w.notional * taker)
    # Funds first: "you don't have it" is a plainer answer than a policy limit.
    if side == "buy":
        avail = acct.cash - reserved_cash(acct, taker)
        if w.notional + fee_est > avail:
            raise Rejected(
                f"Needs ${w.notional + fee_est:.2f} but only ${avail:.2f} cash is free"
            )
    else:
        free = acct.holdings.get(rules.base, D("0")) - reserved_base(acct, rules.base)
        if want > free:
            raise Rejected(f"Selling {want} {rules.base} but only {free} is free")
    _check_limits(acct, side, rules.base, w.notional, mids, new_open_order=False)

    order = Order(
        id=uuid.uuid4().hex[:12],
        client_id=client_id,
        pair=rules.altname,
        base=rules.base,
        side=side,
        type="market",
        qty=want,
        price=None,
        remaining=want,
        post_only=False,
        status="open",
        created=now,
        arrival_mid=mid,
    )
    fill = _settle(
        acct,
        order,
        qty=w.qty,
        price=q5(w.avg),
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
    acct: Account,
    rules,
    sched: FeeSchedule,
    *,
    side: str,
    qty: Decimal,
    price: Decimal,
    book: dict,
    now: float,
    client_id: str = "",
    post_only: bool = False,
    latency_ms: int = 0,
    mids: dict[str, Decimal] | None = None,
) -> tuple[Order, list[Fill], bool]:
    dup = _dedupe(acct, client_id)
    if dup:
        return dup, [f for f in acct.fills if f.order_id == dup.id], True
    _require_usd(rules)
    if side not in ("buy", "sell"):
        raise Rejected("side must be buy or sell")
    want = floor_to(qty, rules.lot_decimals)
    if (price / rules.tick_size) != (price / rules.tick_size).to_integral_value():
        raise Rejected(
            f"Price {price} is not a multiple of the {rules.tick_size} tick size"
        )
    mid = mid_of(book)
    mids = {**(mids or {}), rules.base: mid}
    maker, taker = rates_for(acct, sched, rules.base, now)
    _validate_size(rules, want, want * price)

    levels = book["asks"] if side == "buy" else book["bids"]
    best = levels[0][0] if levels else None
    marketable = best is not None and (
        (side == "buy" and price >= best) or (side == "sell" and price <= best)
    )
    if marketable and post_only:
        raise Rejected("Post-only order would cross the book and take liquidity")

    if side == "buy":
        avail = acct.cash - reserved_cash(acct, taker)
        if want * price * (1 + taker) > avail:
            raise Rejected(
                f"Needs up to ${want * price * (1 + taker):.2f} but only ${avail:.2f} cash is free"
            )
    else:
        free = acct.holdings.get(rules.base, D("0")) - reserved_base(acct, rules.base)
        if want > free:
            raise Rejected(f"Selling {want} {rules.base} but only {free} is free")
    _check_limits(acct, side, rules.base, want * price, mids, new_open_order=True)

    order = Order(
        id=uuid.uuid4().hex[:12],
        client_id=client_id,
        pair=rules.altname,
        base=rules.base,
        side=side,
        type="limit",
        qty=want,
        price=price,
        remaining=want,
        post_only=post_only,
        status="open",
        created=now,
        arrival_mid=mid,
    )
    fills: list[Fill] = []
    if marketable:
        w = walk_book(levels, qty=want, limit_price=price, side=side)
        if w.qty > 0:
            fills.append(
                _settle(
                    acct,
                    order,
                    qty=w.qty,
                    price=q5(w.avg),
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
        order.status = "open"
    else:
        order.status = "filled"
    acct.orders[order.id] = order
    if client_id:
        acct.client_ids[client_id] = order.id
    return order, fills, False


def cancel(acct: Account, order_id: str) -> Order:
    o = acct.orders.get(order_id)
    if not o:
        raise Rejected(f"No such order {order_id}")
    if o.status != "open":
        raise Rejected(f"Order {order_id} is already {o.status}")
    o.status = "cancelled" if o.filled == 0 else "partial_cancelled"
    return o


def on_trades(
    acct: Account, rules, trades, sched: FeeSchedule, now: float
) -> list[Fill]:
    """Feed the real trade tape to resting orders, oldest trade first."""
    out: list[Fill] = []
    recent = acct.seen_trade_keys.setdefault(rules.altname, [])
    seen = set(recent)
    for t in sorted(trades, key=lambda t: t.ts):
        key = f"{t.trade_id}:{t.ts}" if t.trade_id else ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
            recent.append(key)
        rem_vol = t.volume
        for o in sorted(
            (
                o
                for o in acct.orders.values()
                if o.status == "open" and o.pair == rules.altname
            ),
            key=lambda o: o.created,
        ):
            if t.ts < o.created:
                continue
            if o.side == "buy" and not (t.side == "s" and t.price <= o.price):
                continue
            if o.side == "sell" and not (t.side == "b" and t.price >= o.price):
                continue
            if t.price == o.price:  # at our price: the queue ahead of us trades first
                eaten = min(o.queue_ahead, rem_vol)
                o.queue_ahead -= eaten
                rem_vol -= eaten
                got = min(o.remaining, rem_vol)
                rem_vol -= got
            else:  # the market traded through our price: everything at it was already consumed
                got = o.remaining
            if got <= 0:
                continue
            maker, _ = rates_for(acct, sched, o.base, t.ts)
            out.append(
                _settle(
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


# ── reporting ───────────────────────────────────────────────────────────


def portfolio(acct: Account, mids: dict[str, Decimal], now: float) -> dict:
    eq = equity(acct, mids)
    positions = []
    for base, qty in sorted(acct.holdings.items()):
        if qty <= 0:
            continue
        mid = mids[base]
        cost = acct.basis.get(base, D("0"))
        value = qty * mid
        positions.append(
            {
                "asset": base,
                "qty": str(qty),
                "mid": str(mid),
                "value": str(q5(value)),
                "avg_cost": str(q5(cost / qty)),
                "unrealized_pnl": str(q5(value - cost)),
                "unrealized_pct": (
                    str(((value / cost - 1) * 100).quantize(D("0.01"))) if cost else "0"
                ),
            }
        )
    return {
        "equity": str(q5(eq)),
        "cash": str(q5(acct.cash)),
        "start_cash": str(acct.start_cash),
        "total_return_pct": str(((eq / acct.start_cash - 1) * 100).quantize(D("0.01"))),
        "gross_exposure": str(q5(gross_exposure(acct, mids))),
        "positions": positions,
        "open_orders": sum(1 for o in acct.orders.values() if o.status == "open"),
        "volume_30d": str(q5(volume_30d(acct, now))),
    }


def cost_report(
    acct: Account, mids: dict[str, Decimal], now: float, llm_requests: int = 0
) -> dict:
    eq = equity(acct, mids)
    days = D(str(max(now - acct.created, 0))) / D(str(DAY))
    llm = acct.costs.llm_cost_per_request_usd * llm_requests
    fixed = acct.costs.fixed_daily_usd * days
    equity_pnl = eq - acct.start_cash
    net = equity_pnl - llm - fixed
    gross = equity_pnl + acct.fees_paid + acct.slippage_cost
    taker_fills = [f for f in acct.fills if f.liquidity == "taker"]
    maker_fills = [f for f in acct.fills if f.liquidity == "maker"]
    vol = acct.volume
    bps = lambda x: str((x / vol * BPS).quantize(D("0.1"))) if vol else "0"
    # One row per executed order, so partial fills remain visible without
    # reading as separate buy/sell decisions in the portfolio UI.
    executed: dict[str, dict] = {}
    for fill in acct.fills:
        order = acct.orders.get(fill.order_id)
        row = executed.setdefault(
            fill.order_id,
            {
                "order_id": fill.order_id,
                "client_order_id": order.client_id if order else "",
                "pair": f"{fill.base}-USD",
                "side": fill.side,
                "last_fill_ts": fill.ts,
                "fill_count": 0,
                "qty": D("0"),
                "notional": D("0"),
                "fee": D("0"),
                "realized_pnl": D("0"),
            },
        )
        row["last_fill_ts"] = max(row["last_fill_ts"], fill.ts)
        row["fill_count"] += 1
        for key, value in (
            ("qty", fill.qty),
            ("notional", fill.notional),
            ("fee", fill.fee),
            ("realized_pnl", fill.realized_pnl),
        ):
            row[key] += value
    recent_executed_orders = [
        {
            **row,
            "qty": str(row["qty"]),
            "notional": str(q5(row["notional"])),
            "fee": str(q5(row["fee"])),
            "realized_pnl": str(q5(row["realized_pnl"])),
            "calibration": row["client_order_id"].startswith("cal-"),
        }
        for row in sorted(
            executed.values(), key=lambda item: item["last_fill_ts"], reverse=True
        )[:20]
    ]
    return {
        "net_pnl_after_all_costs": str(q5(net)),
        "equity_pnl": str(q5(equity_pnl)),
        "gross_pnl_before_execution_costs": str(q5(gross)),
        "costs": {
            "exchange_fees": str(q5(acct.fees_paid)),
            "slippage_vs_arrival_mid": str(q5(acct.slippage_cost)),
            "llm_requests": llm_requests,
            "llm_cost": str(q5(llm)),
            "fixed_running_cost": str(q5(fixed)),
            "total_execution_and_operating": str(
                q5(acct.fees_paid + acct.slippage_cost + llm + fixed)
            ),
        },
        "traded_volume": str(q5(vol)),
        "fees_bps_of_volume": bps(acct.fees_paid),
        "slippage_bps_of_volume": bps(acct.slippage_cost),
        "fills": {"taker": len(taker_fills), "maker": len(maker_fills)},
        "recent_executed_orders": recent_executed_orders,
        "realized_pnl": str(q5(acct.realized_pnl)),
        "note": "equity_pnl already includes fees and slippage (fills happen at real prices). LLM and fixed costs are not in equity, so they are subtracted for net.",
    }
