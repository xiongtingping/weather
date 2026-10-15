"""Walk-forward test: JAXA cloud / sunshine against the forecast as a P1
likelihood (user 2026-10-15 「重开重测」, after 「探讨云雨辐射作为强似然进p1」).

Baseline: the P1 vector production served (the last of each local hour
07..19), 17 Himawari-disk stations, 09-01..10-14, floored as served.

At an anchor t only JAXA files whose FTP modification time is <= t count:

* A  sunshine deficit  D = (F - O) / max(F, 200 n)  over the newest two
     Level 3 hours (begun within 4 h), F the Open-Meteo day-ahead hourly
     shortwave for the same hours; positive = darker than forecast.
* B  thinner-than-forecast cloud  C = Fc - E  at the newest Level 2 scan
     (within 3 h), E = cover * (1 - exp(-COT / 10)) the scan's effective
     cloud, Fc the day-ahead total cloud for that hour; positive = less or
     thinner cloud than forecast.

Tilts, signs fixed by physics, strengths fitted (>= 0):

    A:  P1(k) * exp(-bA * D * rise_k)        C:  both
    B:  P1(k) * exp(+bB * C * rise_k)

rise_k = max(0, rep_k - running high) in C.  Candidate floored once (1e-3).
For each test day (after MIN_TRAIN days) the strengths are fitted on every
earlier day (station-days weighted equally, mean LogScore) and the day is
scored.  Charter 240: LogScore leads, Brier and ladder CRPS guard; 95 %
intervals by event-day block bootstrap; local 12-18 reported apart.
Fixed strengths 1 and 2 are reported as the 「强」 stress.
"""

from __future__ import annotations

import gzip
import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import minimize

HERE = Path(__file__).resolve().parent
FLOOR = 1e-3
MIN_TRAIN_DAYS = 12
COT_SCALE = 10.0


def utc(s: str) -> datetime:
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def floor_norm(v: np.ndarray) -> np.ndarray:
    v = np.maximum(v, 0.0)
    v = v / v.sum()
    v = np.maximum(v, FLOOR)
    return v / v.sum()


def load_jaxa() -> tuple[dict, dict]:
    """swr[(station, hour_start)] = (W/m2, mtime); clp[(station, scan)] = (reading, mtime)."""

    swr, clp = {}, {}
    for path in sorted((HERE / "jaxa").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("state") != "ok":
                continue
            hour, mtime = utc(rec["hour"]), utc(rec["mtime_utc"])
            for st, v in rec["values"].items():
                if rec["kind"] == "swr" and v is not None:
                    swr[(st, hour)] = (float(v), mtime)
                elif rec["kind"] == "clp" and isinstance(v, dict) and v.get("state") == "ok":
                    clp[(st, hour)] = (v, mtime)
    return swr, clp


def fc_index(when_utc: datetime, zone: ZoneInfo, day: str) -> int | None:
    """Index of the Open-Meteo hour-ending stamp whose hour holds when_utc."""

    local = when_utc.astimezone(zone)
    if local.date().isoformat() != day:
        return None
    return min(23, local.hour + 1) if local.minute or local.second else local.hour


def prepare(rows, swr, clp):
    out = []
    for r in rows:
        iv = r["intervals"]
        if not iv:
            continue
        buckets = sorted(iv, key=lambda b: (iv[b][0] if iv[b][0] is not None else -1e9))
        lo = np.array([iv[b][0] if iv[b][0] is not None else -1e9 for b in buckets], float)
        hi = np.array([iv[b][1] if iv[b][1] is not None else 1e9 for b in buckets], float)
        p = np.array([float(r["p1"].get(b, 0.0)) for b in buckets])
        if p.sum() <= 0:
            continue
        k = next((i for i in range(len(buckets)) if lo[i] - 1e-6 <= r["truth"] < hi[i] - 1e-6), None)
        if k is None:
            continue
        width = np.nanmedian([h - l for l, h in zip(lo, hi) if abs(l) < 1e8 and abs(h) < 1e8]) or 1.0
        rep = np.where(lo < -1e8, hi - width / 2, np.where(hi > 1e8, lo + width / 2, (lo + hi) / 2))
        rise = np.maximum(0.0, rep - float(r["running_high"]))
        t = utc(r["decided_at"])
        zone = ZoneInfo(r["tz"])
        fc = r["fc"]
        st = r["station"]
        # A: the newest two admitted hours begun within 4 h.
        D = None
        cand = []
        for back in range(0, 6):
            h0 = (t - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
            got = swr.get((st, h0))
            if got and got[1] <= t and h0 >= t - timedelta(hours=4):
                cand.append((h0, got[0]))
            if len(cand) == 2:
                break
        if len(cand) == 2:
            f_sum = o_sum = 0.0
            ok = True
            for h0, obs in cand:
                i = fc_index(h0 + timedelta(minutes=30), zone, r["day"])
                if i is None or fc[i]["sw"] is None:
                    ok = False
                    break
                f_sum += float(fc[i]["sw"]); o_sum += obs
            if ok and f_sum >= 100.0:
                D = max(-1.0, min(1.0, (f_sum - o_sum) / max(f_sum, 400.0)))
        # B: the newest admitted scan within 3 h.
        C = None
        for back in range(0, 4):
            s0 = (t - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
            got = clp.get((st, s0))
            if got and got[1] <= t and s0 >= t - timedelta(hours=3):
                v = got[0]
                i = fc_index(s0, zone, r["day"])
                if i is not None and fc[i]["cloud"] is not None and v.get("cover") is not None:
                    cot = v.get("optical_thickness")
                    eff = 0.0 if not v["cover"] else v["cover"] * (1 - math.exp(-(cot or 0.0) / COT_SCALE))
                    C = float(fc[i]["cloud"]) / 100.0 - eff
                break
        out.append({"station": st, "day": r["day"], "hour": r["local_hour"], "p": floor_norm(p), "k": k,
                    "rise": rise, "D": D, "C": C})
    return out



def tilted(x, bA, bB):
    e = np.zeros_like(x["rise"])
    if x["D"] is not None:
        e -= bA * x["D"] * x["rise"]
    if x["C"] is not None:
        e += bB * x["C"] * x["rise"]
    return floor_norm(x["p"] * np.exp(np.clip(e, -30, 30)))


def scores(v, k):
    log = -math.log(v[k])
    brier = float(((v - np.eye(len(v))[k]) ** 2).sum())
    cdf = np.cumsum(v)
    step = (np.arange(len(v)) >= k).astype(float)
    crps = float(((cdf - step) ** 2).sum())   # ladder RPS in bucket steps
    return log, brier, crps


def mean_log(rows, bA, bB):
    by = defaultdict(list)
    for x in rows:
        by[(x["station"], x["day"])].append(-math.log(tilted(x, bA, bB)[x["k"]]))
    return float(np.mean([np.mean(v) for v in by.values()]))


def fit(rows, arm):
    if arm == "A":
        f = lambda z: mean_log(rows, z[0], 0.0)
        res = minimize(f, [0.2], bounds=[(0, 8)], method="L-BFGS-B")
        return float(res.x[0]), 0.0
    if arm == "B":
        f = lambda z: mean_log(rows, 0.0, z[0])
        res = minimize(f, [0.2], bounds=[(0, 8)], method="L-BFGS-B")
        return 0.0, float(res.x[0])
    f = lambda z: mean_log(rows, z[0], z[1])
    res = minimize(f, [0.2, 0.2], bounds=[(0, 8), (0, 8)], method="L-BFGS-B")
    return float(res.x[0]), float(res.x[1])


def main() -> None:
    rows = [json.loads(l) for l in gzip.open(HERE / "rows.jsonl.gz", "rt")]
    swr, clp = load_jaxa()
    data = prepare(rows, swr, clp)
    days = sorted({x["day"] for x in data})
    print(f"rows {len(data)} days {len(days)} with A {sum(x['D'] is not None for x in data)}"
          f" with B {sum(x['C'] is not None for x in data)}", flush=True)
    arms = {"A": [], "B": [], "C": []}
    fixed = {"A1": (1.0, 0.0), "A2": (2.0, 0.0), "B1": (0.0, 1.0), "B2": (0.0, 2.0)}
    per = []   # (day, station, hour, arm, dlog, dbrier, dcrps)
    fits = {}
    for i, day in enumerate(days):
        if i < MIN_TRAIN_DAYS:
            continue
        train = [x for x in data if x["day"] < day]
        test = [x for x in data if x["day"] == day]
        fits[day] = {arm: fit(train, arm) for arm in arms}
        # Bc (added after the in-sample scan, exploratory): the forecast's total
        # cloud counts thin cirrus as full cover, so C carries a fleet offset
        # (mean +0.18); centre it on the training days' mean only.
        cs = [x["C"] for x in train if x["C"] is not None]
        centre = float(np.mean(cs)) if cs else 0.0
        shift = lambda rows: [dict(x, C=None if x["C"] is None else x["C"] - centre) for x in rows]
        fits[day]["Bc"] = fit(shift(train), "B")
        fits[day]["Bc_centre"] = (centre, 0.0)
        for x in test:
            base = scores(x["p"], x["k"])
            xc = shift([x])[0]
            for arm, (bA, bB) in list(fits[day].items()) + list(fixed.items()):
                if arm == "Bc_centre":
                    continue
                cand = scores(tilted(xc if arm == "Bc" else x, bA, bB), x["k"])
                per.append((day, x["station"], x["hour"], arm, *(c - b for c, b in zip(cand, base))))
        print(day, {a: tuple(round(v, 3) for v in b) for a, b in fits[day].items()}, flush=True)
    report = summarise(per)
    report["fits"] = fits
    (HERE / "walk_forward.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    for line in report["lines"]:
        print(line)


def summarise(per):
    lines, out = [], {}
    rng = random.Random(20261015)
    for arm in ("A", "B", "C", "Bc", "A1", "A2", "B1", "B2"):
        for label, keep in (("all", lambda h: True), ("12-18", lambda h: 12 <= h < 18)):
            sel = [p for p in per if p[3] == arm and keep(p[2])]
            if not sel:
                continue
            by_sd = defaultdict(list)
            for p in sel:
                by_sd[(p[0], p[1])].append(p[4:])
            sd = {key: np.mean(v, axis=0) for key, v in by_sd.items()}
            by_day = defaultdict(list)
            for (day, _st), v in sd.items():
                by_day[day].append(v)
            days = sorted(by_day)
            point = np.mean(list(sd.values()), axis=0)
            boots = []
            for _ in range(2000):
                pick = [rng.choice(days) for _ in days]
                vals = [v for d in pick for v in by_day[d]]
                boots.append(np.mean(vals, axis=0))
            lo, hi = np.percentile(boots, 2.5, axis=0), np.percentile(boots, 97.5, axis=0)
            out[f"{arm}|{label}"] = {"n_station_days": len(sd), "days": len(days),
                                     "dlog": [point[0], lo[0], hi[0]], "dbrier": [point[1], lo[1], hi[1]],
                                     "dcrps": [point[2], lo[2], hi[2]]}
            lines.append(f"{arm:3s} {label:6s} sd={len(sd):4d} dLog {point[0]:+.4f} [{lo[0]:+.4f},{hi[0]:+.4f}]"
                         f"  dBrier {point[1]:+.4f} [{lo[1]:+.4f},{hi[1]:+.4f}]  dRPS {point[2]:+.4f} [{lo[2]:+.4f},{hi[2]:+.4f}]")
    out["lines"] = lines
    return out


if __name__ == "__main__":
    main()
