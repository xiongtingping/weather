"""Append-only proof that a materializer invocation committed one posterior fact.

The posterior fact itself is immutable.  Commit timing therefore lives in this
separate ledger, written only after SQLite confirms the fact transaction has
committed.  A missing ledger row is intentionally a missing T3 proof, never a
timestamp inferred from a queue row or a later UI rematerialization.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping


SCHEMA_VERSION = "posterior_materialization_commit_attestation_v1"
TABLE_NAME = "posterior_materialization_commit_attestations"
WRITE_OWNER = "posterior_materialization_attestation_runtime"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def ensure_posterior_materialization_attestation_table(conn: sqlite3.Connection) -> None:
    """Install the additive append-only ledger on a writable archive database."""

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            attestation_id TEXT PRIMARY KEY,
            materialized_fact_id INTEGER NOT NULL UNIQUE,
            task_queue_id INTEGER,
            source_fact_id INTEGER,
            source_received_at TEXT,
            posterior_apply_started_at TEXT,
            posterior_committed_at TEXT NOT NULL,
            fact_snapshot_id TEXT NOT NULL,
            posterior_run_id TEXT NOT NULL,
            timing_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_{TABLE_NAME}_no_update
        BEFORE UPDATE ON {TABLE_NAME}
        BEGIN SELECT RAISE(ABORT, 'posterior_materialization_commit_attestations_append_only'); END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_{TABLE_NAME}_no_delete
        BEFORE DELETE ON {TABLE_NAME}
        BEGIN SELECT RAISE(ABORT, 'posterior_materialization_commit_attestations_append_only'); END
        """
    )


def append_posterior_materialization_commit_attestation(
    conn: sqlite3.Connection,
    *,
    materialized_fact_id: Any,
    task_queue_id: Any = None,
    source_fact_id: Any = None,
    source_received_at: Any = "",
    posterior_apply_started_at: Any = "",
    posterior_committed_at: Any = "",
    fact_snapshot_id: Any = "",
    posterior_run_id: Any = "",
) -> dict[str, Any]:
    """Append one exact materializer-to-fact commit binding and commit it.

    The caller supplies ``posterior_committed_at`` only after the fact writer's
    commit returned.  Retried invocations are idempotent only if every bound
    value is identical; a conflicting replay is rejected instead of overwriting
    the audit record.
    """

    fact_id = _optional_positive_int(materialized_fact_id)
    committed_at = str(posterior_committed_at or "").strip()
    if fact_id is None or not committed_at:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "materialized_fact_id_or_posterior_committed_at_missing",
        }
    timing_payload = {
        "materialized_fact_id": fact_id,
        "task_queue_id": _optional_positive_int(task_queue_id),
        "source_fact_id": _optional_positive_int(source_fact_id),
        "source_received_at": str(source_received_at or "").strip(),
        "posterior_apply_started_at": str(posterior_apply_started_at or "").strip(),
        "posterior_committed_at": committed_at,
        "fact_snapshot_id": str(fact_snapshot_id or "").strip(),
        "posterior_run_id": str(posterior_run_id or "").strip(),
    }
    timing_sha256 = _sha256(timing_payload)
    attestation_id = f"posterior-materialization:{fact_id}"
    ensure_posterior_materialization_attestation_table(conn)
    existing = conn.execute(
        f"SELECT timing_sha256 FROM {TABLE_NAME} WHERE materialized_fact_id=? LIMIT 1",
        (fact_id,),
    ).fetchone()
    if existing is not None:
        existing_hash = str(existing[0] or "")
        if existing_hash == timing_sha256:
            return {
                "schema": SCHEMA_VERSION,
                "ok": True,
                "state": "already_attested",
                "attestation_id": attestation_id,
                "materialized_fact_id": fact_id,
                "posterior_committed_at": committed_at,
                "timing_sha256": timing_sha256,
            }
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "materialized_fact_commit_attestation_conflict",
            "attestation_id": attestation_id,
            "materialized_fact_id": fact_id,
        }
    created_at = _now_utc()
    conn.execute(
        f"""
        INSERT INTO {TABLE_NAME}(
            attestation_id, materialized_fact_id, task_queue_id, source_fact_id,
            source_received_at, posterior_apply_started_at, posterior_committed_at,
            fact_snapshot_id, posterior_run_id, timing_sha256, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attestation_id,
            fact_id,
            timing_payload["task_queue_id"],
            timing_payload["source_fact_id"],
            timing_payload["source_received_at"],
            timing_payload["posterior_apply_started_at"],
            committed_at,
            timing_payload["fact_snapshot_id"],
            timing_payload["posterior_run_id"],
            timing_sha256,
            created_at,
        ),
    )
    conn.commit()
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "appended",
        "attestation_id": attestation_id,
        "materialized_fact_id": fact_id,
        "posterior_committed_at": committed_at,
        "timing_sha256": timing_sha256,
        "created_at": created_at,
    }


__all__ = [
    "SCHEMA_VERSION",
    "TABLE_NAME",
    "append_posterior_materialization_commit_attestation",
    "ensure_posterior_materialization_attestation_table",
]
