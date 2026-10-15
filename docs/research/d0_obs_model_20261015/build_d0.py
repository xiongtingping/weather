"""Rows for the D0 observation model (PROTOCOL.md section 3): one per station,
local day, local hour 07..18, anchored at HH:30 local, everything as of that
instant.

Sources (all read-only):
* station-day frame: ``../jaxa_cloud_rad_p1_20261015/rows_gsmap.jsonl.gz``
  (47 stations, 09-01..10-14: timezone, coordinates, settled truth, ladder,
  Open-Meteo day-ahead hourly curve = research-only shape features);
* METAR/SPECI from archive.db by ``first_seen_at`` (temperature, dew point,
  wind, cloud, precipitation, pressure);
* official deterministic Tmax (IFS HRES / GFS / AIFS single, native point
  runs, ``knowable_at_utc`` <= local midnight of the day): the baseline B;
* JAXA SWR / cloud (east of 70E), GOES DSR + Open-Meteo satellite (the
  previous study's Radiation helper, to 10-07), GSMaP_NOW rain (47);
* the market mid vector at the anchor (official WS snapshot <= 30 min old)
  and the served P1 at the anchor, for judging.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
import os as _os
# Automation (scripts/d0_refit_daily.sh): one runtime folder holds the inputs
# (jaxa/, gsmap/, rows_gsmap.jsonl.gz) and the output.
JX = Path(_os.environ.get("D0_DATA_DIR") or HERE.parent / "jaxa_cloud_rad_p1_20261015")
OUT_DIR = Path(_os.environ.get("D0_DATA_DIR") or HERE)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE.parent / "sat_cloud_obs_20261008"))
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.metar_path_state import parse_metar_physical_state  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
ARCHIVE = ROOT / "data" / "archive.db"
HOURS = range(7, 19)
OFFICIAL = ("ECMWF_IFS_HRES_NATIVE_POINT", "NOAA_GFS_025_NATIVE_POINT", "ECMWF_AIFS_SINGLE_POINT")
WIND_RE = re.compile(r"\b(\d{3}|VRB)(\d{2,3})(?:G\d{2,3})?KT\b")
_LIST_RE_MT = None


def utc(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def z(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_jaxa():
    swr, clp = {}, {}
    for path in sorted((JX / "jaxa").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("state") != "ok":
                continue
            hour, mt = utc(rec["hour"]), utc(rec["mtime_utc"])
            for st, v in rec["values"].items():
                if rec["kind"] == "swr" and v is not None:
                    swr.setdefault(st, []).append((hour, mt, float(v)))
                elif rec["kind"] == "clp" and isinstance(v, dict) and v.get("state") == "ok":
                    clp.setdefault(st, []).append((hour, mt, v))
    return swr, clp


def load_gsmap():
    out = {}
    for path in sorted((JX / "gsmap").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("state") != "ok":
                continue
            slot, mt = utc(rec["slot"]), utc(rec["mtime_utc"])
            for st, v in rec["values"].items():
                if v.get("mm") is not None:
                    out.setdefault(st, []).append((slot, mt, float(v["mm"])))
    return out


def official_tmax(conn, st, day, before):
    vals = []
    for sysid in OFFICIAL:
        row = conn.execute(
            "SELECT forecast_tmax_c FROM calibration_regional_point_run WHERE ensemble_system_id=? AND station_id=?"
            " AND target_local_date=? AND knowable_at_utc<=? AND forecast_tmax_c IS NOT NULL ORDER BY run_init_utc DESC LIMIT 1",
            (sysid, st, day, before)).fetchone()
        if row:
            vals.append(float(row[0]))
    if not vals:
        return None, 0
    vals.sort()
    return vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2, len(vals)


def market(conn, st, day, at):
    hi = at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"
    lo_t = at - timedelta(minutes=30)
    lo = lo_t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{lo_t.microsecond // 1000:03d}Z"
    row = conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1", (st, day, hi, lo)).fetchone()
    if row is None:
        return None
    buckets = json.loads(row[0] or "[]")
    ref = market_reference_v1(buckets)
    if ref.get("state") != "complete":
        return None
    quotes = {}
    for b in buckets:
        ask = b.get("best_ask") if b.get("ask_available") else None
        bid = b.get("bid_price") if b.get("bid_available") else None
        quotes[str(b.get("bucket"))] = [float(ask) if ask else None, float(bid) if bid else None]
    return {"q": {str(k): float(v) for k, v in ref["vector"].items()}, "quotes": quotes}


def served_p1(conn, st, day, at):
    row = conn.execute(
        "SELECT probabilities_json FROM basket_probability_envelope WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND +stage='P1' AND decision_at_utc<=? ORDER BY decision_at_utc DESC LIMIT 1", (st, day, z(at))).fetchone()
    return json.loads(row[0]) if row else None


def main() -> None:
    frames = {}
    for line in gzip.open(JX / "rows_gsmap.jsonl.gz", "rt"):
        r = json.loads(line)
        frames.setdefault((r["station"], r["day"]), r)
    swr, clp = load_jaxa()
    gsmap = load_gsmap()
    from rad_features import Radiation  # noqa: E402  (GOES + Open-Meteo satellite, to 10-07)
    rad = Radiation()
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=30)
    arc = sqlite3.connect(f"file:{ARCHIVE}?mode=ro", uri=True, timeout=30)
    out = gzip.open(OUT_DIR / "rows_d0.jsonl.gz", "wt")
    n = 0
    for (st, day), fr in sorted(frames.items()):
        zone = ZoneInfo(fr["tz"])
        d = datetime.fromisoformat(day).date()
        midnight = datetime.combine(d, dtime(0), zone)
        base, n_off = official_tmax(conn, st, day, z(midnight))
        if base is None:
            continue
        lo, hi = z(midnight), z(midnight + timedelta(days=1))
        mets = []
        for va, raw, temp, seen in arc.execute(
                "SELECT valid_at, raw_text, reported_temperature_c, first_seen_at FROM reported_trajectory_observations"
                " WHERE station_id=? AND valid_at>=? AND valid_at<? AND raw_text<>'' ORDER BY valid_at",
                (st, lo[:19], hi[:19])):
            if temp is None:
                continue
            try:
                ps = parse_metar_physical_state(raw, expected_temperature_c=float(temp))
            except Exception:  # noqa: BLE001
                continue
            m = WIND_RE.search(raw)
            wdir = None if not m or m.group(1) == "VRB" else int(m.group(1))
            mets.append(dict(valid=utc(va), seen=utc(seen) if seen else utc(va), t=float(temp), dew=ps.dewpoint_c,
                             wind=ps.wind_speed_mps, wdir=wdir, cloud=ps.cloud_cover_fraction,
                             precip=bool(ps.precipitation_present), p=ps.pressure_hpa))
        if not mets:
            continue
        fc = fr["fc"]
        fc_t = [x["t"] for x in fc]
        fc_peak_h = max(range(24), key=lambda i: fc_t[i])
        fc_sw = [x["sw"] or 0.0 for x in fc]
        fc_cloud = [x["cloud"] for x in fc]
        fc_pr = [x["pr"] or 0.0 for x in fc]
        for h in HOURS:
            at = datetime.combine(d, dtime(h, 30), zone).astimezone(timezone.utc)
            past = [m for m in mets if m["seen"] <= at and m["valid"] <= at]
            if not past:
                continue
            cur = past[-1]
            if at - cur["valid"] > timedelta(hours=2):
                continue
            high = max(m["t"] for m in past)
            two_h = [m for m in past if m["valid"] >= at - timedelta(hours=2, minutes=10)]
            momentum = (cur["t"] - two_h[0]["t"]) / max(0.5, (cur["valid"] - two_h[0]["valid"]).total_seconds() / 3600) if len(two_h) > 1 else 0.0
            # Forecast curve at this instant (Open-Meteo day-ahead, research shape feature).
            fh = min(23, h)
            fc_now = fc_t[fh - 1] + (fc_t[fh] - fc_t[fh - 1]) * 0.5
            fc_rise = max(0.0, max(fc_t[fh:]) - fc_now)
            fc_first = fc_t[max(0, h - 7)]
            warmed = cur["t"] - past[0]["t"]
            # Satellite sunshine: cumulative since local 05, observed vs forecast, as of.
            sun_obs = sun_fc = 0.0
            sun_n = 0
            src = None
            if st in swr:
                for hour, mt, v in swr[st]:
                    lh = hour.astimezone(zone)
                    if lh.date() == d and 5 <= lh.hour < h and mt <= at:
                        sun_obs += v; sun_fc += fc_sw[min(23, lh.hour + 1)]; sun_n += 1
                src = "jaxa"
            elif st in rad.obs:
                for x in rad.obs[st]:
                    lh = x["at"].astimezone(zone)
                    if lh.date() == d and 5 <= lh.hour < h and x["available"] <= at:
                        f = rad.forecast_at(st, x["at"]) if x["kind"] == "hour_mean" else rad.forecast_at(st, x["at"] + timedelta(minutes=30))
                        if f is not None:
                            sun_obs += x["sw"]; sun_fc += f; sun_n += 1
                src = rad.source.get(st)
            sun_pct = None if sun_n < 2 or sun_fc < 100 else max(-0.8, min(0.8, (sun_obs - sun_fc) / sun_fc))
            # Satellite cloud (JAXA): newest scan within 3 h, effective cover vs forecast total.
            cloud_gap = None
            cot = None
            if st in clp:
                cand = [(hour, v) for hour, mt, v in clp[st] if mt <= at and at - hour <= timedelta(hours=3)]
                if cand:
                    hour, v = max(cand)
                    lh = hour.astimezone(zone)
                    fcl = fc_cloud[min(23, lh.hour + 1)]
                    if fcl is not None and v.get("cover") is not None:
                        cot = v.get("optical_thickness") or 0.0
                        eff = v["cover"] * (1 - math.exp(-cot / 10.0))
                        cloud_gap = eff - fcl / 100.0
            # Rain: GSMaP last 3 h vs forecast, as of.
            rain_obs = rain_fc = 0.0
            rain_n = 0
            for slot, mt, mm in gsmap.get(st, []):
                if mt <= at and at - slot <= timedelta(hours=3, minutes=30) and slot < at:
                    lh = slot.astimezone(zone)
                    rain_obs += mm; rain_fc += fc_pr[min(23, lh.hour + 1)]; rain_n += 1
            rain_gap = (math.log1p(rain_obs) - math.log1p(rain_fc)) if rain_n else None
            mk = market(conn, st, day, at)
            p1 = served_p1(conn, st, day, at)
            out.write(json.dumps({
                "station": st, "day": day, "hour": h, "at": z(at), "tz": fr["tz"], "lat": fr["lat"], "lon": fr["lon"],
                "truth": fr["truth"], "intervals": fr["intervals"], "base": base, "base_n": n_off,
                "high": high, "t": cur["t"], "t_minus_fc": cur["t"] - fc_now, "fc_rise": fc_rise, "fc_peak_h": fc_peak_h,
                "fc_tmax": max(fc_t), "warmed": warmed, "fc_warmed": fc_now - fc_first, "momentum": momentum,
                "dew": cur["dew"], "dew_dep": None if cur["dew"] is None else cur["t"] - cur["dew"],
                "wind": cur["wind"], "wdir": cur["wdir"], "cloud_low": cur["cloud"], "precip_now": cur["precip"],
                "precip_any": any(m["precip"] for m in past), "pressure": cur["p"],
                "sun_pct": sun_pct, "sun_n": sun_n, "sun_src": src, "cloud_gap": cloud_gap, "cot": cot,
                "rain_gap": rain_gap, "rain_obs": rain_obs,
                "market": mk, "p1": p1,
            }) + "\n")
            n += 1
        if n % 2000 < 12:
            print(st, day, "rows", n, flush=True)
    out.close()
    print("done", n)


if __name__ == "__main__":
    main()
