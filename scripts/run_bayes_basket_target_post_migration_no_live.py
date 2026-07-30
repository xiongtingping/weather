#!/usr/bin/env python3
"""Run the sole target worker once after the controlled SSOT migration.

This coordinator does not implement probability, market, basket, decision, or
reconciliation logic.  It proves the post-migration prerequisites and then
invokes the existing ``reported_trajectory_no --once production`` entrypoint,
whose ``TargetProductionWorker`` remains the sole target writer/compiler.

The command can never authorize live submission or call the order gateway.
Missing schema, artifact bindings, protected processes, resources, or official
WS evidence fail closed before the worker is started.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.activation import load_target_activation  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from bayes_basket_target.ssot import production_ssot_schema_status  # noqa: E402
from scripts.ingest_bayes_basket_target_artifacts import (  # noqa: E402
    packet_binding_counts,
    validate_packet_sha256,
)


SCHEMA = "bayes_basket_target_post_migration_no_live_run_v1"
EXECUTION_MODE = "production_no_live"
VERTICAL_SLICE_STATION_ID = "RKPK"
RESIDENT_ACCEPTANCE_SCHEMA = (
    "bayes_basket_target_probability_resident_two_round_acceptance_v1"
)
DEFAULT_CANONICAL_DB = ROOT / "data/archive.db"
DEFAULT_ACTIVATION = (
    ROOT
    / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
)
DEFAULT_PACKET = (
    ROOT / "output/audits/bayes_basket_target_no_live_candidate_artifacts_v2.json"
)
DEFAULT_MARKET_MANIFEST = (
    ROOT / "data/bayes_basket_target_official_ws_manifest.json"
)
DEFAULT_STATUS = (
    ROOT / "output/audits/bayes_basket_target_post_migration_worker_status.json"
)
DEFAULT_RECEIPT = (
    ROOT / "output/audits/bayes_basket_target_post_migration_no_live_run.json"
)
WORKER_ENTRYPOINT = ROOT / "scripts/reported_trajectory_no_daemon.py"

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_TARGET_DB_BYTES = 256 * 1024 * 1024
MAX_TARGET_WAL_BYTES = 64 * 1024 * 1024
MAX_CHILD_FILE_BYTES = MAX_TARGET_DB_BYTES + MAX_TARGET_WAL_BYTES
MAX_RSS_BYTES = 768 * 1024 * 1024
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 600

PROTECTED_PROCESS_PATTERNS = {
    "reported_trajectory_no": "scripts/reported_trajectory_no_daemon.py",
    "market_ws": "scripts/market_ws_listener.py",
    "clob_order_service": "scripts/clob_order_service.mjs",
}
RESIDENT_CORE_SERVICE_LABELS = frozenset(
    {
        "com.weather.reported_trajectory_no",
        "com.weather.market_ws",
        "com.weather.clob_order_service",
    }
)
PACKET_BINDING_KEYS = (
    "cash_phase_policies",
    "cash_phase_authority_manifests",
    "calibration_artifacts",
    "ensemble_selection_evidence",
    "ensemble_artifacts",
    "calibration_activations",
)
PROBABILITY_ACCEPTANCE_TABLES = (
    "basket_probability_evidence_artifact",
    "basket_probability_evidence_promotion",
    "basket_probability_evidence_activation",
    "basket_probability_update_proof",
    "basket_probability_bridge_history",
    "basket_probability_bridge_latest",
    "basket_probability_bridge_bucket_probability",
)
FALSE_BOUNDARY_KEYS = {
    "authorizes_order",
    "calls_order_gateway",
    "live_decision_write_authorized",
    "live_order_authorized",
    "live_submit_authorized",
    "places_orders",
}
ZERO_BOUNDARY_KEYS = {
    "exchange_orders_submitted",
    "orders_submitted",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _rss_bytes() -> int:
    raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return raw if sys.platform == "darwin" else raw * 1024


def _bounded_file(path: Path, *, label: str, limit: int = MAX_INPUT_BYTES) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise RuntimeError(f"{label}_missing_or_symlink")
    size = int(resolved.stat().st_size)
    if size <= 0 or size > limit:
        raise RuntimeError(f"{label}_size_invalid")
    return resolved


def _resource_status(db_path: Path, output_root: Path) -> dict[str, Any]:
    db_size = int(db_path.stat().st_size) if db_path.is_file() else 0
    wal_path = Path(str(db_path) + "-wal")
    wal_size = int(wal_path.stat().st_size) if wal_path.is_file() else 0
    free_bytes = int(shutil.disk_usage(output_root).free)
    rss_bytes = _rss_bytes()
    blockers: list[str] = []
    if db_size <= 0 or db_size > MAX_TARGET_DB_BYTES:
        blockers.append("target_db_size_invalid")
    if wal_size > MAX_TARGET_WAL_BYTES:
        blockers.append("target_wal_size_exceeded")
    if free_bytes < MIN_FREE_BYTES:
        blockers.append("minimum_free_disk_not_met")
    if rss_bytes > MAX_RSS_BYTES:
        blockers.append("coordinator_rss_exceeded")
    return {
        "state": "PASS" if not blockers else "BLOCKED_FAIL_CLOSED",
        "blockers": blockers,
        "target_db_bytes": db_size,
        "target_wal_bytes": wal_size,
        "free_bytes": free_bytes,
        "rss_bytes": rss_bytes,
        "limits": {
            "max_target_db_bytes": MAX_TARGET_DB_BYTES,
            "max_target_wal_bytes": MAX_TARGET_WAL_BYTES,
            "minimum_free_bytes": MIN_FREE_BYTES,
            "max_rss_bytes": MAX_RSS_BYTES,
            "max_input_bytes": MAX_INPUT_BYTES,
            "max_output_bytes": MAX_OUTPUT_BYTES,
        },
    }


def _default_process_snapshot() -> dict[str, int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,args="],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = result.stdout.splitlines()
    snapshot: dict[str, int] = {}
    for name, pattern in PROTECTED_PROCESS_PATTERNS.items():
        matches: list[int] = []
        for row in rows:
            if pattern not in row:
                continue
            head = row.strip().split(None, 1)
            if head and head[0].isdigit():
                matches.append(int(head[0]))
        if len(matches) != 1:
            raise RuntimeError(
                f"protected_process_identity_invalid:{name}:{len(matches)}"
            )
        snapshot[name] = matches[0]
    return snapshot


def _open_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        timeout=30.0,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA query_only=ON")
    return conn


def _db_fingerprint(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "auto_trade_decisions",
        "basket_probability_envelope",
        "basket_official_ws_snapshot",
        "basket_confirmed_inventory_snapshot",
        "basket_order_state_snapshot",
        "basket_target_execution_cycles",
        "basket_target_order_intents",
        "basket_target_order_events",
        "basket_target_capital_reservations",
        "basket_policy_training_fact",
        "basket_policy_training_label",
        "basket_target_execution_version_vectors",
    )
    available = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    return {
        table: (
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            if table in available
            else -1
        )
        for table in tables
    }


def probability_scope_snapshot(
    db_path: Path,
    *,
    station_id: str = VERTICAL_SLICE_STATION_ID,
) -> dict[str, Any]:
    """Read bounded target/non-target counts for every probability table.

    This is deliberately independent of the resident acceptance helper.  It
    gives the cutover a complete before/after witness, including bridge
    tables that the legacy two-round helper does not inspect.
    """

    station = str(station_id or "").strip().upper()
    database = resolve_target_database(db_path)
    with _open_read_only(database) as conn:
        present = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        tables: dict[str, dict[str, int | str]] = {}
        for table in PROBABILITY_ACCEPTANCE_TABLES:
            if table not in present:
                tables[table] = {
                    "state": "MISSING",
                    "target": -1,
                    "non_target": -1,
                }
                continue
            columns = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})")
            }
            if table == "basket_probability_bridge_bucket_probability":
                if (
                    "history_id" not in columns
                    or "basket_probability_bridge_history" not in present
                ):
                    tables[table] = {
                        "state": "STATION_JOIN_MISSING",
                        "target": -1,
                        "non_target": -1,
                    }
                    continue
                target = int(conn.execute(
                    "SELECT COUNT(*) FROM "
                    "basket_probability_bridge_bucket_probability b "
                    "JOIN basket_probability_bridge_history h "
                    "ON h.history_id=b.history_id WHERE h.station_id=?",
                    (station,),
                ).fetchone()[0])
                non_target = int(conn.execute(
                    "SELECT COUNT(*) FROM "
                    "basket_probability_bridge_bucket_probability b "
                    "JOIN basket_probability_bridge_history h "
                    "ON h.history_id=b.history_id "
                    "WHERE UPPER(COALESCE(h.station_id,''))<>?",
                    (station,),
                ).fetchone()[0])
                tables[table] = {
                    "state": "READY",
                    "target": target,
                    "non_target": non_target,
                }
                continue
            if "station_id" not in columns:
                tables[table] = {
                    "state": "STATION_COLUMN_MISSING",
                    "target": -1,
                    "non_target": -1,
                }
                continue
            target = int(conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE station_id=?",
                (station,),
            ).fetchone()[0])
            non_target = int(conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE UPPER(COALESCE(station_id,''))<>?",
                (station,),
            ).fetchone()[0])
            tables[table] = {
                "state": "READY",
                "target": target,
                "non_target": non_target,
            }
    return {
        "schema": "bayes_basket_target_probability_scope_snapshot_v1",
        "station_id": station,
        "database": str(database),
        "tables": tables,
        "missing_tables": sorted(
            table for table, value in tables.items()
            if value.get("state") != "READY"
        ),
        "non_target_counts": {
            table: int(value["non_target"])
            for table, value in tables.items()
        },
    }


def verify_two_round_resident_receipts(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    before_snapshot: Mapping[str, Any] | None = None,
    after_snapshot: Mapping[str, Any] | None = None,
    station_id: str = VERTICAL_SLICE_STATION_ID,
) -> dict[str, Any]:
    """Verify the bounded two-round RKPK resident acceptance contract.

    Forward OOS is intentionally not read here.  It is a promotion/live gate,
    never a production_no_live gate.
    """

    station = str(station_id or "").strip().upper()
    blockers: list[str] = []
    if station != VERTICAL_SLICE_STATION_ID:
        blockers.append("resident_acceptance_station_not_rkpk")
    rounds = (first, second)
    for index, receipt in enumerate(rounds, start=1):
        if receipt.get("state") != "PASS":
            blockers.append(f"resident_round_{index}_not_pass")
        if str(receipt.get("station_id") or station).upper() != station:
            blockers.append(f"resident_round_{index}_station_mismatch")
        if receipt.get("production_no_live") is not True:
            blockers.append(f"resident_round_{index}_production_mode_invalid")
        if receipt.get("calls_order_gateway") is not False:
            blockers.append(f"resident_round_{index}_gateway_boundary_invalid")
        if receipt.get("live_submit_authorized") is not False:
            blockers.append(f"resident_round_{index}_live_boundary_invalid")
        if int(receipt.get("orders_submitted") or 0) != 0:
            blockers.append(f"resident_round_{index}_orders_nonzero")
        resources = receipt.get("resources") or {}
        if (
            int(resources.get("database_bytes") or 0) > MAX_TARGET_DB_BYTES
            or int(resources.get("wal_bytes") or 0) > MAX_TARGET_WAL_BYTES
            or int(resources.get("free_bytes") or 0) < MIN_FREE_BYTES
        ):
            blockers.append(f"resident_round_{index}_resource_gate_invalid")
        resident_rss = resources.get("resident_service_rss_bytes")
        if not isinstance(resident_rss, Mapping) or not resident_rss:
            blockers.append(f"resident_round_{index}_rss_snapshot_missing")
        elif any(
            int(value or 0) > MAX_RSS_BYTES
            for value in resident_rss.values()
        ):
            blockers.append(f"resident_round_{index}_rss_gate_invalid")
        if int(resources.get("acceptance_process_rss_bytes") or 0) > MAX_RSS_BYTES:
            blockers.append(f"resident_round_{index}_acceptance_rss_gate_invalid")
        runtime = receipt.get("probability_evidence_runtime") or {}
        if int(runtime.get("candidate_activation_count_all") or 0) != 0:
            blockers.append(f"resident_round_{index}_candidate_activation_nonzero")
        if runtime.get("fallback_contract_failures"):
            blockers.append(f"resident_round_{index}_fallback_contract_invalid")
        fallback = runtime.get("fallback_proof_counts") or {}
        if int(fallback.get("P0_HISTORICAL_TILT") or 0) < 1:
            blockers.append(f"resident_round_{index}_theta_zero_not_consumed")
        if int(fallback.get("METAR_TRAJECTORY_UPDATE") or 0) < 1:
            blockers.append(f"resident_round_{index}_identity_lr_not_consumed")
        if runtime.get("missing_bridge_tables"):
            blockers.append(f"resident_round_{index}_bridge_schema_missing")

    first_services = first.get("services") or {}
    second_services = second.get("services") or {}
    if first_services != second_services:
        blockers.append("resident_service_pid_drift")
    if not first_services or not second_services:
        blockers.append("resident_service_snapshot_missing")
    if set(first_services) != set(RESIDENT_CORE_SERVICE_LABELS) or set(
        second_services
    ) != set(RESIDENT_CORE_SERVICE_LABELS):
        blockers.append("resident_three_service_set_invalid")
    if any(
        not isinstance(value, int) or value <= 0
        for value in (*first_services.values(), *second_services.values())
    ):
        blockers.append("resident_three_service_pid_invalid")

    first_core = (first.get("core_chain") or {}).get("latest") or {}
    second_core = (second.get("core_chain") or {}).get("latest") or {}
    if not first_core or not second_core:
        blockers.append("resident_core_direction_latest_missing")
    else:
        first_rowid = int(first_core.get("rowid") or 0)
        second_rowid = int(second_core.get("rowid") or 0)
        if second_rowid < first_rowid:
            blockers.append("resident_core_direction_rowid_rewind")
        elif second_rowid == first_rowid:
            # An unchanged poll must be zero-write.  Two identical durable
            # snapshots are a valid second round; requiring a fresh row here
            # would contradict the resident deduplication contract.
            if dict(second_core) != dict(first_core):
                blockers.append("resident_core_direction_same_row_drift")
        else:
            if (
                second_core.get("current_core_buckets")
                != first_core.get("core_buckets")
            ):
                blockers.append("resident_core_predecessor_mismatch")
            if (
                second_core.get("current_direction")
                != first_core.get("direction")
            ):
                blockers.append("resident_direction_predecessor_mismatch")

    if before_snapshot is None or after_snapshot is None:
        blockers.append("probability_scope_before_after_snapshot_missing")
    else:
        before_missing = before_snapshot.get("missing_tables") or []
        after_missing = after_snapshot.get("missing_tables") or []
        if before_missing or after_missing:
            blockers.append("probability_scope_table_missing")
        before_counts = before_snapshot.get("non_target_counts") or {}
        after_counts = after_snapshot.get("non_target_counts") or {}
        if before_counts != after_counts:
            blockers.append("non_rkpk_probability_table_write_detected")

    identity = {
        "schema": RESIDENT_ACCEPTANCE_SCHEMA,
        "station_id": station,
        "round_one_sha256": first.get("round_sha256"),
        "round_two_sha256": second.get("round_sha256"),
        "services": second_services,
        "non_rkpk_counts_before": (
            dict((before_snapshot or {}).get("non_target_counts") or {})
        ),
        "non_rkpk_counts_after": (
            dict((after_snapshot or {}).get("non_target_counts") or {})
        ),
        "blockers": sorted(set(blockers)),
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    return {
        **identity,
        "state": "PASS" if not blockers else "BLOCKED_FAIL_CLOSED",
        "content_sha256": content_sha256(identity),
    }


def _validate_activation(payload: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if payload.get("execution_mode") != EXECUTION_MODE:
        blockers.append("activation_execution_mode_invalid")
    for key in FALSE_BOUNDARY_KEYS | {"network_access", "writes_production_db"}:
        if key in payload and payload.get(key) is not False:
            blockers.append(f"activation_{key}_must_be_false")
    return blockers


def _validate_market_manifest(payload: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if payload.get("evidence_only") is not True:
        blockers.append("market_manifest_not_evidence_only")
    if payload.get("authorizes_order") is not False:
        blockers.append("market_manifest_authorizes_order_invalid")
    scope = payload.get("station_scope")
    if (
        not isinstance(scope, Mapping)
        or scope.get("mode") != "allowlist"
        or not isinstance(scope.get("station_ids"), list)
        or not scope.get("station_ids")
    ):
        blockers.append("market_manifest_exact_station_allowlist_missing")
    if not str(
        payload.get("event_date") or payload.get("target_local_date") or ""
    ).strip():
        blockers.append("market_manifest_event_date_missing")
    return blockers


def _validate_vertical_slice_scope(
    *,
    station_id: str,
    packet: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if station_id != VERTICAL_SLICE_STATION_ID:
        blockers.append("vertical_slice_station_not_frozen_authority")
    packet_stations = sorted(
        {
            str(value or "").strip().upper()
            for value in packet.get("station_ids") or []
            if str(value or "").strip()
        }
    )
    if packet_stations != [station_id]:
        blockers.append("artifact_packet_not_single_station_exact_scope")
    for key, rows in packet.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping) or "station_id" not in row:
                continue
            row_station = str(row.get("station_id") or "").strip().upper()
            if row_station and row_station != station_id:
                blockers.append(f"artifact_packet_cross_station_row:{key}")
                break
    scope = manifest.get("station_scope")
    manifest_stations = (
        sorted(
            {
                str(value or "").strip().upper()
                for value in scope.get("station_ids") or []
                if str(value or "").strip()
            }
        )
        if isinstance(scope, Mapping)
        else []
    )
    if manifest_stations != [station_id]:
        blockers.append("market_manifest_not_single_station_exact_scope")
    return blockers


def _validate_boundary_tree(value: Any, *, path: str = "$") -> list[str]:
    blockers: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in FALSE_BOUNDARY_KEYS and child is not False:
                blockers.append(f"{child_path}_must_be_false")
            if key in ZERO_BOUNDARY_KEYS:
                try:
                    valid_zero = float(child) == 0.0
                except (TypeError, ValueError):
                    valid_zero = False
                if not valid_zero:
                    blockers.append(f"{child_path}_must_be_zero")
            blockers.extend(_validate_boundary_tree(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            blockers.extend(
                _validate_boundary_tree(child, path=f"{path}[{index}]")
            )
    return blockers


def _build_preflight(
    *,
    station_id: str,
    db_path: Path,
    activation_path: Path,
    packet_path: Path,
    market_manifest_path: Path,
    output_root: Path,
    process_snapshotter: Callable[[], dict[str, int]],
) -> dict[str, Any]:
    blockers: list[str] = []
    resources = _resource_status(db_path, output_root)
    blockers.extend(resources["blockers"])

    activation = load_target_activation(activation_path)
    blockers.extend(_validate_activation(activation))
    packet = load_bounded_json(packet_path)
    if not isinstance(packet, Mapping):
        raise RuntimeError("artifact_packet_mapping_required")
    packet_sha = validate_packet_sha256(packet, required=True)
    blockers.extend(_validate_boundary_tree(packet, path="$.artifact_packet"))
    manifest = load_bounded_json(market_manifest_path)
    if not isinstance(manifest, Mapping):
        raise RuntimeError("market_manifest_mapping_required")
    blockers.extend(_validate_market_manifest(manifest))
    blockers.extend(
        _validate_vertical_slice_scope(
            station_id=station_id,
            packet=packet,
            manifest=manifest,
        )
    )
    blockers.extend(_validate_boundary_tree(manifest, path="$.market_manifest"))

    with _open_read_only(db_path) as conn:
        schema_status = production_ssot_schema_status(conn)
        if (
            schema_status.get("ok") is not True
            or schema_status.get("ledger_schema_ok") is not True
        ):
            blockers.append("target_production_ssot_schema_not_installed")
            bindings = {key: -1 for key in PACKET_BINDING_KEYS}
        else:
            bindings = packet_binding_counts(conn, packet)
            expected = {
                key: len(packet.get(key) or [])
                for key in PACKET_BINDING_KEYS
            }
            for key in PACKET_BINDING_KEYS:
                if bindings.get(key) != expected[key]:
                    blockers.append(f"artifact_binding_incomplete:{key}")
        counts = _table_counts(conn)
    expected = {
        key: len(packet.get(key) or [])
        for key in PACKET_BINDING_KEYS
    }
    processes = process_snapshotter()
    return {
        "state": "PASS" if not blockers else "BLOCKED_FAIL_CLOSED",
        "blockers": sorted(set(blockers)),
        "vertical_slice_station_id": station_id,
        "resources": resources,
        "schema_status": schema_status,
        "artifact_packet": {
            "path": str(packet_path),
            "sha256": packet_sha,
            "expected_counts": expected,
            "binding_counts": bindings,
        },
        "market_manifest": {
            "path": str(market_manifest_path),
            "sha256": _sha256_file(market_manifest_path),
            "event_date": (
                manifest.get("event_date")
                or manifest.get("target_local_date")
            ),
            "station_scope": manifest.get("station_scope"),
        },
        "activation": {
            "path": str(activation_path),
            "sha256": _sha256_file(activation_path),
            "execution_mode": activation.get("execution_mode"),
        },
        "protected_processes": processes,
        "table_counts": counts,
    }


def _child_file_limit() -> None:
    # RLIMIT_FSIZE applies to every child write, including SQLite itself.
    # The old 16 MiB output limit therefore turned a valid >16 MiB target DB
    # transaction into ``disk I/O error``. Keep the process globally bounded
    # by the independently governed DB+WAL ceiling; stdout/stderr are checked
    # against MAX_OUTPUT_BYTES immediately after the child exits.
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (MAX_CHILD_FILE_BYTES, MAX_CHILD_FILE_BYTES),
    )


def _default_worker_runner(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
) -> int:
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        result = subprocess.run(
            list(command),
            cwd=ROOT,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout_seconds,
            check=False,
            preexec_fn=_child_file_limit,
        )
    return int(result.returncode)


def run(
    *,
    station_id: str = VERTICAL_SLICE_STATION_ID,
    db_path: Path = DEFAULT_TARGET_DB,
    canonical_db_path: Path = DEFAULT_CANONICAL_DB,
    activation_path: Path = DEFAULT_ACTIVATION,
    packet_path: Path = DEFAULT_PACKET,
    market_manifest_path: Path = DEFAULT_MARKET_MANIFEST,
    status_path: Path = DEFAULT_STATUS,
    receipt_path: Path = DEFAULT_RECEIPT,
    proxy_url: str = "http://127.0.0.1:7897",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    execute: bool = True,
    reuse_materialized_probability: bool = False,
    process_snapshotter: Callable[[], dict[str, int]] = _default_process_snapshot,
    worker_runner: Callable[..., int] = _default_worker_runner,
) -> dict[str, Any]:
    started_at = _now()
    station = str(station_id or "").strip().upper()
    db = _bounded_file(
        resolve_target_database(db_path),
        label="target_db",
        limit=MAX_TARGET_DB_BYTES,
    )
    canonical_db = _bounded_file(
        canonical_db_path,
        label="canonical_db",
        limit=32 * 1024 * 1024 * 1024,
    )
    activation = _bounded_file(activation_path, label="activation")
    packet = _bounded_file(packet_path, label="artifact_packet")
    manifest = _bounded_file(market_manifest_path, label="market_manifest")
    status = status_path.expanduser().resolve()
    receipt = receipt_path.expanduser().resolve()
    status.parent.mkdir(parents=True, exist_ok=True)
    receipt.parent.mkdir(parents=True, exist_ok=True)

    preflight = _build_preflight(
        station_id=station,
        db_path=db,
        activation_path=activation,
        packet_path=packet,
        market_manifest_path=manifest,
        output_root=receipt.parent,
        process_snapshotter=process_snapshotter,
    )
    db_before = _db_fingerprint(db)
    if preflight["state"] != "PASS":
        result = {
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "stage": "preflight",
            "station_id": station,
            "started_at_utc": started_at,
            "completed_at_utc": _now(),
            "preflight": preflight,
            "worker_invoked": False,
            "execution_mode": EXECUTION_MODE,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
            "target_db_fingerprint_before": db_before,
            "target_db_fingerprint_after": _db_fingerprint(db),
        }
        result["receipt_sha256"] = content_sha256(result)
        write_bounded_json(receipt, result, limit=MAX_OUTPUT_BYTES)
        return result
    if not execute:
        result = {
            "schema": SCHEMA,
            "state": "PREFLIGHT_READY",
            "stage": "preflight",
            "station_id": station,
            "started_at_utc": started_at,
            "completed_at_utc": _now(),
            "preflight": preflight,
            "worker_invoked": False,
            "execution_mode": EXECUTION_MODE,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
            "target_db_fingerprint_before": db_before,
            "target_db_fingerprint_after": _db_fingerprint(db),
        }
        result["receipt_sha256"] = content_sha256(result)
        write_bounded_json(receipt, result, limit=MAX_OUTPUT_BYTES)
        return result

    manifest_payload = load_bounded_json(manifest)
    if not isinstance(manifest_payload, Mapping):
        raise RuntimeError("market_manifest_mapping_required")
    event_date = str(
        manifest_payload.get("event_date")
        or manifest_payload.get("target_local_date")
        or ""
    ).strip()
    if not event_date:
        raise RuntimeError("market_manifest_event_date_missing")
    env = dict(os.environ)
    env.update(
        {
            "PM_TARGET_PRODUCTION_WRITER": "1",
            "PM_TARGET_EXECUTION_MODE": EXECUTION_MODE,
            "PM_TARGET_ACTIVATION": str(activation),
            "PM_TARGET_MARKET_MANIFEST": str(manifest),
            "PM_TARGET_STATION_ALLOWLIST": station,
            "PM_TARGET_EVENT_DATE_ALLOWLIST": event_date,
            "PM_OFFICIAL_NWP_PROXY": proxy_url,
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
        }
    )
    if reuse_materialized_probability:
        env["PM_TARGET_SKIP_NWP_REFRESH"] = "1"
    command = (
        sys.executable,
        str(WORKER_ENTRYPOINT),
        "--db",
        str(canonical_db),
        "--status",
        str(status),
        "--once",
        "production",
    )
    stdout_fd, stdout_name = tempfile.mkstemp(
        prefix=".post-migration-worker.", suffix=".stdout", dir=receipt.parent
    )
    stderr_fd, stderr_name = tempfile.mkstemp(
        prefix=".post-migration-worker.", suffix=".stderr", dir=receipt.parent
    )
    os.close(stdout_fd)
    os.close(stderr_fd)
    stdout_path = Path(stdout_name)
    stderr_path = Path(stderr_name)
    try:
        returncode = worker_runner(
            command,
            env=env,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=timeout_seconds,
        )
        for child_output in (stdout_path, stderr_path):
            if int(child_output.stat().st_size) > MAX_OUTPUT_BYTES:
                raise RuntimeError(
                    "post_migration_worker_output_bytes_exceeded"
                )
        worker_status = load_bounded_json(status)
        if not isinstance(worker_status, Mapping):
            raise RuntimeError("worker_status_mapping_required")
        boundary_blockers = _validate_boundary_tree(
            worker_status,
            path="$.worker_status",
        )
        processes_after = process_snapshotter()
        if processes_after != preflight["protected_processes"]:
            boundary_blockers.append("protected_process_pid_changed")
        with _open_read_only(db) as conn:
            counts_after = _table_counts(conn)
        stderr_excerpt = stderr_path.read_text(
            encoding="utf-8", errors="replace"
        )[-4096:]
        worker_production = worker_status.get("production")
        worker_ok = (
            returncode == 0
            and isinstance(worker_production, Mapping)
            and worker_production.get("execution_mode") == EXECUTION_MODE
            and not boundary_blockers
        )
        result = {
            "schema": SCHEMA,
            "state": (
                "COMPLETED_PRODUCTION_NO_LIVE"
                if worker_ok
                else "BLOCKED_FAIL_CLOSED"
            ),
            "stage": "worker_complete",
            "station_id": station,
            "started_at_utc": started_at,
            "completed_at_utc": _now(),
            "preflight": preflight,
            "worker_invoked": True,
            "worker_returncode": returncode,
            "worker_status_path": str(status),
            "worker_status_sha256": _sha256_file(status),
            "worker_state": (
                worker_production.get("state")
                if isinstance(worker_production, Mapping)
                else "missing"
            ),
            "boundary_blockers": sorted(set(boundary_blockers)),
            "stderr_excerpt": stderr_excerpt,
            "protected_processes_after": processes_after,
            "table_counts_before": preflight["table_counts"],
            "table_counts_after": counts_after,
            "execution_mode": EXECUTION_MODE,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
            "target_db_fingerprint_before": db_before,
            "target_db_fingerprint_after": _db_fingerprint(db),
        }
        result["receipt_sha256"] = content_sha256(result)
        write_bounded_json(receipt, result, limit=MAX_OUTPUT_BYTES)
        return result
    finally:
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station-id", default=VERTICAL_SLICE_STATION_ID)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--canonical-db", type=Path, default=DEFAULT_CANONICAL_DB)
    parser.add_argument("--activation", type=Path, default=DEFAULT_ACTIVATION)
    parser.add_argument("--artifact-packet", type=Path, default=DEFAULT_PACKET)
    parser.add_argument(
        "--market-manifest", type=Path, default=DEFAULT_MARKET_MANIFEST
    )
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--proxy-url", default="http://127.0.0.1:7897")
    parser.add_argument(
        "--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS
    )
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--reuse-materialized-probability",
        action="store_true",
        help=(
            "skip network NWP refresh for an immediate second pass after "
            "a fresh exact-scope P0/P1 materialization"
        ),
    )
    args = parser.parse_args()
    try:
        result = run(
            station_id=args.station_id,
            db_path=args.db,
            canonical_db_path=args.canonical_db,
            activation_path=args.activation,
            packet_path=args.artifact_packet,
            market_manifest_path=args.market_manifest,
            status_path=args.status,
            receipt_path=args.receipt,
            proxy_url=args.proxy_url,
            timeout_seconds=args.timeout_seconds,
            execute=not args.preflight_only,
            reuse_materialized_probability=(
                args.reuse_materialized_probability
            ),
        )
    except Exception as exc:
        result = {
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "stage": "coordinator_exception",
            "reason": f"{type(exc).__name__}:{exc}",
            "execution_mode": EXECUTION_MODE,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
        result["receipt_sha256"] = content_sha256(result)
        write_bounded_json(
            args.receipt.expanduser().resolve(),
            result,
            limit=MAX_OUTPUT_BYTES,
        )
    print(
        json.dumps(
            {
                key: result.get(key)
                for key in (
                    "schema",
                    "state",
                    "stage",
                    "worker_invoked",
                    "worker_returncode",
                    "receipt_sha256",
                )
                if key in result
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.get("state") in {
        "PREFLIGHT_READY",
        "COMPLETED_PRODUCTION_NO_LIVE",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
