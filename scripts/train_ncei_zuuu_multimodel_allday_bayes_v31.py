#!/usr/bin/env python3
"""Train and release-gate an all-day GFS+JMA ZUUU v3.1 candidate."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import calibrate_ncei_zuuu_remaining_warming_v2 as v2
from scripts import join_ncei_zuuu_multimodel_synoptic_features as joiner
from scripts import train_ncei_zuuu_remaining_warming_bayes as v1
from scripts import train_ncei_zuuu_synoptic_bayes_v3 as v3


SCHEMA = "ncei_zuuu_multimodel_allday_bayes_release_v31"
MODEL_SCHEMA = "ncei_zuuu_zero_inflated_multimodel_allday_bayes_artifact_v31"
CORE_MODELS = ("gfs", "jma")
MODEL_ENGINEERED = tuple(v3.SYNOPTIC_ENGINEERED)
CONSENSUS_NUMERIC = tuple(
    f"mm_{stat}__{field}"
    for field in joiner.CONSENSUS_FIELDS
    for stat in ("mean", "spread")
)


def _number(value: Any) -> float | None:
    return v1._number(value)


def _model_numeric(model: str) -> list[str]:
    return [
        *[f"{model}__{field}" for field in v3.SYNOPTIC_DIRECT],
        *[f"{model}__{field}" for field in MODEL_ENGINEERED],
    ]


GFS_NUMERIC = [*v2.V2_NUMERIC, *_model_numeric("gfs")]
MULTIMODEL_NUMERIC = [
    *v2.V2_NUMERIC,
    *_model_numeric("gfs"),
    *_model_numeric("jma"),
    *CONSENSUS_NUMERIC,
]
BOOLEAN = list(v2.V2_BOOLEAN)


def _model_values(row: Mapping[str, Any], model: str) -> dict[str, float | None]:
    prefix = f"{model}__"
    output = {f"{model}__{field}": _number(row.get(prefix + field)) for field in v3.SYNOPTIC_DIRECT}
    hgt500 = _number(row.get(prefix + "basin_center_hgt_500_m"))
    hgt850 = _number(row.get(prefix + "basin_center_hgt_850_m"))
    air850 = _number(row.get(prefix + "basin_center_air_850_c"))
    surface = _number(row.get("temperature_c"))
    solar = _number(row.get("solar_heating_window"))
    rh700 = _number(row.get(prefix + "basin_center_rhum_700_pct"))
    cloud = _number(row.get("cloud_cover_pct"))
    ascent = _number(row.get(prefix + "synoptic_ascent_moisture_proxy"))
    dewpoint = _number(row.get("dewpoint_c"))
    engineered = {
        "synoptic_hgt500_minus_hgt850_m": hgt500 - hgt850 if hgt500 is not None and hgt850 is not None else None,
        "synoptic_surface_air850_gap_c": surface - air850 if surface is not None and air850 is not None else None,
        "interaction_air850_solar": air850 * solar if air850 is not None and solar is not None else None,
        "interaction_rh700_cloud": rh700 * cloud / 100.0 if rh700 is not None and cloud is not None else None,
        "interaction_ascent_surface_moisture": ascent * dewpoint if ascent is not None and dewpoint is not None else None,
    }
    output.update({f"{model}__{key}": value for key, value in engineered.items()})
    return output


def _values(row: Mapping[str, Any], climo: float) -> dict[str, float | None]:
    output = dict(v2._values(row, climo))
    for model in CORE_MODELS:
        output.update(_model_values(row, model))
    for name in CONSENSUS_NUMERIC:
        output[name] = _number(row.get(name))
    return output


class AllDayDesign(v1.Design):
    def _raw(self, rows: Sequence[Mapping[str, Any]], climo_values: Sequence[float]) -> np.ndarray:
        matrix = []
        for row, climo in zip(rows, climo_values):
            values = _values(row, climo)
            numeric = [float(values[name]) if values.get(name) is not None else float("nan") for name in self.numeric]
            missing = [1.0 if values.get(name) is None else 0.0 for name in self.numeric]
            boolean = [float(values.get(name) or 0.0) for name in self.boolean]
            matrix.append([*numeric, *missing, *boolean])
        return np.asarray(matrix, dtype=float)


def _load(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            year = int(str(row.get("event_date_local") or "0000")[:4])
            hour = _number(row.get("local_hour"))
            if (
                2021 <= year <= 2025
                and row.get("multimodel_join_available")
                and row.get("daily_max_label_eligible")
                and not row.get("core_qc_bad")
                and hour is not None
                and 0.0 <= hour < 24.0
                and _number(row.get("remaining_warming_c")) is not None
                and _number(row.get("observed_max_to_time_c")) is not None
            ):
                rows.append(row)
    return rows


def _improvement(old: float, new: float) -> float:
    return 100.0 * (old - new) / old if old else 0.0


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return v2._weighted_summary(rows)


def _segments(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    definitions = {
        "all": lambda row: True,
        "pre_dawn_00_05": lambda row: 0.0 <= float(row["local_hour"]) < 6.0,
        "morning_06_09": lambda row: 6.0 <= float(row["local_hour"]) < 10.0,
        "daytime_10_17": lambda row: 10.0 <= float(row["local_hour"]) < 18.0,
        "evening_18_23": lambda row: 18.0 <= float(row["local_hour"]) < 24.0,
        "hot_season_all": lambda row: 5 <= int(row["month"]) <= 9,
        "hot_season_pre_dawn": lambda row: 5 <= int(row["month"]) <= 9 and float(row["local_hour"]) < 6.0,
        "hot_season_daytime": lambda row: 5 <= int(row["month"]) <= 9 and 10.0 <= float(row["local_hour"]) < 18.0,
    }
    return {name: _summary([row for row in rows if predicate(row)]) for name, predicate in definitions.items()}


def _gains(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, float]:
    return {
        key: _improvement(float(left[key]), float(right[key]))
        for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")
    }


def _climo_by_doy(rows: list[dict[str, Any]]) -> dict[str, float]:
    table, per_day = v1._climo_table(rows)
    fallback = sum(per_day.values()) / len(per_day) if per_day else 30.0
    return {str(doy): float(total / count if count else fallback) for doy, (total, count) in table.items()}


def _feature_importance(artifact: Mapping[str, Any], limit: int = 30) -> list[dict[str, Any]]:
    output = []
    for name, value in zip(artifact["feature_names"], artifact["peak_already_coefficients"]):
        if name.startswith(("gfs__", "jma__", "mm_")):
            output.append({"feature": name, "coefficient": round(float(value), 6), "absolute": round(abs(float(value)), 6)})
    return sorted(output, key=lambda row: float(row["absolute"]), reverse=True)[:limit]


def _render(report: Mapping[str, Any]) -> str:
    aggregate = report["aggregate"]
    gain_v2 = report["improvement_release_candidate_vs_v2_pct"]
    gain_gfs = report["improvement_release_candidate_vs_gfs_pct"]
    return "\n".join(
        [
            "# ZUUU 全天多模式 Bayes v3.1：发布验证",
            "",
            f"- 结论：`{'通过' if report['release_gate']['production_ready'] else '未通过'}`。",
            f"- 分时v3.1相对全天v2：NLL `{gain_v2['bucket_nll']:+.3f}%`、RPS `{gain_v2['bucket_rps']:+.3f}%`、MAE `{gain_v2['mae_c']:+.3f}%`。",
            f"- 分时v3.1相对全天GFS：NLL `{gain_gfs['bucket_nll']:+.3f}%`、RPS `{gain_gfs['bucket_rps']:+.3f}%`、MAE `{gain_gfs['mae_c']:+.3f}%`。",
            "",
            "| 模型 | NLL | RPS | MAE °C | Brier |",
            "|---|---:|---:|---:|---:|",
            f"| 全天v2 | {aggregate['v2']['bucket_nll']:.6f} | {aggregate['v2']['bucket_rps']:.8f} | {aggregate['v2']['mae_c']:.6f} | {aggregate['v2']['continue_brier']:.6f} |",
            f"| 全天GFS | {aggregate['v31_gfs']['bucket_nll']:.6f} | {aggregate['v31_gfs']['bucket_rps']:.8f} | {aggregate['v31_gfs']['mae_c']:.6f} | {aggregate['v31_gfs']['continue_brier']:.6f} |",
            f"| 全天GFS＋JMA | {aggregate['v31_multimodel']['bucket_nll']:.6f} | {aggregate['v31_multimodel']['bucket_rps']:.8f} | {aggregate['v31_multimodel']['mae_c']:.6f} | {aggregate['v31_multimodel']['continue_brier']:.6f} |",
            f"| 分时v3.1（凌晨GFS＋JMA，其余GFS） | {aggregate['v31_regime_gated']['bucket_nll']:.6f} | {aggregate['v31_regime_gated']['bucket_rps']:.8f} | {aggregate['v31_regime_gated']['mae_c']:.6f} | {aggregate['v31_regime_gated']['continue_brier']:.6f} |",
            "",
            "- 每条机场观测只连接各模式不晚于该观测的最近六小时状态；模式背景不能覆盖AMSC硬实况。",
            "- ICON/GEM/ECMWF/CMA因完整同分布历史较短，保留为影子共识，不与五年核心模式等权。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--join-report", type=Path, required=True)
    parser.add_argument("--gfs-manifest", type=Path, required=True)
    parser.add_argument("--jma-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    rows = _load(args.features)
    if not rows:
        raise SystemExit("no all-day causally joined GFS+JMA rows")
    years = sorted({int(str(row["event_date_local"])[:4]) for row in rows})
    if years != [2021, 2022, 2023, 2024, 2025]:
        raise SystemExit(f"unexpected training years: {years}")
    join_report = json.loads(args.join_report.read_text(encoding="utf-8"))
    gfs_manifest = json.loads(args.gfs_manifest.read_text(encoding="utf-8"))
    jma_manifest = json.loads(args.jma_manifest.read_text(encoding="utf-8"))
    if int(join_report.get("future_join_violation_count") or 0) != 0:
        raise SystemExit("future multimodel join violation")
    if str(gfs_manifest.get("model")) != "gfs_seamless" or str(jma_manifest.get("model")) != "jma_gsm":
        raise SystemExit("historical/live model contract mismatch")

    folds: list[dict[str, Any]] = []
    predictions: dict[str, list[dict[str, Any]]] = {
        "v2": [],
        "v31_gfs": [],
        "v31_multimodel": [],
        "v31_regime_gated": [],
    }
    for test_year in years[1:]:
        train = [row for row in rows if int(str(row["event_date_local"])[:4]) < test_year]
        test = [row for row in rows if int(str(row["event_date_local"])[:4]) == test_year]
        fold: dict[str, Any] = {
            "test_year": test_year,
            "train_day_count": len({row["event_date_local"] for row in train}),
            "test_day_count": len({row["event_date_local"] for row in test}),
        }
        fold_predictions_by_version: dict[str, list[dict[str, Any]]] = {}
        for version, design in (
            ("v2", v2.V2Design(v2.V2_NUMERIC, v2.V2_BOOLEAN)),
            ("v31_gfs", AllDayDesign(GFS_NUMERIC, BOOLEAN)),
            ("v31_multimodel", AllDayDesign(MULTIMODEL_NUMERIC, BOOLEAN)),
        ):
            metrics, _, fold_predictions = v2._fit(train, test, version=version, design_override=design)
            fold[version] = metrics
            fold_predictions_by_version[version] = fold_predictions
            predictions[version].extend(fold_predictions)
        gated = [
            dict(multimodel if float(gfs["local_hour"]) < 6.0 else gfs)
            for gfs, multimodel in zip(
                fold_predictions_by_version["v31_gfs"],
                fold_predictions_by_version["v31_multimodel"],
            )
        ]
        fold["v31_regime_gated"] = _summary(gated)
        predictions["v31_regime_gated"].extend(gated)
        folds.append(fold)

    _, multimodel_artifact, _ = v2._fit(
        rows,
        rows,
        version="v31_multimodel",
        design_override=AllDayDesign(MULTIMODEL_NUMERIC, BOOLEAN),
    )
    _, gfs_artifact, _ = v2._fit(
        rows,
        rows,
        version="v31_gfs",
        design_override=AllDayDesign(GFS_NUMERIC, BOOLEAN),
    )
    aggregate = {name: _summary(values) for name, values in predictions.items()}
    segments = {name: _segments(values) for name, values in predictions.items()}
    gain_v2 = _gains(aggregate["v2"], aggregate["v31_regime_gated"])
    gain_gfs = _gains(aggregate["v31_gfs"], aggregate["v31_regime_gated"])
    raw_multimodel_gain_v2 = _gains(aggregate["v2"], aggregate["v31_multimodel"])
    raw_multimodel_gain_gfs = _gains(aggregate["v31_gfs"], aggregate["v31_multimodel"])
    pre_dawn_v2 = _gains(segments["v2"]["pre_dawn_00_05"], segments["v31_multimodel"]["pre_dawn_00_05"])
    pre_dawn_gfs = _gains(segments["v31_gfs"]["pre_dawn_00_05"], segments["v31_multimodel"]["pre_dawn_00_05"])
    daytime_gfs = _gains(segments["v31_gfs"]["daytime_10_17"], segments["v31_multimodel"]["daytime_10_17"])
    year_wins_v2 = sum(
        float(fold["v31_regime_gated"]["bucket_nll"]) < float(fold["v2"]["bucket_nll"])
        and float(fold["v31_regime_gated"]["bucket_rps"]) < float(fold["v2"]["bucket_rps"])
        for fold in folds
    )
    year_wins_gfs = sum(
        float(fold["v31_regime_gated"]["bucket_nll"]) < float(fold["v31_gfs"]["bucket_nll"])
        and float(fold["v31_regime_gated"]["bucket_rps"]) < float(fold["v31_gfs"]["bucket_rps"])
        for fold in folds
    )
    coverage = float(join_report.get("all_models_join_rate_pct") or 0.0)
    production_ready = bool(
        len(folds) == 4
        and all(gain_v2[key] > 0.0 for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier"))
        and all(gain_gfs[key] > 0.0 for key in ("bucket_nll", "bucket_rps", "mae_c"))
        and gain_gfs["continue_brier"] > -0.25
        and all(pre_dawn_v2[key] > 0.0 for key in ("bucket_nll", "bucket_rps", "mae_c"))
        and pre_dawn_gfs["bucket_nll"] > 0.0
        and pre_dawn_gfs["bucket_rps"] > 0.0
        and abs(float(segments["v31_regime_gated"]["daytime_10_17"]["bucket_nll"]) - float(segments["v31_gfs"]["daytime_10_17"]["bucket_nll"])) < 1e-12
        and abs(float(segments["v31_regime_gated"]["daytime_10_17"]["bucket_rps"]) - float(segments["v31_gfs"]["daytime_10_17"]["bucket_rps"])) < 1e-12
        and year_wins_v2 >= 3
        and year_wins_gfs >= 2
        and coverage >= 90.0
    )
    blockers = [] if production_ready else ["strict_multimodel_allday_release_gate_failed"]
    shared_artifact = {
            "schema": MODEL_SCHEMA,
            "version": "v3.1_multimodel_allday",
            "trained_through_year": 2025,
            "training_sample_count": len(rows),
            "training_day_count": len({row["event_date_local"] for row in rows}),
            "trained_local_hour_min": min(float(row["local_hour"]) for row in rows),
            "trained_local_hour_max": max(float(row["local_hour"]) for row in rows),
            "climo_by_day_of_year_c": _climo_by_doy(rows),
            "source_contract": {
                "transport": "Open-Meteo",
                "historical_endpoint": "historical-forecast-api.open-meteo.com/v1/forecast",
                "live_endpoint": "api.open-meteo.com/v1/forecast",
                "core_models": {"gfs": "gfs_seamless", "jma": "jma_gsm"},
                "same_core_variables_historical_and_live": True,
                "temporal_grain": "per-model six-hour causal snapshot",
            },
            "production_ready": production_ready,
            "research_only": not production_ready,
            "activation_mode": "live_primary" if production_ready else "blocked",
    }
    artifact = {
        **shared_artifact,
        "regime_policy": {
            "pre_dawn_00_05": "gfs_jma_multimodel",
            "hours_06_23": "gfs_only",
            "selection_basis": "strict_expanding_year_out_of_sample_segment_metrics",
        },
        "submodels": {
            "gfs_jma_multimodel": {**multimodel_artifact, **shared_artifact, "submodel_role": "pre_dawn_00_05"},
            "gfs_only": {**gfs_artifact, **shared_artifact, "submodel_role": "hours_06_23"},
        },
    }
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": {
            "split": "strict expanding-year walk-forward",
            "timezone": "Asia/Shanghai",
            "hours": "00:00-23:59",
            "pairing_grain": "hourly station state within local day",
            "causal_join": join_report.get("causal_join_rule"),
            "core_models": ["NOAA GFS", "JMA GSM"],
        },
        "folds": folds,
        "aggregate": aggregate,
        "segments": segments,
        "improvement_release_candidate_vs_v2_pct": gain_v2,
        "improvement_release_candidate_vs_gfs_pct": gain_gfs,
        "improvement_raw_multimodel_vs_v2_pct": raw_multimodel_gain_v2,
        "improvement_raw_multimodel_vs_gfs_pct": raw_multimodel_gain_gfs,
        "pre_dawn_improvement_vs_v2_pct": pre_dawn_v2,
        "pre_dawn_improvement_vs_gfs_pct": pre_dawn_gfs,
        "daytime_improvement_vs_gfs_pct": daytime_gfs,
        "year_wins_nll_and_rps_vs_v2": year_wins_v2,
        "year_wins_nll_and_rps_vs_gfs": year_wins_gfs,
        "feature_importance": _feature_importance(multimodel_artifact),
        "data_quality": {
            "all_models_join_rate_pct": coverage,
            "future_join_violation_count": join_report.get("future_join_violation_count"),
            "gfs_downloaded_total_bytes": gfs_manifest.get("downloaded_total_bytes"),
            "jma_downloaded_total_bytes": jma_manifest.get("downloaded_total_bytes"),
        },
        "candidate_model_registry": {
            "core": {
                "gfs_seamless": "five-year same-distribution history and live pressure fields",
                "jma_gsm": "five-year same-distribution history and live pressure fields",
            },
            "shadow": {
                "icon_global": "archive begins 2022-11-24",
                "gem_global": "archive begins 2022-11-23",
                "ecmwf_ifs025": "archive begins 2024-02-03",
                "cma_grapes_global": "archive begins 2023-12-31",
            },
        },
        "release_gate": {
            "production_ready": production_ready,
            "production_blockers": blockers,
            "known_caveats": [
                "ncei_hourly_labels_not_amsc_half_hour_settlement_labels",
                "numerical_models_are_soft_context_and_cannot_override_amsc",
            ],
        },
        "guards": {
            "historical_backfill": False,
            "reads_archive_db": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "changes_parameters": False,
            "restarts_launchd": False,
            "production_integration": False,
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "model_artifact.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output_dir / "walk_forward_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output_dir / "walk_forward_report.md").write_text(_render(report), encoding="utf-8")
    print(json.dumps({"ok": True, "production_ready": production_ready, "improvement_release_candidate_vs_v2": gain_v2, "improvement_release_candidate_vs_gfs": gain_gfs, "output_dir": str(args.output_dir)}, ensure_ascii=False, sort_keys=True))
    return 0 if production_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
