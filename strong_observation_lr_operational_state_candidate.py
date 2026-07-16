"""Shadow-only state-aware extension of the operational strong LR baseline.

The currently activated operational baseline can discriminate candidate final
temperature ``X`` but deliberately gives every latent path state the same
score.  This module is the controlled replacement candidate: it keeps the
same canonical-event feature owner and temperature physics, then adds an
explicit, low-information ``p(o_t | X, Z_t^-)`` state score.  It never writes
SSOT, changes an activation, or places an order.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Mapping

from strong_observation_lr_artifact_contract import validate_strong_observation_lr_runtime_artifact
from strong_observation_lr_evaluator import evaluate_strong_observation_raw_log_scores
from physical_latent_state_contract import LATENT_STATES
from strong_observation_lr_operational_physics_baseline import build_operational_physics_strong_lr_baseline


SCHEMA_VERSION = "strong_observation_lr_operational_state_candidate_v1"
SCORING_KIND = "operational_physical_state_aware_lr_v2_shadow"


STATE_PROFILES: dict[str, dict[str, float]] = {
    "warming": {"step_rate": 0.9, "trend_rate": 0.7, "remaining_capacity_fraction": 0.80, "remaining_floor_c": 0.50},
    "slowing_warming": {"step_rate": 0.3, "trend_rate": 0.35, "remaining_capacity_fraction": 0.45, "remaining_floor_c": 0.25},
    "temporary_platform": {"step_rate": 0.0, "trend_rate": 0.0, "remaining_capacity_fraction": 0.20, "remaining_floor_c": 0.00},
    "temporary_cooling": {"step_rate": -0.7, "trend_rate": -0.2, "remaining_capacity_fraction": 0.45, "remaining_floor_c": 0.25},
    "cooling_then_rebound": {"step_rate": -0.4, "trend_rate": 0.15, "remaining_capacity_fraction": 0.65, "remaining_floor_c": 0.50},
    "weather_suppressed": {"step_rate": -0.25, "trend_rate": -0.2, "remaining_capacity_fraction": 0.10, "remaining_floor_c": 0.00},
    "terminal_cooling": {"step_rate": -0.9, "trend_rate": -0.55, "remaining_capacity_fraction": 0.00, "remaining_floor_c": 0.00},
    "final_locked": {"step_rate": -0.05, "trend_rate": -0.15, "remaining_capacity_fraction": 0.00, "remaining_floor_c": 0.00},
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sha(value: Any) -> str:
    material = {
        key: item
        for key, item in dict(_mapping(value)).items()
        if key not in {"artifact_sha256", "model_artifact_sha256", "artifact_content_sha256"}
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_operational_state_aware_strong_lr_candidate() -> dict[str, Any]:
    """Return a structurally valid candidate without granting live authority."""

    artifact = copy.deepcopy(build_operational_physics_strong_lr_baseline())
    artifact.update(
        {
            "approval_state": "shadow_only",
            "approval_kind": "operational_precalibration_state_aware_candidate",
            "feature_builder_version": SCHEMA_VERSION,
            # The existing evaluator still owns the temperature-dependent
            # operational score.  The state score below is additive and is
            # evaluated only by this candidate module.
            "scoring_kind": "operational_physical_innovation_lr_v1",
            "production_runtime_eligible": False,
            "shadow_only": True,
            "operational_state_model": {
                "schema": SCORING_KIND,
                "state_profiles": STATE_PROFILES,
                "step_rate_sigma_c_per_hour": 1.25,
                "trend_rate_sigma_c_per_hour": 0.85,
                "remaining_sigma_c": 0.85,
                "state_information_scale": 0.30,
                "maximum_relative_log_penalty": 1.75,
                "early_day_terminal_penalty": 1.25,
                "late_day_warming_penalty_per_hour": 0.12,
                "precipitation_warming_penalty": 0.75,
                "clear_weather_suppressed_penalty": 0.55,
            },
        }
    )
    identity = _sha(artifact)
    artifact["artifact_content_sha256"] = identity
    artifact["artifact_sha256"] = identity
    artifact["model_artifact_sha256"] = identity
    validation = validate_strong_observation_lr_runtime_artifact(artifact)
    if validation.get("ok") is not True:
        raise ValueError("operational_state_candidate_invalid:" + ",".join(validation.get("issues") or []))
    return artifact


def _gaussian_penalty(value: float | None, mean: float, sigma: float) -> float:
    if value is None:
        return 0.0
    return -0.5 * ((value - mean) / max(0.05, sigma)) ** 2


def score_operational_state_adjustment(row: Mapping[str, Any], model: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    state = _text(row.get("pre_state"))
    profile = _mapping(_mapping(model.get("state_profiles")).get(state))
    if state not in LATENT_STATES or not profile:
        raise ValueError("operational_state_candidate_unknown_latent_state")
    values = _mapping(row.get("feature_values"))
    categories = _mapping(row.get("feature_categories"))
    physics = _mapping(row.get("operational_physics_trace"))
    current_rate = _finite(values.get("trajectory.current_step_rate_c_per_hour"))
    trend_rates = [
        _finite(values.get("trajectory.slope_60m_c_per_hour")),
        _finite(values.get("trajectory.slope_120m_c_per_hour")),
    ]
    available_trends = [value for value in trend_rates if value is not None]
    trend_rate = sum(available_trends) / len(available_trends) if available_trends else None
    remaining = max(0.0, _finite(physics.get("candidate_remaining_warming_c")) or 0.0)
    capacity = max(0.0, _finite(physics.get("physical_remaining_capacity_c")) or 0.0)
    expected_remaining = max(
        _finite(profile.get("remaining_floor_c")) or 0.0,
        capacity * (_finite(profile.get("remaining_capacity_fraction")) or 0.0),
    )
    components = {
        "step_rate": _gaussian_penalty(
            current_rate,
            _finite(profile.get("step_rate")) or 0.0,
            _finite(model.get("step_rate_sigma_c_per_hour")) or 1.25,
        ),
        "trend_rate": _gaussian_penalty(
            trend_rate,
            _finite(profile.get("trend_rate")) or 0.0,
            _finite(model.get("trend_rate_sigma_c_per_hour")) or 0.85,
        ),
        "remaining_warming": _gaussian_penalty(
            remaining,
            expected_remaining,
            _finite(model.get("remaining_sigma_c")) or 0.85,
        ),
        "phase": 0.0,
        "physical_regime": 0.0,
    }
    local_hour = _finite(values.get("timing.local_hour"))
    if local_hour is not None and local_hour < 12.0 and state in {"terminal_cooling", "final_locked"}:
        components["phase"] -= (_finite(model.get("early_day_terminal_penalty")) or 1.25) * (12.0 - local_hour) / 6.0
    if local_hour is not None and local_hour > 15.0 and state in {"warming", "cooling_then_rebound"}:
        components["phase"] -= (_finite(model.get("late_day_warming_penalty_per_hour")) or 0.12) * (local_hour - 15.0)
    weather = _text(categories.get("physical.weather_regime"))
    cloud = _text(categories.get("physical.cloud_regime"))
    if weather in {"precipitation", "thunderstorm"} and state in {"warming", "slowing_warming", "cooling_then_rebound"}:
        components["physical_regime"] -= _finite(model.get("precipitation_warming_penalty")) or 0.75
    if weather == "none" and cloud == "clear_or_few" and state == "weather_suppressed":
        components["physical_regime"] -= _finite(model.get("clear_weather_suppressed_penalty")) or 0.55
    raw = sum(components.values()) * (_finite(model.get("state_information_scale")) or 0.30)
    cap = _finite(model.get("maximum_relative_log_penalty")) or 1.75
    adjustment = max(-cap, min(0.0, raw))
    return adjustment, {
        "schema": SCORING_KIND,
        "pre_state": state,
        "current_step_rate_c_per_hour": current_rate,
        "trend_rate_c_per_hour": trend_rate,
        "candidate_remaining_warming_c": remaining,
        "expected_remaining_warming_c": expected_remaining,
        "components_before_information_scale": components,
        "information_scale": _finite(model.get("state_information_scale")) or 0.30,
        "bounded_state_log_score": adjustment,
    }


def evaluate_operational_state_aware_raw_log_scores(
    *,
    candidate_artifact: Mapping[str, Any],
    strong_observation_input: Mapping[str, Any],
    posterior_cells: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate temperature and latent-state evidence without composing a posterior."""

    artifact = _mapping(candidate_artifact)
    if _text(artifact.get("feature_builder_version")) != SCHEMA_VERSION or artifact.get("shadow_only") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["operational_state_candidate_identity_invalid"], "read_only": True, "shadow_only": True}
    result = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=strong_observation_input,
        posterior_cells=posterior_cells,
    )
    if result.get("ok") is not True:
        return dict(result)
    model = _mapping(artifact.get("operational_state_model"))
    rescored: list[dict[str, Any]] = []
    for raw in result.get("raw_log_score") or []:
        row = dict(_mapping(raw))
        adjustment, trace = score_operational_state_adjustment(row, model)
        row["temperature_only_raw_log_score"] = float(row["raw_log_score"])
        row["state_log_score_adjustment"] = adjustment
        row["state_model_trace"] = trace
        row["raw_log_score"] = float(row["raw_log_score"]) + adjustment
        rescored.append(row)
    return {
        **dict(result),
        "schema": SCHEMA_VERSION,
        "scoring_kind": SCORING_KIND,
        "raw_log_score": rescored,
        "raw_log_score_sha256": _sha({"rows": rescored}),
        "state_discriminating": len({round(float(row["state_log_score_adjustment"]), 12) for row in rescored}) > 1,
        "read_only": True,
        "shadow_only": True,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "SCORING_KIND",
    "STATE_PROFILES",
    "build_operational_state_aware_strong_lr_candidate",
    "evaluate_operational_state_aware_raw_log_scores",
    "score_operational_state_adjustment",
]
