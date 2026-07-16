"""Read-only hierarchical posterior pipeline.

This adapter composes source normalization, phase/data-contract quality,
hard-observation constraints, and the live continuous remaining-warming
sequential-state Bayes posterior into one no-side-effect contract.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from joint_weak_live_evidence_runtime import build_live_joint_weak_likelihood
from metar_strong_likelihood_runtime import build_metar_strong_likelihood
from phase_weighting_runtime import build_phase_weighting_snapshot
from sequential_remaining_warming_bayes_runtime import build_sequential_remaining_warming_posterior
from zuuu_daily_high_candidate_runtime import build_zuuu_daily_high_candidate_contract


SCHEMA_VERSION = "hierarchical_posterior_pipeline_runtime_v1"
DAILY_HIGH_CANDIDATE_LIVE_MODE_ENV = "PM_ZUUU_DAILY_HIGH_CANDIDATE_LIVE_MODE"
DAILY_HIGH_CANDIDATE_DEFAULT_LIVE_MODE = "live_guarded"
DAILY_HIGH_CANDIDATE_LIVE_MODES = frozenset({"live_guarded", "shadow", "baseline"})
FORECAST_FRESH_FULL_HOURS = 12.0
FORECAST_FRESH_REDUCED_HOURS = 24.0
FORECAST_FRESH_STALE_HOURS = 48.0
OBSERVATION_FRESH_FULL_HOURS = 1.5
OBSERVATION_FRESH_REDUCED_HOURS = 3.0


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def _bucket_labels(buckets: Sequence[Any] | None) -> list[str]:
    labels = [str(item.get("bucket") if isinstance(item, Mapping) else item).strip() for item in (buckets or [])]
    return [label for label in labels if label]


def _hard_mask_from_metar(metar_output: Mapping[str, Any]) -> dict[str, float]:
    hard_constraints = metar_output.get("hard_constraints")
    if not isinstance(hard_constraints, Mapping):
        return {}
    hard_mask = hard_constraints.get("hard_mask")
    if not isinstance(hard_mask, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, value in hard_mask.items():
        try:
            out[str(key)] = 1.0 if float(value) > 0 else 0.0
        except (TypeError, ValueError):
            out[str(key)] = 1.0
    return out


def _combine_hard_masks(labels: Sequence[str], *masks: Mapping[str, Any] | None) -> dict[str, float]:
    combined = {label: 1.0 for label in labels}
    for mask in masks:
        if not isinstance(mask, Mapping):
            continue
        for label in labels:
            value = mask.get(label)
            if value is None:
                continue
            try:
                if float(value) <= 0:
                    combined[label] = 0.0
            except (TypeError, ValueError):
                continue
    return combined


def _hard_mask_signals_from_metar(metar_output: Mapping[str, Any]) -> dict[str, Any]:
    constraints = metar_output.get("hard_constraints")
    if not isinstance(constraints, Mapping):
        return {}
    return {
        "current_temperature_c": constraints.get("current_temperature_c"),
        "observed_today_max_c": constraints.get("observed_today_max_c"),
        "hard_lock": constraints.get("hard_lock"),
        "official_final_result": constraints.get("official_final_result"),
        "masked_bucket_count": constraints.get("masked_zero_count"),
        "hard_mask_reason": constraints.get("hard_mask_reason"),
    }


def _source_event_ids(*outputs: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for output in outputs:
        trace = output.get("source_trace")
        if not isinstance(trace, list):
            continue
        for row in trace:
            if not isinstance(row, Mapping) or not row.get("included"):
                continue
            source_event_id = str(row.get("source_event_id") or "").strip()
            if source_event_id:
                ids.append(source_event_id)
    return list(dict.fromkeys(ids))


def _source_observation_ids(*outputs: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for output in outputs:
        trace = output.get("source_trace")
        if not isinstance(trace, list):
            continue
        for row in trace:
            if not isinstance(row, Mapping) or not row.get("included"):
                continue
            source_observation_id = str(row.get("source_observation_id") or "").strip()
            if source_observation_id:
                ids.append(source_observation_id)
    return list(dict.fromkeys(ids))


def _collect_na_reasons(*outputs: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for output in outputs:
        raw = output.get("na_reasons")
        if isinstance(raw, list):
            reasons.extend(str(item) for item in raw if str(item))
    return sorted(set(reasons))


def _source_summary(*outputs: Mapping[str, Any]) -> dict[str, int]:
    summary = {
        "included": 0,
        "excluded": 0,
        "forecast_weak_included": 0,
        "metar_strong_included": 0,
    }
    for output in outputs:
        trace = output.get("source_trace")
        if not isinstance(trace, list):
            continue
        is_forecast = output.get("schema") in {
            "forecast_weak_likelihood_runtime_v1",
            "joint_forecast_weak_likelihood_v1",
        }
        is_metar = output.get("schema") == "metar_strong_likelihood_runtime_v1"
        for row in trace:
            if not isinstance(row, Mapping):
                continue
            if row.get("included"):
                summary["included"] += 1
                if is_forecast:
                    summary["forecast_weak_included"] += 1
                if is_metar:
                    summary["metar_strong_included"] += 1
            else:
                summary["excluded"] += 1
    return summary


def _uniform_likelihood_map(labels: Sequence[str]) -> dict[str, float]:
    if not labels:
        return {}
    value = 1.0 / len(labels)
    return {label: value for label in labels}


def _joint_weak_temperature_grid() -> list[float]:
    """Return the canonical live physical-X grid for the joint weak model.

    The grid is deliberately independent of the currently listed market
    buckets.  Forecast likelihood is about physical maximum temperature X;
    market bucket integration happens only after the posterior is composed.
    """

    return [round(-20.0 + 0.1 * index, 6) for index in range(801)]


def _joint_weak_forecast_output(
    *,
    event_date: Any,
    as_of: Any,
    source_observations: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    live = build_live_joint_weak_likelihood(
        temperature_grid_c=_joint_weak_temperature_grid(),
        event_date=str(event_date or ""),
        decision_as_of=str(as_of or ""),
        source_observations=source_observations,
    )
    runtime = live.get("joint_weak_likelihood")
    if live.get("ok") is not True or not isinstance(runtime, Mapping):
        return {
            "schema": "joint_forecast_weak_likelihood_v1",
            "ok": False,
            "state": "blocked",
            "block_reasons": list(live.get("block_reasons") or ["joint_weak_live_runtime_unavailable"]),
            "source_trace": [],
            "joint_weak_live_evidence": live,
            "read_only": True,
        }

    output = dict(runtime)
    evidence = live.get("forecast_evidence") if isinstance(live.get("forecast_evidence"), Mapping) else {}
    lineage: list[dict[str, Any]] = []
    for row in evidence.get("source_revisions") or []:
        if not isinstance(row, Mapping):
            continue
        available_at = str(row.get("available_at") or "")
        lineage.append(
            {
                "included": True,
                "source_id": str(row.get("source_id") or ""),
                "source_event_id": str(row.get("source_revision_id") or ""),
                "source_observation_id": str(row.get("source_revision_id") or ""),
                "fetched_at": available_at,
                "valid_time": available_at,
                "forecast_peak_c": row.get("forecast_peak_c"),
                "source_class": "forecast_weak_source",
                "role": "joint_forecast_weak_likelihood_expert",
            }
        )
    output["source_trace"] = lineage
    output["joint_weak_live_evidence"] = live
    output["production_model_ready"] = True
    output["production_model_version"] = "joint_forecast_weak_likelihood_v1"
    output["activation_mode"] = "live_approved_artifact"
    output["shadow_only"] = False
    output["normalization"] = {
        "joint_forecast_weak_likelihood_sum": round(
            sum(
                float(row.get("likelihood") or 0.0)
                for row in output.get("joint_forecast_weak_likelihood") or []
                if isinstance(row, Mapping)
            ),
            12,
        )
    }
    return output


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _piecewise_freshness_quality(
    *,
    age_hours: float,
    full_hours: float,
    reduced_hours: float,
    stale_hours: float | None = None,
) -> float:
    age = max(0.0, float(age_hours))
    if age <= full_hours:
        return 1.0
    if age <= reduced_hours:
        fraction = (age - full_hours) / max(1e-9, reduced_hours - full_hours)
        return 1.0 - 0.2 * fraction
    if stale_hours is None or stale_hours <= reduced_hours:
        return 0.5
    if age <= stale_hours:
        fraction = (age - reduced_hours) / max(1e-9, stale_hours - reduced_hours)
        return 0.8 - 0.3 * fraction
    return 0.25


def _trace_freshness_quality(
    *,
    source_trace: Any,
    as_of: Any,
    full_hours: float,
    reduced_hours: float,
    stale_hours: float | None = None,
    aggregation: str = "weighted_mean",
    freshness_time_field: str = "fetched_at",
) -> tuple[float, list[dict[str, Any]], list[str]]:
    as_of_dt = _parse_datetime(as_of)
    rows = [row for row in source_trace or [] if isinstance(row, Mapping) and row.get("included")]
    if not rows:
        return 0.0, [], ["included_source_missing"]
    scores: list[tuple[float, float]] = []
    details: list[dict[str, Any]] = []
    reasons: list[str] = []
    for row in rows:
        fetched_at = row.get("fetched_at")
        fetched_dt = _parse_datetime(fetched_at)
        valid_time = row.get("valid_time")
        freshness_time = row.get(freshness_time_field)
        freshness_dt = _parse_datetime(freshness_time)
        fetched_in_future = (
            as_of_dt is not None
            and fetched_dt is not None
            and (fetched_dt - as_of_dt).total_seconds() > 60.0
        )
        if fetched_in_future:
            score = 0.0
            age_hours = (as_of_dt - fetched_dt).total_seconds() / 3600.0
            state = "future_fetch_time_blocked"
            reasons.append("future_fetch_time")
        elif as_of_dt is None or freshness_dt is None:
            score = 0.0
            age_hours = None
            state = "freshness_metadata_missing_blocked"
            reasons.append("freshness_metadata_missing")
        else:
            delta_seconds = (as_of_dt - freshness_dt).total_seconds()
            if delta_seconds < -60.0:
                score = 0.0
                age_hours = delta_seconds / 3600.0
                state = f"future_{freshness_time_field}_blocked"
                reasons.append(f"future_{freshness_time_field}")
            else:
                age_hours = max(0.0, delta_seconds / 3600.0)
                score = _piecewise_freshness_quality(
                    age_hours=age_hours,
                    full_hours=full_hours,
                    reduced_hours=reduced_hours,
                    stale_hours=stale_hours,
                )
                state = "fresh" if score >= 0.999 else "aged" if score >= 0.5 else "stale"
        weight = max(1e-9, float(row.get("precision_raw") or 1.0))
        scores.append((score, weight))
        details.append(
            {
                "source_event_id": str(row.get("source_event_id") or ""),
                "source_id": str(row.get("source_id") or ""),
                "fetched_at": str(fetched_at or ""),
                "valid_time": str(valid_time or ""),
                "freshness_time_field": freshness_time_field,
                "freshness_time": str(freshness_time or ""),
                "age_hours": round(age_hours, 6) if age_hours is not None else None,
                "quality": round(score, 12),
                "state": state,
            }
        )
    if any(str(row.get("state") or "").startswith("future_") for row in details):
        quality = 0.0
    elif aggregation == "freshest":
        quality = max((score for score, _weight in scores), default=0.0)
    else:
        total_weight = sum(weight for _score, weight in scores)
        quality = sum(score * weight for score, weight in scores) / total_weight if total_weight > 0 else 0.0
    return max(0.0, min(1.0, quality)), details, sorted(set(reasons))


def _weak_information_quality(forecast_output: Mapping[str, Any], *, as_of: Any) -> dict[str, Any]:
    quality, source_rows, reasons = _trace_freshness_quality(
        source_trace=forecast_output.get("source_trace"),
        as_of=as_of,
        full_hours=FORECAST_FRESH_FULL_HOURS,
        reduced_hours=FORECAST_FRESH_REDUCED_HOURS,
        stale_hours=FORECAST_FRESH_STALE_HOURS,
    )
    excluded_reasons = {
        str(row.get("exclusion_reason") or "")
        for row in forecast_output.get("source_trace") or []
        if isinstance(row, Mapping) and not row.get("included")
    }
    if "forecast_future_fetch_time" in excluded_reasons:
        reasons.append("future_fetch_time")
    return {
        "schema": "likelihood_layer_information_quality_v1",
        "layer": "forecast_weak_likelihood",
        "quality": round(quality, 12),
        "state": "available" if quality > 0 else "missing_or_blocked",
        "source_freshness": source_rows,
        "na_reasons": reasons,
        "contract_only_not_weather_direction": True,
        "legacy_model_taf_special_case_retired": True,
        "thresholds_fitted_from_history": False,
    }


def _strong_information_quality(metar_output: Mapping[str, Any], *, as_of: Any) -> dict[str, Any]:
    constraints = metar_output.get("hard_constraints") if isinstance(metar_output.get("hard_constraints"), Mapping) else {}
    hard_lock = bool(constraints.get("hard_lock"))
    has_observation = constraints.get("current_temperature_c") is not None or constraints.get("observed_today_max_c") is not None
    freshness, source_rows, reasons = _trace_freshness_quality(
        source_trace=metar_output.get("source_trace"),
        as_of=as_of,
        full_hours=OBSERVATION_FRESH_FULL_HOURS,
        reduced_hours=OBSERVATION_FRESH_REDUCED_HOURS,
        aggregation="freshest",
        freshness_time_field="valid_time",
    )
    candidate = metar_output.get("daily_high_observation_candidate") if isinstance(metar_output.get("daily_high_observation_candidate"), Mapping) else {}
    snapshot = candidate.get("canonical_observation_snapshot") if isinstance(candidate.get("canonical_observation_snapshot"), Mapping) else {}
    point_count = int(snapshot.get("distinct_valid_time_count") or len(snapshot.get("selected_points") or []))
    trajectory = metar_output.get("temperature_trajectory") if isinstance(metar_output.get("temperature_trajectory"), Mapping) else {}
    confirmed_windows = [str(value) for value in trajectory.get("confirmed_windows") or [] if str(value)]
    rolling = trajectory.get("rolling_temperature_observation_summary") if isinstance(trajectory.get("rolling_temperature_observation_summary"), Mapping) else {}
    rolling_point_count = int(rolling.get("point_count") or 0)
    trajectory_contract_available = len(confirmed_windows) >= 2 or rolling_point_count >= 2
    if hard_lock:
        coverage_quality = 1.0
        state = "hard_lock"
    elif point_count >= 2 or trajectory_contract_available:
        coverage_quality = 1.0
        state = "canonical_trajectory_available"
    elif has_observation:
        coverage_quality = 0.85
        state = "single_snapshot_or_legacy_observation"
        reasons.append("canonical_trajectory_below_two_valid_times")
    else:
        coverage_quality = 0.0
        state = "observation_missing"
        reasons.append("current_temperature_and_observed_max_missing")
    if not source_rows and has_observation:
        freshness = 0.0
        reasons.append("observation_inputs_without_source_freshness_trace")
    quality = 1.0 if hard_lock else min(freshness, coverage_quality)
    return {
        "schema": "likelihood_layer_information_quality_v1",
        "layer": "metar_strong_likelihood",
        "quality": round(max(0.0, min(1.0, quality)), 12),
        "state": state,
        "canonical_distinct_valid_time_count": point_count,
        "trajectory_confirmed_windows": confirmed_windows,
        "rolling_temperature_point_count": rolling_point_count,
        "coverage_quality": round(coverage_quality, 12),
        "source_freshness": source_rows,
        "na_reasons": sorted(set(reasons)),
        "contract_only_not_weather_direction": True,
        "trajectory_cloud_rain_direction_not_reused_in_beta": True,
        "thresholds_fitted_from_history": False,
    }


def _quality_adjusted_phase_output(
    phase_output: Mapping[str, Any],
    *,
    forecast_output: Mapping[str, Any],
    metar_output: Mapping[str, Any],
    as_of: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    adjusted = dict(phase_output)
    weak = _weak_information_quality(forecast_output, as_of=as_of)
    strong = _strong_information_quality(metar_output, as_of=as_of)
    base_weak = max(0.0, float(phase_output.get("beta_weak") or 0.0))
    base_strong = max(0.0, float(phase_output.get("beta_strong") or 0.0))
    effective_weak = base_weak * float(weak.get("quality") or 0.0)
    effective_strong = base_strong * float(strong.get("quality") or 0.0)
    if bool(phase_output.get("hard_lock_active")):
        effective_weak = 0.0
        effective_strong = base_strong
    quality = {
        "schema": "likelihood_information_quality_v1",
        "policy_version": "phase_beta_times_contract_quality_v1_no_fit",
        "forecast_weak": weak,
        "metar_strong": strong,
        "base_beta_weak": base_weak,
        "base_beta_strong": base_strong,
        "effective_beta_weak": round(effective_weak, 12),
        "effective_beta_strong": round(effective_strong, 12),
        "formula": "effective_beta_layer=phase_base_beta_layer*contract_information_quality_layer",
        "beta_weak_and_beta_strong_need_not_sum_to_one": True,
        "hard_mask_not_tempered_by_quality": True,
        "weather_direction_not_reused_in_quality": True,
        "legacy_model_taf_special_case_retired": True,
        "parameters_fitted_from_history": False,
    }
    adjusted["beta_weak_base"] = base_weak
    adjusted["beta_strong_base"] = base_strong
    adjusted["beta_weak"] = round(effective_weak, 12)
    adjusted["beta_strong"] = round(effective_strong, 12)
    adjusted["beta_source"] = f"{phase_output.get('beta_source') or 'default_schedule'}_times_contract_information_quality"
    adjusted["likelihood_information_quality"] = quality
    return adjusted, quality


def _daily_high_candidate_live_mode() -> tuple[str, str]:
    """Return the guarded rollout mode without letting bad config fail open."""

    configured = str(os.environ.get(DAILY_HIGH_CANDIDATE_LIVE_MODE_ENV, DAILY_HIGH_CANDIDATE_DEFAULT_LIVE_MODE) or "").strip().lower()
    if configured in DAILY_HIGH_CANDIDATE_LIVE_MODES:
        return configured, "configured_or_default"
    return "shadow", "invalid_mode_falls_back_to_shadow"


def _candidate_likelihood_map_for_live_use(
    *,
    labels: Sequence[str],
    candidate: Mapping[str, Any] | None,
) -> tuple[dict[str, float] | None, list[str], Mapping[str, Any]]:
    """Validate an immutable candidate before the pipeline may consume its map.

    The candidate builder remains a pure, candidate-only producer.  This
    boundary is deliberately the only place that can elect to consume its
    already-normalized map in the formal posterior path.
    """

    candidate_map = candidate if isinstance(candidate, Mapping) else {}
    contract = build_zuuu_daily_high_candidate_contract(candidate_map)
    issues = [str(value) for value in contract.get("issues") or [] if str(value)]
    likelihood_row = candidate_map.get("metar_strong_likelihood_candidate")
    likelihood_map = likelihood_row.get("likelihood_map") if isinstance(likelihood_row, Mapping) else None
    if not isinstance(likelihood_map, Mapping):
        issues.append("candidate_likelihood_map_missing")
        return None, sorted(set(issues)), contract

    label_set = set(labels)
    if set(str(key) for key in likelihood_map) != label_set:
        issues.append("candidate_likelihood_labels_mismatch")
    values: dict[str, float] = {}
    for label in labels:
        try:
            value = float(likelihood_map.get(label))
        except (TypeError, ValueError):
            issues.append(f"candidate_likelihood_non_numeric:{label}")
            continue
        if not math.isfinite(value) or value < 0.0:
            issues.append(f"candidate_likelihood_invalid_probability:{label}")
            continue
        values[label] = value

    total = sum(values.values())
    if len(values) != len(labels) or abs(total - 1.0) > 1e-6:
        issues.append("candidate_likelihood_not_normalized")

    remaining = candidate_map.get("remaining_daily_high_distribution")
    hard_floor = remaining.get("hard_floor") if isinstance(remaining, Mapping) else None
    if not isinstance(hard_floor, Mapping):
        issues.append("candidate_hard_floor_missing")
    else:
        for label in labels:
            try:
                masked = float(hard_floor.get(label, 1.0)) <= 0.0
            except (TypeError, ValueError):
                issues.append(f"candidate_hard_floor_invalid:{label}")
                continue
            if masked and values.get(label, 0.0) > 1e-12:
                issues.append(f"candidate_hard_floor_violation:{label}")

    if issues:
        return None, sorted(set(issues)), contract
    return values, [], contract


def _candidate_live_rollout_trace(
    *,
    mode: str,
    mode_reason: str,
    state: str,
    reason: str,
    candidate: Mapping[str, Any] | None,
    contract: Mapping[str, Any] | None = None,
    issues: Sequence[str] | None = None,
) -> dict[str, Any]:
    candidate_map = candidate if isinstance(candidate, Mapping) else {}
    return {
        "schema": "zuuu_daily_high_candidate_live_rollout_v1",
        "mode": mode,
        "mode_reason": mode_reason,
        "state": state,
        "reason": reason,
        "applied_to_metar_strong_likelihood": state == "applied",
        "candidate_schema": str(candidate_map.get("schema") or ""),
        "candidate_policy_version": str(candidate_map.get("policy_version") or ""),
        "candidate_only_producer": candidate_map.get("candidate_only") is True,
        "candidate_production_applied_flag": candidate_map.get("production_applied") is True,
        "candidate_source_observation_ids": list(candidate_map.get("source_observation_ids") or []),
        "candidate_contract_ok": bool((contract or {}).get("ok")),
        "candidate_contract_issues": sorted(set(str(value) for value in (issues or []) if str(value))),
        "rollback": {
            "mode_env": DAILY_HIGH_CANDIDATE_LIVE_MODE_ENV,
            "set_to": "shadow",
            "fallback_likelihood": "baseline_metar_strong_likelihood",
        },
        "guards": {
            "candidate_cannot_place_orders": True,
            "candidate_parameter_learning_requires_evidence_maturity": True,
            "hard_mask_applied_by_posterior": True,
            "phase_guard_applied_before_candidate": True,
        },
    }


def _phase_adjusted_metar_strong_output(
    labels: Sequence[str],
    metar_output: Mapping[str, Any],
    phase_output: Mapping[str, Any],
) -> dict[str, Any]:
    adjusted = dict(metar_output)
    phase = str(phase_output.get("phase") or "")
    rollout_mode, rollout_mode_reason = _daily_high_candidate_live_mode()
    candidate = metar_output.get("daily_high_observation_candidate")
    baseline_map = dict(metar_output.get("metar_strong_likelihood_map") or {})
    adjusted["metar_strong_likelihood_baseline_map"] = baseline_map
    hard_constraints = metar_output.get("hard_constraints")
    hard_lock = bool(phase_output.get("hard_lock_active"))
    if isinstance(hard_constraints, Mapping):
        hard_lock = hard_lock or bool(hard_constraints.get("hard_lock"))

    lower_bound_only_phases = {"D0_pre_dawn", "D0_morning"}
    if phase in lower_bound_only_phases and not hard_lock:
        neutral_map = _uniform_likelihood_map(labels)
        adjusted["metar_strong_likelihood_raw_map"] = baseline_map
        adjusted["metar_strong_likelihood_map"] = neutral_map
        adjusted["metar_strong_likelihood"] = [
            {"bucket": label, "metar_strong_likelihood": value} for label, value in neutral_map.items()
        ]
        normalization = dict(metar_output.get("normalization") or {})
        normalization["metar_strong_likelihood_sum"] = 1.0 if labels else 0.0
        adjusted["normalization"] = normalization
        adjusted["phase_adjustment"] = {
            "state": "neutralized",
            "phase": phase,
            "reason": "d0_early_current_metar_is_lower_bound_not_daily_max_upper_bound",
            "raw_map_preserved_field": "metar_strong_likelihood_raw_map",
        }
        adjusted["daily_high_candidate_live_rollout"] = _candidate_live_rollout_trace(
            mode=rollout_mode,
            mode_reason=rollout_mode_reason,
            state="deferred_by_phase_guard",
            reason="d0_early_lower_bound_guard_keeps_formal_strong_likelihood_neutral",
            candidate=candidate if isinstance(candidate, Mapping) else None,
        )
        return adjusted

    adjusted["phase_adjustment"] = {
        "state": "unchanged",
        "phase": phase,
        "reason": "phase_allows_metar_strong_likelihood_or_hard_lock_active",
    }
    if hard_lock:
        adjusted["daily_high_candidate_live_rollout"] = _candidate_live_rollout_trace(
            mode=rollout_mode,
            mode_reason=rollout_mode_reason,
            state="deferred_by_hard_lock",
            reason="official_final_or_hard_lock_keeps_existing_strong_likelihood_before_posterior_hard_mask",
            candidate=candidate if isinstance(candidate, Mapping) else None,
        )
        return adjusted

    if rollout_mode != "live_guarded":
        adjusted["daily_high_candidate_live_rollout"] = _candidate_live_rollout_trace(
            mode=rollout_mode,
            mode_reason=rollout_mode_reason,
            state="shadow_or_baseline_mode",
            reason="candidate_live_rollout_disabled_by_runtime_mode",
            candidate=candidate if isinstance(candidate, Mapping) else None,
        )
        return adjusted

    candidate_map, candidate_issues, candidate_contract = _candidate_likelihood_map_for_live_use(
        labels=labels,
        candidate=candidate if isinstance(candidate, Mapping) else None,
    )
    if candidate_map is None:
        adjusted["daily_high_candidate_live_rollout"] = _candidate_live_rollout_trace(
            mode=rollout_mode,
            mode_reason=rollout_mode_reason,
            state="fallback_to_baseline",
            reason="candidate_contract_or_probability_guard_failed",
            candidate=candidate if isinstance(candidate, Mapping) else None,
            contract=candidate_contract,
            issues=candidate_issues,
        )
        return adjusted

    adjusted["metar_strong_likelihood_map"] = candidate_map
    adjusted["metar_strong_likelihood"] = [
        {"bucket": label, "metar_strong_likelihood": candidate_map[label]} for label in labels
    ]
    normalization = dict(metar_output.get("normalization") or {})
    normalization["metar_strong_likelihood_sum"] = 1.0 if labels else 0.0
    adjusted["normalization"] = normalization
    adjusted["daily_high_candidate_live_rollout"] = _candidate_live_rollout_trace(
        mode=rollout_mode,
        mode_reason=rollout_mode_reason,
        state="applied",
        reason="candidate_contract_valid_phase_allows_remaining_daily_high_likelihood",
        candidate=candidate if isinstance(candidate, Mapping) else None,
        contract=candidate_contract,
    )
    return adjusted


def build_hierarchical_posterior_pipeline(
    *,
    buckets: Sequence[Any],
    prior_distribution: Any,
    event_date: Any,
    as_of: Any = None,
    station_timezone: str = "Asia/Shanghai",
    source_observations: Iterable[Any] | None = None,
    observation_inputs: Mapping[str, Any] | None = None,
    hard_mask: Mapping[str, Any] | None = None,
    hard_mask_signals: Mapping[str, Any] | None = None,
    hard_mask_source_class: str = "hard_observation_candidate",
    rounding_rule: str = "rounded_degrees",
    posterior_quantile_levels: Sequence[Any] | None = None,
    posterior_run_id: str = "",
    fact_snapshot_id: str = "",
    phase_override: str = "",
    beta_override: Mapping[str, Any] | None = None,
    forecast_group_config: Mapping[str, Any] | None = None,
    forecast_weight_artifact: Mapping[str, Any] | None = None,
    forecast_weight_context: Mapping[str, Any] | None = None,
    forecast_curve_state: Mapping[str, Any] | None = None,
    prior_continuous_distribution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    labels = _bucket_labels(buckets)
    observations = list(source_observations or [])
    metar_output = build_metar_strong_likelihood(
        buckets=labels,
        observation_inputs=observation_inputs,
        source_observations=observations,
        as_of=as_of,
        station_timezone=station_timezone,
        rounding_rule=rounding_rule,
        event_date=str(event_date or ""),
        allow_legacy_v31_probability_overlay=False,
    )
    merged_hard_mask_signals = {
        **_hard_mask_signals_from_metar(metar_output),
        **dict(hard_mask_signals or {}),
    }
    phase_output = build_phase_weighting_snapshot(
        event_date=event_date,
        as_of=as_of,
        station_timezone=station_timezone,
        hard_mask_signals=merged_hard_mask_signals,
        phase_override=phase_override,
        beta_override=beta_override,
        include_schedule=True,
    )
    forecast_output = _joint_weak_forecast_output(
        event_date=event_date,
        as_of=as_of,
        source_observations=observations,
    )
    adjusted_metar_output = _phase_adjusted_metar_strong_output(labels, metar_output, phase_output)
    phase_output, likelihood_information_quality = _quality_adjusted_phase_output(
        phase_output,
        forecast_output=forecast_output,
        metar_output=adjusted_metar_output,
        as_of=as_of,
    )
    combined_hard_mask = _combine_hard_masks(labels, _hard_mask_from_metar(adjusted_metar_output), hard_mask)
    source_event_ids = _source_event_ids(forecast_output, adjusted_metar_output)
    source_observation_ids = _source_observation_ids(forecast_output, adjusted_metar_output)
    posterior_output = build_sequential_remaining_warming_posterior(
        buckets=labels,
        prior_distribution=prior_distribution,
        prior_continuous_distribution=prior_continuous_distribution,
        forecast_output=forecast_output,
        metar_output=adjusted_metar_output,
        source_observations=observations,
        hard_mask=combined_hard_mask,
        event_date=str(event_date or ""),
        as_of=as_of,
        station_timezone=station_timezone,
        rounding_rule=rounding_rule,
        phase=str(phase_output.get("phase") or ""),
        phase_reason=str(phase_output.get("phase_reason") or ""),
        hard_mask_reason=str(phase_output.get("hard_mask_reason") or ""),
        beta_weak=phase_output.get("beta_weak"),
        beta_strong=phase_output.get("beta_strong"),
        information_quality=likelihood_information_quality,
        posterior_quantile_levels=posterior_quantile_levels,
        posterior_run_id=posterior_run_id,
        fact_snapshot_id=fact_snapshot_id,
        source_event_ids=source_event_ids,
    )
    weak_layer = (
        posterior_output.get("layer_contributions", {}).get("forecast_weak_likelihood", {})
        if isinstance(posterior_output.get("layer_contributions"), Mapping)
        else {}
    )
    if isinstance(weak_layer, Mapping) and isinstance(weak_layer.get("distribution"), Mapping):
        forecast_output["forecast_weak_likelihood_map"] = dict(weak_layer["distribution"])
        forecast_output["weak_likelihood_consumed"] = True
    na_reasons = _collect_na_reasons(forecast_output, adjusted_metar_output, posterior_output)
    ok = bool(posterior_output.get("ok")) and bool(phase_output.get("ok")) and bool(forecast_output.get("ok")) and bool(adjusted_metar_output.get("ok"))
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "read_only": True,
        "production_entry_enabled": False,
        "posterior_activation_mode": "live_no_shadow",
        "guards": _guards(),
        "event_date": str(event_date or ""),
        "as_of": str(as_of or ""),
        "station_timezone": str(station_timezone or ""),
        "buckets": list(labels),
        "phase": phase_output.get("phase"),
        "beta_weak": phase_output.get("beta_weak"),
        "beta_strong": phase_output.get("beta_strong"),
        "beta_weak_base": phase_output.get("beta_weak_base"),
        "beta_strong_base": phase_output.get("beta_strong_base"),
        "phase_reason": phase_output.get("phase_reason"),
        "hard_mask_reason": posterior_output.get("hard_mask_reason"),
        "posterior_temperature_distribution": posterior_output.get("posterior_temperature_distribution"),
        "posterior_temperature_distribution_map": posterior_output.get("posterior_temperature_distribution_map"),
        "bayes_stage_distributions": posterior_output.get("bayes_stage_distributions"),
        "posterior_continuous_distribution": posterior_output.get("posterior_continuous_distribution"),
        "posterior_bucket_cdf": posterior_output.get("posterior_bucket_cdf"),
        "posterior_quantiles_c": posterior_output.get("posterior_quantiles_c"),
        "posterior_quantile_grid": posterior_output.get("posterior_quantile_grid"),
        "layer_contributions": posterior_output.get("layer_contributions"),
        "posterior_composer_schema": posterior_output.get("schema"),
        "posterior_composer_policy_version": posterior_output.get("policy_version"),
        "sequential_state_filter": posterior_output.get("sequential_state_filter"),
        "canonical_metar_snapshot": posterior_output.get("canonical_metar_snapshot"),
        "predicted_peak_time_prior": posterior_output.get("predicted_peak_time_prior"),
        "predicted_peak_time_posterior": posterior_output.get("predicted_peak_time_posterior"),
        "observed_peak_platform": posterior_output.get("observed_peak_platform"),
        "remaining_warming_mixture": posterior_output.get("remaining_warming_mixture"),
        "termination_risk": adjusted_metar_output.get("termination_risk"),
        "production_model_version": posterior_output.get("production_model_version"),
        "production_model_ready": posterior_output.get("production_model_ready"),
        "production_block_reason": posterior_output.get("production_block_reason"),
        "remaining_warming_source": posterior_output.get("remaining_warming_source"),
        "v31_multimodel_posterior": posterior_output.get("v31_multimodel_posterior"),
        "observation_information_gate": posterior_output.get("observation_information_gate"),
        "forecast_error_components": posterior_output.get("forecast_error_components"),
        "joint_weak_model": {
            "artifact_sha256": forecast_output.get("artifact_sha256"),
            "forecast_evidence_snapshot_id": forecast_output.get("forecast_evidence_snapshot_id"),
            "weak_source_set": forecast_output.get("weak_source_set"),
            "weak_source_surface_selection": forecast_output.get("weak_source_surface_selection"),
            "forecast_multimodality_trace": forecast_output.get("forecast_multimodality_trace"),
        },
        "likelihood_information_quality": likelihood_information_quality,
        "hard_constraints": {
            "combined_hard_mask": combined_hard_mask,
            "posterior": posterior_output.get("hard_constraints"),
            "metar_strong": adjusted_metar_output.get("hard_constraints"),
            "phase_weight": {
                "hard_lock_active": phase_output.get("hard_lock_active"),
                "hard_mask_reason_parts": phase_output.get("hard_mask_reason_parts"),
            },
        },
        "runtime_outputs": {
            "phase_weighting": phase_output,
            "forecast_weak_likelihood": forecast_output,
            "metar_strong_likelihood": adjusted_metar_output,
            "sequential_remaining_warming_bayes": posterior_output,
            "hierarchical_temperature_posterior": posterior_output,
        },
        "source_lineage": {
            "source_event_ids": source_event_ids,
            "source_observation_ids": source_observation_ids,
            "summary": _source_summary(forecast_output, metar_output),
        },
        "na_reasons": na_reasons,
        "normalization": {
            "posterior_sum": (posterior_output.get("normalization") or {}).get("posterior_sum") if isinstance(posterior_output.get("normalization"), Mapping) else None,
            "continuous_mass_sum": (posterior_output.get("normalization") or {}).get("continuous_mass_sum") if isinstance(posterior_output.get("normalization"), Mapping) else None,
            "prior_mass_sum": (posterior_output.get("normalization") or {}).get("prior_mass_sum") if isinstance(posterior_output.get("normalization"), Mapping) else None,
            "forecast_error_likelihood_sum": (posterior_output.get("normalization") or {}).get("forecast_error_likelihood_sum") if isinstance(posterior_output.get("normalization"), Mapping) else None,
            "remaining_warming_mass_sum": (posterior_output.get("normalization") or {}).get("remaining_warming_mass_sum") if isinstance(posterior_output.get("normalization"), Mapping) else None,
            "forecast_weak_likelihood_sum": (forecast_output.get("normalization") or {}).get("forecast_weak_likelihood_sum") if isinstance(forecast_output.get("normalization"), Mapping) else None,
            "metar_strong_likelihood_sum": (adjusted_metar_output.get("normalization") or {}).get("metar_strong_likelihood_sum") if isinstance(adjusted_metar_output.get("normalization"), Mapping) else None,
            "state_prob_sum": (adjusted_metar_output.get("normalization") or {}).get("state_prob_sum") if isinstance(adjusted_metar_output.get("normalization"), Mapping) else None,
        },
        "boundary": {
            "single_read_only_pipeline_entry": True,
            "does_not_write_fact_snapshot": True,
            "does_not_create_auto_trade_decision": True,
            "does_not_place_order": True,
            "old_additive_evidence_pool_not_used": True,
            "legacy_bucket_product_posterior_not_used": True,
            "live_primary_posterior_composer": "joint_weak_physical_strong_sequential_composer_v1",
            "live_primary_model": posterior_output.get("production_model_version"),
            "legacy_model_fallback_allowed": False,
            "legacy_v31_probability_model_consumed": False,
            "unique_joint_weak_model_consumed": True,
            "live_no_shadow": True,
            "shadow_posterior_emitted": False,
            "continuous_before_bucket_integration": True,
            "metar_valid_times_update_sequential_state_once": True,
            "remaining_warming_zero_mass_explicit": True,
            "predicted_peak_time_is_weak_prior_only": True,
            "observed_peak_time_uses_full_canonical_metar_day": True,
            "predicted_peak_window_cannot_finalize_daily_peak": True,
            "daily_high_candidate_live_rollout": (adjusted_metar_output.get("daily_high_candidate_live_rollout") or {}).get("state"),
            "daily_high_candidate_uses_formal_posterior_only": True,
            "daily_high_candidate_never_bypasses_auto_trade_decisions": True,
            "legacy_model_taf_special_case_retired": True,
            "contract_quality_changes_beta_without_reusing_weather_direction": True,
            "hard_mask_not_tempered_by_information_quality": True,
        },
        "daily_high_candidate_live_rollout": adjusted_metar_output.get("daily_high_candidate_live_rollout"),
        "next_boundary": "weather_fact_snapshot_writer_dry_run_or_no_apply",
    }


__all__ = [
    "SCHEMA_VERSION",
    "build_hierarchical_posterior_pipeline",
]
