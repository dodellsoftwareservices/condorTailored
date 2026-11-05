"""Two-factor login (TOTP) and the cookie/loopback-bearer auth split.

Three layers, each pinned separately:

* ``ConfigManager``'s storage (``enroll_totp``/``confirm_totp``/``disable_totp``/
  ``verify_totp_or_recovery``) — the actual secret handling, independent of any
  route.
* ``routes/auth.py``'s login gate — a user with 2FA enrolled cannot mint a
  session without a correct code, and the session cookie is what actually
  carries the result once they do.
* ``web/auth.py``'s ``get_current_user`` — the browser authenticates via the
  cookie; a bearer header is accepted only from loopback (the MCP subprocess
  and shift runner, both self-minting JWTs and calling 127.0.0.1 exclusively).
"""

import asyncio

import pyotp
import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

import config_manager as cm_module
from condor.web import auth as web_auth
from condor.web.app import create_app
from config_manager import ConfigManager
from utils import config as app_config

ADMIN_ID = 1001


@pytest.fixture
def cm(tmp_path, monkeypatch):
    """A real ConfigManager on a throwaway config.yml — persistence included."""
    monkeypatch.chdir(tmp_path)
    manager = ConfigManager(config_path=str(tmp_path / "config.yml"))
    manager._data["users"] = {
        ADMIN_ID: {"user_id": ADMIN_ID, "username": "root", "role": "admin"},
    }
    manager._data["user_preferences"] = {}
    manager._audit_log = []
    manager._save_config()
    for module in (cm_module, web_auth):
        monkeypatch.setattr(module, "get_config_manager", lambda: manager)
    return manager


# ── ConfigManager storage ──


def test_a_fresh_user_has_no_2fa(cm):
    assert cm.has_2fa_enabled(ADMIN_ID) is False
    assert cm.get_totp_secret(ADMIN_ID) is None


def test_enrolling_does_not_enable_2fa_until_confirmed(cm):
    secret, codes = cm.enroll_totp(ADMIN_ID)
    assert len(codes) == 8
    assert cm.get_totp_secret(ADMIN_ID) == secret
    assert cm.has_2fa_enabled(ADMIN_ID) is False


def test_confirming_with_the_wrong_code_does_not_enable_it(cm):
    cm.enroll_totp(ADMIN_ID)
    assert cm.confirm_totp(ADMIN_ID, "000000") is False
    assert cm.has_2fa_enabled(ADMIN_ID) is False


def test_confirming_with_a_real_code_enables_it(cm):
    secret, _ = cm.enroll_totp(ADMIN_ID)
    code = pyotp.TOTP(secret).now()
    assert cm.confirm_totp(ADMIN_ID, code) is True
    assert cm.has_2fa_enabled(ADMIN_ID) is True


def test_verify_accepts_a_live_totp_code(cm):
    secret, _ = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())

    assert cm.verify_totp_or_recovery(ADMIN_ID, pyotp.TOTP(secret).now()) is True


def test_verify_rejects_a_wrong_code(cm):
    secret, _ = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())

    assert cm.verify_totp_or_recovery(ADMIN_ID, "000000") is False


def test_a_recovery_code_works_once_and_is_then_consumed(cm):
    secret, codes = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())

    assert cm.verify_totp_or_recovery(ADMIN_ID, codes[0]) is True
    # Replaying the same code must fail — it was consumed on first use.
    assert cm.verify_totp_or_recovery(ADMIN_ID, codes[0]) is False
    # The other seven are untouched.
    assert cm.verify_totp_or_recovery(ADMIN_ID, codes[1]) is True


def test_verify_is_a_noop_when_2fa_was_never_enabled(cm):
    """A code that happens to match nothing must not be treated as a pass —
    there is nothing to pass when 2FA was never turned on."""
    assert cm.verify_totp_or_recovery(ADMIN_ID, "000000") is False


def test_disable_forgets_the_secret_and_recovery_codes_entirely(cm):
    secret, codes = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())

    assert cm.disable_totp(ADMIN_ID, actor_id=ADMIN_ID) is True
    assert cm.has_2fa_enabled(ADMIN_ID) is False
    assert cm.get_totp_secret(ADMIN_ID) is None
    assert cm.verify_totp_or_recovery(ADMIN_ID, codes[0]) is False


def test_the_2fa_preference_keys_are_reserved_from_bulk_updates(cm):
    """SEC-250's guard: a bulk ``/auth/preferences`` merge must never be able
    to hand a seat its own 2FA state, the same way it cannot for code_run."""
    with pytest.raises(ValueError):
        cm.set_user_preferences(ADMIN_ID, {cm_module.WEB_2FA_ENABLED_PREF: True})


# ── The login gate (routes/auth.py) ──


@pytest.fixture
def client(cm, monkeypatch):
    monkeypatch.setenv("WEB_JWT_SECRET", "test-secret-for-2fa")
    monkeypatch.setattr("condor.web.routes.auth.get_config_manager", lambda: cm)
    monkeypatch.setattr(app_config, "LOCAL_MODE", True)
    monkeypatch.setattr(app_config, "LOCAL_USER_ID", ADMIN_ID)
    # local-login's dashboard is reached at 127.0.0.1 in practice, and
    # get_current_user's loopback-only bearer fallback keys off
    # request.client.host — TestClient defaults that to "testclient".
    with TestClient(create_app(), client=("127.0.0.1", 50000)) as test_client:
        yield test_client


def test_local_login_succeeds_with_no_2fa_enrolled(client):
    res = client.post("/api/v1/auth/local-login")
    assert res.status_code == 200
    assert res.json()["user"]["id"] == ADMIN_ID


def test_local_login_refuses_with_no_code_once_2fa_is_enabled(cm, client):
    secret, _ = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())

    res = client.post("/api/v1/auth/local-login")
    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "2fa_required"


def test_local_login_refuses_a_wrong_code(cm, client):
    secret, _ = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())

    res = client.post("/api/v1/auth/local-login", json={"otp": "000000"})
    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "2fa_invalid"


def test_local_login_succeeds_with_the_right_code_and_sets_a_session_cookie(cm, client):
    secret, _ = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())

    res = client.post(
        "/api/v1/auth/local-login", json={"otp": pyotp.TOTP(secret).now()}
    )
    assert res.status_code == 200
    assert web_auth.SESSION_COOKIE_NAME in res.cookies


def test_a_recovery_code_also_clears_the_login_gate(cm, client):
    secret, codes = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())

    res = client.post("/api/v1/auth/local-login", json={"otp": codes[0]})
    assert res.status_code == 200


def test_the_session_cookie_alone_authenticates_a_later_request(cm, client):
    """Once logged in, the browser never has to present a bearer token again
    — the cookie ``client`` now holds is enough for ``get_current_user``."""
    secret, _ = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())
    client.post("/api/v1/auth/local-login", json={"otp": pyotp.TOTP(secret).now()})

    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["id"] == ADMIN_ID


def test_session_endpoint_returns_only_a_non_secret_session_identity(cm, client):
    secret, _ = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())
    client.post("/api/v1/auth/local-login", json={"otp": pyotp.TOTP(secret).now()})

    res = client.get("/api/v1/auth/session")
    assert res.status_code == 200
    body = res.json()
    assert body["user"]["id"] == ADMIN_ID
    assert web_auth.decode_jwt(body["token"]) is None
    assert (
        body["token"]
        == web_auth.decode_jwt(client.cookies[web_auth.SESSION_COOKIE_NAME])["jti"]
    )


def test_logout_clears_the_cookie_and_me_then_fails(cm, client):
    secret, _ = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())
    client.post("/api/v1/auth/local-login", json={"otp": pyotp.TOTP(secret).now()})

    assert client.get("/api/v1/auth/me").status_code == 200
    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_logout_revokes_the_token_itself_not_just_the_cookie(cm, client):
    """A copy of the token taken before logout (a stolen cookie, a shared
    machine) must stop working too — not just this browser's own cookie."""
    secret, _ = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())
    client.post("/api/v1/auth/local-login", json={"otp": pyotp.TOTP(secret).now()})
    stolen_token = client.cookies[web_auth.SESSION_COOKIE_NAME]

    client.post("/api/v1/auth/logout")

    replay = client.get(
        "/api/v1/auth/me",
        headers={"Cookie": f"{web_auth.SESSION_COOKIE_NAME}={stolen_token}"},
    )
    assert replay.status_code == 401


# ── The 2FA self-service endpoints ──


def test_enroll_then_confirm_then_status_round_trip(cm, client):
    secret, _ = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())
    client.post("/api/v1/auth/local-login", json={"otp": pyotp.TOTP(secret).now()})

    status_res = client.get("/api/v1/auth/2fa/status")
    assert status_res.json() == {"enabled": True}


def test_enroll_endpoint_returns_a_usable_secret_and_recovery_codes(cm, client):
    """Logged in with no 2FA yet, enrollment via the HTTP endpoint produces a
    secret that the confirm endpoint will actually accept."""
    client.post("/api/v1/auth/local-login")

    enroll = client.post("/api/v1/auth/2fa/enroll")
    assert enroll.status_code == 200
    body = enroll.json()
    assert len(body["recovery_codes"]) == 8
    assert body["otp_uri"].startswith("otpauth://totp/")

    code = pyotp.TOTP(body["secret"]).now()
    confirm = client.post("/api/v1/auth/2fa/confirm", json={"code": code})
    assert confirm.status_code == 200
    assert confirm.json() == {"enabled": True}
    assert cm.has_2fa_enabled(ADMIN_ID) is True


def test_disable_endpoint_requires_a_correct_code(cm, client):
    secret, _ = cm.enroll_totp(ADMIN_ID)
    cm.confirm_totp(ADMIN_ID, pyotp.TOTP(secret).now())
    client.post("/api/v1/auth/local-login", json={"otp": pyotp.TOTP(secret).now()})

    bad = client.post("/api/v1/auth/2fa/disable", json={"code": "000000"})
    assert bad.status_code == 401
    assert cm.has_2fa_enabled(ADMIN_ID) is True

    good = client.post(
        "/api/v1/auth/2fa/disable", json={"code": pyotp.TOTP(secret).now()}
    )
    assert good.status_code == 200
    assert cm.has_2fa_enabled(ADMIN_ID) is False


# ── get_current_user: cookie vs loopback-only bearer (condor/web/auth.py) ──


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, cookies=None, host="127.0.0.1"):
        self.cookies = cookies or {}
        self.client = _FakeClient(host)


def test_a_bearer_token_from_a_non_loopback_host_is_refused(monkeypatch, cm):
    from fastapi.security import HTTPAuthorizationCredentials

    monkeypatch.setattr(web_auth, "_jwt_secret", lambda: "test-secret")
    token = web_auth.create_jwt(ADMIN_ID, role="admin")

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            web_auth.get_current_user(
                _FakeRequest(host="203.0.113.7"),
                HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
            )
        )
    assert excinfo.value.status_code == 401


def test_a_bearer_token_from_loopback_is_accepted(monkeypatch, cm):
    from fastapi.security import HTTPAuthorizationCredentials

    monkeypatch.setattr(web_auth, "_jwt_secret", lambda: "test-secret")
    token = web_auth.create_jwt(ADMIN_ID, role="admin")

    user = asyncio.run(
        web_auth.get_current_user(
            _FakeRequest(host="127.0.0.1"),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        )
    )
    assert user.id == ADMIN_ID


def test_the_cookie_is_accepted_from_any_host_no_bearer_needed(monkeypatch, cm):
    monkeypatch.setattr(web_auth, "_jwt_secret", lambda: "test-secret")
    token = web_auth.create_jwt(ADMIN_ID, role="admin")

    user = asyncio.run(
        web_auth.get_current_user(
            _FakeRequest(
                cookies={web_auth.SESSION_COOKIE_NAME: token}, host="203.0.113.7"
            ),
            None,
        )
    )
    assert user.id == ADMIN_ID


def test_no_cookie_and_no_bearer_is_refused(monkeypatch, cm):
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(web_auth.get_current_user(_FakeRequest(), None))
    assert excinfo.value.status_code == 401


# ── WebSocket auth: the cookie fallback in extract_ws_token ──


class _FakeWebSocket:
    def __init__(self, cookies=None, subprotocols=None):
        self.cookies = cookies or {}
        self.scope = {"subprotocols": subprotocols or []}


def test_ws_token_prefers_the_subprotocol_when_present():
    ws = _FakeWebSocket(subprotocols=[web_auth.WS_AUTH_SUBPROTOCOL, "tok-a"])
    token, accept = web_auth.extract_ws_token(ws, query_token="tok-b")
    assert (token, accept) == ("tok-a", web_auth.WS_AUTH_SUBPROTOCOL)


def test_ws_token_refuses_the_legacy_query_param():
    ws = _FakeWebSocket()
    token, accept = web_auth.extract_ws_token(ws, query_token="tok-b")
    assert (token, accept) == (None, None)


def test_ws_token_falls_back_to_the_session_cookie():
    ws = _FakeWebSocket(cookies={web_auth.SESSION_COOKIE_NAME: "tok-c"})
    token, accept = web_auth.extract_ws_token(ws, query_token=None)
    assert (token, accept) == ("tok-c", None)
