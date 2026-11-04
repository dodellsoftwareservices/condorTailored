"""condor/web/routes/equities.py against the real app, with the market-data
module and the Alpaca client replaced. No live network, no credentials.
"""

import pytest
from starlette.testclient import TestClient

from condor.equity_market_data import EquityDataError, EquitySymbolNotFound, Instrument
from condor.web.app import create_app
from condor.web.auth import get_current_user
from condor.web.models import WebUser
from condor.web.routes import equities
from condor.web.routes._equity_client import get_alpaca_client


class FakeAlpacaForQuotes:
    def __init__(self, quote: dict | None = None, error: Exception | None = None):
        self._quote = quote or {
            "symbol": "AAPL",
            "quote": {
                "bp": 234.5,
                "ap": 234.7,
                "bs": 1,
                "as": 2,
                "t": "2026-10-31T13:00:00Z",
            },
        }
        self._error = error

    async def latest_quote(self, symbol: str) -> dict:
        if self._error is not None:
            raise self._error
        return self._quote


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setenv("EQUITY_MARKETS_ENABLED", "true")
    monkeypatch.setenv("EQUITY_PAPER_TRADING_ENABLED", "false")
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: WebUser(
        id=7, username="u", first_name="U", role="user"
    )
    app.dependency_overrides[get_alpaca_client] = lambda: FakeAlpacaForQuotes()
    return TestClient(app)


def test_search_returns_mocked_results(monkeypatch, app_client):
    async def fake_search(query, limit):
        assert query == "apple"
        assert limit == 8
        return [
            Instrument(
                symbol="AAPL",
                name="Apple Inc.",
                exchange="NMS",
                currency="USD",
                quote_type="EQUITY",
            )
        ]

    monkeypatch.setattr(equities, "search_symbols", fake_search)

    response = app_client.get("/api/v1/equities/search", params={"q": "apple"})

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "apple"
    assert body["results"] == [
        {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "exchange": "NMS",
            "quote_type": "EQUITY",
        }
    ]


def test_search_is_not_swallowed_by_the_symbol_route(monkeypatch, app_client):
    """Regression: /equities/search must not be matched as /{symbol}.

    Declared first in equities.py specifically to avoid this; if that ever
    moves, this call would 200 against get_equity_instrument("search")
    (and go to the yfinance-backed path, which is unmocked here) instead of
    the search handler.
    """
    calls = []

    async def fake_search(q, limit):
        calls.append(q)
        return []

    monkeypatch.setattr(equities, "search_symbols", fake_search)

    response = app_client.get("/api/v1/equities/search", params={"q": "x"})

    assert response.status_code == 200
    assert calls == ["x"]  # reached search_symbols, not fetch_instrument("search")


def test_rollout_status_route_echoes_the_real_function(app_client):
    response = app_client.get("/api/v1/equities/rollout-status")
    assert response.status_code == 200
    assert response.json() == {
        "markets_enabled": True,
        "paper_submissions_enabled": False,
    }


def test_instrument_not_found_is_404(monkeypatch, app_client):
    async def boom(symbol):
        raise EquitySymbolNotFound(symbol)

    monkeypatch.setattr(equities, "fetch_instrument", boom)

    response = app_client.get("/api/v1/equities/NOPE")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "equity_symbol_not_found"


def test_instrument_provider_error_maps_to_equity_upstream_error(
    monkeypatch, app_client
):
    async def boom(symbol):
        raise EquityDataError("upstream exploded")

    monkeypatch.setattr(equities, "fetch_instrument", boom)

    response = app_client.get("/api/v1/equities/AAPL")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "equity_provider_error"


def test_instrument_success(monkeypatch, app_client):
    async def fake(symbol):
        assert symbol == "AAPL"
        return Instrument(
            symbol="AAPL",
            name="Apple Inc.",
            exchange="NMS",
            currency="USD",
            quote_type="EQUITY",
        )

    monkeypatch.setattr(equities, "fetch_instrument", fake)

    response = app_client.get("/api/v1/equities/AAPL")

    assert response.status_code == 200
    assert response.json()["name"] == "Apple Inc."
    assert response.json()["source"] == "yfinance"


def test_bars_not_found_is_404(monkeypatch, app_client):
    async def boom(symbol, period, interval):
        raise EquitySymbolNotFound(symbol)

    monkeypatch.setattr(equities, "fetch_bars", boom)

    response = app_client.get("/api/v1/equities/NOPE/bars")

    assert response.status_code == 404


def test_bars_success_is_shaped_like_candle_data(monkeypatch, app_client):
    async def fake(symbol, period, interval):
        assert (symbol, period, interval) == ("AAPL", "1mo", "1d")
        return [
            {
                "timestamp": 1.0,
                "open": 2.0,
                "high": 3.0,
                "low": 1.0,
                "close": 2.5,
                "volume": 100.0,
            }
        ]

    monkeypatch.setattr(equities, "fetch_bars", fake)

    response = app_client.get(
        "/api/v1/equities/AAPL/bars", params={"period": "1mo", "interval": "1d"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["bars"] == [
        {
            "timestamp": 1.0,
            "open": 2.0,
            "high": 3.0,
            "low": 1.0,
            "close": 2.5,
            "volume": 100.0,
        }
    ]


def test_bars_rejects_an_unsupported_interval(app_client):
    response = app_client.get("/api/v1/equities/AAPL/bars", params={"interval": "3m"})
    assert (
        response.status_code == 422
    )  # FastAPI's own pattern validation, before any handler runs


def test_quote_uses_alpaca_not_yfinance(app_client):
    response = app_client.get("/api/v1/equities/AAPL/quote")

    assert response.status_code == 200
    body = response.json()
    assert body["bid"] == 234.5
    assert body["ask"] == 234.7
    assert body["source"] == "alpaca"
    assert body["timestamp"] > 0


def test_quote_with_no_usable_price_is_a_provider_error_not_a_fake_success(monkeypatch):
    """A shape with no readable bid or ask must not become a 200 of zeros —
    that would read to a caller as "trading at $0", not "could not be read".
    Matches market.py's own "Unexpected response format" precedent for the
    same situation (an upstream call that succeeded but made no sense).

    (Previously this test asserted the 200-of-zeros behavior directly; an
    independent review of this code correctly flagged that as the bug.)
    """
    monkeypatch.setenv("EQUITY_MARKETS_ENABLED", "true")
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: WebUser(
        id=7, username="u", first_name="U", role="user"
    )
    app.dependency_overrides[get_alpaca_client] = lambda: FakeAlpacaForQuotes(
        quote={"unexpected": "shape"}
    )
    client = TestClient(app)

    response = client.get("/api/v1/equities/AAPL/quote")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "equity_provider_error"


def test_quote_with_a_bad_timestamp_is_reported_stale_not_fresh(monkeypatch):
    """Real prices, unparseable "t": the quote is still returned (there is a
    usable price), but its age must read as very old, never as "just now" —
    manufacturing freshness for data of unknown age is worse than admitting
    the age is unknown, since quote_age_seconds exists specifically so a
    caller can refuse an untrustworthy quote.
    """
    monkeypatch.setenv("EQUITY_MARKETS_ENABLED", "true")
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: WebUser(
        id=7, username="u", first_name="U", role="user"
    )
    app.dependency_overrides[get_alpaca_client] = lambda: FakeAlpacaForQuotes(
        quote={"quote": {"bp": 100.0, "ap": 100.5, "t": "not-a-timestamp"}}
    )
    client = TestClient(app)

    response = client.get("/api/v1/equities/AAPL/quote")

    assert response.status_code == 200
    body = response.json()
    assert body["bid"] == 100.0
    # Epoch, not "now": a reader computing age from this gets decades, not a
    # number that could be mistaken for a few seconds of real latency.
    assert body["timestamp"] == 0.0


def test_quote_provider_error_maps_through_equity_upstream_error(monkeypatch):
    from condor.alpaca_paper import AlpacaAPIError

    monkeypatch.setenv("EQUITY_MARKETS_ENABLED", "true")
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: WebUser(
        id=7, username="u", first_name="U", role="user"
    )
    app.dependency_overrides[get_alpaca_client] = lambda: FakeAlpacaForQuotes(
        error=AlpacaAPIError("boom", status_code=404)
    )
    client = TestClient(app)

    response = client.get("/api/v1/equities/AAPL/quote")

    assert (
        response.status_code == 400
    )  # AlpacaAPIError's 4xx is preserved, not flattened to 502
    assert response.json()["detail"]["code"] == "equity_provider_error"


def test_alpaca_not_configured_is_a_clean_503(monkeypatch):
    """The real get_alpaca_client() dependency, with no credentials — proves
    its own try/except converts AlpacaConfigError to a 503, rather than
    overriding the dependency with something that raises directly (which
    would only prove FastAPI turns an *uncaught* exception into a 500, not
    that this module's handling works).
    """
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    monkeypatch.setenv(
        "EQUITY_MARKETS_ENABLED", "true"
    )  # past the gate, into the handler

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: WebUser(
        id=7, username="u", first_name="U", role="user"
    )
    # No override for get_alpaca_client: the real dependency runs.
    client = TestClient(app)

    response = client.get("/api/v1/equities/AAPL/quote")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "equity_not_configured"
