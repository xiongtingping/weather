#!/usr/bin/env python3
"""Four arms over the same out-of-sample days: does the pooled band still
buy anything once the per-station width layer has had its turn?

    A  neither layer          (the cells as served, minus the band)
    B  station width only
    C  pooled band only
    D  station width, then a band fitted on the STATION-CALIBRATED residual

The question D exists to answer is charter-shaped rather than numerical.
Both layers are fitted on served claims, so if the band is fitted on the
pre-station output while the station layer is also serving, the same
miscalibration is corrected twice -- once in each layer -- and the product
overshoots.  That is exactly how 0.75 x 0.5 = 0.375 happened between the
pooled lambda and the station table.  Fitting the band on the residual the
station layer leaves is the arrangement where each layer answers one
question, and D beating B is the only evidence that the band's question is
still worth asking.  D failing to beat B is not a tie: it says the band
should be identity, and being the last layer is not a reason to keep it.

Read-only.  Every day is fitted on earlier days only -- the store's own cell
builder, the serving kernel (``apply_center_width_calibration``,
``apply_band_reliability``) and the band lane's own admission report do all
the arithmetic, so this measures the rules in production rather than a
second copy of them.

    python3 scripts/ablate_station_and_band_layers.py --stage P0
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.band_reliability_store import (  # noqa: E402
    BAND_AXIS_CENTRE_DISTANCE,
    MIN_CELL_STATIONS,
    band_lane_report,
    band_table_axis,
    cells_for_axis,
)
from bayes_basket_target.center_width_calibration import (  # noqa: E402
    apply_center_width_calibration,
)
from bayes_basket_target.center_width_store import (  # noqa: E402
    MAX_STATION_WIDTH_SCALE,
    MIN_STATION_WIDTH_SCALE,
    STATION_WIDTH_MIN_STATION_DAYS,
    STATION_WIDTH_SHRINK_KAPPA_STATION_DAYS,
    STATION_WIDTH_Z_CLIP,
    _station_cell_z,
    _truth,
    serving_cells_by_day,
)
from bayes_basket_target.cross_consensus import (  # noqa: E402
    apply_band_reliability,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
ARMS = ("A_neither", "B_station", "C_band", "D_station_then_band")


def _clip(value: float) -> float:
    return min(MAX_STATION_WIDTH_SCALE, max(MIN_STATION_WIDTH_SCALE, value))


def fit_station_scales(
    by_day: dict, truth: dict, train_days: list[str]
) -> dict[str, float]:
    """The shipped rule: station-days, clipped z, shrunk toward the fleet."""

    zs: dict[str, list[float]] = defaultdict(list)
    days: dict[str, set[str]] = defaultdict(set)
    for day in train_days:
        for cell in by_day[day]:
            actual = truth.get((cell["station_id"], cell["event_date"]))
            if actual is None:
                continue
            z = _station_cell_z(cell, actual)
            if z is None:
                continue
            if abs(z) > STATION_WIDTH_Z_CLIP:
                z = math.copysign(STATION_WIDTH_Z_CLIP, z)
            zs[cell["station_id"]].append(z)
            days[cell["station_id"]].add(str(cell["event_date"]))
    raw = {
        station: statistics.pstdev(values)
        for station, values in zs.items()
        if len(days[station]) >= STATION_WIDTH_MIN_STATION_DAYS
    }
    if not raw:
        return {}
    fleet = statistics.median(raw.values())
    if fleet <= 0:
        return {}
    out = {}
    for station, value in raw.items():
        n = float(len(days[station]))
        weight = n / (n + STATION_WIDTH_SHRINK_KAPPA_STATION_DAYS)
        out[station] = _clip(
            math.exp(weight * math.log(max(value, 1e-6) / fleet))
        )
    return out


def scaled(cell: dict, scale: float) -> dict[str, float]:
    if scale == 1.0:
        return dict(cell["probabilities"])
    adjusted, _ = apply_center_width_calibration(
        cell["probabilities"],
        delta_c=0.0,
        width_lambda=1.0,
        station_width_scale=scale,
        ordered_keys=cell["ordered_keys"],
        key_centers_c=cell["key_centers_c"],
        excluded_buckets=cell["excluded_buckets"],
    )
    total = sum(adjusted.values())
    return {k: v / total for k, v in adjusted.items()} if total > 0 else adjusted


def _records(probabilities: dict, cell: dict) -> list[tuple[float, int]]:
    """One station-day's rungs in bucket order, hit flag on the settled one."""

    return [
        (float(probabilities[key]), 1 if key == cell["settled_bucket"] else 0)
        for key in cell["ordered_keys"]
    ]


def fit_band(
    by_day: dict,
    truth: dict,
    train_days: list[str],
    scales: dict[str, float],
    axis: str,
    bootstrap_samples: int,
) -> list[tuple[tuple[float, float], float]]:
    """The band lane's own admission report, run on whatever the arm serves
    into it -- pre-station output for C, station-calibrated for D."""

    lane: dict[str, list] = defaultdict(list)
    stations: dict[str, list[str]] = defaultdict(list)
    for day in train_days:
        for cell in by_day[day]:
            if truth.get((cell["station_id"], cell["event_date"])) is None:
                continue
            probabilities = scaled(
                cell, float(scales.get(cell["station_id"], 1.0))
            )
            lane[day].append(_records(probabilities, cell))
            stations[day].append(str(cell["station_id"]))
    cells = cells_for_axis(axis)
    report = band_lane_report(
        dict(lane), bands=cells, bootstrap_samples=bootstrap_samples,
        axis=axis, stations_by_day=dict(stations),
        min_cell_stations=MIN_CELL_STATIONS,
    )
    return [
        (band, float(report[band]["conservative_factor"]))
        if band in report else (band, 1.0)
        for band in cells
    ]


def score(
    probabilities: dict, cell: dict, truth_c: float
) -> dict[str, float]:
    keys = list(cell["ordered_keys"])
    centers = list(cell["key_centers_c"])
    values = [float(probabilities[key]) for key in keys]
    total = sum(values)
    values = [v / total for v in values] if total > 0 else values
    settled = keys.index(cell["settled_bucket"])
    nll = -math.log(max(values[settled], 1e-9))
    cumulative = 0.0
    rps = 0.0
    for index in range(len(values)):
        cumulative += values[index]
        rps += (cumulative - (1.0 if index >= settled else 0.0)) ** 2
    mean_c = sum(v * c for v, c in zip(values, centers))
    # PIT of the settled bucket: mass strictly below it, plus half its own.
    below = sum(values[:settled])
    pit = below + 0.5 * values[settled]
    return {
        "nll": nll,
        "rps": rps / max(len(values) - 1, 1),
        "bias_c": float(truth_c) - mean_c,
        "cov50": 1.0 if 0.25 <= pit <= 0.75 else 0.0,
        "cov80": 1.0 if 0.10 <= pit <= 0.90 else 0.0,
        "cov90": 1.0 if 0.05 <= pit <= 0.95 else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--stage", default="P0")
    parser.add_argument("--min-decision-at-utc", default="2026-08-21T00:00:00Z")
    parser.add_argument("--max-decision-at-utc", default="2026-09-06T15:48:00Z")
    parser.add_argument("--min-train-days", type=int, default=8)
    parser.add_argument("--bootstrap-samples", type=int, default=300)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    database = args.db.expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=120.0)
    conn.execute("PRAGMA busy_timeout=120000")
    truth = _truth(conn)
    by_day = serving_cells_by_day(
        conn, truth, stage=args.stage,
        min_decision_at_utc=args.min_decision_at_utc,
    )
    by_day = {
        day: [
            cell for cell in cells
            if str(cell["decision_at_utc"]) < args.max_decision_at_utc
        ]
        for day, cells in by_day.items()
    }
    by_day = {day: cells for day, cells in by_day.items() if cells}
    days = sorted(by_day)
    if len(days) <= args.min_train_days:
        print(f"not enough days: {len(days)}")
        return 1
    axis = band_table_axis(conn, stage=args.stage) or BAND_AXIS_CENTRE_DISTANCE

    per_arm: dict[str, list[dict]] = {arm: [] for arm in ARMS}
    per_arm_station: dict[str, dict[str, list[float]]] = {
        arm: defaultdict(list) for arm in ARMS
    }
    scored_days = []
    for index, forward in enumerate(days):
        train = days[:index]
        if len(train) < args.min_train_days:
            continue
        scales = fit_station_scales(by_day, truth, train)
        band_c = fit_band(by_day, truth, train, {}, axis, args.bootstrap_samples)
        band_d = fit_band(
            by_day, truth, train, scales, axis, args.bootstrap_samples
        )
        scored_days.append(forward)
        for cell in by_day[forward]:
            actual = truth.get((cell["station_id"], cell["event_date"]))
            if actual is None:
                continue
            station = str(cell["station_id"])
            scale = float(scales.get(station, 1.0))
            ordinals = {
                key: position
                for position, key in enumerate(cell["ordered_keys"])
            } if axis == BAND_AXIS_CENTRE_DISTANCE else None
            arms = {
                "A_neither": dict(cell["probabilities"]),
                "B_station": scaled(cell, scale),
            }
            arms["C_band"] = apply_band_reliability(
                arms["A_neither"], factors=band_c, ordinals=ordinals,
            )[0]
            arms["D_station_then_band"] = apply_band_reliability(
                arms["B_station"], factors=band_d, ordinals=ordinals,
            )[0]
            for arm, probabilities in arms.items():
                metrics = score(probabilities, cell, actual)
                per_arm[arm].append(metrics)
                per_arm_station[arm][station].append(metrics["nll"])

    summary = {
        "schema": "station_band_ablation_v1",
        "stage": args.stage,
        "axis": axis,
        "decision_window": [
            args.min_decision_at_utc, args.max_decision_at_utc
        ],
        "scored_days": scored_days,
        "cells": len(per_arm["A_neither"]),
        "arms": {},
    }
    for arm in ARMS:
        rows = per_arm[arm]
        if not rows:
            continue
        station_means = {
            station: statistics.mean(values)
            for station, values in per_arm_station[arm].items()
        }
        ordered = sorted(station_means.values())
        worst_decile = (
            statistics.mean(ordered[-max(1, len(ordered) // 10):])
            if ordered else None
        )
        summary["arms"][arm] = {
            "nll": statistics.mean(r["nll"] for r in rows),
            "rps": statistics.mean(r["rps"] for r in rows),
            "bias_c": statistics.mean(r["bias_c"] for r in rows),
            "cov50": statistics.mean(r["cov50"] for r in rows),
            "cov80": statistics.mean(r["cov80"] for r in rows),
            "cov90": statistics.mean(r["cov90"] for r in rows),
            "worst_station_decile_nll": worst_decile,
        }
    base = summary["arms"].get("A_neither", {}).get("nll")
    print(
        f"stage={args.stage} axis={axis} days={len(scored_days)} "
        f"cells={summary['cells']}"
    )
    header = (
        f"{'arm':22s} {'NLL':>8s} {'vs A':>8s} {'RPS':>7s} {'bias':>7s} "
        f"{'cov50':>6s} {'cov80':>6s} {'cov90':>6s} {'worstD':>7s}"
    )
    print(header)
    for arm in ARMS:
        row = summary["arms"].get(arm)
        if not row:
            continue
        print(
            f"{arm:22s} {row['nll']:8.4f} "
            f"{(base - row['nll']) if base else 0.0:+8.4f} "
            f"{row['rps']:7.4f} {row['bias_c']:+7.3f} "
            f"{row['cov50']:6.3f} {row['cov80']:6.3f} {row['cov90']:6.3f} "
            f"{row['worst_station_decile_nll']:7.3f}"
        )
    d = summary["arms"].get("D_station_then_band", {}).get("nll")
    b = summary["arms"].get("B_station", {}).get("nll")
    if d is not None and b is not None:
        verdict = "D beats B" if d < b else "D does NOT beat B"
        print(
            f"\nband increment over the station layer: {b - d:+.4f} nat"
            f" -- {verdict}."
        )
        if d >= b:
            print(
                "  On this window the pooled band buys nothing the station"
                " layer has not already bought.  Being the last layer is not"
                " a reason to keep a factor."
            )
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
