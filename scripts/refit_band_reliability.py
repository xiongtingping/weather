#!/usr/bin/env python3
"""Re-measure the band reliability table against accumulated truth.

Read-only.  Reports, per evidence lane, what each claimed-probability band
realises against what it claims, and what factor the admission rule would
serve today -- next to what ``BAND_RELIABILITY_FACTORS`` actually serves.
The table constants stay reviewed code; this script only says when the
evidence has outgrown them.

The vintage discipline this script exists to enforce: factors must be
fitted on claims that were served BEFORE the band layer touched them.
Post-band rows are excluded by receipt (serving lane) and by method tag
(gate lane); fitting on corrected claims would stack correction on
correction.

Two lanes, two geometries, deliberately never pooled:

* serving (market buckets, bridge history): the geometry the factors are
  served on.  This lane's numbers are the only ones that may change the
  table.
* gate (1C model buckets, OOS prediction archive): more sample, but its
  deep tail miscalibrates in the OPPOSITE direction (narrow far bins
  over-claim at 0.30 while open-ended market tails under-claim at 1.74),
  so its factors must never be copied across.  It is reported for the
  transfer check only: a cell may be adopted on the serving lane when the
  two lanes agree in direction there (the [0.20, 0.35) precedent).

Admission rule (the rule does not change, the table does): a band earns a
non-identity factor when its by-day bootstrap interval excludes 1.0 AND
its ratio keeps the same sign of miscalibration across a chronological
half-split; it serves the interval end nearest one.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.cross_consensus import (  # noqa: E402
    BAND_RELIABILITY_FACTORS,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
FINE_BANDS = (
    (0.00, 0.02),
    (0.02, 0.05),
    (0.05, 0.08),
    (0.08, 0.12),
    (0.02, 0.12),
    (0.12, 0.20),
    (0.20, 0.35),
    (0.35, 1.01),
)


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _truth(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    return {
        (str(station), str(day)): float(value)
        for station, day, value in conn.execute(
            "SELECT station_id, local_date, official_tmax_c "
            "FROM calibration_station_daily_high_truth"
        )
    }


def _serving_lane(
    conn: sqlite3.Connection,
    truth: dict[tuple[str, str], float],
) -> tuple[dict[str, list[list[tuple[float, int]]]], int]:
    """Last P0 snapshot per station-day, market-bucket geometry."""

    by_day: dict[str, list[list[tuple[float, int]]]] = defaultdict(list)
    excluded = 0
    rows = conn.execute(
        "SELECT h.station_id, h.event_date, h.history_id, h.envelope_json,"
        " MAX(h.decision_at_utc)"
        " FROM basket_probability_bridge_history h WHERE h.stage='P0'"
        " GROUP BY h.station_id, h.event_date"
    ).fetchall()
    for station, event, history_id, envelope_json, _latest in rows:
        actual = truth.get((str(station), str(event)))
        if actual is None:
            continue
        if "band_reliability_json" in str(envelope_json):
            excluded += 1
            continue
        buckets = conn.execute(
            "SELECT lower_c, upper_c, probability"
            " FROM basket_probability_bridge_bucket_probability"
            " WHERE history_id=?",
            (history_id,),
        ).fetchall()
        if not buckets:
            continue
        records = []
        for lower, upper, probability in buckets:
            lower_c = -1e9 if lower is None else float(lower)
            upper_c = 1e9 if upper is None else float(upper)
            records.append((
                float(probability),
                1 if lower_c <= actual < upper_c else 0,
            ))
        by_day[str(event)].append(records)
    return dict(by_day), excluded


def _gate_lane(
    conn: sqlite3.Connection,
    truth: dict[tuple[str, str], float],
) -> tuple[dict[str, list[list[tuple[float, int]]]], int]:
    """Latest-vintage OOS prediction per station-day-lead, 1C geometry."""

    by_day: dict[str, list[list[tuple[float, int]]]] = defaultdict(list)
    excluded = 0
    rows = conn.execute(
        "SELECT p.station_id, p.event_date, p.probabilities_json,"
        " p.bucket_centers_c_json, a.combination_method,"
        " MAX(p.trained_through_event_date || p.decision_at_utc)"
        " FROM calibration_cross_native_ensemble_oos_prediction p"
        " JOIN calibration_cross_native_ensemble_artifact a"
        " ON a.cross_ensemble_artifact_id = p.cross_ensemble_artifact_id"
        " GROUP BY p.station_id, p.event_date, p.lead_bucket"
    ).fetchall()
    for station, event, probabilities_json, centers_json, method, _ in rows:
        actual = truth.get((str(station), str(event)))
        if actual is None:
            continue
        if "band_reliability" in str(method):
            excluded += 1
            continue
        probabilities = json.loads(probabilities_json)
        centers = json.loads(centers_json)
        records = []
        for key in sorted(
            probabilities, key=lambda k: float(centers.get(k, k))
        ):
            center = float(centers.get(key, key))
            records.append((
                float(probabilities[key]),
                1 if (center - 0.5) <= actual < (center + 0.5) else 0,
            ))
        by_day[str(event)].append(records)
    return dict(by_day), excluded


def _band_report(
    by_day: dict[str, list[list[tuple[float, int]]]],
    *,
    bootstrap_samples: int,
    seed: int = 17,
) -> list[dict]:
    days = sorted(by_day)
    day_agg: dict[str, dict[tuple, list[float]]] = {}
    for day in days:
        agg = {band: [0.0, 0] for band in FINE_BANDS}
        for records in by_day[day]:
            for probability, hit in records:
                for band in FINE_BANDS:
                    if band[0] <= probability < band[1]:
                        agg[band][0] += probability
                        agg[band][1] += hit
        day_agg[day] = agg

    def ratios(sample: list[str]) -> dict[tuple, float | None]:
        out = {}
        for band in FINE_BANDS:
            claimed = sum(day_agg[d][band][0] for d in sample)
            realised = sum(day_agg[d][band][1] for d in sample)
            out[band] = realised / claimed if claimed > 0 else None
        return out

    point = ratios(days)
    random.seed(seed)
    bootstrap: dict[tuple, list[float]] = {band: [] for band in FINE_BANDS}
    for _ in range(bootstrap_samples):
        sample = [random.choice(days) for _ in days]
        sampled = ratios(sample)
        for band in FINE_BANDS:
            if sampled[band] is not None:
                bootstrap[band].append(sampled[band])
    first = ratios(days[: len(days) // 2])
    second = ratios(days[len(days) // 2:])
    report = []
    for band in FINE_BANDS:
        ordered = sorted(bootstrap[band])
        if not ordered or point[band] is None:
            continue
        low = ordered[int(0.025 * len(ordered))]
        high = ordered[int(0.975 * len(ordered))]
        sign_stable = (
            first[band] is not None
            and second[band] is not None
            and (first[band] - 1.0) * (second[band] - 1.0) > 0
        )
        qualified = not (low <= 1.0 <= high) and sign_stable
        proposed = (
            (high if high < 1.0 else low) if qualified else 1.0
        )
        report.append({
            "band": list(band),
            "ratio": round(point[band], 3),
            "interval": [round(low, 3), round(high, 3)],
            "halves": [
                None if first[band] is None else round(first[band], 3),
                None if second[band] is None else round(second[band], 3),
            ],
            "sign_stable": sign_stable,
            "qualified": qualified,
            "proposed_factor": round(proposed, 3),
        })
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    args = parser.parse_args()
    conn = _connect(args.db.expanduser().resolve(strict=True))
    truth = _truth(conn)
    current = {
        tuple(band): factor for band, factor in BAND_RELIABILITY_FACTORS
    }
    result: dict = {
        "schema": "band_reliability_refit_report_v1",
        "current_factors": [
            {"band": list(band), "factor": factor}
            for band, factor in BAND_RELIABILITY_FACTORS
        ],
        "lanes": {},
    }
    drift = []
    for lane_name, loader in (
        ("serving_market_buckets", _serving_lane),
        ("gate_model_buckets", _gate_lane),
    ):
        by_day, excluded = loader(conn, truth)
        report = _band_report(
            by_day, bootstrap_samples=args.bootstrap_samples
        )
        result["lanes"][lane_name] = {
            "day_count": len(by_day),
            "station_day_count": sum(
                len(rows) for rows in by_day.values()
            ),
            "post_band_rows_excluded": excluded,
            "bands": report,
        }
    serving_bands = {
        tuple(row["band"]): row
        for row in result["lanes"]["serving_market_buckets"]["bands"]
    }
    gate_bands = {
        tuple(row["band"]): row
        for row in result["lanes"]["gate_model_buckets"]["bands"]
    }
    for band, served in current.items():
        serving = serving_bands.get(band)
        gate = gate_bands.get(band)
        if serving is None:
            continue
        if serving["qualified"]:
            # The serving lane speaks for itself.
            if abs(serving["proposed_factor"] - served) > 0.02:
                drift.append({
                    "band": list(band),
                    "served_factor": served,
                    "proposed_factor": serving["proposed_factor"],
                    "basis": "serving_lane",
                })
            continue
        # Transfer-admitted cell (the [0.20, 0.35) precedent): a factor may
        # stand on the gate lane's qualification when both lanes point the
        # same way there.  Drift means that basis is gone, not that the
        # serving lane still cannot speak alone.
        transfer_holds = (
            served != 1.0
            and gate is not None
            and gate["qualified"]
            and (gate["ratio"] - 1.0) * (served - 1.0) > 0
        )
        if served != 1.0 and not transfer_holds:
            drift.append({
                "band": list(band),
                "served_factor": served,
                "proposed_factor": 1.0,
                "basis": "transfer_basis_lost",
            })
    result["serving_lane_drift"] = drift
    result["table_change_suggested"] = bool(drift)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
