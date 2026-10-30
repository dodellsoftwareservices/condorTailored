"""Reading a shift back: the request ledger, the session files, and the report.

A shift leaves its evidence in five places and none of them was written *for* a
report. The request ledger (``data/llm_budget/``) counts what the model cost.
Each session's ``journal.md``, ``snapshots/``, ``actions.jsonl`` and
``closeout.json`` say what a loop did and how it ended. The Condor server's
session route prices it. The Hummingbot API holds the balances. This module
reads all of them, keeps each one's limits visible, and renders the result as
``report.md`` + ``report.json`` + ``decisions.jsonl`` beside ``shift.json``.

Three rules shape it, the same three the rest of the codebase holds itself to:

**A missing source is said, never zero-filled.** A PnL that could not be priced
is ``None`` and reads ``n/a (why)``; a snapshot the engine had already trimmed is
a tick with no tool-call detail, not a tick with no tool calls. ``coverage`` in
``report.json`` lists every source with the reason it was or was not read.

**The existing readers, not a second accounting.** PnL, volume and deployments
come from ``GET .../sessions/{N}/executors`` (``condor.agents.performance`` and
``condor.agents.attribution`` behind the dashboard's session page), the stop
record from ``condor.agents.closeout``, the status from
``condor.runtime.registry_file``. What is parsed here is only what has no reader:
the snapshot markdown and the request ledger.

**Retention is a fact about the evidence.** The engine keeps 100 snapshots a
session and trims the oldest, so an 8-hour run at a 2-minute cadence loses the
tool-call detail of its first ticks before anyone can ask for it. The runner
therefore *harvests* each new snapshot into ``harvest/`` as the shift goes
(:class:`Harvester`); the report merges what was harvested with what is still on
disk and says how many ticks have no detail.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from condor.agents.actions import (
    ACTIONS_ARCHIVE_FILENAME,
    ACTIONS_FILENAME,
    DEPLOY_VERB,
)
from condor.agents.closeout import read_closeout
from condor.agents.journal import (
    ARCHIVE_NAME,
    JournalManager,
    count_journal_ticks,
    iter_session_dirs,
)
from condor.agents.sessions_index import (
    find_session_dir,
    parse_agent_id,
    session_started_at,
    strategy_dir_for_run_key,
)
from condor.fsutil import atomic_write_json, atomic_write_text
from condor.llm.budget import ledger_dir, utc_day
from condor.runtime.danger import CREATE_EXECUTOR_TOOLS
from condor.runtime.registry_file import LIVE_STATES, read_status

log = logging.getLogger(__name__)

RECORD_FILE = "shift.json"
REPORT_MD = "report.md"
REPORT_JSON = "report.json"
DECISIONS_FILE = "decisions.jsonl"
HARVEST_DIR = "harvest"

#: Sessions with fewer ticks than this say too little about what a tick costs.
MIN_MEASURED_TICKS = 3

#: How many timeline rows ``report.md`` prints before pointing at
#: ``decisions.jsonl`` for the rest. The file always has every row.
TIMELINE_MD_ROWS = 160

#: The one-line action and the stored response are bounded so a harvest of a few
#: hundred ticks stays a few hundred KB, not a copy of every model response.
ACTION_CHARS = 180
RESPONSE_CHARS = 700
OUTPUT_CHARS = 240

#: Journal ``Decisions`` entries that are the risk gate or the venue saying no.
REFUSAL_ACTIONS = frozenset(
    {"risk_blocked", "tick_blocked", "bot_ownership_blocked", "venue_throttled"}
)

#: Errors the journal records that are not a tick failing: the boot pass marking a
#: dead process's run, and the executor-mode / bot-mode mismatch notice.
_NOT_A_TICK_ERROR = ("Interrupted", "Config/behaviour mismatch")


class Api(Protocol):
    """What the report needs of an HTTP client: paths relative to ``/api/v1``."""

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any: ...

    async def post(self, path: str, body: Any = None) -> Any: ...


class ReportError(RuntimeError):
    """The report cannot be built at all (no shift record to build it from)."""


# ── time ──


def utc_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_minute(stamp: str) -> float | None:
    """``"2026-10-28 14:03"`` (a journal's minute stamp, UTC) as epoch seconds."""
    m = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})", stamp or "")
    if not m:
        return None
    try:
        parsed = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


# ── the request ledger ──

_LEDGER_NAME = re.compile(r"^requests-(\d{8})\.jsonl$")


@dataclass
class LedgerView:
    """The ledger events of a time window, as the budget module wrote them."""

    reqs: list[dict[str, Any]] = field(default_factory=list)
    ress: list[dict[str, Any]] = field(default_factory=list)
    limits: list[dict[str, Any]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    #: Lines that were not JSON objects. A half-written last line is normal; a
    #: lot of them is a ledger worth distrusting, so the report says how many.
    bad_lines: int = 0

    @staticmethod
    def counted(record: Mapping[str, Any]) -> bool:
        """Whether the governor counted this ``req`` (``counted`` defaults true)."""
        return bool(record.get("counted", True))


def read_ledger(
    since: float, until: float, directory: Path | None = None
) -> LedgerView:
    """Every ledger event with ``since <= t <= until``. Never raises.

    The files are small (a day is at most the cap plus its retries), so they are
    read whole. The count is derived from the lines, never from the ``n`` field:
    ``n`` is what the writer believed at the time, the lines are what is there.
    """
    view = LedgerView()
    directory = directory if directory is not None else ledger_dir()
    first, last = utc_day(since), utc_day(until)
    try:
        paths = sorted(directory.glob("requests-*.jsonl"))
    except OSError:
        return view
    for path in paths:
        m = _LEDGER_NAME.match(path.name)
        if not m or not (first <= m.group(1) <= last):
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        view.files.append(path.name)
        for line in raw.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                view.bad_lines += 1
                continue
            if not isinstance(record, dict):
                view.bad_lines += 1
                continue
            stamp = record.get("t")
            if not isinstance(stamp, (int, float)) or not (since <= stamp <= until):
                continue
            event = record.get("ev")
            if event == "req":
                view.reqs.append(record)
            elif event == "res":
                view.ress.append(record)
            elif event == "limit":
                view.limits.append(record)
    return view


def used_today(now: float, directory: Path | None = None) -> int:
    """Counted requests so far on ``now``'s UTC day: what the governor enforces."""
    day_start = datetime.strptime(utc_day(now), "%Y%m%d").replace(tzinfo=timezone.utc)
    view = read_ledger(day_start.timestamp(), now, directory)
    return sum(1 for r in view.reqs if view.counted(r))


def requests_in_last(seconds: float, now: float, directory: Path | None = None) -> int:
    view = read_ledger(now - seconds, now, directory)
    return sum(1 for r in view.reqs if view.counted(r))


def requests_by_caller(
    now: float, days: int = 14, directory: Path | None = None
) -> dict[str, int]:
    """Counted requests per caller over the last ``days`` days of ledger."""
    view = read_ledger(now - days * 86400, now, directory)
    out: Counter[str] = Counter()
    for r in view.reqs:
        if view.counted(r):
            out[str(r.get("caller") or "other")] += 1
    return dict(out)


def measure_requests_per_tick(
    run_key: str, strategy_dir: Path, by_caller: Mapping[str, int]
) -> tuple[float | None, int, int]:
    """``(requests per tick, requests, ticks)`` this strategy has measured, or ``None``.

    Requests are the ledger's, ticks are the journals': every session of the
    strategy that spent anything, so a blocked tick (which spends nothing) pulls
    the mean down exactly as it does in life. ``None`` below
    :data:`MIN_MEASURED_TICKS`: two ticks say more about the first two ticks than
    about the loop.
    """
    requests = ticks = 0
    for number, session_dir in iter_session_dirs(strategy_dir):
        spent = by_caller.get(f"{run_key}_{number}", 0)
        if spent <= 0:
            continue
        n = count_journal_ticks(session_dir / "journal.md")
        if n <= 0:
            continue
        requests += spent
        ticks += n
    if ticks < MIN_MEASURED_TICKS:
        return None, requests, ticks
    return requests / ticks, requests, ticks


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; ``None`` for no data."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


# ── journal and snapshot parsing ──

_TICK_LINE = re.compile(
    r"^- tick#(\d+) \| (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \| actions=(\d+) \| ?(.*)$"
)
_DECISION_LINE = re.compile(r"^- \*\*(#\d+|error)\*\* \((\d{2}:\d{2})\) (.*)$")
_SNAPSHOT_HEAD = re.compile(r"^# (?:Snapshot|Tick) #(\d+) — (.+)$")
_SNAPSHOT_FILE = re.compile(r"^snapshot_(\d+)\.md$")
_TOOL_HEAD = re.compile(r"^### (\d+)\. (.+) \(([^()]*)\)\s*$")
_DETAILS = re.compile(r"<details>.*?</details>", re.DOTALL)
_DRAWDOWN = re.compile(r"Drawdown:\s*([0-9]+(?:\.[0-9]+)?)%")
_DURATION = re.compile(r"Duration:\s*([0-9]+(?:\.[0-9]+)?)s")
_ACTION_LABEL = re.compile(
    r"^[\W_]*(?:decision|action|last action|summary)\b[^:\n]{0,20}:\s*(.+)$",
    re.IGNORECASE,
)


def section(text: str, name: str) -> str:
    """Body of ``## name`` in a journal (same rule as the journal's own reader)."""
    m = re.search(rf"^## {re.escape(name)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def parse_ticks(text: str) -> list[dict[str, Any]]:
    """The ``- tick#N | stamp | actions=K | summary`` lines anywhere in ``text``."""
    rows = []
    for line in text.splitlines():
        m = _TICK_LINE.match(line)
        if m:
            rows.append(
                {
                    "tick": int(m.group(1)),
                    "stamp": m.group(2),
                    "actions": int(m.group(3)),
                    "summary": m.group(4).strip(),
                }
            )
    return rows


def parse_decisions(text: str) -> list[dict[str, Any]]:
    """The ``Decisions`` lines anywhere in ``text``: tick, minute, action, reason."""
    rows = []
    for line in text.splitlines():
        m = _DECISION_LINE.match(line)
        if not m:
            continue
        label, hm, rest = m.groups()
        if label == "error":
            rows.append(
                {"tick": None, "hm": hm, "action": "error", "text": rest.strip()}
            )
            continue
        action, _sep, why = rest.partition(" -- ")
        rows.append(
            {
                "tick": int(label[1:]),
                "hm": hm,
                "action": action.strip(),
                "text": why.strip(),
            }
        )
    return rows


def one_line(text: str, limit: int = ACTION_CHARS) -> str:
    """The line that says what the agent decided.

    A playbook's reply has no fixed shape, so this takes a ``Decision:`` /
    ``Action:`` / ``Summary:`` line when the model wrote one and otherwise the
    first line with words in it. Never invented: it is always a cut of the reply.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    chosen = ""
    for ln in lines:
        m = _ACTION_LABEL.match(ln)
        if m:
            chosen = m.group(1)
            break
    if not chosen:
        for ln in lines:
            plain = re.sub(r"^[#>*\-\s`]+", "", ln).strip()
            if re.search(r"[A-Za-z0-9]", plain):
                chosen = plain
                break
    chosen = re.sub(r"\s+", " ", chosen.replace("**", "")).strip()
    return chosen if len(chosen) <= limit else chosen[: limit - 1].rstrip() + "…"


def parse_snapshot(text: str) -> dict[str, Any]:
    """One ``snapshot_N.md`` as the fields a report needs.

    The system prompt is cut out first (it is a few dozen KB of markdown with
    ``##`` headings of its own, and nothing here wants it), and headings inside a
    code fence are not headings: a tool's output is free text.
    """
    text = _DETAILS.sub("", text)
    lines = text.splitlines()
    head = _SNAPSHOT_HEAD.match(lines[0]) if lines else None
    sections: dict[str, list[str]] = {}
    current = "_head"
    in_fence = False
    for line in lines[1:]:
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("## "):
            current = re.sub(r"\s*\(\d+\)$", "", line[3:].strip())
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)

    response = "\n".join(sections.get("Agent Response", [])).strip()
    if response == "No response.":
        response = ""
    risk = "\n".join(sections.get("Risk State", []))
    dd = _DRAWDOWN.search(risk)
    check = re.search(r"Risk Check:\s*(.*)", risk)
    stats = "\n".join(sections.get("Stats", []))
    dur = _DURATION.search(stats)
    return {
        "tick": int(head.group(1)) if head else None,
        "stamp": head.group(2).strip() if head else "",
        "response": response,
        "tools": _parse_tool_calls(sections.get("Tool Calls", [])),
        "drawdown_pct": float(dd.group(1)) if dd else None,
        "risk_check": check.group(1).strip() if check else "",
        "duration_s": float(dur.group(1)) if dur else None,
        "timed_out": response.rstrip().endswith("(timed out)"),
    }


def _parse_tool_calls(lines: list[str]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    marker = ""
    in_fence = False
    buffer: list[str] = []
    for line in lines:
        if in_fence:
            if line.startswith("```"):
                in_fence = False
                body = "\n".join(buffer)
                if current is not None and marker == "input":
                    try:
                        current["input"] = json.loads(body)
                    except ValueError:
                        current["input"] = None
                elif current is not None and marker == "output":
                    current["output"] = body[:OUTPUT_CHARS]
            else:
                buffer.append(line)
            continue
        m = _TOOL_HEAD.match(line)
        if m:
            current = {
                "name": m.group(2).strip(),
                "status": m.group(3).strip(),
                "input": None,
                "output": "",
            }
            calls.append(current)
        elif line.startswith("**Input:**"):
            marker = "input"
        elif line.startswith("**Output:**"):
            marker = "output"
        elif line.startswith("```") and current is not None:
            in_fence, buffer = True, []
    for call in calls:
        call["verb"] = _verb(call["name"], call.get("input"))
    return calls


def _verb(name: str, args: Any) -> str:
    """``tool:action`` for a dispatch tool, the same key the action log uses."""
    action = args.get("action") if isinstance(args, dict) else None
    return f"{name}:{action}" if isinstance(action, str) and action else name


def stop_evidence(journal_text: str) -> dict[str, str]:
    """Why a stopped session stopped, read from the journal's last shutdown.

    The status file records *that* a run stopped, not why, and three different
    things end a run through the emergency winddown: the risk engine's hard
    drawdown limit, the operator's emergency stop, and the shift deadline. The
    journal's ``shutdown_start`` line carries the reason, and it is the only
    place that does. ``none`` means no winddown ran: a plain stop.
    """
    return stop_from_decisions(parse_decisions(journal_text))


def stop_from_decisions(decisions: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """:func:`stop_evidence` over decision rows already parsed."""
    last = None
    for row in decisions:
        if row["action"] == "shutdown_start":
            last = row
    if last is None:
        return {"kind": "none", "text": ""}
    reason = last["text"]
    lowered = reason.lower()
    if "shift ended" in lowered:
        kind = "shift_end"
    elif "shutdown limit" in lowered or lowered.startswith("drawdown"):
        kind = "risk_kill_switch"
    elif "manual emergency stop" in lowered:
        kind = "manual_shutdown"
    else:
        kind = "shutdown_other"
    return {"kind": kind, "text": reason}


# ── a session's files ──


def locate_session(agent_id: str) -> Path | None:
    """The session directory an ``agent_id`` names, or ``None`` when it is not on disk."""
    parsed = parse_agent_id(agent_id)
    if parsed is None or parsed[2] != "session":
        return None
    run_key, number, _kind = parsed
    strategy_dir = strategy_dir_for_run_key(run_key)
    if strategy_dir is None:
        return None
    return find_session_dir(strategy_dir, number)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in _read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def read_all_actions(session_dir: Path) -> list[dict[str, Any]]:
    """Every mutating call the session logged: the archive's rows, then the live file's."""
    return _read_jsonl(session_dir / ACTIONS_ARCHIVE_FILENAME) + _read_jsonl(
        session_dir / ACTIONS_FILENAME
    )


def snapshot_files(session_dir: Path) -> dict[int, Path]:
    found: dict[int, Path] = {}
    for dirname in ("snapshots", "runs"):
        try:
            children = list((session_dir / dirname).iterdir())
        except OSError:
            continue
        for child in children:
            m = _SNAPSHOT_FILE.match(child.name) or re.match(
                r"^run_(\d+)\.md$", child.name
            )
            if m:
                found.setdefault(int(m.group(1)), child)
    return found


class Harvester:
    """Copies each new tick's detail out of the session before the engine trims it.

    ``snapshots/`` holds the last 100 ticks of a session and nothing older. A
    shift's evidence for *what the model called* is exactly that directory, so
    the runner calls :meth:`harvest` on every poll and what it keeps is compact:
    the reply (cut), the tool calls with their outcomes and the risk reading, not
    the system prompt.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._done: dict[str, set[int]] = {}

    def _path(self, agent_id: str) -> Path:
        return self.directory / f"{agent_id}.jsonl"

    def harvested(self, agent_id: str) -> dict[int, dict[str, Any]]:
        return {
            int(row["tick"]): row
            for row in _read_jsonl(self._path(agent_id))
            if isinstance(row.get("tick"), int)
        }

    def harvest(
        self,
        agent_id: str,
        session_dir: Path,
        *,
        now: float,
        min_age: float = 3.0,
    ) -> int:
        """Append every snapshot not yet harvested; returns how many.

        ``min_age``: ``save_full_snapshot`` writes in place, so a file younger
        than this may be half written, and a tick harvested from half a file
        would stay half a tick for good. The final harvest, once the engines are
        down and nothing is writing, passes 0.
        """
        done = self._done.get(agent_id)
        if done is None:
            done = self._done[agent_id] = set(self.harvested(agent_id))
        fresh = 0
        for tick, path in sorted(snapshot_files(session_dir).items()):
            if tick in done:
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age < min_age:
                continue
            parsed = parse_snapshot(_read_text(path))
            if parsed["tick"] is None:
                parsed["tick"] = tick
            row = {
                "tick": tick,
                "stamp": parsed["stamp"],
                "response": parsed["response"][:RESPONSE_CHARS],
                "action": one_line(parsed["response"]),
                "tools": [
                    {k: c[k] for k in ("name", "status", "verb")}
                    | {"output": c["output"][:OUTPUT_CHARS]}
                    for c in parsed["tools"]
                ],
                "drawdown_pct": parsed["drawdown_pct"],
                "risk_check": parsed["risk_check"],
                "duration_s": parsed["duration_s"],
                "timed_out": parsed["timed_out"],
            }
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._path(agent_id).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            done.add(tick)
            fresh += 1
        return fresh


# ── one session, digested ──


def _pnl_stats(
    series: list[dict[str, Any]],
) -> tuple[float | None, float | None, int]:
    """``(last pnl, max drawdown in quote, points)`` of a PnL series.

    The drawdown is peak-to-trough from the session's start, where the PnL is 0:
    a session that lost money from its first reading has drawn down even though it
    never made a peak. One point per tick, so it can miss a swing between two
    ticks; the report labels it that way.
    """
    pnls = [float(p["pnl"]) for p in series if isinstance(p.get("pnl"), (int, float))]
    if not pnls:
        return None, None, 0
    peak, worst = 0.0, 0.0
    for value in pnls:
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return pnls[-1], worst, len(pnls)


def digest_session(
    agent_id: str,
    session_dir: Path | None,
    *,
    harvested: Mapping[int, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Everything a session's files say, with what could not be read named.

    Pure reads: a ``JournalManager`` creates a journal that is not there, so one
    is built only over a ``journal.md`` that exists.
    """
    out: dict[str, Any] = {
        "agent_id": agent_id,
        "found": session_dir is not None and session_dir.is_dir(),
        "session_dir": str(session_dir) if session_dir else "",
    }
    parsed = parse_agent_id(agent_id)
    out["session_num"] = parsed[1] if parsed else None
    if not out["found"]:
        out["notes"] = ["session files not found under this install's agents root"]
        out["rows"] = []
        return out
    assert session_dir is not None
    status = read_status(session_dir) or {}
    journal_path = session_dir / "journal.md"
    journal_text = _read_text(journal_path)
    archive_text = _read_text(session_dir / ARCHIVE_NAME)

    ticks = {
        t["tick"]: t for t in parse_ticks(archive_text) + parse_ticks(journal_text)
    }
    decisions = parse_decisions(archive_text) + parse_decisions(
        section(journal_text, "Decisions")
    )
    state = status.get("state") or "unknown"
    live = state in LIVE_STATES
    started = session_started_at(session_dir)
    ended = status.get("updated_at")
    out.update(
        state=state,
        started_at=started,
        ended_at=None if live or not isinstance(ended, (int, float)) else float(ended),
        ticks=max(
            count_journal_ticks(journal_path),
            int(status.get("tick") or 0),
            max(ticks, default=0),
        ),
    )

    errors = [
        d
        for d in decisions
        if d["action"] == "error" and not d["text"].startswith(_NOT_A_TICK_ERROR)
    ]
    refusals = [d for d in decisions if d["action"] in REFUSAL_ACTIONS]
    out["error_lines"] = [{"hm": e["hm"], "text": e["text"][:300]} for e in errors]
    out["ticks_errored"] = len(errors)
    out["ticks_blocked"] = sum(
        1 for t in ticks.values() if t["summary"].startswith("blocked:")
    )
    out["refusals"] = [
        {
            "tick": r["tick"],
            "kind": r["action"],
            "reason": r["text"][:300],
        }
        for r in refusals
    ]
    out["stop"] = stop_from_decisions(decisions)

    # PnL as the agent saw it, one reading per tick. The journal's own reader
    # drops the ticks that read nothing, so a gap is a gap and not a $0.
    series: list[dict[str, Any]] = []
    if journal_path.is_file():
        try:
            series = JournalManager(agent_id, session_dir=session_dir).get_pnl_series()
        except Exception:  # noqa: BLE001 - a bad journal must not sink the report
            log.warning("shift report: pnl series of %s unreadable", agent_id)
    last_pnl, max_dd, points = _pnl_stats(series)
    out["journal_pnl"] = {
        "last": last_pnl,
        "max_drawdown_quote": max_dd,
        "points": points,
        "series": series,
    }

    actions = read_all_actions(session_dir)
    out["actions"] = actions
    out["deployed_bots"] = sorted(
        {
            str(a.get("subject"))
            for a in actions
            if a.get("verb") == DEPLOY_VERB and a.get("ok") and a.get("subject")
        }
    )
    out["executors_created"] = sum(
        1 for a in actions if a.get("ok") and a.get("tool") in CREATE_EXECUTOR_TOOLS
    )
    out["closeout"] = read_closeout(session_dir)

    rows = _tick_rows(agent_id, session_dir, ticks, decisions, actions, harvested or {})
    out["rows"] = rows
    detailed = [r for r in rows if r["tools"] is not None]
    tool_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in detailed:
        for call in row["tools"]:
            tool_counts[call["name"]][call["status"] or "unknown"] += 1
    out["tool_calls"] = {name: dict(c) for name, c in sorted(tool_counts.items())}
    out["ticks_with_tool_detail"] = len(detailed)
    out["ticks_timed_out"] = sum(1 for r in rows if r.get("timed_out"))
    gate = [r["drawdown_pct"] for r in rows if r.get("drawdown_pct") is not None]
    out["max_gate_drawdown_pct"] = max(gate) if gate else None
    notes = []
    if out["ticks"] and len(detailed) < out["ticks"]:
        notes.append(
            f"tool-call detail exists for {len(detailed)} of {out['ticks']} ticks "
            "(the engine keeps 100 snapshots a session; blocked ticks write none)"
        )
    out["notes"] = notes
    return out


def _tick_rows(
    agent_id: str,
    session_dir: Path,
    ticks: Mapping[int, Mapping[str, Any]],
    decisions: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    harvested: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """One row per tick, from the richest source that still has it.

    Harvested detail wins, then a snapshot still on disk, then only the journal's
    Ticks line. ``tools`` is ``None`` — not ``[]`` — for a tick whose calls were
    never recorded, so "no detail" is never read as "called nothing".
    """
    files = snapshot_files(session_dir)
    numbers = sorted(set(ticks) | set(harvested) | set(files))
    by_tick_actions: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for a in actions:
        by_tick_actions[int(a.get("tick") or 0)].append(a)
    by_tick_decisions: dict[int, list[dict[str, Any]]] = defaultdict(list)
    errors_by_minute: dict[str, list[str]] = defaultdict(list)
    for d in decisions:
        if d["tick"] is not None:
            by_tick_decisions[d["tick"]].append(d)
        elif d["action"] == "error" and not d["text"].startswith(_NOT_A_TICK_ERROR):
            # The journal writes a tick's error with no tick number, only the
            # minute. It is attached to a tick only when that minute names
            # exactly one, so a wrong tick never carries someone else's error.
            errors_by_minute[d["hm"]].append(d["text"])
    ticks_per_minute = Counter(
        str(t.get("stamp", ""))[-5:] for t in ticks.values() if t.get("stamp")
    )

    rows = []
    for n in numbers:
        line = ticks.get(n, {})
        detail: Mapping[str, Any] | None = harvested.get(n)
        source = "harvest"
        if detail is None and n in files:
            snap = parse_snapshot(_read_text(files[n]))
            detail = {
                "stamp": snap["stamp"],
                "response": snap["response"][:RESPONSE_CHARS],
                "action": one_line(snap["response"]),
                "tools": snap["tools"],
                "drawdown_pct": snap["drawdown_pct"],
                "risk_check": snap["risk_check"],
                "duration_s": snap["duration_s"],
                "timed_out": snap["timed_out"],
            }
            source = "snapshot"
        stamp = (detail or {}).get("stamp") or line.get("stamp") or ""
        at = parse_minute(stamp)
        summary = str(line.get("summary") or "")
        blocked = (
            summary[len("blocked:") :].strip()
            if summary.startswith("blocked:")
            else None
        )
        minute = str(line.get("stamp", ""))[-5:]
        errors = (
            errors_by_minute.get(minute, [])
            if minute and ticks_per_minute.get(minute) == 1
            else []
        )
        refused = [
            {"kind": d["action"], "reason": d["text"][:300]}
            for d in by_tick_decisions.get(n, [])
            if d["action"] in REFUSAL_ACTIONS
        ]
        mutating = [
            {
                "verb": a.get("verb"),
                "ok": bool(a.get("ok")),
                "summary": str(a.get("summary") or "")[:200],
                "error": str(a.get("error") or "")[:200],
            }
            for a in by_tick_actions.get(n, [])
        ]
        action = (detail or {}).get("action") or one_line(summary)
        tools = (detail or {}).get("tools")
        rows.append(
            {
                "agent_id": agent_id,
                "tick": n,
                "at": utc_iso(at),
                "at_epoch": at,
                "action": action,
                "tools": (
                    [
                        {
                            "name": c.get("name"),
                            "status": c.get("status"),
                            "verb": c.get("verb"),
                        }
                        for c in tools
                    ]
                    if tools is not None
                    else None
                ),
                "mutating": mutating,
                "refusals": refused,
                "error": errors[0] if errors else None,
                "blocked": blocked,
                "timed_out": (detail or {}).get("timed_out"),
                "drawdown_pct": (detail or {}).get("drawdown_pct"),
                "duration_s": (detail or {}).get("duration_s"),
                "result": _result_line(mutating, refused, errors, blocked),
                "detail": source if detail is not None else "journal",
            }
        )
    return rows


def _result_line(
    mutating: list[dict[str, Any]],
    refused: list[dict[str, Any]],
    errors: list[str],
    blocked: str | None,
) -> str:
    if blocked:
        return f"blocked: {blocked}"[:200]
    parts = []
    if errors:
        parts.append(f"error: {errors[0]}"[:120])
    if mutating:
        ok = sum(1 for m in mutating if m["ok"])
        parts.append(f"{len(mutating)} mutating ({ok} ok)")
        failed = next((m for m in mutating if not m["ok"]), None)
        if failed:
            parts.append(
                f"failed {failed['verb']}: {failed['error'] or 'no reason'}"[:120]
            )
    if refused:
        parts.append(f"{len(refused)} refused: {refused[0]['reason']}"[:140])
    return "; ".join(parts) or "no mutating calls"


# ── balances ──


def summarize_balances(state: Any) -> list[dict[str, Any]]:
    """``POST /portfolio/state`` as flat rows, one per account / connector / token."""
    rows: list[dict[str, Any]] = []
    if not isinstance(state, Mapping):
        return rows
    for account, connectors in state.items():
        if not isinstance(connectors, Mapping):
            continue
        for connector, tokens in connectors.items():
            if not isinstance(tokens, list):
                continue
            for item in tokens:
                if not isinstance(item, Mapping):
                    continue
                rows.append(
                    {
                        "account": str(account),
                        "connector": str(connector),
                        "token": str(item.get("token", "")),
                        "units": _num(item.get("units")),
                        "price": _num(item.get("price")),
                        "value": _num(item.get("value")),
                    }
                )
    return rows


def _num(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def balance_totals(rows: Iterable[Mapping[str, Any]]) -> dict[str, float | None]:
    """Value per connector; ``None`` when any of its tokens carried no value."""
    totals: dict[str, float | None] = {}
    for row in rows:
        key = f"{row['account']}/{row['connector']}"
        value = row.get("value")
        if value is None or totals.get(key, 0.0) is None:
            totals[key] = None
        else:
            totals[key] = (totals.get(key) or 0.0) + value
    return totals


def diff_balances(
    start: list[dict[str, Any]], end: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Per account / connector / token: units at both ends and the change."""
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for label, rows in (("start", start), ("end", end)):
        for r in rows:
            key = (r["account"], r["connector"], r["token"])
            entry = index.setdefault(
                key,
                {
                    "account": key[0],
                    "connector": key[1],
                    "token": key[2],
                    "start_units": None,
                    "end_units": None,
                    "start_value": None,
                    "end_value": None,
                },
            )
            entry[f"{label}_units"] = r["units"]
            entry[f"{label}_value"] = r["value"]
    out = []
    for entry in index.values():
        s, e = entry["start_units"], entry["end_units"]
        entry["delta_units"] = (
            (e or 0.0) - (s or 0.0) if (s is not None or e is not None) else None
        )
        out.append(entry)
    return sorted(out, key=lambda r: (r["connector"], r["token"]))


# ── building the report ──


def load_record(shift_dir: Path) -> dict[str, Any]:
    path = shift_dir / RECORD_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportError(f"{path}: cannot read the shift record — {exc}") from exc
    if not isinstance(record, dict):
        raise ReportError(f"{path}: the shift record must be a JSON object")
    return record


def _discover_sessions(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Sessions that carry this shift's id but that the record never listed.

    The runner records every session it starts, but it can die between the start
    route answering and the record being written. Each session's own
    ``config.yml`` has the ``shift_id`` it was started with, so those sessions
    are still findable — which is why the stamp goes into the run config at all.
    """
    import yaml

    found: list[dict[str, Any]] = []
    seen_dirs: set[Path] = set()
    for loop_id, loop in (record.get("loops") or {}).items():
        agent, strategy = loop.get("agent"), loop.get("strategy")
        if not agent or not strategy:
            continue
        strategy_dir = strategy_dir_for_run_key(f"{agent}.{strategy}")
        if strategy_dir is None or strategy_dir in seen_dirs:
            continue
        seen_dirs.add(strategy_dir)
        for number, session_dir in iter_session_dirs(strategy_dir):
            try:
                cfg = yaml.safe_load(_read_text(session_dir / "config.yml")) or {}
            except yaml.YAMLError:
                continue
            if not isinstance(cfg, dict) or cfg.get("shift_id") != record.get(
                "shift_id"
            ):
                continue
            found.append(
                {
                    "agent_id": f"{agent}.{strategy}_{number}",
                    "session_num": number,
                    "trading_context": cfg.get("trading_context", ""),
                    "agent": agent,
                    "strategy": strategy,
                }
            )
    return found


def collect_sessions(record: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """``{loop id: [session entries]}`` plus ``"?"`` for sessions no loop claims."""
    sessions: dict[str, list[dict[str, Any]]] = {}
    known: set[str] = set()
    loops = record.get("loops") or {}
    for loop_id, loop in loops.items():
        entries = [dict(s) for s in loop.get("sessions") or []]
        sessions[loop_id] = entries
        known.update(s["agent_id"] for s in entries if s.get("agent_id"))
    for extra in _discover_sessions(record):
        if extra["agent_id"] in known:
            continue
        owner = next(
            (
                loop_id
                for loop_id, loop in loops.items()
                if loop.get("agent") == extra["agent"]
                and loop.get("strategy") == extra["strategy"]
                and (loop.get("trading_context") or "") == extra["trading_context"]
            ),
            "?",
        )
        sessions.setdefault(owner, []).append(
            {
                "agent_id": extra["agent_id"],
                "session_num": extra["session_num"],
                "agent": extra["agent"],
                "strategy": extra["strategy"],
                "reason": "discovered on disk (not in shift.json)",
            }
        )
    return sessions


async def _try(coro_fn, *args) -> tuple[Any, str]:
    try:
        return await coro_fn(*args), ""
    except Exception as exc:  # noqa: BLE001 - every source may be down; say which
        return None, f"{type(exc).__name__}: {exc}"[:240]


def _performance(payload: Any, unavailable_note: str) -> dict[str, Any]:
    """The session route's answer as a PnL block that says when it is not a reading."""
    if not isinstance(payload, Mapping):
        return {"available": False, "why": unavailable_note or "no answer"}
    perf = payload.get("performance")
    if not isinstance(perf, Mapping):
        return {"available": False, "why": "the route returned no performance block"}
    if payload.get("unavailable"):
        return {
            "available": False,
            "why": f"server could not price it ({payload['unavailable']})",
            "raw": dict(perf),
        }
    reading = str(perf.get("reading") or "ok")
    block: dict[str, Any] = {
        "available": reading == "ok",
        "reading": reading,
        "realized": perf.get("realized_pnl"),
        "unrealized": perf.get("unrealized_pnl"),
        "total": perf.get("total_pnl"),
        "volume": perf.get("volume"),
        "fees": perf.get("fees"),
        "trade_count": perf.get("trade_count"),
        "win_rate": perf.get("win_rate"),
        "open_count": perf.get("open_count"),
        "closed_count": perf.get("closed_count"),
        "fees_known": perf.get("fees_known", True),
        "unresolved_bases": perf.get("unresolved_bases") or [],
        "close_type_counts": perf.get("close_type_counts") or {},
        "bot_instances": perf.get("bot_instances") or [],
    }
    if reading != "ok":
        block["why"] = (
            f"the performance read was '{reading}': the zeros mean no reading, "
            "not a flat book"
        )
    return block


def _sum_pnl(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """A loop's PnL: the sum over its sessions, or ``n/a`` if any session has none."""
    if not blocks:
        return {"available": False, "why": "no session to price"}
    missing = [b for b in blocks if not b.get("available")]
    if missing:
        return {
            "available": False,
            "why": "; ".join(
                sorted({str(b.get("why", "unavailable")) for b in missing})
            ),
            "sessions_priced": len(blocks) - len(missing),
            "sessions": len(blocks),
        }
    total: dict[str, Any] = {"available": True, "sessions": len(blocks)}
    for key in (
        "realized",
        "unrealized",
        "total",
        "volume",
        "fees",
        "trade_count",
        "open_count",
        "closed_count",
    ):
        values = [b.get(key) for b in blocks]
        total[key] = (
            sum(v for v in values if isinstance(v, (int, float)))
            if any(isinstance(v, (int, float)) for v in values)
            else None
        )
    total["win_rate"] = blocks[0].get("win_rate") if len(blocks) == 1 else None
    total["fees_known"] = all(b.get("fees_known", True) for b in blocks)
    return total


def llm_usage(
    record: Mapping[str, Any],
    callers: Mapping[str, str],
    since: float,
    until: float,
    directory: Path | None = None,
) -> dict[str, Any]:
    """What the shift cost in model requests, from the ledger.

    ``callers`` maps each of this shift's session ids to its loop id: a ledger
    line from any other caller is not this shift's (a chat, another tool on the
    same key) and is counted apart, because it spent the same day's budget.
    """
    view = read_ledger(since, until, directory)
    mine_req = [r for r in view.reqs if r.get("caller") in callers]
    other_req = [r for r in view.reqs if r.get("caller") not in callers]
    mine_res = [r for r in view.ress if r.get("caller") in callers]
    mine_lim = [r for r in view.limits if r.get("caller") in callers]

    by_hour: Counter[str] = Counter()
    by_loop: Counter[str] = Counter()
    for r in mine_req:
        if not view.counted(r):
            continue
        by_hour[
            datetime.fromtimestamp(r["t"], timezone.utc).strftime("%Y-%m-%d %H:00Z")
        ] += 1
        by_loop[callers[r["caller"]]] += 1
    statuses = Counter(str(r.get("status")) for r in mine_res)
    latencies = [
        float(r["ms"])
        for r in mine_res
        if isinstance(r.get("ms"), (int, float)) and r["ms"] > 0
    ]
    limits_429 = [
        {
            "at": utc_iso(r.get("t")),
            "caller": r.get("caller"),
            "loop": callers.get(str(r.get("caller")), "?"),
            "status": r.get("status"),
            "daily": bool(r.get("daily")),
            "reason": str(r.get("reason") or "")[:200],
        }
        for r in mine_lim
    ]
    in_tokens = [
        r["input_tokens"]
        for r in mine_res
        if isinstance(r.get("input_tokens"), (int, float))
    ]
    out_tokens = [
        r["output_tokens"]
        for r in mine_res
        if isinstance(r.get("output_tokens"), (int, float))
    ]
    return {
        "available": bool(view.files),
        "files": view.files,
        "window": [utc_iso(since), utc_iso(until)],
        "requests_counted": sum(by_loop.values()),
        "requests_logged": len(mine_req),
        "retries": sum(1 for r in mine_req if r.get("retry")),
        "uncounted": sum(1 for r in mine_req if not view.counted(r)),
        "by_hour": dict(sorted(by_hour.items())),
        "by_loop": dict(sorted(by_loop.items())),
        "status_codes": dict(sorted(statuses.items())),
        "limit_events": limits_429,
        "latency_ms": {
            "n": len(latencies),
            "mean": (sum(latencies) / len(latencies)) if latencies else None,
            "p95": percentile(latencies, 95),
            "max": max(latencies) if latencies else None,
        },
        "tokens": (
            {"input": int(sum(in_tokens)), "output": int(sum(out_tokens))}
            if in_tokens or out_tokens
            else None
        ),
        "other_callers_counted": sum(1 for r in other_req if view.counted(r)),
        "bad_lines": view.bad_lines,
    }


LIMITS_TEXT = [
    "Testnet liquidity, spreads and fills are not a live market. Books are thin or "
    "synthetic, there is no real adverse selection, and a fill that happens here "
    "may never happen on mainnet. Nothing in this report is a statement about "
    "what the same loops would earn with real money.",
    "One shift is one sample. The number of ticks and the number of fills below "
    "are the sample size, and they are small. A positive PnL here is not "
    "statistical evidence of profit, and a negative one is not evidence of "
    "loss; the difference between the two is inside the noise.",
    "The model is a free-tier model (see the header). Its behaviour says nothing "
    "about any other model, and its decisions were made under a per-minute and "
    "per-day request cap that shaped how often it could look at the market.",
    "PnL is the Condor server's reading of the Hummingbot API's executor and bot "
    "records (realized plus the mark of what was still open). Where it could not "
    "be read it says so; it is never filled with zero. The account value change "
    "is a separate, cruder check: it includes the price move of every token held.",
    "What it does show: whether the loops stayed up unattended, how many ticks "
    "ran and how many failed, what the agent chose and what the risk gate "
    "refused, what the run cost in requests, and an audit trail "
    "(decisions.jsonl) a reader can check against the journals.",
]


async def build_report(
    shift_dir: Path,
    *,
    condor: Api | None,
    hb: Api | None,
    now: float,
    ledger_directory: Path | None = None,
    final: bool = True,
) -> dict[str, Any]:
    """Build and write ``report.md``, ``report.json`` and ``decisions.jsonl``.

    ``condor`` and ``hb`` may be ``None`` (or fail): the report is then built from
    the disk alone and the sections that needed them say so. ``final`` is False
    for a report written while loops are still running, and is stamped on it.
    """
    record = load_record(shift_dir)
    coverage: dict[str, str] = {}
    started = float(record.get("started_at") or now)
    ended = record.get("ended_at")
    until = float(ended) if isinstance(ended, (int, float)) else now
    loops_meta = record.get("loops") or {}
    sessions_by_loop = collect_sessions(record)
    harvester = Harvester(shift_dir / HARVEST_DIR)

    callers: dict[str, str] = {}
    loop_reports: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    all_pnl_available = True
    session_files_missing = False

    for loop_id, entries in sessions_by_loop.items():
        meta = loops_meta.get(loop_id, {})
        digests: list[dict[str, Any]] = []
        pnl_blocks: list[dict[str, Any]] = []
        deployments: list[dict[str, Any]] = []
        for entry in entries:
            agent_id = entry["agent_id"]
            callers[agent_id] = loop_id
            session_dir = locate_session(agent_id)
            if session_dir is not None:
                harvester.harvest(
                    agent_id, session_dir, now=now, min_age=0.0 if final else 3.0
                )
            digest = digest_session(
                agent_id, session_dir, harvested=harvester.harvested(agent_id)
            )
            digest["reason"] = entry.get("reason", "")
            digest["server_boot"] = entry.get("server_boot")
            if not digest["found"]:
                session_files_missing = True
                # What the runner saw while it was up is the only count left.
                digest["ticks"] = int(entry.get("ticks_seen") or 0)
                digest["state"] = entry.get("final_state") or "unknown"
            elif digest["state"] in LIVE_STATES and final:
                digest["notes"].append(
                    f"status.json still says {digest['state']!r} at report time: "
                    "the process may have died without recording an end"
                )
            digests.append(digest)
            number = entry.get("session_num") or digest.get("session_num")
            agent = entry.get("agent") or meta.get("agent")
            strategy = entry.get("strategy") or meta.get("strategy")
            if condor is None:
                pnl_blocks.append(
                    {
                        "available": False,
                        "why": "the Condor server was not reachable for the report",
                    }
                )
            elif number is None or not agent or not strategy:
                pnl_blocks.append(
                    {
                        "available": False,
                        "why": "the session could not be tied to an agent and strategy",
                    }
                )
            else:
                payload, err = await _try(
                    condor.get,
                    f"/agents/{agent}/strategies/{strategy}/sessions/{number}/executors",
                )
                pnl_blocks.append(_performance(payload, err))
                if isinstance(payload, Mapping):
                    deployments.extend(
                        {**d, "session": number}
                        for d in payload.get("deployments") or []
                        if isinstance(d, Mapping)
                    )
            for row in digest["rows"]:
                decisions.append(
                    {
                        "shift_id": record.get("shift_id"),
                        "loop": loop_id,
                        "session": number,
                        **row,
                    }
                )

        pnl = _sum_pnl(pnl_blocks)
        all_pnl_available &= bool(pnl.get("available"))
        loop_reports.append(_loop_report(loop_id, meta, digests, pnl, deployments))

    coverage["session_files"] = (
        "partly missing: some sessions were not found under this install's agents root "
        "(if the server runs with another CONDOR_AGENTS_ROOT / CONDOR_RUNTIME_ROOT, run "
        "the report with the same environment)"
        if session_files_missing
        else "ok"
    )
    coverage["pnl"] = (
        "ok (Condor session route)"
        if all_pnl_available
        else "partly or wholly unavailable: see each loop's pnl.why"
    )

    usage = llm_usage(record, callers, started, until, ledger_directory)
    coverage["request_ledger"] = (
        f"ok ({', '.join(usage['files'])})"
        if usage["available"]
        else "no ledger file in the shift window"
    )
    if usage["tokens"] is None:
        coverage["tokens"] = (
            "not recorded: the request ledger counts requests and loop ticks do not "
            "persist token usage in this build"
        )

    start_rows = record.get("balances_start")
    end_rows = record.get("balances_end")
    balances_note: dict[str, Any] = {}
    if end_rows is None and hb is not None:
        state, err = await _try(
            hb.post, "/portfolio/state", _balance_request(record, refresh=True)
        )
        if state is not None:
            end_rows = summarize_balances(state)
            balances_note["end_captured_by"] = "report (not at shift end)"
            balances_note["end_captured_at"] = utc_iso(now)
        else:
            balances_note["end_error"] = err
    coverage["balances"] = (
        "ok"
        if start_rows is not None and end_rows is not None
        else "incomplete: "
        + ", ".join(
            name
            for name, rows in (("start", start_rows), ("end", end_rows))
            if rows is None
        )
        + " balances were not captured"
    )
    balances = {
        "start": start_rows,
        "end": end_rows,
        "diff": (
            diff_balances(start_rows, end_rows)
            if start_rows is not None and end_rows is not None
            else None
        ),
        "totals_start": balance_totals(start_rows) if start_rows is not None else None,
        "totals_end": balance_totals(end_rows) if end_rows is not None else None,
        **balances_note,
    }

    decisions.sort(key=lambda r: (r.get("at_epoch") or 0, r["loop"], r["tick"]))
    totals = _totals(loop_reports)
    report = {
        "schema": 1,
        "generated_at": utc_iso(now),
        "final": final,
        "shift": {
            "id": record.get("shift_id"),
            "profile": record.get("profile_name"),
            "description": (record.get("profile") or {}).get("description", ""),
            "state": record.get("state"),
            "model": record.get("model"),
            "hours": record.get("hours"),
            "started_at": utc_iso(started),
            "ends_at": utc_iso(record.get("ends_at")),
            "ended_at": utc_iso(ended) if isinstance(ended, (int, float)) else None,
            "capital_quote": record.get("capital_quote"),
            "server_boot_ids": record.get("server_boot_ids", []),
        },
        "loops": loop_reports,
        "totals": totals,
        "balances": balances,
        "llm": usage,
        "events": record.get("events", []),
        "coverage": coverage,
        "limits": LIMITS_TEXT,
        "decisions": {"file": DECISIONS_FILE, "count": len(decisions)},
    }
    atomic_write_json(shift_dir / REPORT_JSON, report, indent=2)
    atomic_write_text(
        shift_dir / DECISIONS_FILE,
        "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in decisions),
    )
    atomic_write_text(shift_dir / REPORT_MD, render_markdown(report, decisions))
    return report


def _balance_request(record: Mapping[str, Any], *, refresh: bool) -> dict[str, Any]:
    connectors = sorted(
        {
            str(loop.get("connector"))
            for loop in (record.get("loops") or {}).values()
            if loop.get("connector") and loop.get("runnable", True)
        }
    )
    return {
        "account_names": [record.get("account", "master_account")],
        "connector_names": connectors,
        "refresh": refresh,
    }


def _loop_report(
    loop_id: str,
    meta: Mapping[str, Any],
    digests: list[dict[str, Any]],
    pnl: dict[str, Any],
    deployments: list[dict[str, Any]],
) -> dict[str, Any]:
    tool_calls: dict[str, Counter[str]] = defaultdict(Counter)
    refusal_reasons: Counter[tuple[str, str]] = Counter()
    for d in digests:
        for name, counts in (d.get("tool_calls") or {}).items():
            tool_calls[name].update(counts)
        for r in d.get("refusals") or []:
            refusal_reasons[(r["kind"], r["reason"])] += 1
    kinds = Counter()
    for (kind, _reason), n in refusal_reasons.items():
        kinds[kind] += n
    drawdowns = [
        d["journal_pnl"]["max_drawdown_quote"]
        for d in digests
        if d.get("found") and d["journal_pnl"]["max_drawdown_quote"] is not None
    ]
    gate = [
        d["max_gate_drawdown_pct"]
        for d in digests
        if d.get("max_gate_drawdown_pct") is not None
    ]
    capital = meta.get("capital_quote")
    worst = max(drawdowns) if drawdowns else None
    return {
        "id": loop_id,
        "agent": meta.get("agent"),
        "strategy": meta.get("strategy"),
        "connector": meta.get("connector"),
        "trading_context": meta.get("trading_context"),
        "capital_quote": capital,
        "request_share": meta.get("request_share"),
        "frequency_sec": meta.get("frequency_sec"),
        "runnable": meta.get("runnable", True),
        "skip_reason": meta.get("skip_reason", ""),
        "final": meta.get("final"),
        "restarts": meta.get("restarts", []),
        "sessions": [
            {
                k: d.get(k)
                for k in (
                    "agent_id",
                    "session_num",
                    "found",
                    "state",
                    "started_at",
                    "ended_at",
                    "ticks",
                    "ticks_errored",
                    "ticks_blocked",
                    "ticks_timed_out",
                    "stop",
                    "reason",
                    "server_boot",
                    "notes",
                )
            }
            for d in digests
        ],
        "ticks": sum(d.get("ticks") or 0 for d in digests),
        "ticks_errored": sum(d.get("ticks_errored") or 0 for d in digests),
        "ticks_blocked": sum(d.get("ticks_blocked") or 0 for d in digests),
        "ticks_timed_out": sum(d.get("ticks_timed_out") or 0 for d in digests),
        "ticks_with_tool_detail": sum(
            d.get("ticks_with_tool_detail") or 0 for d in digests
        ),
        "error_samples": [e for d in digests for e in d.get("error_lines") or []][:8],
        "tool_calls": {n: dict(c) for n, c in sorted(tool_calls.items())},
        "bots_deployed": sorted(
            {b for d in digests for b in d.get("deployed_bots") or []}
        ),
        "executors_created": sum(d.get("executors_created") or 0 for d in digests),
        "deployments": deployments,
        "pnl": pnl,
        "journal_pnl_last": [
            d["journal_pnl"]["last"] for d in digests if d.get("found")
        ],
        "max_drawdown": {
            "quote": worst,
            "pct_of_capital": (
                (worst / capital * 100.0) if worst is not None and capital else None
            ),
            "gate_pct": max(gate) if gate else None,
            "basis": "peak-to-trough of the journal's per-tick PnL readings, from PnL 0 at session "
            "start; gate_pct is the largest drawdown the risk gate itself measured at a tick start",
        },
        "refusals": {
            "total": sum(kinds.values()),
            "by_kind": dict(kinds),
            "reasons": [
                {"kind": k, "reason": r, "count": n}
                for (k, r), n in refusal_reasons.most_common(12)
            ],
        },
        "closeouts": [
            {"agent_id": d["agent_id"], **d["closeout"]}
            for d in digests
            if d.get("closeout")
        ],
    }


def _totals(loops: list[dict[str, Any]]) -> dict[str, Any]:
    priced = [l for l in loops if l["pnl"].get("available")]
    unpriced = [
        l["id"] for l in loops if not l["pnl"].get("available") and l["sessions"]
    ]
    pnl: dict[str, Any] = {"loops_priced": len(priced), "loops_unpriced": unpriced}
    for key in ("realized", "unrealized", "total", "volume", "fees", "trade_count"):
        values = [
            l["pnl"].get(key)
            for l in priced
            if isinstance(l["pnl"].get(key), (int, float))
        ]
        pnl[key] = sum(values) if values else None
    pnl["partial"] = bool(unpriced)
    return {
        "ticks": sum(l["ticks"] for l in loops),
        "ticks_errored": sum(l["ticks_errored"] for l in loops),
        "ticks_blocked": sum(l["ticks_blocked"] for l in loops),
        "tool_calls": sum(
            sum(counts.values()) for l in loops for counts in l["tool_calls"].values()
        ),
        "refusals": sum(l["refusals"]["total"] for l in loops),
        "restarts": sum(len(l["restarts"]) for l in loops),
        "pnl": pnl,
    }


# ── markdown ──


def money(value: Any, signed: bool = True) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{value:+,.2f}" if signed else f"{value:,.2f}"


def _pnl_cell(pnl: Mapping[str, Any], key: str) -> str:
    if not pnl.get("available"):
        return "n/a"
    return money(pnl.get(key))


def render_markdown(report: Mapping[str, Any], decisions: list[dict[str, Any]]) -> str:
    shift = report["shift"]
    totals = report["totals"]
    llm = report["llm"]
    out: list[str] = []
    w = out.append

    w(f"# Shift report: {shift['id']}")
    w("")
    w(f"- profile: `{shift['profile']}` — {shift['description'] or 'no description'}")
    w(
        f"- window: {shift['started_at']} → {shift['ended_at'] or shift['ends_at']} UTC "
        f"({shift['hours']}h planned), state **{shift['state']}**"
        + ("" if report["final"] else " (written while the shift was still running)")
    )
    w(f"- model: `{shift['model']}`")
    w(
        f"- capital allocated: {money(shift['capital_quote'], signed=False)} (quote units, testnet)"
    )
    w(f"- generated: {report['generated_at']}")
    w("")

    w("## What this does and does not show")
    w("")
    for line in report["limits"]:
        w(f"- {line}")
    n_fills = totals["pnl"].get("trade_count")
    w(
        f"- Sample size of this report: **{totals['ticks']} ticks** across "
        f"{len(report['loops'])} loop(s), "
        + (
            f"**{int(n_fills)} trades** (closed round trips)"
            if isinstance(n_fills, (int, float))
            else "trade count not available"
        )
        + f", {totals['tool_calls']} recorded tool calls."
    )
    w("")

    w("## Summary")
    w("")
    pnl = totals["pnl"]
    w("| | |")
    w("|---|---|")
    w(
        f"| ticks run / errored / risk-blocked | {totals['ticks']} / {totals['ticks_errored']} / {totals['ticks_blocked']} |"
    )
    w(f"| risk-gate and venue refusals | {totals['refusals']} |")
    w(f"| loop restarts by the runner | {totals['restarts']} |")
    w(f"| realized PnL (priced loops) | {money(pnl['realized'])} |")
    w(f"| unrealized PnL (priced loops) | {money(pnl['unrealized'])} |")
    w(
        f"| total PnL (priced loops) | {money(pnl['total'])}"
        + (
            f" — PARTIAL, not priced: {', '.join(pnl['loops_unpriced'])}"
            if pnl["partial"]
            else ""
        )
        + " |"
    )
    w(
        f"| volume / fees | {money(pnl['volume'], False)} / {money(pnl['fees'], False)} |"
    )
    w(f"| model requests (this shift, counted) | {llm['requests_counted']} |")
    w("")

    w("## Capital and allocation")
    w("")
    w("| loop | agent / strategy | connector | capital | req share | every | state |")
    w("|---|---|---|---:|---:|---:|---|")
    for lp in report["loops"]:
        state = (
            "SKIPPED: " + lp["skip_reason"]
            if not lp["runnable"]
            else (lp.get("final") or {}).get("state", "n/a")
        )
        w(
            f"| {lp['id']} | {lp['agent']} / {lp['strategy']} | {lp['connector']} | "
            f"{money(lp['capital_quote'], False)} | {lp['request_share']} | {lp['frequency_sec']}s | {state} |"
        )
    w("")

    w("## Balances")
    w("")
    bal = report["balances"]
    if bal["start"] is None or bal["end"] is None:
        w(f"Not complete: {report['coverage']['balances']}.")
    if bal.get("end_captured_by"):
        w(
            f"End balances were captured by the report at {bal['end_captured_at']}, not at the end of the shift."
        )
    if bal.get("diff"):
        w("")
        w("| connector | token | start | end | change |")
        w("|---|---|---:|---:|---:|")
        for r in bal["diff"]:
            w(
                f"| {r['connector']} | {r['token']} | {_units(r['start_units'])} | {_units(r['end_units'])} | {_units(r['delta_units'])} |"
            )
        w("")
        for key, label in (("totals_start", "start"), ("totals_end", "end")):
            for name, value in (bal.get(key) or {}).items():
                w(
                    f"- {label} value of {name}: {money(value, False)} (mark-to-market, includes price moves)"
                )
    w("")

    w("## Loops")
    for lp in report["loops"]:
        w("")
        w(f"### {lp['id']}")
        w("")
        if not lp["runnable"]:
            w(f"Skipped, not run: {lp['skip_reason']}.")
            continue
        w(
            f"- {lp['agent']} / {lp['strategy']} on `{lp['connector']}`, context: _{lp['trading_context']}_"
        )
        w(
            f"- ticks: {lp['ticks']} run, {lp['ticks_errored']} errored, {lp['ticks_blocked']} blocked by the risk gate, "
            f"{lp['ticks_timed_out']} timed out; tool-call detail for {lp['ticks_with_tool_detail']} of them"
        )
        for s in lp["sessions"]:
            note = f" ({s['reason']})" if s.get("reason") else ""
            stop = s.get("stop") or {}
            why = (
                f", stop: {stop['kind']} — {stop['text']}"
                if stop.get("kind") not in (None, "none")
                else ""
            )
            w(
                f"- session `{s['agent_id']}`: state {s['state']}, {s['ticks']} ticks{why}{note}"
            )
        for r in lp["restarts"]:
            w(
                f"- restart #{r.get('attempt')} at {utc_iso(r.get('at'))}: {r.get('reason')} → "
                f"{'ok' if r.get('ok') else 'FAILED'} {r.get('detail', '')}"
            )
        w("")
        p = lp["pnl"]
        if p.get("available"):
            w(
                f"- PnL: realized {_pnl_cell(p, 'realized')}, unrealized {_pnl_cell(p, 'unrealized')}, "
                f"total {_pnl_cell(p, 'total')}; volume {money(p.get('volume'), False)}, fees {money(p.get('fees'), False)}"
                f"{'' if p.get('fees_known', True) else ' (fees are a floor)'}; "
                f"{p.get('trade_count')} trades, {p.get('open_count')} open, {p.get('closed_count')} closed"
            )
        else:
            w(f"- PnL: **n/a** — {p.get('why')}")
        md = lp["max_drawdown"]
        w(
            f"- max drawdown seen: {money(md['quote'], False)} quote"
            + (
                f" ({md['pct_of_capital']:.2f}% of allocated capital)"
                if md["pct_of_capital"] is not None
                else ""
            )
            + (
                f"; the risk gate measured up to {md['gate_pct']:.2f}% at a tick start"
                if md["gate_pct"] is not None
                else ""
            )
            + " — per-tick readings, so a swing between two ticks is not in it"
        )
        dep = lp["bots_deployed"]
        w(
            f"- deployed: {len(dep)} bot(s){': ' + ', '.join(dep) if dep else ''}; {lp['executors_created']} executor(s) created"
        )
        if lp["tool_calls"]:
            w("")
            w("| tool | total | by outcome |")
            w("|---|---:|---|")
            for name, counts in lp["tool_calls"].items():
                w(
                    f"| {name} | {sum(counts.values())} | {', '.join(f'{k}: {v}' for k, v in sorted(counts.items()))} |"
                )
        else:
            w("- tool calls: none recorded (no snapshot or harvest detail)")
        ref = lp["refusals"]
        w("")
        w(
            f"- refusals: {ref['total']}"
            + (
                f" ({', '.join(f'{k}: {v}' for k, v in ref['by_kind'].items())})"
                if ref["by_kind"]
                else ""
            )
        )
        for r in ref["reasons"]:
            w(f"  - {r['count']}× {r['kind']}: {r['reason']}")
        if lp["error_samples"]:
            w(f"- tick errors (first {len(lp['error_samples'])}):")
            for e in lp["error_samples"]:
                w(f"  - {e['hm']} {e['text']}")
        for c in lp["closeouts"]:
            w(
                f"- close-out of `{c['agent_id']}`: {c.get('summary')}"
                + ("" if c.get("clean") else " — NOT CLEAN")
            )
    w("")

    w("## Model requests")
    w("")
    if not llm["available"]:
        w("No request-ledger file covers the shift window; usage is unknown.")
    else:
        w(
            f"- counted for this shift: {llm['requests_counted']} (logged {llm['requests_logged']}, "
            f"{llm['retries']} SDK retries, {llm['uncounted']} not counted); "
            f"other callers spent {llm['other_callers_counted']} of the same day's budget"
        )
        lat = llm["latency_ms"]
        w(
            f"- latency: n={lat['n']}, mean {_ms(lat['mean'])}, p95 {_ms(lat['p95'])}, max {_ms(lat['max'])}"
        )
        w(
            f"- status codes: {', '.join(f'{k}: {v}' for k, v in llm['status_codes'].items()) or 'none recorded'}"
        )
        w(
            "- tokens: "
            + (
                f"{llm['tokens']['input']} in / {llm['tokens']['output']} out"
                if llm["tokens"]
                else report["coverage"].get("tokens", "not recorded")
            )
        )
        w("")
        w("| hour (UTC) | requests |")
        w("|---|---:|")
        for hour, n in llm["by_hour"].items():
            w(f"| {hour} | {n} |")
        w("")
        w("| loop | requests |")
        w("|---|---:|")
        for loop, n in llm["by_loop"].items():
            w(f"| {loop} | {n} |")
        if llm["limit_events"]:
            w("")
            w(f"Provider refusals ({len(llm['limit_events'])}):")
            for ev in llm["limit_events"][:30]:
                w(
                    f"- {ev['at']} {ev['loop']} HTTP {ev['status']}{' (daily)' if ev['daily'] else ''}: {ev['reason']}"
                )
        else:
            w("")
            w("No provider refusals (429) were recorded for this shift.")
    w("")

    w("## Events")
    w("")
    events = report["events"]
    if not events:
        w("None.")
    for ev in events:
        w(
            f"- {utc_iso(ev.get('at'))} **{ev.get('kind')}** {ev.get('loop') or ''} {ev.get('text', '')}"
        )
    w("")

    w("## Decision timeline")
    w("")
    w(
        f"{len(decisions)} tick(s) in `{DECISIONS_FILE}` (one JSON object per tick). "
        "`detail` says where a row's tool calls came from: harvest, snapshot or journal only."
    )
    w("")
    w("| time (UTC) | loop | tick | chose | tools | result |")
    w("|---|---|---:|---|---|---|")
    shown = decisions
    elided = 0
    if len(decisions) > TIMELINE_MD_ROWS:
        half = TIMELINE_MD_ROWS // 2
        shown = decisions[:half] + decisions[-half:]
        elided = len(decisions) - len(shown)
    for i, d in enumerate(shown):
        if elided and i == TIMELINE_MD_ROWS // 2:
            w(
                f"| … | | | {elided} rows elided here; all of them are in {DECISIONS_FILE} | | |"
            )
        tools = "not recorded" if d["tools"] is None else _tool_summary(d["tools"])
        w(
            f"| {(d['at'] or '?')[5:16].replace('T', ' ')} | {d['loop']} | {d['tick']} | {_cell(d['action'])} | {_cell(tools)} | {_cell(d['result'])} |"
        )
    w("")

    w("## Data sources and gaps")
    w("")
    for name, state in report["coverage"].items():
        w(f"- {name}: {state}")
    w("")
    return "\n".join(out)


def _units(value: Any) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{value:,.6g}"


def _ms(value: Any) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{value / 1000:.1f}s"


def _cell(text: Any) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")[:140]


def _tool_summary(tools: list[dict[str, Any]]) -> str:
    counts: Counter[str] = Counter()
    for t in tools:
        counts[
            f"{t['name']}{'' if t['status'] == 'completed' else ' [' + str(t['status']) + ']'}"
        ] += 1
    return ", ".join(f"{k}×{v}" if v > 1 else k for k, v in counts.items()) or "none"
