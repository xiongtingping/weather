#!/usr/bin/env python3
"""The same cases from the production SSOT, on the served P0 roots (read-only).

Run on the machine that holds the SSOT; writes ``<out>/derived/days.jsonl``
and ``<out>/derived/p1.jsonl`` in ``build_cases.py``'s schema plus the served
root's fields, so ``measure.py --p0-source production --cache-dir <out>``
scores the physical line against production's own temperature line:

* the P0 root is the newest COMMITTED P0 envelope at each cutoff -- its
  served mean and spread (``distribution_sigma_c`` on its own ladder), its
  ``model_center_c`` and the radiation receipt it was served with
  (``synoptic_cooling_json``: Kt z, shift);
* TAFs are the forward captures, re-derived from the raw archive with
  ``_forecast_features`` (the stored capture fields carry the pre-09-23
  reading, see ``taf_metar_weather_stage_20260923``);
* METARs are the AWC lane of ``reported_trajectory_observations``, parsed with
  ``parse_metar_physical_state``;
* the model's physics is ``calibration_station_forecast_physics_version`` as
  of the cutoff -- daily values only (production captures no hourly cloud), so
  the elapsed and remaining windows read the day's mean;
* the hourly curve is the root's ``conditional_future_temperature_curve_json``;
* truth is the latest ACCEPTED revision of ``calibration_station_daily_high_truth``.

Not executed where it was written (the SSOT was not reachable); every query
is one an existing research script already runs against the same tables.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for path in (str(ROOT), str(ROOT / "scripts"), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

import capture_bayes_basket_target_awc_taf_calibration as taf_capture  # noqa: E402
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.cross_consensus import distribution_sigma_c  # noqa: E402
from bayes_basket_target.materialization import _bucket_centers_c  # noqa: E402
from bayes_basket_target.metar_four_layer import curve_points  # noqa: E402
from bayes_basket_target.metar_path_state import parse_metar_physical_state  # noqa: E402
from bayes_basket_target.native_ensemble_training_facts import (  # noqa: E402
    taf_restatement_is_current,
)
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402
from build_cases import (  # noqa: E402
    HEAT_START_H, P1_HOURS, cos_zenith, interp, solar_profile, taf_window_features,
)


def _utc(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _unit(ordinals: dict) -> str:
    return "F" if any("°F" in str(name) for name in ordinals) else "C"


def _reported(value_c: float, unit: str) -> float:
    return float(round(value_c * 9.0 / 5.0 + 32.0)) if unit == "F" else float(round(value_c))


def _to_c(value: float, unit: str) -> float:
    return (value - 32.0) * 5.0 / 9.0 if unit == "F" else value


def load_roots(conn, station: str, event_date: str) -> list[tuple[datetime, str]]:
    rows = conn.execute(
        "SELECT decision_at_utc, checkpoint_id FROM basket_probability_envelope"
        " WHERE station_id=? AND event_date=? AND stage='P0' AND state='COMMITTED'",
        (station, event_date),
    ).fetchall()
    return sorted((t, str(c)) for a, c in rows if (t := _utc(a)) is not None)


def root_view(conn, checkpoint_id: str, cache: dict) -> dict | None:
    if checkpoint_id in cache:
        return cache[checkpoint_id]
    row = conn.execute(
        "SELECT probabilities_json, bucket_ordinals_json, lineage_json"
        " FROM basket_probability_envelope WHERE checkpoint_id=? LIMIT 1",
        (checkpoint_id,),
    ).fetchone()
    view = None
    if row:
        probs = {str(k): float(v) for k, v in json.loads(row[0]).items()}
        ords = {str(k): int(v) for k, v in json.loads(row[1]).items()}
        lineage = json.loads(row[2]) if row[2] else {}
        axis = _bucket_centers_c(lineage, bucket_ordinals=ords)
        if axis:
            order, centers = axis
            weights = [probs.get(b, 0.0) for b in order]
            mass = sum(weights)
            receipt = lineage.get("synoptic_cooling_json")
            receipt = json.loads(receipt) if isinstance(receipt, str) and receipt else (receipt or {})
            try:
                c0 = float(lineage.get("model_center_c"))
            except (TypeError, ValueError):
                c0 = None
            if mass > 0 and c0 is not None:
                view = {
                    "unit": _unit(ords),
                    "mu": sum(w * c for w, c in zip(weights, centers)) / mass,
                    "sigma": distribution_sigma_c([w / mass for w in weights], centers),
                    "c0": c0,
                    "radiation_c": float(receipt.get("shift_c") or 0.0),
                    "kt_z": receipt.get("standardised"),
                    "kt": receipt.get("clearness_index"),
                    "kt_hist_sd": receipt.get("history_clearness_sd"),
                    "kt_state": receipt.get("state"),
                    "curve": curve_points(lineage.get("conditional_future_temperature_curve_json")),
                }
    cache[checkpoint_id] = view
    return view


def physics_as_of(conn, station: str, event_date: str, cutoff: datetime) -> dict | None:
    rows = conn.execute(
        "SELECT captured_at_utc, cloudcover_mean, precipitation_sum, windspeed_10m_max, capture_mode"
        " FROM calibration_station_forecast_physics_version WHERE station_id=? AND local_date=?",
        (station, event_date),
    ).fetchall()
    known = [
        (t, r) for r in rows
        if str(r[4]) == "forward" and (t := _utc(r[0])) is not None and t <= cutoff
        and r[1] is not None
    ]
    if not known:
        return None
    _, row = max(known, key=lambda item: item[0])
    return {
        "cloud": float(row[1]) / 100.0,
        "wet": 1.0 if float(row[2] or 0.0) >= 0.5 else 0.0,
        "wind": float(row[3] or 0.0) / 3.6,
    }


def load_tafs(conn, archive_dir: Path) -> dict[tuple[str, str], list[dict]]:
    archive = {}
    for path in sorted(glob.glob(str(archive_dir / "*.jsonl"))):
        for line in open(path):
            try:
                body = json.loads(line)
            except ValueError:
                continue
            row = body.get("awc_row") or {}
            sha = body.get("raw_taf_sha256")
            if sha and (sha not in archive or (not archive[sha].get("fcsts") and row.get("fcsts"))):
                archive[sha] = row
    fetched = conn.execute(
        "SELECT station_id, event_date, settlement_timezone, issued_at_utc,"
        " provider_available_at_utc, received_at_utc, tx_c, source_object_sha256, revision_id"
        " FROM calibration_forward_taf_capture"
    ).fetchall()
    ids = defaultdict(list)
    for row in fetched:
        ids[(row[0], row[1])].append(str(row[8]))
    out = defaultdict(list)
    for station, day, tz, issued, avail, received, tx, sha, rid in fetched:
        if not taf_restatement_is_current(rid, ids[(station, day)]):
            continue
        row = archive.get(sha)
        when = _utc(avail or received or issued)
        if not row or not row.get("fcsts") or when is None:
            continue
        out[(str(station), str(day))].append({
            "avail": when, "taf": row, "tz": tz,
            "tx_c": None if tx is None else float(tx),
        })
    for rows in out.values():
        rows.sort(key=lambda r: r["avail"])
    return out


def taf_at(rows: list[dict], cutoff: datetime, event_date: str) -> dict | None:
    known = [r for r in rows if r["avail"] <= cutoff]
    if not known:
        return None
    pick = known[-1]
    try:
        cloud, precip, wind = taf_capture._forecast_features(
            pick["taf"], event_date=event_date, timezone_name=pick["tz"]
        )
    except (ContractError, ValueError, TypeError):
        return None
    return {
        "source": "SSOT", "avail": pick["avail"].isoformat(),
        "cloud": cloud, "precip": precip, "wind": wind, "tx_c": pick["tx_c"],
        "_row": pick,
    }


def load_metars(archive, station: str, start: datetime, end: datetime) -> list[dict]:
    rows = []
    for obs_id, valid, temp, raw in archive.execute(
        "SELECT observation_id, valid_at, reported_temperature_c, raw_text"
        " FROM reported_trajectory_observations"
        " WHERE station_id=? AND valid_at>=? AND valid_at<? AND source_lane='awc'"
        " AND correction_suspected=0 AND reported_temperature_c IS NOT NULL",
        (station, start.date().isoformat(), (end.date() + timedelta(days=1)).isoformat()),
    ):
        when = _utc(valid)
        if when is None or not (start <= when < end):
            continue
        try:
            state = parse_metar_physical_state(str(raw or ""), expected_temperature_c=round(float(temp)))
        except ContractError:
            state = None
        rows.append({
            "t": when, "temp_c": float(temp),
            "cloud": None if state is None else state.cloud_cover_fraction,
            "precip": None if state is None else bool(state.precipitation_present),
            "wind": None if state is None else state.wind_speed_mps,
            "dew": None if state is None else state.dewpoint_c,
            "complete": bool(state is not None and state.complete),
        })
    rows.sort(key=lambda r: r["t"])
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db", default=str(ROOT / "data" / "bayes_basket_target_ssot.db"))
    parser.add_argument("--archive-db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument(
        "--taf-archive-dir", default=str(ROOT / "data" / "runtime" / "taf_raw_archive")
    )
    parser.add_argument("--from", dest="window_from", default="2026-08-20")
    parser.add_argument("--through", dest="window_through", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.target_db}?mode=ro", uri=True, timeout=30)
    archive = sqlite3.connect(f"file:{args.archive_db}?mode=ro", uri=True, timeout=30)
    zones = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"
    ).fetchall())
    truth = {}
    for station, day, value in conn.execute(
        "SELECT station_id, local_date, official_tmax_c FROM calibration_station_daily_high_truth"
        " WHERE qc_status='ACCEPTED' AND local_date>=? AND local_date<=? ORDER BY revision_no",
        (args.window_from, args.window_through),
    ):
        truth[(str(station), str(day))] = float(value)
    tafs = load_tafs(conn, Path(args.taf_archive_dir))
    derived = Path(args.out) / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    days_out = (derived / "days.jsonl").open("w")
    p1_out = (derived / "p1.jsonl").open("w")
    cache: dict = {}
    counts = defaultdict(int)
    for (station, event_date), truth_c in sorted(truth.items()):
        tz_name = zones.get(station)
        if not tz_name:
            counts["no_zone"] += 1
            continue
        tz = ZoneInfo(tz_name)
        local_day = date.fromisoformat(event_date)
        day_start = datetime.combine(local_day, wall_time.min, tzinfo=tz).astimezone(timezone.utc)
        morning = datetime.combine(local_day, wall_time(HEAT_START_H), tzinfo=tz).astimezone(timezone.utc)
        eve = datetime.combine(local_day - timedelta(days=1), wall_time(18), tzinfo=tz).astimezone(timezone.utc)
        roots = load_roots(conn, station, event_date)
        chosen = [c for t, c in roots if t <= morning]
        if not chosen:
            counts["no_root"] += 1
            continue
        view = root_view(conn, chosen[-1], cache)
        physics = physics_as_of(conn, station, event_date, morning)
        if view is None or physics is None:
            counts["no_view_or_physics"] += 1
            continue
        unit = view["unit"]
        lat = float(station_coordinates(station)["latitude"])
        lon = float(station_coordinates(station)["longitude"])
        truth_rep = _reported(truth_c, unit)
        station_tafs = tafs.get((station, event_date), [])
        taf_rows = {}
        for label, cutoff in (("eve", eve), ("morning", morning)):
            feature = taf_at(station_tafs, cutoff, event_date)
            if feature is not None:
                feature.pop("_row", None)
            taf_rows[label] = feature
        curve = view["curve"]
        hourly = None
        if curve:
            hourly = []
            for hour in range(25):
                when = day_start + timedelta(hours=hour)
                before = [p for p in curve if p[0] <= when]
                after = [p for p in curve if p[0] >= when]
                if not before or not after:
                    hourly = None
                    break
                (t0, v0), (t1, v1) = before[-1], after[0]
                span = (t1 - t0).total_seconds()
                hourly.append(v0 if span <= 0 else v0 + (v1 - v0) * (when - t0).total_seconds() / span)
        days_out.write(json.dumps({
            "station": station, "date": event_date, "unit": unit, "lat": lat,
            "truth_rep": truth_rep, "truth_c": _to_c(truth_rep, unit),
            "p0_mu_c": view["mu"], "p0_sigma_c": view["sigma"], "p0_c0": view["c0"],
            "p0_radiation_c": view["radiation_c"],
            "global_max_c": None if hourly is None else max(hourly[:24]),
            "kt_state": view["kt_state"], "kt": view["kt"], "kt_z": view["kt_z"],
            "kt_hist_sd": view["kt_hist_sd"],
            "model_cloud": physics["cloud"], "model_wet": physics["wet"], "model_wind": physics["wind"],
            "taf_eve": taf_rows["eve"], "taf_morning": taf_rows["morning"],
        }) + "\n")
        counts["days"] += 1
        if hourly is None:
            continue
        reports = load_metars(archive, station, day_start, day_start + timedelta(days=1))
        if len(reports) < 8:
            continue
        weights = solar_profile(lat, lon, day_start)
        total_weight = sum(weights) or 1.0

        def offset(row: dict) -> float:
            return (row["t"] - day_start).total_seconds() / 3600.0

        for hour in P1_HOURS:
            decision = day_start + timedelta(hours=hour)
            prefix = [r for r in reports if r["t"] <= decision]
            rest = [r for r in reports if r["t"] > decision]
            if len(prefix) < 3 or not rest:
                continue
            running_rep = max(_reported(r["temp_c"], unit) for r in prefix)
            rest_rep = max(_reported(r["temp_c"], unit) for r in rest)
            recent = [r for r in prefix if (decision - r["t"]).total_seconds() <= 3 * 3600]
            latest = prefix[-1]
            state = next(
                (r for r in reversed(prefix)
                 if r["complete"] and (decision - r["t"]).total_seconds() <= 7200),
                None,
            )
            el = [r for r in prefix if r["cloud"] is not None and offset(r) >= HEAT_START_H]
            el_w = [cos_zenith(lat, lon, r["t"]) for r in el]
            wsum = sum(el_w)
            standing = taf_at(station_tafs, decision, event_date)
            taf_rem = None
            if standing is not None:
                taf_rem = taf_window_features(
                    standing["_row"]["taf"], start=decision,
                    end=day_start + timedelta(hours=max(hour + 1, 18)),
                )
            p1_out.write(json.dumps({
                "station": station, "date": event_date, "unit": unit, "hour": hour,
                "running_rep": running_rep, "running_c": _to_c(running_rep, unit),
                "rest_rep": rest_rep, "rest_c": _to_c(rest_rep, unit), "truth_rep": truth_rep,
                "innov_raw_g": (
                    sum(r["temp_c"] - interp(hourly, offset(r)) for r in recent) / len(recent)
                    if recent else None
                ),
                "now_gap_raw_g": latest["temp_c"] - interp(hourly, offset(latest)),
                "rem_max_raw_g": max(hourly[hour:25]),
                "global_max_c": max(hourly[:24]),
                "state_cloud": None if state is None else state["cloud"],
                "state_wet": None if state is None else (1.0 if latest["precip"] else 0.0),
                "state_wind": None if state is None else state["wind"],
                "state_dd": None if state is None or state["dew"] is None else latest["temp_c"] - state["dew"],
                "obs_cloud_el": sum(w * r["cloud"] for w, r in zip(el_w, el)) / wsum if wsum else None,
                "mod_cloud_el": physics["cloud"] if wsum else None,
                "obs_wet_el": sum(w * (1.0 if r["precip"] else 0.0) for w, r in zip(el_w, el)) / wsum if wsum else None,
                "mod_wet_el": physics["wet"] if wsum else None,
                "obs_wind_el": sum(w * (r["wind"] or 0.0) for w, r in zip(el_w, el)) / wsum if wsum else None,
                "mod_wind_el": physics["wind"] if wsum else None,
                "f_elapsed": sum(weights[: hour * 6]) / total_weight,
                "mod_cloud_rem": physics["cloud"],
                "taf_rem": taf_rem,
                "taf_tx_c": None if standing is None else standing["tx_c"],
                "taf_source": None if standing is None else "SSOT",
            }) + "\n")
            counts["p1"] += 1
    days_out.close()
    p1_out.close()
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
