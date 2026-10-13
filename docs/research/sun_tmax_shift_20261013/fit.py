"""Morning sunshine against the day-ahead forecast -> the day's maximum
against the day-ahead forecast maximum (熊先生 2026-10-13 「前端显式：上午日照
偏差 → 最高温相对预报的偏差，显示度数」).

Per station-day and per last measured local hour H (Open-Meteo hour-ending
stamps, H = 9..17): x = 100 * (satellite sum - forecast sum) / forecast sum
over the hours stamped 05..H, forecast = ``shortwave_radiation_previous_day1``;
y = settled truth Tmax (AWC / SETTLEMENT_SOURCE, newest revision) - the max of
``temperature_2m_previous_day1`` over the local day.  Both are taken against
each station's own mean (station fixed effect), so the reading is the shift
from that station's usual forecast error.  Output per H: a two-slope line
``shift = a + b_dark*min(x,0) + b_bright*max(x,0)`` with x clipped to
+-X_CLIP (darker days move the maximum about 1.5x as much as brighter ones;
the bin centroids are too noisy to interpolate between), bootstrap 95%
intervals, R^2, and the bin centroids for reference.  Inputs are the cached series of
``obs_increment_p1_20261007`` / ``sat_cloud_obs_20261008`` (no new fetch).

Display only: an Open-Meteo (aggregator_republisher) reading, it enters no
P0/P0-M/P1 layer.
"""
from __future__ import annotations

import gzip
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE.parent / "sat_cloud_obs_20261008"))
from rad_features import FORECAST, Radiation  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
FIRST, LAST = "2026-09-01", "2026-10-06"
HOURS = range(9, 18)
BINS = [(-100, -40), (-40, -20), (-20, -10), (-10, 10), (10, 20), (20, 40), (40, 1000)]
MIN_STATION_DAYS = 8
X_CLIP = 80.0
OUT = ROOT / "config" / "ui" / "sun_tmax_shift_v1.json"


def main() -> None:
    rad = Radiation()
    zone: dict[str, ZoneInfo] = {}
    fc_tmax: dict[tuple[str, str], float] = {}
    for path in FORECAST.glob("*.json.gz"):
        st = path.name.split(".")[0]
        body = json.load(gzip.open(path, "rt"))
        zone[st] = ZoneInfo(body["timezone"])
        for t, v in zip(body["hourly"]["time"], body["hourly"]["temperature_2m_previous_day1"]):
            if v is not None:
                fc_tmax[(st, t[:10])] = max(fc_tmax.get((st, t[:10]), -99.0), float(v))
    con = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=30)
    truth = {(st, d): float(v) for st, d, v in con.execute(
        "SELECT station_id, local_date, official_tmax_c FROM calibration_station_daily_high_truth t"
        " WHERE local_date BETWEEN ? AND ? AND qc_status='ACCEPTED' AND source_lane IN ('AWC','SETTLEMENT_SOURCE')"
        " AND revision_no=(SELECT MAX(revision_no) FROM calibration_station_daily_high_truth x"
        "  WHERE x.station_id=t.station_id AND x.local_date=t.local_date AND x.source_lane IN ('AWC','SETTLEMENT_SOURCE'))",
        (FIRST, LAST))}
    table = {}
    for hour in HOURS:
        rows = []
        for st, obs in rad.obs.items():
            if st not in zone:
                continue
            acc: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
            for x in obs:
                stamp = x["at"].astimezone(zone[st])
                stamp_hour = stamp.hour + stamp.minute / 60 + 0.5   # hour-ending stamp
                if not 5 <= stamp_hour <= hour:
                    continue
                f = rad.forecast_at(st, x["at"])
                if f is None:
                    continue
                a = acc[stamp.date().isoformat()]
                a[0] += x["sw"]; a[1] += f; a[2] += 1
            for day, (o, f, n) in acc.items():
                key = (st, day)
                if key in truth and key in fc_tmax and n >= hour - 6 and f >= 60.0 * n:
                    rows.append((st, 100.0 * (o - f) / f, truth[key] - fc_tmax[key], rad.source[st]))
        by = defaultdict(list)
        for r in rows:
            by[r[0]].append(r)
        xs, ys = [], []
        for st, rs in by.items():
            if len(rs) < MIN_STATION_DAYS:
                continue
            mx = sum(r[1] for r in rs) / len(rs)
            my = sum(r[2] for r in rs) / len(rs)
            for r in rs:
                xs.append((r[1], r[1] - mx, r[2] - my))
        def two_slope(sample):
            # least squares of y on [1, min(x,0), max(x,0)] (3x3 normal equations)
            feats = [(1.0, min(max(x, -X_CLIP), 0.0), max(min(x, X_CLIP), 0.0), y) for x, _, y in sample]
            m = [[sum(f[i] * f[j] for f in feats) for j in range(3)] for i in range(3)]
            v = [sum(f[i] * f[3] for f in feats) for i in range(3)]
            for c in range(3):
                piv = m[c][c]
                for r in range(3):
                    if r != c:
                        k = m[r][c] / piv
                        m[r] = [m[r][j] - k * m[c][j] for j in range(3)]
                        v[r] -= k * v[c]
            coef = [v[i] / m[i][i] for i in range(3)]
            return coef, feats
        coef, feats = two_slope(xs)
        sst = sum((f[3] - sum(f[3] for f in feats) / len(feats)) ** 2 for f in feats)
        sse = sum((f[3] - coef[0] - coef[1] * f[1] - coef[2] * f[2]) ** 2 for f in feats)
        r2 = 1 - sse / sst
        random.seed(hour)
        boot = [two_slope([random.choice(xs) for _ in xs])[0] for _ in range(400)]
        ci = lambda i: [round(10 * sorted(c[i] for c in boot)[10], 3), round(10 * sorted(c[i] for c in boot)[389], 3)]
        points = []
        for lo, hi in BINS:
            sel = [(x, y) for x, _, y in xs if lo <= x < hi]
            if len(sel) < 20:
                continue
            my = sum(y for _, y in sel) / len(sel)
            se = math.sqrt(sum((y - my) ** 2 for _, y in sel) / (len(sel) - 1) / len(sel))
            points.append({"pct": round(sum(x for x, _ in sel) / len(sel), 1), "shift_c": round(my, 2),
                           "ci95_c": round(1.96 * se, 2), "n": len(sel)})
        table[str(hour)] = {"n": len(xs), "stations": len({r[0] for r in rows if len(by[r[0]]) >= MIN_STATION_DAYS}),
                            "intercept_c": round(coef[0], 3),
                            "dark_c_per_10pct": round(10 * coef[1], 3), "dark_ci95": ci(1),
                            "bright_c_per_10pct": round(10 * coef[2], 3), "bright_ci95": ci(2),
                            "r2": round(r2, 3), "resid_sd_c": round(math.sqrt(sse / len(feats)), 2), "points": points}
        t = table[str(hour)]
        print(hour, t["n"], t["intercept_c"], t["dark_c_per_10pct"], t["dark_ci95"], t["bright_c_per_10pct"], t["bright_ci95"], t["r2"], t["resid_sd_c"])
    doc = {
        "schema": "sun_tmax_shift_v1",
        "fitted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": [FIRST, LAST],
        "x_clip_pct": X_CLIP,
        "model": "shift_c = intercept_c + dark/10*min(x,0) + bright/10*max(x,0), x clipped to +-x_clip_pct",
        "x": "100*(satellite - day-ahead forecast)/forecast shortwave, summed over local hour-ending stamps 05..H",
        "y": "settled Tmax - day-ahead forecast Tmax (Open-Meteo previous_day1), station-demeaned",
        "lineage": "docs/research/sun_tmax_shift_20261013/fit.py; Open-Meteo aggregator_republisher, display only",
        "by_last_hour": table,
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
