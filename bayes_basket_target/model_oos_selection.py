"""Canonical cross-family selection with a historical reserved holdout.

The reservation clock is deliberately separate from the event strict-as-of
clock.  Historical events must already satisfy the model/truth ordering in
``model_oos_contract``; the exact component mask and 20+8 partition must be
committed before any cohort evaluator reads the eight holdout labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from datetime import datetime
import json
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .model_oos_contract import (
    CanonicalModelOOSCohortFact as CanonicalLegacyModelOOSCohortFact,
    CanonicalModelOOSEventFact as CanonicalLegacyModelOOSEventFact,
)
from .model_product_identity import assert_formal_p0_native_ensemble_systems
from .native_ensemble_oos_contract import (
    COHORT_FACT_TYPE as NATIVE_ENSEMBLE_COHORT_FACT_TYPE,
    EVENT_FACT_TYPE as NATIVE_ENSEMBLE_EVENT_FACT_TYPE,
    CanonicalNativeEnsembleOOSCohortFact,
    CanonicalNativeEnsembleOOSEventFact,
    derive_native_ensemble_cohort_statistics,
)


CONTRACT_SCHEMA = "bayes_basket_target_canonical_model_oos_selection_contract_v2"
FACT_TYPE = "p0_model_family_selection"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
STATE = "MATERIALIZED_UNPUBLISHED"
SELECTION_METHOD_VERSION = "INVERSE_TRAIN_RMSE_V1"
WEIGHT_QUANTUM = Decimal("0.000000000001")
RMSE_FLOOR = Decimal("0.000001")

COMPONENT_FIELDS = {
    "model_id", "ensemble_system_id", "model_family", "provider_id",
    "provider_product_id", "independence_family_id", "source_class",
    "event_fact_type", "cohort_fact_type", "cohort_content_sha256", "train_event_set_sha256",
    "holdout_event_set_sha256", "selection_train_rmse_c", "weight",
}
FACT_FIELDS = {
    "station_id", "settlement_timezone", "lead_bucket", "lead_bucket_scope",
    "run_cycle", "run_cycle_scope", "season_bin", "source_class",
    "event_oos_protocol_version", "calibration_method_version",
    "authority_scope", "component_cohort_refs", "component_cohort_set_sha256",
    "available_model_set_hash", "independence_family_set_hash",
    "train_event_dates", "holdout_event_dates", "train_truth_vector_sha256",
    "holdout_truth_vector_sha256", "cohort_truth_vector_sha256",
    "selection_method_version", "weight_rmse_floor_c", "weight_quantum",
    "weight_rounding", "selection_as_of_utc", "state", "content_sha256",
    "pre_registered_mask_receipt", "pre_registered_mask_receipt_sha256",
    "holdout_label_access_receipt", "holdout_label_access_receipt_sha256",
    "selection_holdout_metrics", "uniform_holdout_metrics",
    "selection_evaluation_state",
}
MASK_SCHEMA = "bayes_basket_target_p0_historical_reserved_holdout_receipt_v4"
HOLDOUT_ACCESS_SCHEMA = (
    "bayes_basket_target_p0_historical_holdout_label_access_receipt_v2"
)
TERMINAL_RECEIPT_SCHEMA = (
    "bayes_basket_target_model_oos_selection_terminal_receipt_v1"
)
MASK_FIELDS = {
    "schema", "registered_at_utc", "station_id", "settlement_timezone",
    "component_identities",
    "available_model_set_hash", "selection_method_version",
    "component_input_commitments", "component_input_commitment_set_sha256",
    "train_event_dates", "holdout_event_dates", "train_date_vector_sha256",
    "holdout_date_vector_sha256", "partition_vector_sha256",
    "eligible_input_universe_sha256",
    "train_input_manifest_sha256",
    "prior_evaluation_absence_basis_sha256", "holdout_label_state",
    "state", "content_sha256",
}
RESERVATION_COMPONENT_FIELDS = {
    "component_identity", "event_fact_type", "event_input_commitments",
    "event_input_date_vector_sha256", "event_input_vector_sha256",
}
EVENT_INPUT_COMMITMENT_FIELDS = {
    "event_date", "adapter_event_input_content_sha256",
}
EVENT_REFERENCE_FIELDS = {"component_identity", "event_fact_type", "event_input_commitments"}
COHORT_REFERENCE_FIELDS = {
    "event_fact_type", "cohort_fact_type", "cohort_content_sha256",
}
HOLDOUT_TRUTH_COMMITMENT_FIELDS = {
    "event_date", "truth_id", "truth_revision_no", "truth_content_sha256",
    "canonical_truth_fact_content_sha256",
}
HOLDOUT_ACCESS_FIELDS = {
    "schema", "reservation_content_sha256", "holdout_unsealed_at_utc",
    "first_evaluated_at_utc", "evaluator_id", "access_mode",
    "prior_evaluation_absent", "prior_evaluation_absence_basis_sha256",
    "state", "content_sha256",
}
TERMINAL_RECEIPT_FIELDS = {
    "schema", "as_of_utc", "builder_id", "state", "blocker",
    "authority_scope", "station_id", "settlement_timezone", "lead_bucket",
    "lead_bucket_scope", "run_cycle", "run_cycle_scope", "season_bin",
    "source_class", "event_oos_protocol_version",
    "calibration_method_version", "component_cohort_results",
    "component_cohort_set_sha256", "available_model_set_hash",
    "holdout_event_dates", "first_holdout_station_day_end_utc",
    "oos_pass_count", "oos_rejected_count",
    "pre_registered_mask_receipt_sha256", "selection_contract_emitted",
    "writes_database", "publishes_pr04_pointer", "starts_services",
    "calls_order_gateway", "orders_submitted", "consumable",
    "content_sha256",
}


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"model_oos_selection_{label}_missing")
    return result


def _sha(value: Any, label: str) -> str:
    result = _text(value, label).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"model_oos_selection_{label}_invalid")
    return result


def _utc(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"model_oos_selection_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"model_oos_selection_{label}_timezone_required")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _decimal(value: Any, label: str) -> Decimal:
    text = _text(value, label)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ContractError(f"model_oos_selection_{label}_invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ContractError(f"model_oos_selection_{label}_invalid")
    return parsed


def _signed_decimal(value: Any, label: str) -> Decimal:
    text = _text(value, label)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ContractError(f"model_oos_selection_{label}_invalid") from exc
    if not parsed.is_finite():
        raise ContractError(f"model_oos_selection_{label}_invalid")
    return parsed


def _weight_text(value: Decimal) -> str:
    return format(value.quantize(WEIGHT_QUANTUM, rounding=ROUND_HALF_EVEN), ".12f")


def derive_weights(rmses: Sequence[Decimal]) -> tuple[str, ...]:
    if len(rmses) < 2:
        raise ContractError("model_oos_selection_two_components_required")
    inverse = [Decimal(1) / max(value, RMSE_FLOOR) for value in rmses]
    total = sum(inverse)
    weights: list[Decimal] = []
    for value in inverse[:-1]:
        weights.append((value / total).quantize(WEIGHT_QUANTUM, rounding=ROUND_HALF_EVEN))
    weights.append(Decimal(1) - sum(weights))
    if any(value <= 0 for value in weights):
        raise ContractError("model_oos_selection_positive_weight_required")
    return tuple(_weight_text(value) for value in weights)


def _metrics(errors: Sequence[Decimal]) -> dict[str, str | int]:
    if len(errors) != 8:
        raise ContractError("model_oos_selection_holdout_error_count_invalid")
    count = Decimal(len(errors))
    mae = sum(abs(value) for value in errors) / count
    mean_error = sum(errors) / count
    rmse = (sum(value * value for value in errors) / count).sqrt()
    return {
        "mae_c": _weight_text(mae),
        "mean_error_c": _weight_text(mean_error),
        "rmse_c": _weight_text(rmse),
        "sample_count": len(errors),
    }


def _validate_metrics(value: Any, label: str) -> dict[str, str | int]:
    if not isinstance(value, Mapping) or set(value) != {
        "mae_c", "mean_error_c", "rmse_c", "sample_count"
    } or value["sample_count"] != 8:
        raise ContractError(f"model_oos_selection_{label}_invalid")
    return {
        "mae_c": _weight_text(_decimal(value["mae_c"], f"{label}_mae")),
        "mean_error_c": _weight_text(_signed_decimal(value["mean_error_c"], f"{label}_mean_error")),
        "rmse_c": _weight_text(_decimal(value["rmse_c"], f"{label}_rmse")),
        "sample_count": 8,
    }


def _component_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: raw[key] for key in (
        "model_id", "ensemble_system_id", "model_family", "provider_id",
        "provider_product_id", "independence_family_id", "source_class",
    )}


def _seal(identity: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(identity)
    return {**value, "content_sha256": content_sha256(value)}


def _native_event_reference(raw: Any) -> dict[str, Any]:
    """Parse one embargoed, formal-native model-input commitment vector.

    Reservation occurs before labels and cohorts exist.  It therefore binds
    the canonical adapter input for each date, not a later OOS event hash or
    a truth revision.  The explicit fact type prevents a singular legacy
    event or a republisher event from entering the formal-native lane.
    """

    if not isinstance(raw, Mapping) or set(raw) != EVENT_REFERENCE_FIELDS:
        raise ContractError("model_oos_selection_event_reference_fields_invalid")
    value = dict(raw)
    if value["event_fact_type"] != NATIVE_ENSEMBLE_EVENT_FACT_TYPE:
        raise ContractError("model_oos_selection_formal_native_event_fact_type_required")
    identity = value["component_identity"]
    if not isinstance(identity, Mapping) or set(identity) != {
        "model_id", "ensemble_system_id", "model_family", "provider_id",
        "provider_product_id", "independence_family_id", "source_class",
    }:
        raise ContractError("model_oos_selection_input_commitment_identity_invalid")
    value["component_identity"] = _component_identity(identity)
    inputs = value["event_input_commitments"]
    if not isinstance(inputs, list) or len(inputs) != 28:
        raise ContractError("model_oos_selection_input_commitment_vector_invalid")
    commitments: list[dict[str, str]] = []
    for item in inputs:
        if not isinstance(item, Mapping) or set(item) != EVENT_INPUT_COMMITMENT_FIELDS:
            raise ContractError("model_oos_selection_input_commitment_fields_invalid")
        commitments.append({
            "event_date": _text(item["event_date"], "input_commitment_event_date"),
            "adapter_event_input_content_sha256": _sha(
                item["adapter_event_input_content_sha256"],
                "adapter_event_input_content_sha256",
            ),
        })
    dates = [item["event_date"] for item in commitments]
    if dates != sorted(set(dates)):
        raise ContractError("model_oos_selection_input_commitment_date_order_invalid")
    if len({item["adapter_event_input_content_sha256"] for item in commitments}) != 28:
        raise ContractError("model_oos_selection_input_commitment_hash_duplicate")
    value["event_input_commitments"] = commitments
    return value


def _native_cohort_reference(raw: Any) -> dict[str, str]:
    """Parse one explicit, formal-native cohort reference."""

    if not isinstance(raw, Mapping) or set(raw) != COHORT_REFERENCE_FIELDS:
        raise ContractError("model_oos_selection_cohort_reference_fields_invalid")
    value = dict(raw)
    if (
        value["event_fact_type"] != NATIVE_ENSEMBLE_EVENT_FACT_TYPE
        or value["cohort_fact_type"] != NATIVE_ENSEMBLE_COHORT_FACT_TYPE
    ):
        raise ContractError("model_oos_selection_formal_native_cohort_fact_type_required")
    return {
        "event_fact_type": NATIVE_ENSEMBLE_EVENT_FACT_TYPE,
        "cohort_fact_type": NATIVE_ENSEMBLE_COHORT_FACT_TYPE,
        "cohort_content_sha256": _sha(
            value["cohort_content_sha256"], "cohort_reference_content_sha256"
        ),
    }


def _load_native_event(
    conn: sqlite3.Connection, content_sha256: str,
) -> CanonicalNativeEnsembleOOSEventFact:
    return _load_fact(
        conn,
        NATIVE_ENSEMBLE_EVENT_FACT_TYPE,
        _sha(content_sha256, "native_event_content_sha256"),
        CanonicalNativeEnsembleOOSEventFact,
    )


def _load_native_cohort(
    conn: sqlite3.Connection, content_sha256: str,
) -> CanonicalNativeEnsembleOOSCohortFact:
    return _load_fact(
        conn,
        NATIVE_ENSEMBLE_COHORT_FACT_TYPE,
        _sha(content_sha256, "native_cohort_content_sha256"),
        CanonicalNativeEnsembleOOSCohortFact,
    )


def _assert_native_cohort_derivation(
    cohort: CanonicalNativeEnsembleOOSCohortFact,
    events: Sequence[CanonicalNativeEnsembleOOSEventFact],
) -> None:
    """Recompute the cohort before using it in formal cross-family P0."""

    derived = derive_native_ensemble_cohort_statistics(events)
    fields = (
        "calibration_parameters", "selection_train_metrics", "holdout_metrics",
        "baseline_holdout_metrics",
    )
    if any(
        cohort.raw[field] != expected
        for field, expected in zip(fields, derived[:4])
    ) or cohort.raw["evaluation_state"] != derived[4]:
        raise ContractError("model_oos_selection_native_cohort_derivation_mismatch")


def _native_event_input_commitments(
    events: Sequence[CanonicalNativeEnsembleOOSEventFact],
) -> list[dict[str, str]]:
    """Project the pre-label adapter input identity from native OOS events."""

    commitments: list[dict[str, str]] = []
    for event in events:
        try:
            input_sha = event.raw["adapter_event_input_content_sha256"]
        except KeyError as exc:
            raise ContractError(
                "model_oos_selection_native_event_adapter_input_required"
            ) from exc
        commitments.append({
            "event_date": _text(event.raw["event_date"], "event_input_event_date"),
            "adapter_event_input_content_sha256": _sha(
                input_sha, "event_adapter_input_content_sha256"
            ),
        })
    return commitments


def _reservation_component_for_identity(
    reservation: Mapping[str, Any], identity: Mapping[str, Any],
) -> Mapping[str, Any]:
    matches = [
        item for item in reservation["component_input_commitments"]
        if item["component_identity"] == _component_identity(identity)
    ]
    if len(matches) != 1:
        raise ContractError("model_oos_selection_reservation_component_missing")
    return matches[0]


def _reservation_component(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != RESERVATION_COMPONENT_FIELDS:
        raise ContractError("model_oos_selection_reservation_component_invalid")
    value = dict(raw)
    if value["event_fact_type"] != NATIVE_ENSEMBLE_EVENT_FACT_TYPE:
        raise ContractError("model_oos_selection_formal_native_event_fact_type_required")
    identity = value["component_identity"]
    if not isinstance(identity, Mapping) or set(identity) != {
        "model_id", "ensemble_system_id", "model_family", "provider_id",
        "provider_product_id", "independence_family_id", "source_class",
    }:
        raise ContractError("model_oos_selection_reservation_identity_invalid")
    value["component_identity"] = _component_identity(identity)
    inputs = value["event_input_commitments"]
    if not isinstance(inputs, list) or len(inputs) != 28:
        raise ContractError("model_oos_selection_reservation_input_vector_invalid")
    commitments: list[dict[str, str]] = []
    for item in inputs:
        if not isinstance(item, Mapping) or set(item) != EVENT_INPUT_COMMITMENT_FIELDS:
            raise ContractError("model_oos_selection_reservation_input_fields_invalid")
        commitments.append({
            "event_date": _text(item["event_date"], "reservation_input_event_date"),
            "adapter_event_input_content_sha256": _sha(
                item["adapter_event_input_content_sha256"],
                "reservation_adapter_event_input_content_sha256",
            ),
        })
    dates = [item["event_date"] for item in commitments]
    if dates != sorted(set(dates)) or len({
        item["adapter_event_input_content_sha256"] for item in commitments
    }) != 28:
        raise ContractError("model_oos_selection_reservation_input_order_invalid")
    value["event_input_commitments"] = commitments
    for name in ("event_input_date_vector_sha256", "event_input_vector_sha256"):
        value[name] = _sha(value[name], name)
    if (
        value["event_input_date_vector_sha256"] != content_sha256(dates)
        or value["event_input_vector_sha256"] != content_sha256(commitments)
    ):
        raise ContractError("model_oos_selection_reservation_input_hash_mismatch")
    return value


def validate_historical_holdout_reservation(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) != MASK_FIELDS or raw.get("schema") != MASK_SCHEMA:
        raise ContractError("model_oos_selection_reservation_fields_invalid")
    value = dict(raw)
    value["registered_at_utc"] = _utc(
        value["registered_at_utc"], "reservation_registered_at_utc"
    )
    if (
        value["holdout_label_state"] != "EMBARGOED"
        or value["state"] != STATE
        or value["selection_method_version"] != SELECTION_METHOD_VERSION
    ):
        raise ContractError("model_oos_selection_reservation_semantics_invalid")
    value["station_id"] = _text(value["station_id"], "reservation_station_id").upper()
    value["settlement_timezone"] = _text(
        value["settlement_timezone"], "reservation_settlement_timezone"
    )
    if value["settlement_timezone"] != "America/New_York":
        raise ContractError("model_oos_selection_reservation_timezone_not_et")
    components_raw = value["component_input_commitments"]
    if not isinstance(components_raw, list) or len(components_raw) < 2:
        raise ContractError("model_oos_selection_reservation_components_short")
    components = [_reservation_component(item) for item in components_raw]
    identities = [item["component_identity"] for item in components]
    order = lambda item: tuple(str(item[key] or "") for key in item)
    if identities != sorted(identities, key=order) or value["component_identities"] != identities:
        raise ContractError("model_oos_selection_reservation_component_order_invalid")
    if len({item["independence_family_id"] for item in identities}) < 2:
        raise ContractError("model_oos_selection_reservation_families_short")
    assert_formal_p0_native_ensemble_systems(identities)
    value["component_input_commitments"] = components
    for name in (
        "available_model_set_hash", "component_input_commitment_set_sha256",
        "train_date_vector_sha256", "holdout_date_vector_sha256",
        "partition_vector_sha256", "eligible_input_universe_sha256",
        "train_input_manifest_sha256",
        "prior_evaluation_absence_basis_sha256",
    ):
        value[name] = _sha(value[name], name)
    if value["available_model_set_hash"] != content_sha256(identities):
        raise ContractError("model_oos_selection_reservation_available_set_mismatch")
    if value["component_input_commitment_set_sha256"] != content_sha256(components):
        raise ContractError("model_oos_selection_reservation_component_set_mismatch")
    universe = [{
        "component_identity": item["component_identity"],
        "event_fact_type": item["event_fact_type"],
        "event_input_vector_sha256": item["event_input_vector_sha256"],
    } for item in components]
    train_design = [{
        "component_identity": item["component_identity"],
        "event_fact_type": item["event_fact_type"],
        "train_event_input_set_sha256": content_sha256(
            item["event_input_commitments"][:20]
        ),
    } for item in components]
    if (
        value["eligible_input_universe_sha256"] != content_sha256(universe)
        or value["train_input_manifest_sha256"] != content_sha256(train_design)
    ):
        raise ContractError("model_oos_selection_reservation_design_hash_mismatch")
    train_dates = value["train_event_dates"]
    holdout_dates = value["holdout_event_dates"]
    if (
        not isinstance(train_dates, list) or len(train_dates) != 20
        or train_dates != sorted(set(train_dates))
        or not isinstance(holdout_dates, list) or len(holdout_dates) != 8
        or holdout_dates != sorted(set(holdout_dates))
        or train_dates[-1] >= holdout_dates[0]
    ):
        raise ContractError("model_oos_selection_reservation_dates_invalid")
    if (
        value["train_date_vector_sha256"] != content_sha256(train_dates)
        or value["holdout_date_vector_sha256"] != content_sha256(holdout_dates)
        or value["partition_vector_sha256"]
        != content_sha256({"train": train_dates, "holdout": holdout_dates})
    ):
        raise ContractError("model_oos_selection_reservation_date_hash_mismatch")
    if any(
        [item["event_date"] for item in component["event_input_commitments"]]
        != [*train_dates, *holdout_dates]
        for component in components
    ):
        raise ContractError("model_oos_selection_reservation_input_dates_mismatch")
    supplied = _sha(value["content_sha256"], "reservation_content_sha256")
    if supplied != content_sha256({k: v for k, v in value.items() if k != "content_sha256"}):
        raise ContractError("model_oos_selection_reservation_content_sha256_mismatch")
    value["content_sha256"] = supplied
    return value


def validate_historical_holdout_access(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) != HOLDOUT_ACCESS_FIELDS or raw.get("schema") != HOLDOUT_ACCESS_SCHEMA:
        raise ContractError("model_oos_selection_holdout_access_fields_invalid")
    value = dict(raw)
    for name in ("reservation_content_sha256", "prior_evaluation_absence_basis_sha256"):
        value[name] = _sha(value[name], name)
    for name in ("holdout_unsealed_at_utc", "first_evaluated_at_utc"):
        value[name] = _utc(value[name], name)
    if (
        value["first_evaluated_at_utc"] < value["holdout_unsealed_at_utc"]
        or value["prior_evaluation_absent"] is not True
        or value["access_mode"] != "SINGLE_RESERVED_HISTORICAL_EVALUATION"
        or value["state"] != STATE
    ):
        raise ContractError("model_oos_selection_holdout_access_semantics_invalid")
    supplied = _sha(value["content_sha256"], "holdout_access_content_sha256")
    if supplied != content_sha256({k: v for k, v in value.items() if k != "content_sha256"}):
        raise ContractError("model_oos_selection_holdout_access_content_sha256_mismatch")
    value["content_sha256"] = supplied
    return value


@dataclass(frozen=True)
class CanonicalModelOOSSelectionFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalModelOOSSelectionFact":
        if set(raw) != FACT_FIELDS:
            raise ContractError("model_oos_selection_fact_fields_mismatch")
        value = dict(raw)
        for field in (
            "station_id", "settlement_timezone", "lead_bucket", "lead_bucket_scope",
            "run_cycle_scope", "season_bin", "source_class",
            "event_oos_protocol_version", "calibration_method_version",
        ):
            value[field] = _text(value[field], field)
        value["station_id"] = value["station_id"].upper()
        value["run_cycle"] = None if value["run_cycle"] is None else _text(value["run_cycle"], "run_cycle")
        if value["authority_scope"] != AUTHORITY_SCOPE or value["state"] != STATE:
            raise ContractError("model_oos_selection_authority_state_invalid")
        if value["selection_method_version"] != SELECTION_METHOD_VERSION:
            raise ContractError("model_oos_selection_method_invalid")
        if (
            value["weight_rmse_floor_c"] != format(RMSE_FLOOR, "f")
            or value["weight_quantum"] != format(WEIGHT_QUANTUM, "f")
            or value["weight_rounding"] != "ROUND_HALF_EVEN"
        ):
            raise ContractError("model_oos_selection_weight_parameters_invalid")
        value["selection_as_of_utc"] = _utc(value["selection_as_of_utc"], "selection_as_of_utc")
        components_raw = value["component_cohort_refs"]
        if not isinstance(components_raw, list) or len(components_raw) < 2:
            raise ContractError("model_oos_selection_two_components_required")
        components: list[dict[str, Any]] = []
        for raw_component in components_raw:
            if not isinstance(raw_component, Mapping) or set(raw_component) != COMPONENT_FIELDS:
                raise ContractError("model_oos_selection_component_fields_mismatch")
            component = dict(raw_component)
            if (
                component["event_fact_type"] != NATIVE_ENSEMBLE_EVENT_FACT_TYPE
                or component["cohort_fact_type"] != NATIVE_ENSEMBLE_COHORT_FACT_TYPE
            ):
                raise ContractError(
                    "model_oos_selection_formal_native_cohort_fact_type_required"
                )
            for field in (
                "model_id", "model_family", "provider_id", "provider_product_id",
                "independence_family_id", "source_class",
            ):
                component[field] = _text(component[field], f"component_{field}")
            component["model_id"] = component["model_id"].upper()
            component["ensemble_system_id"] = (
                None if component["ensemble_system_id"] is None
                else _text(component["ensemble_system_id"], "component_ensemble_system_id")
            )
            component["cohort_content_sha256"] = _sha(
                component["cohort_content_sha256"], "component_cohort_sha256"
            )
            component["train_event_set_sha256"] = _sha(
                component["train_event_set_sha256"], "component_train_event_set_sha256"
            )
            component["holdout_event_set_sha256"] = _sha(
                component["holdout_event_set_sha256"], "component_holdout_event_set_sha256"
            )
            rmse = _decimal(component["selection_train_rmse_c"], "component_train_rmse")
            component["selection_train_rmse_c"] = format(rmse, "f")
            component["weight"] = _weight_text(_decimal(component["weight"], "component_weight"))
            if component["source_class"] != value["source_class"]:
                raise ContractError("model_oos_selection_source_lane_mixed")
            components.append(component)
        identities = [_component_identity(item) for item in components]
        if identities != sorted(identities, key=lambda item: tuple(str(item[key] or "") for key in item)):
            raise ContractError("model_oos_selection_component_order_invalid")
        if len({json.dumps(item, sort_keys=True) for item in identities}) != len(identities):
            raise ContractError("model_oos_selection_component_duplicate")
        if len({item["independence_family_id"] for item in components}) < 2:
            raise ContractError("model_oos_selection_independent_families_short")
        expected_weights = derive_weights([
            _decimal(item["selection_train_rmse_c"], "component_train_rmse")
            for item in components
        ])
        if tuple(item["weight"] for item in components) != expected_weights:
            raise ContractError("model_oos_selection_weight_mismatch")
        assert_formal_p0_native_ensemble_systems(components)
        value["component_cohort_refs"] = components
        for field in (
            "component_cohort_set_sha256", "available_model_set_hash",
            "independence_family_set_hash", "train_truth_vector_sha256",
            "holdout_truth_vector_sha256", "cohort_truth_vector_sha256",
        ):
            value[field] = _sha(value[field], field)
        cohort_refs = [{
            "event_fact_type": item["event_fact_type"],
            "cohort_fact_type": item["cohort_fact_type"],
            "cohort_content_sha256": item["cohort_content_sha256"],
        } for item in components]
        if value["component_cohort_set_sha256"] != content_sha256(cohort_refs):
            raise ContractError("model_oos_selection_cohort_set_hash_mismatch")
        if value["available_model_set_hash"] != content_sha256(identities):
            raise ContractError("model_oos_selection_available_set_hash_mismatch")
        mask = value["pre_registered_mask_receipt"]
        if not isinstance(mask, Mapping) or set(mask) != MASK_FIELDS:
            raise ContractError("model_oos_selection_mask_receipt_invalid")
        mask_value = validate_historical_holdout_reservation(mask)
        for field in ("station_id", "settlement_timezone"):
            if mask_value[field] != value[field]:
                raise ContractError("model_oos_selection_mask_scope_mismatch")
        if (
            mask_value["component_identities"] != identities
            or mask_value["available_model_set_hash"] != value["available_model_set_hash"]
            or mask_value["selection_method_version"] != value["selection_method_version"]
            or [item["event_fact_type"] for item in mask_value[
                "component_input_commitments"
            ]] != [item["event_fact_type"] for item in components]
        ):
            raise ContractError("model_oos_selection_mask_component_mismatch")
        mask_sha = _sha(mask_value["content_sha256"], "mask_content_sha256")
        if mask_sha != content_sha256({key: item for key, item in mask_value.items() if key != "content_sha256"}):
            raise ContractError("model_oos_selection_mask_content_sha256_mismatch")
        if mask_sha != _sha(value["pre_registered_mask_receipt_sha256"], "mask_receipt_sha256"):
            raise ContractError("model_oos_selection_mask_sha256_mismatch")
        value["pre_registered_mask_receipt"] = {**mask_value, "content_sha256": mask_sha}
        value["pre_registered_mask_receipt_sha256"] = mask_sha
        access = value["holdout_label_access_receipt"]
        if not isinstance(access, Mapping):
            raise ContractError("model_oos_selection_holdout_access_invalid")
        access_value = validate_historical_holdout_access(access)
        if (
            access_value["reservation_content_sha256"] != mask_sha
            or access_value["prior_evaluation_absence_basis_sha256"]
            != mask_value["prior_evaluation_absence_basis_sha256"]
            or mask_value["registered_at_utc"] > access_value["holdout_unsealed_at_utc"]
            or access_value["first_evaluated_at_utc"] > value["selection_as_of_utc"]
        ):
            raise ContractError("model_oos_selection_holdout_access_chain_invalid")
        access_sha = access_value["content_sha256"]
        if access_sha != _sha(
            value["holdout_label_access_receipt_sha256"],
            "holdout_label_access_receipt_sha256",
        ):
            raise ContractError("model_oos_selection_holdout_access_sha256_mismatch")
        value["holdout_label_access_receipt"] = access_value
        value["holdout_label_access_receipt_sha256"] = access_sha
        value["selection_holdout_metrics"] = _validate_metrics(
            value["selection_holdout_metrics"], "selection_holdout_metrics"
        )
        value["uniform_holdout_metrics"] = _validate_metrics(
            value["uniform_holdout_metrics"], "uniform_holdout_metrics"
        )
        if (
            value["selection_evaluation_state"] != "OOS_PASS"
            or Decimal(value["selection_holdout_metrics"]["mae_c"])
            > Decimal(value["uniform_holdout_metrics"]["mae_c"])
        ):
            raise ContractError("model_oos_selection_holdout_evaluation_invalid")
        families = sorted({item["independence_family_id"] for item in components})
        if value["independence_family_set_hash"] != content_sha256(families):
            raise ContractError("model_oos_selection_family_set_hash_mismatch")
        train_dates = value["train_event_dates"]
        dates = value["holdout_event_dates"]
        if (
            not isinstance(train_dates, list) or len(train_dates) != 20
            or train_dates != sorted(set(train_dates))
            or not isinstance(dates, list) or len(dates) != 8
            or dates != sorted(set(dates)) or train_dates[-1] >= dates[0]
        ):
            raise ContractError("model_oos_selection_holdout_dates_invalid")
        value["train_event_dates"] = [_text(item, "train_event_date") for item in train_dates]
        value["holdout_event_dates"] = [_text(item, "holdout_event_date") for item in dates]
        if (
            mask_value["train_event_dates"] != value["train_event_dates"]
            or mask_value["holdout_event_dates"] != value["holdout_event_dates"]
        ):
            raise ContractError("model_oos_selection_reservation_partition_mismatch")
        supplied = _sha(value["content_sha256"], "content_sha256")
        if supplied != content_sha256({key: item for key, item in value.items() if key != "content_sha256"}):
            raise ContractError("model_oos_selection_content_sha256_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(str(self.raw[key] if self.raw[key] is not None else "null") for key in (
            "station_id", "settlement_timezone", "lead_bucket", "lead_bucket_scope",
            "run_cycle", "run_cycle_scope", "season_bin", "source_class",
            "event_oos_protocol_version", "calibration_method_version",
            "authority_scope", "available_model_set_hash", "cohort_truth_vector_sha256",
        ))


@dataclass(frozen=True)
class CanonicalModelOOSSelectionContract:
    as_of_utc: str
    builder_id: str
    selection_facts: tuple[CanonicalModelOOSSelectionFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalModelOOSSelectionContract":
        if set(raw) != {"schema", "as_of_utc", "builder_id", "selection_facts", "content_sha256"}:
            raise ContractError("model_oos_selection_contract_fields_mismatch")
        if raw["schema"] != CONTRACT_SCHEMA:
            raise ContractError("model_oos_selection_contract_schema_invalid")
        if not isinstance(raw["selection_facts"], list) or not raw["selection_facts"]:
            raise ContractError("model_oos_selection_fact_required")
        facts = tuple(CanonicalModelOOSSelectionFact.from_mapping(item) for item in raw["selection_facts"])
        if len({item.scope_key for item in facts}) != len(facts):
            raise ContractError("model_oos_selection_scope_duplicate")
        supplied = _sha(raw["content_sha256"], "contract_content_sha256")
        if supplied != content_sha256({key: item for key, item in raw.items() if key != "content_sha256"}):
            raise ContractError("model_oos_selection_contract_content_sha256_mismatch")
        as_of = _utc(raw["as_of_utc"], "contract_as_of_utc")
        if any(item.raw["selection_as_of_utc"] > as_of for item in facts):
            raise ContractError("model_oos_selection_fact_after_contract_asof")
        return cls(as_of, _text(raw["builder_id"], "builder_id"), facts, supplied)


def _load_fact(conn: sqlite3.Connection, fact_type: str, sha: str, cls: Any) -> Any:
    rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact WHERE fact_type=? AND content_sha256=?",
        (fact_type, sha),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError(f"model_oos_selection_{fact_type}_not_unique")
    return cls.from_mapping(json.loads(str(rows[0][0])))


def build_historical_holdout_reservation(
    *,
    station_id: str,
    settlement_timezone: str,
    component_input_commitments: Sequence[Mapping[str, Any]],
    registered_at_utc: str,
    prior_evaluation_absence_basis_sha256: str,
) -> dict[str, Any]:
    """Freeze an exact native-input 20+8 partition before labels exist.

    This function intentionally has no database handle: a reservation may not
    read a materialized OOS event, closed truth or cohort.  The only inputs are
    canonical adapter input commitments available before label access.
    """

    if len(component_input_commitments) < 2:
        raise ContractError("model_oos_selection_reservation_components_short")
    references = [
        _native_event_reference(item) for item in component_input_commitments
    ]
    date_vectors = [
        [item["event_date"] for item in reference["event_input_commitments"]]
        for reference in references
    ]
    if any(vector != date_vectors[0] for vector in date_vectors[1:]):
        raise ContractError("model_oos_selection_reservation_date_vector_mismatch")
    if date_vectors[0] != sorted(date_vectors[0]):
        raise ContractError("model_oos_selection_reservation_event_order_invalid")
    commitments = [{
        "component_identity": item["component_identity"],
        "event_fact_type": item["event_fact_type"],
        "event_input_commitments": item["event_input_commitments"],
        "event_input_date_vector_sha256": content_sha256(
            [entry["event_date"] for entry in item["event_input_commitments"]]
        ),
        "event_input_vector_sha256": content_sha256(
            item["event_input_commitments"]
        ),
    } for item in references]
    commitments.sort(
        key=lambda item: tuple(
            str(item["component_identity"][key] or "")
            for key in item["component_identity"]
        )
    )
    identities = [item["component_identity"] for item in commitments]
    assert_formal_p0_native_ensemble_systems(identities)
    train_dates = date_vectors[0][:20]
    holdout_dates = date_vectors[0][20:]
    train_design = [{
        "component_identity": item["component_identity"],
        "event_fact_type": item["event_fact_type"],
        "train_event_input_set_sha256": content_sha256(
            item["event_input_commitments"][:20]
        ),
    } for item in commitments]
    universe = [{
        "component_identity": item["component_identity"],
        "event_fact_type": item["event_fact_type"],
        "event_input_vector_sha256": item["event_input_vector_sha256"],
    } for item in commitments]
    identity = {
        "schema": MASK_SCHEMA,
        "registered_at_utc": _utc(registered_at_utc, "reservation_registered_at_utc"),
        "station_id": _text(station_id, "reservation_station_id").upper(),
        "settlement_timezone": _text(
            settlement_timezone, "reservation_settlement_timezone"
        ),
        "component_identities": identities,
        "available_model_set_hash": content_sha256(identities),
        "selection_method_version": SELECTION_METHOD_VERSION,
        "component_input_commitments": commitments,
        "component_input_commitment_set_sha256": content_sha256(commitments),
        "train_event_dates": train_dates,
        "holdout_event_dates": holdout_dates,
        "train_date_vector_sha256": content_sha256(train_dates),
        "holdout_date_vector_sha256": content_sha256(holdout_dates),
        "partition_vector_sha256": content_sha256({"train": train_dates, "holdout": holdout_dates}),
        "eligible_input_universe_sha256": content_sha256(universe),
        "train_input_manifest_sha256": content_sha256(train_design),
        "prior_evaluation_absence_basis_sha256": _sha(
            prior_evaluation_absence_basis_sha256,
            "prior_evaluation_absence_basis_sha256",
        ),
        "holdout_label_state": "EMBARGOED",
        "state": STATE,
    }
    return validate_historical_holdout_reservation(_seal(identity))


def build_historical_holdout_access_receipt(
    *,
    reservation: Mapping[str, Any],
    holdout_unsealed_at_utc: str,
    first_evaluated_at_utc: str,
    evaluator_id: str,
) -> dict[str, Any]:
    reserved = validate_historical_holdout_reservation(reservation)
    identity = {
        "schema": HOLDOUT_ACCESS_SCHEMA,
        "reservation_content_sha256": reserved["content_sha256"],
        "holdout_unsealed_at_utc": _utc(
            holdout_unsealed_at_utc, "holdout_unsealed_at_utc"
        ),
        "first_evaluated_at_utc": _utc(
            first_evaluated_at_utc, "first_evaluated_at_utc"
        ),
        "evaluator_id": _text(evaluator_id, "holdout_evaluator_id"),
        "access_mode": "SINGLE_RESERVED_HISTORICAL_EVALUATION",
        "prior_evaluation_absent": True,
        "prior_evaluation_absence_basis_sha256": reserved[
            "prior_evaluation_absence_basis_sha256"
        ],
        "state": STATE,
    }
    receipt = validate_historical_holdout_access(_seal(identity))
    if reserved["registered_at_utc"] > receipt["holdout_unsealed_at_utc"]:
        raise ContractError("model_oos_selection_holdout_unsealed_before_reservation")
    return receipt


def validate_model_oos_selection_terminal_receipt(
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    if set(raw) != TERMINAL_RECEIPT_FIELDS:
        raise ContractError("model_oos_selection_terminal_fields_mismatch")
    value = dict(raw)
    if (
        value["schema"] != TERMINAL_RECEIPT_SCHEMA
        or value["state"] != "BLOCKED"
        or value["blocker"] != "PRE_REGISTERED_MASK_RECEIPT_MISSING"
        or value["authority_scope"] != AUTHORITY_SCOPE
        or value["pre_registered_mask_receipt_sha256"] is not None
        or value["selection_contract_emitted"] is not False
        or value["writes_database"] is not False
        or value["publishes_pr04_pointer"] is not False
        or value["starts_services"] is not False
        or value["calls_order_gateway"] is not False
        or value["orders_submitted"] != 0
        or value["consumable"] is not False
    ):
        raise ContractError("model_oos_selection_terminal_semantics_invalid")
    value["as_of_utc"] = _utc(value["as_of_utc"], "terminal_as_of_utc")
    value["first_holdout_station_day_end_utc"] = _utc(
        value["first_holdout_station_day_end_utc"],
        "terminal_first_holdout_station_day_end_utc",
    )
    components = value["component_cohort_results"]
    if not isinstance(components, list) or not components:
        raise ContractError("model_oos_selection_terminal_components_missing")
    if value["oos_pass_count"] + value["oos_rejected_count"] != len(components):
        raise ContractError("model_oos_selection_terminal_state_count_mismatch")
    supplied = _sha(value["content_sha256"], "terminal_content_sha256")
    if supplied != content_sha256(
        {key: item for key, item in value.items() if key != "content_sha256"}
    ):
        raise ContractError("model_oos_selection_terminal_content_sha256_mismatch")
    value["content_sha256"] = supplied
    return value


def build_model_oos_selection_terminal_receipt(
    conn: sqlite3.Connection,
    *,
    cohort_content_sha256s: Sequence[str],
    pre_registered_mask_receipt: Mapping[str, Any] | None,
    as_of_utc: str,
) -> dict[str, Any]:
    """Materialize the missing-mask terminal without inventing registration."""

    if pre_registered_mask_receipt is not None:
        raise ContractError("model_oos_selection_terminal_requires_missing_mask")
    if (
        not cohort_content_sha256s
        or len(set(cohort_content_sha256s)) != len(cohort_content_sha256s)
    ):
        raise ContractError("model_oos_selection_terminal_cohort_vector_invalid")
    cohorts = [
        _load_fact(
            conn,
            "model_oos_cohort",
            _sha(value, "terminal_cohort_sha256"),
            CanonicalLegacyModelOOSCohortFact,
        )
        for value in cohort_content_sha256s
    ]
    scope_fields = (
        "station_id", "settlement_timezone", "lead_bucket",
        "lead_bucket_scope", "run_cycle", "run_cycle_scope", "season_bin",
        "source_class", "authority_scope", "event_oos_protocol_version",
        "calibration_method_version",
    )
    if any(
        any(item.raw[field] != cohorts[0].raw[field] for field in scope_fields)
        for item in cohorts[1:]
    ):
        raise ContractError("model_oos_selection_terminal_cohort_scope_mismatch")
    components = []
    holdout_vectors: list[list[str]] = []
    first_holdout_day_ends: list[str] = []
    for cohort in cohorts:
        holdout_hashes = cohort.raw["holdout_event_content_sha256s"]
        holdout_events = [
            _load_fact(
                conn, "model_oos_event", sha, CanonicalLegacyModelOOSEventFact
            )
            for sha in holdout_hashes
        ]
        holdout_vectors.append([event.raw["event_date"] for event in holdout_events])
        first_holdout_day_ends.append(
            holdout_events[0].raw["station_day_end_utc"]
        )
        components.append(
            {
                **_component_identity(cohort.raw),
                "cohort_content_sha256": cohort.content_sha256,
                "evaluation_state": cohort.raw["evaluation_state"],
            }
        )
    if any(vector != holdout_vectors[0] for vector in holdout_vectors[1:]):
        raise ContractError("model_oos_selection_terminal_holdout_dates_mismatch")
    components.sort(
        key=lambda item: tuple(
            str(item[key] or "") for key in _component_identity(item)
        )
    )
    base = cohorts[0].raw
    identity = {
        "schema": TERMINAL_RECEIPT_SCHEMA,
        "as_of_utc": _utc(as_of_utc, "terminal_as_of_utc"),
        "builder_id": "canonical.model_oos_selection_terminal.v1",
        "state": "BLOCKED",
        "blocker": "PRE_REGISTERED_MASK_RECEIPT_MISSING",
        **{field: base[field] for field in scope_fields},
        "component_cohort_results": components,
        "component_cohort_set_sha256": content_sha256(
            sorted(item["cohort_content_sha256"] for item in components)
        ),
        "available_model_set_hash": content_sha256(
            [_component_identity(item) for item in components]
        ),
        "holdout_event_dates": holdout_vectors[0],
        "first_holdout_station_day_end_utc": min(first_holdout_day_ends),
        "oos_pass_count": sum(
            item["evaluation_state"] == "OOS_PASS" for item in components
        ),
        "oos_rejected_count": sum(
            item["evaluation_state"] == "OOS_REJECTED" for item in components
        ),
        "pre_registered_mask_receipt_sha256": None,
        "selection_contract_emitted": False,
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "consumable": False,
    }
    receipt = {**identity, "content_sha256": content_sha256(identity)}
    return validate_model_oos_selection_terminal_receipt(receipt)


def build_model_oos_selection_contract(
    conn: sqlite3.Connection,
    *,
    component_cohort_refs: Sequence[Mapping[str, Any]],
    pre_registered_mask_receipt: Mapping[str, Any],
    holdout_label_access_receipt: Mapping[str, Any],
    as_of_utc: str,
) -> dict[str, Any]:
    if len(component_cohort_refs) < 2:
        raise ContractError("model_oos_selection_cohort_vector_invalid")
    references = [_native_cohort_reference(item) for item in component_cohort_refs]
    if len({item["cohort_content_sha256"] for item in references}) != len(references):
        raise ContractError("model_oos_selection_cohort_vector_invalid")
    cohorts = [
        _load_native_cohort(conn, item["cohort_content_sha256"])
        for item in references
    ]
    selection_as_of = _utc(as_of_utc, "selection_as_of_utc")
    if any(cohort.raw["cohort_as_of_utc"] > selection_as_of for cohort in cohorts):
        raise ContractError("model_oos_selection_cohort_after_asof")
    reservation = validate_historical_holdout_reservation(
        pre_registered_mask_receipt
    )
    access = validate_historical_holdout_access(holdout_label_access_receipt)
    if (
        access["reservation_content_sha256"] != reservation["content_sha256"]
        or reservation["registered_at_utc"] > access["holdout_unsealed_at_utc"]
        or access["first_evaluated_at_utc"] > selection_as_of
    ):
        raise ContractError("model_oos_selection_holdout_access_chain_invalid")
    # The reservation fixes the candidate mask before labels are opened, but a
    # calibration that has already lost to its own raw baseline is not an
    # admissible component.  Otherwise an unrelated weighting comparison can
    # hide a harmful bias correction (the KATL V1 failure mode).
    if any(item.raw["evaluation_state"] != "OOS_PASS" for item in cohorts):
        raise ContractError("model_oos_selection_component_calibration_rejected")
    scope_fields = (
        "station_id", "settlement_timezone", "lead_bucket", "lead_bucket_scope",
        "run_cycle", "run_cycle_scope", "season_bin", "source_class", "authority_scope",
        "event_oos_protocol_version", "calibration_method_version",
    )
    if any(any(item.raw[field] != cohorts[0].raw[field] for field in scope_fields) for item in cohorts[1:]):
        raise ContractError("model_oos_selection_cohort_scope_mismatch")
    if any(
        reservation[field] != cohorts[0].raw[field]
        for field in ("station_id", "settlement_timezone")
    ):
        raise ContractError("model_oos_selection_reservation_scope_mismatch")
    components: list[dict[str, Any]] = []
    truth_vectors: list[list[dict[str, Any]]] = []
    date_vectors: list[list[str]] = []
    component_events: list[list[CanonicalNativeEnsembleOOSEventFact]] = []
    for cohort, reference in zip(cohorts, references):
        event_hashes = [
            *cohort.raw["train_event_content_sha256s"],
            *cohort.raw["holdout_event_content_sha256s"],
        ]
        events = [
            _load_native_event(conn, sha)
            for sha in event_hashes
        ]
        event_scope_fields = (
            "station_id", "model_id", "ensemble_system_id", "model_family",
            "provider_id", "provider_product_id", "independence_family_id",
            "source_class", "authority_scope", "settlement_timezone",
            "lead_bucket", "lead_bucket_scope", "run_cycle", "run_cycle_scope",
            "event_oos_protocol_version",
        )
        if any(
            any(event.raw[field] != cohort.raw[field] for field in event_scope_fields)
            for event in events
        ):
            raise ContractError("model_oos_selection_native_cohort_event_scope_mismatch")
        _assert_native_cohort_derivation(cohort, events)
        if _native_event_input_commitments(events) != _reservation_component_for_identity(
            reservation, cohort.raw
        )["event_input_commitments"]:
            raise ContractError("model_oos_selection_reservation_input_mismatch")
        component_events.append(events)
        date_vectors.append([event.raw["event_date"] for event in events])
        truth_vectors.append([{
            "station_id": event.raw["station_id"],
            "event_date": event.raw["event_date"],
            "settlement_timezone": event.raw["settlement_timezone"],
            "truth_value_c": event.raw["truth_value_c"],
            "station_day_end_utc": event.raw["station_day_end_utc"],
            "truth_canonical_committed_at_utc": event.raw[
                "truth_canonical_committed_at_utc"
            ],
            "canonical_truth_fact_content_sha256": event.raw[
                "canonical_truth_fact_content_sha256"
            ],
            "truth_id": event.raw["truth_id"],
            "truth_revision_no": event.raw["truth_revision_no"],
            "truth_content_sha256": event.raw["truth_content_sha256"],
        } for event in events])
        components.append({
            "model_id": cohort.raw["model_id"],
            "ensemble_system_id": cohort.raw["ensemble_system_id"],
            "model_family": cohort.raw["model_family"],
            "provider_id": cohort.raw["provider_id"],
            "provider_product_id": cohort.raw["provider_product_id"],
            "independence_family_id": cohort.raw["independence_family_id"],
            "source_class": cohort.raw["source_class"],
            "event_fact_type": reference["event_fact_type"],
            "cohort_fact_type": reference["cohort_fact_type"],
            "cohort_content_sha256": cohort.content_sha256,
            "train_event_set_sha256": cohort.raw["train_event_set_sha256"],
            "holdout_event_set_sha256": cohort.raw["holdout_event_set_sha256"],
            "selection_train_rmse_c": format(Decimal(str(cohort.raw["selection_train_metrics"]["rmse_c"])), "f"),
        })
    if any(value != date_vectors[0] for value in date_vectors[1:]) or any(
        value != truth_vectors[0] for value in truth_vectors[1:]
    ):
        raise ContractError("model_oos_selection_holdout_truth_vector_mismatch")
    ordered_components = sorted(
        zip(components, cohorts, component_events),
        key=lambda item: tuple(
            str(item[0][key] or "") for key in _component_identity(item[0])
        ),
    )
    components = [item[0] for item in ordered_components]
    cohorts = [item[1] for item in ordered_components]
    component_events = [item[2] for item in ordered_components]
    assert_formal_p0_native_ensemble_systems(components)
    weights = derive_weights([Decimal(item["selection_train_rmse_c"]) for item in components])
    for item, weight in zip(components, weights):
        item["weight"] = weight
    calibrated_selected_errors: list[Decimal] = []
    calibrated_uniform_errors: list[Decimal] = []
    uncalibrated_selected_errors: list[Decimal] = []
    for holdout_index in range(20, 28):
        uncalibrated = [
            Decimal(str(events[holdout_index].raw["forecast_value_c"]))
            for events in component_events
        ]
        calibrated = [
            Decimal(str(events[holdout_index].raw["forecast_value_c"]))
            + Decimal(str(cohort.raw["calibration_parameters"]["bias_c"]))
            for cohort, events in zip(cohorts, component_events)
        ]
        truth = Decimal(str(component_events[0][holdout_index].raw["truth_value_c"]))
        selected = sum(
            Decimal(weight) * value for weight, value in zip(weights, calibrated)
        )
        calibrated_uniform = sum(calibrated) / Decimal(len(calibrated))
        uncalibrated_selected = sum(
            Decimal(weight) * value
            for weight, value in zip(weights, uncalibrated)
        )
        calibrated_selected_errors.append(selected - truth)
        calibrated_uniform_errors.append(calibrated_uniform - truth)
        uncalibrated_selected_errors.append(uncalibrated_selected - truth)
    calibrated_selected_metrics = _metrics(calibrated_selected_errors)
    calibrated_uniform_metrics = _metrics(calibrated_uniform_errors)
    uncalibrated_selected_metrics = _metrics(uncalibrated_selected_errors)
    if (
        Decimal(calibrated_selected_metrics["mae_c"])
        > Decimal(calibrated_uniform_metrics["mae_c"])
    ):
        raise ContractError("model_oos_selection_holdout_rejected")
    # Formal P0 exists to consume a calibrated forecast.  Equality is not a
    # calibration gain: accepting it lets a bias adjustment alter the error
    # shape (and potentially worsen RMSE) while hiding behind an unchanged
    # MAE.  The frozen holdout must therefore show a *strict* improvement over
    # the identical weighted raw component vector.
    if (
        Decimal(calibrated_selected_metrics["mae_c"])
        >= Decimal(uncalibrated_selected_metrics["mae_c"])
    ):
        raise ContractError("model_oos_selection_calibration_holdout_rejected")
    identities = [_component_identity(item) for item in components]
    matched: list[dict[str, Any]] = []
    for cohort in cohorts:
        commitment = _reservation_component_for_identity(reservation, cohort.raw)
        if (
            commitment["event_fact_type"] != NATIVE_ENSEMBLE_EVENT_FACT_TYPE
            or commitment["event_fact_type"] != next(
                item["event_fact_type"]
                for item in components
                if _component_identity(item) == _component_identity(cohort.raw)
            )
            or commitment["event_input_commitments"]
            != _native_event_input_commitments(component_events[
                cohorts.index(cohort)
            ])
        ):
            raise ContractError("model_oos_selection_reservation_event_mismatch")
        matched.append(commitment)
    if len(matched) != len(reservation["component_input_commitments"]):
        raise ContractError("model_oos_selection_reservation_exact_mask_required")
    base = cohorts[0].raw
    fact_identity = {
        **{field: base[field] for field in scope_fields},
        "component_cohort_refs": components,
        "component_cohort_set_sha256": content_sha256([{
            "event_fact_type": item["event_fact_type"],
            "cohort_fact_type": item["cohort_fact_type"],
            "cohort_content_sha256": item["cohort_content_sha256"],
        } for item in components]),
        "available_model_set_hash": content_sha256(identities),
        "independence_family_set_hash": content_sha256(sorted({item["independence_family_id"] for item in components})),
        "train_event_dates": date_vectors[0][:20],
        "holdout_event_dates": date_vectors[0][20:],
        "train_truth_vector_sha256": content_sha256(truth_vectors[0][:20]),
        "holdout_truth_vector_sha256": content_sha256(truth_vectors[0][20:]),
        "cohort_truth_vector_sha256": content_sha256(truth_vectors[0]),
        "selection_method_version": SELECTION_METHOD_VERSION,
        "weight_rmse_floor_c": format(RMSE_FLOOR, "f"),
        "weight_quantum": format(WEIGHT_QUANTUM, "f"),
        "weight_rounding": "ROUND_HALF_EVEN",
        "selection_as_of_utc": selection_as_of,
        "pre_registered_mask_receipt": dict(pre_registered_mask_receipt),
        "pre_registered_mask_receipt_sha256": pre_registered_mask_receipt.get(
            "content_sha256"
        ),
        "holdout_label_access_receipt": dict(holdout_label_access_receipt),
        "holdout_label_access_receipt_sha256": holdout_label_access_receipt.get(
            "content_sha256"
        ),
        "selection_holdout_metrics": calibrated_selected_metrics,
        "uniform_holdout_metrics": calibrated_uniform_metrics,
        "selection_evaluation_state": "OOS_PASS",
        "state": STATE,
    }
    fact = {**fact_identity, "content_sha256": content_sha256(fact_identity)}
    contract_identity = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": as_of_utc,
        "builder_id": "canonical.model_oos_selection.v2",
        "selection_facts": [fact],
    }
    contract = {**contract_identity, "content_sha256": content_sha256(contract_identity)}
    CanonicalModelOOSSelectionContract.from_mapping(contract)
    return contract


def assert_model_oos_selection_rebuild(
    conn: sqlite3.Connection, selection: CanonicalModelOOSSelectionFact,
) -> None:
    """Fail closed when a persisted selection no longer passes its builder."""

    rebuilt = build_model_oos_selection_contract(
        conn,
        component_cohort_refs=[
            {
                "event_fact_type": item["event_fact_type"],
                "cohort_fact_type": item["cohort_fact_type"],
                "cohort_content_sha256": item["cohort_content_sha256"],
            }
            for item in selection.raw["component_cohort_refs"]
        ],
        pre_registered_mask_receipt=selection.raw[
            "pre_registered_mask_receipt"
        ],
        holdout_label_access_receipt=selection.raw[
            "holdout_label_access_receipt"
        ],
        as_of_utc=selection.raw["selection_as_of_utc"],
    )
    rebuilt_fact = CanonicalModelOOSSelectionContract.from_mapping(
        rebuilt
    ).selection_facts[0]
    if rebuilt_fact.raw != selection.raw:
        raise ContractError("model_oos_selection_reference_rebuild_mismatch")


__all__ = [
    "CONTRACT_SCHEMA", "FACT_TYPE", "CanonicalModelOOSSelectionContract",
    "CanonicalModelOOSSelectionFact", "assert_model_oos_selection_rebuild",
    "build_model_oos_selection_contract",
    "MASK_SCHEMA", "HOLDOUT_ACCESS_SCHEMA",
    "build_historical_holdout_reservation",
    "build_historical_holdout_access_receipt",
    "validate_historical_holdout_reservation",
    "validate_historical_holdout_access",
    "TERMINAL_RECEIPT_SCHEMA",
    "build_model_oos_selection_terminal_receipt",
    "validate_model_oos_selection_terminal_receipt",
]
