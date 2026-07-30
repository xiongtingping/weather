from __future__ import annotations

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


def test_narrow_cutover_stops_only_target_and_preserves_shared_pids() -> None:
    text = _text()
    assert 'launchctl bootout "system/$TARGET_LABEL"' in text
    assert 'launchctl bootout "system/$MARKET_WS_LABEL"' not in text
    assert 'launchctl bootout "system/$CLOB_LABEL"' not in text
    assert "market_ws_pid_changed" in text
    assert "clob_order_service_pid_changed" in text
    assert "market_ws_pid_changed_before_target_restart" in text
    assert "clob_order_service_pid_changed_before_target_restart" in text
    assert "target_writer_pid_did_not_change" in text
    assert "migrate_reported_trajectory_hard_no_timezone.py" not in text
    assert "initialize_forward_observation_store" not in text
    assert "archive.db" not in text


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
    assert "bayes_basket_probability_evidence_RKPK_formal_packet.json" in text
    assert "metar_trajectory_candidate_packet.json" in text
    assert text.index("FORMAL_PROBABILITY_EVIDENCE_PACKET") < text.index(
        "CANDIDATE_PROBABILITY_EVIDENCE_PACKET"
    )
    assert "--probability-evidence-packet" in text
    assert "--expected-probability-station-id RKPK" in text
    assert "--require-no-probability-activations" in text
    assert '"${transaction_args[@]}"' in text


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
    assert text.index("success=1") < text.index(
        "prune_bayes_basket_target_maintenance_artifacts.py"
    )


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
    assert 'exact.get("ready") is not True' in text
    assert 'exact.get("exact_scope_count")' in text
    assert "probability_packet_materialized is not True" in text
    assert "bound_activation_count != len(set(packet_activation_ids))" in text
    assert "event_date='2026-07-27'" not in text
    assert "basket_probability_bridge_bucket_probability" in text
    assert 'resources.get("wal_bytes")' in text


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
