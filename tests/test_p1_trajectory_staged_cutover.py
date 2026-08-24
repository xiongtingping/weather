"""P1 runs under whatever trajectory authority exists; it never goes dark.

The August 2026 outage this pins: the resident lane began failing closed
unless a formally promoted hourly-curve (v3) artifact resolved.  The v3 gate
needs twenty effective folds; curve-bearing roots existed only from 2026-08-03
and folds need settled truth, so no station could promote for weeks.  P1
emitted nothing after 2026-08-01 -- and P1 is the only writer of the
reported-high hard bound into the atomic chain, so the system kept pricing
buckets the day's own observations had excluded.

The cutover rule: the incumbent (identity-LR, hard bound intact) serves until
the successor passes its own gate, and the mode travels in receipts so a
degraded scope is visible rather than inferred.
"""

from __future__ import annotations

from bayes_basket_target.contracts import ProbabilityEnvelope
from bayes_basket_target.metar_trajectory import (
    METAR_TRAJECTORY_AUTHORITY_SCOPE,
    METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
    METAR_TRAJECTORY_METHOD_VERSION,
    build_metar_trajectory_likelihood_artifact,
)
from bayes_basket_target.p1_compiler import (
    _fallback_trajectory_artifact,
    compile_station_p1_packets,
    p1_trajectory_mode,
)


def _p0_root() -> ProbabilityEnvelope:
    return ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "p0",
        "station_id": "KLGA",
        "event_date": "2026-08-09",
        "settlement_timezone": "America/New_York",
        "decision_at_utc": "2026-08-09T04:10:00Z",
        "fresh_until_utc": "2026-08-09T06:00:00Z",
        "bucket_ordinals": {
            "75°F or below": 0,
            "76-77°F": 1,
            "78°F or higher": 2,
        },
        "probabilities": {
            "75°F or below": 0.2,
            "76-77°F": 0.5,
            "78°F or higher": 0.3,
        },
        "posterior_version": "p0",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
        "lineage": {
            "model_run_ids": "r",
            "settlement_timezone": "America/New_York",
            "season_bin": "JJA",
            "settlement_definition": "station_local_calendar_day_tmax",
        },
    })


_REGISTRY = [
    {
        "station_id": "KLGA",
        "event_date": "2026-08-09",
        "settlement_timezone": "America/New_York",
        "settlement_unit": "F",
        "outcome": outcome,
        "registry_json": (
            '{"upper": ' + ("null" if upper is None else str(upper)) + "}"
        ),
    }
    for outcome, upper in (
        ("75°F or below", 75.0),
        ("76-77°F", 77.0),
        ("78°F or higher", None),
    )
]


def _promoted_artifact(local_time_bucket: str):
    return build_metar_trajectory_likelihood_artifact({
        "activation_id": f"activation-{local_time_bucket.lower()}",
        "station_id": "KLGA",
        "source_lane": "awc",
        "season_bin": "JJA",
        "local_time_bucket": local_time_bucket,
        "maturity_bin": "D0",
        "settlement_definition": "station_local_calendar_day_tmax",
        "settlement_timezone": "America/New_York",
        "feature_definitions": {"running_high_c": "max dry-bulb as-of"},
        "missing_observation_policy": "DEGRADE_STRENGTH",
        "expected_interval_minutes": 60,
        "long_interval_minutes": 90,
        "max_missing_intervals": 6,
        "missing_interval_strength_factor": 0.8,
        "long_interval_strength_factor": 0.5,
        "remaining_warming_distribution": {"0": 0.2, "1": 0.5, "2": 0.3},
        "hard_exclusion_baseline_distribution": {
            "0": 0.34, "1": 0.33, "2": 0.33,
        },
        "base_strength": 0.8,
        "maturity_strength": 0.75,
        "likelihood_ratio_floor": 0.25,
        "likelihood_ratio_ceiling": 4.0,
        "sample_n": 120,
        "effective_sample_n": 84.5,
        "oos_metrics": {"brier_score": 0.15, "nll": 0.91, "crps": 0.62},
        "tail_calibration_error": 0.04,
        "trained_through_utc": "2026-08-01T00:00:00Z",
        "valid_from_utc": "2026-08-05T00:00:00Z",
        "valid_until_utc": "2026-08-20T00:00:00Z",
        "promotion_state": "PRODUCTION_NO_LIVE_PROMOTED",
        "authority_scope": METAR_TRAJECTORY_AUTHORITY_SCOPE,
        "activated": True,
        "method_version": METAR_TRAJECTORY_METHOD_VERSION,
    })


def _two_bucket_observations() -> list[dict]:
    """One morning report and one afternoon one, both new highs.

    America/New_York in August is UTC-4, so 14:05Z is 10:05 local (MORNING)
    and 18:05Z is 14:05 local (AFTERNOON).
    """

    return [
        {
            "observation_id": "o-morning",
            "station_id": "KLGA",
            "event_date_local": "2026-08-09",
            "settlement_timezone": "America/New_York",
            "reported_temperature_c": 20.0,
            "valid_at_utc": "2026-08-09T14:00:00Z",
            "canonical_committed_at_utc": "2026-08-09T14:05:00Z",
            "raw_sha256": "1" * 64,
            "source_lane": "AWC",
            "report_type": "METAR",
            "evidence_role": "FORMAL",
            "qc_status": "ACCEPTED",
            "correction_suspected": False,
        },
        {
            "observation_id": "o-afternoon",
            "station_id": "KLGA",
            "event_date_local": "2026-08-09",
            "settlement_timezone": "America/New_York",
            "reported_temperature_c": 23.0,
            "valid_at_utc": "2026-08-09T18:00:00Z",
            "canonical_committed_at_utc": "2026-08-09T18:05:00Z",
            "raw_sha256": "2" * 64,
            "source_lane": "AWC",
            "report_type": "METAR",
            "evidence_role": "FORMAL",
            "qc_status": "ACCEPTED",
            "correction_suspected": False,
        },
    ]


def _strengths(packets) -> list[float]:
    return [
        float(
            packet["expected_envelope"]["lineage"][
                "metar_trajectory_applied_strength"
            ]
        )
        for packet in packets
    ]


def test_a_single_artifact_leaves_the_other_bucket_dark() -> None:
    """The defect, pinned: one artifact cannot govern a two-bucket prefix.

    Resolving one artifact off the wall clock is what production did, and a
    prefix that began before that hour has steps the artifact is not fitted
    for.  Those steps fall to the identity fallback -- correctly, since out of
    its bucket the artifact has nothing to say -- so the layer is dark on them
    even where their own bucket is fully covered.
    """

    packets = compile_station_p1_packets(
        predecessor=_p0_root(),
        observations=_two_bucket_observations(),
        registry_rows=_REGISTRY,
        compilation_at_utc="2026-08-09T19:00:00Z",
        trajectory_artifact=_promoted_artifact("AFTERNOON"),
    )
    assert len(packets) == 2
    morning, afternoon = _strengths(packets)
    assert morning == 0.0
    assert afternoon > 0.0


def test_each_step_is_governed_by_its_own_local_time_bucket() -> None:
    """The fix: hand the compiler one artifact per bucket and both speak."""

    packets = compile_station_p1_packets(
        predecessor=_p0_root(),
        observations=_two_bucket_observations(),
        registry_rows=_REGISTRY,
        compilation_at_utc="2026-08-09T19:00:00Z",
        trajectory_artifact=_promoted_artifact("AFTERNOON"),
        trajectory_artifact_by_local_time_bucket={
            "MORNING": _promoted_artifact("MORNING"),
            "AFTERNOON": _promoted_artifact("AFTERNOON"),
        },
    )
    assert len(packets) == 2
    morning, afternoon = _strengths(packets)
    assert morning > 0.0
    assert afternoon > 0.0
    lineage = packets[0]["expected_envelope"]["lineage"]
    assert lineage["metar_trajectory_activation_id"] == "activation-morning"


def test_a_bucket_absent_from_the_map_keeps_the_single_artifact() -> None:
    """A partial map is a partial answer, not a silent loss of the incumbent.

    Only the afternoon is named; the morning step must behave exactly as it
    does today rather than losing the artifact the caller did pass.
    """

    packets = compile_station_p1_packets(
        predecessor=_p0_root(),
        observations=_two_bucket_observations(),
        registry_rows=_REGISTRY,
        compilation_at_utc="2026-08-09T19:00:00Z",
        trajectory_artifact=_promoted_artifact("AFTERNOON"),
        trajectory_artifact_by_local_time_bucket={
            "AFTERNOON": _promoted_artifact("AFTERNOON"),
        },
    )
    baseline = compile_station_p1_packets(
        predecessor=_p0_root(),
        observations=_two_bucket_observations(),
        registry_rows=_REGISTRY,
        compilation_at_utc="2026-08-09T19:00:00Z",
        trajectory_artifact=_promoted_artifact("AFTERNOON"),
    )
    assert _strengths(packets) == _strengths(baseline)


def test_no_promoted_artifact_is_the_fallback_mode_not_a_blocker() -> None:
    assert p1_trajectory_mode(
        None,
        hourly_curve_method_version=(
            METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION
        ),
    ) == "HARD_BOUND_ONLY_FALLBACK"


def test_promoted_artifacts_are_distinguished_by_curve_capability() -> None:
    """A pre-curve promoted artifact serves; the receipt says which it was."""

    fallback = _fallback_trajectory_artifact(
        _p0_root(),
        source_lane="awc",
        maturity_bin="D0",
        as_of_utc="2026-08-09T15:00:00Z",
    )
    assert p1_trajectory_mode(
        fallback,
        hourly_curve_method_version=(
            METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION
        ),
    ) == "PROMOTED_PRE_CURVE"
    assert p1_trajectory_mode(
        fallback,
        hourly_curve_method_version=fallback.method_version,
    ) == "PROMOTED_HOURLY_CURVE"


def test_fallback_p1_still_hard_excludes_what_observations_ruled_out() -> None:
    """The reason the stage must not go dark, demonstrated end to end.

    With no trajectory artifact at all, a reported 26C running high excludes
    both Fahrenheit buckets it clears, and the surviving bucket takes all the
    probability -- likelihood is identity, so nothing else moves.  Nine days
    of missing this is nine days of pricing impossible buckets.
    """

    packets = compile_station_p1_packets(
        predecessor=_p0_root(),
        observations=[{
            "observation_id": "o1",
            "station_id": "KLGA",
            "event_date_local": "2026-08-09",
            "settlement_timezone": "America/New_York",
            "reported_temperature_c": 26.0,
            "valid_at_utc": "2026-08-09T14:00:00Z",
            "canonical_committed_at_utc": "2026-08-09T14:05:00Z",
            "raw_sha256": "1" * 64,
            "source_lane": "AWC",
            "report_type": "METAR",
            "evidence_role": "FORMAL",
            "qc_status": "ACCEPTED",
            "correction_suspected": False,
        }],
        registry_rows=[
            {
                "station_id": "KLGA",
                "event_date": "2026-08-09",
                "settlement_timezone": "America/New_York",
                "settlement_unit": "F",
                "outcome": outcome,
                "registry_json": (
                    '{"upper": ' + ("null" if upper is None else str(upper)) + "}"
                ),
            }
            for outcome, upper in (
                ("75°F or below", 75.0),
                ("76-77°F", 77.0),
                ("78°F or higher", None),
            )
        ],
        compilation_at_utc="2026-08-09T15:00:00Z",
        trajectory_artifact=None,
    )
    assert packets
    final = packets[-1]["expected_envelope"]
    assert packets[-1]["hard_excluded_buckets"] == [
        "75°F or below",
        "76-77°F",
    ]
    assert final["probabilities"]["78°F or higher"] == 1.0
    # Identity likelihood leaves the exclusion as the whole update: the
    # lineage names the unpromoted placeholder and says why no strength
    # was applied, so a fallback P1 is auditable as one.
    assert final["lineage"]["metar_trajectory_activation_id"] == "NONE"
    assert final["lineage"]["metar_trajectory_applied_strength"] == "0"
    assert (
        "ARTIFACT_NOT_PROMOTED"
        in final["lineage"]["metar_trajectory_degradation_reasons"]
    )
