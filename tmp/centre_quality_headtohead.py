"""Head-to-head: which centre is closer to truth, overall and on the
subset where the two disagree.  Plus the significance of the shoulder gap.
"""
import importlib.util, json, math, os, sqlite3
from collections import Counter

ROOT = "/Users/xiong/weather"
spec = importlib.util.spec_from_file_location(
    "ptl", os.path.join(ROOT, "scripts/paper_trading_ledger.py"))
ptl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ptl)
conn = sqlite3.connect(
    "file:%s/data/bayes_basket_target_ssot.db?mode=ro" % ROOT, uri=True,
    timeout=60)
conn.row_factory = sqlite3.Row
truth = ptl._truth(conn)
rows = conn.execute(
    "SELECT station_id, event_date, stage, decision_at_utc,"
    " ladder_center_bucket FROM paper_trade_position").fetchall()
R = []
for r in rows:
    t = truth.get((r["station_id"], r["event_date"]))
    if t is None: continue
    env = conn.execute(
        "SELECT lineage_json FROM basket_probability_envelope WHERE"
        " station_id=? AND event_date=? AND stage=? AND decision_at_utc=?",
        (r["station_id"], r["event_date"], r["stage"],
         r["decision_at_utc"])).fetchone()
    if env is None: continue
    lin = json.loads(env["lineage_json"] or "{}")
    mcb = str(lin.get("model_center_bucket") or "")
    lad = ptl._settlement_ladder(conn, r["station_id"], r["event_date"],
                                 r["stage"], r["decision_at_utc"])
    if not lad: continue
    landed = ptl._settled_bucket(list(lad), float(t.tmax_c))
    if landed is None: continue
    ords = {str(b): int(o) for b, o, _l, _u, _p in lad}
    c = str(r["ladder_center_bucket"])
    if c not in ords or mcb not in ords: continue
    R.append((ords[c], ords[mcb], landed[1]))

def stats(name, sel):
    if not sel: return
    dl = [s - c for c, m, s in sel]; dm = [s - m for c, m, s in sel]
    f = lambda x: (sum(abs(v) for v in x)/len(x), sum(x)/len(x),
                   sum(1 for v in x if v == 0)/len(x),
                   sum(1 for v in x if abs(v) <= 1)/len(x))
    a = f(dl); b = f(dm)
    print("%-34s n=%3d | ladder mean|d| %.2f signed %+.2f d0 %4.1f%% |d|<=1"
          " %4.1f%% | model mean|d| %.2f signed %+.2f d0 %4.1f%% |d|<=1 %4.1f%%"
          % (name, len(sel), a[0], a[1], 100*a[2], 100*a[3],
             b[0], b[1], 100*b[2], 100*b[3]))

stats("all decisions", R)
stats("offset == 0 (centres agree)", [x for x in R if x[0] == x[1]])
stats("offset != 0 (centres disagree)", [x for x in R if x[0] != x[1]])
stats("offset == +1 (ladder above model)",
      [x for x in R if x[0] - x[1] == 1])
stats("offset == -1 (ladder below model)",
      [x for x in R if x[0] - x[1] == -1])
off = Counter(c - m for c, m, s in R)
print("\noffset distribution on the settled subset:",
      dict(sorted(off.items())))
mean_off = sum(c - m for c, m, s in R)/len(R)
print("mean signed offset %+.3f bucket, |offset|>=1 in %.1f%% of decisions"
      % (mean_off, 100*sum(1 for c,m,s in R if c!=m)/len(R)))

# Poisson tail for 3 observed vs 17.7 expected shoulder-leg hits
lam = 17.7
p = sum(math.exp(-lam)*lam**k/math.factorial(k) for k in range(4))
print("\nP(<=3 shoulder-leg hits | expected 17.7) = %.2e" % p)
