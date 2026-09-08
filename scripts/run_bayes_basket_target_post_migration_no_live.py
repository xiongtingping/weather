#!/usr/bin/env python3
"""Run the sole target worker once after the controlled SSOT migration.

This coordinator does not implement probability, market, basket, decision, or
reconciliation logic.  It proves the post-migration prerequisites and then
invokes the existing ``weather_basket_yes --once production`` entrypoint,
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

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.activation import load_target_activation  # noqa: E402
from bayes_basket_target.atomic_cutover_receipt import (  # noqa: E402
    HISTORICAL_OOS_BOUNDARY as ATOMIC_HISTORICAL_OOS_BOUNDARY,
    build_release_identity,
    seal_stage,
)
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.native_ensemble_authority import (  # noqa: E402
    DEFAULT_NATIVE_OOS_SNAPSHOT_MANIFEST,
    NativeOosSnapshot,
    load_native_oos_snapshot_manifest,
    pointer_activation_mismatch_reasons,
    pointer_for_scope,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_PROCESS_RSS_BYTES,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.ssot import (  # noqa: E402
    native_ensemble_foundation_schema_status,
    production_ssot_schema_status,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)
SCHEMA = "bayes_basket_target_post_migration_no_live_run_v1"
EXECUTION_MODE = "production_no_live"
CANONICAL_STATION_IDS = tuple(
    coordinate_registry_manifest()["station_ids"]
)
MAX_STATION_SCOPE = expected_station_count()
RESIDENT_ACCEPTANCE_SCHEMA = (
    "bayes_basket_target_probability_resident_two_round_acceptance_v2"
)
DEFAULT_CANONICAL_DB = ROOT / "data/archive.db"
DEFAULT_ACTIVATION = (
    ROOT
    / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
)
DEFAULT_MARKET_MANIFEST = (
    ROOT / "data/bayes_basket_target_official_ws_manifest.json"
)
DEFAULT_NATIVE_OOS_SNAPSHOT = DEFAULT_NATIVE_OOS_SNAPSHOT_MANIFEST
DEFAULT_NATIVE_OOS_SNAPSHOT_MANIFEST = DEFAULT_NATIVE_OOS_SNAPSHOT
DEFAULT_STATUS = (
    ROOT / "output/audits/bayes_basket_target_post_migration_worker_status.json"
)
DEFAULT_RECEIPT = (
    ROOT / "output/audits/bayes_basket_target_post_migration_no_live_run.json"
)
WORKER_ENTRYPOINT = ROOT / "scripts/weather_basket_yes_daemon.py"

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_TARGET_DB_BYTES = 4096 * 1024 * 1024
MAX_TARGET_WAL_BYTES = 512 * 1024 * 1024
MAX_CHILD_FILE_BYTES = MAX_TARGET_DB_BYTES + MAX_TARGET_WAL_BYTES
MAX_RSS_BYTES = MAX_TARGET_PROCESS_RSS_BYTES
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 600
# A timed-out no-live child must become an explicit fail-closed receipt rather
# than escaping as a coordinator exception.  The value is deliberately a
# non-zero shell-style sentinel and is never interpreted as a successful run.
WORKER_TIMEOUT_RETURN_CODE = 124
HISTORICAL_OOS_BOUNDARY = dict(ATOMIC_HISTORICAL_OOS_BOUNDARY)
# Import-compatible alias for older callers; the value is historical-only and
# explicitly says future data is neither OOS nor a gate.
FORWARD_OOS_BOUNDARY = HISTORICAL_OOS_BOUNDARY

PROTECTED_PROCESS_PATTERNS = {
    "weather_basket_yes": "scripts/weather_basket_yes_daemon.py",
    "market_ws": "scripts/market_ws_listener.py",
    "clob_order_service": "scripts/clob_order_service.mjs",
}
RESIDENT_CORE_SERVICE_LABELS = frozenset(
    {
        "com.weather.weather_basket_yes",
        "com.weather.market_ws",
        "com.weather.clob_order_service",
    }
)
NATIVE_P0_PATHWAY = "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE"
NATIVE_STRICT_OOS_PROFILE = "FORMAL_PROMOTION_20_TRAIN_8_HOLDOUT"
NATIVE_MIN_TRAIN_DAYS = 20
NATIVE_MIN_HOLDOUT_DAYS = 8
NATIVE_AUTHORITY_SCOPES = frozenset({
    "PRODUCTION_NO_LIVE_ONLY",
    "FORMAL_PROMOTED",
})
OFFICIAL_NATIVE_TRANSPORTS = frozenset({
    "NOAA_GEFS_PDS_S3",
    "ECMWF_OPEN_DATA_S3",
    "ECCC_DD_WEATHER",
})
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
FALSE_BOUNDARY_KEYS.update({"legacy_fallback_used", "old_fallback_used"})


def normalize_station_scope(
    station_ids: Sequence[str] | None = None,
) -> tuple[str, ...]:
    canonical = tuple(CANONICAL_STATION_IDS)
    if len(canonical) != MAX_STATION_SCOPE or len(set(canonical)) != len(
        canonical
    ):
        raise RuntimeError("canonical_station_registry_invalid")
    raw = canonical if station_ids is None else tuple(station_ids)
    normalized = tuple(
        sorted(
            {
                str(station_id or "").strip().upper()
                for station_id in raw
                if str(station_id or "").strip()
            }
        )
    )
    if not normalized:
        raise RuntimeError("post_migration_station_scope_empty")
    if len(normalized) > MAX_STATION_SCOPE:
        raise RuntimeError("post_migration_station_scope_exceeds_limit")
    if len(normalized) != len(raw):
        raise RuntimeError("post_migration_station_scope_duplicate_or_empty")
    unknown = sorted(set(normalized) - set(canonical))
    if unknown:
        raise RuntimeError(
            "post_migration_station_scope_not_canonical:"
            + ",".join(unknown)
        )
    return normalized


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
    station_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Read bounded in-scope/outside-scope counts for probability tables.

    This is deliberately independent of the resident acceptance helper.  It
    gives the cutover a complete before/after witness, including bridge
    tables that the legacy two-round helper does not inspect.
    """

    scope = normalize_station_scope(station_ids)
    placeholders = ",".join("?" for _ in scope)
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
                    "scoped": -1,
                    "outside_scope": -1,
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
                        "scoped": -1,
                        "outside_scope": -1,
                    }
                    continue
                target = int(conn.execute(
                    "SELECT COUNT(*) FROM "
                    "basket_probability_bridge_bucket_probability b "
                    "JOIN basket_probability_bridge_history h "
                    "ON h.history_id=b.history_id "
                    f"WHERE UPPER(COALESCE(h.station_id,'')) IN ({placeholders})",
                    scope,
                ).fetchone()[0])
                outside_scope = int(conn.execute(
                    "SELECT COUNT(*) FROM "
                    "basket_probability_bridge_bucket_probability b "
                    "JOIN basket_probability_bridge_history h "
                    "ON h.history_id=b.history_id "
                    "WHERE UPPER(COALESCE(h.station_id,'')) NOT IN "
                    f"({placeholders})",
                    scope,
                ).fetchone()[0])
                tables[table] = {
                    "state": "READY",
                    "scoped": target,
                    "outside_scope": outside_scope,
                }
                continue
            if table == "basket_probability_evidence_promotion":
                if (
                    "artifact_id" not in columns
                    or "basket_probability_evidence_artifact"
                    not in present
                ):
                    tables[table] = {
                        "state": "STATION_JOIN_MISSING",
                        "scoped": -1,
                        "outside_scope": -1,
                    }
                    continue
                target = int(conn.execute(
                    "SELECT COUNT(*) FROM "
                    "basket_probability_evidence_promotion p "
                    "JOIN basket_probability_evidence_artifact a "
                    "ON a.artifact_id=p.artifact_id "
                    f"WHERE UPPER(COALESCE(a.station_id,'')) IN ({placeholders})",
                    scope,
                ).fetchone()[0])
                outside_scope = int(conn.execute(
                    "SELECT COUNT(*) FROM "
                    "basket_probability_evidence_promotion p "
                    "JOIN basket_probability_evidence_artifact a "
                    "ON a.artifact_id=p.artifact_id "
                    "WHERE UPPER(COALESCE(a.station_id,'')) NOT IN "
                    f"({placeholders})",
                    scope,
                ).fetchone()[0])
                tables[table] = {
                    "state": "READY",
                    "scoped": target,
                    "outside_scope": outside_scope,
                }
                continue
            if "station_id" not in columns:
                tables[table] = {
                    "state": "STATION_COLUMN_MISSING",
                    "scoped": -1,
                    "outside_scope": -1,
                }
                continue
            target = int(conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE UPPER(COALESCE(station_id,'')) IN "
                f"({placeholders})",
                scope,
            ).fetchone()[0])
            outside_scope = int(conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE UPPER(COALESCE(station_id,'')) NOT IN "
                f"({placeholders})",
                scope,
            ).fetchone()[0])
            tables[table] = {
                "state": "READY",
                "scoped": target,
                "outside_scope": outside_scope,
            }
    return {
        "schema": "bayes_basket_target_probability_scope_snapshot_v1",
        "station_scope": {
            "mode": "allowlist",
            "station_ids": list(scope),
            "canonical_default": station_ids is None,
            "max_station_count": MAX_STATION_SCOPE,
        },
        "database": str(database),
        "tables": tables,
        "missing_tables": sorted(
            table for table, value in tables.items()
            if value.get("state") != "READY"
        ),
        "outside_scope_counts": {
            table: int(value["outside_scope"])
            for table, value in tables.items()
        },
        "scoped_counts": {
            table: int(value["scoped"])
            for table, value in tables.items()
        },
    }


def verify_two_round_resident_receipts(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    before_snapshot: Mapping[str, Any] | None = None,
    after_snapshot: Mapping[str, Any] | None = None,
    station_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Verify bounded canonical/allowlisted two-round resident acceptance.

    Forward observations are intentionally not read here.  They are diagnostic
    only: never OOS, never a production_no_live gate, and never a
    formal-promotion input.  Historical strict-as-of evidence owns promotion.
    """

    scope = normalize_station_scope(station_ids)
    blockers: list[str] = []
    rounds = (first, second)
    for index, receipt in enumerate(rounds, start=1):
        if receipt.get("state") != "PASS":
            blockers.append(f"resident_round_{index}_not_pass")
        receipt_scope = tuple(
            (receipt.get("station_scope") or {}).get("station_ids") or ()
        )
        if receipt_scope != scope:
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
        if receipt.get("started_station_ids"):
            if int(fallback.get("P0_HISTORICAL_TILT") or 0) < 1:
                blockers.append(
                    f"resident_round_{index}_theta_zero_not_consumed"
                )
            if int(fallback.get("METAR_TRAJECTORY_UPDATE") or 0) < 1:
                blockers.append(
                    f"resident_round_{index}_identity_lr_not_consumed"
                )
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

    first_station_results = first.get("station_results") or {}
    second_station_results = second.get("station_results") or {}
    started_both = sorted(
        set(first.get("started_station_ids") or [])
        & set(second.get("started_station_ids") or [])
    )
    if not started_both:
        blockers.append("resident_no_canonical_station_started_both_rounds")
    for station in started_both:
        first_core = (
            (first_station_results.get(station) or {}).get("core_chain") or {}
        ).get("latest") or {}
        second_core = (
            (second_station_results.get(station) or {}).get("core_chain") or {}
        ).get("latest") or {}
        if not first_core or not second_core:
            blockers.append(
                f"resident_core_direction_latest_missing:{station}"
            )
            continue
        first_rowid = int(first_core.get("rowid") or 0)
        second_rowid = int(second_core.get("rowid") or 0)
        if second_rowid < first_rowid:
            blockers.append(
                f"resident_core_direction_rowid_rewind:{station}"
            )
        elif second_rowid == first_rowid:
            # An unchanged poll must be zero-write.  Two identical durable
            # snapshots are a valid second round; requiring a fresh row here
            # would contradict the resident deduplication contract.
            if dict(second_core) != dict(first_core):
                blockers.append(
                    f"resident_core_direction_same_row_drift:{station}"
                )
        else:
            if (
                second_core.get("current_core_buckets")
                != first_core.get("core_buckets")
            ):
                blockers.append(
                    f"resident_core_predecessor_mismatch:{station}"
                )
            if (
                second_core.get("current_direction")
                != first_core.get("direction")
            ):
                blockers.append(
                    f"resident_direction_predecessor_mismatch:{station}"
                )

    if before_snapshot is None or after_snapshot is None:
        blockers.append("probability_scope_before_after_snapshot_missing")
    else:
        before_missing = before_snapshot.get("missing_tables") or []
        after_missing = after_snapshot.get("missing_tables") or []
        if before_missing or after_missing:
            blockers.append("probability_scope_table_missing")
        before_scope = tuple(
            (before_snapshot.get("station_scope") or {}).get("station_ids")
            or ()
        )
        after_scope = tuple(
            (after_snapshot.get("station_scope") or {}).get("station_ids")
            or ()
        )
        if before_scope != scope or after_scope != scope:
            blockers.append("probability_scope_snapshot_mismatch")
        before_counts = before_snapshot.get("outside_scope_counts") or {}
        after_counts = after_snapshot.get("outside_scope_counts") or {}
        if before_counts != after_counts:
            blockers.append("outside_scope_probability_table_write_detected")

    identity = {
        "schema": RESIDENT_ACCEPTANCE_SCHEMA,
        "station_scope": {
            "mode": "allowlist",
            "station_ids": list(scope),
            "max_station_count": MAX_STATION_SCOPE,
        },
        "started_station_ids": second.get("started_station_ids") or [],
        "not_started_station_ids": (
            second.get("not_started_station_ids") or []
        ),
        "round_one_sha256": first.get("round_sha256"),
        "round_two_sha256": second.get("round_sha256"),
        "services": second_services,
        "outside_scope_counts_before": (
            dict((before_snapshot or {}).get("outside_scope_counts") or {})
        ),
        "outside_scope_counts_after": (
            dict((after_snapshot or {}).get("outside_scope_counts") or {})
        ),
        "blockers": sorted(set(blockers)),
        "historical_oos_boundary": dict(HISTORICAL_OOS_BOUNDARY),
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


def verify_migration_two_round_resident_receipts(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    before_snapshot: Mapping[str, Any] | None = None,
    after_snapshot: Mapping[str, Any] | None = None,
    station_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Bind M9-06 resident continuity without claiming M9-08 is done."""

    from scripts.accept_bayes_basket_target_probability_resident_two_rounds import (
        compare_migration_rounds,
    )

    scope = normalize_station_scope(station_ids)
    compared = compare_migration_rounds(first, second)
    blockers = list(compared.get("blockers") or [])
    for index, receipt in enumerate((first, second), start=1):
        resources = receipt.get("resources") or {}
        resident_rss = resources.get("resident_service_rss_bytes")
        if not isinstance(resident_rss, Mapping) or not resident_rss:
            blockers.append(
                f"migration_round_{index}_rss_snapshot_missing"
            )
        elif any(
            int(value or 0) > MAX_RSS_BYTES
            for value in resident_rss.values()
        ):
            blockers.append(
                f"migration_round_{index}_rss_gate_invalid"
            )
        if int(
            resources.get("acceptance_process_rss_bytes") or 0
        ) > MAX_RSS_BYTES:
            blockers.append(
                f"migration_round_{index}_acceptance_rss_gate_invalid"
            )
    services = second.get("services") or {}
    if set(services) != set(RESIDENT_CORE_SERVICE_LABELS):
        blockers.append("migration_three_service_set_invalid")
    if any(
        not isinstance(value, int) or value <= 0
        for value in services.values()
    ):
        blockers.append("migration_three_service_pid_invalid")
    if before_snapshot is None or after_snapshot is None:
        blockers.append(
            "migration_probability_scope_snapshots_missing"
        )
    else:
        before_scope = tuple(
            (before_snapshot.get("station_scope") or {}).get(
                "station_ids"
            ) or ()
        )
        after_scope = tuple(
            (after_snapshot.get("station_scope") or {}).get(
                "station_ids"
            ) or ()
        )
        if before_scope != scope or after_scope != scope:
            blockers.append(
                "migration_probability_scope_snapshot_mismatch"
            )
        if (
            before_snapshot.get("missing_tables")
            or after_snapshot.get("missing_tables")
        ):
            blockers.append(
                "migration_probability_scope_table_missing"
            )
        if (
            before_snapshot.get("outside_scope_counts")
            != after_snapshot.get("outside_scope_counts")
        ):
            blockers.append(
                "migration_outside_scope_probability_write_detected"
            )
    identity = {
        "schema": (
            "bayes_basket_target_probability_migration_resident_verified_v1"
        ),
        "station_scope": {
            "mode": "allowlist",
            "station_ids": list(scope),
            "max_station_count": MAX_STATION_SCOPE,
        },
        "started_station_ids": second.get("started_station_ids") or [],
        "candidate_accumulation_station_ids": second.get(
            "candidate_accumulation_station_ids"
        ) or [],
        "not_started_station_ids": second.get(
            "not_started_station_ids"
        ) or [],
        "round_one_sha256": first.get("round_sha256"),
        "round_two_sha256": second.get("round_sha256"),
        "services": services,
        "materialization_pending_blockers": compared.get(
            "materialization_pending_blockers"
        ) or [],
        "outside_scope_counts_before": dict(
            (before_snapshot or {}).get("outside_scope_counts") or {}
        ),
        "outside_scope_counts_after": dict(
            (after_snapshot or {}).get("outside_scope_counts") or {}
        ),
        "blockers": sorted(set(blockers)),
        "historical_oos_boundary": dict(HISTORICAL_OOS_BOUNDARY),
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


def _atomic_stage(stage: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Attach one M9-06 content-addressed stage without changing payload semantics."""

    return seal_stage(stage, payload, state=str(payload.get("state") or "OBSERVED"))


def _release_identity(
    *,
    station_ids: Sequence[str],
    activation_path: Path,
    native_authority_sha256: str,
    schema_status: Mapping[str, Any],
) -> dict[str, Any]:
    return build_release_identity(
        station_ids=station_ids,
        schema_sha256=content_sha256(dict(schema_status)),
        activation_sha256=_sha256_file(activation_path),
        artifact_sha256=native_authority_sha256,
        artifact_role="native_ensemble_ssot_authority",
    )


def _finalize_runner_result(
    result: dict[str, Any],
    *,
    stage: str,
) -> dict[str, Any]:
    result["atomic_stage_receipt"] = _atomic_stage(stage, result)
    result["receipt_sha256"] = content_sha256(result)
    return result


def _validate_market_manifest(payload: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if payload.get("evidence_only") is not True:
        blockers.append("market_manifest_not_evidence_only")
    if payload.get("authorizes_order") is not False:
        blockers.append("market_manifest_authorizes_order_invalid")
    stations, event_dates = _manifest_scope(payload)
    if not stations:
        blockers.append("market_manifest_exact_station_allowlist_missing")
    if not event_dates:
        blockers.append("market_manifest_event_date_missing")
    return blockers


def _manifest_scope(
    manifest: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    scopes: list[Mapping[str, Any]] = []
    top_scope = manifest.get("station_scope")
    if isinstance(top_scope, Mapping):
        scopes.append(manifest)
    nested = manifest.get("scope_manifests")
    if isinstance(nested, Mapping):
        scopes.extend(
            value for value in nested.values()
            if isinstance(value, Mapping)
        )
    elif isinstance(nested, list):
        scopes.extend(
            value for value in nested if isinstance(value, Mapping)
        )
    stations: set[str] = set()
    event_dates: set[str] = set()
    for item in scopes:
        scope = item.get("station_scope")
        if not isinstance(scope, Mapping) or scope.get("mode") != "allowlist":
            continue
        stations.update(
            str(value or "").strip().upper()
            for value in scope.get("station_ids") or []
            if str(value or "").strip()
        )
        event_date = str(
            item.get("event_date")
            or item.get("target_local_date")
            or ""
        ).strip()
        if event_date:
            event_dates.add(event_date)
    return sorted(stations), sorted(event_dates)


def _validate_manifest_station_scope(
    *,
    station_ids: Sequence[str],
    manifest: Mapping[str, Any],
) -> list[str]:
    """Require official WS evidence to remain inside the frozen scope."""

    expected = set(normalize_station_scope(station_ids))
    manifest_stations, _ = _manifest_scope(manifest)
    if not manifest_stations or not set(manifest_stations).issubset(expected):
        return ["market_manifest_outside_station_scope"]
    return []


def _validate_exact_station_scope(
    *,
    station_ids: Sequence[str],
    packet: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> list[str]:
    """Validate a bounded legacy-shaped scope fixture.

    The production coordinator no longer consumes an artifact packet, but
    retaining this pure validator keeps old audit fixtures useful without
    reintroducing packet binding into the runtime path.
    """

    expected = list(normalize_station_scope(station_ids))
    blockers: list[str] = []
    packet_stations = sorted({
        str(value or "").strip().upper()
        for value in packet.get("station_ids") or []
        if str(value or "").strip()
    })
    if packet_stations != expected:
        blockers.append("artifact_packet_not_exact_station_scope")
    for key, rows in packet.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping) or "station_id" not in row:
                continue
            row_station = str(row.get("station_id") or "").strip().upper()
            if row_station and row_station not in expected:
                blockers.append(f"artifact_packet_outside_station_scope:{key}")
                break
    manifest_stations, _ = _manifest_scope(manifest)
    if not manifest_stations or not set(manifest_stations).issubset(set(expected)):
        blockers.append("market_manifest_outside_station_scope")
    return blockers


def _parse_utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone_missing")
    return parsed.astimezone(timezone.utc)


def _sha256(value: Any) -> bool:
    text = str(value or "").lower()
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


def _native_json_list(value: Any) -> list[str]:
    raw = json.loads(str(value))
    if not isinstance(raw, list):
        raise ValueError("list_required")
    normalized = [str(item).strip() for item in raw]
    if not normalized or any(not item for item in normalized):
        raise ValueError("list_members_invalid")
    if len(normalized) != len(set(normalized)):
        raise ValueError("list_members_duplicate")
    return normalized


def _native_json_mapping(value: Any) -> dict[str, str]:
    raw = json.loads(str(value))
    if not isinstance(raw, Mapping):
        raise ValueError("mapping_required")
    normalized = {
        str(key).strip(): str(item).strip()
        for key, item in raw.items()
    }
    if not normalized or any(
        not key or not item for key, item in normalized.items()
    ):
        raise ValueError("mapping_members_invalid")
    return normalized


def _native_authority_failure(
    *,
    code: str,
    details: list[str],
) -> None:
    if code not in details:
        details.append(code)


def native_ensemble_authority_snapshot(
    conn: sqlite3.Connection,
    *,
    station_ids: Sequence[str],
    as_of_utc: str,
    native_oos_snapshot: NativeOosSnapshot | None = None,
    native_oos_snapshot_error: str | None = None,
) -> dict[str, Any]:
    """Read the only admissible P0 calibration authority from SSOT.

    A legacy deterministic artifact packet intentionally has no role here.
    The worker consumes only calibrated native-member cross ensembles, so the
    coordinator validates that same chain: strict-OOS selection evidence,
    cross artifact, exact cross activation, every selected native system
    calibration, and its eight or more actual OOS predictions.
    """

    scope = normalize_station_scope(station_ids)
    status = native_ensemble_foundation_schema_status(conn)
    identity: dict[str, Any] = {
        "schema": "native_ensemble_preflight_authority_v1",
        "as_of_utc": as_of_utc,
        "station_scope": {
            "mode": "allowlist",
            "station_ids": list(scope),
        },
        "threshold_profile": NATIVE_STRICT_OOS_PROFILE,
        "required_train_days": NATIVE_MIN_TRAIN_DAYS,
        "required_holdout_days": NATIVE_MIN_HOLDOUT_DAYS,
        "probability_pathway": NATIVE_P0_PATHWAY,
        "accepted_authority_scopes": sorted(NATIVE_AUTHORITY_SCOPES),
        "official_native_transports": sorted(OFFICIAL_NATIVE_TRANSPORTS),
        "native_schema_status": status,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    if native_oos_snapshot is not None:
        identity["native_oos_snapshot"] = {
            "schema": str(native_oos_snapshot.payload.get("schema") or ""),
            "path": (
                str(native_oos_snapshot.path)
                if native_oos_snapshot.path is not None
                else None
            ),
            "content_sha256": native_oos_snapshot.content_sha256,
            "station_ids": list(native_oos_snapshot.station_ids),
        }
    if native_oos_snapshot_error:
        identity.update({
            "state": "BLOCKED_FAIL_CLOSED",
            "blockers": [
                "native_oos_snapshot_manifest_invalid:"
                + str(native_oos_snapshot_error),
            ],
            "ready_station_ids": [],
            "unready_station_ids": list(scope),
            "station_authority": {},
        })
        return {**identity, "content_sha256": content_sha256(identity)}
    if status.get("ok") is not True:
        identity.update({
            "state": "BLOCKED_FAIL_CLOSED",
            "blockers": ["native_ensemble_foundation_schema_not_installed"],
            "ready_station_ids": [],
            "unready_station_ids": list(scope),
            "station_authority": {},
        })
        return {**identity, "content_sha256": content_sha256(identity)}

    try:
        now = _parse_utc(as_of_utc)
    except (TypeError, ValueError):
        identity.update({
            "state": "BLOCKED_FAIL_CLOSED",
            "blockers": ["native_ensemble_authority_as_of_invalid"],
            "ready_station_ids": [],
            "unready_station_ids": list(scope),
            "station_authority": {},
        })
        return {**identity, "content_sha256": content_sha256(identity)}

    placeholders = ",".join("?" for _ in scope)
    rows = conn.execute(
        "SELECT "
        "a.activation_id,a.artifact_id,a.station_id,a.season_bin,"
        "a.lead_bucket,a.run_cycle,a.run_cycle_by_system_json,"
        "a.run_cycle_by_system_sha256,a.settlement_definition,"
        "a.available_system_set_hash_or_star,a.generation,"
        "a.valid_from_utc,a.valid_until_utc,a.authority_scope,"
        "a.content_sha256,"
        "c.ensemble_system_ids_json,c.ensemble_system_count,"
        "c.independent_family_count,c.selection_evidence_sha256,"
        "c.probability_pathway,c.valid_from_utc,c.valid_until_utc,"
        "c.run_cycle_by_system_json,c.run_cycle_by_system_sha256,"
        "c.content_sha256,"
        "s.selected_system_ids_json,s.selected_system_count,"
        "s.independent_family_count,s.method_version,"
        "s.rolling_origin_fold_count,s.effective_sample_n,"
        "s.run_cycle_by_system_json,s.run_cycle_by_system_sha256,"
        "s.content_sha256 "
        "FROM calibration_native_ensemble_activation a "
        "JOIN calibration_cross_native_ensemble_artifact c "
        "ON c.cross_ensemble_artifact_id=a.artifact_id "
        "JOIN calibration_cross_native_ensemble_selection_evidence s "
        "ON s.content_sha256=c.selection_evidence_sha256 "
        "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
        f"AND UPPER(a.station_id) IN ({placeholders}) "
        "ORDER BY a.station_id,a.generation DESC,a.activation_id",
        tuple(scope),
    ).fetchall()
    observed_by_station: dict[str, list[str]] = {station: [] for station in scope}
    ready_by_station: dict[str, list[dict[str, Any]]] = {
        station: [] for station in scope
    }
    failures_by_station: dict[str, list[str]] = {
        station: [] for station in scope
    }

    for row in rows:
        station = str(row[2]).strip().upper()
        if station not in observed_by_station:
            continue
        activation_id = str(row[0])
        observed_by_station[station].append(activation_id)
        failures: list[str] = []
        if native_oos_snapshot is not None:
            # Charter: the pointer attests a SCOPE, not a station (179b8553).
            # Asking by station alone returned whichever lead happened to be
            # attested and then rejected every activation from any other,
            # which reads as "no artifact".  The row already carries the
            # scope it is being checked in -- the same three fields go to
            # ``pointer_activation_mismatch_reasons`` just below.
            binding = pointer_for_scope(
                native_oos_snapshot,
                station,
                season_bin=str(row[3]),
                lead_bucket=str(row[4]),
                run_cycle=str(row[5]),
            )
            if binding is None:
                _native_authority_failure(
                    code="native_current_pointer_binding_missing",
                    details=failures,
                )
            else:
                pointer_mismatches = pointer_activation_mismatch_reasons(
                    binding,
                    activation_id=activation_id,
                    artifact_id=str(row[1]),
                    activation_sha256=str(row[14]),
                    artifact_sha256=str(row[24]),
                    selection_evidence_sha256=str(row[33]),
                    generation=int(row[10]),
                    season_bin=str(row[3]),
                    lead_bucket=str(row[4]),
                    run_cycle=str(row[5]),
                    run_cycle_by_system_json=str(row[6]),
                    authority_scope=str(row[13]),
                )
                if pointer_mismatches:
                    _native_authority_failure(
                        code=(
                            "native_current_pointer_binding_mismatch:"
                            + ",".join(pointer_mismatches)
                        ),
                        details=failures,
                    )
        try:
            systems = _native_json_list(row[15])
            selected_systems = _native_json_list(row[25])
            activation_cycles = _native_json_mapping(row[6])
            artifact_cycles = _native_json_mapping(row[22])
            selection_cycles = _native_json_mapping(row[31])
        except (TypeError, ValueError, json.JSONDecodeError):
            _native_authority_failure(
                code="native_authority_json_invalid", details=failures
            )
            systems = []
            selected_systems = []
            activation_cycles = {}
            artifact_cycles = {}
            selection_cycles = {}
        if (
            not _sha256(row[7])
            or not _sha256(row[23])
            or not _sha256(row[32])
            or not _sha256(row[14])
            or not _sha256(row[24])
            or not _sha256(row[33])
        ):
            _native_authority_failure(
                code="native_authority_sha_invalid", details=failures
            )
        if str(row[13]) not in NATIVE_AUTHORITY_SCOPES:
            _native_authority_failure(
                code="native_authority_scope_invalid", details=failures
            )
        if str(row[19]) != NATIVE_P0_PATHWAY:
            _native_authority_failure(
                code="native_p0_pathway_invalid", details=failures
            )
        if (
            systems != selected_systems
            or int(row[16]) != len(systems)
            or int(row[26]) != len(selected_systems)
            or len(systems) < 2
            or str(row[9]) != content_sha256(systems)
        ):
            _native_authority_failure(
                code="native_cross_system_mask_invalid", details=failures
            )
        if (
            activation_cycles != artifact_cycles
            or activation_cycles != selection_cycles
            or set(activation_cycles) != set(systems)
            or str(row[7]) != content_sha256(activation_cycles)
            or str(row[23]) != content_sha256(artifact_cycles)
            or str(row[32]) != content_sha256(selection_cycles)
        ):
            _native_authority_failure(
                code="native_cross_cycle_mapping_invalid", details=failures
            )
        if (
            str(row[18]) != str(row[33])
            or int(row[17]) < 2
            or int(row[27]) < 2
            or int(row[17]) != int(row[27])
            or NATIVE_STRICT_OOS_PROFILE not in str(row[28])
            or int(row[29]) < NATIVE_MIN_HOLDOUT_DAYS
            or float(row[30]) < NATIVE_MIN_HOLDOUT_DAYS
        ):
            _native_authority_failure(
                code="native_strict_oos_selection_incomplete", details=failures
            )
        try:
            active = (
                _parse_utc(row[11]) <= now < _parse_utc(row[12])
                and _parse_utc(row[20]) <= now < _parse_utc(row[21])
            )
        except (TypeError, ValueError):
            active = False
        if not active:
            _native_authority_failure(
                code="native_cross_activation_not_current", details=failures
            )

        family_by_system: dict[str, str] = {}
        if systems:
            family_rows = conn.execute(
                "SELECT ensemble_system_id,independence_family_id "
                "FROM calibration_native_ensemble_system "
                "WHERE ensemble_system_id IN ("
                + ",".join("?" for _ in systems)
                + ")",
                tuple(systems),
            ).fetchall()
            family_by_system = {
                str(item[0]): str(item[1]) for item in family_rows
            }
        if (
            set(family_by_system) != set(systems)
            or len(set(family_by_system.values())) < 2
            or len(set(family_by_system.values())) != int(row[17])
        ):
            _native_authority_failure(
                code="native_independent_family_gate_failed", details=failures
            )

        prediction_rows = conn.execute(
            "SELECT event_date,trained_through_event_date,station_id,"
            "season_bin,lead_bucket,run_cycle,run_cycle_by_system_json,"
            "run_cycle_by_system_sha256,settlement_definition,"
            "prediction_oos,authority_scope,content_sha256 "
            "FROM calibration_cross_native_ensemble_oos_prediction "
            "WHERE cross_ensemble_artifact_id=? "
            "ORDER BY event_date,prediction_id LIMIT 128",
            (activation_id and str(row[1]),),
        ).fetchall()
        strict_prediction_dates: set[str] = set()
        for prediction in prediction_rows:
            try:
                prediction_cycles = _native_json_mapping(prediction[6])
            except (TypeError, ValueError, json.JSONDecodeError):
                prediction_cycles = {}
            valid_prediction = (
                str(prediction[2]).strip().upper() == station
                and str(prediction[3]) == str(row[3])
                and str(prediction[4]) == str(row[4])
                and str(prediction[5]) == str(row[5])
                and prediction_cycles == activation_cycles
                and str(prediction[7]) == content_sha256(prediction_cycles)
                and str(prediction[8]) == str(row[8])
                and int(prediction[9]) == 1
                and str(prediction[10]) in NATIVE_AUTHORITY_SCOPES
                and str(prediction[1]) < str(prediction[0])
                and _sha256(prediction[11])
            )
            if valid_prediction:
                strict_prediction_dates.add(str(prediction[0]))
        if len(strict_prediction_dates) < NATIVE_MIN_HOLDOUT_DAYS:
            _native_authority_failure(
                code="native_strict_oos_prediction_coverage_incomplete",
                details=failures,
            )

        for system_id in systems:
            expected_cycle = activation_cycles.get(system_id)
            system_rows = conn.execute(
                "SELECT sa.system_artifact_id,sa.ensemble_system_id,"
                "sa.station_id,sa.season_bin,sa.lead_bucket,sa.run_cycle,"
                "sa.reliability_parameters_json,sa.rolling_origin_fold_count,"
                "sa.effective_sample_n,sa.valid_from_utc,sa.valid_until_utc,"
                "sa.content_sha256,a.activation_id,a.run_cycle_by_system_json,"
                "a.run_cycle_by_system_sha256,a.valid_from_utc,a.valid_until_utc,"
                "a.authority_scope,a.content_sha256 "
                "FROM calibration_native_ensemble_system_artifact sa "
                "JOIN calibration_native_ensemble_activation a "
                "ON a.artifact_id=sa.system_artifact_id "
                "WHERE a.artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
                "AND a.station_id=? AND a.ensemble_system_id_or_star=? "
                "AND a.season_bin=? AND a.lead_bucket=? AND a.run_cycle=? "
                "ORDER BY a.generation DESC,a.activation_id LIMIT 16",
                (
                    station, system_id, str(row[3]), str(row[4]),
                    str(expected_cycle or ""),
                ),
            ).fetchall()
            system_ready = False
            for system_row in system_rows:
                try:
                    system_cycles = _native_json_mapping(system_row[13])
                    reliability = json.loads(str(system_row[6]))
                    system_active = (
                        _parse_utc(system_row[9]) <= now < _parse_utc(system_row[10])
                        and _parse_utc(system_row[15]) <= now < _parse_utc(system_row[16])
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(reliability, Mapping):
                    continue
                if (
                    str(system_row[1]) != system_id
                    or str(system_row[2]).strip().upper() != station
                    or str(system_row[3]) != str(row[3])
                    or str(system_row[4]) != str(row[4])
                    or str(system_row[5]) != str(expected_cycle or "")
                    or system_cycles != {system_id: str(expected_cycle or "")}
                    or str(system_row[14]) != content_sha256(system_cycles)
                    or str(system_row[17]) not in NATIVE_AUTHORITY_SCOPES
                    or not _sha256(system_row[11])
                    or not _sha256(system_row[18])
                    or not system_active
                    or reliability.get("official_native_archive") is not True
                    or str(reliability.get("transport_provider_id") or "")
                    not in OFFICIAL_NATIVE_TRANSPORTS
                    or str(reliability.get("oos_threshold_profile") or "")
                    != NATIVE_STRICT_OOS_PROFILE
                    or int(reliability.get("oos_min_train_days") or 0)
                    < NATIVE_MIN_TRAIN_DAYS
                    or int(reliability.get("oos_min_holdout_days") or 0)
                    < NATIVE_MIN_HOLDOUT_DAYS
                    or int(system_row[7]) < NATIVE_MIN_HOLDOUT_DAYS
                    or float(system_row[8]) < NATIVE_MIN_TRAIN_DAYS
                ):
                    continue
                system_ready = True
                break
            if not system_ready:
                _native_authority_failure(
                    code=f"native_system_calibration_invalid:{system_id}",
                    details=failures,
                )

        if failures:
            failures_by_station[station].extend(failures)
            continue
        ready_by_station[station].append({
            "activation_id": activation_id,
            "activation_sha256": str(row[14]),
            "cross_ensemble_artifact_id": str(row[1]),
            "cross_artifact_sha256": str(row[24]),
            "selection_evidence_sha256": str(row[33]),
            "season_bin": str(row[3]),
            "lead_bucket": str(row[4]),
            "run_cycle": str(row[5]),
            "run_cycle_by_system": activation_cycles,
            "ensemble_system_ids": systems,
            "independence_family_ids": sorted(
                set(family_by_system.values())
            ),
            "strict_oos_prediction_count": len(strict_prediction_dates),
            "generation": int(row[10]),
            "valid_until_utc": str(row[12]),
        })

    station_authority: dict[str, dict[str, Any]] = {}
    ready_stations: list[str] = []
    unready_stations: list[str] = []
    blockers: list[str] = []
    for station in scope:
        candidates = ready_by_station[station]
        if candidates:
            selected = sorted(
                candidates,
                key=lambda item: (
                    int(item["generation"]),
                    str(item["valid_until_utc"]),
                    str(item["activation_id"]),
                ),
                reverse=True,
            )[0]
            station_authority[station] = {
                "state": "STRICT_OOS_READY",
                "candidate_count": len(candidates),
                "selected": selected,
            }
            ready_stations.append(station)
            continue
        unready_stations.append(station)
        failures = sorted(set(failures_by_station[station]))[:16]
        station_authority[station] = {
            "state": "ACCUMULATING_STRICT_OOS",
            "observed_cross_activation_count": len(observed_by_station[station]),
            "failures": failures,
        }
        blockers.append(f"native_strict_oos_not_ready:{station}")
    identity.update({
        "state": "PASS" if not blockers else "BLOCKED_FAIL_CLOSED",
        "blockers": blockers,
        "ready_station_ids": ready_stations,
        "unready_station_ids": unready_stations,
        "station_authority": station_authority,
    })
    return {**identity, "content_sha256": content_sha256(identity)}


def cash_phase_authority_snapshot(
    conn: sqlite3.Connection,
    *,
    station_ids: Sequence[str],
    as_of_utc: str,
    authority_scope: str,
) -> dict[str, Any]:
    """Resolve the cash policy directly from its append-only SSOT tables."""

    scope = normalize_station_scope(station_ids)
    required_scope = str(authority_scope or "").strip().upper()
    identity: dict[str, Any] = {
        "schema": "cash_phase_preflight_authority_v1",
        "as_of_utc": as_of_utc,
        "station_scope": {"mode": "allowlist", "station_ids": list(scope)},
        "required_authority_scope": required_scope,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    if required_scope != "PRODUCTION_NO_LIVE_CANDIDATE":
        identity.update({
            "state": "BLOCKED_FAIL_CLOSED",
            "blockers": ["cash_phase_authority_scope_invalid"],
            "ready_station_ids": [],
            "unready_station_ids": list(scope),
            "station_authority": {},
        })
        return {**identity, "content_sha256": content_sha256(identity)}
    try:
        now = _parse_utc(as_of_utc)
    except (TypeError, ValueError):
        identity.update({
            "state": "BLOCKED_FAIL_CLOSED",
            "blockers": ["cash_phase_authority_as_of_invalid"],
            "ready_station_ids": [],
            "unready_station_ids": list(scope),
            "station_authority": {},
        })
        return {**identity, "content_sha256": content_sha256(identity)}
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    required_tables = {
        "basket_cash_phase_policy_artifact",
        "basket_cash_phase_authority_manifest",
    }
    missing = sorted(required_tables - tables)
    if missing:
        identity.update({
            "state": "BLOCKED_FAIL_CLOSED",
            "blockers": ["cash_phase_authority_tables_missing"],
            "missing_tables": missing,
            "ready_station_ids": [],
            "unready_station_ids": list(scope),
            "station_authority": {},
        })
        return {**identity, "content_sha256": content_sha256(identity)}
    placeholders = ",".join("?" for _ in scope)
    rows = conn.execute(
        "SELECT m.station_id,m.policy_version,m.artifact_sha256,"
        "m.station_timezone,m.valid_from_utc,m.valid_until_utc,"
        "m.authority_scope,m.manifest_sha256,p.promoted,"
        "p.evidence_status,p.content_sha256 "
        "FROM basket_cash_phase_authority_manifest m "
        "JOIN basket_cash_phase_policy_artifact p "
        "ON p.policy_version=m.policy_version "
        "AND p.content_sha256=m.artifact_sha256 "
        "AND p.station_id=m.station_id "
        "AND p.station_timezone=m.station_timezone "
        "AND p.valid_from_utc=m.valid_from_utc "
        "AND p.valid_until_utc=m.valid_until_utc "
        f"WHERE UPPER(m.station_id) IN ({placeholders}) "
        "AND m.authority_scope=? "
        "ORDER BY m.station_id,m.valid_from_utc,m.manifest_sha256",
        (*scope, required_scope),
    ).fetchall()
    by_station: dict[str, list[sqlite3.Row]] = {station: [] for station in scope}
    for row in rows:
        station = str(row[0]).strip().upper()
        if station in by_station:
            by_station[station].append(row)
    station_authority: dict[str, dict[str, Any]] = {}
    ready_stations: list[str] = []
    unready_stations: list[str] = []
    blockers: list[str] = []
    for station in scope:
        active: list[sqlite3.Row] = []
        for row in by_station[station]:
            try:
                valid_now = _parse_utc(row[4]) <= now < _parse_utc(row[5])
            except (TypeError, ValueError):
                valid_now = False
            if (
                valid_now
                and int(row[8]) == 0
                and str(row[9]) != "PROMOTED"
                and _sha256(row[2])
                and _sha256(row[7])
                and _sha256(row[10])
            ):
                active.append(row)
        if len(active) != 1:
            unready_stations.append(station)
            station_authority[station] = {
                "state": "NOT_READY",
                "candidate_manifest_count": len(by_station[station]),
                "active_exact_manifest_count": len(active),
            }
            blockers.append(f"cash_phase_authority_not_ready:{station}")
            continue
        row = active[0]
        station_authority[station] = {
            "state": "READY",
            "policy_version": str(row[1]),
            "artifact_sha256": str(row[2]),
            "manifest_sha256": str(row[7]),
            "valid_until_utc": str(row[5]),
        }
        ready_stations.append(station)
    identity.update({
        "state": "PASS" if not blockers else "BLOCKED_FAIL_CLOSED",
        "blockers": blockers,
        "ready_station_ids": ready_stations,
        "unready_station_ids": unready_stations,
        "station_authority": station_authority,
    })
    return {**identity, "content_sha256": content_sha256(identity)}


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
    station_ids: Sequence[str],
    db_path: Path,
    activation_path: Path,
    market_manifest_path: Path,
    native_oos_snapshot_path: Path = DEFAULT_NATIVE_OOS_SNAPSHOT,
    output_root: Path,
    process_snapshotter: Callable[[], dict[str, int]],
) -> dict[str, Any]:
    blockers: list[str] = []
    resources = _resource_status(db_path, output_root)
    blockers.extend(resources["blockers"])

    activation = load_target_activation(activation_path)
    blockers.extend(_validate_activation(activation))
    manifest = load_bounded_json(market_manifest_path)
    if not isinstance(manifest, Mapping):
        raise RuntimeError("market_manifest_mapping_required")
    blockers.extend(_validate_market_manifest(manifest))
    blockers.extend(_validate_manifest_station_scope(
        station_ids=station_ids,
        manifest=manifest,
    ))
    blockers.extend(_validate_boundary_tree(manifest, path="$.market_manifest"))
    native_oos_snapshot: NativeOosSnapshot | None = None
    native_oos_snapshot_error: str | None = None
    try:
        native_oos_snapshot = load_native_oos_snapshot_manifest(
            native_oos_snapshot_path
        )
        outside_snapshot_scope = sorted(
            set(native_oos_snapshot.station_ids)
            - set(CANONICAL_STATION_IDS)
        )
        if outside_snapshot_scope:
            raise RuntimeError(
                "native_oos_snapshot_station_scope_invalid:"
                + ",".join(outside_snapshot_scope)
            )
    except Exception as exc:
        native_oos_snapshot = None
        native_oos_snapshot_error = (
            f"{type(exc).__name__}:{exc}"
        )
        blockers.append("native_oos_snapshot_manifest_invalid")

    with _open_read_only(db_path) as conn:
        schema_status = production_ssot_schema_status(conn)
        if (
            schema_status.get("ok") is not True
            or schema_status.get("ledger_schema_ok") is not True
        ):
            blockers.append("target_production_ssot_schema_not_installed")
            native_authority = {
                "state": "BLOCKED_FAIL_CLOSED",
                "blockers": ["target_production_ssot_schema_not_installed"],
                "ready_station_ids": [],
                "unready_station_ids": list(station_ids),
                "content_sha256": content_sha256({
                    "schema_status": schema_status,
                    "station_ids": list(station_ids),
                }),
            }
            cash_phase_authority = {
                "state": "BLOCKED_FAIL_CLOSED",
                "blockers": ["target_production_ssot_schema_not_installed"],
                "ready_station_ids": [],
                "unready_station_ids": list(station_ids),
                "content_sha256": content_sha256({
                    "schema_status": schema_status,
                    "station_ids": list(station_ids),
                    "authority": "cash_phase",
                }),
            }
        else:
            as_of_utc = _now()
            native_authority = native_ensemble_authority_snapshot(
                conn,
                station_ids=station_ids,
                as_of_utc=as_of_utc,
                native_oos_snapshot=native_oos_snapshot,
                native_oos_snapshot_error=native_oos_snapshot_error,
            )
            cash_phase_authority = cash_phase_authority_snapshot(
                conn,
                station_ids=station_ids,
                as_of_utc=as_of_utc,
                authority_scope=str(
                    activation.get("policy_authority_scope") or ""
                ),
            )
            # Strict-OOS accumulation is a station-level fail-closed state,
            # not a reason to stop the production_no_live worker.  Formal
            # Promotion/live remains blocked until a historical strict-as-of
            # OOS artifact exists; the worker records unready stations and
            # emits no orders.
            native_soft_blockers = [
                str(value)
                for value in native_authority.get("blockers") or []
                if not str(value).startswith("native_strict_oos_not_ready:")
            ]
            # Ready stations expose their validated rows as ``candidate_count``
            # while accumulating stations expose only
            # ``observed_cross_activation_count``.  Counting only the latter
            # made a fully activated native chain look empty (and blocked the
            # whole no-live preflight) exactly when every ready station had a
            # valid candidate.  Both are evidence of an observed cross
            # activation; keep the per-station readiness gate unchanged.
            observed_cross = sum(
                int(
                    (value or {}).get("candidate_count")
                    or (value or {}).get("observed_cross_activation_count")
                    or 0
                )
                for value in (native_authority.get("station_authority") or {}).values()
                if isinstance(value, Mapping)
            )
            native_authority["production_no_live_soft_blockers"] = sorted(
                set(native_authority.get("blockers") or [])
            )
            native_authority["observed_cross_activation_count"] = observed_cross
            # A missing native foundation is hard; an empty/invalid native
            # chain is also hard because it would make this coordinator a
            # shell around a legacy fallback.  Accumulating station rows are
            # deliberately retained as evidence, never promoted.
            hard_native = [
                code for code in native_soft_blockers
                if code in {
                    "native_ensemble_foundation_schema_not_installed",
                    "native_ensemble_authority_as_of_invalid",
                    "native_oos_snapshot_manifest_invalid",
                }
            ]
            if observed_cross == 0:
                hard_native.append("native_cross_ensemble_evidence_missing")
            blockers.extend(hard_native)
            blockers.extend(cash_phase_authority.get("blockers") or [])
        counts = _table_counts(conn)
    processes = process_snapshotter()
    return {
        "state": "PASS" if not blockers else "BLOCKED_FAIL_CLOSED",
        "blockers": sorted(set(blockers)),
        "station_scope": {
            "mode": "allowlist",
            "station_ids": list(station_ids),
            "max_station_count": MAX_STATION_SCOPE,
        },
        "resources": resources,
        "schema_status": schema_status,
        "native_ensemble_authority": native_authority,
        "native_oos_snapshot": {
            "path": str(native_oos_snapshot_path),
            "state": "PASS" if native_oos_snapshot is not None else "BLOCKED_FAIL_CLOSED",
            "content_sha256": (
                native_oos_snapshot.content_sha256
                if native_oos_snapshot is not None
                else None
            ),
            "station_ids": (
                list(native_oos_snapshot.station_ids)
                if native_oos_snapshot is not None
                else []
            ),
            "error": native_oos_snapshot_error,
        },
        "cash_phase_authority": cash_phase_authority,
        "market_manifest": {
            "path": str(market_manifest_path),
            "sha256": _sha256_file(market_manifest_path),
            "event_dates": _manifest_scope(manifest)[1],
            "station_ids": _manifest_scope(manifest)[0],
        },
        "activation": {
            "path": str(activation_path),
            "sha256": _sha256_file(activation_path),
            "execution_mode": activation.get("execution_mode"),
        },
        "protected_processes": processes,
        "table_counts": counts,
        "historical_oos_boundary": dict(ATOMIC_HISTORICAL_OOS_BOUNDARY),
        "legacy_fallback_used": False,
        "release": _release_identity(
            station_ids=station_ids,
            activation_path=activation_path,
            native_authority_sha256=content_sha256({
                "native_ensemble_authority": native_authority["content_sha256"],
                "cash_phase_authority": cash_phase_authority["content_sha256"],
            }),
            schema_status=schema_status,
        ),
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
    """Run the resident worker with a bounded child lifetime.

    ``subprocess.run`` raises ``TimeoutExpired`` after terminating the child.
    Convert that condition into a deterministic non-zero return code so the
    caller can still emit its content-addressed no-live receipt and preserve
    the fail-closed boundary.
    """
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
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
        except subprocess.TimeoutExpired:
            stderr.write(
                f"worker_timeout_after_seconds={int(timeout_seconds)}\n".encode(
                    "utf-8"
                )
            )
            stderr.flush()
            return WORKER_TIMEOUT_RETURN_CODE
    return int(result.returncode)


def run(
    *,
    station_ids: Sequence[str] | None = None,
    db_path: Path = DEFAULT_TARGET_DB,
    canonical_db_path: Path = DEFAULT_CANONICAL_DB,
    activation_path: Path = DEFAULT_ACTIVATION,
    market_manifest_path: Path = DEFAULT_MARKET_MANIFEST,
    native_oos_snapshot_path: Path = DEFAULT_NATIVE_OOS_SNAPSHOT,
    status_path: Path = DEFAULT_STATUS,
    receipt_path: Path = DEFAULT_RECEIPT,
    proxy_url: str = "http://127.0.0.1:1082",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    execute: bool = True,
    reuse_materialized_probability: bool = False,
    process_snapshotter: Callable[[], dict[str, int]] = _default_process_snapshot,
    worker_runner: Callable[..., int] = _default_worker_runner,
) -> dict[str, Any]:
    started_at = _now()
    scope = normalize_station_scope(station_ids)
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
    manifest = _bounded_file(market_manifest_path, label="market_manifest")
    # Keep the pre-resolved path so the loader can reject a symlink rather
    # than silently following it into an unbounded/replaceable authority.
    native_oos_snapshot = native_oos_snapshot_path.expanduser()
    status = status_path.expanduser().resolve()
    receipt = receipt_path.expanduser().resolve()
    status.parent.mkdir(parents=True, exist_ok=True)
    receipt.parent.mkdir(parents=True, exist_ok=True)

    preflight = _build_preflight(
        station_ids=scope,
        db_path=db,
        activation_path=activation,
        market_manifest_path=manifest,
        native_oos_snapshot_path=native_oos_snapshot,
        output_root=receipt.parent,
        process_snapshotter=process_snapshotter,
    )
    db_before = _db_fingerprint(db)
    if preflight["state"] != "PASS":
        result = {
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "stage": "preflight",
            "station_scope": preflight["station_scope"],
            "started_at_utc": started_at,
            "completed_at_utc": _now(),
            "preflight": preflight,
            "worker_invoked": False,
        "historical_oos_boundary": dict(HISTORICAL_OOS_BOUNDARY),
            "execution_mode": EXECUTION_MODE,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
            "target_db_fingerprint_before": db_before,
            "target_db_fingerprint_after": _db_fingerprint(db),
        }
        _finalize_runner_result(result, stage="preflight")
        write_bounded_json(receipt, result, limit=MAX_OUTPUT_BYTES)
        return result
    if not execute:
        result = {
            "schema": SCHEMA,
            "state": "PREFLIGHT_READY",
            "stage": "preflight",
            "station_scope": preflight["station_scope"],
            "started_at_utc": started_at,
            "completed_at_utc": _now(),
            "preflight": preflight,
            "worker_invoked": False,
        "historical_oos_boundary": dict(HISTORICAL_OOS_BOUNDARY),
            "execution_mode": EXECUTION_MODE,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
            "target_db_fingerprint_before": db_before,
            "target_db_fingerprint_after": _db_fingerprint(db),
        }
        _finalize_runner_result(result, stage="preflight")
        write_bounded_json(receipt, result, limit=MAX_OUTPUT_BYTES)
        return result

    manifest_payload = load_bounded_json(manifest)
    if not isinstance(manifest_payload, Mapping):
        raise RuntimeError("market_manifest_mapping_required")
    _, event_dates = _manifest_scope(manifest_payload)
    if not event_dates:
        raise RuntimeError("market_manifest_event_date_missing")
    env = dict(os.environ)
    env.update(
        {
            "PM_TARGET_PRODUCTION_WRITER": "1",
            "PM_TARGET_EXECUTION_MODE": EXECUTION_MODE,
            "PM_TARGET_ACTIVATION": str(activation),
            "PM_TARGET_MARKET_MANIFEST": str(manifest),
            "PM_TARGET_NATIVE_OOS_SNAPSHOT_MANIFEST": str(native_oos_snapshot),
            "PM_TARGET_STATION_ALLOWLIST": ",".join(scope),
            "PM_TARGET_EVENT_DATE_ALLOWLIST": ",".join(event_dates),
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
        if returncode == WORKER_TIMEOUT_RETURN_CODE:
            boundary_blockers.append("worker_timeout")
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
            "station_scope": preflight["station_scope"],
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
        "historical_oos_boundary": dict(HISTORICAL_OOS_BOUNDARY),
            "execution_mode": EXECUTION_MODE,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
            "target_db_fingerprint_before": db_before,
            "target_db_fingerprint_after": _db_fingerprint(db),
        }
        _finalize_runner_result(result, stage="resident_restart")
        write_bounded_json(receipt, result, limit=MAX_OUTPUT_BYTES)
        return result
    finally:
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--station-allowlist",
        help=(
            "comma-separated canonical station allowlist; omitted means "
            "the frozen canonical 45-station registry"
        ),
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--canonical-db", type=Path, default=DEFAULT_CANONICAL_DB)
    parser.add_argument("--activation", type=Path, default=DEFAULT_ACTIVATION)
    parser.add_argument(
        "--market-manifest", type=Path, default=DEFAULT_MARKET_MANIFEST
    )
    parser.add_argument(
        "--native-oos-snapshot-manifest",
        type=Path,
        default=DEFAULT_NATIVE_OOS_SNAPSHOT,
    )
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--proxy-url", default="http://127.0.0.1:1082")
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
            station_ids=(
                args.station_allowlist.split(",")
                if args.station_allowlist
                else None
            ),
            db_path=args.db,
            canonical_db_path=args.canonical_db,
            activation_path=args.activation,
            market_manifest_path=args.market_manifest,
            native_oos_snapshot_path=args.native_oos_snapshot_manifest,
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
            "historical_oos_boundary": dict(HISTORICAL_OOS_BOUNDARY),
            "execution_mode": EXECUTION_MODE,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
        _finalize_runner_result(result, stage="preflight")
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
