#!/usr/bin/env python3
"""Strict-as-of historical root posteriors from the backfilled native archive.

The METAR trajectory fit scores each fold as the NLL of the settled truth
bucket under ``root posterior x trajectory likelihood``, so a historical fold
needs the calibrated cross-family distribution *as it could have been known
that morning*.  No committed P0 envelope exists for days before 2026-08-02 --
the curve-carrying compiler was born then -- and retro-committing envelopes
would fabricate history in an append-only table.

What can be reconstructed honestly is the distribution itself: for day D, fit
each system's bias/spread on settled days strictly before D (the same
``_fit_parameters`` the production OOS evaluator uses), correct D's members --
which come from runs genuinely issued before D -- and combine families with
inverse-RMSE weights.  Every number is derived from information that predated
the day; downloading the archive later does not change what the models knew.

The output is labelled ``HISTORICAL_EXPANDING_WINDOW_V1`` and is training-only
evidence for the trajectory fit.  It is not a P0, never enters
``basket_probability_envelope``, and cannot be resolved as authority.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    _fit_parameters,
)
from bayes_basket_target.p0_producer import (  # noqa: E402
    bucket_intervals_from_registry_rows,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    write_bounded_json_if_changed,
)

DEFAULT_TARGET_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_OUTPUT_DIR = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "native_posterior_history"
)
SYSTEMS = ("ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05")
EVIDENCE_CLASS = "HISTORICAL_EXPANDING_WINDOW_V1"


def _centers(intervals: dict[str, tuple[float | None, float | None]]) -> dict[str, float]:
    """Bucket centers from interval edges, matching the runtime fallback."""

    result: dict[str, float] = {}
    for bucket, (lower, upper) in intervals.items():
        if lower is not None and upper is not None:
            result[bucket] = (float(lower) + float(upper)) / 2.0
        elif upper is not None:
            result[bucket] = float(upper) - 0.5
        elif lower is not None:
            result[bucket] = float(lower) + 0.5
        else:
            raise ContractError("posterior_history_interval_unbounded")
    return result


def _bucket_share(values: list[float], intervals) -> dict[str, float]:
    counts = {bucket: 0 for bucket in intervals}
    for value in values:
        for bucket, (lower, upper) in intervals.items():
            if (lower is None or value >= lower) and (
                upper is None or value < upper
            ):
                counts[bucket] += 1
                break
    total = max(1, sum(counts.values()))
    return {bucket: count / total for bucket, count in counts.items()}


def build_station_posteriors(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    lead_bucket: str,
    min_train_days: int,
) -> dict[str, object]:
    truth = {
        str(row[0]): float(row[1])
        for row in conn.execute(
            "SELECT local_date, official_tmax_c "
            "FROM calibration_station_daily_high_truth "
            "WHERE station_id=? AND qc_status='ACCEPTED'",
            (station_id,),
        )
    }
    members: dict[str, dict[str, list[float]]] = {s: {} for s in SYSTEMS}
    for system, day, value in conn.execute(
        "SELECT ensemble_system_id, target_local_date, raw_tmax_c "
        "FROM calibration_native_ensemble_historical_member_run "
        "WHERE station_id=? AND archive_lead_bucket=? "
        "  AND transport_provider_id!='OPEN_METEO' "
        f"  AND ensemble_system_id IN ({','.join('?' * len(SYSTEMS))})",
        (station_id, lead_bucket, *SYSTEMS),
    ):
        members[str(system)].setdefault(str(day), []).append(float(value))
    registry_days = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT event_date FROM basket_target_market_registry "
            "WHERE station_id=? AND source_eligible=1",
            (station_id,),
        )
    }
    all_days = sorted(
        set.intersection(*(set(members[s]) for s in SYSTEMS))
    )
    posteriors: list[dict[str, object]] = []
    skipped: dict[str, int] = {}
    for day in all_days:
        if day not in registry_days:
            skipped["no_market_registry"] = skipped.get("no_market_registry", 0) + 1
            continue
        rows = conn.execute(
            "SELECT station_id, event_date, settlement_unit, outcome, "
            "registry_json FROM basket_target_market_registry "
            "WHERE station_id=? AND event_date=? AND source_eligible=1 "
            "GROUP BY outcome",
            (station_id, day),
        ).fetchall()
        try:
            intervals = bucket_intervals_from_registry_rows([
                {
                    "station_id": row[0],
                    "event_date": row[1],
                    "settlement_unit": row[2],
                    "outcome": row[3],
                    "registry_json": row[4],
                }
                for row in rows
            ])
        except ContractError:
            skipped["registry_intervals_invalid"] = (
                skipped.get("registry_intervals_invalid", 0) + 1
            )
            continue
        fits: dict[str, dict[str, object]] = {}
        for system in SYSTEMS:
            train_cases = [
                {"values": members[system][prior], "actual_tmax_c": truth[prior]}
                for prior in all_days
                if prior < day and prior in truth and prior in members[system]
            ]
            if len(train_cases) < min_train_days:
                break
            fits[system] = _fit_parameters(train_cases)
        if len(fits) != len(SYSTEMS):
            skipped["insufficient_train_days"] = (
                skipped.get("insufficient_train_days", 0) + 1
            )
            continue
        weights_raw = {
            system: 1.0 / max(float(fits[system]["train_rmse_c"]), 0.1)
            for system in SYSTEMS
        }
        total_weight = sum(weights_raw.values())
        weights = {s: w / total_weight for s, w in weights_raw.items()}
        member_share = {bucket: 0.0 for bucket in intervals}
        for system in SYSTEMS:
            raw = members[system][day]
            mean = sum(raw) / len(raw)
            bias = float(fits[system]["bias_c"])
            spread = float(fits[system]["spread_factor"])
            corrected = [mean + bias + spread * (v - mean) for v in raw]
            share = _bucket_share(corrected, intervals)
            for bucket in intervals:
                member_share[bucket] += weights[system] * share[bucket]
        climate_values = [
            truth[prior] for prior in all_days if prior < day and prior in truth
        ]
        climate_share = _bucket_share(climate_values, intervals)
        reliability = sum(
            float(fits[system]["reliability_weight"]) for system in SYSTEMS
        ) / len(SYSTEMS)
        probabilities = {
            bucket: reliability * member_share[bucket]
            + (1.0 - reliability) * climate_share[bucket]
            for bucket in intervals
        }
        total = sum(probabilities.values())
        if total <= 0:
            skipped["degenerate_distribution"] = (
                skipped.get("degenerate_distribution", 0) + 1
            )
            continue
        probabilities = {b: p / total for b, p in probabilities.items()}
        posteriors.append({
            "event_date": day,
            "root_probabilities": probabilities,
            "bucket_centers_c": _centers(intervals),
            "bucket_intervals_c": {
                bucket: [lower, upper]
                for bucket, (lower, upper) in intervals.items()
            },
            "system_weights": weights,
            # Per-system additive corrections, carried so the curve combiner
            # can mirror the production shape: point = sum_s w_s * (curve_s +
            # bias_s).  Fitted on days strictly before this one.
            "system_bias_c": {
                system: float(fits[system]["bias_c"]) for system in SYSTEMS
            },
            "train_day_count": min(
                len([
                    prior for prior in all_days
                    if prior < day and prior in truth and prior in members[s]
                ])
                for s in SYSTEMS
            ),
            "truth_visible": day in truth,
        })
    identity = {
        "schema": "native_expanding_window_posterior_history_v1",
        "station_id": station_id,
        "archive_lead_bucket": lead_bucket,
        "evidence_class": EVIDENCE_CLASS,
        "authority_scope": "TRAINING_ONLY_NEVER_P0",
        "min_train_days": min_train_days,
        "systems": list(SYSTEMS),
        "posteriors": posteriors,
        "skipped": skipped,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--lead-bucket", default="d1", choices=["d0", "d1", "d2"])
    parser.add_argument("--min-train-days", type=int, default=20)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    conn = sqlite3.connect(
        f"file:{args.db.expanduser().resolve(strict=True)}?mode=ro", uri=True
    )
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        result = build_station_posteriors(
            conn,
            station_id=str(args.station_id).upper(),
            lead_bucket=args.lead_bucket,
            min_train_days=int(args.min_train_days),
        )
    finally:
        conn.close()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{str(args.station_id).upper()}_{args.lead_bucket}.json"
    wrote = write_bounded_json_if_changed(path, result)
    print(json.dumps({
        "station_id": str(args.station_id).upper(),
        "lead_bucket": args.lead_bucket,
        "posterior_day_count": len(result["posteriors"]),
        "skipped": result["skipped"],
        "output": str(path),
        "wrote": wrote,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
