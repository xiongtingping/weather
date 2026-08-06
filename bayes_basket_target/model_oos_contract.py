"""Canonical V13 model OOS event/cohort contract.

The contract binds an exact historical model input to the V12 AWC
observation/day-bounds truth contract.  It intentionally has no adapter from
the retired ``truth`` or ``republisher_oos_artifact`` schemas.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .model_family_registry import assert_canonical_independence_family
from .native_daily_max_manifest import expected_et_hourly_slots


CONTRACT_SCHEMA = "bayes_basket_target_canonical_model_oos_contract_v4"
MODEL_RECEIPT_SCHEMA = "bayes_basket_target_canonical_model_input_receipt_v1"
HISTORICAL_MODEL_RECEIPT_SCHEMA = (
    "bayes_basket_target_canonical_historical_republisher_input_receipt_v1"
)
EVENT_FACT_TYPE = "model_oos_event"
COHORT_FACT_TYPE = "model_oos_cohort"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
EVENT_STATE = "MATERIALIZED_UNPUBLISHED"
COHORT_STATES = frozenset({"MATERIALIZED_UNPUBLISHED", "CANDIDATE_ONLY"})
PARTITIONS = ("train", "holdout")
CALIBRATION_METHOD_VERSION = "ADDITIVE_BIAS_V1"


def _strict(raw: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(raw) != fields:
        raise ContractError(f"model_oos_{label}_fields_mismatch")


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"model_oos_{label}_missing")
    return result


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _sha(value: Any, label: str) -> str:
    result = _text(value, label).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"model_oos_{label}_invalid")
    return result


def _utc(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"model_oos_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"model_oos_{label}_timezone_required")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _optional_utc(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _utc(value, label)


def _event_date(value: Any) -> str:
    text = _text(value, "event_date")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ContractError("model_oos_event_date_invalid") from exc


def _timezone(value: Any) -> str:
    text = _text(value, "settlement_timezone")
    try:
        ZoneInfo(text)
    except Exception as exc:
        raise ContractError("model_oos_settlement_timezone_invalid") from exc
    return text


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"model_oos_{label}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"model_oos_{label}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"model_oos_{label}_invalid")
    return result


def _hash_vector(value: Any, label: str, *, size: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) != size:
        raise ContractError(f"model_oos_{label}_size_invalid")
    result = tuple(_sha(item, f"{label}_item") for item in value)
    if len(set(result)) != len(result):
        raise ContractError(f"model_oos_{label}_duplicate")
    return result


EVENT_FIELDS = {
    "station_id", "model_id", "ensemble_system_id", "model_family",
    "provider_id", "provider_product_id", "independence_family_id",
    "source_class", "authority_scope", "event_date", "settlement_timezone",
    "lead_bucket", "lead_bucket_scope", "run_init_utc", "run_cycle",
    "run_cycle_scope", "forecast_decision_at_utc", "model_available_at_utc",
    "forecast_value_c", "raw_object_sha256", "model_input_content_sha256",
    "model_receipt", "model_receipt_sha256", "canonical_truth_fact_content_sha256",
    "truth_id", "truth_revision_no", "truth_content_sha256",
    "truth_value_c", "station_day_end_utc",
    "truth_canonical_committed_at_utc", "event_oos_protocol_version",
    "state", "content_sha256",
}

MODEL_RECEIPT_FIELDS = {
    "schema", "station_id", "model_id", "provider_id", "provider_product_id",
    "source_class", "event_date", "model_available_at_utc",
    "forecast_decision_at_utc", "retrieved_at_utc", "availability_clock_source",
    "raw_object_sha256", "model_input_content_sha256", "content_sha256",
}

AVAILABILITY_CLOCK_SOURCES = frozenset({
    "OFFICIAL_PUBLISHED_AT",
    "RETAINED_INGRESS_RECEIVED_AT",
    "REPUBLISHER_PROSPECTIVE_RECEIVED_AT",
    "REPUBLISHER_HISTORICAL_FIXED_LEAD",
})

HISTORICAL_MODEL_RECEIPT_FIELDS = {
    "schema", "station_id", "model_id", "provider_id", "provider_product_id",
    "source_class", "event_date", "run_init_utc", "lead_bucket", "run_cycle",
    "model_available_at_utc",
    "forecast_decision_at_utc", "retrieved_at_utc",
    "availability_clock_source", "raw_object_sha256", "raw_object_ref",
    "model_input_content_sha256", "parent_evidence_sha256",
    "historical_asof_basis_sha256", "historical_asof_method",
    "fixed_lead_hours", "valid_time_vector_sha256", "source_url",
    "native_arrival_indexed", "p1_eligible", "formal_live_authority",
    "content_sha256",
}


def historical_asof_basis_sha256(raw: Mapping[str, Any]) -> str:
    return content_sha256({
        "station_id": raw["station_id"],
        "model_id": raw["model_id"],
        "provider_id": raw["provider_id"],
        "provider_product_id": raw["provider_product_id"],
        "event_date": raw["event_date"],
        "run_init_utc": raw["run_init_utc"],
        "lead_bucket": raw["lead_bucket"],
        "run_cycle": raw["run_cycle"],
        "raw_object_sha256": raw["raw_object_sha256"],
        "raw_object_ref": raw["raw_object_ref"],
        "model_input_content_sha256": raw["model_input_content_sha256"],
        "parent_evidence_sha256": raw["parent_evidence_sha256"],
        "historical_asof_method": raw["historical_asof_method"],
        "fixed_lead_hours": raw["fixed_lead_hours"],
        "valid_time_vector_sha256": raw["valid_time_vector_sha256"],
        "source_url": raw["source_url"],
    })


def _historical_model_receipt(
    raw: Mapping[str, Any], event: Mapping[str, Any]
) -> dict[str, Any]:
    _strict(raw, HISTORICAL_MODEL_RECEIPT_FIELDS, "historical_model_receipt")
    value = dict(raw)
    if value["schema"] != HISTORICAL_MODEL_RECEIPT_SCHEMA:
        raise ContractError("model_oos_historical_model_receipt_schema_invalid")
    value["station_id"] = _text(value["station_id"], "receipt_station_id").upper()
    value["model_id"] = _text(value["model_id"], "receipt_model_id").upper()
    for field in (
        "provider_id", "provider_product_id", "source_class", "raw_object_ref",
        "lead_bucket", "run_cycle", "historical_asof_method", "source_url",
    ):
        value[field] = _text(value[field], f"receipt_{field}")
    value["event_date"] = _event_date(value["event_date"])
    if value["run_init_utc"] is not None:
        raise ContractError("model_oos_historical_model_receipt_run_init_forbidden")
    for field in (
        "model_available_at_utc", "forecast_decision_at_utc", "retrieved_at_utc",
    ):
        value[field] = _utc(value[field], f"receipt_{field}")
    for field in (
        "raw_object_sha256", "model_input_content_sha256",
        "parent_evidence_sha256", "historical_asof_basis_sha256",
        "valid_time_vector_sha256",
    ):
        value[field] = _sha(value[field], f"receipt_{field}")
    if isinstance(value["fixed_lead_hours"], bool):
        raise ContractError("model_oos_historical_model_receipt_lead_invalid")
    try:
        value["fixed_lead_hours"] = int(value["fixed_lead_hours"])
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "model_oos_historical_model_receipt_lead_invalid"
        ) from exc
    expected_slots = expected_et_hourly_slots(value["event_date"])
    valid_times = [
        datetime.fromisoformat(item.replace("Z", "+00:00"))
        for item in expected_slots
    ]
    expected_decision = (
        max(valid_times) - timedelta(hours=value["fixed_lead_hours"])
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if (
        value["source_class"] != "aggregator_republisher"
        or value["availability_clock_source"]
        != "REPUBLISHER_HISTORICAL_FIXED_LEAD"
        or value["historical_asof_method"] != "FIXED_VALID_TIME_OFFSET"
        or value["lead_bucket"] not in {"d1", "d2"}
        or value["run_cycle"]
        != ("PREVIOUS_DAY1" if value["lead_bucket"] == "d1" else "PREVIOUS_DAY2")
        or int(value["fixed_lead_hours"])
        != (24 if value["lead_bucket"] == "d1" else 48)
        or value["valid_time_vector_sha256"] != content_sha256(expected_slots)
        or value["forecast_decision_at_utc"] != expected_decision
        or value["model_available_at_utc"] != value["forecast_decision_at_utc"]
        or value["retrieved_at_utc"] < value["forecast_decision_at_utc"]
        or value["native_arrival_indexed"] is not False
        or value["p1_eligible"] is not False
        or value["formal_live_authority"] is not False
        or event["settlement_timezone"] != "America/New_York"
        or event["lead_bucket_scope"]
        != ("PREVIOUS_DAY1" if value["lead_bucket"] == "d1" else "PREVIOUS_DAY2")
        or event["run_cycle_scope"] != "REPUBLISHER_ARCHIVE_RELATIVE_DAY"
        or event["event_oos_protocol_version"]
        != "STRICT_HISTORICAL_AWC_REPUBLISHER_V13_V1"
        or value["historical_asof_basis_sha256"]
        != historical_asof_basis_sha256(value)
    ):
        raise ContractError("model_oos_historical_model_receipt_semantics_invalid")
    comparable = (
        "station_id", "model_id", "provider_id", "provider_product_id",
        "source_class", "event_date", "run_init_utc", "lead_bucket", "run_cycle",
        "model_available_at_utc",
        "forecast_decision_at_utc", "raw_object_sha256",
        "model_input_content_sha256",
    )
    if any(value[field] != event[field] for field in comparable):
        raise ContractError("model_oos_historical_model_receipt_scope_mismatch")
    supplied = _sha(value["content_sha256"], "historical_model_receipt_content_sha256")
    if supplied != content_sha256(
        {key: item for key, item in value.items() if key != "content_sha256"}
    ):
        raise ContractError("model_oos_historical_model_receipt_hash_mismatch")
    value["content_sha256"] = supplied
    return value


def _model_receipt(raw: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ContractError("model_oos_model_receipt_mapping_required")
    if raw.get("schema") == HISTORICAL_MODEL_RECEIPT_SCHEMA:
        return _historical_model_receipt(raw, event)
    _strict(raw, MODEL_RECEIPT_FIELDS, "model_receipt")
    value = dict(raw)
    if value["schema"] != MODEL_RECEIPT_SCHEMA:
        raise ContractError("model_oos_model_receipt_schema_invalid")
    value["station_id"] = _text(value["station_id"], "receipt_station_id").upper()
    value["model_id"] = _text(value["model_id"], "receipt_model_id").upper()
    for field in ("provider_id", "provider_product_id", "source_class"):
        value[field] = _text(value[field], f"receipt_{field}")
    value["event_date"] = _event_date(value["event_date"])
    for field in (
        "model_available_at_utc", "forecast_decision_at_utc", "retrieved_at_utc",
    ):
        value[field] = _utc(value[field], f"receipt_{field}")
    value["availability_clock_source"] = _text(
        value["availability_clock_source"], "availability_clock_source"
    )
    if value["availability_clock_source"] not in AVAILABILITY_CLOCK_SOURCES:
        raise ContractError("model_oos_model_receipt_clock_source_invalid")
    for field in ("raw_object_sha256", "model_input_content_sha256"):
        value[field] = _sha(value[field], f"receipt_{field}")
    comparable = (
        "station_id", "model_id", "provider_id", "provider_product_id",
        "source_class", "event_date", "model_available_at_utc",
        "forecast_decision_at_utc", "raw_object_sha256",
        "model_input_content_sha256",
    )
    if any(value[field] != event[field] for field in comparable):
        raise ContractError("model_oos_model_receipt_scope_mismatch")
    if not (
        value["model_available_at_utc"]
        <= value["retrieved_at_utc"]
        <= value["forecast_decision_at_utc"]
    ):
        raise ContractError("model_oos_model_receipt_timestamp_order_invalid")
    if (
        value["availability_clock_source"]
        in {"RETAINED_INGRESS_RECEIVED_AT", "REPUBLISHER_PROSPECTIVE_RECEIVED_AT"}
        and value["model_available_at_utc"] != value["retrieved_at_utc"]
    ):
        raise ContractError("model_oos_model_receipt_received_clock_mismatch")
    supplied = _sha(value["content_sha256"], "model_receipt_content_sha256")
    if supplied != content_sha256({k: v for k, v in value.items() if k != "content_sha256"}):
        raise ContractError("model_oos_model_receipt_content_sha256_mismatch")
    value["content_sha256"] = supplied
    return value


@dataclass(frozen=True)
class CanonicalModelOOSEventFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalModelOOSEventFact":
        _strict(raw, EVENT_FIELDS, "event")
        value = dict(raw)
        value["station_id"] = _text(value["station_id"], "station_id").upper()
        value["model_id"] = _text(value["model_id"], "model_id").upper()
        value["ensemble_system_id"] = _optional_text(
            value["ensemble_system_id"], "ensemble_system_id"
        )
        for field in (
            "model_family", "provider_id", "provider_product_id",
            "independence_family_id", "source_class", "lead_bucket",
            "lead_bucket_scope", "run_cycle_scope", "event_oos_protocol_version",
        ):
            value[field] = _text(value[field], field)
        assert_canonical_independence_family(**value)
        value["run_cycle"] = _optional_text(value["run_cycle"], "run_cycle")
        value["run_init_utc"] = _optional_utc(value["run_init_utc"], "run_init_utc")
        if value["authority_scope"] != AUTHORITY_SCOPE or value["state"] != EVENT_STATE:
            raise ContractError("model_oos_event_authority_state_invalid")
        value["event_date"] = _event_date(value["event_date"])
        value["settlement_timezone"] = _timezone(value["settlement_timezone"])
        for field in (
            "forecast_decision_at_utc", "model_available_at_utc",
            "station_day_end_utc", "truth_canonical_committed_at_utc",
        ):
            value[field] = _utc(value[field], field)
        if not (
            value["model_available_at_utc"] <= value["forecast_decision_at_utc"]
            < value["station_day_end_utc"]
            <= value["truth_canonical_committed_at_utc"]
        ):
            raise ContractError("model_oos_event_strict_asof_order_invalid")
        value["forecast_value_c"] = _number(value["forecast_value_c"], "forecast_value_c")
        value["truth_value_c"] = _number(value["truth_value_c"], "truth_value_c")
        value["truth_id"] = _text(value["truth_id"], "truth_id")
        if isinstance(value["truth_revision_no"], bool):
            raise ContractError("model_oos_truth_revision_no_invalid")
        try:
            value["truth_revision_no"] = int(value["truth_revision_no"])
        except (TypeError, ValueError) as exc:
            raise ContractError("model_oos_truth_revision_no_invalid") from exc
        if value["truth_revision_no"] < 0:
            raise ContractError("model_oos_truth_revision_no_invalid")
        for field in (
            "raw_object_sha256", "model_input_content_sha256",
            "model_receipt_sha256", "canonical_truth_fact_content_sha256",
            "truth_content_sha256",
        ):
            value[field] = _sha(value[field], field)
        value["model_receipt"] = _model_receipt(value["model_receipt"], value)
        if value["model_receipt"]["content_sha256"] != value["model_receipt_sha256"]:
            raise ContractError("model_oos_model_receipt_sha256_mismatch")
        supplied = _sha(value["content_sha256"], "event_content_sha256")
        identity = {key: item for key, item in value.items() if key != "content_sha256"}
        if supplied != content_sha256(identity):
            raise ContractError("model_oos_event_content_sha256_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        values = (
            self.raw["station_id"], self.raw["model_id"],
            self.raw["ensemble_system_id"] or "null", self.raw["model_family"],
            self.raw["provider_id"], self.raw["provider_product_id"],
            self.raw["independence_family_id"], self.raw["source_class"],
            self.raw["authority_scope"], self.raw["settlement_timezone"],
            self.raw["run_cycle"] or "null", self.raw["run_init_utc"] or "null",
            self.raw["run_cycle_scope"], self.raw["lead_bucket"],
            self.raw["lead_bucket_scope"], self.raw["event_oos_protocol_version"],
            self.raw["event_date"],
        )
        return "|".join(str(item) for item in values)


COHORT_FIELDS = {
    "station_id", "model_id", "ensemble_system_id", "model_family",
    "provider_id", "provider_product_id", "independence_family_id",
    "source_class", "authority_scope", "settlement_timezone", "lead_bucket",
    "lead_bucket_scope", "run_cycle", "run_cycle_scope", "season_bin",
    "event_oos_protocol_version", "calibration_method_version",
    "cohort_as_of_utc", "train_event_content_sha256s",
    "holdout_event_content_sha256s", "train_event_set_sha256",
    "holdout_event_set_sha256", "cohort_event_vector_sha256",
    "calibration_parameters", "selection_train_metrics", "holdout_metrics",
    "baseline_holdout_metrics",
    "evaluation_state", "state", "content_sha256",
}


def _number_map(raw: Any, label: str) -> dict[str, float]:
    if not isinstance(raw, Mapping) or not raw:
        raise ContractError(f"model_oos_{label}_mapping_required")
    return {str(key): _number(item, f"{label}_{key}") for key, item in sorted(raw.items())}


def _stable(value: float) -> float:
    return round(float(value), 12)


def derive_cohort_statistics(
    events: Sequence[CanonicalModelOOSEventFact],
) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float], str]:
    """Derive the only admitted V13 calibration and holdout metrics."""

    if len(events) != 28:
        raise ContractError("model_oos_cohort_event_count_invalid")
    train = events[:20]
    holdout = events[20:]
    bias = sum(
        event.raw["truth_value_c"] - event.raw["forecast_value_c"]
        for event in train
    ) / len(train)

    def metrics(rows: Sequence[CanonicalModelOOSEventFact], correction: float) -> dict[str, float]:
        errors = [
            event.raw["forecast_value_c"] + correction - event.raw["truth_value_c"]
            for event in rows
        ]
        return {
            "mae_c": _stable(sum(abs(error) for error in errors) / len(errors)),
            "mean_error_c": _stable(sum(errors) / len(errors)),
            "rmse_c": _stable(math.sqrt(sum(error * error for error in errors) / len(errors))),
            "sample_count": float(len(errors)),
        }

    parameters = {"bias_c": _stable(bias), "train_sample_count": float(len(train))}
    train_metrics = metrics(train, bias)
    calibrated = metrics(holdout, bias)
    baseline = metrics(holdout, 0.0)
    # A calibration is admissible only when the frozen holdout demonstrates
    # positive value over the identical raw forecast.  Equality is not a
    # calibration gain and must not create an OOS_PASS component that the
    # downstream formal-P0 selection is required to reject.
    evaluation_state = (
        "OOS_PASS" if calibrated["mae_c"] < baseline["mae_c"] else "OOS_REJECTED"
    )
    return parameters, train_metrics, calibrated, baseline, evaluation_state


@dataclass(frozen=True)
class CanonicalModelOOSCohortFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalModelOOSCohortFact":
        _strict(raw, COHORT_FIELDS, "cohort")
        value = dict(raw)
        value["station_id"] = _text(value["station_id"], "station_id").upper()
        value["model_id"] = _text(value["model_id"], "model_id").upper()
        value["ensemble_system_id"] = _optional_text(
            value["ensemble_system_id"], "ensemble_system_id"
        )
        for field in (
            "model_family", "provider_id", "provider_product_id",
            "independence_family_id", "source_class", "lead_bucket",
            "lead_bucket_scope", "run_cycle_scope", "season_bin",
            "event_oos_protocol_version", "calibration_method_version",
        ):
            value[field] = _text(value[field], field)
        assert_canonical_independence_family(**value)
        value["run_cycle"] = _optional_text(value["run_cycle"], "run_cycle")
        if value["calibration_method_version"] != CALIBRATION_METHOD_VERSION:
            raise ContractError("model_oos_cohort_calibration_method_invalid")
        if value["authority_scope"] != AUTHORITY_SCOPE or value["state"] not in COHORT_STATES:
            raise ContractError("model_oos_cohort_authority_state_invalid")
        if value["evaluation_state"] not in {"OOS_PASS", "OOS_REJECTED"}:
            raise ContractError("model_oos_cohort_evaluation_state_invalid")
        value["settlement_timezone"] = _timezone(value["settlement_timezone"])
        value["cohort_as_of_utc"] = _utc(value["cohort_as_of_utc"], "cohort_as_of_utc")
        train = _hash_vector(value["train_event_content_sha256s"], "train_events", size=20)
        holdout = _hash_vector(value["holdout_event_content_sha256s"], "holdout_events", size=8)
        if set(train) & set(holdout):
            raise ContractError("model_oos_cohort_event_overlap")
        value["train_event_content_sha256s"] = list(train)
        value["holdout_event_content_sha256s"] = list(holdout)
        value["train_event_set_sha256"] = _sha(value["train_event_set_sha256"], "train_event_set_sha256")
        value["holdout_event_set_sha256"] = _sha(value["holdout_event_set_sha256"], "holdout_event_set_sha256")
        value["cohort_event_vector_sha256"] = _sha(value["cohort_event_vector_sha256"], "cohort_event_vector_sha256")
        if value["train_event_set_sha256"] != content_sha256(list(train)):
            raise ContractError("model_oos_cohort_train_set_sha256_mismatch")
        if value["holdout_event_set_sha256"] != content_sha256(list(holdout)):
            raise ContractError("model_oos_cohort_holdout_set_sha256_mismatch")
        vector = [
            *({"partition": "train", "event_content_sha256": item} for item in train),
            *({"partition": "holdout", "event_content_sha256": item} for item in holdout),
        ]
        if value["cohort_event_vector_sha256"] != content_sha256(vector):
            raise ContractError("model_oos_cohort_vector_sha256_mismatch")
        for field in (
            "calibration_parameters", "selection_train_metrics", "holdout_metrics",
            "baseline_holdout_metrics",
        ):
            value[field] = _number_map(value[field], field)
        supplied = _sha(value["content_sha256"], "cohort_content_sha256")
        identity = {key: item for key, item in value.items() if key != "content_sha256"}
        if supplied != content_sha256(identity):
            raise ContractError("model_oos_cohort_content_sha256_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        values = (
            self.raw["station_id"], self.raw["model_id"],
            self.raw["ensemble_system_id"] or "null", self.raw["model_family"],
            self.raw["provider_id"], self.raw["provider_product_id"],
            self.raw["independence_family_id"], self.raw["source_class"],
            self.raw["authority_scope"],
            self.raw["settlement_timezone"], self.raw["lead_bucket"],
            self.raw["lead_bucket_scope"], self.raw["run_cycle"] or "null",
            self.raw["run_cycle_scope"], self.raw["season_bin"],
            self.raw["event_oos_protocol_version"],
            self.raw["calibration_method_version"],
            self.raw["cohort_event_vector_sha256"],
        )
        return "|".join(str(item) for item in values)


@dataclass(frozen=True)
class CanonicalModelOOSContract:
    as_of_utc: str
    builder_id: str
    event_facts: tuple[CanonicalModelOOSEventFact, ...]
    cohort_facts: tuple[CanonicalModelOOSCohortFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalModelOOSContract":
        _strict(
            raw,
            {"schema", "as_of_utc", "builder_id", "event_facts", "cohort_facts", "content_sha256"},
            "contract",
        )
        if raw["schema"] != CONTRACT_SCHEMA:
            raise ContractError("model_oos_contract_schema_invalid")
        if not isinstance(raw["event_facts"], list) or not isinstance(raw["cohort_facts"], list):
            raise ContractError("model_oos_contract_fact_lists_required")
        events = tuple(CanonicalModelOOSEventFact.from_mapping(item) for item in raw["event_facts"])
        cohorts = tuple(CanonicalModelOOSCohortFact.from_mapping(item) for item in raw["cohort_facts"])
        if not events and not cohorts:
            raise ContractError("model_oos_contract_fact_required")
        if len({item.scope_key for item in events}) != len(events) or len({item.scope_key for item in cohorts}) != len(cohorts):
            raise ContractError("model_oos_contract_scope_duplicate")
        as_of = _utc(raw["as_of_utc"], "contract_as_of_utc")
        if any(
            max(
                item.raw["model_available_at_utc"],
                item.raw["forecast_decision_at_utc"],
                item.raw["model_receipt"]["retrieved_at_utc"],
                item.raw["truth_canonical_committed_at_utc"],
            ) > as_of
            for item in events
        ) or any(
            item.raw["cohort_as_of_utc"] > as_of for item in cohorts
        ):
            raise ContractError("model_oos_contract_fact_after_asof")
        supplied = _sha(raw["content_sha256"], "contract_content_sha256")
        identity = {key: item for key, item in raw.items() if key != "content_sha256"}
        if supplied != content_sha256(identity):
            raise ContractError("model_oos_contract_content_sha256_mismatch")
        return cls(as_of, _text(raw["builder_id"], "builder_id"), events, cohorts, supplied)

    def to_mapping(self) -> dict[str, Any]:
        identity = {
            "schema": CONTRACT_SCHEMA,
            "as_of_utc": self.as_of_utc,
            "builder_id": self.builder_id,
            "event_facts": [item.raw for item in self.event_facts],
            "cohort_facts": [item.raw for item in self.cohort_facts],
        }
        return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "AUTHORITY_SCOPE", "COHORT_FACT_TYPE", "CONTRACT_SCHEMA", "EVENT_FACT_TYPE",
    "CALIBRATION_METHOD_VERSION", "MODEL_RECEIPT_SCHEMA", "derive_cohort_statistics",
    "CanonicalModelOOSCohortFact", "CanonicalModelOOSContract",
    "CanonicalModelOOSEventFact",
]
