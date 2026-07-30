from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT / "scripts" / "cutover_rkpk_vertical_slice_v4_no_live.sh"
)


def test_cutover_requires_taf_v4_lineage_before_stopping_writer() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    preflight = source.index(
        '"$PY" - \\\n'
        '  "$ROOT" "$PACKET" "$ACTIVATION" "$TAF_CROSSFIT"'
    )
    stop = source.index('/bin/launchctl bootout "system/$TARGET_LABEL"')
    assert preflight < stop
    for token in (
        "taf_nwp_crossfit_30d_v4.json",
        "taf_exact_join_30d_v4.json",
        "taf_revision_artifact_v4.json",
        "taf_transport_oos_v5_rehearsal.json",
        "load_target_activation",
        '!= "target-v2"',
        "_validate_crossfit",
        "_validate_exact_join",
        "TafRevisionArtifact.from_mapping",
        'transport.get("state") != "PASS"',
        'transport.get("nll_not_worse") is not True',
        'transport.get("crps_not_worse") is not True',
    ):
        assert token in source


def test_cutover_keeps_no_live_and_non_target_service_boundaries() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'MARKET_LABEL="com.weather.market_ws"' in source
    assert 'ORDER_LABEL="com.weather.clob_order_service"' in source
    assert '"live_submit_authorized": False' in source
    assert '"calls_order_gateway": False' in source
    assert '"orders_submitted": 0' in source
    assert "rkpk_v4_cutover_non_target_pid_changed" in source
    assert 'conn.execute("PRAGMA journal_mode=WAL")' in source
    assert "rkpk_v4_cutover_wal_enable_failed" in source
    assert "rkpk_v4_cutover_wal_mode_missing" in source
    assert "bayes_basket_target_no_live_activation_apply.py" in source
    assert (
        "apply_bayes_basket_target_target_v2_station_policy_no_live.py"
        in source
    )
    assert "TARGET_V2_STATION_POLICY_NO_LIVE" in source
    assert "rkpk_v4_cutover_target_v2_station_policy_invalid" in source
    assert "rkpk_v4_cutover_station_policy_receipt_invalid" in source
    assert (
        'int(station_receipt.get("orders_submitted", -1)) != 0'
        in source
    )
    assert (
        'int(station_receipt.get("orders_submitted") or -1) != 0'
        not in source
    )
    assert "rkpk_v4_cutover_legacy_delta_not_superseded" in source
    assert '"strategy_version": "target-v2"' in source
    assert '("d2", "target-v2", 0)' in source
