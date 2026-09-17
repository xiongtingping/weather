from pathlib import Path

from scripts.production_reported_x_market_no_order_drill import ROOT, run_drill


BRIDGE = ROOT / "config/resolution_bridge_production_activation.json"
CONTEXT = ROOT / "config/trading/production_probability_v6_market_context_activation.json"


def test_v6_no_order_drill_blocks_missing_archive(tmp_path: Path) -> None:
    result = run_drill(
        tmp_path / "missing.db",
        bridge_activation=BRIDGE,
        market_context_activation=CONTEXT,
        event_date="2026-07-16",
    )
    assert result["ok"] is False
    assert result["blockers"] == ["archive_or_v6_drill_source_missing"]
