"""Rain timing as an oracle condition (user, 2026-09-23: 「雨落的时间，峰值后肯定没信息」).

Same rows, folds, prior and bootstrap as ``fit_precip.py``.  Conditions are
0/1 indicators off ``timing_features.json``: rain before 15:00 local, rain
only after 15:00 (the control the user predicts is empty), rain before the
day's actual maximum, rain only after it; ASOS millimetre thresholds on the
before-peak amount.  Belief layer (on M1) and model layer (on p).  Usage:
``python3 fit_timing.py P0|P0-M|P1``; writes ``timing_<stage>.json``.
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
    timing = json.loads((HERE / "timing_features.json").read_text())
    rows = []
    for r, f, t in zip(data, feats, timing):
        if r["stage"] != STAGE:
            continue
        r["features"].update({"pc_" + k: v for k, v in f.items()})
        r["features"].update({"tm_" + k: v for k, v in t.items()})
        rows.append(r)
    S = FA.Stage(rows)
    g = S.get
    cond = {
        "pre15_present": g("tm_pre15_present"), "only_post15_present": g("tm_only_post15_present"),
        "premax_present": g("tm_premax_present"), "only_postmax_present": g("tm_only_postmax_present"),
        "pre15_ge1": ge(g("tm_pre15_mm"), 1.0), "pre15_ge5": ge(g("tm_pre15_mm"), 5.0), "pre15_ge10": ge(g("tm_pre15_mm"), 10.0),
        "premax_ge1": ge(g("tm_premax_mm"), 1.0), "premax_ge5": ge(g("tm_premax_mm"), 5.0), "premax_ge10": ge(g("tm_premax_mm"), 10.0),
        "postmax_ge5": ge(g("tm_postmax_mm"), 5.0),
    }
    Lp = S.Lm + S.D
    b_specs = {"M1": [], **{f"+{c}": [c] for c in cond}, "+pre15+post15": ["pre15_present", "only_post15_present"],
               "+premax+postmax": ["premax_present", "only_postmax_present"]}
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
    subsets = {"all": scored, "asos_stations": scored & (g("tm_asos") == 1.0), "evidence_epoch": scored & epoch}
    rng = np.random.default_rng(20260923)
    result = {"stage": STAGE, "wet_station_days": {}, "subsets": {}}
    for c, arr in cond.items():
        ok = scored & ~np.isnan(arr)
        result["wet_station_days"][c] = {"rows": int(ok.sum()), "wet_w": round(float((S.w * arr)[ok].sum()), 1),
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
            "murphy_belief": {n: murphy(n) for n in b_specs},
            "murphy_model": {n: murphy(n) for n in m_specs}}
    # Residuals in °C by timing group, model and market, within station.
    settled = g("pc_settled_tmax_c")
    pm = np.where(S.mask, S.m, 0.0)
    pm = pm / pm.sum(1, keepdims=True)
    mu_m = (pm * S.c).sum(1)
    station = np.array([k.split("|")[0] for k in S.sd])
    mode_hit = (np.where(S.mask, S.p, -1).argmax(1) == S.hit).astype(float)
    covered = g("tm_covered") == 1.0
    groups = {
        "dry_all_day": covered & (g("tm_pre15_present") == 0) & (g("tm_post15_present") == 0),
        "rain_only_after_15": covered & (g("tm_only_post15_present") == 1),
        "rain_before_15": covered & (g("tm_pre15_present") == 1),
        "rain_only_after_max": covered & (g("tm_only_postmax_present") == 1),
        "rain_before_max": covered & (g("tm_premax_present") == 1),
        "asos_pre15_lt1mm": covered & (g("tm_pre15_mm") < 1.0),
        "asos_pre15_1_5mm": covered & (g("tm_pre15_mm") >= 1.0) & (g("tm_pre15_mm") < 5.0),
        "asos_pre15_5_10mm": covered & (g("tm_pre15_mm") >= 5.0) & (g("tm_pre15_mm") < 10.0),
        "asos_pre15_ge10mm": covered & (g("tm_pre15_mm") >= 10.0),
        "asos_premax_ge5mm": covered & (g("tm_premax_mm") >= 5.0),
        "asos_postmax_ge5mm_only": covered & (g("tm_postmax_mm") >= 5.0) & (g("tm_premax_mm") < 1.0),
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
            sel = sel & scored & ~np.isnan(res)
            if sel.sum() < 5:
                continue
            w = S.w[sel]
            desc.setdefault(gname, {})[res_name] = {
                "station_days_w": round(float(w.sum()), 1),
                "residual_c_station_demeaned": round(float(np.average(dm[sel], weights=w)), 3),
                "residual_c": round(float(np.average(res[sel], weights=w)), 3),
                "mode_hit": round(float(np.average(mode_hit[sel], weights=w)), 3)}
    result["residual_by_timing"] = desc
    hours = g("tm_max_local_hour")
    result["max_local_hour_hist"] = {str(int(h)): int((hours[scored] == h).sum()) for h in sorted(set(hours[scored & ~np.isnan(hours)]))}
    result["seconds"] = round(time.time() - T0)
    (HERE / f"timing_{STAGE}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print("done", STAGE, f"{time.time() - T0:.0f}s")


if __name__ == "__main__":
    main()
