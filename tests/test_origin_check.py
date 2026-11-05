"""The Origin-check middleware (`condor/web/app.py`) — the second, independent
layer behind the session cookie's `SameSite=Lax` (system review 2026-10-31,
finding #6).

`SameSite=Lax` relies on the browser honouring it. This does not: it reads
the `Origin` header the browser itself attaches to the request, which a
page cannot spoof, and refuses a state-changing request whose Origin is
neither this exact host nor the explicit CORS allow-list. A cross-site form
POST sends an Origin and will never be either.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from condor.web.app import _origin_check


@pytest.fixture
def app():
    app = FastAPI()
    app.middleware("http")(_origin_check)

    @app.post("/mutate")
    async def mutate():
        return {"ok": True}

    @app.get("/read")
    async def read():
        return {"ok": True}

    return app


@pytest.fixture
def client(app):
    return TestClient(app, base_url="http://127.0.0.1:8088")


def test_a_request_with_no_origin_header_is_unaffected(client):
    """curl, server-to-server calls, the MCP bridge — none send Origin."""
    assert client.post("/mutate").status_code == 200


def test_a_same_origin_mutation_passes(client):
    res = client.post("/mutate", headers={"Origin": "http://127.0.0.1:8088"})
    assert res.status_code == 200


def test_a_cross_site_mutation_is_refused(client):
    res = client.post("/mutate", headers={"Origin": "http://evil.example"})
    assert res.status_code == 403


def test_the_vite_dev_server_origin_is_explicitly_allowed(client):
    """The one legitimate cross-origin caller — CORS already names it."""
    res = client.post("/mutate", headers={"Origin": "http://localhost:5173"})
    assert res.status_code == 200


def test_reads_are_never_checked(client):
    """GET never mutates by this app's own convention, so a foreign Origin
    on a read is not the attack this layer exists to stop."""
    res = client.get("/read", headers={"Origin": "http://evil.example"})
    assert res.status_code == 200


def test_a_web_url_origin_from_env_is_allowed(monkeypatch, app):
    """A Tailscale/VPS deployment's WEB_URL origin is the documented
    cross-origin exception CORS already carries — this reuses the same list
    rather than keeping a second one that could drift from it."""
    monkeypatch.setenv("WEB_URL", "http://griffin.tailxyz.ts.net:8088")
    client = TestClient(app, base_url="http://127.0.0.1:8088")

    res = client.post(
        "/mutate", headers={"Origin": "http://griffin.tailxyz.ts.net:8088"}
    )
    assert res.status_code == 200
