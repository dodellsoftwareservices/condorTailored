"""The request budget: ledger, governor, hooks and pacer (condor.llm.budget).

OpenRouter's free models allow 20 requests a minute and 1000 a day. These tests
pin the properties an unattended 10-hour run rests on: the count survives a
restart and is shared, the day rolls over at 00:00 UTC, the per-minute window
waits instead of failing, the reserve is never spent, a provider "day's limit"
refusal blocks everyone until it lifts, and the pacer makes a budget last its
whole horizon instead of the first hour of it.

Time is a fake clock throughout: nothing here sleeps.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from condor.agent_config import Budget
from condor.llm import budget as b


class Clock:
    """A settable clock whose sleep advances it, so waits cost no real time."""

    def __init__(self, start: float) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def utc(y, m, d, hh=0, mm=0, ss=0) -> float:
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc).timestamp()


NOON = utc(2026, 10, 28, 12)


def make(tmp_path, clock, **overrides):
    cfg = Budget(enabled=True, **overrides)
    ledger = b.RequestLedger(tmp_path / "ledger", clock=clock)
    gov = b.RequestGovernor(lambda: cfg, ledger, clock=clock, sleep=clock.sleep)
    return gov, ledger


def run(coro):
    return asyncio.run(coro)


# ── time helpers ──


def test_the_day_is_utc_and_the_reset_is_the_next_midnight():
    assert b.utc_day(utc(2026, 10, 28, 23, 59, 59)) == "20261028"
    assert b.utc_day(utc(2026, 10, 29, 0, 0, 0)) == "20261029"
    assert b.next_reset(utc(2026, 10, 28, 23, 59, 59)) == utc(2026, 10, 29)
    # strictly after: at exactly midnight the next reset is a day away
    assert b.next_reset(utc(2026, 10, 29)) == utc(2026, 10, 30)
    # 20:00 EDT is the reset the owner's overnight run crosses
    assert b.next_reset(utc(2026, 10, 28, 20, 30)) == utc(2026, 10, 29)


@pytest.mark.parametrize(
    "model, free",
    [
        ("nvidia/nemotron-3.5-super-vl:free", True),
        ("openrouter/free", True),
        ("OpenRouter/Free", True),
        ("nvidia/nemotron-3.5-super-vl", False),
        ("anthropic/claude-sonnet-4-5", False),
        ("", False),
    ],
)
def test_free_models_are_recognised(model, free):
    assert b.is_free_model(model) is free


# ── ledger ──


def test_the_count_survives_a_restart_and_is_shared(tmp_path):
    clock = Clock(NOON)
    first = b.RequestLedger(tmp_path, clock=clock)
    for _ in range(3):
        first.record_request("loop-a", "m:free", free=True)
    first.record_request("loop-b", "m:free", free=True)

    second = b.RequestLedger(tmp_path, clock=clock)  # a new process, same files
    assert second.used_today() == 4
    assert second.used_by("loop-a") == 3 and second.used_by("loop-b") == 1

    # ...and a write by one is seen by the other without either restarting.
    second.record_request("loop-b", "m:free", free=True)
    assert first.used_today() == 5


def test_each_line_is_the_documented_json(tmp_path):
    clock = Clock(NOON)
    led = b.RequestLedger(tmp_path, clock=clock)
    led.record_request(
        "market_making_expert.pmm_mister_operator_3", "m:free", free=True
    )
    led.record_response("market_making_expert.pmm_mister_operator_3", 200, 812.4)

    lines = [
        json.loads(x)
        for x in (tmp_path / "requests-20261028.jsonl").read_text().splitlines()
    ]

    assert lines[0] == {
        "t": NOON,
        "ev": "req",
        "caller": "market_making_expert.pmm_mister_operator_3",
        "model": "m:free",
        "free": True,
        "n": 1,
    }
    assert (
        lines[1]["ev"] == "res" and lines[1]["status"] == 200 and lines[1]["ms"] == 812
    )


def test_uncounted_requests_are_logged_but_do_not_spend_the_budget(tmp_path):
    led = b.RequestLedger(tmp_path, clock=Clock(NOON))
    led.record_request("x", "paid/model", counted=False)

    assert led.used_today() == 0
    assert (tmp_path / "requests-20261028.jsonl").read_text().count("\n") == 1


def test_the_day_rolls_over_at_utc_midnight(tmp_path):
    clock = Clock(utc(2026, 10, 28, 23, 59, 50))
    led = b.RequestLedger(tmp_path, clock=clock)
    for _ in range(5):
        led.record_request("x", "m:free", free=True)
    assert led.used_today() == 5

    clock.now = utc(2026, 10, 29, 0, 0, 5)

    assert led.used_today() == 0
    led.record_request("x", "m:free", free=True)
    assert led.used_today() == 1
    assert (tmp_path / "requests-20261029.jsonl").exists()


def test_a_half_written_line_is_ignored_until_its_newline_arrives(tmp_path):
    clock = Clock(NOON)
    led = b.RequestLedger(tmp_path, clock=clock)
    led.record_request("x", "m:free", free=True)
    path = tmp_path / "requests-20261028.jsonl"
    with path.open("ab") as fh:
        fh.write(b'{"t":1,"ev":"req","caller":"x","counted":tr')  # torn write

    assert led.used_today() == 1

    with path.open("ab") as fh:
        fh.write(b"ue}\n")
    assert led.used_today() == 2


def test_a_truncated_file_is_refolded_from_its_start(tmp_path):
    clock = Clock(NOON)
    led = b.RequestLedger(tmp_path, clock=clock)
    for _ in range(4):
        led.record_request("x", "m:free", free=True)
    assert led.used_today() == 4

    path = tmp_path / "requests-20261028.jsonl"
    path.write_text(path.read_text().splitlines()[0] + "\n")  # operator trimmed it

    assert led.used_today() == 1


def test_garbage_lines_do_not_poison_the_count(tmp_path):
    led = b.RequestLedger(tmp_path, clock=Clock(NOON))
    led.record_request("x", "m:free", free=True)
    with (tmp_path / "requests-20261028.jsonl").open("ab") as fh:
        fh.write(b"not json\n[1,2]\n\n")

    assert led.used_today() == 1


def test_a_daily_limit_is_remembered_until_it_lifts_and_not_past_midnight(tmp_path):
    clock = Clock(NOON)
    led = b.RequestLedger(tmp_path, clock=clock)
    until = NOON + 3600
    led.record_limit("x", 429, until, "free-models-per-day", daily=True)

    assert led.limited_until() == until
    # a second ledger (another process) learns of it too
    assert b.RequestLedger(tmp_path, clock=clock).limited_until() == until
    # a per-minute refusal is logged but blocks nobody for the day
    led2 = b.RequestLedger(tmp_path / "other", clock=clock)
    led2.record_limit("x", 429, NOON + 10, "rate limit", daily=False)
    assert led2.limited_until() == 0.0


def test_cost_is_zero_until_recorded(tmp_path):
    clock = Clock(NOON)
    led = b.RequestLedger(tmp_path, clock=clock)
    assert led.cost_today() == 0.0


def test_recorded_costs_accumulate_for_the_day(tmp_path):
    clock = Clock(NOON)
    led = b.RequestLedger(tmp_path, clock=clock)

    assert led.record_cost("x", 0.12) == pytest.approx(0.12)
    assert led.record_cost("y", 0.08) == pytest.approx(0.20)
    assert led.cost_today() == pytest.approx(0.20)
    # a second ledger (another process) reads the same total
    assert b.RequestLedger(tmp_path, clock=clock).cost_today() == pytest.approx(0.20)


def test_cost_resets_at_utc_midnight(tmp_path):
    clock = Clock(utc(2026, 10, 28, 23, 59, 0))
    led = b.RequestLedger(tmp_path, clock=clock)
    led.record_cost("x", 5.0)
    assert led.cost_today() == pytest.approx(5.0)

    clock.now = utc(2026, 10, 29, 0, 0, 1)
    assert led.cost_today() == 0.0


# ── governor ──


def test_disabled_or_unmetered_models_cost_nothing_and_write_nothing(tmp_path):
    clock = Clock(NOON)
    off = b.RequestGovernor(
        lambda: Budget(enabled=False),
        b.RequestLedger(tmp_path / "a", clock=clock),
        clock=clock,
        sleep=clock.sleep,
    )
    run(off.acquire("x", "m:free"))
    assert not (tmp_path / "a").exists()
    assert off.gate_for("x", "m:free") is None

    gov, ledger = make(tmp_path / "b", clock)
    run(gov.acquire("x", "anthropic/claude-sonnet-4-5"))  # paid: not metered
    assert ledger.used_today() == 0
    assert gov.gate_for("x", "anthropic/claude-sonnet-4-5") is None
    assert gov.gate_for("x", "m:free") is not None


def test_applies_to_all_meters_paid_models_too(tmp_path):
    gov, ledger = make(tmp_path, Clock(NOON), applies_to="all")

    run(gov.acquire("x", "anthropic/claude-sonnet-4-5"))

    assert ledger.used_today() == 1


def test_applies_to_agent_key_only_for_openrouter_keys(tmp_path):
    gov, _ = make(tmp_path, Clock(NOON))

    assert gov.applies_to_agent_key("openrouter:nvidia/nemotron-3.5-super-vl:free")
    assert not gov.applies_to_agent_key("openrouter:anthropic/claude-sonnet-4-5")
    assert not gov.applies_to_agent_key("ollama:llama3.1")
    assert not gov.applies_to_agent_key("claude-acp:sonnet")
    assert not gov.applies_to_agent_key("")


def test_the_reserve_is_never_spent(tmp_path):
    clock = Clock(NOON)
    gov, ledger = make(
        tmp_path,
        clock,
        daily_request_cap=10,
        reserve_requests=3,
        requests_per_minute=1000,
    )

    for _ in range(7):
        run(gov.acquire("x", "m:free"))
    assert gov.remaining() == 0

    with pytest.raises(b.BudgetExhausted) as err:
        run(gov.acquire("x", "m:free"))

    assert "7 of 10" in str(err.value)
    assert err.value.resets_at == utc(2026, 10, 29)
    assert ledger.used_today() == 7  # the refused request was not booked


def test_no_cost_cap_set_never_raises_on_cost(tmp_path):
    clock = Clock(NOON)
    gov, ledger = make(tmp_path, clock, requests_per_minute=1000)
    ledger.record_cost("x", 1_000_000.0)  # absurd, but daily_cost_cap_usd is None
    run(gov.acquire("x", "m:free"))  # no raise


def test_a_cost_cap_blocks_the_next_request_once_crossed(tmp_path):
    clock = Clock(NOON)
    gov, ledger = make(
        tmp_path,
        clock,
        applies_to="all",
        requests_per_minute=1000,
        daily_cost_cap_usd=5.0,
    )
    ledger.record_cost("x", 4.99)
    run(gov.acquire("x", "m:paid"))  # still under the cap

    ledger.record_cost("x", 0.02)  # now at $5.01, over the $5 cap
    with pytest.raises(b.BudgetExhausted) as err:
        run(gov.acquire("x", "m:paid"))

    assert "$5.01" in str(err.value)
    assert "$5.00" in str(err.value)
    assert err.value.resets_at == utc(2026, 10, 29)


def test_a_cost_cap_is_checked_even_when_applies_to_is_all(tmp_path):
    """A paid model is only metered at all when ``appliesTo: "all"`` — the
    live cost-cap scenario the system review describes (finding #21)."""
    clock = Clock(NOON)
    gov, ledger = make(
        tmp_path,
        clock,
        applies_to="all",
        requests_per_minute=1000,
        daily_cost_cap_usd=1.0,
    )
    ledger.record_cost("x", 1.50)

    with pytest.raises(b.BudgetExhausted):
        run(gov.acquire("x", "anthropic/claude-sonnet-4-5"))


def test_the_cost_cap_resets_with_the_day(tmp_path):
    clock = Clock(utc(2026, 10, 28, 23, 59, 0))
    gov, ledger = make(
        tmp_path, clock, requests_per_minute=1000, daily_cost_cap_usd=1.0
    )
    ledger.record_cost("x", 2.0)
    with pytest.raises(b.BudgetExhausted):
        run(gov.acquire("x", "m:free"))

    clock.now = utc(2026, 10, 29, 0, 0, 1)
    run(gov.acquire("x", "m:free"))  # no raise — new day, cost reset to 0


def test_the_budget_comes_back_after_the_reset(tmp_path):
    clock = Clock(utc(2026, 10, 28, 23, 59, 0))
    gov, _ = make(
        tmp_path,
        clock,
        daily_request_cap=4,
        reserve_requests=1,
        requests_per_minute=1000,
    )
    for _ in range(3):
        run(gov.acquire("x", "m:free"))
    with pytest.raises(b.BudgetExhausted):
        run(gov.acquire("x", "m:free"))

    clock.now = utc(2026, 10, 29, 0, 0, 1)

    run(gov.acquire("x", "m:free"))  # no raise
    assert gov.remaining() == 2


def test_the_per_minute_window_waits_instead_of_failing(tmp_path):
    clock = Clock(NOON)
    gov, ledger = make(tmp_path, clock, requests_per_minute=3)

    for _ in range(3):
        run(gov.acquire("x", "m:free"))
    assert clock.slept == []

    run(gov.acquire("x", "m:free"))  # the fourth must wait out the window

    assert clock.now - NOON == pytest.approx(60.0, abs=5.0)
    assert sum(clock.slept) == pytest.approx(60.0, abs=5.0)
    assert ledger.used_today() == 4


def test_the_window_slides_it_does_not_reset_in_blocks(tmp_path):
    clock = Clock(NOON)
    gov, _ = make(tmp_path, clock, requests_per_minute=2)
    run(gov.acquire("x", "m:free"))
    clock.now += 40
    run(gov.acquire("x", "m:free"))

    run(gov.acquire("x", "m:free"))  # needs the first (t=0) to age out at t=60

    assert clock.now - NOON == pytest.approx(60.0, abs=5.0)


def test_a_provider_daily_limit_blocks_every_caller_until_it_lifts(tmp_path):
    clock = Clock(NOON)
    gov, _ = make(tmp_path, clock)
    gov.note_rate_limited("a", 429, NOON + 7200, "free-models-per-day", daily=True)

    with pytest.raises(b.BudgetExhausted) as err:
        run(gov.acquire("b", "m:free"))
    assert err.value.resets_at == NOON + 7200
    assert gov.hold_seconds() == pytest.approx(7200)

    clock.now = NOON + 7201
    run(gov.acquire("b", "m:free"))  # lifted


def test_a_per_minute_refusal_is_a_short_cooldown_not_an_error(tmp_path):
    clock = Clock(NOON)
    gov, _ = make(tmp_path, clock)
    gov.note_rate_limited("a", 429, None, "slow down", daily=False)

    run(gov.acquire("a", "m:free"))

    assert clock.now - NOON >= b.DEFAULT_COOLDOWN_SECONDS - 1e-6


def test_a_provider_asking_for_a_long_cooldown_is_capped(tmp_path):
    clock = Clock(NOON)
    gov, _ = make(tmp_path, clock)
    gov.note_rate_limited("a", 429, NOON + 99999, "slow down", daily=False)

    run(gov.acquire("a", "m:free"))

    assert clock.now - NOON <= b.MAX_COOLDOWN_SECONDS + b.WAIT_STEP_SECONDS


def test_hold_seconds_says_when_a_tick_can_start(tmp_path):
    clock = Clock(utc(2026, 10, 28, 22))
    gov, _ = make(
        tmp_path,
        clock,
        daily_request_cap=10,
        reserve_requests=2,
        requests_per_minute=1000,
    )

    assert gov.hold_seconds(8) == 0.0  # 8 left, a tick needs 8
    for _ in range(3):
        run(gov.acquire("x", "m:free"))

    assert gov.hold_seconds(8) == pytest.approx(2 * 3600)  # 5 left: wait for 00:00 UTC
    assert gov.hold_seconds(5) == 0.0


def test_hold_seconds_is_zero_when_the_budget_is_off(tmp_path):
    clock = Clock(NOON)
    gov = b.RequestGovernor(
        lambda: Budget(enabled=False),
        b.RequestLedger(tmp_path, clock=clock),
        clock=clock,
        sleep=clock.sleep,
    )

    assert gov.hold_seconds(1000) == 0.0


def test_concurrent_callers_are_each_counted_once(tmp_path):
    clock = Clock(NOON)
    gov, ledger = make(
        tmp_path,
        clock,
        requests_per_minute=1000,
        daily_request_cap=100,
        reserve_requests=0,
    )

    async def burst():
        await asyncio.gather(*(gov.acquire(f"c{i % 4}", "m:free") for i in range(40)))

    run(burst())

    assert ledger.used_today() == 40
    assert sorted(ledger.by_caller().values()) == [10, 10, 10, 10]


def test_snapshot_reports_the_whole_picture(tmp_path):
    clock = Clock(NOON)
    gov, _ = make(
        tmp_path,
        clock,
        daily_request_cap=100,
        reserve_requests=10,
        requests_per_minute=1000,
    )
    for _ in range(5):
        run(gov.acquire("x", "m:free"))

    snap = gov.snapshot().to_dict()

    assert snap == {
        "enabled": True,
        "day": "20261028",
        "used": 5,
        "cap": 100,
        "reserve": 10,
        "remaining": 85,
        "requests_per_minute": 1000,
        "resets_at": utc(2026, 10, 29),
        "limited_until": 0.0,
        "cost_used_usd": 0.0,
        "cost_cap_usd": None,
    }


# ── HTTP hooks ──


def _client(gov, handler, caller="loop-1", model="m:free"):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks=gov.event_hooks(caller, model),
    )


def test_hooks_record_how_a_request_ended(tmp_path):
    clock = Clock(NOON)
    gov, ledger = make(tmp_path, clock)

    async def go():
        async with _client(gov, lambda r: httpx.Response(200, json={"ok": True})) as c:
            await c.post("https://openrouter.ai/api/v1/chat/completions", json={})

    run(go())

    lines = [
        json.loads(x)
        for x in (tmp_path / "ledger" / "requests-20261028.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [x["ev"] for x in lines] == [
        "res"
    ]  # the gate counts the request, the hook its outcome
    assert lines[0]["status"] == 200 and lines[0]["caller"] == "loop-1"
    assert ledger.used_today() == 0


def test_requests_that_are_not_completions_are_ignored(tmp_path):
    gov, ledger = make(tmp_path, Clock(NOON))

    async def go():
        async with _client(gov, lambda r: httpx.Response(200, json={})) as c:
            await c.get("https://openrouter.ai/api/v1/models")

    run(go())

    assert not (tmp_path / "ledger").exists()


def test_an_sdk_retry_is_another_request_to_the_provider(tmp_path):
    gov, ledger = make(tmp_path, Clock(NOON))

    async def go():
        async with _client(gov, lambda r: httpx.Response(200, json={})) as c:
            await c.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"x-stainless-retry-count": "1"},
                json={},
            )

    run(go())

    assert ledger.used_today() == 1


def test_a_daily_429_blocks_everyone_and_a_minute_429_only_cools_down(tmp_path):
    clock = Clock(NOON)
    gov, ledger = make(tmp_path, clock)
    reset_ms = int((NOON + 5400) * 1000)

    def daily(request):
        return httpx.Response(
            429,
            headers={"x-ratelimit-reset": str(reset_ms)},
            json={"error": {"message": "Rate limit exceeded: free-models-per-day."}},
        )

    async def go(handler):
        async with _client(gov, handler) as c:
            await c.post("https://openrouter.ai/api/v1/chat/completions", json={})

    run(go(daily))
    assert ledger.limited_until() == pytest.approx(NOON + 5400)
    with pytest.raises(b.BudgetExhausted):
        run(gov.acquire("other", "m:free"))

    # A refusal for another reason (no mention of the day's limit): only a cooldown.
    clock.now = NOON + 6000
    gov2, ledger2 = make(tmp_path / "second", clock)

    async def go2():
        async with _client(
            gov2,
            lambda r: httpx.Response(429, json={"error": {"message": "slow down"}}),
        ) as c:
            await c.post("https://openrouter.ai/api/v1/chat/completions", json={})

    run(go2())
    assert ledger2.limited_until() == 0.0
    run(gov2.acquire("x", "m:free"))  # waits out the cooldown, then goes
    assert clock.now - (NOON + 6000) >= b.MIN_COOLDOWN_SECONDS


@pytest.mark.parametrize(
    "headers, expected",
    [
        ({"x-ratelimit-reset": "1793264400000"}, 1793264400.0),  # ms epoch
        ({"x-ratelimit-reset": "1793264400"}, 1793264400.0),  # s epoch
        ({"retry-after": "30"}, NOON + 30),
        ({"x-ratelimit-reset": "soon"}, None),
        ({"x-ratelimit-reset": "12"}, None),  # neither an epoch nor trusted
        ({}, None),
    ],
)
def test_reset_header_forms(headers, expected):
    assert b._reset_epoch(httpx.Headers(headers), NOON) == expected


# ── pacing ──

H = 3600.0


def pace(**kw):
    base = dict(
        frequency_sec=120,
        tick_seconds=30,
        requests_per_tick=8,
        remaining_requests=1000,
        share=1.0,
        now=NOON,
        shift_end=NOON + 4 * H,
        reset_at=NOON + 8 * H,
    )
    base.update(kw)
    return b.pace_delay(**base)


def test_plenty_of_budget_leaves_the_configured_cadence_alone():
    # 4h = 14400s; 1000/8 = 125 affordable ticks -> 115s apart, under the 120s floor
    assert pace() == 120


def test_a_tight_budget_stretches_the_gap_to_last_the_shift():
    # 400 requests / 8 per tick = 50 ticks across 14400s = 288s apart, less the tick's 30s
    assert pace(remaining_requests=400) == pytest.approx(288 - 30)


def test_pacing_never_speeds_a_loop_up():
    assert pace(frequency_sec=600, remaining_requests=100000) == 600


def test_the_horizon_is_the_earlier_of_shift_end_and_reset():
    # 200 requests / 8 per tick = 25 ticks across the 2h horizon = 288s apart, less the tick's 30s
    reset_first = pace(
        remaining_requests=200, shift_end=NOON + 10 * H, reset_at=NOON + 2 * H
    )
    shift_first = pace(
        remaining_requests=200, shift_end=NOON + 2 * H, reset_at=NOON + 10 * H
    )
    no_shift = pace(remaining_requests=200, shift_end=None, reset_at=NOON + 2 * H)

    assert reset_first == shift_first == no_shift == pytest.approx(7200 / 25 - 30)


def test_a_share_scales_the_rate():
    full = pace(remaining_requests=400)
    half = pace(remaining_requests=400, share=0.5)

    assert half + 30 == pytest.approx(2 * (full + 30))


def test_a_loop_that_cannot_afford_a_tick_waits_out_the_horizon():
    assert pace(remaining_requests=5) == 4 * H
    assert pace(remaining_requests=0) == 4 * H


def test_a_loop_past_its_horizon_does_not_get_a_negative_or_zero_gap():
    assert pace(now=NOON + 4 * H + 100) >= 120


def _simulate(
    *, budget, share=1.0, horizon=4 * H, freq=60, per_tick=8, tick=40, others=0.0
):
    """Run one loop to the horizon the way the engine does and report what it spent."""
    now, end = NOON, NOON + horizon
    spent, ticks, last = 0.0, 0, NOON
    remaining = budget
    while now < end and remaining >= per_tick:
        spent += per_tick
        remaining -= per_tick
        ticks += 1
        last = now
        now += tick
        gap = b.pace_delay(
            frequency_sec=freq,
            tick_seconds=tick,
            requests_per_tick=per_tick,
            remaining_requests=remaining,
            share=share,
            now=now,
            shift_end=end,
            reset_at=end + 10 * H,
        )
        now += min(gap, max(end - now, 0))
    return spent, ticks, last - NOON


def test_paced_ticks_stay_inside_the_budget_and_last_the_whole_shift():
    spent, ticks, last_tick_offset = _simulate(budget=850)

    assert spent <= 850
    assert ticks >= 90  # it used the budget, it did not hoard it
    # the last tick lands near the end of the 4h shift, not an hour in
    assert last_tick_offset > 4 * H - 3 * 600


def test_an_unpaced_loop_would_have_run_dry_early():
    """The reason pacing exists: at the configured cadence the budget is gone in 2h."""
    ticks_unpaced = 4 * H / (60 + 40)
    assert ticks_unpaced * 8 > 850  # 1152 requests wanted, 850 available

    spent, _, last = _simulate(budget=850, freq=60)
    assert last > 3 * H  # paced: still ticking in the final hour


# ── process-wide governor ──


def test_get_governor_is_one_per_process_until_reset():
    b.reset_governor()
    first = b.get_governor()

    assert b.get_governor() is first
    b.reset_governor()
    assert b.get_governor() is not first
