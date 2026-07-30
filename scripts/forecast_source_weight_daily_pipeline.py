#!/usr/bin/env python3
"""Daily external-only source-weight research pipeline; never applies production."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"
DAILY_ROOT = RESEARCH_ROOT / "daily"
CONTEXT_FIELDS = (
    "forecast_consensus_peak_c",
    "forecast_peak_spread_c",
    "forecast_consensus_diurnal_range_c",
    "forecast_consensus_warming_from_day_start_c",
)
SCHEMA_VERSION = "forecast_source_weight_daily_pipeline_v1"
CHENGDU_ZONE = ZoneInfo("Asia/Shanghai")

from forecast_source_weight_artifact_registry import load_approved_forecast_source_weight_artifact
from scripts.build_static_forecast_source_weight_champion import build_static_champion
from scripts.external_forecast_coverage_manifest import build_external_forecast_coverage_manifest
from scripts.fetch_open_meteo_source_skill_runs import execute_request_plan
from scripts.forecast_source_weight_plan_completion_audit import build_completion_audit
from scripts.forecast_source_weight_promotion_packet import build_promotion_packet
from scripts.forecast_source_weight_release_gate import build_forecast_source_weight_release_gate
from scripts.forecast_source_weight_daily_acceptance_audit import (
    SYSTEM_LAUNCHD_ROOT,
    _load_plist,
    build_daily_acceptance_audit,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _run_script(script: str, arguments: Sequence[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *map(str, arguments)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60 * 30,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "")[-4000:]
        raise RuntimeError(f"pipeline_step_failed:{script}:{result.returncode}:{detail}")


def build_daily_pipeline_plan(*, max_incremental_requests: int = 12) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "mode": "plan_only",
        "source_data_policy": "external_only",
        "max_incremental_open_meteo_requests": int(max_incremental_requests),
        "stages": [
            "discover_external_settled_events",
            "plan_and_fetch_only_missing_exact_runs",
            "capture_current_three_model_wu_twc_forward_curves",
            "normalize_immutable_forecasts",
            "export_amsc_observed_first_seen_and_taf",
            "normalize_wu_twc_forward_snapshots",
            "build_strict_as_of_dataset_and_curve_checkpoints",
            "materialize_d1_d0_season_accuracy_research_report",
            "train_static_and_contextual_artifacts",
            "rolling_oos_and_sequential_ablation",
            "release_gate_and_optional_proposal_without_apply",
            "completion_audit",
        ],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "applies_artifact": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def _request_key(row: Mapping[str, Any]) -> str:
    return "|".join(
        (
            str(row.get("source_id") or ""),
            str(row.get("target_event_date_local") or ""),
            str(row.get("run") or ""),
        )
    )


def _incremental_fetch(
    *,
    coverage: Mapping[str, Any],
    normalized: Mapping[str, Any],
    max_requests: int,
    state_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    state = _read(state_path, {})
    permanent_unavailable = set(str(value) for value in (state or {}).get("permanent_unavailable_request_keys") or [])
    seed = _read(RESEARCH_ROOT / "evaluation" / "open_meteo_realized_coverage.json", {})
    for row in (seed or {}).get("explicit_unavailable") or []:
        permanent_unavailable.add(_request_key(row))
    existing = {
        (str(row.get("source_id") or ""), str(row.get("target_event_date_local") or ""))
        for row in normalized.get("forecast_snapshots") or []
        if row.get("state") == "available"
    }
    planned = [dict(row) for row in coverage.get("single_runs_exact_requests") or [] if isinstance(row, Mapping)]
    missing = [
        row
        for row in planned
        if (str(row.get("source_id") or ""), str(row.get("target_event_date_local") or "")) not in existing
        and _request_key(row) not in permanent_unavailable
    ]
    if len(missing) > int(max_requests):
        raise RuntimeError(f"incremental_request_bound_exceeded:{len(missing)}>{int(max_requests)}")
    report = execute_request_plan(
        missing,
        fetch=True,
        allow_research_write=True,
        max_requests=int(max_requests),
        max_retries=2,
        min_interval_seconds=0.25,
    )
    by_url = {str(row.get("url") or ""): row for row in missing}
    for result in report.get("results") or []:
        if result.get("state_reason") == "provider_run_unavailable":
            request = by_url.get(str(result.get("url") or ""))
            if request:
                permanent_unavailable.add(_request_key(request))
    next_state = {
        "schema": "forecast_source_open_meteo_incremental_state_v1",
        "updated_at": _now(),
        "permanent_unavailable_request_keys": sorted(permanent_unavailable),
        "planned_request_count": len(planned),
        "missing_request_count": len(missing),
        "success_count": report.get("success_count", 0),
        "failure_count": report.get("failure_count", 0),
    }
    _atomic_json(state_path, next_state)
    _atomic_json(report_path, report)
    return next_state


def run_daily_pipeline(*, max_incremental_requests: int = 12) -> dict[str, Any]:
    started_at = _now()
    DAILY_ROOT.mkdir(parents=True, exist_ok=True)
    discovery_path = DAILY_ROOT / "discovery_latest.json"
    coverage_path = DAILY_ROOT / "coverage_latest.json"
    open_meteo_path = DAILY_ROOT / "open_meteo_snapshots.json"
    amsc_path = DAILY_ROOT / "amsc_forward.json"
    forward_metrics_path = DAILY_ROOT / "wu_twc_forward_snapshots.json"
    current_forward_capture_path = DAILY_ROOT / "current_forward_capture.json"
    cutoffs_path = DAILY_ROOT / "cutoffs.json"
    dataset_path = DAILY_ROOT / "score_dataset.json"
    accuracy_report_path = DAILY_ROOT / "accuracy_by_horizon_season.json"
    curve_inputs_path = DAILY_ROOT / "curve_inputs.json"
    static_artifact_path = DAILY_ROOT / "static_champion.json"
    weather_artifact_path = DAILY_ROOT / "weather_context_challenger.json"
    weather_training_path = DAILY_ROOT / "weather_context_training.json"
    weather_shadow_path = DAILY_ROOT / "weather_context_shadow.json"
    ablation_path = DAILY_ROOT / "ablation.json"
    release_path = DAILY_ROOT / "release_gate.json"
    promotion_path = DAILY_ROOT / "promotion_packet.json"

    _run_script(
        "polymarket_settled_weather_event_discovery.py",
        ("--fetch", "--max-pages", "20", "--limit-per-type", "50", "--output", discovery_path, "--allow-research-write"),
    )
    # The hourly launchd job supplies the normal cadence.  Retaining one
    # capture here makes the daily research job self-contained after a host
    # restart or a missed hourly invocation; immutable payload dedupe makes it
    # harmless when the curve has not changed.
    _run_script(
        "capture_current_forecast_source_forward.py",
        (
            "--event-date",
            datetime.now(CHENGDU_ZONE).date().isoformat(),
            "--output",
            current_forward_capture_path,
            "--allow-research-write",
        ),
    )
    discovery = _read(discovery_path, {})
    coverage = build_external_forecast_coverage_manifest(discovery, as_of_date=date.today())
    _atomic_json(coverage_path, coverage)

    _run_script(
        "normalize_open_meteo_source_skill_runs.py",
        ("--envelopes", RESEARCH_ROOT / "raw", "--output", open_meteo_path, "--allow-research-write"),
    )
    incremental = _incremental_fetch(
        coverage=coverage,
        normalized=_read(open_meteo_path, {}),
        max_requests=max_incremental_requests,
        state_path=DAILY_ROOT / "open_meteo_incremental_state.json",
        report_path=DAILY_ROOT / "open_meteo_fetch_latest.json",
    )
    if int(incremental.get("success_count") or 0) > 0:
        _run_script(
            "normalize_open_meteo_source_skill_runs.py",
            ("--envelopes", RESEARCH_ROOT / "raw", "--output", open_meteo_path, "--allow-research-write"),
        )
    _run_script(
        "export_amsc_source_skill_forward.py",
        ("--archive-root", ROOT / "data" / "amsc_raw_history", "--output", amsc_path, "--allow-research-write"),
    )
    _run_script(
        "normalize_forecast_source_forward_metrics.py",
        ("--envelopes", RESEARCH_ROOT / "raw", "--output", forward_metrics_path, "--allow-research-write"),
    )
    forecast_arguments = (
        "--forecast-snapshots", open_meteo_path,
        "--forecast-snapshots", amsc_path,
        "--forecast-snapshots", forward_metrics_path,
    )
    _run_script(
        "build_external_forecast_source_skill_cutoffs.py",
        ("--market-events", discovery_path, *forecast_arguments, "--output", cutoffs_path, "--allow-research-write"),
    )
    _run_script(
        "build_external_forecast_source_skill_dataset.py",
        (
            "--market-events", discovery_path,
            *forecast_arguments,
            "--cutoffs", cutoffs_path,
            "--output", dataset_path,
            "--allow-research-write",
        ),
    )
    _run_script(
        "build_forecast_source_accuracy_report.py",
        (
            "--score-dataset", dataset_path,
            "--output", accuracy_report_path,
            "--allow-research-write",
        ),
    )
    _run_script(
        "build_forecast_source_curve_shadow_inputs.py",
        (
            "--market-events", discovery_path,
            *forecast_arguments,
            "--observations", amsc_path,
            "--cutoffs", cutoffs_path,
            "--output", curve_inputs_path,
            "--allow-research-write",
        ),
    )
    _run_script(
        "build_static_forecast_source_weight_champion.py",
        ("--score-dataset", dataset_path, "--output", static_artifact_path, "--allow-research-write"),
    )
    context_arguments = tuple(value for field in CONTEXT_FIELDS for value in ("--context-field", field))
    _run_script(
        "train_forecast_source_contextual_weights.py",
        (
            "--score-dataset", dataset_path,
            "--specification", "weather_context",
            *context_arguments,
            "--output", weather_training_path,
            "--artifact-output", weather_artifact_path,
            "--allow-research-write",
        ),
    )
    _run_script(
        "evaluate_forecast_source_weight_shadow.py",
        (
            "--score-dataset", dataset_path,
            "--specification", "weather_context",
            *context_arguments,
            "--curve-inputs", curve_inputs_path,
            "--output", weather_shadow_path,
            "--allow-research-write",
        ),
    )
    _run_script(
        "evaluate_forecast_source_contextual_ablation.py",
        (
            "--score-dataset", dataset_path,
            *context_arguments,
            "--output", ablation_path,
            "--allow-research-write",
        ),
    )

    dataset = _read(dataset_path, {})
    accuracy_report = _read(accuracy_report_path, {})
    ablation = _read(ablation_path, {})
    selected = str(ablation.get("selected_specification") or "static_champion")
    static_artifact = _read(static_artifact_path, {})
    approved = load_approved_forecast_source_weight_artifact()
    champion = approved if isinstance(approved, Mapping) else static_artifact
    candidate = _read(weather_artifact_path, {})
    selected_shadow = _read(weather_shadow_path, {})
    if selected in {"global", "seasonal_lead"}:
        selected_artifact_path = DAILY_ROOT / f"{selected}_challenger.json"
        selected_training_path = DAILY_ROOT / f"{selected}_training.json"
        _run_script(
            "train_forecast_source_contextual_weights.py",
            (
                "--score-dataset", dataset_path,
                "--specification", selected,
                "--parent-artifact-sha256", str(champion.get("artifact_sha256") or ""),
                "--output", selected_training_path,
                "--artifact-output", selected_artifact_path,
                "--allow-research-write",
            ),
        )
        candidate = _read(selected_artifact_path, {})
        selected_shadow = dict((ablation.get("layers") or {}).get(selected, {}).get("report") or {})
    elif selected == "weather_context":
        candidate = _read(weather_artifact_path, {})
        selected_shadow = dict((ablation.get("layers") or {}).get("weather_context", {}).get("report") or selected_shadow)

    release = build_forecast_source_weight_release_gate(
        artifact=candidate,
        shadow_evaluation=selected_shadow,
        ablation=ablation,
        require_candidate=True,
        require_ablation=True,
    )
    _atomic_json(release_path, release)
    curve_release: dict[str, Any] = {}
    curve_shadow = _read(weather_shadow_path, {})
    if selected in {"global", "seasonal_lead"}:
        selected_curve_shadow_path = DAILY_ROOT / f"{selected}_curve_shadow.json"
        _run_script(
            "evaluate_forecast_source_weight_shadow.py",
            (
                "--score-dataset", dataset_path,
                "--specification", selected,
                "--curve-inputs", curve_inputs_path,
                "--output", selected_curve_shadow_path,
                "--allow-research-write",
            ),
        )
        curve_shadow = _read(selected_curve_shadow_path, {})
    if selected != "static_champion" and release.get("ok") is True:
        curve_release = build_forecast_source_weight_release_gate(
            artifact=candidate,
            shadow_evaluation=curve_shadow,
            ablation=ablation,
            require_candidate=True,
            require_ablation=True,
            require_curve=True,
        )
    _atomic_json(DAILY_ROOT / "curve_release_gate.json", curve_release)
    packet: dict[str, Any] = {}
    if selected != "static_champion" and release.get("ok") is True:
        include_curve = curve_release.get("ok") is True
        packet_shadow = curve_shadow if include_curve else selected_shadow
        packet = build_promotion_packet(
            challenger_artifact=candidate,
            shadow_evaluation=packet_shadow,
            champion_artifact=champion,
            ablation=ablation,
            include_curve=include_curve,
        )
        _atomic_json(promotion_path, packet)
    elif promotion_path.exists():
        promotion_path.unlink()

    completion = build_completion_audit(
        discovery=discovery,
        coverage=coverage,
        fetch_report=_read(DAILY_ROOT / "open_meteo_fetch_latest.json", {}),
        realized_coverage=_read(RESEARCH_ROOT / "evaluation" / "open_meteo_realized_coverage.json", {}),
        skill_dataset=dataset,
        artifact=candidate,
        shadow=selected_shadow,
        promotion_packet=packet,
    )
    _atomic_json(DAILY_ROOT / "completion_audit.json", completion)
    status = {
        "schema": SCHEMA_VERSION,
        "mode": "executed",
        "state": "proposal_ready" if packet.get("promotion_proposal_eligible") is True else "champion_retained",
        "started_at": started_at,
        "completed_at": _now(),
        "source_data_policy": "external_only",
        "selected_specification": selected,
        "approved_champion_loaded": approved is not None,
        "score_row_count": dataset.get("available_score_row_count", 0),
        "paired_event_count": dataset.get("paired_event_count", 0),
        "forecast_source_accuracy_report": {
            "schema": accuracy_report.get("schema"),
            "report_sha256": accuracy_report.get("report_sha256"),
            "generated_at": accuracy_report.get("generated_at"),
            "research_only": accuracy_report.get("research_only") is True,
            "distinct_event_days": (accuracy_report.get("coverage") or {}).get("distinct_event_days"),
        },
        "curve_event_day_count": _read(curve_inputs_path, {}).get("curve_event_day_count", 0),
        "incremental_fetch": incremental,
        "release_gate_ok": release.get("ok") is True,
        "release_blockers": release.get("blockers", []),
        "curve_release_gate_ok": curve_release.get("ok") is True,
        "curve_release_blockers": curve_release.get("blockers", []),
        "completion_passed_count": completion.get("passed_count", 0),
        "completion_required_count": completion.get("required_count", 0),
        "completion_blockers": completion.get("blockers", []),
        "production_apply_allowed": False,
        "guards": build_daily_pipeline_plan(max_incremental_requests=max_incremental_requests)["guards"],
    }
    _atomic_json(DAILY_ROOT / "status_latest.json", status)
    acceptance = build_daily_acceptance_audit(
        status=status,
        curve_inputs=_read(curve_inputs_path, {}),
        amsc_forward=_read(amsc_path, {}),
        research_plist=_load_plist(ROOT / "launchd" / "com.weather.forecast_source_weight_research.plist"),
        monitor_plist=_load_plist(ROOT / "launchd" / "com.weather.monitor.daemon.plist"),
        forward_capture_plist=_load_plist(ROOT / "launchd" / "com.weather.forecast_source_forward_capture.plist"),
        installed_research_plist=_load_plist(SYSTEM_LAUNCHD_ROOT / "com.weather.forecast_source_weight_research.plist"),
        installed_monitor_plist=_load_plist(SYSTEM_LAUNCHD_ROOT / "com.weather.monitor.daemon.plist"),
        installed_forward_capture_plist=_load_plist(SYSTEM_LAUNCHD_ROOT / "com.weather.forecast_source_forward_capture.plist"),
    )
    _atomic_json(DAILY_ROOT / "acceptance_audit.json", acceptance)
    status["daily_acceptance_audit_ok"] = acceptance.get("ok") is True
    status["daily_acceptance_audit_blockers"] = acceptance.get("blockers", [])
    _atomic_json(DAILY_ROOT / "status_latest.json", status)
    history_name = started_at.replace(":", "").replace("-", "") + ".json"
    _atomic_json(DAILY_ROOT / "runs" / history_name, status)
    return status


def main() -> int:
    if (ROOT / "data" / "reported_trajectory_core_only.enabled").exists():
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-incremental-requests", type=int, default=12)
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps(build_daily_pipeline_plan(max_incremental_requests=args.max_incremental_requests), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    DAILY_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = DAILY_ROOT / "pipeline.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"schema": SCHEMA_VERSION, "state": "skipped_already_running"}, sort_keys=True))
            return 0
        try:
            status = run_daily_pipeline(max_incremental_requests=max(0, int(args.max_incremental_requests)))
        except Exception as exc:
            failure = {
                "schema": SCHEMA_VERSION,
                "mode": "executed",
                "state": "operational_failure",
                "failed_at": _now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "production_apply_allowed": False,
                "guards": build_daily_pipeline_plan(max_incremental_requests=args.max_incremental_requests)["guards"],
            }
            _atomic_json(DAILY_ROOT / "status_latest.json", failure)
            print(json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True))
            return 1
    print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
