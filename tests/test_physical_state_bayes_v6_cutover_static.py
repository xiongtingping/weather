from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CUTOVER = ROOT / "scripts/cutover_physical_state_bayes_v6_production.sh"
HOTFIX_CUTOVER = ROOT / "scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh"


def test_v6_cutover_has_irreversible_commit_boundary_without_v5_fallback() -> None:
    text = CUTOVER.read_text(encoding="utf-8")
    schema_at = text.index('step "apply_additive_v6_schema"')
    routes_at = text.index('step "activate_metar_speci_and_taf_amd_v6_routes"')
    activation_at = text.index('step "install_unique_v6_activation"')
    bootstrap_at = text.index('step "bootstrap_v6_p0_and_drain_all_canonical_events"')
    acceptance_at = text.index('step "accept_unique_v6_before_service_resume"')
    commit_at = text.index("V6_COMMITTED=1", acceptance_at)
    resume_at = text.index('step "resume_services_on_v6_only"')

    assert schema_at < routes_at < activation_at < bootstrap_at < acceptance_at < commit_at < resume_at
    assert 'if [ "$V6_COMMITTED" = "1" ]; then' in text
    assert "v6 preserved fail-closed, v5 fallback forbidden" in text
    assert text.index("restore_database") < text.index('V6_COMMITTED=1', acceptance_at)


def test_v6_cutover_uses_et_for_physical_weather_day_boundary() -> None:
    text = CUTOVER.read_text(encoding="utf-8")
    assert 'TZ=America/New_York date +%F' in text
    assert 'TZ=Asia/Shanghai date +%F' not in text


def test_v6_zuuu_physical_entrypoints_use_station_day() -> None:
    physical_entrypoints = (
        ROOT / "scripts/bootstrap_production_probability_v6.py",
        ROOT / "scripts/production_probability_v6_live_acceptance.py",
        ROOT / "scripts/prepare_next_day_production_probability_v6.py",
        ROOT / "scripts/apply_amsc_probability_fast_lane_v6_from_plan.py",
    )
    for path in physical_entrypoints:
        text = path.read_text(encoding="utf-8")
        assert 'ZoneInfo("Asia/Shanghai")' in text


def test_v6_cutover_binds_backup_quiescence_and_post_resume_retry() -> None:
    text = CUTOVER.read_text(encoding="utf-8")
    assert "verify_quiescence" in text
    assert "create_verified_external_database_backup" in text
    assert 'LOCAL_BACKUP_STAGE="/private/tmp/' in text
    assert 'sqlite3 data/archive.db ".backup \'$LOCAL_BACKUP_STAGE\'"' in text
    assert '/bin/cp "$LOCAL_BACKUP_STAGE" "${BACKUP}.partial"' in text
    assert 'LOCAL_BACKUP_SHA256="$(/usr/bin/shasum -a 256 "$LOCAL_BACKUP_STAGE"' in text
    assert 'EXTERNAL_BACKUP_SHA256="$(/usr/bin/shasum -a 256 "${BACKUP}.partial"' in text
    assert 'test "$LOCAL_BACKUP_SHA256" = "$EXTERNAL_BACKUP_SHA256"' in text
    assert 'EXTERNAL_VOLUME_ROOT="${PHYSICAL_STATE_BAYES_V6_EXTERNAL_VOLUME_ROOT:-/Volumes/Expansion}"' in text
    assert "verify_external_backup_mount" in text
    assert 'external backup volume identity drift' in text
    assert 'external backup path resolved onto root workspace filesystem' in text
    assert text.count("verify_external_backup_mount") >= 6
    assert "PRAGMA integrity_check;" in text
    assert 'step "verify_frozen_v6_runtime_after_backup"' in text
    assert "verify_frozen_v6_runtime" in text
    assert 'test "$CANDIDATE_FILE_SHA256" = "$(/usr/bin/shasum -a 256 "$CANDIDATE_ACTIVATION"' in text
    assert "start_sudo_keepalive" in text
    assert "for attempt in $(seq 1 15)" in text
    assert "production_probability_v6_live_acceptance.py" in text


def test_v6_cutover_keeps_verified_local_snapshot_until_commit_or_rollback() -> None:
    text = CUTOVER.read_text(encoding="utf-8")
    backup_start = text.index('step "create_verified_external_database_backup"')
    schema_start = text.index('step "apply_additive_v6_schema"')
    backup_block = text[backup_start:schema_start]
    external_commit = backup_block.index('mv "${BACKUP}.partial" "$BACKUP"')
    assert 'rm -f "$LOCAL_BACKUP_STAGE"' not in backup_block[external_commit:]
    restore = text.split("restore_database()", 1)[1].split("verify_frozen_v6_runtime()", 1)[0]
    assert 'restore_source="$LOCAL_BACKUP_STAGE"' in restore
    assert 'cp -p "$restore_source"' in restore


def test_v6_cutover_stops_root_monitor_before_user_domain_writers() -> None:
    text = CUTOVER.read_text(encoding="utf-8")
    pause = text.split("pause_services()", 1)[1].split("resume_services()", 1)[0]
    assert "stop_system_monitor" in pause
    assert "scripts/weatherctl.sh stop" in pause
    assert pause.index("stop_system_monitor") < pause.index("scripts/weatherctl.sh stop")
    assert "system_monitor_process_group_pids" in text
    assert "sudo kill -KILL $pids" in text


def test_v6_hotfix_cutover_stops_weak_revision_writer_before_bootstrap() -> None:
    text = HOTFIX_CUTOVER.read_text(encoding="utf-8")
    pause = text.split("pause_services()", 1)[1].split("resume_services()", 1)[0]
    assert '"$ROOT/scripts/apply_current_weak_source_revisions_v6.py"' in pause
    assert 'step "pause_all_archive_writers"' in text
    assert text.index('step "pause_all_archive_writers"') < text.index(
        'step "create_new_generation_p0_and_replay_all_canonical_events"'
    )


def test_v6_hotfix_rollback_never_installs_or_resumes_from_partial_copy() -> None:
    text = HOTFIX_CUTOVER.read_text(encoding="utf-8")
    restore = text.split("restore_database()", 1)[1].split("recover()", 1)[0]
    recover = text.split("recover()", 1)[1].split("trap recover EXIT", 1)[0]
    assert '/bin/cp -c "$LOCAL_STAGE" "$restore_tmp"' in restore
    assert 'stat -f%z "$LOCAL_STAGE"' in restore
    assert "PRAGMA quick_check;" in restore
    assert 'mv "$restore_tmp" data/archive.db' in restore
    assert 'restore_ok=0' in recover
    assert 'services remain paused fail-closed' in recover
    assert '[ "$restore_ok" = 1 ]' in recover


def test_v6_hotfix_cutover_proves_shared_writer_gate_is_released() -> None:
    text = HOTFIX_CUTOVER.read_text(encoding="utf-8")
    pause = text.index('step "pause_all_archive_writers"')
    install = text.index(
        'step "install_peak_timing_and_default_off_market_transaction_runtime_then_freeze_candidate"'
    )
    gate = text.index("verify_archive_writer_gate_quiescent", pause)
    assert pause < gate < install
    assert 'owner="physical_state_bayes_v6_cutover_quiescence_probe"' in text
    assert "wait_sec=120.0" in text


def test_v6_hotfix_recovery_baseline_requires_exact_token_and_integrity() -> None:
    text = HOTFIX_CUTOVER.read_text(encoding="utf-8")
    recovery = text.split("verify_explicit_recovery_baseline()", 1)[1].split(
        "restore_database()", 1
    )[0]
    assert 'PHYSICAL_STATE_BAYES_V6_RECOVERY_CONFIRM:-' in recovery
    assert 'RECOVER_VALID_V6_BASELINE' in recovery
    assert 'PRAGMA quick_check' in recovery
    assert 'validate_v6_activation' in recovery
    assert 'recovery_baseline_schema_not_pure_additive' in recovery
