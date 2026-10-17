"""Morning sunshine against the day-ahead forecast -> the day's maximum, on the
absolute difference in W/m² (熊先生 2026-10-17 「改用 W/m² 的绝对差重新拟合这条线」).

The v1 line (docs/research/sun_tmax_shift_20261013/fit.py) read the morning as
a percentage of the day-ahead forecast, so a dim forecast made the ratio blow
up (ZUUU 2026-10-17: +200 W/m² over a 52 W/m² forecast read +106%) and the
fit had to refuse every morning whose forecast was under 60 W/m² an hour.

Here x = mean over the hour-ending stamps 05..H of (satellite - day-ahead
forecast) shortwave, in W/m², clipped to +-X_CLIP; no sunshine gate.  y and
everything else as v1: settled Tmax (AWC / SETTLEMENT_SOURCE, newest
revision) - the max of ``temperature_2m_previous_day1``, station-demeaned;
two slopes (darker / brighter), bootstrap 95% intervals.

Judged against v1 out of sample on v1's own sample (five folds by event
day): mean squared error of y for the v1 line, this line, and the intercept
alone.  Inputs are the same cached series (no new fetch).

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
MIN_STATION_DAYS = 8
X_CLIP = 300.0          # W/m², mean over the morning's hours
PCT_CLIP = 80.0         # v1
BINS = [(-1000, -150), (-150, -75), (-75, -25), (-25, 25), (25, 75), (75, 150), (150, 1000)]
FOLDS = 5
OUT = ROOT / "config" / "ui" / "sun_tmax_shift_v2.json"


def two_slope(sample, clip):
    """Least squares of y on [1, min(x,0), max(x,0)], x clipped to +-clip."""
    feats = [(1.0, min(max(x, -clip), 0.0), max(min(x, clip), 0.0), y) for x, y in sample]
    m = [[sum(f[i] * f[j] for f in feats) for j in range(3)] for i in range(3)]
    v = [sum(f[i] * f[3] for f in feats) for i in range(3)]
    for c in range(3):
        piv = m[c][c]
        for r in range(3):
            if r != c:
                k = m[r][c] / piv
                m[r] = [m[r][j] - k * m[c][j] for j in range(3)]
                v[r] -= k * v[c]
    return [v[i] / m[i][i] for i in range(3)]


def predict(coef, x, clip):
    return coef[0] + coef[1] * min(max(x, -clip), 0.0) + coef[2] * max(min(x, clip), 0.0)


def load():
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
    con.close()
    return rad, zone, fc_tmax, truth


def rows_for(hour, rad, zone, fc_tmax, truth):
    """(station, day, obs_sum, fc_sum, n, y) over stamps 05..hour, station-demeaned y."""
    rows = []
    for st, obs in rad.obs.items():
        if st not in zone:
            continue
        acc: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
        for x in obs:
            stamp = x["at"].astimezone(zone[st])
            stamp_hour = stamp.hour + stamp.minute / 60 + 0.5
            if not 5 <= stamp_hour <= hour:
                continue
            f = rad.forecast_at(st, x["at"])
            if f is None:
                continue
            a = acc[stamp.date().isoformat()]
            a[0] += x["sw"]; a[1] += f; a[2] += 1
        for day, (o, f, n) in acc.items():
            key = (st, day)
            if key in truth and key in fc_tmax and n >= hour - 6:
                rows.append([st, day, o, f, n, truth[key] - fc_tmax[key]])
    by = defaultdict(list)
    for r in rows:
        by[r[0]].append(r)
    out = []
    for st, rs in by.items():
        if len(rs) < MIN_STATION_DAYS:
            continue
        my = sum(r[5] for r in rs) / len(rs)
        for r in rs:
            out.append((r[0], r[1], r[2], r[3], r[4], r[5] - my))
    return out


def cv_mse(rows, xfun, clip):
    days = sorted({r[1] for r in rows})
    fold = {d: i % FOLDS for i, d in enumerate(days)}
    se = 0.0
    for k in range(FOLDS):
        train = [(xfun(r), r[5]) for r in rows if fold[r[1]] != k]
        coef = two_slope(train, clip)
        se += sum((r[5] - predict(coef, xfun(r), clip)) ** 2 for r in rows if fold[r[1]] == k)
    return se / len(rows)


def cv_mse_mean(rows):
    days = sorted({r[1] for r in rows})
    fold = {d: i % FOLDS for i, d in enumerate(days)}
    se = 0.0
    for k in range(FOLDS):
        train = [r[5] for r in rows if fold[r[1]] != k]
        m = sum(train) / len(train)
        se += sum((r[5] - m) ** 2 for r in rows if fold[r[1]] == k)
    return se / len(rows)


def main() -> None:
    rad, zone, fc_tmax, truth = load()
    x_abs = lambda r: (r[2] - r[3]) / r[4]
    x_pct = lambda r: 100.0 * (r[2] - r[3]) / r[3]
    table, compare = {}, {}
    for hour in HOURS:
        rows = rows_for(hour, rad, zone, fc_tmax, truth)
        gated = [r for r in rows if r[3] >= 60.0 * r[4]]
        compare[str(hour)] = {
            "all_rows": len(rows), "v1_rows": len(gated),
            "v1_sample": {"mse_v1_pct": round(cv_mse(gated, x_pct, PCT_CLIP), 4),
                          "mse_v2_abs": round(cv_mse(gated, x_abs, X_CLIP), 4),
                          "mse_mean_only": round(cv_mse_mean(gated), 4)},
            "all_sample": {"mse_v2_abs": round(cv_mse(rows, x_abs, X_CLIP), 4),
                           "mse_mean_only": round(cv_mse_mean(rows), 4)},
        }
        sample = [(x_abs(r), r[5]) for r in rows]
        coef = two_slope(sample, X_CLIP)
        ybar = sum(y for _, y in sample) / len(sample)
        sst = sum((y - ybar) ** 2 for _, y in sample)
        sse = sum((y - predict(coef, x, X_CLIP)) ** 2 for x, y in sample)
        random.seed(hour)
        boot = [two_slope([random.choice(sample) for _ in sample], X_CLIP) for _ in range(400)]
        ci = lambda i: [round(100 * sorted(c[i] for c in boot)[10], 3), round(100 * sorted(c[i] for c in boot)[389], 3)]
        points = []
        for lo, hi in BINS:
            sel = [(x, y) for x, y in sample if lo <= x < hi]
            if len(sel) < 20:
                continue
            my = sum(y for _, y in sel) / len(sel)
            se = math.sqrt(sum((y - my) ** 2 for _, y in sel) / (len(sel) - 1) / len(sel))
            points.append({"wm2": round(sum(x for x, _ in sel) / len(sel), 1), "shift_c": round(my, 2),
                           "ci95_c": round(1.96 * se, 2), "n": len(sel)})
        table[str(hour)] = {"n": len(sample), "stations": len({r[0] for r in rows}),
                            "intercept_c": round(coef[0], 3),
                            "dark_c_per_100wm2": round(100 * coef[1], 3), "dark_ci95": ci(1),
                            "bright_c_per_100wm2": round(100 * coef[2], 3), "bright_ci95": ci(2),
                            "r2": round(1 - sse / sst, 3), "resid_sd_c": round(math.sqrt(sse / len(sample)), 2),
                            "points": points}
        t, c = table[str(hour)], compare[str(hour)]
        print(hour, t["n"], t["intercept_c"], t["dark_c_per_100wm2"], t["dark_ci95"], t["bright_c_per_100wm2"],
              t["bright_ci95"], t["r2"], json.dumps(c))
    doc = {
        "schema": "sun_tmax_shift_v2",
        "fitted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": [FIRST, LAST],
        "x_clip_wm2": X_CLIP,
        "model": "shift_c = intercept_c + dark/100*min(x,0) + bright/100*max(x,0), x clipped to +-x_clip_wm2",
        "x": "mean over local hour-ending stamps 05..H of (satellite - day-ahead forecast) shortwave, W/m²; no sunshine gate",
        "y": "settled Tmax - day-ahead forecast Tmax (Open-Meteo previous_day1), station-demeaned",
        "lineage": "docs/research/sun_tmax_shift_abs_20261017/fit.py; Open-Meteo aggregator_republisher, display only",
        "versus_v1": compare,
        "by_last_hour": table,
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
