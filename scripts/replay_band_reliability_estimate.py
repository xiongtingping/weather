#!/usr/bin/env python3
"""Band-reliability estimation over the multi-year replay archive.

Implements the house method behind ``BAND_RELIABILITY_FACTORS`` on the
replay evidence lane, at scale, with month/season stratification:

  Gate 1  By-day bootstrap CI of (realised hits / summed claim) per
          claimed-probability band must exclude 1.0.
  Gate 2  The miscalibration must keep its sign across a chronological
          half-split.
  Gate 3  Transfer: applying the candidate factor (the interval end
          nearest 1.0, the house convention) to *production served
          claims* must improve NLL/day on the forward part of the
          serving sample.  Replay geometry is two families against four
          in live serving; nothing from gates 1-2 may touch the serving
          table without this gate.

The script only reads databases and writes a JSON report; changing
``cross_consensus.py`` remains a human-reviewed step.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from scripts.multiyear_band_replay_common import (  # noqa: E402
    DEFAULT_REPLAY_DB,
    open_production_db_readonly,
    open_replay_db,
    utc_now_iso,
    write_json_report,
)

BAND_EDGES = (0.00, 0.02, 0.12, 0.20, 0.35, 1.01)
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20260815
MIN_CLAIMS_PER_CELL = 200
MIN_DAYS_PER_CELL = 40
# The band layer entered serving on 2026-08-15; envelopes from that moment
# on already carry factors and cannot referee a new candidate without
# un-application.  The transfer sample therefore ends here.
SERVING_SAMPLE_END_UTC = "2026-08-15T00:00:00Z"
FORWARD_SPLIT = 0.6

_F_RANGE = re.compile(r"^(-?\d+)-(-?\d+)°?F$")
_F_BELOW = re.compile(r"^(-?\d+)°?F or below$")
_F_ABOVE = re.compile(r"^(-?\d+)°?F or higher$")
_C_EXACT = re.compile(r"^(-?\d+)°?C$")
_C_BELOW = re.compile(r"^(-?\d+)°?C or below$")
_C_ABOVE = re.compile(r"^(-?\d+)°?C or higher$")


def _f_to_c(value: float) -> float:
    return (value - 32.0) * 5.0 / 9.0


def bucket_interval_from_label(
    label: str,
) -> tuple[float | None, float | None]:
    """Self-describing market bucket labels to Celsius intervals."""

    text = label.strip()
    match = _C_EXACT.match(text)
    if match:
        center = float(match.group(1))
        return center - 0.5, center + 0.5
    match = _C_BELOW.match(text)
    if match:
        return None, float(match.group(1)) + 0.5
    match = _C_ABOVE.match(text)
    if match:
        return float(match.group(1)) - 0.5, None
    match = _F_RANGE.match(text)
    if match:
        return (
            _f_to_c(float(match.group(1)) - 0.5),
            _f_to_c(float(match.group(2)) + 0.5),
        )
    match = _F_BELOW.match(text)
    if match:
        return None, _f_to_c(float(match.group(1)) + 0.5)
    match = _F_ABOVE.match(text)
    if match:
        return _f_to_c(float(match.group(1)) - 0.5), None
    raise ValueError(f"band_estimate_unparseable_bucket_label:{label}")


def _band_of(probability: float) -> tuple[float, float] | None:
    for lower, upper in zip(BAND_EDGES, BAND_EDGES[1:]):
        if lower <= probability < upper:
            return (lower, upper)
    return None


def _season_bin(day: str) -> str:
    month = int(day[5:7])
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


def _ratio(day_rows: Sequence[tuple[float, int]]) -> float | None:
    claimed = sum(claim for claim, _hit in day_rows)
    if claimed <= 0:
        return None
    return sum(hit for _claim, hit in day_rows) / claimed


def _bootstrap_ci(
    by_day: Mapping[str, list[tuple[float, int]]],
) -> tuple[float, float] | None:
    days = sorted(by_day)
    if len(days) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    ratios = []
    for _resample in range(BOOTSTRAP_RESAMPLES):
        sample: list[tuple[float, int]] = []
        for _pick in range(len(days)):
            sample.extend(by_day[rng.choice(days)])
        ratio = _ratio(sample)
        if ratio is not None:
            ratios.append(ratio)
    if len(ratios) < BOOTSTRAP_RESAMPLES // 2:
        return None
    ratios.sort()
    lower = ratios[int(0.025 * len(ratios))]
    upper = ratios[int(0.975 * len(ratios))]
    return lower, upper


def _cell_estimate(
    by_day: Mapping[str, list[tuple[float, int]]],
) -> dict[str, Any] | None:
    days = sorted(by_day)
    all_rows = [row for day in days for row in by_day[day]]
    if len(all_rows) < MIN_CLAIMS_PER_CELL or len(days) < MIN_DAYS_PER_CELL:
        return None
    point = _ratio(all_rows)
    ci = _bootstrap_ci(by_day)
    if point is None or ci is None:
        return None
    half = len(days) // 2
    first = _ratio([row for day in days[:half] for row in by_day[day]])
    second = _ratio([row for day in days[half:] for row in by_day[day]])
    sign_stable = (
        first is not None
        and second is not None
        and (first - 1.0) * (second - 1.0) > 0
    )
    ci_excludes_one = ci[0] > 1.0 or ci[1] < 1.0
    candidate = None
    if ci_excludes_one and sign_stable:
        # House convention: serve the interval end nearest one.
        candidate = ci[0] if ci[0] > 1.0 else ci[1]
    return {
        "claims": len(all_rows),
        "days": len(days),
        "point_ratio": round(point, 4),
        "ci95": [round(ci[0], 4), round(ci[1], 4)],
        "half_split": [
            round(first, 4) if first is not None else None,
            round(second, 4) if second is not None else None,
        ],
        "gate1_ci_excludes_one": ci_excludes_one,
        "gate2_sign_stable": sign_stable,
        "candidate_factor": (
            round(candidate, 4) if candidate is not None else None
        ),
    }


def estimate_replay_bands(
    replay,
    *,
    lead_days: int,
    run_cycle_hour: int,
) -> dict[str, Any]:
    per_cell: dict[
        tuple[tuple[float, float], str],
        dict[str, list[tuple[float, int]]],
    ] = defaultdict(lambda: defaultdict(list))
    day_count = 0
    for station, event_date, payload_json, truth_bucket in replay.execute(
        "SELECT station_id, event_date, bucket_probabilities_json,"
        " truth_bucket FROM replay_oos_daily"
        " WHERE lead_days=? AND run_cycle_hour=?",
        (lead_days, run_cycle_hour),
    ):
        payload = json.loads(payload_json)
        probabilities = payload["probabilities"]
        day_count += 1
        for bucket, raw_claim in probabilities.items():
            claim = float(raw_claim)
            band = _band_of(claim)
            if band is None:
                continue
            hit = 1 if bucket == truth_bucket else 0
            row = (claim, hit)
            for stratum in (
                "ALL",
                f"season:{_season_bin(event_date)}",
                f"month:{event_date[5:7]}",
            ):
                per_cell[(band, stratum)][str(event_date)].append(row)
    cells = []
    for (band, stratum), by_day in sorted(
        per_cell.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        estimate = _cell_estimate(by_day)
        if estimate is None:
            continue
        cells.append({
            "band": [band[0], band[1]],
            "stratum": stratum,
            **estimate,
        })
    return {"station_day_rows": day_count, "cells": cells}


def _load_serving_sample(
    production,
) -> list[dict[str, Any]]:
    """Final P0 claim per settled station-day, pre-band-layer only."""

    truth: dict[tuple[str, str], float] = {}
    for station, day, tmax in production.execute(
        "SELECT t.station_id, t.local_date, t.official_tmax_c"
        " FROM calibration_station_daily_high_truth AS t"
        " WHERE t.qc_status='ACCEPTED'"
        " AND t.revision_no=("
        "  SELECT MAX(t2.revision_no)"
        "  FROM calibration_station_daily_high_truth AS t2"
        "  WHERE t2.station_id=t.station_id AND t2.local_date=t.local_date"
        "  AND t2.qc_status='ACCEPTED')"
    ):
        truth[(str(station), str(day))] = float(tmax)
    sample = []
    for station, event_date, probabilities_json in production.execute(
        "SELECT e.station_id, e.event_date, e.probabilities_json"
        " FROM basket_probability_envelope AS e"
        " WHERE e.stage='P0' AND e.decision_at_utc<?"
        " AND e.decision_at_utc=("
        "  SELECT MAX(e2.decision_at_utc)"
        "  FROM basket_probability_envelope AS e2"
        "  WHERE e2.station_id=e.station_id AND e2.event_date=e.event_date"
        "  AND e2.stage='P0' AND e2.decision_at_utc<?)",
        (SERVING_SAMPLE_END_UTC, SERVING_SAMPLE_END_UTC),
    ):
        key = (str(station), str(event_date))
        if key not in truth:
            continue
        probabilities = {
            str(bucket): float(value)
            for bucket, value in json.loads(probabilities_json).items()
        }
        truth_c = truth[key]
        truth_bucket = None
        for bucket in probabilities:
            lower, upper = bucket_interval_from_label(bucket)
            if (lower is None or truth_c > lower) and (
                upper is None or truth_c <= upper
            ):
                truth_bucket = bucket
                break
        if truth_bucket is None:
            continue
        sample.append({
            "station_id": key[0],
            "event_date": key[1],
            "probabilities": probabilities,
            "truth_bucket": truth_bucket,
        })
    sample.sort(key=lambda row: (row["event_date"], row["station_id"]))
    return sample


def _apply_factor_table(
    probabilities: Mapping[str, float],
    factor_rows: Sequence[Mapping[str, Any]],
    event_date: str,
) -> dict[str, float]:
    adjusted = {}
    for bucket, value in probabilities.items():
        claim = float(value)
        factor = 1.0
        band = _band_of(claim)
        if band is not None:
            season = f"season:{_season_bin(event_date)}"
            month = f"month:{event_date[5:7]}"
            # The most specific qualifying stratum serves; ALL is the
            # fallback.  Candidate tables list one row per (band, stratum).
            for stratum in (month, season, "ALL"):
                for row in factor_rows:
                    if (
                        tuple(row["band"]) == band
                        and row["stratum"] == stratum
                        and row.get("candidate_factor")
                    ):
                        factor = float(row["candidate_factor"])
                        break
                else:
                    continue
                break
        adjusted[bucket] = claim * factor
    total = sum(adjusted.values())
    return {bucket: value / total for bucket, value in adjusted.items()}


def _nll_per_day(
    sample: Sequence[Mapping[str, Any]],
    factor_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, float]:
    by_day: dict[str, list[float]] = defaultdict(list)
    for row in sample:
        probabilities = row["probabilities"]
        if factor_rows:
            probabilities = _apply_factor_table(
                probabilities, factor_rows, row["event_date"]
            )
        by_day[row["event_date"]].append(
            -math.log(max(float(probabilities[row["truth_bucket"]]), 1e-12))
        )
    return {
        day: sum(values) / len(values)
        for day, values in sorted(by_day.items())
    }


def transfer_gate(
    production,
    candidate_cells: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    sample = _load_serving_sample(production)
    if not sample:
        return {"state": "NO_SERVING_SAMPLE"}
    candidates = [
        cell for cell in candidate_cells if cell.get("candidate_factor")
    ]
    baseline = _nll_per_day(sample)
    banded = _nll_per_day(sample, candidates)
    days = sorted(baseline)
    split = int(len(days) * FORWARD_SPLIT)
    forward_days = days[split:]
    forward_delta = sum(
        baseline[day] - banded[day] for day in forward_days
    ) / max(len(forward_days), 1)
    day_wins = sum(
        1 for day in forward_days if banded[day] < baseline[day]
    )
    return {
        "state": "MEASURED",
        "serving_station_days": len(sample),
        "serving_days": len(days),
        "forward_days": len(forward_days),
        "candidate_cells_applied": len(candidates),
        "forward_nll_per_day_delta": round(forward_delta, 4),
        "forward_day_wins": day_wins,
        "passes": forward_delta > 0 and day_wins * 2 > len(forward_days),
        "note": (
            "Positive delta means the candidate table improves served "
            "claims.  Sample ends at the band layer's own activation; "
            "claims after that carry factors already."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-db", type=Path, default=DEFAULT_REPLAY_DB)
    parser.add_argument(
        "--production-db", type=Path, default=DEFAULT_TARGET_DB
    )
    parser.add_argument("--lead-days", type=int, default=1)
    parser.add_argument("--run-cycle-hour", type=int, default=0)
    parser.add_argument("--skip-transfer", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    replay = open_replay_db(args.replay_db)
    replay_estimates = estimate_replay_bands(
        replay,
        lead_days=args.lead_days,
        run_cycle_hour=args.run_cycle_hour,
    )
    result: dict[str, Any] = {
        "schema": "multiyear_band_reliability_estimate_v1",
        "generated_at_utc": utc_now_iso(),
        "band_edges": list(BAND_EDGES),
        "lead_days": args.lead_days,
        "run_cycle_hour": args.run_cycle_hour,
        "replay": replay_estimates,
    }
    if not args.skip_transfer:
        production = open_production_db_readonly(args.production_db)
        result["transfer_all_stratum"] = transfer_gate(
            production,
            [
                cell for cell in replay_estimates["cells"]
                if cell["stratum"] == "ALL"
            ],
        )
        result["transfer_with_season"] = transfer_gate(
            production,
            [
                cell for cell in replay_estimates["cells"]
                if cell["stratum"] == "ALL"
                or cell["stratum"].startswith("season:")
            ],
        )
    result["guards"] = {
        "writes_archive_db": False,
        "writes_production_ssot": False,
        "writes_replay_db": False,
        "places_orders": False,
    }
    if args.report:
        write_json_report(args.report, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
