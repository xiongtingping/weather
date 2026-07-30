#!/usr/bin/env python3
"""Read-only M0 closure audit for the isolated Bayes basket target lane."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.activation import DEFAULT_PATH as DEFAULT_ACTIVATION  # noqa: E402
from bayes_basket_target.activation import TARGET_STRATEGY_ID, load_target_activation  # noqa: E402
from bayes_basket_target.database import SHARED_LIVE_DB  # noqa: E402
from bayes_basket_target.execution_vector_repair import (  # noqa: E402
    inspect_legacy_blocked_execution_vector_repair,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
    assert_target_db_capacity,
    load_bounded_json,
    write_bounded_json,
)

DEFAULT_DB = ROOT / "data/bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_m0_closure_audit.json"
PENDING_CYCLE_STATES = ("prepared", "submitting", "submitted", "reconcile_required")
ZERO_ORDER_TABLES = (
    "basket_target_order_intents",
    "basket_target_capital_reservations",
    "basket_target_order_events",
    "basket_target_order_reconciliations",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _open_target_read_only(path: Path) -> sqlite3.Connection:
    resolved = assert_target_db_capacity(path, projected_bytes=0).resolve()
    if resolved == SHARED_LIVE_DB.resolve():
        raise ValueError("m0_audit_shared_live_database_forbidden")
    conn = sqlite3.connect(f"file:{resolved}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _activation_audit(
    conn: sqlite3.Connection,
    *,
    activation_path: Path,
) -> dict[str, Any]:
    file_activation = load_target_activation(activation_path)
    rows = conn.execute(
        "SELECT generation,state,strategy_id,activation_sha256,activation_json,updated_at "
        "FROM basket_strategy_activation_state WHERE singleton=1"
    ).fetchall()
    if len(rows) != 1:
        return {
            "ok": False,
            "reason": "target_activation_singleton_not_unique",
            "row_count": len(rows),
        }
    row = rows[0]
    try:
        runtime = json.loads(str(row["activation_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        runtime = {}
    runtime_core = {
        key: runtime.get(key)
        for key in file_activation
        if key != "activation_contract_sha256"
    }
    file_core = {
        key: value
        for key, value in file_activation.items()
        if key != "activation_contract_sha256"
    }
    generation = int(row["generation"] or 0)
    checks = {
        "db_state_enabled": str(row["state"] or "") == "enabled",
        "strategy_id_matches": str(row["strategy_id"] or "") == TARGET_STRATEGY_ID,
        "contract_sha256_matches": (
            str(row["activation_sha256"] or "")
            == str(file_activation["activation_contract_sha256"])
            == str(runtime.get("activation_contract_sha256") or "")
        ),
        "runtime_core_matches_file": runtime_core == file_core,
        "runtime_generation_matches_db": int(runtime.get("generation") or 0) == generation,
        "generation_positive": generation > 0,
        "execution_mode_no_live": runtime.get("execution_mode") == "production_no_live",
        "gateway_disabled": runtime.get("calls_order_gateway") is False,
        "live_submit_disabled": runtime.get("live_submit_authorized") is False,
    }
    return {
        "ok": all(checks.values()),
        "generation": generation,
        "updated_at": str(row["updated_at"] or ""),
        "activation_contract_sha256": str(row["activation_sha256"] or ""),
        "activation_file_sha256": _sha256(activation_path),
        "checks": checks,
    }


def _acceptance_audit(paths: Sequence[Path]) -> dict[str, Any]:
    receipts: list[dict[str, Any]] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        value = load_bounded_json(resolved)
        stat = resolved.stat()
        checks = {
            "schema_valid": isinstance(value, Mapping)
            and value.get("schema") == "bayes_basket_target_production_no_live_acceptance_v1",
            "status_pass": isinstance(value, Mapping) and value.get("status") == "PASS",
            "orders_zero": isinstance(value, Mapping) and value.get("orders_submitted") == 0,
            "gateway_disabled": isinstance(value, Mapping) and value.get("calls_order_gateway") is False,
            "production_db_write_disabled": isinstance(value, Mapping)
            and value.get("writes_production_db") is False,
            "live_order_disabled": isinstance(value, Mapping)
            and value.get("live_order_authorized") is False,
        }
        receipts.append({
            "path": str(resolved),
            "sha256": _sha256(resolved),
            "mtime_ns": int(stat.st_mtime_ns),
            "inode": int(stat.st_ino),
            "checks": checks,
            "ok": all(checks.values()),
        })
    distinct_paths = len({item["path"] for item in receipts}) == len(receipts)
    distinct_files = len({(item["inode"], item["path"]) for item in receipts}) == len(receipts)
    repeatable = len({item["sha256"] for item in receipts}) == 1 if receipts else False
    return {
        "ok": len(receipts) >= 2
        and distinct_paths
        and distinct_files
        and repeatable
        and all(item["ok"] for item in receipts),
        "required_count": 2,
        "receipt_count": len(receipts),
        "distinct_paths": distinct_paths,
        "distinct_files": distinct_files,
        "semantic_output_repeatable": repeatable,
        "receipts": receipts,
    }


def build_audit(
    *,
    db_path: Path = DEFAULT_DB,
    activation_path: Path = DEFAULT_ACTIVATION,
    acceptance_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    assert_process_memory_budget()
    resolved_db = db_path.expanduser().resolve()
    with _open_target_read_only(resolved_db) as conn:
        tables = _table_names(conn)
        required = {
            "basket_strategy_activation_state",
            "basket_target_execution_cycles",
            "auto_trade_decisions",
            "basket_target_execution_version_vectors",
            "basket_target_capital_pool",
            *ZERO_ORDER_TABLES,
        }
        missing = sorted(required - tables)
        if missing:
            raise ValueError("m0_audit_required_tables_missing:" + ",".join(missing))

        integrity_ok = str(conn.execute("PRAGMA integrity_check").fetchone()[0]) == "ok"
        foreign_key_error_count = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        placeholders = ",".join("?" for _ in PENDING_CYCLE_STATES)
        actionable = int(conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_cycles c "
            f"WHERE c.state IN ({placeholders}) "
            "AND NOT EXISTS ("
            "SELECT 1 FROM auto_trade_decisions d "
            "WHERE d.cycle_id=c.cycle_id AND d.decided_state='blocked' AND d.will_order=0"
            ")",
            PENDING_CYCLE_STATES,
        ).fetchone()[0])
        retained_blocked = int(conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_cycles c "
            f"WHERE c.state IN ({placeholders}) "
            "AND EXISTS ("
            "SELECT 1 FROM auto_trade_decisions d "
            "WHERE d.cycle_id=c.cycle_id AND d.decided_state='blocked' AND d.will_order=0"
            ")",
            PENDING_CYCLE_STATES,
        ).fetchone()[0])
        conflicting_decisions = int(conn.execute(
            "SELECT COUNT(DISTINCT c.cycle_id) FROM basket_target_execution_cycles c "
            "JOIN auto_trade_decisions d ON d.cycle_id=c.cycle_id "
            f"WHERE c.state IN ({placeholders}) "
            "AND (d.decided_state<>'blocked' OR d.will_order<>0)",
            PENDING_CYCLE_STATES,
        ).fetchone()[0])
        cycles_without_decision = int(conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_cycles c "
            "WHERE NOT EXISTS (SELECT 1 FROM auto_trade_decisions d WHERE d.cycle_id=c.cycle_id)"
        ).fetchone()[0])
        cycles_without_exactly_one_decision = int(conn.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT c.cycle_id,COUNT(d.decision_id) AS decision_count "
            "FROM basket_target_execution_cycles c "
            "LEFT JOIN auto_trade_decisions d ON d.cycle_id=c.cycle_id "
            "GROUP BY c.cycle_id HAVING decision_count<>1"
            ")"
        ).fetchone()[0])
        empty_reason_decisions = int(conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions "
            "WHERE TRIM(reason_codes_json) IN ('','[]','null','{}')"
        ).fetchone()[0])
        decision_counts = {
            "total": _count(conn, "auto_trade_decisions"),
            "will_order_true": int(conn.execute(
                "SELECT COUNT(*) FROM auto_trade_decisions WHERE will_order<>0"
            ).fetchone()[0]),
            "nonblocked": int(conn.execute(
                "SELECT COUNT(*) FROM auto_trade_decisions WHERE decided_state<>'blocked'"
            ).fetchone()[0]),
        }
        zero_order_counts = {table: _count(conn, table) for table in ZERO_ORDER_TABLES}
        requested_reserve = float(conn.execute(
            "SELECT COALESCE(SUM(requested_reserve_usdc),0) "
            "FROM basket_target_execution_cycles"
        ).fetchone()[0])
        pool = conn.execute(
            "SELECT COUNT(*) AS row_count,COALESCE(SUM(balance_usdc),0) AS balance,"
            "COALESCE(SUM(reserved_usdc),0) AS reserved "
            "FROM basket_target_capital_pool"
        ).fetchone()
        activation = _activation_audit(conn, activation_path=activation_path.expanduser().resolve())
        vector_lineage = inspect_legacy_blocked_execution_vector_repair(conn)
        vector_lineage.pop("_candidates", None)

    cycle_checks = {
        "sqlite_integrity_ok": integrity_ok,
        "foreign_key_errors_zero": foreign_key_error_count == 0,
        "actionable_unresolved_zero": actionable == 0,
        "no_conflicting_nonblocked_decision": conflicting_decisions == 0,
        "every_cycle_has_decision": cycles_without_decision == 0,
        "every_cycle_has_exactly_one_decision": cycles_without_exactly_one_decision == 0,
        "every_decision_has_reason": empty_reason_decisions == 0,
        "blocked_zero_order_execution_vectors_complete": vector_lineage["ok"] is True,
    }
    gateway_checks = {
        "will_order_decisions_zero": decision_counts["will_order_true"] == 0,
        "nonblocked_decisions_zero": decision_counts["nonblocked"] == 0,
        "target_order_intents_zero": zero_order_counts["basket_target_order_intents"] == 0,
        "target_order_events_zero": zero_order_counts["basket_target_order_events"] == 0,
        "target_reconciliations_zero": zero_order_counts["basket_target_order_reconciliations"] == 0,
    }
    capital_checks = {
        "target_reservations_zero": zero_order_counts["basket_target_capital_reservations"] == 0,
        "requested_reserve_zero": abs(requested_reserve) < 1e-9,
        "pool_singleton": int(pool["row_count"]) == 1,
        "pool_balance_zero": abs(float(pool["balance"])) < 1e-9,
        "pool_reserved_zero": abs(float(pool["reserved"])) < 1e-9,
    }
    acceptance = _acceptance_audit(acceptance_paths)
    checks = {
        "cycle_reconciliation_closed": all(cycle_checks.values()),
        "gateway_idempotency_closed": all(gateway_checks.values()),
        "capital_ledger_closed": all(capital_checks.values()),
        "activation_consistent": activation["ok"] is True,
        "two_consecutive_acceptances_pass": acceptance["ok"] is True,
        "shared_live_database_not_read": resolved_db != SHARED_LIVE_DB.resolve(),
    }
    identity = {
        "schema": "bayes_basket_target_m0_closure_audit_v1",
        "status": "PASS" if all(checks.values()) else "BLOCKED",
        "checks": checks,
        "cycle_reconciliation": {
            "pending_state_definition": list(PENDING_CYCLE_STATES),
            "actionable_unresolved_count": actionable,
            "retained_blocked_cycle_count": retained_blocked,
            "conflicting_decision_cycle_count": conflicting_decisions,
            "cycles_without_decision_count": cycles_without_decision,
            "cycles_without_exactly_one_decision_count": cycles_without_exactly_one_decision,
            "empty_reason_decision_count": empty_reason_decisions,
            "sqlite_integrity": "ok" if integrity_ok else "failed",
            "foreign_key_error_count": foreign_key_error_count,
            "checks": cycle_checks,
            "retention_rule": "blocked cycles are append-only audit facts and are not deleted",
            "execution_vector_lineage": vector_lineage,
        },
        "target_local_gateway_boundary": {
            "decision_counts": decision_counts,
            "target_table_counts": zero_order_counts,
            "checks": gateway_checks,
            "scope_note": (
                "shared resident gateway health and idempotency state belong to another "
                "production line and are intentionally not read or mutated"
            ),
        },
        "capital_reconciliation": {
            "requested_reserve_usdc": requested_reserve,
            "pool_row_count": int(pool["row_count"]),
            "pool_balance_usdc": float(pool["balance"]),
            "pool_reserved_usdc": float(pool["reserved"]),
            "checks": capital_checks,
        },
        "activation": activation,
        "acceptance": acceptance,
        "boundary": {
            "read_only": True,
            "target_database": str(resolved_db),
            "shared_live_database": str(SHARED_LIVE_DB.resolve()),
            "reads_shared_live_database": False,
            "writes_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "deletes_retained_cycles": False,
        },
    }
    identity["receipt_sha256"] = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert_process_memory_budget()
    return identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--activation", type=Path, default=DEFAULT_ACTIVATION)
    parser.add_argument("--acceptance", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build_audit(
        db_path=args.db,
        activation_path=args.activation,
        acceptance_paths=args.acceptance,
    )
    write_bounded_json(args.output, result)
    print(json.dumps({
        "schema": result["schema"],
        "status": result["status"],
        "actionable_unresolved_count": result["cycle_reconciliation"]["actionable_unresolved_count"],
        "retained_blocked_cycle_count": result["cycle_reconciliation"]["retained_blocked_cycle_count"],
        "output": str(args.output.expanduser().resolve()),
        "receipt_sha256": result["receipt_sha256"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
