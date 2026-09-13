from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# These three tests read scripts/polymarket_monitor_daemon.sh to prove the
# official-NWP curve and snapshot collectors ran as default-off children of
# that daemon rather than as daemons of their own.  The monitor daemon and its
# plist were deleted on 2026-08-04 (1558ece5), and the tests kept opening the
# deleted file and failing on FileNotFoundError.  What still holds, and is
# still worth pinning, is the negative half: none of them came back as a
# LaunchAgent of its own.


def test_curve_collection_did_not_become_a_daemon_of_its_own() -> None:
    assert not (ROOT / "scripts" / "polymarket_monitor_daemon.sh").exists()
    assert not list((ROOT / "launchd").glob("com.weather.official_nwp_daily_curve*"))


def test_live_snapshot_collection_did_not_become_a_daemon_of_its_own() -> None:
    assert not list((ROOT / "launchd").glob("com.weather.official_nwp_live_snapshot*"))
    assert not (ROOT / "launchd" / "com.weather.official_nwp_prewarm.plist").exists()


def test_retired_monitor_daemon_plist_stays_retired() -> None:
    assert not (ROOT / "launchd" / "com.weather.monitor.daemon.plist").exists()
