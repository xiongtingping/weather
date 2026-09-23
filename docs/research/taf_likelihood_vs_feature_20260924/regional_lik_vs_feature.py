#!/usr/bin/env python3
"""Regional model as LIKELIHOOD vs as FEATURE (centre shift), strictly as-of.

Sample: docs/research/station_regional_features_20260922 (6 stations, clean P0
envelopes 08-01..09-21, >=12 h to settlement, no bias_refused), primary
(station, model) pairs as pre-registered there.  Every parameter used on a row
at decision t is fitted on this station's own earlier cells whose truth was
knowable by t (day end + 24 h), never on the row itself.

Arms, all applied to the served P0 vector:
  base          served
  shift_roll    delta = clip(k d, +-1.5), k = clip(sum((T-c)d)/sum d^2, 0, 1)   [charter 201 form]
  shift_rs      antithetic random-sign control of shift_roll
  lik_deb       served * N(x=d ; a + b y_k, tau), y_k = centre_k - c, line fitted x on y  [TAF form on the debiased feature]
  lik_raw       same with x = R - c (raw; the line's a absorbs the bias)
  lik_lam       lik_deb ^ lambda, lambda in {0.25,0.5,0.75,1} chosen as-of on the training cells' RPS
  lik_pool      TAF production form: pooled line over the six stations, station line shrunk to it with K=15 (station-days)
  lik_naive     served * N(R_deb ; T=centre_k, tau_res), tau_res = sd(T - R_deb) as-of   (the forecast as a direct noisy thermometer)
  lik_mirror    antithetic control of lik_deb: x' = a + s (x - a)   (same |deviation|, random direction)
Scores: RPS on the ladder (primary), p on the true bucket (NLL with 1e-4 floor, NLL_nz on rows every arm >= floor),
abs centre error, mode hit, posterior sd on the axis, claimed mode probability, Murphy REL/RES over (envelope,bucket).
Cells = (day, tier) means; intervals = event-day cluster bootstrap within the station; across stations = equal weight.
"""
import argparse, collections, hashlib, json, math, os, pickle, sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import numpy as np
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.center_width_calibration import served_bucket_axis, apply_center_width_calibration
from bayes_basket_target.delta_v2_settlement_label import _winning_bucket

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--lag-extra", type=float, default=0.0)
ap.add_argument("--min-train-days", type=int, default=10)
ap.add_argument("--out", default=os.path.join(HERE, "results_lik_vs_feature.json"))
a = ap.parse_args()
rng = np.random.default_rng(20260923)

D = pickle.load(open(os.path.join(HERE, "envelopes.pkl"), "rb"))
ENVS, TRUTH = D["envs"], D["truth"]
RUNS = json.load(open(os.path.join(HERE, "runs.json")))["rows"]
TZ = {"SBGR": "America/Sao_Paulo", "ZUUU": "Asia/Shanghai", "KSEA": "America/Los_Angeles",
      "FACT": "Africa/Johannesburg", "KSFO": "America/Los_Angeles", "LTAC": "Europe/Istanbul"}
PRIMARY = {"ZUUU": "cma_grapes_global", "LTAC": "icon_eu", "KSEA": "hrrr+nbm", "KSFO": "hrrr+nbm",
           "SBGR": "ecmwf_ifs", "FACT": "ecmwf_ifs"}
CAP, FLOOR, MIN_TAU, SHRINK_K = 1.5, 1e-4, 0.25, 15.0
LAMBDAS = (0.25, 0.5, 0.75, 1.0)
ARMS = ("base", "shift_roll", "rsA", "rsB", "lik_deb", "lik_raw", "lik_lam", "lik_pool", "lik_naive", "mirA", "mirB", "lik_rec", "lik_rec_lam")


def ts(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))
def tier(h): return "12-24" if h < 24 else "24-36" if h < 36 else "36-48" if h < 48 else "48+"
def day_end_utc(st, d):
    dd = date.fromisoformat(d) + timedelta(days=1)
    return datetime(dd.year, dd.month, dd.day, tzinfo=ZoneInfo(TZ[st])).astimezone(timezone.utc)
def lbin(x): return int(math.floor(x / 12.0))
def sgn(st, d): return 1.0 if int(hashlib.md5(f"{st}|{d}".encode()).hexdigest()[:8], 16) % 2 == 0 else -1.0

R = collections.defaultdict(list)
for r in RUNS:
    R[(r["model"], r["st"], r["date"])].append((ts(r["knowable"]) + timedelta(hours=a.lag_extra), r["tmax"], r["lead_to_day_start_h"], r["run"]))
for k in R: R[k].sort()
TK = {st: {d: day_end_utc(st, d) + timedelta(hours=24) for d in TRUTH[st]} for st in TRUTH}


def latest_run(model, st, d, t):
    best = None
    for kn, tm, lead, run in R.get((model, st, d), ()):
        if kn <= t: best = (tm, lead, run, kn)
    return best


def debias(model, st, t, lead):
    days = sorted((d for d, k in TK[st].items() if k <= t), reverse=True)[:45]
    if not days: return 0.0, 0
    per_bin = collections.defaultdict(list); allb = collections.defaultdict(list)
    for d in days:
        for kn, tm, ld, run in R.get((model, st, d), ()):
            if kn > t: continue
            e = TRUTH[st][d] - tm
            per_bin[(d, lbin(ld))].append(e); allb[d].append(e)
    b = lbin(lead)
    same = [np.mean(v) for (d, bb), v in per_bin.items() if bb == b]
    if len(same) >= 7: return float(np.mean(same)), len(same)
    pooled = [np.mean(v) for v in allb.values()]
    return (float(np.mean(pooled)), len(pooled)) if pooled else (0.0, 0)


def feature(model, st, d, t):
    parts = ["ncep_hrrr_conus", "ncep_nbm_conus"] if model == "hrrr+nbm" else [model]
    deb, raw = [], []
    for m in parts:
        lr = latest_run(m, st, d, t)
        if lr is None: return None
        b, n = debias(m, st, t, lr[1])
        deb.append(lr[0] + b); raw.append(lr[0])
    return float(np.mean(deb)), float(np.mean(raw))


def rps(p, order, tb):
    k = order.index(tb); cp = 0.0; s = 0.0
    for i in range(len(order) - 1):
        cp += p[order[i]]; s += (cp - (1.0 if i >= k else 0.0)) ** 2
    return s
def mean_on(p, order, cen): return sum(p[k] * c for k, c in zip(order, cen))
def sd_on(p, order, cen):
    m = mean_on(p, order, cen); return math.sqrt(max(0.0, sum(p[k] * (c - m) ** 2 for k, c in zip(order, cen))))
def shifted(p, order, cen, edges, delta):
    if abs(delta) < 1e-12: return dict(p)
    return apply_center_width_calibration(p, delta_c=delta, width_lambda=1.0, ordered_keys=order, key_centers_c=cen, open_edge_bounds_c=edges)[0]
def lik(p, order, cen, c, x, a_, b_, tau, lam=1.0):
    ex = [((x - a_ - b_ * (ck - c)) ** 2) / (2.0 * tau * tau) for ck in cen]
    m = min(ex)
    q = {k: p[k] * math.exp(-lam * (e - m)) for k, e in zip(order, ex)}
    s = sum(q.values())
    return {k: v / s for k, v in q.items()} if s > 0 else dict(p)
def line(xs, ys):
    xs, ys = np.asarray(xs, float), np.asarray(ys, float)
    my, mx = ys.mean(), xs.mean(); vy = ((ys - my) ** 2).sum()
    b_ = float(((xs - mx) * (ys - my)).sum() / vy) if vy > 0 else 0.0
    a_ = float(mx - b_ * my)
    resid = ((xs - a_ - b_ * ys) ** 2).sum()
    return a_, b_, float(math.sqrt(max(resid / max(len(xs) - 2, 1), MIN_TAU ** 2)))


# ---------------------------------------------------------------- pass 1: rows
E = []
for e in ENVS:
    if e["refused"] or e["h"] < 12: continue
    st = e["st"]; t = ts(e["dec"]); order = e["order"]
    f = feature(PRIMARY[st], st, e["day"], t)
    if f is None: continue
    cen, edges = served_bucket_axis(e["intervals"], order)
    c = mean_on(e["served"], order, cen)
    E.append(dict(st=st, day=e["day"], t=t, tier=tier(e["h"]), c=c, d=f[0] - c, xr=f[1] - c, Rdeb=f[0], T=e["tmax"],
                  tb=_winning_bucket(official_tmax_c=e["tmax"], bucket_intervals=e["intervals"]), order=order, cen=cen,
                  edges=edges, served=e["served"], mv=e["mv"]))
print("rows with feature", len(E), collections.Counter(x["st"] for x in E), flush=True)

# cells per station for the line / k fits: (day, tier) -> mean c, d, xr, T
CELLS = {}
for st in PRIMARY:
    g = collections.defaultdict(list)
    for x in E:
        if x["st"] == st: g[(x["day"], x["tier"])].append(x)
    CELLS[st] = [(d, tr, float(np.mean([v["c"] for v in vs])), float(np.mean([v["d"] for v in vs])),
                  float(np.mean([v["xr"] for v in vs])), vs[0]["T"]) for (d, tr), vs in g.items()]
ROWS_BY_ST = {st: sorted([x for x in E if x["st"] == st], key=lambda x: x["t"]) for st in PRIMARY}


def avail_days(st, t): return tuple(sorted(d for d, k in TK[st].items() if k <= t))


POOL_CACHE = {}
def pool_line_at(t):
    key = tuple((st, avail_days(st, t)[-1] if avail_days(st, t) else None) for st in PRIMARY)
    if key in POOL_CACHE: return POOL_CACHE[key]
    xs, ys, per_st = [], [], {}
    for st in PRIMARY:
        av = set(avail_days(st, t))
        cs = [(c, d, T) for (dd, tr, c, d, xr, T) in CELLS[st] if dd in av]
        if cs:
            per_st[st] = (line([v[1] for v in cs], [v[2] - v[0] for v in cs]), len({dd for (dd, tr, c, d, xr, T) in CELLS[st] if dd in av}))
        xs += [v[1] for v in cs]; ys += [v[2] - v[0] for v in cs]
    pool = line(xs, ys) if len(xs) >= 5 else None
    POOL_CACHE[key] = (pool, per_st)
    return POOL_CACHE[key]


STATS = {}
PAIRS = {}
for st in PRIMARY:
    ES = ROWS_BY_ST[st]; cells = CELLS[st]
    cache = {}

    def params_at(t):
        av = avail_days(st, t); key = av[-1] if av else None
        if key in cache: return cache[key]
        avs = set(av)
        tr = [(c, d, xr, T) for (dd, tier_, c, d, xr, T) in cells if dd in avs]
        ndays = len({dd for (dd, tier_, c, d, xr, T) in cells if dd in avs})
        out = dict(n=ndays, k=0.0, deb=None, raw=None, lam=1.0, pool=None, tau_naive=None)
        if ndays >= a.min_train_days:
            den = sum(d * d for c, d, xr, T in tr); num = sum((T - c) * d for c, d, xr, T in tr)
            out["k"] = min(1.0, max(0.0, num / den)) if den > 0 else 0.0
            out["deb"] = line([d for c, d, xr, T in tr], [T - c for c, d, xr, T in tr])
            out["raw"] = line([xr for c, d, xr, T in tr], [T - c for c, d, xr, T in tr])
            res = [T - (c + d) for c, d, xr, T in tr]
            out["tau_naive"] = max(MIN_TAU, float(np.sqrt(np.mean(np.square(res)))))
            # lambda chosen on the training envelopes' cell-mean RPS (line in-sample on the training set)
            trrows = [x for x in ES if x["day"] in avs]
            a_, b_, tau = out["deb"]
            best = None
            for lam in LAMBDAS:
                g = collections.defaultdict(list)
                for x in trrows:
                    g[(x["day"], x["tier"])].append(rps(lik(x["served"], x["order"], x["cen"], x["c"], x["d"], a_, b_, tau, lam), x["order"], x["tb"]))
                m = float(np.mean([np.mean(v) for v in g.values()]))
                if best is None or m < best[0] - 1e-12: best = (m, lam)
            out["lam"] = best[1]
            pool, per_st = pool_line_at(t)
            if pool is not None and st in per_st:
                (sa, sb, stau), sn = per_st[st]
                w = sn / (sn + SHRINK_K)
                out["pool"] = (pool[0] + (sa - pool[0]) * w, pool[1] + (sb - pool[1]) * w, pool[2] + (stau - pool[2]) * w)
        cache[key] = out
        return out

    rows = []
    for x in ES:
        P = params_at(x["t"])
        if P["n"] < a.min_train_days: continue
        s = sgn(st, x["day"]); o, cen, ed, c, d = x["order"], x["cen"], x["edges"], x["c"], x["d"]
        dl = max(-CAP, min(CAP, P["k"] * d))
        a_, b_, tau = P["deb"]
        ld = lik(x["served"], o, cen, c, d, a_, b_, tau); ll = lik(x["served"], o, cen, c, d, a_, b_, tau, P["lam"])
        rec = max(-CAP, min(CAP, mean_on(ld, o, cen) - c)); recl = max(-CAP, min(CAP, mean_on(ll, o, cen) - c))
        vec = {"base": dict(x["served"]), "shift_roll": shifted(x["served"], o, cen, ed, dl),
               "rsA": shifted(x["served"], o, cen, ed, s * dl), "rsB": shifted(x["served"], o, cen, ed, -s * dl),
               "lik_deb": ld,
               "lik_raw": lik(x["served"], o, cen, c, x["xr"], *P["raw"]),
               "lik_lam": ll,
               "lik_rec": shifted(x["served"], o, cen, ed, rec), "lik_rec_lam": shifted(x["served"], o, cen, ed, recl),
               "lik_pool": lik(x["served"], o, cen, c, d, *P["pool"]) if P["pool"] else dict(x["served"]),
               "lik_naive": lik(x["served"], o, cen, c, d, 0.0, 1.0, P["tau_naive"]),
               "mirA": lik(x["served"], o, cen, c, a_ + s * (d - a_), a_, b_, tau),
               "mirB": lik(x["served"], o, cen, c, a_ - s * (d - a_), a_, b_, tau)}
        sc = {}
        for arm, p in vec.items():
            sc[arm] = dict(rps=rps(p, o, x["tb"]), pt=p[x["tb"]], ae=abs(mean_on(p, o, cen) - x["T"]),
                           hit=1.0 if max(p, key=p.get) == x["tb"] else 0.0, sd=sd_on(p, o, cen), pmode=max(p.values()),
                           vec=p)
        mk = None
        if x["mv"] is not None:
            mk = dict(rps=rps(x["mv"], o, x["tb"]), pt=x["mv"][x["tb"]], ae=abs(mean_on(x["mv"], o, cen) - x["T"]))
        rows.append(dict(day=x["day"], tier=x["tier"], k=P["k"], lam=P["lam"], line=P["deb"], pool=P["pool"], d=d, T=x["T"], c=c, sc=sc, mk=mk,
                         order=o, tb=x["tb"]))

    def cellavg(sub, fn):
        g = collections.defaultdict(list)
        for r in sub:
            v = fn(r)
            if v is not None: g[(r["day"], r["tier"])].append(v)
        return [(k_[0], float(np.mean(v))) for k_, v in g.items()]

    def boot(pairs, n=4000):
        if not pairs: return None
        g = collections.defaultdict(list)
        for d_, v in pairs: g[d_].append(v)
        G = [np.array(v) for v in g.values()]
        sums = np.array([z.sum() for z in G]); cnt = np.array([len(z) for z in G])
        idx = rng.integers(0, len(G), (n, len(G)))
        b = sums[idx].sum(1) / cnt[idx].sum(1)
        return [round(float(np.mean([v for _, v in pairs])), 5), round(float(np.percentile(b, 2.5)), 5), round(float(np.percentile(b, 97.5)), 5), round(float(np.mean(b > 0)), 4)]

    def nll(pt): return -math.log(max(FLOOR, pt))

    def murphy(sub, arm, nb=10):
        ps, os_ = [], []
        for r in sub:
            p = r["sc"][arm]["vec"]
            for k_ in r["order"]:
                ps.append(p[k_]); os_.append(1.0 if k_ == r["tb"] else 0.0)
        ps, os_ = np.array(ps), np.array(os_)
        bins = np.minimum((ps * nb).astype(int), nb - 1); obar = os_.mean(); rel = res = 0.0
        for i in range(nb):
            m = bins == i
            if m.any():
                rel += m.sum() * (ps[m].mean() - os_[m].mean()) ** 2; res += m.sum() * (os_[m].mean() - obar) ** 2
        return dict(REL=round(float(rel / len(ps)), 5), RES=round(float(res / len(ps)), 5), BS=round(float(((ps - os_) ** 2).mean()), 5))

    def block(sub, arms=("shift_roll", "lik_deb", "lik_raw", "lik_lam", "lik_pool", "lik_naive", "lik_rec", "lik_rec_lam")):
        if not sub: return None
        o = dict(n_env=len(sub), n_days=len({r["day"] for r in sub}), n_cells=len({(r["day"], r["tier"]) for r in sub}))
        o["rps"] = {arm: round(float(np.mean([v for _, v in cellavg(sub, lambda r, arm=arm: r["sc"][arm]["rps"])])), 5) for arm in ("base",) + arms}
        o["sd"] = {arm: round(float(np.mean([r["sc"][arm]["sd"] for r in sub])), 3) for arm in ("base",) + arms}
        o["pmode"] = {arm: round(float(np.mean([r["sc"][arm]["pmode"] for r in sub])), 3) for arm in ("base",) + arms}
        o["hit"] = {arm: round(float(np.mean([r["sc"][arm]["hit"] for r in sub])), 3) for arm in ("base",) + arms}
        o["mae"] = {arm: round(float(np.mean([v for _, v in cellavg(sub, lambda r, arm=arm: r["sc"][arm]["ae"])])), 3) for arm in ("base",) + arms}
        nz = lambda r: all(r["sc"][a_]["pt"] >= FLOOR for a_ in ARMS)
        for arm in arms:
            o[f"d_rps {arm}"] = boot(cellavg(sub, lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["base"]["rps"]))
            o[f"d_nll_nz {arm}"] = boot(cellavg(sub, lambda r, arm=arm: (nll(r["sc"][arm]["pt"]) - nll(r["sc"]["base"]["pt"])) if nz(r) else None))
            o[f"d_hit {arm}"] = boot(cellavg(sub, lambda r, arm=arm: r["sc"][arm]["hit"] - r["sc"]["base"]["hit"]))
        o["d_rps shift_roll - rsign"] = boot(cellavg(sub, lambda r: r["sc"]["shift_roll"]["rps"] - 0.5 * (r["sc"]["rsA"]["rps"] + r["sc"]["rsB"]["rps"])))
        o["d_rps lik_deb - mirror"] = boot(cellavg(sub, lambda r: r["sc"]["lik_deb"]["rps"] - 0.5 * (r["sc"]["mirA"]["rps"] + r["sc"]["mirB"]["rps"])))
        for arm in ("lik_deb", "lik_lam", "lik_pool", "lik_raw", "lik_naive", "lik_rec", "lik_rec_lam"):
            o[f"h2h d_rps {arm} - shift_roll"] = boot(cellavg(sub, lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["shift_roll"]["rps"]))
            o[f"h2h d_nll_nz {arm} - shift_roll"] = boot(cellavg(sub, lambda r, arm=arm: (nll(r["sc"][arm]["pt"]) - nll(r["sc"]["shift_roll"]["pt"])) if nz(r) else None))
        o["murphy"] = {arm: murphy(sub, arm) for arm in ("base", "shift_roll", "lik_deb", "lik_lam", "lik_pool", "lik_naive", "lik_rec", "lik_rec_lam")}
        ms = [r for r in sub if r["mk"] is not None]
        if ms:
            cm = lambda fn: float(np.mean([v for _, v in cellavg(ms, fn)]))
            o["market"] = dict(n=len(ms), rps_market=round(cm(lambda r: r["mk"]["rps"]), 5), rps_base=round(cm(lambda r: r["sc"]["base"]["rps"]), 5),
                               rps_shift=round(cm(lambda r: r["sc"]["shift_roll"]["rps"]), 5), rps_lik=round(cm(lambda r: r["sc"]["lik_deb"]["rps"]), 5),
                               rps_lik_lam=round(cm(lambda r: r["sc"]["lik_lam"]["rps"]), 5))
        o["k_median"] = round(float(np.median([r["k"] for r in sub])), 3)
        o["lam_counter"] = dict(collections.Counter(r["lam"] for r in sub))
        o["line_last"] = [round(v, 3) for v in sub[-1]["line"]]
        o["pool_last"] = [round(v, 3) for v in sub[-1]["pool"]] if sub[-1]["pool"] else None
        return o

    res = {"all": block(rows)}
    for tr in ("12-24", "24-36", "36-48", "48+"):
        res["tier " + tr] = block([r for r in rows if r["tier"] == tr])
    STATS[st] = {"model": PRIMARY[st], "res": res}
    for arm in ("shift_roll", "lik_deb", "lik_raw", "lik_lam", "lik_pool", "lik_naive", "lik_rec", "lik_rec_lam"):
        PAIRS[(st, arm)] = cellavg(rows, lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["base"]["rps"])
        PAIRS[(st, arm + "|h2h")] = cellavg(rows, lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["shift_roll"]["rps"])
        PAIRS[(st, arm + "|hit")] = cellavg(rows, lambda r, arm=arm: r["sc"][arm]["hit"] - r["sc"]["base"]["hit"])
    PAIRS[(st, "shift_roll|ctrl")] = cellavg(rows, lambda r: r["sc"]["shift_roll"]["rps"] - 0.5 * (r["sc"]["rsA"]["rps"] + r["sc"]["rsB"]["rps"]))
    PAIRS[(st, "lik_deb|ctrl")] = cellavg(rows, lambda r: r["sc"]["lik_deb"]["rps"] - 0.5 * (r["sc"]["mirA"]["rps"] + r["sc"]["mirB"]["rps"]))
    b = res["all"]
    print(f"\n{st} {PRIMARY[st]} days={b['n_days']} cells={b['n_cells']} k_med={b['k_median']} line(a,b,tau)={b['line_last']} pool={b['pool_last']} lam={b['lam_counter']}")
    print(f"  RPS base {b['rps']['base']:.4f} | " + " ".join(f"{arm} {b['rps'][arm]:.4f}" for arm in ("shift_roll", "lik_deb", "lik_lam", "lik_pool", "lik_raw", "lik_naive")))
    for arm in ("shift_roll", "lik_deb", "lik_lam", "lik_pool", "lik_raw", "lik_naive", "lik_rec", "lik_rec_lam"):
        d_ = b[f"d_rps {arm}"]; n_ = b[f"d_nll_nz {arm}"]; h_ = b[f"d_hit {arm}"]
        print(f"  {arm:10s} dRPS {d_[0]:+.4f} [{d_[1]:+.4f},{d_[2]:+.4f}] P>0 {d_[3]:.2f} | dNLLnz {n_[0]:+.4f} [{n_[1]:+.4f},{n_[2]:+.4f}] | dHit {h_[0]:+.3f} | sd {b['sd']['base']:.2f}->{b['sd'][arm]:.2f} pmode {b['pmode']['base']:.3f}->{b['pmode'][arm]:.3f} hit {b['hit']['base']:.3f}->{b['hit'][arm]:.3f}")
    print(f"  ctrl: shift-rsign {b['d_rps shift_roll - rsign'][:3]}  lik-mirror {b['d_rps lik_deb - mirror'][:3]}")
    for arm in ("lik_deb", "lik_lam", "lik_pool", "lik_rec", "lik_rec_lam"):
        print(f"  h2h {arm}-shift_roll dRPS {b[f'h2h d_rps {arm} - shift_roll'][:3]} dNLLnz {b[f'h2h d_nll_nz {arm} - shift_roll'][:3]}")
    print("  murphy:", {arm: (v["REL"], v["RES"]) for arm, v in b["murphy"].items()})
    if "market" in b: print("  market:", b["market"])
    for tr in ("12-24", "24-36", "36-48", "48+"):
        bt = res["tier " + tr]
        if bt: print(f"  tier {tr:5s} n_days={bt['n_days']:2d} dRPS shift {bt['d_rps shift_roll'][0]:+.4f} lik {bt['d_rps lik_deb'][0]:+.4f} lik_rec {bt['d_rps lik_rec'][0]:+.4f} | h2h lik-shift {bt['h2h d_rps lik_deb - shift_roll'][:3]} lik_rec-shift {bt['h2h d_rps lik_rec - shift_roll'][:3]}")


def combined(keys, n=4000):
    means, draws = [], []
    for k_ in keys:
        pr = PAIRS.get(k_)
        if not pr: return None
        g = collections.defaultdict(list)
        for d_, v in pr: g[d_].append(v)
        G = [np.array(v) for v in g.values()]
        sums = np.array([z.sum() for z in G]); cnt = np.array([len(z) for z in G])
        idx = rng.integers(0, len(G), (n, len(G)))
        draws.append(sums[idx].sum(1) / cnt[idx].sum(1)); means.append(float(np.mean([v for _, v in pr])))
    b = np.mean(draws, 0)
    return [round(float(np.mean(means)), 5), round(float(np.percentile(b, 2.5)), 5), round(float(np.percentile(b, 97.5)), 5), round(float(np.mean(b > 0)), 4)]


REG4 = ["ZUUU", "LTAC", "KSEA", "KSFO"]; ALL6 = REG4 + ["SBGR", "FACT"]
ACROSS = {}
for name, sts in (("regional_4", REG4), ("all_6", ALL6)):
    for arm in ("shift_roll", "lik_deb", "lik_raw", "lik_lam", "lik_pool", "lik_naive", "lik_rec", "lik_rec_lam"):
        ACROSS[f"{name} dRPS {arm}"] = combined([(s_, arm) for s_ in sts])
        ACROSS[f"{name} dHit {arm}"] = combined([(s_, arm + "|hit") for s_ in sts])
        if arm != "shift_roll": ACROSS[f"{name} h2h {arm} - shift_roll"] = combined([(s_, arm + "|h2h") for s_ in sts])
    ACROSS[f"{name} shift_roll - rsign"] = combined([(s_, "shift_roll|ctrl") for s_ in sts])
    ACROSS[f"{name} lik_deb - mirror"] = combined([(s_, "lik_deb|ctrl") for s_ in sts])
print("\n=== across stations (equal station weight, day bootstrap within station) ===")
for k_, v in ACROSS.items(): print(f"  {k_:40s} {v}")
json.dump(dict(config=vars(a), stations={st: {"model": v["model"], "res": v["res"]} for st, v in STATS.items()}, across=ACROSS), open(a.out, "w"), indent=1, default=str)
print("wrote", a.out)
