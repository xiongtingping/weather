"""10 m wind observed against forecast -> the day's maximum against the
day-ahead forecast (PROTOCOL.md; 熊先生 2026-10-19「风 · 实况对比预报：这个也拟合一下」).

Primary x, the card's basis: at local hour H, the routine report's wind speed
minus the latest forecast's for that hour (m/s).  Secondary x: the mean over
05..H.  Otherwise the dew study's fit (dew_tmax_shift_20261019/fit.py).

    uv run python docs/research/wind_tmax_shift_20261019/fit.py
"""

from __future__ import annotations

import gzip
import importlib.util
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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("cloud_fit", HERE.parent / "cloud_tmax_shift_20261017" / "fit.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)

HOURS = range(9, 18)
X_CLIP = 4.0            # m/s
OUT = ROOT / "config" / "ui" / "wind_tmax_shift_v1.json"


def station_days(frames):
    """(station, day) -> {dew[h] report wind m/s, fc[h] latest forecast m/s, fc_tmax} (the dew
    study's keys, kept so its rows_for applies unchanged)."""
    import report_explain as rex  # noqa: PLC0415
    fc_dew = {}
    for path in (HERE / "cache").glob("*.json.gz"):
        body = json.load(gzip.open(path, "rt"))
        # Open-Meteo serves km/h by default; the board asks for m/s.
        fc_dew[path.name.split(".")[0]] = dict(zip(body["hourly"]["time"],
                                                   [None if v is None else v / 3.6 for v in body["hourly"]["wind_speed_10m"]]))
    arc = sqlite3.connect(f"file:{C.ARCHIVE}?mode=ro", uri=True, timeout=30)
    out = {}
    for (st, day), fr in frames.items():
        fc = fr["fc"]
        if len(fc) < 24 or any(x.get("t") is None for x in fc) or st not in fc_dew:
            continue
        zone = ZoneInfo(fr["tz"])
        d0 = datetime.fromisoformat(day).replace(tzinfo=zone)
        lo, hi = d0.astimezone(timezone.utc), (d0 + timedelta(days=1)).astimezone(timezone.utc)
        dew = [None] * 24
        for va, raw in arc.execute(
                "SELECT valid_at, raw_text FROM reported_trajectory_observations"
                " WHERE station_id=? AND valid_at>=? AND valid_at<? AND raw_text<>''",
                (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"))):
            t = C.utc(va).astimezone(zone)
            if t.minute > 10:
                continue
            v = rex.explain_metar(str(raw)).get("wind_mps")
            if v is not None:
                dew[t.hour] = float(v)
        out[(st, day)] = {"dew": dew, "fc": [fc_dew[st].get(f"{day}T{h:02d}:00") for h in range(24)],
                          "fc_tmax": max(x["t"] for x in fc)}
    arc.close()
    return out


def rows_for(hour, days, truth, sun, mean_over=False):
    rows = []
    for (st, day), d in days.items():
        if (st, day) not in truth:
            continue
        diffs = [d["dew"][h] - d["fc"][h] for h in range(5, hour + 1) if d["dew"][h] is not None and d["fc"][h] is not None]
        if mean_over:
            if len(diffs) < max(2, (hour - 5) // 2):
                continue
            x = sum(diffs) / len(diffs)
        else:
            if d["dew"][hour] is None or d["fc"][hour] is None:
                continue
            x = d["dew"][hour] - d["fc"][hour]
        rows.append({"st": st, "day": day, "x": x, "y": truth[(st, day)] - d["fc_tmax"], "sun": sun.get((st, day, hour))})
    by = defaultdict(list)
    for r in rows:
        by[r["st"]].append(r)
    out = []
    for rs in by.values():
        if len(rs) < C.MIN_STATION_DAYS:
            continue
        my = sum(r["y"] for r in rs) / len(rs)
        for r in rs:
            r["y"] -= my
            out.append(r)
    return out


def line(sample, seed):
    c = C.two_slope([(r["x"], r["y"]) for r in sample], X_CLIP)
    ybar = sum(r["y"] for r in sample) / len(sample)
    sst = sum((r["y"] - ybar) ** 2 for r in sample)
    sse = sum((r["y"] - C.pred(c, r["x"], X_CLIP)) ** 2 for r in sample)
    random.seed(seed)
    bs = [C.two_slope([(r["x"], r["y"]) for r in (random.choice(sample) for _ in sample)], X_CLIP) for _ in range(400)]
    ci = lambda i: [round(sorted(b[i] for b in bs)[10], 3), round(sorted(b[i] for b in bs)[389], 3)]  # noqa: E731
    se, sm = C.cv(sample, lambda r: r["x"], X_CLIP)
    return {"n": len(sample), "stations": len({r["st"] for r in sample}), "intercept_c": round(c[0], 3),
            "calmer_c_per_mps": round(c[1], 3), "calmer_ci95": ci(1), "windier_c_per_mps": round(c[2], 3), "windier_ci95": ci(2),
            "r2": round(1 - sse / sst, 3), "resid_sd_c": round(math.sqrt(sse / len(sample)), 2),
            "cv_vs_mean_only": C.boot(list(se), se, sm)}


def main() -> None:
    frames, _clp, truth = C.load()
    days = station_days(frames)
    sun = C.sun_index(None)               # it reads its own inputs
    C.X_CLIP = X_CLIP                     # joint_cv clips the second regressor with it
    table, secondary, checks = {}, {}, {}
    for hour in HOURS:
        rows = rows_for(hour, days, truth, sun)
        table[str(hour)] = line(rows, hour)
        secondary[str(hour)] = line(rows_for(hour, days, truth, sun, mean_over=True), hour)
        n_joint, add = C.joint_cv(rows)
        checks[str(hour)] = {"rows_with_sun": n_joint, "wind_added_to_sun_mse": add}
        print(hour, json.dumps(table[str(hour)], ensure_ascii=False), "| mean05..H:", secondary[str(hour)]["cv_vs_mean_only"],
              "| beyond sun:", n_joint, add, flush=True)
    doc = {
        "schema": "wind_tmax_shift_v1",
        "fitted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": [min(d for _, d in days), max(d for _, d in days)],
        "x_clip_mps": X_CLIP,
        "model": "shift_c = intercept_c + calmer*min(x,0) + windier*max(x,0), x in m/s clipped to +-x_clip_mps",
        "x": "at local hour H, routine report 10-min mean wind - latest forecast wind_speed_10m for that hour, m/s",
        "y": "settled Tmax - day-ahead forecast Tmax (Open-Meteo previous_day1), station-demeaned",
        "lineage": "docs/research/wind_tmax_shift_20261019/fit.py; Open-Meteo aggregator_republisher, display only",
        "secondary_mean_05_to_H": secondary,
        "wind_beyond_sun": checks,
        "by_hour": table,
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
