#!/usr/bin/env python3
"""After the new models seated: the high-bin and width-lambda checks, again.

Reads the six-family stable-pool replay (``pass1/``: production functions,
AIFS ENS and AIGEFS history on modelled arrivals, 2026-07-20..09-22; see
``fusion_blend_historical_20260923/capture.py``) and, beside it, the same
code's four-family pass (``fusion_blend_historical_20260923/pass1``, where
both backfilled systems' history never arrived in time).  Judged by charter
211 as amended 2026-09-24: bucket Brier and LogScore both lower is closer;
Reliability / Resolution are read, not gated.

1. Modal claim against its hit rate by lead band (integer-degree buckets,
   the replay's own grid), station-day weights.
2. Width lambda: incumbent 0.75, one pooled lambda, one per lead band,
   chosen out of sample (rolling origin, 3-day gap, >= 10 training days) on
   the lambda grid each update carries; scored on NLL and bucket Brier.
Writes results.json.
"""
from __future__ import annotations

import gzip
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import replay_p0_stable_pool as RP  # noqa: E402
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from calibration_dashboard import _core_murphy  # noqa: E402

SIX = HERE / "pass1"
FOUR = ROOT / "docs" / "research" / "fusion_blend_historical_20260923" / "pass1"
GRID = list(RP.LAMBDA_GRID)
INCUMBENT = GRID.index(0.75)
LEADS = (("<12h", 0, 12), ("12-24h", 12, 24), ("24-36h", 24, 36), ("36-48h", 36, 48), (">=48h", 48, 9999))
BANDS = ((0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01))
GAP_DAYS, MIN_TRAIN, BOOT, BLOCK = 3, 10, 2000, 3


def load(directory: Path):
    rows = []
    for path in sorted(directory.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                r = json.loads(line)
                end = datetime.fromisoformat(r["event_date"]).replace(tzinfo=ZoneInfo(r["tz"])) + timedelta(days=1)
                hours = (end - datetime.fromisoformat(r["at"].replace("Z", "+00:00"))).total_seconds() / 3600.0
                if hours <= 0:
                    continue
                vec = r["seat"]["vec"]
                hit = oos._temperature_bucket(r["actual"])
                key = max(vec, key=lambda k: vec[k])
                total = sum(vec.values())
                rows.append({
                    "station": r["station"], "day": r["event_date"],
                    "band": next(n for n, lo, hi in LEADS if lo <= hours < hi),
                    "systems": len(r["pool"]), "claim": vec[key] / total, "hit": int(key == hit),
                    "vec": vec, "hit_key": hit,
                    "nll": [float(x[1]) for x in r["seat"].get("lam") or []],
                    "brier": [float(x[2]) for x in r["seat"].get("lam") or []],
                    "served": r["seat"]["served"],
                })
    copies = Counter((r["station"], r["day"]) for r in rows)
    for r in rows:
        r["w"] = 1.0 / copies[(r["station"], r["day"])]
    return rows


def modal_table(rows):
    out = {}
    for name, _lo, _hi in LEADS + (("all", 0, 0),):
        members = [r for r in rows if (name == "all" or r["band"] == name)]
        top = [r for r in members if r["claim"] >= 0.5]
        wt = sum(r["w"] for r in members)
        tw = sum(r["w"] for r in top)
        out[name] = {
            "station_days": len({(r["station"], r["day"]) for r in members}),
            "top_share": tw / wt if wt else None,
            "top_claimed": sum(r["w"] * r["claim"] for r in top) / tw if tw else None,
            "top_happened": sum(r["w"] * r["hit"] for r in top) / tw if tw else None,
        }
    bands = []
    for lo, hi in BANDS:
        members = [r for r in rows if lo <= r["claim"] < hi]
        w = sum(r["w"] for r in members)
        if w:
            bands.append({"band": [lo, min(hi, 1.0)], "station_days": len({(r["station"], r["day"]) for r in members}),
                          "claimed": sum(r["w"] * r["claim"] for r in members) / w,
                          "happened": sum(r["w"] * r["hit"] for r in members) / w})
    cells = []
    for r in rows:
        keys = sorted(set(r["vec"]) | {r["hit_key"]}, key=int)
        cells.append((r["w"], {"probabilities": [float(r["vec"].get(k, 0.0)) for k in keys], "hit": keys.index(r["hit_key"])}))
    m = _core_murphy(cells)
    w = np.array([r["w"] for r in rows])
    return {
        "by_lead": out, "by_claim": bands,
        "brier": float((w * np.array([r["served"][2] for r in rows])).sum() / w.sum()),
        "nll": float((w * np.array([r["served"][1] for r in rows])).sum() / w.sum()),
        "rel": m["reliability"], "res": m["resolution"],
    }


def lambda_oos(rows):
    rows = [r for r in rows if len(r["nll"]) == len(GRID)]
    days = sorted({r["day"] for r in rows})
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)

    def best(train, band=None):
        members = [r for r in train if band is None or r["band"] == band]
        if not members:
            return INCUMBENT
        w = np.array([r["w"] for r in members])
        return int(np.argmin((w[:, None] * np.array([r["nll"] for r in members])).sum(axis=0)))

    scored = []
    last = None
    for day in days:
        cutoff = (date.fromisoformat(day) - timedelta(days=GAP_DAYS)).isoformat()
        train_days = [d for d in days if d <= cutoff]
        if len(train_days) < MIN_TRAIN:
            continue
        train = [r for d in train_days for r in by_day[d]]
        pooled = best(train)
        per_band = {n: best(train, n) for n, _lo, _hi in LEADS}
        last = {"day": day, "pooled": GRID[pooled], "per_band": {k: GRID[v] for k, v in per_band.items()}}
        for r in by_day[day]:
            for metric in ("nll", "brier"):
                scored.append({"metric": metric, "day": day, "w": r["w"], "band": r["band"],
                               "incumbent": r[metric][INCUMBENT], "pooled": r[metric][pooled],
                               "per_band": r[metric][per_band[r["band"]]]})
    rng = np.random.default_rng(20260924)
    out = {"last_choice": last}
    for metric in ("nll", "brier"):
        subset = [s for s in scored if s["metric"] == metric]
        test_days = sorted({s["day"] for s in subset})
        idx = defaultdict(list)
        for i, s in enumerate(subset):
            idx[s["day"]].append(i)
        w = np.array([s["w"] for s in subset])
        arms = {k: np.array([s[k] for s in subset]) for k in ("incumbent", "pooled", "per_band")}
        diffs = defaultdict(list)
        n_blocks = math.ceil(len(test_days) / BLOCK)
        for _ in range(BOOT):
            pick = np.concatenate([idx[test_days[(s + j) % len(test_days)]]
                                   for s in rng.integers(0, len(test_days), n_blocks) for j in range(BLOCK)])
            ww = w[pick]
            for a, b in (("pooled", "incumbent"), ("per_band", "incumbent"), ("per_band", "pooled")):
                diffs[f"{a}-{b}"].append(float((ww * (arms[a][pick] - arms[b][pick])).sum() / ww.sum()))
        full = defaultdict(lambda: np.zeros(len(GRID)))
        full_w = defaultdict(float)
        for r in rows:
            full[r["band"]] += r["w"] * np.array(r[metric])
            full_w[r["band"]] += r["w"]
        out[metric] = {
            "test_days": len(test_days),
            "mean": {k: float((w * v).sum() / w.sum()) for k, v in arms.items()},
            "diff_mean": {k: float(np.mean(v)) for k, v in diffs.items()},
            "ci95": {k: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] for k, v in diffs.items()},
            "full_sample_best": {b: GRID[int(np.argmin(full[b]))] for b in full},
        }
    return out


def main() -> int:
    result = {}
    for label, directory in (("six_families", SIX), ("four_families", FOUR)):
        rows = load(directory)
        systems = Counter(r["systems"] for r in rows)
        result[label] = {"updates": len(rows), "station_days": len({(r['station'], r['day']) for r in rows}),
                         "pool_sizes": dict(sorted(systems.items())), "calibration": modal_table(rows)}
        if label == "six_families":
            result[label]["lambda"] = lambda_oos(rows)
        c = result[label]["calibration"]
        print(f"== {label}: {len(rows)} updates, {result[label]['station_days']} station-days, pools {dict(sorted(systems.items()))}")
        print(f"   served Brier {c['brier']:.5f} NLL {c['nll']:.4f} REL {c['rel']*1e3:.3f} RES {c['res']*1e3:.2f} (x1e-3)")
        for name, v in c["by_lead"].items():
            if v["top_claimed"] is not None:
                print(f"   {name}: {v['station_days']} sd, modal>=0.5 share {v['top_share']:.2f} says {v['top_claimed']:.3f} got {v['top_happened']:.3f}")
        for b in c["by_claim"]:
            print(f"   modal {b['band']}: {b['station_days']} sd says {b['claimed']:.3f} got {b['happened']:.3f}")
    lam = result["six_families"]["lambda"]
    for metric in ("nll", "brier"):
        m = lam[metric]
        print(f"[λ {metric}] {m['test_days']} test days mean {({k: round(v, 5) for k, v in m['mean'].items()})}")
        print(f"   ci {({k: [round(x, 5) for x in v] for k, v in m['ci95'].items()})}")
        print(f"   full-sample best by lead {m['full_sample_best']}")
    print("   last choice", lam["last_choice"])
    (HERE / "results.json").write_text(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
