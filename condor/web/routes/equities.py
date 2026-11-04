"""Read-only equity research: symbol search, instrument info, quote, bars.

Gated by ``equity_rollout_tap`` (``condor/web/equity_rollout.py``, already
wired into ``create_app()``) on ``EQUITY_MARKETS_ENABLED`` before routing
ever reaches these handlers — nothing here re-checks that flag itself.

Like ``equity_diagnostics.py``, not server-scoped: ``Depends(get_current_user)``
only, no ``{name}`` path segment and no ``require_server_access``, because
Alpaca paper trading is one global account on this install today, not a
configured Hummingbot server.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from condor.alpaca_paper import AlpacaAPIError, AlpacaPaperClient
from condor.equity_market_data import (
    EquityDataError,
    EquitySymbolNotFound,
    fetch_bars,
    fetch_instrument,
    search_symbols,
)
from condor.equity_observability import (
    EquityMeasurement,
    equity_correlation_id,
    equity_metrics,
)
from condor.web.auth import get_current_user
from condor.web.equity_rollout import rollout_status
from condor.web.models import (
    CandleData,
    EquityBarsResponse,
    EquityInstrument,
    EquityQuote,
    EquitySearchResponse,
    EquitySearchResult,
    WebUser,
)
from condor.web.routes._equity_client import get_alpaca_client
from condor.web.routes._errors import equity_upstream_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/equities", tags=["equities"])

_BAR_INTERVALS = ("5m", "15m", "1h", "1d")
_BAR_PERIODS = ("5d", "1mo", "3mo", "6mo", "1y", "2y", "5y")


def _parse_float(value: Any) -> float | None:
    """``None`` when ``value`` is missing or not a real number — distinct from
    a field that is genuinely ``0``, which a bare ``float(x or 0.0)`` cannot
    tell apart from "absent"."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Static paths must be declared before the dynamic "/{symbol}" path below —
# FastAPI/Starlette matches in declaration order, and "search"/"rollout-status"
# would otherwise be swallowed as a literal (invalid) symbol.


@router.get("/search", response_model=EquitySearchResponse)
async def search_equities(
    q: str = Query(..., min_length=1),
    limit: int = Query(8, ge=1, le=25),
    user: WebUser = Depends(get_current_user),
) -> EquitySearchResponse:
    try:
        results = await search_symbols(q, limit)
    except EquityDataError as e:
        logger.exception("Equity search failed for a query")
        raise equity_upstream_error(
            "equity_provider_error", "Search failed", e
        ) from None
    return EquitySearchResponse(
        query=q,
        results=[
            EquitySearchResult(
                symbol=r.symbol,
                name=r.name,
                exchange=r.exchange,
                quote_type=r.quote_type,
            )
            for r in results
        ],
    )


@router.get("/rollout-status")
async def get_equity_rollout_status(user: WebUser = Depends(get_current_user)) -> dict:
    """So the order form can disable itself proactively rather than only
    discovering a disabled feature from a 503 on submit."""
    return rollout_status()


@router.get("/{symbol}", response_model=EquityInstrument)
async def get_equity_instrument(
    symbol: str, user: WebUser = Depends(get_current_user)
) -> EquityInstrument:
    try:
        instrument = await fetch_instrument(symbol)
    except EquitySymbolNotFound:
        raise HTTPException(
            status_code=404,
            detail={"code": "equity_symbol_not_found", "message": symbol},
        ) from None
    except EquityDataError as e:
        logger.exception("Equity instrument lookup failed for a symbol")
        raise equity_upstream_error(
            "equity_provider_error", "Failed to fetch instrument", e
        ) from None
    return EquityInstrument(
        symbol=instrument.symbol,
        name=instrument.name,
        exchange=instrument.exchange,
        currency=instrument.currency,
        quote_type=instrument.quote_type,
    )


@router.get("/{symbol}/quote", response_model=EquityQuote)
async def get_equity_quote(
    symbol: str,
    user: WebUser = Depends(get_current_user),
    client: AlpacaPaperClient = Depends(get_alpaca_client),
) -> EquityQuote:
    """The execution-grade quote, from Alpaca's own data feed — not yfinance.

    This is the price an order would actually be priced against, so it comes
    from the broker, not the free research feed used for search/charting.
    """
    symbol = symbol.strip().upper()
    try:
        raw = await client.latest_quote(symbol)
    except AlpacaAPIError as e:
        logger.exception("Equity quote fetch failed for a symbol")
        raise equity_upstream_error(
            "equity_provider_error", "Failed to fetch quote", e
        ) from None

    # Alpaca's documented shape is {"symbol": ..., "quote": {bp, bs, ap, as, t}},
    # "t" an ISO8601 timestamp — not checked against a live response in this
    # environment (no credentials configured here). A shape with no usable
    # price is a provider error (502), matching market.py's own "Unexpected
    # response format" precedent for exactly this case — it is NOT turned
    # into a 200 of zeros, which would read to a caller as "AAPL is trading
    # at $0" rather than "the quote couldn't be read".
    quote = raw.get("quote") if isinstance(raw, dict) else None
    bid = _parse_float(quote.get("bp")) if isinstance(quote, dict) else None
    ask = _parse_float(quote.get("ap")) if isinstance(quote, dict) else None
    if bid is None and ask is None:
        logger.error("Equity quote response had no usable bid or ask")
        raise HTTPException(
            status_code=502,
            detail={
                "code": "equity_provider_error",
                "message": "Alpaca returned no usable quote",
            },
        )

    # An unparseable or missing timestamp is reported as maximally stale
    # (epoch), never as "now": manufacturing freshness for data whose actual
    # age is unknown is worse than being honestly very out of date — it is
    # exactly quote_age_seconds' job to let a caller refuse a quote that
    # cannot be trusted, which a fabricated "just arrived" age defeats.
    timestamp = 0.0
    ts_raw = quote.get("t")
    if ts_raw is not None:
        try:
            timestamp = datetime.fromisoformat(
                str(ts_raw).replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError):
            logger.warning(
                "Equity quote had an unparseable timestamp; reporting as stale"
            )

    age = max(0.0, time.time() - timestamp)
    equity_metrics.observe(EquityMeasurement.QUOTE_AGE_SECONDS, age)
    logger.info(
        "Equity quote request source=alpaca correlation_id=%s quote_age_seconds=%.3f",
        equity_correlation_id.get(),
        age,
    )
    return EquityQuote(
        symbol=symbol,
        bid=bid or 0.0,
        ask=ask or 0.0,
        bid_size=_parse_float(quote.get("bs")) or 0.0,
        ask_size=_parse_float(quote.get("as")) or 0.0,
        timestamp=timestamp,
    )


@router.get("/{symbol}/bars", response_model=EquityBarsResponse)
async def get_equity_bars(
    symbol: str,
    interval: str = Query("1d", pattern="^(" + "|".join(_BAR_INTERVALS) + ")$"),
    period: str = Query("3mo", pattern="^(" + "|".join(_BAR_PERIODS) + ")$"),
    user: WebUser = Depends(get_current_user),
) -> EquityBarsResponse:
    try:
        rows = await fetch_bars(symbol, period, interval)
    except EquitySymbolNotFound:
        raise HTTPException(
            status_code=404,
            detail={"code": "equity_symbol_not_found", "message": symbol},
        ) from None
    except EquityDataError as e:
        logger.exception("Equity bars fetch failed for a symbol")
        raise equity_upstream_error(
            "equity_provider_error", "Failed to fetch bars", e
        ) from None
    return EquityBarsResponse(
        symbol=symbol.strip().upper(),
        interval=interval,
        period=period,
        bars=[CandleData(**row) for row in rows],
    )
