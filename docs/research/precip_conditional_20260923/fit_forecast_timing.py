"""Forecast rain timing as a condition (the forecast-side counterpart of fit_timing.py).

Conditions off ``forecast_timing_features.json``: the short-lead archive's
(``hist_``) and the as-of (``asof_``) millimetres before the 15:00 peak hour
and after it, as 0/1 thresholds and as log1p amounts; rain forecast only
after the peak is the control.  Belief layer on M1, model layer on p; same
folds, prior and bootstrap.  Usage: ``python3 fit_forecast_timing.py P0|P0-M|P1``;
writes ``forecast_timing_<stage>.json``.
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
from fit_model_side import columns_model_side, rps  # noqa: E402
from calibration_dashboard import _core_murphy  # noqa: E402

STAGE = next(a for a in sys.argv[1:] if a in ("P0", "P0-M", "P1"))
T0 = time.time()


def instant(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def ge(arr, t):
    return np.where(np.isnan(arr), np.nan, (arr >= t).astype(float))


def main():
    data = json.loads((BASE / "dataset.json").read_text())
    feats = json.loads((HERE / "precip_features.json").read_text())
    fc = json.loads((HERE / "forecast_timing_features.json").read_text())
    rows = []
    for r, f, t in zip(data, feats, fc):
        if r["stage"] != STAGE:
            continue
        r["features"].update({"pc_" + k: v for k, v in f.items()})
        r["features"].update({"fc_" + k: v for k, v in t.items()})
        rows.append(r)
    S = FA.Stage(rows)
    g = S.get
    cond = {}
    for src in ("hist", "asof"):
        pre = g(f"fc_{src}_precip_before_peak_mm")
        post = g(f"fc_{src}_precip_after_peak_mm")
        total = g(f"fc_{src}_precip_sum_mm")
        first = g(f"fc_{src}_first_wet_hour")
        cond[f"{src}_pre_ge1"] = ge(pre, 1.0)
        cond[f"{src}_pre_ge5"] = ge(pre, 5.0)
        cond[f"{src}_pre_ge10"] = ge(pre, 10.0)
        cond[f"{src}_pre_log1p"] = np.log1p(np.maximum(0.0, pre))
        cond[f"{src}_post_only_ge1"] = np.where(np.isnan(pre) | np.isnan(post), np.nan, ((post >= 1.0) & (pre < 1.0)).astype(float))
        cond[f"{src}_total_ge1"] = ge(total, 1.0)
        cond[f"{src}_first_wet_before_12"] = np.where(np.isnan(total), np.nan, ((~np.isnan(first)) & (first <= 12)).astype(float))
    Lp = S.Lm + S.D
    b_specs = {"M1": [], **{f"+{c}": [c] for c in cond}}
    m_specs = {"Mp": [], **{f"Mp+{c}": [c] for c in cond}}
    days = sorted(set(S.day))
    lp = {n: np.full(S.n, np.nan) for n in (*b_specs, *m_specs)}
    probs = {n: np.full((S.n, S.k), np.nan) for n in (*b_specs, *m_specs)}
    warm = {}
    for day in days:
        test = S.day == day
        first = S.at[test].min()
        train = S.known < first
        if len(set(S.day[train])) < FA.MIN_TRAIN_DAYS:
            continue
        bcols = {c: FA.columns(S, [(c, "g", cond[c], ("D", "dmu"))], train) for c in cond}
        mcols = {c: columns_model_side(S, c, cond[c], train) for c in cond}
        for name, cs in b_specs.items():
            cols = [("Lm", S.Lm), ("D", S.D)] + [col for c in cs for col in bcols[c]]
            X = np.stack([c[1] for c in cols], axis=2)
            theta = FA.fit(X[train], S.hit[train], S.mask[train], S.w[train], 2, warm.get(name))
            warm[name] = theta
            lp[name][test], probs[name][test] = FA.log_prob(X[test], theta, S.hit[test], S.mask[test])
        for name, cs in m_specs.items():
            cols = [("Lp", Lp)] + [col for c in cs for col in mcols[c]]
            X = np.stack([c[1] for c in cols], axis=2)
            theta = FA.fit(X[train], S.hit[train], S.mask[train], S.w[train], 1, warm.get(name))
            warm[name] = theta
            lp[name][test], probs[name][test] = FA.log_prob(X[test], theta, S.hit[test], S.mask[test])
        print(STAGE, day, f"{time.time() - T0:.0f}s", flush=True)
    scored = ~np.isnan(lp["M1"])
    rp = {n: rps(probs[n], S.mask, S.hit) for n in m_specs}
    epoch = S.at >= instant(FA.EPOCH[STAGE])
    subsets = {"all": scored, "asos_stations": scored & (g("pc_asos") == 1.0), "evidence_epoch": scored & epoch}
    rng = np.random.default_rng(20260923)
    result = {"stage": STAGE, "wet_station_days": {}, "subsets": {}}
    for c, arr in cond.items():
        ok = scored & ~np.isnan(arr)
        result["wet_station_days"][c] = {"rows": int(ok.sum()), "wet_w": round(float((S.w * arr)[ok].sum()), 1) if "log1p" not in c else None,
                                         "total_w": round(float(S.w[ok].sum()), 1)}
    for label, mask in subsets.items():
        if mask.sum() < 30:
            result["subsets"][label] = {"decisions": int(mask.sum()), "skipped": True}
            continue
        keys, day_of, sdv = FA.station_day_table(S, mask, lp)
        _, _, sdr = FA.station_day_table(S, mask, rp)

        def diff(tab, a, b):
            d = tab[a] - tab[b]
            return {"mean": round(float(d.mean()), 5), "ci95": FA.block_bootstrap(day_of, d, rng)}

        copies = Counter(S.sd[i] for i in np.flatnonzero(mask))

        def murphy(name):
            cells = []
            for i in np.flatnonzero(mask):
                k = int(S.mask[i].sum())
                v = np.asarray(probs[name][i, :k], float)
                cells.append((1.0 / copies[S.sd[i]], {"probabilities": list(v / v.sum()), "hit": int(S.hit[i])}))
            m = _core_murphy(cells)
            return {"rel_e3": round(m["reliability"] * 1e3, 3), "res_e3": round(m["resolution"] * 1e3, 3)}

        result["subsets"][label] = {
            "decisions": int(mask.sum()), "station_days": len(keys), "event_days": len(set(day_of)),
            "belief_vs_M1": {n: diff(sdv, n, "M1") for n in b_specs if n != "M1"},
            "model_rps_vs_Mp": {c: diff(sdr, f"Mp+{c}", "Mp") for c in cond},
            "model_lp_vs_Mp": {c: diff(sdv, f"Mp+{c}", "Mp") for c in cond},
            "murphy_belief": {n: murphy(n) for n in b_specs},
            "murphy_model": {n: murphy(n) for n in m_specs}}
    settled = g("pc_settled_tmax_c")
    pm = np.where(S.mask, S.m, 0.0)
    pm = pm / pm.sum(1, keepdims=True)
    mu_m = (pm * S.c).sum(1)
    station = np.array([k.split("|")[0] for k in S.sd])
    mode_hit = (np.where(S.mask, S.p, -1).argmax(1) == S.hit).astype(float)
    pre = g("fc_hist_precip_before_peak_mm")
    post = g("fc_hist_precip_after_peak_mm")
    groups = {
        "fc_dry": (pre < 1.0) & (post < 1.0),
        "fc_rain_only_after_peak": (pre < 1.0) & (post >= 1.0),
        "fc_pre_1_5mm": (pre >= 1.0) & (pre < 5.0),
        "fc_pre_5_10mm": (pre >= 5.0) & (pre < 10.0),
        "fc_pre_ge10mm": pre >= 10.0,
    }
    desc = {}
    for res_name, res in (("model", settled - S.mu), ("market", settled - mu_m)):
        dm = res.copy()
        for st in set(station):
            sel = station == st
            ok = sel & ~np.isnan(res) & scored
            if ok.sum():
                dm[sel] = res[sel] - np.average(res[ok], weights=S.w[ok])
        for gname, sel in groups.items():
            sel = sel & scored & ~np.isnan(res) & ~np.isnan(pre)
            if sel.sum() < 5:
                continue
            w = S.w[sel]
            desc.setdefault(gname, {})[res_name] = {
                "station_days_w": round(float(w.sum()), 1),
                "residual_c_station_demeaned": round(float(np.average(dm[sel], weights=w)), 3),
                "mode_hit": round(float(np.average(mode_hit[sel], weights=w)), 3)}
    result["residual_by_forecast_timing"] = desc
    result["seconds"] = round(time.time() - T0)
    (HERE / f"forecast_timing_{STAGE}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print("done", STAGE, f"{time.time() - T0:.0f}s")


if __name__ == "__main__":
    main()
