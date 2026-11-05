"""The advisory analyst panel: free-only, tool-less, bounded, and advisory."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from condor.sim_exchange import analysts as A
from condor.web.auth import get_current_user
from condor.web.models import WebUser
from condor.web.routes import sim_exchange as routes


def candles(closes, *, forming=True):
    rows = [
        {"ts": i, "open": c, "high": c * 1.01, "low": c * 0.99, "close": str(c)}
        for i, c in enumerate(closes)
    ]
    if forming:
        rows.append({"ts": len(rows), "open": 1, "high": 1, "low": 1, "close": "1"})
    return rows


FACTS = [{"symbol": "BTC-USD", "daily": {"last_close": 100}}]


class Recorder:
    """A runner that records calls and answers per model."""

    def __init__(self, replies=None, delay=0.0):
        self.calls = []
        self.replies = replies or {}
        self.delay = delay

    async def __call__(self, model, system, user, *, caller):
        self.calls.append(
            {"model": model, "system": system, "user": user, "caller": caller}
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies.get(model, "STANCE: neutral - flat")
        if isinstance(reply, Exception):
            raise reply
        return reply


def panel(runner, **kw):
    return A.AnalystPanel(runner, **kw)


def run(coro):
    return asyncio.run(coro)


# ── statistics ──


def test_the_still_forming_candle_is_never_part_of_the_statistics():
    closes = list(range(100, 140))  # 40 completed bars, steadily rising
    stats = A.summarize_candles(candles(closes))
    assert stats["completed_bars"] == 40
    assert stats["last_close"] == 139  # not the forming bar's close of 1
    assert stats["return_pct"]["1_bars"] == pytest.approx(0.72, abs=0.01)
    assert stats["close_vs_sma20_pct"] > 0
    assert stats["sma20_vs_5_bars_ago_pct"] > 0
    assert stats["pct_below_20_high"] < 0 and stats["pct_above_20_low"] > 0
    assert stats["recent_closes"][-1] == 139 and len(stats["recent_closes"]) == 30


def test_a_falling_series_reads_as_below_a_falling_average():
    stats = A.summarize_candles(candles(list(range(140, 100, -1))))
    assert stats["close_vs_sma20_pct"] < 0
    assert stats["sma20_vs_5_bars_ago_pct"] < 0


def test_too_few_bars_is_reported_not_guessed():
    out = A.summarize_candles(candles([1, 2, 3]))
    assert out == {"completed_bars": 3, "note": "too few completed bars"}


def test_garbage_candle_rows_are_skipped():
    rows = candles(list(range(100, 130)))
    rows.insert(3, {"close": "not-a-number"})
    rows.insert(4, {"close": None})
    assert A.summarize_candles(rows)["completed_bars"] == 30


def test_facts_carry_costs_and_funding_without_the_prose_notes():
    daily = {
        "mid": "100",
        "spread_bps": "1.2",
        "round_trip_cost_bps": {"taker_both_ways_before_slippage": "10", "note": "x"},
        "funding": {"annualized_pct": "8.0", "note": "longs pay"},
        "candles": {"rows": candles(list(range(100, 130)))},
    }
    h4 = {"candles": {"rows": candles(list(range(100, 130)))}}
    facts = A.build_facts("futures", "BTC-USD", daily, daily, h4)
    assert facts["round_trip_cost_bps"] == {"taker_both_ways_before_slippage": "10"}
    assert facts["funding"] == {"annualized_pct": "8.0"}
    assert (
        facts["daily"]["completed_bars"] == facts["four_hour"]["completed_bars"] == 30
    )
    json.dumps(facts)  # the brief must serialise


# ── the free-only invariant ──


@pytest.mark.parametrize(
    "model",
    [
        "openrouter:deepseek/deepseek-v4.1-flash-1017",  # paid
        "openrouter:anthropic/claude-opus-4-1",
        "openai:gpt-4o",  # not OpenRouter at all
        "claude-acp:opus",
    ],
)
def test_the_panel_refuses_any_model_that_could_spend_the_paid_budget(model):
    with pytest.raises(ValueError, match="':free'"):
        A.AnalystPanel(Recorder(), models=(model,))


def test_the_worst_case_consult_fits_inside_the_tool_timeout():
    # sim_consult_analysts waits 150s; every model of every lens may time out in turn.
    assert len(A.FREE_ANALYST_MODELS) * A.ANALYST_TIMEOUT_SEC < 150


def test_the_default_analysts_are_free_nemotron_models():
    assert all(
        m.startswith("openrouter:nvidia/nemotron") for m in A.FREE_ANALYST_MODELS
    )
    A.assert_free(A.FREE_ANALYST_MODELS)
    assert 2 <= len(A.LENSES) <= A.MAX_ANALYSTS == 3


# ── consulting ──


def test_each_lens_gets_its_own_prompt_and_only_the_facts_not_the_account():
    rec = Recorder()
    out = run(panel(rec).consult("spot", FACTS, "is a pullback entry valid?"))
    assert out["advisory"] is True and out["cached"] is False
    assert [a["lens"] for a in out["analysts"]] == ["trend", "cost_risk", "counter"]
    assert all(a["status"] == "ok" for a in out["analysts"])
    assert len({c["system"] for c in rec.calls}) == 3  # distinct roles
    assert {c["caller"] for c in rec.calls} == {
        "sim_analyst.spot.trend",
        "sim_analyst.spot.cost_risk",
        "sim_analyst.spot.counter",
    }
    user = rec.calls[0]["user"]
    assert "BTC-USD" in user and "is a pullback entry valid?" in user
    assert "cash" not in user and "equity" not in user  # no account data reaches them
    assert "cannot trade" in rec.calls[0]["system"]
    assert out["symbols"] == ["BTC-USD"]
    assert "never cite an analyst as the only reason" in out["note"]


def test_the_analysts_run_in_parallel_not_one_after_another():
    rec = Recorder(delay=0.15)
    started = time.monotonic()
    run(panel(rec).consult("spot", FACTS))
    assert time.monotonic() - started < 0.40  # three serial calls would take 0.45


def test_at_most_three_lenses_and_unknown_ones_are_rejected():
    out = run(panel(Recorder()).consult("spot", FACTS, lenses=["trend", "trend"]))
    assert [a["lens"] for a in out["analysts"]] == ["trend"]
    with pytest.raises(ValueError, match="Unknown analyst lens"):
        run(panel(Recorder()).consult("spot", FACTS, lenses=["oracle"]))
    with pytest.raises(ValueError, match="at least one symbol"):
        run(panel(Recorder()).consult("spot", []))


def test_a_failing_model_falls_through_to_the_next_free_model():
    first, second = A.FREE_ANALYST_MODELS
    rec = Recorder({first: RuntimeError("rate limited")})
    out = run(panel(rec).consult("spot", FACTS, lenses=["trend"]))
    assert out["analysts"][0]["status"] == "ok"
    assert out["analysts"][0]["model"] == second.partition(":")[2]
    assert [c["model"] for c in rec.calls] == [first, second]


def test_one_analyst_failing_everywhere_never_fails_the_consult():
    models = ("openrouter:nvidia/a:free", "openrouter:nvidia/b:free")
    boom = RuntimeError("down")
    rec = Recorder({m: boom for m in models})
    out = run(panel(rec, models=models).consult("spot", FACTS, lenses=["trend"]))
    assert out["analysts"][0]["status"] == "unavailable"
    assert "Decide without this lens" in out["analysts"][0]["brief"]
    assert "down" not in json.dumps(out)  # errors are not echoed to the desk


def test_a_hung_analyst_times_out_instead_of_stalling_the_desk():
    rec = Recorder(delay=5)
    started = time.monotonic()
    out = run(panel(rec, timeout=0.05).consult("spot", FACTS, lenses=["trend"]))
    assert time.monotonic() - started < 1.0
    assert out["analysts"][0]["status"] == "unavailable"


def test_an_empty_reply_counts_as_unavailable():
    models = ("openrouter:nvidia/a:free",)
    out = run(
        panel(Recorder({models[0]: "   "}), models=models).consult(
            "spot", FACTS, lenses=["trend"]
        )
    )
    assert out["analysts"][0]["status"] == "unavailable"


def test_a_runaway_reply_is_clipped():
    models = ("openrouter:nvidia/a:free",)
    out = run(
        panel(Recorder({models[0]: "x" * 50_000}), models=models).consult(
            "spot", FACTS, lenses=["trend"]
        )
    )
    assert len(out["analysts"][0]["brief"]) == A.MAX_BRIEF_CHARS


def test_the_question_is_truncated_before_it_reaches_a_model():
    rec = Recorder()
    run(panel(rec).consult("spot", FACTS, "q" * 5000, lenses=["trend"]))
    assert "q" * A.MAX_QUESTION_CHARS in rec.calls[0]["user"]
    assert "q" * (A.MAX_QUESTION_CHARS + 1) not in rec.calls[0]["user"]


# ── the rate limit that stops a looping model burning the free tier ──


def test_a_second_consult_inside_the_window_returns_the_cached_briefs():
    now = [1000.0]
    rec = Recorder()
    p = panel(rec, clock=lambda: now[0], min_interval=300)
    first = run(p.consult("spot", FACTS))
    now[0] += 120
    second = run(p.consult("spot", FACTS))
    assert len(rec.calls) == 3  # nobody was asked twice
    assert second["cached"] is True and second["age_sec"] == 120
    assert second["next_fresh_consult_in_sec"] == 180
    assert second["analysts"] == first["analysts"]
    now[0] += 200  # past the window
    third = run(p.consult("spot", FACTS))
    assert third["cached"] is False and len(rec.calls) == 6


def test_each_desk_has_its_own_window():
    rec = Recorder()
    p = panel(rec)
    run(p.consult("spot", FACTS))
    run(p.consult("futures", FACTS))
    assert len(rec.calls) == 6


def test_a_consult_where_nobody_answered_is_not_cached_so_a_retry_can_succeed():
    models = ("openrouter:nvidia/a:free",)
    rec = Recorder({models[0]: RuntimeError("down")})
    p = panel(rec, models=models)
    run(p.consult("spot", FACTS, lenses=["trend"]))
    rec.replies = {}
    again = run(p.consult("spot", FACTS, lenses=["trend"]))
    assert again["cached"] is False and again["analysts"][0]["status"] == "ok"


# ── the real runner: no tools, budget-governed ──


def test_the_default_runner_builds_a_tool_less_unattended_client_and_always_stops_it(
    monkeypatch,
):
    events = []

    class Client:
        async def start(self):
            events.append("start")

        async def prompt(self, text):
            events.append(("prompt", text))
            raise RuntimeError("model fell over")

        async def stop(self):
            events.append("stop")

    seen = {}

    def build(model, **kwargs):
        seen["model"], seen["kwargs"] = model, kwargs
        return Client()

    monkeypatch.setattr("condor.runtime.llm_client.build_llm_client", build)
    with pytest.raises(RuntimeError):
        run(
            A.default_runner(
                "openrouter:nvidia/x:free",
                "SYS",
                "USER",
                caller="sim_analyst.spot.trend",
            )
        )
    assert events == [
        "start",
        ("prompt", "USER"),
        "stop",
    ]  # stopped despite the failure
    assert seen["kwargs"]["system_prompt"] == "SYS"
    assert seen["kwargs"]["budget_caller"] == "sim_analyst.spot.trend"
    assert seen["kwargs"]["unattended"] is True
    assert seen["kwargs"]["reasoning_effort"] == A.ANALYST_REASONING_EFFORT
    assert "mcp_servers" not in seen["kwargs"]  # nothing for the model to call
    assert "allowed_tools" not in seen["kwargs"]


# ── the route ──


@pytest.fixture
def api(monkeypatch):
    snapshots = {"spot": AsyncMock(), "futures": AsyncMock()}

    def snap(minutes_rows):
        async def inner(symbol, candles_n, minutes):
            return {
                "mid": "100",
                "spread_bps": "1",
                "round_trip_cost_bps": {"taker_both_ways_before_slippage": "80"},
                "candles": {"minutes": minutes, "rows": minutes_rows},
            }

        return inner

    rows = candles(list(range(100, 140)))
    spot = SimpleNamespace(
        snapshot=AsyncMock(side_effect=snap(rows)),
        candles=AsyncMock(return_value=rows),
    )
    fut = SimpleNamespace(
        snapshot=AsyncMock(side_effect=snap(rows)),
        candles=AsyncMock(return_value=rows),
    )
    rec = Recorder()
    monkeypatch.setattr(routes, "get_sim_service", lambda: spot)
    monkeypatch.setattr(routes, "get_fut_service", lambda: fut)
    monkeypatch.setattr(routes, "_analyst_panel", A.AnalystPanel(rec))
    monkeypatch.setattr(
        "condor.web.auth.get_config_manager",
        lambda: SimpleNamespace(is_admin=lambda uid: uid == 1),
    )
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: WebUser(id=1, role="admin")
    client = TestClient(app)
    client.spot, client.fut, client.rec = spot, fut, rec
    return client


def test_the_route_fetches_daily_and_four_hour_candles_itself(api):
    res = api.post(
        "/sim/kraken/analysts", json={"desk": "spot", "symbols": ["BTC-USD", "ETH-USD"]}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["symbols"] == ["BTC-USD", "ETH-USD"] and len(body["analysts"]) == 3
    # One full snapshot per symbol (daily, with the ticker and book); the 4-hour
    # candles come from a candles-only call, not a second full snapshot.
    assert [c.args[2] for c in api.spot.snapshot.await_args_list] == [1440, 1440]
    assert [c.args[1:] for c in api.spot.candles.await_args_list] == [(240, 60)] * 2
    assert not api.fut.snapshot.await_count  # the other desk's service is untouched
    facts = json.loads(api.rec.calls[0]["user"].split("\n")[1])
    assert facts[0]["daily"]["completed_bars"] == 40


def test_the_futures_desk_reads_the_futures_service(api):
    res = api.post(
        "/sim/kraken/analysts", json={"desk": "futures", "symbols": ["BTC-USD"]}
    )
    assert res.status_code == 200
    assert api.fut.snapshot.await_count == 1 and api.fut.candles.await_count == 1
    assert not api.spot.snapshot.await_count


@pytest.mark.parametrize(
    "body",
    [
        {"desk": "spot", "symbols": []},
        {"desk": "spot", "symbols": ["A", "B", "C", "D"]},
        {"desk": "options", "symbols": ["BTC-USD"]},
        {"desk": "spot", "symbols": ["BTC-USD"], "question": "q" * 301},
        {"desk": "spot", "symbols": ["BTC-USD"], "lenses": ["oracle"]},
    ],
)
def test_the_route_rejects_malformed_requests(api, body):
    assert api.post("/sim/kraken/analysts", json=body).status_code == 422
    assert not api.rec.calls


def test_only_an_admin_can_consult(api):
    api.app.dependency_overrides[get_current_user] = lambda: WebUser(id=2, role="user")
    res = api.post(
        "/sim/kraken/analysts", json={"desk": "spot", "symbols": ["BTC-USD"]}
    )
    assert res.status_code == 403 and not api.rec.calls


def test_a_model_that_echoes_its_answer_twice_is_cut_at_the_repeat():
    models = ("openrouter:nvidia/a:free",)
    once = "STANCE: unclear - mixed\nEVIDENCE: a number\nINVALIDATION: a level"
    out = run(
        panel(Recorder({models[0]: once + once}), models=models).consult(
            "spot", FACTS, lenses=["trend"]
        )
    )
    assert out["analysts"][0]["brief"] == once
    assert out["analysts"][0]["brief"].count("STANCE:") == 1
