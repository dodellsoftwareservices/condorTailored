"""Cross-venue arbitrage normalization and executable-depth arithmetic."""

import pytest

from condor.arbitrage import (
    BookError,
    compare_books,
    connector_instrument_class,
    normalize_book,
    split_pair,
    walk_book,
)

NOW = 2_000_000_000.0


def payload(
    *,
    bids=((100, 1), (99, 2)),
    asks=((101, 1), (102, 2)),
    timestamp=NOW,
):
    return {
        "bids": [list(level) for level in bids],
        "asks": [list(level) for level in asks],
        "timestamp": timestamp,
    }


def book(connector="kraken", pair="BTC-USD", **kwargs):
    return normalize_book(connector, pair, payload(**kwargs), now=NOW)


def test_pair_and_instrument_classification():
    assert split_pair("btc-usd") == ("BTC", "USD")
    assert connector_instrument_class("kraken") == "spot"
    assert connector_instrument_class("lighter_perpetual") == "perpetual"
    assert connector_instrument_class("lighter_testnet") == "simulation"
    assert connector_instrument_class("kraken_paper_trade") == "simulation"


@pytest.mark.parametrize(
    "pair", ["BTCUSD", "BTC-USD-PERP", "BTC-BTC", "-USD", "BTC USDC"]
)
def test_bad_pairs_fail(pair):
    with pytest.raises(BookError):
        split_pair(pair)


def test_normalizes_dict_levels_and_iso_timestamp():
    result = normalize_book(
        "coinbase_advanced_trade",
        "BTC-USD",
        {
            "bids": [{"price": "100", "size": "1"}],
            "asks": [{"price": "101", "size": "1"}],
            "time": "2033-05-18T03:33:20Z",
        },
        now=NOW,
    )
    assert result.bids[0].price == 100
    assert result.asks[0].quantity == 1


def test_millisecond_timestamp_is_supported():
    assert book(timestamp=NOW * 1000).source_timestamp == pytest.approx(NOW)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"timestamp": NOW - 31}, "stale"),
        ({"timestamp": NOW + 30}, "future"),
        ({"timestamp": None}, "timestamp"),
        ({"bids": ()}, "no bids"),
        ({"asks": ()}, "no asks"),
        ({"bids": ((102, 1),), "asks": ((101, 1),)}, "crossed"),
        ({"bids": ((100, 1), (101, 1))}, "sorted"),
        ({"asks": ((102, 1), (101, 1))}, "sorted"),
        ({"bids": ((100, 1), (100, 2))}, "duplicate"),
        ({"asks": ((101, 0),)}, "positive"),
    ],
)
def test_bad_books_fail_closed(changes, message):
    with pytest.raises(BookError, match=message):
        book(**changes)


@pytest.mark.parametrize(
    "connector", ["lighter_testnet", "kraken_paper_trade", "x_sandbox"]
)
def test_simulation_books_cannot_be_real_market_signals(connector):
    with pytest.raises(BookError, match="simulation"):
        book(connector)


def test_walk_book_uses_multiple_levels_and_base_quantity():
    levels = book().asks
    fill = walk_book(levels, 2)
    assert fill.average_price == pytest.approx(101.5)
    assert fill.gross_quote == pytest.approx(203)
    assert fill.levels_used == 2


def test_walk_book_refuses_to_extrapolate_missing_depth():
    with pytest.raises(BookError, match="insufficient displayed depth"):
        walk_book(book().asks, 10)


def test_zero_fee_route_uses_both_real_book_sides():
    cheaper = book("kraken", asks=((100, 2),), bids=((99, 2),))
    richer = book("gemini", asks=((102, 2),), bids=((101, 2),))
    routes = compare_books(
        [cheaper, richer],
        quantity=1,
        fee_bps={"kraken": 0, "gemini": 0},
        buffer_bps=0,
    )
    assert len(routes) == 1
    route = routes[0]
    assert route.buy_connector == "kraken"
    assert route.sell_connector == "gemini"
    assert route.gross_bps == pytest.approx(100)
    assert route.modeled_net_quote == pytest.approx(1)


def test_both_leg_fees_and_buffer_are_subtracted_from_quote_value():
    cheaper = book("kraken", asks=((100, 2),), bids=((99, 2),))
    richer = book("gemini", asks=((103, 2),), bids=((102, 2),))
    route = compare_books(
        [cheaper, richer],
        quantity=1,
        fee_bps={"kraken": 25, "gemini": 40},
        buffer_bps=10,
    )[0]
    # Gross $2, less 0.25 + 0.408 fees and 0.10 latency buffer.
    assert route.modeled_net_quote == pytest.approx(2 - 0.25 - 0.408 - 0.10)
    assert route.fees_bps == 65
    assert route.modeled_net_bps < route.gross_bps


def test_min_net_threshold_is_applied_after_costs():
    cheaper = book("kraken", asks=((100, 2),), bids=((99, 2),))
    richer = book("gemini", asks=((102, 2),), bids=((101, 2),))
    assert not compare_books(
        [cheaper, richer],
        quantity=1,
        fee_bps={"kraken": 50, "gemini": 50},
        min_net_bps=1,
    )


def test_missing_explicit_fee_fails_instead_of_assuming_zero():
    with pytest.raises(BookError, match="no explicit taker fee"):
        compare_books([book("kraken"), book("gemini")], quantity=1, fee_bps={})


def test_excessive_book_skew_produces_no_signal():
    first = book("kraken", timestamp=NOW - 10)
    second = book("gemini", timestamp=NOW)
    assert not compare_books(
        [first, second],
        quantity=1,
        fee_bps={"kraken": 0, "gemini": 0},
        max_skew_seconds=5,
    )


def test_mixed_spot_and_perpetual_books_are_never_compared():
    with pytest.raises(BookError, match="identical pairs"):
        compare_books(
            [book("kraken"), book("lighter_perpetual", pair="BTC-USD")],
            quantity=1,
            fee_bps={"kraken": 0, "lighter_perpetual": 0},
        )


def test_different_quote_currencies_are_never_compared():
    with pytest.raises(BookError, match="identical pairs"):
        compare_books(
            [book("kraken", pair="BTC-USD"), book("lighter", pair="BTC-USDC")],
            quantity=1,
            fee_bps={"kraken": 0, "lighter": 0},
        )
