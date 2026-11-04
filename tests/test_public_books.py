"""Pinned keyless public-book adapter tests; no external requests."""

import asyncio

import pytest

from condor.public_books import (
    PublicBookError,
    coinbase_product_book_url,
    fetch_public_book,
)


def test_coinbase_url_uses_public_market_path_and_bounded_depth():
    url = coinbase_product_book_url("BTC-USD", 1000)
    assert "/brokerage/market/product_book?" in url
    assert "/brokerage/product_book" not in url
    assert "product_id=BTC-USD" in url
    assert "limit=100" in url


@pytest.mark.parametrize(
    "url",
    [
        "http://api.coinbase.com/book",
        "https://example.com/book",
        "https://api.coinbase.com.evil.test/book",
        "https://api.coinbase.com@evil.test/book",
    ],
)
def test_fetch_refuses_unapproved_destinations_before_network(url):
    with pytest.raises(PublicBookError, match="unapproved"):
        asyncio.run(fetch_public_book(url))
