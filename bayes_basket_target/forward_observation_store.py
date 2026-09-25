"""Bounded target-local materialized store for forward no-live evidence.

``auto_trade_decisions`` remains the sole decision authority.  This database
is a replayable projection used by OOS attribution; it cannot authorize or
submit orders and never reads the live/shared database.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .cash_phase_forward_observation import (
    ForwardObservationError,
    NO_LIVE_FIELDS,
    SCHEMA as PACKET_SCHEMA,
    validate_forward_observation_row,
)
from .contracts import ContractError, content_sha256
from .contracts import canonical_json
from .market_registry import settlement_timezone_for_scope
from .resource_bounds import (
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    bounded_json_bytes,
)
from .canonical_contract import FORMAL_TRUTH_LANES_SQL, FORMAL_TRUTH_ROLES


STORE_SCHEMA = "bayes_basket_target_forward_observation_store_v2"
STORE_SCHEMA_VERSION = 2
MAX_STORE_BYTES = 64 * 1024 * 1024
MAX_STORE_WAL_BYTES = 16 * 1024 * 1024
MAX_BATCH_ROWS = 256
MAX_BATCH_JSON_BYTES = 16 * 1024 * 1024
MAX_LABEL_BATCH_ROWS = 64
MAX_QUARANTINE_BATCH_ROWS = 256
MAX_FALSE_QUARANTINE_REPAIR_ROWS = 32
MAX_RETENTION_BATCH_ROWS = 256
MAX_ERROR_CODE_BYTES = 128
MAX_LINEAGE_PROOF_BYTES = 32 * 1024
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_ERROR_CODE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
DEFAULT_STORE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "runtime"
    / "bayes_basket_target_cash_phase"
).resolve()
DEFAULT_STORE_PATH = DEFAULT_STORE_ROOT / "forward_observations.sqlite3"
FALSE_QUARANTINE_REPAIRABLE_ERRORS = frozenset(
    {"cash_phase_forward_binding_scope_or_clock_mismatch"}
)


DDL = """
CREATE TABLE IF NOT EXISTS forward_store_meta (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  schema_name TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=0),
  last_source_rowid INTEGER NOT NULL CHECK(last_source_rowid>=0),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forward_observation (
  observation_key TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL UNIQUE,
  cycle_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  phase TEXT NOT NULL CHECK(phase IN ('HEAVY','STANDARD','TAIL')),
  decision_at_utc TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  observation_sha256 TEXT NOT NULL UNIQUE CHECK(length(observation_sha256)=64),
  observation_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('ACTIVE','SETTLED')),
  materialized_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS forward_observation_scope_idx
ON forward_observation(station_id,event_date,phase,decision_at_utc);

CREATE TABLE IF NOT EXISTS forward_ingest_state (
  decision_id TEXT PRIMARY KEY,
  source_decision_sha256 TEXT NOT NULL CHECK(length(source_decision_sha256)=64),
  source_rowid INTEGER NOT NULL CHECK(source_rowid>0),
  state TEXT NOT NULL CHECK(state IN ('COMMITTED','QUARANTINED')),
  error_code TEXT,
  first_seen_at_utc TEXT NOT NULL,
  last_seen_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forward_ingest_repair (
  decision_id TEXT PRIMARY KEY,
  source_decision_sha256 TEXT NOT NULL CHECK(length(source_decision_sha256)=64),
  source_rowid INTEGER NOT NULL UNIQUE CHECK(source_rowid>0),
  prior_error_code TEXT NOT NULL,
  observation_key TEXT NOT NULL UNIQUE,
  observation_sha256 TEXT NOT NULL UNIQUE CHECK(length(observation_sha256)=64),
  repaired_at_utc TEXT NOT NULL,
  repair_receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(repair_receipt_sha256)=64)
);

CREATE TABLE IF NOT EXISTS forward_settlement_label (
  observation_key TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>=1),
  truth_id TEXT,
  truth_sha256 TEXT CHECK(truth_sha256 IS NULL OR length(truth_sha256)=64),
  official_tmax_c REAL,
  winning_bucket TEXT NOT NULL,
  labeled_at_utc TEXT NOT NULL,
  settlement_closed_at_utc TEXT,
  settlement_authority TEXT NOT NULL,
  authority_scope TEXT,
  source_fact_id TEXT,
  source_label_id TEXT,
  source_label_content_sha256 TEXT
    CHECK(source_label_content_sha256 IS NULL OR length(source_label_content_sha256)=64),
  supersedes_label_id TEXT,
  outcome_json TEXT,
  official_settlement_sha256 TEXT NOT NULL
    CHECK(length(official_settlement_sha256)=64),
  settlement_timezone TEXT NOT NULL,
  label_sha256 TEXT NOT NULL UNIQUE CHECK(length(label_sha256)=64),
  PRIMARY KEY(observation_key,revision),
  FOREIGN KEY(observation_key) REFERENCES forward_observation(observation_key)
);

CREATE TABLE IF NOT EXISTS forward_label_ingest_state (
  source_label_rowid INTEGER PRIMARY KEY CHECK(source_label_rowid>0),
  source_label_id TEXT NOT NULL UNIQUE,
  source_label_content_sha256 TEXT NOT NULL
    CHECK(length(source_label_content_sha256)=64),
  state TEXT NOT NULL DEFAULT 'MATERIALIZED'
    CHECK(state IN ('MATERIALIZED','SKIPPED_INELIGIBLE')),
  skip_reason TEXT,
  materialized_at_utc TEXT NOT NULL
);

-- A skipped label is an immutable ingest event.  A later observation is
-- represented by a second immutable event instead of updating the first row.
CREATE TABLE IF NOT EXISTS forward_label_ingest_transition (
  source_label_rowid INTEGER NOT NULL CHECK(source_label_rowid>0),
  source_label_id TEXT NOT NULL,
  source_label_content_sha256 TEXT NOT NULL
    CHECK(length(source_label_content_sha256)=64),
  prior_state TEXT NOT NULL CHECK(prior_state='SKIPPED_INELIGIBLE'),
  state TEXT NOT NULL CHECK(state='MATERIALIZED'),
  materialized_at_utc TEXT NOT NULL,
  transition_sha256 TEXT NOT NULL UNIQUE CHECK(length(transition_sha256)=64),
  PRIMARY KEY(source_label_rowid,state)
);
CREATE INDEX IF NOT EXISTS forward_label_transition_rowid_idx
ON forward_label_ingest_transition(source_label_rowid,state);

CREATE TABLE IF NOT EXISTS forward_retention_tombstone (
  observation_key TEXT PRIMARY KEY,
  observation_sha256 TEXT NOT NULL CHECK(length(observation_sha256)=64),
  purged_at_utc TEXT NOT NULL,
  purge_receipt_sha256 TEXT NOT NULL CHECK(length(purge_receipt_sha256)=64)
);

CREATE TRIGGER IF NOT EXISTS forward_observation_no_update
BEFORE UPDATE ON forward_observation BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS forward_label_no_update
BEFORE UPDATE ON forward_settlement_label BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS forward_label_ingest_no_update
BEFORE UPDATE ON forward_label_ingest_state BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS forward_label_ingest_no_delete
BEFORE DELETE ON forward_label_ingest_state BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS forward_label_transition_no_update
BEFORE UPDATE ON forward_label_ingest_transition BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS forward_label_transition_no_delete
BEFORE DELETE ON forward_label_ingest_transition BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS forward_tombstone_no_update
BEFORE UPDATE ON forward_retention_tombstone BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS forward_tombstone_no_delete
BEFORE DELETE ON forward_retention_tombstone BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS forward_ingest_repair_no_update
BEFORE UPDATE ON forward_ingest_repair BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS forward_ingest_repair_no_delete
BEFORE DELETE ON forward_ingest_repair BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS forward_ingest_no_update
BEFORE UPDATE ON forward_ingest_state BEGIN SELECT RAISE(ABORT,'append_only'); END;

DROP TRIGGER IF EXISTS forward_observation_no_delete;
DROP TRIGGER IF EXISTS forward_ingest_no_delete;
DROP TRIGGER IF EXISTS forward_label_no_delete;
CREATE TRIGGER forward_observation_no_delete
BEFORE DELETE ON forward_observation
WHEN forward_store_retention_delete_authorized() <> 1
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER forward_ingest_no_delete
BEFORE DELETE ON forward_ingest_state
WHEN forward_store_retention_delete_authorized() <> 1
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER forward_label_no_delete
BEFORE DELETE ON forward_settlement_label
WHEN forward_store_retention_delete_authorized() <> 1
BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


class ForwardObservationStoreError(ContractError):
    """A store identity, resource, concurrency, or retention violation."""


class _ForwardStoreConnection(sqlite3.Connection):
    """Connection subclass carrying the private retention authorization flag."""


def resolve_forward_store_path(
    path: Path | str = DEFAULT_STORE_PATH,
    *,
    allowed_root: Path | str = DEFAULT_STORE_ROOT,
) -> Path:
    candidate = Path(path).expanduser()
    absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    if absolute.is_symlink():
        raise ForwardObservationStoreError("forward_store_symlink_forbidden")
    destination = absolute.resolve()
    root_candidate = Path(allowed_root).expanduser()
    root_absolute = (
        root_candidate
        if root_candidate.is_absolute()
        else Path.cwd() / root_candidate
    )
    if root_absolute.is_symlink():
        raise ForwardObservationStoreError("forward_store_root_symlink_forbidden")
    root = root_absolute.resolve()
    if not destination.is_relative_to(root):
        raise ForwardObservationStoreError("forward_store_outside_target_root")
    if destination.suffix.lower() not in {".sqlite3", ".sqlite"}:
        raise ForwardObservationStoreError("forward_store_sqlite_suffix_required")
    return destination


def _free_bytes(path: Path) -> int:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        return int(shutil.disk_usage(candidate).free)
    except OSError as exc:
        raise ForwardObservationStoreError(
            "forward_store_disk_probe_failed"
        ) from exc


def _assert_capacity(path: Path, *, projected_bytes: int = 0) -> None:
    database_bytes = int(path.stat().st_size) if path.is_file() else 0
    wal = path.with_name(path.name + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.is_file() else 0
    if database_bytes + max(0, int(projected_bytes)) > MAX_STORE_BYTES:
        raise ForwardObservationStoreError("forward_store_bytes_exceeded")
    if wal_bytes > MAX_STORE_WAL_BYTES:
        raise ForwardObservationStoreError("forward_store_wal_bytes_exceeded")
    if _free_bytes(path.parent) < MIN_TARGET_FREE_BYTES + max(
        0, int(projected_bytes)
    ):
        raise ForwardObservationStoreError(
            "forward_store_min_free_space_guard"
        )


def _meta_identity(
    *,
    generation: int,
    last_source_rowid: int,
    updated_at_utc: str,
) -> dict[str, Any]:
    return {
        "schema_name": STORE_SCHEMA,
        "schema_version": STORE_SCHEMA_VERSION,
        "generation": int(generation),
        "last_source_rowid": int(last_source_rowid),
        "updated_at_utc": str(updated_at_utc),
    }


def _require_sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if _HEX64.fullmatch(result) is None:
        raise ForwardObservationStoreError(f"{name}_invalid")
    return result


def _require_timestamp(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ForwardObservationStoreError(f"{name}_missing")
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForwardObservationStoreError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardObservationStoreError(f"{name}_timezone_required")
    return result


def _require_nonnegative_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ForwardObservationStoreError(f"{name}_invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ForwardObservationStoreError(f"{name}_invalid") from exc
    if result < 0:
        raise ForwardObservationStoreError(f"{name}_invalid")
    return result


def _require_positive_int(name: str, value: Any) -> int:
    result = _require_nonnegative_int(name, value)
    if result <= 0:
        raise ForwardObservationStoreError(f"{name}_invalid")
    return result


def _ensure_schema(connection: sqlite3.Connection) -> None:
    """Apply target-local additive schema repair and trigger hardening."""

    connection.executescript(DDL)
    existing = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(forward_settlement_label)"
        ).fetchall()
    }
    additive = {
        "truth_id": "TEXT",
        "truth_sha256": "TEXT",
        "official_tmax_c": "REAL",
        "settlement_closed_at_utc": "TEXT",
        "authority_scope": "TEXT",
        "source_fact_id": "TEXT",
        "source_label_id": "TEXT",
        "source_label_content_sha256": "TEXT",
        "supersedes_label_id": "TEXT",
        "outcome_json": "TEXT",
    }
    for name, sql_type in additive.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE forward_settlement_label ADD COLUMN {name} {sql_type}"
            )
    label_ingest_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(forward_label_ingest_state)"
        ).fetchall()
    }
    if "state" not in label_ingest_columns:
        connection.execute(
            "ALTER TABLE forward_label_ingest_state ADD COLUMN "
            "state TEXT NOT NULL DEFAULT 'MATERIALIZED' "
            "CHECK(state IN ('MATERIALIZED','SKIPPED_INELIGIBLE'))"
        )
    if "skip_reason" not in label_ingest_columns:
        connection.execute(
            "ALTER TABLE forward_label_ingest_state ADD COLUMN "
            "skip_reason TEXT"
        )


def _open_store(path: Path, *, create: bool) -> sqlite3.Connection:
    _assert_capacity(path)
    if not create and not path.is_file():
        raise ForwardObservationStoreError("forward_store_missing")
    path.parent.mkdir(parents=True, exist_ok=True)
    delete_gate = {"authorized": False}
    connection = sqlite3.connect(
        path,
        timeout=5.0,
        factory=_ForwardStoreConnection,
    )
    connection.create_function(
        "forward_store_retention_delete_authorized",
        0,
        lambda: 1 if delete_gate["authorized"] else 0,
    )
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    if create:
        _ensure_schema(connection)
    # This mutable flag is private to this connection.  Only the retention
    # transaction may enable it; ordinary SQL remains append-only.
    connection._forward_store_delete_gate = delete_gate  # type: ignore[attr-defined]
    return connection


def _set_retention_delete_authorized(
    connection: sqlite3.Connection,
    authorized: bool,
) -> None:
    try:
        gate = connection._forward_store_delete_gate  # type: ignore[attr-defined]
    except AttributeError as exc:
        raise ForwardObservationStoreError(
            "forward_store_retention_connection_invalid"
        ) from exc
    gate["authorized"] = bool(authorized)


def _read_meta_for_write(
    connection: sqlite3.Connection,
    *,
    expected_generation: int,
) -> tuple[int, int]:
    row = connection.execute(
        "SELECT generation,last_source_rowid FROM forward_store_meta "
        "WHERE singleton=1"
    ).fetchone()
    if row is None:
        if expected_generation != 0:
            raise ForwardObservationStoreError(
                "forward_store_generation_cas_mismatch"
            )
        return 0, 0
    current_generation = int(row[0])
    current_cursor = int(row[1])
    if current_generation != expected_generation:
        raise ForwardObservationStoreError(
            "forward_store_generation_cas_mismatch"
        )
    return current_generation, current_cursor


def _write_meta(
    connection: sqlite3.Connection,
    *,
    generation: int,
    last_source_rowid: int,
    updated_at_utc: str,
) -> str:
    identity = _meta_identity(
        generation=generation,
        last_source_rowid=last_source_rowid,
        updated_at_utc=updated_at_utc,
    )
    meta_sha = content_sha256(identity)
    connection.execute(
        "INSERT INTO forward_store_meta VALUES(1,?,?,?,?,?,?) "
        "ON CONFLICT(singleton) DO UPDATE SET "
        "schema_name=excluded.schema_name,"
        "schema_version=excluded.schema_version,"
        "generation=excluded.generation,"
        "last_source_rowid=excluded.last_source_rowid,"
        "content_sha256=excluded.content_sha256,"
        "updated_at_utc=excluded.updated_at_utc",
        (
            STORE_SCHEMA,
            STORE_SCHEMA_VERSION,
            generation,
            last_source_rowid,
            meta_sha,
            updated_at_utc,
        ),
    )
    return meta_sha


def _assert_source_connection(connection: Any) -> sqlite3.Connection:
    if not isinstance(connection, sqlite3.Connection):
        raise ForwardObservationStoreError(
            "forward_store_source_connection_required"
        )
    return connection


def _repair_state_count_sql(connection: sqlite3.Connection) -> tuple[str, str]:
    """Return additive-schema-compatible unresolved/history count fragments."""

    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='forward_ingest_repair'"
    ).fetchone()
    if exists is None:
        return (
            "(SELECT COUNT(*) FROM forward_ingest_state "
            "WHERE state='QUARANTINED')",
            "0",
        )
    return (
        "(SELECT COUNT(*) FROM forward_ingest_state "
        "WHERE state='QUARANTINED' AND NOT EXISTS("
        "SELECT 1 FROM forward_ingest_repair r "
        "WHERE r.decision_id=forward_ingest_state.decision_id))",
        "(SELECT COUNT(*) FROM forward_ingest_repair)",
    )


def read_forward_store_state(
    path: Path | str,
    *,
    allowed_root: Path | str = DEFAULT_STORE_ROOT,
) -> dict[str, Any]:
    destination = resolve_forward_store_path(path, allowed_root=allowed_root)
    if not destination.is_file():
        return {
            "schema": STORE_SCHEMA,
            "generation": 0,
            "last_source_rowid": 0,
            "last_source_label_rowid": 0,
            "observation_count": 0,
            "quarantine_count": 0,
            "historical_quarantine_count": 0,
            "repaired_quarantine_count": 0,
            "label_count": 0,
            "skipped_ineligible_label_count": 0,
            "tombstone_count": 0,
        }
    with _open_store(destination, create=False) as connection:
        meta = connection.execute(
            "SELECT schema_name,schema_version,generation,last_source_rowid,"
            "content_sha256,updated_at_utc FROM forward_store_meta "
            "WHERE singleton=1"
        ).fetchone()
        if meta is None:
            raise ForwardObservationStoreError("forward_store_meta_missing")
        identity = _meta_identity(
            generation=int(meta[2]),
            last_source_rowid=int(meta[3]),
            updated_at_utc=str(meta[5]),
        )
        if (
            str(meta[0]) != STORE_SCHEMA
            or int(meta[1]) != STORE_SCHEMA_VERSION
            or str(meta[4]) != content_sha256(identity)
        ):
            raise ForwardObservationStoreError("forward_store_meta_drift")
        unresolved_sql, repaired_sql = _repair_state_count_sql(connection)
        label_ingest_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(forward_label_ingest_state)"
            ).fetchall()
        }
        transition_table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='forward_label_ingest_transition'"
        ).fetchone() is not None
        skipped_label_sql = (
            "(SELECT COUNT(*) FROM forward_label_ingest_state s "
            " WHERE s.state='SKIPPED_INELIGIBLE' "
            + (
                "AND NOT EXISTS(SELECT 1 FROM forward_label_ingest_transition t "
                "WHERE t.source_label_rowid=s.source_label_rowid "
                "AND t.state='MATERIALIZED')"
                if transition_table_exists
                else ""
            )
            + ")"
            if "state" in label_ingest_columns
            else "0"
        )
        counts = connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM forward_observation),"
            f"{unresolved_sql},"
            "(SELECT COUNT(*) FROM forward_ingest_state "
            " WHERE state='QUARANTINED'),"
            f"{repaired_sql},"
            "(SELECT COUNT(*) FROM forward_retention_tombstone),"
            "(SELECT COUNT(*) FROM forward_settlement_label),"
            f"{skipped_label_sql},"
            "(SELECT COALESCE(MAX(source_label_rowid),0) "
            " FROM forward_label_ingest_state)"
        ).fetchone()
        return {
            "schema": STORE_SCHEMA,
            "generation": int(meta[2]),
            "last_source_rowid": int(meta[3]),
            "observation_count": int(counts[0]),
            "quarantine_count": int(counts[1]),
            "historical_quarantine_count": int(counts[2]),
            "repaired_quarantine_count": int(counts[3]),
            "tombstone_count": int(counts[4]),
            "label_count": int(counts[5]),
            "skipped_ineligible_label_count": int(counts[6]),
            "last_source_label_rowid": int(counts[7]),
            "content_sha256": str(meta[4]),
            "updated_at_utc": str(meta[5]),
        }


def initialize_forward_observation_store(
    path: Path | str,
    *,
    expected_generation: int = 0,
    initialized_at_utc: str,
    allowed_root: Path | str = DEFAULT_STORE_ROOT,
) -> dict[str, Any]:
    """Create the target-local v2 schema/meta even when no rows are eligible.

    Initialization is a generation-CAS no-op: a new store gets generation 0,
    cursor 0 and a content-addressed meta row; re-running with the same
    expected generation is idempotent and never advances the generation.
    """

    generation = _require_nonnegative_int(
        "forward_store_expected_generation", expected_generation
    )
    initialized_at = _require_timestamp(
        "forward_store_initialized_at_utc", initialized_at_utc
    )
    destination = resolve_forward_store_path(path, allowed_root=allowed_root)
    _assert_capacity(destination, projected_bytes=MAX_BATCH_JSON_BYTES // 32)
    with _open_store(destination, create=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT schema_name,schema_version,generation,last_source_rowid,"
                "content_sha256,updated_at_utc FROM forward_store_meta "
                "WHERE singleton=1"
            ).fetchone()
            if row is None:
                if generation != 0:
                    raise ForwardObservationStoreError(
                        "forward_store_generation_cas_mismatch"
                    )
                meta_sha = _write_meta(
                    connection,
                    generation=0,
                    last_source_rowid=0,
                    updated_at_utc=initialized_at,
                )
                state = "initialized"
                current_generation = 0
            else:
                current_generation = int(row[2])
                if current_generation != generation:
                    raise ForwardObservationStoreError(
                        "forward_store_generation_cas_mismatch"
                    )
                identity = _meta_identity(
                    generation=current_generation,
                    last_source_rowid=int(row[3]),
                    updated_at_utc=str(row[5]),
                )
                if (
                    str(row[0]) != STORE_SCHEMA
                    or int(row[1]) != STORE_SCHEMA_VERSION
                    or str(row[4]) != content_sha256(identity)
                ):
                    raise ForwardObservationStoreError("forward_store_meta_drift")
                meta_sha = str(row[4])
                state = "already_initialized"
            counts = connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM forward_observation),"
                "(SELECT COUNT(*) FROM forward_ingest_state "
                " WHERE state='QUARANTINED' AND NOT EXISTS("
                " SELECT 1 FROM forward_ingest_repair r"
                " WHERE r.decision_id=forward_ingest_state.decision_id)),"
                "(SELECT COUNT(*) FROM forward_ingest_state "
                " WHERE state='QUARANTINED'),"
                "(SELECT COUNT(*) FROM forward_ingest_repair),"
                "(SELECT COUNT(*) FROM forward_retention_tombstone),"
                "(SELECT COUNT(*) FROM forward_settlement_label),"
                "(SELECT COUNT(*) FROM forward_label_ingest_state s "
                " WHERE s.state='SKIPPED_INELIGIBLE' "
                "AND NOT EXISTS(SELECT 1 FROM forward_label_ingest_transition t "
                "WHERE t.source_label_rowid=s.source_label_rowid "
                "AND t.state='MATERIALIZED')) ,"
                "(SELECT COALESCE(MAX(source_label_rowid),0) "
                " FROM forward_label_ingest_state)"
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    _assert_capacity(destination)
    return {
        "schema": STORE_SCHEMA,
        "state": state,
        "generation": current_generation,
        "last_source_rowid": 0 if state == "initialized" else int(row[3]),
        "content_sha256": meta_sha,
        "store_path": str(destination),
        "observation_count": int(counts[0]),
        "quarantine_count": int(counts[1]),
        "historical_quarantine_count": int(counts[2]),
        "repaired_quarantine_count": int(counts[3]),
        "tombstone_count": int(counts[4]),
        "label_count": int(counts[5]),
        "skipped_ineligible_label_count": int(counts[6]),
        "last_source_label_rowid": int(counts[7]),
        "execution_mode": "production_no_live",
        "authorizes_order": False,
        "orders_submitted": 0,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }


def commit_forward_observation_batch(
    path: Path | str,
    *,
    records: Sequence[Mapping[str, Any]],
    expected_generation: int,
    last_source_rowid: int,
    materialized_at_utc: str,
    allowed_root: Path | str = DEFAULT_STORE_ROOT,
) -> dict[str, Any]:
    if (
        isinstance(records, (str, bytes, bytearray))
        or not isinstance(records, Sequence)
        or len(records) > MAX_BATCH_ROWS
    ):
        raise ForwardObservationStoreError("forward_store_batch_invalid")
    destination = resolve_forward_store_path(path, allowed_root=allowed_root)
    normalized: list[tuple[dict[str, Any], str, str, int, bytes]] = []
    total_bytes = 0
    for record in records:
        if not isinstance(record, Mapping):
            raise ForwardObservationStoreError(
                "forward_store_record_mapping_required"
            )
        try:
            row = validate_forward_observation_row(record.get("observation"))
        except ForwardObservationError as exc:
            raise ForwardObservationStoreError(
                "forward_store_observation_invalid:" + str(exc)
            ) from exc
        decision_id = str(record.get("decision_id") or "").strip()
        decision_sha = _require_sha(
            "forward_store_decision_sha256", record.get("decision_sha256")
        )
        source_rowid = _require_positive_int(
            "forward_store_source_rowid", record.get("source_rowid")
        )
        if (
            not decision_id
            or len(decision_sha) != 64
            or source_rowid <= 0
            or row["decision_id"] != decision_id
        ):
            raise ForwardObservationStoreError(
                "forward_store_source_identity_invalid"
            )
        raw = bounded_json_bytes(row, limit=MAX_BATCH_JSON_BYTES)
        total_bytes += len(raw)
        if total_bytes > MAX_BATCH_JSON_BYTES:
            raise ForwardObservationStoreError(
                "forward_store_batch_bytes_exceeded"
            )
        normalized.append((row, decision_id, decision_sha, source_rowid, raw))
    assert_process_memory_budget(projected_bytes=total_bytes * 3)
    _assert_capacity(destination, projected_bytes=total_bytes * 2)
    with _open_store(destination, create=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current_generation, current_cursor = _read_meta_for_write(
                connection,
                expected_generation=_require_nonnegative_int(
                    "forward_store_expected_generation", expected_generation
                ),
            )
            inserted = 0
            replayed = 0
            for row, decision_id, decision_sha, source_rowid, raw in normalized:
                prior_ingest = connection.execute(
                    "SELECT source_decision_sha256,state FROM "
                    "forward_ingest_state WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
                if prior_ingest is not None:
                    if (
                        str(prior_ingest[0]) != decision_sha
                        or str(prior_ingest[1]) != "COMMITTED"
                    ):
                        raise ForwardObservationStoreError(
                            "forward_store_ingest_identity_conflict"
                        )
                    replayed += 1
                    continue
                tombstone = connection.execute(
                    "SELECT observation_sha256 FROM forward_retention_tombstone "
                    "WHERE observation_key=?",
                    (row["observation_key"],),
                ).fetchone()
                if tombstone is not None:
                    raise ForwardObservationStoreError(
                        "forward_store_observation_tombstoned"
                    )
                prior_row = connection.execute(
                    "SELECT decision_id,observation_sha256 FROM "
                    "forward_observation WHERE observation_key=?",
                    (row["observation_key"],),
                ).fetchone()
                if prior_row is not None:
                    if (
                        str(prior_row[0]) != decision_id
                        or str(prior_row[1]) != row["observation_sha256"]
                    ):
                        raise ForwardObservationStoreError(
                            "forward_store_observation_identity_conflict"
                        )
                    replayed += 1
                else:
                    connection.execute(
                        "INSERT INTO forward_observation("
                        "observation_key,decision_id,cycle_id,station_id,"
                        "event_date,phase,decision_at_utc,settlement_timezone,"
                        "observation_sha256,observation_json,state,"
                        "materialized_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            row["observation_key"],
                            decision_id,
                            row["cycle_id"],
                            row["station_id"],
                            row["event_date"],
                            row["phase"],
                            row["decision_at_utc"],
                            row["settlement_timezone"],
                            row["observation_sha256"],
                            raw.decode("utf-8"),
                            "ACTIVE",
                            materialized_at_utc,
                        ),
                    )
                    inserted += 1
                connection.execute(
                    "INSERT INTO forward_ingest_state VALUES(?,?,?,?,?,?,?)",
                    (
                        decision_id,
                        decision_sha,
                        source_rowid,
                        "COMMITTED",
                        None,
                        materialized_at_utc,
                        materialized_at_utc,
                    ),
                )
            next_generation = current_generation + 1
            next_cursor = max(
                current_cursor,
                int(last_source_rowid),
                *(item[3] for item in normalized),
            )
            meta_sha = _write_meta(
                connection,
                generation=next_generation,
                last_source_rowid=next_cursor,
                updated_at_utc=_require_timestamp(
                    "forward_store_materialized_at_utc", materialized_at_utc
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM forward_observation"
            ).fetchone()[0]
        )
    _assert_capacity(destination)
    return {
        "schema": STORE_SCHEMA,
        "state": "committed" if inserted else "already_current",
        "generation": next_generation,
        "last_source_rowid": next_cursor,
        "inserted": inserted,
        "replayed": replayed,
        "observation_count": count,
        "store_path": str(destination),
        "content_sha256": meta_sha,
        "execution_mode": "production_no_live",
        "authorizes_order": False,
        "orders_submitted": 0,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }


def _source_row_mapping(cursor: sqlite3.Cursor, row: Any) -> dict[str, Any]:
    columns = tuple(item[0] for item in (cursor.description or ()))
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    return {name: row[index] for index, name in enumerate(columns)}


def _source_table_exists(
    connection: sqlite3.Connection,
    table: str,
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _source_registry_timezone(
    connection: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
) -> str:
    if not _source_table_exists(connection, "basket_target_market_registry"):
        raise ForwardObservationStoreError(
            "forward_store_registry_table_missing"
        )
    # The registry's rows while it carries the scope, the station's bound
    # timezone once retention has retired them (``settlement_timezone_for_scope``);
    # without that every settled day older than three days failed here, and
    # one such row ended the whole batch.
    timezone_name = settlement_timezone_for_scope(
        connection, station_id=station_id, event_date=event_date
    )
    if not timezone_name:
        raise ForwardObservationStoreError(
            "forward_store_registry_settlement_timezone_not_unique"
        )
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise ForwardObservationStoreError(
            "forward_store_registry_settlement_timezone_invalid"
        ) from exc
    return timezone_name


def _label_identity_from_source(row: Mapping[str, Any]) -> dict[str, Any]:
    required = (
        "fact_id",
        "truth_id",
        "truth_revision_no",
        "truth_sha256",
        "official_tmax_c",
        "winning_bucket",
        "settlement_closed_at_utc",
        "labeled_at_utc",
        "outcome_json",
        "authority_scope",
        "supersedes_label_id",
    )
    missing = [field for field in required if field not in row]
    if missing:
        raise ForwardObservationStoreError(
            "forward_store_source_label_fields_missing:" + ",".join(missing)
        )
    try:
        revision = int(row["truth_revision_no"])
    except (TypeError, ValueError) as exc:
        raise ForwardObservationStoreError(
            "forward_store_source_label_revision_invalid"
        ) from exc
    if revision < 0:
        raise ForwardObservationStoreError(
            "forward_store_source_label_revision_invalid"
        )
    truth_sha = _require_sha(
        "forward_store_source_label_truth_sha256", row["truth_sha256"]
    )
    try:
        tmax = float(row["official_tmax_c"])
    except (TypeError, ValueError) as exc:
        raise ForwardObservationStoreError(
            "forward_store_source_label_tmax_invalid"
        ) from exc
    if not math.isfinite(tmax):
        raise ForwardObservationStoreError(
            "forward_store_source_label_tmax_invalid"
        )
    winning_bucket = str(row["winning_bucket"] or "").strip()
    if not winning_bucket:
        raise ForwardObservationStoreError(
            "forward_store_source_label_winning_bucket_missing"
        )
    outcome_json = str(row["outcome_json"] or "")
    if len(outcome_json.encode("utf-8")) > MAX_BATCH_JSON_BYTES:
        raise ForwardObservationStoreError(
            "forward_store_source_label_outcome_resource_limit_exceeded"
        )
    try:
        outcome = json.loads(outcome_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ForwardObservationStoreError(
            "forward_store_source_label_outcome_json_invalid"
        ) from exc
    if not isinstance(outcome, Mapping) or canonical_json(outcome) != outcome_json:
        raise ForwardObservationStoreError(
            "forward_store_source_label_outcome_not_canonical"
        )
    authority_scope = str(row["authority_scope"] or "").strip()
    if authority_scope not in {
        "FORMAL_NATIVE_ELIGIBLE",
        "NO_LIVE_REPLAY_CANDIDATE",
    }:
        raise ForwardObservationStoreError(
            "forward_store_source_label_authority_scope_invalid"
        )
    settlement_closed = _require_timestamp(
        "forward_store_source_label_settlement_closed_at_utc",
        row["settlement_closed_at_utc"],
    )
    labeled_at = _require_timestamp(
        "forward_store_source_label_labeled_at_utc", row["labeled_at_utc"]
    )
    identity = {
        "fact_id": str(row["fact_id"] or "").strip(),
        "truth_id": str(row["truth_id"] or "").strip(),
        "truth_revision_no": revision,
        "truth_sha256": truth_sha,
        "official_tmax_c": tmax,
        "winning_bucket": winning_bucket,
        "settlement_closed_at_utc": settlement_closed,
        "labeled_at_utc": labeled_at,
        "outcome_json": outcome_json,
        "authority_scope": authority_scope,
        "supersedes_label_id": (
            None
            if row["supersedes_label_id"] is None
            else str(row["supersedes_label_id"])
        ),
    }
    expected_sha = content_sha256(
        {"schema": "delta_basket_score_training_label_v2", **identity}
    )
    supplied_sha = _require_sha(
        "forward_store_source_label_content_sha256",
        row.get("content_sha256"),
    )
    supplied_id = str(row.get("label_id") or "").strip()
    if (
        supplied_sha != expected_sha
        or supplied_id != f"d2label_{expected_sha[:40]}"
    ):
        raise ForwardObservationStoreError(
            "forward_store_source_label_content_address_mismatch"
        )
    if not identity["fact_id"] or not identity["truth_id"]:
        raise ForwardObservationStoreError(
            "forward_store_source_label_identity_missing"
        )
    return {
        **identity,
        "label_id": supplied_id,
        "content_sha256": supplied_sha,
    }


def _validate_source_label(
    source: sqlite3.Connection,
    *,
    registry: sqlite3.Connection,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    label = _label_identity_from_source(row)
    fact = row
    station_id = str(
        fact.get("fact_station_id") or fact.get("station_id") or ""
    ).strip()
    event_date = str(fact.get("fact_event_date") or fact.get("event_date") or "").strip()
    decision_id = str(fact.get("fact_decision_id") or fact.get("decision_id") or "").strip()
    fact_timezone = str(
        fact.get("fact_settlement_timezone")
        or fact.get("settlement_timezone")
        or ""
    ).strip()
    if not station_id or not event_date or not decision_id or not fact_timezone:
        raise ForwardObservationStoreError(
            "forward_store_source_fact_identity_missing"
        )
    registry_timezone = _source_registry_timezone(
        registry,
        station_id=station_id,
        event_date=event_date,
    )
    if registry_timezone != fact_timezone:
        raise ForwardObservationStoreError(
            "forward_store_label_registry_settlement_timezone_mismatch"
        )
    truth = source.execute(
        "SELECT truth_id,station_id,local_date,revision_no,content_sha256,"
        "authority_scope,qc_status,timestamps_complete "
        "FROM calibration_station_daily_high_truth "
        # Charter 225: a label may bind a settlement-source revision.
        "WHERE truth_id=? AND source_lane IN " + FORMAL_TRUTH_LANES_SQL + " "
        "AND source_role IN (?,?) LIMIT 2",
        (
            label["truth_id"],
            *FORMAL_TRUTH_ROLES,
        ),
    ).fetchall()
    if len(truth) != 1:
        raise ForwardObservationStoreError(
            "forward_store_label_truth_missing_or_ambiguous"
        )
    truth_row = truth[0]
    if (
        str(truth_row[1]) != station_id
        or str(truth_row[2]) != event_date
        or int(truth_row[3]) != label["truth_revision_no"]
        or _require_sha("forward_store_truth_content_sha256", truth_row[4])
        != label["truth_sha256"]
        or str(truth_row[5]) != label["authority_scope"]
        or str(truth_row[6]) != "ACCEPTED"
        or int(truth_row[7]) != 1
    ):
        raise ForwardObservationStoreError("forward_store_label_truth_binding_mismatch")
    return {
        **label,
        "source_label_rowid": _require_positive_int(
            "forward_store_source_label_rowid",
            row.get("source_label_rowid"),
        ),
        "station_id": station_id,
        "event_date": event_date,
        "decision_id": decision_id,
        "settlement_timezone": fact_timezone,
        "official_settlement_sha256": label["truth_sha256"],
        "settlement_authority": "calibration_station_daily_high_truth",
    }


def _label_transition_sha256(item: Mapping[str, Any]) -> str:
    """Return the content address of the one allowed late-label transition."""

    return content_sha256(
        {
            "schema": "bayes_basket_target_forward_label_ingest_transition_v1",
            "source_label_rowid": int(item["source_label_rowid"]),
            "source_label_id": str(item["label_id"]),
            "source_label_content_sha256": str(item["content_sha256"]),
            "prior_state": "SKIPPED_INELIGIBLE",
            "state": "MATERIALIZED",
        }
    )


def _append_label_materialized_transition(
    connection: sqlite3.Connection,
    *,
    item: Mapping[str, Any],
    materialized_at_utc: str,
) -> bool:
    """Append the late materialization event once, without updating history."""

    transition_sha = _label_transition_sha256(item)
    existing = connection.execute(
        "SELECT source_label_id,source_label_content_sha256,prior_state,state,"
        "materialized_at_utc,transition_sha256 "
        "FROM forward_label_ingest_transition "
        "WHERE source_label_rowid=? AND state='MATERIALIZED'",
        (int(item["source_label_rowid"]),),
    ).fetchone()
    if existing is not None:
        if (
            str(existing[0]) != str(item["label_id"])
            or str(existing[1]) != str(item["content_sha256"])
            or str(existing[2]) != "SKIPPED_INELIGIBLE"
            or str(existing[3]) != "MATERIALIZED"
            or str(existing[5]) != transition_sha
        ):
            raise ForwardObservationStoreError(
                "forward_store_label_transition_identity_conflict"
            )
        return False
    existing_hash = connection.execute(
        "SELECT source_label_rowid FROM forward_label_ingest_transition "
        "WHERE transition_sha256=?",
        (transition_sha,),
    ).fetchone()
    if existing_hash is not None:
        if int(existing_hash[0]) != int(item["source_label_rowid"]):
            raise ForwardObservationStoreError(
                "forward_store_label_transition_content_address_conflict"
            )
        return False
    connection.execute(
        "INSERT INTO forward_label_ingest_transition("
        "source_label_rowid,source_label_id,source_label_content_sha256,"
        "prior_state,state,materialized_at_utc,transition_sha256) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            int(item["source_label_rowid"]),
            str(item["label_id"]),
            str(item["content_sha256"]),
            "SKIPPED_INELIGIBLE",
            "MATERIALIZED",
            materialized_at_utc,
            transition_sha,
        ),
    )
    return True


def append_forward_settlement_labels(
    path: Path | str,
    source_connection: sqlite3.Connection | None = None,
    *,
    source_conn: sqlite3.Connection | None = None,
    registry_connection: sqlite3.Connection | None = None,
    expected_generation: int = 0,
    materialized_at_utc: str | None = None,
    as_of_utc: str | None = None,
    max_labels: int = MAX_LABEL_BATCH_ROWS,
    allowed_root: Path | str = DEFAULT_STORE_ROOT,
) -> dict[str, Any]:
    """Copy only exact, content-addressed settled labels into the local store."""

    if source_connection is not None and source_conn is not None:
        if source_connection is not source_conn:
            raise ForwardObservationStoreError("forward_store_source_connection_conflict")
    source = _assert_source_connection(source_connection or source_conn)
    registry = _assert_source_connection(registry_connection or source)
    if isinstance(max_labels, bool) or not 1 <= int(max_labels) <= MAX_LABEL_BATCH_ROWS:
        raise ForwardObservationStoreError("forward_store_label_batch_limit_invalid")
    timestamp = materialized_at_utc or as_of_utc
    if not timestamp:
        raise ForwardObservationStoreError("forward_store_label_materialized_at_missing")
    materialized_at = _require_timestamp(
        "forward_store_label_materialized_at_utc", timestamp
    )
    generation = _require_nonnegative_int(
        "forward_store_expected_generation", expected_generation
    )
    if not _source_table_exists(source, "basket_policy_training_label") or not _source_table_exists(
        source, "basket_policy_training_fact"
    ):
        raise ForwardObservationStoreError("forward_store_training_source_tables_missing")
    destination = resolve_forward_store_path(path, allowed_root=allowed_root)
    with _open_store(destination, create=True) as cursor_connection:
        cursor_row = cursor_connection.execute(
            "SELECT COALESCE(MAX(source_label_rowid),0) "
            "FROM forward_label_ingest_state"
        ).fetchone()
        source_label_cursor = int(cursor_row[0] or 0)
        retry_rows = cursor_connection.execute(
            "SELECT s.source_label_rowid "
            "FROM forward_label_ingest_state s "
            "WHERE s.state='SKIPPED_INELIGIBLE' "
            "AND NOT EXISTS(SELECT 1 FROM forward_label_ingest_transition t "
            "WHERE t.source_label_rowid=s.source_label_rowid "
            "AND t.state='MATERIALIZED') "
            "ORDER BY s.source_label_rowid LIMIT ?",
            (int(max_labels) + 1,),
        ).fetchall()
    retry_rowids = [int(row[0]) for row in retry_rows]
    retry_placeholders = ",".join("?" for _ in retry_rowids)
    source_sql = (
        "SELECT l.rowid AS source_label_rowid,l.*, "
        "f.station_id AS fact_station_id, "
        "f.event_date AS fact_event_date, "
        "f.settlement_timezone AS fact_settlement_timezone, "
        "f.decision_id AS fact_decision_id "
        "FROM basket_policy_training_label l "
        "JOIN basket_policy_training_fact f ON f.fact_id=l.fact_id "
        "WHERE l.rowid>?"
    )
    source_params: list[Any] = [source_label_cursor]
    if retry_rowids:
        source_sql += f" OR l.rowid IN ({retry_placeholders})"
        source_params.extend(retry_rowids)
    source_sql += (
        " ORDER BY CASE WHEN l.rowid>? THEN 0 ELSE 1 END,l.rowid "
        "LIMIT ?"
    )
    source_params.extend([source_label_cursor, int(max_labels) + 1])
    cursor = source.execute(
        source_sql,
        tuple(source_params),
    )
    source_rows = cursor.fetchall()
    more_pending = len(source_rows) > int(max_labels)
    source_rows = source_rows[: int(max_labels)]
    normalized = [
        _validate_source_label(
            source,
            registry=registry,
            row=_source_row_mapping(cursor, item),
        )
        for item in source_rows
    ]
    total_bytes = sum(
        len(canonical_json(item).encode("utf-8")) for item in normalized
    )
    assert_process_memory_budget(projected_bytes=max(1, total_bytes) * 3)
    _assert_capacity(destination, projected_bytes=max(1, total_bytes) * 2)
    with _open_store(destination, create=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current_generation, current_cursor = _read_meta_for_write(
                connection, expected_generation=generation
            )
            inserted = 0
            replayed = 0
            ingested = 0
            skipped_ineligible = 0
            for item in normalized:
                prior_ingest = connection.execute(
                    "SELECT source_label_id,source_label_content_sha256 "
                    "FROM forward_label_ingest_state "
                    "WHERE source_label_rowid=?",
                    (item["source_label_rowid"],),
                ).fetchone()
                if prior_ingest is not None:
                    if (
                        str(prior_ingest[0]) != item["label_id"]
                        or str(prior_ingest[1]) != item["content_sha256"]
                    ):
                        raise ForwardObservationStoreError(
                            "forward_store_label_ingest_identity_conflict"
                        )
                    prior_state = connection.execute(
                        "SELECT state FROM forward_label_ingest_state "
                        "WHERE source_label_rowid=?",
                        (item["source_label_rowid"],),
                    ).fetchone()
                    if prior_state is None:
                        raise ForwardObservationStoreError(
                            "forward_store_label_ingest_state_missing"
                        )
                    if str(prior_state[0]) == "MATERIALIZED":
                        replayed += 1
                        continue
                    if str(prior_state[0]) != "SKIPPED_INELIGIBLE":
                        raise ForwardObservationStoreError(
                            "forward_store_label_ingest_state_invalid"
                        )
                    transitioned = connection.execute(
                        "SELECT source_label_id,source_label_content_sha256,"
                        "transition_sha256 FROM forward_label_ingest_transition "
                        "WHERE source_label_rowid=? AND state='MATERIALIZED'",
                        (item["source_label_rowid"],),
                    ).fetchone()
                    if transitioned is not None:
                        if (
                            str(transitioned[0]) != item["label_id"]
                            or str(transitioned[1]) != item["content_sha256"]
                            or str(transitioned[2]) != _label_transition_sha256(item)
                        ):
                            raise ForwardObservationStoreError(
                                "forward_store_label_transition_identity_conflict"
                            )
                        replayed += 1
                        continue
                observation = connection.execute(
                    "SELECT observation_key,observation_sha256,station_id,event_date,"
                    "settlement_timezone FROM forward_observation WHERE decision_id=?",
                    (item["decision_id"],),
                ).fetchall()
                if not observation:
                    if prior_ingest is not None:
                        # Keep the original skip immutable and retry this lower
                        # row on a later pass when its observation arrives.
                        replayed += 1
                        continue
                    quarantined = connection.execute(
                        "SELECT error_code FROM forward_ingest_state "
                        "WHERE decision_id=? AND state='QUARANTINED'",
                        (item["decision_id"],),
                    ).fetchone()
                    if quarantined is not None:
                        # The store's own ingest verdict is a valid reason for
                        # a missing observation even when the source decision
                        # claims ELIGIBLE: a quarantined decision never
                        # materializes through the normal lane.  Record the
                        # skip so the label cursor can advance; the retry lane
                        # promotes the label if the quarantine is repaired.
                        skip_reason = "FORWARD_OBSERVATION_QUARANTINED:" + str(
                            quarantined[0] or ""
                        )
                    else:
                        if not _source_table_exists(
                            source, "auto_trade_decisions"
                        ):
                            raise ForwardObservationStoreError(
                                "forward_store_label_decision_authority_missing"
                            )
                        decision_rows = source.execute(
                            "SELECT details_json FROM auto_trade_decisions "
                            "WHERE decision_id=? LIMIT 2",
                            (item["decision_id"],),
                        ).fetchall()
                        if len(decision_rows) != 1:
                            raise ForwardObservationStoreError(
                                "forward_store_label_decision_authority_not_unique"
                            )
                        try:
                            details = json.loads(str(decision_rows[0][0] or "{}"))
                        except (TypeError, ValueError, json.JSONDecodeError) as exc:
                            raise ForwardObservationStoreError(
                                "forward_store_label_decision_details_invalid"
                            ) from exc
                        status = (
                            str(details.get("forward_observation_status") or "")
                            if isinstance(details, Mapping)
                            else ""
                        )
                        if not status.startswith("INELIGIBLE_"):
                            raise ForwardObservationStoreError(
                                "forward_store_label_exact_decision_join_missing_or_ambiguous"
                            )
                        skip_reason = status
                    connection.execute(
                        "INSERT INTO forward_label_ingest_state("
                        "source_label_rowid,source_label_id,"
                        "source_label_content_sha256,state,skip_reason,"
                        "materialized_at_utc"
                        ") VALUES(?,?,?,?,?,?)",
                        (
                            item["source_label_rowid"],
                            item["label_id"],
                            item["content_sha256"],
                            "SKIPPED_INELIGIBLE",
                            skip_reason,
                            materialized_at,
                        ),
                    )
                    ingested += 1
                    skipped_ineligible += 1
                    continue
                if len(observation) != 1:
                    raise ForwardObservationStoreError(
                        "forward_store_label_exact_decision_join_missing_or_ambiguous"
                    )
                observation_key, observation_sha, obs_station, obs_date, obs_tz = observation[0]
                if (
                    str(obs_station) != item["station_id"]
                    or str(obs_date) != item["event_date"]
                    or str(obs_tz) != item["settlement_timezone"]
                ):
                    raise ForwardObservationStoreError(
                        "forward_store_label_observation_registry_binding_mismatch"
                    )
                existing = connection.execute(
                    "SELECT label_sha256,truth_sha256,source_label_id "
                    "FROM forward_settlement_label WHERE observation_key=? AND revision=?",
                    (str(observation_key), item["truth_revision_no"]),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing[0]) != item["content_sha256"]
                        or str(existing[1] or "") != item["truth_sha256"]
                        or str(existing[2] or "") != item["label_id"]
                    ):
                        raise ForwardObservationStoreError(
                            "forward_store_label_revision_conflict"
                        )
                    replayed += 1
                else:
                    latest = connection.execute(
                        "SELECT revision FROM forward_settlement_label "
                        "WHERE observation_key=? ORDER BY revision DESC LIMIT 1",
                        (str(observation_key),),
                    ).fetchone()
                    if latest is not None and int(latest[0]) >= item["truth_revision_no"]:
                        raise ForwardObservationStoreError(
                            "forward_store_label_revision_not_new"
                        )
                    existing_hash = connection.execute(
                        "SELECT observation_key,revision FROM forward_settlement_label "
                        "WHERE label_sha256=?",
                        (item["content_sha256"],),
                    ).fetchone()
                    if existing_hash is not None:
                        raise ForwardObservationStoreError(
                            "forward_store_label_content_address_conflict"
                        )
                    connection.execute(
                        "INSERT INTO forward_settlement_label("
                        "observation_key,revision,truth_id,truth_sha256,official_tmax_c,"
                        "winning_bucket,labeled_at_utc,settlement_closed_at_utc,"
                        "settlement_authority,authority_scope,source_fact_id,source_label_id,"
                        "source_label_content_sha256,supersedes_label_id,outcome_json,"
                        "official_settlement_sha256,settlement_timezone,label_sha256) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            str(observation_key),
                            item["truth_revision_no"],
                            item["truth_id"],
                            item["truth_sha256"],
                            item["official_tmax_c"],
                            item["winning_bucket"],
                            item["labeled_at_utc"],
                            item["settlement_closed_at_utc"],
                            item["settlement_authority"],
                            item["authority_scope"],
                            item["fact_id"],
                            item["label_id"],
                            item["content_sha256"],
                            item["supersedes_label_id"],
                            item["outcome_json"],
                            item["official_settlement_sha256"],
                            item["settlement_timezone"],
                            item["content_sha256"],
                        ),
                    )
                    inserted += 1
                if prior_ingest is None:
                    connection.execute(
                        "INSERT INTO forward_label_ingest_state("
                        "source_label_rowid,source_label_id,"
                        "source_label_content_sha256,state,skip_reason,"
                        "materialized_at_utc"
                        ") VALUES(?,?,?,?,?,?)",
                        (
                            item["source_label_rowid"],
                            item["label_id"],
                            item["content_sha256"],
                            "MATERIALIZED",
                            None,
                            materialized_at,
                        ),
                    )
                else:
                    if _append_label_materialized_transition(
                        connection,
                        item=item,
                        materialized_at_utc=materialized_at,
                    ):
                        # The transition itself is the new append-only ingest
                        # event; the original SKIPPED row remains untouched.
                        pass
                ingested += 1
            if ingested:
                next_generation = current_generation + 1
                meta_sha = _write_meta(
                    connection,
                    generation=next_generation,
                    last_source_rowid=current_cursor,
                    updated_at_utc=materialized_at,
                )
            else:
                next_generation = current_generation
                meta_row = connection.execute(
                    "SELECT content_sha256 FROM forward_store_meta WHERE singleton=1"
                ).fetchone()
                meta_sha = str(meta_row[0]) if meta_row is not None else _write_meta(
                    connection,
                    generation=current_generation,
                    last_source_rowid=current_cursor,
                    updated_at_utc=materialized_at,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    _assert_capacity(destination)
    return {
        "schema": STORE_SCHEMA,
        "state": "committed" if ingested else "already_current",
        "generation": next_generation,
        "last_source_rowid": current_cursor,
        "inserted": inserted,
        "skipped_ineligible": skipped_ineligible,
        "ingested": ingested,
        "replayed": replayed,
        "source_rows": len(normalized),
        "last_source_label_rowid": (
            max(
                source_label_cursor,
                max(item["source_label_rowid"] for item in normalized),
            )
            if normalized
            else source_label_cursor
        ),
        "more_pending": more_pending,
        "content_sha256": meta_sha,
        "promotion_allowed": False,
        "execution_mode": "production_no_live",
        "authorizes_order": False,
        "orders_submitted": 0,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }


def _normalize_quarantine_record(
    record: Mapping[str, Any],
    *,
    default_timestamp: str | None = None,
) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise ForwardObservationStoreError(
            "forward_store_quarantine_record_mapping_required"
        )
    forbidden = {
        str(key)
        for key in record
        if str(key)
        not in {
            "decision_id",
            "source_decision_sha256",
            "decision_sha256",
            "source_rowid",
            "error_code",
            "first_seen_at_utc",
            "last_seen_at_utc",
            "observed_at_utc",
        }
    }
    if forbidden:
        raise ForwardObservationStoreError(
            "forward_store_quarantine_raw_payload_forbidden"
        )
    decision_id = str(record.get("decision_id") or "").strip()
    if not decision_id:
        raise ForwardObservationStoreError(
            "forward_store_quarantine_decision_id_missing"
        )
    source_sha = record.get("source_decision_sha256")
    if source_sha is None:
        source_sha = record.get("decision_sha256")
    source_sha = _require_sha(
        "forward_store_quarantine_source_decision_sha256", source_sha
    )
    source_rowid = _require_positive_int(
        "forward_store_quarantine_source_rowid", record.get("source_rowid")
    )
    error_code = str(record.get("error_code") or "").strip()
    if (
        not error_code
        or len(error_code.encode("utf-8")) > MAX_ERROR_CODE_BYTES
        or _ERROR_CODE.fullmatch(error_code) is None
    ):
        raise ForwardObservationStoreError(
            "forward_store_quarantine_error_code_invalid"
        )
    observed = record.get("observed_at_utc") or default_timestamp
    first_seen = record.get("first_seen_at_utc") or observed
    last_seen = record.get("last_seen_at_utc") or observed
    if first_seen is None or last_seen is None:
        raise ForwardObservationStoreError(
            "forward_store_quarantine_timestamp_missing"
        )
    first = _require_timestamp(
        "forward_store_quarantine_first_seen_at_utc", first_seen
    )
    last = _require_timestamp(
        "forward_store_quarantine_last_seen_at_utc", last_seen
    )
    return {
        "decision_id": decision_id,
        "source_decision_sha256": source_sha,
        "source_rowid": source_rowid,
        "error_code": error_code,
        "first_seen_at_utc": first,
        "last_seen_at_utc": last,
    }


def quarantine_forward_observation_batch(
    path: Path | str,
    *,
    records: Sequence[Mapping[str, Any]],
    expected_generation: int,
    quarantined_at_utc: str | None = None,
    observed_at_utc: str | None = None,
    allowed_root: Path | str = DEFAULT_STORE_ROOT,
) -> dict[str, Any]:
    """Quarantine poison identities without ever persisting their payloads."""

    if (
        isinstance(records, (str, bytes, bytearray))
        or not isinstance(records, Sequence)
        or len(records) > MAX_QUARANTINE_BATCH_ROWS
    ):
        raise ForwardObservationStoreError("forward_store_quarantine_batch_invalid")
    timestamp = quarantined_at_utc or observed_at_utc
    default_timestamp = (
        _require_timestamp("forward_store_quarantine_observed_at_utc", timestamp)
        if timestamp
        else None
    )
    normalized = [
        _normalize_quarantine_record(item, default_timestamp=default_timestamp)
        for item in records
    ]
    # A duplicate identity in one batch is allowed only when byte-for-byte
    # identical; conflicting poison claims must not partially advance cursor.
    by_decision: dict[str, dict[str, Any]] = {}
    for item in normalized:
        prior = by_decision.get(item["decision_id"])
        if prior is not None and prior != item:
            raise ForwardObservationStoreError(
                "forward_store_quarantine_identity_conflict"
            )
        by_decision[item["decision_id"]] = item
    normalized = list(by_decision.values())
    total_bytes = sum(
        len(canonical_json(item).encode("utf-8")) for item in normalized
    )
    assert_process_memory_budget(projected_bytes=max(1, total_bytes) * 3)
    destination = resolve_forward_store_path(path, allowed_root=allowed_root)
    _assert_capacity(destination, projected_bytes=max(1, total_bytes) * 2)
    generation = _require_nonnegative_int(
        "forward_store_expected_generation", expected_generation
    )
    with _open_store(destination, create=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current_generation, current_cursor = _read_meta_for_write(
                connection, expected_generation=generation
            )
            inserted = 0
            replayed = 0
            next_cursor = current_cursor
            for item in normalized:
                prior = connection.execute(
                    "SELECT source_decision_sha256,source_rowid,state,error_code,"
                    "first_seen_at_utc,last_seen_at_utc FROM forward_ingest_state "
                    "WHERE decision_id=?",
                    (item["decision_id"],),
                ).fetchone()
                if prior is not None:
                    if (
                        str(prior[0]) != item["source_decision_sha256"]
                        or int(prior[1]) != item["source_rowid"]
                        or str(prior[2]) != "QUARANTINED"
                        or str(prior[3]) != item["error_code"]
                    ):
                        raise ForwardObservationStoreError(
                            "forward_store_quarantine_identity_conflict"
                        )
                    replayed += 1
                    next_cursor = max(next_cursor, item["source_rowid"])
                    continue
                committed = connection.execute(
                    "SELECT decision_id FROM forward_observation WHERE decision_id=?",
                    (item["decision_id"],),
                ).fetchone()
                if committed is not None:
                    raise ForwardObservationStoreError(
                        "forward_store_quarantine_committed_identity_conflict"
                    )
                rowid_owner = connection.execute(
                    "SELECT decision_id FROM forward_ingest_state WHERE source_rowid=?",
                    (item["source_rowid"],),
                ).fetchone()
                if rowid_owner is not None and str(rowid_owner[0]) != item["decision_id"]:
                    raise ForwardObservationStoreError(
                        "forward_store_quarantine_source_rowid_conflict"
                    )
                connection.execute(
                    "INSERT INTO forward_ingest_state("
                    "decision_id,source_decision_sha256,source_rowid,state,error_code,"
                    "first_seen_at_utc,last_seen_at_utc) VALUES(?,?,?,?,?,?,?)",
                    (
                        item["decision_id"],
                        item["source_decision_sha256"],
                        item["source_rowid"],
                        "QUARANTINED",
                        item["error_code"],
                        item["first_seen_at_utc"],
                        item["last_seen_at_utc"],
                    ),
                )
                inserted += 1
                next_cursor = max(next_cursor, item["source_rowid"])
            if inserted:
                next_generation = current_generation + 1
                meta_sha = _write_meta(
                    connection,
                    generation=next_generation,
                    last_source_rowid=next_cursor,
                    updated_at_utc=(
                        default_timestamp
                        or normalized[-1]["last_seen_at_utc"]
                        if normalized
                        else "1970-01-01T00:00:00Z"
                    ),
                )
            else:
                next_generation = current_generation
                meta_row = connection.execute(
                    "SELECT content_sha256 FROM forward_store_meta WHERE singleton=1"
                ).fetchone()
                meta_sha = str(meta_row[0]) if meta_row is not None else _write_meta(
                    connection,
                    generation=current_generation,
                    last_source_rowid=current_cursor,
                    updated_at_utc=(
                        default_timestamp or "1970-01-01T00:00:00Z"
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    _assert_capacity(destination)
    return {
        "schema": STORE_SCHEMA,
        "state": "blocked_fail_closed",
        "reason": "poison_quarantined" if inserted else "already_quarantined",
        "promotion_allowed": False,
        "generation": next_generation,
        "last_source_rowid": next_cursor,
        "inserted": inserted,
        "replayed": replayed,
        "quarantine_count": inserted + replayed,
        "execution_mode": "production_no_live",
        "authorizes_order": False,
        "orders_submitted": 0,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }


def quarantine_forward_observation(
    path: Path | str,
    *,
    decision_id: str,
    source_decision_sha256: str,
    source_rowid: int,
    error_code: str,
    expected_generation: int,
    quarantined_at_utc: str,
    allowed_root: Path | str = DEFAULT_STORE_ROOT,
) -> dict[str, Any]:
    """Single-record convenience wrapper for the bounded quarantine API."""

    return quarantine_forward_observation_batch(
        path,
        records=[
            {
                "decision_id": decision_id,
                "source_decision_sha256": source_decision_sha256,
                "source_rowid": source_rowid,
                "error_code": error_code,
                "observed_at_utc": quarantined_at_utc,
            }
        ],
        expected_generation=expected_generation,
        quarantined_at_utc=quarantined_at_utc,
        allowed_root=allowed_root,
    )


def repair_false_quarantine_batch(
    path: Path | str,
    *,
    records: Sequence[Mapping[str, Any]],
    expected_generation: int,
    repaired_at_utc: str,
    allowed_root: Path | str = DEFAULT_STORE_ROOT,
) -> dict[str, Any]:
    """Append validated observations that a historical validator quarantined.

    The poison identity remains immutable in ``forward_ingest_state``.  A
    content-addressed repair row supersedes it for effective-state reads, so
    audit history is retained and ordinary UPDATE/DELETE remains forbidden.
    The caller must reconstruct every observation from the authoritative
    decision/training-fact tables; the payload formerly rejected by the store
    is never read or accepted as repair authority.
    """

    if (
        isinstance(records, (str, bytes, bytearray))
        or not isinstance(records, Sequence)
        or not records
        or len(records) > MAX_FALSE_QUARANTINE_REPAIR_ROWS
    ):
        raise ForwardObservationStoreError(
            "forward_store_false_quarantine_repair_batch_invalid"
        )
    repaired_at = _require_timestamp(
        "forward_store_false_quarantine_repaired_at_utc", repaired_at_utc
    )
    normalized: list[
        tuple[dict[str, Any], str, str, int, str, bytes, dict[str, Any]]
    ] = []
    decision_ids: set[str] = set()
    source_rowids: set[int] = set()
    total_bytes = 0
    for record in records:
        if not isinstance(record, Mapping):
            raise ForwardObservationStoreError(
                "forward_store_false_quarantine_repair_record_mapping_required"
            )
        try:
            row = validate_forward_observation_row(record.get("observation"))
        except ForwardObservationError as exc:
            raise ForwardObservationStoreError(
                "forward_store_false_quarantine_repair_observation_invalid:"
                + str(exc)
            ) from exc
        decision_id = str(record.get("decision_id") or "").strip()
        decision_sha = _require_sha(
            "forward_store_false_quarantine_repair_decision_sha256",
            record.get("decision_sha256"),
        )
        source_rowid = _require_positive_int(
            "forward_store_false_quarantine_repair_source_rowid",
            record.get("source_rowid"),
        )
        prior_error = str(record.get("prior_error_code") or "").strip()
        if prior_error not in FALSE_QUARANTINE_REPAIRABLE_ERRORS:
            raise ForwardObservationStoreError(
                "forward_store_false_quarantine_repair_reason_not_allowed"
            )
        if not decision_id or row["decision_id"] != decision_id:
            raise ForwardObservationStoreError(
                "forward_store_false_quarantine_repair_source_identity_invalid"
            )
        if decision_id in decision_ids or source_rowid in source_rowids:
            raise ForwardObservationStoreError(
                "forward_store_false_quarantine_repair_batch_identity_conflict"
            )
        decision_ids.add(decision_id)
        source_rowids.add(source_rowid)
        raw = bounded_json_bytes(row, limit=MAX_BATCH_JSON_BYTES)
        receipt = {
            "schema": "bayes_basket_target_false_quarantine_repair_receipt_v1",
            "decision_id": decision_id,
            "source_decision_sha256": decision_sha,
            "source_rowid": source_rowid,
            "prior_error_code": prior_error,
            "observation_key": row["observation_key"],
            "observation_sha256": row["observation_sha256"],
            "repaired_at_utc": repaired_at,
            "execution_mode": "production_no_live",
            "authorizes_order": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
        }
        receipt["content_sha256"] = content_sha256(receipt)
        total_bytes += len(raw) + len(canonical_json(receipt).encode("utf-8"))
        if total_bytes > MAX_BATCH_JSON_BYTES:
            raise ForwardObservationStoreError(
                "forward_store_false_quarantine_repair_bytes_exceeded"
            )
        normalized.append(
            (
                row,
                decision_id,
                decision_sha,
                source_rowid,
                prior_error,
                raw,
                receipt,
            )
        )
    assert_process_memory_budget(projected_bytes=max(1, total_bytes) * 3)
    destination = resolve_forward_store_path(path, allowed_root=allowed_root)
    _assert_capacity(destination, projected_bytes=max(1, total_bytes) * 2)
    generation = _require_nonnegative_int(
        "forward_store_expected_generation", expected_generation
    )
    with _open_store(destination, create=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current_generation, current_cursor = _read_meta_for_write(
                connection, expected_generation=generation
            )
            inserted = 0
            replayed = 0
            receipts: list[dict[str, Any]] = []
            for (
                row,
                decision_id,
                decision_sha,
                source_rowid,
                prior_error,
                raw,
                receipt,
            ) in normalized:
                prior = connection.execute(
                    "SELECT source_decision_sha256,source_rowid,state,error_code "
                    "FROM forward_ingest_state WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
                if (
                    prior is None
                    or str(prior[0]) != decision_sha
                    or int(prior[1]) != source_rowid
                    or str(prior[2]) != "QUARANTINED"
                    or str(prior[3]) != prior_error
                ):
                    raise ForwardObservationStoreError(
                        "forward_store_false_quarantine_repair_identity_drift"
                    )
                existing_repair = connection.execute(
                    "SELECT source_decision_sha256,source_rowid,prior_error_code,"
                    "observation_key,observation_sha256,repair_receipt_sha256 "
                    "FROM forward_ingest_repair WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
                if existing_repair is not None:
                    expected = (
                        decision_sha,
                        source_rowid,
                        prior_error,
                        row["observation_key"],
                        row["observation_sha256"],
                        receipt["content_sha256"],
                    )
                    actual = (
                        str(existing_repair[0]),
                        int(existing_repair[1]),
                        str(existing_repair[2]),
                        str(existing_repair[3]),
                        str(existing_repair[4]),
                        str(existing_repair[5]),
                    )
                    if actual != expected:
                        raise ForwardObservationStoreError(
                            "forward_store_false_quarantine_repair_replay_drift"
                        )
                    replayed += 1
                    receipts.append(receipt)
                    continue
                if connection.execute(
                    "SELECT 1 FROM forward_retention_tombstone "
                    "WHERE observation_key=?",
                    (row["observation_key"],),
                ).fetchone() is not None:
                    raise ForwardObservationStoreError(
                        "forward_store_false_quarantine_repair_tombstoned"
                    )
                conflict = connection.execute(
                    "SELECT decision_id,observation_sha256 "
                    "FROM forward_observation WHERE observation_key=? "
                    "OR decision_id=? LIMIT 2",
                    (row["observation_key"], decision_id),
                ).fetchall()
                if conflict:
                    if len(conflict) != 1 or (
                        str(conflict[0][0]),
                        str(conflict[0][1]),
                    ) != (decision_id, row["observation_sha256"]):
                        raise ForwardObservationStoreError(
                            "forward_store_false_quarantine_repair_observation_conflict"
                        )
                else:
                    connection.execute(
                        "INSERT INTO forward_observation("
                        "observation_key,decision_id,cycle_id,station_id,"
                        "event_date,phase,decision_at_utc,settlement_timezone,"
                        "observation_sha256,observation_json,state,"
                        "materialized_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            row["observation_key"],
                            decision_id,
                            row["cycle_id"],
                            row["station_id"],
                            row["event_date"],
                            row["phase"],
                            row["decision_at_utc"],
                            row["settlement_timezone"],
                            row["observation_sha256"],
                            raw.decode("utf-8"),
                            "ACTIVE",
                            repaired_at,
                        ),
                    )
                connection.execute(
                    "INSERT INTO forward_ingest_repair("
                    "decision_id,source_decision_sha256,source_rowid,"
                    "prior_error_code,observation_key,observation_sha256,"
                    "repaired_at_utc,repair_receipt_sha256)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        decision_id,
                        decision_sha,
                        source_rowid,
                        prior_error,
                        row["observation_key"],
                        row["observation_sha256"],
                        repaired_at,
                        receipt["content_sha256"],
                    ),
                )
                inserted += 1
                receipts.append(receipt)
            if inserted:
                next_generation = current_generation + 1
                meta_sha = _write_meta(
                    connection,
                    generation=next_generation,
                    last_source_rowid=current_cursor,
                    updated_at_utc=repaired_at,
                )
            else:
                next_generation = current_generation
                meta_sha = str(
                    connection.execute(
                        "SELECT content_sha256 FROM forward_store_meta "
                        "WHERE singleton=1"
                    ).fetchone()[0]
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    state = read_forward_store_state(destination, allowed_root=allowed_root)
    _assert_capacity(destination)
    return {
        "schema": STORE_SCHEMA,
        "state": "repaired" if inserted else "already_repaired",
        "generation": next_generation,
        "last_source_rowid": int(state["last_source_rowid"]),
        "inserted": inserted,
        "replayed": replayed,
        "observation_count": int(state["observation_count"]),
        "quarantine_count": int(state["quarantine_count"]),
        "historical_quarantine_count": int(
            state["historical_quarantine_count"]
        ),
        "repaired_quarantine_count": int(
            state["repaired_quarantine_count"]
        ),
        "repair_receipts": receipts,
        "content_sha256": meta_sha,
        "promotion_allowed": False,
        "execution_mode": "production_no_live",
        "authorizes_order": False,
        "orders_submitted": 0,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }


def _content_addressed_receipt(
    name: str,
    value: Any,
    *,
    expected_schema: str,
    observation_key: str,
    decision_id: str,
    label_revision: int,
) -> tuple[str, Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        raise ForwardObservationStoreError(f"{name}_receipt_mapping_required")
    receipt = dict(value)
    supplied = _require_sha(f"{name}_receipt_content_sha256", receipt.get("content_sha256"))
    expected = content_sha256(
        {key: item for key, item in receipt.items() if key != "content_sha256"}
    )
    if supplied != expected:
        raise ForwardObservationStoreError(f"{name}_receipt_content_address_mismatch")
    if str(receipt.get("schema") or "") != expected_schema:
        raise ForwardObservationStoreError(f"{name}_receipt_schema_invalid")
    if (
        str(receipt.get("observation_key") or "") != observation_key
        or str(receipt.get("decision_id") or "") != decision_id
        or int(receipt.get("label_revision") or 0) != label_revision
    ):
        raise ForwardObservationStoreError(f"{name}_receipt_scope_mismatch")
    if receipt.get("complete") is not True:
        raise ForwardObservationStoreError(f"{name}_receipt_incomplete")
    return supplied, receipt


def _normalize_lineage_proof(
    proof: Mapping[str, Any],
    *,
    observation_key: str,
    decision_id: str,
    label_revision: int,
) -> dict[str, Any]:
    if not isinstance(proof, Mapping):
        raise ForwardObservationStoreError(
            "forward_store_retention_lineage_proof_mapping_required"
        )
    raw = dict(proof)
    schema = str(raw.get("schema") or "").strip()
    if schema not in {
        "bayes_basket_target_forward_retention_lineage_proof_v1",
        "bayes_basket_target_forward_scorecard_lineage_proof_v1",
        "forward_scorecard_lineage_proof_v1",
    }:
        raise ForwardObservationStoreError(
            "forward_store_retention_lineage_proof_schema_invalid"
        )
    stored_proof_sha = _require_sha(
        "forward_store_retention_lineage_proof_content_sha256",
        raw.get("content_sha256"),
    )
    expected_proof_sha = content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    )
    if stored_proof_sha != expected_proof_sha:
        raise ForwardObservationStoreError(
            "forward_store_retention_lineage_proof_content_address_mismatch"
        )
    supplied_key = str(raw.get("observation_key") or "").strip()
    supplied_decision = str(raw.get("decision_id") or "").strip()
    if supplied_key != observation_key or supplied_decision != decision_id:
        raise ForwardObservationStoreError(
            "forward_store_retention_lineage_proof_observation_binding_mismatch"
        )
    supplied_revision = raw.get("label_revision", raw.get("truth_revision_no"))
    try:
        supplied_revision = int(supplied_revision)
    except (TypeError, ValueError) as exc:
        raise ForwardObservationStoreError(
            "forward_store_retention_lineage_proof_label_revision_invalid"
        ) from exc
    if supplied_revision != label_revision:
        raise ForwardObservationStoreError(
            "forward_store_retention_lineage_proof_label_revision_mismatch"
        )
    if raw.get("retention_eligible") is not True:
        raise ForwardObservationStoreError(
            "forward_store_retention_lineage_proof_retention_eligibility_required"
        )
    if raw.get("scorecard_complete") is not True or raw.get("attribution_complete") is not True:
        raise ForwardObservationStoreError(
            "forward_store_retention_lineage_proof_scorecard_attribution_incomplete"
        )
    if raw.get("downstream_dependencies", []) not in ([], None):
        raise ForwardObservationStoreError(
            "forward_store_retention_lineage_proof_downstream_dependency_present"
        )
    scorecard_receipt = raw.get("scorecard_receipt")
    attribution_receipt = raw.get("attribution_receipt")
    if scorecard_receipt is None or attribution_receipt is None:
        raise ForwardObservationStoreError(
            "forward_store_retention_embedded_receipts_required"
        )
    scorecard_sha, _ = _content_addressed_receipt(
        "forward_store_retention_scorecard",
        scorecard_receipt,
        expected_schema="bayes_basket_target_forward_scorecard_retention_receipt_v1",
        observation_key=observation_key,
        decision_id=decision_id,
        label_revision=label_revision,
    )
    attribution_sha, _ = _content_addressed_receipt(
        "forward_store_retention_attribution",
        attribution_receipt,
        expected_schema="bayes_basket_target_forward_attribution_retention_receipt_v1",
        observation_key=observation_key,
        decision_id=decision_id,
        label_revision=label_revision,
    )
    return {
        "schema": schema,
        "content_sha256": stored_proof_sha,
        "observation_key": observation_key,
        "decision_id": decision_id,
        "label_revision": label_revision,
        "scorecard_sha256": scorecard_sha,
        "attribution_sha256": attribution_sha,
    }


def tombstone_forward_observations(
    path: Path | str,
    *,
    lineage_proofs: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    retention_before_utc: str,
    expected_generation: int,
    purged_at_utc: str,
    observation_keys: Sequence[str] | None = None,
    max_rows: int = MAX_RETENTION_BATCH_ROWS,
    allowed_root: Path | str = DEFAULT_STORE_ROOT,
) -> dict[str, Any]:
    """Atomically tombstone and physically purge only proof-backed history.

    A formal scorecard/attribution receipt is an explicit caller-supplied
    content-addressed proof.  This API never invents or queries a shared
    scorecard table.  Missing or unverifiable proof blocks the whole batch.
    """

    if isinstance(max_rows, bool) or not 1 <= int(max_rows) <= MAX_RETENTION_BATCH_ROWS:
        raise ForwardObservationStoreError("forward_store_retention_batch_limit_invalid")
    cutoff = _require_timestamp(
        "forward_store_retention_before_utc", retention_before_utc
    )
    purged_at = _require_timestamp(
        "forward_store_purged_at_utc", purged_at_utc
    )
    generation = _require_nonnegative_int(
        "forward_store_expected_generation", expected_generation
    )
    if isinstance(lineage_proofs, Mapping):
        proof_items = []
        for key, proof in lineage_proofs.items():
            item = dict(proof)
            item.setdefault("observation_key", str(key))
            proof_items.append(item)
    elif isinstance(lineage_proofs, Sequence) and not isinstance(
        lineage_proofs, (str, bytes, bytearray)
    ):
        proof_items = [dict(item) for item in lineage_proofs]
    else:
        raise ForwardObservationStoreError(
            "forward_store_retention_lineage_proofs_invalid"
        )
    if len(proof_items) > int(max_rows):
        raise ForwardObservationStoreError("forward_store_retention_batch_limit_reached")
    proof_by_key: dict[str, Mapping[str, Any]] = {}
    for proof in proof_items:
        key = str(proof.get("observation_key") or "").strip()
        if not key or key in proof_by_key:
            raise ForwardObservationStoreError(
                "forward_store_retention_lineage_proof_key_conflict"
            )
        if len(canonical_json(proof).encode("utf-8")) > MAX_LINEAGE_PROOF_BYTES:
            raise ForwardObservationStoreError(
                "forward_store_retention_lineage_proof_resource_limit_exceeded"
            )
        proof_by_key[key] = proof
    requested = None
    if observation_keys is not None:
        if isinstance(observation_keys, (str, bytes, bytearray)):
            raise ForwardObservationStoreError("forward_store_retention_observation_keys_invalid")
        requested = [str(key).strip() for key in observation_keys]
        if not requested or len(requested) > int(max_rows) or any(not key for key in requested):
            raise ForwardObservationStoreError("forward_store_retention_observation_keys_invalid")
        if len(set(requested)) != len(requested):
            raise ForwardObservationStoreError("forward_store_retention_observation_keys_duplicate")
        if set(requested) != set(proof_by_key):
            raise ForwardObservationStoreError(
                "forward_store_retention_lineage_proof_scope_mismatch"
            )
    destination = resolve_forward_store_path(path, allowed_root=allowed_root)
    _assert_capacity(destination, projected_bytes=max(1, len(proof_items)) * 4096)
    with _open_store(destination, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current_generation, current_cursor = _read_meta_for_write(
                connection, expected_generation=generation
            )
            if requested is None:
                rows = connection.execute(
                    "SELECT o.observation_key,o.decision_id,o.observation_sha256,"
                    "l.revision,l.label_sha256,l.settlement_closed_at_utc "
                    "FROM forward_observation o JOIN forward_settlement_label l "
                    "ON l.observation_key=o.observation_key "
                    "WHERE NOT EXISTS(SELECT 1 FROM forward_retention_tombstone t "
                    "WHERE t.observation_key=o.observation_key) "
                    "AND NOT EXISTS(SELECT 1 FROM forward_settlement_label newer "
                    "WHERE newer.observation_key=l.observation_key AND newer.revision>l.revision) "
                    "ORDER BY l.settlement_closed_at_utc,o.observation_key LIMIT ?",
                    (int(max_rows) + 1,),
                ).fetchall()
                if len(rows) > int(max_rows):
                    raise ForwardObservationStoreError(
                        "forward_store_retention_batch_limit_reached"
                    )
                selected_keys = {str(row[0]) for row in rows}
                if selected_keys != set(proof_by_key):
                    raise ForwardObservationStoreError(
                        "forward_store_retention_lineage_proof_scope_mismatch"
                    )
            else:
                rows = []
                for key in requested:
                    row = connection.execute(
                        "SELECT o.observation_key,o.decision_id,o.observation_sha256,"
                        "l.revision,l.label_sha256,l.settlement_closed_at_utc "
                        "FROM forward_observation o JOIN forward_settlement_label l "
                        "ON l.observation_key=o.observation_key "
                        "WHERE o.observation_key=? AND NOT EXISTS("
                        "SELECT 1 FROM forward_retention_tombstone t "
                        "WHERE t.observation_key=o.observation_key) AND NOT EXISTS("
                        "SELECT 1 FROM forward_settlement_label newer WHERE "
                        "newer.observation_key=l.observation_key AND newer.revision>l.revision)",
                        (key,),
                    ).fetchone()
                    if row is None:
                        raise ForwardObservationStoreError(
                            "forward_store_retention_observation_missing_or_unsettled"
                        )
                    rows.append(row)
            normalized_rows: list[tuple[Any, dict[str, Any]]] = []
            for row in rows:
                closed_at = _require_timestamp(
                    "forward_store_label_settlement_closed_at_utc", row[5]
                )
                try:
                    closed_dt = datetime.fromisoformat(
                        closed_at.replace("Z", "+00:00")
                    )
                    cutoff_dt = datetime.fromisoformat(
                        cutoff.replace("Z", "+00:00")
                    )
                except ValueError as exc:
                    raise ForwardObservationStoreError(
                        "forward_store_retention_timestamp_invalid"
                    ) from exc
                if closed_dt >= cutoff_dt:
                    raise ForwardObservationStoreError(
                        "forward_store_retention_period_not_elapsed"
                    )
                proof = proof_by_key.get(str(row[0]))
                if proof is None:
                    raise ForwardObservationStoreError(
                        "forward_store_retention_lineage_proof_required"
                    )
                normalized_proof = _normalize_lineage_proof(
                    proof,
                    observation_key=str(row[0]),
                    decision_id=str(row[1]),
                    label_revision=int(row[3]),
                )
                normalized_rows.append((row, normalized_proof))
            receipts: list[dict[str, Any]] = []
            new_rows = []
            for row, proof in normalized_rows:
                receipt = {
                    "schema": "bayes_basket_target_forward_purge_receipt_v1",
                    "observation_key": str(row[0]),
                    "decision_id": str(row[1]),
                    "observation_sha256": _require_sha(
                        "forward_store_observation_sha256", row[2]
                    ),
                    "label_revision": int(row[3]),
                    "label_sha256": _require_sha(
                        "forward_store_label_sha256", row[4]
                    ),
                    "scorecard_sha256": proof["scorecard_sha256"],
                    "attribution_sha256": proof["attribution_sha256"],
                    "lineage_proof_sha256": proof["content_sha256"],
                    "purged_at_utc": purged_at,
                }
                receipt_sha = content_sha256(receipt)
                receipts.append({**receipt, "content_sha256": receipt_sha})
                new_rows.append((row, receipt_sha))
            for row, receipt_sha in new_rows:
                existing = connection.execute(
                    "SELECT observation_sha256,purge_receipt_sha256 "
                    "FROM forward_retention_tombstone WHERE observation_key=?",
                    (str(row[0]),),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != str(row[2]):
                        raise ForwardObservationStoreError(
                            "forward_store_retention_tombstone_conflict"
                        )
                    continue
                connection.execute(
                    "INSERT INTO forward_retention_tombstone("
                    "observation_key,observation_sha256,purged_at_utc,purge_receipt_sha256) "
                    "VALUES(?,?,?,?)",
                    (str(row[0]), str(row[2]), purged_at, receipt_sha),
                )
                _set_retention_delete_authorized(connection, True)
                try:
                    connection.execute(
                        "DELETE FROM forward_settlement_label WHERE observation_key=?",
                        (str(row[0]),),
                    )
                    connection.execute(
                        "DELETE FROM forward_ingest_state WHERE decision_id=?",
                        (str(row[1]),),
                    )
                    connection.execute(
                        "DELETE FROM forward_observation WHERE observation_key=?",
                        (str(row[0]),),
                    )
                finally:
                    _set_retention_delete_authorized(connection, False)
            if new_rows:
                next_generation = current_generation + 1
                meta_sha = _write_meta(
                    connection,
                    generation=next_generation,
                    last_source_rowid=current_cursor,
                    updated_at_utc=purged_at,
                )
            else:
                next_generation = current_generation
                meta_row = connection.execute(
                    "SELECT content_sha256 FROM forward_store_meta WHERE singleton=1"
                ).fetchone()
                meta_sha = str(meta_row[0])
            connection.commit()
            # WAL truncation is bounded and prevents retention itself from
            # accumulating an unbounded journal.  VACUUM is deliberately not
            # forced: it may require a second full database-sized workspace.
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            _set_retention_delete_authorized(connection, False)
            connection.rollback()
            raise
    _assert_capacity(destination)
    return {
        "schema": STORE_SCHEMA,
        "state": "committed" if new_rows else "already_tombstoned",
        "generation": next_generation,
        "last_source_rowid": current_cursor,
        "tombstoned_count": len(new_rows),
        "purge_receipts": receipts,
        "content_sha256": meta_sha,
        "promotion_allowed": False,
        "execution_mode": "production_no_live",
        "authorizes_order": False,
        "orders_submitted": 0,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }


def purge_forward_observations(
    path: Path | str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compatibility spelling for the explicit proof-gated retention API."""

    return tombstone_forward_observations(path, **kwargs)


def read_forward_observation_packet(
    path: Path | str,
    *,
    limit: int = 4096,
    allowed_root: Path | str = DEFAULT_STORE_ROOT,
) -> dict[str, Any]:
    """Export the newest bounded page for pure OOS consumers."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4096:
        raise ForwardObservationStoreError("forward_store_export_limit_invalid")
    destination = resolve_forward_store_path(path, allowed_root=allowed_root)
    with _open_store(destination, create=False) as connection:
        raw_rows = connection.execute(
            "SELECT observation_json FROM forward_observation "
            "WHERE NOT EXISTS(SELECT 1 FROM forward_retention_tombstone t "
            "WHERE t.observation_key=forward_observation.observation_key) "
            "ORDER BY decision_at_utc DESC,observation_key DESC LIMIT ?",
            (limit,),
        ).fetchall()
        rows: list[dict[str, Any]] = []
        for raw_row in reversed(raw_rows):
            try:
                value = json.loads(str(raw_row[0]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ForwardObservationStoreError(
                    "forward_store_observation_json_invalid"
                ) from exc
            rows.append(validate_forward_observation_row(value))
        keys = [row["observation_key"] for row in rows]
        labels: list[dict[str, Any]] = []
        if keys:
            placeholders = ",".join("?" for _ in keys)
            label_rows = connection.execute(
                "SELECT l.observation_key,l.revision,l.truth_id,l.truth_sha256,"
                "l.official_tmax_c,l.winning_bucket,l.labeled_at_utc,"
                "l.settlement_closed_at_utc,l.settlement_authority,l.authority_scope,"
                "l.source_fact_id,l.source_label_id,l.source_label_content_sha256,"
                "l.supersedes_label_id,l.outcome_json,l.official_settlement_sha256,"
                "l.settlement_timezone,l.label_sha256 "
                "FROM forward_settlement_label l "
                f"WHERE observation_key IN ({placeholders}) "
                "AND NOT EXISTS(SELECT 1 FROM forward_settlement_label newer "
                "WHERE newer.observation_key=l.observation_key "
                "AND newer.revision>l.revision) "
                "ORDER BY l.observation_key",
                tuple(keys),
            ).fetchall()
            labels = [
                {
                    "observation_key": str(item[0]),
                    "revision": int(item[1]),
                    "truth_id": None if item[2] is None else str(item[2]),
                    "truth_sha256": None if item[3] is None else str(item[3]),
                    "official_tmax_c": item[4],
                    "winning_bucket": str(item[5]),
                    "labeled_at_utc": str(item[6]),
                    "settlement_closed_at_utc": (
                        None if item[7] is None else str(item[7])
                    ),
                    "settlement_authority": str(item[8]),
                    "authority_scope": (
                        None if item[9] is None else str(item[9])
                    ),
                    "source_fact_id": (
                        None if item[10] is None else str(item[10])
                    ),
                    "source_label_id": (
                        None if item[11] is None else str(item[11])
                    ),
                    "source_label_content_sha256": (
                        None if item[12] is None else str(item[12])
                    ),
                    "supersedes_label_id": (
                        None if item[13] is None else str(item[13])
                    ),
                    "outcome_json": item[14],
                    "official_settlement_sha256": str(item[15]),
                    "settlement_timezone": str(item[16]),
                    "label_sha256": str(item[17]),
                }
                for item in label_rows
            ]
    packet = {
        "schema": PACKET_SCHEMA,
        **NO_LIVE_FIELDS,
        "settlement_labels": labels,
        "observations": rows,
    }
    packet["content_sha256"] = content_sha256(packet)
    bounded_json_bytes(packet, limit=MAX_BATCH_JSON_BYTES)
    return packet


__all__ = [
    "DEFAULT_STORE_PATH",
    "DEFAULT_STORE_ROOT",
    "MAX_BATCH_ROWS",
    "MAX_LABEL_BATCH_ROWS",
    "MAX_QUARANTINE_BATCH_ROWS",
    "MAX_FALSE_QUARANTINE_REPAIR_ROWS",
    "MAX_RETENTION_BATCH_ROWS",
    "MAX_STORE_BYTES",
    "MAX_STORE_WAL_BYTES",
    "STORE_SCHEMA",
    "ForwardObservationStoreError",
    "commit_forward_observation_batch",
    "append_forward_settlement_labels",
    "initialize_forward_observation_store",
    "quarantine_forward_observation",
    "quarantine_forward_observation_batch",
    "repair_false_quarantine_batch",
    "tombstone_forward_observations",
    "purge_forward_observations",
    "read_forward_store_state",
    "read_forward_observation_packet",
    "resolve_forward_store_path",
]
