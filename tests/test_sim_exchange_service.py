"""The public-data client and the live wiring, driven by a fake Kraken (no network)."""

import asyncio
import json
from decimal import Decimal as D

import pytest

from condor.sim_exchange import engine as E
from condor.sim_exchange.kraken_public import (
    KrakenPublicClient,
    KrakenPublicError,
    PairRules,
    Trade,
    to_kraken_pair,
)
from condor.sim_exchange.service import SimService, count_llm_requests

RULES = PairRules("XBTUSD", "BTC", "USD", D("0.0001"), D("0.5"), D("0.5"), 4, 1)


def run(coro):
    return asyncio.run(coro)


# ── public client ───────────────────────────────────────────────────────


def test_pair_names_map_to_kraken_altnames():
    assert to_kraken_pair("BTC-USD") == "XBTUSD"
    assert to_kraken_pair("btc/usd") == "XBTUSD"
    assert to_kraken_pair("DOGE-USD") == "XDGUSD"
    assert to_kraken_pair("ETH-USD") == "ETHUSD"
    assert to_kraken_pair("XBTUSD") == "XBTUSD"


def test_client_refuses_anything_outside_the_public_namespace():
    async def boom(path, params):
        raise AssertionError("must not reach the network")

    c = KrakenPublicClient(transport=boom, min_interval_s=0)
    for bad in ("/0/private/Balance", "/0/private/AddOrder", "/0/public/../private/Balance", "/1/anything"):
        with pytest.raises(KrakenPublicError, match="non-public"):
            run(c._get(bad))


def test_client_parses_rules_book_trades_and_surfaces_kraken_errors():
    seen = []

    async def fake(path, params):
        seen.append((path, params))
        if path.endswith("AssetPairs"):
            return {"error": [], "result": {"XXBTZUSD": {
                "altname": "XBTUSD", "wsname": "XBT/USD", "ordermin": "0.00006", "costmin": "0.5",
                "tick_size": "0.1", "lot_decimals": 8, "pair_decimals": 1}}}
        if path.endswith("Depth"):
            return {"error": [], "result": {"XXBTZUSD": {
                "asks": [["100.0", "1.5", 1000]], "bids": [["99.5", "2.0", 1001]]}}}
        if path.endswith("Trades"):
            return {"error": [], "result": {"XXBTZUSD": [["99.5", "0.1", 1002.5, "s", "m", "", 7]], "last": "123"}}
        return {"error": ["EQuery:Unknown asset pair"], "result": {}}

    c = KrakenPublicClient(transport=fake, min_interval_s=0)
    r = run(c.pair_rules("BTC-USD"))
    assert (r.altname, r.base, r.quote, r.tick_size, r.lot_decimals) == ("XBTUSD", "BTC", "USD", D("0.1"), 8)
    book = run(c.depth("XBTUSD"))
    assert book["asks"] == [(D("100.0"), D("1.5"))] and book["bids"] == [(D("99.5"), D("2.0"))]
    trades, last = run(c.trades("XBTUSD", since="5"))
    assert last == "123" and trades[0] == Trade(D("99.5"), D("0.1"), 1002.5, "s", "7")
    assert seen[-1][1]["since"] == "5"
    with pytest.raises(KrakenPublicError, match="Unknown asset pair"):
        run(c._get("/0/public/Ticker", pair="NOPE"))
    # rules are cached: one AssetPairs call however often they are asked for
    run(c.pair_rules("BTC-USD"))
    assert sum(p.endswith("AssetPairs") for p, _ in seen) == 1


# ── service ─────────────────────────────────────────────────────────────


class FakeKraken:
    def __init__(self):
        self.events = []
        self.asks = [(D("100"), D("1")), (D("101"), D("2")), (D("102"), D("5"))]
        self.bids = [(D("99.5"), D("3")), (D("99"), D("10"))]
        self.tape = []
        self.last = "1"

    async def pair_rules(self, pair):
        return RULES

    async def depth(self, alt, count=500):
        self.events.append("depth")
        return {"asks": list(self.asks), "bids": list(self.bids), "ts": 1.0}

    async def ticker(self, alt):
        mid = (self.asks[0][0] + self.bids[0][0]) / 2
        return {"bid": self.bids[0][0], "ask": self.asks[0][0], "last": mid, "volume_24h": D("5"),
                "vwap_24h": mid, "trades_24h": 9, "high_24h": mid, "low_24h": mid}

    async def trades(self, alt, since=None):
        self.events.append("trades")
        out, self.tape = self.tape, []
        return out, str(int(self.last) + 1)

    async def ohlc(self, alt, interval=15):
        return [{"ts": i, "close": "1"} for i in range(5)]


def svc(tmp_path, kraken=None, **kw):
    kraken = kraken or FakeKraken()
    slept = []

    async def sleeper(s):
        slept.append(s)
        kraken.events.append("sleep")

    clock = iter(float(1_800_000_000 + i) for i in range(10_000))
    s = SimService(kraken, tmp_path / "acct.json", E.FeeSchedule.load(), clock=lambda: next(clock), sleeper=sleeper,
                   start_cash=D("10000"), **kw)
    s.slept = slept
    return s, kraken


def test_latency_is_waited_out_before_the_book_is_read(tmp_path):
    s, k = svc(tmp_path)
    res = run(s.place(pair="BTC-USD", side="buy", qty=D("1")))
    assert k.events.index("sleep") < k.events.index("depth")  # real market moves in the window hit the fill
    assert res["simulated_latency_ms"] == int(s.slept[0] * 1000)
    assert 20 <= res["simulated_latency_ms"] <= 2000


def test_dashboard_read_does_not_advance_or_rewrite_resting_orders(tmp_path):
    from unittest.mock import AsyncMock

    s, _ = svc(tmp_path)
    run(s.place(pair="BTC-USD", side="buy", type="limit", qty=D("1"), price=D("50")))
    before = s.path.read_bytes()
    s._advance = AsyncMock(side_effect=AssertionError("Dashboard must not match orders"))
    result = run(s.dashboard())
    assert result["open_orders"] == 1
    assert result["outcomes"] == []
    assert s.path.read_bytes() == before
    s._advance.assert_not_called()


def test_new_sim_accounts_match_confirmed_starting_capital_and_order_cap(tmp_path):
    service = SimService(FakeKraken(), tmp_path / "new-account.json")
    result = run(service.config())
    assert result["start_cash"] == "50000"
    assert result["limits"]["max_order_notional"] == "2500"


def test_account_persists_across_service_instances(tmp_path):
    s, k = svc(tmp_path)
    run(s.place(pair="BTC-USD", side="buy", qty=D("1")))
    s2, _ = svc(tmp_path, k)
    pf = run(s2.portfolio())
    assert pf["positions"][0]["asset"] == "BTC" and pf["positions"][0]["qty"] == "1"
    assert json.loads((tmp_path / "acct.json").read_text())["holdings"]["BTC"] == "1"


def test_a_retried_client_order_id_does_not_wait_or_fill_twice(tmp_path):
    s, k = svc(tmp_path)
    run(s.place(pair="BTC-USD", side="buy", qty=D("1"), client_order_id="x1"))
    waits = len(s.slept)
    again = run(s.place(pair="BTC-USD", side="buy", qty=D("1"), client_order_id="x1"))
    assert again["duplicate"] is True and len(s.slept) == waits
    assert run(s.portfolio())["positions"][0]["qty"] == "1"


def test_calibration_buy_is_rejected_before_any_market_call(tmp_path):
    s, k = svc(tmp_path)
    with pytest.raises(E.Rejected, match="Calibration buys are disabled|Fee-only calibration buys are disabled"):
        run(s.place(pair="BTC-USD", side="buy", qty=D("1"), client_order_id="cal-tick9-BTC-buy"))
    assert k.events == []
    assert not s.path.exists()


def test_limit_order_fills_only_from_the_real_tape_via_advance(tmp_path):
    s, k = svc(tmp_path)
    res = run(s.place(pair="BTC-USD", side="buy", type="limit", qty=D("1"), price=D("99.5")))
    oid = res["order"]["id"]
    assert res["fills"] == [] and res["order"]["queue_ahead"] == "3"
    assert run(s.advance())["filled"] == []  # an empty tape fills nothing
    ts = 1_800_000_500.0
    k.tape = [Trade(D("99.5"), D("3"), ts, "s", "1"), Trade(D("99.5"), D("1"), ts + 1, "s", "2")]
    out = run(s.advance())
    assert [f["liquidity"] for f in out["filled"]] == ["maker"] and D(out["filled"][0]["qty"]) == 1
    listing = run(s.orders())
    assert listing["open"] == [] and listing["recent"][0]["id"] == oid and listing["recent"][0]["status"] == "filled"


def test_snapshot_reports_costs_an_agent_must_beat(tmp_path):
    s, _ = svc(tmp_path)
    snap = run(s.snapshot("BTC-USD", candles=3))
    assert snap["spread_bps"] == "50.13"  # (100 - 99.5) / 99.75
    assert snap["your_fee_rates_pct"] == {"maker": "0.400", "taker": "0.800"}
    assert snap["round_trip_cost_bps"]["taker_both_ways_before_slippage"] == "160.0"
    assert snap["market_order_impact_bps"]["500"]["buy"] is not None
    assert snap["market_order_impact_bps"]["2500"]["buy"] is None  # visible depth ($815) cannot absorb $2,500
    assert len(snap["candles"]["rows"]) == 3


def test_owner_config_changes_limits_but_rejects_unknown_keys(tmp_path):
    s, _ = svc(tmp_path)
    cfg = run(s.set_config(limits={"max_order_notional": "50"}, costs={"fixed_daily_usd": "2"}))
    assert cfg["limits"]["max_order_notional"] == "50" and cfg["costs"]["fixed_daily_usd"] == "2"
    with pytest.raises(E.Rejected, match="Unknown limit"):
        run(s.set_config(limits={"max_everything": "1"}))
    with pytest.raises(E.Rejected, match="per-order limit"):
        run(s.place(pair="BTC-USD", side="buy", qty=D("1")))  # ~$100 notional against a $50 cap


def test_llm_requests_are_counted_from_the_shared_budget_ledger(tmp_path):
    ledger = tmp_path / "llm_budget"
    ledger.mkdir()
    day = "20271015"
    rows = [
        {"t": 1_800_000_100.0, "ev": "req", "model": "m", "n": 1},
        {"t": 1_800_000_101.0, "ev": "res", "status": 200},
        {"t": 1_800_000_102.0, "ev": "req", "model": "m", "n": 2},
        {"t": 1_700_000_000.0, "ev": "req", "model": "m", "n": 3},  # before the account existed
    ]
    (ledger / f"requests-{day}.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    assert count_llm_requests(1_800_000_000.0, ledger) == 2
    assert count_llm_requests(1_800_000_000.0, tmp_path / "missing") == 0


def test_reset_keeps_limits_and_archives_the_old_account(tmp_path):
    s, _ = svc(tmp_path)
    run(s.set_config(limits={"max_order_notional": "777"}))
    run(s.place(pair="BTC-USD", side="buy", qty=D("1")))
    run(s.reset(D("5000")))
    cfg = run(s.config())
    assert cfg["start_cash"] == "5000" and cfg["limits"]["max_order_notional"] == "777"
    assert run(s.portfolio())["positions"] == []
    assert list(tmp_path.glob("acct.*.bak"))


# ── fee what-if (owner-only) ────────────────────────────────────────────


def test_fee_override_reprices_orders_and_survives_reset_but_is_never_default(tmp_path):
    s, _ = svc(tmp_path)
    assert run(s.config())["fee_override"] is None
    assert run(s.config())["fee_rates_now_pct"] == {"maker": "0.4000", "taker": "0.8000"}
    cfg = run(s.set_config(fee_override={"maker": "0.25", "taker": "0.40", "label": "help-page rate"}))
    assert cfg["fee_rates_now_pct"] == {"maker": "0.2500", "taker": "0.4000"}
    snap = run(s.snapshot("BTC-USD", candles=1))
    assert snap["your_fee_rates_pct"] == {"maker": "0.2500", "taker": "0.400"}
    res = run(s.place(pair="BTC-USD", side="buy", qty=D("1")))
    assert D(res["fills"][0]["fee"]) == (D(res["fills"][0]["price"]) * D(res["fills"][0]["qty"]) * D("0.004")).quantize(D(res["fills"][0]["fee"]))
    run(s.reset(D("5000")))
    assert run(s.config())["fee_override"]["maker"] == "0.25"  # a reset keeps the owner's scenario
    assert run(s.set_config(fee_override={}))["fee_override"] is None  # {} clears it


def test_fee_override_can_price_as_a_volume_tier_and_rejects_nonsense(tmp_path):
    s, _ = svc(tmp_path)
    cfg = run(s.set_config(fee_override={"volume_usd": "10000000"}))
    assert cfg["fee_rates_now_pct"] == {"maker": "0.0000", "taker": "0.1000"}
    for bad in ({"maker": "0.25"}, {"maker": "-1", "taker": "0.4"}, {"maker": "5", "taker": "5"}, {"volume_usd": "x"}):
        with pytest.raises(E.Rejected, match="fee_override"):
            run(s.set_config(fee_override=bad))
