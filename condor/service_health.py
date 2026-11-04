"""Aggregate backend/feed outages across routines and announce recovery."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)
NOTICE_TIMEOUT_SECONDS = 2


@dataclass
class Outage:
    checks: int
    started: float
    last_notice: float


class ServiceHealth:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._outages: dict[tuple[int, str], Outage] = {}

    async def report(self, user_id: int | None, service: str, healthy: bool) -> None:
        if not user_id:
            return
        key = (user_id, service)
        now = self._clock()
        outage = self._outages.get(key)
        if healthy:
            if outage is None:
                return
            del self._outages[key]
            await self._notify(
                user_id,
                f"{service} is responding again after {int(now - outage.started)}s.",
                title="Market service recovered",
                link="/settings",
                kind="system",
            )
            return
        if outage is None:
            outage = self._outages[key] = Outage(0, now, now)
        outage.checks += 1
        if outage.checks not in (1, 3) and now - outage.last_notice < 900:
            return
        outage.last_notice = now
        persistent = outage.checks >= 3
        await self._notify(
            user_id,
            f"{service} {'remains unavailable' if persistent else 'is unavailable'}. "
            f"Failed checks: {outage.checks}. Check the server, credentials, or feed; run make doctor for backend diagnostics.",
            title=(
                "Action needed: market service unavailable"
                if persistent
                else "Market service unavailable"
            ),
            link="/settings",
            kind="system",
        )

    async def _notify(self, user_id, text, **kwargs):
        from condor.notifications import record

        try:
            # Notification push sinks must not hold up client acquisition or
            # turn a good market-data tick into another failure.
            await asyncio.wait_for(
                record(user_id, text, **kwargs), NOTICE_TIMEOUT_SECONDS
            )
        except Exception:
            log.warning(
                "Health notification could not be fully delivered", exc_info=True
            )


health = ServiceHealth()
_JOB = "backend_health"


async def _probe_backends(context):
    from config_manager import get_config_manager

    cm = get_config_manager()
    semaphore = asyncio.Semaphore(3)

    async def probe(name):
        async with semaphore:
            try:
                await cm.get_client(name)
            except Exception:
                # Client acquisition reports the aggregate failure itself.
                log.debug("Backend health check failed for %s", name)

    await asyncio.gather(*(probe(name) for name in cm.list_servers()))


def register_jobs(application):
    queue = getattr(application, "job_queue", None)
    if queue is None:
        return
    for job in queue.get_jobs_by_name(_JOB):
        job.schedule_removal()
    queue.run_repeating(
        _probe_backends,
        interval=60,
        first=20,
        name=_JOB,
        job_kwargs={"max_instances": 1, "coalesce": True},
    )
