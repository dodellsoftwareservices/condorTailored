"""Intra-venue triangular arbitrage scanner with depth-aware cycle pricing."""

CATEGORY = "Arbitrage"

import asyncio
import logging
from itertools import permutations

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from condor.venues import data_connector_for, is_test_venue
from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)

# Pricing every discovered cycle would hammer the venue; cap concurrency and
# cycle count so a scan stays inside a routine's runtime budget.
MAX_CONCURRENT = 8
HARD_CYCLE_CAP = 400


class Config(BaseModel):
    """Find profitable A->B->C->A cycles on a single exchange, net of fees."""

    connector: str = Field(
        default="kraken",
        description="Connector to scan (single venue; triangular arb is intra-venue). "
        "A paper or testnet name is read from its mainnet venue: the API serves "
        "no ticker feed for those.",
    )
    base_currency: str = Field(
        default="USD",
        description="Currency the cycle starts and ends in (USD, USDT, BTC...)",
    )
    notional: float = Field(
        default=1000.0, description="Cycle size, in base_currency units"
    )
    taker_fee_bps: float = Field(
        default=26.0,
        description="Taker fee per leg, in basis points (Kraken taker ~26bps)",
    )
    min_profit_bps: float = Field(
        default=5.0, description="Only report cycles clearing this net profit (bps)"
    )
    max_cycles: int = Field(
        default=120,
        description="Max cycles to price, most liquid first (24h quote volume of "
        "the legs touching base_currency)",
    )
    show_n: int = Field(default=20, description="Rows to show in the report")


class MarketsUnavailable(RuntimeError):
    """The venue's ticker feed could not be read."""


# ---------------------------------------------------------------------------
# Pair discovery
# ---------------------------------------------------------------------------


def _split_pair(symbol: str) -> tuple[str, str] | None:
    """Split a Hummingbot trading pair ("ETH-USD") into (base, quote)."""
    if not isinstance(symbol, str) or "-" not in symbol:
        return None
    base, _, quote = symbol.partition("-")
    base, quote = base.strip().upper(), quote.strip().upper()
    if not base or not quote or base == quote:
        return None
    return base, quote


def _positive_float(raw: object) -> float | None:
    try:
        value = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and value > 0 else None


async def _discover_pairs(client, connector: str) -> dict[tuple[str, str], dict]:
    """Map (base, quote) -> {"pair": str, "price": float|None, "volume": float|None}.

    hummingbot-api answers tickers nested by connector:
    ``{"tickers": {<connector>: {<pair>: {"price": ...}}}, "updated_at": {...}}``.
    Older servers flatten one or both levels, so unwrap defensively rather than
    indexing a shape that varies by deployment.

    Only the orientation the venue lists is stored; the cycle pricer derives
    which side to hit from the direction it needs. ``volume`` is the 24h quote
    volume where the venue reports one, the liquidity proxy cycles are ranked by.

    Raises:
        MarketsUnavailable: the feed was refused or unreadable. Returning an
            empty map here would make that look like a venue with no markets.
    """
    try:
        result = await client.market_data.get_tickers(connector)
    except Exception as e:
        raise MarketsUnavailable(str(e)) from e

    if not isinstance(result, dict):
        raise MarketsUnavailable(
            f"unexpected ticker response ({type(result).__name__})"
        )

    raw = result.get("tickers", result)
    if isinstance(raw, dict) and connector in raw and isinstance(raw[connector], dict):
        raw = raw[connector]

    # Normalise to an iterable of (symbol, fields).
    items: list[tuple[str, object]] = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        for row in raw:
            if isinstance(row, dict):
                sym = row.get("trading_pair") or row.get("symbol") or row.get("pair")
                if sym:
                    items.append((sym, row))

    out: dict[tuple[str, str], dict] = {}
    for symbol, fields in items:
        parts = _split_pair(symbol)
        if not parts:
            continue
        volume = None
        if isinstance(fields, dict):
            raw_price = (
                fields.get("price")
                or fields.get("last_price")
                or fields.get("close")
                or fields.get("last")
            )
            volume = _positive_float(fields.get("quote_volume"))
        else:
            raw_price = fields
        out[parts] = {
            "pair": symbol,
            "price": _positive_float(raw_price),
            "volume": volume,
        }
    return out


def _liquidity(pairs: dict[tuple[str, str], dict], base: str, ccy: str) -> float:
    """24h quote volume of the market joining ``ccy`` to ``base``; 0 when unknown."""
    for key in ((ccy, base), (base, ccy)):
        entry = pairs.get(key)
        if entry and entry.get("volume"):
            return entry["volume"]
    return 0.0


def _find_cycles(
    pairs: dict[tuple[str, str], dict], base: str, cap: int
) -> list[tuple[str, str, str]]:
    """Every base -> X -> Y -> base triangle the venue's markets can express.

    Most liquid first, scored by the thinner of the two legs that touch ``base``:
    a cycle is only as deep as its shallowest market. Ties (and venues that report
    no volume) keep alphabetical order, so the result is deterministic. Cutting
    the list at ``cap`` without ranking would price whichever tickers sort first.
    """
    # Currencies reachable from base in one hop, either orientation.
    neighbours: set[str] = set()
    for b, q in pairs:
        if b == base:
            neighbours.add(q)
        elif q == base:
            neighbours.add(b)

    def connected(a: str, b: str) -> bool:
        return (a, b) in pairs or (b, a) in pairs

    liquidity = {n: _liquidity(pairs, base, n) for n in neighbours}
    scored: list[tuple[float, tuple[str, str, str]]] = []
    for x, y in permutations(sorted(neighbours), 2):
        if connected(x, y):
            scored.append((min(liquidity[x], liquidity[y]), (base, x, y)))

    scored.sort(key=lambda s: s[0], reverse=True)  # stable: ties stay alphabetical
    return [cycle for _, cycle in scored[: min(cap, HARD_CYCLE_CAP)]]


# ---------------------------------------------------------------------------
# Depth-aware conversion
# ---------------------------------------------------------------------------


async def _convert(
    client,
    connector: str,
    pairs: dict[tuple[str, str], dict],
    from_ccy: str,
    to_ccy: str,
    amount_from: float,
    fee_rate: float,
) -> float | None:
    """Convert amount_from of from_ccy into to_ccy across the real book.

    Two orientations exist and they are not symmetric:

    - the venue lists FROM-TO: we SELL base FROM, volume is already known
    - the venue lists TO-FROM: we BUY base TO, and the volume the depth query
      wants is denominated in TO, which is the thing we are solving for. The
      last price seeds an estimate; the fill price then prices that estimate.
      A thin book makes that seed optimistic, which is noted in the report.
    """
    sell_side = pairs.get((from_ccy, to_ccy))
    buy_side = pairs.get((to_ccy, from_ccy))

    if sell_side:
        pair, volume, is_buy = sell_side["pair"], amount_from, False
    elif buy_side:
        seed = buy_side.get("price")
        if not seed or seed <= 0:
            return None
        pair, volume, is_buy = buy_side["pair"], amount_from / seed, True
    else:
        return None

    if volume <= 0:
        return None

    try:
        result = await client.market_data.get_price_for_volume(
            connector_name=connector,
            trading_pair=pair,
            volume=volume,
            is_buy=is_buy,
        )
    except Exception as e:
        logger.debug("fill price failed %s/%s: %s", connector, pair, e)
        return None

    if not isinstance(result, dict):
        return None
    raw = (
        result.get("result_price") or result.get("price") or result.get("average_price")
    )
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None

    gross = amount_from * price if not is_buy else amount_from / price
    return gross * (1.0 - fee_rate)


async def _price_cycle(
    client,
    connector: str,
    pairs: dict[tuple[str, str], dict],
    cycle: tuple[str, str, str],
    notional: float,
    fee_rate: float,
) -> dict | None:
    a, b, c = cycle
    leg1 = await _convert(client, connector, pairs, a, b, notional, fee_rate)
    if not leg1:
        return None
    leg2 = await _convert(client, connector, pairs, b, c, leg1, fee_rate)
    if not leg2:
        return None
    leg3 = await _convert(client, connector, pairs, c, a, leg2, fee_rate)
    if not leg3:
        return None

    profit = leg3 - notional
    return {
        "cycle": f"{a} -> {b} -> {c} -> {a}",
        "start": notional,
        "end": leg3,
        "profit": profit,
        "profit_bps": (profit / notional) * 10_000 if notional else 0.0,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run(
    config: Config, context: ContextTypes.DEFAULT_TYPE
) -> str | RoutineResult:
    """Scan one venue for fee-adjusted triangular arbitrage cycles."""
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None
    client = await get_client(chat_id, context=context)
    if not client:
        return RoutineResult(
            text="No server available. Configure servers in /config.", failed=True
        )

    requested = config.connector.strip()
    # The API serves no ticker feed for paper/testnet names (their prices are not
    # a market). A scan reads books and places nothing, so the mainnet venue the
    # name imitates is the right thing to read.
    connector = data_connector_for(requested)
    base = config.base_currency.strip().upper()
    fee_rate = config.taker_fee_bps / 10_000.0

    try:
        pairs = await _discover_pairs(client, connector)
    except MarketsUnavailable as e:
        hint = ""
        if is_test_venue(connector):
            hint = (
                "\n\n`" + connector + "` is a paper/testnet name with no mainnet "
                "venue to read instead; scan the mainnet connector."
            )
        return RoutineResult(
            text=f"Could not read markets for `{connector}`: {e}{hint}", failed=True
        )
    if not pairs:
        return f"`{connector}` returned no markets."

    cycles = _find_cycles(pairs, base, config.max_cycles)
    if not cycles:
        return (
            f"No triangles through {base} on `{connector}` "
            f"({len(pairs)} markets seen). Try a different base_currency."
        )

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def bounded(cyc):
        async with sem:
            return await _price_cycle(
                client, connector, pairs, cyc, config.notional, fee_rate
            )

    priced = await asyncio.gather(*[bounded(c) for c in cycles], return_exceptions=True)
    failures = [r for r in priced if isinstance(r, Exception)]
    if failures:
        logger.warning(
            "%d of %d cycles raised while pricing on %s; first: %r",
            len(failures),
            len(cycles),
            connector,
            failures[0],
        )
    results = [r for r in priced if isinstance(r, dict) and r["profit_bps"] is not None]
    results.sort(key=lambda r: r["profit_bps"], reverse=True)

    winners = [r for r in results if r["profit_bps"] >= config.min_profit_bps]

    # ---- report ----------------------------------------------------------
    from condor.reports import ReportBuilder

    builder = ReportBuilder(f"Triangular Arb: {base} on {connector}")
    builder.source("routine", "triangular_arb").tags(
        ["arbitrage", "triangular", connector, base]
    )
    builder.manual_order()

    builder.kpi("Markets", str(len(pairs)))
    builder.kpi("Cycles priced", str(len(results)))
    builder.kpi("Above threshold", str(len(winners)))
    builder.kpi("Fee/leg", f"{config.taker_fee_bps:.1f} bps")
    if results:
        builder.kpi("Best", f"{results[0]['profit_bps']:+.1f} bps")

    if connector != requested:
        builder.markdown(
            f"`{requested}` serves no market data, so this scan read `{connector}`, "
            "the mainnet venue it imitates."
        )

    builder.markdown(
        f"## Cycles\n\n"
        f"Start size {config.notional:,.2f} {base}, "
        f"three taker legs at {config.taker_fee_bps:.1f} bps each "
        f"({config.taker_fee_bps * 3:.1f} bps round trip). "
        "Prices are walked through real book depth, not top-of-book."
    )

    rows = [
        {
            "Cycle": r["cycle"],
            "End": f"{r['end']:,.2f}",
            "P&L": f"{r['profit']:+,.2f}",
            "bps": f"{r['profit_bps']:+.1f}",
        }
        for r in results[: config.show_n]
    ]
    if rows:
        builder.table(rows)
    else:
        builder.markdown("_No cycle could be priced end to end._")

    builder.markdown(
        "### Reading this\n\n"
        "A positive number here is **not** a filled trade. Three taker legs "
        "settle sequentially, so the book moves underneath you between legs, "
        "and a fill competes with everyone else hitting the same quotes. This "
        "routine only reads books; it places no orders. Treat anything under "
        "roughly 20 bps as noise."
    )
    await builder.save()

    if not results:
        return (
            f"Priced 0 of {len(cycles)} cycles on {connector}. Check the markets exist."
        )
    head = results[0]
    if winners:
        return (
            f"{len(winners)} cycle(s) above {config.min_profit_bps:.0f} bps. "
            f"Best: {head['cycle']} at {head['profit_bps']:+.1f} bps."
        )
    return (
        f"No cycle cleared {config.min_profit_bps:.0f} bps. "
        f"Best was {head['cycle']} at {head['profit_bps']:+.1f} bps "
        f"(fees cost {config.taker_fee_bps * 3:.0f} bps round trip)."
    )
