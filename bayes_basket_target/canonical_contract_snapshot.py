"""Read-only V12 snapshot builder for the frozen PR04 release contract.

This is a new producer, not an adapter for an older contract.  It reads all
eight V4 canonical fact arrays from one V12 SQLite snapshot.  V12 extension
facts (republisher/bridge/P0) are intentionally not renamed into V4 arrays;
their typed builders remain authoritative.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from .canonical_contract import CONTRACT_VERSION, SCHEMA, CanonicalContract
from .canonical_ssot_schema import inspect_canonical_ssot_schema
from .contracts import content_sha256
from .resource_bounds import assert_process_memory_budget, assert_target_db_capacity
from .resource_bounds import load_bounded_json


class CanonicalContractSnapshotError(ValueError):
    """Raised when a V12 fact cannot enter the exact PR04 snapshot."""


BUILDER_ID = "pr04.v12.canonical_v4_snapshot.v1"
INCLUDED_FACT_TYPES = (
    "model",
    "ensemble_member",
    "native_arrival",
    "ensemble_member_receipt_manifest",
    "ensemble_member_arrival_binding",
    "truth",
    "residual",
    "activation",
)
COLLECTION_BY_FACT_TYPE = {
    "model": "model_facts",
    "ensemble_member": "ensemble_member_facts",
    "native_arrival": "native_arrival_facts",
    "ensemble_member_receipt_manifest": "ensemble_member_receipt_manifest_facts",
    "ensemble_member_arrival_binding": "ensemble_member_arrival_binding_facts",
    "truth": "truth_facts",
    "residual": "residual_facts",
    "activation": "activation_facts",
}
MAX_SELECTED_FACTS = 150_000
# The audited V11 member payload is about 76 MiB.  This object-specific guard
# is deliberately broader than the generic 64 MiB input cap but remains hard.
MAX_CANONICAL_CONTRACT_SNAPSHOT_BYTES = 128 * 1024 * 1024
MAX_CANONICAL_CONTRACT_SNAPSHOT_NODES = 4_000_000


def _utc_z(value: str) -> str:
    text = str(value or "")
    if len(text) != 20 or text[4] != "-" or text[7] != "-" or text[10] != "T" or text[-1] != "Z":
        raise CanonicalContractSnapshotError("canonical_contract_snapshot_as_of_invalid")
    return text


def build_canonical_contract_snapshot(
    db_path: Path | str,
    *,
    as_of_utc: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build and re-parse one exact V12 contract without writing the database."""

    cutoff = _utc_z(as_of_utc)
    source = assert_target_db_capacity(Path(db_path).expanduser().resolve(strict=True))
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    collections: dict[str, list[dict[str, Any]]] = {
        name: [] for name in COLLECTION_BY_FACT_TYPE.values()
    }
    selected_rows: list[dict[str, str]] = []
    selected_json_bytes = 0
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        schema_status = inspect_canonical_ssot_schema(conn)
        database_schema = str(schema_status.get("state") or "")
        if database_schema not in {"V11", "V12", "V13", "V14"} or schema_status.get("ok") is not True:
            raise CanonicalContractSnapshotError(
                "canonical_contract_snapshot_requires_v11_v12_v13_or_v14"
            )
        if database_schema == "V14":
            # The V4-shaped release snapshot has no arrays for the four new
            # V14 P0 facts.  Accepting V14 here would silently omit their
            # lineage from PR04, so release construction remains blocked
            # until the final PR04 merger gains an exact V14 contract.
            raise CanonicalContractSnapshotError(
                "canonical_contract_snapshot_v14_release_blocked"
            )
        placeholders = ",".join("?" for _ in INCLUDED_FACT_TYPES)
        rows = conn.execute(
            "SELECT fact_id,fact_type,content_sha256,fact_json,ingested_at_utc "
            f"FROM canonical_ssot_fact WHERE fact_type IN ({placeholders}) "
            "AND ingested_at_utc<=? ORDER BY fact_type,fact_id",
            (*INCLUDED_FACT_TYPES, cutoff),
        )
        for fact_id, fact_type, stored_sha, fact_json, ingested_at in rows:
            if len(selected_rows) >= MAX_SELECTED_FACTS:
                raise CanonicalContractSnapshotError("canonical_contract_snapshot_fact_limit")
            raw_text = str(fact_json)
            selected_json_bytes += len(raw_text.encode("utf-8"))
            if selected_json_bytes > MAX_CANONICAL_CONTRACT_SNAPSHOT_BYTES:
                raise CanonicalContractSnapshotError("canonical_contract_snapshot_json_bytes_limit")
            try:
                fact = json.loads(raw_text)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CanonicalContractSnapshotError(
                    "canonical_contract_snapshot_fact_json_invalid"
                ) from exc
            if not isinstance(fact, dict):
                raise CanonicalContractSnapshotError("canonical_contract_snapshot_fact_not_object")
            fact_type_text = str(fact_type)
            stored_sha_text = str(stored_sha)
            if (
                fact_type_text not in COLLECTION_BY_FACT_TYPE
                or str(fact_id) != f"{fact_type_text}:{stored_sha_text}"
                or fact.get("content_sha256") != stored_sha_text
            ):
                raise CanonicalContractSnapshotError("canonical_contract_snapshot_fact_identity_mismatch")
            collections[COLLECTION_BY_FACT_TYPE[fact_type_text]].append(fact)
            selected_rows.append(
                {
                    "fact_id": str(fact_id),
                    "fact_type": fact_type_text,
                    "content_sha256": stored_sha_text,
                    "ingested_at_utc": str(ingested_at),
                }
            )
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.rollback()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()

    assert_process_memory_budget(projected_bytes=selected_json_bytes)
    builder_id = f"pr04.{database_schema.lower()}.canonical_v4_snapshot.v1"
    identity = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": cutoff,
        "builder_id": builder_id,
        "model_facts": collections["model_facts"],
        "ensemble_member_facts": collections["ensemble_member_facts"],
        "native_arrival_facts": collections["native_arrival_facts"],
        "ensemble_member_receipt_manifest_facts": collections[
            "ensemble_member_receipt_manifest_facts"
        ],
        "ensemble_member_arrival_binding_facts": collections[
            "ensemble_member_arrival_binding_facts"
        ],
        "truth_facts": collections["truth_facts"],
        "residual_facts": collections["residual_facts"],
        "activation_facts": collections["activation_facts"],
    }
    contract = CanonicalContract.from_mapping(
        {**identity, "content_sha256": content_sha256(identity)}
    ).to_mapping()
    counts = {name: len(contract[name]) for name in (
        "model_facts",
        "ensemble_member_facts",
        "native_arrival_facts",
        "ensemble_member_receipt_manifest_facts",
        "ensemble_member_arrival_binding_facts",
        "truth_facts",
        "residual_facts",
        "activation_facts",
    )}
    receipt_identity = {
        "schema": f"bayes_basket_target_pr04_{database_schema.lower()}_contract_snapshot_receipt_v1",
        "state": "READY",
        "database": str(source),
        "database_schema": database_schema,
        "database_data_version": data_version,
        "database_schema_version": schema_version,
        "as_of_utc": cutoff,
        "builder_id": builder_id,
        "included_fact_types": list(INCLUDED_FACT_TYPES),
        "extension_fact_policy": "TYPED_SCOPE_ONLY_NO_V4_ALIAS",
        "fact_counts": counts,
        "selected_fact_count": len(selected_rows),
        "selected_rows_content_sha256": content_sha256(selected_rows),
        "contract_content_sha256": contract["content_sha256"],
        "writes_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "production_no_live": True,
    }
    return contract, {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }


def rebind_truth_gap_evidence(
    source_receipt_path: Path | str,
    contract_mapping: dict[str, Any],
) -> dict[str, Any]:
    """Bind terminal station gaps to a new V4 snapshot after exact truth proof."""

    source_path = Path(source_receipt_path).expanduser().resolve(strict=True)
    source_raw = source_path.read_bytes()
    source = load_bounded_json(source_path)
    if not isinstance(source, dict):
        raise CanonicalContractSnapshotError("truth_gap_rebind_source_mapping_required")
    supplied = source.get("content_sha256")
    if (
        source.get("schema")
        != "bayes_basket_target_historical_awc_truth_contract_build_v1"
        or supplied
        != content_sha256(
            {key: value for key, value in source.items() if key != "content_sha256"}
        )
    ):
        raise CanonicalContractSnapshotError("truth_gap_rebind_source_invalid")
    contract = CanonicalContract.from_mapping(contract_mapping)
    contract_pairs = {
        (fact.station_id, fact.event_date) for fact in contract.truth_facts
    }
    stations = source.get("stations")
    if not isinstance(stations, list) or len(stations) != 45:
        raise CanonicalContractSnapshotError("truth_gap_rebind_station_scope_invalid")
    receipt_pairs: set[tuple[str, str]] = set()
    station_ids: list[str] = []
    for row in stations:
        if not isinstance(row, dict):
            raise CanonicalContractSnapshotError("truth_gap_rebind_station_row_invalid")
        station_id = str(row.get("station_id") or "")
        ready_dates = row.get("truth_ready_dates")
        if not station_id or not isinstance(ready_dates, list):
            raise CanonicalContractSnapshotError("truth_gap_rebind_station_row_invalid")
        station_ids.append(station_id)
        receipt_pairs.update((station_id, str(event_date)) for event_date in ready_dates)
    if (
        station_ids != sorted(set(station_ids))
        or receipt_pairs != contract_pairs
        or source.get("truth_fact_count") != len(contract_pairs)
    ):
        raise CanonicalContractSnapshotError("truth_gap_rebind_truth_scope_mismatch")
    identity = {
        **{key: value for key, value in source.items() if key != "content_sha256"},
        "as_of_utc": contract.as_of_utc,
        "builder_id": "pr04.v11.truth_gap_rebind.v1",
        "canonical_contract_sha256": contract.content_sha256,
        "source_truth_gap_content_sha256": str(supplied),
        "source_truth_gap_file_sha256": hashlib.sha256(source_raw).hexdigest(),
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "production_no_live": True,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "BUILDER_ID",
    "MAX_CANONICAL_CONTRACT_SNAPSHOT_BYTES",
    "MAX_CANONICAL_CONTRACT_SNAPSHOT_NODES",
    "CanonicalContractSnapshotError",
    "build_canonical_contract_snapshot",
    "rebind_truth_gap_evidence",
]
