"""Unique packet-to-probability materializer used by CLI and resident worker."""

from __future__ import annotations

from typing import Any, Mapping

from .calibration import materialize_p0
from .contracts import (
    CalibrationActivation,
    ContractError,
    CurrentAsOfModelRun,
    ModelCalibrationArtifact,
    ModelEnsembleArtifact,
    ProbabilityEnvelope,
)
from .materialization import materialize_p0m, materialize_p1


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
    "decision_at_utc", "fresh_until_utc",
    "bridge_sha256", "checkpoint_id", "posterior_version",
    "created_at_utc",
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
            **common,
        )
    raise ContractError("probability_materialization_stage_invalid")


__all__ = ["ALLOWED_KEYS", "build_probability_envelope"]
