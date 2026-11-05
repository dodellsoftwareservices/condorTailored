"""A minimal lockout for the auth endpoints that have none.

``/auth/token-login``, ``/auth/local-login``, ``/auth/2fa/confirm`` and
``/auth/2fa/disable`` had no throttle at all before this: a 6-digit TOTP code
is roughly 1-in-333,000 per guess, and unthrottled a sustained attack from one
source can realistically land a hit within hours. This closes that with a
sliding window per key — ``client_ip`` for the pre-auth login endpoints,
``user_id`` for the two 2FA endpoints that act on an already-authenticated
session (a stolen cookie without the current code is exactly the attack those
two exist to stop).

Deliberately in-memory, not the ledger-on-disk pattern :mod:`condor.llm.budget`
uses for spend: a restart resetting the window costs nothing here (brute
force needs sustained, unattended guessing, not a handful of requests), and
the single-process constraint that makes a file-backed ledger worth it for a
shared daily cap does not apply to a per-source lockout.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from fastapi import HTTPException, status

# A failed attempt counts against the key for this long before aging out.
WINDOW_SECONDS = 300.0
# The attempt that trips the lockout — the (MAX_ATTEMPTS + 1)th within the window.
MAX_ATTEMPTS = 5

_lock = threading.Lock()
_failures: dict[str, list[float]] = defaultdict(list)


def _prune(key: str, now: float) -> None:
    kept = [t for t in _failures[key] if now - t < WINDOW_SECONDS]
    if kept:
        _failures[key] = kept
    else:
        _failures.pop(key, None)


def check(key: str) -> None:
    """Raise 429 if ``key`` has tripped the lockout. Call before verifying."""
    now = time.time()
    with _lock:
        _prune(key, now)
        attempts = _failures.get(key, [])
        if len(attempts) >= MAX_ATTEMPTS:
            retry_after = max(1, int(WINDOW_SECONDS - (now - attempts[0])))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many attempts. Try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )


def record_failure(key: str) -> None:
    """Count a wrong code/token/password attempt against ``key``."""
    now = time.time()
    with _lock:
        _prune(key, now)
        _failures[key].append(now)


def record_success(key: str) -> None:
    """Clear ``key``'s history — a correct attempt is not evidence of an attack."""
    with _lock:
        _failures.pop(key, None)


def client_ip(request) -> str:
    """The caller's address for the pre-auth endpoints that have no user yet.

    No ``X-Forwarded-For`` trust here: this install has no documented reverse
    proxy in front of it, and trusting a client-supplied header for the thing
    that decides the rate-limit key would make the limiter trivially
    bypassable by whoever is being limited.
    """
    return request.client.host if request.client else "unknown"
