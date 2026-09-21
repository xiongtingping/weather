#!/usr/bin/env python3
"""Research only: pair the two replay arms and say what AIFS ENS did to P0.

Reads ``data/aifs_ens_research/replay/{baseline,with_aifs}/*.jsonl.gz`` from
``research_replay_aifs_stable_pool.py``.  A record is one P0 update; the arms
pair on (station, event date, update time), which AIFS rows sharing PF50's
arrival stamps make exact.

The scheme read is the one production serves -- the stable pool at its
configured kappa -- scored three ways the replay already records: ``served``
(the band and centre-width tables active at the update), ``noband`` and
``raw``.  The tables were fitted on a pool without AIFS, so the served score
alone could credit or blame AIFS for a table mismatch; the other two say
whether the answer depends on them.

Two weightings, because they answer different questions: per update (what
the served stream looked like) and per station-day (each settled day counted
once -- the unit a trade is settled on, and the one a station with many
updates cannot dominate).  Intervals resample whole event dates.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.stable_pool import STABLE_POOL_KAPPA  # noqa: E402

REPLAY = ROOT / "data" / "aifs_ens_research" / "replay"
AIFS = "ECMWF_AIFS_ENS_GLOBAL"
METRICS = ("crps", "nll", "brier")


def load(arm: str) -> dict[tuple[str, str, str], dict]:
    out = {}
    for path in sorted((REPLAY / arm).glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                record = json.loads(line)
                out[(record["station"], record["event_date"], record["at"])] = record
    return out


def bootstrap(pairs: list[dict], value, *, draws: int = 2000, seed: int = 13):
    by_date = defaultdict(list)
    for pair in pairs:
        by_date[pair["event_date"]].append(pair)
    dates = sorted(by_date)
    point = statistics.fmean(value(p) for p in pairs)
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sample = [p for d in rng.choices(dates, k=len(dates)) for p in by_date[d]]
        means.append(statistics.fmean(value(p) for p in sample))
    means.sort()
    return point, means[int(0.025 * draws)], means[int(0.975 * draws)]


def station_day_means(pairs: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    for pair in pairs:
        grouped[(pair["station"], pair["event_date"])].append(pair)
    out = []
    for (station, day), rows in grouped.items():
        merged = {"station": station, "event_date": day, "lead": rows[-1]["lead"]}
        for key in rows[0]:
            if key.startswith(("base_", "with_")):
                merged[key] = statistics.fmean(r[key] for r in rows)
        out.append(merged)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kappa", type=float, default=STABLE_POOL_KAPPA)
    parser.add_argument("--baseline-arm", default="baseline")
    parser.add_argument("--with-arm", default="with_aifs")
    args = parser.parse_args()
    scheme = f"C{args.kappa:g}"
    base, with_ = load(args.baseline_arm), load(args.with_arm)
    shared = sorted(set(base) & set(with_))
    pairs = []
    seated = 0
    aifs_weight = []
    for key in shared:
        b, w = base[key]["schemes"].get(scheme), with_[key]["schemes"].get(scheme)
        if not b or not w:
            continue
        pair = {"station": key[0], "event_date": key[1], "at": key[2], "lead": base[key]["lead"]}
        for view in ("served", "noband", "raw"):
            for i, metric in enumerate(METRICS):
                pair[f"base_{view}_{metric}"] = float(b[view][i])
                pair[f"with_{view}_{metric}"] = float(w[view][i])
        if AIFS in with_[key].get("pool", ()):
            seated += 1
            aifs_weight.append(float(w.get("w", {}).get(AIFS, 0.0)))
        pairs.append(pair)
    report = {
        "scheme": scheme,
        "records": {"baseline": len(base), "with_aifs": len(with_), "paired": len(pairs)},
        "unpaired_baseline": len(set(base) - set(with_)),
        "unpaired_with_aifs": len(set(with_) - set(base)),
        "aifs_seated_updates": seated,
        "aifs_weight_mean": round(statistics.fmean(aifs_weight), 4) if aifs_weight else None,
        "event_dates": len({p["event_date"] for p in pairs}),
        "stations": len({p["station"] for p in pairs}),
        "results": {},
    }
    for weighting, rows in (("per_update", pairs), ("per_station_day", station_day_means(pairs))):
        block = {}
        for view in ("served", "noband", "raw"):
            for metric in METRICS:
                base_mean = statistics.fmean(r[f"base_{view}_{metric}"] for r in rows)
                point, low, high = bootstrap(
                    rows, lambda r: r[f"with_{view}_{metric}"] - r[f"base_{view}_{metric}"]
                )
                block[f"{view}_{metric}"] = {
                    "baseline": round(base_mean, 4),
                    "delta": round(point, 4), "ci95": [round(low, 4), round(high, 4)],
                    "relative": round(point / base_mean, 4) if base_mean else None,
                }
        leads = {}
        for lead in sorted({r["lead"] for r in rows}):
            sub = [r for r in rows if r["lead"] == lead]
            point, low, high = bootstrap(sub, lambda r: r["with_served_crps"] - r["base_served_crps"])
            base_mean = statistics.fmean(r["base_served_crps"] for r in sub)
            leads[lead] = {"n": len(sub), "baseline": round(base_mean, 4), "delta": round(point, 4),
                           "ci95": [round(low, 4), round(high, 4)],
                           "relative": round(point / base_mean, 4) if base_mean else None}
        block["served_crps_by_lead"] = leads
        by_station = defaultdict(list)
        for r in rows:
            by_station[r["station"]].append(r["with_served_crps"] - r["base_served_crps"])
        block["stations_better_with_aifs"] = sum(1 for v in by_station.values() if statistics.fmean(v) < 0)
        block["stations"] = len(by_station)
        report["results"][weighting] = block
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
