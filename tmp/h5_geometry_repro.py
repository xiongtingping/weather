"""H5 step 0: reproduce the 41/23/21/19 edge blocks under the stage-aware rule."""
import json, pickle, sys
from bisect import bisect_right
sys.path.insert(0, "tmp")
from stage_pick import local_close, parse

C = pickle.load(open("tmp/h5_cache.pkl", "rb"))
pos, truth, tz, chains, books = (C["pos"], C["truth"], C["tz"], C["chains"], C["books"])
RANK = {"P1": 2, "P0-M": 1, "P0": 0}

def asks_at(k, when):
    h = books.get(k)
    if not h:
        return {}
    i = bisect_right([x[0] for x in h], when)
    return h[i-1][1] if i else {}

def build(alive_thresh, require_stage=None):
    rows = []
    for k, e in pos.items():
        if k not in truth or k not in chains or k not in tz:
            continue
        close = local_close(k[1], tz[k])
        best = None
        for stage, at, env in chains[k]:
            if at < e["opened"] or parse(at) >= close:
                continue
            if alive_thresh is not None and sum(
                    v for b, v in env["probabilities"].items()
                    if b in e["held"]) <= alive_thresh:
                continue
            r = RANK.get(stage, 0)
            if best is None or (r, at) >= (best[0], best[1]):
                best = (r, at, stage, env)
        if best is None:
            continue
        _, at, stage, env = best
        p, o = env["probabilities"], env["bucket_ordinals"]
        tb = truth[k][0]
        outside = {b: v for b, v in p.items() if b not in e["held"]}
        if not outside or tb not in o:
            continue
        target = max(outside, key=outside.get)
        ask = asks_at(k, at).get(target)
        if ask is None:
            continue
        rows.append({"k": k, "at": at, "stage": stage, "p": outside[target],
                     "ask": ask, "edge": outside[target]-ask,
                     "won": target == tb})
    return rows

for th in (None, 0.0, 0.05):
    rows = build(th)
    print("\nalive_thresh=%s  n=%d  stages=%s" % (
        th, len(rows), {s: sum(1 for r in rows if r["stage"] == s)
                        for s in ("P1", "P0-M", "P0")}))
    for name, sel in (("<=0", [r for r in rows if r["edge"] <= 0]),
                      ("0-0.10", [r for r in rows if 0 < r["edge"] <= .10]),
                      ("0.10-0.25", [r for r in rows if .10 < r["edge"] <= .25]),
                      (">0.25", [r for r in rows if r["edge"] > .25])):
        if sel:
            print("  %-10s n=%-3d model %.0f%% market %.0f%% actual %.0f%%" % (
                name, len(sel), 100*sum(r["p"] for r in sel)/len(sel),
                100*sum(r["ask"] for r in sel)/len(sel),
                100*sum(r["won"] for r in sel)/len(sel)))
