"""PR12 schema seam for canonical AWC observation materialization."""

from __future__ import annotations

from collections import Counter
import sqlite3
from typing import Any, Mapping

from .awc_observation_builder import BUILDER_RECEIPT_SCHEMA
from .awc_observation_contract import CanonicalAWCObservationContract
from .canonical_expected_scope_manifest import build_scope_basis
from .contracts import ContractError, content_sha256


SCHEMA = "bayes_basket_target_pr12_awc_observation_schema_v1"
EXPECTED_SCOPE_BUNDLE_SCHEMA = (
    "bayes_basket_target_pr12_awc_expected_scope_basis_bundle_v1"
)


def _sealed_builder_receipt(
    raw: Mapping[str, Any], *, contract: CanonicalAWCObservationContract
) -> dict[str, Any]:
    value = dict(raw)
    supplied = str(value.pop("receipt_sha256", "")).strip().lower()
    if supplied != content_sha256(value):
        raise ContractError("pr12_expected_basis_builder_receipt_hash_mismatch")
    if value.get("schema") != BUILDER_RECEIPT_SCHEMA:
        raise ContractError("pr12_expected_basis_builder_receipt_schema_invalid")
    if value.get("contract_content_sha256") != contract.content_sha256:
        raise ContractError("pr12_expected_basis_contract_mismatch")
    if int(value.get("revision_fact_count", -1)) != len(contract.revision_facts):
        raise ContractError("pr12_expected_basis_revision_count_mismatch")
    if int(value.get("day_bounds_fact_count", -1)) != len(contract.day_bounds_facts):
        raise ContractError("pr12_expected_basis_day_bounds_count_mismatch")
    if value.get("publishes_pr04_pointer") is not False or value.get(
        "starts_services"
    ) is not False:
        raise ContractError("pr12_expected_basis_release_boundary_invalid")
    return {**value, "receipt_sha256": supplied}


def build_pr12_expected_scope_bases(
    contract_raw: Mapping[str, Any],
    builder_receipt_raw: Mapping[str, Any],
    *,
    release_as_of_utc: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build PR12 bases from its frozen domain contract, never observed typed rows."""

    contract = CanonicalAWCObservationContract.from_mapping(contract_raw)
    builder_receipt = _sealed_builder_receipt(
        builder_receipt_raw, contract=contract
    )
    terminal_gaps = list(builder_receipt.get("terminal_gaps") or ())
    unresolved = (
        (builder_receipt["receipt_sha256"],) if terminal_gaps else ()
    )
    source_hashes = (
        contract.content_sha256,
        builder_receipt["receipt_sha256"],
        str(builder_receipt["evidence_receipt_sha256"]),
    )
    observation_keys = (
        {
            "scope_kind": "REVISION",
            "natural_key": {
                "station_id": fact.raw["station_id"],
                "event_date": fact.raw["event_date"],
                "settlement_timezone": fact.raw["settlement_timezone"],
                "observation_id": fact.raw["observation_id"],
                "revision_no": fact.raw["revision_no"],
                "observed_at_utc": fact.raw["observed_at_utc"],
                "canonical_committed_at_utc": fact.raw[
                    "canonical_committed_at_utc"
                ],
                "source_lane": fact.raw["source_lane"],
            },
        }
        for fact in contract.revision_facts
    )
    day_keys = (
        {
            "scope_kind": "DAY_BOUNDS",
            "natural_key": {
                "station_id": fact.raw["station_id"],
                "event_date": fact.raw["event_date"],
                "settlement_timezone": fact.raw["settlement_timezone"],
                "bounds_version": fact.raw["bounds_version"],
            },
        }
        for fact in contract.day_bounds_facts
    )
    observation_basis = build_scope_basis(
        scope_type="TRUTH_OBSERVATION",
        as_of_utc=release_as_of_utc,
        expected_keys=observation_keys,
        source_content_sha256s=source_hashes,
        unresolved_input_receipts=unresolved,
    )
    day_basis = build_scope_basis(
        scope_type="TRUTH_DAY",
        as_of_utc=release_as_of_utc,
        expected_keys=day_keys,
        source_content_sha256s=source_hashes,
        unresolved_input_receipts=unresolved,
    )
    gap_codes = Counter(
        str(item.get("failure_code") or "UNKNOWN")
        for item in terminal_gaps
        if isinstance(item, Mapping)
    )
    gap_stations = Counter(
        str(item.get("station_id") or "UNKNOWN")
        for item in terminal_gaps
        if isinstance(item, Mapping)
    )
    identity = {
        "schema": EXPECTED_SCOPE_BUNDLE_SCHEMA,
        "state": "BLOCKED" if terminal_gaps else "MATERIALIZED_UNPUBLISHED",
        "release_state": "BLOCKED" if terminal_gaps else "MATERIALIZED_UNPUBLISHED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "as_of_utc": observation_basis["as_of_utc"],
        "contract_content_sha256": contract.content_sha256,
        "builder_receipt_sha256": builder_receipt["receipt_sha256"],
        "truth_observation_basis_content_sha256": observation_basis[
            "content_sha256"
        ],
        "truth_day_basis_content_sha256": day_basis["content_sha256"],
        "truth_observation_expected_key_count": len(
            observation_basis["expected_keys"]
        ),
        "truth_day_expected_key_count": len(day_basis["expected_keys"]),
        "terminal_gap_count": len(terminal_gaps),
        "terminal_gap_counts_by_code": dict(sorted(gap_codes.items())),
        "terminal_gap_counts_by_station": dict(sorted(gap_stations.items())),
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return observation_basis, day_basis, {
        **identity,
        "content_sha256": content_sha256(identity),
    }


def stage_observation_timezone_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    """Add the required identity carrier without backfilling legacy rows.

    The caller owns the outer transaction.  Existing rows remain explicitly
    unbound and therefore cannot enter P1; only the canonical AWC ingress may
    append new rows with a proved settlement timezone.
    """

    if not conn.in_transaction:
        raise ContractError("pr12_observation_schema_outer_transaction_required")
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='basket_target_observation_facts'"
    ).fetchone()
    if table is None:
        raise ContractError("pr12_observation_fact_table_missing")
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(basket_target_observation_facts)")
    }
    before = int(
        conn.execute("SELECT COUNT(*) FROM basket_target_observation_facts").fetchone()[0]
    )
    added_columns: list[str] = []
    if "settlement_timezone" not in columns:
        conn.execute(
            "ALTER TABLE basket_target_observation_facts "
            "ADD COLUMN settlement_timezone TEXT"
        )
        added_columns.append("settlement_timezone")
    if "observed_at_utc" not in columns:
        conn.execute(
            "ALTER TABLE basket_target_observation_facts "
            "ADD COLUMN observed_at_utc TEXT"
        )
        added_columns.append("observed_at_utc")
    if "awc_source_receipt_sha256" not in columns:
        conn.execute(
            "ALTER TABLE basket_target_observation_facts "
            "ADD COLUMN awc_source_receipt_sha256 TEXT"
        )
        added_columns.append("awc_source_receipt_sha256")
    state = "STAGED_NULLABLE_WITHOUT_BACKFILL" if added_columns else "ALREADY_PRESENT"
    unbound = int(
        conn.execute(
            "SELECT COUNT(*) FROM basket_target_observation_facts "
            "WHERE settlement_timezone IS NULL OR trim(settlement_timezone)=''"
        ).fetchone()[0]
    )
    identity = {
        "schema": SCHEMA,
        "state": state,
        "rows_preserved": before,
        "legacy_rows_left_unbound": unbound,
        "added_nullable_columns": added_columns,
        "default_value_used": False,
        "registry_backfill_used": False,
        "cross_table_compatibility_used": False,
        "release_state": "MATERIALIZED_UNPUBLISHED",
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


__all__ = [
    "EXPECTED_SCOPE_BUNDLE_SCHEMA",
    "SCHEMA",
    "build_pr12_expected_scope_bases",
    "stage_observation_timezone_schema",
]
