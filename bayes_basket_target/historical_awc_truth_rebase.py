"""Preserve immutable canonical truth identities during PR02.C append."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .canonical_contract import (
    CONTRACT_VERSION,
    SCHEMA as CONTRACT_SCHEMA,
    CanonicalContract,
    CanonicalTruthFact,
)
from .contracts import ContractError, content_sha256


SCHEMA = "historical_awc_truth_canonical_identity_rebase_v1"
TRUTH_GAP_BUILD_SCHEMA = "bayes_basket_target_historical_awc_truth_contract_build_v1"
BUILDER_ID = "truth.awc_historical_replay.pr02c_append.v1"
_SEMANTIC_FIELDS = (
    "station_id",
    "event_date",
    "truth_revision_no",
    "truth_value",
    "station_day_end_utc",
    "closed_day",
    "source_lane",
    "source_role",
    "authority_scope",
    "settlement_timezone",
)


def rebase_historical_awc_truth_contract(
    proposed_contract: Mapping[str, Any],
    *,
    existing_truth_facts: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a contract that replays existing identities and appends gaps.

    A later replay envelope changes its evidence hash and as-of even when the
    underlying daily truth did not change.  Canonical facts are append-only:
    for an occupied natural key, require all business semantics to match and
    retain the exact stored fact.  Any semantic difference fails closed and
    must enter the explicit truth revision/correction workflow.
    """

    proposed = CanonicalContract.from_mapping(proposed_contract)
    if (
        proposed.model_facts
        or proposed.ensemble_member_facts
        or proposed.native_arrival_facts
        or proposed.ensemble_member_receipt_manifest_facts
        or proposed.ensemble_member_arrival_binding_facts
        or proposed.residual_facts
        or proposed.activation_facts
    ):
        raise ContractError("historical_awc_truth_rebase_truth_only_required")
    existing_by_scope: dict[str, CanonicalTruthFact] = {}
    for raw in existing_truth_facts:
        fact = CanonicalTruthFact.from_mapping(raw)
        if fact.scope_key in existing_by_scope:
            raise ContractError("historical_awc_truth_rebase_existing_scope_duplicate")
        existing_by_scope[fact.scope_key] = fact

    rebased_rows: list[dict[str, Any]] = []
    preserved_scopes: list[str] = []
    append_scopes: list[str] = []
    for proposed_fact in proposed.truth_facts:
        existing = existing_by_scope.get(proposed_fact.scope_key)
        if existing is None:
            append_scopes.append(proposed_fact.scope_key)
            rebased_rows.append(proposed_fact.to_mapping())
            continue
        proposed_mapping = proposed_fact.to_mapping()
        existing_mapping = existing.to_mapping()
        if any(
            proposed_mapping[field] != existing_mapping[field]
            for field in _SEMANTIC_FIELDS
        ):
            raise ContractError(
                "historical_awc_truth_rebase_existing_scope_semantic_conflict"
            )
        preserved_scopes.append(proposed_fact.scope_key)
        rebased_rows.append(existing_mapping)

    if set(existing_by_scope) - set(preserved_scopes):
        # A PR02.C append contract may not silently drop an active canonical
        # truth scope.  The current full replay must cover every active fact.
        raise ContractError("historical_awc_truth_rebase_existing_scope_missing")
    identity = {
        "schema": CONTRACT_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": proposed.as_of_utc,
        "builder_id": BUILDER_ID,
        "model_facts": [],
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": sorted(
            rebased_rows,
            key=lambda row: (
                row["station_id"], row["event_date"], row["truth_revision_no"]
            ),
        ),
        "residual_facts": [],
        "activation_facts": [],
    }
    rebased = {**identity, "content_sha256": content_sha256(identity)}
    rebased = CanonicalContract.from_mapping(rebased).to_mapping()
    receipt_identity = {
        "schema": SCHEMA,
        "source_contract_content_sha256": proposed.content_sha256,
        "rebased_contract_content_sha256": rebased["content_sha256"],
        "source_truth_fact_count": len(proposed.truth_facts),
        "existing_truth_fact_count": len(existing_by_scope),
        "preserved_existing_truth_fact_count": len(preserved_scopes),
        "append_candidate_truth_fact_count": len(append_scopes),
        "existing_truth_fact_root_sha256": content_sha256(
            sorted(fact.content_sha256 for fact in existing_by_scope.values())
        ),
        "preserved_scope_root_sha256": content_sha256(sorted(preserved_scopes)),
        "append_scope_root_sha256": content_sha256(sorted(append_scopes)),
        "semantic_match_fields": list(_SEMANTIC_FIELDS),
        "boundary": {
            "production_no_live": True,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "overwrites_existing_truth": False,
        },
    }
    receipt = {**receipt_identity, "content_sha256": content_sha256(receipt_identity)}
    return rebased, receipt


def rebase_truth_gap_build_receipt(
    source_receipt: Mapping[str, Any],
    *,
    source_contract_content_sha256: str,
    rebased_contract_content_sha256: str,
    identity_rebase_receipt_content_sha256: str,
    preserved_existing_truth_fact_count: int,
    append_candidate_truth_fact_count: int,
) -> dict[str, Any]:
    if source_receipt.get("schema") != TRUTH_GAP_BUILD_SCHEMA:
        raise ContractError("historical_awc_truth_rebase_build_receipt_schema_invalid")
    supplied = str(source_receipt.get("content_sha256") or "")
    identity = {
        key: value for key, value in source_receipt.items() if key != "content_sha256"
    }
    if supplied != content_sha256(identity):
        raise ContractError("historical_awc_truth_rebase_build_receipt_sha_mismatch")
    if source_receipt.get("canonical_contract_sha256") != source_contract_content_sha256:
        raise ContractError("historical_awc_truth_rebase_build_receipt_contract_mismatch")
    if int(source_receipt.get("truth_fact_count") or -1) != (
        int(preserved_existing_truth_fact_count)
        + int(append_candidate_truth_fact_count)
    ):
        raise ContractError("historical_awc_truth_rebase_build_receipt_truth_count_mismatch")
    identity["builder_id"] = BUILDER_ID
    identity["canonical_contract_sha256"] = rebased_contract_content_sha256
    identity["canonical_identity_rebase"] = {
        "receipt_content_sha256": identity_rebase_receipt_content_sha256,
        "source_contract_content_sha256": source_contract_content_sha256,
        "preserved_existing_truth_fact_count": preserved_existing_truth_fact_count,
        "append_candidate_truth_fact_count": append_candidate_truth_fact_count,
        "overwrites_existing_truth": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "BUILDER_ID",
    "SCHEMA",
    "rebase_historical_awc_truth_contract",
    "rebase_truth_gap_build_receipt",
]
