"""Which side does the seat extend to, and which side does truth land on?"""
import importlib.util, json, os, sqlite3
from collections import Counter, defaultdict

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
    " ladder_center_bucket, window_buckets_json, leg_shares_json"
    " FROM paper_trade_position").fetchall()

recs = []
for r in rows:
    t = truth.get((r["station_id"], r["event_date"]))
    if t is None: continue
    lad = ptl._settlement_ladder(conn, r["station_id"], r["event_date"],
                                 r["stage"], r["decision_at_utc"])
    if not lad: continue
    landed = ptl._settled_bucket(list(lad), float(t.tmax_c))
    if landed is None: continue
    sb, so = landed
    ords = {str(b): int(o) for b, o, _l, _u, _p in lad}
    centre = str(r["ladder_center_bucket"])
    if centre not in ords: continue
    window = [str(b) for b in json.loads(r["window_buckets_json"] or "[]")]
    legs = {b for b, q in (json.loads(r["leg_shares_json"] or "{}") or {}
                           ).items() if float(q) > 0}
    sh = [b for b in legs if ptl._is_shoulder(b, centre, window)]
    recs.append(dict(d=so - ords[centre], sh_sides={
        ("above" if ords[b] > ords[centre] else "below") for b in sh},
        n_sh=len(sh)))

sub = [r for r in recs if r["n_sh"] > 0]
print("decisions with >=1 shoulder leg bought: %d of %d" % (len(sub), len(recs)))
for name, group in (("ALL decisions", recs),
                    ("decisions that bought a shoulder", sub)):
    c = Counter(r["d"] for r in group)
    n = len(group)
    print("\n%s (n=%d): d=-1 %.1f%%  d=0 %.1f%%  d=+1 %.1f%%  |d|=1 %.1f%%"
          % (name, n, 100*c.get(-1,0)/n, 100*c.get(0,0)/n,
             100*c.get(1,0)/n, 100*(c.get(-1,0)+c.get(1,0))/n))

print("\n=== among seats that bought EXACTLY ONE shoulder, side bought vs"
      " truth side")
one = [r for r in sub if r["n_sh"] == 1]
tab = Counter()
for r in one:
    side = next(iter(r["sh_sides"]))
    tside = "above" if r["d"] > 0 else ("below" if r["d"] < 0 else "centre")
    tab[(side, tside)] += 1
for k in sorted(tab): print("  bought %-5s truth %-6s : %d" % (k[0], k[1], tab[k]))
print("  n =", len(one))

# expected vs observed shoulder-leg hits under independence
c = Counter(r["d"] for r in recs); n = len(recs)
p_up, p_dn = c.get(1,0)/n, c.get(-1,0)/n
legs_up = sum(1 for r in sub for s in r["sh_sides"] if s == "above")
legs_dn = sum(1 for r in sub for s in r["sh_sides"] if s == "below")
print("\nexpected shoulder-leg hits if side choice were independent of truth:"
      " %.1f  (above %d x %.3f + below %d x %.3f)"
      % (legs_up*p_up + legs_dn*p_dn, legs_up, p_up, legs_dn, p_dn))
