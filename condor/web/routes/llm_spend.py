"""Where the paid-model spend stands against the monthly cap. Read-only, admin only."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends

from condor.llm.budget import BudgetExhausted, get_governor
from condor.llm.provider_usage import BASELINE_FILE, READING_FILE
from condor.web.auth import get_current_user, require_admin
from condor.web.models import WebUser

router = APIRouter(prefix="/llm", tags=["llm"])


def _read(path):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


@router.get("/spend")
async def spend(user: WebUser = Depends(get_current_user)):
    """The monthly dollar guard's own view: what counts, what is held, what is left."""
    require_admin(user)
    gov = get_governor()
    cfg = gov.config
    if not cfg.enabled or cfg.monthly_cost_cap_usd is None:
        return {"enabled": False}
    now = time.time()
    folder = gov.ledger.directory
    out = {
        "enabled": True,
        "cap_usd": cfg.monthly_cost_cap_usd,
        "reserve_usd": cfg.request_cost_reserve_usd,
        "allowed_paid_models": list(cfg.allowed_paid_models),
        "baseline": _read(folder / BASELINE_FILE),
        "provider": _read(folder / READING_FILE),
    }
    try:
        window = gov.ledger.spend_window(now)
    except BudgetExhausted as exc:
        # An unreadable baseline pauses paid requests; say so instead of guessing.
        return {**out, "status": "paused", "reason": str(exc)}
    booked = window["unpriced"] * cfg.request_cost_reserve_usd
    used = window["month_used_usd"] + booked
    return {
        **out,
        "status": "ok",
        "month": window["month"],
        "month_used_usd": window["month_used_usd"],
        "unpriced_responses": window["unpriced"],
        "unpriced_booked_usd": booked,
        "reserved_in_flight_usd": window["reserved_usd"],
        "counted_usd": used,
        "headroom_usd": max(
            cfg.monthly_cost_cap_usd - used - window["reserved_usd"], 0
        ),
        "day_used_usd": window["day_used_usd"],
    }
