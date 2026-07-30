#!/usr/bin/env python3
"""Two-round read-only acceptance for RKPK probability-evidence resident use."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import (  # noqa: E402
    ContractError,
    content_sha256,
)
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.target_ssot_replay import (  # noqa: E402
    replay_durable_target_chain,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from scripts.bayes_basket_target_production_readiness import (  # noqa: E402
    _probability_evidence_runtime_status,
)


SCHEMA = "bayes_basket_target_probability_resident_two_round_acceptance_v1"
ROUND_SCHEMA = "bayes_basket_target_probability_resident_round_v1"
CORE_SERVICES = (
    "com.weather.reported_trajectory_no",
    "com.weather.market_ws",
    "com.weather.clob_order_service",
)
STATION_TABLES = (
    "basket_probability_evidence_artifact",
    "basket_probability_evidence_activation",
    "basket_probability_update_proof",
    "basket_probability_bridge_history",
    "basket_probability_bridge_latest",
    "basket_probability_envelope",
    "auto_trade_decisions",
    "basket_policy_training_fact",
)
MAX_CORE_ROWS = 512
MAX_JSON_BYTES = 1024 * 1024
MAX_DB_BYTES = 256 * 1024 * 1024
MAX_WAL_BYTES = 64 * 1024 * 1024
MIN_FREE_BYTES = 1024 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _service_pids() -> dict[str, int]:
    result: dict[str, int] = {}
    for label in CORE_SERVICES:
        completed = subprocess.run(
            ["/bin/launchctl", "print", f"system/{label}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"resident_acceptance_service_missing:{label}")
        pids = []
        for line in completed.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("pid = "):
                value = stripped.removeprefix("pid = ").strip()
                if value.isdigit():
                    pids.append(int(value))
        if len(pids) != 1:
            raise RuntimeError(
                f"resident_acceptance_service_pid_invalid:{label}"
            )
        result[label] = pids[0]
    return result


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _station_counts(
    conn: sqlite3.Connection,
    *,
    station_id: str,
) -> tuple[dict[str, int], dict[str, int]]:
    present = _table_names(conn)
    target: dict[str, int] = {}
    other: dict[str, int] = {}
    for table in STATION_TABLES:
        if table not in present:
            target[table] = -1
            other[table] = -1
            continue
        columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if "station_id" not in columns:
            target[table] = -1
            other[table] = -1
            continue
        target[table] = int(conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE station_id=?",
            (station_id,),
        ).fetchone()[0])
        other[table] = int(conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE station_id<>?",
            (station_id,),
        ).fetchone()[0])
    return target, other


def _validate_core_chain_rows(
    rows: Sequence[tuple[int, int, str]],
) -> dict[str, Any]:
    validated: list[dict[str, Any]] = []
    malformed = 0
    legacy = 0
    violations: list[str] = []
    for rowid, byte_length, raw_json in rows:
        try:
            if int(byte_length) <= 0 or int(byte_length) > MAX_JSON_BYTES:
                raise ValueError
            details = json.loads(str(raw_json))
            core = details.get("core_buckets")
            if core is None:
                continue
            if (
                not isinstance(core, list)
                or not core
                or any(
                    not isinstance(bucket, str) or not bucket.strip()
                    for bucket in core
                )
                or len(core) != len(set(core))
            ):
                raise ValueError
            direction = str(details.get("direction") or "").upper()
            current_direction_raw = details.get("current_direction")
            current_direction = (
                None
                if current_direction_raw in (None, "")
                else str(current_direction_raw).upper()
            )
            transition = details.get("core_transition")
            if (
                direction not in {"UP", "DOWN", "NEUTRAL"}
                or not isinstance(transition, Mapping)
                or str(
                    transition.get("direction_transition") or ""
                ).upper()
                not in {
                    "INITIALIZE",
                    "MIGRATE",
                    "NEUTRALIZE",
                    "KEEP",
                    "HOLD",
                }
            ):
                legacy += 1
                continue
            current_core = details.get("current_core_buckets") or []
            if not isinstance(current_core, list):
                raise ValueError
            validated.append({
                "rowid": int(rowid),
                "core_buckets": list(core),
                "current_core_buckets": list(current_core),
                "direction": direction,
                "current_direction": current_direction,
                "direction_transition": str(
                    transition["direction_transition"]
                ).upper(),
            })
        except (TypeError, ValueError, json.JSONDecodeError):
            malformed += 1
    for previous, current in zip(validated, validated[1:]):
        if current["current_core_buckets"] != previous["core_buckets"]:
            violations.append(
                f"core_predecessor_mismatch:{previous['rowid']}:{current['rowid']}"
            )
        if current["current_direction"] != previous["direction"]:
            violations.append(
                "direction_predecessor_mismatch:"
                f"{previous['rowid']}:{current['rowid']}"
            )
    return {
        "validated_count": len(validated),
        "legacy_count": legacy,
        "malformed_count": malformed,
        "violations": violations,
        "latest": validated[-1] if validated else None,
        "content_sha256": content_sha256(validated),
    }


def _latest_core_chain_rows(
    conn: sqlite3.Connection,
    *,
    station_id: str,
) -> tuple[list[tuple[int, int, str]], bool]:
    """Read only the newest bounded window, returned in causal order."""

    newest_first = conn.execute(
        "SELECT rowid,length(CAST(details_json AS BLOB)),details_json "
        "FROM auto_trade_decisions "
        "WHERE station_id=? AND strategy_lane='basket' "
        "ORDER BY rowid DESC LIMIT ?",
        (station_id, MAX_CORE_ROWS + 1),
    ).fetchall()
    truncated = len(newest_first) > MAX_CORE_ROWS
    bounded = newest_first[:MAX_CORE_ROWS]
    bounded.reverse()
    return bounded, truncated


def _forbidden_execution_counts(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    present = _table_names(conn)
    queries = {
        "will_order": (
            "auto_trade_decisions",
            "SELECT COUNT(*) FROM auto_trade_decisions WHERE will_order<>0",
        ),
        "live_submit_authorized": (
            "basket_target_order_intents",
            "SELECT COUNT(*) FROM basket_target_order_intents "
            "WHERE live_submit_authorized<>0",
        ),
        "order_events": (
            "basket_target_order_events",
            "SELECT COUNT(*) FROM basket_target_order_events",
        ),
        "execution_receipts": (
            "basket_target_execution_receipts",
            "SELECT COUNT(*) FROM basket_target_execution_receipts",
        ),
    }
    return {
        name: (
            int(conn.execute(sql).fetchone()[0])
            if table in present
            else -1
        )
        for name, (table, sql) in queries.items()
    }


def capture_round(
    db: Path,
    *,
    station_id: str = "RKPK",
    service_probe: Any = _service_pids,
) -> dict[str, Any]:
    database = db.expanduser().resolve()
    if not database.is_file() or database.is_symlink():
        raise RuntimeError("resident_acceptance_database_invalid")
    wal = Path(str(database) + "-wal")
    resources = {
        "database_bytes": int(database.stat().st_size),
        "wal_bytes": int(wal.stat().st_size) if wal.is_file() else 0,
        "free_bytes": int(shutil.disk_usage(database.parent).free),
    }
    blockers: list[str] = []
    if resources["database_bytes"] > MAX_DB_BYTES:
        blockers.append("database_size_exceeded")
    if resources["wal_bytes"] > MAX_WAL_BYTES:
        blockers.append("wal_size_exceeded")
    if resources["free_bytes"] < MIN_FREE_BYTES:
        blockers.append("minimum_free_disk_not_met")
    with sqlite3.connect(
        f"file:{database}?mode=ro",
        uri=True,
        timeout=30,
    ) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            blockers.append("quick_check_failed")
        runtime = _probability_evidence_runtime_status(
            conn,
            station_ids=(station_id,),
        )
        target_counts, other_counts = _station_counts(
            conn,
            station_id=station_id,
        )
        present = _table_names(conn)
        core_rows, core_rows_truncated = (
            _latest_core_chain_rows(conn, station_id=station_id)
            if "auto_trade_decisions" in present
            else ([], False)
        )
        core_chain = _validate_core_chain_rows(core_rows)
        core_chain["history_truncated"] = core_rows_truncated
        forbidden = _forbidden_execution_counts(conn)
        latest_fact_day = conn.execute(
            "SELECT event_date FROM basket_policy_training_fact "
            "WHERE station_id=? ORDER BY event_date DESC,rowid DESC LIMIT 1",
            (station_id,),
        ).fetchone()
        durable_replay: dict[str, Any] | None = None
        durable_replay_error: str | None = None
        if latest_fact_day is None:
            durable_replay_error = "durable_replay_training_facts_missing"
        else:
            try:
                durable_replay = replay_durable_target_chain(
                    conn,
                    station_id=station_id,
                    event_date=str(latest_fact_day[0]),
                )
            except ContractError as exc:
                durable_replay_error = str(exc)
    candidate_total = sum(
        int(value)
        for value in runtime.get("candidate_artifact_counts", {}).values()
    )
    fallback = runtime.get("fallback_proof_counts") or {}
    if runtime.get("candidate_activation_count") != 0:
        blockers.append("candidate_activation_not_zero")
    if candidate_total < 1:
        blockers.append("candidate_artifact_missing")
    if int(runtime.get("candidate_rejected_count") or 0) != candidate_total:
        blockers.append("candidate_rejection_incomplete")
    if int(fallback.get("P0_HISTORICAL_TILT") or 0) < 1:
        blockers.append("theta_zero_fallback_not_consumed")
    if int(fallback.get("METAR_TRAJECTORY_UPDATE") or 0) < 1:
        blockers.append("identity_likelihood_fallback_not_consumed")
    if runtime.get("fallback_contract_failures"):
        blockers.append("fallback_contract_invalid")
    if core_chain["validated_count"] < 1:
        blockers.append("directional_core_decision_missing")
    blockers.extend(core_chain["violations"])
    if any(value != 0 for value in forbidden.values()):
        blockers.append("forbidden_execution_fact_present")
    if durable_replay is None or durable_replay.get("state") != "PASS":
        blockers.append(
            "durable_ssot_replay_failed:"
            + str(durable_replay_error or "unknown")
        )
    identity = {
        "schema": ROUND_SCHEMA,
        "station_id": station_id,
        "database": str(database),
        "services": service_probe(),
        "resources": resources,
        "probability_evidence_runtime": runtime,
        "station_counts": target_counts,
        "non_target_station_counts": other_counts,
        "core_chain": core_chain,
        "durable_ssot_replay": durable_replay,
        "durable_ssot_replay_error": durable_replay_error,
        "forbidden_execution_counts": forbidden,
        "blockers": sorted(set(blockers)),
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    return {
        **identity,
        "captured_at_utc": _now(),
        "round_sha256": content_sha256(identity),
        "state": "PASS" if not blockers else "BLOCKED_FAIL_CLOSED",
    }


def compare_rounds(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []
    if first.get("state") != "PASS":
        blockers.append("round_one_not_ready")
    if second.get("state") != "PASS":
        blockers.append("round_two_not_ready")
    if first.get("services") != second.get("services"):
        blockers.append("service_pid_drift")
    if (
        first.get("non_target_station_counts")
        != second.get("non_target_station_counts")
    ):
        blockers.append("non_target_station_write_detected")
    first_counts = first.get("station_counts") or {}
    second_counts = second.get("station_counts") or {}
    for table, before in first_counts.items():
        after = second_counts.get(table)
        if (
            isinstance(before, int)
            and isinstance(after, int)
            and before >= 0
            and after < before
        ):
            blockers.append(f"target_append_only_count_decreased:{table}")
    first_replay = first.get("durable_ssot_replay")
    second_replay = second.get("durable_ssot_replay")
    if not isinstance(first_replay, Mapping) or not isinstance(
        second_replay, Mapping
    ):
        blockers.append("durable_ssot_replay_missing")
    else:
        first_rows = first_replay.get("rows")
        second_rows = second_replay.get("rows")
        if (
            first_replay.get("station_id") != second_replay.get("station_id")
            or first_replay.get("event_date") != second_replay.get("event_date")
            or first_replay.get("settlement_timezone")
            != second_replay.get("settlement_timezone")
            or not isinstance(first_rows, list)
            or not isinstance(second_rows, list)
            or len(second_rows) < len(first_rows)
            or second_rows[:len(first_rows)] != first_rows
        ):
            blockers.append("durable_ssot_replay_not_append_only_prefix")
        elif len(second_rows) == len(first_rows) and (
            first_replay.get("continuation")
            != second_replay.get("continuation")
        ):
            blockers.append("durable_ssot_replay_root_drift_without_append")
    for round_value in (first, second):
        if (
            round_value.get("production_no_live") is not True
            or round_value.get("calls_order_gateway") is not False
            or round_value.get("live_submit_authorized") is not False
            or int(round_value.get("orders_submitted") or 0) != 0
        ):
            blockers.append("no_live_boundary_invalid")
    identity = {
        "schema": SCHEMA,
        "round_one_sha256": first.get("round_sha256"),
        "round_two_sha256": second.get("round_sha256"),
        "services": second.get("services"),
        "non_target_station_counts": second.get(
            "non_target_station_counts"
        ),
        "target_station_counts": second_counts,
        "durable_ssot_replay": second_replay,
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
        "rounds": [dict(first), dict(second)],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--station-id", default="RKPK")
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 0.0 <= args.interval_seconds <= 60.0:
        raise SystemExit("interval-seconds must be in [0,60]")
    station = str(args.station_id).strip().upper()
    if station != "RKPK":
        raise SystemExit("two-round acceptance is frozen to RKPK")
    first = capture_round(args.db, station_id=station)
    if args.interval_seconds:
        time.sleep(args.interval_seconds)
    second = capture_round(args.db, station_id=station)
    result = compare_rounds(first, second)
    write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps({
        "state": result["state"],
        "content_sha256": result["content_sha256"],
        "output": str(args.output.expanduser().resolve()),
    }, sort_keys=True))
    return 0 if result["state"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
