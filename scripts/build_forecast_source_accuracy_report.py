#!/usr/bin/env python3
"""Build a D1/D0 × season forecast-source accuracy research report.

The input is the immutable strict-as-of score dataset.  This report is an
evaluation surface only: it never trains, promotes, or applies source weights.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"
SCHEMA_VERSION = "forecast_source_accuracy_report_v1"
MIN_RANKABLE_EVENT_DAYS = 30
D1_MIN_LEAD_HOURS = 23.5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _mean(values: Sequence[Any]) -> float | None:
    valid = [number for value in values if (number := _finite(value)) is not None]
    return sum(valid) / len(valid) if valid else None


def _rmse(values: Sequence[Any]) -> float | None:
    valid = [number for value in values if (number := _finite(value)) is not None]
    return math.sqrt(sum(number * number for number in valid) / len(valid)) if valid else None


def _season(event_date: str) -> str:
    try:
        month = int(str(event_date)[5:7])
    except (TypeError, ValueError):
        return "未知"
    if month in (3, 4, 5):
        return "春"
    if month in (6, 7, 8):
        return "夏"
    if month in (9, 10, 11):
        return "秋"
    return "冬"


def _horizon(row: Mapping[str, Any]) -> str | None:
    lead_hours = _finite(row.get("lead_hours"))
    if lead_hours is None or lead_hours < 0:
        return None
    return "D1" if lead_hours >= D1_MIN_LEAD_HOURS else "D0"


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    event_days = sorted({str(row.get("event_date_local") or "") for row in rows if str(row.get("event_date_local") or "")})
    lineage_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        lineage_counts[str(row.get("lineage_quality") or "unknown")] += 1
    event_day_count = len(event_days)
    return {
        "sample_count": len(rows),
        "event_day_count": event_day_count,
        "event_date_from": event_days[0] if event_days else "",
        "event_date_through": event_days[-1] if event_days else "",
        "interval_mae_c": _mean([row.get("interval_mae") for row in rows]),
        "mean_bias_c": _mean([row.get("bias_interval") for row in rows]),
        "rmse_c": _rmse([row.get("bias_interval") for row in rows]),
        "nll": _mean([row.get("nll") for row in rows]),
        "rps": _mean([row.get("rps") for row in rows]),
        "top1_hit_rate_diagnostic_only": _mean([1.0 if row.get("top1_hit") else 0.0 for row in rows]),
        "top3_contains_rate_diagnostic_only": _mean([1.0 if row.get("top3_contains") else 0.0 for row in rows]),
        "lineage_quality_counts": dict(sorted(lineage_counts.items())),
        "ranking_state": "eligible_for_research_ranking" if event_day_count >= MIN_RANKABLE_EVENT_DAYS else "insufficient_independent_event_days",
        "ranking_na_reason": "" if event_day_count >= MIN_RANKABLE_EVENT_DAYS else f"event_day_count_below_{MIN_RANKABLE_EVENT_DAYS}",
    }


def build_forecast_source_accuracy_report(dataset: Mapping[str, Any]) -> dict[str, Any]:
    raw_rows = dataset.get("score_rows") if isinstance(dataset, Mapping) else None
    rows = [dict(row) for row in raw_rows or [] if isinstance(row, Mapping) and row.get("state") == "available"]
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    source_families: dict[str, str] = {}
    excluded: dict[str, int] = defaultdict(int)
    for row in rows:
        source_id = str(row.get("source_id") or "").strip()
        event_date = str(row.get("event_date_local") or "").strip()
        horizon = _horizon(row)
        if not source_id:
            excluded["source_id_missing"] += 1
            continue
        if not event_date:
            excluded["event_date_missing"] += 1
            continue
        if not horizon:
            excluded["lead_hours_missing_or_invalid"] += 1
            continue
        source_families[source_id] = str(row.get("provider_family") or "unknown")
        groups[(source_id, horizon, _season(event_date))].append(row)
    grouped_rows = []
    for (source_id, horizon, season), group in sorted(groups.items()):
        grouped_rows.append(
            {
                "source_id": source_id,
                "provider_family": source_families.get(source_id, "unknown"),
                "horizon": horizon,
                "season": season,
                **_summary(group),
            }
        )
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in grouped_rows:
        by_source[str(row["source_id"])].append(row)
    report = {
        "schema": SCHEMA_VERSION,
        "generated_at": _now(),
        "source_data_policy": str(dataset.get("source_data_policy") or "unknown"),
        "research_only": True,
        "production_weight_input": False,
        "method": {
            "label": "settlement_bucket_interval",
            "bias_definition": "forecast_minus_final_settlement; negative_means_underforecast",
            "d1_definition": f"lead_hours_greater_than_or_equal_to_{D1_MIN_LEAD_HOURS}",
            "d0_definition": f"lead_hours_less_than_{D1_MIN_LEAD_HOURS}",
            "season_definition": "meteorological_season_by_target_event_date_local",
            "d0_independence_warning": "multiple D0 revisions on one event day are not independent days; event_day_count governs ranking eligibility",
            "ranking_minimum_independent_event_days": MIN_RANKABLE_EVENT_DAYS,
        },
        "coverage": {
            "available_score_rows": len(rows),
            "grouped_score_rows": sum(len(group) for group in groups.values()),
            "distinct_event_days": len({str(row.get("event_date_local") or "") for row in rows if row.get("event_date_local")}),
            "excluded_counts": dict(sorted(excluded.items())),
        },
        "rows": grouped_rows,
        "by_source": {source: rows for source, rows in sorted(by_source.items())},
        "guards": {
            "changes_parameters": False,
            "promotes_artifact": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }
    identity = json.dumps({key: value for key, value in report.items() if key != "generated_at"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    report["report_sha256"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return report


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score-dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    dataset = json.loads(args.score_dataset.read_text(encoding="utf-8"))
    if not isinstance(dataset, Mapping):
        raise ValueError("score_dataset_mapping_required")
    report = build_forecast_source_accuracy_report(dataset)
    if args.output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
        output = args.output.expanduser().resolve()
        root = RESEARCH_ROOT.resolve()
        if output != root and root not in output.parents:
            raise ValueError("accuracy_report_output_outside_research_root")
        _atomic_write(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
