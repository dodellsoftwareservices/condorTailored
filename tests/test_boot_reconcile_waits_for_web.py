"""Loops resumed at boot call back into this process, so they wait for its port."""

import asyncio
import socket
from types import SimpleNamespace

import main


def run(coro):
    return asyncio.run(coro)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def point_at(monkeypatch, port, host="127.0.0.1"):
    monkeypatch.setattr("utils.config.WEB_PORT", port)
    monkeypatch.setattr("utils.config.WEB_HOST", host)


def test_the_wait_returns_true_as_soon_as_the_port_accepts_connections(monkeypatch):
    async def scenario():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        point_at(monkeypatch, server.sockets[0].getsockname()[1])
        try:
            return await main._wait_for_local_web(timeout=5, interval=0.05)
        finally:
            server.close()

    assert run(scenario()) is True


def test_a_wildcard_bind_address_is_probed_on_loopback(monkeypatch):
    async def scenario():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        point_at(monkeypatch, server.sockets[0].getsockname()[1], host="0.0.0.0")
        try:
            return await main._wait_for_local_web(timeout=5, interval=0.05)
        finally:
            server.close()

    assert run(scenario()) is True


def test_the_wait_gives_up_when_nothing_ever_listens(monkeypatch):
    point_at(monkeypatch, free_port())
    assert run(main._wait_for_local_web(timeout=0.3, interval=0.05)) is False


def test_the_wait_notices_a_server_that_comes_up_late(monkeypatch):
    port = free_port()
    point_at(monkeypatch, port)

    async def scenario():
        waiter = asyncio.create_task(main._wait_for_local_web(timeout=5, interval=0.05))
        await asyncio.sleep(0.3)
        assert not waiter.done()  # still waiting while nothing listens
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", port)
        try:
            return await asyncio.wait_for(waiter, 5)
        finally:
            server.close()

    assert run(scenario()) is True


def test_loops_are_reconciled_only_after_the_web_server_is_up(monkeypatch):
    """Regression: reconcile ran before uvicorn existed, so every resumed loop failed
    its first tick calling the simulator API and the restart was reported as failed."""
    order = []
    gate = asyncio.Event

    async def scenario():
        opened = gate()

        async def wait_for_web(*a, **k):
            order.append("waiting")
            await opened.wait()
            order.append("web up")
            return True

        async def reconcile_boot():
            order.append("reconcile")
            return SimpleNamespace(total=0, orphaned_executors=[], restarted=[])

        monkeypatch.setattr(main, "_wait_for_local_web", wait_for_web)
        monkeypatch.setattr(
            "condor.runtime.loops.get_supervisor",
            lambda: SimpleNamespace(reconcile_boot=reconcile_boot),
        )
        task = asyncio.create_task(main._reconcile_loops(object()))
        await asyncio.sleep(0.1)
        assert order == ["waiting"]  # nothing reconciled while the port is closed
        opened.set()
        await task

    run(scenario())
    assert order == ["waiting", "web up", "reconcile"]


def test_a_port_that_never_opens_still_settles_the_loops(monkeypatch):
    called = []

    async def never(*a, **k):
        return False

    async def reconcile_boot():
        called.append(1)
        return SimpleNamespace(total=0, orphaned_executors=[], restarted=[])

    monkeypatch.setattr(main, "_wait_for_local_web", never)
    monkeypatch.setattr(
        "condor.runtime.loops.get_supervisor",
        lambda: SimpleNamespace(reconcile_boot=reconcile_boot),
    )
    run(main._reconcile_loops(object()))
    assert called == [1]  # settling states matters even if resumed ticks may fail


def test_a_reconcile_that_raises_never_takes_startup_down(monkeypatch):
    async def ready(*a, **k):
        return True

    async def boom():
        raise RuntimeError("supervisor fell over")

    monkeypatch.setattr(main, "_wait_for_local_web", ready)
    monkeypatch.setattr(
        "condor.runtime.loops.get_supervisor",
        lambda: SimpleNamespace(reconcile_boot=boom),
    )
    run(main._reconcile_loops(object()))  # logs, does not raise
