from __future__ import annotations

from scripts.run_official_nwp_daily_curve_monitor_job import _try_acquire_run_lease


def test_curve_monitor_process_lease_rejects_overlap_and_releases_on_close(tmp_path) -> None:
    first = _try_acquire_run_lease(tmp_path / "monitor.lock")
    assert first is not None
    assert _try_acquire_run_lease(tmp_path / "monitor.lock") is None
    first.close()
    third = _try_acquire_run_lease(tmp_path / "monitor.lock")
    assert third is not None
    third.close()
