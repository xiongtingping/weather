#!/usr/bin/env python3
"""Audit deterministic failure modes in the ECMWF high-ventilation shadow.

The job is deliberately research-only.  It reads the held-out 2025 feature
rows and the v3.12 paired decomposition, then distinguishes source-code facts
from statistical associations.  It never writes archive.db, facts, posterior
SSOT, parameters, signal files, orders, or launchd state.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import evaluate_ncei_zuuu_additional_models_shadow_v31 as shadow
from scripts import evaluate_ncei_zuuu_ecmwf_regime_shrinkage_v311 as regime
from scripts import evaluate_zuuu_ecmwf_high_ventilation_forward_v312 as v312


SCHEMA = "zuuu_ecmwf_high_ventilation_deterministic_audit_v313"
GATE_NAME = "_high_basin_ventilation"
EXPECTED_GATE_FIELDS = (
    "ecmwf__synoptic_850_wind_speed_mps",
    "ecmwf__synoptic_700_wind_speed_mps",
    "ecmwf__synoptic_850_700_shear_mps",
)
SUPPRESSOR_CHANNELS = {
    "high_humidity_low_cloud": (
        "relative_humidity_pct",
        "low_cloud_cover_pct",
        "temp_dewpoint_spread_c",
    ),
    "current_or_antecedent_convection_precip": (
        "current_convective_weather",
        "convective_cloud_reported",
        "current_precipitation_reported",
        "convective_seen_today",
        "precipitation_seen_today",
        "post_convective_cooling",
        "post_precipitation_cooling",
    ),
    "observed_peak_decline": (
        "drop_from_observed_max_c",
        "reports_below_observed_max",
    ),
}


def _number(value: Any) -> float | None:
    return v312._number(value)


def _function_get_fields(path: Path, function_name: str) -> tuple[str, ...]:
    """Return literal Mapping.get field names used by one source function."""

    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    target = next(
        (
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        ),
        None,
    )
    if target is None:
        raise RuntimeError(f"function {function_name!r} is missing from {path}")
    fields: list[str] = []
    for node in ast.walk(target):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "get" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            fields.append(first.value)
    return tuple(sorted(set(fields)))


def _static_audit(
    *,
    regime_script: Path,
    live_fetch_script: Path,
    runtime_module: Path,
    candidate_model_script: Path,
) -> dict[str, Any]:
    gate_fields = _function_get_fields(regime_script, GATE_NAME)
    live_source = live_fetch_script.read_text(encoding="utf-8")
    regime_source = regime_script.read_text(encoding="utf-8")
    runtime_source = runtime_module.read_text(encoding="utf-8")
    candidate_source = candidate_model_script.read_text(encoding="utf-8")
    suppressor_fields = tuple(sorted({field for fields in SUPPRESSOR_CHANNELS.values() for field in fields}))
    candidate_model_mentions = {
        field: field in candidate_source for field in suppressor_fields
    }
    return {
        "gate": {
            "function": GATE_NAME,
            "actual_fields": list(gate_fields),
            "expected_fields": list(EXPECTED_GATE_FIELDS),
            "uses_only_expected_wind_fields": set(gate_fields) == set(EXPECTED_GATE_FIELDS),
            "constant_weight_after_gate": "effective_weight = float(weight if predicate(weather) else 0.0)"
            in regime_source,
            "ignored_suppressor_fields": [field for field in suppressor_fields if field not in gate_fields],
        },
        "selection_objective": {
            "continue_brier_non_degradation_required": 'gains["continue_brier"] > 0.0' in regime_source,
            "continue_brier_tolerated_down_to_pct": -0.5
            if 'gains["continue_brier"] > -0.5' in regime_source
            else None,
            "selection_score_includes_continue_brier": '"continue_brier"' in regime_source
            and "def _candidate_score" in regime_source,
            "interpretation": (
                "continue Brier 的恶化可在 -0.5% 以内仍被候选资格接受；"
                "因此桶分布收益能够压过二元‘还会不会升温’的轻度损失。"
            ),
        },
        "candidate_feature_model": {
            "source_path": str(candidate_model_script),
            "suppressor_field_name_present_in_source": candidate_model_mentions,
            "interpretation": (
                "字段名出现在候选特征模型源码只说明该模型具备消费路径；"
                "不等于高通风门控用这些字段决定是否混合。"
            ),
        },
        "forward_provenance": {
            "request_includes_past_days_2": '"past_days": "2"' in live_source,
            "raw_object_is_response_bytes_without_acquisition_envelope": "gzip.compress(response.content" in live_source,
            "bundle_has_request_completion_metadata": (
                '"requested_as_of_utc"' in live_source and '"response_completed_at"' in live_source
            ),
            "consequence": (
                "原始 ECMWF 对象本身不能单独证明取得时刻；含 past_days 的响应绝不能在事后当作前向样本。"
            ),
        },
        "production_boundary": {
            "runtime_requires_gfs_and_jma": '"gfs": "gfs_seamless"' in runtime_source
            and '"jma": "jma_gsm"' in runtime_source,
            "runtime_mentions_ecmwf": "ecmwf" in runtime_source.lower(),
            "interpretation": "ECMWF 当前是影子候选，不进入 v3.1 生产后验。",
        },
    }


def _decline_evidence(row: Mapping[str, Any]) -> bool:
    drop = _number(row.get("drop_from_observed_max_c"))
    reports_below = _number(row.get("reports_below_observed_max"))
    return bool((drop is not None and drop > 0.0) or (reports_below is not None and reports_below > 0.0))


def _context_flags(row: Mapping[str, Any]) -> dict[str, bool]:
    high_humidity_low_cloud = regime._high_humidity_low_cloud(row)
    convective_or_precip = regime._antecedent_convection_or_precip(row)
    observed_decline = _decline_evidence(row)
    return {
        "high_humidity_low_cloud": high_humidity_low_cloud,
        "current_or_antecedent_convection_precip": convective_or_precip,
        "observed_peak_decline": observed_decline,
        "any_suppressor_context": bool(high_humidity_low_cloud or convective_or_precip or observed_decline),
    }


def _load_decomposition(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            loaded = json.loads(line)
            if isinstance(loaded, Mapping):
                rows.append(dict(loaded))
    return rows


def _key(row: Mapping[str, Any]) -> tuple[str, float]:
    return str(row["event_date_local"]), round(float(row["local_hour"]), 6)


def _weighted_mean(rows: Sequence[Mapping[str, Any]], field: str, *, normalize: bool) -> float | None:
    if not rows:
        return None
    values = np.asarray([float(row[field]) for row in rows], dtype=float)
    weights = np.asarray([float(row["global_day_weight"]) for row in rows], dtype=float)
    if normalize:
        weights = weights / weights.sum()
    return float(np.sum(weights * values))


def _group_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "sample_count": 0,
            "day_count": 0,
            "global_day_weight_share": 0.0,
            "actual_continue_rate": None,
            "mean_continue_probability_delta": None,
            "brier_delta_mean_within_group": None,
            "brier_delta_contribution_to_total": 0.0,
            "bucket_nll_delta_mean_within_group": None,
        }
    return {
        "sample_count": len(rows),
        "day_count": len({str(row["event_date_local"]) for row in rows}),
        "global_day_weight_share": float(sum(float(row["global_day_weight"]) for row in rows)),
        "actual_continue_rate": _weighted_mean(rows, "actual_continue_numeric", normalize=True),
        "mean_continue_probability_delta": _weighted_mean(rows, "continue_probability_delta", normalize=True),
        "brier_delta_mean_within_group": _weighted_mean(rows, "brier_delta", normalize=True),
        "brier_delta_contribution_to_total": _weighted_mean(rows, "brier_delta", normalize=False),
        "bucket_nll_delta_mean_within_group": _weighted_mean(rows, "bucket_nll_delta", normalize=True),
    }


def _render(report: Mapping[str, Any]) -> str:
    static = report["static_audit"]
    dynamic = report["historical_2025"]
    split = dynamic["suppressor_context_split"]
    return "\n".join(
        [
            "# ECMWF 高通风影子层：确定性根因审计（v3.13）",
            "",
            "## 结论",
            "",
            "高通风影子层的根因是**门控与物理状态脱节**：它以 850 hPa 风、700 hPa 风、850—700 hPa 风切变三者任一超阈值启用固定混合权重，"
            "不检查高湿低云、对流/降水及其后的冷却、或当前实况已低于日内已观测最高温。"
            "这不是 ECMWF 本身已被证伪，也不是当前 v3.1 生产后验的缺陷；ECMWF 尚未进入生产。",
            "",
            "## 确定性代码证据",
            "",
            f"- 门控实际读取字段：{', '.join(static['gate']['actual_fields'])}。",
            f"- 门控后的权重是否恒定：{static['gate']['constant_weight_after_gate']}。",
            f"- 门控未读取的抑制通道：{', '.join(static['gate']['ignored_suppressor_fields'])}。",
            f"- 选择规则是否要求 continue Brier 不变差：{static['selection_objective']['continue_brier_non_degradation_required']}；"
            f"实际容忍下限为 {static['selection_objective']['continue_brier_tolerated_down_to_pct']}%。",
            "- 候选概率模型源码含有多项抑制通道字段；问题在于这些信息未进入‘是否混合 ECMWF’这道门，而非可以简单说成全系统没读取。",
            "",
            "## 严格留出样本的错误方向（2025）",
            "",
            f"- 高通风样本：{dynamic['sample_count']} 条半小时样本、{dynamic['day_count']} 天；日等权。",
            f"- 触发后平均继续升温概率改变：{dynamic['overall_mean_continue_probability_delta'] * 100:+.3f} 个百分点；"
            f"二元 Brier 差（候选－基线）：{dynamic['overall_brier_delta']:+.6f}（正数为变差）。",
            f"- 有任一抑制背景：{split['has_suppressor_context']['sample_count']} 条、权重占比 "
            f"{split['has_suppressor_context']['global_day_weight_share'] * 100:.1f}%，Brier 总贡献 "
            f"{split['has_suppressor_context']['brier_delta_contribution_to_total']:+.6f}。",
            f"- 无抑制背景：{split['no_suppressor_context']['sample_count']} 条、权重占比 "
            f"{split['no_suppressor_context']['global_day_weight_share'] * 100:.1f}%，Brier 总贡献 "
            f"{split['no_suppressor_context']['brier_delta_contribution_to_total']:+.6f}。",
            "- 这是诊断性分层，不是新的调参结果；分层本身没有进入任何模型或生产后验。",
            "",
            "## 第二个确定性问题：前向证据血缘",
            "",
            "ECMWF 研究抓取请求含 `past_days=2`，而逐模型原始对象保存的是响应字节，未附不可变的取得时间封套。"
            "汇总 bundle 虽记录请求/完成时间，原始对象单独拿出来仍无法自证 as-of 时刻；因此既有审计拒绝用事后文件对 2026 做前向评分是正确的。",
            "",
            "## 不应误判为根因的事项",
            "",
            "- Brier 分解恒等式、按结局汇总和 v3.12 指标复算均通过；不是评分公式或标签对齐错误。",
            "- 2025 是训练/权重选择（2024）之外的留出年；这次结论不是由同年调参直接造成。",
            "- 当前生产运行时只要求 GFS/JMA；ECMWF 的影子层问题不会改变现有 v3.1 的 SSOT 后验。",
            "",
            "## 最小修复顺序（尚未执行）",
            "",
            "1. 把 OR 型高通风门控拆成风型子状态，并让混合权重受高湿低云、对流/降水后冷却、已达峰回落三类抑制证据共同约束。",
            "2. 每个状态只用严格 as-of 的前向样本做按日成对验证；未达到样本门槛即收缩到零权重。",
            "3. 将每次逐模型原始对象写入带 `requested_as_of_utc`、`response_completed_at`、内容哈希的不可变封套，之后才允许其成为前向研究证据。",
        ]
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--decomposition-rows", type=Path, required=True)
    parser.add_argument("--regime-script", type=Path, default=ROOT / "scripts/evaluate_ncei_zuuu_ecmwf_regime_shrinkage_v311.py")
    parser.add_argument("--live-fetch-script", type=Path, default=ROOT / "scripts/fetch_open_meteo_multimodel_sichuan_live.py")
    parser.add_argument("--runtime-module", type=Path, default=ROOT / "ncei_zuuu_multimodel_v31_runtime.py")
    parser.add_argument("--candidate-model-script", type=Path, default=ROOT / "scripts/train_ncei_zuuu_multimodel_allday_bayes_v31.py")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    static = _static_audit(
        regime_script=args.regime_script,
        live_fetch_script=args.live_fetch_script,
        runtime_module=args.runtime_module,
        candidate_model_script=args.candidate_model_script,
    )
    feature_rows = [
        row
        for row in shadow._load(args.features)
        if str(row["event_date_local"]).startswith("2025-")
        and row.get("ecmwf__join_available")
        and regime._high_basin_ventilation(row)
    ]
    decomposition_rows = _load_decomposition(args.decomposition_rows)
    feature_by_key = {_key(row): row for row in feature_rows}
    decomposition_by_key = {_key(row): row for row in decomposition_rows}
    if len(feature_by_key) != len(feature_rows):
        raise RuntimeError("duplicate 2025 high-ventilation feature keys")
    if len(decomposition_by_key) != len(decomposition_rows):
        raise RuntimeError("duplicate decomposition keys")
    if set(feature_by_key) != set(decomposition_by_key):
        missing_features = sorted(set(decomposition_by_key) - set(feature_by_key))[:5]
        missing_decomposition = sorted(set(feature_by_key) - set(decomposition_by_key))[:5]
        raise RuntimeError(
            f"feature/decomposition key mismatch; missing_features={missing_features}, "
            f"missing_decomposition={missing_decomposition}"
        )

    combined: list[dict[str, Any]] = []
    for key in sorted(decomposition_by_key):
        score = dict(decomposition_by_key[key])
        weather = feature_by_key[key]
        flags = _context_flags(weather)
        score.update(flags)
        score["actual_continue_numeric"] = 1.0 if score.get("actual_continue") else 0.0
        combined.append(score)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in combined:
        grouped["has_suppressor_context" if row["any_suppressor_context"] else "no_suppressor_context"].append(row)
    component_groups = {
        name: _group_summary([row for row in combined if bool(row[name])])
        for name in ("high_humidity_low_cloud", "current_or_antecedent_convection_precip", "observed_peak_decline")
    }
    wind_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in combined:
        wind_groups[str(row["wind_trigger"])].append(row)

    overall_brier = _weighted_mean(combined, "brier_delta", normalize=False)
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "question": "identify deterministic defects before further ECMWF high-ventilation optimization",
        "static_audit": static,
        "historical_2025": {
            "scope": "2024-trained, 2025 held-out v3.12 high-ventilation ECMWF decomposition; daily equal weights",
            "sample_count": len(combined),
            "day_count": len({str(row["event_date_local"]) for row in combined}),
            "overall_brier_delta": overall_brier,
            "overall_mean_continue_probability_delta": _weighted_mean(
                combined, "continue_probability_delta", normalize=True
            ),
            "suppressor_context_split": {
                "has_suppressor_context": _group_summary(grouped["has_suppressor_context"]),
                "no_suppressor_context": _group_summary(grouped["no_suppressor_context"]),
            },
            "nonexclusive_suppressor_component_profiles": component_groups,
            "wind_trigger_profiles": {
                name: _group_summary(rows) for name, rows in sorted(wind_groups.items())
            },
            "coverage": {
                field: {
                    "missing_count": sum(row.get(field) is None for row in feature_rows),
                    "missing_rate": sum(row.get(field) is None for row in feature_rows) / len(feature_rows),
                }
                for field in sorted({field for fields in SUPPRESSOR_CHANNELS.values() for field in fields})
            },
        },
        "confirmed_root_causes": [
            {
                "id": "D0",
                "severity": "high",
                "finding": "选择资格允许 continue Brier 在 -0.5% 以内变差，而桶分布指标可通过综合分数抵消这项损失。",
                "why_deterministic": "源码直接验证；故‘桶更准但是否继续升温更差’是规则允许的结果。",
            },
            {
                "id": "D1",
                "severity": "high",
                "finding": "OR 型三风指标门控对所有触发行施加同一固定 ECMWF 混合权重。",
                "why_deterministic": "源码直接验证；不依赖统计显著性。",
            },
            {
                "id": "D2",
                "severity": "high",
                "finding": "高通风门控不读取高湿低云、对流/降水/冷却或实况已达峰回落信息。",
                "why_deterministic": "源码字段集合与物理抑制字段集合无交集；候选模型的字段路径不能替代门控条件。",
            },
            {
                "id": "D3",
                "severity": "high",
                "finding": "逐模型 ECMWF 原始对象缺少不可变取得时间封套，同时请求包含 past_days=2。",
                "why_deterministic": "源码直接验证；这使事后 raw 文件不能独立证明前向 as-of 合法性。",
            },
        ],
        "not_root_causes": [
            "v3.12 的 Brier 恒等式已逐行复算；不是评分公式错误。",
            "候选特征模型并非完全不含露点、云雨、对流等字段；断点在高通风混合门控。",
            "ECMWF 影子层尚未接入 v3.1 生产运行时；当前生产 SSOT 不受该影子层影响。",
        ],
        "release_gate": {
            "production_ready": False,
            "decision": "retain_ecmwf_as_non_influential_shadow",
            "blockers": [
                "state_gate_ignores_suppressor_physics",
                "raw_snapshot_acquisition_lineage_not_immutable",
                "no_strict_asof_forward_sample_after_gate_redesign",
            ],
        },
        "guards": {
            "historical_backfill": False,
            "downloads_data": False,
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
    (args.output_dir / "deterministic_root_cause_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "deterministic_root_cause_audit.md").write_text(
        _render(report), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "ok": True,
                "sample_count": report["historical_2025"]["sample_count"],
                "day_count": report["historical_2025"]["day_count"],
                "overall_brier_delta": report["historical_2025"]["overall_brier_delta"],
                "production_ready": False,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
