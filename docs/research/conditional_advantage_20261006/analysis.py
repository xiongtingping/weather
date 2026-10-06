#!/usr/bin/env python3
"""Conditional advantage: conditions 1-32, compounds, the walk-forward
regression and the economics -- protocol sections 2-5.  Reads rows/*.jsonl.gz
written by extract.py; writes results_*.json; prints the tables.

    python3 analysis.py            # everything
    python3 analysis.py A B        # only those sections
"""
import collections
import glob
import gzip
import json
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from bayes_basket_target import realisation_layer as rl  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = 20261006
B = 2000
BLOCK = 3
MIN_SD = 40
MIN_DAYS = 8
FEE = 0.05
FAMILIES = ("P0>36h", "P0<=36h", "P0-M", "P1")
SECTIONS = set(a.upper() for a in sys.argv[1:]) or {"A", "B", "C", "D", "E"}

# The production realisation fit's market-only terms (data/realisation_layer/active.json,
# fit 2026-10-06T09:46Z): market_power, modal_bucket, beside_mode.  Section E's
# "market structure, not model" baseline.
M1P = {"alpha": 1.0997816109195764, "modal": 0.37511299412927973, "beside": 0.43365428375110726}


def iso(t):
    from datetime import datetime, timezone
    t = t.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(t).astimezone(timezone.utc)
    except ValueError:
        return None


def hours_between(a, b):
    ta, tb = iso(a), iso(b)
    if ta is None or tb is None:
        return None
    return (ta - tb).total_seconds() / 3600.0


def entropy(v):
    v = np.asarray(v, dtype=float)
    v = v[v > 0]
    return float(-(v * np.log(v)).sum())


def kl(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    return float((a * np.log(a / b)).sum())


def js(a, b):
    m = 0.5 * (np.asarray(a) + np.asarray(b))
    return 0.5 * kl(a, m) + 0.5 * kl(b, m)


def moments_c(vec, temps, rung=1.0):
    """Mean and spread in °C, each bucket a uniform block ``rung`` wide
    (the same within-rung variance the realisation layer adds on the rung axis)."""
    v = np.asarray(vec, dtype=float)
    t = np.asarray(temps, dtype=float)
    mean = float((v * t).sum())
    return mean, math.sqrt(float((v * (t - mean) ** 2).sum()) + rl.WITHIN_RUNG_VARIANCE * rung * rung)


def argmax_low(v):
    v = np.asarray(v, dtype=float)
    return int(np.flatnonzero(v == v.max())[0])


def family_of(r):
    if r["stage"] == "P0":
        return "P0>36h" if (r["h"] or 0.0) > 36.0 else "P0<=36h"
    return r["stage"]


# ---------------------------------------------------------------------------
# 1. load and feature rows


def load(rows_dir=None):
    rows = []
    for f in sorted(glob.glob(os.path.join(rows_dir or os.path.join(HERE, "rows"), "*.jsonl.gz"))):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: (r["st"], r["d"], r["stage"], r["t"]))
    return rows


def model_credibility(r, mu_q_c):
    """Conditions 10-13, 16, 17 from a P0 row's own evidence."""
    out = {"B": None, "neff": None, "seated": None, "consist": None, "arrival_age_h": None, "A": None}
    centres = r.get("centres") or {}
    w = r.get("w") or {}
    keys = [k for k in centres if k in w and w[k] is not None and w[k] > 0]
    if keys:
        ws = np.asarray([float(w[k]) for k in keys])
        ws = ws / ws.sum()
        mus = np.asarray([float(centres[k]) for k in keys])
        mbar = float((ws * mus).sum())
        out["B"] = float((ws * (mus - mbar) ** 2).sum())
        out["neff"] = float(1.0 / (ws ** 2).sum())
        if mu_q_c is not None:
            side = np.sign(mbar - mu_q_c)
            out["consist"] = float(ws[np.sign(mus - mu_q_c) == side].sum()) if side != 0 else None
    out["seated"] = len(r.get("systems") or keys)
    arr = r.get("arrival") or {}
    stamps = [iso(v) for v in arr.values() if isinstance(v, str)]
    stamps = [s for s in stamps if s is not None]
    if stamps:
        t = iso(r["t"])
        out["arrival_age_h"] = (t - max(stamps)).total_seconds() / 3600.0 if t else None
    ss = r.get("sys_stats") or {}
    keys2 = [k for k in ss if k in w and w[k]]
    if keys2:
        ws = np.asarray([float(w[k]) for k in keys2])
        ws = ws / ws.sum()
        out["A"] = float((ws * np.asarray([ss[k][1] ** 2 for k in keys2])).sum())
    return out


def featurise(rows):
    """One feature dict per decision with a complete market vector."""
    by_sd = collections.defaultdict(list)
    for r in rows:
        by_sd[(r["st"], r["d"])].append(r)
    feats = []
    for (st, d), group in by_sd.items():
        p0 = sorted([r for r in group if r["stage"] == "P0"], key=lambda r: r["t"])
        prev_by_stage = {}
        hist_by_stage = collections.defaultdict(list)
        for r in sorted(group, key=lambda r: (r["t"])):
            stage = r["stage"]
            names = r["names"]
            temps = r["temps"]
            rung = float(r["rung"] or 1.0)
            if any(t is None for t in temps) or not r["p"]:
                continue
            p = rl.floored([x or 0.0 for x in r["p"]])
            mu_p, sd_p = moments_c(p, temps, rung)
            # run-to-run and trajectory read every decision, with or without q
            prev = prev_by_stage.get(stage)
            prev_by_stage[stage] = (r["t"], mu_p, r.get("q"), r.get("temps"))
            t_now = iso(r["t"])
            hist = hist_by_stage[stage]
            traj = [m for (tt, m) in hist if t_now and tt and (t_now - tt).total_seconds() <= 24 * 3600]
            hist.append((t_now, mu_p))
            if not r.get("q") or r["y"] not in names:
                continue
            q = rl.floored([x or 0.0 for x in r["q"]])
            y = names.index(r["y"])
            mu_q, sd_q = moments_c(q, temps, rung)
            f = {
                "st": st, "d": d, "t": r["t"], "stage": stage, "fam": family_of(r), "h": r["h"],
                "seq": r["seq"], "unit": "F" if "°F" in names[0] else "C", "season": r.get("season"),
                "k": len(names), "y": y, "rung": rung, "ms": r.get("ms"),
                "d_": float(math.log(p[y]) - math.log(q[y])),
                "D": kl(q, p),
                "mu_p": mu_p, "mu_q": mu_q, "sd_p": sd_p, "sd_q": sd_q,
                "dmu": (mu_p - mu_q) / rung,
                "kl_pq": kl(p, q), "js": js(p, q),
                "wr": math.log(sd_p / sd_q),
                "dH": entropy(p) - entropy(q),
                "mode_p": argmax_low(p), "mode_q": argmax_low(q),
            }
            f["mode_agree"] = int(f["mode_p"] == f["mode_q"])
            f["mode_side"] = 0 if f["mode_agree"] else (1 if f["mode_p"] > f["mode_q"] else -1)
            # 7: decomposition shares on the rung axis
            try:
                m_idx, s_idx = rl.moments(p)
                v1 = rl.shifted_to(q, m_idx)
                v2 = rl.matched_to(q, m_idx, s_idx)
                parts = [float(np.abs(v1 - q).sum()), float(np.abs(v2 - v1).sum()), float(np.abs(p - v2).sum())]
                tot = sum(parts) or 1.0
                f["share_c"], f["share_w"], f["share_s"] = [x / tot for x in parts]
                f["share_max"] = ["centre", "width", "shape"][int(np.argmax(parts))]
            except Exception:
                f["share_c"] = f["share_w"] = f["share_s"] = None
                f["share_max"] = None
            # model credibility: own evidence for P0, the latest earlier P0 for P0-M/P1
            src = r if stage == "P0" else None
            if src is None:
                earlier = [x for x in p0 if x["t"] <= r["t"]]
                src = earlier[-1] if earlier else None
            cred = model_credibility(src, mu_q) if src else model_credibility({}, mu_q)
            f.update(cred)
            f["cred_inherited"] = int(stage != "P0")
            # run-to-run (14, 15)
            f["first"] = int(prev is None)
            f["dmu_p_prev"] = abs(mu_p - prev[1]) / rung if prev else None
            f["traj_sd"] = float(np.std(traj) / rung) if len(traj) >= 2 else None
            # market quality (18-25)
            f["snap_age_min"] = (hours_between(r["t"], r["snap_at"]) or 0.0) * 60.0 if r.get("snap_at") else None
            if prev and prev[2] and prev[3] and len(prev[2]) == len(prev[3]) and len(prev[2]) == len(q):
                qp = rl.floored([x or 0.0 for x in prev[2]])
                mu_qp, _ = moments_c(qp, prev[3], rung)
                f["dmu_q_prev"] = abs(mu_q - mu_qp) / rung
                f["l1_q_prev"] = float(np.abs(q - qp).sum())
            else:
                f["dmu_q_prev"] = None
                f["l1_q_prev"] = None
            f["moved_not"] = (int(f["dmu_p_prev"] >= 0.3 and f["dmu_q_prev"] < 0.1)
                              if f["dmu_p_prev"] is not None and f["dmu_q_prev"] is not None else None)
            book = r["book"]
            bid_m, ask_m = book[f["mode_q"]][0], book[f["mode_q"]][1]
            f["spread_mode"] = (ask_m - bid_m) if (ask_m is not None and bid_m is not None) else None
            f["depth_mode_ask"] = book[f["mode_q"]][3]
            asks = [b[1] for b in book]
            f["ask_sum"] = float(sum(asks)) if all(a is not None for a in asks) else None
            f["ms_dev"] = abs(float(r["ms"]) - 1.0) if r.get("ms") else None
            f["H_q"] = entropy(q)
            qa = np.asarray(q)
            peaks = [i for i in range(len(qa)) if qa[i] >= 0.03 and (i == 0 or qa[i] > qa[i - 1]) and (i == len(qa) - 1 or qa[i] >= qa[i + 1])]
            f["multimodal"] = int(len(peaks) > 1)
            f["gap"] = int(any(qa[i] >= 0.03 and qa[i + 1] >= 0.03 and max(qa[i] / qa[i + 1], qa[i + 1] / qa[i]) > 5 for i in range(len(qa) - 1)))
            # weather (30-32)
            cool = r.get("cooling") or {}
            f["clearness"] = cool.get("clearness_index")
            f["taf_gap"] = None
            f["taf_applied"] = None
            if stage == "P0-M" and r.get("taf"):
                like = r["taf"].get("like") or {}
                f["taf_gap"] = (float(like["x_c"]) / rung) if like.get("x_c") is not None else None
                f["taf_applied"] = int(r["taf"].get("mode") == "P0M_PRIOR_X_TAF_LIKELIHOOD_V1")
            f["obs_gap"] = None
            if stage == "P1" and r.get("obs") and r["obs"].get("running_high_c") is not None:
                f["obs_gap"] = (mu_p - float(r["obs"]["running_high_c"])) / rung
            # legs (8, 9, 21, 22 and the economics)
            legs = []
            for kk in range(len(names)):
                side = 1 if p[kk] > q[kk] else -1
                bid, ask, bsh, ash = book[kk]
                hit = 1.0 if kk == y else 0.0
                mid = q[kk] * float(r["ms"]) if r.get("ms") else None
                if side > 0:
                    price = ask
                    depth = ash
                    pi = (hit - ask - FEE * ask * (1 - ask)) if ask is not None else None
                    L = (ask + FEE * ask * (1 - ask) - mid) if (ask is not None and mid is not None) else None
                else:
                    price = bid
                    depth = bsh
                    pi = (bid - hit - FEE * bid * (1 - bid)) if bid is not None else None
                    L = (mid - bid + FEE * bid * (1 - bid)) if (bid is not None and mid is not None) else None
                z = (temps[kk] - mu_p) / sd_p
                legs.append({
                    "k": kk, "side": side, "s": float(math.log(p[kk] / q[kk])), "z": z,
                    "pos": "centre" if abs(z) < 0.5 else ("shoulder" if abs(z) <= 1.5 else "tail"),
                    "dist": abs(kk - f["mode_q"]),
                    "r": side * (hit - q[kk]), "claim": abs(p[kk] - q[kk]),
                    "spread": (ask - bid) if (ask is not None and bid is not None) else None,
                    "depth": depth, "price": price, "pi": pi, "L": L, "hit": hit, "q": float(q[kk]), "p": float(p[kk]),
                })
            f["legs"] = legs
            # paper: Delta S + D - L on the tradable legs, lambda = |s|
            trad = [lg for lg in legs if lg["L"] is not None]
            f["pi_exec"] = float(sum(lg["s"] * (lg["hit"] - lg["q"]) - abs(lg["s"]) * lg["L"] for lg in trad)) if trad else None
            f["lambda_cost"] = float(sum(abs(lg["s"]) * lg["L"] for lg in trad)) if trad else None
            f["n_trad"] = len(trad)
            # section E: market-only belief
            z_ = M1P["alpha"] * np.log(q)
            z_[f["mode_q"]] += M1P["modal"]
            for j in (f["mode_q"] - 1, f["mode_q"] + 1):
                if 0 <= j < len(q):
                    z_[j] += M1P["beside"]
            b = np.exp(z_ - z_.max())
            b /= b.sum()
            f["d_m1p"] = float(math.log(b[y]) - math.log(q[y]))
            feats.append(f)
    # station-day weights within family
    cnt = collections.Counter((f["fam"], f["st"], f["d"]) for f in feats)
    for f in feats:
        f["wt"] = 1.0 / cnt[(f["fam"], f["st"], f["d"])]
    return feats


# ---------------------------------------------------------------------------
# 2. statistics: day-level sums, block bootstrap, halves, BH


class Boot:
    def __init__(self, days):
        self.days = list(days)
        self.index = {d: i for i, d in enumerate(self.days)}
        n = len(self.days)
        rng = np.random.default_rng(SEED)
        starts = rng.integers(0, max(n - BLOCK + 1, 1), size=(B, math.ceil(n / BLOCK)))
        draws = (starts[:, :, None] + np.arange(BLOCK)[None, None, :]).reshape(B, -1)[:, :n]
        self.draws = np.minimum(draws, n - 1)

    def summarise(self, num, den):
        """Weighted mean and 95% interval from day-level numerators/denominators."""
        tot_den = den.sum()
        if tot_den <= 0:
            return None
        point = float(num.sum() / tot_den)
        n_b = num[self.draws].sum(axis=1)
        d_b = den[self.draws].sum(axis=1)
        ok = d_b > 0
        ratio = n_b[ok] / d_b[ok]
        if ratio.size < 100:
            return {"mean": point, "lo": None, "hi": None, "p_le0": None}
        return {"mean": point, "lo": float(np.quantile(ratio, 0.025)), "hi": float(np.quantile(ratio, 0.975)),
                "p_le0": float(max((ratio <= 0).mean(), 1.0 / ratio.size)), "p_ge0": float(max((ratio >= 0).mean(), 1.0 / ratio.size))}


def day_sums(items, days, value, weight):
    idx = {d: i for i, d in enumerate(days)}
    num = np.zeros(len(days))
    den = np.zeros(len(days))
    for it in items:
        v = value(it)
        if v is None or not math.isfinite(v):
            continue
        w = weight(it)
        num[idx[it["d"]]] += w * v
        den[idx[it["d"]]] += w
    return num, den


def cell_reading(items, halves, value=lambda f: f["d_"], weight=lambda f: f["wt"]):
    """Point and interval in each half, with sizes."""
    out = {}
    for name, (days, boot) in halves.items():
        sub = [it for it in items if it["d"] in boot.index]
        num, den = day_sums(sub, days, value, weight)
        sd = len({(it["st"], it["d"]) for it in sub if value(it) is not None})
        nd = len({it["d"] for it in sub if value(it) is not None})
        s = boot.summarise(num, den) if sd else None
        out[name] = {"station_days": sd, "days": nd, "enough": bool(sd >= MIN_SD and nd >= MIN_DAYS), **(s or {})}
    return out


def verdict(reading, direction):
    """Protocol section 5 (i), (ii), (iv) for one cell and one direction (+1: d>0 expected)."""
    h1, h2 = reading["H1"], reading["H2"]
    if not (h1.get("enough") and h2.get("enough")):
        return {"status": "insufficient"}
    res = {}
    for disc, conf in (("H1", "H2"), ("H2", "H1")):
        a, b = reading[disc], reading[conf]
        if a.get("mean") is None or b.get("lo") is None:
            res[f"{disc}->{conf}"] = "na"
            continue
        same = (a["mean"] > 0) == (direction > 0)
        clear = (b["lo"] > 0) if direction > 0 else (b["hi"] < 0)
        res[f"{disc}->{conf}"] = "pass" if (same and clear) else "fail"
    pval = None
    if reading["H2"].get("p_le0") is not None:
        pval = reading["H2"]["p_le0"] if direction > 0 else reading["H2"]["p_ge0"]
    return {"status": "survive" if any(v == "pass" for v in res.values()) else "fail", "directions": res, "p_conf_H2": pval}


def bh(pvals, q=0.10):
    """Benjamini-Hochberg: which of the given p-values pass at level q."""
    items = [(p, i) for i, p in enumerate(pvals) if p is not None]
    items.sort()
    m = len(items)
    passed = set()
    best = 0
    for rank, (p, i) in enumerate(items, start=1):
        if p <= q * rank / m:
            best = rank
    for rank, (p, i) in enumerate(items, start=1):
        if rank <= best:
            passed.add(i)
    return passed


# ---------------------------------------------------------------------------
# 3. the condition catalogue (protocol section 3)

def tercile_cuts(values):
    v = np.asarray([x for x in values if x is not None and math.isfinite(x)])
    if v.size < 10:
        return None
    return [float(np.quantile(v, 1 / 3)), float(np.quantile(v, 2 / 3))]


def binned(x, cuts, labels):
    if x is None or not math.isfinite(x):
        return None
    for c, lab in zip(cuts, labels):
        if x < c:
            return lab
    return labels[-1]


CONDITIONS = [
    # (id, name, layer, kind, key, cuts, labels, counted-direction bins {label: +1/-1 expected sign of d})
    (1, "Δμ signed (rungs)", 1, "fixed", "dmu", [-1, -0.3, 0.3, 1], ["≤-1", "(-1,-0.3]", "(-0.3,0.3)", "[0.3,1)", "≥1"], {"≤-1": +1, "≥1": +1}),
    (2, "|Δμ| (rungs)", 1, "fixed", "abs_dmu", [0.3, 1], ["<0.3", "0.3-1", "≥1"], {"<0.3": +1, "≥1": +1}),
    (3, "KL(P‖Q)", 1, "tercile", "kl_pq", None, ["low", "mid", "high"], {"low": +1, "high": +1}),
    (4, "width ratio log(σP/σQ)", 1, "fixed", "wr", [-0.2, 0.2], ["<-0.2", "[-0.2,0.2]", ">0.2"], {"<-0.2": +1, ">0.2": +1}),
    (5, "entropy diff H(P)-H(Q)", 1, "tercile", "dH", None, ["low", "mid", "high"], {"low": +1, "high": +1}),
    (6, "mode agree / model warmer / colder", 1, "cat", "mode_side", None, None, {"agree": +1, "warmer": +1, "colder": +1}),
    (7, "largest disagreement part", 1, "cat", "share_max", None, None, {"centre": +1, "width": +1, "shape": +1}),
    (10, "centre dispersion B (°C²)", 2, "fixed", "B", [0.1, 0.5], ["<0.1", "0.1-0.5", "≥0.5"], {"<0.1": +1, "≥0.5": +1}),
    (11, "N_eff", 2, "fixed", "neff", [1.5, 3], ["<1.5", "1.5-3", "≥3"], {"<1.5": +1, "≥3": +1}),
    (12, "seated systems", 2, "fixed", "seated", [3, 5], ["2", "3-4", "5-6"], {"2": +1, "5-6": +1}),
    (13, "direction consistency (|Δμ|≥0.3)", 2, "fixed", "consist_def", [0.6, 0.8], ["<0.6", "0.6-0.8", "≥0.8"], {"<0.6": +1, "≥0.8": +1}),
    (14, "run-to-run |Δμ_P| (rungs)", 2, "fixed", "dmu_p_prev", [0.2, 0.5], ["0-0.2", "0.2-0.5", "≥0.5"], {"0-0.2": +1, "≥0.5": +1}),
    (15, "24h trajectory sd (rungs)", 2, "fixed", "traj_sd", [0.2, 0.5], ["<0.2", "0.2-0.5", "≥0.5"], {"<0.2": +1, "≥0.5": +1}),
    (16, "newest member arrival age (h)", 2, "fixed", "arrival_age_h", [1, 3], ["≤1h", "1-3h", ">3h"], {"≤1h": +1, ">3h": +1}),
    (17, "member spread A (°C², W2)", 2, "tercile", "A", None, ["low", "mid", "high"], {"low": +1, "high": +1}),
    (18, "snapshot age (min)", 3, "fixed", "snap_age_min", [5, 30], ["≤5", "5-30", ">30"], {"≤5": +1, ">30": +1}),
    (19, "market move since prev |Δμ_Q| (rungs)", 3, "fixed", "dmu_q_prev", [0.1, 0.3], ["<0.1", "0.1-0.3", "≥0.3"], {"<0.1": +1, "≥0.3": +1}),
    (20, "model moved, market did not", 3, "cat", "moved_not", None, None, {"1": +1}),
    (21, "mode-bucket spread", 3, "fixed", "spread_mode", [0.03, 0.08], ["≤0.03", "0.03-0.08", ">0.08"], {"≤0.03": +1, ">0.08": +1}),
    (22, "mode-bucket ask depth (shares)", 3, "fixed", "depth_mode_ask", [50, 300], ["<50", "50-300", "≥300"], {"<50": +1, "≥300": +1}),
    (23, "ladder ask sum", 3, "fixed", "ask_sum", [1.05, 1.20], ["≤1.05", "1.05-1.20", ">1.20"], {"≤1.05": +1, ">1.20": +1}),
    (24, "market entropy H(Q)", 3, "tercile", "H_q", None, ["low", "mid", "high"], {"low": +1, "high": +1}),
    (25, "market multimodal / gap", 3, "cat", "inconsistent", None, None, {"1": +1}),
    (26, "hours to settlement", 4, "fixed", "h", [12, 24, 36, 48, 60], ["≤12", "12-24", "24-36", "36-48", "48-60", ">60"], {"≤12": +1, ">60": +1}),
    (28, "settlement unit", 4, "cat", "unit", None, None, {"C": +1, "F": +1}),
    (29, "season phase", 4, "cat", "season", None, None, {}),
    (30, "clearness index", 5, "tercile", "clearness", None, ["low", "mid", "high"], {"low": +1, "high": +1}),
    (31, "TAF TX gap (rungs, P0-M)", 5, "fixed", "taf_gap", [-1, 1], ["<-1", "[-1,1]", ">1"], {"<-1": +1, ">1": +1}),
    (32, "observed-high gap μP-obs (rungs, P1)", 5, "fixed", "obs_gap", [0, 1], ["<0", "0-1", ">1"], {"<0": +1, ">1": +1}),
]


def condition_value(f, key):
    if key == "abs_dmu":
        return abs(f["dmu"])
    if key == "consist_def":
        return f["consist"] if abs(f["dmu"]) >= 0.3 else None
    if key == "mode_side":
        return {0: "agree", 1: "warmer", -1: "colder"}[f["mode_side"]]
    if key == "inconsistent":
        return str(int(f["multimodal"] or f["gap"]))
    if key == "moved_not":
        return None if f["moved_not"] is None else str(f["moved_not"])
    return f.get(key)


def assign_bins(feats, cond, cuts_source):
    cid, name, layer, kind, key, cuts, labels, counted = cond
    if kind == "tercile":
        cuts = tercile_cuts([condition_value(f, key) for f in cuts_source])
        if cuts is None:
            return None, None
    out = {}
    for f in feats:
        v = condition_value(f, key)
        if kind == "cat":
            out[id(f)] = None if v is None else str(v)
        else:
            out[id(f)] = binned(v, cuts, labels)
    return out, cuts


COMPOUNDS = {
    "C1": lambda f: f["dmu"] >= 1 and (f["consist"] or 0) >= 0.8 and f["dmu_q_prev"] is not None and f["dmu_q_prev"] < 0.1,
    "C2": lambda f: f["dmu"] <= -1 and (f["consist"] or 0) >= 0.8 and f["dmu_q_prev"] is not None and f["dmu_q_prev"] < 0.1,
    "C3": lambda f: abs(f["dmu"]) >= 1 and (f["neff"] or 0) >= 3 and f["snap_age_min"] is not None and f["snap_age_min"] <= 5,
    "C4": lambda f: abs(f["dmu"]) >= 1 and f["arrival_age_h"] is not None and f["arrival_age_h"] <= 1 and f["dmu_q_prev"] is not None and f["dmu_q_prev"] < 0.1,
    "C5": lambda f: f["mode_agree"] == 1 and f["wr"] < -0.2,
    "C6": lambda f: f["mode_agree"] == 1 and f["wr"] > 0.2,
}
COMPOUND_EXPECT = {"C1": +1, "C2": +1, "C3": -1, "C4": +1, "C5": +1, "C6": +1}


def g12_cell(f):
    a = "≤-1" if f["dmu"] <= -1 else ("≥+1" if f["dmu"] >= 1 else ("|·|<0.3" if abs(f["dmu"]) < 0.3 else None))
    if a is None or f["consist"] is None or f["dmu_q_prev"] is None:
        return None
    b = "cons≥0.8" if f["consist"] >= 0.8 else "cons<0.8"
    c = "mkt still" if f["dmu_q_prev"] < 0.1 else "mkt moved"
    return f"{a} × {b} × {c}"


# ---------------------------------------------------------------------------
# 4. sections


def halves_for(feats):
    days = sorted({f["d"] for f in feats})
    mid = len(days) // 2
    return {
        "H1": (days[:mid], Boot(days[:mid])), "H2": (days[mid:], Boot(days[mid:])),
        "odd": (days[0::2], Boot(days[0::2])), "even": (days[1::2], Boot(days[1::2])),
        "all": (days, Boot(days)),
    }


def section_A(feats):
    results = {}
    for fam in FAMILIES:
        ff = [f for f in feats if f["fam"] == fam]
        if not ff:
            continue
        halves = halves_for(ff)
        base = cell_reading(ff, halves)
        fam_res = {"n": len(ff), "station_days": len({(f["st"], f["d"]) for f in ff}),
                   "days": len({f["d"] for f in ff}), "H1_days": halves["H1"][0], "H2_days": halves["H2"][0],
                   "unconditional": base, "conditions": {}, "counted": []}
        h1_src = [f for f in ff if f["d"] in halves["H1"][1].index]
        for cond in CONDITIONS:
            cid, name, layer, kind, key, cuts, labels, counted = cond
            bins, used_cuts = assign_bins(ff, cond, h1_src)
            if bins is None:
                continue
            entry = {"name": name, "layer": layer, "cuts": used_cuts, "bins": {}}
            labs = labels or sorted({b for b in bins.values() if b is not None})
            for lab in labs:
                items = [f for f in ff if bins[id(f)] == lab]
                if not items:
                    continue
                rd = cell_reading(items, halves)
                rd["r_leg_mode"] = None
                entry["bins"][lab] = rd
                if lab in counted:
                    v = verdict(rd, counted[lab])
                    entry["bins"][lab]["verdict"] = v
                    fam_res["counted"].append({"cond": cid, "bin": lab, "status": v.get("status"), "p": v.get("p_conf_H2")})
            fam_res["conditions"][cid] = entry
        # BH within family over the counted cells
        pv = [c["p"] if c["status"] == "survive" else None for c in fam_res["counted"]]
        passed = bh(pv)
        for i, c in enumerate(fam_res["counted"]):
            c["bh"] = bool(i in passed)
        fam_res["expected_false_positives"] = round(0.025 * sum(1 for c in fam_res["counted"] if c["status"] != "insufficient"), 2)
        results[fam] = fam_res
    return results


def section_B(feats):
    results = {}
    for fam in FAMILIES:
        ff = [f for f in feats if f["fam"] == fam]
        if not ff:
            continue
        halves = halves_for(ff)
        fam_res = {"compounds": {}, "G12": {}, "counted": []}
        for cname, rule in COMPOUNDS.items():
            items = [f for f in ff if rule(f)]
            rd = cell_reading(items, halves) if items else {"H1": {"station_days": 0, "enough": False}, "H2": {"station_days": 0, "enough": False}}
            v = verdict(rd, COMPOUND_EXPECT[cname]) if items else {"status": "insufficient"}
            rd["verdict"] = v
            fam_res["compounds"][cname] = rd
            fam_res["counted"].append({"cell": cname, "status": v.get("status"), "p": v.get("p_conf_H2")})
        # C7 / C8 are leg-level
        for cname, rule in (("C7", lambda lg: lg["pos"] == "tail" and lg["side"] < 0 and (lg["depth"] or 0) >= 50),
                            ("C8", lambda lg: lg["pos"] == "centre" and lg["side"] > 0 and lg["spread"] is not None and lg["spread"] <= 0.03)):
            legs = [dict(f, leg=lg) for f in ff for lg in f["legs"] if rule(lg)]
            for metric, key in (("r", "r"), ("pi", "pi")):
                rd = cell_reading(legs, halves, value=lambda x, key=key: x["leg"][key]) if legs else {"H1": {"enough": False}, "H2": {"enough": False}}
                v = verdict(rd, +1) if legs else {"status": "insufficient"}
                rd["verdict"] = v
                fam_res["compounds"][f"{cname}:{metric}"] = rd
                if metric == "r":
                    fam_res["counted"].append({"cell": cname, "status": v.get("status"), "p": v.get("p_conf_H2")})
        cells = collections.defaultdict(list)
        for f in ff:
            c = g12_cell(f)
            if c:
                cells[c].append(f)
        for c, items in sorted(cells.items()):
            rd = cell_reading(items, halves)
            v = verdict(rd, +1)
            rd["verdict"] = v
            fam_res["G12"][c] = rd
            fam_res["counted"].append({"cell": "G12:" + c, "status": v.get("status"), "p": v.get("p_conf_H2")})
        pv = [c["p"] if c["status"] == "survive" else None for c in fam_res["counted"]]
        passed = bh(pv)
        for i, c in enumerate(fam_res["counted"]):
            c["bh"] = bool(i in passed)
        fam_res["expected_false_positives"] = round(0.025 * sum(1 for c in fam_res["counted"] if c["status"] != "insufficient"), 2)
        results[fam] = fam_res
    return results


NUMERIC = ["dmu", "abs_dmu", "kl_pq", "js", "wr", "dH", "mode_agree", "mode_side", "share_c", "share_w", "share_s",
           "B", "neff", "seated", "consist", "dmu_p_prev", "traj_sd", "arrival_age_h",
           "snap_age_min", "dmu_q_prev", "l1_q_prev", "spread_mode", "depth_mode_ask", "ask_sum", "ms_dev", "H_q",
           "multimodal", "gap", "h", "unit_f", "clearness"]


def design(feats):
    X = []
    for f in feats:
        row = []
        for key in NUMERIC:
            if key == "abs_dmu":
                v = abs(f["dmu"])
            elif key == "unit_f":
                v = 1.0 if f["unit"] == "F" else 0.0
            else:
                v = f.get(key)
            if key == "depth_mode_ask" and v is not None:
                v = math.log1p(v)
            row.append(v)
        X.append(row)
    X = np.asarray(X, dtype=object)
    cols = []
    for j in range(X.shape[1]):
        col = np.asarray([np.nan if v is None else float(v) for v in X[:, j]])
        miss = np.isnan(col)
        cols.append(np.where(miss, 0.0, col))
        if miss.any():
            cols.append(miss.astype(float))
    return np.column_stack(cols)


def section_C(feats):
    from sklearn.ensemble import GradientBoostingRegressor
    results = {}
    for fam in ("P0>36h", "P0-M", "P1"):
        ff = [f for f in feats if f["fam"] == fam]
        if len(ff) < 500:
            continue
        days = sorted({f["d"] for f in ff})
        X = design(ff)
        yv = np.asarray([f["d_"] for f in ff])
        w = np.asarray([f["wt"] for f in ff])
        dd = np.asarray([f["d"] for f in ff])
        pred = {"ridge": np.full(len(ff), np.nan), "gbm": np.full(len(ff), np.nan)}
        for start in range(MIN_DAYS, len(days), 3):
            test_days = set(days[start:start + 3])
            train = np.isin(dd, days[:start])
            test = np.isin(dd, list(test_days))
            if train.sum() < 200 or test.sum() == 0:
                continue
            Xt = X[train]
            mu, sd = Xt.mean(axis=0), Xt.std(axis=0) + 1e-9
            Xs = (Xt - mu) / sd
            Xte = (X[test] - mu) / sd
            # ridge with intercept, alpha=1 after standardisation
            A = np.column_stack([np.ones(len(Xs)), Xs])
            W = np.sqrt(w[train])[:, None]
            reg = np.eye(A.shape[1]) * 1.0
            reg[0, 0] = 0.0
            beta = np.linalg.solve((A * W).T @ (A * W) + reg, (A * W).T @ (yv[train] * W[:, 0]))
            pred["ridge"][test] = np.column_stack([np.ones(len(Xte)), Xte]) @ beta
            gbm = GradientBoostingRegressor(max_depth=3, n_estimators=100, learning_rate=0.05, subsample=0.8, random_state=SEED)
            gbm.fit(Xs, yv[train], sample_weight=w[train])
            pred["gbm"][test] = gbm.predict(Xte)
        fam_res = {"n": len(ff), "oos_days": days[MIN_DAYS:]}
        for name, pr in pred.items():
            ok = ~np.isnan(pr)
            if ok.sum() < 100:
                continue
            from scipy.stats import spearmanr
            rho = float(spearmanr(pr[ok], yv[ok]).correlation)
            # weighted slope of realised on predicted
            x, yy, ww = pr[ok], yv[ok], w[ok]
            xm, ym = np.average(x, weights=ww), np.average(yy, weights=ww)
            slope = float(np.sum(ww * (x - xm) * (yy - ym)) / max(np.sum(ww * (x - xm) ** 2), 1e-12))
            # deciles of predicted d (weighted), realised mean per decile with day-block interval
            order = np.argsort(x)
            cw = np.cumsum(ww[order]) / ww.sum()
            dec = np.minimum((cw * 10).astype(int), 9)
            decile = np.empty(ok.sum(), dtype=int)
            decile[order] = dec
            items = [dict(ff[i], pred=float(pr[i])) for i in np.flatnonzero(ok)]
            boot = Boot(sorted({it["d"] for it in items}))
            table = []
            for q in range(10):
                sub = [it for it, dq in zip(items, decile) if dq == q]
                num, den = day_sums(sub, boot.days, lambda it: it["d_"], lambda it: it["wt"])
                s = boot.summarise(num, den) or {}
                table.append({"decile": q + 1, "n": len(sub), "pred_mean": float(np.mean([it["pred"] for it in sub])) if sub else None, **s})
            fam_res[name] = {"spearman": rho, "slope": slope, "deciles": table, "n_oos": int(ok.sum())}
        results[fam] = fam_res
    return results


def section_D(feats, resA, resB):
    """Economics by position (always) and for every surviving cell."""
    results = {}
    for fam in FAMILIES:
        ff = [f for f in feats if f["fam"] == fam]
        if not ff:
            continue
        halves = halves_for(ff)
        fam_res = {"paper": {}, "position": {}, "surviving": {}}
        # the paper's decomposition, whole ladder, lambda=|s|
        for key in ("d_", "D", "lambda_cost", "pi_exec"):
            fam_res["paper"][key] = cell_reading(ff, halves, value=lambda f, key=key: f.get(key))
        # legs by side x distance to the market mode
        legs = [dict(f, leg=lg) for f in ff for lg in f["legs"]]
        for side_name, side in (("YES", 1), ("NO", -1)):
            for dist_name, rule in (("mode", lambda lg: lg["dist"] == 0), ("±1", lambda lg: lg["dist"] == 1), ("≥2", lambda lg: lg["dist"] >= 2)):
                sub = [x for x in legs if x["leg"]["side"] == side and rule(x["leg"])]
                fam_res["position"][f"{side_name} {dist_name}"] = {
                    "claim": cell_reading(sub, halves, value=lambda x: x["leg"]["claim"]),
                    "realised_r": cell_reading(sub, halves, value=lambda x: x["leg"]["r"]),
                    "pi_taker": cell_reading(sub, halves, value=lambda x: x["leg"]["pi"]),
                }
        # surviving cells: re-read d, pi_exec and the market-only d
        surv = []
        for c in resA.get(fam, {}).get("counted", []):
            if c["status"] == "survive":
                surv.append(("A", c["cond"], c["bin"], c["bh"]))
        for c in resB.get(fam, {}).get("counted", []):
            if c["status"] == "survive":
                surv.append(("B", c["cell"], None, c["bh"]))
        h1_src = [f for f in ff if f["d"] in halves["H1"][1].index]
        for src, cid, lab, bhp in surv:
            if src == "A":
                cond = next(c for c in CONDITIONS if c[0] == cid)
                bins, _ = assign_bins(ff, cond, h1_src)
                items = [f for f in ff if bins[id(f)] == lab]
                key = f"A{cid}:{lab}"
            elif cid.startswith("G12:"):
                items = [f for f in ff if g12_cell(f) == cid[4:]]
                key = cid
            elif cid in COMPOUNDS:
                items = [f for f in ff if COMPOUNDS[cid](f)]
                key = cid
            else:
                continue
            fam_res["surviving"][key] = {
                "bh": bhp,
                "d": cell_reading(items, halves),
                "d_market_only": cell_reading(items, halves, value=lambda f: f["d_m1p"]),
                "pi_exec": cell_reading(items, halves, value=lambda f: f.get("pi_exec")),
                "D": cell_reading(items, halves, value=lambda f: f["D"]),
            }
        results[fam] = fam_res
    return results


def main():
    rows = load()
    feats = featurise(rows)
    print(f"rows={len(rows)} featured(with q)={len(feats)}")
    for fam in FAMILIES:
        ff = [f for f in feats if f["fam"] == fam]
        sds = len({(f["st"], f["d"]) for f in ff})
        nds = len({f["d"] for f in ff})
        print(f"  {fam:8s} n={len(ff):6d} station-days={sds:5d} days={nds}")
    out = {}
    if "A" in SECTIONS:
        out["A"] = section_A(feats)
        json.dump(out["A"], open(os.path.join(HERE, "results_A.json"), "w"), ensure_ascii=False, indent=1, default=str)
    if "B" in SECTIONS:
        out["B"] = section_B(feats)
        json.dump(out["B"], open(os.path.join(HERE, "results_B.json"), "w"), ensure_ascii=False, indent=1, default=str)
    if "C" in SECTIONS:
        out["C"] = section_C(feats)
        json.dump(out["C"], open(os.path.join(HERE, "results_C.json"), "w"), ensure_ascii=False, indent=1, default=str)
    if "D" in SECTIONS:
        resA = out.get("A") or json.load(open(os.path.join(HERE, "results_A.json")))
        resB = out.get("B") or json.load(open(os.path.join(HERE, "results_B.json")))
        out["D"] = section_D(feats, resA, resB)
        json.dump(out["D"], open(os.path.join(HERE, "results_D.json"), "w"), ensure_ascii=False, indent=1, default=str)
    print("done")


if __name__ == "__main__":
    main()
