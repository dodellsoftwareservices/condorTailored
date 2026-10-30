"""``condor/alpaca_paper.py`` and ``routines/alpaca_etf_sweep.py``.

The client's one job is to refuse a live account; the routine's is to never sweep
the same P&L twice. Both are tested against the real modules, with Alpaca and the
Hummingbot API faked at their edges.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from condor import alpaca_paper as A
from condor.reports.builder import ReportBuilder
from routines import alpaca_etf_sweep as S

# ── the client: paper only ──


@pytest.fixture(autouse=True)
def _no_ambient_alpaca_keys(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)


@pytest.mark.parametrize(
    "bad",
    [
        "https://api.alpaca.markets",
        "https://api.alpaca.markets/",
        "https://broker-api.alpaca.markets",
        "http://evil.test",
    ],
)
def test_live_trading_hosts_are_refused_at_construction(bad):
    with pytest.raises(A.AlpacaConfigError):
        A.AlpacaPaperClient("k", "s", trading_host=bad)


def test_the_data_host_is_pinned_too():
    """Every request carries the key pair; a caller-supplied host is a place to send it."""
    with pytest.raises(A.AlpacaConfigError):
        A.AlpacaPaperClient("k", "s", data_host="https://evil.test")


def test_paper_host_is_accepted_and_a_trailing_slash_normalises():
    assert A.AlpacaPaperClient("k", "s")._trading_host == A.PAPER_TRADING_HOST
    slashed = A.AlpacaPaperClient("k", "s", trading_host=A.PAPER_TRADING_HOST + "/")
    assert slashed._trading_host == A.PAPER_TRADING_HOST


def test_missing_credentials_name_the_variables():
    with pytest.raises(A.AlpacaConfigError, match="ALPACA_API_KEY_ID"):
        A.AlpacaPaperClient()


def test_credentials_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")
    client = A.AlpacaPaperClient()
    assert client._headers["APCA-API-KEY-ID"] == "id"


def test_non_positive_notional_is_rejected_before_any_http():
    client = A.AlpacaPaperClient("k", "s")
    with pytest.raises(ValueError):
        asyncio.run(client.submit_notional_order("VOO", -5))


# ── the routine: parsing ──


def test_weights_normalise():
    assert S._parse_weights("VOO:70,VTI:30") == {"VOO": 0.7, "VTI": 0.3}
    assert S._parse_weights("VOO:7,VTI:3") == {"VOO": 0.7, "VTI": 0.3}
    three = S._parse_weights("VOO:50,VTI:30,SPY:20")
    assert abs(sum(three.values()) - 1.0) < 1e-9


@pytest.mark.parametrize("raw", ["garbage", "VOO:0,VTI:0", ""])
def test_unusable_weights_parse_to_nothing(raw):
    assert S._parse_weights(raw) == {}


def test_sweep_pct_cannot_exceed_the_profit():
    with pytest.raises(ValueError):
        S.Config(sweep_pct=150)
    with pytest.raises(ValueError):
        S.Config(sweep_pct=-1)


def test_defaults_place_no_orders():
    assert S.Config().dry_run is True


# ── the routine: P&L comes out of the real bot-status shape ──


def bot_status(*realized):
    """The API's shape: data -> bot -> performance -> controller -> performance."""
    return {
        "status": "success",
        "data": {
            "bot-1": {
                "status": "running",
                "performance": {
                    f"ctrl_{i}": {
                        "status": "running",
                        "performance": {"realized_pnl_quote": pnl},
                    }
                    for i, pnl in enumerate(realized)
                },
            }
        },
    }


def test_realized_pnl_is_read_one_level_down(monkeypatch):
    """The first draft read the controller dict itself and so always saw 0."""

    async def fetch(client):
        return bot_status(120.5, -20.0)

    monkeypatch.setattr(S, "fetch_bots_status", fetch)
    total, controllers = asyncio.run(S._realized_pnl(object()))
    assert total == pytest.approx(100.5)
    assert controllers == 2


def test_an_unreadable_status_is_not_zero_pnl(monkeypatch):
    async def fetch(client):
        raise ConnectionError("api down")

    monkeypatch.setattr(S, "fetch_bots_status", fetch)
    with pytest.raises(S.StatusUnavailable, match="api down"):
        asyncio.run(S._realized_pnl(object()))


# ── the routine: run() ──


class FakeAlpaca:
    """Stands in for AlpacaPaperClient; records orders, rejects chosen symbols."""

    def __init__(self, is_open=True, reject=()):
        self.is_open = is_open
        self.reject = set(reject)
        self.orders: list[tuple[str, float]] = []

    async def is_market_open(self):
        return self.is_open, "2026-11-02T14:30:00Z"

    async def submit_notional_order(self, symbol, notional, side="buy"):
        if symbol in self.reject:
            raise A.AlpacaAPIError(f"POST /v2/orders -> 422: {symbol} rejected")
        self.orders.append((symbol, notional))
        return {"id": f"order-{symbol}-xxxx", "status": "accepted"}


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _quiet_reports(monkeypatch):
    async def save(self, report_id=None):
        return "report-id"

    monkeypatch.setattr(ReportBuilder, "save", save)


def write_ledger(data_dir, swept):
    (data_dir / S.LEDGER_NAME).write_text(
        json.dumps({"swept_pnl_total": swept, "events": []})
    )


def read_ledger(data_dir):
    return json.loads((data_dir / S.LEDGER_NAME).read_text())


def run_sweep(monkeypatch, *, realized, alpaca=None, **cfg):
    alpaca = alpaca or FakeAlpaca()

    async def get_client(chat_id, context=None):
        return object()

    async def fetch(client):
        return bot_status(realized)

    monkeypatch.setattr(S, "get_client", get_client)
    monkeypatch.setattr(S, "fetch_bots_status", fetch)
    monkeypatch.setattr(S, "AlpacaPaperClient", lambda: alpaca)
    out = asyncio.run(S.run(S.Config(**cfg), SimpleNamespace(_chat_id=1)))
    return out, alpaca


def test_dry_run_places_nothing_and_leaves_the_ledger_alone(monkeypatch, data):
    out, alpaca = run_sweep(monkeypatch, realized=200.0)
    assert out.startswith("DRY RUN: would sweep $100.00")
    assert alpaca.orders == []
    assert not (data / S.LEDGER_NAME).exists()


def test_live_run_splits_by_weight_and_advances_the_ledger(monkeypatch, data):
    out, alpaca = run_sweep(monkeypatch, realized=200.0, dry_run=False)
    assert alpaca.orders == [("VOO", pytest.approx(70.0)), ("VTI", pytest.approx(30.0))]
    # $100 swept at 50% consumed $200 of P&L.
    assert read_ledger(data)["swept_pnl_total"] == pytest.approx(200.0)
    assert out.startswith("Swept $100.00 into VOO, VTI")


def test_a_second_run_does_not_sweep_the_same_pnl_again(monkeypatch, data):
    run_sweep(monkeypatch, realized=200.0, dry_run=False)
    out, alpaca = run_sweep(monkeypatch, realized=200.0, dry_run=False)
    assert alpaca.orders == []
    assert out.startswith("No new realized P&L")


def test_partial_failure_advances_only_by_what_was_placed(monkeypatch, data):
    out, alpaca = run_sweep(
        monkeypatch,
        realized=200.0,
        dry_run=False,
        alpaca=FakeAlpaca(reject={"VTI"}),
    )
    assert alpaca.orders == [("VOO", pytest.approx(70.0))]
    # $70 placed at 50% accounts for $140 of the $200; $60 stays pending.
    assert read_ledger(data)["swept_pnl_total"] == pytest.approx(140.0)
    assert "(1 failed)" in out


def test_total_failure_leaves_the_ledger_unwritten(monkeypatch, data):
    out, _ = run_sweep(
        monkeypatch,
        realized=200.0,
        dry_run=False,
        alpaca=FakeAlpaca(reject={"VOO", "VTI"}),
    )
    assert "failed entirely" in out
    assert not (data / S.LEDGER_NAME).exists()


def test_a_drawdown_defers_and_never_rewinds(monkeypatch, data):
    write_ledger(data, 1000.0)
    out, alpaca = run_sweep(monkeypatch, realized=900.0, dry_run=False)
    assert alpaca.orders == []
    assert read_ledger(data)["swept_pnl_total"] == 1000.0
    assert out.startswith("No new realized P&L")


def test_below_the_minimum_the_pnl_accumulates(monkeypatch, data):
    out, alpaca = run_sweep(monkeypatch, realized=20.0, dry_run=False)
    assert alpaca.orders == []
    assert "below $25.00 minimum" in out


def test_a_closed_market_defers_the_sweep(monkeypatch, data):
    out, alpaca = run_sweep(
        monkeypatch,
        realized=200.0,
        dry_run=False,
        alpaca=FakeAlpaca(is_open=False),
    )
    assert alpaca.orders == []
    assert out.startswith("Equities market closed")
    assert not (data / S.LEDGER_NAME).exists()


def test_a_closed_market_can_be_overridden_to_queue_for_the_open(monkeypatch, data):
    _, alpaca = run_sweep(
        monkeypatch,
        realized=200.0,
        dry_run=False,
        ignore_market_hours=True,
        alpaca=FakeAlpaca(is_open=False),
    )
    assert len(alpaca.orders) == 2


def test_a_corrupt_ledger_refuses_rather_than_resweeping_everything(monkeypatch, data):
    (data / S.LEDGER_NAME).write_text("{not json")
    out, alpaca = run_sweep(monkeypatch, realized=200.0, dry_run=False)
    assert alpaca.orders == []
    assert out.startswith("Sweep not run:")
    assert "second time" in out


def test_a_ledger_without_a_total_is_corrupt_too(monkeypatch, data):
    (data / S.LEDGER_NAME).write_text(json.dumps({"events": []}))
    out, alpaca = run_sweep(monkeypatch, realized=200.0, dry_run=False)
    assert alpaca.orders == []
    assert out.startswith("Sweep not run:")


def test_unreadable_bot_status_is_reported_not_swept_as_zero(monkeypatch, data):
    async def get_client(chat_id, context=None):
        return object()

    async def fetch(client):
        raise ConnectionError("api down")

    monkeypatch.setattr(S, "get_client", get_client)
    monkeypatch.setattr(S, "fetch_bots_status", fetch)
    monkeypatch.setattr(S, "AlpacaPaperClient", lambda: FakeAlpaca())
    out = asyncio.run(S.run(S.Config(dry_run=False), SimpleNamespace(_chat_id=1)))
    assert out.startswith("Sweep not run:")
    assert "api down" in out


def test_orders_placed_but_ledger_unsaved_is_said_loudly(monkeypatch, data):
    def broken_save(ledger):
        raise OSError("disk full")

    monkeypatch.setattr(S, "_save_ledger", broken_save)
    out, alpaca = run_sweep(monkeypatch, realized=200.0, dry_run=False)
    assert len(alpaca.orders) == 2
    assert "LEDGER NOT SAVED" in out
    assert "sweep this P&L again" in out


def test_missing_alpaca_keys_are_reported_not_raised(monkeypatch, data):
    async def get_client(chat_id, context=None):
        return object()

    monkeypatch.setattr(S, "get_client", get_client)
    out = asyncio.run(S.run(S.Config(), SimpleNamespace(_chat_id=1)))
    assert out.startswith("Alpaca not configured.")
