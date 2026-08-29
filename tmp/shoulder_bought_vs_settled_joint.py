"""Given the settled bucket sits at distance d from the ladder centre,
did the seat actually BUY that bucket?  This is the joint the two
marginals hide.
"""
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

tab = defaultdict(lambda: Counter())
wsize = Counter()
shoulders_bought = Counter()
centre_on_edge = Counter()
for r in rows:
    t = truth.get((r["station_id"], r["event_date"]))
    if t is None:
        continue
    lad = ptl._settlement_ladder(conn, r["station_id"], r["event_date"],
                                 r["stage"], r["decision_at_utc"])
    if not lad:
        continue
    landed = ptl._settled_bucket(list(lad), float(t.tmax_c))
    if landed is None:
        continue
    sb, so = landed
    ords = {str(b): int(o) for b, o, _l, _u, _p in lad}
    centre = str(r["ladder_center_bucket"])
    if centre not in ords:
        continue
    window = [str(b) for b in json.loads(r["window_buckets_json"] or "[]")]
    legs = {b: float(q) for b, q in
            (json.loads(r["leg_shares_json"] or "{}") or {}).items()
            if float(q) > 0}
    d = so - ords[centre]
    bought = "bought" if sb in legs else (
        "seated_unfilled" if sb in window else "not_seated")
    tab[d][bought] += 1
    wsize[len(window)] += 1
    # how many of the two shoulder buckets did this seat buy?
    sh = [b for b in legs
          if ptl._is_shoulder(b, centre, window)]
    shoulders_bought[len(sh)] += 1
    if window:
        centre_on_edge["endpoint" if centre in (window[0], window[-1])
                       else "interior"] += 1

print("=== settled distance from ladder centre  x  was that bucket bought?")
tot = sum(sum(v.values()) for v in tab.values())
print("  d      n   bought  seated_unfilled  not_seated")
for d in sorted(tab):
    v = tab[d]
    n = sum(v.values())
    print("  %+3d  %4d   %5d %12d %13d   (%.1f%% of all days)"
          % (d, n, v["bought"], v["seated_unfilled"], v["not_seated"],
             100.0 * n / tot))
print("\ntotal decisions scored:", tot)
print("window sizes:", dict(sorted(wsize.items())))
print("shoulder legs bought per seat:", dict(sorted(shoulders_bought.items())))
print("ladder centre position on posted window:", dict(centre_on_edge))
