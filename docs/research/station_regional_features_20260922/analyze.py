"""Station regional features, step 4: a station-level P0 centre correction
from a regional / high-resolution point forecast, measured strictly as-of.

For each clean P0 envelope (no bias_refused system; hours to settlement >= 12)
at station s, event day d, decision t:

  c   = served mean on the served bucket axis (served_bucket_axis centres)
  R   = local-day max of the newest run of model M with knowable_at <= t that
        holds the whole local day d (runs.json)
  b_R = the model's own as-of debias: mean(T_e - R_e) over the station's
        runs in the same lead bin (hours from init to local-day start, 12 h
        bins) for event days e whose truth was knowable by t (e's local-day
        end + 24 h <= t), last 45 such days; all bins pooled if < 7 days
  d   = (R + b_R) - c                        regional minus served centre
  arms:
    fixed   delta = 0.5 d                    pre-registered, nothing fitted
    roll    delta = k d, k = clip(sum((T-c)d)/sum(d^2), 0, 1) over this
            station's own earlier clean cells (truth knowable by t); k = 0
            (no correction) until >= 10 training event days
    rsign   delta = -/+ k d, antithetic pair (sign per station-day), the
            random-sign control: same |shift|, no direction
  delta is capped at +/-1.5 C (production MAX_ABS_SHIFT_C) and applied to the
  served vector with production's apply_center_width_calibration
  (width_lambda 1, open edges declared).

Scores per envelope, averaged inside (station, day, tier) cells, then over
cells; intervals are cluster bootstraps over EVENT DAYS within the station
(never pooled across stations).  RPS on the ladder; NLL on the true bucket
with a 1e-4 floor, and "NLL_nz" on rows where every arm put >= 1e-4 on the
true bucket (zero-bucket smoothing artifact guard).  Market: production's
market_reference_v1 vector at the decision (<= 30 min), same cells only.

usage: analyze.py [--lag-extra H] [--out results.json]
"""
import argparse, bisect, collections, hashlib, json, math, os, pickle, sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import numpy as np
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.center_width_calibration import served_bucket_axis, apply_center_width_calibration
from bayes_basket_target.delta_v2_settlement_label import _winning_bucket

HERE = os.path.dirname(os.path.abspath(__file__))
SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/srf/"
ap = argparse.ArgumentParser()
ap.add_argument("--lag-extra", type=float, default=0.0)
ap.add_argument("--out", default=os.path.join(HERE, "results.json"))
ap.add_argument("--min-train-days", type=int, default=10)
a = ap.parse_args()
rng = np.random.default_rng(20260922)

D = pickle.load(open(SP + "envelopes.pkl", "rb"))
ENVS, TRUTH = D["envs"], D["truth"]
RUNS = json.load(open(os.path.join(HERE, "runs.json")))["rows"]
TZ = {"SBGR": "America/Sao_Paulo", "ZUUU": "Asia/Shanghai", "KSEA": "America/Los_Angeles",
      "FACT": "Africa/Johannesburg", "KSFO": "America/Los_Angeles", "LTAC": "Europe/Istanbul"}
# primary pair per station (pre-registered before any score was read), then the secondaries
FEATURES = {
    "ZUUU": ["cma_grapes_global", "ecmwf_ifs"],
    "LTAC": ["icon_eu", "ecmwf_ifs"],
    "KSEA": ["hrrr+nbm", "ncep_hrrr_conus", "ncep_nbm_conus", "ecmwf_ifs"],
    "KSFO": ["hrrr+nbm", "ncep_hrrr_conus", "ncep_nbm_conus", "ecmwf_ifs", "ecmwf_ifs025"],
    "SBGR": ["ecmwf_ifs", "ukmo_global_deterministic_10km", "ecmwf_aifs025_single", "ecmwf_aifs025_single_6h"],
    "FACT": ["ecmwf_ifs", "ukmo_global_deterministic_10km", "ecmwf_aifs025_single", "ecmwf_aifs025_single_6h"],
}
CAP = 1.5
FLOOR = 1e-4


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def tier(h):
    return "12-24" if h < 24 else "24-36" if h < 36 else "36-48" if h < 48 else "48+"


def day_end_utc(st, d):
    dd = date.fromisoformat(d) + timedelta(days=1)
    return datetime(dd.year, dd.month, dd.day, tzinfo=ZoneInfo(TZ[st])).astimezone(timezone.utc)


def lbin(x):
    return int(math.floor(x / 12.0))


# ---- regional runs indexed by (model, station, date): sorted by knowable
R = collections.defaultdict(list)
for r in RUNS:
    kn = ts(r["knowable"]) + timedelta(hours=a.lag_extra)
    R[(r["model"], r["st"], r["date"])].append((kn, r["tmax"], r["lead_to_day_start_h"], r["run"]))
for k in R:
    R[k].sort()
# truth knowable time per (st, day)
TK = {st: {d: day_end_utc(st, d) + timedelta(hours=24) for d in TRUTH[st]} for st in TRUTH}


def latest_run(model, st, d, t):
    """newest run of model that holds local day d and is knowable at t"""
    v = R.get((model, st, d))
    if not v:
        return None
    best = None
    for kn, tm, lead, run in v:
        if kn <= t:
            best = (tm, lead, run, kn)
    return best


def debias(model, st, t, lead):
    """as-of mean(T - R) over the station's runs in the same lead bin, last 45 knowable truth days"""
    days = sorted((d for d, k in TK[st].items() if k <= t), reverse=True)[:45]
    if not days:
        return 0.0, 0
    per_bin = collections.defaultdict(list); allb = collections.defaultdict(list)
    for d in days:
        for kn, tm, ld, run in R.get((model, st, d), ()):
            if kn > t:
                continue
            e = TRUTH[st][d] - tm
            per_bin[(d, lbin(ld))].append(e); allb[d].append(e)
    b = lbin(lead)
    same = [np.mean(v) for (d, bb), v in per_bin.items() if bb == b]
    if len(same) >= 7:
        return float(np.mean(same)), len(same)
    pooled = [np.mean(v) for v in allb.values()]
    return (float(np.mean(pooled)), len(pooled)) if pooled else (0.0, 0)


def feature(model, st, d, t):
    """debiased regional forecast (and its components); None if no knowable run"""
    parts = ["ncep_hrrr_conus", "ncep_nbm_conus"] if model == "hrrr+nbm" else [model]
    vals, meta = [], []
    for m in parts:
        lr = latest_run(m, st, d, t)
        if lr is None:
            return None
        b, n = debias(m, st, t, lr[1])
        vals.append(lr[0] + b); meta.append(dict(model=m, raw=lr[0], b=b, nb=n, lead=lr[1], run=lr[2],
                                                 age_h=(t - ts(lr[2])).total_seconds() / 3600))
    return float(np.mean(vals)), meta


def rps(p, order, tb):
    k = order.index(tb); cp = 0.0; s = 0.0
    for i in range(len(order) - 1):
        cp += p[order[i]]; s += (cp - (1.0 if i >= k else 0.0)) ** 2
    return s


def mean_on(p, order, cen):
    return sum(p[k] * c for k, c in zip(order, cen))


def shifted(p, order, cen, edges, delta):
    if abs(delta) < 1e-12:
        return dict(p)
    return apply_center_width_calibration(p, delta_c=delta, width_lambda=1.0, ordered_keys=order,
                                          key_centers_c=cen, open_edge_bounds_c=edges)[0]


def sgn(st, d):
    return 1.0 if int(hashlib.md5(f"{st}|{d}".encode()).hexdigest()[:8], 16) % 2 == 0 else -1.0


# ---------------------------------------------------------------- pass 1: features per envelope
E = []
for e in ENVS:
    if e["refused"] or e["h"] < 12:
        continue
    t = ts(e["dec"])
    order = e["order"]
    cen, edges = served_bucket_axis(e["intervals"], order)
    c = mean_on(e["served"], order, cen)
    tb = _winning_bucket(official_tmax_c=e["tmax"], bucket_intervals=e["intervals"])
    feats = {}
    for m in FEATURES[e["st"]]:
        f = feature(m, e["st"], e["day"], t)
        if f is not None:
            feats[m] = f
    E.append(dict(st=e["st"], day=e["day"], t=t, tier=tier(e["h"]), c=c, T=e["tmax"], tb=tb, order=order, cen=cen,
                  edges=edges, served=e["served"], mv=e["mv"], feats=feats, era=("A:08-01..09-06" if e["day"] <= "2026-09-06"
                  else "B:09-07..09-13" if e["day"] <= "2026-09-13" else "C:09-14..09-21")))
print("clean >=12h envelopes", len(E))

# ---------------------------------------------------------------- pass 2: rolling k and scoring
OUT = {"config": dict(lag_extra_h=a.lag_extra, min_train_days=a.min_train_days, cap_c=CAP, nll_floor=FLOOR), "stations": {}}
PAIRS = {}   # (st, model, arm) -> [(event_day, cell delta RPS)]  for the across-station summary
for st in FEATURES:
    ES = sorted([x for x in E if x["st"] == st], key=lambda x: x["t"])
    for m in FEATURES[st]:
        # training cells: (day, tier) -> mean c, mean d, T   (cell means over envelopes that carried the feature)
        cell = collections.defaultdict(list)
        for x in ES:
            if m in x["feats"]:
                cell[(x["day"], x["tier"])].append((x["c"], x["feats"][m][0] - x["c"], x["T"]))
        cells = [(d, np.mean([v[0] for v in vs]), np.mean([v[1] for v in vs]), vs[0][2]) for (d, tr), vs in cell.items()]
        kcache = {}

        def k_at(t):
            avail = tuple(sorted(d for d, k in TK[st].items() if k <= t))
            key = avail[-1] if avail else None
            if key in kcache:
                return kcache[key]
            av = set(avail)
            tr = [(c0, dd, T) for (d, c0, dd, T) in cells if d in av]
            ndays = len({d for (d, c0, dd, T) in cells if d in av})
            if ndays < a.min_train_days:
                kcache[key] = (0.0, ndays, None)
            else:
                num = sum((T - c0) * dd for c0, dd, T in tr); den = sum(dd * dd for c0, dd, T in tr)
                kraw = num / den if den > 0 else 0.0
                kcache[key] = (min(1.0, max(0.0, kraw)), ndays, kraw)
            return kcache[key]

        rows = []
        for x in ES:
            if m not in x["feats"]:
                continue
            dval, meta = x["feats"][m]
            dd = dval - x["c"]
            k, ntr, kraw = k_at(x["t"])
            s = sgn(st, x["day"])
            arms = {"base": 0.0, "fixed": 0.5 * dd, "roll": k * dd, "rsA": s * k * dd, "rsB": -s * k * dd,
                    "full": dd}
            sc = {}
            for arm, delta in arms.items():
                dcap = max(-CAP, min(CAP, delta))
                p = shifted(x["served"], x["order"], x["cen"], x["edges"], dcap)
                sc[arm] = dict(rps=rps(p, x["order"], x["tb"]), pt=p[x["tb"]], ae=abs(mean_on(p, x["order"], x["cen"]) - x["T"]),
                               clipped=abs(delta) > CAP)
            mk = None
            if x["mv"] is not None:
                mk = dict(rps=rps(x["mv"], x["order"], x["tb"]), pt=x["mv"][x["tb"]], ae=abs(mean_on(x["mv"], x["order"], x["cen"]) - x["T"]))
            rows.append(dict(day=x["day"], tier=x["tier"], era=x["era"], k=k, kraw=kraw, ntr=ntr, d=dd, T=x["T"], c=x["c"],
                             sc=sc, mk=mk, evaluable=ntr >= a.min_train_days, age=meta[0]["age_h"], lead=meta[0]["lead"]))

        def cellavg(sub, fn):
            g = collections.defaultdict(list)
            for r in sub:
                v = fn(r)
                if v is not None:
                    g[(r["day"], r["tier"])].append(v)
            return [(k_[0], float(np.mean(v))) for k_, v in g.items()]

        def boot(pairs, n=4000):
            if not pairs:
                return None
            g = collections.defaultdict(list)
            for d, v in pairs:
                g[d].append(v)
            G = [np.array(v) for v in g.values()]
            sums = np.array([z.sum() for z in G]); cnt = np.array([len(z) for z in G])
            idx = rng.integers(0, len(G), (n, len(G)))
            b = sums[idx].sum(1) / cnt[idx].sum(1)
            return [float(np.mean([v for _, v in pairs])), float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5)),
                    float(np.mean(b > 0))]

        def nll(pt):
            return -math.log(max(FLOOR, pt))

        def block(sub):
            if not sub:
                return None
            o = dict(n_envelopes=len(sub), n_event_days=len({r["day"] for r in sub}),
                     n_cells=len({(r["day"], r["tier"]) for r in sub}))
            ca = lambda fn: [v for _, v in cellavg(sub, fn)]
            o["mae"] = {arm: float(np.mean(ca(lambda r, arm=arm: r["sc"][arm]["ae"]))) for arm in ("base", "fixed", "roll", "full")}
            o["mae"]["rsign"] = float(np.mean(ca(lambda r: 0.5 * (r["sc"]["rsA"]["ae"] + r["sc"]["rsB"]["ae"]))))
            o["rps"] = {arm: float(np.mean(ca(lambda r, arm=arm: r["sc"][arm]["rps"]))) for arm in ("base", "fixed", "roll", "full")}
            o["d_mae_roll"] = boot(cellavg(sub, lambda r: r["sc"]["roll"]["ae"] - r["sc"]["base"]["ae"]))
            o["d_mae_fixed"] = boot(cellavg(sub, lambda r: r["sc"]["fixed"]["ae"] - r["sc"]["base"]["ae"]))
            o["d_rps_roll"] = boot(cellavg(sub, lambda r: r["sc"]["roll"]["rps"] - r["sc"]["base"]["rps"]))
            o["d_rps_fixed"] = boot(cellavg(sub, lambda r: r["sc"]["fixed"]["rps"] - r["sc"]["base"]["rps"]))
            o["d_rps_roll_minus_rsign"] = boot(cellavg(sub, lambda r: r["sc"]["roll"]["rps"] - 0.5 * (r["sc"]["rsA"]["rps"] + r["sc"]["rsB"]["rps"])))
            o["d_rps_rsign"] = boot(cellavg(sub, lambda r: 0.5 * (r["sc"]["rsA"]["rps"] + r["sc"]["rsB"]["rps"]) - r["sc"]["base"]["rps"]))
            o["d_nll_roll"] = boot(cellavg(sub, lambda r: nll(r["sc"]["roll"]["pt"]) - nll(r["sc"]["base"]["pt"])))
            nz = lambda r: all(r["sc"][a_]["pt"] >= FLOOR for a_ in ("base", "roll", "rsA", "rsB"))
            o["d_nll_nz_roll"] = boot(cellavg(sub, lambda r: (nll(r["sc"]["roll"]["pt"]) - nll(r["sc"]["base"]["pt"])) if nz(r) else None))
            o["d_nll_nz_roll_minus_rsign"] = boot(cellavg(sub, lambda r: (nll(r["sc"]["roll"]["pt"]) - 0.5 * (nll(r["sc"]["rsA"]["pt"]) + nll(r["sc"]["rsB"]["pt"]))) if nz(r) else None))
            # leave-best-days-out: drop the N event days that gained most, recompute (does the gain rest on a few days?)
            pairs = cellavg(sub, lambda r: r["sc"]["roll"]["rps"] - r["sc"]["base"]["rps"])
            dm = collections.defaultdict(list)
            for d_, v in pairs:
                dm[d_].append(v)
            ranked = sorted(dm, key=lambda d_: np.mean(dm[d_]))
            for nd in (1, 2, 3):
                drop = set(ranked[:nd])
                o[f"d_rps_roll_drop_best{nd}"] = boot([(d_, v) for d_, v in pairs if d_ not in drop])
            o["days_helped_hurt"] = [int(sum(np.mean(v) < -1e-9 for v in dm.values())), int(sum(np.mean(v) > 1e-9 for v in dm.values()))]
            o["share_clipped_roll"] = float(np.mean([r["sc"]["roll"]["clipped"] for r in sub]))
            o["mean_abs_shift_roll"] = float(np.mean([min(CAP, abs(r["k"] * r["d"])) for r in sub]))
            o["k_last"] = sub[-1]["k"]; o["kraw_last"] = sub[-1]["kraw"]
            o["k_median"] = float(np.median([r["k"] for r in sub]))
            # the market, on the market-paired envelopes of the same cells
            ms = [r for r in sub if r["mk"] is not None]
            if ms:
                cm = lambda fn: [v for _, v in cellavg(ms, fn)]
                o["market"] = dict(n_envelopes=len(ms), n_event_days=len({r["day"] for r in ms}),
                                   mae_market=float(np.mean(cm(lambda r: r["mk"]["ae"]))),
                                   mae_base=float(np.mean(cm(lambda r: r["sc"]["base"]["ae"]))),
                                   mae_roll=float(np.mean(cm(lambda r: r["sc"]["roll"]["ae"]))),
                                   mae_fixed=float(np.mean(cm(lambda r: r["sc"]["fixed"]["ae"]))),
                                   rps_market=float(np.mean(cm(lambda r: r["mk"]["rps"]))),
                                   rps_base=float(np.mean(cm(lambda r: r["sc"]["base"]["rps"]))),
                                   rps_roll=float(np.mean(cm(lambda r: r["sc"]["roll"]["rps"]))),
                                   rps_fixed=float(np.mean(cm(lambda r: r["sc"]["fixed"]["rps"]))))
                o["market"]["gap_mae_base"] = o["market"]["mae_base"] - o["market"]["mae_market"]
                o["market"]["gap_mae_roll"] = o["market"]["mae_roll"] - o["market"]["mae_market"]
                o["market"]["gap_rps_base"] = o["market"]["rps_base"] - o["market"]["rps_market"]
                o["market"]["gap_rps_roll"] = o["market"]["rps_roll"] - o["market"]["rps_market"]
                o["market"]["gap_rps_fixed"] = o["market"]["rps_fixed"] - o["market"]["rps_market"]
                o["market"]["d_gap_rps_roll"] = boot(cellavg(ms, lambda r: r["sc"]["roll"]["rps"] - r["sc"]["base"]["rps"]))
            return o

        ev = [r for r in rows if r["evaluable"]]
        # per event day (evaluable, market-paired where available): the day's served centre, correction, truth, market mean
        byday = collections.defaultdict(list)
        for r in ev:
            byday[r["day"]].append(r)
        day_rows = []
        for d_, rs in sorted(byday.items()):
            mm = [r["mk"]["ae"] for r in rs if r["mk"] is not None]
            day_rows.append(dict(day=d_, n=len(rs), T=rs[0]["T"], c=float(np.mean([r["c"] for r in rs])),
                                 R_debiased=float(np.mean([r["c"] + r["d"] for r in rs])), k=float(np.mean([r["k"] for r in rs])),
                                 c_corrected=float(np.mean([r["c"] + max(-CAP, min(CAP, r["k"] * r["d"])) for r in rs])),
                                 ae_base=float(np.mean([r["sc"]["base"]["ae"] for r in rs])), ae_roll=float(np.mean([r["sc"]["roll"]["ae"] for r in rs])),
                                 ae_market=float(np.mean(mm)) if mm else None,
                                 d_rps=float(np.mean([r["sc"]["roll"]["rps"] - r["sc"]["base"]["rps"] for r in rs]))))
        res = {"coverage": dict(clean_envelopes=len(ES), with_feature=len(rows), evaluable=len(ev),
                                median_run_age_h=float(np.median([r["age"] for r in rows])) if rows else None),
               "evaluable_all": block(ev),
               "fixed_all_with_feature": block(rows), "by_day": day_rows}
        for era in ("A:08-01..09-06", "B:09-07..09-13", "C:09-14..09-21"):
            res["era " + era] = block([r for r in ev if r["era"] == era])
        for tr in ("12-24", "24-36", "36-48", "48+"):
            res["tier " + tr] = block([r for r in ev if r["tier"] == tr])
        # description only (in-sample, all rows): least-squares k and the corr of (T-c) with d
        if rows:
            cc = collections.defaultdict(list)
            for r in rows:
                cc[(r["day"], r["tier"])].append((r["T"] - r["c"], r["d"]))
            X = np.array([np.mean([v[1] for v in vs]) for vs in cc.values()]); Y = np.array([np.mean([v[0] for v in vs]) for vs in cc.values()])
            res["in_sample_description"] = dict(k_ls=float((X * Y).sum() / (X * X).sum()), corr=float(np.corrcoef(X, Y)[0, 1]),
                                                mean_T_minus_c=float(Y.mean()), sd_T_minus_c=float(Y.std()), sd_d=float(X.std()))
        OUT["stations"].setdefault(st, {})[m] = res
        for arm in ("roll", "fixed"):
            PAIRS[(st, m, arm)] = cellavg(ev, lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["base"]["rps"])
        # information check: corr of the served centre's error (T - c) with d at cell level, day-bootstrap CI
        cc = collections.defaultdict(list)
        for r in ev:
            cc[(r["day"], r["tier"])].append((r["T"] - r["c"], r["d"]))
        if len(cc) > 5:
            keys_ = list(cc); days_ = sorted({k_[0] for k_ in keys_})
            Y = np.array([np.mean([v[0] for v in cc[k_]]) for k_ in keys_]); X = np.array([np.mean([v[1] for v in cc[k_]]) for k_ in keys_])
            di = {d_: [i for i, k_ in enumerate(keys_) if k_[0] == d_] for d_ in days_}
            bs = []
            for _ in range(2000):
                ii = [i for d_ in rng.choice(days_, len(days_)) for i in di[d_]]
                if np.std(X[ii]) > 0 and np.std(Y[ii]) > 0:
                    bs.append(np.corrcoef(X[ii], Y[ii])[0, 1])
            res["evaluable_corr_err_vs_d"] = [float(np.corrcoef(X, Y)[0, 1]), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]
        b = res["evaluable_all"]
        if b:
            mk = b.get("market") or {}
            print(f"{st} {m:32s} ev_days={b['n_event_days']:2d} cells={b['n_cells']:3d} k_med={b['k_median']:.2f} "
                  f"MAE base {b['mae']['base']:.2f} roll {b['mae']['roll']:.2f} fixed {b['mae']['fixed']:.2f} | "
                  f"dRPS roll {b['d_rps_roll'][0]:+.4f} [{b['d_rps_roll'][1]:+.4f},{b['d_rps_roll'][2]:+.4f}] "
                  f"fixed {b['d_rps_fixed'][0]:+.4f} [{b['d_rps_fixed'][1]:+.4f},{b['d_rps_fixed'][2]:+.4f}] "
                  f"roll-rsign {b['d_rps_roll_minus_rsign'][0]:+.4f} [{b['d_rps_roll_minus_rsign'][1]:+.4f},{b['d_rps_roll_minus_rsign'][2]:+.4f}] | "
                  f"mkt MAE {mk.get('mae_market', float('nan')):.2f} gapMAE {mk.get('gap_mae_base', float('nan')):+.2f}->{mk.get('gap_mae_roll', float('nan')):+.2f} "
                  f"gapRPS {mk.get('gap_rps_base', float('nan')):+.4f}->{mk.get('gap_rps_roll', float('nan')):+.4f}", flush=True)
        else:
            print(st, m, "no evaluable rows", res["coverage"])
# across stations: each station's mean over its own event days, averaged with equal station weight;
# bootstrap resamples event days WITHIN each station independently (no pooling of days across stations)
def combined(keys, n=4000):
    means, draws = [], []
    for k_ in keys:
        pr = PAIRS.get(k_)
        if not pr:
            return None
        g = collections.defaultdict(list)
        for d_, v in pr:
            g[d_].append(v)
        G = [np.array(v) for v in g.values()]
        sums = np.array([z.sum() for z in G]); cnt = np.array([len(z) for z in G])
        idx = rng.integers(0, len(G), (n, len(G)))
        draws.append(sums[idx].sum(1) / cnt[idx].sum(1)); means.append(float(np.mean([v for _, v in pr])))
    b = np.mean(draws, 0)
    return [float(np.mean(means)), float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5)), float(np.mean(b > 0))]


PRIMARY = [("ZUUU", "cma_grapes_global"), ("LTAC", "icon_eu"), ("KSEA", "hrrr+nbm"), ("KSFO", "hrrr+nbm")]
OUT["across_stations"] = {
    "primary_regional_4_roll": combined([(s_, m_, "roll") for s_, m_ in PRIMARY]),
    "primary_regional_4_fixed": combined([(s_, m_, "fixed") for s_, m_ in PRIMARY]),
    "primary_all_6_roll": combined([(s_, m_, "roll") for s_, m_ in PRIMARY + [("SBGR", "ecmwf_ifs"), ("FACT", "ecmwf_ifs")]]),
    "posthoc_hrrr_only_us_roll": combined([("ZUUU", "cma_grapes_global", "roll"), ("LTAC", "icon_eu", "roll"),
                                           ("KSEA", "ncep_hrrr_conus", "roll"), ("KSFO", "ncep_hrrr_conus", "roll")]),
}
print("across stations:", json.dumps(OUT["across_stations"]))
json.dump(OUT, open(a.out, "w"), indent=1)
print("wrote", a.out)
