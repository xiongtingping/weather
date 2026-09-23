"""Model-side companion: does precipitation sharpen the served p itself?

Same rows, folds, prior and bootstrap as ``fit_precip.py``, but the base is
the model alone (score = β·logit p, recalibrated walk-forward) and each
condition enters as a centre shift g·(c − μp).  Scored by out-of-sample log
score, RPS and REL/RES.  Also reads the market-implied centre's residual by
rain bin, to see whether the market already prices rain days.
Usage: ``python3 fit_model_side.py P0|P0-M|P1``; writes ``model_side_<stage>.json``.
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
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
import fit_precip as FP  # noqa: E402
from calibration_dashboard import _core_murphy  # noqa: E402

STAGE = FP.STAGE
T0 = time.time()


def instant(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def columns_model_side(S, name, g, train):
    """One standardised centre-shift column, g·(c − μp), with no missing flag.

    ``fit_attribute.columns`` adds ``missing×D`` for a condition that is
    sometimes missing, and D = logit p − logit m carries the market: on the
    model-only base that term let the market in through the back door (the
    first run read +0.26 nat for a TAF condition covering 18% of P0 rows).
    Here a missing condition is simply zero.
    """

    missing = np.isnan(g)
    ok = train & ~missing
    if ok.sum() < 20:
        return []
    mean = np.average(g[ok], weights=S.w[ok])
    sd = float(np.sqrt(np.average((g[ok] - mean) ** 2, weights=S.w[ok])))
    if sd < 1e-9:
        return []
    z = np.clip(np.where(missing, 0.0, (g - mean) / sd), -FA.Z_CAP, FA.Z_CAP)
    arr = np.where(S.mask, z[:, None] * S.dmu, 0.0)
    scale = float(arr[train][S.mask[train]].std())
    if not np.isfinite(scale) or scale < 1e-9:
        return []
    return [(f"{name}*dmu", arr / scale)]


def rps(prob, mask, hit):
    out = np.zeros(len(hit))
    for i in range(len(hit)):
        k = int(mask[i].sum())
        p = np.asarray(prob[i, :k], float)
        p = p / p.sum()
        F = np.cumsum(p)
        H = (np.arange(k) >= hit[i]).astype(float)
        out[i] = float(((F - H) ** 2).sum() / max(1, k - 1))
    return out


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
    cond = FP.conditions(S)
    Lp = S.Lm + S.D
    base = ["kt", "taf"] + (["obs"] if STAGE == "P1" else [])
    specs = {"Mp": [], "Mp+kt": ["kt"], "Mp+taf": ["taf"], "Mp+sofar": ["sofar"], "Mp+base": base,
             "Mp+asof": ["om_asof"], "Mp+sameday": ["om_sameday"], "Mp+truth": ["truth"], "Mp+truthmm": ["truth_mm"],
             "Mp+kt+sameday": ["kt", "om_sameday"], "Mp+kt+truth": ["kt", "truth"],
             "Mp+base+sameday": base + ["om_sameday"], "Mp+base+truth": base + ["truth"]}
    if STAGE == "P1":
        specs["Mp+obs"] = ["obs"]
    days = sorted(set(S.day))
    lp = {name: np.full(S.n, np.nan) for name in specs}
    probs = {name: np.full((S.n, S.k), np.nan) for name in specs}
    warm = {}
    for day in days:
        test = S.day == day
        first = S.at[test].min()
        train = S.known < first
        if len(set(S.day[train])) < FA.MIN_TRAIN_DAYS:
            continue
        cache = {c: columns_model_side(S, c, cond[c], train) for c in cond}
        for name, cs in specs.items():
            cols = [("Lp", Lp)] + [col for c in cs for col in cache[c]]
            X = np.stack([c[1] for c in cols], axis=2)
            theta = FA.fit(X[train], S.hit[train], S.mask[train], S.w[train], 1, warm.get(name))
            warm[name] = theta
            lpt, prob = FA.log_prob(X[test], theta, S.hit[test], S.mask[test])
            lp[name][test] = lpt
            probs[name][test] = prob
        print(STAGE, day, f"{time.time() - T0:.0f}s", flush=True)
    scored = ~np.isnan(lp["Mp"])
    raw_p = np.log(np.maximum(S.p[np.arange(S.n), S.hit], FA.RAW_FLOOR) / np.where(S.mask, np.maximum(S.p, FA.RAW_FLOOR), 0).sum(1))
    lp["raw_model"] = raw_p
    rp = {name: rps(probs[name], S.mask, S.hit) for name in specs}
    rp["raw_model"] = rps(S.p, S.mask, S.hit)
    rp["raw_market"] = rps(S.m, S.mask, S.hit)
    epoch = S.at >= instant(FA.EPOCH[STAGE])
    subsets = {"all": scored, "asof_available": scored & ~np.isnan(cond["om_asof"]),
               "asos_stations": scored & (S.get("pc_asos") == 1.0), "evidence_epoch": scored & epoch}
    rng = np.random.default_rng(20260923)
    result = {"stage": STAGE, "subsets": {}}
    for label, mask in subsets.items():
        if mask.sum() < 30:
            result["subsets"][label] = {"decisions": int(mask.sum()), "skipped": True}
            continue
        keys, day_of, sdv = FA.station_day_table(S, mask, lp)
        _, _, sdr = FA.station_day_table(S, mask, rp)

        def diff(tab, a, b):
            d = tab[a] - tab[b]
            return {"mean": round(float(d.mean()), 5), "ci95": FA.block_bootstrap(day_of, d, rng)}

        out = {"decisions": int(mask.sum()), "station_days": len(keys), "event_days": len(set(day_of)),
               "log_score": {k: round(float(v.mean()), 5) for k, v in sdv.items()},
               "rps": {k: round(float(v.mean()), 5) for k, v in sdr.items()},
               "lp_vs_Mp": {k: diff(sdv, k, "Mp") for k in specs if k != "Mp"},
               "rps_vs_Mp": {k: diff(sdr, k, "Mp") for k in specs if k != "Mp"},
               "pairs_rps": {"sameday_over_kt": diff(sdr, "Mp+kt+sameday", "Mp+kt"), "truth_over_kt": diff(sdr, "Mp+kt+truth", "Mp+kt"),
                             "sameday_over_base": diff(sdr, "Mp+base+sameday", "Mp+base"), "truth_over_base": diff(sdr, "Mp+base+truth", "Mp+base"),
                             "kt_over_sameday": diff(sdr, "Mp+kt+sameday", "Mp+sameday"), "kt_over_truth": diff(sdr, "Mp+kt+truth", "Mp+truth")}}
        copies = Counter(S.sd[i] for i in np.flatnonzero(mask))
        murphy = {}
        for name in ("raw_model", "raw_market", *specs):
            cells = []
            for i in np.flatnonzero(mask):
                k = int(S.mask[i].sum())
                v = S.p[i, :k] if name == "raw_model" else S.m[i, :k] if name == "raw_market" else probs[name][i, :k]
                v = np.asarray(v, float)
                cells.append((1.0 / copies[S.sd[i]], {"probabilities": list(v / v.sum()), "hit": int(S.hit[i])}))
            m = _core_murphy(cells)
            murphy[name] = {"rel_e3": round(m["reliability"] * 1e3, 3), "res_e3": round(m["resolution"] * 1e3, 3)}
        out["murphy"] = murphy
        result["subsets"][label] = out
    # Market-implied centre residual by rain bin (does the market price rain days?).
    settled = S.get("pc_settled_tmax_c")
    pm = np.where(S.mask, S.m, 0.0)
    pm = pm / pm.sum(1, keepdims=True)
    mu_m = (pm * S.c).sum(1)
    sd_m = np.sqrt(np.maximum(1e-6, (pm * (S.c - mu_m[:, None]) ** 2).sum(1)))
    station = np.array([k.split("|")[0] for k in S.sd])
    desc = {}
    for name, arr, edges, labels in (
        ("truth_present", S.get("pc_truth_present"), [-0.5, 0.5, 1.5], ["dry", "wet"]),
        ("om_sameday_mm", S.get("pc_om_sameday"), [-0.1, 0.05, 1.0, 5.0, 1e9], ["0 mm", "0–1 mm", "1–5 mm", "≥5 mm"]),
    ):
        # Residuals in degrees (not σ units: P1's σ collapses near settlement).
        res_p = settled - S.mu
        res_m = settled - mu_m
        for arr_name, res in (("model_c", res_p), ("market_c", res_m)):
            dm = res.copy()
            for st in set(station):
                sel = station == st
                ok = sel & ~np.isnan(res) & scored
                if ok.sum():
                    dm[sel] = res[sel] - np.average(res[ok], weights=S.w[ok])
            rows_out = []
            for lo, hi, lab in zip(edges[:-1], edges[1:], labels):
                sel = scored & ~np.isnan(arr) & (arr >= lo) & (arr < hi) & ~np.isnan(res)
                if sel.sum() < 5:
                    continue
                w = S.w[sel]
                rows_out.append({"bin": lab, "station_days_w": round(float(w.sum()), 1),
                                 "residual_c": round(float(np.average(res[sel], weights=w)), 3),
                                 "residual_c_station_demeaned": round(float(np.average(dm[sel], weights=w)), 3)})
            desc[f"{name}:{arr_name}"] = rows_out
    result["centre_residual_by_rain"] = desc
    result["seconds"] = round(time.time() - T0)
    (HERE / f"model_side_{STAGE}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print("done", STAGE, f"{time.time() - T0:.0f}s")


if __name__ == "__main__":
    main()
