"""Proper scoring and diagnostics for one external source forecast event."""

from __future__ import annotations

import math
from typing import Any, Mapping


SCHEMA_VERSION = "forecast_source_score_event_v1"
EPS = 1e-12


def _bucket_rows(market_event: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [dict(row) for row in market_event.get("bucket_schema") or [] if isinstance(row, Mapping)]
    rows.sort(key=lambda row: float("-inf") if row.get("lower") is None else float(row["lower"]))
    return rows


def _normalize(values: list[float]) -> list[float]:
    cleaned = [max(0.0, float(value)) for value in values]
    total = sum(cleaned)
    return [value / total for value in cleaned] if total > EPS else []


def _interval_error(value: float | None, winner: Mapping[str, Any]) -> tuple[float | None, float | None]:
    if value is None:
        return None, None
    low = winner.get("lower")
    high = winner.get("upper")
    if low is not None and value < float(low):
        return float(low) - value, value - float(low)
    if high is not None and value >= float(high):
        return value - float(high), value - float(high)
    return 0.0, 0.0


def score_forecast_source_event(
    *,
    market_event: Mapping[str, Any],
    bucket_probabilities: Mapping[str, Any],
    source_id: str,
    provider_family: str,
    cutoff_id: str,
    cutoff_at: str,
    forecast_peak: float | None = None,
    forecast_snapshot_ids: list[str] | None = None,
    lineage_quality: str = "",
    coverage_quality: str = "complete",
) -> dict[str, Any]:
    rows = _bucket_rows(market_event)
    labels = [str(row.get("label") or "") for row in rows]
    winner_label = str(market_event.get("winning_bucket_label") or "")
    if not labels or winner_label not in labels:
        return {
            "schema": SCHEMA_VERSION,
            "state": "unavailable",
            "score_na_reason": "market_winning_bucket_missing_from_bucket_schema",
        }
    raw = []
    matched = 0
    for label in labels:
        try:
            value = float(bucket_probabilities.get(label, 0.0))
        except (TypeError, ValueError):
            value = 0.0
        raw.append(max(0.0, value))
        matched += int(label in bucket_probabilities)
    probabilities = _normalize(raw)
    if matched == 0 or not probabilities:
        return {
            "schema": SCHEMA_VERSION,
            "state": "unavailable",
            "score_na_reason": "source_bucket_distribution_missing",
        }
    winner_index = labels.index(winner_label)
    empirical = [1.0 if index == winner_index else 0.0 for index in range(len(labels))]
    nll = -math.log(max(probabilities[winner_index], EPS))
    brier = sum((probability - actual) ** 2 for probability, actual in zip(probabilities, empirical))
    forecast_cdf = []
    actual_cdf = []
    running_forecast = 0.0
    running_actual = 0.0
    for probability, actual in zip(probabilities, empirical):
        running_forecast += probability
        running_actual += actual
        forecast_cdf.append(running_forecast)
        actual_cdf.append(running_actual)
    rps = sum((left - right) ** 2 for left, right in zip(forecast_cdf, actual_cdf)) / max(len(labels) - 1, 1)
    maximum = max(probabilities)
    top1_labels = [label for label, probability in zip(labels, probabilities) if abs(probability - maximum) <= 1e-12]
    ranked = sorted(zip(labels, probabilities), key=lambda item: (-item[1], item[0]))
    winner_row = rows[winner_index]
    interval_mae, bias_interval = _interval_error(forecast_peak, winner_row)
    return {
        "schema": SCHEMA_VERSION,
        "state": "available",
        "event_id": str(market_event.get("event_id") or ""),
        "event_date_local": str(market_event.get("event_date_local") or ""),
        "station_id": str(market_event.get("station_id") or ""),
        "cutoff_id": str(cutoff_id or ""),
        "cutoff_at": str(cutoff_at or ""),
        "source_id": str(source_id or ""),
        "provider_family": str(provider_family or ""),
        "forecast_snapshot_ids": list(forecast_snapshot_ids or []),
        "winning_bucket": winner_label,
        "winning_bucket_edge": winner_row.get("lower") is None or winner_row.get("upper") is None,
        "bucket_order": labels,
        "bucket_probabilities": {label: probability for label, probability in zip(labels, probabilities)},
        "nll": nll,
        "rps": rps,
        "brier_multiclass": brier,
        "top1_hit": winner_label in top1_labels,
        "top1_labels": top1_labels,
        "top3_contains": winner_label in [label for label, _probability in ranked[:3]],
        "interval_mae": interval_mae,
        "bias_interval": bias_interval,
        "forecast_peak": forecast_peak,
        "coverage_quality": coverage_quality,
        "lineage_quality": lineage_quality,
        "score_na_reason": None,
    }


__all__ = ["SCHEMA_VERSION", "score_forecast_source_event"]
