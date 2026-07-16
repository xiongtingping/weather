"""Full Transaction-B writer for an explicitly isolated public-SSOT shadow DB.

The writer refuses a normal archive database.  It is for contract/fault
injection tests only until a separate accepted cutover authorizes production.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from sequential_bayes_shadow_writer import (
    ShadowTransactionIntegrityError,
    commit_shadow_physical_posterior_transaction,
    commit_shadow_transaction_b,
)


SCHEMA_VERSION = "sequential_bayes_public_ssot_shadow_writer_v1"
SHADOW_MANIFEST_TABLE = "v2_shadow_runtime_manifest"
SHADOW_MANIFEST_STATE = "isolated_shadow_only"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _parse_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(_text(value))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _runtime_dependency_identity_from_fact_json(value: Any) -> str:
    parsed = _parse_json(value)
    ssot = _mapping(parsed.get("posterior_ssot_fact"))
    lineage = _mapping(ssot.get("runtime_dependency_lineage"))
    return _text(lineage.get("resident_fast_lane_prewarm_dependency_identity"))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def build_isolated_public_ssot_shadow_boundary() -> dict[str, Any]:
    """Declare the non-production DB boundary for static/runtime audits.

    The sentinel is necessary but not merely documentary: every mutating
    entrypoint calls ``_assert_isolated_shadow_db`` before its first write.
    """

    return {
        "schema": "isolated_public_ssot_shadow_boundary_v1",
        "execution_mode": "isolated_shadow_only_temp_db_never_production_archive",
        "requires_manifest_table": SHADOW_MANIFEST_TABLE,
        "requires_manifest_state": SHADOW_MANIFEST_STATE,
        "writes_production_archive": False,
        "writes_production_auto_trade_decisions": False,
        "places_orders": False,
        "read_only": False,
    }


def initialize_isolated_public_ssot_shadow_schema(conn: sqlite3.Connection) -> None:
    """Create test-only public tables plus an explicit isolation sentinel."""

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SHADOW_MANIFEST_TABLE} (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            activation_state TEXT NOT NULL,
            writer_schema TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"INSERT OR IGNORE INTO {SHADOW_MANIFEST_TABLE}(singleton, activation_state, writer_schema) VALUES(1, ?, ?)",
        (SHADOW_MANIFEST_STATE, SCHEMA_VERSION),
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            composer_version TEXT NOT NULL,
            fact_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            fact_snapshot_id TEXT NOT NULL,
            posterior_run_id TEXT NOT NULL,
            UNIQUE(posterior_run_id, fact_snapshot_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_fact_latest (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            fact_snapshot_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            PRIMARY KEY(slug, event_date)
        )
        """
    )


def _assert_isolated_shadow_db(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ShadowTransactionIntegrityError("public_ssot_shadow_manifest_missing")
    row = conn.execute(
        f"SELECT activation_state, writer_schema FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE or _text(row[1]) != SCHEMA_VERSION:
        raise ShadowTransactionIntegrityError("public_ssot_shadow_manifest_invalid")
    if not _table_exists(conn, "event_fact_history") or not _table_exists(conn, "event_fact_latest"):
        raise ShadowTransactionIntegrityError("public_ssot_shadow_public_fact_tables_missing")


def materialize_public_posterior_fact_row(
    *,
    checkpoint_row: Mapping[str, Any],
    realization_state_row: Mapping[str, Any],
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
    resident_fast_lane_prewarm_dependency_identity: str = "",
) -> dict[str, Any]:
    """Build the one public fact payload from the same checkpoint/run/state."""

    checkpoint = _mapping(checkpoint_row)
    realization = _mapping(realization_state_row)
    required = (
        "posterior_checkpoint_id",
        "posterior_run_id",
        "resolution_contract_id",
        "joint_posterior_sha256",
        "physical_high_distribution_json",
        "settlement_scalar_distribution_json",
        "market_bucket_distribution_json",
        "committed_at",
    )
    missing = [key for key in required if not _text(checkpoint.get(key))]
    if missing:
        raise ValueError(f"public_ssot_checkpoint_fields_missing:{','.join(missing)}")
    if _text(realization.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")):
        raise ShadowTransactionIntegrityError("public_ssot_realization_run_binding_mismatch")
    if _text(realization.get("resolution_contract_id")) != _text(checkpoint.get("resolution_contract_id")):
        raise ShadowTransactionIntegrityError("public_ssot_realization_resolution_binding_mismatch")
    if not _text(realization.get("realization_state_id")) or not _text(realization.get("fact_snapshot_id")):
        raise ValueError("public_ssot_realization_identity_missing")
    if not _text(slug) or not _text(event_date) or not _text(fact_as_of) or not _text(correlation_id):
        raise ValueError("public_ssot_fact_identity_missing")
    runtime_dependency_identity = _text(resident_fast_lane_prewarm_dependency_identity)
    realization_payload = _parse_json(realization.get("state_json"))
    timing = _mapping(_mapping(realization_payload.get("projection")).get("peak_timing_state"))
    ssot = {
        "schema": "sequential_temperature_posterior_ssot_v2",
        "state": "available",
        "fact_snapshot_id": _text(realization.get("fact_snapshot_id")),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "realization_state_id": _text(realization.get("realization_state_id")),
        "resolution_contract_id": _text(checkpoint.get("resolution_contract_id")),
        "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
        "physical_high_distribution": _parse_json(checkpoint.get("physical_high_distribution_json")),
        "settlement_scalar_distribution": _parse_json(checkpoint.get("settlement_scalar_distribution_json")),
        "market_bucket_probabilities": _parse_json(checkpoint.get("market_bucket_distribution_json")),
        "last_canonical_event_id": _text(checkpoint.get("last_canonical_event_id")),
        "last_strong_update_id": _text(checkpoint.get("last_strong_update_id")),
        "observed_max_lower_bound_c": checkpoint.get("observed_max_lower_bound_c"),
        "committed_at": _text(checkpoint.get("committed_at")),
        "peak_timing_state": dict(timing) if timing else {
            "schema": "peak_timing_state_l3_v1",
            "ok": True,
            "state": "na",
            "na_reason": "peak_timing_state_not_materialized",
        },
        "shadow_only": True,
    }
    if runtime_dependency_identity:
        ssot["runtime_dependency_lineage"] = {
            "resident_fast_lane_prewarm_dependency_identity": runtime_dependency_identity,
        }
    return {
        "run_date": _text(event_date),
        "slug": _text(slug),
        "event_date": _text(event_date),
        "fact_as_of": _text(fact_as_of),
        "composer_version": "sequential_bayes_v2_shadow",
        "fact_json": _json({"posterior_ssot_fact": ssot, "shadow_only": True}),
        "created_at": _text(fact_as_of),
        "correlation_id": _text(correlation_id),
        "fact_snapshot_id": ssot["fact_snapshot_id"],
        "posterior_run_id": ssot["posterior_run_id"],
    }


def materialize_public_physical_posterior_fact_row(
    *,
    checkpoint_row: Mapping[str, Any],
    peak_timing_state: Mapping[str, Any],
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
    market_bucket_probabilities_na_reason: str,
    resident_fast_lane_prewarm_dependency_identity: str = "",
) -> dict[str, Any]:
    """Build the public physical-X SSOT when the market bridge is unavailable.

    A missing X→S→B bridge removes only settlement/market projection and all
    trade outputs.  It must never hide an otherwise valid physical posterior
    checkpoint from the unique public probability SSOT.
    """

    checkpoint = _mapping(checkpoint_row)
    timing = _mapping(peak_timing_state)
    required = (
        "posterior_checkpoint_id",
        "posterior_run_id",
        "resolution_contract_id",
        "joint_posterior_sha256",
        "physical_high_distribution_json",
        "committed_at",
    )
    missing = [key for key in required if not _text(checkpoint.get(key))]
    if missing:
        raise ValueError(f"public_physical_ssot_checkpoint_fields_missing:{','.join(missing)}")
    if (
        _text(timing.get("schema")) != "peak_timing_state_l3_v1"
        or timing.get("ok") is not True
        or _text(timing.get("state")) not in {"available", "na"}
        or _text(timing.get("canonical_observation_event_id"))
        != _text(checkpoint.get("last_canonical_event_id"))
        or _text(timing.get("strong_observation_update_id"))
        != _text(checkpoint.get("last_strong_update_id"))
    ):
        raise ValueError("public_physical_ssot_peak_timing_contract_invalid")
    if not all(_text(value) for value in (fact_snapshot_id, slug, event_date, fact_as_of, correlation_id)):
        raise ValueError("public_physical_ssot_fact_identity_missing")
    na_reason = _text(market_bucket_probabilities_na_reason)
    if not na_reason:
        raise ValueError("public_physical_ssot_market_projection_na_reason_missing")
    runtime_dependency_identity = _text(resident_fast_lane_prewarm_dependency_identity)
    ssot = {
        "schema": "sequential_temperature_posterior_ssot_v2",
        "state": "physical_only_market_projection_na",
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "realization_state_id": "",
        "realization_state_na_reason": "market_bucket_projection_unavailable",
        "resolution_contract_id": _text(checkpoint.get("resolution_contract_id")),
        "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
        "physical_high_distribution": _parse_json(checkpoint.get("physical_high_distribution_json")),
        "settlement_scalar_distribution": [],
        "settlement_scalar_distribution_na_reason": na_reason,
        "market_bucket_probabilities": {},
        "market_bucket_probabilities_na_reason": na_reason,
        "last_canonical_event_id": _text(checkpoint.get("last_canonical_event_id")),
        "last_strong_update_id": _text(checkpoint.get("last_strong_update_id")),
        "observed_max_lower_bound_c": checkpoint.get("observed_max_lower_bound_c"),
        "committed_at": _text(checkpoint.get("committed_at")),
        "peak_timing_state": dict(timing),
        "shadow_only": True,
    }
    if runtime_dependency_identity:
        ssot["runtime_dependency_lineage"] = {
            "resident_fast_lane_prewarm_dependency_identity": runtime_dependency_identity,
        }
    return {
        "run_date": _text(event_date),
        "slug": _text(slug),
        "event_date": _text(event_date),
        "fact_as_of": _text(fact_as_of),
        "composer_version": "sequential_bayes_v2_shadow",
        "fact_json": _json({"posterior_ssot_fact": ssot, "shadow_only": True}),
        "created_at": _text(fact_as_of),
        "correlation_id": _text(correlation_id),
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": ssot["posterior_run_id"],
    }


def _public_fact_existing(conn: sqlite3.Connection, public_row: Mapping[str, Any]) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT id, slug, event_date, fact_snapshot_id, posterior_run_id, correlation_id, fact_json
        FROM event_fact_history
        WHERE posterior_run_id=? AND fact_snapshot_id=?
        LIMIT 1
        """,
        (_text(public_row.get("posterior_run_id")), _text(public_row.get("fact_snapshot_id"))),
    ).fetchone()


def commit_public_ssot_shadow_transaction_b(
    conn: sqlite3.Connection,
    *,
    expected_previous_checkpoint_id: str,
    expected_previous_posterior_run_id: str,
    strong_update_row: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    realization_state_row: Mapping[str, Any],
    outbox_row: Mapping[str, Any],
    outbox_execution_contract_row: Mapping[str, Any],
    public_fact_row: Mapping[str, Any],
    fault_after: str = "",
) -> dict[str, Any]:
    """Atomically write P1 rows, public shadow fact/latest pointer, and outbox.

    ``fault_after`` exists solely for test fault injection and accepts
    ``p1``/``history``/``latest``.  It must never be wired to production.
    """

    _assert_isolated_shadow_db(conn)
    public_row = _mapping(public_fact_row)
    if _text(public_row.get("posterior_run_id")) != _text(checkpoint_row.get("posterior_run_id")):
        raise ShadowTransactionIntegrityError("public_ssot_fact_checkpoint_run_binding_mismatch")
    if _text(public_row.get("fact_snapshot_id")) != _text(realization_state_row.get("fact_snapshot_id")):
        raise ShadowTransactionIntegrityError("public_ssot_fact_realization_snapshot_binding_mismatch")
    existing = _public_fact_existing(conn, public_row)
    if existing is not None:
        latest = conn.execute(
            "SELECT fact_id, fact_snapshot_id FROM event_fact_latest WHERE slug=? AND event_date=?",
            (_text(public_row.get("slug")), _text(public_row.get("event_date"))),
        ).fetchone()
        if latest is None or int(latest[0]) != int(existing["id"]) or _text(latest[1]) != _text(public_row.get("fact_snapshot_id")):
            raise ShadowTransactionIntegrityError("public_ssot_existing_fact_latest_pointer_inconsistent")
        existing_dependency_identity = _runtime_dependency_identity_from_fact_json(existing["fact_json"])
        requested_dependency_identity = _runtime_dependency_identity_from_fact_json(public_row.get("fact_json"))
        if existing_dependency_identity != requested_dependency_identity:
            raise ShadowTransactionIntegrityError("public_ssot_existing_fact_runtime_dependency_binding_mismatch")
    conn.execute("SAVEPOINT sequential_bayes_public_ssot_tx_b")
    try:
        p1 = commit_shadow_transaction_b(
            conn,
            expected_previous_checkpoint_id=expected_previous_checkpoint_id,
            expected_previous_posterior_run_id=expected_previous_posterior_run_id,
            strong_update_row=strong_update_row,
            checkpoint_row=checkpoint_row,
            realization_state_row=realization_state_row,
            outbox_row=outbox_row,
            outbox_execution_contract_row=outbox_execution_contract_row,
        )
        if fault_after == "p1":
            raise RuntimeError("public_ssot_fault_after_p1")
        if existing is None:
            fields = list(public_row)
            cur = conn.execute(
                f"INSERT INTO event_fact_history({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                tuple(public_row[field] for field in fields),
            )
            fact_id = int(cur.lastrowid)
        else:
            fact_id = int(existing["id"])
        if fault_after == "history":
            raise RuntimeError("public_ssot_fault_after_history")
        conn.execute(
            """
            INSERT INTO event_fact_latest(slug,event_date,fact_id,updated_at,fact_snapshot_id,correlation_id)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(slug,event_date) DO UPDATE SET
              fact_id=excluded.fact_id,
              updated_at=excluded.updated_at,
              fact_snapshot_id=excluded.fact_snapshot_id,
              correlation_id=excluded.correlation_id
            """,
            (
                _text(public_row.get("slug")),
                _text(public_row.get("event_date")),
                fact_id,
                _text(public_row.get("fact_as_of")),
                _text(public_row.get("fact_snapshot_id")),
                _text(public_row.get("correlation_id")),
            ),
        )
        if fault_after == "latest":
            raise RuntimeError("public_ssot_fault_after_latest")
    except Exception:
        conn.execute("ROLLBACK TO sequential_bayes_public_ssot_tx_b")
        conn.execute("RELEASE sequential_bayes_public_ssot_tx_b")
        raise
    conn.execute("RELEASE sequential_bayes_public_ssot_tx_b")
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "idempotent": bool(existing is not None and p1.get("idempotent")),
        "shadow_only": True,
        "fact_id": fact_id,
        "posterior_run_id": _text(public_row.get("posterior_run_id")),
        "fact_snapshot_id": _text(public_row.get("fact_snapshot_id")),
        "correlation_id": _text(public_row.get("correlation_id")),
    }


def commit_public_physical_ssot_shadow_transaction(
    conn: sqlite3.Connection,
    *,
    expected_previous_checkpoint_id: str,
    expected_previous_posterior_run_id: str,
    strong_update_row: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    public_fact_row: Mapping[str, Any],
    fault_after: str = "",
) -> dict[str, Any]:
    """Atomically publish X-only posterior SSOT without trade-side rows."""

    _assert_isolated_shadow_db(conn)
    public_row = _mapping(public_fact_row)
    if _text(public_row.get("posterior_run_id")) != _text(checkpoint_row.get("posterior_run_id")):
        raise ShadowTransactionIntegrityError("public_physical_ssot_fact_checkpoint_run_binding_mismatch")
    existing = _public_fact_existing(conn, public_row)
    if existing is not None:
        latest = conn.execute(
            "SELECT fact_id, fact_snapshot_id FROM event_fact_latest WHERE slug=? AND event_date=?",
            (_text(public_row.get("slug")), _text(public_row.get("event_date"))),
        ).fetchone()
        if latest is None or int(latest[0]) != int(existing["id"]) or _text(latest[1]) != _text(public_row.get("fact_snapshot_id")):
            raise ShadowTransactionIntegrityError("public_physical_ssot_existing_fact_latest_pointer_inconsistent")
        if _runtime_dependency_identity_from_fact_json(existing["fact_json"]) != _runtime_dependency_identity_from_fact_json(public_row.get("fact_json")):
            raise ShadowTransactionIntegrityError("public_physical_ssot_existing_runtime_dependency_binding_mismatch")
    conn.execute("SAVEPOINT sequential_bayes_public_physical_ssot")
    try:
        p1 = commit_shadow_physical_posterior_transaction(
            conn,
            expected_previous_checkpoint_id=expected_previous_checkpoint_id,
            expected_previous_posterior_run_id=expected_previous_posterior_run_id,
            strong_update_row=strong_update_row,
            checkpoint_row=checkpoint_row,
        )
        if fault_after == "p1":
            raise RuntimeError("public_physical_ssot_fault_after_p1")
        if existing is None:
            fields = list(public_row)
            cur = conn.execute(
                f"INSERT INTO event_fact_history({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                tuple(public_row[field] for field in fields),
            )
            fact_id = int(cur.lastrowid)
        else:
            fact_id = int(existing["id"])
        if fault_after == "history":
            raise RuntimeError("public_physical_ssot_fault_after_history")
        conn.execute(
            """
            INSERT INTO event_fact_latest(slug,event_date,fact_id,updated_at,fact_snapshot_id,correlation_id)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(slug,event_date) DO UPDATE SET
              fact_id=excluded.fact_id,
              updated_at=excluded.updated_at,
              fact_snapshot_id=excluded.fact_snapshot_id,
              correlation_id=excluded.correlation_id
            """,
            (
                _text(public_row.get("slug")),
                _text(public_row.get("event_date")),
                fact_id,
                _text(public_row.get("fact_as_of")),
                _text(public_row.get("fact_snapshot_id")),
                _text(public_row.get("correlation_id")),
            ),
        )
        if fault_after == "latest":
            raise RuntimeError("public_physical_ssot_fault_after_latest")
    except Exception:
        conn.execute("ROLLBACK TO sequential_bayes_public_physical_ssot")
        conn.execute("RELEASE sequential_bayes_public_physical_ssot")
        raise
    conn.execute("RELEASE sequential_bayes_public_physical_ssot")
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "idempotent": bool(existing is not None and p1.get("idempotent")),
        "shadow_only": True,
        "physical_only": True,
        "fact_id": fact_id,
        "posterior_run_id": _text(public_row.get("posterior_run_id")),
        "fact_snapshot_id": _text(public_row.get("fact_snapshot_id")),
        "correlation_id": _text(public_row.get("correlation_id")),
    }


__all__ = [
    "SCHEMA_VERSION",
    "build_isolated_public_ssot_shadow_boundary",
    "commit_public_physical_ssot_shadow_transaction",
    "commit_public_ssot_shadow_transaction_b",
    "initialize_isolated_public_ssot_shadow_schema",
    "materialize_public_physical_posterior_fact_row",
    "materialize_public_posterior_fact_row",
]
