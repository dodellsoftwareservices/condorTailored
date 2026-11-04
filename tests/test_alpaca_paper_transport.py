"""Real paper-client requests with the HTTP boundary replaced; no provider access."""

import asyncio
import json

import aiohttp
import pytest

from condor import alpaca_paper as A
from condor.equity_observability import EquityMetrics, equity_correlation_id


@pytest.fixture
def transport(monkeypatch):
    calls = []
    metrics = EquityMetrics()
    monkeypatch.setattr(A, "equity_metrics", metrics)

    def install(*, status=200, body='{"status":"accepted","id":"order-1"}', error=None):
        class Response:
            async def __aenter__(self):
                if error is not None:
                    raise error
                return self

            async def __aexit__(self, *_):
                pass

            async def text(self):
                return body

        class Session:
            def __init__(self, **kwargs):
                assert kwargs["timeout"].total == 20

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            def request(self, method, url, **kwargs):
                calls.append({"method": method, "url": url, **kwargs})
                response = Response()
                response.status = status
                return response

        monkeypatch.setattr(A.aiohttp, "ClientSession", Session)

    return install, calls, metrics


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_never_forward_paper_credentials(transport, status):
    install, calls, _ = transport
    install(status=status)
    with pytest.raises(A.AlpacaAPIError) as exc:
        asyncio.run(A.AlpacaPaperClient("key", "secret").latest_quote("AAPL"))
    assert exc.value.status_code == status
    assert len(calls) == 1
    assert calls[0]["allow_redirects"] is False
    assert calls[0]["url"].startswith(A.DATA_HOST + "/")


@pytest.mark.parametrize("status", [400, 401, 403, 422, 429, 500])
def test_error_bodies_never_escape_and_status_is_preserved(transport, status):
    install, _, metrics = transport
    install(status=status, body="echoed-secret account-id order-payload")
    with pytest.raises(A.AlpacaAPIError) as exc:
        asyncio.run(
            A.AlpacaPaperClient("key", "secret").submit_notional_order("AAPL", 10)
        )
    assert exc.value.status_code == status
    assert "secret" not in str(exc.value)
    counters = metrics.snapshot()["counters"]
    assert counters["provider_error"] == 1
    assert counters["provider_rate_limited"] == int(status == 429)
    assert counters["order_rejected"] == int(400 <= status < 500)


@pytest.mark.parametrize("body", ['{"secret":invalid}', '"secret"', "42", "null"])
def test_malformed_and_scalar_json_is_sanitized(transport, body):
    install, _, _ = transport
    install(body=body)
    with pytest.raises(A.AlpacaAPIError) as exc:
        asyncio.run(A.AlpacaPaperClient("key", "secret").latest_quote("AAPL"))
    assert "secret" not in str(exc.value)
    assert exc.value.__cause__ is None


@pytest.mark.parametrize(
    "error", [TimeoutError("secret"), aiohttp.ClientConnectionError("secret")]
)
def test_ambiguous_submission_is_not_retried_or_reported_rejected(transport, error):
    install, calls, metrics = transport
    install(error=error)
    with pytest.raises(
        A.AlpacaTransportError, match="reconcile orders before retrying"
    ) as exc:
        asyncio.run(
            A.AlpacaPaperClient("key", "secret").submit_notional_order("AAPL", 10)
        )
    assert "secret" not in str(exc.value)
    assert len(calls) == 1
    snapshot = metrics.snapshot()
    assert snapshot["counters"]["order_rejected"] == 0
    assert snapshot["counters"]["provider_timeout"] == int(
        isinstance(error, TimeoutError)
    )
    assert snapshot["measurements"]["provider_latency_seconds"]["count"] == 1


def test_cancellation_propagates_without_retry(transport):
    install, calls, _ = transport
    install(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(A.AlpacaPaperClient("key", "secret").latest_quote("AAPL"))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status",
    ["accepted", "pending_new", "partially_filled", "filled", "rejected", "canceled"],
)
def test_provider_order_status_is_returned_without_claiming_a_fill(transport, status):
    install, calls, metrics = transport
    install(body=json.dumps({"id": "order-1", "status": status}))
    order = asyncio.run(
        A.AlpacaPaperClient("key", "secret").submit_notional_order("aapl", 10)
    )
    assert order == {"id": "order-1", "status": status}
    assert calls[0]["json"]["symbol"] == "AAPL"
    assert metrics.snapshot()["counters"]["provider_ok"] == 1


@pytest.mark.parametrize(
    "amount", [float("nan"), float("inf"), -float("inf"), 0, -1, 0.001]
)
def test_invalid_notional_never_reaches_http(transport, amount):
    install, calls, _ = transport
    install()
    with pytest.raises(ValueError):
        asyncio.run(
            A.AlpacaPaperClient("key", "secret").submit_notional_order("AAPL", amount)
        )
    assert calls == []


def test_unknown_side_never_reaches_http(transport):
    install, calls, _ = transport
    install()
    with pytest.raises(ValueError):
        asyncio.run(
            A.AlpacaPaperClient("key", "secret").submit_notional_order(
                "AAPL", 10, side="short"
            )
        )
    assert calls == []


@pytest.mark.parametrize("host", ["https://api.alpaca.markets", "https://evil.test"])
def test_internal_request_boundary_also_refuses_unapproved_hosts(transport, host):
    install, calls, _ = transport
    install()
    with pytest.raises(A.AlpacaConfigError):
        asyncio.run(
            A.AlpacaPaperClient("key", "secret")._request("GET", host, "/v2/account")
        )
    assert calls == []


@pytest.mark.parametrize("path", ["@evil.test/v2/orders", "https://evil.test", ""])
def test_request_path_cannot_change_the_destination(transport, path):
    install, calls, _ = transport
    install()
    with pytest.raises(A.AlpacaConfigError):
        asyncio.run(
            A.AlpacaPaperClient("key", "secret")._request(
                "GET", A.PAPER_TRADING_HOST, path
            )
        )
    assert calls == []


@pytest.mark.parametrize("argument", ["trading_host", "data_host"])
@pytest.mark.parametrize(
    "host",
    [
        "http://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets.evil.test",
        "https://paper-api.alpaca.markets@evil.test",
        "https://paper-api.alpaca.markets:443",
        "https://paper-api.alpaca.markets?host=evil.test",
        "https://paper-api.alpaca.markets#evil.test",
    ],
)
def test_allowlist_rejects_url_confusion_before_http(transport, argument, host):
    install, calls, _ = transport
    install()
    with pytest.raises(A.AlpacaConfigError):
        A.AlpacaPaperClient("key", "secret", **{argument: host})
    assert calls == []


def test_provider_logs_preserve_source_and_correlation_without_secrets(
    transport, caplog
):
    install, _, _ = transport
    install(status=403, body="secret-response")
    token = equity_correlation_id.set("generated-correlation")
    try:
        with caplog.at_level("INFO", logger="condor.alpaca_paper"):
            with pytest.raises(A.AlpacaAPIError):
                asyncio.run(
                    A.AlpacaPaperClient("secret-key", "secret-value").latest_quote(
                        "secret-symbol"
                    )
                )
    finally:
        equity_correlation_id.reset(token)
    messages = [
        r.getMessage() for r in caplog.records if r.name == "condor.alpaca_paper"
    ]
    assert len(messages) == 1
    assert "source=alpaca_data" in messages[0]
    assert "correlation_id=generated-correlation" in messages[0]
    assert "outcome=http_403" in messages[0]
    assert "secret" not in messages[0]
