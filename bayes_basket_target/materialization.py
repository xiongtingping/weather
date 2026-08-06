"""Fail-closed materialization of target probability checkpoints.

This module is the only boundary that turns a pure probability checkpoint
into a committed target-SSOT row.  It accepts no raw weather payload and has
no network, model-training, market, or gateway dependency.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from typing import Any, Mapping, Sequence

from .contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
    timestamp_lt,
    timestamp_lte,
)
from .et_history import assert_event_day_started
from .settlement_time import settlement_day_window
from .ssot import production_ssot_schema_status
from .resource_bounds import assert_connection_target_capacity
from .metar_trajectory import (
    METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
    METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION,
    MetarTrajectoryLikelihoodArtifact,
    MetarTrajectoryObservation,
    MetarTrajectoryUpdateProof,
    validate_metar_trajectory_update_proof,
)
from .taf_revision import (
    TafRevisionArtifact,
    TafRevisionProof,
    validate_taf_revision_proof,
)


MATERIALIZATION_SCHEMA = "bayes_basket_probability_materialization_v1"
_LINEAGE_FORBIDDEN_KEYS = frozenset(
    {
        "temperature",
        "metar",
        "speci",
        "taf",
        "nwp",
        "regime",
        "direction",
        "center_bucket",
    }
)

_NATIVE_FORWARD_CORE_KEYS = (
    "run_cycle_scope",
    "run_cycle_by_system_json",
    "run_init_utc_by_system_json",
    "archive_query_at_utc_by_system_json",
    "native_forward_availability_evidence_json",
    "native_forward_lineage_sha256",
)
_NATIVE_FORWARD_TAF_KEYS = (
    "taf_nwp_live_compatibility_json",
    "taf_nwp_live_compatibility_fingerprint_sha256",
)
_NATIVE_FORWARD_SCOPES = frozenset(
    {"SAME_NATIVE_CYCLE", "PER_SYSTEM_NATIVE_CYCLE"}
)
_NATIVE_RUN_CYCLES = frozenset({"00Z", "06Z", "12Z", "18Z"})
_MIXED_NATIVE_CYCLES = "MIXED_NATIVE_CYCLES"


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def _lineage_json_mapping(name: str, value: Any) -> dict[str, Any]:
    """Decode a content-addressed lineage JSON object without coercion."""

    try:
        decoded = json.loads(_text(name, value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(f"{name}_invalid_json") from exc
    if not isinstance(decoded, Mapping):
        raise ContractError(f"{name}_invalid_json")
    return {str(key): item for key, item in decoded.items()}


def _lineage_json_identity(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON object with a valid embedded content hash."""

    content = value.get("content_sha256")
    _sha(f"{name}_content_sha256", content)
    identity = {key: item for key, item in value.items() if key != "content_sha256"}
    if str(content).lower() != content_sha256(identity):
        raise ContractError(f"{name}_content_sha256_mismatch")
    return identity


def _native_forward_lineage(
    stage: str,
    *,
    root: ProbabilityEnvelope,
    predecessor: ProbabilityEnvelope,
) -> dict[str, str]:
    """Validate and return native-forward fields owned by the P0 root.

    A legacy deterministic/same-cycle envelope can omit all native-forward
    fields. Once any native-forward field is present, the materialization
    boundary requires the complete scope and evidence set. This keeps old
    same-cycle fixtures readable while making a mixed-cycle omission or
    mutation impossible to propagate into P0-M/P1.
    """

    root_lineage = root.lineage
    predecessor_lineage = predecessor.lineage

    def _has_core(lineage: Mapping[str, str]) -> bool:
        if any(key in lineage for key in _NATIVE_FORWARD_CORE_KEYS):
            return True
        compatibility = lineage.get("taf_nwp_live_compatibility_json")
        if compatibility is None:
            return False
        try:
            decoded = _lineage_json_mapping(
                "taf_nwp_live_compatibility_json", compatibility
            )
        except ContractError:
            return False
        return isinstance(decoded.get("native_forward_lineage"), Mapping)

    root_has_core = _has_core(root_lineage)
    predecessor_has_core = _has_core(predecessor_lineage)
    if not root_has_core:
        if predecessor_has_core:
            raise ContractError(f"{stage}_native_forward_root_lineage_missing")
        return {}
    # Open-Meteo/other republisher envelopes deliberately use an explicit
    # non-native scope. They remain on the legacy compatibility path and must
    # never be upgraded into official native evidence by this boundary.
    if root_lineage.get("run_cycle_scope") == "AGGREGATOR_NATIVE_CYCLE_UNKNOWN":
        return {}
    if not predecessor_has_core:
        raise ContractError(f"{stage}_native_forward_predecessor_lineage_missing")

    for key in _NATIVE_FORWARD_CORE_KEYS:
        if key not in root_lineage or not str(root_lineage.get(key) or "").strip():
            raise ContractError(f"{stage}_native_forward_lineage_missing:{key}")

    scope = _text("run_cycle_scope", root_lineage.get("run_cycle_scope"))
    run_cycle = _text("run_cycle", root_lineage.get("run_cycle"))
    if scope not in _NATIVE_FORWARD_SCOPES:
        raise ContractError(f"{stage}_native_forward_run_cycle_scope_invalid")
    mixed = scope == "PER_SYSTEM_NATIVE_CYCLE" or run_cycle == _MIXED_NATIVE_CYCLES
    if mixed and (scope != "PER_SYSTEM_NATIVE_CYCLE" or run_cycle != _MIXED_NATIVE_CYCLES):
        raise ContractError(f"{stage}_native_forward_mixed_scope_mismatch")
    if not mixed and (scope != "SAME_NATIVE_CYCLE" or run_cycle not in _NATIVE_RUN_CYCLES):
        raise ContractError(f"{stage}_native_forward_same_scope_mismatch")

    cycles = _lineage_json_mapping(
        "run_cycle_by_system_json", root_lineage["run_cycle_by_system_json"]
    )
    inits = _lineage_json_mapping(
        "run_init_utc_by_system_json", root_lineage["run_init_utc_by_system_json"]
    )
    queries = _lineage_json_mapping(
        "archive_query_at_utc_by_system_json",
        root_lineage["archive_query_at_utc_by_system_json"],
    )
    if set(cycles) != set(inits) or set(cycles) != set(queries):
        raise ContractError(f"{stage}_native_forward_system_lineage_incomplete")
    if mixed and not cycles:
        raise ContractError(f"{stage}_native_forward_system_lineage_empty")
    if not mixed and any(str(value or "").strip() != run_cycle for value in cycles.values()):
        raise ContractError(f"{stage}_native_forward_same_cycle_mismatch")

    for system_id in sorted(cycles):
        cycle = _text("run_cycle_by_system", cycles[system_id]).upper()
        if cycle not in _NATIVE_RUN_CYCLES:
            raise ContractError(f"{stage}_native_forward_cycle_invalid")
        if mixed or inits[system_id] or queries[system_id]:
            init = datetime.fromisoformat(
                _timestamp("run_init_utc_by_system", inits[system_id]).replace(
                    "Z", "+00:00"
                )
            )
            query = datetime.fromisoformat(
                _timestamp(
                    "archive_query_at_utc_by_system", queries[system_id]
                ).replace("Z", "+00:00")
            )
            if init.tzinfo is None or query.tzinfo is None or query < init:
                raise ContractError(f"{stage}_native_forward_timestamp_mismatch")
            if init.hour != int(cycle[:2]):
                raise ContractError(f"{stage}_native_forward_cycle_timestamp_mismatch")

    availability = _lineage_json_mapping(
        "native_forward_availability_evidence_json",
        root_lineage["native_forward_availability_evidence_json"],
    )
    _lineage_json_identity(
        "native_forward_availability_evidence", availability
    )
    availability_by_system = availability.get("by_system")
    if not isinstance(availability_by_system, Mapping):
        raise ContractError(f"{stage}_native_forward_availability_invalid")
    if set(str(key) for key in availability_by_system) != set(cycles):
        raise ContractError(f"{stage}_native_forward_availability_incomplete")
    for system_id in sorted(cycles):
        receipt = availability_by_system[system_id]
        if not isinstance(receipt, Mapping):
            raise ContractError(f"{stage}_native_forward_availability_invalid")
        if (
            str(receipt.get("run_cycle") or "") != str(cycles[system_id])
            or str(receipt.get("run_init_utc") or "") != str(inits[system_id])
            or str(receipt.get("archive_query_at_utc") or "")
            != str(queries[system_id])
        ):
            raise ContractError(f"{stage}_native_forward_availability_mismatch")

    native_lineage_sha = _sha(
        "native_forward_lineage_sha256",
        root_lineage["native_forward_lineage_sha256"],
    )
    compatibility_json = root_lineage.get("taf_nwp_live_compatibility_json")
    compatibility_fingerprint = root_lineage.get(
        "taf_nwp_live_compatibility_fingerprint_sha256"
    )
    compatibility: dict[str, Any] | None = None
    if compatibility_json:
        compatibility = _lineage_json_mapping(
            "taf_nwp_live_compatibility_json", compatibility_json
        )
        embedded_fingerprint = _sha(
            "taf_nwp_live_compatibility_fingerprint_sha256",
            compatibility.get("fingerprint_sha256"),
        )
        compatibility_identity = {
            key: item
            for key, item in compatibility.items()
            if key != "fingerprint_sha256"
        }
        if embedded_fingerprint != content_sha256(compatibility_identity):
            raise ContractError(
                f"{stage}_taf_nwp_live_compatibility_fingerprint_mismatch"
            )
        if compatibility_fingerprint is not None and _sha(
            "taf_nwp_live_compatibility_fingerprint_sha256", compatibility_fingerprint
        ) != embedded_fingerprint:
            raise ContractError(
                f"{stage}_taf_nwp_live_compatibility_fingerprint_mismatch"
            )
        native_in_compatibility = compatibility.get("native_forward_lineage")
        if not isinstance(native_in_compatibility, Mapping):
            raise ContractError(
                f"{stage}_taf_nwp_live_compatibility_native_lineage_missing"
            )
        if content_sha256(native_in_compatibility) != native_lineage_sha:
            raise ContractError(f"{stage}_native_forward_lineage_hash_mismatch")
        expected_native = {
            "run_cycle_scope": scope,
            "run_cycle": run_cycle,
            "run_cycle_by_system": cycles,
            "run_init_utc_by_system": inits,
            "archive_query_at_utc_by_system": queries,
            "availability_evidence": availability,
        }
        for key, expected in expected_native.items():
            if native_in_compatibility.get(key) != expected:
                raise ContractError(
                    f"{stage}_native_forward_compatibility_mismatch"
                )
    elif compatibility_fingerprint is None:
        raise ContractError(
            f"{stage}_taf_nwp_live_compatibility_missing"
        )
    else:
        _sha(
            "taf_nwp_live_compatibility_fingerprint_sha256",
            compatibility_fingerprint,
        )

    native_values = {"run_cycle": run_cycle}
    native_values.update({
        key: _text(key, root_lineage.get(key))
        for key in _NATIVE_FORWARD_CORE_KEYS
    })
    for key in _NATIVE_FORWARD_TAF_KEYS:
        if root_lineage.get(key) is not None:
            native_values[key] = _text(key, root_lineage[key])

    # Every descendant must carry exactly the root-owned native fields. A
    # changed JSON string is rejected even if its parsed object is equivalent;
    # this preserves the content-addressed root evidence byte-for-byte.
    for key, expected in native_values.items():
        supplied = predecessor_lineage.get(key)
        if supplied is None or str(supplied) != expected:
            raise ContractError(f"{stage}_native_forward_lineage_conflict:{key}")
    return native_values


def _likelihoods(
    probabilities: Mapping[str, float],
    raw: Mapping[str, Any],
    *,
    name: str,
) -> dict[str, float]:
    if not isinstance(raw, Mapping) or set(raw) != set(probabilities):
        raise ContractError(f"{name}_bucket_support_mismatch")
    result: dict[str, float] = {}
    for bucket in probabilities:
        value = raw[bucket]
        if isinstance(value, bool):
            raise ContractError(f"{name}_invalid")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"{name}_invalid") from exc
        if not math.isfinite(number) or number < 0.0:
            raise ContractError(f"{name}_invalid")
        result[str(bucket)] = number
    if not any(value > 0.0 for value in result.values()):
        raise ContractError(f"{name}_all_zero")
    return result


def _lineage(
    stage: str,
    raw: Mapping[str, Any],
    *,
    predecessor: ProbabilityEnvelope,
    required: Sequence[str],
) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ContractError("probability_lineage_invalid")
    values: dict[str, str] = {}
    for key, value in raw.items():
        key_text = _text("lineage_key", key)
        if key_text.lower() in _LINEAGE_FORBIDDEN_KEYS:
            raise ContractError("probability_lineage_weather_field_forbidden")
        values[key_text] = _text("lineage_value", value)
    for key in required:
        if key not in values:
            raise ContractError(f"{stage}_lineage_missing:{key}")
    values["predecessor_checkpoint_id"] = predecessor.checkpoint_id
    values["predecessor_content_sha256"] = predecessor.content_sha256
    values["materialization_schema"] = MATERIALIZATION_SCHEMA
    values["settlement_timezone"] = predecessor.settlement_timezone
    for key in (
        "selection_evidence_sha256",
        "ensemble_artifact_content_sha256",
        "ensemble_activation_content_sha256",
        "probability_pathway",
        "p0_probability_pathway",
        "season_bin",
        "lead_bucket",
        "run_cycle",
        "settlement_definition",
        "diagnostic_model_run_ids",
        "diagnostic_calibration_artifact_ids",
        "diagnostic_activation_ids",
        "diagnostic_model_set_hash",
        "model_envelope_lower_c",
        "model_envelope_upper_c",
        "model_envelope_span_c",
        "model_envelope_lower_bucket",
        "model_envelope_upper_bucket",
        "model_envelope_lower_ordinal",
        "model_envelope_upper_ordinal",
        "peripheral_model_reliability_by_bucket_json",
        "bucket_intervals_c_json",
        "bucket_centers_c_json",
        "formal_model_members_json",
        "formal_model_anchor_lower_bucket",
        "formal_model_anchor_upper_bucket",
        "formal_model_anchor_lower_ordinal",
        "formal_model_anchor_upper_ordinal",
        "p0_historical_tilt_proof_id",
        "p0_historical_tilt_proof_sha256",
        "p0_historical_tilt_artifact_id",
        "p0_historical_climatology_artifact_id",
        "p0_historical_applied_theta",
        "p0_historical_theta_zero_fallback",
        "conditional_future_temperature_curve_json",
        "conditional_future_temperature_curve_sha256",
        "conditional_future_temperature_curve_root_stage",
        "conditional_future_temperature_curve_root_checkpoint_id",
        "conditional_future_temperature_curve_root_probability_sha256",
    ):
        inherited = predecessor.lineage.get(key)
        if inherited is not None:
            supplied = values.get(key)
            if supplied is not None and supplied != inherited:
                raise ContractError("probability_model_envelope_lineage_conflict")
            values[key] = inherited
    return values


def _posterior(
    predecessor: ProbabilityEnvelope,
    *,
    stage: str,
    likelihoods: Mapping[str, float],
    hard_excluded_buckets: Sequence[str] = (),
    lineage: Mapping[str, str],
    checkpoint_id: str | None,
    posterior_version: str,
    decision_at_utc: str,
    fresh_until_utc: str,
    bridge_sha256: str,
) -> ProbabilityEnvelope:
    if predecessor.stage not in {"P0", "P0-M", "P1"}:
        raise ContractError("probability_predecessor_stage_invalid")
    if not timestamp_lt(predecessor.decision_at_utc, decision_at_utc):
        raise ContractError("probability_decision_time_rewind")
    if not timestamp_lt(decision_at_utc, fresh_until_utc):
        raise ContractError("probability_freshness_window_invalid")
    if stage == "P1":
        assert_event_day_started(
            predecessor.event_date,
            as_of_utc=decision_at_utc,
            target_timezone=predecessor.settlement_timezone,
        )
    excluded = {str(bucket) for bucket in hard_excluded_buckets}
    unknown = excluded - set(predecessor.probabilities)
    if unknown:
        raise ContractError("hard_exclusion_bucket_unknown")
    unnormalized = {
        bucket: predecessor.probabilities[bucket]
        * likelihoods[bucket]
        * (0.0 if bucket in excluded else 1.0)
        for bucket in predecessor.probabilities
    }
    total = sum(unnormalized.values())
    if not math.isfinite(total) or total <= 0.0:
        raise ContractError("probability_posterior_empty_after_hard_bound")
    probabilities = {bucket: value / total for bucket, value in unnormalized.items()}
    identity = {
        "schema": MATERIALIZATION_SCHEMA,
        "stage": stage,
        "predecessor_content_sha256": predecessor.content_sha256,
        "decision_at_utc": decision_at_utc,
        "fresh_until_utc": fresh_until_utc,
        "probabilities": probabilities,
        "likelihoods": dict(likelihoods),
        "hard_excluded_buckets": sorted(excluded),
        "lineage": dict(lineage),
        "posterior_version": posterior_version,
        "bridge_sha256": bridge_sha256,
    }
    posterior_sha256 = content_sha256(identity)
    return ProbabilityEnvelope.from_mapping({
        "stage": stage,
        "checkpoint_id": checkpoint_id or f"{stage.lower().replace('-', '').replace('^', 'r')}_{posterior_sha256[:32]}",
        "station_id": predecessor.station_id,
        "event_date": predecessor.event_date,
        "settlement_timezone": predecessor.settlement_timezone,
        "decision_at_utc": decision_at_utc,
        "fresh_until_utc": fresh_until_utc,
        "bucket_ordinals": dict(predecessor.bucket_ordinals),
        "probabilities": probabilities,
        "posterior_version": posterior_version,
        "posterior_sha256": posterior_sha256,
        "bridge_sha256": _sha("bridge_sha256", bridge_sha256),
        "lineage": dict(lineage),
    })


def materialize_p0m(
    predecessor: ProbabilityEnvelope | Mapping[str, Any],
    *,
    p0_root: ProbabilityEnvelope | Mapping[str, Any],
    taf_revision_artifact: TafRevisionArtifact | Mapping[str, Any],
    taf_revision_proof: TafRevisionProof | Mapping[str, Any],
    decision_at_utc: str,
    fresh_until_utc: str,
    bridge_sha256: str,
    checkpoint_id: str | None = None,
    posterior_version: str = "p0m_taf_target_v1",
    revision: bool = False,
) -> ProbabilityEnvelope:
    """Materialize TAF confirmation/correction as the P0-M checkpoint.

    The first TAF must consume NWP P0. Subsequent AMD/COR revisions consume
    P0-M. Every version is recomputed from the same immutable P0 root using
    an activated OOS artifact and a content-addressed bounded revision proof.
    No raw TAF/METAR/NWP payload or caller-authored likelihood ratio is
    accepted here.
    """

    previous = predecessor if isinstance(predecessor, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(predecessor)
    root = p0_root if isinstance(p0_root, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(p0_root)
    stage = "P0-M"
    if (previous.stage != "P0" and not revision) or (
        revision and previous.stage != "P0-M"
    ):
        raise ContractError("p0m_predecessor_stage_invalid")
    if root.stage != "P0":
        raise ContractError("p0m_root_stage_invalid")
    native_values = _native_forward_lineage(
        stage,
        root=root,
        predecessor=previous,
    )
    proof = validate_taf_revision_proof(
        taf_revision_proof,
        artifact=taf_revision_artifact,
        predecessor=previous,
        p0_root=root,
    )
    values = _lineage(
        stage,
        {
            **native_values,
            "forecast_revision_id": proof.forecast_revision_id,
            "forecast_source_object_sha256": proof.forecast_source_object_sha256,
            "taf_revision_artifact_id": proof.artifact_id,
            "taf_revision_artifact_sha256": proof.artifact_content_sha256,
            "taf_revision_activation_id": proof.activation_id,
            "taf_revision_proof_id": proof.proof_id,
            "taf_revision_proof_sha256": proof.content_sha256,
            "taf_revision_mode": proof.mode,
            "taf_probability_mass_transfer": f"{proof.probability_mass_transfer:.17g}",
            "taf_center_displacement_buckets": str(proof.center_displacement_buckets),
            "taf_applied_strength": f"{proof.applied_strength:.17g}",
            "taf_tx_calibrated_center_c": f"{proof.tx_calibrated_center_c:.17g}",
            "taf_tx_c": f"{proof.taf_tx_c:.17g}",
            "taf_physical_features_sha256": proof.physical_features_sha256,
            "taf_cloud_cover_fraction": (
                f"{proof.physical_features['cloud_cover_fraction']:.17g}"
            ),
            "taf_precipitation_probability": (
                f"{proof.physical_features['precipitation_probability']:.17g}"
            ),
            "taf_wind_speed_normalized": (
                f"{proof.physical_features['wind_speed_normalized']:.17g}"
            ),
            "taf_physically_confirmed_center_c": (
                f"{proof.physically_confirmed_center_c:.17g}"
            ),
            "taf_physical_adjustment_c": f"{proof.physical_adjustment_c:.17g}",
            "taf_physical_confirmation_state": proof.physical_confirmation_state,
            "taf_physical_consistency_factor": (
                f"{proof.physical_consistency_factor:.17g}"
            ),
            "taf_hard_exclusion": "FORBIDDEN",
        },
        predecessor=previous,
        required=("forecast_revision_id", "forecast_source_object_sha256"),
    )
    values["p0_root_checkpoint_id"] = root.checkpoint_id
    values["p0_root_content_sha256"] = root.content_sha256
    values["p0_root_posterior_sha256"] = root.posterior_sha256
    values["revision_mode"] = (
        "root_replacement" if revision else "first_taf_confirmation"
    )
    _sha("forecast_source_object_sha256", values["forecast_source_object_sha256"])
    return _posterior(
        root,
        stage=stage,
        likelihoods=_likelihoods(
            root.probabilities,
            proof.likelihood_ratio_by_bucket,
            name="forecast_likelihood_ratio",
        ),
        lineage=values,
        checkpoint_id=checkpoint_id,
        posterior_version=posterior_version,
        decision_at_utc=_timestamp("decision_at_utc", decision_at_utc),
        fresh_until_utc=_timestamp("fresh_until_utc", fresh_until_utc),
        bridge_sha256=bridge_sha256,
    )


def _p1_prediction_root_lineage(root: ProbabilityEnvelope) -> tuple[str, str]:
    """Return the immutable P0 reference for a legal P1 prediction root.

    P1 may be rooted at raw NWP P0 when no TAF checkpoint exists, or at P0-M
    after the TAF/physical confirmation is materialized.  The root stage,
    checkpoint and content hash are all written into P1 lineage, so neither
    form can be rebound across branches later.
    """

    if root.stage == "P0":
        return root.checkpoint_id, root.content_sha256
    if root.stage != "P0-M":
        raise ContractError("p1_prediction_root_stage_invalid")
    return (
        _text(
            "p1_prediction_root_p0_root_checkpoint_id",
            root.lineage.get("p0_root_checkpoint_id"),
        ),
        _sha(
            "p1_prediction_root_p0_root_content_sha256",
            root.lineage.get("p0_root_content_sha256"),
        ),
    )


def materialize_p1(
    predecessor: ProbabilityEnvelope | Mapping[str, Any],
    *,
    prediction_root: ProbabilityEnvelope | Mapping[str, Any],
    trajectory_artifact: MetarTrajectoryLikelihoodArtifact | Mapping[str, Any],
    trajectory_proof: MetarTrajectoryUpdateProof | Mapping[str, Any],
    trajectory_observations: Sequence[
        MetarTrajectoryObservation | Mapping[str, Any]
    ],
    bucket_centers_c: Mapping[str, Any],
    bucket_upper_bounds_c: Mapping[str, Any],
    reported_high_advanced: bool,
    decision_at_utc: str,
    fresh_until_utc: str,
    bridge_sha256: str,
    checkpoint_id: str | None = None,
    posterior_version: str = "p1_metar_trajectory_target_v2",
) -> ProbabilityEnvelope:
    """Recompute P1 from one immutable P0/P0-M root and full METAR prefix."""

    previous = predecessor if isinstance(predecessor, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(predecessor)
    root = (
        prediction_root
        if isinstance(prediction_root, ProbabilityEnvelope)
        else ProbabilityEnvelope.from_mapping(prediction_root)
    )
    p0_root_checkpoint_id, p0_root_content_sha256 = _p1_prediction_root_lineage(
        root
    )
    if not isinstance(reported_high_advanced, bool):
        raise ContractError("p1_reported_high_advanced_invalid")
    if previous.stage == "P1":
        if not timestamp_lt(previous.decision_at_utc, decision_at_utc):
            raise ContractError("probability_decision_time_rewind")
        if previous.lineage.get("prediction_root_stage") != root.stage:
            raise ContractError("p1_predecessor_root_stage_mismatch")
        previous_root = _text(
            "p1_predecessor_prediction_root_checkpoint_id",
            previous.lineage.get("prediction_root_checkpoint_id"),
        )
        if previous_root != root.checkpoint_id:
            raise ContractError("p1_prediction_root_branch_mismatch")
        previous_root_content = _sha(
            "p1_predecessor_prediction_root_content_sha256",
            previous.lineage.get("prediction_root_content_sha256"),
        )
        if previous_root_content != root.content_sha256:
            raise ContractError("p1_prediction_root_content_mismatch")
        if (
            _text(
                "p1_predecessor_p0_root_checkpoint_id",
                previous.lineage.get("p0_root_checkpoint_id"),
            )
            != p0_root_checkpoint_id
            or _sha(
                "p1_predecessor_p0_root_content_sha256",
                (
                    previous.lineage.get("p0_root_content_sha256")
                    or (
                        previous.lineage.get("prediction_root_content_sha256")
                        if str(
                            previous.lineage.get("prediction_root_stage") or ""
                        ).strip().upper() == "P0"
                        else None
                    )
                ),
            )
            != p0_root_content_sha256
        ):
            raise ContractError("p1_p0_root_branch_mismatch")
    else:
        if previous.stage != root.stage:
            raise ContractError("p1_initial_predecessor_root_stage_mismatch")
        if previous.checkpoint_id != root.checkpoint_id:
            raise ContractError("p1_initial_predecessor_must_equal_prediction_root")
        if previous.content_sha256 != root.content_sha256:
            raise ContractError("p1_initial_predecessor_content_mismatch")
    native_values = _native_forward_lineage(
        "P1",
        root=root,
        predecessor=previous,
    )
    proof = validate_metar_trajectory_update_proof(
        trajectory_proof,
        prediction_root=root,
        observations=trajectory_observations,
        artifact=trajectory_artifact,
        bucket_centers_c=bucket_centers_c,
        bucket_upper_bounds_c=bucket_upper_bounds_c,
    )
    governed_trajectory_artifact = (
        trajectory_artifact
        if isinstance(trajectory_artifact, MetarTrajectoryLikelihoodArtifact)
        else MetarTrajectoryLikelihoodArtifact.from_mapping(trajectory_artifact)
    )
    conditional_path = (
        governed_trajectory_artifact.method_version in {
            METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION,
            METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
        }
    )
    conditional_hourly_curve = (
        governed_trajectory_artifact.method_version
        == METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION
    )
    if conditional_path and proof.conditional_path_state_sha256 is None:
        raise ContractError("p1_conditional_path_state_proof_missing")
    future_curve_sha = str(
        root.lineage.get("conditional_future_temperature_curve_sha256") or ""
    ).strip().lower()
    if conditional_hourly_curve and (
        len(future_curve_sha) != 64
        or any(char not in "0123456789abcdef" for char in future_curve_sha)
    ):
        raise ContractError("p1_conditional_future_curve_lineage_missing")
    if proof.as_of_utc != decision_at_utc:
        raise ContractError("p1_trajectory_proof_decision_time_mismatch")
    last_event_id = proof.canonical_observation_ids[-1]
    normalized_observations = tuple(
        item
        if isinstance(item, MetarTrajectoryObservation)
        else MetarTrajectoryObservation.from_mapping(item)
        for item in trajectory_observations
    )
    last_matches = tuple(
        item
        for item in normalized_observations
        if item.event.observation_id == last_event_id
    )
    if len(last_matches) != 1:
        raise ContractError(
            "p1_canonical_observation_identity_ambiguous"
        )
    last_observation = last_matches[0]
    surviving_buckets = [
        bucket
        for bucket in root.probabilities
        if bucket not in set(proof.hard_excluded_buckets)
    ]
    if not surviving_buckets:
        raise ContractError("p1_reported_high_bucket_missing")
    reported_high_bucket = min(
        surviving_buckets,
        key=lambda bucket: (root.bucket_ordinals[bucket], bucket),
    )
    values = _lineage(
        "P1",
        {
            **native_values,
            "canonical_observation_event_id": last_event_id,
            "canonical_observation_sha256": (
                last_observation.event.raw_sha256
            ),
            "canonical_observation_committed_at": (
                last_observation.canonical_committed_at_utc
            ),
            "settlement_timezone": (
                last_observation.event.settlement_timezone
            ),
            "source_lane": last_observation.event.source_lane.upper(),
            "report_type": last_observation.event.report_type,
            "canonical_observation_sequence_sha256": (
                proof.canonical_observation_sequence_sha256
            ),
            "canonical_observation_ids": ",".join(
                proof.canonical_observation_ids
            ),
            "hard_bound_id": "hardbound-" + proof.content_sha256[:32],
            "metar_trajectory_artifact_id": proof.artifact_id,
            "metar_trajectory_artifact_sha256": (
                proof.artifact_content_sha256
            ),
            "metar_trajectory_activation_id": proof.activation_id,
            "metar_trajectory_proof_id": proof.proof_id,
            "metar_trajectory_proof_sha256": proof.content_sha256,
            "metar_conditional_path_state_sha256": (
                proof.conditional_path_state_sha256 or "NONE"
            ),
            "metar_future_temperature_curve_sha256": (
                future_curve_sha or "NONE"
            ),
            "metar_trajectory_applied_strength": (
                f"{proof.applied_strength:.17g}"
            ),
            "metar_trajectory_degradation_reasons": (
                ",".join(proof.degradation_reasons) or "NONE"
            ),
            "metar_trajectory_running_high_c": (
                f"{proof.running_high_c:.17g}"
            ),
            "reported_high_c": f"{proof.running_high_c:.17g}",
            "reported_high_bucket": reported_high_bucket,
            "reported_high_ordinal": str(
                root.bucket_ordinals[reported_high_bucket]
            ),
            "reported_high_advanced": (
                "true" if reported_high_advanced else "false"
            ),
            "metar_trajectory_direction": proof.direction,
            "metar_trajectory_direction_role": (
                "DIAGNOSTIC_ONLY_BASKET_RECOMPUTES_POLICY_DIRECTION"
            ),
            "metar_trajectory_center_bucket": proof.center_bucket,
            "metar_trajectory_positive_core_buckets": ",".join(
                proof.positive_core_buckets
            ),
            "metar_trajectory_positive_core_role": (
                "DIAGNOSTIC_POSITIVE_PROBABILITY_SUPPORT_NOT_BASKET_CORE"
            ),
            "observation_likelihood_mode": (
                "CONDITIONAL_HOURLY_FUTURE_PATH_SOFT_LR_PLUS_HARD_EXCLUSION_V4"
                if conditional_hourly_curve
                else "CONDITIONAL_FUTURE_PATH_SOFT_LR_PLUS_HARD_EXCLUSION_V3"
                if conditional_path
                else "FULL_PREFIX_ROOT_RECOMPUTE_SOFT_LR_PLUS_HARD_EXCLUSION_V2"
            ),
        },
        predecessor=previous,
        required=(
            "canonical_observation_event_id",
            "canonical_observation_sha256",
            "canonical_observation_committed_at",
            "settlement_timezone",
            "source_lane",
            "report_type",
            "hard_bound_id",
            "metar_trajectory_proof_sha256",
        ),
    )
    values["prediction_root_stage"] = root.stage
    values["prediction_root_checkpoint_id"] = root.checkpoint_id
    values["prediction_root_content_sha256"] = root.content_sha256
    values["p0_root_checkpoint_id"] = p0_root_checkpoint_id
    values["p0_root_content_sha256"] = p0_root_content_sha256
    _sha("canonical_observation_sha256", values["canonical_observation_sha256"])
    values["hard_excluded_buckets"] = (
        ",".join(proof.hard_excluded_buckets)
        or "NONE"
    )
    values["event_consumed_once"] = "full_prefix_content_addressed"
    return _posterior(
        root,
        stage="P1",
        likelihoods=_likelihoods(
            root.probabilities,
            proof.likelihood_ratio_by_bucket,
            name="observation_likelihood",
        ),
        hard_excluded_buckets=proof.hard_excluded_buckets,
        lineage=values,
        checkpoint_id=checkpoint_id,
        posterior_version=posterior_version,
        decision_at_utc=_timestamp("decision_at_utc", decision_at_utc),
        fresh_until_utc=_timestamp("fresh_until_utc", fresh_until_utc),
        bridge_sha256=bridge_sha256,
    )


def insert_probability_envelope(
    conn: sqlite3.Connection,
    envelope: ProbabilityEnvelope,
    *,
    created_at_utc: str,
    state: str = "COMMITTED",
) -> dict[str, Any]:
    """Append one validated envelope; equal replay is idempotent."""

    status = production_ssot_schema_status(conn)
    if not status["ok"]:
        raise ContractError("production_ssot_schema_incomplete")
    assert_connection_target_capacity(conn)
    if state not in {"COMMITTED", "QUARANTINED"}:
        raise ContractError("probability_state_invalid")
    if (
        envelope.stage == "P0"
        and envelope.lineage.get("probability_pathway")
        == "REPUBLISHER_NO_LIVE_P0_V1"
    ):
        try:
            authorized = int(
                conn.execute("SELECT canonical_p0_projection_authorized()").fetchone()[0]
            )
        except sqlite3.Error as exc:
            raise ContractError("republisher_p0_direct_envelope_writer_forbidden") from exc
        if authorized != 1:
            raise ContractError("republisher_p0_direct_envelope_writer_forbidden")
    settlement_day_window(envelope.event_date, envelope.settlement_timezone)
    created = _timestamp("created_at_utc", created_at_utc)
    if envelope.stage == "P1":
        assert_event_day_started(
            envelope.event_date,
            as_of_utc=envelope.decision_at_utc,
            target_timezone=envelope.settlement_timezone,
        )
    encoded_lineage = json.dumps(dict(envelope.lineage), ensure_ascii=False, sort_keys=True)
    existing = conn.execute(
        "SELECT content_sha256 FROM basket_probability_envelope WHERE envelope_id=?",
        (envelope.checkpoint_id,),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != envelope.content_sha256:
            raise ContractError("probability_checkpoint_id_content_conflict")
        return {"schema": MATERIALIZATION_SCHEMA, "state": "ALREADY_COMMITTED", "wrote": False, "envelope_id": envelope.checkpoint_id}
    same_stage_instant = conn.execute(
        "SELECT checkpoint_id,content_sha256 "
        "FROM basket_probability_envelope "
        "WHERE station_id=? AND event_date=? AND stage=? "
        "AND decision_at_utc=? AND state='COMMITTED'",
        (
            envelope.station_id,
            envelope.event_date,
            envelope.stage,
            envelope.decision_at_utc,
        ),
    ).fetchall()
    if same_stage_instant:
        # Equal replay is handled by the checkpoint-id branch above.  Two
        # different committed authorities for one stage and decision instant
        # would make stage routing insertion-order dependent, so reject the
        # second write instead of letting a later reader choose accidentally.
        raise ContractError("probability_stage_decision_time_conflict")
    if envelope.stage == "P1":
        event_id = envelope.lineage.get("canonical_observation_event_id")
        if not event_id:
            raise ContractError("p1_canonical_event_id_missing")
        for row in conn.execute(
            "SELECT lineage_json,content_sha256 FROM basket_probability_envelope WHERE station_id=? AND event_date=? AND stage='P1'",
            (envelope.station_id, envelope.event_date),
        ).fetchall():
            old_lineage = json.loads(str(row[0]))
            if (
                old_lineage.get("canonical_observation_event_id") == event_id
                and old_lineage.get("p0_root_checkpoint_id")
                == envelope.lineage.get("p0_root_checkpoint_id")
            ):
                if str(row[1]) == envelope.content_sha256:
                    return {"schema": MATERIALIZATION_SCHEMA, "state": "ALREADY_COMMITTED", "wrote": False, "envelope_id": envelope.checkpoint_id}
                raise ContractError("p1_canonical_event_already_consumed")
    conn.execute(
        """INSERT INTO basket_probability_envelope(
            envelope_id,stage,checkpoint_id,station_id,event_date,settlement_timezone,decision_at_utc,
            fresh_until_utc,bucket_ordinals_json,probabilities_json,posterior_version,
            posterior_sha256,bridge_sha256,lineage_json,content_sha256,state,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            envelope.checkpoint_id,
            envelope.stage,
            envelope.checkpoint_id,
            envelope.station_id,
            envelope.event_date,
            envelope.settlement_timezone,
            envelope.decision_at_utc,
            envelope.fresh_until_utc,
            json.dumps(dict(envelope.bucket_ordinals), ensure_ascii=False, sort_keys=True),
            json.dumps(dict(envelope.probabilities), ensure_ascii=False, sort_keys=True),
            envelope.posterior_version,
            envelope.posterior_sha256,
            envelope.bridge_sha256,
            encoded_lineage,
            envelope.content_sha256,
            state,
            created,
        ),
    )
    return {"schema": MATERIALIZATION_SCHEMA, "state": "COMMITTED", "wrote": True, "envelope_id": envelope.checkpoint_id}


__all__ = [
    "MATERIALIZATION_SCHEMA",
    "insert_probability_envelope",
    "materialize_p0m",
    "materialize_p1",
]
