#!/usr/bin/env python3
"""The gate for the regional calibration's regression arm.

``regional_point_debias.FEATURE_CALIBRATION`` raises a regional run's
remaining peak by its fitted bias before the temperature path's regression
reads the headroom off it.  Whether that serves is answered here, the way
charter 95 step two answered whether the features serve at all: every event
day's regression refitted on strictly earlier days, the chain in the form
production runs it (reanchor off), the same rows scored under both feature
builds, differences clustered by station-day.

Two sample sets, identical but for the switch, built by the sample builder
with the replay table each morning could have read:

    python3 scripts/fit_regional_point_debias.py
    python3 scripts/build_metar_phase_hazard_samples.py --from 2026-08-10 \\
        --through <yesterday> --out raw.db --regional-calibration off
    python3 scripts/build_metar_phase_hazard_samples.py --from 2026-08-10 \\
        --through <yesterday> --out cal.db --regional-calibration on
    python3 scripts/measure_regional_feature_calibration.py \\
        --raw raw.db --calibrated cal.db

Arms: ``base`` (no regional features), ``raw`` and ``calibrated`` (each
regression refitted on its own build), and ``shipped_raw`` /
``shipped_calibrated`` -- the serving hazard artifact fed each build, which is
what flipping the switch without a refit would do.  The arm's claim is
``calibrated`` against ``raw``; the switch moves only with a refit.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.materialization import (  # noqa: E402
    _anchor_bucket_for_temperature,
)
from bayes_basket_target.metar_four_layer import (  # noqa: E402
    compile_four_layer_posterior,
    feature_row,
)
from bayes_basket_target.metar_trajectory import (  # noqa: E402
    hard_bound_excluded_buckets,
)
import evaluate_metar_four_layer_walk_forward as E  # noqa: E402
import fit_metar_phase_hazard_artifact as H  # noqa: E402

ARMS = ("base", "raw", "calibrated", "shipped_raw", "shipped_calibrated")
METRICS = (
    ("hit", "命中"), ("within_one", "±1桶"), ("log_score", "对数分"),
    ("brier", "Brier"), ("centre_abs_error_c", "中心MAE"),
)
SHIPPED = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "metar_phase_hazard_artifact.json"
)


def _load(path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(row) for row in conn.execute(
                "SELECT * FROM metar_phase_hazard_sample"
                " ORDER BY event_date, station_id, as_of_utc"
            )
        ]
    finally:
        conn.close()


def _regional_fit(rows: Sequence[Mapping[str, Any]], day: str) -> dict | None:
    """``+regional_full`` over one build: the whole fleet, both features."""

    everything = [row for row in rows if str(row["event_date"]) < day]
    covered = [row for row in everything if row["regional_headroom_c"] is not None]
    if (
        len(covered) < 400
        or len({row["event_date"] for row in covered}) < 3
        or sum(1 for row in covered if int(row["delta_c"]) >= 1) < 100
    ):
        return None
    try:
        return E.hazard_artifact(
            everything,
            phase_names=H.PHASE_FEATURES + H.REGIONAL_FEATURES,
            full_names=H.FULL_FEATURES + H.REGIONAL_FEATURES,
        )
    except (SystemExit, ValueError):
        return None


def _features(sample: Mapping[str, Any]) -> dict[str, Any]:
    return feature_row(
        remaining_hours=float(sample["remaining_hours"]),
        drawdown_from_high_c=float(sample["drawdown_from_high_c"]),
        momentum_c_per_hour=float(sample["momentum_c_per_hour"]),
        cloud_cover_fraction=float(sample["cloud_cover_fraction"]),
        precipitation_present=bool(sample["precipitation_present"]),
        dewpoint_depression_c=float(sample["dewpoint_depression_c"]),
        wind_speed_mps=float(sample["wind_speed_mps"]),
        warming_so_far_c=float(sample["warming_so_far_c"]),
        curve_shape_realized=sample["curve_shape_realized"],
        regional_headroom_c=sample["regional_headroom_c"],
        regional_live_gap_c=sample["regional_live_gap_c"],
    )


def score_rows(
    raw: Sequence[Mapping[str, Any]],
    calibrated: Sequence[Mapping[str, Any]],
    *,
    target: sqlite3.Connection,
    shipped: Mapping[str, Any],
    min_train_days: int,
) -> list[dict[str, Any]]:
    by_id = {str(row["sample_id"]): row for row in calibrated}
    by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in raw:
        by_day[str(row["event_date"])].append(row)
    roots = E.RootCache(target)
    scored: list[dict[str, Any]] = []
    for index, day in enumerate(sorted(by_day)):
        if index < min_train_days:
            continue
        fitted = E.walk_forward_artifacts(raw, day)
        raw_fit = _regional_fit(raw, day)
        cal_fit = _regional_fit(calibrated, day)
        if fitted is None or raw_fit is None or cal_fit is None:
            continue
        base = fitted[0]
        for sample in by_day[day]:
            twin = by_id.get(str(sample["sample_id"]))
            view = roots.get(str(sample["root_checkpoint_id"]))
            if twin is None or view is None:
                continue
            high = float(sample["running_high_c"])
            excluded = hard_bound_excluded_buckets(
                sorted(view["ordinals"], key=lambda b: view["ordinals"][b]),
                view["uppers"], high,
            )
            lineage = {"bucket_intervals_c_json": view["intervals_json"]}
            truth_bucket = _anchor_bucket_for_temperature(
                float(sample["truth_tmax_c"]), root_lineage=lineage,
                bucket_ordinals=view["ordinals"],
            )
            high_bucket = _anchor_bucket_for_temperature(
                high, root_lineage=lineage, bucket_ordinals=view["ordinals"],
            )
            if (
                truth_bucket is None or high_bucket is None
                or truth_bucket in set(excluded)
            ):
                continue
            common = {
                "root_probabilities": view["probabilities"],
                "bucket_ordinals": view["ordinals"],
                "bucket_centers_c": view["centers"],
                "bucket_intervals_c": view["intervals"],
                "hard_excluded_buckets": excluded,
                "running_high_c": high,
                "running_high_bucket": high_bucket,
                "bias_recent_c": sample["root_bias_recent_c"],
                "bias_report_count": int(sample["root_bias_report_count"]),
            }
            distributions: dict[str, Mapping[str, float]] = {}
            try:
                for arm, artifact, row in (
                    ("base", base, sample), ("raw", raw_fit, sample),
                    ("calibrated", cal_fit, twin),
                    ("shipped_raw", shipped, sample),
                    ("shipped_calibrated", shipped, twin),
                ):
                    distributions[arm], _ = compile_four_layer_posterior(
                        **common, features=_features(row),
                        hazard_artifact=artifact, reanchor_artifact=None,
                        reanchor_enabled=False,
                    )
            except ContractError:
                continue
            scored.append({
                "station_id": str(sample["station_id"]),
                "event_date": day,
                "as_of_utc": str(sample["as_of_utc"]),
                "settlement_timezone": str(sample["settlement_timezone"]),
                "weight": float(sample["station_day_weight"]),
                "pre_peak": int(sample["delta_c"]) >= 1,
                "covered": (
                    sample["regional_headroom_c"] is not None
                    or twin["regional_headroom_c"] is not None
                ),
                "applied": twin.get("regional_calibration_bias_c") is not None,
                "bias_c": twin.get("regional_calibration_bias_c"),
                "system_id": (
                    sample["regional_system_id"] or twin["regional_system_id"]
                ),
                "scores": {
                    arm: E.score(
                        distribution, truth_bucket=truth_bucket,
                        ordinals=view["ordinals"], centers=view["centers"],
                        truth_c=float(sample["truth_tmax_c"]),
                    )
                    for arm, distribution in distributions.items()
                },
            })
        print(f"  {day}: {len(scored)}", file=sys.stderr)
    return scored


def summarise(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    total = sum(row["weight"] for row in rows) or 1.0
    return {
        arm: {
            metric: sum(row["weight"] * row["scores"][arm][metric] for row in rows)
            / total
            for metric, _ in METRICS
        }
        for arm in ARMS
    }


def paired(
    rows: Sequence[Mapping[str, Any]], a: str, b: str, *,
    replicates: int = 2000, seed: int = 7,
) -> tuple[float, float, float, float]:
    """``a - b`` in log score, clustered by station-day: (point, lo, hi, P(<=0))."""

    # Each station-day reduces to one (weighted difference, weight) pair
    # before resampling, so a replicate costs clusters, not rows.
    sums: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for row in rows:
        cell = sums[(row["station_id"], row["event_date"])]
        cell[0] += row["weight"] * (
            row["scores"][a]["log_score"] - row["scores"][b]["log_score"]
        )
        cell[1] += row["weight"]
    cells = list(sums.values())

    def statistic(chosen: Sequence[list[float]]) -> float:
        denominator = sum(cell[1] for cell in chosen)
        return sum(cell[0] for cell in chosen) / denominator if denominator else 0.0

    rng = random.Random(seed)
    draws = sorted(
        statistic([rng.choice(cells) for _ in cells]) for _ in range(replicates)
    )
    return (
        statistic(cells), draws[int(0.025 * replicates)],
        draws[int(0.975 * replicates)],
        sum(value <= 0 for value in draws) / replicates,
    )


def standing(rows: Sequence[Mapping[str, Any]], hour: int) -> list[Mapping[str, Any]]:
    held: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        zone = ZoneInfo(row["settlement_timezone"])
        instant = datetime.fromisoformat(row["as_of_utc"].replace("Z", "+00:00"))
        cutoff = datetime.fromisoformat(row["event_date"]).replace(
            hour=hour, tzinfo=zone
        )
        if instant <= cutoff:
            key = (row["station_id"], row["event_date"])
            if key not in held or held[key]["as_of_utc"] < row["as_of_utc"]:
                held[key] = row
    return list(held.values())


def block(title: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        print(f"\n{title}: 无样本")
        return None
    summary = summarise(rows)
    days = len({(row["station_id"], row["event_date"]) for row in rows})
    print(f"\n{title}  n={len(rows)}  站日={days}")
    print("  arm                 " + "".join(f"{label:>9}" for _, label in METRICS))
    for arm in ARMS:
        print(f"  {arm:<20}" + "".join(
            f"{summary[arm][metric]:9.4f}" for metric, _ in METRICS
        ))
    verdicts = {}
    for a, b in (
        ("calibrated", "raw"), ("shipped_calibrated", "shipped_raw"),
        ("raw", "base"), ("calibrated", "base"),
    ):
        point, low, high, worse = paired(rows, a, b)
        verdicts[f"{a}-{b}"] = [point, low, high, worse]
        print(f"  Δ对数分 {a}−{b}: {point:+.4f} [{low:+.4f},{high:+.4f}]  P(≤0)={worse:.3f}")
    return {"n": len(rows), "station_days": days, "summary": summary,
            "paired": verdicts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--calibrated", type=Path, required=True)
    parser.add_argument(
        "--target-db", type=Path,
        default=ROOT / "data" / "bayes_basket_target_ssot.db",
    )
    parser.add_argument("--shipped", type=Path, default=SHIPPED)
    parser.add_argument("--min-train-days", type=int, default=7)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    target = sqlite3.connect(f"file:{args.target_db}?mode=ro", uri=True)
    rows = score_rows(
        _load(args.raw), _load(args.calibrated), target=target,
        shipped=json.loads(args.shipped.read_text(encoding="utf-8")),
        min_train_days=args.min_train_days,
    )
    covered = [row for row in rows if row["covered"]]
    applied = [row for row in covered if row["applied"]]
    report = {
        "all": block("== 全部 ==", rows),
        "covered": block("== 有区域覆盖 ==", covered),
        "applied": block("== 校正生效（峰前且有校准件）==", applied),
        "applied_large": block(
            "== 校正生效·|偏差|≥1.5°C ==",
            [row for row in applied if abs(float(row["bias_c"])) >= 1.5],
        ),
        "covered_pre_peak": block(
            "== 有区域覆盖·峰值前 ==", [row for row in covered if row["pre_peak"]]
        ),
        "noon": block("== 有区域覆盖·当地正午站立 ==", standing(covered, 12)),
        "systems": {
            system: block(f"== 系统 {system} ==", [
                row for row in covered if row["system_id"] == system
            ])
            for system in sorted({
                str(row["system_id"]) for row in covered if row["system_id"]
            })
        },
    }
    if args.out:
        args.out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
