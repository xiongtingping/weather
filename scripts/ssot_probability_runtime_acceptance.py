#!/usr/bin/env python3
"""Read-only acceptance of the deployed SSOT probability runtime generation."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from joint_weak_baseline_artifact_runtime import load_approved_joint_weak_artifact  # noqa: E402
from p10_shadow_preparation_readiness import audit_p10_shadow_preparation_readiness  # noqa: E402
from scripts.probability_ssot_mainline_audit import run_audit as run_probability_ssot_mainline_audit  # noqa: E402


SCHEMA_VERSION = "ssot_probability_runtime_acceptance_v1"
EXPECTED_MONITOR_GENERATION = "ssot_probability_mainline_v1"
LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return _mapping(value)


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _pid_alive(value: Any) -> bool:
    try:
        pid = int(value)
        os.kill(pid, 0)
    except (TypeError, ValueError, OSError):
        return False
    return pid > 1


def build_acceptance(
    *,
    shadow_db: Path,
    monitor_status_path: Path,
    resident_status_path: Path,
    observed_at: datetime,
) -> dict[str, Any]:
    blockers: list[str] = []
    monitor = _read_json(monitor_status_path)
    monitor_age = None
    monitor_time = _time(monitor.get("observed_at"))
    if monitor_time is not None:
        monitor_age = max(0.0, (observed_at - monitor_time).total_seconds())
    if _text(monitor.get("runtime_generation")) != EXPECTED_MONITOR_GENERATION:
        blockers.append("ssot_probability_monitor_generation_not_loaded")
    if _text(monitor.get("state")) != "running" or not _pid_alive(monitor.get("monitor_pid")):
        blockers.append("ssot_probability_monitor_process_not_proven_running")

    local_date = observed_at.astimezone(LOCAL_ZONE).date()
    local_hour = observed_at.astimezone(LOCAL_ZONE).hour
    expected_prepared_date = local_date + timedelta(days=1) if local_hour >= 18 else local_date
    target = f"physical_weather_day_v1:ZUUU:{expected_prepared_date.isoformat()}"
    with sqlite3.connect(f"file:{shadow_db.resolve()}?mode=ro", uri=True) as conn:
        readiness = audit_p10_shadow_preparation_readiness(
            conn,
            target_identity=target,
            final_label_db_path=ROOT / "data" / "shadow" / "resolution_final_labels.db",
            inbox_dir=ROOT / "data" / "shadow" / "p10_amsc_inbox",
            source_status_path=ROOT / "data" / "shadow" / "p10_amsc_source_status_latest.json",
            source_raw_archive_dir=ROOT / "data" / "shadow" / "p10_amsc_raw",
        )
    if readiness.get("ok") is not True:
        blockers.extend(readiness.get("blockers") or ["ssot_probability_target_not_ready"])
    if _text(_mapping(readiness.get("shadow_db")).get("prepared_cycle_status")) != "prepared_cycle_present":
        blockers.append("ssot_probability_expected_prepared_cycle_missing")

    resident = _read_json(resident_status_path)
    resident_expected = expected_prepared_date == local_date
    resident_age = None
    resident_time = _time(resident.get("observed_at"))
    if resident_time is not None:
        resident_age = max(0.0, (observed_at - resident_time).total_seconds())
    if resident_expected:
        if (
            _text(resident.get("state")) != "running"
            or _text(resident.get("event_date")) != local_date.isoformat()
            or not _pid_alive(resident.get("resident_pid"))
            or resident_age is None
            or resident_age > 15.0
        ):
            blockers.append("ssot_probability_current_day_resident_not_running_or_fresh")

    weak = load_approved_joint_weak_artifact()
    if weak.get("ok") is not True:
        blockers.append("ssot_probability_default_joint_weak_artifact_invalid")
    structural = run_probability_ssot_mainline_audit(root=ROOT)
    if structural.get("ok") is not True:
        blockers.extend(structural.get("violations") or ["ssot_probability_structural_audit_failed"])
    blockers = sorted(set(blockers))
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "accepted_runtime_ready" if not blockers else "blocked",
        "blockers": blockers,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "expected_target_identity": target,
        "monitor": {
            "runtime_generation": monitor.get("runtime_generation"),
            "state": monitor.get("state"),
            "pid_alive": _pid_alive(monitor.get("monitor_pid")),
            "status_age_seconds": monitor_age,
        },
        "resident": {
            "required_for_current_local_day": resident_expected,
            "event_date": resident.get("event_date"),
            "state": resident.get("state"),
            "pid_alive": _pid_alive(resident.get("resident_pid")),
            "status_age_seconds": resident_age,
        },
        "target_readiness": {
            "ok": readiness.get("ok"),
            "state": readiness.get("state"),
            "runtime_blockers": readiness.get("blockers") or [],
            "offline_promotion_blockers": readiness.get("offline_promotion_blockers") or [],
            "shadow_db": readiness.get("shadow_db"),
        },
        "joint_weak": {
            "ok": weak.get("ok"),
            "artifact_sha256": _mapping(weak.get("artifact")).get("artifact_sha256"),
            "artifact_path": weak.get("artifact_path"),
        },
        "structural_mainline": {
            "ok": structural.get("ok"),
            "state": structural.get("state"),
            "violations": structural.get("violations") or [],
        },
        "boundary": {
            "read_only": True,
            "offline_promotion_blockers_are_not_runtime_blockers": True,
            "places_orders": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shadow-db", type=Path, default=ROOT / "data" / "shadow" / "p10_runtime.db")
    parser.add_argument("--monitor-status", type=Path, default=ROOT / "data" / "shadow" / "p10_monitor_runtime_status_latest.json")
    parser.add_argument("--resident-status", type=Path, default=ROOT / "data" / "shadow" / "resident_p10_shadow_status_latest.json")
    args = parser.parse_args()
    try:
        result = build_acceptance(
            shadow_db=args.shadow_db.expanduser().resolve(),
            monitor_status_path=args.monitor_status.expanduser().resolve(),
            resident_status_path=args.resident_status.expanduser().resolve(),
            observed_at=datetime.now(timezone.utc),
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": [f"ssot_probability_runtime_acceptance_failed:{type(exc).__name__}"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
