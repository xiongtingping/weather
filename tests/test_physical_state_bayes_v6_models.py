from __future__ import annotations

import json
from pathlib import Path

import pytest

from physical_latent_state_contract import LATENT_STATES
from physical_state_bayes_v6.artifact_contracts import (
    OBSERVATION_ARTIFACT_SCHEMA,
    TRANSITION_ARTIFACT_SCHEMA,
    validate_observation_artifact,
    validate_transition_artifact,
)
from physical_state_bayes_v6.contracts import DurationClocks, JointCell, PreEventPrediction
from physical_state_bayes_v6.core import predict_to_event, update_with_observation
from physical_state_bayes_v6.observation_model import (
    ObservationFeatureVector,
    build_joint_observation_emission,
)
from physical_state_bayes_v6.observation_adapter import build_canonical_emission_input
from physical_state_bayes_v6.temporal_context import CandidateTemporalCondition, ObservationTemporalContext
from physical_state_bayes_v6.transition_model import (
    ForecastScenarioControl,
    build_candidate_forcing_slices,
)


def _transition_artifact():
    transitions = {state: {} for state in LATENT_STATES}
    transitions["warming"] = {
        "slowing_warming": {
            "base_rate_per_hour": 0.2,
            "coefficients": {"forecast_rate_c_per_hour": -0.4},
        },
        "terminal_cooling": {
            "base_rate_per_hour": 0.05,
            "coefficients": {
                "future_max_increment_c": -0.8,
                "precipitation_probability": 2.0,
            },
        },
    }
    transitions["slowing_warming"] = {
        "warming": {
            "base_rate_per_hour": 0.15,
            "coefficients": {"forecast_rate_c_per_hour": 0.5},
        }
    }
    return validate_transition_artifact(
        {
            "schema": TRANSITION_ARTIFACT_SCHEMA,
            "state": "candidate",
            "production_runtime_eligible": False,
            "shadow_only": False,
            "transitions": transitions,
        }
    )


def _observation_artifact():
    models = {}
    for state in LATENT_STATES:
        mean_rate = 1.0 if state == "warming" else (-1.0 if state == "terminal_cooling" else 0.0)
        models[state] = {
            "continuous_mean": [mean_rate, 8.0],
            "continuous_covariance": [[0.5, 0.0], [0.0, 4.0]],
            "weather_signature_probabilities": {
                "clear_dry": 0.8 if state == "warming" else 0.2,
                "precip_convective": 0.1 if state == "warming" else 0.7,
                "__other__": 0.1,
            },
        }
    return validate_observation_artifact(
        {
            "schema": OBSERVATION_ARTIFACT_SCHEMA,
            "state": "candidate",
            "production_runtime_eligible": False,
            "shadow_only": False,
            "state_models": models,
        }
    )


def test_transition_model_is_candidate_conditioned_and_row_normalized() -> None:
    scenarios = {
        34.0: (
            ForecastScenarioControl("low", 1.0, -0.5, 0.2, 1.5, -30.0, 1.0, 0.8, 1.0, 0.8, 1.0, 0.2, 1.0),
        ),
        38.0: (
            ForecastScenarioControl("high", 1.0, 1.0, 4.0, 0.2, 120.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0),
        ),
    }
    slices = build_candidate_forcing_slices(
        y_grid_c=(34.0, 38.0),
        reported_regular_max_c=33,
        pre_event_duration_clocks=DurationClocks(),
        scenarios_by_y=scenarios,
        delta_minutes=30.0,
        transition_artifact=_transition_artifact(),
    )

    low = slices[0].scenarios[0].matrix["warming"]
    high = slices[1].scenarios[0].matrix["warming"]
    assert abs(sum(low.values()) - 1.0) < 1e-9
    assert abs(sum(high.values()) - 1.0) < 1e-9
    assert low["terminal_cooling"] > high["terminal_cooling"]


def test_joint_emission_updates_candidate_mass_through_predicted_state() -> None:
    forcing = build_candidate_forcing_slices(
        y_grid_c=(34.0, 38.0),
        reported_regular_max_c=33,
        pre_event_duration_clocks=DurationClocks(),
        scenarios_by_y={
            34.0: (ForecastScenarioControl("low", 1.0, -0.5, 0.2, 1.5, -30.0, 1.0, 0.8, 1.0, 0.8, 1.0, 0.2, 1.0),),
            38.0: (ForecastScenarioControl("high", 1.0, 1.0, 4.0, 0.2, 120.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0),),
        },
        delta_minutes=90.0,
        transition_artifact=_transition_artifact(),
    )
    prediction = predict_to_event(
        previous_joint=(
            JointCell(34.0, "warming", 0.5),
            JointCell(38.0, "warming", 0.5),
        ),
        previous_checkpoint_sha256="a" * 64,
        previous_reported_regular_max_c=33,
        previous_y_lower_bound_c=32.5,
        duration_clocks_before=DurationClocks(),
        event_id="event-1",
        event_valid_at="2026-07-15T08:00:00Z",
        delta_minutes=90.0,
        forcing_generation_sha256="b" * 64,
        transition_artifact_sha256=str(_transition_artifact()["artifact_sha256"]),
        candidate_forcing=forcing,
    )
    emission = build_joint_observation_emission(
        prediction=prediction,
        is_regular_metar=True,
        reported_temperature_c=34.0,
        cloud_suppressed=False,
        precipitation_present=False,
        cb_present=False,
        features=ObservationFeatureVector(1.0, 8.0, "clear_dry"),
        observation_artifact=_observation_artifact(),
    )
    post = update_with_observation(prediction=prediction, emission=emission)
    mass = {}
    for row in post.joint:
        mass[row.y_c] = mass.get(row.y_c, 0.0) + row.probability

    assert mass[38.0] > 0.5
    assert post.invariant_report["current_event_consumed_only_in_emission"] is True


def test_short_interval_integer_jump_uses_quantization_covariance_and_robust_tail() -> None:
    artifact = json.loads(
        (Path(__file__).resolve().parents[1] / "config/physical_state_bayes_v6_observation_artifact.json").read_text()
    )
    states = tuple(LATENT_STATES)
    prediction = PreEventPrediction(
        schema="test",
        previous_checkpoint_sha256="a" * 64,
        event_id="short-integer-jump",
        event_valid_at="2026-07-16T16:36:00Z",
        delta_minutes=20.0,
        previous_reported_regular_max_c=27,
        previous_y_lower_bound_c=26.5,
        duration_clocks_before=DurationClocks(),
        joint=tuple(JointCell(30.0, state, 1.0 / len(states)) for state in states),
        forcing_generation_sha256="b" * 64,
        transition_artifact_sha256="c" * 64,
        candidate_transition_trace={},
    )
    emission = build_joint_observation_emission(
        prediction=prediction,
        is_regular_metar=False,
        reported_temperature_c=25.0,
        cloud_suppressed=False,
        precipitation_present=True,
        cb_present=True,
        features=ObservationFeatureVector(
            -6.0,
            0.0,
            "cloud=scattered|weather=convective|convective=1",
            current_step_rate_interval_c_per_hour=(-9.0, -3.0),
            dewpoint_depression_interval_c=(-1.0, 1.0),
            continuous_measurement_covariance=((1.5, 0.25), (0.25, 1.0 / 6.0)),
        ),
        observation_artifact=artifact,
    )
    scores = {state: emission.log_emission_by_cell[(30.0, state)] for state in states}

    assert max(scores.values()) - min(scores.values()) < 8.0
    assert scores["final_locked"] < scores["terminal_cooling"]
    assert emission.observation_feature_trace["likelihood_policy"] == "quantization_covariance_plus_huber_mahalanobis"
    for state in states:
        components = emission.log_component_by_cell[(30.0, state)]
        assert components["total_log_emission"] == pytest.approx(
            components["temperature_momentum_log_score"]
            + components["physical_fields_log_score"]
            + components["temporal_adjustment_log_score"]
        )


def test_correlated_storm_reports_temper_soft_emission_but_not_first_observation() -> None:
    artifact = json.loads(
        (Path(__file__).resolve().parents[1] / "config/physical_state_bayes_v6_observation_artifact.json").read_text()
    )
    states = tuple(LATENT_STATES)

    def emission(*, previous_max, clocks, delta_minutes):
        prediction = PreEventPrediction(
            schema="test", previous_checkpoint_sha256="a" * 64,
            event_id=f"storm-{previous_max}-{delta_minutes}",
            event_valid_at="2026-07-16T16:14:00Z", delta_minutes=delta_minutes,
            previous_reported_regular_max_c=previous_max,
            previous_y_lower_bound_c=None if previous_max is None else previous_max - 0.5,
            duration_clocks_before=clocks,
            joint=tuple(JointCell(34.0, state, 1.0 / len(states)) for state in states),
            forcing_generation_sha256="b" * 64, transition_artifact_sha256="c" * 64,
            candidate_transition_trace={},
        )
        return build_joint_observation_emission(
            prediction=prediction, is_regular_metar=previous_max is None,
            reported_temperature_c=27.0, cloud_suppressed=False,
            precipitation_present=True, cb_present=True,
            features=ObservationFeatureVector(
                -2.0, 1.0, "cloud=scattered|weather=convective|convective_kind=thunderstorm",
                convective_evidence_kind="thunderstorm",
                physical_event_cluster_signature="storm-cluster",
            ),
            observation_artifact=artifact,
            temporal_context=_temporal(early=True, regime="convection_or_showers"),
        )

    first = emission(previous_max=None, clocks=DurationClocks(), delta_minutes=30.0)
    repeated = emission(
        previous_max=27,
        clocks=DurationClocks(
            precipitation_minutes=14.0, cb_minutes=14.0,
            precipitation_active=True, cb_active=True,
            canonical_event_count=1,
            physical_event_cluster_signature="storm-cluster",
            physical_event_cluster_information_spent=1.0,
        ),
        delta_minutes=2.0,
    )
    first_trace = first.observation_feature_trace["time_conditioned_likelihood"]["event_correlation_discount"]
    repeated_trace = repeated.observation_feature_trace["time_conditioned_likelihood"]["event_correlation_discount"]
    assert first_trace["event_information_weight"] == 1.0
    assert repeated_trace["event_information_weight"] == pytest.approx(0.13)
    assert repeated_trace["hard_bounds_discounted"] is False
    first_spread = max(first.log_emission_by_cell.values()) - min(first.log_emission_by_cell.values())
    repeated_spread = max(repeated.log_emission_by_cell.values()) - min(repeated.log_emission_by_cell.values())
    assert repeated_spread == pytest.approx(first_spread * 0.13)


def test_first_cb_cannot_gain_high_temperature_mass_through_latent_state_coupling() -> None:
    artifact = json.loads(
        (Path(__file__).resolve().parents[1] / "config/physical_state_bayes_v6_observation_artifact.json").read_text()
    )
    states = tuple(LATENT_STATES)
    low_condition = CandidateTemporalCondition(1.0, 2.0, 120.0, 1.0)
    high_condition = CandidateTemporalCondition(1.0, 7.0, 720.0, 1.0)
    context = ObservationTemporalContext(
        local_time="2026-07-17T00:14:00+08:00", local_hour=0.23,
        solar_elevation_sine=0.0, hours_until_sunset=18.7,
        conditions_by_y={29.0: low_condition, 34.0: high_condition},
        forecast_regime_probabilities={"clear_dry": 1.0},
        forecast_regime_unknown_probability=0.0,
    )
    prediction = PreEventPrediction(
        schema="test", previous_checkpoint_sha256="a" * 64, event_id="first-cb",
        event_valid_at="2026-07-16T16:14:00Z", delta_minutes=14.0,
        previous_reported_regular_max_c=None, previous_y_lower_bound_c=None,
        duration_clocks_before=DurationClocks(),
        joint=tuple(
            JointCell(y_c, state, 0.5 / len(states))
            for y_c in (29.0, 34.0) for state in states
        ),
        forcing_generation_sha256="b" * 64, transition_artifact_sha256="c" * 64,
        candidate_transition_trace={},
    )
    emission = build_joint_observation_emission(
        prediction=prediction, is_regular_metar=True, reported_temperature_c=27.0,
        cloud_suppressed=False, precipitation_present=True, cb_present=True,
        features=ObservationFeatureVector(
            -2.0, 1.0, "cloud=scattered|weather=convective|convective_kind=thunderstorm",
            convective_evidence_kind="thunderstorm",
            physical_event_cluster_signature="storm-cluster",
        ),
        observation_artifact=artifact, temporal_context=context,
    )
    post = update_with_observation(prediction=prediction, emission=emission)
    mass = {y_c: sum(row.probability for row in post.joint if row.y_c == y_c) for y_c in (29.0, 34.0)}
    assert mass[34.0] <= 0.5
    assert mass[29.0] >= 0.5
    guard = emission.observation_feature_trace["time_conditioned_likelihood"]["adverse_y_coupling_guard"]
    assert guard["latent_state_update"] == "candidate_conditional_y_neutral"
    assert guard["candidate_effect_direction"] == "one_sided_high_temperature_suppression_only"
    penalties = {
        float(y): row["candidate_log_penalty"]
        for y, row in guard["candidate_trace"].items()
    }
    assert penalties[34.0] <= penalties[29.0]
    assert guard["tempering_order"] == "temper_then_candidate_conditional_y_center_then_monotone_penalty"


def test_tempered_adverse_state_update_is_y_neutral_before_monotone_penalty() -> None:
    artifact = json.loads(
        (Path(__file__).resolve().parents[1] / "config/physical_state_bayes_v6_observation_artifact.json").read_text()
    )
    states = tuple(LATENT_STATES)
    prediction = PreEventPrediction(
        schema="test", previous_checkpoint_sha256="a" * 64, event_id="neutral-after-temper",
        event_valid_at="2026-07-16T16:16:00Z", delta_minutes=2.0,
        previous_reported_regular_max_c=27, previous_y_lower_bound_c=26.5,
        duration_clocks_before=DurationClocks(
            precipitation_minutes=14.0, cb_minutes=14.0,
            precipitation_active=True, cb_active=True, canonical_event_count=2,
            physical_event_cluster_signature="storm-cluster",
            physical_event_cluster_information_spent=1.2,
        ),
        joint=tuple(
            JointCell(y, state, (0.7 if y == 29.0 else 0.3) / len(states))
            for y in (29.0, 34.0) for state in states
        ),
        forcing_generation_sha256="b" * 64, transition_artifact_sha256="c" * 64,
        candidate_transition_trace={},
    )
    context = ObservationTemporalContext(
        local_time="2026-07-17T00:16:00+08:00", local_hour=0.27,
        solar_elevation_sine=0.0, hours_until_sunset=18.7,
        conditions_by_y={
            29.0: CandidateTemporalCondition(0.5, 2.0, 120.0, 1.0),
            34.0: CandidateTemporalCondition(1.0, 7.0, 720.0, 1.0),
        },
        forecast_regime_probabilities={"convection_or_showers": 1.0},
        forecast_regime_unknown_probability=0.0,
    )
    emission = build_joint_observation_emission(
        prediction=prediction, is_regular_metar=False, reported_temperature_c=27.0,
        cloud_suppressed=False, precipitation_present=True, cb_present=True,
        features=ObservationFeatureVector(
            -1.0, 1.0, "cloud=scattered|weather=convective|convective_kind=thunderstorm",
            convective_evidence_kind="thunderstorm",
            physical_event_cluster_signature="storm-cluster",
        ),
        observation_artifact=artifact, temporal_context=context,
    )
    guard = emission.observation_feature_trace["time_conditioned_likelihood"]["adverse_y_coupling_guard"]
    for y in (29.0, 34.0):
        total_y = sum(cell.probability for cell in prediction.joint if cell.y_c == y)
        marginal = sum(
            cell.probability / total_y
            * __import__("math").exp(emission.log_emission_by_cell[(cell.y_c, cell.state)])
            for cell in prediction.joint if cell.y_c == y
        )
        expected = __import__("math").exp(guard["candidate_trace"][f"{y:g}"]["candidate_log_penalty"])
        assert marginal == pytest.approx(expected, abs=1e-12)


def test_tcu_is_distinct_from_cb_and_thunderstorm() -> None:
    def event(event_id: str, cloud: str, weather: str):
        return {
            "canonical_observation_event_id": event_id,
            "valid_time": "2026-07-16T20:30:00Z",
            "report_type": "METAR",
            "parsed_observation": {
                "temperature_c": 23.0, "dewpoint_c": 22.0,
                "cloud_text": cloud, "weather_text": weather,
            },
        }
    tcu = build_canonical_emission_input(
        canonical_event=event("tcu", "FEW040TCU SCT040", "SHRA"), pre_event_history=[]
    )
    cb = build_canonical_emission_input(
        canonical_event=event("cb", "FEW040CB SCT040", "SHRA"), pre_event_history=[]
    )
    ts = build_canonical_emission_input(
        canonical_event=event("ts", "FEW040 SCT040", "TSRA"), pre_event_history=[]
    )
    assert (tcu.tcu_present, tcu.cb_present, tcu.convective_evidence_kind) == (True, False, "tcu")
    assert (cb.tcu_present, cb.cb_present, cb.convective_evidence_kind) == (False, True, "cb")
    assert (ts.tcu_present, ts.cb_present, ts.convective_evidence_kind) == (False, True, "thunderstorm")


def test_physical_event_cluster_uses_event_count_change_points_and_budget() -> None:
    artifact = json.loads(
        (Path(__file__).resolve().parents[1] / "config/physical_state_bayes_v6_observation_artifact.json").read_text()
    )
    states = tuple(LATENT_STATES)

    def build(clocks: DurationClocks, signature: str, *, rain: bool, cb: bool, delta: float):
        prediction = PreEventPrediction(
            schema="test", previous_checkpoint_sha256="a" * 64, event_id=f"cluster-{signature}-{delta}",
            event_valid_at="2026-07-16T16:16:00Z", delta_minutes=delta,
            previous_reported_regular_max_c=None, previous_y_lower_bound_c=None,
            duration_clocks_before=clocks,
            joint=tuple(JointCell(34.0, state, 1.0 / len(states)) for state in states),
            forcing_generation_sha256="b" * 64, transition_artifact_sha256="c" * 64,
            candidate_transition_trace={},
        )
        return build_joint_observation_emission(
            prediction=prediction, is_regular_metar=False, reported_temperature_c=27.0,
            cloud_suppressed=False, precipitation_present=rain, cb_present=cb,
            features=ObservationFeatureVector(
                0.0, 1.0, "signature", convective_evidence_kind="thunderstorm" if cb else "none",
                physical_event_cluster_signature=signature,
            ),
            observation_artifact=artifact,
            temporal_context=_temporal(early=True, regime="convection_or_showers"),
        )

    first_speci = build(DurationClocks(), "storm", rain=True, cb=True, delta=2.0)
    repeated = build(
        DurationClocks(
            precipitation_minutes=2.0, cb_minutes=2.0,
            precipitation_active=True, cb_active=True,
            canonical_event_count=1, physical_event_cluster_signature="storm",
            physical_event_cluster_information_spent=1.0,
        ),
        "storm", rain=True, cb=True, delta=2.0,
    )
    exhausted = build(
        DurationClocks(
            precipitation_minutes=60.0, cb_minutes=60.0,
            precipitation_active=True, cb_active=True,
            canonical_event_count=5, physical_event_cluster_signature="storm",
            physical_event_cluster_information_spent=1.95,
        ),
        "storm", rain=True, cb=True, delta=30.0,
    )
    cessation = build(
        DurationClocks(
            precipitation_minutes=60.0, cb_minutes=60.0,
            precipitation_active=True, cb_active=True,
            canonical_event_count=5, physical_event_cluster_signature="storm",
            physical_event_cluster_information_spent=2.0,
        ),
        "dry-platform", rain=False, cb=False, delta=30.0,
    )
    traces = [
        item.observation_feature_trace["time_conditioned_likelihood"]["event_correlation_discount"]
        for item in (first_speci, repeated, exhausted, cessation)
    ]
    assert traces[0]["event_information_weight"] == 1.0
    assert traces[0]["first_observation_full_weight_applied"] is True
    assert traces[1]["event_information_weight"] < 1.0
    assert traces[1]["first_observation_full_weight_applied"] is False
    assert traces[2]["event_information_weight"] == pytest.approx(0.05)
    assert traces[3]["event_information_weight"] == 1.0
    assert traces[3]["physical_event_cluster_change_point"] is True


def _temporal(*, early: bool, regime: str = "") -> ObservationTemporalContext:
    condition = CandidateTemporalCondition(
        forecast_rate_c_per_hour=1.0 if early else -0.3,
        future_max_increment_c=7.0 if early else 0.0,
        minutes_to_curve_peak=720.0 if early else -30.0,
        forcing_availability=1.0,
    )
    return ObservationTemporalContext(
        local_time="2026-07-17T00:36:00+08:00" if early else "2026-07-17T16:00:00+08:00",
        local_hour=0.6 if early else 16.0,
        solar_elevation_sine=0.0 if early else 0.45,
        hours_until_sunset=18.7 if early else 3.0,
        conditions_by_y={34.0: condition},
        forecast_regime_probabilities=({regime: 1.0} if regime else {}),
        forecast_regime_unknown_probability=0.0 if regime else 1.0,
    )


def _phase_scores(
    features: ObservationFeatureVector, *, early: bool, cloud: bool, rain: bool, cb: bool,
    regime: str = "",
):
    artifact = json.loads(
        (Path(__file__).resolve().parents[1] / "config/physical_state_bayes_v6_observation_artifact.json").read_text()
    )
    states = tuple(LATENT_STATES)
    prediction = PreEventPrediction(
        schema="test", previous_checkpoint_sha256="a" * 64, event_id="phase-test",
        event_valid_at="2026-07-16T16:36:00Z", delta_minutes=30.0,
        previous_reported_regular_max_c=27, previous_y_lower_bound_c=26.5,
        duration_clocks_before=DurationClocks(),
        joint=tuple(JointCell(34.0, state, 1.0 / len(states)) for state in states),
        forcing_generation_sha256="b" * 64, transition_artifact_sha256="c" * 64,
        candidate_transition_trace={},
    )
    emission = build_joint_observation_emission(
        prediction=prediction, is_regular_metar=False, reported_temperature_c=27.0,
        cloud_suppressed=cloud, precipitation_present=rain, cb_present=cb,
        features=features, observation_artifact=artifact,
        temporal_context=_temporal(early=early, regime=regime),
    )
    return {state: emission.log_emission_by_cell[(34.0, state)] for state in states}, emission


def test_all_adverse_physical_evidence_is_time_conditioned_not_only_thunderstorm() -> None:
    cases = (
        (ObservationFeatureVector(-2.0, 2.0, "cloud=broken|weather=none|convective=0"), True, False, False),
        (ObservationFeatureVector(-2.0, 1.0, "cloud=scattered|weather=precipitation|convective=0"), False, True, False),
        (ObservationFeatureVector(-3.0, 0.0, "cloud=scattered|weather=convective|convective=1"), False, True, True),
        (ObservationFeatureVector(-1.0, 1.0, "cloud=clear_or_few|weather=none|convective=0", visibility_m=1200.0, fog_or_mist_present=True), False, False, False),
    )
    for features, cloud, rain, cb in cases:
        early, trace = _phase_scores(features, early=True, cloud=cloud, rain=rain, cb=cb)
        late, _ = _phase_scores(features, early=False, cloud=cloud, rain=rain, cb=cb)
        assert early["final_locked"] < late["final_locked"]
        assert early["temporary_cooling"] > late["temporary_cooling"]
        assert trace.observation_feature_trace["time_conditioned_likelihood"]["forecast_role"] == "conditioning_modulator_not_independent_likelihood"


def test_clear_dry_warming_observation_is_early_warming_evidence_not_early_lock() -> None:
    features = ObservationFeatureVector(
        2.0, 10.0, "cloud=clear_or_few|weather=none|convective=0", visibility_m=10000.0
    )
    early, _ = _phase_scores(features, early=True, cloud=False, rain=False, cb=False)
    late, _ = _phase_scores(features, early=False, cloud=False, rain=False, cb=False)
    assert early["warming"] > late["warming"]
    assert early["final_locked"] < late["final_locked"]


def test_forecast_regime_only_conditions_observed_adverse_evidence() -> None:
    fair = ObservationFeatureVector(
        2.0, 10.0, "cloud=clear_or_few|weather=none|convective=0", visibility_m=10000.0
    )
    fair_unknown, _ = _phase_scores(
        fair, early=True, cloud=False, rain=False, cb=False, regime=""
    )
    fair_adverse_forecast, _ = _phase_scores(
        fair, early=True, cloud=False, rain=False, cb=False, regime="convection_or_showers"
    )
    assert fair_adverse_forecast == fair_unknown

    storm = ObservationFeatureVector(
        -2.0, 1.0, "cloud=scattered|weather=convective|convective=1", visibility_m=4000.0
    )
    clear_forecast, clear_trace = _phase_scores(
        storm, early=True, cloud=False, rain=True, cb=True, regime="clear_dry"
    )
    persistent_forecast, _ = _phase_scores(
        storm, early=True, cloud=False, rain=True, cb=True, regime="convection_or_showers"
    )
    assert clear_forecast["cooling_then_rebound"] > persistent_forecast["cooling_then_rebound"]
    assert clear_forecast["final_locked"] < persistent_forecast["final_locked"]
    assert persistent_forecast["weather_suppressed"] > clear_forecast["weather_suppressed"]
    trace = clear_trace.observation_feature_trace["time_conditioned_likelihood"]
    assert trace["forecast_regime_probabilities"] == {"clear_dry": 1.0}
    assert trace["forecast_regime_unknown_probability"] == 0.0
