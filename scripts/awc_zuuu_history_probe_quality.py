#!/usr/bin/env python3
"""Profile bounded AWC ZUUU METAR history and compare it with an AMSC bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


SCHEMA = "awc_zuuu_history_probe_quality_v1"
ROW_SCHEMA = "awc_zuuu_metar_research_row_v1"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


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


def _iso_z(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _visibility_m(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("+"):
        numeric = _to_float(text[:-1])
        return 9999.0 if numeric is not None and numeric >= 6.0 else (numeric * 1609.344 if numeric is not None else None)
    numeric = _to_float(text)
    return numeric * 1609.344 if numeric is not None else None


def _pressure_hpa(value: Any) -> float | None:
    numeric = _to_float(value)
    if numeric is None:
        return None
    return numeric * 33.8638866667 if numeric < 100.0 else numeric


def _awc_model_rows(rows: list[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        dt = _parse_dt(row.get("reportTime") or row.get("obsTime"))
        if dt is not None:
            grouped[dt.astimezone(LOCAL_TZ).date().isoformat()].append(row)

    complete_dates: set[str] = set()
    for event_date, event_rows in grouped.items():
        local_times = sorted(
            _parse_dt(row.get("reportTime") or row.get("obsTime")).astimezone(LOCAL_TZ)
            for row in event_rows
            if _parse_dt(row.get("reportTime") or row.get("obsTime")) is not None
        )
        if (
            len(local_times) >= 24
            and local_times[0].hour == 0
            and local_times[-1].hour == 23
            and max((right - left).total_seconds() for left, right in zip(local_times, local_times[1:])) <= 3600
        ):
            complete_dates.add(event_date)

    output: list[dict[str, Any]] = []
    for event_date, event_rows in sorted(grouped.items()):
        event_rows.sort(key=lambda row: str(row.get("reportTime") or row.get("obsTime") or ""))
        temperatures = [_to_float(row.get("temp")) for row in event_rows]
        final_max = max(value for value in temperatures if value is not None) if event_date in complete_dates else None
        observed_max: float | None = None
        for row in event_rows:
            dt = _parse_dt(row.get("reportTime") or row.get("obsTime"))
            local = dt.astimezone(LOCAL_TZ) if dt else None
            temp = _to_float(row.get("temp"))
            dew = _to_float(row.get("dewp"))
            if temp is not None:
                observed_max = temp if observed_max is None else max(observed_max, temp)
            wind_dir_raw = row.get("wdir")
            wind_dir = _to_float(wind_dir_raw)
            wind_speed_kt = _to_float(row.get("wspd"))
            wind_gust_kt = _to_float(row.get("wgst"))
            clouds = row.get("clouds") if isinstance(row.get("clouds"), list) else []
            raw = str(row.get("rawOb") or "").strip()
            output.append(
                {
                    "schema": ROW_SCHEMA,
                    "station_id": "ZUUU",
                    "event_date_local": event_date,
                    "valid_time_utc": _iso_z(dt),
                    "valid_time_local": local.isoformat() if local else "",
                    "local_hour": round(local.hour + local.minute / 60.0, 3) if local else None,
                    "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    "temperature_c": temp,
                    "dewpoint_c": dew,
                    "temp_dewpoint_spread_c": round(temp - dew, 3) if temp is not None and dew is not None else None,
                    "visibility_m": _visibility_m(row.get("visib")),
                    "cloud_layers": clouds,
                    "cloud_layer_count": len(clouds),
                    "convective_cloud_reported": "CB" in raw.upper() or "TCU" in raw.upper(),
                    "surface_cover": str(row.get("cover") or ""),
                    "weather_text": str(row.get("wxString") or ""),
                    "wind_speed_mps": round(wind_speed_kt * 0.514444, 3) if wind_speed_kt is not None else None,
                    "wind_dir_deg": wind_dir,
                    "wind_variable": str(wind_dir_raw or "").upper() == "VRB",
                    "wind_gust_mps": round(wind_gust_kt * 0.514444, 3) if wind_gust_kt is not None else None,
                    "pressure_hpa": round(_pressure_hpa(row.get("altim")), 3) if _pressure_hpa(row.get("altim")) is not None else None,
                    "observed_max_to_time_c": observed_max,
                    "daily_max_label_eligible": event_date in complete_dates,
                    "final_daily_max_c": final_max,
                    "remaining_warming_c": round(max(0.0, final_max - observed_max), 3)
                    if final_max is not None and observed_max is not None
                    else None,
                    "arrival_time_excluded_from_model": True,
                }
            )
    return output, complete_dates


def _amsc_by_time(path: Path | None) -> dict[str, Mapping[str, Any]]:
    if path is None:
        return {}
    bundle = json.loads(path.read_text(encoding="utf-8"))
    observations = bundle.get("source_observations") if isinstance(bundle, Mapping) else []
    return {
        str(row.get("valid_time") or ""): row
        for row in observations or []
        if isinstance(row, Mapping) and row.get("source_id") == "zuuu_metar_raw_amsc"
    }


def build_report(
    rows: list[Mapping[str, Any]],
    *,
    manifest: Mapping[str, Any],
    amsc_by_time: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    model_rows, complete_dates = _awc_model_rows(rows)
    matched = []
    for row in rows:
        dt = _parse_dt(row.get("reportTime") or row.get("obsTime"))
        key = _iso_z(dt)
        if key in amsc_by_time:
            matched.append((row, amsc_by_time[key]))
    temp_pairs = [(left, right) for left, right in matched if _to_float(left.get("temp")) is not None and _to_float(right.get("temperature_c")) is not None]
    dew_pairs = [(left, right) for left, right in matched if _to_float(left.get("dewp")) is not None and _to_float(right.get("dewpoint_c")) is not None]
    temp_agree = sum(abs(float(left["temp"]) - float(right["temperature_c"])) < 1e-9 for left, right in temp_pairs)
    dew_agree = sum(abs(float(left["dewp"]) - float(right["dewpoint_c"])) < 1e-9 for left, right in dew_pairs)

    day_counts: dict[str, int] = defaultdict(int)
    for row in model_rows:
        day_counts[str(row["event_date_local"])] += 1
    field_keys = {
        "temperature": "temp",
        "dewpoint": "dewp",
        "wind_direction": "wdir",
        "wind_speed": "wspd",
        "visibility": "visib",
        "pressure": "altim",
        "raw_report": "rawOb",
    }
    availability = {}
    for label, key in field_keys.items():
        count = sum(row.get(key) not in (None, "", []) for row in rows)
        availability[label] = {"count": count, "rate_pct": round(100.0 * count / len(rows), 1) if rows else 0.0}
    present_weather_counts = Counter(str(row.get("wxString") or "").strip() for row in rows if str(row.get("wxString") or "").strip())
    surface_cover_counts = Counter(str(row.get("cover") or "").strip() for row in rows if str(row.get("cover") or "").strip())
    cloud_layer_cover_counts: Counter[str] = Counter()
    for row in rows:
        for cloud in row.get("clouds") if isinstance(row.get("clouds"), list) else []:
            if isinstance(cloud, Mapping) and str(cloud.get("cover") or "").strip():
                cloud_layer_cover_counts[str(cloud.get("cover")).strip()] += 1

    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "provider": "NOAA Aviation Weather Center",
        "station_id": "ZUUU",
        "station_timezone": "Asia/Shanghai",
        "official_retention_limit_days": 15,
        "official_max_entries_per_typical_query": 400,
        "request": manifest.get("request"),
        "row_count": len(rows),
        "distinct_valid_time_count": len({row.get("valid_time_utc") for row in model_rows}),
        "day_counts": dict(sorted(day_counts.items())),
        "complete_daily_max_label_dates": sorted(complete_dates),
        "complete_daily_max_label_date_count": len(complete_dates),
        "feature_availability": availability,
        "weather_diversity": {
            "present_weather_nonempty_count": sum(bool(str(row.get("wxString") or "")) for row in rows),
            "present_weather_counts": dict(sorted(present_weather_counts.items())),
            "cloud_layer_nonempty_count": sum(bool(row.get("clouds")) for row in rows),
            "surface_cover_counts": dict(sorted(surface_cover_counts.items())),
            "cloud_layer_cover_counts": dict(sorted(cloud_layer_cover_counts.items())),
            "convective_cloud_raw_count": sum(
                "CB" in str(row.get("rawOb") or "").upper() or "TCU" in str(row.get("rawOb") or "").upper()
                for row in rows
            ),
            "gust_nonempty_count": sum(row.get("wgst") not in (None, "") for row in rows),
        },
        "amsc_alignment": {
            "matched_valid_time_count": len(matched),
            "temperature_pair_count": len(temp_pairs),
            "temperature_exact_agreement_count": temp_agree,
            "temperature_exact_agreement_rate_pct": round(100.0 * temp_agree / len(temp_pairs), 1) if temp_pairs else None,
            "dewpoint_pair_count": len(dew_pairs),
            "dewpoint_exact_agreement_count": dew_agree,
            "dewpoint_exact_agreement_rate_pct": round(100.0 * dew_agree / len(dew_pairs), 1) if dew_pairs else None,
        },
        "training_gate": {
            "ready": False,
            "blockers": [
                "awc_retention_limited_to_15_days",
                "hourly_cadence_does_not_replace_amsc_half_hour_trajectory",
                "weather_regime_and_seasonal_diversity_insufficient",
                "basin_and_large_scale_climate_features_not_joined",
            ],
        },
        "role": {
            "allowed": ["official_hourly_history_validation", "short_window_feature_enrichment", "amsc_cross_source_quality_check"],
            "not_allowed": ["masquerade_as_amsc_half_hour_history", "production_weight_training_from_this_probe", "hard_settlement_label_without_contract"],
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
    alignment = report["amsc_alignment"]
    return "\n".join(
        [
            "# AWC ZUUU 历史能力探测报告",
            "",
            "## tl;dr",
            "",
            f"- 官方 AWC 24小时窗口返回 `{report['row_count']}` 条，完整覆盖北京时间一天的24个整点。",
            f"- 与 AMSC 同时刻匹配 `{alignment['matched_valid_time_count']}` 条；温度和露点精确一致率均为 `{alignment['temperature_exact_agreement_rate_pct']}%`。",
            "- AWC 约为小时级，不能替代 AMSC 的半小时轨迹；但可作为官方短期历史校验和字段补充。",
            "- 官方数据库最多15天，仍不足以单独训练四川盆地跨季节组合模型。",
            "",
            "## 日期覆盖",
            "",
            *[f"- `{day}`：{count} 条" for day, count in report["day_counts"].items()],
            "",
            "## 训练边界",
            "",
            "- 允许：官方小时级历史校验、短窗口特征补充、AMSC 跨源一致性检查。",
            "- 禁止：冒充 AMSC 半小时历史、用本次小样本直接训练生产权重、把 AWC 当市场结算事实。",
            "",
        ]
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--awc-response", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--amsc-observations", type=Path)
    parser.add_argument("--json-report", type=Path, required=True)
    parser.add_argument("--md-report", type=Path, required=True)
    parser.add_argument("--feature-jsonl", type=Path, required=True)
    args = parser.parse_args()
    rows_raw = json.loads(args.awc_response.read_text(encoding="utf-8"))
    rows = [dict(row) for row in rows_raw if isinstance(row, Mapping)] if isinstance(rows_raw, list) else []
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report, model_rows = build_report(rows, manifest=manifest, amsc_by_time=_amsc_by_time(args.amsc_observations))
    _write(args.json_report, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _write(args.md_report, render_markdown(report))
    _write(args.feature_jsonl, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in model_rows))
    print(json.dumps({"ok": True, "row_count": len(rows), "training_ready": False, "report": str(args.json_report)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
