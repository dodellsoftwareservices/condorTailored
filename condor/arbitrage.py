"""Deterministic, read-only cross-venue order-book comparisons.

No AI estimates, last-trade prices, testnet prices, or cross-instrument spread
labels. Every candidate walks the asks on the buy venue and bids on the sell
venue for the same requested base quantity. The result is a *modeled* spread,
not a promise of a fill or withdrawable/settled profit.

Stdlib-only by design: routines, scripts and tests can share the normalization
and arithmetic without importing Telegram, the web app, or a connector SDK.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from typing import Any, Iterable


class BookError(ValueError):
    """A book cannot safely contribute to an opportunity calculation."""


@dataclass(frozen=True)
class Level:
    price: float
    quantity: float


@dataclass(frozen=True)
class Book:
    connector: str
    pair: str
    base: str
    quote: str
    instrument_class: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    source_timestamp: float
    received_timestamp: float
    timestamp_origin: str

    @property
    def mid(self) -> float:
        return (self.bids[0].price + self.asks[0].price) / 2


@dataclass(frozen=True)
class Fill:
    average_price: float
    gross_quote: float
    quantity: float
    levels_used: int


@dataclass(frozen=True)
class Opportunity:
    buy_connector: str
    sell_connector: str
    quantity: float
    buy_price: float
    sell_price: float
    gross_bps: float
    fees_bps: float
    buffer_bps: float
    modeled_net_bps: float
    modeled_net_quote: float
    book_skew_seconds: float


def split_pair(pair: str) -> tuple[str, str]:
    if not isinstance(pair, str) or pair.count("-") != 1:
        raise BookError("trading pair must be BASE-QUOTE")
    base, quote = (part.strip().upper() for part in pair.split("-"))
    if not base or not quote or base == quote:
        raise BookError("trading pair must contain two different assets")
    if any(not part.replace(".", "").isalnum() for part in (base, quote)):
        raise BookError("trading pair contains unsupported characters")
    return base, quote


def connector_instrument_class(connector: str) -> str:
    lowered = connector.lower()
    if any(marker in lowered for marker in ("testnet", "paper_trade", "sandbox")):
        return "simulation"
    if "perpetual" in lowered:
        return "perpetual"
    return "spot"


def _positive(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise BookError(f"{label} is not numeric") from None
    if not math.isfinite(result) or result <= 0:
        raise BookError(f"{label} must be finite and positive")
    return result


def parse_timestamp(value: Any, *, now: float) -> float:
    if value is None:
        raise BookError("book has no exchange timestamp")
    try:
        if isinstance(value, str) and not value.replace(".", "").isdigit():
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        else:
            timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        raise BookError("book timestamp is invalid") from None
    if not math.isfinite(timestamp):
        raise BookError("book timestamp is invalid")
    if timestamp > 10_000_000_000:  # milliseconds
        timestamp /= 1000
    if timestamp > now + 5:
        raise BookError("book timestamp is in the future")
    return timestamp


def _level(raw: Any) -> Level:
    if isinstance(raw, dict):
        price = raw.get("price", raw.get("Price"))
        quantity = raw.get(
            "quantity",
            raw.get("size", raw.get("amount", raw.get("Quantity"))),
        )
    elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
        price, quantity = raw[:2]
    else:
        raise BookError("book level has an unsupported shape")
    return Level(_positive(price, "level price"), _positive(quantity, "level quantity"))


def _levels(raw: Any, side: str, *, descending: bool) -> tuple[Level, ...]:
    if not isinstance(raw, list) or not raw:
        raise BookError(f"book has no {side}")
    levels = tuple(_level(item) for item in raw)
    prices = [level.price for level in levels]
    ordered = sorted(prices, reverse=descending)
    if prices != ordered:
        raise BookError(f"{side} are not sorted best-first")
    if len(set(prices)) != len(prices):
        raise BookError(f"{side} contain duplicate price levels")
    return levels


def normalize_book(
    connector: str,
    pair: str,
    payload: Any,
    *,
    now: float,
    received_timestamp: float | None = None,
    max_age_seconds: float = 30,
    timestamp_origin: str = "snapshot",
) -> Book:
    """Validate connector data into a timestamped, comparable book."""
    if not isinstance(payload, dict):
        raise BookError("book response is not an object")
    base, quote = split_pair(pair)
    instrument_class = connector_instrument_class(connector)
    if instrument_class == "simulation":
        raise BookError("simulation venues cannot produce real-market signals")
    if timestamp_origin not in ("snapshot", "exchange"):
        raise BookError("unknown book timestamp origin")
    source_timestamp = parse_timestamp(
        payload.get("timestamp", payload.get("time")), now=now
    )
    age = now - source_timestamp
    if age > max_age_seconds:
        raise BookError(f"book is stale ({age:.1f}s old)")
    received = now if received_timestamp is None else received_timestamp
    if received < source_timestamp - 5:
        raise BookError("book receive time precedes its source timestamp")
    bids = _levels(payload.get("bids"), "bids", descending=True)
    asks = _levels(payload.get("asks"), "asks", descending=False)
    if bids[0].price >= asks[0].price:
        raise BookError("book is crossed or locked")
    return Book(
        connector=connector,
        pair=f"{base}-{quote}",
        base=base,
        quote=quote,
        instrument_class=instrument_class,
        bids=bids,
        asks=asks,
        source_timestamp=source_timestamp,
        received_timestamp=received,
        timestamp_origin=timestamp_origin,
    )


def walk_book(levels: Iterable[Level], quantity: float) -> Fill:
    """Volume-weighted fill for a fixed base quantity; never extrapolate depth."""
    remaining = _positive(quantity, "requested quantity")
    requested = remaining
    gross = 0.0
    used = 0
    for level in levels:
        take = min(remaining, level.quantity)
        gross += take * level.price
        remaining -= take
        used += 1
        if remaining <= 1e-12:
            return Fill(gross / requested, gross, requested, used)
    raise BookError(f"insufficient displayed depth for {requested:g} base units")


def compare_books(
    books: Iterable[Book],
    *,
    quantity: float,
    fee_bps: dict[str, float],
    buffer_bps: float = 0,
    max_skew_seconds: float = 5,
    min_net_bps: float = 0,
) -> list[Opportunity]:
    """Return fee/buffer-adjusted routes, best modeled net first.

    Fee values are explicit per connector, not inferred from a venue brand or
    account tier. Transfer/withdrawal costs and fill uncertainty are excluded;
    ``buffer_bps`` is the caller's latency/slippage allowance.
    """
    quantity = _positive(quantity, "requested quantity")
    buffer_bps = _nonnegative(buffer_bps, "latency buffer")
    max_skew_seconds = _nonnegative(max_skew_seconds, "maximum book skew")
    candidates = list(books)
    opportunities: list[Opportunity] = []
    for first, second in combinations(candidates, 2):
        if (
            first.pair != second.pair
            or first.instrument_class != second.instrument_class
        ):
            raise BookError(
                "only identical pairs and instrument classes can be compared"
            )
        for buy, sell in ((first, second), (second, first)):
            skew = abs(buy.source_timestamp - sell.source_timestamp)
            if skew > max_skew_seconds:
                continue
            buy_fee = _fee(fee_bps, buy.connector)
            sell_fee = _fee(fee_bps, sell.connector)
            buy_fill = walk_book(buy.asks, quantity)
            sell_fill = walk_book(sell.bids, quantity)
            gross = sell_fill.gross_quote - buy_fill.gross_quote
            gross_bps = gross / buy_fill.gross_quote * 10_000
            fees_quote = (
                buy_fill.gross_quote * buy_fee / 10_000
                + sell_fill.gross_quote * sell_fee / 10_000
            )
            buffer_quote = buy_fill.gross_quote * buffer_bps / 10_000
            net_quote = gross - fees_quote - buffer_quote
            net_bps = net_quote / buy_fill.gross_quote * 10_000
            if net_bps < min_net_bps:
                continue
            opportunities.append(
                Opportunity(
                    buy_connector=buy.connector,
                    sell_connector=sell.connector,
                    quantity=quantity,
                    buy_price=buy_fill.average_price,
                    sell_price=sell_fill.average_price,
                    gross_bps=gross_bps,
                    fees_bps=buy_fee + sell_fee,
                    buffer_bps=buffer_bps,
                    modeled_net_bps=net_bps,
                    modeled_net_quote=net_quote,
                    book_skew_seconds=skew,
                )
            )
    return sorted(opportunities, key=lambda item: item.modeled_net_bps, reverse=True)


def _nonnegative(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise BookError(f"{label} is not numeric") from None
    if not math.isfinite(result) or result < 0:
        raise BookError(f"{label} must be finite and nonnegative")
    return result


def _fee(fees: dict[str, float], connector: str) -> float:
    if connector not in fees:
        raise BookError(f"no explicit taker fee configured for {connector}")
    return _nonnegative(fees[connector], f"taker fee for {connector}")
