"""A scheduled/continuous routine stuck failing the same way every tick must
notify its owner once, not once per tick (system review 2026-10-31, finding
#14 — the stale-password P0 posted the same ConnectionError every ~30s for an
extended period, with no dedup anywhere in the notification path).

Mirrors ``TickEngine._last_error`` (``condor/agents/engine.py``): the
instance's own record/telemetry still see every run; only the push to the
owner (``_report_run``) is gated on the error text actually having changed.
"""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import condor.reports as rep
from condor.routine_store import RoutineStore


@pytest.fixture
def reports_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_REPORTS_DIR", str(tmp_path))
    return tmp_path


def _routine(run_fn):
    class Config(BaseModel):
        pass

    return SimpleNamespace(
        name="probe", source="global", config_class=Config, run_fn=run_fn
    )


def _store() -> RoutineStore:
    store = RoutineStore()
    store._instances["i1"] = store._new_instance_meta(
        "probe", {}, "srv", 0, source="web"
    )
    return store


def _run(store: RoutineStore, routine) -> None:
    asyncio.run(
        store._execute_and_record(
            "i1", routine, {}, "srv", status_after="completed", fire_hooks=False
        )
    )


def _spy_report_run(store, monkeypatch):
    calls: list[str | None] = []

    async def fake_report_run(instance_id, summary, error):
        calls.append(error)

    monkeypatch.setattr(store, "_report_run", fake_report_run)
    return calls


async def _always_fails_the_same_way(config, ctx):
    raise ConnectionError("Server 'local' is unreachable")


async def _succeeds(config, ctx):
    return "ok"


def test_the_same_failure_repeated_notifies_only_once(reports_dir, monkeypatch):
    store = _store()
    calls = _spy_report_run(store, monkeypatch)
    routine = _routine(_always_fails_the_same_way)

    for _ in range(5):
        _run(store, routine)

    assert len(calls) == 1
    assert "unreachable" in calls[0]
    # The instance's own record still sees every tick, not just the first.
    assert store._instances["i1"]["run_count"] == 5
    assert store._instances["i1"]["error"] is not None


def test_a_different_failure_notifies_again(reports_dir, monkeypatch):
    store = _store()
    calls = _spy_report_run(store, monkeypatch)

    _run(store, _routine(_always_fails_the_same_way))

    async def _fails_differently(config, ctx):
        raise ValueError("a completely different problem")

    _run(store, _routine(_fails_differently))

    assert len(calls) == 2
    assert "unreachable" in calls[0]
    assert "different problem" in calls[1]


def test_recovery_then_the_same_failure_again_notifies(reports_dir, monkeypatch):
    """Clearing on success means a failure seen before is not silenced forever."""
    store = _store()
    calls = _spy_report_run(store, monkeypatch)

    _run(store, _routine(_always_fails_the_same_way))  # 1: notifies (failure)
    _run(store, _routine(_always_fails_the_same_way))  # repeat: suppressed
    _run(store, _routine(_succeeds))  # 2: notifies (recovery itself is a change)
    _run(store, _routine(_always_fails_the_same_way))  # 3: same text, notifies again

    assert len(calls) == 3
    assert [c is not None for c in calls] == [True, False, True]


def test_removing_the_instance_forgets_its_error_memory(reports_dir, monkeypatch):
    store = _store()
    _run(store, _routine(_always_fails_the_same_way))
    assert "i1" in store._last_run_error

    store.remove_instance("i1")

    assert "i1" not in store._last_run_error
