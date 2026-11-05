"""A request budget for rate-capped model providers: ledger, limiter and pacer.

OpenRouter caps a free model at 20 requests a minute and, once $10 of credit has
been bought, 1000 a day, counted from 00:00 UTC. Every model request an agent
makes is one of them, so ``maxSteps`` (how many requests one *run* may make) and
the day's allowance are two different numbers: a single 1000-step run could
spend the whole day. Three small pieces keep an unattended agent inside it:

* :class:`RequestLedger` — an append-only log of every request, one file per UTC
  day under ``data/llm_budget/``. The count is read back from the file, never
  held only in memory, so it survives a restart and is shared by every caller
  in the process (and by a second process on the same checkout). The same log
  is the record of what a shift cost.
* :class:`RequestGovernor` — decides whether the next request may go: a daily
  cap with a reserve left unspent, a sliding one-minute window, and a cool-down
  after the provider itself says "slow down". It waits for a free slot and
  raises :class:`BudgetExhausted` when none will open before the reset.
* :func:`pace_delay` — how long a loop should sleep between ticks so the
  budget lasts to the end of its shift (or to the next reset, whichever comes
  first) instead of being spent in the first hour.

The governor is a no-op until ``agent-config.json`` turns ``budget.enabled`` on
(see :mod:`condor.agent_config`), and only counts the models the cap applies to.

Counting is deliberately conservative: a request the provider refuses, and each
retry the SDK makes on its own, still counts. A reserve that errs high costs a
few ticks; one that errs low costs the rest of the day.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from condor.agent_config import Budget, get_agent_config
from condor.paths import data_dir

log = logging.getLogger(__name__)


# A paid request cannot legitimately be in flight longer than a tick may run, so a
# reservation older than this belongs to a process that died mid-request. Without a
# limit every crash would permanently shave RESERVE dollars off the month's cap.
RESERVATION_TTL_SEC = 1800

# With a dollar cap, output is limited to this many tokens (see CostTrackingModel). It is
# part of the worst-case cost of one request, so the config validator uses it too. It has
# to leave room to THINK: a reasoning model's thinking tokens count toward it, and at 4,096
# DeepSeek ran out before writing any answer ("Model token limit exceeded before any
# response was generated"), losing the desks' ticks. At $0.55/M output, 16,384 tokens add
# under a cent to the worst case.
PAID_MAX_OUTPUT_TOKENS = 16384


def unmetered_backend_error() -> str | None:
    budget = get_agent_config().budget
    if budget.enabled and (
        budget.daily_cost_cap_usd is not None or budget.monthly_cost_cap_usd is not None
    ):
        return (
            "The configured dollar budget cannot meter ACP subscription clients. "
            "Choose a direct model provider (OpenRouter, OpenAI, Anthropic, etc.) "
            "or explicitly remove dailyCostCapUsd to use this client."
        )
    return None


try:  # POSIX advisory lock; the ledger still works without it (single writer).
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

LEDGER_DIRNAME = "llm_budget"
WINDOW_SECONDS = 60.0

# A cool-down after a per-minute refusal is bounded: a provider that asks for
# more than this is treated as the daily limit, which has its own path.
MIN_COOLDOWN_SECONDS = 5.0
MAX_COOLDOWN_SECONDS = 120.0
DEFAULT_COOLDOWN_SECONDS = 20.0

# The longest a limiter sleeps before looking again. Short, so a cancelled task
# leaves promptly and a limit set by a concurrent response is noticed.
WAIT_STEP_SECONDS = 5.0


# ── time ──


def utc_day(ts: float) -> str:
    """The UTC calendar day of ``ts`` as ``YYYYMMDD`` — the unit OpenRouter resets on."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d")


def next_reset(ts: float) -> float:
    """The next 00:00 UTC strictly after ``ts``, as epoch seconds."""
    now = datetime.fromtimestamp(ts, timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return tomorrow.timestamp()


def _clock_label(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M UTC")


def is_free_model(model_id: str) -> bool:
    """Whether OpenRouter meters this model under the free-model caps."""
    model_id = (model_id or "").strip().lower()
    return model_id.endswith(":free") or model_id == "openrouter/free"


class BudgetExhausted(RuntimeError):
    """No request may go out before ``resets_at`` (epoch seconds)."""

    def __init__(self, message: str, resets_at: float) -> None:
        super().__init__(message)
        self.resets_at = resets_at


# ── ledger ──


def ledger_dir() -> Path:
    return data_dir() / LEDGER_DIRNAME


_SPEND_EVENTS = frozenset({"cost", "cost_unknown", "cost_reserve", "cost_release"})


def parse_baseline(raw: Any) -> dict[str, Any]:
    """Validate the contents of ``spend-baseline.json``; raises on anything doubtful.

    Every field is required. A baseline missing ``month`` used to pass validation and then
    contribute nothing (it only applies when its month matches), silently dropping the
    dollars it records from the cap; an unreadable baseline must pause paid requests
    instead, never loosen the guard.
    """
    if not isinstance(raw, dict):
        raise ValueError("baseline is not an object")
    for key in ("as_of", "day_used_usd", "month_used_usd"):
        value = float(raw[key])
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid baseline {key}")
    month, day = raw["month"], raw["day"]
    for label, text, width in (("month", month, 6), ("day", day, 8)):
        if not (isinstance(text, str) and len(text) == width and text.isdigit()):
            raise ValueError(f"invalid baseline {label}")
    if day[:6] != month:
        raise ValueError("baseline day is not in its month")
    return raw


def _spend_row(line: str) -> tuple[str, float, float, str] | None:
    """One ledger line as ``(event, time, usd, id)``, ``None`` if it is not spend-related,
    or ``("bad", ...)`` if it is unreadable (counted as an unpriced response)."""
    try:
        row = json.loads(line)
        event = row.get("ev")
        if event not in _SPEND_EVENTS:
            return None
        stamp = float(row.get("t", 0))
        if event == "cost_unknown":
            return (event, stamp, 0.0, "")
        if event == "cost_release":
            return (event, stamp, 0.0, str(row["id"]))
        amount = float(row["usd"])
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("invalid amount")
        return (event, stamp, amount, str(row["id"]) if event == "cost_reserve" else "")
    except (ValueError, TypeError, KeyError, AttributeError):
        return ("bad", 0.0, 0.0, "")


class RequestLedger:
    """Append-only, per-day record of model requests, read back incrementally.

    Same shape as the throttle log (:mod:`condor.agents.throttle`): one shared
    file, folded from a byte offset so a read costs only what was appended since
    the last one. Counts are *always* derived from the file — including this
    process's own lines, which are folded back in right after being written —
    so two writers cannot drift apart.
    """

    def __init__(
        self,
        directory: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._directory = directory
        self._clock = clock
        self._lock = threading.RLock()
        # Per ledger file: (bytes consumed, spend-related rows). The files are
        # append-only, so a read parses only what was added since the last one
        # instead of the whole month on every paid request.
        self._spend_cache: dict[Path, tuple[int, list[tuple]]] = {}
        self._day = ""
        self._offset = 0
        self._count = 0
        self._by_caller: dict[str, int] = {}
        self._limited_until = 0.0
        self._cost_usd = 0.0
        self._unpriced_costs = 0

    # -- paths --

    @property
    def directory(self) -> Path:
        return self._directory if self._directory is not None else ledger_dir()

    def path_for(self, day: str) -> Path:
        return self.directory / f"requests-{day}.jsonl"

    # -- reading --

    def _fold(self, now: float) -> None:
        day = utc_day(now)
        if day != self._day:
            self._day, self._offset, self._count = day, 0, 0
            self._by_caller = {}
            self._limited_until = 0.0
            self._cost_usd = 0.0
            self._unpriced_costs = 0
        path = self.path_for(day)
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self._offset:
            # Truncated or replaced: what was folded no longer describes the
            # file, so start the day over from its first byte.
            self._offset, self._count, self._by_caller = 0, 0, {}
            self._limited_until = 0.0
            self._cost_usd = 0.0
            self._unpriced_costs = 0
        if size == self._offset:
            return
        try:
            with path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except OSError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return  # a half-written line: wait for its newline
        self._offset += end + 1
        for raw in chunk[:end].split(b"\n"):
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            event = record.get("ev")
            if event == "req" and record.get("counted", True):
                self._count += 1
                caller = str(record.get("caller") or "other")
                self._by_caller[caller] = self._by_caller.get(caller, 0) + 1
            elif event == "limit" and record.get("daily"):
                try:
                    self._limited_until = max(
                        self._limited_until, float(record.get("until") or 0)
                    )
                except (TypeError, ValueError):
                    pass
            elif event == "cost":
                try:
                    amount = float(record.get("usd") or 0)
                    if not math.isfinite(amount) or amount < 0:
                        raise ValueError("Invalid cost")
                    self._cost_usd += amount
                except (TypeError, ValueError):
                    self._unpriced_costs += 1
            elif event == "cost_unknown":
                self._unpriced_costs += 1

    def sync(self, now: float | None = None) -> None:
        with self._lock:
            self._fold(self._clock() if now is None else now)

    def used_today(self, now: float | None = None) -> int:
        with self._lock:
            self._fold(self._clock() if now is None else now)
            return self._count

    def used_by(self, caller: str, now: float | None = None) -> int:
        with self._lock:
            self._fold(self._clock() if now is None else now)
            return self._by_caller.get(caller, 0)

    def by_caller(self, now: float | None = None) -> dict[str, int]:
        with self._lock:
            self._fold(self._clock() if now is None else now)
            return dict(self._by_caller)

    def limited_until(self, now: float | None = None) -> float:
        """Epoch seconds until which the provider said the day's limit is hit."""
        with self._lock:
            self._fold(self._clock() if now is None else now)
            return self._limited_until

    def cost_today(self, now: float | None = None) -> float:
        """Dollars folded in today via :meth:`record_cost` — 0.0 if never recorded."""
        with self._lock:
            self._fold(self._clock() if now is None else now)
            return self._cost_usd

    def unpriced_today(self, now: float | None = None) -> int:
        with self._lock:
            self._fold(self._clock() if now is None else now)
            return self._unpriced_costs

    def _spend_rows(self, path: Path) -> list[tuple]:
        """The file's spend-related rows, folded incrementally from the last offset."""
        size = path.stat().st_size
        offset, rows = self._spend_cache.get(path, (0, []))
        if size < offset:  # truncated or replaced: start over
            offset, rows = 0, []
        if size > offset:
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(size - offset)
            end = data.rfind(b"\n")
            if end >= 0:  # only whole lines; a half-written tail waits for its newline
                chunk = data[: end + 1]
                offset += len(chunk)
                for line in chunk.decode(errors="replace").splitlines():
                    if line.strip():
                        row = _spend_row(line)
                        if row is not None:
                            rows.append(row)
        self._spend_cache[path] = (offset, rows)
        return rows

    def spend_window(self, now: float | None = None) -> dict[str, Any]:
        """Month/day costs after a verified provider baseline, including unknowns.

        The baseline books historical provider spend without rewriting old
        request records whose adapter could not retain billed cost.
        """
        now = self._clock() if now is None else now
        day = utc_day(now)
        month = day[:6]
        baseline_path = self.directory / "spend-baseline.json"
        baseline: dict[str, Any] = {}
        if baseline_path.exists():
            try:
                baseline = parse_baseline(json.loads(baseline_path.read_text()))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise BudgetExhausted(
                    "Spend baseline is unreadable; paid requests paused",
                    next_reset(now),
                ) from exc
        cutoff = (
            float(baseline.get("as_of", 0)) if baseline.get("month") == month else 0.0
        )
        monthly = float(baseline.get("month_used_usd", 0)) if cutoff else 0.0
        daily = (
            float(baseline.get("day_used_usd", 0))
            if baseline.get("day") == day
            else 0.0
        )
        unknown = 0
        unknown_today = 0
        reservations: dict[str, tuple[float, float]] = {}
        today = self.path_for(day)
        with self._lock:
            # Sorted: a reservation in yesterday's file is released in today's, and
            # reading them the other way round would leave it held until it expires.
            for path in sorted(self.directory.glob(f"requests-{month}*.jsonl")):
                try:
                    rows = self._spend_rows(path)
                except OSError as exc:
                    raise BudgetExhausted(
                        "Cannot read monthly spend ledger; paid requests paused",
                        next_reset(now),
                    ) from exc
                is_today = path == today
                for event, stamp, amount, rid in rows:
                    if event == "bad":
                        unknown += 1
                        unknown_today += is_today
                    elif stamp <= cutoff:
                        continue
                    elif event == "cost_unknown":
                        unknown += 1
                        unknown_today += is_today
                    elif event == "cost_reserve":
                        reservations[rid] = (amount, stamp)
                    elif event == "cost_release":
                        reservations.pop(rid, None)
                    elif event == "cost":
                        monthly += amount
                        if is_today:
                            daily += amount
        held = sum(
            amount
            for amount, started in reservations.values()
            if now - started < RESERVATION_TTL_SEC
        )
        return {
            "month": month,
            "month_used_usd": monthly,
            "day_used_usd": daily,
            "reserved_usd": held,
            "unpriced": unknown,
            "unpriced_today": unknown_today,
        }

    def record_unpriced(self, caller: str) -> None:
        with self._lock:
            now = self._clock()
            self._append(now, {"t": now, "ev": "cost_unknown", "caller": caller})
            self._fold(now)

    # -- writing --

    def _append(self, now: float, record: dict[str, Any]) -> None:
        path = self.path_for(utc_day(now))
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(record, separators=(",", ":")) + "\n").encode()
        with path.open("ab") as handle:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.write(line)
                handle.flush()
            finally:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _write(self, now: float, record: dict[str, Any]) -> None:
        """Append one line, then fold it back in so counts come from the file."""
        with self._lock:
            self._fold(now)
            self._append(now, record)
            self._fold(now)

    def record_request(
        self,
        caller: str,
        model: str,
        *,
        counted: bool = True,
        free: bool = False,
        retry: bool = False,
        now: float | None = None,
    ) -> int:
        """Log one outgoing request; returns the day's counted total after it."""
        now = self._clock() if now is None else now
        with self._lock:
            self._fold(now)
            record: dict[str, Any] = {
                "t": round(now, 3),
                "ev": "req",
                "caller": caller or "other",
                "model": model,
                "free": free,
                "n": self._count + (1 if counted else 0),
            }
            if not counted:
                record["counted"] = False
            if retry:
                record["retry"] = True
            self._append(now, record)
            self._fold(now)
            return self._count

    def record_response(
        self,
        caller: str,
        status: int,
        ms: float,
        *,
        now: float | None = None,
    ) -> None:
        now = self._clock() if now is None else now
        self._write(
            now,
            {
                "t": round(now, 3),
                "ev": "res",
                "caller": caller or "other",
                "status": int(status),
                "ms": int(ms),
            },
        )

    def record_cost(
        self,
        caller: str,
        cost_usd: float,
        *,
        now: float | None = None,
    ) -> float:
        """Record one response's priced cost, retaining sub-microdollar values.

        Zero means known free usage; unknown prices use record_unpriced so an
        enabled dollar cap cannot confuse missing pricing with free requests.
        """
        cost_usd = float(cost_usd)
        if not math.isfinite(cost_usd) or cost_usd < 0:
            raise ValueError("Cost must be finite and nonnegative")
        now = self._clock() if now is None else now
        with self._lock:
            self._fold(now)
            self._append(
                now,
                {
                    "t": round(now, 3),
                    "ev": "cost",
                    "caller": caller or "other",
                    "usd": cost_usd,
                },
            )
            self._fold(now)
            return self._cost_usd

    def record_limit(
        self,
        caller: str,
        status: int,
        reset: float | None,
        reason: str,
        *,
        daily: bool,
        now: float | None = None,
    ) -> None:
        """Log a provider refusal. A daily one blocks every caller until ``reset``."""
        now = self._clock() if now is None else now
        record: dict[str, Any] = {
            "t": round(now, 3),
            "ev": "limit",
            "caller": caller or "other",
            "status": int(status),
            "reset": reset,
            "reason": reason[:200],
        }
        if daily:
            record["daily"] = True
            record["until"] = reset if reset else next_reset(now)
        self._write(now, record)


# ── governor ──


@dataclass(frozen=True)
class BudgetSnapshot:
    """What a status surface shows: the day's budget at one instant."""

    enabled: bool
    day: str
    used: int
    cap: int
    reserve: int
    remaining: int
    requests_per_minute: int
    resets_at: float
    limited_until: float
    cost_used_usd: float
    cost_cap_usd: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "day": self.day,
            "used": self.used,
            "cap": self.cap,
            "reserve": self.reserve,
            "remaining": self.remaining,
            "requests_per_minute": self.requests_per_minute,
            "resets_at": self.resets_at,
            "limited_until": self.limited_until,
            "cost_used_usd": self.cost_used_usd,
            "cost_cap_usd": self.cost_cap_usd,
        }


class RequestGovernor:
    """Admits model requests against the day's budget and the per-minute cap.

    ``config`` is a callable returning the current :class:`Budget`, read on every
    request so an edit to ``agent-config.json`` takes effect without a restart.
    """

    def __init__(
        self,
        config: Callable[[], Budget] | None = None,
        ledger: RequestLedger | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config or (lambda: get_agent_config().budget)
        self.ledger = ledger or RequestLedger(clock=clock)
        self._clock = clock
        self._sleep = sleep
        self._window: deque[float] = deque()
        self._cooldown_until = 0.0
        self._gate: asyncio.Lock | None = None
        self._cost_gate: asyncio.Lock | None = None

    # -- configuration --

    @property
    def config(self) -> Budget:
        return self._config()

    def applies(self, model_id: str, cfg: Budget | None = None) -> bool:
        """Whether a request to ``model_id`` is metered by this budget."""
        cfg = cfg or self.config
        if not cfg.enabled:
            return False
        return cfg.applies_to == "all" or is_free_model(model_id)

    def applies_to_agent_key(self, agent_key: str) -> bool:
        """The same question for a Condor agent key (``openrouter:<slug>``)."""
        prefix, _, model_id = (agent_key or "").partition(":")
        return prefix.split("@", 1)[0] == "openrouter" and self.applies(model_id)

    # -- state --

    def remaining(self, now: float | None = None) -> int:
        """Requests the agents may still spend today (the reserve is not spendable)."""
        now = self._clock() if now is None else now
        cfg = self.config
        return max(0, cfg.spendable - self.ledger.used_today(now))

    def snapshot(self, now: float | None = None) -> BudgetSnapshot:
        now = self._clock() if now is None else now
        cfg = self.config
        used = self.ledger.used_today(now)
        return BudgetSnapshot(
            enabled=cfg.enabled,
            day=utc_day(now),
            used=used,
            cap=cfg.daily_request_cap,
            reserve=cfg.reserve_requests,
            remaining=max(0, cfg.spendable - used),
            requests_per_minute=cfg.requests_per_minute,
            resets_at=next_reset(now),
            limited_until=self.ledger.limited_until(now),
            cost_used_usd=self.ledger.cost_today(now),
            cost_cap_usd=cfg.daily_cost_cap_usd,
        )

    def hold_seconds(self, minimum: int = 1, now: float | None = None) -> float:
        """How long to wait before a tick needing ``minimum`` requests can start.

        0 means go. Otherwise the seconds to the end of whatever is blocking:
        the daily reset when the budget is spent, the provider's own deadline
        when it said the day's limit was hit.
        """
        now = self._clock() if now is None else now
        cfg = self.config
        if not cfg.enabled:
            return 0.0
        snap = self.snapshot(now)
        if cfg.daily_cost_cap_usd is not None and (
            snap.cost_used_usd >= cfg.daily_cost_cap_usd
            or self.ledger.unpriced_today(now)
        ):
            return snap.resets_at - now
        if snap.limited_until > now:
            return snap.limited_until - now
        if snap.remaining < max(1, minimum):
            return snap.resets_at - now
        return 0.0

    # -- admission --

    def check_cost_budget(self, reserve_usd: float = 0.0) -> None:
        cfg = self.config
        if not cfg.enabled or (
            cfg.daily_cost_cap_usd is None and cfg.monthly_cost_cap_usd is None
        ):
            return
        now = self._clock()
        window = (
            self.ledger.spend_window(now)
            if cfg.monthly_cost_cap_usd is not None
            else None
        )
        if window is None and self.ledger.unpriced_today(now):
            raise BudgetExhausted(
                "Dollar budget paused: a response could not be priced. "
                "Check provider pricing before continuing; resets at "
                + _clock_label(next_reset(now)),
                next_reset(now),
            )
        if window is None:
            spent = self.ledger.cost_today(now)
            if cfg.daily_cost_cap_usd is not None and spent >= cfg.daily_cost_cap_usd:
                raise BudgetExhausted(
                    f"daily cost budget spent (${spent:.2f} of ${cfg.daily_cost_cap_usd:.2f}); "
                    f"resets at {_clock_label(next_reset(now))}",
                    next_reset(now),
                )
            return
        # Monthly mode. The paid model is pinned with price ceilings, so one request can
        # never cost more than the reserve. A response whose price is unknown (a timeout,
        # a cancelled stream, a missing usage block) is therefore booked at the reserve
        # rather than halting every paid request until the month rolls over.
        month_used = (
            window["month_used_usd"] + window["unpriced"] * cfg.request_cost_reserve_usd
        )
        day_used = (
            window["day_used_usd"]
            + window["unpriced_today"] * cfg.request_cost_reserve_usd
        )
        held = window["reserved_usd"]
        if month_used + held + reserve_usd >= cfg.monthly_cost_cap_usd:
            dt = datetime.fromtimestamp(now, timezone.utc)
            reset = datetime(
                dt.year + (dt.month == 12), dt.month % 12 + 1, 1, tzinfo=timezone.utc
            ).timestamp()
            raise BudgetExhausted(
                f"monthly cost budget reached: ${month_used:.4f} used of "
                f"${cfg.monthly_cost_cap_usd:.2f}; ${reserve_usd:.2f} required for the next request",
                reset,
            )
        if (
            cfg.daily_cost_cap_usd is not None
            and day_used + held + reserve_usd >= cfg.daily_cost_cap_usd
        ):
            raise BudgetExhausted(
                f"daily cost budget spent (${day_used:.2f} of ${cfg.daily_cost_cap_usd:.2f}); "
                f"resets at {_clock_label(next_reset(now))}",
                next_reset(now),
            )

    @asynccontextmanager
    async def cost_slot(self, *, model_key: str = ""):
        """One priced request in flight across processes when dollars are capped.

        Actual cost is only known after a response. Serializing admission limits
        overshoot to that response, instead of a whole run or concurrent runs.
        """
        cfg = self.config
        free = model_key.startswith("openrouter:") and is_free_model(model_key)
        if (
            not cfg.enabled
            or free
            or (cfg.daily_cost_cap_usd is None and cfg.monthly_cost_cap_usd is None)
        ):
            yield
            return
        if (
            cfg.monthly_cost_cap_usd is not None
            and model_key not in cfg.allowed_paid_models
        ):
            raise BudgetExhausted(
                "This paid model is outside the monthly budget's approved pricing envelope",
                next_reset(self._clock()),
            )
        if self._cost_gate is None:
            self._cost_gate = asyncio.Lock()
        async with self._cost_gate:
            fd = None
            reservation = None
            try:
                if fcntl is not None:
                    self.ledger.directory.mkdir(parents=True, exist_ok=True)
                    fd = os.open(
                        self.ledger.directory / "cost-admission.lock",
                        os.O_CREAT | os.O_RDWR,
                        0o600,
                    )
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            await asyncio.sleep(0.05)
                self.check_cost_budget(
                    cfg.request_cost_reserve_usd
                    if cfg.monthly_cost_cap_usd is not None
                    else 0.0
                )
                if cfg.monthly_cost_cap_usd is not None:
                    reservation = uuid.uuid4().hex
                    now = self._clock()
                    self.ledger._append(
                        now,
                        {
                            "t": now,
                            "ev": "cost_reserve",
                            "id": reservation,
                            "usd": cfg.request_cost_reserve_usd,
                        },
                    )
                yield
            finally:
                if reservation is not None:
                    now = self._clock()
                    self.ledger._append(
                        now, {"t": now, "ev": "cost_release", "id": reservation}
                    )
                if fd is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    def _lock(self) -> asyncio.Lock:
        if self._gate is None:
            self._gate = asyncio.Lock()
        return self._gate

    def _rpm_wait(self, now: float, rpm: int) -> float:
        while self._window and now - self._window[0] >= WINDOW_SECONDS:
            self._window.popleft()
        if len(self._window) < rpm:
            return 0.0
        return self._window[0] + WINDOW_SECONDS - now

    async def acquire(self, caller: str, model_id: str) -> None:
        """Wait for a slot and count the request, or raise :class:`BudgetExhausted`.

        Held under one lock so concurrent callers are admitted in order and the
        count each one checks includes everyone ahead of it.
        """
        cfg = self.config
        if not self.applies(model_id, cfg):
            return
        async with self._lock():
            while True:
                now = self._clock()
                self.ledger.sync(now)
                until = self.ledger.limited_until(now)
                if until > now:
                    raise BudgetExhausted(
                        "OpenRouter says the daily free-model request limit is "
                        f"reached; it resets at {_clock_label(until)}",
                        until,
                    )
                used = self.ledger.used_today(now)
                if used >= cfg.spendable:
                    raise BudgetExhausted(
                        f"daily request budget spent ({used} of "
                        f"{cfg.daily_request_cap} used, {cfg.reserve_requests} held "
                        f"back); resets at {_clock_label(next_reset(now))}",
                        next_reset(now),
                    )
                if not is_free_model(model_id):
                    self.check_cost_budget()
                wait = max(
                    self._cooldown_until - now,
                    self._rpm_wait(now, cfg.requests_per_minute),
                )
                if wait <= 0:
                    break
                await self._sleep(min(wait, WAIT_STEP_SECONDS))
            self.ledger.record_request(
                caller, model_id, free=is_free_model(model_id), now=now
            )
            self._window.append(now)

    def note_rate_limited(
        self,
        caller: str,
        status: int,
        reset: float | None,
        reason: str,
        *,
        daily: bool,
    ) -> None:
        """The provider refused a request: remember it so nobody hammers it."""
        now = self._clock()
        self.ledger.record_limit(caller, status, reset, reason, daily=daily, now=now)
        if not daily:
            hold = (reset - now) if reset else DEFAULT_COOLDOWN_SECONDS
            hold = min(max(hold, MIN_COOLDOWN_SECONDS), MAX_COOLDOWN_SECONDS)
            self._cooldown_until = max(self._cooldown_until, now + hold)

    # -- hooks --

    def gate_for(
        self, caller: str, model_id: str
    ) -> Callable[[], Awaitable[None]] | None:
        """The coroutine a model awaits before each request, or ``None`` if the
        budget does not apply to ``model_id`` (nothing to gate, nothing to count)."""
        if not self.applies(model_id):
            return None

        async def gate() -> None:
            await self.acquire(caller, model_id)

        return gate

    def event_hooks(self, caller: str, model_id: str) -> dict[str, list]:
        """httpx event hooks that finish the accounting the gate starts.

        The gate counts the first attempt of every model request. The hooks
        record how it ended, count the SDK's own retries (each is another
        request to the provider), and turn a 429 into a cool-down or, when the
        provider says the *day's* limit is hit, a block for every caller.
        """

        async def on_request(request: Any) -> None:
            if not _is_completion(request):
                return
            request.extensions["condor_t0"] = self._clock()
            try:
                retries = int(request.headers.get("x-stainless-retry-count", "0") or 0)
            except ValueError:
                retries = 0
            if retries > 0:
                self.ledger.record_request(
                    caller, model_id, free=is_free_model(model_id), retry=True
                )

        async def on_response(response: Any) -> None:
            request = response.request
            if not _is_completion(request):
                return
            started = request.extensions.get("condor_t0")
            elapsed_ms = (self._clock() - started) * 1000 if started else 0
            status = response.status_code
            self.ledger.record_response(caller, status, elapsed_ms)
            if status != 429:
                return
            try:
                await response.aread()
                text = response.text
            except (
                Exception
            ):  # noqa: BLE001 - a body we cannot read is not a reason to fail
                text = ""
            lowered = text.lower()
            daily = "per-day" in lowered or "per day" in lowered
            self.note_rate_limited(
                caller,
                status,
                _reset_epoch(response.headers, self._clock()),
                text.strip().replace("\n", " "),
                daily=daily,
            )

        return {"request": [on_request], "response": [on_response]}


def _is_completion(request: Any) -> bool:
    return request.method == "POST" and str(request.url.path).endswith(
        "/chat/completions"
    )


def _reset_epoch(headers: Any, now: float) -> float | None:
    """When the provider says the limit lifts, as epoch seconds, or ``None``.

    ``X-RateLimit-Reset`` is an epoch in milliseconds on OpenRouter; ``Retry-After``
    is seconds from now. Anything else (or garbage) is no answer.
    """
    raw = headers.get("x-ratelimit-reset")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 1e11:
            return value / 1000.0
        if value > 1e9:
            return value
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            return now + float(retry_after)
        except ValueError:
            return None
    return None


# ── pacing ──


def pace_delay(
    *,
    frequency_sec: float,
    tick_seconds: float,
    requests_per_tick: float,
    remaining_requests: float,
    share: float,
    now: float,
    shift_end: float | None,
    reset_at: float,
) -> float:
    """Seconds to sleep before the next tick so the budget lasts its horizon.

    The horizon is whichever comes first of the shift's end and the next daily
    reset: past the reset a fresh day's budget exists, and it is paced on its
    own when the loop gets there. ``share`` is this loop's portion of what is
    left. The answer is never shorter than ``frequency_sec`` — pacing only ever
    slows a loop, it never speeds one up — and when the loop cannot afford even
    one more tick it waits out the horizon (capped by the caller, which keeps
    re-asking).
    """
    horizon_end = reset_at if shift_end is None else min(shift_end, reset_at)
    seconds_left = max(horizon_end - now, 1.0)
    per_tick = max(float(requests_per_tick), 1.0)
    affordable = (
        max(float(remaining_requests), 0.0) * min(max(share, 0.0), 1.0) / per_tick
    )
    if affordable < 1.0:
        return max(float(frequency_sec), seconds_left)
    interval = seconds_left / affordable
    return max(float(frequency_sec), interval - max(float(tick_seconds), 0.0))


# ── process-wide governor ──

_governor: RequestGovernor | None = None
_governor_lock = threading.Lock()


def get_governor() -> RequestGovernor:
    """The shared governor. One per process, so every caller meets the same limit."""
    global _governor
    with _governor_lock:
        if _governor is None:
            _governor = RequestGovernor()
        return _governor


def reset_governor() -> None:
    """Drop the shared governor (tests; a caller that changed ``CONDOR_DATA_DIR``)."""
    global _governor
    with _governor_lock:
        _governor = None
