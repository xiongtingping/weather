"""Round 34 scorer -- rest a YES bid on the forecast bucket five minutes after
listing, exactly the protocol committed in README (2c67a166).
Fill (conservative): some trade in (T0, T0+W] at a YES-equivalent price
strictly below X (YES at p -> p, NO at p -> 1-p); optimistic: <= X.
Per filled share: hit - X; unfilled: 0.  ROI = sum / cost; day-clustered 95%.
"""
import collections
import json
import statistics
import sys
from pathlib import Path

import numpy as np

B = Path(sys.argv[1])
rows = json.load(open(B / "r34_rows.json"))
tapes = json.load(open(B / "r34_tapes.json"))
print(f"station-days {len(rows)} (event days {min(r['ed'] for r in rows)}..{max(r['ed'] for r in rows)})")


def yes_prices(cid, lo, hi):
    return [x["price"] if x["outcome"] == "Yes" else 1.0 - x["price"] for x in tapes.get(cid, []) if lo < x["t"] <= hi]


def run(leg, X, W, strict):
    book = collections.defaultdict(list)
    n_all = n_fill = hits_all = hits_fill = 0
    for r in rows:
        lab = r["legs"][leg]
        cid = r["cid"].get(lab)
        if not cid:
            continue
        ps = yes_prices(cid, r["T0"], r["T0"] + W * 3600)
        hit = r["truth"] == lab
        n_all += 1
        hits_all += hit
        filled = any(p < X for p in ps) if strict else any(p <= X for p in ps)
        if filled:
            n_fill += 1
            hits_fill += hit
            book[r["ed"]].append(((1.0 if hit else 0.0) - X, X))
    if not book:
        return None
    keys = list(book)
    S = np.array([[sum(p for p, c in book[k]), sum(c for p, c in book[k])] for k in keys])
    rng = np.random.default_rng(34)
    idx = rng.integers(0, len(keys), size=(2000, len(keys)))
    T = S[idx].sum(axis=1)
    bs = np.sort(T[:, 0] / T[:, 1])
    return {"n": n_all, "fill": n_fill / n_all, "hit_all": hits_all / n_all, "hit_fill": hits_fill / max(1, n_fill),
            "roi": 100 * S[:, 0].sum() / S[:, 1].sum(), "lo": 100 * bs[50], "hi": 100 * bs[1949], "days": len(keys),
            "per_share": 100 * (hits_fill / max(1, n_fill) - X)}


print("\n== PRIMARY: YES@F, X=0.35, W=2h, conservative fill")
p = run("F", 0.35, 2, True)
print(f"   legs {p['n']}  fill rate {p['fill']:.2f}  hit (all legs) {p['hit_all']:.3f}  hit (filled) {p['hit_fill']:.3f}  per filled share {p['per_share']:+.1f} c"
      f"  ROI {p['roi']:+.1f}% [{p['lo']:+.1f}, {p['hi']:+.1f}] ({p['days']} days)  -> {'YES' if p['lo'] > 0 else 'no'}")
print("\n== secondary grid (leg, W, fill rule): fill / hit all / hit filled / per share / ROI [95%]")
for leg in ("F", "C", "F+1"):
    for W in (2, 12):
        for strict in (True, False):
            cells = []
            for X in (0.25, 0.30, 0.35, 0.40):
                s = run(leg, X, W, strict)
                cells.append("-" if not s else f"X{X:.2f}: {s['fill']:.2f}/{s['hit_all']:.2f}/{s['hit_fill']:.2f}/{s['per_share']:+.0f}c/{s['roi']:+.0f}% [{s['lo']:+.0f},{s['hi']:+.0f}]")
            print(f"   {leg:3s} W={W:2d}h {'conservative' if strict else 'optimistic  '}  " + "   ".join(cells))

# post-hoc robustness of the primary cell (not in the protocol)
CL = {"0x0e0e8174", "0xf4d89f7e", "0x6fdd2bda", "0x7076cb96", "0x5bae7577", "0x7d640ebd", "0x30200b89", "0x8fbd7cf5"}


def run2(leg, X, W, min_shares=0.0, drop_cluster=False, days=None):
    book = collections.defaultdict(list)
    for r in rows:
        if days and not days(r["ed"]):
            continue
        lab = r["legs"][leg]
        cid = r["cid"].get(lab)
        if not cid:
            continue
        sub = [x for x in tapes.get(cid, []) if r["T0"] < x["t"] <= r["T0"] + W * 3600 and not (drop_cluster and x["w"].lower() in CL)]
        below = [x for x in sub if (x["price"] if x["outcome"] == "Yes" else 1 - x["price"]) < X]
        if not below or sum(x["size"] for x in below) < min_shares:
            continue
        book[r["ed"]].append(((1.0 if r["truth"] == lab else 0.0) - X, X))
    keys = list(book)
    S = np.array([[sum(p for p, c in book[k]), sum(c for p, c in book[k])] for k in keys])
    rng = np.random.default_rng(341)
    idx = rng.integers(0, len(keys), size=(2000, len(keys)))
    T = S[idx].sum(axis=1)
    bs = np.sort(T[:, 0] / T[:, 1])
    per_day = sorted((k, 100 * S[i, 0] / S[i, 1]) for i, k in enumerate(keys))
    return sum(len(v) for v in book.values()), 100 * S[:, 0].sum() / S[:, 1].sum(), 100 * bs[50], 100 * bs[1949], per_day


print("\n== post-hoc robustness of the primary cell (YES@F, X=0.35, W=2h, trade strictly below X)")
for name, kw in (("as primary", {}), (">=5 shares traded below X", {"min_shares": 5.0}), (">=20 shares traded below X", {"min_shares": 20.0}),
                 ("cluster's own trades removed", {"drop_cluster": True}),
                 ("event days <= 10-04", {"days": lambda d: d <= "2026-10-04"}), ("event days >= 10-05", {"days": lambda d: d >= "2026-10-05"})):
    n, roi, lo, hi, pdays = run2("F", 0.35, 2, **kw)
    print(f"   {name:30s} fills {n:3d}  ROI {roi:+.1f}% [{lo:+.1f}, {hi:+.1f}]  days positive {sum(1 for d, v in pdays if v > 0)}/{len(pdays)}")
n, roi, lo, hi, pdays = run2("F", 0.35, 2)
print("   per event day ROI: " + "  ".join(f"{d[5:]} {v:+.0f}%" for d, v in pdays))
for X in (0.25, 0.30):
    for name, kw in (("as primary", {}), (">=5 shares", {"min_shares": 5.0}), ("cluster removed", {"drop_cluster": True}),
                     ("<= 10-04", {"days": lambda d: d <= "2026-10-04"}), (">= 10-05", {"days": lambda d: d >= "2026-10-05"})):
        n, roi, lo, hi, pdays = run2("F", X, 2, **kw)
        print(f"   X={X:.2f} {name:18s} fills {n:3d}  ROI {roi:+.1f}% [{lo:+.1f}, {hi:+.1f}]  days positive {sum(1 for d, v in pdays if v > 0)}/{len(pdays)}")
