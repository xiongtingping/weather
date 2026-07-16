#!/usr/bin/env python3
"""Train and release-gate a live-compatible GFS-backed ZUUU v3 model."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import calibrate_ncei_zuuu_remaining_warming_v2 as v2
from scripts import train_ncei_zuuu_remaining_warming_bayes as v1
from scripts import train_ncei_zuuu_synoptic_bayes_v3 as v3


SCHEMA = "ncei_zuuu_gfs_synoptic_bayes_release_v3"
MODEL_SCHEMA = "ncei_zuuu_zero_inflated_gfs_synoptic_bayes_artifact_v3"


def _load(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            hour = v1._number(row.get("local_hour"))
            if (
                row.get("synoptic_join_available")
                and row.get("daily_max_label_eligible")
                and not row.get("core_qc_bad")
                and hour is not None
                and 6.0 <= hour <= 19.0
                and v1._number(row.get("remaining_warming_c")) is not None
                and v1._number(row.get("observed_max_to_time_c")) is not None
            ):
                rows.append(row)
    return rows


def _improvement(old: float, new: float) -> float:
    return 100.0 * (old - new) / old if old else 0.0


def _climo_by_doy(rows: list[dict[str, Any]]) -> dict[str, float]:
    table, per_day = v1._climo_table(rows)
    fallback = sum(per_day.values()) / len(per_day) if per_day else 30.0
    return {
        str(doy): float(total / count if count else fallback)
        for doy, (total, count) in table.items()
    }


def _render(report: Mapping[str, Any]) -> str:
    left = report["aggregate"]["v2"]
    right = report["aggregate"]["v3_gfs"]
    gain = report["improvement_pct"]
    return "\n".join(
        [
            "# ZUUU GFS 同分布实时 v3：发布验证",
            "",
            f"- 结论：`{'通过' if report['release_gate']['production_ready'] else '未通过'}`。",
            f"- 严格逐年样本外：NLL `{gain['bucket_nll']:+.3f}%`、RPS `{gain['bucket_rps']:+.3f}%`、MAE `{gain['mae_c']:+.3f}%`、Brier `{gain['continue_brier']:+.3f}%`。",
            f"- 年度双胜：`{report['year_wins_nll_and_rps']}/{len(report['folds'])}`；样本 `{right['sample_count']}` 条、完整日 `{right['day_count']}` 天。",
            "",
            "| 模型 | NLL | RPS | MAE °C | Brier |",
            "|---|---:|---:|---:|---:|",
            f"| v2 | {left['bucket_nll']:.6f} | {left['bucket_rps']:.8f} | {left['mae_c']:.6f} | {left['continue_brier']:.6f} |",
            f"| v3 GFS | {right['bucket_nll']:.6f} | {right['bucket_rps']:.8f} | {right['mae_c']:.6f} | {right['continue_brier']:.6f} |",
            "",
            "- GFS 历史与实时均使用 Open-Meteo 的 `gfs_seamless` 同变量合同；该层仍是软物理背景，不能覆盖 AMSC 硬实况。",
            "- 训练标签仍为 NCEI 小时日最高温；AMSC 半小时轨迹由运行时硬下界与最终锁定继续约束。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--join-report", type=Path, required=True)
    parser.add_argument("--download-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    rows = _load(args.features)
    if not rows:
        raise SystemExit("no causally joined GFS training rows")
    years = sorted({int(str(row["event_date_local"])[:4]) for row in rows})
    if years != [2021, 2022, 2023, 2024, 2025]:
        raise SystemExit(f"unexpected training years: {years}")
    join_report = json.loads(args.join_report.read_text(encoding="utf-8"))
    manifest = json.loads(args.download_manifest.read_text(encoding="utf-8"))
    if int(join_report.get("future_join_violation_count") or 0) != 0:
        raise SystemExit("future synoptic join violation")
    if str(manifest.get("model") or "") != "gfs_seamless":
        raise SystemExit("historical/live model contract mismatch")

    folds: list[dict[str, Any]] = []
    predictions: dict[str, list[dict[str, Any]]] = {"v2": [], "v3_gfs": []}
    for test_year in years[1:]:
        train_rows = [row for row in rows if int(str(row["event_date_local"])[:4]) < test_year]
        test_rows = [row for row in rows if int(str(row["event_date_local"])[:4]) == test_year]
        if not train_rows or not test_rows:
            continue
        v2_metrics, _, v2_predictions = v2._fit(
            train_rows,
            test_rows,
            version="v2",
            design_override=v2.V2Design(v2.V2_NUMERIC, v2.V2_BOOLEAN),
        )
        v3_metrics, _, v3_predictions = v2._fit(
            train_rows,
            test_rows,
            version="v3_gfs",
            design_override=v3.V3Design(v3.V3_NUMERIC, v3.V3_BOOLEAN),
        )
        folds.append(
            {
                "test_year": test_year,
                "train_day_count": len({row["event_date_local"] for row in train_rows}),
                "test_day_count": len({row["event_date_local"] for row in test_rows}),
                "v2": v2_metrics,
                "v3_gfs": v3_metrics,
            }
        )
        predictions["v2"].extend(v2_predictions)
        predictions["v3_gfs"].extend(v3_predictions)

    _, artifact, _ = v2._fit(
        rows,
        rows,
        version="v3_gfs",
        design_override=v3.V3Design(v3.V3_NUMERIC, v3.V3_BOOLEAN),
    )
    aggregate = {name: v2._weighted_summary(values) for name, values in predictions.items()}
    segments = {name: v2._segments(values) for name, values in predictions.items()}
    gain = {
        key: _improvement(float(aggregate["v2"][key]), float(aggregate["v3_gfs"][key]))
        for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")
    }
    hot_gain = {
        key: _improvement(
            float(segments["v2"]["hot_season_all"][key]),
            float(segments["v3_gfs"]["hot_season_all"][key]),
        )
        for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")
    }
    year_wins = sum(
        float(fold["v3_gfs"]["bucket_nll"]) < float(fold["v2"]["bucket_nll"])
        and float(fold["v3_gfs"]["bucket_rps"]) < float(fold["v2"]["bucket_rps"])
        for fold in folds
    )
    production_ready = bool(
        len(folds) == 4
        and gain["bucket_nll"] > 0.0
        and gain["bucket_rps"] > 0.0
        and gain["mae_c"] > 0.0
        and gain["continue_brier"] > 0.0
        and hot_gain["bucket_nll"] > 0.0
        and hot_gain["bucket_rps"] > 0.0
        and hot_gain["mae_c"] > 0.0
        and year_wins >= 3
    )
    blockers: list[str] = []
    if not production_ready:
        blockers.append("strict_walk_forward_release_gate_failed")
    if float(join_report.get("join_rate_pct") or 0.0) < 90.0:
        blockers.append("historical_gfs_join_coverage_below_90pct")
        production_ready = False

    artifact.update(
        {
            "schema": MODEL_SCHEMA,
            "version": "v3_gfs_live",
            "trained_through_year": max(years),
            "training_sample_count": len(rows),
            "training_day_count": len({str(row["event_date_local"]) for row in rows}),
            "climo_by_day_of_year_c": _climo_by_doy(rows),
            "source_contract": {
                "data_origin": "NOAA NCEP GFS",
                "transport": "Open-Meteo",
                "model": "gfs_seamless",
                "historical_endpoint": "historical-forecast-api.open-meteo.com/v1/forecast",
                "live_endpoint": "api.open-meteo.com/v1/forecast",
                "temporal_grain": "6-hourly causal snapshot",
                "same_variables_historical_and_live": True,
            },
            "production_ready": production_ready,
            "research_only": not production_ready,
            "activation_mode": "live_primary" if production_ready else "blocked",
        }
    )
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": {
            "split": "strict expanding-year walk-forward",
            "timezone": "Asia/Shanghai",
            "pairing_grain": "hourly station state within local day",
            "causal_join": "latest GFS six-hour state not later than station observation",
            "historical_live_contract": "open_meteo_gfs_seamless_same_variables",
        },
        "folds": folds,
        "aggregate": aggregate,
        "segments": segments,
        "improvement_pct": gain,
        "hot_season_improvement_pct": hot_gain,
        "year_wins_nll_and_rps": year_wins,
        "synoptic_feature_importance": v3._synoptic_importance(artifact),
        "data_quality": {
            "join_rate_pct": join_report.get("join_rate_pct"),
            "future_join_violation_count": join_report.get("future_join_violation_count"),
            "downloaded_total_bytes": manifest.get("downloaded_total_bytes"),
            "normalized_timestamp_count": manifest.get("normalized_timestamp_count"),
        },
        "release_gate": {
            "research_pass": production_ready,
            "production_ready": production_ready,
            "production_blockers": blockers,
            "known_caveats": [
                "ncei_hourly_labels_not_amsc_half_hour_settlement_labels",
                "gfs_is_soft_synoptic_context_not_hard_observation",
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
    print(json.dumps({"ok": True, "production_ready": production_ready, "year_wins": year_wins, "improvement_pct": gain, "output_dir": str(args.output_dir)}, ensure_ascii=False, sort_keys=True))
    return 0 if production_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
