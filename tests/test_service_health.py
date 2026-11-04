import asyncio

from condor.service_health import ServiceHealth, register_jobs


def test_outage_is_aggregated_escalated_then_cleared_on_recovery(monkeypatch):
    notices = []

    async def record(user_id, text, **kwargs):
        notices.append((user_id, text, kwargs["title"]))

    monkeypatch.setattr("condor.notifications.record", record)
    clock = [100.0]
    health = ServiceHealth(lambda: clock[0])

    async def scenario():
        for _ in range(8):
            await health.report(1, "Trading backend local", False)
        assert len(notices) == 2
        assert notices[1][2].startswith("Action needed")
        clock[0] += 901
        await health.report(1, "Trading backend local", False)
        assert len(notices) == 3
        await health.report(1, "Trading backend local", True)
        await health.report(1, "Trading backend local", True)
        assert len(notices) == 4
        assert "recovered" in notices[-1][2]
        await health.report(1, "Trading backend local", False)
        assert len(notices) == 5

    asyncio.run(scenario())


def test_periodic_probe_covers_each_backend_without_a_running_routine(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    import config_manager

    queue = Mock()
    queue.get_jobs_by_name.return_value = []
    register_jobs(SimpleNamespace(job_queue=queue))
    callback = queue.run_repeating.call_args.args[0]
    assert queue.run_repeating.call_args.kwargs["interval"] == 60
    cm = SimpleNamespace(
        list_servers=lambda: {"one": {}, "two": {}}, get_client=AsyncMock()
    )
    monkeypatch.setattr(config_manager, "get_config_manager", lambda: cm)
    asyncio.run(callback(None))
    assert cm.get_client.await_count == 2


def test_a_stalled_notification_sink_does_not_stall_market_health(monkeypatch):
    import condor.service_health as service_health

    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("condor.notifications.record", stalled)
    monkeypatch.setattr(service_health, "NOTICE_TIMEOUT_SECONDS", 0.01)
    health = ServiceHealth()

    async def scenario():
        await asyncio.wait_for(health.report(1, "feed", False), timeout=1)
        await asyncio.wait_for(health.report(1, "feed", True), timeout=1)

    asyncio.run(scenario())


def test_cleanup_failure_does_not_hide_the_original_backend_outage(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from config_manager import ConfigManager

    cm = ConfigManager(str(tmp_path / "config.yml"))
    cm._data["servers"] = {
        "local": {
            "host": "localhost",
            "port": 8000,
            "username": "test",
            "password": "test",
        }
    }
    client = SimpleNamespace(
        init=AsyncMock(),
        accounts=SimpleNamespace(
            list_accounts=AsyncMock(side_effect=ConnectionError("offline"))
        ),
        close=AsyncMock(side_effect=RuntimeError("cleanup failed")),
    )
    reporter = AsyncMock()
    monkeypatch.setattr(cm, "_report_backend_health", reporter)

    async def scenario():
        import pytest

        with pytest.raises(ConnectionError, match="offline"):
            await cm._get_or_create_client("local", lambda **kwargs: client)

    asyncio.run(scenario())
    reporter.assert_awaited_once_with("local", False)
    assert "local" in cm._client_failures


def test_cancelled_acquisition_closes_the_client_and_preserves_cancellation(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest

    from config_manager import ConfigManager

    cm = ConfigManager(str(tmp_path / "config.yml"))
    cm._data["servers"] = {
        "local": {
            "host": "localhost",
            "port": 8000,
            "username": "test",
            "password": "test",
        }
    }
    client = SimpleNamespace(
        init=AsyncMock(side_effect=asyncio.CancelledError()),
        close=AsyncMock(side_effect=RuntimeError("cleanup failed")),
    )

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await cm._get_or_create_client("local", lambda **kwargs: client)

    asyncio.run(scenario())
    client.close.assert_awaited_once()
    assert "local" not in cm._client_failures


def test_price_monitor_reports_failures_before_the_continuous_run_ends(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from routines import price_monitor

    feed = AsyncMock(
        side_effect=[ConnectionError("offline"), 100.0, asyncio.CancelledError()]
    )
    monkeypatch.setattr(price_monitor, "get_client", AsyncMock(return_value=object()))
    monkeypatch.setattr(price_monitor, "fetch_current_price", feed)
    reporter = SimpleNamespace(report=AsyncMock())
    monkeypatch.setattr(price_monitor, "health", reporter)

    class Report:
        def __init__(self, *args, **kwargs):
            self.builder = self

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

        async def update(self):
            pass

    monkeypatch.setattr("condor.reports.LiveReport", Report)
    context = SimpleNamespace(
        _chat_id=1, server_name="local", bot=SimpleNamespace(send_message=AsyncMock())
    )
    asyncio.run(price_monitor.run(price_monitor.Config(interval_sec=0), context))
    assert [call.args[2] for call in reporter.report.await_args_list] == [False, True]
