"""The simulated-exchange market maker: quote math, requoting, inventory, loss halt."""

import asyncio
from decimal import Decimal as D

import pytest

from condor.sim_exchange import engine as E
from condor.sim_exchange.kraken_public import PairRules, Trade
from condor.sim_exchange.market_maker import MarketMaker, MMConfig, compute_quotes
from condor.sim_exchange.service import SimService

RULES = PairRules("XBTUSD", "BTC", "USD", D("0.0001"), D("0.5"), D("0.5"), 4, 1)
T0 = 1_800_000_000.0


def run(coro):
    return asyncio.run(coro)


def book(bid="99.5", ask="100"):
    return {"bids": [(D(bid), D("50"))], "asks": [(D(ask), D("50"))], "ts": T0}


class FakeKraken:
    def __init__(self):
        self.bid, self.ask = "99.5", "100"
        self.tape = []

    async def pair_rules(self, pair):
        return RULES

    async def depth(self, alt, count=500):
        return book(self.bid, self.ask)

    async def ticker(self, alt):
        mid = (D(self.bid) + D(self.ask)) / 2
        return {
            "bid": D(self.bid),
            "ask": D(self.ask),
            "last": mid,
            "volume_24h": D("5"),
            "vwap_24h": mid,
            "trades_24h": 9,
            "high_24h": mid,
            "low_24h": mid,
        }

    async def trades(self, alt, since=None):
        out, self.tape = self.tape, []
        return out, "1"

    async def ohlc(self, alt, interval=15):
        return []


def make(tmp_path, **cfg):
    k = FakeKraken()
    clock = iter(float(T0 + i) for i in range(100_000))

    async def nosleep(_):
        return None

    svc = SimService(
        k,
        tmp_path / "mm.json",
        E.FeeSchedule.load(),
        clock=lambda: next(clock),
        sleeper=nosleep,
        start_cash=D("10000"),
    )
    base = {"pairs": ["BTC-USD"], "max_quote_age_sec": 10**9, "refresh_sec": 5}
    base.update(cfg)
    mm = MarketMaker(svc, MMConfig.from_dict(base), clock=svc._clock)
    return mm, svc, k


# ── quote math (pure) ────────────────────────────────────────────────────


def test_fee_aware_half_spread_covers_the_maker_fee_plus_margin():
    cfg = MMConfig.from_dict({"pairs": ["BTC-USD"]})
    q = compute_quotes(cfg, RULES, book(), maker_rate=D("0.004"), free_base=D("0"))
    assert q.half_spread_bps == D("45")  # 40 bps fee + 10/2 margin
    assert (
        q.bid_price <= D("99.5") and q.ask_price is None
    )  # spot cannot short: no ask without inventory
    assert q.bid_price % D("0.5") == 0  # on the tick


def test_quotes_never_sit_inside_the_touch_even_with_a_tiny_spread_setting():
    cfg = MMConfig.from_dict({"pairs": ["BTC-USD"], "half_spread_bps": "1"})
    q = compute_quotes(cfg, RULES, book(), maker_rate=D("0.004"), free_base=D("1"))
    assert q.bid_price <= D("99.5") and q.ask_price >= D("100")


def test_inventory_leans_quotes_down_and_stops_the_bid_at_the_cap():
    cfg = MMConfig.from_dict(
        {
            "pairs": ["BTC-USD"],
            "half_spread_bps": "100",
            "max_inventory_notional": "1000",
        }
    )
    flat = compute_quotes(cfg, RULES, book(), maker_rate=D("0.004"), free_base=D("0"))
    long_ = compute_quotes(cfg, RULES, book(), maker_rate=D("0.004"), free_base=D("5"))
    assert long_.bid_price < flat.bid_price  # wants to buy less
    full = compute_quotes(
        cfg, RULES, book(), maker_rate=D("0.004"), free_base=D("10")
    )  # $1,000 held
    assert full.bid_price is None and full.ask_price is not None


def test_ask_size_never_exceeds_what_is_held():
    cfg = MMConfig.from_dict(
        {"pairs": ["BTC-USD"], "half_spread_bps": "50", "order_notional": "250"}
    )
    q = compute_quotes(cfg, RULES, book(), maker_rate=D("0.004"), free_base=D("0.5"))
    assert q.ask_qty == D("0.5")


def test_bad_config_is_refused():
    for bad in (
        {"pairs": ["BTC-EUR"]},
        {"refresh_sec": 0.5},
        {"order_notional": "5000", "max_inventory_notional": "100"},
        {"half_spread_bps": "0"},
    ):
        with pytest.raises(ValueError):
            MMConfig.from_dict(bad)


# ── the loop ─────────────────────────────────────────────────────────────


def open_mm(svc):
    return [
        o for o in run(svc.orders())["open"] if o["client_order_id"].startswith("mm-")
    ]


def test_first_cycle_posts_a_post_only_bid_and_a_steady_market_is_left_alone(tmp_path):
    mm, svc, _ = make(tmp_path)
    run(mm.step())
    orders = open_mm(svc)
    assert [o["side"] for o in orders] == ["buy"] and orders[0]["post_only"] is True
    placed, cancelled = mm.stats.quotes_placed, mm.stats.quotes_cancelled
    run(mm.step())
    assert (
        mm.stats.quotes_placed == placed and mm.stats.quotes_cancelled == cancelled
    )  # no churn


def test_a_drifted_quote_is_cancelled_and_replaced(tmp_path):
    mm, svc, k = make(tmp_path)
    run(mm.step())
    first = open_mm(svc)[0]["id"]
    k.bid, k.ask = "96", "96.5"  # market falls ~3%
    run(mm.step())
    now = open_mm(svc)
    assert len(now) == 1 and now[0]["id"] != first and D(now[0]["price"]) < D("96.5")
    assert mm.stats.quotes_cancelled == 1


def test_a_fill_builds_inventory_then_the_maker_quotes_an_ask(tmp_path):
    mm, svc, k = make(tmp_path)
    run(mm.step())
    bid = D(open_mm(svc)[0]["price"])
    k.tape = [Trade(bid - 1, D("500"), T0 + 99_000, "s", "1")]  # a sale through our bid
    run(mm.step())
    assert mm.stats.maker_fills == 1 and mm.stats.buys == 1 and mm.stats.fees > 0
    sides = sorted(o["side"] for o in open_mm(svc))
    assert "sell" in sides  # now holds inventory, so it offers it


def test_loss_limit_halts_and_pulls_every_quote(tmp_path):
    mm, svc, k = make(tmp_path, max_loss_usd="20")
    run(mm.step())
    bid = D(open_mm(svc)[0]["price"])
    k.tape = [Trade(bid - 1, D("500"), T0 + 99_000, "s", "1")]
    run(mm.step())  # buys ~2.5 BTC
    k.bid, k.ask = "80", "80.5"  # a 20% crash on the inventory
    out = run(mm.step())
    assert "loss limit" in out["halted"]
    assert open_mm(svc) == []
    assert run(mm.step())["halted"]  # stays halted


# ── runner and routes ────────────────────────────────────────────────────


def test_runner_loops_stops_and_pulls_its_quotes(tmp_path):
    from condor.sim_exchange.market_maker import MMRunner

    async def go():
        k = FakeKraken()

        async def nosleep(_):
            return None

        svc = SimService(
            k,
            tmp_path / "r.json",
            E.FeeSchedule.load(),
            sleeper=nosleep,
            start_cash=D("10000"),
        )
        runner = MMRunner(svc)
        await runner.start(MMConfig.from_dict({"pairs": ["BTC-USD"], "refresh_sec": 2}))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if runner.maker.stats.quotes_placed:
                break
        with pytest.raises(RuntimeError):
            await runner.start(
                MMConfig.from_dict({"pairs": ["BTC-USD"]})
            )  # one maker at a time
        st = await runner.status()
        out = await runner.stop()
        left = [
            o
            for o in (await svc.orders())["open"]
            if o["client_order_id"].startswith("mm-")
        ]
        return st, out, left

    st, out, left = run(go())
    assert st["running"] is True and st["quotes_placed"] >= 1
    assert out["quotes_pulled"] >= 1 and left == []


def test_runner_survives_a_timeout_and_a_stray_error_but_halts_if_it_never_clears(
    tmp_path,
):
    from condor.sim_exchange.market_maker import MMRunner

    async def go(fail_forever):
        k = FakeKraken()

        async def nosleep(_):
            return None

        svc = SimService(
            k,
            tmp_path / ("f.json" if fail_forever else "s.json"),
            E.FeeSchedule.load(),
            sleeper=nosleep,
            start_cash=D("10000"),
        )
        runner = MMRunner(svc)
        runner.MAX_CONSECUTIVE_FAILURES = 3
        maker = MarketMaker(
            svc, MMConfig.from_dict({"pairs": ["BTC-USD"], "refresh_sec": 2})
        )
        maker.cfg.refresh_sec = (
            0.01  # validated at construction; shrink afterwards so the test is fast
        )
        real_step, calls = maker.step, {"n": 0}

        async def flaky():
            calls["n"] += 1
            if fail_forever or calls["n"] <= 2:
                raise (
                    RuntimeError("boom") if calls["n"] % 2 else asyncio.TimeoutError()
                )
            return await real_step()

        maker.step = flaky
        runner.maker = maker
        runner._task = asyncio.create_task(runner._loop(maker))
        for _ in range(400):
            await asyncio.sleep(0.02)
            if maker.halted or maker.stats.quotes_placed:
                break
        out = (maker.halted, maker.stats.errors, maker.stats.quotes_placed)
        await runner.stop()
        return out

    halted, errors, placed = run(go(False))
    assert (
        not halted and errors >= 2 and placed >= 1
    )  # two blips, then it carried on quoting
    halted, errors, placed = run(go(True))
    assert (
        halted == "crashed: see logs" and errors >= 3
    )  # never clears -> quotes pulled, loudly halted


def test_mm_routes_exist_and_every_one_demands_an_admin():
    import inspect

    from condor.web.routes import sim_exchange as routes

    mm = [r for r in routes.router.routes if "/mm" in r.path]
    assert {r.path for r in mm} >= {
        "/sim/kraken/mm",
        "/sim/kraken/mm/start",
        "/sim/kraken/mm/stop",
        "/sim/kraken/mm/portfolio",
    }
    for r in mm:
        assert "require_admin(user)" in inspect.getsource(r.endpoint), r.path


def test_autostart_file_exists_only_while_running_and_a_restart_resumes_it(tmp_path):
    from condor.sim_exchange.market_maker import MMRunner

    flag = tmp_path / "mm_autostart.json"

    async def go():
        async def nosleep(_):
            return None

        def new_runner(name):
            svc = SimService(
                FakeKraken(),
                tmp_path / name,
                E.FeeSchedule.load(),
                sleeper=nosleep,
                start_cash=D("10000"),
            )
            return MMRunner(svc, autostart_path=flag)

        r1 = new_runner("a.json")
        assert await r1.resume_from_disk() is False  # nothing to resume
        await r1.start(
            MMConfig.from_dict(
                {"pairs": ["BTC-USD", "ETH-USD"], "order_notional": "100"}
            )
        )
        assert flag.exists()
        r2 = new_runner("b.json")  # a new process after a restart
        assert await r2.resume_from_disk() is True and r2.maker.cfg.pairs == (
            "BTC-USD",
            "ETH-USD",
        )
        assert r2.maker.cfg.order_notional == D("100")
        await r2.stop()
        assert not flag.exists()  # stopped on purpose: stays stopped
        await r1.stop()
        r3 = new_runner("c.json")
        assert await r3.resume_from_disk() is False

        # a halted maker (loss limit) must not come back either
        r4 = new_runner("d.json")
        await r4.start(MMConfig.from_dict({"pairs": ["BTC-USD"]}))
        r4.maker.halted = "loss limit: test"
        for _ in range(100):
            await asyncio.sleep(0.02)
            if not flag.exists():
                break
        assert not flag.exists()
        await r4.stop()

    run(go())
