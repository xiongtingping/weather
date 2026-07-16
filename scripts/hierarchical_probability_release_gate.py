#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import forecast_weak_likelihood_contract_audit as forecast_audit
from scripts import forecast_source_weight_release_gate as forecast_weight_audit
from scripts import fact_writer_persisted_apply_contract_audit as writer_apply_audit
from scripts import canonical_writer_persisted_coverage_audit as canonical_persisted_audit
from scripts import hierarchical_probability_unit_contract_audit as unit_contract_audit
from scripts import metar_strong_likelihood_contract_audit as metar_audit
from scripts import parameter_governance_surface_audit as parameter_governance_audit
from scripts import posterior_ssot_fact_coverage_audit as posterior_ssot_audit
from scripts import prior_layer_contract_audit as prior_audit
from scripts import production_chain_uniqueness_audit as chain_audit
from scripts import source_acquisition_contract_audit as source_acquisition_audit
from scripts import ssot_chain_replay_audit as replay_audit
from scripts import ui_snapshot_lineage_audit as ui_lineage_audit

DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "hierarchical_probability_release_gate_v1"


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "writes_daily_weather_reviews": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "executes_sql": False,
    }


def _coverage_count(report: Mapping[str, Any]) -> int:
    coverage = report.get("coverage")
    if isinstance(coverage, Mapping):
        try:
            return int(coverage.get("checked_fact_count") or 0)
        except Exception:
            return 0
    counts = report.get("counts")
    if isinstance(counts, Mapping):
        for key in ("checked_facts", "replayed_facts", "source_fact_ids", "fact_items"):
            try:
                value = int(counts.get(key) or 0)
            except Exception:
                value = 0
            if value:
                return value
    try:
        return int(report.get("signal_count") or 0)
    except Exception:
        return 0
    return 0


def _report_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    coverage = report.get("coverage")
    counts = report.get("counts")
    snapshot = report.get("snapshot")
    return {
        "schema": str(report.get("schema") or ""),
        "ok": bool(report.get("ok")),
        "read_only": bool(report.get("read_only")),
        "coverage_mode": str(report.get("coverage_mode") or ""),
        "coverage": dict(coverage) if isinstance(coverage, Mapping) else {},
        "counts": dict(counts) if isinstance(counts, Mapping) else {},
        "snapshot": dict(snapshot) if isinstance(snapshot, Mapping) else {},
        "signal_count": int(report.get("signal_count") or 0),
        "issue_counts": dict(report.get("issue_counts") or {}),
        "blockers": list(report.get("blockers") or []),
        "warnings": list(report.get("warnings") or []),
        "next_boundary": str(report.get("next_boundary") or ""),
    }


def _exception_report(schema: str, blocker: str, exc: BaseException) -> dict[str, Any]:
    return {
        "schema": schema,
        "ok": False,
        "read_only": True,
        "coverage": {"checked_fact_count": 0},
        "issue_counts": {blocker: 1},
        "blockers": [
            {
                "blocker": blocker,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        ],
        "warnings": [],
        "guards": _guards(),
        "next_boundary": f"fix_{blocker}_before_release_gate",
    }


def _audit_report_blockers(
    *,
    gate: str,
    report_name: str,
    report: Mapping[str, Any],
    min_checked_facts: int,
    require_checked_facts: bool,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if report.get("ok") is not True:
        blockers.append(
            {
                "blocker": f"{gate}_{report_name}_not_ok",
                "report": report_name,
                "schema": str(report.get("schema") or ""),
                "issue_counts": dict(report.get("issue_counts") or {}),
                "report_blockers": list(report.get("blockers") or []),
            }
        )
    checked = _coverage_count(report)
    if require_checked_facts and checked < max(1, int(min_checked_facts or 1)):
        blockers.append(
            {
                "blocker": f"{gate}_{report_name}_insufficient_fact_scope",
                "report": report_name,
                "checked_fact_count": checked,
                "min_checked_facts": max(1, int(min_checked_facts or 1)),
            }
        )
    guards = report.get("guards")
    if isinstance(guards, Mapping):
        write_flags = {
            key: value
            for key, value in guards.items()
            if key.startswith("writes_") and bool(value)
        }
        unsafe_flags = {
            key: value
            for key, value in guards.items()
            if key in {"places_orders", "restarts_launchd", "historical_backfill", "changes_parameters"}
            and bool(value)
        }
        if write_flags or unsafe_flags:
            blockers.append(
                {
                    "blocker": f"{gate}_{report_name}_not_read_only",
                    "report": report_name,
                    "write_flags": write_flags,
                    "unsafe_flags": unsafe_flags,
                }
            )
    return blockers


def _make_gate(
    *,
    gate: str,
    description: str,
    reports: Mapping[str, Mapping[str, Any]],
    min_checked_facts: int,
    require_checked_facts: bool,
    continue_to: str,
) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    for report_name, report in reports.items():
        blockers.extend(
            _audit_report_blockers(
                gate=gate,
                report_name=report_name,
                report=report,
                min_checked_facts=min_checked_facts,
                require_checked_facts=require_checked_facts,
            )
        )
    return {
        "gate": gate,
        "ok": not blockers,
        "description": description,
        "reports": {name: _report_summary(report) for name, report in reports.items()},
        "blockers": blockers,
        "next_boundary": continue_to if not blockers else f"fix_{gate}_before_continue",
    }


def _posterior_entrypoint_report(chain_report: Mapping[str, Any]) -> dict[str, Any]:
    checks = chain_report.get("checks")
    checks = checks if isinstance(checks, Mapping) else {}
    posterior_ok = bool(checks.get("reported_x_single_production_mainline"))
    blockers = [
        dict(blocker)
        for blocker in chain_report.get("blockers") or []
        if isinstance(blocker, Mapping)
        and str(blocker.get("blocker") or "").startswith(
            (
                "reported_x_production_mainline_",
                "legacy_additive_runtime_",
                "retired_probability_",
            )
        )
    ]
    return {
        "schema": "reported_x_mainline_release_gate_v1",
        "ok": bool(posterior_ok and not blockers),
        "read_only": True,
        "coverage": {"checked_fact_count": 1},
        "checks": {"reported_x_single_production_mainline": posterior_ok},
        "blockers": blockers,
        "warnings": list(chain_report.get("warnings") or []),
        "next_boundary": (
            "reported_x_mainline_ok_continue_to_posterior_ssot_gate"
            if posterior_ok and not blockers
            else "fix_reported_x_production_mainline_before_posterior_ssot_gate"
        ),
    }


def _unit_stage_report(unit_report: Mapping[str, Any], stage: str) -> dict[str, Any]:
    stage_reports = unit_report.get("stage_reports")
    if isinstance(stage_reports, Mapping):
        report = stage_reports.get(stage)
        if isinstance(report, Mapping):
            return dict(report)
    return {
        "schema": "hierarchical_probability_unit_stage_contract_missing_v1",
        "stage": str(stage or ""),
        "ok": False,
        "read_only": True,
        "coverage": {"checked_fact_count": 0},
        "issue_counts": {"unit_stage_report_missing": 1},
        "blockers": [
            {
                "blocker": f"{stage}_unit_stage_report_missing",
                "stage": str(stage or ""),
            }
        ],
        "warnings": [],
        "guards": _guards(),
        "next_boundary": f"fix_{stage}_unit_contract_before_release_gate",
    }


def _ui_snapshot_lineage_release_report(
    *,
    db_path: Path,
    mode: str,
    run_date: str,
    ui_limit: int,
    city_filter: str,
    run_replay: bool,
    since_hours: float,
    replay_limit: int,
    max_items_output: int,
) -> dict[str, Any]:
    def _audit(force_rebuild: bool) -> dict[str, Any]:
        payload = ui_lineage_audit.load_ui_snapshot(
            mode=str(mode or "summary"),
            run_date=str(run_date or ""),
            limit=int(ui_limit),
            city_filter=str(city_filter or ""),
            force_rebuild=bool(force_rebuild),
        )
        return ui_lineage_audit.audit_payload(
            payload,
            db_path=db_path,
            run_replay=bool(run_replay),
            since_hours=float(since_hours),
            replay_limit=int(replay_limit),
            max_items_output=int(max_items_output),
        )

    def _needs_force_retry(report: Mapping[str, Any]) -> bool:
        counts = report.get("counts")
        fact_items = 0
        if isinstance(counts, Mapping):
            try:
                fact_items = int(counts.get("fact_items") or 0)
            except Exception:
                fact_items = 0
        snapshot = report.get("snapshot")
        snapshot_map = snapshot if isinstance(snapshot, Mapping) else {}
        blockers = {str(item or "") for item in report.get("blockers") or []}
        busy_or_placeholder = (
            str(snapshot_map.get("snapshot_freshness_state") or "") == "blocked"
            or bool(snapshot_map.get("lineage_placeholder"))
        )
        return fact_items <= 0 and busy_or_placeholder and (
            "desktop_first_screen_fact_item_missing" in blockers
            or "desktop_first_screen_bayes_posterior_ssot_section_missing" in blockers
        )

    try:
        report = _audit(False)
        if _needs_force_retry(report):
            retry_report = _audit(True)
            warnings = list(retry_report.get("warnings") or [])
            warnings.append("ui_snapshot_lineage_force_rebuild_retry_after_busy_placeholder")
            retry_report = dict(retry_report)
            retry_report["warnings"] = warnings
            retry_report["initial_retry_reason"] = "ui_snapshot_busy_placeholder_without_fact_items"
            return retry_report
        return report
    except Exception as exc:
        return _exception_report(
            "ui_snapshot_lineage_audit_exception_v1",
            "ui_snapshot_lineage_audit_exception",
            exc,
        )


def _ssot_chain_replay_release_report(
    *,
    db_path: Path,
    since_hours: float,
    replay_limit: int,
    max_items_output: int,
    require_awc_signal_sample: bool,
) -> dict[str, Any]:
    try:
        report = replay_audit.audit(
            db_path=db_path,
            since_hours=float(since_hours),
            limit=int(replay_limit),
            max_signals_output=int(max_items_output),
            include_details=False,
            fail_legacy_artifacts=False,
        )
    except Exception as exc:
        return _exception_report(
            "ssot_chain_replay_audit_exception_v1",
            "ssot_chain_replay_audit_exception",
            exc,
        )
    report = dict(report)
    blockers = [str(item) for item in report.get("blockers") or []]
    warnings = [str(item) for item in report.get("warnings") or []]
    if not require_awc_signal_sample and "no_awc_hard_source_signals_found" in blockers:
        blockers = [item for item in blockers if item != "no_awc_hard_source_signals_found"]
        if "release_gate_no_recent_awc_hard_source_signal_sample" not in warnings:
            warnings.append("release_gate_no_recent_awc_hard_source_signal_sample")
    report["blockers"] = blockers
    report["warnings"] = warnings
    report["ok"] = not blockers
    report["release_gate_sample_required"] = bool(require_awc_signal_sample)
    return report


def build_hierarchical_probability_release_gate(
    *,
    root: Path = ROOT,
    db_path: Path = DEFAULT_DB,
    since_hours: float = 168.0,
    latest_only: bool = True,
    active_only: bool = False,
    project_forward: bool = True,
    limit: int = 50,
    max_items_output: int = 5,
    min_checked_facts: int = 1,
    include_production_uniqueness: bool = True,
    include_parameter_governance: bool = True,
    max_samples_per_pattern: int = 10,
    include_ui_lineage: bool = True,
    ui_mode: str = "summary",
    ui_run_date: str = "",
    ui_limit: int = 120,
    ui_city_filter: str = "",
    run_ui_replay: bool = True,
    include_chain_replay: bool = True,
    replay_limit: int = 50,
    require_awc_signal_sample: bool = False,
    include_forecast_source_weight_gate: bool = False,
    forecast_source_weight_artifact: Mapping[str, Any] | None = None,
    forecast_source_weight_shadow: Mapping[str, Any] | None = None,
    forecast_source_weight_ablation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = Path(db_path)
    common = {
        "db_path": db,
        "since_hours": float(since_hours),
        "latest_only": bool(latest_only),
        "active_only": bool(active_only),
        "project_forward": bool(project_forward),
        "limit": int(limit),
        "max_items_output": int(max_items_output),
    }
    unit_report = unit_contract_audit.build_hierarchical_probability_unit_contract_audit()
    source_acquisition_report = source_acquisition_audit.build_source_acquisition_contract_audit(
        root=Path(root),
        max_items_output=int(max_items_output),
    )
    prior_report = prior_audit.build_prior_layer_contract_audit(**common)
    forecast_report = forecast_audit.build_forecast_weak_likelihood_contract_audit(**common)
    metar_report = metar_audit.build_metar_strong_likelihood_contract_audit(**common)
    posterior_ssot_report = posterior_ssot_audit.audit(
        db,
        since_hours=float(since_hours),
        latest_only=bool(latest_only),
        project_forward=bool(project_forward),
        active_only=bool(active_only),
        limit=int(limit),
        max_items_output=int(max_items_output),
    )
    writer_apply_report = writer_apply_audit.build_fact_writer_persisted_apply_contract_audit()
    canonical_persisted_report = canonical_persisted_audit.build_canonical_writer_persisted_coverage_audit(
        db_path=db,
        since_hours=float(since_hours),
        limit=int(limit),
        max_items_output=int(max_items_output),
        latest_only=bool(latest_only),
        require_marked=False,
        project_forward=bool(project_forward),
        active_only=bool(active_only),
    )
    if include_production_uniqueness:
        chain_report = chain_audit.build_production_chain_uniqueness_audit(
            Path(root),
            db_path=db,
            max_samples_per_pattern=max(0, int(max_samples_per_pattern or 0)),
        )
    else:
        chain_report = {
            "schema": "production_chain_uniqueness_audit_skipped_v1",
            "ok": True,
            "read_only": True,
            "checks": {"reported_x_single_production_mainline": True},
            "blockers": [],
            "warnings": ["production_chain_uniqueness_audit_skipped"],
        }
    posterior_entrypoint = _posterior_entrypoint_report(chain_report)
    ui_lineage_report = (
        _ui_snapshot_lineage_release_report(
            db_path=db,
            mode=str(ui_mode or "summary"),
            run_date=str(ui_run_date or ""),
            ui_limit=int(ui_limit),
            city_filter=str(ui_city_filter or ""),
            run_replay=bool(run_ui_replay),
            since_hours=float(since_hours),
            replay_limit=int(replay_limit),
            max_items_output=int(max_items_output),
        )
        if include_ui_lineage
        else {
            "schema": "ui_snapshot_lineage_audit_skipped_v1",
            "ok": True,
            "read_only": True,
            "coverage": {"checked_fact_count": 1},
            "blockers": [],
            "warnings": ["ui_snapshot_lineage_audit_skipped"],
            "guards": _guards(),
        }
    )
    replay_report = (
        _ssot_chain_replay_release_report(
            db_path=db,
            since_hours=float(since_hours),
            replay_limit=int(replay_limit),
            max_items_output=int(max_items_output),
            require_awc_signal_sample=bool(require_awc_signal_sample),
        )
        if include_chain_replay
        else {
            "schema": "ssot_chain_replay_audit_skipped_v1",
            "ok": True,
            "read_only": True,
            "coverage": {"checked_fact_count": 1},
            "blockers": [],
            "warnings": ["ssot_chain_replay_audit_skipped"],
            "guards": _guards(),
        }
    )
    parameter_governance_report = (
        parameter_governance_audit.build_audit(Path(root))
        if include_parameter_governance
        else {
            "schema": "parameter_governance_surface_audit_skipped_v1",
            "ok": True,
            "read_only": True,
            "blockers": [],
            "warnings": ["parameter_governance_surface_audit_skipped"],
            "guards": _guards(),
        }
    )
    forecast_weight_report = forecast_weight_audit.build_forecast_source_weight_release_gate(
        artifact=forecast_source_weight_artifact,
        shadow_evaluation=forecast_source_weight_shadow,
        ablation=forecast_source_weight_ablation,
        require_candidate=bool(include_forecast_source_weight_gate),
        require_ablation=bool(include_forecast_source_weight_gate),
    )

    gates = [
        _make_gate(
            gate="prior_gate",
            description="P_prior(k) only uses historical climate, date, station, terrain, and weather type.",
            reports={
                "prior_unit_contract": _unit_stage_report(unit_report, "prior"),
                "prior_layer_contract": prior_report,
            },
            min_checked_facts=min_checked_facts,
            require_checked_facts=True,
            continue_to="likelihood_gate",
        ),
        _make_gate(
            gate="likelihood_gate",
            description="Forecast weak likelihood and METAR strong likelihood are both present and boundary-safe.",
            reports={
                "likelihood_unit_contract": _unit_stage_report(unit_report, "likelihood"),
                "source_acquisition_contract": source_acquisition_report,
                "forecast_weak_likelihood_contract": forecast_report,
                "metar_strong_likelihood_contract": metar_report,
            },
            min_checked_facts=min_checked_facts,
            require_checked_facts=True,
            continue_to="posterior_gate",
        ),
        _make_gate(
            gate="posterior_gate",
            description="The production posterior has one reported-X v5 mainline.",
            reports={
                "posterior_unit_contract": _unit_stage_report(unit_report, "posterior"),
                "posterior_entrypoint": posterior_entrypoint,
            },
            min_checked_facts=1,
            require_checked_facts=True,
            continue_to="posterior_ssot_gate",
        ),
        _make_gate(
            gate="posterior_ssot_gate",
            description="posterior_ssot_fact and same-source target selection are available from the fact snapshot.",
            reports={
                "posterior_ssot_fact_coverage": posterior_ssot_report,
                "writer_persisted_apply_contract": writer_apply_report,
                "canonical_writer_persisted_coverage": canonical_persisted_report,
            },
            min_checked_facts=min_checked_facts,
            require_checked_facts=True,
            continue_to="downstream_read_only_consumers",
        ),
    ]
    if include_production_uniqueness:
        gates.append(
            _make_gate(
                gate="production_chain_uniqueness_gate",
                description="No second posterior, UI recomputation, non-canonical fact writer, or monitor signal alias.",
                reports={"production_chain_uniqueness": chain_report},
                min_checked_facts=0,
                require_checked_facts=False,
                continue_to="ui_trade_review_release_checks",
            )
        )
    if include_parameter_governance:
        gates.append(
            _make_gate(
                gate="parameter_governance_surface_gate",
                description="Parameter, config, launchd restart, and rollback surfaces are read-only/audited and cannot bypass approved calibration candidates.",
                reports={"parameter_governance_surface": parameter_governance_report},
                min_checked_facts=0,
                require_checked_facts=False,
                continue_to="ui_snapshot_lineage_gate",
            )
        )
    if include_forecast_source_weight_gate:
        gates.append(
            _make_gate(
                gate="forecast_source_weight_gate",
                description="External-only contextual/curve source weights pass causal OOS and remain behind explicit approval.",
                reports={"forecast_source_weight": forecast_weight_report},
                min_checked_facts=1,
                require_checked_facts=True,
                continue_to="ui_snapshot_lineage_gate",
            )
        )
    if include_ui_lineage:
        gates.append(
            _make_gate(
                gate="ui_snapshot_lineage_gate",
                description="/api/ui_snapshot renders the same fact_snapshot_id/source_observation_id/posterior_run_id as the backend SSOT fact.",
                reports={"ui_snapshot_lineage": ui_lineage_report},
                min_checked_facts=min_checked_facts,
                require_checked_facts=True,
                continue_to="ssot_chain_replay_gate",
            )
        )
    if include_chain_replay:
        gates.append(
            _make_gate(
                gate="ssot_chain_replay_gate",
                description="Signal-to-order replay is read-only and proves fact -> posterior -> decision -> job/order lineage when a hard-source sample exists.",
                reports={"ssot_chain_replay": replay_report},
                min_checked_facts=0,
                require_checked_facts=False,
                continue_to="release_gate_complete",
            )
        )

    blockers = [
        dict(blocker)
        for gate in gates
        for blocker in gate.get("blockers") or []
        if isinstance(blocker, Mapping)
    ]
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "hierarchical_probability_release_gate_read_only",
        "root": str(Path(root).expanduser().resolve()),
        "db": str(db),
        "guards": _guards(),
        "scope": {
            "since_hours": float(since_hours),
            "latest_only": bool(latest_only),
            "active_only": bool(active_only),
            "project_forward": bool(project_forward),
            "limit": int(limit),
            "min_checked_facts": max(1, int(min_checked_facts or 1)),
            "include_production_uniqueness": bool(include_production_uniqueness),
            "include_parameter_governance": bool(include_parameter_governance),
            "include_forecast_source_weight_gate": bool(include_forecast_source_weight_gate),
            "include_ui_lineage": bool(include_ui_lineage),
            "ui_mode": str(ui_mode or "summary"),
            "ui_run_date": str(ui_run_date or ""),
            "ui_limit": int(ui_limit),
            "ui_city_filter": str(ui_city_filter or ""),
            "run_ui_replay": bool(run_ui_replay),
            "include_chain_replay": bool(include_chain_replay),
            "replay_limit": int(replay_limit),
            "require_awc_signal_sample": bool(require_awc_signal_sample),
        },
        "gate_order": [str(gate.get("gate") or "") for gate in gates],
        "checks": {str(gate.get("gate") or ""): bool(gate.get("ok")) for gate in gates},
        "gates": gates,
        "blockers": blockers,
        "next_actions": (
            [
                "fix_failed_probability_release_gate_before_downstream_ui_trade_or_review_work",
                "rerun_scripts/hierarchical_probability_release_gate.py",
            ]
            if blockers
            else [
                "probability_release_gates_ok_continue_to_downstream_read_only_consumers",
                "keep_prior_likelihood_posterior_gates_in_release_checklist",
            ]
        ),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only release gate for prior -> likelihood -> posterior -> posterior SSOT."
    )
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since-hours", type=float, default=168.0)
    parser.add_argument("--all-history", action="store_true", help="Audit history scope instead of latest facts only.")
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--persisted-only", action="store_true", help="Do not use no-apply writer projection.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--max-items-output", type=int, default=5)
    parser.add_argument("--min-checked-facts", type=int, default=1)
    parser.add_argument("--skip-production-uniqueness", action="store_true")
    parser.add_argument("--skip-parameter-governance", action="store_true")
    parser.add_argument("--forecast-source-weight-artifact", type=Path)
    parser.add_argument("--forecast-source-weight-shadow", type=Path)
    parser.add_argument("--forecast-source-weight-ablation", type=Path)
    parser.add_argument("--max-samples-per-pattern", type=int, default=10)
    parser.add_argument("--skip-ui-lineage", action="store_true")
    parser.add_argument("--ui-mode", choices=("full", "summary"), default="summary")
    parser.add_argument("--ui-run-date", default="")
    parser.add_argument("--ui-limit", type=int, default=120)
    parser.add_argument("--ui-city-filter", default="")
    parser.add_argument("--skip-ui-replay", action="store_true")
    parser.add_argument("--skip-chain-replay", action="store_true")
    parser.add_argument("--replay-limit", type=int, default=50)
    parser.add_argument("--require-awc-signal-sample", action="store_true")
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    weight_artifact = json.loads(args.forecast_source_weight_artifact.read_text(encoding="utf-8")) if args.forecast_source_weight_artifact else None
    weight_shadow = json.loads(args.forecast_source_weight_shadow.read_text(encoding="utf-8")) if args.forecast_source_weight_shadow else None
    weight_ablation = json.loads(args.forecast_source_weight_ablation.read_text(encoding="utf-8")) if args.forecast_source_weight_ablation else None
    report = build_hierarchical_probability_release_gate(
        root=Path(args.root),
        db_path=Path(args.db),
        since_hours=float(args.since_hours),
        latest_only=not bool(args.all_history),
        active_only=bool(args.active_only),
        project_forward=not bool(args.persisted_only),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        min_checked_facts=max(1, int(args.min_checked_facts or 1)),
        include_production_uniqueness=not bool(args.skip_production_uniqueness),
        include_parameter_governance=not bool(args.skip_parameter_governance),
        include_forecast_source_weight_gate=bool(args.forecast_source_weight_artifact or args.forecast_source_weight_shadow or args.forecast_source_weight_ablation),
        forecast_source_weight_artifact=weight_artifact,
        forecast_source_weight_shadow=weight_shadow,
        forecast_source_weight_ablation=weight_ablation,
        max_samples_per_pattern=max(0, int(args.max_samples_per_pattern or 0)),
        include_ui_lineage=not bool(args.skip_ui_lineage),
        ui_mode=str(args.ui_mode or "summary"),
        ui_run_date=str(args.ui_run_date or ""),
        ui_limit=int(args.ui_limit),
        ui_city_filter=str(args.ui_city_filter or ""),
        run_ui_replay=not bool(args.skip_ui_replay),
        include_chain_replay=not bool(args.skip_chain_replay),
        replay_limit=int(args.replay_limit),
        require_awc_signal_sample=bool(args.require_awc_signal_sample),
    )
    if args.summary:
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "execution_mode": report["execution_mode"],
            "scope": report["scope"],
            "gate_order": report["gate_order"],
            "checks": report["checks"],
            "blockers": report["blockers"],
            "next_actions": report["next_actions"],
            "guards": report["guards"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
