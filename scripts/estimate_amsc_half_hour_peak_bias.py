#!/usr/bin/env python3
"""Estimate the daily-maximum sampling gap between AMSC half-hour and hourly views.

This is a research-only sampling-bias proxy.  It does not pretend that the
hour-mark AMSC subset is an independent NOAA/NCEI observation series.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA = "amsc_half_hour_peak_sampling_bias_research_v2"


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 1.0
    p = successes / total
    denominator = 1.0 + z * z / total
    center = (p + z * z / (2.0 * total)) / denominator
    radius = z * math.sqrt(p * (1.0 - p) / total + z * z / (4.0 * total * total)) / denominator
    return max(0.0, center - radius), min(1.0, center + radius)


def _render(report: dict[str, Any]) -> str:
    estimate = report["sampling_bias_estimate"]
    gate = report["calibration_gate"]
    lines = [
        "# AMSC 半小时峰值相对整点采样的偏差探针",
        "",
        "## 结论",
        "",
        f"- 完整日仅 `{estimate['complete_day_count']}` 天；半小时日最高温比整点子样本平均高 `{estimate['mean_bias_c']:+.3f}°C`，最大高 `{estimate['max_bias_c']:+.1f}°C`。",
        f"- 半点额外抬高日最高温发生率 `{estimate['positive_bias_rate_pct']:.1f}%`，但95% Wilson区间为 `{estimate['positive_bias_rate_wilson_95_pct'][0]:.1f}%–{estimate['positive_bias_rate_wilson_95_pct'][1]:.1f}%`，不确定性很大。",
        f"- 正式校准门：`{'通过' if gate['ready'] else '未通过'}`；当前 `{estimate['complete_day_count']} < {gate['minimum_complete_days']}`。",
        f"- AMSC半小时与NCEI小时直接配对：严格完整重叠日 `{report['direct_ncei_pairing']['complete_pair_day_count']}` 天，日最高温平均差 `{report['direct_ncei_pairing']['mean_bias_c']:+.3f}°C`。",
        "",
        "## 逐日",
        "",
        "| 北京日期 | 半小时全样本峰值 | 整点子样本峰值 | 偏差 | 峰值是否只出现在半点 |",
        "|---|---:|---:|---:|---|",
    ]
    for row in report["daily_comparisons"]:
        lines.append(
            f"| {row['event_date_local']} | {row['half_hour_max_c']:.1f}°C | {row['hour_mark_max_c']:.1f}°C | {row['bias_c']:+.1f}°C | {'是' if row['half_hour_only_peak'] else '否'} |"
        )
    lines.extend(
        [
            "",
            "## 口径边界",
            "",
            "- 这是 AMSC 半小时序列与其整点子样本的采样偏差代理，不是 AMSC 与 NOAA/NCEI 两套仪器的直接配对偏差。",
            "- 报告另列AMSC与NCEI直接配对；只有NCEI本地0–23时24个整点齐全的日期才进入正式直接偏差统计。",
            "- 当前 AMSC 在线接口仅覆盖约5天，2021–2025报文需要官方历史资料申请；因此该数值不能写入模型参数。",
            "- 只写独立研究目录，不写 archive.db、facts、策略信号或订单。",
            "",
        ]
    )
    return "\n".join(lines)


def _load_ncei_days(path: Path) -> dict[str, dict[str, Any]]:
    timezone_local = ZoneInfo("Asia/Shanghai")
    grouped: dict[str, list[tuple[int, float]]] = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="|"):
            raw_temp = str(row.get("temperature") or "").strip()
            raw_time = str(row.get("DATE") or "").strip()
            if not raw_temp or not raw_time:
                continue
            valid_utc = datetime.fromisoformat(raw_time).replace(tzinfo=timezone.utc)
            valid_local = valid_utc.astimezone(timezone_local)
            grouped[valid_local.date().isoformat()].append((valid_local.hour, float(raw_temp)))
    result = {}
    for date, values in grouped.items():
        hours = sorted({hour for hour, _ in values})
        result[date] = {
            "row_count": len(values),
            "distinct_local_hours": hours,
            "complete_local_day": hours == list(range(24)),
            "daily_max_c": max(temp for _, temp in values),
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=Path, required=True)
    parser.add_argument("--quality-report", type=Path, required=True)
    parser.add_argument("--coverage-report", type=Path, required=True)
    parser.add_argument("--ncei-psv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--minimum-complete-days", type=int, default=100)
    args = parser.parse_args()

    quality = json.loads(args.quality_report.read_text(encoding="utf-8"))
    coverage = json.loads(args.coverage_report.read_text(encoding="utf-8"))
    complete_dates = set(quality["metar_profile"]["complete_daily_max_label_dates"])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _load_jsonl(args.rows):
        date = str(row.get("event_date_local") or "")
        if date in complete_dates and row.get("temperature_c") is not None:
            grouped[date].append(row)

    comparisons = []
    for date in sorted(complete_dates):
        day_rows = grouped.get(date, [])
        half_hour_max = max(float(row["temperature_c"]) for row in day_rows)
        hour_rows = [row for row in day_rows if abs(float(row["local_hour"]) - round(float(row["local_hour"]))) < 1e-9]
        if not hour_rows:
            raise RuntimeError(f"no hour-mark samples for {date}")
        hour_max = max(float(row["temperature_c"]) for row in hour_rows)
        half_hour_only_peak = any(
            float(row["temperature_c"]) == half_hour_max
            and abs(float(row["local_hour"]) - round(float(row["local_hour"]))) >= 1e-9
            for row in day_rows
        ) and hour_max < half_hour_max
        comparisons.append(
            {
                "event_date_local": date,
                "half_hour_row_count": len(day_rows),
                "hour_mark_row_count": len(hour_rows),
                "half_hour_max_c": half_hour_max,
                "hour_mark_max_c": hour_max,
                "bias_c": half_hour_max - hour_max,
                "half_hour_only_peak": half_hour_only_peak,
            }
        )

    ncei_days = _load_ncei_days(args.ncei_psv)
    direct_pairs = []
    for row in comparisons:
        ncei = ncei_days.get(row["event_date_local"])
        if not ncei:
            continue
        direct_pairs.append(
            {
                "event_date_local": row["event_date_local"],
                "amsc_half_hour_max_c": row["half_hour_max_c"],
                "ncei_hourly_max_c": ncei["daily_max_c"],
                "amsc_minus_ncei_c": row["half_hour_max_c"] - float(ncei["daily_max_c"]),
                "ncei_row_count": ncei["row_count"],
                "ncei_complete_local_day": ncei["complete_local_day"],
            }
        )
    complete_direct_biases = [
        float(row["amsc_minus_ncei_c"]) for row in direct_pairs if row["ncei_complete_local_day"]
    ]

    biases = [float(row["bias_c"]) for row in comparisons]
    positive = sum(value > 0.0 for value in biases)
    low, high = _wilson_interval(positive, len(biases))
    blockers = []
    if len(comparisons) < args.minimum_complete_days:
        blockers.append(f"complete_days_below_minimum:{len(comparisons)}<{args.minimum_complete_days}")
    if not coverage["historical_2021_2025_available"]:
        blockers.append("amsc_2021_2025_online_history_unavailable")
    if len(complete_direct_biases) < args.minimum_complete_days:
        blockers.append(f"complete_direct_ncei_pair_days_below_minimum:{len(complete_direct_biases)}<{args.minimum_complete_days}")
    ready = not blockers
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "station_id": "ZUUU",
        "timezone": "Asia/Shanghai",
        "comparison_definition": "all available AMSC half-hour observations versus the exact-hour subset of the same AMSC sequence",
        "direct_ncei_pairing": {
            "performed": True,
            "source": "NOAA/NCEI GHCNh CHI0000ZUUU 2026 YTD",
            "comparison_count_including_partial_ncei_days": len(direct_pairs),
            "complete_pair_day_count": len(complete_direct_biases),
            "mean_bias_c": mean(complete_direct_biases) if complete_direct_biases else None,
            "median_bias_c": median(complete_direct_biases) if complete_direct_biases else None,
            "min_bias_c": min(complete_direct_biases) if complete_direct_biases else None,
            "max_bias_c": max(complete_direct_biases) if complete_direct_biases else None,
            "daily_comparisons": direct_pairs,
        },
        "daily_comparisons": comparisons,
        "sampling_bias_estimate": {
            "complete_day_count": len(comparisons),
            "mean_bias_c": mean(biases) if biases else None,
            "median_bias_c": median(biases) if biases else None,
            "min_bias_c": min(biases) if biases else None,
            "max_bias_c": max(biases) if biases else None,
            "positive_bias_day_count": positive,
            "positive_bias_rate_pct": 100.0 * positive / len(biases) if biases else None,
            "positive_bias_rate_wilson_95_pct": [100.0 * low, 100.0 * high],
        },
        "calibration_gate": {
            "ready": ready,
            "minimum_complete_days": args.minimum_complete_days,
            "blockers": blockers,
            "allowed_use": "diagnostic_sampling-bias prior only; no parameter write",
        },
        "guards": {
            "historical_backfill": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "changes_parameters": False,
            "restarts_launchd": False,
            "production_integration": False,
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "half_hour_peak_bias_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "half_hour_peak_bias_report.md").write_text(_render(report), encoding="utf-8")
    print(json.dumps({"ok": True, "ready": ready, "sampling_bias_estimate": report["sampling_bias_estimate"], "output_dir": str(args.output_dir), "guards": report["guards"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
