#!/usr/bin/env python3
"""Build a leakage-safe ZUUU joint-weather research dataset from NCEI GHCNh.

The output retains raw-report lineage, separates current METAR weather from
trend forecasts, combines thermodynamic/cloud/weather/wind/pressure evidence,
and emits remaining-warming labels only for complete Asia/Shanghai days.
Production databases, facts, signals, parameters, and launchd are untouched.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.fetch_zuuu_metar_taf import _build_metar_source_payload  # noqa: E402


SCHEMA = "ncei_zuuu_joint_model_dataset_v1"
ROW_SCHEMA = "ncei_zuuu_joint_weather_research_row_v1"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
STATION_ID = "CHI0000ZUUU"
# GHCNh REM prefixes the original report with a length token such as
# ``MET060METAR``; therefore METAR is not preceded by a regex word boundary.
RAW_METAR_RE = re.compile(r"METAR\s+ZUUU\b.*?=", re.IGNORECASE)
CONVECTIVE_RE = re.compile(r"(?:TS|CB|TCU)", re.IGNORECASE)
PRECIP_RE = re.compile(r"(?:DZ|RA|SHRA|TSRA|SN|SG|PL|GR|GS)", re.IGNORECASE)
FOG_RE = re.compile(r"(?:FG|BR)", re.IGNORECASE)
HAZE_RE = re.compile(r"(?:HZ|FU|DU|SA)", re.IGNORECASE)
CORE_QC_BAD = {"2", "3", "5", "6", "7"}


def _to_float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso_z(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z") if value is not None else ""


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(float(value), digits) if value is not None else None


def _extract_raw_metar(rem: Any) -> str:
    match = RAW_METAR_RE.search(str(rem or ""))
    return match.group(0).strip() if match else ""


def _quality_bad(row: Mapping[str, Any], field: str) -> bool:
    return str(row.get(f"{field}_Quality_Code") or "").strip() in CORE_QC_BAD


def _angular_difference(left: float | None, right: float | None) -> float | None:
    if left is None or right is None or left >= 990 or right >= 990:
        return None
    return abs((float(left) - float(right) + 180.0) % 360.0 - 180.0)


def _cloud_cover_from_layers(values: list[str]) -> float | None:
    scores = {"CLR": 0.0, "SKC": 0.0, "FEW": 18.75, "SCT": 43.75, "BKN": 75.0, "OVC": 100.0, "VV": 100.0}
    found = []
    for value in values:
        prefix = str(value or "").split(":", 1)[0].upper()
        if prefix in scores:
            found.append(scores[prefix])
    return max(found) if found else None


def _weather_codes(row: Mapping[str, Any]) -> list[str]:
    keys = [f"pres_wx_{family}{index}" for family in ("MW", "AU", "AW") for index in (1, 2, 3)]
    return [str(row.get(key) or "").strip() for key in keys if str(row.get(key) or "").strip()]


def _load_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    expected_header: list[str] | None = None
    for path in paths:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="|")
            if expected_header is None:
                expected_header = list(reader.fieldnames or [])
            elif list(reader.fieldnames or []) != expected_header:
                raise RuntimeError(f"schema drift in {path}")
            for row in reader:
                if str(row.get("STATION") or "") != STATION_ID or str(row.get("Station_name") or "").upper() != "SHUANGLIU":
                    raise RuntimeError(f"station identity mismatch in {path}: {row.get('STATION')} {row.get('Station_name')}")
                row["_source_path"] = str(path)
                rows.append(dict(row))
    return rows


def _base_feature(row: Mapping[str, Any]) -> dict[str, Any]:
    valid_time = _parse_utc(row.get("DATE"))
    local = valid_time.astimezone(LOCAL_TZ) if valid_time else None
    raw = _extract_raw_metar(row.get("REM"))
    parsed = _build_metar_source_payload(
        {"content": raw, "airportName": "SHUANGLIU", "airportEnName": "SHUANGLIU"},
        fetched_at=_iso_z(valid_time),
        duration_ms=None,
    ) if raw and valid_time else None
    parsed = parsed or {}
    raw_valid_time = _parse_utc(parsed.get("valid_time_utc"))
    cloud_layers = [str(row.get(f"sky_cover_layer_{index}") or "") for index in range(1, 5)]
    cloud_bases = [_to_float(row.get(f"sky_cover_layer_baseht_{index}")) for index in range(1, 5)]
    weather_codes = _weather_codes(row)
    weather_joined = " ".join([str(parsed.get("weather_text") or ""), *weather_codes]).upper()
    cloud_text = str(parsed.get("cloud_text") or "").upper()
    temp = _to_float(row.get("temperature"))
    dewpoint = _to_float(row.get("dew_point_temperature"))
    wind_direction = _to_float(row.get("wind_direction"))
    wind_speed = _to_float(row.get("wind_speed"))
    pressure = _to_float(row.get("altimeter"))
    visibility_km = _to_float(row.get("visibility"))
    decoded_cover = _cloud_cover_from_layers(cloud_layers)
    parser_cover = _to_float(parsed.get("cloud_cover_pct"))
    cloud_cover = parser_cover if parser_cover is not None else decoded_cover
    low_layers = [
        cloud_layers[index]
        for index, base in enumerate(cloud_bases)
        if base is not None and base <= 1000.0
    ]
    low_cover = _cloud_cover_from_layers(low_layers)
    if low_cover is None:
        low_cover = _to_float(parsed.get("low_cloud_cover_pct"))
    core_qc_bad = any(
        _quality_bad(row, field)
        for field in ("temperature", "dew_point_temperature", "wind_direction", "wind_speed", "visibility", "altimeter")
    )
    day_of_year = local.timetuple().tm_yday if local else None
    day_angle = 2.0 * math.pi * (float(day_of_year) - 1.0) / 365.2425 if day_of_year else None
    hour = local.hour + local.minute / 60.0 if local else None
    solar_window = max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0)) if hour is not None and 6.0 <= hour <= 18.0 else 0.0
    return {
        "schema": ROW_SCHEMA,
        "station_id": "ZUUU",
        "ghcnh_station_id": STATION_ID,
        "event_date_local": local.date().isoformat() if local else "",
        "valid_time_utc": _iso_z(valid_time),
        "valid_time_local": local.isoformat() if local else "",
        "local_hour": _round(hour, 3),
        "month": local.month if local else None,
        "day_of_year": day_of_year,
        "day_of_year_sin": _round(math.sin(day_angle), 6) if day_angle is not None else None,
        "day_of_year_cos": _round(math.cos(day_angle), 6) if day_angle is not None else None,
        "solar_heating_window": _round(solar_window, 6),
        "report_type": str(row.get("temperature_Report_Type") or ""),
        "source_code": str(row.get("temperature_Source_Code") or ""),
        "source_station_id": str(row.get("temperature_Source_Station_ID") or ""),
        "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else "",
        "raw_metar_available": bool(raw),
        "raw_time_matches_psv": bool(raw_valid_time == valid_time) if raw_valid_time and valid_time else False,
        "temperature_c": temp,
        "dewpoint_c": dewpoint,
        "temp_dewpoint_spread_c": _round(temp - dewpoint) if temp is not None and dewpoint is not None else None,
        "relative_humidity_pct": _to_float(row.get("relative_humidity")),
        "visibility_m": _round(visibility_km * 1000.0, 1) if visibility_km is not None else None,
        "pressure_hpa": pressure,
        "wind_speed_mps": wind_speed,
        "wind_dir_deg": None if wind_direction is not None and wind_direction >= 990 else wind_direction,
        "wind_variable": bool(wind_direction is not None and wind_direction >= 990) or bool(parsed.get("wind_variable")),
        "wind_gust_mps": _to_float(row.get("wind_gust")),
        "cloud_text": cloud_text,
        "cloud_cover_pct": cloud_cover,
        "low_cloud_cover_pct": low_cover,
        "cloud_layers": [value for value in cloud_layers if value],
        "cloud_layer_bases_m": [value for value in cloud_bases if value is not None],
        "convective_cloud_reported": bool("CB" in raw.upper() or "TCU" in raw.upper()),
        "weather_text": str(parsed.get("weather_text") or "").upper(),
        "weather_codes": weather_codes,
        "current_precipitation_reported": bool(PRECIP_RE.search(weather_joined)),
        "current_convective_weather": bool(CONVECTIVE_RE.search(weather_joined)),
        "fog_or_mist_reported": bool(FOG_RE.search(weather_joined)),
        "haze_or_dust_reported": bool(HAZE_RE.search(weather_joined)),
        "current_no_significant_weather": bool(parsed.get("metar_current_no_significant_weather")),
        "metar_trend_text": str(parsed.get("metar_trend_text") or "").upper(),
        "metar_trend_weather_text": str(parsed.get("metar_trend_weather_text") or "").upper(),
        "trend_convective_or_precipitation": bool(
            CONVECTIVE_RE.search(f"{parsed.get('metar_trend_text') or ''} {parsed.get('metar_trend_weather_text') or ''}")
            or PRECIP_RE.search(f"{parsed.get('metar_trend_text') or ''} {parsed.get('metar_trend_weather_text') or ''}")
        ),
        "metar_trend_nosig": bool(parsed.get("metar_trend_nosig")),
        "temperature_qc": str(row.get("temperature_Quality_Code") or ""),
        "dewpoint_qc": str(row.get("dew_point_temperature_Quality_Code") or ""),
        "core_qc_bad": core_qc_bad,
        "numeric_precipitation_available": _to_float(row.get("precipitation")) is not None,
        "numeric_precipitation_mm": _to_float(row.get("precipitation")),
        "arrival_time_excluded_from_model": True,
    }


def _complete_days(features: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in features:
        grouped[str(row["event_date_local"])].append(row)
    summaries: dict[str, dict[str, Any]] = {}
    complete: set[str] = set()
    for day, rows in sorted(grouped.items()):
        rows.sort(key=lambda row: str(row["valid_time_utc"]))
        times = [_parse_utc(row["valid_time_utc"]) for row in rows]
        times = [value for value in times if value is not None]
        local_hours = sorted({str(row["valid_time_local"])[11:16] for row in rows})
        temp_rows = [row for row in rows if row.get("temperature_c") is not None and not row.get("core_qc_bad")]
        max_gap = max(((right - left).total_seconds() for left, right in zip(times, times[1:])), default=None)
        is_complete = bool(
            len(local_hours) == 24
            and local_hours[0] == "00:00"
            and local_hours[-1] == "23:00"
            and len(temp_rows) == 24
            and (max_gap or 0) <= 3600
        )
        if is_complete:
            complete.add(day)
        final_max = max((float(row["temperature_c"]) for row in temp_rows), default=None) if is_complete else None
        max_rows = [row for row in temp_rows if final_max is not None and float(row["temperature_c"]) == final_max]
        summaries[day] = {
            "event_date_local": day,
            "row_count": len(rows),
            "unique_local_hour_count": len(local_hours),
            "training_eligible_temperature_count": len(temp_rows),
            "max_gap_seconds": int(max_gap) if max_gap is not None else None,
            "complete_for_daily_max_label": is_complete,
            "final_daily_max_c": final_max,
            "first_max_valid_time_local": str(max_rows[0]["valid_time_local"]) if max_rows else "",
        }
    return summaries, complete


def _apply_sequence_features(features: list[dict[str, Any]], daily: Mapping[str, Mapping[str, Any]], complete: set[str]) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in features:
        grouped[str(row["event_date_local"])].append(row)
    for day, rows in grouped.items():
        rows.sort(key=lambda row: str(row["valid_time_utc"]))
        observed_max: float | None = None
        last_observed_max_at: datetime | None = None
        last_convective_at: datetime | None = None
        last_precipitation_at: datetime | None = None
        convective_seen_today = False
        precipitation_seen_today = False
        reports_below_observed_max = 0
        max_cloud_cover_to_time: float | None = None
        final_max = _to_float(daily.get(day, {}).get("final_daily_max_c")) if day in complete else None
        for index, row in enumerate(rows):
            valid_time = _parse_utc(row.get("valid_time_utc"))
            temp = _to_float(row.get("temperature_c"))
            if temp is not None and not row.get("core_qc_bad"):
                if observed_max is None or temp > observed_max:
                    observed_max = temp
                    last_observed_max_at = valid_time
                    reports_below_observed_max = 0
                elif temp == observed_max:
                    # A plateau at the realized maximum resets peak age without
                    # using any observation after the current row.
                    last_observed_max_at = valid_time
                    reports_below_observed_max = 0
                else:
                    reports_below_observed_max += 1

            current_convective = bool(row.get("current_convective_weather") or row.get("convective_cloud_reported"))
            current_precipitation = bool(row.get("current_precipitation_reported"))
            if current_convective:
                convective_seen_today = True
                last_convective_at = valid_time
            if current_precipitation:
                precipitation_seen_today = True
                last_precipitation_at = valid_time
            cloud_cover = _to_float(row.get("cloud_cover_pct"))
            if cloud_cover is not None:
                max_cloud_cover_to_time = cloud_cover if max_cloud_cover_to_time is None else max(max_cloud_cover_to_time, cloud_cover)

            row["observed_max_to_time_c"] = observed_max
            row["drop_from_observed_max_c"] = _round(observed_max - temp) if observed_max is not None and temp is not None else None
            row["hours_since_observed_max"] = _round(
                (valid_time - last_observed_max_at).total_seconds() / 3600.0
                if valid_time is not None and last_observed_max_at is not None
                else None
            )
            row["reports_below_observed_max"] = reports_below_observed_max
            row["convective_seen_today"] = convective_seen_today
            row["precipitation_seen_today"] = precipitation_seen_today
            row["hours_since_convective"] = _round(
                (valid_time - last_convective_at).total_seconds() / 3600.0
                if valid_time is not None and last_convective_at is not None
                else None
            )
            row["hours_since_precipitation"] = _round(
                (valid_time - last_precipitation_at).total_seconds() / 3600.0
                if valid_time is not None and last_precipitation_at is not None
                else None
            )
            row["max_cloud_cover_to_time_pct"] = _round(max_cloud_cover_to_time)
            row["post_convective_cooling"] = bool(
                convective_seen_today and (_to_float(row.get("drop_from_observed_max_c")) or 0.0) >= 1.0
            )
            row["post_precipitation_cooling"] = bool(
                precipitation_seen_today and (_to_float(row.get("drop_from_observed_max_c")) or 0.0) >= 1.0
            )
            row["interaction_peak_age_drop"] = _round(
                (_to_float(row.get("hours_since_observed_max")) or 0.0)
                * (_to_float(row.get("drop_from_observed_max_c")) or 0.0)
            )
            row["daily_max_label_eligible"] = day in complete
            row["final_daily_max_c"] = final_max
            row["remaining_warming_c"] = _round(max(0.0, final_max - observed_max)) if final_max is not None and observed_max is not None else None
            previous = rows[index - 1] if index >= 1 else None
            prior3 = rows[index - 3] if index >= 3 else None
            for suffix, prior in (("1h", previous), ("3h", prior3)):
                if prior is None:
                    continue
                for out_key, field in (
                    ("temperature_delta", "temperature_c"),
                    ("dewpoint_delta", "dewpoint_c"),
                    ("pressure_delta", "pressure_hpa"),
                    ("wind_speed_delta", "wind_speed_mps"),
                    ("visibility_delta", "visibility_m"),
                    ("cloud_cover_delta", "cloud_cover_pct"),
                ):
                    current_value = _to_float(row.get(field))
                    prior_value = _to_float(prior.get(field))
                    row[f"{out_key}_{suffix}"] = _round(current_value - prior_value) if current_value is not None and prior_value is not None else None
            row["wind_direction_shift_1h_deg"] = _round(
                _angular_difference(_to_float(row.get("wind_dir_deg")), _to_float(previous.get("wind_dir_deg")) if previous else None)
            )
            cloud_fraction = (_to_float(row.get("cloud_cover_pct")) or 0.0) / 100.0
            row["interaction_dewpoint_cloud"] = _round((_to_float(row.get("dewpoint_c")) or 0.0) * cloud_fraction)
            row["interaction_cloud_solar"] = _round(cloud_fraction * (_to_float(row.get("solar_heating_window")) or 0.0))
            row["interaction_convective_outflow"] = _round(
                (1.0 if row.get("current_convective_weather") or row.get("convective_cloud_reported") else 0.0)
                * max(0.0, _to_float(row.get("wind_speed_delta_1h")) or 0.0)
            )
            row["interaction_precip_moisture"] = _round(
                (1.0 if row.get("current_precipitation_reported") else 0.0)
                * max(0.0, _to_float(row.get("dewpoint_c")) or 0.0)
            )


def _availability(features: list[Mapping[str, Any]], fields: list[str]) -> dict[str, dict[str, Any]]:
    total = len(features)
    result = {}
    for field in fields:
        count = sum(row.get(field) not in (None, "", []) for row in features)
        result[field] = {"count": count, "rate_pct": round(100.0 * count / total, 2) if total else 0.0}
    return result


def build_report(raw_rows: list[dict[str, Any]], features: list[dict[str, Any]], daily: Mapping[str, Mapping[str, Any]], complete: set[str]) -> dict[str, Any]:
    years = Counter(str(row.get("Year") or "") for row in raw_rows)
    report_types = Counter(str(row.get("temperature_Report_Type") or "") for row in raw_rows)
    source_codes = Counter(str(row.get("temperature_Source_Code") or "") for row in raw_rows)
    weather = Counter(code for row in raw_rows for code in _weather_codes(row))
    current_precip = sum(bool(row.get("current_precipitation_reported")) for row in features)
    convective = sum(bool(row.get("current_convective_weather") or row.get("convective_cloud_reported")) for row in features)
    raw_available = sum(bool(row.get("raw_metar_available")) for row in features)
    raw_time_matches = sum(bool(row.get("raw_time_matches_psv")) for row in features)
    qc_bad = sum(bool(row.get("core_qc_bad")) for row in features)
    hot_season_days = sum(day[5:7] in {"05", "06", "07", "08", "09"} for day in complete)
    labels = [_to_float(row.get("final_daily_max_c")) for row in daily.values() if row.get("complete_for_daily_max_label")]
    labels = [value for value in labels if value is not None]
    distinct_times = {str(row.get("DATE") or "") for row in raw_rows}
    blockers = []
    research_ready = True
    if len(complete) < 100:
        blockers.append(f"complete_days_below_100:{len(complete)}")
        research_ready = False
    if hot_season_days < 100:
        blockers.append(f"hot_season_days_below_100:{hot_season_days}")
        research_ready = False
    if current_precip < 100 or convective < 50:
        blockers.append(f"weather_diversity_low:precip={current_precip},convective={convective}")
        research_ready = False
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "station": {"ghcnh_station_id": STATION_ID, "icao": "ZUUU", "name": "SHUANGLIU", "timezone": "Asia/Shanghai"},
        "grain": "one GHCNh hourly FM15 report per UTC timestamp",
        "row_count": len(raw_rows),
        "distinct_utc_timestamp_count": len(distinct_times),
        "duplicate_utc_timestamp_count": len(raw_rows) - len(distinct_times),
        "rows_by_year": dict(sorted(years.items())),
        "report_type_counts": dict(sorted(report_types.items())),
        "source_code_counts": dict(sorted(source_codes.items())),
        "raw_metar": {
            "available_count": raw_available,
            "available_rate_pct": round(100.0 * raw_available / len(features), 3),
            "time_match_count": raw_time_matches,
            "time_match_rate_pct": round(100.0 * raw_time_matches / max(1, raw_available), 3),
        },
        "feature_availability": _availability(
            features,
            [
                "temperature_c", "dewpoint_c", "relative_humidity_pct", "visibility_m", "pressure_hpa",
                "wind_speed_mps", "wind_dir_deg", "wind_gust_mps", "cloud_text", "cloud_cover_pct",
                "low_cloud_cover_pct", "weather_codes", "numeric_precipitation_mm",
            ],
        ),
        "quality": {
            "core_qc_bad_row_count": qc_bad,
            "core_qc_bad_rate_pct": round(100.0 * qc_bad / len(features), 3),
            "numeric_precipitation_missing_count": sum(not bool(row.get("numeric_precipitation_available")) for row in features),
        },
        "weather_diversity": {
            "present_weather_code_counts": dict(weather.most_common()),
            "current_precipitation_report_count": current_precip,
            "current_convective_or_cb_tcu_report_count": convective,
            "fog_or_mist_report_count": sum(bool(row.get("fog_or_mist_reported")) for row in features),
            "haze_or_dust_report_count": sum(bool(row.get("haze_or_dust_reported")) for row in features),
            "trend_section_count": sum(bool(row.get("metar_trend_text")) for row in features),
        },
        "daily_labels": {
            "station_local_day_count": len(daily),
            "complete_day_count": len(complete),
            "hot_season_complete_day_count": hot_season_days,
            "complete_day_rate_pct": round(100.0 * len(complete) / max(1, len(daily)), 2),
            "min_final_daily_max_c": min(labels) if labels else None,
            "max_final_daily_max_c": max(labels) if labels else None,
            "label_semantics": "maximum of QC-eligible hourly FM15 observations within Asia/Shanghai day",
            "caveat": "hourly maximum may miss between-hour or settlement-source extremes",
        },
        "training_gate": {
            "research_surface_model_ready": research_ready,
            "blockers": blockers,
            "production_ready": False,
            "production_blockers": [
                "hourly_max_not_yet_calibrated_to_amsc_half_hour_or_market_settlement",
                "numeric_precipitation_amount_missing_for_station",
                "large_scale_synoptic_and_reanalysis_context_not_joined",
                "walk_forward_year_holdout_not_yet_run",
            ],
        },
        "guards": {
            "reads_archive_db": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "changes_parameters": False,
            "restarts_launchd": False,
            "production_backfill": False,
        },
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    labels = report["daily_labels"]
    diversity = report["weather_diversity"]
    gate = report["training_gate"]
    return "\n".join(
        [
            "# NOAA/NCEI ZUUU 2021–2025 数据质量报告",
            "",
            "## tl;dr",
            "",
            f"- `{report['row_count']}` 条小时 FM15/METAR，UTC时间戳重复 `{report['duplicate_utc_timestamp_count']}` 条。",
            f"- 原始 METAR 保留率 `{report['raw_metar']['available_rate_pct']}%`，报文时间与PSV时间一致率 `{report['raw_metar']['time_match_rate_pct']}%`。",
            f"- 北京时间完整日 `{labels['complete_day_count']}` 个，其中暖季 `{labels['hot_season_complete_day_count']}` 个。",
            f"- 降水现象 `{diversity['current_precipitation_report_count']}` 条，对流/CB/TCU `{diversity['current_convective_or_cb_tcu_report_count']}` 条。",
            f"- 研究层表面天气模型：`{'可训练' if gate['research_surface_model_ready'] else '阻断'}`；生产接入：`阻断`。",
            "",
            "## 关键质量结论",
            "",
            "- 站点固定为 CHI0000ZUUU / ZUUU / SHUANGLIU，不混入旧成都站。",
            "- 温度、露点、风、能见度和气压可用于组合模型；云和天气现象同时保留原始代码与解析字段。",
            "- 数量降水字段缺失，只能学习降水类型/是否发生，不能学习毫米强度。",
            "- 日最高温标签是北京时间日内小时观测最大值，必须在后续对 AMSC 半小时峰值和市场结算源做偏差校准。",
            "- first_seen/fetch 时间不进入特征；训练按年份前推验证，禁止随机拆分造成未来信息泄漏。",
            "",
        ]
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    paths = sorted(args.input_dir.glob("GHCNh_CHI0000ZUUU_*.psv"))
    if not paths:
        raise SystemExit("no NCEI ZUUU PSV files found")
    raw_rows = _load_rows(paths)
    features = [_base_feature(row) for row in raw_rows]
    features.sort(key=lambda row: str(row["valid_time_utc"]))
    daily, complete = _complete_days(features)
    _apply_sequence_features(features, daily, complete)
    report = build_report(raw_rows, features, daily, complete)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output_dir / "hourly_features.jsonl.gz", "wt", encoding="utf-8") as handle:
        for row in features:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    daily_fields = [
        "event_date_local", "row_count", "unique_local_hour_count", "training_eligible_temperature_count",
        "max_gap_seconds", "complete_for_daily_max_label", "final_daily_max_c", "first_max_valid_time_local",
    ]
    with (args.output_dir / "daily_labels.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=daily_fields)
        writer.writeheader()
        writer.writerows(daily[day] for day in sorted(daily))
    _write_json(args.output_dir / "quality_report.json", report)
    (args.output_dir / "quality_report.md").write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "hourly_rows": len(features),
                "complete_days": len(complete),
                "research_surface_model_ready": report["training_gate"]["research_surface_model_ready"],
                "production_ready": False,
                "output_dir": str(args.output_dir),
                "guards": report["guards"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
