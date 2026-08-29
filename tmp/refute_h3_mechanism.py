"""Independent mechanism audit of H3.

Rebuilds the case set from the DB (not from tmp/h3_cases.json), pulls
applied_strength / likelihood_ratio_by_bucket from the real proof rows, and
asks the question H3 must answer: does the named mechanism VARY with edge?
"""
import json, math, sqlite3, statistics, sys
from bisect import bisect_right
from collections import Counter
sys.path.insert(0, "tmp")
from stage_pick import DB, local_close, parse

conn = sqlite3.connect(DB, uri=True, timeout=120.0)

held, settled, tzs = {}, {}, {}
for r in conn.execute("SELECT station_id,event_date,decision_at_utc,"
                      "window_buckets_json,leg_shares_json FROM paper_trade_position"
                      " ORDER BY decision_at_utc"):
    k = (str(r[0]), str(r[1]))
    e = held.setdefault(k, {"held": set(), "opened": str(r[2])})
    legs = json.loads(r[4] or "{}")
    e["held"].update([b for b, s in legs.items() if (s or 0) > 0] or json.loads(r[3]))
for r in conn.execute("SELECT station_id,event_date,settled_bucket,settled_tmax_c"
                      " FROM paper_trade_settlement"):
    settled[(str(r[0]), str(r[1]))] = (str(r[2]), r[3])
for r in conn.execute("SELECT DISTINCT station_id,event_date,settlement_timezone"
                      " FROM basket_probability_bridge_history"):
    tzs[(str(r[0]), str(r[1]))] = str(r[2])

books = {}
for r in conn.execute("SELECT station_id,event_date,observed_at,buckets_json"
                      " FROM basket_official_ws_snapshot ORDER BY observed_at"):
    k = (str(r[0]), str(r[1]))
    if k in settled:
        books.setdefault(k, []).append((str(r[2]), r[3]))
def asks_at(k, when):
    h = books.get(k)
    if not h: return {}
    i = bisect_right([x[0] for x in h], when)
    return ({b["bucket"]: float(b["best_ask"]) for b in json.loads(h[i-1][1])
             if b.get("best_ask")} if i else {})

_pc = {}
def proof(sha):
    if sha not in _pc:
        r = conn.execute("SELECT proof_json FROM basket_probability_update_proof"
                         " WHERE content_sha256=? LIMIT 1", (sha,)).fetchone()
        _pc[sha] = json.loads(r[0]) if r else None
    return _pc[sha]
_rc = {}
def p0root(k, sha):
    if k not in _rc:
        _rc[k] = {str(x[0]): x[1] for x in conn.execute(
            "SELECT envelope_content_sha256,envelope_json FROM"
            " basket_probability_bridge_history WHERE station_id=? AND"
            " event_date=? AND stage='P0'", k)}
    b = _rc[k].get(sha)
    return json.loads(b)["probabilities"] if b else None

rows = []
for k in sorted(held):
    if k not in settled or k not in tzs: continue
    close = local_close(k[1], tzs[k])
    chain = [(str(x[0]), x[1]) for x in conn.execute(
        "SELECT decision_at_utc,envelope_json FROM basket_probability_bridge_history"
        " WHERE state='COMMITTED' AND station_id=? AND event_date=? AND stage='P1'"
        " ORDER BY decision_at_utc", k)]
    alive = None
    for at, blob in chain:
        if at < held[k]["opened"]: continue
        if (close - parse(at)).total_seconds() < 0: continue
        env = json.loads(blob)
        if sum(v for b, v in env["probabilities"].items() if b in held[k]["held"]) > 0.05:
            alive = (at, env)
    if alive is None: continue
    at, env = alive
    p = env["probabilities"]; lin = env.get("lineage") or {}
    outside = {b: v for b, v in p.items() if b not in held[k]["held"]}
    if not outside: continue
    t = max(outside, key=outside.get)
    ask = asks_at(k, at).get(t)
    if ask is None: continue
    pr = proof(str(lin.get("metar_trajectory_proof_sha256") or ""))
    if pr is None: continue
    iv = json.loads(lin.get("bucket_intervals_c_json") or "{}")
    cen = {}
    for b, (lo, hi) in iv.items():
        if lo is None and hi is None: continue
        cen[b] = (hi-0.5 if lo is None else lo+0.5 if hi is None else (lo+hi)/2.0)
    rows.append(dict(k=k, at=at, p=p, target=t, p_t=outside[t], ask=ask,
                     edge=outside[t]-ask, won=settled[k][0]==t,
                     truth=settled[k][0], tmax=settled[k][1],
                     lead=(close-parse(at)).total_seconds()/3600.0,
                     s=float(pr["applied_strength"]), lr=pr["likelihood_ratio_by_bucket"],
                     rh=float(pr["running_high_c"]), exc=set(pr["hard_excluded_buckets"]),
                     cen=cen, prior=p0root(k, str(lin.get("p0_root_content_sha256") or "")),
                     held=held[k]["held"], iv=iv))

print("independent rebuild: n=%d" % len(rows))
BINS = (("<=0", lambda e: e<=0), ("0~0.10", lambda e: 0<e<=.10),
        ("0.10-0.25", lambda e: .10<e<=.25), (">0.25", lambda e: e>.25))
print("bucket_intervals present in %d/%d rows" % (sum(1 for r in rows if r["cen"]), len(rows)))

print("\n=== T1  does the NAMED mechanism vary with edge?")
print("  edge        n    s=0 share   median s   target LR   target above running-high   lead_med")
for name, f in BINS:
    sel = [r for r in rows if f(r["edge"])]
    ab = [r for r in sel if r["cen"].get(r["target"]) is not None]
    print("  %-10s %3d   %5.0f%%      %.3f      %.3f       %5.0f%% (%d/%d)          %5.2fh" % (
        name, len(sel), 100*sum(1 for r in sel if r["s"]==0)/len(sel),
        statistics.median([r["s"] for r in sel]),
        statistics.median([float(r["lr"].get(r["target"],1.0)) for r in sel]),
        100*sum(1 for r in ab if r["cen"][r["target"]]>r["rh"])/max(len(ab),1),
        sum(1 for r in ab if r["cen"][r["target"]]>r["rh"]), len(ab),
        statistics.median([r["lead"] for r in sel])))

print("\n=== T2  cross-tab: strength on/off  x  edge>0.25 ?  (chi-ish by hand)")
c = Counter((r["s"]>0, r["edge"]>0.25) for r in rows)
print("   ", dict(c))
on = [r for r in rows if r["s"]>0]; off=[r for r in rows if r["s"]==0]
for nm, sel in (("s=0", off), ("s>0", on)):
    print("  %s n=%d  edge>0.25 share %.0f%%  model %.1f%%  actual %.1f%%" % (
        nm, len(sel), 100*sum(1 for r in sel if r["edge"]>.25)/len(sel),
        100*statistics.mean(r["p_t"] for r in sel),
        100*sum(r["won"] for r in sel)/len(sel)))

print("\n=== T3  what actually separates the bins: ask, or the model?")
for name, f in BINS:
    sel = [r for r in rows if f(r["edge"])]
    print("  %-10s model %.3f  ask %.3f  -> edge is %.0f%% driven by ask spread" % (
        name, statistics.mean(r["p_t"] for r in sel),
        statistics.mean(r["ask"] for r in sel), 0))
mp=[r["p_t"] for r in rows]; ma=[r["ask"] for r in rows]; me=[r["edge"] for r in rows]
def corr(a,b):
    ma_,mb_=statistics.mean(a),statistics.mean(b)
    num=sum((x-ma_)*(y-mb_) for x,y in zip(a,b))
    return num/math.sqrt(sum((x-ma_)**2 for x in a)*sum((y-mb_)**2 for y in b))
print("  corr(edge, model_p)=%.3f   corr(edge, -ask)=%.3f" % (corr(me,mp), corr(me,[-x for x in ma])))

print("\n=== T4  is the target-bucket posterior really the untouched P0 prior?")
for nm, sel in (("s=0", [r for r in rows if r["s"]==0 and r["prior"]]),
                ("s>0", [r for r in rows if r["s"]>0 and r["prior"]])):
    v=[]
    for r in sel:
        pr0 = r["prior"].get(r["target"])
        if not pr0: continue
        cut = sum(r["prior"].get(b,0.0) for b in r["exc"])
        if cut>=1: continue
        v.append((r["p"][r["target"]]/pr0)*(1-cut))
    print("  %s n=%d  (post/prior)x(1-cut) median %.3f  10/90 %.3f/%.3f" % (
        nm, len(v), statistics.median(v), sorted(v)[len(v)//10], sorted(v)[9*len(v)//10]))

print("\n=== T5  the discriminator candidate: how far above the running high, x outcome")
for lo,hi,nm in ((-99,0,"target at/below running high"),(0,1,"+0~1C"),(1,2,"+1~2C"),
                 (2,3,"+2~3C"),(3,99,">=+3C")):
    sel=[r for r in rows if r["cen"].get(r["target"]) is not None
         and lo <= r["cen"][r["target"]]-r["rh"] < hi]
    if not sel: continue
    print("  %-28s n=%-3d model %.1f%%  ask %.1f%%  actual %.1f%%  s=0 %.0f%%" % (
        nm, len(sel), 100*statistics.mean(r["p_t"] for r in sel),
        100*statistics.mean(r["ask"] for r in sel),
        100*sum(r["won"] for r in sel)/len(sel),
        100*sum(1 for r in sel if r["s"]==0)/len(sel)))
json.dump([{**{kk:(list(vv) if isinstance(vv,set) else vv) for kk,vv in r.items() if kk!="k"},
            "k":list(r["k"])} for r in rows], open("tmp/refute_h3_cases.json","w"))
