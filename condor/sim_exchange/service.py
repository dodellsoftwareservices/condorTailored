"""Live wiring for the simulated exchange: real Kraken data in, a persisted account out.

One account per file. All mutation happens under one lock in the main process;
the MCP tools reach it over the web API, so there is a single writer.

Latency is simulated by waiting a sampled network delay *before* fetching the
order book, so whatever the real market did in that window really does hit the
fill. It is not a fixed slippage fudge.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from typing import Awaitable, Callable

from condor.fsutil import atomic_write_json

from . import engine as E
from .kraken_public import KrakenPublicClient

DEFAULT_START_CASH = D("50000")  # per desk; the owner funds each desk with $50k
MIN_LATENCY_MS, MAX_LATENCY_MS = 20, 2000


def count_llm_requests(since_ts: float, directory: Path) -> int:
    """Model requests logged by the shared budget ledger since ``since_ts``."""
    n = 0
    if not directory.exists():
        return 0
    first = datetime.fromtimestamp(since_ts, timezone.utc).strftime("%Y%m%d")
    for p in sorted(directory.glob("requests-*.jsonl")):
        if p.stem.split("-")[-1] < first:
            continue
        try:
            for line in p.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("ev") == "req" and float(row.get("t", 0)) >= since_ts:
                    n += 1
        except OSError:
            continue
    return n


class SimService:
    def __init__(
        self,
        client: KrakenPublicClient,
        path: Path,
        sched: E.FeeSchedule | None = None,
        *,
        latency_ms: int = 150,
        start_cash: D = DEFAULT_START_CASH,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        ledger_dir: Path | None = None,
        mid_ttl_s: float = 5.0,
    ) -> None:
        self.client = client
        self.path = Path(path)
        self.sched = sched or E.FeeSchedule.load()
        self.latency_ms = latency_ms
        self.start_cash = start_cash
        self._clock = clock
        self._sleep = sleeper
        self._rng = rng or random.Random()
        self._ledger_dir = ledger_dir
        self._mid_ttl = mid_ttl_s
        self._mids: dict[str, tuple[float, D]] = {}
        self._lock = asyncio.Lock()

    # ── persistence ──

    def _load(self) -> E.Account:
        if self.path.exists():
            return E.account_from_dict(json.loads(self.path.read_text()))
        acct = E.Account.new("kraken_default", self.start_cash, self._clock())
        self._save(acct)
        return acct

    def _save(self, acct: E.Account) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, E.account_to_dict(acct))

    def _sample_latency(self) -> int:
        ms = self._rng.lognormvariate(math.log(max(self.latency_ms, 1)), 0.4)
        return int(min(max(ms, MIN_LATENCY_MS), MAX_LATENCY_MS))

    # ── prices ──

    async def _mid(self, base: str) -> D:
        hit = self._mids.get(base)
        if hit and self._clock() - hit[0] < self._mid_ttl:
            return hit[1]
        rules = await self.client.pair_rules(f"{base}-USD")
        t = await self.client.ticker(rules.altname)
        mid = (t["bid"] + t["ask"]) / 2
        self._mids[base] = (self._clock(), mid)
        return mid

    async def _held_mids(
        self, acct: E.Account, known: dict[str, D] | None = None
    ) -> dict[str, D]:
        mids = dict(known or {})
        for base, qty in acct.holdings.items():
            if qty and base not in mids:
                mids[base] = await self._mid(base)
        return mids

    # ── public reads ──

    async def snapshot(
        self, pair: str, candles: int = 0, candle_minutes: int = 15
    ) -> dict:
        rules = await self.client.pair_rules(pair)
        ticker = await self.client.ticker(rules.altname)
        book = await self.client.depth(rules.altname, 100)
        mid = E.mid_of(book)
        async with self._lock:
            acct = self._load()
        maker, taker = E.rates_for(acct, self.sched, rules.base, self._clock())
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

        out = {
            "pair": rules.altname,
            "base": rules.base,
            "quote": rules.quote,
            "bid": str(book["bids"][0][0]),
            "ask": str(book["asks"][0][0]),
            "mid": str(mid),
            "spread_bps": str((spread / mid * E.BPS).quantize(D("0.01"))),
            "last": str(ticker["last"]),
            "vwap_24h": str(ticker["vwap_24h"]),
            "volume_24h": str(ticker["volume_24h"]),
            "high_24h": str(ticker["high_24h"]),
            "low_24h": str(ticker["low_24h"]),
            "trades_24h": ticker["trades_24h"],
            "depth_usd_within_bps": {
                str(b): {
                    "bids": depth_within(book["bids"], "bid", b),
                    "asks": depth_within(book["asks"], "ask", b),
                }
                for b in (10, 25, 50)
            },
            "market_order_impact_bps": {
                str(n): {
                    "buy": _fmt(E.impact_bps(book["asks"], D(n), mid, "buy")),
                    "sell": _fmt(E.impact_bps(book["bids"], D(n), mid, "sell")),
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
                "note": "An edge must beat this plus spread and impact before a trade is worth taking.",
            },
            "rules": {
                "tick_size": str(rules.tick_size),
                "min_order_qty": str(rules.ordermin),
                "min_order_value_usd": str(rules.costmin),
                "lot_decimals": rules.lot_decimals,
            },
            "as_of": book["ts"],
        }
        if candles > 0:
            rows = await self.client.ohlc(rules.altname, candle_minutes)
            out["candles"] = {"minutes": candle_minutes, "rows": rows[-candles:]}
        return out

    async def candles(self, pair: str, candle_minutes: int, count: int) -> list[dict]:
        """Just the candles, without the ticker and order book a snapshot also pulls."""
        rules = await self.client.pair_rules(pair)
        rows = await self.client.ohlc(rules.altname, candle_minutes)
        return rows[-count:]

    # ── orders ──

    async def place(
        self,
        *,
        pair: str,
        side: str,
        type: str = "market",
        qty: D | None = None,
        quote_amount: D | None = None,
        price: D | None = None,
        post_only: bool = False,
        client_order_id: str = "",
    ) -> dict:
        if side == "buy" and client_order_id.lower().startswith("cal-"):
            raise E.Rejected(
                "Fee-only calibration buys are disabled; a new buy needs a trading edge"
            )
        async with self._lock:
            acct = self._load()
            rules = await self.client.pair_rules(pair)
            if type not in ("market", "limit"):
                raise E.Rejected("type must be market or limit")
            if type == "limit" and (price is None or qty is None):
                raise E.Rejected("A limit order needs both price and qty")
            latency = self._sample_latency()
            if client_order_id and client_order_id in acct.client_ids:
                latency = 0  # a retry is a no-op: do not even wait
            else:
                await self._sleep(latency / 1000)
            book = await self.client.depth(rules.altname)
            mids = await self._held_mids(acct, {rules.base: E.mid_of(book)})
            now = self._clock()
            if type == "market":
                order, fills, dup = E.place_market(
                    acct,
                    rules,
                    self.sched,
                    side=side,
                    qty=qty,
                    quote_amount=quote_amount,
                    book=book,
                    now=now,
                    client_id=client_order_id,
                    latency_ms=latency,
                    mids=mids,
                )
            else:
                order, fills, dup = E.place_limit(
                    acct,
                    rules,
                    self.sched,
                    side=side,
                    qty=qty,
                    price=price,
                    book=book,
                    now=now,
                    client_id=client_order_id,
                    post_only=post_only,
                    latency_ms=latency,
                    mids=mids,
                )
                if order.status == "open" and rules.altname not in acct.cursors:
                    _, last = await self.client.trades(rules.altname)
                    acct.cursors[rules.altname] = last
            self._save(acct)
            return {
                "duplicate": dup,
                "order": _order(order),
                "fills": [_fill(f) for f in fills],
                "cash": str(E.q5(acct.cash)),
                "holding": str(acct.holdings.get(rules.base, D("0"))),
                "simulated_latency_ms": latency,
            }

    async def cancel(self, order_id: str) -> dict:
        async with self._lock:
            acct = self._load()
            o = E.cancel(acct, order_id)
            self._save(acct)
            return {"order": _order(o)}

    async def advance(self) -> dict:
        """Feed the real trade tape to resting limit orders."""
        async with self._lock:
            return await self._advance(self._load())

    async def _advance(self, acct: E.Account) -> dict:
        new_fills = []
        pairs = sorted({o.pair for o in acct.orders.values() if o.status == "open"})
        for alt in pairs:
            rules = await self.client.pair_rules(alt)
            cursor = acct.cursors.get(alt)
            for _ in range(5):  # Kraken returns up to 1000 trades a call
                trades, last = await self.client.trades(alt, since=cursor)
                new_fills += E.on_trades(acct, rules, trades, self.sched, self._clock())
                cursor = last
                if len(trades) < 1000:
                    break
            acct.cursors[alt] = cursor
        self._save(acct)
        return {
            "filled": [_fill(f) for f in new_fills],
            "open_orders": sum(1 for o in acct.orders.values() if o.status == "open"),
        }

    async def quote_state(self, bases: list[str]) -> dict:
        """What a quoting loop needs in one read, without advancing orders.

        Open orders, free (unreserved) holdings, the current maker/taker fee rate per
        base, and mark-to-mid equity.
        """
        async with self._lock:
            acct = self._load()
            now = self._clock()
            mids = await self._held_mids(acct)
            return {
                "open": [_order(o) for o in acct.orders.values() if o.status == "open"],
                "free": {
                    b: acct.holdings.get(b, D("0")) - E.reserved_base(acct, b)
                    for b in bases
                },
                "rates": {b: E.rates_for(acct, self.sched, b, now) for b in bases},
                "equity": E.equity(acct, mids),
                "volume_30d": E.volume_30d(acct, now),
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
                "open": [_order(o) for o in rows if o.status == "open"],
                "recent": [_order(o) for o in rows if o.status != "open"][:limit],
                "fills_just_processed": adv["filled"],
            }

    # ── reports ──

    async def dashboard(self) -> dict:
        """Read marks and settled outcomes without advancing resting orders."""
        async with self._lock:
            acct = self._load()
            mids = await self._held_mids(acct)
            report = E.portfolio(acct, mids, self._clock())
            closed: dict[str, dict] = {}
            for fill in acct.fills:
                if fill.side != "sell":
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

    async def portfolio(self) -> dict:
        async with self._lock:
            acct = self._load()
            if any(o.status == "open" for o in acct.orders.values()):
                await self._advance(acct)
            mids = await self._held_mids(acct)
            return E.portfolio(acct, mids, self._clock())

    async def costs(self) -> dict:
        async with self._lock:
            acct = self._load()
            mids = await self._held_mids(acct)
            llm = (
                count_llm_requests(acct.created, self._ledger_dir)
                if self._ledger_dir
                else 0
            )
            return E.cost_report(acct, mids, self._clock(), llm_requests=llm)

    # ── owner controls (never exposed to the agent's tools) ──

    async def config(self) -> dict:
        async with self._lock:
            a = self._load()
            return {
                "limits": E._enc(vars(a.limits)),
                "costs": E._enc(vars(a.costs)),
                "start_cash": str(a.start_cash),
                "created": a.created,
                "fee_source": self.sched.source,
                "fee_verification": self.sched.verification,
                "fee_override": a.fee_override,
                "fee_rates_now_pct": {
                    "maker": str(
                        (
                            E.rates_for(a, self.sched, "BTC", self._clock())[0] * 100
                        ).quantize(D("0.0001"))
                    ),
                    "taker": str(
                        (
                            E.rates_for(a, self.sched, "BTC", self._clock())[1] * 100
                        ).quantize(D("0.0001"))
                    ),
                },
            }

    async def set_config(
        self,
        limits: dict | None = None,
        costs: dict | None = None,
        fee_override: dict | None = None,
    ) -> dict:
        """``fee_override``: None leaves it alone, ``{}`` clears it, otherwise a validated what-if."""
        async with self._lock:
            a = self._load()
            if fee_override is not None:
                a.fee_override = E.parse_fee_override(fee_override)
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

    async def reset(self, start_cash: D) -> dict:
        async with self._lock:
            old = self._load()
            fresh = E.Account.new("kraken_default", start_cash, self._clock())
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


def _fmt(x: D | None) -> str | None:
    return None if x is None else str(x.quantize(D("0.01")))


def _order(o: E.Order) -> dict:
    return {
        "id": o.id,
        "client_order_id": o.client_id,
        "pair": o.pair,
        "side": o.side,
        "type": o.type,
        "qty": str(o.qty),
        "price": None if o.price is None else str(o.price),
        "filled": str(o.filled),
        "remaining": str(o.remaining),
        "status": o.status,
        "post_only": o.post_only,
        "queue_ahead": str(o.queue_ahead),
        "created": o.created,
    }


def _fill(f: E.Fill) -> dict:
    return {
        "id": f.id,
        "order_id": f.order_id,
        "ts": f.ts,
        "side": f.side,
        "pair": f.pair,
        "price": str(f.price),
        "qty": str(f.qty),
        "notional": str(f.notional),
        "fee": str(f.fee),
        "liquidity": f.liquidity,
        "slippage_cost_vs_arrival_mid": str(f.slippage_cost),
        "realized_pnl": str(f.realized_pnl),
        "latency_ms": f.latency_ms,
    }
