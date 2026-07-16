"""Append-only isolated registry for L0 market-resolution target contracts."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from resolution_target_contract_runtime import SCHEMA_VERSION, validate_market_resolution_target_contract_proposal
from sequential_bayes_public_ssot_shadow_writer import SHADOW_MANIFEST_STATE, SHADOW_MANIFEST_TABLE
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError


REGISTRY_SCHEMA_VERSION = "resolution_target_contract_shadow_writer_v1"
TARGET_TABLE = "resolution_target_contracts"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_isolated_shadow_db(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ShadowTransactionIntegrityError("resolution_target_contract_shadow_manifest_missing")
    row = conn.execute(f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1").fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
        raise ShadowTransactionIntegrityError("resolution_target_contract_shadow_manifest_invalid")


def initialize_isolated_resolution_target_contract_schema(conn: sqlite3.Connection) -> None:
    """Create only the append-only target contract registry in a sentinel DB."""

    _assert_isolated_shadow_db(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
            target_contract_id TEXT PRIMARY KEY,
            target_identity TEXT NOT NULL,
            resolution_local_date TEXT NOT NULL,
            target_contract_sha256 TEXT NOT NULL UNIQUE,
            supersedes_target_contract_sha256 TEXT,
            revision_no INTEGER NOT NULL,
            contract_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(target_identity,resolution_local_date,revision_no)
        )
        """
    )
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_{TARGET_TABLE}_no_update
        BEFORE UPDATE ON {TARGET_TABLE} BEGIN SELECT RAISE(ABORT, 'resolution_target_contract_append_only'); END"""
    )
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_{TARGET_TABLE}_no_delete
        BEFORE DELETE ON {TARGET_TABLE} BEGIN SELECT RAISE(ABORT, 'resolution_target_contract_append_only'); END"""
    )


def _validate_contract(value: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    row = dict(_mapping(value))
    issues: list[str] = []
    proposal_validation = validate_market_resolution_target_contract_proposal(row)
    issues.extend(proposal_validation.get("block_reasons") or [])
    for field in (
        "target_contract_id",
        "target_contract_sha256",
        "target_identity",
        "resolution_local_date",
        "authority_contract_sha256",
        "resolution_source_id",
    ):
        if not _text(row.get(field)):
            issues.append(f"target_contract_{field}_missing")
    if row.get("bridge_closed") is not False or row.get("validated") is not False:
        issues.append("target_contract_must_remain_unvalidated_data_accumulation")
    raw_snapshot = _mapping(row.get("market_rule_snapshot"))
    for field in ("raw_ref", "raw_sha256", "extractor_version", "extraction_sha256"):
        if not _text(raw_snapshot.get(field)):
            issues.append(f"target_contract_market_rule_{field}_missing")
    fragments = raw_snapshot.get("extraction_evidence_fragments")
    if not isinstance(fragments, list) or not fragments or not all(_text(fragment) for fragment in fragments):
        issues.append("target_contract_market_rule_extraction_evidence_fragments_invalid")
    if not _mapping(row.get("bucket_contract")):
        issues.append("target_contract_bucket_contract_missing")
    sha = _text(row.get("target_contract_sha256")).lower()
    if len(sha) != 64 or any(character not in "0123456789abcdef" for character in sha):
        issues.append("target_contract_sha256_invalid")
    return row, sorted(set(issues))


def persist_resolution_target_contract_shadow(
    conn: sqlite3.Connection,
    contract: Mapping[str, Any],
    *,
    created_at: str,
) -> dict[str, Any]:
    """Append a first contract or an explicit successor; never rewrite history."""

    _assert_isolated_shadow_db(conn)
    if not _table_exists(conn, TARGET_TABLE):
        raise ShadowTransactionIntegrityError("resolution_target_contract_shadow_schema_missing")
    row, issues = _validate_contract(contract)
    if issues:
        raise ValueError("resolution_target_contract_invalid:" + ",".join(issues))
    encoded = _json(row)
    existing = conn.execute(
        f"SELECT contract_json,revision_no FROM {TARGET_TABLE} WHERE target_contract_sha256=?",
        (_text(row["target_contract_sha256"]),),
    ).fetchone()
    if existing is not None:
        if _text(existing[0]) != encoded:
            raise ShadowTransactionIntegrityError("resolution_target_contract_replay_drift")
        return {
            "schema": REGISTRY_SCHEMA_VERSION,
            "ok": True,
            "idempotent": True,
            "revision_no": int(existing[1]),
            "shadow_only": True,
        }
    latest = conn.execute(
        f"""SELECT target_contract_sha256,revision_no FROM {TARGET_TABLE}
            WHERE target_identity=? AND resolution_local_date=?
            ORDER BY revision_no DESC LIMIT 1""",
        (_text(row["target_identity"]), _text(row["resolution_local_date"])[:10]),
    ).fetchone()
    supersedes = _text(row.get("supersedes_target_contract_sha256")).lower()
    if latest is None:
        if supersedes:
            raise ShadowTransactionIntegrityError("resolution_target_contract_unexpected_supersedes")
        revision_no = 0
    else:
        latest_sha, latest_revision = _text(latest[0]).lower(), int(latest[1])
        if supersedes != latest_sha:
            raise ShadowTransactionIntegrityError("resolution_target_contract_successor_not_bound_to_latest")
        revision_no = latest_revision + 1
    conn.execute(
        f"""INSERT INTO {TARGET_TABLE}(
            target_contract_id,target_identity,resolution_local_date,target_contract_sha256,
            supersedes_target_contract_sha256,revision_no,contract_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            _text(row["target_contract_id"]),
            _text(row["target_identity"]),
            _text(row["resolution_local_date"])[:10],
            _text(row["target_contract_sha256"]),
            supersedes or None,
            revision_no,
            encoded,
            _text(created_at),
        ),
    )
    return {
        "schema": REGISTRY_SCHEMA_VERSION,
        "ok": True,
        "idempotent": False,
        "revision_no": revision_no,
        "shadow_only": True,
    }


def load_latest_resolution_target_contract_shadow(
    conn: sqlite3.Connection,
    *,
    target_identity: str,
    resolution_local_date: str,
) -> dict[str, Any] | None:
    """Load exactly one latest immutable contract from the isolated registry."""

    _assert_isolated_shadow_db(conn)
    if not _table_exists(conn, TARGET_TABLE):
        raise ShadowTransactionIntegrityError("resolution_target_contract_shadow_schema_missing")
    row = conn.execute(
        f"""SELECT contract_json FROM {TARGET_TABLE}
            WHERE target_identity=? AND resolution_local_date=?
            ORDER BY revision_no DESC LIMIT 1""",
        (_text(target_identity), _text(resolution_local_date)[:10]),
    ).fetchone()
    return dict(json.loads(row[0])) if row is not None else None


def list_latest_resolution_target_contracts_shadow(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return one latest immutable target contract per target/day, read-only."""

    _assert_isolated_shadow_db(conn)
    if not _table_exists(conn, TARGET_TABLE):
        raise ShadowTransactionIntegrityError("resolution_target_contract_shadow_schema_missing")
    rows = conn.execute(
        f"""SELECT contract_json FROM {TARGET_TABLE}
            WHERE (target_identity,resolution_local_date,revision_no) IN (
                SELECT target_identity,resolution_local_date,MAX(revision_no)
                FROM {TARGET_TABLE}
                GROUP BY target_identity,resolution_local_date
            )
            ORDER BY resolution_local_date,target_identity"""
    ).fetchall()
    return [dict(json.loads(row[0])) for row in rows]


__all__ = [
    "REGISTRY_SCHEMA_VERSION",
    "TARGET_TABLE",
    "initialize_isolated_resolution_target_contract_schema",
    "list_latest_resolution_target_contracts_shadow",
    "load_latest_resolution_target_contract_shadow",
    "persist_resolution_target_contract_shadow",
]
