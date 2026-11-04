"""Documentation and schemas must respect login, logout, and proxy prefixes."""

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from condor.web import auth
from condor.web.app import create_app
from config_manager import UserRole

PATHS = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEB_JWT_SECRET", "docs-test-only-signing-secret")
    monkeypatch.setattr(
        auth,
        "get_config_manager",
        lambda: SimpleNamespace(get_user_role=lambda user_id: UserRole.USER),
    )
    return TestClient(create_app())


@pytest.mark.parametrize("path", PATHS)
def test_documentation_requires_a_session(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", PATHS)
def test_documentation_accepts_cookie_and_rejects_revoked_session(client, path):
    token = auth.create_jwt(user_id=123)
    client.cookies.set(auth.SESSION_COOKIE_NAME, token)
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    if path == "/openapi.json":
        assert "/api/v1/auth/me" in response.json()["paths"]
        assert "/docs" not in response.json()["paths"]
    auth.revoke_jwt(token)
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_schema_links_preserve_proxy_prefix(monkeypatch, path):
    app = create_app()
    app.dependency_overrides[auth.get_current_user] = lambda: object()
    client = TestClient(app, root_path="/griffin")
    response = client.get(path)
    assert response.status_code == 200
    assert "/griffin/openapi.json" in response.text


@pytest.mark.parametrize("include_prefix", [True, False])
def test_schema_try_it_out_uses_proxy_prefix_without_polluting_other_requests(
    include_prefix,
):
    app = create_app()
    app.root_path_in_servers = include_prefix
    app.dependency_overrides[auth.get_current_user] = lambda: object()
    proxied = TestClient(app, root_path="/griffin").get("/openapi.json")
    servers = proxied.json().get("servers", [])
    assert ({"url": "/griffin"} in servers) is include_prefix
    direct = TestClient(app).get("/openapi.json")
    assert {"url": "/griffin"} not in direct.json().get("servers", [])
