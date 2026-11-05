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

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

from condor.equity_observability import (
    EquityEvent,
    EquityMeasurement,
    equity_correlation_id,
    equity_metrics,
)
from condor.fsutil import atomic_write_json
from condor.paths import data_dir

logger = logging.getLogger(__name__)

PAPER_TRADING_HOST = "https://paper-api.alpaca.markets"
DATA_HOST = "https://data.alpaca.markets"

# Any host not on these lists is rejected. One entry each, on purpose.
_ALLOWED_TRADING_HOSTS = frozenset({PAPER_TRADING_HOST})
_ALLOWED_DATA_HOSTS = frozenset({DATA_HOST})

_TIMEOUT = aiohttp.ClientTimeout(total=20)
_split_cache: tuple[float, str, list[dict]] | None = None
_SPLIT_REGISTRY = "paper_corporate_actions.json"


def _stored_splits() -> list[dict]:
    path = data_dir() / _SPLIT_REGISTRY
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text())
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("expected an array of actions")
        return rows
    except (OSError, ValueError) as exc:
        raise AlpacaAPIError(
            "Local paper corporate-action registry is unreadable"
        ) from exc


def _remember_splits(rows: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for row in [*_stored_splits(), *rows]:
        symbol = str(
            row.get("target_symbol") or row.get("initiating_symbol") or ""
        ).upper()
        ex_date = str(row.get("ex_date") or "")
        if str(row.get("ca_type") or "").lower() == "split" and symbol and ex_date:
            merged[(symbol, ex_date)] = row
    result = list(merged.values())
    try:
        atomic_write_json(data_dir() / _SPLIT_REGISTRY, result, indent=2)
    except OSError as exc:
        raise AlpacaAPIError("Could not save paper corporate-action registry") from exc
    return result


class AlpacaConfigError(RuntimeError):
    """Credentials missing, or a non-paper host was supplied."""


class AlpacaAPIError(RuntimeError):
    """Alpaca answered with a non-2xx status."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AlpacaTransportError(AlpacaAPIError):
    """No reliable response. An order may exist: reconcile before retrying."""


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
        if host not in (self._trading_host, self._data_host):
            raise AlpacaConfigError("Refusing an unapproved Alpaca request host")
        if not path.startswith("/"):
            raise AlpacaConfigError("Alpaca request paths must start with a slash")
        url = f"{host}{path}"
        # aiohttp retains custom APCA authentication headers on redirects.
        # Validate every destination by refusing redirects altogether.
        kw["allow_redirects"] = False
        started = time.monotonic()
        outcome = "abandoned"
        order = method == "POST" and host == self._trading_host and path == "/v2/orders"
        if order:
            equity_metrics.record(EquityEvent.ORDER_SUBMISSION)
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.request(
                    method, url, headers=self._headers, **kw
                ) as resp:
                    outcome = f"http_{resp.status}"
                    if not 200 <= resp.status < 300:
                        if resp.status == 429:
                            equity_metrics.record(EquityEvent.PROVIDER_RATE_LIMITED)
                        if order and 400 <= resp.status < 500:
                            equity_metrics.record(EquityEvent.ORDER_REJECTED)
                        # Never echo upstream bodies: they can include credentials
                        # or order/account details, including in malformed JSON.
                        raise AlpacaAPIError(
                            f"Alpaca request failed (HTTP {resp.status})",
                            status_code=resp.status,
                        )
                    body = await resp.text()
                    try:
                        result = json.loads(body) if body else {}
                    except ValueError:
                        outcome = "invalid_json"
                        raise AlpacaAPIError("Invalid JSON from Alpaca") from None
                    if not isinstance(result, (dict, list)):
                        outcome = "invalid_shape"
                        raise AlpacaAPIError("Unexpected response shape from Alpaca")
                    equity_metrics.record(EquityEvent.PROVIDER_OK)
                    return result
        except TimeoutError:
            outcome = "timeout"
            equity_metrics.record(EquityEvent.PROVIDER_TIMEOUT)
            raise AlpacaTransportError(
                "Alpaca request timed out; reconcile orders before retrying"
            ) from None
        except aiohttp.ClientError:
            outcome = "connection_error"
            equity_metrics.record(EquityEvent.PROVIDER_ERROR)
            raise AlpacaTransportError(
                "Alpaca connection failed; reconcile orders before retrying"
            ) from None
        except AlpacaAPIError:
            equity_metrics.record(EquityEvent.PROVIDER_ERROR)
            raise
        finally:
            elapsed = time.monotonic() - started
            equity_metrics.observe(EquityMeasurement.PROVIDER_LATENCY_SECONDS, elapsed)
            logger.info(
                "Equity provider request source=%s correlation_id=%s outcome=%s latency_seconds=%.6f",
                "alpaca_paper" if host == self._trading_host else "alpaca_data",
                equity_correlation_id.get(),
                outcome,
                elapsed,
            )

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

    async def get_portfolio_history(self, period: str = "1M") -> dict:
        if period not in {"1D", "1W", "1M", "3M"}:
            raise ValueError("Unsupported portfolio history period")
        raw = await self._request(
            "GET",
            self._trading_host,
            "/v2/account/portfolio/history",
            params={"period": period, "timeframe": "5Min" if period == "1D" else "1D"},
        )
        if not isinstance(raw, dict):
            raise AlpacaAPIError("Invalid portfolio history from Alpaca")
        return raw

    async def get_fill_history(self) -> tuple[list[dict], bool]:
        """Oldest-first fills, with explicit completeness for cost-basis matching."""
        rows: list[dict] = []
        cursor = None
        seen: set[str] = set()
        for _ in range(20):
            params = {"direction": "asc", "page_size": 100}
            if cursor:
                params["page_token"] = cursor
            page = await self._request(
                "GET", self._trading_host, "/v2/account/activities/FILL", params=params
            )
            if not isinstance(page, list):
                raise AlpacaAPIError("Invalid fill history from Alpaca")
            rows.extend(page)
            if len(page) < 100:
                return rows, True
            cursor = str(page[-1].get("id") or "")
            if not cursor or cursor in seen:
                return rows, False
            seen.add(cursor)
        return rows, False

    async def get_split_announcements(self) -> list[dict]:
        """Recent and upcoming splits from Alpaca's corporate-action feed.

        Paper accounts do not process splits reliably, so both valuation and
        pre-trade checks use this feed. Cache the calendar, not account state.
        """
        global _split_cache
        today = datetime.now(ZoneInfo("America/New_York")).date()
        key = today.isoformat()
        if (
            _split_cache
            and _split_cache[1] == key
            and time.monotonic() - _split_cache[0] < 600
        ):
            return _split_cache[2]

        async def window(since, until):
            return await self._request(
                "GET",
                self._trading_host,
                "/v2/corporate_actions/announcements",
                params={
                    "ca_types": "Split",
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                },
            )

        past, future = await asyncio.gather(
            window(today - timedelta(days=89), today),
            window(today + timedelta(days=1), today + timedelta(days=89)),
        )
        if not isinstance(past, list) or not isinstance(future, list):
            raise AlpacaAPIError("Invalid corporate-action response from Alpaca")
        remembered = _remember_splits([*past, *future])
        _split_cache = (time.monotonic(), key, remembered)
        return remembered

    async def submit_notional_order(
        self, symbol: str, notional: float, side: str = "buy"
    ) -> dict:
        """Buy a dollar amount rather than a share count.

        Notional orders are how a sweep of an arbitrary P&L figure becomes a
        position without rounding to whole shares. Alpaca accepts these as
        market DAY orders on fractionable symbols, which VOO, VTI and SPY are.
        """
        if (
            not math.isfinite(notional)
            or notional <= 0
            or float(f"{notional:.2f}") <= 0
        ):
            raise ValueError("notional must be finite and positive at cent precision")
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if side == "buy":
            # An announced split can leave paper quantity stale while the
            # market price changes. Reject before the order reaches Alpaca.
            today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
            for action in await self.get_split_announcements():
                if (
                    str(
                        action.get("target_symbol")
                        or action.get("initiating_symbol")
                        or ""
                    ).upper()
                    == symbol.upper()
                    and str(action.get("ca_type") or "").lower() == "split"
                    and str(action.get("ex_date") or "")
                    >= (datetime.fromisoformat(today) - timedelta(days=30))
                    .date()
                    .isoformat()
                ):
                    raise ValueError(
                        f"{symbol.upper()} has an announced recent/upcoming split; "
                        "paper buys are blocked until its account history is reconciled"
                    )
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
