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
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from condor.agent_config import Budget, get_agent_config
from condor.paths import data_dir

log = logging.getLogger(__name__)

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
        self._day = ""
        self._offset = 0
        self._count = 0
        self._by_caller: dict[str, int] = {}
        self._limited_until = 0.0

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
        if snap.limited_until > now:
            return snap.limited_until - now
        if snap.remaining < max(1, minimum):
            return snap.resets_at - now
        return 0.0

    # -- admission --

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
