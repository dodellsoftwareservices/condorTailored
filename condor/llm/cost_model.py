"""Price each provider response before the next model/tool step is admitted."""

import math
from contextlib import asynccontextmanager

from pydantic_ai.models.wrapper import WrapperModel

from condor.llm.budget import BudgetExhausted, is_free_model


class CostTrackingModel(WrapperModel):
    def __init__(self, model, governor, caller: str, model_key: str):
        super().__init__(model)
        self.governor = governor
        self.caller = caller
        self.model_key = model_key

    def _bounded_settings(self, settings):
        if (
            self.governor.config.enabled
            and self.governor.config.daily_cost_cap_usd is not None
        ):
            settings = dict(settings or {})
            settings["max_tokens"] = min(settings.get("max_tokens") or 4096, 4096)
        return settings

    def _record(self, response):
        if not self.governor.config.enabled:
            return
        try:
            if self.model_key.startswith("openrouter:") and is_free_model(
                self.model_key
            ):
                cost = 0.0
            else:
                cost = float(response.cost().total_price)
            if not math.isfinite(cost) or cost < 0:
                raise ValueError("Invalid provider pricing")
        except Exception:
            self.governor.ledger.record_unpriced(self.caller)
        else:
            self.governor.ledger.record_cost(self.caller, cost)

    async def request(self, messages, model_settings, model_request_parameters):
        async with self.governor.cost_slot():
            try:
                response = await self.wrapped.request(
                    messages,
                    self._bounded_settings(model_settings),
                    model_request_parameters,
                )
            except BudgetExhausted:
                raise
            except BaseException:
                # A failed/cancelled transport may have spent tokens upstream.
                if self.governor.config.enabled:
                    self.governor.ledger.record_unpriced(self.caller)
                raise
            self._record(response)
            return response

    @asynccontextmanager
    async def request_stream(
        self, messages, model_settings, model_request_parameters, run_context=None
    ):
        async with self.governor.cost_slot():
            response = None
            budget_blocked = False
            completed = False
            try:
                async with self.wrapped.request_stream(
                    messages,
                    self._bounded_settings(model_settings),
                    model_request_parameters,
                    run_context,
                ) as stream:
                    response = stream
                    yield stream
                    completed = True
            except BudgetExhausted:
                budget_blocked = True
                raise
            finally:
                if response is not None:
                    try:
                        final = response.get()
                    except Exception:
                        if self.governor.config.enabled:
                            self.governor.ledger.record_unpriced(self.caller)
                    else:
                        self._record(final)
                        if (
                            not completed
                            and self.governor.config.enabled
                            and not (
                                self.model_key.startswith("openrouter:")
                                and is_free_model(self.model_key)
                            )
                        ):
                            # Interrupted streams may omit final billed usage.
                            self.governor.ledger.record_unpriced(self.caller)
                elif not budget_blocked and self.governor.config.enabled:
                    self.governor.ledger.record_unpriced(self.caller)
