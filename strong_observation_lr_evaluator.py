"""Deterministic runtime evaluator for a trained strong-observation artifact.

It has one responsibility: evaluate the immutable, approved likelihood
artifact against the input already bound to a canonical D0 observation.  It
returns raw log scores only.  The sequential composer remains the sole owner
of hard conditioning, centring, normalisation and posterior mutation.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from strong_observation_lr_artifact_contract import (
    INPUT_SCHEMA_VERSION,
    REQUIRED_FEATURE_IDS,
    SCHEMA_VERSION as ARTIFACT_SCHEMA_VERSION,
    validate_strong_observation_lr_runtime_artifact,
)
from strong_observation_lr_operational_state_baseline import (
    SCORING_KIND as STATE_AWARE_SCORING_KIND,
    score_operational_state_adjustment,
)
from temperature_trajectory_feature_runtime import cloud_text_contains_cb


SCHEMA_VERSION = "strong_observation_lr_evaluator_v2"
STATION_TIMEZONES = {"ZUUU": "Asia/Shanghai"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_dt(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _base_score(artifact: Mapping[str, Any], *, temperature_c: float, pre_state: str) -> float | None:
    by_cell = _mapping(artifact.get("raw_log_score_by_temperature_and_pre_state"))
    cell_key = f"{temperature_c:g}|{pre_state}"
    if cell_key in by_cell:
        return _finite(by_cell.get(cell_key))
    by_state = _mapping(artifact.get("raw_log_score_by_pre_state"))
    if pre_state in by_state:
        return _finite(by_state.get(pre_state))
    if "default_raw_log_score" in artifact:
        return _finite(artifact.get("default_raw_log_score"))
    return None


def _cloud_regime(value: Any) -> str:
    text = _text(value).upper()
    if not text:
        return "__missing__"
    if any(token in text for token in ("OVC", "VV")):
        return "overcast_or_obscured"
    if "BKN" in text:
        return "broken"
    if "SCT" in text:
        return "scattered"
    if any(token in text for token in ("FEW", "NSC", "NCD", "SKC", "CLR")):
        return "clear_or_few"
    return "__other__"


def _weather_regime(value: Any) -> str:
    text = _text(value).upper()
    if not text or text in {"NSW", "NONE", "-"}:
        return "none"
    if "TS" in text:
        return "thunderstorm"
    if any(token in text for token in ("RA", "DZ", "SN", "SH")):
        return "precipitation"
    if any(token in text for token in ("FG", "BR", "HZ", "FU", "DU", "SA")):
        return "visibility_reduction"
    return "__other__"


def _source_reliability_state(input_row: Mapping[str, Any]) -> str:
    selected_source_id = _text(input_row.get("selected_source_id"))
    pre_state = _mapping(input_row.get("source_reliability_pre_state"))
    source = pre_state.get(selected_source_id)
    if isinstance(source, Mapping):
        state = _text(source.get("state"))
    else:
        state = _text(source)
    return state or "__missing__"


def _trajectory_observation_features(
    canonical: Mapping[str, Any], trajectory: Mapping[str, Any]
) -> dict[str, Any]:
    current_temperature = _finite(canonical.get("temperature_c"))
    current_time = _parse_dt(canonical.get("valid_time"))
    previous_temperature = _finite(trajectory.get("latest_temperature_c"))
    previous_time = _parse_dt(trajectory.get("latest_valid_time"))
    elapsed_hours = (
        (current_time - previous_time).total_seconds() / 3600.0
        if current_time is not None and previous_time is not None and current_time > previous_time
        else None
    )
    current_step_rate = (
        (current_temperature - previous_temperature) / elapsed_hours
        if current_temperature is not None
        and previous_temperature is not None
        and elapsed_hours is not None
        and elapsed_hours > 0.0
        else None
    )
    current_step_state = (
        "no_prior_observation"
        if previous_temperature is None
        else "warming"
        if current_temperature is not None and current_temperature > previous_temperature
        else "cooling"
        if current_temperature is not None and current_temperature < previous_temperature
        else "flat"
    )
    path_evidence = _mapping(trajectory.get("path_state_evidence"))
    previous_peak = _finite(path_evidence.get("observed_peak_display_c"))
    observed_peak = (
        max(value for value in (previous_peak, current_temperature) if value is not None)
        if previous_peak is not None or current_temperature is not None
        else None
    )
    cooling_depth = (
        max(0.0, observed_peak - current_temperature)
        if observed_peak is not None and current_temperature is not None
        else None
    )
    peak_time = _parse_dt(path_evidence.get("observed_peak_valid_time"))
    if current_temperature is not None and observed_peak is not None and current_temperature >= observed_peak:
        cooling_duration = 0.0
    elif current_time is not None and peak_time is not None and current_time >= peak_time:
        cooling_duration = (current_time - peak_time).total_seconds() / 60.0
    else:
        cooling_duration = None
    slopes = _mapping(trajectory.get("slopes"))

    def slope(window: str) -> float | None:
        row = _mapping(slopes.get(window))
        return _finite(row.get("display_rate_c_per_hour")) if _text(row.get("state")) == "available" else None

    station_id = _text(canonical.get("station_id")).upper()
    local_hour = None
    timezone_name = STATION_TIMEZONES.get(station_id)
    if current_time is not None and timezone_name:
        local = current_time.astimezone(ZoneInfo(timezone_name))
        local_hour = local.hour + local.minute / 60.0 + local.second / 3600.0
    return {
        "timing.local_hour": local_hour,
        "trajectory.pre_event_path_state": _text(trajectory.get("path_state_candidate")) or "__missing__",
        "trajectory.current_step_state": current_step_state,
        "trajectory.current_step_rate_c_per_hour": current_step_rate,
        "trajectory.slope_60m_c_per_hour": slope("60m"),
        "trajectory.slope_120m_c_per_hour": slope("120m"),
        "trajectory.acceleration_c_per_hour_squared": _finite(trajectory.get("acceleration_c_per_hour_squared")),
        "trajectory.observed_peak_including_current_c": observed_peak,
        "trajectory.cooling_depth_including_current_c": cooling_depth,
        "trajectory.cooling_duration_minutes": cooling_duration,
    }


def _first_cb_features(canonical: Mapping[str, Any], trajectory: Mapping[str, Any]) -> dict[str, Any]:
    """Build strict-as-of First-CB evidence from pre-event lineage plus O_t."""

    prior = _mapping(trajectory.get("first_cb_evidence"))
    prior_seen = prior.get("cb_seen") is True
    current_seen = cloud_text_contains_cb(canonical.get("cloud_text"))
    current_temperature = _finite(canonical.get("temperature_c"))
    path_evidence = _mapping(trajectory.get("path_state_evidence"))
    prior_peak = _finite(path_evidence.get("observed_peak_display_c"))
    observed_peak = (
        max(value for value in (prior_peak, current_temperature) if value is not None)
        if prior_peak is not None or current_temperature is not None
        else None
    )
    if prior_seen:
        event_state = "cb_already_seen"
        first_valid_time = _text(prior.get("first_cb_valid_time"))
        first_temperature = _finite(prior.get("first_cb_temperature_c"))
        first_at_peak = prior.get("first_cb_at_observed_peak")
    elif current_seen:
        event_state = "first_cb_this_event"
        first_valid_time = _text(canonical.get("valid_time"))
        first_temperature = current_temperature
        first_at_peak = (
            current_temperature >= observed_peak
            if current_temperature is not None and observed_peak is not None
            else None
        )
    else:
        event_state = "no_cb_yet"
        first_valid_time = ""
        first_temperature = None
        first_at_peak = None

    first_time = _parse_dt(first_valid_time)
    station_id = _text(canonical.get("station_id")).upper()
    timezone_name = STATION_TIMEZONES.get(station_id)
    first_local_hour = None
    if first_time is not None and timezone_name:
        local = first_time.astimezone(ZoneInfo(timezone_name))
        first_local_hour = local.hour + local.minute / 60.0 + local.second / 3600.0
    timing_phase = (
        "no_cb_yet"
        if first_local_hour is None
        else "early_before_peak_window"
        if first_local_hour < 14.0
        else "peak_window"
        if first_local_hour < 16.0
        else "late_after_peak_window"
    )
    peak_relation = (
        "no_cb_yet"
        if first_at_peak is None
        else "at_observed_peak"
        if first_at_peak is True
        else "below_observed_peak"
    )
    return {
        "timing.first_cb_local_hour": first_local_hour,
        "physical.first_cb_event_state": event_state,
        "physical.first_cb_timing_phase": timing_phase,
        "physical.first_cb_peak_relation": peak_relation,
        # Audit-only values are intentionally not independent score features.
        "physical.first_cb_valid_time": first_valid_time,
        "physical.first_cb_temperature_c": first_temperature,
        "physical.cb_present_in_current_event": current_seen,
    }


def derive_candidate_dependent_feature_values(
    feature_values: Mapping[str, Any], *, candidate_temperature_c: Any
) -> dict[str, Any]:
    """Bind one candidate X cell to observation evidence without mutating it."""

    values = dict(feature_values)
    candidate = _finite(candidate_temperature_c)
    observed_peak = _finite(values.get("trajectory.observed_peak_including_current_c"))
    values["candidate.remaining_warming_c"] = (
        candidate - observed_peak
        if candidate is not None and observed_peak is not None
        else None
    )
    return values


def extract_strong_observation_feature_values(
    input_row: Mapping[str, Any], *, candidate_temperature_c: Any = None
) -> dict[str, Any]:
    """Extract the fixed physical and trajectory feature vocabulary once.

    The extractor is deterministic and makes no meteorological prediction.  A
    trained artifact chooses the bins/categories and supplies the calibrated
    score surfaces; missing values are explicit evidence states, never zeros.
    """

    canonical = _mapping(input_row.get("canonical_observation"))
    trajectory = _mapping(input_row.get("trajectory_feature_state"))
    temperature = _finite(canonical.get("temperature_c"))
    dewpoint = _finite(canonical.get("dewpoint_c"))
    values = {
        "canonical.temperature_c": temperature,
        **_trajectory_observation_features(canonical, trajectory),
        **_first_cb_features(canonical, trajectory),
        "candidate.remaining_warming_c": None,
        "physical.dewpoint_depression_c": temperature - dewpoint if temperature is not None and dewpoint is not None else None,
        "physical.cloud_regime": _cloud_regime(canonical.get("cloud_text")),
        "physical.weather_regime": _weather_regime(canonical.get("weather_text")),
        "source_reliability.selected_source_state": _source_reliability_state(input_row),
    }
    derived = derive_candidate_dependent_feature_values(
        values,
        candidate_temperature_c=candidate_temperature_c,
    )
    return {feature_id: derived.get(feature_id) for feature_id in REQUIRED_FEATURE_IDS}


def _category_for_feature(definition: Mapping[str, Any], value: Any) -> tuple[str, str | None]:
    """Return calibrated category or an explicit block reason."""

    kind = _text(definition.get("kind"))
    adjustments = _mapping(definition.get("value_score_adjustments"))
    if value is None or value == "__missing__":
        return "__missing__", None if "__missing__" in adjustments else "strong_lr_feature_missing_handler_absent"
    if kind == "categorical":
        label = _text(value)
        if label in adjustments:
            return label, None
        return "__other__", None if "__other__" in adjustments else "strong_lr_feature_other_handler_absent"
    numeric = _finite(value)
    if numeric is None:
        return "__missing__", None if "__missing__" in adjustments else "strong_lr_feature_missing_handler_absent"
    matched: list[str] = []
    for raw_bin in definition.get("bins") or []:
        bin_row = _mapping(raw_bin)
        lower = _finite(bin_row.get("lower_inclusive")) if "lower_inclusive" in bin_row else None
        upper = _finite(bin_row.get("upper_exclusive")) if "upper_exclusive" in bin_row else None
        if (lower is None or numeric >= lower) and (upper is None or numeric < upper):
            matched.append(_text(bin_row.get("label")))
    if len(matched) != 1:
        return "", "strong_lr_feature_numeric_bin_unresolved"
    return matched[0], None


def _adjustment_score(
    adjustment: Mapping[str, Any], *, temperature_c: float, pre_state: str
) -> float | None:
    surface = _mapping(adjustment.get("raw_log_score_adjustment_by_temperature_and_pre_state"))
    cell_key = f"{temperature_c:g}|{pre_state}"
    if cell_key in surface:
        return _finite(surface.get(cell_key))
    if "default" in surface:
        return _finite(surface.get("default"))
    return None


def _operational_physics_score(
    *,
    artifact: Mapping[str, Any],
    feature_values: Mapping[str, Any],
    feature_categories: Mapping[str, str],
    pre_state: str,
) -> tuple[float, dict[str, Any]]:
    """Score one X cell with the governed low-information physics baseline."""

    params = _mapping(artifact.get("operational_physics_parameters"))
    remaining = max(0.0, _finite(feature_values.get("candidate.remaining_warming_c")) or 0.0)
    local_hour = _finite(feature_values.get("timing.local_hour"))
    reference_peak = _finite(params.get("reference_peak_local_hour")) or 15.0
    minimum_hours = _finite(params.get("minimum_hours_to_peak")) or 0.5
    hours_to_peak = max(minimum_hours, reference_peak - local_hour) if local_hour is not None else 2.0

    rate_values = [
        (_finite(feature_values.get("trajectory.current_step_rate_c_per_hour")), 0.5),
        (_finite(feature_values.get("trajectory.slope_60m_c_per_hour")), 0.3),
        (_finite(feature_values.get("trajectory.slope_120m_c_per_hour")), 0.2),
    ]
    available_rate_weight = sum(weight for value, weight in rate_values if value is not None)
    observed_rate = (
        sum(float(value) * weight for value, weight in rate_values if value is not None) / available_rate_weight
        if available_rate_weight > 0.0 else None
    )
    expected_rate = remaining / hours_to_peak
    sigma = max(0.1, _finite(params.get("rate_sigma_c_per_hour")) or 1.25)
    rate_penalty = (
        -0.5 * ((observed_rate - expected_rate) / sigma) ** 2
        if observed_rate is not None else 0.0
    )

    cloud = _text(feature_categories.get("physical.cloud_regime")) or "__missing__"
    weather = _text(feature_categories.get("physical.weather_regime")) or "__missing__"
    dewpoint = _text(feature_categories.get("physical.dewpoint_depression_c")) or "__missing__"
    cloud_multiplier = _finite(_mapping(params.get("cloud_capacity_multiplier")).get(cloud))
    weather_multiplier = _finite(_mapping(params.get("weather_capacity_multiplier")).get(weather))
    dewpoint_multiplier = _finite(_mapping(params.get("dewpoint_capacity_multiplier")).get(dewpoint))
    capacity = (
        hours_to_peak
        * (_finite(params.get("base_clear_heating_capacity_c_per_hour")) or 1.1)
        * (cloud_multiplier if cloud_multiplier is not None else 0.65)
        * (weather_multiplier if weather_multiplier is not None else 0.7)
        * (dewpoint_multiplier if dewpoint_multiplier is not None else 0.8)
    )
    margin = _finite(params.get("remaining_capacity_soft_margin_c")) or 0.75
    excess = max(0.0, remaining - capacity - margin)
    capacity_penalty = -(_finite(params.get("remaining_capacity_excess_penalty")) or 0.22) * excess * excess

    cooling_depth = max(0.0, _finite(feature_values.get("trajectory.cooling_depth_including_current_c")) or 0.0)
    cooling_minutes = max(0.0, _finite(feature_values.get("trajectory.cooling_duration_minutes")) or 0.0)
    cooling_penalty = -remaining * (
        (_finite(params.get("cooling_depth_penalty_per_c")) or 0.16) * cooling_depth
        + (_finite(params.get("cooling_duration_penalty_per_hour")) or 0.12) * cooling_minutes / 60.0
    )
    post_peak_penalty = (
        -remaining * (_finite(params.get("post_peak_remaining_penalty_per_c")) or 0.28)
        if local_hour is not None and local_hour >= reference_peak else 0.0
    )
    first_cb_event_state = _text(feature_categories.get("physical.first_cb_event_state")) or "no_cb_yet"
    first_cb_phase = _text(feature_categories.get("physical.first_cb_timing_phase")) or "no_cb_yet"
    first_cb_peak_relation = _text(feature_categories.get("physical.first_cb_peak_relation")) or "no_cb_yet"
    first_cb_penalty_per_c = _finite(
        _mapping(params.get("first_cb_remaining_warming_penalty_per_c")).get(first_cb_phase)
    ) or 0.0
    first_cb_relation_multiplier = (
        _finite(params.get("first_cb_below_observed_peak_multiplier")) or 1.25
        if first_cb_peak_relation == "below_observed_peak"
        else 1.0
    )
    # First-CB timing is one canonical innovation.  Once its first occurrence
    # has already been consumed by an earlier checkpoint, retaining the timing
    # in trajectory state is useful context but must not multiply the same
    # likelihood penalty again on every later METAR/SPECI.
    first_cb_remaining_warming_penalty = (
        -remaining * first_cb_penalty_per_c * first_cb_relation_multiplier
        if first_cb_event_state == "first_cb_this_event"
        else 0.0
    )
    raw = (
        rate_penalty
        + capacity_penalty
        + cooling_penalty
        + post_peak_penalty
        + first_cb_remaining_warming_penalty
    )
    information_scale = _finite(params.get("event_information_scale")) or 0.35
    preheating_end = _finite(params.get("diurnal_preheating_end_local_hour")) or 8.0
    full_heating_start = _finite(params.get("diurnal_full_heating_start_local_hour")) or 9.0
    if local_hour is None or local_hour <= preheating_end:
        diurnal_information_multiplier = 0.0
    elif local_hour < full_heating_start:
        diurnal_information_multiplier = (
            (local_hour - preheating_end) / max(0.25, full_heating_start - preheating_end)
        )
    else:
        diurnal_information_multiplier = 1.0
    information_scale *= diurnal_information_multiplier
    if _text(feature_categories.get("trajectory.pre_event_path_state")) == "no_prior_observation":
        information_scale *= _finite(params.get("no_history_information_scale")) or 0.25
    cap = max(0.1, _finite(params.get("maximum_relative_log_penalty")) or 2.5)
    score = max(-cap, min(0.0, raw * information_scale))
    return score, {
        "scoring_kind": _text(artifact.get("scoring_kind")),
        "candidate_remaining_warming_c": remaining,
        "observed_rate_c_per_hour": observed_rate,
        "expected_rate_c_per_hour": expected_rate,
        "hours_to_reference_peak": hours_to_peak,
        "physical_remaining_capacity_c": capacity,
        "rate_penalty": rate_penalty,
        "capacity_penalty": capacity_penalty,
        "cooling_penalty": cooling_penalty,
        "post_peak_penalty": post_peak_penalty,
        "first_cb_event_state": first_cb_event_state,
        "first_cb_direct_penalty_consumed_this_event": first_cb_event_state == "first_cb_this_event",
        "first_cb_timing_phase": first_cb_phase,
        "first_cb_peak_relation": first_cb_peak_relation,
        "first_cb_remaining_warming_penalty": first_cb_remaining_warming_penalty,
        "first_cb_penalty_is_zero_at_observed_peak": remaining <= 0.0,
        "information_scale": information_scale,
        "diurnal_information_multiplier": diurnal_information_multiplier,
        "preheating_is_x_likelihood_neutral": diurnal_information_multiplier == 0.0,
        "bounded_raw_log_score": score,
        "pre_state": pre_state,
    }


def _input_blockers(input_row: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if _text(input_row.get("schema")) != INPUT_SCHEMA_VERSION:
        blockers.append("strong_lr_evaluator_input_schema_invalid")
    if input_row.get("ok") is not True or _text(input_row.get("state")) != "available":
        blockers.append("strong_lr_evaluator_input_not_available")
    if not _text(input_row.get("canonical_observation_event_id")):
        blockers.append("strong_lr_evaluator_input_event_identity_missing")
    if not isinstance(input_row.get("canonical_observation"), Mapping):
        blockers.append("strong_lr_evaluator_input_canonical_observation_missing")
    if not isinstance(input_row.get("trajectory_feature_state"), Mapping):
        blockers.append("strong_lr_evaluator_input_trajectory_missing")
    return blockers


def _event_conditioned_post_state_transition(
    *,
    raw_rows: Sequence[Mapping[str, Any]],
    posterior_cells: Sequence[Mapping[str, Any]],
    model: Mapping[str, Any],
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    """Redistribute latent state after O_t without changing physical-X mass.

    The likelihood first scores ``P(O_t | X, Z_t^-)``.  This row-normalized
    conditional then represents ``P(Z_t^+ | O_t, X, Z_t^-)``.  Because every
    row sums to one it cannot apply a second likelihood to X; it only prevents
    a morning regime from becoming an immutable all-day label.
    """

    states = sorted({_text(row.get("pre_state")) for row in raw_rows if _text(row.get("pre_state"))})
    weighted_score = {state: 0.0 for state in states}
    state_mass = {state: 0.0 for state in states}
    unweighted: dict[str, list[float]] = {state: [] for state in states}
    for row, cell in zip(raw_rows, posterior_cells):
        state = _text(row.get("pre_state"))
        score = _finite(row.get("state_log_score_adjustment"))
        probability = _finite(cell.get("probability"))
        if state not in state_mass or score is None:
            continue
        unweighted[state].append(score)
        if probability is not None and probability > 0.0:
            state_mass[state] += probability
            weighted_score[state] += probability * score
    mean_score = {
        state: (
            weighted_score[state] / state_mass[state]
            if state_mass[state] > 0.0
            else sum(unweighted[state]) / len(unweighted[state])
        )
        for state in states
        if unweighted[state]
    }
    if set(mean_score) != set(states):
        raise ValueError("event_conditioned_state_transition_scores_incomplete")
    scale = _finite(model.get("post_state_target_logit_scale")) or 10.0
    maximum = max(mean_score.values())
    exponent = {state: math.exp((mean_score[state] - maximum) * scale) for state in states}
    total = sum(exponent.values())
    floor = _finite(model.get("post_state_probability_floor")) or 0.0
    free_mass = 1.0 - floor * len(states)
    target = {state: floor + free_mass * exponent[state] / total for state in states}
    persistence = _finite(model.get("post_state_persistence")) or 0.0
    transition = {
        pre_state: {
            post_state: (persistence if pre_state == post_state else 0.0)
            + (1.0 - persistence) * target[post_state]
            for post_state in states
        }
        for pre_state in states
    }
    return transition, {
        "schema": "event_conditioned_post_state_transition_v1",
        "mean_state_log_score_adjustment": mean_score,
        "target_post_state_distribution": target,
        "persistence": persistence,
        "probability_floor": floor,
        "target_logit_scale": scale,
        "row_normalized": all(abs(sum(row.values()) - 1.0) <= 1e-12 for row in transition.values()),
        "physical_x_mass_unchanged_by_contract": True,
    }


def evaluate_strong_observation_raw_log_scores(
    *,
    approved_lr_artifact: Mapping[str, Any],
    strong_observation_input: Mapping[str, Any],
    posterior_cells: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate immutable feature maps for the supplied joint posterior cells."""

    artifact = _mapping(approved_lr_artifact)
    input_row = _mapping(strong_observation_input)
    # The hot path validates only the structural scorer.  Strict-as-of data
    # sufficiency and OOS evidence govern offline artifact promotion, never a
    # real-time trade decision.
    artifact_validation = validate_strong_observation_lr_runtime_artifact(artifact)
    blockers = list(artifact_validation.get("issues") or []) + _input_blockers(input_row)
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(blockers)),
            "read_only": True,
        }
    evaluator = _mapping(artifact.get("feature_evaluator"))
    definitions = _mapping(evaluator.get("feature_definitions"))
    evidence_feature_values = extract_strong_observation_feature_values(input_row)
    first_cb_audit = {
        key: value
        for key, value in _first_cb_features(
            _mapping(input_row.get("canonical_observation")),
            _mapping(input_row.get("trajectory_feature_state")),
        ).items()
        if key not in REQUIRED_FEATURE_IDS
    }
    raw_rows: list[dict[str, Any]] = []
    for cell in posterior_cells:
        temperature_c = _finite(cell.get("temperature_c"))
        pre_state = _text(cell.get("state"))
        if temperature_c is None or not pre_state:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "block_reasons": ["strong_lr_evaluator_posterior_cell_invalid"],
                "read_only": True,
            }
        feature_values = derive_candidate_dependent_feature_values(
            evidence_feature_values,
            candidate_temperature_c=temperature_c,
        )
        categories: dict[str, str] = {}
        for feature_id in REQUIRED_FEATURE_IDS:
            category, reason = _category_for_feature(
                _mapping(definitions.get(feature_id)), feature_values.get(feature_id)
            )
            if reason:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "block_reasons": [f"{reason}:{feature_id}"],
                    "read_only": True,
                }
            categories[feature_id] = category
        base = _base_score(artifact, temperature_c=temperature_c, pre_state=pre_state)
        if base is None:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "block_reasons": ["strong_lr_evaluator_base_raw_log_score_missing"],
                "read_only": True,
            }
        contributions: list[dict[str, Any]] = []
        total = base
        for feature_id in REQUIRED_FEATURE_IDS:
            adjustment = _mapping(_mapping(definitions.get(feature_id)).get("value_score_adjustments")).get(categories[feature_id])
            score = _adjustment_score(_mapping(adjustment), temperature_c=temperature_c, pre_state=pre_state)
            if score is None:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "block_reasons": [f"strong_lr_evaluator_feature_score_missing:{feature_id}"],
                    "read_only": True,
                }
            total += score
            contributions.append(
                {
                    "feature_id": feature_id,
                    "value": feature_values[feature_id],
                    "category": categories[feature_id],
                    "raw_log_score_adjustment": score,
                }
            )
        operational_trace: dict[str, Any] = {}
        scoring_kind = _text(artifact.get("scoring_kind"))
        if scoring_kind in {
            "operational_physical_innovation_lr_v1",
            STATE_AWARE_SCORING_KIND,
        }:
            operational_score, operational_trace = _operational_physics_score(
                artifact=artifact,
                feature_values=feature_values,
                feature_categories=categories,
                pre_state=pre_state,
            )
            total += operational_score
        temperature_only_raw_log_score = total
        state_adjustment = 0.0
        state_trace: dict[str, Any] = {}
        if scoring_kind == STATE_AWARE_SCORING_KIND:
            try:
                state_adjustment, state_trace = score_operational_state_adjustment(
                    {
                        "pre_state": pre_state,
                        "feature_values": feature_values,
                        "feature_categories": categories,
                        "operational_physics_trace": operational_trace,
                    },
                    _mapping(artifact.get("operational_state_model")),
                )
            except ValueError as exc:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "block_reasons": [f"strong_lr_state_evaluator_rejected:{exc}"],
                    "read_only": True,
                }
            total += state_adjustment
        raw_rows.append(
            {
                "temperature_c": temperature_c,
                "pre_state": pre_state,
                "base_raw_log_score": base,
                "feature_contributions": contributions,
                "feature_values": feature_values,
                "feature_audit": first_cb_audit,
                "feature_categories": categories,
                "operational_physics_trace": operational_trace,
                "temperature_only_raw_log_score": temperature_only_raw_log_score,
                "state_log_score_adjustment": state_adjustment,
                "state_model_trace": state_trace,
                "scoring_kind": scoring_kind,
                "raw_log_score": total,
            }
        )
    if _text(artifact.get("scoring_kind")) == STATE_AWARE_SCORING_KIND and raw_rows:
        # State evidence is relative evidence over the joint X×state grid.
        # Hard clipping each absolute negative score independently collapses
        # every state to the same floor under strong late-day cooling.  Anchor
        # the best state score at zero (a single global additive constant),
        # then use a monotone soft cap so extreme evidence remains bounded
        # without destroying state ordering.
        unbounded = [
            float(_mapping(row.get("state_model_trace")).get("unbounded_state_log_score") or 0.0)
            for row in raw_rows
        ]
        best = max(unbounded)
        model = _mapping(artifact.get("operational_state_model"))
        cap = max(0.05, float(model.get("maximum_relative_log_penalty") or 1.75))
        for row, raw_state_score in zip(raw_rows, unbounded):
            relative = min(0.0, raw_state_score - best)
            adjustment = -cap * math.tanh((-relative) / cap)
            trace = dict(_mapping(row.get("state_model_trace")))
            trace.update(
                {
                    "relative_score_transform": "global_best_zero_soft_cap_v1",
                    "global_best_unbounded_state_log_score": best,
                    "relative_unbounded_state_log_score": relative,
                    "bounded_state_log_score": adjustment,
                }
            )
            row["state_log_score_adjustment"] = adjustment
            row["state_model_trace"] = trace
            row["raw_log_score"] = float(row.get("temperature_only_raw_log_score") or 0.0) + adjustment
    post_state_transition: dict[str, dict[str, float]] | None = None
    post_state_transition_trace: dict[str, Any] = {}
    if _text(artifact.get("scoring_kind")) == STATE_AWARE_SCORING_KIND:
        model = _mapping(artifact.get("operational_state_model"))
        if model.get("dynamic_post_state_transition") is not True:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "block_reasons": ["state_aware_dynamic_post_transition_missing"],
                "read_only": True,
            }
        try:
            post_state_transition, post_state_transition_trace = _event_conditioned_post_state_transition(
                raw_rows=raw_rows,
                posterior_cells=posterior_cells,
                model=model,
            )
        except ValueError as exc:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "block_reasons": [f"state_aware_dynamic_post_transition_rejected:{exc}"],
                "read_only": True,
            }
    evidence_lineage = {
        "canonical_observation_event_id": _text(input_row.get("canonical_observation_event_id")),
        "pre_event_trajectory_event_ids": list(
            _mapping(input_row.get("trajectory_feature_state")).get("canonical_sequence_event_ids") or []
        ),
        "trajectory_sequence_sha256": _text(_mapping(input_row.get("trajectory_feature_state")).get("canonical_sequence_sha256")),
        "forecast_evidence_snapshot_id": _text(input_row.get("forecast_evidence_snapshot_id")),
        "prior_context_id": _text(input_row.get("prior_context_id")),
        "source_reliability_pre_state_sha256": _sha256(_mapping(input_row.get("source_reliability_pre_state"))),
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "model_artifact_sha256": _text(artifact.get("model_artifact_sha256")),
        "strong_observation_input_sha256": _sha256(input_row),
        "evidence_lineage": evidence_lineage,
        "evidence_feature_values": evidence_feature_values,
        "evidence_feature_audit": first_cb_audit,
        "raw_log_score": raw_rows,
        "raw_log_score_sha256": _sha256(raw_rows),
        "scoring_kind": _text(artifact.get("scoring_kind")),
        "state_discriminating": len(
            {round(float(row.get("state_log_score_adjustment") or 0.0), 12) for row in raw_rows}
        ) > 1,
        "post_state_transition": post_state_transition,
        "post_state_transition_trace": post_state_transition_trace,
        "read_only": True,
    }


__all__ = [
    "SCHEMA_VERSION",
    "derive_candidate_dependent_feature_values",
    "evaluate_strong_observation_raw_log_scores",
    "extract_strong_observation_feature_values",
]
