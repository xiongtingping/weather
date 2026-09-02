"""Unique sequential P1 compiler for offline replay and resident ingestion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import (
    MAX_REPORT_TRANSMISSION_LEAD_SECONDS,
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from .materialization import materialize_p1
from .metar_trajectory import (
    METAR_TRAJECTORY_AUTHORITY_SCOPE,
    METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
    METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION,
    METAR_TRAJECTORY_METHOD_VERSION,
    METAR_TRAJECTORY_PHYSICAL_ABSOLUTE_TMAX_METHOD_VERSION,
    SOFT_PATH_SOURCE_LANES,
    MetarTrajectoryLikelihoodArtifact,
    MetarTrajectoryObservation,
    build_metar_trajectory_likelihood_artifact,
    compile_metar_trajectory_update_proof,
)
from .stages import STAGE_PRECEDENCE


_MAX_TRANSMISSION_LEAD = timedelta(
    seconds=MAX_REPORT_TRANSMISSION_LEAD_SECONDS
)


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
    """First reported Celsius value whose conservative hard lower exceeds value.

    ``value`` is the bucket's upper label in market units, so the bucket dies
    at the first report one rung above it: ``value + 1`` degrees C, or
    ``value + 1`` degrees F expressed in Celsius.  The margin has to be taken
    on the *reporting* grid, not in Celsius for both -- C stations report whole
    degrees C, F stations report whole degrees F, which is 5/9 C.  Adding a
    whole Celsius degree on an F market overshoots the next report by 0.4F and
    leaves the bucket alive one rung too long: KSFO 2026-08-20 reached 68F
    (20.0C) with the 66-67F bucket, dead above 67.5F, still holding 27.2% of
    the posterior because the threshold had been placed at 20.22C.
    """

    if unit == "C":
        return float(value) + 1.0
    return (float(value) + 1.0 - 32.0) / 1.8


def _bucket_geometry_c(
    envelope: ProbabilityEnvelope,
    *,
    unit: str,
    upper_by_bucket: Mapping[str, float | None],
) -> tuple[dict[str, float], dict[str, float]]:
    centers_lineage = envelope.lineage.get("bucket_centers_c_json")
    intervals_lineage = envelope.lineage.get("bucket_intervals_c_json")
    # The intervals alone are enough, and requiring the centres alongside them
    # threw away the exact geometry on 99.9% of roots (88822 of 88878): they
    # carry ``bucket_intervals_c_json`` and no ``bucket_centers_c_json``, so
    # every one of them fell through to re-deriving bounds the root had
    # already stated exactly.  Centres are the midpoints of those intervals,
    # so there is nothing to recover them from that the intervals do not say.
    if intervals_lineage:
        try:
            intervals = {
                str(bucket): bounds
                for bucket, bounds in json.loads(intervals_lineage).items()
            }
            stated = (
                {
                    str(bucket): float(value)
                    for bucket, value in json.loads(centers_lineage).items()
                }
                if centers_lineage
                else {}
            )
            # An open-ended first or last bucket has no midpoint of its own;
            # it gets the ladder's own rung, which is the narrowest closed
            # interval on it.  A hardcoded half degree was a C-market
            # assumption -- F markets rung in 2F = 10/9 C.
            widths = [
                float(bounds[1]) - float(bounds[0])
                for bounds in intervals.values()
                if bounds[0] is not None and bounds[1] is not None
            ]
            step = min(widths) if widths else (
                1.0 if unit == "C" else 10.0 / 9.0
            )
            centers = {}
            uppers = {}
            for bucket, bounds in intervals.items():
                low = None if bounds[0] is None else float(bounds[0])
                high = None if bounds[1] is None else float(bounds[1])
                if low is not None and high is not None:
                    center = (low + high) / 2.0
                elif high is not None:
                    center = high - step / 2.0
                elif low is not None:
                    center = low + step / 2.0
                else:
                    raise ContractError(
                        "p1_probability_bucket_geometry_invalid"
                    )
                centers[bucket] = stated.get(bucket, center)
                uppers[bucket] = (
                    high if high is not None else centers[bucket] + step / 2.0
                )
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
        # Degenerate ladder (fewer than two distinct finite uppers): fall back to
        # the settled per-unit rung. C markets ladder in 1°C; F markets ladder in
        # 2°F = 10/9 °C, never 1°F (charter clause 53).
        default=(1.0 if unit == "C" else 10.0 / 9.0),
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
    """Create the hard-bound-only identity-LR artifact.

    This is the incumbent P1 behaviour while no formally promoted trajectory
    artifact exists for the scope: likelihood one everywhere, so the update
    claims nothing it has not learned, while the official running high still
    hard-excludes the buckets the day's observations have ruled out.  Demanding
    a promoted successor before any P1 could run was tried in August 2026 and
    silenced the stage for nine days -- taking the hard bound down with it,
    since P1 is that bound's only writer into the atomic chain.  A promoted
    artifact supersedes this the moment one resolves.
    """

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


def p1_trajectory_mode(
    trajectory_artifact: MetarTrajectoryLikelihoodArtifact | None,
    *,
    hourly_curve_method_version: str,
) -> str:
    """Name the trajectory authority this P1 compile runs under.

    The stage runs in every case -- that is the cutover rule this function
    exists to pin.  Requiring the hourly-curve successor before any P1 could
    run was tried in August 2026 and silenced the stage for nine days, taking
    the reported-high hard bound with it, because the successor's own gate
    needs twenty effective folds that could not exist yet.  The mode is
    recorded in receipts so a scope running degraded is visible, not inferred.
    """

    if trajectory_artifact is None:
        return "HARD_BOUND_ONLY_FALLBACK"
    if trajectory_artifact.method_version == hourly_curve_method_version:
        return "PROMOTED_HOURLY_CURVE"
    if (
        trajectory_artifact.method_version
        == METAR_TRAJECTORY_PHYSICAL_ABSOLUTE_TMAX_METHOD_VERSION
    ):
        return "PROMOTED_PHYSICAL_TMAX"
    return "PROMOTED_PRE_CURVE"


def compile_station_p1_packets(
    *,
    predecessor: ProbabilityEnvelope | Mapping[str, Any],
    prediction_root: ProbabilityEnvelope | Mapping[str, Any] | None = None,
    observations: Sequence[Mapping[str, Any]],
    registry_rows: Sequence[Mapping[str, Any]],
    compilation_at_utc: str,
    freshness_seconds: int = 3600,
    decision_floor_at_utc: str | None = None,
    consumed_observation_ids: set[str] | None = None,
    trajectory_artifact: (
        MetarTrajectoryLikelihoodArtifact | Mapping[str, Any] | None
    ) = None,
    trajectory_artifact_by_local_time_bucket: (
        Mapping[
            str, MetarTrajectoryLikelihoodArtifact | Mapping[str, Any] | None
        ] | None
    ) = None,
    maturity_bin: str = "D0",
    band_table: tuple[
        Sequence[tuple[tuple[float, float], float]], str
    ] | None = None,
    center_width: tuple[float, float, str] | None = None,
    station_width: tuple[Mapping[str, float], str] | None = None,
    fleet_regime: Mapping[str, Any] | None = None,
    ceiling_artifact: Mapping[str, Any] | None = None,
    soft_likelihood_policy: str | None = None,
) -> list[dict[str, Any]]:
    """Compile every canonical P1 prefix; emit on running-high advances.

    One deliberate exception: after a packet closed by a scheduled ASOS
    substitute (asos_1min / madis_hfmetar), the first AWC report emits a
    re-anchoring packet even without a new high, restoring an atomic-capable
    head for the order chain.  ``reported_high_advanced`` stays honest on it.

    The two post-hoc correction layers are passed in rather than loaded here:
    this module takes no connection by design, and the compiled packet is the
    envelope the worker commits verbatim.  Passing None leaves the posterior
    byte-identical, which is what every caller that has not been given a
    table gets.

    ``trajectory_artifact_by_local_time_bucket`` exists because one prefix
    spans more than one local-time bucket and an artifact is fitted for
    exactly one of them.  A caller that resolves a single artifact off its own
    wall clock hands the morning's steps the afternoon's artifact, which the
    proof compiler correctly refuses -- so those steps fell to the identity
    fallback with an activation for their own bucket sitting unused.  Measured
    2026-08-24 over event dates from 2026-08-18: that accounted for 49% of the
    dark station-days at local noon, 55% at 15:00 and 58% an hour before local
    settlement.  Passing the map lets each step be governed by the artifact
    fitted for the hour it actually happened in; ``trajectory_artifact``
    remains the fallback for any bucket the map does not name, so a caller
    that passes only the single artifact behaves exactly as before.
    """

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
        raise ContractError("p1_prediction_root_stage_invalid")
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
    # The lane of the observation the current head packet was closed on.
    # An ASOS substitute may close a packet -- the posterior and hard bound
    # must move the moment the evidence exists -- and it roots the atomic
    # chain through its parallel identity fact
    # (``substitute_observation_revision``), which the PR12 sweep stages on
    # its own 600s timer.  The first AWC report after a substitute-closed
    # head still re-anchors -- a packet even without a new high -- so the
    # order chain regains a METAR-rooted head promptly instead of riding
    # the staging race, and keeps working even if the identity sweep lags.
    last_closing_lane: str | None = (
        (
            str(previous.lineage.get("source_lane") or "")
            .strip()
            .lower()
            or None
        )
        if previous.stage == "P1"
        else None
    )
    last_decision = _parse_utc(previous.decision_at_utc)
    if decision_floor_at_utc is not None:
        floor = _parse_utc(decision_floor_at_utc)
        if floor > compile_at:
            raise ContractError("p1_decision_floor_future")
        if floor > last_decision:
            # A root rebase can replay a prefix whose old P1 branch used the
            # same observation timestamp.  Keep the append-only stage clock
            # strictly after every visible P1 branch, not only the selected
            # predecessor, so stage routing never sees same-instant competing
            # authorities.
            last_decision = floor
    # The local-midnight report may commit a transmission lead ahead of its
    # own nominal minute (OEJN -6.4s, SBGR ~-30s on 7 of 12 days measured
    # 2026-08-30).  Both as-of gates below already allow that lead, but a
    # packet whose decision clock inherits the early commit sits before the
    # settlement day's start and materialize_p1's day-started assert
    # fail-closes the whole scope -- until the first post-midnight P0 root
    # lifts last_decision past midnight, which on SBGR 2026-08-29 took seven
    # hours of the event morning.  A decision about this day cannot predate
    # the day: floor every packet's clock at the day's own start.
    event_day_start_utc = datetime.fromisoformat(
        previous.event_date
    ).replace(tzinfo=ZoneInfo(root.settlement_timezone)).astimezone(
        timezone.utc
    )
    trajectory_prefix: list[MetarTrajectoryObservation] = []
    truth_revision_sha_by_observation: dict[str, str] = {}
    # Parsed once for the exhaustion test below.  A malformed curve stays
    # None here so the proof compiler keeps naming the corruption itself;
    # only a well-formed curve may argue it still has something to say.
    curve_point_times_utc: list[datetime] | None = None
    _curve_raw = root.lineage.get("conditional_future_temperature_curve_json")
    if _curve_raw:
        try:
            curve_point_times_utc = sorted(
                datetime.fromisoformat(
                    str(point.get("valid_time_utc")).replace("Z", "+00:00")
                )
                for point in (
                    json.loads(str(_curve_raw)).get("points") or []
                )
                if isinstance(point, Mapping)
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            curve_point_times_utc = None
    # The day's running high over the whole visible prefix, so each step can
    # say whether its own high is still the current one.  The cooling-side
    # ceiling is fitted only on the belief a sweep actually leaves standing --
    # the step whose high the day still holds -- and it must stay silent on
    # the replayed steps behind it, where today's clock would cut above a
    # high the day has already beaten.  Same eligibility filters as the walk;
    # an unparsable temperature is left for the walk to raise on.
    day_running_high_c: float | None = None
    for observation in ordered:
        if (
            str(observation.get("qc_status") or "").upper() != "ACCEPTED"
            or str(observation.get("evidence_role") or "").upper() != "FORMAL"
            or bool(observation.get("correction_suspected"))
            or str(observation.get("station_id") or "") != previous.station_id
            or str(observation.get("event_date_local") or "")
            != previous.event_date
        ):
            continue
        try:
            value = float(observation["reported_temperature_c"])
        except (KeyError, TypeError, ValueError):
            continue
        day_running_high_c = (
            value
            if day_running_high_c is None
            else max(day_running_high_c, value)
        )
    for sequence, observation in enumerate(ordered, start=1):
        # SOFT_PATH rows (the intra-hour ASOS carrier readings) walk the
        # prefix like everything else, so the path state sees the dense
        # trajectory -- but they hold no hard rights below: no running-high
        # update, no packet, no closing identity.
        if (
            str(observation.get("qc_status") or "").upper() != "ACCEPTED"
            or str(observation.get("evidence_role") or "").upper()
            not in {"FORMAL", "SOFT_PATH"}
            or bool(observation.get("correction_suspected"))
        ):
            continue
        if (
            str(observation.get("station_id") or "") != previous.station_id
            or str(observation.get("event_date_local") or "")
            != previous.event_date
        ):
            continue
        observation_timezone = str(
            observation.get("settlement_timezone") or ""
        ).strip()
        if not observation_timezone:
            raise ContractError("p1_observation_settlement_timezone_missing")
        if observation_timezone != previous.settlement_timezone:
            raise ContractError("p1_observation_settlement_timezone_mismatch")
        committed = _parse_utc(
            str(observation.get("canonical_committed_at_utc") or "")
        )
        valid_at = _parse_utc(str(observation.get("valid_at_utc") or ""))
        if committed > compile_at:
            raise ContractError("p1_canonical_commit_not_strict_asof")
        # Same split as the resident reader's as-of gate: the commit may not
        # follow the decision, but a report's own nominal minute may sit a
        # transmission lead ahead of its commit.  Both gates have to allow it
        # or the second one re-closes the station-day the first just opened.
        if valid_at - committed > _MAX_TRANSMISSION_LEAD:
            raise ContractError("p1_canonical_observation_time_order_invalid")
        try:
            reported_c = float(observation["reported_temperature_c"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("p1_reported_temperature_invalid") from exc
        observation_lane = str(
            observation.get("source_lane") or ""
        ).strip().lower()
        # Lane decides hard rights, not the role label: a soft-lane row may
        # never move the official running high, whatever a caller stamped on
        # it.  Settlement follows the formal METAR series (measured
        # 2026-08-29), so only formal rows may claim what it will honor.
        soft_path_only = (
            observation_lane in SOFT_PATH_SOURCE_LANES
            or str(observation.get("evidence_role") or "").upper()
            == "SOFT_PATH"
        )
        old_high = running_high_c
        if not soft_path_only:
            running_high_c = (
                reported_c
                if running_high_c is None
                else max(running_high_c, reported_c)
            )
        observation_id = str(observation.get("observation_id") or "")
        if not observation_id:
            raise ContractError("p1_observation_id_missing")
        # Carried alongside the trajectory rather than inside it: the METAR
        # event describes the report, while this names the canonical revision
        # fact the truth ledger is keyed on.  Tracked for every observation so
        # the closing one can be looked up once the prefix is known.
        truth_revision_sha_by_observation[observation_id] = str(
            observation.get("truth_observation_revision_content_sha256") or ""
        ).strip().lower()
        trajectory_prefix.append(
            MetarTrajectoryObservation.from_mapping({
                "event": {
                    "observation_id": observation_id,
                    "station_id": previous.station_id,
                    "event_date": previous.event_date,
                    "settlement_timezone": observation_timezone,
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
                "raw_text": str(observation.get("raw_text") or "").strip()
                or None,
            })
        )
        if soft_path_only:
            # Path evidence delivered; nothing below (consumption, advance,
            # emission, closing identity) belongs to a soft row.
            continue
        if observation_id in consumed:
            continue
        high_advanced = old_high is None or reported_c > old_high
        reanchors_atomic_head = (
            not high_advanced
            and observation_lane == "awc"
            and last_closing_lane not in (None, "awc")
        )
        if not high_advanced and not reanchors_atomic_head:
            continue
        decision = max(
            committed,
            last_decision + timedelta(microseconds=1),
            event_day_start_utc,
        )
        decision_text = _stamp(decision)
        # This loop replays the whole station-day: every observation gets its
        # own decision time, and those walk backwards through the local-time
        # buckets.  A promoted artifact is fitted for exactly one bucket and
        # the proof refuses to be applied outside it, so handing the
        # afternoon's artifact to the morning's observation raised
        # ``metar_trajectory_local_time_bucket_mismatch`` and fail-closed the
        # entire station-day (six stations on 2026-08-16, all of them at
        # UTC+8 with a morning already behind them).  Out of its own bucket a
        # promoted artifact has nothing fitted to say, which is the exact
        # situation the identity fallback exists for -- so each step now asks
        # for the artifact fitted for its own hour, and only falls back when
        # there is none.
        step_bucket = _local_time_bucket(
            decision_text, root.settlement_timezone
        )
        step_artifact = trajectory_artifact
        if trajectory_artifact_by_local_time_bucket is not None:
            named = trajectory_artifact_by_local_time_bucket.get(step_bucket)
            if named is not None:
                step_artifact = named
        explicit_artifact = (
            step_artifact
            if isinstance(step_artifact, MetarTrajectoryLikelihoodArtifact)
            else MetarTrajectoryLikelihoodArtifact.from_mapping(step_artifact)
            if isinstance(step_artifact, Mapping)
            else None
        )
        # The hourly-curve method reads a conditional future-temperature
        # curve off the root.  A root compiled before that curve existed --
        # and on the event day itself the root is frozen from the day before,
        # so it cannot be recompiled to grow one -- has nothing for it to
        # read, and the proof raised
        # ``metar_trajectory_root_future_curve_missing`` rather than
        # degrading.  Same rule as the bucket: the promoted artifact governs
        # only where it can actually be evaluated.
        # Same rule extended to the evening: the feature compiler demands at
        # least two strictly-future curve points, and on the event day the
        # frozen root's curve simply runs out of them as local midnight
        # approaches -- ``future_temperature_curve_future_points_insufficient``
        # took three UTC+8 stations' whole P1 down at 20:49 local on
        # 2026-08-20.  An exhausted curve leaves the promoted artifact
        # nothing to evaluate, which is exactly the identity-fallback
        # situation; the hard bound keeps killing buckets either way.
        artifact_governs_root = explicit_artifact is not None and (
            explicit_artifact.method_version
            != METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION
            or (
                bool(
                    root.lineage.get(
                        "conditional_future_temperature_curve_json"
                    )
                )
                and (
                    curve_point_times_utc is None
                    or sum(
                        1
                        for point_time in curve_point_times_utc
                        if point_time > decision
                    )
                    >= 2
                )
            )
        )
        artifact_in_bucket = (
            artifact_governs_root
            and explicit_artifact.local_time_bucket == step_bucket
        )
        # The conditional/curve methods need three observations behind them
        # before they may speak.  That minimum belongs to the promoted
        # artifact, so it only applies on the steps that artifact actually
        # governs; on a fallback step it would skip an observation the
        # identity update was ready to consume.
        if (
            artifact_in_bucket
            and explicit_artifact.method_version
            in {
                METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION,
                METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
                METAR_TRAJECTORY_PHYSICAL_ABSOLUTE_TMAX_METHOD_VERSION,
            }
            and len(trajectory_prefix) < 3
        ):
            continue
        generation += 1
        governed_artifact = (
            explicit_artifact
            if artifact_in_bucket
            else _fallback_trajectory_artifact(
                root,
                # The governing lane, not the closer's: an ASOS substitute
                # closes a step inside an AWC-governed scope, and the proof
                # canonicalizes the whole prefix against the artifact's
                # lane -- a substitute-laned artifact would refuse the AWC
                # reports around it.
                source_lane=(
                    "awc"
                    if str(observation.get("source_lane") or "")
                    .strip()
                    .lower()
                    in {"asos_1min", "madis_hfmetar"}
                    else str(observation.get("source_lane") or "")
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
            prefix_is_current=(
                day_running_high_c is not None
                and running_high_c >= day_running_high_c - 1e-9
            ),
            # None is production's policy (``SOFT_LIKELIHOOD_POLICY``); tests
            # of the layer's own mechanics pass ACTIVE explicitly.
            soft_likelihood_policy=soft_likelihood_policy,
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
            truth_observation_revision_content_sha256=(
                truth_revision_sha_by_observation.get(observation_id) or None
            ),
            band_table=band_table,
            center_width=center_width,
            station_width=station_width,
            fleet_regime=fleet_regime,
            ceiling_artifact=ceiling_artifact,
        )
        packets.append({
            "observation_id": observation_id,
            # Carried on the packet, not only into the envelope built above:
            # the strict materializer contract is rebuilt from this packet, so
            # a field the projection cannot see is a field the rebuilt envelope
            # will not have.
            "truth_observation_revision_content_sha256": (
                truth_revision_sha_by_observation.get(observation_id) or ""
            ),
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
        last_closing_lane = observation_lane
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
    """Select the newest available P0/P0-M branch and replay cursor.

    A replay cursor is durable history, not necessarily a currently
    consumable probability.  It may therefore be stale while a delayed
    formal observation is being applied.  The resident writer must append
    that deterministic replay result even when it is already stale, so the
    observation is consumed exactly once.  P1 is rooted at the newest
    compatible P0-M when that checkpoint exists; otherwise it is rooted at
    the current P0.  Both cases bind the exact root checkpoint and content
    hash, so an old P1 from another root cannot become a replay cursor.
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
        _select_replay_checkpoint(branch_taf, as_of_utc=as_of_utc)
        if branch_taf
        else latest_p0
    )
    if base.stage == "P0":
        p0_root_content = base.content_sha256
    else:
        p0_root_content = str(
            base.lineage.get("p0_root_content_sha256") or ""
        ).lower()
        if len(p0_root_content) != 64 or any(
            character not in "0123456789abcdef"
            for character in p0_root_content
        ):
            raise ContractError("p1_replay_p0m_root_lineage_missing")
        if p0_root_content != latest_p0.content_sha256:
            raise ContractError("p1_replay_p0m_root_lineage_mismatch")
    branch_p1: list[ProbabilityEnvelope] = []
    for row in rows:
        if row.stage != "P1" or row.decision_at_utc > as_of_utc:
            continue
        lineage = row.lineage
        if lineage.get("prediction_root_stage") != base.stage:
            continue
        if str(lineage.get("prediction_root_checkpoint_id") or "") != base.checkpoint_id:
            continue
        if str(lineage.get("prediction_root_content_sha256") or "").lower() != base.content_sha256:
            continue
        if str(lineage.get("p0_root_checkpoint_id") or "") != root_id:
            continue
        p0_content = str(lineage.get("p0_root_content_sha256") or "").lower()
        # Early P0-root P1 rows predate the explicit ancestor-content field;
        # for that branch the prediction-root content is the exact P0
        # ancestor and is safe to derive.  P0-M-root rows must carry the
        # explicit P0 ancestor and never receive this compatibility shortcut.
        if not p0_content and lineage.get("prediction_root_stage") == "P0":
            p0_content = str(lineage.get("prediction_root_content_sha256") or "").lower()
        if p0_content == p0_root_content:
            branch_p1.append(row)
    if branch_p1:
        predecessor = _select_replay_checkpoint(
            branch_p1,
            as_of_utc=as_of_utc,
        )
        consumed: set[str] = set()
        for row in branch_p1:
            # Each P1 row carries the complete canonical prefix used to
            # recompute it.  Replaying only the last event would feed the
            # already-consumed prefix back into the compiler and then fail
            # append-only insertion with p1_canonical_event_already_consumed.
            prefix = str(
                row.lineage.get("canonical_observation_ids") or ""
            )
            consumed.update(
                item.strip()
                for item in prefix.split(",")
                if item.strip()
            )
            event_id = str(
                row.lineage.get("canonical_observation_event_id") or ""
            ).strip()
            if event_id:
                consumed.add(event_id)
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
        # Projected explicitly.  This dict is the whole input to the strict
        # materializer, so anything omitted here is silently absent from the
        # rebuilt envelope -- and without this key the envelope compiles but
        # can never become an atomic fact.
        "truth_observation_revision_content_sha256": str(
            packet.get("truth_observation_revision_content_sha256") or ""
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
