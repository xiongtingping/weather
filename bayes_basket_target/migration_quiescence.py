"""Read-only writer-quiescence probe for the target M5 migration boundary."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "bayes_basket_target_writer_quiescence_receipt_v3"
MAX_RECEIPT_AGE_SECONDS = 120
CORE_SERVICES = (
    "com.weather.reported_trajectory_no",
    "com.weather.market_ws",
    "com.weather.clob_order_service",
)
WRITER_PROCESS_PATTERNS = (
    "reported_trajectory_no_daemon.py",
    "market_ws_listener.py",
    "clob_order_service.mjs",
)


def _sha(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _database_identity(db: Path) -> dict[str, int]:
    try:
        stat = db.stat()
    except OSError as exc:
        raise ValueError("writer_quiescence_database_stat_failed") from exc
    if not db.is_file() or db.is_symlink():
        raise ValueError("writer_quiescence_database_path_invalid")
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _observed_at(value: object) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("writer_quiescence_receipt_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("writer_quiescence_receipt_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _probe(command: Sequence[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except OSError as exc:
        return False, f"probe_error:{type(exc).__name__}:{exc}"
    return completed.returncode == 0, (completed.stdout or completed.stderr or "").strip()[:500]


def build_writer_quiescence_receipt(
    *,
    db_path: Path,
    uid: int | None = None,
    probe_launchd: bool = True,
) -> dict[str, Any]:
    """Probe only; never stops a process, changes launchd, or writes SQLite."""

    db = db_path.expanduser().resolve()
    uid_value = os.getuid() if uid is None else int(uid)
    service_states: dict[str, dict[str, bool]] = {"system": {}, "user": {}}
    probe_errors: list[str] = []
    for domain, prefix in (("system", "system"), ("user", f"gui/{uid_value}")):
        for label in CORE_SERVICES:
            if not probe_launchd:
                service_states[domain][label] = False
                continue
            loaded, output = _probe(["launchctl", "print", f"{prefix}/{label}"])
            service_states[domain][label] = loaded
            if output.startswith("probe_error:"):
                probe_errors.append(f"{domain}/{label}:{output}")

    writer_process_hits: list[str] = []
    for pattern in WRITER_PROCESS_PATTERNS:
        present, output = _probe(["/usr/bin/pgrep", "-af", "--", pattern])
        if present and output:
            pid = output.split(None, 1)[0]
            writer_process_hits.append(f"{pattern}:{pid}")
        elif output.startswith("probe_error:"):
            probe_errors.append(f"writer:{pattern}:{output}")

    all_services_absent = all(not value for values in service_states.values() for value in values.values())
    identity = {
        "schema": SCHEMA,
        "database": str(db),
        "database_identity": _database_identity(db),
        "observed_at_utc": _utc_now(),
        "service_states": service_states,
        "all_core_services_absent": all_services_absent,
        "writer_process_hits": writer_process_hits,
        "probe_errors": sorted(probe_errors),
        "quiescent": bool(all_services_absent and not writer_process_hits and not probe_errors),
        "boundary": {
            "read_only_probe": True,
            "writes_production_db": False,
            "stops_services": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "receipt_sha256": _sha(identity)}


def validate_writer_quiescence_receipt(
    receipt: Mapping[str, Any],
    *,
    db_path: Path,
    now_utc: datetime | None = None,
    max_age_seconds: int = MAX_RECEIPT_AGE_SECONDS,
    require_current_database_identity: bool = False,
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping) or receipt.get("schema") != SCHEMA:
        raise ValueError("writer_quiescence_receipt_schema_invalid")
    identity = {str(key): value for key, value in receipt.items() if key != "receipt_sha256"}
    if str(receipt.get("receipt_sha256") or "") != _sha(identity):
        raise ValueError("writer_quiescence_receipt_hash_invalid")
    if str(receipt.get("database") or "") != str(db_path.expanduser().resolve()):
        raise ValueError("writer_quiescence_receipt_database_mismatch")
    if require_current_database_identity and receipt.get(
        "database_identity"
    ) != _database_identity(db_path.expanduser().resolve()):
        raise ValueError("writer_quiescence_receipt_database_identity_mismatch")
    observed = _observed_at(receipt.get("observed_at_utc"))
    current = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_seconds = (current - observed).total_seconds()
    if age_seconds < -5 or age_seconds > int(max_age_seconds):
        raise ValueError("writer_quiescence_receipt_expired")
    if receipt.get("quiescent") is not True:
        raise ValueError("writer_quiescence_receipt_not_quiescent")
    fence_mode = receipt.get("probe_mode") == "sqlite_begin_immediate_fence"
    if receipt.get("probe_errors") != []:
        raise ValueError("writer_quiescence_receipt_process_or_probe_hits")
    if fence_mode:
        boundary = receipt.get("boundary", {})
        if (
            boundary.get("exclusive_target_write_fence") is not True
            or boundary.get("competing_begin_immediate_blocked") is not True
            or boundary.get("stops_services") is not False
            or boundary.get("target_database_only") is not True
        ):
            raise ValueError("writer_quiescence_receipt_boundary_invalid")
    else:
        if receipt.get("writer_process_hits") != []:
            raise ValueError(
                "writer_quiescence_receipt_process_or_probe_hits"
            )
        if receipt.get("boundary", {}).get("read_only_probe") is not True:
            raise ValueError("writer_quiescence_receipt_boundary_invalid")
    return dict(receipt)


def synthetic_writer_quiescence_receipt(db_path: Path) -> dict[str, Any]:
    """Build an explicit isolated-test receipt; never valid for CLI probing."""

    db = str(db_path.expanduser().resolve())
    identity = {
        "schema": SCHEMA,
        "database": db,
        "database_identity": _database_identity(Path(db)),
        "observed_at_utc": _utc_now(),
        "service_states": {"system": {label: False for label in CORE_SERVICES}, "user": {label: False for label in CORE_SERVICES}},
        "all_core_services_absent": True,
        "writer_process_hits": [],
        "probe_errors": [],
        "quiescent": True,
        "boundary": {
            "read_only_probe": True,
            "writes_production_db": False,
            "stops_services": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "receipt_sha256": _sha(identity)}


def isolated_target_writer_quiescence_receipt(db_path: Path) -> dict[str, Any]:
    """Prove that no process currently holds the isolated target database.

    Unrelated market-WS and gateway services may remain running, but the
    resident ``reported_trajectory_no`` owner and every open handle to the
    target SQLite file must be absent.  This is a real read-only probe, not an
    assumption derived from the target path being separate from ``archive``.
    """

    db = db_path.expanduser().resolve()
    probe_errors: list[str] = []
    system_loaded, system_output = _probe([
        "launchctl",
        "print",
        "system/com.weather.reported_trajectory_no",
    ])
    user_loaded, user_output = _probe([
        "launchctl",
        "print",
        f"gui/{os.getuid()}/com.weather.reported_trajectory_no",
    ])
    for label, loaded, output in (
        ("system", system_loaded, system_output),
        ("user", user_loaded, user_output),
    ):
        if not loaded and output.startswith("probe_error:"):
            probe_errors.append(f"{label}/reported_trajectory_no:{output}")

    held, lsof_output = _probe([
        "/usr/sbin/lsof",
        "-nP",
        "-Fpcfn",
        "--",
        str(db),
    ])
    writer_process_hits: list[str] = []
    if held:
        pid = ""
        command = ""
        for line in lsof_output.splitlines():
            if line.startswith("p"):
                if pid:
                    writer_process_hits.append(
                        f"{command or 'unknown'}:{pid}"
                    )
                pid = line[1:].strip()
                command = ""
            elif line.startswith("c"):
                command = line[1:].strip()
        if pid:
            writer_process_hits.append(f"{command or 'unknown'}:{pid}")
    elif lsof_output.startswith("probe_error:"):
        probe_errors.append(f"target_database_lsof:{lsof_output}")

    quiescent = bool(not writer_process_hits and not probe_errors)
    identity = {
        "schema": SCHEMA,
        "database": str(db),
        "database_identity": _database_identity(db),
        "observed_at_utc": _utc_now(),
        "probe_mode": "isolated_target_database_scope",
        "service_states": {
            "system": {
                "com.weather.reported_trajectory_no": system_loaded,
                "com.weather.market_ws": "not_probed_unrelated",
                "com.weather.clob_order_service": "not_probed_unrelated",
            },
            "user": {
                "com.weather.reported_trajectory_no": user_loaded,
                "com.weather.market_ws": "not_probed_unrelated",
                "com.weather.clob_order_service": "not_probed_unrelated",
            },
        },
        "all_core_services_absent": None,
        "writer_process_hits": writer_process_hits,
        "probe_errors": probe_errors,
        "quiescent": quiescent,
        "boundary": {
            "read_only_probe": True,
            "database_scope": "isolated_target_no_live",
            "reported_target_writer_absent": not writer_process_hits,
            "reported_service_loaded": bool(system_loaded or user_loaded),
            "target_database_handles_absent": not writer_process_hits,
            "unrelated_core_services_may_run": True,
            "writes_production_db": False,
            "stops_services": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "receipt_sha256": _sha(identity)}


def transaction_fenced_writer_quiescence_receipt(
    conn: sqlite3.Connection,
    *,
    db_path: Path,
) -> dict[str, Any]:
    """Attest a target-DB-only ``BEGIN IMMEDIATE`` maintenance fence.

    The caller must acquire the transaction before invoking this function.
    A second connection proves that another writer cannot acquire the same
    database.  Resident processes may remain alive: only writes to the
    isolated target DB are briefly fenced, so unrelated AMSC/AWC, market WS
    and gateway work is not stopped or restarted.
    """

    db = db_path.expanduser().resolve()
    database_rows = conn.execute("PRAGMA database_list").fetchall()
    connection_path = next(
        (
            str(row[2] or "")
            for row in database_rows
            if str(row[1]) == "main"
        ),
        "",
    )
    if (
        not conn.in_transaction
        or not connection_path
        or Path(connection_path).expanduser().resolve() != db
    ):
        raise ValueError("writer_quiescence_transaction_fence_missing")

    competitor_blocked = False
    try:
        with sqlite3.connect(
            db,
            timeout=0.0,
            isolation_level=None,
        ) as competitor:
            competitor.execute("PRAGMA busy_timeout=0")
            try:
                competitor.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise ValueError(
                        "writer_quiescence_competitor_probe_failed"
                    ) from exc
                competitor_blocked = True
            else:
                competitor.rollback()
    except ValueError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ValueError(
            "writer_quiescence_competitor_probe_unavailable"
        ) from exc
    if not competitor_blocked:
        raise ValueError("writer_quiescence_transaction_fence_unproven")

    identity = {
        "schema": SCHEMA,
        "database": str(db),
        "database_identity": _database_identity(db),
        "observed_at_utc": _utc_now(),
        "probe_mode": "sqlite_begin_immediate_fence",
        "service_states": {
            "system": "not_stopped",
            "user": "not_stopped",
        },
        "all_core_services_absent": None,
        "writer_process_hits": [],
        "probe_errors": [],
        "quiescent": True,
        "fence_owner_pid": os.getpid(),
        "boundary": {
            "read_only_probe": False,
            "exclusive_target_write_fence": True,
            "competing_begin_immediate_blocked": True,
            "target_database_only": True,
            "stops_services": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "receipt_sha256": _sha(identity)}


__all__ = [
    "CORE_SERVICES",
    "MAX_RECEIPT_AGE_SECONDS",
    "SCHEMA",
    "build_writer_quiescence_receipt",
    "isolated_target_writer_quiescence_receipt",
    "synthetic_writer_quiescence_receipt",
    "transaction_fenced_writer_quiescence_receipt",
    "validate_writer_quiescence_receipt",
]
