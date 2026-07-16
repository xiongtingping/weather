from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CUTOVER = ROOT / "scripts/cutover_amsc_slot_complete_v6_and_trade_caps.sh"
RECOVERY = ROOT / "scripts/recover_archive_after_failed_slot_complete_cutover.sh"
READINESS = ROOT / "scripts/production_probability_v6_readiness.py"


def test_cutover_reclaims_only_dead_writer_lease_after_quiescence() -> None:
    text = CUTOVER.read_text(encoding="utf-8")
    pause = text.index('step "pause_all_archive_writers"')
    quiescence = text.index("verify_quiescence", pause)
    reclaim = text.index("reclaim_quiescent_writer_lease", quiescence)
    install = text.index('step "install_slot_complete_route_and_freeze_new_v6_activation"')

    assert pause < quiescence < reclaim < install
    function = text.split("reclaim_quiescent_writer_lease()", 1)[1].split("recover()", 1)[0]
    assert 'ps -p "$holder_pid"' in function
    assert 'sudo rm -rf "$lock"' in function
    assert "archive writer lease still has live holder" in function


def test_cutover_rollback_is_same_filesystem_atomic_without_second_db_copy() -> None:
    text = CUTOVER.read_text(encoding="utf-8")
    restore = text.split("restore_database()", 1)[1].split(
        "reclaim_quiescent_writer_lease()", 1
    )[0]

    assert 'test "$(stat -f%d "$LOCAL_STAGE")" = "$(stat -f%d data)"' in restore
    assert 'local failed_db="${LOCAL_STAGE}.failed-cutover-db"' in restore
    assert 'mv data/archive.db "$failed_db"' in restore
    assert 'mv "$LOCAL_STAGE" data/archive.db' in restore
    assert "PRAGMA quick_check;" in restore
    assert 'cp -p "$LOCAL_STAGE"' not in restore


def test_failed_rollback_never_resumes_services_or_deletes_recovery_materials() -> None:
    text = CUTOVER.read_text(encoding="utf-8")
    recovery = text.split("recover()", 1)[1].split("trap recover EXIT", 1)[0]

    assert "rollback_safe=1" in recovery
    assert "allow_resume=0" in recovery
    assert 'if [ "$DB_MUTATED" = 1 ] && ! restore_database' in recovery
    assert "production_probability_v6_live_acceptance.py" in recovery
    assert '--activation "$ACTIVE_ACTIVATION"' in recovery
    assert 'if [ "$rollback_safe" != 1 ] || [ "$allow_resume" != 1 ]' in recovery
    assert "recovery_state=fail_closed_services_stopped" in recovery
    assert 'if [ "$CUTOVER_COMPLETE" = 1 ] || { [ "$rollback_safe" = 1 ]' in recovery
    assert "recovery_materials_preserved=true" in recovery


def test_emergency_recovery_stops_services_before_atomic_restore() -> None:
    text = RECOVERY.read_text(encoding="utf-8")
    stop = text.index("launchctl disable system/com.weather.monitor.daemon")
    handles = text.index('lsof "$ACTIVE"')
    source_check = text.index("sqlite3 \"$SOURCE\" 'PRAGMA quick_check;'")
    move_old = text.index('mv "$ACTIVE" "$FAILED"')
    move_new = text.index('mv "$SOURCE" "$ACTIVE"')
    resume = text.index('bash "$ROOT/scripts/weatherctl.sh" start')

    assert stop < handles < source_check < move_old < move_new < resume
    assert "archive.db still has open handles" in text
    assert 'cp -p "$SOURCE"' not in text


def test_cutover_uses_candidate_pre_rehearsal_readiness_before_service_pause() -> None:
    cutover = CUTOVER.read_text(encoding="utf-8")
    readiness = READINESS.read_text(encoding="utf-8")

    preflight = cutover.index('step "preflight_existing_v6_candidate_route_and_trade_caps"')
    build = cutover.index("build_production_probability_v6_candidate_activation.py --write", preflight)
    candidate_audit = cutover.index("--cutover-candidate-pre-rehearsal", build)
    pause = cutover.index('step "pause_all_archive_writers"', candidate_audit)
    assert preflight < build < candidate_audit < pause
    assert 'parser.add_argument("--cutover-candidate-pre-rehearsal"' in readiness
    assert "ready_for_candidate_rehearsal" in readiness
    assert "not cutover_candidate_pre_rehearsal" in readiness
