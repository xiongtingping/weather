"""Phase 0 score: clear-sky ceiling discriminative power on decisions.jsonl.gz.

Station-day equal weight (rows averaged within a station-day), bootstrap by
event date (2,000 draws, seed 7).  Masked buckets keep the served 1e-3 floor
(a truth above the ceiling costs log(1e-3), never infinity).
"""
from __future__ import annotations

import gzip
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

Q_ARMS = ("q95", "q90", "q50")
BOOT = 2000


def floored(order, p):
    vec, _ = floor_probabilities(dict(zip(order, p)), ())
    return np.array([vec[b] for b in order])


def scores(pv, rec, ti):
    ls = -math.log(max(pv[ti], 1e-300))
    brier = float(((pv - (np.arange(len(pv)) == ti)) ** 2).sum())
    edges = np.array(rec["upper"][:-1])
    widths = np.diff(np.concatenate([[edges[0] - (edges[1] - edges[0])], edges]))
    F = np.cumsum(pv)[:-1]
    crps = float((((F - (rec["truth_c"] <= edges)) ** 2) * widths).sum())
    return ls, crps, brier


def main():
    recs = [json.loads(l) for l in gzip.open(HERE / "decisions.jsonl.gz", "rt")]
    # Amendment (after the first run, see RESULTS.md): a replayed prefix
    # (SILENT_PREFIX_NOT_CURRENT) carries the compile instant as its decision
    # clock, not the instant its running high was observed, so S0_rem read off
    # that clock is wrong for it.  Only rows whose prefix is current at the
    # compile instant are decisions at their own clock.
    n_all = len(recs)
    recs = [r for r in recs if r["ol_state"] == "APPLIED"]
    print("rows all", n_all, "prefix-current rows", len(recs))
    n = len(recs)
    sd_key = {}
    day_key = {}
    sd_idx = np.zeros(n, int)
    day_of_sd = {}
    cols = defaultdict(lambda: np.full(n, np.nan))
    for i, r in enumerate(recs):
        k = r["station_id"] + "|" + r["event_date"]
        sd_idx[i] = sd_key.setdefault(k, len(sd_key))
        day_of_sd[sd_idx[i]] = day_key.setdefault(r["event_date"], len(day_key))
        order = r["order"]
        ti = order.index(r["truth_bucket"])
        lower = np.array(r["lower"])
        for arm in ("p1", "root"):
            p = r[arm]
            if p is None:
                continue
            base = floored(order, p)
            b = scores(base, r, ti)
            for q in Q_ARMS:
                c = r["ceil"][q]
                mask = lower >= c
                raw = np.array(p)
                above = float(raw[mask].sum())
                kept = np.where(mask, 0.0, raw)
                s = kept.sum()
                m = floored(order, (kept / s) if s > 0 else raw)
                a = scores(m, r, ti)
                cols[f"{arm}|{q}|dlog"][i] = a[0] - b[0]
                cols[f"{arm}|{q}|dcrps"][i] = a[1] - b[1]
                cols[f"{arm}|{q}|dbrier"][i] = a[2] - b[2]
                cols[f"{arm}|{q}|mass"][i] = above
                cols[f"{arm}|{q}|viol"][i] = float(r["truth_c"] >= c)
    print("rows", n, "station-days", len(sd_key), "days", len(day_key))
    n_sd = len(sd_key)
    sd_day = np.array([day_of_sd[j] for j in range(n_sd)])
    n_day = len(day_key)
    rng = np.random.default_rng(7)
    draws = rng.multinomial(n_day, np.full(n_day, 1.0 / n_day), size=BOOT)  # day resample counts

    hours = np.array([r["hours_to_end"] for r in recs])
    lhour = np.array([r["local_hour"] for r in recs])
    unit_f = np.array([r["unit"] == "F" for r in recs])
    official = np.array([r["rad_official"] == 1 for r in recs])
    applied = np.array([r["ol_state"] == "APPLIED" for r in recs])
    slices = {
        "all": np.ones(n, bool),
        "local_12_18": (lhour >= 12) & (lhour < 18),
        "hours_to_end_6_10": (hours >= 6) & (hours < 10),
        "hours_to_end_lt_3": hours < 3,
        "goes_official": official,
        "relay_source": ~official,
        "f_station": unit_f,
        "mass_above_ge_2pct_q95": cols["p1|q95|mass"] >= 0.02,
        "ol_applied": applied,
    }

    def sd_mean(col, sel):
        ok = sel & ~np.isnan(col)
        s = np.bincount(sd_idx[ok], weights=col[ok], minlength=n_sd)
        c = np.bincount(sd_idx[ok], minlength=n_sd)
        has = c > 0
        return np.where(has, s / np.maximum(c, 1), 0.0), has

    def boot(col, sel):
        m, has = sd_mean(col, sel)
        if has.sum() == 0:
            return None
        day_sum = np.bincount(sd_day, weights=m * has, minlength=n_day)
        day_cnt = np.bincount(sd_day, weights=has.astype(float), minlength=n_day)
        point = day_sum.sum() / day_cnt.sum()
        bs = (draws @ day_sum) / np.maximum(draws @ day_cnt, 1e-9)
        lo, hi = np.quantile(bs, [0.025, 0.975])
        return [float(point), float(lo), float(hi)], int(has.sum())

    out = {"arms": {}, "by_hours_to_end": {}}
    for arm in ("p1", "root"):
        for q in Q_ARMS:
            for name, sel in slices.items():
                r = {}
                for metric in ("dlog", "dcrps", "dbrier"):
                    b = boot(cols[f"{arm}|{q}|{metric}"], sel)
                    if b:
                        r[metric], r["station_days"] = b
                if r:
                    mass = cols[f"{arm}|{q}|mass"]
                    ok = sel & ~np.isnan(mass)
                    r["rows"] = int(ok.sum())
                    r["mass_above_mean"] = float(mass[ok].mean())
                    r["mass_above_p90"] = float(np.quantile(mass[ok], 0.9))
                    r["rows_mass_ge_2pct"] = float((mass[ok] >= 0.02).mean())
                    r["truth_above_ceiling_rate_rows"] = float(cols[f"{arm}|{q}|viol"][ok].mean())
                    v, has = sd_mean(cols[f"{arm}|{q}|viol"], sel)
                    r["truth_above_ceiling_rate_station_days_any"] = float((v[has] > 0).mean())
                    out["arms"][f"{arm}|{q}|{name}"] = r
    for lo_, hi_ in ((0, 2), (2, 4), (4, 6), (6, 8), (8, 10), (10, 99)):
        sel = (hours >= lo_) & (hours < hi_)
        if sel.sum():
            head = np.array([r["ceil"]["q95"] - r["H"] for r in recs])
            out["by_hours_to_end"][f"{lo_}-{hi_}h"] = {
                "rows": int(sel.sum()),
                "truth_above_q95": float(cols["p1|q95|viol"][sel].mean()),
                "truth_above_q50": float(cols["p1|q50|viol"][sel].mean()),
                "p1_mass_above_q95": float(cols["p1|q95|mass"][sel].mean()),
                "p1_mass_above_q50": float(cols["p1|q50|mass"][sel].mean()),
                "median_headroom_q95_c": float(np.median(head[sel])),
                "dlog_q95": float(np.nanmean(cols["p1|q95|dlog"][sel])),
            }
    json.dump(out, open(HERE / "readings_applied.json", "w"), indent=1)
    for k, v in out["arms"].items():
        print(k, "sd", v["station_days"], "mass", round(v["mass_above_mean"], 4), "viol_row", round(v["truth_above_ceiling_rate_rows"], 4),
              "dLog", [round(x, 5) for x in v["dlog"]], "dCRPS", [round(x, 5) for x in v["dcrps"]], "dBrier", [round(x, 5) for x in v["dbrier"]])
    print(json.dumps(out["by_hours_to_end"], indent=1))


if __name__ == "__main__":
    main()
