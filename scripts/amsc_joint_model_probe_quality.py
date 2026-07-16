#!/usr/bin/env python3
"""Audit one bounded AMSC history probe and build leakage-safe research rows.

This research-only utility reads an observation bundle produced by
``scripts/fetch_zuuu_metar_taf.py --observations-json``.  It never opens
archive.db, never writes facts or signals, and never changes production model
parameters.  Derived labels are emitted only for station-local days that are
complete enough for a full-day maximum.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


SCHEMA = "amsc_joint_model_probe_quality_v1"
FEATURE_SCHEMA = "amsc_metar_remaining_warming_research_row_v1"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
PRECIP_RE = re.compile(r"(?:^|\s)(?:-|\+|VC)?(?:SH|TS|FZ)?(?:DZ|RA|SN|SG|PL|GR|GS)(?:\s|$)")
CONVECTIVE_RE = re.compile(r"(?:CB|TCU|TSRA|VCTS|SHRA)")


def _to_float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("payload")
    return value if isinstance(value, Mapping) else {}


def _value(row: Mapping[str, Any], key: str) -> Any:
    value = row.get(key)
    if value not in (None, ""):
        return value
    return _payload(row).get(key)


def _round(value: float | None, digits: int = 3) -> float | None:
    return round(float(value), digits) if value is not None else None


def _percentage(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 1) if denominator else 0.0


def _source_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("source_id") or "unknown")].append(row)
    out: dict[str, Any] = {}
    for source_id, source_rows in sorted(grouped.items()):
        valid_times = sorted({str(row.get("valid_time") or "") for row in source_rows if row.get("valid_time")})
        out[source_id] = {
            "row_count": len(source_rows),
            "distinct_raw_sha256_count": len({str(row.get("raw_sha256") or "") for row in source_rows}),
            "distinct_valid_time_count": len(valid_times),
            "station_local_date_count": len({str(row.get("event_date") or "") for row in source_rows}),
            "first_valid_time_utc": valid_times[0] if valid_times else None,
            "last_valid_time_utc": valid_times[-1] if valid_times else None,
        }
    return out


def _metar_day_profiles(rows: list[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
    grouped: dict[str, list[datetime]] = defaultdict(list)
    for row in rows:
        dt = _parse_dt(row.get("valid_time"))
        if dt is None:
            continue
        local = dt.astimezone(LOCAL_TZ)
        grouped[local.date().isoformat()].append(local)

    profiles: list[dict[str, Any]] = []
    complete_dates: set[str] = set()
    for event_date, values in sorted(grouped.items()):
        values.sort()
        gaps = [int((right - left).total_seconds()) for left, right in zip(values, values[1:])]
        max_gap = max(gaps) if gaps else None
        complete = bool(
            len(values) >= 40
            and values[0].hour == 0
            and values[0].minute == 0
            and values[-1].hour == 23
            and values[-1].minute == 30
            and (max_gap or 0) <= 3600
        )
        if complete:
            complete_dates.add(event_date)
        profiles.append(
            {
                "event_date": event_date,
                "row_count": len(values),
                "first_valid_time_local": values[0].isoformat(),
                "last_valid_time_local": values[-1].isoformat(),
                "max_gap_seconds": max_gap,
                "complete_for_daily_max_label": complete,
            }
        )
    return profiles, complete_dates


def _availability(rows: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    total = len(rows)

    def summarize(name: str, predicate) -> tuple[str, dict[str, Any]]:
        present = sum(1 for row in rows if predicate(row))
        return name, {"count": present, "rate_pct": _percentage(present, total)}

    checks = [
        summarize("temperature", lambda row: _to_float(row.get("temperature_c")) is not None),
        summarize("dewpoint", lambda row: _to_float(row.get("dewpoint_c")) is not None),
        summarize("visibility", lambda row: _to_float(_value(row, "visibility_m")) is not None),
        summarize("cloud_report", lambda row: str(_value(row, "cloud_text") or "").strip() != ""),
        summarize("cloud_cover", lambda row: _to_float(_value(row, "cloud_cover_pct")) is not None),
        summarize("low_cloud_numeric", lambda row: _to_float(_value(row, "low_cloud_cover_pct")) is not None),
        summarize(
            "current_weather_status",
            lambda row: bool(str(_value(row, "weather_text") or "").strip())
            or _value(row, "metar_current_no_significant_weather") is not None,
        ),
        summarize("wind_speed", lambda row: _to_float(_value(row, "wind_speed_mps")) is not None),
        summarize(
            "wind_direction_or_variable",
            lambda row: _to_float(_value(row, "wind_dir_deg")) is not None or bool(_value(row, "wind_variable")),
        ),
        summarize("pressure", lambda row: _to_float(_value(row, "pressure_hpa")) is not None),
    ]
    return dict(checks)


def _range(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, float | None]:
    values = [_to_float(_value(row, key)) for row in rows]
    values = [value for value in values if value is not None]
    return {
        "min": _round(min(values)) if values else None,
        "max": _round(max(values)) if values else None,
    }


def _model_rows(metar_rows: list[Mapping[str, Any]], complete_dates: set[str]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in metar_rows:
        grouped[str(row.get("event_date") or "")].append(row)

    output: list[dict[str, Any]] = []
    for event_date, rows in sorted(grouped.items()):
        rows.sort(key=lambda row: str(row.get("valid_time") or ""))
        temperatures = [_to_float(row.get("temperature_c")) for row in rows]
        daily_max = max(value for value in temperatures if value is not None) if event_date in complete_dates else None
        observed_max: float | None = None
        for row in rows:
            payload = _payload(row)
            valid_dt = _parse_dt(row.get("valid_time"))
            local_dt = valid_dt.astimezone(LOCAL_TZ) if valid_dt else None
            temperature = _to_float(row.get("temperature_c"))
            dewpoint = _to_float(row.get("dewpoint_c"))
            if temperature is not None:
                observed_max = temperature if observed_max is None else max(observed_max, temperature)
            cloud_text = str(payload.get("cloud_text") or "").upper()
            weather_text = str(payload.get("weather_text") or "").upper()
            trend_text = str(payload.get("metar_trend_text") or "").upper()
            trend_weather = str(payload.get("metar_trend_weather_text") or "").upper()
            row_out = {
                "schema": FEATURE_SCHEMA,
                "station_id": str(row.get("station_id") or "ZUUU"),
                "event_date_local": event_date,
                "valid_time_utc": str(row.get("valid_time") or ""),
                "valid_time_local": local_dt.isoformat() if local_dt else "",
                "local_hour": _round(local_dt.hour + local_dt.minute / 60.0) if local_dt else None,
                "raw_sha256": str(row.get("raw_sha256") or ""),
                "raw_object_ref": str(payload.get("raw_object_ref") or ""),
                "temperature_c": temperature,
                "dewpoint_c": dewpoint,
                "temp_dewpoint_spread_c": _round(temperature - dewpoint) if temperature is not None and dewpoint is not None else None,
                "visibility_m": _to_float(payload.get("visibility_m")),
                "cloud_text": cloud_text,
                "cloud_cover_pct": _to_float(payload.get("cloud_cover_pct")),
                "low_cloud_cover_pct": _to_float(payload.get("low_cloud_cover_pct")),
                "low_cloud_layer_reported": payload.get("low_cloud_cover_pct") is not None,
                "convective_cloud_reported": bool(CONVECTIVE_RE.search(cloud_text)),
                "weather_text": weather_text,
                "current_precipitation_reported": bool(PRECIP_RE.search(f" {weather_text} ")),
                "current_no_significant_weather": bool(payload.get("metar_current_no_significant_weather")),
                "metar_trend_text": trend_text,
                "metar_trend_weather_text": trend_weather,
                "trend_convective_or_precipitation": bool(CONVECTIVE_RE.search(f"{trend_text} {trend_weather}")),
                "metar_trend_nosig": bool(payload.get("metar_trend_nosig")),
                "wind_speed_mps": _to_float(payload.get("wind_speed_mps")),
                "wind_dir_deg": _to_float(payload.get("wind_dir_deg")),
                "wind_gust_mps": _to_float(payload.get("wind_gust_mps")),
                "wind_variable": bool(payload.get("wind_variable")),
                "wind_variable_from_deg": _to_float(payload.get("wind_variable_from_deg")),
                "wind_variable_to_deg": _to_float(payload.get("wind_variable_to_deg")),
                "pressure_hpa": _to_float(payload.get("pressure_hpa")),
                "observed_max_to_time_c": observed_max,
                "daily_max_label_eligible": event_date in complete_dates,
                "final_daily_max_c": daily_max,
                "remaining_warming_c": _round(max(0.0, daily_max - observed_max))
                if daily_max is not None and observed_max is not None
                else None,
                "arrival_time_excluded_from_model": True,
            }
            output.append(row_out)
    return output


def build_report(bundle: Mapping[str, Any], *, requested_count: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    observations = bundle.get("source_observations")
    rows = [row for row in observations if isinstance(row, Mapping)] if isinstance(observations, list) else []
    metar_rows = [row for row in rows if row.get("source_id") == "zuuu_metar_raw_amsc"]
    taf_rows = [row for row in rows if row.get("source_id") == "zuuu_taf_forecast_amsc"]
    day_profiles, complete_dates = _metar_day_profiles(metar_rows)
    model_rows = _model_rows(metar_rows, complete_dates)
    label_rows = [row for row in model_rows if row.get("daily_max_label_eligible")]

    current_weather_count = sum(bool(str(_value(row, "weather_text") or "").strip()) for row in metar_rows)
    convective_cloud_count = sum(
        bool(CONVECTIVE_RE.search(str(_value(row, "cloud_text") or "").upper())) for row in metar_rows
    )
    trend_count = sum(bool(str(_value(row, "metar_trend_text") or "").strip()) for row in metar_rows)
    cavok_count = sum("CAVOK" in str(_value(row, "cloud_text") or "").upper() for row in metar_rows)
    nosig_count = sum(bool(_value(row, "metar_trend_nosig")) for row in metar_rows)
    variable_wind_count = sum(bool(_value(row, "wind_variable")) for row in metar_rows)

    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "station_id": "ZUUU",
        "station_timezone": "Asia/Shanghai",
        "requested_count": requested_count,
        "returned_observation_count": len(rows),
        "returned_vs_requested_pct": _percentage(len(rows), requested_count),
        "source_summary": _source_summary(rows),
        "metar_profile": {
            "row_count": len(metar_rows),
            "distinct_valid_time_count": len({str(row.get("valid_time") or "") for row in metar_rows}),
            "distinct_raw_sha256_count": len({str(row.get("raw_sha256") or "") for row in metar_rows}),
            "day_profiles": day_profiles,
            "complete_daily_max_label_dates": sorted(complete_dates),
            "complete_daily_max_label_date_count": len(complete_dates),
            "model_row_count": len(model_rows),
            "labeled_model_row_count": len(label_rows),
            "feature_availability": _availability(metar_rows),
            "numeric_ranges": {
                "temperature_c": _range(metar_rows, "temperature_c"),
                "dewpoint_c": _range(metar_rows, "dewpoint_c"),
                "visibility_m": _range(metar_rows, "visibility_m"),
                "cloud_cover_pct": _range(metar_rows, "cloud_cover_pct"),
                "wind_speed_mps": _range(metar_rows, "wind_speed_mps"),
                "pressure_hpa": _range(metar_rows, "pressure_hpa"),
            },
            "weather_diversity": {
                "current_weather_nonempty_count": current_weather_count,
                "convective_cloud_count": convective_cloud_count,
                "trend_section_count": trend_count,
                "cavok_count": cavok_count,
                "nosig_count": nosig_count,
                "variable_wind_count": variable_wind_count,
            },
        },
        "taf_profile": {
            "row_count": len(taf_rows),
            "distinct_valid_time_count": len({str(row.get("valid_time") or "") for row in taf_rows}),
            "role": "weak_forecast_context_not_hard_observation_or_label",
        },
        "arrival_replay_quality": {
            "strict_as_of_replay_eligible": False,
            "reason": "historical reports were first observed during the probe; provider-valid time is preserved but original arrival time is unavailable",
            "model_policy": "exclude_first_seen_at_and_fetch_time_from_features; use valid_time only for research sequences",
        },
        "training_gate": {
            "ready": False,
            "blockers": [
                "complete_daily_max_label_dates_below_minimum:3<100",
                "no_seasonal_or_interannual_coverage",
                "current_precipitation_weather_diversity_missing",
                "historical_original_arrival_times_unavailable",
                "basin_and_large_scale_climate_features_not_joined",
            ],
        },
        "findings": [
            {
                "severity": "critical",
                "finding": "requested_count_did_not_produce_requested_history_depth",
                "evidence": {"requested": requested_count, "returned": len(rows), "metar": len(metar_rows), "taf": len(taf_rows)},
                "impact": "the current latest-count endpoint alone cannot supply a training-length history",
            },
            {
                "severity": "high",
                "finding": "only_three_complete_station_local_days_can_be_labeled",
                "evidence": sorted(complete_dates),
                "impact": "weather-combination effects and calibration cannot be estimated reliably",
            },
            {
                "severity": "high",
                "finding": "weather_regime_diversity_is_too_narrow",
                "evidence": {
                    "current_weather_nonempty_count": current_weather_count,
                    "convective_cloud_count": convective_cloud_count,
                    "cavok_count": cavok_count,
                },
                "impact": "rain, thunderstorm outflow, cloud-radiation and humidity interactions are not identifiable",
            },
            {
                "severity": "high",
                "finding": "backfilled_first_seen_time_is_not_historical_arrival_time",
                "impact": "strict real-time replay would leak future availability unless arrival time is excluded or independently sourced",
            },
        ],
        "recommended_next_step": {
            "action": "identify a bounded official historical-report endpoint or export before any larger fetch",
            "minimum_target": "at least 2 warm seasons and 100 complete days, preferably multiple years",
            "storage_boundary": "independent research archive only; no archive.db or fact-layer backfill",
        },
        "guards": {
            "reads_archive_db": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "changes_parameters": False,
            "restarts_launchd": False,
        },
    }
    return report, model_rows


def render_markdown(report: Mapping[str, Any]) -> str:
    metar = report["metar_profile"]
    diversity = metar["weather_diversity"]
    lines = [
        "# AMSC ZUUU 历史能力探测与数据质量报告",
        "",
        "## tl;dr",
        "",
        f"- 请求 `{report['requested_count']}` 条，实际返回 `{report['returned_observation_count']}` 条：METAR `{report['source_summary']['zuuu_metar_raw_amsc']['row_count']}`，TAF `{report['source_summary']['zuuu_taf_forecast_amsc']['row_count']}`。",
        f"- 仅 `{metar['complete_daily_max_label_date_count']}` 个北京时间完整日可生成日最高温标签，远不足以训练或校准生产模型。",
        f"- 当前天气现象非空样本 `{diversity['current_weather_nonempty_count']}` 条；虽有积雨云/塔状积云 `{diversity['convective_cloud_count']}` 条，但没有足够降雨组合，不能估计云雨、露点、风和盆地背景的联合效应。",
        "- 历史报文是在本次探测时首次抓到，原始发布时间不可恢复；严格实时回放时必须排除 first_seen/fetch 时间。",
        "",
        "## 数据与口径",
        "",
        "- 粒度：ZUUU 单条 METAR/TAF 报文；日界为 Asia/Shanghai。",
        "- METAR 用于硬观测轨迹研究；TAF 只作为弱预报背景，不作为硬标签。",
        "- 只有本地日覆盖从 00:00 到 23:30、至少40条且最大间隔不超过1小时，才生成最终日最高温和剩余升温标签。",
        "",
        "## 日期覆盖",
        "",
        "| 日期 | 报文数 | 首条 | 末条 | 最大间隔(秒) | 可作完整日标签 |",
        "|---|---:|---|---|---:|---|",
    ]
    for row in metar["day_profiles"]:
        lines.append(
            f"| {row['event_date']} | {row['row_count']} | {row['first_valid_time_local'][11:16]} | "
            f"{row['last_valid_time_local'][11:16]} | {row['max_gap_seconds']} | "
            f"{'是' if row['complete_for_daily_max_label'] else '否'} |"
        )
    lines.extend(
        [
            "",
            "## 训练阻断",
            "",
            "- 样本不足：只有3个完整日，无法做跨天气型、跨季节验证。",
            "- 组合不足：当前降水样本为空，云雨与雷暴外流效应不可识别。",
            "- 时间不可回放：历史首次到达时间缺失，不能直接做严格实时延迟回测。",
            "- 背景缺口：尚未关联副高、西南涡、850 hPa暖层、辐射和盆地通风等大尺度特征。",
            "",
            "## 下一步",
            "",
            "先定位可按日期或分页导出的官方历史报文入口；目标至少覆盖两个暖季、100个完整日，研究数据继续与生产 archive.db 隔离。",
            "",
        ]
    )
    return "\n".join(lines)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--requested-count", type=int, required=True)
    parser.add_argument("--json-report", type=Path, required=True)
    parser.add_argument("--md-report", type=Path, required=True)
    parser.add_argument("--feature-jsonl", type=Path, required=True)
    args = parser.parse_args()

    bundle = json.loads(args.observations.read_text(encoding="utf-8"))
    if not isinstance(bundle, Mapping):
        raise SystemExit("observation bundle must be a JSON object")
    report, model_rows = build_report(bundle, requested_count=args.requested_count)
    _write_text(args.json_report, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _write_text(args.md_report, render_markdown(report))
    _write_text(args.feature_jsonl, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in model_rows))
    print(
        json.dumps(
            {
                "ok": True,
                "report": str(args.json_report),
                "markdown": str(args.md_report),
                "feature_jsonl": str(args.feature_jsonl),
                "model_rows": len(model_rows),
                "labeled_model_rows": sum(bool(row.get("daily_max_label_eligible")) for row in model_rows),
                "training_ready": report["training_gate"]["ready"],
                "guards": report["guards"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
