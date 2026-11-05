"""Scoring of the paper basket against its pre-registered OKRs."""

from datetime import date

from routines.paper_okr_report import evaluate, format_report

START = date(2026, 11, 3)


def journal(**over):
    j = {
        "objective": "test",
        "window": {"start": "2026-11-03", "end": "2026-12-03"},
        "order_cap": 2500,
        "exit_review_pct": -0.10,
        "benchmark": {"symbol": "SPY", "start_price": 100.0},
        "key_results": {
            "KR1": {"name": "excess", "target": 0.0},
            "KR2": {"name": "hit", "target": 0.5},
            "KR3": {"name": "dd", "target": 0.08},
            "KR4": {"name": "process", "target": 1.0},
            "KR5": {"name": "coverage", "target": 0.9},
        },
        "approved_symbols": ["AAA", "BBB"],
        "positions": {"AAA": {"thesis": "x"}, "BBB": {"thesis": "y"}},
        "orders": [
            {"symbol": "AAA", "notional": 2500.0},
            {"symbol": "BBB", "notional": 2500.0},
        ],
        "snapshots": [],
        "caveat": "small sample",
    }
    j.update(over)
    return j


def pos(symbol, cost, ret):
    return {
        "symbol": symbol,
        "market_value": cost * (1 + ret),
        "unrealized_pl": cost * ret,
    }


def test_excess_return_is_basket_minus_spy():
    ev = evaluate(
        journal(),
        [pos("AAA", 2500, 0.05), pos("BBB", 2500, 0.03)],
        102.0,
        date(2026, 11, 20),
    )
    assert abs(ev["basket_ret"] - 0.04) < 1e-9
    assert abs(ev["spy_ret"] - 0.02) < 1e-9
    assert abs(ev["excess"] - 0.02) < 1e-9
    assert ev["kr"]["KR1"] == "ON TRACK"


def test_hit_rate_and_losing_basket_flagged_after_early_window():
    ev = evaluate(
        journal(),
        [pos("AAA", 2500, -0.02), pos("BBB", 2500, -0.01)],
        100.0,
        date(2026, 11, 20),
    )
    assert ev["hit_rate"] == 0.0
    assert ev["kr"]["KR2"] == "OFF TRACK"
    assert ev["kr"]["KR1"] == "OFF TRACK"


def test_first_days_are_too_early_to_judge():
    ev = evaluate(
        journal(),
        [pos("AAA", 2500, -0.002), pos("BBB", 2500, -0.001)],
        100.0,
        date(2026, 11, 4),
    )
    assert ev["kr"]["KR1"] == "TOO EARLY"
    assert ev["kr"]["KR2"] == "TOO EARLY"


def test_position_at_minus_ten_percent_triggers_exit_review():
    ev = evaluate(
        journal(),
        [pos("AAA", 2500, -0.10), pos("BBB", 2500, 0.10)],
        100.0,
        date(2026, 11, 20),
    )
    assert ev["breached"] == ["AAA"]
    assert ev["kr"]["KR3"] == "WATCH"
    assert "EXIT REVIEW: AAA" in format_report(journal(), ev, None)


def test_drawdown_uses_snapshot_peak():
    snaps = [
        {"date": "2026-11-05", "basket_value": 5400.0},
        {"date": "2026-11-06", "basket_value": 5000.0},
    ]
    ev = evaluate(
        journal(snapshots=snaps),
        [pos("AAA", 2500, 0.0), pos("BBB", 2500, 0.0)],
        100.0,
        date(2026, 11, 7),
    )
    assert abs(ev["max_dd"] - (5000 / 5400 - 1)) < 1e-9
    assert ev["kr"]["KR3"] == "ON TRACK"  # -7.4% is inside the 8% limit


def test_drawdown_beyond_limit_is_off_track():
    snaps = [{"date": "2026-11-05", "basket_value": 5500.0}]
    ev = evaluate(
        journal(snapshots=snaps),
        [pos("AAA", 2500, -0.04), pos("BBB", 2500, -0.04)],
        100.0,
        date(2026, 11, 7),
    )
    assert ev["max_dd"] < -0.08
    assert ev["kr"]["KR3"] == "OFF TRACK"


def test_process_kr_catches_over_cap_unrecorded_and_off_list():
    j = journal(orders=[{"symbol": "AAA", "notional": 2600.0}])
    ev = evaluate(
        j, [pos("AAA", 2500, 0.0), pos("ZZZ", 1000, 0.0)], 100.0, date(2026, 11, 20)
    )
    assert ev["over_cap"] == ["AAA"]
    assert ev["unrecorded"] == ["ZZZ"]
    assert ev["off_list"] == ["ZZZ"]
    assert ev["kr"]["KR4"] == "OFF TRACK"


def test_coverage_counts_distinct_snapshot_days():
    snaps = [
        {"date": "2026-11-03", "basket_value": 1.0},
        {"date": "2026-11-04", "basket_value": 1.0},
    ]
    ev = evaluate(
        journal(snapshots=snaps), [pos("AAA", 2500, 0.0)], 100.0, date(2026, 11, 5)
    )
    assert abs(ev["coverage"] - 2 / 3) < 1e-9  # Tue, Wed, Thu elapsed; 2 captured
    assert ev["kr"]["KR5"] == "OFF TRACK"


def test_missing_benchmark_gives_no_excess_instead_of_a_guess():
    ev = evaluate(
        journal(benchmark={"symbol": "SPY", "start_price": None}),
        [pos("AAA", 2500, 0.01)],
        100.0,
        date(2026, 11, 20),
    )
    assert ev["excess"] is None
    assert ev["kr"]["KR1"] == "N/A"
