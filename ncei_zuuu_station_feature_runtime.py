"""Shared ZUUU station feature construction for the production v3.1 model."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from scripts import train_ncei_zuuu_remaining_warming_bayes as v1


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
PRECIP_RE = re.compile(r"(?:^|\s)(?:[+\-]?DZ|[+\-]?RA|[+\-]?SHRA|[+\-]?TSRA|SN|SG|PL|GR|GS)(?:\s|$)")
CONVECTIVE_RE = re.compile(r"(?:TS|CB|TCU)")
FOG_RE = re.compile(r"(?:^|\s)(?:FG|BR)(?:\s|$)")
HAZE_RE = re.compile(r"(?:^|\s)(?:HZ|FU|DU|SA)(?:\s|$)")


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _relative_humidity_pct(temp_c: float | None, dewpoint_c: float | None) -> float | None:
    if temp_c is None or dewpoint_c is None:
        return None
    a, b = 17.625, 243.04
    rh = 100.0 * math.exp(a * dewpoint_c / (b + dewpoint_c) - a * temp_c / (b + temp_c))
    return max(0.0, min(100.0, rh))


def _angular(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return abs((left - right + 180.0) % 360.0 - 180.0)


def _prior_point(rows: Sequence[Mapping[str, Any]], index: int, hours: float) -> Mapping[str, Any] | None:
    current = parse_utc(rows[index].get("valid_time"))
    if current is None:
        return None
    eligible = []
    for prior in rows[:index]:
        valid = parse_utc(prior.get("valid_time"))
        if valid is None:
            continue
        delta = (current - valid).total_seconds() / 3600.0
        if delta >= hours - 0.26:
            eligible.append((abs(delta - hours), prior))
    return min(eligible, key=lambda item: item[0])[1] if eligible else None


def station_feature_row(points: Sequence[Mapping[str, Any]], event_date: str) -> dict[str, Any] | None:
    clean = [
        dict(point)
        for point in points
        if number(point.get("temperature_c")) is not None and parse_utc(point.get("valid_time")) is not None
    ]
    clean.sort(key=lambda row: parse_utc(row.get("valid_time")) or datetime.min.replace(tzinfo=timezone.utc))
    clean = [
        row
        for row in clean
        if (parse_utc(row.get("valid_time")) or datetime.min.replace(tzinfo=timezone.utc))
        .astimezone(LOCAL_TZ)
        .date()
        .isoformat()
        == event_date
    ]
    if not clean:
        return None
    observed_max: float | None = None
    last_max_at: datetime | None = None
    convective_seen = False
    precip_seen = False
    last_convective: datetime | None = None
    last_precip: datetime | None = None
    max_cloud: float | None = None
    reports_below_hour_bins: set[str] = set()
    output: dict[str, Any] | None = None
    for index, point in enumerate(clean):
        valid = parse_utc(point.get("valid_time"))
        assert valid is not None
        local = valid.astimezone(LOCAL_TZ)
        temp = number(point.get("temperature_c"))
        dew = number(point.get("dewpoint_c"))
        assert temp is not None
        weather = str(point.get("weather_text") or "").upper()
        cloud = str(point.get("cloud_text") or "").upper()
        joined = f" {weather} {cloud} "
        current_precip = bool(PRECIP_RE.search(joined))
        current_convective = bool(CONVECTIVE_RE.search(joined))
        if observed_max is None or temp > observed_max:
            observed_max, last_max_at = temp, valid
            reports_below_hour_bins.clear()
        elif temp == observed_max:
            last_max_at = valid
            reports_below_hour_bins.clear()
        else:
            reports_below_hour_bins.add(local.strftime("%Y-%m-%dT%H"))
        if current_convective:
            convective_seen, last_convective = True, valid
        if current_precip:
            precip_seen, last_precip = True, valid
        cloud_cover = number(point.get("cloud_cover_pct"))
        if cloud_cover is not None:
            max_cloud = cloud_cover if max_cloud is None else max(max_cloud, cloud_cover)
        angle = 2.0 * math.pi * (local.timetuple().tm_yday - 1.0) / 365.2425
        hour = local.hour + local.minute / 60.0
        solar = max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0)) if 6.0 <= hour <= 18.0 else 0.0
        row: dict[str, Any] = {
            "event_date_local": event_date,
            "valid_time_utc": valid.isoformat().replace("+00:00", "Z"),
            "valid_time_local": local.isoformat(),
            "local_hour": hour,
            "month": local.month,
            "day_of_year_sin": math.sin(angle),
            "day_of_year_cos": math.cos(angle),
            "solar_heating_window": solar,
            "temperature_c": temp,
            "dewpoint_c": dew,
            "temp_dewpoint_spread_c": temp - dew if dew is not None else None,
            "relative_humidity_pct": _relative_humidity_pct(temp, dew),
            "visibility_m": number(point.get("visibility_m")),
            "pressure_hpa": number(point.get("pressure_hpa")),
            "wind_speed_mps": number(point.get("wind_speed_mps")),
            "wind_dir_deg": number(point.get("wind_dir_degrees")),
            "wind_variable": bool(point.get("wind_variable")),
            "wind_gust_mps": number(point.get("wind_gust_mps")),
            "cloud_cover_pct": cloud_cover,
            "low_cloud_cover_pct": number(point.get("low_cloud_cover_pct")),
            "current_precipitation_reported": current_precip,
            "current_convective_weather": current_convective,
            "convective_cloud_reported": "CB" in cloud or "TCU" in cloud,
            "fog_or_mist_reported": bool(FOG_RE.search(joined)),
            "haze_or_dust_reported": bool(HAZE_RE.search(joined)),
            "current_no_significant_weather": bool(point.get("metar_current_no_significant_weather")),
            "trend_convective_or_precipitation": bool(
                CONVECTIVE_RE.search(str(point.get("metar_trend_text") or "").upper())
                or PRECIP_RE.search(f" {str(point.get('metar_trend_weather_text') or '').upper()} ")
            ),
            "metar_trend_nosig": bool(point.get("metar_trend_nosig")),
            "observed_max_to_time_c": observed_max,
            "drop_from_observed_max_c": observed_max - temp,
            "hours_since_observed_max": (valid - last_max_at).total_seconds() / 3600.0 if last_max_at else None,
            "reports_below_observed_max": len(reports_below_hour_bins),
            "convective_seen_today": convective_seen,
            "precipitation_seen_today": precip_seen,
            "hours_since_convective": (valid - last_convective).total_seconds() / 3600.0 if last_convective else None,
            "hours_since_precipitation": (valid - last_precip).total_seconds() / 3600.0 if last_precip else None,
            "max_cloud_cover_to_time_pct": max_cloud,
            "post_convective_cooling": bool(convective_seen and observed_max - temp >= 1.0),
            "post_precipitation_cooling": bool(precip_seen and observed_max - temp >= 1.0),
            "interaction_peak_age_drop": ((valid - last_max_at).total_seconds() / 3600.0 if last_max_at else 0.0)
            * (observed_max - temp),
        }
        for suffix, hours_back in (("1h", 1.0), ("3h", 3.0)):
            prior = _prior_point(clean, index, hours_back)
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
                current_value = number(point.get(field))
                prior_value = number(prior.get(field))
                row[f"{out_key}_{suffix}"] = (
                    current_value - prior_value
                    if current_value is not None and prior_value is not None
                    else None
                )
            if suffix == "1h":
                row["wind_direction_shift_1h_deg"] = _angular(
                    number(point.get("wind_dir_degrees")),
                    number(prior.get("wind_dir_degrees")),
                )
        cloud_fraction = (cloud_cover or 0.0) / 100.0
        row["interaction_dewpoint_cloud"] = (dew or 0.0) * cloud_fraction
        row["interaction_cloud_solar"] = cloud_fraction * solar
        row["interaction_convective_outflow"] = (1.0 if current_convective else 0.0) * max(
            0.0, number(row.get("wind_speed_delta_1h")) or 0.0
        )
        row["interaction_precip_moisture"] = (1.0 if current_precip else 0.0) * max(0.0, dew or 0.0)
        output = row
    return output


def settlement_bucket_map(labels: Sequence[str], integer_probs: Sequence[float]) -> dict[str, float]:
    by_int = {
        int(round(float(bucket))): float(probability)
        for bucket, probability in zip(v1.BUCKETS, integer_probs)
    }
    values: dict[str, float] = {}
    for label in labels:
        numbers = [int(value) for value in re.findall(r"-?\d+", str(label))]
        if not numbers:
            values[str(label)] = 0.0
        elif "below" in str(label).lower() or "or lower" in str(label).lower() or "≤" in str(label):
            values[str(label)] = sum(probability for bucket, probability in by_int.items() if bucket <= numbers[0])
        elif "higher" in str(label).lower() or "or above" in str(label).lower() or "≥" in str(label):
            values[str(label)] = sum(probability for bucket, probability in by_int.items() if bucket >= numbers[0])
        else:
            values[str(label)] = by_int.get(numbers[0], 0.0)
    total = sum(values.values())
    return {key: value / total for key, value in values.items()} if total > 0 else values


__all__ = ["number", "parse_utc", "settlement_bucket_map", "station_feature_row"]
