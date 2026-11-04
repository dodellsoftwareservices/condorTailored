"""`PydanticAIClient._fold_usage` feeds the LLM budget's cost ledger.

A run's dollar cost is only known once pydantic-ai's bundled pricing table
has priced its responses — after the fact, unlike the request-count gate
which checks *before* sending. This pins that the cost gets folded into the
same per-day ledger `condor.llm.budget`'s daily cost cap reads, gated on
`budget.enabled` the same way the request ledger already is (system review
2026-10-31, finding #21).
"""

from __future__ import annotations

from types import SimpleNamespace

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.usage import RequestUsage

from condor.acp.pydantic_ai_client import PydanticAIClient
from condor.agent_config import Budget
from condor.llm import budget as b


def _priced_response(cost_usd: float) -> ModelResponse:
    response = ModelResponse(
        parts=[TextPart("hi")],
        usage=RequestUsage(input_tokens=10, output_tokens=5),
    )
    response.cost = lambda: SimpleNamespace(total_price=cost_usd)
    return response


def _fake_run(response: ModelResponse) -> SimpleNamespace:
    return SimpleNamespace(
        usage=RequestUsage(input_tokens=10, output_tokens=5),
        result=None,
        new_messages=lambda: [response],
    )


def _governor(tmp_path, monkeypatch, **overrides):
    cfg = Budget(enabled=True, **overrides)
    ledger = b.RequestLedger(tmp_path)
    gov = b.RequestGovernor(lambda: cfg, ledger)
    monkeypatch.setattr(b, "_governor", gov)
    return gov, ledger


def test_final_usage_does_not_duplicate_the_response_cost_ledger(tmp_path, monkeypatch):
    gov, ledger = _governor(tmp_path, monkeypatch, applies_to="all")
    client = PydanticAIClient("openai:gpt-4o", budget_caller="interactive")
    ledger.record_cost("interactive", 0.42)

    client._fold_usage(_fake_run(_priced_response(0.42)))

    assert ledger.cost_today() == 0.42
    assert client.usage.cost_usd == 0.42


def test_an_unpriced_run_records_nothing(tmp_path, monkeypatch):
    gov, ledger = _governor(tmp_path, monkeypatch, applies_to="all")
    client = PydanticAIClient("openai:gpt-4o", budget_caller="interactive")
    response = _priced_response(0.0)
    response.cost = lambda: (_ for _ in ()).throw(LookupError("no pricing"))

    client._fold_usage(_fake_run(response))

    assert ledger.cost_today() == 0.0
    assert client.usage.unpriced_turns == 1


def test_nothing_is_recorded_while_the_budget_is_disabled(tmp_path, monkeypatch):
    cfg = Budget(enabled=False)
    ledger = b.RequestLedger(tmp_path)
    gov = b.RequestGovernor(lambda: cfg, ledger)
    monkeypatch.setattr(b, "_governor", gov)
    client = PydanticAIClient("openai:gpt-4o", budget_caller="interactive")

    client._fold_usage(_fake_run(_priced_response(1.23)))

    assert ledger.cost_today() == 0.0
    assert client.usage.cost_usd == 1.23  # the client's own total still counts it
