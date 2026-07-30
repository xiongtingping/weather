#!/usr/bin/env python3
"""Audit and purge isolated target operational rows created before ET start.

Older target-only workers could materialize a blocked cycle/decision for an ET
day before that day started.  Such rows are neither formal model/market input
nor useful operational state once their zero-order boundary has been proved.
This command writes a compact content-addressed audit receipt and, only with
``--purge``, removes rows that satisfy every no-downstream invariant.

The command is hard-scoped to the isolated target ``production_no_live`` DB.
It never opens the shared live DB, changes launchd, or calls a gateway.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import event_day_window  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


SCHEMA = "bayes_basket_target_future_operational_residue_cleanup_v1"
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = ROOT / "output" / "audits" / "bayes_basket_target_future_operational_residue_cleanup.json"
PROTECTED_DB = ROOT / "data" / "archive.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("future_operational_residue_as_of_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("future_operational_residue_as_of_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _tables_with_column(conn: sqlite3.Connection, column: str) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows if column in _columns(conn, str(row[0]))]


def _row_dict(conn: sqlite3.Connection, table: str, raw: sqlite3.Row) -> dict[str, Any]:
    columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
    return dict(zip(columns, raw))


def _row_hash(row: Mapping[str, Any]) -> str:
    return content_sha256(dict(row))


def _candidate_rows(conn: sqlite3.Connection, *, as_of_utc: str) -> tuple[list[dict[str, Any]], list[str]]:
    required_tables = ("basket_target_execution_cycles", "auto_trade_decisions")
    blockers = [f"{table}_missing" for table in required_tables if not _table_exists(conn, table)]
    if blockers:
        return [], blockers

    cycles: dict[str, dict[str, Any]] = {}
    cycle_columns = _columns(conn, "basket_target_execution_cycles")
    required_cycle_columns = {"cycle_id", "event_date", "state", "created_at", "requested_reserve_usdc"}
    missing_cycle_columns = sorted(required_cycle_columns - cycle_columns)
    if missing_cycle_columns:
        return [], ["cycle_columns_missing:" + ",".join(missing_cycle_columns)]

    for raw in conn.execute("SELECT * FROM basket_target_execution_cycles ORDER BY cycle_id"):
        row = _row_dict(conn, "basket_target_execution_cycles", raw)
        try:
            window = event_day_window(str(row.get("event_date") or ""), as_of_utc=str(row.get("created_at") or ""))
        except (ContractError, TypeError, ValueError) as exc:
            blockers.append(f"cycle_time_invalid:{row.get('cycle_id')}:{type(exc).__name__}")
            continue
        if window["state"] != "not_started":
            continue
        cycle_id = str(row.get("cycle_id") or "")
        if not cycle_id:
            blockers.append("cycle_id_missing")
            continue
        cycles[cycle_id] = row

    decisions_by_cycle: dict[str, list[dict[str, Any]]] = {}
    decision_columns = _columns(conn, "auto_trade_decisions")
    required_decision_columns = {"decision_id", "cycle_id", "decided_state", "will_order", "created_at"}
    missing_decision_columns = sorted(required_decision_columns - decision_columns)
    if missing_decision_columns:
        return [], ["decision_columns_missing:" + ",".join(missing_decision_columns)]
    for raw in conn.execute("SELECT * FROM auto_trade_decisions ORDER BY decision_id"):
        row = _row_dict(conn, "auto_trade_decisions", raw)
        cycle_id = str(row.get("cycle_id") or "")
        if cycle_id in cycles:
            decisions_by_cycle.setdefault(cycle_id, []).append(row)

    # All tables carrying a cycle/decision/order identity are checked.  The
    # two source rows themselves are excluded from this downstream scan.
    cycle_ref_tables = set(_tables_with_column(conn, "cycle_id")) - {
        "basket_target_execution_cycles",
        "auto_trade_decisions",
    }
    decision_ref_tables = set(_tables_with_column(conn, "decision_id")) - {"auto_trade_decisions"}
    order_ref_tables = set(_tables_with_column(conn, "order_intent_id"))

    candidates: list[dict[str, Any]] = []
    for cycle_id, cycle in sorted(cycles.items()):
        decisions = decisions_by_cycle.get(cycle_id, [])
        if len(decisions) != 1:
            blockers.append(f"cycle_decision_cardinality:{cycle_id}:{len(decisions)}")
            continue
        decision = decisions[0]
        if str(cycle.get("state") or "") != "prepared":
            blockers.append(f"cycle_state_not_prepared:{cycle_id}")
            continue
        try:
            reserve = float(cycle.get("requested_reserve_usdc") or 0.0)
        except (TypeError, ValueError):
            blockers.append(f"cycle_reserve_invalid:{cycle_id}")
            continue
        if reserve != 0.0:
            blockers.append(f"cycle_reserve_nonzero:{cycle_id}")
            continue
        if str(decision.get("decided_state") or "") != "blocked" or int(decision.get("will_order") or 0) != 0:
            blockers.append(f"decision_not_zero_order_blocked:{cycle_id}")
            continue

        downstream_counts: dict[str, int] = {}
        for table in sorted(cycle_ref_tables):
            downstream_counts[table] = int(conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE cycle_id=?", (cycle_id,)
            ).fetchone()[0])
        decision_id = str(decision.get("decision_id") or "")
        for table in sorted(decision_ref_tables):
            downstream_counts[table] = downstream_counts.get(table, 0) + int(conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE decision_id=?", (decision_id,)
            ).fetchone()[0])
        # An order-intent row would be reachable via its own identity even if
        # an older schema omitted cycle_id/decision_id from the child table.
        for table in sorted(order_ref_tables - {"basket_target_order_intents"}):
            downstream_counts[table] = downstream_counts.get(table, 0)
        if any(downstream_counts.values()):
            blockers.append(f"future_residue_has_downstream:{cycle_id}")
            continue

        candidates.append({
            "cycle_id": cycle_id,
            "decision_id": decision_id,
            "station_id": str(cycle.get("station_id") or ""),
            "event_date": str(cycle.get("event_date") or ""),
            "cycle_created_at": str(cycle.get("created_at") or ""),
            "decision_created_at": str(decision.get("created_at") or ""),
            "cycle_sha256": _row_hash(cycle),
            "decision_sha256": _row_hash(decision),
            "downstream_counts": downstream_counts,
        })
    return candidates, sorted(set(blockers))


def _build_receipt(*, db: Path, as_of_utc: str, candidates: list[dict[str, Any]], blockers: list[str], purge_requested: bool, purged: bool, deleted_counts: Mapping[str, int]) -> dict[str, Any]:
    core = {
        "schema": SCHEMA,
        "generated_at_utc": _now(),
        "as_of_utc": as_of_utc,
        "target_database": str(db),
        "database_scope": "isolated_target_no_live",
        "protected_database_untouched": str(db) != str(PROTECTED_DB.resolve()),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "blockers": blockers,
        "purge_requested": purge_requested,
        "purged": purged,
        "deleted_counts": dict(deleted_counts),
        "boundary": {
            "writes_shared_live_database": False,
            "writes_unrelated_production_line": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    return {**core, "receipt_sha256": content_sha256(core)}


def run(*, db: Path = DEFAULT_DB, output: Path = DEFAULT_OUTPUT, as_of_utc: str = "", purge: bool = False) -> dict[str, Any]:
    target = db.expanduser().resolve()
    if target == PROTECTED_DB.resolve():
        raise ContractError("protected_database_target_forbidden")
    if target != DEFAULT_DB.resolve():
        raise ContractError("future_residue_target_must_be_isolated_target_db")
    if not target.is_file():
        raise ContractError("target_database_missing")
    clock = _utc(as_of_utc or _now())
    deleted = {"basket_target_execution_cycles": 0, "auto_trade_decisions": 0}
    purge_blockers: list[str] = []
    purge_succeeded = False
    with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        candidates, blockers = _candidate_rows(conn, as_of_utc=clock)
    if purge and blockers:
        raise ContractError("future_residue_purge_blocked:" + ";".join(blockers[:10]))
    if purge and candidates:
        ids = [str(item["cycle_id"]) for item in candidates]
        decision_ids = [str(item["decision_id"]) for item in candidates]
        try:
            with sqlite3.connect(target, timeout=30.0) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                live_candidates, live_blockers = _candidate_rows(conn, as_of_utc=clock)
                if live_blockers or [item["cycle_id"] for item in live_candidates] != ids:
                    conn.rollback()
                    raise ContractError("future_residue_purge_target_changed")
                conn.executemany("DELETE FROM auto_trade_decisions WHERE decision_id=?", [(item,) for item in decision_ids])
                deleted["auto_trade_decisions"] = int(conn.total_changes)
                conn.executemany("DELETE FROM basket_target_execution_cycles WHERE cycle_id=?", [(item,) for item in ids])
                deleted["basket_target_execution_cycles"] = int(conn.total_changes) - deleted["auto_trade_decisions"]
                conn.commit()
            purge_succeeded = True
        except sqlite3.IntegrityError as exc:
            # The target ledger is append-only by contract.  Do not attempt to
            # disable triggers or rewrite the SSOT merely to reclaim residue;
            # retain the compact audit receipt instead.
            deleted = {"basket_target_execution_cycles": 0, "auto_trade_decisions": 0}
            purge_blockers.append("target_ssot_append_only:" + str(exc))
    receipt = _build_receipt(
        db=target,
        as_of_utc=clock,
        candidates=candidates,
        blockers=sorted(set(blockers + purge_blockers)),
        purge_requested=purge,
        purged=purge_succeeded,
        deleted_counts=deleted,
    )
    write_bounded_json(output.expanduser().resolve(), receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--as-of-utc", default="")
    parser.add_argument("--purge", action="store_true", help="delete only fully proven isolated future residue")
    args = parser.parse_args()
    result = run(db=args.db, output=args.output, as_of_utc=args.as_of_utc, purge=args.purge)
    print(json.dumps({
        "schema": result["schema"],
        "candidate_count": result["candidate_count"],
        "blocker_count": len(result["blockers"]),
        "purged": result["purged"],
        "deleted_counts": result["deleted_counts"],
        "protected_database_untouched": result["protected_database_untouched"],
        "receipt_sha256": result["receipt_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not result["blockers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
