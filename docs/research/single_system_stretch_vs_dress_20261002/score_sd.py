#!/usr/bin/env python3
"""Score PROTOCOL.md: charter 240 verdicts for dress / beta / dress_beta against base.  Writes results.json."""
from __future__ import annotations

import gzip
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ARMS = ("base", "dress", "beta", "dress_beta")
IDX = {"crps": 0, "nll": 1, "brier": 2}
BLOCK, BOOT, SEED = 3, 2000, 20261002
MODE_FLOOR = 0.02


def load():
    rows = []
    for path in sorted((HERE / "pass1_sd").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            rows.extend(json.loads(line) for line in handle)
    return rows


def compare(rows, arm, base):
    if not rows:
        return None
    copies = Counter((r["station"], r["event_date"]) for r in rows)
    w = np.array([1.0 / copies[(r["station"], r["event_date"])] for r in rows])
    days = sorted({r["event_date"] for r in rows})
    idx_by_day = defaultdict(list)
    for i, r in enumerate(rows):
        idx_by_day[r["event_date"]].append(i)
    d = {m: np.array([r[arm]["served"][k] - r[base]["served"][k] for r in rows]) for m, k in IDX.items()}
    lvl = {m: float((w * np.array([r[base]["served"][k] for r in rows])).sum() / w.sum()) for m, k in IDX.items()}
    rng = np.random.default_rng(SEED)
    n_blocks = math.ceil(len(days) / BLOCK)
    boots = {m: [] for m in IDX}
    for _ in range(BOOT):
        idx = np.concatenate([idx_by_day[days[(s + j) % len(days)]]
                              for s in rng.integers(0, len(days), n_blocks) for j in range(BLOCK)])
        ww = w[idx]
        for m in IDX:
            boots[m].append(float((ww * d[m][idx]).sum() / ww.sum()))
    out = {"updates": len(rows), "station_days": len(copies), "event_days": len(days), "base_level": lvl}
    for m in IDX:
        out[f"d_{m}"] = [float((w * d[m]).sum() / w.sum()), float(np.percentile(boots[m], 2.5)), float(np.percentile(boots[m], 97.5))]
    out["primary"] = out["d_nll"][2] < 0
    out["crps_not_worse"] = out["d_crps"][1] <= 0
    out["brier_not_worse"] = out["d_brier"][1] <= 0
    out["verdict_240"] = bool(out["primary"] and out["crps_not_worse"] and out["brier_not_worse"])
    return out


def modes(vec):
    keys = sorted(vec, key=int)
    p = [vec[k] for k in keys]
    count = 0
    for i, v in enumerate(p):
        left = p[i - 1] if i > 0 else 0.0
        right = p[i + 1] if i + 1 < len(p) else 0.0
        if v >= MODE_FLOOR and v > left and v >= right:
            count += 1
    return count


def main():
    rows = load()
    by_day = defaultdict(list)
    for r in rows:
        by_day[(r["station"], r["event_date"])].append(r)
    roots = []
    for (_s, day), recs in by_day.items():
        midnight = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(recs[0]["tz"]))
        before = [r for r in recs if midnight - timedelta(days=1) <= datetime.fromisoformat(r["at"].replace("Z", "+00:00")) < midnight]
        if before:
            roots.append(max(before, key=lambda r: r["at"]))
    hi = [r for r in rows if any(f["ratio"] >= 1.5 for f in r["fit"].values())]
    lo = [r for r in rows if all(0 <= f["ratio"] < 1.5 for f in r["fit"].values())]
    seg = {
        "all": rows,
        "recent_0914_1001": [r for r in rows if r["event_date"] >= "2026-09-14"],
        "early_0720_0913": [r for r in rows if r["event_date"] <= "2026-09-13"],
        "day_ahead_roots": roots,
        "high_spread_dM_ge_1p5": hi,
        "normal_spread_dM_lt_1p5": lo,
        "high_spread_recent": [r for r in hi if r["event_date"] >= "2026-09-14"],
    }
    res = {"protocol": "PROTOCOL.md", "updates": len(rows), "stations": len({r["station"] for r in rows}),
           "event_days": [min(r["event_date"] for r in rows), max(r["event_date"] for r in rows)],
           "vs_base": {a: {k: compare(v, a, "base") for k, v in seg.items()} for a in ARMS[1:]}}
    for a in ARMS[1:]:
        res["vs_base"][a]["adopt"] = bool(res["vs_base"][a]["all"]["verdict_240"] and res["vs_base"][a]["recent_0914_1001"]["verdict_240"])
    fits = [f for r in rows for f in r["fit"].values()]
    hi_fits = [f for f in fits if f["ratio"] >= 1.5]
    res["single_system_multimodal_share"] = {a: {"all": float(np.mean([f[f"{a}_modes"] >= 2 for f in fits])),
                                                 "dM_ge_1p5": float(np.mean([f[f"{a}_modes"] >= 2 for f in hi_fits]))} for a in ARMS}
    res["pooled_multimodal_share"] = {a: float(np.mean([modes(r[a]["vec"]) >= 2 for r in rows])) for a in ARMS}
    res["fits"] = {"n_system_rows": len(fits), "share_dM_ge_1p5": len(hi_fits) / len(fits),
                   "share_updates_any_dM_ge_1p5": len(hi) / len(rows),
                   "rule_216_share": float(np.mean([f["rule_216"] for f in fits])),
                   **{k: [float(np.percentile([f[k] for f in fits if f["ratio"] >= 0], q)) for q in (10, 50, 90)] for k in ("ratio", "s", "beta_s", "dress_bw")}}
    (HERE / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    f = lambda v: "%+.5f [%+.5f,%+.5f]" % tuple(v)
    print("updates", res["updates"], "stations", res["stations"], "days", res["event_days"])
    print("fits", {k: (v if not isinstance(v, list) else [round(x, 3) for x in v]) for k, v in res["fits"].items()})
    print("single-system >=2 modes", res["single_system_multimodal_share"])
    print("pooled >=2 modes", res["pooled_multimodal_share"])
    for a in ARMS[1:]:
        print("==", a, "vs base  adopt:", res["vs_base"][a]["adopt"])
        for k, c in res["vs_base"][a].items():
            if isinstance(c, dict):
                print(f"  {k:24s} sd={c['station_days']:5d} d={c['event_days']:2d}  dLog {f(c['d_nll'])}  dCRPS {f(c['d_crps'])}  dBrier {f(c['d_brier'])}  -> {'PASS' if c['verdict_240'] else 'FAIL'}")


if __name__ == "__main__":
    main()
