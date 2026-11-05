"""Sweep realized crypto P&L into ETFs on Alpaca paper. Reallocation, not a hedge."""

CATEGORY = "Analysis"

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from condor.alpaca_paper import (
    AlpacaAPIError,
    AlpacaConfigError,
    AlpacaPaperClient,
    utc_now_iso,
)
from condor.fetchers.bots import build_bots_page, fetch_bots_status
from condor.fsutil import atomic_write_json
from condor.paths import data_dir
from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)

LEDGER_NAME = "alpaca_etf_sweep_ledger.json"


class Config(BaseModel):
    """Move a share of realized crypto P&L into index ETFs on Alpaca paper."""

    weights: str = Field(
        default="VOO:70,VTI:30",
        description="Target split, SYMBOL:PERCENT comma-separated (VOO/VTI are Vanguard; SPY is State Street)",
    )
    sweep_pct: float = Field(
        default=50.0,
        ge=0.0,
        le=100.0,
        description="Percent of NEW realized P&L to sweep (the rest stays as trading capital)",
    )
    min_sweep_usd: float = Field(
        default=25.0,
        description="Skip the sweep below this amount — ETF orders are not worth the churn",
    )
    dry_run: bool = Field(
        default=True,
        description="Compute and report only; place no orders. Turn off deliberately.",
    )
    ignore_market_hours: bool = Field(
        default=False,
        description="Queue orders outside RTH (they fill at next open) instead of skipping",
    )


class LedgerError(RuntimeError):
    """The ledger exists and cannot be trusted."""


class StatusUnavailable(RuntimeError):
    """The bot status could not be read, so there is no P&L figure to act on."""


# ---------------------------------------------------------------------------
# Ledger — what has already been swept
# ---------------------------------------------------------------------------


def _ledger_path() -> Path:
    return data_dir() / LEDGER_NAME


def _load_ledger() -> dict:
    """The ledger, or a fresh one when none has been written yet.

    An unreadable ledger is an error, not a fresh start: ``swept_pnl_total`` is
    what stops lifetime P&L from being swept a second time, and starting from
    zero would do exactly that on the next run.
    """
    p = _ledger_path()
    if not p.exists():
        return {"swept_pnl_total": 0.0, "events": []}
    try:
        ledger = json.loads(p.read_text())
        float(ledger["swept_pnl_total"])
    except (ValueError, OSError, KeyError, TypeError) as e:
        raise LedgerError(
            f"{p} is unreadable ({type(e).__name__}). Refusing to sweep: a fresh "
            "ledger would sweep all lifetime P&L a second time. Repair the file "
            "or delete it deliberately."
        ) from e
    return ledger


def _save_ledger(ledger: dict) -> None:
    # Keep the file small: the running total is the load-bearing field.
    ledger["events"] = ledger.get("events", [])[-50:]
    atomic_write_json(_ledger_path(), ledger, indent=2)


def _parse_weights(raw: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        sym, _, pct = chunk.partition(":")
        try:
            out[sym.strip().upper()] = float(pct)
        except ValueError:
            continue
    total = sum(out.values())
    if total <= 0:
        return {}
    # Normalise so 70/30 and 7/3 behave identically.
    return {k: v / total for k, v in out.items()}


# ---------------------------------------------------------------------------
# Crypto side: realized P&L across active bots
# ---------------------------------------------------------------------------


async def _realized_pnl(client) -> tuple[float, int]:
    """Realized P&L in quote terms, summed across the controllers of active bots.

    Read through the same transform the bots page uses: the API nests a
    controller's figures one level down (``performance[ctrl]["performance"]``),
    which a hand-written walk over the raw status misses without complaint.

    Only *active* bots count. A bot that has been stopped and removed takes its
    P&L with it, and the sweep then waits until the remaining total climbs back
    past what the ledger already holds.
    """
    try:
        raw = await fetch_bots_status(client)
    except Exception as e:
        raise StatusUnavailable(f"Could not read bot status: {e}") from e

    controllers = build_bots_page(raw).get("controllers") or []
    total = sum(float(c.get("realized_pnl_quote") or 0.0) for c in controllers)
    return total, len(controllers)


# ---------------------------------------------------------------------------


async def run(
    config: Config, context: ContextTypes.DEFAULT_TYPE
) -> str | RoutineResult:
    """Compute the sweep, and on a live run place paper ETF orders."""
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None
    hb = await get_client(chat_id, context=context)
    if not hb:
        return RoutineResult(
            text="No server available. Configure servers in /config.", failed=True
        )

    weights = _parse_weights(config.weights)
    if not weights:
        return RoutineResult(
            text=f"Could not parse weights {config.weights!r}. Use e.g. `VOO:70,VTI:30`.",
            failed=True,
        )

    try:
        alpaca = AlpacaPaperClient()
    except AlpacaConfigError as e:
        return RoutineResult(text=f"Alpaca not configured.\n\n{e}", failed=True)

    try:
        realized, controllers = await _realized_pnl(hb)
        ledger = _load_ledger()
    except (StatusUnavailable, LedgerError) as e:
        return RoutineResult(text=f"Sweep not run: {e}", failed=True)

    already = float(ledger.get("swept_pnl_total") or 0.0)
    new_pnl = realized - already

    sweep = max(0.0, new_pnl) * (config.sweep_pct / 100.0)

    from condor.reports import ReportBuilder

    builder = ReportBuilder("ETF Sweep (paper)")
    builder.source("routine", "alpaca_etf_sweep").tags(["sweep", "etf", "alpaca"])
    builder.manual_order()
    builder.kpi("Realized P&L", f"${realized:,.2f}")
    builder.kpi("Already swept", f"${already:,.2f}")
    builder.kpi("New since last", f"${new_pnl:,.2f}")
    builder.kpi("Sweep amount", f"${sweep:,.2f}")
    builder.kpi("Controllers", str(controllers))

    if new_pnl <= 0:
        builder.markdown(
            "## Nothing to sweep\n\nRealized P&L has not advanced since the last "
            "sweep. Drawdowns are never swept backwards — the ledger only moves up, "
            "so a losing stretch simply defers the next transfer."
        )
        await builder.save()
        return f"No new realized P&L (${new_pnl:,.2f}). Nothing swept."

    if sweep < config.min_sweep_usd:
        builder.markdown(
            f"## Below threshold\n\n${sweep:,.2f} is under the "
            f"${config.min_sweep_usd:,.2f} minimum; letting it accumulate."
        )
        await builder.save()
        return f"Sweep ${sweep:,.2f} below ${config.min_sweep_usd:,.2f} minimum. Held."

    # Allocation table
    alloc = {sym: sweep * w for sym, w in weights.items()}
    builder.markdown("## Allocation")
    builder.table(
        [
            {
                "Symbol": s,
                "Weight": f"{weights[s] * 100:.0f}%",
                "Notional": f"${a:,.2f}",
            }
            for s, a in alloc.items()
        ]
    )

    if config.dry_run:
        builder.markdown(
            "## Dry run\n\nNo orders were placed and the ledger was **not** advanced, "
            "so this same amount is still pending. Set `dry_run=false` to execute."
        )
        await builder.save()
        return (
            f"DRY RUN: would sweep ${sweep:,.2f} into "
            + ", ".join(f"{s} ${a:,.2f}" for s, a in alloc.items())
            + ". Ledger unchanged."
        )

    # -- live (paper) execution -------------------------------------------
    try:
        is_open, nxt = await alpaca.is_market_open()
    except AlpacaAPIError as e:
        await builder.save()
        return RoutineResult(text=f"Alpaca clock check failed: {e}", failed=True)

    if not is_open and not config.ignore_market_hours:
        builder.markdown(
            f"## Market closed\n\nNext open: {nxt}. Nothing placed, ledger unchanged. "
            "Set `ignore_market_hours=true` to queue orders for the open instead."
        )
        await builder.save()
        return f"Equities market closed (next open {nxt}). Sweep deferred."

    placed, failed = [], []
    for sym, notional in alloc.items():
        if notional < 1.0:
            continue
        try:
            order = await alpaca.submit_notional_order(sym, notional, side="buy")
            placed.append(
                {
                    "Symbol": sym,
                    "Notional": f"${notional:,.2f}",
                    "Order": str(order.get("id", ""))[:8],
                    "Status": order.get("status", "?"),
                }
            )
        except (AlpacaAPIError, ValueError) as e:
            failed.append(
                {"Symbol": sym, "Notional": f"${notional:,.2f}", "Error": str(e)[:120]}
            )

    if placed:
        builder.markdown("## Orders placed (paper)")
        builder.table(placed)
    if failed:
        builder.markdown("## Failed")
        builder.table(failed)

    # Advance the ledger only by what Alpaca accepted, so a partial failure leaves
    # the unplaced remainder pending instead of silently vanishing. "Accepted" is
    # not "filled": an order queued outside market hours counts from the moment
    # Alpaca takes it.
    ledger_warning = ""
    if placed:
        placed_notional = sum(
            alloc[row["Symbol"]] for row in placed if row["Symbol"] in alloc
        )
        consumed = (
            placed_notional / (config.sweep_pct / 100.0) if config.sweep_pct else 0.0
        )
        ledger["swept_pnl_total"] = already + consumed
        ledger.setdefault("events", []).append(
            {
                "at": utc_now_iso(),
                "realized_pnl": realized,
                "swept_usd": placed_notional,
                "symbols": [r["Symbol"] for r in placed],
            }
        )
        try:
            _save_ledger(ledger)
        except OSError as e:
            # The orders are already with Alpaca; say so loudly rather than let
            # the next run sweep the same P&L again without anyone knowing.
            logger.error("Sweep ledger not saved after orders were placed: %s", e)
            ledger_warning = (
                f" LEDGER NOT SAVED ({e}): the next run will sweep this P&L again."
            )

    builder.markdown(
        "### What this is\n\n"
        "A **reallocation**, not a hedge. VOO and VTI are positively correlated "
        "with crypto in risk-off conditions — they will not offset a crypto "
        "drawdown, and nothing here is sized against live crypto exposure. "
        "This moves realized profit off the trading book and into an index. "
        "Both legs are paper: no real funds moved, and in production no API "
        "exists to wire cash from an exchange to a brokerage anyway."
    )
    await builder.save()

    if not placed:
        return RoutineResult(
            text=f"Sweep of ${sweep:,.2f} failed entirely — {len(failed)} order(s) rejected. Ledger unchanged.",
            failed=True,
        )
    return (
        f"Swept ${sum(alloc[r['Symbol']] for r in placed):,.2f} into "
        + ", ".join(r["Symbol"] for r in placed)
        + (f" ({len(failed)} failed)" if failed else "")
        + " on Alpaca paper."
        + ledger_warning
    )
