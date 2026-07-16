#!/usr/bin/env python3
"""Aggregate strict as-of external source scores by source and context."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "external_forecast_source_skill_report_v1"


def _mean(values: Sequence[Any]) -> float | None:
    cleaned = []
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            cleaned.append(parsed)
    return sum(cleaned) / len(cleaned) if cleaned else None


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "sample_count": len(rows),
        "event_day_count": len({str(row.get("event_date_local") or "") for row in rows}),
        "nll": _mean([row.get("nll") for row in rows]),
        "rps": _mean([row.get("rps") for row in rows]),
        "brier_multiclass": _mean([row.get("brier_multiclass") for row in rows]),
        "top1_hit_rate_diagnostic_only": _mean([1.0 if row.get("top1_hit") else 0.0 for row in rows]),
        "top3_contains_rate_diagnostic_only": _mean([1.0 if row.get("top3_contains") else 0.0 for row in rows]),
        "interval_mae": _mean([row.get("interval_mae") for row in rows]),
        "bias_interval": _mean([row.get("bias_interval") for row in rows]),
        "immutable_lineage_rate": _mean([1.0 if row.get("lineage_quality") == "immutable_envelope" else 0.0 for row in rows]),
    }


def _source_error_correlations(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_sample: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    families: dict[str, str] = {}
    for row in rows:
        try:
            nll = float(row.get("nll"))
        except (TypeError, ValueError):
            continue
        source = str(row.get("source_id") or "")
        by_sample[(str(row.get("event_id") or ""), str(row.get("cutoff_id") or ""))][source] = nll
        families[source] = str(row.get("provider_family") or source)
    sources = sorted(families)
    output = []
    for left_index, left in enumerate(sources):
        for right in sources[left_index + 1 :]:
            paired = [(values[left], values[right]) for values in by_sample.values() if left in values and right in values]
            correlation = None
            if len(paired) >= 3:
                left_values = [value[0] for value in paired]
                right_values = [value[1] for value in paired]
                left_mean, right_mean = _mean(left_values), _mean(right_values)
                left_ss = sum((value - left_mean) ** 2 for value in left_values)
                right_ss = sum((value - right_mean) ** 2 for value in right_values)
                if left_ss > 0.0 and right_ss > 0.0:
                    correlation = sum(
                        (left_value - left_mean) * (right_value - right_mean)
                        for left_value, right_value in paired
                    ) / math.sqrt(left_ss * right_ss)
            output.append(
                {
                    "left_source_id": left,
                    "right_source_id": right,
                    "left_provider_family": families[left],
                    "right_provider_family": families[right],
                    "same_provider_family": families[left] == families[right],
                    "paired_sample_count": len(paired),
                    "nll_error_correlation": correlation,
                    "na_reason": None if correlation is not None else "insufficient_or_zero_variance_paired_errors",
                }
            )
    return output


def evaluate_external_source_skill(score_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    available = [dict(row) for row in score_rows if row.get("state") == "available"]
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_lead: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_regime: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in available:
        by_source[str(row.get("source_id") or "unknown")].append(row)
        by_family[str(row.get("provider_family") or "unknown")].append(row)
        date_text = str(row.get("event_date_local") or "")
        by_month[date_text[:7] if len(date_text) >= 7 else "unknown"].append(row)
        lead = row.get("lead_hours")
        by_lead[str(lead) if lead is not None else "unknown"].append(row)
        context = row.get("context") if isinstance(row.get("context"), Mapping) else {}
        labels = context.get("segments")
        if isinstance(labels, str):
            labels = [labels]
        if not isinstance(labels, Sequence):
            labels = [context.get("weather_regime")] if context.get("weather_regime") else []
        for label in labels:
            if label:
                by_regime[str(label)].append(row)
    return {
        "schema": SCHEMA_VERSION,
        "source_data_policy": "external_only",
        "score_row_count": len(score_rows),
        "available_score_row_count": len(available),
        "paired_event_day_count": len({str(row.get("event_date_local") or "") for row in available}),
        "overall": _summary(available),
        "by_source": {key: _summary(value) for key, value in sorted(by_source.items())},
        "by_provider_family": {key: _summary(value) for key, value in sorted(by_family.items())},
        "by_event_month_diagnostic": {key: _summary(value) for key, value in sorted(by_month.items())},
        "by_lead_hours": {key: _summary(value) for key, value in sorted(by_lead.items())},
        "by_weather_context": {key: _summary(value) for key, value in sorted(by_regime.items())},
        "provider_error_correlations": _source_error_correlations(available),
        "metric_policy": {
            "promotion_primary": ["nll", "rps"],
            "hit_rate_diagnostic_only": True,
            "no_weight_promotion_from_this_in_sample_aggregate": True,
        },
        "guards": {
            "reads_existing_local_history": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score-dataset", required=True, type=Path)
    args = parser.parse_args()
    payload = json.loads(args.score_dataset.read_text(encoding="utf-8"))
    rows = payload.get("score_rows") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        raise ValueError("score_rows_missing")
    print(json.dumps(evaluate_external_source_skill(rows), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
