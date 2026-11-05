"""Routine-level tests for the read-only US same-instrument scanner."""

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from condor.arbitrage import BookError
from routines import us_arb_scanner as scanner
from routines.base import normalize_result


class Client:
    def __init__(self, books=None, failures=None):
        self.market_data = self
        self.books = books or {}
        self.failures = failures or {}
        self.calls = []

    async def get_order_book(self, connector_name, trading_pair, depth):
        self.calls.append((connector_name, trading_pair, depth))
        if connector_name in self.failures:
            raise RuntimeError(self.failures[connector_name])
        return self.books[connector_name]


def book(bid, ask, *, timestamp=None, quantity=100):
    return {
        "bids": [[bid, quantity]],
        "asks": [[ask, quantity]],
        "timestamp": time.time() if timestamp is None else timestamp,
    }


def config(**overrides):
    values = {
        "trading_pair": "BTC-USD",
        "exchanges": "kraken,gemini",
        "amount": 1,
        "taker_fee_bps": 10,
        "latency_buffer_bps": 0,
        "max_book_skew_seconds": 30,
    }
    values.update(overrides)
    return scanner.Config(**values)


def test_default_is_same_class_us_spot_not_perpetual_vs_global_spot():
    cfg = scanner.Config()
    names = scanner._exchanges(cfg.exchanges)
    assert cfg.trading_pair == "BTC-USD"
    assert "lighter_perpetual" not in names
    assert "kucoin" not in names
    assert len(names) >= 4


def test_scan_walks_books_and_returns_modeled_net_route():
    now = time.time()
    client = Client(
        {
            "kraken": book(99, 100, timestamp=now),
            "gemini": book(102, 103, timestamp=now),
        }
    )
    result = asyncio.run(scanner.scan(client, config()))
    assert not result["rejected"]
    assert len(result["routes"]) == 1
    route = result["routes"][0]
    assert route.buy_connector == "kraken"
    assert route.sell_connector == "gemini"
    assert route.modeled_net_quote == pytest.approx(2 - 0.1 - 0.102)


def test_coinbase_uses_direct_public_adapter_and_exchange_timestamp(monkeypatch):
    now = time.time()
    direct = book(100, 101, timestamp=now)
    direct["time"] = direct.pop("timestamp")
    direct["time"] = datetime.fromtimestamp(now, timezone.utc).isoformat()

    async def fetch(url):
        assert "/market/product_book" in url
        return direct

    monkeypatch.setattr(scanner, "fetch_public_book", fetch)
    client = Client({"kraken": book(99, 102, timestamp=now)})
    result = asyncio.run(
        scanner.scan(
            client,
            config(exchanges="kraken,coinbase_advanced_trade"),
        )
    )
    assert len(result["books"]) == 2
    coinbase = next(
        book for book in result["books"] if book.connector.startswith("coinbase")
    )
    assert coinbase.timestamp_origin == "exchange"
    assert client.calls == [("kraken", "BTC-USD", 100)]


def test_failed_venue_is_reported_not_treated_as_empty_market():
    now = time.time()
    client = Client(
        {"kraken": book(99, 100, timestamp=now)},
        failures={"gemini": "upstream unavailable"},
    )
    result = asyncio.run(scanner.scan(client, config()))
    assert len(result["books"]) == 1
    assert result["rejected"][0][0] == "gemini"
    assert "upstream unavailable" in result["rejected"][0][1]


def test_stale_venue_is_excluded():
    now = time.time()
    client = Client(
        {
            "kraken": book(99, 100, timestamp=now - 120),
            "gemini": book(102, 103, timestamp=now),
        }
    )
    result = asyncio.run(scanner.scan(client, config()))
    assert [book.connector for book in result["books"]] == ["gemini"]
    assert "stale" in result["rejected"][0][1]


@pytest.mark.parametrize(
    "exchanges",
    [
        "kraken,lighter_perpetual",
        "kraken,lighter_testnet",
        "kraken,kraken_paper_trade",
    ],
)
def test_mixed_or_simulated_scans_are_refused(exchanges):
    with pytest.raises(BookError):
        asyncio.run(scanner.scan(Client(), config(exchanges=exchanges)))


def test_unknown_venue_requires_an_explicit_fee():
    with pytest.raises(BookError, match="fee"):
        asyncio.run(
            scanner.scan(Client(), config(exchanges="kraken,unknown", taker_fee_bps=-1))
        )


def test_duplicate_venues_cannot_compare_a_book_to_itself():
    with pytest.raises(BookError, match="two distinct"):
        asyncio.run(scanner.scan(Client(), config(exchanges="kraken,kraken")))


def test_run_returns_structured_routes_and_no_orders(monkeypatch):
    now = time.time()
    client = Client(
        {
            "kraken": book(99, 100, timestamp=now),
            "gemini": book(102, 103, timestamp=now),
        }
    )
    monkeypatch.setattr(scanner, "get_client", AsyncMock(return_value=client))
    result = normalize_result(
        asyncio.run(scanner.run(config(), SimpleNamespace(_chat_id=1)))
    )
    assert not result.failed
    assert result.table_data
    assert result.table_data[0]["Buy venue"] == "kraken"
    assert "Modeled only" in result.text
    assert "withdrawal/transfer costs" in result.text
    assert [call[0] for call in client.calls] == ["kraken", "gemini"]


def test_run_fails_when_fewer_than_two_valid_books(monkeypatch):
    client = Client(failures={"kraken": "no", "gemini": "no"})
    monkeypatch.setattr(scanner, "get_client", AsyncMock(return_value=client))
    result = normalize_result(
        asyncio.run(scanner.run(config(), SimpleNamespace(_chat_id=1)))
    )
    assert result.failed
    assert "Fewer than two" in result.text


def test_missing_backend_is_a_failed_run(monkeypatch):
    monkeypatch.setattr(scanner, "get_client", AsyncMock(return_value=None))
    result = normalize_result(
        asyncio.run(scanner.run(config(), SimpleNamespace(_chat_id=1)))
    )
    assert result.failed
