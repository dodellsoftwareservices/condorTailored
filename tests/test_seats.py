"""``condor.agents.seats``: one table of the model each seat actually runs.

The report is only worth reading if it is the runtime's own answer, so the model
columns are checked against the functions the engine and the shift runner call,
not against a second copy of their rules.
"""

import json
from types import SimpleNamespace

import pytest

from condor import agent_config, shift
from condor.agents import seats as seats_module
from condor.agents.agent import AgentStore
from condor.agents.agent_run import fallback_agent_key
from condor.agents.engine import resolve_agent_key
from condor.agents.seats import Seat, log_seats, render, resolve_seats
from condor.llm import options


@pytest.fixture(autouse=True)
def _no_fallback_env(monkeypatch):
    monkeypatch.delenv("AGENT_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("CONSULT_FALLBACK_MODEL", raising=False)


def _seat(tools):
    return Seat(name="x", kind="agent", model="m", source="s", tools=tools)


def _by_name(seats):
    return {s.name: s for s in seats}


def _write_config(tmp_path, monkeypatch, body):
    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps(body))
    monkeypatch.setenv(agent_config.CONFIG_ENV, str(path))
    agent_config.reset_cache()


# ── the tool profile ──


def test_an_empty_allowlist_is_unrestricted():
    assert _seat(None).unrestricted
    assert not _seat(None).read_only


def test_an_allowlist_of_reads_is_read_only():
    seat = _seat(("get_portfolio", "mcp__condor__list_executors"))

    assert seat.read_only
    assert seat.mutating_tools == ()


@pytest.mark.parametrize(
    "tool",
    [
        "manage_bots",
        "stop_executor",
        "mcp__condor__create_grid_executor",
        "place_order",
    ],
)
def test_an_allowlisted_mutation_is_named(tool):
    seat = _seat(("get_portfolio", tool))

    assert not seat.read_only
    assert seat.mutating_tools == (tool,)


# ── the model column is the runtime's own answer ──


def test_an_agent_with_a_key_reports_it_and_the_engine_agrees():
    agent = AgentStore().create("Quant", agent_key="openrouter:moonshotai/kimi")

    seat = _by_name(resolve_seats())["quant"]

    assert seat.model == "openrouter:moonshotai/kimi"
    assert seat.model == resolve_agent_key({}, SimpleNamespace(agent_key=""), agent)


def test_an_agent_without_a_key_inherits_the_chat_default():
    AgentStore().create("Scribe", agent_key="")

    seat = _by_name(resolve_seats())["scribe"]

    assert seat.model == options.DEFAULT_AGENT
    assert "chat default" in seat.source


def test_the_shift_seat_is_the_one_agent_config_reaches(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"model": "openrouter:vendor/some-model:free"})

    shift_seat = _by_name(resolve_seats())["shift"]
    cfg = agent_config.get_agent_config()

    assert shift_seat.model == "openrouter:vendor/some-model:free"
    assert (shift_seat.model, "agent-config.json") == shift.resolve_model(
        SimpleNamespace(model=None), cfg
    )


def test_the_shift_seat_says_so_when_nothing_names_a_model():
    shift_seat = _by_name(resolve_seats())["shift"]

    assert shift_seat.model == ""
    assert "nowhere" in shift_seat.source


def test_the_chat_seat_comes_first_and_every_agent_is_listed_once():
    store = AgentStore()
    store.create("Beta")
    store.create("Alpha")

    names = [s.name for s in resolve_seats()]

    assert names[:2] == ["chat", "shift"]
    assert names[2:] == sorted(names[2:])
    assert {"alpha", "beta"} <= set(names)
    assert len(names) == len(set(names))


# ── the fallback ──


def test_the_fallback_defaults_to_claude_code():
    assert fallback_agent_key() == "claude-code"


def test_the_fallback_env_wins_and_the_old_name_is_still_honoured(monkeypatch):
    monkeypatch.setenv("CONSULT_FALLBACK_MODEL", "gemini")
    assert fallback_agent_key() == "gemini"

    monkeypatch.setenv("AGENT_FALLBACK_MODEL", "codex")
    assert fallback_agent_key() == "codex"


def test_an_empty_fallback_env_turns_the_fallback_off(monkeypatch):
    monkeypatch.setenv("AGENT_FALLBACK_MODEL", "")

    assert fallback_agent_key() == ""
    assert "none (the run fails)" in render(resolve_seats())


# ── the table ──


def test_the_table_flags_what_a_reader_must_not_miss():
    AgentStore().create("Trader")
    AgentStore().create("Analyst", tools=["get_portfolio"])
    AgentStore().create("Operator", tools=["manage_bots"])

    table = render(resolve_seats())
    rows = {line.split()[0]: line for line in table.splitlines() if line.split()}

    assert "UNRESTRICTED" in rows["trader"]
    assert "read-only (1)" in rows["analyst"]
    assert "can mutate: manage_bots" in rows["operator"]
    assert "fallback when a backend is down: claude-code" in table


def test_a_seat_with_no_model_reads_none():
    table = render([Seat(name="shift", kind="shift", model="", source="nowhere")])

    assert "NONE" in table


def test_logging_the_table_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(seats_module, "resolve_seats", boom)

    log_seats()
