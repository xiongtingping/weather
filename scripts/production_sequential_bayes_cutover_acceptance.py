#!/usr/bin/env python3
"""Read-only acceptance for the sequential-Bayes production cutover."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_activation_contract import (  # noqa: E402
    ProductionProbabilityActivationError,
    load_production_probability_activation,
)
from scripts.production_probability_transaction_b_cutover_readiness import build_readiness  # noqa: E402
from peak_timing_state_runtime import validate_peak_timing_state_contract  # noqa: E402


SCHEMA_VERSION = "production_sequential_bayes_cutover_acceptance_v1"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _launchd_service_state(domain_label: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["launchctl", "print", domain_label],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "domain_label": domain_label,
            "available": False,
            "running": False,
            "pid": None,
            "na_reason": f"launchctl_inspection_failed:{exc}",
        }
    text = str(proc.stdout or "")
    state_match = re.search(r"^\s*state\s*=\s*(\w+)", text, re.M)
    pid_match = re.search(r"^\s*pid\s*=\s*(\d+)", text, re.M)
    state = str(state_match.group(1) if state_match else "")
    return {
        "domain_label": domain_label,
        "available": proc.returncode == 0,
        "running": proc.returncode == 0 and state == "running",
        "state": state,
        "pid": int(pid_match.group(1)) if pid_match else None,
        "na_reason": "" if proc.returncode == 0 else "launchd_service_not_loaded",
    }


def build_service_recovery_acceptance(*, root: Path | str = ROOT) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    monitor = _launchd_service_state("system/com.weather.monitor.daemon")
    target_worker = _launchd_service_state(
        f"gui/{os.getuid()}/com.weather.target_realization_worker"
    )
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        worker_rows = [
            line.strip()
            for line in str(proc.stdout or "").splitlines()
            if f"{base}/scan_weather_market.py" in line
            and "--auto-trade-job-worker" in line
        ] if proc.returncode == 0 else []
        worker_inspection_na_reason = "" if proc.returncode == 0 else "process_list_unavailable"
    except (OSError, subprocess.SubprocessError) as exc:
        worker_rows = []
        worker_inspection_na_reason = f"process_list_failed:{exc}"
    blockers: list[str] = []
    if monitor.get("running") is not True:
        blockers.append("system_weather_monitor_daemon_not_running")
    if target_worker.get("running") is not True:
        blockers.append("target_realization_worker_service_not_running")
    if worker_inspection_na_reason:
        blockers.append("target_realization_worker_process_inspection_unavailable")
    elif len(worker_rows) != 1:
        blockers.append("target_realization_worker_process_count_not_one")
    return {
        "schema": "production_probability_service_recovery_acceptance_v1",
        "ok": not blockers,
        "blockers": blockers,
        "system_weather_monitor_daemon": monitor,
        "target_realization_worker_service": target_worker,
        "target_realization_worker_process_count": len(worker_rows),
        "target_realization_worker_processes": worker_rows,
        "target_realization_worker_processes_na_reason": worker_inspection_na_reason,
        "read_only": True,
    }


def _physical_distribution(value: Any) -> tuple[list[tuple[float, float]], list[str]]:
    if isinstance(value, Mapping):
        iterator: Sequence[Any] = [
            {"temperature_c": key, "probability": probability}
            for key, probability in value.items()
        ]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        iterator = value
    else:
        return [], ["physical_high_distribution_missing"]
    rows: list[tuple[float, float]] = []
    for raw in iterator:
        if not isinstance(raw, Mapping):
            return [], ["physical_high_distribution_row_invalid"]
        try:
            temperature = float(raw.get("temperature_c", raw.get("value_c", raw.get("x"))))
            probability = float(raw.get("probability", raw.get("mass", raw.get("posterior"))))
        except (TypeError, ValueError):
            return [], ["physical_high_distribution_row_invalid"]
        if probability < 0.0:
            return [], ["physical_high_distribution_probability_negative"]
        rows.append((temperature, probability))
    if not rows or abs(sum(probability for _, probability in rows) - 1.0) > 1e-8:
        return [], ["physical_high_distribution_not_normalized"]
    return rows, []


def build_acceptance(db_path: Path | str, *, event_date: str) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    readiness = build_readiness(db)
    blockers: list[str] = []
    try:
        activation = load_production_probability_activation()
    except ProductionProbabilityActivationError as exc:
        activation = {}
        blockers.append(f"activation_invalid:{exc}")
    activation_effective_at = _time(activation.get("activation_effective_at"))
    if activation and activation_effective_at is None:
        blockers.append("activation_effective_at_invalid")
    if readiness.get("production_cutover", {}).get("ok") is not True:
        blockers.extend(str(value) for value in readiness.get("production_cutover", {}).get("blockers", []))
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT h.id,h.slug,h.fact_as_of,h.composer_version,h.fact_json,h.posterior_run_id
               FROM event_fact_latest l JOIN event_fact_history h ON h.id=l.fact_id
               WHERE h.event_date=? AND lower(h.slug) LIKE 'highest-temperature-in-chengdu-on-%'
               ORDER BY h.fact_as_of DESC LIMIT 1""",
            (event_date,),
        ).fetchone()
        if row is None:
            blockers.append("current_event_latest_fact_missing")
            fact = {}
            ssot = {}
        else:
            fact = dict(row)
            ssot = _mapping(_parse_json(row["fact_json"]).get("posterior_ssot_fact"))
        if str(ssot.get("schema") or "") != "sequential_temperature_posterior_ssot_v2":
            blockers.append("latest_fact_not_sequential_temperature_posterior_ssot_v2")
        if str(ssot.get("writer_mode") or "") != "production":
            blockers.append("latest_fact_not_production_writer")
        physical_rows, physical_issues = _physical_distribution(ssot.get("physical_high_distribution"))
        blockers.extend(physical_issues)
        checkpoint_id = str(ssot.get("posterior_checkpoint_id") or "")
        checkpoint = conn.execute(
            "SELECT target_identity FROM posterior_checkpoints WHERE posterior_checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone() if checkpoint_id else None
        target_identity = str(checkpoint["target_identity"] or "") if checkpoint else ""
        lineage = conn.execute(
            "SELECT posterior_checkpoint_id,activation_contract_sha256 FROM production_initial_posterior_lineage WHERE target_identity=? ORDER BY created_at DESC LIMIT 1",
            (target_identity,),
        ).fetchone() if target_identity else None
        if lineage is None:
            blockers.append("p0_initial_lineage_missing")
        elif str(lineage["activation_contract_sha256"] or "") != str(activation.get("activation_contract_sha256") or ""):
            blockers.append("p0_lineage_not_bound_to_current_activation")
        last_event_id = str(ssot.get("last_canonical_event_id") or "")
        last_update_id = str(ssot.get("last_strong_update_id") or "")
        canonical = conn.execute(
            "SELECT first_actionable_seen_at,report_type FROM canonical_observation_events WHERE canonical_observation_event_id=?",
            (last_event_id,),
        ).fetchone() if last_event_id else None
        strong = conn.execute(
            """SELECT strong_observation_update_id,completed_at
               FROM strong_observation_updates
               WHERE strong_observation_update_id=? AND canonical_observation_event_id=?""",
            (last_update_id, last_event_id),
        ).fetchone() if last_event_id and last_update_id else None
    peak_timing_evidence: dict[str, Any] = {
        "state": "na",
        "entry_peak_at": "",
        "entry_peak_at_na_reason": "initial_checkpoint_has_no_canonical_event",
        "does_not_update_physical_x": True,
    }
    if last_event_id:
        if canonical is None:
            blockers.append("latest_ssot_canonical_event_missing")
        elif str(canonical["report_type"] or "") not in {"METAR", "SPECI"}:
            blockers.append("latest_ssot_canonical_report_type_not_metar_or_speci")
        if not last_update_id:
            blockers.append("latest_ssot_last_strong_update_id_missing")
        if strong is None:
            blockers.append("latest_ssot_bound_strong_update_missing")
        peak_timing_evidence, peak_timing_issues = validate_peak_timing_state_contract(
            ssot.get("peak_timing_state"),
            expected_event_id=last_event_id,
            expected_update_id=last_update_id,
        )
        blockers.extend(peak_timing_issues)
    t1 = _time(canonical["first_actionable_seen_at"]) if canonical else None
    t3 = _time(ssot.get("committed_at") or fact.get("fact_as_of")) if ssot else None
    fact_is_post_activation = bool(t3 and activation_effective_at and t3 >= activation_effective_at)
    if activation_effective_at and not fact_is_post_activation:
        blockers.append("latest_sequential_fact_precedes_current_activation")
    event_is_post_activation = bool(t1 and activation_effective_at and t1 >= activation_effective_at)
    latency_ms = (t3 - t1).total_seconds() * 1000.0 if event_is_post_activation and t3 else None
    if event_is_post_activation and strong is None:
        blockers.append("post_activation_canonical_strong_update_missing")
    forward_slo_state = (
        "passed"
        if latency_ms is not None and 0.0 <= latency_ms <= 3000.0
        else "failed"
        if latency_ms is not None
        else "pending_next_canonical_event"
    )
    if forward_slo_state == "failed":
        blockers.append("forward_three_second_slo_failed")
    forward_slo = {
        "state": forward_slo_state,
        "last_canonical_event_id": last_event_id,
        "strong_observation_update_id": str(strong["strong_observation_update_id"] or "") if strong else "",
        "t1_first_actionable_seen_at": canonical["first_actionable_seen_at"] if canonical else "",
        "t3_ssot_committed_at": str(ssot.get("committed_at") or "") if ssot else "",
        "t1_to_t3_ms": round(latency_ms, 3) if latency_ms is not None else None,
        "t1_to_t3_ms_na_reason": (
            "" if latency_ms is not None else "awaiting_first_post_activation_canonical_event"
        ),
        "threshold_ms": 3000,
        "activation_effective_at": activation.get("activation_effective_at"),
        "event_is_post_activation": event_is_post_activation,
    }
    core_ok = not blockers
    return {
        "schema": SCHEMA_VERSION,
        "ok": core_ok,
        "state": (
            "cutover_and_forward_slo_accepted"
            if core_ok and forward_slo["state"] == "passed"
            else "cutover_accepted_forward_slo_pending"
            if core_ok and forward_slo["state"] == "pending_next_canonical_event"
            else "cutover_accepted_forward_slo_failed"
            if core_ok
            else "blocked"
        ),
        "event_date": event_date,
        "blockers": sorted(set(blockers)),
        "latest_fact": {
            "id": fact.get("id"),
            "slug": fact.get("slug"),
            "fact_as_of": fact.get("fact_as_of"),
            "composer_version": fact.get("composer_version"),
            "posterior_schema": ssot.get("schema"),
            "posterior_state": ssot.get("state"),
            "posterior_checkpoint_id": checkpoint_id,
            "physical_distribution_count": len(physical_rows),
            "physical_distribution_probability_sum": sum(probability for _, probability in physical_rows),
            "market_projection_na_reason": ssot.get("market_bucket_probabilities_na_reason"),
            "last_canonical_event_id": last_event_id,
            "last_strong_update_id": last_update_id,
            "canonical_report_type": str(canonical["report_type"] or "") if canonical else "",
            "peak_timing_state": peak_timing_evidence,
        },
        "activation_contract_sha256": activation.get("activation_contract_sha256"),
        "activation_effective_at": activation.get("activation_effective_at"),
        "forward_three_second_slo": forward_slo,
        "samples_oos_bridge_block_live": False,
        "places_orders": False,
        "authorizes_clob": False,
        "read_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data/archive.db"))
    parser.add_argument("--event-date", required=True)
    parser.add_argument(
        "--skip-service-recovery",
        action="store_true",
        help="do not inspect launchd/process recovery (test or offline DB use only)",
    )
    args = parser.parse_args()
    result = build_acceptance(args.db, event_date=str(args.event_date)[:10])
    if not args.skip_service_recovery:
        service_recovery = build_service_recovery_acceptance()
        result["service_recovery"] = service_recovery
        if service_recovery.get("ok") is not True:
            result["blockers"] = sorted(set(
                list(result.get("blockers") or [])
                + list(service_recovery.get("blockers") or [])
            ))
            result["ok"] = False
            result["state"] = "blocked"
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
