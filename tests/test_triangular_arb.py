"""``routines/triangular_arb.py``: cycle discovery, pricing math, and the venue it reads."""

import asyncio
from types import SimpleNamespace

import pytest

from condor.reports.builder import ReportBuilder
from routines import triangular_arb as T
from routines.base import normalize_result

# A synthetic venue: USD, BTC, ETH. Listed orientation mirrors a real one:
# BTC-USD, ETH-USD, ETH-BTC.
PAIRS = {
    ("BTC", "USD"): {"pair": "BTC-USD", "price": 100_000.0, "volume": None},
    ("ETH", "USD"): {"pair": "ETH-USD", "price": 4_000.0, "volume": None},
    ("ETH", "BTC"): {"pair": "ETH-BTC", "price": 0.04, "volume": None},
}

FLAT_BOOK = {"BTC-USD": 100_000.0, "ETH-USD": 4_000.0, "ETH-BTC": 0.04}


class FlatBookClient:
    """Returns one price per pair, no slippage, so the math is checkable by hand."""

    def __init__(self, book, tickers=None, ticker_error=None):
        self.market_data = self
        self.book = book
        self.tickers = tickers
        self.ticker_error = ticker_error
        self.ticker_calls: list[str] = []

    async def get_tickers(self, connector):
        self.ticker_calls.append(connector)
        if self.ticker_error:
            raise self.ticker_error
        return self.tickers

    async def get_price_for_volume(self, connector_name, trading_pair, volume, is_buy):
        return {"result_price": self.book[trading_pair]}


def price(client, cycle, fee=0.0, pairs=PAIRS):
    return asyncio.run(T._price_cycle(client, "x", pairs, cycle, 1000.0, fee))


# ── pair discovery ──


def test_split_pair():
    assert T._split_pair("ETH-USD") == ("ETH", "USD")
    assert T._split_pair("BADPAIR") is None
    assert T._split_pair("USD-USD") is None


def test_discover_reads_the_nested_ticker_shape_and_volume():
    client = FlatBookClient(
        FLAT_BOOK,
        tickers={
            "tickers": {
                "kraken": {
                    "BTC-USD": {"price": 100_000, "quote_volume": 5e8},
                    "ETH-USD": {"price": 4_000, "quote_volume": None},
                    "junk": {"price": 1},
                }
            }
        },
    )
    pairs = asyncio.run(T._discover_pairs(client, "kraken"))
    assert pairs[("BTC", "USD")] == {
        "pair": "BTC-USD",
        "price": 100_000.0,
        "volume": 5e8,
    }
    assert pairs[("ETH", "USD")]["volume"] is None
    assert len(pairs) == 2


def test_discover_raises_instead_of_reporting_an_empty_venue():
    """A refused feed used to come back as {} and be reported as 'no markets'."""
    client = FlatBookClient({}, ticker_error=RuntimeError("400: excluded"))
    with pytest.raises(T.MarketsUnavailable, match="400: excluded"):
        asyncio.run(T._discover_pairs(client, "kraken_paper_trade"))


# ── cycle discovery ──


def test_find_cycles_both_directions_and_no_revisits():
    cycles = T._find_cycles(PAIRS, "USD", 50)
    assert ("USD", "BTC", "ETH") in cycles
    assert ("USD", "ETH", "BTC") in cycles
    assert all(len(set(c)) == 3 for c in cycles)


def test_find_cycles_ranks_by_liquidity_not_by_name():
    """Capping an alphabetical list would price whichever tickers sort first."""
    pairs = {
        ("AAA", "USD"): {"pair": "AAA-USD", "price": 1.0, "volume": 10.0},
        ("BBB", "USD"): {"pair": "BBB-USD", "price": 1.0, "volume": 20.0},
        ("ZZZ", "USD"): {"pair": "ZZZ-USD", "price": 1.0, "volume": 9e9},
        ("YYY", "USD"): {"pair": "YYY-USD", "price": 1.0, "volume": 8e9},
        ("AAA", "BBB"): {"pair": "AAA-BBB", "price": 1.0, "volume": 1.0},
        ("YYY", "ZZZ"): {"pair": "YYY-ZZZ", "price": 1.0, "volume": 1.0},
    }
    top = T._find_cycles(pairs, "USD", 2)
    assert {c[1:] for c in top} == {("YYY", "ZZZ"), ("ZZZ", "YYY")}


def test_find_cycles_without_volume_is_alphabetical_and_deterministic():
    first = T._find_cycles(PAIRS, "USD", 50)
    assert first == T._find_cycles(PAIRS, "USD", 50)
    assert first[0] == ("USD", "BTC", "ETH")


def test_find_cycles_respects_the_cap():
    assert len(T._find_cycles(PAIRS, "USD", 1)) == 1


# ── pricing ──


def test_zero_fee_closed_loop_returns_the_start():
    """With consistent cross rates and no fees, a cycle must return exactly 1.0x."""
    r = price(FlatBookClient(FLAT_BOOK), ("USD", "BTC", "ETH"))
    assert r is not None
    assert abs(r["end"] - 1000.0) < 1e-6
    assert abs(r["profit_bps"]) < 1e-6


def test_fee_drag_is_three_legs():
    r = price(FlatBookClient(FLAT_BOOK), ("USD", "BTC", "ETH"), fee=0.0026)
    assert abs(r["end"] - 1000.0 * (1 - 0.0026) ** 3) < 1e-6
    assert -79 < r["profit_bps"] < -77


def test_real_dislocation_is_detected():
    """ETH-BTC 1% rich: the cycle that sells ETH for BTC must profit."""
    book = {**FLAT_BOOK, "ETH-BTC": 0.0404}
    r = price(FlatBookClient(book), ("USD", "ETH", "BTC"))
    assert r["profit_bps"] > 90


def test_missing_market_is_dropped_not_guessed():
    only_btc = {("BTC", "USD"): PAIRS[("BTC", "USD")]}
    r = price(
        FlatBookClient({"BTC-USD": 100_000.0}), ("USD", "BTC", "ETH"), pairs=only_btc
    )
    assert r is None


# ── run(): which venue it reads, and what it says when that fails ──


@pytest.fixture
def report_spy(monkeypatch):
    """ReportBuilder with save() stubbed and every markdown block recorded."""
    blocks: list[str] = []
    real_markdown = ReportBuilder.markdown

    def markdown(self, text):
        blocks.append(text)
        return real_markdown(self, text)

    async def save(self, report_id=None):
        return "report-id"

    monkeypatch.setattr(ReportBuilder, "markdown", markdown)
    monkeypatch.setattr(ReportBuilder, "save", save)
    return blocks


def run_routine(monkeypatch, client, **cfg):
    async def get_client(chat_id, context=None):
        return client

    monkeypatch.setattr(T, "get_client", get_client)
    raw = asyncio.run(T.run(T.Config(**cfg), SimpleNamespace(_chat_id=1)))
    return normalize_result(raw).text


TICKERS = {
    "tickers": {
        "kraken": {
            "BTC-USD": {"price": 100_000, "quote_volume": 5e8},
            "ETH-USD": {"price": 4_000, "quote_volume": 3e8},
            "ETH-BTC": {"price": 0.04, "quote_volume": 1e7},
        }
    }
}


def test_default_config_reads_a_venue_the_api_serves():
    assert T.Config().connector == "kraken"


def test_scan_reads_the_mainnet_twin_of_a_paper_name(monkeypatch, report_spy):
    client = FlatBookClient(FLAT_BOOK, tickers=TICKERS)
    out = run_routine(monkeypatch, client, connector="kraken_paper_trade")
    assert client.ticker_calls == ["kraken"]
    assert any("`kraken_paper_trade` serves no market data" in b for b in report_spy)
    assert "cycle" in out.lower() or "bps" in out


def test_scan_of_a_plain_venue_says_nothing_about_redirects(monkeypatch, report_spy):
    client = FlatBookClient(FLAT_BOOK, tickers=TICKERS)
    run_routine(monkeypatch, client, connector="kraken")
    assert client.ticker_calls == ["kraken"]
    assert not any("serves no market data" in b for b in report_spy)


def test_a_refused_feed_reports_the_api_error_not_a_guess(monkeypatch, report_spy):
    client = FlatBookClient({}, ticker_error=RuntimeError("HTTP 502: upstream down"))
    out = run_routine(monkeypatch, client, connector="kraken")
    assert "HTTP 502: upstream down" in out
    assert "geo" not in out.lower()


def test_a_refused_feed_reports_as_failed_not_ok(monkeypatch, report_spy):
    """The caught MarketsUnavailable must not report ✅ through the executor (finding #10)."""
    client = FlatBookClient({}, ticker_error=RuntimeError("HTTP 502: upstream down"))

    async def get_client(chat_id, context=None):
        return client

    monkeypatch.setattr(T, "get_client", get_client)
    raw = asyncio.run(T.run(T.Config(connector="kraken"), SimpleNamespace(_chat_id=1)))
    assert normalize_result(raw).failed is True


def test_unmappable_test_venue_is_named_in_the_error(monkeypatch, report_spy):
    client = FlatBookClient({}, ticker_error=RuntimeError("400: excluded"))
    out = run_routine(monkeypatch, client, connector="foo_testnet_v2")
    assert "no mainnet venue to read instead" in out
