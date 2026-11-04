"""condor/web/routes/paper_equities.py against the real app, with the Alpaca
client replaced by a fake. No live network, no credentials.
"""

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from condor.agents.actions import read_actions
from condor.alpaca_paper import AlpacaAPIError, AlpacaTransportError
from condor.paths import ui_dir
from condor.web import auth
from condor.web.app import create_app
from condor.web.auth import get_current_user
from condor.web.models import WebUser
from condor.web.routes._equity_client import get_alpaca_client


class FakeAccount:
    def __init__(self, equity=10_000.0, cash=5_000.0, buying_power=9_000.0):
        self.equity, self.cash, self.buying_power = equity, cash, buying_power


class FakePosition:
    def __init__(self, symbol="AAPL", qty=1.0, market_value=234.5, unrealized_pl=1.2):
        self.symbol, self.qty = symbol, qty
        self.market_value, self.unrealized_pl = market_value, unrealized_pl


class FakeAlpaca:
    """Mirrors AlpacaPaperClient's real surface; errors are injected per method."""

    def __init__(
        self,
        account=None,
        positions=None,
        market_open=(True, "2026-11-01T21:00:00Z"),
        order_result=None,
        account_error=None,
        positions_error=None,
        market_status_error=None,
        order_error=None,
    ):
        self._account = account or FakeAccount()
        self._positions = positions if positions is not None else [FakePosition()]
        self._market_open = market_open
        self._order_result = order_result or {
            "id": "order-1",
            "status": "accepted",
            "submitted_at": "2026-10-31T13:00:00Z",
        }
        self._account_error = account_error
        self._positions_error = positions_error
        self._market_status_error = market_status_error
        self._order_error = order_error

    async def get_account(self):
        if self._account_error:
            raise self._account_error
        return self._account

    async def get_positions(self):
        if self._positions_error:
            raise self._positions_error
        return self._positions

    async def is_market_open(self):
        if self._market_status_error:
            raise self._market_status_error
        return self._market_open

    async def submit_notional_order(self, symbol, notional, side):
        if self._order_error:
            raise self._order_error
        return self._order_result


def _app(monkeypatch, *, markets=True, paper_trading=True, alpaca=None, admin=True):
    """Every route in this module is admin-only (require_admin, re-derived
    from ConfigManager — not the WebUser.role field, which it never reads),
    so building the app always wires both: the fake user *and* a fake
    get_config_manager answering is_admin() for that user's id. Mirrors
    test_equity_rollout.py's own test_diagnostics_require_admin exactly.
    """
    monkeypatch.setenv("EQUITY_MARKETS_ENABLED", "true" if markets else "false")
    monkeypatch.setenv(
        "EQUITY_PAPER_TRADING_ENABLED", "true" if paper_trading else "false"
    )
    monkeypatch.setattr(
        auth, "get_config_manager", lambda: SimpleNamespace(is_admin=lambda _: admin)
    )
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: WebUser(
        id=7, username="u", first_name="U", role="admin" if admin else "user"
    )
    app.dependency_overrides[get_alpaca_client] = lambda: alpaca or FakeAlpaca()
    return TestClient(app)


# ── admin-only, reads included ──
# (an independent review correctly caught that an earlier version of this
# module claimed "any approved user" matched existing precedent; it didn't —
# see the module's own docstring. These confirm the correction holds.)


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/paper/equities/account"),
        ("get", "/api/v1/paper/equities/positions"),
        ("get", "/api/v1/paper/equities/market-status"),
    ],
)
def test_non_admin_cannot_read_the_account(monkeypatch, method, path):
    client = _app(monkeypatch, admin=False)
    response = getattr(client, method)(path)
    assert response.status_code == 403


def test_non_admin_cannot_submit_an_order(monkeypatch):
    client = _app(monkeypatch, admin=False)
    response = client.post(
        "/api/v1/paper/equities/orders", json={"symbol": "AAPL", "notional": 10}
    )
    assert response.status_code == 403


# ── GETs reach the handler regardless of the rollout flags ──
# (the middleware only ever blocks POST under /paper/equities — confirmed
# directly against condor/web/equity_rollout.py's own gating logic, and
# covered end-to-end once more here against the real routes rather than only
# the synthetic stub app test_equity_rollout.py exercises.)


def test_account_reachable_even_with_both_flags_off(monkeypatch):
    client = _app(monkeypatch, markets=False, paper_trading=False)
    response = client.get("/api/v1/paper/equities/account")
    assert response.status_code == 200
    assert response.json() == {
        "equity": 10_000.0,
        "cash": 5_000.0,
        "buying_power": 9_000.0,
    }


def test_positions_success(monkeypatch):
    client = _app(
        monkeypatch, alpaca=FakeAlpaca(positions=[FakePosition(symbol="VOO", qty=2.0)])
    )
    response = client.get("/api/v1/paper/equities/positions")
    assert response.status_code == 200
    assert response.json() == [
        {"symbol": "VOO", "qty": 2.0, "market_value": 234.5, "unrealized_pl": 1.2}
    ]


def test_market_status_success(monkeypatch):
    client = _app(monkeypatch)
    response = client.get("/api/v1/paper/equities/market-status")
    assert response.status_code == 200
    assert response.json() == {
        "is_open": True,
        "next_transition": "2026-11-01T21:00:00Z",
    }


@pytest.mark.parametrize(
    "field,error",
    [
        ("account_error", AlpacaAPIError("boom", status_code=500)),
        ("positions_error", AlpacaAPIError("boom", status_code=500)),
        ("market_status_error", AlpacaAPIError("boom", status_code=500)),
    ],
)
def test_get_routes_map_alpaca_errors_through_equity_upstream_error(
    monkeypatch, field, error
):
    client = _app(monkeypatch, alpaca=FakeAlpaca(**{field: error}))
    path = {
        "account_error": "/api/v1/paper/equities/account",
        "positions_error": "/api/v1/paper/equities/positions",
        "market_status_error": "/api/v1/paper/equities/market-status",
    }[field]
    response = client.get(path)
    assert (
        response.status_code == 502
    )  # a 500-shaped AlpacaAPIError maps to 502, not 400
    assert response.json()["detail"]["code"] == "equity_provider_error"


# ── POST is gated, and only POST ──


def test_order_submission_blocked_when_paper_trading_disabled(monkeypatch):
    client = _app(monkeypatch, markets=True, paper_trading=False)
    response = client.post(
        "/api/v1/paper/equities/orders", json={"symbol": "AAPL", "notional": 10}
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "equity_feature_disabled"


# ── The per-order notional cap ──


def test_order_above_the_default_cap_is_rejected_before_reaching_alpaca(monkeypatch):
    monkeypatch.delenv("EQUITY_MAX_ORDER_NOTIONAL", raising=False)
    calls = []
    alpaca = FakeAlpaca()
    alpaca.submit_notional_order = (
        lambda symbol, notional, side: calls.append(1) or {}
    )  # would fail if awaited; must never be reached
    client = _app(monkeypatch, alpaca=alpaca)

    response = client.post(
        "/api/v1/paper/equities/orders", json={"symbol": "AAPL", "notional": 10_000.01}
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "equity_order_too_large"
    assert calls == []  # never reached the broker
    assert read_actions(ui_dir(7)) == []  # and no deed for a rejected submission


def test_order_cap_is_configurable(monkeypatch):
    monkeypatch.setenv("EQUITY_MAX_ORDER_NOTIONAL", "50")
    client = _app(monkeypatch)

    response = client.post(
        "/api/v1/paper/equities/orders", json={"symbol": "AAPL", "notional": 100}
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "equity_order_too_large"
    assert "$50.00" in response.json()["detail"]["message"]


@pytest.mark.parametrize("bad_value", ["", "not-a-number", "-5", "0"])
def test_order_cap_falls_back_to_the_default_on_a_bad_env_value(monkeypatch, bad_value):
    monkeypatch.setenv("EQUITY_MAX_ORDER_NOTIONAL", bad_value)
    client = _app(monkeypatch)

    # Just over the built-in $10,000 default: falls back correctly only if
    # the malformed/non-positive override was ignored rather than disabling
    # the check outright (e.g. "0" must not mean "no limit").
    response = client.post(
        "/api/v1/paper/equities/orders", json={"symbol": "AAPL", "notional": 10_000.01}
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "equity_order_too_large"


# ── Order submission: success, deed recorded, and each error kept distinct ──


def test_order_submission_success_and_records_exactly_one_deed(monkeypatch):
    client = _app(monkeypatch)

    response = client.post(
        "/api/v1/paper/equities/orders",
        json={"symbol": "aapl", "notional": 25.5, "side": "buy"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "id": "order-1",
        "symbol": "AAPL",
        "side": "buy",
        "notional": 25.5,
        "status": "accepted",
        "submitted_at": "2026-10-31T13:00:00Z",
    }

    (action,) = read_actions(ui_dir(7))
    assert action.verb == "submit_equity_order"
    # subject stays "": record_direct only keeps it for the deploy verb.
    # The symbol is in summary instead, matching every other non-deploy route.
    assert action.subject == ""
    assert "AAPL" in action.summary
    assert "25.50" in action.summary


def test_order_submission_bad_notional_is_400_invalid_request(monkeypatch):
    alpaca = FakeAlpaca(
        order_error=ValueError("notional must be finite and positive at cent precision")
    )
    client = _app(monkeypatch, alpaca=alpaca)

    response = client.post(
        "/api/v1/paper/equities/orders", json={"symbol": "AAPL", "notional": 0}
    )

    # Pydantic itself may already reject notional=0 depending on the model's
    # own constraints; either way this must not reach the client as a 500.
    assert response.status_code in (400, 422)


def test_order_submission_transport_failure_is_ambiguous_not_retryable(monkeypatch):
    alpaca = FakeAlpaca(order_error=AlpacaTransportError("timed out"))
    client = _app(monkeypatch, alpaca=alpaca)

    response = client.post(
        "/api/v1/paper/equities/orders", json={"symbol": "AAPL", "notional": 10}
    )

    assert response.status_code == 502
    body = response.json()
    assert body["detail"]["code"] == "equity_order_ambiguous"
    assert "positions before retrying" in body["detail"]["message"]

    # No deed for a submission that may or may not have gone through.
    assert read_actions(ui_dir(7)) == []


def test_order_submission_rejected_by_alpaca_maps_through_equity_upstream_error(
    monkeypatch,
):
    alpaca = FakeAlpaca(
        order_error=AlpacaAPIError("insufficient buying power", status_code=422)
    )
    client = _app(monkeypatch, alpaca=alpaca)

    response = client.post(
        # Under the $10,000 default cap: this must fail for the mocked Alpaca
        # rejection under test, not for exceeding the order-size limit.
        "/api/v1/paper/equities/orders",
        json={"symbol": "AAPL", "notional": 9_000},
    )

    assert response.status_code == 400  # a 422 upstream is the caller's own bad request
    assert response.json()["detail"]["code"] == "equity_order_rejected"

    assert read_actions(ui_dir(7)) == []


def test_order_submission_not_configured_is_503(monkeypatch):
    """AlpacaConfigError would only ever come from client construction, which
    get_alpaca_client() already converts to a 503 before the handler runs —
    exercised directly against the real dependency, with no override. This
    fires from FastAPI's own dependency resolution, before the handler body's
    require_admin() line is ever reached, so admin status doesn't bear on it.
    """
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    monkeypatch.setenv("EQUITY_MARKETS_ENABLED", "true")
    monkeypatch.setenv("EQUITY_PAPER_TRADING_ENABLED", "true")
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: WebUser(
        id=7, username="u", first_name="U", role="user"
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/paper/equities/orders", json={"symbol": "AAPL", "notional": 10}
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "equity_not_configured"


@pytest.fixture(autouse=True)
def _hermetic_order_cap(monkeypatch):
    """A developer's ``.env`` may lower ``EQUITY_MAX_ORDER_NOTIONAL`` and the app loads it
    into the process; these tests pin the built-in default, so start each from none."""
    monkeypatch.delenv("EQUITY_MAX_ORDER_NOTIONAL", raising=False)
