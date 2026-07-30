#!/usr/bin/env python3
"""Read-only inventory for parameter/config/restart governance surfaces.

This audit keeps the daily-review calibration chain separate from legacy
optimizer/config paths. It never writes DB/config files and never calls
launchctl.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from parameter_change_governance import parameter_change_governance_contract  # noqa: E402


SCHEMA_VERSION = "parameter_governance_surface_audit_v1"

SECRET_KEY_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PRIVATE", "CREDENTIAL")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _redact_if_secret(key: str, value: Any) -> Any:
    upper = str(key or "").upper()
    if any(marker in upper for marker in SECRET_KEY_MARKERS):
        return "***REDACTED***" if str(value or "") else ""
    return value


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _categorize_env_key(key: str) -> str:
    upper = key.upper()
    if "OPTIMIZER" in upper or "BIAS" in upper or "CALIBR" in upper:
        return "calibration_or_optimizer"
    if upper.startswith("PM_HARD_SOURCE") or "AWC" in upper or "METAR" in upper:
        return "source_acquisition_or_latency"
    if upper.startswith("PM_AUTO_TRADE") or upper.startswith("PM_EDGE") or upper.startswith("PM_EXIT"):
        return "trade_strategy_or_execution"
    if upper.startswith("PM_ARCHIVE") or upper.startswith("PM_DB"):
        return "db_retention_or_maintenance"
    if upper.endswith("_APPLY") or "_APPLY_" in upper:
        return "apply_gate"
    return "runtime_config"


def _load_launchd_plists(root: Path) -> list[dict[str, Any]]:
    launchd_dir = root / "launchd"
    items: list[dict[str, Any]] = []
    if not launchd_dir.exists():
        return items
    for path in sorted(launchd_dir.glob("*.plist")):
        try:
            with path.open("rb") as handle:
                data = plistlib.load(handle)
        except Exception as exc:
            items.append(
                {
                    "path": _rel(path, root),
                    "parse_ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "label": "",
                    "env": {},
                    "program": "",
                }
            )
            continue
        env = data.get("EnvironmentVariables") if isinstance(data, Mapping) else {}
        program = " ".join(str(part) for part in _as_sequence(data.get("ProgramArguments")))
        items.append(
            {
                "path": _rel(path, root),
                "parse_ok": True,
                "label": str(data.get("Label") or ""),
                "disabled": bool(data.get("Disabled")),
                "run_at_load": bool(data.get("RunAtLoad")),
                "keep_alive": bool(data.get("KeepAlive")),
                "env": dict(env) if isinstance(env, Mapping) else {},
                "program": program,
            }
        )
    return items


def _launchd_env_inventory(plists: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for plist in plists:
        env = plist.get("env") if isinstance(plist.get("env"), Mapping) else {}
        for key, value in sorted(env.items()):
            key_text = str(key or "")
            category = _categorize_env_key(key_text)
            rows.append(
                {
                    "key": key_text,
                    "value": _redact_if_secret(key_text, value),
                    "label": plist.get("label"),
                    "path": plist.get("path"),
                    "category": category,
                    "governance_required": category
                    in {
                        "calibration_or_optimizer",
                        "trade_strategy_or_execution",
                        "source_acquisition_or_latency",
                        "apply_gate",
                    },
                    "truthy": _truthy(value),
                }
            )
    return rows


def _source_state(root: Path) -> dict[str, str]:
    paths = {
        "parameter_change_governance": root / "parameter_change_governance.py",
        "parameter_change_event_transition_apply": root / "scripts" / "parameter_change_event_transition_apply.py",
        "daily_review_persistence_writer": root / "daily_review_persistence_writer.py",
        "daily_review_persistence_apply": root / "scripts" / "daily_review_persistence_apply.py",
        "feedback_loop": root / "scripts" / "feedback_loop.py",
        "feedback_loop_hourly": root / "scripts" / "feedback_loop_hourly.sh",
        "ui_server": root / "ui" / "server.py",
        "weatherctl": root / "scripts" / "weatherctl.sh",
        "install_monitor_launchdaemon": root / "scripts" / "install_monitor_launchdaemon.sh",
        "install_launchdaemon": root / "scripts" / "install_launchdaemon.sh",
        "install_launchagent": root / "scripts" / "install_launchagent.sh",
    }
    return {name: _read_text(path) for name, path in paths.items()}


def _parameter_write_surfaces(root: Path, sources: Mapping[str, str]) -> list[dict[str, Any]]:
    ui = sources.get("ui_server", "")
    feedback = sources.get("feedback_loop", "")
    writer = sources.get("daily_review_persistence_writer", "")
    apply_cli = sources.get("daily_review_persistence_apply", "")
    transition_cli = sources.get("parameter_change_event_transition_apply", "")
    surfaces: list[dict[str, Any]] = [
        {
            "id": "bayes_parameter_change_governance_runtime",
            "file": "parameter_change_governance.py",
            "role": "state_machine_and_contract",
            "writes_archive_db": False,
            "changes_parameters": False,
            "allowed_states": parameter_change_governance_contract()["state_machine"]["states"],
            "required_state_before_apply": "approved",
            "boundary": "runtime emits proposed requests only; no production parameter mutation",
            "status": "canonical_no_apply_contract",
        },
        {
            "id": "daily_review_parameter_event_ledger",
            "file": "daily_review_persistence_writer.py",
            "role": "planned_parameter_change_event_ledger",
            "writes_archive_db_if_confirmed": "parameter_change_events_only",
            "changes_parameters": False,
            "requires_confirmation": "APPLY_DAILY_REVIEW_PARAMETER_EVENTS",
            "allowed_content": "ledger event for calibration_candidate_id/state/change_json",
            "boundary": "parameter event ledger is not production config mutation",
            "status": "controlled_ledger_write"
            if "allow_parameter_event_apply" in writer or "PARAMETER_CHANGE_EVENT_INSERT_SQL" in writer
            else "not_detected",
        },
        {
            "id": "daily_review_persistence_apply_cli",
            "file": "scripts/daily_review_persistence_apply.py",
            "role": "controlled_review_calibration_candidate_persistence",
            "writes_archive_db_if_confirmed": True,
            "changes_parameters": False,
            "required_confirmations": [
                "APPLY_DAILY_REVIEW_PERSISTENCE",
                "APPLY_DAILY_REVIEW_PARAMETER_EVENTS for parameter event ledger only",
            ],
            "boundary": "does not modify auto_trade_config or restart launchd",
            "status": "controlled_apply_contract"
            if "CONFIRMATION_PHRASE" in apply_cli and "PARAMETER_EVENT_CONFIRMATION_PHRASE" in apply_cli
            else "not_detected",
        },
        {
            "id": "parameter_change_event_transition_apply_cli",
            "file": "scripts/parameter_change_event_transition_apply.py",
            "role": "controlled_parameter_change_event_transition_ledger",
            "writes_archive_db_if_confirmed": "parameter_change_events_only",
            "changes_parameters": False,
            "required_confirmation": "APPLY_PARAMETER_CHANGE_EVENT_TRANSITIONS",
            "allowed_transitions": parameter_change_governance_contract()["state_machine"]["transitions"],
            "boundary": "append-only state transition ledger; no production config mutation",
            "status": "controlled_transition_contract"
            if "APPLY_PARAMETER_CHANGE_EVENT_TRANSITIONS" in transition_cli
            and "STATE_TRANSITIONS" in transition_cli
            and "changes_parameters" in transition_cli
            else "not_detected",
        },
    ]
    if "PM_OPTIMIZER_AUTO_APPLY" in feedback or "apply_optimizer_suggestions" in feedback:
        surfaces.append(
            {
                "id": "legacy_feedback_optimizer_auto_apply",
                "file": "scripts/feedback_loop.py",
                "role": "legacy_edge_bias_optimizer",
                "writes_archive_db_if_enabled": ["edge_bias_calibration_*", "feedback_snapshots", "optimizer_actions"],
                "changes_parameters_if_enabled": "data/auto_trade_config.json entry_edge_confidence_min",
                "restarts_launchd_if_enabled": "restart_auto_trade_watcher via ui.server",
                "governance_gap": "not_bound_to_calibration_candidate_approved_state",
                "boundary_required": "diagnostic_snapshot_only_until_migrated_to_parameter_change_governance",
                "status": "legacy_conflict_review_required",
            }
        )
    if "/api/optimizer-apply" in ui and "apply_edge_bias_gate_suggestion" in ui:
        endpoint_block = re.search(r'path\s*==\s*["\']/api/optimizer-apply["\'](?P<body>.{0,2200})', ui, re.S)
        body = endpoint_block.group("body") if endpoint_block else ""
        retired_no_write = (
            "_legacy_optimizer_apply_retired_payload" in body
            or "legacy_optimizer_apply_retired" in body
        )
        surfaces.append(
            {
                "id": "ui_optimizer_apply_endpoint",
                "file": "ui/server.py",
                "role": "manual_http_config_apply_endpoint",
                "writes_config_if_called": False if retired_no_write else "data/auto_trade_config.json",
                "restarts_launchd_default": False if retired_no_write else "restart=1 unless query restart=0",
                "confirmation_detected_near_endpoint": "confirm" in body.lower(),
                "retired_no_write": retired_no_write,
                "governance_gap": "not_bound_to_parameter_change_events_or_approved_candidate",
                "boundary_required": "must require explicit approval and calibration_candidate_id before use",
                "status": "retired_no_write" if retired_no_write else "unconfirmed_legacy_apply_surface",
            }
        )
    if "save_auto_trade_config(data)" in ui and "def load_auto_trade_config" in ui:
        surfaces.append(
            {
                "id": "auto_trade_config_load_time_migration_write",
                "file": "ui/server.py",
                "role": "config_schema_migration_on_read",
                "writes_config_if_triggered": "data/auto_trade_config.json",
                "changes_parameters": "can fill defaults while loading config",
                "governance_gap": "read path can write config outside parameter governance",
                "boundary_required": "schema migration must be explicit or audited separately",
                "status": "review_required",
            }
        )
    return surfaces


def _launchd_restart_surfaces(root: Path, sources: Mapping[str, str]) -> list[dict[str, Any]]:
    weatherctl = sources.get("weatherctl", "")
    install_monitor = sources.get("install_monitor_launchdaemon", "")
    install_daemon = sources.get("install_launchdaemon", "")
    install_agent = sources.get("install_launchagent", "")
    surfaces: list[dict[str, Any]] = []
    if "restart)" in weatherctl and "launchctl kickstart" in weatherctl:
        surfaces.append(
            {
                "id": "weatherctl_restart_launchagents",
                "file": "scripts/weatherctl.sh",
                "domain": "gui/${uid}",
                "scope": "ui_market_model_entry_engine_agents",
                "command": "scripts/weatherctl.sh restart",
                "requires_manual_invocation": True,
                "boundary": "not a parameter apply path; operational restart only",
                "status": "manual_restart_surface",
            }
        )
    if "LABEL=\"com.weather.monitor.daemon\"" in install_monitor and "launchctl bootstrap" in install_monitor:
        surfaces.append(
            {
                "id": "monitor_launchagent_install_and_bootstrap",
                "file": "scripts/install_monitor_launchdaemon.sh",
                "domain": "gui/${uid}/com.weather.monitor.daemon",
                "scope": "monitor_hard_source_daemon_only",
                "command": "bash scripts/install_monitor_launchdaemon.sh",
                "requires_sudo": False,
                "boundary": "only accepted user monitor domain for this repo",
                "status": "allowed_monitor_restart_path",
            }
        )
    if "com.weather.monitor.daemon.plist" in install_daemon and "launchctl kickstart" in install_daemon:
        surfaces.append(
            {
                "id": "broad_launchdaemon_install",
                "file": "scripts/install_launchdaemon.sh",
                "domain": "system/*",
                "scope": "broad_service_set_including_monitor",
                "command": "sudo bash scripts/install_launchdaemon.sh",
                "requires_sudo": True,
                "boundary": "broad operational apply; not for calibration parameter changes",
                "status": "broad_restart_surface_review_required",
            }
        )
    if "PM_LAUNCHD_BOOTSTRAP" in install_agent and "launchctl kickstart" in install_agent:
        surfaces.append(
            {
                "id": "broad_launchagent_install",
                "file": "scripts/install_launchagent.sh",
                "domain": "gui/${uid}/*",
                "scope": "broad_agent_set",
                "command": "bash scripts/install_launchagent.sh",
                "requires_sudo": False,
                "boundary": "operator service management only",
                "status": "broad_restart_surface_review_required",
            }
        )
    return surfaces


def _rollback_surfaces() -> list[dict[str, Any]]:
    return [
        {
            "id": "parameter_governance_rollback_plan",
            "source": "parameter_change_governance.py",
            "rollback_scope": "production parameter change request",
            "required_before_state": "approved",
            "measurement_windows_days": [7, 30, 90],
            "terminal_states": ["kept", "rolled_back"],
            "boundary": "rollback plan is part of request; no runtime auto rollback without measured review",
        },
        {
            "id": "daily_review_persistence_restore_backup",
            "source": "scripts/daily_review_persistence_apply.py",
            "rollback_scope": "daily review/calibration/candidate ledger rows",
            "required_asset": "fresh archive.db backup supplied before apply",
            "boundary": "restore DB backup or targeted SQL rollback after operator confirmation",
        },
        {
            "id": "legacy_optimizer_config_restore",
            "source": "ui/server.py apply_edge_bias_gate_suggestion",
            "rollback_scope": "data/auto_trade_config.json",
            "required_asset": "previous config snapshot",
            "boundary": "must be migrated under parameter_change_governance before future use",
        },
        {
            "id": "launchd_restart_rollback",
            "source": "scripts/install*_launch*.sh",
            "rollback_scope": "service domain",
            "required_asset": "previous plist or disable/bootout command",
            "boundary": "operational rollback, not a probability or parameter rollback",
        },
    ]


def _issue_summary(
    *,
    plists: Sequence[Mapping[str, Any]],
    env_rows: Sequence[Mapping[str, Any]],
    parameter_surfaces: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    labels = {str(item.get("label") or "") for item in plists}
    if "com.polymarket.monitor.daemon" in labels:
        blockers.append("forbidden_polymarket_monitor_daemon_label_in_weather_repo")
    for row in env_rows:
        if row.get("key") == "PM_OPTIMIZER_AUTO_APPLY" and row.get("truthy"):
            blockers.append("legacy_optimizer_auto_apply_enabled_in_launchd")
    for surface in parameter_surfaces:
        sid = str(surface.get("id") or "")
        if (
            sid == "ui_optimizer_apply_endpoint"
            and not surface.get("retired_no_write")
            and not surface.get("confirmation_detected_near_endpoint")
        ):
            blockers.append("unconfirmed_ui_optimizer_apply_endpoint_present")
        if sid == "legacy_feedback_optimizer_auto_apply":
            blockers.append("legacy_optimizer_apply_surface_not_bound_to_bayes_parameter_governance")
        if sid == "auto_trade_config_load_time_migration_write":
            warnings.append("auto_trade_config_load_path_can_write_defaults")
    for plist in plists:
        label = str(plist.get("label") or "")
        if "monitor" in label and label != "com.weather.monitor.daemon":
            blockers.append(f"unexpected_monitor_label:{label}")
    return sorted(set(blockers)), sorted(set(warnings))


def build_audit(root: Path | str = ROOT, *, max_env_rows: int = 200) -> dict[str, Any]:
    repo = Path(root).expanduser().resolve()
    plists = _load_launchd_plists(repo)
    env_rows_all = _launchd_env_inventory(plists)
    env_rows = env_rows_all[: max(0, int(max_env_rows))]
    sources = _source_state(repo)
    parameter_surfaces = _parameter_write_surfaces(repo, sources)
    restart_surfaces = _launchd_restart_surfaces(repo, sources)
    rollback_surfaces = _rollback_surfaces()
    blockers, warnings = _issue_summary(
        plists=plists,
        env_rows=env_rows_all,
        parameter_surfaces=parameter_surfaces,
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "root": str(repo),
        "parameter_change_governance_contract": parameter_change_governance_contract(),
        "parameter_write_surfaces": parameter_surfaces,
        "launchd_restart_surfaces": restart_surfaces,
        "config_surface": {
            "schema": "parameter_config_surface_inventory_v1",
            "launchd_plist_count": len(plists),
            "env_key_count": len(env_rows_all),
            "env_rows": env_rows,
            "env_rows_truncated": len(env_rows_all) > len(env_rows),
            "high_risk_keys": [
                row
                for row in env_rows_all
                if row.get("key") in {"PM_OPTIMIZER_AUTO_APPLY", "PM_ARCHIVE_DB_GOVERNANCE_APPLY"}
                or str(row.get("key") or "").endswith("_APPLY")
            ],
            "config_files": [
                {
                    "path": "data/auto_trade_config.json",
                    "writer": "ui.server save_auto_trade_config",
                    "status": "legacy_config_surface_review_required",
                }
            ],
        },
        "launchd_plists": [
            {
                "label": item.get("label"),
                "path": item.get("path"),
                "disabled": item.get("disabled"),
                "run_at_load": item.get("run_at_load"),
                "keep_alive": item.get("keep_alive"),
                "program": item.get("program"),
                "parse_ok": item.get("parse_ok"),
            }
            for item in plists
        ],
        "rollback_surfaces": rollback_surfaces,
        "blockers": blockers,
        "warnings": warnings,
        "boundary": {
            "weak_likelihood_parameter_changes": "must originate from approved calibration candidate before production use",
            "strong_likelihood_parameter_changes": "must preserve hard-source factual constraints and source lineage",
            "daily_review_candidates": "proposed only until reviewed/approved",
            "legacy_optimizer": "diagnostic only until migrated or disabled",
            "launchd_restart": "operator action only; never hidden inside calibration apply",
        },
        "guards": {
            "writes_archive_db": False,
            "writes_config_files": False,
            "writes_parameter_change_events": False,
            "changes_parameters": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "next_boundary": (
            "resolve_parameter_governance_surface_blockers"
            if blockers
            else "wire_parameter_surface_audit_into_release_acceptance"
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--max-env-rows", type=int, default=200)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)

    report = build_audit(args.root, max_env_rows=int(args.max_env_rows))
    if args.summary:
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "parameter_surface_count": len(report["parameter_write_surfaces"]),
            "launchd_restart_surface_count": len(report["launchd_restart_surfaces"]),
            "env_key_count": report["config_surface"]["env_key_count"],
            "blockers": report["blockers"],
            "warnings": report["warnings"],
            "guards": report["guards"],
            "next_boundary": report["next_boundary"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
