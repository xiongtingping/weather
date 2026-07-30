from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_v6_runtime_hotfix_cutover_is_station_day_atomic_and_orderless() -> None:
    text = (ROOT / "scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh").read_text()
    assert text.count("TZ=Asia/Shanghai date +%F") == 2
    assert "operations_date_et" in text
    assert "prepare_production_probability_v6_hotfix_bundle.py" in text
    assert "production_probability_v6_hotfix_bundle_audit.py" in text
    assert "production_probability_v6_hotfix_install_rehearsal.py" in text
    assert "verify_candidate_frozen" in text
    assert 'cp -p "$BUNDLE_DIR/zero_order.json"' in text
    assert 'cp -p "$BUNDLE_DIR/host_rehearsal.json"' in text
    assert "production_storage_boundary_audit.py --summary" in text
    assert "production_probability_v6_peak_timing_acceptance.py" in text
    assert "atomically_install_new_v6_activation" in text
    assert "create_new_generation_p0_and_replay_all_canonical_events" in text
    assert "create_new_generation_next_day_p0_at_strict_handoff_asof" in text
    assert "apply_next_day_forward_only_weak_source_revision" in text
    assert "NEXT_DAY_HANDOFF_AS_OF" in text
    assert "scripts/apply_current_weak_source_revisions_v6.py" in text
    assert "BOOTSTRAP_PRODUCTION_PHYSICAL_STATE_BAYES_V6" in text
    assert "check_ui_page_probability_consistency.py --runtime --refresh --summary" in text
    assert "old activation fallback forbidden" in text
    assert '"${LOCAL_STAGE}-wal" "${LOCAL_STAGE}-shm"' in text
    assert 'step "create_local_apfs_rollback_clone"' in text
    assert '/bin/cp -c data/archive.db "$LOCAL_STAGE"' in text
    assert "create_verified_external_database_backup" not in text
    assert "EXTERNAL_BACKUP_DIR" not in text
    assert "verify_external_backup_mount" not in text
    assert "/Volumes/" not in text
    assert "EXTERNAL_VOLUME" not in text
    assert "EXTERNAL_BACKUP" not in text
    assert "shasum -a 256 \"$LOCAL_STAGE\"" not in text
    assert "external_archive=outside_production_boundary_not_required" in text
    assert "--live" not in text
    assert "places_order" not in text


def test_v6_runtime_hotfix_defers_sudo_until_candidate_preview_then_rehearses_installed_source_while_paused() -> None:
    text = (ROOT / "scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh").read_text()
    sudo_boundary = text.index('step "authorize_maintenance_window"')
    prepare = text.index("python3 scripts/prepare_production_probability_v6_hotfix_bundle.py")
    rehearsal = text.index("python3 scripts/production_probability_v6_hotfix_install_rehearsal.py")
    pause = text.index('step "pause_all_archive_writers"')
    install = text.index('cp -p "$BUNDLE_DIR/production_probability_v6_canonical_event_drain_runtime.py"')
    p0 = text.index("python3 scripts/bootstrap_production_probability_v6.py")
    install_activation = text.index('step "atomically_install_new_v6_activation"')
    next_p0 = text.index('step "create_new_generation_next_day_p0_at_strict_handoff_asof"')
    weak_revision = text.index('step "apply_next_day_forward_only_weak_source_revision"')
    assert prepare < rehearsal < sudo_boundary
    assert "sudo -v" not in text
    assert 'MONITOR_DOMAIN="gui/$(id -u)/com.weather.monitor.daemon"' in text
    assert pause < install < install_activation < p0
    assert p0 < next_p0 < weak_revision
    assert text.index('cp -p "$BUNDLE_DIR/zero_order.json"') < install_activation
    assert text.index('cp -p "$BUNDLE_DIR/host_rehearsal.json"') < install_activation
    assert 'mv "$ACTIVE_DRAIN_RUNTIME.tmp.$$" "$ACTIVE_DRAIN_RUNTIME"' in text
    assert 'mv "$ACTIVE_INGRESS_RUNTIME.tmp.$$" "$ACTIVE_INGRESS_RUNTIME"' in text


def test_v6_runtime_hotfix_installs_default_off_market_branch_without_external_disk_dependency() -> None:
    text = (ROOT / "scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh").read_text()
    assert "install_v6_market_transaction_branch_runtime.py" in text
    assert "test_production_probability_v6_transaction_commit_runtime.py" in text
    assert "test_production_probability_v6_market_transaction_writer.py" in text
    assert "/Volumes/" not in text
    assert "EXTERNAL_BACKUP" not in text
