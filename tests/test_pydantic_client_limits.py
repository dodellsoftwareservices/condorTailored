"""``maxSteps``, thinking and the request budget, as the pydantic-ai client applies them.

pydantic-ai stops a run at 50 model requests unless told otherwise, and Condor
never told it: ``agent-config.json``'s ``maxSteps`` is what finally does. The
properties worth pinning:

* a run that hits its limit ends as a *turn* (ACP's ``max_turn_requests``), not
  as an error, and says so in the text a journal keeps;
* with no config the limit is still pydantic-ai's 50, so a stock install is
  unchanged;
* the budget gate sits in the model, so a spent budget reaches the run as
  ``BudgetExhausted`` — the OpenAI SDK would turn anything raised from an HTTP
  hook into "Connection error" after its own retries;
* thinking is only ever asked of OpenRouter, in OpenRouter's spelling.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.providers.openai import OpenAIProvider

from condor import agent_config
from condor.acp.client import PromptDone, TextChunk
from condor.acp.pydantic_ai_client import PydanticAIClient, _make_openai_compat_model
from condor.llm import budget

OR_FREE = "openrouter:nvidia/nemotron-3.5-super-vl:free"


def _config(tmp_path, monkeypatch, body: dict) -> None:
    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps(body))
    monkeypatch.setenv(agent_config.CONFIG_ENV, str(path))
    agent_config.reset_cache()
    budget.reset_governor()


def _looping_client(
    calls: list, *, model_name="openai:gpt-4o", **kwargs
) -> PydanticAIClient:
    """A client whose model calls a tool forever: only a request limit stops it."""

    def respond(messages: list, info: AgentInfo) -> ModelResponse:
        calls.append(1)
        return ModelResponse(parts=[ToolCallPart("ping", {})])

    def ping() -> str:
        return "pong"

    client = PydanticAIClient(model_name, **kwargs)
    client._agent = Agent(FunctionModel(respond), tools=[ping])
    return client


def _drive(client: PydanticAIClient) -> list:
    async def go() -> list:
        return [event async for event in client.prompt_stream("go")]

    return asyncio.run(go())


def _done(events: list) -> PromptDone:
    return next(e for e in events if isinstance(e, PromptDone))


# ── the request limit ──


def test_without_a_config_the_library_limit_of_fifty_still_applies():
    calls: list = []
    client = _looping_client(calls)

    events = _drive(client)

    assert len(calls) == 50
    assert _done(events).stop_reason == "max_turn_requests"


def test_max_steps_is_the_limit_and_the_run_ends_as_a_turn_not_an_error(
    tmp_path, monkeypatch
):
    _config(tmp_path, monkeypatch, {"maxSteps": 7})
    calls: list = []
    client = _looping_client(calls)

    events = _drive(client)

    assert len(calls) == 7
    assert _done(events).stop_reason == "max_turn_requests"
    text = "".join(e.text for e in events if isinstance(e, TextChunk))
    assert "stopped early" in text and "maxSteps" in text
    # the partial turn is kept, so the next message sees what was done
    assert client._message_history, "a stopped run must still land in the history"


def test_a_thousand_steps_is_allowed_when_asked_for(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {"maxSteps": 1000})
    client = PydanticAIClient("openai:gpt-4o")

    limits = client._usage_limits()

    assert limits is not None and limits.request_limit == 1000


def test_a_loop_tick_is_also_held_to_the_per_run_cap_and_the_days_budget(
    tmp_path, monkeypatch
):
    _config(
        tmp_path,
        monkeypatch,
        {"maxSteps": 1000, "budget": {"enabled": True, "perRunRequestCap": 100}},
    )

    tick = PydanticAIClient(OR_FREE, unattended=True)
    chat = PydanticAIClient(OR_FREE)
    other = PydanticAIClient("ollama:llama3.1", unattended=True)  # not metered

    assert tick._usage_limits().request_limit == 100
    assert chat._usage_limits().request_limit == 1000
    assert other._usage_limits().request_limit == 100  # cap applies, budget does not

    # with only 30 requests left in the day the tick may not make 100
    gov = budget.get_governor()
    for _ in range(gov.config.spendable - 30):
        gov.ledger.record_request("x", "m:free", free=True)
    assert tick._usage_limits().request_limit == 30


def test_a_run_limit_is_never_below_one_request(tmp_path, monkeypatch):
    _config(
        tmp_path,
        monkeypatch,
        {"budget": {"enabled": True, "dailyRequestCap": 10, "reserveRequests": 0}},
    )
    gov = budget.get_governor()
    for _ in range(10):
        gov.ledger.record_request("x", "m:free", free=True)

    assert PydanticAIClient(OR_FREE, unattended=True)._usage_limits().request_limit == 1


# ── thinking and model settings ──

FREE_BILLING_BODY = {
    "usage": {"include": True},
    "provider": {"max_price": {"prompt": 0, "completion": 0}},
}


def test_thinking_is_openrouters_reasoning_request(tmp_path, monkeypatch):
    _config(
        tmp_path,
        monkeypatch,
        {"thinking": {"effort": "high"}, "maxOutputTokens": 8192},
    )

    assert PydanticAIClient(OR_FREE)._model_settings() == {
        "max_tokens": 8192,
        "extra_body": {
            "reasoning": {"effort": "high"},
            **FREE_BILLING_BODY,
        },
    }


def test_thinking_is_not_sent_to_providers_that_spell_it_differently(
    tmp_path, monkeypatch
):
    _config(tmp_path, monkeypatch, {"thinking": {"effort": "high"}})

    assert PydanticAIClient("ollama:llama3.1")._model_settings() is None
    assert PydanticAIClient("openai:gpt-4o")._model_settings() is None


def test_openrouter_always_reports_billed_usage_and_free_models_are_priced_at_zero():
    """Even with no config: the billed cost is what prices a model the bundled
    catalog does not know, and a ``:free`` model must never route to a paid endpoint."""
    assert PydanticAIClient(OR_FREE)._model_settings() == {
        "extra_body": FREE_BILLING_BODY
    }
    assert PydanticAIClient("ollama:llama3.1")._model_settings() is None


def test_a_reasoning_budget_in_tokens_is_the_alternative_to_an_effort(
    tmp_path, monkeypatch
):
    _config(tmp_path, monkeypatch, {"thinking": {"maxTokens": 4000, "exclude": True}})

    assert PydanticAIClient(OR_FREE)._model_settings() == {
        "extra_body": {
            "reasoning": {"max_tokens": 4000, "exclude": True},
            **FREE_BILLING_BODY,
        }
    }


def test_the_model_settings_reach_the_request(tmp_path, monkeypatch):
    """Not just built: the body OpenRouter receives carries the reasoning request."""
    _config(tmp_path, monkeypatch, {"thinking": {"effort": "high"}})
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_completion("ok"))

    model = _openrouter_model(handler)
    agent = Agent(model, model_settings=PydanticAIClient(OR_FREE)._model_settings())

    asyncio.run(agent.run("hi"))

    assert seen[0]["reasoning"] == {"effort": "high"}


def test_openrouter_provider_pin_is_scoped_to_exact_model(tmp_path, monkeypatch):
    target = "openrouter:deepseek/deepseek-v4.1-flash-1017"
    _config(
        tmp_path,
        monkeypatch,
        {
            "openrouterProviderOnly": {
                "deepseek/deepseek-v4.1-flash-1017": ["wafer/mxfp4"]
            }
        },
    )
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_completion("ok"))

    agent = Agent(
        _openrouter_model(handler),
        model_settings=PydanticAIClient(target)._model_settings(),
    )
    asyncio.run(agent.run("hi"))

    assert seen[0]["provider"] == {"only": ["wafer/mxfp4"]}
    assert seen[0]["usage"] == {"include": True}
    # The pin never leaks onto another model.
    assert (
        "only"
        not in PydanticAIClient(OR_FREE)._model_settings()["extra_body"]["provider"]
    )


def test_a_monthly_cap_pins_the_paid_model_to_price_ceilings_without_fallbacks(
    tmp_path, monkeypatch
):
    """The $0.15 reserve is only a true worst case because the request cannot be
    routed to a dearer endpoint."""
    target = "openrouter:deepseek/deepseek-v4.1-flash-1017"
    _config(
        tmp_path,
        monkeypatch,
        {
            "openrouterProviderOnly": {
                "deepseek/deepseek-v4.1-flash-1017": ["wafer/mxfp4"]
            },
            "budget": {
                "enabled": True,
                "monthlyCostCapUsd": 30,
                "requestCostReserveUsd": 0.2,
                "paidPriceCeilingUsdPerMillion": {"prompt": 0.17, "completion": 0.55},
            },
        },
    )
    body = PydanticAIClient(target)._model_settings()["extra_body"]
    assert body["usage"] == {"include": True}
    assert body["provider"] == {
        "only": ["wafer/mxfp4"],
        "max_price": {"prompt": 0.17, "completion": 0.55},
        "allow_fallbacks": False,
    }
    # A free model in the same config keeps its own zero ceiling.
    free = PydanticAIClient(OR_FREE)._model_settings()["extra_body"]
    assert free["provider"] == {"max_price": {"prompt": 0, "completion": 0}}


# ── the budget gate, through the real model class ──


def _completion(text: str) -> dict:
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 1,
        "model": "m:free",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _openrouter_model(handler, *, gate=None, hooks=None):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), event_hooks=hooks or {}
    )
    sdk = AsyncOpenAI(
        api_key="test", base_url="https://openrouter.ai/api/v1", http_client=http
    )
    return _make_openai_compat_model(
        "m:free", OpenAIProvider(openai_client=sdk), gate=gate
    )


def test_every_model_request_is_counted_and_a_spent_budget_arrives_as_itself(
    tmp_path, monkeypatch
):
    _config(
        tmp_path,
        monkeypatch,
        {
            "budget": {
                "enabled": True,
                "dailyRequestCap": 3,
                "reserveRequests": 1,
                "requestsPerMinute": 1000,
            }
        },
    )
    gov = budget.get_governor()
    model = _openrouter_model(
        lambda r: httpx.Response(200, json=_completion("ok")),
        gate=gov.gate_for("loop-1", "m:free"),
        hooks=gov.event_hooks("loop-1", "m:free"),
    )
    agent = Agent(model)

    asyncio.run(agent.run("one"))
    asyncio.run(agent.run("two"))
    assert gov.ledger.used_today() == 2 and gov.remaining() == 0

    with pytest.raises(budget.BudgetExhausted) as err:
        asyncio.run(agent.run("three"))

    # the real exception, not the SDK's "Connection error" wrapper
    assert "daily request budget spent" in str(err.value)
    assert gov.ledger.used_today() == 2  # nothing was sent for the third


def test_a_client_turns_a_spent_budget_into_a_clear_error_turn(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {})

    def respond(messages, info):
        raise budget.BudgetExhausted(
            "daily request budget spent (950 of 1000 used)", 0.0
        )

    client = PydanticAIClient("openai:gpt-4o")
    client._agent = Agent(FunctionModel(respond))

    events = _drive(client)

    assert "request budget exhausted" in "".join(
        e.text for e in events if isinstance(e, TextChunk)
    )
    assert _done(events).stop_reason == "error"


# ── building the OpenRouter model ──


def _build(monkeypatch, model_name: str, **kwargs):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client = PydanticAIClient(model_name, **kwargs)
    model = asyncio.run(client._build_model())
    return client, model


def test_the_gate_is_installed_only_when_the_budget_covers_the_model(
    tmp_path, monkeypatch
):
    _config(tmp_path, monkeypatch, {"budget": {"enabled": True}})

    client, model = _build(monkeypatch, OR_FREE)
    assert model._condor_gate is not None and client._http_client is not None

    _, paid = _build(monkeypatch, "openrouter:anthropic/claude-sonnet-4-5")
    assert paid._condor_gate is None  # applies_to defaults to free models


def test_with_no_budget_the_openrouter_client_is_built_as_it_always_was(monkeypatch):
    client, model = _build(monkeypatch, OR_FREE)

    assert model._condor_gate is None
    assert client._http_client is None


def test_stop_closes_the_http_client_it_opened():
    closed: list[bool] = []

    class Http:
        async def aclose(self):
            closed.append(True)

    client = PydanticAIClient(OR_FREE)
    client._http_client = Http()

    asyncio.run(client.stop())

    assert closed == [True] and client._http_client is None


def test_stop_survives_a_pool_that_will_not_close():
    class Http:
        async def aclose(self):
            raise RuntimeError("already closed")

    client = PydanticAIClient(OR_FREE)
    client._http_client = Http()

    asyncio.run(client.stop())  # no raise


# ── the factory passes the budget identity through ──


def test_build_llm_client_forwards_the_caller_and_the_unattended_flag(monkeypatch):
    from condor.runtime.llm_client import build_llm_client

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client = build_llm_client(OR_FREE, budget_caller="a.b_3", unattended=True)
    chat = build_llm_client(OR_FREE)

    assert (client.budget_caller, client.unattended) == ("a.b_3", True)
    assert (chat.budget_caller, chat.unattended) == ("interactive", False)


def test_a_per_client_reasoning_effort_overrides_the_file_for_that_client_only(
    tmp_path, monkeypatch
):
    _config(tmp_path, monkeypatch, {"thinking": {"effort": "high"}})
    analyst = PydanticAIClient(OR_FREE, reasoning_effort="low")
    desk = PydanticAIClient(OR_FREE)
    assert analyst._model_settings()["extra_body"]["reasoning"] == {"effort": "low"}
    assert desk._model_settings()["extra_body"]["reasoning"] == {"effort": "high"}


def test_a_reasoning_override_applies_even_with_no_thinking_in_the_file():
    body = PydanticAIClient(OR_FREE, reasoning_effort="low")._model_settings()
    assert body["extra_body"]["reasoning"] == {"effort": "low"}
