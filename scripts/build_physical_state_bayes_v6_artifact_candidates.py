#!/usr/bin/env python3
"""Build auditable v6 artifact candidates from historical physical observations."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from physical_latent_state_contract import LATENT_STATES
from physical_state_bayes_v6.artifact_contracts import (
    OBSERVATION_ARTIFACT_SCHEMA,
    TRANSITION_ARTIFACT_SCHEMA,
    artifact_sha256,
    validate_observation_artifact,
    validate_transition_artifact,
    default_time_conditioned_observation_policy,
)


SCHEMA_VERSION = "physical_state_bayes_v6_artifact_candidate_build_v1"
DEFAULT_INPUT = ROOT / "data/research/ncei_zuuu_joint_model/2021_2025/derived/hourly_features.jsonl.gz"
DEFAULT_OUTPUT = ROOT / "output/audits/physical_state_bayes_v6_artifact_candidates"


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _rows(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            value = json.loads(line)
            if isinstance(value, Mapping):
                yield dict(value)


def _weather_signature(row: Mapping[str, Any]) -> str:
    cloud_text = str(row.get("cloud_text") or "").upper()
    cover = _finite(row.get("cloud_cover_pct"))
    if "OVC" in cloud_text or "VV" in cloud_text or (cover is not None and cover >= 87.5):
        cloud = "overcast_or_obscured"
    elif "BKN" in cloud_text or (cover is not None and cover >= 62.5):
        cloud = "broken"
    elif "SCT" in cloud_text or (cover is not None and cover >= 37.5):
        cloud = "scattered"
    else:
        cloud = "clear_or_few"
    convective = bool(row.get("convective_cloud_reported") or row.get("current_convective_weather"))
    precipitation = bool(row.get("current_precipitation_reported"))
    weather = "convective" if convective else "precipitation" if precipitation else "none"
    return f"cloud={cloud}|weather={weather}|convective={int(convective)}"


def _label(
    row: Mapping[str, Any],
    previous_row: Mapping[str, Any] | None,
    next_row: Mapping[str, Any] | None,
) -> str:
    delta = _finite(row.get("temperature_delta_1h")) or 0.0
    remaining = _finite(row.get("remaining_warming_c"))
    remaining = max(0.0, remaining) if remaining is not None else 99.0
    hour_since_max = _finite(row.get("hours_since_observed_max")) or 0.0
    local_hour = _finite(row.get("local_hour")) or 0.0
    cloud = _finite(row.get("cloud_cover_pct")) or 0.0
    weather = bool(
        row.get("current_precipitation_reported")
        or row.get("current_convective_weather")
        or (cloud >= 75.0 and delta <= 0.5)
    )
    next_delta = _finite(next_row.get("temperature_delta_1h")) if next_row else None
    previous_delta = _finite(previous_row.get("temperature_delta_1h")) if previous_row else None
    if remaining <= 0.25 and hour_since_max >= 2.0:
        return "final_locked"
    if delta <= -0.5 and remaining <= 0.25 and (hour_since_max >= 1.0 or local_hour >= 17.0):
        return "terminal_cooling"
    if weather and delta <= 0.5:
        return "weather_suppressed"
    if delta <= -0.25 and next_delta is not None and next_delta >= 0.5 and remaining > 0.25:
        return "cooling_then_rebound"
    if delta <= -0.25:
        return "temporary_cooling"
    if delta > 0.0 and previous_delta is not None and previous_delta - delta >= 0.75:
        return "slowing_warming"
    if delta >= 0.75:
        return "warming"
    if delta > 0.15:
        return "slowing_warming"
    return "temporary_platform"


def _covariance(rows: list[tuple[float, float]]) -> tuple[list[float], list[list[float]]]:
    if not rows:
        return [0.0, 8.0], [[4.0, 0.0], [0.0, 16.0]]
    x = [item[0] for item in rows]
    y = [item[1] for item in rows]
    mean_x, mean_y = statistics.fmean(x), statistics.fmean(y)
    denom = max(1, len(rows) - 1)
    var_x = max(0.25, sum((item - mean_x) ** 2 for item in x) / denom)
    var_y = max(1.0, sum((item - mean_y) ** 2 for item in y) / denom)
    covariance = sum((a - mean_x) * (b - mean_y) for a, b in rows) / denom
    limit = 0.9 * math.sqrt(var_x * var_y)
    covariance = min(limit, max(-limit, covariance))
    return [mean_x, mean_y], [[var_x, covariance], [covariance, var_y]]


def _target_coefficients(target: str) -> dict[str, float]:
    if target == "weather_suppressed":
        return {
            "precipitation_probability": 1.2,
            "precipitation_feature_available": 0.15,
            "cloud_suppression_probability": 0.9,
            "cloud_feature_available": 0.1,
            "cb_probability": 0.7,
            "cb_feature_available": 0.1,
            "precipitation_duration_hours": 0.2,
            "cloud_suppression_duration_hours": 0.15,
        }
    if target == "terminal_cooling":
        return {
            "remaining_warming_c": -0.55,
            "forecast_rate_c_per_hour": -0.3,
            "future_max_increment_c": -0.5,
            "future_max_drawdown_c": 0.25,
            "minutes_to_curve_peak_scaled": -0.15,
            "forcing_availability": 0.1,
            "precipitation_duration_hours": 0.15,
            "cloud_suppression_duration_hours": 0.1,
        }
    if target in {"warming", "cooling_then_rebound"}:
        return {
            "remaining_warming_c": 0.15,
            "forecast_rate_c_per_hour": 0.3,
            "future_max_increment_c": 0.35,
            "future_max_drawdown_c": -0.15,
            "minutes_to_curve_peak_scaled": 0.05,
            "forcing_availability": 0.1,
            "precipitation_duration_hours": -0.2,
            "cloud_suppression_duration_hours": -0.15,
        }
    if target == "final_locked":
        return {
            "remaining_warming_c": -0.8,
            "future_max_increment_c": -0.6,
            "future_max_drawdown_c": 0.2,
            "minutes_to_curve_peak_scaled": -0.2,
        }
    return {}


def build(
    input_path: Path,
    output_dir: Path,
    *,
    training_end_exclusive: str = "2025-01-01",
) -> dict[str, Any]:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    raw_count = 0
    for row in _rows(input_path):
        raw_count += 1
        if row.get("core_qc_bad") or not row.get("daily_max_label_eligible"):
            continue
        temperature = _finite(row.get("temperature_c"))
        dewpoint = _finite(row.get("dewpoint_c"))
        delta = _finite(row.get("temperature_delta_1h"))
        day = str(row.get("event_date_local") or "")[:10]
        if (
            not day
            or day >= training_end_exclusive
            or temperature is None
            or dewpoint is None
            or delta is None
        ):
            continue
        by_day[day].append(row)
    labelled: list[tuple[dict[str, Any], str]] = []
    transitions: Counter[tuple[str, str]] = Counter()
    exposure: Counter[str] = Counter()
    for rows in by_day.values():
        rows.sort(key=lambda item: str(item.get("valid_time_utc") or ""))
        labels = [
            _label(
                row,
                rows[index - 1] if index > 0 else None,
                rows[index + 1] if index + 1 < len(rows) else None,
            )
            for index, row in enumerate(rows)
        ]
        labelled.extend(zip(rows, labels, strict=True))
        for source, target in zip(labels, labels[1:]):
            exposure[source] += 1
            if target != source:
                transitions[(source, target)] += 1
    observation_rows: dict[str, list[tuple[float, float]]] = defaultdict(list)
    signatures: dict[str, Counter[str]] = defaultdict(Counter)
    state_counts: Counter[str] = Counter()
    for row, state in labelled:
        delta = float(row["temperature_delta_1h"])
        depression = float(row["temperature_c"]) - float(row["dewpoint_c"])
        observation_rows[state].append((delta, depression))
        signatures[state][_weather_signature(row)] += 1
        state_counts[state] += 1
    all_signatures = sorted({signature for counter in signatures.values() for signature in counter})
    state_models = {}
    for state in LATENT_STATES:
        mean, covariance = _covariance(observation_rows[state])
        counts = signatures[state]
        alpha = 0.5
        denominator = sum(counts.values()) + alpha * (len(all_signatures) + 1)
        probabilities = {signature: (counts[signature] + alpha) / denominator for signature in all_signatures}
        probabilities["__other__"] = alpha / denominator
        state_models[state] = {
            "continuous_mean": mean,
            "continuous_covariance": covariance,
            "weather_signature_probabilities": probabilities,
            "training_row_count": state_counts[state],
        }
    transition_models: dict[str, dict[str, Any]] = {state: {} for state in LATENT_STATES}
    for source in LATENT_STATES:
        if source == "final_locked":
            continue
        possible = sorted(target for (candidate, target), count in transitions.items() if candidate == source and count > 0)
        for target in possible:
            transition_models[source][target] = {
                "base_rate_per_hour": max(1e-6, transitions[(source, target)] / max(1, exposure[source])),
                "coefficients": _target_coefficients(target),
                "training_transition_count": transitions[(source, target)],
            }
    source_file_sha = _sha_file(input_path)
    dataset_sha = _sha(
        {
            "source_file_sha256": source_file_sha,
            "training_end_exclusive": training_end_exclusive,
            "eligible_row_ids": [
                str(row.get("raw_sha256") or row.get("valid_time_utc") or "")
                for row, _state in labelled
            ],
        }
    )
    code_sha = _sha_file(Path(__file__).resolve())
    common = {
        "state": "candidate",
        "production_runtime_eligible": False,
        "shadow_only": False,
        "training_dataset_sha256": dataset_sha,
        "training_code_sha256": code_sha,
        "promotion_evidence_sha256": "",
        "training_scope": "NCEI_ZUUU_2021_2025_hourly_physical_observation",
        "training_end_exclusive": training_end_exclusive,
        "label_policy": "deterministic_physical_state_label_v1",
    }
    transition = {
        "schema": TRANSITION_ARTIFACT_SCHEMA,
        **common,
        "transitions": transition_models,
        "control_coefficient_policy": "frozen_physics_monotonic_v1_not_fit_from_future_truth",
    }
    transition["artifact_sha256"] = artifact_sha256(transition)
    observation = {
        "schema": OBSERVATION_ARTIFACT_SCHEMA,
        **common,
        "quantized_observation_policy": {
            "schema": "quantized_integer_temperature_robust_likelihood_v1",
            "temperature_quantization_c": 1.0,
            "measurement_error_model": "independent_uniform_rounding_moment_matched_gaussian",
            "huberize_mahalanobis_tail": True,
            "robust_mahalanobis_radius": 3.0,
        },
        "time_conditioned_observation_policy": default_time_conditioned_observation_policy(),
        "state_models": state_models,
    }
    observation["artifact_sha256"] = artifact_sha256(observation)
    validate_transition_artifact(transition)
    validate_observation_artifact(observation)
    report = {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "candidate_artifacts_built",
        "input": str(input_path.resolve()),
        "raw_row_count": raw_count,
        "eligible_row_count": len(labelled),
        "event_day_count": len(by_day),
        "state_counts": dict(state_counts),
        "transition_count": sum(transitions.values()),
        "training_dataset_sha256": dataset_sha,
        "source_file_sha256": source_file_sha,
        "training_end_exclusive": training_end_exclusive,
        "training_code_sha256": code_sha,
        "transition_artifact_sha256": transition["artifact_sha256"],
        "observation_artifact_sha256": observation["artifact_sha256"],
        "production_runtime_eligible": False,
        "shadow_runtime": False,
        "places_orders": False,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "transition_artifact.candidate.json").write_text(_json(transition) + "\n", encoding="utf-8")
    (output_dir / "observation_artifact.candidate.json").write_text(_json(observation) + "\n", encoding="utf-8")
    (output_dir / "build_report.json").write_text(_json(report) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--training-end-exclusive", default="2025-01-01")
    args = parser.parse_args()
    report = build(
        Path(args.input).expanduser().resolve(),
        Path(args.output_dir).expanduser().resolve(),
        training_end_exclusive=args.training_end_exclusive,
    )
    print(_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
