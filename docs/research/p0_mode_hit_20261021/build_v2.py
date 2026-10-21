#!/usr/bin/env python3
"""v2: all three stages, all decisions, with momentum / depth / per-system side /
persistence / local-time / region / rolling-station features.  Output recs_v2.jsonl:
one line per decision (flag `first_band`, `last_stage` mark the two views)."""
import gzip, glob, json, math, os, sqlite3, statistics, collections
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SRC = {"A": os.path.join(HERE, "..", "conditional_advantage_20261006", "rows"), "B": os.path.join(HERE, "rows")}
BANDS = [(60, 1e9, "60h+"), (48, 60, "48-60h"), (36, 48, "36-48h"), (24, 36, "24-36h"), (12, 24, "12-24h"), (6, 12, "6-12h"), (-1e9, 6, "<6h")]
F_ST = {"KATL","KAUS","KBKF","KDAL","KDEN","KHOU","KLAX","KLGA","KMIA","KORD","KSEA","KSFO"}
SYS = ["ECMWF_IFS_ENS_OPEN_PF50","ECMWF_AIFS_ENS_GLOBAL","DWD_ICON_GLOBAL_EPS","NOAA_GEFS_05","ECCC_GEM_GLOBAL_ENS","NOAA_AIGEFS_OPS_025","NOAA_AIGEFS_025"]
SHORT = {"ECMWF_IFS_ENS_OPEN_PF50":"ifs","ECMWF_AIFS_ENS_GLOBAL":"aifs","DWD_ICON_GLOBAL_EPS":"icon","NOAA_GEFS_05":"gefs","ECCC_GEM_GLOBAL_ENS":"gem","NOAA_AIGEFS_OPS_025":"aigefs","NOAA_AIGEFS_025":"aigefs"}
REGION = {"K":"US","C":"CA","E":"EU_N","L":"EU_S","R":"JP_KR","Z":"CN","V":"IN_SEA","W":"SEA","S":"SAM","M":"MEX_CAM","F":"AFR","N":"NZ","O":"ME","U":"RU","Y":"AU","P":"US"}
def band(h):
    for lo, hi, n in BANDS:
        if lo <= h < hi: return n
def mom(v, t):
    s = sum(v); m = sum(a*b for a, b in zip(v, t))/s
    return m, math.sqrt(max(sum(a*(b-m)**2 for a, b in zip(v, t))/s, 0))
def ent(v): return -sum(x*math.log(x) for x in v if x > 0)
def iso(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))
# truth map for persistence
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, isolation_level=None, timeout=30)
conn.execute("PRAGMA query_only=1")
truth = {}
for st, d, tmax in conn.execute("SELECT station_id, event_date, settled_tmax_c FROM basket_station_day_settlement_label l WHERE truth_revision_no=(SELECT MAX(truth_revision_no) FROM basket_station_day_settlement_label x WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"):
    if tmax is not None: truth[(st, d)] = float(tmax)
conn.close()
def prev_truth(st, d, back=1):
    from datetime import date, timedelta
    y = (date.fromisoformat(d) - timedelta(days=back)).isoformat(); return truth.get((st, y))
out = open(os.path.join(HERE, "recs_v2.jsonl"), "w"); n = 0
for win, dsrc in SRC.items():
    for f in sorted(glob.glob(os.path.join(dsrc, "*.jsonl.gz"))):
        bydays = collections.defaultdict(list)
        for line in gzip.open(f, "rt"):
            r = json.loads(line); bydays[r["d"]].append(r)
        for d, rows in bydays.items():
            rows.sort(key=lambda r: r["t"])
            hist = []  # per-decision (t, stage, mean_q, qm, mean_p, pm, eid) in time order, only complete-market rows
            seen_band = set(); last_idx = {}
            for i, r in enumerate(rows):
                if r.get("q") is not None and r["y"] in r["names"]: last_idx[r["stage"]] = i
            for i, r in enumerate(rows):
                if r.get("q") is None or r.get("h") is None or r["y"] not in r["names"]: continue
                p = [float(x or 0) for x in r["p"]]; q = [float(x or 0) for x in r["q"]]
                if sum(p) <= 0 or sum(q) <= 0: continue
                sp, sq = sum(p), sum(q); p = [x/sp for x in p]; q = [x/sq for x in q]
                K = len(p); idx = list(range(K)); yi = r["names"].index(r["y"])
                pm = max(idx, key=lambda k: p[k]); qm = max(idx, key=lambda k: q[k])
                mp, sdp = mom(p, idx); mq, sdq = mom(q, idx)
                temps = r["temps"]; valid = [(k, t) for k, t in enumerate(temps) if t is not None]
                inner = valid[1:-1] if len(valid) > 3 else valid
                (i0, t0), (i1, t1) = inner[0], inner[-1]
                def t2o(tc): return i0 + (tc - t0) * (i1 - i0) / (t1 - t0)
                def o2t(o): return t0 + (o - i0) * (t1 - t0) / (i1 - i0)
                cen = r.get("centres") or {}; w = r.get("w") or {}
                side = (1 if pm > qm else -1) if pm != qm else 0
                rec = dict(win=win, st=r["st"], d=r["d"], stage=r["stage"], band=band(r["h"]), h=round(r["h"], 2), t=r["t"], pv=r["pv"],
                           unit="F" if r["st"] in F_ST else "C", region=REGION.get(r["st"][0], "?"), season=r.get("season"), K=K,
                           yi=yi, pm=pm, qm=qm, dmode=pm-qm, absdmode=abs(pm-qm), warm=int(pm > qm), p_hit=int(pm==yi), q_hit=int(qm==yi),
                           p_pm=p[pm], q_qm=q[qm], q_at_pm=q[pm], p_at_qm=p[qm], q_second=sorted(q)[-2], p_second=sorted(p)[-2],
                           gap_q=q[qm]-q[pm], gap_p=p[pm]-p[qm], mean_p=mp, mean_q=mq, dmu=mp-mq, absdmu=abs(mp-mq), sd_p=sdp, sd_q=sdq,
                           sharp_ratio=sdp/sdq if sdq else None, ent_p=ent(p), ent_q=ent(q), qm_edge=int(qm in (0, K-1)), pm_edge=int(pm in (0, K-1)),
                           maxsp=r.get("maxsp"), ms=r.get("ms"), bridge_gap=(r.get("bridge") or {}).get("center_gap_c"),
                           mode_temp=temps[pm], first_band=0, last_stage=int(last_idx.get(r["stage"]) == i),
                           logp=math.log(max(p[yi], 1e-3)), logq=math.log(max(q[yi], 1e-3)))
                key = (r["stage"], rec["band"])
                if key not in seen_band: seen_band.add(key); rec["first_band"] = 1
                bk = r["book"]
                def g(i, j): return bk[i][j] if bk[i] and bk[i][j] is not None else None
                rec.update(ask_pm=g(pm,1), bid_pm=g(pm,0), ask_qm=g(qm,1), bid_qm=g(qm,0), askdepth_pm=g(pm,3), askdepth_qm=g(qm,3), biddepth_pm=g(pm,2), biddepth_qm=g(qm,2))
                rec["spread_pm"] = (g(pm,1)-g(pm,0)) if g(pm,1) is not None and g(pm,0) is not None else None
                rec["spread_qm"] = (g(qm,1)-g(qm,0)) if g(qm,1) is not None and g(qm,0) is not None else None
                tot_ask = [x[3] for x in bk if x and x[3] is not None]; rec["askdepth_total"] = sum(tot_ask) if tot_ask else None
                rec["depth_ratio_pm_qm"] = (g(pm,3)/g(qm,3)) if g(pm,3) and g(qm,3) else None
                # systems
                sysc = {sid: t2o(c) for sid, c in cen.items() if isinstance(c, (int, float))}
                rec["n_sys"] = len(sysc)
                if sysc:
                    vals = list(sysc.values()); rec["sys_sd"] = statistics.pstdev(vals) if len(vals) > 1 else 0.0
                    rec["sys_side_frac"] = (sum(1 for c in vals if (c-mq)*side > 0)/len(vals)) if side else None
                    rec["sys_in_pm"] = sum(1 for c in vals if round(c) == pm)/len(vals); rec["sys_in_qm"] = sum(1 for c in vals if round(c) == qm)/len(vals)
                    for sid, c in sysc.items():
                        sh = SHORT.get(sid)
                        if sh and side: rec[f"side_{sh}"] = int((c-mq)*side > 0)
                    if w:
                        top = max(w, key=lambda s: w[s]); rec["w_max"] = w[top]; rec["top_sys"] = SHORT.get(top, top)
                        if top in sysc and side: rec["top_side_p0"] = int((sysc[top]-mq)*side > 0)
                        # weighted mean vs market
                        rec["wmean_minus_mq"] = sum(w[s]*sysc[s] for s in sysc if s in w)/max(sum(w[s] for s in sysc if s in w), 1e-9) - mq
                ri = r.get("run_init") or {}
                if ri:
                    newest = max(v for v in ri.values() if isinstance(v, str)); rec["run_cycle"] = newest[11:13]+"Z"
                    rec["run_age_h"] = (iso(r["t"]) - iso(newest)).total_seconds()/3600
                arr = [v for v in (r.get("arrival") or {}).values() if isinstance(v, str)]
                if arr: rec["age_h"] = (iso(r["t"]) - max(iso(a) for a in arr)).total_seconds()/3600
                c = r.get("cooling") or {}
                rec["clear"] = c.get("clearness_index")
                if c.get("clearness_index") is not None and c.get("history_clearness_mean") is not None and c.get("history_clearness_sd"):
                    rec["clear_anom"] = (c["clearness_index"]-c["history_clearness_mean"])/c["history_clearness_sd"]
                # local time / weekday
                try:
                    lt = iso(r["t"]).astimezone(ZoneInfo(r["tz"])); rec["local_hour"] = lt.hour; rec["dec_wday"] = lt.weekday()
                except Exception: pass
                rec["event_wday"] = datetime.fromisoformat(r["d"]).weekday()
                # persistence
                y1 = prev_truth(r["st"], r["d"]); y2 = prev_truth(r["st"], r["d"], 2)
                if y1 is not None:
                    rec["pm_minus_y1"] = temps[pm]-y1 if temps[pm] is not None else None
                    rec["qm_minus_y1"] = temps[qm]-y1 if temps[qm] is not None else None
                    if rec["pm_minus_y1"] is not None and rec["qm_minus_y1"] is not None:
                        rec["p0_closer_persist"] = int(abs(rec["pm_minus_y1"]) < abs(rec["qm_minus_y1"]))
                    rec["truth_minus_y1"] = r["tmax"]-y1 if r.get("tmax") is not None else None
                if y1 is not None and y2 is not None: rec["y1_minus_y2"] = y1-y2
                # momentum within station-day
                if hist:
                    pt, pstage, pmq, pqm, pmp, ppm, peid = hist[-1]
                    dt = (iso(r["t"])-iso(pt)).total_seconds()/3600
                    rec["dt_prev_h"] = dt; rec["dmq_prev"] = mq-pmq; rec["dmp_prev"] = mp-pmp
                    rec["mkt_toward_p0_prev"] = (mq-pmq)*side if side else None
                    rec["mkt_mode_changed"] = int(qm != pqm); rec["model_mode_changed"] = int(pm != ppm)
                    rec["mkt_moved_1h"] = int(abs(mq-pmq) > 0.05 and dt <= 1.5)
                    ft, fstage, fmq, fqm, fmp, fpm, feid = hist[0]
                    rec["dmq_first"] = mq-fmq; rec["dmp_first"] = mp-fmp; rec["mkt_toward_p0_first"] = (mq-fmq)*side if side else None
                    rec["n_prev_dec"] = len(hist); rec["hours_since_first"] = (iso(r["t"])-iso(ft)).total_seconds()/3600
                    stab = 0
                    for hh in reversed(hist):
                        if hh[5] == pm: stab += 1
                        else: break
                    rec["mode_stable_n"] = stab
                    rec["n_mkt_modes_seen"] = len({hh[3] for hh in hist} | {qm})
                else:
                    rec["n_prev_dec"] = 0; rec["mode_stable_n"] = 0; rec["n_mkt_modes_seen"] = 1
                hist.append((r["t"], r["stage"], mq, qm, mp, pm, r["eid"]))
                if r["stage"] == "P0-M":
                    taf = r.get("taf") or {}
                    rec["taf_mode"] = taf.get("mode"); rec["taf_rev"] = taf.get("rev")
                    if taf.get("tx_c") is not None and temps[pm] is not None and temps[qm] is not None:
                        rec["taf_minus_pm"] = taf["tx_c"]-temps[pm]; rec["taf_minus_qm"] = taf["tx_c"]-temps[qm]
                        rec["taf_closer_p0"] = int(abs(rec["taf_minus_pm"]) < abs(rec["taf_minus_qm"]))
                        rec["taf_bucket_is_pm"] = int(round(t2o(taf["tx_c"])) == pm); rec["taf_bucket_is_qm"] = int(round(t2o(taf["tx_c"])) == qm)
                    if taf.get("running_high_c") is not None and temps[pm] is not None:
                        rec["runhigh_minus_pm"] = taf["running_high_c"]-temps[pm]
                if r["stage"] == "P1":
                    ob = r.get("obs") or {}
                    rh = ob.get("running_high_c")
                    if rh is not None and temps[pm] is not None and temps[qm] is not None:
                        rec["runhigh_minus_pm"] = rh-temps[pm]; rec["runhigh_minus_qm"] = rh-temps[qm]
                        rec["runhigh_bucket"] = round(t2o(rh)); rec["pm_is_runhigh"] = int(round(t2o(rh)) == pm); rec["qm_is_runhigh"] = int(round(t2o(rh)) == qm)
                        rec["pm_above_runhigh"] = pm-round(t2o(rh)); rec["qm_above_runhigh"] = qm-round(t2o(rh))
                    ex = ob.get("excluded"); rec["n_excluded"] = len(ex) if isinstance(ex, list) else None
                    lk = ob.get("like") or {}
                    for k2 in ("mode", "sigma_c", "weight", "n_obs"):
                        if k2 in lk: rec[f"p1_{k2}"] = lk[k2]
                out.write(json.dumps(rec)+"\n"); n += 1
print("recs", n)
