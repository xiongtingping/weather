"""Walk-forward OOS evaluation for the exact-source-set weak model.

This module is offline-only.  Labels are exposed only after a weak likelihood
has been produced and are never passed into the likelihood runtime.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Mapping, Sequence

from joint_forecast_weak_likelihood_runtime import build_joint_forecast_weak_likelihood, canonical_artifact_sha256
from joint_forecast_weak_likelihood_training_runtime import train_joint_weak_source_set_artifact
from joint_weak_bias_calibration_runtime import ALLOWED_EWMA_ALPHAS, DEFAULT_EWMA_ALPHA, SCHEMA_VERSION as BIAS_CALIBRATION_SCHEMA_VERSION
from joint_weak_oos_safety_runtime import build_joint_weak_oos_safety_evidence


SCHEMA_VERSION = "joint_forecast_weak_likelihood_oos_evaluation_v1"
DEFAULT_GRID_C = tuple(-20.0 + index * 0.25 for index in range(321))


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _mean_probability_and_rps(rows: Sequence[Mapping[str, Any]], target_c: float) -> tuple[float, float, float]:
    temperatures = [float(row["temperature_c"]) for row in rows]
    probabilities = [float(row["likelihood"]) for row in rows]
    mean = sum(temperature * probability for temperature, probability in zip(temperatures, probabilities))
    nearest = min(range(len(temperatures)), key=lambda index: abs(temperatures[index] - target_c))
    cumulative = 0.0
    rps_sum = 0.0
    for index, probability in enumerate(probabilities[:-1]):
        cumulative += probability
        observed_cumulative = 1.0 if nearest <= index else 0.0
        rps_sum += (cumulative - observed_cumulative) ** 2
    rps = rps_sum / max(len(probabilities) - 1, 1)
    return mean, max(probabilities[nearest], 1e-300), rps


def _score_bias_alpha(
    *, train_dataset: Mapping[str, Any], oos_samples: Sequence[Mapping[str, Any]],
    minimum_event_days_per_surface: int, minimum_pair_samples: int,
    temperature_grid_c: Sequence[float], bias_ewma_alpha: float,
) -> dict[str, Any]:
    """Fit one immutable alpha candidate then score only strictly later days."""

    training = train_joint_weak_source_set_artifact(
        dataset=train_dataset,
        minimum_event_days_per_surface=minimum_event_days_per_surface,
        minimum_pair_samples=minimum_pair_samples,
        bias_ewma_alpha=bias_ewma_alpha,
    )
    artifact = _mapping(training.get("artifact"))
    if not artifact:
        return {
            "bias_ewma_alpha": bias_ewma_alpha, "state": "blocked",
            "blockers": ["oos_train_artifact_unavailable", *list(training.get("blockers") or [])],
            "training": training, "rows": [], "blocked_oos_counts": {},
        }
    # The validator requires approved shape. This in-memory copy is evaluation
    # only; neither it nor its self-hash is ever persisted or made active.
    evaluation_artifact = dict(artifact)
    evaluation_artifact["approval_state"] = "approved"
    evaluation_artifact["artifact_sha256"] = canonical_artifact_sha256(evaluation_artifact)
    raw_evaluation_artifact = dict(evaluation_artifact)
    raw_evaluation_artifact["bias_correction"] = {
        "schema": BIAS_CALIBRATION_SCHEMA_VERSION,
        "residual_definition": "forecast_peak_c_minus_physical_x_final_c",
        "source_global_states": {}, "season_states": {}, "context_states": {},
    }
    raw_evaluation_artifact["artifact_sha256"] = canonical_artifact_sha256(raw_evaluation_artifact)

    def score(runtime_artifact: Mapping[str, Any], *, score_kind: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
        rows: list[dict[str, Any]] = []
        blocked: dict[str, int] = {}
        for sample in oos_samples:
            value_sample = _mapping(sample)
            runtime = build_joint_forecast_weak_likelihood(
                temperature_grid_c=temperature_grid_c,
                source_revisions=value_sample.get("source_revisions") or [], artifact=runtime_artifact,
                forecast_context={**_mapping(value_sample.get("forecast_context")), "forecast_evidence_snapshot_id": f"oos-{value_sample.get('event_id', '')}"},
                event_date=str(value_sample.get("event_date") or ""), decision_as_of=str(value_sample.get("decision_as_of") or ""),
            )
            if runtime.get("ok") is not True:
                for reason in runtime.get("block_reasons") or ["unknown_runtime_block"]:
                    blocked[str(reason)] = blocked.get(str(reason), 0) + 1
                continue
            target = float(value_sample["physical_x_final_c"])
            likelihood = runtime["joint_forecast_weak_likelihood"]
            predicted, target_probability, rps = _mean_probability_and_rps(likelihood, target)
            rows.append({
                "event_id": value_sample.get("event_id"), "event_date": value_sample.get("event_date"), "decision_as_of": value_sample.get("decision_as_of"),
                "source_set_key": value_sample.get("source_set_key"), "target_physical_x_final_c": target,
                "predicted_peak_c": predicted, "absolute_error_c": abs(predicted - target),
                "negative_log_probability": -math.log(target_probability), "ranked_probability_score": rps,
                "weak_information_confidence": runtime.get("weak_information_confidence"),
                "artifact_sha256": artifact.get("artifact_sha256"), "bias_ewma_alpha": bias_ewma_alpha,
                "score_kind": score_kind,
            })
        return rows, dict(sorted(blocked.items()))

    rows, blocked = score(evaluation_artifact, score_kind="corrected")
    raw_rows, raw_blocked = score(raw_evaluation_artifact, score_kind="zero_bias_raw")
    if not rows:
        return {
            "bias_ewma_alpha": bias_ewma_alpha, "state": "blocked",
            "blockers": ["no_oos_samples_with_supported_exact_source_set"], "training": training,
            "rows": [], "raw_rows": raw_rows, "blocked_oos_counts": blocked, "raw_blocked_oos_counts": raw_blocked,
        }
    mae = sum(float(row["absolute_error_c"]) for row in rows) / len(rows)
    nll = sum(float(row["negative_log_probability"]) for row in rows) / len(rows)
    safety = build_joint_weak_oos_safety_evidence(
        candidate_rows=rows,
        raw_rows=raw_rows,
        training_samples=train_dataset.get("samples") if isinstance(train_dataset.get("samples"), Sequence) else [],
        candidate_artifact_sha256=str(artifact.get("artifact_sha256") or ""),
    )
    return {
        "bias_ewma_alpha": bias_ewma_alpha, "state": "evaluated_shadow_only", "training": training,
        "artifact_sha256": artifact.get("artifact_sha256"), "sample_count": len(rows), "mae_c": mae,
        "mean_negative_log_probability": nll, "rows": rows,
        "raw_rows": raw_rows, "blocked_oos_counts": blocked, "raw_blocked_oos_counts": raw_blocked,
        "oos_safety": safety,
    }


def evaluate_joint_weak_walk_forward(
    *, dataset: Mapping[str, Any], evaluation_start_date: str, minimum_event_days_per_surface: int = 30,
    minimum_pair_samples: int = 20, temperature_grid_c: Sequence[float] = DEFAULT_GRID_C,
    candidate_bias_ewma_alpha: float = DEFAULT_EWMA_ALPHA,
    compared_bias_ewma_alphas: Sequence[float] = ALLOWED_EWMA_ALPHAS,
) -> dict[str, Any]:
    """Train before ``evaluation_start_date`` and score strictly later samples."""

    value = _mapping(dataset)
    split = _date(evaluation_start_date)
    samples = value.get("samples") if isinstance(value.get("samples"), Sequence) else []
    if split is None:
        return {"schema": SCHEMA_VERSION, "state": "blocked", "blockers": ["evaluation_start_date_invalid"], "boundary": {"strict_asof_oos_runtime_gate": False}}
    candidate_alphas = sorted({float(alpha) for alpha in compared_bias_ewma_alphas})
    if candidate_bias_ewma_alpha not in ALLOWED_EWMA_ALPHAS or not candidate_alphas or any(alpha not in ALLOWED_EWMA_ALPHAS for alpha in candidate_alphas):
        return {"schema": SCHEMA_VERSION, "state": "blocked", "blockers": ["bias_ewma_alpha_not_preregistered"], "boundary": {"strict_asof_oos_runtime_gate": False}}
    if candidate_bias_ewma_alpha not in candidate_alphas:
        candidate_alphas.append(candidate_bias_ewma_alpha)
        candidate_alphas.sort()
    train_samples = [dict(row) for row in samples if (_date(_mapping(row).get("event_date")) or date.max) < split]
    oos_samples = [dict(row) for row in samples if (_date(_mapping(row).get("event_date")) or date.min) >= split]
    train_dataset = {"samples": train_samples, "source_catalog": _mapping(value.get("source_catalog")), "manifest": _mapping(value.get("manifest"))}
    candidate_results = [
        _score_bias_alpha(
            train_dataset=train_dataset, oos_samples=oos_samples,
            minimum_event_days_per_surface=minimum_event_days_per_surface,
            minimum_pair_samples=minimum_pair_samples, temperature_grid_c=temperature_grid_c,
            bias_ewma_alpha=alpha,
        )
        for alpha in candidate_alphas
    ]
    primary = next(item for item in candidate_results if item["bias_ewma_alpha"] == candidate_bias_ewma_alpha)
    valid = [item for item in candidate_results if item.get("state") == "evaluated_shadow_only"]
    common_event_ids = {(str(row.get("event_id")), str(row.get("event_date")), str(row.get("decision_as_of"))) for row in valid[0].get("rows") or []} if valid else set()
    comparable = len(valid) == len(candidate_results) and all(
        {(str(row.get("event_id")), str(row.get("event_date")), str(row.get("decision_as_of"))) for row in item.get("rows") or []} == common_event_ids for item in valid
    )
    safety_valid = [item for item in valid if _mapping(item.get("oos_safety")).get("state") == "passed"]
    winner = min(safety_valid, key=lambda item: (float(item["mean_negative_log_probability"]), float(item["mae_c"]), float(item["bias_ewma_alpha"]))) if comparable and safety_valid else None
    comparison = [{
        "bias_ewma_alpha": item["bias_ewma_alpha"], "state": item.get("state"),
        "artifact_sha256": item.get("artifact_sha256"), "sample_count": item.get("sample_count", 0),
        "mae_c": item.get("mae_c"), "mean_negative_log_probability": item.get("mean_negative_log_probability"),
        "blocked_oos_counts": item.get("blocked_oos_counts", {}), "blockers": item.get("blockers", []),
        "oos_safety_state": _mapping(item.get("oos_safety")).get("state"),
    } for item in candidate_results]
    if primary.get("state") != "evaluated_shadow_only":
        return {"schema": SCHEMA_VERSION, "state": "blocked", "blockers": list(primary.get("blockers") or []), "training": primary.get("training"), "bias_alpha_comparison": comparison, "boundary": {"strict_asof_oos_runtime_gate": False}}
    selection = {
        "candidate_bias_ewma_alpha": candidate_bias_ewma_alpha,
        "selection_metric_order": ["mean_negative_log_probability", "mae_c", "lower_alpha_on_exact_tie"],
        "comparison_complete": comparable,
        "selected_bias_ewma_alpha": winner.get("bias_ewma_alpha") if winner else None,
        "selected_artifact_sha256": winner.get("artifact_sha256") if winner else None,
        "candidate_is_selected": bool(winner and winner.get("artifact_sha256") == primary.get("artifact_sha256")),
        "na_reason": None if winner else ("alpha_candidates_not_comparable_on_identical_oos_events" if not comparable else "no_alpha_candidate_passed_raw_baseline_harm_and_tail_protection"),
    }
    return {"schema": SCHEMA_VERSION, "state": "evaluated_shadow_only", "evaluation_start_date": evaluation_start_date, "training": {"artifact_sha256": primary.get("artifact_sha256"), "surface_count": _mapping(primary.get("training")).get("surface_count"), "state": _mapping(primary.get("training")).get("state")}, "sample_count": primary.get("sample_count"), "mae_c": primary.get("mae_c"), "mean_negative_log_probability": primary.get("mean_negative_log_probability"), "rows": primary.get("rows"), "raw_rows": primary.get("raw_rows"), "blocked_oos_counts": primary.get("blocked_oos_counts"), "raw_blocked_oos_counts": primary.get("raw_blocked_oos_counts"), "oos_safety": primary.get("oos_safety"), "bias_alpha_comparison": comparison, "bias_alpha_selection": selection, "boundary": {"physical_x_label_used_only_after_likelihood": True, "evaluation_artifact_memory_only": True, "strict_asof_oos_runtime_gate": False, "writes_posterior": False, "places_orders": False}}


def evaluate_joint_weak_expanding_window(
    *, dataset: Mapping[str, Any], evaluation_start_date: str, minimum_event_days_per_surface: int = 30,
    minimum_pair_samples: int = 20, temperature_grid_c: Sequence[float] = DEFAULT_GRID_C,
    compared_bias_ewma_alphas: Sequence[float] = ALLOWED_EWMA_ALPHAS,
) -> dict[str, Any]:
    """Research-only rolling-origin evidence; never a substitute for one artifact SHA.

    Each fold trains using event days strictly before the scored day.  Every
    fold therefore has its own immutable artifact identity, so this report is
    useful for structural stability but intentionally cannot satisfy the
    promotion packet's single-candidate identity requirement.
    """

    value = _mapping(dataset)
    split = _date(evaluation_start_date)
    samples = [dict(row) for row in value.get("samples") if isinstance(row, Mapping)] if isinstance(value.get("samples"), Sequence) else []
    alphas = sorted({float(alpha) for alpha in compared_bias_ewma_alphas})
    if split is None:
        return {"schema": SCHEMA_VERSION, "state": "blocked", "blockers": ["evaluation_start_date_invalid"], "boundary": {"strict_asof_oos_runtime_gate": False}}
    if not alphas or any(alpha not in ALLOWED_EWMA_ALPHAS for alpha in alphas):
        return {"schema": SCHEMA_VERSION, "state": "blocked", "blockers": ["bias_ewma_alpha_not_preregistered"], "boundary": {"strict_asof_oos_runtime_gate": False}}
    event_days = sorted({_date(row.get("event_date")) for row in samples if _date(row.get("event_date")) is not None})
    origins = [day for day in event_days if day >= split]
    initial_train = [row for row in samples if (_date(row.get("event_date")) or date.max) < split]
    aggregate: dict[float, dict[str, Any]] = {alpha: {"rows": [], "raw_rows": [], "artifact_sha256s": [], "fold_blockers": {}} for alpha in alphas}
    folds: list[dict[str, Any]] = []
    for origin in origins:
        fold_train = [row for row in samples if (_date(row.get("event_date")) or date.max) < origin]
        fold_oos = [row for row in samples if _date(row.get("event_date")) == origin]
        train_dataset = {"samples": fold_train, "source_catalog": _mapping(value.get("source_catalog")), "manifest": _mapping(value.get("manifest"))}
        fold_summary: dict[str, Any] = {"origin_event_date": origin.isoformat(), "training_event_day_count": len({_date(row.get("event_date")) for row in fold_train}), "oos_event_count": len(fold_oos), "candidates": []}
        for alpha in alphas:
            result = _score_bias_alpha(
                train_dataset=train_dataset, oos_samples=fold_oos,
                minimum_event_days_per_surface=minimum_event_days_per_surface,
                minimum_pair_samples=minimum_pair_samples, temperature_grid_c=temperature_grid_c,
                bias_ewma_alpha=alpha,
            )
            fold_summary["candidates"].append({"bias_ewma_alpha": alpha, "state": result.get("state"), "artifact_sha256": result.get("artifact_sha256"), "sample_count": result.get("sample_count", 0), "blockers": result.get("blockers", [])})
            if result.get("state") == "evaluated_shadow_only":
                aggregate[alpha]["rows"].extend(result.get("rows") or [])
                aggregate[alpha]["raw_rows"].extend(result.get("raw_rows") or [])
                aggregate[alpha]["artifact_sha256s"].append(result.get("artifact_sha256"))
            else:
                for blocker in result.get("blockers") or ["unknown_fold_block"]:
                    aggregate[alpha]["fold_blockers"][str(blocker)] = aggregate[alpha]["fold_blockers"].get(str(blocker), 0) + 1
        folds.append(fold_summary)
    candidates: list[dict[str, Any]] = []
    required_folds = len(origins)
    for alpha in alphas:
        rows = aggregate[alpha]["rows"]
        raw_rows = aggregate[alpha]["raw_rows"]
        complete = len(aggregate[alpha]["artifact_sha256s"]) == required_folds and bool(rows)
        safety = build_joint_weak_oos_safety_evidence(
            candidate_rows=rows, raw_rows=raw_rows, training_samples=initial_train,
            candidate_artifact_sha256="|".join(sorted(str(item) for item in aggregate[alpha]["artifact_sha256s"])),
        ) if complete else {"state": "blocked", "blockers": ["one_or_more_expanding_folds_unavailable"], "live_runtime_gate": False}
        mae = sum(float(row["absolute_error_c"]) for row in rows) / len(rows) if rows else None
        nll = sum(float(row["negative_log_probability"]) for row in rows) / len(rows) if rows else None
        candidates.append({"bias_ewma_alpha": alpha, "state": "evaluated_shadow_only" if complete else "blocked", "sample_count": len(rows), "mae_c": mae, "mean_negative_log_probability": nll, "fold_count": len(aggregate[alpha]["artifact_sha256s"]), "artifact_sha256s": sorted(set(str(item) for item in aggregate[alpha]["artifact_sha256s"])), "fold_blockers": dict(sorted(aggregate[alpha]["fold_blockers"].items())), "oos_safety": safety})
    safe = [item for item in candidates if item["state"] == "evaluated_shadow_only" and _mapping(item.get("oos_safety")).get("state") == "passed"]
    winner = min(safe, key=lambda item: (float(item["mean_negative_log_probability"]), float(item["mae_c"]), float(item["bias_ewma_alpha"]))) if safe else None
    return {"schema": SCHEMA_VERSION, "state": "evaluated_research_only" if candidates else "blocked", "evaluation_mode": "expanding_window_rolling_origin", "evaluation_start_date": evaluation_start_date, "fold_count": len(folds), "folds": folds, "candidates": candidates, "bias_alpha_selection": {"selection_metric_order": ["mean_negative_log_probability", "mae_c", "lower_alpha_on_exact_tie"], "selected_bias_ewma_alpha": winner.get("bias_ewma_alpha") if winner else None, "selected_fold_artifact_sha256s": winner.get("artifact_sha256s") if winner else [], "na_reason": None if winner else "no_candidate_completed_all_folds_and_passed_raw_baseline_safety"}, "promotion_eligible": False, "promotion_na_reason": "expanding_window_uses_multiple_fold_artifacts_not_one_frozen_candidate_sha", "boundary": {"strict_time_order_per_fold": True, "physical_x_label_used_only_after_likelihood": True, "writes_runtime_artifact_pointer": False, "strict_asof_oos_runtime_gate": False, "writes_posterior": False, "places_orders": False}}


__all__ = ["DEFAULT_GRID_C", "SCHEMA_VERSION", "evaluate_joint_weak_expanding_window", "evaluate_joint_weak_walk_forward"]
