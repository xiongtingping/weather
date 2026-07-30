"""Controlled target-only capital epoch migration for production-no-live.

The runtime budget and the atomic ledger must describe the same target-owned
capital envelope.  This module is deliberately maintenance-only: it performs
no network I/O, never reads shared live capital, and refuses to replace a
non-empty or execution-bound epoch.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .activation import assert_target_database_owner
from .contracts import canonical_json, content_sha256
from .runtime_config import load_target_no_live_runtime_config


SCHEMA = "bayes_basket_target_no_live_capital_epoch_migration_v1"
ZERO_RESERVATION_RELEASE_REASON = "no_live_zero_reservation_epoch_migration"
MIGRATION_DDL = """
CREATE TABLE IF NOT EXISTS basket_target_capital_epoch_migrations (
  migration_id TEXT PRIMARY KEY,
  schema TEXT NOT NULL,
  previous_epoch_id TEXT NOT NULL,
  next_epoch_id TEXT NOT NULL UNIQUE,
  previous_balance_usdc REAL NOT NULL CHECK(previous_balance_usdc>=0),
  next_balance_usdc REAL NOT NULL CHECK(next_balance_usdc>0),
  previous_version INTEGER NOT NULL CHECK(previous_version>=0),
  next_version INTEGER NOT NULL CHECK(next_version=previous_version+1),
  zero_reservations_released INTEGER NOT NULL
    CHECK(zero_reservations_released>=0),
  runtime_contract_sha256 TEXT NOT NULL
    CHECK(length(runtime_contract_sha256)=64),
  migration_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  created_at TEXT NOT NULL
);
"""


class NoLiveCapitalEpochError(RuntimeError):
    """Raised when a capital epoch cannot be migrated without ambiguity."""


def _timestamp(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NoLiveCapitalEpochError("capital_epoch_migration_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NoLiveCapitalEpochError("capital_epoch_migration_timestamp_timezone_required")
    return text


def target_epoch_identity(runtime: Mapping[str, Any]) -> dict[str, Any]:
    """Return the deterministic epoch derived only from the validated contract."""

    identity = {
        "schema": str(runtime["schema"]),
        "state": str(runtime["state"]),
        "execution_mode": str(runtime["execution_mode"]),
        "target_only": runtime["target_only"] is True,
        "capital_scope": str(runtime["capital_scope"]),
        "target_remaining_budget_usdc": round(
            float(runtime["target_remaining_budget_usdc"]), 8
        ),
        "live_submit_authorized": runtime["live_submit_authorized"] is True,
        "places_orders": runtime["places_orders"] is True,
        "writes_production_db": runtime["writes_production_db"] is True,
        "calls_order_gateway": runtime["calls_order_gateway"] is True,
        "reads_shared_live_capital": runtime["reads_shared_live_capital"] is True,
    }
    contract_sha256 = content_sha256(identity)
    return {
        **identity,
        "runtime_contract_sha256": contract_sha256,
        "capital_epoch_id": f"target-no-live-{contract_sha256[:24]}",
    }


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        raise NoLiveCapitalEpochError(
            f"capital_epoch_migration_required_table_missing:{table}"
        )
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _verified_prior_target_epoch(
    conn: sqlite3.Connection,
    *,
    epoch_id: str,
    balance_usdc: float,
) -> bool:
    row = conn.execute(
        "SELECT migration_id,migration_json,content_sha256 "
        "FROM basket_target_capital_epoch_migrations "
        "WHERE next_epoch_id=?",
        (epoch_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        identity = json.loads(str(row[1]))
    except (TypeError, ValueError):
        return False
    return (
        str(identity.get("next_epoch_id") or "") == epoch_id
        and abs(
            float(identity.get("next_balance_usdc", -1.0))
            - balance_usdc
        )
        <= 1e-9
        and content_sha256(identity) == str(row[2])
    )


def migrate_target_no_live_capital_epoch(
    conn: sqlite3.Connection,
    *,
    runtime_config_path: Path | str,
    migrated_at_utc: str,
) -> dict[str, Any]:
    """CAS-migrate an empty bootstrap pool to the target runtime budget.

    A replay against the already-selected epoch is strictly zero-write even
    after a no-live candidate has reserved capital.  Any first-time migration
    requires a completely execution-empty ledger and zero reserved capital.
    """

    if conn.in_transaction:
        raise NoLiveCapitalEpochError(
            "capital_epoch_migration_requires_outer_transaction_absent"
        )
    migrated_at = _timestamp(migrated_at_utc)
    runtime = load_target_no_live_runtime_config(runtime_config_path)
    target = target_epoch_identity(runtime)
    target_balance = float(target["target_remaining_budget_usdc"])
    if target_balance <= 0.0:
        raise NoLiveCapitalEpochError(
            "capital_epoch_migration_positive_target_budget_required"
        )
    assert_target_database_owner(conn)
    attached = {
        str(row[1])
        for row in conn.execute("PRAGMA database_list").fetchall()
        if str(row[1]) not in {"main", "temp"}
    }
    if attached:
        raise NoLiveCapitalEpochError(
            "capital_epoch_migration_attached_database_forbidden"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(MIGRATION_DDL)
        row = conn.execute(
            "SELECT balance_usdc,reserved_usdc,capital_epoch_id,version "
            "FROM basket_target_capital_pool WHERE pool_id=1"
        ).fetchone()
        if row is None:
            raise NoLiveCapitalEpochError("capital_epoch_migration_pool_missing")
        before = {
            "balance_usdc": float(row[0]),
            "reserved_usdc": float(row[1]),
            "capital_epoch_id": str(row[2]),
            "version": int(row[3]),
        }
        if (
            abs(before["balance_usdc"] - target_balance) <= 1e-9
            and before["capital_epoch_id"] == target["capital_epoch_id"]
        ):
            receipt = conn.execute(
                "SELECT migration_id,migration_json,content_sha256 "
                "FROM basket_target_capital_epoch_migrations "
                "WHERE next_epoch_id=?",
                (target["capital_epoch_id"],),
            ).fetchone()
            if receipt is None:
                raise NoLiveCapitalEpochError(
                    "capital_epoch_migration_receipt_missing"
                )
            try:
                receipt_identity = json.loads(str(receipt[1]))
            except (TypeError, ValueError) as exc:
                raise NoLiveCapitalEpochError(
                    "capital_epoch_migration_receipt_invalid"
                ) from exc
            if (
                str(receipt[0])
                != f"capital_epoch_{target['runtime_contract_sha256'][:40]}"
                or content_sha256(receipt_identity) != str(receipt[2])
                or receipt_identity.get("next_epoch_id")
                != target["capital_epoch_id"]
                or receipt_identity.get("runtime_contract_sha256")
                != target["runtime_contract_sha256"]
            ):
                raise NoLiveCapitalEpochError(
                    "capital_epoch_migration_receipt_drift"
                )
            conn.rollback()
            return {
                "schema": SCHEMA,
                "state": "UNCHANGED_ZERO_WRITE",
                "idempotent": True,
                "before": before,
                "after": before,
                "runtime_contract_sha256": target["runtime_contract_sha256"],
                "target_owned_only": True,
                "reads_shared_live_capital": False,
                "calls_order_gateway": False,
                "orders_submitted": 0,
                "rows_written": 0,
                "migration_id": str(receipt[0]),
                "migration_content_sha256": str(receipt[2]),
            }

        if abs(before["reserved_usdc"]) > 1e-9:
            raise NoLiveCapitalEpochError(
                "capital_epoch_migration_reserved_capital_present"
            )
        if (
            abs(before["balance_usdc"]) > 1e-9
            and not _verified_prior_target_epoch(
                conn,
                epoch_id=before["capital_epoch_id"],
                balance_usdc=before["balance_usdc"],
            )
        ):
            raise NoLiveCapitalEpochError(
                "capital_epoch_migration_nonzero_source_balance"
            )
        positive_active = int(
            conn.execute(
                "SELECT COUNT(*) FROM basket_target_capital_reservations "
                "WHERE state='active' AND amount_usdc>0"
            ).fetchone()[0]
        )
        if positive_active:
            raise NoLiveCapitalEpochError(
                "capital_epoch_migration_positive_active_reservation_present"
            )
        # Historical no-live candidates and their cancelled intents are
        # audit evidence, not executable residue.  A rollover is allowed only
        # when every intent is terminal-cancelled and all actual execution
        # surfaces remain empty.
        execution_counts = {
            "non_cancelled_order_intents": int(conn.execute(
                "SELECT COUNT(*) FROM basket_target_order_intents "
                "WHERE state<>'cancelled'"
            ).fetchone()[0]),
            "order_events": _table_count(
                conn, "basket_target_order_events"
            ),
            "execution_receipts": _table_count(
                conn, "basket_target_execution_receipts"
            ),
            "execution_transition_versions": _table_count(
                conn, "basket_target_execution_transition_versions"
            ),
            "post_fill_recompute_requests": _table_count(
                conn, "basket_target_post_fill_recompute_requests"
            ),
            "non_quiescent_cycles": int(conn.execute(
                "SELECT COUNT(*) FROM basket_target_execution_cycles "
                "WHERE state IN "
                "('submitting','submitted','reconcile_required')"
            ).fetchone()[0]),
        }
        if any(execution_counts.values()):
            raise NoLiveCapitalEpochError(
                "capital_epoch_migration_execution_residue_present"
            )
        zero_active = int(
            conn.execute(
                "SELECT COUNT(*) FROM basket_target_capital_reservations "
                "WHERE state='active' AND amount_usdc=0"
            ).fetchone()[0]
        )
        released = conn.execute(
            "UPDATE basket_target_capital_reservations "
            "SET state='released',release_reason=?,updated_at=? "
            "WHERE state='active' AND amount_usdc=0",
            (ZERO_RESERVATION_RELEASE_REASON, migrated_at),
        ).rowcount
        if released != zero_active:
            raise NoLiveCapitalEpochError(
                "capital_epoch_migration_zero_reservation_cas_failed"
            )
        updated = conn.execute(
            "UPDATE basket_target_capital_pool SET "
            "balance_usdc=?,capital_epoch_id=?,version=version+1,updated_at=? "
            "WHERE pool_id=1 AND version=? AND balance_usdc=? AND reserved_usdc=0 "
            "AND capital_epoch_id=?",
            (
                target_balance,
                target["capital_epoch_id"],
                migrated_at,
                before["version"],
                before["balance_usdc"],
                before["capital_epoch_id"],
            ),
        )
        if updated.rowcount != 1:
            raise NoLiveCapitalEpochError(
                "capital_epoch_migration_pool_cas_failed"
            )
        after_row = conn.execute(
            "SELECT balance_usdc,reserved_usdc,capital_epoch_id,version "
            "FROM basket_target_capital_pool WHERE pool_id=1"
        ).fetchone()
        after = {
            "balance_usdc": float(after_row[0]),
            "reserved_usdc": float(after_row[1]),
            "capital_epoch_id": str(after_row[2]),
            "version": int(after_row[3]),
        }
        receipt_identity = {
            "schema": SCHEMA,
            "previous_epoch_id": before["capital_epoch_id"],
            "next_epoch_id": after["capital_epoch_id"],
            "previous_balance_usdc": before["balance_usdc"],
            "next_balance_usdc": after["balance_usdc"],
            "previous_version": before["version"],
            "next_version": after["version"],
            "zero_reservations_released": released,
            "runtime_contract_sha256": target["runtime_contract_sha256"],
            "execution_counts": execution_counts,
            "target_owned_only": True,
            "reads_shared_live_capital": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
        migration_id = (
            f"capital_epoch_{target['runtime_contract_sha256'][:40]}"
        )
        migration_sha256 = content_sha256(receipt_identity)
        conn.execute(
            "INSERT INTO basket_target_capital_epoch_migrations("
            "migration_id,schema,previous_epoch_id,next_epoch_id,"
            "previous_balance_usdc,next_balance_usdc,previous_version,"
            "next_version,zero_reservations_released,runtime_contract_sha256,"
            "migration_json,content_sha256,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                migration_id,
                SCHEMA,
                before["capital_epoch_id"],
                after["capital_epoch_id"],
                before["balance_usdc"],
                after["balance_usdc"],
                before["version"],
                after["version"],
                released,
                target["runtime_contract_sha256"],
                canonical_json(receipt_identity),
                migration_sha256,
                migrated_at,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "schema": SCHEMA,
        "state": "COMMITTED",
        "idempotent": False,
        "before": before,
        "after": after,
        "runtime_contract_sha256": target["runtime_contract_sha256"],
        "execution_counts": execution_counts,
        "zero_active_reservations_released": released,
        "target_owned_only": True,
        "reads_shared_live_capital": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "rows_written": released + 2,
        "migration_id": migration_id,
        "migration_content_sha256": migration_sha256,
    }


__all__ = [
    "NoLiveCapitalEpochError",
    "SCHEMA",
    "ZERO_RESERVATION_RELEASE_REASON",
    "migrate_target_no_live_capital_epoch",
    "target_epoch_identity",
]
