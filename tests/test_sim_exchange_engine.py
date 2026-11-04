"""Hand-checked scenarios for the simulated Kraken exchange engine."""

from decimal import Decimal as D

import pytest

from condor.sim_exchange import engine as E
from condor.sim_exchange.kraken_public import PairRules, Trade

RULES = PairRules("XBTUSD", "BTC", "USD", D("0.0001"), D("0.5"), D("0.5"), 4, 1)
SCHED = E.FeeSchedule.load()
NOW = 1_800_000_000.0


def book(asks=None, bids=None):
    return {
        "asks": [(D(p), D(s)) for p, s in (asks or [("100", "1"), ("101", "2"), ("102", "5")])],
        "bids": [(D(p), D(s)) for p, s in (bids or [("99.5", "3"), ("99", "10")])],
        "ts": NOW,
    }


def acct(cash="10000", **limits):
    a = E.Account.new("t", D(cash), NOW)
    a.limits = E.Limits(
        max_order_notional=D(limits.get("order", "100000")),
        max_position_notional=D(limits.get("position", "100000")),
        max_gross_exposure_pct=D(limits.get("exposure", "100")),
        min_cash_reserve_pct=D(limits.get("reserve", "0")),
        max_open_orders=limits.get("open", 10),
    )
    return a


def buy(a, qty, **kw):
    return E.place_market(a, RULES, SCHED, side="buy", qty=D(qty), book=kw.pop("book", book()), now=NOW, **kw)


# ── fees ────────────────────────────────────────────────────────────────


def test_fee_tier_follows_rolling_volume():
    assert SCHED.rates(D("0"), "BTC") == (D("0.004"), D("0.008"))
    assert SCHED.rates(D("2500"), "BTC") == (D("0.003"), D("0.006"))
    assert SCHED.rates(D("9999"), "BTC") == (D("0.003"), D("0.006"))


def test_stablecoin_bases_use_their_own_schedule():
    assert SCHED.rates(D("0"), "USDT") == (D("0.002"), D("0.002"))


# ── market orders ───────────────────────────────────────────────────────


def test_market_buy_walks_the_book_and_charges_taker_fee():
    a = acct()
    order, fills, dup = buy(a, "2.5")
    # 1 @ 100 + 1.5 @ 101 = 251.5 ; avg 100.6 ; fee 0.8% = 2.012
    f = fills[0]
    assert (f.qty, f.notional, f.price) == (D("2.5"), D("251.50000"), D("100.60000"))
    assert f.fee == D("2.01200") and f.liquidity == "taker"
    assert a.cash == D("10000") - D("251.5") - D("2.012")
    # mid = (100 + 99.5) / 2 = 99.75 -> shortfall (100.6 - 99.75) * 2.5 = 2.125
    assert f.slippage_cost == D("2.12500") and a.slippage_cost == D("2.12500")
    assert order.status == "filled" and not dup


def test_thin_book_gives_a_partial_fill_not_an_invention():
    a = acct()
    order, fills, _ = buy(a, "20", book=book(asks=[("100", "1"), ("101", "2")]))
    assert fills[0].qty == D("3") and order.status == "partial_cancelled"
    assert a.holdings["BTC"] == D("3")


def test_quote_amount_buys_what_the_budget_covers_including_fee():
    a = acct()
    o, fills, _ = E.place_market(a, RULES, SCHED, side="buy", quote_amount=D("500"), book=book(), now=NOW)
    spent = fills[0].notional + fills[0].fee
    assert spent <= D("500") and D("500") - spent < D("101") * D("0.0001") * 2  # within one lot
    assert o.qty == fills[0].qty


def test_sell_realises_pnl_against_average_cost_including_fees():
    a = acct()
    buy(a, "1")  # 1 @ 100, fee 0.8 -> basis 100.8
    _, fills, _ = E.place_market(a, RULES, SCHED, side="sell", qty=D("1"), book=book(bids=[("110", "5")]), now=NOW)
    # proceeds 110 - 0.88 fee = 109.12 ; realised 109.12 - 100.8 = 8.32
    assert fills[0].realized_pnl == D("8.32000")
    assert a.holdings["BTC"] == D("0") and a.basis["BTC"] == D("0")


def test_venue_minimums_and_unknown_quote_are_enforced():
    with pytest.raises(E.Rejected, match="minimum order size"):
        buy(acct(), "0.00001")
    tiny = PairRules("XBTUSD", "BTC", "USD", D("0.0001"), D("500"), D("0.5"), 4, 1)
    with pytest.raises(E.Rejected, match="minimum"):
        E.place_market(acct(), tiny, SCHED, side="buy", qty=D("1"), book=book(), now=NOW)
    eur = PairRules("XBTEUR", "BTC", "EUR", D("0.0001"), D("0.5"), D("0.5"), 4, 1)
    with pytest.raises(E.Rejected, match="USD"):
        E.place_market(acct(), eur, SCHED, side="buy", qty=D("1"), book=book(), now=NOW)


def test_cannot_sell_what_is_not_held():
    with pytest.raises(E.Rejected, match="only 0"):
        E.place_market(acct(), RULES, SCHED, side="sell", qty=D("1"), book=book(), now=NOW)


def test_risk_limits_refuse_oversized_and_overexposed_orders():
    with pytest.raises(E.Rejected, match="per-order limit"):
        buy(acct(order="100"), "2")
    with pytest.raises(E.Rejected, match="per-asset limit"):
        buy(acct(position="150"), "2")
    with pytest.raises(E.Rejected, match="Gross exposure"):
        buy(acct(cash="1000", exposure="10"), "2")
    with pytest.raises(E.Rejected, match="reserve"):
        buy(acct(cash="1000", reserve="90"), "2")


def test_insufficient_cash_is_refused():
    with pytest.raises(E.Rejected, match="cash is free"):
        buy(acct(cash="50"), "1")


def test_client_order_id_makes_a_retry_a_no_op():
    a = acct()
    o1, _, d1 = buy(a, "1", client_id="abc")
    cash = a.cash
    o2, _, d2 = buy(a, "1", client_id="abc")
    assert (d1, d2) == (False, True) and o1.id == o2.id
    assert a.cash == cash and len(a.fills) == 1


# ── limit orders and the queue ──────────────────────────────────────────


def tape(*rows):
    return [Trade(D(p), D(v), NOW + dt, side, str(i)) for i, (p, v, side, dt) in enumerate(rows)]


def test_resting_buy_waits_behind_the_queue_then_fills_as_maker():
    a = acct()
    o, fills, _ = E.place_limit(a, RULES, SCHED, side="buy", qty=D("1"), price=D("99.5"), book=book(), now=NOW)
    assert not fills and o.status == "open" and o.queue_ahead == D("3")
    # 2 sold at our price: eaten by the 3 ahead of us, nothing for us
    assert E.on_trades(a, RULES, tape(("99.5", "2", "s", 1)), SCHED, NOW + 2) == []
    assert o.queue_ahead == D("1")
    # 1.6 more: 1 clears the queue, 0.6 fills us
    f = E.on_trades(a, RULES, tape(("99.5", "1.6", "s", 3)), SCHED, NOW + 4)
    assert [x.qty for x in f] == [D("0.6")] and f[0].liquidity == "maker" and f[0].price == D("99.5")
    assert f[0].fee == E.q5(D("0.6") * D("99.5") * D("0.004"))
    assert o.status == "open" and o.remaining == D("0.4")


def test_replayed_kraken_trade_does_not_fill_a_resting_order_twice():
    a = acct()
    o, _, _ = E.place_limit(a, RULES, SCHED, side="buy", qty=D("1"), price=D("99.5"), book=book(), now=NOW)
    trade = Trade(D("99.5"), D("3.4"), NOW + 1, "s", "kraken-trade-1")
    first = E.on_trades(a, RULES, [trade], SCHED, NOW + 2)
    assert [f.qty for f in first] == [D("0.4")]
    restored = E.account_from_dict(E.account_to_dict(a))
    assert E.on_trades(restored, RULES, [trade], SCHED, NOW + 3) == []
    assert restored.orders[o.id].remaining == D("0.6")
    assert restored.fees_paid == first[0].fee

    # A different Kraken ID is a different trade, even with the same tape values.
    second = Trade(D("99.5"), D("0.3"), NOW + 1, "s", "kraken-trade-2")
    assert [f.qty for f in E.on_trades(restored, RULES, [second], SCHED, NOW + 4)] == [D("0.3")]


def test_a_trade_through_our_price_fills_the_whole_order():
    a = acct()
    o, _, _ = E.place_limit(a, RULES, SCHED, side="buy", qty=D("1"), price=D("99.5"), book=book(), now=NOW)
    f = E.on_trades(a, RULES, tape(("99.0", "0.1", "s", 1)), SCHED, NOW + 2)
    assert f[0].qty == D("1") and f[0].price == D("99.5") and o.status == "filled"


def test_wrong_side_and_pre_order_trades_do_not_fill():
    a = acct()
    o, _, _ = E.place_limit(a, RULES, SCHED, side="buy", qty=D("1"), price=D("99.5"), book=book(), now=NOW)
    assert E.on_trades(a, RULES, tape(("99.0", "5", "b", 1)), SCHED, NOW + 2) == []  # buyer-aggressor
    assert E.on_trades(a, RULES, tape(("99.0", "5", "s", -5)), SCHED, NOW + 2) == []  # before we existed
    assert o.status == "open"


def test_resting_sell_fills_on_buyer_initiated_trades():
    a = acct()
    buy(a, "1")
    o, _, _ = E.place_limit(a, RULES, SCHED, side="sell", qty=D("1"), price=D("100.5"), book=book(), now=NOW)
    f = E.on_trades(a, RULES, tape(("101", "0.2", "b", 1)), SCHED, NOW + 2)
    assert f and f[0].price == D("100.5") and o.status == "filled"


def test_post_only_that_would_cross_is_refused():
    with pytest.raises(E.Rejected, match="Post-only"):
        E.place_limit(acct(), RULES, SCHED, side="buy", qty=D("1"), price=D("100"), book=book(), now=NOW, post_only=True)


def test_marketable_limit_takes_then_rests_the_remainder():
    a = acct()
    o, fills, _ = E.place_limit(a, RULES, SCHED, side="buy", qty=D("3"), price=D("100"), book=book(), now=NOW)
    assert fills[0].qty == D("1") and fills[0].liquidity == "taker"  # only 1 offered at <= 100
    assert o.status == "open" and o.remaining == D("2")


def test_prices_must_sit_on_the_tick():
    with pytest.raises(E.Rejected, match="tick"):
        E.place_limit(acct(), RULES, SCHED, side="buy", qty=D("1"), price=D("99.3"), book=book(), now=NOW)


def test_open_limit_orders_reserve_cash_and_can_be_cancelled():
    a = acct(cash="1000")
    o, _, _ = E.place_limit(a, RULES, SCHED, side="buy", qty=D("9"), price=D("99.5"), book=book(), now=NOW)
    with pytest.raises(E.Rejected, match="cash is free"):
        buy(a, "2")  # ~202 needed, only ~100 is free
    E.cancel(a, o.id)
    buy(a, "2")  # cash is released
    with pytest.raises(E.Rejected, match="already cancelled"):
        E.cancel(a, o.id)


# ── persistence and reports ─────────────────────────────────────────────


def test_account_survives_a_json_round_trip_without_losing_precision():
    import json

    a = acct()
    buy(a, "1.2345")
    E.place_limit(a, RULES, SCHED, side="buy", qty=D("1"), price=D("99.5"), book=book(), now=NOW, client_id="k")
    b = E.account_from_dict(json.loads(json.dumps(E.account_to_dict(a))))
    assert E.account_to_dict(b) == E.account_to_dict(a)
    assert isinstance(b.cash, D) and isinstance(next(iter(b.orders.values())).queue_ahead, D)


def test_cost_report_separates_costs_and_nets_operating_costs():
    a = acct()
    a.costs = E.CostConfig(fixed_daily_usd=D("1"), llm_cost_per_request_usd=D("0.01"))
    buy(a, "1")  # fee 0.8, slippage (100-99.75) = 0.25
    r = E.cost_report(a, {"BTC": D("99.75")}, NOW + 86400, llm_requests=10)
    assert r["costs"]["exchange_fees"] == "0.80000"
    assert r["costs"]["slippage_vs_arrival_mid"] == "0.25000"
    assert r["costs"]["llm_cost"] == "0.10000" and r["costs"]["fixed_running_cost"] == "1.00000"
    # equity: cash 9899.2 + 99.75 = 9998.95 -> equity_pnl -1.05 ; net = -1.05 - 0.10 - 1.00
    assert r["equity_pnl"] == "-1.05000" and r["net_pnl_after_all_costs"] == "-2.15000"
    assert r["gross_pnl_before_execution_costs"] == "0.00000"


def test_cost_report_groups_partial_fills_into_executed_orders():
    a = acct()
    order, _, _ = E.place_limit(
        a, RULES, SCHED, side="buy", qty=D("1"), price=D("99.5"),
        book=book(), now=NOW, client_id="cal-old-BTC-buy",
    )
    E.on_trades(a, RULES, [Trade(D("99.5"), D("3.4"), NOW + 1, "s", "one")], SCHED, NOW + 1)
    E.on_trades(a, RULES, [Trade(D("99.5"), D("0.6"), NOW + 2, "s", "two")], SCHED, NOW + 2)
    rows = E.cost_report(a, {"BTC": D("100")}, NOW + 3)["recent_executed_orders"]
    assert len(rows) == 1
    assert rows[0]["order_id"] == order.id
    assert rows[0]["side"] == "buy" and rows[0]["pair"] == "BTC-USD"
    assert rows[0]["fill_count"] == 2 and D(rows[0]["qty"]) == D("1")
    assert rows[0]["notional"] == "99.50000"
    assert rows[0]["fee"] == str(E.q5(a.fees_paid))
    assert rows[0]["calibration"] is True


def test_depth_impact_estimate_matches_a_manual_walk():
    levels = book()["asks"]
    mid = D("99.75")
    # $302 buys 1 @ 100 + 2 @ 101 -> avg (100+202)/3 = 100.6667
    bps = E.impact_bps(levels, D("302"), mid, "buy")
    assert abs(bps - (D("100.66666667") - mid) / mid * D(10000)) < D("0.01")
    assert E.impact_bps(levels, D("1000000"), mid, "buy") is None
