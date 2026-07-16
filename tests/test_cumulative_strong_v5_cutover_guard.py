from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_initial_v5_cutover_refuses_existing_v5_activation_before_patch_or_service_mutation() -> None:
    text = (ROOT / "scripts/cutover_cumulative_strong_likelihood_v5.sh").read_text(
        encoding="utf-8"
    )
    guard = text.index("initial v5 cutover already complete; no mutation performed")
    patch_check = text.index("git apply --check scripts/cumulative_strong_likelihood_v5.patch")
    pause = text.index('step "pause_all_writers"')

    assert guard < patch_check < pause
    assert "bash scripts/refresh_reported_x_runtime_manifest_epoch.sh" in text[guard:patch_check]
    assert "CUTOVER_COMPLETE=1" in text[guard:patch_check]


def test_manifest_refresh_verifies_unique_probability_and_safe_trade_loop_after_readiness() -> None:
    text = (ROOT / "scripts/refresh_reported_x_runtime_manifest_epoch.sh").read_text(
        encoding="utf-8"
    )
    readiness = text.index('step "resume_services_and_verify_readiness"')
    aggregate = text.index('step "verify_unique_probability_and_safe_trade_loop"')
    external_copy = text.index('step "copy_backup_to_external_volume_if_available"')

    assert readiness < aggregate < external_copy
    aggregate_block = text[aggregate:external_copy]
    assert "scripts/production_chain_uniqueness_audit.py" in aggregate_block
    assert "trade_strategy_loop_audit.py" in aggregate_block


def test_manifest_refresh_prunes_only_superseded_same_family_local_backups_after_acceptance() -> None:
    text = (ROOT / "scripts/refresh_reported_x_runtime_manifest_epoch.sh").read_text(
        encoding="utf-8"
    )
    external_copy = text.index('step "copy_backup_to_external_volume_if_available"')
    rollback_disabled = text.index("trap - EXIT", external_copy)
    prune = text.index('step "prune_superseded_local_refresh_backups"', rollback_disabled)
    complete = text.index("reported_x_runtime_manifest_epoch_refresh_complete", prune)

    assert external_copy < rollback_disabled < prune < complete
    function = text.split("prune_superseded_local_refresh_backups()", 1)[1].split(
        "recover()", 1
    )[0]
    assert "archive.pre-reported-x-manifest-refresh.*.db" in function
    assert '[ "$candidate" = "$BACKUP" ] && continue' in function
    assert "reported_x_refresh_local_backup_prune_manifest_v1" in function
    assert "deleted_superseded_local_backups" in function
    assert "retained_current_local_backup" in function


def test_manifest_refresh_archives_only_checkpoint_consumed_pending_plans_before_acceptance() -> None:
    text = (ROOT / "scripts/refresh_reported_x_runtime_manifest_epoch.sh").read_text(
        encoding="utf-8"
    )
    replay = text.index('step "commit_new_p0_generation_and_replay_all_canonical_events"')
    archive = text.index('step "archive_pending_plans_consumed_by_new_checkpoint"')
    acceptance = text.index('step "zero_order_acceptance"')

    assert replay < archive < acceptance
    archive_block = text[archive:acceptance]
    assert "scripts/amsc_probability_pending_queue.py archive-consumed" in archive_block
    assert "--confirm ARCHIVE_CONSUMED_AMSC_PROBABILITY_PLANS" in archive_block
    assert 'sudo "$PYTHON_BIN"' in archive_block


def test_manifest_refresh_stops_system_monitor_before_user_domain_writers() -> None:
    text = (ROOT / "scripts" / "refresh_reported_x_runtime_manifest_epoch.sh").read_text(
        encoding="utf-8"
    )
    pause = text.split("pause_services()", 1)[1].split("resume_services()", 1)[0]

    assert "SERVICES_PAUSED=1" in pause
    assert "stop_system_monitor" in pause
    assert "scripts/weatherctl.sh stop" in pause
    assert pause.index("SERVICES_PAUSED=1") < pause.index("stop_system_monitor")
    assert pause.index("stop_system_monitor") < pause.index("scripts/weatherctl.sh stop")


def test_manifest_refresh_force_stops_only_repository_system_monitor_after_wait() -> None:
    text = (ROOT / "scripts" / "refresh_reported_x_runtime_manifest_epoch.sh").read_text(
        encoding="utf-8"
    )
    stopper = text.split("stop_system_monitor()", 1)[1].split("pause_services()", 1)[0]

    assert 'pgrep -f "$ROOT/scripts/polymarket_monitor_daemon.sh"' in text
    assert "system_monitor_process_group_pids" in text
    assert 'ps -p "$pid" -o pgid=' in stopper
    assert 'pids="$(system_monitor_process_group_pids "$monitor_pgids")"' in stopper
    assert "sudo launchctl disable system/com.weather.monitor.daemon" in stopper
    assert "sudo launchctl bootout system /Library/LaunchDaemons/com.weather.monitor.daemon.plist" in stopper
    assert "sudo kill -TERM $pids" in stopper
    assert "sudo kill -KILL $pids" in stopper


def test_manifest_refresh_preflight_covers_readiness_and_trade_loop_before_pause() -> None:
    text = (ROOT / "scripts" / "refresh_reported_x_runtime_manifest_epoch.sh").read_text(
        encoding="utf-8"
    )
    preflight = text.split('step "preflight_document_and_runtime_tests"', 1)[1].split(
        'step "freeze_activation_and_pause_all_writers"', 1
    )[0]

    assert "tests/test_production_reported_x_runtime_readiness.py" in preflight
    assert "tests/test_production_reported_x_amsc_archive_probability_ingress.py" in preflight
    assert "tests/test_p2_canonical_amsc_forward_writer.py" in preflight
    assert "tests/test_archive_db_write_queue.py" in preflight
    assert "tests/test_fetch_zuuu_metar_taf.py" in preflight
    assert "tests/test_trade_strategy_loop_audit.py" in preflight
    assert "python3 trade_strategy_loop_audit.py" in preflight
    assert preflight.index("python3 trade_strategy_loop_audit.py") < preflight.index("sudo -v")


def test_manifest_refresh_proves_writer_gate_reclaimable_after_quiescence() -> None:
    text = (ROOT / "scripts" / "refresh_reported_x_runtime_manifest_epoch.sh").read_text(
        encoding="utf-8"
    )
    freeze = text.index('step "freeze_activation_and_pause_all_writers"')
    quiescence = text.index("controlled_apply_maintenance_window_packet.py", freeze)
    reclaim = text.index('step "prove_archive_writer_gate_reclaimable"', quiescence)
    backup = text.index('step "create_exact_pre_refresh_backup"', reclaim)
    activation = text.index('step "materialize_new_orderless_runtime_activation"', backup)

    assert quiescence < reclaim < backup < activation
    reclaim_block = text[reclaim:backup]
    assert 'owner="reported_x_manifest_refresh_quiescence_probe"' in reclaim_block
    assert "wait_sec=5.0" in reclaim_block
