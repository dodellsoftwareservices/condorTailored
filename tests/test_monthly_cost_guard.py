"""The calendar-month dollar guard: reservations, baseline, free-model bypass, pricing."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from condor.agent_config import Budget
from condor.llm.budget import (
    RESERVATION_TTL_SEC,
    BudgetExhausted,
    RequestGovernor,
    RequestLedger,
)
from condor.llm.cost_model import response_cost_usd

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
PAID = "openrouter:deepseek/deepseek-v4.1-flash-1017"
FREE = "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free"


def make(tmp_path, *, now=NOW, cap=30.0, **overrides):
    cfg = Budget(
        enabled=True,
        monthly_cost_cap_usd=cap,
        allowed_paid_models=(PAID,),
        **overrides,
    )
    ledger = RequestLedger(tmp_path, lambda: now)
    return RequestGovernor(lambda: cfg, ledger, lambda: now), ledger, cfg


def admit(gov, model=PAID):
    async def go():
        async with gov.cost_slot(model_key=model):
            pass

    asyncio.run(go())


def ledger_rows(tmp_path):
    rows = []
    for path in sorted(tmp_path.glob("requests-*.jsonl")):
        rows += [json.loads(line) for line in path.read_text().splitlines()]
    return rows


def write_baseline(tmp_path, **fields):
    base = {
        "as_of": NOW - 100,
        "month": "202611",
        "day": "20261115",
        "day_used_usd": 0.5,
        "month_used_usd": 12.0,
    }
    (tmp_path / "spend-baseline.json").write_text(json.dumps({**base, **fields}))


# ── configuration ──


def test_the_camel_case_file_keys_load_and_an_impossible_reserve_is_refused():
    cfg = Budget.model_validate(
        {
            "monthlyCostCapUsd": 30,
            "requestCostReserveUsd": 0.15,
            "allowedPaidModels": [PAID],
        }
    )
    assert cfg.monthly_cost_cap_usd == 30
    assert cfg.request_cost_reserve_usd == 0.15
    assert cfg.allowed_paid_models == (PAID,)
    with pytest.raises(ValidationError, match="must be below"):
        Budget(monthly_cost_cap_usd=0.10, request_cost_reserve_usd=0.15)


def test_a_monthly_cap_is_off_unless_configured():
    assert Budget().monthly_cost_cap_usd is None


# ── pricing a response ──


def unknown_price():
    def boom():
        raise LookupError("model not in the price catalog")

    return boom


def test_openrouters_billed_cost_prices_a_model_the_catalog_does_not_know():
    response = SimpleNamespace(provider_details={"cost": 0.0123}, cost=unknown_price())
    assert response_cost_usd(response, PAID) == 0.0123


def test_a_free_model_is_always_free_even_if_a_cost_is_reported():
    response = SimpleNamespace(provider_details={"cost": 9.0}, cost=unknown_price())
    assert response_cost_usd(response, FREE) == 0.0


def test_another_provider_is_priced_by_the_catalog_not_by_provider_details():
    response = SimpleNamespace(
        provider_details={"cost": 9.0},
        cost=lambda: SimpleNamespace(total_price=0.5),
    )
    assert response_cost_usd(response, "openai:gpt-4o") == 0.5
    with pytest.raises(LookupError):
        response_cost_usd(
            SimpleNamespace(provider_details=None, cost=unknown_price()), PAID
        )


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_an_invalid_billed_cost_is_never_recorded_as_spend(bad):
    response = SimpleNamespace(provider_details={"cost": bad}, cost=unknown_price())
    with pytest.raises(ValueError):
        response_cost_usd(response, PAID)


# ── admission ──


def test_a_paid_request_under_the_cap_reserves_then_releases(tmp_path):
    gov, ledger, _ = make(tmp_path)
    seen = {}

    async def go():
        async with gov.cost_slot(model_key=PAID):
            seen["during"] = ledger.spend_window(NOW)["reserved_usd"]

    asyncio.run(go())
    assert seen["during"] == 0.15
    assert ledger.spend_window(NOW)["reserved_usd"] == 0
    events = [r["ev"] for r in ledger_rows(tmp_path)]
    assert events == ["cost_reserve", "cost_release"]
    rows = ledger_rows(tmp_path)
    assert rows[0]["id"] == rows[1]["id"]


def test_free_models_bypass_the_cap_even_when_the_month_is_spent(tmp_path):
    gov, ledger, _ = make(tmp_path)
    ledger.record_cost("x", 30.0)
    admit(gov, FREE)  # no raise, and no reservation written
    assert [r["ev"] for r in ledger_rows(tmp_path)] == ["cost"]
    with pytest.raises(BudgetExhausted, match="monthly cost budget reached"):
        admit(gov, PAID)


def test_a_paid_model_that_is_not_on_the_allowlist_is_refused(tmp_path):
    gov, _, _ = make(tmp_path)
    with pytest.raises(BudgetExhausted, match="approved pricing envelope"):
        admit(gov, "openrouter:anthropic/claude-opus-4-1")


def test_the_cap_counts_the_next_requests_worst_case_before_it_is_sent(tmp_path):
    gov, ledger, _ = make(tmp_path)
    ledger.record_cost("x", 29.80)  # 29.80 + 0.15 reserve = 29.95: still room
    admit(gov)
    ledger.record_cost("x", 0.10)  # 29.90 + 0.15 >= 30: no room for another
    with pytest.raises(BudgetExhausted, match="monthly cost budget reached"):
        admit(gov)


def test_the_refusal_resets_on_the_first_of_the_next_month(tmp_path):
    gov, ledger, _ = make(tmp_path)
    ledger.record_cost("x", 30.0)
    with pytest.raises(BudgetExhausted) as hit:
        admit(gov)
    assert hit.value.resets_at == datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()


def test_the_december_reset_rolls_into_january(tmp_path):
    december = datetime(2026, 12, 20, 9, tzinfo=timezone.utc).timestamp()
    gov, ledger, _ = make(tmp_path, now=december)
    ledger.record_cost("x", 30.0, now=december)
    with pytest.raises(BudgetExhausted) as hit:
        admit(gov)
    assert hit.value.resets_at == datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()


def test_spend_does_not_carry_from_one_month_into_the_next(tmp_path):
    october = datetime(2026, 10, 31, 23, tzinfo=timezone.utc).timestamp()
    gov, ledger, _ = make(tmp_path)
    ledger.record_cost("x", 29.99, now=october)
    admit(gov)  # November starts at zero


def test_a_daily_cap_can_sit_inside_the_monthly_one(tmp_path):
    gov, ledger, _ = make(tmp_path, daily_cost_cap_usd=1.0)
    ledger.record_cost("x", 0.90)
    with pytest.raises(BudgetExhausted, match="daily cost budget spent"):
        admit(gov)


# ── reservations ──


def test_an_in_flight_reservation_counts_against_the_cap(tmp_path):
    gov, ledger, _ = make(tmp_path)
    ledger.record_cost("x", 29.75)
    ledger._append(NOW, {"t": NOW, "ev": "cost_reserve", "id": "other", "usd": 0.15})
    # 29.75 + 0.15 held + 0.15 for this request >= 30
    with pytest.raises(BudgetExhausted, match="monthly cost budget reached"):
        admit(gov)


def test_a_reservation_orphaned_by_a_crash_expires_instead_of_eating_the_cap(
    tmp_path,
):
    gov, ledger, _ = make(tmp_path)
    started = NOW - RESERVATION_TTL_SEC - 1
    ledger._append(
        started, {"t": started, "ev": "cost_reserve", "id": "dead", "usd": 0.15}
    )
    assert ledger.spend_window(NOW)["reserved_usd"] == 0
    admit(gov)


# ── the verified provider baseline ──


def test_the_baseline_books_earlier_spend_and_ignores_older_unpriced_rows(tmp_path):
    gov, ledger, _ = make(tmp_path)
    write_baseline(tmp_path)
    ledger._append(NOW - 200, {"t": NOW - 200, "ev": "cost_unknown", "caller": "x"})
    ledger.record_cost("x", 1.0, now=NOW - 50)
    window = ledger.spend_window(NOW)
    assert window["month_used_usd"] == pytest.approx(13.0)
    assert window["day_used_usd"] == pytest.approx(1.5)
    assert window["unpriced"] == 0
    admit(gov)


def test_an_unreadable_baseline_pauses_paid_requests_rather_than_guessing(tmp_path):
    gov, _, _ = make(tmp_path)
    (tmp_path / "spend-baseline.json").write_text("{not json")
    with pytest.raises(BudgetExhausted, match="baseline is unreadable"):
        admit(gov)
    write_baseline(tmp_path, month_used_usd=-5)
    with pytest.raises(BudgetExhausted, match="baseline is unreadable"):
        admit(gov)


def test_a_baseline_from_an_earlier_month_is_ignored(tmp_path):
    gov, ledger, _ = make(tmp_path)
    write_baseline(tmp_path, month="202610", month_used_usd=29.99)
    assert ledger.spend_window(NOW)["month_used_usd"] == 0
    admit(gov)


# ── unpriced responses ──


def test_an_unpriced_response_is_booked_at_the_reserve_not_a_month_long_pause(
    tmp_path,
):
    gov, ledger, _ = make(tmp_path, cap=0.50)
    ledger.record_unpriced("x")
    ledger.record_unpriced("x")  # 2 * 0.15 = 0.30; +0.15 reserve = 0.45 < 0.50
    admit(gov)  # a timeout does not stop paid work
    ledger.record_unpriced("x")  # 0.45 + 0.15 >= 0.50
    with pytest.raises(BudgetExhausted, match="monthly cost budget reached"):
        admit(gov)


def test_unpriced_responses_also_count_toward_the_daily_cap(tmp_path):
    gov, ledger, _ = make(tmp_path, daily_cost_cap_usd=0.40)
    ledger.record_unpriced("x")
    ledger.record_unpriced("x")  # 0.30 + 0.15 >= 0.40
    with pytest.raises(BudgetExhausted, match="daily cost budget spent"):
        admit(gov)
