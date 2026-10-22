#!/usr/bin/env python3
"""User 10-22 「比较确定性模型和p0的真值命中率」: bucket hit rate of the deterministic forecasts
(each system, the ten-system median raw and bias-corrected) against P0's mode bucket and the
charter-279 shadow's mode, on the first decision of each settled station-day x stage family,
decisions from 2026-10-01 (records: consensus_roster_recs.pkl).  The bias and kappa are fitted
only on earlier event days."""
import collections, os, pickle, statistics, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from bayes_basket_target import deterministic_consensus_shift as DCS
HERE = os.path.dirname(os.path.abspath(__file__))
out = open(os.path.join(HERE, "det_vs_p0_hit.txt"), "w")
def P(*a): print(*a, flush=True); print(*a, file=out)
recs = pickle.load(open(os.path.join(HERE, "consensus_roster_recs.pkl"), "rb"))
FROM = DCS.SCORE_FROM_DECISION_UTC
def bucket(value, intervals):
    for b, (lo, hi) in intervals.items():
        if (lo is None or value >= lo) and (hi is None or value < hi): return b
    return None
NAMES = {"AIFS": "AIFS单成员", "IFS9KM": "IFS 9km", "UKMO10": "UKMO中继", "CMA": "CMA", "IFS_HRES": "IFS HRES",
         "GFS": "GFS", "AIGFS": "AIGFS", "GDPS": "GDPS", "ICON_GLOBAL": "ICON全球", "UKMO_GLOBAL": "UKMO原生"}
for family in DCS.STAGE_FAMILIES:
    fam = [r for r in recs if r["family"] == family]
    days = sorted({r["day"] for r in fam})
    hits = collections.defaultdict(list); bias_by_day = {}
    for day in days:
        prior = [r for r in fam if r["day"] < day]
        if len({r["day"] for r in prior}) < DCS.MIN_FIT_EVENT_DAYS: continue
        params = DCS.fit_parameters([dict(r, f_c={"wide": r["F"]["ten_279"]}) for r in prior if r["F"].get("ten_279") is not None], family=family) if False else None
        rs_prior = [dict(r, f_c={"wide": r.get("f_c", {}).get("wide")}) for r in prior if (r.get("f_c") or {}).get("wide") is not None]
        params = DCS.fit_parameters(rs_prior, family=family)
        sys_bias = {}
        for s in NAMES:
            e = [r["y_c"] - r["systems"][s] for r in prior if s in r.get("systems", {})]
            sys_bias[s] = statistics.mean(e) if len(e) >= 30 else None
        for r in (x for x in fam if x["day"] == day and x["decision_at_utc"] >= FROM):
            iv, win = r["intervals"], r["winner"]
            mode = max(r["ordered"], key=lambda k: r["p"][k])
            hits["P0 众数桶"].append(mode == win)
            f = (r.get("f_c") or {}).get("wide")
            if f is not None:
                hits["十家中位（原始）"].append(bucket(f, iv) == win)
                p = params.get("fleet_wide")
                if p:
                    fcal = f + p["bias_c"]
                    hits["十家中位（修偏）"].append(bucket(fcal, iv) == win)
                    delta = max(-DCS.CAP_C, min(DCS.CAP_C, p["kappa"] * (fcal - r["centre_c"])))
                    sh = DCS.shift_served(r["p"], r["ordered"], iv, delta, excluded=r.get("excluded") or (), censor=DCS.CENSOR_BY_FAMILY.get(family, False))
                    hits["影子（P0 挪后众数）"].append(max(r["ordered"], key=lambda k: sh[k]) == win)
                    hits["P0 众数桶（同批）"].append(mode == win)
            for s, nm in NAMES.items():
                v = r.get("systems", {}).get(s)
                if v is None: continue
                hits[f"  {nm} 原始"].append(bucket(v, iv) == win)
                if sys_bias.get(s) is not None:
                    hits[f"  {nm} 修偏"].append(bucket(v + sys_bias[s], iv) == win)
    n_days = len({r["day"] for r in fam if r["decision_at_utc"] >= FROM})
    P(f"\n=== {family}（10-01 起首决策，{n_days} 个事件日）")
    order = ["P0 众数桶", "影子（P0 挪后众数）", "P0 众数桶（同批）", "十家中位（原始）", "十家中位（修偏）"] + sorted(k for k in hits if k.startswith("  "))
    for k in order:
        v = hits.get(k)
        if not v: continue
        P(f"   {k:22s} 命中 {sum(v)/len(v):6.1%}  n={len(v)}")
out.close()
