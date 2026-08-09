"""Unique packet-to-probability materializer used by CLI and resident worker."""

from __future__ import annotations

import json
from typing import Any, Mapping

from .calibration import materialize_p0
from .contracts import (
    CalibrationActivation,
    ContractError,
    CurrentAsOfModelRun,
    ModelCalibrationArtifact,
    ModelEnsembleArtifact,
    ProbabilityEnvelope,
    content_sha256,
)
from .materialization import materialize_p0m, materialize_p1
from .native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    validate_cross_native_ensemble_weights,
)


ALLOWED_KEYS = {
    "stage", "predecessor", "current_model_runs", "diagnostic_model_runs",
    "calibration_artifacts", "diagnostic_calibration_artifacts",
    "ensemble_artifact", "calibration_activations", "bucket_intervals",
    "station_id", "event_date", "settlement_timezone",
    "p0_historical_tilt_proof", "p0_root",
    "taf_revision_artifact", "taf_revision_proof", "prediction_root",
    "trajectory_artifact", "trajectory_proof", "trajectory_observations",
    "bucket_centers_c", "bucket_upper_bounds_c",
    "reported_high_advanced",
    # P1 only: the canonical AWC revision the closing observation came from.
    # Distinct from the raw object hash the trajectory carries -- the truth
    # ledger is keyed on the revision, so an envelope without it compiles and
    # then fails at the atomic-fact boundary.
    "truth_observation_revision_content_sha256",
    "decision_at_utc", "fresh_until_utc",
    "bridge_sha256", "checkpoint_id", "posterior_version",
    "created_at_utc",
    "probability_pathway", "native_probability_envelope",
}


def build_probability_envelope(
    packet: Mapping[str, Any],
) -> ProbabilityEnvelope:
    unknown = sorted(set(packet) - ALLOWED_KEYS)
    if unknown:
        raise ContractError(
            "materialization_packet_unknown_fields:" + ",".join(unknown)
        )
    stage = str(packet.get("stage") or "")
    if stage == "P0":
        if packet.get("probability_pathway") == P0_PROBABILITY_PATHWAY:
            raw_native = packet.get("native_probability_envelope")
            if not isinstance(raw_native, Mapping):
                raise ContractError(
                    "native_ensemble_p0_envelope_missing"
                )
            envelope = ProbabilityEnvelope.from_mapping(raw_native)
            if (
                envelope.stage != "P0"
                or envelope.station_id
                != str(packet.get("station_id") or "")
                or envelope.event_date
                != str(packet.get("event_date") or "")
                or envelope.settlement_timezone
                != str(packet.get("settlement_timezone") or "")
                or envelope.decision_at_utc
                != str(packet.get("decision_at_utc") or "")
                or envelope.fresh_until_utc
                != str(packet.get("fresh_until_utc") or "")
            ):
                raise ContractError(
                    "native_ensemble_p0_scope_mismatch"
                )
            lineage = envelope.lineage
            if (
                lineage.get("probability_pathway")
                != P0_PROBABILITY_PATHWAY
                or lineage.get("p0_probability_pathway")
                != P0_PROBABILITY_PATHWAY
                or lineage.get("native_authority_scope")
                != "PRODUCTION_NO_LIVE_ONLY"
            ):
                raise ContractError(
                    "native_ensemble_p0_lineage_authority_invalid"
                )
            required_hashes = (
                "native_cross_ensemble_artifact_sha256",
                "native_selection_evidence_sha256",
                "native_cross_activation_sha256",
                "native_probability_evidence_sha256",
            )
            if any(
                len(str(lineage.get(key) or "")) != 64
                for key in required_hashes
            ):
                raise ContractError(
                    "native_ensemble_p0_lineage_hash_missing"
                )
            try:
                system_ids = json.loads(
                    str(lineage["ensemble_system_ids_json"])
                )
                family_ids = json.loads(
                    str(lineage["independence_family_ids_json"])
                )
                system_families = json.loads(
                    str(lineage["system_family_ids_json"])
                )
                system_weights = json.loads(
                    str(lineage["system_weights_json"])
                )
                family_caps = json.loads(
                    str(lineage["family_weight_caps_json"])
                )
                bucket_intervals = json.loads(
                    str(lineage["bucket_intervals_c_json"])
                )
            except (
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                raise ContractError(
                    "native_ensemble_p0_lineage_json_invalid"
                ) from exc
            if (
                not isinstance(system_ids, list)
                or len(system_ids) < 2
                or len(set(map(str, system_ids)))
                != len(system_ids)
                or not isinstance(family_ids, list)
                or len(set(map(str, family_ids))) < 2
                or not isinstance(system_families, Mapping)
                or set(map(str, system_ids))
                != set(map(str, system_families))
                or not isinstance(system_weights, Mapping)
                or set(map(str, system_ids))
                != set(map(str, system_weights))
                or not isinstance(family_caps, Mapping)
                or not isinstance(bucket_intervals, Mapping)
                or set(map(str, bucket_intervals))
                != set(envelope.probabilities)
            ):
                raise ContractError(
                    "native_ensemble_p0_lineage_shape_invalid"
                )
            weight_receipt = validate_cross_native_ensemble_weights(
                system_weights={
                    str(key): float(value)
                    for key, value in system_weights.items()
                },
                systems_by_id={
                    str(system_id): {
                        "independence_family_id": str(
                            system_families[str(system_id)]
                        ),
                    }
                    for system_id in system_ids
                },
                family_weight_caps={
                    str(key): float(value)
                    for key, value in family_caps.items()
                },
            )
            if set(weight_receipt["family_weights"]) != set(
                map(str, family_ids)
            ):
                raise ContractError(
                    "native_ensemble_p0_family_set_mismatch"
                )
            expected_bridge_sha = content_sha256({
                "bucket_intervals": bucket_intervals,
                "settlement_definition": lineage.get(
                    "settlement_definition"
                ),
                "settlement_timezone": envelope.settlement_timezone,
            })
            if envelope.bridge_sha256 != expected_bridge_sha:
                raise ContractError(
                    "native_ensemble_p0_bridge_sha_mismatch"
                )
            expected_posterior_sha = content_sha256({
                "station_id": envelope.station_id,
                "event_date": envelope.event_date,
                "decision_at_utc": envelope.decision_at_utc,
                "probabilities": dict(envelope.probabilities),
                "native_probability_evidence_sha256": lineage[
                    "native_probability_evidence_sha256"
                ],
            })
            if envelope.posterior_sha256 != expected_posterior_sha:
                raise ContractError(
                    "native_ensemble_p0_posterior_sha_mismatch"
                )
            return envelope
        raw_runs = packet.get("current_model_runs")
        raw_diagnostic_runs = packet.get("diagnostic_model_runs")
        raw_artifacts = packet.get("calibration_artifacts")
        raw_diagnostic_artifacts = packet.get(
            "diagnostic_calibration_artifacts"
        )
        raw_ensemble = packet.get("ensemble_artifact")
        raw_activations = packet.get("calibration_activations")
        raw_intervals = packet.get("bucket_intervals")
        station_id = str(packet.get("station_id") or "")
        event_date = str(packet.get("event_date") or "")
        if (
            not isinstance(raw_runs, list)
            or not isinstance(raw_diagnostic_runs, list)
            or not isinstance(raw_artifacts, Mapping)
            or not isinstance(raw_diagnostic_artifacts, Mapping)
        ):
            raise ContractError("p0m_calibration_inputs_missing")
        if (
            not isinstance(raw_ensemble, Mapping)
            or not isinstance(raw_activations, list)
        ):
            raise ContractError("p0m_ensemble_inputs_missing")
        if (
            not isinstance(raw_intervals, Mapping)
            or not station_id
            or not event_date
        ):
            raise ContractError("p0m_bucket_scope_missing")
        intervals = {}
        for bucket, bounds in raw_intervals.items():
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                raise ContractError("p0m_bucket_interval_invalid")
            intervals[str(bucket)] = (bounds[0], bounds[1])
        artifacts = {
            str(model): ModelCalibrationArtifact.from_mapping(value)
            for model, value in raw_artifacts.items()
            if isinstance(value, Mapping)
        }
        diagnostic_artifacts = {
            str(model): ModelCalibrationArtifact.from_mapping(value)
            for model, value in raw_diagnostic_artifacts.items()
            if isinstance(value, Mapping)
        }
        if len(artifacts) != len(raw_artifacts):
            raise ContractError("p0m_calibration_artifact_invalid")
        if len(diagnostic_artifacts) != len(raw_diagnostic_artifacts):
            raise ContractError("p0m_diagnostic_calibration_artifact_invalid")
        return materialize_p0(
            [
                CurrentAsOfModelRun.from_mapping(value)
                for value in raw_runs
            ],
            diagnostic_runs=[
                CurrentAsOfModelRun.from_mapping(value)
                for value in raw_diagnostic_runs
            ],
            calibration_artifacts=artifacts,
            diagnostic_calibration_artifacts=diagnostic_artifacts,
            ensemble_artifact=ModelEnsembleArtifact.from_mapping(
                raw_ensemble
            ),
            calibration_activations=[
                CalibrationActivation.from_mapping(value)
                for value in raw_activations
            ],
            bucket_intervals=intervals,
            station_id=station_id,
            event_date=event_date,
            settlement_timezone=packet.get("settlement_timezone"),
            decision_at_utc=packet.get("decision_at_utc"),
            fresh_until_utc=packet.get("fresh_until_utc"),
            bridge_sha256=packet.get("bridge_sha256"),
            historical_tilt_proof=packet.get(
                "p0_historical_tilt_proof"
            ),
            required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
        )
    predecessor = packet.get("predecessor")
    if not isinstance(predecessor, Mapping):
        raise ContractError("materialization_predecessor_missing")
    common = {
        "decision_at_utc": packet.get("decision_at_utc"),
        "fresh_until_utc": packet.get("fresh_until_utc"),
        "bridge_sha256": packet.get("bridge_sha256"),
        "checkpoint_id": packet.get("checkpoint_id"),
    }
    if packet.get("posterior_version") not in (None, ""):
        common["posterior_version"] = str(packet["posterior_version"])
    if stage == "P0-M":
        previous_stage = str(predecessor.get("stage") or "")
        return materialize_p0m(
            predecessor,
            p0_root=packet.get("p0_root"),
            taf_revision_artifact=packet.get("taf_revision_artifact"),
            taf_revision_proof=packet.get("taf_revision_proof"),
            revision=previous_stage == "P0-M",
            **common,
        )
    if stage == "P1":
        return materialize_p1(
            predecessor,
            prediction_root=packet.get("prediction_root"),
            trajectory_artifact=packet.get("trajectory_artifact"),
            trajectory_proof=packet.get("trajectory_proof"),
            trajectory_observations=(
                packet.get("trajectory_observations") or []
            ),
            bucket_centers_c=packet.get("bucket_centers_c"),
            bucket_upper_bounds_c=packet.get("bucket_upper_bounds_c"),
            reported_high_advanced=packet.get(
                "reported_high_advanced"
            ),
            # Empty means "the caller had no canonical store to resolve it
            # against", which is the historical-replay case; the envelope then
            # compiles without it and simply cannot become an atomic fact.
            truth_observation_revision_content_sha256=(
                str(
                    packet.get("truth_observation_revision_content_sha256")
                    or ""
                ).strip()
                or None
            ),
            **common,
        )
    raise ContractError("probability_materialization_stage_invalid")


__all__ = ["ALLOWED_KEYS", "build_probability_envelope"]
