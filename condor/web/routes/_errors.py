"""One mapping from a failed upstream call to a response that leaks nothing.

``str(exc)`` on an ``aiohttp`` client exception embeds the backend's own URL, so
handing it to a browser as an ``HTTPException`` detail publishes the internal
host and port to anyone who can provoke a backend blip — a timeout, a refused
connection, an upstream 5xx. SEC-116 established the remedy for the executor
mutations and SEC-126 extended it to the executor reads; this module is that
same remedy lifted out of ``routes/executors.py`` so every route module shares
one implementation instead of growing a second sanitizer.

The description itself still comes from :func:`condor.fetchers.describe_executor_error`,
which reads the safe pieces off the exception's attributes (``status`` is the
code the API answered with, ``message`` is the API's own ``detail``) and
collapses a transport failure — which has neither — to a generic line.
"""

from __future__ import annotations

from fastapi import HTTPException

from condor.fetchers.executors import describe_executor_error

__all__ = ["upstream_error", "equity_upstream_error"]


def upstream_error(action: str, exc: Exception) -> HTTPException:
    """Map a failed backend call to an ``HTTPException`` that leaks nothing.

    An upstream 4xx is the caller's own bad request and stays a 400; anything
    else — an upstream 5xx, a timeout, a refused connection — is this gateway
    failing to reach its backend, so 502. The detail carries the API's message
    but never the raw exception.

    Callers log the exception before raising: the address belongs in the server
    log, where an operator needs it, not in the response, where a trader on a
    shared server would read it.
    """
    status, message = describe_executor_error(exc)
    code = 400 if status is not None and 400 <= status < 500 else 502
    return HTTPException(status_code=code, detail=f"{action}: {message}")


def equity_upstream_error(code: str, action: str, exc: Exception) -> HTTPException:
    """:func:`upstream_error`'s sibling for the equities routes, not a reuse of it.

    ``upstream_error`` reads ``describe_executor_error(exc)``, which expects
    the Hummingbot API client's own exception shape (a ``.status`` /
    ``.message`` pair). ``AlpacaAPIError`` carries its code as
    ``.status_code`` instead, and ``EquityDataError`` (condor/equity_market_data.py)
    carries no HTTP status at all — both would silently fall through
    ``upstream_error`` to a flat 502, even on a clean 4xx such as an unknown
    symbol or insufficient buying power.

    ``code`` is a fixed, machine-readable string the frontend branches on,
    matching the ``{code, message}`` shape ``equity_rollout.py``'s own 503
    already uses — so every equities error body looks the same regardless of
    which layer raised it.
    """
    status = getattr(exc, "status_code", None)
    http_status = 400 if isinstance(status, int) and 400 <= status < 500 else 502
    return HTTPException(
        status_code=http_status, detail={"code": code, "message": f"{action}: {exc}"}
    )
