import concurrent.futures

import pytest

from condor.equity_observability import EquityEvent, EquityMeasurement, EquityMetrics


def test_concurrent_observations_are_complete_and_snapshots_are_detached():
    metrics = EquityMetrics()

    def record(_):
        metrics.record(EquityEvent.CACHE_HIT)
        metrics.observe(EquityMeasurement.QUOTE_AGE_SECONDS, 2)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(500)))
    snapshot = metrics.snapshot()
    assert snapshot["counters"]["cache_hit"] == 500
    assert snapshot["measurements"]["quote_age_seconds"] == {
        "count": 500,
        "sum": 1000,
        "max": 2,
        "last": 2,
    }
    snapshot["counters"]["cache_hit"] = -1
    snapshot["measurements"]["quote_age_seconds"]["count"] = -1
    assert metrics.snapshot()["counters"]["cache_hit"] == 500
    assert metrics.snapshot()["measurements"]["quote_age_seconds"]["count"] == 500


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_measurements_do_not_poison_monitoring(value):
    metrics = EquityMetrics()
    with pytest.raises(ValueError):
        metrics.observe(EquityMeasurement.QUOTE_AGE_SECONDS, value)
    assert metrics.snapshot()["measurements"] == {}


def test_arbitrary_labels_cannot_capture_secrets_or_increase_cardinality():
    metrics = EquityMetrics()
    with pytest.raises(ValueError):
        metrics.record("https://provider.test?api_key=secret")
    with pytest.raises(ValueError):
        metrics.observe("AAPL", 1)
    assert not any(metrics.snapshot()["counters"].values())
