#!/usr/bin/env python3
"""Chronological out-of-sample replay over the multi-year evidence archive.

Research-only.  Walks each accepted station's history in strict date order
and, for every day with truth and members, serves the day from parameters
fitted only on earlier days -- the same constant-bias/spread mechanism,
variance-preserving kernel, climate blend and inverse-RMSE family weights
the production trainer uses (imported from ``native_ensemble_oos``, not
reimplemented).  The day's distribution is projected onto a market-shaped
bucket ladder centered where a lister would center it, and the claimed
probabilities land in ``replay_oos_daily`` for the band-reliability
estimator.

The replay geometry is two families at most (GEFS alone before 2024-11),
against four in live serving; every conclusion drawn from these rows must
therefore pass the serving-geometry transfer gate before touching
``BAND_RELIABILITY_FACTORS``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    _apply_parameters,
    _bounded_family_weights,
    _fit_parameters,
    _temperature_bucket,
)
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.market_registry import (  # noqa: E402
    load_active_registry_rows,
)
from bayes_basket_target.p0_producer import (  # noqa: E402
    bucket_intervals_from_registry_rows,
)
from scripts.multiyear_band_replay_common import (  # noqa: E402
    DEFAULT_REPLAY_DB,
    FAMILY_SYSTEM_IDS,
    classify_geometry,
    open_production_db_readonly,
    open_replay_db,
    project_integer_probability_to_intervals,
    synthesize_bucket_intervals,
    truth_bucket_for_intervals,
    utc_now_iso,
)

BUILD_VERSION = "replay_oos_v1"
FAMILY_IDS = {"gefs": "NOAA_GEFS", "ecmwf": "ECMWF_IFS"}
MIN_TRAIN_DAYS = 20
TRAIN_WINDOW_DAYS = 60
INTERIOR_BUCKET_COUNT = 9
EPS = 1e-12


def _crps(probability: Mapping[str, float], truth_int: int) -> float:
    keys = [int(key) for key in probability]
    lo = min(min(keys), truth_int - 1)
    hi = max(max(keys), truth_int + 1)
    cdf = 0.0
    total = 0.0
    for bucket in range(lo, hi + 1):
        cdf += float(probability.get(str(bucket), 0.0))
        indicator = 1.0 if bucket >= truth_int else 0.0
        total += (cdf - indicator) ** 2
    return total


def _ensure_geometry(
    replay,
    production,
    station_id: str,
) -> str:
    row = replay.execute(
        "SELECT geometry_class FROM replay_bucket_geometry"
        " WHERE station_id=?",
        (station_id,),
    ).fetchone()
    if row is not None:
        return str(row[0])
    rows = load_active_registry_rows(production, station_id=station_id)
    reference = max((str(r.get("event_date") or "") for r in rows), default="")
    rows = [r for r in rows if str(r.get("event_date") or "") == reference]
    intervals = bucket_intervals_from_registry_rows(rows)
    geometry_class = classify_geometry(intervals)
    replay.execute(
        "INSERT INTO replay_bucket_geometry(station_id, geometry_class,"
        " interior_bucket_count, reference_event_date,"
        " reference_intervals_json, captured_at_utc) VALUES(?,?,?,?,?,?)",
        (station_id, geometry_class, INTERIOR_BUCKET_COUNT, reference,
         json.dumps(intervals, sort_keys=True, default=str), utc_now_iso()),
    )
    replay.commit()
    return geometry_class


def _build_station(
    replay,
    production,
    *,
    station_id: str,
    lead_days: int,
    run_cycle_hour: int,
    rebuild: bool,
) -> dict[str, Any]:
    geometry_class = _ensure_geometry(replay, production, station_id)
    truth: dict[str, tuple[float, int]] = {
        str(row[0]): (float(row[1]), int(row[2]))
        for row in replay.execute(
            "SELECT local_date, tmax_c, tmax_int FROM replay_daily_truth"
            " WHERE station_id=? AND coverage_ok=1",
            (station_id,),
        )
    }
    from bayes_basket_target import ecmwf_ifs_archive, noaa_gefs_archive

    lane_extraction = {
        "gefs": noaa_gefs_archive.extraction_version_for_lead(
            lead_days, run_cycle_hour
        ),
        "ecmwf": ecmwf_ifs_archive.extraction_version_for_lead(
            lead_days, run_cycle_hour
        ),
    }
    members: dict[str, dict[str, list[float]]] = defaultdict(dict)
    for family, system_id in FAMILY_SYSTEM_IDS.items():
        for event_date, values_json in replay.execute(
            "SELECT target_local_date,"
            " json_group_array(raw_tmax_c) FROM replay_member_run"
            " WHERE ensemble_system_id=? AND station_id=?"
            " AND extraction_version=?"
            " GROUP BY target_local_date",
            (system_id, station_id, lane_extraction[family]),
        ):
            values = json.loads(values_json)
            if values:
                members[str(event_date)][family] = [
                    float(v) for v in values
                ]
    existing = set()
    if not rebuild:
        existing = {
            str(row[0])
            for row in replay.execute(
                "SELECT event_date FROM replay_oos_daily"
                " WHERE station_id=? AND lead_days=? AND run_cycle_hour=?"
                " AND build_version=?",
                (station_id, lead_days, run_cycle_hour, BUILD_VERSION),
            )
        }
    eligible_days = sorted(set(members) & set(truth))
    written = 0
    skipped_thin_train = 0
    for index, event_date in enumerate(eligible_days):
        if event_date in existing:
            continue
        history = [
            day for day in eligible_days[:index]
            if day < event_date
        ][-TRAIN_WINDOW_DAYS:]
        family_probability: dict[str, dict[str, float]] = {}
        train_rmse: dict[str, float] = {}
        train_n: dict[str, int] = {}
        parameters_receipt: dict[str, Any] = {}
        for family, values in members[event_date].items():
            cases = [
                {
                    "values": members[day][family],
                    "actual_tmax_c": truth[day][0],
                }
                for day in history
                if family in members[day]
            ]
            if len(cases) < MIN_TRAIN_DAYS:
                continue
            parameters = _fit_parameters(cases)
            family_probability[family] = _apply_parameters(
                values, parameters
            )
            # The scale a served distribution has to cover is the error left
            # after the centre shift, not the error about zero.
            train_rmse[family] = float(
                parameters.get("train_residual_sigma_c")
                or parameters["train_rmse_c"]
            )
            train_n[family] = len(cases)
            parameters_receipt[family] = {
                key: value
                for key, value in parameters.items()
                if key != "climate_samples_c"
            }
        if not family_probability:
            skipped_thin_train += 1
            continue
        if len(family_probability) == 1:
            family = next(iter(family_probability))
            weights = {family: 1.0}
        else:
            system_weights = _bounded_family_weights(
                train_error_scale_by_system={
                    FAMILY_SYSTEM_IDS[family]: rmse
                    for family, rmse in train_rmse.items()
                },
                family_by_system={
                    FAMILY_SYSTEM_IDS[family]: FAMILY_IDS[family]
                    for family in train_rmse
                },
                family_caps={},
                train_n_by_system={
                    FAMILY_SYSTEM_IDS[family]: n
                    for family, n in train_n.items()
                },
            )
            weights = {
                family: system_weights[FAMILY_SYSTEM_IDS[family]]
                for family in family_probability
            }
        keys = set()
        for probability in family_probability.values():
            keys |= set(probability)
        mixture = {
            key: sum(
                weights[family]
                * family_probability[family].get(key, 0.0)
                for family in family_probability
            )
            for key in sorted(keys, key=int)
        }
        total = sum(mixture.values())
        mixture = {key: value / total for key, value in mixture.items()}
        center = sum(
            float(int(key)) * value for key, value in mixture.items()
        )
        intervals = synthesize_bucket_intervals(
            geometry_class=geometry_class,
            center_c=center,
            interior_bucket_count=INTERIOR_BUCKET_COUNT,
        )
        bucket_probability = project_integer_probability_to_intervals(
            mixture, intervals
        )
        truth_c, truth_int = truth[event_date]
        truth_bucket = truth_bucket_for_intervals(truth_int, intervals)
        nll = -math.log(max(bucket_probability[truth_bucket], EPS))
        crps = _crps(mixture, truth_int)
        replay.execute(
            "INSERT OR REPLACE INTO replay_oos_daily(station_id,"
            " event_date, lead_days, run_cycle_hour, family_set,"
            " train_days, bucket_probabilities_json,"
            " integer_probabilities_json, truth_bucket, truth_tmax_int,"
            " nll, crps, parameters_json, build_version, built_at_utc)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                station_id, event_date, lead_days, run_cycle_hour,
                "+".join(sorted(family_probability)),
                max(train_n.values()),
                json.dumps({
                    "intervals": {
                        bucket: list(bounds)
                        for bucket, bounds in intervals.items()
                    },
                    "probabilities": bucket_probability,
                    "weights": weights,
                }, sort_keys=True),
                json.dumps(mixture, sort_keys=True),
                truth_bucket, truth_int, nll, crps,
                json.dumps(parameters_receipt, sort_keys=True),
                BUILD_VERSION, utc_now_iso(),
            ),
        )
        written += 1
    replay.commit()
    return {
        "station_id": station_id,
        "eligible_days": len(eligible_days),
        "written": written,
        "skipped_thin_train": skipped_thin_train,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-db", type=Path, default=DEFAULT_REPLAY_DB)
    parser.add_argument(
        "--production-db", type=Path, default=DEFAULT_TARGET_DB
    )
    parser.add_argument("--station-id")
    parser.add_argument("--lead-days", type=int, default=1)
    parser.add_argument("--run-cycle-hour", type=int, default=0)
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    replay = open_replay_db(args.replay_db)
    production = open_production_db_readonly(args.production_db)
    stations = [
        str(row[0])
        for row in replay.execute(
            "SELECT station_id FROM replay_truth_source"
            " WHERE acceptance_state='ACCEPTED'"
            + (" AND station_id=?" if args.station_id else "")
            + " ORDER BY station_id",
            ((args.station_id.strip().upper(),) if args.station_id else ()),
        )
    ]
    if not stations:
        raise RuntimeError("replay_oos_no_accepted_stations")
    results = [
        _build_station(
            replay,
            production,
            station_id=station,
            lead_days=args.lead_days,
            run_cycle_hour=args.run_cycle_hour,
            rebuild=args.rebuild,
        )
        for station in stations
    ]
    print(json.dumps({
        "build_version": BUILD_VERSION,
        "lead_days": args.lead_days,
        "run_cycle_hour": args.run_cycle_hour,
        "stations": results,
        "total_written": sum(r["written"] for r in results),
        "guards": {
            "writes_archive_db": False,
            "writes_production_ssot": False,
            "places_orders": False,
        },
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
