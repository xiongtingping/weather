"""Counterfactual: renormalise away mass strictly above the running-high
bucket once the decision is past a local-hour cut, and re-derive the
out-of-window argmax target and its edge."""
import json, sqlite3, statistics, sys
sys.path.insert(0, "tmp")
from stage_pick import local_close, parse

rows = json.load(open("tmp/h1_edge_targets_rows.json"))
conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",
                       uri=True, timeout=120)
pos = {}
for a, b, w, l in conn.execute(
    "SELECT station_id,event_date,window_buckets_json,leg_shares_json"
    " FROM paper_trade_position ORDER BY decision_at_utc"):
    k = (str(a), str(b))
    legs = json.loads(l or "{}")
    pos.setdefault(k, set()).update(
        [x for x, s in legs.items() if (s or 0) > 0] or json.loads(w))
truth = {(str(a), str(b)): str(c) for a, b, c in conn.execute(
    "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}

hi = [r for r in rows if r["edge"] > .25]
print("edge>0.25 n=%d" % len(hi))
print("  target strictly above the measured-high bucket: %d/%d"
      % (sum(r["above_ceiling"] for r in hi), len(hi)))
print("  decision after the day's peak was already reached: 94%% (table A)")
print("  mean mass sitting above the ceiling in those beliefs: %.3f"
      % statistics.mean(r["mass_above_ceiling"] for r in hi))
odd = [r for r in hi if not r["above_ceiling"]]
for r in odd:
    print("  NOT above ceiling:", r["station"], r["date"],
          "target", r["target"], "rh_c", r["rh_c"], "local %.1fh" % r["local_hour"],
          "edge %.2f" % r["edge"], "won", r["won"])

for CUT in (16, 17, 18, 19):
    surv = []
    for r in rows:
        if r["local_hour"] < CUT or r["ceil_ord"] is None:
            surv.append(r); continue
        # zero above-ceiling mass -> the out-of-window argmax must come from
        # buckets at or below the ceiling; those are almost all hard-excluded
        # already, so the target either vanishes or collapses in claim
        if r["above_ceiling"]:
            continue          # this claim would not exist
        surv.append(r)
    n_hi = sum(1 for r in surv if r["edge"] > .25)
    print("cut local %02d:00 -> edge>0.25 survivors %2d (was 19); "
          "all targets left %3d; their hit rate %.1f%%"
          % (CUT, n_hi, len(surv),
             100 * sum(r["won"] for r in surv) / len(surv)))

print("\n=== D. above-ceiling vs at-or-below-ceiling targets, split by clock ===")
print(f"{'':<26}{'n':>4}{'claim':>8}{'ask':>7}{'hit%':>7}")
for lab, sel in (
    ("late(>=17) above ceiling", [r for r in rows if r["local_hour"] >= 17 and r["above_ceiling"]]),
    ("late(>=17) at/below ceil", [r for r in rows if r["local_hour"] >= 17 and r["ceil_ord"] is not None and not r["above_ceiling"]]),
    ("early(<17) above ceiling", [r for r in rows if r["local_hour"] < 17 and r["above_ceiling"]]),
    ("early(<17) at/below ceil", [r for r in rows if r["local_hour"] < 17 and r["ceil_ord"] is not None and not r["above_ceiling"]]),
):
    if not sel:
        print(f"{lab:<26}{0:>4}"); continue
    print(f"{lab:<26}{len(sel):>4}{statistics.mean(r['p_t'] for r in sel):>8.3f}"
          f"{statistics.mean(r['ask'] for r in sel):>7.3f}"
          f"{100*sum(r['won'] for r in sel)/len(sel):>6.1f}%")
