#!/usr/bin/env python3
"""Materialize posterior SSOT after a new AMSC source-only observation.

This is a production materialization lane, not a historical repair tool. It
only consumes the current latest fact for one slug/event plus newer AMSC source
history already appended by the AMSC poller, then writes a fresh posterior fact
through weather_fact_snapshot_writer. It never fetches external sources, places
orders, writes auto_trade_decisions, or performs historical backfill.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from posterior_ssot_projection_runtime import build_fact_projection_no_apply  # noqa: E402
from weather_fact_snapshot_writer import (  # noqa: E402
    apply_fact_snapshot_write_plan,
    compact_fact_snapshot_writer_apply_result_for_lineage,
    sha256_json,
)
from archive_db_write_queue import connect_archive_db_rw  # noqa: E402
from posterior_materialization_attestation_runtime import (  # noqa: E402
    append_posterior_materialization_commit_attestation,
)
from official_nwp_prewarm_readiness import audit_official_nwp_prewarm_readiness  # noqa: E402
from official_nwp_multimodel_prewarm import GENERATION_SCHEMA, validate_generation_reference  # noqa: E402
from weak_likelihood_source_set_runtime import build_weak_likelihood_source_set  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_V31_LIVE_MULTIMODEL_CACHE = ROOT / "data" / "runtime" / "official_nwp_multimodel_zuuu_v1" / "prewarm" / "current.json"
DEFAULT_V31_PREWARM_MULTIMODEL_CACHE = DEFAULT_V31_LIVE_MULTIMODEL_CACHE
OFFICIAL_NWP_CATALOG_PATH = ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json"
DEFAULT_V31_PREWARM_MAX_CACHE_AGE_SEC = 1200
V31_MULTIMODEL_CONTEXT_POLICY_SCHEMA = "official_nwp_context_policy_v1"
V31_PREWARMED_CACHE_ONLY = "prewarmed_cache_only_v1"
RESULT_SCHEMA = "amsc_posterior_materialization_task_result_v1"
PAYLOAD_SCHEMA = "amsc_posterior_materialization_task_payload_v1"
PEAK_WINDOW_LEAD_HOURS = 1.0
PEAK_WINDOW_POST_HOURS = 2.0


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_json(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or ""))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _independent_weak_source_availability(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Derive only TAF/WU/TWC presence already visible as-of the source fact."""

    candidates: list[Mapping[str, Any]] = []
    for field in ("source_observations",):
        value = payload.get(field)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            candidates.extend(item for item in value if isinstance(item, Mapping))
    trace = _as_mapping(payload.get("source_acquisition_trace"))
    trace_rows = trace.get("source_observations")
    if isinstance(trace_rows, Sequence) and not isinstance(trace_rows, (str, bytes, bytearray)):
        candidates.extend(item for item in trace_rows if isinstance(item, Mapping))
    available: set[str] = set()
    for row in candidates:
        marker = " ".join(
            str(row.get(key) or "")
            for key in ("source_id", "source", "role", "provider_family", "forecast_group")
        ).lower()
        if "taf" in marker:
            available.add("taf")
        if "wunderground" in marker or marker.startswith("wu") or " wu_" in marker:
            available.add("wu")
        if "weather_com" in marker or "weather.com" in marker or "twc" in marker:
            available.add("twc")
    return {"available_source_ids": sorted(available), "missing_source_ids": [source for source in ("taf", "wu", "twc") if source not in available]}


def _attach_v31_live_multimodel_snapshot(
    payload: Mapping[str, Any],
    *,
    cache_path: Path = DEFAULT_V31_LIVE_MULTIMODEL_CACHE,
    as_of: Any = None,
    require_prewarmed_cache: bool = True,
    require_valid_cache: bool = True,
    cache_generation_ref: Mapping[str, Any] | None = None,
    max_cache_age_sec: int = DEFAULT_V31_PREWARM_MAX_CACHE_AGE_SEC,
) -> tuple[dict[str, Any], dict[str, Any]]:
    out = copy.deepcopy(dict(payload))
    generation_validation: dict[str, Any] = {}
    if cache_generation_ref is not None:
        generation_validation = validate_generation_reference(cache_generation_ref, catalog_path=OFFICIAL_NWP_CATALOG_PATH)
        snapshot = _safe_json(generation_validation.get("bundle")) if generation_validation.get("ok") is True else {}
        if generation_validation.get("ok") is True:
            cache_path = Path(str(generation_validation.get("generation_path") or cache_path)).resolve()
    else:
        try:
            snapshot = _safe_json(cache_path.read_text(encoding="utf-8"))
        except Exception:
            snapshot = {}
    features = copy.deepcopy(dict(_as_mapping(out.get("weather_likelihood_features"))))
    snapshot_completed = _parse_utc(snapshot.get("response_completed_at"))
    projection_as_of = _parse_utc(as_of)
    snapshot_time_aligned = not (
        snapshot_completed is not None
        and projection_as_of is not None
        and (snapshot_completed - projection_as_of).total_seconds() > 60.0
    )
    if cache_generation_ref is not None and generation_validation.get("ok") is not True:
        readiness = {
            "ok": False,
            "state": "blocked",
            "blockers": list(generation_validation.get("issues") or ["v31_cache_generation_validation_failed"]),
            "cache_path": str(cache_path),
        }
    else:
        readiness = audit_official_nwp_prewarm_readiness(
            cache_path=cache_path,
            catalog_path=OFFICIAL_NWP_CATALOG_PATH,
            as_of=as_of,
            max_cache_age_sec=max(1, int(max_cache_age_sec)),
        )
    # The live posterior only consumes a task-pinned official generation. Its
    # frozen available/missing mask may be partial; the one weak model selects
    # an exact approved combination surface. No legacy mutable cache,
    # source-inline refresh, proxy, or unapproved combination can satisfy this
    # boundary.
    snapshot_eligible = readiness.get("ok") is True and snapshot_time_aligned
    state = "available" if snapshot_eligible else (
        "future_fetch_time_rejected" if not snapshot_time_aligned else "required_official_generation_unavailable"
    )
    if snapshot_eligible:
        source_set = build_weak_likelihood_source_set(
            official_nwp_generation=snapshot,
            independent_source_availability=_independent_weak_source_availability(out),
        )
        features["official_nwp_generation"] = snapshot
        features["weak_likelihood_source_set"] = source_set
        out["official_nwp_generation"] = snapshot
        out["weak_likelihood_source_set"] = source_set
        out["official_nwp_generation_state"] = state
        out["official_nwp_generation_path"] = str(cache_path)
    else:
        out["official_nwp_generation_state"] = state
        out["official_nwp_generation_path"] = str(cache_path)
    out["official_nwp_prewarm_readiness"] = readiness
    if cache_generation_ref is not None:
        out["official_nwp_generation_validation"] = generation_validation
    if features:
        out["weather_likelihood_features"] = features
    return out, readiness


def _env_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_positive_int(name: str, default: int) -> int:
    try:
        parsed = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _resolve_v31_multimodel_context_policy(
    payload: Mapping[str, Any],
    *,
    explicit_require_prewarmed_cache: bool | None,
) -> tuple[dict[str, Any], str]:
    """Resolve the immutable queued context policy before any posterior write.

    Old queued tasks have no policy field and retain the legacy environment
    behavior.  New AMSC tasks carry an explicit policy so a cutover cannot be
    split between the source poller and a separately launched queue worker.
    """

    raw = _as_mapping(payload.get("official_nwp_context_policy"))
    if not raw:
        # A posterior task has no right to borrow the then-current forecast
        # generation.  Old queued tasks remain source facts only.
        return {}, "official_nwp_context_policy_missing"
    if raw.get("schema") != V31_MULTIMODEL_CONTEXT_POLICY_SCHEMA:
        return {}, "official_nwp_context_policy_schema_invalid"
    mode = str(raw.get("mode") or "").strip()
    if mode != V31_PREWARMED_CACHE_ONLY:
        return {}, "official_nwp_context_policy_mode_invalid"
    if explicit_require_prewarmed_cache is False:
        return {}, "official_nwp_context_policy_override_conflict"
    generation_ref = _as_mapping(raw.get("cache_generation_ref"))
    required_generation_fields = ("generation_id", "bundle_sha256", "generation_path", "catalog_id", "response_completed_at")
    if generation_ref.get("schema") != GENERATION_SCHEMA or any(
        not str(generation_ref.get(key) or "").strip() for key in required_generation_fields
    ):
        return {}, "official_nwp_context_policy_generation_ref_missing_or_invalid"
    if str(generation_ref.get("catalog_id") or "").strip() != "official_nwp_zuuu_v1":
        return {}, "official_nwp_context_policy_catalog_id_mismatch"
    return {
        "schema": V31_MULTIMODEL_CONTEXT_POLICY_SCHEMA,
        "mode": mode,
        "require_prewarmed_cache": True,
        "require_valid_cache": True,
        "cache_generation_ref": generation_ref,
        "policy_source": "queued_official_nwp_generation",
    }, ""


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace(" ", "T"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ms_between(later: datetime | None, earlier: datetime | None) -> int | None:
    if later is None or earlier is None:
        return None
    return int(round((later - earlier).total_seconds() * 1000))


def _slo_stage_status(value_ms: int | None, slo_ms: int) -> str:
    if value_ms is None:
        return "missing"
    if value_ms < 0:
        return "clock_order_anomaly"
    return "met" if value_ms <= slo_ms else "breached"


def _build_amsc_latency_slo_audit(
    *,
    latest_source_row: Mapping[str, Any],
    latest_valid_dt: datetime | None,
    latest_response_dt: datetime | None,
    latest_source_fact_dt: datetime | None,
    posterior_dt: datetime | None,
    source_event_ids: Sequence[str],
    source_observation_ids: Sequence[str],
) -> dict[str, Any]:
    stage_slo_ms = {
        "valid_to_fetched": 90_000,
        "fetched_to_fact": 5_000,
        "fact_to_posterior": 10_000,
        "posterior_to_decision": 3_000,
    }
    stage_latencies_ms: dict[str, int | None] = {
        "valid_to_fetched": _ms_between(latest_response_dt, latest_valid_dt),
        "fetched_to_fact": _ms_between(latest_source_fact_dt, latest_response_dt),
        "fact_to_posterior": _ms_between(posterior_dt, latest_source_fact_dt),
        "posterior_to_decision": None,
    }
    missing_fields: list[str] = []
    for key, value in (
        ("valid_time", latest_valid_dt),
        ("fetched_at", latest_response_dt),
        ("fact_as_of", latest_source_fact_dt),
        ("posterior_as_of", posterior_dt),
        ("decision_at", None),
    ):
        if value is None:
            missing_fields.append(key)
    stage_status = {
        key: _slo_stage_status(value, int(stage_slo_ms[key]))
        for key, value in stage_latencies_ms.items()
    }
    available_stage_values = {
        key: value
        for key, value in stage_latencies_ms.items()
        if isinstance(value, int) and value >= 0
    }
    slow_stage = max(available_stage_values, key=available_stage_values.get) if available_stage_values else ""
    breached_stages = [key for key, status in stage_status.items() if status == "breached"]
    state = "partial" if missing_fields else "available"
    return {
        "schema": "amsc_latency_slo_audit_v1",
        "state": state,
        "state_reason": (
            "missing_fields:" + ",".join(missing_fields)
            if missing_fields
            else "all_chain_timestamps_available"
        ),
        "source": "scripts/materialize_amsc_posterior_task.py",
        "source_provider": "AMSC",
        "source_id": str(latest_source_row.get("source_id") or ""),
        "source_event_id": str(latest_source_row.get("source_event_id") or ""),
        "source_observation_id": str(latest_source_row.get("source_observation_id") or ""),
        "source_event_ids": [str(value) for value in source_event_ids if str(value or "").strip()][:20],
        "source_observation_ids": [
            str(value) for value in source_observation_ids if str(value or "").strip()
        ][:20],
        "valid_time": _iso_utc(latest_valid_dt),
        "fetched_at": _iso_utc(latest_response_dt),
        "fact_as_of": _iso_utc(latest_source_fact_dt),
        "posterior_as_of": _iso_utc(posterior_dt),
        "decision_at": "",
        "decision_at_na_reason": "decision_not_written_by_posterior_materialization_task",
        "stage_order": ["valid_time", "fetched_at", "fact_as_of", "posterior_as_of", "decision_at"],
        "stage_latencies_ms": stage_latencies_ms,
        "stage_slo_ms": stage_slo_ms,
        "stage_status": stage_status,
        "slow_stage": slow_stage,
        "breached_stages": breached_stages,
        "valid_to_fetched_ms": stage_latencies_ms["valid_to_fetched"],
        "fetched_to_fact_ms": stage_latencies_ms["fetched_to_fact"],
        "fact_to_posterior_ms": stage_latencies_ms["fact_to_posterior"],
        "posterior_to_decision_ms": None,
        "valid_to_posterior_ms": _ms_between(posterior_dt, latest_valid_dt),
        "fetched_to_posterior_ms": _ms_between(posterior_dt, latest_response_dt),
        "valid_to_decision_ms": None,
        "fetched_to_decision_ms": None,
        "fact_to_decision_ms": None,
        "missing_fields": missing_fields,
        "slo_met": False if breached_stages else None,
        "hard_source_latency_slo_met": False if breached_stages else None,
        "read_only": True,
        "frontend_recompute_allowed": False,
        "decision_recompute_allowed": False,
    }


def _prob01(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    if parsed > 1.0 and parsed <= 100.0:
        parsed /= 100.0
    return max(0.0, min(1.0, parsed))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _load_task_queue_context(conn: sqlite3.Connection) -> dict[str, Any]:
    task_id_raw = str(os.environ.get("TASK_QUEUE_ID") or "").strip()
    if not task_id_raw.isdigit() or not _table_exists(conn, "task_queue"):
        return {"available": False, "reason": "task_queue_id_or_table_missing"}
    columns = _table_columns(conn, "task_queue")
    wanted = ["id", "task_type", "status", "created_at", "started_at", "completed_at", "retry_count", "max_retries"]
    select_cols = [col for col in wanted if col in columns]
    if not {"id", "created_at"}.issubset(set(select_cols)):
        return {"available": False, "reason": "task_queue_schema_incomplete", "task_id": int(task_id_raw)}
    try:
        row = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM task_queue WHERE id=? LIMIT 1",
            (int(task_id_raw),),
        ).fetchone()
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"task_queue_read_failed:{exc}", "task_id": int(task_id_raw)}
    if row is None:
        return {"available": False, "reason": "task_queue_row_missing", "task_id": int(task_id_raw)}
    return {
        "available": True,
        **{col: row[col] for col in select_cols},
    }


def _append_unique(out: list[str], raw: Any) -> None:
    text = str(raw or "").strip()
    if text and text not in out:
        out.append(text)


def _payload_source_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        key = (
            str(raw.get("source_observation_id") or ""),
            str(raw.get("source_event_id") or ""),
            str(raw.get("source_id") or ""),
            str(raw.get("valid_time") or ""),
        )
        if key in seen:
            return
        seen.add(key)
        rows.append(raw)

    for row in payload.get("source_latency_trace") or []:
        add(row)
    for row in payload.get("source_observations") or []:
        add(row)
    trace = payload.get("source_acquisition_trace")
    if isinstance(trace, Mapping):
        for row in trace.get("source_latency_trace") or []:
            add(row)
        for row in trace.get("source_observations") or []:
            add(row)
    return rows


def _latest_dt(rows: list[Mapping[str, Any]], keys: Sequence[str]) -> datetime | None:
    latest: datetime | None = None
    for row in rows:
        for key in keys:
            parsed = _parse_utc(row.get(key))
            if parsed is not None and (latest is None or parsed > latest):
                latest = parsed
    return latest


def _latest_row(rows: list[Mapping[str, Any]], keys: Sequence[str]) -> Mapping[str, Any]:
    best_row: Mapping[str, Any] = {}
    best_dt: datetime | None = None
    for row in rows:
        row_dt = _latest_dt([row], keys)
        if row_dt is not None and (best_dt is None or row_dt > best_dt):
            best_dt = row_dt
            best_row = row
    return best_row


def _build_source_to_posterior_timing(
    *,
    plan: Mapping[str, Any],
    effective_payload: Mapping[str, Any],
    task_payload: Mapping[str, Any],
    projection_context: Mapping[str, Any],
    task_queue_context: Mapping[str, Any],
    materialization_started_at: str,
) -> dict[str, Any]:
    source_context = _as_mapping(projection_context.get("source_observation_context"))
    source_rows = _payload_source_rows(effective_payload)
    amsc_rows = [
        row
        for row in source_rows
        if "amsc" in str(row.get("source_id") or row.get("source") or "").lower()
    ]
    hard_rows = [
        row
        for row in amsc_rows
        if str(row.get("source_class") or "").strip() == "hard_observation_candidate"
        or "metar" in str(row.get("role") or "").lower()
    ]
    lineage_rows = amsc_rows or source_rows
    candidate_rows = hard_rows or amsc_rows or source_rows
    latest_source_row = _latest_row(candidate_rows, ("response_completed_at", "fetched_at", "valid_time"))
    source_event_ids: list[str] = []
    source_observation_ids: list[str] = []
    for row in lineage_rows:
        _append_unique(source_event_ids, row.get("source_event_id"))
        _append_unique(source_observation_ids, row.get("source_observation_id"))
    for raw in plan.get("source_event_ids") or []:
        _append_unique(source_event_ids, raw)
    for raw in effective_payload.get("source_observation_ids") or []:
        _append_unique(source_observation_ids, raw)
    for raw in plan.get("source_observation_ids") or []:
        _append_unique(source_observation_ids, raw)

    latest_response_dt = _latest_dt(candidate_rows, ("response_completed_at", "fetched_at"))
    latest_valid_dt = _latest_dt(candidate_rows, ("valid_time",))
    source_fact_as_of_candidates = [
        task_payload.get("amsc_source_fact_as_of"),
        source_context.get("source_history_fact_as_of_max"),
        plan.get("fact_as_of"),
        effective_payload.get("fact_as_of"),
    ]
    latest_source_fact_dt: datetime | None = None
    for raw in source_fact_as_of_candidates:
        parsed = _parse_utc(raw)
        if parsed is not None and (latest_source_fact_dt is None or parsed > latest_source_fact_dt):
            latest_source_fact_dt = parsed

    materialization_started_dt = _parse_utc(materialization_started_at)
    task_created_dt = _parse_utc(task_queue_context.get("created_at")) if task_queue_context.get("available") else None
    task_started_dt = _parse_utc(task_queue_context.get("started_at")) if task_queue_context.get("available") else None

    state = "available" if latest_source_fact_dt or latest_response_dt or candidate_rows else "partial"
    timing: dict[str, Any] = {
        "schema": "source_to_posterior_timing_v1",
        "state": state,
        "state_reason": (
            "amsc_source_history_materialized_into_posterior_fact"
            if state == "available"
            else "source_history_rows_missing_timing_fields"
        ),
        "surface": "amsc_hard_source_to_posterior_materialization",
        "source": "scripts/materialize_amsc_posterior_task.py",
        "source_provider": "AMSC",
        "source_kind": "hard_observation_candidate_plus_taf_weak_likelihood",
        "slug": str(task_payload.get("slug") or plan.get("slug") or effective_payload.get("slug") or ""),
        "event_date": str(
            task_payload.get("event_date") or plan.get("event_date") or effective_payload.get("event_date") or ""
        ),
        "task_queue_id": int(task_queue_context.get("id") or 0) if task_queue_context.get("available") else None,
        "task_queue_status": str(task_queue_context.get("status") or "") if task_queue_context.get("available") else "",
        "task_created_at": _iso_utc(task_created_dt),
        "task_started_at": _iso_utc(task_started_dt),
        "posterior_apply_started_at": _iso_utc(materialization_started_dt),
        "latest_source_fact_as_of": _iso_utc(latest_source_fact_dt),
        "latest_source_response_completed_at": _iso_utc(latest_response_dt),
        "latest_source_valid_time": _iso_utc(latest_valid_dt),
        "latest_source_id": str(latest_source_row.get("source_id") or ""),
        "latest_source_observation_id": str(latest_source_row.get("source_observation_id") or ""),
        "latest_source_event_id": str(latest_source_row.get("source_event_id") or ""),
        "latest_temperature_c": latest_source_row.get("temperature_c"),
        "latest_observed_today_max_c": latest_source_row.get("observed_today_max_c"),
        "source_history_fact_ids": list(source_context.get("source_history_fact_ids") or []),
        "source_history_fact_count": int(source_context.get("source_history_fact_count") or 0),
        "source_history_merged_observation_count": int(
            source_context.get("source_history_merged_observation_count") or 0
        ),
        "source_event_ids": source_event_ids,
        "source_observation_ids": source_observation_ids,
        "source_response_to_posterior_apply_start_ms": _ms_between(materialization_started_dt, latest_response_dt),
        "source_fact_to_posterior_apply_start_ms": _ms_between(materialization_started_dt, latest_source_fact_dt),
        "source_valid_to_posterior_apply_start_ms": _ms_between(materialization_started_dt, latest_valid_dt),
        "task_queue_wait_ms": _ms_between(task_started_dt, task_created_dt),
        "task_created_to_posterior_apply_start_ms": _ms_between(materialization_started_dt, task_created_dt),
        "source_response_completed_at_na_reason": (
            "" if latest_response_dt else "source_rows_do_not_include_response_completed_or_fetched_at"
        ),
        "order_signal_applicability": "not_applicable_no_order_signal",
        "guards": {
            "fetches_external_weather_sources": False,
            "writes_auto_trade_decisions": False,
            "writes_auto_trade_jobs": False,
            "places_orders": False,
            "historical_backfill": False,
        },
    }
    timing["amsc_latency_slo_audit"] = _build_amsc_latency_slo_audit(
        latest_source_row=latest_source_row,
        latest_valid_dt=latest_valid_dt,
        latest_response_dt=latest_response_dt,
        latest_source_fact_dt=latest_source_fact_dt,
        posterior_dt=materialization_started_dt,
        source_event_ids=source_event_ids,
        source_observation_ids=source_observation_ids,
    )
    return timing


def _plan_with_source_to_posterior_timing(
    plan: Mapping[str, Any],
    timing: Mapping[str, Any],
) -> dict[str, Any]:
    plan_out = copy.deepcopy(dict(plan))
    payload = copy.deepcopy(dict(_as_mapping(plan_out.get("effective_payload"))))
    if not payload:
        return plan_out
    timing_payload = copy.deepcopy(dict(timing))
    source_event_ids: list[str] = []
    source_observation_ids: list[str] = []
    for row in _payload_source_rows(payload):
        _append_unique(source_event_ids, row.get("source_event_id"))
        _append_unique(source_observation_ids, row.get("source_observation_id"))
    for raw in plan_out.get("source_event_ids") or []:
        _append_unique(source_event_ids, raw)
    for raw in plan_out.get("source_observation_ids") or []:
        _append_unique(source_observation_ids, raw)
    for raw in timing_payload.get("source_event_ids") or []:
        _append_unique(source_event_ids, raw)
    for raw in timing_payload.get("source_observation_ids") or []:
        _append_unique(source_observation_ids, raw)
    if source_event_ids:
        plan_out["source_event_ids"] = source_event_ids
        payload["source_event_ids"] = source_event_ids
    if source_observation_ids:
        plan_out["source_observation_ids"] = source_observation_ids
        payload["source_observation_ids"] = source_observation_ids
    payload["source_to_posterior_timing"] = timing_payload
    payload["amsc_posterior_materialization_timing"] = timing_payload
    if isinstance(timing_payload.get("amsc_latency_slo_audit"), Mapping):
        payload["amsc_latency_slo_audit"] = copy.deepcopy(timing_payload["amsc_latency_slo_audit"])

    signal_timing = copy.deepcopy(dict(_as_mapping(payload.get("signal_to_order_timing"))))
    if not signal_timing:
        signal_timing = {
            "schema": "signal_to_order_timing_placeholder_v1",
            "state": "not_applicable",
            "state_reason": "fact_snapshot_write_does_not_submit_orders",
        }
    signal_timing.setdefault("schema", "signal_to_order_timing_placeholder_v1")
    signal_timing.setdefault("state", "not_applicable")
    signal_timing.setdefault("state_reason", "fact_snapshot_write_does_not_submit_orders")
    signal_timing.setdefault("fact_snapshot_id", str(payload.get("fact_snapshot_id") or plan_out.get("fact_snapshot_id") or ""))
    signal_timing.setdefault("posterior_run_id", str(payload.get("posterior_run_id") or plan_out.get("posterior_run_id") or ""))
    if not signal_timing.get("source_observation_ids"):
        signal_timing["source_observation_ids"] = list(source_observation_ids or timing_payload.get("source_observation_ids") or [])
    if not signal_timing.get("source_event_ids"):
        signal_timing["source_event_ids"] = list(source_event_ids or timing_payload.get("source_event_ids") or [])
    signal_timing["source_to_posterior_surface"] = "amsc_hard_source_to_posterior_materialization"
    signal_timing["source_to_posterior_state"] = str(timing_payload.get("state") or "")
    signal_timing["source_to_posterior_timing"] = timing_payload
    if isinstance(timing_payload.get("amsc_latency_slo_audit"), Mapping):
        signal_timing["amsc_latency_slo_audit"] = copy.deepcopy(timing_payload["amsc_latency_slo_audit"])
    payload["signal_to_order_timing"] = signal_timing

    plan_out["effective_payload"] = payload
    plan_out["payload_hash_after"] = sha256_json(payload)
    plan_out["payload_changed_by_source_to_posterior_timing"] = True
    return plan_out


def _current_projection_materialization_reasons(
    current_payload: Mapping[str, Any],
    projected_payload: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    current_trajectory = _as_mapping(current_payload.get("temperature_trajectory"))
    projected_trajectory = _as_mapping(projected_payload.get("temperature_trajectory"))
    if not current_trajectory and projected_trajectory:
        reasons.append("temperature_trajectory_root_missing")

    current_features = _as_mapping(current_payload.get("weather_likelihood_features"))
    projected_features = _as_mapping(projected_payload.get("weather_likelihood_features"))
    for key in (
        "temperature_trajectory_state",
        "temperature_trajectory_gate_state",
        "temperature_trajectory_stable_rate_c_per_hour",
        "temperature_trajectory_effective_rate_c_per_hour",
    ):
        if current_features.get(key) in (None, "", [], {}) and projected_features.get(key) not in (None, "", [], {}):
            reasons.append(f"weather_likelihood_features.{key}_missing")

    current_ssot = _as_mapping(current_payload.get("posterior_ssot_fact"))
    projected_ssot = _as_mapping(projected_payload.get("posterior_ssot_fact"))
    if str(current_ssot.get("state") or "") != "available" and str(projected_ssot.get("state") or "") == "available":
        reasons.append("posterior_ssot_fact_not_materialized")
    if str(projected_ssot.get("state") or "") == "available":
        current_label = str(
            current_ssot.get("target_bucket")
            or current_ssot.get("top_bucket")
            or current_ssot.get("selected_bucket")
            or ""
        ).strip()
        projected_label = str(
            projected_ssot.get("target_bucket")
            or projected_ssot.get("top_bucket")
            or projected_ssot.get("selected_bucket")
            or ""
        ).strip()
        if current_label and projected_label and current_label != projected_label:
            reasons.append("posterior_ssot_fact_target_bucket_changed")

        current_prob = _prob01(
            current_ssot.get("target_probability")
            if current_ssot.get("target_probability") not in (None, "")
            else current_ssot.get("target_probability_pct")
        )
        projected_prob = _prob01(
            projected_ssot.get("target_probability")
            if projected_ssot.get("target_probability") not in (None, "")
            else projected_ssot.get("target_probability_pct")
        )
        if (
            current_prob is not None
            and projected_prob is not None
            and abs(float(current_prob) - float(projected_prob)) > 1e-9
        ):
            reasons.append("posterior_ssot_fact_target_probability_changed")
        if bool(projected_ssot.get("final_bucket_lock")) and not bool(current_ssot.get("final_bucket_lock")):
            reasons.append("posterior_ssot_fact_final_bucket_lock_changed")

    current_model = str(current_payload.get("production_model_version") or "").strip()
    projected_model = str(projected_payload.get("production_model_version") or "").strip()
    if projected_model and current_model != projected_model:
        reasons.append("production_model_version_changed")
    current_v3 = _as_mapping(current_payload.get("v3_synoptic_posterior"))
    projected_v3 = _as_mapping(projected_payload.get("v3_synoptic_posterior"))
    for key in ("state", "state_reason", "model_version", "regime"):
        if str(current_v3.get(key) or "") != str(projected_v3.get(key) or ""):
            reasons.append(f"v3_synoptic_posterior.{key}_changed")

    return reasons


def _guards(*, writes: bool) -> dict[str, bool]:
    return {
        "writes_archive_db": bool(writes),
        "writes_event_fact_history": bool(writes),
        "writes_event_fact_latest": bool(writes),
        "writes_event_fact_summary_cache": bool(writes),
        "writes_source_observations": bool(writes),
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_auto_orders": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "restarts_launchd": False,
        "executes_sql": bool(writes),
    }


def _blocked(reason: str, *, payload: Mapping[str, Any] | None = None, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload_map = _as_mapping(payload)
    out = {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "applied": False,
        "skipped": False,
        "blocked": True,
        "block_reason": str(reason or "blocked"),
        "slug": str(payload_map.get("slug") or ""),
        "event_date": str(payload_map.get("event_date") or ""),
        "guards": _guards(writes=False),
    }
    out.update(dict(extra or {}))
    return out


def _skipped(reason: str, *, payload: Mapping[str, Any], extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "schema": RESULT_SCHEMA,
        "ok": True,
        "applied": False,
        "skipped": True,
        "blocked": False,
        "skip_reason": str(reason or "skipped"),
        "slug": str(payload.get("slug") or ""),
        "event_date": str(payload.get("event_date") or ""),
        "guards": _guards(writes=False),
    }
    out.update(dict(extra or {}))
    return out


@contextmanager
def _connect_rw(db_path: Path):
    with connect_archive_db_rw(
        db_path,
        owner="materialize_amsc_posterior_task",
        timeout_sec=30.0,
        busy_timeout_ms=5000,
        row_factory=True,
    ) as conn:
        yield conn


@contextmanager
def _connect_ro(db_path: Path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()


def _load_latest_fact(conn: sqlite3.Connection, *, slug: str, event_date: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT h.id, h.run_date, h.slug, h.event_date, h.fact_as_of, h.composer_version, h.fact_json, h.created_at
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        LIMIT 1
        """,
        (slug, event_date),
    ).fetchone()


def _load_source_fact_anchor(
    conn: sqlite3.Connection,
    *,
    source_fact_id: Any,
    slug: str,
    event_date: str,
) -> sqlite3.Row | None:
    try:
        fact_id = int(source_fact_id or 0)
    except (TypeError, ValueError):
        return None
    if fact_id <= 0:
        return None
    return conn.execute(
        """
        SELECT id, run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        FROM event_fact_history
        WHERE id = ?
          AND slug = ?
          AND event_date = ?
        LIMIT 1
        """,
        (fact_id, slug, event_date),
    ).fetchone()


def _load_payload(raw: str = "", payload_path: Path | None = None) -> dict[str, Any]:
    if payload_path:
        return _safe_json(payload_path.read_text(encoding="utf-8"))
    if raw:
        return _safe_json(raw)
    env_payload = os.environ.get("TASK_PAYLOAD_JSON", "")
    if env_payload:
        return _safe_json(env_payload)
    env_path = os.environ.get("TASK_PAYLOAD_PATH", "")
    if env_path:
        return _safe_json(Path(env_path).expanduser().read_text(encoding="utf-8"))
    return {}


PEAK_WINDOW_ANCHOR_KEYS = (
    "expected_peak_hour_local",
    "expected_peak_at_local",
    "expected_peak_window_start_hour_local",
    "expected_peak_window_end_hour_local",
    "expected_peak_window_source",
    "expected_peak_window_basis",
    "taf_forecast_peak_at",
    "taf_forecast_peak_local",
    "taf_forecast_peak_c",
    "taf_forecast_peak_raw",
)


def _payload_features(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    features = _as_mapping(payload.get("weather_likelihood_features"))
    if features:
        return features
    return _as_mapping(payload.get("weather_likelihood_features_compact"))


def _same_scalar(left: Any, right: Any) -> bool:
    if left in (None, "", [], {}) and right in (None, "", [], {}):
        return True
    try:
        left_float = float(left)
        right_float = float(right)
    except Exception:
        return str(left or "").strip() == str(right or "").strip()
    return abs(left_float - right_float) <= 1e-9


def _local_hour_from_iso(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return float(parsed.hour) + float(parsed.minute) / 60.0 + float(parsed.second) / 3600.0
    local = parsed.astimezone(parsed.tzinfo)
    return float(local.hour) + float(local.minute) / 60.0 + float(local.second) / 3600.0


def _derived_peak_window_anchor_features(source_features: Mapping[str, Any]) -> dict[str, Any]:
    peak_local = str(
        source_features.get("expected_peak_at_local")
        or source_features.get("taf_forecast_peak_local")
        or ""
    ).strip()
    peak_hour = _local_hour_from_iso(peak_local)
    if peak_hour is None:
        return {}
    return {
        "expected_peak_hour_local": round(float(peak_hour), 3),
        "expected_peak_at_local": peak_local,
        "expected_peak_window_start_hour_local": round(max(0.0, float(peak_hour) - PEAK_WINDOW_LEAD_HOURS), 3),
        "expected_peak_window_end_hour_local": round(min(23.99, float(peak_hour) + PEAK_WINDOW_POST_HOURS), 3),
    }


def _source_anchor_should_drive_projection(
    *,
    latest_payload: Mapping[str, Any],
    source_payload: Mapping[str, Any],
) -> bool:
    source_features = _payload_features(source_payload)
    if not source_features:
        return False
    latest_features = _payload_features(latest_payload)
    for key in PEAK_WINDOW_ANCHOR_KEYS:
        source_value = source_features.get(key)
        if source_value in (None, "", [], {}):
            continue
        if not _same_scalar(latest_features.get(key), source_value):
            return True
    for key, source_value in _derived_peak_window_anchor_features(source_features).items():
        if source_value in (None, "", [], {}):
            continue
        if not _same_scalar(latest_features.get(key), source_value):
            return True
    return False


def materialize_amsc_posterior_task(
    payload: Mapping[str, Any],
    *,
    db_path: Path | str = DEFAULT_DB,
    allow_apply: bool = True,
    generated_at: str = "",
    v31_cache_path: Path | str = DEFAULT_V31_LIVE_MULTIMODEL_CACHE,
    require_prewarmed_v31_cache: bool | None = None,
) -> dict[str, Any]:
    payload_map = dict(payload or {})
    slug = str(payload_map.get("slug") or "").strip()
    event_date = str(payload_map.get("event_date") or "").strip()
    if not slug or not event_date:
        return _blocked("slug_or_event_date_missing", payload=payload_map)
    db = Path(db_path).expanduser().resolve()
    if not db.exists():
        return _blocked("archive_db_missing", payload=payload_map, extra={"db": str(db)})
    if not allow_apply:
        return _blocked("allow_apply_false", payload=payload_map, extra={"db": str(db)})

    generated = str(generated_at or _now_utc())
    cache_path = Path(v31_cache_path).expanduser().resolve()
    context_policy, context_policy_error = _resolve_v31_multimodel_context_policy(
        payload_map,
        explicit_require_prewarmed_cache=require_prewarmed_v31_cache,
    )
    if context_policy_error:
        return _blocked(
            context_policy_error,
            payload=payload_map,
            extra={
            "official_nwp_generation_path": str(cache_path),
                "writes_posterior": False,
                "reason_scope": "queued_multimodel_context_policy",
            },
        )
    require_prewarmed_cache = bool(context_policy["require_prewarmed_cache"])
    require_valid_cache = bool(context_policy["require_valid_cache"])
    max_prewarm_age_sec = _env_positive_int(
        "AMSC_ZUUU_V31_PREWARM_MAX_CACHE_AGE_SEC",
        DEFAULT_V31_PREWARM_MAX_CACHE_AGE_SEC,
    )
    # Build the complete projection without holding the global archive writer
    # gate.  Only the final CAS + apply section is serialized as a short write
    # transaction below.
    with _connect_ro(db) as conn:
        latest = _load_latest_fact(conn, slug=slug, event_date=event_date)
        source_anchor = _load_source_fact_anchor(
            conn,
            source_fact_id=payload_map.get("amsc_source_fact_id"),
            slug=slug,
            event_date=event_date,
        )
        if latest is None:
            if source_anchor is None:
                return _blocked(
                    "event_fact_latest_missing",
                    payload=payload_map,
                    extra={
                        "db": str(db),
                        "amsc_source_fact_id": payload_map.get("amsc_source_fact_id"),
                        "fallback_anchor": "amsc_source_fact_id",
                        "fallback_anchor_state": "missing_or_mismatched",
                    },
                )
            latest = source_anchor
            fallback_anchor = "amsc_source_fact_id"
        else:
            fallback_anchor = "event_fact_latest"
        latest_payload = _safe_json(latest["fact_json"])
        if not latest_payload:
            return _blocked(
                "latest_fact_json_invalid",
                payload=payload_map,
                extra={"current_fact_id": int(latest["id"] or 0)},
            )
        latest_fact_as_of = str(latest["fact_as_of"] or latest_payload.get("fact_as_of") or generated)
        source_fact_as_of = str(source_anchor["fact_as_of"] or "") if source_anchor is not None else ""
        latest_fact_time = _parse_utc(latest_fact_as_of)
        source_fact_time = _parse_utc(source_fact_as_of)
        projection_fact_as_of = (
            source_fact_as_of
            if source_fact_time is not None and (latest_fact_time is None or source_fact_time > latest_fact_time)
            else latest_fact_as_of
        )
        latest_payload, prewarm_readiness = _attach_v31_live_multimodel_snapshot(
            latest_payload,
            cache_path=cache_path,
            as_of=projection_fact_as_of,
            require_prewarmed_cache=require_prewarmed_cache,
            require_valid_cache=require_valid_cache,
            cache_generation_ref=(
                _as_mapping(context_policy.get("cache_generation_ref"))
                if "cache_generation_ref" in context_policy
                else None
            ),
            max_cache_age_sec=max_prewarm_age_sec,
        )
        if require_valid_cache and (
            prewarm_readiness.get("ok") is not True
            or str(latest_payload.get("official_nwp_generation_state") or "") != "available"
        ):
            return _blocked(
                "official_nwp_generation_required_but_unavailable",
                payload=payload_map,
                extra={
                    "official_nwp_generation_path": str(cache_path),
                    "official_nwp_context_policy": context_policy,
                    "official_nwp_prewarm_readiness": prewarm_readiness,
                    "writes_posterior": False,
                    "reason_scope": "prewarm_cutover_fail_closed",
                },
            )
        source_anchor_peak_window_update_pending = False
        if source_anchor is not None and int(source_anchor["id"] or 0) != int(latest["id"] or 0):
            source_payload = _safe_json(source_anchor["fact_json"])
            if source_payload and _source_anchor_should_drive_projection(
                latest_payload=latest_payload,
                source_payload=source_payload,
            ):
                source_anchor_peak_window_update_pending = True
        projection = build_fact_projection_no_apply(
            slug=slug,
            event_date=event_date,
            run_date=str(latest_payload.get("run_date") or latest["run_date"] or event_date),
            fact_as_of=projection_fact_as_of,
            payload=latest_payload,
            current_fact_id=int(latest["id"] or 0),
            current_fact_as_of=str(latest["fact_as_of"] or ""),
            composer_version=str(latest["composer_version"] or ""),
            db_path=db,
            conn=conn,
            write_reason="amsc_posterior_materialization_task",
            # A queued source task may be retried long after later reports or
            # forecasts exist. Its posterior must remain exactly as-of the
            # source fact it was asked to materialize; never borrow a newer
            # fact, model cache row, or soft-forecast event run on retry.
            as_of_cutoff=source_fact_as_of or str(payload_map.get("amsc_source_fact_as_of") or ""),
            enable_apply_contract=True,
            write_latest=True,
        )
        context = _as_mapping(projection.get("context"))
        source_history_merged = bool(context.get("source_history_observations_merged"))
        runtime_policy_recompute = bool(context.get("runtime_policy_recompute"))
        runtime_policy_context = _as_mapping(context.get("runtime_policy_context"))
        plan = copy.deepcopy(dict(_as_mapping(projection.get("plan"))))
        projected_payload = _as_mapping(plan.get("effective_payload") or projection.get("projected_payload"))
        current_projection_reasons = _current_projection_materialization_reasons(latest_payload, projected_payload)
        if not source_history_merged and not current_projection_reasons and not runtime_policy_recompute:
            return _skipped(
                "no_newer_amsc_source_history_to_materialize",
                payload=payload_map,
                extra={
                    "current_fact_id": int(latest["id"] or 0),
                    "current_fact_as_of": str(latest["fact_as_of"] or ""),
                    "projection_mode": str(projection.get("projection_mode") or ""),
                    "source_observation_context": dict(_as_mapping(context.get("source_observation_context"))),
                    "runtime_policy_context": dict(runtime_policy_context),
                    "current_projection_materialization_reasons": [],
                    "source_anchor_peak_window_update_pending": bool(source_anchor_peak_window_update_pending),
                },
            )
        if projection.get("ok") is not True or plan.get("ok") is not True or plan.get("apply_supported") is not True:
            return _blocked(
                "posterior_projection_not_apply_ready",
                payload=payload_map,
                extra={
                    "current_fact_id": int(latest["id"] or 0),
                    "projection_mode": str(projection.get("projection_mode") or ""),
                    "projection_issues": list(projection.get("issues") or []),
                    "writer_plan_ok": bool(plan.get("ok")),
                    "writer_plan_apply_supported": bool(plan.get("apply_supported")),
                },
            )
        effective_payload = _as_mapping(plan.get("effective_payload"))
        task_queue_context = _load_task_queue_context(conn)
        source_to_posterior_timing = _build_source_to_posterior_timing(
            plan=plan,
            effective_payload=effective_payload,
            task_payload=payload_map,
            projection_context=context,
            task_queue_context=task_queue_context,
            materialization_started_at=generated,
        )
        plan = _plan_with_source_to_posterior_timing(plan, source_to_posterior_timing)
        with _connect_rw(db) as write_conn:
            latest_before_apply = _load_latest_fact(
                write_conn,
                slug=slug,
                event_date=event_date,
            )
            expected_fact_id = int(latest["id"] or 0)
            actual_fact_id = int(latest_before_apply["id"] or 0) if latest_before_apply is not None else 0
            source_anchor_bootstrap = bool(
                fallback_anchor == "amsc_source_fact_id" and actual_fact_id == 0
            )
            if actual_fact_id != expected_fact_id and not source_anchor_bootstrap:
                return _blocked(
                    "event_fact_latest_changed_during_projection",
                    payload=payload_map,
                    extra={
                        "expected_fact_id": expected_fact_id,
                        "actual_fact_id": actual_fact_id,
                        "retry_safe": True,
                        "projection_writes_discarded": True,
                    },
                )
            writer_result = apply_fact_snapshot_write_plan(
                write_conn,
                plan,
                allow_apply=True,
                commit=True,
                created_at=generated,
            )
            committed_at = str(writer_result.get("committed_at") or "").strip()
            attestation = append_posterior_materialization_commit_attestation(
                write_conn,
                materialized_fact_id=writer_result.get("fact_id"),
                task_queue_id=task_queue_context.get("id") if task_queue_context.get("available") else None,
                source_fact_id=payload_map.get("amsc_source_fact_id"),
                source_received_at=source_to_posterior_timing.get("latest_source_response_completed_at"),
                posterior_apply_started_at=source_to_posterior_timing.get("posterior_apply_started_at"),
                posterior_committed_at=committed_at,
                fact_snapshot_id=writer_result.get("fact_snapshot_id"),
                posterior_run_id=writer_result.get("posterior_run_id"),
            ) if bool(writer_result.get("applied")) else {
                "ok": False,
                "state": "not_applicable_fact_not_applied",
            }
        completed_at = str(writer_result.get("committed_at") or _now_utc())
        result_timing = dict(source_to_posterior_timing)
        result_timing["posterior_apply_completed_at"] = completed_at
        result_timing["posterior_apply_runtime_ms"] = _ms_between(
            _parse_utc(completed_at),
            _parse_utc(source_to_posterior_timing.get("posterior_apply_started_at")),
        )
        summary = compact_fact_snapshot_writer_apply_result_for_lineage(writer_result)
        writes = bool(writer_result.get("applied"))
        return {
            "schema": RESULT_SCHEMA,
            "ok": bool(writer_result.get("ok")),
            "applied": writes,
            "skipped": False,
            "blocked": bool(writer_result.get("blocked")),
            "block_reason": str(writer_result.get("block_reason") or ""),
            "slug": slug,
            "event_date": event_date,
            "db": str(db),
            "current_fact_id": int(latest["id"] or 0),
            "current_fact_as_of": str(latest["fact_as_of"] or ""),
            "current_fact_anchor": fallback_anchor,
            "new_fact_id": writer_result.get("fact_id"),
            "posterior_commit_attestation": attestation,
            "latest_repointed": bool(writer_result.get("latest_repointed")),
            "projection_mode": str(projection.get("projection_mode") or ""),
            "source_observation_context": dict(_as_mapping(context.get("source_observation_context"))),
            "runtime_policy_context": dict(runtime_policy_context),
            "runtime_policy_recompute": bool(runtime_policy_recompute),
            "current_projection_materialization_reasons": current_projection_reasons,
            "source_anchor_peak_window_update_pending": bool(source_anchor_peak_window_update_pending),
            "official_nwp_context_policy": context_policy,
            "source_to_posterior_timing": result_timing,
            "writer_apply_result": writer_result,
            "writer_apply_result_summary": summary,
            "guards": _guards(writes=writes),
        }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--payload-json", default="")
    parser.add_argument("--payload-file", type=Path, default=None)
    parser.add_argument("--no-apply", action="store_true")
    parser.add_argument("--result-json", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    payload = _load_payload(args.payload_json, args.payload_file)
    result = materialize_amsc_posterior_task(
        payload,
        db_path=args.db,
        allow_apply=not bool(args.no_apply),
    )
    if args.result_json:
        _write_json_atomic(args.result_json, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if bool(result.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
