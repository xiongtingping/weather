from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "scripts/cutover_bayes_basket_target_probability_evidence_no_live.sh"
)
TRANSACTION_SCRIPT = (
    ROOT
    / "scripts/cutover_bayes_basket_target_ssot_v4_transaction_fenced.py"
)


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _transaction_text() -> str:
    return TRANSACTION_SCRIPT.read_text(encoding="utf-8")


def test_narrow_cutover_stops_only_target_and_preserves_shared_pids() -> None:
    text = _text()
    assert 'launchctl bootout "${SERVICE_DOMAIN}/$TARGET_LABEL"' in text
    assert 'SERVICE_DOMAIN="gui/${UID_VALUE}"' in text
    assert "sudo -u" not in text
    assert 'launchctl bootout "${SERVICE_DOMAIN}/$MARKET_WS_LABEL"' not in text
    assert 'launchctl bootout "${SERVICE_DOMAIN}/$CLOB_LABEL"' not in text
    assert "market_ws_pid_changed" in text
    assert "clob_order_service_pid_changed" in text
    assert "market_ws_pid_changed_before_target_restart" in text
    assert "clob_order_service_pid_changed_before_target_restart" in text
    assert "target_writer_pid_did_not_change" in text
    assert "migrate_reported_trajectory_hard_no_timezone.py" not in text
    assert "initialize_forward_observation_store" not in text
    assert "archive.db" not in text
    assert 'if [ ! -x "$PY" ]; then' in text
    assert '"$PY" "$TARGET_PLIST"' not in text
    assert '\n} 2>/dev/null\n)"' in text
    assert '\n})"' not in text


def test_transaction_fenced_probe_is_user_domain_only_and_rejects_system_residue() -> None:
    text = _transaction_text()
    assert 'SERVICE_DOMAIN = f"gui/{os.getuid()}"' in text
    assert 'SYSTEM_SERVICE_DOMAIN = "system"' in text
    assert 'f"{domain}/{label}"' in text
    assert "_launchctl_print(SERVICE_DOMAIN, label)" in text
    assert "_launchctl_print(SYSTEM_SERVICE_DOMAIN, label)" in text
    assert "forbidden_system_service_residue" in text
    assert "required_user_service_pid_missing" in text
    assert '"required_domain": REQUIRED_SERVICE_DOMAIN' in text
    assert 'REQUIRED_SERVICE_DOMAIN = "user"' in text
    assert "sudo" not in text.lower()
    assert "launchdaemon" not in text.lower()


def test_transaction_fenced_rkpk_is_schema_bootstrap_not_production_scope() -> None:
    text = _transaction_text()
    assert "_validate_schema_bootstrap_packet" in text
    assert "schema_bootstrap_artifact" in text
    assert '"artifact_type": "schema_bootstrap"' in text
    assert '"not_production_station_scope": True' in text
    assert '"caller_scope_contract": "canonical45"' in text
    assert "transaction_fenced_schema_bootstrap_station_binding_invalid" in text
    assert "transaction-fenced-schema-bootstrap-v3" in text
    assert "transaction-fenced-rkpk-v2" not in text
    assert '"packet": str(packet_file)' not in text
    assert '"station_scope":' not in text
    assert '"execution_mode": "production_no_live"' in text
    assert '"calls_order_gateway": False' in text
    assert '"orders_submitted": 0' in text


def test_narrow_cutover_checkpoints_before_transaction_and_reprobes() -> None:
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
    cutover = text.index(
        "cutover_bayes_basket_target_ssot_v4_transaction_fenced.py",
        second_probe,
    )
    assert first_probe < checkpoint < second_probe < cutover
    assert "--target-writer-stopped" in text
    assert "bayes_basket_target_no_live_vertical_slice_RKPK_v3.json" in text
    assert "SCHEMA_BOOTSTRAP_PACKET" in text
    assert "coordinate_registry_manifest" in text
    assert 'len(stations) != 45' in text
    assert '"${READINESS_STATION_ARGS[@]}"' in text
    assert "probability_evidence_packet=\"\"" in text
    assert "--expected-probability-station-id RKPK" not in text
    assert '"${transaction_args[@]}"' in text


def test_no_live_cutover_does_not_require_forward_oos_or_exact_45_scope() -> None:
    text = _text()
    assert 'receipt.get("unfinished_target_inputs") != []' in text
    assert 'probability_stage_counts.get("P0")' in text
    assert 'probability_stage_counts.get("P0-M")' in text
    assert 'probability_stage_counts.get("P1")' in text
    assert 'target_inputs.get("ready") is not True' not in text
    assert 'exact.get("ready") is not True' not in text
    assert 'exact.get("blocked_scope_count")' not in text
    assert "formal_promotion_ready" not in text


def test_two_round_acceptance_requires_user_residents_and_no_system_residue() -> None:
    text = _text()
    assert "assert_user_domain_resident" in text
    assert "assert_user_domain_quiescent" not in text
    assert '["launchctl", "print", f"system/{label}"]' in text
    assert '["launchctl", "print", f"gui/{uid}/{label}"]' in text
    assert "forbidden_system_service_residue" in text
    assert "resident_core_service_missing_user_domain" in text


def test_narrow_cutover_retains_live_pause_and_has_local_rollback() -> None:
    text = _text()
    assert '/usr/bin/touch "$LIVE_PAUSE"' in text
    assert "restore_backup" in text
    assert "restore_bayes_basket_target_backup.py" in text
    assert "target_probability_evidence_rollback_failed_writer_left_stopped" in text
    assert "wait_target_stopped" in text
    assert "wait_target_started" in text
    assert "protected_service_drift=1" in text
    assert (
        "target_probability_evidence_shared_service_drift_writer_left_stopped"
        in text
    )
    assert (
        "target_probability_evidence_rollback_restart_failed_writer_left_stopped"
        in text
    )
    assert "write_failure_receipt" in text
    assert "bayes_basket_target_probability_evidence_failure_v1" in text
    assert '"backup_retained": Path(backup).is_file()' in text
    assert '"live_pause_retained": True' in text
    assert "restart_target || true" not in text
    assert "curl -X POST" not in text
    assert "prune_bayes_basket_target_maintenance_artifacts.py" in text
    assert "APPLY_BOUNDED_MAINTENANCE_RETENTION" in text
    assert text.index("prune_bayes_basket_target_maintenance_artifacts.py") < text.rindex(
        "success=1"
    )
    assert "delete_recovery_after_success" in text
    assert "bayes_basket_target_m9_06_atomic_cutover" in text
    assert "m9_06_schema_activation_source_invalid" in text
    assert "target_database_mutated" in text
    assert 'source("schema_activation_replace", cutover_raw)' not in text
    assert '"round": round_payload' not in text
    assert '"materialization_pending_blockers": round_payload.get(' in text


def test_narrow_cutover_requires_schema_resource_ws_and_no_live_postcheck() -> None:
    text = _text()
    assert 'schema.get("missing_tables") != []' in text
    assert 'vector.get("ok") is not True' in text
    assert 'resources.get("ok") is not True' in text
    assert 'ws.get("state") != "loaded"' in text
    assert 'int(ws.get("incomplete_scope_count") or 0) != 0' in text
    assert 'boundary.get("calls_order_gateway") is not False' in text
    assert 'boundary.get("live_submit_authorized") is not False' in text
    assert 'boundary.get("places_orders") is not False' in text
    assert 'boundary.get("orders_submitted")' in text
    assert 'receipt.get("unfinished_target_inputs") != []' in text
    assert 'target_inputs.get("probability_materialized") is not True' in text
    assert 'target_inputs.get("order_state_materialized") is not True' in text
    assert "probability_packet_materialized is not True" in text
    assert "bound_activation_count != len(set(packet_activation_ids))" in text
    assert "event_date='2026-07-27'" not in text
    assert "basket_probability_bridge_bucket_probability" in text
    assert 'resources.get("wal_bytes")' in text
    assert 'target_inputs.get("ready") is not True' not in text


def test_canonical_two_round_scope_and_future_day_contract_are_explicit() -> None:
    text = _text()
    assert "capture_migration_round(db)" in text
    assert "compare_migration_rounds(first, second)" in text
    assert "verify_migration_two_round_resident_receipts(" in text
    assert "probability_scope_snapshot(Path(sys.argv[1]))" in text
    assert "OUTSIDE_SCOPE_BEFORE" in text
    assert "OUTSIDE_SCOPE_AFTER" in text
    assert "NON_RKPK" not in text
    assert "started_station_ids" in text
    assert "bayes_basket_target_daily_no_live_canonical_index_v2" in text
    assert "daily_no_live_safety_boundary_invalid" in text
    assert "materialization_pending_blockers" in text
    assert "completed.returncode not in (0, 2)" in text
    assert "not_started_station_ids" in text
    assert '"${SETTLEMENT_STATION_ARGS[@]}"' in text


def test_m9_06_atomic_receipt_closes_all_stages_and_recovery_order() -> None:
    text = _text()
    assert "ATOMIC_STAGE_DIR" in text
    assert "ATOMIC_RECEIPT" in text
    assert "ATOMIC_LATEST" in text
    assert "build_cutover_receipt" in text
    assert "build_release_identity" in text
    assert "build_idempotent_replay_receipt" in text
    assert "delete_recovery_after_success" in text
    assert "SCHEMA_BOOTSTRAP_PACKET" in text
    assert '"legacy_fallback_used": False' in text
    assert text.rindex("success=1") < text.rindex(
        "delete_recovery_after_success"
    )
    assert text.index("protected_pid_postcheck") < text.index(
        "resident_acceptance_round_{index}"
    )
    assert text.index("enumerate(rounds, start=1)") < text.rindex(
        "delete_recovery_after_success"
    )


def test_cutover_materializes_daily_and_settlement_version_receipts_after_postcheck() -> None:
    text = _text()
    postcheck = text.index(
        "target_probability_evidence_post_readiness_invalid"
    )
    daily = text.index(
        "accept_bayes_basket_target_daily_no_live.py",
        postcheck,
    )
    settlement = text.index(
        "audit_bayes_basket_target_settlement_version.py",
        daily,
    )
    success = text.index("success=1", settlement)

    assert postcheck < daily < settlement < success
    assert "--latest-labeled-event-date" in text[settlement:success]
    assert "--raw-root" in text[daily:settlement]


def test_transaction_commit_unknown_preserves_pre_cutover_backup() -> None:
    text = TRANSACTION_SCRIPT.read_text(encoding="utf-8")
    attempted_init = text.index("commit_attempted = False")
    attempted_set = text.index("commit_attempted = True", attempted_init)
    commit_call = text.index("conn.commit()", attempted_set)
    committed_set = text.index("committed = True", commit_call)
    preserve_guard = text.index(
        "if not commit_attempted and not committed and backup.exists()"
    )
    assert attempted_init < attempted_set < commit_call < committed_set
    assert preserve_guard > committed_set


def test_probability_evidence_is_staged_inside_cutover_transaction() -> None:
    text = TRANSACTION_SCRIPT.read_text(encoding="utf-8")
    begin = text.index('conn.execute("BEGIN IMMEDIATE")')
    schema = text.index("apply_controlled_migration(", begin)
    evidence = text.index(
        "ingest_probability_evidence(",
        schema,
    )
    commit = text.index("conn.commit()", evidence)
    assert begin < schema < evidence < commit
    assert "finalize_transaction=False" in text[evidence:commit]
    assert "transaction_fenced_probability_evidence_replay_not_idempotent" in text
    assert "required_user_service_pid_missing" in text
    assert "required_system_service_pid_missing" not in text
