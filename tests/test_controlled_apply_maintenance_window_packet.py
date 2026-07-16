from pathlib import Path

from scripts import controlled_apply_maintenance_window_packet as packet


def test_system_monitor_pause_command_is_best_effort_and_uses_bootout() -> None:
    """A benign post-disable stop result must not trigger maintenance recovery."""

    assert "launchctl disable system/com.weather.monitor.daemon || true" in packet.SYSTEM_MONITOR_PAUSE_COMMAND
    assert "launchctl bootout system /Library/LaunchDaemons/com.weather.monitor.daemon.plist" in packet.SYSTEM_MONITOR_PAUSE_COMMAND
    assert "&& sudo launchctl stop" not in packet.SYSTEM_MONITOR_PAUSE_COMMAND


def test_maintenance_packet_blocks_write_capable_ui_and_hard_source(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.write_text("", encoding="utf-8")
    lsof = f"""COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
Python  36814 xiong   10u   REG   1,17      0 123 {db}
Python  76686 xiong    3u   REG   1,17      0 123 {db}
Python  49887 xiong    4r   REG   1,17      0 123 {db}
"""
    ps = """36814 1 U /Library/Python -m ui.server
76686 59397 U /Library/Python /Users/xiong/weather/scripts/hard_source_direct_poller.py --loop
49887 47418 U python scripts/archive_db_rebuild_build.py --source-db /Users/xiong/weather/data/archive.db
"""

    out = packet.build_maintenance_window_packet(
        db,
        lsof_output=lsof,
        ps_output=ps,
        sudo_status={
            "schema": "maintenance_window_sudo_status_v1",
            "sudo_available": True,
            "noninteractive_sudo_available": False,
            "returncode": 1,
            "stderr": "a password is required",
            "command": "sudo -n true",
        },
        generated_at="2026-07-06T00:00:00Z",
    )

    assert out["read_only"] is True
    assert out["guards"]["writes_archive_db"] is False
    assert out["guards"]["restarts_launchd"] is False
    assert out["writer_quiescent"] is False
    assert out["counts"]["writer_quiescence_blocker_count"] == 2
    assert out["counts"]["reader_warning_count"] == 1
    labels = set(out["required_stop_labels"])
    assert "gui/$(id -u)/com.xiong.weather.ui" in labels
    assert "system/com.weather.monitor.daemon" in labels
    blockers = {row["owner_class"]: row for row in out["blockers"]}
    assert blockers["ui_server"]["block_reason"] == "write_capable_archive_db_handle"
    assert blockers["hard_source_direct_poller"]["service_label"] == "system/com.weather.monitor.daemon"
    assert out["boundary"]["packet_executes_stop_or_start"] is False
    assert out["boundary"]["monitor_daemon_domain"] == "system/com.weather.monitor.daemon"
    assert out["stop_authorization"]["system_monitor_stop_required"] is True
    assert out["stop_authorization"]["noninteractive_sudo_available"] is False
    assert out["stop_authorization"]["can_pause_required_services_now"] is False
    assert "system_monitor_daemon_stop_requires_interactive_sudo" in out["stop_authorization"]["blockers"]
    assert packet.SYSTEM_MONITOR_PAUSE_COMMAND in out["operator_commands"]["suggested_pause_commands"]
    assert packet.SYSTEM_MONITOR_FORCE_PAUSE_COMMAND in out["operator_commands"]["suggested_force_pause_commands"]
    assert packet.SYSTEM_MONITOR_RESUME_COMMAND in out["operator_commands"]["suggested_resume_commands"]
    by_step = {row["step"]: row for row in out["maintenance_sequence"]}
    assert by_step["pause_system_monitor_daemon"]["command"] == packet.SYSTEM_MONITOR_PAUSE_COMMAND
    assert by_step["pause_system_monitor_daemon"]["fallback_command"] == packet.SYSTEM_MONITOR_FORCE_PAUSE_COMMAND
    assert by_step["resume_services"]["command"] == (
        packet.SYSTEM_MONITOR_RESUME_COMMAND + " && "
        "scripts/weatherctl.sh start  # resume GUI LaunchAgents after apply"
    )
    assert by_step["resume_services"]["required"] is True
    assert out["next_boundary"] == "obtain_interactive_sudo_for_system_monitor_pause_then_rerun_packet"


def test_maintenance_packet_allows_reader_only_unknown_holder(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.write_text("", encoding="utf-8")
    lsof = f"""COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
Python  12345 xiong    4r   REG   1,17      0 123 {db}
"""
    ps = "12345 1 S python scripts/read_only_probe.py --db data/archive.db\n"

    out = packet.build_maintenance_window_packet(
        db,
        lsof_output=lsof,
        ps_output=ps,
        sudo_status={"noninteractive_sudo_available": False},
        generated_at="2026-07-06T00:00:00Z",
    )

    assert out["writer_quiescent"] is True
    assert out["counts"]["writer_quiescence_blocker_count"] == 0
    assert out["counts"]["reader_warning_count"] == 1
    assert out["reader_warnings"][0]["owner_class"] == "unknown_db_holder"
    assert out["next_boundary"] == "run_controlled_apply_commands"
    assert out["stop_authorization"]["system_monitor_stop_required"] is False
    assert out["stop_authorization"]["can_pause_required_services_now"] is True
    by_step = {row["step"]: row for row in out["maintenance_sequence"]}
    assert by_step["resume_services"]["command"] == "no_writer_services_to_resume"
    assert by_step["resume_services"]["required"] is False


def test_maintenance_packet_ignores_lsof_pid_that_vanished_before_ps_revalidation(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.write_text("", encoding="utf-8")
    lsof = f"""COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
sqlite3 34536 xiong    3u   REG   1,17      0 123 {db}
"""

    out = packet.build_maintenance_window_packet(
        db,
        lsof_output=lsof,
        ps_output="",
        sudo_status={"noninteractive_sudo_available": True},
        generated_at="2026-07-14T14:15:30Z",
    )

    assert out["writer_quiescent"] is True
    assert out["blockers"] == []
    assert out["counts"]["db_holder_process_count"] == 0
    assert out["holder_revalidation"]["vanished_pid_count"] == 1


def test_maintenance_packet_blocks_known_writer_process_without_current_db_handle(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.write_text("", encoding="utf-8")
    process_scan = """2521 732 S /Library/Python /Users/xiong/weather/scripts/hard_source_direct_poller.py --loop
1863 1 S /Library/Python /Users/xiong/weather/scripts/task_queue_worker.py
4183 4154 S /Library/Python /Users/xiong/weather/scripts/edge_executable_audit.py --event-date 2026-07-15
"""

    out = packet.build_maintenance_window_packet(
        db,
        lsof_output="",
        ps_output="",
        process_scan_output=process_scan,
        sudo_status={
            "schema": "maintenance_window_sudo_status_v1",
            "sudo_available": True,
            "noninteractive_sudo_available": False,
            "returncode": 1,
            "stderr": "a password is required",
            "command": "sudo -n true",
        },
        generated_at="2026-07-07T00:00:00Z",
    )

    assert out["writer_quiescent"] is False
    assert out["counts"]["db_holder_process_count"] == 0
    assert out["counts"]["write_capable_handle_process_count"] == 0
    assert out["counts"]["known_writer_process_active_count"] == 3
    labels = set(out["required_stop_labels"])
    assert "gui/$(id -u)/com.weather.task_queue_worker" in labels
    assert "gui/$(id -u)/com.weather.feedback_loop_hourly" in labels
    assert "system/com.weather.monitor.daemon" in labels
    blockers = {row["owner_class"]: row for row in out["blockers"]}
    assert blockers["hard_source_direct_poller"]["block_reason"] == "known_archive_db_writer_process_active"
    assert blockers["task_queue_worker"]["block_reason"] == "known_archive_db_writer_process_active"
    assert blockers["edge_executable_audit"]["block_reason"] == "known_archive_db_writer_process_active"
    assert out["operator_commands"]["suggested_pause_commands"] == [
        "scripts/weatherctl.sh stop",
        packet.SYSTEM_MONITOR_PAUSE_COMMAND,
    ]
    assert out["operator_commands"]["suggested_force_pause_commands"] == [packet.SYSTEM_MONITOR_FORCE_PAUSE_COMMAND]


def test_maintenance_packet_attributes_auto_trade_job_worker_to_target_realization_service(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.write_text("", encoding="utf-8")
    lsof = f"""COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
Python  4183 xiong    3u   REG   1,17      0 123 {db}
"""
    ps = (
        "4183 4154 SN /Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python "
        "/Users/xiong/weather/scan_weather_market.py --auto-trade-job-worker\n"
    )

    out = packet.build_maintenance_window_packet(
        db,
        lsof_output=lsof,
        ps_output=ps,
        sudo_status={"noninteractive_sudo_available": True},
        generated_at="2026-07-14T06:25:57Z",
    )

    assert out["writer_quiescent"] is False
    assert out["required_stop_labels"] == [
        "gui/$(id -u)/com.weather.target_realization_worker"
    ]
    blocker = out["blockers"][0]
    assert blocker["owner_class"] == "target_realization_worker"
    assert blocker["service_label"] == "gui/$(id -u)/com.weather.target_realization_worker"
    assert blocker["operator_action"] == ""
    assert "scripts/weatherctl.sh stop" in out["operator_commands"]["suggested_pause_commands"]


def test_maintenance_packet_does_not_treat_shell_text_mention_as_target_worker(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.write_text("", encoding="utf-8")
    process_scan = (
        "5001 1 S /bin/zsh -c ps | rg "
        "'scan_weather_market.py --auto-trade-job-worker'\n"
    )

    out = packet.build_maintenance_window_packet(
        db,
        lsof_output="",
        ps_output="",
        process_scan_output=process_scan,
        sudo_status={"noninteractive_sudo_available": True},
        generated_at="2026-07-14T06:30:00Z",
    )

    assert out["writer_quiescent"] is True
    assert out["counts"]["known_writer_process_active_count"] == 0
    assert out["required_stop_labels"] == []


def test_maintenance_packet_does_not_classify_git_hash_object_paths_as_writer(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.write_text("", encoding="utf-8")
    process_scan = (
        "3001 1 R git -c core.hooksPath=/dev/null hash-object -- "
        "scripts/hard_source_direct_poller.py scripts/polymarket_monitor_daemon.sh\n"
    )

    out = packet.build_maintenance_window_packet(
        db,
        lsof_output="",
        ps_output="",
        process_scan_output=process_scan,
        sudo_status={"noninteractive_sudo_available": False},
        generated_at="2026-07-07T00:00:00Z",
    )

    assert out["writer_quiescent"] is True
    assert out["counts"]["known_writer_process_active_count"] == 0
    assert out["required_stop_labels"] == []


def test_maintenance_packet_classifies_market_ws_listener_as_gui_writer(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.write_text("", encoding="utf-8")
    lsof = f"""COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
Python  30225 xiong    7u   REG   1,17      0 123 {db}
"""
    ps = "30225 1 S /Library/Python /Users/xiong/weather/scripts/market_ws_listener.py\n"

    out = packet.build_maintenance_window_packet(
        db,
        lsof_output=lsof,
        ps_output=ps,
        sudo_status={
            "schema": "maintenance_window_sudo_status_v1",
            "sudo_available": True,
            "noninteractive_sudo_available": False,
            "returncode": 1,
            "stderr": "a password is required",
            "command": "sudo -n true",
        },
        generated_at="2026-07-06T00:00:00Z",
    )

    assert out["writer_quiescent"] is False
    assert out["counts"]["writer_quiescence_blocker_count"] == 1
    assert out["required_stop_labels"] == ["gui/$(id -u)/com.weather.market_ws"]
    blockers = {row["owner_class"]: row for row in out["blockers"]}
    assert blockers["market_ws_listener"]["service_label"] == "gui/$(id -u)/com.weather.market_ws"
    assert out["stop_authorization"]["system_monitor_stop_required"] is False
    assert out["stop_authorization"]["can_pause_required_services_now"] is True
    assert out["operator_commands"]["suggested_pause_commands"] == ["scripts/weatherctl.sh stop"]
    assert out["operator_commands"]["suggested_resume_commands"] == [
        "scripts/weatherctl.sh start  # resume GUI LaunchAgents after apply"
    ]
    assert out["operator_commands"]["manual_wait_or_stop"] == []


def test_maintenance_packet_classifies_task_queue_worker_as_gui_writer(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.write_text("", encoding="utf-8")
    lsof = f"""COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
Python  1863 xiong    3u   REG   1,17      0 123 {db}
"""
    ps = "1863 1 S /Library/Python /Users/xiong/weather/scripts/task_queue_worker.py\n"

    out = packet.build_maintenance_window_packet(
        db,
        lsof_output=lsof,
        ps_output=ps,
        sudo_status={
            "schema": "maintenance_window_sudo_status_v1",
            "sudo_available": True,
            "noninteractive_sudo_available": False,
            "returncode": 1,
            "stderr": "a password is required",
            "command": "sudo -n true",
        },
        generated_at="2026-07-07T00:00:00Z",
    )

    assert out["writer_quiescent"] is False
    assert out["counts"]["writer_quiescence_blocker_count"] == 1
    assert out["required_stop_labels"] == ["gui/$(id -u)/com.weather.task_queue_worker"]
    blockers = {row["owner_class"]: row for row in out["blockers"]}
    assert blockers["task_queue_worker"]["service_label"] == "gui/$(id -u)/com.weather.task_queue_worker"
    assert blockers["task_queue_worker"]["operator_action"] == ""
    assert out["operator_commands"]["suggested_pause_commands"] == ["scripts/weatherctl.sh stop"]
    assert out["operator_commands"]["suggested_resume_commands"] == [
        "scripts/weatherctl.sh start  # resume GUI LaunchAgents after apply"
    ]
    assert out["operator_commands"]["manual_wait_or_stop"] == []


def test_maintenance_packet_classifies_manual_blockers_for_backup_and_anomaly_monitor(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.write_text("", encoding="utf-8")
    lsof = f"""COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
Python  1887 xiong    3u   REG   1,17      0 123 {db}
sqlite3 4505 xiong    3u   REG   1,17      0 123 {db}
"""
    ps = """1887 1 S /Library/Python /Users/xiong/weather/scripts/weather_market_anomaly_monitor.py --city chengdu
4505 3932 U+ sqlite3 /Users/xiong/weather/data/archive.db .backup
"""

    out = packet.build_maintenance_window_packet(
        db,
        lsof_output=lsof,
        ps_output=ps,
        sudo_status={"noninteractive_sudo_available": False},
        generated_at="2026-07-06T00:00:00Z",
    )

    assert out["writer_quiescent"] is False
    assert out["required_stop_labels"] == []
    blockers = {row["owner_class"]: row for row in out["blockers"]}
    assert blockers["weather_market_anomaly_monitor"]["block_reason"] == "write_capable_archive_db_handle"
    assert blockers["sqlite_backup"]["block_reason"] == "archive_db_backup_in_progress"
    assert blockers["weather_market_anomaly_monitor"]["operator_action"] == (
        "wait_for_weather_market_anomaly_monitor_or_stop_pid(pid=1887)"
    )
    assert blockers["sqlite_backup"]["operator_action"] == "wait_for_backup_process_to_finish(pid=4505)"
    assert out["operator_commands"]["suggested_pause_commands"] == []
    assert out["operator_commands"]["manual_wait_or_stop"] == [
        "wait_for_weather_market_anomaly_monitor_or_stop_pid(pid=1887)",
        "wait_for_backup_process_to_finish(pid=4505)",
    ]


def test_maintenance_packet_summary_keeps_operator_commands(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.write_text("", encoding="utf-8")
    full = packet.build_maintenance_window_packet(
        db,
        lsof_output="",
        ps_output="",
        sudo_status={"noninteractive_sudo_available": False},
        generated_at="2026-07-06T00:00:00Z",
    )

    summary = packet.compact_packet(full)

    assert summary["schema"] == "controlled_apply_maintenance_window_packet_v1_summary"
    assert summary["writer_quiescent"] is True
    assert summary["guards"]["writes_archive_db"] is False
    assert "verify_writer_quiescence" in summary["operator_commands"]
    assert summary["operator_commands"]["suggested_pause_commands"] == []
    assert summary["operator_commands"]["suggested_resume_commands"] == []
    assert summary["stop_authorization"]["can_pause_required_services_now"] is True


def test_maintenance_packet_only_suggests_system_stop_when_system_monitor_holds_db(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.write_text("", encoding="utf-8")
    lsof = f"""COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
Python  36814 xiong   10u   REG   1,17      0 123 {db}
"""
    ps = "36814 1 U /Library/Python -m ui.server\n"

    out = packet.build_maintenance_window_packet(
        db,
        lsof_output=lsof,
        ps_output=ps,
        sudo_status={
            "schema": "maintenance_window_sudo_status_v1",
            "sudo_available": True,
            "noninteractive_sudo_available": False,
            "returncode": 1,
            "stderr": "a password is required",
            "command": "sudo -n true",
        },
        generated_at="2026-07-06T00:00:00Z",
    )

    assert out["required_stop_labels"] == ["gui/$(id -u)/com.xiong.weather.ui"]
    assert out["stop_authorization"]["system_monitor_stop_required"] is False
    assert out["stop_authorization"]["can_pause_required_services_now"] is True
    assert out["operator_commands"]["suggested_pause_commands"] == ["scripts/weatherctl.sh stop"]
    assert out["operator_commands"]["suggested_resume_commands"] == [
        "scripts/weatherctl.sh start  # resume GUI LaunchAgents after apply"
    ]
    by_step = {row["step"]: row for row in out["maintenance_sequence"]}
    assert by_step["pause_gui_refresh_writers"]["required"] is True
    assert by_step["pause_system_monitor_daemon"]["required"] is False
    assert by_step["resume_services"]["command"] == "scripts/weatherctl.sh start  # resume GUI LaunchAgents after apply"
    assert by_step["resume_services"]["required"] is True
