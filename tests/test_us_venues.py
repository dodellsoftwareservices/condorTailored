"""US venue catalog and credential/feed doctor; no live services or real keys."""

from __future__ import annotations

import importlib.util
import io
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from condor.us_venues import US_VENUES, VENUES_BY_ID
from condor.venues import is_test_venue

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "us_venues.py"
spec = importlib.util.spec_from_file_location("us_venues_cli_under_test", SCRIPT)
assert spec and spec.loader
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class FakeApi:
    def __init__(self, *, registered=(), book=None, post_result=(200, {})):
        self.registered = set(registered)
        self.book = book or {
            "bids": [{"price": 100.0, "amount": 1}],
            "asks": [{"price": 101.0, "amount": 1}],
            "timestamp": time.time(),
        }
        self.post_result = post_result
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/accounts/master_account/credentials":
            return 200, sorted(self.registered)
        if path == "/market-data/order-book":
            return 200, self.book
        if path == "/portfolio/state":
            return 200, {"master_account": {body["connector_names"][0]: []}}
        return self.post_result


@pytest.fixture
def run(tmp_path):
    def invoke(argv, *, env_text="", api=None):
        env_path = tmp_path / ".env"
        env_path.write_text(env_text)
        config_path = tmp_path / "config.yml"
        config_path.write_text(
            "servers:\n"
            "  local:\n"
            "    host: localhost\n"
            "    port: 8000\n"
            "    username: tester\n"
            "    password: fake-server-password\n"
            "default_server: local\n"
        )
        fake = api or FakeApi()
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            argv,
            env_path=env_path,
            config_path=config_path,
            connect=lambda _server: fake,
            stdout=out,
            stderr=err,
        )
        return code, out.getvalue(), err.getvalue(), fake

    return invoke


def test_catalog_keeps_mainnet_and_testnet_credentials_distinct():
    assert set(VENUES_BY_ID) == {v.id for v in US_VENUES}
    for venue in US_VENUES:
        assert venue.connector != "alpaca"  # Alpaca is not a Hummingbot connector here.
        assert not venue.connector or venue.direct_env or venue.connector == venue.id
        assert venue.eligibility_note
        assert venue.data_note
        if venue.simulation:
            assert venue.direct_env or is_test_venue(venue.connector)
        for line in venue.credentials:
            assert line.variable
            assert venue.direct_env or line.field

    perp = {line.field for line in VENUES_BY_ID["lighter_perpetual"].credentials}
    testnet = {
        line.field for line in VENUES_BY_ID["lighter_perpetual_testnet"].credentials
    }
    assert "lighter_perpetual_api_private_key" in perp
    assert "lighter_perpetual_testnet_api_private_key" in testnet
    assert perp.isdisjoint(testnet)
    assert any(
        line.field == "lighter_account_index"
        for line in VENUES_BY_ID["lighter"].credentials
    )
    assert not any(
        line.field.endswith("account_index")
        for line in VENUES_BY_ID["lighter_perpetual"].credentials
    )


def test_status_without_network_checks_is_read_only(run):
    code, out, err, api = run(["status"])

    assert code == 0, err
    assert "lighter_perpetual" in out
    assert "coinbase_advanced_trade" in out
    assert "alpaca_paper" in out
    assert "no orders or transfers" in out
    assert [call[:2] for call in api.calls] == [
        ("GET", "/accounts/master_account/credentials")
    ]


def test_status_checks_public_books_without_credentials(run):
    code, out, err, api = run(["status", "--venue", "kraken", "--check-data"])

    assert code == 0, err
    assert "public book OK" in out
    book_call = next(c for c in api.calls if c[1] == "/market-data/order-book")
    assert book_call[2] == {
        "connector_name": "kraken",
        "trading_pair": "BTC-USD",
        "depth": 5,
    }


def test_coinbase_probe_uses_keyless_public_endpoint_not_authenticated_path(
    run, monkeypatch
):
    now = time.time()
    monkeypatch.setattr(
        cli,
        "_direct_public_book",
        lambda url: {
            "pricebook": {
                "bids": [[100, 1]],
                "asks": [[101, 1]],
                "time": datetime_from_epoch(now),
            }
        },
    )
    code, out, err, api = run(
        ["status", "--venue", "coinbase_advanced_trade", "--check-data"]
    )
    assert code == 0, err
    assert "public book OK" in out
    assert not any(c[1] == "/market-data/order-book" for c in api.calls)


def datetime_from_epoch(value):
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    )


def test_direct_public_feed_refuses_non_coinbase_host():
    with pytest.raises(ValueError, match="unapproved"):
        cli._direct_public_book("https://example.com/book")


def test_testnet_probe_uses_mainnet_data_twin(run):
    code, out, err, api = run(
        ["status", "--venue", "lighter_perpetual_testnet", "--check-data"]
    )
    assert code == 0, err
    assert "public book OK" in out
    book_call = next(c for c in api.calls if c[1] == "/market-data/order-book")
    assert book_call[2]["connector_name"] == "lighter_perpetual"


def test_stale_book_is_a_failure_not_an_arbitrage_feed(run):
    book = {
        "bids": [[100, 1]],
        "asks": [[101, 1]],
        "timestamp": time.time() - 120,
    }
    code, out, _, _ = run(
        ["status", "--venue", "gemini", "--check-data"],
        api=FakeApi(book=book),
    )
    assert code == 1
    assert "public book FAILED" in out
    assert "stale" in out


@pytest.mark.parametrize(
    "book, message",
    [
        ({"bids": [], "asks": [[101, 1]], "timestamp": time.time()}, "two-sided"),
        (
            {"bids": [[102, 1]], "asks": [[101, 1]], "timestamp": time.time()},
            "best bid",
        ),
        ({"bids": [[100, 1]], "asks": [[101, 1]]}, "timestamp"),
        ({"bids": [[0, 1]], "asks": [[101, 1]], "timestamp": time.time()}, "price"),
    ],
)
def test_invalid_books_fail_closed(book, message):
    with pytest.raises(ValueError, match=message):
        cli._book_summary(book, now=time.time(), max_age=30)


def test_millisecond_book_timestamp_is_accepted():
    now = time.time()
    result = cli._book_summary(
        {
            "bids": [[100, 1]],
            "asks": [[101, 1]],
            "timestamp": int(now * 1000),
        },
        now=now,
        max_age=30,
    )
    assert result["age_seconds"] < 1


LIGHTER_ENV = """\
LIGHTER_PERPETUAL_L1_ADDRESS=0xfake-wallet-address
LIGHTER_PERPETUAL_API_KEY_INDEX=17
LIGHTER_PERPETUAL_API_PRIVATE_KEY=fake-lighter-private-key-Zq9xKdW3mVt7
LIGHTER_PERPETUAL_ACCOUNT_LIMIT=Standard
"""


def test_import_maps_lighter_fields_and_coerces_index(run):
    code, out, err, api = run(
        ["import", "--venue", "lighter_perpetual"], env_text=LIGHTER_ENV
    )

    assert code == 0, err
    write = next(c for c in api.calls if c[1].startswith("/accounts/add-credential/"))
    assert write[1].endswith("/lighter_perpetual")
    assert write[2]["lighter_perpetual_api_key_index"] == 17
    assert write[2]["lighter_perpetual_l1_address"] == "0xfake-wallet-address"
    assert write[2]["lighter_perpetual_account_limit"] == "Standard"
    assert "encrypted credential saved" in out
    assert "fake-lighter-private-key" not in out


def test_import_never_overwrites_without_force(run):
    api = FakeApi(registered=["lighter_perpetual"])
    code, _, err, _ = run(
        ["import", "--venue", "lighter_perpetual"],
        env_text=LIGHTER_ENV,
        api=api,
    )
    assert code == 1
    assert "already registered" in err
    assert not any(c[1].startswith("/accounts/add-credential/") for c in api.calls)


def test_force_is_the_only_path_that_replaces_a_credential(run):
    api = FakeApi(registered=["lighter_perpetual"])
    code, _, err, _ = run(
        ["import", "--venue", "lighter_perpetual", "--force"],
        env_text=LIGHTER_ENV,
        api=api,
    )
    assert code == 0, err
    assert any(c[1].startswith("/accounts/add-credential/") for c in api.calls)


def test_missing_fields_do_not_reach_the_api(run):
    code, _, err, api = run(
        ["import", "--venue", "lighter_perpetual"],
        env_text="LIGHTER_PERPETUAL_API_KEY_INDEX=2\n",
    )
    assert code == 1
    assert "missing .env variables" in err
    assert "LIGHTER_PERPETUAL_API_PRIVATE_KEY" in err
    assert not any(c[1].startswith("/accounts/add-credential/") for c in api.calls)


def test_upstream_error_cannot_echo_credentials_or_server_password(run):
    secret = "fake-lighter-private-key-Zq9xKdW3mVt7"
    api = FakeApi(post_result=(400, {"detail": f"bad key {secret}"}))
    code, out, err, _ = run(
        ["import", "--venue", "lighter_perpetual"],
        env_text=LIGHTER_ENV,
        api=api,
    )
    assert code == 1
    assert secret not in out + err
    assert "fake-server-password" not in out + err
    assert "***" in err


def test_alpaca_import_never_writes_to_hummingbot(run, monkeypatch):
    calls = []

    monkeypatch.setattr(cli, "_check_alpaca", lambda env: calls.append("checked"))
    code, out, err, api = run(
        ["import", "--venue", "alpaca_paper"],
        env_text="ALPACA_API_KEY_ID=fake-paper-id\nALPACA_API_SECRET_KEY=fake-paper-secret\n",
    )
    assert code == 0, err
    assert calls == ["checked"]
    assert "credentials remain in condor/.env" in out
    assert not any(c[1].startswith("/accounts/add-credential/") for c in api.calls)
