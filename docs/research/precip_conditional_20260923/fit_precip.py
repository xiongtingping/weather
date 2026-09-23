"""Walk-forward attribution of the precipitation conditions (PROTOCOL.md).

Usage: ``python3 fit_precip.py P0|P0-M|P1``.  Reuses the conditional-belief
study's Stage/columns/fit/log_prob and its fold, prior and bootstrap rules
(robust bounds); writes ``results_<stage>.json``.
"""
from __future__ import annotations

import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BASE = HERE.parent / "conditional_belief_20260923"
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
if "--robust" not in sys.argv:
    sys.argv.append("--robust")
import fit_attribute as FA  # noqa: E402
from calibration_dashboard import _core_murphy  # noqa: E402

STAGE = next(a for a in sys.argv[1:] if a in ("P0", "P0-M", "P1"))
ARMS = {"asof": "om_asof", "sameday": "om_sameday", "truth": "truth"}
T0 = time.time()


def instant(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def log1p(a):
    return np.log1p(np.maximum(0.0, a))


def conditions(S):
    cond = {
        "kt": S.get("kt"),
        "taf": S.get("precip"),
        "sofar": S.get("pc_sofar_present"),
        "om_asof": log1p(S.get("pc_om_asof")),
        "om_sameday": log1p(S.get("pc_om_sameday")),
        "truth": S.get("pc_truth_present"),
        "truth_mm": log1p(S.get("pc_truth_mm")),
    }
    if STAGE == "P1":
        cond["obs"] = S.get("obs_precipitation_present")
    return cond


def specs_for():
    base = ["kt", "taf"] + (["obs"] if STAGE == "P1" else [])
    sp = {"M1": [], "+kt": ["kt"], "+taf": ["taf"], "+sofar": ["sofar"], "base": base, "+truthmm": ["truth_mm"]}
    if STAGE == "P1":
        sp["+obs"] = ["obs"]
    for arm, om in ARMS.items():
        sp[f"+{arm}"] = [om]
        sp[f"+{arm}+kt"] = [om, "kt"]
        sp[f"+{arm}+taf"] = [om, "taf"]
        sp[f"+{arm}+sofar"] = [om, "sofar"]
        if STAGE == "P1":
            sp[f"+{arm}+obs"] = [om, "obs"]
        sp[f"base+{arm}"] = base + [om]
    return sp


def main():
    data = json.loads((BASE / "dataset.json").read_text())
    feats = json.loads((HERE / "precip_features.json").read_text())
    rows = []
    for r, f in zip(data, feats):
        if r["stage"] != STAGE:
            continue
        r["features"].update({"pc_" + k: v for k, v in f.items()})
        rows.append(r)
    S = FA.Stage(rows)
    cond = conditions(S)
    specs = specs_for()
    days = sorted(set(S.day))
    lp = {name: np.full(S.n, np.nan) for name in specs}
    probs = {name: np.full((S.n, S.k), np.nan) for name in specs}
    warm = {}
    folds = []
    for day in days:
        test = S.day == day
        first = S.at[test].min()
        train = S.known < first
        if len(set(S.day[train])) < FA.MIN_TRAIN_DAYS:
            continue
        folds.append(day)
        cache = {c: FA.columns(S, [(c, "g", cond[c], ("D", "dmu"))], train) for c in cond}
        for name, cs in specs.items():
            cols = [("Lm", S.Lm), ("D", S.D)] + [col for c in cs for col in cache[c]]
            X = np.stack([c[1] for c in cols], axis=2)
            theta = FA.fit(X[train], S.hit[train], S.mask[train], S.w[train], 2, warm.get(name))
            warm[name] = theta
            lpt, prob = FA.log_prob(X[test], theta, S.hit[test], S.mask[test])
            lp[name][test] = lpt
            probs[name][test] = prob
        print(STAGE, day, int(test.sum()), f"{time.time() - T0:.0f}s", flush=True)
    raw_m = np.log(np.maximum(S.m[np.arange(S.n), S.hit], FA.RAW_FLOOR)
                   / np.where(S.mask, np.maximum(S.m, FA.RAW_FLOOR), 0).sum(1))
    lp["raw_market"] = raw_m
    scored = ~np.isnan(lp["M1"])
    epoch = S.at >= instant(FA.EPOCH[STAGE])
    subsets = {
        "all": scored,
        "asof_available": scored & ~np.isnan(cond["om_asof"]),
        "asos_stations": scored & (S.get("pc_asos") == 1.0),
        "evidence_epoch": scored & epoch,
    }
    rng = np.random.default_rng(20260923)
    result = {"stage": STAGE, "protocol": "PROTOCOL.md", "variant": "robust", "folds": folds,
              "condition_rows": {c: int((~np.isnan(v)).sum()) for c, v in cond.items()},
              "subsets": {}}
    for label, mask in subsets.items():
        if mask.sum() < 30:
            result["subsets"][label] = {"decisions": int(mask.sum()), "skipped": True}
            continue
        keys, day_of, sdv = FA.station_day_table(S, mask, lp)

        def diff(a, b):
            d = sdv[a] - sdv[b]
            return {"mean": round(float(d.mean()), 5), "ci95": FA.block_bootstrap(day_of, d, rng)}

        out = {"decisions": int(mask.sum()), "station_days": len(keys), "event_days": len(set(day_of)),
               "first_day": min(day_of), "last_day": max(day_of),
               "log_score": {k: round(float(v.mean()), 5) for k, v in sdv.items()},
               "vs_M1": {k: diff(k, "M1") for k in specs if k != "M1"},
               "pairs": {}}
        for arm in ARMS:
            pr = {
                f"{arm}_over_kt": diff(f"+{arm}+kt", "+kt"),
                f"{arm}_over_taf": diff(f"+{arm}+taf", "+taf"),
                f"{arm}_over_sofar": diff(f"+{arm}+sofar", "+sofar"),
                f"{arm}_over_base": diff(f"base+{arm}", "base"),
                f"kt_over_{arm}": diff(f"+{arm}+kt", f"+{arm}"),
                f"taf_over_{arm}": diff(f"+{arm}+taf", f"+{arm}"),
                f"sofar_over_{arm}": diff(f"+{arm}+sofar", f"+{arm}"),
            }
            if STAGE == "P1":
                pr[f"{arm}_over_obs"] = diff(f"+{arm}+obs", "+obs")
                pr[f"obs_over_{arm}"] = diff(f"+{arm}+obs", f"+{arm}")
            out["pairs"].update(pr)
        # North star per model on this subset.
        copies = Counter(S.sd[i] for i in np.flatnonzero(mask))
        murphy = {}
        for name in ("raw_market", *specs):
            cells = []
            for i in np.flatnonzero(mask):
                k = int(S.mask[i].sum())
                v = S.m[i, :k] if name == "raw_market" else probs[name][i, :k]
                v = np.asarray(v, float)
                cells.append((1.0 / copies[S.sd[i]], {"probabilities": list(v / v.sum()), "hit": int(S.hit[i])}))
            m = _core_murphy(cells)
            murphy[name] = {"rel_e3": round(m["reliability"] * 1e3, 3), "res_e3": round(m["resolution"] * 1e3, 3),
                            "brier": round(m["mean_bucket_brier"], 5)}
        out["murphy"] = murphy
        result["subsets"][label] = out
    # Descriptive: does precipitation separate outcomes at all (station-demeaned z).
    settled = S.get("pc_settled_tmax_c")
    z = (settled - S.mu) / S.sigma
    station = np.array([k.split("|")[0] for k in S.sd])
    zdm = z.copy()
    for st in set(station):
        sel = station == st
        ok = sel & ~np.isnan(z)
        if ok.sum():
            zdm[sel] = z[sel] - np.average(z[ok], weights=S.w[ok])
    mode_hit = (np.where(S.mask, S.p, -1).argmax(1) == S.hit).astype(float)
    desc = {}

    def binned(name, arr, edges, labels):
        rows_out = []
        for lo, hi, lab in zip(edges[:-1], edges[1:], labels):
            sel = scored & ~np.isnan(arr) & (arr >= lo) & (arr < hi)
            if sel.sum() < 5:
                continue
            w = S.w[sel]
            rows_out.append({"bin": lab, "decisions": int(sel.sum()), "station_days_w": round(float(w.sum()), 1),
                             "z_mean": round(float(np.average(z[sel], weights=w)), 3),
                             "z_station_demeaned": round(float(np.average(zdm[sel], weights=w)), 3),
                             "mode_hit": round(float(np.average(mode_hit[sel], weights=w)), 3),
                             "lp_M1": round(float(np.average(lp["M1"][sel], weights=w)), 4),
                             "lp_market": round(float(np.average(lp["raw_market"][sel], weights=w)), 4)})
        desc[name] = rows_out

    mm_edges = [-0.1, 0.05, 1.0, 5.0, 1e9]
    mm_labels = ["0 mm", "0–1 mm", "1–5 mm", "≥5 mm"]
    binned("om_sameday_mm", S.get("pc_om_sameday"), mm_edges, mm_labels)
    binned("om_asof_mm", S.get("pc_om_asof"), mm_edges, mm_labels)
    binned("truth_mm_asos", S.get("pc_truth_mm"), mm_edges, mm_labels)
    binned("truth_present", S.get("pc_truth_present"), [-0.5, 0.5, 1.5], ["dry", "wet"])
    binned("sofar_present", S.get("pc_sofar_present"), [-0.5, 0.5, 1.5], ["dry so far", "wet so far"])
    binned("taf_precip", S.get("precip"), [-0.01, 0.01, 0.5, 1.01], ["0", "0–0.5", "≥0.5"])
    kt = S.get("kt")
    if (~np.isnan(kt)).sum() > 30:
        q = np.nanpercentile(kt, [33.3, 66.7])
        binned("kt_terciles", kt, [-1e9, q[0], q[1], 1e9], ["low kt (cloudy)", "mid", "high kt (clear)"])
    if STAGE == "P1":
        binned("obs_present", S.get("obs_precipitation_present"), [-0.5, 0.5, 1.5], ["dry", "wet"])
    # Correlations among the conditions, pooled and within station.
    names = [c for c in cond if (~np.isnan(cond[c])).sum() > 30]
    corr = {"pooled": {}, "within_station": {}, "n": {}}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ok = scored & ~np.isnan(cond[a]) & ~np.isnan(cond[b])
            if ok.sum() < 30:
                continue
            x, y = cond[a][ok], cond[b][ok]
            corr["n"][f"{a}~{b}"] = int(ok.sum())
            corr["pooled"][f"{a}~{b}"] = round(float(np.corrcoef(x, y)[0, 1]), 3) if x.std() > 0 and y.std() > 0 else None
            xd, yd = x.copy(), y.copy()
            for st in set(station[ok]):
                sel = station[ok] == st
                xd[sel] -= x[sel].mean()
                yd[sel] -= y[sel].mean()
            corr["within_station"][f"{a}~{b}"] = round(float(np.corrcoef(xd, yd)[0, 1]), 3) if xd.std() > 0 and yd.std() > 0 else None
    # All-data coefficients of the two ceiling models, to read (never scored).
    everything = np.ones(S.n, bool)
    coef = {}
    for name in ("base+sameday", "base+truth", "base+asof"):
        cache = {c: FA.columns(S, [(c, "g", cond[c], ("D", "dmu"))], everything) for c in specs[name]}
        cols = [("Lm", S.Lm), ("D", S.D)] + [col for c in specs[name] for col in cache[c]]
        X = np.stack([c[1] for c in cols], axis=2)
        theta = FA.fit(X, S.hit, S.mask, S.w, 2)
        coef[name] = [{"term": c[0], "coef": round(float(t), 4)} for c, t in zip(cols, theta)]
    result.update({"descriptive": desc, "correlations": corr, "coefficients_all_data": coef,
                   "seconds": round(time.time() - T0)})
    (HERE / f"results_{STAGE}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print("done", STAGE, f"{time.time() - T0:.0f}s")


if __name__ == "__main__":
    main()
