"""Per-response spend admission, including multi-step and concurrent runs."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel

from condor.agent_config import Budget
from condor.llm.budget import (
    PAID_MAX_OUTPUT_TOKENS,
    BudgetExhausted,
    RequestGovernor,
    RequestLedger,
)
from condor.llm.cost_model import CostTrackingModel


def governor(tmp_path, **overrides):
    cfg = Budget(enabled=True, daily_cost_cap_usd=0.50, **overrides)
    return RequestGovernor(lambda: cfg, RequestLedger(tmp_path))


def response(cost, parts=None):
    result = ModelResponse(parts=parts or [TextPart("done")])
    result.cost = lambda: SimpleNamespace(total_price=cost)
    return result


def test_one_tool_run_stops_before_its_next_request_after_crossing_cap(tmp_path):
    gov = governor(tmp_path)
    calls = []

    def provider(messages, info):
        calls.append(info.model_settings)
        return response(
            0.30, [ToolCallPart("lookup", {}, tool_call_id=str(len(calls)))]
        )

    model = CostTrackingModel(FunctionModel(provider), gov, "loop", "openai:test")
    agent = Agent(model)

    @agent.tool_plain
    def lookup() -> str:
        return "data"

    with pytest.raises(BudgetExhausted):
        agent.run_sync("work")
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == PAID_MAX_OUTPUT_TOKENS
    assert gov.ledger.cost_today() == 0.60


def test_two_governors_sharing_a_ledger_allow_only_one_paid_request_in_flight(tmp_path):
    first, second = governor(tmp_path), governor(tmp_path)
    calls = []

    async def provider(messages, info):
        calls.append(1)
        await asyncio.sleep(0.02)
        return response(0.60)

    models = [
        CostTrackingModel(FunctionModel(provider), gov, "chat", "custom:test")
        for gov in (first, second)
    ]

    async def scenario():
        return await asyncio.gather(
            *(m.request([], None, ModelRequestParameters()) for m in models),
            return_exceptions=True,
        )

    results = asyncio.run(scenario())
    assert len(calls) == 1
    assert sum(isinstance(value, BudgetExhausted) for value in results) == 1
    assert first.ledger.cost_today() == 0.60


def test_unknown_pricing_pauses_later_requests_and_keeps_known_spend(tmp_path):
    gov = governor(tmp_path)
    calls = []

    def provider(messages, info):
        calls.append(1)
        if len(calls) == 1:
            return response(0.10)
        unknown = response(0)
        unknown.cost = lambda: (_ for _ in ()).throw(LookupError("new model"))
        return unknown

    model = CostTrackingModel(FunctionModel(provider), gov, "chat", "anthropic:new")

    async def scenario():
        await model.request([], None, ModelRequestParameters())
        await model.request([], None, ModelRequestParameters())
        with pytest.raises(BudgetExhausted, match="could not be priced"):
            await model.request([], None, ModelRequestParameters())

    asyncio.run(scenario())
    assert len(calls) == 2
    assert gov.ledger.cost_today() == 0.10
    assert gov.ledger.unpriced_today() == 1
    assert gov.hold_seconds() > 0


def test_stream_cancellation_keeps_known_cost_and_blocks_uncertain_spend(
    tmp_path, monkeypatch
):
    gov = governor(tmp_path)

    @asynccontextmanager
    async def stream(*args, **kwargs):
        yield SimpleNamespace(get=lambda: response(0.20))

    raw = FunctionModel(lambda messages, info: response(0.10))
    monkeypatch.setattr(raw, "request_stream", stream)
    model = CostTrackingModel(raw, gov, "chat", "openai:test")

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            async with model.request_stream([], None, ModelRequestParameters()):
                raise asyncio.CancelledError()
        with pytest.raises(BudgetExhausted, match="could not be priced"):
            await asyncio.wait_for(
                model.request([], None, ModelRequestParameters()), timeout=1
            )

    asyncio.run(scenario())
    assert gov.ledger.cost_today() == 0.20


def test_dollar_cap_cannot_be_bypassed_by_selecting_an_acp_client(
    tmp_path, monkeypatch
):
    from condor.agent_config import AgentRuntimeConfig
    from condor.runtime import llm_client

    cfg = AgentRuntimeConfig(budget=governor(tmp_path).config)
    monkeypatch.setattr("condor.llm.budget.get_agent_config", lambda: cfg)
    with pytest.raises(RuntimeError, match="cannot meter ACP"):
        llm_client.build_llm_client("claude-code")
    assert "cannot meter ACP" in asyncio.run(llm_client.agent_key_error("claude-code"))


def test_wrapper_is_applied_to_direct_providers_too(tmp_path, monkeypatch):
    from condor.acp.pydantic_ai_client import PydanticAIClient
    from condor.llm import budget

    gov = governor(tmp_path)
    monkeypatch.setattr(budget, "_governor", gov)
    client = PydanticAIClient("openai:test")

    async def build():
        return FunctionModel(lambda messages, info: response(0.1))

    monkeypatch.setattr(client, "_build_provider_model", build)
    assert isinstance(asyncio.run(client._build_model()), CostTrackingModel)


def test_enabling_a_dollar_cap_also_covers_an_already_running_chat(
    tmp_path, monkeypatch
):
    from condor.acp.pydantic_ai_client import PydanticAIClient
    from condor.llm import budget

    cfg = [Budget(enabled=False)]
    gov = RequestGovernor(lambda: cfg[0], RequestLedger(tmp_path))
    monkeypatch.setattr(budget, "_governor", gov)
    calls = []

    def provider(messages, info):
        calls.append(1)
        return response(0.60)

    client = PydanticAIClient("openai:test")

    async def build():
        return FunctionModel(provider)

    monkeypatch.setattr(client, "_build_model", build)

    async def scenario():
        await client.start()
        try:
            cfg[0] = Budget(enabled=True, daily_cost_cap_usd=0.50)
            await client._agent.run("one")
            with pytest.raises(BudgetExhausted):
                await client._agent.run("two")
        finally:
            await client.stop()

    asyncio.run(scenario())
    assert len(calls) == 1


def test_existing_acp_clients_recheck_the_cost_policy_on_the_next_turn(
    tmp_path, monkeypatch
):
    from condor.acp.client import ACPClient
    from condor.agent_config import AgentRuntimeConfig

    cfg = AgentRuntimeConfig(budget=governor(tmp_path).config)
    monkeypatch.setattr("condor.llm.budget.get_agent_config", lambda: cfg)
    client = ACPClient(command=["unused"])

    async def scenario():
        with pytest.raises(RuntimeError, match="cannot meter ACP"):
            await anext(client.prompt_stream("do not send"))

    asyncio.run(scenario())


def test_sub_microdollar_prices_are_not_rounded_away(tmp_path):
    ledger = RequestLedger(tmp_path)
    for _ in range(10):
        ledger.record_cost("chat", 0.0000004)
    assert ledger.cost_today() == pytest.approx(0.000004)


@pytest.mark.parametrize("cost", [-1, float("nan"), float("inf")])
def test_invalid_prices_cannot_reduce_or_poison_spend(tmp_path, cost):
    ledger = RequestLedger(tmp_path)
    ledger.record_cost("chat", 0.10)
    with pytest.raises(ValueError):
        ledger.record_cost("chat", cost)
    assert ledger.cost_today() == 0.10


# ── a provider rejection is not spend ──


def _http_error(status):
    from pydantic_ai.exceptions import ModelHTTPError

    return ModelHTTPError(status, "openrouter:deepseek/deepseek-v4.1-flash-1017", {})


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422, 429])
def test_a_client_error_reply_books_no_spend(tmp_path, status):
    """Regression: a mis-set price ceiling made every paid request 404, and each one was
    booked at the worst-case reserve, so a broken config could burn the month's cap."""
    gov = governor(tmp_path)

    def provider(messages, info):
        raise _http_error(status)

    model = CostTrackingModel(FunctionModel(provider), gov, "loop", "openai:test")

    async def scenario():
        with pytest.raises(Exception, match=str(status)):
            await model.request([], None, ModelRequestParameters())

    asyncio.run(scenario())
    assert gov.ledger.unpriced_today() == 0


@pytest.mark.parametrize("status", [408, 500, 502, 503])
def test_a_timeout_or_server_error_stays_uncertain_spend(tmp_path, status):
    gov = governor(tmp_path)

    def provider(messages, info):
        raise _http_error(status)

    model = CostTrackingModel(FunctionModel(provider), gov, "loop", "openai:test")

    async def scenario():
        with pytest.raises(Exception, match=str(status)):
            await model.request([], None, ModelRequestParameters())

    asyncio.run(scenario())
    assert gov.ledger.unpriced_today() == 1


def test_an_error_with_no_status_stays_uncertain_spend(tmp_path):
    gov = governor(tmp_path)

    def provider(messages, info):
        raise ConnectionError("reset by peer")

    model = CostTrackingModel(FunctionModel(provider), gov, "loop", "openai:test")

    async def scenario():
        with pytest.raises(ConnectionError):
            await model.request([], None, ModelRequestParameters())

    asyncio.run(scenario())
    assert gov.ledger.unpriced_today() == 1


# ── a free model never books spend ──


def _free_governor(tmp_path):
    return governor(tmp_path)


@pytest.mark.parametrize(
    "failure", [asyncio.TimeoutError(), ConnectionError("reset"), _http_error(503)]
)
def test_a_failed_free_request_books_no_spend(tmp_path, failure):
    """Regression: slow free analyst calls were timing out and each was booked at the
    worst-case reserve, quietly eating the paid month's cap for requests that cost $0.
    """
    gov = _free_governor(tmp_path)

    def provider(messages, info):
        raise failure

    model = CostTrackingModel(
        FunctionModel(provider),
        gov,
        "sim_analyst.spot.trend",
        "openrouter:nvidia/nemotron-3.5-super-vl:free",
    )

    async def scenario():
        with pytest.raises(BaseException):
            await model.request([], None, ModelRequestParameters())

    asyncio.run(scenario())
    assert gov.ledger.unpriced_today() == 0


def test_the_same_failure_on_a_paid_model_is_still_booked(tmp_path):
    gov = _free_governor(tmp_path)

    def provider(messages, info):
        raise ConnectionError("reset")

    model = CostTrackingModel(
        FunctionModel(provider),
        gov,
        "loop",
        "openrouter:deepseek/deepseek-v4.1-flash-1017",
    )

    async def scenario():
        with pytest.raises(ConnectionError):
            await model.request([], None, ModelRequestParameters())

    asyncio.run(scenario())
    assert gov.ledger.unpriced_today() == 1
