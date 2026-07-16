"""Event-day-weighted, read-only evaluation for daily-high shadow candidates."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from temperature_posterior_runtime import temperature_bucket_bounds
from zuuu_daily_high_candidate_runtime import build_zuuu_daily_high_candidate


SCHEMA_VERSION = "zuuu_daily_high_shadow_evaluation_v1"
POLICY_VERSION = "zuuu_daily_high_shadow_evaluation_no_fit_v1"
MIN_EVENT_DAYS_FOR_PERFORMANCE_CONCLUSION = 30


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _normalize_map(value: Mapping[str, Any] | None, labels: Sequence[str]) -> dict[str, float] | None:
    source = _mapping(value)
    parsed: dict[str, float] = {}
    for label in labels:
        item = _number(source.get(label))
        if item is None or item < -1e-12:
            return None
        parsed[label] = max(0.0, item)
    total = sum(parsed.values())
    return {label: item / total for label, item in parsed.items()} if total > 0 else None


def _ordered_labels(probabilities: Mapping[str, Any]) -> list[str]:
    rows: list[tuple[float, str]] = []
    for label in probabilities:
        bounds = temperature_bucket_bounds(str(label), rounding_rule="rounded_degrees")
        if bounds is None:
            return []
        rows.append((float(bounds[0]), str(label)))
    return [label for _, label in sorted(rows)]


def _actual_index(actual_c: float, labels: Sequence[str]) -> int | None:
    for index, label in enumerate(labels):
        bounds = temperature_bucket_bounds(label, rounding_rule="rounded_degrees")
        if bounds is None:
            return None
        lower, upper = bounds
        if float(lower) <= actual_c < float(upper):
            return index
    return None


def _metrics(probabilities: Mapping[str, Any], *, labels: Sequence[str], actual_c: float) -> dict[str, Any] | None:
    normalized = _normalize_map(probabilities, labels)
    actual_index = _actual_index(actual_c, labels)
    if normalized is None or actual_index is None:
        return None
    ordered = [normalized[label] for label in labels]
    one_hot = [1.0 if index == actual_index else 0.0 for index in range(len(labels))]
    log_loss = -math.log(max(ordered[actual_index], 1e-12))
    brier = sum((probability - target) ** 2 for probability, target in zip(ordered, one_hot))
    if len(labels) > 1:
        cdf_probability = 0.0
        cdf_target = 0.0
        rps = 0.0
        for probability, target in zip(ordered[:-1], one_hot[:-1]):
            cdf_probability += probability
            cdf_target += target
            rps += (cdf_probability - cdf_target) ** 2
        rps /= len(labels) - 1
    else:
        rps = 0.0
    top_index = max(range(len(labels)), key=lambda index: ordered[index])
    return {
        "labels": list(labels),
        "actual_bucket": labels[actual_index],
        "actual_bucket_probability": ordered[actual_index],
        "rps": rps,
        "bucket_log_loss": log_loss,
        "multi_bucket_brier": brier,
        "map_bucket_hit": top_index == actual_index,
        "within_one_bucket_hit": abs(top_index - actual_index) <= 1,
        "top_bucket": labels[top_index],
    }


def build_shadow_prediction(
    sample: Mapping[str, Any],
    *,
    buckets: Sequence[str],
    baseline_likelihood_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay the candidate at the sample cutoff without applying it."""

    row = _mapping(sample)
    candidate = build_zuuu_daily_high_candidate(
        buckets=list(buckets),
        source_observations=row.get("canonical_metar_sequence") or [],
        as_of=str(row.get("cutoff_as_of") or ""),
        event_date=str(row.get("event_date") or ""),
    )
    candidate_map = _mapping(_mapping(candidate.get("metar_strong_likelihood_candidate")).get("likelihood_map"))
    return {
        "schema": "zuuu_daily_high_shadow_prediction_v1",
        "sample_id": str(row.get("sample_id") or ""),
        "event_date": str(row.get("event_date") or ""),
        "cutoff_as_of": str(row.get("cutoff_as_of") or ""),
        "quality_grade": str(row.get("quality_grade") or ""),
        "evaluation_eligible": row.get("evaluation_eligible") is True,
        "label": dict(_mapping(row.get("label"))),
        "observed_today_max_c_at_cutoff": _number(row.get("observed_today_max_c_at_cutoff")),
        "candidate_likelihood_map": dict(candidate_map),
        "baseline_likelihood_map": dict(_mapping(baseline_likelihood_map)),
        "candidate_physical_state": dict(_mapping(candidate.get("physical_environment_state_posterior"))),
        "candidate_remaining_distribution": dict(_mapping(candidate.get("remaining_daily_high_distribution"))),
        "candidate_only": candidate.get("candidate_only") is True,
        "production_applied": candidate.get("production_applied") is True,
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
            "changes_parameters": False,
        },
    }


def _local_hour(cutoff_as_of: Any) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(cutoff_as_of).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    local = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
    return local.hour + local.minute / 60.0


def _mean(rows: Sequence[float]) -> float | None:
    return sum(rows) / len(rows) if rows else None


def evaluate_shadow_predictions(predictions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Score candidate/baseline per cutoff, then average cutoffs within event day."""

    eligible_rows: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for source in predictions:
        row = _mapping(source)
        if row.get("evaluation_eligible") is not True:
            excluded.append({"sample_id": row.get("sample_id"), "event_date": row.get("event_date"), "reason": "sample_not_a_class"})
            continue
        actual = _number(_mapping(row.get("label")).get("final_daily_max_label"))
        candidate_map = _mapping(row.get("candidate_likelihood_map"))
        labels = _ordered_labels(candidate_map)
        if actual is None:
            excluded.append({"sample_id": row.get("sample_id"), "event_date": row.get("event_date"), "reason": "continuous_final_label_missing"})
            continue
        candidate_metrics = _metrics(candidate_map, labels=labels, actual_c=actual) if labels else None
        if candidate_metrics is None:
            excluded.append({"sample_id": row.get("sample_id"), "event_date": row.get("event_date"), "reason": "candidate_bucket_contract_invalid"})
            continue
        baseline_map = _mapping(row.get("baseline_likelihood_map"))
        baseline_metrics = _metrics(baseline_map, labels=labels, actual_c=actual) if baseline_map else None
        observed_max = _number(row.get("observed_today_max_c_at_cutoff"))
        physical = _mapping(row.get("candidate_physical_state"))
        distribution = _mapping(row.get("candidate_remaining_distribution"))
        positive_tail = _number(_mapping(distribution.get("remaining_increment_positive_quantiles_c")).get("p90"))
        actual_remaining = actual - observed_max if observed_max is not None else None
        local_hour = _local_hour(row.get("cutoff_as_of"))
        recovery = "recovery_signal" in {str(value) for value in physical.get("evidence_codes") or []}
        eligible_rows.append(
            {
                "sample_id": row.get("sample_id"),
                "event_date": row.get("event_date"),
                "cutoff_as_of": row.get("cutoff_as_of"),
                "candidate": candidate_metrics,
                "baseline": baseline_metrics,
                "premature_cutoff": bool(physical.get("physical_cutoff_confirmed")) and actual_remaining is not None and actual_remaining > 0,
                "post_15_tail_case": local_hour is not None and local_hour >= 15.0 and actual_remaining is not None and actual_remaining > 0,
                "post_15_tail_captured": local_hour is not None and local_hour >= 15.0 and actual_remaining is not None and actual_remaining > 0 and positive_tail is not None and positive_tail > 0,
                "recovery_case": recovery and actual_remaining is not None and actual_remaining > 0,
                "recovery_captured": recovery and actual_remaining is not None and actual_remaining > 0 and positive_tail is not None and positive_tail > 0,
            }
        )

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible_rows:
        by_day[str(row.get("event_date") or "")].append(row)
    daily_rows: list[dict[str, Any]] = []
    for event_date, rows in sorted(by_day.items()):
        candidate_rps = _mean([float(row["candidate"]["rps"]) for row in rows])
        candidate_log_loss = _mean([float(row["candidate"]["bucket_log_loss"]) for row in rows])
        candidate_brier = _mean([float(row["candidate"]["multi_bucket_brier"]) for row in rows])
        baseline_rows = [row["baseline"] for row in rows if row.get("baseline")]
        daily_rows.append(
            {
                "event_date": event_date,
                "cutoff_count": len(rows),
                "candidate": {
                    "rps": candidate_rps,
                    "bucket_log_loss": candidate_log_loss,
                    "multi_bucket_brier": candidate_brier,
                    "map_bucket_hit_rate": _mean([1.0 if row["candidate"]["map_bucket_hit"] else 0.0 for row in rows]),
                    "within_one_bucket_hit_rate": _mean([1.0 if row["candidate"]["within_one_bucket_hit"] else 0.0 for row in rows]),
                },
                "baseline": {
                    "available_cutoff_count": len(baseline_rows),
                    "rps": _mean([float(row["rps"]) for row in baseline_rows]),
                    "bucket_log_loss": _mean([float(row["bucket_log_loss"]) for row in baseline_rows]),
                    "multi_bucket_brier": _mean([float(row["multi_bucket_brier"]) for row in baseline_rows]),
                },
                "premature_cutoff_count": sum(1 for row in rows if row["premature_cutoff"]),
                "post_15_tail_case_count": sum(1 for row in rows if row["post_15_tail_case"]),
                "post_15_tail_capture_count": sum(1 for row in rows if row["post_15_tail_captured"]),
                "recovery_case_count": sum(1 for row in rows if row["recovery_case"]),
                "recovery_capture_count": sum(1 for row in rows if row["recovery_captured"]),
            }
        )
    candidate_rps_days = [float(row["candidate"]["rps"]) for row in daily_rows if row["candidate"]["rps"] is not None]
    baseline_rps_pairs = [
        (float(row["candidate"]["rps"]), float(row["baseline"]["rps"]))
        for row in daily_rows
        if row["candidate"]["rps"] is not None and row["baseline"]["rps"] is not None
    ]
    event_day_count = len(daily_rows)
    return {
        "schema": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "daily_rows": daily_rows,
        "cutoff_rows": eligible_rows,
        "excluded": excluded,
        "event_day_count": event_day_count,
        "candidate_cutoff_count": len(eligible_rows),
        "candidate_event_day_mean_rps": _mean(candidate_rps_days),
        "paired_baseline_event_day_count": len(baseline_rps_pairs),
        "paired_candidate_minus_baseline_rps": _mean([candidate - baseline for candidate, baseline in baseline_rps_pairs]),
        "premature_cutoff_count": sum(row["premature_cutoff_count"] for row in daily_rows),
        "post_15_tail_case_count": sum(row["post_15_tail_case_count"] for row in daily_rows),
        "post_15_tail_capture_count": sum(row["post_15_tail_capture_count"] for row in daily_rows),
        "recovery_case_count": sum(row["recovery_case_count"] for row in daily_rows),
        "recovery_capture_count": sum(row["recovery_capture_count"] for row in daily_rows),
        "performance_conclusion_allowed": event_day_count >= MIN_EVENT_DAYS_FOR_PERFORMANCE_CONCLUSION,
        "performance_conclusion_reason": (
            "a_class_event_days_at_or_above_minimum"
            if event_day_count >= MIN_EVENT_DAYS_FOR_PERFORMANCE_CONCLUSION
            else "insufficient_a_class_event_days_no_performance_conclusion"
        ),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
            "changes_parameters": False,
            "historical_backfill": False,
        },
    }


__all__ = [
    "MIN_EVENT_DAYS_FOR_PERFORMANCE_CONCLUSION",
    "POLICY_VERSION",
    "SCHEMA_VERSION",
    "build_shadow_prediction",
    "evaluate_shadow_predictions",
]
