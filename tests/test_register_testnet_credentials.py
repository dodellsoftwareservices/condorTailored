"""``scripts/register_testnet_credentials.py``: testnet keys from ``.env`` to the API.

The script moves a key pair between two places that must never show it to a third:
``.env`` and the Hummingbot API, with a terminal and a log between them. So what is
pinned here is mostly what it refuses to print, including when the server it is
talking to quotes the key back in an error, and what it reports and exits with, since
the shift runner uses ``--check`` as a gate.

No network and no real credentials: the API is a fake behind the script's own seam
(``connect``), the transport is exercised against a fake opener, and every key,
secret and password below is made up.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from condor.venues import is_test_venue

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "register_testnet_credentials.py"
)


def _load():
    # Registered in sys.modules before it runs: its dataclasses read their own module.
    name = "register_testnet_credentials_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


script = _load()

# Made up, and chosen so that no six-character stretch of one turns up in ordinary
# output by accident.
BN_KEY = "Zq9xKdW3mVt7RbL2pHy8NcA5sFgJ4eU1"
BN_SECRET = "Xv6TnB0wQjY3kMz8RdC5uHa9LpEs2GfI"
BY_KEY = "Hj4PwN7cQxT1vBm9KdRz3LsYa6EuFg0V"
BY_SECRET = "Cn8WbS2yUqD5tMk1JxAh7ReLp4ZoGf3I"
PASSWORD = "Wm3Qz8Lr5Vk1Dj7Pt9Xa"
SECRETS = [BN_KEY, BN_SECRET, BY_KEY, BY_SECRET, PASSWORD]

BN_ENV = f"BINANCE_EXCHANGE_API={BN_KEY}\nBINANCE_SECRET_KEY={BN_SECRET}\n"
BY_ENV = f"BYBIT_TESTNET_API_KEY={BY_KEY}\nBYBIT_TESTNET_API_SECRET={BY_SECRET}\n"

BN, BY = "binance_perpetual_testnet", "bybit_perpetual_testnet"
BALANCES = {
    BN: [
        {"token": "BTC", "units": 0.01, "price": 80000.0, "value": 800.0},
        {"token": "USDT", "units": 5000.0, "price": 1.0, "value": 5000.0},
    ],
    BY: [{"token": "USDT", "units": 1234.5, "price": 1.0, "value": 1234.5}],
}

CONFIG = f"""\
servers:
  local:
    host: localhost
    port: 8000
    username: tester
    password: {PASSWORD}
default_server: local
"""


def assert_no_secret(*streams: str) -> None:
    """Neither a value, nor the front or back of one, is anywhere in the output."""
    blob = "\n".join(streams)
    for secret in SECRETS:
        for piece in (secret, secret[:6], secret[-6:]):
            assert piece not in blob, f"{piece[:2]}... leaked"


class FakeApi:
    """The slice of the Hummingbot API this script talks to."""

    def __init__(
        self,
        registered=(),
        balances=None,
        register=None,
        credentials=None,
        state=None,
        unreachable=None,
    ):
        self.registered = set(registered)
        self.balances = BALANCES if balances is None else balances
        self._register = (
            register  # (status, body) or a callable(body) -> (status, body)
        )
        self._credentials = credentials  # (status, body) override
        self._state = state  # (status, body) override
        self._unreachable = unreachable
        self.calls: list[tuple[str, str, object]] = []

    @property
    def writes(self):
        return [c for c in self.calls if c[1].startswith("/accounts/add-credential/")]

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self._unreachable is not None:
            raise self._unreachable
        if (method, path) == ("GET", "/accounts/master_account/credentials"):
            return self._credentials or (200, sorted(self.registered))
        if path.startswith("/accounts/add-credential/master_account/"):
            outcome = self._register
            if callable(outcome):
                outcome = outcome(body)
            if outcome is None or outcome[0] in (200, 201):
                self.registered.add(path.rsplit("/", 1)[1])
                return 201, {"message": "Connector credentials added successfully."}
            return outcome
        if (method, path) == ("POST", "/portfolio/state"):
            if self._state:
                return self._state
            connector = body["connector_names"][0]
            tokens = self.balances.get(connector)
            if connector not in self.registered or tokens is None:
                return 200, {"master_account": {}}
            return 200, {"master_account": {connector: tokens}}
        raise AssertionError(f"unexpected request {method} {path}")


@pytest.fixture
def files(tmp_path):
    def write(env: str | None = BN_ENV, config: str | None = CONFIG):
        env_path, config_path = tmp_path / ".env", tmp_path / "config.yml"
        if env is not None:
            env_path.write_text(env)
        if config is not None:
            config_path.write_text(config)
        return env_path, config_path

    return write


@pytest.fixture
def run(files, capsys):
    """``run(argv, api, env=..., config=...) -> (exit code, stdout, stderr)``."""

    def go(argv, api, env=BN_ENV, config=CONFIG, servers=None):
        env_path, config_path = files(env, config)

        def connect(server):
            if servers is not None:
                servers.append(server)
            return api

        try:
            code = script.main(
                argv, env_path=env_path, config_path=config_path, connect=connect
            )
        except SystemExit as e:  # argparse
            code = e.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return go


# ── what gets registered ──


def test_each_venues_variables_fill_its_connectors_credential_fields(run):
    api = FakeApi()

    code, out, err = run([], api, env=BN_ENV + BY_ENV)

    assert code == 0, err
    assert api.writes == [
        (
            "POST",
            f"/accounts/add-credential/master_account/{BN}",
            {
                "binance_perpetual_testnet_api_key": BN_KEY,
                "binance_perpetual_testnet_api_secret": BN_SECRET,
            },
        ),
        (
            "POST",
            f"/accounts/add-credential/master_account/{BY}",
            {
                "bybit_perpetual_testnet_api_key": BY_KEY,
                "bybit_perpetual_testnet_secret_key": BY_SECRET,
            },
        ),
    ]
    assert "2 ready, 0 not configured, 0 failed" in out


def test_the_env_file_is_read_as_dotenv_reads_it(run):
    """Quotes, ``export`` and comments are dotenv's business, not ours to re-parse."""
    api = FakeApi()
    env = (
        "# testnet keys\n"
        f'export BINANCE_EXCHANGE_API="{BN_KEY}"\n'
        f"BINANCE_SECRET_KEY='{BN_SECRET}'\n"
        "OPENROUTER_API_KEY=sk-or-not-ours\n"
    )

    code, _, err = run([], api, env=env)

    assert code == 0, err
    assert api.writes[0][2] == {
        "binance_perpetual_testnet_api_key": BN_KEY,
        "binance_perpetual_testnet_api_secret": BN_SECRET,
    }


def test_a_successful_registration_is_verified_by_reading_balances_back(run):
    api = FakeApi()

    code, out, err = run([], api)

    assert code == 0, err
    assert [c[:2] for c in api.calls] == [
        ("GET", "/accounts/master_account/credentials"),
        ("POST", f"/accounts/add-credential/master_account/{BN}"),
        ("POST", "/portfolio/state"),
    ]
    assert api.calls[-1][2] == {
        "account_names": ["master_account"],
        "connector_names": [BN],
        "refresh": True,
    }
    assert "  action      registered" in out
    assert "reachable, 2 token(s)" in out
    assert "BTC" in out and "0.01" in out
    assert "USDT" in out and "5000" in out
    assert_no_secret(out, err)


def test_only_the_venue_asked_for_is_touched(run):
    api = FakeApi()

    code, out, _ = run(["--venue", "bybit"], api, env=BN_ENV + BY_ENV)

    assert code == 0
    assert [w[1].rsplit("/", 1)[1] for w in api.writes] == [BY]
    assert "binance" not in out


# ── not configured, and half configured ──


def test_a_venue_with_no_variables_is_not_configured_and_not_an_error(run):
    api = FakeApi()

    code, out, err = run([], api, env=BN_ENV)

    assert code == 0, err
    assert [w[1].rsplit("/", 1)[1] for w in api.writes] == [BN]
    assert "bybit (bybit_perpetual_testnet)" in out
    assert "not configured (set BYBIT_TESTNET_API_KEY, BYBIT_TESTNET_API_SECRET" in out
    assert "1 ready, 1 not configured, 0 failed" in out
    assert err == ""


def test_no_env_file_means_nothing_is_configured_and_nothing_is_written(run, files):
    api = FakeApi()

    code, out, _ = run([], api, env=None)

    assert code == 0
    assert api.writes == []
    assert "not found, so no venue has any variable set" in out
    assert "0 ready, 2 not configured, 0 failed" in out


def test_blank_values_count_as_not_set(run):
    api = FakeApi()

    code, out, _ = run([], api, env="BINANCE_EXCHANGE_API=\nBINANCE_SECRET_KEY=   \n")

    assert code == 0
    assert api.writes == []
    assert "0 ready, 2 not configured, 0 failed" in out


def test_half_a_key_pair_is_an_error_that_names_the_missing_variable(run):
    api = FakeApi()

    code, out, err = run([], api, env=f"BINANCE_EXCHANGE_API={BN_KEY}\n")

    assert code == 1
    assert api.writes == []
    assert "incomplete: BINANCE_SECRET_KEY not set" in out
    assert "FAILED binance: .env sets only part of the key pair" in err
    assert_no_secret(out, err)


# ── already registered, and --force ──


def test_a_registered_venue_is_skipped_with_a_message_but_still_verified(run):
    api = FakeApi(registered=[BN])

    code, out, err = run([], api)

    assert code == 0, err
    assert api.writes == []
    assert "already registered, skipped (--force to overwrite)" in out
    assert "reachable, 2 token(s)" in out
    assert ("POST", "/portfolio/state") in [c[:2] for c in api.calls]


def test_force_registers_a_venue_the_api_already_holds(run):
    api = FakeApi(registered=[BN])

    code, out, err = run(["--force"], api)

    assert code == 0, err
    assert [w[1].rsplit("/", 1)[1] for w in api.writes] == [BN]
    assert "re-registered" in out
    assert "skipped" not in out


def test_the_credentials_listing_may_carry_the_yml_suffix(run):
    api = FakeApi(registered=[BN], credentials=(200, [f"{BN}.yml"]))

    code, out, _ = run([], api)

    assert code == 0
    assert api.writes == []
    assert "already registered" in out


# ── --check ──


def test_check_changes_nothing_and_reports_all_three_facts(run):
    api = FakeApi(registered=[BN])

    code, out, err = run(["--check"], api, env=BN_ENV + BY_ENV)

    assert api.writes == []
    # Reads only: the listing, and balances for the venue that is registered.
    assert {c[:2] for c in api.calls} <= {
        ("GET", "/accounts/master_account/credentials"),
        ("POST", "/portfolio/state"),
    }
    assert "check, changes nothing" in out
    assert "env         present (BINANCE_EXCHANGE_API, BINANCE_SECRET_KEY)" in out
    assert "registered  yes" in out
    assert "balances    reachable, 2 token(s)" in out
    # bybit has its variables but was never registered: that is the finding.
    assert "registered  no" in out
    assert "balances    n/a (not registered)" in out
    assert code == 1
    assert "FAILED bybit: has a key pair in .env but is not registered" in err
    assert_no_secret(out, err)


def test_check_passes_when_every_configured_venue_is_registered_and_readable(run):
    api = FakeApi(registered=[BN])

    code, out, err = run(["--check"], api)

    assert code == 0, err
    assert api.writes == []
    assert "1 ready, 1 not configured, 0 failed" in out


def test_check_fails_a_registered_venue_whose_balances_cannot_be_read(run):
    api = FakeApi(registered=[BN], state=(500, {"detail": "connector error"}))

    code, out, err = run(["--check"], api)

    assert code == 1
    assert api.writes == []
    assert "balances    FAILED: HTTP 500: connector error" in out
    assert "FAILED binance: balances not reachable" in err


def test_check_only_informs_about_a_venue_that_has_no_variables(run):
    """Registered some other way and unreadable: said, but not an error. It is not
    configured here, so this script has no standing to fail it."""
    api = FakeApi(registered=[BY], state=(500, {"detail": "nope"}))

    code, out, err = run(["--check", "--venue", "bybit"], api, env="")

    assert code == 0, err
    assert "not configured" in out
    assert "registered  yes" in out
    assert "balances    FAILED: HTTP 500: nope" in out
    assert "0 ready, 1 not configured, 0 failed" in out


def test_check_and_force_contradict_each_other(run):
    api = FakeApi()

    code, _, err = run(["--check", "--force"], api)

    assert code == 2
    assert "not allowed with" in err
    assert api.calls == []


def test_an_unknown_venue_is_a_usage_error(run):
    code, _, err = run(["--venue", "kraken"], FakeApi())

    assert code == 2
    assert "invalid choice" in err


# ── failures and their exit codes ──


def test_a_registration_the_api_refuses_exits_nonzero(run):
    api = FakeApi(register=(400, {"detail": "invalid API key"}))

    code, out, err = run([], api)

    assert code == 1
    assert "action      FAILED to register: HTTP 400: invalid API key" in out
    assert "FAILED binance: registration failed" in err
    assert "1 ready" not in out and "0 ready, 1 not configured, 1 failed" in out
    # Not verified: there is nothing to verify.
    assert ("POST", "/portfolio/state") not in [c[:2] for c in api.calls]


@pytest.mark.parametrize(
    "state",
    [
        (500, {"detail": "exchange unreachable"}),
        (200, {}),
        (200, {"master_account": {}}),
        (200, {"master_account": {BN: []}}),
        (200, "not json"),
    ],
)
def test_a_registration_that_cannot_be_verified_exits_nonzero(run, state):
    api = FakeApi(state=state)

    code, out, err = run([], api)

    assert code == 1
    assert "action      registered" in out
    assert "balances    FAILED" in out
    assert "FAILED binance: balances not reachable" in err


def test_an_unreachable_api_fails_a_configured_venue(run):
    api = FakeApi(unreachable=script.ApiUnreachable("ConnectionRefusedError: refused"))

    code, out, err = run([], api)

    assert code == 1
    assert "the API's credentials list failed: ConnectionRefusedError: refused" in err
    assert "registered  unknown" in out


def test_an_unreachable_api_does_not_fail_a_venue_nobody_configured(run):
    api = FakeApi(unreachable=script.ApiUnreachable("ConnectionRefusedError: refused"))

    code, out, _ = run([], api, env="")

    assert code == 0
    assert "0 ready, 2 not configured, 0 failed" in out


def test_one_venue_failing_does_not_stop_the_next_and_still_fails_the_run(run):
    def register(body):
        return (
            (400, {"detail": "bad"})
            if "binance_perpetual_testnet_api_key" in body
            else None
        )

    api = FakeApi(register=register)

    code, out, err = run([], api, env=BN_ENV + BY_ENV)

    assert code == 1
    assert [w[1].rsplit("/", 1)[1] for w in api.writes] == [BN, BY]
    assert "1 ready, 0 not configured, 1 failed" in out
    assert "FAILED binance" in err and "FAILED bybit" not in err


# ── the key and secret never reach the terminal ──


def _echo(style):
    """The request body, quoted back to the caller the way servers do."""

    def register(body):
        key, secret = list(body.values())
        if style == "plain":
            return 400, {"detail": f"cannot validate key {key} with secret {secret}"}
        if style == "validation-error":
            return 422, {
                "detail": [{"type": "bad", "loc": ["body"], "msg": "no", "input": body}]
            }
        if style == "text":
            return 400, f"<html>Bad request: {key} {secret}</html>"
        if style == "url-encoded":
            quoted = urllib.parse.quote(secret, safe="")
            return 400, {"detail": f"GET /sign?key={key}&secret={quoted}"}
        if style == "past-the-clip":
            # A secret that begins just inside the clip would otherwise leave its front half.
            return 400, {"detail": "x" * (script.MAX_DETAIL - 10) + key + secret}
        raise AssertionError(style)

    return register


@pytest.mark.parametrize(
    "style", ["plain", "validation-error", "text", "url-encoded", "past-the-clip"]
)
def test_a_server_that_quotes_the_key_back_does_not_get_it_printed(run, style):
    api = FakeApi(register=_echo(style))

    code, out, err = run([], api)

    assert code == 1
    assert "FAILED to register" in out
    assert "***" in out or style == "past-the-clip"
    assert_no_secret(out, err)


def test_a_secret_is_scrubbed_in_every_shape_a_server_can_quote_it():
    """As written, JSON-escaped, URL-encoded: base64 secrets carry ``/ + =``, and a
    JSON body escapes what a quote or backslash in a value would break."""
    secret = 'a/B+c=D"e\\fég'
    scrub = script.Scrubber([secret])

    for shape in (
        secret,
        script.json.dumps(secret)[1:-1],
        urllib.parse.quote(secret, safe=""),
        urllib.parse.quote_plus(secret),
    ):
        assert scrub(f"before {shape} after") == "before *** after"
    assert scrub("nothing to hide") == "nothing to hide"
    assert script.Scrubber([""])("empty secrets match nothing") == (
        "empty secrets match nothing"
    )


def test_the_api_password_is_scrubbed_from_what_the_server_says(run):
    api = FakeApi(credentials=(401, {"detail": f"bad login tester:{PASSWORD}"}))

    code, out, err = run([], api)

    assert code == 1
    assert "HTTP 401" in err
    assert_no_secret(out, err)


def test_an_unexpected_error_prints_no_traceback_and_no_secret(run):
    class Boom(FakeApi):
        def request(self, method, path, body=None):
            if path == "/portfolio/state":
                raise RuntimeError(f"exploded holding {BN_KEY} and {BN_SECRET}")
            return super().request(method, path, body)

    code, out, err = run([], Boom())

    assert code == 1
    assert "error: unexpected RuntimeError: exploded holding *** and ***" in err
    assert "Traceback" not in out + err
    assert_no_secret(out, err)


def test_an_unreachable_error_that_quotes_the_url_is_scrubbed(run):
    api = FakeApi(
        unreachable=script.ApiUnreachable(f"URLError: http://tester:{PASSWORD}@host")
    )

    code, out, err = run([], api)

    assert code == 1
    assert_no_secret(out, err)


def test_a_config_error_never_quotes_the_file(run):
    """A YAML error carries a snippet of the offending line; here that line is a
    password."""
    broken = f"servers:\n  local:\n    password: [{PASSWORD}\n"

    code, out, err = run([], FakeApi(), config=broken)

    assert code == 2
    assert "cannot read config.yml (ParserError)" in err
    assert_no_secret(out, err)


# ── the server comes from config.yml ──


def test_the_server_is_the_default_one_in_config_yml(run):
    servers = []
    config = f"""\
servers:
  remote:
    host: 10.0.0.9
    port: 9000
    username: other
    password: not-this-one
  local:
    host: localhost
    port: 8000
    username: tester
    password: {PASSWORD}
default_server: local
"""

    code, out, _ = run([], FakeApi(), config=config, servers=servers)

    assert code == 0
    assert servers == [
        script.Server("local", "http://localhost:8000", "tester", PASSWORD)
    ]
    assert "account master_account on local (http://localhost:8000)" in out


def test_with_no_default_server_the_first_one_is_used(run):
    servers = []
    config = CONFIG.replace("default_server: local\n", "default_server: gone\n")

    run([], FakeApi(), config=config, servers=servers)

    assert servers[0].name == "local"


@pytest.mark.parametrize(
    "config,complaint",
    [
        (None, "cannot read config.yml (FileNotFoundError)"),
        ("servers: {}\n", "lists no servers"),
        ("default_server: x\n", "lists no servers"),
        (
            "servers:\n  local:\n    host: localhost\n    port: 8000\n",
            "server 'local' in config.yml needs host, port, username, password",
        ),
    ],
)
def test_a_config_with_no_usable_server_is_exit_2(run, config, complaint):
    api = FakeApi()

    code, _, err = run([], api, config=config)

    assert code == 2
    assert complaint in err
    assert api.calls == []


# ── the transport ──


class _Response:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _http(result):
    api = script.HttpApi("http://api:8000/", "tester", PASSWORD, timeout=7)
    api._opener = _Opener(result)
    return api


def test_the_transport_sends_basic_auth_and_a_json_body():
    api = _http(_Response(201, b'{"message": "ok"}'))

    status, body = api.request("POST", "/x/y", {"a": 1})

    request, timeout = api._opener.requests[0]
    assert (status, body) == (201, {"message": "ok"})
    assert request.full_url == "http://api:8000/x/y"
    assert request.get_method() == "POST"
    assert request.data == b'{"a": 1}'
    assert request.get_header("Authorization") == (
        "Basic " + script.base64.b64encode(f"tester:{PASSWORD}".encode()).decode()
    )
    assert request.get_header("Content-type") == "application/json"
    assert timeout == 7


def test_the_transport_sends_no_body_on_a_get():
    api = _http(_Response(200, b'["a", "b"]'))

    assert api.request("GET", "/x") == (200, ["a", "b"])
    assert api._opener.requests[0][0].data is None


def test_an_error_status_is_an_answer_not_an_exception():
    error = urllib.error.HTTPError(
        "http://api:8000/x", 400, "Bad Request", {}, io.BytesIO(b'{"detail": "no"}')
    )

    assert _http(error).request("POST", "/x", {}) == (400, {"detail": "no"})


def test_a_body_that_is_not_json_comes_back_as_text():
    assert _http(_Response(200, b"<html>hi</html>")).request("GET", "/x") == (
        200,
        "<html>hi</html>",
    )


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("refused"),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
    ],
)
def test_no_answer_at_all_is_unreachable(failure):
    with pytest.raises(script.ApiUnreachable, match=type(failure).__name__):
        _http(failure).request("GET", "/x")


def test_the_transport_does_not_go_through_a_proxy(monkeypatch):
    """The aiohttp client Condor uses for the same server ignores the environment's
    proxy, and a proxy that cannot see localhost would turn "refused" into a 502."""
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    proxy = script.urllib.request.ProxyHandler

    default = script.urllib.request.build_opener()
    ours = script.HttpApi("http://localhost:8000", "u", "p")._opener

    assert any(isinstance(h, proxy) for h in default.handlers)  # the control
    assert not any(isinstance(h, proxy) for h in ours.handlers)


# ── the table ──


def test_every_venue_is_a_testnet_with_its_own_variables_and_fields():
    """The script exists to register test keys, so a row for a mainnet connector is a
    mistake this will not let through."""
    venues = script.VENUES

    assert len({v.name for v in venues}) == len(venues)
    assert len({v.connector for v in venues}) == len(venues)
    variables = [var for v in venues for var, _ in v.fields]
    assert len(set(variables)) == len(variables)
    for v in venues:
        assert is_test_venue(v.connector), v.connector
        assert v.fields
        # Hummingbot names a connector's credential fields after the connector.
        assert all(field.startswith(v.connector) for _, field in v.fields), v.name


def test_the_script_runs_as_a_program():
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True
    )

    assert done.returncode == 0
    assert "--check" in done.stdout and "--venue" in done.stdout
