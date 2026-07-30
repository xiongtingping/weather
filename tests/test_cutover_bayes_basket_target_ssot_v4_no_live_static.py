from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_bayes_basket_target_ssot_v4_no_live.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_cutover_stops_only_the_target_writer_and_preserves_other_pids() -> None:
    text = _text()
    assert 'launchctl bootout "system/$TARGET_LABEL"' in text
    assert 'launchctl bootout "system/$MARKET_WS_LABEL"' not in text
    assert 'launchctl bootout "system/$CLOB_LABEL"' not in text
    assert 'market_ws_pid_after" != "$market_ws_pid_before' in text
    assert 'clob_pid_after" != "$clob_pid_before' in text
    assert 'target_stopped=0' in text
    assert 'target_stopped=1' in text
    assert 'no target service mutation occurred' in text
    assert 'market_ws_pid_before="$(service_pid "$MARKET_WS_LABEL")"' in text
    assert 'clob_pid_before="$(service_pid "$CLOB_LABEL")"' in text
    assert 'market_ws_pid_after="$(service_pid "$MARKET_WS_LABEL")"' in text
    assert 'clob_pid_after="$(service_pid "$CLOB_LABEL")"' in text


def test_cutover_requires_quiescence_two_backups_and_activation_cas() -> None:
    text = _text()
    assert "probe_bayes_basket_target_writer_quiescence.py" in text
    assert "--writer-quiescence-receipt" in text
    assert "SCHEMA_BACKUP=" in text
    assert "ACTIVATION_BACKUP=" in text
    assert "bayes_basket_target_production_ssot_apply.py" in text
    assert "bayes_basket_target_no_live_activation_apply.py" in text
    assert "migrate_reported_trajectory_hard_no_timezone.py" in text
    assert "HARD_NO_BACKUP=" in text


def test_cutover_checkpoints_only_target_wal_then_refreshes_quiescence() -> None:
    text = _text()
    first_probe = text.index(
        "probe_bayes_basket_target_writer_quiescence.py"
    )
    checkpoint = text.index(
        "checkpoint_bayes_basket_target_wal.py",
        first_probe,
    )
    second_probe = text.index(
        "probe_bayes_basket_target_writer_quiescence.py",
        checkpoint,
    )
    migration = text.index(
        "bayes_basket_target_production_ssot_apply.py",
        second_probe,
    )

    assert first_probe < checkpoint < second_probe < migration
    assert '--db "$TARGET_DB"' in text[checkpoint:second_probe]
    assert '--quiescence-receipt "$QUIESCENCE"' in text[
        checkpoint:second_probe
    ]
    assert 'write_cutover_journal "target_wal_checkpoint_complete"' in text


def test_cutover_keeps_every_live_and_gateway_boundary_closed() -> None:
    text = _text()
    assert 'touch "$LIVE_PAUSE"' in text
    assert 'x.get("read_only") is True' in text
    assert 'x.get("order_submission_enabled") is False' in text
    assert '"execution_mode": "production_no_live"' in text
    assert '"live_submit_authorized": False' in text
    assert '"calls_order_gateway": False' in text
    assert '"authorizes_order": False' in text
    assert '"orders_submitted": 0' in text
    assert '127.0.0.1:7897' in text
    assert 'x.get("authorizes_order") is False' in text
    assert 'x.get("live_submit_authorized") is False' in text
    assert 'curl -X POST' not in text
    assert 'curl --data' not in text


def test_cutover_requires_real_v2_store_and_bounded_resource_probes() -> None:
    text = _text()
    assert 'FORWARD_STORE_ROOT="$ROOT/data/runtime/bayes_basket_target_cash_phase"' in text
    assert 'FORWARD_STORE="$FORWARD_STORE_ROOT/forward_observations.sqlite3"' in text
    assert 'OFFLINE_EXPORT_ROOT=' in text
    assert 'initialize_forward_observation_store' in text
    assert 'forward_store_initializer_api_unavailable' in text
    assert 'STORE_SCHEMA_VERSION' in text
    assert 'forward_store_schema_version_mismatch' in text
    assert 'MAX_FORWARD_STORE_BYTES=67108864' in text
    assert 'MAX_FORWARD_WAL_BYTES=16777216' in text
    assert 'MIN_FORWARD_FREE_BYTES=536870912' in text
    assert 'assert_process_memory_budget' in text
    assert 'forward_store_disk_probe_unavailable' in text
    assert 'forward_store_path_component_symlink_forbidden' in text
    assert 'forward_store_sidecar_symlink_forbidden' in text
    assert 'root_missing_before_maintenance and ensure_empty' in text
    assert 'root_missing_before_maintenance and not ensure_empty' in text
    assert 'forward_store_database_bytes_exceeded' in text
    assert 'forward_store_wal_bytes_exceeded' in text
    assert 'forward_store_min_free_space_guard' in text
    assert 'forward_store_sqlite_quick_check_failed' in text
    assert 'probe_forward_store 0 0 "$FORWARD_PRECHECK"' in text
    assert 'probe_forward_store 1 0 "$FORWARD_INIT_CHECK"' in text
    # The script may call the storage initializer, but may not copy its DDL.
    assert 'executescript(DDL)' not in text
    assert 'CREATE TABLE IF NOT EXISTS forward_' not in text


def test_cutover_runs_two_monotonic_idempotency_rounds() -> None:
    text = _text()
    assert 'FORWARD_ROUND_1=' in text
    assert 'FORWARD_ROUND_2=' in text
    assert 'probe_forward_store 1 0 "$FORWARD_ROUND_1"' in text
    assert 'probe_forward_store 1 0 "$FORWARD_ROUND_2"' in text
    assert 'assert_forward_store_rounds' in text
    assert 'forward_store_source_cursor_regressed' in text
    assert 'forward_store_generation_regressed' in text
    assert 'forward_store_cursor_changed_without_generation' in text
    assert 'forward_store_generation_changed_without_progress' in text
    assert 'duplicate_decision_ids' in text
    assert '/bin/sleep 16' in text


def test_cutover_has_read_only_preflight_and_quiescent_initializer_order() -> None:
    text = _text()
    preflight = text.index('probe_forward_store 0 0 "$FORWARD_PRECHECK"')
    # The rollback trap also contains a defensive bootout.  Select the
    # maintenance-flow invocation after the read-only preflight.
    bootout = text.index(
        '/bin/launchctl bootout "system/$TARGET_LABEL"',
        preflight,
    )
    quiescence = text.index('probe_bayes_basket_target_writer_quiescence.py')
    initializer = text.index('\ninitialize_forward_store\nprobe_forward_store 1 0 "$FORWARD_INIT_CHECK"')
    initialized_check = text.index('probe_forward_store 1 0 "$FORWARD_INIT_CHECK"')
    restart = text.index('\nrestart_target_writer\ndeadline')
    round1 = text.index('probe_forward_store 1 0 "$FORWARD_ROUND_1"')
    assert preflight < bootout < quiescence < initializer < initialized_check < restart < round1
    assert 'PM_TARGET_CASH_PHASE_FORWARD_STORE' in text
    assert 'legacy_resident_json_forward_store_forbidden' in text
    assert 'resident_forward_store_override_not_v2_sqlite' in text


def test_cutover_is_frozen_to_rkpk_without_market_replay() -> None:
    text = _text()
    assert "bayes_basket_target_no_live_vertical_slice_RKPK_v3.json" in text
    assert 'artifact_ingress.get("station_ids") != ["RKPK"]' in text
    for forbidden in (
        "2026-07-27",
        "--once awc",
        "export_bayes_basket_target_market_registry.py",
        "materialize_bayes_basket_target_probability.py",
    ):
        assert forbidden not in text


def test_cutover_materializes_content_addressed_candidate_authority_before_activation() -> None:
    text = _text()
    schema = text.index("bayes_basket_target_production_ssot_apply.py")
    ingress = text.index("ingest_bayes_basket_target_artifacts.py")
    activation = text.index(
        "bayes_basket_target_no_live_activation_apply.py"
    )

    assert schema < ingress < activation
    assert "ARTIFACT_PACKET=" in text
    assert "ARTIFACT_INGRESS_RECEIPT=" in text
    assert "--require-packet-sha256" in text
    assert "--confirm INGEST_BAYES_BASKET_TARGET_ARTIFACTS" in text
    assert 'write_cutover_journal "candidate_artifact_ingress_complete"' in text
    assert 'artifact_ingress.get("state") != "COMMITTED"' in text
    assert '"PRODUCTION_NO_LIVE_CANDIDATE"' in text
    assert 'artifact_ingress.get("live_submit_authorized") is not False' in text
    assert 'artifact_ingress.get("authorizes_order") is not False' in text
    assert 'artifact_ingress.get("calls_order_gateway") is not False' in text
    assert 'int(artifact_ingress.get("orders_submitted") or 0) != 0' in text
    assert 'int(ingress_bound.get("cash_phase_policies") or 0) != 1' in text
    assert (
        'int(ingress_bound.get("cash_phase_authority_manifests") or 0) != 1'
        in text
    )
    assert '"calibration_artifacts"' in text
    assert '"ensemble_selection_evidence"' in text
    assert '"ensemble_artifacts"' in text
    assert '"calibration_activations"' in text
    assert "candidate_calibration_authority_ingress_incomplete:" in text
