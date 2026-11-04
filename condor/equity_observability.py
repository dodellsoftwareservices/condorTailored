"""Local, bounded equity metrics. Never stores payloads, symbols, or credentials.

One instance per process; counters reset on restart. Provider adapters and cache /
socket services can use the same recorder without importing the web application.
"""

from __future__ import annotations

import math
import threading
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
from enum import StrEnum

equity_correlation_id: ContextVar[str | None] = ContextVar(
    "equity_correlation_id", default=None
)


class EquityEvent(StrEnum):
    PROVIDER_OK = "provider_ok"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_TIMEOUT = "provider_timeout"
    CACHE_HIT = "cache_hit"
    CACHE_MISS = "cache_miss"
    STALE_QUOTE_REJECTED = "stale_quote_rejected"
    WS_RECONNECT = "ws_reconnect"
    ORDER_SUBMISSION = "order_submission"
    ORDER_REJECTED = "order_rejected"
    RECONCILIATION_FAILURE = "reconciliation_failure"


class EquityMeasurement(StrEnum):
    PROVIDER_LATENCY_SECONDS = "provider_latency_seconds"
    QUOTE_AGE_SECONDS = "quote_age_seconds"


class EquityMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: Counter[str] = Counter()
        self._measurements: dict[str, dict] = {}
        self._started_at = datetime.now(timezone.utc).isoformat()

    def record(self, event: EquityEvent) -> None:
        # Enforce the enum at runtime too: arbitrary error strings / URLs must
        # never become unbounded labels or smuggle secrets into diagnostics.
        event = EquityEvent(event)
        with self._lock:
            self._events[event.value] += 1

    def observe(self, measurement: EquityMeasurement, value: float) -> None:
        measurement = EquityMeasurement(measurement)
        if not math.isfinite(value) or value < 0:
            raise ValueError("Equity measurements must be finite and non-negative")
        with self._lock:
            series = self._measurements.setdefault(
                measurement.value, {"count": 0, "sum": 0.0, "max": 0.0, "last": 0.0}
            )
            series["count"] += 1
            series["sum"] += value
            series["max"] = max(series["max"], value)
            series["last"] = value

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "started_at": self._started_at,
                "scope": "process",
                "counters": {
                    event.value: self._events[event.value] for event in EquityEvent
                },
                "measurements": {
                    key: dict(value) for key, value in self._measurements.items()
                },
            }


equity_metrics = EquityMetrics()
