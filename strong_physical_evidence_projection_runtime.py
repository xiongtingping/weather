"""Composer-owned strong-physical evidence projection for L3.

This module does not rescore raw weather and does not create a second
posterior.  It projects the already-applied strong Bayes update into explicit
bucket support/exclusion conclusions that a strategy can consume without
reinterpreting METAR, trajectory, momentum, cloud, rain, or dewpoint.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from strong_observation_lr_artifact_contract import REQUIRED_FEATURE_IDS


SCHEMA_VERSION = "strong_physical_evidence_projection_v1"
AMSC_SOURCE_ID = "zuuu_metar_raw_amsc"
EPSILON = 1e-12


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


def _blocked(*reasons: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted(set(reason for reason in reasons if reason)),
        "read_only": True,
        "creates_probability": False,
        "creates_strategy_signal": False,
    }


def _probabilities(value: Any, labels: list[str], prefix: str) -> tuple[dict[str, float], list[str]]:
    source = _mapping(value)
    parsed: dict[str, float] = {}
    issues: list[str] = []
    for label in labels:
        probability = _finite(source.get(label))
        if probability is None or probability < 0.0 or probability > 1.0:
            issues.append(f"strong_evidence_{prefix}_bucket_probability_invalid")
        else:
            parsed[label] = probability
    if parsed and abs(sum(parsed.values()) - 1.0) > 1e-8:
        issues.append(f"strong_evidence_{prefix}_bucket_probability_not_normalized")
    return parsed, issues


def _log_probability_ratio(post: float, pre: float) -> float | None:
    if post <= EPSILON and pre <= EPSILON:
        return 0.0
    if post <= EPSILON:
        return -50.0
    if pre <= EPSILON:
        return 50.0
    return math.log(post / pre)


def build_strong_physical_evidence_projection(
    *,
    previous_market_bucket_probabilities: Mapping[str, Any],
    posterior_market_bucket_probabilities: Mapping[str, Any],
    bucket_contract: Mapping[str, Any],
    settlement_exclusion_certificates: Mapping[str, Any],
    canonical_observation_event: Mapping[str, Any],
    strong_observation_update: Mapping[str, Any],
    observed_daily_high_c: Any,
    observed_max_lower_bound_c: Any,
    realization_policy_artifact_sha256: str,
    strong_support_log_probability_ratio_floor: float,
    strong_exclusion_probability_ceiling: float,
    strong_exclusion_log_probability_ratio_ceiling: float,
) -> dict[str, Any]:
    """Project one already-applied strong update into auditable L3 evidence."""

    event = _mapping(canonical_observation_event)
    update = _mapping(strong_observation_update)
    labels = sorted(_text(label) for label in _mapping(bucket_contract) if _text(label))
    issues: list[str] = []
    if not labels:
        issues.append("strong_evidence_bucket_contract_missing")
    if _text(event.get("canonical_observation_event_id")) != _text(update.get("canonical_observation_event_id")):
        issues.append("strong_evidence_event_update_binding_mismatch")
    if _text(event.get("state")) not in {"accepted_primary", "accepted_substitute"}:
        issues.append("strong_evidence_canonical_event_not_accepted")
    if update.get("ok") is not True or _text(update.get("state")) not in {"applied", "neutral"}:
        issues.append("strong_evidence_strong_update_not_available")
    if not _text(realization_policy_artifact_sha256):
        issues.append("strong_evidence_realization_policy_identity_missing")
    support_floor = _finite(strong_support_log_probability_ratio_floor)
    exclusion_probability = _finite(strong_exclusion_probability_ceiling)
    exclusion_log_ratio = _finite(strong_exclusion_log_probability_ratio_ceiling)
    if support_floor is None:
        issues.append("strong_evidence_support_log_ratio_floor_invalid")
    if exclusion_probability is None or not 0.0 <= exclusion_probability <= 1.0:
        issues.append("strong_evidence_exclusion_probability_ceiling_invalid")
    if exclusion_log_ratio is None or exclusion_log_ratio >= 0.0:
        issues.append("strong_evidence_exclusion_log_ratio_ceiling_invalid")
    previous, previous_issues = _probabilities(previous_market_bucket_probabilities, labels, "previous")
    posterior, posterior_issues = _probabilities(posterior_market_bucket_probabilities, labels, "posterior")
    issues.extend(previous_issues)
    issues.extend(posterior_issues)
    feature_trace = list(update.get("strong_feature_trace_by_cell") or [])
    feature_trace_incomplete = not feature_trace or any(
        any(
            feature_id not in _mapping(_mapping(row).get("feature_values"))
            or not _text(_mapping(_mapping(row).get("feature_categories")).get(feature_id))
            for feature_id in REQUIRED_FEATURE_IDS
        )
        for row in feature_trace
    )
    if feature_trace_incomplete:
        issues.append("strong_evidence_joint_feature_trace_incomplete")
    if issues:
        return _blocked(*issues)

    parsed = _mapping(event.get("parsed_observation"))
    observed_temperature = _finite(parsed.get("temperature_c") if "temperature_c" in parsed else event.get("temperature_c"))
    observed_daily_high = _finite(observed_daily_high_c)
    observed_max_lower_bound = _finite(observed_max_lower_bound_c)
    source_id = _text(event.get("selected_source_id"))
    amsc_primary = source_id == AMSC_SOURCE_ID
    if amsc_primary:
        if observed_temperature is None:
            return _blocked("strong_evidence_amsc_temperature_missing")
        if observed_daily_high is None:
            return _blocked("strong_evidence_amsc_observed_daily_high_missing")
        if observed_max_lower_bound is None:
            return _blocked("strong_evidence_amsc_observed_max_lower_bound_missing")
        if observed_daily_high + EPSILON < observed_temperature:
            return _blocked("strong_evidence_amsc_observed_daily_high_below_current")
        if observed_daily_high + EPSILON < observed_max_lower_bound:
            return _blocked("strong_evidence_amsc_daily_high_below_lower_bound")
    certificates = _mapping(settlement_exclusion_certificates)

    ranked = sorted(posterior.items(), key=lambda item: (-item[1], item[0]))
    top_bucket = ranked[0][0]
    bucket_evidence: dict[str, dict[str, Any]] = {}
    exclusions: list[dict[str, Any]] = []
    for label in labels:
        bounds = _mapping(_mapping(bucket_contract).get(label))
        raw_lower = bounds.get("lower_inclusive")
        raw_upper = bounds.get("upper_exclusive")
        lower = float("-inf") if raw_lower is None else _finite(raw_lower)
        upper = float("inf") if raw_upper is None else _finite(raw_upper)
        if (
            lower is None
            or upper is None
            or upper <= lower
            or (math.isinf(lower) and math.isinf(upper))
        ):
            return _blocked("strong_evidence_bucket_contract_row_invalid")
        ratio = _log_probability_ratio(posterior[label], previous[label])
        support = bool(ratio is not None and ratio >= float(support_floor))
        # X (AMSC physical high) and B (market settlement bucket) are distinct
        # domains. Never compare the physical observation directly with a B
        # boundary. A hard B exclusion requires the closed X->S->B bridge's
        # certificate for this exact posterior distribution.
        certificate = _mapping(certificates.get(label))
        hard_exceeded = bool(
            amsc_primary
            and observed_daily_high is not None
            and observed_max_lower_bound is not None
            and posterior[label] <= EPSILON
            and _text(certificate.get("state")) == "valid"
        )
        remaining_unattainable = bool(
            not hard_exceeded
            and posterior[label] <= float(exclusion_probability) + EPSILON
            and ratio is not None
            and ratio <= float(exclusion_log_ratio)
            and _text(update.get("state")) == "applied"
        )
        exclusion_kind = (
            "observed_daily_high_exceeded"
            if hard_exceeded
            else "remaining_warming_unattainable"
            if remaining_unattainable
            else ""
        )
        row = {
            "bucket": label,
            "previous_probability": previous[label],
            "posterior_probability": posterior[label],
            "log_probability_ratio": ratio,
            "joint_strong_support": support,
            "excluded": bool(exclusion_kind),
            "exclusion_kind": exclusion_kind,
            "exclusion_confidence": 1.0 if hard_exceeded else 1.0 - posterior[label] if remaining_unattainable else None,
            "settlement_exclusion_certificate": dict(certificate) if hard_exceeded else {},
        }
        bucket_evidence[label] = row
        if exclusion_kind:
            exclusions.append(
                {
                    "bucket": label,
                    "excluded": True,
                    "kind": exclusion_kind,
                    "confidence": row["exclusion_confidence"],
                    "composer_joint_strong_model": True,
                    "settlement_exclusion_certificate_state": (
                        _text(certificate.get("state")) if hard_exceeded else "not_required_for_soft_physical_exclusion"
                    ),
                }
            )

    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "canonical_observation_event_id": _text(event.get("canonical_observation_event_id")),
        "strong_observation_update_id": _text(update.get("strong_observation_update_id")),
        "model_artifact_sha256": _text(update.get("model_artifact_sha256")),
        "realization_policy_artifact_sha256": _text(realization_policy_artifact_sha256),
        "observation_source_id": source_id,
        "joint_feature_model_applied": True,
        "joint_feature_ids": list(REQUIRED_FEATURE_IDS),
        "top_bucket": top_bucket,
        "top_bucket_joint_strong_support": bool(bucket_evidence[top_bucket]["joint_strong_support"]),
        "safe_no_amsc_observation": amsc_primary,
        "safe_no_observation_source": source_id,
        "safe_no_monotonic_daily_high_floor": amsc_primary and observed_daily_high is not None,
        "safe_no_observed_max_c": observed_daily_high if amsc_primary else None,
        "safe_no_observed_max_lower_bound_c": observed_max_lower_bound if amsc_primary else None,
        "safe_no_market_bucket_hard_exclusion_requires_closed_bridge_certificate": True,
        "bucket_evidence": bucket_evidence,
        "bucket_exclusions": exclusions,
        "thresholds": {
            "strong_support_log_probability_ratio_floor": support_floor,
            "strong_exclusion_probability_ceiling": exclusion_probability,
            "strong_exclusion_log_probability_ratio_ceiling": exclusion_log_ratio,
        },
        "boundary": {
            "composer_owned": True,
            "uses_applied_bayes_update_only": True,
            "does_not_rescore_raw_weather": True,
            "does_not_create_probability": True,
            "does_not_rank_forecast_sources": True,
            "market_quote_not_consumed": True,
            "physical_x_not_compared_directly_to_market_bucket_b": True,
            "hard_market_bucket_exclusion_certificate_required": True,
        },
        "read_only": True,
        "creates_probability": False,
        "creates_strategy_signal": False,
    }


__all__ = ["AMSC_SOURCE_ID", "SCHEMA_VERSION", "build_strong_physical_evidence_projection"]
