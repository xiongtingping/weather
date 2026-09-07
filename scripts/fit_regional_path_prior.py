#!/usr/bin/env python3
"""Fit charter 99's regional path prior: how wrong the curve's remaining peak is.

At a decision instant the layer serves ``max(H, N(peak + bias, sigma))``.  This
fits that ``bias`` and ``sigma`` from settled days: for every regional run, at
every formal observation instant after it, the residual is the day's settled
maximum minus the greater of the running high and the curve's remaining peak.
Binned by hours left to local midnight -- the ceiling's axis, so the two layers
are read on one clock -- with a per-station bias where a station has earned one.

The peak this residual is taken against is the *debiased* one (charter 95,
2026-09-07): serving corrects the curve before it fuses, so a residual fitted
on the raw curve would be measuring an input production no longer has, and the
two layers would each remove a share of the same cold offset.  The order is
debias, then fit the residual of what is left.

``--replay-table`` emits one artifact per event date fitted only on strictly
earlier dates, the discipline the ceiling replay uses, so the paper ledger can
score a rule that could actually have been run that morning.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from bayes_basket_target.regional_path_prior import (  # noqa: E402
    HOURS_EDGES, MIN_CELL_SAMPLES, MIN_STATION_SAMPLES, MIN_SIGMA_C,
    MAX_RUN_AGE_HOURS, SCHEMA, hours_bin,
)
from bayes_basket_target.regional_point_debias import (  # noqa: E402
    ENABLED as DEBIAS_ENABLED,
    apply_remaining_peak_debias, resolve_correction as resolve_debias,
)
import fit_regional_point_debias as debias_fit  # noqa: E402

FORMAL = ("METAR", "SPECI")


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def collect(target_db: Path, archive_db: Path) -> list[dict]:
    target = sqlite3.connect(f"file:{target_db}?mode=ro", uri=True, timeout=120.0)
    archive = sqlite3.connect(f"file:{archive_db}?mode=ro", uri=True, timeout=120.0)
    # Charter 99 (2026-09-06): the truth is the calibration lane's settled
    # daily high, not the trajectory lane's station-day bounds.  The bounds
    # row is wrong on 87 station-days since 2026-08-20 -- KAUS 2026-08-23
    # records 28.0C against a day whose own METAR series reached 40.0C,
    # because the row keeps the maximum as of an early-morning report and
    # never advances -- and a fit trained on it learns that days do not warm.
    truth = {
        (str(r[0]), str(r[1])): float(r[2])
        for r in target.execute(
            "SELECT t.station_id,t.local_date,t.official_tmax_c "
            "FROM calibration_station_daily_high_truth AS t "
            "WHERE t.source_lane='AWC' AND t.qc_status='ACCEPTED' "
            "AND t.authority_scope IN "
            "('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') "
            "AND t.revision_no=("
            "  SELECT MAX(t2.revision_no) FROM calibration_station_daily_high_truth AS t2 "
            "  WHERE t2.station_id=t.station_id AND t2.local_date=t.local_date "
            "  AND t2.source_lane='AWC' AND t2.qc_status='ACCEPTED')"
        )
    }
    formal: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for row in archive.execute(
        "SELECT station_id,valid_at,reported_temperature_c"
        " FROM reported_trajectory_observations"
        " WHERE reported_temperature_c IS NOT NULL AND report_type IN (?,?)",
        FORMAL,
    ):
        try:
            formal[str(row[0])].append((_parse(row[1]), float(row[2])))
        except (TypeError, ValueError):
            continue
    for points in formal.values():
        points.sort()
    samples: list[dict] = []
    for row in target.execute(
        "SELECT ensemble_system_id,station_id,target_local_date,"
        "settlement_timezone,run_init_utc,hourly_c_json"
        " FROM calibration_regional_point_run ORDER BY target_local_date"
    ):
        system_id, station, day, zone_name, run_init_utc, curve_json = (
            str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5])
        )
        if (station, day) not in truth:
            continue
        try:
            zone = ZoneInfo(zone_name)
            curve = {_parse(k): float(v) for k, v in json.loads(curve_json).items()}
            run_init = _parse(run_init_utc)
        except (ValueError, KeyError, TypeError):
            continue
        day_start = datetime.combine(
            datetime.fromisoformat(day).date(), datetime.min.time(), tzinfo=zone
        ).astimezone(timezone.utc)
        close = day_start + timedelta(days=1)
        actual = truth[(station, day)]
        points = [p for p in formal.get(station, []) if day_start <= p[0] < close]
        if not points:
            continue
        curve_peak = max(curve.values()) if curve else None
        if curve_peak is None:
            continue
        running = None
        for when, value in points:
            running = value if running is None else max(running, value)
            if when <= run_init:
                continue
            age = (when - run_init).total_seconds() / 3600.0
            if age > MAX_RUN_AGE_HOURS:
                continue
            remaining = [v for stamp, v in curve.items() if stamp > when]
            if not remaining:
                continue
            # The anchor is finished later, once the debias artifact for this
            # sample's own cutoff is known: a replay table fits one path prior
            # per event date, and each must see the curve corrected by what was
            # knowable that morning, not by today's offsets.
            samples.append({
                "system_id": system_id, "station_id": station, "event_date": day,
                "hours_left": (close - when).total_seconds() / 3600.0,
                "raw_peak_c": max(remaining), "curve_peak_c": float(curve_peak),
                "running_c": float(running), "actual_c": actual,
                "run_age_hours": age,
            })
    return samples


def anchored(samples: list[dict], debias_artifact: dict | None) -> list[dict]:
    """Attach ``error_c`` -- the residual of the anchor production serves.

    ``debias_artifact`` of ``None`` means the raw curve, which is what the
    serving side hands the fusion whenever charter 95's correction is off.
    The residual has to be fitted on the input production actually has: a
    table fitted on debiased peaks and served against raw ones is a
    correction applied to a quantity it never saw.
    """

    out = []
    for sample in samples:
        peak, _note = apply_remaining_peak_debias(
            float(sample["raw_peak_c"]),
            curve_peak_c=float(sample["curve_peak_c"]),
            correction=resolve_debias(
                debias_artifact, system_id=str(sample["system_id"]),
                station_id=str(sample["station_id"]),
                run_age_hours=float(sample["run_age_hours"]),
            ),
            enabled=True,
        )
        anchor = max(float(sample["running_c"]), peak)
        out.append({**sample, "error_c": sample["actual_c"] - anchor})
    return out


def _scope(samples: list[dict]) -> dict:
    """Cells and station biases over one set of samples.

    Called once for the cross-system pool and once per system.  Charter 99,
    2026-09-06: the first artifact had no system dimension, so cells fitted
    on ten US HRRR/NBM stations served seven European ICON-EU station-days
    -- the dispersion that decides how much mass sits above the running
    high, taken from a different model on a different continent.  Fitting
    each system separately does not by itself forbid the borrow (ICON-EU's
    archive keeps no history, so it has none to fit); it makes the borrow
    something the serving side can see and declare.
    """

    per_cell: dict[int, list[float]] = defaultdict(list)
    per_station: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        per_cell[hours_bin(sample["hours_left"])].append(sample["error_c"])
        per_station[sample["station_id"]].append(sample["error_c"])
    pooled_sigma = (
        statistics.pstdev([s["error_c"] for s in samples])
        if len(samples) > 1 else MIN_SIGMA_C
    )
    cells = {}
    for index in range(len(HOURS_EDGES) - 1):
        values = per_cell.get(index, [])
        enough = len(values) >= MIN_CELL_SAMPLES
        cells[str(index)] = {
            "hours_from": HOURS_EDGES[index],
            "hours_to": HOURS_EDGES[index + 1],
            "bias_c": (sum(values) / len(values)) if enough else 0.0,
            "sigma_c": max(
                MIN_SIGMA_C,
                statistics.pstdev(values) if enough and len(values) > 1 else pooled_sigma,
            ),
            "sample_n": len(values),
            "backed_off": not enough,
        }
    stations = {
        station: {
            "bias_c": sum(values) / len(values),
            "sample_n": len(values),
        }
        for station, values in sorted(per_station.items())
        if len(values) >= MIN_STATION_SAMPLES
    }
    return {
        "sample_n": len(samples),
        "station_n": len({s["station_id"] for s in samples}),
        "station_day_n": len({(s["station_id"], s["event_date"]) for s in samples}),
        "trained_from": min(s["event_date"] for s in samples),
        "trained_through": max(s["event_date"] for s in samples),
        "pooled_sigma_c": pooled_sigma,
        "cells": cells,
        "stations": stations,
    }


def fit(samples: list[dict]) -> dict:
    by_system: dict[str, list[dict]] = defaultdict(list)
    for sample in samples:
        by_system[str(sample["system_id"])].append(sample)
    return {
        "schema": SCHEMA,
        "hours_edges": list(HOURS_EDGES),
        "minimum_cell_samples": MIN_CELL_SAMPLES,
        "minimum_station_samples": MIN_STATION_SAMPLES,
        # The cross-system pool stays at the top level, unchanged in shape
        # and meaning: it is the fallback a system without its own history
        # serves from, and an older artifact that has only this still loads.
        **_scope(samples),
        "systems": {
            system: _scope(rows) for system, rows in sorted(by_system.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "bayes_basket_target_ssot.db")
    parser.add_argument("--archive", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--through", type=str, default=None)
    parser.add_argument("--replay-table", action="store_true")
    parser.add_argument(
        "--no-debias", action="store_true",
        help="force the raw curve even where PM_REGIONAL_POINT_DEBIAS is on",
    )
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
        / "regional_path_prior_artifact.json",
    )
    args = parser.parse_args()
    samples = collect(args.db, args.archive)
    if args.through:
        samples = [s for s in samples if s["event_date"] <= args.through]
    if not samples:
        raise SystemExit("no samples")
    # The debias the serving side will have applied, fitted on the same rows.
    debias_samples = debias_fit.collect(args.db, args.archive)
    if args.through:
        debias_samples = [
            s for s in debias_samples if s["event_date"] <= args.through
        ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.replay_table:
        dates = sorted({s["event_date"] for s in samples})
        table = {}
        for date in dates:
            earlier = [s for s in samples if s["event_date"] < date]
            if len(earlier) < MIN_CELL_SAMPLES:
                continue
            earlier_debias = [
                s for s in debias_samples if s["event_date"] < date
            ]
            as_of_debias = (
                None if (args.no_debias or not DEBIAS_ENABLED) else
                debias_fit.fit(earlier_debias)
                if len(earlier_debias) >= debias_fit.MIN_POOL_SAMPLES
                else None
            )
            table[date] = fit(anchored(earlier, as_of_debias))
        payload = {
            "schema": SCHEMA + "_replay", "dates": table,
            "covered_from": min(table) if table else None,
            "covered_through": max(table) if table else None,
        }
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"{args.out}\n  {len(table)} 个事件日各一条只用更早日期拟合的表")
        return 0
    artifact = fit(anchored(
        samples,
        None if (args.no_debias or not DEBIAS_ENABLED) else
        debias_fit.fit(debias_samples)
        if len(debias_samples) >= debias_fit.MIN_POOL_SAMPLES else None,
    ))
    args.out.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    print(f"{args.out}")
    print(
        f"  样本 {artifact['sample_n']} · {artifact['station_day_n']} 站日 · "
        f"{artifact['station_n']} 站 · {artifact['trained_from']}..{artifact['trained_through']}"
        f" · 池化 σ {artifact['pooled_sigma_c']:.2f}"
    )
    for key in sorted(artifact["cells"], key=int):
        cell = artifact["cells"][key]
        print(
            "  %5.1f-%-5s h  偏差 %+5.2f°C  σ %4.2f  n=%5d%s" % (
                cell["hours_from"],
                "∞" if cell["hours_to"] > 1e8 else f"{cell['hours_to']:.0f}",
                cell["bias_c"], cell["sigma_c"], cell["sample_n"],
                "  (样本不足，服务池化 σ、零偏差)" if cell["backed_off"] else "",
            )
        )
    if artifact["stations"]:
        print("  站偏差:", {k: round(v["bias_c"], 2) for k, v in artifact["stations"].items()})
    print("  分系统（服务时优先用自家格子，没有才退回上面的池化格子）：")
    for system, scope in sorted(artifact["systems"].items()):
        live = [
            k for k in sorted(scope["cells"], key=int)
            if not scope["cells"][k]["backed_off"]
        ]
        print(
            f"    {system:22s} 样本 {scope['sample_n']:6d} · {scope['station_n']:3d} 站 · "
            f"{scope['trained_from']}..{scope['trained_through']} · "
            f"可服务格子 {len(live)}/{len(scope['cells'])} · 池化 σ {scope['pooled_sigma_c']:.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
