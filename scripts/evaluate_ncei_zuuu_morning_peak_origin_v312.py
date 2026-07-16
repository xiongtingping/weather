#!/usr/bin/env python3
"""Research-only validation of a causal morning peak-origin interaction.

The candidate augments the v3.1 GFS daytime branch only for 06:00–10:00
Asia/Shanghai snapshots.  It never changes a fact, strategy parameter,
signal, order, or daemon.  The candidate's extra fields are all available at
the snapshot time: whether the current local-day running maximum was first
seen at midnight, its age, and the observed drop from that maximum.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import calibrate_ncei_zuuu_remaining_warming_v2 as v2
from scripts import train_ncei_zuuu_multimodel_allday_bayes_v31 as v31
from scripts import train_ncei_zuuu_remaining_warming_bayes as v1


SCHEMA = "ncei_zuuu_morning_peak_origin_validation_v312"
VERSION = "v3.1.2_morning_peak_origin_research"
MORNING_START_HOUR = 6.0
MORNING_END_HOUR = 10.0
ORIGIN_TOLERANCE_HOURS = 0.25
ORIGIN_NUMERIC = (
    "morning_origin_peak_age_h",
    "morning_origin_drop_c",
    "morning_origin_age_x_drop",
)
ORIGIN_BOOLEAN = "morning_pre_sunrise_peak_origin"


def _number(value: Any) -> float | None:
    return v1._number(value)


def morning_peak_origin(row: Mapping[str, Any]) -> bool:
    """Return whether the running high was established at local midnight.

    The state uses only current-snapshot fields.  A high re-established after
    midnight does not qualify, because its ``hours_since_observed_max`` is
    shorter than the elapsed local-day time.
    """

    hour = _number(row.get("local_hour"))
    peak_age = _number(row.get("hours_since_observed_max"))
    if hour is None or peak_age is None:
        return False
    return bool(
        MORNING_START_HOUR <= hour < MORNING_END_HOUR
        and peak_age >= hour - ORIGIN_TOLERANCE_HOURS
    )


class MorningPeakOriginDesign(v31.AllDayDesign):
    """v3.1 GFS design with causal morning-origin interactions only."""

    def _raw(self, rows: Sequence[Mapping[str, Any]], climo_values: Sequence[float]) -> np.ndarray:
        matrix: list[list[float]] = []
        for row, climo in zip(rows, climo_values):
            values = v31._values(row, climo)
            origin = morning_peak_origin(row)
            peak_age = _number(row.get("hours_since_observed_max")) or 0.0
            drop = _number(row.get("drop_from_observed_max_c")) or 0.0
            values.update(
                {
                    "morning_origin_peak_age_h": peak_age if origin else 0.0,
                    "morning_origin_drop_c": drop if origin else 0.0,
                    "morning_origin_age_x_drop": peak_age * drop if origin else 0.0,
                    ORIGIN_BOOLEAN: 1.0 if origin else 0.0,
                }
            )
            numeric = [float(values[name]) if values.get(name) is not None else float("nan") for name in self.numeric]
            missing = [1.0 if values.get(name) is None else 0.0 for name in self.numeric]
            boolean = [float(values.get(name) or 0.0) for name in self.boolean]
            matrix.append([*numeric, *missing, *boolean])
        return np.asarray(matrix, dtype=float)


def _baseline_v31(gfs: Sequence[Mapping[str, Any]], multimodel: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if len(gfs) != len(multimodel):
        raise ValueError("v31_gfs_and_multimodel_prediction_lengths_differ")
    result: list[dict[str, Any]] = []
    for gfs_row, multimodel_row in zip(gfs, multimodel):
        if (gfs_row.get("event_date_local"), gfs_row.get("local_hour")) != (
            multimodel_row.get("event_date_local"),
            multimodel_row.get("local_hour"),
        ):
            raise ValueError("v31_gfs_and_multimodel_prediction_keys_differ")
        result.append(dict(multimodel_row if float(gfs_row["local_hour"]) < MORNING_START_HOUR else gfs_row))
    return result


def _candidate_gated(
    gfs: Sequence[Mapping[str, Any]],
    multimodel: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not (len(gfs) == len(multimodel) == len(candidate)):
        raise ValueError("candidate_prediction_lengths_differ")
    result: list[dict[str, Any]] = []
    for gfs_row, multimodel_row, candidate_row in zip(gfs, multimodel, candidate):
        key = (gfs_row.get("event_date_local"), gfs_row.get("local_hour"))
        if key != (multimodel_row.get("event_date_local"), multimodel_row.get("local_hour")) or key != (
            candidate_row.get("event_date_local"),
            candidate_row.get("local_hour"),
        ):
            raise ValueError("candidate_prediction_keys_differ")
        hour = float(gfs_row["local_hour"])
        result.append(
            dict(
                candidate_row
                if MORNING_START_HOUR <= hour < MORNING_END_HOUR
                else (multimodel_row if hour < MORNING_START_HOUR else gfs_row)
            )
        )
    return result


def _origin_segment(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for raw in rows:
        if not morning_peak_origin(raw):
            continue
        drop = _number(raw.get("drop_from_observed_max_c"))
        if drop is None or drop < 2.0:
            continue
        selected.append(dict(raw))
    return selected


def _metric_value(row: Mapping[str, Any], metric: str) -> float:
    if metric in {"bucket_nll", "bucket_rps"}:
        return float(row[metric])
    if metric == "mae_c":
        return abs(float(row["expected_final_max_c"]) - float(row["final_daily_max_c"]))
    if metric == "continue_brier":
        actual = 1.0 if row.get("actual_continue") else 0.0
        return (float(row["predicted_continue_probability"]) - actual) ** 2
    raise KeyError(metric)


def _paired_day_validation(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
    *,
    bootstrap_replicates: int,
) -> dict[str, Any]:
    baseline_by_key = {(str(row["event_date_local"]), float(row["local_hour"])): row for row in baseline}
    candidate_by_key = {(str(row["event_date_local"]), float(row["local_hour"])): row for row in candidate}
    keys = sorted(set(baseline_by_key) & set(candidate_by_key))
    by_day: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for key in keys:
        by_day[key[0]].append(key)
    metrics: dict[str, Any] = {}
    rng = np.random.default_rng(312)
    for metric in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier"):
        day_deltas = np.asarray(
            [
                float(
                    np.mean(
                        [
                            _metric_value(candidate_by_key[key], metric) - _metric_value(baseline_by_key[key], metric)
                            for key in day_keys
                        ]
                    )
                )
                for day_keys in by_day.values()
            ],
            dtype=float,
        )
        if not len(day_deltas):
            metrics[metric] = {"candidate_minus_baseline_mean": None, "bootstrap_95pct_ci": None}
            continue
        indexes = rng.integers(0, len(day_deltas), size=(max(100, int(bootstrap_replicates)), len(day_deltas)))
        bootstrap_means = day_deltas[indexes].mean(axis=1)
        metrics[metric] = {
            "candidate_minus_baseline_mean": float(day_deltas.mean()),
            "candidate_minus_baseline_median": float(np.median(day_deltas)),
            "bootstrap_95pct_ci": [
                float(np.quantile(bootstrap_means, 0.025)),
                float(np.quantile(bootstrap_means, 0.975)),
            ],
            "candidate_better_day_count": int(np.sum(day_deltas < 0.0)),
            "candidate_worse_day_count": int(np.sum(day_deltas > 0.0)),
            "equal_day_count": int(np.sum(np.isclose(day_deltas, 0.0))),
        }
    return {
        "pairing_grain": "event_date_local_equal_weight_within_day",
        "paired_day_count": len(by_day),
        "paired_snapshot_count": len(keys),
        "metrics": metrics,
    }


def _improvement(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, float]:
    return {
        key: 100.0 * (float(baseline[key]) - float(candidate[key])) / float(baseline[key])
        for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")
    }


def _render(report: Mapping[str, Any]) -> str:
    gains = report["origin_segment"]["improvement_vs_v31_pct"]
    gate = report["release_gate"]
    return "\n".join(
        [
            "# ZUUU 上午午夜高点来源交互：研究验证",
            "",
            f"- 结论：`{gate['decision']}`。",
            "- 候选仅在 06:00–10:00 本地时段使用因果峰值来源交互；不会对概率设上限或下限。",
            f"- 午夜高点且回落≥2°C 子群相对 v3.1：NLL `{gains['bucket_nll']:+.3f}%`、RPS `{gains['bucket_rps']:+.3f}%`、MAE `{gains['mae_c']:+.3f}%`、Brier `{gains['continue_brier']:+.3f}%`。",
            "",
            "生产替换固定为否；只有全部门槛通过后才允许进入 shadow。",
            "",
        ]
    )


def evaluate(features_path: Path, *, bootstrap_replicates: int) -> dict[str, Any]:
    rows = v31._load(features_path)
    years = sorted({int(str(row["event_date_local"])[:4]) for row in rows})
    if years != [2021, 2022, 2023, 2024, 2025]:
        raise ValueError(f"unexpected_training_years:{years}")

    baseline_all: list[dict[str, Any]] = []
    candidate_all: list[dict[str, Any]] = []
    folds: list[dict[str, Any]] = []
    candidate_numeric = [*v31.GFS_NUMERIC, *ORIGIN_NUMERIC]
    candidate_boolean = [*v31.BOOLEAN, ORIGIN_BOOLEAN]
    for test_year in years[1:]:
        train = [row for row in rows if int(str(row["event_date_local"])[:4]) < test_year]
        test = [row for row in rows if int(str(row["event_date_local"])[:4]) == test_year]
        _, _, gfs = v2._fit(train, test, version="v31_gfs", design_override=v31.AllDayDesign(v31.GFS_NUMERIC, v31.BOOLEAN))
        _, _, multimodel = v2._fit(
            train,
            test,
            version="v31_multimodel",
            design_override=v31.AllDayDesign(v31.MULTIMODEL_NUMERIC, v31.BOOLEAN),
        )
        _, _, origin_candidate = v2._fit(
            train,
            test,
            version=VERSION,
            design_override=MorningPeakOriginDesign(candidate_numeric, candidate_boolean),
        )
        baseline = _baseline_v31(gfs, multimodel)
        candidate = _candidate_gated(gfs, multimodel, origin_candidate)
        baseline_all.extend(baseline)
        candidate_all.extend(candidate)
        folds.append(
            {
                "test_year": test_year,
                "train_day_count": len({str(row["event_date_local"]) for row in train}),
                "test_day_count": len({str(row["event_date_local"]) for row in test}),
                "baseline_v31": v2._weighted_summary(baseline),
                "candidate": v2._weighted_summary(candidate),
                "origin_segment_baseline": v2._weighted_summary(_origin_segment(baseline)),
                "origin_segment_candidate": v2._weighted_summary(_origin_segment(candidate)),
            }
        )

    baseline_origin = _origin_segment(baseline_all)
    candidate_origin = _origin_segment(candidate_all)
    baseline_summary = v2._weighted_summary(baseline_all)
    candidate_summary = v2._weighted_summary(candidate_all)
    origin_baseline_summary = v2._weighted_summary(baseline_origin)
    origin_candidate_summary = v2._weighted_summary(candidate_origin)
    paired = _paired_day_validation(
        baseline_origin,
        candidate_origin,
        bootstrap_replicates=bootstrap_replicates,
    )
    gains = _improvement(origin_baseline_summary, origin_candidate_summary)
    paired_metrics = paired["metrics"]
    nll_ci = paired_metrics["bucket_nll"]["bootstrap_95pct_ci"]
    rps_ci = paired_metrics["bucket_rps"]["bootstrap_95pct_ci"]
    ready_for_shadow = bool(
        all(gains[key] > 0.0 for key in ("bucket_nll", "bucket_rps", "mae_c"))
        and gains["continue_brier"] >= 0.0
        and nll_ci is not None
        and rps_ci is not None
        and nll_ci[1] <= 0.0
        and rps_ci[1] <= 0.0
    )
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": {
            "split": "strict_expanding_year_walk_forward_2022_2025",
            "timezone": "Asia/Shanghai",
            "baseline": "v3.1 pre_dawn_multimodel_then_gfs_06_23",
            "candidate": "gfs_06_10_with_causal_midnight_peak_origin_interactions",
            "candidate_application_window_local": "06:00-10:00",
            "candidate_features": [*ORIGIN_NUMERIC, ORIGIN_BOOLEAN],
            "origin_definition": "hours_since_observed_max >= local_hour - 0.25 within 06:00-10:00",
            "current_day_final_max_used_as_feature": False,
        },
        "data_quality": {
            "feature_path": str(features_path),
            "row_count": len(rows),
            "day_count": len({str(row["event_date_local"]) for row in rows}),
            "years": years,
        },
        "folds": folds,
        "all_rows": {
            "baseline_v31": baseline_summary,
            "candidate": candidate_summary,
            "improvement_vs_v31_pct": _improvement(baseline_summary, candidate_summary),
        },
        "origin_segment": {
            "definition": "06:00-10:00; running high set at local midnight; observed drop from high >= 2C",
            "baseline_v31": origin_baseline_summary,
            "candidate": origin_candidate_summary,
            "improvement_vs_v31_pct": gains,
            "paired_day_validation": paired,
        },
        "release_gate": {
            "ready_for_shadow": ready_for_shadow,
            "production_ready": False,
            "decision": "advance_to_shadow_only" if ready_for_shadow else "retain_v31_no_runtime_change",
            "production_blockers": [
                "amsc_half_hour_and_settlement_label_validation_required",
                "live_shadow_stability_not_observed",
                "production_replacement_not_authorized_by_this_research_audit",
            ],
        },
        "guards": {
            "historical_backfill": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "changes_parameters": False,
            "restarts_launchd": False,
            "production_integration": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, help="Optional research-only report destination.")
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    args = parser.parse_args()
    report = evaluate(args.features, bootstrap_replicates=max(100, int(args.bootstrap_replicates)))
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "morning_peak_origin_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (args.output_dir / "morning_peak_origin_report.md").write_text(_render(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "decision": report["release_gate"]["decision"],
                "ready_for_shadow": report["release_gate"]["ready_for_shadow"],
                "origin_segment_improvement_vs_v31_pct": report["origin_segment"]["improvement_vs_v31_pct"],
                "output_dir": str(args.output_dir) if args.output_dir else None,
                "guards": report["guards"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
