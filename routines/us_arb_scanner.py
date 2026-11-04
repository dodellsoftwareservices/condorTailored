"""Read-only, same-instrument US-venue arbitrage research scanner."""

CATEGORY = "Arbitrage"

import asyncio
import logging
import time

from pydantic import BaseModel, Field, field_validator
from telegram.ext import ContextTypes

from condor.arbitrage import (
    BookError,
    compare_books,
    connector_instrument_class,
    normalize_book,
    split_pair,
    walk_book,
)
from condor.public_books import coinbase_product_book_url, fetch_public_book
from condor.us_venues import VENUES_BY_ID
from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)

# Research defaults, not authenticated account-tier fee discovery. Operators
# must replace them with their actual fees before using results for decisions.
DEFAULT_FEES = {
    "coinbase_advanced_trade": 60.0,
    "kraken": 40.0,
    "gemini": 40.0,
    "bitstamp": 30.0,
    "lighter": 0.0,
    "lighter_perpetual": 0.0,
}


class Config(BaseModel):
    """Compare fresh executable books for one identical crypto instrument."""

    trading_pair: str = Field(
        default="BTC-USD",
        description="Exact BASE-QUOTE pair on every venue; no USD/USDC conversion is assumed",
    )
    exchanges: str = Field(
        default="coinbase_advanced_trade,kraken,gemini,bitstamp",
        description="Mainnet spot connectors, or perpetual-only connectors in a separate scan",
    )
    amount: float = Field(
        default=0.01, gt=0, description="Base-asset quantity to walk on BOTH books"
    )
    taker_fee_bps: float = Field(
        default=-1,
        ge=-1,
        description="Per-leg fee for every venue; -1 uses conservative research defaults",
    )
    latency_buffer_bps: float = Field(
        default=10,
        ge=0,
        description="Extra cost allowance for latency and unmodeled slippage",
    )
    min_net_bps: float = Field(
        default=0,
        description="Minimum modeled net spread to include in the routes table",
    )
    max_book_age_seconds: float = Field(
        default=10, gt=0, le=120, description="Reject a book older than this"
    )
    max_book_skew_seconds: float = Field(
        default=3,
        ge=0,
        le=30,
        description="Maximum source-timestamp skew; Hummingbot timestamps are local snapshots",
    )
    ob_depth: int = Field(
        default=100, ge=2, le=100, description="Levels requested per venue"
    )

    @field_validator("trading_pair")
    @classmethod
    def normalize_pair(cls, value):
        base, quote = split_pair(value)
        return f"{base}-{quote}"


def _exchanges(raw: str) -> list[str]:
    names = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return list(dict.fromkeys(names))


def _fees(config: Config, exchanges: list[str]) -> dict[str, float]:
    if config.taker_fee_bps >= 0:
        return {name: config.taker_fee_bps for name in exchanges}
    unknown = [name for name in exchanges if name not in DEFAULT_FEES]
    if unknown:
        raise BookError(
            "No research fee default for "
            + ", ".join(unknown)
            + "; set taker_fee_bps explicitly"
        )
    return {name: DEFAULT_FEES[name] for name in exchanges}


async def _fetch_book(client, connector: str, config: Config):
    try:
        if connector_instrument_class(connector) == "simulation":
            raise BookError("paper/testnet venues cannot produce real-market signals")
        direct = VENUES_BY_ID.get(connector)
        if direct and direct.direct_public:
            payload = await fetch_public_book(
                coinbase_product_book_url(config.trading_pair, config.ob_depth)
            )
            timestamp_origin = "exchange"
        else:
            payload = await client.market_data.get_order_book(
                connector_name=connector,
                trading_pair=config.trading_pair,
                depth=config.ob_depth,
            )
            timestamp_origin = "snapshot"
        received = time.time()
        book = normalize_book(
            connector,
            config.trading_pair,
            payload,
            now=received,
            received_timestamp=received,
            max_age_seconds=config.max_book_age_seconds,
            timestamp_origin=timestamp_origin,
        )
        return book, None
    except Exception as exc:  # each failed venue is shown; no fake empty book
        logger.info("US arb book rejected for %s: %s", connector, exc)
        return None, f"{type(exc).__name__}: {exc}"[:500]


async def scan(client, config: Config) -> dict:
    """Collect and compare one snapshot set; performs no authenticated writes."""
    exchanges = _exchanges(config.exchanges)
    if len(exchanges) < 2:
        raise BookError("select at least two distinct venues")
    classes = {connector_instrument_class(name) for name in exchanges}
    if "simulation" in classes:
        raise BookError("paper/testnet venues cannot produce real-market signals")
    if len(classes) != 1:
        raise BookError("do not mix spot and perpetual venues in one scan")
    fees = _fees(config, exchanges)
    results = await asyncio.gather(
        *(_fetch_book(client, connector, config) for connector in exchanges)
    )
    books, rejected = [], []
    for connector, (book, error) in zip(exchanges, results):
        if book:
            books.append(book)
        else:
            rejected.append((connector, error or "unknown book error"))
    routes = compare_books(
        books,
        quantity=config.amount,
        fee_bps=fees,
        buffer_bps=config.latency_buffer_bps,
        max_skew_seconds=config.max_book_skew_seconds,
        min_net_bps=config.min_net_bps,
    )
    return {"books": books, "rejected": rejected, "routes": routes, "fees": fees}


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> RoutineResult:
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None
    client = await get_client(chat_id, context=context)
    if not client:
        return RoutineResult(
            text="No server available. Configure servers in /config.", failed=True
        )
    try:
        result = await scan(client, config)
    except (BookError, ValueError) as exc:
        return RoutineResult(text=f"Scan refused: {exc}", failed=True)

    books, rejected, routes = result["books"], result["rejected"], result["routes"]
    if len(books) < 2:
        detail = "\n".join(f"- {name}: {error}" for name, error in rejected)
        return RoutineResult(
            text=(
                f"Fewer than two fresh, valid {config.trading_pair} books; no comparison.\n"
                f"{detail}"
            ),
            failed=True,
        )

    best = routes[0] if routes else None
    positive = [route for route in routes if route.modeled_net_quote > 0]
    lines = [
        f"US same-instrument scan: {config.trading_pair} ({books[0].instrument_class})",
        f"Size: {config.amount:g} {books[0].base} | Valid venues: {len(books)}",
        "Modeled only — not a fill guarantee or settled-profit estimate.",
        "",
        "Venue snapshots (VWAP for requested size):",
    ]
    for book in books:
        buy = walk_book(book.asks, config.amount)
        sell = walk_book(book.bids, config.amount)
        lines.append(
            f"- {book.connector}: buy {buy.average_price:.8g} / "
            f"sell {sell.average_price:.8g} {book.quote}; "
            f"fee assumption {result['fees'][book.connector]:g} bps/leg; "
            f"{book.timestamp_origin} age "
            f"{book.received_timestamp - book.source_timestamp:.3f}s"
        )
    if rejected:
        lines.extend(["", "Excluded venues:"])
        lines.extend(f"- {name}: {error}" for name, error in rejected)
    if best:
        lines.extend(
            [
                "",
                f"Best modeled route: BUY {best.buy_connector} → SELL {best.sell_connector}",
                f"Gross {best.gross_bps:+.2f} bps; fees {best.fees_bps:.2f} bps; "
                f"buffer {best.buffer_bps:.2f} bps",
                f"Modeled net {best.modeled_net_bps:+.2f} bps / "
                f"{best.modeled_net_quote:+.4f} {books[0].quote}",
                f"Source timestamp skew: {best.book_skew_seconds:.3f}s",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "No route met the configured modeled-net threshold. Lower the "
                "threshold to inspect negative routes, or check excluded venues/depth. ",
            ]
        )
    lines.extend(
        [
            "",
            f"Routes clearing threshold: {len(routes)}; positive modeled net: {len(positive)}.",
            "Hummingbot timestamps are local snapshot times; only direct Coinbase "
            "supplies a venue timestamp. Skew filtering is not full clock synchronization.",
            "Not included: withdrawal/transfer costs, inventory settlement, funding/borrow, "
            "partial fills, order rejection, taxes, and account-specific fee verification.",
        ]
    )

    rows = [
        {
            "Buy venue": route.buy_connector,
            "Sell venue": route.sell_connector,
            "Buy VWAP": route.buy_price,
            "Sell VWAP": route.sell_price,
            "Gross bps": round(route.gross_bps, 4),
            "Fees bps": route.fees_bps,
            "Buffer bps": route.buffer_bps,
            "Modeled net bps": round(route.modeled_net_bps, 4),
            f"Modeled net {books[0].quote}": round(route.modeled_net_quote, 6),
            "Book skew s": round(route.book_skew_seconds, 4),
        }
        for route in routes
    ]
    return RoutineResult(
        text="\n".join(lines),
        table_data=rows or None,
        table_columns=list(rows[0]) if rows else None,
        failed=False,
    )
