"""The sliding-window lockout in `condor/web/login_rate_limit.py`."""

import pytest

from condor.web import login_rate_limit as rl


@pytest.fixture(autouse=True)
def _clean_state():
    rl._failures.clear()
    yield
    rl._failures.clear()


def test_a_fresh_key_is_never_locked_out():
    rl.check("k")  # does not raise


def test_failures_below_the_threshold_do_not_lock(monkeypatch):
    for _ in range(rl.MAX_ATTEMPTS - 1):
        rl.record_failure("k")
    rl.check("k")  # still does not raise


def test_the_nth_failure_trips_the_lockout():
    for _ in range(rl.MAX_ATTEMPTS):
        rl.record_failure("k")

    with pytest.raises(Exception) as excinfo:
        rl.check("k")
    assert excinfo.value.status_code == 429


def test_a_success_clears_the_history():
    for _ in range(rl.MAX_ATTEMPTS):
        rl.record_failure("k")
    rl.record_success("k")

    rl.check("k")  # does not raise


def test_keys_do_not_interfere():
    for _ in range(rl.MAX_ATTEMPTS):
        rl.record_failure("attacker")

    rl.check("victim")  # a different key is unaffected


def test_old_failures_age_out_of_the_window(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(rl.time, "time", lambda: t[0])

    for _ in range(rl.MAX_ATTEMPTS):
        rl.record_failure("k")

    t[0] += rl.WINDOW_SECONDS + 1
    rl.check("k")  # the window has passed; no longer locked
