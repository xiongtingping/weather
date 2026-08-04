"""Versioned station-policy post-label utility contract boundary.

Station policy follows ``ProbabilityEnvelope``; it cannot issue a second
weather probability.  Nor may a decision-time fact contain a realised policy
score.  This module therefore contains two deliberately separate objects:

* a content-addressed *contract artifact* which describes the immutable
  inputs and symbolic formulae that a future scorer must bind; and
* the existing future-outcome materialisation skeleton, which points at that
  contract but remains ``UNAVAILABLE`` until real candidate/baseline,
  official-WS, fill, and settlement lineage exists.

The contract is descriptive only.  It never evaluates a utility, accepts a
caller-provided score, changes promotion authority, calls an order gateway,
or treats a candidate intent/simulated receipt as a direct fill.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .settlement_time import settlement_day_window


MATERIALIZATION_SCHEMA = "station_policy_forward_outcome_materialization_v2"
POLICY_FAMILIES = ("target", "reserve", "cadence")
HISTORICAL_STRICT_OOS = "historical_strict_oos"
OBSERVATIONAL_CANDIDATE_NOT_OOS = "observational_candidate_not_oos"
UNAVAILABLE_REASONS = {
    "target": "station_policy_target_inventory_ws_cost_settlement_utility_binding_unavailable",
    "reserve": "station_policy_reserve_fill_utility_binding_unavailable",
    "cadence": "station_policy_cadence_timing_opportunity_cost_binding_unavailable",
}

# The contract itself is an evidence-schema artifact, not a promotion or
# scoring artifact.  Keep the version explicit so a later scorer cannot
# silently reinterpret historical facts after a formula/field change.
UTILITY_CONTRACT_SCHEMA = "station_policy_utility_contract_v2"
UTILITY_CONTRACT_POINTER_SCHEMA = "station_policy_utility_contract_pointer_v2"
UTILITY_CONTRACT_VERSION = "2.0.0"
UTILITY_CONTRACT_ARTIFACT_ID = "station_policy_utility_contract_v2"
METRIC_CONTRACT_STATE = "DEFINED_UNBOUND"

_SCOPE_FIELDS = (
    "station_id",
    "event_date",
    "settlement_timezone",
    "stage",
    "decision_at_utc",
    "strategy_version",
    "season_bin",
    "maturity_bin",
    "liquidity_bin",
)

# Derived values are intentionally absent from the binding schema.  A caller
# may bind raw evidence needed to calculate them later, but must not smuggle a
# realised score into a decision-time fact or a no-live receipt.
FORBIDDEN_DERIVED_FIELDS = frozenset(
    {
        "score",
        "candidate_score",
        "baseline_score",
        "delta_score",
        "utility_score",
        "settlement_utility_usdc",
        "opportunity_cost_usdc",
        "metric_value",
    }
)


def _contract_family(
    *,
    family: str,
    extra_execution_fields: Sequence[str] = (),
    extra_timing_fields: Sequence[str] = (),
    formulas: Mapping[str, str],
    purpose: str,
) -> dict[str, Any]:
    """Return one JSON-safe family contract declaration.

    Names in ``required_bindings`` are field names, not values.  Keeping them
    in the immutable contract makes missing candidate-vs-baseline or
    settlement evidence machine-auditable without fabricating that evidence.
    """

    return {
        "family": family,
        "purpose": purpose,
        # Formula and raw-evidence shapes are now fixed and auditable.  This
        # does *not* mean a score exists: each fact remains UNBOUND until the
        # exact candidate/baseline, official WS, confirmed receipt and closed
        # settlement objects are joined by an independent post-label scorer.
        "metric_state": METRIC_CONTRACT_STATE,
        "binding_state": "UNBOUND",
        "direct_fill_binding": "UNBOUND",
        "required_bindings": {
            "scope": list(_SCOPE_FIELDS),
            "candidate_vs_baseline": [
                "candidate_decision_fact_sha256",
                "baseline_decision_fact_sha256",
                "candidate_plan_sha256",
                "baseline_plan_sha256",
                "candidate_inventory_vector_sha256",
                "baseline_inventory_vector_sha256",
                "candidate_support_bucket_ids",
                "baseline_support_bucket_ids",
            ],
            # Keep the two inventory sides explicit as well as paired.  This
            # prevents a scorer from silently dropping the baseline or
            # comparing vectors with different bucket contracts.
            "candidate_inventory": [
                "plan_sha256",
                "inventory_vector_sha256",
                "support_bucket_ids",
                "target_quantity_by_bucket",
            ],
            "baseline_inventory": [
                "plan_sha256",
                "inventory_vector_sha256",
                "support_bucket_ids",
                "target_quantity_by_bucket",
            ],
            "official_clob_ws": [
                "candidate_ws_snapshot_sha256",
                "baseline_ws_snapshot_sha256",
                "candidate_quote_sequence",
                "baseline_quote_sequence",
                "candidate_all_in_yes_ask_by_bucket",
                "baseline_all_in_yes_ask_by_bucket",
                "quote_source",
                "quote_observed_at_utc",
                "quote_expires_at_utc",
            ],
            "settlement": [
                "settlement_truth_sha256",
                "settlement_outcome_sha256",
                "winner_bucket",
                "settlement_closed_at_utc",
            ],
            "execution": [
                "candidate_fill_receipt_bindings",
                "baseline_fill_receipt_bindings",
                "candidate_fill_receipt_set_sha256",
                "baseline_fill_receipt_set_sha256",
                "candidate_confirmed_filled_qty_by_bucket",
                "baseline_confirmed_filled_qty_by_bucket",
                "candidate_confirmed_filled_notional_usdc_by_bucket",
                "baseline_confirmed_filled_notional_usdc_by_bucket",
                "candidate_fill_outcome_by_bucket",
                "baseline_fill_outcome_by_bucket",
                *extra_execution_fields,
            ],
            "timing": [*extra_timing_fields],
        },
        "formula_language": "symbolic_no_evaluation",
        "formulas": dict(formulas),
        "metric_definition": {
            "evaluation_phase": "POST_SETTLEMENT_ONLY",
            "unit": "USDC delta versus same-scope baseline",
            "candidate_baseline_pairing": "one_to_one_exact_scope",
            "execution_cost_basis": (
                "confirmed_filled_notional_from_direct_receipt_bindings"
            ),
            "zero_fill_rule": (
                "only explicit authoritative zero-fill receipt; absence of a receipt is never zero fill"
            ),
            "partial_fill_rule": (
                "use confirmed filled quantity and notional by bucket; never promote requested quantity to filled quantity"
            ),
            "ws_cost_role": (
                "official CLOB WS all-in YES asks are same-decision cost marks, not a fill or utility substitute"
            ),
            "settlement_rule": (
                "closed official winner bucket maps only direct confirmed YES fills to payout"
            ),
            "score_row_emission": (
                "forbidden unless all required raw bindings validate and an independent scorer version is explicitly activated"
            ),
        },
        "units": {
            "quantity": "YES shares",
            "price": "USDC per YES share",
            "cost": "USDC",
            "time": "UTC seconds",
        },
        "hard_requirements": [
            "candidate_and_baseline_same_exact_decision_scope",
            "candidate_and_baseline_immutable_content_addressed",
            "official_clob_ws_only_and_quote_fresh_at_decision",
            "settlement_truth_closed_and_content_addressed",
            "confirmed_fill_receipt_direct_binding_required_before_utility",
            "no_score_or_proxy_outcome_in_decision_fact",
        ],
        "promotion_permitted": False,
    }


def build_station_policy_utility_contract() -> dict[str, Any]:
    """Build the immutable, non-scoring utility contract artifact.

    The symbolic formulae are copied from the station-policy doctrine.  They
    are intentionally strings: this function never computes a utility from
    prices, weather probabilities, intents, or simulated receipts.
    """

    identity = {
        "schema": UTILITY_CONTRACT_SCHEMA,
        "artifact_id": UTILITY_CONTRACT_ARTIFACT_ID,
        "contract_version": UTILITY_CONTRACT_VERSION,
        "authority": "EVIDENCE_SCHEMA_ONLY",
        "families": {
            "target": _contract_family(
                family="target",
                formulas={
                    "ws_mark_cost_usdc": (
                        "sum_b(quantity_b * all_in_yes_ask_b) + fees_usdc"
                    ),
                    "confirmed_fill_cost_usdc": (
                        "sum_b(confirmed_filled_notional_usdc_b)"
                    ),
                    "settlement_payout_usdc": (
                        "confirmed_filled_quantity_winner_bucket"
                        " if winner_bucket in support else 0"
                    ),
                    "settlement_net_usdc": (
                        "settlement_payout_usdc - confirmed_fill_cost_usdc"
                    ),
                    "settlement_utility_delta_usdc": (
                        "candidate_settlement_net_usdc"
                        " - baseline_settlement_net_usdc"
                    ),
                },
                purpose=(
                    "Compare one frozen candidate inventory vector with its "
                    "same-scope frozen baseline using official WS all-in cost "
                    "and closed settlement truth."
                ),
            ),
            "reserve": _contract_family(
                family="reserve",
                extra_execution_fields=(
                    "queue_entered_at_utc",
                    "queue_exit_at_utc",
                    "cancel_requested_at_utc",
                    "cancel_confirmed_at_utc",
                    "cancel_receipt_sha256",
                    "adverse_selection_cost_usdc",
                    "capital_lock_cost_usdc",
                    "frozen_queue_cost_rate_usdc_per_second",
                    "cancel_fee_usdc",
                    "reprice_cost_usdc",
                ),
                formulas={
                    "confirmed_fill_utility_delta_usdc": (
                        "settlement_utility_delta_usdc"
                        " - queue_cost_usdc"
                        " - cancel_cost_usdc"
                        " - adverse_selection_cost_usdc"
                        " - capital_lock_cost_usdc"
                    ),
                    "queue_cost_usdc": (
                        "queue_wait_seconds * frozen_queue_cost_rate_usdc_per_second"
                    ),
                    "cancel_cost_usdc": (
                        "cancel_fee_usdc + reprice_cost_usdc"
                    ),
                },
                purpose=(
                    "Attribute confirmed Maker fill, queue wait, and cancel "
                    "outcomes against the same-scope baseline."
                ),
            ),
            "cadence": _contract_family(
                family="cadence",
                extra_execution_fields=(
                    "wait_started_at_utc",
                    "wait_ended_at_utc",
                    "wait_or_act_arm",
                    "capital_lock_cost_usdc",
                    "adverse_selection_cost_usdc",
                    "counterfactual_act_receipt_bindings",
                    "counterfactual_wait_receipt_bindings",
                ),
                extra_timing_fields=(
                    "candidate_decision_at_utc",
                    "baseline_decision_at_utc",
                    "next_checkpoint_at_utc",
                    "event_deadline_at_utc",
                ),
                formulas={
                    "cadence_utility_delta_usdc": (
                        "settlement_utility_delta_usdc"
                        " - wait_opportunity_cost_usdc"
                        " - adverse_selection_cost_usdc"
                        " - capital_lock_cost_usdc"
                    ),
                    "wait_opportunity_cost_usdc": (
                        "counterfactual_act_utility_usdc"
                        " - counterfactual_wait_utility_usdc"
                    ),
                },
                purpose=(
                    "Compare act-now versus wait/queue timing under identical "
                    "scope, with explicit opportunity-cost and deadline facts."
                ),
            ),
        },
        "global_requirements": {
            "scope_key": "station_id|event_date|stage|decision_at_utc|strategy_version",
            "candidate_baseline_pairing": "one_to_one_same_scope",
            "quote_source": "OFFICIAL_CLOB_WS",
            "settlement_authority": "OFFICIAL_CLOSED_SETTLEMENT_TRUTH",
            "direct_fill_binding": "UNBOUND",
            "metric_contract_state": METRIC_CONTRACT_STATE,
            "score_emission": "FORBIDDEN",
            "proxy_weather_or_quote_scoring": "FORBIDDEN",
        },
        "production_no_live": True,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "promotion_permitted": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def assert_station_policy_utility_contract(
    contract: Mapping[str, Any],
) -> None:
    """Validate the contract's identity, family formulae and no-live boundary."""

    if not isinstance(contract, Mapping):
        raise ContractError("station_policy_utility_contract_mapping_required")
    identity = {key: value for key, value in contract.items() if key != "content_sha256"}
    if contract.get("schema") != UTILITY_CONTRACT_SCHEMA:
        raise ContractError("station_policy_utility_contract_schema_invalid")
    if contract.get("artifact_id") != UTILITY_CONTRACT_ARTIFACT_ID:
        raise ContractError("station_policy_utility_contract_artifact_id_invalid")
    if contract.get("contract_version") != UTILITY_CONTRACT_VERSION:
        raise ContractError("station_policy_utility_contract_version_invalid")
    if contract.get("content_sha256") != content_sha256(identity):
        raise ContractError("station_policy_utility_contract_hash_invalid")
    families = contract.get("families")
    if not isinstance(families, Mapping) or set(families) != set(POLICY_FAMILIES):
        raise ContractError("station_policy_utility_contract_families_invalid")
    for family in POLICY_FAMILIES:
        declaration = families.get(family)
        if not isinstance(declaration, Mapping):
            raise ContractError("station_policy_utility_contract_family_invalid")
        if declaration.get("family") != family:
            raise ContractError("station_policy_utility_contract_family_identity_invalid")
        if declaration.get("metric_state") != METRIC_CONTRACT_STATE:
            raise ContractError("station_policy_utility_contract_metric_state_invalid")
        if declaration.get("binding_state") != "UNBOUND":
            raise ContractError("station_policy_utility_contract_binding_state_invalid")
        if declaration.get("direct_fill_binding") != "UNBOUND":
            raise ContractError("station_policy_utility_contract_direct_fill_binding_invalid")
        required = declaration.get("required_bindings")
        if not isinstance(required, Mapping) or not required.get("scope"):
            raise ContractError("station_policy_utility_contract_required_bindings_invalid")
        formulas = declaration.get("formulas")
        if not isinstance(formulas, Mapping) or not formulas:
            raise ContractError("station_policy_utility_contract_formulas_invalid")
    boundary = contract.get("global_requirements")
    if (
        not isinstance(boundary, Mapping)
        or boundary.get("direct_fill_binding") != "UNBOUND"
        or boundary.get("metric_contract_state") != METRIC_CONTRACT_STATE
    ):
        raise ContractError("station_policy_utility_contract_global_boundary_invalid")
    if (
        contract.get("production_no_live") is not True
        or contract.get("writes_production_db") is not False
        or contract.get("calls_order_gateway") is not False
        or contract.get("orders_submitted") != 0
        or contract.get("promotion_permitted") is not False
    ):
        raise ContractError("station_policy_utility_contract_authority_invalid")


def build_station_policy_utility_contract_pointer(
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a small immutable pointer suitable for scorecards/ledgers."""

    artifact = build_station_policy_utility_contract() if contract is None else contract
    assert_station_policy_utility_contract(artifact)
    identity = {
        "schema": UTILITY_CONTRACT_POINTER_SCHEMA,
        "artifact_schema": UTILITY_CONTRACT_SCHEMA,
        "artifact_id": str(artifact["artifact_id"]),
        "artifact_version": str(artifact["contract_version"]),
        "artifact_content_sha256": str(artifact["content_sha256"]),
        "state": "DEFINED_UNBOUND",
        "direct_fill_binding": "UNBOUND",
        "metric_state": METRIC_CONTRACT_STATE,
        "promotion_permitted": False,
    }
    return {**identity, "pointer_sha256": content_sha256(identity)}


def assert_station_policy_utility_contract_pointer(
    pointer: Mapping[str, Any],
) -> None:
    if not isinstance(pointer, Mapping):
        raise ContractError("station_policy_utility_contract_pointer_mapping_required")
    identity = {key: value for key, value in pointer.items() if key != "pointer_sha256"}
    if pointer.get("schema") != UTILITY_CONTRACT_POINTER_SCHEMA:
        raise ContractError("station_policy_utility_contract_pointer_schema_invalid")
    if pointer.get("artifact_schema") != UTILITY_CONTRACT_SCHEMA:
        raise ContractError("station_policy_utility_contract_pointer_artifact_schema_invalid")
    if pointer.get("artifact_id") != UTILITY_CONTRACT_ARTIFACT_ID:
        raise ContractError("station_policy_utility_contract_pointer_artifact_id_invalid")
    if pointer.get("artifact_version") != UTILITY_CONTRACT_VERSION:
        raise ContractError("station_policy_utility_contract_pointer_version_invalid")
    artifact_hash = str(pointer.get("artifact_content_sha256") or "").lower()
    if len(artifact_hash) != 64 or any(char not in "0123456789abcdef" for char in artifact_hash):
        raise ContractError("station_policy_utility_contract_pointer_artifact_hash_invalid")
    if pointer.get("state") != "DEFINED_UNBOUND" or pointer.get("direct_fill_binding") != "UNBOUND":
        raise ContractError("station_policy_utility_contract_pointer_state_invalid")
    if (
        pointer.get("metric_state") != METRIC_CONTRACT_STATE
        or pointer.get("promotion_permitted") is not False
    ):
        raise ContractError("station_policy_utility_contract_pointer_authority_invalid")
    if pointer.get("pointer_sha256") != content_sha256(identity):
        raise ContractError("station_policy_utility_contract_pointer_hash_invalid")


def _sha256(value: Any) -> bool:
    text = str(value or "").lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _nonnegative_mapping(value: Any) -> bool:
    if not isinstance(value, Mapping) or not value:
        return False
    try:
        return all(
            str(bucket).strip()
            and float(quantity) >= 0.0
            for bucket, quantity in value.items()
        )
    except (TypeError, ValueError):
        return False


def _bucket_ids(value: Any) -> set[str] | None:
    if not isinstance(value, list) or not value:
        return None
    ids = {str(bucket).strip() for bucket in value}
    if not ids or "" in ids or len(ids) != len(value):
        return None
    return ids


def _nonnegative_number(value: Any) -> bool:
    try:
        return float(value) >= 0.0
    except (TypeError, ValueError):
        return False


def _validate_fill_side(
    execution: Mapping[str, Any],
    *,
    side: str,
    decision_at: datetime | None,
    settlement_closed_at: datetime | None,
    blockers: list[str],
) -> None:
    """Validate one direct fill/zero-fill receipt side without scoring it."""

    raw_receipts = execution.get(f"{side}_fill_receipt_bindings")
    receipts = raw_receipts if isinstance(raw_receipts, list) else []
    if not receipts:
        blockers.append(f"execution_{side}_fill_receipt_bindings_invalid")
        return
    receipt_hashes: list[str] = []
    receipt_ids: set[str] = set()
    receipt_content_hashes: set[str] = set()
    # One GTC order can yield multiple independent partial-fill receipts for
    # the same bucket.  Retain every receipt hash, then compare the aggregate
    # to the declared actual quantity/notional.  A requested size is never an
    # acceptable substitute for this aggregate.
    receipt_by_bucket: dict[str, tuple[float, float, bool]] = {}
    allowed_fill_types = {
        "FOK_FULL_FILL",
        "MAKER_FULL_FILL",
        "MAKER_PARTIAL_FILL",
    }
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            blockers.append(f"execution_{side}_fill_receipt_mapping_invalid")
            continue
        receipt_id = str(receipt.get("receipt_id") or "").strip()
        content_sha = str(receipt.get("receipt_content_sha256") or "")
        bucket = str(receipt.get("bucket") or "").strip()
        receipt_type = str(receipt.get("receipt_type") or "").upper()
        observed_at = _utc(receipt.get("observed_at_utc"))
        ws_sha = receipt.get("official_ws_snapshot_sha256")
        if (
            not receipt_id
            or not str(receipt.get("order_intent_id") or "").strip()
            or not bucket
            or not _sha256(content_sha)
            or not _sha256(ws_sha)
            or observed_at is None
        ):
            blockers.append(f"execution_{side}_fill_receipt_identity_invalid")
            continue
        if receipt_id in receipt_ids:
            blockers.append(f"execution_{side}_fill_receipt_id_duplicate")
        if content_sha in receipt_content_hashes:
            blockers.append(f"execution_{side}_fill_receipt_content_hash_duplicate")
        receipt_ids.add(receipt_id)
        receipt_content_hashes.add(content_sha)
        if decision_at is not None and observed_at < decision_at:
            blockers.append(f"execution_{side}_fill_receipt_before_decision")
        if settlement_closed_at is not None and observed_at > settlement_closed_at:
            blockers.append(f"execution_{side}_fill_receipt_after_settlement")
        if receipt.get("authoritative") is not True:
            blockers.append(f"execution_{side}_fill_receipt_not_authoritative")
        if receipt.get("simulated") is not False:
            blockers.append(f"execution_{side}_fill_receipt_simulated_or_unknown")
        try:
            quantity = float(receipt.get("matched_size"))
            notional = float(receipt.get("matched_notional_usdc"))
        except (TypeError, ValueError):
            blockers.append(f"execution_{side}_fill_receipt_amount_invalid")
            continue
        if quantity < 0.0 or notional < 0.0 or (quantity == 0.0) != (notional == 0.0):
            blockers.append(f"execution_{side}_fill_receipt_amount_inconsistent")
            continue
        is_zero_fill = receipt_type == "CANCEL_CONFIRMED"
        if quantity > 0.0 and receipt_type not in allowed_fill_types:
            blockers.append(f"execution_{side}_fill_receipt_type_not_confirmed_fill")
        if quantity == 0.0 and not is_zero_fill:
            blockers.append(f"execution_{side}_zero_fill_receipt_not_confirmed")
        previous_qty, previous_notional, previous_positive = receipt_by_bucket.get(
            bucket, (0.0, 0.0, False)
        )
        receipt_by_bucket[bucket] = (
            previous_qty + quantity,
            previous_notional + notional,
            previous_positive or quantity > 0.0,
        )
        receipt_hashes.append(content_sha)
    if not receipt_hashes:
        return
    if str(execution.get(f"{side}_fill_receipt_set_sha256") or "") != content_sha256(sorted(receipt_hashes)):
        blockers.append(f"execution_{side}_fill_receipt_set_hash_mismatch")
    quantities = execution.get(f"{side}_confirmed_filled_qty_by_bucket")
    notionals = execution.get(f"{side}_confirmed_filled_notional_usdc_by_bucket")
    outcomes = execution.get(f"{side}_fill_outcome_by_bucket")
    if not _nonnegative_mapping(quantities):
        blockers.append(f"execution_{side}_confirmed_quantity_mapping_invalid")
    if not _nonnegative_mapping(notionals):
        blockers.append(f"execution_{side}_confirmed_notional_mapping_invalid")
    if not isinstance(outcomes, Mapping) or set(map(str, outcomes)) != set(receipt_by_bucket):
        blockers.append(f"execution_{side}_fill_outcome_mapping_invalid")
        return
    if isinstance(quantities, Mapping) and isinstance(notionals, Mapping):
        if set(map(str, quantities)) != set(receipt_by_bucket) or set(map(str, notionals)) != set(receipt_by_bucket):
            blockers.append(f"execution_{side}_receipt_amount_bucket_mismatch")
        for bucket, (quantity, notional, has_positive_fill) in receipt_by_bucket.items():
            try:
                quantity_match = abs(float(quantities[bucket]) - quantity) <= 1e-9
                notional_match = abs(float(notionals[bucket]) - notional) <= 1e-9
            except (KeyError, TypeError, ValueError):
                quantity_match = notional_match = False
            expected_outcome = (
                "CONFIRMED_FILL" if has_positive_fill else "CONFIRMED_ZERO_FILL"
            )
            if not quantity_match or not notional_match:
                blockers.append(f"execution_{side}_receipt_amount_mapping_mismatch")
            if str(outcomes.get(bucket) or "") != expected_outcome:
                blockers.append(f"execution_{side}_fill_outcome_not_receipt_backed")


def _validate_complete_raw_binding(
    binding: Mapping[str, Any],
    *,
    family: str,
    blockers: list[str],
) -> None:
    """Audit concrete cross-side raw evidence; never calculate utility."""

    scope = binding.get("scope")
    decision = _utc(scope.get("decision_at_utc")) if isinstance(scope, Mapping) else None
    if decision is None:
        blockers.append("scope_decision_at_utc_invalid")
    candidate_baseline = binding.get("candidate_vs_baseline")
    candidate_inventory = binding.get("candidate_inventory")
    baseline_inventory = binding.get("baseline_inventory")
    if isinstance(candidate_baseline, Mapping):
        for field in (
            "candidate_decision_fact_sha256",
            "baseline_decision_fact_sha256",
            "candidate_plan_sha256",
            "baseline_plan_sha256",
            "candidate_inventory_vector_sha256",
            "baseline_inventory_vector_sha256",
        ):
            if not _sha256(candidate_baseline.get(field)):
                blockers.append(f"candidate_vs_baseline_{field}_invalid")
        for side, inventory in (
            ("candidate", candidate_inventory),
            ("baseline", baseline_inventory),
        ):
            if not isinstance(inventory, Mapping):
                continue
            if str(candidate_baseline.get(f"{side}_plan_sha256") or "") != str(inventory.get("plan_sha256") or ""):
                blockers.append(f"candidate_vs_baseline_{side}_plan_hash_mismatch")
            if str(candidate_baseline.get(f"{side}_inventory_vector_sha256") or "") != str(inventory.get("inventory_vector_sha256") or ""):
                blockers.append(f"candidate_vs_baseline_{side}_inventory_hash_mismatch")
            quantities = inventory.get("target_quantity_by_bucket")
            support = _bucket_ids(inventory.get("support_bucket_ids"))
            if (
                not _sha256(inventory.get("plan_sha256"))
                or not _sha256(inventory.get("inventory_vector_sha256"))
                or not _nonnegative_mapping(quantities)
                or support is None
            ):
                blockers.append(f"{side}_inventory_shape_invalid")
            elif set(map(str, quantities)) != support:
                blockers.append(f"{side}_inventory_support_bucket_mismatch")
        candidate_support = _bucket_ids(
            candidate_baseline.get("candidate_support_bucket_ids")
        )
        baseline_support = _bucket_ids(
            candidate_baseline.get("baseline_support_bucket_ids")
        )
        if candidate_support is None or baseline_support is None:
            blockers.append("candidate_vs_baseline_support_bucket_ids_invalid")
        else:
            for side, inventory, paired_support in (
                ("candidate", candidate_inventory, candidate_support),
                ("baseline", baseline_inventory, baseline_support),
            ):
                if (
                    isinstance(inventory, Mapping)
                    and _bucket_ids(inventory.get("support_bucket_ids"))
                    != paired_support
                ):
                    blockers.append(
                        f"candidate_vs_baseline_{side}_support_bucket_mismatch"
                    )
    official_ws = binding.get("official_clob_ws")
    if isinstance(official_ws, Mapping):
        for side in ("candidate", "baseline"):
            if not _sha256(official_ws.get(f"{side}_ws_snapshot_sha256")):
                blockers.append(f"official_clob_ws_{side}_snapshot_hash_invalid")
            try:
                if int(official_ws.get(f"{side}_quote_sequence")) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                blockers.append(f"official_clob_ws_{side}_quote_sequence_invalid")
            prices = official_ws.get(f"{side}_all_in_yes_ask_by_bucket")
            if not _nonnegative_mapping(prices):
                blockers.append(f"official_clob_ws_{side}_ask_mapping_invalid")
            else:
                try:
                    if any(float(price) >= 1.0 for price in prices.values()):
                        blockers.append(f"official_clob_ws_{side}_ask_not_postable")
                except (TypeError, ValueError):
                    blockers.append(f"official_clob_ws_{side}_ask_mapping_invalid")
        observed = _utc(official_ws.get("quote_observed_at_utc"))
        expires = _utc(official_ws.get("quote_expires_at_utc"))
        if observed is None or expires is None or observed > expires:
            blockers.append("official_clob_ws_quote_clock_invalid")
        elif decision is not None and (observed > decision or expires < decision):
            blockers.append("official_clob_ws_quote_not_fresh_at_decision")
    settlement = binding.get("settlement")
    settlement_closed_at = (
        _utc(settlement.get("settlement_closed_at_utc"))
        if isinstance(settlement, Mapping)
        else None
    )
    execution = binding.get("execution")
    if isinstance(execution, Mapping):
        for side in ("candidate", "baseline"):
            _validate_fill_side(
                execution,
                side=side,
                decision_at=decision,
                settlement_closed_at=settlement_closed_at,
                blockers=blockers,
            )
            receipts = execution.get(f"{side}_fill_receipt_bindings")
            expected_ws_sha = (
                official_ws.get(f"{side}_ws_snapshot_sha256")
                if isinstance(official_ws, Mapping)
                else None
            )
            if isinstance(receipts, list) and _sha256(expected_ws_sha):
                for receipt in receipts:
                    if (
                        isinstance(receipt, Mapping)
                        and receipt.get("official_ws_snapshot_sha256") != expected_ws_sha
                    ):
                        blockers.append(
                            f"execution_{side}_receipt_ws_snapshot_mismatch"
                        )
            inventory = (
                candidate_inventory if side == "candidate" else baseline_inventory
            )
            quantities = (
                inventory.get("target_quantity_by_bucket")
                if isinstance(inventory, Mapping)
                else None
            )
            actual = execution.get(f"{side}_confirmed_filled_qty_by_bucket")
            if isinstance(quantities, Mapping) and isinstance(actual, Mapping):
                for bucket, filled in actual.items():
                    try:
                        if float(filled) > float(quantities.get(bucket, -1.0)):
                            blockers.append(
                                f"execution_{side}_filled_quantity_exceeds_inventory"
                            )
                    except (TypeError, ValueError):
                        blockers.append(
                            f"execution_{side}_filled_quantity_inventory_comparison_invalid"
                        )
    if isinstance(settlement, Mapping):
        if settlement.get("settlement_authority") != "OFFICIAL_CLOSED_SETTLEMENT_TRUTH":
            blockers.append("settlement_authority_invalid")
        if not _sha256(settlement.get("settlement_truth_sha256")) or not _sha256(settlement.get("settlement_outcome_sha256")):
            blockers.append("settlement_hash_lineage_invalid")
        settled = settlement_closed_at
        if settled is None or (decision is not None and settled <= decision):
            blockers.append("settlement_close_clock_invalid")
    if family == "reserve" and isinstance(execution, Mapping):
        timestamps: dict[str, datetime] = {}
        for field in ("queue_entered_at_utc", "queue_exit_at_utc", "cancel_requested_at_utc", "cancel_confirmed_at_utc"):
            parsed = _utc(execution.get(field))
            if parsed is None:
                blockers.append(f"execution_{field}_invalid")
            else:
                timestamps[field] = parsed
        if not _sha256(execution.get("cancel_receipt_sha256")):
            blockers.append("execution_cancel_receipt_hash_invalid")
        for field in (
            "adverse_selection_cost_usdc",
            "capital_lock_cost_usdc",
            "frozen_queue_cost_rate_usdc_per_second",
            "cancel_fee_usdc",
            "reprice_cost_usdc",
        ):
            if not _nonnegative_number(execution.get(field)):
                blockers.append(f"execution_{field}_invalid")
        if (
            len(timestamps) == 4
            and not (
                timestamps["queue_entered_at_utc"]
                <= timestamps["queue_exit_at_utc"]
                <= timestamps["cancel_requested_at_utc"]
                <= timestamps["cancel_confirmed_at_utc"]
            )
        ):
            blockers.append("execution_reserve_queue_cancel_clock_order_invalid")
    if family == "cadence":
        timing = binding.get("timing")
        if isinstance(timing, Mapping):
            timestamps: dict[str, datetime] = {}
            for field in ("candidate_decision_at_utc", "baseline_decision_at_utc", "next_checkpoint_at_utc", "event_deadline_at_utc"):
                parsed = _utc(timing.get(field))
                if parsed is None:
                    blockers.append(f"timing_{field}_invalid")
                else:
                    timestamps[field] = parsed
            if (
                len(timestamps) == 4
                and not (
                    timestamps["candidate_decision_at_utc"]
                    <= timestamps["next_checkpoint_at_utc"]
                    <= timestamps["event_deadline_at_utc"]
                    and timestamps["baseline_decision_at_utc"]
                    <= timestamps["event_deadline_at_utc"]
                )
            ):
                blockers.append("timing_cadence_clock_order_invalid")


def validate_station_policy_utility_binding(
    binding: Mapping[str, Any],
    *,
    family: str,
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate raw evidence references without evaluating utility.

    A returned ``UNBOUND`` receipt is intentional.  Missing/invalid fields
    are reported as blockers; no utility number, fill probability, or score is
    produced.  This makes the function safe to call from a read-only audit.
    """

    if family not in POLICY_FAMILIES:
        raise ContractError("station_policy_utility_binding_family_invalid")
    artifact = build_station_policy_utility_contract() if contract is None else contract
    assert_station_policy_utility_contract(artifact)
    blockers: list[str] = []
    if not isinstance(binding, Mapping):
        raise ContractError("station_policy_utility_binding_mapping_required")
    pointer = binding.get("utility_contract_pointer")
    try:
        assert_station_policy_utility_contract_pointer(pointer)  # type: ignore[arg-type]
    except ContractError as exc:
        blockers.append(str(exc))
    if isinstance(pointer, Mapping) and str(pointer.get("artifact_content_sha256")) != str(artifact.get("content_sha256")):
        blockers.append("station_policy_utility_binding_contract_pointer_mismatch")
    if str(binding.get("family") or "") != family:
        blockers.append("station_policy_utility_binding_family_mismatch")
    required = artifact["families"][family]["required_bindings"]
    for group, fields in required.items():
        section = binding.get(group)
        if not isinstance(section, Mapping):
            blockers.append(f"{group}_binding_section_missing")
            continue
        for field in fields:
            value = section.get(field)
            if value in (None, "", [], {}):
                blockers.append(f"{group}_{field}_missing")
    scope = binding.get("scope")
    if isinstance(scope, Mapping):
        for field in _SCOPE_FIELDS:
            if not str(scope.get(field) or "").strip():
                blockers.append(f"scope_{field}_missing")
    candidate_baseline = binding.get("candidate_vs_baseline")
    if isinstance(candidate_baseline, Mapping):
        for field in (
            "candidate_plan_sha256",
            "baseline_plan_sha256",
            "candidate_inventory_vector_sha256",
            "baseline_inventory_vector_sha256",
        ):
            value = str(candidate_baseline.get(field) or "").lower()
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                blockers.append(f"candidate_vs_baseline_{field}_invalid")
    official_ws = binding.get("official_clob_ws")
    if isinstance(official_ws, Mapping):
        if str(official_ws.get("quote_source") or "") != "OFFICIAL_CLOB_WS":
            blockers.append("official_clob_ws_quote_source_invalid")
        # A future quote or an already expired quote cannot bind a decision.
        decision_at = str(scope.get("decision_at_utc") or "") if isinstance(scope, Mapping) else ""
        try:
            decision = datetime.fromisoformat(decision_at.replace("Z", "+00:00")).astimezone(timezone.utc)
            observed = datetime.fromisoformat(str(official_ws.get("quote_observed_at_utc") or "").replace("Z", "+00:00")).astimezone(timezone.utc)
            expires = datetime.fromisoformat(str(official_ws.get("quote_expires_at_utc") or "").replace("Z", "+00:00")).astimezone(timezone.utc)
            if observed > decision:
                blockers.append("official_clob_ws_quote_after_decision")
            if expires < decision:
                blockers.append("official_clob_ws_quote_expired_at_decision")
        except (TypeError, ValueError, OverflowError):
            blockers.append("official_clob_ws_quote_timestamp_invalid")
    _validate_complete_raw_binding(binding, family=family, blockers=blockers)
    def _forbidden_paths(value: Any, prefix: str = "") -> list[str]:
        paths: list[str] = []
        if isinstance(value, Mapping):
            for key, nested in value.items():
                name = str(key)
                path = f"{prefix}.{name}" if prefix else name
                if name in FORBIDDEN_DERIVED_FIELDS:
                    paths.append(path)
                paths.extend(_forbidden_paths(nested, path))
        elif isinstance(value, (list, tuple)):
            for index, nested in enumerate(value):
                paths.extend(_forbidden_paths(nested, f"{prefix}[{index}]"))
        return paths

    for path in _forbidden_paths(binding):
        blockers.append(f"derived_{path.replace('.', '_').replace('[', '_').replace(']', '')}_forbidden")
    complete = not blockers
    return {
        "schema": "station_policy_utility_binding_validation_v1",
        "family": family,
        "state": "BINDING_COMPLETE_NOT_SCORED" if complete else "UNBOUND",
        "binding_valid": complete,
        "blockers": sorted(set(blockers)),
        "utility_evaluated": False,
        "score_emitted": False,
        "direct_fill_binding": (
            "VALIDATED_RAW_EVIDENCE_NOT_SCORED" if complete else "UNBOUND"
        ),
        "promotion_permitted": False,
        "contract_pointer": build_station_policy_utility_contract_pointer(artifact),
    }


def validate_historical_station_policy_utility_binding(
    binding: Mapping[str, Any],
    *,
    family: str,
    fact: Mapping[str, Any],
    label: Mapping[str, Any],
    evaluation_as_of_utc: str,
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one immutable, closed-day utility binding without scoring it.

    ``validate_station_policy_utility_binding`` proves that the supplied raw
    candidate/baseline, official-WS, fill and settlement objects have the
    shape required by the metric declaration.  This historical wrapper binds
    that declaration to one append-only policy fact and one accepted AWC
    settlement label.  It deliberately still does *not* evaluate utility:
    the v2 contract is an evidence contract, and a valid raw binding is not a
    licence to turn requested quantity, a quote, or a no-live receipt into a
    realised score.

    The scorecard uses this boundary only for closed historical facts.  A
    future or observational record therefore remains visible as
    ``observational_candidate_not_oos`` but can never be upgraded to OOS by
    adding a label-shaped payload.
    """

    if family not in POLICY_FAMILIES:
        raise ContractError("station_policy_historical_utility_family_invalid")
    if not isinstance(fact, Mapping):
        raise ContractError("station_policy_historical_utility_fact_mapping_required")
    if not isinstance(label, Mapping):
        raise ContractError("station_policy_historical_utility_label_mapping_required")

    raw = validate_station_policy_utility_binding(
        binding,
        family=family,
        contract=contract,
    )
    blockers = list(raw["blockers"])
    if not isinstance(binding, Mapping):  # Defensive; generic validator has checked it.
        blockers.append("historical_binding_mapping_invalid")
        binding = {}

    if str(binding.get("evidence_role") or "").strip().lower() != HISTORICAL_STRICT_OOS:
        blockers.append("historical_binding_evidence_role_not_historical_strict_oos")

    scope = binding.get("scope")
    if not isinstance(scope, Mapping):
        blockers.append("historical_binding_scope_missing")
        scope = {}
    fact_scope = {
        "station_id": str(fact.get("station_id") or "").strip().upper(),
        "event_date": str(fact.get("event_date") or "").strip(),
        "settlement_timezone": str(fact.get("settlement_timezone") or "").strip(),
        "stage": str(fact.get("stage") or "").strip(),
        "decision_at_utc": str(fact.get("decision_at_utc") or "").strip(),
    }
    # The binding's declared role is not authoritative on its own: a caller
    # could otherwise relabel a closed production-no-live forward fact as
    # historical after settlement.  The immutable fact must carry the same
    # strict historical authority before this utility evidence can enter an
    # OOS denominator.
    if (
        str(fact.get("authority_scope") or "").strip().upper()
        != "HISTORICAL_STRICT_OOS"
    ):
        blockers.append(
            "historical_binding_fact_authority_scope_not_historical_strict_oos"
        )
    for field, expected in fact_scope.items():
        actual = str(scope.get(field) or "").strip()
        if field == "station_id":
            actual = actual.upper()
        if actual != expected:
            blockers.append(f"historical_binding_scope_{field}_mismatch")

    evaluation_as_of = _utc(evaluation_as_of_utc)
    decision_at = _utc(fact_scope["decision_at_utc"])
    settlement_closed_at = _utc(label.get("settlement_closed_at_utc"))
    labeled_at = _utc(label.get("labeled_at_utc"))
    if evaluation_as_of is None:
        blockers.append("historical_binding_evaluation_asof_invalid")
    if decision_at is None:
        blockers.append("historical_binding_fact_decision_at_invalid")
    if settlement_closed_at is None:
        blockers.append("historical_binding_settlement_closed_at_invalid")
    if labeled_at is None:
        blockers.append("historical_binding_labeled_at_invalid")
    if (
        evaluation_as_of is not None
        and settlement_closed_at is not None
        and settlement_closed_at > evaluation_as_of
    ):
        blockers.append("historical_binding_future_event_not_oos")
    if (
        evaluation_as_of is not None
        and labeled_at is not None
        and labeled_at > evaluation_as_of
    ):
        blockers.append("historical_binding_label_after_evaluation_asof")
    if (
        settlement_closed_at is not None
        and labeled_at is not None
        and labeled_at < settlement_closed_at
    ):
        blockers.append("historical_binding_label_before_settlement_close")
    if (
        decision_at is not None
        and settlement_closed_at is not None
        and settlement_closed_at <= decision_at
    ):
        blockers.append("historical_binding_settlement_before_decision")
    try:
        expected_settlement_closed_at = _utc(
            settlement_day_window(
                fact_scope["event_date"], fact_scope["settlement_timezone"]
            )["target_day_end_utc"]
        )
    except (ContractError, KeyError):
        expected_settlement_closed_at = None
        blockers.append("historical_binding_fact_settlement_scope_invalid")
    if (
        expected_settlement_closed_at is not None
        and settlement_closed_at is not None
        and expected_settlement_closed_at != settlement_closed_at
    ):
        blockers.append("historical_binding_settlement_close_scope_mismatch")

    if label.get("official_awc_truth") is not True:
        blockers.append("historical_binding_official_awc_truth_required")
    if str(label.get("truth_source_lane") or "").strip().upper() not in {
        "AWC",
        "OFFICIAL_AWC",
    }:
        blockers.append("historical_binding_truth_source_lane_not_awc")
    if str(label.get("truth_qc_status") or "").strip().upper() != "ACCEPTED":
        blockers.append("historical_binding_truth_qc_not_accepted")

    candidate_baseline = binding.get("candidate_vs_baseline")
    if isinstance(candidate_baseline, Mapping):
        fact_sha = str(fact.get("content_sha256") or "").lower()
        if not _sha256(fact_sha):
            blockers.append("historical_binding_fact_content_hash_invalid")
        elif fact_sha not in {
            str(candidate_baseline.get("candidate_decision_fact_sha256") or "").lower(),
            str(candidate_baseline.get("baseline_decision_fact_sha256") or "").lower(),
        }:
            blockers.append("historical_binding_fact_not_in_candidate_baseline_pair")
        plan_sha = str(fact.get("plan_sha256") or "").lower()
        if not _sha256(plan_sha):
            blockers.append("historical_binding_fact_plan_hash_invalid")
        elif plan_sha not in {
            str(candidate_baseline.get("candidate_plan_sha256") or "").lower(),
            str(candidate_baseline.get("baseline_plan_sha256") or "").lower(),
        }:
            blockers.append("historical_binding_fact_plan_not_in_candidate_baseline_pair")
        inventory_sha = str(fact.get("inventory_sha256") or "").lower()
        if not _sha256(inventory_sha):
            blockers.append("historical_binding_fact_inventory_hash_invalid")
        elif inventory_sha not in {
            str(candidate_baseline.get("candidate_inventory_vector_sha256") or "").lower(),
            str(candidate_baseline.get("baseline_inventory_vector_sha256") or "").lower(),
        }:
            blockers.append("historical_binding_fact_inventory_not_in_candidate_baseline_pair")

    official_ws = binding.get("official_clob_ws")
    if isinstance(official_ws, Mapping):
        snapshot_sha = str(fact.get("ws_snapshot_content_sha256") or "").lower()
        if not _sha256(snapshot_sha):
            blockers.append("historical_binding_fact_ws_snapshot_hash_invalid")
        elif snapshot_sha not in {
            str(official_ws.get("candidate_ws_snapshot_sha256") or "").lower(),
            str(official_ws.get("baseline_ws_snapshot_sha256") or "").lower(),
        }:
            blockers.append("historical_binding_fact_ws_not_in_candidate_baseline_pair")

    settlement = binding.get("settlement")
    if isinstance(settlement, Mapping):
        if str(settlement.get("settlement_truth_sha256") or "").lower() != str(
            label.get("truth_sha256") or ""
        ).lower():
            blockers.append("historical_binding_settlement_truth_hash_mismatch")
        if str(settlement.get("settlement_outcome_sha256") or "").lower() != str(
            label.get("outcome_sha256") or ""
        ).lower():
            blockers.append("historical_binding_settlement_outcome_hash_mismatch")
        if str(settlement.get("winner_bucket") or "") != str(
            label.get("winning_bucket") or ""
        ):
            blockers.append("historical_binding_winner_bucket_mismatch")
        bound_close = _utc(settlement.get("settlement_closed_at_utc"))
        if bound_close is None or bound_close != settlement_closed_at:
            blockers.append("historical_binding_settlement_close_mismatch")

    complete = not blockers
    return {
        "schema": "station_policy_historical_utility_binding_validation_v1",
        "family": family,
        "state": (
            "HISTORICAL_BINDING_COMPLETE_NOT_SCORED" if complete else "UNBOUND"
        ),
        "binding_valid": complete,
        "historical_strict_oos": complete,
        "utility_evaluated": False,
        "score_emitted": False,
        "direct_fill_binding": raw["direct_fill_binding"] if complete else "UNBOUND",
        "blockers": sorted(set(blockers)),
        "promotion_permitted": False,
        "contract_pointer": raw["contract_pointer"],
    }


def assert_station_policy_utility_binding(
    binding: Mapping[str, Any],
    *,
    family: str,
    contract: Mapping[str, Any] | None = None,
) -> None:
    result = validate_station_policy_utility_binding(
        binding, family=family, contract=contract
    )
    if result["blockers"]:
        raise ContractError(
            "station_policy_utility_binding_invalid:" + ",".join(result["blockers"])
        )


def build_future_station_policy_outcome_materialization(
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    stage: str,
    decision_at_utc: str,
    strategy_version: str,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
) -> dict[str, Any]:
    """Return the non-authorizing future-fact contract gap explicitly.

    This is deliberately a skeleton: the caller cannot supply an arbitrary
    prediction, action, cost, or score.  A later contract must add each
    family only with its constitution-approved frozen output and independent
    post-label utility scorer.
    """

    utility_contract = build_station_policy_utility_contract()
    identity = {
        "schema": MATERIALIZATION_SCHEMA,
        "decision_scope": {
            "station_id": str(station_id),
            "event_date": str(event_date),
            "settlement_timezone": str(settlement_timezone),
            "stage": str(stage),
            "strategy_version": str(strategy_version),
            "season_bin": str(season_bin),
            "maturity_bin": str(maturity_bin),
            "liquidity_bin": str(liquidity_bin),
            "decision_at_utc": str(decision_at_utc),
        },
        "state": "UNAVAILABLE",
        "family_states": {
            family: "UNAVAILABLE" for family in POLICY_FAMILIES
        },
        # The pointer is explicit lineage only.  It does not make the
        # family available and cannot be used as a score/promotion input.
        "utility_contract_pointer": build_station_policy_utility_contract_pointer(
            utility_contract
        ),
        "bindings": {},
        "unavailable_families": dict(UNAVAILABLE_REASONS),
        "production_no_live": True,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "promotion_permitted": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def assert_future_station_policy_outcome_materialization(
    materialization: Mapping[str, Any],
) -> None:
    identity = {
        key: value for key, value in materialization.items()
        if key != "content_sha256"
    }
    if materialization.get("schema") != MATERIALIZATION_SCHEMA:
        raise ValueError("station_policy_forward_outcome_materialization_schema_invalid")
    if materialization.get("content_sha256") != content_sha256(identity):
        raise ValueError("station_policy_forward_outcome_materialization_hash_invalid")
    if materialization.get("state") != "UNAVAILABLE":
        raise ValueError("station_policy_forward_outcome_materialization_state_invalid")
    pointer = materialization.get("utility_contract_pointer")
    try:
        assert_station_policy_utility_contract_pointer(pointer)  # type: ignore[arg-type]
    except ContractError as exc:
        raise ValueError(str(exc)) from exc
    if materialization.get("bindings") != {}:
        raise ValueError("station_policy_forward_outcome_materialization_bindings_forbidden")
    if materialization.get("family_states") != {
        family: "UNAVAILABLE" for family in POLICY_FAMILIES
    } or materialization.get("unavailable_families") != UNAVAILABLE_REASONS:
        raise ValueError("station_policy_forward_outcome_materialization_family_state_invalid")
    if (
        materialization.get("production_no_live") is not True
        or materialization.get("writes_production_db") is not False
        or materialization.get("calls_order_gateway") is not False
        or materialization.get("orders_submitted") != 0
        or materialization.get("promotion_permitted") is not False
    ):
        raise ValueError("station_policy_forward_outcome_materialization_authority_invalid")


__all__ = [
    "FORBIDDEN_DERIVED_FIELDS",
    "HISTORICAL_STRICT_OOS",
    "MATERIALIZATION_SCHEMA",
    "OBSERVATIONAL_CANDIDATE_NOT_OOS",
    "POLICY_FAMILIES",
    "UTILITY_CONTRACT_ARTIFACT_ID",
    "UTILITY_CONTRACT_POINTER_SCHEMA",
    "UTILITY_CONTRACT_SCHEMA",
    "UTILITY_CONTRACT_VERSION",
    "UNAVAILABLE_REASONS",
    "assert_station_policy_utility_binding",
    "assert_station_policy_utility_contract",
    "assert_station_policy_utility_contract_pointer",
    "assert_future_station_policy_outcome_materialization",
    "build_station_policy_utility_contract",
    "build_station_policy_utility_contract_pointer",
    "build_future_station_policy_outcome_materialization",
    "validate_station_policy_utility_binding",
    "validate_historical_station_policy_utility_binding",
]
