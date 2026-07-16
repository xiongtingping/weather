#!/usr/bin/env python3
"""Read-only production audit for the archive.db WAL latency keeper."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Any


SCHEMA_VERSION = "archive_db_wal_read_keeper_audit_v1"


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _lsof_access_modes(pid: int, db: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-F", "fan", "--", str(db)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    modes: list[str] = []
    current = ""
    for line in str(result.stdout or "").splitlines():
        if line.startswith("f"):
            current = ""
        elif line.startswith("a"):
            current = line[1:].strip().lower()
        elif line.startswith("n") and Path(line[1:]).resolve() == db:
            modes.append(current)
    return modes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--max-heartbeat-age-sec", type=float, default=15.0)
    args = parser.parse_args(argv)
    db = args.db.expanduser().resolve()
    status_path = args.status.expanduser().resolve()
    blockers: list[str] = []
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        status = {}
        blockers.append("wal_keeper_status_unreadable")
    try:
        pid = int(status.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    heartbeat = _time(status.get("heartbeat_at"))
    age = (
        max(0.0, (datetime.now(timezone.utc) - heartbeat).total_seconds())
        if heartbeat is not None
        else None
    )
    if status.get("schema") != "archive_db_wal_read_keeper_v1":
        blockers.append("wal_keeper_status_schema_invalid")
    if status.get("state") != "running":
        blockers.append("wal_keeper_not_running")
    if Path(str(status.get("db") or "")).expanduser().resolve() != db:
        blockers.append("wal_keeper_db_binding_mismatch")
    if status.get("read_only") is not True or status.get("writes_archive_db") is not False:
        blockers.append("wal_keeper_read_only_boundary_invalid")
    if status.get("holds_transaction") is not False:
        blockers.append("wal_keeper_transaction_boundary_invalid")
    if status.get("places_orders") is not False:
        blockers.append("wal_keeper_order_boundary_invalid")
    if age is None or age > max(0.1, float(args.max_heartbeat_age_sec)):
        blockers.append("wal_keeper_heartbeat_stale")
    pid_inspection_permission_denied = False
    try:
        os.kill(pid, 0)
    except PermissionError:
        # A system LaunchDaemon owns the production keeper.  EPERM proves that
        # the PID exists but this caller cannot inspect it; it is not evidence
        # that the process died.
        pid_inspection_permission_denied = True
    except ProcessLookupError:
        blockers.append("wal_keeper_pid_not_alive")
    except OSError:
        blockers.append("wal_keeper_pid_inspection_failed")
    access_modes = _lsof_access_modes(pid, db) if pid > 0 else []
    if not access_modes:
        blockers.append(
            "wal_keeper_archive_handle_inspection_permission_denied"
            if pid_inspection_permission_denied
            else "wal_keeper_archive_handle_missing"
        )
    elif any(mode != "r" for mode in access_modes):
        blockers.append("wal_keeper_archive_handle_not_read_only")
    result = {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "db": str(db),
        "status_path": str(status_path),
        "pid": pid,
        "heartbeat_age_sec": round(age, 3) if age is not None else None,
        "archive_handle_access_modes": access_modes,
        "pid_inspection_permission_denied": pid_inspection_permission_denied,
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
