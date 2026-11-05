"""Paper split reconciliation must not turn a share-count error into a loss."""

import asyncio
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from condor import alpaca_paper as A
from condor.alpaca_paper import AlpacaPaperClient, Position
from condor.paper_valuation import reconcile

ITA_SPLIT = {
    "ca_type": "split",
    "target_symbol": "ITA",
    "ex_date": "2026-11-05",
    "old_rate": "1",
    "new_rate": "3",
}
FILL = {
    "symbol": "ITA",
    "side": "buy",
    "qty": "11.697501403",
    "transaction_time": "2026-11-04T17:01:11Z",
}


def test_unapplied_forward_split_corrects_only_displayed_paper_value():
    raw = Position("ITA", 11.697501403, 794.37732, -1705.61268)
    rows, correction, notes, reliable = reconcile(
        [raw], [FILL], True, [ITA_SPLIT], date(2026, 11, 5)
    )
    assert reliable
    assert rows[0].qty == pytest.approx(raw.qty * 3)
    assert rows[0].market_value == pytest.approx(raw.market_value * 3)
    assert rows[0].unrealized_pl == pytest.approx(-116.85804)
    assert correction == pytest.approx(raw.market_value * 2)
    assert "Alpaca Paper" in notes[0]
    assert raw.qty == 11.697501403  # broker data is never mutated


def test_already_applied_split_is_never_doubled():
    adjusted = Position("ITA", 11.697501403 * 3, 794.37732 * 3, -116.85804)
    rows, correction, notes, reliable = reconcile(
        [adjusted], [FILL], True, [ITA_SPLIT], date(2026, 11, 5)
    )
    assert reliable and correction == 0 and rows == [adjusted] and not notes


def test_unverified_extreme_paper_move_withholds_valuation():
    raw = Position("ITA", 11.697501403, 794.37732, -1705.61268)
    _, _, notes, reliable = reconcile([raw], [FILL], False, None, date(2026, 11, 5))
    assert not reliable
    assert "withheld" in notes[0]


def test_announced_split_blocks_paper_buy_before_post(monkeypatch):
    client = AlpacaPaperClient("test-key", "test-secret")
    tomorrow = (
        datetime.now(ZoneInfo("America/New_York")).date() + timedelta(days=1)
    ).isoformat()
    monkeypatch.setattr(
        client,
        "get_split_announcements",
        AsyncMock(return_value=[{**ITA_SPLIT, "ex_date": tomorrow}]),
    )
    post = AsyncMock(return_value={"id": "must-not-exist"})
    monkeypatch.setattr(client, "_request", post)
    with pytest.raises(ValueError, match="announced recent/upcoming split"):
        asyncio.run(client.submit_notional_order("ITA", 2500, "buy"))
    post.assert_not_awaited()


def test_observed_split_remains_available_after_feed_window_moves(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(A, "data_dir", lambda: tmp_path)
    first = A._remember_splits([ITA_SPLIT])
    assert first == [ITA_SPLIT]
    assert A._remember_splits([]) == [ITA_SPLIT]
