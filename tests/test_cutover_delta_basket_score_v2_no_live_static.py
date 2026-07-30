import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_delta_basket_score_v2_no_live.sh"


def test_cutover_stops_only_target_writer_during_schema_migration() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'launchctl bootout "system/$LABEL"' in text
    assert 'launchctl bootout "system/$MARKET_LABEL"' not in text
    assert 'launchctl bootout "system/$ORDER_LABEL"' not in text
    assert 'launchctl bootstrap system "$INSTALLED_PLIST"' in text
    assert "--confirm-reported-trajectory-no-stopped" in text
    assert "repair_bayes_basket_target_false_quarantine.py" in text
    assert '/usr/bin/sudo -u "$CUTOVER_USER" /usr/bin/python3' in text
    assert (
        '/usr/bin/sudo -u "$CUTOVER_USER" /usr/bin/python3 - "$ROOT"'
        in text
    )
    assert 'CUTOVER_USER="${SUDO_USER:-xiong}"' in text
    assert text.index("launchctl bootout") < text.index(
        "repair_bayes_basket_target_false_quarantine.py"
    ) < text.rindex('launchctl bootstrap system "$INSTALLED_PLIST"')
    assert "accept_delta_basket_score_v2_no_live.py" in text
    assert "--allow-already-current" in text


def test_cutover_keeps_no_live_and_oos_boundaries_explicit() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--confirm-delta-basket-score-v2-production-no-live" in text
    assert 'touch "$PAUSE"' in text
    assert 'rm -f "$PAUSE"' not in text
    assert "NO_LIVE_ACTIVATED" in text
    assert "oos_gate_passed=0" in text
    assert '"d2_fallback", "NO_LIVE_ACTIVATED", 0' in text
    assert "forward_oos_blocks_production_no_live=false" in text
    assert "forward_oos_blocks_live_or_parameter_promotion=true" in text
    assert "economic_policy_schema" in text
    assert "economic_policy_artifact_sha256" in text
    assert "delta_v2_cutover_post_fill_v2_columns_missing" in text
    assert "delta_v2_cutover_closed_scope_truth_missing" in text
    assert "closed_truth_scopes_verified=" in text
    assert "delta_v2_cutover_forward_repair_not_idempotent" in text
    assert "delta_v2_cutover_forward_quarantine_history_lost" in text
    assert "forward_false_quarantine_repair_verified=2" in text


def test_cutover_preserves_other_service_pids_and_writer_wiring() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "PM_TARGET_PRODUCTION_WRITER" in text
    assert "PM_TARGET_EXECUTION_MODE" in text
    assert "market_pid_before" in text
    assert "market_pid_after" in text
    assert "order_pid_before" in text
    assert "order_pid_after" in text
    assert "delta_v2_cutover_non_target_pid_changed" in text
    assert "delta_v2_cutover_error_path_market_pid_changed" in text
    assert "delta_v2_cutover_error_path_order_pid_changed" in text
    assert "delta_v2_cutover_current_maturity_not_covered" in text


def test_cutover_uses_v2_manifest_without_subminimum_clip() -> None:
    manifest = json.loads(
        (
            ROOT
            / "config/trading/"
            "delta_basket_score_v2_rkpk_no_live_bootstrap_v2.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["strategy_version"] == "target-v2"
    assert manifest["cash_clip_grid"] == [1.0, 2.0]
    assert min(manifest["price_grid"]) == 0.01
    assert 0.20 in manifest["price_grid"]
    assert max(manifest["price_grid"]) == 0.98
    assert manifest["live_submit_authorized"] is False
    assert manifest["calls_order_gateway"] is False
