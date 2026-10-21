#!/usr/bin/env python3
"""v3: new directions.  Per station-day, the first disagreeing P0>36h (and P0-M)
decision, with: trajectory of the market afterwards (does it come around?),
book staleness, ET hour, persistence anchoring, six-system own modes, station
truth volatility, log-score d, day-level clustering keys."""
import gzip, glob, json, math, os, sqlite3, statistics, collections
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SRC = {"A": os.path.join(HERE, "..", "conditional_advantage_20261006", "rows"), "B": os.path.join(HERE, "rows")}
F_ST = {"KATL","KAUS","KBKF","KDAL","KDEN","KHOU","KLAX","KLGA","KMIA","KORD","KSEA","KSFO"}
REGION = {"K":"US","C":"CA","E":"EU_N","L":"EU_S","R":"JP_KR","Z":"CN","V":"IN_SEA","W":"SEA","S":"SAM","M":"MEX_CAM","F":"AFR","N":"NZ","O":"ME","U":"RU","Y":"AU","P":"US"}
ET = ZoneInfo("America/New_York")
def iso(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))
def mom(v, t):
    s = sum(v); m = sum(a*b for a, b in zip(v, t))/s; return m, math.sqrt(max(sum(a*(b-m)**2 for a, b in zip(v, t))/s, 0))
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, isolation_level=None, timeout=30)
conn.execute("PRAGMA query_only=1")
truth = {}
for st, d, tmax in conn.execute("SELECT station_id, event_date, settled_tmax_c FROM basket_station_day_settlement_label l WHERE truth_revision_no=(SELECT MAX(truth_revision_no) FROM basket_station_day_settlement_label x WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"):
    if tmax is not None: truth[(st, d)] = float(tmax)
conn.close()
# station truth volatility (std of day-to-day change) over all labels
vol = {}
by_st = collections.defaultdict(dict)
for (st, d), t in truth.items(): by_st[st][d] = t
for st, dd in by_st.items():
    ch = [dd[d]-dd[(date.fromisoformat(d)-timedelta(days=1)).isoformat()] for d in dd if (date.fromisoformat(d)-timedelta(days=1)).isoformat() in dd]
    if len(ch) >= 10: vol[st] = statistics.pstdev(ch)
first_seen = {}
fs = os.path.join(HERE, "market_first_seen.tsv")
if os.path.exists(fs):
    for line in open(fs):
        p = line.strip().split("|")
        if len(p) == 3: first_seen[(p[0], p[1])] = p[2]
def norm_vec(v):
    v = [float(x or 0) for x in v]; s = sum(v); return [x/s for x in v] if s > 0 else None
out = open(os.path.join(HERE, "recs_v3.jsonl"), "w"); n = 0
for win, dsrc in SRC.items():
    for f in sorted(glob.glob(os.path.join(dsrc, "*.jsonl.gz"))):
        bydays = collections.defaultdict(list)
        for line in gzip.open(f, "rt"):
            r = json.loads(line); bydays[r["d"]].append(r)
        for d, rows in bydays.items():
            rows.sort(key=lambda r: r["t"])
            good = [r for r in rows if r.get("q") is not None and r.get("h") is not None and r["y"] in r["names"] and norm_vec(r["p"]) and norm_vec(r["q"])]
            if not good: continue
            for stage_sel in ("P0", "P0-M"):
                cand = [r for r in good if r["stage"] == stage_sel and (stage_sel != "P0" or r["h"] > 36)]
                first = None
                for r in cand:
                    p, q = norm_vec(r["p"]), norm_vec(r["q"]); K = len(p)
                    pm = max(range(K), key=lambda i: p[i]); qm = max(range(K), key=lambda i: q[i])
                    if pm != qm: first = (r, p, q, pm, qm); break
                if first is None: continue
                r, p, q, pm, qm = first; K = len(p); yi = r["names"].index(r["y"]); idx = list(range(K))
                mp, sdp = mom(p, idx); mq, sdq = mom(q, idx)
                temps = r["temps"]
                rec = dict(win=win, st=r["st"], d=d, tz=r.get("tz"), stage=stage_sel, h=round(r["h"], 2), t=r["t"], region=REGION.get(r["st"][0], "?"),
                           unit="F" if r["st"] in F_ST else "C", K=K, yi=yi, pm=pm, qm=qm, warm=int(pm > qm), absdmode=abs(pm-qm),
                           p_hit=int(pm==yi), q_hit=int(qm==yi), p_pm=p[pm], q_qm=q[qm], q_at_pm=q[pm], dmu=mp-mq, absdmu=abs(mp-mq), sd_q=sdq, sd_p=sdp,
                           d_log=math.log(max(p[yi],1e-3))-math.log(max(q[yi],1e-3)), truth_minus_qm=yi-qm, truth_minus_pm=yi-pm,
                           season=r.get("season"), vol=vol.get(r["st"]))
                bk = r["book"]; rec["ask_pm"] = bk[pm][1] if bk[pm] and bk[pm][1] is not None else None
                # staleness
                if r.get("book_at"):
                    rec["book_stale_h"] = (iso(r["t"]) - iso(r["book_at"][1])).total_seconds()/3600
                    rec["book_oldest_h"] = (iso(r["t"]) - iso(r["book_at"][0])).total_seconds()/3600
                rec["snap_age_min"] = (iso(r["t"]) - iso(r["snap_at"])).total_seconds()/60
                # ET clock
                et = iso(r["t"]).astimezone(ET); rec["et_hour"] = et.hour; rec["et_wday"] = et.weekday()
                rec["et_night"] = int(et.hour < 7 or et.hour >= 23); rec["et_weekend"] = int(et.weekday() >= 5)
                # market age
                fsn = first_seen.get((r["st"], d))
                if fsn: rec["market_age_h"] = (iso(r["t"]) - iso(fsn)).total_seconds()/3600
                # persistence anchoring
                y1 = truth.get((r["st"], (date.fromisoformat(d)-timedelta(days=1)).isoformat()))
                valid = [(i, t) for i, t in enumerate(temps) if t is not None]; inner = valid[1:-1] if len(valid) > 3 else valid
                (i0, t0), (i1, t1) = inner[0], inner[-1]
                def t2o(tc): return i0 + (tc - t0) * (i1 - i0) / (t1 - t0)
                if y1 is not None:
                    y1b = round(t2o(y1)); rec["qm_is_y1"] = int(qm == y1b); rec["pm_is_y1"] = int(pm == y1b)
                    rec["pm_change"] = pm - y1b; rec["qm_change"] = qm - y1b
                    rec["model_bigger_change"] = int(abs(pm - y1b) > abs(qm - y1b)); rec["abs_pm_change"] = abs(pm - y1b)
                    # 7-day mean anomaly
                    past = [truth.get((r["st"], (date.fromisoformat(d)-timedelta(days=k)).isoformat())) for k in range(1, 8)]
                    past = [x for x in past if x is not None]
                    if len(past) >= 4:
                        mb = t2o(statistics.mean(past)); rec["pm_anom7"] = pm - mb; rec["qm_anom7"] = qm - mb; rec["model_bigger_anom"] = int(abs(pm-mb) > abs(qm-mb))
                # six systems' own modes (normal approx from sys_stats on ordinal axis)
                ss = r.get("sys_stats") or {}
                in_pm = in_qm = 0; nsys = 0
                for sid, (m_c, s_c) in ss.items():
                    o = t2o(m_c); nsys += 1
                    if round(o) == pm: in_pm += 1
                    elif round(o) == qm: in_qm += 1
                rec["nsys"] = nsys; rec["sys_mode_in_pm"] = in_pm; rec["sys_mode_in_qm"] = in_qm; rec["sys_mode_elsewhere"] = nsys - in_pm - in_qm
                # trajectory afterwards: market at model's bucket pm and market mode at later checkpoints
                later = [x for x in good if x["t"] > r["t"]]
                def at(sel):
                    if not sel: return None
                    x = sel[-1]; qq = norm_vec(x["q"]); 
                    return dict(q_at_pm0=qq[pm], qm=max(range(len(qq)), key=lambda i: qq[i]), h=x["h"], stage=x["stage"])
                rec["last_gt36"] = at([x for x in later if x["h"] > 36])
                rec["last_gt12"] = at([x for x in later if x["h"] > 12])
                rec["last_any"] = at(later)
                la = rec["last_any"]
                if la:
                    rec["mkt_came_around"] = int(la["qm"] == pm); rec["final_q_at_pm0"] = la["q_at_pm0"]
                    rec["mkt_moved_to_pm_by_36h"] = int(rec["last_gt36"]["qm"] == pm) if rec["last_gt36"] else None
                # max q at pm0 while h>36 (best exit price within Proper window)
                w36 = [norm_vec(x["q"])[pm] for x in later if x["h"] > 36]
                rec["max_q_pm0_gt36"] = max(w36) if w36 else None
                out.write(json.dumps(rec)+"\n"); n += 1
print("recs", n, "first_seen", len(first_seen), "vol", len(vol))
