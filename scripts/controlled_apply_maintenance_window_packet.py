#!/usr/bin/env python3
"""Read-only writer-quiescence packet for controlled archive.db applies.

This script does not stop services, create backups, write archive.db, restart
launchd, fetch weather data, or place orders. It only classifies current DB
holders so an operator can decide whether a maintenance window is actually
quiet enough for apply commands that require ``--writer-quiescent``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "controlled_apply_maintenance_window_packet_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"
SYSTEM_MONITOR_LABEL = "system/com.weather.monitor.daemon"
SYSTEM_MONITOR_PLIST = "/Library/LaunchDaemons/com.weather.monitor.daemon.plist"
# ``launchctl stop`` can legitimately return non-zero after ``disable`` has
# already caused a KeepAlive daemon to exit.  A maintenance window must not
# abort at that point and immediately run its EXIT recovery trap.  The
# mandatory packet check that follows is the authority for quiescence, so the
# pause command is intentionally best-effort and uses bootout as the durable
# removal path.
SYSTEM_MONITOR_PAUSE_COMMAND = (
    f"sudo launchctl disable {SYSTEM_MONITOR_LABEL} || true; "
    f"sudo launchctl bootout system {SYSTEM_MONITOR_PLIST} 2>/dev/null || true"
)
SYSTEM_MONITOR_FORCE_PAUSE_COMMAND = (
    SYSTEM_MONITOR_PAUSE_COMMAND
)
SYSTEM_MONITOR_RESUME_COMMAND = (
    f"sudo launchctl enable {SYSTEM_MONITOR_LABEL} && "
    f"(sudo launchctl bootstrap system {SYSTEM_MONITOR_PLIST} 2>/dev/null || true) && "
    f"sudo launchctl kickstart -k {SYSTEM_MONITOR_LABEL}  # resume after apply"
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _q(value: Path | str) -> str:
    return shlex.quote(str(value))


def _guards() -> dict[str, bool]:
    return {
        "executes_sql_writes": False,
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_event_fact_summary_cache": False,
        "writes_daily_weather_reviews": False,
        "writes_daily_calibration_records": False,
        "writes_bias_correction_candidates": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "fetches_external_weather_sources": False,
        "historical_backfill": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _run_text(cmd: Sequence[str], *, timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=max(0.5, float(timeout)),
            check=False,
        )
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or "timeout"
    return int(proc.returncode), proc.stdout or "", proc.stderr or ""


def _sudo_noninteractive_status() -> dict[str, Any]:
    sudo = shutil.which("sudo")
    if not sudo:
        return {
            "schema": "maintenance_window_sudo_status_v1",
            "sudo_available": False,
            "noninteractive_sudo_available": False,
            "returncode": 127,
            "stderr": "sudo_not_found",
            "command": "sudo -n true",
        }
    code, _out, err = _run_text([sudo, "-n", "true"], timeout=3.0)
    return {
        "schema": "maintenance_window_sudo_status_v1",
        "sudo_available": True,
        "noninteractive_sudo_available": code == 0,
        "returncode": code,
        "stderr": err.strip(),
        "command": "sudo -n true",
    }


def _parse_system_monitor_launchd_state(disabled_raw: str, print_raw: str) -> dict[str, Any]:
    disabled_state = "unknown"
    label_match = re.search(r'"?com\.weather\.monitor\.daemon"?\s*=>\s*(\w+)', str(disabled_raw or ""))
    if label_match:
        disabled_state = label_match.group(1)
    state_match = re.search(r"^\s*state\s*=\s*(\w+)", str(print_raw or ""), re.M)
    pid_match = re.search(r"^\s*pid\s*=\s*(\d+)", str(print_raw or ""), re.M)
    path_match = re.search(r"^\s*path\s*=\s*(.+)$", str(print_raw or ""), re.M)
    properties_match = re.search(r"^\s*properties\s*=\s*(.+)$", str(print_raw or ""), re.M)
    properties = str(properties_match.group(1)).strip() if properties_match else ""
    return {
        "schema": "system_monitor_launchd_state_v1",
        "label": SYSTEM_MONITOR_LABEL,
        "plist": SYSTEM_MONITOR_PLIST,
        "disabled_state": disabled_state,
        "enabled": disabled_state == "enabled",
        "disabled": disabled_state == "disabled",
        "state": str(state_match.group(1)).strip() if state_match else "unknown",
        "pid": int(pid_match.group(1)) if pid_match else None,
        "path": str(path_match.group(1)).strip() if path_match else "",
        "keepalive": "keepalive" in properties.lower(),
        "properties": properties,
        "pause_command": SYSTEM_MONITOR_PAUSE_COMMAND,
        "force_pause_command": SYSTEM_MONITOR_FORCE_PAUSE_COMMAND,
        "resume_command": SYSTEM_MONITOR_RESUME_COMMAND,
    }


def _collect_system_monitor_launchd_state() -> dict[str, Any]:
    disabled_code, disabled_out, disabled_err = _run_text(["launchctl", "print-disabled", "system"], timeout=3.0)
    print_code, print_out, print_err = _run_text(["launchctl", "print", SYSTEM_MONITOR_LABEL], timeout=3.0)
    state = _parse_system_monitor_launchd_state(disabled_out, print_out)
    state.update(
        {
            "print_disabled_returncode": disabled_code,
            "print_disabled_stderr": disabled_err.strip(),
            "print_returncode": print_code,
            "print_stderr": print_err.strip(),
            "available": disabled_code == 0 or print_code == 0,
        }
    )
    return state


def parse_lsof_output(raw: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in str(raw or "").splitlines():
        text = line.rstrip()
        if not text or text.startswith("COMMAND "):
            continue
        parts = text.split(None, 8)
        if len(parts) < 8:
            continue
        command, pid, user, fd, typ, device, size_off, node = parts[:8]
        name = parts[8] if len(parts) > 8 else ""
        try:
            pid_int = int(pid)
        except Exception:
            continue
        fd_text = str(fd or "")
        write_capable = "u" in fd_text.lower() or "w" in fd_text.lower()
        rows.append(
            {
                "command": command,
                "pid": pid_int,
                "user": user,
                "fd": fd_text,
                "type": typ,
                "device": device,
                "size_off": size_off,
                "node": node,
                "name": name,
                "write_capable_handle": write_capable,
            }
        )
    return rows


def parse_ps_output(raw: str) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for line in str(raw or "").splitlines():
        text = line.strip()
        if not text or text.startswith("PID "):
            continue
        parts = text.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except Exception:
            continue
        out[pid] = {
            "pid": pid,
            "ppid": ppid,
            "stat": parts[2],
            "command_line": parts[3],
        }
    return out


def _launchctl_label_for_command(command_line: str) -> str:
    text = str(command_line or "")
    executable = text.split(None, 1)[0] if text.strip() else ""
    if "-m ui.server" in text or "/ui/server.py" in text:
        return "gui/$(id -u)/com.xiong.weather.ui"
    if executable.endswith("Python") and "scripts/hard_source_direct_poller.py" in text and "--loop" in text:
        return "system/com.weather.monitor.daemon"
    if executable.endswith(("bash", "zsh", "sh")) and "scripts/polymarket_monitor_daemon.sh" in text:
        return "system/com.weather.monitor.daemon"
    if "scripts/archive_db_auto_guard.py" in text or "scripts/archive_db_rebuild_build.py" in text:
        return "gui/$(id -u)/com.weather.archive_db_auto_guard"
    if "scripts/feedback_loop_hourly.sh" in text or "scripts/edge_executable_audit.py" in text:
        return "gui/$(id -u)/com.weather.feedback_loop_hourly"
    if "scripts/amsc_zuuu_metar_taf_poll" in text:
        return "system/com.weather.monitor.daemon"
    if "scripts/market_ws_listener.py" in text or "scripts/market_ws_listener.sh" in text:
        return "gui/$(id -u)/com.weather.market_ws"
    if "scripts/task_queue_worker.py" in text:
        return "gui/$(id -u)/com.weather.task_queue_worker"
    if (
        Path(executable).name.lower().startswith("python")
        and "scan_weather_market.py" in text
        and "--auto-trade-job-worker" in text
    ):
        return "gui/$(id -u)/com.weather.target_realization_worker"
    if "scripts/weather_market_anomaly_monitor.py" in text:
        return ""
    if "sqlite3 " in text and " .backup" in text:
        return ""
    if "market_refresh_1m" in text:
        return "gui/$(id -u)/com.weather.market_refresh_1m"
    if "model_scan" in text:
        return "gui/$(id -u)/com.weather.model_scan"
    return ""


def _owner_class_for_command(command_line: str) -> str:
    text = str(command_line or "")
    executable = text.split(None, 1)[0] if text.strip() else ""
    if "-m ui.server" in text or "/ui/server.py" in text:
        return "ui_server"
    if executable.endswith("Python") and "scripts/hard_source_direct_poller.py" in text:
        return "hard_source_direct_poller"
    if executable.endswith(("bash", "zsh", "sh")) and "scripts/polymarket_monitor_daemon.sh" in text:
        return "weather_monitor_daemon"
    if "scripts/archive_db_auto_guard.py" in text or "scripts/archive_db_rebuild_build.py" in text:
        return "archive_db_auto_guard"
    if "scripts/edge_executable_audit.py" in text:
        return "edge_executable_audit"
    if "scripts/feedback_loop_hourly.sh" in text:
        return "feedback_loop_hourly"
    if "scripts/amsc_zuuu_metar_taf_poll" in text:
        return "weather_monitor_daemon"
    if "scripts/market_ws_listener.py" in text or "scripts/market_ws_listener.sh" in text:
        return "market_ws_listener"
    if "scripts/task_queue_worker.py" in text:
        return "task_queue_worker"
    if (
        Path(executable).name.lower().startswith("python")
        and "scan_weather_market.py" in text
        and "--auto-trade-job-worker" in text
    ):
        return "target_realization_worker"
    if "scripts/weather_market_anomaly_monitor.py" in text:
        return "weather_market_anomaly_monitor"
    if "sqlite3 " in text and " .backup" in text:
        return "sqlite_backup"
    if "market_refresh" in text:
        return "market_refresh"
    if "model_scan" in text:
        return "model_scan"
    return "unknown_db_holder"


def _is_writer_owner(owner_class: str) -> bool:
    return owner_class in {
        "ui_server",
        "hard_source_direct_poller",
        "weather_monitor_daemon",
        "edge_executable_audit",
        "feedback_loop_hourly",
        "market_ws_listener",
        "task_queue_worker",
        "target_realization_worker",
        "market_refresh",
        "model_scan",
    }


def _operator_action_for_blocker(row: Mapping[str, Any]) -> str:
    owner_class = str(row.get("owner_class") or "")
    pid = int(row.get("pid") or 0)
    if owner_class == "sqlite_backup":
        return f"wait_for_backup_process_to_finish(pid={pid})"
    if owner_class == "weather_market_anomaly_monitor":
        return f"wait_for_weather_market_anomaly_monitor_or_stop_pid(pid={pid})"
    if not str(row.get("service_label") or ""):
        return f"inspect_or_stop_unknown_archive_db_holder(pid={pid})"
    return ""


def classify_db_holders(
    lsof_rows: Sequence[Mapping[str, Any]],
    ps_rows: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in lsof_rows:
        try:
            pid = int(row.get("pid") or 0)
        except Exception:
            continue
        if pid <= 0:
            continue
        ps = dict(ps_rows.get(pid) or {})
        command_line = str(ps.get("command_line") or row.get("command") or "")
        owner_class = _owner_class_for_command(command_line)
        service_label = _launchctl_label_for_command(command_line)
        item = grouped.setdefault(
            pid,
            {
                "pid": pid,
                "ppid": ps.get("ppid"),
                "stat": str(ps.get("stat") or ""),
                "command": str(row.get("command") or ""),
                "command_line": command_line,
                "owner_class": owner_class,
                "service_label": service_label,
                "fd_count": 0,
                "write_capable_fd_count": 0,
                "read_only_fd_count": 0,
                "fds": [],
            },
        )
        item["fd_count"] = int(item.get("fd_count") or 0) + 1
        fd = str(row.get("fd") or "")
        item["fds"].append(fd)
        if bool(row.get("write_capable_handle")):
            item["write_capable_fd_count"] = int(item.get("write_capable_fd_count") or 0) + 1
        else:
            item["read_only_fd_count"] = int(item.get("read_only_fd_count") or 0) + 1
    rows = []
    for item in grouped.values():
        owner_class = str(item.get("owner_class") or "")
        write_capable = int(item.get("write_capable_fd_count") or 0) > 0
        writer_owner = _is_writer_owner(owner_class)
        item["writer_quiescence_blocker"] = bool(write_capable or writer_owner)
        item["block_reason"] = (
            "archive_db_backup_in_progress"
            if owner_class == "sqlite_backup" and write_capable
            else "write_capable_archive_db_handle"
            if write_capable
            else ("known_archive_db_writer_owner_active" if writer_owner else "")
        )
        item["operator_action"] = _operator_action_for_blocker(item)
        rows.append(item)
    rows.sort(key=lambda row: (not bool(row.get("writer_quiescence_blocker")), int(row.get("pid") or 0)))
    return rows


def _ps_for_pids(
    pids: Sequence[int],
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    clean = [int(pid) for pid in pids if int(pid or 0) > 0]
    if not clean:
        return {}, {"available": True, "returncode": 0, "stderr": "", "requested_pid_count": 0}
    code, out, _err = _run_text(["ps", "-p", ",".join(str(pid) for pid in clean), "-o", "pid=,ppid=,stat=,command="])
    if code not in (0, 1):
        return {}, {
            "available": False,
            "returncode": code,
            "stderr": _err.strip(),
            "requested_pid_count": len(clean),
        }
    rows = parse_ps_output(out)
    return rows, {
        "available": True,
        "returncode": code,
        "stderr": _err.strip(),
        "requested_pid_count": len(clean),
        "live_pid_count": len(rows),
    }


def _collect_process_scan() -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    code, out, err = _run_text(["ps", "-axo", "pid=,ppid=,stat=,command="])
    rows = parse_ps_output(out) if code == 0 else {}
    return rows, {
        "available": code == 0,
        "returncode": code,
        "stderr": err.strip(),
        "row_count": len(rows),
    }


def _known_writer_process_blockers(
    process_rows: Mapping[int, Mapping[str, Any]],
    *,
    exclude_pids: set[int],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for pid, row in process_rows.items():
        try:
            pid_int = int(pid)
        except Exception:
            continue
        if pid_int <= 0 or pid_int in exclude_pids:
            continue
        command_line = str(row.get("command_line") or "")
        owner_class = _owner_class_for_command(command_line)
        if not _is_writer_owner(owner_class):
            continue
        service_label = _launchctl_label_for_command(command_line)
        blockers.append(
            {
                "pid": pid_int,
                "ppid": row.get("ppid"),
                "stat": str(row.get("stat") or ""),
                "command": str(command_line.split(None, 1)[0] if command_line else ""),
                "command_line": command_line,
                "owner_class": owner_class,
                "service_label": service_label,
                "fd_count": 0,
                "write_capable_fd_count": 0,
                "read_only_fd_count": 0,
                "fds": [],
                "writer_quiescence_blocker": True,
                "block_reason": "known_archive_db_writer_process_active",
                "operator_action": "",
            }
        )
    blockers.sort(key=lambda row: (str(row.get("service_label") or ""), int(row.get("pid") or 0)))
    return blockers


def _collect_lsof(db_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lsof = shutil.which("lsof")
    if not lsof:
        return [], {"available": False, "returncode": 127, "stderr": "lsof_not_found"}
    code, out, err = _run_text([lsof, str(db_path)])
    rows = parse_lsof_output(out)
    return rows, {
        "available": True,
        "returncode": code,
        "stderr": err.strip(),
        "row_count": len(rows),
    }


def build_maintenance_window_packet(
    db_path: Path | str = DEFAULT_DB,
    *,
    generated_at: str | None = None,
    lsof_output: str | None = None,
    ps_output: str | None = None,
    process_scan_output: str | None = None,
    sudo_status: Mapping[str, Any] | None = None,
    max_items_output: int = 20,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    generated = generated_at or _now_utc()
    if lsof_output is None:
        lsof_rows, lsof_meta = _collect_lsof(db)
    else:
        lsof_rows = parse_lsof_output(lsof_output)
        lsof_meta = {
            "available": True,
            "returncode": 0,
            "stderr": "",
            "row_count": len(lsof_rows),
            "source": "provided_lsof_output",
        }
    lsof_pid_count_before_revalidation = len({int(row.get("pid") or 0) for row in lsof_rows})
    if ps_output is None:
        ps_rows, holder_revalidation = _ps_for_pids(
            [int(row.get("pid") or 0) for row in lsof_rows]
        )
    else:
        ps_rows = parse_ps_output(ps_output)
        holder_revalidation = {
            "available": True,
            "returncode": 0,
            "stderr": "",
            "requested_pid_count": lsof_pid_count_before_revalidation,
            "live_pid_count": len(ps_rows),
            "source": "provided_ps_output",
        }
    if holder_revalidation.get("available") is True:
        live_pids = set(ps_rows)
        lsof_rows = [
            row for row in lsof_rows
            if int(row.get("pid") or 0) in live_pids
        ]
    holder_revalidation["vanished_pid_count"] = max(
        0,
        lsof_pid_count_before_revalidation
        - len({int(row.get("pid") or 0) for row in lsof_rows}),
    )
    db_holders = classify_db_holders(lsof_rows, ps_rows)
    if process_scan_output is not None:
        process_rows = parse_ps_output(process_scan_output)
        process_scan_meta = {
            "available": True,
            "returncode": 0,
            "stderr": "",
            "row_count": len(process_rows),
            "source": "provided_process_scan_output",
        }
    elif lsof_output is None and ps_output is None and db == DEFAULT_DB.expanduser().resolve():
        process_rows, process_scan_meta = _collect_process_scan()
    else:
        process_rows = {}
        process_scan_meta = {
            "available": False,
            "returncode": None,
            "stderr": "",
            "row_count": 0,
            "source": "not_scanned_for_injected_fixture",
        }
    known_writer_process_blockers = _known_writer_process_blockers(
        process_rows,
        exclude_pids={int(row.get("pid") or 0) for row in db_holders},
    )
    holders = db_holders + known_writer_process_blockers
    blockers = [row for row in holders if bool(row.get("writer_quiescence_blocker"))]
    reader_warnings = [row for row in db_holders if not bool(row.get("writer_quiescence_blocker"))]
    labels = []
    for row in blockers:
        label = str(row.get("service_label") or "")
        if label and label not in labels:
            labels.append(label)
    system_monitor_stop_required = SYSTEM_MONITOR_LABEL in labels
    sudo_probe = dict(sudo_status or _sudo_noninteractive_status())
    noninteractive_sudo_available = bool(sudo_probe.get("noninteractive_sudo_available"))
    can_pause_required_services_now = bool(
        not system_monitor_stop_required or noninteractive_sudo_available
    )
    launchd_authorization_blockers: list[str] = []
    if system_monitor_stop_required and not noninteractive_sudo_available:
        launchd_authorization_blockers.append("system_monitor_daemon_stop_requires_interactive_sudo")
    system_monitor_launchd_state = (
        _collect_system_monitor_launchd_state()
        if system_monitor_stop_required and lsof_output is None and ps_output is None
        else {
            "schema": "system_monitor_launchd_state_v1",
            "label": SYSTEM_MONITOR_LABEL,
            "plist": SYSTEM_MONITOR_PLIST,
            "available": False,
            "not_collected_reason": "not_required_or_fixture_input",
            "pause_command": SYSTEM_MONITOR_PAUSE_COMMAND,
            "force_pause_command": SYSTEM_MONITOR_FORCE_PAUSE_COMMAND,
            "resume_command": SYSTEM_MONITOR_RESUME_COMMAND,
        }
    )

    gui_stop_required = any(str(label).startswith("gui/") for label in labels)
    pause_commands: list[str] = []
    force_pause_commands: list[str] = []
    resume_commands: list[str] = []
    if gui_stop_required:
        pause_commands.append("scripts/weatherctl.sh stop")
    if system_monitor_stop_required:
        pause_commands.append(SYSTEM_MONITOR_PAUSE_COMMAND)
        force_pause_commands.append(SYSTEM_MONITOR_FORCE_PAUSE_COMMAND)
        resume_commands.append(SYSTEM_MONITOR_RESUME_COMMAND)
    if gui_stop_required:
        resume_commands.append("scripts/weatherctl.sh start  # resume GUI LaunchAgents after apply")
    manual_wait_or_stop = [
        str(row.get("operator_action") or "")
        for row in blockers
        if str(row.get("operator_action") or "").strip()
    ]
    verify_command = (
        "python3 scripts/controlled_apply_maintenance_window_packet.py "
        f"--db {_q(db)} --summary --max-items-output {int(max_items_output)}"
    )
    writer_quiescent = not blockers and bool(lsof_meta.get("available"))
    return {
        "schema": SCHEMA_VERSION,
        "read_only": True,
        "execution_mode": "controlled_apply_maintenance_window_packet_read_only",
        "generated_at": generated,
        "db": str(db),
        "ok": bool(writer_quiescent),
        "writer_quiescent": bool(writer_quiescent),
        "blockers": blockers[: max(0, int(max_items_output))],
        "blockers_truncated": len(blockers) > max(0, int(max_items_output)),
        "reader_warnings": reader_warnings[: max(0, int(max_items_output))],
        "reader_warnings_truncated": len(reader_warnings) > max(0, int(max_items_output)),
        "counts": {
            "db_holder_process_count": len(db_holders),
            "known_writer_process_active_count": len(known_writer_process_blockers),
            "writer_quiescence_blocker_count": len(blockers),
            "reader_warning_count": len(reader_warnings),
            "write_capable_handle_process_count": sum(
                1 for row in db_holders if int(row.get("write_capable_fd_count") or 0) > 0
            ),
        },
        "lsof": lsof_meta,
        "holder_revalidation": holder_revalidation,
        "process_scan": process_scan_meta,
        "required_stop_labels": labels,
        "operator_commands": {
            "verify_writer_quiescence": verify_command,
            "suggested_pause_commands": pause_commands,
            "suggested_force_pause_commands": force_pause_commands,
            "suggested_resume_commands": resume_commands,
            "manual_wait_or_stop": manual_wait_or_stop,
        },
        "stop_authorization": {
            "schema": "maintenance_window_stop_authorization_v1",
            "system_monitor_stop_required": system_monitor_stop_required,
            "noninteractive_sudo_available": noninteractive_sudo_available,
            "can_pause_required_services_now": can_pause_required_services_now,
            "sudo_status": sudo_probe,
            "blockers": launchd_authorization_blockers,
            "system_monitor_launchd_state": system_monitor_launchd_state,
            "operator_route": (
                "run_interactive_sudo_launchctl_stop_system_com_weather_monitor_daemon_then_rerun_packet"
                if launchd_authorization_blockers
                else "pause_commands_can_be_run_from_current_context_or_not_needed"
            ),
        },
        "maintenance_sequence": [
            {
                "order": 1,
                "step": "pause_gui_refresh_writers",
                "command": "scripts/weatherctl.sh stop",
                "writes_archive_db": False,
                "restarts_launchd": True,
                "required": gui_stop_required,
            },
            {
                "order": 2,
                "step": "pause_system_monitor_daemon",
                "command": (
                    SYSTEM_MONITOR_PAUSE_COMMAND
                ),
                "fallback_command": SYSTEM_MONITOR_FORCE_PAUSE_COMMAND,
                "fallback_reason": "KeepAlive daemon may remain running after disable_and_stop; use bootout if packet still reports system monitor blockers",
                "writes_archive_db": False,
                "restarts_launchd": True,
                "required": system_monitor_stop_required,
                "boundary": f"use {SYSTEM_MONITOR_LABEL} only",
            },
            {
                "order": 3,
                "step": "verify_writer_quiescence",
                "command": verify_command,
                "writes_archive_db": False,
                "required": True,
                "pass_condition": "writer_quiescent == true and blocker_count == 0",
            },
            {
                "order": 4,
                "step": "run_controlled_apply_sequence",
                "command": "run commands from daily_review_post_apply_resolution_confirmation_packet",
                "writes_archive_db": True,
                "required": True,
            },
            {
                "order": 5,
                "step": "resume_services",
                "command": " && ".join(resume_commands) or "no_writer_services_to_resume",
                "writes_archive_db": False,
                "restarts_launchd": True,
                "required": bool(resume_commands),
            },
        ],
        "boundary": {
            "packet_executes_stop_or_start": False,
            "packet_executes_apply": False,
            "operator_confirmation_required_before_service_pause": True,
            "writer_quiescent_required_before_apply": True,
            "monitor_daemon_domain": "system/com.weather.monitor.daemon",
        },
        "guards": _guards(),
        "next_boundary": (
            "run_controlled_apply_commands"
            if writer_quiescent
            else "obtain_interactive_sudo_for_system_monitor_pause_then_rerun_packet"
            if launchd_authorization_blockers
            else "pause_required_writer_services_then_rerun_this_packet"
        ),
    }


def compact_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": packet.get("schema"),
        "read_only": bool(packet.get("read_only")),
        "execution_mode": packet.get("execution_mode"),
        "db": packet.get("db"),
        "ok": bool(packet.get("ok")),
        "writer_quiescent": bool(packet.get("writer_quiescent")),
        "counts": dict(packet.get("counts") or {}),
        "required_stop_labels": list(packet.get("required_stop_labels") or []),
        "blockers": list(packet.get("blockers") or []),
        "reader_warnings": list(packet.get("reader_warnings") or []),
        "operator_commands": dict(packet.get("operator_commands") or {}),
        "stop_authorization": dict(packet.get("stop_authorization") or {}),
        "boundary": dict(packet.get("boundary") or {}),
        "guards": dict(packet.get("guards") or {}),
        "next_boundary": packet.get("next_boundary"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--max-items-output", type=int, default=20)
    args = parser.parse_args(argv)
    packet = build_maintenance_window_packet(args.db, max_items_output=args.max_items_output)
    payload = compact_packet(packet) if args.summary else packet
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if bool(payload.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
