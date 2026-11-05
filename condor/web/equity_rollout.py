"""Rollout gates for the versioned equity routes, independent of their adapters.

Disabling submissions preserves account reads, reconciliation, and cancellation.
Authentication / account ownership remain responsibilities of the route handlers.
"""

from __future__ import annotations

import logging
import os
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse

from condor.equity_observability import equity_correlation_id

log = logging.getLogger(__name__)


def rollout_status() -> dict[str, bool]:
    markets = os.getenv("EQUITY_MARKETS_ENABLED", "").strip().lower() == "true"
    execution = os.getenv("EQUITY_PAPER_TRADING_ENABLED", "").strip().lower() == "true"
    return {
        "markets_enabled": markets,
        "paper_submissions_enabled": markets and execution,
    }


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


async def equity_rollout_tap(request: Request, call_next):
    path = request.url.path
    research = _under(path, "/api/v1/equities")
    paper = _under(path, "/api/v1/paper/equities")
    if not research and not paper:
        return await call_next(request)

    correlation_id = uuid4().hex
    request.state.equity_correlation_id = correlation_id
    status = rollout_status()
    blocked = (research and not status["markets_enabled"]) or (
        paper and request.method == "POST" and not status["paper_submissions_enabled"]
    )
    if blocked and request.method != "OPTIONS":
        response = JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "equity_feature_disabled",
                    "message": "Paper Markets is not enabled.",
                }
            },
        )
    else:
        token = equity_correlation_id.set(correlation_id)
        try:
            response = await call_next(request)
        finally:
            equity_correlation_id.reset(token)
    response.headers["X-Correlation-ID"] = correlation_id
    # No URL, query, request body, account, symbol, or upstream exception text.
    log.info(
        "Equity API request correlation_id=%s method=%s status=%s",
        correlation_id,
        request.method,
        response.status_code,
    )
    return response
