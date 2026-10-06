#!/usr/bin/env python3
"""PROTOCOL_V2: conditions on every tier (charter 255), and the realisation
form with condition-modulated model weights fitted forward.

    python3 round2.py grid      # method 1 -> results_v2_grid.json
    python3 round2.py fitwf     # method 2, replay walk-forward -> results_v2_fit_wf.json
    python3 round2.py fitapply  # method 2, fitted then applied out of time -> results_v2_fit_apply.json
"""
import collections
import json
import math
import os
import sys

import numpy as np
from scipy.optimize import minimize

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import analysis as an  # noqa: E402
from bayes_basket_target import realisation_layer as rl  # noqa: E402

CURRENT = {"P0": 27, "P0-M": 30, "P1": 38}
NEAR = {"P0": (25, 26), "P0-M": (28, 29), "P1": (36, 37)}
FEE = 0.05
L2 = 1.0
Z_BASE = ["abs_dmu", "kl_pq", "wr", "dH", "H_q", "spread_mode", "dmu_q_prev", "dmu_p_prev", "h", "neff"]
Z_EXTRA = {"P0-M": ["consist", "B", "taf_gap"], "P1": ["consist", "B", "obs_gap"]}
MODEL_COLS = (1, 2, 3, 4)

an.CONDITIONS.extend([
    (33, "regime label", 5, "cat", "regime", None, None, {}),
    (34, "model_disagreement (regime artifact)", 2, "tercile", "mdis", None, ["low", "mid", "high"], {"low": +1, "high": +1}),
    (35, "center_stable (regime artifact)", 2, "cat", "cstable", None, None, {"True": +1, "False": +1}),
])


def version(pv):
    try:
        return int(str(pv).rsplit("_v", 1)[1])
    except (IndexError, ValueError):
        return None


def segment(stage, v):
    if v == CURRENT[stage]:
        return "cur"
    return "near" if v in NEAR[stage] else "old"


def load_all():
    regime = json.load(open(os.path.join(HERE, "regime.json")))
    served_rows = an.load(os.path.join(HERE, "rows")) + an.load(os.path.join(HERE, "rows_early"))
    replay_rows = an.load(os.path.join(HERE, "rows_replay"))
    raw = {}
    for r in served_rows:
        raw[("s", r["st"], r["d"], r["stage"], r["t"])] = r
    for r in replay_rows:
        raw[("r", r["st"], r["d"], r["stage"], r["t"])] = r
    served = an.featurise(served_rows)
    for f in served:
        r = raw[("s", f["st"], f["d"], f["stage"], f["t"])]
        f["src"], f["ver"] = "s", version(r["pv"])
        f["seg"] = segment(f["stage"], f["ver"]) if f["ver"] is not None else "old"
        g = regime.get(r["eid"] or "") or [None, None, None]
        f["regime"], f["mdis"], f["cstable"] = g[0], g[1], (None if g[2] is None else str(bool(g[2])))
        f["praw"], f["qraw"], f["book"] = r["p"], r["q"], r["book"]
    replay = an.featurise(replay_rows)
    for f in replay:
        r = raw[("r", f["st"], f["d"], f["stage"], f["t"])]
        f["src"], f["seg"], f["ver"] = "r", "replay", None
        w = [float(v) for v in (r.get("w") or {}).values() if v]
        f["neff"] = (1.0 / sum((x / sum(w)) ** 2 for x in w)) if w else None
        f["seated"] = r.get("seated")
        f["regime"] = f["mdis"] = f["cstable"] = None
        f["praw"], f["qraw"], f["book"] = r["p"], r["q"], r["book"]
    return served, replay


# ---------------------------------------------------------------------------
# method 1


def grid(served, replay):
    out = {"replay": {"A": an.section_A(replay), "B": an.section_B(replay)}}
    for seg in ("cur", "near", "old"):
        ff = [f for f in served if f["seg"] == seg]
        out[seg] = {"A": an.section_A(ff), "B": an.section_B(ff)}
    json.dump(out, open(os.path.join(HERE, "results_v2_grid.json"), "w"), ensure_ascii=False, indent=1, default=str)
    return out


# ---------------------------------------------------------------------------
# method 2


def decision_terms(f):
    feats = rl.features([float(x or 0.0) for x in f["praw"]], [float(x or 0.0) for x in f["qraw"]])
    p, q = feats["p"], feats["q"]
    mean_p, _ = rl.moments(p)
    v1 = rl.floored(rl.shifted_to(q, mean_p))
    return feats["terms"], np.log(v1) - np.log(q)


def zvec(f, names):
    return [f.get(n) if n != "abs_dmu" else abs(f["dmu"]) for n in names]


class Design:
    """Standardised conditions (training moments), missing indicators, the
    arms' column sets."""

    def __init__(self, train, names):
        self.names = list(names)
        cols = []
        for j, n in enumerate(self.names):
            vals = np.asarray([np.nan if v is None else float(v) for v in (zvec(f, self.names)[j] for f in train)])
            ok = ~np.isnan(vals)
            mu = float(vals[ok].mean()) if ok.any() else 0.0
            sd = float(vals[ok].std()) if ok.sum() > 1 else 1.0
            cols.append((mu, sd if sd > 1e-9 else 1.0, bool((~ok).any())))
        self.cols = cols

    def z(self, f):
        out = []
        for (mu, sd, miss), v in zip(self.cols, zvec(f, self.names)):
            if v is None or not math.isfinite(float(v)):
                out.append(0.0)
                if miss:
                    out.append(1.0)
            else:
                out.append((float(v) - mu) / sd)
                if miss:
                    out.append(0.0)
        return np.asarray(out)

    def x(self, f, arm):
        terms, centre = f["_terms"], f["_centre"]
        if arm == "M1p":
            return terms[:, [0, 5, 6]], 0
        if arm == "M5":
            return terms, 0
        z = self.z(f)
        inter = [terms[:, c:c + 1] * z[None, :] for c in MODEL_COLS]
        parts = [terms] + inter
        if arm == "M5ZC":
            parts += [centre[:, None], centre[:, None] * z[None, :]]
        X = np.concatenate(parts, axis=1)
        return X, X.shape[1] - 7 - (1 if arm == "M5ZC" else 0)


def pack(items, design, arm):
    xs = [design.x(f, arm) for f in items]
    n, kmax, j = len(items), max(x.shape[0] for x, _ in xs), xs[0][0].shape[1]
    X = np.zeros((n, kmax, j))
    mask = np.zeros((n, kmax), dtype=bool)
    for i, (x, _) in enumerate(xs):
        X[i, :x.shape[0]] = x
        mask[i, :x.shape[0]] = True
    # L2 on the interaction coefficients only; the centre term's own
    # weight (M5ZC, the column after the 4 x m model interactions) is free.
    pen = np.zeros(j)
    if arm in ("M5Z", "M5ZC"):
        pen[7:] = 1.0
        if arm == "M5ZC":
            pen[7 + 4 * len(design.z(items[0]))] = 0.0
    return X, mask, pen


def probs(X, mask, theta):
    zz = X @ theta
    zz = np.where(mask, zz, -np.inf)
    zz = zz - zz.max(axis=1, keepdims=True)
    e = np.exp(zz)
    return e / e.sum(axis=1, keepdims=True)


def fit(items, design, arm):
    X, mask, pen = pack(items, design, arm)
    y = np.asarray([f["y"] for f in items])
    w = np.asarray([f["wt"] for f in items])
    rows = np.arange(len(y))
    x0 = np.zeros(X.shape[2])
    x0[0] = 1.0

    def fg(theta):
        b = probs(X, mask, theta)
        loss = -(w * np.log(b[rows, y] + 1e-300)).sum() + 0.5 * L2 * (pen * theta * theta).sum()
        mean_x = np.einsum("nk,nkj->nj", b, X)
        grad = -((w[:, None] * (X[rows, y] - mean_x)).sum(axis=0)) + L2 * pen * theta
        return loss, grad

    res = minimize(fg, x0, jac=True, method="L-BFGS-B", options={"maxiter": 500})
    return res.x


def score(items, design, arm, theta):
    X, mask, _ = pack(items, design, arm)
    b = probs(X, mask, theta)
    out = []
    for i, f in enumerate(items):
        k = int(mask[i].sum())
        bi = b[i, :k]
        onehot = np.zeros(k)
        onehot[f["y"]] = 1.0
        out.append({"ls": float(-math.log(max(bi[f["y"]], 1e-300))), "brier": float(((bi - onehot) ** 2).sum()), "b": bi})
    return out


def legs_pnl(items, beliefs):
    """Each station-day bucket's first decision where the arm's belief clears
    the taker price plus fee; per-share P&L."""
    seen = set()
    legs = []
    for f, s in sorted(zip(items, beliefs), key=lambda x: x[0]["t"]):
        for k, b in enumerate(s["b"]):
            key = (f["st"], f["d"], k)
            if key in seen:
                continue
            bid, ask = f["book"][k][0], f["book"][k][1]
            hit = 1.0 if k == f["y"] else 0.0
            if ask is not None and b > ask + FEE * ask * (1 - ask):
                seen.add(key)
                legs.append({"d": f["d"], "st": f["st"], "pnl": hit - ask - FEE * ask * (1 - ask), "side": "YES"})
            elif bid is not None and (1 - b) > (1 - bid) + FEE * bid * (1 - bid):
                seen.add(key)
                legs.append({"d": f["d"], "st": f["st"], "pnl": (1 - hit) - (1 - bid) - FEE * bid * (1 - bid), "side": "NO"})
    return legs


def contrast(items, a, b, key):
    days = sorted({f["d"] for f in items})
    boot = an.Boot(days)
    diffs = [{"d": f["d"], "wt": f["wt"], "v": x[key] - y[key]} for f, x, y in zip(items, a, b)]
    num, den = an.day_sums(diffs, days, lambda x: x["v"], lambda x: x["wt"])
    return boot.summarise(num, den) if len(days) >= 2 else {"mean": float(num.sum() / den.sum()) if den.sum() else None}


def legs_summary(legs, days):
    if not legs:
        return {"legs": 0}
    boot = an.Boot(sorted(set(days)))
    num, den = an.day_sums(legs, sorted(set(days)), lambda x: x["pnl"], lambda x: 1.0)
    s = boot.summarise(num, den) if len(set(days)) >= 2 else {"mean": float(num.sum() / den.sum())}
    return {"legs": len(legs), "yes": sum(1 for x in legs if x["side"] == "YES"), **(s or {})}


ARMS = ("M1p", "M5", "M5Z", "M5ZC")


def evaluate(test, fits, design):
    sc = {arm: score(test, design, arm, fits[arm]) for arm in ARMS}
    days = [f["d"] for f in test]
    res = {"decisions": len(test), "station_days": len({(f["st"], f["d"]) for f in test}), "days": len(set(days))}
    for arm in ARMS:
        res[arm] = {"ls": float(np.average([s["ls"] for s in sc[arm]], weights=[f["wt"] for f in test])),
                    "brier": float(np.average([s["brier"] for s in sc[arm]], weights=[f["wt"] for f in test])),
                    "legs": legs_summary(legs_pnl(test, sc[arm]), [x["d"] for x in legs_pnl(test, sc[arm])])}
    for arm in ("M5Z", "M5ZC", "M5"):
        for ref in ("M5", "M1p"):
            if arm == ref:
                continue
            res[f"{arm}-{ref}"] = {"dLS": contrast(test, sc[arm], sc[ref], "ls"), "dBrier": contrast(test, sc[arm], sc[ref], "brier")}
    return res, sc


def prepare(items):
    for f in items:
        f["_terms"], f["_centre"] = decision_terms(f)


def walk_forward(items, names, label):
    days = sorted({f["d"] for f in items})
    preds = {arm: [] for arm in ARMS}
    tested = []
    for start in range(an.MIN_DAYS, len(days), 3):
        test_days = set(days[start:start + 3])
        train = [f for f in items if f["d"] < days[start]]
        test = [f for f in items if f["d"] in test_days]
        if len(train) < 200 or not test:
            continue
        design = Design(train, names)
        fits = {arm: fit(train, design, arm) for arm in ARMS}
        for arm in ARMS:
            preds[arm].extend(score(test, design, arm, fits[arm]))
        tested.extend(test)
        print(f"  {label}: tested through {max(test_days)} ({len(tested)} decisions)", flush=True)
    res = {"decisions": len(tested), "station_days": len({(f["st"], f["d"]) for f in tested}),
           "days": len({f["d"] for f in tested})}
    for arm in ARMS:
        lg = legs_pnl(tested, preds[arm])
        res[arm] = {"ls": float(np.average([s["ls"] for s in preds[arm]], weights=[f["wt"] for f in tested])),
                    "brier": float(np.average([s["brier"] for s in preds[arm]], weights=[f["wt"] for f in tested])),
                    "legs": legs_summary(lg, [x["d"] for x in lg])}
    for arm in ("M5Z", "M5ZC", "M5"):
        for ref in ("M5", "M1p"):
            if arm != ref:
                res[f"{arm}-{ref}"] = {"dLS": contrast(tested, preds[arm], preds[ref], "ls"),
                                       "dBrier": contrast(tested, preds[arm], preds[ref], "brier")}
    return res, tested, preds


def fit_section(served, replay, part):
    out = {}
    rp = [f for f in replay if f["qraw"]]
    prepare(rp)
    if part == "apply":
        return apply_section(served, rp)
    # tier 丙2: walk-forward on the replay; P0 all, then the >36 h subset of the same predictions
    res, tested, preds = walk_forward(rp, Z_BASE, "replay P0")
    out["replay_P0_all"] = res
    far = [i for i, f in enumerate(tested) if (f["h"] or 0) > 36]
    sub = [tested[i] for i in far]
    subp = {arm: [preds[arm][i] for i in far] for arm in ARMS}
    r36 = {"decisions": len(sub), "station_days": len({(f["st"], f["d"]) for f in sub}), "days": len({f["d"] for f in sub})}
    for arm in ARMS:
        lg = legs_pnl(sub, subp[arm])
        r36[arm] = {"ls": float(np.average([s["ls"] for s in subp[arm]], weights=[f["wt"] for f in sub])),
                    "brier": float(np.average([s["brier"] for s in subp[arm]], weights=[f["wt"] for f in sub])),
                    "legs": legs_summary(lg, [x["d"] for x in lg])}
    for arm in ("M5Z", "M5ZC", "M5"):
        for ref in ("M5", "M1p"):
            if arm != ref:
                r36[f"{arm}-{ref}"] = {"dLS": contrast(sub, subp[arm], subp[ref], "ls"),
                                       "dBrier": contrast(sub, subp[arm], subp[ref], "brier")}
    out["replay_P0_gt36h"] = r36
    json.dump(out, open(os.path.join(HERE, "results_v2_fit_wf.json"), "w"), ensure_ascii=False, indent=1, default=str)
    return out


def apply_section(served, rp):
    out = {}
    # tiers 丙1 / 甲 for P0: arms fitted on the whole replay, applied out of time
    design = Design(rp, Z_BASE)
    fits = {arm: fit(rp, design, arm) for arm in ARMS}
    for seg in ("near", "cur"):
        test = [f for f in served if f["stage"] == "P0" and f["seg"] == seg and f["qraw"]]
        if test:
            prepare(test)
            out[f"served_P0_{seg}"] = evaluate(test, fits, design)[0]
    # P0-M / P1: fitted on the stage's old segment, applied to near / cur
    for stage in ("P0-M", "P1"):
        names = Z_BASE + Z_EXTRA[stage]
        old = [f for f in served if f["stage"] == stage and f["seg"] == "old" and f["qraw"]]
        prepare(old)
        design = Design(old, names)
        fits = {arm: fit(old, design, arm) for arm in ARMS}
        for seg in ("near", "cur"):
            test = [f for f in served if f["stage"] == stage and f["seg"] == seg and f["qraw"]]
            if test:
                prepare(test)
                out[f"served_{stage}_{seg}"] = evaluate(test, fits, design)[0]
    json.dump(out, open(os.path.join(HERE, "results_v2_fit_apply.json"), "w"), ensure_ascii=False, indent=1, default=str)
    return out


def main():
    served, replay = load_all()
    print(f"served featured {len(served)}, replay featured {len(replay)}", flush=True)
    what = sys.argv[1] if len(sys.argv) > 1 else "grid"
    if what == "grid":
        grid(served, replay)
    elif what == "fitwf":
        fit_section(served, replay, "wf")
    else:
        fit_section(served, replay, "apply")
    print("done", flush=True)


if __name__ == "__main__":
    main()
