import math
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from condor.dashboard_history import record_equity
from condor.dashboard_metrics import (
    alpaca_outcomes,
    clean_points,
    outcome_metrics,
    risk_metrics,
)
from condor.web.auth import get_current_user
from condor.web.models import WebUser
from condor.web.routes import dashboard

DAY = 86400
NOW = int(datetime(2026, 11, 3, tzinfo=timezone.utc).timestamp())


def test_alpaca_history_contract_stays_on_paper_host():
    import asyncio

    from condor.alpaca_paper import PAPER_TRADING_HOST, AlpacaPaperClient

    client = AlpacaPaperClient(key_id="test-only", secret_key="test-only")
    client._request = AsyncMock(return_value={"timestamp": [], "equity": []})
    asyncio.run(client.get_portfolio_history("1D"))
    client._request.assert_awaited_once_with(
        "GET",
        PAPER_TRADING_HOST,
        "/v2/account/portfolio/history",
        params={"period": "1D", "timeframe": "5Min"},
    )
    with pytest.raises(ValueError):
        asyncio.run(client.get_portfolio_history("invalid"))
    assert client._request.await_count == 1


def test_alpaca_fill_pages_follow_cursor_and_report_incomplete_history():
    import asyncio

    from condor.alpaca_paper import AlpacaPaperClient

    client = AlpacaPaperClient(key_id="test-only", secret_key="test-only")
    first = [{"id": str(i)} for i in range(100)]
    client._request = AsyncMock(side_effect=[first, [{"id": "100"}]])
    rows, complete = asyncio.run(client.get_fill_history())
    assert complete and len(rows) == 101
    assert client._request.await_args_list[1].kwargs["params"]["page_token"] == "99"
    client._request = AsyncMock(return_value=first)
    _, complete = asyncio.run(client.get_fill_history())
    assert complete is False
    assert client._request.await_count == 2


def test_batting_average_counts_flat_closes_and_never_open_positions():
    metrics = outcome_metrics([{"pnl": 10}, {"pnl": -5}, {"pnl": 0}])
    assert metrics["batting_average"] == pytest.approx(100 / 3)
    assert metrics["profit_factor"] == 2
    assert metrics["closed_trades"] == 3
    assert outcome_metrics([])["batting_average"] is None
    assert outcome_metrics([{"pnl": 10}])["profit_factor"] is None


def test_sharpe_uses_daily_returns_and_sample_deviation():
    equity = 100.0
    points = [{"timestamp": NOW - 9 * DAY - 1, "equity": equity}]
    for i, value in enumerate([0.1, -0.1, 0.1, -0.1, 0.1, -0.1, 0.1]):
        equity *= 1 + value
        points.append({"timestamp": NOW - (8 - i) * DAY - 1, "equity": equity})
    metrics = risk_metrics(points, NOW)
    assert metrics["daily_observations"] == 7
    assert metrics["sharpe"] == pytest.approx(0.133630620956)
    assert metrics["max_drawdown_pct"] == pytest.approx(11.791)


def test_intraday_points_gaps_and_zero_variance_do_not_invent_sharpe():
    intraday = [
        {"timestamp": NOW - 10000 + i * 60, "equity": 100 + i} for i in range(10)
    ]
    assert risk_metrics(intraday, NOW)["daily_observations"] == 0
    points = [{"timestamp": NOW - i * DAY - 1, "equity": 100} for i in range(12)]
    assert risk_metrics(points, NOW)["sharpe"] is None
    assert risk_metrics(points[::2], NOW)["daily_observations"] == 0
    assert risk_metrics(points[:4], NOW)["sharpe"] is None


def test_history_retains_complete_loss_but_drops_unfunded_prefix_and_bad_data():
    points = [
        {"timestamp": NOW - 4, "equity": 0},
        {"timestamp": NOW - 3, "equity": 100},
        {"timestamp": NOW - 2, "equity": 0},
        {"timestamp": NOW + 1, "equity": 500},
        {"timestamp": NOW - 1, "equity": math.nan},
    ]
    assert [p["equity"] for p in clean_points(points, NOW)] == [100, 0]
    assert risk_metrics(points, NOW)["max_drawdown_pct"] == 100


def fill(id, side, qty, price, order=None, second=0):
    return {
        "id": id,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "symbol": "AAPL",
        "order_id": order or id,
        "transaction_time": f"2026-11-02T12:00:{second:02d}Z",
    }


def test_alpaca_fifo_matches_partial_closes_once_and_dedupes():
    rows = [
        fill("a", "buy", 2, 100),
        fill("b", "buy", 1, 120, second=1),
        fill("c", "sell", 1, 110, "sell-order", 2),
        fill("d", "sell", 2, 130, "sell-order", 3),
    ]
    outcomes, volume, complete = alpaca_outcomes([*rows, rows[-1]])
    assert complete
    assert volume == 690
    assert len(outcomes) == 1
    assert outcomes[0]["pnl"] == 50


def test_missing_basis_and_nonfinite_fill_prices_withhold_outcomes():
    assert alpaca_outcomes([fill("a", "sell", 1, 100)])[2] is False
    assert alpaca_outcomes([fill("a", "buy", 1, "NaN")])[2] is False


def test_durable_sim_history_replaces_bucket_and_starts_fresh_after_reset():
    first = record_equity("kraken_sim", 1000, 100010, created=900, starting=100000)
    second = record_equity("kraken_sim", 1001, 100020, created=900, starting=100000)
    assert len(first) == len(second) == 2
    assert second[-1]["equity"] == 100020
    reset = record_equity("kraken_sim", 2000, 100000, created=1900, starting=100000)
    assert reset[0] == {"timestamp": 1900, "equity": 100000}
    assert all(row["timestamp"] >= 1900 for row in reset)


@pytest.fixture
def api_client(monkeypatch):
    alpaca = SimpleNamespace(
        get_account=AsyncMock(return_value=SimpleNamespace(equity=100500, cash=98000)),
        get_positions=AsyncMock(return_value=[SimpleNamespace(market_value=2500)]),
        get_portfolio_history=AsyncMock(
            return_value={"timestamp": [NOW - 2 * DAY], "equity": [100000]}
        ),
        get_fill_history=AsyncMock(
            return_value=(
                [fill("a", "buy", 1, 100), fill("b", "sell", 1, 110, second=1)],
                True,
            )
        ),
    )
    sim = SimpleNamespace(
        dashboard=AsyncMock(
            return_value={
                "start_cash": "100000",
                "equity": "99750",
                "cash": "97000",
                "gross_exposure": "2750",
                "max_order_notional": "2500",
                "volume": "3000",
                "fees": "8",
                "outcomes": [
                    {"pnl": 100, "timestamp": NOW - 10},
                    {"pnl": -350, "timestamp": NOW - 20},
                ],
                "created": NOW - 2 * DAY,
            }
        )
    )
    monkeypatch.setattr(dashboard, "get_alpaca_client", lambda: alpaca)
    monkeypatch.setattr(dashboard, "get_sim_service", lambda: sim)
    monkeypatch.setattr(dashboard, "_cache", {})
    monkeypatch.setattr(dashboard.time, "time", lambda: NOW)
    monkeypatch.setenv("EQUITY_MAX_ORDER_NOTIONAL", "2500")
    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[get_current_user] = lambda: WebUser(id=1, role="admin")
    return TestClient(app), alpaca, sim


def test_dashboard_uses_initial_equity_and_preserves_order_cap(api_client):
    client, alpaca, sim = api_client
    result = client.get("/dashboard/paper?range=1M")
    assert result.status_code == 200
    data = result.json()
    assert data["summary"]["starting_capital"] == 200000
    assert data["summary"]["equity"] == 200250
    assert data["summary"]["pnl"] == 250
    assert data["summary"]["roi_pct"] == pytest.approx(0.125)
    assert data["summary"]["batting_average"] == pytest.approx(200 / 3)
    assert data["summary"]["profit_factor"] == pytest.approx(110 / 350)
    assert [a["order_cap"] for a in data["accounts"]] == [2500, 2500]
    client.get("/dashboard/paper?range=1M")
    assert alpaca.get_account.await_count == sim.dashboard.await_count == 1


def test_unavailable_account_does_not_turn_partial_balances_into_total(api_client):
    client, alpaca, _ = api_client
    alpaca.get_account.side_effect = OSError("private provider URL and credentials")
    result = client.get("/dashboard/paper").json()
    assert result["summary"]["equity"] is None
    assert result["summary"]["pnl"] is None
    assert result["summary"]["starting_capital"] == 200000
    assert result["accounts"][1]["equity"] == 99750
    assert "private provider" not in str(result)


def test_secondary_history_failure_preserves_current_equity(api_client):
    client, alpaca, _ = api_client
    alpaca.get_fill_history.side_effect = RuntimeError("bad history")
    result = client.get("/dashboard/paper").json()
    assert result["accounts"][0]["equity"] == 100500
    assert result["accounts"][0]["batting_average"] is None
    assert result["summary"]["batting_average"] is None


def test_shared_accounts_remain_admin_only(api_client):
    client, alpaca, sim = api_client
    client.app.dependency_overrides[get_current_user] = lambda: WebUser(
        id=2, role="user"
    )
    assert client.get("/dashboard/paper").status_code == 403
    assert not alpaca.get_account.called
    assert not sim.dashboard.called


def test_background_sampling_uses_observation_path_without_advancing_orders(
    tmp_path, monkeypatch
):
    import asyncio

    from condor import dashboard_history
    from condor.web.routes import sim_exchange

    path = dashboard_history.data_dir() / "sim_exchange" / "kraken_default.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    service = SimpleNamespace(
        dashboard=AsyncMock(
            return_value={
                "equity": "100025",
                "created": NOW - 100,
                "start_cash": "100000",
            }
        ),
        portfolio=AsyncMock(
            side_effect=AssertionError("Must not process resting orders")
        ),
    )
    monkeypatch.setattr(sim_exchange, "get_sim_service", lambda: service)
    monkeypatch.setattr(dashboard_history.time, "time", lambda: NOW)
    asyncio.run(dashboard_history.sample_sim_equity())
    service.dashboard.assert_awaited_once()
    service.portfolio.assert_not_called()
    history = record_equity(
        "kraken_sim", NOW + 1, 100025, created=NOW - 100, starting=100000
    )
    assert history[-1]["equity"] == 100025
