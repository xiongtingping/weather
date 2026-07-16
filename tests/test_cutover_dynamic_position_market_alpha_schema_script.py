from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cutover_script_enforces_quiescence_external_backup_and_disabled_activation() -> None:
    text = (ROOT / "scripts" / "cutover_dynamic_position_market_alpha_schema.sh").read_text(
        encoding="utf-8"
    )
    assert "scripts/weatherctl.sh stop" in text
    assert "controlled_apply_maintenance_window_packet.py" in text
    assert text.count("controlled_apply_maintenance_window_packet.py") == 2
    assert "/Volumes/*" in text
    assert "ACTIVATE_DYNAMIC_POSITION_MARKET_ALPHA_SCHEMA" in text
    assert "assert_disabled_activation_contracts" in text
    assert "already_applied_read_only_acceptance" in text
    assert "production_trade_gate_zero_audit.py" in text
    assert "production_probability_transaction_b_cutover_readiness.py" in text
    assert "production_dynamic_three_leg_readiness.py" in text
    assert "dynamic_three_leg_execution" in text


def test_weatherctl_stop_retires_governed_target_worker_child() -> None:
    text = (ROOT / "scripts" / "weatherctl.sh").read_text(encoding="utf-8")
    assert "_retire_target_realization_worker_children" in text
    assert 'scan_weather_market.py.*--auto-trade-job-worker' in text
    stop_section = text.split("  stop)", 1)[1].split("  restart)", 1)[0]
    assert "_retire_target_realization_worker_children" in stop_section


def test_weatherctl_stop_retires_all_governed_writer_children() -> None:
    text = (ROOT / "scripts" / "weatherctl.sh").read_text(encoding="utf-8")
    stop_section = text.split("  stop)", 1)[1].split("    ;;", 1)[0]

    assert "_retire_governed_writer_children" in text
    assert "_governed_writer_child_pids" in text
    assert "_retire_governed_writer_children" in stop_section
    for token in (
        "market_ws_listener.py",
        "market_refresh_1m.sh",
        "model_scan.sh",
        "task_queue_worker.py",
        "weather_strategy_batch.py",
        "scan_weather_market.py",
        "ui/server.py",
        "-m ui[.]server",
        "archive_db_auto_guard.py",
        "archive_db_rebuild_build.py",
        "db_maintenance.sh",
        "sqlite3 (data/archive[.]db|$ROOT/data/archive[.]db).*[.]backup",
    ):
        assert token in text


def test_weatherctl_maintenance_covers_archive_guard_launchagents_without_kickstart() -> None:
    text = (ROOT / "scripts" / "weatherctl.sh").read_text(encoding="utf-8")
    assert '"com.weather.archive_db_auto_guard"' in text
    assert '"com.weather.archive_db_governance_offpeak"' in text
    bootstrap = text.split("_bootstrap_label()", 1)[1].split("_bounded_launchctl_client", 1)[0]
    assert "com.weather.archive_db_auto_guard|com.weather.archive_db_governance_offpeak" in bootstrap


def test_weatherctl_maintenance_governs_feedback_loop_writer() -> None:
    text = (ROOT / "scripts" / "weatherctl.sh").read_text(encoding="utf-8")
    labels = text.split("labels=(", 1)[1].split(")", 1)[0]
    assert '"com.weather.feedback_loop_hourly"' in labels
    assert '"$ROOT/scripts/feedback_loop_hourly.sh"' in text
    assert '"$ROOT/scripts/feedback_loop.py"' in text


def test_weatherctl_start_bounds_each_launchctl_kickstart() -> None:
    text = (ROOT / "scripts" / "weatherctl.sh").read_text(encoding="utf-8")
    assert "_bounded_kickstart" in text
    assert 'kill -TERM "$child_pid"' in text
    assert '_bounded_kickstart "${GUI_DOMAIN}/${label}"' in text


def test_weatherctl_stop_disables_keepalive_before_bounded_bootout() -> None:
    text = (ROOT / "scripts" / "weatherctl.sh").read_text(encoding="utf-8")
    bootout = text.split("_bootout_label()", 1)[1].split(
        "_retire_target_realization", 1
    )[0]
    assert "_bounded_launchctl_client" in text
    assert '_bounded_launchctl_client disable "${GUI_DOMAIN}/${label}"' in bootout
    assert '_bounded_launchctl_client stop "${GUI_DOMAIN}/${label}"' in bootout
    assert '_bounded_launchctl_client bootout "${GUI_DOMAIN}/${label}"' in bootout


def test_weatherctl_start_enables_label_before_bootstrap() -> None:
    text = (ROOT / "scripts" / "weatherctl.sh").read_text(encoding="utf-8")
    bootstrap = text.split("_bootstrap_label()", 1)[1].split("_bounded_launchctl_client", 1)[0]
    assert bootstrap.index('launchctl enable "${GUI_DOMAIN}/${label}"') < bootstrap.index(
        'launchctl bootstrap "${GUI_DOMAIN}" "$plist"'
    )
