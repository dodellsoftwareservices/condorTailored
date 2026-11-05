"""The baseline is refreshed from OpenRouter's own usage figure and never loosens."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from condor.agent_config import Budget
from condor.llm import provider_usage as P
from condor.llm.budget import RequestGovernor, RequestLedger

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
KEY = "sk-or-v1-SECRETSECRETSECRET"


def run(coro):
    return asyncio.run(coro)


def usage(month=5.0, day=0.5, limit=50.0, remaining=45.0, as_of=NOW):
    return P.ProviderUsage(month, day, limit, remaining, as_of)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok_body(**fields):
    data = {
        "usage_monthly": 5.0,
        "usage_daily": 0.5,
        "limit": 50,
        "limit_remaining": 45,
        **fields,
    }
    return {"data": data}


# ── reading the provider ──


def test_a_good_reading_is_parsed_and_the_key_is_sent_as_a_bearer_token():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json=ok_body())

    got = run(P.fetch_usage(KEY, client=client_for(handler), clock=lambda: NOW))
    assert got == usage()
    assert seen["auth"] == f"Bearer {KEY}" and seen["url"] == P.USAGE_URL


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": "no"}),
        httpx.Response(500, text="oops"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"data": {}}),
        httpx.Response(200, json={"data": {"usage_monthly": -1, "usage_daily": 0}}),
        httpx.Response(200, json={"data": {"usage_monthly": "NaN", "usage_daily": 0}}),
        httpx.Response(200, json={"data": {"usage_monthly": 5}}),
    ],
)
def test_a_bad_reply_is_none_not_an_exception(response):
    assert run(P.fetch_usage(KEY, client=client_for(lambda r: response))) is None


def test_a_network_failure_is_none_and_the_key_is_never_logged(caplog):
    def handler(request):
        raise httpx.ConnectError("boom")

    with caplog.at_level(logging.DEBUG):
        assert run(P.fetch_usage(KEY, client=client_for(handler))) is None
    assert KEY not in caplog.text and "SECRET" not in caplog.text


# ── writing the baseline ──


def read_baseline(tmp_path):
    return json.loads((tmp_path / P.BASELINE_FILE).read_text())


def test_a_reading_becomes_a_baseline_with_an_overlap_before_it(tmp_path):
    assert P.apply_usage(tmp_path, usage(month=5.0, day=0.5)) == "written"
    base = read_baseline(tmp_path)
    assert base["month"] == "202611" and base["day"] == "20261115"
    assert base["month_used_usd"] == 5.0 and base["day_used_usd"] == 0.5
    # Requests just before the reading are counted again, not dropped.
    assert base["as_of"] == NOW - P.OVERLAP_SEC
    assert "OpenRouter" in base["source"]
    assert json.loads((tmp_path / P.READING_FILE).read_text())["month_used"] == 5.0


def test_a_lower_reading_for_the_same_month_never_loosens_the_baseline(tmp_path):
    P.apply_usage(tmp_path, usage(month=5.0))
    assert P.apply_usage(tmp_path, usage(month=4.9, as_of=NOW + 600)) == "kept"
    assert read_baseline(tmp_path)["month_used_usd"] == 5.0


def test_a_kept_baseline_still_records_the_fresh_provider_reading(tmp_path):
    """So the status route shows the provider's real figure (and the gap to a stricter
    baseline) instead of a reading frozen at the last time the baseline was written."""
    P.apply_usage(tmp_path, usage(month=5.0, as_of=NOW))
    assert P.apply_usage(tmp_path, usage(month=4.0, as_of=NOW + 600)) == "kept"
    reading = json.loads((tmp_path / P.READING_FILE).read_text())
    assert reading["month_used"] == 4.0 and reading["as_of"] == NOW + 600
    assert read_baseline(tmp_path)["month_used_usd"] == 5.0  # the baseline did not move


def test_a_higher_reading_replaces_it(tmp_path):
    P.apply_usage(tmp_path, usage(month=5.0))
    assert P.apply_usage(tmp_path, usage(month=7.5, as_of=NOW + 600)) == "written"
    assert read_baseline(tmp_path)["month_used_usd"] == 7.5


def test_a_new_month_starts_from_its_own_reading_even_if_lower(tmp_path):
    P.apply_usage(tmp_path, usage(month=29.0))
    december = datetime(2026, 12, 1, 1, tzinfo=timezone.utc).timestamp()
    assert P.apply_usage(tmp_path, usage(month=0.1, as_of=december)) == "written"
    assert read_baseline(tmp_path)["month"] == "202612"


def test_an_unreadable_baseline_is_repaired_from_the_provider(tmp_path):
    (tmp_path / P.BASELINE_FILE).write_text("{broken")
    assert P.apply_usage(tmp_path, usage()) == "written"
    assert read_baseline(tmp_path)["month_used_usd"] == 5.0


# ── the guard sees it ──


def test_the_guard_counts_the_provider_figure_plus_only_requests_after_the_overlap(
    tmp_path,
):
    ledger = RequestLedger(tmp_path, lambda: NOW)
    # Spend the provider already includes, and an old unpriced row from before it.
    ledger.record_cost("x", 2.0, now=NOW - 3600)
    ledger._append(NOW - 3000, {"t": NOW - 3000, "ev": "cost_unknown", "caller": "x"})
    P.apply_usage(tmp_path, usage(month=5.0, day=0.5))
    # A request inside the overlap (counted twice on purpose) and one after the reading.
    ledger.record_cost("x", 0.01, now=NOW - 60)
    ledger.record_cost("x", 0.02, now=NOW + 30)
    window = ledger.spend_window(NOW + 60)
    assert window["month_used_usd"] == pytest.approx(5.0 + 0.01 + 0.02)
    assert window["unpriced"] == 0  # the provider's figure replaced the guesswork


def test_spend_by_something_else_on_the_same_key_now_reaches_the_cap(tmp_path):
    cfg = Budget(
        enabled=True,
        monthly_cost_cap_usd=30.0,
        request_cost_reserve_usd=0.2,
        paid_price_ceiling_usd_per_million={"prompt": 0.17, "completion": 0.55},
        allowed_paid_models=("openrouter:deepseek/deepseek-v4.1-flash-1017",),
    )
    gov = RequestGovernor(
        lambda: cfg, RequestLedger(tmp_path, lambda: NOW), lambda: NOW
    )
    P.apply_usage(tmp_path, usage(month=29.9))  # another app burned the month
    with pytest.raises(Exception, match="monthly cost budget reached"):

        async def go():
            async with gov.cost_slot(
                model_key="openrouter:deepseek/deepseek-v4.1-flash-1017"
            ):
                pass

        run(go())


# ── the scheduled job ──


def job_env(monkeypatch, tmp_path, *, cap=30.0, key=KEY):
    cfg = SimpleNamespace(monthly_cost_cap_usd=cap)
    gov = SimpleNamespace(
        config=cfg, ledger=SimpleNamespace(directory=tmp_path / "ledger")
    )
    monkeypatch.setattr(P, "get_governor", lambda: gov)
    if key:
        monkeypatch.setenv("OPENROUTER_API_KEY", key)
    else:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return gov


def test_the_job_does_nothing_without_a_monthly_cap(monkeypatch, tmp_path):
    job_env(monkeypatch, tmp_path, cap=None)
    assert run(P.sync_provider_usage()) == "disabled"
    assert not (tmp_path / "ledger").exists()


def test_the_job_does_nothing_without_a_key(monkeypatch, tmp_path):
    job_env(monkeypatch, tmp_path, key="")
    assert run(P.sync_provider_usage()) == "no-key"


def test_a_failed_read_leaves_the_existing_baseline_alone(monkeypatch, tmp_path):
    job_env(monkeypatch, tmp_path)
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    P.apply_usage(ledger, usage(month=5.0))
    before = (ledger / P.BASELINE_FILE).read_text()

    async def down(*a, **k):
        return None

    monkeypatch.setattr(P, "fetch_usage", down)
    assert run(P.sync_provider_usage()) == "unavailable"
    assert (ledger / P.BASELINE_FILE).read_text() == before


def test_the_job_writes_the_baseline_it_read(monkeypatch, tmp_path):
    job_env(monkeypatch, tmp_path)
    seen = {}

    async def fake(key, **kw):
        seen["key"] = key
        return usage(month=6.25, as_of=NOW)

    monkeypatch.setattr(P, "fetch_usage", fake)
    assert run(P.sync_provider_usage()) == "written"
    assert seen["key"] == KEY
    base = json.loads((tmp_path / "ledger" / P.BASELINE_FILE).read_text())
    assert base["month_used_usd"] == 6.25


def test_the_job_is_registered_every_ten_minutes():
    calls = []
    app = SimpleNamespace(
        job_queue=SimpleNamespace(run_repeating=lambda *a, **k: calls.append((a, k)))
    )
    P.register_jobs(app)
    ((args, kwargs),) = calls
    assert args[0] is P.sync_provider_usage
    assert kwargs["interval"] == 600 and kwargs["name"] == "llm_provider_usage_sync"
