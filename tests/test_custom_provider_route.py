"""The admin gate on `POST /settings/custom-providers` (system review
2026-10-31, finding #5 — an SSRF/internal-scanner primitive).

`add_custom_provider` drives a real outbound GET to a caller-supplied
`base_url`, and loopback/RFC1918 targets are allowed by default (a local
Ollama/vLLM server is the common case for this feature, so
`CUSTOM_LLM_BLOCK_PRIVATE_URLS` is opt-in, not a default). Without an
admin-only gate, any approved seat had a working probe of the host's loopback
and Docker network — including hummingbot-api's own Basic-Auth port,
Postgres and EMQX. This pins that the route now refuses anyone who is not an
admin, before it ever reaches the network.
"""

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import condor.web.routes.settings as settings_routes
from condor.web.auth import get_current_user
from condor.web.models import WebUser

SEAT = WebUser(id=2, username="seat", first_name="S", role="user")
ADMIN = WebUser(id=3, username="admin", first_name="A", role="admin")


@pytest.fixture
def app():
    return FastAPI()


def as_user(app, user):
    app.dependency_overrides[get_current_user] = lambda: user
    app.include_router(settings_routes.router)
    return TestClient(app)


def test_a_non_admin_seat_is_refused_before_any_network_call(app, monkeypatch):
    called = False

    async def _fetch_models(*a, **kw):
        nonlocal called
        called = True
        return "http://example.com", ["model"]

    monkeypatch.setattr("condor.llm.custom_models.fetch_models", _fetch_models)
    # Real config, not mocked, would also refuse SEAT — mock explicitly so
    # this test does not depend on what happens to be in config.yml.
    monkeypatch.setattr(
        "condor.web.auth.get_config_manager",
        lambda: type(
            "CM", (), {"is_admin": staticmethod(lambda uid: uid == ADMIN.id)}
        )(),
    )

    res = as_user(app, SEAT).post(
        "/settings/custom-providers",
        json={"base_url": "http://169.254.169.254/latest/meta-data", "api_key": ""},
    )

    assert res.status_code == 403
    assert called is False, "the SSRF-capable fetch must never run for a non-admin"


def test_an_admin_reaches_past_the_gate(app, monkeypatch):
    """Proves the gate is `require_admin`, not an accidental blanket 403 —
    an admin's request must reach the real validation logic beyond it."""
    from condor.llm.custom_models import CustomProviderError

    async def _fetch_models(*a, **kw):
        raise CustomProviderError("simulated unreachable")

    monkeypatch.setattr("condor.llm.custom_models.fetch_models", _fetch_models)
    # `require_admin` re-reads the role from ConfigManager rather than
    # trusting the WebUser claim — fake just enough of it to answer that.
    monkeypatch.setattr(
        "condor.web.auth.get_config_manager",
        lambda: type(
            "CM", (), {"is_admin": staticmethod(lambda uid: uid == ADMIN.id)}
        )(),
    )

    res = as_user(app, ADMIN).post(
        "/settings/custom-providers",
        json={"base_url": "http://10.0.0.5:11434", "api_key": ""},
    )

    # 502, not 403: the admin got past the gate and failed downstream instead.
    assert res.status_code == 502
