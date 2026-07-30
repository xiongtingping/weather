"""Content-addressed settlement evidence and attribution terms.

This module is deliberately pure.  It accepts already materialized evidence
references and returns an immutable packet; it never opens a database, writes
an artifact, calls a service, or authorizes execution.  The packet is the
settlement-version boundary between a no-live label and later attribution.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .contracts import ContractError, content_sha256


SCHEMA = "bayes_basket_target_settlement_evidence_packet_v1"
SETTLEMENT_VERSION_BINDING_SCHEMA = (
    "bayes_basket_target_settlement_version_label_binding_v1"
)
TERMS_SCHEMA = "delta_basket_score_settled_attribution_terms_v1"
REQUESTED_INTENT_SEMANTICS = "hypothetical_requested_intents_not_fills"
NA_ACTUAL_FILL_EVIDENCE = "NA_ACTUAL_FILL_EVIDENCE_MISSING"
EPS = 1e-8


def _sha(value: Any, *, name: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"settlement_evidence_{name}_sha256_invalid")
    return result


def _number(value: Any, *, name: str, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise ContractError(f"settlement_evidence_{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"settlement_evidence_{name}_invalid") from exc
    if not math.isfinite(result) or (nonnegative and result < 0.0):
        raise ContractError(f"settlement_evidence_{name}_invalid")
    return result


def _required_text(value: Any, *, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"settlement_evidence_{name}_missing")
    return result


def build_attribution_terms(
    *,
    fact_id: str,
    fact_content_sha256: str,
    truth_sha256: str,
    winning_bucket: str,
    policy_version_vector_sha256: str,
    coverage_usdc: float,
    overlap_usdc: float = 0.0,
    edge_usdc: float = 0.0,
    execution_cost_usdc: float,
    failure_cost_usdc: float = 0.0,
    attribution_basis: str = "HYPOTHETICAL_REQUESTED_INTENTS",
    realized_attribution_status: str = "NA_PRODUCTION_NO_LIVE",
) -> dict[str, Any]:
    """Build and hash the five-term counterfactual attribution.

    ``actual_fill`` values are intentionally not accepted here.  A no-live
    label may describe requested intents only; missing actual-fill evidence is
    represented by the packet's explicit NA status, never by numeric zero.
    """

    core: dict[str, Any] = {
        "schema": TERMS_SCHEMA,
        "attribution_basis": _required_text(
            attribution_basis, name="attribution_basis"
        ),
        "realized_attribution_status": _required_text(
            realized_attribution_status, name="realized_attribution_status"
        ),
        "fact_id": _required_text(fact_id, name="fact_id"),
        "fact_content_sha256": _sha(
            fact_content_sha256, name="fact_content"
        ),
        "truth_sha256": _sha(truth_sha256, name="truth"),
        "winning_bucket": _required_text(
            winning_bucket, name="winning_bucket"
        ),
        "policy_version_vector_sha256": _sha(
            policy_version_vector_sha256, name="policy_vector"
        ),
        "coverage_usdc": _number(coverage_usdc, name="coverage"),
        "overlap_usdc": _number(overlap_usdc, name="overlap"),
        "edge_usdc": _number(edge_usdc, name="edge"),
        "execution_cost_usdc": _number(
            execution_cost_usdc, name="execution_cost", nonnegative=True
        ),
        "failure_cost_usdc": _number(
            failure_cost_usdc, name="failure_cost", nonnegative=True
        ),
    }
    counterfactual = (
        core["coverage_usdc"]
        + core["overlap_usdc"]
        + core["edge_usdc"]
        - core["execution_cost_usdc"]
        - core["failure_cost_usdc"]
    )
    core["counterfactual_pnl_usdc"] = counterfactual
    core["closure_residual_usdc"] = 0.0
    return {
        **core,
        "content_sha256": content_sha256(core),
    }


def validate_attribution_terms(
    terms: Mapping[str, Any],
    *,
    expected_fact_id: str | None = None,
    expected_fact_content_sha256: str | None = None,
    expected_truth_sha256: str | None = None,
    expected_policy_version_vector_sha256: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate identity, arithmetic closure, and optional lineage pins."""

    if not isinstance(terms, Mapping) or terms.get("schema") != TERMS_SCHEMA:
        return None, "settled_attribution_schema_invalid"
    stored = terms.get("content_sha256")
    if not isinstance(stored, str) or content_sha256(
        {key: value for key, value in terms.items() if key != "content_sha256"}
    ) != stored:
        return None, "settled_attribution_content_drift"
    try:
        values = {
            key: _number(terms.get(key), name=key)
            for key in (
                "coverage_usdc",
                "overlap_usdc",
                "edge_usdc",
                "execution_cost_usdc",
                "failure_cost_usdc",
                "counterfactual_pnl_usdc",
                "closure_residual_usdc",
            )
        }
    except ContractError:
        return None, "settled_attribution_terms_invalid"
    if values["execution_cost_usdc"] < 0 or values["failure_cost_usdc"] < 0:
        return None, "settled_attribution_terms_invalid"
    expected = (
        values["coverage_usdc"]
        + values["overlap_usdc"]
        + values["edge_usdc"]
        - values["execution_cost_usdc"]
        - values["failure_cost_usdc"]
    )
    if abs(expected - values["counterfactual_pnl_usdc"]) > EPS:
        return None, "settled_attribution_closure_failed"
    if abs(values["closure_residual_usdc"]) > EPS:
        return None, "settled_attribution_closure_residual_nonzero"
    expected_values = {
        "fact_id": expected_fact_id,
        "fact_content_sha256": expected_fact_content_sha256,
        "truth_sha256": expected_truth_sha256,
        "policy_version_vector_sha256": expected_policy_version_vector_sha256,
    }
    for key, expected_value in expected_values.items():
        if expected_value is not None and terms.get(key) != expected_value:
            return None, "settled_attribution_lineage_drift"
    return dict(terms), None


def build_settlement_version_evidence_packet(
    *,
    settlement_version_binding: Mapping[str, Any],
    attribution_terms: Mapping[str, Any],
    actual_fill_evidence: Mapping[str, Any] | None = None,
    production_no_live_blocked_by_forward_oos: bool = False,
) -> dict[str, Any]:
    """Materialize one immutable Settlement-Version Evidence Packet v1."""

    binding = dict(settlement_version_binding)
    if binding.get("schema") != SETTLEMENT_VERSION_BINDING_SCHEMA:
        raise ContractError("settlement_evidence_binding_schema_invalid")
    binding_sha = binding.get("content_sha256")
    if not isinstance(binding_sha, str) or content_sha256(
        {key: value for key, value in binding.items() if key != "content_sha256"}
    ) != binding_sha:
        raise ContractError("settlement_evidence_binding_content_drift")
    terms, terms_error = validate_attribution_terms(
        attribution_terms,
        expected_fact_id=binding.get("fact_id"),
        expected_fact_content_sha256=binding.get("fact_content_sha256"),
        expected_truth_sha256=binding.get("truth_sha256"),
        expected_policy_version_vector_sha256=binding.get(
            "policy_version_vector_sha256"
        ),
    )
    if terms_error or terms is None:
        raise ContractError(terms_error or "settlement_evidence_terms_invalid")
    if actual_fill_evidence is None:
        fill_evidence: dict[str, Any] = {
            "status": NA_ACTUAL_FILL_EVIDENCE,
            "source": "production_no_live",
        }
    elif isinstance(actual_fill_evidence, Mapping):
        fill_evidence = dict(actual_fill_evidence)
        if not str(fill_evidence.get("status") or "").strip():
            raise ContractError("settlement_evidence_actual_fill_status_missing")
    else:
        raise ContractError("settlement_evidence_actual_fill_mapping_required")
    if fill_evidence.get("status") != NA_ACTUAL_FILL_EVIDENCE:
        raise ContractError("settlement_evidence_actual_fill_not_available")
    core: dict[str, Any] = {
        "schema": SCHEMA,
        "packet_version": 1,
        "settlement_version_binding": binding,
        "settlement_version_binding_sha256": binding_sha,
        "evidence_refs": {
            "truth": {
                "truth_id": binding["truth_id"],
                "truth_revision_no": binding["truth_revision_no"],
                "truth_sha256": binding["truth_sha256"],
            },
            "probability": {
                "probability_envelope_id": binding["probability_envelope_id"],
                "posterior_sha256": binding["posterior_sha256"],
            },
            "official_ws": {
                "ws_snapshot_id": binding["ws_snapshot_id"],
                "quote_sha256": binding["quote_sha256"],
            },
            "inventory": {
                "inventory_snapshot_id": binding["inventory_snapshot_id"],
                "inventory_sha256": binding["inventory_sha256"],
            },
            "order_state": {
                "order_state_snapshot_id": binding["order_state_snapshot_id"],
                "order_state_sha256": binding["order_state_sha256"],
            },
            "decision": {
                "cycle_id": binding["cycle_id"],
                "decision_id": binding["decision_id"],
                "decision_sha256": binding["decision_sha256"],
                "plan_sha256": binding["plan_sha256"],
            },
            "policy": {
                "policy_version_vector_sha256": binding[
                    "policy_version_vector_sha256"
                ],
            },
        },
        "attribution_terms": terms,
        "actual_fill_evidence": fill_evidence,
        "production_boundary": {
            "production_no_live": True,
            "live_submit_authorized": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "production_no_live_blocked_by_forward_oos": (
                bool(production_no_live_blocked_by_forward_oos)
            ),
        },
        "promotion_boundary": {
            "forward_oos_blocks_formal_promotion": True,
            "online_parameter_tuning_allowed": False,
            "live_promotion_allowed": False,
        },
    }
    if core["production_boundary"]["production_no_live_blocked_by_forward_oos"]:
        raise ContractError("settlement_evidence_no_live_boundary_invalid")
    return {**core, "content_sha256": content_sha256(core)}


def validate_settlement_version_evidence_packet(
    packet: Mapping[str, Any],
    *,
    expected_binding_sha256: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Verify packet hash, binding hash, terms closure, and no-live boundary."""

    if not isinstance(packet, Mapping) or packet.get("schema") != SCHEMA:
        return None, "settlement_evidence_packet_schema_invalid"
    stored = packet.get("content_sha256")
    if not isinstance(stored, str) or content_sha256(
        {key: value for key, value in packet.items() if key != "content_sha256"}
    ) != stored:
        return None, "settlement_evidence_packet_content_drift"
    binding_sha = packet.get("settlement_version_binding_sha256")
    if expected_binding_sha256 is not None and binding_sha != expected_binding_sha256:
        return None, "settlement_evidence_packet_binding_drift"
    binding = packet.get("settlement_version_binding")
    if not isinstance(binding, Mapping) or binding.get("content_sha256") != binding_sha:
        return None, "settlement_evidence_packet_binding_missing"
    if binding.get("schema") != SETTLEMENT_VERSION_BINDING_SCHEMA:
        return None, "settlement_evidence_packet_binding_schema_invalid"
    if content_sha256(
        {key: value for key, value in binding.items() if key != "content_sha256"}
    ) != binding_sha:
        return None, "settlement_evidence_packet_binding_content_drift"
    boundary = packet.get("production_boundary")
    if not isinstance(boundary, Mapping) or any(
        boundary.get(key) != value
        for key, value in (
            ("production_no_live", True),
            ("live_submit_authorized", False),
            ("calls_order_gateway", False),
            ("orders_submitted", 0),
            ("production_no_live_blocked_by_forward_oos", False),
        )
    ):
        return None, "settlement_evidence_no_live_boundary_invalid"
    fill_evidence = packet.get("actual_fill_evidence")
    if not isinstance(fill_evidence, Mapping):
        return None, "settlement_evidence_actual_fill_evidence_missing"
    if boundary.get("production_no_live") is True:
        if fill_evidence.get("status") != NA_ACTUAL_FILL_EVIDENCE:
            return None, "settlement_evidence_actual_fill_status_invalid"
        if any(
            key in fill_evidence
            for key in ("fill_count", "filled_shares", "fills", "realized_pnl_usdc")
        ):
            return None, "settlement_evidence_actual_fill_fabricated"
    terms = packet.get("attribution_terms")
    if not isinstance(terms, Mapping):
        return None, "settlement_evidence_packet_terms_missing"
    valid_terms, error = validate_attribution_terms(terms)
    if error or valid_terms is None:
        return None, error or "settlement_evidence_packet_terms_invalid"
    if terms.get("fact_id") != binding.get("fact_id") or terms.get(
        "fact_content_sha256"
    ) != binding.get("fact_content_sha256") or terms.get("truth_sha256") != binding.get(
        "truth_sha256"
    ):
        return None, "settlement_evidence_packet_terms_lineage_drift"
    return dict(packet), None


# Short aliases make the contract discoverable without introducing multiple
# schemas or builders.
build_settlement_evidence_packet = build_settlement_version_evidence_packet
validate_settlement_evidence_packet = validate_settlement_version_evidence_packet


__all__ = [
    "NA_ACTUAL_FILL_EVIDENCE",
    "REQUESTED_INTENT_SEMANTICS",
    "SCHEMA",
    "SETTLEMENT_VERSION_BINDING_SCHEMA",
    "TERMS_SCHEMA",
    "build_attribution_terms",
    "build_settlement_evidence_packet",
    "build_settlement_version_evidence_packet",
    "validate_attribution_terms",
    "validate_settlement_evidence_packet",
    "validate_settlement_version_evidence_packet",
]
