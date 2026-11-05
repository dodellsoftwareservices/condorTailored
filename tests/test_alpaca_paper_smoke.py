"""Explicitly opted-in, read-only paper smoke test. Never submits an order."""

import asyncio
import math
import os

import pytest

from condor.alpaca_paper import AlpacaPaperClient


@pytest.mark.skipif(
    os.getenv("RUN_ALPACA_PAPER_SMOKE") != "1",
    reason="Set RUN_ALPACA_PAPER_SMOKE=1 explicitly with paper credentials",
)
def test_paper_account_clock_positions_and_quote():
    # Missing credentials fail when explicitly enabled rather than silently
    # reporting success. No dotenv loading and no credentials printed.
    client = AlpacaPaperClient()

    async def check():
        account = await client.get_account()
        positions = await client.get_positions()
        opened, transition = await client.is_market_open()
        quote = await client.latest_quote("AAPL")
        assert all(
            math.isfinite(v)
            for v in (account.equity, account.cash, account.buying_power)
        )
        assert isinstance(positions, list)
        assert isinstance(opened, bool)
        assert transition != "unknown"
        assert isinstance(quote.get("quote"), dict)
        assert quote["quote"].get("t")

    asyncio.run(check())
