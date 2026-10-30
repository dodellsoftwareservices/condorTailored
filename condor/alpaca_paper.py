"""Minimal async Alpaca client, scoped to paper trading.

Deliberately not the ``alpaca-py`` SDK: this needs four endpoints, and the SDK
pulls pandas and a websocket stack the routine layer does not use. aiohttp is
already a dependency here.

**This module refuses to talk to a live account.** Equities orders are
irreversible in a way a crypto paper fill is not, so the host allowlists below
fail closed rather than trusting a config value. Pointing it at
``api.alpaca.markets`` raises at construction time, not at order time. The data
host is pinned too: every request carries the key pair, and a caller-supplied
host would be somewhere to send it.

Not under ``condor/venues/``: that name is taken by ``condor/venues.py``, the
test-venue to mainnet mapping, and a package of the same name would shadow it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

PAPER_TRADING_HOST = "https://paper-api.alpaca.markets"
DATA_HOST = "https://data.alpaca.markets"

# Any host not on these lists is rejected. One entry each, on purpose.
_ALLOWED_TRADING_HOSTS = frozenset({PAPER_TRADING_HOST})
_ALLOWED_DATA_HOSTS = frozenset({DATA_HOST})

_TIMEOUT = aiohttp.ClientTimeout(total=20)


class AlpacaConfigError(RuntimeError):
    """Credentials missing, or a non-paper host was supplied."""


class AlpacaAPIError(RuntimeError):
    """Alpaca answered with a non-2xx status."""


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    market_value: float
    unrealized_pl: float


@dataclass(frozen=True)
class Account:
    equity: float
    cash: float
    buying_power: float


class AlpacaPaperClient:
    """Thin wrapper over the Alpaca paper trading and market data REST APIs."""

    def __init__(
        self,
        key_id: str | None = None,
        secret_key: str | None = None,
        trading_host: str = PAPER_TRADING_HOST,
        data_host: str = DATA_HOST,
    ) -> None:
        host = trading_host.rstrip("/")
        if host not in _ALLOWED_TRADING_HOSTS:
            raise AlpacaConfigError(
                f"Refusing to use trading host {host!r}. This client is paper-only; "
                f"the sole permitted host is {PAPER_TRADING_HOST}. If you genuinely "
                "want live equities execution, that belongs in a separate, "
                "deliberately-written module with its own risk controls."
            )
        data = data_host.rstrip("/")
        if data not in _ALLOWED_DATA_HOSTS:
            raise AlpacaConfigError(
                f"Refusing to use data host {data!r}. Requests carry the API key "
                f"pair; the sole permitted host is {DATA_HOST}."
            )
        self._trading_host = host
        self._data_host = data

        self._key = key_id or os.getenv("ALPACA_API_KEY_ID", "")
        self._secret = secret_key or os.getenv("ALPACA_API_SECRET_KEY", "")
        if not self._key or not self._secret:
            raise AlpacaConfigError(
                "Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env. "
                "Generate them at app.alpaca.markets under Paper Trading — "
                "paper keys are distinct from live keys."
            )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._key,
            "APCA-API-SECRET-KEY": self._secret,
            "accept": "application/json",
        }

    async def _request(self, method: str, host: str, path: str, **kw) -> dict | list:
        url = f"{host}{path}"
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.request(
                method, url, headers=self._headers, **kw
            ) as resp:
                body = await resp.text()
                if resp.status == 403 and host == self._data_host:
                    raise AlpacaAPIError(
                        "403 from the data API. The free Basic plan serves IEX "
                        "only; this symbol or feed needs Algo Trader Plus."
                    )
                if resp.status >= 400:
                    raise AlpacaAPIError(
                        f"{method} {path} -> {resp.status}: {body[:300]}"
                    )
                try:
                    return json.loads(body) if body else {}
                except ValueError as e:
                    raise AlpacaAPIError(
                        f"Unparseable response from {path}: {e}"
                    ) from e

    # -- account ----------------------------------------------------------

    async def get_account(self) -> Account:
        raw = await self._request("GET", self._trading_host, "/v2/account")
        return Account(
            equity=float(raw.get("equity") or 0),
            cash=float(raw.get("cash") or 0),
            buying_power=float(raw.get("buying_power") or 0),
        )

    async def get_positions(self) -> list[Position]:
        raw = await self._request("GET", self._trading_host, "/v2/positions")
        out = []
        for p in raw if isinstance(raw, list) else []:
            out.append(
                Position(
                    symbol=p.get("symbol", ""),
                    qty=float(p.get("qty") or 0),
                    market_value=float(p.get("market_value") or 0),
                    unrealized_pl=float(p.get("unrealized_pl") or 0),
                )
            )
        return out

    async def is_market_open(self) -> tuple[bool, str]:
        """(open_now, human readable next transition)."""
        raw = await self._request("GET", self._trading_host, "/v2/clock")
        is_open = bool(raw.get("is_open"))
        nxt = raw.get("next_open") if not is_open else raw.get("next_close")
        return is_open, str(nxt or "unknown")

    # -- orders -----------------------------------------------------------

    async def submit_notional_order(
        self, symbol: str, notional: float, side: str = "buy"
    ) -> dict:
        """Buy a dollar amount rather than a share count.

        Notional orders are how a sweep of an arbitrary P&L figure becomes a
        position without rounding to whole shares. Alpaca accepts these as
        market DAY orders on fractionable symbols, which VOO, VTI and SPY are.
        """
        if notional <= 0:
            raise ValueError("notional must be positive")
        payload = {
            "symbol": symbol.upper(),
            "notional": f"{notional:.2f}",
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }
        return await self._request(
            "POST", self._trading_host, "/v2/orders", json=payload
        )

    async def latest_quote(self, symbol: str) -> dict:
        return await self._request(
            "GET",
            self._data_host,
            f"/v2/stocks/{symbol.upper()}/quotes/latest",
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
