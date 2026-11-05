"""Testnet and sandbox venues are selectable: they are the paper-trading path.

Hummingbot's ``*_paper_trade`` connectors cannot run V2 controllers, so the
exchange testnets are the only paper market. The connector picker used to drop
every name containing "testnet" or "sandbox", and ``is_gateway_network`` would
have routed the ones that survived to the DEX flow.
"""

from __future__ import annotations

import asyncio

import pytest

from condor.web.routes import settings
from handlers import is_gateway_network

RAW = [
    "binance_perpetual",
    "binance_perpetual_testnet",
    "bybit_testnet",
    "architect_perpetual_sandbox",
    "solana/raydium/clmm",
]


class _Sds:
    async def get_or_fetch(self, server, data_type):
        return RAW


@pytest.fixture
def listing(monkeypatch):
    monkeypatch.setattr(settings, "get_server_data_service", lambda: _Sds())

    def call(type=None):
        out = asyncio.run(
            settings.list_connectors(server="local", type=type, user=None)
        )
        return {c["name"]: c["type"] for c in out["connectors"]}

    return call


def test_testnet_and_sandbox_connectors_are_listed(listing):
    names = listing()
    assert "binance_perpetual_testnet" in names
    assert "bybit_testnet" in names
    assert "architect_perpetual_sandbox" in names


def test_gateway_connectors_are_still_filtered(listing):
    assert "solana/raydium/clmm" not in listing()


def test_perpetual_filter_keeps_perpetual_testnets(listing):
    names = listing("perpetual")
    assert set(names) == {
        "binance_perpetual",
        "binance_perpetual_testnet",
        "architect_perpetual_sandbox",
    }
    assert all(t == "perpetual" for t in names.values())
    assert set(listing("spot")) == {"bybit_testnet"}


@pytest.mark.parametrize(
    "name",
    ["binance_perpetual_testnet", "bybit_testnet", "architect_perpetual_sandbox"],
)
def test_cex_testnets_are_not_gateway_networks(name):
    assert not is_gateway_network(name)


@pytest.mark.parametrize("name", ["solana-mainnet-beta", "ethereum-mainnet", "base"])
def test_gateway_networks_are_still_detected(name):
    assert is_gateway_network(name)
