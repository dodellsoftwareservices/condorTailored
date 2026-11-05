"""Small durable history of observed simulation equity, sampled at five minutes."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from condor.fsutil import atomic_write_json
from condor.paths import data_dir

logger = logging.getLogger(__name__)

# Account keys are chosen by the server, never taken from a URL. One per Kraken desk:
# spot trader (hodl), market maker, perpetual futures.
DESK_ACCOUNTS = frozenset({"kraken_sim", "kraken_mm", "kraken_fut"})


def record_equity(
    account: str, timestamp: float, equity: float, *, created: float, starting: float
) -> list[dict]:
    # Account keys are selected by the server, never taken from a URL.
    if account not in DESK_ACCOUNTS:
        raise ValueError("Unknown dashboard history account")
    path = data_dir() / "dashboard" / f"{account}.json"
    history = json.loads(path.read_text()) if path.exists() else []
    # Preserve the known opening cash point; do not invent intervening marks.
    if not history or history[0]["timestamp"] != created:
        history = [{"timestamp": created, "equity": starting}]
    if timestamp > created:
        point = {"timestamp": timestamp, "equity": equity}
        if len(history) > 1 and int(history[-1]["timestamp"] // 300) == int(
            timestamp // 300
        ):
            history[-1] = point
        else:
            history.append(point)
    # Keep roughly three months of five-minute marks, plus known opening cash.
    history = [history[0], *history[1:][-27000:]]
    atomic_write_json(path, history)
    return history


async def sample_sim_equity(context=None) -> None:
    """Collect marks for every Kraken desk, even when the browser is closed.

    Never matches orders. Each desk is sampled independently so one unreachable
    ledger or market-data call does not stop the others recording.
    """
    from condor.web.routes.sim_exchange import (
        get_fut_service,
        get_mm_service,
        get_sim_service,
    )

    desks = (
        ("kraken_sim", "kraken_default.json", get_sim_service),
        ("kraken_mm", "kraken_mm.json", get_mm_service),
        ("kraken_fut", "kraken_futures.json", get_fut_service),
    )
    for account, ledger, getter in desks:
        if not (data_dir() / "sim_exchange" / ledger).exists():
            continue
        try:
            result = await asyncio.wait_for(getter().dashboard(), timeout=12)
            record_equity(
                account,
                time.time(),
                float(result["equity"]),
                created=result["created"],
                starting=float(result["start_cash"]),
            )
        except Exception as exc:
            logger.warning(
                "Dashboard equity sample for %s unavailable (%s)",
                account,
                type(exc).__name__,
            )


def register_jobs(application) -> None:
    application.job_queue.run_repeating(
        sample_sim_equity, interval=300, first=10, name="dashboard_equity_sample"
    )
