"""Prospective Tier-A weak-evidence capture inside the resident service.

This module exists so strict-as-of days continue to accumulate while the new
composer is still inactive.  It is isolated from the hard-NO lane and cannot
write posterior/SSOT, decisions, intents, or orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from amsc_raw_history_archive import archive_amsc_poll, attach_archive_metadata
from archive_db_write_queue import connect_archive_db_rw
from official_nwp_daily_curve_monitor_job_runtime import (
    build_official_nwp_daily_curve_monitor_job,
    load_monitor_job_contract,
)
from official_nwp_daily_curve_plan_runtime import load_daily_curve_run_contract
from official_nwp_request_control import try_acquire_capture_lease
from scripts.fetch_zuuu_metar_taf import (
    PARSER_VERSION as AMSC_PARSER_VERSION,
    build_amsc_observation_bundle,
    fetch_reports_with_transport,
    source_payloads_from_amsc_payload,
    token_candidates,
    utc_now_iso,
)
from scripts.materialize_official_nwp_daily_peak_p1_ingress import (
    materialize_official_nwp_daily_peak_p1,
)
from weather_fact_snapshot_writer import persist_source_observations_for_payload


ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = "prospective_physical_evidence_capture_v1"
CONFIG_SCHEMA = "identifiable_physical_evidence_capture_config_v1"
DEFAULT_CONFIG = ROOT / "config/identifiable_physical_evidence_capture.json"
DEFAULT_DB = ROOT / "data/archive.db"
DEFAULT_TAF_ARCHIVE = ROOT / "data/prospective_physical_evidence/amsc_taf_raw"
DEFAULT_NWP_ROOT = ROOT / "data/runtime/official_nwp_curve_sources_v2"
DEFAULT_NWP_STATUS = DEFAULT_NWP_ROOT / "embedded_capture_status_latest.json"
DEFAULT_NWP_JOB = ROOT / "config/forecast_sources/official_nwp_daily_curve_monitor_job_v1.json"
DEFAULT_NWP_CURVE_CONTRACT = ROOT / "config/forecast_sources/zuuu_official_daily_curve_run_contract_v1.json"
DEFAULT_NWP_CATALOG = ROOT / "config/forecast_sources/official_nwp_zuuu_v1.json"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _capture_channel_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep resident status auditable without copying full NWP payloads."""

    summary = {
        key: value.get(key)
        for key in (
            "ok",
            "state",
            "target_event_date",
            "source_observation_count",
            "provider_success_count",
            "provider_result_count",
            "selected_providers",
            "child_returncode",
        )
        if key in value
    }
    materialization = _mapping(value.get("p1_materialization"))
    if materialization:
        summary["p1_materialization"] = {
            key: materialization.get(key)
            for key in ("ok", "state")
            if key in materialization
        }
    return summary


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_capture_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping) or value.get("schema") != CONFIG_SCHEMA:
        raise ValueError("physical_evidence_capture_config_invalid")
    config = dict(value)
    if config.get("state") != "enabled":
        raise ValueError("physical_evidence_capture_not_enabled")
    boundary = _mapping(config.get("boundary"))
    if (
        boundary.get("hard_no_lane_reads_this_capture") is not False
        or boundary.get("writes_posterior") is not False
        or boundary.get("writes_ssot") is not False
        or boundary.get("writes_decision_or_intent") is not False
        or boundary.get("places_orders") is not False
        or boundary.get("adds_fourth_service") is not False
        or boundary.get("nwp_child_is_ephemeral") is not True
    ):
        raise ValueError("physical_evidence_capture_boundary_invalid")
    return config


def capture_amsc_taf_amd(
    *,
    db_path: Path,
    archive_dir: Path,
    count: int,
    timeout_sec: int,
) -> dict[str, Any]:
    """Capture only TAF/AMD weak evidence with observed transport timing."""

    errors = []
    for token in token_candidates():
        poll_started_at = utc_now_iso()
        started = time.monotonic()
        try:
            payload, transport = fetch_reports_with_transport(
                token, max(1, int(count)), max(1, int(timeout_sec))
            )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{exc}")
            continue
        completed_at = utc_now_iso()
        if payload.get("code") != 200:
            errors.append("amsc_taf_response_not_200")
            continue
        if transport.get("response_headers_observed") is not True or not _text(
            transport.get("response_started_at")
        ):
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked_non_tier_a_transport",
                "block_reasons": ["amsc_taf_response_headers_not_observed"],
                "writes_posterior": False,
                "places_orders": False,
            }
        sources = [
            row
            for row in source_payloads_from_amsc_payload(
                payload,
                fetched_at=completed_at,
                duration_ms=round((time.monotonic() - started) * 1000.0, 3),
            )
            if _text(row.get("source_id")) == "zuuu_taf_forecast_amsc"
        ]
        if not sources:
            return {
                "schema": SCHEMA_VERSION,
                "ok": True,
                "state": "no_taf_amd_in_response",
                "source_observation_count": 0,
                "writes_posterior": False,
                "places_orders": False,
            }
        archive = archive_amsc_poll(
            archive_dir=archive_dir,
            source_payloads=sources,
            poll_started_at=poll_started_at,
            response_completed_at=completed_at,
            response_started_at=_text(transport.get("response_started_at")),
            response_headers_observed=True,
            response_status=payload.get("code"),
            request_metadata={
                "cccc": "ZUUU",
                "count": max(1, int(count)),
                "tt": "FC,FT,",
                "lane": "prospective_physical_weak_evidence",
            },
            fetcher_version=AMSC_PARSER_VERSION,
        )
        attached = attach_archive_metadata(sources, archive)
        bundle = build_amsc_observation_bundle(
            payload,
            fetched_at=completed_at,
            source_payloads=attached,
        )
        observations = [
            row
            for row in bundle.get("source_observations") or []
            if isinstance(row, Mapping)
            and _text(row.get("source_id")) == "zuuu_taf_forecast_amsc"
        ]
        source_payload = {
            "source_observations": observations,
            "source_observation_ids": [
                _text(row.get("source_observation_id")) for row in observations
            ],
            "source_acquisition_trace": dict(
                bundle.get("source_acquisition_trace") or {}
            ),
        }
        with connect_archive_db_rw(
            db_path,
            owner="prospective_physical_taf_evidence",
            queue_wait_sec=2.0,
            timeout_sec=2.0,
            busy_timeout_ms=1000,
        ) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                persistence = persist_source_observations_for_payload(
                    conn, source_payload, created_at=completed_at
                )
                if persistence.get("ok") is not True:
                    raise ValueError("physical_taf_source_observation_persistence_blocked")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "taf_amd_tier_a_materialized",
            "source_observation_count": len(observations),
            "source_observation_persistence": persistence,
            "raw_archive_manifest_sha256": archive.get("manifest_sha256"),
            "hard_no_lane_consumed_taf": False,
            "writes_posterior": False,
            "places_orders": False,
        }
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked_amsc_taf_unavailable",
        "block_reasons": errors[-5:] or ["amsc_session_unavailable"],
        "writes_posterior": False,
        "places_orders": False,
    }


def _target_event_date(
    curve_contract: Mapping[str, Any], *, target_day_offset: int
) -> str:
    try:
        zone = ZoneInfo(_text(curve_contract.get("target_timezone")))
        return (
            datetime.now(timezone.utc).astimezone(zone).date()
            + timedelta(days=int(target_day_offset))
        ).isoformat()
    except Exception as exc:
        raise ValueError("official_nwp_target_event_date_invalid") from exc


def _completed_provider_runs(
    status_path: Path, *, expected_target_event_date: str
) -> dict[str, str]:
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if _text(status.get("target_event_date")) != expected_target_event_date:
        return {}
    return {
        _text(key): _text(value)
        for key, value in _mapping(status.get("completed_provider_runs")).items()
        if _text(key) and _text(value)
    }


def _provider_rotation(
    status_path: Path, *, providers: list[str], cycle_size: int
) -> tuple[list[str], int]:
    available = [_text(provider) for provider in providers if _text(provider)]
    if not available:
        raise ValueError("official_nwp_capture_providers_missing")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        status = {}
    try:
        start = int(_mapping(status).get("next_provider_index") or 0) % len(
            available
        )
    except (TypeError, ValueError):
        start = 0
    count = max(1, min(len(available), int(cycle_size)))
    selected = [available[(start + offset) % len(available)] for offset in range(count)]
    return selected, (start + count) % len(available)


def capture_official_nwp_tier_a(
    *,
    db_path: Path,
    curve_source_root: Path,
    status_path: Path,
    providers: list[str],
    max_provider_runs: int,
    target_day_offset: int,
) -> dict[str, Any]:
    """Capture complete official curves, then append valid Tier-A peaks to P1."""

    capture_lease = try_acquire_capture_lease(curve_source_root)
    if capture_lease is None:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked_official_nwp_capture_already_in_progress",
            "blockers": ["official_nwp_capture_already_in_progress"],
            "ephemeral_child_process": True,
            "writes_posterior": False,
            "places_orders": False,
        }
    try:
        return _capture_official_nwp_tier_a_locked(
            db_path=db_path,
            curve_source_root=curve_source_root,
            status_path=status_path,
            providers=providers,
            max_provider_runs=max_provider_runs,
            target_day_offset=target_day_offset,
        )
    finally:
        capture_lease.close()


def _capture_official_nwp_tier_a_locked(
    *,
    db_path: Path,
    curve_source_root: Path,
    status_path: Path,
    providers: list[str],
    max_provider_runs: int,
    target_day_offset: int,
) -> dict[str, Any]:
    """Capture body; the caller owns the shared cross-process lease."""

    selected_providers, next_provider_index = _provider_rotation(
        status_path,
        providers=providers,
        cycle_size=max_provider_runs,
    )
    job = load_monitor_job_contract(DEFAULT_NWP_JOB)
    job["providers"] = selected_providers
    job["target_day_offset"] = int(target_day_offset)
    job["target_day_offsets"] = [int(target_day_offset)]
    budget = _mapping(job.get("execution_budget"))
    budget["max_provider_runs_per_job"] = max(1, int(max_provider_runs))
    job["execution_budget"] = budget
    curve_contract = load_daily_curve_run_contract(DEFAULT_NWP_CURVE_CONTRACT)
    expected_target_event_date = _target_event_date(
        curve_contract, target_day_offset=int(target_day_offset)
    )
    capture = build_official_nwp_daily_curve_monitor_job(
        job_contract=job,
        curve_contract=curve_contract,
        execute=True,
        curve_source_root=curve_source_root,
        catalog_path=DEFAULT_NWP_CATALOG,
        completed_provider_runs=_completed_provider_runs(
            status_path,
            expected_target_event_date=expected_target_event_date,
        ),
    )
    capture["provider_rotation"] = {
        "provider_sequence": list(providers),
        "selected_providers": selected_providers,
        "advance_on_failure": True,
    }
    capture["next_provider_index"] = next_provider_index
    _atomic_json(status_path, capture)
    materialization = materialize_official_nwp_daily_peak_p1(
        db=db_path,
        curve_source_root=curve_source_root,
        station_id="ZUUU",
        apply=True,
    )
    provider_successes = sum(
        row.get("ok") is True for row in capture.get("provider_results") or []
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": provider_successes > 0 and materialization.get("ok") is True,
        "state": "official_nwp_tier_a_captured"
        if provider_successes > 0 and materialization.get("ok") is True
        else "official_nwp_partial_or_blocked",
        "target_event_date": capture.get("target_event_date"),
        "provider_success_count": provider_successes,
        "provider_result_count": len(capture.get("provider_results") or []),
        "selected_providers": selected_providers,
        "next_provider_index": next_provider_index,
        "capture": capture,
        "p1_materialization": materialization,
        "writes_posterior": False,
        "places_orders": False,
    }


def _nwp_subprocess_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    capture = _mapping(result.get("capture"))
    materialization = _mapping(result.get("p1_materialization"))
    return {
        "schema": SCHEMA_VERSION,
        "ok": result.get("ok") is True,
        "state": _text(result.get("state")),
        "target_event_date": _text(result.get("target_event_date"))[:10],
        "provider_success_count": int(result.get("provider_success_count") or 0),
        "provider_result_count": int(result.get("provider_result_count") or 0),
        "selected_providers": list(result.get("selected_providers") or []),
        "next_provider_index": result.get("next_provider_index"),
        "provider_results": [
            {
                key: row.get(key)
                for key in (
                    "provider_id",
                    "ok",
                    "state",
                    "run_at_utc",
                    "fallback_run_used",
                    "blockers",
                )
            }
            for row in capture.get("provider_results") or []
            if isinstance(row, Mapping)
        ],
        "p1_materialization": {
            "ok": materialization.get("ok") is True,
            "state": _text(materialization.get("state")),
            "outcomes": [
                {
                    key: row.get(key)
                    for key in (
                        "provider_id",
                        "event_date",
                        "state",
                        "block_reasons",
                    )
                }
                for row in materialization.get("outcomes") or []
                if isinstance(row, Mapping)
            ],
        },
        "ephemeral_child_process": True,
        "adds_fourth_service": False,
        "writes_posterior": False,
        "places_orders": False,
    }


def capture_official_nwp_tier_a_subprocess(
    *,
    db_path: Path,
    curve_source_root: Path,
    status_path: Path,
    providers: list[str],
    max_provider_runs: int,
    target_day_offset: int,
    runtime_python: Path,
    cycle_timeout_sec: int,
) -> dict[str, Any]:
    """Run dependency-heavy official collectors in their approved venv."""

    # A venv launcher is commonly a symlink to the base interpreter.  Calling
    # resolve() here erases the venv identity and silently drops site-packages.
    python = Path(os.path.abspath(Path(runtime_python).expanduser()))
    if not python.is_file() or not os.access(python, os.X_OK):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked_official_nwp_runtime_missing",
            "runtime_python": str(python),
            "adds_fourth_service": False,
            "writes_posterior": False,
            "places_orders": False,
        }
    selected_on_failure, next_on_failure = _provider_rotation(
        status_path,
        providers=providers,
        cycle_size=max_provider_runs,
    )

    def advance_after_child_failure(state: str) -> None:
        try:
            current = json.loads(Path(status_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            current = {}
        current_map = _mapping(current)
        child_rotation = _mapping(current_map.get("provider_rotation"))
        if (
            current_map.get("next_provider_index") == next_on_failure
            and list(child_rotation.get("selected_providers") or [])
            == selected_on_failure
        ):
            return
        _atomic_json(
            Path(status_path),
            {
                **current_map,
                "ok": False,
                "state": state,
                "updated_at": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
                "provider_rotation": {
                    "provider_sequence": list(providers),
                    "selected_providers": selected_on_failure,
                    "advance_on_failure": True,
                    "advanced_by_parent_after_child_failure": True,
                },
                "next_provider_index": next_on_failure,
                "boundary": {
                    "writes_posterior": False,
                    "places_orders": False,
                },
            },
        )
    command = [
        str(python),
        str(Path(__file__).resolve()),
        "--official-nwp-once",
        "--db",
        str(Path(db_path).expanduser().resolve()),
        "--curve-source-root",
        str(Path(curve_source_root).expanduser().resolve()),
        "--nwp-status",
        str(Path(status_path).expanduser().resolve()),
        "--providers",
        ",".join(providers),
        "--max-provider-runs",
        str(max(1, int(max_provider_runs))),
        "--target-day-offset",
        str(int(target_day_offset)),
    ]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(
            timeout=max(30, int(cycle_timeout_sec))
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        advance_after_child_failure("blocked_official_nwp_cycle_timeout")
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked_official_nwp_cycle_timeout",
            "cycle_timeout_sec": max(30, int(cycle_timeout_sec)),
            "ephemeral_child_process": True,
            "child_process_group_terminated": True,
            "adds_fourth_service": False,
            "writes_posterior": False,
            "places_orders": False,
        }
    try:
        payload = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, Mapping) or not payload:
        advance_after_child_failure("blocked_official_nwp_child_failed")
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked_official_nwp_child_failed",
            "returncode": process.returncode,
            "stderr_tail": _text(stderr)[-500:],
            "ephemeral_child_process": True,
            "adds_fourth_service": False,
            "writes_posterior": False,
            "places_orders": False,
        }
    if process.returncode != 0:
        advance_after_child_failure(_text(payload.get("state")) or "official_nwp_child_failed")
    result = dict(payload)
    result.update(
        {
            "ephemeral_child_process": True,
            "adds_fourth_service": False,
            "writes_posterior": False,
            "places_orders": False,
            "child_returncode": process.returncode,
        }
    )
    return result


class ProspectivePhysicalEvidenceCaptureWorker:
    """Cadence controller; all network and write work is off the hot path."""

    def __init__(
        self,
        db_path: Path = DEFAULT_DB,
        *,
        config_path: Path = DEFAULT_CONFIG,
        taf_archive_dir: Path = DEFAULT_TAF_ARCHIVE,
        nwp_root: Path = DEFAULT_NWP_ROOT,
        nwp_status_path: Path = DEFAULT_NWP_STATUS,
        taf_capture: Callable[..., dict[str, Any]] = capture_amsc_taf_amd,
        nwp_capture: Callable[..., dict[str, Any]] = (
            capture_official_nwp_tier_a_subprocess
        ),
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.config_path = Path(config_path).expanduser().resolve()
        self.taf_archive_dir = Path(taf_archive_dir).expanduser().resolve()
        self.nwp_root = Path(nwp_root).expanduser().resolve()
        self.nwp_status_path = Path(nwp_status_path).expanduser().resolve()
        self.taf_capture = taf_capture
        self.nwp_capture = nwp_capture
        self.next_taf = 0.0
        self.next_nwp = 0.0
        self._nwp_cycle_index = 0
        self.last_capture_at = ""
        self.last_capture_summary: dict[str, Any] = {}

    def run(self) -> dict[str, Any]:
        try:
            config = load_capture_config(self.config_path)
            now = time.monotonic()
            outcomes: dict[str, Any] = {}
            taf = _mapping(config.get("taf_amd"))
            if taf.get("enabled") is True and now >= self.next_taf:
                result = self.taf_capture(
                    db_path=self.db_path,
                    archive_dir=self.taf_archive_dir,
                    count=int(taf.get("count") or 12),
                    timeout_sec=int(taf.get("timeout_sec") or 15),
                )
                outcomes["taf_amd"] = result
                interval = (
                    float(taf.get("success_interval_sec") or 1800)
                    if result.get("ok") is True
                    else float(taf.get("failure_retry_sec") or 300)
                )
                self.next_taf = time.monotonic() + max(30.0, interval)
            nwp = _mapping(config.get("official_nwp"))
            if nwp.get("enabled") is True and now >= self.next_nwp:
                raw_offsets = nwp.get("target_day_offsets")
                offsets: list[int] = []
                if isinstance(raw_offsets, (list, tuple)):
                    for value in raw_offsets:
                        try:
                            offset = int(value)
                        except (TypeError, ValueError):
                            continue
                        if 0 <= offset <= 3 and offset not in offsets:
                            offsets.append(offset)
                if not offsets:
                    offsets = [int(nwp.get("target_day_offset") or 1)]
                target_day_offset = offsets[self._nwp_cycle_index % len(offsets)]
                self._nwp_cycle_index += 1
                result = self.nwp_capture(
                    db_path=self.db_path,
                    curve_source_root=self.nwp_root,
                    status_path=self.nwp_status_path,
                    providers=[_text(item) for item in nwp.get("providers") or []],
                    max_provider_runs=int(
                        nwp.get("max_provider_runs_per_cycle") or 3
                    ),
                    target_day_offset=target_day_offset,
                    runtime_python=Path(
                        _text(nwp.get("runtime_python"))
                        or "/Users/xiong/.local/weather-official-nwp-venv/bin/python"
                    ),
                    cycle_timeout_sec=int(nwp.get("cycle_timeout_sec") or 600),
                )
                outcomes["official_nwp"] = result
                interval = (
                    float(nwp.get("success_interval_sec") or 21600)
                    if result.get("ok") is True
                    else float(nwp.get("failure_retry_sec") or 900)
                )
                self.next_nwp = time.monotonic() + max(60.0, interval)
            if outcomes:
                self.last_capture_at = utc_now_iso()
                self.last_capture_summary = {
                    channel: _capture_channel_summary(result)
                    for channel, result in outcomes.items()
                    if isinstance(result, Mapping)
                }
            return {
                "schema": SCHEMA_VERSION,
                "ok": all(row.get("ok") is True for row in outcomes.values()),
                "state": "captured" if outcomes else "not_due",
                "outcomes": outcomes,
                "last_capture_at": self.last_capture_at,
                "last_capture": dict(self.last_capture_summary),
                "activation_independent": True,
                "hard_no_lane_consumes_capture": False,
                "writes_posterior": False,
                "writes_ssot": False,
                "places_orders": False,
            }
        except Exception as exc:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
                "last_capture_at": self.last_capture_at,
                "last_capture": dict(self.last_capture_summary),
                "writes_posterior": False,
                "writes_ssot": False,
                "places_orders": False,
            }


__all__ = [
    "ProspectivePhysicalEvidenceCaptureWorker",
    "SCHEMA_VERSION",
    "capture_amsc_taf_amd",
    "capture_official_nwp_tier_a",
    "capture_official_nwp_tier_a_subprocess",
    "load_capture_config",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-nwp-once", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--curve-source-root", type=Path, default=DEFAULT_NWP_ROOT)
    parser.add_argument("--nwp-status", type=Path, default=DEFAULT_NWP_STATUS)
    parser.add_argument("--providers", default="ecmwf,icon,gfs")
    parser.add_argument("--max-provider-runs", type=int, default=1)
    parser.add_argument("--target-day-offset", type=int, default=1)
    args = parser.parse_args(argv)
    if not args.official_nwp_once:
        parser.error("--official-nwp-once is required")
    result = capture_official_nwp_tier_a(
        db_path=args.db,
        curve_source_root=args.curve_source_root,
        status_path=args.nwp_status,
        providers=[value.strip() for value in args.providers.split(",") if value.strip()],
        max_provider_runs=max(1, int(args.max_provider_runs)),
        target_day_offset=int(args.target_day_offset),
    )
    print(json.dumps(_nwp_subprocess_summary(result), ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
