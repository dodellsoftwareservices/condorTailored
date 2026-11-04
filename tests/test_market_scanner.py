"""`fetch_top_pairs` in `routines/market_scanner.py`.

Previously hit Binance's futures REST ticker directly — hardcoded to one
exchange and 451-blocked for US IPs (system review 2026-10-31, finding #10).
Now reads the connector-agnostic `fetch_tickers` used elsewhere in this file,
so these pin: the USDT/USD/USDC quote filter, the volume ranking/cutoff, and
the loud failure when a connector's ticker source can't answer a
volume-ranking question at all (some newer connectors fall back to a
price-only read with every volume reported as 0).
"""

import asyncio
from types import SimpleNamespace

import pytest

from routines import market_scanner as S
from routines.base import normalize_result


def _tickers(mapping):
    return {"tickers": mapping, "updated_at": None}


def test_filters_to_stable_quoted_pairs_and_ranks_by_volume(monkeypatch):
    async def fake_fetch_tickers(client, connector, strict=False):
        return _tickers(
            {
                "BTC-USDT": {"price": 100.0, "usd_volume": 50_000_000},
                "ETH-USDC": {"price": 10.0, "usd_volume": 20_000_000},
                "SOL-BTC": {
                    "price": 0.001,
                    "usd_volume": 30_000_000,
                },  # not stable-quoted
                "DUST-USD": {"price": 0.01, "usd_volume": 100},  # below min_volume
            }
        )

    monkeypatch.setattr(S, "fetch_tickers", fake_fetch_tickers)
    pairs = asyncio.run(
        S.fetch_top_pairs(client=object(), connector="x", top_n=10, min_volume=1_000)
    )
    assert [p["trading_pair"] for p in pairs] == ["BTC-USDT", "ETH-USDC"]
    assert pairs[0]["volume_24h_usd"] == 50_000_000


def test_respects_top_n(monkeypatch):
    async def fake_fetch_tickers(client, connector, strict=False):
        return _tickers(
            {f"T{i}-USDT": {"price": 1.0, "usd_volume": float(i)} for i in range(1, 6)}
        )

    monkeypatch.setattr(S, "fetch_tickers", fake_fetch_tickers)
    pairs = asyncio.run(
        S.fetch_top_pairs(client=object(), connector="x", top_n=2, min_volume=0)
    )
    assert len(pairs) == 2
    assert pairs[0]["trading_pair"] == "T5-USDT"  # highest volume first


def test_falls_back_to_quote_volume_when_usd_volume_is_absent(monkeypatch):
    async def fake_fetch_tickers(client, connector, strict=False):
        return _tickers({"BTC-USDT": {"price": 100.0, "quote_volume": 2_000_000}})

    monkeypatch.setattr(S, "fetch_tickers", fake_fetch_tickers)
    pairs = asyncio.run(
        S.fetch_top_pairs(
            client=object(), connector="x", top_n=10, min_volume=1_000_000
        )
    )
    assert pairs and pairs[0]["volume_24h_usd"] == 2_000_000


def test_raises_clearly_when_connector_has_no_volume_data(monkeypatch):
    """A price-only ticker source (every volume 0) must not read as 'nothing liquid.'"""

    async def fake_fetch_tickers(client, connector, strict=False):
        return _tickers(
            {
                "BTC-USDC": {"price": 100.0, "usd_volume": 0, "quote_volume": 0},
                "ETH-USDC": {"price": 10.0, "usd_volume": 0, "quote_volume": 0},
            }
        )

    monkeypatch.setattr(S, "fetch_tickers", fake_fetch_tickers)
    with pytest.raises(RuntimeError, match="no 24h volume data available"):
        asyncio.run(
            S.fetch_top_pairs(
                client=object(), connector="lighter_perpetual", top_n=10, min_volume=0
            )
        )


def test_an_empty_ticker_pool_does_not_raise_the_no_volume_error(monkeypatch):
    """No pairs at all is a different, legitimate outcome from 'volume unreported.'"""

    async def fake_fetch_tickers(client, connector, strict=False):
        return _tickers({})

    monkeypatch.setattr(S, "fetch_tickers", fake_fetch_tickers)
    pairs = asyncio.run(
        S.fetch_top_pairs(client=object(), connector="x", top_n=10, min_volume=0)
    )
    assert pairs == []


def test_run_reports_failed_not_ok_when_fetch_top_pairs_raises(monkeypatch):
    """finding #10: a caught ticker-fetch failure must not report ✅."""

    async def get_client(chat_id, context=None):
        return object()

    async def fake_fetch_top_pairs(client, connector, top_n, min_volume):
        raise RuntimeError("boom")

    monkeypatch.setattr(S, "get_client", get_client)
    monkeypatch.setattr(S, "fetch_top_pairs", fake_fetch_top_pairs)
    raw = asyncio.run(S.run(S.Config(), SimpleNamespace(_chat_id=1)))
    result = normalize_result(raw)
    assert result.failed is True
    assert "boom" in result.text


@pytest.mark.parametrize("candles", [{}, {"BTC-USDC": []}])
def test_all_failed_or_unusable_candles_are_failed_runs(monkeypatch, candles):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(S, "get_client", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        S, "fetch_top_pairs", AsyncMock(return_value=[{"trading_pair": "BTC-USDC"}])
    )
    monkeypatch.setattr(S, "fetch_all_candles", AsyncMock(return_value=candles))
    raw = asyncio.run(S.run(S.Config(), SimpleNamespace(_chat_id=1)))
    assert normalize_result(raw).failed


def test_an_empty_successful_market_scan_is_not_an_outage(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(S, "get_client", AsyncMock(return_value=object()))
    monkeypatch.setattr(S, "fetch_top_pairs", AsyncMock(return_value=[]))
    raw = asyncio.run(S.run(S.Config(), SimpleNamespace(_chat_id=1)))
    assert not normalize_result(raw).failed
