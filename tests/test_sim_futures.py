"""Simulated Kraken perpetuals: shorting, leverage, funding, liquidation, queue fills. No network."""

import asyncio
from decimal import Decimal as D

import pytest

from condor.sim_exchange import engine as E
from condor.sim_exchange import futures as F
from condor.sim_exchange.kraken_public import KrakenPublicError

RULES = F.FutRules("PF_XBTUSD", "BTC", D("1"), 4, D("1200"))
NOW = 1_800_000_000.0


def run(coro):
    return asyncio.run(coro)


def book(bid="100", ask="101", size="50"):
    return {"bids": [(D(bid), D(size)), (D(bid) - 1, D(size))], "asks": [(D(ask), D(size)), (D(ask) + 1, D(size))], "ts": 1.0}


def acct(cash="10000"):
    return F.FAccount.new("t", D(cash), NOW)


def mkt(a, side, qty, bk=None, marks=None, **kw):
    return F.place_market(a, RULES, side=side, qty=D(qty), book=bk or book(), marks=marks or {}, now=NOW, **kw)


# ── shorting and P&L ────────────────────────────────────────────────────


def test_short_profits_when_price_falls_and_pays_taker_fees_both_ways():
    a = acct()
    mkt(a, "sell", "10")  # short 10 at the bid, 100
    assert a.positions["PF_XBTUSD"].qty == D("-10") and a.positions["PF_XBTUSD"].entry == D("100")
    fall = book("89", "90")
    _, fills, _ = mkt(a, "buy", "10", fall)  # cover at the ask, 90
    assert "PF_XBTUSD" not in a.positions
    assert a.realized_pnl == D("100")  # 10 * (100 - 90)
    assert a.fees_paid == D("1000") * D("0.0005") + D("900") * D("0.0005")  # 0.5 + 0.45
    assert a.cash == D("10000") + D("100") - a.fees_paid
    assert fills[0].realized_pnl == D("100")


def test_a_short_loses_when_price_rises_and_equity_marks_to_the_mark():
    a = acct()
    mkt(a, "sell", "10")
    assert F.unrealized(a, {"PF_XBTUSD": D("110")}) == D("-100")
    assert F.equity(a, {"PF_XBTUSD": D("110")}) == a.cash - D("100")


def test_flipping_through_zero_realizes_the_old_side_and_opens_the_new_at_the_fill():
    a = acct()
    mkt(a, "buy", "5")  # long 5 at 101
    mkt(a, "sell", "8", book("105", "106"))  # sell through: close 5 at 105, open short 3 at 105
    p = a.positions["PF_XBTUSD"]
    assert p.qty == D("-3") and p.entry == D("105")
    assert a.realized_pnl == D("20")  # 5 * (105 - 101)


def test_adding_to_a_position_averages_the_entry():
    a = acct()
    mkt(a, "buy", "2", book("100", "100"))
    mkt(a, "buy", "2", book("109", "110"))
    assert a.positions["PF_XBTUSD"].entry == D("105") and a.positions["PF_XBTUSD"].qty == D("4")


# ── hard limits ─────────────────────────────────────────────────────────


def test_order_cap_leverage_cap_and_reduce_only_are_enforced():
    a = acct("10000")
    with pytest.raises(E.Rejected, match="per-order limit"):
        mkt(a, "sell", "30")  # $3,000 > $2,500
    a.limits.max_leverage = D("1")
    a.limits.max_position_notional = D("100000")
    mkt(a, "sell", "24")  # ~$2,400 of $10k: fine at 1x
    mkt(a, "sell", "24")
    mkt(a, "sell", "24")
    mkt(a, "sell", "24")  # position now ~$9,600
    with pytest.raises(E.Rejected, match="x equity|per-contract"):
        mkt(a, "sell", "24")
    with pytest.raises(E.Rejected, match="reduce_only"):
        mkt(a, "sell", "1", reduce_only=True)  # would grow the short
    mkt(a, "buy", "1", reduce_only=True)  # shrinking is fine


def test_resting_orders_count_against_leverage_so_they_cannot_be_stacked():
    a = acct("10000")
    a.limits.max_leverage = D("1")
    for i in range(4):
        F.place_limit(a, RULES, side="sell", qty=D("24"), price=D("101"), book=book(), marks={}, now=NOW, client_id=f"c{i}")
    with pytest.raises(E.Rejected, match="x equity"):
        F.place_limit(a, RULES, side="sell", qty=D("24"), price=D("101"), book=book(), marks={}, now=NOW, client_id="c5")


# ── funding and liquidation ─────────────────────────────────────────────


def test_funding_longs_pay_shorts_receive_when_the_rate_is_positive():
    long_, short = acct(), acct()
    mkt(long_, "buy", "10")
    mkt(short, "sell", "10")
    marks, rate = {"PF_XBTUSD": D("100")}, {"PF_XBTUSD": D("0.0001")}  # 1 bp an hour
    for a in (long_, short):
        before = a.cash
        paid = F.accrue_funding(a, rate, marks, NOW + 7200)  # two hours
        assert abs(paid) == D("0.2")  # 10 * 100 * 0.0001 * 2
        assert a.cash == before - paid
    assert long_.funding_paid == D("0.2") and short.funding_paid == D("-0.2")


def test_a_move_that_breaches_maintenance_margin_liquidates_at_a_penalty():
    a = acct("1000")
    a.limits.max_leverage = D("10")
    a.limits.max_order_notional = D("5000")
    a.limits.max_position_notional = D("10000")
    mkt(a, "buy", "40")  # ~$4,040 long on $1,000 of equity: 4x, allowed at these owner limits
    marks = {"PF_XBTUSD": D("100")}  # unrealized -40 on entry 101: well above maintenance
    assert F.liquidate_if_needed(a, marks, NOW) == []
    crash = {"PF_XBTUSD": D("78")}  # equity ~ $80 against a ~$156 maintenance requirement
    assert F.liquidate_if_needed(a, crash, NOW) == ["PF_XBTUSD"]
    assert a.positions == {} and a.liquidations == 1
    assert D("0") < a.cash < D("200")  # most of it lost, but the account is not left negative


# ── resting orders ──────────────────────────────────────────────────────


def test_post_only_never_crosses_and_a_queued_limit_fills_only_from_the_real_tape():
    a = acct()
    with pytest.raises(E.Rejected, match="Post-only"):
        F.place_limit(a, RULES, side="buy", qty=D("1"), price=D("101"), book=book(), marks={}, now=NOW, post_only=True)
    o, fills, _ = F.place_limit(a, RULES, side="buy", qty=D("1"), price=D("100"), book=book(size="3"), marks={}, now=NOW, post_only=True)
    assert fills == [] and o.queue_ahead == D("3")
    ts = NOW + 5
    tape = [F.FutTrade(D("100"), D("3"), ts, "sell", "t1"), F.FutTrade(D("100"), D("0.4"), ts + 1, "sell", "t2")]
    got = F.on_trades(a, RULES, tape, ts + 2)
    assert [f.liquidity for f in got] == ["maker"] and got[0].qty == D("0.4")
    assert F.on_trades(a, RULES, tape, ts + 3) == []  # a replayed tape cannot fill twice
    assert a.positions["PF_XBTUSD"].qty == D("0.4")
    assert a.fees_paid == D("0.4") * D("100") * D("0.0002")


def test_a_trade_through_the_price_fills_a_resting_sell_in_full_and_opens_a_short():
    a = acct()
    F.place_limit(a, RULES, side="sell", qty=D("2"), price=D("102"), book=book(), marks={}, now=NOW)
    got = F.on_trades(a, RULES, [F.FutTrade(D("103"), D("0.1"), NOW + 1, "buy", "x")], NOW + 2)
    assert got[0].qty == D("2") and a.positions["PF_XBTUSD"].qty == D("-2")


# ── persistence and the fee what-if ─────────────────────────────────────


def test_account_round_trips_through_json_including_reduce_only_orders():
    import json

    a = acct()
    mkt(a, "sell", "3")
    F.place_limit(a, RULES, side="buy", qty=D("1"), price=D("99"), book=book(), marks={}, now=NOW, reduce_only=True)
    back = F.account_from_dict(json.loads(json.dumps(F.account_to_dict(a))))
    assert back.positions["PF_XBTUSD"].qty == D("-3")
    assert [o.reduce_only for o in back.orders.values() if o.status == "open"] == [True]
    assert back.cash == a.cash and len(back.fills) == 1


def test_fee_override_changes_futures_fees_and_rejects_volume_tiers():
    a = acct()
    a.fee_override = E.parse_fee_override({"maker": "0", "taker": "0.1"})
    mkt(a, "sell", "10")
    assert a.fees_paid == D("1000") * D("0.001")


# ── public client and service (fake Kraken) ─────────────────────────────


def test_client_refuses_private_endpoints_and_sorts_the_book_best_first():
    async def boom(path, params):
        raise AssertionError("must not reach the network")

    c = F.KrakenFuturesClient(transport=boom, min_interval_s=0)
    for bad in ("sendorder", "accounts", "openpositions", "../private"):
        with pytest.raises(KrakenPublicError, match="non-public"):
            run(c._get(bad))

    async def fake(path, params):
        return {"result": "success", "orderBook": {"bids": [[1, 5], [99, 2], [100, 1]], "asks": [[101, 1], [102, 3], [999, 0]]}}

    c = F.KrakenFuturesClient(transport=fake, min_interval_s=0)
    b = run(c.orderbook("PF_XBTUSD"))
    assert b["bids"][0] == (D("100"), D("1")) and b["asks"][0] == (D("101"), D("1")) and len(b["asks"]) == 2


def test_symbol_names_map_and_timestamps_with_nanoseconds_parse():
    assert F.to_symbol("BTC-USD") == "PF_XBTUSD" and F.to_symbol("eth/usd") == "PF_ETHUSD"
    assert F.to_symbol("DOGE") == "PF_XDGUSD" and F.to_symbol("PF_SOLUSD") == "PF_SOLUSD"
    assert F._ts("2026-11-04T20:05:09.97684461Z") == pytest.approx(1793822709.976844, abs=1e-3)


class FakeFut:
    def __init__(self):
        self.funding = D("0")
        self.tape = []

    async def rules(self, sym):
        return RULES

    async def ticker(self, sym):
        return {"bid": D("100"), "ask": D("101"), "last": D("100.5"), "mark": D("100.5"), "index": D("100.5"),
                "funding_hourly": self.funding, "funding_predicted_hourly": self.funding, "volume_24h": D("9"),
                "open_interest": D("9"), "high_24h": D("101"), "low_24h": D("99")}

    async def orderbook(self, sym):
        return book()

    async def trades(self, sym):
        out, self.tape = self.tape, []
        return out

    async def candles(self, sym, minutes, count):
        return [{"ts": 1, "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"}] * count


def test_service_shorts_persists_accrues_funding_and_reports_costs(tmp_path):
    clock = iter(float(NOW + i * 3600) for i in range(10_000))
    k = FakeFut()

    async def nosleep(_):
        return None

    s = F.FuturesService(k, tmp_path / "f.json", clock=lambda: next(clock), sleeper=nosleep, start_cash=D("10000"))
    k.funding = D("0.0001")
    res = run(s.place(symbol="BTC-USD", side="sell", qty=D("10"), client_order_id="a1"))
    assert D(res["position"]["qty"]) == D("-10") and D(res["fills"][0]["price"]) == D("100")
    again = run(s.place(symbol="BTC-USD", side="sell", qty=D("10"), client_order_id="a1"))
    assert again["duplicate"] is True
    pf = run(s.portfolio())
    assert pf["positions"][0]["side"] == "short" and pf["positions"][0]["asset"] == "BTC"
    assert D(pf["funding_paid"]) < 0  # a short was paid because the rate is positive
    cost = run(s.costs())
    assert D(cost["costs"]["funding_paid"]) < 0 and cost["fills"]["taker"] == 1
    snap = run(s.snapshot("BTC-USD", candles=2))
    assert snap["funding"]["annualized_pct"] and D(snap["your_position"]["qty"]) == D("-10") and len(snap["candles"]["rows"]) == 2
    s2 = F.FuturesService(k, tmp_path / "f.json", clock=lambda: next(clock), sleeper=nosleep)
    assert D(run(s2.portfolio())["positions"][0]["qty"]) == D("-10")  # survives a new process
    cfg = run(s.set_config(fee_override={"maker": "0.01", "taker": "0.02"}))
    assert cfg["fee_rates_now_pct"] == {"maker": "0.0100", "taker": "0.0200"}
    with pytest.raises(E.Rejected, match="volume_usd"):
        run(s.set_config(fee_override={"volume_usd": "1000"}))
    run(s.reset(D("5000")))
    assert run(s.portfolio())["positions"] == [] and run(s.config())["fee_override"]["maker"] == "0.01"
