"""D: reliability on the buckets the ledger BOUGHT vs ALL buckets, same instants."""
import sqlite3, json, math, sys
from collections import defaultdict

DB = "file:data/bayes_basket_target_ssot.db?mode=ro"
c = sqlite3.connect(DB, uri=True, timeout=60)

rows = c.execute(
    "SELECT p.position_id, p.station_id, p.event_date, p.stage,"
    "       p.decision_at_utc, p.ladder_center_bucket, p.window_buckets_json,"
    "       p.posted_buckets_json, p.leg_shares_json, p.probabilities_json,"
    "       s.settled_bucket"
    " FROM paper_trade_position p"
    " JOIN paper_trade_settlement s"
    "   ON s.station_id=p.station_id AND s.event_date=p.event_date"
).fetchall()

bought = []   # (p, y, shares, meta)
allobs = []
per_row = 0
for (pid, st, ed, stage, dat, lcb, winj, postj, legj, probj, settled) in rows:
    probs = json.loads(probj or "{}") or {}
    legs = json.loads(legj or "{}") or {}
    if not probs:
        continue
    per_row += 1
    for b, p in probs.items():
        y = 1 if b == settled else 0
        allobs.append((float(p), y, b, pid, st, ed, stage, lcb, winj, postj))
    for b, q in legs.items():
        q = float(q)
        if q <= 0:
            continue
        if b not in probs:
            print("MISSING PROB", pid, b); continue
        y = 1 if b == settled else 0
        bought.append((float(probs[b]), y, q, b, pid, st, ed, stage, lcb, winj, postj))

print("settled station-days:", len({(r[1], r[2]) for r in rows}))
print("position rows used:", per_row)
print("bought (row,bucket) obs:", len(bought), " all (row,bucket) obs:", len(allobs))

EDGES = [0.0, 0.02, 0.05, 0.10, 0.20, 0.35, 0.50, 0.70, 1.01]
def binof(p):
    for i in range(len(EDGES)-1):
        if EDGES[i] <= p < EDGES[i+1]:
            return i
    return len(EDGES)-2

def table(obs, label, getp=lambda o: o[0], gety=lambda o: o[1]):
    agg = defaultdict(lambda: [0, 0, 0.0])
    for o in obs:
        p, y = getp(o), gety(o)
        a = agg[binof(p)]
        a[0] += 1; a[1] += y; a[2] += p
    print("\n== %s ==" % label)
    print("%-14s %6s %8s %9s %9s" % ("claimed bin", "n", "claimed", "realised", "gap"))
    tn = th = 0; tp = 0.0
    for i in range(len(EDGES)-1):
        n, h, sp = agg[i]
        if not n: continue
        tn += n; th += h; tp += sp
        print("%-14s %6d %8.3f %9.3f %+9.3f" % (
            "[%.2f,%.2f)" % (EDGES[i], EDGES[i+1]), n, sp/n, h/n, h/n - sp/n))
    print("%-14s %6d %8.3f %9.3f %+9.3f" % ("TOTAL", tn, tp/tn, th/tn, th/tn - tp/tn))
    return tn, th, tp

table(allobs, "ALL buckets at the same instants (n=%d)" % len(allobs))
table(bought, "BOUGHT buckets only (n=%d)" % len(bought))

boughtkeys = {(o[4], o[3]) for o in bought}  # (pid, bucket)
notbought = [o for o in allobs if (o[3], o[2]) not in boughtkeys]
table(notbought, "NOT-bought buckets at the same instants (n=%d)" % len(notbought))

# share-weighted bought
agg = defaultdict(lambda: [0.0, 0.0, 0.0])
for p, y, q, *_ in bought:
    a = agg[binof(p)]
    a[0] += q; a[1] += y*q; a[2] += p*q
print("\n== BOUGHT, share-weighted ==")
for i in range(len(EDGES)-1):
    w, h, sp = agg[i]
    if not w: continue
    print("%-14s w=%9.1f claimed %.3f realised %.3f gap %+.3f" % (
        "[%.2f,%.2f)" % (EDGES[i], EDGES[i+1]), w, sp/w, h/w, h/w-sp/w))

# ratio summary
def summ(obs, lab):
    n=len(obs); cl=sum(o[0] for o in obs)/n; re=sum(o[1] for o in obs)/n
    print("%-12s n=%5d claimed=%.4f realised=%.4f  realised/claimed=%.3f" % (lab,n,cl,re,re/cl if cl else float('nan')))
print()
summ(allobs, "ALL"); summ(bought, "BOUGHT"); summ(notbought, "NOT-BOUGHT")
