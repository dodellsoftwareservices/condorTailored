"""Which mainnet venue's public market data stands in for a test venue.

A testnet is somewhere to *trade* on paper, not a market to *read*. The Hummingbot
API draws that line on purpose: ``services/market_data_service.py`` keeps every
connector whose name carries ``paper_trade``, ``testnet`` or ``sandbox`` out of its
ticker pool (test networks trade worthless assets at arbitrary prices), and the
candle routes only accept the connectors ``CandlesFactory`` knows, all of them
mainnet. ``POST /market-data/candles`` therefore answers a testnet name with
``Unsupported connector 'binance_perpetual_testnet'``, and ``GET /market-data/tickers``
with a 4xx saying the connector "is excluded from the ticker pool".

What a trade actually meets still answers under the test name: the order book, the
trading rules, quotes (``/market-data/prices``), balances, orders and positions. That
is the venue's own state, and it stays on the connector being traded. The *market*
(candle history, and funding, which the API will quote for a testnet but not as the
market's number: ``binance_perpetual_testnet`` answered 0.0001 while
``binance_perpetual`` read 0.00009263 at the same moment) is read from the mainnet
venue the testnet imitates: ``data_connector_for("binance_perpetual_testnet")`` is
``"binance_perpetual"``.

A stand-in is an approximation, and callers that print a price derived from it say
so. A mainnet candle close is the mainnet price; the testnet's own book can sit
somewhere else, more so on thin testnets than on Binance's.

This is a leaf: standard library only, so a routine, the main process and the MCP
subprocesses can all import it without pulling the rest of Condor in behind it.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Collection
from typing import Any

logger = logging.getLogger(__name__)

#: Mirrors ``_NON_MARKET_CONNECTOR_MARKERS`` in hummingbot-api's
#: ``services/market_data_service.py``: a connector whose name contains one of these is
#: one the API will not serve market data for. ``tests/test_venues.py`` compares the two
#: whenever the API checkout sits next to this one, so a marker added there is noticed.
TEST_VENUE_MARKERS = ("paper_trade", "testnet", "sandbox")

_SUFFIXES = tuple(f"_{marker}" for marker in TEST_VENUE_MARKERS)

#: Test venue -> the mainnet connector that stands in for it, for a name the suffix
#: rule gets wrong. Empty today: every test venue the API lists follows the rule
#: (``tests/test_venues.py`` pins all sixteen), so the first entry is the first
#: connector whose mainnet candle feed is registered under a different name.
DATA_VENUE_EXCEPTIONS: dict[str, str] = {}

#: The ``client.market_data`` readers that fetch *market* data. Each takes the
#: connector first. Everything else on the router (prices, order book, trading
#: rules, price-for-volume, tickers) reads the venue's own state and is left alone.
_MARKET_READERS = frozenset(
    {
        "get_candles",
        "get_historical_candles",
        "get_candles_last_days",
        "get_funding_info",
    }
)


def is_test_venue(connector: Any) -> bool:
    """Whether the Hummingbot API refuses market data for this connector name.

    The API's own test (a marker anywhere in the lower-cased name), so a name that is
    a test venue here is one it will not serve candles for. Anything that is not a
    string is not a venue at all.
    """
    if not isinstance(connector, str):
        return False
    lowered = connector.lower()
    return any(marker in lowered for marker in TEST_VENUE_MARKERS)


def _mainnet_name(connector: str) -> str:
    """``connector`` with its test suffix removed, or "" when the rule has no answer.

    Strips repeatedly, because a name like ``x_testnet_sandbox`` is a test venue until
    both are gone. A marker that is not a suffix (``x_testnet_v2``) leaves nothing to
    strip, which is "no answer" rather than a guess.
    """
    name = connector
    while is_test_venue(name):
        for suffix in _SUFFIXES:
            if name.lower().endswith(suffix):
                name = name[: -len(suffix)]
                break
        else:
            return ""
    return name


def data_connector_for(connector: str, available: Collection[str] | None = None) -> str:
    """The connector whose public market data stands in for ``connector``.

    A test venue maps to its mainnet counterpart (``binance_perpetual_testnet`` to
    ``binance_perpetual``, ``bybit_testnet`` to ``bybit``, ``architect_perpetual_sandbox``
    to ``architect_perpetual``). Anything else, including a ``None`` or an empty name,
    comes back unchanged, and so does a test venue the rule cannot map: the caller then
    gets the API's own refusal, naming the connector it asked about.

    Args:
        connector: The connector being traded.
        available: The connector names the candle feed supports
            (``/market-data/available-candle-connectors``). With it, a test venue
            whose counterpart is not in the feed comes back unchanged instead of
            mapped to a name the API would refuse as well, so the refusal names
            the connector that was asked about. Pass ``None``, not an empty
            collection, when the feed is unknown: an empty one means nothing is
            supported.
    """
    if not is_test_venue(connector):
        return connector

    mapped = DATA_VENUE_EXCEPTIONS.get(connector) or _mainnet_name(connector)
    # A "stand-in" that is itself a test venue would be refused all the same.
    if not mapped or is_test_venue(mapped):
        return connector
    if available is not None and mapped not in available:
        return connector
    return mapped


def _read_from_data_venue(method):
    """``method`` with its connector argument pointed at the data venue.

    Positional first, as every reader in :data:`_MARKET_READERS` takes the connector
    first; ``connector_name=`` by keyword is what the routines write. A call with no
    connector at all is passed through, so the router raises its usual TypeError.
    """

    def redirect(asked: Any) -> Any:
        data = data_connector_for(asked)
        if data != asked:
            logger.debug("%s: reading %s for %s", method.__name__, data, asked)
        return data

    @functools.wraps(method)
    async def read(*args, **kwargs):
        if args:
            args = (redirect(args[0]), *args[1:])
        elif "connector_name" in kwargs:
            kwargs["connector_name"] = redirect(kwargs["connector_name"])
        return await method(*args, **kwargs)

    return read


class _DataVenueMarketData:
    """``client.market_data`` with its market readers pointed at the data venue."""

    def __init__(self, router: Any) -> None:
        self._router = router

    def __getattr__(self, name: str) -> Any:
        if name == "_router":  # not set yet (copy, pickle): do not recurse
            raise AttributeError(name)
        attr = getattr(self._router, name)
        if name in _MARKET_READERS and callable(attr):
            return _read_from_data_venue(attr)
        return attr


class _DataVenueClient:
    """A Hummingbot API client whose ``market_data`` reads test venues from mainnet.

    Every other attribute is the real client's, so ``client.portfolio``,
    ``client.executors`` and the rest behave exactly as before.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        if name == "_client":  # not set yet (copy, pickle): do not recurse
            raise AttributeError(name)
        attr = getattr(self._client, name)
        if name == "market_data" and attr is not None:
            return _DataVenueMarketData(attr)
        return attr


def data_venue_client(client: Any) -> Any:
    """``client`` with test-venue market reads redirected to the mainnet venue.

    For code that is handed a raw client and names connectors itself, a ``run_code``
    snippet above all. Candles, historical candles and funding for a test venue are
    read from its data venue (:func:`data_connector_for`); every other call, and every
    call for a connector that is not a test venue, reaches the real client unchanged.

    ``None``, and anything without a ``market_data`` (a test double, a client that has
    not been initialised), comes back as it was.
    """
    if isinstance(client, _DataVenueClient):
        return client
    try:
        market_data = client.market_data
    except Exception:
        return client
    if market_data is None:
        return client
    return _DataVenueClient(client)
