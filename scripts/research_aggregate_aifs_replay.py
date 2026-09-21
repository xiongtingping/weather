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
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

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
    """Mean and 95% interval, resampling whole event dates.

    Per-date sums and counts, so a draw is a weighted sum over dates rather
    than a rebuilt list of every pair -- the same estimator, 10^4 times
    cheaper at fleet scale.
    """

    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for pair in pairs:
        sums[pair["event_date"]] += value(pair)
        counts[pair["event_date"]] += 1
    dates = sorted(sums)
    s = np.array([sums[d] for d in dates])
    n = np.array([counts[d] for d in dates], dtype=float)
    point = float(s.sum() / n.sum())
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(dates), size=(draws, len(dates)))
    means = np.sort(s[picks].sum(axis=1) / n[picks].sum(axis=1))
    return point, float(means[int(0.025 * draws)]), float(means[int(0.975 * draws)])


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
    parser.add_argument("--first-event-date", default="")
    args = parser.parse_args()
    scheme = f"C{args.kappa:g}"
    base, with_ = load(args.baseline_arm), load(args.with_arm)
    shared = sorted(k for k in set(base) & set(with_) if k[1] >= args.first_event_date)
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
        pair["seated"] = AIFS in with_[key].get("pool", ())
        if pair["seated"]:
            seated += 1
            aifs_weight.append(float(w.get("w", {}).get(AIFS, 0.0)))
        pairs.append(pair)
    # Where AIFS did not sit, the with_aifs arm served the baseline pool, so
    # its scores must equal the baseline's to the digit.  Anything else means
    # the pairing slipped or the view changed more than one member.
    unseated_drift = max(
        (abs(p[f"with_{v}_{m}"] - p[f"base_{v}_{m}"])
         for p in pairs if not p["seated"] for v in ("served", "noband", "raw") for m in METRICS),
        default=0.0,
    )
    report = {
        "scheme": scheme,
        "records": {"baseline": len(base), "with_aifs": len(with_), "paired": len(pairs)},
        "unpaired_baseline": len(set(base) - set(with_)),
        "unpaired_with_aifs": len(set(with_) - set(base)),
        "aifs_seated_updates": seated,
        "max_abs_delta_where_aifs_not_seated": unseated_drift,
        "aifs_weight_mean": round(statistics.fmean(aifs_weight), 4) if aifs_weight else None,
        "event_dates": len({p["event_date"] for p in pairs}),
        "stations": len({p["station"] for p in pairs}),
        "results": {},
    }
    seated_pairs = [p for p in pairs if p["seated"]]
    for weighting, rows in (
        ("per_update", pairs),
        ("per_station_day", station_day_means(pairs)),
        ("per_update_where_seated", seated_pairs),
        ("per_station_day_where_seated", station_day_means(seated_pairs)),
    ):
        if not rows:
            continue
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
