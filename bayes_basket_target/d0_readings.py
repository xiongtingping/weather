"""The D0 observation model, computed in the background (user 2026-10-15
「后台计算，前端只读，保留历史数据」).

Every station inside its local 07..19 window gets one reading per run: the
official deterministic baseline (median IFS HRES / GFS / AIFS single Tmax
knowable by local midnight) plus the stored ridge fit applied to the
day's METAR, the Open-Meteo day-ahead hourly curve (``*_previous_day1``,
the research's own forecast columns -- temperature, total cloud,
shortwave, precipitation), JAXA Himawari sunshine and cloud and GSMaP rain,
with the feature definitions of docs/research/d0_obs_model_20261015/build_d0.py.

Writes ``data/board_readings/d0_latest.json`` (what the board shows) and
appends to ``data/board_readings/d0_history/<event date>.jsonl`` whenever a
station-day's reading changes.  A reader: the SSOT and archive read-only,
one Open-Meteo call per station-day (charged to the ``d0_readings`` lane,
cached on disk), files under data/ only.  Display only; enters no layer.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import ssl
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import station_day_board as board

ROOT = Path(__file__).resolve().parents[1]
MODEL_PATHS = (ROOT / "data" / "runtime" / "d0_refit" / "d0_obs_model_v1.json",
               ROOT / "config" / "ui" / "d0_obs_model_v1.json")
OUT_DIR = ROOT / "data" / "board_readings"
LATEST_PATH = OUT_DIR / "d0_latest.json"
HISTORY_DIR = OUT_DIR / "d0_history"
FORECAST_DIR = ROOT / "data" / "runtime" / "d0_readings" / "forecast"
OFFICIAL = ("ECMWF_IFS_HRES_NATIVE_POINT", "NOAA_GFS_025_NATIVE_POINT", "ECMWF_AIFS_SINGLE_POINT")
QUOTA_LANE = "d0_readings"
FORECAST_FIELDS = ("temperature_2m_previous_day1", "cloud_cover_previous_day1",
                   "shortwave_radiation_previous_day1", "precipitation_previous_day1")
PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
FORECAST_KEEP_DAYS = 3


def load_model() -> dict[str, Any] | None:
    for path in MODEL_PATHS:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def baseline(conn, station: str, day: str, zone: ZoneInfo) -> tuple[float | None, int]:
    midnight = board._iso(datetime.combine(date.fromisoformat(day), dtime(0), zone))
    vals = []
    for system in OFFICIAL:
        row = conn.execute(
            "SELECT forecast_tmax_c FROM calibration_regional_point_run WHERE ensemble_system_id=? AND station_id=?"
            " AND target_local_date=? AND knowable_at_utc<=? AND forecast_tmax_c IS NOT NULL"
            " ORDER BY run_init_utc DESC LIMIT 1", (system, station, day, midnight)).fetchone()
        if row:
            vals.append(float(row[0]))
    if not vals:
        return None, 0
    vals.sort()
    mid = len(vals) // 2
    return (vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2), len(vals)


def forecast_curve(station: str, lat: float, lon: float, tz: str, day: str) -> dict[str, list[float | None]] | None:
    """The day's 24 hourly day-ahead values per field (stamp h = the hour
    ending h:00), fetched once per station-day and kept on disk."""

    path = FORECAST_DIR / day / f"{station}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    from urllib.parse import urlencode  # noqa: PLC0415
    from urllib.request import Request, urlopen  # noqa: PLC0415

    from . import open_meteo_quota as quota  # noqa: PLC0415

    quota.charge(QUOTA_LANE, 1)
    query = urlencode({"latitude": lat, "longitude": lon, "timezone": tz, "start_date": day, "end_date": day,
                       "hourly": ",".join(FORECAST_FIELDS)})
    try:
        import certifi  # noqa: PLC0415

        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    with urlopen(Request(f"{PREVIOUS_RUNS}?{query}", headers={"User-Agent": "weather-d0-readings"}),
                 timeout=30, context=context) as resp:  # noqa: S310 -- fixed https host
        hourly = json.loads(resp.read().decode("utf-8")).get("hourly") or {}
    curve = {"temp": hourly.get(FORECAST_FIELDS[0]) or [], "cloud": hourly.get(FORECAST_FIELDS[1]) or [],
             "sw": hourly.get(FORECAST_FIELDS[2]) or [], "pr": hourly.get(FORECAST_FIELDS[3]) or []}
    if len(curve["temp"]) != 24 or any(v is None for v in curve["temp"]):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(curve), encoding="utf-8")
    os.replace(tmp, path)
    return curve


def compute(model: Mapping[str, Any], station: str, zone: ZoneInfo, now: datetime, axis: Sequence[Mapping[str, Any]],
            base: float, n_base: int, metar: Sequence[Mapping[str, Any]], fc: Mapping[str, Sequence[Any]],
            sat_sw: Sequence[Any] | None, sat_cloud: Sequence[Any] | None,
            rain: Mapping[str, Any] | None) -> dict[str, Any]:
    """One reading at ``now``.  ``sat_sw``/``sat_cloud`` are indexed by the
    forecast's stamp (h = hour ending h:00; JAXA only, else None) and
    ``rain`` by the same stamp as "HH" (GSMaP); the features follow
    build_d0.py: sunshine summed over stamps 6..h, cloud from the newest scan
    of the last three stamps, rain over the last three stamps."""

    h = now.astimezone(zone).hour
    band_name, band = next(((n, b) for n, b in model["bands"].items() if b["hours"][0] <= h < b["hours"][1]),
                           (None, None))
    if band is None:
        return {"state": "outside_hours"}
    obs = [m for m in metar if m.get("temp_c") is not None and (board._utc(m["valid_at_utc"]) or now) <= now]
    if not obs:
        return {"state": "no_reports"}
    cur = obs[-1]
    if now - board._utc(cur["valid_at_utc"]) > timedelta(hours=2):
        return {"state": "stale_report"}
    high = max(m["temp_c"] for m in obs)
    two_h = [m for m in obs if board._utc(m["valid_at_utc"]) >= now - timedelta(hours=2, minutes=10)]
    momentum = 0.0
    if len(two_h) > 1:
        span = (board._utc(two_h[-1]["valid_at_utc"]) - board._utc(two_h[0]["valid_at_utc"])).total_seconds() / 3600
        momentum = (two_h[-1]["temp_c"] - two_h[0]["temp_c"]) / max(0.5, span)
    fc_t = [float(v) for v in fc["temp"]]
    fh = min(23, max(1, h))
    fc_now = fc_t[fh - 1] + (fc_t[fh] - fc_t[fh - 1]) * 0.5
    fc_peak_h = max(range(24), key=lambda k: fc_t[k])
    num = lambda seq, k: None if seq is None or k >= len(seq) or seq[k] is None else float(seq[k])  # noqa: E731
    sun_obs = sun_fc = 0.0
    sun_n = 0
    for k in range(6, h + 1):
        s, f = num(sat_sw, k), num(fc.get("sw"), k)
        if s is not None and f is not None:
            sun_obs += s; sun_fc += f; sun_n += 1
    sun_pct = None if sun_n < 2 or sun_fc < 100 else max(-0.8, min(0.8, (sun_obs - sun_fc) / sun_fc))
    cloud_gap = None
    for k in range(h, max(-1, h - 3), -1):
        c = (sat_cloud or [None] * 24)[k] if sat_cloud and k < len(sat_cloud) else None
        fcl = num(fc.get("cloud"), k)
        if c and c.get("cover") is not None and fcl is not None:
            eff = c["cover"] * (1 - math.exp(-(c.get("optical_thickness") or 0.0) / 10.0))
            cloud_gap = eff - fcl / 100.0
            break
    rain_gap = None
    got = [(k, (rain or {}).get(f"{k:02d}")) for k in range(max(0, h - 2), h + 1)]
    got = [(k, v) for k, v in got if v and v.get("mm") is not None]
    if got:
        rain_gap = math.log1p(sum(v["mm"] for _k, v in got)) - math.log1p(
            sum(num(fc.get("pr"), k) or 0.0 for k, _v in got))
    feats = {
        "t_minus_fc": cur["temp_c"] - fc_now,
        "warmed_gap": (cur["temp_c"] - obs[0]["temp_c"]) - (fc_now - fc_t[max(0, h - 7)]),
        "momentum": momentum, "high_minus_base": high - base, "fc_rise": max(0.0, max(fc_t[fh:]) - fc_now),
        "dew_dep": None if cur.get("dew_c") is None else cur["temp_c"] - cur["dew_c"], "dew_minus_fc_dew": None,
        "wind": cur.get("wind_mps"), "cloud_low": cur.get("cloud_cover"),
        "precip_now": float(bool(cur.get("precip"))), "precip_any": float(any(m.get("precip") for m in obs)),
        "sun_pct": sun_pct, "cloud_gap": cloud_gap, "rain_gap": rain_gap, "hours_to_peak": fc_peak_h - h,
    }
    x, names = [], []
    flagged = set(model["missing_indicator_after"])
    for c in model["numeric"]:
        v = feats.get(c)
        x.append(0.0 if v is None else float(v)); names.append(c)
        if c in flagged:
            x.append(1.0 if v is None else 0.0); names.append(c + "_missing")
    nf = band["nf"]
    xs = [(xi - band["mu"][i]) / band["sd"][i] for i, xi in enumerate(x[:nf])]
    beta = band["beta"]
    parts = {names[i]: beta[i] * xs[i] for i in range(nf)}
    station_effect = beta[nf + model["stations"].index(station)] if station in model["stations"] else 0.0
    centre = base + sum(parts.values()) + station_effect + beta[-1]
    spread = [feats["hours_to_peak"], max(0.0, feats["hours_to_peak"]), feats["fc_rise"], feats["high_minus_base"],
              abs(feats["t_minus_fc"])]
    ss = [(v - band["smu"][i]) / band["ssd"][i] for i, v in enumerate(spread)] + [1.0]
    sigma = max(0.35, min(3.0, math.exp(sum(g * v for g, v in zip(band["gamma"], ss))) * band["scale"]))
    vector = {}
    for rung in axis:
        lo = -1e9 if rung["lo_c"] is None else rung["lo_c"]
        hi = 1e9 if rung["hi_c"] is None else rung["hi_c"]
        if hi <= high + 1e-9:
            p = 0.0
        elif lo <= high + 1e-9:
            p = _cdf((hi - centre) / sigma)
        else:
            p = _cdf((hi - centre) / sigma) - _cdf((lo - centre) / sigma)
        vector[rung["bucket"]] = max(p, 0.0)
    total = sum(vector.values()) or 1.0
    vector = {b: max(p / total, 1e-3) for b, p in vector.items()}
    total = sum(vector.values())
    vector = {b: round(p / total, 4) for b, p in vector.items()}
    mode = max(vector, key=vector.get)
    top = sorted(((k, v) for k, v in parts.items() if abs(v) >= 0.05), key=lambda kv: -abs(kv[1]))[:4]
    return {"state": "ok", "band": band_name, "base_c": round(base, 2), "base_systems": n_base,
            "centre_c": round(centre, 2), "sigma_c": round(sigma, 2), "running_high_c": high,
            "mode": mode, "mode_p": vector[mode], "vector": vector, "station_effect_c": round(station_effect, 2),
            "latest_report_utc": cur["valid_at_utc"],
            "parts": [{"feature": k, "c": round(v, 2), "value": feats.get(k.replace("_missing", ""))} for k, v in top],
            "inputs": {k: (None if v is None else round(float(v), 3)) for k, v in feats.items() if k != "dew_minus_fc_dew"},
            "walk_forward_mae": band["walk_forward_mae"], "fitted_at_utc": model.get("fitted_at_utc"),
            "window": model.get("window"), "as_of_utc": board._iso(now)}


def _cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _inputs(conn, station: str, zone: ZoneInfo, day: str) -> dict[str, Any] | None:
    heads = [board._envelope_head(conn, station, day, s) for s in board.STAGES]
    newest = max((x for x in heads if x), key=lambda x: str(x["decided_at_utc"]), default=None)
    if newest is None:
        return None
    axis = board.ladder_axis(newest["ordinals"], board._json(newest["lineage"].get("bucket_intervals_c_json"), {}))
    where = board._station_lat_lon(conn, station)
    sat = None
    if where and board._satellite_product(where[1]) == "jaxa_ptree_himawari":
        sat = board._jaxa_hours(station, where[0], where[1], day, zone)
    return {"axis": axis, "where": where, "sat": sat, "rain": board._precip_sat(station, day, zone)}


def _wait_for_readers(deadline: float) -> None:
    """The JAXA and GSMaP readers queue the files a call asked for and read
    them on their own threads; wait (bounded) for both queues to drain."""

    from . import jaxa_gsmap, jaxa_ptree_radiation  # noqa: PLC0415

    while time.monotonic() < deadline:
        if not jaxa_gsmap.status().get("pending") and not jaxa_ptree_radiation.status().get("pending"):
            return
        time.sleep(2.0)


def run(*, db: Path = board.DEFAULT_DB, now: datetime | None = None, wait_sec: float = 240.0) -> dict[str, Any]:
    model = load_model()
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    if model is None:
        return {"state": "no_model"}
    lo_h = min(b["hours"][0] for b in model["bands"].values())
    hi_h = max(b["hours"][1] for b in model["bands"].values())
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    todo: list[tuple[str, ZoneInfo, str]] = []
    try:
        conn.execute("PRAGMA query_only=1")
        for entry in board.fleet(conn, now):
            station = entry["station_id"]
            zone = ZoneInfo(board.station_timezone(conn, station))
            local = now.astimezone(zone)
            if lo_h <= local.hour < hi_h:
                todo.append((station, zone, local.date().isoformat()))
        # First pass asks the satellite readers for every hour the day needs.
        for station, zone, day in todo:
            _inputs(conn, station, zone, day)
    finally:
        conn.close()
    _wait_for_readers(started + wait_sec)
    now = datetime.now(timezone.utc)
    readings: dict[str, Any] = {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("PRAGMA query_only=1")
        for station, zone, day in todo:
            try:
                readings[station] = {"event_date": day, **_one(conn, model, station, zone, day, now)}
            except Exception as exc:  # noqa: BLE001 -- one station's failure is its own row
                readings[station] = {"event_date": day, "state": "error",
                                     "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        conn.close()
    _write(readings, now)
    return {"state": "ok", "stations": len(todo),
            "ok": sum(1 for r in readings.values() if r.get("state") == "ok"),
            "seconds": round(time.monotonic() - started, 1)}


def _one(conn, model, station: str, zone: ZoneInfo, day: str, now: datetime) -> dict[str, Any]:
    base, n_base = baseline(conn, station, day, zone)
    if base is None:
        return {"state": "no_baseline"}
    got = _inputs(conn, station, zone, day)
    if got is None:
        return {"state": "no_ladder"}
    if got["where"] is None:
        return {"state": "no_coordinates"}
    fc = forecast_curve(station, got["where"][0], got["where"][1], str(zone.key), day)
    if fc is None:
        return {"state": "no_forecast_curve"}
    sat = got["sat"] or {}
    clouds = sat.get("clouds")
    metar = board._metar(station, day, zone)
    return compute(model, station, zone, now, got["axis"], base, n_base, metar, fc,
                   sat.get("values"), clouds, (got["rain"] or {}).get("hours"))


def _write(readings: Mapping[str, Any], now: datetime) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(LATEST_PATH.read_text(encoding="utf-8")).get("readings") or {}
    except (OSError, ValueError):
        previous = {}
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    for station, r in readings.items():
        if r.get("state") != "ok":
            continue
        old = previous.get(station) or {}
        same = (old.get("event_date") == r["event_date"] and old.get("centre_c") == r["centre_c"]
                and old.get("sigma_c") == r["sigma_c"] and old.get("inputs") == r["inputs"])
        if same:
            continue
        line = {"station": station, **{k: r[k] for k in (
            "event_date", "as_of_utc", "band", "base_c", "centre_c", "sigma_c", "mode", "mode_p",
            "running_high_c", "latest_report_utc", "inputs", "vector", "fitted_at_utc")}}
        with (HISTORY_DIR / f"{r['event_date']}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    # A station leaving its window keeps its last reading until its next day.
    merged = {s: r for s, r in previous.items() if s not in readings}
    merged.update(readings)
    tmp = LATEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"schema": "d0_readings_v1", "computed_at_utc": board._iso(now), "readings": merged},
                              ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, LATEST_PATH)
    cutoff = (now - timedelta(days=FORECAST_KEEP_DAYS)).date().isoformat()
    for folder in FORECAST_DIR.glob("*") if FORECAST_DIR.exists() else []:
        if folder.name < cutoff:
            for f in folder.glob("*"):
                f.unlink()
            folder.rmdir()


def stored(station: str, day: str) -> dict[str, Any] | None:
    """What the board shows: the newest stored reading of the station-day
    and its history (time, centre, sigma, mode), or None."""

    try:
        doc = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "not_computed"}
    r = (doc.get("readings") or {}).get(station)
    if not r or r.get("event_date") != day:
        return {"state": "not_computed"}
    history = []
    try:
        with (HISTORY_DIR / f"{day}.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                if f'"station": "{station}"' not in line:
                    continue
                x = json.loads(line)
                history.append({"at": x["as_of_utc"], "centre_c": x["centre_c"], "sigma_c": x["sigma_c"],
                                "mode": x["mode"], "mode_p": x["mode_p"], "high_c": x.get("running_high_c")})
    except OSError:
        pass
    return {**r, "computed_at_utc": doc.get("computed_at_utc"), "history": history}


def main() -> None:
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
