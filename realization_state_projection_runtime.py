"""Deterministic L3 projection from a committed posterior to trading-state fields."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from lineage_identity_runtime import build_realization_state_id


SCHEMA_VERSION = "realization_state_projection_v2"
POLICY_SCHEMA_VERSION = "realization_state_policy_v2"
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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _policy(value: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    policy = _mapping(value)
    issues: list[str] = []
    if policy.get("schema") != POLICY_SCHEMA_VERSION:
        issues.append("realization_policy_schema_invalid")
    if _text(policy.get("artifact_sha256")) == "" or _text(policy.get("approval_state")) != "approved":
        issues.append("realization_policy_identity_or_approval_invalid")
    parsed: dict[str, Any] = dict(policy)
    for key in (
        "credible_probability_floor",
        "strong_exclusion_probability_ceiling",
    ):
        number = _finite(policy.get(key))
        if number is None or number < 0.0 or number > 1.0:
            issues.append(f"realization_policy_{key}_invalid")
        else:
            parsed[key] = number
    support_floor = _finite(policy.get("strong_support_log_probability_ratio_floor"))
    exclusion_log_ratio = _finite(policy.get("strong_exclusion_log_probability_ratio_ceiling"))
    if support_floor is None:
        issues.append("realization_policy_strong_support_log_probability_ratio_floor_invalid")
    else:
        parsed["strong_support_log_probability_ratio_floor"] = support_floor
    if exclusion_log_ratio is None or exclusion_log_ratio >= 0.0:
        issues.append("realization_policy_strong_exclusion_log_probability_ratio_ceiling_invalid")
    else:
        parsed["strong_exclusion_log_probability_ratio_ceiling"] = exclusion_log_ratio
    return parsed, sorted(set(issues))


def _bucket_rows(contract: Mapping[str, Any]) -> tuple[list[tuple[str, float, float]], list[str]]:
    rows: list[tuple[str, float, float]] = []
    for label, raw in _mapping(contract.get("bucket_contract")).items():
        bounds = _mapping(raw)
        raw_lower = bounds.get("lower_inclusive")
        raw_upper = bounds.get("upper_exclusive")
        lower = float("-inf") if raw_lower is None else _finite(raw_lower)
        upper = float("inf") if raw_upper is None else _finite(raw_upper)
        if (
            not _text(label)
            or lower is None
            or upper is None
            or upper <= lower
            or (math.isinf(lower) and math.isinf(upper))
        ):
            return [], ["resolution_bucket_contract_invalid"]
        rows.append((_text(label), lower, upper))
    rows.sort(key=lambda row: (row[1], row[2], row[0]))
    for left, right in zip(rows, rows[1:]):
        if right[1] < left[2] - EPSILON:
            return [], ["resolution_bucket_contract_overlap"]
    return rows, [] if rows else ["resolution_bucket_contract_missing"]


def build_realization_state_projection(
    *,
    checkpoint: Mapping[str, Any],
    resolution_bridge: Mapping[str, Any],
    resolution_contract: Mapping[str, Any],
    latest_canonical_observation_event: Mapping[str, Any],
    strong_observation_update: Mapping[str, Any],
    strong_physical_evidence: Mapping[str, Any],
    realization_policy: Mapping[str, Any],
    temperature_quantization_c: float,
    peak_timing_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive policy-neutral L3 weather conclusions from committed probability.

    The policy holds only pre-approved posterior thresholds; it cannot consume
    market quotes, EV, Edge, capital, position, or raw weather outside the
    canonical event already bound to this posterior update.
    """

    cp = _mapping(checkpoint)
    bridge = _mapping(resolution_bridge)
    event = _mapping(latest_canonical_observation_event)
    update = _mapping(strong_observation_update)
    physical_evidence = _mapping(strong_physical_evidence)
    timing = _mapping(peak_timing_state)
    policy, issues = _policy(realization_policy)
    if not _text(cp.get("posterior_run_id")) or not _text(cp.get("posterior_checkpoint_id")):
        issues.append("realization_projection_checkpoint_identity_missing")
    if bridge.get("ok") is not True or _text(bridge.get("state")) != "available":
        issues.append("realization_projection_resolution_bridge_unavailable")
    if _text(bridge.get("resolution_contract_id")) != _text(_mapping(resolution_contract).get("resolution_contract_id")):
        issues.append("realization_projection_market_resolution_contract_mismatch")
    if not _text(cp.get("resolution_contract_id")):
        issues.append("realization_projection_physical_resolution_contract_missing")
    if _text(event.get("state")) not in {"accepted_primary", "accepted_substitute"}:
        issues.append("realization_projection_latest_canonical_event_not_accepted")
    if _text(event.get("canonical_observation_event_id")) != _text(cp.get("last_canonical_event_id")):
        issues.append("realization_projection_latest_event_checkpoint_mismatch")
    if _text(update.get("strong_observation_update_id")) != _text(cp.get("last_strong_update_id")):
        issues.append("realization_projection_strong_update_checkpoint_mismatch")
    if physical_evidence.get("ok") is not True or _text(physical_evidence.get("state")) != "available":
        issues.append("realization_projection_strong_physical_evidence_unavailable")
    if _text(physical_evidence.get("canonical_observation_event_id")) != _text(event.get("canonical_observation_event_id")):
        issues.append("realization_projection_strong_physical_evidence_event_mismatch")
    if _text(physical_evidence.get("strong_observation_update_id")) != _text(update.get("strong_observation_update_id")):
        issues.append("realization_projection_strong_physical_evidence_update_mismatch")
    if _text(physical_evidence.get("model_artifact_sha256")) != _text(update.get("model_artifact_sha256")):
        issues.append("realization_projection_strong_physical_evidence_artifact_mismatch")
    if _text(physical_evidence.get("realization_policy_artifact_sha256")) != _text(policy.get("artifact_sha256")):
        issues.append("realization_projection_strong_physical_evidence_policy_mismatch")
    if timing:
        if timing.get("ok") is not True or _text(timing.get("state")) not in {"available", "na"}:
            issues.append("realization_projection_peak_timing_state_invalid")
        if _text(timing.get("canonical_observation_event_id")) != _text(event.get("canonical_observation_event_id")):
            issues.append("realization_projection_peak_timing_event_mismatch")
        if _text(timing.get("strong_observation_update_id")) != _text(update.get("strong_observation_update_id")):
            issues.append("realization_projection_peak_timing_update_mismatch")
    evidence_thresholds = _mapping(physical_evidence.get("thresholds"))
    for key in (
        "strong_support_log_probability_ratio_floor",
        "strong_exclusion_probability_ceiling",
        "strong_exclusion_log_probability_ratio_ceiling",
    ):
        if _finite(evidence_thresholds.get(key)) != _finite(policy.get(key)):
            issues.append(f"realization_projection_strong_physical_evidence_{key}_mismatch")
    bucket_rows, bucket_issues = _bucket_rows(resolution_contract)
    issues.extend(bucket_issues)
    probabilities = _mapping(bridge.get("market_bucket_probabilities"))
    if not probabilities or any(_finite(probabilities.get(label)) is None for label, _lower, _upper in bucket_rows):
        issues.append("realization_projection_market_bucket_probability_missing")
    if issues:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(issues)),
            "read_only": True,
            "market_data_consumed": False,
            "strategy_or_capital_data_consumed": False,
        }
    probability_map = {label: float(probabilities[label]) for label, _lower, _upper in bucket_rows}
    total = sum(probability_map.values())
    if abs(total - 1.0) > 1e-8:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["realization_projection_market_bucket_probability_not_normalized"],
            "read_only": True,
            "market_data_consumed": False,
            "strategy_or_capital_data_consumed": False,
        }
    credible = [label for label, _lower, _upper in bucket_rows if probability_map[label] >= policy["credible_probability_floor"]]
    certificates = _mapping(bridge.get("settlement_exclusion_certificates"))
    ranked = sorted(probability_map.items(), key=lambda item: (-item[1], item[0]))
    timing_projection = dict(timing) if timing else {
        "schema": "peak_timing_state_l3_v1",
        "ok": True,
        "state": "na",
        "na_reason": "peak_timing_state_not_materialized",
        "canonical_observation_event_id": _text(event.get("canonical_observation_event_id")),
        "strong_observation_update_id": _text(update.get("strong_observation_update_id")),
        "entry_peak_at": "",
        "does_not_update_physical_x": True,
        "read_only": True,
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "posterior_run_id": _text(cp.get("posterior_run_id")),
        "posterior_checkpoint_id": _text(cp.get("posterior_checkpoint_id")),
        "resolution_contract_id": _text(bridge.get("resolution_contract_id")),
        "physical_resolution_contract_id": _text(cp.get("resolution_contract_id")),
        "canonical_observation_event_id": _text(event.get("canonical_observation_event_id")),
        "strong_observation_update_id": _text(update.get("strong_observation_update_id")),
        "target_bucket": ranked[0][0],
        "target_probability": ranked[0][1],
        "second_target_bucket": ranked[1][0] if len(ranked) > 1 else "",
        "second_target_probability": ranked[1][1] if len(ranked) > 1 else None,
        "credible_bucket_set": credible,
        "current_report_bucket": "",
        "current_report_bucket_probability": None,
        "probability_above_current_report_bucket": None,
        "current_report_bucket_hard_zero": False,
        "current_report_bucket_na_reason": "physical_x_must_not_be_directly_mapped_to_market_bucket_b",
        "settlement_exclusion_certificates": dict(certificates),
        "strong_physical_evidence": dict(physical_evidence),
        "peak_timing_state": timing_projection,
        "realization_policy_artifact_sha256": _text(policy.get("artifact_sha256")),
        "market_bucket_probabilities": probability_map,
        "boundary": {
            "same_posterior_run_only": True,
            "market_projection_separate_from_physical_checkpoint": True,
            "raw_weather_not_exposed_to_strategy": True,
            "market_data_consumed": False,
            "strategy_or_capital_data_consumed": False,
            "ev_edge_or_fair_value_not_consumed": True,
            "strong_physical_evidence_is_composer_projection": True,
            "peak_timing_is_composer_projection": True,
            "peak_timing_does_not_update_physical_x": True,
            "safe_remote_no_retired": True,
            "trajectory_no_retired": True,
            "current_report_no_retired": True,
            "physical_report_to_market_bucket_direct_mapping_retired": True,
        },
        "read_only": True,
    }


def materialize_projected_realization_state_row(
    *,
    checkpoint_row: Mapping[str, Any],
    realization_projection: Mapping[str, Any],
    fact_snapshot_id: str,
    created_at: str,
    committed_at: str,
    candidate_mode: str,
) -> dict[str, Any]:
    """Materialize a projection as the one P1/L3 realization-state row.

    This function has no database dependency.  Its output is suitable for the
    Transaction-B writer and carries the entire L3 weather conclusion in
    ``state_json`` rather than asking L4 to reconstruct it from raw evidence.
    """

    checkpoint = _mapping(checkpoint_row)
    projection = _mapping(realization_projection)
    if projection.get("ok") is not True or _text(projection.get("state")) != "available":
        raise ValueError("projected_realization_state_projection_not_available")
    if _text(projection.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")):
        raise ValueError("projected_realization_state_posterior_run_mismatch")
    if _text(projection.get("physical_resolution_contract_id")) != _text(checkpoint.get("resolution_contract_id")):
        raise ValueError("projected_realization_state_physical_resolution_contract_mismatch")
    if not _text(projection.get("resolution_contract_id")):
        raise ValueError("projected_realization_state_market_resolution_contract_missing")
    if not _text(fact_snapshot_id) or not _text(created_at) or not _text(committed_at):
        raise ValueError("projected_realization_state_fact_or_time_identity_missing")
    mode = _text(candidate_mode)
    if mode not in {"shadow", "production"}:
        raise ValueError("projected_realization_state_candidate_mode_invalid")
    required_text = (
        "posterior_run_id",
        "posterior_checkpoint_id",
        "resolution_contract_id",
        "canonical_observation_event_id",
        "strong_observation_update_id",
        "target_bucket",
        "realization_policy_artifact_sha256",
    )
    missing = [key for key in required_text if not _text(projection.get(key))]
    if missing:
        raise ValueError(f"projected_realization_state_required_projection_field_missing:{','.join(missing)}")
    for key in (
        "target_probability",
    ):
        value = _finite(projection.get(key))
        if value is None or value < 0.0 or value > 1.0:
            raise ValueError(f"projected_realization_state_probability_invalid:{key}")
    credible = projection.get("credible_bucket_set")
    if not isinstance(credible, list) or any(not _text(bucket) for bucket in credible):
        raise ValueError("projected_realization_state_credible_bucket_set_invalid")
    state_payload = {
        "schema": "realization_state_l3_v2",
        "posterior_run_id": _text(projection.get("posterior_run_id")),
        "posterior_checkpoint_id": _text(projection.get("posterior_checkpoint_id")),
        "canonical_observation_event_id": _text(projection.get("canonical_observation_event_id")),
        "strong_observation_update_id": _text(projection.get("strong_observation_update_id")),
        "projection": dict(projection),
        "candidate_mode": mode,
        "shadow_only": mode == "shadow",
    }
    state_sha256 = _sha256(state_payload)
    state_id = build_realization_state_id(
        posterior_run_id=_text(projection.get("posterior_run_id")),
        fact_snapshot_id=_text(fact_snapshot_id),
        state_sha256=state_sha256,
    )
    certificates = _mapping(projection.get("settlement_exclusion_certificates"))
    return {
        "realization_state_id": state_id,
        "posterior_run_id": _text(projection.get("posterior_run_id")),
        "fact_snapshot_id": _text(fact_snapshot_id),
        "resolution_contract_id": _text(projection.get("resolution_contract_id")),
        "target_bucket": _text(projection.get("target_bucket")),
        "second_target_bucket": _text(projection.get("second_target_bucket")),
        "credible_bucket_set_json": _canonical_json(credible),
        "current_report_bucket": _text(projection.get("current_report_bucket")),
        "current_report_bucket_probability": projection.get("current_report_bucket_probability"),
        "probability_above_current_report_bucket": projection.get("probability_above_current_report_bucket"),
        "current_report_bucket_hard_zero": 1 if bool(projection.get("current_report_bucket_hard_zero")) else 0,
        "settlement_exclusion_certificates_json": _canonical_json(certificates),
        "state_json": _canonical_json(state_payload),
        "state_sha256": state_sha256,
        "created_at": _text(created_at),
        "committed_at": _text(committed_at),
    }


__all__ = [
    "POLICY_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "build_realization_state_projection",
    "materialize_projected_realization_state_row",
]
