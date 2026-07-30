#!/usr/bin/env python3
"""Read-only health audit for the active reported-X production runtime."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_p0_runtime import build_zuuu_physical_public_slug  # noqa: E402
from production_probability_successor_activation_contract import (  # noqa: E402
    PRODUCTION_PATH,
    PUBLIC_SSOT_SCHEMA,
    TARGET_TYPE,
    load_successor_activation,
)
from production_probability_successor_runtime_manifest import (  # noqa: E402
    audit_successor_runtime_manifest_dependency_closure,
    successor_runtime_manifest_sha256,
)
from reported_x_successor_checkpoint_reader import load_latest_reported_x_checkpoint  # noqa: E402
from reported_x_successor_schema_contract import verify_schema  # noqa: E402


SCHEMA_VERSION = "production_reported_x_runtime_readiness_v1"
INSTALLED_MONITOR_PLIST = Path.home() / "Library/LaunchAgents/com.weather.monitor.daemon.plist"
REQUIRED_MONITOR_ENVIRONMENT = {
    "PM_AMSC_ZUUU_POLL_ENABLED": "1",
    "AMSC_ZUUU_PRODUCTION_PROBABILITY_UPDATE_ENABLED": "1",
    "PM_AMSC_ZUUU_SPECI_WATCH_ENABLED": "1",
    "PM_AMSC_ZUUU_SPECI_PRODUCTION_PROBABILITY_INGRESS_ENABLED": "1",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def audit_installed_monitor_environment(path: Path) -> dict[str, Any]:
    environment: dict[str, str] = {}
    load_error = ""
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
        environment = {
            str(key): str(value)
            for key, value in _mapping(_mapping(payload).get("EnvironmentVariables")).items()
        }
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError) as exc:
        load_error = type(exc).__name__
    checks = {
        key: environment.get(key) == expected
        for key, expected in REQUIRED_MONITOR_ENVIRONMENT.items()
    }
    return {
        "ok": not load_error and all(checks.values()),
        "path": str(path),
        "load_error": load_error,
        "checks": checks,
        "read_only": True,
    }


def audit_monitor_runtime_entrypoints(
    *, root: Path, installed_monitor_plist: Path
) -> dict[str, Any]:
    paths = {
        "repository_plist": root / "launchd/com.weather.monitor.daemon.plist",
        "installed_plist": installed_monitor_plist,
        "launchagent_entrypoint": root / "scripts/weather_monitor_launchagent_entrypoint.sh",
        "monitor": root / "scripts/polymarket_monitor_daemon.sh",
        "immutable_wrapper": root / "scripts/run_amsc_zuuu_metar_taf_poll_immutable.sh",
        "poller": root / "scripts/amsc_zuuu_metar_taf_poll_once.sh",
    }
    texts: dict[str, str] = {}
    errors: dict[str, str] = {}
    for key, path in paths.items():
        try:
            texts[key] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            texts[key] = ""
            errors[key] = type(exc).__name__
    try:
        wrapper_executable = bool(paths["immutable_wrapper"].stat().st_mode & 0o111)
    except OSError:
        wrapper_executable = False
    monitor = texts["monitor"]
    wrapper = texts["immutable_wrapper"]
    poller = texts["poller"]
    checks = {
        "installed_plist_matches_repository": bool(texts["repository_plist"])
        and "com.weather.monitor.daemon" in texts["installed_plist"]
        and "scripts/weather_monitor_launchagent_entrypoint.sh" in texts["installed_plist"]
        and "PM_MONITOR_LAUNCH_DOMAIN" in texts["installed_plist"],
        "monitor_calls_immutable_wrapper": '"$ROOT/scripts/run_amsc_zuuu_metar_taf_poll_immutable.sh"'
        in monitor,
        "monitor_does_not_execute_mutable_poller_directly": '"$ROOT/scripts/amsc_zuuu_metar_taf_poll_once.sh" \\'
        not in monitor,
        "immutable_wrapper_executable": wrapper_executable,
        "immutable_wrapper_requires_stable_source_sha": all(
            token in wrapper
            for token in (
                'source_sha_before="$(shasum -a 256 "$SOURCE"',
                '[ "$source_sha_before" = "$snapshot_sha256" ]',
                '[ "$snapshot_sha256" = "$source_sha_after" ]',
            )
        ),
        "immutable_wrapper_syntax_checks_snapshot": '/bin/bash -n "$SNAPSHOT"' in wrapper,
        "immutable_wrapper_executes_snapshot": '/bin/bash "$SNAPSHOT" "$@"' in wrapper,
        "poller_accepts_frozen_project_root": 'ROOT="${AMSC_POLL_PROJECT_ROOT:-' in poller,
    }
    return {
        "ok": not errors and all(checks.values()),
        "checks": checks,
        "errors": errors,
        "paths": {key: str(path) for key, path in paths.items()},
        "sha256": {
            key: hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
            for key, text in texts.items()
        },
        "read_only": True,
    }


def audit_running_monitor_runtime(
    *, launchctl_stdout: str, launchctl_returncode: int, runtime_paths: list[Path]
) -> dict[str, Any]:
    state_match = re.search(r"^\s*state\s*=\s*(\w+)", launchctl_stdout, re.M)
    pid_match = re.search(r"^\s*pid\s*=\s*(\d+)", launchctl_stdout, re.M)
    state = state_match.group(1) if state_match else ""
    pid = int(pid_match.group(1)) if pid_match else None
    started_at = ""
    started_epoch: float | None = None
    process_error = ""
    if pid is not None:
        try:
            process = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart="],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                env={**os.environ, "LC_ALL": "C", "LANG": "C"},
            )
            started_at = _text(process.stdout)
            if process.returncode == 0 and started_at:
                started_epoch = datetime.strptime(
                    started_at, "%a %b %d %H:%M:%S %Y"
                ).timestamp()
            else:
                process_error = "monitor_process_start_unavailable"
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            process_error = f"monitor_process_start_invalid:{type(exc).__name__}"
    else:
        process_error = "monitor_pid_unavailable"
    path_mtimes: dict[str, float | None] = {}
    for path in runtime_paths:
        try:
            path_mtimes[str(path)] = path.stat().st_mtime
        except OSError:
            path_mtimes[str(path)] = None
    valid_mtimes = [value for value in path_mtimes.values() if value is not None]
    newest_runtime_mtime = max(valid_mtimes) if valid_mtimes else None
    loaded_current_runtime = bool(
        started_epoch is not None
        and newest_runtime_mtime is not None
        and started_epoch + 1.0 >= newest_runtime_mtime
    )
    running = launchctl_returncode == 0 and state == "running" and pid is not None
    return {
        "ok": running and not process_error and loaded_current_runtime,
        "running": running,
        "state": state,
        "pid": pid,
        "started_at": started_at,
        "started_epoch": started_epoch,
        "newest_runtime_mtime": newest_runtime_mtime,
        "runtime_path_mtimes": path_mtimes,
        "loaded_current_runtime": loaded_current_runtime,
        "error": process_error,
        "read_only": True,
    }


def audit(*, db: Path, activation_path: Path, event_date: str) -> dict[str, Any]:
    blockers: list[str] = []
    day = _text(event_date)[:10]
    target = f"physical_weather_day_v1:ZUUU:{day}"
    try:
        activation = load_successor_activation(activation_path)
    except (OSError, ValueError) as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": [f"successor_activation_unavailable:{type(exc).__name__}"],
            "read_only": True,
            "places_orders": False,
        }
    current_manifest = successor_runtime_manifest_sha256()
    if _text(activation.get("runtime_manifest_sha256")) != current_manifest:
        blockers.append("successor_runtime_manifest_drift")
    dependency_closure = audit_successor_runtime_manifest_dependency_closure()
    if dependency_closure.get("ok") is not True:
        blockers.append("successor_runtime_manifest_dependency_closure_incomplete")
    evidence: dict[str, Any] = {"runtime_manifest_dependency_closure": dependency_closure}
    with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        schema = verify_schema(conn)
        if schema.get("ok") is not True:
            blockers.append("reported_x_schema_unavailable")
        lineages = conn.execute(
            """SELECT p.replay_generation FROM production_initial_posterior_lineage l
               JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id
               WHERE l.target_identity=? AND l.activation_contract_sha256=?""",
            (target, _text(activation.get("activation_contract_sha256"))),
        ).fetchall()
        if len(lineages) != 1:
            blockers.append("active_successor_p0_lineage_not_unique")
            generation = None
        else:
            generation = int(lineages[0][0] or 0)
        try:
            checkpoint = load_latest_reported_x_checkpoint(
                conn,
                target_identity=target,
                event_date=day,
                replay_generation=generation,
            ) if generation is not None else None
        except ValueError as exc:
            checkpoint = None
            blockers.append(f"active_reported_x_checkpoint_invalid:{exc}")
        if checkpoint is None:
            blockers.append("active_reported_x_checkpoint_missing")
        latest = conn.execute(
            """SELECT h.fact_json,h.composer_version,h.fact_as_of FROM event_fact_latest l
               JOIN event_fact_history h ON h.id=l.fact_id
               WHERE l.slug=? AND l.event_date=?""",
            (build_zuuu_physical_public_slug(day), day),
        ).fetchone()
        if latest is None:
            blockers.append("active_reported_x_public_ssot_missing")
            ssot: Mapping[str, Any] = {}
        else:
            try:
                ssot = _mapping(json.loads(_text(latest["fact_json"])).get("posterior_ssot_fact"))
            except (TypeError, ValueError):
                ssot = {}
                blockers.append("active_reported_x_public_ssot_invalid_json")
        distribution = _mapping(ssot.get("reported_physical_x_distribution"))
        try:
            normalized = abs(sum(float(value) for value in distribution.values()) - 1.0) <= 1e-8
            integer_support = bool(distribution) and all(float(key).is_integer() for key in distribution)
        except (TypeError, ValueError):
            normalized = False
            integer_support = False
        if (
            _text(ssot.get("schema")) != PUBLIC_SSOT_SCHEMA
            or _text(ssot.get("probability_target_type")) != TARGET_TYPE
            or not normalized
            or not integer_support
            or "physical_high_distribution" in ssot
            or "latent_scheduled_slot_high_distribution" in ssot
            or ssot.get("latent_probability_public") is not False
        ):
            blockers.append("active_reported_x_public_ssot_contract_invalid")
        likelihood_identity = _mapping(ssot.get("strong_likelihood_update_identity"))
        if _text(activation.get("strong_update_schema")) == "strong_observation_update_v5_cumulative_replacement_speci_soft":
            if (
                _text(likelihood_identity.get("schema"))
                != "strong_likelihood_cumulative_replacement_identity_v1"
                or _text(likelihood_identity.get("likelihood_update_kind"))
                != "cumulative_evidence_replacement_delta_log_lr_v1"
                or not _text(likelihood_identity.get("current_cumulative_raw_log_score_sha256"))
                or not _text(likelihood_identity.get("conditional_innovation_log_score_sha256"))
            ):
                blockers.append("active_strong_likelihood_replacement_identity_invalid")
            latest_update_id = _text((checkpoint or {}).get("last_strong_update_id"))
            latest_update = conn.execute(
                "SELECT raw_log_score_sha256,path_auxiliary_json FROM strong_observation_updates "
                "WHERE strong_observation_update_id=?",
                (latest_update_id,),
            ).fetchone() if latest_update_id else None
            if latest_update is None:
                blockers.append("active_strong_likelihood_update_missing")
            else:
                persisted_identity = _mapping(
                    json.loads(_text(latest_update["path_auxiliary_json"]) or "{}").get(
                        "strong_likelihood_update_identity"
                    )
                )
                if (
                    dict(persisted_identity) != dict(likelihood_identity)
                    or _text(persisted_identity.get("current_cumulative_raw_log_score_sha256"))
                    != _text(latest_update["raw_log_score_sha256"])
                ):
                    blockers.append("active_strong_likelihood_ssot_ledger_mismatch")
        pending = []
        accepted_regular_metar_max: int | None = None
        if checkpoint is not None:
            consumed = set(checkpoint.get("consumed_event_ids") or [])
            canonical_rows = conn.execute(
                    """SELECT canonical_observation_event_id,report_type,parsed_observation_json
                       FROM canonical_observation_events
                       WHERE target_identity=? AND station_id='ZUUU'
                         AND selected_source_id='zuuu_metar_raw_amsc' AND state='accepted_primary'
                       ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id""",
                    (target,),
                ).fetchall()
            pending = [_text(row[0]) for row in canonical_rows if _text(row[0]) not in consumed]
            regular_temperatures: list[int] = []
            for row in canonical_rows:
                if _text(row[1]).upper() != "METAR":
                    continue
                try:
                    parsed = _mapping(json.loads(_text(row[2]) or "{}"))
                    regular_temperatures.append(int(round(float(parsed["temperature_c"]))))
                except (KeyError, TypeError, ValueError):
                    blockers.append("accepted_regular_metar_temperature_invalid")
            accepted_regular_metar_max = max(regular_temperatures) if regular_temperatures else None
        if pending:
            blockers.append("active_reported_x_canonical_events_pending")
        checkpoint_lower = (checkpoint or {}).get("reported_x_lower_bound_c")
        ssot_lower = ssot.get("reported_x_lower_bound_c")
        if accepted_regular_metar_max is not None:
            if checkpoint_lower != accepted_regular_metar_max:
                blockers.append("active_checkpoint_reported_x_lower_bound_not_regular_metar_max")
            if ssot_lower != accepted_regular_metar_max:
                blockers.append("active_public_ssot_reported_x_lower_bound_not_regular_metar_max")
        evidence.update(
            {
                "replay_generation": generation,
                "posterior_checkpoint_id": _text((checkpoint or {}).get("posterior_checkpoint_id")),
                "reported_x_lower_bound_c": (checkpoint or {}).get("reported_x_lower_bound_c"),
                "public_ssot_reported_x_lower_bound_c": ssot_lower,
                "accepted_regular_metar_reported_max_c": accepted_regular_metar_max,
                "reported_x_lower_bound_matches_accepted_regular_metar_max": bool(
                    accepted_regular_metar_max is not None
                    and checkpoint_lower == accepted_regular_metar_max
                    and ssot_lower == accepted_regular_metar_max
                ),
                "reported_x_distribution": dict(distribution),
                "public_fact_as_of": _text(latest["fact_as_of"] if latest else ""),
                "pending_canonical_event_ids": pending,
                "strong_likelihood_update_identity": dict(likelihood_identity),
            }
        )
        ingress_counts = {
            "accepted_regular_metar_count": 0,
            "accepted_half_hour_regular_metar_count": 0,
            "accepted_speci_count": 0,
            "captured_taf_original_count": 0,
            "captured_taf_amendment_count": 0,
            "captured_taf_correction_count": 0,
            "applied_taf_revision_count": 0,
        }
        canonical_ingress_rows = conn.execute(
            """SELECT report_type,valid_time FROM canonical_observation_events
               WHERE target_identity=? AND station_id='ZUUU'
                 AND selected_source_id='zuuu_metar_raw_amsc'
                 AND state IN ('accepted_primary','accepted_substitute','confirmed')""",
            (target,),
        ).fetchall()
        for ingress_row in canonical_ingress_rows:
            report_type = _text(ingress_row[0]).upper()
            valid_time = _text(ingress_row[1])
            if report_type == "METAR":
                ingress_counts["accepted_regular_metar_count"] += 1
                if valid_time[14:16] == "30":
                    ingress_counts["accepted_half_hour_regular_metar_count"] += 1
            elif report_type == "SPECI":
                ingress_counts["accepted_speci_count"] += 1
        try:
            taf_rows = conn.execute(
                """SELECT observation_json FROM source_observations
                   WHERE station_id='ZUUU' AND event_date=?
                     AND source_id='zuuu_taf_forecast_amsc'""",
                (day,),
            ).fetchall()
        except sqlite3.Error:
            taf_rows = []
        for taf_row in taf_rows:
            try:
                taf_payload = _mapping(_mapping(json.loads(_text(taf_row[0]))).get("payload"))
            except (TypeError, ValueError):
                continue
            revision_kind = _text(taf_payload.get("forecast_revision_kind")).lower()
            key = f"captured_taf_{revision_kind}_count"
            if key in ingress_counts:
                ingress_counts[key] += 1
        try:
            ingress_counts["applied_taf_revision_count"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM weak_forecast_revision_updates WHERE target_identity=?",
                    (target,),
                ).fetchone()[0]
            )
        except sqlite3.Error:
            ingress_counts["applied_taf_revision_count"] = 0
        evidence["ingress_event_evidence"] = {
            **ingress_counts,
            "absence_of_speci_or_post_activation_taf_revision_blocks_live": False,
            "sample_and_oos_counts_block_live": False,
        }
    installed_monitor = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/com.weather.monitor.daemon"],
        capture_output=True,
        text=True,
        check=False,
    )
    monitor_runtime_entrypoints = audit_monitor_runtime_entrypoints(
        root=ROOT,
        installed_monitor_plist=INSTALLED_MONITOR_PLIST,
    )
    monitor_runtime = audit_running_monitor_runtime(
        launchctl_stdout=_text(installed_monitor.stdout),
        launchctl_returncode=installed_monitor.returncode,
        runtime_paths=[
            ROOT / "scripts/polymarket_monitor_daemon.sh",
            ROOT / "scripts/run_amsc_zuuu_metar_taf_poll_immutable.sh",
            ROOT / "scripts/amsc_zuuu_metar_taf_poll_once.sh",
        ],
    )
    if monitor_runtime.get("running") is not True:
        blockers.append("system_weather_monitor_not_running")
    if monitor_runtime_entrypoints.get("ok") is not True:
        blockers.append("system_weather_monitor_immutable_poller_entrypoint_invalid")
    if monitor_runtime.get("running") is True and monitor_runtime.get("loaded_current_runtime") is not True:
        blockers.append("system_weather_monitor_predates_runtime_entrypoint")
    monitor_environment_audit = audit_installed_monitor_environment(INSTALLED_MONITOR_PLIST)
    if monitor_environment_audit.get("load_error"):
        blockers.append("installed_system_monitor_plist_unavailable")
    monitor_environment_checks = dict(_mapping(monitor_environment_audit.get("checks")))
    if not all(monitor_environment_checks.values()):
        blockers.append("installed_system_monitor_reported_x_ingress_disabled_or_stale")
    entrypoints = {
        "metar_fast_lane": "production_reported_x_amsc_archive_probability_ingress" in (ROOT / "scripts/apply_amsc_probability_fast_lane_from_plan.py").read_text(encoding="utf-8"),
        "speci_fast_lane": "production_reported_x_amsc_archive_probability_ingress" in (ROOT / "scripts/watch_zuuu_speci_once.py").read_text(encoding="utf-8"),
        "taf_revision": "production_reported_x_weak_revision_event_runtime" in (ROOT / "scripts/apply_amsc_taf_forecast_revision_from_plan.py").read_text(encoding="utf-8"),
    }
    if not all(entrypoints.values()):
        blockers.append("reported_x_live_entrypoint_not_fully_wired")
    evidence["entrypoints"] = entrypoints
    evidence["monitor_running"] = monitor_runtime.get("running") is True
    evidence["monitor_runtime"] = monitor_runtime
    evidence["monitor_runtime_entrypoints"] = monitor_runtime_entrypoints
    evidence["installed_monitor_plist"] = str(INSTALLED_MONITOR_PLIST)
    evidence["installed_monitor_environment_audit"] = monitor_environment_audit
    evidence["installed_monitor_environment_checks"] = monitor_environment_checks
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "event_date": day,
        "activation_contract_sha256": _text(activation.get("activation_contract_sha256")),
        "runtime_manifest_sha256": current_manifest,
        "evidence": evidence,
        "read_only": True,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data/archive.db"))
    parser.add_argument("--activation", default=str(PRODUCTION_PATH))
    parser.add_argument("--event-date", required=True)
    args = parser.parse_args()
    result = audit(
        db=Path(args.db).expanduser(),
        activation_path=Path(args.activation).expanduser(),
        event_date=args.event_date,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
