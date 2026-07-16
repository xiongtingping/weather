"""Offline calibration of p(S-X) from immutable observed X/S pairs."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from typing import Any, Mapping, Sequence

from resolution_bridge_artifact_runtime import (
    AUTHORITY_CONTRACT_ID,
    CANDIDATE_STATE,
    PHYSICAL_SOURCE_ID,
    SCHEMA_VERSION as ARTIFACT_SCHEMA,
    SETTLEMENT_SOURCE_ID,
    artifact_sha256,
    calendar_season,
    validate_resolution_bridge_artifact,
)


SCHEMA_VERSION = "resolution_bridge_calibration_v1"
RESIDUAL_PRIOR_STRENGTH = 3.0
SEASON_SHRINKAGE_STRENGTH = 10.0
MIN_SEASON_ROWS = 30


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sha256(value: Any) -> bool:
    text = _text(value).lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _normalize(values: Mapping[int, float]) -> dict[int, float]:
    total = sum(float(value) for value in values.values())
    if total <= 0.0:
        raise ValueError("resolution_bridge_pmf_empty")
    return {key: float(value) / total for key, value in sorted(values.items())}


def _prior(support: Sequence[int]) -> dict[int, float]:
    return _normalize({residual: math.exp(-abs(residual) / 0.75) for residual in support})


def _fit_pmf(residuals: Sequence[int], support: Sequence[int], *, prior_pmf: Mapping[int, float], strength: float) -> dict[int, float]:
    counts = Counter(residuals)
    return _normalize({
        residual: float(counts.get(residual, 0)) + float(strength) * float(prior_pmf.get(residual, 0.0))
        for residual in support
    })


def _valid_rows(pairs: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    issues: list[str] = []
    seen_days: set[str] = set()
    for pair in pairs:
        value = _mapping(pair)
        if (
            _text(value.get("authority_contract_id")) != AUTHORITY_CONTRACT_ID
            or _text(_mapping(value.get("physical_source")).get("source_id")) != PHYSICAL_SOURCE_ID
            or _text(_mapping(value.get("settlement_source")).get("source_id")) != SETTLEMENT_SOURCE_ID
        ):
            # Append-only registries retain retired authority generations.
            # They are historical evidence, not corruption of the active set.
            continue
        day = _text(value.get("event_date"))[:10]
        x = _finite(value.get("final_physical_high_c"))
        s = _finite(value.get("settlement_scalar_c"))
        if (
            _text(value.get("state")) != "available_for_bridge_calibration_only"
            or not day
            or x is None
            or s is None
            or abs(x - round(x)) > 1e-8
            or abs(s - round(s)) > 1e-8
            or not _sha256(value.get("observed_x_s_pair_sha256"))
        ):
            issues.append("bridge_calibration_pair_invalid")
            continue
        if day in seen_days:
            issues.append("bridge_calibration_duplicate_event_day")
            continue
        seen_days.add(day)
        residual = int(round(s - x))
        if abs(residual) > 8:
            issues.append("bridge_calibration_residual_out_of_contract")
            continue
        rows.append({
            "event_date": day,
            "x": int(round(x)),
            "s": int(round(s)),
            "residual": residual,
            "pair_sha256": _text(value.get("observed_x_s_pair_sha256")),
        })
    return sorted(rows, key=lambda row: row["event_date"]), sorted(set(issues))


def _crps(pmf: Mapping[int, float], actual: int) -> float:
    values = sorted(pmf)
    left = min(min(values), actual)
    right = max(max(values), actual)
    cumulative = 0.0
    total = 0.0
    for value in range(left, right + 1):
        cumulative += float(pmf.get(value, 0.0))
        observed = 1.0 if value >= actual else 0.0
        total += (cumulative - observed) ** 2
    return total


def _prequential_oos(rows: Sequence[Mapping[str, Any]], support: Sequence[int]) -> dict[str, Any]:
    minimum_history = max(30, int(math.floor(len(rows) * 0.70)))
    scores: list[dict[str, float]] = []
    base_prior = _prior(support)
    for index in range(minimum_history, len(rows)):
        train = rows[:index]
        current = rows[index]
        global_pmf = _fit_pmf(
            [int(row["residual"]) for row in train], support,
            prior_pmf=base_prior, strength=RESIDUAL_PRIOR_STRENGTH,
        )
        season = calendar_season(str(current["event_date"]))
        seasonal = [int(row["residual"]) for row in train if calendar_season(str(row["event_date"])) == season]
        pmf = _fit_pmf(
            seasonal, support, prior_pmf=global_pmf, strength=SEASON_SHRINKAGE_STRENGTH,
        ) if len(seasonal) >= MIN_SEASON_ROWS else global_pmf
        actual = int(current["residual"])
        probability = max(float(pmf.get(actual, 0.0)), 1e-12)
        expected = sum(residual * probability_mass for residual, probability_mass in pmf.items())
        scores.append({
            "log_loss": -math.log(probability),
            "absolute_error_c": abs(expected - actual),
            "crps": _crps(pmf, actual),
        })
    if not scores:
        return {"evaluated_event_days": 0, "mean_log_loss": None, "mean_absolute_error_c": None, "mean_crps": None}
    return {
        "evaluated_event_days": len(scores),
        "mean_log_loss": sum(row["log_loss"] for row in scores) / len(scores),
        "mean_absolute_error_c": sum(row["absolute_error_c"] for row in scores) / len(scores),
        "mean_crps": sum(row["crps"] for row in scores) / len(scores),
        "evaluation_kind": "chronological_expanding_prequential_v1",
        "minimum_history_event_days": minimum_history,
    }


def fit_resolution_bridge_candidate(
    pairs: Sequence[Mapping[str, Any]],
    *,
    authority_contract_sha256: str,
    minimum_distinct_event_days: int = 90,
    minimum_distinct_x_values: int = 12,
    minimum_distinct_s_values: int = 12,
) -> dict[str, Any]:
    rows, issues = _valid_rows(pairs)
    days = {row["event_date"] for row in rows}
    x_values = {row["x"] for row in rows}
    s_values = {row["s"] for row in rows}
    coverage = {
        "pair_count": len(rows),
        "distinct_event_days": len(days),
        "distinct_physical_x_values": len(x_values),
        "distinct_settlement_s_values": len(s_values),
        "minimum_distinct_event_days": int(minimum_distinct_event_days),
        "minimum_distinct_physical_x_values": int(minimum_distinct_x_values),
        "minimum_distinct_settlement_s_values": int(minimum_distinct_s_values),
    }
    blockers = list(issues)
    if len(days) < minimum_distinct_event_days:
        blockers.append("bridge_pair_event_day_coverage_below_minimum")
    if len(x_values) < minimum_distinct_x_values:
        blockers.append("bridge_pair_physical_x_support_below_minimum")
    if len(s_values) < minimum_distinct_s_values:
        blockers.append("bridge_pair_settlement_s_support_below_minimum")
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "accumulating",
            "block_reasons": sorted(set(blockers)),
            "coverage": coverage,
            "candidate_artifact": None,
            "read_only": True,
            "shadow_only": True,
            "places_orders": False,
        }
    residuals = [int(row["residual"]) for row in rows]
    support = list(range(min(-3, min(residuals)), max(3, max(residuals)) + 1))
    prior = _prior(support)
    global_pmf = _fit_pmf(residuals, support, prior_pmf=prior, strength=RESIDUAL_PRIOR_STRENGTH)
    seasonal: dict[str, dict[str, float]] = {}
    seasonal_counts: dict[str, int] = {}
    for season in ("djf", "mam", "jja", "son"):
        values = [int(row["residual"]) for row in rows if calendar_season(str(row["event_date"])) == season]
        seasonal_counts[season] = len(values)
        if len(values) >= MIN_SEASON_ROWS:
            seasonal[season] = {
                str(key): value for key, value in _fit_pmf(
                    values, support, prior_pmf=global_pmf, strength=SEASON_SHRINKAGE_STRENGTH,
                ).items()
            }
    oos = _prequential_oos(rows, support)
    core = {
        "schema": ARTIFACT_SCHEMA,
        "state": CANDIDATE_STATE,
        "authority_contract_id": AUTHORITY_CONTRACT_ID,
        "authority_contract_sha256": _text(authority_contract_sha256),
        "physical_source_id": PHYSICAL_SOURCE_ID,
        "settlement_source_id": SETTLEMENT_SOURCE_ID,
        "target_type": "amsc_scheduled_regular_metar_reported_daily_max_c",
        "settlement_type": "wunderground_zuuu_historical_observation_daily_max_c",
        "kernel_kind": "calendar_season_shrunk_discrete_residual_pmf_v1",
        "global_residual_pmf": {str(key): value for key, value in global_pmf.items()},
        "seasonal_residual_pmf": seasonal,
        "seasonal_training_counts": seasonal_counts,
        "prior": {
            "kind": "zero_centered_discrete_laplace_v1",
            "scale_c": 0.75,
            "strength": RESIDUAL_PRIOR_STRENGTH,
        },
        "season_shrinkage_strength": SEASON_SHRINKAGE_STRENGTH,
        "coverage": coverage,
        "oos_evaluation": oos,
        "training_dataset_sha256": _sha([row["pair_sha256"] for row in rows]),
        "training_event_date_min": rows[0]["event_date"],
        "training_event_date_max": rows[-1]["event_date"],
        "bridge_closed": False,
        "validated": False,
        "production_runtime_eligible": False,
        "shadow_only": True,
        "places_orders": False,
    }
    artifact = {**core, "artifact_sha256": artifact_sha256(core)}
    try:
        validate_resolution_bridge_artifact(artifact)
    except ValueError as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "candidate_invalid",
            "block_reasons": [str(exc)],
            "coverage": coverage,
            "candidate_artifact": None,
            "read_only": True,
            "shadow_only": True,
            "places_orders": False,
        }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "candidate_ready_for_review",
        "coverage": coverage,
        "candidate_artifact": artifact,
        "read_only": True,
        "shadow_only": True,
        "places_orders": False,
        "boundary": {
            "activates_artifact": False,
            "closes_bridge": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "fit_resolution_bridge_candidate"]
