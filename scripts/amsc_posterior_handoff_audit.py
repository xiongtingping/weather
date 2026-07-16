#!/usr/bin/env python3
"""Read-only audit of AMSC Transaction A -> A.1 posterior hand-off lineage.

Transaction A is the append-only AMSC source fact.  Transaction A.1 is a
separate, idempotent task hand-off bound to that exact fact and to one frozen
official-NWP generation.  A missing A.1 must be observable, but never causes
this audit to write, requeue, or reinterpret an older AMSC observation.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
SOURCE_SCHEMA = "amsc_zuuu_metar_taf_observation_bundle_v1"
TASK_SCHEMA = "amsc_posterior_materialization_task_payload_v1"
POLICY_SCHEMA = "official_nwp_context_policy_v1"
TASK_TYPE = "amsc_posterior_materialize"
AUDIT_SCHEMA = "amsc_posterior_handoff_audit_v1"


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_task_queue": False,
        "requeues_tasks": False,
        "places_orders": False,
        "restarts_launchd": False,
    }


def _source_rows(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT id, slug, event_date, fact_as_of, created_at, source_event_id, fact_snapshot_id, fact_json
        FROM event_fact_history
        ORDER BY id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [
        row
        for row in rows
        if _mapping(row["fact_json"]).get("amsc_observation_bundle_schema") == SOURCE_SCHEMA
    ]


def _task_for_source(conn: sqlite3.Connection, source_fact_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, task_type, status, created_at, started_at, completed_at, error, payload_json
        FROM task_queue
        WHERE task_type = ?
          AND json_extract(payload_json, '$.amsc_source_fact_id') = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (TASK_TYPE, int(source_fact_id)),
    ).fetchone()


def _audit_source(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    payload = _mapping(row["fact_json"])
    features = _mapping(payload.get("weather_likelihood_features"))
    fact_id = int(row["id"])
    task = _task_for_source(conn, fact_id)
    result: dict[str, Any] = {
        "source_fact_id": fact_id,
        "source_fact_as_of": str(row["fact_as_of"] or ""),
        "source_event_id": str(row["source_event_id"] or ""),
        "fact_snapshot_id": str(row["fact_snapshot_id"] or ""),
        "latest_metar_at": str(features.get("zuuu_metar_raw_amsc_latest_at") or ""),
        "transaction_a": "committed",
        "transaction_a1": "not_enqueued",
        "ok": True,
        "warnings": [],
    }
    if task is None:
        result["warnings"].append("posterior_handoff_not_enqueued")
        return result

    task_payload = _mapping(task["payload_json"])
    policy = _mapping(task_payload.get("official_nwp_context_policy"))
    policy_ref = _mapping(policy.get("cache_generation_ref"))
    blockers: list[str] = []
    if task_payload.get("schema") != TASK_SCHEMA:
        blockers.append("task_payload_schema_invalid")
    if int(task_payload.get("amsc_source_fact_id") or 0) != fact_id:
        blockers.append("task_source_fact_binding_invalid")
    if policy.get("schema") != POLICY_SCHEMA:
        blockers.append("official_nwp_policy_schema_invalid")
    if not str(policy.get("mode") or "").strip():
        blockers.append("official_nwp_policy_mode_missing")
    if not policy_ref:
        blockers.append("official_nwp_generation_reference_missing")
    result.update(
        {
            "transaction_a1": "enqueued",
            "task": {
                "id": int(task["id"]),
                "status": str(task["status"] or ""),
                "created_at": str(task["created_at"] or ""),
                "started_at": str(task["started_at"] or ""),
                "completed_at": str(task["completed_at"] or ""),
                "error": str(task["error"] or ""),
                "payload_schema": task_payload.get("schema"),
                "official_nwp_policy_schema": policy.get("schema"),
                "official_nwp_policy_mode": policy.get("mode"),
                "official_nwp_generation_id": policy_ref.get("generation_id") or policy_ref.get("bundle_id") or "",
            },
            "blockers": blockers,
            "ok": not blockers,
        }
    )
    return result


def audit_amsc_posterior_handoff(
    conn: sqlite3.Connection,
    *,
    limit: int = 200,
    latest_only: bool = False,
    require_enqueued: bool = False,
) -> dict[str, Any]:
    rows = _source_rows(conn, limit=limit)
    if latest_only:
        rows = rows[:1]
    sources = [_audit_source(conn, row) for row in rows]
    if require_enqueued:
        for source in sources:
            if source["transaction_a1"] == "not_enqueued":
                source["ok"] = False
                source.setdefault("blockers", []).append("posterior_handoff_required_but_not_enqueued")
    malformed = [row["source_fact_id"] for row in sources if not row["ok"]]
    return {
        "schema": AUDIT_SCHEMA,
        "ok": not malformed,
        "state": "no_amsc_source_fact_found" if not sources else "lineage_valid",
        "source_fact_count": len(sources),
        "invalid_source_fact_ids": malformed,
        "sources": sources,
        "guards": _guards(),
        "semantics": {
            "transaction_a": "AMSC source fact is durable before weak-source inspection",
            "transaction_a1": "only a task pinned to the same source fact and frozen official-NWP generation may materialize posterior",
            "no_historical_requeue": "not_enqueued is reported only; this audit never requeues or backfills",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--latest-only", action="store_true")
    parser.add_argument(
        "--require-enqueued",
        action="store_true",
        help="fail only when a selected committed AMSC fact has no Transaction A.1 task; never requeues it",
    )
    args = parser.parse_args()
    db = Path(args.db).expanduser().resolve()
    if not db.exists():
        print(json.dumps({"schema": AUDIT_SCHEMA, "ok": False, "state": "archive_db_missing", "guards": _guards()}))
        return 2
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        result = audit_amsc_posterior_handoff(
            conn,
            limit=args.limit,
            latest_only=args.latest_only,
            require_enqueued=args.require_enqueued,
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
