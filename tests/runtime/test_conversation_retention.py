"""Age-based conversation retention (system review 2026-10-31, finding #23).

``list_conversations``'s own docstring admits the store was never pruned, so
the directory count (and `transcript_archive.jsonl` within each one) grows
for the life of the install. Unlike the byte-bounded transcript retention
(`test_transcript_retention.py`), which only ever *moves* turns and never
deletes a conversation, this path genuinely deletes one — the same failure
mode that retention policy was written to avoid — so it stays off (0 days,
never prune) unless an operator explicitly opts in via
`CONDOR_CONVERSATION_RETENTION_DAYS`. These pin that default, the cutoff
itself, and the per-tick cap that protects a first run on a long-never-
pruned install.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from condor.runtime import conversations
from condor.runtime.conversations import (
    _conv_dir,
    conversation_retention_days,
    get_conversation,
    maybe_prune_stale_conversations,
    new_conversation,
    prune_stale_conversations,
)
from condor.runtime.keys import WEB

USER = 42
OTHER_USER = 43


@pytest.fixture
def conv_root(isolated_conversation_root):
    return isolated_conversation_root


@pytest.fixture(autouse=True)
def _reset_prune_gate(monkeypatch):
    monkeypatch.setattr(conversations, "_last_conversation_prune", None)


def _backdate(user_id: int, conv_id: str, age_days: float) -> None:
    """Directly rewrite meta.json's updated_at — write_status always
    re-stamps it to real now, so the public API cannot backdate it."""
    path = _conv_dir(user_id, conv_id) / "meta.json"
    data = json.loads(path.read_text())
    then = datetime.now(timezone.utc) - timedelta(days=age_days)
    data["updated_at"] = then.isoformat()
    path.write_text(json.dumps(data))


def test_retention_is_off_by_default(conv_root):
    assert conversation_retention_days() == 0


def test_disabled_retention_prunes_nothing(conv_root):
    meta = new_conversation(USER, WEB)
    _backdate(USER, meta.id, age_days=400)

    assert prune_stale_conversations() == 0  # max_age_days=None reads the (0) default
    assert get_conversation(USER, meta.id) is not None


def test_max_age_zero_is_an_explicit_no_op(conv_root):
    meta = new_conversation(USER, WEB)
    _backdate(USER, meta.id, age_days=400)

    assert prune_stale_conversations(max_age_days=0) == 0
    assert get_conversation(USER, meta.id) is not None


def test_a_stale_conversation_past_the_cutoff_is_deleted(conv_root):
    meta = new_conversation(USER, WEB)
    _backdate(USER, meta.id, age_days=31)

    assert prune_stale_conversations(max_age_days=30) == 1
    assert get_conversation(USER, meta.id) is None


def test_a_recent_conversation_is_kept(conv_root):
    meta = new_conversation(USER, WEB)  # updated_at is "now"

    assert prune_stale_conversations(max_age_days=30) == 0
    assert get_conversation(USER, meta.id) is not None


def test_prunes_across_every_user(conv_root):
    mine = new_conversation(USER, WEB)
    theirs = new_conversation(OTHER_USER, WEB)
    _backdate(USER, mine.id, age_days=100)
    _backdate(OTHER_USER, theirs.id, age_days=100)

    assert prune_stale_conversations(max_age_days=30) == 2
    assert get_conversation(USER, mine.id) is None
    assert get_conversation(OTHER_USER, theirs.id) is None


def test_the_env_var_sets_the_retention_days(conv_root, monkeypatch):
    monkeypatch.setenv("CONDOR_CONVERSATION_RETENTION_DAYS", "45")
    assert conversation_retention_days() == 45

    meta = new_conversation(USER, WEB)
    _backdate(USER, meta.id, age_days=46)

    assert prune_stale_conversations() == 1  # reads the env-set default
    assert get_conversation(USER, meta.id) is None


def test_an_unreadable_env_var_falls_back_to_off(conv_root, monkeypatch):
    monkeypatch.setenv("CONDOR_CONVERSATION_RETENTION_DAYS", "not-a-number")
    assert conversation_retention_days() == 0


def test_per_tick_cap_leaves_the_rest_for_next_time(conv_root, monkeypatch):
    monkeypatch.setattr(conversations, "CONVERSATION_PRUNE_PER_TICK", 2)
    metas = [new_conversation(USER, WEB) for _ in range(5)]
    for meta in metas:
        _backdate(USER, meta.id, age_days=100)

    deleted = prune_stale_conversations(max_age_days=30)

    assert deleted == 2
    remaining = sum(1 for m in metas if get_conversation(USER, m.id) is not None)
    assert remaining == 3


def test_maybe_prune_runs_once_then_waits_out_the_interval(conv_root):
    meta = new_conversation(USER, WEB)
    _backdate(USER, meta.id, age_days=400)

    import os

    os.environ["CONDOR_CONVERSATION_RETENTION_DAYS"] = "30"
    try:
        assert maybe_prune_stale_conversations() == 1
        assert get_conversation(USER, meta.id) is None

        # A second stale conversation arrives, but the gate is still closed.
        meta2 = new_conversation(USER, WEB)
        _backdate(USER, meta2.id, age_days=400)
        assert maybe_prune_stale_conversations() == 0
        assert get_conversation(USER, meta2.id) is not None
    finally:
        del os.environ["CONDOR_CONVERSATION_RETENTION_DAYS"]


def test_maybe_prune_is_a_harmless_noop_when_something_goes_wrong(
    conv_root, monkeypatch
):
    def _boom(*a, **kw):
        raise RuntimeError("disk is gone")

    monkeypatch.setattr(conversations, "prune_stale_conversations", _boom)
    monkeypatch.setenv("CONDOR_CONVERSATION_RETENTION_DAYS", "30")

    assert maybe_prune_stale_conversations() == 0  # swallowed, not raised


# ── Registration ──


def test_register_jobs_schedules_one_repeating_job(conv_root):
    class Queue:
        def __init__(self):
            self.scheduled = []

        def get_jobs_by_name(self, name):
            return []

        def run_repeating(self, callback, interval, first, name):
            self.scheduled.append((interval, first, name))

    class App:
        job_queue = Queue()

    app = App()
    conversations.register_jobs(app)

    assert app.job_queue.scheduled == [
        (
            conversations._CONVERSATION_PRUNE_INTERVAL.total_seconds(),
            600,
            conversations._CONVERSATION_PRUNE_JOB,
        )
    ]


def test_register_jobs_survives_an_application_without_a_queue(conv_root):
    class App:
        job_queue = None

    conversations.register_jobs(App())  # must not raise
