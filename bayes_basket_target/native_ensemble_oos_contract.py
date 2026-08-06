"""Canonical historical OOS facts for official native ensemble member sets.

This contract is intentionally separate from ``model_oos_contract``.  A
native ensemble event depends on a *set* of member inputs and their field
receipts, so representing that set by V13's singular raw-object field would
lose lineage.  The historical availability proof below is deliberately not a
native-arrival proof: it records only an upper bound derived from official HTTP
Last-Modified metadata and is confined to historical, no-live OOS.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import math
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .model_oos_contract import derive_cohort_statistics


CONTRACT_SCHEMA = "bayes_basket_target_native_ensemble_oos_contract_v1"
EVENT_FACT_TYPE = "native_ensemble_oos_event"
COHORT_FACT_TYPE = "native_ensemble_oos_cohort"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
EVENT_STATE = "MATERIALIZED_UNPUBLISHED"
COHORT_STATES = frozenset({"MATERIALIZED_UNPUBLISHED", "CANDIDATE_ONLY"})
CALIBRATION_METHOD_VERSION = "ADDITIVE_BIAS_V1"
EVENT_OOS_PROTOCOL_VERSION = (
    "STRICT_HISTORICAL_AWC_NATIVE_ENSEMBLE_AVAILABILITY_V1"
)
AVAILABILITY_EVIDENCE_KIND = "OFFICIAL_HTTP_LAST_MODIFIED_UPPER_BOUND"
SETTLEMENT_TIMEZONE = "America/New_York"
LEAD_BUCKET = "d2"
LEAD_BUCKET_SCOPE = "PREVIOUS_DAY2"
RUN_CYCLE = "18Z"
RUN_CYCLE_SCOPE = "NATIVE_UTC_CYCLE"
PARTITIONS = ("train", "holdout")
ENSEMBLE_REDUCER_VERSION = "ENSEMBLE_MEAN_OF_MEMBER_ET_DAY_MAX_V1"
NATIVE_ENSEMBLE_SYSTEM_REGISTRY = {
    "NOAA_GEFS_05": {
        "member_count": 31,
        "role_counts": {"CONTROL": 1, "PERTURBED": 30},
        "daily_max_extraction_version": (
            "noaa_gefs_pds_0p50_18z_d2_et_member_tmax_v1"
        ),
    },
    "ECMWF_IFS_ENS_OPEN_PF50": {
        "member_count": 50,
        "role_counts": {"CONTROL": 0, "PERTURBED": 50},
        "daily_max_extraction_version": (
            "ecmwf_open_data_ifs_0p25_enfo_pf50_18z_d2_et_member_tmax_v1"
        ),
    },
}


def _strict(raw: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(raw) != fields:
        raise ContractError(f"native_ensemble_oos_{label}_fields_mismatch")


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"native_ensemble_oos_{label}_missing")
    return result


def _sha(value: Any, label: str) -> str:
    result = _text(value, label).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"native_ensemble_oos_{label}_invalid")
    return result


def _utc(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"native_ensemble_oos_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"native_ensemble_oos_{label}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _event_date(value: Any) -> str:
    text = _text(value, "event_date")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ContractError("native_ensemble_oos_event_date_invalid") from exc


def _timezone(value: Any) -> str:
    text = _text(value, "settlement_timezone")
    try:
        ZoneInfo(text)
    except Exception as exc:
        raise ContractError("native_ensemble_oos_settlement_timezone_invalid") from exc
    return text


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"native_ensemble_oos_{label}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"native_ensemble_oos_{label}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"native_ensemble_oos_{label}_invalid")
    return result


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"native_ensemble_oos_{label}_invalid")
    return value


def _hash_vector(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"native_ensemble_oos_{label}_required")
    result = tuple(_sha(item, f"{label}_item") for item in value)
    if list(result) != sorted(result) or len(set(result)) != len(result):
        raise ContractError(f"native_ensemble_oos_{label}_ordering_invalid")
    return result


def _text_vector(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"native_ensemble_oos_{label}_required")
    if any(not isinstance(item, str) for item in value):
        raise ContractError(f"native_ensemble_oos_{label}_text_required")
    result = tuple(_text(item, f"{label}_item") for item in value)
    if list(result) != sorted(result) or len(set(result)) != len(result):
        raise ContractError(f"native_ensemble_oos_{label}_ordering_invalid")
    return result


MEMBER_EVIDENCE_FIELDS = {
    "member_id",
    "role",
    "value_c",
    "source_member_evidence_sha256",
    "field_receipt_manifest_sha256",
}
MEMBER_ROLES = frozenset({"CONTROL", "PERTURBED"})


@dataclass(frozen=True)
class NativeEnsembleMemberEvidence:
    member_id: str
    role: str
    value_c: float
    source_member_evidence_sha256: str
    field_receipt_manifest_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "NativeEnsembleMemberEvidence":
        _strict(raw, MEMBER_EVIDENCE_FIELDS, "member_evidence")
        member_id = _text(raw["member_id"], "member_id")
        role = _text(raw["role"], "member_role")
        if role not in MEMBER_ROLES:
            raise ContractError("native_ensemble_oos_member_role_invalid")
        return cls(
            member_id=member_id,
            role=role,
            value_c=_number(raw["value_c"], "member_value_c"),
            source_member_evidence_sha256=_sha(
                raw["source_member_evidence_sha256"],
                "source_member_evidence_sha256",
            ),
            field_receipt_manifest_sha256=_sha(
                raw["field_receipt_manifest_sha256"],
                "field_receipt_manifest_sha256",
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "role": self.role,
            "value_c": self.value_c,
            "source_member_evidence_sha256": self.source_member_evidence_sha256,
            "field_receipt_manifest_sha256": self.field_receipt_manifest_sha256,
        }


EVENT_FIELDS = {
    "station_id",
    "model_id",
    "ensemble_system_id",
    "model_family",
    "provider_id",
    "provider_product_id",
    "independence_family_id",
    "source_class",
    "authority_scope",
    "event_date",
    "settlement_timezone",
    "lead_bucket",
    "lead_bucket_scope",
    "run_init_utc",
    "run_cycle",
    "run_cycle_scope",
    "forecast_decision_at_utc",
    "official_object_available_by_utc",
    "availability_evidence_kind",
    "native_arrival_indexed",
    "formal_live_authority",
    "p1_eligible",
    "member_count",
    "daily_max_extraction_version",
    "ensemble_reducer_version",
    "forecast_value_c",
    "member_evidence_vector",
    "member_evidence_vector_sha256",
    "raw_message_sha256s",
    "raw_message_vector_sha256",
    "raw_object_ids",
    "raw_object_id_vector_sha256",
    "source_packet_sha256",
    "source_packet_ref",
    "adapter_event_input_content_sha256",
    "archive_retrieved_at_utc",
    "canonical_truth_fact_content_sha256",
    "truth_id",
    "truth_revision_no",
    "truth_content_sha256",
    "truth_value_c",
    "station_day_end_utc",
    "truth_canonical_committed_at_utc",
    "event_oos_protocol_version",
    "state",
    "content_sha256",
}


def _member_vector(value: Any) -> tuple[NativeEnsembleMemberEvidence, ...]:
    if not isinstance(value, list) or len(value) < 2:
        raise ContractError("native_ensemble_oos_member_evidence_vector_invalid")
    if any(not isinstance(item, Mapping) for item in value):
        raise ContractError("native_ensemble_oos_member_evidence_mapping_required")
    members = tuple(NativeEnsembleMemberEvidence.from_mapping(item) for item in value)
    ids = [item.member_id for item in members]
    if ids != sorted(ids) or len(set(ids)) != len(ids):
        raise ContractError("native_ensemble_oos_member_evidence_ordering_invalid")
    if len({item.source_member_evidence_sha256 for item in members}) != len(members):
        raise ContractError("native_ensemble_oos_member_evidence_sha_duplicate")
    if len({item.field_receipt_manifest_sha256 for item in members}) != len(members):
        raise ContractError("native_ensemble_oos_member_manifest_sha_duplicate")
    return members


def _validate_system_member_roster(
    *,
    ensemble_system_id: str,
    member_count: int,
    members: Sequence[NativeEnsembleMemberEvidence],
    daily_max_extraction_version: Any,
    ensemble_reducer_version: Any,
) -> None:
    registry = NATIVE_ENSEMBLE_SYSTEM_REGISTRY.get(ensemble_system_id)
    if registry is None:
        raise ContractError("native_ensemble_oos_system_unregistered")
    if member_count != len(members):
        raise ContractError("native_ensemble_oos_member_count_vector_mismatch")
    if member_count != registry["member_count"]:
        raise ContractError("native_ensemble_oos_member_count_roster_mismatch")
    observed_roles = {
        role: sum(item.role == role for item in members)
        for role in MEMBER_ROLES
    }
    if observed_roles != registry["role_counts"]:
        raise ContractError("native_ensemble_oos_member_role_roster_mismatch")
    if (
        _text(daily_max_extraction_version, "daily_max_extraction_version")
        != registry["daily_max_extraction_version"]
    ):
        raise ContractError("native_ensemble_oos_daily_max_extraction_version_invalid")
    if ensemble_reducer_version != ENSEMBLE_REDUCER_VERSION:
        raise ContractError("native_ensemble_oos_ensemble_reducer_version_invalid")


@dataclass(frozen=True)
class CanonicalNativeEnsembleOOSEventFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalNativeEnsembleOOSEventFact":
        _strict(raw, EVENT_FIELDS, "event")
        value = dict(raw)
        value["station_id"] = _text(value["station_id"], "station_id").upper()
        value["model_id"] = _text(value["model_id"], "model_id").upper()
        value["ensemble_system_id"] = _text(
            value["ensemble_system_id"], "ensemble_system_id"
        ).upper()
        for field in (
            "model_family",
            "provider_id",
            "provider_product_id",
            "independence_family_id",
        ):
            value[field] = _text(value[field], field)
        if value["source_class"] != "native_official":
            raise ContractError("native_ensemble_oos_source_class_invalid")
        if value["authority_scope"] != AUTHORITY_SCOPE:
            raise ContractError("native_ensemble_oos_authority_scope_invalid")
        value["event_date"] = _event_date(value["event_date"])
        value["settlement_timezone"] = _timezone(value["settlement_timezone"])
        if value["settlement_timezone"] != SETTLEMENT_TIMEZONE:
            raise ContractError("native_ensemble_oos_settlement_timezone_not_et")
        if (
            value["lead_bucket"] != LEAD_BUCKET
            or value["lead_bucket_scope"] != LEAD_BUCKET_SCOPE
            or value["run_cycle"] != RUN_CYCLE
            or value["run_cycle_scope"] != RUN_CYCLE_SCOPE
        ):
            raise ContractError("native_ensemble_oos_lead_cycle_scope_invalid")
        for field in (
            "run_init_utc",
            "forecast_decision_at_utc",
            "official_object_available_by_utc",
            "station_day_end_utc",
            "truth_canonical_committed_at_utc",
            "archive_retrieved_at_utc",
        ):
            value[field] = _utc(value[field], field)
        if value["availability_evidence_kind"] != AVAILABILITY_EVIDENCE_KIND:
            raise ContractError("native_ensemble_oos_availability_evidence_kind_invalid")
        if any(
            value[field] is not False
            for field in (
                "native_arrival_indexed",
                "formal_live_authority",
                "p1_eligible",
            )
        ):
            raise ContractError("native_ensemble_oos_historical_authority_escalation")
        if value["event_oos_protocol_version"] != EVENT_OOS_PROTOCOL_VERSION:
            raise ContractError("native_ensemble_oos_protocol_version_invalid")
        if value["state"] != EVENT_STATE:
            raise ContractError("native_ensemble_oos_event_state_invalid")
        if not (
            value["run_init_utc"]
            <= value["official_object_available_by_utc"]
            <= value["forecast_decision_at_utc"]
            < value["station_day_end_utc"]
            <= value["truth_canonical_committed_at_utc"]
        ):
            raise ContractError("native_ensemble_oos_event_temporal_order_invalid")
        value["forecast_value_c"] = _number(
            value["forecast_value_c"], "forecast_value_c"
        )
        value["truth_value_c"] = _number(value["truth_value_c"], "truth_value_c")
        value["member_count"] = _integer(value["member_count"], "member_count")
        members = _member_vector(value["member_evidence_vector"])
        value["member_evidence_vector"] = [item.to_mapping() for item in members]
        _validate_system_member_roster(
            ensemble_system_id=value["ensemble_system_id"],
            member_count=value["member_count"],
            members=members,
            daily_max_extraction_version=value["daily_max_extraction_version"],
            ensemble_reducer_version=value["ensemble_reducer_version"],
        )
        member_mean = sum(item.value_c for item in members) / len(members)
        if not math.isclose(
            value["forecast_value_c"], member_mean, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ContractError("native_ensemble_oos_forecast_member_mean_mismatch")
        value["member_evidence_vector_sha256"] = _sha(
            value["member_evidence_vector_sha256"],
            "member_evidence_vector_sha256",
        )
        if value["member_evidence_vector_sha256"] != content_sha256(
            value["member_evidence_vector"]
        ):
            raise ContractError("native_ensemble_oos_member_evidence_vector_hash_mismatch")
        vector = _hash_vector(value["raw_message_sha256s"], "raw_message_sha256s")
        value["raw_message_sha256s"] = list(vector)
        value["raw_message_vector_sha256"] = _sha(
            value["raw_message_vector_sha256"], "raw_message_vector_sha256"
        )
        if value["raw_message_vector_sha256"] != content_sha256(
            value["raw_message_sha256s"]
        ):
            raise ContractError(
                "native_ensemble_oos_raw_message_vector_sha256_mismatch"
            )
        object_ids = _text_vector(value["raw_object_ids"], "raw_object_ids")
        value["raw_object_ids"] = list(object_ids)
        value["raw_object_id_vector_sha256"] = _sha(
            value["raw_object_id_vector_sha256"],
            "raw_object_id_vector_sha256",
        )
        if value["raw_object_id_vector_sha256"] != content_sha256(
            value["raw_object_ids"]
        ):
            raise ContractError(
                "native_ensemble_oos_raw_object_id_vector_sha256_mismatch"
            )
        for field in (
            "source_packet_sha256",
            "adapter_event_input_content_sha256",
            "canonical_truth_fact_content_sha256",
            "truth_content_sha256",
        ):
            value[field] = _sha(value[field], field)
        value["source_packet_ref"] = _text(
            value["source_packet_ref"], "source_packet_ref"
        )
        value["truth_id"] = _text(value["truth_id"], "truth_id")
        value["truth_revision_no"] = _integer(
            value["truth_revision_no"], "truth_revision_no"
        )
        if value["truth_revision_no"] < 0:
            raise ContractError("native_ensemble_oos_truth_revision_no_invalid")
        supplied = _sha(value["content_sha256"], "event_content_sha256")
        identity = {key: item for key, item in value.items() if key != "content_sha256"}
        if supplied != content_sha256(identity):
            raise ContractError("native_ensemble_oos_event_content_sha256_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(
            str(item)
            for item in (
                self.raw["station_id"],
                self.raw["ensemble_system_id"],
                self.raw["independence_family_id"],
                self.raw["settlement_timezone"],
                self.raw["lead_bucket"],
                self.raw["run_cycle"],
                self.raw["event_date"],
            )
        )


COHORT_FIELDS = {
    "station_id",
    "model_id",
    "ensemble_system_id",
    "model_family",
    "provider_id",
    "provider_product_id",
    "independence_family_id",
    "source_class",
    "authority_scope",
    "settlement_timezone",
    "lead_bucket",
    "lead_bucket_scope",
    "run_cycle",
    "run_cycle_scope",
    "season_bin",
    "event_oos_protocol_version",
    "calibration_method_version",
    "cohort_as_of_utc",
    "train_event_content_sha256s",
    "holdout_event_content_sha256s",
    "train_event_set_sha256",
    "holdout_event_set_sha256",
    "cohort_event_vector_sha256",
    "calibration_parameters",
    "selection_train_metrics",
    "holdout_metrics",
    "baseline_holdout_metrics",
    "evaluation_state",
    "state",
    "content_sha256",
}


def _cohort_hash_vector(value: Any, label: str, *, size: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) != size:
        raise ContractError(f"native_ensemble_oos_{label}_size_invalid")
    result = tuple(_sha(item, f"{label}_item") for item in value)
    if len(set(result)) != len(result):
        raise ContractError(f"native_ensemble_oos_{label}_duplicate")
    return result


def _number_map(value: Any, label: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or not value:
        raise ContractError(f"native_ensemble_oos_{label}_mapping_required")
    return {
        _text(key, f"{label}_key"): _number(item, f"{label}_{key}")
        for key, item in sorted(value.items())
    }


@dataclass(frozen=True)
class CanonicalNativeEnsembleOOSCohortFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalNativeEnsembleOOSCohortFact":
        _strict(raw, COHORT_FIELDS, "cohort")
        value = dict(raw)
        value["station_id"] = _text(value["station_id"], "station_id").upper()
        value["model_id"] = _text(value["model_id"], "model_id").upper()
        value["ensemble_system_id"] = _text(
            value["ensemble_system_id"], "ensemble_system_id"
        ).upper()
        for field in (
            "model_family",
            "provider_id",
            "provider_product_id",
            "independence_family_id",
            "season_bin",
        ):
            value[field] = _text(value[field], field)
        if value["source_class"] != "native_official":
            raise ContractError("native_ensemble_oos_cohort_source_class_invalid")
        if value["authority_scope"] != AUTHORITY_SCOPE:
            raise ContractError("native_ensemble_oos_cohort_authority_scope_invalid")
        value["settlement_timezone"] = _timezone(value["settlement_timezone"])
        if value["settlement_timezone"] != SETTLEMENT_TIMEZONE:
            raise ContractError("native_ensemble_oos_cohort_timezone_not_et")
        if (
            value["lead_bucket"] != LEAD_BUCKET
            or value["lead_bucket_scope"] != LEAD_BUCKET_SCOPE
            or value["run_cycle"] != RUN_CYCLE
            or value["run_cycle_scope"] != RUN_CYCLE_SCOPE
        ):
            raise ContractError("native_ensemble_oos_cohort_lead_cycle_scope_invalid")
        if value["event_oos_protocol_version"] != EVENT_OOS_PROTOCOL_VERSION:
            raise ContractError("native_ensemble_oos_cohort_protocol_version_invalid")
        if value["calibration_method_version"] != CALIBRATION_METHOD_VERSION:
            raise ContractError("native_ensemble_oos_cohort_calibration_method_invalid")
        value["cohort_as_of_utc"] = _utc(
            value["cohort_as_of_utc"], "cohort_as_of_utc"
        )
        train = _cohort_hash_vector(
            value["train_event_content_sha256s"], "train_events", size=20
        )
        holdout = _cohort_hash_vector(
            value["holdout_event_content_sha256s"], "holdout_events", size=8
        )
        if set(train) & set(holdout):
            raise ContractError("native_ensemble_oos_cohort_event_overlap")
        value["train_event_content_sha256s"] = list(train)
        value["holdout_event_content_sha256s"] = list(holdout)
        for field in (
            "train_event_set_sha256",
            "holdout_event_set_sha256",
            "cohort_event_vector_sha256",
        ):
            value[field] = _sha(value[field], field)
        if value["train_event_set_sha256"] != content_sha256(list(train)):
            raise ContractError("native_ensemble_oos_cohort_train_set_sha256_mismatch")
        if value["holdout_event_set_sha256"] != content_sha256(list(holdout)):
            raise ContractError("native_ensemble_oos_cohort_holdout_set_sha256_mismatch")
        vector = [
            *(
                {"partition": "train", "event_content_sha256": item}
                for item in train
            ),
            *(
                {"partition": "holdout", "event_content_sha256": item}
                for item in holdout
            ),
        ]
        if value["cohort_event_vector_sha256"] != content_sha256(vector):
            raise ContractError("native_ensemble_oos_cohort_vector_sha256_mismatch")
        for field in (
            "calibration_parameters",
            "selection_train_metrics",
            "holdout_metrics",
            "baseline_holdout_metrics",
        ):
            value[field] = _number_map(value[field], field)
        if value["evaluation_state"] not in {"OOS_PASS", "OOS_REJECTED"}:
            raise ContractError("native_ensemble_oos_cohort_evaluation_state_invalid")
        if value["state"] not in COHORT_STATES:
            raise ContractError("native_ensemble_oos_cohort_state_invalid")
        supplied = _sha(value["content_sha256"], "cohort_content_sha256")
        identity = {key: item for key, item in value.items() if key != "content_sha256"}
        if supplied != content_sha256(identity):
            raise ContractError("native_ensemble_oos_cohort_content_sha256_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(
            str(item)
            for item in (
                self.raw["station_id"],
                self.raw["ensemble_system_id"],
                self.raw["independence_family_id"],
                self.raw["settlement_timezone"],
                self.raw["lead_bucket"],
                self.raw["run_cycle"],
                self.raw["season_bin"],
                self.raw["event_oos_protocol_version"],
            )
        )


def derive_native_ensemble_cohort_statistics(
    events: Sequence[CanonicalNativeEnsembleOOSEventFact],
) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float], str]:
    """Use the established 20+8 additive-bias statistic semantics.

    The existing routine only reads the ``raw`` forecast/truth values, so this
    preserves its calculation without coercing an ensemble member set into the
    singular V13 event contract.
    """

    return derive_cohort_statistics(events)  # type: ignore[arg-type]


def _same_scope(event: Mapping[str, Any], cohort: Mapping[str, Any]) -> bool:
    return all(
        event[field] == cohort[field]
        for field in (
            "station_id",
            "model_id",
            "ensemble_system_id",
            "model_family",
            "provider_id",
            "provider_product_id",
            "independence_family_id",
            "source_class",
            "authority_scope",
            "settlement_timezone",
            "lead_bucket",
            "lead_bucket_scope",
            "run_cycle",
            "run_cycle_scope",
            "event_oos_protocol_version",
        )
    )


def _assert_cohort_derivation(
    cohort: CanonicalNativeEnsembleOOSCohortFact,
    event_by_sha: Mapping[str, CanonicalNativeEnsembleOOSEventFact],
) -> None:
    ordered_hashes = [
        *cohort.raw["train_event_content_sha256s"],
        *cohort.raw["holdout_event_content_sha256s"],
    ]
    try:
        events = [event_by_sha[item] for item in ordered_hashes]
    except KeyError as exc:
        raise ContractError("native_ensemble_oos_cohort_event_reference_missing") from exc
    if any(not _same_scope(event.raw, cohort.raw) for event in events):
        raise ContractError("native_ensemble_oos_cohort_event_scope_mismatch")
    dates = [str(event.raw["event_date"]) for event in events]
    if dates != sorted(set(dates)):
        raise ContractError("native_ensemble_oos_cohort_event_date_order_invalid")
    if dates[19] >= dates[20]:
        raise ContractError("native_ensemble_oos_cohort_partition_date_overlap")
    if any(
        event.raw["truth_canonical_committed_at_utc"]
        > cohort.raw["cohort_as_of_utc"]
        for event in events
    ):
        raise ContractError("native_ensemble_oos_cohort_truth_after_asof")
    derived = derive_native_ensemble_cohort_statistics(events)
    fields = (
        "calibration_parameters",
        "selection_train_metrics",
        "holdout_metrics",
        "baseline_holdout_metrics",
    )
    if any(cohort.raw[field] != expected for field, expected in zip(fields, derived[:4])):
        raise ContractError("native_ensemble_oos_cohort_statistics_mismatch")
    if cohort.raw["evaluation_state"] != derived[4]:
        raise ContractError("native_ensemble_oos_cohort_evaluation_mismatch")


@dataclass(frozen=True)
class CanonicalNativeEnsembleOOSContract:
    as_of_utc: str
    builder_id: str
    event_facts: tuple[CanonicalNativeEnsembleOOSEventFact, ...]
    cohort_facts: tuple[CanonicalNativeEnsembleOOSCohortFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalNativeEnsembleOOSContract":
        _strict(
            raw,
            {
                "schema",
                "as_of_utc",
                "builder_id",
                "event_facts",
                "cohort_facts",
                "content_sha256",
            },
            "contract",
        )
        if raw["schema"] != CONTRACT_SCHEMA:
            raise ContractError("native_ensemble_oos_contract_schema_invalid")
        if not isinstance(raw["event_facts"], list) or not isinstance(
            raw["cohort_facts"], list
        ):
            raise ContractError("native_ensemble_oos_contract_fact_lists_required")
        events = tuple(
            CanonicalNativeEnsembleOOSEventFact.from_mapping(item)
            for item in raw["event_facts"]
        )
        cohorts = tuple(
            CanonicalNativeEnsembleOOSCohortFact.from_mapping(item)
            for item in raw["cohort_facts"]
        )
        if not events:
            raise ContractError("native_ensemble_oos_contract_event_required")
        if len({item.scope_key for item in events}) != len(events):
            raise ContractError("native_ensemble_oos_contract_event_scope_duplicate")
        if len({item.scope_key for item in cohorts}) != len(cohorts):
            raise ContractError("native_ensemble_oos_contract_cohort_scope_duplicate")
        as_of = _utc(raw["as_of_utc"], "contract_as_of_utc")
        if any(
            event.raw["truth_canonical_committed_at_utc"] > as_of
            for event in events
        ) or any(
            event.raw["archive_retrieved_at_utc"] > as_of
            for event in events
        ) or any(cohort.raw["cohort_as_of_utc"] > as_of for cohort in cohorts):
            raise ContractError("native_ensemble_oos_contract_fact_after_asof")
        event_by_sha = {item.content_sha256: item for item in events}
        for cohort in cohorts:
            _assert_cohort_derivation(cohort, event_by_sha)
        supplied = _sha(raw["content_sha256"], "contract_content_sha256")
        identity = {key: item for key, item in raw.items() if key != "content_sha256"}
        if supplied != content_sha256(identity):
            raise ContractError("native_ensemble_oos_contract_content_sha256_mismatch")
        return cls(
            as_of_utc=as_of,
            builder_id=_text(raw["builder_id"], "builder_id"),
            event_facts=events,
            cohort_facts=cohorts,
            content_sha256=supplied,
        )

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
    "AUTHORITY_SCOPE",
    "AVAILABILITY_EVIDENCE_KIND",
    "CALIBRATION_METHOD_VERSION",
    "COHORT_FACT_TYPE",
    "CONTRACT_SCHEMA",
    "EVENT_FACT_TYPE",
    "EVENT_OOS_PROTOCOL_VERSION",
    "ENSEMBLE_REDUCER_VERSION",
    "NATIVE_ENSEMBLE_SYSTEM_REGISTRY",
    "CanonicalNativeEnsembleOOSContract",
    "CanonicalNativeEnsembleOOSCohortFact",
    "CanonicalNativeEnsembleOOSEventFact",
    "NativeEnsembleMemberEvidence",
    "derive_native_ensemble_cohort_statistics",
]
