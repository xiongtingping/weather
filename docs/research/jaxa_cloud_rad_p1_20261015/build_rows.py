"""Served P1 rows for the JAXA cloud/sunshine re-test (2026-10-15 「重开重测」),
built by obs_increment_p1_20261007/build.py's own code over 09-01..10-14 and
the 17 Himawari-disk stations only.  Its original docstring follows.

Observed-minus-forecast sky/temperature increments at every P1 decision.

User 2026-10-07 「以今天zuuu，25度为研究……系统是否有实际云、雨、辐射对比预测的
云、雨、辐射的对比增量，并作为强似然来修改p1的概率」/「我想实现的是增量，作为强似然
和强权重」, ruling: fit the strength on history, serve only if out of sample
the scores improve.

One row per (station, local day, local hour 07..19): the last served P1 of
that hour (a decision second's replay collapsed to its last envelope), the
day's settled high (formal lanes, newest revision), and at that instant:

* the newest METAR at or before it: temperature, low cloud cover (the
  production parser's fractions), precipitation;
* the Open-Meteo hourly forecast issued a day before (``*_previous_day1`` --
  strictly as-of: every value was published >= 24 h before its hour):
  temperature, total cloud, shortwave, precipitation, for the whole day.

Read-only.  Open-Meteo responses are cached under ``cache/`` per station.
"""

from __future__ import annotations

import gzip
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.metar_path_state import parse_metar_physical_state  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
ARCHIVE = ROOT / "data" / "archive.db"
FIRST, LAST = "2026-09-01", "2026-10-14"
#: The re-test's stations: the Himawari disk JAXA covers (east of 70E).
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from bayes_basket_target import jaxa_ptree_radiation as _J  # noqa: E402
import os as _os
from bayes_basket_target import jaxa_gsmap as _G  # noqa: E402
#: ROWS_SET=gsmap: every station GSMaP covers (the rain arm), else the
#: Himawari-disk stations (cloud and sunshine).
STATIONS = sorted(_G._fleet() if _os.environ.get("ROWS_SET") == "gsmap" else _J._fleet())
ROWS_FILE = "rows_gsmap.jsonl.gz" if _os.environ.get("ROWS_SET") == "gsmap" else "rows.jsonl.gz"
HOURS = range(7, 20)


def utc(s: str) -> datetime:
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def open_meteo(station: str, lat: float, lon: float, tz: str) -> dict:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{station}.json.gz"
    if path.exists():
        return json.load(gzip.open(path, "rt"))
    import ssl
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen
    import certifi
    q = urlencode({"latitude": lat, "longitude": lon, "timezone": tz,
                   "start_date": FIRST, "end_date": LAST,
                   "hourly": "temperature_2m_previous_day1,cloud_cover_previous_day1,"
                             "shortwave_radiation_previous_day1,precipitation_previous_day1"})
    ctx = ssl.create_default_context(cafile=certifi.where())
    # Research calls are charged to the production ledger (10-11 incident):
    # 44 days of four variables count about four calls.
    from bayes_basket_target import open_meteo_quota as quota  # noqa: PLC0415
    quota.charge("research", 4)
    for attempt in range(4):
        try:
            with urlopen(Request("https://previous-runs-api.open-meteo.com/v1/forecast?" + q,
                                 headers={"User-Agent": "weather-research"}), timeout=60, context=ctx) as r:
                data = json.loads(r.read().decode())
            break
        except Exception as exc:  # noqa: BLE001
            print("  retry", station, exc, flush=True)
            time.sleep(5 * (attempt + 1))
    else:
        raise RuntimeError(f"open-meteo failed for {station}")
    json.dump(data, gzip.open(path, "wt"))
    time.sleep(1.0)
    return data


def main() -> None:
    con = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=30)
    arc = sqlite3.connect(f"file:{ARCHIVE}?mode=ro", uri=True, timeout=30)
    stations = STATIONS
    truth = {}
    for st, day, v, lane in con.execute(
            "SELECT station_id, local_date, official_tmax_c, source_lane FROM calibration_station_daily_high_truth t"
            " WHERE local_date BETWEEN ? AND ? AND qc_status='ACCEPTED' AND source_lane IN ('AWC','SETTLEMENT_SOURCE')"
            " AND revision_no=(SELECT MAX(revision_no) FROM calibration_station_daily_high_truth x"
            "   WHERE x.station_id=t.station_id AND x.local_date=t.local_date AND x.source_lane IN ('AWC','SETTLEMENT_SOURCE'))",
            (FIRST, LAST)):
        truth[(st, day)] = float(v)
    out = gzip.open(HERE / ROWS_FILE, "wt")
    n_rows = 0
    for st in stations:
        tzrow = con.execute("SELECT settlement_timezone FROM calibration_station_market_timezone_binding WHERE station_id=? LIMIT 1", (st,)).fetchone()
        ll = con.execute("SELECT latitude, longitude FROM calibration_station_forecast_physics_version WHERE station_id=? AND latitude IS NOT NULL ORDER BY captured_at_utc DESC LIMIT 1", (st,)).fetchone()
        if not tzrow or not ll:
            continue
        tz = tzrow[0]; zone = ZoneInfo(tz)
        om = open_meteo(st, ll[0], ll[1], tz)["hourly"]
        fc = {t: {"t": om["temperature_2m_previous_day1"][i], "cloud": om["cloud_cover_previous_day1"][i],
                  "sw": om["shortwave_radiation_previous_day1"][i], "pr": om["precipitation_previous_day1"][i]}
              for i, t in enumerate(om["time"])}
        day = datetime.fromisoformat(FIRST).date()
        while day <= datetime.fromisoformat(LAST).date():
            d = day.isoformat(); day += timedelta(days=1)
            if (st, d) not in truth:
                continue
            start = datetime(int(d[:4]), int(d[5:7]), int(d[8:]), tzinfo=zone)
            lo, hi = start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)
            p1 = con.execute(
                "SELECT decision_at_utc, probabilities_json FROM basket_probability_envelope"
                " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND +stage='P1' ORDER BY decision_at_utc",
                (st, d)).fetchall()
            if not p1:
                continue
            last_by_hour = {}
            for at, pj in p1:
                h = utc(at).astimezone(zone).hour
                if h in HOURS:
                    last_by_hour[h] = (at, pj)
            if not last_by_hour:
                continue
            meta = con.execute(
                "SELECT json_extract(lineage_json,'$.bucket_intervals_c_json') FROM basket_probability_envelope"
                " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND +stage='P1' LIMIT 1", (st, d)).fetchone()
            intervals = json.loads(meta[0]) if meta and meta[0] else None
            mets = []
            for va, raw, temp in arc.execute(
                    "SELECT valid_at, raw_text, reported_temperature_c FROM reported_trajectory_observations"
                    " WHERE station_id=? AND valid_at>=? AND valid_at<? AND raw_text<>'' ORDER BY valid_at",
                    (st, lo.isoformat()[:19], hi.isoformat()[:19])):
                if temp is None:
                    continue
                try:
                    ps = parse_metar_physical_state(raw, expected_temperature_c=float(temp))
                except Exception:  # noqa: BLE001
                    continue
                mets.append((utc(va), None if temp is None else float(temp), ps.cloud_cover_fraction, bool(ps.precipitation_present)))
            fday = {h: fc.get(f"{d}T{h:02d}:00") for h in range(24)}
            if any(v is None or v["t"] is None for v in fday.values()):
                continue
            for h, (at, pj) in sorted(last_by_hour.items()):
                t = utc(at)
                past = [m for m in mets if m[0] <= t]
                if not past:
                    continue
                high = max(m[1] for m in past if m[1] is not None)
                cur = past[-1]
                clouds = [m[2] for m in past[-3:] if m[2] is not None]
                out.write(json.dumps({
                    "station": st, "day": d, "decided_at": at, "local_hour": round(t.astimezone(zone).hour + t.astimezone(zone).minute / 60, 3),
                    "lat": ll[0], "lon": ll[1], "tz": tz,
                    "p1": json.loads(pj), "intervals": intervals, "truth": truth[(st, d)],
                    "running_high": high, "temp_now": cur[1], "cloud_now": cur[2],
                    "cloud_recent": clouds, "precip_now": cur[3],
                    "precip_any_so_far": any(m[3] for m in past),
                    "fc": [fday[x] for x in range(24)],
                }) + "\n")
                n_rows += 1
        print(st, "rows so far", n_rows, flush=True)
    out.close()
    print("done", n_rows)


if __name__ == "__main__":
    main()
