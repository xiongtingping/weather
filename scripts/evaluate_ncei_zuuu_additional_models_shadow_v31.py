#!/usr/bin/env python3
"""Strict held-out shadow evaluation for additional v3.1 global models.

The comparison uses identical station rows on both sides.  The baseline is the
released v3.1 regime (GFS+JMA before 06:00, GFS otherwise); the candidate adds
one shadow model to the corresponding regime.  Outputs stay in research data
and can never promote a model or write the production fact store.
"""

from __future__ import annotations

import argparse
import gzip
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

from scripts import calibrate_ncei_zuuu_remaining_warming_v2 as v2
from scripts import join_ncei_zuuu_multimodel_synoptic_features as joiner
from scripts import train_ncei_zuuu_multimodel_allday_bayes_v31 as v31
from scripts import train_ncei_zuuu_remaining_warming_bayes as v1


SCHEMA = "ncei_zuuu_additional_global_models_shadow_evaluation_v31"
CANDIDATES = ("ecmwf", "icon", "gem", "cma")


def _number(value: Any) -> float | None:
    return v1._number(value)


def _model_numeric(model: str) -> list[str]:
    return v31._model_numeric(model)


def _numeric(models: Sequence[str]) -> list[str]:
    return [
        *v2.V2_NUMERIC,
        *[field for model in models for field in _model_numeric(model)],
        *v31.CONSENSUS_NUMERIC,
    ]


def _values(row: Mapping[str, Any], climo: float, models: Sequence[str]) -> dict[str, float | None]:
    output = dict(v2._values(row, climo))
    for model in models:
        output.update(v31._model_values(row, model))
    consensus = joiner._consensus(dict(row), list(models))
    output.update({name: _number(consensus.get(name)) for name in v31.CONSENSUS_NUMERIC})
    return output


class ShadowDesign(v1.Design):
    def __init__(self, models: Sequence[str]):
        self.models = tuple(models)
        super().__init__(_numeric(self.models), v31.BOOLEAN)

    def _raw(self, rows: Sequence[Mapping[str, Any]], climo_values: Sequence[float]) -> np.ndarray:
        matrix = []
        for row, climo in zip(rows, climo_values):
            values = _values(row, climo, self.models)
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
            hour = _number(row.get("local_hour"))
            if (
                row.get("daily_max_label_eligible")
                and not row.get("core_qc_bad")
                and hour is not None
                and 0.0 <= hour < 24.0
                and row.get("gfs__join_available")
                and row.get("jma__join_available")
                and _number(row.get("remaining_warming_c")) is not None
                and _number(row.get("observed_max_to_time_c")) is not None
            ):
                rows.append(row)
    return rows


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return dict(v2._weighted_summary(rows))


def _gains(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, float]:
    return {
        key: 100.0 * (float(baseline[key]) - float(candidate[key])) / float(baseline[key])
        for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")
    }


def _regime_predictions(
    gfs: Sequence[Mapping[str, Any]],
    predawn: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if len(gfs) != len(predawn):
        raise RuntimeError("paired prediction widths differ")
    output = []
    for gfs_row, predawn_row in zip(gfs, predawn):
        if (
            gfs_row.get("event_date_local") != predawn_row.get("event_date_local")
            or float(gfs_row.get("local_hour") or 0.0) != float(predawn_row.get("local_hour") or 0.0)
        ):
            raise RuntimeError("paired prediction ordering differs")
        output.append(dict(predawn_row if float(gfs_row["local_hour"]) < 6.0 else gfs_row))
    return output


def _evaluate(rows: list[dict[str, Any]], candidate: str) -> dict[str, Any]:
    eligible = [row for row in rows if row.get(f"{candidate}__join_available")]
    years = sorted({int(str(row["event_date_local"])[:4]) for row in eligible})
    if 2025 not in years:
        return {"candidate": candidate, "state": "blocked", "state_reason": "held_out_2025_missing"}
    train = [row for row in eligible if int(str(row["event_date_local"])[:4]) < 2025]
    test = [row for row in eligible if int(str(row["event_date_local"])[:4]) == 2025]
    if not train or not test:
        return {"candidate": candidate, "state": "blocked", "state_reason": "train_or_test_empty"}

    designs = {
        "baseline_day": ShadowDesign(("gfs",)),
        "baseline_predawn": ShadowDesign(("gfs", "jma")),
        "candidate_day": ShadowDesign(("gfs", candidate)),
        "candidate_predawn": ShadowDesign(("gfs", "jma", candidate)),
    }
    predictions: dict[str, list[dict[str, Any]]] = {}
    artifacts: dict[str, Mapping[str, Any]] = {}
    for name, design in designs.items():
        _metrics, artifact, rows_out = v2._fit(train, test, version=name, design_override=design)
        predictions[name] = rows_out
        artifacts[name] = artifact
    baseline_rows = _regime_predictions(predictions["baseline_day"], predictions["baseline_predawn"])
    candidate_rows = _regime_predictions(predictions["candidate_day"], predictions["candidate_predawn"])
    baseline = _summary(baseline_rows)
    extended = _summary(candidate_rows)
    gains = _gains(baseline, extended)
    all_key_metrics_improve = all(gains[key] > 0.0 for key in gains)
    return {
        "candidate": candidate,
        "state": "evaluated_shadow_only",
        "train_years": sorted({int(str(row["event_date_local"])[:4]) for row in train}),
        "test_years": [2025],
        "train_sample_count": len(train),
        "train_day_count": len({row["event_date_local"] for row in train}),
        "test_sample_count": len(test),
        "test_day_count": len({row["event_date_local"] for row in test}),
        "baseline_v31_regime": baseline,
        "candidate_augmented_regime": extended,
        "improvement_candidate_vs_baseline_pct": gains,
        "all_key_metrics_improve": all_key_metrics_improve,
        "feature_counts": {
            name: len(artifact.get("feature_names") or [])
            for name, artifact in artifacts.items()
        },
        "release_gate": {
            "production_ready": False,
            "shadow_ready": True,
            "production_blockers": [
                "only_one_complete_independent_held_out_year",
                "short_archive_does_not_cover_multiple_climate_regimes",
                *([] if all_key_metrics_improve else ["candidate_does_not_improve_all_key_metrics"]),
            ],
        },
    }


def _render(report: Mapping[str, Any]) -> str:
    lines = [
        "# ZUUU v3.1 其他全球模式影子验证",
        "",
        "基线是当前分时 v3.1：凌晨 GFS＋JMA，其余时段 GFS。候选在相同时段加入对应模式。",
        "",
        "| 模式 | 训练日 | 2025测试日 | NLL改善 | RPS改善 | MAE改善 | Brier改善 | 结论 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in report.get("candidates") or []:
        gains = row.get("improvement_candidate_vs_baseline_pct") or {}
        lines.append(
            f"| {str(row.get('candidate') or '').upper()} | {row.get('train_day_count', 0)} | {row.get('test_day_count', 0)} | "
            f"{float(gains.get('bucket_nll') or 0.0):+.3f}% | {float(gains.get('bucket_rps') or 0.0):+.3f}% | "
            f"{float(gains.get('mae_c') or 0.0):+.3f}% | {float(gains.get('continue_brier') or 0.0):+.3f}% | 仅影子，不晋级 |"
        )
    lines.extend(
        [
            "",
            "- 全部特征按各模式最新且不晚于观测时刻的6小时状态连接，最大滞后6小时。",
            "- 标签仍为 NCEI 小时最高温，不是 AMSC 半小时结算值。",
            "- 只有一个完整独立测试年，因此无论单年指标多好都不能替代生产 v3.1。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--join-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    rows = _load(args.features)
    join_report = json.loads(args.join_report.read_text(encoding="utf-8"))
    candidates = [_evaluate(rows, candidate) for candidate in CANDIDATES]
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": {
            "split": "train on all candidate-available rows before 2025, test only on 2025",
            "comparison": "released v3.1 regime versus same regime augmented by one candidate",
            "pairing": "identical station rows and labels",
            "timezone": "Asia/Shanghai",
            "causal_features_only": True,
        },
        "input_row_count": len(rows),
        "join_quality": join_report,
        "candidates": candidates,
        "guards": {
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
    (args.output_dir / "shadow_evaluation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "shadow_evaluation_report.md").write_text(_render(report), encoding="utf-8")
    print(json.dumps({"ok": True, "candidates": [{"candidate": row.get("candidate"), "state": row.get("state"), "improvement": row.get("improvement_candidate_vs_baseline_pct")} for row in candidates], "output_dir": str(args.output_dir)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
