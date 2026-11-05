"""Project-level agent runtime config: ``agent-config.json``.

One file for the knobs that decide how much an unattended agent may do and how
hard it may think, modelled on the ``agent.config.json`` of the Albatross CLI
harness so the two read alike::

    {
      "maxSteps": 1000,
      "approvalPolicy": "always",
      "alwaysAllow": ["manage_bots:deploy", "stop_executor"],
      "thinking": {"effort": "high"},
      "budget": {"dailyRequestCap": 1000, "requestsPerMinute": 18}
    }

Resolution order, later wins: built-in defaults, ``agent-config.json`` at the
project root (or the path in ``$CONDOR_AGENT_CONFIG``), then the few
``CONDOR_*`` environment variables below. With no file and no variables every
value is the one Condor has always run with, so installing this module changes
nothing until a file is dropped in.

========================  ====================================================
``maxSteps``              The most model requests one agent *run* may make: a
                          loop tick, a chat turn or a delegation. pydantic-ai
                          stops a run at 50 requests unless told otherwise.
                          ``$CONDOR_MAX_STEPS``.
``approvalPolicy``        ``always`` asks a human before a dangerous tool call
                          unless it is on ``alwaysAllow``; ``never`` asks no
                          one. ``dangerous-only`` is accepted for Albatross
                          parity and means ``always`` here: Condor only ever
                          asks about dangerous calls. ``$CONDOR_APPROVAL_POLICY``.
``alwaysAllow``           Tool names, or ``tool:action`` pairs, a human no
                          longer has to approve. Chat and delegation seats
                          only: a loop tick is already auto-approved, and its
                          risk gate is never configurable from here.
``thinking``              ``effort`` (``none``..``xhigh``) or ``maxTokens`` (a
                          reasoning-token budget), never both; ``exclude`` hides
                          the reasoning from the response. OpenRouter models
                          only. ``$CONDOR_THINKING_EFFORT``.
``openrouterProviderOnly`` Model ID to allowed OpenRouter endpoint slugs. Applied
                          only when that exact model is used.
``maxOutputTokens``       Cap on one response, reasoning included.
``tickTimeoutSec``        Wall-clock budget of one loop tick, unless the loop's
                          own config sets ``tick_timeout_sec``.
``budget``                The request ledger and pacer: see
                          :mod:`condor.llm.budget`.
========================  ====================================================

A key this build does not know is reported (``unrecognized``) and logged, never
silently dropped: a setting that does nothing reads, to whoever wrote it, like
one that works. Keys starting with ``_`` or ``$`` are comments and exempt.

``python -m condor.agent_config`` validates the file and prints what resolved.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.alias_generators import to_camel

log = logging.getLogger(__name__)

CONFIG_ENV = "CONDOR_AGENT_CONFIG"
FILENAME = "agent-config.json"

# Overrides applied on top of the file, name -> top-level JSON key.
ENV_OVERRIDES: dict[str, str] = {
    "CONDOR_MAX_STEPS": "maxSteps",
    "CONDOR_APPROVAL_POLICY": "approvalPolicy",
    "CONDOR_THINKING_EFFORT": "thinking.effort",
}

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


class AgentConfigError(ValueError):
    """The config file exists but cannot be used. Carries a message fit to show."""


class _Model(BaseModel):
    # camelCase on the wire, snake_case in Python; unknown keys are kept (not
    # raised on) so they can be *reported*, which is the whole point of
    # ``unrecognized``.
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="allow",
        frozen=True,
    )


class PriceCeiling(_Model):
    """OpenRouter ``max_price``: dollars per million tokens, per direction."""

    prompt: float = Field(gt=0)
    completion: float = Field(gt=0)


class Thinking(_Model):
    """How hard the model is asked to think (OpenRouter ``reasoning``)."""

    effort: Effort | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    exclude: bool = False

    @model_validator(mode="after")
    def _one_of(self) -> "Thinking":
        if self.effort not in (None, "none") and self.max_tokens is not None:
            raise ValueError(
                "thinking.effort and thinking.maxTokens are alternatives — "
                "OpenRouter takes one of them, so set one"
            )
        return self

    @property
    def active(self) -> bool:
        return self.effort is not None or self.max_tokens is not None

    def openrouter_reasoning(self) -> dict[str, Any] | None:
        """The ``reasoning`` object of an OpenRouter chat request, or ``None``."""
        if not self.active:
            return None
        body: dict[str, Any] = {}
        if self.effort is not None:
            body["effort"] = self.effort
        else:
            body["max_tokens"] = self.max_tokens
        if self.exclude:
            body["exclude"] = True
        return body


class Budget(_Model):
    """The request ledger and pacer (:mod:`condor.llm.budget`).

    Off unless a file turns it on: with no file there is no ledger, no pacing
    and no rate limit, exactly as before.
    """

    enabled: bool = False
    # ``free-models`` counts only ``:free`` / ``openrouter/free`` requests, the
    # ones OpenRouter caps per day; ``all`` counts every OpenRouter request.
    applies_to: Literal["free-models", "all"] = "free-models"
    # OpenRouter: 1000 free-model requests a day once $10 of credit has been
    # bought, 50 before, reset at 00:00 UTC. Failed requests count too.
    daily_request_cap: int = Field(default=1000, ge=1)
    # Left unspent so a manual chat, or another tool on the same key, still has
    # room. The ledger refuses a request that would eat into it.
    reserve_requests: int = Field(default=50, ge=0)
    # OpenRouter's free models allow 20 a minute; stay under it.
    requests_per_minute: int = Field(default=18, ge=1)
    # Loop ticks only. A tick that needs more than this is a model stuck in a
    # tool loop, and with no cap one such tick spends the day. ``None`` lets
    # ``maxSteps`` alone decide.
    per_run_request_cap: int | None = Field(default=100, ge=1)
    # Stretch the gap between ticks so the budget lasts until the shift ends or
    # the daily reset, whichever is first.
    pacing: bool = True
    # What a tick is assumed to cost until the loop has measured its own.
    assumed_requests_per_tick: int = Field(default=8, ge=1)
    # ``None`` (the default) leaves spend uncapped — every limit above is a
    # *request count*, which is moot on a free model and does nothing once
    # this is pointed at a paid one (system review 2026-10-31, finding #21).
    # Checked before each provider request. With a dollar cap, requests are
    # serialized across processes and output is limited to 4096 tokens; actual
    # prices are recorded after each response. One response may overshoot the
    # threshold. Unknown pricing pauses admission until reset. ACP subscription
    # clients cannot be metered and are refused while a dollar cap is enabled.
    daily_cost_cap_usd: float | None = Field(
        default=None,
        gt=0,
        description="UTC daily spend threshold across direct model providers. With a cap, requests are serialized and output is capped at 4096 tokens. One response can overshoot; unknown pricing pauses requests. ACP subscription clients are refused. Null disables the dollar limit.",
    )
    monthly_cost_cap_usd: float | None = Field(
        default=None,
        gt=0,
        description="UTC calendar-month spend ceiling. Paid requests reserve their configured worst-case cost before admission; free models remain available.",
    )
    request_cost_reserve_usd: float = Field(
        default=0.15,
        gt=0,
        description="Worst-case cost of one paid request, held against the monthly cap while it is in flight. An unpriced response is booked at this amount. Must be at least the real worst case for the pinned model (its price ceilings times its context window).",
    )
    paid_price_ceiling_usd_per_million: PriceCeiling | None = Field(
        default=None,
        description="OpenRouter max_price sent with every paid request, in dollars per million tokens. Required with a monthly cap. Set it a little above the pinned endpoint's current price: if the endpoint becomes dearer than this, requests fail with 404 rather than overspend.",
    )
    paid_max_context_tokens: int = Field(
        default=1_048_576,
        ge=1,
        description="Largest prompt the pinned paid model accepts. Together with the price ceiling it bounds one request's cost, which requestCostReserveUsd must cover.",
    )
    allowed_paid_models: tuple[str, ...] = Field(
        default=(),
        description="Paid model keys (e.g. openrouter:vendor/model) permitted while a monthly cap is set. Anything else paid is refused, because only these have price ceilings pinned. Free models are always allowed.",
    )

    @model_validator(mode="after")
    def _reserve_fits(self) -> "Budget":
        if self.reserve_requests >= self.daily_request_cap:
            raise ValueError(
                f"budget.reserveRequests ({self.reserve_requests}) must be below "
                f"budget.dailyRequestCap ({self.daily_request_cap})"
            )
        cap = self.monthly_cost_cap_usd
        if cap is None:
            return self
        if self.request_cost_reserve_usd >= cap:
            raise ValueError(
                f"budget.requestCostReserveUsd ({self.request_cost_reserve_usd}) "
                f"must be below budget.monthlyCostCapUsd ({cap})"
            )
        ceiling = self.paid_price_ceiling_usd_per_million
        if ceiling is None:
            raise ValueError(
                "budget.monthlyCostCapUsd needs budget.paidPriceCeilingUsdPerMillion: "
                "without a price ceiling a paid request has no worst-case cost to reserve"
            )
        from condor.llm.budget import PAID_MAX_OUTPUT_TOKENS

        worst = (
            self.paid_max_context_tokens * ceiling.prompt
            + PAID_MAX_OUTPUT_TOKENS * ceiling.completion
        ) / 1_000_000
        if worst > self.request_cost_reserve_usd:
            raise ValueError(
                f"budget.requestCostReserveUsd ({self.request_cost_reserve_usd}) is below "
                f"the worst case of one paid request (${worst:.4f}: "
                f"{self.paid_max_context_tokens:,} prompt tokens at "
                f"${ceiling.prompt}/M plus {PAID_MAX_OUTPUT_TOKENS:,} output tokens at "
                f"${ceiling.completion}/M)"
            )
        return self

    @property
    def spendable(self) -> int:
        """Requests a day the agents may spend: the cap less the reserve."""
        return self.daily_request_cap - self.reserve_requests


class AgentRuntimeConfig(_Model):
    """The resolved ``agent-config.json``."""

    # The model the shift runner pins on the loops it starts. Chat and every
    # other surface keep choosing theirs the way they always did.
    model: str = ""
    # ``None`` leaves pydantic-ai's own limit (50) in force.
    max_steps: int | None = Field(default=None, ge=1, le=100_000)
    approval_policy: Literal["always", "dangerous-only", "never"] = "always"
    always_allow: tuple[str, ...] = ()
    thinking: Thinking = Field(default_factory=Thinking)
    openrouter_provider_only: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    max_output_tokens: int | None = Field(default=None, ge=1)
    tick_timeout_sec: int | None = Field(default=None, ge=30)
    budget: Budget = Field(default_factory=Budget)

    # ── derived views ──

    def run_limit(
        self, *, unattended: bool, budget_left: int | None = None
    ) -> int | None:
        """The request limit to hand pydantic-ai for one run, or ``None``.

        ``maxSteps`` bounds every run. An *unattended* one (a loop tick) is also
        held to ``budget.perRunRequestCap`` and to what is left of the day's
        budget, so one stuck tick cannot spend the day; a human at a chat is
        not second-guessed. ``None`` means "leave the library default".
        """
        limits = [self.max_steps] if self.max_steps is not None else []
        if self.budget.enabled and unattended:
            if self.budget.per_run_request_cap is not None:
                limits.append(self.budget.per_run_request_cap)
            if budget_left is not None:
                limits.append(max(budget_left, 1))
        return min(limits) if limits else None

    def tool_always_allowed(self, tool_name: str, action: str | None) -> bool:
        """Is this call on ``alwaysAllow`` — by tool name, or as ``tool:action``?"""
        allowed = set(self.always_allow)
        if tool_name in allowed:
            return True
        return bool(action) and f"{tool_name}:{action}" in allowed

    @property
    def never_ask(self) -> bool:
        return self.approval_policy == "never"

    def to_public(self) -> dict[str, Any]:
        """The resolved config as plain JSON, for status surfaces (comments left out)."""

        def clean(node: Any) -> Any:
            if isinstance(node, dict):
                return {
                    k: clean(v)
                    for k, v in node.items()
                    if not str(k).startswith(("_", "$"))
                }
            return node

        return clean(self.model_dump(by_alias=True, mode="json", exclude_none=False))


class LoadedConfig:
    """A resolved config plus where it came from and what it did not understand."""

    __slots__ = ("config", "path", "unrecognized", "error")

    def __init__(
        self,
        config: AgentRuntimeConfig,
        path: Path | None,
        unrecognized: tuple[str, ...] = (),
        error: str = "",
    ) -> None:
        self.config = config
        self.path = path
        self.unrecognized = unrecognized
        self.error = error


# ── loading ──


def config_path() -> Path:
    """Where the file is read from: ``$CONDOR_AGENT_CONFIG``, else the project root."""
    override = os.environ.get(CONFIG_ENV, "").strip()
    return Path(override).expanduser() if override else _PROJECT_ROOT / FILENAME


def _set_dotted(raw: dict[str, Any], dotted: str, value: Any) -> None:
    node = raw
    *parents, leaf = dotted.split(".")
    for key in parents:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[leaf] = value


def _apply_env(raw: dict[str, Any], environ: dict[str, str]) -> None:
    for name, dotted in ENV_OVERRIDES.items():
        value = environ.get(name, "").strip()
        if not value:
            continue
        if name == "CONDOR_MAX_STEPS":
            try:
                _set_dotted(raw, dotted, int(value))
            except ValueError:
                raise AgentConfigError(f"${name} must be an integer, got {value!r}")
        else:
            _set_dotted(raw, dotted, value)


def _unknown_keys(model: BaseModel, prefix: str = "") -> list[str]:
    found = [
        f"{prefix}{key}"
        for key in (model.model_extra or {})
        if not key.startswith(("_", "$"))
    ]
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            found.extend(_unknown_keys(value, f"{prefix}{field.alias or name}."))
    return found


def _format_validation(err: ValidationError) -> str:
    lines = []
    for item in err.errors():
        where = ".".join(str(p) for p in item["loc"]) or "(root)"
        lines.append(f"{where}: {item['msg']}")
    return "; ".join(lines)


def load(
    path: Path | None = None, environ: dict[str, str] | None = None
) -> LoadedConfig:
    """Read and validate the config. Raises :class:`AgentConfigError` on a bad file.

    A missing file is not an error: it resolves to the defaults, which are
    Condor's behaviour from before this file existed.
    """
    path = path if path is not None else config_path()
    environ = environ if environ is not None else dict(os.environ)

    raw: dict[str, Any] = {}
    source: Path | None = None
    if path.is_file():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentConfigError(f"{path}: cannot read — {exc}") from exc
        if not isinstance(parsed, dict):
            raise AgentConfigError(f"{path}: the top level must be a JSON object")
        raw, source = parsed, path

    _apply_env(raw, environ)
    try:
        config = AgentRuntimeConfig.model_validate(raw)
    except ValidationError as exc:
        where = str(source) if source else "environment overrides"
        raise AgentConfigError(f"{where}: {_format_validation(exc)}") from exc
    return LoadedConfig(config, source, tuple(_unknown_keys(config)))


# The cache is keyed on what could change the answer: the path, the file's
# mtime and the override variables. An edit takes effect on the next read, so a
# running loop picks up a new maxSteps on its next tick without a restart.
_lock = threading.Lock()
_cached: tuple[tuple, LoadedConfig] | None = None


def _fingerprint(path: Path) -> tuple:
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = -1
    env = tuple(os.environ.get(name, "") for name in sorted(ENV_OVERRIDES))
    return (str(path), stamp, env)


def get_loaded(*, strict: bool = False) -> LoadedConfig:
    """The current config with provenance. Never raises unless ``strict``.

    A file that cannot be used logs an error and resolves to the defaults, so a
    typo cannot take the bot down; ``error`` on the result says so, and the
    shift runner calls with ``strict=True`` so an unattended run never starts on
    settings it was not given.
    """
    global _cached
    path = config_path()
    key = _fingerprint(path)
    with _lock:
        if _cached is not None and _cached[0] == key:
            hit = _cached[1]
            if strict and hit.error:
                raise AgentConfigError(hit.error)
            return hit

        try:
            loaded = load(path)
        except AgentConfigError as exc:
            if strict:
                raise
            log.error("agent-config unusable, running on defaults: %s", exc)
            loaded = LoadedConfig(AgentRuntimeConfig(), None, (), str(exc))
        else:
            if loaded.unrecognized:
                log.warning(
                    "%s: %s are not settings in this build and do nothing",
                    loaded.path or FILENAME,
                    ", ".join(loaded.unrecognized),
                )
        _cached = (key, loaded)
        return loaded


def get_agent_config() -> AgentRuntimeConfig:
    """The current resolved config (defaults when there is no usable file)."""
    return get_loaded().config


def reset_cache() -> None:
    """Forget the cached config (tests, and a caller that just wrote the file)."""
    global _cached
    with _lock:
        _cached = None


def json_schema() -> dict[str, Any]:
    """The JSON Schema of the file, for editor completion (``--schema``)."""
    schema = AgentRuntimeConfig.model_json_schema(by_alias=True)
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"
    schema["title"] = "Condor agent-config.json"
    return schema


def _main(argv: list[str]) -> int:
    if "--schema" in argv:
        print(json.dumps(json_schema(), indent=2))
        return 0
    try:
        loaded = get_loaded(strict=True)
    except AgentConfigError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"source: {loaded.path or '(none — built-in defaults)'}")
    for key in loaded.unrecognized:
        print(f"warning: {key} is not a setting in this build", file=sys.stderr)
    print(json.dumps(loaded.config.to_public(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
