#!/usr/bin/env python3
"""Charter 279 follow-up (user 10-22 「那确定性预报是用六家还是十家好？」): which deterministic
roster's median, bias-corrected with the fleet bias, best shifts the served centre.
Rosters: six = charter 267's F roster (AIFS single, IFS HRES, GFS, AIGFS, GDPS, ICON global);
four = the relays 补记十一 read (AIFS single, IFS 9 km, UKMO relay, CMA); ten = charter 279's.
Walk-forward exactly as the daily fit (parameters on all earlier event days), scored on
decisions from 2026-10-01; compared on each roster's own coverage and on the decisions
where all three have >= 3 systems."""
import collections, json, os, pickle, statistics, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "scripts"))
from bayes_basket_target import deterministic_consensus_shift as DCS
import fit_deterministic_consensus_shift as FIT
HERE = os.path.dirname(os.path.abspath(__file__))
PKL = os.path.join(HERE, "consensus_roster_recs.pkl")
out = open(os.path.join(HERE, "consensus_roster.txt"), "w")
def P(*a): print(*a, flush=True); print(*a, file=out)
ROSTERS = {"six_267": ("AIFS", "IFS_HRES", "GFS", "AIGFS", "GDPS", "ICON_GLOBAL"),
           "four_relay": ("AIFS", "IFS9KM", "UKMO10", "CMA"),
           "ten_279": tuple(DCS.PRODUCTS)}
if os.path.exists(PKL) and "--refresh" not in sys.argv:
    recs = pickle.load(open(PKL, "rb"))
else:
    import sqlite3
    from datetime import datetime, timedelta, timezone
    real = DCS.consensus_read
    def keep(conn, **kw):
        r = real(conn, **kw); keep.last = r; return r
    FIT.DCS.consensus_read = keep
    conn = sqlite3.connect(f"file:{FIT.DB}?mode=ro", uri=True, timeout=120); conn.execute("PRAGMA query_only=ON")
    today = datetime.now(timezone.utc).date()
    # collect() reads each record's consensus right before appending it; wrap append to grab systems
    orig_collect = FIT.collect
    recs, counts = [], None
    class L(list):
        def append(self, rec):
            rec["systems"] = {k: v["day_max_c"] for k, v in (keep.last.get("systems") or {}).items()}
            super().append(rec)
    import types
    src_records = L()
    FIT_collect_code = FIT.collect.__code__
    def collect_wrapped(*a, **k):
        g = dict(FIT.collect.__globals__)
        return orig_collect(*a, **k)
    t0 = time.time()
    # re-implement the loop by monkeypatching list creation is brittle; call collect then re-read systems per record
    recs, counts = orig_collect(conn, start=(today - timedelta(days=60)).isoformat(), end=(today - timedelta(days=1)).isoformat(),
                                stations=None, budget_seconds=1500, started=t0)
    runs_cache = {}
    from bayes_basket_target.regional_point_lane import runs_for
    for r in recs:
        key = (r["station"], r["day"])
        if key not in runs_cache:
            runs_cache.clear(); runs_cache[key] = runs_for(conn, station_id=r["station"], event_date=r["day"], as_of_utc="9999-12-31T23:59:59Z")
        tz = conn.execute("SELECT settlement_timezone FROM basket_probability_envelope WHERE station_id=? AND event_date=? LIMIT 1", key).fetchone()[0]
        rd = real(None, station_id=r["station"], event_date=r["day"], as_of_utc=r["decision_at_utc"], settlement_timezone=tz, rows=runs_cache[key])
        r["systems"] = {k: v["day_max_c"] for k, v in (rd.get("systems") or {}).items()}
    conn.close(); pickle.dump(recs, open(PKL, "wb"))
    P(f"collected {len(recs)} records in {time.time()-t0:.0f}s")
for r in recs:
    r["F"] = {}
    for name, members in ROSTERS.items():
        vals = [r["systems"][p] for p in members if p in r["systems"]]
        r["F"][name] = statistics.median(vals) if len(vals) >= 3 else None
FROM = DCS.SCORE_FROM_DECISION_UTC
def run(name, family, subset):
    rs = [dict(r, f_c={"wide": r["F"][name]}) for r in recs if r["family"] == family and r["F"][name] is not None]
    wf = DCS.walk_forward(rs, family=family, score_from_utc=FROM)
    keys = subset
    # restrict scoring to the common subset by re-running on records whose scoring is allowed
    if keys is not None:
        rs2 = [dict(r, decision_at_utc=(r["decision_at_utc"] if (r["station"], r["day"]) in keys else "0000")) for r in rs]
        wf = DCS.walk_forward(rs2, family=family, score_from_utc=FROM)
    return wf["fleet_wide"]
for family in DCS.STAGE_FAMILIES:
    fam = [r for r in recs if r["family"] == family and r["decision_at_utc"] >= FROM]
    common = {(r["station"], r["day"]) for r in fam if all(r["F"][n] is not None for n in ROSTERS)}
    P(f"\n=== {family}: 10-01 起决策 {len(fam)}；三名单都 ≥3 家 {len(common)}；各名单可用 " +
      ", ".join(f"{n} {sum(1 for r in fam if r['F'][n] is not None)}" for n in ROSTERS) +
      "；在场家数中位 " + ", ".join(f"{n} {statistics.median([sum(1 for p in m if p in r['systems']) for r in fam])}" for n, m in ROSTERS.items()))
    for label, subset in (("各自覆盖", None), ("同一批决策", common)):
        for name in ROSTERS:
            w = run(name, family, subset)
            if not w.get("n"): P(f"   {label:8s} {name:10s} n=0"); continue
            ls = w["logscore"]
            P(f"   {label:8s} {name:10s} n={w['n']:4d} 天={w['n_days']:2d} ΔLS {ls['delta']:+.4f} [{ls['ci'][0]:+.4f},{ls['ci'][1]:+.4f}] ΔCRPS {w['crps']['delta']:+.4f} ΔBrier {w['brier']['delta']:+.4f} 改善天 {w['days_better']}/{w['n_days']} {w['verdict']}")
out.close()
