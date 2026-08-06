"""Canonical train-fitted residual distribution for the successor P0 chain."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import json
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .model_oos_selection import (
    COMPONENT_FIELDS,
    CanonicalModelOOSSelectionFact,
    assert_model_oos_selection_rebuild,
)
from .model_product_identity import assert_formal_p0_native_ensemble_systems
from .native_ensemble_oos_contract import (
    COHORT_FACT_TYPE as NATIVE_ENSEMBLE_COHORT_FACT_TYPE,
    EVENT_FACT_TYPE as NATIVE_ENSEMBLE_EVENT_FACT_TYPE,
    CanonicalNativeEnsembleOOSCohortFact,
    CanonicalNativeEnsembleOOSEventFact,
)


CONTRACT_SCHEMA = "bayes_basket_target_canonical_p0_calibrated_distribution_contract_v2"
FACT_TYPE = "p0_calibrated_distribution"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
STATE = "MATERIALIZED_UNPUBLISHED"
METHOD_VERSION = "EMPIRICAL_TRAIN_RESIDUAL_CDF_V1"
VALUE_QUANTUM = Decimal("0.000000000001")
PROBABILITY_QUANTUM = Decimal("0.000000000001")

SCENARIO_FIELDS = {"event_date", "residual_c", "probability"}
METRIC_FIELDS = {"mean_crps_c", "sample_count"}
FACT_FIELDS = {
    "station_id", "settlement_timezone", "lead_bucket", "lead_bucket_scope",
    "run_cycle", "run_cycle_scope", "season_bin", "source_class",
    "event_oos_protocol_version", "calibration_method_version",
    "authority_scope", "selection_content_sha256",
    "component_cohort_refs", "component_cohort_set_sha256", "available_model_set_hash",
    "independence_family_set_hash", "train_truth_vector_sha256",
    "holdout_truth_vector_sha256", "cohort_truth_vector_sha256",
    "distribution_method_version", "residual_value_quantum_c",
    "probability_quantum", "rounding", "train_residual_scenarios",
    "train_residual_scenario_set_sha256", "holdout_distribution_metrics",
    "baseline_holdout_metrics", "distribution_evaluation_state",
    "distribution_as_of_utc", "state", "content_sha256",
}


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"p0_calibrated_distribution_{label}_missing")
    return result


def _sha(value: Any, label: str) -> str:
    result = _text(value, label).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"p0_calibrated_distribution_{label}_invalid")
    return result


def _utc(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"p0_calibrated_distribution_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"p0_calibrated_distribution_{label}_timezone_required")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _decimal(value: Any, label: str, *, nonnegative: bool = False) -> Decimal:
    try:
        result = Decimal(_text(value, label))
    except InvalidOperation as exc:
        raise ContractError(f"p0_calibrated_distribution_{label}_invalid") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise ContractError(f"p0_calibrated_distribution_{label}_invalid")
    return result


def _value_text(value: Decimal) -> str:
    return format(value.quantize(VALUE_QUANTUM, rounding=ROUND_HALF_EVEN), ".12f")


def _metric(errors: Sequence[Decimal], scenarios: Sequence[Decimal]) -> dict[str, Any]:
    if len(errors) != 8 or len(scenarios) != 20:
        raise ContractError("p0_calibrated_distribution_metric_sample_count_invalid")
    pair_term = sum(abs(left - right) for left in scenarios for right in scenarios)
    pair_term /= Decimal(2 * len(scenarios) * len(scenarios))
    scores = [
        sum(abs(item - observed) for item in scenarios) / Decimal(len(scenarios)) - pair_term
        for observed in errors
    ]
    return {"mean_crps_c": _value_text(sum(scores) / Decimal(len(scores))), "sample_count": 8}


def _validate_metric(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != METRIC_FIELDS or value["sample_count"] != 8:
        raise ContractError(f"p0_calibrated_distribution_{label}_invalid")
    return {
        "mean_crps_c": _value_text(_decimal(value["mean_crps_c"], label, nonnegative=True)),
        "sample_count": 8,
    }


def _formal_native_component_cohort_refs(value: Any) -> list[dict[str, Any]]:
    """Validate the durable typed lineage inherited from frozen selection."""

    if not isinstance(value, list) or len(value) < 2:
        raise ContractError("p0_calibrated_distribution_component_refs_invalid")
    components: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != COMPONENT_FIELDS:
            raise ContractError("p0_calibrated_distribution_component_ref_fields_invalid")
        component = dict(raw)
        if (
            component["event_fact_type"] != NATIVE_ENSEMBLE_EVENT_FACT_TYPE
            or component["cohort_fact_type"] != NATIVE_ENSEMBLE_COHORT_FACT_TYPE
        ):
            raise ContractError(
                "p0_calibrated_distribution_formal_native_typed_refs_required"
            )
        for field in (
            "model_id", "model_family", "provider_id", "provider_product_id",
            "independence_family_id", "source_class",
        ):
            component[field] = _text(component[field], f"component_{field}")
        component["model_id"] = component["model_id"].upper()
        component["ensemble_system_id"] = _text(
            component["ensemble_system_id"], "component_ensemble_system_id"
        )
        for field in (
            "cohort_content_sha256", "train_event_set_sha256",
            "holdout_event_set_sha256",
        ):
            component[field] = _sha(component[field], f"component_{field}")
        component["selection_train_rmse_c"] = format(
            _decimal(
                component["selection_train_rmse_c"], "component_train_rmse",
                nonnegative=True,
            ),
            "f",
        )
        component["weight"] = _value_text(
            _decimal(component["weight"], "component_weight", nonnegative=True)
        )
        if Decimal(component["weight"]) <= 0:
            raise ContractError("p0_calibrated_distribution_positive_weight_required")
        components.append(component)
    identities = [{key: item[key] for key in (
        "model_id", "ensemble_system_id", "model_family", "provider_id",
        "provider_product_id", "independence_family_id", "source_class",
    )} for item in components]
    if identities != sorted(
        identities, key=lambda item: tuple(str(item[key] or "") for key in item)
    ) or len({item["ensemble_system_id"] for item in identities}) != len(identities):
        raise ContractError("p0_calibrated_distribution_component_order_invalid")
    assert_formal_p0_native_ensemble_systems(identities)
    return components


@dataclass(frozen=True)
class CanonicalP0CalibratedDistributionFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalP0CalibratedDistributionFact":
        if set(raw) != FACT_FIELDS:
            raise ContractError("p0_calibrated_distribution_fact_fields_mismatch")
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
            raise ContractError("p0_calibrated_distribution_authority_state_invalid")
        if value["distribution_method_version"] != METHOD_VERSION:
            raise ContractError("p0_calibrated_distribution_method_invalid")
        if (
            value["residual_value_quantum_c"] != format(VALUE_QUANTUM, "f")
            or value["probability_quantum"] != format(PROBABILITY_QUANTUM, "f")
            or value["rounding"] != "ROUND_HALF_EVEN"
        ):
            raise ContractError("p0_calibrated_distribution_numeric_contract_invalid")
        for field in (
            "selection_content_sha256", "component_cohort_set_sha256",
            "available_model_set_hash", "independence_family_set_hash",
            "train_truth_vector_sha256", "holdout_truth_vector_sha256",
            "cohort_truth_vector_sha256", "train_residual_scenario_set_sha256",
        ):
            value[field] = _sha(value[field], field)
        components = _formal_native_component_cohort_refs(
            value["component_cohort_refs"]
        )
        typed_cohort_refs = [{
            "event_fact_type": item["event_fact_type"],
            "cohort_fact_type": item["cohort_fact_type"],
            "cohort_content_sha256": item["cohort_content_sha256"],
        } for item in components]
        if value["component_cohort_set_sha256"] != content_sha256(
            typed_cohort_refs
        ):
            raise ContractError("p0_calibrated_distribution_cohort_set_hash_mismatch")
        identities = [{key: item[key] for key in (
            "model_id", "ensemble_system_id", "model_family", "provider_id",
            "provider_product_id", "independence_family_id", "source_class",
        )} for item in components]
        if value["available_model_set_hash"] != content_sha256(identities):
            raise ContractError("p0_calibrated_distribution_available_set_hash_mismatch")
        families = sorted({item["independence_family_id"] for item in components})
        if value["independence_family_set_hash"] != content_sha256(families):
            raise ContractError("p0_calibrated_distribution_family_set_hash_mismatch")
        value["component_cohort_refs"] = components
        scenarios_raw = value["train_residual_scenarios"]
        if not isinstance(scenarios_raw, list) or len(scenarios_raw) != 20:
            raise ContractError("p0_calibrated_distribution_train_scenarios_invalid")
        scenarios: list[dict[str, str]] = []
        for item in scenarios_raw:
            if not isinstance(item, Mapping) or set(item) != SCENARIO_FIELDS:
                raise ContractError("p0_calibrated_distribution_scenario_fields_mismatch")
            scenarios.append({
                "event_date": _text(item["event_date"], "scenario_event_date"),
                "residual_c": _value_text(_decimal(item["residual_c"], "scenario_residual_c")),
                "probability": _value_text(_decimal(item["probability"], "scenario_probability", nonnegative=True)),
            })
        if [item["event_date"] for item in scenarios] != sorted({item["event_date"] for item in scenarios}):
            raise ContractError("p0_calibrated_distribution_scenario_dates_invalid")
        if sum(Decimal(item["probability"]) for item in scenarios) != Decimal("1"):
            raise ContractError("p0_calibrated_distribution_probability_sum_invalid")
        if any(Decimal(item["probability"]) != Decimal("0.05") for item in scenarios):
            raise ContractError("p0_calibrated_distribution_probability_weight_invalid")
        if value["train_residual_scenario_set_sha256"] != content_sha256(scenarios):
            raise ContractError("p0_calibrated_distribution_scenario_set_hash_mismatch")
        value["train_residual_scenarios"] = scenarios
        value["holdout_distribution_metrics"] = _validate_metric(
            value["holdout_distribution_metrics"], "holdout_metrics"
        )
        value["baseline_holdout_metrics"] = _validate_metric(
            value["baseline_holdout_metrics"], "baseline_holdout_metrics"
        )
        if (
            value["distribution_evaluation_state"] != "OOS_PASS"
            or Decimal(value["holdout_distribution_metrics"]["mean_crps_c"])
            > Decimal(value["baseline_holdout_metrics"]["mean_crps_c"])
        ):
            raise ContractError("p0_calibrated_distribution_holdout_evaluation_invalid")
        value["distribution_as_of_utc"] = _utc(value["distribution_as_of_utc"], "distribution_as_of_utc")
        supplied = _sha(value["content_sha256"], "content_sha256")
        if supplied != content_sha256({key: item for key, item in value.items() if key != "content_sha256"}):
            raise ContractError("p0_calibrated_distribution_content_sha256_mismatch")
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
class CanonicalP0CalibratedDistributionContract:
    as_of_utc: str
    builder_id: str
    distribution_facts: tuple[CanonicalP0CalibratedDistributionFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalP0CalibratedDistributionContract":
        if set(raw) != {"schema", "as_of_utc", "builder_id", "distribution_facts", "content_sha256"}:
            raise ContractError("p0_calibrated_distribution_contract_fields_mismatch")
        if raw["schema"] != CONTRACT_SCHEMA:
            raise ContractError("p0_calibrated_distribution_contract_schema_invalid")
        facts_raw = raw["distribution_facts"]
        if not isinstance(facts_raw, list) or not facts_raw:
            raise ContractError("p0_calibrated_distribution_fact_required")
        facts = tuple(CanonicalP0CalibratedDistributionFact.from_mapping(item) for item in facts_raw)
        if len({item.scope_key for item in facts}) != len(facts):
            raise ContractError("p0_calibrated_distribution_scope_duplicate")
        as_of = _utc(raw["as_of_utc"], "contract_as_of_utc")
        if any(item.raw["distribution_as_of_utc"] > as_of for item in facts):
            raise ContractError("p0_calibrated_distribution_fact_after_contract_asof")
        supplied = _sha(raw["content_sha256"], "contract_content_sha256")
        if supplied != content_sha256({key: item for key, item in raw.items() if key != "content_sha256"}):
            raise ContractError("p0_calibrated_distribution_contract_content_sha256_mismatch")
        return cls(as_of, _text(raw["builder_id"], "builder_id"), facts, supplied)


def _load(conn: sqlite3.Connection, fact_type: str, sha: str, cls: Any) -> Any:
    rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact WHERE fact_type=? AND content_sha256=?",
        (fact_type, _sha(sha, f"{fact_type}_sha256")),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError(f"p0_calibrated_distribution_{fact_type}_not_unique")
    return cls.from_mapping(json.loads(str(rows[0][0])))


def _load_native_cohort(
    conn: sqlite3.Connection, sha: str,
) -> CanonicalNativeEnsembleOOSCohortFact:
    return _load(
        conn,
        NATIVE_ENSEMBLE_COHORT_FACT_TYPE,
        sha,
        CanonicalNativeEnsembleOOSCohortFact,
    )


def _load_native_event(
    conn: sqlite3.Connection, sha: str,
) -> CanonicalNativeEnsembleOOSEventFact:
    return _load(
        conn,
        NATIVE_ENSEMBLE_EVENT_FACT_TYPE,
        sha,
        CanonicalNativeEnsembleOOSEventFact,
    )


def build_p0_calibrated_distribution_contract(
    conn: sqlite3.Connection, *, selection_content_sha256: str, as_of_utc: str
) -> dict[str, Any]:
    selection = _load(
        conn, "p0_model_family_selection", selection_content_sha256,
        CanonicalModelOOSSelectionFact,
    )
    as_of = _utc(as_of_utc, "distribution_as_of_utc")
    if selection.raw["selection_as_of_utc"] > as_of:
        raise ContractError("p0_calibrated_distribution_selection_after_asof")
    assert_model_oos_selection_rebuild(conn, selection)
    components = selection.raw["component_cohort_refs"]
    cohorts = [
        _load_native_cohort(conn, item["cohort_content_sha256"])
        for item in components
    ]
    component_events: list[list[CanonicalNativeEnsembleOOSEventFact]] = []
    for cohort in cohorts:
        component_events.append([
            _load_native_event(conn, sha)
            for sha in (*cohort.raw["train_event_content_sha256s"], *cohort.raw["holdout_event_content_sha256s"])
        ])
    weights = [Decimal(item["weight"]) for item in components]
    selected_residuals: list[Decimal] = []
    for index in range(28):
        point = sum(
            weight * (
                Decimal(str(events[index].raw["forecast_value_c"]))
                + Decimal(str(cohort.raw["calibration_parameters"]["bias_c"]))
            )
            for weight, cohort, events in zip(weights, cohorts, component_events)
        )
        truth = Decimal(str(component_events[0][index].raw["truth_value_c"]))
        selected_residuals.append(truth - point)
    scenarios = [{
        "event_date": selection.raw["train_event_dates"][index],
        "residual_c": _value_text(selected_residuals[index]),
        "probability": "0.050000000000",
    } for index in range(20)]
    holdout_errors = selected_residuals[20:]
    trained_metrics = _metric(holdout_errors, selected_residuals[:20])
    # The durable V2 field name is kept for hash compatibility.  This compares
    # the train-residual CDF with a zero-residual point distribution; it is not
    # the raw-model baseline, which is enforced by model_oos_selection.
    baseline_metrics = _metric(holdout_errors, [Decimal(0)] * 20)
    if (
        Decimal(trained_metrics["mean_crps_c"])
        > Decimal(baseline_metrics["mean_crps_c"])
    ):
        raise ContractError("p0_calibrated_distribution_holdout_rejected")
    inherited = (
        "station_id", "settlement_timezone", "lead_bucket", "lead_bucket_scope",
        "run_cycle", "run_cycle_scope", "season_bin", "source_class",
        "event_oos_protocol_version", "calibration_method_version", "authority_scope",
        "component_cohort_set_sha256", "available_model_set_hash",
        "independence_family_set_hash", "train_truth_vector_sha256",
        "holdout_truth_vector_sha256", "cohort_truth_vector_sha256",
    )
    identity = {
        **{field: selection.raw[field] for field in inherited},
        "selection_content_sha256": selection.content_sha256,
        "component_cohort_refs": components,
        "distribution_method_version": METHOD_VERSION,
        "residual_value_quantum_c": format(VALUE_QUANTUM, "f"),
        "probability_quantum": format(PROBABILITY_QUANTUM, "f"),
        "rounding": "ROUND_HALF_EVEN",
        "train_residual_scenarios": scenarios,
        "train_residual_scenario_set_sha256": content_sha256(scenarios),
        "holdout_distribution_metrics": trained_metrics,
        "baseline_holdout_metrics": baseline_metrics,
        "distribution_evaluation_state": "OOS_PASS",
        "distribution_as_of_utc": as_of,
        "state": STATE,
    }
    fact = {**identity, "content_sha256": content_sha256(identity)}
    contract_identity = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": as_of,
        "builder_id": "canonical.p0_calibrated_distribution.v2",
        "distribution_facts": [fact],
    }
    contract = {**contract_identity, "content_sha256": content_sha256(contract_identity)}
    CanonicalP0CalibratedDistributionContract.from_mapping(contract)
    return contract


__all__ = [
    "CONTRACT_SCHEMA", "FACT_TYPE", "CanonicalP0CalibratedDistributionContract",
    "CanonicalP0CalibratedDistributionFact", "build_p0_calibrated_distribution_contract",
]
