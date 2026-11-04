"""Small keyless public-book adapters with pinned hosts and no credentials."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode, urlsplit

import aiohttp


class PublicBookError(RuntimeError):
    """The keyless public endpoint did not return a usable HTTP response."""


def coinbase_product_book_url(pair: str, depth: int) -> str:
    """Official public Advanced Trade product-book URL, not the private path."""
    base, quote = pair.split("-", 1)
    query = urlencode(
        {
            "product_id": f"{base}-{quote}",
            "limit": min(max(int(depth), 1), 100),
        }
    )
    return f"https://api.coinbase.com/api/v3/brokerage/market/product_book?{query}"


async def fetch_public_book(url: str, *, timeout: float = 10) -> dict:
    """GET a catalog-built HTTPS URL. Redirects and caller-selected hosts fail."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "api.coinbase.com":
        raise PublicBookError("unapproved public-book host")
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(
                url,
                headers={"Accept": "application/json"},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise PublicBookError(
                        f"public book returned HTTP {response.status}"
                    )
                payload = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise PublicBookError(
            f"public book request failed: {type(exc).__name__}"
        ) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("pricebook"), dict):
        raise PublicBookError("public book response has no pricebook")
    return payload["pricebook"]
