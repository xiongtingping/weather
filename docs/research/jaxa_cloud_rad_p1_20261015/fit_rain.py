"""Walk-forward test of satellite rain (JAXA GSMaP_NOW) against the forecast
as a P1 likelihood -- the rain arm of the 2026-10-15 re-test.

Baseline: served P1 (last of each local hour 07..19), every station GSMaP
covers (``rows_gsmap.jsonl.gz``), 09-01..10-14.  At an anchor t, the newest
two GSMaP hours whose file was published by t (begun within 3 h):

    W = ln(1 + S) - ln(1 + F)

S the satellite rain over those hours (mm, the station's cell; ``Rg`` the
gauge-calibrated rate), F the Open-Meteo day-ahead precipitation for the
same hours; positive = wetter than forecast.  Tilt, sign fixed (wetter ->
less rise still to come), strength fitted >= 0 on earlier days only:

    P1(k) * exp(-b * W * rise_k)

Scored and judged as fit_jaxa.py (charter 240, block bootstrap by event
day, floor once).
"""

from __future__ import annotations

import gzip
import json
import math
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import minimize

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fit_jaxa as base  # noqa: E402

MIN_TRAIN_DAYS = base.MIN_TRAIN_DAYS


def load_gsmap() -> dict:
    out = {}
    for path in sorted((HERE / "gsmap").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("state") != "ok":
                continue
            slot, mtime = base.utc(rec["slot"]), base.utc(rec["mtime_utc"])
            for st, v in rec["values"].items():
                out[(st, slot)] = (v, mtime)
    return out


def prepare(rows, rain, key):
    data = []
    for r in rows:
        prepped = base.prepare([r], {}, {})
        if not prepped:
            continue
        x = prepped[0]
        t = base.utc(r["decided_at"])
        zone = ZoneInfo(r["tz"])
        got = []
        for back in range(0, 5):
            s0 = (t - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
            v = rain.get((r["station"], s0))
            if v and v[1] <= t and s0 >= t - timedelta(hours=3) and v[0].get(key) is not None:
                got.append((s0, float(v[0][key])))
            if len(got) == 2:
                break
        W = None
        if len(got) == 2:
            S = sum(mm for _s, mm in got)
            F = 0.0
            ok = True
            for s0, _mm in got:
                i = base.fc_index(s0 + timedelta(minutes=30), zone, r["day"])
                if i is None or r["fc"][i]["pr"] is None:
                    ok = False
                    break
                F += float(r["fc"][i]["pr"])
            if ok:
                W = math.log1p(S) - math.log1p(F)
        x["W"] = W
        data.append(x)
    return data


def tilted(x, b):
    if x["W"] is None or b == 0.0:
        return x["p"]
    return base.floor_norm(x["p"] * np.exp(np.clip(-b * x["W"] * x["rise"], -30, 30)))


def mean_log(rows, b):
    by = defaultdict(list)
    for x in rows:
        by[(x["station"], x["day"])].append(-math.log(tilted(x, b)[x["k"]]))
    return float(np.mean([np.mean(v) for v in by.values()]))


def main() -> None:
    rows = [json.loads(l) for l in gzip.open(HERE / "rows_gsmap.jsonl.gz", "rt")]
    rain = load_gsmap()
    per, fits = [], {}
    for arm, key in (("R", "mm"), ("Rg", "mm_gc")):
        data = prepare(rows, rain, key)
        days = sorted({x["day"] for x in data})
        Ws = [x["W"] for x in data if x["W"] is not None]
        print(arm, f"rows {len(data)} with W {len(Ws)} wetter {np.mean(np.array(Ws) > 0.05):.3f}"
                   f" drier {np.mean(np.array(Ws) < -0.05):.3f}", flush=True)
        for i, day in enumerate(days):
            if i < MIN_TRAIN_DAYS:
                continue
            train = [x for x in data if x["day"] < day]
            res = minimize(lambda z: mean_log(train, z[0]), [0.2], bounds=[(0, 8)], method="L-BFGS-B")
            b = float(res.x[0])
            fits.setdefault(day, {})[arm] = b
            for x in (y for y in data if y["day"] == day):
                b0 = base.scores(x["p"], x["k"])
                for name, bb in ((arm, b), (arm + "1", 1.0), (arm + "2", 2.0)):
                    c = base.scores(tilted(x, bb), x["k"])
                    per.append((day, x["station"], x["hour"], name, *(u - v for u, v in zip(c, b0))))
        print(arm, "fitted", [round(fits[d][arm], 3) for d in sorted(fits)][::5], flush=True)
    report = summarise(per)
    report["fits"] = fits
    (HERE / "walk_forward_rain.json").write_text(json.dumps(report, indent=1))
    for line in report["lines"]:
        print(line)


def summarise(per):
    return base.summarise(per, arms=("R", "Rg", "R1", "R2", "Rg1", "Rg2"))


if __name__ == "__main__":
    main()
