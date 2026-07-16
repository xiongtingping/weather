from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_v6_runtime_hotfix_cutover_is_station_day_atomic_and_orderless() -> None:
    text = (ROOT / "scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh").read_text()
    assert "TZ=Asia/Shanghai date +%F" in text
    assert "build_production_probability_v6_candidate_activation.py --write" in text
    assert "verify_candidate_frozen" in text
    assert "production_probability_v6_zero_order_drill.py" in text
    assert "production_probability_v6_host_rehearsal.py" in text
    assert "production_storage_boundary_audit.py --summary" in text
    assert "production_probability_v6_peak_timing_acceptance.py" in text
    assert "atomically_install_new_v6_activation" in text
    assert "create_new_generation_p0_and_replay_all_canonical_events" in text
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
    preview = text.index("python3 scripts/install_v6_market_transaction_branch_runtime.py")
    install = text.index("--apply --confirm INSTALL_V6_MARKET_TRANSACTION_BRANCH_RUNTIME")
    pause = text.index('step "pause_all_archive_writers"')
    replay = text.index("python3 scripts/replay_physical_state_bayes_v6_canonical.py")
    install_activation = text.index('step "atomically_install_new_v6_activation"')
    assert preview < sudo_boundary
    assert text.index("sudo -v") > sudo_boundary
    assert pause < install < replay < install_activation
    assert text.index("python3 scripts/production_probability_v6_zero_order_drill.py") < install_activation
    assert text.index("python3 scripts/production_probability_v6_host_rehearsal.py") < install_activation
    assert "production_probability_v6_canonical_event_drain_runtime.py\" \"$ACTIVE_DRAIN_RUNTIME" in text
    assert "production_probability_v6_amsc_archive_ingress.py\" \"$ACTIVE_INGRESS_RUNTIME" in text


def test_v6_runtime_hotfix_installs_default_off_market_branch_without_external_disk_dependency() -> None:
    text = (ROOT / "scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh").read_text()
    assert "install_v6_market_transaction_branch_runtime.py" in text
    assert "test_production_probability_v6_transaction_commit_runtime.py" in text
    assert "test_production_probability_v6_market_transaction_writer.py" in text
    assert "/Volumes/" not in text
    assert "EXTERNAL_BACKUP" not in text
