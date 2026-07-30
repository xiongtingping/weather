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
    proof = validate_taf_revision_proof(
        taf_revision_proof,
        artifact=taf_revision_artifact,
        predecessor=previous,
        p0_root=root,
    )
    values = _lineage(
        stage,
        {
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
    """Recompute P1 from one immutable P0-M/P0 root and full METAR prefix."""

    previous = predecessor if isinstance(predecessor, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(predecessor)
    root = (
        prediction_root
        if isinstance(prediction_root, ProbabilityEnvelope)
        else ProbabilityEnvelope.from_mapping(prediction_root)
    )
    if root.stage not in {"P0", "P0-M"}:
        raise ContractError("p1_prediction_root_stage_invalid")
    if not isinstance(reported_high_advanced, bool):
        raise ContractError("p1_reported_high_advanced_invalid")
    if previous.stage == "P1":
        if not timestamp_lt(previous.decision_at_utc, decision_at_utc):
            raise ContractError("probability_decision_time_rewind")
        previous_root = str(
            previous.lineage.get("prediction_root_checkpoint_id")
            or previous.lineage.get("p0_root_checkpoint_id")
            or ""
        )
        if previous_root and previous_root != root.checkpoint_id:
            raise ContractError("p1_prediction_root_branch_mismatch")
    elif previous.checkpoint_id != root.checkpoint_id:
        raise ContractError("p1_initial_predecessor_must_equal_prediction_root")
    proof = validate_metar_trajectory_update_proof(
        trajectory_proof,
        prediction_root=root,
        observations=trajectory_observations,
        artifact=trajectory_artifact,
        bucket_centers_c=bucket_centers_c,
        bucket_upper_bounds_c=bucket_upper_bounds_c,
    )
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
            "canonical_observation_event_id": last_event_id,
            "canonical_observation_sha256": (
                last_observation.event.raw_sha256
            ),
            "canonical_observation_committed_at": (
                last_observation.canonical_committed_at_utc
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
                "FULL_PREFIX_ROOT_RECOMPUTE_SOFT_LR_PLUS_HARD_EXCLUSION_V2"
            ),
        },
        predecessor=previous,
        required=(
            "canonical_observation_event_id",
            "canonical_observation_sha256",
            "canonical_observation_committed_at",
            "source_lane",
            "report_type",
            "hard_bound_id",
            "metar_trajectory_proof_sha256",
        ),
    )
    values["prediction_root_stage"] = root.stage
    values["prediction_root_checkpoint_id"] = root.checkpoint_id
    values["prediction_root_content_sha256"] = root.content_sha256
    values["p0_root_checkpoint_id"] = (
        root.checkpoint_id
        if root.stage == "P0"
        else str(root.lineage.get("p0_root_checkpoint_id") or "")
    )
    if not values["p0_root_checkpoint_id"]:
        raise ContractError("p1_p0_root_checkpoint_id_missing")
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
