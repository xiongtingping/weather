"""Relative humidity observed against forecast -> the day's maximum against
the day-ahead forecast (PROTOCOL.md; 熊先生 2026-10-19「卡片增加湿度并拟合」).

Primary x, the card's basis: at local hour H, the routine report's relative
humidity (Magnus, from its temperature and dew point -- the board's
``relative_humidity``) minus the latest forecast's for that hour (percentage
points).  Otherwise the dew study's fit; plus a paired out-of-sample
comparison against the dew-point difference on the same station-days.

    uv run python docs/research/rh_tmax_shift_20261019/fit.py
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

from bayes_basket_target.station_day_board import relative_humidity  # noqa: E402

HOURS = range(9, 18)
X_CLIP = 20.0           # percentage points
OUT = ROOT / "config" / "ui" / "rh_tmax_shift_v1.json"
DEW_FIT = HERE.parent / "dew_tmax_shift_20261019" / "fit.py"


def station_days(frames):
    """(station, day) -> {dew[h] report, fc[h] latest forecast, fc_tmax}."""
    import report_explain as rex  # noqa: PLC0415
    fc_dew = {}
    for path in (HERE / "cache").glob("*.json.gz"):
        body = json.load(gzip.open(path, "rt"))
        fc_dew[path.name.split(".")[0]] = dict(zip(body["hourly"]["time"], body["hourly"]["relative_humidity_2m"]))
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
            parsed = rex.explain_metar(str(raw))
            v = relative_humidity(parsed.get("temp_c"), parsed.get("dew_c"))
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
    ci = lambda i: [round(sorted(b[i] for b in bs)[10], 5), round(sorted(b[i] for b in bs)[389], 5)]  # noqa: E731
    se, sm = C.cv(sample, lambda r: r["x"], X_CLIP)
    return {"n": len(sample), "stations": len({r["st"] for r in sample}), "intercept_c": round(c[0], 3),
            "drier_c_per_pp": round(c[1], 4), "drier_ci95": [round(v, 4) for v in ci(1)], "moister_c_per_pp": round(c[2], 4), "moister_ci95": [round(v, 4) for v in ci(2)],
            "r2": round(1 - sse / sst, 3), "resid_sd_c": round(math.sqrt(sse / len(sample)), 2),
            "cv_vs_mean_only": C.boot(list(se), se, sm)}


def main() -> None:
    frames, _clp, truth = C.load()
    days = station_days(frames)
    sun = C.sun_index(None)               # it reads its own inputs
    C.X_CLIP = X_CLIP                     # joint_cv clips the second regressor with it
    import importlib.util as _u  # noqa: PLC0415
    _s = _u.spec_from_file_location("dew_fit", DEW_FIT)
    D = _u.module_from_spec(_s); _s.loader.exec_module(D)
    dew_days = D.station_days(frames)
    table, secondary, checks, versus_dew = {}, {}, {}, {}
    for hour in HOURS:
        rows = rows_for(hour, days, truth, sun)
        dew_rows = {(r["st"], r["day"]): r for r in D.rows_for(hour, dew_days, truth, sun)}
        both = [r for r in rows if (r["st"], r["day"]) in dew_rows]
        se_rh, _ = C.cv(both, lambda r: r["x"], X_CLIP)
        se_dew, _ = C.cv([dew_rows[(r["st"], r["day"])] for r in both], lambda r: r["x"], D.X_CLIP)
        versus_dew[str(hour)] = {"rows": len(both), "rh_minus_dew_mse": C.boot(list(se_rh), se_rh, se_dew)}
        table[str(hour)] = line(rows, hour)
        secondary[str(hour)] = line(rows_for(hour, days, truth, sun, mean_over=True), hour)
        n_joint, add = C.joint_cv(rows)
        checks[str(hour)] = {"rows_with_sun": n_joint, "rh_added_to_sun_mse": add}
        print(hour, json.dumps(table[str(hour)], ensure_ascii=False), "| mean05..H:", secondary[str(hour)]["cv_vs_mean_only"],
              "| beyond sun:", n_joint, add, flush=True)
    doc = {
        "schema": "rh_tmax_shift_v1",
        "fitted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": [min(d for _, d in days), max(d for _, d in days)],
        "x_clip_pp": X_CLIP,
        "model": "shift_c = intercept_c + drier*min(x,0) + moister*max(x,0), x in percentage points clipped to +-x_clip_pp",
        "x": "at local hour H, report relative humidity (Magnus from its temperature and dew point) - latest forecast relative_humidity_2m for that hour, percentage points",
        "versus_dew": versus_dew,
        "y": "settled Tmax - day-ahead forecast Tmax (Open-Meteo previous_day1), station-demeaned",
        "lineage": "docs/research/rh_tmax_shift_20261019/fit.py; Open-Meteo aggregator_republisher, display only",
        "secondary_mean_05_to_H": secondary,
        "rh_beyond_sun": checks,
        "by_hour": table,
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
