#!/usr/bin/env python3
"""Atomically align the target DB activation with the no-live file contract.

This is a maintenance-window operation.  It never enables live submission,
opens a gateway, or creates an order.  The writer-quiescence receipt is
re-probed immediately before the activation transaction so a stale receipt
cannot authorize this DB mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.activation import load_target_activation  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, is_isolated_target_database, resolve_target_database  # noqa: E402
from bayes_basket_target.migration_quiescence import (  # noqa: E402
    build_writer_quiescence_receipt,
    isolated_target_writer_quiescence_receipt,
)
from bayes_basket_target.activation import assert_target_database_owner  # noqa: E402
from bayes_basket_target.ssot import production_ssot_schema_status  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_DB_BYTES,
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    write_bounded_json,
)


SCHEMA = "bayes_basket_target_no_live_activation_apply_v1"
NO_LIVE_FALSE_FIELDS = (
    "authorizes_order",
    "live_decision_write_authorized",
    "live_order_authorized",
    "live_submit_authorized",
    "production_mutation",
    "places_orders",
    "writes_production_db",
    "calls_order_gateway",
    "network_access",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _backup_database(source: Path, destination: Path) -> dict[str, Any]:
    source_input = source.expanduser()
    destination_input = destination.expanduser()
    if source_input.is_symlink() or destination_input.is_symlink():
        raise RuntimeError("activation_backup_symlink_forbidden")
    source = source_input.resolve()
    destination = destination_input.resolve()
    if destination == source or destination.exists():
        raise RuntimeError("activation_backup_path_invalid_or_reused")
    if os.path.commonpath((str(source.parent), str(destination.parent))) != str(source.parent):
        raise RuntimeError("activation_backup_outside_database_directory")
    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise RuntimeError("activation_backup_external_filesystem_forbidden")
    source_bytes = int(source.stat().st_size)
    wal_path = source.with_name(source.name + "-wal")
    wal_bytes = int(wal_path.stat().st_size) if wal_path.is_file() else 0
    if source_bytes > MAX_TARGET_DB_BYTES:
        raise RuntimeError("activation_backup_source_bytes_exceeded")
    free_bytes = int(shutil.disk_usage(destination.parent).free)
    projected_bytes = source_bytes + wal_bytes
    if free_bytes < MIN_TARGET_FREE_BYTES + projected_bytes:
        raise RuntimeError("activation_backup_free_space_insufficient")
    assert_process_memory_budget(projected_bytes=8 * 1024 * 1024)
    try:
        with sqlite3.connect(
            f"file:{source}?mode=ro",
            uri=True,
            timeout=60.0,
        ) as source_conn:
            if [
                str(row[0])
                for row in source_conn.execute("PRAGMA quick_check")
            ] != ["ok"]:
                raise RuntimeError(
                    "activation_backup_source_quick_check_failed"
                )
            with sqlite3.connect(
                destination,
                timeout=60.0,
            ) as destination_conn:
                source_conn.backup(destination_conn)
                destination_conn.commit()
                if [
                    str(row[0])
                    for row in destination_conn.execute(
                        "PRAGMA quick_check"
                    )
                ] != ["ok"]:
                    raise RuntimeError(
                        "activation_backup_quick_check_failed"
                    )
        backup_bytes = int(destination.stat().st_size)
        if backup_bytes > MAX_TARGET_DB_BYTES:
            raise RuntimeError("activation_backup_bytes_exceeded")
        assert_process_memory_budget()
        return {
            "path": str(destination),
            "bytes": backup_bytes,
            "source_bytes": source_bytes,
            "source_wal_bytes": wal_bytes,
            "free_bytes_before": free_bytes,
            "quick_check": "ok",
        }
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _online_no_live_cas_guard(
    db: Path,
    *,
    next_activation: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove an active isolated writer can observe only a no-live CAS.

    This deliberately does not claim process quiescence.  It proves the
    narrower safety property needed for a one-row activation replacement:
    both generations prohibit every live/network boundary, no exchange-side
    state exists, and every unclosed cycle is already blocked by the sole
    decision authority.
    """

    if next_activation.get("execution_mode") != "production_no_live":
        raise RuntimeError("online_activation_next_mode_not_no_live")
    if any(next_activation.get(key) is not False for key in NO_LIVE_FALSE_FIELDS):
        raise RuntimeError("online_activation_next_live_boundary_invalid")
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0) as conn:
        row = conn.execute(
            "SELECT generation,state,strategy_id,activation_sha256,activation_json "
            "FROM basket_strategy_activation_state WHERE singleton=1"
        ).fetchone()
        if row is None or str(row[1]) != "enabled":
            raise RuntimeError("online_activation_current_state_invalid")
        try:
            current = json.loads(str(row[4] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("online_activation_current_json_invalid") from exc
        legacy_authorizes_order_missing = (
            isinstance(current, Mapping)
            and "authorizes_order" not in current
        )
        current_false_fields = tuple(
            key
            for key in NO_LIVE_FALSE_FIELDS
            if key != "authorizes_order"
        )
        if (
            not isinstance(current, Mapping)
            or current.get("execution_mode") != "production_no_live"
            or any(
                current.get(key) is not False
                for key in current_false_fields
            )
            or current.get("authorizes_order") not in {None, False}
        ):
            raise RuntimeError("online_activation_current_live_boundary_invalid")
        if str(row[2]) != str(next_activation.get("strategy_id") or ""):
            raise RuntimeError("online_activation_strategy_owner_drift")
        unsafe_counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "basket_target_order_intents",
                "basket_target_capital_reservations",
                "basket_target_order_events",
            )
        }
        if any(unsafe_counts.values()):
            raise RuntimeError("online_activation_execution_state_not_empty")
        authorizing_decisions = int(
            conn.execute(
                "SELECT COUNT(*) FROM auto_trade_decisions "
                "WHERE will_order<>0 OR decided_state IN "
                "('submitting','submitted','reconcile_required')"
            ).fetchone()[0]
        )
        uncovered_cycles = int(
            conn.execute(
                "SELECT COUNT(*) FROM basket_target_execution_cycles c "
                "LEFT JOIN auto_trade_decisions d ON d.cycle_id=c.cycle_id "
                "WHERE c.state IN "
                "('prepared','submitting','submitted','reconcile_required') "
                "AND (d.decision_id IS NULL OR d.decided_state<>'blocked' "
                "OR d.will_order<>0)"
            ).fetchone()[0]
        )
        if authorizing_decisions or uncovered_cycles:
            raise RuntimeError("online_activation_unblocked_execution_scope_present")
        return {
            "mode": "online_no_live_compare_and_swap",
            "current_generation": int(row[0]),
            "current_activation_sha256": str(row[3] or ""),
            "unsafe_execution_counts": unsafe_counts,
            "authorizing_decision_count": authorizing_decisions,
            "uncovered_cycle_count": uncovered_cycles,
            "legacy_authorizes_order_missing": (
                legacy_authorizes_order_missing
            ),
            "next_authorizes_order": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
        }


def apply_activation(
    *,
    db: Path,
    activation_path: Path,
    backup_path: Path,
    allow_online_no_live_cas: bool = False,
) -> dict[str, Any]:
    db_input = resolve_target_database(db)
    activation_input = activation_path.expanduser()
    backup_input = backup_path.expanduser()
    if db_input.is_symlink() or activation_input.is_symlink() or backup_input.is_symlink():
        raise RuntimeError("activation_path_symlink_forbidden")
    db = db_input
    activation_path = activation_input.resolve()
    backup_path = backup_input.resolve()
    if not db.is_file():
        raise RuntimeError("production_database_path_invalid")
    activation = load_target_activation(activation_path)
    if activation.get("execution_mode") != "production_no_live":
        raise RuntimeError("target_activation_not_enabled_no_live")
    if any(activation.get(key) is not False for key in NO_LIVE_FALSE_FIELDS):
        raise RuntimeError("target_activation_live_boundary_invalid")

    quiescence = (
        build_writer_quiescence_receipt(db_path=db)
        if not is_isolated_target_database(db)
        else isolated_target_writer_quiescence_receipt(db)
    )
    online_guard: dict[str, Any] | None = None
    if quiescence.get("quiescent") is not True:
        if not allow_online_no_live_cas:
            raise RuntimeError("writer_quiescence_not_proven")
        if not is_isolated_target_database(db):
            raise RuntimeError("online_activation_requires_isolated_target_database")
        online_guard = _online_no_live_cas_guard(
            db,
            next_activation=activation,
        )
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0) as conn:
        try:
            assert_target_database_owner(conn)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        status = production_ssot_schema_status(conn)
        if not status.get("ok") or not status.get("ledger_schema_ok"):
            raise RuntimeError("target_ssot_schema_not_ready")
        if "basket_strategy_activation_state" not in {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }:
            raise RuntimeError("basket_strategy_activation_table_missing")

    backup = _backup_database(db, backup_path)
    activated_at = _now()
    with sqlite3.connect(db, timeout=60.0, isolation_level=None) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        row = conn.execute(
            "SELECT generation,activation_sha256 "
            "FROM basket_strategy_activation_state WHERE singleton=1"
        ).fetchone()
        generation = int(row[0] or 0) + 1 if row else 1
        previous_generation = int(row[0] or 0) if row else 0
        previous_sha256 = str(row[1] or "") if row else ""
        payload = {**activation, "generation": generation, "activated_at": activated_at}
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """INSERT INTO basket_strategy_activation_state(
                     singleton,generation,state,strategy_id,activation_sha256,activation_json,updated_at
                   ) VALUES(1,?,'enabled',?,?,?,?)
                   ON CONFLICT(singleton) DO UPDATE SET
                     generation=excluded.generation,state='enabled',strategy_id=excluded.strategy_id,
                     activation_sha256=excluded.activation_sha256,activation_json=excluded.activation_json,
                     updated_at=excluded.updated_at
                   WHERE basket_strategy_activation_state.generation=?
                     AND basket_strategy_activation_state.activation_sha256=?""",
                (
                    generation,
                    str(activation["strategy_id"]),
                    str(activation["activation_contract_sha256"]),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    activated_at,
                    previous_generation,
                    previous_sha256,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("target_activation_compare_and_swap_failed")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    identity = {
        "schema": SCHEMA,
        "database": str(db),
        "activation_path": str(activation_path),
        "backup_path": str(backup_path),
        "backup": backup,
        "activation_contract_sha256": activation["activation_contract_sha256"],
        "generation": generation,
        "activated_at": activated_at,
        "execution_mode": "production_no_live",
        "live_decision_write_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "writes_orders": False,
        "orders_submitted": 0,
        "writer_quiescence": quiescence,
        "activation_update_mode": (
            "quiescent_writer" if online_guard is None
            else "online_no_live_compare_and_swap"
        ),
        "online_no_live_guard": online_guard,
    }
    return {**identity, "receipt_sha256": _sha(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--activation",
        type=Path,
        default=ROOT / "config/trading/bayes_basket_target_production_no_live_activation_v1.json",
    )
    parser.add_argument("--backup-db", type=Path, required=True)
    parser.add_argument(
        "--allow-online-no-live-cas",
        action="store_true",
        help=(
            "allow an active isolated target writer only after proving both "
            "activation generations are no-live and execution residue is empty"
        ),
    )
    parser.add_argument("--receipt", type=Path, default=ROOT / "output/audits/bayes_basket_target_no_live_activation_apply.json")
    args = parser.parse_args()
    result = apply_activation(
        db=args.db,
        activation_path=args.activation,
        backup_path=args.backup_db,
        allow_online_no_live_cas=args.allow_online_no_live_cas,
    )
    receipt = args.receipt.expanduser().resolve()
    write_bounded_json(receipt, result)
    print(json.dumps({"schema": SCHEMA, "generation": result["generation"], "receipt": str(receipt), "orders_submitted": 0}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
