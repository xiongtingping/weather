import json, pickle, sys
from bisect import bisect_right
sys.path.insert(0, "tmp")
from stage_pick import local_close, parse
C = pickle.load(open("tmp/h5_cache.pkl", "rb"))
pos, truth, tz, chains, books = (C["pos"], C["truth"], C["tz"], C["chains"], C["books"])
RANK = {"P1": 2, "P0-M": 1, "P0": 0}
def asks_at(k, when):
    h = books.get(k)
    if not h: return {}
    i = bisect_right([x[0] for x in h], when)
    return h[i-1][1] if i else {}
def build(th, since_open, incl_close):
    rows = []
    for k, e in pos.items():
        if k not in truth or k not in chains or k not in tz: continue
        close = local_close(k[1], tz[k]); best = None
        for stage, at, env in chains[k]:
            if since_open and at < e["opened"]: continue
            d = parse(at)
            if (d > close) if incl_close else (d >= close): continue
            if th is not None and sum(v for b, v in env["probabilities"].items()
                                      if b in e["held"]) <= th: continue
            r = RANK.get(stage, 0)
            if best is None or (r, at) >= (best[0], best[1]): best = (r, at, stage, env)
        if best is None: continue
        _, at, stage, env = best
        p, o = env["probabilities"], env["bucket_ordinals"]; tb = truth[k][0]
        outside = {b: v for b, v in p.items() if b not in e["held"]}
        if not outside or tb not in o: continue
        t = max(outside, key=outside.get); ask = asks_at(k, at).get(t)
        if ask is None: continue
        rows.append({"edge": outside[t]-ask})
    return rows
for th in (None, 0.0, 0.02, 0.05, 0.10):
    for so in (True, False):
        for ic in (False, True):
            rows = build(th, so, ic)
            c = [len([r for r in rows if r["edge"] <= 0]),
                 len([r for r in rows if 0 < r["edge"] <= .10]),
                 len([r for r in rows if .10 < r["edge"] <= .25]),
                 len([r for r in rows if r["edge"] > .25])]
            print("th=%-5s since_open=%-5s incl_close=%-5s -> %s  n=%d %s" % (
                th, so, ic, c, len(rows), "  <== MATCH" if c == [41,23,21,19] else ""))
