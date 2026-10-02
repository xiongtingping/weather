#!/usr/bin/env python3
"""Score PROTOCOL_fit.md: charter 240 verdicts for the shape arms against base,
the pre-registered adoption rule, multimodality and tail diagnostics.  Writes
results_fit.json."""
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
ARMS = ("base", "dress", "fit_c0", "dress_fit", "shash")
IDX = {"crps": 0, "nll": 1, "brier": 2}
BLOCK, BOOT, SEED = 3, 2000, 20261002
MODE_FLOOR = 0.02
ARTIFACT_FREE_MAX_SHARE = 0.05


def load():
    rows = []
    for path in sorted((HERE / "pass1_fit").glob("*.jsonl.gz")):
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
    out["no_harm"] = bool(out["d_nll"][1] <= 0 and out["crps_not_worse"] and out["brier_not_worse"])
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


def pct(values, qs=(10, 50, 90)):
    return [float(np.percentile(values, q)) for q in qs]


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
        "early_0730_0913": [r for r in rows if r["event_date"] <= "2026-09-13"],
        "day_ahead_roots": roots,
        "high_spread_dM_ge_1p5": hi,
        "normal_spread_dM_lt_1p5": lo,
        "high_spread_recent": [r for r in hi if r["event_date"] >= "2026-09-14"],
    }
    res = {"protocol": "PROTOCOL_fit.md", "updates": len(rows), "stations": len({r["station"] for r in rows}),
           "event_days": [min(r["event_date"] for r in rows), max(r["event_date"] for r in rows)],
           "vs_base": {a: {k: compare(v, a, "base") for k, v in seg.items()} for a in ARMS[1:]}}
    fits = [f for r in rows for f in r["fit"].values()]
    hi_fits = [f for f in fits if f["ratio"] >= 1.5]
    res["single_system"] = {a: {
        "multimodal_share": float(np.mean([f[f"{a}_modes"] >= 2 for f in fits])),
        "multimodal_share_dM_ge_1p5": float(np.mean([f[f"{a}_modes"] >= 2 for f in hi_fits])),
        "tail_mass_predicted": float(np.mean([f[f"{a}_tail_mass"] for f in fits])),
        "tail_hit_realised": float(np.mean([f[f"{a}_tail_hit"] for f in fits])),
        "tail_mass_predicted_dM_ge_1p5": float(np.mean([f[f"{a}_tail_mass"] for f in hi_fits])),
        "tail_hit_realised_dM_ge_1p5": float(np.mean([f[f"{a}_tail_hit"] for f in hi_fits])),
        "vector_sd_mean": float(np.mean([f[f"{a}_vsd"] for f in fits])),
    } for a in ARMS}
    res["pooled_multimodal_share"] = {a: float(np.mean([modes(r[a]["vec"]) >= 2 for r in rows])) for a in ARMS}
    res["parameters_p10_50_90"] = {k: pct([f[k] for f in fits]) for k in (
        "s_mom", "base_s", "dress_c", "fit_c0_c", "fit_c0_raw_c", "fit_c0_s", "dress_fit_c", "dress_fit_raw_c", "dress_fit_s",
        "shash_s", "shash_eps", "shash_raw_eps", "shash_tau", "shash_raw_tau")}
    res["parameters_p10_50_90"]["fit_c0_raw_c_share_at_upper_bound"] = float(np.mean([f["fit_c0_raw_c"] >= 0.998 for f in fits]))
    res["parameters_p10_50_90"]["fit_c0_raw_c_share_at_zero"] = float(np.mean([f["fit_c0_raw_c"] <= 1e-6 for f in fits]))
    # Pre-registered adoption rule.
    rule = {}
    for a in ARMS[1:]:
        all_, rec = res["vs_base"][a]["all"], res["vs_base"][a]["recent_0914_1001"]
        rule[a] = {
            "artifact_free": res["single_system"][a]["multimodal_share"] <= ARTIFACT_FREE_MAX_SHARE,
            "no_harm_both": bool(all_["no_harm"] and rec["no_harm"]),
            "pass_240_both": bool(all_["verdict_240"] and rec["verdict_240"]),
            "d_nll_all": all_["d_nll"][0],
        }
    candidates = [a for a, v in rule.items() if v["artifact_free"] and v["no_harm_both"]]
    full = [a for a in candidates if rule[a]["pass_240_both"]]
    chosen = min(full or candidates, key=lambda a: rule[a]["d_nll_all"]) if candidates else None
    res["adoption"] = {"per_arm": rule, "candidates": candidates, "pass_240_both": full, "chosen": chosen,
                       "basis": "pass_240_both" if full else ("no_harm_lowest_logscore" if candidates else "none_report_for_ruling")}
    (HERE / "results_fit.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    f = lambda v: "%+.5f [%+.5f,%+.5f]" % tuple(v)
    print("updates", res["updates"], "stations", res["stations"], "days", res["event_days"])
    print("parameters", {k: ([round(x, 3) for x in v] if isinstance(v, list) else round(v, 3)) for k, v in res["parameters_p10_50_90"].items()})
    for a in ARMS:
        s = res["single_system"][a]
        print(f"{a:10s} >=2modes {s['multimodal_share']:.3f} (hi {s['multimodal_share_dM_ge_1p5']:.3f})  tail pred/real {s['tail_mass_predicted']:.4f}/{s['tail_hit_realised']:.4f}  hi {s['tail_mass_predicted_dM_ge_1p5']:.4f}/{s['tail_hit_realised_dM_ge_1p5']:.4f}  vsd {s['vector_sd_mean']:.3f}  pooled>=2 {res['pooled_multimodal_share'][a]:.4f}")
    for a in ARMS[1:]:
        print("==", a, "vs base", rule[a])
        for k, c in res["vs_base"][a].items():
            print(f"  {k:24s} sd={c['station_days']:5d} d={c['event_days']:2d}  dLog {f(c['d_nll'])}  dCRPS {f(c['d_crps'])}  dBrier {f(c['d_brier'])}  -> {'PASS' if c['verdict_240'] else 'FAIL'}{' no-harm' if c['no_harm'] else ' HARM'}")
    print("== adoption", res["adoption"])


if __name__ == "__main__":
    main()
