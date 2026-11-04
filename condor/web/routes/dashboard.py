"""Read-only paper-account overview. Never submit or advance simulated orders."""

from __future__ import annotations

import asyncio
import logging
import time
from bisect import bisect_right
from typing import Literal

from fastapi import APIRouter, Depends, Query

from condor.asyncutil import SingleFlight
from condor.dashboard_history import record_equity
from condor.dashboard_metrics import (
    alpaca_outcomes,
    clean_points,
    number,
    outcome_metrics,
    risk_metrics,
)
from condor.paths import data_dir
from condor.web.auth import get_current_user, require_admin
from condor.web.models import WebUser
from condor.web.routes._equity_client import get_alpaca_client
from condor.web.routes.paper_equities import _max_order_notional
from condor.web.routes.sim_exchange import get_sim_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])
STARTING_CAPITAL = 100_000.0  # Operator-confirmed initial Alpaca paper balance.
RANGES = {"1D": 86400, "1W": 7 * 86400, "1M": 30 * 86400, "3M": 90 * 86400}
_cache: dict[tuple, tuple[float, dict]] = {}
_flights = SingleFlight()


async def _read(coro):
    try:
        return await asyncio.wait_for(coro, timeout=12)
    except Exception as exc:
        return exc


def _finish(account: dict, period: str, now: float) -> dict:
    points = clean_points(account.get("points", []), now)
    account["points"] = [p for p in points if p["timestamp"] >= now - RANGES[period]]
    equity, starting = account.get("equity"), account["starting_capital"]
    account["pnl"] = equity - starting if equity is not None else None
    account["roi_pct"] = (
        account["pnl"] / starting * 100
        if account["pnl"] is not None and starting > 0
        else None
    )
    outcomes = [
        r
        for r in account.pop("outcomes", [])
        if now - RANGES[period] <= r["timestamp"] <= now
    ]
    account.update(outcome_metrics(outcomes))
    if not account.get("outcomes_complete", False):
        for name in (
            "batting_average",
            "profit_factor",
            "average_win",
            "average_loss",
            "closed_trades",
            "wins",
            "losses",
            "breakeven",
            "gross_profit",
            "gross_loss",
        ):
            account[name] = None
    account.update(
        risk_metrics(account["points"], now, calendar=account.get("calendar", "crypto"))
    )
    return account


async def _alpaca(period: str, now: float) -> dict:
    account = {
        "id": "alpaca",
        "label": "Alpaca Paper",
        "status": "unavailable",
        "starting_capital": STARTING_CAPITAL,
        "equity": None,
        "cash": None,
        "deployed": None,
        "order_cap": _max_order_notional(),
        "calendar": "equities",
        "volume": None,
        "fees": None,
        "outcomes_complete": False,
        "points": [],
        "notes": [],
    }
    try:
        client = get_alpaca_client()
    except Exception:
        account["notes"] = ["Alpaca paper credentials are not configured."]
        return _finish(account, period, now)
    balance, positions, history, fills = await asyncio.gather(
        _read(client.get_account()),
        _read(client.get_positions()),
        _read(client.get_portfolio_history(period)),
        _read(client.get_fill_history()),
    )
    if (
        isinstance(balance, Exception)
        or number(balance.equity) is None
        or number(balance.cash) is None
    ):
        account["notes"] = [
            "Could not read the Alpaca paper account. Retry to reconnect."
        ]
        return _finish(account, period, now)
    account.update(status="online", equity=balance.equity, cash=balance.cash)
    if not isinstance(positions, Exception):
        account["deployed"] = sum(abs(p.market_value) for p in positions)
    else:
        account["notes"].append("Position exposure could not be loaded.")
    if not isinstance(history, Exception):
        account["points"] = [
            {"timestamp": stamp, "equity": value}
            for stamp, value in zip(
                history.get("timestamp", []), history.get("equity", [])
            )
        ]
    else:
        account["notes"].append("Portfolio history could not be loaded.")
    account["points"].append({"timestamp": now, "equity": balance.equity})
    if not isinstance(fills, Exception):
        rows, complete = fills
        outcomes, volume, matched = alpaca_outcomes(rows)
        account.update(
            outcomes=outcomes,
            volume=volume if complete and matched else None,
            outcomes_complete=complete and matched,
        )
        if not complete or not matched:
            account["notes"].append(
                "Closing-trade metrics need complete, matchable fill history."
            )
    else:
        account["notes"].append("Closing-trade history could not be loaded.")
    return _finish(account, period, now)


async def _kraken(period: str, now: float) -> dict:
    account = {
        "id": "kraken_sim",
        "label": "KrakenSim",
        "status": "unavailable",
        "starting_capital": STARTING_CAPITAL,
        "equity": None,
        "cash": None,
        "deployed": None,
        "order_cap": 2500,
        "volume": None,
        "fees": None,
        "outcomes_complete": False,
        "points": [],
        "notes": [],
    }
    result = await _read(get_sim_service().dashboard())
    if isinstance(result, Exception):
        account["notes"] = [
            "Could not mark KrakenSim holdings using current Kraken prices."
        ]
        return _finish(account, period, now)
    account.update(
        status="online",
        starting_capital=float(result["start_cash"]),
        equity=float(result["equity"]),
        cash=float(result["cash"]),
        deployed=float(result["gross_exposure"]),
        order_cap=float(result["max_order_notional"]),
        volume=float(result["volume"]),
        fees=float(result["fees"]),
        outcomes=result["outcomes"],
        outcomes_complete=True,
    )
    try:
        account["points"] = record_equity(
            "kraken_sim",
            now,
            account["equity"],
            created=result["created"],
            starting=account["starting_capital"],
        )
    except (OSError, ValueError):
        logger.exception("Could not record KrakenSim dashboard history")
        account["points"] = [{"timestamp": now, "equity": account["equity"]}]
        account["notes"].append("Equity history could not be saved.")
    return _finish(account, period, now)


def _combined(accounts: list[dict], now: float) -> dict:
    complete = all(a["status"] == "online" for a in accounts)

    def total(key):
        values = [a.get(key) for a in accounts]
        return sum(values) if all(v is not None for v in values) else None

    starting, equity = total("starting_capital"), total("equity")
    histories = [a["points"] for a in accounts]
    stamps = sorted({p["timestamp"] for rows in histories for p in rows})
    series = []
    indexes = [[p["timestamp"] for p in rows] for rows in histories]
    for stamp in stamps:
        values = []
        for account, rows, index in zip(accounts, histories, indexes):
            position = bisect_right(index, stamp) - 1
            # Do not backfill an unobserved simulation with invented prices.
            tolerance = 4 * 86400 if account["id"] == "alpaca" else 900
            if position < 0 or stamp - rows[position]["timestamp"] > tolerance:
                break
            values.append(rows[position]["equity"])
        if len(values) == len(accounts):
            series.append({"timestamp": stamp, "equity": sum(values)})
    wins, count = total("wins"), total("closed_trades")
    summary = {
        "id": "all",
        "label": "All paper accounts",
        "status": "online" if complete else "partial",
        "starting_capital": starting,
        "equity": equity,
        "cash": total("cash"),
        "deployed": total("deployed"),
        "pnl": equity - starting if equity is not None else None,
        "roi_pct": (
            (equity / starting - 1) * 100
            if equity is not None and starting > 0
            else None
        ),
        "batting_average": wins / count * 100 if count else None,
        "wins": wins,
        "closed_trades": count,
        "losses": total("losses"),
        "breakeven": total("breakeven"),
        "volume": total("volume"),
        "fees": total("fees"),
        "points": series,
        "notes": [],
        "outcomes_complete": all(a["outcomes_complete"] for a in accounts),
        "profit_factor": (
            total("gross_profit") / total("gross_loss")
            if total("gross_loss") and total("gross_profit") is not None
            else None
        ),
    }
    summary.update(risk_metrics(series, now))
    return summary


@router.get("/paper")
async def paper_dashboard(
    range: Literal["1D", "1W", "1M", "3M"] = Query("1M"),
    user: WebUser = Depends(get_current_user),
):
    require_admin(user)
    key = (str(data_dir()), range)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < 25:
        return cached[1]

    async def load():
        now = time.time()
        accounts = list(await asyncio.gather(_alpaca(range, now), _kraken(range, now)))
        response = {
            "as_of": now,
            "range": range,
            "mode": "paper",
            "accounts": accounts,
            "summary": _combined(accounts, now),
        }
        if len(_cache) > 16:
            _cache.clear()
        _cache[key] = (time.monotonic(), response)
        return response

    return await _flights.run(key, load)
