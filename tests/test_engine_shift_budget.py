"""The tick engine's shift clock and request budget.

A loop started by the shift runner carries ``shift_ends_at``, so a run ends when
its shift does whether or not anything is still watching; it holds ticks while
the day's request budget cannot pay for one instead of dying halfway through a
deploy; and it stretches the gap between ticks so the budget lasts its horizon.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

from condor import agent_config
from condor.agents import engine as engine_mod
from condor.llm import budget

OR_FREE = "openrouter:nvidia/nemotron-3.5-super-vl:free"

NOON = datetime(2026, 10, 28, 12, tzinfo=timezone.utc).timestamp()  # 08:00 EDT
H = 3600.0


class FakeClock:
    """One settable clock for the engine and the governor, so no result depends on
    the time of day the suite runs (the pacing horizon is the time to 00:00 UTC)."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock(NOON)
    real = time

    class _EngineTime:  # what engine.py sees as ``time``
        time = staticmethod(lambda: fake.now)
        monotonic = staticmethod(real.monotonic)
        perf_counter = staticmethod(real.perf_counter)

    monkeypatch.setattr(engine_mod, "time", _EngineTime)
    return fake


def _budget_config(tmp_path, monkeypatch, clock=None, **budget_kw):
    body = {"budget": {"enabled": True, "requestsPerMinute": 1000, **budget_kw}}
    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps(body))
    monkeypatch.setenv(agent_config.CONFIG_ENV, str(path))
    agent_config.reset_cache()
    budget.reset_governor()
    if clock is not None:
        ledger = budget.RequestLedger(tmp_path / "ledger", clock=clock)
        monkeypatch.setattr(
            budget, "_governor", budget.RequestGovernor(ledger=ledger, clock=clock)
        )


def _engine(tmp_path, monkeypatch, **config):
    from condor.agents.agent import Agent
    from condor.agents.engine import TickEngine
    from condor.agents.strategy import Strategy

    monkeypatch.setenv("CONDOR_AGENTS_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("CONDOR_REPORTS_DIR", str(tmp_path / "reports"))
    strategy = Strategy(agent_slug="brigado", name="Grid")
    strategy.home.mkdir(parents=True, exist_ok=True)
    engine = TickEngine(
        agent=Agent(slug="brigado", name="Brigado"),
        strategy=strategy,
        config={"execution_mode": "loop", "agent_key": OR_FREE, **config},
        chat_id=1,
        user_id=1,
    )
    engine.notices = []

    async def notify(message):
        engine.notices.append(message)

    monkeypatch.setattr(engine, "_notify", notify)
    return engine


def _journal_text(engine) -> str:
    return engine.journal._path.read_text()


def _spend(n: int, caller: str = "someone-else") -> None:
    gov = budget.get_governor()
    for _ in range(n):
        gov.ledger.record_request(caller, "m:free", free=True)


# ── the shift deadline ──


def test_no_deadline_means_the_run_is_never_over(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)

    assert engine._shift_end() == 0.0
    assert asyncio.run(engine._shift_over()) is False


def test_a_future_deadline_is_not_the_end(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch, shift_ends_at=time.time() + 3600)

    assert asyncio.run(engine._shift_over()) is False


@pytest.mark.parametrize("junk", ["soon", None, "", {}, []])
def test_an_unreadable_deadline_is_no_deadline(tmp_path, monkeypatch, junk):
    engine = _engine(tmp_path, monkeypatch, shift_ends_at=junk)

    assert engine._shift_end() == 0.0
    assert asyncio.run(engine._shift_over()) is False


def test_at_the_deadline_the_default_is_to_wind_the_positions_down(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path, monkeypatch, shift_ends_at=time.time() - 1)
    calls = []

    async def shutdown(reason):
        calls.append(reason)

    monkeypatch.setattr(engine, "_run_shutdown", shutdown)

    assert asyncio.run(engine._shift_over()) is True

    assert calls == ["shift ended"]
    assert "shift_ended" in _journal_text(engine)
    assert any("shift ended" in n for n in engine.notices)


def test_stop_action_only_stops_and_keeps_positions(tmp_path, monkeypatch):
    engine = _engine(
        tmp_path, monkeypatch, shift_ends_at=time.time() - 1, shift_end_action="stop"
    )
    finished = []

    async def finish(state, reason):
        finished.append((state, reason))

    async def shutdown(reason):  # must not run
        raise AssertionError("a stop must not wind down")

    monkeypatch.setattr(engine, "_finish", finish)
    monkeypatch.setattr(engine, "_run_shutdown", shutdown)

    assert asyncio.run(engine._shift_over()) is True

    assert finished == [(engine_mod.LoopState.COMPLETED, "shift_deadline")]


def test_the_loop_ticks_until_the_deadline_then_ends_itself(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch, shift_ends_at=time.time() + 0.25)
    ticks, shutdowns = [], []

    async def tick():
        ticks.append(time.time())

    async def shutdown(reason):
        shutdowns.append(reason)
        engine._running = False

    monkeypatch.setattr(engine, "_tick", tick)
    monkeypatch.setattr(engine, "_run_shutdown", shutdown)
    engine._running = True

    asyncio.run(asyncio.wait_for(engine._run_ticks(0.05, "loop"), timeout=10))

    assert len(ticks) >= 2 and shutdowns == ["shift ended"]
    assert all(t < engine._shift_end() + 0.1 for t in ticks)


def test_a_long_gap_never_sleeps_past_the_deadline(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch, shift_ends_at=time.time() + 5)

    # a 10-minute cadence is cut to the 5 seconds left, so the run wakes AT the end
    assert 4 < engine._next_gap(600) <= 5


def test_a_paused_loop_still_ends_with_its_shift(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch, shift_ends_at=time.time() - 1)
    shutdowns = []

    async def shutdown(reason):
        shutdowns.append(reason)
        engine._running = False

    monkeypatch.setattr(engine, "_run_shutdown", shutdown)
    engine._running, engine._paused = True, True

    asyncio.run(asyncio.wait_for(engine._run_ticks(60, "loop"), timeout=10))

    assert shutdowns == ["shift ended"]


# ── holding for the request budget ──


def test_budget_off_never_holds(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)

    assert asyncio.run(engine._budget_hold()) == 0.0
    assert engine._requests_booked() == 0


def test_an_unmetered_model_never_holds(tmp_path, monkeypatch, clock):
    _budget_config(tmp_path, monkeypatch, clock, dailyRequestCap=10, reserveRequests=0)
    _spend(10)
    engine = _engine(
        tmp_path, monkeypatch, agent_key="openrouter:anthropic/claude-sonnet-4-5"
    )

    assert asyncio.run(engine._budget_hold()) == 0.0


def test_it_holds_while_the_budget_cannot_pay_for_a_tick_and_says_so_once(
    tmp_path, monkeypatch, clock
):
    _budget_config(
        tmp_path,
        monkeypatch,
        clock,
        dailyRequestCap=100,
        reserveRequests=10,
        assumedRequestsPerTick=8,
    )
    engine = _engine(tmp_path, monkeypatch)
    _spend(85)  # 90 spendable, 5 left, a tick needs 8

    first = asyncio.run(engine._budget_hold())
    again = asyncio.run(engine._budget_hold())

    assert 30 <= first <= 900 and 30 <= again <= 900
    assert "budget_hold" in _journal_text(engine)
    assert len(engine.notices) == 1, "a hold lasting hours is announced once"
    assert "85 of 100" in engine.notices[0]


def test_it_resumes_and_says_so_when_the_budget_comes_back(
    tmp_path, monkeypatch, clock
):
    _budget_config(
        tmp_path, monkeypatch, clock, dailyRequestCap=100, reserveRequests=10
    )
    engine = _engine(tmp_path, monkeypatch)
    _spend(85)
    assert 30 <= asyncio.run(engine._budget_hold()) <= 900
    assert engine._budget_hold_noted

    clock.now += 12 * H + 60  # past 00:00 UTC: a new day, a fresh budget
    resumed = asyncio.run(engine._budget_hold())

    assert resumed == 0.0 and not engine._budget_hold_noted
    assert "budget_resumed" in _journal_text(engine)


def test_a_provider_daily_limit_holds_even_with_budget_left(
    tmp_path, monkeypatch, clock
):
    _budget_config(tmp_path, monkeypatch, clock)
    engine = _engine(tmp_path, monkeypatch)
    budget.get_governor().note_rate_limited(
        "x", 429, clock.now + 3 * H, "free-models-per-day", daily=True
    )

    assert 30 <= asyncio.run(engine._budget_hold()) <= 900


def test_a_held_loop_makes_no_tick(tmp_path, monkeypatch, clock):
    _budget_config(
        tmp_path, monkeypatch, clock, dailyRequestCap=100, reserveRequests=10
    )
    _spend(95)
    engine = _engine(tmp_path, monkeypatch)
    ticks, sleeps = [], []

    async def tick():
        ticks.append(1)

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError  # the supervisor stopping the run

    monkeypatch.setattr(engine, "_tick", tick)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    engine._running = True

    asyncio.run(engine._run_ticks(60, "loop"))

    assert ticks == []
    assert len(sleeps) == 1 and 30 <= sleeps[0] <= 900


# ── learning what a tick costs ──


def test_the_per_tick_cost_is_a_moving_average_of_what_the_ledger_booked(
    tmp_path, monkeypatch, clock
):
    _budget_config(tmp_path, monkeypatch, clock)
    engine = _engine(tmp_path, monkeypatch)
    assert engine._expected_tick_requests() == 8  # the configured assumption

    before = engine._requests_booked()
    _spend(10, engine.agent_id)
    engine._learn_requests_per_tick(before)
    assert engine._requests_per_tick == 10
    assert engine._expected_tick_requests() == 10

    before = engine._requests_booked()
    _spend(20, engine.agent_id)
    engine._learn_requests_per_tick(before)
    assert engine._requests_per_tick == pytest.approx(0.7 * 10 + 0.3 * 20)


def test_a_tick_that_booked_nothing_teaches_nothing(tmp_path, monkeypatch, clock):
    _budget_config(tmp_path, monkeypatch, clock)
    engine = _engine(tmp_path, monkeypatch)
    engine._requests_per_tick = 9.0

    engine._learn_requests_per_tick(engine._requests_booked())

    assert engine._requests_per_tick == 9.0


def test_other_sessions_requests_are_not_this_sessions_cost(
    tmp_path, monkeypatch, clock
):
    _budget_config(tmp_path, monkeypatch, clock)
    engine = _engine(tmp_path, monkeypatch)
    before = engine._requests_booked()

    _spend(50, "some.other_loop_1")

    assert engine._requests_booked() == before


# ── pacing ──


def test_with_plenty_of_budget_the_cadence_is_left_alone(tmp_path, monkeypatch, clock):
    _budget_config(tmp_path, monkeypatch, clock)
    engine = _engine(tmp_path, monkeypatch, shift_ends_at=clock.now + 4 * H)

    # 950 spendable / 8 per tick over 4h affords a tick every 121s: 300s is well inside it
    assert engine._next_gap(300) == 300
    assert engine._paced_gap == 0.0


def test_a_tight_budget_stretches_the_gap_and_records_it(tmp_path, monkeypatch, clock):
    _budget_config(
        tmp_path, monkeypatch, clock, dailyRequestCap=1000, reserveRequests=50
    )
    _spend(750)  # 200 spendable left
    engine = _engine(tmp_path, monkeypatch, shift_ends_at=clock.now + 4 * H)
    engine._last_tick_duration = 30

    gap = engine._next_gap(120)

    # 200 / 8 = 25 ticks over the 4h shift: 576s apart, less the 30s the tick took
    assert gap == pytest.approx(14400 / 25 - 30)
    assert engine._paced_gap == gap


def test_pacing_off_leaves_the_cadence_alone(tmp_path, monkeypatch, clock):
    _budget_config(tmp_path, monkeypatch, clock, pacing=False)
    _spend(900)
    engine = _engine(tmp_path, monkeypatch)

    assert engine._next_gap(120) == 120


def test_a_share_slows_one_loop_relative_to_its_siblings(tmp_path, monkeypatch, clock):
    _budget_config(
        tmp_path, monkeypatch, clock, dailyRequestCap=1000, reserveRequests=50
    )
    _spend(750)
    ends = clock.now + 4 * H
    solo = _engine(tmp_path, monkeypatch, shift_ends_at=ends, request_share=1.0)
    third = _engine(tmp_path, monkeypatch, shift_ends_at=ends, request_share=1 / 3)

    assert third._next_gap(120) > 2 * solo._next_gap(120)


def test_a_garbage_share_is_treated_as_the_whole_budget(tmp_path, monkeypatch, clock):
    _budget_config(tmp_path, monkeypatch, clock)
    engine = _engine(
        tmp_path, monkeypatch, request_share="lots", shift_ends_at=clock.now + 4 * H
    )
    whole = _engine(
        tmp_path, monkeypatch, request_share=1.0, shift_ends_at=clock.now + 4 * H
    )

    assert engine._next_gap(300) == whole._next_gap(300) == 300  # no raise


def test_after_a_tick_the_loop_sleeps_the_paced_gap(tmp_path, monkeypatch, clock):
    _budget_config(
        tmp_path, monkeypatch, clock, dailyRequestCap=1000, reserveRequests=50
    )
    _spend(750)
    engine = _engine(tmp_path, monkeypatch, shift_ends_at=clock.now + 4 * H)
    sleeps = []

    async def tick():
        _spend(8, engine.agent_id)

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        engine._running = False

    monkeypatch.setattr(engine, "_tick", tick)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    engine._running = True

    asyncio.run(engine._run_ticks(120, "loop"))

    assert engine._requests_per_tick == 8
    assert len(sleeps) == 1 and sleeps[0] > 400


# ── reporting ──


def test_get_info_reports_the_budget_only_when_there_is_one(
    tmp_path, monkeypatch, clock
):
    plain = _engine(tmp_path, monkeypatch)
    assert plain.get_info()["request_budget"] == {}

    _budget_config(tmp_path, monkeypatch, clock)
    ends = clock.now + H
    metered = _engine(tmp_path, monkeypatch, shift_ends_at=ends, shift_id="s-1")
    _spend(5, metered.agent_id)
    info = metered.get_info()["request_budget"]

    assert info["shift_ends_at"] == ends and info["shift_id"] == "s-1"
    assert info["used"] == 5 and info["this_run_requests"] == 5
    assert info["cap"] == 1000 and info["holding"] is False


def test_the_tick_timeout_defaults_to_agent_config_and_a_run_setting_wins(
    tmp_path, monkeypatch
):
    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps({"tickTimeoutSec": 900}))
    monkeypatch.setenv(agent_config.CONFIG_ENV, str(path))
    agent_config.reset_cache()

    assert _engine(tmp_path, monkeypatch).get_info()["tick_timeout_sec"] == 900
    assert (
        _engine(tmp_path, monkeypatch, tick_timeout_sec=60).get_info()[
            "tick_timeout_sec"
        ]
        == 60
    )
