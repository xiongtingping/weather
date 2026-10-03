#!/usr/bin/env python3
"""PROTOCOL.md verdicts: per-station width lambda arms against the fleet 0.75.

    python3 judge.py a     # data A (long replay, rows_a/)
    python3 judge.py b     # data B (current chain, rows_b/)
    python3 judge.py cf    # robustness: conditional_width_lambda_20261003 replay, charter 224 floored scores
    python3 judge.py cu    # same replay, unfloored

Rows carry, per decision, (LogScore, Brier, CRPS) at every lambda of the grid.
Every arm picks one lambda per (station, scoring day) from rows strictly
before it (day <= d-2, truth known by d 00:00Z); the scored table is the
station-day mean of each decision's triple at the picked lambda, as in
``thin_pool_width_long_20261002/judge.py``.  The gate is production's
``center_width_store._proper_score_gate`` (charter 240, station-day clusters).
"""
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
from bayes_basket_target import center_width_store as store  # noqa: E402

DATA = sys.argv[1] if len(sys.argv) > 1 else "a"
CONF = {
    "a": {"rows": "rows_a", "prefix_from": "2026-02-15", "score_from": "2026-04-01", "score_to": "2026-09-30",
          "halves": (("04-06", "2026-04-01", "2026-06-30"), ("07-09", "2026-07-01", "2026-09-30"))},
    "cf": {"rows": "rows_cf", "prefix_from": "2026-07-30", "score_from": "2026-09-01", "score_to": "2026-10-01",
           "halves": (("09-01..09-15", "2026-09-01", "2026-09-15"), ("09-16..10-01", "2026-09-16", "2026-10-01"))},
    "cu": {"rows": "rows_cu", "prefix_from": "2026-07-30", "score_from": "2026-09-01", "score_to": "2026-10-01",
           "halves": (("09-01..09-15", "2026-09-01", "2026-09-15"), ("09-16..10-01", "2026-09-16", "2026-10-01"))},
    "b": {"rows": "rows_b", "prefix_from": "2026-07-30", "score_from": "2026-09-01", "score_to": "2026-10-01",
          "halves": (("09-01..09-15", "2026-09-01", "2026-09-15"), ("09-16..10-01", "2026-09-16", "2026-10-01"))},
}[DATA]
BASE = "0.75"
MIN_STATION_DAYS = 20
KAPPAS = {"S0": 0.0, "S10": 10.0, "S30": 30.0, "S100": 100.0}
PER_STATION = tuple(KAPPAS)
BONFERRONI = 1.0 - 0.05 / len(PER_STATION)
BLOCK, BOOT, SEED = 3, 2000, 20261003


def load():
    rows = []
    for name in sorted(os.listdir(os.path.join(HERE, CONF["rows"]))):
        with open(os.path.join(HERE, CONF["rows"], name)) as handle:
            rows.extend(json.load(handle))
    return [r for r in rows if r["day"] >= CONF["prefix_from"]]


def main():
    rows = load()
    grid = sorted(rows[0]["s"], key=float)
    gi = {g: i for i, g in enumerate(grid)}
    S = np.array([[r["s"][g] for g in grid] for r in rows])          # rows x grid x (nll, brier, crps)
    station = np.array([r["station"] for r in rows])
    day = np.array([r["day"] for r in rows])
    known = np.array([datetime.fromisoformat(r["known"]).timestamp() for r in rows])
    stations = sorted(set(station))
    score_days = sorted({d for d in day if CONF["score_from"] <= d <= CONF["score_to"]})
    # ---- walk-forward picks: pick[arm][(station, day)] = grid label
    pick = {arm: {} for arm in (*PER_STATION, "F")}
    trace = defaultdict(dict)
    for d in score_days:
        cut_day = (date.fromisoformat(d) - timedelta(days=2)).isoformat()
        cut_ts = datetime.fromisoformat(d + "T00:00:00+00:00").timestamp()
        prior = (day <= cut_day) & (known <= cut_ts)
        if not prior.any():
            for arm in pick:
                for s in stations:
                    pick[arm][(s, d)] = BASE
            continue
        fleet = S[prior, :, 0].mean(axis=0)                           # L_F(lambda)
        pick["F"].update({(s, d): grid[int(np.argmin(fleet))] for s in stations})
        for s in stations:
            m = prior & (station == s)
            n_s = len(set(day[m]))
            if n_s < MIN_STATION_DAYS:
                for arm in PER_STATION:
                    pick[arm][(s, d)] = BASE
                continue
            own = S[m, :, 0].mean(axis=0)
            for arm, kappa in KAPPAS.items():
                blended = (n_s * own + kappa * fleet) / (n_s + kappa)
                pick[arm][(s, d)] = grid[int(np.argmin(blended))]
            trace[s][d] = {"n_s": n_s, **{arm: pick[arm][(s, d)] for arm in PER_STATION}}
    # ---- station-day tables
    scored = np.array([CONF["score_from"] <= d <= CONF["score_to"] for d in day])
    idx_by_sd = defaultdict(list)
    for i in np.nonzero(scored)[0]:
        idx_by_sd[(station[i], day[i])].append(i)
    keys = sorted(idx_by_sd)

    def table(arm, subset=None):
        out = {}
        for k in keys:
            idx = idx_by_sd[k] if subset is None else [i for i in idx_by_sd[k] if subset(rows[i])]
            if not idx:
                continue
            lab = BASE if arm == BASE else (arm if arm in gi else pick[arm][k])
            out[k] = tuple(float(x) for x in S[idx, gi[lab], :].mean(axis=0))
        return out

    def gate(cand, base, ks, conf):
        saved = store.GATE_CONFIDENCE
        store.GATE_CONFIDENCE = conf
        try:
            g = store._proper_score_gate([base[k] for k in ks], [cand[k] for k in ks], ks)
        finally:
            store.GATE_CONFIDENCE = saved
        return {f: g[f] for f in ("d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "ci_crps",
                                  "primary_lower", "crps_not_worse", "brier_not_worse", "lower", "station_days",
                                  "confidence")}

    def block_ci(cand, base, ks):
        days = sorted({k[1] for k in ks})
        by_day = defaultdict(list)
        for j, k in enumerate(ks):
            by_day[k[1]].append(j)
        dl = np.array([cand[k][0] - base[k][0] for k in ks])
        rng = np.random.default_rng(SEED)
        n_blocks = math.ceil(len(days) / BLOCK)
        boots = []
        for _ in range(BOOT):
            idx = np.concatenate([by_day[days[(s0 + j) % len(days)]]
                                  for s0 in rng.integers(0, len(days), n_blocks) for j in range(BLOCK)])
            boots.append(float(dl[idx].mean()))
        return [float(dl.mean()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]

    base = table(BASE)
    res = {"data": DATA, "decisions": int(scored.sum()), "station_days": len(keys), "score_days": len(score_days),
           "grid": grid, "bonferroni_confidence": BONFERRONI, "arms": {}}
    strata = {"nf2": lambda r: r["nf"] == 2, "nf3_4": lambda r: 3 <= r["nf"] <= 4, "nf5_6": lambda r: r["nf"] >= 5}
    tables = {arm: table(arm) for arm in (*PER_STATION, "F")}
    for arm in (*PER_STATION, "F"):
        cand = tables[arm]
        conf = BONFERRONI if arm in PER_STATION else 0.95
        a = {"all": gate(cand, base, keys, conf)}
        for name, lo, hi in CONF["halves"]:
            ks = [k for k in keys if lo <= k[1] <= hi]
            a[name] = gate(cand, base, ks, conf)
        a["block3_logscore"] = block_ci(cand, base, keys)
        if arm != "F":
            a["vs_F"] = gate(cand, tables["F"], keys, conf)
        a["strata"] = {}
        for sname, pred in strata.items():
            b_s, c_s = table(BASE, pred), table(arm, pred)
            ks = sorted(set(b_s) & set(c_s))
            if ks:
                a["strata"][sname] = gate(c_s, b_s, ks, conf)
        a["picked"] = dict(sorted(Counter(pick[arm][k] for k in keys).items(), key=lambda kv: float(kv[0])))
        halves_neg = all(a[name]["d_logscore"] < 0 for name, _lo, _hi in CONF["halves"])
        a["passes"] = bool(a["all"]["lower"] and halves_neg and (arm == "F" or a["vs_F"]["d_logscore"] < 0))
        res["arms"][arm] = a
    # ---- in-sample ceiling: each station's best lambda on the scored period itself
    oracle = {}
    for s in stations:
        ks = [k for k in keys if k[0] == s]
        if not ks:
            continue
        best = min(grid, key=lambda g: np.mean([np.mean(S[idx_by_sd[k], gi[g], 0]) for k in ks]))
        oracle[s] = best
        for k in ks:
            pass
    ora = {k: tuple(float(x) for x in S[idx_by_sd[k], gi[oracle[k[0]]], :].mean(axis=0)) for k in keys}
    res["oracle_in_sample"] = {"lambda_by_station": oracle, "gate_95": gate(ora, base, keys, 0.95)}
    # fixed-lambda in-sample curve (fleet), for orientation
    res["fixed_dlog_vs_base"] = {g: round(float(np.mean([np.mean(S[idx_by_sd[k], gi[g], 0]) - np.mean(S[idx_by_sd[k], gi[BASE], 0]) for k in keys])), 5) for g in grid}
    last = score_days[-1]
    res["final_picks"] = {s: trace[s].get(last) for s in stations}
    with open(os.path.join(HERE, f"results_{DATA}.json"), "w") as handle:
        json.dump(res, handle, ensure_ascii=False, indent=1)
    f = lambda v: "%+.4f [%+.4f,%+.4f]" % (v[0], v[1], v[2])  # noqa: E731
    print(f"data {DATA}: {res['decisions']} decisions, {res['station_days']} station-days, {res['score_days']} days")
    print("fixed lambda (in-sample, fleet) dLog vs 0.75:", res["fixed_dlog_vs_base"])
    for arm, a in res["arms"].items():
        x = a["all"]
        print(f"== {arm} passes={a['passes']}  conf={x['confidence']}")
        for part in ("all", *[h[0] for h in CONF["halves"]]):
            y = a[part]
            print(f"   {part:13s} sd={y['station_days']:5d} dLog {y['d_logscore']:+.4f} [{y['ci_logscore'][0]:+.4f},{y['ci_logscore'][1]:+.4f}]"
                  f"  dBrier {y['d_brier']:+.5f} [{y['ci_brier'][0]:+.5f},{y['ci_brier'][1]:+.5f}]"
                  f"  dCRPS {y['d_crps']:+.5f} [{y['ci_crps'][0]:+.5f},{y['ci_crps'][1]:+.5f}]  lower={y['lower']}")
        print("   block3 dLog", f(a["block3_logscore"]))
        if "vs_F" in a:
            y = a["vs_F"]
            print(f"   vs F         dLog {y['d_logscore']:+.4f} [{y['ci_logscore'][0]:+.4f},{y['ci_logscore'][1]:+.4f}]")
        for sname, y in a["strata"].items():
            print(f"   {sname:12s} sd={y['station_days']:5d} dLog {y['d_logscore']:+.4f} [{y['ci_logscore'][0]:+.4f},{y['ci_logscore'][1]:+.4f}]")
        print("   picked", a["picked"])
    o = res["oracle_in_sample"]["gate_95"]
    print(f"== ORACLE (in-sample ceiling) dLog {o['d_logscore']:+.4f} [{o['ci_logscore'][0]:+.4f},{o['ci_logscore'][1]:+.4f}]")
    print("   oracle lambdas", dict(Counter(res["oracle_in_sample"]["lambda_by_station"].values())))
    print("   final picks", {s: (v or {}).get("S30") for s, v in res["final_picks"].items()})


if __name__ == "__main__":
    main()
