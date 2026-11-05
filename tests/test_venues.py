"""Test venues read their market from the mainnet venue they imitate.

The Hummingbot API refuses test networks for market data on purpose: its candle
routes only know mainnet connectors (``Unsupported connector
'binance_perpetual_testnet'``) and its ticker pool leaves every ``paper_trade`` /
``testnet`` / ``sandbox`` name out. The order book, trading rules, prices, balances
and orders still answer under the test name, so a testnet loop trades on the test
connector and reads its candles and funding from the mainnet one.

``condor/venues.py`` holds the rule; the rest of this file pins every call site that
applies it, and that none of them changes what a non-test connector does. The fakes
here answer like the API does: market data for mainnet names only.

Sync tests driving coroutines with ``asyncio.run``: ``pytest-asyncio`` is a dev
dependency but is not installed in this venv.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import condor.web.routes.market as market
from condor import code_runner, venues
from condor.code_runs import CodeRunStore
from condor.venues import data_connector_for, data_venue_client, is_test_venue
from condor.web.models import WebUser
from mcp_servers.hummingbot_api.tools.market_data import get_market_data

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"

#: Every connector ``GET /connectors/`` listed on the API this was written against.
LIVE_CONNECTORS = tuple("""
    aevo_perpetual aevo_perpetual_testnet architect_perpetual
    architect_perpetual_sandbox backpack backpack_perpetual binance
    binance_perpetual binance_perpetual_testnet bing_x bitget bitget_perpetual
    bitmart bitmart_perpetual bitrue bitstamp btc_markets bybit bybit_perpetual
    bybit_perpetual_testnet bybit_testnet coinbase_advanced_trade decibel_perpetual
    decibel_perpetual_testnet derive derive_perpetual derive_perpetual_testnet
    derive_testnet dexalot dexalot_testnet dydx_v4_perpetual evedex_perpetual
    foxbit gate_io gate_io_perpetual gemini grvt_perpetual grvt_perpetual_testnet
    htx hyperliquid hyperliquid_perpetual hyperliquid_perpetual_testnet
    hyperliquid_testnet injective_v2 injective_v2_perpetual kalshi_perpetual kraken
    kucoin kucoin_hft kucoin_perpetual lambdaplex lighter lighter_perpetual
    lighter_perpetual_testnet lighter_testnet mexc ndax ndax_testnet okx
    okx_perpetual pacifica_perpetual pacifica_perpetual_testnet xrpl
    """.split())

#: ``GET /market-data/available-candle-connectors`` on the same API.
LIVE_CANDLE_FEED = tuple("""
    aevo_perpetual backpack backpack_perpetual binance binance_perpetual bitget
    bitget_perpetual bitmart_perpetual btc_markets bybit bybit_perpetual
    decibel_perpetual dexalot evedex_perpetual gate_io gate_io_perpetual
    grvt_perpetual hyperliquid hyperliquid_perpetual kraken kucoin kucoin_perpetual
    lighter lighter_perpetual mexc mexc_perpetual okx okx_perpetual
    pacifica_perpetual
    """.split())

#: The sixteen names among them the API will not serve market data for, and the
#: mainnet connector each one imitates.
TEST_VENUES = {
    "aevo_perpetual_testnet": "aevo_perpetual",
    "architect_perpetual_sandbox": "architect_perpetual",
    "binance_perpetual_testnet": "binance_perpetual",
    "bybit_perpetual_testnet": "bybit_perpetual",
    "bybit_testnet": "bybit",
    "decibel_perpetual_testnet": "decibel_perpetual",
    "derive_perpetual_testnet": "derive_perpetual",
    "derive_testnet": "derive",
    "dexalot_testnet": "dexalot",
    "grvt_perpetual_testnet": "grvt_perpetual",
    "hyperliquid_perpetual_testnet": "hyperliquid_perpetual",
    "hyperliquid_testnet": "hyperliquid",
    "lighter_perpetual_testnet": "lighter_perpetual",
    "lighter_testnet": "lighter",
    "ndax_testnet": "ndax",
    "pacifica_perpetual_testnet": "pacifica_perpetual",
}

#: Test venues whose mainnet counterpart the candle feed above does not serve.
NO_FEED = {
    "architect_perpetual_sandbox",
    "derive_perpetual_testnet",
    "derive_testnet",
    "ndax_testnet",
}

MAINNET = [c for c in LIVE_CONNECTORS if c not in TEST_VENUES]


# ── the rule ──


def test_the_fixture_lists_agree_with_each_other():
    """The sixteen are exactly the names carrying a marker, so the table below is
    a statement about the live list and not a hand-picked sample of it."""
    assert {c for c in LIVE_CONNECTORS if is_test_venue(c)} == set(TEST_VENUES)
    assert len(TEST_VENUES) == 16 and len(LIVE_CONNECTORS) == 63
    assert set(TEST_VENUES) <= set(LIVE_CONNECTORS)


@pytest.mark.parametrize("testnet,mainnet", sorted(TEST_VENUES.items()))
def test_every_test_venue_the_api_lists_maps_to_its_mainnet_counterpart(
    testnet, mainnet
):
    assert is_test_venue(testnet)
    assert data_connector_for(testnet) == mainnet
    # The stand-in is a connector that exists, not a name the rule made up.
    assert mainnet in LIVE_CONNECTORS
    assert not is_test_venue(mainnet)


@pytest.mark.parametrize("connector", MAINNET)
def test_every_other_connector_comes_back_unchanged(connector):
    """Stock behaviour for a non-test connector is untouched, spot or perp, CEX or DEX."""
    assert not is_test_venue(connector)
    assert data_connector_for(connector) == connector
    assert data_connector_for(connector, available=LIVE_CANDLE_FEED) == connector
    assert data_connector_for(connector, available=[]) == connector


@pytest.mark.parametrize("testnet,mainnet", sorted(TEST_VENUES.items()))
def test_the_candle_feed_decides_whether_the_stand_in_is_used(testnet, mainnet):
    """A counterpart the feed does not serve would be refused all the same, so the
    caller gets the name it asked about back, and the refusal names that."""
    expected = testnet if testnet in NO_FEED else mainnet
    assert data_connector_for(testnet, available=LIVE_CANDLE_FEED) == expected


def test_no_test_venue_is_served_by_the_feed_itself():
    """The reason a stand-in is needed at all, stated against the live feed."""
    assert not set(TEST_VENUES) & set(LIVE_CANDLE_FEED)
    assert {m for t, m in TEST_VENUES.items() if m not in LIVE_CANDLE_FEED} == {
        TEST_VENUES[t] for t in NO_FEED
    }


@pytest.mark.parametrize(
    "available",
    [
        list(LIVE_CANDLE_FEED),
        tuple(LIVE_CANDLE_FEED),
        set(LIVE_CANDLE_FEED),
        frozenset(LIVE_CANDLE_FEED),
        dict.fromkeys(LIVE_CANDLE_FEED, True),
        dict.fromkeys(LIVE_CANDLE_FEED, True).keys(),
    ],
)
def test_available_is_any_collection_of_names(available):
    assert data_connector_for("bybit_testnet", available) == "bybit"
    assert data_connector_for("ndax_testnet", available) == "ndax_testnet"


def test_an_empty_feed_means_nothing_is_supported_and_none_means_unknown():
    assert data_connector_for("binance_perpetual_testnet", []) == (
        "binance_perpetual_testnet"
    )
    assert data_connector_for("binance_perpetual_testnet", None) == "binance_perpetual"


@pytest.mark.parametrize("junk", [None, "", 0, 3.5, [], ("binance",)])
def test_something_that_is_not_a_connector_name_is_left_alone(junk):
    assert not is_test_venue(junk)
    assert data_connector_for(junk) == junk
    assert data_connector_for(junk, available=LIVE_CANDLE_FEED) == junk


@pytest.mark.parametrize(
    "name,mainnet",
    [
        # The three markers the API checks, as a suffix.
        ("binance_paper_trade", "binance"),
        ("kucoin_perpetual_testnet", "kucoin_perpetual"),
        ("some_exchange_sandbox", "some_exchange"),
        # The API lower-cases before it looks, so does the rule; the base keeps its case.
        ("Binance_Perpetual_TESTNET", "Binance_Perpetual"),
        # Stacked: a test venue until every marker is gone.
        ("binance_perpetual_testnet_sandbox", "binance_perpetual"),
        ("binance_paper_trade_testnet", "binance"),
    ],
)
def test_the_suffix_rule_beyond_the_names_the_api_lists_today(name, mainnet):
    assert is_test_venue(name)
    assert data_connector_for(name) == mainnet


@pytest.mark.parametrize(
    "name",
    [
        "binance_testnet_v2",  # a marker that is not a suffix: nothing to strip
        "_testnet",  # stripping leaves nothing
        "testnet",
        "paper_trade",
    ],
)
def test_a_test_venue_the_rule_cannot_map_comes_back_unchanged(name):
    """Fails closed: the caller meets the API's own refusal, naming what it asked."""
    assert is_test_venue(name)
    assert data_connector_for(name) == name


def test_an_exception_beats_the_suffix_rule(monkeypatch):
    monkeypatch.setitem(
        venues.DATA_VENUE_EXCEPTIONS, "foo_perpetual_testnet", "foo_futures"
    )
    assert data_connector_for("foo_perpetual_testnet") == "foo_futures"
    # ... and is still subject to what the feed serves.
    assert data_connector_for("foo_perpetual_testnet", ["foo_perpetual"]) == (
        "foo_perpetual_testnet"
    )
    assert data_connector_for("foo_perpetual_testnet", ["foo_futures"]) == "foo_futures"


def test_an_exception_that_names_another_test_venue_is_ignored(monkeypatch):
    """A stand-in the API would refuse as well is no stand-in."""
    monkeypatch.setitem(
        venues.DATA_VENUE_EXCEPTIONS, "foo_testnet", "foo_perpetual_testnet"
    )
    assert data_connector_for("foo_testnet") == "foo_testnet"


def test_the_exceptions_table_is_empty_because_the_suffix_rule_covers_the_live_list():
    """Pinned so that adding the first entry is a decision someone has to read here."""
    assert venues.DATA_VENUE_EXCEPTIONS == {}


API_SERVICE = ROOT.parent / "hummingbot-api" / "services" / "market_data_service.py"


@pytest.mark.skipif(
    not API_SERVICE.is_file(),
    reason="the hummingbot-api checkout is not next to this one",
)
def test_the_markers_are_the_ones_the_api_applies():
    """The rule is a mirror of the API's; this is what notices it drifting."""
    text = API_SERVICE.read_text()
    match = re.search(r"^_NON_MARKET_CONNECTOR_MARKERS\s*=\s*(\(.*?\))\s*$", text, re.M)
    assert match, "hummingbot-api no longer defines _NON_MARKET_CONNECTOR_MARKERS"
    assert ast.literal_eval(match.group(1)) == venues.TEST_VENUE_MARKERS


# ── the client wrapper ──


class _Router:
    """A ``client.market_data`` that records the connector each reader was given."""

    def __init__(self):
        self.calls = []

    async def get_candles(
        self, connector_name, trading_pair, interval="1m", max_records=100
    ):
        self.calls.append(("get_candles", connector_name))
        return []

    async def get_historical_candles(
        self,
        connector_name,
        trading_pair,
        interval="1m",
        start_time=None,
        end_time=None,
    ):
        self.calls.append(("get_historical_candles", connector_name))
        return []

    async def get_candles_last_days(
        self, connector_name, trading_pair, days, interval="1h"
    ):
        self.calls.append(("get_candles_last_days", connector_name))
        return []

    async def get_funding_info(self, connector_name, trading_pair):
        self.calls.append(("get_funding_info", connector_name))
        return {}

    async def get_prices(self, connector_name, trading_pairs):
        self.calls.append(("get_prices", connector_name))
        return {}

    async def get_order_book(self, connector_name, trading_pair, depth=10):
        self.calls.append(("get_order_book", connector_name))
        return {}

    async def get_price_for_volume(self, connector_name, trading_pair, volume, is_buy):
        self.calls.append(("get_price_for_volume", connector_name))
        return {}

    async def get_tickers(self, connectors=None, refresh=False, max_age=None):
        self.calls.append(("get_tickers", connectors))
        return {}

    async def get_rates(self, trading_pairs, connector=None):
        self.calls.append(("get_rates", connector))
        return {}

    async def get_available_candle_connectors(self):
        self.calls.append(("get_available_candle_connectors", None))
        return []


class _Client:
    def __init__(self):
        self.market_data = _Router()
        self.portfolio = SimpleNamespace(name="portfolio")


TESTNET, MAINNET_NAME = "binance_perpetual_testnet", "binance_perpetual"


def _wrapped_calls(make_calls):
    client = _Client()
    asyncio.run(make_calls(data_venue_client(client).market_data))
    return client.market_data.calls


def test_the_market_readers_follow_a_test_venue_to_mainnet():
    async def go(md):
        await md.get_candles(TESTNET, "ETH-USDT", "1m", 10)
        await md.get_historical_candles(TESTNET, "ETH-USDT", "1h", 1, 2)
        await md.get_candles_last_days(TESTNET, "ETH-USDT", 7, "1h")
        await md.get_funding_info(TESTNET, "ETH-USDT")

    assert _wrapped_calls(go) == [
        ("get_candles", MAINNET_NAME),
        ("get_historical_candles", MAINNET_NAME),
        ("get_candles_last_days", MAINNET_NAME),
        ("get_funding_info", MAINNET_NAME),
    ]


def test_the_connector_may_be_passed_by_keyword_as_the_routines_do():
    async def go(md):
        await md.get_candles(
            connector_name=TESTNET, trading_pair="ETH-USDT", interval="5m"
        )
        await md.get_candles_last_days(
            connector_name=TESTNET, trading_pair="ETH-USDT", days=1
        )
        await md.get_funding_info(connector_name=TESTNET, trading_pair="ETH-USDT")

    assert [c for _, c in _wrapped_calls(go)] == [MAINNET_NAME] * 3


def test_what_the_venue_itself_answers_is_not_redirected():
    """Quotes, the book and the tickers are the venue's own state: they stay on the
    connector being traded, or the loop would price its orders off another market."""

    async def go(md):
        await md.get_prices(TESTNET, ["ETH-USDT"])
        await md.get_order_book(TESTNET, "ETH-USDT", depth=5)
        await md.get_price_for_volume(TESTNET, "ETH-USDT", 1.0, True)
        await md.get_tickers(TESTNET)
        await md.get_rates(["ETH-USDT"], connector=TESTNET)

    assert _wrapped_calls(go) == [
        ("get_prices", TESTNET),
        ("get_order_book", TESTNET),
        ("get_price_for_volume", TESTNET),
        ("get_tickers", TESTNET),
        ("get_rates", TESTNET),
    ]


def test_the_feed_listing_is_the_apis_own_and_not_widened():
    assert _wrapped_calls(lambda md: md.get_available_candle_connectors()) == [
        ("get_available_candle_connectors", None)
    ]


def test_a_connector_that_is_not_a_test_venue_reaches_the_router_as_it_was_asked():
    async def go(md):
        await md.get_candles("binance_perpetual", "ETH-USDT", "1m", 10)
        await md.get_candles(connector_name="binance", trading_pair="ETH-USDT")
        await md.get_funding_info("hyperliquid_perpetual", "ETH-USD")

    assert _wrapped_calls(go) == [
        ("get_candles", "binance_perpetual"),
        ("get_candles", "binance"),
        ("get_funding_info", "hyperliquid_perpetual"),
    ]


def test_everything_but_market_data_is_the_real_clients():
    client = _Client()
    wrapped = data_venue_client(client)

    assert wrapped.portfolio is client.portfolio
    with pytest.raises(AttributeError):
        wrapped.no_such_router


def test_the_redirected_readers_keep_their_names_and_stay_coroutine_functions():
    md = data_venue_client(_Client()).market_data

    assert md.get_candles.__name__ == "get_candles"
    assert asyncio.iscoroutinefunction(md.get_candles)
    assert asyncio.iscoroutinefunction(md.get_prices)


def test_a_client_without_market_data_comes_back_as_it_was():
    """``None`` (no server resolved), a test double, an uninitialised client."""

    class Bare:
        pass

    class Uninitialised:
        @property
        def market_data(self):
            raise RuntimeError("Client not initialized")

    bare, uninit = Bare(), Uninitialised()
    assert data_venue_client(None) is None
    assert data_venue_client(bare) is bare
    assert data_venue_client(uninit) is uninit
    half = SimpleNamespace(market_data=None)
    assert data_venue_client(half) is half


def test_wrapping_twice_does_not_stack_wrappers():
    once = data_venue_client(_Client())

    assert data_venue_client(once) is once


# ── the call sites ──


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _series(n: int, last_close: float) -> list[dict]:
    """``n`` candles walking up and down, ending exactly on ``last_close``."""
    rows = []
    for i in range(n):
        base = last_close + ((i % 11) - 5) * 0.8 - (n - 1 - i) * 0.05
        rows.append(
            {
                "timestamp": 1_700_000_000.0 + i * 60,
                "open": base,
                "high": base + 2.0,
                "low": base - 2.0,
                "close": base + 0.4,
                "volume": 100.0 + (i % 7),
            }
        )
    rows[-1]["close"] = last_close
    return rows


def _refuse_unless_in_feed(connector):
    """The API's candle routes: only the connectors ``CandlesFactory`` knows."""
    if connector not in LIVE_CANDLE_FEED:
        raise RuntimeError(
            f"400, message=\"Unsupported connector '{connector}'. "
            "Available connectors: ['binance_perpetual', ...]\""
        )


class _ApiMarketData:
    """Answers the way the Hummingbot API does.

    Candles and funding for the candle feed's names only; the book, quotes and
    trading rules under any name. A routine that hands a test name to a candle reader
    gets the same 400 it gets from the live server, so the assertion that it works is
    the real one.
    """

    LAST_CLOSE = 2342.45
    BOOK_BID, BOOK_ASK = 2344.0, 2344.5

    def __init__(self):
        self.calls = []

    def _market(self, kind, connector):
        self.calls.append((kind, connector))
        _refuse_unless_in_feed(connector)

    def reads(self, kind):
        return [c for k, c in self.calls if k == kind]

    async def get_candles(
        self, connector_name, trading_pair, interval="1m", max_records=100
    ):
        self._market("get_candles", connector_name)
        return _series(min(max_records, 200), self.LAST_CLOSE)

    async def get_candles_last_days(
        self, connector_name, trading_pair, days, interval="1h"
    ):
        self._market("get_candles_last_days", connector_name)
        return _series(200, self.LAST_CLOSE)

    async def get_funding_info(self, connector_name, trading_pair):
        self._market("get_funding_info", connector_name)
        return {"funding_rate": 0.0001}

    async def get_order_book(self, connector_name, trading_pair, depth=10):
        self.calls.append(("get_order_book", connector_name))
        return {
            "bids": [
                {"price": self.BOOK_BID - i * 0.5, "amount": 2.0 + i} for i in range(20)
            ],
            "asks": [
                {"price": self.BOOK_ASK + i * 0.5, "amount": 1.5 + i} for i in range(20)
            ],
        }


def _client_of(market_data):
    async def fake_get_client(*args, **kwargs):
        return SimpleNamespace(market_data=market_data)

    return fake_get_client


class _Report:
    """Swallows every ReportBuilder call: the report is not what is asserted on."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: self

    async def save(self):
        return "report"


@pytest.fixture
def no_reports(monkeypatch):
    monkeypatch.setattr("condor.reports.ReportBuilder", _Report)


@pytest.fixture(scope="module")
def market_analyzer():
    return _load(
        AGENTS / "market_making_expert" / "routines" / "market_analyzer.py",
        "market_analyzer_under_test",
    )


def _analyze(module, monkeypatch, md, connector):
    monkeypatch.setattr(module, "get_client", _client_of(md))
    monkeypatch.setattr(
        module, "build_estimated_footprint_figure", lambda *a, **k: None
    )
    config = module.Config(trading_pair="ETH-USDT", connector_name=connector)
    return asyncio.run(module.run(config, SimpleNamespace(_chat_id=0)))


def test_market_analyzer_reads_candles_and_funding_from_mainnet_and_the_book_from_the_testnet(
    market_analyzer, monkeypatch, no_reports
):
    md = _ApiMarketData()

    summary = _analyze(market_analyzer, monkeypatch, md, TESTNET)

    assert md.reads("get_candles") == [MAINNET_NAME] * 4
    assert md.reads("get_funding_info") == [MAINNET_NAME]
    assert md.reads("get_order_book") == [TESTNET]
    # The analysis is a real one, not the error string the unpatched routine returns.
    assert "Error fetching candles" not in summary
    assert "**Market Analysis: ETH-USDT on binance_perpetual_testnet**" in summary
    assert "funding_rate: 0.0001" in summary
    assert "book_spread_bps:" in summary


def test_market_analyzer_says_where_its_data_came_from_and_how_far_off_the_book_is(
    market_analyzer, monkeypatch, no_reports
):
    md = _ApiMarketData()

    summary = _analyze(market_analyzer, monkeypatch, md, TESTNET)

    mid = (md.BOOK_BID + md.BOOK_ASK) / 2
    basis = (mid - md.LAST_CLOSE) / md.LAST_CLOSE * 10000
    assert (
        "data_venue: candles, funding from binance_perpetual "
        "(stand-in for binance_perpetual_testnet) | order book from "
        f"binance_perpetual_testnet | basis: {basis:+.1f} bps (book mid vs last close)"
    ) in summary


def test_market_analyzer_is_unchanged_for_a_connector_that_is_not_a_test_venue(
    market_analyzer, monkeypatch, no_reports
):
    md = _ApiMarketData()

    summary = _analyze(market_analyzer, monkeypatch, md, "binance_perpetual")

    assert md.reads("get_candles") == ["binance_perpetual"] * 4
    assert md.reads("get_funding_info") == ["binance_perpetual"]
    assert md.reads("get_order_book") == ["binance_perpetual"]
    assert "data_venue" not in summary and "basis" not in summary
    assert [line.split(":")[0] for line in summary.splitlines()] == [
        "**Market Analysis",
        "Overall Regime",
        "Spread Recommendation",
        "funding_rate",
        "book_spread_bps",
        "daily_vol",
    ]


def test_market_analyzer_still_skips_funding_on_a_spot_test_venue(
    market_analyzer, monkeypatch, no_reports
):
    md = _ApiMarketData()

    summary = _analyze(market_analyzer, monkeypatch, md, "bybit_testnet")

    assert md.reads("get_candles") == ["bybit"] * 4
    assert md.reads("get_funding_info") == []
    assert md.reads("get_order_book") == ["bybit_testnet"]
    assert "stand-in for bybit_testnet" in summary


def test_market_analyzer_names_both_connectors_when_the_stand_in_is_refused(
    market_analyzer, monkeypatch, no_reports
):
    """ndax has no mainnet candle feed on this API, so the stand-in is refused. The
    API's 400 names only the stand-in, which would read as a different problem."""
    md = _ApiMarketData()

    summary = _analyze(market_analyzer, monkeypatch, md, "ndax_testnet")

    assert summary.startswith(
        "Error fetching candles from ndax (stand-in for ndax_testnet): "
    )
    assert "Unsupported connector 'ndax'" in summary


def test_market_analyzer_error_is_unchanged_for_a_connector_that_is_not_a_test_venue(
    market_analyzer, monkeypatch, no_reports
):
    summary = _analyze(market_analyzer, monkeypatch, _ApiMarketData(), "ndax")

    assert summary.startswith("Error fetching candles: 400, message=")


def test_the_adaptive_grid_routines_read_candles_from_mainnet(monkeypatch, no_reports):
    baseline = _load(
        AGENTS / "adaptive_grid_trader" / "routines" / "baseline_7d.py",
        "baseline_7d_under_test",
    )
    hourly = _load(
        AGENTS / "adaptive_grid_trader" / "routines" / "hourly_mtf_check.py",
        "hourly_mtf_check_under_test",
    )
    out = {}
    for name, module, config in (
        ("baseline", baseline, baseline.Config(connector_name=TESTNET)),
        ("hourly", hourly, hourly.Config(connector_name=TESTNET)),
    ):
        md = _ApiMarketData()
        monkeypatch.setattr(module, "get_client", _client_of(md))
        out[name] = (
            md,
            asyncio.run(module.run(config, SimpleNamespace(_chat_id=0))),
        )

    for name, (md, text) in out.items():
        assert md.reads("get_candles"), name
        assert set(md.reads("get_candles")) == {MAINNET_NAME}, name
        assert f"(stand-in for {TESTNET})" in text, name
    assert len(out["baseline"][0].reads("get_candles")) == 1
    assert len(out["hourly"][0].reads("get_candles")) == 3
    assert "levels are on its price scale" in out["hourly"][1]


def test_the_adaptive_grid_routines_are_unchanged_for_mainnet(monkeypatch, no_reports):
    baseline = _load(
        AGENTS / "adaptive_grid_trader" / "routines" / "baseline_7d.py",
        "baseline_7d_mainnet",
    )
    hourly = _load(
        AGENTS / "adaptive_grid_trader" / "routines" / "hourly_mtf_check.py",
        "hourly_mtf_check_mainnet",
    )
    for module in (baseline, hourly):
        md = _ApiMarketData()
        monkeypatch.setattr(module, "get_client", _client_of(md))
        text = asyncio.run(
            module.run(
                module.Config(connector_name="binance_perpetual"),
                SimpleNamespace(_chat_id=0),
            )
        )
        assert set(md.reads("get_candles")) == {"binance_perpetual"}
        assert "stand-in" not in text and "data_venue" not in text.lower()


def test_ema_research_reads_history_from_mainnet_on_both_rungs():
    ema = _load(
        AGENTS / "directional_trader" / "routines" / "ema_research_charts.py",
        "ema_research_under_test",
    )
    md = _ApiMarketData()
    client = SimpleNamespace(market_data=md)

    df = asyncio.run(ema._fetch_df(client, TESTNET, "ETH-USDT", "15m", 30))
    assert df is not None and len(df) == 200
    assert md.reads("get_candles_last_days") == [MAINNET_NAME]

    # The fallback rung, when the ranged call comes back empty.
    async def empty(*a, **k):
        md.calls.append(("get_candles_last_days", a[0] if a else k["connector_name"]))
        return []

    md.get_candles_last_days = empty
    md.calls.clear()
    df = asyncio.run(ema._fetch_df(client, TESTNET, "ETH-USDT", "15m", 30))
    assert df is not None
    assert md.reads("get_candles_last_days") == [MAINNET_NAME]
    assert md.reads("get_candles") == [MAINNET_NAME]


def test_mm_dashboard_reads_no_market_data_so_it_needs_no_mapping():
    """It asks the portfolio and the bots only, both of which are account state that
    answers under the test name. If it ever starts fetching candles, it needs this."""
    text = (
        AGENTS / "market_making_expert" / "routines" / "mm_dashboard.py"
    ).read_text()

    for reader in ("get_candles", "get_funding_info", "get_historical_candles"):
        assert reader not in text


# ── the MCP tool ──


class _McpMarketData:
    def __init__(self):
        self.calls = []

    async def get_candles(self, connector_name, trading_pair, interval, limit):
        self.calls.append(("get_candles", connector_name))
        _refuse_unless_in_feed(connector_name)
        return [[1_757_000_000, 1, 2, 0.5, 1.5, 100]]

    async def get_historical_candles(
        self, connector_name, trading_pair, interval, start_time=None, end_time=None
    ):
        self.calls.append(("get_historical_candles", connector_name))
        _refuse_unless_in_feed(connector_name)
        return {"data": [[1_757_000_000, 1, 2, 0.5, 1.5, 100]]}


def _mcp(**kwargs):
    md = _McpMarketData()
    result = asyncio.run(get_market_data(SimpleNamespace(market_data=md), **kwargs))
    return result, md


@pytest.mark.parametrize(
    "action,extra", [("candles", {}), ("historical_candles", {"start_time": 1})]
)
def test_get_market_data_reads_a_test_venue_from_mainnet_and_says_so(action, extra):
    result, md = _mcp(
        action=action, connector_name=TESTNET, trading_pair="ETH-USDT", **extra
    )

    assert {c for _, c in md.calls} == {MAINNET_NAME}
    assert result["count"] == 1
    assert result["connector_name"] == TESTNET
    assert result["data_connector"] == MAINNET_NAME


@pytest.mark.parametrize(
    "action,extra", [("candles", {}), ("historical_candles", {"start_time": 1})]
)
def test_get_market_data_result_is_unchanged_for_a_connector_that_is_not_a_test_venue(
    action, extra
):
    result, md = _mcp(
        action=action,
        connector_name="binance_perpetual",
        trading_pair="ETH-USDT",
        **extra,
    )

    assert {c for _, c in md.calls} == {"binance_perpetual"}
    assert "data_connector" not in result
    assert set(result) == {
        "action",
        "connector_name",
        "trading_pair",
        "interval",
        "count",
        "candles",
    }


# ── the web candle route ──


class _RouteCm:
    def __init__(self, client):
        self._client = client

    def has_server_access(self, user_id, name):
        return True

    async def get_client(self, name):
        return self._client


class _RouteClient:
    def __init__(self):
        self.market_data = self
        self.calls = []

    async def get_historical_candles(self, connector_name, *a, **kw):
        self.calls.append(connector_name)
        _refuse_unless_in_feed(connector_name)
        return [
            {
                "timestamp": 1000.0,
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 10.0,
            }
        ]

    async def get_candles(self, connector_name, *a, **kw):
        self.calls.append(connector_name)
        return []


@pytest.fixture
def clean_candle_state():
    market._candle_cache.clear()
    market._candle_inflight.clear()
    yield
    market._candle_cache.clear()
    market._candle_inflight.clear()


def _route(monkeypatch, connector):
    client = _RouteClient()
    monkeypatch.setattr(
        "condor.web.routes.market.get_config_manager", lambda: _RouteCm(client)
    )
    candles = asyncio.run(
        market.get_candles(
            "srv",
            connector=connector,
            trading_pair="ETH-USDT",
            interval="1m",
            limit=100,
            start_time=1_700_000_000.0,
            end_time=1_700_003_600.0,
            pool_address=None,
            user=WebUser(id=1, role="user"),
        )
    )
    return candles, client, dict(market._candle_cache)


def test_the_candle_route_charts_a_test_venue_from_its_mainnet_counterpart(
    monkeypatch, clean_candle_state
):
    candles, client, cached = _route(monkeypatch, TESTNET)

    assert [c.timestamp for c in candles] == [1000.0]
    assert client.calls == [MAINNET_NAME]
    # The cache is keyed by the name the dashboard asked for, so a chart of the
    # testnet venue and a chart of the mainnet one never share an entry by accident.
    assert [key[1] for key in cached] == [TESTNET]


def test_the_candle_route_is_unchanged_for_mainnet(monkeypatch, clean_candle_state):
    candles, client, cached = _route(monkeypatch, "binance_perpetual")

    assert client.calls == ["binance_perpetual"]
    assert [key[1] for key in cached] == ["binance_perpetual"]


# ── run_code ──


class _CodeClient:
    """What the main process hands a snippet: a client with a market_data router."""

    def __init__(self):
        self.market_data = _ApiMarketData()


@pytest.fixture
def code_store(tmp_path, monkeypatch):
    written = CodeRunStore(tmp_path / "code_runs")
    monkeypatch.setattr(code_runner, "get_code_run_store", lambda: written)
    return written


def _run_code(monkeypatch, code, client):
    import config_manager

    async def fake_get_client(*args, **kwargs):
        return client

    monkeypatch.setattr(config_manager, "get_client", fake_get_client)
    return asyncio.run(code_runner.execute_code(code))


def test_a_snippet_reads_candles_and_funding_for_a_test_venue_without_knowing_it(
    monkeypatch, code_store
):
    client = _CodeClient()
    out = _run_code(
        monkeypatch,
        "candles = await client.market_data.get_candles("
        "'binance_perpetual_testnet', 'ETH-USDT', '1m', 5)\n"
        "funding = await client.market_data.get_funding_info("
        "connector_name='binance_perpetual_testnet', trading_pair='ETH-USDT')\n"
        "book = await client.market_data.get_order_book("
        "'binance_perpetual_testnet', 'ETH-USDT')\n"
        "result = (len(candles), funding['funding_rate'], len(book['bids']))",
        client,
    )

    assert out["status"] == "ok", out["error"]
    assert out["result"] == "(5, 0.0001, 20)"
    assert client.market_data.calls == [
        ("get_candles", MAINNET_NAME),
        ("get_funding_info", MAINNET_NAME),
        ("get_order_book", TESTNET),
    ]


def test_a_snippet_on_a_connector_that_is_not_a_test_venue_is_untouched(
    monkeypatch, code_store
):
    client = _CodeClient()
    out = _run_code(
        monkeypatch,
        "await client.market_data.get_candles('binance_perpetual', 'ETH-USDT', '1m', 5)",
        client,
    )

    assert out["status"] == "ok", out["error"]
    assert client.market_data.calls == [("get_candles", "binance_perpetual")]


def test_a_snippet_still_gets_the_client_it_would_have_got_when_there_is_no_market_data(
    monkeypatch, code_store
):
    class Bare:
        pass

    out = _run_code(monkeypatch, "result = type(client).__name__", Bare())

    assert out["result"] == "Bare"
