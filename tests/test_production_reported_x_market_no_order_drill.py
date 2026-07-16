from pathlib import Path

from scripts.production_reported_x_market_no_order_drill import ROOT, run_drill


def test_no_order_drill_refuses_live_archive() -> None:
    result = run_drill(ROOT / "data/archive.db")
    assert result["ok"] is False
    assert result["blockers"] == ["maintenance_backup_required_live_archive_refused"]
    assert result["places_orders"] is False


def test_no_order_drill_blocks_missing_backup(tmp_path: Path) -> None:
    result = run_drill(tmp_path / "missing.db")
    assert result["ok"] is False
    assert result["blockers"] == ["backup_or_drill_source_missing"]
