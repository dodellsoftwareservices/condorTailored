"""Score the Alpaca paper basket against its pre-registered OKRs.

Read-only: places no orders. The objective, key results, per-position theses and
exit triggers are written to the journal BEFORE the trades (see
``data/paper_okr_journal.json``), so the targets cannot drift to fit the result.
Each run also records one snapshot per calendar day, which is what drawdown and
coverage are measured from.
"""

CATEGORY = "Analysis"

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from condor.alpaca_paper import AlpacaAPIError, AlpacaConfigError, AlpacaPaperClient
from condor.fsutil import atomic_write_json
from condor.paths import data_dir
from routines.base import RoutineResult

logger = logging.getLogger(__name__)

JOURNAL_NAME = "paper_okr_journal.json"


class Config(BaseModel):
    """Report on the paper basket's OKRs."""

    record_snapshot: bool = Field(
        default=True,
        description="Store today's snapshot (one per day) so drawdown and coverage can be measured",
    )


def journal_path() -> Path:
    return data_dir() / JOURNAL_NAME


def load_journal() -> dict:
    p = journal_path()
    if not p.exists():
        raise FileNotFoundError(f"No OKR journal at {p}. Create it before trading.")
    return json.loads(p.read_text())


def _business_days(start: date, end: date) -> int:
    """Weekdays in [start, end]. Ignores market holidays, so coverage is a floor."""
    n, d = 0, start
    while d <= end:
        n += d.weekday() < 5
        d = date.fromordinal(d.toordinal() + 1)
    return n


def _status(ok: bool | None, warn: bool = False) -> str:
    if ok is None:
        return "N/A"
    return "ON TRACK" if ok and not warn else ("WATCH" if ok else "OFF TRACK")


MIN_DAYS_TO_JUDGE = 5


def _early(status: str, today: date, start: date) -> str:
    """Return/hit-rate verdicts inside the first few days are noise, not signal."""
    if status == "OFF TRACK" and (today - start).days < MIN_DAYS_TO_JUDGE:
        return "TOO EARLY"
    return status


def evaluate(journal: dict, positions: list[dict], spy_now: float | None, today: date) -> dict:
    """Pure scoring of the journal against live positions.

    ``positions``: dicts with ``symbol``, ``market_value``, ``unrealized_pl``.
    Cost basis is ``market_value - unrealized_pl``.
    """
    krs = journal["key_results"]
    held = {p["symbol"]: p for p in positions}
    cost = sum(p["market_value"] - p["unrealized_pl"] for p in positions)
    value = sum(p["market_value"] for p in positions)
    basket_ret = (value / cost - 1) if cost > 0 else 0.0

    spy0 = (journal.get("benchmark") or {}).get("start_price")
    spy_ret = (spy_now / spy0 - 1) if (spy0 and spy_now) else None
    excess = (basket_ret - spy_ret) if spy_ret is not None else None

    rows = []
    for sym, p in sorted(held.items()):
        c = p["market_value"] - p["unrealized_pl"]
        rows.append({"symbol": sym, "cost": c, "pl": p["unrealized_pl"], "ret": (p["unrealized_pl"] / c) if c else 0.0})
    greens = sum(1 for r in rows if r["ret"] > 0)
    hit_rate = (greens / len(rows)) if rows else None
    review_pct = journal.get("exit_review_pct", -0.10)
    breached = [r["symbol"] for r in rows if r["ret"] <= review_pct]

    # Drawdown of basket value from its running peak, over stored snapshots + now.
    series = [s["basket_value"] for s in journal.get("snapshots", []) if s.get("basket_value")]
    if value:
        series.append(value)
    peak, max_dd = 0.0, 0.0
    for v in series:
        peak = max(peak, v)
        if peak:
            max_dd = min(max_dd, v / peak - 1)

    cap = journal["order_cap"]
    over_cap = [o["symbol"] for o in journal.get("orders", []) if o["notional"] > cap + 1e-9]
    approved = set(journal["approved_symbols"])
    unrecorded = [s for s in held if s not in journal.get("positions", {})]
    off_list = [s for s in held if s not in approved]

    start = date.fromisoformat(journal["window"]["start"])
    end = date.fromisoformat(journal["window"]["end"])
    expected = max(_business_days(start, min(today, end)), 1)
    have = len({s["date"] for s in journal.get("snapshots", [])})
    coverage = min(have / expected, 1.0)

    return {
        "cost": cost, "value": value, "basket_ret": basket_ret, "spy_ret": spy_ret, "excess": excess,
        "rows": rows, "hit_rate": hit_rate, "breached": breached, "max_dd": max_dd,
        "over_cap": over_cap, "unrecorded": unrecorded, "off_list": off_list,
        "coverage": coverage, "days_left": (end - today).days, "day_n": (today - start).days,
        "kr": {
            "KR1": _early(_status(None if excess is None else excess >= krs["KR1"]["target"]), today, start),
            "KR2": _early(_status(None if hit_rate is None else hit_rate >= krs["KR2"]["target"]), today, start),
            # Basket drawdown inside the limit is on track; a single position at the
            # exit-review line downgrades that to WATCH, not OFF TRACK.
            "KR3": _status(max_dd >= -krs["KR3"]["target"], warn=bool(breached)),
            "KR4": _status(not over_cap and not unrecorded and not off_list),
            "KR5": _status(coverage >= krs["KR5"]["target"]),
        },
    }


def format_report(journal: dict, ev: dict, equity: float | None) -> str:
    pct = lambda x: "n/a" if x is None else f"{100 * x:+.2f}%"
    krs = journal["key_results"]
    lines = [
        f"PAPER OKR REPORT — day {ev['day_n']} of {journal['window']['start']} → {journal['window']['end']} ({ev['days_left']}d left)",
        f"Objective: {journal['objective']}",
        "",
        f"Basket: ${ev['value']:,.2f} on ${ev['cost']:,.2f} cost  ({pct(ev['basket_ret'])})"
        + (f" | account equity ${equity:,.2f}" if equity else ""),
        f"SPY since start: {pct(ev['spy_ret'])}   Excess vs SPY: {pct(ev['excess'])}",
        "",
        f"{'KR':4}{'Status':10}Measure",
        f"{'KR1':4}{ev['kr']['KR1']:10}{krs['KR1']['name']}: {pct(ev['excess'])}  (target ≥ {pct(krs['KR1']['target'])})",
        f"{'KR2':4}{ev['kr']['KR2']:10}{krs['KR2']['name']}: "
        + ("n/a" if ev["hit_rate"] is None else f"{100 * ev['hit_rate']:.0f}%")
        + f"  (target ≥ {100 * krs['KR2']['target']:.0f}%)",
        f"{'KR3':4}{ev['kr']['KR3']:10}{krs['KR3']['name']}: drawdown {pct(ev['max_dd'])}"
        + (f"; EXIT REVIEW: {', '.join(ev['breached'])}" if ev["breached"] else "")
        + f"  (limit −{100 * krs['KR3']['target']:.0f}%, per-position {pct(journal.get('exit_review_pct', -0.10))})",
        f"{'KR4':4}{ev['kr']['KR4']:10}{krs['KR4']['name']}: "
        + ("clean" if not (ev["over_cap"] or ev["unrecorded"] or ev["off_list"])
           else f"over-cap {ev['over_cap']} unrecorded {ev['unrecorded']} off-list {ev['off_list']}"),
        f"{'KR5':4}{ev['kr']['KR5']:10}{krs['KR5']['name']}: {100 * ev['coverage']:.0f}% of trading days snapshotted"
        f"  (target ≥ {100 * krs['KR5']['target']:.0f}%)",
        "",
        f"{'Pos':6}{'Cost':>10}{'P&L':>10}{'Ret':>9}",
    ]
    for r in sorted(ev["rows"], key=lambda r: -r["ret"]):
        flag = "  <- exit review" if r["symbol"] in ev["breached"] else ""
        lines.append(f"{r['symbol']:6}{r['cost']:10.2f}{r['pl']:10.2f}{100 * r['ret']:8.2f}%{flag}")
    lines += ["", "Caveat: " + journal["caveat"]]
    return "\n".join(lines)


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str | RoutineResult:
    try:
        journal = load_journal()
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return RoutineResult(text=f"OKR journal unavailable: {e}", failed=True)
    try:
        alpaca = AlpacaPaperClient()
    except AlpacaConfigError as e:
        return RoutineResult(text=f"Alpaca not configured.\n\n{e}", failed=True)

    try:
        positions = await alpaca.get_positions()
        account = await alpaca.get_account()
        quote = (await alpaca.latest_quote(journal["benchmark"]["symbol"])).get("quote", {})
    except AlpacaAPIError as e:
        return RoutineResult(text=f"Could not read the paper account: {e}", failed=True)

    bid, ask = float(quote.get("bp") or 0), float(quote.get("ap") or 0)
    spy_now = (bid + ask) / 2 if bid and ask else (ask or bid or None)

    pos = [{"symbol": p.symbol, "market_value": p.market_value, "unrealized_pl": p.unrealized_pl} for p in positions]
    today = datetime.now(timezone.utc).date()

    # Record today's snapshot first, so coverage and drawdown include it.
    if config.record_snapshot:
        value = sum(p["market_value"] for p in pos)
        cost = sum(p["market_value"] - p["unrealized_pl"] for p in pos)
        snaps = [s for s in journal.get("snapshots", []) if s["date"] != today.isoformat()]
        snaps.append({"date": today.isoformat(), "basket_value": value, "basket_cost": cost,
                      "equity": account.equity, "spy": spy_now})
        journal["snapshots"] = snaps
        atomic_write_json(journal_path(), journal)

    ev = evaluate(journal, pos, spy_now, today)
    return format_report(journal, ev, account.equity)
