#!/usr/bin/env python3
"""Score PROTOCOL.md: charter 240 verdicts for the kappa arms.  Writes results.json."""
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
ARMS = ("k100", "k075", "k050", "k025", "k000")
IDX = {"crps": 0, "nll": 1, "brier": 2}
BLOCK, BOOT, SEED = 3, 2000, 20261001


def load():
    rows = []
    for path in sorted((HERE / "pass1_c240").glob("*.jsonl.gz")):
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
    seg = {
        "all": rows,
        "select_0720_0913": [r for r in rows if r["event_date"] <= "2026-09-13"],
        "judge_0914_0930": [r for r in rows if r["event_date"] >= "2026-09-14"],
        "new_since_216_0924_0930": [r for r in rows if r["event_date"] >= "2026-09-24"],
        "day_ahead_roots": roots,
    }
    res = {"protocol": "PROTOCOL.md", "updates": len(rows), "stations": len({r["station"] for r in rows}),
           "q1_k050_vs_k100": {k: compare(v, "k050", "k100") for k, v in seg.items()},
           "grid_vs_k100": {a: {k: compare(v, a, "k100") for k, v in seg.items()} for a in ARMS[1:]},
           "grid_vs_k050": {a: {k: compare(v, a, "k050") for k, v in seg.items()} for a in ("k075", "k025", "k000")}}
    sel = seg["select_0720_0913"]
    copies = Counter((r["station"], r["event_date"]) for r in sel)
    means = {a: float(sum(r[a]["served"][1] / copies[(r["station"], r["event_date"])] for r in sel) / len(copies)) for a in ("k000", "k025", "k050", "k075")}
    chosen = min(means, key=means.get)
    res["q2"] = {"selection_logscore": means, "chosen": chosen,
                 "judged_vs_k050": None if chosen == "k050" else compare(seg["judge_0914_0930"], chosen, "k050"),
                 "verdict": "KEEP_0.5" if chosen == "k050" else ("SWITCH" if compare(seg["judge_0914_0930"], chosen, "k050")["verdict_240"] else "KEEP_0.5")}
    fits = [f for r in rows for f in r["fit"].values()]
    res["fits"] = {k: [float(np.percentile([f[k] for f in fits], q)) for q in (10, 50, 90)] for k in ("s_prod", "s_15", "delta_15")}
    (HERE / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    f = lambda v: "%+.5f [%+.5f,%+.5f]" % tuple(v)
    print("updates", res["updates"], "stations", res["stations"], "fits p10/50/90", {k: [round(x, 3) for x in v] for k, v in res["fits"].items()})
    for name, block in (("Q1 216(k050) vs moments(k100)", res["q1_k050_vs_k100"]),):
        print("==", name)
        for k, c in block.items():
            if c: print(f"  {k:26s} sd={c['station_days']:5d} d={c['event_days']:2d}  dLog {f(c['d_nll'])}  dCRPS {f(c['d_crps'])}  dBrier {f(c['d_brier'])}  -> {'PASS' if c['verdict_240'] else 'FAIL'}")
    for a, block in res["grid_vs_k100"].items():
        c = block["all"]; print(f"  grid {a} vs k100 all: dLog {f(c['d_nll'])} dCRPS {f(c['d_crps'])} dBrier {f(c['d_brier'])} -> {'PASS' if c['verdict_240'] else 'FAIL'}")
    print("== Q2 selection", res["q2"]["selection_logscore"], "chosen", res["q2"]["chosen"], "verdict", res["q2"]["verdict"])
    if res["q2"]["judged_vs_k050"]:
        c = res["q2"]["judged_vs_k050"]; print(f"  judged on 09-14..09-30 vs k050: dLog {f(c['d_nll'])} dCRPS {f(c['d_crps'])} dBrier {f(c['d_brier'])}")
    for a, block in res["grid_vs_k050"].items():
        c = block["judge_0914_0930"]; print(f"  {a} vs k050 judge segment: dLog {f(c['d_nll'])} dCRPS {f(c['d_crps'])} dBrier {f(c['d_brier'])} -> {'PASS' if c['verdict_240'] else 'FAIL'}")


if __name__ == "__main__":
    main()
