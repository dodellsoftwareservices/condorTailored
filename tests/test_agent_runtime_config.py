"""``agent-config.json``: the file that says how much an unattended agent may do.

Three properties matter more than any single value:

* with no file the resolved config is Condor's behaviour from before the file
  existed, so installing the module changes nothing on its own;
* a setting this build does not understand is *reported*, never dropped, since a
  setting that does nothing reads, to whoever wrote it, like one that works;
* the shipped file only ever pre-approves calls the gate classifies as
  dangerous by name, so ``alwaysAllow`` cannot rot into a list of dead entries
  or quietly grow to cover the DEX signing tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from condor import agent_config
from condor.agent_config import (
    AgentConfigError,
    AgentRuntimeConfig,
    Budget,
    Thinking,
    get_agent_config,
    get_loaded,
    load,
    reset_cache,
)
from condor.runtime import danger

REPO = Path(agent_config.__file__).resolve().parent.parent
SHIPPED = REPO / agent_config.FILENAME


def _write(tmp_path: Path, monkeypatch, body) -> Path:
    path = tmp_path / "agent-config.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    monkeypatch.setenv(agent_config.CONFIG_ENV, str(path))
    reset_cache()
    return path


# ── defaults: no file, nothing changes ──


def test_no_file_resolves_to_the_pre_existing_behaviour():
    loaded = get_loaded()
    cfg = loaded.config

    assert loaded.path is None and loaded.error == ""
    assert cfg.max_steps is None  # pydantic-ai's own limit stays in force
    assert cfg.approval_policy == "always" and cfg.always_allow == ()
    assert cfg.thinking.openrouter_reasoning() is None
    assert cfg.max_output_tokens is None and cfg.tick_timeout_sec is None
    assert cfg.budget.enabled is False


def test_no_file_leaves_every_run_limit_to_the_library():
    cfg = get_agent_config()

    assert cfg.run_limit(unattended=False) is None
    assert cfg.run_limit(unattended=True, budget_left=5) is None  # budget is off
    assert cfg.never_ask is False


# ── the shipped file ──


def test_the_shipped_file_is_valid_and_says_what_the_owner_asked_for(monkeypatch):
    monkeypatch.setenv(agent_config.CONFIG_ENV, str(SHIPPED))
    reset_cache()
    loaded = get_loaded(strict=True)
    cfg = loaded.config

    assert loaded.unrecognized == ()
    assert cfg.max_steps == 1000
    assert cfg.thinking.effort == "high"
    assert cfg.budget.enabled and cfg.budget.daily_request_cap == 1000
    assert cfg.model.startswith("openrouter:")


def test_the_shipped_schema_is_the_models_schema():
    on_disk = json.loads((REPO / "agent-config.schema.json").read_text())

    assert on_disk == agent_config.json_schema(), (
        "agent-config.schema.json is stale: regenerate it with "
        "`uv run python -m condor.agent_config --schema > agent-config.schema.json`"
    )


def test_every_always_allow_entry_names_a_call_the_gate_calls_dangerous():
    """No dead entries, and nothing that signs for a DEX wallet."""
    never = {"place_order", "execute_swap", "manage_clmm", "manage_amm"}
    raw = json.loads(SHIPPED.read_text())

    for entry in raw["alwaysAllow"]:
        tool, _, action = entry.partition(":")
        assert tool not in never, f"{entry}: signing tools are never pre-approved"
        if tool == "manage_bots":
            assert action in danger.DANGEROUS_BOT_ACTIONS, entry
        elif tool == "control_agent":
            assert action in danger.DANGEROUS_CONTROL_ACTIONS, entry
        else:
            assert not action, f"{entry}: only manage_bots/control_agent take an action"
            assert tool in danger.DANGEROUS_TOOLS, f"{entry} is not a dangerous tool"


# ── parsing and validation ──


def test_camel_case_keys_load(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {
            "maxSteps": 321,
            "approvalPolicy": "never",
            "alwaysAllow": ["stop_executor"],
            "thinking": {"effort": "low", "exclude": True},
            "maxOutputTokens": 4096,
            "tickTimeoutSec": 120,
            "budget": {
                "enabled": True,
                "requestsPerMinute": 7,
                "perRunRequestCap": None,
            },
        },
    )
    cfg = get_agent_config()

    assert cfg.max_steps == 321 and cfg.never_ask
    assert cfg.always_allow == ("stop_executor",)
    assert cfg.thinking.openrouter_reasoning() == {"effort": "low", "exclude": True}
    assert cfg.max_output_tokens == 4096 and cfg.tick_timeout_sec == 120
    assert (
        cfg.budget.requests_per_minute == 7 and cfg.budget.per_run_request_cap is None
    )


def test_unknown_keys_are_reported_not_dropped(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {"maxStep": 9, "thinking": {"effrot": "high"}, "budget": {"daylyCap": 1}},
    )

    assert get_loaded().unrecognized == (
        "maxStep",
        "thinking.effrot",
        "budget.daylyCap",
    )


def test_comment_and_schema_keys_are_exempt(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"$schema": "x", "_about": "notes", "maxSteps": 5})

    assert get_loaded().unrecognized == ()


def test_effort_and_max_tokens_are_alternatives():
    with pytest.raises(ValueError, match="alternatives"):
        Thinking(effort="high", max_tokens=100)

    assert Thinking(max_tokens=2000).openrouter_reasoning() == {"max_tokens": 2000}
    # "none" disables reasoning and may sit beside nothing else, but is not a conflict
    assert Thinking(effort="none").openrouter_reasoning() == {"effort": "none"}


def test_the_reserve_must_fit_inside_the_cap():
    with pytest.raises(ValueError, match="reserveRequests"):
        Budget(daily_request_cap=100, reserve_requests=100)

    assert Budget(daily_request_cap=100, reserve_requests=40).spendable == 60


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("{not json", "cannot read"),
        ("[1, 2]", "top level must be a JSON object"),
        ('{"maxSteps": 0}', "maxSteps"),
        ('{"approvalPolicy": "sometimes"}', "approvalPolicy"),
        ('{"thinking": {"effort": "extreme"}}', "thinking.effort"),
    ],
)
def test_a_bad_file_is_an_error_strict_and_the_defaults_otherwise(
    tmp_path, monkeypatch, body, fragment
):
    _write(tmp_path, monkeypatch, body)

    with pytest.raises(AgentConfigError, match=fragment):
        get_loaded(strict=True)

    # A typo must not take the bot down: non-strict falls back, and says so.
    loaded = get_loaded()
    assert loaded.error and fragment in loaded.error
    assert loaded.config == AgentRuntimeConfig()


# ── environment overrides and the cache ──


def test_environment_overrides_beat_the_file(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"maxSteps": 10, "thinking": {"effort": "low"}})
    monkeypatch.setenv("CONDOR_MAX_STEPS", "77")
    monkeypatch.setenv("CONDOR_THINKING_EFFORT", "xhigh")
    monkeypatch.setenv("CONDOR_APPROVAL_POLICY", "never")
    reset_cache()
    cfg = get_agent_config()

    assert cfg.max_steps == 77 and cfg.thinking.effort == "xhigh" and cfg.never_ask


def test_a_non_numeric_max_steps_variable_is_an_error(monkeypatch):
    monkeypatch.setenv("CONDOR_MAX_STEPS", "lots")

    with pytest.raises(AgentConfigError, match="CONDOR_MAX_STEPS"):
        load()


def test_an_edit_applies_on_the_next_read_without_a_restart(tmp_path, monkeypatch):
    path = _write(tmp_path, monkeypatch, {"maxSteps": 10})
    assert get_agent_config().max_steps == 10

    path.write_text(json.dumps({"maxSteps": 20}))
    # mtime granularity: force a distinct stamp so the test is not clock-bound
    stat = path.stat()
    import os

    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000))

    assert get_agent_config().max_steps == 20


# ── derived behaviour ──


def test_max_steps_bounds_every_run_and_the_budget_only_the_unattended(
    tmp_path, monkeypatch
):
    _write(
        tmp_path,
        monkeypatch,
        {"maxSteps": 1000, "budget": {"enabled": True, "perRunRequestCap": 100}},
    )
    cfg = get_agent_config()

    # A human at a chat is not second-guessed.
    assert cfg.run_limit(unattended=False, budget_left=3) == 1000
    # A loop tick is held to the per-run cap...
    assert cfg.run_limit(unattended=True, budget_left=500) == 100
    # ...and to whatever is left of the day, but never below one request.
    assert cfg.run_limit(unattended=True, budget_left=40) == 40
    assert cfg.run_limit(unattended=True, budget_left=0) == 1


def test_per_run_cap_none_lets_max_steps_decide(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {"maxSteps": 1000, "budget": {"enabled": True, "perRunRequestCap": None}},
    )

    assert get_agent_config().run_limit(unattended=True, budget_left=5000) == 1000


def test_always_allow_matches_a_tool_or_a_tool_action_pair(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {"alwaysAllow": ["stop_executor", "manage_bots:deploy"]},
    )
    cfg = get_agent_config()

    assert cfg.tool_always_allowed("stop_executor", None)
    assert cfg.tool_always_allowed("manage_bots", "deploy")
    assert not cfg.tool_always_allowed("manage_bots", "stop_bot")
    assert not cfg.tool_always_allowed("manage_bots", None)
    assert not cfg.tool_always_allowed("place_order", None)
