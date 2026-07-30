"""Unique sequential P1 compiler for offline replay and resident ingestion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import ContractError, ProbabilityEnvelope, content_sha256
from .materialization import materialize_p1
from .metar_trajectory import (
    METAR_TRAJECTORY_AUTHORITY_SCOPE,
    METAR_TRAJECTORY_METHOD_VERSION,
    MetarTrajectoryLikelihoodArtifact,
    MetarTrajectoryObservation,
    build_metar_trajectory_likelihood_artifact,
    compile_metar_trajectory_update_proof,
)
from .stages import STAGE_PRECEDENCE


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("p1_packet_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("p1_packet_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def reported_hard_lower_in_unit(
    reported_temperature_c: float,
    unit: str,
) -> int:
    """Return the conservative integer settlement lower bound."""

    reported_c = float(reported_temperature_c)
    if unit == "C":
        return math.floor(reported_c)
    minimum_f = (reported_c - 0.5) * 9.0 / 5.0 + 32.0
    return math.floor(minimum_f + 0.5)


def _registry_bucket_bounds(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_station_id: str | None = None,
    expected_event_date: str | None = None,
    expected_settlement_timezone: str | None = None,
) -> tuple[str, dict[str, float | None]]:
    if not rows:
        raise ContractError("p1_registry_rows_missing")
    unit = str(rows[0].get("settlement_unit") or "").upper()
    if unit not in {"C", "F"}:
        raise ContractError("p1_registry_unit_invalid")
    result: dict[str, float | None] = {}
    for row in rows:
        if str(row.get("settlement_unit") or "").upper() != unit:
            raise ContractError("p1_registry_unit_mixed")
        if expected_station_id is not None and str(
            row.get("station_id") or ""
        ).strip().upper() != str(expected_station_id).strip().upper():
            raise ContractError("p1_registry_station_scope_mismatch")
        if expected_event_date is not None and str(
            row.get("event_date") or ""
        ).strip() != str(expected_event_date).strip():
            raise ContractError("p1_registry_event_date_scope_mismatch")
        if expected_settlement_timezone is not None and str(
            row.get("settlement_timezone") or ""
        ).strip() != str(expected_settlement_timezone).strip():
            raise ContractError("p1_registry_settlement_timezone_mismatch")
        try:
            registry = json.loads(str(row.get("registry_json") or ""))
        except (TypeError, ValueError) as exc:
            raise ContractError("p1_registry_json_invalid") from exc
        if not isinstance(registry, Mapping):
            raise ContractError("p1_registry_json_invalid")
        bucket = str(row.get("outcome") or registry.get("outcome") or "")
        if not bucket or bucket in result:
            raise ContractError("p1_registry_bucket_identity_invalid")
        upper = registry.get("upper")
        result[bucket] = None if upper is None else float(upper)
    return unit, result


def _hard_exclusion_threshold_c(value: float, unit: str) -> float:
    """First reported Celsius value whose conservative hard lower exceeds value."""

    if unit == "C":
        return float(value) + 1.0
    return (float(value) - 30.6) / 1.8


def _bucket_geometry_c(
    envelope: ProbabilityEnvelope,
    *,
    unit: str,
    upper_by_bucket: Mapping[str, float | None],
) -> tuple[dict[str, float], dict[str, float]]:
    centers_lineage = envelope.lineage.get("bucket_centers_c_json")
    intervals_lineage = envelope.lineage.get("bucket_intervals_c_json")
    if centers_lineage and intervals_lineage:
        try:
            centers = {
                str(bucket): float(value)
                for bucket, value in json.loads(centers_lineage).items()
            }
            intervals = json.loads(intervals_lineage)
            uppers = {
                str(bucket): (
                    float(bounds[1])
                    if bounds[1] is not None
                    else float(centers[str(bucket)]) + 0.5
                )
                for bucket, bounds in intervals.items()
            }
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ContractError("p1_probability_bucket_geometry_invalid") from exc
        if set(centers) == set(envelope.probabilities) == set(uppers):
            return centers, uppers
        raise ContractError("p1_probability_bucket_geometry_scope_mismatch")
    ordered = sorted(
        envelope.probabilities,
        key=lambda bucket: envelope.bucket_ordinals[bucket],
    )
    finite_uppers = [
        _hard_exclusion_threshold_c(float(upper), unit)
        for upper in upper_by_bucket.values()
        if upper is not None
    ]
    if not finite_uppers:
        raise ContractError("p1_registry_bucket_geometry_missing")
    step = min(
        (
            right - left
            for left, right in zip(
                sorted(set(finite_uppers)),
                sorted(set(finite_uppers))[1:],
            )
            if right > left
        ),
        default=(1.0 if unit == "C" else 5.0 / 9.0),
    )
    centers: dict[str, float] = {}
    uppers: dict[str, float] = {}
    previous_upper: float | None = None
    for bucket in ordered:
        raw_upper = upper_by_bucket[bucket]
        upper = (
            None
            if raw_upper is None
            else _hard_exclusion_threshold_c(float(raw_upper), unit)
        )
        if upper is None:
            if previous_upper is None:
                raise ContractError("p1_registry_bucket_geometry_missing")
            center = previous_upper + step
            effective_upper = center + step / 2.0
        elif previous_upper is None:
            center = upper - step / 2.0
            effective_upper = upper
        else:
            center = (previous_upper + upper) / 2.0
            effective_upper = upper
        centers[bucket] = center
        uppers[bucket] = effective_upper
        previous_upper = effective_upper
    return centers, uppers


def _local_time_bucket(as_of_utc: str, timezone_name: str) -> str:
    hour = _parse_utc(as_of_utc).astimezone(ZoneInfo(timezone_name)).hour
    if hour < 6:
        return "NIGHT"
    if hour < 12:
        return "MORNING"
    if hour < 18:
        return "AFTERNOON"
    return "EVENING"


def _fallback_trajectory_artifact(
    root: ProbabilityEnvelope,
    *,
    source_lane: str,
    maturity_bin: str,
    as_of_utc: str,
) -> MetarTrajectoryLikelihoodArtifact:
    """Create a content-addressed alpha-zero artifact for hard-bound continuity."""

    return build_metar_trajectory_likelihood_artifact({
        "activation_id": "NONE",
        "station_id": root.station_id,
        "source_lane": source_lane.lower(),
        "season_bin": str(root.lineage.get("season_bin") or "UNKNOWN"),
        "local_time_bucket": _local_time_bucket(
            as_of_utc, root.settlement_timezone
        ),
        "maturity_bin": maturity_bin,
        "settlement_definition": str(
            root.lineage.get("settlement_definition") or "UNKNOWN"
        ),
        "settlement_timezone": root.settlement_timezone,
        "feature_definitions": {
            "fallback": "identity LR; official running high remains hard",
        },
        "missing_observation_policy": "DEGRADE_STRENGTH",
        "expected_interval_minutes": 60,
        "long_interval_minutes": 90,
        "max_missing_intervals": 0,
        "missing_interval_strength_factor": 0.0,
        "long_interval_strength_factor": 0.0,
        "remaining_warming_distribution": {"0": 1.0},
        "hard_exclusion_baseline_distribution": {"0": 1.0},
        "base_strength": 0.0,
        "maturity_strength": 0.0,
        "likelihood_ratio_floor": 1.0,
        "likelihood_ratio_ceiling": 1.0,
        "sample_n": 1,
        "effective_sample_n": 1.0,
        "oos_metrics": {"brier_score": 0.0, "nll": 0.0, "crps": 0.0},
        "tail_calibration_error": 0.0,
        "trained_through_utc": "1970-01-01T00:00:00Z",
        "valid_from_utc": "1970-01-02T00:00:00Z",
        "valid_until_utc": "2100-01-01T00:00:00Z",
        "promotion_state": "CANDIDATE_ONLY",
        "authority_scope": METAR_TRAJECTORY_AUTHORITY_SCOPE,
        "activated": False,
        "method_version": METAR_TRAJECTORY_METHOD_VERSION,
    })


def compile_station_p1_packets(
    *,
    predecessor: ProbabilityEnvelope | Mapping[str, Any],
    prediction_root: ProbabilityEnvelope | Mapping[str, Any] | None = None,
    observations: Sequence[Mapping[str, Any]],
    registry_rows: Sequence[Mapping[str, Any]],
    compilation_at_utc: str,
    freshness_seconds: int = 3600,
    consumed_observation_ids: set[str] | None = None,
    trajectory_artifact: (
        MetarTrajectoryLikelihoodArtifact | Mapping[str, Any] | None
    ) = None,
    maturity_bin: str = "D0",
) -> list[dict[str, Any]]:
    """Compile every canonical P1 prefix; flag only running-high advances."""

    previous = (
        predecessor
        if isinstance(predecessor, ProbabilityEnvelope)
        else ProbabilityEnvelope.from_mapping(predecessor)
    )
    root = (
        previous
        if prediction_root is None and previous.stage in {"P0", "P0-M"}
        else (
            prediction_root
            if isinstance(prediction_root, ProbabilityEnvelope)
            else ProbabilityEnvelope.from_mapping(prediction_root)
            if isinstance(prediction_root, Mapping)
            else None
        )
    )
    if root is None or root.stage not in {"P0", "P0-M"}:
        raise ContractError("p1_prediction_root_missing")
    if (
        root.station_id != previous.station_id
        or root.event_date != previous.event_date
        or root.settlement_timezone != previous.settlement_timezone
    ):
        raise ContractError("p1_prediction_root_scope_mismatch")
    compile_at = _parse_utc(compilation_at_utc)
    if freshness_seconds <= 0:
        raise ContractError("p1_freshness_seconds_invalid")
    unit, upper_by_bucket = _registry_bucket_bounds(
        registry_rows,
        expected_station_id=previous.station_id,
        expected_event_date=previous.event_date,
        expected_settlement_timezone=previous.settlement_timezone,
    )
    if set(upper_by_bucket) != set(previous.probabilities):
        raise ContractError("p1_registry_probability_bucket_mismatch")
    if set(root.probabilities) != set(previous.probabilities):
        raise ContractError("p1_prediction_root_bucket_support_mismatch")
    bucket_centers_c, bucket_upper_bounds_c = _bucket_geometry_c(
        root,
        unit=unit,
        upper_by_bucket=upper_by_bucket,
    )
    consumed = set(consumed_observation_ids or ())
    ordered = sorted(
        observations,
        key=lambda item: (
            str(item.get("valid_at_utc") or ""),
            str(item.get("canonical_committed_at_utc") or ""),
            str(item.get("observation_id") or ""),
        ),
    )
    packets: list[dict[str, Any]] = []
    running_high_c: float | None = None
    generation = 0
    last_decision = _parse_utc(previous.decision_at_utc)
    trajectory_prefix: list[MetarTrajectoryObservation] = []
    for sequence, observation in enumerate(ordered, start=1):
        if (
            str(observation.get("qc_status") or "").upper() != "ACCEPTED"
            or str(observation.get("evidence_role") or "").upper() != "FORMAL"
            or bool(observation.get("correction_suspected"))
        ):
            continue
        if (
            str(observation.get("station_id") or "") != previous.station_id
            or str(observation.get("event_date_local") or "")
            != previous.event_date
        ):
            continue
        committed = _parse_utc(
            str(observation.get("canonical_committed_at_utc") or "")
        )
        valid_at = _parse_utc(str(observation.get("valid_at_utc") or ""))
        if committed > compile_at:
            raise ContractError("p1_canonical_commit_not_strict_asof")
        if valid_at > committed:
            raise ContractError("p1_canonical_observation_time_order_invalid")
        try:
            reported_c = float(observation["reported_temperature_c"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("p1_reported_temperature_invalid") from exc
        old_high = running_high_c
        running_high_c = (
            reported_c
            if running_high_c is None
            else max(running_high_c, reported_c)
        )
        observation_id = str(observation.get("observation_id") or "")
        if not observation_id:
            raise ContractError("p1_observation_id_missing")
        trajectory_prefix.append(
            MetarTrajectoryObservation.from_mapping({
                "event": {
                    "observation_id": observation_id,
                    "station_id": previous.station_id,
                    "event_date": previous.event_date,
                    "source_lane": str(
                        observation.get("source_lane") or ""
                    ).lower(),
                    "report_type": str(
                        observation.get("report_type") or "METAR"
                    ).upper(),
                    "sequence": sequence,
                    "observed_at_utc": _stamp(valid_at),
                    "received_at_utc": _stamp(committed),
                    "raw_sha256": str(
                        observation.get("raw_sha256") or ""
                    ).lower(),
                    "qc_state": "ACCEPTED",
                    "supersedes_observation_id": None,
                },
                "canonical_committed_at_utc": _stamp(committed),
                "temperature_c": reported_c,
            })
        )
        if observation_id in consumed:
            continue
        high_advanced = old_high is None or reported_c > old_high
        decision = max(
            committed,
            last_decision + timedelta(microseconds=1),
        )
        generation += 1
        decision_text = _stamp(decision)
        governed_artifact = (
            trajectory_artifact
            if isinstance(
                trajectory_artifact, MetarTrajectoryLikelihoodArtifact
            )
            else MetarTrajectoryLikelihoodArtifact.from_mapping(
                trajectory_artifact
            )
            if isinstance(trajectory_artifact, Mapping)
            else _fallback_trajectory_artifact(
                root,
                source_lane=str(
                    observation.get("source_lane") or ""
                ),
                maturity_bin=maturity_bin,
                as_of_utc=decision_text,
            )
        )
        proof = compile_metar_trajectory_update_proof(
            root,
            observations=trajectory_prefix,
            artifact=governed_artifact,
            bucket_centers_c=bucket_centers_c,
            bucket_upper_bounds_c=bucket_upper_bounds_c,
            maturity_bin=maturity_bin,
            as_of_utc=decision_text,
        )
        excluded = list(proof.hard_excluded_buckets)
        reported_value = reported_hard_lower_in_unit(running_high_c, unit)
        reported_high_bucket = proof.center_bucket
        reported_high_ordinal = root.bucket_ordinals[reported_high_bucket]
        bound_identity = {
            "schema": "bayes_basket_target_compiled_hard_bound_v2",
            "observation_id": observation_id,
            "station_id": previous.station_id,
            "event_date": previous.event_date,
            "bound_generation": generation,
            "reported_high_c": running_high_c,
            "hard_lower_bound": reported_value,
            "settlement_unit": unit,
            "hard_excluded_buckets": excluded,
        }
        hard_bound_id = "hardbound-" + proof.content_sha256[:32]
        fresh_until = decision + timedelta(seconds=int(freshness_seconds))
        envelope = materialize_p1(
            previous,
            prediction_root=root,
            trajectory_artifact=governed_artifact,
            trajectory_proof=proof,
            trajectory_observations=trajectory_prefix,
            bucket_centers_c=bucket_centers_c,
            bucket_upper_bounds_c=bucket_upper_bounds_c,
            reported_high_advanced=high_advanced,
            decision_at_utc=decision_text,
            fresh_until_utc=_stamp(fresh_until),
            bridge_sha256=root.bridge_sha256,
        )
        packets.append({
            "observation_id": observation_id,
            "predecessor": previous.to_dict(),
            "prediction_root": root.to_dict(),
            "trajectory_artifact": governed_artifact.to_dict(),
            "trajectory_proof": proof.to_dict(),
            "trajectory_observations": [
                item.to_dict() for item in trajectory_prefix
            ],
            "bucket_centers_c": bucket_centers_c,
            "bucket_upper_bounds_c": bucket_upper_bounds_c,
            "reported_high_advanced": high_advanced,
            "hard_excluded_buckets": excluded,
            "hard_bound_id": hard_bound_id,
            "decision_at_utc": decision_text,
            "fresh_until_utc": _stamp(fresh_until),
            "created_at_utc": _stamp(decision),
            "compiled_bound": {
                **bound_identity,
                "bound_id": hard_bound_id,
            },
            "expected_envelope": envelope.to_dict(),
        })
        previous = envelope
        last_decision = decision
    return packets


def select_p1_replay_base(
    envelopes: Sequence[ProbabilityEnvelope | Mapping[str, Any]],
    *,
    as_of_utc: str,
) -> tuple[
    ProbabilityEnvelope,
    ProbabilityEnvelope,
    set[str],
    bool,
]:
    """Select the newest dynamic P0 branch and incremental replay cursor.

    A replay cursor is durable history, not necessarily a currently
    consumable probability.  It may therefore be stale while a delayed
    formal observation is being applied.  The resident writer must append
    that deterministic replay result even when it is already stale, so the
    observation is consumed exactly once.  Stage selection independently
    rejects a stale P1 for decisions and never falls back to P0.
    """

    rows = [
        item
        if isinstance(item, ProbabilityEnvelope)
        else ProbabilityEnvelope.from_mapping(item)
        for item in envelopes
    ]
    p0_rows = [
        row
        for row in rows
        if row.stage == "P0" and row.decision_at_utc <= as_of_utc
    ]
    if not p0_rows:
        raise ContractError("p1_replay_p0_root_missing")
    latest_p0 = _select_replay_checkpoint(
        p0_rows,
        as_of_utc=as_of_utc,
    )
    root_id = latest_p0.checkpoint_id
    branch_taf = [
        row
        for row in rows
        if row.stage == "P0-M"
        and row.decision_at_utc <= as_of_utc
        and str(row.lineage.get("p0_root_checkpoint_id") or "")
        == root_id
    ]
    base = (
        _select_replay_checkpoint(
            branch_taf,
            as_of_utc=as_of_utc,
        )
        if branch_taf
        else latest_p0
    )
    branch_p1 = [
        row
        for row in rows
        if row.stage == "P1"
        and row.decision_at_utc <= as_of_utc
        and (
            str(
                row.lineage.get("prediction_root_checkpoint_id")
                or ""
            )
            == base.checkpoint_id
            or (
                not row.lineage.get("prediction_root_checkpoint_id")
                and str(row.lineage.get("p0_root_checkpoint_id") or "")
                == root_id
            )
        )
    ]
    if branch_p1:
        predecessor = _select_replay_checkpoint(
            branch_p1,
            as_of_utc=as_of_utc,
        )
        consumed = {
            str(
                row.lineage.get(
                    "canonical_observation_event_id"
                ) or ""
            )
            for row in branch_p1
        } - {""}
        return base, predecessor, consumed, False
    return base, base, set(), any(row.stage == "P1" for row in rows)


def _select_replay_checkpoint(
    rows: Sequence[ProbabilityEnvelope],
    *,
    as_of_utc: str,
) -> ProbabilityEnvelope:
    """Select one visible durable cursor without extending its freshness."""

    visible = [
        row for row in rows
        if row.decision_at_utc <= as_of_utc
    ]
    if not visible:
        raise ContractError("p1_replay_checkpoint_asof_missing")
    station_days = {
        (row.station_id, row.event_date)
        for row in visible
    }
    if len(station_days) != 1:
        raise ContractError("p1_replay_checkpoint_scope_mismatch")
    identities = [
        (row.stage, row.checkpoint_id)
        for row in visible
    ]
    if len(identities) != len(set(identities)):
        raise ContractError("p1_replay_checkpoint_duplicate")
    latest_at = max(row.decision_at_utc for row in visible)
    latest = [
        row for row in visible
        if row.decision_at_utc == latest_at
    ]
    highest = max(
        STAGE_PRECEDENCE[row.stage]
        for row in latest
    )
    winners = [
        row for row in latest
        if STAGE_PRECEDENCE[row.stage] == highest
    ]
    if len(winners) != 1:
        raise ContractError("p1_replay_checkpoint_ambiguous")
    return winners[0]


def materialization_packet(
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Project rich P1 evidence onto the strict materializer contract."""

    expected_raw = packet.get("expected_envelope")
    predecessor = packet.get("predecessor")
    if (
        not isinstance(expected_raw, Mapping)
        or not isinstance(predecessor, Mapping)
    ):
        raise ContractError("p1_materialization_projection_invalid")
    expected = ProbabilityEnvelope.from_mapping(expected_raw)
    return {
        "stage": "P1",
        "predecessor": dict(predecessor),
        "prediction_root": dict(packet.get("prediction_root") or {}),
        "trajectory_artifact": dict(
            packet.get("trajectory_artifact") or {}
        ),
        "trajectory_proof": dict(packet.get("trajectory_proof") or {}),
        "trajectory_observations": list(
            packet.get("trajectory_observations") or []
        ),
        "bucket_centers_c": dict(packet.get("bucket_centers_c") or {}),
        "bucket_upper_bounds_c": dict(
            packet.get("bucket_upper_bounds_c") or {}
        ),
        "reported_high_advanced": packet.get(
            "reported_high_advanced"
        ),
        "decision_at_utc": expected.decision_at_utc,
        "fresh_until_utc": expected.fresh_until_utc,
        "bridge_sha256": expected.bridge_sha256,
        "checkpoint_id": expected.checkpoint_id,
        "posterior_version": expected.posterior_version,
        "created_at_utc": str(
            packet.get("created_at_utc")
            or expected.decision_at_utc
        ),
    }


def canonical_materialization_packet(
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Project rich P1 evidence onto the canonical-ingress contract."""

    projected = materialization_packet(packet)
    observation_id = str(packet.get("observation_id") or "").strip()
    hard_bound_id = str(packet.get("hard_bound_id") or "").strip()
    if not observation_id:
        raise ContractError("p1_canonical_packet_observation_id_missing")
    if not hard_bound_id:
        raise ContractError("p1_canonical_packet_hard_bound_id_missing")
    expected_raw = packet.get("expected_envelope")
    lineage = (
        expected_raw.get("lineage")
        if isinstance(expected_raw, Mapping)
        else None
    )
    if (
        not isinstance(lineage, Mapping)
        or str(lineage.get("canonical_observation_event_id") or "")
        != observation_id
        or str(lineage.get("hard_bound_id") or "") != hard_bound_id
    ):
        raise ContractError("p1_canonical_packet_lineage_mismatch")
    return {
        **projected,
        "observation_id": observation_id,
        "hard_bound_id": hard_bound_id,
    }


__all__ = [
    "canonical_materialization_packet",
    "compile_station_p1_packets",
    "materialization_packet",
    "reported_hard_lower_in_unit",
    "select_p1_replay_base",
]
