"""What the Sim Crypto Trader seat can and cannot mount."""

from pathlib import Path

import pytest

from condor.runtime import toolsets

SIM = {
    "sim_market_snapshot",
    "sim_submit_order",
    "sim_cancel_order",
    "sim_list_orders",
    "sim_portfolio",
    "sim_cost_report",
}
STOCK_AGENTS = Path(__file__).resolve().parent.parent / "agents"


@pytest.fixture(autouse=True)
def _real_stock_agents(monkeypatch):
    """The suite isolates agent folders; this test is about the shipped agent."""
    monkeypatch.setenv("CONDOR_STOCK_AGENTS_ROOT", str(STOCK_AGENTS))


def test_the_sim_seat_mounts_its_sim_tools_and_none_that_could_reach_a_real_account():
    muted = toolsets.allowlist_mutes("sim_crypto_trader")
    assert (
        "run_code" in muted
    ), "the sim agent was not found, so this test proves nothing"
    assert not (SIM & muted), sorted(SIM & muted)
    for dangerous in (
        "run_code",
        "delegate",
        "manage_routines",
        "create_order_executor",
        "execute_swap",
        "manage_bots",
    ):
        assert dangerous in muted, dangerous


def test_server_less_agent_is_not_reported_as_missing_a_server(tmp_path):
    """The sim seat has no Hummingbot server by design; the UI must not call that a fault."""
    import asyncio

    from condor.web.routes import agents as routes

    loop_dir = tmp_path / "agents" / "sim_crypto_trader" / "loops" / "kraken_sim_paper"
    loop_dir.mkdir(parents=True)

    class _Agent:
        server_required = False

    class _Store:
        def get(self, slug):
            return _Agent() if slug == "sim_crypto_trader" else None

    orig = routes._agent_store
    routes._agent_store = lambda: _Store()
    try:
        got = asyncio.run(routes._get_client_for_strategy(loop_dir, {}, 1))
    finally:
        routes._agent_store = orig
    assert got.client is None and got.unavailable == ""


FUT = {
    "sim_fut_snapshot",
    "sim_fut_submit_order",
    "sim_fut_cancel_order",
    "sim_fut_list_orders",
    "sim_fut_portfolio",
    "sim_fut_cost_report",
}


def test_spot_and_futures_seats_each_mount_only_their_own_simulator():
    spot_muted = toolsets.allowlist_mutes("sim_crypto_trader")
    fut_muted = toolsets.allowlist_mutes("sim_futures_trader")
    assert (
        "run_code" in fut_muted
    ), "the futures agent was not found, so this test proves nothing"
    assert FUT <= spot_muted and not (SIM & spot_muted)
    assert SIM <= fut_muted and not (FUT & fut_muted)
    for dangerous in (
        "run_code",
        "delegate",
        "manage_routines",
        "create_order_executor",
        "execute_swap",
        "manage_bots",
    ):
        assert dangerous in fut_muted, dangerous
