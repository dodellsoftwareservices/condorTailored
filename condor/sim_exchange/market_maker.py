"""A fee-aware market maker for the simulated Kraken exchange.

Not an LLM: a fixed quoting rule, run on a timer, against the same simulated
engine (real public books and tape, queue-based limit fills, real fee tiers).

What it does each cycle, per pair: post one post-only bid and, when it holds inventory,
one post-only ask around the live mid; cancel and replace a quote only when it has
drifted or aged out (every cancel loses queue position, which is a real cost);
lean both quotes against inventory; stop quoting a side at its inventory cap; halt
everything if equity falls past a loss limit.

Spot has no shorting, so the maker starts bid-only and quotes asks only against what it
holds. It is not high-frequency in the colocated sense: the public REST API is polled
about once a second, so a cycle takes several seconds.

The honest economics: a round trip earns about twice the half-spread and pays two maker
fees. Kraken's fee falls with the account's 30-day volume, which is the only reason
market making is viable at scale. At the opening tier (0.40% maker) the spread needed to
break even sits far from the touch and fills are rare. The simulator charges the real
tier for the volume it has actually traded, so a run measures that, rather than assuming it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR
from decimal import Decimal as D
from pathlib import Path

from condor.sim_exchange import engine as E
from condor.sim_exchange.kraken_public import KrakenPublicError
from condor.sim_exchange.service import SimService

log = logging.getLogger(__name__)

BPS = D(10000)
MAX_PAIRS = 6


@dataclass
class MMConfig:
    pairs: tuple[str, ...] = ("BTC-USD", "ETH-USD")
    order_notional: D = D("250")  # per quote, per side
    half_spread_bps: D | None = None  # None: fee-aware (maker fee + half the margin)
    min_profit_bps: D = D(
        "10"
    )  # round-trip margin over both maker fees, fee-aware mode
    max_inventory_notional: D = D("1500")  # per pair; the bid stops here
    skew_strength: D = D("1")  # how hard quotes lean against inventory
    refresh_sec: float = 10.0
    requote_bps: D = D("5")  # keep a resting quote this close to its target
    max_quote_age_sec: float = 300.0
    max_loss_usd: D = D("1000")  # halt and pull quotes past this drawdown

    @classmethod
    def from_dict(cls, raw: dict | None) -> "MMConfig":
        raw = dict(raw or {})
        cfg = cls()
        for name in (
            "order_notional",
            "min_profit_bps",
            "max_inventory_notional",
            "skew_strength",
            "requote_bps",
            "max_loss_usd",
        ):
            if raw.get(name) is not None:
                setattr(cfg, name, D(str(raw[name])))
        if raw.get("half_spread_bps") not in (None, ""):
            cfg.half_spread_bps = D(str(raw["half_spread_bps"]))
        for name in ("refresh_sec", "max_quote_age_sec"):
            if raw.get(name) is not None:
                setattr(cfg, name, float(raw[name]))
        if raw.get("pairs"):
            cfg.pairs = tuple(str(p).upper() for p in raw["pairs"])
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not 1 <= len(self.pairs) <= MAX_PAIRS or any(
            not p.endswith("-USD") for p in self.pairs
        ):
            raise ValueError(
                f"pairs must be 1-{MAX_PAIRS} USD-quoted pairs such as BTC-USD"
            )
        if self.half_spread_bps is not None and self.half_spread_bps <= 0:
            raise ValueError("half_spread_bps must be positive")
        for name in (
            "order_notional",
            "max_inventory_notional",
            "max_loss_usd",
            "requote_bps",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.refresh_sec < 2 or self.max_quote_age_sec < self.refresh_sec:
            raise ValueError(
                "refresh_sec must be at least 2 and no larger than max_quote_age_sec"
            )
        if self.max_inventory_notional < self.order_notional:
            raise ValueError("max_inventory_notional must be at least one order")

    def public(self) -> dict:
        return {
            "pairs": list(self.pairs),
            "order_notional": str(self.order_notional),
            "half_spread_bps": (
                None if self.half_spread_bps is None else str(self.half_spread_bps)
            ),
            "min_profit_bps": str(self.min_profit_bps),
            "max_inventory_notional": str(self.max_inventory_notional),
            "skew_strength": str(self.skew_strength),
            "refresh_sec": self.refresh_sec,
            "requote_bps": str(self.requote_bps),
            "max_quote_age_sec": self.max_quote_age_sec,
            "max_loss_usd": str(self.max_loss_usd),
        }


@dataclass
class Quotes:
    bid_price: D | None
    bid_qty: D
    ask_price: D | None
    ask_qty: D
    half_spread_bps: D
    mid: D


def _tick_down(x: D, tick: D) -> D:
    return (x / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _tick_up(x: D, tick: D) -> D:
    return (x / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def compute_quotes(
    cfg: MMConfig, rules, book: dict, *, maker_rate: D, free_base: D
) -> Quotes:
    """The quote prices and sizes for one pair. Pure: no I/O, no state."""
    best_bid, best_ask = book["bids"][0][0], book["asks"][0][0]
    mid = (best_bid + best_ask) / 2
    fee_bps = maker_rate * BPS
    half = (
        cfg.half_spread_bps
        if cfg.half_spread_bps is not None
        else fee_bps + cfg.min_profit_bps / 2
    )

    inv_notional = free_base * mid
    ratio = min(max(inv_notional / cfg.max_inventory_notional, D(0)), D(1))
    center = mid * (1 - cfg.skew_strength * ratio * half / BPS)
    # Never inside the touch: a post-only quote that crosses would be refused or take.
    bid = _tick_down(min(center * (1 - half / BPS), best_bid), rules.tick_size)
    ask = _tick_up(max(center * (1 + half / BPS), best_ask), rules.tick_size)

    bid_qty = (
        E.floor_to(cfg.order_notional / bid, rules.lot_decimals) if bid > 0 else D(0)
    )
    ask_qty = min(
        E.floor_to(cfg.order_notional / ask, rules.lot_decimals),
        E.floor_to(free_base, rules.lot_decimals),
    )
    ok = lambda q, p: q >= rules.ordermin and q * p >= rules.costmin  # noqa: E731
    want_bid = (
        ok(bid_qty, bid)
        and inv_notional + cfg.order_notional <= cfg.max_inventory_notional
    )
    want_ask = ok(ask_qty, ask)
    return Quotes(
        bid if want_bid else None,
        bid_qty if want_bid else D(0),
        ask if want_ask else None,
        ask_qty if want_ask else D(0),
        half,
        mid,
    )


@dataclass
class MMStats:
    cycles: int = 0
    quotes_placed: int = 0
    quotes_cancelled: int = 0
    rejected: int = 0
    errors: int = 0
    maker_fills: int = 0
    buys: int = 0
    sells: int = 0
    volume: D = D(0)
    fees: D = D(0)
    realized_pnl: D = D(0)
    last_error: str = ""


class MarketMaker:
    def __init__(
        self,
        svc: SimService,
        cfg: MMConfig,
        *,
        clock=time.time,
        events_path: Path | None = None,
    ) -> None:
        self.svc, self.cfg, self._clock = svc, cfg, clock
        self.stats = MMStats()
        self.start_equity: D | None = None
        self.halted = ""
        self._seq = 0
        self._events = events_path
        self.last: dict[str, dict] = {}

    def _event(self, kind: str, **data) -> None:
        if not self._events:
            return
        try:
            self._events.parent.mkdir(parents=True, exist_ok=True)
            with self._events.open("a") as fh:
                fh.write(json.dumps({"t": self._clock(), "ev": kind, **data}) + "\n")
        except OSError:
            pass

    def _count_fills(self, fills: list[dict]) -> None:
        for f in fills:
            self.stats.maker_fills += 1
            self.stats.buys += f["side"] == "buy"
            self.stats.sells += f["side"] == "sell"
            self.stats.volume += D(f["notional"])
            self.stats.fees += D(f["fee"])
            self.stats.realized_pnl += D(f["realized_pnl"])
            self._event(
                "fill",
                pair=f["pair"],
                side=f["side"],
                price=f["price"],
                qty=f["qty"],
                fee=f["fee"],
                realized_pnl=f["realized_pnl"],
            )

    async def cancel_all(self) -> int:
        n = 0
        state = await self.svc.quote_state([])
        for o in state["open"]:
            if o["client_order_id"].startswith("mm-"):
                try:
                    await self.svc.cancel(o["id"])
                    n += 1
                except E.Rejected:
                    pass
        self.stats.quotes_cancelled += n
        return n

    async def step(self) -> dict:
        """One cycle: take tape fills, check the loss limit, requote each pair."""
        if self.halted:
            return {"halted": self.halted}
        self.stats.cycles += 1
        adv = await self.svc.advance()
        self._count_fills(adv["filled"])

        bases = [p.split("-")[0] for p in self.cfg.pairs]
        state = await self.svc.quote_state(bases)
        equity = state["equity"]
        if self.start_equity is None:
            self.start_equity = equity
        if self.start_equity - equity > self.cfg.max_loss_usd:
            self.halted = f"loss limit: equity down {E.q5(self.start_equity - equity)} (limit {self.cfg.max_loss_usd})"
            await self.cancel_all()
            self._event("halt", reason=self.halted)
            return {"halted": self.halted}

        for pair in self.cfg.pairs:
            try:
                await self._requote(pair, state)
            except KrakenPublicError as e:
                self.stats.errors += 1
                self.stats.last_error = str(e)[:160]
        return {"cycle": self.stats.cycles, "filled": len(adv["filled"])}

    async def _requote(self, pair: str, state: dict) -> None:
        rules = await self.svc.client.pair_rules(pair)
        book = await self.svc.client.depth(rules.altname, 25)
        if not book["bids"] or not book["asks"]:
            return
        maker_rate, _ = state["rates"][rules.base]
        q = compute_quotes(
            self.cfg,
            rules,
            book,
            maker_rate=maker_rate,
            free_base=state["free"][rules.base],
        )
        self.last[pair] = {
            "mid": str(q.mid),
            "half_spread_bps": str(E.q5(q.half_spread_bps)),
            "maker_fee_bps": str(E.q5(maker_rate * BPS)),
            "bid": None if q.bid_price is None else str(q.bid_price),
            "ask": None if q.ask_price is None else str(q.ask_price),
            "inventory": str(state["free"][rules.base]),
        }
        now = self._clock()
        mine = [
            o
            for o in state["open"]
            if o["client_order_id"].startswith("mm-") and o["pair"] == rules.altname
        ]
        for side, price, qty in (
            ("buy", q.bid_price, q.bid_qty),
            ("sell", q.ask_price, q.ask_qty),
        ):
            existing = [o for o in mine if o["side"] == side]
            keep = False
            for o in existing:
                drift = (
                    abs(D(o["price"]) - price) / q.mid * BPS
                    if price is not None
                    else None
                )
                fresh = now - o["created"] <= self.cfg.max_quote_age_sec
                if (
                    price is not None
                    and not keep
                    and drift <= self.cfg.requote_bps
                    and fresh
                ):
                    keep = True
                    continue
                try:
                    await self.svc.cancel(o["id"])
                    self.stats.quotes_cancelled += 1
                except E.Rejected:
                    pass  # filled or cancelled since the read
            if price is None or keep:
                continue
            self._seq += 1
            try:
                await self.svc.place(
                    pair=pair,
                    side=side,
                    type="limit",
                    qty=qty,
                    price=price,
                    post_only=True,
                    client_order_id=f"mm-{rules.altname}-{side}-{self._seq}",
                )
                self.stats.quotes_placed += 1
            except E.Rejected as e:
                self.stats.rejected += 1
                self.stats.last_error = str(e)[:160]

    def status(self, equity: D | None = None) -> dict:
        s = self.stats
        net = (
            None
            if equity is None or self.start_equity is None
            else equity - self.start_equity
        )
        return {
            "halted": self.halted,
            "cycles": s.cycles,
            "quotes_placed": s.quotes_placed,
            "quotes_cancelled": s.quotes_cancelled,
            "rejected": s.rejected,
            "errors": s.errors,
            "maker_fills": s.maker_fills,
            "buys": s.buys,
            "sells": s.sells,
            "volume": str(E.q5(s.volume)),
            "fees_paid": str(E.q5(s.fees)),
            "realized_pnl": str(E.q5(s.realized_pnl)),
            "net_pnl": None if net is None else str(E.q5(net)),
            "last_error": s.last_error,
            "pairs": self.last,
            "config": self.cfg.public(),
        }


class MMRunner:
    """Owns the background task. One maker per process; start replaces nothing."""

    def __init__(
        self,
        svc: SimService,
        events_path: Path | None = None,
        autostart_path: Path | None = None,
    ) -> None:
        self.svc = svc
        self._events = events_path
        # While running, the config is kept here so a Condor restart can pick the maker back up.
        # Stop and halt delete it: a maker the owner stopped, or that tripped its loss limit,
        # must not come back on its own.
        self._autostart = autostart_path
        self.maker: MarketMaker | None = None
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, cfg: MMConfig) -> None:
        if self.running:
            raise RuntimeError("market maker is already running")
        self.maker = MarketMaker(self.svc, cfg, events_path=self._events)
        self._remember(cfg)
        self._task = asyncio.create_task(self._loop(self.maker))

    def _remember(self, cfg: MMConfig | None) -> None:
        if not self._autostart:
            return
        try:
            if cfg is None:
                self._autostart.unlink(missing_ok=True)
            else:
                self._autostart.parent.mkdir(parents=True, exist_ok=True)
                self._autostart.write_text(json.dumps(cfg.public()))
        except OSError:
            log.exception("could not update the market maker autostart file")

    async def resume_from_disk(self) -> bool:
        """Boot hook: restart the maker if it was running when the process went down."""
        if self.running or not self._autostart or not self._autostart.exists():
            return False
        try:
            cfg = MMConfig.from_dict(json.loads(self._autostart.read_text()))
        except (OSError, ValueError, ArithmeticError):
            log.exception(
                "market maker autostart file unreadable; leaving the maker stopped"
            )
            return False
        await self.start(cfg)
        return True

    MAX_CONSECUTIVE_FAILURES = (
        12  # ~2 minutes of nothing but errors before pulling quotes
    )

    async def _loop(self, maker: MarketMaker) -> None:
        failures = 0
        try:
            while not maker.halted:
                try:
                    await maker.step()
                    failures = 0
                except (
                    KrakenPublicError
                ) as e:  # Kraken slow or unreachable: wait it out, quotes keep resting
                    maker.stats.errors += 1
                    maker.stats.last_error = str(e)[:160]
                except E.Rejected as e:
                    maker.stats.rejected += 1
                    maker.stats.last_error = str(e)[:160]
                except asyncio.CancelledError:
                    raise
                except (
                    Exception
                ) as e:  # unexpected: log it, retry, and only halt if it never clears
                    failures += 1
                    maker.stats.errors += 1
                    maker.stats.last_error = f"{type(e).__name__}: {e}"[:160]
                    log.exception("market maker step failed (%d in a row)", failures)
                    if failures >= self.MAX_CONSECUTIVE_FAILURES:
                        maker.halted = "crashed: see logs"
                        await maker.cancel_all()
                        self._remember(None)
                        return
                await asyncio.sleep(maker.cfg.refresh_sec)
            self._remember(
                None
            )  # left the loop because it halted (loss limit): do not resurrect it
        except asyncio.CancelledError:
            raise

    async def stop(self) -> dict:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._remember(None)
        pulled = await self.maker.cancel_all() if self.maker else 0
        return {"stopped": True, "quotes_pulled": pulled}

    async def status(self) -> dict:
        if not self.maker:
            return {"running": False, "configured": False}
        state = await self.svc.quote_state([])
        return {
            "running": self.running,
            "configured": True,
            **self.maker.status(state["equity"]),
        }
