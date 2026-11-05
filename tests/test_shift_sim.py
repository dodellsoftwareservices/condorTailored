"""A simulated Kraken shift does not depend on exchange credentials."""

import asyncio
import json

from condor import agent_config, shift, shift_report


def _profile():
    return shift.load_profile("kraken-spot-8h")[0]


class _Condor:
    base_url = "http://localhost:8088"

    def __init__(self, instances=None, orders=None):
        self.instances = instances or []
        self.orders = orders or []

    async def get(self, path):
        if path == "/auth/me":
            return {"id": 1, "role": "admin"}
        if path == "/sessions/options":
            return {"agents": [{"key": "openrouter:", "ready": True}]}
        if path == "/sim/kraken/portfolio":
            return {
                "cash": "49000",
                "positions": [
                    {"asset": "BTC", "qty": "0.01", "mid": "100000", "value": "1000"}
                ],
            }
        if path == "/sim/kraken/costs":
            return {
                "equity_pnl": "0",
                "realized_pnl": "0",
                "traded_volume": "0",
                "costs": {"exchange_fees": "0"},
                "fills": {"taker": 0, "maker": 0},
            }
        if path == "/sim/kraken/orders":
            return {"open": self.orders}
        if path == "/agents/sim_crypto_trader/strategies/kraken_sim_paper":
            return {"instances": self.instances}
        raise AssertionError(path)


class _NoHummingbot:
    base_url = "http://localhost:8000"

    async def get(self, path):
        raise AssertionError(f"sim shift unexpectedly called Hummingbot GET {path}")

    async def post(self, path, body):
        raise AssertionError(f"sim shift unexpectedly called Hummingbot POST {path}")


def test_sim_shift_preflight_prices_portfolio_without_exchange_keys(tmp_path):
    cfg_path = tmp_path / "agent-config.json"
    cfg_path.write_text(json.dumps({"budget": {"enabled": True, "appliesTo": "all"}}))
    result = asyncio.run(
        shift.run_preflight(
            _profile(),
            cfg=agent_config.load(cfg_path),
            model="openrouter:deepseek/deepseek-v4.1-flash-1017",
            condor=_Condor(),
            hb=_NoHummingbot(),
            now=1793850000.0,
            shift_root=tmp_path / "shifts",
            live_shifts=lambda: [],
            ledger_directory=tmp_path / "ledger",
        )
    )
    assert result.ok
    assert shift_report.balance_totals(result.balances) == {
        "master_account/kraken_sim": 50000.0
    }
    assert result.sim_costs_start["equity_pnl"] == "0"


def test_sim_shift_blocks_an_existing_session_even_with_another_context(tmp_path):
    cfg_path = tmp_path / "agent-config.json"
    cfg_path.write_text(json.dumps({"budget": {"enabled": True, "appliesTo": "all"}}))
    result = asyncio.run(
        shift.run_preflight(
            _profile(),
            cfg=agent_config.load(cfg_path),
            model="openrouter:deepseek/deepseek-v4.1-flash-1017",
            condor=_Condor(
                instances=[
                    {"agent_id": "other", "status": "running", "trading_context": ""}
                ]
            ),
            hb=_NoHummingbot(),
            now=1793850000.0,
            shift_root=tmp_path / "shifts",
            live_shifts=lambda: [],
            ledger_directory=tmp_path / "ledger",
        )
    )
    assert not result.ok
    assert any("already runs" in check.detail for check in result.failures)


def test_sim_pnl_uses_account_cost_deltas_after_a_completed_round_trip():
    start = {
        "equity_pnl": "-4.34",
        "realized_pnl": "0",
        "traded_volume": "1000",
        "costs": {"exchange_fees": "4"},
        "fills": {"taker": 2, "maker": 0},
    }
    end = {
        "equity_pnl": "-9.34",
        "realized_pnl": "-5",
        "traded_volume": "3500",
        "costs": {"exchange_fees": "5"},
        "fills": {"taker": 3, "maker": 1},
    }
    result = shift_report._sim_performance(end, start, "")
    assert result["available"]
    assert result["total"] == -5
    assert result["realized"] == -5
    assert result["unrealized"] == 0
    assert result["fees"] == 1
    assert result["volume"] == 2500
    assert result["trade_count"] == 2


def test_sim_pnl_does_not_fill_a_missing_start_with_zero():
    result = shift_report._sim_performance({"equity_pnl": "0"}, None, "")
    assert not result["available"]
