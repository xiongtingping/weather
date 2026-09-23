"""Higher rain thresholds (user, 2026-09-23: 「把降雨的阈值调高测试，0-1mm 太低了」).

Same rows, folds, prior and bootstrap as ``fit_precip.py``; each condition is
the 0/1 indicator "the day's precipitation ≥ T mm" for T in 1, 5, 10, 20, on
three sources: the same-day Open-Meteo capture (look-ahead ceiling), the
as-of capture (honest) and the ASOS observed amount (oracle, 11 stations).
Belief layer (on M1, g·D and g·(c−μp)) and model layer (on p, g·(c−μp))
both.  Usage: ``python3 fit_threshold.py P0|P0-M|P1``; writes
``threshold_<stage>.json``.
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
THRESHOLDS = (1.0, 5.0, 10.0, 20.0)
T0 = time.time()


def instant(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def indicator(arr, threshold):
    return np.where(np.isnan(arr), np.nan, (arr >= threshold).astype(float))


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
    sources = {"sameday": S.get("pc_om_sameday"), "asof": S.get("pc_om_asof"), "truth": S.get("pc_truth_mm")}
    cond = {f"{src}_ge{T:g}": indicator(arr, T) for src, arr in sources.items() for T in THRESHOLDS}
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
    subsets = {"all": scored, "asos_stations": scored & (S.get("pc_asos") == 1.0),
               "asof_available": scored & ~np.isnan(sources["asof"]), "evidence_epoch": scored & epoch}
    rng = np.random.default_rng(20260923)
    result = {"stage": STAGE, "thresholds_mm": THRESHOLDS,
              "wet_station_days": {}, "subsets": {}}
    # How many station-days each condition calls wet (weighted, scored rows).
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

        out = {"decisions": int(mask.sum()), "station_days": len(keys), "event_days": len(set(day_of)),
               "belief_vs_M1": {c: diff(sdv, f"+{c}", "M1") for c in cond},
               "model_rps_vs_Mp": {c: diff(sdr, f"Mp+{c}", "Mp") for c in cond},
               "model_lp_vs_Mp": {c: diff(sdv, f"Mp+{c}", "Mp") for c in cond},
               "murphy_belief": {n: murphy(n) for n in b_specs},
               "murphy_model": {n: murphy(n) for n in m_specs}}
        result["subsets"][label] = out
    # Residuals in °C by threshold bin, model and market, within station.
    settled = S.get("pc_settled_tmax_c")
    pm = np.where(S.mask, S.m, 0.0)
    pm = pm / pm.sum(1, keepdims=True)
    mu_m = (pm * S.c).sum(1)
    station = np.array([k.split("|")[0] for k in S.sd])
    mode_hit = (np.where(S.mask, S.p, -1).argmax(1) == S.hit).astype(float)
    desc = {}
    for src, arr in sources.items():
        for res_name, res in (("model", settled - S.mu), ("market", settled - mu_m)):
            dm = res.copy()
            for st in set(station):
                sel = station == st
                ok = sel & ~np.isnan(res) & scored
                if ok.sum():
                    dm[sel] = res[sel] - np.average(res[ok], weights=S.w[ok])
            rows_out = []
            edges = [-0.1, 1.0, 5.0, 10.0, 20.0, 1e9]
            labels = ["<1 mm", "1–5 mm", "5–10 mm", "10–20 mm", "≥20 mm"]
            for lo, hi, lab in zip(edges[:-1], edges[1:], labels):
                sel = scored & ~np.isnan(arr) & (arr >= lo) & (arr < hi) & ~np.isnan(res)
                if sel.sum() < 5:
                    continue
                w = S.w[sel]
                rows_out.append({"bin": lab, "station_days_w": round(float(w.sum()), 1),
                                 "residual_c_station_demeaned": round(float(np.average(dm[sel], weights=w)), 3),
                                 "residual_c": round(float(np.average(res[sel], weights=w)), 3),
                                 "mode_hit": round(float(np.average(mode_hit[sel], weights=w)), 3)})
            desc[f"{src}:{res_name}"] = rows_out
    result["residual_by_bin"] = desc
    result["seconds"] = round(time.time() - T0)
    (HERE / f"threshold_{STAGE}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print("done", STAGE, f"{time.time() - T0:.0f}s")


if __name__ == "__main__":
    main()
