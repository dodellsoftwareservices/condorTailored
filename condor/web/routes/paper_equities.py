"""Paper-equity account state and order submission, via the shared Alpaca
paper account (condor/alpaca_paper.py) — one account for this install, not
per-user and not per-Hummingbot-server.

``equity_rollout_tap`` (already wired into ``create_app()``) gates this
prefix: GET/DELETE always reach these handlers once ``EQUITY_MARKETS_ENABLED``
is set, so a position can always be read and an order always cancelled even
with trading turned off; POST additionally needs
``EQUITY_PAPER_TRADING_ENABLED``. Nothing here re-checks those flags.

Admin-only throughout, reads included — not merely "logged in". An
independent review (2026-10-31) correctly caught that an earlier version of
this module claimed "any approved user" matched the precedent other mutating
settings routes set; it doesn't. ``add_server``/``delete_server`` check
``has_server_access``/ownership in-body, and ``add_credential``/
``delete_credential`` go through a ``require_server_access`` dependency
*plus* an explicit owner check — every real precedent here is "more than
authenticated," not "merely authenticated". This account has no per-user
grants of its own yet (one shared Alpaca login for the whole install), so
admin is the honest equivalent until that exists. Research in ``equities.py``
stays open to any approved user: it never reads this account's state.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException

from condor.alpaca_paper import AlpacaAPIError, AlpacaPaperClient, AlpacaTransportError
from condor.paper_valuation import read_paper_valuation
from condor.web.auth import get_current_user, require_admin
from condor.web.models import (
    EquityAccount,
    EquityMarketStatus,
    EquityOrderRequest,
    EquityOrderResult,
    EquityPosition,
    WebUser,
)
from condor.web.routes._deeds import record_ui_deed
from condor.web.routes._equity_client import get_alpaca_client
from condor.web.routes._errors import equity_upstream_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/paper/equities", tags=["equities"])

# A floor under an otherwise-unbounded order, not the deterministic preflight
# (fresh quote, account/open-order exposure, concentration policy) the
# original integration plan calls for — that is real future-milestone work.
# This alone is cheap and closes the sharpest gap an independent review
# demonstrated directly: a mocked $100,000,000 order reached submission with
# zero application-level checks, relying entirely on the broker to refuse it.
_DEFAULT_MAX_ORDER_NOTIONAL = 10_000.0


def _max_order_notional() -> float:
    """``EQUITY_MAX_ORDER_NOTIONAL``, or the built-in default.

    The default applies even when nobody has configured anything — a cap
    that only protects an install that remembered to set an env var is not
    much of a cap. Blank, unparseable or non-positive falls back to the
    default rather than disabling the check.
    """
    raw = os.environ.get("EQUITY_MAX_ORDER_NOTIONAL", "").strip()
    if not raw:
        return _DEFAULT_MAX_ORDER_NOTIONAL
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_MAX_ORDER_NOTIONAL
    return value if value > 0 else _DEFAULT_MAX_ORDER_NOTIONAL


@router.get("/account", response_model=EquityAccount)
async def get_equity_account(
    user: WebUser = Depends(get_current_user),
    client: AlpacaPaperClient = Depends(get_alpaca_client),
) -> EquityAccount:
    require_admin(user)
    try:
        valuation = await read_paper_valuation(client)
    except AlpacaAPIError as e:
        logger.exception("Equity account fetch failed")
        raise equity_upstream_error(
            "equity_provider_error", "Failed to fetch account", e
        ) from None
    return EquityAccount(
        equity=valuation.equity,
        cash=valuation.account.cash,
        buying_power=valuation.account.buying_power,
        provider_equity=valuation.provider_equity,
        valuation_adjusted=bool(valuation.adjusted_symbols),
        valuation_note=" ".join(valuation.notes),
    )


@router.get("/positions", response_model=list[EquityPosition])
async def get_equity_positions(
    user: WebUser = Depends(get_current_user),
    client: AlpacaPaperClient = Depends(get_alpaca_client),
) -> list[EquityPosition]:
    require_admin(user)
    try:
        valuation = await read_paper_valuation(client)
    except AlpacaAPIError as e:
        logger.exception("Equity positions fetch failed")
        raise equity_upstream_error(
            "equity_provider_error", "Failed to fetch positions", e
        ) from None
    if not valuation.reliable:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "equity_valuation_unverified",
                "message": "Paper positions require split reconciliation.",
            },
        )
    return [
        EquityPosition(
            symbol=p.symbol,
            qty=p.qty,
            market_value=p.market_value,
            unrealized_pl=p.unrealized_pl,
            valuation_adjusted=p.symbol in valuation.adjusted_symbols,
        )
        for p in valuation.positions
    ]


@router.get("/market-status", response_model=EquityMarketStatus)
async def get_equity_market_status(
    user: WebUser = Depends(get_current_user),
    client: AlpacaPaperClient = Depends(get_alpaca_client),
) -> EquityMarketStatus:
    require_admin(user)
    try:
        is_open, next_transition = await client.is_market_open()
    except AlpacaAPIError as e:
        logger.exception("Equity market status fetch failed")
        raise equity_upstream_error(
            "equity_provider_error", "Failed to fetch market status", e
        ) from None
    return EquityMarketStatus(is_open=is_open, next_transition=next_transition)


@router.post("/orders", response_model=EquityOrderResult)
async def submit_equity_order(
    body: EquityOrderRequest,
    user: WebUser = Depends(get_current_user),
    client: AlpacaPaperClient = Depends(get_alpaca_client),
) -> EquityOrderResult:
    """Submit one paper market order. Notional only — no share-quantity or
    limit orders yet; see AlpacaPaperClient's own surface.

    Three outcomes are kept distinct rather than collapsed to one error
    shape, because ``AlpacaTransportError``'s "an order may exist, do not
    blindly retry" meaning is worth preserving even without a reconcile UI:
    a bad request (400) is safe to correct and resubmit, an ambiguous
    transport failure (502, ``equity_order_ambiguous``) is not, and a clean
    rejection from Alpaca itself (400/502 via ``equity_upstream_error``) is
    neither.
    """
    require_admin(user)

    limit = _max_order_notional()
    if body.notional > limit:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "equity_order_too_large",
                "message": f"${body.notional:.2f} exceeds the ${limit:.2f} per-order limit",
            },
        )

    try:
        raw = await client.submit_notional_order(body.symbol, body.notional, body.side)
    except ValueError as e:
        # Pre-HTTP validation inside the client itself (bad notional/side).
        raise HTTPException(
            status_code=400,
            detail={"code": "equity_invalid_request", "message": str(e)},
        ) from None
    except AlpacaTransportError as e:
        logger.exception("Equity order submission had an ambiguous transport failure")
        raise HTTPException(
            status_code=502,
            detail={
                "code": "equity_order_ambiguous",
                "message": f"Order may have been submitted: {e}. Check positions before retrying.",
            },
        ) from None
    except AlpacaAPIError as e:
        logger.exception("Equity order submission was rejected")
        raise equity_upstream_error(
            "equity_order_rejected", "Failed to submit paper order", e
        ) from None

    # No subject: record_direct only ever keeps it for the deploy verb (it
    # names a claimed bot, not a general-purpose field) — every other
    # existing route (stop_executor, clear_position_held) puts the
    # identifying detail in summary instead, which already has the symbol.
    record_ui_deed(
        user,
        verb="submit_equity_order",
        summary=f"Paper {body.side} ${body.notional:.2f} of {body.symbol.upper()}",
    )
    return EquityOrderResult(
        id=str(raw.get("id") or ""),
        symbol=body.symbol.upper(),
        side=body.side,
        notional=body.notional,
        status=str(raw.get("status") or ""),
        submitted_at=raw.get("submitted_at"),
    )
