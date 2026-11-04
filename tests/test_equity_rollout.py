import re

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient

from condor.equity_observability import EquityMetrics, equity_correlation_id
from condor.web.equity_rollout import equity_rollout_tap
from condor.web.models import WebUser


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("EQUITY_MARKETS_ENABLED", raising=False)
    monkeypatch.delenv("EQUITY_PAPER_TRADING_ENABLED", raising=False)
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"]
    )
    app.middleware("http")(equity_rollout_tap)

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
    def handler(path):
        return {"path": path, "correlation_id": equity_correlation_id.get()}

    return TestClient(app)


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/equities",
        "/api/v1/equities/AAPL/quote",
        "/api/v1/paper/equities/orders",
        "/api/v1/paper/equities/orders/preview",
    ],
)
def test_disabled_by_default_before_handlers_run(client, url):
    response = client.post(url) if "/paper/" in url else client.get(url)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "equity_feature_disabled"


@pytest.mark.parametrize("value", ["false", "1", "yes", "enabled", "garbage", ""])
def test_malformed_flag_cannot_enable_submissions(client, monkeypatch, value):
    monkeypatch.setenv("EQUITY_MARKETS_ENABLED", "true")
    monkeypatch.setenv("EQUITY_PAPER_TRADING_ENABLED", value)
    assert client.post("/api/v1/paper/equities/orders").status_code == 503


def test_research_does_not_enable_execution(client, monkeypatch):
    monkeypatch.setenv("EQUITY_MARKETS_ENABLED", "true")
    assert client.get("/api/v1/equities/AAPL/quote").status_code == 200
    assert client.post("/api/v1/paper/equities/orders").status_code == 503


def test_execution_requires_both_flags(client, monkeypatch):
    monkeypatch.setenv("EQUITY_PAPER_TRADING_ENABLED", "true")
    assert client.post("/api/v1/paper/equities/orders").status_code == 503
    monkeypatch.setenv("EQUITY_MARKETS_ENABLED", "true")
    assert client.post("/api/v1/paper/equities/orders").status_code == 200
    monkeypatch.setenv("EQUITY_MARKETS_ENABLED", "false")
    assert client.post("/api/v1/paper/equities/orders").status_code == 503


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/paper/equities/account",
        "/api/v1/paper/equities/positions",
        "/api/v1/paper/equities/orders",
    ],
)
def test_rollback_preserves_account_reads_and_reconciliation(client, url):
    assert client.get(url).status_code == 200


def test_rollback_preserves_cancellation(client):
    assert client.delete("/api/v1/paper/equities/orders/order-1").status_code == 200


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/equities-other",
        "/api/v1/paper/equities-other",
        "/api/v1/servers/crypto/orders",
        "/api/v1/settings/data-providers/equities",
    ],
)
def test_gate_does_not_match_unrelated_routes(client, url):
    assert client.post(url).status_code == 200


def test_correlation_ids_are_generated_and_logs_exclude_user_data(client, caplog):
    with caplog.at_level("INFO", logger="condor.web.equity_rollout"):
        one = client.get(
            "/api/v1/equities/secret-symbol?api_key=secret-key",
            headers={"X-Correlation-ID": "secret-header"},
        )
        two = client.get("/api/v1/equities/AAPL/quote")
    assert re.fullmatch("[a-f0-9]{32}", one.headers["X-Correlation-ID"])
    assert one.headers["X-Correlation-ID"] != two.headers["X-Correlation-ID"]
    records = [
        r.getMessage() for r in caplog.records if r.name == "condor.web.equity_rollout"
    ]
    assert records
    assert all("secret" not in record for record in records)


def test_disabled_feature_still_allows_cors_preflight(client):
    response = client.options(
        "/api/v1/equities/AAPL/quote",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_context_reaches_handlers_and_does_not_leak_into_other_requests(
    client, monkeypatch
):
    monkeypatch.setenv("EQUITY_MARKETS_ENABLED", "true")
    response = client.get("/api/v1/equities/AAPL/quote")
    assert response.json()["correlation_id"] == response.headers["X-Correlation-ID"]
    assert client.get("/api/v1/servers").json()["correlation_id"] is None
    assert equity_correlation_id.get() is None


@pytest.fixture
def diagnostics(monkeypatch):
    from condor.web.app import create_app
    from condor.web.auth import get_current_user
    from condor.web.routes import equity_diagnostics

    metrics = EquityMetrics()
    monkeypatch.setattr(equity_diagnostics, "equity_metrics", metrics)
    monkeypatch.setenv("EQUITY_MARKETS_ENABLED", "false")
    app = create_app()
    return app, get_current_user


def test_diagnostics_require_authentication(diagnostics):
    app, _ = diagnostics
    assert TestClient(app).get(
        "/api/v1/settings/data-providers/equities"
    ).status_code in (401, 403)


def test_real_application_installs_rollout_gate(diagnostics):
    app, _ = diagnostics
    response = TestClient(app).post("/api/v1/paper/equities/orders")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "equity_feature_disabled"


def test_diagnostics_require_admin(diagnostics, monkeypatch):
    from types import SimpleNamespace

    from condor.web import auth

    app, user_dependency = diagnostics
    app.dependency_overrides[user_dependency] = lambda: WebUser(
        id=7, username="u", first_name="U", role="user"
    )
    monkeypatch.setattr(
        auth,
        "get_config_manager",
        lambda: SimpleNamespace(
            is_admin=lambda _: False, get_user_role=lambda _: "user"
        ),
    )
    assert (
        TestClient(app).get("/api/v1/settings/data-providers/equities").status_code
        == 403
    )


def test_diagnostics_are_local_and_do_not_claim_health_without_samples(
    diagnostics, monkeypatch
):
    from types import SimpleNamespace

    from condor.web import auth

    app, user_dependency = diagnostics
    app.dependency_overrides[user_dependency] = lambda: WebUser(
        id=7, username="u", first_name="U", role="admin"
    )
    monkeypatch.setattr(
        auth, "get_config_manager", lambda: SimpleNamespace(is_admin=lambda _: True)
    )
    response = TestClient(app).get("/api/v1/settings/data-providers/equities")
    assert response.status_code == 200
    body = response.json()
    assert body["environment"] == "paper"
    assert body["rollout"]["markets_enabled"] is False
    assert body["provider_observed"] is False
    assert not any(body["metrics"]["counters"].values())
