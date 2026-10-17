"""Morning cloud against the day-ahead forecast -> the day's maximum
(熊先生 2026-10-17 「云 · 实况对比预报：计算拟合一个对于温度的∆数据」).

Per station-day and last local hour H (9..17): x = mean over the local hours
05..H that have both of (observed cloud cover - day-ahead forecast total
cloud ``cloud_cover_previous_day1``), in tenths (成).  Observed cover is the
JAXA Himawari cloud product's cover where it sees the station ("sat"), else
the METAR's reported cover nearest the hour ("report").  y = settled Tmax -
max of ``temperature_2m_previous_day1`` over the local day, station-demeaned.
Two slopes (clearer / cloudier) per source, x clipped to +-X_CLIP, bootstrap
95% intervals; out of sample (five folds by event day) against the mean
alone, and, where satellite sunshine is in hand, whether cloud adds to the
sunshine line (docs/research/sun_tmax_shift_abs_20261017).

Inputs already on disk: jaxa_cloud_rad_p1_20261015 (frames with the
day-ahead hourly forecast, JAXA cloud), the archive's METAR (read-only),
the SSOT's settled truth (read-only).  No fetch.  Display only.
"""
from __future__ import annotations

import gzip
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
JX = HERE.parent / "jaxa_cloud_rad_p1_20261015"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE.parent / "sat_cloud_obs_20261008"))
from bayes_basket_target.metar_path_state import parse_metar_physical_state  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
ARCHIVE = ROOT / "data" / "archive.db"
HOURS = range(9, 18)
X_CLIP = 6.0            # 成
MIN_STATION_DAYS = 8
FOLDS = 5
OUT = HERE / "results.json"


def utc(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def two_slope(sample, clip):
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


def pred(c, x, clip):
    return c[0] + c[1] * min(max(x, -clip), 0.0) + c[2] * max(min(x, clip), 0.0)


def load():
    frames = {}
    for line in gzip.open(JX / "rows_gsmap.jsonl.gz", "rt"):
        r = json.loads(line)
        frames.setdefault((r["station"], r["day"]), r)
    clp = defaultdict(list)
    for path in sorted((JX / "jaxa").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("state") != "ok" or rec["kind"] != "clp":
                continue
            for st, v in rec["values"].items():
                if isinstance(v, dict) and v.get("state") == "ok" and v.get("cover") is not None:
                    clp[st].append((utc(rec["hour"]), float(v["cover"])))
    con = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=30)
    truth = {(st, d): float(v) for st, d, v in con.execute(
        "SELECT station_id, local_date, official_tmax_c FROM calibration_station_daily_high_truth t"
        " WHERE qc_status='ACCEPTED' AND source_lane IN ('AWC','SETTLEMENT_SOURCE')"
        " AND revision_no=(SELECT MAX(revision_no) FROM calibration_station_daily_high_truth x"
        "  WHERE x.station_id=t.station_id AND x.local_date=t.local_date AND x.source_lane IN ('AWC','SETTLEMENT_SOURCE'))")}
    con.close()
    return frames, clp, truth


def hourly_cover(frames, clp):
    """(station, day) -> {source, cover[h] (0..1 or None), fc_cloud[h] (%), fc_tmax}."""
    arc = sqlite3.connect(f"file:{ARCHIVE}?mode=ro", uri=True, timeout=30)
    out = {}
    for (st, day), fr in frames.items():
        zone = ZoneInfo(fr["tz"])
        fc = fr["fc"]
        if len(fc) < 24 or any(x.get("t") is None for x in fc):
            continue
        cover = [None] * 24
        if st in clp:
            source = "sat"
            for at, c in clp[st]:
                lh = at.astimezone(zone)
                if lh.date().isoformat() == day:
                    cover[lh.hour] = c
        else:
            source = "report"
            d0 = datetime.fromisoformat(day).replace(tzinfo=zone)
            lo, hi = d0.astimezone(timezone.utc), (d0 + timedelta(days=1)).astimezone(timezone.utc)
            best = {}
            for va, raw, temp in arc.execute(
                    "SELECT valid_at, raw_text, reported_temperature_c FROM reported_trajectory_observations"
                    " WHERE station_id=? AND valid_at>=? AND valid_at<? AND raw_text<>''",
                    (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"))):
                try:
                    ps = parse_metar_physical_state(raw, expected_temperature_c=None if temp is None else float(temp))
                except Exception:  # noqa: BLE001
                    continue
                if ps.cloud_cover_fraction is None:
                    continue
                t = utc(va).astimezone(zone)
                h = (t + timedelta(minutes=30)).hour if (t + timedelta(minutes=30)).date() == t.date() else None
                if h is None:
                    continue
                gap = abs((t - t.replace(minute=0, second=0, microsecond=0)).total_seconds() - (3600 if t.minute >= 30 else 0))
                if h not in best or gap < best[h][0]:
                    best[h] = (gap, float(ps.cloud_cover_fraction))
            for h, (_, c) in best.items():
                cover[h] = c
        out[(st, day)] = {"source": source, "cover": cover, "fc_cloud": [x.get("cloud") for x in fc],
                          "fc_sw": [x.get("sw") or 0.0 for x in fc], "fc_tmax": max(x["t"] for x in fc)}
    arc.close()
    return out


def rows_for(hour, days, truth, sun):
    rows = []
    for (st, day), d in days.items():
        if (st, day) not in truth:
            continue
        diffs = [10.0 * (d["cover"][h] - d["fc_cloud"][h] / 100.0) for h in range(5, hour + 1)
                 if d["cover"][h] is not None and d["fc_cloud"][h] is not None]
        if len(diffs) < max(2, (hour - 5) // 2):
            continue
        rows.append({"st": st, "day": day, "source": d["source"], "x": sum(diffs) / len(diffs),
                     "y": truth[(st, day)] - d["fc_tmax"], "sun": sun.get((st, day, hour))})
    by = defaultdict(list)
    for r in rows:
        by[r["st"]].append(r)
    out = []
    for st, rs in by.items():
        if len(rs) < MIN_STATION_DAYS:
            continue
        my = sum(r["y"] for r in rs) / len(rs)
        for r in rs:
            r["y"] -= my
            out.append(r)
    return out


def sun_index(days):
    """(station, day, H) -> mean W/m² (satellite - day-ahead forecast) over 05..H, the sunshine line's x."""
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("sun_abs_fit", HERE.parent / "sun_tmax_shift_abs_20261017" / "fit.py")
    S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)
    out = {}
    radd, zone, fc_tmax, truth = S.load()
    for h in HOURS:
        for r in S.rows_for(h, radd, zone, fc_tmax, truth):
            out[(r[0], r[1], h)] = (r[2] - r[3]) / r[4]
    return out


def cv(rows, xf, clip):
    days = sorted({r["day"] for r in rows})
    fold = {d: i % FOLDS for i, d in enumerate(days)}
    se, sm = {}, {}
    for k in range(FOLDS):
        tr = [r for r in rows if fold[r["day"]] != k]
        c = two_slope([(xf(r), r["y"]) for r in tr], clip)
        m = sum(r["y"] for r in tr) / len(tr)
        for r in rows:
            if fold[r["day"]] == k:
                se[(r["st"], r["day"])] = (r["y"] - pred(c, xf(r), clip)) ** 2
                sm[(r["st"], r["day"])] = (r["y"] - m) ** 2
    return se, sm


def boot(keys, a, b):
    by = defaultdict(list)
    for k in keys:
        by[k[1]].append(a[k] - b[k])
    days = sorted(by)
    random.seed(7)
    est = []
    for _ in range(1000):
        pick = [random.choice(days) for _ in days]
        vals = [v for d in pick for v in by[d]]
        est.append(sum(vals) / len(vals))
    est.sort()
    allv = [a[k] - b[k] for k in keys]
    return round(sum(allv) / len(allv), 4), round(est[25], 4), round(est[974], 4)


def joint_cv(rows):
    """Where sunshine is in hand: y ~ sun line alone vs sun + cloud (both two-slope, additive)."""
    rows = [r for r in rows if r["sun"] is not None]
    days = sorted({r["day"] for r in rows})
    fold = {d: i % FOLDS for i, d in enumerate(days)}
    import numpy as np  # noqa: PLC0415

    def X(r, with_cloud):
        s = max(-300.0, min(300.0, r["sun"]))
        v = [1.0, min(s, 0.0), max(s, 0.0)]
        if with_cloud:
            c = max(-X_CLIP, min(X_CLIP, r["x"]))
            v += [min(c, 0.0), max(c, 0.0)]
        return v
    errs = {False: {}, True: {}}
    for k in range(FOLDS):
        tr = [r for r in rows if fold[r["day"]] != k]
        for wc in (False, True):
            A = np.array([X(r, wc) for r in tr]); y = np.array([r["y"] for r in tr])
            beta = np.linalg.lstsq(A, y, rcond=None)[0]
            for r in rows:
                if fold[r["day"]] == k:
                    errs[wc][(r["st"], r["day"])] = (r["y"] - float(np.array(X(r, wc)) @ beta)) ** 2
    return len(rows), boot(list(errs[True]), errs[True], errs[False])


def main() -> None:
    frames, clp, truth = load()
    days = hourly_cover(frames, clp)
    sun = sun_index(days)
    table, checks = {}, {}
    xf = lambda r: r["x"]
    for hour in HOURS:
        rows = rows_for(hour, days, truth, sun)
        entry = {}
        for src in ("sat", "report"):
            sample = [r for r in rows if r["source"] == src]
            if len(sample) < 100:
                continue
            c = two_slope([(r["x"], r["y"]) for r in sample], X_CLIP)
            ybar = sum(r["y"] for r in sample) / len(sample)
            sst = sum((r["y"] - ybar) ** 2 for r in sample)
            sse = sum((r["y"] - pred(c, r["x"], X_CLIP)) ** 2 for r in sample)
            random.seed(hour)
            bs = [two_slope([(r["x"], r["y"]) for r in (random.choice(sample) for _ in sample)], X_CLIP) for _ in range(400)]
            ci = lambda i: [round(sorted(b[i] for b in bs)[10], 3), round(sorted(b[i] for b in bs)[389], 3)]
            se, sm = cv(sample, xf, X_CLIP)
            entry[src] = {"n": len(sample), "stations": len({r["st"] for r in sample}), "intercept_c": round(c[0], 3),
                          "clear_c_per_tenth": round(c[1], 3), "clear_ci95": ci(1),
                          "cloudy_c_per_tenth": round(c[2], 3), "cloudy_ci95": ci(2),
                          "r2": round(1 - sse / sst, 3), "resid_sd_c": round(math.sqrt(sse / len(sample)), 2),
                          "cv_vs_mean_only": boot(list(se), se, sm)}
        table[str(hour)] = entry
        n_joint, add = joint_cv(rows)
        checks[str(hour)] = {"rows_with_sun": n_joint, "cloud_added_to_sun_mse": add}
        print(hour, json.dumps(entry, ensure_ascii=False), "| cloud beyond sun:", n_joint, add, flush=True)
    doc = {
        "schema": "cloud_tmax_shift_v1",
        "fitted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": [min(d for _, d in days), max(d for _, d in days)],
        "x_clip_tenths": X_CLIP,
        "model": "shift_c = intercept_c + clear*min(x,0) + cloudy*max(x,0), x in tenths clipped to +-x_clip_tenths",
        "x": "mean over local hours 05..H of (observed cover - day-ahead forecast total cloud), tenths; observed = JAXA cloud product (sat) else METAR cover (report)",
        "y": "settled Tmax - day-ahead forecast Tmax (Open-Meteo previous_day1), station-demeaned",
        "lineage": "docs/research/cloud_tmax_shift_20261017/fit.py; Open-Meteo aggregator_republisher, display only",
        "cloud_beyond_sun": checks,
        "by_last_hour": table,
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
