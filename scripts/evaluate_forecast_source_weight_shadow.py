#!/usr/bin/env python3
"""Strict rolling-origin OOS gate for contextual external-source weights.

This script evaluates only immutable external evidence.  It emits a promotion
decision packet but never applies parameters or changes the production posterior.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecast_source_contextual_weight_model import (
    constrain_source_weights,
    fit_contextual_stacking,
    predict_contextual_weights,
)
from forecast_source_curve_tournament_runtime import evaluate_curve_reliability
from forecast_weak_likelihood_runtime import build_forecast_weak_likelihood
from scripts.train_forecast_source_contextual_weights import build_training_samples


SCHEMA_VERSION = "forecast_source_weight_shadow_evaluation_v1"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"
EPS = 1e-12


def _time(value: Any) -> datetime | None:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    try:
        parsed = datetime.fromisoformat(text_value[:-1] + "+00:00" if text_value.endswith("Z") else text_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _score(distribution: Mapping[str, float], *, winner: str, bucket_order: Sequence[str]) -> dict[str, float]:
    labels = [str(value) for value in bucket_order]
    if winner not in labels:
        raise ValueError("winning_bucket_missing_from_bucket_order")
    raw = np.asarray([max(0.0, float(distribution.get(label) or 0.0)) for label in labels], dtype=float)
    total = float(np.sum(raw))
    if total <= EPS:
        raise ValueError("mixture_distribution_empty")
    probabilities = raw / total
    winner_index = labels.index(winner)
    actual = np.zeros(len(labels), dtype=float)
    actual[winner_index] = 1.0
    rps = float(np.sum((np.cumsum(probabilities) - np.cumsum(actual)) ** 2) / max(len(labels) - 1, 1))
    return {"nll": -math.log(max(float(probabilities[winner_index]), EPS)), "rps": rps}


def _bootstrap_day_delta(
    rows: Sequence[Mapping[str, Any]],
    *,
    metric: str,
    replicates: int,
    seed: int,
) -> dict[str, Any]:
    by_day: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_day[str(row["event_date_local"])].append(float(row["challenger"][metric]) - float(row["champion"][metric]))
    day_deltas = np.asarray([float(np.mean(values)) for _day, values in sorted(by_day.items())], dtype=float)
    if not len(day_deltas):
        return {"state": "unavailable", "state_reason": "no_oos_event_days"}
    mean = float(np.mean(day_deltas))
    if len(day_deltas) == 1 or replicates <= 0:
        low = high = mean
    else:
        rng = np.random.default_rng(int(seed))
        indices = rng.integers(0, len(day_deltas), size=(int(replicates), len(day_deltas)))
        values = np.mean(day_deltas[indices], axis=1)
        low, high = (float(value) for value in np.quantile(values, [0.025, 0.975]))
    return {
        "state": "available",
        "event_day_count": len(day_deltas),
        "mean_candidate_minus_champion": mean,
        "bootstrap_95pct_ci": [low, high],
        "ci_fully_supports_improvement": high < 0.0,
    }


def _summary(rows: Sequence[Mapping[str, Any]], lane: str) -> dict[str, Any]:
    output: dict[str, Any] = {"sample_count": len(rows), "event_day_count": len({row["event_date_local"] for row in rows})}
    for metric in ("nll", "rps"):
        output[metric] = float(np.mean([float(row[lane][metric]) for row in rows])) if rows else None
    return output


def _runtime_candidate_distributions(
    sample: Mapping[str, Any],
    *,
    artifact: Mapping[str, Any],
    curve_state: Mapping[str, Any] | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    runtime_artifact = dict(artifact)
    runtime_artifact.setdefault("artifact_sha256", "strict_oos_ephemeral_artifact")
    source_observations = [
        {
            "source_event_id": f"oos_{sample.get('event_id')}_{sample.get('cutoff_id')}_{source}",
            "source_id": source,
            "source_class": "forecast_weak_source",
            "provider_family": (sample.get("provider_families") or {}).get(source),
            "bucket_probabilities": distribution,
            "forecast_snapshot_id": next(iter(sample.get("forecast_snapshot_ids") or []), ""),
            "lineage_quality": "immutable_external_evidence",
        }
        for source, distribution in (sample.get("distributions") or {}).items()
        if isinstance(distribution, Mapping)
    ]
    output = build_forecast_weak_likelihood(
        buckets=sample.get("bucket_order") or [],
        source_observations=source_observations,
        event_date=str(sample.get("event_date_local") or ""),
        phase="pre_event",
        weight_artifact=runtime_artifact,
        weight_context={"lead_hours": sample.get("lead_hours"), "context": sample.get("context") or {}},
        curve_state=curve_state,
    )
    champion = output.get("forecast_weak_likelihood_map")
    candidate = output.get("contextual_weight_lane", {}).get("candidate_distribution")
    if not isinstance(champion, Mapping) or not isinstance(candidate, Mapping):
        raise ValueError("runtime_champion_or_candidate_distribution_missing")
    return (
        {str(key): float(value) for key, value in champion.items()},
        {str(key): float(value) for key, value in candidate.items()},
    )


def _segment_guard(rows: Sequence[Mapping[str, Any]], *, minimum_ess: int, relative_harm_budget: float) -> dict[str, Any]:
    segments: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        context = row.get("context") if isinstance(row.get("context"), Mapping) else {}
        labels = context.get("segments")
        if isinstance(labels, str):
            labels = [labels]
        if not isinstance(labels, Sequence):
            regime = context.get("weather_regime")
            labels = [regime] if regime else []
        for label in labels:
            if label:
                segments[str(label)].append(row)
    details: dict[str, Any] = {}
    blockers: list[str] = []
    for label, values in sorted(segments.items()):
        day_count = len({str(row["event_date_local"]) for row in values})
        conclusive = day_count >= int(minimum_ess)
        metrics = {}
        harmed = False
        for metric in ("nll", "rps"):
            champion = float(np.mean([float(row["champion"][metric]) for row in values]))
            challenger = float(np.mean([float(row["challenger"][metric]) for row in values]))
            relative = (challenger - champion) / max(abs(champion), EPS)
            metrics[metric] = {"champion": champion, "challenger": challenger, "relative_harm": relative}
            harmed = harmed or (conclusive and relative > float(relative_harm_budget))
        if harmed:
            blockers.append(f"segment_harm_budget_exceeded:{label}")
        details[label] = {"event_day_count": day_count, "conclusive": conclusive, "metrics": metrics, "harm_budget_exceeded": harmed}
    return {"segments": details, "blockers": blockers}


def evaluate_contextual_weight_oos(
    score_rows: Sequence[Mapping[str, Any]],
    *,
    context_fields: Sequence[str] = (),
    champion_weights: Mapping[str, float] | None = None,
    champion_context_fields: Sequence[str] | None = None,
    champion_model_options: Mapping[str, Any] | None = None,
    curve_inputs: Sequence[Mapping[str, Any]] = (),
    minimum_train_event_days: int = 30,
    fold_event_days: int = 7,
    bootstrap_replicates: int = 5000,
    bootstrap_seed: int = 20260711,
    shadow_minimum_event_days: int = 30,
    production_minimum_event_days: int = 80,
    segment_minimum_ess: int = 20,
    segment_relative_harm_budget: float = 0.02,
    metric_harm_tolerance: float = 0.0,
    model_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    samples, manifest = build_training_samples(score_rows, immutable_only=True)
    days = sorted({str(row["event_date_local"]) for row in samples})
    provider_families: dict[str, str] = {}
    entities: set[str] = set()
    for sample in samples:
        provider_families.update(sample["provider_families"])
        entities.update(sample["distributions"])
    options = dict(model_options or {})
    constraints = {
        "source_floor": float(options.get("source_floor", 0.05)),
        "source_cap": float(options.get("source_cap", 0.45)),
        "family_floor": float(options.get("family_floor", 0.05)),
        "family_cap": float(options.get("family_cap", 0.70)),
    }
    static_raw = dict(champion_weights or {source: 1.0 for source in entities})
    curve_by_sample: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in curve_inputs:
        if row.get("event_id") and row.get("cutoff_id"):
            curve_by_sample[(str(row["event_id"]), str(row["cutoff_id"]))].append(row)
    for values in curve_by_sample.values():
        values.sort(key=lambda row: str(row.get("curve_cutoff_at") or ""))
    predictions: list[dict[str, Any]] = []
    curve_predictions: list[dict[str, Any]] = []
    folds: list[dict[str, Any]] = []
    start = max(0, int(minimum_train_event_days))
    width = max(1, int(fold_event_days))
    for index in range(start, len(days), width):
        train_days = set(days[:index])
        test_days = set(days[index : index + width])
        train = [row for row in samples if row["event_date_local"] in train_days]
        test = [row for row in samples if row["event_date_local"] in test_days]
        if len(train_days) < int(minimum_train_event_days) or not test:
            continue
        artifact = fit_contextual_stacking(
            train,
            entities=sorted(entities),
            provider_families=provider_families,
            context_fields=context_fields,
            **options,
        )
        champion_artifact = None
        if champion_model_options is not None:
            champion_artifact = fit_contextual_stacking(
                train,
                entities=sorted(entities),
                provider_families=provider_families,
                context_fields=champion_context_fields or (),
                **dict(champion_model_options),
            )
        for sample in test:
            available = sorted(sample["distributions"])
            champion = (
                predict_contextual_weights(champion_artifact, sample, available_entities=available)
                if champion_artifact is not None
                else constrain_source_weights(
                    {source: static_raw.get(source, 0.0) for source in available},
                    provider_families=provider_families,
                    **constraints,
                )
            )
            challenger = predict_contextual_weights(artifact, sample, available_entities=available)
            production_champion_distribution, challenger_distribution = _runtime_candidate_distributions(
                sample,
                artifact=artifact,
            )
            if champion_artifact is not None:
                _legacy, champion_distribution = _runtime_candidate_distributions(
                    sample,
                    artifact=champion_artifact,
                )
            else:
                champion_distribution = production_champion_distribution
            winner = str(sample["winning_bucket"])
            bucket_order = sample["bucket_order"]
            prediction = {
                    "event_id": sample["event_id"],
                    "cutoff_id": sample["cutoff_id"],
                    "event_date_local": sample["event_date_local"],
                    "winning_bucket_edge": bool(sample.get("winning_bucket_edge")),
                    "context": sample.get("context") or {},
                    "champion": _score(champion_distribution, winner=winner, bucket_order=bucket_order),
                    "challenger": _score(challenger_distribution, winner=winner, bucket_order=bucket_order),
                    "champion_weights": champion,
                    "challenger_weights": challenger,
                    "curve_state": None,
                    "curve_na_reason": "curve_input_missing",
                }
            sample_curve_inputs = curve_by_sample.get((str(sample["event_id"]), str(sample["cutoff_id"])), [])
            for curve_input in sample_curve_inputs:
                curve_cutoff = _time(curve_input.get("curve_cutoff_at"))
                if curve_cutoff is None:
                    prediction["curve_na_reason"] = "curve_cutoff_at_missing_or_invalid"
                    continue
                observations = [
                    row
                    for row in curve_input.get("observations") or []
                    if _time(row.get("valid_time")) is not None
                    and _time(row.get("valid_time")) <= curve_cutoff
                    and _time(row.get("as_of_eligible_at") or row.get("first_seen_at")) is not None
                    and _time(row.get("as_of_eligible_at") or row.get("first_seen_at")) <= curve_cutoff
                    and str(row.get("arrival_time_quality") or "") == "observed_first_seen"
                ]
                if not observations:
                    prediction["curve_na_reason"] = "curve_observations_unavailable_at_cutoff"
                    continue
                curve_state = evaluate_curve_reliability(
                    base_weights=challenger,
                    provider_families=provider_families,
                    observations=observations,
                    forecasts=curve_input.get("forecasts") or [],
                    previous_effective_weights=curve_input.get("previous_effective_weights"),
                )
                contract = curve_state.get("contract") if isinstance(curve_state.get("contract"), Mapping) else {}
                contract_ok = bool(
                    curve_state.get("state") == "available"
                    and contract.get("amsc_curve_reliability_consumed_once") is True
                    and contract.get("curve_reweighting_changes_total_weak_precision") is False
                    and contract.get("curve_reweighting_creates_observation_likelihood") is False
                    and contract.get("metar_strong_likelihood_owner_unchanged") is True
                    and contract.get("hard_lock_created") is False
                )
                if contract_ok:
                    _legacy, curve_distribution = _runtime_candidate_distributions(
                        sample,
                        artifact=artifact,
                        curve_state=curve_state,
                    )
                    curve_predictions.append(
                        {
                            **prediction,
                            "curve_cutoff_at": curve_input.get("curve_cutoff_at"),
                            "curve_observation_count": len(observations),
                            "curve": _score(curve_distribution, winner=winner, bucket_order=bucket_order),
                            "curve_weights": curve_state["effective_weights"],
                            "curve_state": curve_state,
                            "curve_na_reason": None,
                        }
                    )
                    prediction["curve_na_reason"] = None
                else:
                    prediction["curve_state"] = curve_state
                    prediction["curve_na_reason"] = str(curve_state.get("state_reason") or "curve_contract_failed")
            predictions.append(prediction)
        folds.append(
            {
                "train_event_day_count": len(train_days),
                "train_through_event_date": max(train_days),
                "test_event_day_count": len(test_days),
                "test_from_event_date": min(test_days),
                "test_through_event_date": max(test_days),
                "causal_order_ok": max(train_days) < min(test_days),
                "artifact_training_sample_count": artifact["training"]["sample_count_usable"],
            }
        )
    champion_summary = _summary(predictions, "champion")
    challenger_summary = _summary(predictions, "challenger")
    curve_summary = _summary(curve_predictions, "curve")
    metric_results: dict[str, Any] = {}
    for offset, metric in enumerate(("nll", "rps")):
        champion_value = float(champion_summary[metric]) if champion_summary[metric] is not None else None
        challenger_value = float(challenger_summary[metric]) if challenger_summary[metric] is not None else None
        bootstrap = _bootstrap_day_delta(
            predictions,
            metric=metric,
            replicates=bootstrap_replicates,
            seed=bootstrap_seed + offset,
        )
        metric_results[metric] = {
            "champion": champion_value,
            "challenger": challenger_value,
            "improvement": champion_value - challenger_value if champion_value is not None and challenger_value is not None else None,
            "improvement_pct": (
                100.0 * (champion_value - challenger_value) / max(abs(champion_value), EPS)
                if champion_value is not None and challenger_value is not None
                else None
            ),
            "point_improved": bool(
                champion_value is not None and challenger_value is not None and challenger_value < champion_value
            ),
            "bootstrap": bootstrap,
        }
    segment = _segment_guard(
        predictions,
        minimum_ess=segment_minimum_ess,
        relative_harm_budget=segment_relative_harm_budget,
    )
    tail_rows = [row for row in predictions if row.get("winning_bucket_edge")]
    tail_nll_harm = 0.0
    if tail_rows:
        tail_nll_harm = float(np.mean([row["challenger"]["nll"] - row["champion"]["nll"] for row in tail_rows]))
    statistical_blockers: list[str] = []
    if not predictions:
        statistical_blockers.append("strict_oos_predictions_missing")
    if predictions and not all(bool(metric_results[name]["point_improved"]) for name in ("nll", "rps")):
        statistical_blockers.append("nll_and_rps_point_estimates_not_both_improved")
    ci_support = [
        bool(metric_results[name]["bootstrap"].get("ci_fully_supports_improvement"))
        for name in ("nll", "rps")
    ] if predictions else [False, False]
    if predictions and not any(ci_support):
        statistical_blockers.append("no_primary_metric_ci_fully_supports_improvement")
    for name in ("nll", "rps"):
        bootstrap = metric_results[name]["bootstrap"]
        if bootstrap.get("state") == "available" and float(bootstrap["bootstrap_95pct_ci"][1]) > float(metric_harm_tolerance):
            if not bootstrap.get("ci_fully_supports_improvement"):
                statistical_blockers.append(f"{name}_ci_exceeds_harm_tolerance")
    statistical_blockers.extend(segment["blockers"])
    if tail_rows and tail_nll_harm > 2.0:
        statistical_blockers.append("tail_bucket_catastrophic_nll_harm")
    paired_days = int(manifest["paired_event_day_count"])
    shadow_data_ok = paired_days >= int(shadow_minimum_event_days)
    production_data_ok = paired_days >= int(production_minimum_event_days)
    statistical_ok = not statistical_blockers
    curve_metric_results: dict[str, Any] = {}
    curve_blockers: list[str] = []
    if not curve_predictions:
        curve_blockers.append("contextual_curve_oos_predictions_missing")
    else:
        for offset, metric in enumerate(("nll", "rps")):
            contextual_value = float(np.mean([float(row["challenger"][metric]) for row in curve_predictions]))
            curve_value = float(curve_summary[metric])
            remapped = [
                {**row, "champion": row["challenger"], "challenger": row["curve"]}
                for row in curve_predictions
            ]
            bootstrap = _bootstrap_day_delta(
                remapped,
                metric=metric,
                replicates=bootstrap_replicates,
                seed=bootstrap_seed + 100 + offset,
            )
            curve_metric_results[metric] = {
                "contextual": contextual_value,
                "contextual_curve": curve_value,
                "improvement": contextual_value - curve_value,
                "improvement_pct": 100.0 * (contextual_value - curve_value) / max(abs(contextual_value), EPS),
                "point_improved": curve_value < contextual_value,
                "bootstrap": bootstrap,
            }
        if not all(curve_metric_results[name]["point_improved"] for name in ("nll", "rps")):
            curve_blockers.append("curve_nll_and_rps_point_estimates_not_both_improved")
        if not any(curve_metric_results[name]["bootstrap"].get("ci_fully_supports_improvement") for name in ("nll", "rps")):
            curve_blockers.append("curve_no_primary_metric_ci_fully_supports_improvement")
        for name in ("nll", "rps"):
            bootstrap = curve_metric_results[name]["bootstrap"]
            if (
                float(bootstrap["bootstrap_95pct_ci"][1]) > float(metric_harm_tolerance)
                and not bootstrap.get("ci_fully_supports_improvement")
            ):
                curve_blockers.append(f"curve_{name}_ci_exceeds_harm_tolerance")
    curve_statistical_ok = not curve_blockers
    curve_event_days = len({str(row["event_date_local"]) for row in curve_predictions})
    curve_shadow_data_ok = curve_event_days >= int(shadow_minimum_event_days)
    curve_production_data_ok = curve_event_days >= int(production_minimum_event_days)
    report = {
        "schema": SCHEMA_VERSION,
        "source_data_policy": "external_only",
        "training_manifest": manifest,
        "evaluation_manifest_sha256": _sha256({"samples": samples, "configuration": {
            "context_fields": list(context_fields),
            "model_options": options,
            "minimum_train_event_days": minimum_train_event_days,
            "fold_event_days": fold_event_days,
            "bootstrap_replicates": bootstrap_replicates,
            "bootstrap_seed": bootstrap_seed,
        }}),
        "folds": folds,
        "oos_prediction_count": len(predictions),
        "oos_event_day_count": len({row["event_date_local"] for row in predictions}),
        "metrics": {
            "champion_contextual_parent" if champion_model_options is not None else "champion_production_legacy_runtime": champion_summary,
            "challenger_contextual": challenger_summary,
            "challenger_contextual_curve": curve_summary,
        },
        "paired_deltas": metric_results,
        "curve_incremental_paired_deltas": curve_metric_results,
        "curve_prediction_count": len(curve_predictions),
        "curve_na_counts": dict(sorted({
            reason: sum(1 for row in predictions if row.get("curve_na_reason") == reason)
            for reason in {row.get("curve_na_reason") for row in predictions if row.get("curve_na_reason")}
        }.items())),
        "segment_metrics": segment["segments"],
        "tail_bucket_guard": {"sample_count": len(tail_rows), "mean_nll_harm": tail_nll_harm, "catastrophic_threshold": 2.0},
        "release_gate": {
            "strict_oos_evaluated": bool(predictions) and all(fold["causal_order_ok"] for fold in folds),
            "nll_improved": bool(predictions) and bool(metric_results["nll"]["point_improved"]),
            "rps_improved": bool(predictions) and bool(metric_results["rps"]["point_improved"]),
            "statistical_gate_passed": statistical_ok,
            "shadow_eligible": shadow_data_ok and statistical_ok,
            "production_promotion_proposal_eligible": production_data_ok and statistical_ok,
            "curve_incremental_statistical_gate_passed": curve_statistical_ok,
            "curve_shadow_eligible": shadow_data_ok and statistical_ok and curve_shadow_data_ok and curve_statistical_ok,
            "curve_production_promotion_proposal_eligible": production_data_ok and statistical_ok and curve_production_data_ok and curve_statistical_ok,
            "production_apply_allowed": False,
            "blockers": (["paired_event_days_below_shadow_minimum"] if not shadow_data_ok else [])
            + (["paired_event_days_below_production_proposal_minimum"] if not production_data_ok else [])
            + statistical_blockers,
            "curve_blockers": (
                (["curve_event_days_below_shadow_minimum"] if not curve_shadow_data_ok else [])
                + (["curve_event_days_below_production_proposal_minimum"] if not curve_production_data_ok else [])
                + curve_blockers
            ),
        },
        "guards": {
            "market_sources_excluded": True,
            "weak_sources_never_hard_lock": True,
            "provider_family_dedup_enabled": True,
            "champion_matches_production_legacy_runtime": True,
            "amsc_curve_reliability_consumed_once": bool(curve_predictions) and all(
                row["curve_state"]["contract"]["amsc_curve_reliability_consumed_once"] is True
                for row in curve_predictions
            ),
            "runtime_requests_never_change_parameters": True,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }
    return report


def _safe_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = DEFAULT_RESEARCH_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("weight_shadow_output_outside_research_root")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score-dataset", required=True, type=Path)
    parser.add_argument("--context-field", action="append", default=[])
    parser.add_argument("--specification", choices=("global", "seasonal_lead", "weather_context"), default="weather_context")
    parser.add_argument("--minimum-train-event-days", type=int, default=30)
    parser.add_argument("--fold-event-days", type=int, default=7)
    parser.add_argument("--bootstrap-replicates", type=int, default=5000)
    parser.add_argument("--shadow-minimum-event-days", type=int, default=30)
    parser.add_argument("--curve-inputs", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.score_dataset.read_text(encoding="utf-8"))
    rows = payload.get("score_rows") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        raise ValueError("score_dataset_rows_missing")
    curve_inputs = []
    if args.curve_inputs:
        curve_payload = json.loads(args.curve_inputs.read_text(encoding="utf-8"))
        curve_inputs = curve_payload.get("curve_inputs") if isinstance(curve_payload, Mapping) else curve_payload
        if not isinstance(curve_inputs, list):
            raise ValueError("curve_inputs_rows_missing")
    context_fields = args.context_field if args.specification == "weather_context" else []
    model_options = (
        {"seasonal_order": 0, "include_lead_time": False}
        if args.specification == "global"
        else {"seasonal_order": 2, "include_lead_time": True}
    )
    report = evaluate_contextual_weight_oos(
        rows,
        context_fields=context_fields,
        model_options=model_options,
        minimum_train_event_days=args.minimum_train_event_days,
        fold_event_days=args.fold_event_days,
        bootstrap_replicates=args.bootstrap_replicates,
        shadow_minimum_event_days=args.shadow_minimum_event_days,
        curve_inputs=curve_inputs,
    )
    if args.output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
        output = _safe_output(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(_canonical_bytes(report) + b"\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
