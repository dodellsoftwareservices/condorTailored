"""JWT revocation on logout (system review 2026-10-31, finding #7).

Before this, `/auth/logout` only cleared the browser's cookie — the JWT
itself stayed valid at the server for the rest of its 24h life, so a copy
taken before logout (a stolen cookie, a shared machine) kept authenticating
regardless of the real owner logging out. These pin that the exact token a
session held is dead immediately once that session logs out, while every
other still-valid token (a different login, a loopback service token) is
unaffected.
"""

import pytest

from condor.web import auth as web_auth


def test_a_fresh_token_decodes_normally():
    token = web_auth.create_jwt(user_id=1, username="u")
    assert web_auth.decode_jwt(token) is not None


def test_revoking_a_token_makes_it_decode_to_none():
    token = web_auth.create_jwt(user_id=1, username="u")
    web_auth.revoke_jwt(token)
    assert web_auth.decode_jwt(token) is None


def test_revoking_one_token_does_not_touch_a_different_one():
    """Two logins (or a login plus a loopback service token) must not share
    fate — revoking one session's token cannot silently kill another's."""
    token_a = web_auth.create_jwt(user_id=1, username="u")
    token_b = web_auth.create_jwt(user_id=1, username="u")

    web_auth.revoke_jwt(token_a)

    assert web_auth.decode_jwt(token_a) is None
    assert web_auth.decode_jwt(token_b) is not None


def test_revoking_garbage_is_a_harmless_no_op():
    web_auth.revoke_jwt("not-a-real-token")  # must not raise


def test_revoking_twice_is_also_harmless():
    token = web_auth.create_jwt(user_id=1, username="u")
    web_auth.revoke_jwt(token)
    web_auth.revoke_jwt(token)  # must not raise
    assert web_auth.decode_jwt(token) is None


def test_each_token_gets_its_own_jti():
    token_a = web_auth.create_jwt(user_id=1)
    token_b = web_auth.create_jwt(user_id=1)
    assert web_auth.decode_jwt(token_a)["jti"] != web_auth.decode_jwt(token_b)["jti"]
