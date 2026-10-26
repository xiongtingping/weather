#!/usr/bin/env python3
"""PROTOCOL 补记一: the served belief shifted by kappa * TX revision step, walk-forward on history.

    python3 docs/research/taf_tx_revision_20261026/usage.py
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import explore as X  # noqa: E402  (bulletins, D: P0_SQL, iso, NGR, TF, rate)

D = X.D
FLOOR = 1e-3
GRID = [round(0.02 * i, 2) for i in range(51)]
SERVED_SQL = (
    "SELECT probabilities_json FROM basket_probability_envelope INDEXED BY basket_probability_envelope_asof_idx"
    " WHERE station_id=? AND event_date=? AND stage=? AND state='COMMITTED' AND decision_at_utc<=?"
    " ORDER BY decision_at_utc DESC LIMIT 1"
)


def bucket_of(value, ordered, intervals):
    for i, k in enumerate(ordered):
        lo, hi = intervals[k]
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return i
    return None


def collect():
    conn = sqlite3.connect(f"file:{D.DB}?mode=ro", uri=True, timeout=120)
    tzs = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM basket_probability_envelope"
        " WHERE event_date='2026-10-20' AND stage='P0' GROUP BY station_id"))
    truth = D.TF.load_truth_revisions(conn)
    events = []
    for (s, day), seq in sorted(X.bulletins(tzs).items()):
        if (s, day) not in truth or not ("2026-08-20" <= day <= "2026-10-24"):
            continue
        y = truth[(s, day)][-1][2]
        prev = None
        for t, tx, _kind, _rel in seq:
            if prev is not None and tx != prev:
                p0 = conn.execute(D.P0_SQL, (s, day, D.iso(t))).fetchone()
                if p0 is not None:
                    _pj, oj, ivj, _cj = p0
                    ordinals, intervals = json.loads(oj), json.loads(ivj or "{}")
                    ordered = sorted(ordinals, key=lambda k: ordinals[k])
                    vec = None
                    for stage in ("P1", "P0-M", "P0"):
                        row = conn.execute(SERVED_SQL, (s, day, stage, D.iso(t))).fetchone()
                        if row is not None:
                            p = json.loads(row[0])
                            if set(p) == set(ordered) and not (set(ordered) - set(intervals)):
                                vec = [float(p[k]) for k in ordered]
                                if stage == "P0":
                                    cj = p0[3]
                                    if cj and '"SERVED"' in cj:
                                        pre = json.loads(cj).get("pre_shift_probabilities")
                                        vec = [float(pre[k]) for k in ordered] if pre else vec
                                break
                    w = bucket_of(y, ordered, intervals)
                    if vec is not None and w is not None and vec[w] > 0:
                        events.append({"station": s, "day": day, "step": tx - prev, "p": vec,
                                       "geom": D.NGR.geometry(ordered, intervals), "winner": w})
            prev = tx
    conn.close()
    return events


def shift(p, geom, delta):
    p = np.asarray(p, float)
    dead = p <= 0.0
    slo, shi = np.asarray(geom["slo"]) + delta, np.asarray(geom["shi"]) + delta
    tlo, thi = np.asarray(geom["tlo"]), np.asarray(geom["thi"])
    lo = np.maximum(slo[:, None], tlo[None, :])
    hi = np.minimum(shi[:, None], thi[None, :])
    q = (p[:, None] * (np.clip(hi - lo, 0, None) / (shi - slo)[:, None])).sum(0)
    q = np.where(dead, 0.0, q)
    if q.sum() <= 0:
        return p / p.sum()
    q = q / q.sum()
    q = np.where(dead, 0.0, np.maximum(q, FLOOR))
    return q / q.sum()


def score(e, delta):
    q = shift(e["p"], e["geom"], delta)
    o = np.zeros_like(q)
    o[e["winner"]] = 1.0
    return {"logscore": float(-math.log(q[e["winner"]])), "brier": float(((q - o) ** 2).sum()),
            "crps": float(((np.cumsum(q) - np.cumsum(o)) ** 2).sum())}


def fit(train, use_sign):
    best = (0.0, None)
    for k in GRID:
        ls = statistics.mean(score(e, k * (np.sign(e["step"]) if use_sign else e["step"]))["logscore"] for e in train)
        if best[1] is None or ls < best[1]:
            best = (k, ls)
    return best[0]


def boot(per_day):
    days = sorted(per_day)
    sums = np.array([sum(per_day[d]) for d in days])
    cnts = np.array([len(per_day[d]) for d in days])
    rng = np.random.default_rng(281)
    picks = rng.integers(0, len(days), (2000, len(days)))
    st = sums[picks].sum(1) / cnts[picks].sum(1)
    return float(np.percentile(st, 2.5)), float(np.percentile(st, 97.5))


def main() -> int:
    events = collect()
    days = sorted({e["day"] for e in events})
    print("events", len(events), "days", len(days), flush=True)
    rows, path = [], []
    for day in days:
        train = [e for e in events if e["day"] < day]
        if len({e["day"] for e in train}) < 7:
            continue
        k, ks = fit(train, False), fit(train, True)
        path.append({"day": day, "kappa_step": k, "kappa_sign": ks})
        for e in (e for e in events if e["day"] == day):
            r = {"day": day, "station": e["station"], "step": e["step"]}
            for arm, delta in (("R", 0.0), ("U", k * e["step"]), ("U_sign", ks * np.sign(e["step"]))):
                for m, v in score(e, delta).items():
                    r[f"{arm}_{m}"] = v
            rows.append(r)
    out = {"n_events": len(events), "kappa_path": path}
    half = sorted({r["day"] for r in rows})[len({r["day"] for r in rows}) // 2]
    for arm in ("U", "U_sign"):
        res = {}
        for name, keep in (("all", lambda r: True), ("first_half", lambda r: r["day"] < half),
                           ("second_half", lambda r: r["day"] >= half), ("step_ge2", lambda r: abs(r["step"]) >= 2)):
            sub = {}
            for m in ("logscore", "crps", "brier"):
                per_day = defaultdict(list)
                for r in rows:
                    if keep(r):
                        per_day[r["day"]].append(r[f"{arm}_{m}"] - r[f"R_{m}"])
                allv = [v for vs in per_day.values() for v in vs]
                ci = boot(per_day)
                sub[m] = {"delta": round(statistics.mean(allv), 5), "ci": [round(ci[0], 5), round(ci[1], 5)]}
                if m == "logscore":
                    sub["n"], sub["n_days"] = len(allv), len(per_day)
                    sub["days_better"] = sum(1 for v in per_day.values() if statistics.mean(v) < 0)
            res[name] = sub
        a = res["all"]
        res["verdict"] = ("INSUFFICIENT" if a["n_days"] < 20 else
                          "PASS" if a["logscore"]["ci"][1] < 0 and a["crps"]["ci"][0] <= 0 and a["brier"]["ci"][0] <= 0 else "REFUSED")
        by_st = defaultdict(list)
        for r in rows:
            by_st[r["station"]].append(r[f"{arm}_logscore"] - r["R_logscore"])
        res["stations"] = {s: (len(v), round(statistics.mean(v), 4)) for s, v in sorted(by_st.items())}
        out[arm] = res
    (HERE / "usage_results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print("kappa path (first, mid, last):", path[0], path[len(path) // 2], path[-1])
    for arm in ("U", "U_sign"):
        r = out[arm]
        print(arm, r["verdict"])
        for name in ("all", "first_half", "second_half", "step_ge2"):
            v = r[name]
            print(f"  {name:12s} LS {v['logscore']['delta']:+.4f} [{v['logscore']['ci'][0]:+.4f},{v['logscore']['ci'][1]:+.4f}]"
                  f" RPS {v['crps']['delta']:+.4f}{v['crps']['ci']} Br {v['brier']['delta']:+.4f}{v['brier']['ci']}"
                  f" n{v['n']} {v['days_better']}/{v['n_days']}d")
        print("  stations:", r["stations"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
