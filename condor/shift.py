"""The shift runner: start a profile's loops, keep an unattended run honest, report it.

A *shift* is a bounded stretch of trading by a fixed set of Condor loops on test
venues, described by a profile (``shifts/NAME.json``, schema in
``shifts/shift-profile.schema.json``). This module is the machinery around it::

    python -m condor.shift validate --profile NAME   # the profile, offline
    python -m condor.shift plan     --profile NAME   # what it would cost, touches nothing
    python -m condor.shift check    --profile NAME   # validate + every preflight, starts nothing
    python -m condor.shift start    --profile NAME   # preflight, start, supervise, report
    python -m condor.shift status   [--profile NAME] # one screen
    python -m condor.shift stop     [--profile NAME] # stop the loops, write the report
    python -m condor.shift report   [--profile NAME] # rebuild report.md / report.json

It talks to the *running* Condor server over HTTP, as a dashboard user would
(an admin JWT minted locally from the same secret the server verifies with), and
never imports the engine: the loops live in the server process, and a runner that
reached into them would only work until the day they were somewhere else.

Three properties the rest of the file exists to keep:

**It never starts on settings it was not given.** ``agent-config.json`` is read
strict, the model is resolved and checked against the server's own environment,
and a profile key it does not know is an error, not a shrug.

**An unattended run is supervised, and says what it did.** Every minute it polls
the loops, logs one line, and restarts one that died (bounded, with backoff).
What it will *not* restart is a loop that was stopped on purpose: the risk
engine's hard drawdown limit, an operator's emergency stop. Restarting those
would be the runner overruling the one component built to overrule it.

**Nothing is left running unnoticed.** The loops carry their own deadline
(``shift_ends_at``), so a dead runner cannot leave them trading past the shift;
Ctrl-C / SIGTERM asks them to stop; and either way the report is written.

Everything here is read-only against the trading side except the four calls a
runner exists to make: start, stop, shutdown, and the balance refresh.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import signal
import socket
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from condor import agent_config, shift_report
from condor.fsutil import atomic_write_json
from condor.llm.budget import is_free_model, next_reset, utc_day
from condor.paths import data_dir

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = PROJECT_ROOT / "shifts"
SHIFTS_DIRNAME = "shifts"

#: Every dashboard route lives under this prefix (``condor/web/app.py``).
API_PREFIX = "/api/v1"

#: The Hummingbot API account the loops trade on. Condor's tools default to it.
ACCOUNT = "master_account"

POLL_SEC = 60
#: Restart policy: how many times a loop may be brought back in one shift, and how
#: long to wait before each. A loop that dies twice in a minute is not a
#: transient fault; waiting longer before the later attempts keeps a systematic
#: failure from becoming a restart storm that spends the request budget.
MAX_RESTARTS = 3
RESTART_BACKOFF_SEC = (60, 300, 300)
#: No restart this close to the deadline: it could not tick before the engine's
#: own shift-end winddown, so it would open a session only to close it.
RESTART_CUTOFF_SEC = 120

MIN_FREQUENCY_SEC = 30
MAX_HOURS = 24

#: What ``recommended frequency`` aims at: a tenth of the window's budget left
#: unspent for ticks that cost more than their average.
HEADROOM = 0.9

#: Testnet/sandbox connectors and the local simulated Kraken account. A profile's
#: connector must be one of these: the point of a shift is paper money, and a
#: typo that lands on a mainnet connector must fail here, not at the exchange.
TEST_VENUES = frozenset(
    {
        "kraken_sim",
        "binance_perpetual_testnet",
        "bybit_perpetual_testnet",
        "bybit_testnet",
        "hyperliquid_perpetual_testnet",
        "hyperliquid_testnet",
        "derive_perpetual_testnet",
        "derive_testnet",
        "aevo_perpetual_testnet",
        "grvt_perpetual_testnet",
        "lighter_perpetual_testnet",
        "lighter_testnet",
        "pacifica_perpetual_testnet",
        "decibel_perpetual_testnet",
        "dexalot_testnet",
        "ndax_testnet",
        "architect_perpetual_sandbox",
    }
)

SIM_PORTFOLIOS = {"kraken_sim": "/sim/kraken/portfolio"}

#: The mainnet name behind each test venue. A trading context that names one of
#: these is worth a second look: it may be a candle source (legitimate: candles
#: are only served for mainnet names) or the loop about to trade the wrong venue.
MAINNET_TWINS = frozenset(
    re.sub(r"_(testnet|sandbox)$", "", venue) for venue in TEST_VENUES
)

#: Run-config keys the runner owns. A profile ``config`` that sets one would
#: change what the shift accounts for (capital, cadence, share) without the
#: profile saying so anywhere a reader of the plan would look.
RESERVED_CONFIG_KEYS = frozenset(
    {
        "agent_key",
        "frequency_sec",
        "total_amount_quote",
        "shift_id",
        "shift_ends_at",
        "request_share",
    }
)

SHIFT_END_ACTIONS = ("winddown", "stop")

_SECRET_KEY = re.compile(
    r"(secret|passw|token|api[_-]?key|private[_-]?key|credential|authorization)",
    re.IGNORECASE,
)

LOOP_ID = r"^[a-z0-9][a-z0-9-]{0,39}$"
PROFILE_NAME = r"^[a-z0-9][a-z0-9-]{0,63}$"
SLUG = r"^[a-z0-9_]+$"
SHIFT_ID = re.compile(r"^\d{8}T\d{4}Z-[a-z0-9][a-z0-9-]*$")


class ShiftError(RuntimeError):
    """The shift cannot go on; the message is fit to show an operator."""


# ── time ──


class Clock:
    """Wall time and sleeping, behind one seam so the supervisor can be tested.

    The supervisor's behaviour is a function of elapsed time (backoffs,
    deadlines, grace), and a test that waits real minutes is a test nobody runs.
    """

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def stamp_id(ts: float, profile_name: str) -> str:
    """``20261028T1830Z-testnet-4h``: sortable, and says which profile."""
    return (
        datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%dT%H%MZ")
        + f"-{profile_name}"
    )


def hhmm(ts: float | None) -> str:
    return (
        "?"
        if ts is None
        else datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")
    )


def iso(ts: float | None) -> str:
    return shift_report.utc_iso(ts) or "?"


def shifts_dir() -> Path:
    return data_dir() / SHIFTS_DIRNAME


# ── the profile ──


@dataclass(frozen=True)
class Issue:
    level: str  # "error" | "warning"
    path: str
    message: str

    def render(self) -> str:
        return f"  {self.level.upper():7} {self.path or '(profile)'}: {self.message}"


class ProfileError(ShiftError):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("\n".join(i.render() for i in issues))


class _Model(BaseModel):
    # camelCase on the wire like agent-config.json; an unknown key is an error,
    # unlike there. agent-config.json is read by a server that must keep running
    # on a typo, this file is read once by an operator who can fix it, and a
    # setting that silently does nothing is how a shift runs on the wrong cadence.
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )


class Loop(_Model):
    id: str = Field(pattern=LOOP_ID, description="Unique, lowercase [a-z0-9-].")
    enabled: bool = True
    agent: str = Field(
        pattern=SLUG, description="Agent slug, e.g. market_making_expert."
    )
    strategy: str = Field(
        pattern=SLUG, description="Loop (strategy) slug under the agent."
    )
    connector: str = Field(
        min_length=1,
        description="Test venue the loop trades on.",
        json_schema_extra={"enum": sorted(TEST_VENUES)},
    )
    trading_context: str = Field(
        default="",
        description="Sent as the run's trading_context; names the pair and venue.",
    )
    capital_quote: float = Field(
        gt=0, description="Quote-currency capital for this loop."
    )
    request_share: float = Field(
        gt=0,
        le=1,
        description="Share of the request budget; normalised over the loops started.",
    )
    frequency_sec: int = Field(
        ge=MIN_FREQUENCY_SEC,
        le=86400,
        description="Seconds between ticks (the pacer may stretch it).",
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Run-config overrides applied last. Not agent_key, frequency_sec, "
        "total_amount_quote, shift_id, shift_ends_at or request_share.",
    )


class Profile(_Model):
    name: str = Field(
        pattern=PROFILE_NAME, description="Equals the file name without .json."
    )
    description: str = ""
    hours: float = Field(gt=0, le=MAX_HOURS, description="Length of the shift.")
    capital_quote: float = Field(
        gt=0, description="Ceiling on the sum of enabled loops' capital."
    )
    model: str | None = Field(
        default=None,
        description="Agent key for every loop; null uses agent-config.json 'model'.",
    )
    request_budget: int | None = Field(
        default=None,
        ge=1,
        description="Most model requests the shift plans to spend; null = what the daily "
        "budget leaves.",
    )
    loops: list[Loop] = Field(min_length=1)


def json_schema() -> dict[str, Any]:
    """The JSON Schema of a profile file. ``shifts/shift-profile.schema.json`` is this."""
    schema = Profile.model_json_schema(by_alias=True)
    comments = {"^[_$]": {}}
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Condor shift profile"
    # Keys starting with _ or $ are comments (the agent-config.json convention),
    # stripped before validation; the schema must let an editor accept them.
    schema["patternProperties"] = comments
    schema["$defs"]["Loop"]["patternProperties"] = comments
    return schema


def _strip_comments(raw: Any) -> Any:
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if not str(k).startswith(("_", "$"))}
    return raw


def parse_profile(raw: Any) -> Profile:
    """Validate a decoded profile. Raises :class:`ProfileError` with every problem."""
    if not isinstance(raw, dict):
        raise ProfileError([Issue("error", "", "the top level must be a JSON object")])
    cleaned = _strip_comments(raw)
    if isinstance(cleaned.get("loops"), list):
        loops = []
        for loop in cleaned["loops"]:
            loop = _strip_comments(loop)
            if isinstance(loop, dict) and isinstance(loop.get("config"), dict):
                loop = {**loop, "config": _strip_comments(loop["config"])}
            loops.append(loop)
        cleaned = {**cleaned, "loops": loops}
    try:
        return Profile.model_validate(cleaned)
    except ValidationError as exc:
        raise ProfileError(
            [
                Issue(
                    "error",
                    _loc(item["loc"]),
                    "unknown key" if item["type"] == "extra_forbidden" else item["msg"],
                )
                for item in exc.errors()
            ]
        ) from exc


def _loc(loc: Iterable[Any]) -> str:
    out = ""
    for part in loc:
        out += (
            f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
        )
    return out


def profile_path(name: str) -> Path:
    """``shifts/NAME.json``, or the path itself when one is given."""
    if os.sep in name or name.endswith(".json"):
        return Path(name).expanduser()
    return PROFILES_DIR / f"{name}.json"


def load_profile(name: str) -> tuple[Profile, Path]:
    path = profile_path(name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(
            [
                Issue(
                    "error",
                    str(path),
                    f"cannot read the profile — {exc.strerror or exc}",
                )
            ]
        ) from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ProfileError(
            [Issue("error", str(path), f"not valid JSON — {exc}")]
        ) from exc
    return parse_profile(raw), path


def enabled_loops(profile: Profile) -> list[Loop]:
    return [loop for loop in profile.loops if loop.enabled]


def resolve_model(
    profile: Profile, cfg: agent_config.AgentRuntimeConfig
) -> tuple[str, str]:
    """``(model, where it came from)``; empty when neither names one."""
    if profile.model:
        return profile.model, "profile"
    if cfg.model:
        return cfg.model, "agent-config.json"
    return "", ""


def load_agent_config() -> agent_config.LoadedConfig:
    """``agent-config.json`` strict: an unattended run starts on what it was given."""
    try:
        return agent_config.get_loaded(strict=True)
    except agent_config.AgentConfigError as exc:
        raise ShiftError(f"agent-config.json is not usable: {exc}") from exc


def validate_profile(
    profile: Profile,
    *,
    stem: str | None = None,
    cfg: agent_config.AgentRuntimeConfig | None = None,
    check_agents: bool = True,
) -> list[Issue]:
    """Everything wrong with a parsed profile; errors block a start, warnings do not."""
    issues: list[Issue] = []

    def error(path: str, message: str) -> None:
        issues.append(Issue("error", path, message))

    def warn(path: str, message: str) -> None:
        issues.append(Issue("warning", path, message))

    if stem is not None and profile.name != stem:
        error(
            "name",
            f"'{profile.name}' must equal the file name '{stem}': a shift id is made "
            "from the name and has to lead back to exactly one file",
        )

    ids = Counter(loop.id for loop in profile.loops)
    for loop_id, n in ids.items():
        if n > 1:
            error("loops", f"id '{loop_id}' is used {n} times; ids must be unique")

    live = enabled_loops(profile)
    if not live:
        error("loops", "no loop is enabled; there is nothing to run")
    capital = sum(loop.capital_quote for loop in live)
    if capital > profile.capital_quote + 1e-9:
        error(
            "capitalQuote",
            f"the enabled loops allocate {capital:,.2f}, more than the profile's "
            f"{profile.capital_quote:,.2f}",
        )

    for i, loop in enumerate(profile.loops):
        where = f"loops[{i}]"
        if loop.connector not in TEST_VENUES:
            error(
                f"{where}.connector",
                f"'{loop.connector}' is not a paper venue (a mainnet name would trade real "
                f"money); paper venues: {', '.join(sorted(TEST_VENUES))}",
            )
        for key in loop.config:
            if key in RESERVED_CONFIG_KEYS:
                error(
                    f"{where}.config.{key}",
                    "is set by the runner from the loop's own fields; set the field instead",
                )
            if _SECRET_KEY.search(str(key)):
                error(
                    f"{where}.config.{key}",
                    "looks like a credential; a profile is a file in the repo, "
                    "credentials belong in the Hummingbot API",
                )
        mode = loop.config.get("execution_mode", "loop")
        if mode != "loop":
            error(
                f"{where}.config.execution_mode",
                f"'{mode}' runs a single experiment tick with no journal; a shift needs 'loop'",
            )
        action = loop.config.get("shift_end_action", "winddown")
        if action not in SHIFT_END_ACTIONS:
            error(
                f"{where}.config.shift_end_action",
                f"'{action}' is not one of {', '.join(SHIFT_END_ACTIONS)}",
            )
        context = loop.trading_context.lower()
        configured = " ".join(
            str(v) for v in loop.config.values() if isinstance(v, str)
        )
        if loop.connector not in context and loop.connector not in configured:
            warn(
                f"{where}.tradingContext",
                f"does not mention '{loop.connector}' (nor does config): the loop reads its "
                "venue from its context, so it may not trade where the profile says",
            )
        for token in sorted(set(re.findall(r"[a-z0-9]+(?:_[a-z0-9]+)+", context))):
            if token in MAINNET_TWINS and token != loop.connector:
                warn(
                    f"{where}.tradingContext",
                    f"names the mainnet connector '{token}': fine for reading candles "
                    "(testnets serve none), wrong if orders would go there",
                )
        if check_agents and loop.enabled:
            issues.extend(_check_agent_exists(loop, where))

    if cfg is not None:
        model, _source = resolve_model(profile, cfg)
        if not model:
            error(
                "model",
                "no model: set 'model' in the profile or in agent-config.json; a loop "
                "would otherwise run on the agent's own default",
            )
        elif is_free_model(model) and not cfg.budget.enabled:
            warn(
                "model",
                "is a free model and agent-config.json budget.enabled is false: nothing "
                "will pace the loops or stop them at the provider's daily cap",
            )
    return issues


def _check_agent_exists(loop: Loop, where: str) -> list[Issue]:
    from condor.agents.agent import AgentStore
    from condor.agents.strategy import StrategyStore

    if AgentStore().get(loop.agent) is None:
        return [
            Issue("error", f"{where}.agent", f"no agent '{loop.agent}' in this install")
        ]
    strategy = StrategyStore().get(loop.agent, loop.strategy)
    if strategy is None:
        return [
            Issue(
                "error",
                f"{where}.strategy",
                f"agent '{loop.agent}' has no loop '{loop.strategy}'",
            )
        ]
    if strategy.slug != loop.strategy:
        # The routes find a loop's engines by the directory name and the engine
        # registers under the slug of the strategy's *name*. When they differ the
        # stop route answers "no running strategy" for a loop that is running.
        return [
            Issue(
                "error",
                f"{where}.strategy",
                f"the loop's folder is '{loop.strategy}' but its name slugifies to "
                f"'{strategy.slug}'; the stop route could not find it",
            )
        ]
    return []


def redact(value: Any) -> Any:
    """``value`` with every credential-looking key blanked, for anything written to disk."""
    if isinstance(value, dict):
        return {
            k: ("***" if _SECRET_KEY.search(str(k)) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"(://[^/\s:@]+:)[^@\s]+@", r"\1***@", value)
    return value


def start_config(
    loop: Loop, *, model: str, shift_id: str, ends_at: float, share: float
) -> dict[str, Any]:
    """The run-config overrides one loop is started with. The profile's ``config`` wins last."""
    config: dict[str, Any] = {
        "agent_key": model,
        "frequency_sec": loop.frequency_sec,
        "total_amount_quote": _tidy(loop.capital_quote),
        "shift_id": shift_id,
        "shift_ends_at": round(ends_at, 3),
        "request_share": round(share, 4),
    }
    config.update(_strip_comments(loop.config))
    return config


def _tidy(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def normalised_shares(loops: Iterable[Loop]) -> dict[str, float]:
    loops = list(loops)
    total = sum(loop.request_share for loop in loops)
    return {loop.id: loop.request_share / total for loop in loops} if total > 0 else {}


# ── the request-budget forecast ──


@dataclass(frozen=True)
class LoopRate:
    loop_id: str
    frequency_sec: int
    share: float
    requests_per_tick: float
    source: str


@dataclass(frozen=True)
class Segment:
    """The part of a shift that falls on one UTC day, against that day's budget."""

    start: float
    end: float
    day: str
    available: float
    used_before: int
    planned_by_loop: dict[str, float]
    allotted_by_loop: dict[str, float]

    @property
    def hours(self) -> float:
        return (self.end - self.start) / 3600.0

    @property
    def planned(self) -> float:
        return sum(self.planned_by_loop.values())

    @property
    def over(self) -> bool:
        return self.planned > self.available + 1e-9

    @property
    def overspend(self) -> int:
        return max(0, math.ceil(self.planned - self.available - 1e-9))


@dataclass(frozen=True)
class Forecast:
    segments: list[Segment]
    request_budget: int | None
    #: Per loop, the ``frequencySec`` that fits (with headroom); ``None`` = no
    #: budget in some window, so no frequency would help.
    recommended: dict[str, int | None]
    #: Loops whose own share cannot pay for their cadence, even if the total fits.
    over_share: list[str]

    @property
    def total_planned(self) -> float:
        return sum(s.planned for s in self.segments)

    @property
    def over_cap(self) -> bool:
        return (
            self.request_budget is not None
            and self.total_planned > self.request_budget + 1e-9
        )

    @property
    def overspend(self) -> bool:
        return self.over_cap or any(s.over for s in self.segments)


def split_at_resets(start: float, end: float) -> list[tuple[float, float]]:
    """``[start, end]`` cut at every 00:00 UTC inside it, the day the budget resets."""
    cuts = [start]
    reset = next_reset(start)
    while reset < end:
        cuts.append(reset)
        reset = next_reset(reset)
    cuts.append(end)
    return list(zip(cuts, cuts[1:]))


def _round_up(value: float, step: int = 5) -> int:
    return int(math.ceil(value / step) * step)


def forecast(
    rates: list[LoopRate],
    *,
    start: float,
    end: float,
    spendable: int,
    used_today: int,
    request_budget: int | None = None,
) -> Forecast:
    """Requests the plan would spend against what each UTC day can pay for.

    A shift that crosses 00:00 UTC is judged on each side against its own day: the
    first segment has today's budget less what is already used, every later one a
    full day's (nothing else is assumed to spend it). A loop is also held to its
    ``share`` of each segment's budget, because the pacer holds it to exactly that
    whether or not its siblings use theirs.

    The tick rate is ``3600 / frequencySec``, which ignores the time a tick takes,
    so the forecast is an upper bound on the plan. It errs towards the warning.
    """
    segments: list[Segment] = []
    for index, (seg_start, seg_end) in enumerate(split_at_resets(start, end)):
        used = used_today if index == 0 else 0
        available = float(max(0, spendable - used))
        seconds = seg_end - seg_start
        planned = {
            r.loop_id: seconds * r.requests_per_tick / r.frequency_sec for r in rates
        }
        allotted = {r.loop_id: available * r.share for r in rates}
        segments.append(
            Segment(
                seg_start,
                seg_end,
                utc_day(seg_start),
                available,
                used,
                planned,
                allotted,
            )
        )

    recommended: dict[str, int | None] = {}
    over_share: list[str] = []
    total = sum(s.planned for s in segments)
    scale = 1.0
    if request_budget is not None and total > request_budget:
        scale = total / (request_budget * HEADROOM)
    for r in rates:
        needed = float(r.frequency_sec) * (scale if scale > 1.0 else 1.0)
        starved = False
        for seg in segments:
            allot = seg.allotted_by_loop[r.loop_id] * HEADROOM
            planned = seg.planned_by_loop[r.loop_id]
            if planned <= 0:
                continue
            if allot <= 0:
                starved = True
                break
            if planned > allot:
                needed = max(
                    needed, (seg.end - seg.start) * r.requests_per_tick / allot
                )
        if starved:
            recommended[r.loop_id] = None
        else:
            recommended[r.loop_id] = (
                _round_up(needed) if needed > r.frequency_sec else r.frequency_sec
            )
        if any(
            seg.planned_by_loop[r.loop_id] > seg.allotted_by_loop[r.loop_id] + 1e-9
            for seg in segments
        ):
            over_share.append(r.loop_id)
    return Forecast(segments, request_budget, recommended, over_share)


# ── the plan ──


@dataclass
class PlanLoop:
    loop: Loop
    #: ``True`` / ``False`` from the Hummingbot API's credential list, ``None``
    #: when it could not be asked.
    runnable: bool | None
    reason: str
    share: float
    requests_per_tick: float
    source: str


@dataclass
class Plan:
    profile: Profile
    model: str
    model_source: str
    metered: bool
    start: float
    end: float
    budget: agent_config.Budget
    used_today: int
    loops: list[PlanLoop]
    forecast: Forecast | None
    credentials_note: str = ""


def build_plan(
    profile: Profile,
    *,
    now: float,
    cfg: agent_config.AgentRuntimeConfig,
    credentials: set[str] | None,
    credentials_note: str = "",
    ledger_directory: Path | None = None,
) -> Plan:
    """The plan for ``profile`` if it started at ``now``. Reads files; changes nothing."""
    from condor.agents.strategy import StrategyStore

    model, source = resolve_model(profile, cfg)
    metered = (
        bool(model)
        and cfg.budget.applies_to == "all"
        or is_free_model(
            model.partition(":")[2] if model.startswith("openrouter") else model
        )
    )
    end = now + profile.hours * 3600.0
    used = shift_report.used_today(now, ledger_directory)
    by_caller = shift_report.requests_by_caller(now, directory=ledger_directory)

    live = enabled_loops(profile)
    planned_loops: list[PlanLoop] = []
    for loop in live:
        runnable: bool | None = (
            True
            if loop.connector in SIM_PORTFOLIOS
            else None if credentials is None else loop.connector in credentials
        )
        reason = (
            ""
            if runnable is not False
            else f"no credential for {loop.connector} in {ACCOUNT}"
        )
        planned_loops.append(PlanLoop(loop, runnable, reason, 0.0, 0.0, ""))

    will_run = [p for p in planned_loops if p.runnable is not False]
    shares = normalised_shares(p.loop for p in will_run)
    for p in planned_loops:
        p.share = shares.get(p.loop.id, 0.0)
        strategy = StrategyStore().get(p.loop.agent, p.loop.strategy)
        measured: tuple[float | None, int, int] = (None, 0, 0)
        if strategy is not None:
            measured = shift_report.measure_requests_per_tick(
                f"{p.loop.agent}.{strategy.slug}", strategy.home, by_caller
            )
        if measured[0] is not None:
            p.requests_per_tick = measured[0]
            p.source = f"measured: {measured[1]} requests over {measured[2]} ticks"
        else:
            p.requests_per_tick = float(cfg.budget.assumed_requests_per_tick)
            p.source = (
                f"assumed (budget.assumedRequestsPerTick; {measured[2]} measured ticks, "
                f"need {shift_report.MIN_MEASURED_TICKS})"
            )

    fc = None
    if will_run:
        fc = forecast(
            [
                LoopRate(
                    p.loop.id,
                    p.loop.frequency_sec,
                    p.share,
                    p.requests_per_tick,
                    p.source,
                )
                for p in will_run
            ],
            start=now,
            end=end,
            spendable=cfg.budget.spendable,
            used_today=used,
            request_budget=profile.request_budget,
        )
    return Plan(
        profile,
        model,
        source,
        metered,
        now,
        end,
        cfg.budget,
        used,
        planned_loops,
        fc,
        credentials_note,
    )


def render_plan(plan: Plan) -> str:
    p = plan.profile
    out: list[str] = []
    w = out.append
    w(f"Shift plan: {p.name} — {p.description or 'no description'}")
    w(f"  window   {iso(plan.start)} → {iso(plan.end)}  ({p.hours:g}h, if started now)")
    w(f"  model    {plan.model or 'NONE'}  (from {plan.model_source or 'nowhere'})")
    w(
        f"  capital  {p.capital_quote:,.2f} ceiling; enabled loops allocate "
        f"{sum(pl.loop.capital_quote for pl in plan.loops):,.2f}"
    )
    b = plan.budget
    if plan.metered and b.enabled:
        w(
            f"  budget   enforced: cap {b.daily_request_cap}/day, {b.reserve_requests} reserved, "
            f"{b.spendable} spendable; {plan.used_today} already used today (UTC)"
        )
    elif plan.metered:
        w(
            f"  budget   NOT ENFORCED (agent-config.json budget.enabled is false); the "
            f"provider's own cap of {b.daily_request_cap}/day is the only brake. "
            f"{plan.used_today} used today per the ledger"
        )
    else:
        w(
            "  budget   does not apply to this model (not a :free model); forecast is informational"
        )
    if plan.credentials_note:
        w(f"  venues   {plan.credentials_note}")
    w("")
    w("LOOPS")
    w(
        f"  {'id':16} {'agent / strategy':44} {'connector':28} {'capital':>9} {'share':>6} {'every':>6}  runnable"
    )
    for pl in plan.loops:
        lp = pl.loop
        state = {True: "yes", False: "NO — SKIPPED", None: "unknown"}[pl.runnable]
        w(
            f"  {lp.id:16} {lp.agent + ' / ' + lp.strategy:44} {lp.connector:28} "
            f"{lp.capital_quote:9,.0f} {pl.share:6.2f} {lp.frequency_sec:5d}s  {state}"
        )
        if pl.reason:
            w(f"  {'':16} -> {pl.reason}")
    disabled = [lp for lp in p.loops if not lp.enabled]
    if disabled:
        w(f"  disabled: {', '.join(lp.id for lp in disabled)}")
    w("")
    fc = plan.forecast
    if fc is None:
        w("FORECAST  nothing would run, so nothing would be spent.")
        return "\n".join(out)

    w(
        "REQUEST FORECAST  (upper bound: 3600/frequencySec ticks an hour, tick duration ignored)"
    )
    w(f"  {'loop':16} {'req/tick':>9} {'ticks/h':>8} {'req/h':>7}   source of req/tick")
    for pl in plan.loops:
        if pl.runnable is False:
            continue
        per_h = 3600.0 / pl.loop.frequency_sec
        w(
            f"  {pl.loop.id:16} {pl.requests_per_tick:9.1f} {per_h:8.1f} {per_h * pl.requests_per_tick:7.1f}   {pl.source}"
        )
    w("")
    for seg in fc.segments:
        w(
            f"  {hhmm(seg.start)}–{hhmm(seg.end)} UTC on {seg.day}  ({seg.hours:.2f}h): plans "
            f"{math.ceil(seg.planned)} requests against {int(seg.available)} available "
            f"({seg.used_before} used before it)"
            + ("  <-- OVERSPENDS by " + str(seg.overspend) if seg.over else "  OK")
        )
    if len(fc.segments) > 1:
        w(
            "  The shift crosses 00:00 UTC, when the daily budget resets: each side is "
            "judged against its own day. The later side assumes nothing else spends it."
        )
    if fc.request_budget is not None:
        w(
            f"  profile requestBudget {fc.request_budget}: plan totals {math.ceil(fc.total_planned)}"
            + ("  <-- OVER" if fc.over_cap else "  OK")
        )
    w("")
    if fc.overspend:
        w(
            "WARNING: this plan would overspend the request budget. The pacer would stretch the"
        )
        w(
            "loops (or they would hold, idle, until the reset), so the shift would not run the"
        )
        w(
            "cadence the profile asks for. Recommended frequencySec to fit with 10% headroom:"
        )
        for loop_id, freq in fc.recommended.items():
            current = next(
                pl.loop.frequency_sec for pl in plan.loops if pl.loop.id == loop_id
            )
            if freq is None:
                w(f"  {loop_id}: no budget left in some window; start after the reset")
            elif freq != current:
                w(f"  {loop_id}: {current}s -> {freq}s")
    elif fc.over_share:
        w(
            "NOTE: the total fits, but these loops' own request share cannot pay for their cadence;"
        )
        w("the pacer will stretch them even though siblings leave budget unused:")
        for loop_id in fc.over_share:
            freq = fc.recommended.get(loop_id)
            w(
                f"  {loop_id}: raise requestShare, or use frequencySec {freq}"
                if freq
                else f"  {loop_id}"
            )
    else:
        w("OK: the plan fits the request budget.")
    return "\n".join(out)


# ── clients ──


class ApiError(ShiftError):
    def __init__(self, status: int, detail: str, where: str):
        self.status = status
        self.detail = detail
        super().__init__(f"{where}: HTTP {status} {detail}".strip())


class Unreachable(ShiftError):
    """The server did not answer at all (down, refusing, timing out)."""


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])[:300]
    return str(body)[:200]


class _Client:
    """A JSON-over-HTTP client that maps failure to two exceptions and nothing else."""

    prefix = ""

    def __init__(
        self,
        base_url: str,
        *,
        auth: tuple[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url, auth=auth, transport=transport, timeout=timeout
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        return {}

    def _unauthorized(self) -> bool:
        """Called on a 401; True if a retry might now succeed."""
        return False

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        where = f"{method} {path}"
        for attempt in (0, 1):
            try:
                response = await self._http.request(
                    method,
                    self.prefix + path,
                    json=json,
                    params=params,
                    headers=self._headers(),
                    timeout=(
                        timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT
                    ),
                )
            except httpx.HTTPError as exc:
                raise Unreachable(f"{where}: {type(exc).__name__} {exc}"[:240]) from exc
            if response.status_code == 401 and attempt == 0 and self._unauthorized():
                continue
            break
        if response.status_code >= 400:
            raise ApiError(response.status_code, _detail(response), where)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return await self.request("POST", path, json=body, **kw)


class CondorClient(_Client):
    """The Condor dashboard API, authenticated with a locally minted admin JWT."""

    prefix = API_PREFIX

    def __init__(self, base_url: str, mint_token: Callable[[], str], **kw: Any) -> None:
        super().__init__(base_url, **kw)
        self._mint = mint_token
        self._token: str | None = None

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            self._token = self._mint()
        return {"Authorization": f"Bearer {self._token}"}

    def _unauthorized(self) -> bool:
        # A 24 h token over a 10 h run is fine; one minted at 20:00 for a run that
        # starts at 20:05 the next day is not. Mint again once, then give up.
        self._token = None
        return True


class HummingbotClient(_Client):
    """The Hummingbot API, basic auth from the server entry in ``config.yml``."""


def mint_admin_token() -> str:
    """An admin JWT signed with the secret the server verifies against."""
    from condor.web.auth import create_jwt
    from config_manager import get_config_manager

    admin = get_config_manager().admin_id or int(os.environ.get("ADMIN_USER_ID") or 0)
    if not admin:
        raise ShiftError("no admin user: set admin_id in config.yml or ADMIN_USER_ID")
    return create_jwt(int(admin), role="admin")


def dashboard_url() -> str:
    from utils.config import WEB_URL

    return WEB_URL


def make_clients() -> tuple[CondorClient, HummingbotClient]:
    """Clients for this install: the dashboard, and the Hummingbot API behind it."""
    from config_manager import get_config_manager

    cm = get_config_manager()
    name = cm.get_default_server() or next(iter(cm.list_servers()), None)
    server = cm.get_server(name) if name else None
    if not server:
        raise ShiftError("config.yml has no Hummingbot API server")
    hb = HummingbotClient(
        f"http://{server['host']}:{server['port']}",
        auth=(str(server.get("username", "")), str(server.get("password", ""))),
    )
    return CondorClient(dashboard_url(), mint_admin_token, timeout=60.0), hb


# ── preflight ──

PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"
_BADGE = {PASS: "✓", WARN: "!", FAIL: "✗", UNKNOWN: "?"}


@dataclass
class Check:
    name: str
    state: str
    detail: str
    loop: str | None = None

    def render(self) -> str:
        scope = f"[{self.loop}] " if self.loop else ""
        return f"  {_BADGE[self.state]} {scope}{self.name}: {self.detail}"


@dataclass
class Preflight:
    """What was checked before a start, and which loops will not run."""

    checks: list[Check] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    balances: list[dict[str, Any]] | None = None
    sim_costs_start: dict[str, Any] | None = None
    runnable: list[str] = field(default_factory=list)

    def add(self, name: str, state: str, detail: str, loop: str | None = None) -> Check:
        check = Check(name, state, detail, loop)
        self.checks.append(check)
        return check

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.state == FAIL and c.loop is None]

    @property
    def ok(self) -> bool:
        return not self.failures and bool(self.runnable)

    def render(self) -> str:
        lines = ["Preflight", *[c.render() for c in self.checks]]
        for loop_id, reason in self.skipped.items():
            lines.append(f"  SKIPPED {loop_id}: {reason}")
        lines.append("  => " + ("READY" if self.ok else "NOT READY"))
        return "\n".join(lines)


def _server_ready(options: Any, model: str) -> tuple[str, str]:
    """Whether the server's own environment can run ``model``, from ``/sessions/options``."""
    base = model.split(":", 1)[0]
    for row in (options or {}).get("agents", []) if isinstance(options, dict) else []:
        if str(row.get("key", "")).split(":", 1)[0] == base:
            if row.get("ready") is False:
                return FAIL, str(
                    row.get("detail") or "the server says it cannot run this model"
                )
            return PASS, str(row.get("detail") or "ready")
    return UNKNOWN, f"the server's model list has no '{base}' row to read"


async def run_preflight(
    profile: Profile,
    *,
    cfg: agent_config.LoadedConfig,
    model: str,
    condor: CondorClient,
    hb: HummingbotClient,
    now: float,
    shift_root: Path,
    live_shifts: Callable[[], list[str]] | None = None,
    model_error: Callable[[str], Any] | None = None,
    ledger_directory: Path | None = None,
) -> Preflight:
    """Every check a start depends on. Global failures refuse; per-loop ones skip the loop.

    A loop whose venue has no credential is skipped *loudly* rather than failing
    the run: the owner may have keys for one exchange tonight and not another,
    and the loops that can trade should. ``UNKNOWN`` never refuses (a check that
    could not run is not a check that failed) except where the thing it was
    checking is what the run cannot do without.
    """
    pre = Preflight()
    live = enabled_loops(profile)

    unrecognized = loaded_unrecognized(cfg)
    pre.add(
        "agent-config",
        WARN if unrecognized else PASS,
        f"{cfg.path or 'built-in defaults'}; maxSteps {cfg.config.max_steps}, thinking "
        f"{cfg.config.thinking.effort or cfg.config.thinking.max_tokens or 'off'}"
        + (
            f"; settings this build ignores: {', '.join(unrecognized)}"
            if unrecognized
            else ""
        ),
    )

    try:
        me = await condor.get("/auth/me")
        pre.add(
            "condor server",
            PASS,
            f"{condor.base_url} answers, authenticated as user {me.get('id')} ({me.get('role')})",
        )
    except ShiftError as exc:
        pre.add(
            "condor server", FAIL, f"{exc}; is Condor running (tmux session 'condor')?"
        )
        return pre

    if any(loop.connector not in SIM_PORTFOLIOS for loop in live):
        try:
            info = await hb.get("/")
            pre.add(
                "hummingbot api",
                PASS,
                f"{hb.base_url} {info.get('status', 'answers') if isinstance(info, dict) else 'answers'}",
            )
        except ShiftError as exc:
            pre.add("hummingbot api", FAIL, str(exc))
            return pre
    else:
        pre.add("hummingbot api", PASS, "not needed for simulated Kraken")

    # The server's environment, not this process's: the key the loops use is the
    # one the *server* loaded at boot, and a ``.env`` edited since is not in it.
    try:
        options = await condor.get("/sessions/options")
        state, detail = _server_ready(options, model)
    except ShiftError as exc:
        state, detail = UNKNOWN, f"could not ask the server: {exc}"
    pre.add("model credentials (server env)", state, detail)

    if model_error is not None:
        try:
            problem = await model_error(model)
        except Exception as exc:  # noqa: BLE001 - a checker that crashes is "unknown"
            pre.add("model buildable", UNKNOWN, f"{type(exc).__name__}: {exc}"[:200])
        else:
            pre.add("model buildable", FAIL if problem else PASS, problem or model)

    budget = cfg.config.budget
    used = shift_report.used_today(now, ledger_directory)
    if is_free_model(model) or budget.applies_to == "all":
        if not budget.enabled:
            pre.add(
                "request budget",
                FAIL,
                "agent-config.json budget.enabled is false: nothing would pace the loops "
                "or stop them at the provider's daily cap on an unattended run",
            )
        elif budget.spendable - used <= 0:
            pre.add(
                "request budget",
                FAIL,
                f"today's budget is spent ({used} of {budget.spendable}); it resets at "
                f"{hhmm(next_reset(now))} UTC",
            )
        else:
            pre.add(
                "request budget",
                PASS,
                f"{budget.spendable - used} of {budget.spendable} spendable requests left today "
                f"(cap {budget.daily_request_cap}, reserve {budget.reserve_requests})",
            )
    else:
        pre.add("request budget", PASS, "does not apply to this model")

    if live_shifts is not None:
        running = live_shifts()
        if running:
            pre.add(
                "no shift already running",
                FAIL,
                f"{', '.join(running)} is still active; stop it first",
            )
            return pre
        pre.add("no shift already running", PASS, "no active shift record")

    try:
        shift_root.mkdir(parents=True, exist_ok=True)
        probe = shift_root / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
        pre.add("shift directory", PASS, str(shift_root))
    except OSError as exc:
        pre.add(
            "shift directory",
            FAIL,
            f"{shift_root} is not writable: {exc.strerror or exc}",
        )
        return pre

    registered: set[str] = set()
    if any(loop.connector not in SIM_PORTFOLIOS for loop in live):
        try:
            credentials = await hb.get(f"/accounts/{ACCOUNT}/credentials")
            registered = set(credentials) if isinstance(credentials, list) else set()
        except ShiftError as exc:
            pre.add(
                "credentials", FAIL, f"could not list {ACCOUNT}'s credentials: {exc}"
            )
            return pre

    for loop in live:
        if loop.connector in SIM_PORTFOLIOS:
            pre.add("credential", PASS, "simulated account; no exchange key", loop.id)
            continue
        if loop.connector not in registered:
            pre.skipped[loop.id] = (
                f"no credential for {loop.connector} registered in {ACCOUNT} "
                f"(registered: {', '.join(sorted(registered)) or 'none'})"
            )
            pre.add("credential", FAIL, f"{loop.connector} is not registered", loop.id)
            continue
        pre.add("credential", PASS, f"{loop.connector} registered", loop.id)

    balances: list[dict[str, Any]] = []
    for connector in sorted({l.connector for l in live if l.id not in pre.skipped}):
        owners = [
            l for l in live if l.connector == connector and l.id not in pre.skipped
        ]
        try:
            if connector in SIM_PORTFOLIOS:
                state_json = await condor.get(SIM_PORTFOLIOS[connector])
                rows = shift_report.summarize_sim_balances(state_json, connector)
                if connector == "kraken_sim":
                    pre.sim_costs_start = await condor.get("/sim/kraken/costs")
            else:
                state_json = await hb.post(
                    "/portfolio/state",
                    {
                        "account_names": [ACCOUNT],
                        "connector_names": [connector],
                        "refresh": True,
                    },
                )
                rows = [
                    r
                    for r in shift_report.summarize_balances(state_json)
                    if r["connector"] == connector
                ]
        except ShiftError as exc:
            for loop in owners:
                pre.skipped[loop.id] = f"balance unreachable on {connector}: {exc}"
                pre.add("balance", FAIL, str(exc), loop.id)
            continue
        if not rows:
            for loop in owners:
                pre.skipped[loop.id] = (
                    f"{connector} returned no balances (wrong keys, or an empty account)"
                )
                pre.add("balance", FAIL, "no balances returned", loop.id)
            continue
        balances.extend(rows)
        value = shift_report.balance_totals(rows).get(f"{ACCOUNT}/{connector}")
        need = sum(l.capital_quote for l in owners)
        for loop in owners:
            if value is None:
                pre.add(
                    "balance",
                    UNKNOWN,
                    f"{connector} reachable; no value to compare with capital",
                    loop.id,
                )
            elif value < need:
                pre.add(
                    "balance",
                    WARN,
                    f"{connector} holds about {value:,.0f}, the loops here allocate {need:,.0f}",
                    loop.id,
                )
            else:
                pre.add(
                    "balance",
                    PASS,
                    f"{connector} holds about {value:,.0f} (loops allocate {need:,.0f})",
                    loop.id,
                )
    pre.balances = balances

    pre.runnable = [l.id for l in live if l.id not in pre.skipped]
    if not pre.runnable:
        pre.add(
            "runnable loops", FAIL, "every enabled loop was skipped; nothing to run"
        )
        return pre

    # Live sessions of the same loop and context: a second one would trade the
    # same book twice. Found by asking the server, so a loop started by hand from
    # the dashboard counts too.
    for loop in live:
        if loop.id in pre.skipped:
            continue
        try:
            detail_json = await condor.get(
                f"/agents/{loop.agent}/strategies/{loop.strategy}"
            )
        except ShiftError as exc:
            pre.add(
                "existing sessions",
                UNKNOWN,
                f"could not list {loop.agent}/{loop.strategy}: {exc}",
                loop.id,
            )
            continue
        clash = [
            i["agent_id"]
            for i in (detail_json or {}).get("instances", [])
            if i.get("status") in ("running", "paused")
            and (
                loop.connector in SIM_PORTFOLIOS
                or (i.get("trading_context") or "") == loop.trading_context
            )
        ]
        if clash:
            pre.add(
                "existing sessions",
                FAIL,
                f"{', '.join(clash)} already runs this loop and context",
                None,
            )
        else:
            pre.add(
                "existing sessions",
                PASS,
                "no live session with the same context",
                loop.id,
            )
        if loop.connector in SIM_PORTFOLIOS:
            try:
                orders = await condor.get("/sim/kraken/orders")
                open_orders = (orders or {}).get("open") or []
                pre.add(
                    "existing sim orders",
                    FAIL if open_orders else PASS,
                    (
                        f"[{loop.id}] {len(open_orders)} open order(s)"
                        if open_orders
                        else f"[{loop.id}] none"
                    ),
                )
            except ShiftError as exc:
                pre.add("existing sim orders", FAIL, f"[{loop.id}] {exc}")
    return pre


def loaded_unrecognized(cfg: agent_config.LoadedConfig) -> list[str]:
    return list(cfg.unrecognized)


# ── the shift record ──

ACTIVE_STATES = ("starting", "running", "stopping")
MAX_EVENTS = 500


class ShiftRecord:
    """``shift.json``: the one file that says what this shift is and what happened to it."""

    def __init__(self, directory: Path, data: dict[str, Any]) -> None:
        self.directory = directory
        self.data = data

    @classmethod
    def load(cls, directory: Path) -> "ShiftRecord":
        return cls(directory, shift_report.load_record(directory))

    def save(self) -> None:
        atomic_write_json(
            self.directory / shift_report.RECORD_FILE, self.data, indent=2
        )

    def event(
        self, kind: str, text: str, *, now: float, loop: str | None = None
    ) -> None:
        events = self.data.setdefault("events", [])
        events.append({"at": round(now, 3), "kind": kind, "loop": loop, "text": text})
        del events[:-MAX_EVENTS]

    @property
    def shift_id(self) -> str:
        return str(self.data["shift_id"])

    @property
    def state(self) -> str:
        return str(self.data.get("state", ""))

    @property
    def loops(self) -> dict[str, dict[str, Any]]:
        return self.data["loops"]


def pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def runner_alive(record: ShiftRecord, now: float, poll_sec: int = POLL_SEC) -> bool:
    """Whether the runner process is up: its pid exists *and* it has polled lately.

    A pid alone is not proof (they are recycled), and a heartbeat alone is not
    (a runner killed a second ago still has a fresh one).
    """
    runner = record.data.get("runner") or {}
    if not pid_alive(runner.get("pid")):
        return False
    live = _read_json(record.directory / LIVE_FILE)
    beat = (live or {}).get("at")
    return isinstance(beat, (int, float)) and now - beat < 3 * poll_sec + 30


def list_shift_dirs() -> list[Path]:
    root = shifts_dir()
    try:
        return sorted(
            p
            for p in root.iterdir()
            if p.is_dir() and (p / shift_report.RECORD_FILE).is_file()
        )
    except OSError:
        return []


def active_shift_ids(now: float) -> list[str]:
    """Shifts whose record says active and whose loops may still be running."""
    out = []
    for directory in list_shift_dirs():
        record = _read_json(directory / shift_report.RECORD_FILE) or {}
        if record.get("state") not in ACTIVE_STATES:
            continue
        ends = record.get("ends_at")
        pid = (record.get("runner") or {}).get("pid")
        # Past its deadline and with no runner, the engines have stopped
        # themselves: the record is stale, not the shift live.
        if isinstance(ends, (int, float)) and now > ends + 3600 and not pid_alive(pid):
            continue
        out.append(directory.name)
    return out


def find_shift(
    shift_id: str | None, profile: str | None, *, active_only: bool = False
) -> ShiftRecord:
    """The shift a status/stop/report command means."""
    if shift_id:
        directory = shifts_dir() / shift_id
        if not (directory / shift_report.RECORD_FILE).is_file():
            raise ShiftError(f"no shift '{shift_id}' under {shifts_dir()}")
        return ShiftRecord.load(directory)
    candidates = list_shift_dirs()
    if profile:
        name = Path(profile).stem if profile.endswith(".json") else profile
        candidates = [d for d in candidates if d.name.endswith(f"-{name}")]
    if active_only:
        active = set(active_shift_ids(time.time()))
        candidates = [d for d in candidates if d.name in active]
    if not candidates:
        what = "active shift" if active_only else "shift"
        raise ShiftError(
            f"no {what} found" + (f" for profile '{profile}'" if profile else "")
        )
    return ShiftRecord.load(candidates[-1])


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


LIVE_FILE = "live.json"
STOP_FILE = "STOP"
RUNNER_LOG = "runner.log"


# ── the supervisor ──


@dataclass
class Observation:
    """What one poll learned about one loop's current session."""

    loop_id: str
    agent_id: str = ""
    session_num: int = 0
    #: running | paused | winding_down | completed | stopped | suspended |
    #: interrupted | error | missing | unreachable
    state: str = "unreachable"
    ticks: int = 0
    last_tick_at: float = 0.0
    last_tick_duration: float = 0.0
    last_error: str = ""
    pnl_total: float | None = None
    pnl_realized: float | None = None
    pnl_unrealized: float | None = None
    open_count: int | None = None
    pnl_note: str = ""
    stop: dict[str, str] | None = None

    @property
    def live(self) -> bool:
        return self.state in ("running", "paused")

    @property
    def known(self) -> bool:
        return self.state != "unreachable"


RUNNING, WAITING, FINISHED, DELIBERATE, ABANDONED, SKIPPED, PENDING = (
    "running",
    "waiting_restart",
    "finished",
    "stopped_on_purpose",
    "abandoned",
    "skipped",
    "pending",
)
TERMINAL = (FINISHED, DELIBERATE, ABANDONED, SKIPPED)


@dataclass
class LoopTracker:
    """One loop's life through a shift: running, dead and waiting, or over.

    The restart policy lives here, free of HTTP and of the clock, so the rules
    that matter most on an unattended night (what is restarted, what never is,
    when it gives up) can be tested exhaustively and read in one place.
    """

    loop_id: str
    phase: str = PENDING
    agent_id: str = ""
    session_num: int = 0
    session_boot: str | None = None
    restarts: int = 0
    restart_at: float = 0.0
    reason: str = ""
    stop_requested: bool = False
    max_restarts: int = MAX_RESTARTS
    backoff: tuple[int, ...] = RESTART_BACKOFF_SEC

    def started(self, agent_id: str, session_num: int, boot: str | None) -> None:
        self.phase, self.agent_id, self.session_num = RUNNING, agent_id, session_num
        self.session_boot, self.reason, self.restart_at = boot, "", 0.0

    def skip(self, reason: str) -> None:
        self.phase, self.reason = SKIPPED, reason

    def observe(
        self,
        obs: Observation,
        *,
        now: float,
        deadline: float,
        boot: str | None,
    ) -> list[tuple[str, str]]:
        """Fold one observation in; returns ``(level, text)`` events worth logging.

        ``level`` is ``info``, ``warn`` or ``alert``. Observations that tell us
        nothing (the server did not answer) change nothing: restarting a loop
        because the *poll* failed is how one gets two engines on one book.
        """
        if self.phase in TERMINAL or self.phase in (WAITING, PENDING) or not obs.known:
            return []
        if obs.live:
            self.phase = RUNNING
            return []
        if obs.state == "winding_down":
            return []

        evidence = (obs.stop or {}).get("kind", "none")
        why = (obs.stop or {}).get("text", "")
        if self.stop_requested:
            return self._end(FINISHED, "stopped on the runner's request", "info")
        if obs.state == "completed":
            early = now < deadline - RESTART_CUTOFF_SEC
            return self._end(
                FINISHED,
                "completed"
                + (
                    " before the deadline (max_ticks, or shift_end_action=stop)"
                    if early
                    else ""
                ),
                "warn" if early else "info",
            )
        if evidence == "shift_end" or now >= deadline - 1:
            return self._end(FINISHED, "shift ended", "info")
        if evidence == "risk_kill_switch":
            return self._end(
                DELIBERATE,
                f"RISK KILL-SWITCH: {why}. The risk engine stopped this loop on purpose; "
                "the runner will NOT restart it",
                "alert",
            )
        if evidence in ("manual_shutdown", "shutdown_other"):
            return self._end(
                DELIBERATE,
                f"emergency shutdown ({why}); stopped on purpose, the runner will NOT restart it",
                "alert",
            )
        died_with_server = (
            boot is not None
            and self.session_boot is not None
            and boot != self.session_boot
        )
        if obs.state in ("stopped", "suspended") and not (
            died_with_server or obs.state == "suspended"
        ):
            return self._end(
                DELIBERATE,
                "was stopped from outside the runner (dashboard, API or terminal); "
                "taken as the operator's decision, NOT restarted",
                "warn",
            )
        # interrupted, error, missing, or stopped/suspended because the server went down
        cause = (
            "the Condor server restarted"
            if died_with_server or obs.state == "suspended"
            else f"state '{obs.state}'"
        )
        return self._died(cause, now, deadline)

    def _end(self, phase: str, reason: str, level: str) -> list[tuple[str, str]]:
        self.phase, self.reason = phase, reason
        return [(level, reason)]

    def _died(self, cause: str, now: float, deadline: float) -> list[tuple[str, str]]:
        if now >= deadline - RESTART_CUTOFF_SEC:
            return self._end(
                FINISHED, f"died ({cause}) too close to the deadline to restart", "warn"
            )
        if self.restarts >= self.max_restarts:
            return self._end(
                ABANDONED,
                f"died ({cause}) and has used all {self.max_restarts} restarts; GIVING UP on it",
                "alert",
            )
        wait = self.backoff[min(self.restarts, len(self.backoff) - 1)]
        self.phase, self.restart_at = WAITING, now + wait
        self.reason = f"died ({cause})"
        return [
            (
                "warn",
                f"{self.reason}; restart {self.restarts + 1}/{self.max_restarts} in {wait}s",
            )
        ]

    def restart_due(self, now: float, deadline: float) -> bool:
        return (
            self.phase == WAITING
            and now >= self.restart_at
            and now < deadline - RESTART_CUTOFF_SEC
        )

    def begin_restart(self) -> int:
        """Count the attempt (a failed start spends one as surely as a good one)."""
        self.restarts += 1
        return self.restarts

    def restart_failed(
        self, why: str, now: float, deadline: float
    ) -> list[tuple[str, str]]:
        return self._died(f"restart failed: {why}", now, deadline)

    def cancel_for_deadline(self) -> list[tuple[str, str]]:
        if self.phase == WAITING:
            return self._end(
                FINISHED, "shift deadline reached while waiting to restart", "warn"
            )
        return []


class ShiftRunner:
    """Starts a shift's loops, supervises them to the deadline, and reports.

    Everything it knows about the world arrives through ``condor`` and ``hb``
    (HTTP clients) and ``clock``; with fakes for those it runs a whole shift in
    milliseconds, which is how its restart and stop rules are tested.
    """

    def __init__(
        self,
        *,
        profile: Profile,
        record: ShiftRecord,
        condor: CondorClient,
        hb: HummingbotClient | None,
        clock: Clock,
        model: str,
        budget: agent_config.Budget,
        tick_timeout: int = 600,
        poll_sec: int = POLL_SEC,
        detach: bool = False,
        echo: Callable[[str], None] | None = print,
        ledger_directory: Path | None = None,
    ) -> None:
        self.profile = profile
        self.record = record
        self.condor = condor
        self.hb = hb
        self.clock = clock
        self.model = model
        self.budget = budget
        self.poll_sec = poll_sec
        self.detach = detach
        self.echo = echo
        self.ledger_directory = ledger_directory
        self.ends_at = float(record.data["ends_at"])
        # The longest an engine can take to wind down after its deadline: it may
        # be mid-tick (one tick budget), then run the shutdown's own LLM pass
        # (another), plus the deterministic work either side.
        self.drain_grace = 2 * tick_timeout + 300
        self.trackers = {loop_id: LoopTracker(loop_id) for loop_id in record.loops}
        self.harvester = shift_report.Harvester(
            record.directory / shift_report.HARVEST_DIR
        )
        self._stop = asyncio.Event()
        self._stop_reason = ""
        self._winddown = False
        self._boot: str | None = None
        self._polls = 0
        self._unreachable_polls = 0
        self._journal_cache: dict[str, dict[str, str]] = {}

    # -- output --

    def log(self, text: str) -> None:
        line = f"{iso(self.clock.now())} {text}"
        try:
            with (self.record.directory / RUNNER_LOG).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        if self.echo is not None:
            self.echo(line)

    def _event(self, level: str, text: str, loop: str | None = None) -> None:
        scope = f"[{loop}] " if loop else ""
        tag = {"alert": "ALERT", "warn": "WARN", "info": "EVENT"}.get(level, "EVENT")
        self.log(f"{tag} {scope}{text}")
        self.record.event(level, text, now=self.clock.now(), loop=loop)
        self.record.save()

    # -- control --

    def request_stop(self, reason: str, *, winddown: bool = False) -> None:
        if not self._stop.is_set():
            self._stop_reason = reason
            self._winddown = winddown
            self._stop.set()

    async def _wait(self, seconds: float) -> None:
        """Sleep, but wake the moment a stop is asked for."""
        if self._stop.is_set() or seconds <= 0:
            return
        sleeper = asyncio.ensure_future(self.clock.sleep(seconds))
        stopper = asyncio.ensure_future(self._stop.wait())
        _done, pending = await asyncio.wait(
            {sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def _check_stop_marker(self) -> None:
        marker = self.record.directory / STOP_FILE
        if marker.exists() and not self._stop.is_set():
            try:
                winddown = marker.read_text().strip() == "winddown"
            except OSError:
                winddown = False
            self.request_stop("stop requested with `shift stop`", winddown=winddown)

    # -- starting --

    def _loop_meta(self, loop_id: str) -> dict[str, Any]:
        return self.record.loops[loop_id]

    async def start_loops(self) -> None:
        for loop_id, meta in self.record.loops.items():
            tracker = self.trackers[loop_id]
            if not meta.get("runnable", True):
                tracker.skip(meta.get("skip_reason", "skipped"))
                self._event("alert", f"SKIPPED: {meta.get('skip_reason')}", loop_id)
                continue
            if self._stop.is_set():
                break
            await self._start_one(loop_id, reason="initial start")
        self.record.save()

    async def _start_one(self, loop_id: str, *, reason: str) -> bool:
        meta = self._loop_meta(loop_id)
        tracker = self.trackers[loop_id]
        body = {
            "config": meta["start_config_raw"],
            "trading_context": meta["trading_context"],
            "chat_id": 0,
        }
        try:
            # The route waits for the first tick (up to 20 s) and the model check,
            # so a start is allowed far longer than a poll.
            result = await self.condor.post(
                f"/agents/{meta['agent']}/strategies/{meta['strategy']}/start",
                body,
                timeout=180.0,
            )
        except ShiftError as exc:
            self._event("alert", f"start failed ({reason}): {exc}", loop_id)
            (
                meta.setdefault("restarts", []).append(
                    {
                        "at": round(self.clock.now(), 3),
                        "attempt": tracker.restarts,
                        "reason": reason,
                        "ok": False,
                        "detail": str(exc)[:200],
                    }
                )
                if tracker.restarts
                else None
            )
            return False
        agent_id = str((result or {}).get("agent_id", ""))
        session_num = int((result or {}).get("session_num") or 0)
        tracker.started(agent_id, session_num, self._boot)
        meta.setdefault("sessions", []).append(
            {
                "agent_id": agent_id,
                "session_num": session_num,
                "agent": meta["agent"],
                "strategy": meta["strategy"],
                "started_at": round(self.clock.now(), 3),
                "server_boot": self._boot,
                "reason": reason,
            }
        )
        self._event("info", f"started session {agent_id} ({reason})", loop_id)
        return True

    # -- polling --

    async def _read_boot(self) -> None:
        try:
            env = await self.condor.get("/meta/env")
            boot = str((env or {}).get("boot_id") or "") or None
        except ShiftError:
            return
        if boot and boot != self._boot:
            known = self.record.data.setdefault("server_boot_ids", [])
            if self._boot is not None:
                self._event(
                    "warn",
                    f"the Condor server restarted (boot {boot[:8]}, was {self._boot[:8]})",
                )
            known.append({"boot_id": boot, "seen_at": round(self.clock.now(), 3)})
            self._boot = boot

    async def _stop_evidence(
        self, meta: dict[str, Any], session_num: int, agent_id: str
    ) -> dict[str, str]:
        cached = self._journal_cache.get(agent_id)
        if cached is not None:
            return cached
        text = ""
        try:
            payload = await self.condor.get(
                f"/agents/{meta['agent']}/strategies/{meta['strategy']}/sessions/{session_num}/journal"
            )
            text = str((payload or {}).get("content") or "")
        except ShiftError:
            session_dir = shift_report.locate_session(agent_id)
            if session_dir is not None:
                text = shift_report._read_text(session_dir / "journal.md")
        evidence = shift_report.stop_evidence(text)
        # A loop that has not logged a shutdown yet may still be about to; only
        # cache what is settled (a winddown's own lines are written before it ends).
        self._journal_cache[agent_id] = evidence
        return evidence

    async def poll(self) -> dict[str, Observation]:
        """Read every started loop's current session once."""
        self._polls += 1
        await self._read_boot()
        observations: dict[str, Observation] = {}
        groups: dict[tuple[str, str], list[str]] = {}
        for loop_id, tracker in self.trackers.items():
            if tracker.phase in (PENDING, SKIPPED) or not tracker.agent_id:
                continue
            meta = self._loop_meta(loop_id)
            groups.setdefault((meta["agent"], meta["strategy"]), []).append(loop_id)

        runs_cache: dict[str, list[dict[str, Any]] | None] = {}
        for (agent, strategy), loop_ids in groups.items():
            try:
                detail = await self.condor.get(f"/agents/{agent}/strategies/{strategy}")
            except ShiftError:
                for loop_id in loop_ids:
                    observations[loop_id] = Observation(
                        loop_id, self.trackers[loop_id].agent_id
                    )
                continue
            instances = {
                str(i.get("agent_id")): i
                for i in (detail or {}).get("instances", [])
                if isinstance(i, dict)
            }
            unavailable = str((detail or {}).get("unavailable") or "")
            for loop_id in loop_ids:
                tracker = self.trackers[loop_id]
                inst = instances.get(tracker.agent_id)
                if inst is not None:
                    observations[loop_id] = self._from_instance(
                        loop_id, inst, unavailable
                    )
                    continue
                if tracker.phase == WAITING:
                    continue
                if agent not in runs_cache:
                    try:
                        runs_cache[agent] = (
                            (await self.condor.get(f"/agents/{agent}/runs")) or {}
                        ).get("runs", [])
                    except ShiftError:
                        runs_cache[agent] = None
                observations[loop_id] = await self._from_runs(
                    loop_id, runs_cache[agent]
                )
        return observations

    def _from_instance(
        self, loop_id: str, inst: dict[str, Any], unavailable: str
    ) -> Observation:
        status = str(inst.get("status") or "")
        obs = Observation(
            loop_id,
            str(inst.get("agent_id") or ""),
            int(inst.get("session_num") or 0),
            state=status if status in ("running", "paused") else "winding_down",
            ticks=int(inst.get("tick_count") or 0),
            last_tick_at=float(inst.get("last_tick_at") or 0.0),
            last_tick_duration=float(inst.get("last_tick_duration") or 0.0),
            last_error=str(inst.get("last_error") or ""),
        )
        if unavailable:
            obs.pnl_note = f"unpriced ({unavailable})"
        else:
            obs.pnl_total = _opt_float(inst.get("total_pnl"))
            obs.pnl_realized = _opt_float(inst.get("realized_pnl"))
            obs.pnl_unrealized = _opt_float(inst.get("unrealized_pnl"))
            obs.open_count = inst.get("open_count")
        return obs

    async def _from_runs(
        self, loop_id: str, runs: list[dict[str, Any]] | None
    ) -> Observation:
        tracker = self.trackers[loop_id]
        obs = Observation(loop_id, tracker.agent_id, tracker.session_num)
        if runs is None:
            return obs  # could not ask: unreachable, which changes nothing
        row = next(
            (
                r
                for r in runs
                if r.get("kind") == "session" and r.get("agent_id") == tracker.agent_id
            ),
            None,
        )
        if row is None:
            obs.state = "missing"
            return obs
        state = str(row.get("status") or "")
        obs.ticks = int(row.get("tick_count") or 0)
        # A "running" row with no engine behind it is a run the process lost.
        obs.state = (
            "interrupted" if state in ("running", "paused", "idle", "") else state
        )
        if obs.state in ("stopped", "suspended", "completed"):
            meta = self._loop_meta(loop_id)
            obs.stop = await self._stop_evidence(
                meta, tracker.session_num, tracker.agent_id
            )
        return obs

    # -- supervising --

    def _apply(self, observations: dict[str, Observation]) -> None:
        now = self.clock.now()
        for loop_id, obs in observations.items():
            tracker = self.trackers[loop_id]
            meta = self._loop_meta(loop_id)
            sessions = meta.get("sessions") or []
            if sessions and obs.known:
                sessions[-1]["ticks_seen"] = max(
                    obs.ticks, int(sessions[-1].get("ticks_seen") or 0)
                )
                sessions[-1]["final_state"] = obs.state
            before = tracker.phase
            for level, text in tracker.observe(
                obs, now=now, deadline=self.ends_at, boot=self._boot
            ):
                self._event(level, text, loop_id)
            if tracker.phase in TERMINAL and before not in TERMINAL:
                meta["final"] = {"state": tracker.phase, "reason": tracker.reason}
                self._final_harvest(loop_id)

    def _harvest(self, final: bool = False) -> None:
        now = self.clock.now()
        for loop_id, meta in self.record.loops.items():
            for entry in meta.get("sessions") or []:
                session_dir = shift_report.locate_session(entry["agent_id"])
                if session_dir is not None:
                    self.harvester.harvest(
                        entry["agent_id"],
                        session_dir,
                        now=now,
                        min_age=0.0 if final else 3.0,
                    )

    def _final_harvest(self, loop_id: str) -> None:
        now = self.clock.now()
        for entry in self._loop_meta(loop_id).get("sessions") or []:
            session_dir = shift_report.locate_session(entry["agent_id"])
            if session_dir is not None:
                self.harvester.harvest(
                    entry["agent_id"], session_dir, now=now, min_age=0.0
                )

    async def _restart_due(self) -> None:
        now = self.clock.now()
        for loop_id, tracker in self.trackers.items():
            if not tracker.restart_due(now, self.ends_at) or self._stop.is_set():
                continue
            meta = self._loop_meta(loop_id)
            attempt = tracker.begin_restart()
            conflict = await self._live_conflict(meta, tracker)
            if conflict:
                self._event("warn", f"restart {attempt} not made: {conflict}", loop_id)
                for level, text in tracker.restart_failed(conflict, now, self.ends_at):
                    self._event(level, text, loop_id)
                continue
            self._event(
                "info",
                f"restart {attempt}/{tracker.max_restarts} after: {tracker.reason}",
                loop_id,
            )
            ok = await self._start_one(loop_id, reason=f"restart {attempt}")
            (
                meta.setdefault("restarts", []).append(
                    {
                        "at": round(now, 3),
                        "attempt": attempt,
                        "reason": tracker.reason or "died",
                        "ok": ok,
                        "detail": "" if not ok else (meta["sessions"][-1]["agent_id"]),
                    }
                )
                if ok
                else None
            )
            if not ok:
                for level, text in tracker.restart_failed(
                    "the start was refused", self.clock.now(), self.ends_at
                ):
                    self._event(level, text, loop_id)
        self.record.save()

    async def _live_conflict(self, meta: dict[str, Any], tracker: LoopTracker) -> str:
        """Why restarting would put a second engine on this book, or ``""``."""
        try:
            detail = await self.condor.get(
                f"/agents/{meta['agent']}/strategies/{meta['strategy']}"
            )
        except ShiftError as exc:
            return f"could not check for a live session first ({exc})"
        for inst in (detail or {}).get("instances", []):
            if inst.get("status") not in ("running", "paused"):
                continue
            if inst.get("agent_id") == tracker.agent_id:
                tracker.phase = RUNNING
                return f"{tracker.agent_id} is in fact still live"
            if (inst.get("trading_context") or "") == meta["trading_context"]:
                return f"{inst.get('agent_id')} already runs this context"
        return ""

    def _line(self, observations: dict[str, Observation]) -> str:
        now = self.clock.now()
        parts = []
        for loop_id, tracker in self.trackers.items():
            obs = observations.get(loop_id)
            if tracker.phase == SKIPPED:
                parts.append(f"{loop_id} SKIPPED")
            elif (
                obs is not None and obs.known and (obs.live or tracker.phase == RUNNING)
            ):
                age = (
                    f"{fmt_age(now - obs.last_tick_at)} ago"
                    if obs.last_tick_at
                    else "no tick yet"
                )
                pnl = "n/a" if obs.pnl_total is None else f"{obs.pnl_total:+.2f}"
                err = " ERR" if obs.last_error else ""
                parts.append(
                    f"{loop_id} {obs.state} t={obs.ticks} pnl={pnl} last={age}{err}"
                )
            elif obs is not None and not obs.known:
                parts.append(f"{loop_id} UNREACHABLE")
            else:
                parts.append(f"{loop_id} {tracker.phase}")
        used = shift_report.used_today(now, self.ledger_directory)
        left = max(0, self.ends_at - now)
        return (
            f"poll#{self._polls} T-{shift_report.fmt_duration(left)} | "
            + " | ".join(parts)
            + f" | req {used}/{self.budget.spendable}"
        )

    def _write_live(self, observations: dict[str, Observation]) -> None:
        now = self.clock.now()
        atomic_write_json(
            self.record.directory / LIVE_FILE,
            {
                "at": round(now, 3),
                "poll": self._polls,
                "server_boot": self._boot,
                "loops": {
                    loop_id: {
                        "phase": self.trackers[loop_id].phase,
                        "restarts": self.trackers[loop_id].restarts,
                        "reason": self.trackers[loop_id].reason,
                        **(
                            {k: v for k, v in vars(obs).items()}
                            if (obs := observations.get(loop_id))
                            else {}
                        ),
                    }
                    for loop_id in self.trackers
                },
            },
            indent=2,
        )

    async def step(self) -> dict[str, Observation]:
        """One supervision cycle: poll, apply, restart what is due, log one line."""
        self._check_stop_marker()
        observations = await self.poll()
        self._unreachable_polls = (
            self._unreachable_polls + 1
            if observations and all(not o.known for o in observations.values())
            else 0
        )
        if self._unreachable_polls in (1, 5) or (
            self._unreachable_polls and self._unreachable_polls % 30 == 0
        ):
            self._event(
                "warn",
                f"the Condor server has not answered for {self._unreachable_polls} poll(s); "
                "no loop is judged dead on that evidence",
            )
        self._apply(observations)
        if self.clock.now() >= self.ends_at:
            for tracker in self.trackers.values():
                for level, text in tracker.cancel_for_deadline():
                    self._event(level, text, tracker.loop_id)
        await self._restart_due()
        self._harvest()
        self.log(self._line(observations))
        self._write_live(observations)
        self.record.save()
        return observations

    async def supervise(self) -> str:
        """Poll to the deadline (and through the drain after it); returns why it ended."""
        while True:
            if self._stop.is_set():
                return "stop"
            observations = await self.step()
            now = self.clock.now()
            if all(t.phase in TERMINAL for t in self.trackers.values()):
                return "all loops ended"
            if now >= self.ends_at:
                live = [
                    l
                    for l, o in observations.items()
                    if o.live or o.state == "winding_down"
                ]
                if not live:
                    return "deadline"
                if now >= self.ends_at + self.drain_grace:
                    self._event(
                        "alert",
                        f"{', '.join(live)} still live {self.drain_grace}s after the deadline; stopping them",
                    )
                    return "drain timeout"
            await self._wait(
                min(self.poll_sec, max(self.ends_at - now, 1.0))
                if now < self.ends_at
                else self.poll_sec
            )

    # -- stopping --

    async def stop_loops(self, *, winddown: bool = False) -> None:
        """Ask every live loop to stop, and note that the runner asked."""
        verb = "shutdown" if winddown else "stop"
        for loop_id, tracker in self.trackers.items():
            if tracker.phase in TERMINAL or not tracker.agent_id:
                continue
            meta = self._loop_meta(loop_id)
            tracker.stop_requested = True
            try:
                await self.condor.post(
                    f"/agents/{meta['agent']}/strategies/{meta['strategy']}/{verb}",
                    None,
                    params={"agent_id": tracker.agent_id},
                    timeout=900.0 if winddown else 180.0,
                )
                self._event("info", f"asked {tracker.agent_id} to {verb}", loop_id)
            except ApiError as exc:
                if exc.status == 404:
                    self._event("info", f"{tracker.agent_id} was already gone", loop_id)
                else:
                    self._event(
                        "alert", f"could not {verb} {tracker.agent_id}: {exc}", loop_id
                    )
            except ShiftError as exc:
                self._event(
                    "alert", f"could not {verb} {tracker.agent_id}: {exc}", loop_id
                )

    async def _settle(self, seconds: float) -> None:
        """After asking loops to stop, watch until they are gone (bounded)."""
        end = self.clock.now() + seconds
        while self.clock.now() < end:
            observations = await self.poll()
            self._apply(observations)
            if all(
                t.phase in TERMINAL or not t.agent_id for t in self.trackers.values()
            ):
                return
            await self.clock.sleep(min(10.0, self.poll_sec))

    async def _capture_end_balances(self) -> list[dict[str, Any]]:
        connectors = sorted(
            {
                meta["connector"]
                for meta in self.record.loops.values()
                if meta.get("runnable", True)
            }
        )
        rows: list[dict[str, Any]] = []
        for connector in connectors:
            if connector in SIM_PORTFOLIOS:
                state = await self.condor.get(SIM_PORTFOLIOS[connector])
                sim_rows = shift_report.summarize_sim_balances(state, connector)
                if not sim_rows:
                    raise ShiftError(f"{connector} returned no usable portfolio")
                rows.extend(sim_rows)
            elif self.hb is not None:
                state = await self.hb.post(
                    "/portfolio/state",
                    {
                        "account_names": [ACCOUNT],
                        "connector_names": [connector],
                        "refresh": True,
                    },
                )
                rows.extend(shift_report.summarize_balances(state))
            else:
                raise ShiftError(f"no Hummingbot client for {connector}")
        return rows

    async def _cancel_sim_orders(self) -> None:
        if not any(
            meta.get("connector") == "kraken_sim" and meta.get("runnable", True)
            for meta in self.record.loops.values()
        ):
            return
        try:
            payload = await self.condor.get("/sim/kraken/orders")
            open_orders = (payload or {}).get("open") or []
            for order in open_orders:
                order_id = str(order.get("id") or "")
                if order_id:
                    await self.condor.request(
                        "DELETE", f"/sim/kraken/orders/{order_id}"
                    )
                    self._event("info", f"cancelled simulated order {order_id}")
            remaining = (await self.condor.get("/sim/kraken/orders") or {}).get(
                "open"
            ) or []
            if remaining:
                self._event(
                    "alert",
                    f"{len(remaining)} simulated orders still open at shift end",
                )
        except ShiftError as exc:
            self._event("alert", f"could not cancel simulated orders: {exc}")

    async def finalize(self, reason: str) -> None:
        """Stop what must be stopped, capture the end, write the report."""
        now = self.clock.now()
        detaching = self.detach and reason == "signal"
        if reason in ("stop", "signal", "drain timeout") and not detaching:
            await self.stop_loops(winddown=self._winddown)
            await self._settle(self.drain_grace if self._winddown else 120.0)
        if detaching:
            self._event(
                "warn",
                "detached: the loops keep running to their deadline; "
                f"the engines stop themselves at {iso(self.ends_at)}",
            )
        self._harvest(final=not detaching)

        data = self.record.data
        if not detaching:
            data["ended_at"] = round(self.clock.now(), 3)
            await self._cancel_sim_orders()
            try:
                data["balances_end"] = await self._capture_end_balances()
            except ShiftError as exc:
                data["balances_end_error"] = str(exc)[:200]
                self._event("warn", f"end balances not captured: {exc}")
        alerts = [e for e in data.get("events", []) if e.get("kind") == "alert"]
        data["state"] = (
            "detached"
            if detaching
            else (
                "stopped"
                if reason in ("stop", "signal") and self._stop_reason
                else "finished"
            )
        )
        data["end_reason"] = reason
        data["alerts"] = len(alerts)
        self.record.save()
        try:
            await shift_report.build_report(
                self.record.directory,
                condor=self.condor,
                hb=self.hb,
                now=self.clock.now(),
                ledger_directory=self.ledger_directory,
                final=not detaching,
            )
            self.log(
                f"report written: {self.record.directory / shift_report.REPORT_MD}"
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 - say so loudly; the record is already saved
            self._event(
                "alert", f"the report could not be built: {type(exc).__name__}: {exc}"
            )
            log.exception("shift report failed")
        self.log(
            f"shift {self.record.shift_id} {data['state']} ({reason}), {len(alerts)} alert(s), "
            f"{now and shift_report.fmt_duration(self.clock.now() - float(data['started_at']))} elapsed"
        )

    async def run(self) -> int:
        """start → supervise → finalize. Returns the process exit code."""
        reason = "error"
        try:
            await self.start_loops()
            if self._stop.is_set():
                reason = "stop"
            elif not any(t.agent_id for t in self.trackers.values()):
                self._event("alert", "no loop could be started")
                reason = "nothing started"
            else:
                self.record.data["state"] = "running"
                self.record.save()
                reason = await self.supervise()
                if self._stop.is_set():
                    reason = (
                        "signal" if self._stop_reason.startswith("signal") else "stop"
                    )
        except asyncio.CancelledError:
            reason = "signal"
            raise
        except (
            Exception
        ) as exc:  # noqa: BLE001 - never leave loops running on a runner bug
            self._event("alert", f"the runner failed: {type(exc).__name__}: {exc}")
            log.exception("shift runner failed")
            reason = "stop"
            self.request_stop(f"runner error {type(exc).__name__}")
        finally:
            await self.finalize(reason)
        alerts = int(self.record.data.get("alerts") or 0)
        if reason in ("nothing started", "error"):
            return 1
        return 2 if alerts else 0


def _opt_float(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def fmt_age(seconds: float) -> str:
    return shift_report.fmt_duration(max(0.0, seconds))


# ── commands ──


def _new_record(
    profile: Profile,
    *,
    shift_id: str,
    directory: Path,
    now: float,
    model: str,
    cfg: agent_config.LoadedConfig,
    pre: Preflight,
    boot: str | None,
) -> ShiftRecord:
    live = enabled_loops(profile)
    runnable = [l for l in live if l.id in pre.runnable]
    ends_at = now + profile.hours * 3600.0
    shares = normalised_shares(runnable)
    loops: dict[str, dict[str, Any]] = {}
    for loop in profile.loops:
        meta: dict[str, Any] = {
            "enabled": loop.enabled,
            "runnable": loop.id in pre.runnable,
            "skip_reason": pre.skipped.get(
                loop.id, "" if loop.enabled else "disabled in the profile"
            ),
            "agent": loop.agent,
            "strategy": loop.strategy,
            "connector": loop.connector,
            "trading_context": loop.trading_context,
            "capital_quote": loop.capital_quote,
            "request_share": shares.get(loop.id, 0.0),
            "frequency_sec": loop.frequency_sec,
            "sessions": [],
            "restarts": [],
        }
        if loop.id in pre.runnable:
            raw = start_config(
                loop,
                model=model,
                shift_id=shift_id,
                ends_at=ends_at,
                share=shares[loop.id],
            )
            meta["start_config_raw"] = raw
            meta["start_config"] = redact(raw)
        loops[loop.id] = meta
    data = {
        "schema": 1,
        "shift_id": shift_id,
        "profile_name": profile.name,
        "profile": redact(profile.model_dump(by_alias=True, mode="json")),
        "model": model,
        "agent_config": redact(cfg.config.to_public()),
        "started_at": round(now, 3),
        "ends_at": round(ends_at, 3),
        "hours": profile.hours,
        "capital_quote": profile.capital_quote,
        "account": ACCOUNT,
        "state": "starting",
        "runner": {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "argv": sys.argv[1:],
        },
        "balances_start": pre.balances,
        "sim_costs_start": pre.sim_costs_start,
        "server_boot_ids": [],
        "loops": loops,
        "events": [],
    }
    return ShiftRecord(directory, data)


def _public_record(data: dict[str, Any]) -> dict[str, Any]:
    """shift.json without the raw start configs: those can carry what ``redact`` cannot see."""
    out = json.loads(json.dumps(data))
    for meta in out.get("loops", {}).values():
        meta.pop("start_config_raw", None)
    return out


class _SafeRecord(ShiftRecord):
    """A record whose file never holds the raw start configs; memory keeps them to restart with."""

    def save(self) -> None:
        atomic_write_json(
            self.directory / shift_report.RECORD_FILE,
            _public_record(self.data),
            indent=2,
        )

    @classmethod
    def from_record(cls, record: ShiftRecord) -> "_SafeRecord":
        return cls(record.directory, record.data)


async def _model_error(model: str) -> Any:
    from condor.runtime.llm_client import agent_key_error

    return await agent_key_error(model)


def _echo_lines(text: str) -> None:
    print(text, flush=True)


async def cmd_validate(args: argparse.Namespace) -> int:
    try:
        profile, path = load_profile(args.profile)
    except ProfileError as exc:
        print(f"INVALID {args.profile}:\n{exc}")
        return 1
    cfg = None
    try:
        cfg = load_agent_config().config
    except ShiftError as exc:
        issues_cfg = [Issue("error", "agent-config.json", str(exc))]
    else:
        issues_cfg = []
    issues = validate_profile(profile, stem=path.stem, cfg=cfg) + issues_cfg
    for issue in issues:
        print(issue.render())
    errors = [i for i in issues if i.level == "error"]
    if errors:
        print(f"INVALID {path.name}: {len(errors)} error(s)")
        return 1
    live = enabled_loops(profile)
    model, source = resolve_model(profile, cfg) if cfg else ("", "")
    print(
        f"OK {path.name}: {len(live)} enabled loop(s), capital "
        f"{sum(l.capital_quote for l in live):,.2f} of {profile.capital_quote:,.2f}, "
        f"{profile.hours:g}h, model {model} ({source})"
    )
    return 0


async def _credentials(hb: HummingbotClient | None) -> tuple[set[str] | None, str]:
    if hb is None:
        return None, "not checked"
    try:
        listed = await hb.get(f"/accounts/{ACCOUNT}/credentials")
    except ShiftError as exc:
        return (
            None,
            f"could not ask the Hummingbot API ({exc}); runnable status unknown",
        )
    names = set(listed) if isinstance(listed, list) else set()
    return names, f"registered in {ACCOUNT}: {', '.join(sorted(names)) or 'none'}"


async def cmd_plan(
    args: argparse.Namespace,
    *,
    hb: HummingbotClient | None = None,
    now: float | None = None,
) -> int:
    try:
        profile, path = load_profile(args.profile)
        loaded = load_agent_config()
    except ShiftError as exc:
        print(f"cannot plan: {exc}")
        return 1
    issues = validate_profile(profile, stem=path.stem, cfg=loaded.config)
    if any(i.level == "error" for i in issues):
        for issue in issues:
            print(issue.render())
        print("cannot plan an invalid profile; run `validate`")
        return 1
    sim_only = all(l.connector in SIM_PORTFOLIOS for l in enabled_loops(profile))
    owns = hb is None and not sim_only
    if owns:
        try:
            _condor, hb = make_clients()
            await _condor.aclose()
        except ShiftError:
            hb = None
    if sim_only:
        credentials, note = set(), "local simulator; no exchange credential"
    else:
        try:
            credentials, note = await _credentials(hb)
        finally:
            if owns and hb is not None:
                await hb.aclose()
    plan = build_plan(
        profile,
        now=now if now is not None else time.time(),
        cfg=loaded.config,
        credentials=credentials,
        credentials_note=note,
    )
    for issue in issues:
        print(issue.render())
    print(render_plan(plan))
    return 0


async def _preflight(
    profile: Profile,
    path: Path,
    *,
    condor: CondorClient,
    hb: HummingbotClient,
    now: float,
) -> tuple[Preflight, agent_config.LoadedConfig, str, list[Issue]]:
    loaded = load_agent_config()
    issues = validate_profile(profile, stem=path.stem, cfg=loaded.config)
    model, _source = resolve_model(profile, loaded.config)
    if any(i.level == "error" for i in issues):
        return Preflight(), loaded, model, issues
    pre = await run_preflight(
        profile,
        cfg=loaded,
        model=model,
        condor=condor,
        hb=hb,
        now=now,
        shift_root=shifts_dir(),
        live_shifts=lambda: active_shift_ids(now),
        model_error=_model_error,
    )
    return pre, loaded, model, issues


async def cmd_check(args: argparse.Namespace) -> int:
    try:
        profile, path = load_profile(args.profile)
        condor, hb = make_clients()
    except ShiftError as exc:
        print(f"cannot check: {exc}")
        return 1
    try:
        pre, _loaded, _model, issues = await _preflight(
            profile, path, condor=condor, hb=hb, now=time.time()
        )
    except ShiftError as exc:
        print(f"cannot check: {exc}")
        return 1
    finally:
        await condor.aclose()
        await hb.aclose()
    for issue in issues:
        print(issue.render())
    if any(i.level == "error" for i in issues):
        print("profile invalid; run `validate`")
        return 1
    print(pre.render())
    return 0 if pre.ok else 1


async def cmd_start(args: argparse.Namespace) -> int:
    try:
        profile, path = load_profile(args.profile)
        condor, hb = make_clients()
    except ShiftError as exc:
        print(f"cannot start: {exc}")
        return 1
    clock = Clock()
    now = clock.now()
    try:
        try:
            pre, loaded, model, issues = await _preflight(
                profile, path, condor=condor, hb=hb, now=now
            )
        except ShiftError as exc:
            print(f"cannot start: {exc}")
            return 1
        for issue in issues:
            print(issue.render())
        if any(i.level == "error" for i in issues):
            print("cannot start: the profile is invalid")
            return 1
        print(pre.render())
        if not pre.ok:
            print("cannot start: preflight failed; nothing was started")
            return 1

        shift_id = args.shift_id or stamp_id(now, profile.name)
        if not SHIFT_ID.match(shift_id) or not shift_id.endswith(f"-{profile.name}"):
            print(
                f"cannot start: --shift-id must look like 20261028T1830Z-{profile.name}"
            )
            return 1
        directory = shifts_dir() / shift_id
        if (directory / shift_report.RECORD_FILE).exists():
            print(f"cannot start: {directory} already holds a shift")
            return 1
        directory.mkdir(parents=True, exist_ok=True)

        plan_cfg = loaded.config
        record = _SafeRecord.from_record(
            _new_record(
                profile,
                shift_id=shift_id,
                directory=directory,
                now=now,
                model=model,
                cfg=loaded,
                pre=pre,
                boot=None,
            )
        )
        record.save()
        runner = ShiftRunner(
            profile=profile,
            record=record,
            condor=condor,
            hb=hb,
            clock=clock,
            model=model,
            budget=plan_cfg.budget,
            tick_timeout=plan_cfg.tick_timeout_sec or 600,
            poll_sec=args.poll_sec,
            detach=args.detach,
            echo=_echo_lines,
        )
        runner.log(
            f"shift {shift_id} starting: {len(pre.runnable)} loop(s), ends {iso(runner.ends_at)}"
        )
        for loop_id, reason in pre.skipped.items():
            runner.log(f"SKIPPED {loop_id}: {reason}")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                loop.add_signal_handler(sig, runner.request_stop, f"signal {sig.name}")
            except (NotImplementedError, RuntimeError):
                pass
        return await runner.run()
    finally:
        await condor.aclose()
        await hb.aclose()


def _read_live(record: ShiftRecord) -> dict[str, Any] | None:
    return _read_json(record.directory / LIVE_FILE)


async def cmd_status(args: argparse.Namespace) -> int:
    try:
        record = find_shift(args.shift_id, args.profile)
    except ShiftError as exc:
        print(str(exc))
        return 1
    now = time.time()
    data = record.data
    live = _read_live(record)
    alive = runner_alive(record, now)
    print(render_status(record, live, alive, now))
    return 0


def render_status(
    record: ShiftRecord, live: dict[str, Any] | None, alive: bool, now: float
) -> str:
    data = record.data
    ends = float(data.get("ends_at") or 0)
    runner = data.get("runner") or {}
    out = [
        f"Shift {record.shift_id}  [{data.get('state')}]",
        f"  window  {iso(data.get('started_at'))} → {iso(ends)}   "
        + (
            f"time left {shift_report.fmt_duration(ends - now)}"
            if ends > now
            else "past its deadline"
        ),
        f"  runner  pid {runner.get('pid')} on {runner.get('host')}: "
        + (
            "alive"
            if alive
            else "NOT RUNNING"
            + (
                " (the loops carry their own deadline and stop themselves)"
                if data.get("state") in ACTIVE_STATES
                else ""
            )
        ),
        f"  model   {data.get('model')}",
    ]
    snapshot = live or {}
    if live:
        age = now - float(live.get("at") or 0)
        out.append(
            f"  poll    #{live.get('poll')} {shift_report.fmt_duration(age)} ago"
            + ("  (STALE)" if alive and age > 150 else "")
        )
    out.append("LOOPS")
    pnl_sum, priced = 0.0, 0
    for loop_id, meta in data["loops"].items():
        lv = (snapshot.get("loops") or {}).get(loop_id, {})
        if not meta.get("runnable", True):
            out.append(f"  {loop_id:16} SKIPPED — {meta.get('skip_reason')}")
            continue
        last = lv.get("last_tick_at") or 0
        pnl = lv.get("pnl_total")
        if isinstance(pnl, (int, float)):
            pnl_sum += pnl
            priced += 1
        counts = _session_counts(meta)
        out.append(
            f"  {loop_id:16} {lv.get('phase', '?'):18} state={lv.get('state', '?'):11} ticks={lv.get('ticks', '?'):>4} "
            f"last tick {fmt_age(now - last) + ' ago' if last else 'n/a':>10} "
            f"pnl={'n/a' if pnl is None else format(pnl, '+.2f')} restarts={lv.get('restarts', 0)} "
            f"refusals={counts['refusals']} tick-errors={counts['errors']}"
        )
        if lv.get("last_error"):
            out.append(f"  {'':16} last error: {str(lv['last_error'])[:140]}")
        if lv.get("reason") and lv.get("phase") not in ("running",):
            out.append(f"  {'':16} {lv['reason'][:160]}")
    out.append(
        f"  total PnL {'n/a' if not priced else format(pnl_sum, '+.2f')} ({priced} of {len([m for m in data['loops'].values() if m.get('runnable', True)])} loops priced)"
    )
    budget = (data.get("agent_config") or {}).get("budget") or {}
    spendable = (budget.get("dailyRequestCap") or 0) - (
        budget.get("reserveRequests") or 0
    )
    used = shift_report.used_today(now)
    last_hour = shift_report.requests_in_last(3600, now)
    projected = used + last_hour * max(
        0.0, (min(ends, shift_report_next_reset(now)) - now) / 3600.0
    )
    out.append(
        f"REQUESTS  {used}/{spendable} spendable today; last hour {last_hour}; projected at "
        f"{'the shift end' if ends < shift_report_next_reset(now) else '00:00 UTC'}: {projected:.0f}"
        + ("  <-- OVER" if spendable and projected > spendable else "")
    )
    alerts = [e for e in data.get("events", []) if e.get("kind") in ("alert", "warn")][
        -6:
    ]
    if alerts:
        out.append("RECENT ALERTS")
        out.extend(
            f"  {hhmm(e['at'])} {e.get('kind').upper()} {e.get('loop') or ''} {e.get('text')}"[
                :200
            ]
            for e in alerts
        )
    return "\n".join(out)


def shift_report_next_reset(now: float) -> float:
    return next_reset(now)


def _session_counts(meta: dict[str, Any]) -> dict[str, int]:
    errors = refusals = 0
    for entry in meta.get("sessions") or []:
        session_dir = shift_report.locate_session(entry["agent_id"])
        if session_dir is None:
            continue
        counts = shift_report.quick_counts(session_dir)
        errors += counts["errors"]
        refusals += counts["refusals"]
    return {"errors": errors, "refusals": refusals}


async def cmd_stop(args: argparse.Namespace) -> int:
    try:
        record = find_shift(
            args.shift_id, args.profile, active_only=args.shift_id is None
        )
    except ShiftError as exc:
        print(str(exc))
        return 1
    now = time.time()
    marker = record.directory / STOP_FILE
    marker.write_text("winddown" if args.winddown else "stop")
    print(
        f"stop requested for {record.shift_id}"
        + (" (emergency winddown)" if args.winddown else "")
    )
    if runner_alive(record, now):
        print(
            "the runner is up; waiting for it to stop the loops and write the report ..."
        )
        deadline = now + 1800
        while time.time() < deadline:
            await asyncio.sleep(5)
            fresh = ShiftRecord.load(record.directory)
            if fresh.state not in ACTIVE_STATES:
                print(
                    f"shift {fresh.state}; report: {record.directory / shift_report.REPORT_MD}"
                )
                return 0
        print("the runner has not finished after 30 minutes; read runner.log")
        return 2

    print("no runner is supervising this shift; stopping the loops from here")
    try:
        condor, hb = make_clients()
    except ShiftError as exc:
        print(f"cannot reach Condor: {exc}")
        return 1
    try:
        loaded = load_agent_config()
        safe = _SafeRecord.from_record(record)
        safe.data["state"] = "stopping"
        runner = ShiftRunner(
            profile=parse_profile(record.data["profile"]),
            record=safe,
            condor=condor,
            hb=hb,
            clock=Clock(),
            model=str(record.data.get("model")),
            budget=loaded.config.budget,
            tick_timeout=loaded.config.tick_timeout_sec or 600,
            echo=_echo_lines,
        )
        # Rebuild what the dead runner knew: which session each loop is on.
        for loop_id, meta in safe.loops.items():
            sessions = meta.get("sessions") or []
            if sessions and meta.get("runnable", True):
                runner.trackers[loop_id].started(
                    sessions[-1]["agent_id"],
                    int(sessions[-1].get("session_num") or 0),
                    None,
                )
        runner._winddown = args.winddown
        runner._stop.set()
        runner._stop_reason = "stop requested with `shift stop`"
        await runner.finalize("stop")
        print(f"report: {record.directory / shift_report.REPORT_MD}")
        return 0
    finally:
        await condor.aclose()
        await hb.aclose()


async def cmd_report(args: argparse.Namespace) -> int:
    try:
        record = find_shift(args.shift_id, args.profile)
    except ShiftError as exc:
        print(str(exc))
        return 1
    try:
        condor, hb = make_clients()
    except ShiftError as exc:
        condor = hb = None
        print(f"building from disk only: {exc}")
    try:
        final = record.state not in ACTIVE_STATES
        report = await shift_report.build_report(
            record.directory, condor=condor, hb=hb, now=time.time(), final=final
        )
    except shift_report.ReportError as exc:
        print(str(exc))
        return 1
    finally:
        if condor is not None:
            await condor.aclose()
        if hb is not None:
            await hb.aclose()
    print(f"report: {record.directory / shift_report.REPORT_MD}")
    print(f"        {record.directory / shift_report.REPORT_JSON}")
    print(
        f"        {record.directory / shift_report.DECISIONS_FILE} ({report['decisions']['count']} ticks)"
    )
    gaps = {k: v for k, v in report["coverage"].items() if not str(v).startswith("ok")}
    for name, why in gaps.items():
        print(f"  gap: {name}: {why}")
    return 0


async def cmd_schema(args: argparse.Namespace) -> int:
    text = json.dumps(json_schema(), indent=2) + "\n"
    if args.write:
        target = PROFILES_DIR / "shift-profile.schema.json"
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target}")
    else:
        print(text, end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m condor.shift",
        description="Run, watch and report a bounded trading shift on test venues.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help: str, *, profile_required: bool) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.add_argument(
            "--profile", required=profile_required, help="shifts/NAME.json (or a path)"
        )
        return p

    add("validate", "check a profile offline", profile_required=True)
    add(
        "plan",
        "print the plan and the request-budget forecast; touches nothing",
        profile_required=True,
    )
    add(
        "check", "validate, then every preflight; starts nothing", profile_required=True
    )
    start = add(
        "start",
        "preflight, start the loops, supervise to the deadline, report",
        profile_required=True,
    )
    start.add_argument(
        "--detach",
        action="store_true",
        help="on Ctrl-C/SIGTERM leave the loops running to their deadline",
    )
    start.add_argument(
        "--shift-id",
        help="use this id (<UTC stamp>-<profile>); the launcher script sets it",
    )
    start.add_argument(
        "--poll-sec",
        type=int,
        default=POLL_SEC,
        help="seconds between polls (default 60)",
    )
    for name, help in (
        ("status", "one screen on a shift"),
        ("stop", "stop a shift's loops and write the report"),
        ("report", "rebuild the report"),
    ):
        p = add(name, help, profile_required=False)
        p.add_argument(
            "--shift-id",
            help="default: the newest (active, for stop) shift of --profile",
        )
        if name == "stop":
            p.add_argument(
                "--winddown",
                action="store_true",
                help="emergency winddown (close positions per shutdown.md) instead of a plain stop",
            )
    schema = sub.add_parser("schema", help="print the profile JSON Schema")
    schema.add_argument(
        "--write", action="store_true", help="write shifts/shift-profile.schema.json"
    )
    return parser


COMMANDS = {
    "validate": cmd_validate,
    "plan": cmd_plan,
    "check": cmd_check,
    "start": cmd_start,
    "status": cmd_status,
    "stop": cmd_stop,
    "report": cmd_report,
    "schema": cmd_schema,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "start" and args.poll_sec < 5:
        print("--poll-sec must be at least 5")
        return 1
    # ConfigManager reads ./config.yml, and the dotenv the server loaded is the
    # one the model check should see: both are relative to the checkout.
    os.chdir(PROJECT_ROOT)
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    try:
        return asyncio.run(COMMANDS[args.command](args))
    except KeyboardInterrupt:
        return 130
    except ShiftError as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
