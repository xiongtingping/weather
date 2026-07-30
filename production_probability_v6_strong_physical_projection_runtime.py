"""V6-native L3 projection of already-applied strong physical evidence.

This module never re-scores weather and never changes ``p(X)`` or ``p(B)``.
It translates one committed v6 Predict→Update result into the closed evidence
surface consumed by the dynamic three-leg strategy.
"""

from __future__ import annotations

import json
import hashlib
import math
from typing import Any, Mapping


SCHEMA_VERSION = "strong_physical_evidence_projection_v6"
UPDATE_SCHEMA = "strong_observation_update_v6_pre_event_predict_emission"
AMSC_SOURCE_ID = "zuuu_metar_raw_amsc"
EPSILON = 1e-12
TERMINATION_SUPPORT_FLOOR = 0.5
TERMINAL_STATE_MASS_FLOOR = 0.5
TERMINAL_STATES = frozenset(
    {"weather_suppressed", "terminal_cooling", "final_locked"}
)
WARMING_STATES = frozenset({"warming", "slowing_warming", "cooling_then_rebound"})
COOLING_PATH_STATES = frozenset(
    {"cooling", "pullback_watch", "temporary_platform"}
)
WARMING_PATH_STATES = frozenset({"sustained_warming", "rebound"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse(value: Any, default: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    try:
        return json.loads(_text(value))
    except (TypeError, ValueError):
        return default


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _blocked(*reasons: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted({_text(reason) for reason in reasons if _text(reason)}),
        "does_not_update_physical_x": True,
        "creates_probability": False,
        "creates_strategy_signal": False,
        "read_only": True,
    }


def _canonical_sha(value: Mapping[str, Any], *, omit: str = "") -> str:
    payload = {key: item for key, item in value.items() if key != omit}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _valid_hard_exclusion_certificate(
    certificate: Mapping[str, Any], *, bucket: str
) -> bool:
    cert = _mapping(certificate)
    required_hashes = (
        "bridge_artifact_sha256",
        "target_contract_sha256",
        "physical_distribution_sha256",
        "physical_hard_support_sha256",
        "hard_residual_support_authority_sha256",
    )
    return bool(
        _text(cert.get("state")) == "valid"
        and cert.get("full_support_exclusion_verified") is True
        and _text(cert.get("bucket")) == bucket
        and _text(cert.get("resolution_contract_id"))
        and _text(cert.get("posterior_checkpoint_id"))
        and all(len(_text(cert.get(field))) == 64 for field in required_hashes)
        and isinstance(cert.get("physical_hard_support_c"), list)
        and bool(cert.get("physical_hard_support_c"))
        and isinstance(cert.get("hard_residual_support_c"), list)
        and bool(cert.get("hard_residual_support_c"))
        and isinstance(cert.get("settlement_support_witness_c"), list)
        and bool(cert.get("settlement_support_witness_c"))
        and len(_text(cert.get("certificate_sha256"))) == 64
        and _text(cert.get("certificate_sha256"))
        == _canonical_sha(cert, omit="certificate_sha256")
    )


def _probabilities(
    raw: Mapping[str, Any], labels: list[str], *, name: str
) -> tuple[dict[str, float], list[str]]:
    result: dict[str, float] = {}
    issues: list[str] = []
    source = _mapping(raw)
    for label in labels:
        value = _finite(source.get(label))
        if value is None or value < 0.0 or value > 1.0:
            issues.append(f"v6_strong_projection_{name}_probability_invalid")
        else:
            result[label] = value
    if result and abs(sum(result.values()) - 1.0) > 1e-8:
        issues.append(f"v6_strong_projection_{name}_not_normalized")
    return result, issues


def _log_ratio(current: float, previous: float) -> float:
    if current <= EPSILON and previous <= EPSILON:
        return 0.0
    if current <= EPSILON:
        return -50.0
    if previous <= EPSILON:
        return 50.0
    return math.log(current / previous)


def build_v6_strong_physical_evidence_projection(
    *,
    previous_market_bucket_probabilities: Mapping[str, Any],
    posterior_market_bucket_probabilities: Mapping[str, Any],
    bucket_contract: Mapping[str, Any],
    settlement_exclusion_certificates: Mapping[str, Any],
    canonical_observation_event: Mapping[str, Any],
    strong_observation_update: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    realization_policy: Mapping[str, Any],
) -> dict[str, Any]:
    event = _mapping(canonical_observation_event)
    update = _mapping(strong_observation_update)
    cp = _mapping(checkpoint)
    policy = _mapping(realization_policy)
    labels = sorted(_text(label) for label in _mapping(bucket_contract) if _text(label))
    issues: list[str] = []
    if not labels:
        issues.append("v6_strong_projection_bucket_contract_missing")
    event_id = _text(event.get("canonical_observation_event_id"))
    update_id = _text(update.get("strong_observation_update_id"))
    if (
        not event_id
        or _text(event.get("state")) not in {"accepted_primary", "accepted_substitute"}
        or _text(update.get("canonical_observation_event_id")) != event_id
        or _text(cp.get("last_canonical_event_id")) != event_id
        or _text(cp.get("last_strong_update_id")) != update_id
    ):
        issues.append("v6_strong_projection_event_update_checkpoint_binding_invalid")
    if (
        _text(update.get("probability_update_schema")) != UPDATE_SCHEMA
        or _text(update.get("state")) != "applied"
        or int(update.get("no_post_emission_transition") or 0) != 1
        or _parse(update.get("post_state_transition_json"), None) != {}
    ):
        issues.append("v6_strong_projection_update_semantics_invalid")
    if (
        not _text(update.get("observation_artifact_sha256"))
        or _text(update.get("observation_artifact_sha256"))
        != _text(cp.get("observation_artifact_sha256"))
    ):
        issues.append("v6_strong_projection_observation_artifact_binding_invalid")
    auxiliary = _mapping(_parse(update.get("path_auxiliary_json"), {}))
    if (
        auxiliary.get("current_observation_consumed_once") is not True
        or auxiliary.get("no_post_emission_state_transition") is not True
        or auxiliary.get("nwp_hourly_path_likelihood_reapplied") is not False
    ):
        issues.append("v6_strong_projection_evidence_consumption_contract_invalid")
    trajectory = _mapping(auxiliary.get("temperature_trajectory_feature_state"))
    termination = _mapping(auxiliary.get("termination_risk"))
    emission = _mapping(_parse(update.get("observation_emission_json"), {}))
    if (
        trajectory.get("ok") is not True
        or _text(trajectory.get("temporal_role")) != "post_event"
        or _text(trajectory.get("latest_canonical_observation_event_id")) != event_id
    ):
        issues.append("v6_strong_projection_post_event_trajectory_invalid")
    termination_probability = _finite(
        termination.get("probability_no_material_new_daily_high")
    )
    if (
        termination_probability is None
        or not 0.0 <= termination_probability <= 1.0
        or _text(termination.get("canonical_observation_event_id")) != event_id
        or termination.get("updates_posterior") is not False
    ):
        issues.append("v6_strong_projection_termination_invalid")
    if (
        _text(emission.get("event_id")) != event_id
        or _text(emission.get("observation_artifact_sha256"))
        != _text(update.get("observation_artifact_sha256"))
        or not isinstance(emission.get("log_emission_by_cell"), list)
        or not emission.get("log_emission_by_cell")
    ):
        issues.append("v6_strong_projection_emission_lineage_invalid")
    support_floor = _finite(policy.get("strong_support_log_probability_ratio_floor"))
    exclusion_ceiling = _finite(policy.get("strong_exclusion_probability_ceiling"))
    exclusion_ratio_ceiling = _finite(
        policy.get("strong_exclusion_log_probability_ratio_ceiling")
    )
    policy_sha = _text(policy.get("artifact_sha256"))
    if (
        _text(policy.get("schema")) != "realization_state_policy_v2"
        or _text(policy.get("approval_state")) != "approved"
        or not policy_sha
        or support_floor is None
        or exclusion_ceiling is None
        or not 0.0 <= exclusion_ceiling <= 1.0
        or exclusion_ratio_ceiling is None
        or exclusion_ratio_ceiling >= 0.0
    ):
        issues.append("v6_strong_projection_realization_policy_invalid")
    previous, previous_issues = _probabilities(
        previous_market_bucket_probabilities, labels, name="previous"
    )
    posterior, posterior_issues = _probabilities(
        posterior_market_bucket_probabilities, labels, name="posterior"
    )
    issues.extend(previous_issues)
    issues.extend(posterior_issues)
    latent = _mapping(_parse(cp.get("latent_post_state_json"), {}))
    latent_values = {state: _finite(value) for state, value in latent.items()}
    if (
        not latent_values
        or any(value is None or value < 0.0 for value in latent_values.values())
        or abs(sum(value or 0.0 for value in latent_values.values()) - 1.0) > 1e-8
    ):
        issues.append("v6_strong_projection_latent_state_marginal_invalid")
    if issues:
        return _blocked(*issues)

    terminal_mass = sum(float(latent_values.get(state) or 0.0) for state in TERMINAL_STATES)
    warming_mass = sum(float(latent_values.get(state) or 0.0) for state in WARMING_STATES)
    path_state = _text(trajectory.get("path_state_candidate"))
    clocks = _mapping(auxiliary.get("duration_clocks_after"))
    weather_suppression = bool(
        emission.get("cloud_suppressed")
        or emission.get("precipitation_present")
        or emission.get("cb_present")
        or clocks.get("cloud_suppression_active")
        or clocks.get("precipitation_active")
        or clocks.get("cb_active")
    )
    physical_exclusion_support = {
        "termination_risk": termination_probability >= TERMINATION_SUPPORT_FLOOR,
        "terminal_latent_state_mass": terminal_mass >= TERMINAL_STATE_MASS_FLOOR,
        "trajectory_cooling_or_platform": path_state in COOLING_PATH_STATES,
        "weather_suppression": weather_suppression,
    }
    physical_exclusion_support_count = sum(physical_exclusion_support.values())
    physical_reachability_support = {
        "warming_latent_state_mass": warming_mass >= 0.5,
        "trajectory_warming_or_rebound": path_state in WARMING_PATH_STATES,
        "continuation_not_terminated": termination_probability < TERMINATION_SUPPORT_FLOOR,
    }
    top_bucket = max(posterior.items(), key=lambda item: (item[1], item[0]))[0]
    certificates = _mapping(settlement_exclusion_certificates)
    bucket_evidence: dict[str, dict[str, Any]] = {}
    exclusions: list[dict[str, Any]] = []
    source_id = _text(event.get("selected_source_id"))
    for label in labels:
        ratio = _log_ratio(posterior[label], previous[label])
        certificate = _mapping(certificates.get(label))
        hard_excluded = bool(
            _valid_hard_exclusion_certificate(certificate, bucket=label)
            and source_id == AMSC_SOURCE_ID
        )
        soft_unreachable = bool(
            not hard_excluded
            and posterior[label] <= float(exclusion_ceiling) + EPSILON
            and ratio <= float(exclusion_ratio_ceiling)
            and physical_exclusion_support["termination_risk"]
            and physical_exclusion_support_count >= 2
        )
        kind = (
            "observed_daily_high_exceeded"
            if hard_excluded
            else ""
        )
        joint_support = bool(
            ratio >= float(support_floor)
            and any(physical_reachability_support.values())
        )
        bucket_evidence[label] = {
            "bucket": label,
            "previous_probability": previous[label],
            "posterior_probability": posterior[label],
            "log_probability_ratio": ratio,
            "joint_strong_support": joint_support,
            "excluded": bool(kind),
            "exclusion_kind": kind,
            "exclusion_confidence": 1.0 if hard_excluded else None,
            "settlement_exclusion_certificate": dict(certificate) if hard_excluded else {},
            "soft_unreachability_diagnostic": soft_unreachable,
        }
        if kind:
            exclusions.append(
                {
                    "bucket": label,
                    "excluded": True,
                    "kind": kind,
                    "confidence": bucket_evidence[label]["exclusion_confidence"],
                    "composer_joint_strong_model": True,
                    "physical_support": dict(physical_exclusion_support),
                    "physical_support_count": physical_exclusion_support_count,
                    "settlement_exclusion_certificate_state": (
                        "valid"
                    ),
                    "settlement_exclusion_certificate": dict(certificate),
                    "full_support_exclusion_verified": True,
                }
            )
    parsed = _mapping(event.get("parsed_observation"))
    observed = _finite(parsed.get("temperature_c"))
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "canonical_observation_event_id": event_id,
        "strong_observation_update_id": update_id,
        "model_artifact_sha256": _text(update.get("model_artifact_sha256")),
        "observation_artifact_sha256": _text(update.get("observation_artifact_sha256")),
        "realization_policy_artifact_sha256": policy_sha,
        "observation_source_id": source_id,
        "joint_feature_model_applied": True,
        "joint_model_kind": "physical_state_bayes_v6_predict_update",
        "top_bucket": top_bucket,
        "top_bucket_joint_strong_support": bucket_evidence[top_bucket]["joint_strong_support"],
        "safe_no_amsc_observation": source_id == AMSC_SOURCE_ID,
        "safe_no_observation_source": source_id,
        "safe_no_monotonic_daily_high_floor": source_id == AMSC_SOURCE_ID,
        "safe_no_observed_max_c": observed,
        "safe_no_observed_max_lower_bound_c": cp.get("reported_x_lower_bound_c"),
        "safe_no_market_bucket_hard_exclusion_requires_closed_bridge_certificate": True,
        "bucket_evidence": bucket_evidence,
        "bucket_exclusions": exclusions,
        "physical_state_summary": {
            "termination_probability": termination_probability,
            "terminal_latent_state_mass": terminal_mass,
            "warming_latent_state_mass": warming_mass,
            "trajectory_path_state": path_state,
            "weather_suppression": weather_suppression,
            "exclusion_support": physical_exclusion_support,
            "reachability_support": physical_reachability_support,
        },
        "thresholds": {
            "strong_support_log_probability_ratio_floor": support_floor,
            "strong_exclusion_probability_ceiling": exclusion_ceiling,
            "strong_exclusion_log_probability_ratio_ceiling": exclusion_ratio_ceiling,
            "termination_support_floor": TERMINATION_SUPPORT_FLOOR,
            "terminal_state_mass_floor": TERMINAL_STATE_MASS_FLOOR,
            "minimum_joint_physical_exclusion_support_count": 2,
        },
        "boundary": {
            "composer_owned": True,
            "uses_applied_v6_bayes_update_only": True,
            "does_not_rescore_raw_weather": True,
            "does_not_update_physical_x": True,
            "does_not_create_probability": True,
            "market_quote_not_consumed": True,
            "physical_x_not_compared_directly_to_market_bucket_b": True,
            "hard_market_bucket_exclusion_certificate_required": True,
            "soft_unreachability_never_authorizes_no": True,
            "old_v5_feature_trace_not_consumed": True,
        },
        "read_only": True,
        "creates_probability": False,
        "creates_strategy_signal": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "build_v6_strong_physical_evidence_projection",
]
