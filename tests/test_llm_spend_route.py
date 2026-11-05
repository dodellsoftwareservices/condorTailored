"""GET /llm/spend: the monthly guard's view, admin only."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from condor.agent_config import Budget
from condor.llm import provider_usage as P
from condor.llm.budget import RequestLedger
from condor.web.auth import get_current_user
from condor.web.models import WebUser
from condor.web.routes import llm_spend

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
PAID = "openrouter:deepseek/deepseek-v4.1-flash-1017"


def budget(**kw):
    return Budget(
        enabled=True,
        monthly_cost_cap_usd=30.0,
        request_cost_reserve_usd=0.2,
        paid_price_ceiling_usd_per_million={"prompt": 0.17, "completion": 0.55},
        allowed_paid_models=(PAID,),
        **kw,
    )


@pytest.fixture
def api(monkeypatch, tmp_path):
    ledger = RequestLedger(tmp_path, lambda: NOW)
    gov = SimpleNamespace(config=budget(), ledger=ledger)
    monkeypatch.setattr(llm_spend, "get_governor", lambda: gov)
    monkeypatch.setattr(llm_spend.time, "time", lambda: NOW)
    monkeypatch.setattr(
        "condor.web.auth.get_config_manager",
        lambda: SimpleNamespace(is_admin=lambda uid: uid == 1),
    )
    app = FastAPI()
    app.include_router(llm_spend.router)
    app.dependency_overrides[get_current_user] = lambda: WebUser(id=1, role="admin")
    client = TestClient(app)
    client.gov, client.ledger, client.dir = gov, ledger, tmp_path
    return client


def test_it_reports_the_cap_what_counts_and_the_headroom(api, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-NEVERSHOWN")
    P.apply_usage(
        api.dir, P.ProviderUsage(5.0, 0.5, 50.0, 45.0, NOW - 600)
    )  # provider reading
    api.ledger.record_cost("x", 1.0, now=NOW - 60)
    api.ledger._append(NOW - 30, {"t": NOW - 30, "ev": "cost_unknown", "caller": "x"})
    api.ledger._append(
        NOW - 10, {"t": NOW - 10, "ev": "cost_reserve", "id": "a", "usd": 0.2}
    )
    body = api.get("/llm/spend").json()
    assert body["enabled"] and body["status"] == "ok"
    assert body["cap_usd"] == 30 and body["reserve_usd"] == 0.2
    assert body["month_used_usd"] == pytest.approx(6.0)
    assert body["unpriced_responses"] == 1 and body["unpriced_booked_usd"] == 0.2
    assert body["reserved_in_flight_usd"] == 0.2
    assert body["counted_usd"] == pytest.approx(6.2)
    assert body["headroom_usd"] == pytest.approx(30 - 6.2 - 0.2)
    assert body["allowed_paid_models"] == [PAID]
    assert body["provider"]["month_used"] == 5.0
    assert body["baseline"]["month_used_usd"] == 5.0
    assert "sk-or-v1-NEVERSHOWN" not in json.dumps(body)  # the key's value never leaves


def test_it_says_so_when_no_monthly_cap_is_set(api):
    api.gov.config = Budget(enabled=True)
    assert api.get("/llm/spend").json() == {"enabled": False}


def test_an_unreadable_baseline_reads_as_paused_with_the_reason(api):
    (api.dir / P.BASELINE_FILE).write_text("{broken")
    body = api.get("/llm/spend").json()
    assert body["status"] == "paused" and "baseline is unreadable" in body["reason"]


def test_only_an_admin_can_read_it(api):
    api.app.dependency_overrides[get_current_user] = lambda: WebUser(id=2, role="user")
    assert api.get("/llm/spend").status_code == 403
