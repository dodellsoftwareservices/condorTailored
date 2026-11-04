"""`PydanticAIClient._extend_message_history` bounds history growth.

A long-lived chat session reuses one client across every turn, resending the
whole accumulated history on each one — unbounded, O(N^2) cumulative resend
cost on a paid model (system review 2026-10-31, finding #22). This pins that
growth is capped at whole turns, never a message mid-turn, since cutting
between a tool call and its result would hand the model a dangling call with
no answer.
"""

from __future__ import annotations

from condor.acp.pydantic_ai_client import MAX_HISTORY_TURNS, PydanticAIClient


def _client() -> PydanticAIClient:
    return PydanticAIClient("openai:gpt-4o")


def test_history_is_unbounded_under_the_cap():
    client = _client()
    for i in range(MAX_HISTORY_TURNS - 1):
        client._extend_message_history([f"turn-{i}-a", f"turn-{i}-b"])

    assert len(client._history_turn_lens) == MAX_HISTORY_TURNS - 1
    assert client._message_history[0] == "turn-0-a"
    assert client._message_history[-1] == f"turn-{MAX_HISTORY_TURNS - 2}-b"


def test_the_oldest_whole_turn_is_dropped_once_the_cap_is_crossed():
    client = _client()
    for i in range(MAX_HISTORY_TURNS):
        client._extend_message_history([f"turn-{i}"])
    assert client._message_history[0] == "turn-0"

    client._extend_message_history([f"turn-{MAX_HISTORY_TURNS}"])

    assert len(client._history_turn_lens) == MAX_HISTORY_TURNS
    assert "turn-0" not in client._message_history
    assert client._message_history[0] == "turn-1"
    assert client._message_history[-1] == f"turn-{MAX_HISTORY_TURNS}"


def test_a_turn_is_never_split_mid_tool_call():
    """A fat turn (several tool-call/tool-result pairs) is dropped whole,
    never partially — never leaving a tool call with no matching result."""
    client = _client()
    fat_turn = ["call-1", "result-1", "call-2", "result-2"]
    client._extend_message_history(fat_turn)
    for i in range(MAX_HISTORY_TURNS):
        client._extend_message_history([f"turn-{i}"])

    assert "call-1" not in client._message_history
    assert "result-1" not in client._message_history
    # The whole fat turn went, or none of it did — no split pair.
    assert ("call-2" in client._message_history) == (
        "result-2" in client._message_history
    )


def test_message_history_sent_to_the_model_is_the_same_object_being_trimmed():
    """``message_history=self._message_history`` in `_run_turn_steps` has to
    see the trimmed list, not a stale copy taken before a trim."""
    client = _client()
    for i in range(MAX_HISTORY_TURNS + 5):
        client._extend_message_history([f"turn-{i}"])

    assert len(client._message_history) == MAX_HISTORY_TURNS
    assert "turn-0" not in client._message_history
