from __future__ import annotations

from copy import deepcopy
import json

import pytest

from bayes_basket_target.contracts import ContractError, ProbabilityEnvelope
from bayes_basket_target.metar_trajectory import (
    METAR_TRAJECTORY_AUTHORITY_SCOPE,
    METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION,
    METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
    METAR_TRAJECTORY_METHOD_VERSION,
    METAR_TRAJECTORY_PHYSICAL_ABSOLUTE_TMAX_METHOD_VERSION,
    MAX_METAR_TRAJECTORY_BUCKETS,
    MAX_METAR_TRAJECTORY_OBSERVATIONS,
    MetarTrajectoryLikelihoodArtifact,
    MetarTrajectoryObservation,
    MetarTrajectoryUpdateProof,
    build_metar_trajectory_likelihood_artifact,
    canonicalize_metar_trajectory_observations,
    compile_metar_trajectory_update_proof,
    validate_metar_trajectory_update_proof,
)
from bayes_basket_target.future_temperature_curve import (
    build_root_future_temperature_curve,
)
from bayes_basket_target.metar_path_state import PATH_STATE_SCHEMA


BUCKET_CENTERS = {
    "33C": 33.0,
    "34C": 34.0,
    "35C": 35.0,
    "36C": 36.0,
    "37C": 37.0,
}
BUCKET_UPPER_BOUNDS = {
    "33C": 33.49,
    "34C": 34.49,
    "35C": 35.49,
    "36C": 36.49,
    "37C": 37.49,
}


def _root(
    *,
    stage: str = "P0-M",
    station: str = "RKPK",
    event_date: str = "2026-07-29",
    timezone: str = "Asia/Seoul",
    bucket_centers=BUCKET_CENTERS,
    probabilities=None,
) -> ProbabilityEnvelope:
    governed_probabilities = probabilities or {
        "33C": 0.10,
        "34C": 0.20,
        "35C": 0.40,
        "36C": 0.20,
        "37C": 0.10,
    }
    return ProbabilityEnvelope.from_mapping({
        "stage": stage,
        "checkpoint_id": f"{stage.lower()}-root",
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": timezone,
        "decision_at_utc": "2026-07-28T23:00:00Z",
        "fresh_until_utc": "2026-07-30T16:00:00Z",
        "bucket_ordinals": {
            bucket: index for index, bucket in enumerate(bucket_centers)
        },
        "probabilities": governed_probabilities,
        "posterior_version": "fixture-v1",
        "posterior_sha256": "1" * 64,
        "bridge_sha256": "2" * 64,
        "lineage": {
            "root": "immutable-formal-root",
            "season_bin": "JJA",
            "settlement_definition": "station_local_calendar_day_tmax",
        },
    })


def _artifact(
    *,
    promoted: bool = True,
    source_lane: str = "awc",
    timezone: str = "Asia/Seoul",
    station: str = "RKPK",
    local_time_bucket: str = "AFTERNOON",
    max_missing_intervals: int = 4,
    base_strength: float = 0.8,
) -> MetarTrajectoryLikelihoodArtifact:
    return build_metar_trajectory_likelihood_artifact({
        "activation_id": "metar-trajectory-activation-rkpk-v1",
        "station_id": station,
        "source_lane": source_lane,
        "season_bin": "JJA",
        "local_time_bucket": local_time_bucket,
        "maturity_bin": "D0",
        "settlement_definition": "station_local_calendar_day_tmax",
        "settlement_timezone": timezone,
        "feature_definitions": {
            "running_high_c": "max canonical dry-bulb temperature as-of",
            "remaining_warming_c": "final tmax minus running high",
            "elapsed_minutes": "canonical observed-at interval",
        },
        "missing_observation_policy": "DEGRADE_STRENGTH",
        "expected_interval_minutes": 60,
        "long_interval_minutes": 90,
        "max_missing_intervals": max_missing_intervals,
        "missing_interval_strength_factor": 0.8,
        "long_interval_strength_factor": 0.5,
        "remaining_warming_distribution": {
            "0": 0.10,
            "1": 0.50,
            "2": 0.30,
            "3": 0.10,
        },
        "hard_exclusion_baseline_distribution": {
            "0": 0.25,
            "1": 0.25,
            "2": 0.25,
            "3": 0.25,
        },
        "base_strength": base_strength,
        "maturity_strength": 0.75,
        "likelihood_ratio_floor": 0.25,
        "likelihood_ratio_ceiling": 4.0,
        "sample_n": 120,
        "effective_sample_n": 84.5,
        "oos_metrics": {
            "brier_score": 0.15,
            "nll": 0.91,
            "crps": 0.62,
        },
        "tail_calibration_error": 0.04,
        "trained_through_utc": "2026-07-01T00:00:00Z",
        "valid_from_utc": "2026-07-20T00:00:00Z",
        "valid_until_utc": "2026-08-10T00:00:00Z",
        "promotion_state": (
            "PRODUCTION_NO_LIVE_PROMOTED" if promoted else "CANDIDATE_ONLY"
        ),
        "authority_scope": METAR_TRAJECTORY_AUTHORITY_SCOPE,
        "activated": promoted,
        "method_version": METAR_TRAJECTORY_METHOD_VERSION,
    })


def _observation(
    suffix: str,
    *,
    sequence: int,
    observed: str,
    received: str | None = None,
    temperature_c: float,
    lane: str = "awc",
    station: str = "RKPK",
    event_date: str = "2026-07-29",
    raw_text: str | None = None,
) -> MetarTrajectoryObservation:
    return MetarTrajectoryObservation.from_mapping({
        "event": {
            "observation_id": f"obs-{suffix}",
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": "Asia/Seoul",
            "source_lane": lane,
            "report_type": "METAR",
            "sequence": sequence,
            "observed_at_utc": observed,
            "received_at_utc": received or observed,
            "raw_sha256": f"{sequence:x}".rjust(64, "0"),
            "qc_state": "ACCEPTED",
            "supersedes_observation_id": None,
        },
        "canonical_committed_at_utc": received or observed,
        "temperature_c": temperature_c,
        "raw_text": raw_text,
    })


def _conditional_artifact() -> MetarTrajectoryLikelihoodArtifact:
    payload = _artifact().to_dict()
    payload.pop("artifact_id")
    payload.pop("content_sha256")
    payload.update({
        "path_feature_schema": PATH_STATE_SCHEMA,
        "path_model_coefficients": {
            "base_distribution_mean": 0.3,
            "root_expected_remaining_warming": 0.5,
            "momentum_c_per_hour": 0.35,
            "acceleration_c_per_hour2": 0.1,
            "drawdown_from_high_c": -0.5,
            "dewpoint_depression_c": 0.01,
            "cloud_cover_fraction": -0.8,
            "precipitation_present": -1.0,
            "wind_speed_mps": -0.02,
            "remaining_hours": 0.02,
        },
        "path_model_sigma_c": 0.8,
        "method_version": METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION,
    })
    return build_metar_trajectory_likelihood_artifact(payload)


def _hourly_curve_artifact() -> MetarTrajectoryLikelihoodArtifact:
    payload = _conditional_artifact().to_dict()
    payload.pop("artifact_id")
    payload.pop("content_sha256")
    payload["path_model_coefficients"].update({
        "predicted_peak_remaining_warming_c": 0.35,
        "minutes_to_predicted_peak": 0.02,
        "next_curve_delta_from_high_c": 0.15,
        "future_curve_slope_c_per_hour": 0.10,
    })
    payload["method_version"] = (
        METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION
    )
    return build_metar_trajectory_likelihood_artifact(payload)


def _root_with_hourly_curve(
    *,
    available_at_utc: str = "2026-07-28T23:00:00Z",
    curve_checkpoint_id: str = "p0-m-root",
    declared_sha256: str | None = None,
) -> ProbabilityEnvelope:
    root = _root()
    curve = build_root_future_temperature_curve({
        "station_id": "RKPK",
        "event_date": "2026-07-29",
        "settlement_timezone": "Asia/Seoul",
        "root_checkpoint_id": curve_checkpoint_id,
        "root_probability_sha256": root.posterior_sha256,
        "available_at_utc": available_at_utc,
        "source_snapshot_sha256": "3" * 64,
        "independence_family_ids": ["ECMWF_IFS", "NOAA_GEFS"],
        "points": [
            {"valid_time_utc": "2026-07-29T07:00:00Z", "temperature_c": 35.5},
            {"valid_time_utc": "2026-07-29T09:00:00Z", "temperature_c": 36.5},
            {"valid_time_utc": "2026-07-29T12:00:00Z", "temperature_c": 36.0},
            {"valid_time_utc": "2026-07-29T14:00:00Z", "temperature_c": 35.0},
        ],
    })
    raw = root.to_dict()
    raw["lineage"].update({
        "conditional_future_temperature_curve_json": json.dumps(
            curve.to_dict(), sort_keys=True, separators=(",", ":")
        ),
        "conditional_future_temperature_curve_sha256": (
            declared_sha256 or curve.content_sha256
        ),
    })
    return ProbabilityEnvelope.from_mapping(raw)


def _compile(
    observations,
    *,
    artifact: MetarTrajectoryLikelihoodArtifact | None = None,
    root: ProbabilityEnvelope | None = None,
    as_of: str = "2026-07-29T06:30:00Z",
    prefix_is_current: bool = True,
    soft_likelihood_policy: str | None = "ACTIVE",
) -> MetarTrajectoryUpdateProof:
    # The layer's own mechanics are exercised with the policy ACTIVE; the
    # production default (suspension, 2026-09-02) has its own tests below.
    return compile_metar_trajectory_update_proof(
        root or _root(),
        observations=observations,
        artifact=artifact or _artifact(),
        bucket_centers_c=BUCKET_CENTERS,
        bucket_upper_bounds_c=BUCKET_UPPER_BOUNDS,
        maturity_bin="D0",
        as_of_utc=as_of,
        prefix_is_current=prefix_is_current,
        soft_likelihood_policy=soft_likelihood_policy,
    )


def test_artifact_is_content_addressed_and_deeply_immutable() -> None:
    artifact = _artifact()
    assert artifact.artifact_id == (
        "metar-trajectory-" + artifact.content_sha256[:32]
    )
    assert MetarTrajectoryLikelihoodArtifact.from_mapping(
        artifact.to_dict()
    ) == artifact
    with pytest.raises(TypeError):
        artifact.oos_metrics["nll"] = 1.2  # type: ignore[index]
    with pytest.raises(TypeError):
        artifact.remaining_warming_distribution[1.0] = 0.0  # type: ignore[index]

    tampered = artifact.to_dict()
    tampered["base_strength"] = 0.1
    with pytest.raises(
        ContractError, match="artifact_content_sha256_mismatch"
    ):
        MetarTrajectoryLikelihoodArtifact.from_mapping(tampered)


def test_conditional_path_artifact_forecasts_beyond_running_high() -> None:
    rising = [
        _observation("r1", sequence=1, observed="2026-07-29T04:00:00Z", temperature_c=33.0, raw_text="METAR RKPK 290400Z 18005KT CAVOK 33/20 Q1008"),
        _observation("r2", sequence=2, observed="2026-07-29T05:00:00Z", temperature_c=34.0, raw_text="METAR RKPK 290500Z 18005KT CAVOK 34/20 Q1008"),
        _observation("r3", sequence=3, observed="2026-07-29T06:00:00Z", temperature_c=35.0, raw_text="METAR RKPK 290600Z 18005KT CAVOK 35/20 Q1008"),
    ]
    cooling = [
        _observation("c1", sequence=1, observed="2026-07-29T04:00:00Z", temperature_c=35.0, raw_text="METAR RKPK 290400Z 18010KT OVC020 35/24 Q1008"),
        _observation("c2", sequence=2, observed="2026-07-29T05:00:00Z", temperature_c=34.0, raw_text="METAR RKPK 290500Z 18010KT OVC020 -RA 34/24 Q1008"),
        _observation("c3", sequence=3, observed="2026-07-29T06:00:00Z", temperature_c=33.0, raw_text="METAR RKPK 290600Z 18010KT OVC020 -RA 33/24 Q1008"),
    ]
    rising_proof = _compile(rising, artifact=_conditional_artifact())
    cooling_proof = _compile(cooling, artifact=_conditional_artifact())
    assert rising_proof.conditional_path_state_sha256 is not None
    assert cooling_proof.conditional_path_state_sha256 is not None
    assert rising_proof.posterior_probabilities["37C"] > cooling_proof.posterior_probabilities["37C"]
    assert rising_proof.posterior_probabilities["35C"] < cooling_proof.posterior_probabilities["35C"]


def _hourly_curve_observations() -> list[MetarTrajectoryObservation]:
    return [
        _observation(
            "h1", sequence=1, observed="2026-07-29T04:00:00Z",
            temperature_c=33.0,
            raw_text="METAR RKPK 290400Z 18005KT CAVOK 33/20 Q1008",
        ),
        _observation(
            "h2", sequence=2, observed="2026-07-29T05:00:00Z",
            temperature_c=34.0,
            raw_text="METAR RKPK 290500Z 18005KT CAVOK 34/20 Q1008",
        ),
        _observation(
            "h3", sequence=3, observed="2026-07-29T06:00:00Z",
            temperature_c=35.0,
            raw_text="METAR RKPK 290600Z 18005KT CAVOK 35/20 Q1008",
        ),
    ]


def test_hourly_curve_artifact_uses_strict_asof_root_bound_curve() -> None:
    proof = _compile(
        _hourly_curve_observations(),
        artifact=_hourly_curve_artifact(),
        root=_root_with_hourly_curve(),
    )
    assert proof.conditional_path_state_sha256 is not None
    assert proof.posterior_probabilities["36C"] > 0.0
    assert proof.posterior_probabilities["37C"] > 0.0


@pytest.mark.parametrize(
    ("root", "message"),
    [
        (_root(), "root_future_curve_missing"),
        (
            _root_with_hourly_curve(
                available_at_utc="2026-07-29T07:00:00Z"
            ),
            "future_temperature_curve_not_strict_asof",
        ),
        (
            _root_with_hourly_curve(curve_checkpoint_id="wrong-root"),
            "future_curve_root_scope_mismatch",
        ),
        (
            _root_with_hourly_curve(declared_sha256="4" * 64),
            "root_future_curve_sha256_mismatch",
        ),
    ],
)
def test_hourly_curve_artifact_fails_closed_without_same_cutoff_curve(
    root: ProbabilityEnvelope,
    message: str,
) -> None:
    with pytest.raises(ContractError, match=message):
        _compile(
            _hourly_curve_observations(),
            artifact=_hourly_curve_artifact(),
            root=root,
        )


def _physical_tmax_artifact() -> MetarTrajectoryLikelihoodArtifact:
    payload = _conditional_artifact().to_dict()
    payload.pop("artifact_id")
    payload.pop("content_sha256")
    payload["path_model_coefficients"].pop("root_expected_remaining_warming")
    payload["method_version"] = (
        METAR_TRAJECTORY_PHYSICAL_ABSOLUTE_TMAX_METHOD_VERSION
    )
    return build_metar_trajectory_likelihood_artifact(payload)


def test_physical_tmax_artifact_runs_on_a_curveless_root() -> None:
    # The root here carries no hourly curve at all -- the exact situation
    # where the curve method fails closed and P1 fell to identity fallback.
    proof = _compile(
        _hourly_curve_observations(), artifact=_physical_tmax_artifact()
    )
    assert proof.applied_strength > 0.0
    assert proof.conditional_path_state_sha256 is not None
    ratios = set(proof.likelihood_ratio_by_bucket.values())
    assert len(ratios) > 1


def test_physical_tmax_artifact_ignores_model_evidence_bitwise() -> None:
    # Independence as an executable assertion: shifting the root's own
    # probabilities (its expected tmax) must not move one bit of the
    # physical likelihood ratios.
    base = _root().to_dict()
    shifted = deepcopy(base)
    shifted["probabilities"] = {
        "33C": 0.90, "34C": 0.04, "35C": 0.03, "36C": 0.02, "37C": 0.01,
    }
    left = _compile(
        _hourly_curve_observations(),
        artifact=_physical_tmax_artifact(),
        root=ProbabilityEnvelope.from_mapping(base),
    )
    right = _compile(
        _hourly_curve_observations(),
        artifact=_physical_tmax_artifact(),
        root=ProbabilityEnvelope.from_mapping(shifted),
    )
    assert (
        left.likelihood_ratio_by_bucket == right.likelihood_ratio_by_bucket
    )
    assert left.applied_strength == right.applied_strength


def test_conditioner_runs_on_the_prefix_evidence_clock() -> None:
    # The same prefix compiled under two decision stamps hours apart -- a
    # live step and its replay under a rebased root.  The path state is a
    # function of the evidence alone, so the conditioner's input and the
    # physical likelihood must be byte-identical; before the split a root
    # rebase in the evening told the physical method a morning prefix had
    # no day left in it and the posterior collapsed onto the stale high.
    # Both stamps sit in the artifact's AFTERNOON bucket, so the only thing
    # that moves between the two compiles is the decision clock itself.
    early = _compile(
        _hourly_curve_observations(),
        artifact=_physical_tmax_artifact(),
        as_of="2026-07-29T06:30:00Z",
    )
    late = _compile(
        _hourly_curve_observations(),
        artifact=_physical_tmax_artifact(),
        as_of="2026-07-29T08:00:00Z",
    )
    assert (
        early.conditional_path_state_sha256
        == late.conditional_path_state_sha256
    )
    assert (
        early.likelihood_ratio_by_bucket == late.likelihood_ratio_by_bucket
    )


def _ceiling_payload() -> dict:
    return {
        "schema": "bayes_basket_target_metar_ceiling_v1",
        "trained_through": "2026-07-01",
        "bins": {
            "0": {"hours_from": 0.0, "hours_to": 10.0, "p_still_rose": 0.05},
        },
    }


def test_the_proof_carries_no_clock_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Charter 150: the ceiling is retired from the proof as from the envelope.

    It used to cut the mass above the running high to its fitted rate here
    whenever the prefix's high was current.  With the switch on and an
    artifact that would say 5%, the proof now leaves the root's own split
    under the hard bound, current prefix or not.
    """

    from bayes_basket_target import metar_ceiling

    monkeypatch.setattr(metar_ceiling, "CEILING_ENABLED", True)
    monkeypatch.setattr(
        metar_ceiling, "load_artifact", lambda path=None: _ceiling_payload()
    )
    observations = _hourly_curve_observations()
    current = _compile(
        observations, artifact=_physical_tmax_artifact(),
        prefix_is_current=True,
    )
    stale = _compile(
        observations, artifact=_physical_tmax_artifact(),
        prefix_is_current=False,
    )
    assert current.posterior_probabilities == stale.posterior_probabilities


def test_physical_tmax_artifact_rejects_model_coupled_coefficients() -> None:
    payload = _conditional_artifact().to_dict()
    payload.pop("artifact_id")
    payload.pop("content_sha256")
    payload["method_version"] = (
        METAR_TRAJECTORY_PHYSICAL_ABSOLUTE_TMAX_METHOD_VERSION
    )
    # Full conditional coefficient set still carries the model-coupled term.
    with pytest.raises(
        ContractError, match="metar_trajectory_physical_tmax_model_incomplete"
    ):
        build_metar_trajectory_likelihood_artifact(payload)


def test_artifact_sealing_is_stable_for_numeric_distribution_keys() -> None:
    payload = _artifact().to_dict()
    payload.pop("artifact_id")
    payload.pop("content_sha256")
    payload["remaining_warming_distribution"] = {
        3: 0.10, 1: 0.50, 0: 0.10, 2: 0.30
    }
    payload["hard_exclusion_baseline_distribution"] = {
        3: 0.25, 1: 0.25, 0: 0.25, 2: 0.25
    }
    resealed = build_metar_trajectory_likelihood_artifact(payload)
    assert resealed.to_dict() == _artifact().to_dict()


def test_canonical_order_is_event_time_order_and_exact_retry_is_deduplicated() -> None:
    early = _observation(
        "early", sequence=2, observed="2026-07-29T00:00:00Z",
        received="2026-07-29T02:05:00Z", temperature_c=33.0,
    )
    late = _observation(
        "late", sequence=1, observed="2026-07-29T02:00:00Z",
        received="2026-07-29T02:01:00Z", temperature_c=35.0,
    )
    ordered = canonicalize_metar_trajectory_observations(
        [late, early, early],
        as_of_utc="2026-07-29T06:30:00Z",
        station_id="RKPK",
        event_date="2026-07-29",
        source_lane="awc",
        settlement_timezone="Asia/Seoul",
    )
    assert [item.event.observation_id for item in ordered] == [
        "obs-early", "obs-late"
    ]


def test_compile_applies_soft_lr_but_hard_exclusion_never_revives() -> None:
    proof = _compile([
        _observation(
            "a", sequence=1, observed="2026-07-29T04:00:00Z",
            temperature_c=34.0,
        ),
        _observation(
            "b", sequence=2, observed="2026-07-29T05:00:00Z",
            temperature_c=35.2,
        ),
        _observation(
            "c", sequence=3, observed="2026-07-29T06:00:00Z",
            temperature_c=35.0,
        ),
    ])
    assert proof.running_high_c == 35.2
    assert proof.hard_excluded_buckets == ("33C", "34C")
    assert proof.posterior_probabilities["33C"] == 0.0
    assert proof.posterior_probabilities["34C"] == 0.0
    assert proof.likelihood_ratio_by_bucket["36C"] > 1.0
    assert 0.25 <= min(proof.likelihood_ratio_by_bucket.values())
    assert max(proof.likelihood_ratio_by_bucket.values()) <= 4.0
    assert sum(proof.posterior_probabilities.values()) == pytest.approx(1.0)
    assert proof.positive_core_buckets == ("35C", "36C", "37C")


def test_a_candidate_artifact_governs_at_its_own_sample_strength() -> None:
    """Losing the OOS gate labels an artifact; it does not switch it off.

    The gate used to decide whether the layer ran at all, and it answered no
    almost always -- 2385 rejections against 76 promotions, leaving 301 of
    350 P1 station-days with applied strength pinned at zero.  Contributing
    nothing is not neutral: it leaves P1 as the model's own tail
    renormalised, which is the shape that fails on the days the model got
    the day wrong.  Measured, the layer is worth 30 points of bucket
    accuracy (75.8% against 45.7%), so a candidate governs at whatever
    ``maturity_strength`` its sample size entitles it to (user ruling,
    2026-08-18).
    """

    observation = [_observation(
        "a", sequence=1, observed="2026-07-29T06:00:00Z",
        temperature_c=34.6,
    )]
    candidate = _compile(observation, artifact=_artifact(promoted=False))
    promoted = _compile(observation, artifact=_artifact(promoted=True))
    assert candidate.applied_strength == pytest.approx(0.8 * 0.75)
    assert candidate.applied_strength == promoted.applied_strength
    assert set(candidate.likelihood_ratio_by_bucket.values()) != {1.0}
    assert candidate.degradation_reasons == ("ARTIFACT_CANDIDATE_AUTHORITY",)
    # It is the same update a promoted artifact would have applied -- the
    # label is the only difference, and it is recorded.
    assert candidate.posterior_probabilities == promoted.posterior_probabilities
    # The hard bound is unaffected either way: it is physical, not fitted.
    assert candidate.posterior_probabilities["33C"] == 0.0
    assert candidate.posterior_probabilities["34C"] == 0.0


def test_an_artifactless_placeholder_still_reads_as_unpromoted() -> None:
    """No artifact at all is not a candidate, and must not act like one.

    P1 compiles an identity placeholder when the scope has nothing fitted.
    It is built at zero base strength so it cannot move anything, and it
    carries the same ``CANDIDATE_ONLY`` label -- so the label alone cannot be
    the test.
    """

    proof = _compile(
        [_observation(
            "a", sequence=1, observed="2026-07-29T06:00:00Z",
            temperature_c=34.6,
        )],
        artifact=_artifact(promoted=False, base_strength=0.0),
    )
    assert proof.applied_strength == 0.0
    assert set(proof.likelihood_ratio_by_bucket.values()) == {1.0}
    assert proof.degradation_reasons == ("ARTIFACT_NOT_PROMOTED",)
    assert proof.posterior_probabilities == {
        "33C": 0.0,
        "34C": 0.0,
        "35C": 4 / 7,
        "36C": 2 / 7,
        "37C": 1 / 7,
    }


def test_missing_and_long_intervals_have_deterministic_strength_degradation() -> None:
    rows = [
        _observation(
            "a", sequence=1, observed="2026-07-29T00:00:00Z",
            temperature_c=32.0,
        ),
        _observation(
            "b", sequence=2, observed="2026-07-29T03:00:00Z",
            temperature_c=34.0,
        ),
        _observation(
            "c", sequence=3, observed="2026-07-29T06:00:00Z",
            temperature_c=35.0,
        ),
    ]
    proof = _compile(rows)
    assert proof.inferred_missing_intervals == 4
    assert proof.long_interval_count == 2
    assert proof.applied_strength == pytest.approx(
        0.8 * 0.75 * (0.8 ** 4) * (0.5 ** 2)
    )
    assert proof.degradation_reasons == (
        "MISSING_INTERVALS", "LONG_INTERVALS"
    )

    zero = _compile(rows, artifact=_artifact(max_missing_intervals=3))
    assert zero.applied_strength == 0.0
    assert set(zero.likelihood_ratio_by_bucket.values()) == {1.0}
    assert "MISSING_INTERVAL_LIMIT_EXCEEDED" in zero.degradation_reasons


def test_duplicate_and_arrival_order_replay_are_byte_identical() -> None:
    rows = [
        _observation(
            "a", sequence=1, observed="2026-07-29T04:00:00Z",
            temperature_c=34.0,
        ),
        _observation(
            "b", sequence=2, observed="2026-07-29T05:00:00Z",
            temperature_c=35.0,
        ),
        _observation(
            "c", sequence=3, observed="2026-07-29T06:00:00Z",
            temperature_c=34.5,
        ),
    ]
    canonical = _compile(rows)
    replay = _compile([rows[2], rows[0], rows[1], rows[0]])
    assert replay.to_dict() == canonical.to_dict()
    assert replay.canonical_observation_sequence_sha256 == (
        canonical.canonical_observation_sequence_sha256
    )


def test_low_high_low_asof_rewind_returns_to_identical_low_proof() -> None:
    low = _observation(
        "low", sequence=1, observed="2026-07-29T00:00:00Z",
        received="2026-07-29T00:01:00Z", temperature_c=33.0,
    )
    high = _observation(
        "high", sequence=2, observed="2026-07-29T01:00:00Z",
        received="2026-07-29T01:01:00Z", temperature_c=36.0,
    )
    low_again = _observation(
        "low-again", sequence=3, observed="2026-07-29T02:00:00Z",
        received="2026-07-29T02:01:00Z", temperature_c=34.0,
    )
    rows = [low, high, low_again]
    artifact = _artifact(local_time_bucket="MORNING")
    first_low = _compile(
        rows, artifact=artifact, as_of="2026-07-29T00:30:00Z"
    )
    at_high = _compile(
        rows, artifact=artifact, as_of="2026-07-29T02:30:00Z"
    )
    rewound_low = _compile(
        list(reversed(rows)),
        artifact=artifact,
        as_of="2026-07-29T00:30:00Z",
    )
    assert first_low.to_dict() == rewound_low.to_dict()
    assert at_high.running_high_c == 36.0
    assert at_high.posterior_probabilities["35C"] == 0.0
    assert at_high.running_high_c > low_again.temperature_c


def test_zuuu_local_midnight_starts_new_day_hard_mask_from_valid_time() -> None:
    centers = {
        "26C": 26.0,
        "27C": 27.0,
        "28C": 28.0,
        "29C": 29.0,
        "30C": 30.0,
    }
    upper_bounds = {
        "26C": 26.49,
        "27C": 27.49,
        "28C": 28.49,
        "29C": 29.49,
        "30C": 30.49,
    }
    root = _root(
        station="ZUUU",
        event_date="2026-07-30",
        timezone="Asia/Shanghai",
        bucket_centers=centers,
        probabilities={bucket: 0.2 for bucket in centers},
    )
    artifact = _artifact(
        station="ZUUU",
        source_lane="amsc",
        timezone="Asia/Shanghai",
        local_time_bucket="NIGHT",
    )
    midnight = _observation(
        "midnight",
        sequence=1,
        station="ZUUU",
        lane="amsc",
        event_date="2026-07-30",
        observed="2026-07-29T16:00:00Z",
        received="2026-07-29T16:01:00Z",
        temperature_c=28.0,
    )
    later_low = _observation(
        "later-low",
        sequence=2,
        station="ZUUU",
        lane="amsc",
        event_date="2026-07-30",
        observed="2026-07-29T17:00:00Z",
        received="2026-07-29T17:01:00Z",
        temperature_c=27.0,
    )
    later_high = _observation(
        "later-high",
        sequence=3,
        station="ZUUU",
        lane="amsc",
        event_date="2026-07-30",
        observed="2026-07-29T18:00:00Z",
        received="2026-07-29T18:01:00Z",
        temperature_c=29.2,
    )

    def compile_rows(rows, as_of):
        return compile_metar_trajectory_update_proof(
            root,
            observations=rows,
            artifact=artifact,
            bucket_centers_c=centers,
            bucket_upper_bounds_c=upper_bounds,
            maturity_bin="D0",
            as_of_utc=as_of,
            soft_likelihood_policy="ACTIVE",
        )

    at_midnight = compile_rows(
        [midnight], "2026-07-29T16:10:00Z"
    )
    after_lower = compile_rows(
        [later_low, midnight, midnight], "2026-07-29T17:10:00Z"
    )
    after_higher = compile_rows(
        [later_high, midnight, later_low], "2026-07-29T18:10:00Z"
    )
    replay = compile_rows(
        [later_low, later_high, midnight, midnight],
        "2026-07-29T18:10:00Z",
    )

    assert at_midnight.running_high_c == 28.0
    assert at_midnight.hard_excluded_buckets == ("26C", "27C")
    assert at_midnight.posterior_probabilities["26C"] == 0.0
    assert at_midnight.posterior_probabilities["27C"] == 0.0
    assert at_midnight.posterior_probabilities["28C"] > 0.0
    assert after_lower.running_high_c == 28.0
    assert after_lower.hard_excluded_buckets == ("26C", "27C")
    assert after_higher.running_high_c == 29.2
    assert after_higher.hard_excluded_buckets == ("26C", "27C", "28C")
    assert replay.to_dict() == after_higher.to_dict()

    previous_day = _observation(
        "previous-day",
        sequence=4,
        station="ZUUU",
        lane="amsc",
        event_date="2026-07-30",
        observed="2026-07-29T15:59:59Z",
        received="2026-07-29T16:00:30Z",
        temperature_c=31.0,
    )
    with pytest.raises(
        ContractError,
        match="observation_settlement_day_mismatch",
    ):
        compile_rows(
            [previous_day, midnight], "2026-07-29T16:10:00Z"
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("wrong_lane", "source_lane_mismatch"),
        ("wrong_station", "observation_scope_mismatch"),
        ("wrong_day", "observation_scope_mismatch"),
        ("wrong_timezone", "prediction_root_scope_mismatch"),
    ],
)
def test_formal_lane_scope_and_settlement_timezone_fail_closed(
    change: str, message: str
) -> None:
    kwargs = {}
    artifact = _artifact()
    root = _root()
    if change == "wrong_lane":
        kwargs["lane"] = "amsc"
    elif change == "wrong_station":
        kwargs["station"] = "RJTT"
    elif change == "wrong_day":
        kwargs["event_date"] = "2026-07-30"
    else:
        artifact = _artifact(timezone="Asia/Tokyo")
    row = _observation(
        "scope", sequence=1, observed="2026-07-29T06:00:00Z",
        temperature_c=34.0, **kwargs,
    )
    with pytest.raises(ContractError, match=message):
        _compile([row], artifact=artifact, root=root)


def test_asof_gate_excludes_unavailable_reports_and_rejects_future_event() -> None:
    available = _observation(
        "available", sequence=1, observed="2026-07-29T05:00:00Z",
        received="2026-07-29T05:01:00Z", temperature_c=34.0,
    )
    unavailable = _observation(
        "unavailable", sequence=2, observed="2026-07-29T06:00:00Z",
        received="2026-07-29T07:00:00Z", temperature_c=36.0,
    )
    proof = _compile([unavailable, available])
    assert proof.canonical_observation_ids == ("obs-available",)
    assert proof.running_high_c == 34.0

    with pytest.raises(ContractError, match="received_before_observed"):
        _observation(
            "future", sequence=3, observed="2026-07-29T07:00:00Z",
            received="2026-07-29T06:00:00Z", temperature_c=37.0,
        )


def test_conflicting_duplicate_and_same_slot_are_rejected() -> None:
    row = _observation(
        "same", sequence=1, observed="2026-07-29T06:00:00Z",
        temperature_c=34.0,
    )
    conflict = MetarTrajectoryObservation(
        event=row.event,
        canonical_committed_at_utc=row.canonical_committed_at_utc,
        temperature_c=35.0,
    )
    with pytest.raises(ContractError, match="duplicate_id_conflict"):
        _compile([row, conflict])

    # A same-slot re-transmission that restates the same temperature is the
    # same measurement said again (publisher COR chains and spacing typos);
    # the last-received transmission speaks for the slot.
    same_slot = _observation(
        "other", sequence=2, observed="2026-07-29T06:00:00Z",
        received="2026-07-29T06:02:00Z", temperature_c=34.0,
    )
    proof = _compile([row, same_slot])
    assert proof.canonical_observation_ids == ("obs-other",)

    # A same-slot temperature change resolves to the LOWER reading rather
    # than fail-closing the station-day: this layer feeds bounds, and the
    # day's high is at least the lower transmission whichever one was
    # right, so the conservative pick never wrongly excludes a bucket.
    # (VILK 2026-08-30 00:30Z: 28 then a silently amended 27 with no COR
    # token, so no review machinery would ever have lifted the old halt.)
    warmer_slot = _observation(
        "warmer", sequence=3, observed="2026-07-29T06:00:00Z",
        received="2026-07-29T06:03:00Z", temperature_c=35.0,
    )
    proof = _compile([row, warmer_slot])
    assert proof.canonical_observation_ids == ("obs-same",)
    assert proof.running_high_c == 34.0

    # Symmetric: when the later transmission is the cooler one, it wins the
    # slot -- order of arrival must not decide the bound.
    cooler_slot = _observation(
        "cooler", sequence=4, observed="2026-07-29T06:00:00Z",
        received="2026-07-29T06:04:00Z", temperature_c=33.0,
    )
    proof = _compile([row, cooler_slot])
    assert proof.canonical_observation_ids == ("obs-cooler",)
    assert proof.running_high_c == 33.0


def test_future_unavailable_conflict_cannot_contaminate_asof_prefix() -> None:
    available = _observation(
        "same", sequence=1, observed="2026-07-29T05:00:00Z",
        received="2026-07-29T05:01:00Z", temperature_c=34.0,
    )
    unavailable_conflict = MetarTrajectoryObservation.from_mapping({
        "event": {
            **available.event.to_dict(),
            "received_at_utc": "2026-07-29T07:00:00Z",
            "raw_sha256": "f" * 64,
        },
        "canonical_committed_at_utc": "2026-07-29T07:00:00Z",
        "temperature_c": 36.0,
    })
    proof = _compile([available, unavailable_conflict])
    assert proof.canonical_observation_ids == ("obs-same",)
    assert proof.running_high_c == 34.0


def test_observation_resource_limit_fails_closed() -> None:
    row = _observation(
        "same", sequence=1, observed="2026-07-29T06:00:00Z",
        temperature_c=34.0,
    )
    with pytest.raises(ContractError, match="resource_limit_exceeded"):
        _compile([row] * (MAX_METAR_TRAJECTORY_OBSERVATIONS + 1))


def test_proof_parser_has_independent_sequence_and_vector_resource_limits() -> None:
    row = _observation(
        "bounded",
        sequence=1,
        observed="2026-07-29T06:00:00Z",
        temperature_c=34.0,
    )
    proof = _compile([row]).to_dict()
    excessive_ids = {
        **proof,
        "canonical_observation_ids": [
            f"obs-{index}"
            for index in range(MAX_METAR_TRAJECTORY_OBSERVATIONS + 1)
        ],
    }
    with pytest.raises(ContractError, match="resource_limit_exceeded"):
        MetarTrajectoryUpdateProof.from_mapping(excessive_ids)

    excessive_ratios = {
        **proof,
        "likelihood_ratio_by_bucket": {
            f"bucket-{index}": 1.0
            for index in range(MAX_METAR_TRAJECTORY_BUCKETS + 1)
        },
    }
    with pytest.raises(ContractError, match="resource_limit_exceeded"):
        MetarTrajectoryUpdateProof.from_mapping(excessive_ratios)


def test_previous_p1_cannot_be_used_as_root_or_accumulated() -> None:
    row = _observation(
        "a", sequence=1, observed="2026-07-29T06:00:00Z",
        temperature_c=34.0,
    )
    with pytest.raises(
        ContractError, match="prediction_root_stage_invalid"
    ):
        _compile([row], root=_root(stage="P1"))


def test_root_scope_local_time_training_and_validity_gates_fail_closed() -> None:
    row = _observation(
        "a", sequence=1, observed="2026-07-29T06:00:00Z",
        temperature_c=34.0,
    )
    missing_scope = _root().to_dict()
    missing_scope["lineage"].pop("season_bin")
    with pytest.raises(ContractError, match="root_season_bin_missing"):
        _compile([row], root=ProbabilityEnvelope.from_mapping(missing_scope))

    wrong_scope = _root().to_dict()
    wrong_scope["lineage"]["season_bin"] = "DJF"
    with pytest.raises(ContractError, match="root_season_bin_mismatch"):
        _compile([row], root=ProbabilityEnvelope.from_mapping(wrong_scope))

    with pytest.raises(ContractError, match="local_time_bucket_mismatch"):
        _compile([row], artifact=_artifact(local_time_bucket="MORNING"))

    leaking_payload = _artifact().to_dict()
    leaking_payload.pop("artifact_id")
    leaking_payload.pop("content_sha256")
    leaking_payload["trained_through_utc"] = "2026-07-29T06:00:00Z"
    leaking = build_metar_trajectory_likelihood_artifact(leaking_payload)
    with pytest.raises(ContractError, match="artifact_training_leakage"):
        _compile([row], artifact=leaking)

    stale_root_replay = _compile(
        [row],
        as_of="2026-07-29T15:00:00Z",
        artifact=_artifact(local_time_bucket="NIGHT"),
    )
    assert stale_root_replay.prediction_root_checkpoint_id == (
        _root().checkpoint_id
    )


def test_proof_is_content_addressed_and_full_replay_validation_detects_tamper() -> None:
    rows = [_observation(
        "a", sequence=1, observed="2026-07-29T06:00:00Z",
        temperature_c=34.0,
    )]
    proof = _compile(rows)
    assert proof.proof_id.startswith("metar-proof-")
    assert validate_metar_trajectory_update_proof(
        proof,
        prediction_root=_root(),
        observations=rows,
        artifact=_artifact(),
        bucket_centers_c=BUCKET_CENTERS,
        bucket_upper_bounds_c=BUCKET_UPPER_BOUNDS,
    ) == proof
    with pytest.raises(TypeError):
        proof.posterior_probabilities["35C"] = 0.0  # type: ignore[index]

    tampered = deepcopy(proof.to_dict())
    tampered["posterior_probabilities"]["36C"] += 0.01
    tampered["posterior_probabilities"]["35C"] -= 0.01
    with pytest.raises(
        ContractError,
        match="(proof_id_not_content_addressed|proof_content_sha256_mismatch)",
    ):
        MetarTrajectoryUpdateProof.from_mapping(tampered)


def test_lr_bounds_and_hard_exhaustion_fail_closed() -> None:
    artifact_raw = _artifact().to_dict()
    artifact_raw["likelihood_ratio_floor"] = 1.1
    with pytest.raises(ContractError, match="likelihood_ratio_bounds_invalid"):
        MetarTrajectoryLikelihoodArtifact.from_mapping(artifact_raw)

    row = _observation(
        "hot", sequence=1, observed="2026-07-29T06:00:00Z",
        temperature_c=40.0,
    )
    with pytest.raises(ContractError, match="hard_bound_exhausts_support"):
        _compile([row])


def test_a_bucket_is_dead_the_moment_the_running_high_reaches_its_upper() -> None:
    """The exclusion boundary is ``>=``, for both shapes of upper bound.

    For half-open interval uppers, a settled max equal to the upper lies
    outside the bucket; for first-killing-value thresholds, equality is the
    definition of killed.  The previous strict ``<`` kept the exactly-killed
    bucket alive: LFPB 2026-07-31 carried a running high of 29 against a
    threshold-style "28°C" upper of 29.0, and the provably dead bucket kept
    2.6% of the posterior.  Threshold-style uppers hit the boundary routinely
    because integer-degree markets produce integer thresholds and METAR
    reports integer degrees.
    """

    threshold_style = {
        # First reported C that kills each bucket of a 1°C market.
        "33C": 34.0,
        "34C": 35.0,
        "35C": 36.0,
        "36C": 37.0,
        "37C": 38.0,
    }
    proof = _compile(
        [
            _observation(
                "boundary",
                sequence=1,
                observed="2026-07-29T04:00:00Z",
                temperature_c=35.0,
            ),
        ],
    )
    # Interval-style uppers (34.49 < 35): "34C" and below dead, "35C" alive.
    assert proof.hard_excluded_buckets == ("33C", "34C")

    boundary = compile_metar_trajectory_update_proof(
        _root(),
        observations=[
            _observation(
                "boundary-threshold",
                sequence=1,
                observed="2026-07-29T04:00:00Z",
                temperature_c=35.0,
            ),
        ],
        artifact=_artifact(),
        bucket_centers_c=BUCKET_CENTERS,
        bucket_upper_bounds_c=threshold_style,
        maturity_bin="D0",
        as_of_utc="2026-07-29T04:05:00Z",
    )
    # Threshold-style: running high 35 has exactly reached "34C"'s killing
    # value of 35.0 -- that bucket is dead, not merely wounded.  The running
    # high's own bucket ("35C", threshold 36.0) always survives, so a stricter
    # boundary cannot exhaust the support.
    assert boundary.hard_excluded_buckets == ("33C", "34C")


def test_production_policy_suspends_the_soft_likelihood_and_says_so() -> None:
    """The 2026-09-02 ruling: identity ratios, zero strength, declared.

    Nothing about the artifact changes -- it is still resolved and named in
    the proof, so the record shows what would have governed -- but the
    posterior it produces is the root under the hard bound alone, and the
    proof carries the policy so the served lineage and the execution gate can
    read it rather than infer it from a zero.
    """

    from bayes_basket_target.metar_trajectory import (
        SOFT_LIKELIHOOD_POLICY,
        SOFT_LIKELIHOOD_POLICY_SUSPENDED,
        SOFT_LIKELIHOOD_SUSPENDED_REASON,
    )

    assert SOFT_LIKELIHOOD_POLICY == SOFT_LIKELIHOOD_POLICY_SUSPENDED
    observations = [
        _observation("o1", sequence=1, observed="2026-07-29T04:00:00Z",
                     temperature_c=30.0),
        _observation("o2", sequence=2, observed="2026-07-29T05:00:00Z",
                     temperature_c=33.0),
    ]
    active = _compile(observations, soft_likelihood_policy="ACTIVE")
    default = _compile(observations, soft_likelihood_policy=None)
    assert active.applied_strength > 0.0
    assert set(active.likelihood_ratio_by_bucket.values()) != {1.0}
    assert default.soft_likelihood_policy == SOFT_LIKELIHOOD_POLICY_SUSPENDED
    assert default.applied_strength == 0.0
    assert set(default.likelihood_ratio_by_bucket.values()) == {1.0}
    assert SOFT_LIKELIHOOD_SUSPENDED_REASON in default.degradation_reasons
    # The artifact is still the artifact: lineage is preserved, only the
    # update is silenced.
    assert default.artifact_id == active.artifact_id
    assert default.activation_id == active.activation_id
    assert default.hard_excluded_buckets == active.hard_excluded_buckets
    assert (
        default.conditional_path_state_sha256
        == active.conditional_path_state_sha256
    )
    # The policy is part of the body, so the two proofs are different facts.
    assert default.content_sha256 != active.content_sha256
    assert default.to_dict()["soft_likelihood_policy"] == (
        SOFT_LIKELIHOOD_POLICY_SUSPENDED
    )
    # Round trip and byte-stable replay under the recorded policy.
    parsed = MetarTrajectoryUpdateProof.from_mapping(default.to_dict())
    assert parsed.to_dict() == default.to_dict()
    replayed = validate_metar_trajectory_update_proof(
        default.to_dict(),
        prediction_root=_root(),
        observations=observations,
        artifact=_artifact(),
        bucket_centers_c=BUCKET_CENTERS,
        bucket_upper_bounds_c=BUCKET_UPPER_BOUNDS,
    )
    assert replayed.content_sha256 == default.content_sha256


def test_active_proofs_keep_their_canonical_form_and_replay_as_themselves() -> None:
    """Absent means ACTIVE: proofs written before the field keep their hash,
    and the validator recompiles a historical proof under its own policy
    rather than under today's."""

    observations = [
        _observation("o1", sequence=1, observed="2026-07-29T04:00:00Z",
                     temperature_c=30.0),
        _observation("o2", sequence=2, observed="2026-07-29T05:00:00Z",
                     temperature_c=33.0),
    ]
    active = _compile(observations, soft_likelihood_policy="ACTIVE")
    body = active.to_dict()
    assert "soft_likelihood_policy" not in body
    parsed = MetarTrajectoryUpdateProof.from_mapping(body)
    assert parsed.soft_likelihood_policy == "ACTIVE"
    assert parsed.content_sha256 == active.content_sha256
    # Production's default is now suspension; the validator must still find
    # this ACTIVE proof byte-identical to itself.
    replayed = validate_metar_trajectory_update_proof(
        body,
        prediction_root=_root(),
        observations=observations,
        artifact=_artifact(),
        bucket_centers_c=BUCKET_CENTERS,
        bucket_upper_bounds_c=BUCKET_UPPER_BOUNDS,
    )
    assert replayed.to_dict() == body


def test_a_suspended_proof_cannot_smuggle_strength_or_a_live_ratio() -> None:
    """The policy is a statement about the vectors, and the parser holds it
    to that: a body that says suspended and still carries a non-identity
    ratio, a strength, or no reason is refused, as is an unknown policy."""

    from bayes_basket_target.contracts import content_sha256
    from bayes_basket_target.metar_trajectory import (
        SOFT_LIKELIHOOD_POLICY_SUSPENDED,
        SOFT_LIKELIHOOD_SUSPENDED_REASON,
    )

    observations = [
        _observation("o1", sequence=1, observed="2026-07-29T04:00:00Z",
                     temperature_c=30.0),
        _observation("o2", sequence=2, observed="2026-07-29T05:00:00Z",
                     temperature_c=33.0),
    ]
    active = _compile(observations, soft_likelihood_policy="ACTIVE").to_dict(
        include_content_sha256=False
    )
    active.pop("proof_id")
    forged = {**active, "soft_likelihood_policy": SOFT_LIKELIHOOD_POLICY_SUSPENDED}
    digest = content_sha256(forged)
    forged = {"proof_id": "metar-proof-" + digest[:32], **forged}
    forged["content_sha256"] = content_sha256(forged)
    with pytest.raises(
        ContractError,
        match="metar_trajectory_soft_likelihood_suspension_violated",
    ):
        MetarTrajectoryUpdateProof.from_mapping(forged)
    with pytest.raises(
        ContractError, match="metar_trajectory_soft_likelihood_policy_invalid"
    ):
        _compile(observations, soft_likelihood_policy="PAUSED")
    suspended = _compile(observations, soft_likelihood_policy=None)
    assert SOFT_LIKELIHOOD_SUSPENDED_REASON in suspended.degradation_reasons
