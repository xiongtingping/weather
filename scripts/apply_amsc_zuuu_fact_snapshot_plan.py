#!/usr/bin/env python3
"""Apply a ZUUU AMSC fact snapshot writer plan to archive.db."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weather_fact_snapshot_writer import (  # noqa: E402
    apply_fact_snapshot_write_plan,
    build_fact_snapshot_write_plan,
    compact_fact_snapshot_writer_apply_result_for_lineage,
    utc_now_iso,
)
from scripts.fetch_zuuu_metar_taf import AMSC_LATEST_REPOINT_BLOCK_REASON  # noqa: E402
from archive_db_write_queue import connect_archive_db_rw  # noqa: E402
from official_nwp_multimodel_prewarm import GENERATION_SCHEMA  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
CONFIRM_PHRASE = "APPLY_AMSC_ZUUU_FACT_SNAPSHOT"
RESULT_SCHEMA = "amsc_zuuu_fact_snapshot_apply_result_v1"
POSTERIOR_MATERIALIZATION_TASK_TYPE = "amsc_posterior_materialize"
POSTERIOR_MATERIALIZATION_PAYLOAD_SCHEMA = "amsc_posterior_materialization_task_payload_v1"
V31_MULTIMODEL_CONTEXT_POLICY_SCHEMA = "official_nwp_context_policy_v1"
V31_PREWARMED_CACHE_ONLY = "prewarmed_cache_only_v1"
DEFAULT_V31_MULTIMODEL_CONTEXT_MODE = V31_PREWARMED_CACHE_ONLY


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_loads(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _load_v31_multimodel_generation_ref(value: Mapping[str, Any] | None) -> dict[str, Any]:
    ref = dict(value) if isinstance(value, Mapping) else {}
    required = ("generation_id", "bundle_sha256", "generation_path", "catalog_id", "response_completed_at")
    if ref.get("schema") != GENERATION_SCHEMA or any(not str(ref.get(key) or "").strip() for key in required):
        return {"schema": GENERATION_SCHEMA, "state": "missing_or_invalid"}
    return {"schema": GENERATION_SCHEMA, **{key: ref.get(key) for key in required}, "provider_set": list(ref.get("provider_set") or [])}


def _amsc_signature(payload: Mapping[str, Any]) -> dict[str, Any]:
    features = _as_mapping(payload.get("weather_likelihood_features"))
    return {
        "schema": features.get("schema") or "",
        "source": features.get("source") or payload.get("source") or "",
        "station_id": features.get("station_id") or payload.get("station_id") or "",
        "metar_at": features.get("zuuu_metar_raw_amsc_latest_at") or "",
        "metar_local": features.get("zuuu_metar_raw_amsc_latest_local") or "",
        "metar_temp_c": features.get("zuuu_metar_raw_amsc_latest_temp_c"),
        "metar_dewpoint_c": features.get("zuuu_metar_raw_amsc_latest_dewpoint_c"),
        "metar_raw": features.get("zuuu_metar_raw_amsc_latest_raw") or "",
        "taf_peak_at": features.get("taf_forecast_peak_at") or "",
        "taf_peak_c": features.get("taf_forecast_peak_c"),
        "taf_raw": features.get("taf_forecast_peak_raw") or "",
    }


def _latest_fact(conn: sqlite3.Connection, *, slug: str, event_date: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT h.id, h.fact_as_of, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        LIMIT 1
        """,
        (slug, event_date),
    ).fetchone()
    if row is None:
        return {}
    return {
        "id": int(row["id"]),
        "fact_as_of": str(row["fact_as_of"] or ""),
        "payload": _json_loads(row["fact_json"]),
    }


def _latest_amsc_fact(conn: sqlite3.Connection, *, slug: str, event_date: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, fact_as_of, fact_json
        FROM event_fact_history
        WHERE slug = ?
          AND event_date = ?
          AND (
            composer_version LIKE 'amsc_zuuu_metar_taf%'
            OR json_extract(fact_json, '$.source') = 'amsc_packet_retrieval'
          )
        ORDER BY fact_as_of DESC, id DESC
        LIMIT 1
        """,
        (slug, event_date),
    ).fetchone()
    if row is None:
        return {}
    return {
        "id": int(row["id"]),
        "fact_as_of": str(row["fact_as_of"] or ""),
        "payload": _json_loads(row["fact_json"]),
    }


def _rebuild_plan_with_current(
    plan: Mapping[str, Any],
    *,
    current_fact_id: int | None,
    current_fact_as_of: str = "",
    current_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    incoming_payload = copy.deepcopy(dict(_as_mapping(plan.get("effective_payload"))))
    merge_current_payload = copy.deepcopy(dict(current_payload or {}))
    # AMSC is the new observation source for this write. Keep older posterior and
    # market fields as fill-ins, but do not let stale source fields mask the
    # current METAR/TAF observation bundle.
    for key in (
        "weather_likelihood_features",
        "source_observations",
        "source_observation_ids",
        "source_acquisition_trace",
        "source_latency_trace",
        "source",
        "station_id",
        "source_update_cadence_sec",
        "amsc_observation_bundle_schema",
    ):
        merge_current_payload.pop(key, None)
    trace = _as_mapping(incoming_payload.get("source_acquisition_trace")) or _as_mapping(
        plan.get("source_acquisition_trace")
    )
    rebuilt = build_fact_snapshot_write_plan(
        slug=str(plan.get("slug") or incoming_payload.get("slug") or ""),
        event_date=str(plan.get("event_date") or incoming_payload.get("event_date") or ""),
        run_date=str(plan.get("run_date") or incoming_payload.get("run_date") or ""),
        fact_as_of=str(plan.get("fact_as_of") or incoming_payload.get("fact_as_of") or utc_now_iso()),
        incoming_payload=incoming_payload,
        current_fact_id=current_fact_id,
        current_fact_as_of=current_fact_as_of,
        current_payload=merge_current_payload,
        source_acquisition_trace=trace,
        composer_version=str(plan.get("parser_version") or "amsc_zuuu_metar_taf_apply"),
        write_reason="amsc_zuuu_metar_taf_archive_apply",
        include_payload=True,
        enable_apply_contract=True,
        write_latest=False,
    )
    rebuilt["caller"] = {
        "module": "scripts/apply_amsc_zuuu_fact_snapshot_plan.py",
        "function": "apply_amsc_zuuu_fact_snapshot_plan",
        "lane": "amsc_zuuu_metar_taf_archive_apply",
        "source_update_cadence_sec": 1800,
        "apply_contract_enabled": True,
        "write_latest_requested": True,
        "write_latest_effective": False,
        "write_latest_block_reason": AMSC_LATEST_REPOINT_BLOCK_REASON,
    }
    rebuilt["source_acquisition_latest_repoint_policy"] = {
        "requested": True,
        "effective": False,
        "block_reason": AMSC_LATEST_REPOINT_BLOCK_REASON,
        "boundary": "source_acquisition_may_append_history_but_must_not_repoint_posterior_ssot_latest",
    }
    return rebuilt


def _blocked(reason: str, *, plan: Mapping[str, Any] | None = None, allow_apply: bool = False) -> dict[str, Any]:
    plan_map = _as_mapping(plan)
    return {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "applied": False,
        "blocked": True,
        "skipped": False,
        "block_reason": reason,
        "allow_apply": bool(allow_apply),
        "slug": str(plan_map.get("slug") or ""),
        "event_date": str(plan_map.get("event_date") or ""),
        "guards": {
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_event_fact_summary_cache": False,
            "writes_facts_sqlite": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
            "executes_sql": False,
        },
    }


def _task_queue_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_queue' LIMIT 1"
    ).fetchone()
    return row is not None


def _task_queue_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        return {str(row["name"] or "") for row in conn.execute("PRAGMA table_info(task_queue)").fetchall()}
    except sqlite3.Error:
        return set()


def _existing_posterior_materialization_task_id(
    conn: sqlite3.Connection,
    *,
    source_fact_id: int,
    slug: str,
    event_date: str,
) -> int | None:
    try:
        row = conn.execute(
            """
            SELECT id
            FROM task_queue
            WHERE task_type = ?
              AND status IN ('pending', 'running')
              AND json_extract(payload_json, '$.amsc_source_fact_id') = ?
              AND json_extract(payload_json, '$.slug') = ?
              AND json_extract(payload_json, '$.event_date') = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (POSTERIOR_MATERIALIZATION_TASK_TYPE, int(source_fact_id), slug, event_date),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is None:
        return None
    return int(row["id"] or 0)


def _enqueue_posterior_materialization_task(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    source_fact_id: int,
    source_fact_as_of: str,
    source_observation_ids: list[str],
    v31_multimodel_context_mode: str = DEFAULT_V31_MULTIMODEL_CONTEXT_MODE,
    v31_multimodel_generation_ref: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if source_fact_id <= 0:
        return {
            "schema": "amsc_posterior_materialization_enqueue_v1",
            "ok": False,
            "enqueued": False,
            "state": "source_fact_id_missing",
            "guards": {"writes_task_queue": False, "places_orders": False},
        }
    if not _task_queue_exists(conn):
        return {
            "schema": "amsc_posterior_materialization_enqueue_v1",
            "ok": True,
            "enqueued": False,
            "state": "task_queue_table_missing",
            "na_reason": "phase0_task_queue_schema_not_applied",
            "guards": {"writes_task_queue": False, "places_orders": False},
        }
    columns = _task_queue_columns(conn)
    required = {"task_type", "priority", "payload_json", "max_retries"}
    missing = sorted(required - columns)
    if missing:
        return {
            "schema": "amsc_posterior_materialization_enqueue_v1",
            "ok": True,
            "enqueued": False,
            "state": "task_queue_schema_incomplete",
            "missing_columns": missing,
            "guards": {"writes_task_queue": False, "places_orders": False},
        }
    existing_task_id = _existing_posterior_materialization_task_id(
        conn,
        source_fact_id=source_fact_id,
        slug=slug,
        event_date=event_date,
    )
    if existing_task_id:
        return {
            "schema": "amsc_posterior_materialization_enqueue_v1",
            "ok": True,
            "enqueued": False,
            "state": "already_enqueued",
            "task_id": existing_task_id,
            "guards": {"writes_task_queue": False, "places_orders": False},
        }
    payload = {
        "schema": POSTERIOR_MATERIALIZATION_PAYLOAD_SCHEMA,
        "source": "amsc_zuuu_fact_snapshot_apply",
        "slug": slug,
        "event_date": event_date,
        "amsc_source_fact_id": int(source_fact_id),
        "amsc_source_fact_as_of": str(source_fact_as_of or ""),
        "source_observation_ids": list(source_observation_ids or []),
        "materialization_reason": "amsc_source_history_after_archive_apply",
        "write_latest_policy": "posterior_writer_only",
        # This is part of the queued source-to-posterior contract, not a
        # worker-local environment toggle.  A later cutover therefore cannot
        # accidentally execute an already queued source event under a
        # different multimodel freshness policy.
        # This generation reference freezes the weak-source inputs that were
        # available before the AMSC observation.  It is deliberately an
        # official four-provider bundle, never an Open-Meteo/aggregator cache.
        "official_nwp_context_policy": {
            "schema": V31_MULTIMODEL_CONTEXT_POLICY_SCHEMA,
            "mode": str(v31_multimodel_context_mode or "").strip(),
            "cache_generation_ref": _load_v31_multimodel_generation_ref(v31_multimodel_generation_ref),
        },
        "guards": {
            "source_acquisition_direct_latest_repoint": False,
            "posterior_writer_may_repoint_latest": True,
            "places_orders": False,
        },
    }
    cur = conn.execute(
        """
        INSERT INTO task_queue(task_type, priority, payload_json, max_retries)
        VALUES (?, ?, ?, ?)
        """,
        (
            POSTERIOR_MATERIALIZATION_TASK_TYPE,
            200,
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            1,
        ),
    )
    return {
        "schema": "amsc_posterior_materialization_enqueue_v1",
        "ok": True,
        "enqueued": True,
        "state": "enqueued",
        "task_id": int(cur.lastrowid or 0),
        "task_type": POSTERIOR_MATERIALIZATION_TASK_TYPE,
        "payload": payload,
        "guards": {"writes_task_queue": True, "places_orders": False},
    }


def apply_amsc_zuuu_fact_snapshot_plan(
    plan: Mapping[str, Any],
    *,
    db_path: Path = DEFAULT_DB,
    allow_apply: bool = False,
    confirm: str = "",
    created_at: str = "",
    enqueue_posterior: bool = True,
    v31_multimodel_context_mode: str = DEFAULT_V31_MULTIMODEL_CONTEXT_MODE,
    v31_multimodel_generation_ref: Mapping[str, Any] | None = None,
    archive_write_queue_wait_sec: float | None = None,
    db_timeout_sec: float = 10.0,
    db_busy_timeout_ms: int = 5000,
) -> dict[str, Any]:
    plan_map = _as_mapping(plan)
    if not allow_apply:
        return _blocked("allow_apply_false", plan=plan_map, allow_apply=allow_apply)
    if confirm != CONFIRM_PHRASE:
        return _blocked("confirmation_phrase_missing_or_invalid", plan=plan_map, allow_apply=allow_apply)
    if plan_map.get("schema") != "weather_fact_snapshot_writer_plan_v1":
        return _blocked("invalid_plan_schema", plan=plan_map, allow_apply=allow_apply)
    if not bool(plan_map.get("apply_supported")):
        return _blocked("plan_apply_contract_not_enabled", plan=plan_map, allow_apply=allow_apply)
    incoming_payload = _as_mapping(plan_map.get("effective_payload"))
    if incoming_payload.get("station_id") != "ZUUU" or incoming_payload.get("source") != "amsc_packet_retrieval":
        return _blocked("not_amsc_zuuu_plan", plan=plan_map, allow_apply=allow_apply)
    if not db_path.exists():
        return _blocked("archive_db_missing", plan=plan_map, allow_apply=allow_apply)

    archive_write_budget = {
        "queue_wait_sec": archive_write_queue_wait_sec,
        "db_timeout_sec": max(0.0, float(db_timeout_sec)),
        "db_busy_timeout_ms": max(0, int(db_busy_timeout_ms)),
    }

    with connect_archive_db_rw(
        db_path,
        owner="amsc_zuuu_fact_snapshot_apply",
        timeout_sec=max(0.0, float(db_timeout_sec)),
        busy_timeout_ms=max(0, int(db_busy_timeout_ms)),
        queue_wait_sec=(
            max(0.0, float(archive_write_queue_wait_sec))
            if archive_write_queue_wait_sec is not None
            else None
        ),
        row_factory=True,
    ) as conn:
        slug = str(plan_map.get("slug") or incoming_payload.get("slug") or "")
        event_date = str(plan_map.get("event_date") or incoming_payload.get("event_date") or "")
        current = _latest_fact(conn, slug=slug, event_date=event_date)
        latest_amsc = _latest_amsc_fact(conn, slug=slug, event_date=event_date)
        current_payload = _as_mapping(current.get("payload"))
        duplicate_payload = _as_mapping(latest_amsc.get("payload")) or current_payload
        if duplicate_payload and _amsc_signature(duplicate_payload) == _amsc_signature(incoming_payload):
            # A source fact may be committed before its already-frozen weak
            # forecast generation is attached to a posterior task.  Retrying
            # the same physical AMSC observation must therefore be able to
            # enqueue the *existing* source-fact ID later.  Do not require a
            # second fact write (or manufacture a new source arrival) merely
            # to recover that independent hand-off.
            posterior_enqueue = {
                "schema": "amsc_posterior_materialization_enqueue_v1",
                "ok": True,
                "enqueued": False,
                "state": "disabled",
                "guards": {"writes_task_queue": False, "places_orders": False},
            }
            source_fact_id = int(latest_amsc.get("id") or 0)
            if enqueue_posterior:
                posterior_enqueue = _enqueue_posterior_materialization_task(
                    conn,
                    slug=slug,
                    event_date=event_date,
                    source_fact_id=source_fact_id,
                    source_fact_as_of=str(latest_amsc.get("fact_as_of") or ""),
                    source_observation_ids=list(plan_map.get("source_observation_ids") or []),
                    v31_multimodel_context_mode=v31_multimodel_context_mode,
                    v31_multimodel_generation_ref=v31_multimodel_generation_ref,
                )
                if posterior_enqueue.get("enqueued"):
                    conn.commit()
            return {
                "schema": RESULT_SCHEMA,
                "ok": True,
                "applied": False,
                "blocked": False,
                "skipped": True,
                "skip_reason": "duplicate_amsc_observation_signature",
                "allow_apply": True,
                "slug": slug,
                "event_date": event_date,
                "current_fact_id": current.get("id"),
                "current_fact_as_of": current.get("fact_as_of") or "",
                "duplicate_amsc_fact_id": latest_amsc.get("id"),
                "duplicate_amsc_fact_as_of": latest_amsc.get("fact_as_of") or "",
                "fact_id": source_fact_id or None,
                "incoming_signature": _amsc_signature(incoming_payload),
                "archive_write_budget": archive_write_budget,
                "posterior_materialization_enqueue": posterior_enqueue,
                "guards": {
                    "writes_archive_db": False,
                    "writes_event_fact_history": False,
                    "writes_event_fact_latest": False,
                    "writes_event_fact_summary_cache": False,
                    "writes_facts_sqlite": False,
                    "places_orders": False,
                    "restarts_launchd": False,
                    "historical_backfill": False,
                    "changes_parameters": False,
                    "executes_sql": False,
                    "writes_task_queue": bool(posterior_enqueue.get("enqueued")),
                },
            }

        rebuilt_plan = _rebuild_plan_with_current(
            plan_map,
            current_fact_id=current.get("id") if current else None,
            current_fact_as_of=str(current.get("fact_as_of") or "") if current else "",
            current_payload=current_payload,
        )
        # Keep the fresh AMSC fact and its immutable posterior work item in
        # one SQLite transaction.  Previously the fact was committed first,
        # then a second generic-writer transaction enqueued the task.  That
        # created an avoidable source→posterior gap whenever another writer
        # owned the gate.  A missing weak-context reference still commits the
        # hard fact alone; it never suppresses or rolls back the observation.
        writer_result = apply_fact_snapshot_write_plan(
            conn,
            rebuilt_plan,
            allow_apply=True,
            commit=False,
            created_at=str(created_at or rebuilt_plan.get("fact_as_of") or utc_now_iso()),
        )
        summary = compact_fact_snapshot_writer_apply_result_for_lineage(writer_result)
        posterior_enqueue = {
            "schema": "amsc_posterior_materialization_enqueue_v1",
            "ok": True,
            "enqueued": False,
            "state": "disabled",
            "guards": {"writes_task_queue": False, "places_orders": False},
        }
        if enqueue_posterior and bool(writer_result.get("ok")) and bool(writer_result.get("applied")):
            try:
                posterior_enqueue = _enqueue_posterior_materialization_task(
                    conn,
                    slug=slug,
                    event_date=event_date,
                    source_fact_id=int(writer_result.get("fact_id") or 0),
                    source_fact_as_of=str(rebuilt_plan.get("fact_as_of") or ""),
                    source_observation_ids=list(rebuilt_plan.get("source_observation_ids") or []),
                    v31_multimodel_context_mode=v31_multimodel_context_mode,
                    v31_multimodel_generation_ref=v31_multimodel_generation_ref,
                )
            except sqlite3.Error as exc:
                # The AMSC fact is the authoritative observation.  A task
                # outbox/schema failure is recorded for recovery but cannot
                # erase the committed hard fact.
                posterior_enqueue = {
                    "schema": "amsc_posterior_materialization_enqueue_v1",
                    "ok": False,
                    "enqueued": False,
                    "state": "enqueue_failed_fact_committed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "guards": {"writes_task_queue": False, "places_orders": False},
                }
        if bool(writer_result.get("ok")) and bool(writer_result.get("applied")):
            conn.commit()
            # `apply_fact_snapshot_write_plan` intentionally does not write
            # the just-created fact again after commit.  The timestamp belongs
            # to the hand-off result/attestation, not a second fact mutation.
            writer_result["commit"] = True
            writer_result["committed_at"] = utc_now_iso()
        return {
            "schema": RESULT_SCHEMA,
            "ok": bool(writer_result.get("ok")),
            "applied": bool(writer_result.get("applied")),
            "blocked": bool(writer_result.get("blocked")),
            "skipped": False,
            "block_reason": str(writer_result.get("block_reason") or ""),
            "allow_apply": True,
            "slug": slug,
            "event_date": event_date,
            "db_path": str(db_path),
            "current_fact_id_before": current.get("id") if current else None,
            "fact_id": writer_result.get("fact_id"),
            "latest_repointed": bool(writer_result.get("latest_repointed")),
            "summary_cache_written": bool(writer_result.get("summary_cache_written")),
            "archive_write_budget": archive_write_budget,
            "source_acquisition_latest_repoint_policy": dict(
                _as_mapping(rebuilt_plan.get("source_acquisition_latest_repoint_policy"))
            ),
            "posterior_materialization_enqueue": posterior_enqueue,
            "transaction_boundary": (
                "amsc_source_fact_and_posterior_task_atomic"
                if bool(posterior_enqueue.get("enqueued"))
                else "amsc_source_fact_only_atomic"
            ),
            "writer_apply_result": writer_result,
            "writer_apply_result_summary": summary,
            "guards": dict(_as_mapping(writer_result.get("guards"))),
        }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply a ZUUU AMSC writer plan to archive.db.")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--result-json", type=Path, default=None)
    parser.add_argument("--no-enqueue-posterior", action="store_true")
    parser.add_argument(
        "--archive-write-queue-wait-sec",
        type=float,
        default=None,
        help="optional bounded archive writer-queue wait; source fast lane supplies a sub-second value",
    )
    parser.add_argument(
        "--db-timeout-sec",
        type=float,
        default=10.0,
        help="SQLite connection/write timeout; source fast lane supplies a sub-second value",
    )
    parser.add_argument(
        "--db-busy-timeout-ms",
        type=int,
        default=5000,
        help="SQLite busy timeout; source fast lane supplies a bounded value",
    )
    parser.add_argument(
        "--fast-lane",
        action="store_true",
        help="emit an explicit deferred result on archive contention instead of hiding it behind retries",
    )
    parser.add_argument(
        "--posterior-official-nwp-context-mode",
        default=DEFAULT_V31_MULTIMODEL_CONTEXT_MODE,
        help=(
            "only prewarmed_cache_only_v1 is accepted; it must bind one immutable "
            "four-provider official-NWP generation or posterior materialization fails closed"
        ),
    )
    parser.add_argument(
        "--posterior-official-nwp-generation-ref-json",
        default="",
        help="immutable four-provider official-NWP generation reference serialized by the source poller",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    generation_ref = _json_loads(args.posterior_official_nwp_generation_ref_json)
    try:
        result = apply_amsc_zuuu_fact_snapshot_plan(
            plan,
            db_path=args.db,
            allow_apply=bool(args.apply),
            confirm=args.confirm,
            enqueue_posterior=not bool(args.no_enqueue_posterior),
            v31_multimodel_context_mode=args.posterior_official_nwp_context_mode,
            v31_multimodel_generation_ref=generation_ref,
            archive_write_queue_wait_sec=args.archive_write_queue_wait_sec,
            db_timeout_sec=args.db_timeout_sec,
            db_busy_timeout_ms=args.db_busy_timeout_ms,
        )
    except (TimeoutError, sqlite3.Error) as exc:
        fast_lane = bool(args.fast_lane)
        result = {
            "schema": RESULT_SCHEMA,
            "ok": False,
            "applied": False,
            "blocked": True,
            "skipped": False,
            "block_reason": "archive_db_fast_lane_deferred" if fast_lane else "archive_db_write_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "slug": str(plan.get("slug") or ""),
            "event_date": str(plan.get("event_date") or ""),
            "archive_write_budget": {
                "mode": "source_fast_lane" if fast_lane else "standard",
                "queue_wait_sec": args.archive_write_queue_wait_sec,
                "db_timeout_sec": args.db_timeout_sec,
                "db_busy_timeout_ms": args.db_busy_timeout_ms,
            },
            "guards": {
                "writes_archive_db": False,
                "writes_event_fact_history": False,
                "writes_event_fact_latest": False,
                "writes_event_fact_summary_cache": False,
                "writes_facts_sqlite": False,
                "writes_task_queue": False,
                "places_orders": False,
                "restarts_launchd": False,
                "historical_backfill": False,
                "changes_parameters": False,
                "executes_sql": False,
            },
        }
    if args.result_json:
        _write_json_atomic(args.result_json, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(result.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
