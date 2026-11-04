"""Portfolio dashboard calculations; missing observations never become zero returns."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime, timezone

DAY = 86400


def number(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def clean_points(points: list[dict], now: float) -> list[dict]:
    values = {}
    for row in points:
        stamp, equity = number(row.get("timestamp")), number(row.get("equity"))
        if (
            stamp is not None
            and 0 < stamp <= now
            and equity is not None
            and equity >= 0
        ):
            values[stamp] = equity
    result = [
        {"timestamp": stamp, "equity": equity}
        for stamp, equity in sorted(values.items())
    ]
    while result and result[0]["equity"] == 0:
        result.pop(0)
    return result


def risk_metrics(points: list[dict], now: float, *, calendar: str = "crypto") -> dict:
    """Observed drawdown plus daily Sharpe (sample deviation, zero risk-free rate).

    No annualization is applied. Equity observations from a still-open UTC day
    and gaps in a crypto daily series do not qualify as daily returns.
    """
    points = clean_points(points, now)
    peak = 0.0
    drawdown = 0.0
    for point in points:
        peak = max(peak, point["equity"])
        drawdown = max(drawdown, (peak - point["equity"]) / peak)
    closes = {}
    for point in points:
        day = int(point["timestamp"] // DAY)
        if day < int(now // DAY):
            closes[day] = point
    daily = sorted(closes.items())
    returns = []
    for (day_a, a), (day_b, b) in zip(daily, daily[1:]):
        if a["equity"] <= 0:
            continue
        gap = day_b - day_a
        # Alpaca skips non-trading days. Crypto trades every calendar day.
        if gap != 1 and not (calendar == "equities" and 1 < gap <= 4):
            continue
        if calendar == "crypto" and (
            a["timestamp"] % DAY < DAY - 3600 or b["timestamp"] % DAY < DAY - 3600
        ):
            continue
        returns.append(b["equity"] / a["equity"] - 1)
    sharpe = None
    if len(returns) >= 7:
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        if variance > 1e-18:
            sharpe = mean / math.sqrt(variance)
    return {
        "max_drawdown_pct": drawdown * 100 if len(points) >= 2 else None,
        "sharpe": sharpe,
        "daily_observations": len(returns),
    }


def outcome_metrics(outcomes: list[dict]) -> dict:
    values = [number(row.get("pnl")) for row in outcomes]
    values = [value for value in values if value is not None]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_loss = -sum(losses)
    return {
        "closed_trades": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(values) - len(wins) - len(losses),
        "batting_average": len(wins) / len(values) * 100 if values else None,
        "profit_factor": sum(wins) / gross_loss if gross_loss > 0 else None,
        "average_win": sum(wins) / len(wins) if wins else None,
        "average_loss": sum(losses) / len(losses) if losses else None,
        "gross_profit": sum(wins),
        "gross_loss": gross_loss,
    }


def alpaca_outcomes(fills: list[dict]) -> tuple[list[dict], float, bool]:
    """FIFO match long-side fills, grouping partial closing fills by sell order.

    Missing entry lots (shorts, transfers, or incomplete history) make the
    outcome population unknown rather than fabricating a zero acquisition cost.
    """
    lots = defaultdict(deque)
    closes = {}
    volume = 0.0
    seen = set()
    try:
        ordered = sorted(fills, key=lambda row: row["transaction_time"])
        for row in ordered:
            if row.get("id") in seen:
                continue
            if row.get("id"):
                seen.add(row["id"])
            qty, price = number(row.get("qty")), number(row.get("price"))
            if qty is None or price is None or qty <= 0 or price <= 0:
                return [], volume, False
            volume += qty * price
            symbol = row["symbol"]
            if row.get("side") == "buy":
                lots[symbol].append([qty, price])
                continue
            if row.get("side") != "sell":
                return [], volume, False
            pnl = 0.0
            remaining = qty
            while remaining > 1e-9:
                if not lots[symbol]:
                    return [], volume, False
                lot = lots[symbol][0]
                matched = min(remaining, lot[0])
                pnl += matched * (price - lot[1])
                remaining -= matched
                lot[0] -= matched
                if lot[0] < 1e-9:
                    lots[symbol].popleft()
            stamp = datetime.fromisoformat(
                row["transaction_time"].replace("Z", "+00:00")
            )
            order = row.get("order_id") or row["id"]
            item = closes.setdefault(
                order, {"pnl": 0.0, "timestamp": stamp.timestamp()}
            )
            item["pnl"] += pnl
            item["timestamp"] = max(item["timestamp"], stamp.timestamp())
    except (KeyError, TypeError, ValueError):
        return [], volume, False
    return list(closes.values()), volume, True
