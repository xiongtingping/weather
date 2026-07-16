"""Offline safety evidence for weak-likelihood bias candidates.

All statistics are paired against a zero-bias raw baseline and resampled by
event day.  This module is deliberately unable to approve artifacts, write a
pointer, consume market settlement, or affect a live update.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "joint_weak_oos_safety_v1"
BOOTSTRAP_REPLICATES = 1_000
CONFIDENCE_LEVEL = 0.95
MAX_MAE_HARM_C = 0.10
MAX_NLL_HARM = 0.05
MAX_RPS_HARM = 0.02
TAIL_QUANTILES = (0.10, 0.90)
MINIMUM_TAIL_EVENT_DAYS = 5


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (_text(row.get("event_id")), _text(row.get("event_date")), _text(row.get("decision_as_of")))


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * probability)))
    return ordered[index]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _bootstrap_ci_by_event_day(
    *, paired_rows: Sequence[Mapping[str, Any]], field: str, replicates: int,
    seed_material: str,
) -> dict[str, Any]:
    blocks: dict[str, list[float]] = defaultdict(list)
    for row in paired_rows:
        candidate = _finite(row.get(f"candidate_{field}"))
        baseline = _finite(row.get(f"raw_{field}"))
        day = _text(row.get("event_date"))
        if candidate is None or baseline is None or not day:
            continue
        blocks[day].append(candidate - baseline)
    days = sorted(blocks)
    if not days:
        return {"ok": False, "reason": "paired_metric_or_event_day_missing"}
    observed = _mean([value for values in blocks.values() for value in values])
    seed = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    draws: list[float] = []
    for _index in range(replicates):
        sampled = [rng.choice(days) for _item in days]
        values = [value for day in sampled for value in blocks[day]]
        draws.append(_mean(values))
    draws.sort()
    lower = _quantile(draws, (1.0 - CONFIDENCE_LEVEL) / 2.0)
    upper = _quantile(draws, 1.0 - (1.0 - CONFIDENCE_LEVEL) / 2.0)
    return {
        "ok": True, "event_day_count": len(days), "sample_count": sum(len(values) for values in blocks.values()),
        "observed_delta_candidate_minus_raw": observed, "confidence_level": CONFIDENCE_LEVEL,
        "bootstrap_replicates": replicates, "ci_lower": lower, "ci_upper": upper,
    }


def _paired_rows(candidate_rows: Sequence[Mapping[str, Any]], raw_rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    candidate = {_identity(row): row for row in candidate_rows if _identity(row) != ("", "", "")}
    raw = {_identity(row): row for row in raw_rows if _identity(row) != ("", "", "")}
    if set(candidate) != set(raw):
        return [], ["raw_and_corrected_oos_identity_sets_differ"]
    rows: list[dict[str, Any]] = []
    for key in sorted(candidate):
        corrected, baseline = candidate[key], raw[key]
        target = _finite(corrected.get("target_physical_x_final_c"))
        if target is None or _finite(baseline.get("target_physical_x_final_c")) != target:
            return [], ["raw_and_corrected_oos_target_mismatch"]
        rows.append({
            "event_id": key[0], "event_date": key[1], "decision_as_of": key[2], "target_physical_x_final_c": target,
            "candidate_absolute_error_c": corrected.get("absolute_error_c"), "raw_absolute_error_c": baseline.get("absolute_error_c"),
            "candidate_negative_log_probability": corrected.get("negative_log_probability"), "raw_negative_log_probability": baseline.get("negative_log_probability"),
            "candidate_ranked_probability_score": corrected.get("ranked_probability_score"), "raw_ranked_probability_score": baseline.get("ranked_probability_score"),
        })
    return rows, []


def _metric_gate(evidence: Mapping[str, Any], *, max_harm: float) -> bool:
    upper = _finite(evidence.get("ci_upper"))
    return evidence.get("ok") is True and upper is not None and upper <= max_harm


def build_joint_weak_oos_safety_evidence(
    *, candidate_rows: Sequence[Mapping[str, Any]], raw_rows: Sequence[Mapping[str, Any]],
    training_samples: Sequence[Mapping[str, Any]], candidate_artifact_sha256: str,
    bootstrap_replicates: int = BOOTSTRAP_REPLICATES,
) -> dict[str, Any]:
    """Return promotion evidence; insufficient tails are a candidate blocker, not live gate."""

    if int(bootstrap_replicates) < 100:
        return {"schema": SCHEMA_VERSION, "state": "blocked", "blockers": ["bootstrap_replicates_below_minimum"], "live_runtime_gate": False}
    paired, blockers = _paired_rows(candidate_rows, raw_rows)
    if blockers:
        return {"schema": SCHEMA_VERSION, "state": "blocked", "blockers": blockers, "live_runtime_gate": False}
    train_targets = [
        target for sample in training_samples if isinstance(sample, Mapping)
        for target in [_finite(sample.get("physical_x_final_c"))] if target is not None
    ]
    low_threshold = _quantile(train_targets, TAIL_QUANTILES[0])
    high_threshold = _quantile(train_targets, TAIL_QUANTILES[1])
    if low_threshold is None or high_threshold is None:
        return {"schema": SCHEMA_VERSION, "state": "blocked", "blockers": ["training_physical_x_tail_thresholds_unavailable"], "live_runtime_gate": False}
    seed_base = f"{candidate_artifact_sha256}|{len(paired)}"
    overall = {
        "mae": _bootstrap_ci_by_event_day(paired_rows=paired, field="absolute_error_c", replicates=int(bootstrap_replicates), seed_material=f"{seed_base}|mae"),
        "nll": _bootstrap_ci_by_event_day(paired_rows=paired, field="negative_log_probability", replicates=int(bootstrap_replicates), seed_material=f"{seed_base}|nll"),
        "rps": _bootstrap_ci_by_event_day(paired_rows=paired, field="ranked_probability_score", replicates=int(bootstrap_replicates), seed_material=f"{seed_base}|rps"),
    }
    tails: dict[str, Any] = {}
    for name, tail_rows in (
        ("low_physical_x", [row for row in paired if float(row["target_physical_x_final_c"]) <= low_threshold]),
        ("high_physical_x", [row for row in paired if float(row["target_physical_x_final_c"]) >= high_threshold]),
    ):
        day_count = len({row["event_date"] for row in tail_rows})
        if day_count < MINIMUM_TAIL_EVENT_DAYS:
            tails[name] = {"state": "insufficient_event_days", "event_day_count": day_count, "minimum_event_days": MINIMUM_TAIL_EVENT_DAYS}
            continue
        mae = _bootstrap_ci_by_event_day(paired_rows=tail_rows, field="absolute_error_c", replicates=int(bootstrap_replicates), seed_material=f"{seed_base}|{name}|mae")
        nll = _bootstrap_ci_by_event_day(paired_rows=tail_rows, field="negative_log_probability", replicates=int(bootstrap_replicates), seed_material=f"{seed_base}|{name}|nll")
        rps = _bootstrap_ci_by_event_day(paired_rows=tail_rows, field="ranked_probability_score", replicates=int(bootstrap_replicates), seed_material=f"{seed_base}|{name}|rps")
        tails[name] = {"state": "passed" if _metric_gate(mae, max_harm=MAX_MAE_HARM_C) and _metric_gate(nll, max_harm=MAX_NLL_HARM) and _metric_gate(rps, max_harm=MAX_RPS_HARM) else "harm_tolerance_failed", "event_day_count": day_count, "mae": mae, "nll": nll, "rps": rps}
    overall_pass = _metric_gate(overall["mae"], max_harm=MAX_MAE_HARM_C) and _metric_gate(overall["nll"], max_harm=MAX_NLL_HARM) and _metric_gate(overall["rps"], max_harm=MAX_RPS_HARM)
    tails_pass = all(item.get("state") == "passed" for item in tails.values())
    return {
        "schema": SCHEMA_VERSION, "state": "passed" if overall_pass and tails_pass else "blocked",
        "blockers": ([] if overall_pass else ["overall_raw_vs_corrected_harm_tolerance_failed"]) + (["physical_x_tail_protection_failed_or_insufficient"] if not tails_pass else []),
        "baseline": "zero_bias_raw_forecast_same_artifact_surface", "paired_event_day_bootstrap": True,
        "harm_tolerance": {"mae_c": MAX_MAE_HARM_C, "nll": MAX_NLL_HARM, "rps": MAX_RPS_HARM, "confidence_level": CONFIDENCE_LEVEL, "bootstrap_replicates": int(bootstrap_replicates)},
        "overall": overall, "physical_x_tail_thresholds_c": {"low_q10": low_threshold, "high_q90": high_threshold},
        "tails": tails, "paired_sample_count": len(paired), "live_runtime_gate": False,
    }


__all__ = ["BOOTSTRAP_REPLICATES", "SCHEMA_VERSION", "build_joint_weak_oos_safety_evidence"]
