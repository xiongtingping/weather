from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_combined_cutover_rebuilds_date_bound_market_context_after_v6_and_keeps_trading_off() -> None:
    text = (ROOT / "scripts/cutover_v6_probability_and_soft_market_projection.sh").read_text(encoding="utf-8")
    assert "CUTOVER_V6_AND_ACTIVATE_SOFT_MARKET_PROJECTION" in text
    assert text.index("cutover_physical_state_bayes_v6_runtime_hotfix.sh") < text.index("prepare_operational_bridge_market_release.py")
    assert text.index("prepare_operational_bridge_market_release.py") < text.index("production_reported_x_market_no_order_drill.py")
    assert text.index("production_reported_x_market_no_order_drill.py") < text.index("activate_operational_bridge_market_release.py")
    assert "ACTIVATE_SAMPLE_FREE_OPERATIONAL_X_S_B_SOFT_PROJECTION" in text
    assert "posterior_checkpoint_v4_physical_state_bayes_v6" in text
    assert "latest_committed_v6_event_date_missing" in text
    assert "dynamic_three_leg_live=false" in text
    assert "external_disk_required=false" in text
    assert "/Volumes" not in text
    assert "data/backups" not in text
