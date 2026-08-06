"""Canonical V10 contracts for the republisher no-live P0 evidence chain.

The three fact types are intentionally separate.  A static historical
activation is not a forward receipt, a forward point is not a probability
distribution, and neither one alone is a P0 snapshot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .canonical_contract import _event_date, _sha, _text, _timestamp, _timezone
from .contracts import ContractError, content_sha256


CONTRACT_SCHEMA = "bayes_basket_target_canonical_republisher_no_live_p0_contract_v1"
PRODUCTS = {
    "AIFS": {
        "republisher_product_id": "ecmwf_aifs025_single",
        "independence_family_id": "ECMWF",
        "native_provider_id": "ecmwf_aifs025_official",
        "native_product_id": "ECMWF_AIFS_SINGLE_0P25",
    },
    "ICON": {
        "republisher_product_id": "dwd_icon_global",
        "independence_family_id": "DWD",
        "native_provider_id": "icon_global_official",
        "native_product_id": "DWD_ICON_GLOBAL",
    },
}
RUN_CYCLE_SCOPE = "REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
SOURCE_CLASS = "aggregator_republisher"
TRANSPORT_PROVIDER_ID = "OPEN_METEO"

_COMPONENT_RECEIPT_FIELDS = frozenset(
    {
        "upstream_model_id",
        "station_id",
        "event_date",
        "settlement_timezone",
        "station_coordinate_registry_sha256",
        "republisher_product_id",
        "independence_family_id",
        "request_url",
        "http_status",
        "requested_at_utc",
        "response_started_at_utc",
        "response_completed_at_utc",
        "raw_object_sha256",
        "requested_latitude",
        "requested_longitude",
        "response_latitude",
        "response_longitude",
        "hourly_valid_time_count",
        "hourly_curve_content_sha256",
        "normalized_content_sha256",
        "forecast_daily_max_c",
        "receipt_content_sha256",
    }
)
_NATIVE_RECEIPT_FIELDS = frozenset(
    {
        "upstream_model_id",
        "station_id",
        "event_date",
        "settlement_timezone",
        "station_scope_sha256",
        "native_provider_id",
        "native_product_id",
        "candidate_cycle_policy_sha256",
        "wait_deadline_policy_sha256",
        "checked_at_utc",
        "expires_at_utc",
        "terminal_status",
        "required_object_count",
        "terminal_object_receipt_set_sha256",
        "receipt_content_sha256",
    }
)
_FORWARD_FIELDS = frozenset(
    {
        "applicability_id",
        "station_id",
        "event_date",
        "settlement_timezone",
        "settlement_definition",
        "season_bin",
        "lead_bucket",
        "run_cycle",
        "run_cycle_scope",
        "decision_at_utc",
        "activation_id",
        "activation_generation",
        "activation_content_sha256",
        "selection_content_sha256",
        "component_receipts",
        "component_receipt_set_sha256",
        "native_unavailability_receipts",
        "native_unavailability_receipt_set_sha256",
        "evidence_manifest_content_sha256",
        "evidence_manifest_content_address",
        "source_class",
        "transport_provider_id",
        "authority_scope",
        "evidence_role",
        "strict_oos_sample_eligible",
        "fresh_until_utc",
        "native_evidence_expires_at_utc",
        "state",
        "native_arrival_indexed",
        "p1_eligible",
        "formal_live_authority",
        "production_no_live_only",
        "content_sha256",
    }
)
_SCENARIO_FIELDS = frozenset(
    {
        "event_date",
        "canonical_truth_fact_content_sha256",
        "aifs_model_input_content_sha256",
        "icon_model_input_content_sha256",
        "selected_calibrated_point_c",
        "residual_c",
        "scenario_content_sha256",
    }
)
_DISTRIBUTION_FIELDS = frozenset(
    {
        "artifact_id",
        "station_id",
        "settlement_timezone",
        "settlement_definition",
        "season_bin",
        "lead_bucket",
        "run_cycle",
        "run_cycle_scope",
        "selection_id",
        "selection_content_sha256",
        "component_artifact_content_sha256s",
        "method_version",
        "train_n",
        "holdout_n",
        "train_scenarios",
        "train_scenario_set_sha256",
        "train_residual_mean_c",
        "train_residual_mae_c",
        "train_residual_rmse_c",
        "holdout_case_set_sha256",
        "selected_holdout_mean_crps_c",
        "climatology_holdout_mean_crps_c",
        "holdout_evaluation_passed",
        "fit_content_sha256",
        "source_class",
        "transport_provider_id",
        "authority_scope",
        "state",
        "future_event_count",
        "native_arrival_indexed",
        "p1_eligible",
        "formal_live_authority",
        "production_no_live_only",
        "content_sha256",
    }
)
_BUCKET_INTERVAL_FIELDS = frozenset(
    {"bucket_id", "ordinal", "lower_c", "upper_c"}
)
_BUCKET_PROBABILITY_FIELDS = frozenset(
    {"bucket_id", "ordinal", "scenario_count", "probability"}
)
_P0_FIELDS = frozenset(
    {
        "snapshot_id",
        "station_id",
        "event_date",
        "settlement_timezone",
        "settlement_definition",
        "season_bin",
        "lead_bucket",
        "run_cycle",
        "run_cycle_scope",
        "decision_at_utc",
        "materialized_at_utc",
        "fresh_until_utc",
        "activation_id",
        "activation_generation",
        "activation_content_sha256",
        "selection_content_sha256",
        "forward_applicability_content_sha256",
        "probability_distribution_artifact_content_sha256",
        "bucket_contract_sha256",
        "market_bucket_bridge_sha256",
        "bucket_intervals",
        "selected_forward_point_c",
        "scenario_temperature_set_sha256",
        "bucket_probabilities",
        "probability_root_sha256",
        "posterior_version",
        "posterior_sha256",
        "stage",
        "source_class",
        "transport_provider_id",
        "authority_scope",
        "evidence_role",
        "strict_oos_sample_eligible",
        "state",
        "native_arrival_indexed",
        "p1_eligible",
        "formal_live_authority",
        "production_no_live_only",
        "content_sha256",
    }
)
_CONTRACT_FIELDS = frozenset(
    {
        "schema",
        "as_of_utc",
        "builder_id",
        "forward_applicability_facts",
        "probability_distribution_artifact_facts",
        "p0_snapshot_facts",
        "content_sha256",
    }
)


def _strict(raw: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    if frozenset(raw) != fields:
        raise ContractError(f"canonical_{label}_fields_mismatch")


def _verify_hash(raw: Mapping[str, Any], field: str, label: str) -> str:
    supplied = _sha(raw.get(field), field)
    expected = content_sha256({k: v for k, v in raw.items() if k != field})
    if supplied != expected:
        raise ContractError(f"canonical_{label}_hash_mismatch")
    return supplied


def _content_address(value: Any, expected_sha256: str, field: str) -> str:
    address = _text(value, field)
    if address != f"cas+sha256://manifests/{expected_sha256}":
        raise ContractError(f"canonical_{field}_invalid")
    return address


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _decimal12(value: Any, field: str, *, nonnegative: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"canonical_{field}_decimal12_invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ContractError(f"canonical_{field}_decimal12_invalid") from exc
    if (
        not parsed.is_finite()
        or value != format(parsed, ".12f")
        or (parsed == 0 and value.startswith("-"))
    ):
        raise ContractError(f"canonical_{field}_decimal12_invalid")
    if nonnegative and parsed < 0:
        raise ContractError(f"canonical_{field}_negative")
    return value


def _positive_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"canonical_{field}_invalid")
    return value


def _fixed_authority(raw: Mapping[str, Any], *, state: str) -> None:
    fixed = {
        "source_class": SOURCE_CLASS,
        "transport_provider_id": TRANSPORT_PROVIDER_ID,
        "authority_scope": AUTHORITY_SCOPE,
        "state": state,
        "native_arrival_indexed": False,
        "p1_eligible": False,
        "formal_live_authority": False,
        "production_no_live_only": True,
    }
    for key, expected in fixed.items():
        if raw.get(key) != expected:
            raise ContractError(f"canonical_republisher_no_live_p0_{key}_invalid")


def _component_receipt(raw: Mapping[str, Any]) -> dict[str, Any]:
    _strict(raw, _COMPONENT_RECEIPT_FIELDS, "forward_component_receipt")
    model = _text(raw.get("upstream_model_id"), "upstream_model_id")
    if model not in PRODUCTS:
        raise ContractError("canonical_forward_component_model_invalid")
    product = PRODUCTS[model]["republisher_product_id"]
    family = PRODUCTS[model]["independence_family_id"]
    if (
        raw.get("republisher_product_id") != product
        or raw.get("independence_family_id") != family
        or raw.get("http_status") != 200
    ):
        raise ContractError("canonical_forward_component_product_invalid")
    request_url = _text(raw.get("request_url"), "request_url")
    parsed_url = urlparse(request_url)
    query = parse_qs(parsed_url.query)
    models = query.get("models", [])
    event = _event_date(raw.get("event_date"))
    timezone_name = _timezone(raw.get("settlement_timezone"))
    requested_latitude = _decimal12(raw.get("requested_latitude"), "requested_latitude")
    requested_longitude = _decimal12(raw.get("requested_longitude"), "requested_longitude")
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "api.open-meteo.com"
        or models != [product]
        or query.get("hourly") != ["temperature_2m"]
        or query.get("start_date") != [event]
        or query.get("end_date") != [event]
        or query.get("timezone") != [timezone_name]
        or query.get("latitude") != [format(Decimal(requested_latitude), ".6f")]
        or query.get("longitude") != [format(Decimal(requested_longitude), ".6f")]
    ):
        raise ContractError("canonical_forward_component_request_url_invalid")
    requested = _timestamp(raw.get("requested_at_utc"), "requested_at_utc")
    started = _timestamp(
        raw.get("response_started_at_utc"), "response_started_at_utc"
    )
    completed = _timestamp(
        raw.get("response_completed_at_utc"), "response_completed_at_utc"
    )
    if not (_instant(requested) <= _instant(started) <= _instant(completed)):
        raise ContractError("canonical_forward_component_timestamp_order_invalid")
    result = {
        "upstream_model_id": model,
        "station_id": _text(raw.get("station_id"), "station_id").upper(),
        "event_date": event,
        "settlement_timezone": timezone_name,
        "station_coordinate_registry_sha256": _sha(
            raw.get("station_coordinate_registry_sha256"),
            "station_coordinate_registry_sha256",
        ),
        "republisher_product_id": product,
        "independence_family_id": family,
        "request_url": request_url,
        "http_status": 200,
        "requested_at_utc": requested,
        "response_started_at_utc": started,
        "response_completed_at_utc": completed,
        "raw_object_sha256": _sha(raw.get("raw_object_sha256"), "raw_object_sha256"),
        "requested_latitude": requested_latitude,
        "requested_longitude": requested_longitude,
        "response_latitude": _decimal12(raw.get("response_latitude"), "response_latitude"),
        "response_longitude": _decimal12(raw.get("response_longitude"), "response_longitude"),
        "hourly_valid_time_count": _positive_int(
            raw.get("hourly_valid_time_count"), "hourly_valid_time_count"
        ),
        "hourly_curve_content_sha256": _sha(
            raw.get("hourly_curve_content_sha256"),
            "hourly_curve_content_sha256",
        ),
        "normalized_content_sha256": _sha(
            raw.get("normalized_content_sha256"), "normalized_content_sha256"
        ),
        "forecast_daily_max_c": _decimal12(
            raw.get("forecast_daily_max_c"), "forecast_daily_max_c"
        ),
        "receipt_content_sha256": _sha(
            raw.get("receipt_content_sha256"), "receipt_content_sha256"
        ),
    }
    _verify_hash(result, "receipt_content_sha256", "forward_component_receipt")
    return result


def _native_receipt(raw: Mapping[str, Any]) -> dict[str, Any]:
    _strict(raw, _NATIVE_RECEIPT_FIELDS, "native_unavailability_receipt")
    model = _text(raw.get("upstream_model_id"), "upstream_model_id")
    if model not in PRODUCTS:
        raise ContractError("canonical_native_receipt_model_invalid")
    native_provider = PRODUCTS[model]["native_provider_id"]
    native_product = PRODUCTS[model]["native_product_id"]
    if (
        raw.get("native_provider_id") != native_provider
        or raw.get("native_product_id") != native_product
        or raw.get("terminal_status")
        not in {"NATIVE_UNAVAILABLE", "NATIVE_INVALID", "NATIVE_LATE"}
    ):
        raise ContractError("canonical_native_receipt_status_invalid")
    checked = _timestamp(raw.get("checked_at_utc"), "checked_at_utc")
    expires = _timestamp(raw.get("expires_at_utc"), "expires_at_utc")
    if _instant(checked) >= _instant(expires):
        raise ContractError("canonical_native_receipt_expiry_invalid")
    result = {
        "upstream_model_id": model,
        "station_id": _text(raw.get("station_id"), "station_id").upper(),
        "event_date": _event_date(raw.get("event_date")),
        "settlement_timezone": _timezone(raw.get("settlement_timezone")),
        "station_scope_sha256": _sha(
            raw.get("station_scope_sha256"), "station_scope_sha256"
        ),
        "native_provider_id": native_provider,
        "native_product_id": native_product,
        "candidate_cycle_policy_sha256": _sha(
            raw.get("candidate_cycle_policy_sha256"),
            "candidate_cycle_policy_sha256",
        ),
        "wait_deadline_policy_sha256": _sha(
            raw.get("wait_deadline_policy_sha256"), "wait_deadline_policy_sha256"
        ),
        "checked_at_utc": checked,
        "expires_at_utc": expires,
        "terminal_status": str(raw.get("terminal_status")),
        "required_object_count": _positive_int(
            raw.get("required_object_count"), "required_object_count"
        ),
        "terminal_object_receipt_set_sha256": _sha(
            raw.get("terminal_object_receipt_set_sha256"),
            "terminal_object_receipt_set_sha256",
        ),
        "receipt_content_sha256": _sha(
            raw.get("receipt_content_sha256"), "receipt_content_sha256"
        ),
    }
    _verify_hash(result, "receipt_content_sha256", "native_unavailability_receipt")
    return result


@dataclass(frozen=True)
class CanonicalForwardApplicabilityFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalForwardApplicabilityFact":
        _strict(raw, _FORWARD_FIELDS, "forward_applicability_fact")
        _fixed_authority(
            raw, state="PRODUCTION_NO_LIVE_FORWARD_APPLICABILITY_READY"
        )
        if (
            raw.get("run_cycle") is not None
            or raw.get("run_cycle_scope") != RUN_CYCLE_SCOPE
            or raw.get("evidence_role") != "observational_candidate_not_oos"
            or raw.get("strict_oos_sample_eligible") is not False
        ):
            raise ContractError("canonical_forward_applicability_temporal_role_invalid")
        components_raw = raw.get("component_receipts")
        native_raw = raw.get("native_unavailability_receipts")
        if not isinstance(components_raw, list) or not isinstance(native_raw, list):
            raise ContractError("canonical_forward_applicability_receipts_required")
        components = [_component_receipt(item) for item in components_raw]
        native = [_native_receipt(item) for item in native_raw]
        if [item["upstream_model_id"] for item in components] != ["AIFS", "ICON"]:
            raise ContractError("canonical_forward_component_set_invalid")
        if [item["upstream_model_id"] for item in native] != ["AIFS", "ICON"]:
            raise ContractError("canonical_native_receipt_set_invalid")
        station = _text(raw.get("station_id"), "station_id").upper()
        event = _event_date(raw.get("event_date"))
        settlement_timezone = _timezone(raw.get("settlement_timezone"))
        if any(
            (
                item["station_id"],
                item["event_date"],
                item["settlement_timezone"],
            )
            != (station, event, settlement_timezone)
            for item in [*components, *native]
        ):
            raise ContractError("canonical_forward_receipt_scope_mismatch")
        expected_station_scope_sha = content_sha256(
            {
                "station_id": station,
                "event_date": event,
                "settlement_timezone": settlement_timezone,
            }
        )
        if any(
            item["station_scope_sha256"] != expected_station_scope_sha
            for item in native
        ):
            raise ContractError("canonical_native_station_scope_hash_mismatch")
        component_set_sha = _sha(
            raw.get("component_receipt_set_sha256"),
            "component_receipt_set_sha256",
        )
        native_set_sha = _sha(
            raw.get("native_unavailability_receipt_set_sha256"),
            "native_unavailability_receipt_set_sha256",
        )
        evidence_manifest_sha = _sha(
            raw.get("evidence_manifest_content_sha256"),
            "evidence_manifest_content_sha256",
        )
        evidence_manifest_address = _content_address(
            raw.get("evidence_manifest_content_address"),
            evidence_manifest_sha,
            "evidence_manifest_content_address",
        )
        if component_set_sha != content_sha256(components):
            raise ContractError("canonical_forward_component_set_hash_mismatch")
        if native_set_sha != content_sha256(native):
            raise ContractError("canonical_native_receipt_set_hash_mismatch")
        decision = _timestamp(raw.get("decision_at_utc"), "decision_at_utc")
        fresh_until = _timestamp(raw.get("fresh_until_utc"), "fresh_until_utc")
        native_expires = _timestamp(
            raw.get("native_evidence_expires_at_utc"),
            "native_evidence_expires_at_utc",
        )
        latest_component = max(
            _instant(item["response_completed_at_utc"]) for item in components
        )
        earliest_fresh = min(_instant(fresh_until), _instant(native_expires))
        latest_native = max(_instant(item["checked_at_utc"]) for item in native)
        if not (
            latest_component <= latest_native <= _instant(decision) < earliest_fresh
        ):
            raise ContractError("canonical_forward_applicability_freshness_invalid")
        if _instant(fresh_until) != latest_component + timedelta(hours=6):
            raise ContractError("canonical_forward_applicability_ttl_invalid")
        if _instant(native_expires) != latest_native + timedelta(minutes=5):
            raise ContractError("canonical_native_receipt_ttl_invalid")
        if any(item["expires_at_utc"] != native_expires for item in native):
            raise ContractError("canonical_native_receipt_expiry_mismatch")
        try:
            decision_local_date = _instant(decision).astimezone(
                ZoneInfo(settlement_timezone)
            ).date()
            event_local_date = datetime.fromisoformat(event).date()
        except Exception as exc:
            raise ContractError("canonical_forward_applicability_lead_invalid") from exc
        lead_days = (event_local_date - decision_local_date).days
        if lead_days not in {0, 1, 2} or raw.get("lead_bucket") != f"d{lead_days}":
            raise ContractError("canonical_forward_applicability_lead_invalid")
        activation_generation = _positive_int(
            raw.get("activation_generation"), "activation_generation"
        )
        identity_seed = {
            "station_id": str(raw.get("station_id")).upper(),
            "event_date": str(raw.get("event_date")),
            "decision_at_utc": decision,
            "lead_bucket": str(raw.get("lead_bucket")),
            "activation_content_sha256": str(raw.get("activation_content_sha256")),
            "component_receipt_set_sha256": component_set_sha,
            "native_unavailability_receipt_set_sha256": native_set_sha,
        }
        applicability_id = "forward-applicability-" + content_sha256(identity_seed)[:40]
        if raw.get("applicability_id") != applicability_id:
            raise ContractError("canonical_forward_applicability_id_mismatch")
        result = dict(raw)
        result.update(
            {
                "applicability_id": applicability_id,
                "station_id": station,
                "event_date": event,
                "settlement_timezone": settlement_timezone,
                "settlement_definition": _text(
                    raw.get("settlement_definition"), "settlement_definition"
                ),
                "season_bin": _text(raw.get("season_bin"), "season_bin"),
                "lead_bucket": _text(raw.get("lead_bucket"), "lead_bucket"),
                "decision_at_utc": decision,
                "activation_id": _text(raw.get("activation_id"), "activation_id"),
                "activation_generation": activation_generation,
                "activation_content_sha256": _sha(
                    raw.get("activation_content_sha256"),
                    "activation_content_sha256",
                ),
                "selection_content_sha256": _sha(
                    raw.get("selection_content_sha256"),
                    "selection_content_sha256",
                ),
                "component_receipts": components,
                "component_receipt_set_sha256": component_set_sha,
                "native_unavailability_receipts": native,
                "native_unavailability_receipt_set_sha256": native_set_sha,
                "evidence_manifest_content_sha256": evidence_manifest_sha,
                "evidence_manifest_content_address": evidence_manifest_address,
                "fresh_until_utc": fresh_until,
                "native_evidence_expires_at_utc": native_expires,
                "content_sha256": _sha(
                    raw.get("content_sha256"), "forward_applicability_content_sha256"
                ),
            }
        )
        _verify_hash(result, "content_sha256", "forward_applicability_fact")
        return cls(result)

    def to_mapping(self) -> dict[str, Any]:
        return dict(self.raw)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(
            str(self.raw[key])
            for key in (
                "station_id",
                "event_date",
                "decision_at_utc",
                "lead_bucket",
                "activation_content_sha256",
                "component_receipt_set_sha256",
                "native_unavailability_receipt_set_sha256",
            )
        )


def _scenario(raw: Mapping[str, Any]) -> dict[str, Any]:
    _strict(raw, _SCENARIO_FIELDS, "probability_train_scenario")
    result = {
        "event_date": _event_date(raw.get("event_date")),
        "canonical_truth_fact_content_sha256": _sha(
            raw.get("canonical_truth_fact_content_sha256"),
            "canonical_truth_fact_content_sha256",
        ),
        "aifs_model_input_content_sha256": _sha(
            raw.get("aifs_model_input_content_sha256"),
            "aifs_model_input_content_sha256",
        ),
        "icon_model_input_content_sha256": _sha(
            raw.get("icon_model_input_content_sha256"),
            "icon_model_input_content_sha256",
        ),
        "selected_calibrated_point_c": _decimal12(
            raw.get("selected_calibrated_point_c"), "selected_calibrated_point_c"
        ),
        "residual_c": _decimal12(raw.get("residual_c"), "residual_c"),
        "scenario_content_sha256": _sha(
            raw.get("scenario_content_sha256"), "scenario_content_sha256"
        ),
    }
    _verify_hash(result, "scenario_content_sha256", "probability_train_scenario")
    return result


@dataclass(frozen=True)
class CanonicalProbabilityDistributionArtifactFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalProbabilityDistributionArtifactFact":
        _strict(raw, _DISTRIBUTION_FIELDS, "probability_distribution_artifact_fact")
        _fixed_authority(
            raw, state="PRODUCTION_NO_LIVE_TRAIN_RESIDUAL_DISTRIBUTION_READY"
        )
        if (
            raw.get("run_cycle") is not None
            or raw.get("run_cycle_scope") != RUN_CYCLE_SCOPE
            or raw.get("method_version") != "train_residual_bootstrap_v1"
            or raw.get("future_event_count") != 0
            or raw.get("holdout_evaluation_passed") is not True
        ):
            raise ContractError("canonical_probability_distribution_role_invalid")
        if raw.get("train_n") != 20 or raw.get("holdout_n") != 8:
            raise ContractError("canonical_probability_distribution_sample_gate_invalid")
        scenarios_raw = raw.get("train_scenarios")
        if not isinstance(scenarios_raw, list) or len(scenarios_raw) != 20:
            raise ContractError("canonical_probability_distribution_scenarios_required")
        scenarios = [_scenario(item) for item in scenarios_raw]
        if [item["event_date"] for item in scenarios] != sorted(
            {item["event_date"] for item in scenarios}
        ):
            raise ContractError("canonical_probability_distribution_scenario_dates_invalid")
        scenario_set_sha = _sha(
            raw.get("train_scenario_set_sha256"), "train_scenario_set_sha256"
        )
        if scenario_set_sha != content_sha256(scenarios):
            raise ContractError("canonical_probability_distribution_scenario_set_mismatch")
        component_shas = raw.get("component_artifact_content_sha256s")
        if (
            not isinstance(component_shas, list)
            or len(component_shas) != 2
            or len(set(component_shas)) != 2
        ):
            raise ContractError("canonical_probability_distribution_components_invalid")
        component_shas = [_sha(value, "component_artifact_content_sha256") for value in component_shas]
        residuals = [Decimal(item["residual_c"]) for item in scenarios]
        mean = sum(residuals) / Decimal(20)
        if abs(mean) > Decimal("0.000000000001"):
            raise ContractError("canonical_probability_distribution_residual_mean_invalid")
        fit_identity = {
            "selection_content_sha256": str(raw.get("selection_content_sha256")),
            "component_artifact_content_sha256s": component_shas,
            "method_version": "train_residual_bootstrap_v1",
            "train_scenario_set_sha256": scenario_set_sha,
        }
        fit_sha = _sha(raw.get("fit_content_sha256"), "fit_content_sha256")
        if fit_sha != content_sha256(fit_identity):
            raise ContractError("canonical_probability_distribution_fit_hash_mismatch")
        artifact_seed = {
            "station_id": str(raw.get("station_id")).upper(),
            "selection_content_sha256": str(raw.get("selection_content_sha256")),
            "method_version": "train_residual_bootstrap_v1",
            "train_scenario_set_sha256": scenario_set_sha,
        }
        artifact_id = "republisher-temperature-distribution-" + content_sha256(
            artifact_seed
        )[:40]
        if raw.get("artifact_id") != artifact_id:
            raise ContractError("canonical_probability_distribution_artifact_id_mismatch")
        result = dict(raw)
        result.update(
            {
                "artifact_id": artifact_id,
                "station_id": _text(raw.get("station_id"), "station_id").upper(),
                "settlement_timezone": _timezone(raw.get("settlement_timezone")),
                "settlement_definition": _text(
                    raw.get("settlement_definition"), "settlement_definition"
                ),
                "season_bin": _text(raw.get("season_bin"), "season_bin"),
                "lead_bucket": _text(raw.get("lead_bucket"), "lead_bucket"),
                "selection_id": _text(raw.get("selection_id"), "selection_id"),
                "selection_content_sha256": _sha(
                    raw.get("selection_content_sha256"), "selection_content_sha256"
                ),
                "component_artifact_content_sha256s": component_shas,
                "train_scenarios": scenarios,
                "train_scenario_set_sha256": scenario_set_sha,
                "train_residual_mean_c": _decimal12(
                    raw.get("train_residual_mean_c"), "train_residual_mean_c"
                ),
                "train_residual_mae_c": _decimal12(
                    raw.get("train_residual_mae_c"),
                    "train_residual_mae_c",
                    nonnegative=True,
                ),
                "train_residual_rmse_c": _decimal12(
                    raw.get("train_residual_rmse_c"),
                    "train_residual_rmse_c",
                    nonnegative=True,
                ),
                "holdout_case_set_sha256": _sha(
                    raw.get("holdout_case_set_sha256"), "holdout_case_set_sha256"
                ),
                "selected_holdout_mean_crps_c": _decimal12(
                    raw.get("selected_holdout_mean_crps_c"),
                    "selected_holdout_mean_crps_c",
                    nonnegative=True,
                ),
                "climatology_holdout_mean_crps_c": _decimal12(
                    raw.get("climatology_holdout_mean_crps_c"),
                    "climatology_holdout_mean_crps_c",
                    nonnegative=True,
                ),
                "fit_content_sha256": fit_sha,
                "content_sha256": _sha(
                    raw.get("content_sha256"),
                    "probability_distribution_artifact_content_sha256",
                ),
            }
        )
        if Decimal(result["selected_holdout_mean_crps_c"]) >= Decimal(
            result["climatology_holdout_mean_crps_c"]
        ):
            raise ContractError("canonical_probability_distribution_holdout_gate_failed")
        _verify_hash(result, "content_sha256", "probability_distribution_artifact_fact")
        return cls(result)

    def to_mapping(self) -> dict[str, Any]:
        return dict(self.raw)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(
            str(self.raw[key])
            for key in (
                "station_id",
                "settlement_timezone",
                "settlement_definition",
                "season_bin",
                "lead_bucket",
                "run_cycle_scope",
                "selection_content_sha256",
                "method_version",
                "train_scenario_set_sha256",
            )
        )


def _bucket_intervals(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ContractError("canonical_p0_bucket_intervals_required")
    result: list[dict[str, Any]] = []
    for expected_ordinal, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ContractError("canonical_p0_bucket_interval_invalid")
        _strict(item, _BUCKET_INTERVAL_FIELDS, "p0_bucket_interval")
        ordinal = _positive_int(item.get("ordinal"), "bucket_ordinal", allow_zero=True)
        if ordinal != expected_ordinal:
            raise ContractError("canonical_p0_bucket_ordinal_invalid")
        lower = item.get("lower_c")
        upper = item.get("upper_c")
        lower_value = None if lower is None else _decimal12(lower, "bucket_lower_c")
        upper_value = None if upper is None else _decimal12(upper, "bucket_upper_c")
        if ordinal == 0 and lower_value is not None:
            raise ContractError("canonical_p0_bucket_lower_tail_invalid")
        if ordinal == len(raw) - 1 and upper_value is not None:
            raise ContractError("canonical_p0_bucket_upper_tail_invalid")
        if ordinal not in {0, len(raw) - 1} and (lower_value is None or upper_value is None):
            raise ContractError("canonical_p0_bucket_internal_bound_missing")
        if lower_value is not None and upper_value is not None and Decimal(lower_value) >= Decimal(upper_value):
            raise ContractError("canonical_p0_bucket_interval_order_invalid")
        result.append(
            {
                "bucket_id": _text(item.get("bucket_id"), "bucket_id"),
                "ordinal": ordinal,
                "lower_c": lower_value,
                "upper_c": upper_value,
            }
        )
    if len({item["bucket_id"] for item in result}) != len(result):
        raise ContractError("canonical_p0_bucket_id_duplicate")
    for left, right in zip(result, result[1:]):
        if left["upper_c"] != right["lower_c"]:
            raise ContractError("canonical_p0_bucket_intervals_not_contiguous")
    return result


def _bucket_probabilities(raw: Any, intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) != len(intervals):
        raise ContractError("canonical_p0_bucket_probabilities_required")
    result: list[dict[str, Any]] = []
    total_count = 0
    total_probability = Decimal(0)
    for interval, item in zip(intervals, raw):
        if not isinstance(item, Mapping):
            raise ContractError("canonical_p0_bucket_probability_invalid")
        _strict(item, _BUCKET_PROBABILITY_FIELDS, "p0_bucket_probability")
        count = _positive_int(
            item.get("scenario_count"), "scenario_count", allow_zero=True
        )
        probability = _decimal12(
            item.get("probability"), "bucket_probability", nonnegative=True
        )
        expected_probability = format(Decimal(count) / Decimal(20), ".12f")
        if (
            item.get("bucket_id") != interval["bucket_id"]
            or item.get("ordinal") != interval["ordinal"]
            or probability != expected_probability
        ):
            raise ContractError("canonical_p0_bucket_probability_mismatch")
        total_count += count
        total_probability += Decimal(probability)
        result.append(
            {
                "bucket_id": interval["bucket_id"],
                "ordinal": interval["ordinal"],
                "scenario_count": count,
                "probability": probability,
            }
        )
    if total_count != 20 or total_probability != Decimal(1):
        raise ContractError("canonical_p0_probability_not_normalized")
    return result


@dataclass(frozen=True)
class CanonicalP0SnapshotFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalP0SnapshotFact":
        _strict(raw, _P0_FIELDS, "p0_snapshot_fact")
        _fixed_authority(raw, state="PRODUCTION_NO_LIVE_REPUBLISHER_P0_READY")
        if (
            raw.get("run_cycle") is not None
            or raw.get("run_cycle_scope") != RUN_CYCLE_SCOPE
            or raw.get("stage") != "P0"
            or raw.get("posterior_version")
            != "REPUBLISHER_NO_LIVE_TRAIN_RESIDUAL_BOOTSTRAP_V1"
            or raw.get("evidence_role") != "observational_candidate_not_oos"
            or raw.get("strict_oos_sample_eligible") is not False
        ):
            raise ContractError("canonical_p0_snapshot_role_invalid")
        intervals = _bucket_intervals(raw.get("bucket_intervals"))
        probabilities = _bucket_probabilities(raw.get("bucket_probabilities"), intervals)
        decision = _timestamp(raw.get("decision_at_utc"), "decision_at_utc")
        materialized = _timestamp(raw.get("materialized_at_utc"), "materialized_at_utc")
        fresh_until = _timestamp(raw.get("fresh_until_utc"), "fresh_until_utc")
        if not (_instant(decision) <= _instant(materialized) < _instant(fresh_until)):
            raise ContractError("canonical_p0_snapshot_freshness_invalid")
        generation = _positive_int(raw.get("activation_generation"), "activation_generation")
        if generation < 2:
            raise ContractError("canonical_p0_distribution_activation_required")
        probability_root = _sha(raw.get("probability_root_sha256"), "probability_root_sha256")
        expected_root = content_sha256(
            {
                "bucket_contract_sha256": str(raw.get("bucket_contract_sha256")),
                "market_bucket_bridge_sha256": str(raw.get("market_bucket_bridge_sha256")),
                "bucket_intervals": intervals,
                "bucket_probabilities": probabilities,
            }
        )
        if probability_root != expected_root:
            raise ContractError("canonical_p0_probability_root_mismatch")
        posterior_sha = _sha(raw.get("posterior_sha256"), "posterior_sha256")
        expected_posterior = content_sha256(
            {
                "selection_content_sha256": str(raw.get("selection_content_sha256")),
                "forward_applicability_content_sha256": str(
                    raw.get("forward_applicability_content_sha256")
                ),
                "probability_distribution_artifact_content_sha256": str(
                    raw.get("probability_distribution_artifact_content_sha256")
                ),
                "selected_forward_point_c": str(raw.get("selected_forward_point_c")),
                "scenario_temperature_set_sha256": str(
                    raw.get("scenario_temperature_set_sha256")
                ),
                "probability_root_sha256": probability_root,
            }
        )
        if posterior_sha != expected_posterior:
            raise ContractError("canonical_p0_posterior_hash_mismatch")
        snapshot_seed = {
            "station_id": str(raw.get("station_id")).upper(),
            "event_date": str(raw.get("event_date")),
            "decision_at_utc": decision,
            "activation_content_sha256": str(raw.get("activation_content_sha256")),
            "forward_applicability_content_sha256": str(
                raw.get("forward_applicability_content_sha256")
            ),
            "probability_distribution_artifact_content_sha256": str(
                raw.get("probability_distribution_artifact_content_sha256")
            ),
            "bucket_contract_sha256": str(raw.get("bucket_contract_sha256")),
        }
        snapshot_id = "republisher-no-live-p0-" + content_sha256(snapshot_seed)[:40]
        if raw.get("snapshot_id") != snapshot_id:
            raise ContractError("canonical_p0_snapshot_id_mismatch")
        result = dict(raw)
        result.update(
            {
                "snapshot_id": snapshot_id,
                "station_id": _text(raw.get("station_id"), "station_id").upper(),
                "event_date": _event_date(raw.get("event_date")),
                "settlement_timezone": _timezone(raw.get("settlement_timezone")),
                "settlement_definition": _text(
                    raw.get("settlement_definition"), "settlement_definition"
                ),
                "season_bin": _text(raw.get("season_bin"), "season_bin"),
                "lead_bucket": _text(raw.get("lead_bucket"), "lead_bucket"),
                "decision_at_utc": decision,
                "materialized_at_utc": materialized,
                "fresh_until_utc": fresh_until,
                "activation_id": _text(raw.get("activation_id"), "activation_id"),
                "activation_generation": generation,
                "activation_content_sha256": _sha(
                    raw.get("activation_content_sha256"), "activation_content_sha256"
                ),
                "selection_content_sha256": _sha(
                    raw.get("selection_content_sha256"), "selection_content_sha256"
                ),
                "forward_applicability_content_sha256": _sha(
                    raw.get("forward_applicability_content_sha256"),
                    "forward_applicability_content_sha256",
                ),
                "probability_distribution_artifact_content_sha256": _sha(
                    raw.get("probability_distribution_artifact_content_sha256"),
                    "probability_distribution_artifact_content_sha256",
                ),
                "bucket_contract_sha256": _sha(
                    raw.get("bucket_contract_sha256"), "bucket_contract_sha256"
                ),
                "market_bucket_bridge_sha256": _sha(
                    raw.get("market_bucket_bridge_sha256"),
                    "market_bucket_bridge_sha256",
                ),
                "bucket_intervals": intervals,
                "selected_forward_point_c": _decimal12(
                    raw.get("selected_forward_point_c"), "selected_forward_point_c"
                ),
                "scenario_temperature_set_sha256": _sha(
                    raw.get("scenario_temperature_set_sha256"),
                    "scenario_temperature_set_sha256",
                ),
                "bucket_probabilities": probabilities,
                "probability_root_sha256": probability_root,
                "posterior_sha256": posterior_sha,
                "content_sha256": _sha(
                    raw.get("content_sha256"), "p0_snapshot_content_sha256"
                ),
            }
        )
        _verify_hash(result, "content_sha256", "p0_snapshot_fact")
        return cls(result)

    def to_mapping(self) -> dict[str, Any]:
        return dict(self.raw)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(
            str(self.raw[key])
            for key in (
                "station_id",
                "event_date",
                "decision_at_utc",
                "lead_bucket",
                "activation_content_sha256",
                "forward_applicability_content_sha256",
                "probability_distribution_artifact_content_sha256",
                "bucket_contract_sha256",
            )
        )


@dataclass(frozen=True)
class CanonicalRepublisherNoLiveP0Contract:
    as_of_utc: str
    builder_id: str
    forward_applicability_facts: tuple[CanonicalForwardApplicabilityFact, ...]
    probability_distribution_artifact_facts: tuple[
        CanonicalProbabilityDistributionArtifactFact, ...
    ]
    p0_snapshot_facts: tuple[CanonicalP0SnapshotFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalRepublisherNoLiveP0Contract":
        _strict(raw, _CONTRACT_FIELDS, "republisher_no_live_p0_contract")
        if raw.get("schema") != CONTRACT_SCHEMA:
            raise ContractError("canonical_republisher_no_live_p0_contract_schema_invalid")
        lists = (
            raw.get("forward_applicability_facts"),
            raw.get("probability_distribution_artifact_facts"),
            raw.get("p0_snapshot_facts"),
        )
        if any(not isinstance(value, list) for value in lists) or not any(lists):
            raise ContractError("canonical_republisher_no_live_p0_contract_facts_required")
        forward = tuple(CanonicalForwardApplicabilityFact.from_mapping(item) for item in lists[0])
        distributions = tuple(
            CanonicalProbabilityDistributionArtifactFact.from_mapping(item)
            for item in lists[1]
        )
        p0 = tuple(CanonicalP0SnapshotFact.from_mapping(item) for item in lists[2])
        for facts, label in (
            (forward, "forward_applicability"),
            (distributions, "probability_distribution_artifact"),
            (p0, "p0_snapshot"),
        ):
            scopes = [item.scope_key for item in facts]
            if len(scopes) != len(set(scopes)):
                raise ContractError(f"canonical_{label}_scope_duplicate")
        as_of = _timestamp(raw.get("as_of_utc"), "as_of_utc")
        as_of_instant = _instant(as_of)
        if any(
            _instant(str(item.raw[field])) > as_of_instant
            for facts, field in ((forward, "decision_at_utc"), (p0, "materialized_at_utc"))
            for item in facts
        ):
            raise ContractError("canonical_republisher_no_live_p0_after_contract_asof")
        supplied = _verify_hash(
            raw, "content_sha256", "republisher_no_live_p0_contract"
        )
        return cls(
            as_of_utc=as_of,
            builder_id=_text(raw.get("builder_id"), "builder_id"),
            forward_applicability_facts=forward,
            probability_distribution_artifact_facts=distributions,
            p0_snapshot_facts=p0,
            content_sha256=supplied,
        )

    def to_mapping(self) -> dict[str, Any]:
        identity = {
            "schema": CONTRACT_SCHEMA,
            "as_of_utc": self.as_of_utc,
            "builder_id": self.builder_id,
            "forward_applicability_facts": [item.to_mapping() for item in self.forward_applicability_facts],
            "probability_distribution_artifact_facts": [item.to_mapping() for item in self.probability_distribution_artifact_facts],
            "p0_snapshot_facts": [item.to_mapping() for item in self.p0_snapshot_facts],
        }
        return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "AUTHORITY_SCOPE",
    "CONTRACT_SCHEMA",
    "CanonicalForwardApplicabilityFact",
    "CanonicalP0SnapshotFact",
    "CanonicalProbabilityDistributionArtifactFact",
    "CanonicalRepublisherNoLiveP0Contract",
    "PRODUCTS",
    "RUN_CYCLE_SCOPE",
    "SOURCE_CLASS",
    "TRANSPORT_PROVIDER_ID",
]
