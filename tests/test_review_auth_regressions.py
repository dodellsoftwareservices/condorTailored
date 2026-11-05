"""Cross-route/browser contracts missed by the October 31 remediation tests."""

import importlib

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config_manager as cm_module
from condor.web import auth, login_rate_limit, session_revocations
from condor.web.routes import admin as admin_routes
from condor.web.routes import auth as auth_routes


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cm = cm_module.ConfigManager(str(tmp_path / "config.yml"))
    cm._data["users"] = {
        1: {"user_id": 1, "role": "admin"},
        2: {"user_id": 2, "role": "user"},
        3: {"user_id": 3, "role": "admin"},
    }
    cm._data["user_preferences"] = {}
    for module in (cm_module, auth, auth_routes, admin_routes):
        monkeypatch.setattr(module, "get_config_manager", lambda: cm)
    monkeypatch.setenv("WEB_JWT_SECRET", "review-regression-test-key")
    monkeypatch.setattr(auth_routes.app_config, "LOCAL_MODE", True)
    monkeypatch.setattr(auth_routes.app_config, "LOCAL_USER_ID", 1)
    login_rate_limit._failures.clear()
    auth._pending_login_tokens.clear()
    app = FastAPI()
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.include_router(admin_routes.router, prefix="/api/v1")
    with TestClient(app) as client:
        yield cm, client, app
    login_rate_limit._failures.clear()
    auth._pending_login_tokens.clear()


def enable(cm, user_id):
    secret, codes = cm.enroll_totp(user_id)
    assert cm.confirm_totp(user_id, pyotp.TOTP(secret).now())
    return secret, codes


@pytest.mark.parametrize("method", ["get", "patch"])
def test_preferences_never_return_authenticator_material(setup, method):
    cm, client, _ = setup
    secret, _ = enable(cm, 2)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_jwt(2))
    kwargs = {"json": {"updates": {"theme": "dark"}}} if method == "patch" else {}
    response = getattr(client, method)("/api/v1/auth/preferences", **kwargs)
    assert response.status_code == 200
    assert secret not in response.text
    assert cm_module.WEB_2FA_SECRET_PREF not in response.json()
    assert cm_module.WEB_2FA_RECOVERY_PREF not in response.json()
    assert cm.has_2fa_enabled(2)


def test_telegram_browser_challenge_retries_then_consumes_once(setup):
    cm, client, _ = setup
    secret, _ = enable(cm, 2)
    token = auth.create_login_token(2)

    def submit(**extra):
        return client.post("/api/v1/auth/token-login", json={"token": token, **extra})

    assert submit().json()["detail"]["code"] == "2fa_required"
    assert submit(otp="not-a-code").json()["detail"]["code"] == "2fa_invalid"
    response = submit(otp=pyotp.TOTP(secret).now())
    assert response.status_code == 200
    assert auth.decode_jwt(response.json()["token"]) is None
    assert client.get("/api/v1/auth/me").json()["id"] == 2
    assert submit(otp=pyotp.TOTP(secret).now()).status_code == 401


def test_pending_challenge_expires_before_second_factor(setup):
    cm, client, _ = setup
    secret, _ = enable(cm, 2)
    token = auth.create_login_token(2)
    client.post("/api/v1/auth/token-login", json={"token": token})
    auth._pending_login_tokens[token]["created_at"] -= auth._LOGIN_TOKEN_TTL + 1
    response = client.post(
        "/api/v1/auth/token-login",
        json={"token": token, "otp": pyotp.TOTP(secret).now()},
    )
    assert response.status_code == 401
    assert token not in auth._pending_login_tokens


def test_challenge_lockout_survives_changing_source_address(setup):
    cm, client, app = setup
    enable(cm, 2)
    token = auth.create_login_token(2)
    for _ in range(login_rate_limit.MAX_ATTEMPTS):
        assert (
            client.post(
                "/api/v1/auth/token-login", json={"token": token, "otp": "bad"}
            ).status_code
            == 401
        )
    with TestClient(app, client=("198.51.100.7", 9000)) as other:
        assert (
            other.post(
                "/api/v1/auth/token-login", json={"token": token, "otp": "bad"}
            ).status_code
            == 429
        )


@pytest.mark.parametrize("target", [1, 3])
def test_admin_recovery_cannot_remove_an_admin_factor(setup, target):
    cm, client, _ = setup
    secret, _ = enable(cm, target)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_jwt(1))
    response = client.delete(f"/api/v1/admin/people/{target}/2fa")
    assert response.status_code == 403
    assert cm.get_totp_secret(target) == secret
    assert cm.has_2fa_enabled(target)


def test_session_reads_never_mint_a_replayable_credential_or_extend_expiry(setup):
    _, client, _ = setup
    login = client.post("/api/v1/auth/local-login")
    credential = client.cookies[auth.SESSION_COOKIE_NAME]
    identity = login.json()["token"]
    assert identity == auth.decode_jwt(credential)["jti"]
    for _ in range(3):
        response = client.get("/api/v1/auth/session")
        assert response.status_code == 200
        assert response.json()["token"] == identity
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
    client.post("/api/v1/auth/logout")
    importlib.reload(session_revocations)  # restart must not undo revocation
    for stolen in (identity, credential):
        assert (
            client.get(
                "/api/v1/auth/me",
                headers={"Cookie": f"{auth.SESSION_COOKIE_NAME}={stolen}"},
            ).status_code
            == 401
        )


def test_revocation_does_not_end_a_separate_login(setup):
    _, client, _ = setup
    first = auth.create_jwt(2)
    other = auth.create_jwt(2)
    auth.revoke_jwt(first)
    importlib.reload(session_revocations)
    assert auth.decode_jwt(first) is None
    assert auth.decode_jwt(other)["sub"] == "2"


def test_unlinked_pre_migration_tokens_require_a_new_login(setup):
    token = auth.create_jwt(2)
    payload = auth.decode_jwt(token)
    payload.pop("v")
    old_token = auth.jwt.encode(payload, auth._jwt_secret(), algorithm="HS256")
    assert auth.decode_jwt(old_token) is None


def test_local_recovery_code_is_accepted_in_the_body_only(setup):
    cm, client, _ = setup
    _, codes = enable(cm, 1)
    assert client.post(f"/api/v1/auth/local-login?otp={codes[0]}").status_code == 401
    assert (
        client.post("/api/v1/auth/local-login", json={"otp": codes[0]}).status_code
        == 200
    )


def test_corrupt_revocation_store_fails_closed(setup):
    token = auth.create_jwt(2)
    session_revocations._path().parent.mkdir(parents=True, exist_ok=True)
    session_revocations._path().write_bytes(b"invalid database")
    assert auth.decode_jwt(token) is None


def test_cookie_websockets_reject_foreign_origins_and_close_after_logout(
    setup, monkeypatch
):
    from fastapi import WebSocketDisconnect

    from condor.web.routes import ws as routes
    from condor.web.ws_manager import WebSocketManager

    _, client, app = setup
    monkeypatch.setattr(routes, "get_ws_manager", lambda: WebSocketManager())
    app.include_router(routes.router, prefix="/api/v1")
    client.post("/api/v1/auth/local-login")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/v1/ws", headers={"origin": "http://evil.example"}
        ):
            pytest.fail("Cross-origin socket authenticated")
    with client.websocket_connect(
        "/api/v1/ws", headers={"origin": "http://testserver"}
    ) as socket:
        assert client.post("/api/v1/auth/logout").status_code == 200
        socket.send_json({"action": "subscribe", "channel": "portfolio:local"})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
