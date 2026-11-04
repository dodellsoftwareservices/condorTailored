#!/usr/bin/env python3
"""Inspect or import US-oriented venue connections without printing secrets.

Run from ``condor/``::

    uv run python scripts/us_venues.py status
    uv run python scripts/us_venues.py status --check-data
    uv run python scripts/us_venues.py import --venue lighter_perpetual
    uv run python scripts/us_venues.py import --venue alpaca_paper

Status is read-only. Import moves Hummingbot connector credentials from
``condor/.env`` into Hummingbot's encrypted store. Alpaca is different: its
paper-only client reads ``.env`` directly, so its import only validates an
account read. Nothing in this script places an order or transfers funds.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

from register_testnet_credentials import (  # noqa: E402
    ApiError,
    ApiUnreachable,
    HttpApi,
    Scrubber,
    Server,
    SetupError,
    _detail,
    _Scrubbed,
    load_server,
)

from condor.us_venues import US_VENUES, VENUES_BY_ID, Venue, venue_ids  # noqa: E402

ENV_PATH = REPO / ".env"
CONFIG_PATH = REPO / "config.yml"
ACCOUNT = "master_account"
MAX_BOOK_AGE_SECONDS = 30.0


def _env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return {
        key: value.strip()
        for key, value in dotenv_values(path, interpolate=False).items()
        if isinstance(value, str) and value.strip()
    }


def _missing(venue: Venue, env: Mapping[str, str]) -> list[str]:
    return [line.variable for line in venue.credentials if not env.get(line.variable)]


def _all_env_values(env: Mapping[str, str]) -> list[str]:
    """Scrub credential-shaped env values without mangling ordinary status words."""
    sensitive_fragments = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PRIVATE", "ADDRESS")
    return [
        value
        for key, value in env.items()
        if value and any(fragment in key.upper() for fragment in sensitive_fragments)
    ]


def _direct_public_book(url: str) -> Any:
    """Fetch a catalog-pinned keyless URL; no caller-supplied host or credentials."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "api.coinbase.com":
        raise ValueError("unapproved direct public-data host")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        if response.status != 200:
            raise ValueError(f"direct public feed returned HTTP {response.status}")
        return json.loads(response.read())


def _book_summary(payload: Any, *, now: float, max_age: float) -> dict[str, Any]:
    """Validate an order book and return only non-sensitive freshness/depth facts."""
    if not isinstance(payload, dict):
        raise ValueError("response was not an object")
    bids, asks = payload.get("bids"), payload.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        raise ValueError("response had no two-sided book")

    def price(level: Any) -> float:
        if isinstance(level, dict):
            value = level.get("price", level.get("Price"))
        elif isinstance(level, (list, tuple)) and level:
            value = level[0]
        else:
            value = None
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError("book contained an invalid price")
        return result

    bid, ask = price(bids[0]), price(asks[0])
    if bid >= ask:
        raise ValueError("best bid was not below best ask")
    raw_timestamp = payload.get("timestamp", payload.get("time"))
    if raw_timestamp is None:
        raise ValueError("book had no exchange timestamp")
    if isinstance(raw_timestamp, str) and not raw_timestamp.replace(".", "").isdigit():
        timestamp = datetime.fromisoformat(
            raw_timestamp.replace("Z", "+00:00")
        ).timestamp()
    else:
        timestamp = float(raw_timestamp)
    if timestamp > 10_000_000_000:  # milliseconds rather than seconds
        timestamp /= 1000
    age = max(0.0, now - timestamp)
    if age > max_age:
        raise ValueError(f"book was stale ({age:.1f}s old)")
    if timestamp > now + 5:
        raise ValueError("book timestamp was in the future")
    return {
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "age_seconds": round(age, 3),
    }


def _probe(api: Any, venue: Venue) -> dict[str, Any]:
    if venue.public_book_url:
        payload = _direct_public_book(venue.public_book_url)
        book = payload.get("pricebook") if isinstance(payload, dict) else None
        return _book_summary(book, now=time.time(), max_age=MAX_BOOK_AGE_SECONDS)
    if not venue.connector:
        raise ValueError("this venue uses the separate Alpaca data client")
    data_connector = (
        venue.data_connectors[0] if venue.data_connectors else venue.connector
    )
    data_venue = (
        VENUES_BY_ID[data_connector] if data_connector in VENUES_BY_ID else venue
    )
    if not data_venue.probe_pair:
        raise ValueError(f"no public-data probe is configured for {data_connector}")
    status, body = api.request(
        "POST",
        "/market-data/order-book",
        {
            "connector_name": data_connector,
            "trading_pair": data_venue.probe_pair,
            "depth": 5,
        },
    )
    if status != 200:
        raise ApiError(status, body)
    return _book_summary(body, now=time.time(), max_age=MAX_BOOK_AGE_SECONDS)


def _registered(api: Any) -> set[str]:
    status, body = api.request("GET", f"/accounts/{ACCOUNT}/credentials")
    if status != 200 or not isinstance(body, list):
        raise ApiError(status, body)
    return {str(name).removesuffix(".yml") for name in body}


def _print_venue(
    venue: Venue, env: Mapping[str, str], out: Callable[[str], None]
) -> None:
    missing = _missing(venue, env)
    out(f"\n{venue.id}: {venue.label}")
    out(f"  assets      {venue.assets}")
    out(f"  storage     {venue.storage}")
    if venue.connector:
        out(f"  connector   {venue.connector}")
    out(f"  data        {venue.data_note}")
    out(f"  simulation  {venue.simulation_note}")
    if not venue.credentials:
        out("  env         keyless public data")
    elif missing:
        out(f"  env         missing: {', '.join(missing)}")
    else:
        out("  env         complete")
    out(f"  eligibility {venue.eligibility_note}")


def _status(args: argparse.Namespace, env: Mapping[str, str], api: Any, out) -> int:
    registered: set[str] | None = None
    registry_error = ""
    try:
        registered = _registered(api)
    except (ApiError, ApiUnreachable) as exc:
        registry_error = str(exc)

    selected = [v for v in US_VENUES if args.venue in ("all", v.id)]
    failures = 0
    for venue in selected:
        _print_venue(venue, env, out)
        if venue.direct_env:
            out("  registered  n/a (direct paper client)")
        elif registered is None:
            out("  registered  unknown")
        else:
            out(f"  registered  {'yes' if venue.connector in registered else 'no'}")

        if args.check_data:
            if venue.direct_env:
                out("  public book n/a (use --check-account for Alpaca)")
            else:
                try:
                    summary = _probe(api, venue)
                    out(
                        "  public book OK: "
                        f"{summary['bid_levels']} bids/{summary['ask_levels']} asks, "
                        f"{summary['age_seconds']}s old"
                    )
                except (ApiError, ApiUnreachable, ValueError, TypeError) as exc:
                    failures += 1
                    out(f"  public book FAILED: {exc}")

        if args.check_account:
            if not venue.credentials or _missing(venue, env):
                out("  account     SKIP: credentials incomplete")
            else:
                try:
                    if venue.direct_env:
                        _check_alpaca(env)
                    elif registered is not None and venue.connector in registered:
                        _check_balances(api, venue.connector)
                    else:
                        raise ValueError("not registered in Hummingbot")
                    out("  account     OK (read-only)")
                except Exception as exc:  # output is scrubbed by the caller
                    failures += 1
                    out(f"  account     FAILED: {_detail(str(exc))}")

    if registry_error:
        out(f"\nHummingbot credential registry unavailable: {registry_error}")
        if args.check_account:
            failures += 1
    out("\nRead-only status complete; no orders or transfers were submitted.")
    return 1 if failures else 0


def _check_balances(api: Any, connector: str) -> None:
    status, body = api.request(
        "POST",
        "/portfolio/state",
        {"account_names": [ACCOUNT], "connector_names": [connector], "refresh": True},
    )
    if status != 200:
        raise ApiError(status, body)


def _check_alpaca(env: Mapping[str, str]) -> None:
    from condor.alpaca_paper import AlpacaPaperClient

    client = AlpacaPaperClient(
        key_id=env.get("ALPACA_API_KEY_ID"),
        secret_key=env.get("ALPACA_API_SECRET_KEY"),
    )
    asyncio.run(client.get_account())


def _import_venue(
    venue: Venue, env: Mapping[str, str], registered: set[str], api: Any, out
) -> None:
    missing = _missing(venue, env)
    if missing:
        raise SetupError(f"{venue.id}: missing .env variables: {', '.join(missing)}")
    if venue.direct_env:
        out(f"{venue.id}: validating existing paper credentials; no Hummingbot write")
        _check_alpaca(env)
        out(f"{venue.id}: paper account read OK; credentials remain in condor/.env")
        return
    if venue.connector in registered and not args_force:
        raise SetupError(f"{venue.id}: already registered; use --force to replace it")
    credentials = {
        line.field: _coerce(line.field, env[line.variable])
        for line in venue.credentials
    }
    status, body = api.request(
        "POST",
        f"/accounts/add-credential/{ACCOUNT}/{venue.connector}",
        credentials,
    )
    if status not in (200, 201):
        raise ApiError(status, body)
    out(f"{venue.id}: encrypted credential saved and accepted by Hummingbot")
    _check_balances(api, venue.connector)
    out(f"{venue.id}: account read OK; no order was submitted")


# Set only inside main, after parsing. Kept out of Venue/credential data so this
# helper remains trivial to exercise with fake APIs.
args_force = False


def _coerce(field: str, value: str) -> str | int:
    if field.endswith("_index"):
        try:
            return int(value)
        except ValueError:
            raise SetupError(f"{field} must be an integer") from None
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="read-only credential/feed/account status")
    status.add_argument("--venue", choices=["all", *venue_ids()], default="all")
    status.add_argument("--check-data", action="store_true")
    status.add_argument("--check-account", action="store_true")
    importer = sub.add_parser("import", help="write one venue's credentials explicitly")
    importer.add_argument("--venue", choices=venue_ids(), required=True)
    importer.add_argument(
        "--force", action="store_true", help="replace an existing credential"
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
    global args_force
    args = _parser().parse_args(argv)
    args_force = bool(getattr(args, "force", False))
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    env = _env(env_path)
    scrub = Scrubber(_all_env_values(env))

    def out(line: str) -> None:
        print(scrub(line), file=stdout)

    def err(line: str) -> None:
        print(scrub(line), file=stderr)

    try:
        server = load_server(config_path)
        scrub.add(
            [
                server.password,
                base64.b64encode(
                    f"{server.username}:{server.password}".encode()
                ).decode(),
            ]
        )
        api = _Scrubbed(connect(server), scrub)
        if args.command == "status":
            return _status(args, env, api, out)
        venue = next(v for v in US_VENUES if v.id == args.venue)
        _import_venue(venue, env, _registered(api), api, out)
        return 0
    except (ApiError, ApiUnreachable, SetupError, ValueError, TypeError) as exc:
        err(f"error: {exc}")
        return 1
    except Exception as exc:  # no traceback: an upstream error could quote a key
        err(f"error: unexpected {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
