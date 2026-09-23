#!/usr/bin/env python3
"""TAF TX as LIKELIHOOD (production P0-M form) vs as FEATURE (charter-201 centre shift), strictly as-of.

Sample: clean P0 envelopes (no bias_refused system, >=12 h to settlement) at the 17 TX stations, 08-01..09-21.
Row = one P0 envelope; the TAF is the newest capture with a TX for that event day whose provider-available
time (+ --lag-extra h) is <= the envelope's decision time.  Every parameter used on a row at decision t is
fitted on earlier cells whose truth was knowable by t (local day end + 24 h), never on the row itself.

Arms (all applied to the served P0 vector):
  base           served
  shift_roll     delta = clip(k d, +-1.5), d = TX_deb - c, k = clip(sum((T-c)d)/sum d^2, 0, 1)         [charter 201 form]
  shift_raw      same on raw x = TX - c (no debias)
  shift_ab       delta = clip(alpha + k x), (alpha,k) OLS of T-c on x with intercept (the line's intercept absorbs bias)
  rsA/rsB        antithetic random-sign controls of shift_roll
  lik_deb        served * N(d ; a + b y_k, tau), y_k = centre_k - c, station line fitted x on y
  lik_raw        same with raw x                                                                       [charter 150 station form]
  lik_lam        lik_deb ^ lambda, lambda as-of on training RPS
  lik_pool       raw-x line pooled over the 17 stations, station line shrunk to it with K=15          [charter 150 production form]
  lik_pool_temp  lik_pool with charter-158 tempering: lambda per disagreement group (cloud/precip), fitted as-of on
                 pooled training rows, min 12 station-days per group, grid 0..1.5                     [production form today]
  lik_naive      served * N(TX_deb ; centre_k, tau_res)   (the TAF as a direct noisy thermometer)
  mirA/mirB      antithetic controls of lik_deb (same |deviation|, random direction)
  lik_rec        the likelihood's centre only: shift by (mean(lik_deb) - c), width restored
  lik_rec_lam    same from lik_lam
Scores: RPS on the ladder (primary), NLL on the true bucket (1e-4 floor; _nz rows), mode hit, posterior sd, claimed mode
probability, Murphy REL/RES.  Cells = (day, tier) means; intervals = event-day cluster bootstrap within station;
across stations = equal weight.
"""
import argparse, collections, hashlib, json, math, os, pickle, sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import numpy as np
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.center_width_calibration import served_bucket_axis, apply_center_width_calibration
from bayes_basket_target.delta_v2_settlement_label import _winning_bucket
from bayes_basket_target.taf_likelihood import disagreement_group

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--lag-extra", type=float, default=0.0)
ap.add_argument("--min-train-days", type=int, default=10)
ap.add_argument("--max-taf-age-h", type=float, default=1e9)
ap.add_argument("--out", default=os.path.join(HERE, "results_taf.json"))
a = ap.parse_args()
rng = np.random.default_rng(20260923)

D = pickle.load(open(os.path.join(HERE, "envelopes.pkl"), "rb"))
ENVS, TRUTH, TAFS, TZ = D["envs"], D["truth"], D["tafs"], D["tz"]
STATIONS = sorted(TZ)
CAP, FLOOR, MIN_TAU, SHRINK_K = 1.5, 1e-4, 0.25, 15.0
LAMBDAS = (0.25, 0.5, 0.75, 1.0)
TGRID = (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5)
GROUPS = ("REASONED", "UNREASONED_COLD", "OTHER", "SMALL")
ARMS = ("base", "shift_roll", "shift_raw", "shift_ab", "shift_temp", "rsA", "rsB", "lik_deb", "lik_raw", "lik_lam", "lik_pool", "lik_pool_temp",
        "lik_naive", "mirA", "mirB", "lik_rec", "lik_rec_lam", "lik_rec_temp", "lik_prod_lam", "lik_low_lam", "lik_g025", "lik_g050", "lik_g075")
REPORT = ("shift_roll", "shift_raw", "shift_ab", "shift_temp", "lik_deb", "lik_raw", "lik_lam", "lik_pool", "lik_pool_temp", "lik_naive", "lik_rec", "lik_rec_lam", "lik_rec_temp",
          "lik_prod_lam", "lik_low_lam", "lik_g025", "lik_g050", "lik_g075")
# fixed tables: the production artifact's (fitted 2026-09-22) and the as-of endpoint of this walk-forward (in-sample for the CHOICE of table)
PROD_LAMBDA = {"REASONED": 1.0, "UNREASONED_COLD": 0.5, "OTHER": 1.0, "SMALL": 1.0}
LOW_LAMBDA = {"REASONED": 0.5, "UNREASONED_COLD": 0.25, "OTHER": 0.5, "SMALL": 0.75}
FIXED = {"lik_prod_lam": PROD_LAMBDA, "lik_low_lam": LOW_LAMBDA, "lik_g025": {g: 0.25 for g in GROUPS}, "lik_g050": {g: 0.5 for g in GROUPS}, "lik_g075": {g: 0.75 for g in GROUPS}}


def ts(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))
def tier(h): return "12-24" if h < 24 else "24-36" if h < 36 else "36-48" if h < 48 else "48+"
def day_start_utc(st, d):
    dd = date.fromisoformat(d)
    return datetime(dd.year, dd.month, dd.day, tzinfo=ZoneInfo(TZ[st])).astimezone(timezone.utc)
def day_end_utc(st, d): return day_start_utc(st, d) + timedelta(days=1)
def lbin(x): return int(math.floor(x / 12.0))
def sgn(st, d): return 1.0 if int(hashlib.md5(f"{st}|{d}".encode()).hexdigest()[:8], 16) % 2 == 0 else -1.0

R = collections.defaultdict(list)
for r in TAFS:
    lead = (day_start_utc(r["st"], r["day"]) - ts(r["issued"])).total_seconds() / 3600.0
    R[(r["st"], r["day"])].append((ts(r["avail"]) + timedelta(hours=a.lag_extra), r["tx"], lead, r["kind"], r["cloud"], r["precip"]))
for k in R: R[k].sort(key=lambda z: z[0])
TK = {st: {d: day_end_utc(st, d) + timedelta(hours=24) for d in TRUTH[st]} for st in STATIONS}


def latest_taf(st, d, t):
    best = None
    for kn, tx, lead, kind, cloud, precip in R.get((st, d), ()):
        if kn <= t and (t - kn).total_seconds() <= a.max_taf_age_h * 3600: best = (tx, lead, kind, cloud, precip, kn)
    return best


def debias(st, t, lead):
    days = sorted((d for d, k in TK[st].items() if k <= t), reverse=True)[:45]
    if not days: return 0.0, 0
    per_bin = collections.defaultdict(list); allb = collections.defaultdict(list)
    for d in days:
        for kn, tx, ld, kind, cloud, precip in R.get((st, d), ()):
            if kn > t: continue
            e = TRUTH[st][d] - tx
            per_bin[(d, lbin(ld))].append(e); allb[d].append(e)
    b = lbin(lead)
    same = [np.mean(v) for (d, bb), v in per_bin.items() if bb == b]
    if len(same) >= 7: return float(np.mean(same)), len(same)
    pooled = [np.mean(v) for v in allb.values()]
    return (float(np.mean(pooled)), len(pooled)) if pooled else (0.0, 0)


def feature(st, d, t):
    lt = latest_taf(st, d, t)
    if lt is None: return None
    b, n = debias(st, t, lt[1])
    return lt[0] + b, lt[0], lt[2], lt[3], lt[4], lt[1], (t - lt[5]).total_seconds() / 3600.0


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
    if lam == 0.0: return dict(p)
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
def ols(xs, ys):
    xs, ys = np.asarray(xs, float), np.asarray(ys, float)
    mx, my = xs.mean(), ys.mean(); vx = ((xs - mx) ** 2).sum()
    k_ = float(((xs - mx) * (ys - my)).sum() / vx) if vx > 0 else 0.0
    return float(my - k_ * mx), k_


# ---------------------------------------------------------------- pass 1: rows
E = []; drop = collections.Counter()
for e in ENVS:
    if e["refused"] or e["h"] < 12 or not e["intervals"]: drop["refused/short/no-axis"] += 1; continue
    st = e["st"]; t = ts(e["dec"]); order = e["order"]
    f = feature(st, e["day"], t)
    if f is None: drop["no taf as-of"] += 1; continue
    cen, edges = served_bucket_axis(e["intervals"], order)
    c = mean_on(e["served"], order, cen)
    E.append(dict(st=st, day=e["day"], t=t, tier=tier(e["h"]), c=c, d=f[0] - c, xr=f[1] - c, T=e["tmax"], kind=f[2], cloud=f[3], precip=f[4],
                  taf_lead=f[5], taf_age=f[6],
                  grp=disagreement_group(f[1] - c, f[3], f[4]),
                  tb=_winning_bucket(official_tmax_c=e["tmax"], bucket_intervals=e["intervals"]), order=order, cen=cen, edges=edges, served=e["served"]))
print("rows with TAF", len(E), "dropped", dict(drop), flush=True)
print("per station", dict(collections.Counter(x["st"] for x in E)), flush=True)
print("groups", dict(collections.Counter(x["grp"] for x in E)), "tiers", dict(collections.Counter(x["tier"] for x in E)), flush=True)

CELLS = {}
for st in STATIONS:
    g = collections.defaultdict(list)
    for x in E:
        if x["st"] == st: g[(x["day"], x["tier"])].append(x)
    CELLS[st] = [(d, tr, float(np.mean([v["c"] for v in vs])), float(np.mean([v["d"] for v in vs])),
                  float(np.mean([v["xr"] for v in vs])), vs[0]["T"]) for (d, tr), vs in g.items()]
ROWS_BY_ST = {st: sorted([x for x in E if x["st"] == st], key=lambda x: x["t"]) for st in STATIONS}


def avail_days(st, t): return tuple(sorted(d for d, k in TK[st].items() if k <= t))


POOL_CACHE = {}
def pool_line_at(t):
    key = tuple((st, avail_days(st, t)[-1] if avail_days(st, t) else None) for st in STATIONS)
    if key in POOL_CACHE: return POOL_CACHE[key]
    xs, ys, per_st = [], [], {}
    for st in STATIONS:
        av = set(avail_days(st, t))
        cs = [(c, xr, T) for (dd, tr, c, d, xr, T) in CELLS[st] if dd in av]
        if len(cs) >= 3:
            per_st[st] = (line([v[1] for v in cs], [v[2] - v[0] for v in cs]), len({dd for (dd, tr, c, d, xr, T) in CELLS[st] if dd in av}))
        xs += [v[1] for v in cs]; ys += [v[2] - v[0] for v in cs]
    pool = line(xs, ys) if len(xs) >= 5 else None
    shr = {}
    if pool is not None:
        for st, ((sa, sb, stau), sn) in per_st.items():
            w = sn / (sn + SHRINK_K)
            shr[st] = (pool[0] + (sa - pool[0]) * w, pool[1] + (sb - pool[1]) * w, pool[2] + (stau - pool[2]) * w)
    # charter-158 tempering, as-of: lambda per group on pooled training rows (last 25 available days per station, <=200 rows/station)
    lam_by_grp = {g_: 1.0 for g_ in GROUPS}
    if pool is not None:
        tr_rows = []
        for st in STATIONS:
            if st not in shr: continue
            av = avail_days(st, t)[-25:]; avs = set(av)
            rs = [x for x in ROWS_BY_ST[st] if x["day"] in avs]
            if len(rs) > 200: rs = [rs[i] for i in np.linspace(0, len(rs) - 1, 200).astype(int)]
            tr_rows += rs
        for g_ in GROUPS:
            sub = [x for x in tr_rows if x["grp"] == g_]
            if len({(x["st"], x["day"]) for x in sub}) < 12: continue
            best = None
            for lam in TGRID:
                cells = collections.defaultdict(list)
                for x in sub:
                    cells[(x["st"], x["day"], x["tier"])].append(rps(lik(x["served"], x["order"], x["cen"], x["c"], x["xr"], *shr[x["st"]], lam), x["order"], x["tb"]))
                m = float(np.mean([np.mean(v) for v in cells.values()]))
                if best is None or m < best[0] - 1e-12: best = (m, lam)
            lam_by_grp[g_] = best[1]
    # the same tempering machinery on the FEATURE form: delta = k_st * m_g * x, m_g per group on the same pooled training rows
    kraw_st, m_by_grp = {}, {g_: 1.0 for g_ in GROUPS}
    if pool is not None:
        for st in STATIONS:
            av = set(avail_days(st, t))
            cs = [(c, xr, T) for (dd, tr, c, d, xr, T) in CELLS[st] if dd in av]
            den = sum(xr * xr for c, xr, T in cs); num = sum((T - c) * xr for c, xr, T in cs)
            kraw_st[st] = min(1.0, max(0.0, num / den)) if den > 0 and len(cs) >= 5 else 0.0
        tr2 = []
        for st in STATIONS:
            if st not in shr: continue
            av = avail_days(st, t)[-25:]; avs = set(av)
            rs = [x for x in ROWS_BY_ST[st] if x["day"] in avs]
            if len(rs) > 120: rs = [rs[i] for i in np.linspace(0, len(rs) - 1, 120).astype(int)]
            tr2 += rs
        for g_ in GROUPS:
            sub = [x for x in tr2 if x["grp"] == g_]
            if len({(x["st"], x["day"]) for x in sub}) < 12: continue
            best = None
            for m_ in TGRID:
                cells = collections.defaultdict(list)
                for x in sub:
                    dl_ = max(-CAP, min(CAP, kraw_st[x["st"]] * m_ * x["xr"]))
                    cells[(x["st"], x["day"], x["tier"])].append(rps(shifted(x["served"], x["order"], x["cen"], x["edges"], dl_), x["order"], x["tb"]))
                mm = float(np.mean([np.mean(v) for v in cells.values()]))
                if best is None or mm < best[0] - 1e-12: best = (mm, m_)
            m_by_grp[g_] = best[1]
    POOL_CACHE[key] = (pool, shr, lam_by_grp, m_by_grp)
    return POOL_CACHE[key]


STATS = {}; PAIRS = {}
for st in STATIONS:
    ES = ROWS_BY_ST[st]; cells = CELLS[st]; cache = {}

    def params_at(t):
        av = avail_days(st, t); key = av[-1] if av else None
        if key in cache: return cache[key]
        avs = set(av)
        tr = [(c, d, xr, T) for (dd, tier_, c, d, xr, T) in cells if dd in avs]
        ndays = len({dd for (dd, tier_, c, d, xr, T) in cells if dd in avs})
        out = dict(n=ndays, k=0.0, kraw=0.0, ab=(0.0, 0.0), deb=None, raw=None, lam=1.0, pool=None, temp=None, mtemp=None, tau_naive=None)
        if ndays >= a.min_train_days and len(tr) >= 5:
            den = sum(d * d for c, d, xr, T in tr); num = sum((T - c) * d for c, d, xr, T in tr)
            out["k"] = min(1.0, max(0.0, num / den)) if den > 0 else 0.0
            den = sum(xr * xr for c, d, xr, T in tr); num = sum((T - c) * xr for c, d, xr, T in tr)
            out["kraw"] = min(1.0, max(0.0, num / den)) if den > 0 else 0.0
            al, kk = ols([xr for c, d, xr, T in tr], [T - c for c, d, xr, T in tr]); out["ab"] = (al, min(1.0, max(0.0, kk)))
            out["deb"] = line([d for c, d, xr, T in tr], [T - c for c, d, xr, T in tr])
            out["raw"] = line([xr for c, d, xr, T in tr], [T - c for c, d, xr, T in tr])
            res = [T - (c + d) for c, d, xr, T in tr]
            out["tau_naive"] = max(MIN_TAU, float(np.sqrt(np.mean(np.square(res)))))
            trrows = [x for x in ES if x["day"] in avs]
            a_, b_, tau = out["deb"]; best = None
            for lam in LAMBDAS:
                g = collections.defaultdict(list)
                for x in trrows:
                    g[(x["day"], x["tier"])].append(rps(lik(x["served"], x["order"], x["cen"], x["c"], x["d"], a_, b_, tau, lam), x["order"], x["tb"]))
                m = float(np.mean([np.mean(v) for v in g.values()]))
                if best is None or m < best[0] - 1e-12: best = (m, lam)
            out["lam"] = best[1]
            pool, shr, lam_by_grp, m_by_grp = pool_line_at(t)
            if pool is not None and st in shr:
                out["pool"] = shr[st]; out["temp"] = dict(lam_by_grp); out["mtemp"] = dict(m_by_grp)
        cache[key] = out
        return out

    rows = []
    for x in ES:
        P = params_at(x["t"])
        if P["n"] < a.min_train_days or P["deb"] is None: continue
        s = sgn(st, x["day"]); o, cen, ed, c, d, xr = x["order"], x["cen"], x["edges"], x["c"], x["d"], x["xr"]
        dl = max(-CAP, min(CAP, P["k"] * d)); dlr = max(-CAP, min(CAP, P["kraw"] * xr)); dab = max(-CAP, min(CAP, P["ab"][0] + P["ab"][1] * xr))
        a_, b_, tau = P["deb"]
        ld = lik(x["served"], o, cen, c, d, a_, b_, tau); ll = lik(x["served"], o, cen, c, d, a_, b_, tau, P["lam"])
        rec = max(-CAP, min(CAP, mean_on(ld, o, cen) - c)); recl = max(-CAP, min(CAP, mean_on(ll, o, cen) - c))
        lp = lik(x["served"], o, cen, c, xr, *P["pool"]) if P["pool"] else dict(x["served"])
        lpt = lik(x["served"], o, cen, c, xr, *P["pool"], P["temp"][x["grp"]]) if P["pool"] else dict(x["served"])
        dst = max(-CAP, min(CAP, P["kraw"] * (P["mtemp"][x["grp"]] if P["mtemp"] else 1.0) * xr))
        rect = max(-CAP, min(CAP, mean_on(lpt, o, cen) - c))
        vec = {"base": dict(x["served"]), "shift_roll": shifted(x["served"], o, cen, ed, dl), "shift_raw": shifted(x["served"], o, cen, ed, dlr),
               "shift_ab": shifted(x["served"], o, cen, ed, dab), "shift_temp": shifted(x["served"], o, cen, ed, dst),
               "lik_rec_temp": shifted(x["served"], o, cen, ed, rect),
               "rsA": shifted(x["served"], o, cen, ed, s * dl), "rsB": shifted(x["served"], o, cen, ed, -s * dl),
               "lik_deb": ld, "lik_raw": lik(x["served"], o, cen, c, xr, *P["raw"]), "lik_lam": ll,
               "lik_rec": shifted(x["served"], o, cen, ed, rec), "lik_rec_lam": shifted(x["served"], o, cen, ed, recl),
               "lik_pool": lp, "lik_pool_temp": lpt,
               **{arm: (lik(x["served"], o, cen, c, xr, *P["pool"], tbl[x["grp"]]) if P["pool"] else dict(x["served"])) for arm, tbl in FIXED.items()},
               "lik_naive": lik(x["served"], o, cen, c, d, 0.0, 1.0, P["tau_naive"]),
               "mirA": lik(x["served"], o, cen, c, a_ + s * (d - a_), a_, b_, tau), "mirB": lik(x["served"], o, cen, c, a_ - s * (d - a_), a_, b_, tau)}
        sc = {}
        for arm, p in vec.items():
            sc[arm] = dict(rps=rps(p, o, x["tb"]), pt=p[x["tb"]], ae=abs(mean_on(p, o, cen) - x["T"]), hit=1.0 if max(p, key=p.get) == x["tb"] else 0.0,
                           sd=sd_on(p, o, cen), pmode=max(p.values()), vec=np.array([p[k_] for k_ in o], dtype=np.float32))
        rows.append(dict(day=x["day"], tier=x["tier"], k=P["k"], kraw=P["kraw"], ab=P["ab"], lam=P["lam"], line=P["deb"], rawline=P["raw"], pool=P["pool"],
                         temp=P["temp"], mtemp=P["mtemp"], grp=x["grp"], d=d, xr=xr, T=x["T"], c=c, sc=sc, tbi=o.index(x["tb"]), taf_age=x["taf_age"]))

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
        ps = np.concatenate([r["sc"][arm]["vec"] for r in sub]).astype(float)
        os_ = np.concatenate([np.eye(len(r["sc"][arm]["vec"]))[r["tbi"]] for r in sub])
        bins = np.minimum((ps * nb).astype(int), nb - 1); obar = os_.mean(); rel = res = 0.0
        for i in range(nb):
            m = bins == i
            if m.any():
                rel += m.sum() * (ps[m].mean() - os_[m].mean()) ** 2; res += m.sum() * (os_[m].mean() - obar) ** 2
        return dict(REL=round(float(rel / len(ps)), 5), RES=round(float(res / len(ps)), 5), BS=round(float(((ps - os_) ** 2).mean()), 5))

    def block(sub, arms=REPORT):
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
        for arm in arms:
            if arm == "shift_roll": continue
            o[f"h2h d_rps {arm} - shift_roll"] = boot(cellavg(sub, lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["shift_roll"]["rps"]))
            o[f"h2h d_nll_nz {arm} - shift_roll"] = boot(cellavg(sub, lambda r, arm=arm: (nll(r["sc"][arm]["pt"]) - nll(r["sc"]["shift_roll"]["pt"])) if nz(r) else None))
        o["h2h d_rps lik_pool_temp - lik_pool"] = boot(cellavg(sub, lambda r: r["sc"]["lik_pool_temp"]["rps"] - r["sc"]["lik_pool"]["rps"]))
        o["h2h d_rps lik_pool_temp - shift_temp"] = boot(cellavg(sub, lambda r: r["sc"]["lik_pool_temp"]["rps"] - r["sc"]["shift_temp"]["rps"]))
        o["h2h d_nll_nz lik_pool_temp - shift_temp"] = boot(cellavg(sub, lambda r: (nll(r["sc"]["lik_pool_temp"]["pt"]) - nll(r["sc"]["shift_temp"]["pt"])) if nz(r) else None))
        o["h2h d_rps lik_rec_temp - shift_temp"] = boot(cellavg(sub, lambda r: r["sc"]["lik_rec_temp"]["rps"] - r["sc"]["shift_temp"]["rps"]))
        for arm in FIXED:
            o[f"h2h d_rps {arm} - lik_pool_temp"] = boot(cellavg(sub, lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["lik_pool_temp"]["rps"]))
            o[f"h2h d_nll_nz {arm} - lik_pool_temp"] = boot(cellavg(sub, lambda r, arm=arm: (nll(r["sc"][arm]["pt"]) - nll(r["sc"]["lik_pool_temp"]["pt"])) if nz(r) else None))
        o["h2h d_rps lik_low_lam - lik_prod_lam"] = boot(cellavg(sub, lambda r: r["sc"]["lik_low_lam"]["rps"] - r["sc"]["lik_prod_lam"]["rps"]))
        o["h2h d_nll_nz lik_low_lam - lik_prod_lam"] = boot(cellavg(sub, lambda r: (nll(r["sc"]["lik_low_lam"]["pt"]) - nll(r["sc"]["lik_prod_lam"]["pt"])) if nz(r) else None))
        o["murphy"] = {arm: murphy(sub, arm) for arm in ("base", "shift_roll", "shift_ab", "shift_temp", "lik_deb", "lik_pool", "lik_pool_temp", "lik_naive", "lik_rec", "lik_rec_temp")}
        o["mtemp_last"] = sub[-1]["mtemp"]
        o["k_median"] = round(float(np.median([r["k"] for r in sub])), 3); o["kraw_median"] = round(float(np.median([r["kraw"] for r in sub])), 3)
        o["ab_last"] = [round(v, 3) for v in sub[-1]["ab"]]
        o["lam_counter"] = dict(collections.Counter(r["lam"] for r in sub))
        o["line_last"] = [round(v, 3) for v in sub[-1]["line"]]; o["rawline_last"] = [round(v, 3) for v in sub[-1]["rawline"]]
        o["pool_last"] = [round(v, 3) for v in sub[-1]["pool"]] if sub[-1]["pool"] else None
        o["temp_last"] = sub[-1]["temp"]
        o["grp_counter"] = dict(collections.Counter(r["grp"] for r in sub))
        o["taf_age_h_median"] = round(float(np.median([r["taf_age"] for r in sub])), 1)
        return o

    res = {"all": block(rows)}
    for tr in ("12-24", "24-36", "36-48", "48+"):
        res["tier " + tr] = block([r for r in rows if r["tier"] == tr])
    for g_ in GROUPS:
        res["grp " + g_] = block([r for r in rows if r["grp"] == g_])
    STATS[st] = {"res": res}
    for arm in REPORT:
        PAIRS[(st, arm)] = cellavg(rows, lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["base"]["rps"])
        PAIRS[(st, arm + "|h2h")] = cellavg(rows, lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["shift_roll"]["rps"])
        PAIRS[(st, arm + "|hit")] = cellavg(rows, lambda r, arm=arm: r["sc"][arm]["hit"] - r["sc"]["base"]["hit"])
        for tr in ("12-24", "24-36", "36-48", "48+"):
            PAIRS[(st, arm + "|tier" + tr)] = cellavg([r for r in rows if r["tier"] == tr], lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["base"]["rps"])
            PAIRS[(st, arm + "|h2h|tier" + tr)] = cellavg([r for r in rows if r["tier"] == tr], lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["shift_roll"]["rps"])
        for g_ in GROUPS:
            PAIRS[(st, arm + "|grp" + g_)] = cellavg([r for r in rows if r["grp"] == g_], lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["base"]["rps"])
            PAIRS[(st, arm + "|h2h|grp" + g_)] = cellavg([r for r in rows if r["grp"] == g_], lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["shift_roll"]["rps"])
    PAIRS[(st, "shift_roll|ctrl")] = cellavg(rows, lambda r: r["sc"]["shift_roll"]["rps"] - 0.5 * (r["sc"]["rsA"]["rps"] + r["sc"]["rsB"]["rps"]))
    PAIRS[(st, "lik_deb|ctrl")] = cellavg(rows, lambda r: r["sc"]["lik_deb"]["rps"] - 0.5 * (r["sc"]["mirA"]["rps"] + r["sc"]["mirB"]["rps"]))
    PAIRS[(st, "temp-pool")] = cellavg(rows, lambda r: r["sc"]["lik_pool_temp"]["rps"] - r["sc"]["lik_pool"]["rps"])
    PAIRS[(st, "temp-shifttemp")] = cellavg(rows, lambda r: r["sc"]["lik_pool_temp"]["rps"] - r["sc"]["shift_temp"]["rps"])
    PAIRS[(st, "rectemp-shifttemp")] = cellavg(rows, lambda r: r["sc"]["lik_rec_temp"]["rps"] - r["sc"]["shift_temp"]["rps"])
    for arm in FIXED:
        PAIRS[(st, arm + "|vs_temp")] = cellavg(rows, lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["lik_pool_temp"]["rps"])
        PAIRS[(st, arm + "|vs_temp|nll")] = cellavg(rows, lambda r, arm=arm: (nll(r["sc"][arm]["pt"]) - nll(r["sc"]["lik_pool_temp"]["pt"])) if all(r["sc"][a_]["pt"] >= FLOOR for a_ in ARMS) else None)
        PAIRS[(st, arm + "|nll")] = cellavg(rows, lambda r, arm=arm: (nll(r["sc"][arm]["pt"]) - nll(r["sc"]["base"]["pt"])) if all(r["sc"][a_]["pt"] >= FLOOR for a_ in ARMS) else None)
        for g_ in GROUPS:
            PAIRS[(st, arm + "|vs_temp|grp" + g_)] = cellavg([r for r in rows if r["grp"] == g_], lambda r, arm=arm: r["sc"][arm]["rps"] - r["sc"]["lik_pool_temp"]["rps"])
    PAIRS[(st, "low-prod")] = cellavg(rows, lambda r: r["sc"]["lik_low_lam"]["rps"] - r["sc"]["lik_prod_lam"]["rps"])
    PAIRS[(st, "low-prod|nll")] = cellavg(rows, lambda r: (nll(r["sc"]["lik_low_lam"]["pt"]) - nll(r["sc"]["lik_prod_lam"]["pt"])) if all(r["sc"][a_]["pt"] >= FLOOR for a_ in ARMS) else None)
    PAIRS[(st, "temp|nll")] = cellavg(rows, lambda r: (nll(r["sc"]["lik_pool_temp"]["pt"]) - nll(r["sc"]["base"]["pt"])) if all(r["sc"][a_]["pt"] >= FLOOR for a_ in ARMS) else None)
    for g_ in GROUPS:
        PAIRS[(st, "low-prod|grp" + g_)] = cellavg([r for r in rows if r["grp"] == g_], lambda r: r["sc"]["lik_low_lam"]["rps"] - r["sc"]["lik_prod_lam"]["rps"])
    for tr in ("12-24", "24-36", "36-48", "48+"):
        PAIRS[(st, "temp-shifttemp|tier" + tr)] = cellavg([r for r in rows if r["tier"] == tr], lambda r: r["sc"]["lik_pool_temp"]["rps"] - r["sc"]["shift_temp"]["rps"])
    for g_ in GROUPS:
        PAIRS[(st, "temp-shifttemp|grp" + g_)] = cellavg([r for r in rows if r["grp"] == g_], lambda r: r["sc"]["lik_pool_temp"]["rps"] - r["sc"]["shift_temp"]["rps"])
    b = res["all"]
    if not b: print(f"\n{st}: no evaluable rows", flush=True); continue
    print(f"\n{st} days={b['n_days']} cells={b['n_cells']} k_med={b['k_median']} kraw_med={b['kraw_median']} ab={b['ab_last']} line={b['line_last']} rawline={b['rawline_last']} pool={b['pool_last']} temp={b['temp_last']} lam={b['lam_counter']} grp={b['grp_counter']} age_med={b['taf_age_h_median']}h")
    print(f"  RPS base {b['rps']['base']:.4f} | " + " ".join(f"{arm} {b['rps'][arm]:.4f}" for arm in REPORT))
    for arm in REPORT:
        d_ = b[f"d_rps {arm}"]; n_ = b[f"d_nll_nz {arm}"]; h_ = b[f"d_hit {arm}"]
        print(f"  {arm:13s} dRPS {d_[0]:+.4f} [{d_[1]:+.4f},{d_[2]:+.4f}] P>0 {d_[3]:.2f} | dNLLnz {n_[0]:+.4f} [{n_[1]:+.4f},{n_[2]:+.4f}] | dHit {h_[0]:+.3f} | sd {b['sd']['base']:.2f}->{b['sd'][arm]:.2f} pmode {b['pmode']['base']:.3f}->{b['pmode'][arm]:.3f} hit {b['hit']['base']:.3f}->{b['hit'][arm]:.3f}")
    print(f"  ctrl: shift-rsign {b['d_rps shift_roll - rsign'][:3]}  lik-mirror {b['d_rps lik_deb - mirror'][:3]}  temp-pool {b['h2h d_rps lik_pool_temp - lik_pool'][:3]}  temp-shifttemp {b['h2h d_rps lik_pool_temp - shift_temp'][:3]}  rectemp-shifttemp {b['h2h d_rps lik_rec_temp - shift_temp'][:3]}  mtemp={b['mtemp_last']}")
    for arm in ("lik_pool", "lik_pool_temp", "lik_raw", "lik_rec", "shift_ab"):
        print(f"  h2h {arm}-shift_roll dRPS {b[f'h2h d_rps {arm} - shift_roll'][:3]} dNLLnz {b[f'h2h d_nll_nz {arm} - shift_roll'][:3]}")
    print("  murphy:", {arm: (v["REL"], v["RES"]) for arm, v in b["murphy"].items()})
    for tr in ("12-24", "24-36", "36-48", "48+"):
        bt = res["tier " + tr]
        if bt: print(f"  tier {tr:5s} n_days={bt['n_days']:2d} dRPS shift {bt['d_rps shift_roll'][0]:+.4f} pool {bt['d_rps lik_pool'][0]:+.4f} temp {bt['d_rps lik_pool_temp'][0]:+.4f} rec {bt['d_rps lik_rec'][0]:+.4f} | h2h pool-shift {bt['h2h d_rps lik_pool - shift_roll'][:3]} temp-shift {bt['h2h d_rps lik_pool_temp - shift_roll'][:3]}")
    for g_ in GROUPS:
        bg = res["grp " + g_]
        if bg: print(f"  grp {g_:15s} n_days={bg['n_days']:2d} dRPS shift {bg['d_rps shift_roll'][0]:+.4f} pool {bg['d_rps lik_pool'][0]:+.4f} temp {bg['d_rps lik_pool_temp'][0]:+.4f} | h2h pool-shift {bg['h2h d_rps lik_pool - shift_roll'][:3]}")


def combined(keys, n=4000):
    means, draws = [], []
    for k_ in keys:
        pr = PAIRS.get(k_)
        if not pr: continue
        g = collections.defaultdict(list)
        for d_, v in pr: g[d_].append(v)
        G = [np.array(v) for v in g.values()]
        sums = np.array([z.sum() for z in G]); cnt = np.array([len(z) for z in G])
        idx = rng.integers(0, len(G), (n, len(G)))
        draws.append(sums[idx].sum(1) / cnt[idx].sum(1)); means.append(float(np.mean([v for _, v in pr])))
    if not draws: return None
    b = np.mean(draws, 0)
    return [round(float(np.mean(means)), 5), round(float(np.percentile(b, 2.5)), 5), round(float(np.percentile(b, 97.5)), 5), round(float(np.mean(b > 0)), 4), len(draws)]


SETS = {"all_17": STATIONS, "cn_8": [s_ for s_ in STATIONS if s_.startswith("Z")], "kr_ph_3": ["RKPK", "RKSI", "RPLL"],
        "rest_6": ["FACT", "KBKF", "LEMD", "MMMX", "SAEZ", "SBGR"]}
ACROSS = {}
for name, sts in SETS.items():
    for arm in REPORT:
        ACROSS[f"{name} dRPS {arm}"] = combined([(s_, arm) for s_ in sts])
        ACROSS[f"{name} dHit {arm}"] = combined([(s_, arm + "|hit") for s_ in sts])
        if arm != "shift_roll": ACROSS[f"{name} h2h {arm} - shift_roll"] = combined([(s_, arm + "|h2h") for s_ in sts])
    ACROSS[f"{name} shift_roll - rsign"] = combined([(s_, "shift_roll|ctrl") for s_ in sts])
    ACROSS[f"{name} lik_deb - mirror"] = combined([(s_, "lik_deb|ctrl") for s_ in sts])
    ACROSS[f"{name} lik_pool_temp - lik_pool"] = combined([(s_, "temp-pool") for s_ in sts])
    ACROSS[f"{name} lik_pool_temp - shift_temp"] = combined([(s_, "temp-shifttemp") for s_ in sts])
    ACROSS[f"{name} lik_rec_temp - shift_temp"] = combined([(s_, "rectemp-shifttemp") for s_ in sts])
    for arm in FIXED:
        ACROSS[f"{name} {arm} - lik_pool_temp"] = combined([(s_, arm + "|vs_temp") for s_ in sts])
        ACROSS[f"{name} dNLL {arm}"] = combined([(s_, arm + "|nll") for s_ in sts])
        ACROSS[f"{name} dNLL {arm} - lik_pool_temp"] = combined([(s_, arm + "|vs_temp|nll") for s_ in sts])
    ACROSS[f"{name} dNLL lik_pool_temp"] = combined([(s_, "temp|nll") for s_ in sts])
    ACROSS[f"{name} lik_low_lam - lik_prod_lam"] = combined([(s_, "low-prod") for s_ in sts])
    ACROSS[f"{name} dNLL lik_low_lam - lik_prod_lam"] = combined([(s_, "low-prod|nll") for s_ in sts])
    if name == "all_17":
        for g_ in GROUPS:
            ACROSS[f"{name} grp{g_} lik_low_lam - lik_prod_lam"] = combined([(s_, "low-prod|grp" + g_) for s_ in sts])
            for arm in FIXED:
                ACROSS[f"{name} grp{g_} {arm} - lik_pool_temp"] = combined([(s_, arm + "|vs_temp|grp" + g_) for s_ in sts])
    if name == "all_17":
        for tr in ("12-24", "24-36", "36-48", "48+"):
            for arm in ("shift_roll", "shift_ab", "shift_temp", "lik_pool", "lik_pool_temp", "lik_rec", "lik_rec_temp"):
                ACROSS[f"{name} tier{tr} dRPS {arm}"] = combined([(s_, arm + "|tier" + tr) for s_ in sts])
                if arm != "shift_roll": ACROSS[f"{name} tier{tr} h2h {arm} - shift_roll"] = combined([(s_, arm + "|h2h|tier" + tr) for s_ in sts])
            ACROSS[f"{name} tier{tr} lik_pool_temp - shift_temp"] = combined([(s_, "temp-shifttemp|tier" + tr) for s_ in sts])
        for g_ in GROUPS:
            for arm in ("shift_roll", "shift_temp", "lik_pool", "lik_pool_temp", "lik_rec_temp"):
                ACROSS[f"{name} grp{g_} dRPS {arm}"] = combined([(s_, arm + "|grp" + g_) for s_ in sts])
                if arm != "shift_roll": ACROSS[f"{name} grp{g_} h2h {arm} - shift_roll"] = combined([(s_, arm + "|h2h|grp" + g_) for s_ in sts])
            ACROSS[f"{name} grp{g_} lik_pool_temp - shift_temp"] = combined([(s_, "temp-shifttemp|grp" + g_) for s_ in sts])
print("\n=== across stations (equal station weight, day bootstrap within station) ===")
for k_, v in ACROSS.items(): print(f"  {k_:46s} {v}")
json.dump(dict(config=vars(a), stations={st: v["res"] for st, v in STATS.items()}, across=ACROSS), open(a.out, "w"), indent=1, default=str)
print("wrote", a.out)
