#!/usr/bin/env python3
"""Audit the daily source-weight automation and its fail-closed boundaries."""

from __future__ import annotations

import argparse
import json
import plistlib
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DAILY_ROOT = ROOT / "data" / "research" / "forecast_source_skill" / "daily"
RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"
SYSTEM_LAUNCHD_ROOT = Path("/Library/LaunchDaemons")


def build_daily_acceptance_audit(
    *,
    status: Mapping[str, Any],
    curve_inputs: Mapping[str, Any],
    amsc_forward: Mapping[str, Any] | None = None,
    research_plist: Mapping[str, Any],
    monitor_plist: Mapping[str, Any],
    forward_capture_plist: Mapping[str, Any] | None = None,
    installed_research_plist: Mapping[str, Any] | None = None,
    installed_monitor_plist: Mapping[str, Any] | None = None,
    installed_forward_capture_plist: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    arguments = [str(value) for value in research_plist.get("ProgramArguments") or []]
    forward_capture_plist = forward_capture_plist or {}
    forward_arguments = [str(value) for value in forward_capture_plist.get("ProgramArguments") or []]
    forward_command = " ".join(forward_arguments)
    installed_research_plist = installed_research_plist or {}
    installed_monitor_plist = installed_monitor_plist or {}
    installed_forward_capture_plist = installed_forward_capture_plist or {}
    amsc_forward = amsc_forward or {}
    installed_research_arguments = [str(value) for value in installed_research_plist.get("ProgramArguments") or []]
    installed_forward_arguments = [str(value) for value in installed_forward_capture_plist.get("ProgramArguments") or []]
    installed_forward_command = " ".join(installed_forward_arguments)
    taf_rows = [
        row
        for row in (amsc_forward.get("forecast_snapshots") or [])
        if isinstance(row, Mapping)
        and row.get("state") == "available"
        and row.get("source_id") == "zuuu_taf_forecast_amsc"
    ]
    strict_taf_rows = [
        row
        for row in taf_rows
        if str(row.get("lineage_quality") or "").startswith("immutable_")
        and row.get("arrival_time_quality") == "observed_first_seen"
    ]
    status_guards = status.get("guards") if isinstance(status.get("guards"), Mapping) else {}
    checks = {
        "daily_run_completed": status.get("schema") == "forecast_source_weight_daily_pipeline_v1" and status.get("state") in {"champion_retained", "proposal_ready"},
        "daily_run_production_apply_closed": status.get("production_apply_allowed") is False,
        "daily_run_mutation_guards_closed": all(status_guards.get(key) is False for key in ("changes_parameters", "applies_artifact", "places_orders", "restarts_launchd")),
        "launchd_executes_bounded_pipeline": "--execute" in arguments and "--max-incremental-requests" in arguments,
        "launchd_has_no_apply_argument": "apply" not in " ".join(arguments).lower(),
        "launchd_not_keepalive_monitor": research_plist.get("KeepAlive") is False,
        "monitor_archives_wu_twc_forward": str((monitor_plist.get("EnvironmentVariables") or {}).get("FORECAST_SOURCE_FORWARD_ARCHIVE_ENABLED") or "") == "1",
        "hourly_forward_capture_is_research_only": (
            forward_capture_plist.get("Label") == "com.weather.forecast_source_forward_capture"
            and forward_capture_plist.get("StartCalendarInterval") == {"Minute": 0}
            and [str(value) for value in (forward_capture_plist.get("ProgramArguments") or [])][:2] == ["/bin/zsh", "-c"]
            and "capture_current_forecast_source_forward.py" in forward_command
            and "--allow-research-write" in forward_command
            and "forecast_source_skill/forward" in forward_command
            and "--live" not in forward_command
        ),
        "installed_daily_research_schedule_matches_contract": (
            installed_research_plist.get("Label") == "com.weather.forecast_source_weight_research"
            and "--execute" in installed_research_arguments
            and "--max-incremental-requests" in installed_research_arguments
            and "apply" not in " ".join(installed_research_arguments).lower()
            and installed_research_plist.get("KeepAlive") is False
        ),
        "installed_hourly_forward_capture_matches_contract": (
            installed_forward_capture_plist.get("Label") == "com.weather.forecast_source_forward_capture"
            and installed_forward_capture_plist.get("StartCalendarInterval") == {"Minute": 0}
            and [str(value) for value in (installed_forward_capture_plist.get("ProgramArguments") or [])][:2] == ["/bin/zsh", "-c"]
            and "capture_current_forecast_source_forward.py" in installed_forward_command
            and "--allow-research-write" in installed_forward_command
            and "forecast_source_skill/forward" in installed_forward_command
            and "--live" not in installed_forward_command
        ),
        "installed_monitor_archives_wu_twc_forward": str(
            (installed_monitor_plist.get("EnvironmentVariables") or {}).get("FORECAST_SOURCE_FORWARD_ARCHIVE_ENABLED") or ""
        ) == "1",
        "taf_forward_has_immutable_first_seen_lineage": bool(strict_taf_rows),
        "curve_requires_observed_first_seen": curve_inputs.get("guards", {}).get("requires_observed_first_seen_arrival_lineage") is True,
        "curve_rejects_future_observations": curve_inputs.get("guards", {}).get("uses_observations_after_curve_cutoff") is False,
        "proposal_or_champion_state_consistent": (
            status.get("state") == "proposal_ready" and status.get("release_gate_ok") is True
        ) or (
            status.get("state") == "champion_retained" and status.get("production_apply_allowed") is False
        ),
    }
    blockers = [name for name, ok in checks.items() if not ok]
    return {
        "schema": "forecast_source_weight_daily_acceptance_audit_v1",
        "ok": not blockers,
        "read_only": True,
        "checks": checks,
        "blockers": blockers,
        "evidence": {
            "status_state": status.get("state"),
            "selected_specification": status.get("selected_specification"),
            "paired_event_count": status.get("paired_event_count"),
            "curve_event_day_count": status.get("curve_event_day_count"),
            "release_gate_ok": status.get("release_gate_ok"),
            "curve_release_gate_ok": status.get("curve_release_gate_ok"),
            "hourly_forward_capture_label": forward_capture_plist.get("Label"),
            "installed_research_schedule_label": installed_research_plist.get("Label"),
            "installed_forward_capture_label": installed_forward_capture_plist.get("Label"),
            "installed_monitor_forward_archive_enabled": str(
                (installed_monitor_plist.get("EnvironmentVariables") or {}).get("FORECAST_SOURCE_FORWARD_ARCHIVE_ENABLED") or ""
            ),
            "taf_forward_snapshot_count": len(taf_rows),
            "taf_forward_strict_lineage_count": len(strict_taf_rows),
        },
        "guards": {"writes_archive_db": False, "changes_parameters": False, "places_orders": False, "restarts_launchd": False},
    }


def _load_plist(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=Path, default=DAILY_ROOT / "status_latest.json")
    parser.add_argument("--curve-inputs", type=Path, default=DAILY_ROOT / "curve_inputs.json")
    parser.add_argument("--amsc-forward", type=Path, default=DAILY_ROOT / "amsc_forward.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    status = json.loads(args.status.read_text(encoding="utf-8"))
    curve_inputs = json.loads(args.curve_inputs.read_text(encoding="utf-8"))
    amsc_forward = json.loads(args.amsc_forward.read_text(encoding="utf-8"))
    research_plist = _load_plist(ROOT / "launchd" / "com.weather.forecast_source_weight_research.plist")
    monitor_plist = _load_plist(ROOT / "launchd" / "com.weather.monitor.daemon.plist")
    forward_capture_plist = _load_plist(ROOT / "launchd" / "com.weather.forecast_source_forward_capture.plist")
    report = build_daily_acceptance_audit(
        status=status,
        curve_inputs=curve_inputs,
        amsc_forward=amsc_forward,
        research_plist=research_plist,
        monitor_plist=monitor_plist,
        forward_capture_plist=forward_capture_plist,
        installed_research_plist=_load_plist(SYSTEM_LAUNCHD_ROOT / "com.weather.forecast_source_weight_research.plist"),
        installed_monitor_plist=_load_plist(SYSTEM_LAUNCHD_ROOT / "com.weather.monitor.daemon.plist"),
        installed_forward_capture_plist=_load_plist(SYSTEM_LAUNCHD_ROOT / "com.weather.forecast_source_forward_capture.plist"),
    )
    if args.output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
        output = args.output.expanduser().resolve()
        root = RESEARCH_ROOT.resolve()
        if output != root and root not in output.parents:
            raise ValueError("daily_acceptance_output_outside_research_root")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
