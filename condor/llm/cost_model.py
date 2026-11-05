"""Price each provider response before the next model/tool step is admitted."""

import math
from contextlib import asynccontextmanager

from pydantic_ai.models.wrapper import WrapperModel

from condor.llm.budget import PAID_MAX_OUTPUT_TOKENS, BudgetExhausted, is_free_model


def response_cost_usd(response, model_key: str = "") -> float:
    """Prefer the gateway's billed cost over a bundled model-price catalog."""
    if model_key.startswith("openrouter:") and is_free_model(model_key):
        return 0.0
    details = getattr(response, "provider_details", None) or {}
    if model_key.startswith("openrouter:") and "cost" in details:
        cost = float(details["cost"])
    else:
        cost = float(response.cost().total_price)
    if not math.isfinite(cost) or cost < 0:
        raise ValueError("Invalid provider pricing")
    return cost


def rejected_before_generation(exc: BaseException) -> bool:
    """A client-error reply (400-499, bar 408) means the provider refused the request
    before running the model, so nothing was billed. Booking such a failure at the
    worst-case reserve would let a mis-set routing option (a 404) burn the whole month's
    cap in a few hundred retries. Timeouts, 5xx, dropped streams and cancellations stay
    uncertain: tokens may have been spent upstream.
    """
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and status != 408


class CostTrackingModel(WrapperModel):
    def __init__(self, model, governor, caller: str, model_key: str):
        super().__init__(model)
        self.governor = governor
        self.caller = caller
        self.model_key = model_key

    def _bounded_settings(self, settings):
        if self.governor.config.enabled and (
            self.governor.config.daily_cost_cap_usd is not None
            or self.governor.config.monthly_cost_cap_usd is not None
        ):
            settings = dict(settings or {})
            settings["max_tokens"] = min(
                settings.get("max_tokens") or PAID_MAX_OUTPUT_TOKENS,
                PAID_MAX_OUTPUT_TOKENS,
            )
        return settings

    def _record(self, response):
        if not self.governor.config.enabled:
            return
        try:
            cost = response_cost_usd(response, self.model_key)
        except Exception:
            self.governor.ledger.record_unpriced(self.caller)
        else:
            self.governor.ledger.record_cost(self.caller, cost)

    async def request(self, messages, model_settings, model_request_parameters):
        async with self.governor.cost_slot(model_key=self.model_key):
            try:
                response = await self.wrapped.request(
                    messages,
                    self._bounded_settings(model_settings),
                    model_request_parameters,
                )
            except BudgetExhausted:
                raise
            except BaseException as exc:
                # A failed/cancelled transport may have spent tokens upstream, unless the
                # provider rejected the request outright.
                if self.governor.config.enabled and not rejected_before_generation(exc):
                    self.governor.ledger.record_unpriced(self.caller)
                raise
            self._record(response)
            return response

    @asynccontextmanager
    async def request_stream(
        self, messages, model_settings, model_request_parameters, run_context=None
    ):
        async with self.governor.cost_slot(model_key=self.model_key):
            response = None
            budget_blocked = False
            rejected = False
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
            except BaseException as exc:
                # Refused before the stream opened (a 4xx): nothing was generated.
                rejected = response is None and rejected_before_generation(exc)
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
                elif (
                    not budget_blocked and not rejected and self.governor.config.enabled
                ):
                    self.governor.ledger.record_unpriced(self.caller)
