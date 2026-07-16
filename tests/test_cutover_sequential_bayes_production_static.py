from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_state_aware_strong_lr_epoch_cutover_is_backup_first_append_only_and_orderless() -> None:
    text = (ROOT / "scripts/cutover_state_aware_strong_lr_probability_epoch.sh").read_text(encoding="utf-8")
    replay_preview = text.index('cutover_step "read_only_full_canonical_state_replay"')
    manifest_closure = text.index('cutover_step "verify_probability_runtime_manifest_dependency_closure"')
    clone_preflight = text.index('cutover_step "preflight_local_apfs_clone_capability"')
    preflight = text.index('cutover_step "preflight_transaction_b_on_latest_verified_backup"')
    pause = text.index('cutover_step "pause_writer_services"')
    quiescence = text.index('cutover_step "verify_writer_quiescence"')
    queue_claim = text.index('cutover_step "verify_archive_writer_queue_claim"')
    local_backup = text.index('cutover_step "create_exact_local_apfs_clone"')
    artifact = text.index('cutover_step "materialize_content_addressed_strong_lr_v2"')
    activation = text.index('cutover_step "materialize_successor_probability_activation"')
    p0 = text.index('cutover_step "commit_successor_p0_and_replay_canonical_events"')
    readiness = text.index('cutover_step "verify_orderless_probability_epoch"')
    schema = text.index('cutover_step "apply_default_off_position_continuation_market_alpha_schema"')
    weak_schema = text.index('cutover_step "apply_weak_forecast_revision_schema"')
    resume = text.index('cutover_step "resume_services"')
    acceptance = text.index('cutover_step "accept_ssot_and_state_discrimination"')
    external_backup = text.index('cutover_step "copy_frozen_pre_cutover_image_to_external_backup"')

    assert replay_preview < manifest_closure < clone_preflight < preflight < pause < quiescence < queue_claim < local_backup < schema < weak_schema < artifact < activation < p0 < readiness < resume < acceptance < external_backup
    assert "/Volumes/Expansion/熊/backups" in text
    assert "PRAGMA wal_checkpoint(TRUNCATE)" in text
    assert "cp -c data/archive.db" in text
    assert "data/local_cutover_backups" in text
    assert 'local APFS backup directory must not be a symlink' in text
    assert 'stat -f %d data/archive.db' in text
    assert 'stat -f %d "$LOCAL_BACKUP_DIR/."' in text
    assert 'cp -c config/production_probability_runtime_activation.json "$LOCAL_BACKUP_PROBE"' in text
    assert 'cmp config/production_probability_runtime_activation.json "$LOCAL_BACKUP_PROBE"' in text
    assert 'COPYFILE_DISABLE=1 cp "$LOCAL_BACKUP"' in text
    assert 'CUTOVER_COMPLETE=1\n\ncutover_step "copy_frozen_pre_cutover_image_to_external_backup"' in text
    assert "probability_cutover_external_backup_deferred_v1" in text
    assert "probability_epoch_already_committed_and_accepted" in text
    assert "external_backup_deferred_receipt=" in text
    assert 'EXTERNAL_BACKUP_STATE="copied_and_verified"' in text
    assert 'COPYFILE_DISABLE=1 cp data/archive.db' not in text
    assert ".backup '$BACKUP'" not in text
    assert "start_sudo_keepalive" in text
    assert "sudo -n -v" in text
    assert "MATERIALIZE_OPERATIONAL_STATE_STRONG_LR_BASELINE_V2" in text
    assert "audit_runtime_manifest_dependency_closure" in text
    assert "ACTIVATE_SEQUENTIAL_BAYES_PROBABILITY_MAINLINE" in text
    assert "BOOTSTRAP_PRODUCTION_PROBABILITY_P0" in text
    assert "current_activation_has_p0_lineage" in text
    assert 'owner="state_aware_probability_cutover_preflight"' in text
    assert "verify_writer_quiescence()" in text
    assert "for attempt in 1 2 3" in text
    assert 'sleep 0.25' in text
    assert "state_aware_probability_epoch_active" in text
    assert "already_active_read_only_acceptance" in text
    already_active = text[text.index('cutover_step "already_active_read_only_acceptance"'):text.index('if ! sudo -n true 2>/dev/null;', text.index('cutover_step "already_active_read_only_acceptance"')) + len('if ! sudo -n true 2>/dev/null;')]
    assert "wait_for_archive_wal_keeper" in text[text.index('cutover_step "already_active_read_only_acceptance"'):text.index('CUTOVER_COMPLETE=1', text.index('cutover_step "already_active_read_only_acceptance"'))]
    assert "sudo -n true" in already_active
    assert "committed append-only probability epoch preserved" in text
    assert "prior activation restored" in text
    assert "audit_strong_state_discrimination.py" in text
    assert "archive_db_wal_keeper_audit.py" in text
    assert "wait_for_archive_wal_keeper()" in text
    assert 'PM_ARCHIVE_WAL_KEEPER_ACCEPTANCE_ATTEMPTS:-30' in text
    assert 'sudo -n "$python_bin" scripts/archive_db_wal_keeper_audit.py' in text
    assert "preview_state_aware_strong_lr_replay.py" in text
    assert text.count("production_transaction_b_no_order_drill.py") >= 1
    assert text.count("production_sequential_bayes_chain_audit.py") >= 2
    assert text.count("production_sequential_bayes_cutover_acceptance.py") >= 2
    assert text.count("check_ssot_alignment.py") >= 2
    assert "ACTIVATE_DYNAMIC_POSITION_MARKET_ALPHA_SCHEMA" in text
    assert "ACTIVATE_WEAK_FORECAST_REVISION_SCHEMA" in text
    assert "assert_trading_activations_disabled" in text
    assert "clob_order" not in text
