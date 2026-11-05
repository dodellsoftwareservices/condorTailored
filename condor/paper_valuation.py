"""Split-aware valuation of Alpaca's paper account.

Alpaca Paper can move a split security's price without moving its share count.
Only reconcile a position when its complete fill history proves which quantity
the broker is showing. Otherwise withhold a suspicious valuation.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from condor.alpaca_paper import Account, AlpacaPaperClient, Position
from condor.asyncutil import SingleFlight

_cache: tuple[float, str, "PaperValuation"] | None = None
_flights = SingleFlight()


@dataclass(frozen=True)
class PaperValuation:
    account: Account
    positions: list[Position]
    provider_equity: float
    adjustment: float
    notes: list[str]
    reliable: bool
    fills: list[dict]
    fills_complete: bool
    adjusted_symbols: list[str]
    adjusted_since: date | None

    @property
    def equity(self) -> float | None:
        return self.provider_equity + self.adjustment if self.reliable else None


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.0001, abs(b) * 0.00001)


def _fill_qty(rows: list[dict], symbol: str, *, before: str | None = None) -> float:
    total = 0.0
    for row in rows:
        if str(row.get("symbol") or "").upper() != symbol:
            continue
        stamp = str(row.get("transaction_time") or "")[:10]
        if before is not None and (not stamp or stamp >= before):
            continue
        qty = float(row["qty"])
        total += qty if row.get("side") == "buy" else -qty
    return total


def reconcile(
    positions: list[Position],
    fills: list[dict],
    fills_complete: bool,
    actions: list[dict] | None,
    today: date,
) -> tuple[list[Position], float, list[str], bool]:
    """Return display positions, equity correction, visible notes, reliability."""
    notes: list[str] = []
    if actions is None or not fills_complete:
        suspect = [
            p.symbol
            for p in positions
            if p.market_value - p.unrealized_pl > 0
            and abs(p.unrealized_pl / (p.market_value - p.unrealized_pl)) >= 0.45
        ]
        if suspect:
            notes.append(
                f"Valuation withheld for {', '.join(suspect)}: split announcements or complete fills are unavailable."
            )
            return positions, 0.0, notes, False
        if actions is None:
            notes.append(
                "Corporate-action feed unavailable; split checks are incomplete."
            )
        return positions, 0.0, notes, True

    adjusted: list[Position] = []
    correction = 0.0
    reliable = True
    for p in positions:
        applicable = [
            action
            for action in actions
            if str(action.get("ca_type") or "").lower() == "split"
            and str(
                action.get("target_symbol") or action.get("initiating_symbol") or ""
            ).upper()
            == p.symbol.upper()
            and str(action.get("ex_date") or "") <= today.isoformat()
        ]
        if not applicable:
            basis = p.market_value - p.unrealized_pl
            if basis > 0 and abs(p.unrealized_pl / basis) >= 0.45:
                reliable = False
                notes.append(
                    f"{p.symbol} moved at least 45% with no matching split announcement; valuation withheld pending review."
                )
            adjusted.append(p)
            continue
        if len(applicable) != 1 or not p.qty:
            reliable = False
            notes.append(f"{p.symbol} split history is ambiguous; valuation withheld.")
            adjusted.append(p)
            continue
        action = applicable[0]
        try:
            ratio = float(action["new_rate"]) / float(action["old_rate"])
            ex_date = str(action["ex_date"])
            raw_qty = _fill_qty(fills, p.symbol)
            pre_qty = _fill_qty(fills, p.symbol, before=ex_date)
            expected_qty = raw_qty + pre_qty * (ratio - 1)
            if (
                not all(
                    math.isfinite(x) for x in (ratio, raw_qty, pre_qty, expected_qty)
                )
                or ratio <= 0
            ):
                raise ValueError("invalid split ratio or quantity")
            if _close(p.qty, expected_qty):
                adjusted.append(p)  # broker has already applied it
            elif _close(p.qty, raw_qty):
                change = (expected_qty - p.qty) * p.market_value / p.qty
                correction += change
                adjusted.append(
                    Position(
                        symbol=p.symbol,
                        qty=expected_qty,
                        market_value=p.market_value + change,
                        unrealized_pl=p.unrealized_pl + change,
                    )
                )
                notes.append(
                    f"{p.symbol} {ratio:g}:1 split: Alpaca Paper still shows pre-split shares. "
                    f"Added an estimated ${change:,.2f} to displayed equity; broker cash and positions were not changed."
                )
            else:
                raise ValueError(
                    "broker quantity matches neither fill history nor split-adjusted shares"
                )
        except (
            KeyError,
            ValueError,
            TypeError,
            ZeroDivisionError,
            OverflowError,
        ) as exc:
            reliable = False
            adjusted.append(p)
            notes.append(
                f"{p.symbol} split reconciliation failed ({exc}); valuation withheld."
            )
    return adjusted, correction, notes, reliable


async def read_paper_valuation(client: AlpacaPaperClient) -> PaperValuation:
    """Share a short-lived verified snapshot across dashboard and Equities."""
    global _cache
    key = (
        hashlib.sha256(str(client._key).encode()).hexdigest()
        if getattr(client, "_key", None)
        else f"test-client-{id(client)}"
    )
    if _cache and _cache[1] == key and time.monotonic() - _cache[0] < 15:
        return _cache[2]

    async def load() -> PaperValuation:
        global _cache
        if _cache and _cache[1] == key and time.monotonic() - _cache[0] < 15:
            return _cache[2]
        account, positions, fill_result, actions = await asyncio.gather(
            client.get_account(),
            client.get_positions(),
            client.get_fill_history(),
            client.get_split_announcements(),
            return_exceptions=True,
        )
        if isinstance(account, Exception):
            raise account
        if isinstance(positions, Exception):
            raise positions
        fills, complete = (
            ([], False) if isinstance(fill_result, Exception) else fill_result
        )
        announcements = None if isinstance(actions, Exception) else actions
        now = datetime.now(ZoneInfo("America/New_York")).date()
        display, delta, notes, reliable = reconcile(
            positions, fills, complete, announcements, now
        )
        adjusted_symbols = [
            raw.symbol
            for raw, marked in zip(positions, display)
            if not _close(raw.qty, marked.qty)
        ]
        dates = [
            date.fromisoformat(str(a["ex_date"]))
            for a in announcements or []
            if str(a.get("target_symbol") or a.get("initiating_symbol") or "").upper()
            in adjusted_symbols
            and a.get("ex_date")
        ]
        result = PaperValuation(
            account=account,
            positions=display,
            provider_equity=account.equity,
            adjustment=delta,
            notes=notes,
            reliable=reliable,
            fills=fills,
            fills_complete=complete,
            adjusted_symbols=adjusted_symbols,
            adjusted_since=min(dates) if dates else None,
        )
        _cache = (time.monotonic(), key, result)
        return result

    return await _flights.run(key, load)
