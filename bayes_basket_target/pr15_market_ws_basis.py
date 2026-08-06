"""PR15 canonical market registry basis and unresolved WS snapshot terminals."""

from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from .canonical_contract import CanonicalTypedScopeEnvelope, TYPED_SCOPE_ENVELOPE_SCHEMA, normalize_typed_scope_natural_key
from .canonical_expected_scope_manifest import build_scope_basis
from .contracts import ContractError, content_sha256
from .market_bucket_bridge import CanonicalMarketBucketBridgeFact
from .station_registry import coordinate_registry_manifest

SCHEMA = "bayes_basket_target_pr15_market_ws_terminal_bundle_v1"
BUILDER_ID = "pr15.canonical_market_ws_basis.v1"

class PR15MarketWSBasisError(ContractError):
    pass

def _utc(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PR15MarketWSBasisError("pr15_as_of_invalid") from exc
    if parsed.tzinfo is None:
        raise PR15MarketWSBasisError("pr15_as_of_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _record(key: dict[str, Any], *, as_of: str, evidence_sha: str) -> dict[str, Any]:
    identity = {
        "scope_type": "MARKET_WS", "scope_kind": "REGISTRY",
        "natural_key": normalize_typed_scope_natural_key(scope_type="MARKET_WS", scope_kind="REGISTRY", raw=key),
        "version": None, "as_of_utc": as_of, "state": "DATA_READY", "blockers": [],
        "evidence_content_sha256": evidence_sha,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    return {**identity, "content_sha256": content_sha256(identity)}

def build_pr15_market_ws_artifacts(db_path: str | Path, *, as_of_utc: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    cutoff = _utc(as_of_utc)
    source = Path(db_path).expanduser().resolve(strict=True)
    registry = coordinate_registry_manifest()
    stations = list(registry["station_ids"])
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON"); conn.execute("BEGIN")
        rows = conn.execute("SELECT fact_id,content_sha256,fact_json,ingested_at_utc FROM canonical_ssot_fact WHERE fact_type='market_bucket_bridge' AND ingested_at_utc<=? ORDER BY fact_id", (cutoff,)).fetchall()
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0]); schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.rollback()
    except sqlite3.Error as exc:
        if conn.in_transaction: conn.rollback()
        raise PR15MarketWSBasisError("pr15_read_failed") from exc
    finally:
        conn.close()
    facts: dict[str, CanonicalMarketBucketBridgeFact] = {}
    sources: list[dict[str, str]] = []
    for fact_id, stored_sha, raw_json, ingested_at in rows:
        try: fact = CanonicalMarketBucketBridgeFact.from_mapping(json.loads(raw_json))
        except (ContractError, json.JSONDecodeError) as exc: raise PR15MarketWSBasisError("pr15_bridge_contract_invalid") from exc
        if stored_sha != fact.content_sha256 or _utc(ingested_at) > cutoff: raise PR15MarketWSBasisError("pr15_bridge_hash_or_asof_invalid")
        station = fact.raw["station_id"]
        if station in facts: raise PR15MarketWSBasisError("pr15_bridge_station_duplicate")
        facts[station] = fact; sources.append({"fact_id": str(fact_id), "content_sha256": fact.content_sha256})
    if set(facts) != set(stations): raise PR15MarketWSBasisError("pr15_bridge_set_not_exact_45")
    inventory_sha = content_sha256(sources)
    expected_keys: list[dict[str, Any]] = []; typed_records: list[dict[str, Any]] = []; terminals: list[dict[str, Any]] = []
    for station in stations:
        fact = facts[station]
        for bucket in fact.raw["bucket_contracts"]:
            key = {"station_id": station, "event_date": fact.raw["event_date"], "market_id": bucket["market_id"], "yes_token_id": bucket["yes_token"], "bucket_contract_sha256": fact.raw["registry_scope_rowset_sha256"]}
            expected_keys.append({"scope_kind": "REGISTRY", "natural_key": key})
            typed_records.append(_record(key, as_of=cutoff, evidence_sha=fact.content_sha256))
        terminal_identity = {
            "schema": "bayes_basket_target_pr15_ws_snapshot_station_terminal_v1",
            "station_id": station, "event_date": fact.raw["event_date"], "as_of_utc": cutoff,
            "bridge_content_sha256": fact.content_sha256, "state": "BLOCKED",
            "blockers": ["WS_SNAPSHOT_MISSING"], "unproven_natural_key_fields": ["ws_sequence", "snapshot_at_utc"],
            "legacy_ws_snapshot_used": False,
        }
        terminals.append({**terminal_identity, "content_sha256": content_sha256(terminal_identity)})
    expected = build_scope_basis(scope_type="MARKET_WS", as_of_utc=cutoff, expected_keys=expected_keys, source_content_sha256s=(registry["registry_sha256"], inventory_sha), unresolved_input_receipts=[x["content_sha256"] for x in terminals])
    envelope_identity = {"schema": TYPED_SCOPE_ENVELOPE_SCHEMA, "as_of_utc": cutoff, "builder_id": BUILDER_ID, "scope_type": "MARKET_WS", "records": typed_records, "production_no_live": True, "future_data_counts_as_oos": False}
    envelope = {**envelope_identity, "content_sha256": content_sha256(envelope_identity)}; CanonicalTypedScopeEnvelope.from_mapping(envelope)
    terminal_identity = {"schema": "bayes_basket_target_pr15_ws_snapshot_terminal_basis_v1", "as_of_utc": cutoff, "state": "BLOCKED", "terminal_scope_count": 45, "terminal_scopes": terminals, "writes_database": False, "calls_order_gateway": False, "orders_submitted": 0}
    terminal_basis = {**terminal_identity, "content_sha256": content_sha256(terminal_identity)}
    bundle_identity = {"schema": SCHEMA, "as_of_utc": cutoff, "state": "BLOCKED", "release_state": "BLOCKED", "registry_expected_key_count": len(expected_keys), "registry_typed_record_count": len(typed_records), "snapshot_terminal_scope_count": 45, "snapshot_typed_record_count": 0, "expected_basis_content_sha256": expected["content_sha256"], "registry_envelope_content_sha256": envelope["content_sha256"], "terminal_basis_content_sha256": terminal_basis["content_sha256"], "database": str(source), "database_data_version": data_version, "database_schema_version": schema_version, "publishes_pr04_pointer": False, "starts_services": False, "calls_order_gateway": False, "orders_submitted": 0, "production_no_live": True}
    bundle = {**bundle_identity, "content_sha256": content_sha256(bundle_identity)}
    return expected, envelope, terminal_basis, bundle

__all__ = ["PR15MarketWSBasisError", "build_pr15_market_ws_artifacts"]
