"""Authoritative no-live execution-receipt transition runtime.

This is the only target-lane boundary that consumes execution receipts.  It is
deliberately a local SQLite transition engine: there is no gateway client, no
network path, and no live-submit escape hatch.  A receipt is accepted only
when its order scope, exact execution version vector, official WS sequence
and quote clocks all match the immutable intent evidence.

The existing ``basket_target_order_events`` table remains the low-level event
ledger.  The tables created here are the formal receipt/transition contract:
they keep bounded, content-addressed receipt history, one current transition
version per intent, and one post-fill recompute request per authoritative
terminal Maker fill (partial or full) or full FOK fill.  They are created
lazily so an isolated no-live rehearsal can use the
same production contract without mutating the shared schema migration.
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

from .contracts import ContractError, canonical_json, content_sha256
from .execution_reconciliation import (
    EVENT_TYPES,
    ReconciliationError,
    _intent_row,
    _schema_guard,
    _stored_vector,
    _text,
    _timestamp,
)
from .execution_version_vector import VERSION_FIELDS, build_execution_version_vector
from .resource_bounds import (
    MAX_JSON_INPUT_BYTES,
    assert_connection_target_capacity,
    assert_process_memory_budget,
)


SCHEMA = "bayes_basket_target_execution_transition_v1"
RECEIPT_SCHEMA = "bayes_basket_target_execution_receipt_v1"
SCOPE_SCHEMA = "bayes_basket_target_execution_scope_v1"
RECOMPUTE_SCHEMA = "bayes_basket_target_post_fill_recompute_request_v1"
RECOMPUTE_RESULT_SCHEMA = "bayes_basket_target_post_fill_recompute_result_v1"
MAX_RECEIPT_BYTES = min(MAX_JSON_INPUT_BYTES, 2 * 1024 * 1024)
MAX_RECOMPUTE_RESULT_BYTES = min(MAX_JSON_INPUT_BYTES, 512 * 1024)
EPS = 1e-9
ACTIVE_CYCLE_STATES = frozenset({"prepared", "submitting", "submitted", "reconcile_required"})

RECEIPT_TYPES = frozenset(
    {
        "POST_ACK",
        "CANCEL_REQUESTED",
        "CANCEL_CONFIRMED",
        "FOK_SUBMITTED",
        "FOK_FULL_FILL",
        "MAKER_FULL_FILL",
        "FOK_PARTIAL_FILL",
        "MAKER_PARTIAL_FILL",
        "FOK_UNKNOWN",
        "POST_UNKNOWN",
        "LATE_FILL",
        "CANCEL_FILL_RACE",
        "FOK_FAILED",
    }
)
AUTHORITATIVE_TYPES = frozenset(
    {
        "CANCEL_CONFIRMED",
        "FOK_FULL_FILL",
        "MAKER_FULL_FILL",
        "MAKER_PARTIAL_FILL",
    }
)
FULL_FILL_TYPES = frozenset({"FOK_FULL_FILL", "MAKER_FULL_FILL"})
RECOMPUTE_FILL_STATES = frozenset({"FULL_FILLED", "PARTIAL_FILLED"})
RECONCILE_TYPES = frozenset(
    {
        "FOK_PARTIAL_FILL",
        "FOK_UNKNOWN",
        "POST_UNKNOWN",
        "LATE_FILL",
        "CANCEL_FILL_RACE",
        "FOK_FAILED",
    }
)
MAKER_RECEIPT_TYPES = frozenset(
    {
        "POST_ACK",
        "CANCEL_REQUESTED",
        "CANCEL_CONFIRMED",
        "MAKER_FULL_FILL",
        "MAKER_PARTIAL_FILL",
        "POST_UNKNOWN",
        "LATE_FILL",
        "CANCEL_FILL_RACE",
    }
)
FOK_RECEIPT_TYPES = frozenset(
    {
        "FOK_SUBMITTED",
        "FOK_FULL_FILL",
        "FOK_PARTIAL_FILL",
        "FOK_UNKNOWN",
        "FOK_FAILED",
    }
)
_EVENT_TYPE_MAP = {
    "POST_ACK": "POST_ACK",
    "CANCEL_REQUESTED": "POST_ACK",
    "CANCEL_CONFIRMED": "CANCEL_ACK",
    "FOK_SUBMITTED": "POST_ACK",
    "FOK_FULL_FILL": "FILL",
    "MAKER_FULL_FILL": "FILL",
    "FOK_PARTIAL_FILL": "PARTIAL_FILL",
    "MAKER_PARTIAL_FILL": "PARTIAL_FILL",
    "FOK_UNKNOWN": "UNKNOWN_POST",
    "POST_UNKNOWN": "UNKNOWN_POST",
    "LATE_FILL": "LATE_FILL",
    "CANCEL_FILL_RACE": "LATE_FILL",
    "FOK_FAILED": "UNKNOWN_POST",
}


def _same_instant(left: Any, right: Any) -> bool:
    """Compare timezone-aware ISO timestamps by instant, not text precision."""

    try:
        left_dt = datetime.fromisoformat(str(left).replace("Z", "+00:00"))
        right_dt = datetime.fromisoformat(str(right).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionTransitionError(
            "execution_receipt_quote_timestamp_invalid"
        ) from exc
    if (
        left_dt.tzinfo is None
        or left_dt.utcoffset() is None
        or right_dt.tzinfo is None
        or right_dt.utcoffset() is None
    ):
        raise ExecutionTransitionError(
            "execution_receipt_quote_timestamp_timezone_required"
        )
    return left_dt == right_dt


class ExecutionTransitionError(ReconciliationError):
    """Raised when a formal receipt cannot safely advance the state machine."""


def _table_sql(conn: sqlite3.Connection, table: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return None if row is None else str(row[0] or "")


def _execute_ddl_script(
    conn: sqlite3.Connection,
    script: str,
) -> None:
    """Execute complete DDL statements without sqlite3.executescript commits."""

    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise ExecutionTransitionError(
            "execution_transition_schema_statement_incomplete"
        )


def _migrate_recompute_request_table(conn: sqlite3.Connection) -> bool:
    """Move the original PENDING-only table forward without dropping its rows.

    SQLite cannot alter a CHECK constraint in place.  The old table is retained
    under a legacy name, then its rows are copied into the bounded v2 table.
    Keeping the legacy table makes the migration recoverable and preserves
    every historical request even if a process is interrupted between steps.
    """

    table = "basket_target_post_fill_recompute_requests"
    sql = _table_sql(conn, table)
    if sql is None:
        return False
    compact = sql.upper().replace(" ", "")
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if (
        "CHECK(STATE='PENDING')" not in compact
        and {
            "economic_policy_schema",
            "economic_policy_artifact_sha256",
        }.issubset(columns)
    ):
        return False
    legacy = "basket_target_post_fill_recompute_requests_legacy_v1"
    if _table_sql(conn, legacy) is None:
        conn.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
    else:
        # A prior interrupted migration may have left both names.  Preserve the
        # second copy under a bounded deterministic name instead of overwriting.
        backup = legacy + "_retry"
        if _table_sql(conn, backup) is None:
            conn.execute(f"ALTER TABLE {table} RENAME TO {backup}")
        else:
            raise ExecutionTransitionError("post_fill_recompute_schema_migration_conflict")
    return True


def _mapping(name: str, value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ExecutionTransitionError(f"{name}_json_invalid") from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise ExecutionTransitionError(f"{name}_mapping_required")


def _nonnegative(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ExecutionTransitionError(f"{name}_invalid_number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionTransitionError(f"{name}_invalid_number") from exc
    if not math.isfinite(number) or number < 0:
        raise ExecutionTransitionError(f"{name}_invalid_number")
    return number


def _integer(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ExecutionTransitionError(f"{name}_invalid_integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionTransitionError(f"{name}_invalid_integer") from exc
    if number < 0:
        raise ExecutionTransitionError(f"{name}_invalid_integer")
    return number


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ExecutionTransitionError(f"{name}_invalid_sha256")
    return result


def _canonical_scope(raw: Mapping[str, Any]) -> dict[str, str]:
    scope = {
        "station_id": _text("scope_station_id", raw.get("station_id")),
        "event_date": _text("scope_event_date", raw.get("event_date")),
        "settlement_timezone": _text(
            "scope_settlement_timezone", raw.get("settlement_timezone")
        ),
        "bucket": _text("scope_bucket", raw.get("bucket")),
        "token_id": _text("scope_token_id", raw.get("token_id")),
        "side": _text("scope_side", raw.get("side")).upper(),
        "order_action": _text("scope_order_action", raw.get("order_action")).upper(),
    }
    if scope["side"] not in {"YES", "NO"} or scope["order_action"] not in {"BUY", "SELL"}:
        raise ExecutionTransitionError("scope_order_semantics_invalid")
    return scope


def scope_sha256(scope: Mapping[str, Any]) -> str:
    normalized = _canonical_scope(scope)
    return content_sha256({"schema": SCOPE_SCHEMA, **normalized})


def post_fill_recompute_binding_sha256(
    request: Mapping[str, Any],
) -> str:
    """Hash the complete source vector that a recompute must acknowledge."""

    value = _mapping("post_fill_recompute_request", request)
    if value.get("schema") != RECOMPUTE_SCHEMA:
        raise ExecutionTransitionError(
            "post_fill_recompute_request_schema_invalid"
        )
    vector_raw = _mapping(
        "post_fill_recompute_source_vector",
        value.get("source_execution_version_vector"),
    )
    try:
        vector = build_execution_version_vector(vector_raw).to_dict()
    except Exception as exc:
        raise ExecutionTransitionError(
            "post_fill_recompute_source_vector_invalid:" + str(exc)
        ) from exc
    if (
        _sha(
            "post_fill_recompute_source_vector_sha256",
            value.get("source_execution_version_vector_sha256"),
        )
        != vector["vector_sha256"]
    ):
        raise ExecutionTransitionError(
            "post_fill_recompute_source_vector_sha_mismatch"
        )
    field_bindings = {
        "probability_sha256": "probability_sha256",
        "official_ws_quote_sha256": "official_ws_quote_sha256",
        "station_policy_sha256": "station_policy_sha256",
        "cash_policy_decision_sha256": "cash_policy_decision_sha256",
        "economic_policy_artifact_sha256": (
            "economic_policy_artifact_sha256"
        ),
    }
    if value.get("economic_policy_schema") != vector.get(
        "economic_policy_schema"
    ):
        raise ExecutionTransitionError(
            "post_fill_recompute_source_vector_field_mismatch:"
            "economic_policy_schema"
        )
    for request_field, vector_field in field_bindings.items():
        if _sha(
            "post_fill_recompute_" + request_field,
            value.get(request_field),
        ) != vector[vector_field]:
            raise ExecutionTransitionError(
                "post_fill_recompute_source_vector_field_mismatch:"
                + request_field
            )
    normalized_scope = _canonical_scope(
        _mapping("post_fill_recompute_scope", value.get("scope"))
    )
    if _sha(
        "post_fill_recompute_source_scope_sha256",
        value.get("source_scope_sha256"),
    ) != scope_sha256(normalized_scope):
        raise ExecutionTransitionError(
            "post_fill_recompute_source_scope_sha_mismatch"
        )
    binding = {
        "schema": "bayes_basket_target_post_fill_recompute_source_binding_v1",
        "request_id": _text("post_fill_recompute_request_id", value.get("request_id")),
        "order_intent_id": _text(
            "post_fill_recompute_order_intent_id",
            value.get("order_intent_id"),
        ),
        "cycle_id": _text(
            "post_fill_recompute_cycle_id",
            value.get("cycle_id"),
        ),
        "receipt_id": _text(
            "post_fill_recompute_receipt_id",
            value.get("receipt_id"),
        ),
        "scope": normalized_scope,
        "source_execution_version_vector": vector,
        "source_transition_sha256": _sha(
            "post_fill_recompute_source_transition_sha256",
            value.get("source_transition_sha256"),
        ),
        "inventory_version": _text(
            "post_fill_recompute_inventory_version",
            value.get("inventory_version"),
        ),
        "order_state_version": _text(
            "post_fill_recompute_order_state_version",
            value.get("order_state_version"),
        ),
        "quote_sequence": _integer(
            "post_fill_recompute_quote_sequence",
            value.get("quote_sequence"),
        ),
    }
    return content_sha256(binding)


def _receipt_identity(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in receipt.items()
        if key not in {"content_sha256", "receipt_id"}
    }


def _normalize_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise ExecutionTransitionError("execution_receipt_mapping_required")
    try:
        raw_bytes = canonical_json(dict(receipt)).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExecutionTransitionError("execution_receipt_json_invalid") from exc
    if len(raw_bytes) > MAX_RECEIPT_BYTES:
        raise ExecutionTransitionError("execution_receipt_bytes_exceeded")
    assert_process_memory_budget(projected_bytes=len(raw_bytes))
    if receipt.get("schema") not in (None, RECEIPT_SCHEMA):
        raise ExecutionTransitionError("execution_receipt_schema_invalid")
    receipt_type = _text("receipt_type", receipt.get("receipt_type")).upper()
    if receipt_type not in RECEIPT_TYPES:
        raise ExecutionTransitionError("execution_receipt_type_invalid")
    mode = _text("execution_mode", receipt.get("execution_mode"))
    if mode != "production_no_live":
        raise ExecutionTransitionError("execution_receipt_live_mode_forbidden")
    if receipt.get("simulated") is not True:
        raise ExecutionTransitionError("execution_receipt_simulation_required")
    for field in ("live_submit_authorized", "places_orders", "calls_order_gateway"):
        if receipt.get(field) is not False:
            raise ExecutionTransitionError(f"execution_receipt_{field}_boundary_invalid")
    if int(receipt.get("orders_submitted", 0) or 0) != 0:
        raise ExecutionTransitionError("execution_receipt_orders_submitted_invalid")
    intent_id = _text("order_intent_id", receipt.get("order_intent_id"))
    observed_at = _timestamp("observed_at_utc", receipt.get("observed_at_utc"))
    sequence = _integer("quote_sequence", receipt.get("quote_sequence", receipt.get("sequence")))
    vector_raw = _mapping("version_vector", receipt.get("version_vector", receipt.get("evidence")))
    try:
        vector = build_execution_version_vector(vector_raw).to_dict()
    except Exception as exc:
        raise ExecutionTransitionError("execution_receipt_version_vector_invalid:" + str(exc)) from exc
    scope = _canonical_scope(_mapping("scope", receipt.get("scope")))
    matched_size = _nonnegative("matched_size", receipt.get("matched_size", 0.0))
    matched_notional = _nonnegative(
        "matched_notional_usdc", receipt.get("matched_notional_usdc", 0.0)
    )
    requested_size = _nonnegative("requested_size", receipt.get("requested_size", 0.0))
    if requested_size <= 0:
        raise ExecutionTransitionError("requested_size_invalid")
    authoritative = receipt.get("authoritative") is True
    if authoritative != (receipt_type in AUTHORITATIVE_TYPES):
        raise ExecutionTransitionError("execution_receipt_authority_flag_invalid")
    predecessor = str(receipt.get("predecessor_order_intent_id") or "").strip() or None
    normalized = {
        "schema": RECEIPT_SCHEMA,
        "receipt_type": receipt_type,
        "order_intent_id": intent_id,
        "predecessor_order_intent_id": predecessor,
        "exchange_order_id": str(receipt.get("exchange_order_id") or "").strip() or None,
        "execution_mode": mode,
        "simulated": True,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "authoritative": authoritative,
        "scope": scope,
        "scope_sha256": scope_sha256(scope),
        "version_vector": vector,
        "quote_sequence": sequence,
        "observed_at_utc": observed_at,
        "matched_size": matched_size,
        "matched_notional_usdc": matched_notional,
        "requested_size": requested_size,
        "event_payload": _mapping("event_payload", receipt.get("event_payload", {})),
    }
    supplied_sha = receipt.get("content_sha256")
    digest = content_sha256(_receipt_identity(normalized))
    if supplied_sha not in (None, "") and str(supplied_sha).lower() != digest:
        raise ExecutionTransitionError("execution_receipt_content_address_mismatch")
    supplied_id = receipt.get("receipt_id")
    expected_id = "receipt_" + digest[:40]
    if supplied_id not in (None, "") and str(supplied_id) != expected_id:
        raise ExecutionTransitionError("execution_receipt_id_content_address_mismatch")
    normalized["content_sha256"] = digest
    normalized["receipt_id"] = expected_id
    return normalized


def build_no_live_execution_receipt(
    *,
    receipt_type: str,
    order_intent_id: str,
    scope: Mapping[str, Any],
    version_vector: Mapping[str, Any],
    quote_sequence: int,
    observed_at_utc: str,
    requested_size: float,
    matched_size: float = 0.0,
    matched_notional_usdc: float = 0.0,
    exchange_order_id: str | None = None,
    predecessor_order_intent_id: str | None = None,
    event_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one content-addressed simulator receipt; never submits anything."""

    raw = {
        "schema": RECEIPT_SCHEMA,
        "receipt_type": str(receipt_type).upper(),
        "order_intent_id": order_intent_id,
        "predecessor_order_intent_id": predecessor_order_intent_id,
        "exchange_order_id": exchange_order_id,
        "execution_mode": "production_no_live",
        "simulated": True,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "authoritative": str(receipt_type).upper() in AUTHORITATIVE_TYPES,
        "scope": dict(scope),
        "version_vector": dict(version_vector),
        "quote_sequence": quote_sequence,
        "observed_at_utc": observed_at_utc,
        "matched_size": matched_size,
        "matched_notional_usdc": matched_notional_usdc,
        "requested_size": requested_size,
        "event_payload": dict(event_payload or {}),
    }
    return _normalize_receipt(raw)


def apply_execution_transition_schema(conn: sqlite3.Connection, *, commit: bool = True) -> dict[str, Any]:
    """Create only the bounded target-owned receipt tables on this connection."""

    _schema_guard(conn)
    assert_connection_target_capacity(conn)
    owned_transaction = not conn.in_transaction
    migrated_legacy = _migrate_recompute_request_table(conn)
    _execute_ddl_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS basket_target_execution_receipts (
          receipt_id TEXT PRIMARY KEY,
          order_intent_id TEXT NOT NULL,
          cycle_id TEXT NOT NULL,
          exchange_order_id TEXT,
          receipt_type TEXT NOT NULL,
          state_before TEXT NOT NULL,
          state_after TEXT NOT NULL,
          authoritative INTEGER NOT NULL CHECK(authoritative IN (0,1)),
          execution_mode TEXT NOT NULL CHECK(execution_mode='production_no_live'),
          simulated INTEGER NOT NULL CHECK(simulated=1),
          quote_sequence INTEGER NOT NULL CHECK(quote_sequence>=0),
          observed_at_utc TEXT NOT NULL,
          matched_size REAL NOT NULL CHECK(matched_size>=0),
          matched_notional_usdc REAL NOT NULL CHECK(matched_notional_usdc>=0),
          scope_json TEXT NOT NULL,
          vector_json TEXT NOT NULL,
          receipt_json TEXT NOT NULL,
          content_sha256 TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL,
          UNIQUE(order_intent_id,content_sha256),
          FOREIGN KEY(order_intent_id) REFERENCES basket_target_order_intents(order_intent_id)
        );
        CREATE TABLE IF NOT EXISTS basket_target_execution_transition_versions (
          order_intent_id TEXT PRIMARY KEY,
          cycle_id TEXT NOT NULL,
          decision_id TEXT NOT NULL,
          station_id TEXT NOT NULL,
          event_date TEXT NOT NULL,
          settlement_timezone TEXT NOT NULL,
          bucket TEXT NOT NULL,
          token_id TEXT NOT NULL,
          transition_seq INTEGER NOT NULL CHECK(transition_seq>=0),
          state TEXT NOT NULL,
          confirmed_pending_size REAL NOT NULL CHECK(confirmed_pending_size>=0),
          confirmed_pending_notional_usdc REAL NOT NULL CHECK(confirmed_pending_notional_usdc>=0),
          filled_size REAL NOT NULL CHECK(filled_size>=0),
          filled_notional_usdc REAL NOT NULL CHECK(filled_notional_usdc>=0),
          inventory_version TEXT NOT NULL,
          order_state_version TEXT NOT NULL,
          reconciliation_version TEXT NOT NULL,
          last_receipt_id TEXT NOT NULL,
          last_receipt_sha256 TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          transition_json TEXT NOT NULL,
          content_sha256 TEXT NOT NULL UNIQUE,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(order_intent_id) REFERENCES basket_target_order_intents(order_intent_id)
        );
        CREATE TABLE IF NOT EXISTS basket_target_post_fill_recompute_requests (
          request_id TEXT PRIMARY KEY,
          order_intent_id TEXT NOT NULL,
          cycle_id TEXT NOT NULL,
          receipt_id TEXT NOT NULL UNIQUE,
          probability_sha256 TEXT NOT NULL,
          official_ws_quote_sha256 TEXT NOT NULL,
          station_policy_sha256 TEXT NOT NULL,
          cash_policy_decision_sha256 TEXT NOT NULL,
          strict_delta_artifact_sha256 TEXT,
          economic_policy_schema TEXT,
          economic_policy_artifact_sha256 TEXT,
          inventory_version TEXT NOT NULL,
          order_state_version TEXT NOT NULL,
          quote_sequence INTEGER NOT NULL CHECK(quote_sequence>=0),
          scope_json TEXT NOT NULL,
          request_json TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('PENDING','APPLIED')),
          content_sha256 TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL,
          result_json TEXT,
          result_content_sha256 TEXT,
          completion_sha256 TEXT,
          applied_at TEXT,
          FOREIGN KEY(order_intent_id) REFERENCES basket_target_order_intents(order_intent_id),
          CHECK(
            (
              strict_delta_artifact_sha256 IS NOT NULL
              AND economic_policy_schema IS NULL
              AND economic_policy_artifact_sha256 IS NULL
            )
            OR
            (
              strict_delta_artifact_sha256 IS NULL
              AND economic_policy_schema='delta_basket_score_policy_artifact_v2'
              AND economic_policy_artifact_sha256 IS NOT NULL
            )
          )
        );
        CREATE INDEX IF NOT EXISTS basket_target_execution_receipts_intent_idx
          ON basket_target_execution_receipts(order_intent_id,created_at,receipt_id);
        CREATE INDEX IF NOT EXISTS basket_target_execution_transition_state_idx
          ON basket_target_execution_transition_versions(state,updated_at);
        CREATE INDEX IF NOT EXISTS basket_target_post_fill_recompute_state_v2_idx
          ON basket_target_post_fill_recompute_requests(state,created_at);
        """
    )
    # Copy legacy rows after the v2 table exists.  The legacy table is retained
    # as an immutable migration source; INSERT OR IGNORE makes retries safe.
    for legacy in (
        "basket_target_post_fill_recompute_requests_legacy_v1",
        "basket_target_post_fill_recompute_requests_legacy_v1_retry",
    ):
        if _table_sql(conn, legacy) is not None:
            conn.execute(
                f"""
                INSERT OR IGNORE INTO basket_target_post_fill_recompute_requests(
                  request_id,order_intent_id,cycle_id,receipt_id,
                  probability_sha256,official_ws_quote_sha256,station_policy_sha256,
                  cash_policy_decision_sha256,strict_delta_artifact_sha256,
                  inventory_version,order_state_version,quote_sequence,scope_json,
                  request_json,state,content_sha256,created_at
                )
                SELECT request_id,order_intent_id,cycle_id,receipt_id,
                  probability_sha256,official_ws_quote_sha256,station_policy_sha256,
                  cash_policy_decision_sha256,strict_delta_artifact_sha256,
                  inventory_version,order_state_version,quote_sequence,scope_json,
                  request_json,'PENDING',content_sha256,created_at
                FROM {legacy}
                """
            )
    if commit and owned_transaction:
        conn.commit()
    return {
        "schema": SCHEMA,
        "state": "READY",
        "tables": [
            "basket_target_execution_receipts",
            "basket_target_execution_transition_versions",
            "basket_target_post_fill_recompute_requests",
        ],
        "migrated_legacy": migrated_legacy,
        "places_orders": False,
        "calls_order_gateway": False,
    }


@contextmanager
def _transition_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    own = not conn.in_transaction
    savepoint = "target_execution_receipt_transition"
    if own:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except Exception:
        if own:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        if own:
            conn.commit()
        else:
            conn.execute(f"RELEASE {savepoint}")


def _load_binding(conn: sqlite3.Connection, receipt: Mapping[str, Any]) -> dict[str, Any]:
    intent_id = str(receipt["order_intent_id"])
    row = conn.execute(
        "SELECT state,bucket,requested_size,token_id,order_action,side,"
        "cycle_id,decision_id,limit_price,requested_notional_usdc,"
        "intent_json,target_execution_hard_deadline_at "
        "FROM basket_target_order_intents WHERE order_intent_id=?",
        (intent_id,),
    ).fetchone()
    if row is None:
        raise ExecutionTransitionError("order_intent_missing")
    cycle = conn.execute(
        "SELECT station_id,event_date,settlement_timezone,plan_sha256 FROM basket_target_execution_cycles WHERE cycle_id=?",
        (str(row[6]),),
    ).fetchone()
    if cycle is None:
        raise ExecutionTransitionError("execution_cycle_missing")
    expected_scope = _canonical_scope(
        {
            "station_id": cycle[0],
            "event_date": cycle[1],
            "settlement_timezone": cycle[2],
            "bucket": row[1],
            "token_id": row[3],
            "side": row[5],
            "order_action": row[4],
        }
    )
    if dict(receipt["scope"]) != expected_scope:
        raise ExecutionTransitionError("execution_receipt_scope_mismatch")
    vector = _stored_vector(conn, intent_id)
    if dict(receipt["version_vector"]) != vector:
        raise ExecutionTransitionError("execution_receipt_evidence_vector_mismatch")
    intent_json = _mapping("execution_intent_json", row[10])
    execution_candidate = str(
        intent_json.get("execution_candidate") or ""
    ).strip().upper()
    if not execution_candidate:
        raise ExecutionTransitionError(
            "execution_intent_candidate_missing"
        )
    if execution_candidate not in {"MAKER", "TAKER_FOK"}:
        raise ExecutionTransitionError(
            "execution_intent_candidate_invalid"
        )
    receipt_type = str(receipt["receipt_type"])
    if (
        receipt_type in MAKER_RECEIPT_TYPES
        and execution_candidate != "MAKER"
    ):
        raise ExecutionTransitionError(
            "maker_receipt_intent_candidate_mismatch"
        )
    if (
        receipt_type in FOK_RECEIPT_TYPES
        and execution_candidate != "TAKER_FOK"
    ):
        raise ExecutionTransitionError(
            "fok_receipt_intent_candidate_mismatch"
        )
    if receipt["quote_sequence"] < 0:
        raise ExecutionTransitionError("execution_receipt_sequence_invalid")
    # Each evidence row is looked up inside the cycle's own station-day: the
    # (station_id, event_date, ...) indexes seek, where content_sha256 alone
    # scanned 2.2M rows -- 214 s, 48 s, 35 s (query-plan guard, 2026-10-15).
    # A row from another station-day was never the cycle's evidence.
    quote = conn.execute(
        "SELECT * FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND content_sha256=?",
        (cycle[0], cycle[1], vector["official_ws_quote_sha256"]),
    ).fetchone()
    if quote is None:
        raise ExecutionTransitionError("execution_receipt_quote_evidence_missing")
    quote_columns = [str(item[0]) for item in conn.execute("SELECT * FROM basket_official_ws_snapshot LIMIT 0").description or ()]
    quote_map = {key: quote[index] for index, key in enumerate(quote_columns)}
    try:
        quote_buckets = json.loads(str(quote_map["buckets_json"]))
        quote_cash = json.loads(str(quote_map["cash_phase_input_json"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionTransitionError("execution_receipt_quote_json_invalid") from exc
    quote_identity = {**quote_map, "buckets_json": quote_buckets, "cash_phase_input_json": quote_cash}
    quote_identity.pop("content_sha256", None)
    if content_sha256({"schema": "basket_official_ws_snapshot_v1", **quote_identity}) != vector["official_ws_quote_sha256"]:
        raise ExecutionTransitionError("execution_receipt_quote_content_sha_mismatch")
    if int(quote_map["sequence"]) != int(receipt["quote_sequence"]):
        raise ExecutionTransitionError("execution_receipt_quote_sequence_mismatch")
    if str(quote_map["state"]) != "COMMITTED" or str(quote_map["quote_source"]) != "OFFICIAL_CLOB_WS":
        raise ExecutionTransitionError("execution_receipt_quote_authority_invalid")
    if (
        not _same_instant(
            quote_map["quote_observed_at"],
            vector["quote_observed_at"],
        )
        or not _same_instant(
            quote_map["quote_expires_at"],
            vector["quote_expires_at"],
        )
    ):
        raise ExecutionTransitionError("execution_receipt_quote_freshness_binding_mismatch")
    inventory = conn.execute(
        "SELECT * FROM basket_confirmed_inventory_snapshot"
        " WHERE station_id=? AND event_date=? AND content_sha256=?",
        (cycle[0], cycle[1], vector["inventory_sha256"]),
    ).fetchone()
    order_state = conn.execute(
        "SELECT * FROM basket_order_state_snapshot"
        " WHERE station_id=? AND event_date=? AND content_sha256=?",
        (cycle[0], cycle[1], vector["order_state_sha256"]),
    ).fetchone()
    if inventory is None or order_state is None:
        raise ExecutionTransitionError("execution_receipt_inventory_order_evidence_missing")
    inventory_columns = [str(item[0]) for item in conn.execute("SELECT * FROM basket_confirmed_inventory_snapshot LIMIT 0").description or ()]
    order_columns = [str(item[0]) for item in conn.execute("SELECT * FROM basket_order_state_snapshot LIMIT 0").description or ()]
    inventory_map = {key: inventory[index] for index, key in enumerate(inventory_columns)}
    order_map = {key: order_state[index] for index, key in enumerate(order_columns)}
    try:
        inventory_json = json.loads(str(inventory_map["inventory_json"]))
        order_json = json.loads(str(order_map["order_state_json"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionTransitionError("execution_receipt_inventory_order_json_invalid") from exc
    inventory_identity = {**inventory_map, "inventory_json": inventory_json}
    order_identity = {**order_map, "order_state_json": order_json}
    inventory_identity.pop("content_sha256", None)
    order_identity.pop("content_sha256", None)
    if content_sha256({"schema": "basket_confirmed_inventory_snapshot_v1", **inventory_identity}) != vector["inventory_sha256"]:
        raise ExecutionTransitionError("execution_receipt_inventory_content_sha_mismatch")
    if content_sha256({"schema": "basket_order_state_snapshot_v1", **order_identity}) != vector["order_state_sha256"]:
        raise ExecutionTransitionError("execution_receipt_order_state_content_sha_mismatch")
    if str(quote_map["inventory_version"]) != str(inventory_map["inventory_version"]) or str(quote_map["order_state_version"]) != str(order_map["order_state_version"]):
        raise ExecutionTransitionError("execution_receipt_quote_state_version_mismatch")
    observed = datetime.fromisoformat(str(receipt["observed_at_utc"]).replace("Z", "+00:00"))
    expires = datetime.fromisoformat(str(vector["quote_expires_at"]).replace("Z", "+00:00"))
    fresh = observed < expires
    latest = conn.execute(
        "SELECT MAX(sequence) FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'",
        (cycle[0], cycle[1]),
    ).fetchone()
    latest_sequence = int(latest[0]) if latest and latest[0] is not None else int(receipt["quote_sequence"])
    return {
        "order_intent_id": intent_id,
        "intent_state": str(row[0]),
        "bucket": str(row[1]),
        "requested_size_db": float(row[2]),
        "token_id": str(row[3]),
        "order_action": str(row[4]),
        "side": str(row[5]),
        "cycle_id": str(row[6]),
        "decision_id": str(row[7]),
        "limit_price": float(row[8]),
        "requested_notional_usdc_db": float(row[9]),
        "execution_candidate": execution_candidate,
        "intent_json": intent_json,
        "target_execution_hard_deadline_at": str(row[11] or ""),
        "scope": expected_scope,
        "vector": vector,
        "station_id": str(cycle[0]),
        "event_date": str(cycle[1]),
        "settlement_timezone": str(cycle[2]),
        "fresh": fresh,
        "latest_sequence": latest_sequence,
    }


def _prior_transition(conn: sqlite3.Connection, intent_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT transition_seq,state,confirmed_pending_size,"
        "confirmed_pending_notional_usdc,filled_size,filled_notional_usdc,"
        "last_receipt_id,last_receipt_sha256,evidence_json,updated_at "
        "FROM basket_target_execution_transition_versions WHERE order_intent_id=?",
        (intent_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "transition_seq": int(row[0]),
        "state": str(row[1]),
        "confirmed_pending_size": float(row[2]),
        "confirmed_pending_notional_usdc": float(row[3]),
        "filled_size": float(row[4]),
        "filled_notional_usdc": float(row[5]),
        "last_receipt_id": str(row[6]),
        "last_receipt_sha256": str(row[7]),
        "evidence": _mapping("prior_transition_evidence", row[8]),
        "updated_at": _timestamp("prior_transition_updated_at", row[9]),
    }


def _insert_event(conn: sqlite3.Connection, receipt: Mapping[str, Any], binding: Mapping[str, Any]) -> str:
    event_type = _EVENT_TYPE_MAP[str(receipt["receipt_type"])]
    if event_type not in EVENT_TYPES:
        raise ExecutionTransitionError("execution_receipt_event_type_invalid")
    event_id = "event_" + str(receipt["content_sha256"])[:40]
    payload = {
        "schema": RECEIPT_SCHEMA,
        "receipt": dict(receipt),
        "scope_sha256": receipt["scope_sha256"],
        "version_vector_sha256": receipt["version_vector"]["vector_sha256"],
        "station_id": binding["station_id"],
        "event_date": binding["event_date"],
    }
    existing = conn.execute(
        "SELECT event_sha256 FROM basket_target_order_events WHERE event_id=?", (event_id,)
    ).fetchone()
    event_sha = content_sha256({"schema": "basket_target_order_event_v1", "event_id": event_id, **payload})
    if existing is not None:
        if str(existing[0]) != event_sha:
            raise ExecutionTransitionError("execution_event_content_conflict")
        return event_id
    conn.execute(
        "INSERT INTO basket_target_order_events(event_id,order_intent_id,exchange_order_id,event_type,observed_at,matched_size,matched_notional_usdc,event_json,event_sha256) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            receipt["order_intent_id"],
            receipt["exchange_order_id"],
            event_type,
            receipt["observed_at_utc"],
            float(receipt["matched_size"]),
            float(receipt["matched_notional_usdc"]),
            canonical_json(payload),
            event_sha,
        ),
    )
    return event_id


def can_submit_fok(
    conn: sqlite3.Connection,
    *,
    predecessor_order_intent_id: str,
    current_vector: Mapping[str, Any],
    at_utc: str,
) -> dict[str, Any]:
    """Allow FOK only after an authoritative, fresh, zero-fill cancel confirm."""

    apply_execution_transition_schema(conn)
    predecessor = _text("predecessor_order_intent_id", predecessor_order_intent_id)
    at = _timestamp("at_utc", at_utc)
    expected = _stored_vector(conn, predecessor)
    try:
        current = build_execution_version_vector(current_vector).to_dict()
    except Exception as exc:
        raise ExecutionTransitionError("fok_current_version_vector_invalid:" + str(exc)) from exc
    if current != expected:
        raise ExecutionTransitionError("fok_cancel_confirm_version_drift")
    prior = _prior_transition(conn, predecessor)
    if (
        prior is None
        or prior["state"] != "CANCEL_CONFIRMED"
        or float(prior["filled_size"]) > EPS
        or float(prior["filled_notional_usdc"]) > EPS
    ):
        return {
            "schema": SCHEMA,
            "allowed": False,
            "reason": "authoritative_cancel_confirmation_required",
            "places_orders": False,
            "calls_order_gateway": False,
        }
    if datetime.fromisoformat(at.replace("Z", "+00:00")) <= datetime.fromisoformat(
        str(prior["updated_at"]).replace("Z", "+00:00")
    ):
        return {
            "schema": SCHEMA,
            "allowed": False,
            "reason": "fok_after_cancel_confirmation_clock_required",
            "places_orders": False,
            "calls_order_gateway": False,
        }
    return {
        "schema": SCHEMA,
        "allowed": True,
        "reason": "authoritative_cancel_confirmed",
        "predecessor_order_intent_id": predecessor,
        "at_utc": at,
        "cancel_receipt_id": prior["last_receipt_id"],
        "cancel_receipt_sha256": prior["last_receipt_sha256"],
        "places_orders": False,
        "calls_order_gateway": False,
    }


def _state_for_receipt(receipt: Mapping[str, Any], binding: Mapping[str, Any]) -> tuple[str, str, str, bool]:
    receipt_type = str(receipt["receipt_type"])
    requested = float(binding["requested_size_db"])
    matched = float(receipt["matched_size"])
    matched_notional = float(receipt["matched_notional_usdc"])
    if abs(float(receipt["requested_size"]) - requested) > EPS:
        raise ExecutionTransitionError("execution_receipt_requested_size_mismatch")
    if matched > requested + EPS:
        raise ExecutionTransitionError("execution_receipt_matched_size_exceeded")
    if (matched <= EPS) != (matched_notional <= EPS):
        raise ExecutionTransitionError(
            "execution_receipt_fill_notional_size_inconsistent"
        )
    if matched_notional > float(binding["requested_notional_usdc_db"]) + EPS:
        raise ExecutionTransitionError(
            "execution_receipt_matched_notional_exceeded"
        )
    if (
        matched > EPS
        and matched_notional / matched > float(binding["limit_price"]) + EPS
    ):
        raise ExecutionTransitionError(
            "execution_receipt_fill_price_above_limit"
        )
    if receipt_type == "CANCEL_CONFIRMED":
        if not receipt["authoritative"] or matched > EPS:
            raise ExecutionTransitionError("cancel_confirmation_requires_authoritative_zero_fill")
        if not binding["fresh"] or binding["latest_sequence"] != int(receipt["quote_sequence"]):
            return "RECONCILE_REQUIRED", "reconcile_required", "cancel_confirmation_stale_or_superseded", False
        return "CANCEL_CONFIRMED", "cancelled", "authoritative_cancel_confirmed", True
    if receipt_type in FULL_FILL_TYPES:
        if not receipt["authoritative"] or abs(matched - requested) > EPS:
            raise ExecutionTransitionError("authoritative_full_fill_size_invalid")
        if not binding["fresh"] or binding["latest_sequence"] != int(receipt["quote_sequence"]):
            return "RECONCILE_REQUIRED", "reconcile_required", "full_fill_stale_or_superseded", False
        return "FULL_FILLED", "filled", "authoritative_full_fill", True
    if receipt_type == "MAKER_PARTIAL_FILL":
        if (
            not receipt["authoritative"]
            or matched <= EPS
            or matched >= requested - EPS
        ):
            raise ExecutionTransitionError(
                "authoritative_maker_partial_fill_size_invalid"
            )
        if (
            not binding["fresh"]
            or binding["latest_sequence"] != int(receipt["quote_sequence"])
        ):
            return (
                "RECONCILE_REQUIRED",
                "reconcile_required",
                "maker_partial_fill_stale_or_superseded",
                False,
            )
        return (
            "PARTIAL_FILLED",
            "cancelled",
            "authoritative_maker_partial_fill",
            False,
        )
    if receipt_type == "CANCEL_REQUESTED":
        if matched > EPS:
            return "RECONCILE_REQUIRED", "reconcile_required", "cancel_request_with_fill", False
        return "CANCEL_REQUESTED", "submitted", "cancel_confirmation_pending", False
    if receipt_type == "POST_ACK":
        return "POST_ACKED", "submitted", "post_ack_no_fill_confirmation", False
    if receipt_type == "FOK_SUBMITTED":
        return "FOK_SUBMITTED", "submitted", "fok_posted_no_fill_confirmation", False
    if receipt_type in RECONCILE_TYPES:
        return "RECONCILE_REQUIRED", "reconcile_required", receipt_type.lower(), False
    raise ExecutionTransitionError("execution_receipt_transition_unsupported")


def _insert_receipt_row(
    conn: sqlite3.Connection,
    receipt: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    state_before: str,
    state_after: str,
) -> None:
    existing = conn.execute(
        "SELECT content_sha256 FROM basket_target_execution_receipts WHERE receipt_id=?",
        (receipt["receipt_id"],),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != str(receipt["content_sha256"]):
            raise ExecutionTransitionError("execution_receipt_content_conflict")
        return
    conn.execute(
        "INSERT INTO basket_target_execution_receipts(receipt_id,order_intent_id,cycle_id,exchange_order_id,receipt_type,state_before,state_after,authoritative,execution_mode,simulated,quote_sequence,observed_at_utc,matched_size,matched_notional_usdc,scope_json,vector_json,receipt_json,content_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            receipt["receipt_id"], receipt["order_intent_id"], binding["cycle_id"],
            receipt["exchange_order_id"], receipt["receipt_type"], state_before, state_after,
            int(receipt["authoritative"]), receipt["execution_mode"], int(receipt["simulated"]),
            int(receipt["quote_sequence"]), receipt["observed_at_utc"],
            float(receipt["matched_size"]), float(receipt["matched_notional_usdc"]),
            canonical_json(receipt["scope"]), canonical_json(receipt["version_vector"]),
            canonical_json(receipt), receipt["content_sha256"], receipt["observed_at_utc"],
        ),
    )


def _validate_transition_order(
    *,
    prior: Mapping[str, Any] | None,
    receipt: Mapping[str, Any],
) -> None:
    """Reject late, rewind, repeated-state, and terminal regressions."""

    if prior is None:
        return
    observed = datetime.fromisoformat(
        str(receipt["observed_at_utc"]).replace("Z", "+00:00")
    )
    previous_observed = datetime.fromisoformat(
        str(prior["updated_at"]).replace("Z", "+00:00")
    )
    if observed <= previous_observed:
        raise ExecutionTransitionError(
            "execution_receipt_observed_at_not_strictly_increasing"
        )
    prior_quote_sequence = _integer(
        "prior_transition_quote_sequence",
        prior["evidence"].get("quote_sequence"),
    )
    if int(receipt["quote_sequence"]) < prior_quote_sequence:
        raise ExecutionTransitionError(
            "execution_receipt_quote_sequence_rewind"
        )
    allowed: dict[str, frozenset[str]] = {
        "POST_ACKED": frozenset({
            "CANCEL_REQUESTED",
            "CANCEL_CONFIRMED",
            "MAKER_FULL_FILL",
            "MAKER_PARTIAL_FILL",
            "POST_UNKNOWN",
        }),
        "CANCEL_REQUESTED": frozenset({
            "CANCEL_CONFIRMED",
            "MAKER_FULL_FILL",
            "LATE_FILL",
            "CANCEL_FILL_RACE",
            "POST_UNKNOWN",
        }),
        "FOK_SUBMITTED": frozenset({
            "FOK_FULL_FILL",
            "FOK_PARTIAL_FILL",
            "FOK_UNKNOWN",
            "FOK_FAILED",
        }),
    }
    if str(receipt["receipt_type"]) not in allowed.get(
        str(prior["state"]),
        frozenset(),
    ):
        raise ExecutionTransitionError(
            "execution_receipt_state_transition_forbidden"
        )


def _validate_fok_predecessor(
    conn: sqlite3.Connection,
    *,
    receipt: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> None:
    """Bind every FOK lifecycle to one zero-fill Maker cancel confirmation."""

    if str(receipt["receipt_type"]) not in FOK_RECEIPT_TYPES:
        if receipt.get("predecessor_order_intent_id") is not None:
            raise ExecutionTransitionError(
                "maker_receipt_predecessor_forbidden"
            )
        return
    predecessor_id = str(
        receipt.get("predecessor_order_intent_id") or ""
    ).strip()
    if not predecessor_id:
        raise ExecutionTransitionError(
            "fok_predecessor_order_intent_required"
        )
    if predecessor_id == str(receipt["order_intent_id"]):
        raise ExecutionTransitionError(
            "fok_predecessor_must_be_distinct"
        )
    row = conn.execute(
        "SELECT bucket,token_id,side,order_action,intent_json "
        "FROM basket_target_order_intents WHERE order_intent_id=?",
        (predecessor_id,),
    ).fetchone()
    if row is None:
        raise ExecutionTransitionError(
            "fok_predecessor_order_intent_missing"
        )
    predecessor_intent = _mapping("fok_predecessor_intent_json", row[4])
    if (
        str(predecessor_intent.get("execution_candidate") or "").upper()
        != "MAKER"
    ):
        raise ExecutionTransitionError("fok_predecessor_not_maker")
    if (
        str(row[0]) != str(binding["bucket"])
        or str(row[1]) != str(binding["token_id"])
        or str(row[2]) != str(binding["side"])
        or str(row[3]) != str(binding["order_action"])
    ):
        raise ExecutionTransitionError("fok_predecessor_scope_mismatch")
    predecessor_vector = _stored_vector(conn, predecessor_id)
    if predecessor_vector != receipt["version_vector"]:
        raise ExecutionTransitionError("fok_cancel_confirm_version_drift")
    predecessor = _prior_transition(conn, predecessor_id)
    if (
        predecessor is None
        or predecessor["state"] != "CANCEL_CONFIRMED"
        or float(predecessor["filled_size"]) > EPS
        or float(predecessor["filled_notional_usdc"]) > EPS
    ):
        raise ExecutionTransitionError(
            "fok_authoritative_zero_fill_cancel_confirmation_missing"
        )
    if datetime.fromisoformat(
        str(receipt["observed_at_utc"]).replace("Z", "+00:00")
    ) <= datetime.fromisoformat(
        str(predecessor["updated_at"]).replace("Z", "+00:00")
    ):
        raise ExecutionTransitionError(
            "fok_before_cancel_confirmation_forbidden"
        )


def _advance_cycle_state(
    conn: sqlite3.Connection,
    binding: Mapping[str, Any],
    *,
    state_after: str,
    at_utc: str,
) -> tuple[str, str]:
    """Atomically close a cycle or poison it for reconciliation.

    A cycle may contain multiple legs.  Any authoritative terminal fill closes
    the old bundle only when no sibling remains in flight; a
    reconcile-required receipt always propagates to the cycle.
    """

    cycle_id = str(binding["cycle_id"])
    row = conn.execute(
        "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
        (cycle_id,),
    ).fetchone()
    if row is None:
        raise ExecutionTransitionError("execution_cycle_missing")
    before = str(row[0])
    after = before
    if state_after == "RECONCILE_REQUIRED":
        # The triggering leg is unknown, so its capital cannot be released or
        # its inventory guessed.  Still-unsubmitted siblings are known safe:
        # invalidate them immediately so the poisoned bundle cannot continue
        # leg-by-leg while reconciliation is pending.
        conn.execute(
            "UPDATE basket_target_order_intents SET state='cancelled' "
            "WHERE cycle_id=? AND order_intent_id<>? "
            "AND state='prepared'",
            (cycle_id, str(binding["order_intent_id"])),
        )
        after = "reconcile_required"
    elif state_after in RECOMPUTE_FILL_STATES:
        sibling_states = [
            str(item[0])
            for item in conn.execute(
                "SELECT state FROM basket_target_order_intents "
                "WHERE cycle_id=? AND order_intent_id<>?",
                (cycle_id, str(binding["order_intent_id"])),
            ).fetchall()
        ]
        if any(
            state in {
                "submitting",
                "submitted",
                "reconcile_required",
                "filled",
            }
            for state in sibling_states
        ):
            # A sibling already in flight/filled cannot be guessed away.
            after = "reconcile_required"
        else:
            # Candidate rows are evaluation outputs, not permission to keep
            # stale sibling legs alive after the first authoritative fill.
            # Supersede every still-unsubmitted sibling before recomputing q.
            conn.execute(
                "UPDATE basket_target_order_intents SET state='cancelled' "
                "WHERE cycle_id=? AND order_intent_id<>? "
                "AND state='prepared'",
                (cycle_id, str(binding["order_intent_id"])),
            )
            reservation = conn.execute(
                "SELECT reservation_id,amount_usdc,state FROM "
                "basket_target_capital_reservations WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()
            if reservation is None:
                raise ExecutionTransitionError(
                    "execution_cycle_reservation_missing"
                )
            reservation_id = str(reservation[0])
            reserved_amount = float(reservation[1])
            reservation_state = str(reservation[2])
            if reservation_state != "active":
                raise ExecutionTransitionError(
                    "execution_cycle_reservation_not_active"
                )
            filled_row = conn.execute(
                "SELECT COALESCE(SUM(filled_notional_usdc),0) FROM "
                "basket_target_execution_transition_versions "
                "WHERE cycle_id=? "
                "AND state IN ('FULL_FILLED','PARTIAL_FILLED')",
                (cycle_id,),
            ).fetchone()
            consumed_amount = float(
                filled_row[0] if filled_row is not None else 0.0
            )
            if (
                consumed_amount <= EPS
                or consumed_amount > reserved_amount + EPS
            ):
                raise ExecutionTransitionError(
                    "execution_cycle_consumed_amount_invalid"
                )
            pool = conn.execute(
                "SELECT balance_usdc,reserved_usdc FROM "
                "basket_target_capital_pool WHERE pool_id=1"
            ).fetchone()
            if pool is None:
                raise ExecutionTransitionError(
                    "execution_capital_pool_missing"
                )
            balance = float(pool[0])
            reserved = float(pool[1])
            if (
                consumed_amount > balance + EPS
                or reserved_amount > reserved + EPS
            ):
                raise ExecutionTransitionError(
                    "execution_capital_pool_drift"
                )
            conn.execute(
                "UPDATE basket_target_capital_pool SET "
                "balance_usdc=balance_usdc-?,"
                "reserved_usdc=reserved_usdc-?,version=version+1,"
                "updated_at=? WHERE pool_id=1",
                (
                    consumed_amount,
                    reserved_amount,
                    at_utc,
                ),
            )
            conn.execute(
                "UPDATE basket_target_capital_reservations SET "
                "state='consumed',release_reason=?,updated_at=? "
                "WHERE reservation_id=? AND state='active'",
                (
                    "authoritative_terminal_fill_actual_usdc="
                    + format(consumed_amount, ".8f"),
                    at_utc,
                    reservation_id,
                ),
            )
            after = "settled"
    if after != before:
        conn.execute(
            "UPDATE basket_target_execution_cycles SET state=?,updated_at=? WHERE cycle_id=?",
            (after, at_utc, cycle_id),
        )
    return before, after


def _persist_transition(
    conn: sqlite3.Connection,
    receipt: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    state_before: str,
    state_after: str,
    intent_state: str,
    reason: str,
    prior: Mapping[str, Any] | None,
    event_id: str,
) -> dict[str, Any]:
    requested = float(binding["requested_size_db"])
    matched = float(receipt["matched_size"])
    if state_after == "FULL_FILLED":
        filled = requested
        pending = 0.0
    elif state_after == "PARTIAL_FILLED":
        filled = matched
        pending = 0.0
    elif state_after == "CANCEL_CONFIRMED":
        filled = float(prior["filled_size"] if prior else 0.0)
        pending = 0.0
    elif state_after in {"POST_ACKED", "FOK_SUBMITTED", "CANCEL_REQUESTED"}:
        filled = float(prior["filled_size"] if prior else 0.0)
        pending = max(0.0, requested - filled)
    else:
        filled = max(float(prior["filled_size"] if prior else 0.0), matched)
        pending = 0.0
    prior_notional = float(prior["filled_notional_usdc"] if prior else 0.0)
    if state_after in RECOMPUTE_FILL_STATES:
        filled_notional = float(receipt["matched_notional_usdc"])
    elif state_after in {"POST_ACKED", "FOK_SUBMITTED", "CANCEL_REQUESTED", "CANCEL_CONFIRMED"}:
        filled_notional = prior_notional
    else:
        filled_notional = max(
            prior_notional,
            float(receipt["matched_notional_usdc"]),
        )
    transition_seq = int(prior["transition_seq"] + 1 if prior else 1)
    common = {
        "schema": SCHEMA,
        "order_intent_id": receipt["order_intent_id"],
        "cycle_id": binding["cycle_id"],
        "decision_id": binding["decision_id"],
        "scope_sha256": receipt["scope_sha256"],
        "transition_seq": transition_seq,
        "state": state_after,
        "reason": reason,
        "filled_size": filled,
        "filled_notional_usdc": filled_notional,
        "last_receipt_sha256": receipt["content_sha256"],
        "evidence": receipt["version_vector"],
    }
    inventory_version = "invrec_" + content_sha256({**common, "kind": "inventory"})[:40]
    order_state_version = "ordrec_" + content_sha256({**common, "kind": "order_state"})[:40]
    reconciliation_version = "recon_" + content_sha256({**common, "kind": "reconciliation"})[:40]
    evidence_json = canonical_json({
        "version_vector": receipt["version_vector"],
        "quote_sequence": receipt["quote_sequence"],
        "observed_at_utc": receipt["observed_at_utc"],
        "scope_sha256": receipt["scope_sha256"],
    })
    transition_json = canonical_json({
        **common,
        "inventory_version": inventory_version,
        "order_state_version": order_state_version,
        "reconciliation_version": reconciliation_version,
        "event_id": event_id,
    })
    transition_sha = content_sha256({
        "schema": "basket_target_execution_transition_version_v1",
        "transition": json.loads(transition_json),
        "evidence": json.loads(evidence_json),
    })
    conn.execute(
        "INSERT INTO basket_target_execution_transition_versions(order_intent_id,cycle_id,decision_id,station_id,event_date,settlement_timezone,bucket,token_id,transition_seq,state,confirmed_pending_size,confirmed_pending_notional_usdc,filled_size,filled_notional_usdc,inventory_version,order_state_version,reconciliation_version,last_receipt_id,last_receipt_sha256,evidence_json,transition_json,content_sha256,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(order_intent_id) DO UPDATE SET cycle_id=excluded.cycle_id,decision_id=excluded.decision_id,station_id=excluded.station_id,event_date=excluded.event_date,settlement_timezone=excluded.settlement_timezone,bucket=excluded.bucket,token_id=excluded.token_id,transition_seq=excluded.transition_seq,state=excluded.state,confirmed_pending_size=excluded.confirmed_pending_size,confirmed_pending_notional_usdc=excluded.confirmed_pending_notional_usdc,filled_size=excluded.filled_size,filled_notional_usdc=excluded.filled_notional_usdc,inventory_version=excluded.inventory_version,order_state_version=excluded.order_state_version,reconciliation_version=excluded.reconciliation_version,last_receipt_id=excluded.last_receipt_id,last_receipt_sha256=excluded.last_receipt_sha256,evidence_json=excluded.evidence_json,transition_json=excluded.transition_json,content_sha256=excluded.content_sha256,updated_at=excluded.updated_at",
        (
            receipt["order_intent_id"], binding["cycle_id"], binding["decision_id"], binding["station_id"],
            binding["event_date"], binding["settlement_timezone"], binding["bucket"], binding["token_id"],
            transition_seq, state_after, pending, 0.0, filled, filled_notional, inventory_version,
            order_state_version, reconciliation_version, receipt["receipt_id"], receipt["content_sha256"],
            evidence_json, transition_json, transition_sha, receipt["observed_at_utc"],
        ),
    )
    conn.execute(
        "UPDATE basket_target_order_intents SET state=? WHERE order_intent_id=?",
        (intent_state, receipt["order_intent_id"]),
    )
    cycle_state_before, cycle_state_after = _advance_cycle_state(
        conn,
        binding,
        state_after=state_after,
        at_utc=str(receipt["observed_at_utc"]),
    )
    if state_after in RECOMPUTE_FILL_STATES:
        request_identity = {
            "schema": RECOMPUTE_SCHEMA,
            "order_intent_id": receipt["order_intent_id"],
            "cycle_id": binding["cycle_id"],
            "receipt_id": receipt["receipt_id"],
            "probability_sha256": receipt["version_vector"]["probability_sha256"],
            "official_ws_quote_sha256": receipt["version_vector"]["official_ws_quote_sha256"],
            "station_policy_sha256": receipt["version_vector"]["station_policy_sha256"],
            "cash_policy_decision_sha256": receipt["version_vector"]["cash_policy_decision_sha256"],
            "economic_policy_schema": receipt["version_vector"][
                "economic_policy_schema"
            ],
            "economic_policy_artifact_sha256": receipt[
                "version_vector"
            ]["economic_policy_artifact_sha256"],
            "source_execution_version_vector": dict(
                receipt["version_vector"]
            ),
            "source_execution_version_vector_sha256": receipt[
                "version_vector"
            ]["vector_sha256"],
            "source_scope_sha256": receipt["scope_sha256"],
            "source_transition_sha256": transition_sha,
            "inventory_version": inventory_version,
            "order_state_version": order_state_version,
            "quote_sequence": int(receipt["quote_sequence"]),
            "scope": receipt["scope"],
            "state": "PENDING",
        }
        request_sha = content_sha256(request_identity)
        request_id = "recompute_" + request_sha[:40]
        request_payload = {**request_identity, "request_id": request_id}
        existing_request = conn.execute(
            "SELECT content_sha256 FROM basket_target_post_fill_recompute_requests WHERE receipt_id=?",
            (receipt["receipt_id"],),
        ).fetchone()
        if existing_request is None:
            conn.execute(
                "INSERT INTO basket_target_post_fill_recompute_requests(request_id,order_intent_id,cycle_id,receipt_id,probability_sha256,official_ws_quote_sha256,station_policy_sha256,cash_policy_decision_sha256,strict_delta_artifact_sha256,economic_policy_schema,economic_policy_artifact_sha256,inventory_version,order_state_version,quote_sequence,scope_json,request_json,state,content_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id, receipt["order_intent_id"], binding["cycle_id"], receipt["receipt_id"],
                    request_identity["probability_sha256"], request_identity["official_ws_quote_sha256"],
                    request_identity["station_policy_sha256"], request_identity["cash_policy_decision_sha256"],
                    None, request_identity["economic_policy_schema"],
                    request_identity["economic_policy_artifact_sha256"],
                    inventory_version, order_state_version,
                    int(receipt["quote_sequence"]), canonical_json(receipt["scope"]),
                    canonical_json(request_payload), "PENDING", request_sha, receipt["observed_at_utc"],
                ),
            )
        elif str(existing_request[0]) != request_sha:
            raise ExecutionTransitionError("post_fill_recompute_request_content_conflict")
    return {
        "inventory_version": inventory_version,
        "order_state_version": order_state_version,
        "reconciliation_version": reconciliation_version,
        "transition_seq": transition_seq,
        "filled_size": filled,
        "filled_notional_usdc": filled_notional,
        "pending_size": pending,
        "transition_sha256": transition_sha,
        "post_fill_recompute_required": state_after in RECOMPUTE_FILL_STATES,
        "cycle_state_before": cycle_state_before,
        "cycle_state_after": cycle_state_after,
    }


def apply_execution_receipt(conn: sqlite3.Connection, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically apply one strict no-live receipt and return its transition."""

    normalized = _normalize_receipt(receipt)
    apply_execution_transition_schema(conn)
    assert_connection_target_capacity(conn)
    with _transition_transaction(conn):
        binding = _load_binding(conn, normalized)
        existing = conn.execute(
            "SELECT content_sha256,state_after FROM basket_target_execution_receipts WHERE receipt_id=?",
            (normalized["receipt_id"],),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != str(normalized["content_sha256"]):
                raise ExecutionTransitionError("execution_receipt_content_conflict")
            return {
                "schema": SCHEMA,
                "state": "DUPLICATE_RECEIPT",
                "idempotent": True,
                "receipt_id": normalized["receipt_id"],
                "receipt_sha256": normalized["content_sha256"],
                "state_after": str(existing[1]),
                "places_orders": False,
                "calls_order_gateway": False,
            }
        prior = _prior_transition(conn, normalized["order_intent_id"])
        if prior is not None and prior["last_receipt_sha256"] == normalized["content_sha256"]:
            raise ExecutionTransitionError("execution_receipt_duplicate_state_missing")
        state_before = prior["state"] if prior else str(binding["intent_state"])
        if prior is None and state_before not in {
            "prepared",
            "submitting",
            "submitted",
        }:
            raise ExecutionTransitionError(
                "execution_receipt_initial_intent_state_invalid"
            )
        _validate_transition_order(prior=prior, receipt=normalized)
        _validate_fok_predecessor(
            conn,
            receipt=normalized,
            binding=binding,
        )
        state_after, intent_state, reason, authorized = _state_for_receipt(normalized, binding)
        event_id = _insert_event(conn, normalized, binding)
        _insert_receipt_row(
            conn, normalized, binding, state_before=state_before, state_after=state_after
        )
        transition = _persist_transition(
            conn,
            normalized,
            binding,
            state_before=state_before,
            state_after=state_after,
            intent_state=intent_state,
            reason=reason,
            prior=prior,
            event_id=event_id,
        )
        return {
            "schema": SCHEMA,
            "state": "APPLIED",
            "idempotent": False,
            "receipt_id": normalized["receipt_id"],
            "receipt_sha256": normalized["content_sha256"],
            "receipt_type": normalized["receipt_type"],
            "state_before": state_before,
            "state_after": state_after,
            "intent_state": intent_state,
            "reason": reason,
            "authoritative": normalized["authoritative"],
            "authorized_for_next_fok": bool(
                authorized
                and state_after == "CANCEL_CONFIRMED"
                and float(transition["filled_size"]) <= EPS
                and float(transition["filled_notional_usdc"]) <= EPS
            ),
            **transition,
            "places_orders": False,
            "calls_order_gateway": False,
        }


def complete_post_fill_recompute_request(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    scope: Mapping[str, Any],
    inventory_version: str,
    order_state_version: str,
    result: Mapping[str, Any],
    result_content_sha256: str,
    completed_at_utc: str,
) -> dict[str, Any]:
    """Durably apply one post-fill recompute result.

    The request remains PENDING on every validation or persistence failure, so
    a worker can retry.  Once APPLIED, only the exact same binding and result
    can be replayed idempotently; all other completions fail closed.
    """

    rid = _text("request_id", request_id)
    normalized_scope = _canonical_scope(scope)
    inv = _text("inventory_version", inventory_version)
    order = _text("order_state_version", order_state_version)
    completed_at = _timestamp("completed_at_utc", completed_at_utc)
    if not isinstance(result, Mapping):
        raise ExecutionTransitionError("recompute_result_mapping_required")
    result_obj = dict(result)
    result_bytes = canonical_json(result_obj).encode("utf-8")
    if len(result_bytes) > MAX_RECOMPUTE_RESULT_BYTES:
        raise ExecutionTransitionError("recompute_result_bytes_exceeded")
    assert_process_memory_budget(projected_bytes=len(result_bytes))
    supplied_result_sha = _sha("result_content_sha256", result_content_sha256)
    expected_result_sha = content_sha256(result_obj)
    if supplied_result_sha != expected_result_sha:
        raise ExecutionTransitionError("recompute_result_content_hash_mismatch")
    apply_execution_transition_schema(conn)
    assert_connection_target_capacity(conn)
    with _transition_transaction(conn):
        row = conn.execute(
            """
            SELECT request_id,order_intent_id,scope_json,inventory_version,
                   order_state_version,state,content_sha256,result_json,
                   result_content_sha256,completion_sha256,applied_at,
                   request_json,created_at
            FROM basket_target_post_fill_recompute_requests
            WHERE request_id=?
            """,
            (rid,),
        ).fetchone()
        if row is None:
            raise ExecutionTransitionError("post_fill_recompute_request_missing")
        stored_scope = _canonical_scope(_mapping("stored_scope", row[2]))
        stored_request = _mapping("stored_request", row[11])
        if str(stored_request.get("request_id") or "") != rid:
            raise ExecutionTransitionError(
                "post_fill_recompute_stored_request_id_mismatch"
            )
        request_identity = {
            key: value
            for key, value in stored_request.items()
            if key != "request_id"
        }
        if content_sha256(request_identity) != str(row[6]):
            raise ExecutionTransitionError(
                "post_fill_recompute_stored_request_hash_mismatch"
            )
        source_binding_sha = post_fill_recompute_binding_sha256(
            stored_request
        )
        if datetime.fromisoformat(
            completed_at.replace("Z", "+00:00")
        ) < datetime.fromisoformat(
            _timestamp(
                "post_fill_recompute_created_at",
                row[12],
            ).replace("Z", "+00:00")
        ):
            raise ExecutionTransitionError(
                "post_fill_recompute_completion_clock_rewind"
            )
        if _sha(
            "recompute_result_source_request_binding_sha256",
            result_obj.get("source_request_binding_sha256"),
        ) != source_binding_sha:
            raise ExecutionTransitionError(
                "post_fill_recompute_result_source_binding_mismatch"
            )
        stored_result = None
        if row[7] is not None:
            stored_result = _mapping("stored_result", row[7])
        same_binding = (
            stored_scope == normalized_scope
            and str(row[3]) == inv
            and str(row[4]) == order
        )
        if str(row[5]) == "APPLIED":
            same_result = (
                same_binding
                and str(row[8] or "") == supplied_result_sha
                and stored_result == result_obj
            )
            if not same_result:
                raise ExecutionTransitionError("post_fill_recompute_completion_conflict")
            return {
                "schema": RECOMPUTE_RESULT_SCHEMA,
                "request_id": rid,
                "state": "APPLIED",
                "idempotent": True,
                "result_content_sha256": supplied_result_sha,
                "completion_sha256": str(row[9]),
                "applied_at": str(row[10]),
                "places_orders": False,
                "calls_order_gateway": False,
            }
        if str(row[5]) != "PENDING":
            raise ExecutionTransitionError("post_fill_recompute_request_state_invalid")
        if not same_binding:
            raise ExecutionTransitionError("post_fill_recompute_binding_mismatch")
        completion_identity = {
            "schema": RECOMPUTE_RESULT_SCHEMA,
            "request_id": rid,
            "request_content_sha256": str(row[6]),
            "scope": normalized_scope,
            "inventory_version": inv,
            "order_state_version": order,
            "source_request_binding_sha256": source_binding_sha,
            "result_content_sha256": supplied_result_sha,
            "result": result_obj,
        }
        completion_sha = content_sha256(completion_identity)
        updated = conn.execute(
            """
            UPDATE basket_target_post_fill_recompute_requests
               SET state='APPLIED',result_json=?,result_content_sha256=?,
                   completion_sha256=?,applied_at=?
             WHERE request_id=? AND state='PENDING'
            """,
            (canonical_json(result_obj), supplied_result_sha, completion_sha, completed_at, rid),
        ).rowcount
        if int(updated) != 1:
            raise ExecutionTransitionError("post_fill_recompute_apply_race")
        return {
            "schema": RECOMPUTE_RESULT_SCHEMA,
            "request_id": rid,
            "state": "APPLIED",
            "idempotent": False,
            "result_content_sha256": supplied_result_sha,
            "completion_sha256": completion_sha,
            "applied_at": completed_at,
            "places_orders": False,
            "calls_order_gateway": False,
        }


def load_post_fill_recompute_requests(
    conn: sqlite3.Connection,
    *,
    order_intent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read bounded pending requests for the worker's future consumption."""

    apply_execution_transition_schema(conn)
    if order_intent_id:
        rows = conn.execute(
            "SELECT request_json FROM basket_target_post_fill_recompute_requests WHERE state='PENDING' AND order_intent_id=? ORDER BY created_at,request_id LIMIT 128",
            (_text("order_intent_id", order_intent_id),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT request_json FROM basket_target_post_fill_recompute_requests WHERE state='PENDING' ORDER BY created_at,request_id LIMIT 128"
        ).fetchall()
    return [json.loads(str(row[0])) for row in rows]


__all__ = [
    "AUTHORITATIVE_TYPES",
    "FULL_FILL_TYPES",
    "RECEIPT_SCHEMA",
    "RECEIPT_TYPES",
    "RECOMPUTE_SCHEMA",
    "SCHEMA",
    "ExecutionTransitionError",
    "apply_execution_receipt",
    "apply_execution_transition_schema",
    "build_no_live_execution_receipt",
    "can_submit_fok",
    "complete_post_fill_recompute_request",
    "load_post_fill_recompute_requests",
    "post_fill_recompute_binding_sha256",
    "scope_sha256",
]
