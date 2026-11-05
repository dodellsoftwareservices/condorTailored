import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from routines.base import normalize_result


@pytest.mark.parametrize(
    "routine",
    [
        "market_scanner",
        "price_monitor",
        "arb_check",
        "triangular_arb",
        "technical_analysis",
        "alpaca_etf_sweep",
        "mean_reversion_research",
    ],
)
def test_missing_backend_is_a_failed_run(routine, monkeypatch):
    module = importlib.import_module(f"routines.{routine}")
    monkeypatch.setattr(module, "get_client", AsyncMock(return_value=None))
    result = asyncio.run(module.run(module.Config(), SimpleNamespace(_chat_id=1)))
    assert normalize_result(result).failed


def test_logs_summary_without_servers_is_a_failed_run(monkeypatch):
    from routines import logs_summary

    monkeypatch.setattr(
        logs_summary,
        "get_config_manager",
        lambda: SimpleNamespace(list_servers=lambda: {}),
    )
    result = asyncio.run(
        logs_summary.run(
            logs_summary.Config(), SimpleNamespace(_chat_id=1, user_data={})
        )
    )
    assert normalize_result(result).failed
