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
    installer = (ROOT / "scripts" / "install_monitor_launchdaemon.sh").read_text(encoding="utf-8")
    assert "Library/LaunchAgents" in installer
    assert "gui/${UID_VALUE}" in installer


def test_weak_revision_consumer_cannot_materialize_forecast_as_p1() -> None:
    text = (ROOT / "scripts" / "polymarket_monitor_daemon.sh").read_text(encoding="utf-8")
    assert 'WEAK_REVISION_CONSUMER_ENABLED="${PM_WEAK_REVISION_CONSUMER_ENABLED:-0}"' in text
    assert 'WEAK_REVISION_CONSUMER_INTERVAL="${PM_WEAK_REVISION_CONSUMER_INTERVAL_SEC:-15}"' in text
    assert "start_weak_revision_consumer()" in text
    block = text[text.index("run_weak_revision_consumer_once()") : text.index("start_weak_revision_consumer()")]
    assert "materialize_official_nwp_daily_peak_p1_ingress.py" not in block
    collection = text[text.index("start_official_nwp_curve_collection()") : text.index("run_weak_revision_consumer_once()")]
    assert "apply_current_weak_source_revisions_v6.py" not in collection
    assert "materialize_official_nwp_daily_peak_p1_ingress.py" not in collection
    assert not (ROOT / "launchd" / "com.weather.monitor.daemon.plist").exists()
