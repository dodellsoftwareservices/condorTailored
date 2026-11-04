"""The Alpaca paper client, as a FastAPI dependency.

Beyond a trivial indirection — on its own ``AlpacaPaperClient()`` would do —
for two reasons:

- It gives ``equities.py``/``paper_equities.py``'s tests a dependency to
  override (``app.dependency_overrides[get_alpaca_client] = lambda:
  FakeAlpaca(...)``) instead of monkeypatching the class globally, the same
  shape ``get_current_user``/``require_server_access`` already give every
  other route.
- ``AlpacaPaperClient()`` raises ``AlpacaConfigError`` at *construction*
  when credentials are missing — which FastAPI resolves before a route
  handler's own body (and its try/except) ever runs. Left uncaught here,
  that would surface as a generic 500 instead of the clean 503 every other
  "not configured" path in this feature gives. Catching it here and raising
  ``HTTPException`` directly is the same pattern ``get_current_user``
  already uses for an invalid token (``condor/web/auth.py``) — a dependency
  is allowed to be the thing that decides the response.

Shared by both route modules rather than defined in one and imported by the
other, since neither owns the other.
"""

from __future__ import annotations

from fastapi import HTTPException

from condor.alpaca_paper import AlpacaConfigError, AlpacaPaperClient


def get_alpaca_client() -> AlpacaPaperClient:
    try:
        return AlpacaPaperClient()
    except AlpacaConfigError as e:
        raise HTTPException(
            status_code=503,
            detail={"code": "equity_not_configured", "message": str(e)},
        ) from None
