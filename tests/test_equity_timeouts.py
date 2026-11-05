"""Request bounds and worker capacity persist after a caller times out."""

import asyncio
import threading
from unittest.mock import patch

import pytest
import requests

from condor import equity_market_data as data


@pytest.mark.parametrize(
    "supplied,expected",
    [(None, 20), (30, 20), (5, 5), ((None, 60), (20, 20)), ((2, 10), (2, 10))],
)
def test_request_timeout_cannot_disable_or_exceed_ceiling(supplied, expected):
    with patch.object(requests.Session, "request") as send:
        data._TimeoutSession().get("https://example.invalid", timeout=supplied)
    assert send.call_args.kwargs["timeout"] == expected


def test_worker_slots_stay_occupied_until_the_actual_threads_finish(monkeypatch):
    monkeypatch.setattr(data, "_OPERATION_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(
        data, "_worker_slots", threading.BoundedSemaphore(data._WORKER_COUNT)
    )
    finish = threading.Event()
    started = threading.Barrier(data._WORKER_COUNT)

    def blocked():
        started.wait(timeout=2)
        finish.wait(timeout=3)
        return "late result"

    async def scenario():
        try:
            results = await asyncio.gather(
                *(data._bounded_sync_fetch(blocked) for _ in range(data._WORKER_COUNT)),
                return_exceptions=True,
            )
            assert all(isinstance(result, TimeoutError) for result in results)
            with pytest.raises(data.EquityDataError, match="busy"):
                await data._bounded_sync_fetch(lambda: "should not run")
            # The process's default executor remains available.
            assert await asyncio.to_thread(lambda: "responsive") == "responsive"
        finally:
            finish.set()
            for _ in range(200):
                if data._worker_slots._value == data._WORKER_COUNT:
                    break
                await asyncio.sleep(0.005)
        assert await data._bounded_sync_fetch(lambda: "recovered") == "recovered"

    asyncio.run(scenario())
