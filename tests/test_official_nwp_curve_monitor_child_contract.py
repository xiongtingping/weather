from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_curve_collection_is_a_default_disabled_monitor_child_not_a_new_daemon() -> None:
    text = (ROOT / "scripts" / "polymarket_monitor_daemon.sh").read_text(encoding="utf-8")
    assert 'OFFICIAL_NWP_CURVE_COLLECTION_ENABLED="${PM_OFFICIAL_NWP_CURVE_COLLECTION_ENABLED:-0}"' in text
    assert "start_official_nwp_curve_collection()" in text
    assert "run_official_nwp_daily_curve_monitor_job.py\" --execute" in text
    assert "launchd/com.weather.official_nwp_daily_curve" not in text


def test_live_snapshot_collection_is_also_a_monitor_child_not_a_hot_path() -> None:
    text = (ROOT / "scripts" / "polymarket_monitor_daemon.sh").read_text(encoding="utf-8")
    assert 'OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_ENABLED="${PM_OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_ENABLED:-0}"' in text
    assert "start_official_nwp_live_snapshot_collection()" in text
    assert "run_official_nwp_live_snapshot_monitor_job.py\" --execute" in text
    assert "launchd/com.weather.official_nwp_live_snapshot" not in text
    assert not (ROOT / "launchd" / "com.weather.official_nwp_prewarm.plist").exists()
    assert "com.weather.official_nwp_prewarm" in (ROOT / "scripts" / "install_monitor_launchdaemon.sh").read_text(encoding="utf-8")
