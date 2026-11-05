"""The 2FA enrollment/confirm/disable contract (`condor/web/routes/auth.py`).

The one rule worth pinning here: every route that turns 2FA *off*, or that
re-arms it over an *already-enabled* factor, has to prove possession of the
current code first. `disable_2fa` always did. `enroll_2fa` did not — calling
it with nothing but a live session cookie would silently overwrite the
victim's secret and clear the `enabled` flag, letting an attacker turn around
and `confirm` their own code to re-arm 2FA under a secret only they hold,
while looking to the real owner like nothing happened. That bypass is what
these tests exist to keep closed.
"""

import asyncio

import pyotp
import pytest
from fastapi import HTTPException

import config_manager as cm_module
from condor.web import auth as web_auth
from condor.web.models import WebUser
from condor.web.routes import auth as auth_routes
from condor.web.routes.auth import (
    Confirm2FARequest,
    Disable2FARequest,
    Enroll2FARequest,
    confirm_2fa,
    disable_2fa,
    enroll_2fa,
)

OWNER = WebUser(id=1, username="owner", role="user")


@pytest.fixture
def cm(tmp_path, monkeypatch):
    """A real ConfigManager on a throwaway config.yml — persistence included."""
    monkeypatch.chdir(tmp_path)
    manager = cm_module.ConfigManager(config_path=str(tmp_path / "config.yml"))
    manager._data["users"] = {
        OWNER.id: {"user_id": OWNER.id, "role": "user", "username": "owner"},
    }
    manager._data["user_preferences"] = {}
    manager._audit_log = []
    manager._save_config()
    for module in (cm_module, web_auth, auth_routes):
        monkeypatch.setattr(module, "get_config_manager", lambda: manager)
    return manager


def _enroll(body=None):
    return asyncio.run(enroll_2fa(body=body, user=OWNER))


def _confirm(code):
    return asyncio.run(confirm_2fa(Confirm2FARequest(code=code), user=OWNER))


def _disable(code):
    return asyncio.run(disable_2fa(Disable2FARequest(code=code), user=OWNER))


def _enable_2fa(cm):
    """Walk a fresh account through enroll -> confirm, the normal path."""
    result = _enroll()
    code = pyotp.TOTP(result["secret"]).now()
    _confirm(code)
    return result["secret"]


def expect_refusal(fn, status):
    with pytest.raises(HTTPException) as excinfo:
        fn()
    assert excinfo.value.status_code == status
    assert excinfo.value.detail
    return excinfo.value


def test_first_time_enroll_needs_no_code(cm):
    """Nobody has to prove possession of a factor that does not exist yet."""
    result = _enroll(body=None)
    assert result["secret"]
    assert len(result["recovery_codes"]) == 8
    assert cm.has_2fa_enabled(OWNER.id) is False


def test_confirming_the_real_code_turns_2fa_on(cm):
    secret = _enable_2fa(cm)
    assert cm.has_2fa_enabled(OWNER.id) is True
    assert secret


def test_re_enrolling_over_an_active_factor_without_a_code_is_refused(cm):
    """The bypass this module exists to close: a bare POST with just a
    session cookie must not be able to silently swap the secret."""
    _enable_2fa(cm)
    old_secret = cm.get_totp_secret(OWNER.id)

    expect_refusal(lambda: _enroll(body=None), 401)

    # Refused means refused — the old secret must still be the live one.
    assert cm.get_totp_secret(OWNER.id) == old_secret
    assert cm.has_2fa_enabled(OWNER.id) is True


def test_re_enrolling_with_a_wrong_code_is_also_refused(cm):
    _enable_2fa(cm)

    expect_refusal(lambda: _enroll(body=Enroll2FARequest(code="000000")), 401)
    assert cm.has_2fa_enabled(OWNER.id) is True


def test_re_enrolling_with_the_real_current_code_is_allowed(cm):
    """Proving possession first is exactly the `disable_2fa` pattern — and
    re-enrollment should work the same way, not be blocked outright."""
    old_secret = _enable_2fa(cm)
    current_code = pyotp.TOTP(old_secret).now()

    result = _enroll(body=Enroll2FARequest(code=current_code))

    assert result["secret"] != old_secret
    # Enrollment always leaves 2FA pending confirm, even when re-enrolling.
    assert cm.has_2fa_enabled(OWNER.id) is False


def test_disable_still_requires_a_real_code(cm):
    _enable_2fa(cm)
    expect_refusal(lambda: _disable("000000"), 401)
    assert cm.has_2fa_enabled(OWNER.id) is True


def test_disable_with_the_real_code_turns_it_off(cm):
    secret = _enable_2fa(cm)
    code = pyotp.TOTP(secret).now()
    _disable(code)
    assert cm.has_2fa_enabled(OWNER.id) is False
