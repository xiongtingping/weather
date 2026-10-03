#!/usr/bin/env python3
"""PROTOCOL.md verdicts: conditional width lambda arms against the fixed 0.75.

Rows (rows_c/) carry each representative decision's features (h, nf, D,
sigma) and (LogScore, Brier, CRPS) at every lambda of the fine grid.  Every
arm picks a lambda per decision from rows strictly before its scoring day
(day <= d-2, truth known by d 00:00Z); the scored table is the station-day
mean of the picked triples; the gate is production's
``center_width_store._proper_score_gate`` (charter 240, station-day clusters).
"""
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import numpy as np
from scipy.optimize import minimize

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
from bayes_basket_target import center_width_store as store  # noqa: E402

SCORE_FROM, SCORE_TO = "2026-09-01", "2026-10-01"
HALVES = (("09-01..09-15", "2026-09-01", "2026-09-15"), ("09-16..10-01", "2026-09-16", "2026-10-01"))
MIN_DAYS = 8
SCORE_KEY = sys.argv[1] if len(sys.argv) > 1 else "sf"   # sf: charter 224 floored (primary); s: unfloored
ARMS = ("L_h", "L_nf", "L_D", "L_sigma", "L_joint")
CONF = 1.0 - 0.05 / len(ARMS)
BLOCK, BOOT, SEED = 3, 2000, 20261003


def load():
    rows = []
    for name in sorted(os.listdir(os.path.join(HERE, "rows_c"))):
        with open(os.path.join(HERE, "rows_c", name)) as handle:
            rows.extend(json.load(handle))
    return rows


rows = load()
GRID = sorted(rows[0][SCORE_KEY], key=float)
GV = np.array([float(g) for g in GRID])
I75 = GRID.index("0.750")
S = np.array([[r[SCORE_KEY][g] for g in GRID] for r in rows])          # n x grid x 3
N = len(rows)
station = np.array([r["station"] for r in rows])
day = np.array([r["day"] for r in rows])
known = np.array([datetime.fromisoformat(r["known"]).timestamp() for r in rows])
h = np.array([r["h"] for r in rows])
nf = np.array([r["nf"] for r in rows])
D = np.array([r["D"] for r in rows])
sig = np.array([r["sigma"] for r in rows])
hbin = np.array([r["bin"] for r in rows])
nfc = np.where(nf == 2, 0, np.where(nf <= 4, 1, 2))
thin_far = (nf <= 3) & (h > 36.0)
score_days = sorted({d for d in day if SCORE_FROM <= d <= SCORE_TO})


def interp_nll(rows_idx, lam):
    """LogScore at a continuous lambda, linear between grid points."""
    lam = np.clip(lam, GV[0], GV[-1])
    pos = np.interp(lam, GV, np.arange(len(GV)))
    lo = np.floor(pos).astype(int); hi = np.minimum(lo + 1, len(GV) - 1); fr = pos - lo
    a = S[rows_idx, lo, 0]; b = S[rows_idx, hi, 0]
    return a * (1 - fr) + b * fr


def nearest(lam):
    return np.abs(GV[None, :] - np.asarray(lam)[:, None]).argmin(axis=1)


def joint_design(idx, mu_sd):
    (mD, sD), (mS, sS) = mu_sd
    return np.column_stack([np.ones(len(idx)), (nf[idx] <= 3).astype(float), (D[idx] - mD) / sD,
                            (sig[idx] - mS) / sS, h[idx] / 24.0])


pick = {arm: np.full(N, I75) for arm in (*ARMS, "F")}
joint_params = {}
for d in score_days:
    cut = (date.fromisoformat(d) - timedelta(days=2)).isoformat()
    ts = datetime.fromisoformat(d + "T00:00:00+00:00").timestamp()
    prior = np.nonzero((day <= cut) & (known <= ts))[0]
    today = np.nonzero(day == d)[0]
    if len(prior) == 0 or len(today) == 0:
        continue
    pick["F"][today] = int(np.argmin(S[prior, :, 0].mean(axis=0)))

    def by_cells(cell_prior, cell_today, arm):
        for c in set(cell_today.tolist()):
            m = prior[cell_prior == c]
            if len(set(day[m])) < MIN_DAYS:
                continue
            pick[arm][today[cell_today == c]] = int(np.argmin(S[m, :, 0].mean(axis=0)))

    by_cells(hbin[prior], hbin[today], "L_h")
    by_cells(nfc[prior], nfc[today], "L_nf")
    for arm, x in (("L_D", D), ("L_sigma", sig)):
        q1, q2 = np.quantile(x[prior], [1 / 3, 2 / 3])
        cell = lambda v: np.where(v <= q1, 0, np.where(v <= q2, 1, 2))  # noqa: E731
        by_cells(cell(x[prior]), cell(x[today]), arm)
    mu_sd = ((D[prior].mean(), D[prior].std() or 1.0), (sig[prior].mean(), sig[prior].std() or 1.0))
    X = joint_design(prior, mu_sd)
    obj = lambda th: float(interp_nll(prior, np.exp(X @ th)).mean())  # noqa: E731
    th0 = np.array([math.log(0.75), 0, 0, 0, 0])
    res = minimize(obj, th0, method="Nelder-Mead", options={"xatol": 1e-4, "fatol": 1e-6, "maxiter": 4000})
    joint_params[d] = [float(v) for v in res.x]
    pick["L_joint"][today] = nearest(np.clip(np.exp(joint_design(today, mu_sd) @ res.x), GV[0], GV[-1]))

scored = np.array([SCORE_FROM <= d <= SCORE_TO for d in day])
idx_by_sd = defaultdict(list)
for i in np.nonzero(scored)[0]:
    idx_by_sd[(station[i], day[i])].append(i)


def table(arm, subset=None):
    out = {}
    for k, idx in idx_by_sd.items():
        idx = [i for i in idx if subset is None or subset[i]]
        if not idx:
            continue
        lam = np.full(len(idx), I75) if arm == "0.75" else pick[arm][idx]
        out[k] = tuple(float(x) for x in S[idx, lam, :].mean(axis=0))
    return out


def gate(cand, base, ks, conf):
    saved = store.GATE_CONFIDENCE
    store.GATE_CONFIDENCE = conf
    try:
        g = store._proper_score_gate([base[k] for k in ks], [cand[k] for k in ks], ks)
    finally:
        store.GATE_CONFIDENCE = saved
    return {f: g[f] for f in ("d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "ci_crps", "lower",
                              "station_days", "confidence")}


def block_ci(cand, base, ks):
    days = sorted({k[1] for k in ks}); by_day = defaultdict(list)
    for j, k in enumerate(ks):
        by_day[k[1]].append(j)
    dl = np.array([cand[k][0] - base[k][0] for k in ks]); rng = np.random.default_rng(SEED)
    nb = math.ceil(len(days) / BLOCK); boots = []
    for _ in range(BOOT):
        ii = np.concatenate([by_day[days[(s0 + j) % len(days)]] for s0 in rng.integers(0, len(days), nb) for j in range(BLOCK)])
        boots.append(float(dl[ii].mean()))
    return [float(dl.mean()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]


base = table("0.75")
keys = sorted(base)
not_thin = ~thin_far
base_nt = table("0.75", not_thin)
keys_nt = sorted(base_nt)
res = {"score_key": SCORE_KEY, "decisions": int(scored.sum()), "station_days": len(keys), "score_days": len(score_days),
       "thin_far_share": float(thin_far[scored].mean()), "confidence": CONF, "arms": {}}
tabs = {arm: table(arm) for arm in (*ARMS, "F")}
for arm in (*ARMS, "F"):
    conf = CONF if arm in ARMS else 0.95
    a = {"all": gate(tabs[arm], base, keys, conf)}
    for name, lo, hi in HALVES:
        ks = [k for k in keys if lo <= k[1] <= hi]
        a[name] = gate(tabs[arm], base, ks, conf)
    a["block3_logscore"] = block_ci(tabs[arm], base, keys)
    if arm != "F":
        a["vs_F"] = gate(tabs[arm], tabs["F"], keys, conf)
    c_nt = table(arm, not_thin)
    a["without_thin_far"] = gate(c_nt, base_nt, keys_nt, conf)
    a["picked"] = dict(sorted(Counter(GRID[i] for i in pick[arm][scored]).items()))
    a["passes"] = bool(a["all"]["lower"] and all(a[n]["d_logscore"] < 0 for n, _l, _h in HALVES)
                       and (arm == "F" or a["vs_F"]["d_logscore"] < 0))
    res["arms"][arm] = a
res["joint_params_last"] = joint_params.get(score_days[-1])
res["joint_params_first"] = joint_params.get(score_days[0])
# in-sample ceilings on the scored period
cs = np.nonzero(scored)[0]
w = np.zeros(N)
for k, idx in idx_by_sd.items():
    w[idx] = 1.0 / len(idx)


def mean_at(lams):
    return float((w[cs] * S[cs, lams[cs], 0]).sum() / w[cs].sum())


ceil = {}
b0 = mean_at(np.full(N, I75))
for name, cell in (("fleet", np.zeros(N, int)), ("h", hbin), ("nf", nfc),
                   ("D_tercile", np.digitize(D, np.quantile(D[cs], [1 / 3, 2 / 3]))),
                   ("sigma_tercile", np.digitize(sig, np.quantile(sig[cs], [1 / 3, 2 / 3])))):
    lam = np.full(N, I75)
    for c in set(cell[cs].tolist()):
        m = cs[cell[cs] == c]
        lam[cell == c] = int(np.argmin((w[m][:, None] * S[m, :, 0]).sum(axis=0)))
    ceil[name] = round(mean_at(lam) - b0, 5)
X = joint_design(cs, ((D[cs].mean(), D[cs].std()), (sig[cs].mean(), sig[cs].std())))
r_is = minimize(lambda th: float((w[cs] * interp_nll(cs, np.exp(X @ th))).sum() / w[cs].sum()),
                np.array([math.log(0.75), 0, 0, 0, 0]), method="Nelder-Mead", options={"maxiter": 4000})
ceil["joint"] = round(float(r_is.fun) - b0, 5)
ceil["joint_params"] = [round(float(v), 4) for v in r_is.x]
res["in_sample_ceiling_dlog"] = ceil
with open(os.path.join(HERE, f"results_{SCORE_KEY}.json"), "w") as handle:
    json.dump(res, handle, ensure_ascii=False, indent=1)
f = lambda v: "%+.4f [%+.4f,%+.4f]" % (v[0], v[1], v[2])  # noqa: E731
print(f"[{SCORE_KEY}] {res['decisions']} decisions, {res['station_days']} station-days, {res['score_days']} days, thin-far share {res['thin_far_share']:.3f}")
print("in-sample ceilings dLog vs 0.75:", ceil)
for arm, a in res["arms"].items():
    print(f"== {arm} passes={a['passes']} conf={a['all']['confidence']}")
    for part in ("all", *[x[0] for x in HALVES], "without_thin_far"):
        y = a[part]
        print(f"   {part:16s} sd={y['station_days']:5d} dLog {y['d_logscore']:+.4f} [{y['ci_logscore'][0]:+.4f},{y['ci_logscore'][1]:+.4f}]"
              f" dBrier {y['d_brier']:+.5f} [{y['ci_brier'][0]:+.5f},{y['ci_brier'][1]:+.5f}]"
              f" dCRPS {y['d_crps']:+.5f} [{y['ci_crps'][0]:+.5f},{y['ci_crps'][1]:+.5f}] lower={y['lower']}")
    print("   block3", f(a["block3_logscore"]))
    if "vs_F" in a:
        print(f"   vs F dLog {a['vs_F']['d_logscore']:+.4f} [{a['vs_F']['ci_logscore'][0]:+.4f},{a['vs_F']['ci_logscore'][1]:+.4f}]")
    print("   picked", a["picked"])
print("joint params (a, b_thin, c_D, e_sigma, f_h) first/last:", res["joint_params_first"], res["joint_params_last"])
