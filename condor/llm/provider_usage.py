"""Reconcile the local spend ledger with OpenRouter's own usage figure.

The monthly guard (:mod:`condor.llm.budget`) counts what Condor itself sends. That leaves
two gaps: spend that happened before metering began, and spend by anything else holding
the same API key. Both are visible in OpenRouter's ``GET /api/v1/key`` (``usage_monthly``,
``usage_daily``), so a job re-reads it and rewrites ``spend-baseline.json`` from it. The
ledger then counts only what Condor sent after that reading.

Fail-safe by construction: a failed or implausible reading never loosens the guard.

* No network, a bad status, or a malformed body: the existing baseline stays.
* A provider figure *lower* than the baseline already held for the month (reporting lag):
  the existing baseline stays. Spend only ever ratchets up within a month.
* The new baseline's ``as_of`` is set a few minutes in the past, so requests that finished
  just before the reading, and that the provider may not have settled yet, are counted
  twice rather than not at all.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from condor.fsutil import atomic_write_json
from condor.llm.budget import get_governor, utc_day

logger = logging.getLogger(__name__)

USAGE_URL = "https://openrouter.ai/api/v1/key"
#: Requests newer than (reading - this) are counted again on top of the provider's figure.
OVERLAP_SEC = 180.0
SYNC_INTERVAL_SEC = 600
BASELINE_FILE = "spend-baseline.json"
READING_FILE = "provider-usage.json"


@dataclass(frozen=True)
class ProviderUsage:
    month_used: float
    day_used: float
    limit: float | None
    limit_remaining: float | None
    as_of: float


def _amount(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out >= 0 else None


async def fetch_usage(
    api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
    clock: Callable[[], float] = time.time,
    timeout: float = 15.0,
) -> ProviderUsage | None:
    """The key's current usage, or ``None`` on any failure. Never raises, never logs the key."""
    own = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        resp = await http.get(USAGE_URL, headers={"Authorization": f"Bearer {api_key}"})
        if resp.status_code != 200:
            logger.warning("OpenRouter usage read refused (HTTP %s)", resp.status_code)
            return None
        data = (resp.json() or {}).get("data") or {}
    except Exception as exc:  # noqa: BLE001 - a usage read must never break anything
        logger.warning("OpenRouter usage read failed (%s)", type(exc).__name__)
        return None
    finally:
        if own:
            await http.aclose()
    month, day = _amount(data.get("usage_monthly")), _amount(data.get("usage_daily"))
    if month is None or day is None:
        logger.warning("OpenRouter usage read returned no usable figures")
        return None
    return ProviderUsage(
        month_used=month,
        day_used=day,
        limit=_amount(data.get("limit")),
        limit_remaining=_amount(data.get("limit_remaining")),
        as_of=clock(),
    )


def _read_baseline(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return None
        for key in ("as_of", "day_used_usd", "month_used_usd"):
            if _amount(raw.get(key)) is None:
                return None
        return raw
    except (OSError, ValueError, TypeError):
        return None


def apply_usage(ledger_dir: Path, usage: ProviderUsage) -> str:
    """Rewrite the baseline from a provider reading. Returns what happened.

    ``written``: a new baseline replaced the old (or there was none, or it was unreadable).
    ``kept``: the provider's month figure is below the baseline already held for that
    month, which is reporting lag, so the stricter existing baseline stands.
    """
    day = utc_day(usage.as_of)
    month = day[:6]
    path = ledger_dir / BASELINE_FILE
    held = _read_baseline(path)
    if (
        held is not None
        and held.get("month") == month
        and usage.month_used + 1e-9 < float(held["month_used_usd"])
    ):
        return "kept"
    atomic_write_json(
        path,
        {
            "as_of": usage.as_of - OVERLAP_SEC,
            "month": month,
            "day": day,
            "day_used_usd": usage.day_used,
            "month_used_usd": usage.month_used,
            "source": "OpenRouter GET /api/v1/key, read "
            + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(usage.as_of)),
        },
    )
    atomic_write_json(ledger_dir / READING_FILE, asdict(usage))
    return "written"


async def sync_provider_usage(context: Any = None) -> str:
    """The scheduled job: read the key's usage and refresh the baseline from it.

    Does nothing unless a monthly cap is configured and a key is present.
    """
    governor = get_governor()
    if governor.config.monthly_cost_cap_usd is None:
        return "disabled"
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return "no-key"
    usage = await fetch_usage(key)
    if usage is None:
        return "unavailable"
    ledger_dir = governor.ledger.directory
    ledger_dir.mkdir(parents=True, exist_ok=True)
    outcome = apply_usage(ledger_dir, usage)
    logger.info(
        "Provider usage sync: %s (provider month $%.4f, day $%.4f)",
        outcome,
        usage.month_used,
        usage.day_used,
    )
    return outcome


def register_jobs(application) -> None:
    application.job_queue.run_repeating(
        sync_provider_usage,
        interval=SYNC_INTERVAL_SEC,
        first=15,
        name="llm_provider_usage_sync",
    )
