#!/usr/bin/env python3
"""Per station: does the model put more probability on the settled bucket
than the market?

熊先生 2026-09-26 追问：「如果按照站点呢，哪些站点模型比市场有优势？」  Same
population and arithmetic as model_right_market_wrong.py (d = log p_model(truth)
- log p_market(truth); each station-day x stage counted once).  Per station:
d with an event-day bootstrap interval, the two halves of the event window
(split at the median event date), the top-bucket net (model right & market
wrong minus the reverse), and P0 alone.  Read-only; writes
station_advantage_results.json.

    python3 station_advantage.py [population_cache.pkl [asks_cache.pkl]]
"""
from __future__ import annotations

import json
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from measure import ROOT, load  # noqa: E402
from model_right_market_wrong import build, load_asks  # noqa: E402

DRAWS = 1000
SEED = 20260926


def ci(items, f, seed):
    days = sorted({d["date"] for d in items})
    if len(days) < 3:
        return None
    index = {day: i for i, day in enumerate(days)}
    di = np.array([index[d["date"]] for d in items])
    w = np.array([d["w"] for d in items])
    v = np.array([float(f(d)) for d in items])
    a = np.bincount(di, weights=w * v, minlength=len(days))
    b = np.bincount(di, weights=w, minlength=len(days))
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(DRAWS):
        pick = np.bincount(rng.integers(0, len(days), len(days)), minlength=len(days)).astype(float)
        boots.append(float(pick @ a / (pick @ b)))
    return [float(a.sum() / b.sum()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5)), len(days)]


def modal_net(d):
    return d["model_top_right"] * (1 - d["market_top_right"]) - d["market_top_right"] * (1 - d["model_top_right"])


def main():
    pop_cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    ask_cache = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if pop_cache is not None and pop_cache.exists():
        rows = pickle.loads(pop_cache.read_bytes())
        rows = rows[0] if isinstance(rows, tuple) else rows
    else:
        rows = load(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro")
    asks = pickle.loads(ask_cache.read_bytes()) if ask_cache is not None and ask_cache.exists() else load_asks()
    recs = build(rows, asks)
    dates = sorted({d["date"] for d in recs})
    split = dates[len(dates) // 2]
    by_station = defaultdict(list)
    for d in recs:
        by_station[d["station"]].append(d)
    stations = {}
    for i, (station, items) in enumerate(sorted(by_station.items())):
        first = [d for d in items if d["date"] < split]
        second = [d for d in items if d["date"] >= split]
        p0 = [d for d in items if d["stage"] == "P0"]
        seed = SEED + 10 * i
        stations[station] = {
            "unit": "F" if items[0]["unit_F"] else "C",
            "d": ci(items, lambda d: d["d"], seed),
            "d_first_half": ci(first, lambda d: d["d"], seed + 1),
            "d_second_half": ci(second, lambda d: d["d"], seed + 2),
            "top_bucket_net": ci(items, modal_net, seed + 3),
            "top_bucket_net_first_half": ci(first, modal_net, seed + 4),
            "top_bucket_net_second_half": ci(second, modal_net, seed + 5),
            "p0_d": ci(p0, lambda d: d["d"], seed + 6),
            "p0_d_first_half": ci([d for d in p0 if d["date"] < split], lambda d: d["d"], seed + 7),
            "p0_d_second_half": ci([d for d in p0 if d["date"] >= split], lambda d: d["d"], seed + 8),
        }
    halves = [(v["d_first_half"][0], v["d_second_half"][0]) for v in stations.values()
              if v["d_first_half"] and v["d_second_half"]]
    nets = [(v["top_bucket_net_first_half"][0], v["top_bucket_net_second_half"][0]) for v in stations.values()
            if v["top_bucket_net_first_half"] and v["top_bucket_net_second_half"]]
    result = {
        "question": "熊先生 2026-09-26：哪些站点模型比市场有优势",
        "split_event_date": split,
        "stations_model_ahead": [s for s, v in stations.items() if v["d"][0] > 0],
        "stations_significantly_ahead": [s for s, v in stations.items() if v["d"][1] > 0],
        "stations_significantly_behind": [s for s, v in stations.items() if v["d"][2] < 0],
        "stations_tied": [s for s, v in stations.items() if v["d"][1] <= 0 <= v["d"][2]],
        "split_half_correlation_d": float(np.corrcoef(*zip(*halves))[0, 1]),
        "split_half_correlation_top_bucket_net": float(np.corrcoef(*zip(*nets))[0, 1]),
        "stations": stations,
    }
    (HERE / "station_advantage_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for key in ("stations_model_ahead", "stations_significantly_ahead", "stations_tied"):
        print(key, result[key])
    print("behind", len(result["stations_significantly_behind"]),
          "corr d", round(result["split_half_correlation_d"], 3),
          "corr net", round(result["split_half_correlation_top_bucket_net"], 3))


if __name__ == "__main__":
    main()
