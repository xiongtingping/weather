"""Three tests of PROTOCOL.md on rows.jsonl.gz: market delay, per-station gain, direction strong likelihood."""
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

FEATS = ("dT", "dSW", "dCloud", "dRain")
MIN_TRAIN_DAYS = 10
MIN_STATION_DAYS = 8
SHRINK_N = 20.0
BOOT = 2000
KAPPAS = {"k1.5": math.log(1.5), "k2": math.log(2.0), "k3": math.log(3.0)}
KAPPA_GRID = [0.0, math.log(1.25), math.log(1.5), math.log(2.0), math.log(3.0)]
rng_global = np.random.default_rng(7)


def centres(intervals):
    order = sorted(intervals, key=lambda b: (intervals[b][0] is not None, intervals[b][0] if intervals[b][0] is not None else -1e9))
    widths = [u - l for l, u in (intervals[b] for b in order) if l is not None and u is not None]
    w = float(np.median(widths))
    c = []
    for b in order:
        l, u = intervals[b]
        c.append((l + u) / 2 if l is not None and u is not None else (u - w / 2 if l is None else l + w / 2))
    return order, np.array(c), w


def load():
    recs = []
    for line in gzip.open(HERE / "rows.jsonl.gz", "rt"):
        r = json.loads(line)
        order, c, w = centres(r["intervals"])
        p = np.array([float(r["p1"].get(b, 0.0)) for b in order])
        if p.sum() <= 0:
            continue
        p = p / p.sum()
        T = r["truth"]
        ti = next((i for i, b in enumerate(order) if (r["intervals"][b][0] is None or r["intervals"][b][0] <= T) and (r["intervals"][b][1] is None or T < r["intervals"][b][1])), None)
        if ti is None:
            continue
        mk = {}
        for h, m in r["market"].items():
            q = np.array([float(m["q"].get(b, np.nan)) for b in order])
            if np.isnan(q).any():
                continue
            q = q / q.sum()
            mode_i = order.index(m["mode"]) if m["mode"] in order else int(np.argmax(q))
            mk[h] = {"q": q, "mean": float((q * c).sum()), "mode_i": mode_i, "age": m.get("age_min")}
        if "0" not in mk:
            continue
        recs.append({**{k: r[k] for k in ("station", "day", "local_hour", "unit", "rad_official", "posterior_version")},
                     "order": order, "c": c, "w": w, "p": p, "ti": ti, "T": T,
                     "x": [r[f] if r[f] is not None else np.nan for f in FEATS], "mk": mk})
    return recs


def score(p, r):
    excluded = [b for b, v in zip(r["order"], p) if v <= 0.0]
    vec, _ = floor_probabilities(dict(zip(r["order"], p)), excluded)
    pv = np.array([vec[b] for b in r["order"]])
    ls = -math.log(max(pv[r["ti"]], 1e-300))
    brier = float(((pv - (np.arange(len(pv)) == r["ti"])) ** 2).sum())
    edges = (r["c"][:-1] + r["c"][1:]) / 2
    F = np.cumsum(pv)[:-1]
    crps = float((((F - (r["T"] <= edges)) ** 2) * r["w"]).sum())
    return np.array([ls, crps, brier])


def tilt_shift(p, c, e):
    mu = float((p * c).sum())
    var = float((p * (c - mu) ** 2).sum())
    if var <= 1e-9:
        return p
    z = np.where(p > 0, np.log(np.maximum(p, 1e-300)) + e * (c - mu) / var, -np.inf)
    z -= z.max()
    q = np.where(p > 0, np.exp(z), 0.0)
    return q / q.sum()


def tilt_direction(p, mode_i, d, kappa):
    side = np.sign(np.arange(len(p)) - mode_i)
    z = np.where(p > 0, np.log(np.maximum(p, 1e-300)) + kappa * d * side, -np.inf)
    z -= z.max()
    q = np.where(p > 0, np.exp(z), 0.0)
    return q / q.sum()


def standardise(X, idx):
    mu = np.nanmean(X[idx], 0)
    sd = np.nanstd(X[idx], 0) + 1e-9
    Z = (X - mu) / sd
    return np.where(np.isnan(Z), 0.0, Z)


def ridge(X, y, lam=1.0, prior=None):
    k = X.shape[1]
    prior = np.zeros(k) if prior is None else prior
    A = X.T @ X + lam * np.eye(k)
    return np.linalg.solve(A, X.T @ y + lam * prior)


def shrunk_station_predict(Z, y, station, days, train_mask, test_idx):
    """Fleet ridge + per-station ridge shrunk toward fleet by n/(n+20) station-days; intercept per station."""
    tr = np.where(train_mask)[0]
    Xf = np.hstack([np.ones((len(tr), 1)), Z[tr]])
    bf = ridge(Xf, y[tr])
    out = np.zeros(len(test_idx))
    for j, i in enumerate(test_idx):
        s = station[i]
        st_tr = tr[station[tr] == s]
        nsd = len(set(days[st_tr]))
        b = bf
        if nsd >= MIN_STATION_DAYS:
            Xs = np.hstack([np.ones((len(st_tr), 1)), Z[st_tr]])
            bs = ridge(Xs, y[st_tr], lam=1.0, prior=bf)
            wgt = nsd / (nsd + SHRINK_N)
            b = wgt * bs + (1 - wgt) * bf
        out[j] = b[0] + Z[i] @ b[1:]
    return out


def boot_sd_day(delta, sd_idx, sd_day, n_day, draws, sel=None):
    ok = ~np.isnan(delta) if sel is None else (sel & ~np.isnan(delta))
    if not ok.any():
        return None
    n_sd = sd_idx.max() + 1
    s = np.bincount(sd_idx[ok], weights=delta[ok], minlength=n_sd)
    cnt = np.bincount(sd_idx[ok], minlength=n_sd)
    has = cnt > 0
    m = np.where(has, s / np.maximum(cnt, 1), 0.0)
    ds = np.bincount(sd_day, weights=m * has, minlength=n_day)
    dc = np.bincount(sd_day, weights=has.astype(float), minlength=n_day)
    point = ds.sum() / dc.sum()
    bs = (draws @ ds) / np.maximum(draws @ dc, 1e-9)
    valid = (draws @ dc) > 0
    bs = bs[valid]
    lo, hi = np.quantile(bs, [0.025, 0.975])
    p_two = 2 * min((bs >= 0).mean(), (bs <= 0).mean())
    return {"point": float(point), "lo": float(lo), "hi": float(hi), "p": float(max(p_two, 1.0 / len(bs))), "station_days": int(has.sum())}


def bh(pvals, q=0.10):
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    passed = set()
    k_max = 0
    for k, (_, p) in enumerate(items, 1):
        if p <= q * k / m:
            k_max = k
    for k, (name, _) in enumerate(items, 1):
        if k <= k_max:
            passed.add(name)
    return passed


def main():
    recs = load()
    n = len(recs)
    station = np.array([r["station"] for r in recs])
    days = np.array([r["day"] for r in recs])
    X = np.array([r["x"] for r in recs], float)
    sd_key = np.array([r["station"] + "|" + r["day"] for r in recs])
    sd_ids, sd_idx = np.unique(sd_key, return_inverse=True)
    day_ids, day_idx = np.unique(days, return_inverse=True)
    sd_day = np.zeros(len(sd_ids), int)
    sd_day[sd_idx] = day_idx
    n_day = len(day_ids)
    draws = rng_global.multinomial(n_day, np.full(n_day, 1.0 / n_day), size=BOOT)
    print("rows", n, "station-days", len(sd_ids), "days", n_day, "stations", len(set(station)))
    print("feature availability", {f: round(float((~np.isnan(X[:, k])).mean()), 3) for k, f in enumerate(FEATS)})
    out = {"rows": n, "station_days": len(sd_ids), "days": n_day, "amendments": ["market snapshot max age 90 min (30 min left 61% of rows without one)"]}

    mean0 = np.array([r["mk"]["0"]["mean"] for r in recs])
    p1mean = np.array([(r["p"] * r["c"]).sum() for r in recs])
    T = np.array([r["T"] for r in recs])
    W = np.array([r["w"] for r in recs])
    mode_c = np.array([r["c"][r["mk"]["0"]["mode_i"]] for r in recs])
    mkt_tilt = mean0 - mode_c

    test_days = [d for d in day_ids if (day_ids < d).sum() >= MIN_TRAIN_DAYS]

    # ---------------- Test 1: market delay -------------------------------------
    t1 = {}
    pred = {}
    for h in ("0", "1", "3", "6"):
        y = np.array([T[i] - recs[i]["mk"][h]["mean"] if h in recs[i]["mk"] else np.nan for i in range(n)])
        yhat = np.full(n, np.nan)
        for d in test_days:
            train = (days < d) & ~np.isnan(y)
            test = np.where((days == d) & ~np.isnan(y))[0]
            if len(test) == 0:
                continue
            Z = standardise(X, np.where(train)[0])
            yhat[test] = shrunk_station_predict(Z, y, station, days, train, test)
            # baseline: station mean only
        base = np.full(n, np.nan)
        for d in test_days:
            train = (days < d) & ~np.isnan(y)
            for s in set(station[days == d]):
                m = train & (station == s)
                base[(days == d) & (station == s)] = y[m].mean() if m.any() else y[train].mean()
        pred[h] = (y, yhat, base)
        ok = ~np.isnan(y) & ~np.isnan(yhat)
        # skill: squared-error improvement over station-mean baseline, station-day equal weight
        dmse = (y - yhat) ** 2 - (y - base) ** 2
        corr = float(np.corrcoef((y - base)[ok], (yhat - base)[ok])[0, 1])
        t1[h] = {"rows": int(ok.sum()), "corr_oos": corr, "dMSE_vs_station_mean": boot_sd_day(np.where(ok, dmse, np.nan), sd_idx, sd_day, n_day, draws),
                 "sd_y": float(np.nanstd(y[ok]))}
        per = {}
        for s in sorted(set(station)):
            m = ok & (station == s)
            if m.sum() > 30:
                per[s] = round(float(np.corrcoef((y - base)[m], (yhat - base)[m])[0, 1]), 3)
        t1[h]["corr_by_station"] = per
    # does the market move toward the increment prediction over the next 3h?
    y0, yhat0, base0 = pred["0"]
    mv3 = np.array([recs[i]["mk"]["3"]["mean"] - mean0[i] if "3" in recs[i]["mk"] else np.nan for i in range(n)])
    ok = ~np.isnan(mv3) & ~np.isnan(yhat0)
    sig = (yhat0 - base0)[ok]
    slope = float((sig * mv3[ok]).sum() / (sig * sig).sum())
    bs = []
    sd_ok = sd_idx[ok]
    for _ in range(500):
        pick = rng_global.choice(len(sd_ids), len(sd_ids))
        cnt = np.bincount(pick, minlength=len(sd_ids))
        wts = cnt[sd_ok]
        bs.append((wts * sig * mv3[ok]).sum() / max((wts * sig * sig).sum(), 1e-9))
    t1["market_move_3h_on_signal"] = {"slope": slope, "lo": float(np.quantile(bs, 0.025)), "hi": float(np.quantile(bs, 0.975)), "rows": int(ok.sum())}
    out["test1_market_delay"] = t1
    print("TEST1", json.dumps({h: {k: v for k, v in t1[h].items() if k != "corr_by_station"} for h in ("0", "1", "3", "6")}, indent=0))
    print("market move 3h", t1["market_move_3h_on_signal"])

    # ---------------- Test 2: per-station shift of P1 -------------------------
    e = T - p1mean
    ehat = np.full(n, np.nan)
    for d in test_days:
        train = days < d
        test = np.where(days == d)[0]
        Z = standardise(X, np.where(train)[0])
        ehat[test] = shrunk_station_predict(Z, e, station, days, train, test)
    base_s = np.full((n, 3), np.nan)
    shift_s = np.full((n, 3), np.nan)
    for i in np.where(~np.isnan(ehat))[0]:
        r = recs[i]
        base_s[i] = score(r["p"], r)
        shift_s[i] = score(tilt_shift(r["p"], r["c"], ehat[i]), r)
    d2 = shift_s - base_s
    t2 = {"fleet": {m: boot_sd_day(d2[:, k], sd_idx, sd_day, n_day, draws) for k, m in enumerate(("dlog", "dcrps", "dbrier"))}, "stations": {}}
    pvals = {}
    halves = {}
    for s in sorted(set(station)):
        sel = station == s
        res = {m: boot_sd_day(d2[:, k], sd_idx, sd_day, n_day, draws, sel) for k, m in enumerate(("dlog", "dcrps", "dbrier"))}
        if res["dlog"] is None:
            continue
        okk = sel & ~np.isnan(ehat)
        hit = float((np.sign(ehat[okk]) == np.sign(e[okk])).mean())
        sdays = sorted(set(days[okk]))
        mid = sdays[len(sdays) // 2] if sdays else None
        h1 = np.nanmean(d2[okk & (days < mid), 0]) if mid else np.nan
        h2 = np.nanmean(d2[okk & (days >= mid), 0]) if mid else np.nan
        res.update({"hit_rate": hit, "first_half_dlog": float(h1), "second_half_dlog": float(h2),
                    "unit": recs[np.where(sel)[0][0]]["unit"], "rad_official": recs[np.where(sel)[0][0]]["rad_official"]})
        t2["stations"][s] = res
        pvals[s] = res["dlog"]["p"]
    passed = bh(pvals)
    for s in t2["stations"]:
        t2["stations"][s]["bh_q10"] = s in passed
    out["test2_per_station_shift"] = t2
    print("TEST2 fleet", t2["fleet"]["dlog"])
    for s, v in sorted(t2["stations"].items(), key=lambda kv: kv[1]["dlog"]["point"]):
        print(f"  {s} {v['unit']} sd{v['dlog']['station_days']:3d} dLog {v['dlog']['point']:+.4f} [{v['dlog']['lo']:+.4f},{v['dlog']['hi']:+.4f}] p{v['dlog']['p']:.3f} BH{'*' if v['bh_q10'] else ' '} hit {v['hit_rate']:.2f} halves {v['first_half_dlog']:+.4f}/{v['second_half_dlog']:+.4f}")

    # ---------------- Test 3: direction strong likelihood ---------------------
    target_m = T - mode_c
    Xm = np.hstack([X, mkt_tilt[:, None]])
    arms = {"increments_plus_market": Xm, "market_only": mkt_tilt[:, None]}
    t3 = {}
    for arm, XX in arms.items():
        em = np.full(n, np.nan)
        for d in test_days:
            train = days < d
            test = np.where(days == d)[0]
            Z = standardise(XX, np.where(train)[0])
            em[test] = shrunk_station_predict(Z, target_m, station, days, train, test)
        dirn = np.where(np.isnan(em), np.nan, np.where(np.abs(em) < 0.25 * W, 0.0, np.sign(em)))
        truth_side = np.array([np.sign(r["ti"] - r["mk"]["0"]["mode_i"]) for r in recs])
        p1_side = np.array([np.sign(r["p"][r["mk"]["0"]["mode_i"] + 1:].sum() - r["p"][:r["mk"]["0"]["mode_i"]].sum()) for r in recs])
        mk_side = np.array([np.sign(r["mk"]["0"]["q"][r["mk"]["0"]["mode_i"] + 1:].sum() - r["mk"]["0"]["q"][:r["mk"]["0"]["mode_i"]].sum()) for r in recs])
        live = (~np.isnan(dirn)) & (dirn != 0) & (truth_side != 0)
        res = {"rows_with_direction": int(((~np.isnan(dirn)) & (dirn != 0)).sum()), "rows_scored_off_mode": int(live.sum()),
               "hit_signal": float((dirn[live] == truth_side[live]).mean()),
               "hit_market_tilt_same_rows": float((mk_side[live] == truth_side[live]).mean()),
               "hit_p1_tilt_same_rows": float((p1_side[live] == truth_side[live]).mean()),
               "signal_agrees_market_tilt": float((dirn[live] == mk_side[live]).mean())}
        dl = {}
        base = np.full((n, 3), np.nan)
        for i in np.where(~np.isnan(dirn))[0]:
            base[i] = score(recs[i]["p"], recs[i])
        for kname, kap in KAPPAS.items():
            sc = np.full((n, 3), np.nan)
            for i in np.where(~np.isnan(dirn))[0]:
                r = recs[i]
                sc[i] = score(tilt_direction(r["p"], r["mk"]["0"]["mode_i"], dirn[i], kap), r)
            dd = sc - base
            dl[kname] = {"fleet": {m: boot_sd_day(dd[:, k], sd_idx, sd_day, n_day, draws) for k, m in enumerate(("dlog", "dcrps", "dbrier"))}}
            per = {}
            pv = {}
            for s in sorted(set(station)):
                b = boot_sd_day(dd[:, 0], sd_idx, sd_day, n_day, draws, station == s)
                if b:
                    per[s] = b
                    pv[s] = b["p"]
            ps = bh(pv)
            dl[kname]["stations"] = {s: {**v, "bh_q10": s in ps} for s, v in per.items()}
            dl[kname]["stations_better_bh"] = sorted(s for s in ps if per[s]["point"] < 0)
            dl[kname]["stations_worse_bh"] = sorted(s for s in ps if per[s]["point"] > 0)
            dl[kname]["_dd"] = dd
        # walk-forward per-station kappa
        wf = np.full((n, 3), np.nan)
        for d in test_days:
            for s in set(station[days == d]):
                tr = (days < d) & (station == s) & ~np.isnan(dirn)
                best_k, best_v = 0.0, 0.0
                if len(set(days[tr])) >= MIN_STATION_DAYS:
                    for kap in KAPPA_GRID[1:]:
                        kn = [kk for kk, vv in KAPPAS.items() if abs(vv - kap) < 1e-12]
                        if kn:
                            v = np.nanmean(dl[kn[0]]["_dd"][tr, 0])
                        else:
                            v = np.nanmean([score(tilt_direction(recs[i]["p"], recs[i]["mk"]["0"]["mode_i"], dirn[i], kap), recs[i])[0] - base[i, 0] for i in np.where(tr)[0]])
                        if v < best_v:
                            best_k, best_v = kap, v
                te = np.where((days == d) & (station == s) & ~np.isnan(dirn))[0]
                for i in te:
                    wf[i] = score(tilt_direction(recs[i]["p"], recs[i]["mk"]["0"]["mode_i"], dirn[i], best_k), recs[i]) - base[i]
        dl["walk_forward_station_kappa"] = {"fleet": {m: boot_sd_day(wf[:, k], sd_idx, sd_day, n_day, draws) for k, m in enumerate(("dlog", "dcrps", "dbrier"))}}
        per = {s: boot_sd_day(wf[:, 0], sd_idx, sd_day, n_day, draws, station == s) for s in sorted(set(station))}
        per = {s: v for s, v in per.items() if v}
        ps = bh({s: v["p"] for s, v in per.items()})
        dl["walk_forward_station_kappa"]["stations"] = {s: {**v, "bh_q10": s in ps} for s, v in per.items()}
        dl["walk_forward_station_kappa"]["stations_better_bh"] = sorted(s for s in ps if per[s]["point"] < 0)
        dl["walk_forward_station_kappa"]["stations_worse_bh"] = sorted(s for s in ps if per[s]["point"] > 0)
        for kname in KAPPAS:
            dl[kname].pop("_dd")
        res["likelihood"] = dl
        t3[arm] = res
        print("TEST3", arm, {k: v for k, v in res.items() if k != "likelihood"})
        for kname, v in dl.items():
            f = v["fleet"]["dlog"]
            print(f"   {kname}: fleet dLog {f['point']:+.4f} [{f['lo']:+.4f},{f['hi']:+.4f}]  better(BH) {v['stations_better_bh']}  worse(BH) {v['stations_worse_bh']}")
    out["test3_direction"] = t3
    json.dump(out, open(HERE / "readings.json", "w"), indent=1, default=float)


if __name__ == "__main__":
    main()
