#!/usr/bin/env python3
"""v5: conditional directional bias.  Per first-band decision: side masses
(above / below the market mode) for model, market mid and ask-sum; realized
side; conditions known at decision time.  Station market bias uses only days
settled before the decision (event_date <= D-2)."""
import gzip, glob, json, math, os, sqlite3, statistics, collections
from datetime import datetime, date, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SRC = {"A": os.path.join(HERE, "..", "conditional_advantage_20261006", "rows"), "B": os.path.join(HERE, "rows")}
BANDS = [(60, 1e9, "60h+"), (48, 60, "48-60h"), (36, 48, "36-48h"), (24, 36, "24-36h"), (12, 24, "12-24h"), (6, 12, "6-12h"), (-1e9, 6, "<6h")]
F_ST = {"KATL","KAUS","KBKF","KDAL","KDEN","KHOU","KLAX","KLGA","KMIA","KORD","KSEA","KSFO"}
REGION = {"K":"US","C":"CA","E":"EU","L":"EU","R":"JPKR","Z":"CN","V":"IN_SEA","W":"SEA","S":"SAM","M":"MEX","F":"AFR","N":"NZ","O":"ME","U":"RU"}
def band(h):
    for lo, hi, n in BANDS:
        if lo <= h < hi: return n
def norm(v):
    v = [float(x or 0) for x in v]; s = sum(v); return [x/s for x in v] if s > 0 else None
def mean_ord(v): return sum(i*x for i, x in enumerate(v))
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, isolation_level=None, timeout=30); conn.execute("PRAGMA query_only=1")
truth = {}
for st, d, tmax in conn.execute("SELECT station_id, event_date, settled_tmax_c FROM basket_station_day_settlement_label l WHERE truth_revision_no=(SELECT MAX(truth_revision_no) FROM basket_station_day_settlement_label x WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"):
    if tmax is not None: truth[(st, d)] = float(tmax)
conn.close()
# pass 1: per station-day first P0 decision market error (market mean ordinal - truth ordinal) for as-of station bias
decs = []
for win, dsrc in SRC.items():
    for f in sorted(glob.glob(os.path.join(dsrc, "*.jsonl.gz"))):
        bydays = collections.defaultdict(list)
        for line in gzip.open(f, "rt"):
            r = json.loads(line); bydays[r["d"]].append(r)
        for d, rows in bydays.items():
            rows.sort(key=lambda r: r["t"]); seen = set()
            for r in rows:
                if r.get("q") is None or r.get("h") is None or r["y"] not in r["names"]: continue
                p, q = norm(r["p"]), norm(r["q"])
                if not p or not q: continue
                fam = "P0>36h" if r["stage"] == "P0" and r["h"] > 36 else ("P0≤36h" if r["stage"] == "P0" else r["stage"])
                key = (fam, band(r["h"]))
                if key in seen: continue
                seen.add(key); decs.append((win, fam, key[1], d, r, p, q))
# station-day market error from first P0>36h decision
mkt_err = {}
for win, fam, b, d, r, p, q in decs:
    if fam == "P0>36h" and (r["st"], d) not in mkt_err:
        yi = r["names"].index(r["y"]); mkt_err[(r["st"], d)] = mean_ord(q) - yi
by_st_err = collections.defaultdict(dict)
for (st, d), e in mkt_err.items(): by_st_err[st][d] = e
out = open(os.path.join(HERE, "recs_v5.jsonl"), "w"); n = 0
for win, fam, b, d, r, p, q in decs:
    if fam == "P0≤36h": continue
    K = len(p); yi = r["names"].index(r["y"]); qm = max(range(K), key=lambda i: q[i]); pm = max(range(K), key=lambda i: p[i])
    bk = r["book"]; temps = r["temps"]
    def side(ks):
        ks = [k for k in ks if 0 <= k < K]
        asks = [bk[k][1] for k in ks if bk[k] and bk[k][1] is not None]
        return dict(M=sum(p[k] for k in ks), Q=sum(q[k] for k in ks), A=(sum(asks) if len(asks) == len(ks) and ks else None), hit=int(yi in ks), n=len(ks))
    above3 = side(range(qm+1, qm+4)); below3 = side(range(qm-3, qm)); above_all = side(range(qm+1, K)); below_all = side(range(0, qm))
    rec = dict(win=win, fam=fam, band=b, st=r["st"], d=d, h=round(r["h"], 2), unit="F" if r["st"] in F_ST else "C", region=REGION.get(r["st"][0], "?"), season=r.get("season"),
               K=K, yi=yi, qm=qm, pm=pm, truth_side=(1 if yi > qm else (-1 if yi < qm else 0)), truth_minus_qm=yi-qm, truth_minus_qmean=yi-mean_ord(q), truth_minus_pmean=yi-mean_ord(p),
               dmu=mean_ord(p)-mean_ord(q), model_side=(1 if pm > qm else (-1 if pm < qm else 0)),
               Mab=above_all["M"], Mbe=below_all["M"], Qab=above_all["Q"], Qbe=below_all["Q"], skew_q=above_all["Q"]-below_all["Q"], skew_m=above_all["M"]-below_all["M"],
               side_gap=(above_all["M"]-above_all["Q"]) - (below_all["M"]-below_all["Q"]),
               ab3=above3, be3=below3, qm_edge_hi=int(qm >= K-2), qm_edge_lo=int(qm <= 1), q_qm=q[qm], p_pm=p[pm])
    c = r.get("cooling") or {}
    if c.get("clearness_index") is not None and c.get("history_clearness_mean") is not None and c.get("history_clearness_sd"):
        rec["clear_anom"] = (c["clearness_index"]-c["history_clearness_mean"])/c["history_clearness_sd"]
    valid = [(i, t) for i, t in enumerate(temps) if t is not None]; inner = valid[1:-1] if len(valid) > 3 else valid
    (i0, t0), (i1, t1) = inner[0], inner[-1]
    def t2o(tc): return i0 + (tc - t0) * (i1 - i0) / (t1 - t0)
    y1 = truth.get((r["st"], (date.fromisoformat(d)-timedelta(days=1)).isoformat()))
    if y1 is not None: rec["y1_side"] = (1 if t2o(y1) > qm+0.5 else (-1 if t2o(y1) < qm-0.5 else 0)); rec["y1_minus_qm"] = t2o(y1)-qm
    cen = r.get("centres") or {}
    sysc = [t2o(v) for v in cen.values() if isinstance(v, (int, float))]
    if sysc:
        mq = mean_ord(q); rec["sys_above_frac"] = sum(1 for c_ in sysc if c_ > mq)/len(sysc); rec["n_sys"] = len(sysc)
    if r["stage"] == "P0-M":
        taf = (r.get("taf") or {}).get("tx_c")
        if taf is not None: rec["taf_side"] = (1 if t2o(taf) > qm+0.5 else (-1 if t2o(taf) < qm-0.5 else 0)); rec["taf_minus_qm"] = t2o(taf)-qm
    if r["stage"] == "P1":
        rh = (r.get("obs") or {}).get("running_high_c")
        if rh is not None: rec["runhigh_minus_qm"] = t2o(rh)-qm
    # as-of station market bias: days settled <= D-2
    prev = [e for dd, e in by_st_err[r["st"]].items() if dd <= (date.fromisoformat(d)-timedelta(days=2)).isoformat()]
    if len(prev) >= 8:
        m_ = statistics.mean(prev); se = statistics.stdev(prev)/math.sqrt(len(prev)); rec["st_bias_asof"] = m_; rec["st_bias_t"] = m_/se if se else 0.0
    last = sorted((dd, e) for dd, e in by_st_err[r["st"]].items() if dd <= (date.fromisoformat(d)-timedelta(days=2)).isoformat())
    if last: rec["last_err"] = last[-1][1]; rec["last_err_sign"] = (1 if last[-1][1] > 0.3 else (-1 if last[-1][1] < -0.3 else 0))
    out.write(json.dumps(rec)+"\n"); n += 1
print("recs", n)
