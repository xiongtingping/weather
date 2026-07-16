#!/usr/bin/env python3
"""Evidence-based Definition-of-Done audit for the source-weight plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "forecast_source_weight_plan_completion_audit_v1"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"


def _mapping(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return dict(value) if isinstance(value, Mapping) else {}


def _check(check_id: str, requirement: str, ok: bool, evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": check_id, "requirement": requirement, "ok": bool(ok), "evidence": dict(evidence)}


def build_completion_audit(
    *,
    discovery: Mapping[str, Any] | None = None,
    coverage: Mapping[str, Any] | None = None,
    fetch_report: Mapping[str, Any] | None = None,
    realized_coverage: Mapping[str, Any] | None = None,
    skill_dataset: Mapping[str, Any] | None = None,
    artifact: Mapping[str, Any] | None = None,
    shadow: Mapping[str, Any] | None = None,
    promotion_packet: Mapping[str, Any] | None = None,
    root: Path = ROOT,
) -> dict[str, Any]:
    discovery, coverage, fetch_report = discovery or {}, coverage or {}, fetch_report or {}
    realized = realized_coverage or {}
    dataset, artifact, shadow, packet = skill_dataset or {}, artifact or {}, shadow or {}, promotion_packet or {}
    score_rows = [row for row in dataset.get("score_rows") or [] if isinstance(row, Mapping) and row.get("state") == "available"]
    immutable_rows = [row for row in score_rows if str(row.get("lineage_quality") or "").startswith("immutable_")]
    grouped: dict[tuple[str, str], set[str]] = {}
    for row in immutable_rows:
        key = (str(row.get("event_id") or ""), str(row.get("cutoff_id") or ""))
        grouped.setdefault(key, set()).add(str(row.get("source_id") or ""))
    required_models = {"ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"}
    triplet_pairs = sum(1 for sources in grouped.values() if required_models.issubset(sources))
    source_coverage = coverage.get("source_coverage") if isinstance(coverage.get("source_coverage"), Mapping) else {}
    release = shadow.get("release_gate") if isinstance(shadow.get("release_gate"), Mapping) else {}
    guards = shadow.get("guards") if isinstance(shadow.get("guards"), Mapping) else {}
    checks = [
        _check(
            "dod_1_external_immutable_training_evidence",
            "所有训练证据来自新拉取外部数据并有不可变血缘",
            bool(
                (fetch_report.get("authorized") is True or realized.get("authorized") is True)
                and (int(fetch_report.get("success_count") or 0) > 0 or int(realized.get("successful_immutable_envelope_count") or 0) > 0)
                and score_rows
                and len(immutable_rows) == len(score_rows)
            ),
            {
                "fetch_authorized": fetch_report.get("authorized"),
                "fetch_success_count": fetch_report.get("success_count", 0),
                "cumulative_fetch_authorized": realized.get("authorized"),
                "cumulative_immutable_envelopes": realized.get("successful_immutable_envelope_count", 0),
                "score_rows": len(score_rows),
                "immutable_rows": len(immutable_rows),
            },
        ),
        _check(
            "dod_2_external_settlement_replay",
            "Polymarket settlement_actual 可由外部 API 独立重放",
            bool(discovery.get("source_data_policy") == "external_only" and int(discovery.get("resolved_event_day_count") or discovery.get("event_count") or 0) > 0 and discovery.get("guards", {}).get("reads_existing_local_history") is False),
            {"schema": discovery.get("schema"), "resolved_event_day_count": discovery.get("resolved_event_day_count", discovery.get("event_count", 0))},
        ),
        _check(
            "dod_3_three_models_strictly_paired",
            "ECMWF/GFS/ICON 在相同 event/cutoff/bucket 下严格配对",
            triplet_pairs >= 30,
            {"strict_triplet_pair_count": triplet_pairs, "minimum_research_pairs": 30},
        ),
        _check(
            "dod_4_forward_only_sources_explicit",
            "TAF/WU/TWC 缺历史时明确 forward-only",
            bool(
                source_coverage.get("taf", {}).get("older_history_not_backfilled") is True
                and "forward" in str(source_coverage.get("wu_forecast", {}).get("state") or "")
                and source_coverage.get("twc_forecast", {}).get("historical_forecast_archive_required_for_backfill") is True
            ),
            {"taf": source_coverage.get("taf", {}), "wu": source_coverage.get("wu_forecast", {}), "twc": source_coverage.get("twc_forecast", {})},
        ),
        _check(
            "dod_4a_provider_unavailability_explicit_na",
            "上游 run 缺档显式 NA，不插值、不记零分、不做日期特判",
            bool(
                realized.get("request_accounting_complete") is True
                and realized.get("unmatched_result_count") == 0
                and realized.get("availability_policy", {}).get("provider_unavailable_never_receives_zero_score") is True
                and realized.get("availability_policy", {}).get("date_specific_exceptions") is False
            ),
            {
                "request_accounting_complete": realized.get("request_accounting_complete"),
                "explicit_unavailable_count": realized.get("explicit_unavailable_count"),
                "unmatched_result_count": realized.get("unmatched_result_count"),
                "availability_policy": realized.get("availability_policy", {}),
            },
        ),
        _check(
            "dod_5_provider_family_dedup",
            "provider-family 去重有合同与测试",
            bool(guards.get("provider_family_dedup_enabled") is True and (Path(root) / "tests/test_forecast_source_contextual_weight_model.py").exists()),
            {"runtime_guard": guards.get("provider_family_dedup_enabled"), "test_file_present": (Path(root) / "tests/test_forecast_source_contextual_weight_model.py").exists()},
        ),
        _check(
            "dod_6_contextual_strict_oos",
            "contextual 权重严格 OOS NLL/RPS 同时过门槛",
            bool(release.get("strict_oos_evaluated") is True and release.get("nll_improved") is True and release.get("rps_improved") is True and release.get("statistical_gate_passed") is True),
            {key: release.get(key) for key in ("strict_oos_evaluated", "nll_improved", "rps_improved", "statistical_gate_passed")},
        ),
        _check(
            "dod_7_curve_incremental_oos_fixed_precision",
            "curve tournament 增量严格 OOS 过门且弱层总精度不增加",
            bool(release.get("curve_incremental_statistical_gate_passed") is True and guards.get("amsc_curve_reliability_consumed_once") is True),
            {"curve_incremental_statistical_gate_passed": release.get("curve_incremental_statistical_gate_passed"), "consumed_once": guards.get("amsc_curve_reliability_consumed_once")},
        ),
        _check(
            "dod_8_sparse_context_fallback_no_hardcode",
            "稀疏季节/天气型自动回退，无温度/小时硬编码",
            bool(artifact.get("sparse_context_fallback_enabled") is True and artifact.get("seasonal_order") is not None and artifact.get("context_support") is not None),
            {"sparse_context_fallback_enabled": artifact.get("sparse_context_fallback_enabled"), "seasonal_order": artifact.get("seasonal_order"), "context_support_present": artifact.get("context_support") is not None},
        ),
        _check(
            "dod_9_artifact_reproducible_auditable_rollback",
            "challenger artifact 可重复、版本化、可审计、可回滚",
            bool(
                artifact.get("artifact_sha256")
                and artifact.get("training_manifest_sha256")
                and (
                    packet.get("parameter_change_request", {}).get("rollback_plan", {}).get("requires_retraining") is False
                    or (
                        release.get("statistical_gate_passed") is False
                        and (Path(root) / "scripts/forecast_source_weight_promotion_packet.py").exists()
                        and (Path(root) / "tests/test_forecast_source_weight_artifact_apply.py").exists()
                    )
                )
            ),
            {"artifact_sha256": artifact.get("artifact_sha256"), "training_manifest_sha256": artifact.get("training_manifest_sha256"), "packet_sha256": packet.get("packet_sha256")},
        ),
        _check(
            "dod_10_no_unapproved_production_mutation",
            "未经批准不改变 posterior/参数/信号/订单/launchd",
            bool(
                release.get("production_apply_allowed") is False
                and (not packet or packet.get("production_apply_allowed") is False)
                and (
                    not packet
                    or all(packet.get("guards", {}).get(key) is False for key in ("changes_parameters", "places_orders", "restarts_launchd"))
                )
            ),
            {"shadow_apply_allowed": release.get("production_apply_allowed"), "packet_apply_allowed": packet.get("production_apply_allowed"), "packet_guards": packet.get("guards", {})},
        ),
    ]
    blockers = [row["id"] for row in checks if not row["ok"]]
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "checks": checks,
        "passed_count": len(checks) - len(blockers),
        "required_count": len(checks),
        "blockers": blockers,
        "engineering_note": "工程完成不等于统计证据完成；真实外部样本与 OOS 门必须由输入证据证明。",
        "guards": {"writes_archive_db": False, "changes_parameters": False, "places_orders": False, "restarts_launchd": False},
        "next_boundary": "goal_complete" if not blockers else "collect_missing_external_evidence_without_relaxing_gates",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discovery", type=Path)
    parser.add_argument("--coverage", type=Path)
    parser.add_argument("--fetch-report", type=Path)
    parser.add_argument("--realized-coverage", type=Path)
    parser.add_argument("--skill-dataset", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--shadow", type=Path)
    parser.add_argument("--promotion-packet", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args(argv)
    report = build_completion_audit(
        discovery=_mapping(args.discovery),
        coverage=_mapping(args.coverage),
        fetch_report=_mapping(args.fetch_report),
        realized_coverage=_mapping(args.realized_coverage),
        skill_dataset=_mapping(args.skill_dataset),
        artifact=_mapping(args.artifact),
        shadow=_mapping(args.shadow),
        promotion_packet=_mapping(args.promotion_packet),
    )
    if args.output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
        output = args.output.expanduser().resolve()
        research_root = DEFAULT_RESEARCH_ROOT.resolve()
        if output != research_root and research_root not in output.parents:
            raise ValueError("completion_audit_output_outside_research_root")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
