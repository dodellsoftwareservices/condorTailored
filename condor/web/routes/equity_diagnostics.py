"""Admin-only equity rollout / monitoring snapshot; performs no upstream I/O."""

from fastapi import APIRouter, Depends

from condor.equity_observability import equity_metrics
from condor.web.auth import get_current_user, require_admin
from condor.web.equity_rollout import rollout_status
from condor.web.models import WebUser

router = APIRouter(prefix="/settings/data-providers", tags=["settings"])


@router.get("/equities")
async def equity_diagnostics(user: WebUser = Depends(get_current_user)) -> dict:
    require_admin(user)
    metrics = equity_metrics.snapshot()
    counters = metrics["counters"]
    return {
        "environment": "paper",
        "rollout": rollout_status(),
        "metrics": metrics,
        # No observations is unknown, never a green health signal.
        "provider_observed": any(
            count for name, count in counters.items() if name.startswith("provider_")
        ),
    }
