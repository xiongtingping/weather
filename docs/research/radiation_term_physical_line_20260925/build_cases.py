#!/usr/bin/env python3
"""Build station-day and decision-hour cases from the fetched public inputs.

Every physical feature is read with the production function that reads it in
serving, so the study measures production's semantics rather than a
re-implementation of them:

* TAF cloud / precipitation / wind over the local 06-15 heating window:
  ``capture_bayes_basket_target_awc_taf_calibration._forecast_features``
  (the 2026-09-23 semantics), TX: its ``_tx``;
* METAR cloud / precipitation / wind / dewpoint:
  ``metar_path_state.parse_metar_physical_state``;
* the P0 radiation term: ``cross_consensus.forecast_clearness_shift_sigma``
  on the station's own 180 days of forecast radiation before the event day,
  at the served coefficient ``RADIATION_SHIFT_SIGMA``.

Outputs (under ``<cache>/derived``):

* ``days.jsonl``  one row per settled station-day: truth, the day-ahead model
  maximum, the radiation term, the model's heating-window cloud /
  precipitation / wind, and the TAF standing at each P0-M cutoff;
* ``p1.jsonl``    one row per station-day per local decision hour: the
  running high, the temperature path against the model curve, the METAR
  physical state now, the elapsed-daylight observed-vs-forecast physics, the
  remaining-window forecast physics (model and inherited TAF), and the
  observed rest-of-day maximum.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for path in (str(ROOT), str(ROOT / "scripts"), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from stations import station_table  # noqa: E402
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.cross_consensus import (  # noqa: E402
    RADIATION_SHIFT_SIGMA,
    extraterrestrial_radiation_mj,
    forecast_clearness_shift_sigma,
)
from bayes_basket_target.metar_path_state import (  # noqa: E402
    parse_metar_physical_state,
)
import capture_bayes_basket_target_awc_taf_calibration as taf_capture  # noqa: E402

HEAT_START_H = 6   # taf_rotation_state.TAF_PHYSICAL_PEAK_WINDOW_START_HOUR_LOCAL
HEAT_END_H = 15    # taf_rotation_state.TAF_PHYSICAL_PEAK_WINDOW_END_HOUR_LOCAL
P1_HOURS = (8, 10, 12, 14, 16)
HISTORY_DAYS = 180  # native_ensemble_p0.FORECAST_PHYSICS_HISTORY_DAYS
TEMP_DEW = re.compile(r"(?<![A-Z0-9])(M?\d{2})/(M?\d{2}|XX|//)(?![A-Z0-9])")
T_GROUP = re.compile(r"\bT([01])(\d{3})([01])(\d{3})\b")
METAR_SKY = {"FEW": 0.25, "SCT": 0.50, "BKN": 0.875, "OVC": 1.0, "VV": 1.0}


# --------------------------------------------------------------------- solar
def cos_zenith(lat: float, lon: float, when: datetime) -> float:
    """Cosine of the solar zenith angle (NOAA low-precision formulas)."""

    doy = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60.0 + when.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (doy - 1 + (hour - 12.0) / 24.0)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma)
    )
    true_solar_min = hour * 60.0 + eqtime + 4.0 * lon
    hour_angle = math.radians(true_solar_min / 4.0 - 180.0)
    phi = math.radians(lat)
    value = (
        math.sin(phi) * math.sin(decl)
        + math.cos(phi) * math.cos(decl) * math.cos(hour_angle)
    )
    return max(0.0, value)


def solar_profile(lat: float, lon: float, day_start_utc: datetime) -> list[float]:
    """Clear-sky weight per 10 minutes over the local day (144 slots)."""

    return [
        cos_zenith(lat, lon, day_start_utc + timedelta(minutes=10 * i + 5))
        for i in range(144)
    ]


# --------------------------------------------------------------------- METAR
def parse_metar_rows(paths: list[Path]) -> list[dict]:
    seen = set()
    rows = []
    for path in paths:
        with path.open(newline="") as handle:
            for record in csv.DictReader(handle):
                raw = (record.get("metar") or "").strip()
                valid = (record.get("valid") or "").strip()
                if not raw or not valid or (valid, raw) in seen:
                    continue
                seen.add((valid, raw))
                when = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(
                    tzinfo=timezone.utc
                )
                body = raw.upper().split(" RMK ", 1)[0]
                match = TEMP_DEW.search(body)
                if match is None:
                    continue
                token = match.group(1)
                whole = float(-int(token[1:]) if token.startswith("M") else int(token))
                tenths = None
                t_group = T_GROUP.search(raw.upper())
                if t_group is not None:
                    sign = -1.0 if t_group.group(1) == "1" else 1.0
                    tenths = sign * int(t_group.group(2)) / 10.0
                    if abs(tenths - whole) > 0.6:
                        tenths = None
                try:
                    state = parse_metar_physical_state(raw, expected_temperature_c=whole)
                except ContractError:
                    state = None
                rows.append({
                    "t": when, "whole_c": whole, "tenths_c": tenths,
                    "cloud": None if state is None else state.cloud_cover_fraction,
                    "precip": None if state is None else bool(state.precipitation_present),
                    "wind": None if state is None else state.wind_speed_mps,
                    "dew": None if state is None else state.dewpoint_c,
                    "complete": bool(state is not None and state.complete),
                })
    rows.sort(key=lambda r: r["t"])
    return rows


def reported(row: dict, unit: str) -> float:
    """The settlement-scale reading: whole C, or whole F from the T-group."""

    if unit == "F":
        base = row["tenths_c"] if row["tenths_c"] is not None else row["whole_c"]
        return float(round(base * 9.0 / 5.0 + 32.0))
    return float(row["whole_c"])


def to_c(value: float, unit: str) -> float:
    return (value - 32.0) * 5.0 / 9.0 if unit == "F" else value


# --------------------------------------------------------------------- model
def load_model(path: Path) -> dict[datetime, dict]:
    body = json.loads(path.read_text())
    hourly = body["hourly"]
    out = {}
    for index, stamp in enumerate(hourly["time"]):
        when = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
        out[when] = {
            "temp": hourly["temperature_2m_previous_day1"][index],
            "sw": hourly["shortwave_radiation_previous_day1"][index],
            "cloud": hourly["cloud_cover_previous_day1"][index],
            "precip": hourly["precipitation_previous_day1"][index],
            "wind": hourly["wind_speed_10m_previous_day1"][index],
        }
    return out


MODEL_KEYS = ("temp", "sw", "cloud", "precip", "wind")


def model_at(model: Mapping[datetime, dict], when: datetime) -> dict | None:
    """Model values at ``when``, linear between the bracketing UTC hours.

    Only a station on a half-hour zone (VILK, UTC+5:30) ever interpolates.
    """

    floor = when.replace(minute=0, second=0, microsecond=0)
    frac = (when - floor).total_seconds() / 3600.0
    left = model.get(floor)
    if left is None or any(left[k] is None for k in MODEL_KEYS):
        return None
    if frac == 0.0:
        return left
    right = model.get(floor + timedelta(hours=1))
    if right is None or any(right[k] is None for k in MODEL_KEYS):
        return None
    return {k: left[k] * (1.0 - frac) + right[k] * frac for k in MODEL_KEYS}


def model_day(model: Mapping[datetime, dict], day_start: datetime) -> dict | None:
    """The day's 25 hourly model values at local hour marks 0..24."""

    values = []
    for hour in range(25):
        row = model_at(model, day_start + timedelta(hours=hour))
        if row is None:
            return None
        values.append(row)
    return {"hours": values}


GLOBAL_SUFFIXES = ("ecmwf_ifs025", "gfs_global", "icon_global", "gem_global")


def load_global(path: Path | None) -> dict[str, dict[datetime, float]]:
    """Per global model, UTC hour -> day-ahead 2 m temperature."""

    if path is None:
        return {}
    hourly = json.loads(path.read_text())["hourly"]
    times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in hourly["time"]]
    out = {}
    for suffix in GLOBAL_SUFFIXES:
        values = hourly.get(f"temperature_2m_previous_day1_{suffix}")
        if values:
            out[suffix] = {t: v for t, v in zip(times, values) if v is not None}
    return out


def global_day(models: Mapping[str, Mapping[datetime, float]], day_start: datetime) -> dict | None:
    """Mean of the global models' daily maxima and their mean hourly curve."""

    maxima, curves = [], []
    for series in models.values():
        values = []
        for hour in range(25):
            when = day_start + timedelta(hours=hour)
            floor = when.replace(minute=0)
            frac = (when - floor).total_seconds() / 3600.0
            left = series.get(floor)
            right = series.get(floor + timedelta(hours=1)) if frac else left
            if left is None or right is None:
                values = None
                break
            values.append(left * (1.0 - frac) + right * frac)
        if values is None:
            continue
        maxima.append(max(values[:24]))
        curves.append(values)
    if len(maxima) < 3:
        return None
    curve = [sum(c[h] for c in curves) / len(curves) for h in range(25)]
    return {
        "max_c": sum(maxima) / len(maxima),
        "spread_c": (max(maxima) - min(maxima)) / 2.0,
        "models": len(maxima),
        "curve": curve,
    }


def interp(series: list[float], offset_hours: float) -> float:
    left = max(0, min(len(series) - 1, int(math.floor(offset_hours))))
    right = min(len(series) - 1, left + 1)
    frac = offset_hours - left
    return series[left] * (1.0 - frac) + series[right] * frac


# --------------------------------------------------------------------- TAF
def load_awc_tafs(cache: Path) -> dict[str, list[dict]]:
    by_station: dict[str, dict] = defaultdict(dict)
    for path in sorted((cache / "awc_taf").glob("*.json")):
        for taf in json.loads(path.read_text()):
            key = (taf.get("issueTime"), taf.get("rawTAF"))
            station = str(taf.get("icaoId") or "").upper()
            if key not in by_station[station]:
                by_station[station][key] = taf
    out = {}
    for station, items in by_station.items():
        rows = []
        for taf in items.values():
            try:
                avail = taf_capture._parse_utc(taf.get("dbPopTime") or taf.get("issueTime"))
                issue = taf_capture._parse_utc(taf.get("issueTime"))
            except (TypeError, ValueError):
                continue
            rows.append({"avail": avail, "issue": issue, "taf": taf, "source": "AWC"})
        rows.sort(key=lambda r: r["avail"])
        out[station] = rows
    return out


PROB_RE = re.compile(r"^PROB(\d{2})")
WIND_GROUP = re.compile(r"(?<![A-Z0-9])(?:\d{3}|VRB)(\d{2,3})(?:G\d{2,3})?(KT|MPS)(?![A-Z0-9])")


def _list_field(value: str) -> list[str]:
    value = (value or "").strip()
    if not value or value in {"[]", "M"}:
        return []
    return [item.strip(" '\"") for item in value.strip("[]").split(",") if item.strip(" '\"")]


def load_iem_tafs(cache: Path) -> dict[str, list[dict]]:
    """IEM's US TAF rows, reassembled into AWC's decoded shape per issuance."""

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path in sorted((cache / "iem_taf").glob("*.csv")):
        station = path.name.split("_", 1)[0]
        with path.open(newline="") as handle:
            for record in csv.DictReader(handle):
                groups[(station, record["product_id"])].append(record)
    out: dict[str, list[dict]] = defaultdict(list)
    for (station, product), records in groups.items():
        try:
            issue = datetime.strptime(records[0]["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        prevailing, overlays = [], []
        for record in records:
            raw = (record.get("raw") or "").strip().upper()
            try:
                start = datetime.strptime(record["fx_valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            end_raw = (record.get("fx_valid_end") or "").strip()
            end = (
                datetime.strptime(end_raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                if end_raw else None
            )
            try:
                wspd = float(record["sknt"]) if record.get("sknt") not in (None, "", "M") else None
            except ValueError:
                wspd = None
            if wspd is None:
                # IEM leaves ``sknt`` blank for a variable wind (``VRB03KT``);
                # AWC decodes its speed, so read it from the group text.
                wind_match = WIND_GROUP.search(raw)
                if wind_match is not None:
                    wspd = float(wind_match.group(1)) * (
                        1.0 if wind_match.group(2) == "KT" else 1.0 / 0.514444
                    )
            wx = " ".join(_list_field(record.get("presentwx")))
            clouds = [{"cover": cover} for cover in _list_field(record.get("skyc"))]
            prob_match = PROB_RE.match(raw)
            if prob_match or str(record.get("is_tempo")).lower() == "true" or raw.startswith("TEMPO"):
                change = "PROB" if prob_match else "TEMPO"
                if end is None:
                    continue
                overlays.append({
                    "timeFrom": start.isoformat(), "timeTo": end.isoformat(),
                    "fcstChange": change,
                    "probability": int(prob_match.group(1)) if prob_match else None,
                    "wspd": wspd, "wxString": wx or None, "clouds": clouds,
                })
            else:
                change = (
                    None if record.get("ftype") == "Observation"
                    else ("BECMG" if raw.startswith("BECMG") else "FM")
                )
                prevailing.append({
                    "timeFrom": start, "fcstChange": change, "probability": None,
                    "wspd": wspd, "wxString": wx or None, "clouds": clouds,
                })
        if not prevailing:
            continue
        prevailing.sort(key=lambda row: row["timeFrom"])
        valid_end = issue + timedelta(hours=30)
        fcsts = []
        for index, row in enumerate(prevailing):
            stop = prevailing[index + 1]["timeFrom"] if index + 1 < len(prevailing) else valid_end
            if stop <= row["timeFrom"]:
                continue
            fcsts.append({**row, "timeFrom": row["timeFrom"].isoformat(), "timeTo": stop.isoformat()})
        fcsts.extend(overlays)
        out[station].append({
            "avail": issue + timedelta(minutes=5), "issue": issue, "source": "IEM",
            "taf": {"icaoId": station, "issueTime": issue.isoformat(), "rawTAF": "", "fcsts": fcsts},
        })
    for rows in out.values():
        rows.sort(key=lambda r: r["avail"])
    return out


def taf_window_features(taf: Mapping[str, Any], *, start: datetime, end: datetime) -> tuple[float, float, float] | None:
    """``_forecast_features`` over an arbitrary window (same integration)."""

    prevailing, overlays = [], []
    for row in taf.get("fcsts", []):
        if not isinstance(row, Mapping):
            continue
        segment = taf_capture._forecast_segment(row)
        if segment is None:
            continue
        (overlays if segment[2] else prevailing).append(segment)
    cuts = {start, end}
    for seg_start, seg_end, *_ in prevailing + overlays:
        for edge in (seg_start, seg_end):
            if start < edge < end:
                cuts.add(edge)
    edges = sorted(cuts)
    sums = [0.0, 0.0, 0.0]
    total = 0.0
    for left, right in zip(edges, edges[1:]):
        weight = (right - left).total_seconds()
        if weight <= 0:
            continue
        in_force = [seg for seg in prevailing if seg[0] <= left < seg[1]]
        if not in_force:
            continue
        _, _, _, base_cloud, base_precip, base_wind, _ = max(in_force, key=lambda seg: seg[0])
        cloud = float(base_cloud or 0.0)
        precip = float(base_precip or 0.0)
        wind = float(base_wind or 0.0)
        for o_start, o_end, _, o_cloud, o_precip, o_wind, chance in overlays:
            if not (o_start <= left < o_end):
                continue
            if o_cloud is not None:
                cloud = max(cloud, chance * o_cloud + (1.0 - chance) * float(base_cloud or 0.0))
            if o_wind is not None:
                wind = max(wind, chance * o_wind + (1.0 - chance) * float(base_wind or 0.0))
            if o_precip is not None:
                precip = max(precip, o_precip)
        sums[0] += weight * cloud
        sums[1] += weight * precip
        sums[2] += weight * wind
        total += weight
    if total <= 0:
        return None
    return (sums[0] / total, sums[1] / total, sums[2] / total)


def standing_taf(rows: list[dict], cutoff: datetime, window_start: datetime) -> dict | None:
    """The newest TAF available by ``cutoff`` whose validity reaches the window."""

    index = bisect_right([r["avail"] for r in rows], cutoff)
    for candidate in reversed(rows[max(0, index - 6):index]):
        fcsts = candidate["taf"].get("fcsts") or []
        ends = []
        for row in fcsts:
            try:
                ends.append(taf_capture._parse_utc(row.get("timeTo")))
            except (TypeError, ValueError):
                continue
        if ends and max(ends) > window_start:
            return candidate
    return None


def taf_features(candidate: dict, *, event_date: str, tz: str) -> dict | None:
    taf = candidate["taf"]
    try:
        cloud, precip, wind = taf_capture._forecast_features(taf, event_date=event_date, timezone_name=tz)
    except (ContractError, ValueError, TypeError):
        return None
    tx = None
    raw = str(taf.get("rawTAF") or "")
    if raw:
        start, end = taf_capture._event_window(event_date, tz)
        try:
            tx, _ = taf_capture._tx(raw, issue_time=candidate["issue"], start=start, end=end)
        except (ContractError, ValueError):
            tx = None
    return {
        "source": candidate["source"], "issue": candidate["issue"].isoformat(),
        "avail": candidate["avail"].isoformat(), "cloud": cloud, "precip": precip,
        "wind": wind, "tx_c": tx,
    }


# --------------------------------------------------------------------- build
def build(cache: Path, only: set[str] | None = None) -> None:
    table = {
        station: meta for station, meta in station_table().items()
        if not only or station in only
    }
    awc = load_awc_tafs(cache)
    iem = load_iem_tafs(cache)
    derived = cache / "derived"
    derived.mkdir(exist_ok=True)
    days_out = (derived / "days.jsonl").open("w")
    p1_out = (derived / "p1.jsonl").open("w")
    counts = defaultdict(int)
    for station, meta in table.items():
        tz = ZoneInfo(meta["timezone"])
        unit = meta["unit"]
        lat, lon = meta["latitude"], meta["longitude"]
        model_path = next(iter(sorted((cache / "model").glob(f"{station}_*.json"))), None)
        metar_paths = sorted((cache / "metar").glob(f"{station}_*.csv"))
        if model_path is None or not metar_paths:
            print("skip", station, flush=True)
            continue
        model = load_model(model_path)
        global_models = load_global(
            next(iter(sorted((cache / "global").glob(f"{station}_*.json"))), None)
        )
        metars = parse_metar_rows(metar_paths)
        by_day: dict[str, list[dict]] = defaultdict(list)
        for row in metars:
            by_day[row["t"].astimezone(tz).date().isoformat()].append(row)
        tafs = sorted(awc.get(station, []) + iem.get(station, []), key=lambda r: r["avail"])
        radiation_by_day: dict[str, float] = {}
        day_models: dict[str, dict] = {}
        all_days = sorted({(t.astimezone(tz)).date() for t in model})
        for local_day in all_days:
            start = datetime.combine(local_day, wall_time.min, tzinfo=tz).astimezone(timezone.utc)
            day = model_day(model, start)
            if day is None:
                continue
            day_models[local_day.isoformat()] = day
            radiation_by_day[local_day.isoformat()] = sum(
                h["sw"] for h in day["hours"][1:]
            ) * 3600.0 / 1e6
        radiation_days = sorted(radiation_by_day)
        for event_date in sorted(by_day):
            reports = by_day[event_date]
            day = day_models.get(event_date)
            if day is None or len(reports) < 18:
                continue
            local_day = date.fromisoformat(event_date)
            day_start = datetime.combine(local_day, wall_time.min, tzinfo=tz).astimezone(timezone.utc)
            local_hours = [r["t"].astimezone(tz).hour for r in reports]
            if any(not any(lo <= h < lo + 2 for h in local_hours) for lo in range(8, 18, 2)):
                counts["incomplete_day"] += 1
                continue
            truth_rep = max(reported(r, unit) for r in reports)
            glob = global_day(global_models, day_start) if global_models else None
            temps = [h["temp"] for h in day["hours"]]
            gtemps = glob["curve"] if glob else None
            clouds = [h["cloud"] / 100.0 for h in day["hours"]]
            winds = [h["wind"] / 3.6 for h in day["hours"]]
            precs = [h["precip"] for h in day["hours"]]
            model_max = max(temps[:24])
            # The P0 radiation term, exactly as served: clearness form over
            # the station's own 180 days strictly before the event day.
            position = radiation_days.index(event_date) if event_date in radiation_by_day else None
            history = [
                (d, radiation_by_day[d])
                for d in radiation_days[max(0, position - HISTORY_DAYS):position]
            ] if position is not None else []
            shift_sigma, receipt = forecast_clearness_shift_sigma(
                radiation_sum=radiation_by_day.get(event_date),
                event_date=event_date,
                latitude_deg=lat,
                station_history=history,
                shift_sigma_per_unit=RADIATION_SHIFT_SIGMA,
            )
            heat = range(HEAT_START_H, HEAT_END_H)
            model_cloud = sum(clouds[h] for h in heat) / len(heat)
            model_wind = sum(winds[h] for h in heat) / len(heat)
            model_wet = sum(1.0 for h in range(HEAT_START_H + 1, HEAT_END_H + 1) if precs[h] >= 0.1) / len(heat)
            window_start = datetime.combine(local_day, wall_time(HEAT_START_H), tzinfo=tz).astimezone(timezone.utc)
            taf_rows = {}
            for label, cutoff in (
                ("eve", datetime.combine(local_day - timedelta(days=1), wall_time(18), tzinfo=tz).astimezone(timezone.utc)),
                ("morning", window_start),
            ):
                candidate = standing_taf(tafs, cutoff, window_start)
                taf_rows[label] = None if candidate is None else taf_features(candidate, event_date=event_date, tz=meta["timezone"])
            days_out.write(json.dumps({
                "station": station, "date": event_date, "unit": unit, "lat": lat,
                "truth_rep": truth_rep, "truth_c": to_c(truth_rep, unit),
                "model_max_c": model_max,
                "global_max_c": None if glob is None else glob["max_c"],
                "global_spread_c": None if glob is None else glob["spread_c"],
                "global_models": 0 if glob is None else glob["models"],
                "radiation_sum": radiation_by_day.get(event_date),
                "kt_state": receipt.get("state"),
                "kt": receipt.get("clearness_index"),
                "kt_z": receipt.get("standardised"),
                "kt_hist_mean": receipt.get("history_clearness_mean"),
                "kt_hist_sd": receipt.get("history_clearness_sd"),
                "p0_shift_sigma": shift_sigma,
                "model_cloud": model_cloud, "model_wet": model_wet, "model_wind": model_wind,
                "taf_eve": taf_rows["eve"], "taf_morning": taf_rows["morning"],
            }) + "\n")
            counts["days"] += 1
            # P1 decision hours.
            weights = solar_profile(lat, lon, day_start)
            total_weight = sum(weights) or 1.0
            for hour in P1_HOURS:
                decision = day_start + timedelta(hours=hour)
                prefix = [r for r in reports if r["t"] <= decision]
                rest = [r for r in reports if r["t"] > decision]
                if len(prefix) < 3 or not rest:
                    continue
                running_rep = max(reported(r, unit) for r in prefix)
                rest_rep = max(reported(r, unit) for r in rest)

                def obs_c(row: dict) -> float:
                    return row["tenths_c"] if row["tenths_c"] is not None else row["whole_c"]

                def offset(row: dict) -> float:
                    return (row["t"] - day_start).total_seconds() / 3600.0

                recent = [r for r in prefix if (decision - r["t"]).total_seconds() <= 3 * 3600]
                innov_raw = (
                    sum(obs_c(r) - interp(temps, offset(r)) for r in recent) / len(recent)
                    if recent else None
                )
                latest = prefix[-1]
                now_gap_raw = obs_c(latest) - interp(temps, offset(latest))
                rem_max_raw = max(temps[hour:25])
                if gtemps is not None:
                    innov_raw_g = (
                        sum(obs_c(r) - interp(gtemps, offset(r)) for r in recent) / len(recent)
                        if recent else None
                    )
                    now_gap_raw_g = obs_c(latest) - interp(gtemps, offset(latest))
                    rem_max_raw_g = max(gtemps[hour:25])
                else:
                    innov_raw_g = now_gap_raw_g = rem_max_raw_g = None
                # The METAR physical state now: newest complete report within
                # two hours (metar_path_state's carry rule).
                state = next(
                    (r for r in reversed(prefix)
                     if r["complete"] and (decision - r["t"]).total_seconds() <= 7200),
                    None,
                )
                # Elapsed daylight in the heating window, solar-weighted.
                el = [
                    r for r in prefix
                    if r["cloud"] is not None and offset(r) >= HEAT_START_H
                ]
                el_w = [cos_zenith(lat, lon, r["t"]) for r in el]
                wsum = sum(el_w)
                if wsum > 0:
                    obs_cloud_el = sum(w * r["cloud"] for w, r in zip(el_w, el)) / wsum
                    mod_cloud_el = sum(w * interp(clouds, offset(r)) for w, r in zip(el_w, el)) / wsum
                    obs_wet_el = sum(w * (1.0 if r["precip"] else 0.0) for w, r in zip(el_w, el)) / wsum
                    mod_wet_el = sum(
                        w * (1.0 if precs[min(24, int(math.floor(offset(r))) + 1)] >= 0.1 else 0.0)
                        for w, r in zip(el_w, el)
                    ) / wsum
                    obs_wind_el = sum(w * (r["wind"] or 0.0) for w, r in zip(el_w, el)) / wsum
                    mod_wind_el = sum(w * interp(winds, offset(r)) for w, r in zip(el_w, el)) / wsum
                else:
                    obs_cloud_el = mod_cloud_el = obs_wet_el = mod_wet_el = None
                    obs_wind_el = mod_wind_el = None
                f_elapsed = sum(weights[: hour * 6]) / total_weight
                rem_hours = list(range(hour, max(hour + 1, 18)))
                rem_w = [
                    cos_zenith(lat, lon, day_start + timedelta(hours=h, minutes=30))
                    for h in rem_hours
                ]
                rw = sum(rem_w) or 1.0
                mod_cloud_rem = sum(w * clouds[h] for w, h in zip(rem_w, rem_hours)) / rw
                standing = standing_taf(tafs, decision, decision)
                taf_rem = None
                taf_tx = None
                if standing is not None:
                    rem_start = decision
                    rem_end = day_start + timedelta(hours=max(hour + 1, 18))
                    taf_rem = taf_window_features(standing["taf"], start=rem_start, end=rem_end)
                    full = taf_features(standing, event_date=event_date, tz=meta["timezone"])
                    taf_tx = None if full is None else full["tx_c"]
                p1_out.write(json.dumps({
                    "station": station, "date": event_date, "unit": unit, "hour": hour,
                    "running_rep": running_rep, "running_c": to_c(running_rep, unit),
                    "rest_rep": rest_rep, "rest_c": to_c(rest_rep, unit),
                    "truth_rep": truth_rep,
                    "innov_raw": innov_raw, "now_gap_raw": now_gap_raw,
                    "rem_max_raw": rem_max_raw, "model_max_c": model_max,
                    "innov_raw_g": innov_raw_g, "now_gap_raw_g": now_gap_raw_g,
                    "rem_max_raw_g": rem_max_raw_g,
                    "global_max_c": None if glob is None else glob["max_c"],
                    "state_cloud": None if state is None else state["cloud"],
                    "state_wet": None if state is None else (1.0 if (prefix[-1]["precip"]) else 0.0),
                    "state_wind": None if state is None else state["wind"],
                    "state_dd": None if state is None or state["dew"] is None else obs_c(latest) - state["dew"],
                    "obs_cloud_el": obs_cloud_el, "mod_cloud_el": mod_cloud_el,
                    "obs_wet_el": obs_wet_el, "mod_wet_el": mod_wet_el,
                    "obs_wind_el": obs_wind_el, "mod_wind_el": mod_wind_el,
                    "f_elapsed": f_elapsed, "mod_cloud_rem": mod_cloud_rem,
                    "taf_rem": taf_rem, "taf_tx_c": taf_tx,
                    "taf_source": None if standing is None else standing["source"],
                }) + "\n")
                counts["p1"] += 1
        print("built", station, dict(counts), flush=True)
    days_out.close()
    p1_out.close()
    print(json.dumps(counts))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--stations", default="", help="comma list (debug)")
    args = parser.parse_args()
    only = {s.strip().upper() for s in args.stations.split(",") if s.strip()}
    build(Path(args.cache_dir), only or None)


if __name__ == "__main__":
    main()
