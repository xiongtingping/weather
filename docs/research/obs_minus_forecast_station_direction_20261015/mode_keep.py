"""PROTOCOL_MODE_KEEP.md: the mode bucket keeps its probability, mass moves between the two sides by direction."""
from __future__ import annotations

import json
import math

import numpy as np

import analyze as A

KAPPAS = {"k1.5": math.log(1.5), "k2": math.log(2.0), "k3": math.log(3.0)}
GRID = [0.0, math.log(1.25), math.log(1.5), math.log(2.0), math.log(3.0)]


def mode_keep(p, mode_i, d, kappa):
    if not d or kappa == 0.0:
        return p
    above = p[mode_i + 1:].sum()
    below = p[:mode_i].sum()
    if above <= 0.0 or below <= 0.0:
        return p
    R = above + below
    if d > 0:
        a2 = R * above * math.exp(kappa) / (above * math.exp(kappa) + below)
    else:
        a2 = R * above / (above + below * math.exp(kappa))
    b2 = R - a2
    q = p.copy()
    q[mode_i + 1:] *= a2 / above
    q[:mode_i] *= b2 / below
    return q


def main():
    recs = A.load()
    n = len(recs)
    st = np.array([r["station"] for r in recs])
    days = np.array([r["day"] for r in recs])
    X = np.array([r["x"] for r in recs], float)
    T = np.array([r["T"] for r in recs])
    W = np.array([r["w"] for r in recs])
    mode_i = np.array([r["mk"]["0"]["mode_i"] for r in recs])
    mode_c = np.array([r["c"][r["mk"]["0"]["mode_i"]] for r in recs])
    mean0 = np.array([r["mk"]["0"]["mean"] for r in recs])
    tilt = mean0 - mode_c
    truth_side = np.array([np.sign(r["ti"] - r["mk"]["0"]["mode_i"]) for r in recs])
    sd_key = np.array([r["station"] + "|" + r["day"] for r in recs])
    sd_ids, sd_idx = np.unique(sd_key, return_inverse=True)
    day_ids, day_idx = np.unique(days, return_inverse=True)
    sd_day = np.zeros(len(sd_ids), int)
    sd_day[sd_idx] = day_idx
    nd = len(day_ids)
    draws = np.random.default_rng(7).multinomial(nd, np.full(nd, 1.0 / nd), size=2000)
    test_days = [d for d in day_ids if (day_ids < d).sum() >= A.MIN_TRAIN_DAYS]
    sd_count = {s: len(set(sd_key[st == s])) for s in set(st)}

    def direction(XX):
        em = np.full(n, np.nan)
        for d in test_days:
            tr = days < d
            te = np.where(days == d)[0]
            Z = A.standardise(XX, np.where(tr)[0])
            em[te] = A.shrunk_station_predict(Z, T - mode_c, st, days, tr, te)
        return np.where(np.isnan(em), np.nan, np.where(np.abs(em) < 0.25 * W, 0.0, np.sign(em)))

    tested = np.isin(days, test_days)
    arms = {
        "increments_plus_market": direction(np.hstack([X, tilt[:, None]])),
        "market_only": direction(tilt[:, None]),
        "oracle": np.where(tested, truth_side.astype(float), np.nan),
    }
    base = np.full((n, 3), np.nan)
    for i in np.where(tested)[0]:
        base[i] = A.score(recs[i]["p"], recs[i])
    out = {"rows_tested": int(tested.sum()), "arms": {}}
    for arm, dirn in arms.items():
        res = {}
        dd_by_k = {}
        for kname, kap in list(KAPPAS.items()) + [("k1.25", math.log(1.25))]:
            sc = np.full((n, 3), np.nan)
            for i in np.where(~np.isnan(dirn))[0]:
                sc[i] = A.score(mode_keep(recs[i]["p"], mode_i[i], dirn[i], kap), recs[i])
            dd_by_k[kap] = sc - base
        # walk-forward per-station kappa
        wf = np.full((n, 3), np.nan)
        for d in test_days:
            for s in set(st[days == d]):
                tr = (days < d) & (st == s) & ~np.isnan(dirn)
                trf = (days < d) & ~np.isnan(dirn)
                pool = tr if len(set(days[tr])) >= A.MIN_STATION_DAYS else trf
                best = 0.0
                if pool.any():
                    vals = {k: np.nanmean(dd_by_k[k][pool, 0]) for k in GRID[1:]}
                    kbest = min(vals, key=vals.get)
                    best = kbest if vals[kbest] < 0 else 0.0
                te = (days == d) & (st == s) & ~np.isnan(dirn)
                wf[te] = dd_by_k[best][te] if best else 0.0
        variants = {k: dd_by_k[v] for k, v in KAPPAS.items()}
        variants["walk_forward_station_kappa"] = wf
        for vname, dd in variants.items():
            fleet = {m: A.boot_sd_day(dd[:, k], sd_idx, sd_day, nd, draws) for k, m in enumerate(("dlog", "dcrps", "dbrier"))}
            per = {}
            for s in sorted(set(st)):
                b = A.boot_sd_day(dd[:, 0], sd_idx, sd_day, nd, draws, st == s)
                if b:
                    per[s] = b
            eligible = {s: v["p"] for s, v in per.items() if sd_count[s] >= 10}
            passed = A.bh(eligible)
            act = ~np.isnan(dirn) & (dirn != 0)
            parts = {
                "truth_on_mode": float(np.nanmean(dd[act & (truth_side == 0), 0])),
                "off_mode_right": float(np.nanmean(dd[act & (truth_side != 0) & (dirn == truth_side), 0])),
                "off_mode_wrong": float(np.nanmean(dd[act & (truth_side != 0) & (dirn != truth_side), 0])),
                "n_on": int((act & (truth_side == 0)).sum()), "n_right": int((act & (truth_side != 0) & (dirn == truth_side)).sum()),
                "n_wrong": int((act & (truth_side != 0) & (dirn != truth_side)).sum()),
            }
            res[vname] = {"fleet": fleet, "stations": {s: {**v, "bh_q10": s in passed, "station_days_total": sd_count[s]} for s, v in per.items()},
                          "better_bh": sorted(s for s in passed if per[s]["point"] < 0),
                          "worse_bh": sorted(s for s in passed if per[s]["point"] > 0), "parts": parts}
            f = fleet["dlog"]
            print(f"{arm:24s} {vname:28s} dLog {f['point']:+.4f} [{f['lo']:+.4f},{f['hi']:+.4f}] CRPS {fleet['dcrps']['point']:+.4f} Brier {fleet['dbrier']['point']:+.4f}"
                  f" | on {parts['truth_on_mode']:+.4f}(n{parts['n_on']}) right {parts['off_mode_right']:+.4f}(n{parts['n_right']}) wrong {parts['off_mode_wrong']:+.4f}(n{parts['n_wrong']})"
                  f" | BH+ {res[vname]['better_bh']} BH- {res[vname]['worse_bh']}")
        out["arms"][arm] = res
    json.dump(out, open("readings_mode_keep.json", "w"), indent=1, default=float)
    main_arm = out["arms"]["increments_plus_market"]["walk_forward_station_kappa"]["stations"]
    print("\nper station (main arm, walk-forward kappa):")
    for s, v in sorted(main_arm.items(), key=lambda kv: kv[1]["point"]):
        print(f"  {s} sd{v['station_days']:3d} {v['point']:+.4f} [{v['lo']:+.4f},{v['hi']:+.4f}] {'BH*' if v['bh_q10'] else ''}{' (<10sd)' if v['station_days_total'] < 10 else ''}")


if __name__ == "__main__":
    main()
