from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_retired_weak_forecast_scheduler_is_outside_production_closure() -> None:
    daemon = (ROOT / "scripts/weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    assert "taf_release_schedule" not in daemon
    assert "ForecastSnapshotWorker" not in daemon
    assert "DailyMarketSsotWorker" not in daemon
    assert "TargetProductionWorker" in daemon
