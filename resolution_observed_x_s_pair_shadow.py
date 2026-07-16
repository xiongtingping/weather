"""Append raw final physical-X / settlement-S pairs before bridge calibration.

An observed X/S pair is deliberately *not* a final X→S→B label.  The bridge
cannot be assumed closed before enough such raw, source-bound pairs exist to
calibrate and validate it.  This registry therefore breaks the circular
dependency safely: it accumulates evidence for calibration but exposes no
bucket label, posterior input, or trading authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime
from typing import Any, Mapping

from resolution_final_source_runtime import PHYSICAL_ROLE, PHYSICAL_SOURCE_ID, SETTLEMENT_ROLE, SETTLEMENT_SOURCE_ID
from sequential_bayes_public_ssot_shadow_writer import SHADOW_MANIFEST_STATE, SHADOW_MANIFEST_TABLE
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError


SCHEMA_VERSION = "resolution_observed_x_s_pair_v1"
PAIR_TABLE = "resolution_observed_x_s_pairs"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _time(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sha256(value: Any) -> bool:
    text = _text(value).lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_sentinel(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ShadowTransactionIntegrityError("resolution_observed_x_s_pair_shadow_manifest_missing")
    row = conn.execute(f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1").fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
        raise ShadowTransactionIntegrityError("resolution_observed_x_s_pair_shadow_manifest_invalid")


def initialize_isolated_resolution_observed_x_s_pair_schema(conn: sqlite3.Connection) -> None:
    _assert_sentinel(conn)
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {PAIR_TABLE} (
            observed_x_s_pair_sha256 TEXT PRIMARY KEY,
            target_contract_sha256 TEXT NOT NULL,
            target_identity TEXT NOT NULL,
            event_date TEXT NOT NULL,
            physical_source_observation_id TEXT NOT NULL,
            settlement_source_observation_id TEXT NOT NULL,
            pair_json TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(target_contract_sha256,event_date)
        )"""
    )
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_{PAIR_TABLE}_no_update
        BEFORE UPDATE ON {PAIR_TABLE} BEGIN SELECT RAISE(ABORT, 'resolution_observed_x_s_pair_append_only'); END"""
    )
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_{PAIR_TABLE}_no_delete
        BEFORE DELETE ON {PAIR_TABLE} BEGIN SELECT RAISE(ABORT, 'resolution_observed_x_s_pair_append_only'); END"""
    )


def _source_surface(observation: Mapping[str, Any], *, expected_source: str, expected_role: str, scalar: str) -> tuple[dict[str, Any], list[str]]:
    row = _mapping(observation)
    payload = _mapping(row.get("payload"))
    issues: list[str] = []
    if _text(row.get("source_id")) != expected_source or _text(row.get("role")) != expected_role:
        issues.append("observed_x_s_pair_final_source_identity_invalid")
    if _text(row.get("source_class")) != "settlement_source" or _text(row.get("station_id")).upper() != "ZUUU":
        issues.append("observed_x_s_pair_final_source_role_or_station_invalid")
    if _text(payload.get("label_finality")) != "final":
        issues.append("observed_x_s_pair_final_source_not_final")
    if not _text(row.get("source_observation_id")) or not _text(row.get("raw_ref")) or not _sha256(row.get("raw_sha256")):
        issues.append("observed_x_s_pair_final_source_raw_lineage_invalid")
    value = _finite(payload.get(scalar))
    if value is None:
        issues.append("observed_x_s_pair_final_source_scalar_invalid")
    return {
        "source_observation_id": _text(row.get("source_observation_id")),
        "source_id": _text(row.get("source_id")),
        "raw_sha256": _text(row.get("raw_sha256")),
        "raw_ref": _text(row.get("raw_ref")),
        "observed_at": _text(row.get("fetched_at")),
        "value_c": value,
        "event_date": _text(row.get("event_date"))[:10],
    }, issues


def build_observed_x_s_pair(
    *,
    target_contract: Mapping[str, Any],
    market_finality_attestation: Mapping[str, Any],
    physical_final_source: Mapping[str, Any],
    settlement_final_source: Mapping[str, Any],
    captured_at: str,
) -> dict[str, Any]:
    """Build an immutable calibration pair without deriving B or closing bridge."""

    target = _mapping(target_contract)
    finality = _mapping(market_finality_attestation)
    physical, issues = _source_surface(
        physical_final_source, expected_source=PHYSICAL_SOURCE_ID, expected_role=PHYSICAL_ROLE, scalar="final_physical_high_c"
    )
    settlement, settlement_issues = _source_surface(
        settlement_final_source, expected_source=SETTLEMENT_SOURCE_ID, expected_role=SETTLEMENT_ROLE, scalar="settlement_scalar_c"
    )
    issues.extend(settlement_issues)
    contract_sha = _text(target.get("target_contract_sha256"))
    target_identity = _text(target.get("target_identity"))
    day = _text(target.get("resolution_local_date"))[:10]
    authority_contract_id = _text(target.get("authority_contract_id"))
    authority_contract_sha256 = _text(target.get("authority_contract_sha256"))
    target_resolution_source_id = _text(target.get("resolution_source_id"))
    if not _sha256(contract_sha) or not target_identity or not day:
        issues.append("observed_x_s_pair_target_contract_identity_invalid")
    if target.get("bridge_closed") is not False or target.get("validated") is not False:
        issues.append("observed_x_s_pair_target_contract_data_accumulation_state_invalid")
    if (
        authority_contract_id != "zuuu_amsc_wunderground_daily_high_v4"
        or not _sha256(authority_contract_sha256)
        or target_resolution_source_id != SETTLEMENT_SOURCE_ID
    ):
        issues.append("observed_x_s_pair_target_contract_authority_or_settlement_source_mismatch")
    if finality.get("ok") is not True or finality.get("market_resolution_final") is not True:
        issues.append("observed_x_s_pair_market_finality_not_attested")
    if _text(finality.get("target_contract_sha256")) != contract_sha or _text(finality.get("target_identity")) != target_identity or _text(finality.get("event_date"))[:10] != day:
        issues.append("observed_x_s_pair_market_finality_contract_binding_invalid")
    if physical["event_date"] != day or settlement["event_date"] != day:
        issues.append("observed_x_s_pair_final_source_event_date_mismatch")
    if not _time(captured_at):
        issues.append("observed_x_s_pair_captured_at_invalid")
    if issues:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(issues)), "read_only": True, "shadow_only": True, "places_orders": False}
    core = {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available_for_bridge_calibration_only",
        "target_contract_sha256": contract_sha,
        "target_identity": target_identity,
        "event_date": day,
        "authority_contract_id": authority_contract_id,
        "authority_contract_sha256": authority_contract_sha256,
        "target_resolution_source_id": target_resolution_source_id,
        "final_physical_high_c": physical["value_c"],
        "settlement_scalar_c": settlement["value_c"],
        "physical_source": physical,
        "settlement_source": settlement,
        "market_finality_raw_sha256": _text(finality.get("market_finality_raw_sha256")),
        "market_finality_raw_ref": _text(finality.get("market_finality_raw_ref")),
        "market_finality_observed_at": _text(finality.get("resolution_state_observed_at")),
        "captured_at": _text(captured_at),
        "bridge_closed": False,
        "market_settlement_bucket": None,
        "market_settlement_bucket_na_reason": "x_to_s_to_b_bridge_data_accumulation_not_calibrated",
        "read_only": True,
        "shadow_only": True,
        "places_orders": False,
    }
    return {**core, "observed_x_s_pair_sha256": _sha(core)}


def persist_observed_x_s_pair_shadow(conn: sqlite3.Connection, pair: Mapping[str, Any]) -> dict[str, Any]:
    _assert_sentinel(conn)
    if not _table_exists(conn, PAIR_TABLE):
        raise ShadowTransactionIntegrityError("resolution_observed_x_s_pair_shadow_schema_missing")
    row = _mapping(pair)
    if row.get("ok") is not True or _text(row.get("state")) != "available_for_bridge_calibration_only":
        raise ValueError("resolution_observed_x_s_pair_not_ready")
    supplied = _text(row.get("observed_x_s_pair_sha256"))
    unsigned = {key: value for key, value in row.items() if key != "observed_x_s_pair_sha256"}
    if not _sha256(supplied) or _sha(unsigned) != supplied:
        raise ValueError("resolution_observed_x_s_pair_sha256_invalid")
    existing = conn.execute(
        f"SELECT pair_json FROM {PAIR_TABLE} WHERE target_contract_sha256=? AND event_date=?",
        (_text(row.get("target_contract_sha256")), _text(row.get("event_date"))[:10]),
    ).fetchone()
    encoded = _canonical(row)
    if existing is not None:
        if _text(existing[0]) != encoded:
            raise ShadowTransactionIntegrityError("resolution_observed_x_s_pair_replay_drift")
        return {"schema": SCHEMA_VERSION, "ok": True, "idempotent": True, "shadow_only": True}
    conn.execute(
        f"""INSERT INTO {PAIR_TABLE}(
            observed_x_s_pair_sha256,target_contract_sha256,target_identity,event_date,
            physical_source_observation_id,settlement_source_observation_id,pair_json,observed_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            supplied, _text(row.get("target_contract_sha256")), _text(row.get("target_identity")), _text(row.get("event_date"))[:10],
            _text(_mapping(row.get("physical_source")).get("source_observation_id")),
            _text(_mapping(row.get("settlement_source")).get("source_observation_id")), encoded,
            _text(row.get("captured_at")), _text(row.get("captured_at")),
        ),
    )
    return {"schema": SCHEMA_VERSION, "ok": True, "idempotent": False, "shadow_only": True}


__all__ = [
    "PAIR_TABLE", "SCHEMA_VERSION", "build_observed_x_s_pair",
    "initialize_isolated_resolution_observed_x_s_pair_schema", "persist_observed_x_s_pair_shadow",
]
