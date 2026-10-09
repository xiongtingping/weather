#!/usr/bin/env python3
"""Reliability of every bucket vs the bucket actually bought, on the same served
vectors (charter 259 reading's own resolvers: stage_authority's served belief at
the intent instant, 224 floor; the market's two-sided normalised mid).  Read-only."""
import json, sqlite3, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent; ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import board_readings as br
from bayes_basket_target.label_knowability import knowable_labels

EDGES = [0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0 + 1e-9]
conn = sqlite3.connect(f"file:{br.DEFAULT_DB}?mode=ro", uri=True, timeout=60); conn.execute("PRAGMA query_only=ON")
legs, counts = br.traded_calibration_legs(conn)
labels = knowable_labels(conn)
vec_cache, mid_cache = {}, {}
moments = {}   # (station, day, created) -> (vector, mids, truth)
for l in legs:
    key = (l["station_id"], l["event_date"], l["created_at"])
    if key in moments:
        continue
    stage, vec = br._traded_served_vector(conn, *key, vec_cache)
    mids = br._traded_market_mid(conn, *key, mid_cache)
    moments[key] = (vec, mids, str(labels[(l["station_id"], l["event_date"])]["bucket"]))
conn.close()

def table(pairs):
    """pairs: (p, q, y, cluster)"""
    out = []
    a = np.array([(p, q, y) for p, q, y, _c in pairs]) if pairs else np.zeros((0, 3))
    for lo, hi in zip(EDGES[:-1], EDGES[1:]):
        m = (a[:, 0] >= lo) & (a[:, 0] < hi) if len(a) else []
        if len(a) and m.sum():
            out.append({"bin": f"{lo:.2f}–{min(hi,1):.2f}", "n": int(m.sum()), "said": float(a[m, 0].mean()),
                        "mkt": float(a[m, 1].mean()), "landed": float(a[m, 2].mean())})
    if len(a):
        k = np.digitize(a[:, 0], EDGES) - 1
        N = len(a); ece = 0.0; rel = 0.0
        for j in set(k):
            m = k == j; ece += m.sum() * abs(a[m, 0].mean() - a[m, 2].mean()); rel += m.sum() * (a[m, 0].mean() - a[m, 2].mean()) ** 2
        km = np.digitize(a[:, 1], EDGES) - 1; ece_m = 0.0
        for j in set(km):
            m = km == j; ece_m += m.sum() * abs(a[m, 1].mean() - a[m, 2].mean())
        summary = {"n": N, "said": float(a[:, 0].mean()), "mkt": float(a[:, 1].mean()), "landed": float(a[:, 2].mean()),
                   "ECE_ours": ece / N, "ECE_mkt": ece_m / N, "REL_ours": rel / N}
    else:
        summary = {"n": 0}
    return {"summary": summary, "bins": out}

def all_buckets(keys, split=None):
    pairs = []
    for key in keys:
        vec, mids, truth = moments[key]
        if vec is None or mids is None:
            continue
        for b, p in vec.items():
            if b not in mids:
                continue
            if split == "p>q" and not p > mids[b]: continue
            if split == "p<=q" and p > mids[b]: continue
            pairs.append((p, mids[b], 1.0 if b == truth else 0.0, key[:2]))
    return pairs

result = {"counts": counts}
for pop, sel in (("filled", lambda l: l["filled"]), ("intended", lambda l: True)):
    chosen = [l for l in legs if sel(l)]
    keys = sorted({(l["station_id"], l["event_date"], l["created_at"]) for l in chosen})
    result[pop] = {
        "全部桶·同一批时刻": table(all_buckets(keys)),
        "全部桶·我方>市场": table(all_buckets(keys, "p>q")),
        "全部桶·我方≤市场": table(all_buckets(keys, "p<=q")),
        "买入的桶·按买入方向": table([(l["p"], l["q"], l["win"], (l["station_id"], l["event_date"])) for l in chosen]),
        "买入的桶·只YES": table([(l["p"], l["q"], l["win"], (l["station_id"], l["event_date"])) for l in chosen if l["side"] == "YES"]),
        "decision_instants": len(keys),
    }
(HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
for pop in ("filled", "intended"):
    print(f"######## {pop}  时刻 {result[pop]['decision_instants']}")
    for name, t in result[pop].items():
        if not isinstance(t, dict): continue
        s = t["summary"]
        if not s.get("n"): continue
        print(f"== {name}: n={s['n']} 平均 说{s['said']:.3f} 市场{s['mkt']:.3f} 中{s['landed']:.3f}  ECE 我方{s['ECE_ours']:.4f} 市场{s['ECE_mkt']:.4f}")
        for r in t["bins"]:
            print(f"   {r['bin']}  n={r['n']:5d}  我方说 {r['said']:.3f}  市场说 {r['mkt']:.3f}  实际中 {r['landed']:.3f}")
