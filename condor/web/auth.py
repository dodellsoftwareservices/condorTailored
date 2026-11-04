from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from typing import Optional

from fastapi import Depends, HTTPException, Query, Request, Response, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from condor.web import session_revocations
from condor.web.models import WebUser
from config_manager import ServerPermission, UserRole, get_config_manager

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"
_TOKEN_EXPIRE_SECONDS = 86400  # 24 hours
_LOGIN_TOKEN_TTL = 300  # one-time login tokens valid for 5 minutes

# ``auto_error=False``: a missing header must fall through to the cookie
# check in ``get_current_user`` rather than HTTPBearer raising 403 on its own
# before that check ever runs.
_bearer_scheme = HTTPBearer(auto_error=False)

# The browser's session lives in an HttpOnly cookie, never in page JS/
# localStorage — a stolen token via XSS can no longer be read out and reused
# from elsewhere. ``Lax`` is what stops a *different* site's cross-origin
# POST from riding along with this cookie (that is what SameSite exists to
# do — the attacker's flow, not any flow this app offers on purpose); it
# holds up here because every real request this dashboard makes is same-site
# to begin with, so Lax costs this app nothing while still denying the
# cookie to anyone else's form or fetch. ``create_app``'s Origin check on
# mutating routes is the second, independent layer — this one is not meant
# to stand alone.
SESSION_COOKIE_NAME = "condor_session"

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# In-memory store: token_str -> {user_id, username, first_name, created_at}
_pending_login_tokens: dict[str, dict] = {}


def _jwt_secret() -> str:
    """Return the secret used to sign/verify web session JWTs.

    Prefers the dedicated ``WEB_JWT_SECRET`` environment variable so the secret
    can be shared across instances or rotated on demand. When it is not set, a
    strong random secret is generated once and persisted to ``config.yml`` (via
    :class:`ConfigManager`), so the dashboard is secure by default with no
    operator configuration and web sessions survive restarts. The secret is
    never derived from ``TELEGRAM_TOKEN`` — that coupled the two trust domains
    and broke when the token was empty.
    """
    web_secret = os.getenv("WEB_JWT_SECRET")
    if web_secret:
        return web_secret
    return get_config_manager().get_or_create_web_jwt_secret()


# ── JWT helpers ──


def create_jwt(
    user_id: int, username: str = "", first_name: str = "", role: str = "user"
) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "first_name": first_name,
        "role": role,
        "jti": secrets.token_hex(16),
        # Retire pre-migration credentials, including unlinked JWTs previously
        # minted by /auth/session. Users sign in once after this upgrade.
        "v": 1,
        "exp": int(time.time()) + _TOKEN_EXPIRE_SECONDS,
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_ALGORITHM)


def decode_jwt(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[_ALGORITHM])
    except JWTError:
        return None
    jti = payload.get("jti")
    if payload.get("v") != 1 or not isinstance(jti, str) or not jti:
        return None
    if session_revocations.is_revoked(jti):
        return None
    return payload


def revoke_jwt(token: str) -> None:
    """Revoke one token server-side, ahead of its natural expiry.

    ``logout`` clearing the cookie is, on its own, a no-op against the server
    — the JWT itself stays valid for the rest of its 24h life, so anyone who
    copied it earlier (a shared machine, a proxy log, an XSS payload) keeps
    using it regardless of whether the real owner logged out. This closes
    that: the token decodes once here (unrevoked, since it has not happened
    yet), its ``jti`` is recorded until its own ``exp``, and every
    ``decode_jwt`` call from this point on refuses it immediately. A token
    with no ``jti`` (none should exist post-rollout) or already expired is a
    silent no-op — there is nothing left to revoke.
    """
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[_ALGORITHM])
    except JWTError:
        return
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not jti or not exp:
        return
    session_revocations.revoke(jti, int(exp))


# ── Session cookie (browser) ──


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    """Set the browser's session cookie after a successful login.

    ``secure`` is conditional on the request having actually arrived over
    HTTPS rather than hardcoded True: local mode serves plain HTTP on
    ``127.0.0.1``, and a cookie marked Secure there would silently never be
    sent back on the next request, breaking login with no visible error.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=_TOKEN_EXPIRE_SECONDS,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


# ── WebSocket auth (browser cookie, service-client subprotocol) ──

# Sentinel subprotocol that marks a JWT-carrying handshake. The client offers
# ``[WS_AUTH_SUBPROTOCOL, <jwt>]`` as Sec-WebSocket-Protocol values so the token
# stays out of the URL (and thus out of proxy/access logs and browser history).
WS_AUTH_SUBPROTOCOL = "condor-jwt"


def extract_ws_token(
    ws: WebSocket, query_token: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Extract the auth JWT from a WebSocket handshake.

    Browsers send the HttpOnly session cookie automatically. Service clients
    can send ``[WS_AUTH_SUBPROTOCOL, <jwt>]`` as subprotocol values. A browser
    Origin must match this endpoint or the configured CORS origins. URL token
    parameters are ignored because access logs retain them.

    Returns ``(token, accept_subprotocol)``. ``accept_subprotocol`` is the
    sentinel (never the token) when the subprotocol path is used and must be
    echoed back in ``ws.accept(subprotocol=...)`` or the browser rejects the
    handshake; it is ``None`` for cookie authentication.
    """
    origin = getattr(ws, "headers", {}).get("origin")
    if origin:
        from condor.web.app import _build_cors_origins

        scheme = "https" if ws.url.scheme == "wss" else "http"
        if (
            origin != f"{scheme}://{ws.url.netloc}"
            and origin not in _build_cors_origins()
        ):
            return None, None
    subprotocols = ws.scope.get("subprotocols", [])
    if (
        subprotocols
        and subprotocols[0] == WS_AUTH_SUBPROTOCOL
        and len(subprotocols) >= 2
    ):
        return subprotocols[1], WS_AUTH_SUBPROTOCOL
    # URL credentials are no longer accepted: access logs retain query strings.
    return ws.cookies.get(SESSION_COOKIE_NAME), None


async def watch_websocket_session(ws: WebSocket, token: str, validate) -> None:
    """Close idle sockets after expiry/logout, including across processes."""
    try:
        while True:
            await asyncio.sleep(2)
            if validate(token) is None:
                await ws.close(code=4001, reason="Session expired")
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Session watcher ended after WebSocket closed", exc_info=True)


# ── FastAPI dependency ──


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> WebUser:
    """FastAPI dependency that extracts and validates the session JWT.

    The browser authenticates via the HttpOnly session cookie set by
    ``token_login``/``local_login`` — the token never touches page JS, so an
    XSS payload can no longer read it out of ``localStorage`` and replay it.
    A bare ``Authorization: Bearer`` header is still accepted, but only from
    loopback: the only two callers that ever send one are the MCP subprocess
    (``mcp_servers/condor/condor_client.py``) and the shift runner
    (``condor/shift.py``), both of which mint their own JWT with
    ``create_jwt`` and call this server at ``127.0.0.1`` exclusively.
    Narrowing bearer to loopback keeps that path working without leaving a
    second, equally-valid way for a leaked browser token to authenticate from
    anywhere else on the network.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token is None and credentials is not None:
        client = request.client
        if client and client.host in _LOOPBACK_HOSTS:
            token = credentials.credentials

    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    payload = decode_jwt(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        )

    user_id = int(payload["sub"])
    cm = get_config_manager()
    role = cm.get_user_role(user_id)

    if role not in (UserRole.USER, UserRole.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Access denied"
        )

    return WebUser(
        id=user_id,
        username=payload.get("username", ""),
        first_name=payload.get("first_name", ""),
        role=role.value,
    )


# ── Role gates ──


def require_admin(user: WebUser) -> None:
    """Refuse anyone who is not a Condor admin.

    The role is re-read from the ConfigManager rather than trusted from the JWT
    claim, so demoting a user takes effect on their next request rather than
    when their 24h token expires.

    Lives here, beside the server-scoped gates below, because it is the same
    kind of thing: the one implementation of a permission line that more than
    one router needs. It began as ``_require_admin`` in ``routes/admin.py`` and
    was promoted when ``routes/updates.py`` needed the same line — importing a
    private name across modules is how a gate quietly grows a second, drifting
    copy.
    """
    if not get_config_manager().is_admin(user.id):
        # Name the role rather than the screen. Local mode admits both `user`
        # and `admin` at login while every admin route demands `admin`, so a
        # hand-edited config.yml produces an install that boots, logs in, and
        # silently has no Updates tab — with nothing anywhere saying why.
        # Best effort: naming the role is a courtesy, and a gate must never
        # fail open (or crash) because the courtesy could not be paid.
        try:
            found = get_config_manager().get_user_role(user.id)
            # ``get_user_role`` hands back the enum, and interpolating that
            # renders "UserRole.USER" — a name that appears nowhere in
            # config.yml, so the reader cannot search for it and the value it
            # tells them to change looks nothing like the one on disk.
            role = str(getattr(found, "value", found) or "")
        except Exception:  # noqa: BLE001
            role = ""
        detail = "Admin access required"
        if role:
            detail += (
                f" — this user's role is '{role}'. Set it to 'admin' in config.yml."
            )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def report_owner_filter(user: WebUser) -> int | None:
    """Whose reports a listing may show: everyone's for admins, own otherwise.

    ``None`` disables the store's owner filter — the admin override the other
    server-data surfaces already grant (``_owner`` in conversations,
    ``_require_ownership`` in sessions). Anyone else is scoped to entries
    stamped with their own id; legacy entries with no owner are dropped for
    them (fail closed, SEC-196).

    Lives here rather than in ``routes/reports.py`` (SEC-593) because the
    per-routine, per-strategy and per-session report listings need the same
    line, and ``require_admin`` above records what happens when a gate is
    imported privately across route modules instead: it grows copies.
    """
    return None if get_config_manager().is_admin(user.id) else user.id


# ── Server-scoped access (SEC-147) ──


def check_server_access(user_id: int, server_name: str) -> None:
    """Raise ``403 No access`` unless ``user_id`` may use ``server_name``.

    Single implementation of the guard that used to be hand-copied into every
    server-scoped web endpoint. ``has_server_access`` defaults to
    :attr:`ServerPermission.TRADER`, which is the level every web call already
    required. Endpoints that need a *stronger* level (owner-only credential and
    server mutations in ``routes/settings.py``) still layer their own check on
    top of this one — this is the floor, never the ceiling.

    Endpoints whose server name arrives in the request **body** call this
    directly (a path/query dependency cannot see the body); endpoints that take
    it as a path or query parameter use the ``require_server_access*``
    dependencies below, which are thin wrappers over this function.
    """
    if not get_config_manager().has_server_access(user_id, server_name):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access")


def require_owner(cm, user_id: int, server_name: str) -> None:
    """Enforce the OWNER line on top of the TRADER floor every route already has.

    The rule (SEC-153, extended to the gateway by SEC-166, and to the gateway
    token list by SEC-207):

    * **Reading** a server's state — status, logs, wallet and network listings,
      configured connectors — needs TRADER. A shared trader has to be able to
      see what they are trading against, and to tell the owner when it is down.
    * **Trading** on a server needs TRADER. That is what the share is for.
    * **Mutating a server's configuration or its infrastructure** needs OWNER.
      Exchange credentials, the gateway container lifecycle, the private keys
      in its keystore, the RPC endpoints it dials and the entries on its token
      list are all the owner's machine, not a trading action — and each of them
      can break the owner's running bots for everyone else on the server.

    Admins keep the bypass they hold everywhere else in the web layer.

    Lives here rather than in ``routes/settings.py`` (its first home) because
    it is the ceiling to this module's floor, and a second hand-copied
    implementation in the next router that needs it is how the line drifts.
    """
    perm = cm.get_server_permission(user_id, server_name)
    if perm != ServerPermission.OWNER and not cm.is_admin(user_id):
        raise HTTPException(status_code=403, detail="Owner access required")


async def require_server_access(
    name: str, user: WebUser = Depends(get_current_user)
) -> WebUser:
    """Auth dependency for endpoints routed under ``/servers/{name}/...``.

    Drop-in replacement for ``Depends(get_current_user)``: it returns the same
    :class:`WebUser`, having first enforced access to the ``{name}`` path
    parameter. If the route has no ``{name}`` path parameter FastAPI treats it
    as a required *query* parameter and the request fails with 422 — the guard
    fails closed, never open.
    """
    check_server_access(user.id, name)
    return user


async def require_server_access_query(
    server: str = Query(...), user: WebUser = Depends(get_current_user)
) -> WebUser:
    """Same as :func:`require_server_access` for a ``?server=`` query param."""
    check_server_access(user.id, server)
    return user


# ── One-time login tokens (generated from Telegram /web command) ──


def _gc_expired_login_tokens(now: float) -> None:
    """Remove expired one-time login tokens from the in-memory store."""
    expired = [
        k
        for k, v in _pending_login_tokens.items()
        if now - v["created_at"] > _LOGIN_TOKEN_TTL
    ]
    for k in expired:
        _pending_login_tokens.pop(k, None)


def create_login_token(user_id: int, username: str = "", first_name: str = "") -> str:
    """Create a one-time login token for a Telegram user."""
    now = time.time()
    _gc_expired_login_tokens(now)

    token = secrets.token_urlsafe(32)
    _pending_login_tokens[token] = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "created_at": now,
    }
    return token


def redeem_login_token(token: str) -> Optional[dict]:
    """Redeem a one-time login token. Returns user info or None if invalid/expired.

    The security control is the token itself: 32 bytes of cryptographically
    random, single-use (popped on first lookup) data with a short TTL. A token
    cannot be brute-forced, and the user_id is only known *after* a valid token
    is presented, so there is no per-user threat to rate-limit. Garbage-collects
    expired tokens on every call (so unredeemed tokens do not leak memory).
    """
    now = time.time()
    # Sweep expired tokens up front so the store does not grow unbounded
    # even when tokens are never redeemed.
    _gc_expired_login_tokens(now)

    info = _pending_login_tokens.pop(token, None)
    if info is None:
        return None

    return info


def peek_login_token(token: str) -> Optional[dict]:
    """Inspect a pending login without consuming its second-factor challenge.

    The route verifies the factor synchronously, then redeems before yielding
    control, so concurrent requests cannot both complete the same login.
    """
    _gc_expired_login_tokens(time.time())
    info = _pending_login_tokens.get(token)
    return dict(info) if info is not None else None
