#!/usr/bin/env python3
"""Evaluate NOAA PSL Sichuan synoptic context as a research-only v3 layer."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import calibrate_ncei_zuuu_remaining_warming_v2 as v2  # noqa: E402
from scripts import train_ncei_zuuu_remaining_warming_bayes as v1  # noqa: E402


SCHEMA = "ncei_zuuu_sichuan_synoptic_bayes_research_v3"
MODEL_SCHEMA = "ncei_zuuu_zero_inflated_synoptic_bayes_artifact_v3"

SYNOPTIC_DIRECT = [
    "synoptic_lag_hours",
    "basin_center_air_850_c",
    "basin_center_rhum_850_pct",
    "basin_center_rhum_700_pct",
    "basin_center_hgt_500_m",
    "basin_center_hgt_850_m",
    "basin_center_uwnd_850_mps",
    "basin_center_vwnd_850_mps",
    "basin_center_uwnd_700_mps",
    "basin_center_vwnd_700_mps",
    "basin_center_omega_700_pa_per_s",
    "synoptic_850_wind_speed_mps",
    "synoptic_700_wind_speed_mps",
    "synoptic_850_700_shear_mps",
    "synoptic_air850_west_minus_east_c",
    "synoptic_air850_north_minus_south_c",
    "synoptic_hgt500_west_minus_east_m",
    "synoptic_hgt500_north_minus_south_m",
    "synoptic_warm_advection_850_c_per_h",
    "synoptic_westerly_dry_foehn_proxy",
    "synoptic_southerly_moisture_proxy",
    "synoptic_ascent_moisture_proxy",
]

SYNOPTIC_ENGINEERED = [
    "synoptic_hgt500_minus_hgt850_m",
    "synoptic_surface_air850_gap_c",
    "interaction_air850_solar",
    "interaction_rh700_cloud",
    "interaction_ascent_surface_moisture",
]

V3_NUMERIC = v2.V2_NUMERIC + SYNOPTIC_DIRECT + SYNOPTIC_ENGINEERED
V3_BOOLEAN = v2.V2_BOOLEAN


def _values(row: Mapping[str, Any], climo: float) -> dict[str, float | None]:
    values = dict(v2._values(row, climo))
    for name in SYNOPTIC_DIRECT:
        values[name] = v1._number(row.get(name))
    hgt500 = v1._number(row.get("basin_center_hgt_500_m"))
    hgt850 = v1._number(row.get("basin_center_hgt_850_m"))
    surface_temp = v1._number(row.get("temperature_c"))
    air850 = v1._number(row.get("basin_center_air_850_c"))
    solar = v1._number(row.get("solar_heating_window"))
    rh700 = v1._number(row.get("basin_center_rhum_700_pct"))
    cloud = v1._number(row.get("cloud_cover_pct"))
    ascent = v1._number(row.get("synoptic_ascent_moisture_proxy"))
    dewpoint = v1._number(row.get("dewpoint_c"))
    values.update(
        {
            "synoptic_hgt500_minus_hgt850_m": hgt500 - hgt850 if hgt500 is not None and hgt850 is not None else None,
            "synoptic_surface_air850_gap_c": surface_temp - air850 if surface_temp is not None and air850 is not None else None,
            "interaction_air850_solar": air850 * solar if air850 is not None and solar is not None else None,
            "interaction_rh700_cloud": rh700 * cloud / 100.0 if rh700 is not None and cloud is not None else None,
            "interaction_ascent_surface_moisture": ascent * dewpoint if ascent is not None and dewpoint is not None else None,
        }
    )
    return values


class V3Design(v1.Design):
    def _raw(self, rows: Sequence[Mapping[str, Any]], climo_values: Sequence[float]) -> np.ndarray:
        matrix = []
        for row, climo in zip(rows, climo_values):
            values = _values(row, climo)
            numeric = [float(values[name]) if values.get(name) is not None else float("nan") for name in self.numeric]
            missing = [1.0 if values.get(name) is None else 0.0 for name in self.numeric]
            boolean = [float(values.get(name) or 0.0) for name in self.boolean]
            matrix.append([*numeric, *missing, *boolean])
        return np.asarray(matrix, dtype=float)


def _improvement(old: float, new: float) -> float:
    return 100.0 * (old - new) / old if old else 0.0


def _synoptic_importance(artifact: Mapping[str, Any], limit: int = 20) -> list[dict[str, float | str]]:
    result = []
    for name, coefficient in zip(artifact["feature_names"], artifact["peak_already_coefficients"]):
        if name in SYNOPTIC_DIRECT or name in SYNOPTIC_ENGINEERED:
            result.append({"feature": name, "coefficient": round(float(coefficient), 6), "absolute": round(abs(float(coefficient)), 6)})
    return sorted(result, key=lambda row: float(row["absolute"]), reverse=True)[:limit]


def _render(report: Mapping[str, Any]) -> str:
    old = report["aggregate"]["v2"]
    new = report["aggregate"]["v3"]
    gain = report["improvement_pct"]
    hot_gain = report["hot_season_improvement_pct"]
    hot_peak_gain = report["hot_season_peak_window_improvement_pct"]
    lines = [
        "# ZUUU 四川盆地大尺度背景 Bayes v3：逐年时间外验证",
        "",
        "## tl;dr",
        "",
        f"- 2022–2025逐年只用过去年份训练；v3相对v2：桶NLL `{gain['bucket_nll']:+.2f}%`、RPS `{gain['bucket_rps']:+.2f}%`、MAE `{gain['mae_c']:+.2f}%`。",
        f"- 四个年份中NLL与RPS同时胜出的年份：`{report['year_wins_nll_and_rps']}/4`。",
        f"- 暖季专项：桶NLL `{hot_gain['bucket_nll']:+.2f}%`、RPS `{hot_gain['bucket_rps']:+.2f}%`、MAE `{hot_gain['mae_c']:+.2f}%`。",
        f"- 暖季10–17时峰值窗：桶NLL `{hot_peak_gain['bucket_nll']:+.2f}%`、RPS `{hot_peak_gain['bucket_rps']:+.2f}%`、MAE `{hot_peak_gain['mae_c']:+.2f}%`；负值表示该指标回退。",
        f"- 研究门：`{'通过' if report['release_gate']['research_pass'] else '未通过'}`；生产门固定关闭。",
        "",
        "## 聚合指标",
        "",
        "| 模型 | MAE °C | 桶NLL | 桶RPS | 峰值已出现Brier |",
        "|---|---:|---:|---:|---:|",
        f"| v2报文顺序模型 | {old['mae_c']:.4f} | {old['bucket_nll']:.4f} | {old['bucket_rps']:.6f} | {old['continue_brier']:.6f} |",
        f"| v3＋大尺度背景 | {new['mae_c']:.4f} | {new['bucket_nll']:.4f} | {new['bucket_rps']:.6f} | {new['continue_brier']:.6f} |",
        "",
        "## 边界",
        "",
        "- NOAA再分析是气候/环流背景，不是机场硬实况，不能覆盖AMSC观测。",
        "- 每条小时观测只匹配其之前最近一次六小时场，延迟0–5小时，无未来穿越。",
        "- AMSC 2021–2025半小时历史仍需官方历史资料申请；生产校准与接入继续阻断。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    rows = v1._load(args.features)
    if not rows or not all(row.get("synoptic_join_available") for row in rows):
        raise SystemExit("synoptic coverage missing in model rows")
    years = sorted({int(str(row["event_date_local"])[:4]) for row in rows})
    folds = []
    predictions: dict[str, list[dict[str, Any]]] = {"v2": [], "v3": []}
    for test_year in years[1:]:
        train_rows = [row for row in rows if int(str(row["event_date_local"])[:4]) < test_year]
        test_rows = [row for row in rows if int(str(row["event_date_local"])[:4]) == test_year]
        fold = {"test_year": test_year, "train_day_count": len({row["event_date_local"] for row in train_rows})}
        v2_metrics, _, v2_predictions = v2._fit(
            train_rows,
            test_rows,
            version="v2",
            design_override=v2.V2Design(v2.V2_NUMERIC, v2.V2_BOOLEAN),
        )
        v3_metrics, _, v3_predictions = v2._fit(
            train_rows,
            test_rows,
            version="v3",
            design_override=V3Design(V3_NUMERIC, V3_BOOLEAN),
        )
        fold.update({"v2": v2_metrics, "v3": v3_metrics})
        predictions["v2"].extend(v2_predictions)
        predictions["v3"].extend(v3_predictions)
        folds.append(fold)

    _, final_artifact, _ = v2._fit(
        rows,
        rows,
        version="v3",
        design_override=V3Design(V3_NUMERIC, V3_BOOLEAN),
    )
    final_artifact.update({"schema": MODEL_SCHEMA, "trained_through_year": max(years), "training_sample_count": len(rows)})
    aggregate = {version: v2._weighted_summary(values) for version, values in predictions.items()}
    segments = {version: v2._segments(values) for version, values in predictions.items()}
    gain = {
        "bucket_nll": _improvement(float(aggregate["v2"]["bucket_nll"]), float(aggregate["v3"]["bucket_nll"])),
        "bucket_rps": _improvement(float(aggregate["v2"]["bucket_rps"]), float(aggregate["v3"]["bucket_rps"])),
        "mae_c": _improvement(float(aggregate["v2"]["mae_c"]), float(aggregate["v3"]["mae_c"])),
        "continue_brier": _improvement(float(aggregate["v2"]["continue_brier"]), float(aggregate["v3"]["continue_brier"])),
    }
    hot_gain = {
        key: _improvement(float(segments["v2"]["hot_season_all"][key]), float(segments["v3"]["hot_season_all"][key]))
        for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")
    }
    hot_peak_gain = {
        key: _improvement(
            float(segments["v2"]["hot_season_peak_window_10_17"][key]),
            float(segments["v3"]["hot_season_peak_window_10_17"][key]),
        )
        for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")
    }
    year_wins = sum(
        float(fold["v3"]["bucket_nll"]) < float(fold["v2"]["bucket_nll"])
        and float(fold["v3"]["bucket_rps"]) < float(fold["v2"]["bucket_rps"])
        for fold in folds
    )
    research_pass = bool(
        gain["bucket_nll"] > 0.0
        and gain["bucket_rps"] > 0.0
        and gain["mae_c"] > 0.0
        and gain["continue_brier"] > 0.0
        and hot_gain["bucket_nll"] > 0.0
        and hot_gain["bucket_rps"] > 0.0
        and hot_gain["mae_c"] > 0.0
        and year_wins >= 3
    )
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": {
            "split": "strict expanding-year walk-forward",
            "timezone": "Asia/Shanghai",
            "synoptic_source": "NOAA PSL NCEP/DOE Reanalysis 2",
            "synoptic_grain": "6-hourly 2.5-degree grid",
            "causal_join": "latest synoptic valid time not later than station observation",
        },
        "folds": folds,
        "aggregate": aggregate,
        "segments": segments,
        "improvement_pct": gain,
        "hot_season_improvement_pct": hot_gain,
        "hot_season_peak_window_improvement_pct": hot_peak_gain,
        "year_wins_nll_and_rps": year_wins,
        "synoptic_peak_already_feature_importance": _synoptic_importance(final_artifact),
        "release_gate": {
            "research_pass": research_pass,
            "production_ready": False,
            "production_blockers": [
                "amsc_2021_2025_half_hour_history_unavailable_without_external_application",
                "market_settlement_calibration_missing",
                "shadow_runtime_not_evaluated",
                "fact_summary_merge_fill_mismatch_unresolved",
            ],
            "research_caveats": [
                "hot_season_peak_window_bucket_nll_regression"
            ] if hot_peak_gain["bucket_nll"] <= 0.0 else [],
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
    (args.output_dir / "walk_forward_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output_dir / "walk_forward_report.md").write_text(_render(report), encoding="utf-8")
    (args.output_dir / "model_artifact.json").write_text(json.dumps(final_artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "research_pass": research_pass, "year_wins": year_wins, "improvement_pct": gain, "output_dir": str(args.output_dir), "guards": report["guards"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
