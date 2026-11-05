"""``alwaysAllow`` / ``approvalPolicy`` in agent-config.json, at the confirmation gate.

The shared permission callback makes a human answer every dangerous tool call.
The config can say "not this one" — and may only ever skip the *wait for a
human*. Everything that refuses a call outright (a raw controller-code push),
everything that cannot read a call's arguments, and everything that signs with a
wallet stays exactly as strict as it was.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from condor import agent_config
from condor.runtime import confirmations as c
from condor.runtime.confirmations import ConfirmationRegistry, build_permission_callback

OPTIONS = [{"optionId": "allow", "kind": "allow_once"}, {"optionId": "deny"}]


class Human:
    """Stands in for the dashboard: records whether anyone was asked, and says no."""

    def __init__(self, answer: bool = False):
        self.answer = answer
        self.asked: list = []

    async def deliver(self, pending):
        self.asked.append(pending)
        await c._registry.resolve(pending.id, approved=self.answer, by_user_id=1)


def _config(tmp_path, monkeypatch, body: dict) -> None:
    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps(body))
    monkeypatch.setenv(agent_config.CONFIG_ENV, str(path))
    agent_config.reset_cache()


def _call(tool_call: dict, human: Human) -> dict:
    async def go():
        c._registry = ConfirmationRegistry()
        try:
            callback = build_permission_callback(
                "web:1:main", 1, channels=[human], timeout_seconds=5
            )
            return await callback(tool_call, OPTIONS)
        finally:
            c._registry = ConfirmationRegistry()

    return asyncio.run(go())


def bots(action, **extra):
    return {"tool": "manage_bots", "input": {"action": action, **extra}}


def selected(result) -> bool:
    return result.get("outcome", {}).get("outcome") == "selected"


# ── defaults ──


def test_with_no_config_a_dangerous_call_waits_for_a_human():
    human = Human(answer=False)

    result = _call(bots("deploy", bot_name="x"), human)

    assert len(human.asked) == 1
    assert not selected(result)


# ── alwaysAllow ──


def test_a_listed_tool_is_approved_without_asking_anyone(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {"alwaysAllow": ["stop_executor"]})
    human = Human()

    result = _call({"tool": "stop_executor", "input": {"executor_id": "e1"}}, human)

    assert selected(result) and human.asked == []


def test_a_tool_action_pair_approves_that_action_only(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {"alwaysAllow": ["manage_bots:deploy"]})
    human = Human()

    deployed = _call(bots("deploy", bot_name="x"), human)
    assert selected(deployed) and human.asked == []

    stopped = _call(bots("stop_bot", bot_name="x"), human)
    assert not selected(stopped) and len(human.asked) == 1


def test_the_acp_wire_name_is_matched_like_the_bare_one(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {"alwaysAllow": ["manage_bots:deploy"]})
    human = Human()

    result = _call(
        {"tool": "mcp__mcp-hummingbot__manage_bots", "input": {"action": "deploy"}},
        human,
    )

    assert selected(result) and human.asked == []


def test_a_call_whose_arguments_cannot_be_read_still_goes_to_a_human(
    tmp_path, monkeypatch
):
    # The tool name alone is on the list: that must not cover an unreadable call.
    _config(tmp_path, monkeypatch, {"alwaysAllow": ["manage_bots", "stop_executor"]})
    human = Human()

    for call in (
        {"tool": "manage_bots"},  # no input at all
        {"tool": "manage_bots", "input": "{not json"},
        {"tool": "stop_executor", "input": ["a", "list"]},
    ):
        assert not selected(_call(call, human))

    assert len(human.asked) == 3


def test_a_tool_the_list_does_not_name_still_asks(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {"alwaysAllow": ["stop_executor"]})
    human = Human()

    result = _call(
        {
            "tool": "create_grid_executor",
            "input": {"controller_id": "c", "total_amount_quote": 5},
        },
        human,
    )

    assert not selected(result) and len(human.asked) == 1


# ── approvalPolicy ──


def test_never_ask_approves_the_dangerous_calls(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {"approvalPolicy": "never"})
    human = Human()

    for call in (
        bots("deploy", bot_name="x"),
        {"tool": "stop_executor", "input": {"executor_id": "e"}},
        {"tool": "place_order", "input": {"trading_pair": "ETH-USDT"}},
    ):
        assert selected(_call(call, human))

    assert human.asked == []


@pytest.mark.parametrize(
    "tool, action",
    [("execute_swap", None), ("manage_clmm", "open"), ("manage_amm", "add_liquidity")],
)
def test_never_ask_does_not_cover_the_wallet_signing_tools(
    tmp_path, monkeypatch, tool, action
):
    _config(tmp_path, monkeypatch, {"approvalPolicy": "never"})
    human = Human()
    args = {"amount": 1, **({"action": action} if action else {})}

    result = _call({"tool": tool, "input": args}, human)

    assert not selected(result) and len(human.asked) == 1


def test_a_signing_tool_can_still_be_named_one_by_one(tmp_path, monkeypatch):
    _config(
        tmp_path,
        monkeypatch,
        {"approvalPolicy": "never", "alwaysAllow": ["execute_swap"]},
    )
    human = Human()

    assert selected(_call({"tool": "execute_swap", "input": {"amount": 1}}, human))
    assert human.asked == []


def test_dangerous_only_means_always_here_because_only_dangerous_calls_ask(
    tmp_path, monkeypatch
):
    _config(tmp_path, monkeypatch, {"approvalPolicy": "dangerous-only"})
    human = Human()

    assert not selected(_call(bots("deploy", bot_name="x"), human))
    assert len(human.asked) == 1


# ── what the config can never override ──


def test_a_raw_controller_code_push_is_refused_whatever_the_config_says(
    tmp_path, monkeypatch
):
    _config(
        tmp_path,
        monkeypatch,
        {"approvalPolicy": "never", "alwaysAllow": ["manage_controllers"]},
    )
    human = Human()
    push = {
        "tool": "manage_controllers",
        "input": {
            "action": "upsert",
            "target": "controller",
            "name": "x",
            "content": "code",
        },
    }

    result = _call(push, human)

    assert result["outcome"]["outcome"] == "cancelled" and result.get("reason")
    assert human.asked == []  # refused outright, never put to a human either


def test_a_config_that_cannot_be_read_never_approves(tmp_path, monkeypatch):
    human = Human()

    def broken():
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(agent_config, "get_agent_config", broken)

    assert not selected(_call(bots("deploy", bot_name="x"), human))
    assert len(human.asked) == 1


def test_a_call_that_is_not_dangerous_never_reaches_the_config(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {})
    human = Human()

    result = _call(
        {"tool": "get_prices", "input": {"connector_name": "binance"}}, human
    )

    assert selected(result) and human.asked == []
