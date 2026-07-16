from __future__ import annotations

import math
import re
from typing import Any, Mapping, Sequence

from lineage_identity_runtime import lineage_id_from_parts


SCHEMA_VERSION = "weather_bias_calibration_runtime_v1"
METRICS_SCHEMA = "daily_weather_calibration_metrics_v1"
ATTRIBUTION_SCHEMA = "daily_weather_error_attribution_v1"
CALIBRATION_RECORD_SCHEMA = "daily_calibration_record_v1"

EPS = 1e-12


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _bucket_number(label: Any) -> float | None:
    text = str(label or "").replace("℃", "°C").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _bucket_matches_actual(label: Any, actual: float | None) -> bool:
    if actual is None:
        return False
    text = str(label or "").lower()
    value = _bucket_number(label)
    if value is None:
        return False
    if "or higher" in text or "higher" in text or ">=" in text:
        return float(actual) >= float(value)
    if "or below" in text or "below" in text or "<=" in text:
        return float(actual) <= float(value)
    return int(round(float(actual))) == int(round(float(value)))


def _settlement_bucket_label(rows: Sequence[Mapping[str, Any]], actual: float | None) -> str:
    if actual is None:
        return ""
    for row in rows:
        label = _first_text(row.get("label"), row.get("outcome"), row.get("bucket"))
        if label and _bucket_matches_actual(label, actual):
            return label
    return f"{int(round(float(actual)))}°C"


def _distribution_from_any(value: Any) -> list[dict[str, Any]]:
    source = value
    if isinstance(source, Mapping):
        for key in (
            "bucket_probabilities",
            "distribution",
            "rows",
            "probabilities",
            "probs",
        ):
            nested = source.get(key)
            if isinstance(nested, (Mapping, Sequence)) and not isinstance(nested, (str, bytes, bytearray)):
                source = nested
                break
    rows: list[dict[str, Any]] = []
    if isinstance(source, Mapping):
        iterable = source.items()
        for key, raw_prob in iterable:
            prob = _float_or_none(raw_prob)
            if prob is None and isinstance(raw_prob, Mapping):
                prob = _probability_from_row(raw_prob)
            if prob is None:
                continue
            rows.append({"label": str(key), "probability": max(0.0, min(1.0, prob)), "raw": raw_prob})
        return _normalize_rows(rows)
    for item in _as_sequence(source):
        row = dict(_as_mapping(item))
        label = _first_text(row.get("label"), row.get("outcome"), row.get("bucket"))
        prob = _probability_from_row(row)
        if label and prob is not None:
            rows.append({"label": label, "probability": max(0.0, min(1.0, prob)), "raw": row})
    return _normalize_rows(rows)


def _probability_from_row(row: Mapping[str, Any]) -> float | None:
    for key in (
        "probability",
        "prob",
        "q_temperature_bucket_posterior",
        "q_bayes_posterior",
        "q_posterior",
        "q_calibrated",
        "q_round",
    ):
        value = _float_or_none(row.get(key))
        if value is not None:
            return value
    return None


def _normalize_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    total = sum(max(0.0, float(row.get("probability") or 0.0)) for row in rows)
    if total <= 0:
        return [dict(row) for row in rows]
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["probability"] = max(0.0, float(item.get("probability") or 0.0)) / total
        out.append(item)
    return out


def _mean_for_rows(rows: Sequence[Mapping[str, Any]]) -> float | None:
    total = 0.0
    weight = 0.0
    for row in rows:
        value = _bucket_number(_first_text(row.get("label"), row.get("outcome"), row.get("bucket")))
        prob = _float_or_none(row.get("probability"))
        if value is None or prob is None:
            continue
        total += float(value) * float(prob)
        weight += float(prob)
    if weight <= 0:
        return None
    return total / weight


def _actual_bucket_index(rows: Sequence[Mapping[str, Any]], actual: float | None) -> int | None:
    if actual is None:
        return None
    for idx, row in enumerate(rows):
        label = _first_text(row.get("label"), row.get("outcome"), row.get("bucket"))
        if _bucket_matches_actual(label, actual):
            return idx
    return None


def _prob_of_actual(rows: Sequence[Mapping[str, Any]], actual: float | None) -> float | None:
    idx = _actual_bucket_index(rows, actual)
    if idx is None:
        return None
    return _float_or_none(rows[idx].get("probability"))


def _rank_of_actual(rows: Sequence[Mapping[str, Any]], actual: float | None) -> int | None:
    idx = _actual_bucket_index(rows, actual)
    if idx is None:
        return None
    ranked = sorted(
        enumerate(rows),
        key=lambda item: float(item[1].get("probability") or 0.0),
        reverse=True,
    )
    for rank, (row_idx, _row) in enumerate(ranked, start=1):
        if row_idx == idx:
            return rank
    return None


def _multi_bucket_brier(rows: Sequence[Mapping[str, Any]], actual: float | None) -> float | None:
    idx = _actual_bucket_index(rows, actual)
    if idx is None or not rows:
        return None
    return sum(
        (float(row.get("probability") or 0.0) - (1.0 if i == idx else 0.0)) ** 2
        for i, row in enumerate(rows)
    )


def _log_loss(rows: Sequence[Mapping[str, Any]], actual: float | None) -> float | None:
    prob = _prob_of_actual(rows, actual)
    if prob is None:
        return None
    return -math.log(max(EPS, min(1.0, float(prob))))


def _calibration_bin(prob: float | None) -> str:
    if prob is None:
        return ""
    idx = min(9, max(0, int(float(prob) * 10.0)))
    return f"{idx / 10:.1f}-{(idx + 1) / 10:.1f}"


def _layer_distribution(fact_snapshot: Mapping[str, Any], *paths: str) -> list[dict[str, Any]]:
    for path in paths:
        node: Any = fact_snapshot
        for part in path.split("."):
            node = _as_mapping(node).get(part)
        rows = _distribution_from_any(node)
        if rows:
            return rows
    return []


def _hard_constraint_violation_count(
    rows: Sequence[Mapping[str, Any]],
    fact_snapshot: Mapping[str, Any],
) -> int:
    hard = _as_mapping(fact_snapshot.get("hard_constraints"))
    observed_floor = _float_or_none(
        hard.get("observed_today_max_c")
        if hard.get("observed_today_max_c") is not None
        else fact_snapshot.get("observed_today_max_c")
    )
    if observed_floor is None:
        timing = _as_mapping(fact_snapshot.get("signal_to_order_timing"))
        observed_floor = _float_or_none(timing.get("observed_today_max_c"))
    count = 0
    if observed_floor is not None:
        for row in rows:
            label_value = _bucket_number(_first_text(row.get("label"), row.get("outcome"), row.get("bucket")))
            prob = _float_or_none(row.get("probability"))
            if label_value is not None and prob is not None and label_value < observed_floor and prob > 1e-9:
                count += 1
    hard_mask = hard.get("hard_mask")
    if isinstance(hard_mask, Mapping):
        for label, allowed in hard_mask.items():
            if allowed is False:
                for row in rows:
                    if _first_text(row.get("label")) == str(label) and float(row.get("probability") or 0.0) > 1e-9:
                        count += 1
    return count


def _source_latency_ms_by_source(fact_snapshot: Mapping[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in list(_as_sequence(fact_snapshot.get("source_latency_trace"))) + list(
        _as_sequence(fact_snapshot.get("source_observations"))
    ):
        item = _as_mapping(row)
        source_id = _first_text(item.get("source_id"), item.get("source"))
        latency = _float_or_none(item.get("response_duration_ms"))
        if source_id and latency is not None:
            out[source_id] = float(latency)
    return out


def _decision_miss_reason(decision_summary: Mapping[str, Any], target_hit: bool | None) -> str:
    if target_hit is not False:
        return ""
    if int(decision_summary.get("decision_count") or 0) <= 0:
        return "decision_missing_for_missed_target"
    if int(decision_summary.get("will_order_count") or 0) <= 0:
        return "no_will_order_for_missed_target"
    return ""


def _execution_miss_reason(
    decision_summary: Mapping[str, Any],
    execution_summary: Mapping[str, Any],
) -> str:
    if int(decision_summary.get("will_order_count") or 0) > 0 and int(execution_summary.get("submitted_order_count") or 0) <= 0:
        return "will_order_without_submitted_order"
    return ""


def build_calibration_metrics(
    *,
    fact_snapshot: Mapping[str, Any],
    posterior_rows: Sequence[Mapping[str, Any]],
    settlement_actual: float | None,
    target_bucket: str,
    target_probability: float | None,
    target_hit: bool | None,
    decision_summary: Mapping[str, Any],
    execution_summary: Mapping[str, Any],
    first_bad_link: str,
) -> dict[str, Any]:
    fact = _as_mapping(fact_snapshot)
    posterior = _normalize_rows([dict(row) for row in posterior_rows])
    prior = _layer_distribution(
        fact,
        "prior_distribution",
        "layer_contributions.prior.distribution",
        "layer_contributions.prior.bucket_probabilities",
    )
    weak = _layer_distribution(
        fact,
        "forecast_weak_likelihood",
        "forecast_weak_likelihood.distribution",
        "forecast_weak_likelihood.bucket_probabilities",
        "layer_contributions.forecast_weak_likelihood.distribution",
    )
    strong = _layer_distribution(
        fact,
        "metar_strong_likelihood",
        "metar_strong_likelihood.distribution",
        "metar_strong_likelihood.bucket_probabilities",
        "layer_contributions.metar_strong_likelihood.distribution",
    )
    actual = settlement_actual
    actual_bucket = _settlement_bucket_label(posterior, actual)
    posterior_actual_prob = _prob_of_actual(posterior, actual)
    prior_actual_prob = _prob_of_actual(prior, actual)
    weak_actual_prob = _prob_of_actual(weak, actual)
    strong_actual_prob = _prob_of_actual(strong, actual)
    prior_mean = _mean_for_rows(prior)
    weak_mean = _mean_for_rows(weak)
    strong_mean = _mean_for_rows(strong)
    metrics = {
        "schema": METRICS_SCHEMA,
        "read_only": True,
        "actual_tmax_c": actual,
        "settlement_bucket": actual_bucket,
        "target_bucket": target_bucket,
        "target_probability": target_probability,
        "target_hit": target_hit,
        "posterior_actual_bucket_probability": posterior_actual_prob,
        "posterior_brier": _multi_bucket_brier(posterior, actual),
        "posterior_log_loss": _log_loss(posterior, actual),
        "calibration_bin": _calibration_bin(posterior_actual_prob),
        "posterior_rank_of_actual_bucket": _rank_of_actual(posterior, actual),
        "hard_constraint_violation_count": _hard_constraint_violation_count(posterior, fact),
        "prior_mean_error": None if prior_mean is None or actual is None else prior_mean - float(actual),
        "prior_bucket_rank": _rank_of_actual(prior, actual),
        "weak_likelihood_shift_error": None if weak_mean is None or actual is None else weak_mean - float(actual),
        "strong_likelihood_shift_error": None if strong_mean is None or actual is None else strong_mean - float(actual),
        "D1_to_D0_probability_delta": (
            None
            if posterior_actual_prob is None or weak_actual_prob is None
            else posterior_actual_prob - weak_actual_prob
        ),
        "source_latency_ms_by_source": _source_latency_ms_by_source(fact),
        "decision_miss_reason": _decision_miss_reason(decision_summary, target_hit),
        "execution_miss_reason": _execution_miss_reason(decision_summary, execution_summary),
        "first_bad_link": first_bad_link,
        "layer_actual_bucket_probabilities": {
            "prior": prior_actual_prob,
            "forecast_weak": weak_actual_prob,
            "metar_strong": strong_actual_prob,
            "posterior": posterior_actual_prob,
        },
        "layer_distribution_presence": {
            "prior": bool(prior),
            "forecast_weak": bool(weak),
            "metar_strong": bool(strong),
            "posterior": bool(posterior),
        },
        "na_reasons": {},
    }
    na = metrics["na_reasons"]
    if actual is None:
        na["actual_tmax_c"] = "settlement_actual_missing"
    if not prior:
        na["prior_mean_error"] = "prior_distribution_missing"
        na["prior_bucket_rank"] = "prior_distribution_missing"
    if not weak:
        na["weak_likelihood_shift_error"] = "forecast_weak_likelihood_missing"
        na["D1_to_D0_probability_delta"] = "forecast_weak_likelihood_missing"
    if not strong:
        na["strong_likelihood_shift_error"] = "metar_strong_likelihood_missing"
    if posterior_actual_prob is None:
        na["posterior_actual_bucket_probability"] = "actual_bucket_not_in_posterior_distribution"
        na["posterior_brier"] = "actual_bucket_not_in_posterior_distribution"
        na["posterior_log_loss"] = "actual_bucket_not_in_posterior_distribution"
    return metrics


def build_error_attribution(
    *,
    first_bad_link: str,
    first_bad_link_reason: str,
    calibration_metrics: Mapping[str, Any],
    audit_summary: Mapping[str, Any],
) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    link = _first_text(first_bad_link, "unknown")
    reason = _first_text(first_bad_link_reason, "unspecified")
    components.append(
        {
            "link": link,
            "reason": reason,
            "primary": True,
            "evidence": {
                "target_hit": calibration_metrics.get("target_hit"),
                "posterior_rank_of_actual_bucket": calibration_metrics.get("posterior_rank_of_actual_bucket"),
                "decision_miss_reason": calibration_metrics.get("decision_miss_reason"),
                "execution_miss_reason": calibration_metrics.get("execution_miss_reason"),
            },
        }
    )
    if int(calibration_metrics.get("hard_constraint_violation_count") or 0) > 0:
        components.append(
            {
                "link": "posterior",
                "reason": "hard_constraint_violation",
                "primary": False,
                "evidence": {
                    "hard_constraint_violation_count": calibration_metrics.get("hard_constraint_violation_count"),
                },
            }
        )
    if calibration_metrics.get("target_hit") is False and link not in {"posterior", "posterior_calibration"}:
        components.append(
            {
                "link": "posterior_calibration",
                "reason": "target_bucket_missed_settlement_secondary",
                "primary": False,
                "evidence": {
                    "target_bucket": calibration_metrics.get("target_bucket"),
                    "settlement_bucket": calibration_metrics.get("settlement_bucket"),
                    "posterior_actual_bucket_probability": calibration_metrics.get(
                        "posterior_actual_bucket_probability"
                    ),
                    "posterior_rank_of_actual_bucket": calibration_metrics.get("posterior_rank_of_actual_bucket"),
                },
            }
        )
    for item in _as_sequence(audit_summary.get("items")):
        entry = _as_mapping(item)
        if entry.get("ok") is False:
            components.append(
                {
                    "link": "audit",
                    "reason": f"{entry.get('name') or 'attachment'}_failed",
                    "primary": False,
                    "evidence": {
                        "blockers": list(entry.get("blockers") or []),
                    },
                }
            )
    return {
        "schema": ATTRIBUTION_SCHEMA,
        "read_only": True,
        "primary_link": link,
        "primary_reason": reason,
        "components": components,
        "no_double_counting": True,
    }


def build_daily_calibration_record(
    *,
    review_id: str,
    run_date: str,
    slug: str,
    event_date: str,
    fact_snapshot_id: str,
    calibration_metrics: Mapping[str, Any],
    error_attribution: Mapping[str, Any],
    fact_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    fact = _as_mapping(fact_snapshot)
    record_id = lineage_id_from_parts(
        "daily_calibration_record_id",
        review_id,
        fact_snapshot_id,
        calibration_metrics.get("settlement_bucket"),
    )
    return {
        "schema": CALIBRATION_RECORD_SCHEMA,
        "daily_calibration_record_id": record_id,
        "review_id": review_id,
        "run_date": str(run_date or ""),
        "slug": str(slug or ""),
        "event_date": str(event_date or ""),
        "fact_snapshot_id": str(fact_snapshot_id or ""),
        "state": "no_apply",
        "read_only": True,
        "calibration_metrics": dict(calibration_metrics),
        "error_attribution": dict(error_attribution),
        "strata": {
            "station_id": _first_text(fact.get("station_id"), fact.get("resolution_source")),
            "event_month": str(event_date or "")[:7],
            "phase": _first_text(fact.get("phase")),
            "weather_type": _first_text(
                _as_mapping(fact.get("climo_regime")).get("weather_type"),
                _as_mapping(fact.get("basin_heat_mechanism_state")).get("status"),
            ),
            "environment_state": _first_text(
                _as_mapping(fact.get("metar_strong_likelihood")).get("state"),
                _as_mapping(fact.get("basin_heat_mechanism_state")).get("state"),
            ),
        },
        "guards": {
            "writes_archive_db": False,
            "writes_daily_weather_reviews": False,
            "writes_bias_correction_candidates": False,
            "changes_parameters": False,
            "historical_backfill": False,
            "places_orders": False,
        },
    }
