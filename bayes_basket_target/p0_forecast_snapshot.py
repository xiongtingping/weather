"""Canonical unpublished P0 snapshot from an exact activation and forward receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .model_family_registry import assert_canonical_independence_family
from .model_oos_selection import (
    CanonicalModelOOSSelectionFact,
    assert_model_oos_selection_rebuild,
)
from .native_ensemble_oos_builder import build_native_ensemble_oos_event_inputs
from .native_ensemble_oos_contract import (
    COHORT_FACT_TYPE as NATIVE_ENSEMBLE_OOS_COHORT_FACT_TYPE,
    CanonicalNativeEnsembleOOSCohortFact,
)
from .market_bucket_bridge import CanonicalMarketBucketBridgeFact
from .p0_activation_root import (
    CanonicalP0ActivationRootFact,
    assert_p0_activation_root_rebuild,
)
from .p0_calibrated_distribution import CanonicalP0CalibratedDistributionFact


CONTRACT_SCHEMA = "bayes_basket_target_canonical_p0_forecast_snapshot_contract_v1"
RECEIPT_SCHEMA = "bayes_basket_target_canonical_p0_forward_component_receipt_v2"
FACT_TYPE = "p0_forecast_snapshot"
STATE = "MATERIALIZED_UNPUBLISHED"
Q = Decimal("0.000000000001")
RECEIPT_FIELDS = {
    "schema", "station_id", "model_id", "ensemble_system_id", "model_family",
    "provider_id", "provider_product_id", "independence_family_id", "source_class",
    "event_date", "settlement_timezone", "lead_bucket", "run_init_utc", "run_cycle",
    "model_available_at_utc", "retrieved_at_utc", "forecast_decision_at_utc",
    "forecast_value_c", "member_count", "member_evidence_vector_sha256",
    "raw_message_vector_sha256", "raw_object_id_vector_sha256",
    "source_packet_content_sha256", "source_capture_content_sha256",
    "retained_arrival_evidence_content_sha256",
    "model_input_content_sha256", "content_sha256",
}
FACT_FIELDS = {
    "station_id", "event_date", "settlement_timezone", "lead_bucket",
    "lead_bucket_scope", "run_cycle", "run_cycle_scope", "season_bin",
    "source_class", "authority_scope", "event_oos_protocol_version",
    "calibration_method_version", "available_model_set_hash",
    "activation_root_content_sha256", "selection_content_sha256",
    "calibrated_distribution_content_sha256", "component_forward_receipts",
    "component_forward_receipt_set_sha256", "forecast_decision_at_utc",
    "calibrated_point_c", "temperature_scenarios",
    "market_bucket_bridge_content_sha256", "market_registry_snapshot_sha256",
    "registry_scope_rowset_sha256", "bucket_probabilities",
    "bucket_probability_vector_sha256", "state", "content_sha256",
}


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"p0_forecast_snapshot_{label}_missing")
    return result


def _sha(value: Any, label: str) -> str:
    result = _text(value, label).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"p0_forecast_snapshot_{label}_invalid")
    return result


def _utc(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"p0_forecast_snapshot_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"p0_forecast_snapshot_{label}_timezone_required")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _optional_utc(value: Any, label: str) -> str | None:
    return None if value is None else _utc(value, label)


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(_text(value, label))
    except InvalidOperation as exc:
        raise ContractError(f"p0_forecast_snapshot_{label}_invalid") from exc
    if not result.is_finite():
        raise ContractError(f"p0_forecast_snapshot_{label}_invalid")
    return result


def _num(value: Decimal) -> str:
    return format(value.quantize(Q, rounding=ROUND_HALF_EVEN), ".12f")


def _receipt(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) != RECEIPT_FIELDS or raw.get("schema") != RECEIPT_SCHEMA:
        raise ContractError("p0_forecast_snapshot_component_receipt_fields_invalid")
    value = dict(raw)
    for field in (
        "station_id", "model_id", "model_family", "provider_id", "provider_product_id",
        "independence_family_id", "source_class", "event_date", "settlement_timezone",
        "lead_bucket",
    ):
        value[field] = _text(value[field], f"receipt_{field}")
    value["station_id"] = value["station_id"].upper()
    value["model_id"] = value["model_id"].upper()
    value["ensemble_system_id"] = None if value["ensemble_system_id"] is None else _text(value["ensemble_system_id"], "ensemble_system_id")
    value["run_cycle"] = None if value["run_cycle"] is None else _text(value["run_cycle"], "run_cycle")
    value["run_init_utc"] = _optional_utc(value["run_init_utc"], "run_init_utc")
    for field in ("model_available_at_utc", "retrieved_at_utc", "forecast_decision_at_utc"):
        value[field] = _utc(value[field], field)
    if not (value["model_available_at_utc"] <= value["retrieved_at_utc"] <= value["forecast_decision_at_utc"]):
        raise ContractError("p0_forecast_snapshot_component_clock_invalid")
    value["forecast_value_c"] = _num(_decimal(value["forecast_value_c"], "forecast_value_c"))
    if isinstance(value["member_count"], bool):
        raise ContractError("p0_forecast_snapshot_member_count_invalid")
    try:
        value["member_count"] = int(value["member_count"])
    except (TypeError, ValueError) as exc:
        raise ContractError("p0_forecast_snapshot_member_count_invalid") from exc
    if value["member_count"] < 2:
        raise ContractError("p0_forecast_snapshot_member_count_invalid")
    for field in (
        "member_evidence_vector_sha256", "raw_message_vector_sha256",
        "raw_object_id_vector_sha256", "source_packet_content_sha256",
        "source_capture_content_sha256",
        "retained_arrival_evidence_content_sha256",
        "model_input_content_sha256",
    ):
        value[field] = _sha(value[field], field)
    assert_canonical_independence_family(**value)
    supplied = _sha(value["content_sha256"], "receipt_content_sha256")
    if supplied != content_sha256({key: item for key, item in value.items() if key != "content_sha256"}):
        raise ContractError("p0_forecast_snapshot_component_receipt_hash_mismatch")
    value["content_sha256"] = supplied
    return value


def build_native_p0_forward_component_receipts(
    packet: Mapping[str, Any], *, source_packet_ref: str | Path,
    station_id: str,
) -> list[dict[str, Any]]:
    """Seal collection-valued native ensemble inputs without fake raw SHA.

    A native ensemble daily maximum is derived from hundreds of member-field
    messages.  The canonical receipt therefore binds the exact message and
    object-id vectors plus the normalized member-input manifest.  It must
    never rename a vector/set hash into singular ``raw_object_sha256``.
    """

    packet_sha = _sha(packet.get("content_sha256"), "source_packet_content_sha256")
    if packet_sha != content_sha256({
        key: value for key, value in packet.items() if key != "content_sha256"
    }):
        raise ContractError("p0_forecast_snapshot_source_packet_hash_mismatch")
    arrival = packet.get("retained_arrival_evidence")
    if not isinstance(arrival, Mapping):
        raise ContractError("p0_forecast_snapshot_retained_arrival_evidence_required")
    arrival_identity = {
        key: value for key, value in arrival.items() if key != "content_sha256"
    }
    arrival_sha = _sha(
        arrival.get("content_sha256"),
        "retained_arrival_evidence_content_sha256",
    )
    if (
        arrival.get("schema") != "official_native_retained_arrival_projection_v1"
        or arrival.get("network_download_count") != 0
        or arrival_sha != content_sha256(arrival_identity)
        or _utc(arrival.get("retained_at_utc"), "retained_at_utc")
        != _utc(packet.get("archive_query_at_utc"), "archive_query_at_utc")
    ):
        raise ContractError("p0_forecast_snapshot_retained_arrival_evidence_invalid")
    inputs = build_native_ensemble_oos_event_inputs(
        packet,
        source_packet_ref=source_packet_ref,
        station_ids=[station_id],
    )
    receipts: list[dict[str, Any]] = []
    for item in inputs:
        identity = {
            "schema": RECEIPT_SCHEMA,
            "station_id": str(item["station_id"]),
            "model_id": str(item["model_id"]),
            "ensemble_system_id": str(item["ensemble_system_id"]),
            "model_family": str(item["model_family"]),
            "provider_id": str(item["provider_id"]),
            "provider_product_id": str(item["provider_product_id"]),
            "independence_family_id": str(item["independence_family_id"]),
            "source_class": str(item["source_class"]),
            "event_date": str(item["event_date"]),
            "settlement_timezone": str(item["settlement_timezone"]),
            "lead_bucket": str(item["lead_bucket"]),
            "run_init_utc": _utc(item["run_init_utc"], "run_init_utc"),
            "run_cycle": str(item["run_cycle"]),
            "model_available_at_utc": _utc(
                item["official_object_available_by_utc"],
                "model_available_at_utc",
            ),
            "retrieved_at_utc": _utc(
                item["archive_query_at_utc"], "retrieved_at_utc"
            ),
            "forecast_decision_at_utc": _utc(
                item["archive_query_at_utc"], "forecast_decision_at_utc"
            ),
            "forecast_value_c": _num(
                _decimal(item["forecast_value"], "forecast_value_c")
            ),
            "member_count": int(item["member_count"]),
            "member_evidence_vector_sha256": str(
                item["member_evidence_vector_sha256"]
            ),
            "raw_message_vector_sha256": str(item["raw_message_vector_sha256"]),
            "raw_object_id_vector_sha256": str(
                item["raw_object_id_vector_sha256"]
            ),
            "source_packet_content_sha256": packet_sha,
            "source_capture_content_sha256": str(
                item["source_capture_content_sha256"]
            ),
            "retained_arrival_evidence_content_sha256": arrival_sha,
            "model_input_content_sha256": str(item["content_sha256"]),
        }
        receipt = {**identity, "content_sha256": content_sha256(identity)}
        receipts.append(_receipt(receipt))
    return sorted(receipts, key=lambda item: tuple(str(item[key] or "") for key in (
        "model_id", "ensemble_system_id", "model_family", "provider_id",
        "provider_product_id", "independence_family_id", "source_class",
    )))


@dataclass(frozen=True)
class CanonicalP0ForecastSnapshotFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalP0ForecastSnapshotFact":
        if set(raw) != FACT_FIELDS:
            raise ContractError("p0_forecast_snapshot_fact_fields_mismatch")
        value = dict(raw)
        for field in (
            "station_id", "event_date", "settlement_timezone", "lead_bucket",
            "lead_bucket_scope", "run_cycle_scope", "season_bin", "source_class",
            "authority_scope", "event_oos_protocol_version", "calibration_method_version",
        ):
            value[field] = _text(value[field], field)
        value["station_id"] = value["station_id"].upper()
        value["run_cycle"] = None if value["run_cycle"] is None else _text(value["run_cycle"], "run_cycle")
        if value["state"] != STATE or value["authority_scope"] != "PRODUCTION_NO_LIVE_ONLY":
            raise ContractError("p0_forecast_snapshot_authority_state_invalid")
        for field in (
            "available_model_set_hash", "activation_root_content_sha256",
            "selection_content_sha256", "calibrated_distribution_content_sha256",
            "component_forward_receipt_set_sha256", "market_bucket_bridge_content_sha256",
            "market_registry_snapshot_sha256", "registry_scope_rowset_sha256",
            "bucket_probability_vector_sha256",
        ):
            value[field] = _sha(value[field], field)
        receipts_raw = value["component_forward_receipts"]
        if not isinstance(receipts_raw, list) or len(receipts_raw) < 2:
            raise ContractError("p0_forecast_snapshot_component_receipts_required")
        receipts = [_receipt(item) for item in receipts_raw]
        identities = [{key: item[key] for key in (
            "model_id", "ensemble_system_id", "model_family", "provider_id",
            "provider_product_id", "independence_family_id", "source_class",
        )} for item in receipts]
        if identities != sorted(identities, key=lambda item: tuple(str(item[key] or "") for key in item)):
            raise ContractError("p0_forecast_snapshot_component_order_invalid")
        if value["component_forward_receipt_set_sha256"] != content_sha256(receipts):
            raise ContractError("p0_forecast_snapshot_component_set_hash_mismatch")
        value["component_forward_receipts"] = receipts
        value["forecast_decision_at_utc"] = _utc(value["forecast_decision_at_utc"], "forecast_decision_at_utc")
        value["calibrated_point_c"] = _num(_decimal(value["calibrated_point_c"], "calibrated_point_c"))
        scenarios = value["temperature_scenarios"]
        if not isinstance(scenarios, list) or len(scenarios) != 20:
            raise ContractError("p0_forecast_snapshot_temperature_scenarios_invalid")
        normalized = []
        for item in scenarios:
            if not isinstance(item, Mapping) or set(item) != {"temperature_c", "probability"}:
                raise ContractError("p0_forecast_snapshot_temperature_scenario_fields_invalid")
            normalized.append({
                "temperature_c": _num(_decimal(item["temperature_c"], "temperature_c")),
                "probability": _num(_decimal(item["probability"], "probability")),
            })
        if sum(Decimal(item["probability"]) for item in normalized) != Decimal("1"):
            raise ContractError("p0_forecast_snapshot_probability_sum_invalid")
        value["temperature_scenarios"] = normalized
        probabilities_raw = value["bucket_probabilities"]
        if not isinstance(probabilities_raw, list) or len(probabilities_raw) < 2:
            raise ContractError("p0_forecast_snapshot_bucket_probabilities_invalid")
        probabilities = []
        for ordinal, item in enumerate(probabilities_raw):
            if not isinstance(item, Mapping) or set(item) != {
                "ordinal", "outcome", "probability"
            } or item["ordinal"] != ordinal:
                raise ContractError("p0_forecast_snapshot_bucket_probability_fields_invalid")
            probabilities.append({
                "ordinal": ordinal,
                "outcome": _text(item["outcome"], "bucket_outcome"),
                "probability": _num(_decimal(item["probability"], "bucket_probability")),
            })
        if sum(Decimal(item["probability"]) for item in probabilities) != Decimal("1"):
            raise ContractError("p0_forecast_snapshot_bucket_probability_sum_invalid")
        if value["bucket_probability_vector_sha256"] != content_sha256(probabilities):
            raise ContractError("p0_forecast_snapshot_bucket_probability_hash_mismatch")
        value["bucket_probabilities"] = probabilities
        supplied = _sha(value["content_sha256"], "content_sha256")
        if supplied != content_sha256({key: item for key, item in value.items() if key != "content_sha256"}):
            raise ContractError("p0_forecast_snapshot_content_sha256_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(str(self.raw[key] if self.raw[key] is not None else "null") for key in (
            "station_id", "event_date", "settlement_timezone", "lead_bucket", "run_cycle",
            "available_model_set_hash", "forecast_decision_at_utc",
        ))


@dataclass(frozen=True)
class CanonicalP0ForecastSnapshotContract:
    as_of_utc: str
    builder_id: str
    snapshot_facts: tuple[CanonicalP0ForecastSnapshotFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalP0ForecastSnapshotContract":
        if set(raw) != {"schema", "as_of_utc", "builder_id", "snapshot_facts", "content_sha256"} or raw.get("schema") != CONTRACT_SCHEMA:
            raise ContractError("p0_forecast_snapshot_contract_invalid")
        facts_raw = raw["snapshot_facts"]
        if not isinstance(facts_raw, list) or not facts_raw:
            raise ContractError("p0_forecast_snapshot_fact_required")
        facts = tuple(CanonicalP0ForecastSnapshotFact.from_mapping(item) for item in facts_raw)
        if len({item.scope_key for item in facts}) != len(facts):
            raise ContractError("p0_forecast_snapshot_scope_duplicate")
        as_of = _utc(raw["as_of_utc"], "contract_as_of_utc")
        if any(item.raw["forecast_decision_at_utc"] > as_of for item in facts):
            raise ContractError("p0_forecast_snapshot_fact_after_asof")
        supplied = _sha(raw["content_sha256"], "contract_content_sha256")
        if supplied != content_sha256({key: item for key, item in raw.items() if key != "content_sha256"}):
            raise ContractError("p0_forecast_snapshot_contract_hash_mismatch")
        return cls(as_of, _text(raw["builder_id"], "builder_id"), facts, supplied)


def _load(conn: sqlite3.Connection, fact_type: str, sha: str, cls: Any) -> Any:
    rows = conn.execute("SELECT fact_json FROM canonical_ssot_fact WHERE fact_type=? AND content_sha256=?", (fact_type, _sha(sha, f"{fact_type}_sha256"))).fetchall()
    if len(rows) != 1:
        raise ContractError(f"p0_forecast_snapshot_{fact_type}_not_unique")
    return cls.from_mapping(json.loads(str(rows[0][0])))


def build_p0_forecast_snapshot_contract(
    conn: sqlite3.Connection, *, activation_root_content_sha256: str,
    market_bucket_bridge_content_sha256: str,
    component_forward_receipts: Sequence[Mapping[str, Any]], as_of_utc: str,
) -> dict[str, Any]:
    activation = _load(conn, "p0_activation_root", activation_root_content_sha256, CanonicalP0ActivationRootFact)
    # Revalidate every persisted ancestor before any forward receipt is
    # combined with it.  This closes the historical-writer hole in which an
    # OOS_REJECTED component had descendants whose individual hashes were
    # valid but whose calibration chain was not.
    assert_p0_activation_root_rebuild(conn, activation)
    selection = _load(conn, "p0_model_family_selection", activation.raw["selection_content_sha256"], CanonicalModelOOSSelectionFact)
    assert_model_oos_selection_rebuild(conn, selection)
    distribution = _load(conn, "p0_calibrated_distribution", activation.raw["calibrated_distribution_content_sha256"], CanonicalP0CalibratedDistributionFact)
    bridge = _load(
        conn, "market_bucket_bridge", market_bucket_bridge_content_sha256,
        CanonicalMarketBucketBridgeFact,
    )
    receipts = [_receipt(item) for item in component_forward_receipts]
    receipts.sort(key=lambda item: tuple(str(item[key] or "") for key in (
        "model_id", "ensemble_system_id", "model_family", "provider_id",
        "provider_product_id", "independence_family_id", "source_class",
    )))
    component_ids = [{key: item[key] for key in (
        "model_id", "ensemble_system_id", "model_family", "provider_id",
        "provider_product_id", "independence_family_id", "source_class",
    )} for item in receipts]
    selected_ids = [{key: item[key] for key in component_ids[0]} for item in selection.raw["component_cohort_refs"]]
    if component_ids != selected_ids or content_sha256(component_ids) != selection.raw["available_model_set_hash"]:
        raise ContractError("p0_forecast_snapshot_available_model_set_mismatch")
    common = ("station_id", "event_date", "settlement_timezone", "lead_bucket", "forecast_decision_at_utc")
    if any(any(item[field] != receipts[0][field] for field in common) for item in receipts[1:]):
        raise ContractError("p0_forecast_snapshot_component_scope_mismatch")
    for field in ("station_id", "settlement_timezone", "lead_bucket", "source_class"):
        if receipts[0][field] != activation.raw[field]:
            raise ContractError("p0_forecast_snapshot_activation_scope_mismatch")
    if activation.raw["run_cycle"] is not None and any(item["run_cycle"] != activation.raw["run_cycle"] for item in receipts):
        raise ContractError("p0_forecast_snapshot_run_cycle_mismatch")
    if any(item["forecast_decision_at_utc"] > _utc(as_of_utc, "as_of_utc") for item in receipts):
        raise ContractError("p0_forecast_snapshot_component_after_asof")
    cohorts = []
    for item in selection.raw["component_cohort_refs"]:
        # Formal P0 selections are native-ensemble only.  Loading these
        # references through the retired deterministic cohort fact type makes
        # a valid native selection look absent and silently re-opens the old
        # parallel lineage.  The recorded fact type is authoritative; there is
        # no cross-table fallback.
        if item["cohort_fact_type"] != NATIVE_ENSEMBLE_OOS_COHORT_FACT_TYPE:
            raise ContractError(
                "p0_forecast_snapshot_formal_native_cohort_required"
            )
        cohorts.append(
            _load(
                conn,
                NATIVE_ENSEMBLE_OOS_COHORT_FACT_TYPE,
                item["cohort_content_sha256"],
                CanonicalNativeEnsembleOOSCohortFact,
            )
        )
    point = sum(
        Decimal(component["weight"]) * (Decimal(receipt["forecast_value_c"]) + Decimal(str(cohort.raw["calibration_parameters"]["bias_c"])))
        for component, receipt, cohort in zip(selection.raw["component_cohort_refs"], receipts, cohorts)
    )
    scenarios = [{
        "temperature_c": _num(point + Decimal(item["residual_c"])),
        "probability": item["probability"],
    } for item in distribution.raw["train_residual_scenarios"]]
    if any(
        bridge.raw[field] != receipts[0][field]
        for field in ("station_id", "event_date", "settlement_timezone")
    ):
        raise ContractError("p0_forecast_snapshot_market_bridge_scope_mismatch")
    bucket_probabilities = []
    for bucket in bridge.raw["bucket_contracts"]:
        lower = None if bucket["lower_c"] is None else Decimal(bucket["lower_c"])
        upper = None if bucket["upper_c"] is None else Decimal(bucket["upper_c"])
        probability = sum(
            (
                Decimal(item["probability"])
                for item in scenarios
                if (lower is None or Decimal(item["temperature_c"]) >= lower)
                and (upper is None or Decimal(item["temperature_c"]) < upper)
            ),
            Decimal("0"),
        )
        bucket_probabilities.append({
            "ordinal": bucket["ordinal"], "outcome": bucket["outcome"],
            "probability": _num(probability),
        })
    if sum(Decimal(item["probability"]) for item in bucket_probabilities) != Decimal("1"):
        raise ContractError("p0_forecast_snapshot_bucket_probability_sum_invalid")
    inherited = ("settlement_timezone", "lead_bucket", "lead_bucket_scope", "run_cycle", "run_cycle_scope", "season_bin", "source_class", "authority_scope", "event_oos_protocol_version", "calibration_method_version", "available_model_set_hash")
    identity = {
        "station_id": activation.raw["station_id"], "event_date": receipts[0]["event_date"],
        **{field: activation.raw[field] for field in inherited},
        "activation_root_content_sha256": activation.content_sha256,
        "selection_content_sha256": selection.content_sha256,
        "calibrated_distribution_content_sha256": distribution.content_sha256,
        "component_forward_receipts": receipts,
        "component_forward_receipt_set_sha256": content_sha256(receipts),
        "forecast_decision_at_utc": receipts[0]["forecast_decision_at_utc"],
        "calibrated_point_c": _num(point), "temperature_scenarios": scenarios,
        "market_bucket_bridge_content_sha256": bridge.content_sha256,
        "market_registry_snapshot_sha256": bridge.raw["market_registry_snapshot_sha256"],
        "registry_scope_rowset_sha256": bridge.raw["registry_scope_rowset_sha256"],
        "bucket_probabilities": bucket_probabilities,
        "bucket_probability_vector_sha256": content_sha256(bucket_probabilities),
        "state": STATE,
    }
    fact = {**identity, "content_sha256": content_sha256(identity)}
    contract_identity = {"schema": CONTRACT_SCHEMA, "as_of_utc": _utc(as_of_utc, "as_of_utc"), "builder_id": "canonical.p0_forecast_snapshot.v1", "snapshot_facts": [fact]}
    contract = {**contract_identity, "content_sha256": content_sha256(contract_identity)}
    CanonicalP0ForecastSnapshotContract.from_mapping(contract)
    return contract


def assert_p0_forecast_snapshot_rebuild(
    conn: sqlite3.Connection, snapshot: CanonicalP0ForecastSnapshotFact,
) -> None:
    """Fail closed unless a persisted forward P0 still rebuilds exactly.

    This is intentionally a consumer-side check as well as an ingress check:
    immutable historical rows may have been produced before a parent gate was
    tightened.  Replaying the exact forward receipts and bridge prevents such
    rows from becoming a P1 or basket parent later.
    """

    rebuilt = build_p0_forecast_snapshot_contract(
        conn,
        activation_root_content_sha256=snapshot.raw[
            "activation_root_content_sha256"
        ],
        market_bucket_bridge_content_sha256=snapshot.raw[
            "market_bucket_bridge_content_sha256"
        ],
        component_forward_receipts=snapshot.raw["component_forward_receipts"],
        as_of_utc=snapshot.raw["forecast_decision_at_utc"],
    )
    expected = CanonicalP0ForecastSnapshotContract.from_mapping(
        rebuilt
    ).snapshot_facts[0]
    if expected.raw != snapshot.raw:
        raise ContractError("p0_forecast_snapshot_reference_rebuild_mismatch")


__all__ = [
    "CONTRACT_SCHEMA", "RECEIPT_SCHEMA", "CanonicalP0ForecastSnapshotContract",
    "CanonicalP0ForecastSnapshotFact", "assert_p0_forecast_snapshot_rebuild",
    "build_native_p0_forward_component_receipts",
    "build_p0_forecast_snapshot_contract",
]
