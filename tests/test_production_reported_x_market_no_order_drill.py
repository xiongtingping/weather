from pathlib import Path

from scripts.production_reported_x_market_no_order_drill import ROOT, run_drill


BRIDGE = ROOT / "config/resolution_bridge_production_activation.json"
CONTEXT = ROOT / "config/trading/production_probability_v6_market_context_activation.json"


def test_v6_no_order_drill_reads_live_archive_without_backup_or_mutation() -> None:
    result = run_drill(
        ROOT / "data/archive.db",
        bridge_activation=BRIDGE,
        market_context_activation=CONTEXT,
        event_date="2026-07-16",
    )
    assert result["ok"] is True
    assert result["archive_open_mode"] == "read_only_immutable"
    assert result["archive_unchanged"] is True
    assert result["external_disk_required"] is False
    assert result["writes_archive_db"] is False
    assert result["places_orders"] is False
    assert set(result["runtime_sha256"]) == {"writer", "bundle", "branch", "commit"}


def test_v6_no_order_drill_blocks_missing_archive(tmp_path: Path) -> None:
    result = run_drill(
        tmp_path / "missing.db",
        bridge_activation=BRIDGE,
        market_context_activation=CONTEXT,
        event_date="2026-07-16",
    )
    assert result["ok"] is False
    assert result["blockers"] == ["archive_or_v6_drill_source_missing"]
