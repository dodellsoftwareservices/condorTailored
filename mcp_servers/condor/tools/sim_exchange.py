"""Simulated Kraken spot exchange: live public prices, simulated fills and costs.

Every call goes through the main process (``/sim/kraken/*``), which is the single
writer of the simulated account. Nothing here can reach a real exchange or a real
API key: the backend only ever reads Kraken's unauthenticated market data.

Risk limits and cost settings are deliberately not exposed. They are the owner's
to change; a model cannot raise its own.
"""

from urllib.parse import urlencode

from mcp_servers.condor.condor_client import call_main_api

_BASE = "/sim/kraken"


async def snapshot(pair: str, candles: int, candle_minutes: int) -> dict:
    q = {"pair": pair, "candles": candles, "candle_minutes": candle_minutes}
    return await call_main_api("GET", f"{_BASE}/snapshot?{urlencode(q)}", timeout=60)


async def submit_order(body: dict) -> dict:
    return await call_main_api("POST", f"{_BASE}/orders", body, timeout=60)


async def cancel_order(order_id: str) -> dict:
    return await call_main_api("DELETE", f"{_BASE}/orders/{order_id}", timeout=60)


async def list_orders() -> dict:
    return await call_main_api("GET", f"{_BASE}/orders", timeout=60)


async def portfolio() -> dict:
    return await call_main_api("GET", f"{_BASE}/portfolio", timeout=60)


async def cost_report() -> dict:
    return await call_main_api("GET", f"{_BASE}/costs", timeout=60)


# ── Futures (perpetuals) ────────────────────────────────────────────────

_FUT = "/sim/kraken/fut"


async def fut_snapshot(symbol: str, candles: int, candle_minutes: int) -> dict:
    q = {"symbol": symbol, "candles": candles, "candle_minutes": candle_minutes}
    return await call_main_api("GET", f"{_FUT}/snapshot?{urlencode(q)}", timeout=60)


async def fut_submit_order(body: dict) -> dict:
    return await call_main_api("POST", f"{_FUT}/orders", body, timeout=60)


async def fut_cancel_order(order_id: str) -> dict:
    return await call_main_api("DELETE", f"{_FUT}/orders/{order_id}", timeout=60)


async def fut_list_orders() -> dict:
    return await call_main_api("GET", f"{_FUT}/orders", timeout=60)


async def fut_portfolio() -> dict:
    return await call_main_api("GET", f"{_FUT}/portfolio", timeout=60)


async def fut_cost_report() -> dict:
    return await call_main_api("GET", f"{_FUT}/costs", timeout=60)
