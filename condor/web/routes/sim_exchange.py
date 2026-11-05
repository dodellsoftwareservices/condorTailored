"""Simulated Kraken spot exchange: live public prices, simulated fills and costs.

Admin-only, like the paper-equity routes. Nothing here can reach a real
account: the service behind it only ever calls Kraken's public market-data
endpoints. Risk limits and cost settings are edited at ``/config`` by an admin;
the agent's MCP tools do not expose that route, so a model cannot raise its own
limits.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from condor.paths import data_dir
from condor.sim_exchange.engine import Rejected
from condor.sim_exchange.futures import CANDLE_MINUTES as FUT_CANDLE_MINUTES
from condor.sim_exchange.futures import FuturesService, KrakenFuturesClient
from condor.sim_exchange.kraken_public import KrakenPublicClient, KrakenPublicError
from condor.sim_exchange.market_maker import MMConfig, MMRunner
from condor.sim_exchange.service import SimService
from condor.web.auth import get_current_user, require_admin
from condor.web.models import WebUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sim/kraken", tags=["sim-exchange"])

_service: SimService | None = None

CANDLE_MINUTES = (1, 5, 15, 30, 60, 240, 1440)


def get_sim_service() -> SimService:
    global _service
    if _service is None:
        base = data_dir()
        _service = SimService(
            KrakenPublicClient(),
            base / "sim_exchange" / "kraken_default.json",
            ledger_dir=base / "llm_budget",
        )
    return _service


_mm_service: SimService | None = None
_mm_runner: MMRunner | None = None


def get_mm_service() -> SimService:
    """The market maker's own simulated account, so it never shares limits, open-order
    slots or inventory with the LLM agent's account."""
    global _mm_service
    if _mm_service is None:
        _mm_service = SimService(
            KrakenPublicClient(), data_dir() / "sim_exchange" / "kraken_mm.json"
        )
    return _mm_service


def get_mm_runner() -> MMRunner:
    global _mm_runner
    if _mm_runner is None:
        _mm_runner = MMRunner(
            get_mm_service(),
            data_dir() / "sim_exchange" / "mm_events.jsonl",
            autostart_path=data_dir() / "sim_exchange" / "mm_autostart.json",
        )
    return _mm_runner


def _dec(value: str | float | None, name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise HTTPException(
            400, {"code": "sim_bad_number", "message": f"{name} is not a number"}
        ) from None
    if not d.is_finite() or d <= 0:
        raise HTTPException(
            400,
            {"code": "sim_bad_number", "message": f"{name} must be a positive number"},
        )
    return d


async def _guard(coro):
    try:
        return await coro
    except Rejected as e:
        raise HTTPException(400, {"code": "sim_rejected", "message": str(e)}) from None
    except KrakenPublicError as e:
        logger.warning("Kraken public data error: %s", e)
        raise HTTPException(
            502, {"code": "kraken_public_error", "message": str(e)}
        ) from None


class OrderRequest(BaseModel):
    pair: str
    side: Literal["buy", "sell"]
    type: Literal["market", "limit"] = "market"
    qty: str | None = None
    quote_amount: str | None = None
    price: str | None = None
    post_only: bool = False
    client_order_id: str = ""


class ConfigRequest(BaseModel):
    limits: dict[str, str | int | float] | None = None
    costs: dict[str, str | int | float] | None = None
    # A labelled what-if for fees; {} clears it. Owner-only, never an agent tool.
    fee_override: dict[str, str | int | float] | None = None


class ResetRequest(BaseModel):
    start_cash: str = "50000"


@router.get("/snapshot")
async def snapshot(
    pair: str = Query(..., description="e.g. BTC-USD"),
    candles: int = Query(0, ge=0, le=200),
    candle_minutes: int = Query(15, description="One of 1, 5, 15, 30, 60, 240, 1440"),
    user: WebUser = Depends(get_current_user),
):
    require_admin(user)
    # A plain int, checked here: FastAPI will not coerce the query string "15" to an
    # integer Literal, which made every call that sent the parameter a 422.
    if candle_minutes not in CANDLE_MINUTES:
        raise HTTPException(
            400,
            {
                "code": "sim_bad_candle_minutes",
                "message": f"candle_minutes must be one of {list(CANDLE_MINUTES)}",
            },
        )
    return await _guard(get_sim_service().snapshot(pair, candles, candle_minutes))


@router.post("/orders")
async def place_order(body: OrderRequest, user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(
        get_sim_service().place(
            pair=body.pair,
            side=body.side,
            type=body.type,
            qty=_dec(body.qty, "qty"),
            quote_amount=_dec(body.quote_amount, "quote_amount"),
            price=_dec(body.price, "price"),
            post_only=body.post_only,
            client_order_id=body.client_order_id,
        )
    )


@router.get("/orders")
async def list_orders(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_sim_service().orders())


@router.delete("/orders/{order_id}")
async def cancel_order(order_id: str, user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_sim_service().cancel(order_id))


@router.post("/advance")
async def advance(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_sim_service().advance())


@router.get("/portfolio")
async def portfolio(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_sim_service().portfolio())


@router.get("/costs")
async def costs(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_sim_service().costs())


@router.get("/config")
async def get_config(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_sim_service().config())


@router.put("/config")
async def put_config(body: ConfigRequest, user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(
        get_sim_service().set_config(body.limits, body.costs, body.fee_override)
    )


@router.post("/reset")
async def reset(body: ResetRequest, user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_sim_service().reset(_dec(body.start_cash, "start_cash")))


# ── Market maker (its own account) ──────────────────────────────────────


class MMStartRequest(BaseModel):
    pairs: list[str] | None = None
    order_notional: str | None = None
    half_spread_bps: str | None = None
    min_profit_bps: str | None = None
    max_inventory_notional: str | None = None
    skew_strength: str | None = None
    refresh_sec: float | None = None
    requote_bps: str | None = None
    max_quote_age_sec: float | None = None
    max_loss_usd: str | None = None


@router.get("/mm")
async def mm_status(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_mm_runner().status())


@router.post("/mm/start")
async def mm_start(body: MMStartRequest, user: WebUser = Depends(get_current_user)):
    require_admin(user)
    try:
        cfg = MMConfig.from_dict(body.model_dump(exclude_none=True))
    except (ValueError, InvalidOperation) as e:
        raise HTTPException(400, {"code": "mm_bad_config", "message": str(e)}) from None
    try:
        await get_mm_runner().start(cfg)
    except RuntimeError as e:
        raise HTTPException(409, {"code": "mm_running", "message": str(e)}) from None
    return {"started": True, "config": cfg.public()}


@router.put("/mm/config")
async def put_mm_config(body: ConfigRequest, user: WebUser = Depends(get_current_user)):
    """Owner-only knobs for the maker's account, e.g. a fee what-if."""
    require_admin(user)
    return await _guard(
        get_mm_service().set_config(body.limits, body.costs, body.fee_override)
    )


@router.get("/mm/config")
async def get_mm_config(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_mm_service().config())


async def resume_market_maker_on_boot() -> bool:
    """Called once from main.py at startup. Never raises: a maker that cannot resume stays stopped."""
    try:
        return await get_mm_runner().resume_from_disk()
    except Exception:  # noqa: BLE001
        logger.exception("Market maker boot resume failed; leaving it stopped")
        return False


@router.post("/mm/stop")
async def mm_stop(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_mm_runner().stop())


@router.get("/mm/portfolio")
async def mm_portfolio(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_mm_service().portfolio())


@router.get("/mm/costs")
async def mm_costs(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_mm_service().costs())


@router.post("/mm/reset")
async def mm_reset(body: ResetRequest, user: WebUser = Depends(get_current_user)):
    require_admin(user)
    if get_mm_runner().running:
        raise HTTPException(
            409,
            {
                "code": "mm_running",
                "message": "Stop the market maker before resetting its account",
            },
        )
    return await _guard(get_mm_service().reset(_dec(body.start_cash, "start_cash")))


# ── Futures (perpetuals) paper account ──────────────────────────────────
#
# Same rules as the spot routes: admin-only, and the model reaches them through the MCP
# tools, which never expose limits, costs or the fee what-if.

_fut_service: FuturesService | None = None


def get_fut_service() -> FuturesService:
    global _fut_service
    if _fut_service is None:
        base = data_dir()
        _fut_service = FuturesService(
            KrakenFuturesClient(),
            base / "sim_exchange" / "kraken_futures.json",
            ledger_dir=base / "llm_budget",
        )
    return _fut_service


class FutOrderRequest(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    type: Literal["market", "limit"] = "market"
    qty: str | None = None
    price: str | None = None
    post_only: bool = False
    reduce_only: bool = False
    client_order_id: str = ""


@router.get("/fut/snapshot")
async def fut_snapshot(
    symbol: str = Query(..., description="e.g. BTC-USD or PF_XBTUSD"),
    candles: int = Query(0, ge=0, le=200),
    candle_minutes: int = Query(15),
    user: WebUser = Depends(get_current_user),
):
    require_admin(user)
    if candle_minutes not in FUT_CANDLE_MINUTES:
        raise HTTPException(
            400,
            {
                "code": "sim_bad_candle_minutes",
                "message": f"candle_minutes must be one of {list(FUT_CANDLE_MINUTES)}",
            },
        )
    return await _guard(get_fut_service().snapshot(symbol, candles, candle_minutes))


@router.post("/fut/orders")
async def fut_place_order(
    body: FutOrderRequest, user: WebUser = Depends(get_current_user)
):
    require_admin(user)
    return await _guard(
        get_fut_service().place(
            symbol=body.symbol,
            side=body.side,
            type=body.type,
            qty=_dec(body.qty, "qty"),
            price=_dec(body.price, "price"),
            post_only=body.post_only,
            reduce_only=body.reduce_only,
            client_order_id=body.client_order_id,
        )
    )


@router.get("/fut/orders")
async def fut_list_orders(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_fut_service().orders())


@router.delete("/fut/orders/{order_id}")
async def fut_cancel_order(order_id: str, user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_fut_service().cancel(order_id))


@router.get("/fut/portfolio")
async def fut_portfolio(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_fut_service().portfolio())


@router.get("/fut/costs")
async def fut_costs(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_fut_service().costs())


@router.get("/fut/config")
async def fut_get_config(user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_fut_service().config())


@router.put("/fut/config")
async def fut_put_config(
    body: ConfigRequest, user: WebUser = Depends(get_current_user)
):
    require_admin(user)
    return await _guard(
        get_fut_service().set_config(body.limits, body.costs, body.fee_override)
    )


@router.post("/fut/reset")
async def fut_reset(body: ResetRequest, user: WebUser = Depends(get_current_user)):
    require_admin(user)
    return await _guard(get_fut_service().reset(_dec(body.start_cash, "start_cash")))
