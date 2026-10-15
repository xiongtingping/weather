"""PROTOCOL.md: P0 (and P0-M) two-side shrink toward symmetric around its own mode."""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs/research/obs_minus_forecast_station_direction_20261015"))
import analyze as A  # noqa: E402

INPUTS = ROOT / "docs/research/p1_forward_physics_20261015/inputs.db"
FIXED = [0.1, 0.2, 0.3]
GRID = [0.0, 0.1, 0.2, 0.3, 0.5]
MIN_TRAIN_DAYS = 10
MIN_STATION_DAYS = 8


def utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def shrink(p, lam):
    m = int(np.argmax(p))  # first max = lower rung on a tie
    A_, B_ = p[m + 1:].sum(), p[:m].sum()
    if lam == 0 or A_ <= 0 or B_ <= 0:
        return p
    R = A_ + B_
    a2 = R * ((1 - lam) * A_ / R + lam / 2)
    q = p.copy()
    q[m + 1:] *= a2 / A_
    q[:m] *= (R - a2) / B_
    return q


def load(stage):
    db = sqlite3.connect(INPUTS)
    tz = dict(db.execute("select station_id, settlement_timezone from tz"))
    truth = {}
    for st, d, t, rev in db.execute("select station_id, local_date, official_tmax_c, revision_no from truth order by revision_no"):
        truth[(st, d)] = float(t)
    ladder = {}
    for st, d, ij in db.execute("select station_id, event_date, intervals_json from p1 where intervals_json is not null"):
        ladder.setdefault((st, d), json.loads(ij))
    latest = {}
    for ck, sg, st, d, dec, pj in db.execute(
            "select checkpoint_id, stage, station_id, event_date, decision_at, probabilities_json from root where stage=? and event_date between '2026-09-01' and '2026-10-06'", (stage,)):
        key = (st, d, dec[:13])
        if key not in latest or dec > latest[key][0]:
            latest[key] = (dec, pj)
    rows, drop = [], defaultdict(int)
    for (st, d, _), (dec, pj) in latest.items():
        if (st, d) not in truth:
            drop["no_truth"] += 1; continue
        iv = ladder.get((st, d))
        p = json.loads(pj)
        if not iv or set(iv) != set(p):
            drop["ladder"] += 1; continue
        order, c, w = A.centres(iv)
        T = truth[(st, d)]
        ti = next((i for i, b in enumerate(order) if (iv[b][0] is None or iv[b][0] <= T) and (iv[b][1] is None or T < iv[b][1])), None)
        if ti is None:
            drop["truth_bucket"] += 1; continue
        v = np.array([float(p[b]) for b in order])
        v = v / v.sum()
        start = datetime.fromisoformat(d).replace(tzinfo=ZoneInfo(tz[st])).astimezone(timezone.utc)
        h = (start - utc(dec)).total_seconds() / 3600
        rows.append({"station": st, "day": d, "order": order, "c": c, "w": w, "p": v, "ti": ti, "T": T, "h": h,
                     "unit": "F" if "°F" in order[0] else "C"})
    return rows, dict(drop)


def run(stage):
    recs, drop = load(stage)
    n = len(recs)
    st = np.array([r["station"] for r in recs])
    days = np.array([r["day"] for r in recs])
    h = np.array([r["h"] for r in recs])
    unit = np.array([r["unit"] for r in recs])
    lean = np.array([abs(r["p"][int(np.argmax(r["p"])) + 1:].sum() - r["p"][:int(np.argmax(r["p"]))].sum()) for r in recs])
    base = np.array([A.score(r["p"], r) for r in recs])
    dd = {lam: np.array([A.score(shrink(r["p"], lam), r) for r in recs]) - base for lam in GRID if lam > 0}
    dd[0.0] = np.zeros_like(base)
    day_ids = np.unique(days)
    test_days = [d for d in day_ids if (day_ids < d).sum() >= MIN_TRAIN_DAYS]
    wf = np.full((n, 3), np.nan)
    chosen = defaultdict(list)
    for d in test_days:
        prior = days < d
        fleet_lam = min(GRID, key=lambda l: dd[l][prior, 0].mean())
        for s in set(st[days == d]):
            ps = prior & (st == s)
            lam = min(GRID, key=lambda l: dd[l][ps, 0].mean()) if len(set(days[ps])) >= MIN_STATION_DAYS else fleet_lam
            m = (days == d) & (st == s)
            wf[m] = dd[lam][m]
            chosen[s].append(lam)
    sd_key = np.array([r["station"] + "|" + r["day"] for r in recs])
    sd_ids, sd_idx = np.unique(sd_key, return_inverse=True)
    di, day_idx = np.unique(days, return_inverse=True)
    sd_day = np.zeros(len(sd_ids), int)
    sd_day[sd_idx] = day_idx
    nd = len(di)
    draws = np.random.default_rng(7).multinomial(nd, np.full(nd, 1.0 / nd), size=2000)
    tested = np.isin(days, test_days)
    slices = {"all": tested, "h>36": tested & (h > 36), "h12_36": tested & (h > 12) & (h <= 36), "h0_12": tested & (h > 0) & (h <= 12),
              "same_day": tested & (h <= 0), "C": tested & (unit == "C"), "F": tested & (unit == "F")}
    out = {"stage": stage, "rows": n, "dropped": drop, "tested_rows": int(tested.sum()), "mean_abs_lean": float(lean.mean()), "arms": {}}
    arms = {f"fixed_{l}": dd[l] for l in FIXED}
    arms["station_walk_forward"] = wf
    for name, arr in arms.items():
        res = {}
        for sname, sel in slices.items():
            res[sname] = {m: A.boot_sd_day(arr[:, k], sd_idx, sd_day, nd, draws, sel) for k, m in enumerate(("dlog", "dcrps", "dbrier"))}
        per = {}
        for s in sorted(set(st)):
            b = A.boot_sd_day(arr[:, 0], sd_idx, sd_day, nd, draws, tested & (st == s))
            if b:
                sdays = sorted(set(days[tested & (st == s)]))
                mid = sdays[len(sdays) // 2]
                b["halves"] = [float(np.nanmean(arr[tested & (st == s) & (days < mid), 0])), float(np.nanmean(arr[tested & (st == s) & (days >= mid), 0]))]
                per[s] = b
        passed = A.bh({s: v["p"] for s, v in per.items() if v["station_days"] >= 10})
        out["arms"][name] = {"slices": res, "stations": {s: {**v, "bh_q10": s in passed} for s, v in per.items()},
                             "better_bh": sorted(s for s in passed if per[s]["point"] < 0), "worse_bh": sorted(s for s in passed if per[s]["point"] > 0)}
    out["lambda_chosen_last_by_station"] = {s: v[-1] for s, v in chosen.items()}
    return out


def main():
    allout = {}
    for stage in ("P0", "P0-M"):
        o = run(stage)
        allout[stage] = o
        print(f"== {stage} rows {o['rows']} tested {o['tested_rows']} dropped {o['dropped']} mean|lean| {o['mean_abs_lean']:.3f}")
        for name, a in o["arms"].items():
            line = " ".join(f"{s}:{v['dlog']['point']:+.4f}[{v['dlog']['lo']:+.4f},{v['dlog']['hi']:+.4f}]" for s, v in a["slices"].items() if v["dlog"])
            print(f"  {name:22s} {line}")
            f = a["slices"]["all"]
            print(f"  {'':22s} CRPS {f['dcrps']['point']:+.4f}[{f['dcrps']['lo']:+.4f},{f['dcrps']['hi']:+.4f}] Brier {f['dbrier']['point']:+.4f}[{f['dbrier']['lo']:+.4f},{f['dbrier']['hi']:+.4f}] BH+ {a['better_bh']} BH- {a['worse_bh']}")
        if stage == "P0":
            print("  per station (station walk-forward):")
            for s, v in sorted(o["arms"]["station_walk_forward"]["stations"].items(), key=lambda kv: kv[1]["point"]):
                print(f"   {s} sd{v['station_days']:3d} {v['point']:+.4f} [{v['lo']:+.4f},{v['hi']:+.4f}] halves {v['halves'][0]:+.4f}/{v['halves'][1]:+.4f} {'BH*' if v['bh_q10'] else ''} lam {o['lambda_chosen_last_by_station'].get(s)}")
    json.dump(allout, open(HERE / "readings.json", "w"), indent=1, default=float)


if __name__ == "__main__":
    main()
