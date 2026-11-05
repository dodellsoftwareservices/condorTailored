#!/usr/bin/env python3
"""Register exchange testnet credentials with the Hummingbot API, from condor/.env.

    uv run python scripts/register_testnet_credentials.py [--check] [--force] [--venue binance|bybit|all]

Each venue in :data:`VENUES` names the ``.env`` variables that hold its testnet key
pair and the connector they belong to. A venue whose variables are all present and
that the API does not already hold is registered, then verified by reading the
account's balances back (``POST /portfolio/state``) and printing token and units. A
key the exchange refuses never gets that far: the API validates a credential by
initialising the connector with it, and rolls the write back when that fails.

- A venue with no variables set is "not configured": reported, never an error.
- A venue with only some of its variables set is an error, as is one that has them all
  and fails to register or to verify. ``--check`` holds a venue to the same standard
  without writing anything, so it doubles as a pre-shift gate.
- ``--force`` registers a venue the API already holds (the API updates in place and
  restores the old file if the new key is refused).

Exit status: 0 when nothing that is configured failed, 1 when something did, 2 when
the script could not start (bad arguments, no usable server in ``config.yml``).

The key and secret never reach the terminal. Nothing here prints a variable's value or
any part of one, and the values (as written, JSON-escaped and URL-encoded) are scrubbed
out of whatever the server says before the script looks at it, and again out of every
line it prints, as is the API password: a validation error that echoes its input must
not be how a key gets into a log. Scrubbing comes first so that clipping a long error
afterwards can never leave the front half of a secret behind.

Standard library, plus python-dotenv and PyYAML, which Condor already depends on.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent
ENV_PATH = REPO / ".env"
CONFIG_PATH = REPO / "config.yml"

#: The Hummingbot API account Condor trades from.
ACCOUNT = "master_account"

#: Longest stretch of a server's error body that is echoed.
MAX_DETAIL = 300


@dataclass(frozen=True)
class Venue:
    """One testnet: its connector, and which variable fills which credential field."""

    #: The ``--venue`` value.
    name: str
    #: The connector the credentials are registered under.
    connector: str
    #: ``(.env variable, credential field the connector asks for)``, in order.
    fields: tuple[tuple[str, str], ...]


#: To add a venue, add a row; the fields are what ``GET /connectors/{connector}/config-map``
#: lists for its connector. Every connector here must be a test venue, which
#: ``tests/test_register_testnet_credentials.py`` checks.
VENUES = (
    Venue(
        name="binance",
        connector="binance_perpetual_testnet",
        fields=(
            ("BINANCE_EXCHANGE_API", "binance_perpetual_testnet_api_key"),
            ("BINANCE_SECRET_KEY", "binance_perpetual_testnet_api_secret"),
        ),
    ),
    Venue(
        name="bybit",
        connector="bybit_perpetual_testnet",
        fields=(
            ("BYBIT_TESTNET_API_KEY", "bybit_perpetual_testnet_api_key"),
            ("BYBIT_TESTNET_API_SECRET", "bybit_perpetual_testnet_secret_key"),
        ),
    ),
)


# ── keeping secrets out of the output ──


class Scrubber:
    """Replaces every known secret in a string with ``***``.

    Each value is matched as written, JSON-escaped and URL-encoded, which is every
    shape one takes when a server quotes it back. Longest first, so a secret that
    contains another is replaced whole.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._forms: list[str] = []
        self.add(secrets)

    def add(self, secrets: Iterable[str]) -> None:
        forms = set(self._forms)
        for secret in secrets:
            if secret:
                forms.update(
                    {
                        secret,
                        json.dumps(secret)[1:-1],
                        urllib.parse.quote(secret, safe=""),
                        urllib.parse.quote_plus(secret),
                    }
                )
        self._forms = sorted((f for f in forms if f), key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for form in self._forms:
            text = text.replace(form, "***")
        return text

    def deep(self, value: Any) -> Any:
        """``value`` with every string in it, keys included, scrubbed."""
        if isinstance(value, str):
            return self(value)
        if isinstance(value, list):
            return [self.deep(v) for v in value]
        if isinstance(value, dict):
            return {self.deep(k): self.deep(v) for k, v in value.items()}
        return value


# ── the server ──


class SetupError(Exception):
    """The script cannot run at all."""


class ApiError(Exception):
    """The API answered, and the answer was a refusal."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"HTTP {status}: {_detail(body)}")


class ApiUnreachable(Exception):
    """The API did not answer."""


@dataclass(frozen=True)
class Server:
    name: str
    base_url: str
    username: str
    password: str


def load_server(config_path: Path) -> Server:
    """The default server of ``config.yml``, the one Condor itself talks to.

    Errors name what is wrong and never quote the file: a YAML error carries a
    snippet of the line it choked on, and the lines here hold a password.
    """
    try:
        config = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise SetupError(
            f"cannot read {config_path.name} ({type(e).__name__})"
        ) from None

    servers = config.get("servers") if isinstance(config, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise SetupError(f"{config_path.name} lists no servers")
    name = config.get("default_server")
    if name not in servers:
        name = next(iter(servers))
    entry = servers[name]
    needed = ("host", "port", "username", "password")
    if not isinstance(entry, dict) or any(entry.get(k) in (None, "") for k in needed):
        raise SetupError(
            f"server '{name}' in {config_path.name} needs {', '.join(needed)}"
        )
    return Server(
        name=str(name),
        base_url=f"http://{entry['host']}:{entry['port']}",
        username=str(entry["username"]),
        password=str(entry["password"]),
    )


class HttpApi:
    """The Hummingbot API over HTTP basic auth.

    ``request`` returns ``(status, body)`` for any answer, an error status included,
    and raises :class:`ApiUnreachable` only when there was none. No proxy is used,
    matching the aiohttp client Condor reaches the same server with.
    """

    def __init__(
        self, base_url: str, username: str, password: str, timeout: float = 60.0
    ) -> None:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def for_server(cls, server: Server) -> HttpApi:
        return cls(server.base_url, server.username, server.password)

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self._base_url + path, data=data, method=method, headers=self._headers
        )
        try:
            with self._opener.open(req, timeout=self._timeout) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as e:
            return e.code, _decode(e.read())
        except (OSError, http.client.HTTPException) as e:
            raise ApiUnreachable(f"{type(e).__name__}: {e}") from None


class _Scrubbed:
    """An API whose answers have been through the scrubber before anyone reads them."""

    def __init__(self, api: Any, scrub: Scrubber) -> None:
        self._api = api
        self._scrub = scrub

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        try:
            status, payload = self._api.request(method, path, body)
        except ApiUnreachable as e:
            raise ApiUnreachable(self._scrub(str(e))) from None
        return status, self._scrub.deep(payload)


def _decode(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _detail(body: Any) -> str:
    """The part of an error body worth reading: FastAPI's ``detail``, else the body."""
    if isinstance(body, dict) and "detail" in body:
        body = body["detail"]
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    text = " ".join(text.split())
    return text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 3] + "..."


# ── the work ──


def _registered(api: Any) -> set[str]:
    status, body = api.request("GET", f"/accounts/{ACCOUNT}/credentials")
    if status != 200 or not isinstance(body, list):
        raise ApiError(status, body)
    return {name.removesuffix(".yml") for name in map(str, body)}


def _register(api: Any, venue: Venue, env: Mapping[str, str]) -> None:
    credentials = {field: env[var] for var, field in venue.fields}
    status, body = api.request(
        "POST", f"/accounts/add-credential/{ACCOUNT}/{venue.connector}", credentials
    )
    if status not in (200, 201):
        raise ApiError(status, body)


def _balances(api: Any, connector: str) -> list[tuple[str, float]]:
    """The account's balances on ``connector``, read from the exchange.

    ``refresh`` makes the API ask the exchange rather than answer from its last
    snapshot, which is the point: this is the proof the key works. It reads; it writes
    nothing. An answer with no tokens in it counts as a failure: a key that sees an
    empty wallet has nothing to trade with and nothing to print as proof.
    """
    status, body = api.request(
        "POST",
        "/portfolio/state",
        {"account_names": [ACCOUNT], "connector_names": [connector], "refresh": True},
    )
    if status != 200:
        raise ApiError(status, body)
    tokens = (
        (body.get(ACCOUNT) or {}).get(connector) if isinstance(body, dict) else None
    )
    rows = [
        (str(t.get("token")), float(t.get("units") or 0))
        for t in tokens or []
        if isinstance(t, dict)
    ]
    if not rows:
        raise ApiError(200, f"no balances came back for {connector}")
    return rows


def _verify(api: Any, venue: Venue, out: Callable[[str], None]) -> bool:
    """Read the balances back and print token and units. False when they cannot be."""
    try:
        rows = _balances(api, venue.connector)
    except (ApiError, ApiUnreachable) as e:
        out(f"  balances    FAILED: {e}")
        return False
    out(f"  balances    reachable, {len(rows)} token(s)")
    for token, units in rows:
        out(f"              {token:<10} {units:.8g}")
    return True


def _handle(
    venue: Venue,
    env: Mapping[str, str],
    registered: set[str] | None,
    registry_error: str,
    api: Any,
    args: argparse.Namespace,
    out: Callable[[str], None],
) -> tuple[str, str]:
    """One venue. Returns ``(outcome, why)``: ready, unconfigured, or failed and why."""
    present = [var for var, _ in venue.fields if env.get(var)]
    missing = [var for var, _ in venue.fields if not env.get(var)]
    out(f"{venue.name} ({venue.connector})")

    if not present:
        out(f"  env         not configured (set {', '.join(missing)} in .env)")
    elif missing:
        out(f"  env         incomplete: {', '.join(missing)} not set")
        return "failed", f"{venue.name}: .env sets only part of the key pair"
    else:
        out(f"  env         present ({', '.join(present)})")

    if registered is None:
        out("  registered  unknown")
        out("  balances    n/a")
        if present:
            return "failed", f"{venue.name}: {registry_error}"
        return "unconfigured", ""
    is_registered = venue.connector in registered
    out(f"  registered  {'yes' if is_registered else 'no'}")

    if not present:
        # Not ours to register, but a venue that was registered some other way can
        # still be looked at when asked to check. What it says is information only.
        if args.check and is_registered:
            _verify(api, venue, out)
        elif not is_registered:
            out("  balances    n/a (not registered)")
        else:
            out("  balances    n/a (not configured)")
        return "unconfigured", ""

    if args.check:
        if not is_registered:
            out("  balances    n/a (not registered)")
            return (
                "failed",
                f"{venue.name}: has a key pair in .env but is not registered",
            )
    elif is_registered and not args.force:
        out("  action      already registered, skipped (--force to overwrite)")
    else:
        try:
            _register(api, venue, env)
        except (ApiError, ApiUnreachable) as e:
            out(f"  action      FAILED to register: {e}")
            return "failed", f"{venue.name}: registration failed"
        out("  action      " + ("re-registered" if is_registered else "registered"))

    if not _verify(api, venue, out):
        return "failed", f"{venue.name}: balances not reachable"
    return "ready", ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="register_testnet_credentials.py",
        description=(
            "Register testnet API credentials from condor/.env with the Hummingbot API."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="change nothing; report env, registration and balances per venue",
    )
    mode.add_argument(
        "--force",
        action="store_true",
        help="register a venue the API already holds",
    )
    parser.add_argument(
        "--venue",
        choices=[v.name for v in VENUES] + ["all"],
        default="all",
        help="which venue to handle (default: all)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env_path: Path = ENV_PATH,
    config_path: Path = CONFIG_PATH,
    connect: Callable[[Server], Any] = HttpApi.for_server,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    env: dict[str, str] = {}
    if env_path.is_file():
        env = {
            k: v.strip()
            for k, v in dotenv_values(env_path, interpolate=False).items()
            if isinstance(v, str) and v.strip()
        }

    # Everything printed goes through here, and the secrets are known before anything
    # is printed, so no line, however it was made, is written unscrubbed.
    scrub = Scrubber(env[var] for v in VENUES for var, _ in v.fields if var in env)

    def out(line: str) -> None:
        print(scrub(line), file=stdout)

    def err(line: str) -> None:
        print(scrub(line), file=stderr)

    try:
        server = load_server(config_path)
    except SetupError as e:
        err(f"error: {e}")
        return 2
    scrub.add(
        [
            server.password,
            base64.b64encode(f"{server.username}:{server.password}".encode()).decode(),
        ]
    )

    try:
        api = _Scrubbed(connect(server), scrub)
        return _run(args, env, server, api, env_path, out, err)
    except Exception as e:  # noqa: BLE001 - no traceback: it could quote a secret
        err(f"error: unexpected {type(e).__name__}: {e}")
        return 1


def _run(
    args: argparse.Namespace,
    env: Mapping[str, str],
    server: Server,
    api: Any,
    env_path: Path,
    out: Callable[[str], None],
    err: Callable[[str], None],
) -> int:
    venues = [v for v in VENUES if args.venue in ("all", v.name)]
    out(
        f"{'check, changes nothing' if args.check else 'register'}: "
        f"account {ACCOUNT} on {server.name} ({server.base_url})"
    )
    if not env_path.is_file():
        out(f"note: {env_path} not found, so no venue has any variable set")

    registry_error = ""
    try:
        registered: set[str] | None = _registered(api)
    except (ApiError, ApiUnreachable) as e:
        registered, registry_error = None, f"the API's credentials list failed: {e}"

    outcomes: list[str] = []
    failures: list[str] = []
    for venue in venues:
        outcome, why = _handle(venue, env, registered, registry_error, api, args, out)
        outcomes.append(outcome)
        if why:
            failures.append(why)

    out(
        f"{outcomes.count('ready')} ready, {outcomes.count('unconfigured')} not "
        f"configured, {outcomes.count('failed')} failed"
    )
    for why in failures:
        err(f"FAILED {why}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
