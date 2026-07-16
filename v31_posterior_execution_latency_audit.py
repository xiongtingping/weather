"""Read-only evidence audit for the current v3.1 source-to-decision path.

This is intentionally not a P10 success report.  It reconstructs what the
legacy AMSC -> materialization task -> poller path actually recorded, so its
task-queue/subprocess boundary and missing CLOB POST evidence remain visible
instead of being relabelled as a three-second fast lane.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "v31_posterior_execution_latency_audit_v2"
V31_COMPOSER_SCHEMA = "sequential_remaining_warming_bayes_runtime_v31"
POSTERIOR_MATERIALIZATION_ATTESTATION_TABLE = "posterior_materialization_commit_attestations"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _posterior_schema(payload: Mapping[str, Any]) -> str:
    nested = _mapping(payload.get("posterior_ssot_fact"))
    return str(payload.get("posterior_composer_schema") or nested.get("posterior_composer_schema") or "").strip()


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ms(later: Any, earlier: Any) -> float | None:
    later_dt = _parse_dt(later)
    earlier_dt = _parse_dt(earlier)
    if later_dt is None or earlier_dt is None:
        return None
    return round((later_dt - earlier_dt).total_seconds() * 1000.0, 3)


def _nonnegative_ms(value: Any) -> float | None:
    """Return a persisted duration only when it is a finite nonnegative ms value."""

    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return round(parsed, 3) if math.isfinite(parsed) and parsed >= 0 else None


def _materialization_segment_timing(
    *,
    source_to_posterior_ms: float | None,
    reported_source_to_apply_start_ms: Any,
) -> dict[str, Any]:
    """Expose a derivation, never an inferred causal attribution.

    The legacy payload may record a source-to-apply-start duration while the
    fact commit happens much later.  Showing the residual prevents a short
    queue-wait field from being misread as the end-to-end source delay.
    """

    apply_start_ms = _nonnegative_ms(reported_source_to_apply_start_ms)
    if apply_start_ms is None:
        return {
            "state": "not_reported",
            "reported_source_to_posterior_apply_start_ms": None,
            "derived_apply_start_to_posterior_commit_ms": None,
        }
    if source_to_posterior_ms is None:
        return {
            "state": "source_to_commit_unavailable",
            "reported_source_to_posterior_apply_start_ms": apply_start_ms,
            "derived_apply_start_to_posterior_commit_ms": None,
        }
    if apply_start_ms > source_to_posterior_ms:
        return {
            "state": "inconsistent_reported_apply_start_after_commit",
            "reported_source_to_posterior_apply_start_ms": apply_start_ms,
            "source_to_posterior_commit_ms": source_to_posterior_ms,
            "derived_apply_start_to_posterior_commit_ms": None,
        }
    return {
        "state": "derived_from_persisted_durations",
        "reported_source_to_posterior_apply_start_ms": apply_start_ms,
        "source_to_posterior_commit_ms": source_to_posterior_ms,
        "derived_apply_start_to_posterior_commit_ms": round(source_to_posterior_ms - apply_start_ms, 3),
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
        ).fetchone()
    )


def _task_queue_lifecycle(conn: sqlite3.Connection, task_id: Any) -> dict[str, Any]:
    """Return queue timestamps as diagnostics, never as a posterior commit."""

    try:
        parsed_id = int(task_id)
    except (TypeError, ValueError):
        return {"state": "unavailable", "reason": "task_queue_id_missing_or_invalid"}
    if parsed_id <= 0 or not _table_exists(conn, "task_queue"):
        return {"state": "unavailable", "reason": "task_queue_row_unavailable", "task_queue_id": parsed_id}
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(task_queue)").fetchall()}
    required = {"id", "status", "created_at", "started_at", "completed_at"}
    if not required.issubset(columns):
        return {"state": "unavailable", "reason": "task_queue_schema_incomplete", "task_queue_id": parsed_id}
    row = conn.execute(
        "SELECT id, status, created_at, started_at, completed_at FROM task_queue WHERE id=? LIMIT 1",
        (parsed_id,),
    ).fetchone()
    if row is None:
        return {"state": "unavailable", "reason": "task_queue_row_missing", "task_queue_id": parsed_id}
    return {
        "state": "observed",
        "task_queue_id": parsed_id,
        "status": str(row["status"] or ""),
        "created_at": str(row["created_at"] or ""),
        "started_at": str(row["started_at"] or ""),
        "completed_at": str(row["completed_at"] or ""),
        "boundary": "queue_completion_is_not_posterior_commit",
    }


def _posterior_commit_attestation(
    *,
    conn: sqlite3.Connection,
    fact_id: int,
    timing: Mapping[str, Any],
) -> dict[str, Any]:
    """Require an explicit task-to-fact commit binding before publishing T3.

    UI/event rematerializations can reuse a posterior run much later.  Their
    ``created_at`` is not evidence that the original AMSC materializer
    committed at that time, so the audit must never infer T3 from it.
    """

    if _table_exists(conn, POSTERIOR_MATERIALIZATION_ATTESTATION_TABLE):
        row = conn.execute(
            f"""
            SELECT materialized_fact_id, task_queue_id, source_fact_id,
                   posterior_committed_at, fact_snapshot_id, posterior_run_id,
                   timing_sha256
            FROM {POSTERIOR_MATERIALIZATION_ATTESTATION_TABLE}
            WHERE materialized_fact_id=?
            LIMIT 1
            """,
            (int(fact_id),),
        ).fetchone()
        if row is not None:
            committed_at = str(row["posterior_committed_at"] or "").strip()
            if _parse_dt(committed_at) is not None:
                return {
                    "state": "present",
                    "reason": "append_only_materializer_task_to_fact_commit_attestation",
                    "posterior_committed_at": committed_at,
                    "bound_fact_id": int(row["materialized_fact_id"] or 0),
                    "task_queue_id": row["task_queue_id"],
                    "source_fact_id": row["source_fact_id"],
                    "fact_snapshot_id": str(row["fact_snapshot_id"] or ""),
                    "posterior_run_id": str(row["posterior_run_id"] or ""),
                    "timing_sha256": str(row["timing_sha256"] or ""),
                }
            return {
                "state": "missing",
                "reason": "append_only_materializer_commit_attestation_invalid",
                "posterior_committed_at": "",
                "bound_fact_id": int(fact_id),
            }
    completed_at = str(timing.get("posterior_apply_completed_at") or "").strip()
    bound_fact_id_raw = timing.get("posterior_commit_fact_id") or timing.get("materialized_fact_id")
    try:
        bound_fact_id = int(bound_fact_id_raw)
    except (TypeError, ValueError):
        bound_fact_id = 0
    if not completed_at:
        return {
            "state": "missing",
            "reason": "materializer_posterior_apply_completed_at_missing",
            "posterior_committed_at": "",
            "bound_fact_id": bound_fact_id or None,
        }
    if bound_fact_id != fact_id:
        return {
            "state": "missing",
            "reason": "materializer_task_to_fact_commit_binding_missing",
            "posterior_committed_at": "",
            "bound_fact_id": bound_fact_id or None,
        }
    if _parse_dt(completed_at) is None:
        return {
            "state": "missing",
            "reason": "materializer_posterior_apply_completed_at_invalid",
            "posterior_committed_at": "",
            "bound_fact_id": bound_fact_id,
        }
    return {
        "state": "present",
        "reason": "explicit_materializer_task_to_fact_commit_binding",
        "posterior_committed_at": completed_at,
        "bound_fact_id": bound_fact_id,
    }


def _fact_rows(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    if not _table_exists(conn, "event_fact_history"):
        return []
    try:
        return list(
            conn.execute(
                """
                SELECT id, slug, event_date, fact_as_of, created_at, fact_json
                FROM event_fact_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        )
    except sqlite3.Error:
        return []


def _latest_fact_rows(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    if not _table_exists(conn, "event_fact_latest"):
        return _fact_rows(conn, limit=limit)
    try:
        return list(
            conn.execute(
                """
                SELECT h.id,h.slug,h.event_date,h.fact_as_of,h.created_at,h.fact_json
                FROM event_fact_latest l
                JOIN event_fact_history h ON h.id=l.fact_id
                ORDER BY h.id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        )
    except sqlite3.Error:
        return _fact_rows(conn, limit=limit)


def _latest_pointer_integrity(conn: sqlite3.Connection) -> dict[str, Any]:
    """Verify that a current v3.1 posterior pointer is its attested fact.

    A later UI/source refresh can carry a copied posterior run while repointing
    ``event_fact_latest``.  That row is neither a new AMSC materialization nor
    evidence of its Transaction-B equivalent, so it must never be considered
    the current executable posterior.
    """

    if not _table_exists(conn, "event_fact_latest"):
        return {
            "state": "not_applicable_latest_pointer_table_missing",
            "ok": True,
            "violations": [],
            "checked": 0,
        }
    try:
        rows = conn.execute(
            """
            SELECT h.id,h.slug,h.event_date,h.fact_json
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id=l.fact_id
            ORDER BY h.id DESC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        return {
            "state": "latest_pointer_query_failed",
            "ok": False,
            "violations": [f"latest_pointer_query_failed:{type(exc).__name__}"],
            "checked": 0,
        }

    checks: list[dict[str, Any]] = []
    violations: list[str] = []
    has_attestations = _table_exists(conn, POSTERIOR_MATERIALIZATION_ATTESTATION_TABLE)
    for row in rows:
        payload = _json_mapping(row["fact_json"])
        timing = _mapping(
            payload.get("amsc_posterior_materialization_timing")
            or payload.get("source_to_posterior_timing")
        )
        if _posterior_schema(payload) != V31_COMPOSER_SCHEMA:
            continue
        fact_id = int(row["id"] or 0)
        fact_snapshot_id = str(payload.get("fact_snapshot_id") or "")
        posterior_run_id = str(payload.get("posterior_run_id") or "")
        attestation = _posterior_commit_attestation(conn=conn, fact_id=fact_id, timing=timing)
        bound = bool(
            has_attestations
            and attestation.get("state") == "present"
            and str(attestation.get("fact_snapshot_id") or "") == fact_snapshot_id
            and str(attestation.get("posterior_run_id") or "") == posterior_run_id
        )
        reason = "attested_current_v31_posterior" if bound else "latest_v31_posterior_not_bound_to_materializer_attestation"
        checks.append(
            {
                "fact_id": fact_id,
                "slug": str(row["slug"] or ""),
                "event_date": str(row["event_date"] or ""),
                "fact_snapshot_id": fact_snapshot_id,
                "posterior_run_id": posterior_run_id,
                "attested": bound,
                "reason": reason,
            }
        )
        if not bound:
            violations.append(f"{reason}:fact_id={fact_id}")
    active_event_date = max((str(item.get("event_date") or "") for item in checks), default="")
    runtime_violations = [
        violation
        for violation, check in zip(violations, (item for item in checks if not item.get("attested")))
        if str(check.get("event_date") or "") == active_event_date
    ]
    return {
        "state": (
            "verified" if not violations else
            "historical_violation_only" if not runtime_violations else
            "violation"
        ),
        "ok": not runtime_violations,
        "checked": len(checks),
        "active_event_date": active_event_date,
        "checks": checks,
        "violations": violations,
        "runtime_violations": runtime_violations,
    }


def _decision_rows(
    conn: sqlite3.Connection,
    *,
    fact_id: int,
    slug: str,
    event_date: str,
) -> list[sqlite3.Row]:
    if not _table_exists(conn, "auto_trade_decisions"):
        return []
    try:
        return list(
            conn.execute(
                """
                SELECT id, created_at, action, decided_state, will_order,
                       reason_codes_json, details_json
                FROM auto_trade_decisions
                WHERE slug = ?
                  AND event_date = ?
                ORDER BY created_at ASC, id ASC
                """,
                (str(slug), str(event_date)),
            ).fetchall()
        )
    except sqlite3.Error:
        return []


def _first_decision(rows: Sequence[sqlite3.Row], *, fact_id: int) -> dict[str, Any]:
    best: dict[str, Any] = {}
    for row in rows:
        details = _json_mapping(row["details_json"])
        try:
            decision_fact_id = int(details.get("fact_id") or 0)
        except (TypeError, ValueError):
            decision_fact_id = 0
        if decision_fact_id != int(fact_id):
            continue
        started_at = str(details.get("decision_started_at") or "").strip()
        created_at = str(row["created_at"] or "").strip()
        candidate = {
            "decision_id": int(row["id"] or 0),
            "decision_started_at": started_at,
            "decision_written_at": created_at,
            "action": str(row["action"] or ""),
            "decided_state": str(row["decided_state"] or ""),
            "will_order": bool(row["will_order"]),
            "reason_codes": _reason_codes(row["reason_codes_json"]),
            "details": details,
        }
        # Some older diagnostics have no start timestamp.  Prefer an actual
        # start over a mere decision row, otherwise retain the first write.
        if not best or (not best.get("decision_started_at") and started_at):
            best = candidate
            if started_at:
                break
    return best


def _reason_codes(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = []
    return [str(item) for item in parsed if str(item or "").strip()] if isinstance(parsed, list) else []


def _observation_t1_gate_status(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Expose whether the persisted runtime contract actually gates on T1.

    Source code and an archive decision can legitimately be at different
    deployment revisions.  The audit must expose that difference instead of
    inferring T1 protection from a local code checkout.
    """

    if not contract:
        return {
            "state": "missing",
            "reason": "runtime_execution_contract_missing",
            "observation_trigger_at": "",
            "observation_trigger_time_source": "",
        }
    trigger_at = str(contract.get("observation_trigger_at") or "").strip()
    trigger_source = str(contract.get("observation_trigger_time_source") or "").strip()
    hard_deadline = str(contract.get("observation_to_order_hard_deadline_at") or "").strip()
    if not trigger_at or not trigger_source or not hard_deadline:
        return {
            "state": "missing",
            "reason": "runtime_contract_missing_observation_t1_gate",
            "observation_trigger_at": trigger_at,
            "observation_trigger_time_source": trigger_source,
            "observation_to_order_hard_deadline_at": hard_deadline,
        }
    return {
        "state": "present",
        "reason": "runtime_contract_carries_observation_t1_gate",
        "observation_trigger_at": trigger_at,
        "observation_trigger_time_source": trigger_source,
        "observation_to_order_hard_deadline_at": hard_deadline,
    }


def _live_clob_t5_evidence(
    conn: sqlite3.Connection,
    decision_details: Mapping[str, Any],
) -> dict[str, Any]:
    """Read only gateway-attested T5; local decision/ack timestamps are invalid."""

    signal_id = str(decision_details.get("signal_id") or "").strip()
    if not signal_id:
        nested = _mapping(decision_details.get("signal_to_order_timing"))
        signal_id = str(nested.get("signal_id") or "").strip()
    if not signal_id or not _table_exists(conn, "signal_to_order_timing"):
        return {
            "state": "missing",
            "reason": "signal_to_order_timing_row_unavailable",
            "signal_id": signal_id,
            "order_submitted_at": "",
        }
    row = conn.execute(
        "SELECT order_submitted_at, timing_json FROM signal_to_order_timing WHERE signal_id=? LIMIT 1",
        (signal_id,),
    ).fetchone()
    if row is None:
        return {
            "state": "missing",
            "reason": "signal_to_order_timing_row_missing",
            "signal_id": signal_id,
            "order_submitted_at": "",
        }
    timing = _json_mapping(row["timing_json"])
    submitted_at = str(row["order_submitted_at"] or timing.get("order_submitted_at") or "").strip()
    exchange_started_at = str(timing.get("exchange_post_started_at") or "").strip()
    quality = str(timing.get("order_submission_time_quality") or "").strip()
    attested = bool(timing.get("exchange_post_started"))
    valid = bool(
        submitted_at
        and _parse_dt(submitted_at) is not None
        and submitted_at == exchange_started_at
        and attested
        and quality == "exchange_post_started_at"
    )
    return {
        "state": "verified" if valid else "unverified",
        "reason": (
            "gateway_exchange_post_started_at_attested"
            if valid
            else "gateway_exchange_post_t5_attestation_missing_or_inconsistent"
        ),
        "signal_id": signal_id,
        "order_submitted_at": submitted_at,
        "exchange_post_started": attested,
        "exchange_post_started_at": exchange_started_at,
        "order_submission_time_quality": quality,
    }


def _record_for_fact(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any] | None:
    payload = _json_mapping(row["fact_json"])
    if _posterior_schema(payload) != V31_COMPOSER_SCHEMA:
        return None
    timing = _mapping(
        payload.get("amsc_posterior_materialization_timing")
        or payload.get("source_to_posterior_timing")
    )
    timing_available = timing.get("schema") == "source_to_posterior_timing_v1"
    source_received_at = str(
        timing.get("latest_source_response_completed_at")
        or timing.get("latest_source_fact_as_of")
        or ""
    ).strip()
    fact_id = int(row["id"] or 0)
    commit_attestation = _posterior_commit_attestation(conn=conn, fact_id=fact_id, timing=timing)
    posterior_committed_at = str(commit_attestation.get("posterior_committed_at") or "")
    decision = (
        _first_decision(
            _decision_rows(
                conn,
                fact_id=fact_id,
                slug=str(row["slug"] or ""),
                event_date=str(row["event_date"] or ""),
            ),
            fact_id=fact_id,
        )
        if commit_attestation.get("state") == "present"
        else {}
    )
    contract = _mapping(decision.get("details", {}).get("sequential_bayes_execution_contract"))
    observation_t1_gate = _observation_t1_gate_status(contract)
    live_clob_t5 = _live_clob_t5_evidence(conn, _mapping(decision.get("details")))
    decision_started_at = str(decision.get("decision_started_at") or "").strip()
    decision_written_at = str(decision.get("decision_written_at") or "").strip()
    source_to_posterior_ms = _ms(posterior_committed_at, source_received_at)
    posterior_to_decision_start_ms = _ms(decision_started_at, posterior_committed_at)
    source_to_decision_start_ms = _ms(decision_started_at, source_received_at)
    source_to_decision_write_ms = _ms(decision_written_at, source_received_at)
    observation_t1 = str(observation_t1_gate.get("observation_trigger_at") or "")
    t5_at = str(live_clob_t5.get("order_submitted_at") or "")
    t1_to_t5_ms = _ms(t5_at, observation_t1)
    materialization_segments = _materialization_segment_timing(
        source_to_posterior_ms=source_to_posterior_ms,
        reported_source_to_apply_start_ms=timing.get("source_response_to_posterior_apply_start_ms"),
    )
    execution_blockers = [str(value) for value in contract.get("blockers") or [] if str(value)]
    missing: list[str] = []
    if not source_received_at:
        missing.append("source_received_at")
    if not posterior_committed_at:
        missing.append("posterior_commit_attestation")
    if not decision_started_at:
        missing.append("decision_started_at")
    if live_clob_t5.get("state") != "verified":
        missing.append("gateway_attested_exchange_post_t5")
    legacy_queue_path = bool(timing.get("task_queue_id"))
    p10_eligible = bool(
        not missing
        and observation_t1_gate.get("state") == "present"
        and live_clob_t5.get("state") == "verified"
        and t1_to_t5_ms is not None
        and not legacy_queue_path
    )
    return {
        "fact_id": fact_id,
        "slug": str(row["slug"] or ""),
        "event_date": str(row["event_date"] or ""),
        "fact_as_of": str(row["fact_as_of"] or ""),
        "fact_snapshot_id": str(payload.get("fact_snapshot_id") or ""),
        "posterior_run_id": str(payload.get("posterior_run_id") or ""),
        "source_received_at": source_received_at,
        "posterior_committed_at": posterior_committed_at,
        "decision_started_at": decision_started_at,
        "decision_written_at": decision_written_at,
        "timings_ms": {
            "source_to_posterior_commit": source_to_posterior_ms,
            "posterior_commit_to_decision_start": posterior_to_decision_start_ms,
            "source_to_decision_start": source_to_decision_start_ms,
            "source_to_decision_write": source_to_decision_write_ms,
            "source_to_live_clob_post": _ms(t5_at, source_received_at),
            "observation_t1_to_live_clob_post": t1_to_t5_ms,
        },
        "materialization": {
            "timing_available": timing_available,
            "timing_na_reason": "source_to_posterior_timing_missing_from_current_posterior_fact" if not timing_available else "",
            "task_queue_id": timing.get("task_queue_id"),
            "task_queue_wait_ms": timing.get("task_queue_wait_ms"),
            "source_response_to_posterior_apply_start_ms": timing.get(
                "source_response_to_posterior_apply_start_ms"
            ),
            "legacy_task_queue_path": legacy_queue_path,
            "segment_timing": materialization_segments,
            "task_queue_lifecycle": _task_queue_lifecycle(conn, timing.get("task_queue_id")),
        },
        "posterior_commit_attestation": commit_attestation,
        "decision": {
            key: value
            for key, value in decision.items()
            if key != "details"
        } if decision else {},
        "execution_contract": contract,
        "execution_contract_observation_t1_gate": observation_t1_gate,
        "live_clob_t5_evidence": live_clob_t5,
        "execution_blockers": execution_blockers,
        "evidence_state": "complete_to_decision" if not missing else "partial",
        "missing_timestamps": missing,
        "p10_live_slo": {
            "eligible": p10_eligible,
            "reason": (
                "materializer_task_to_fact_commit_binding_missing"
                if commit_attestation.get("state") != "present"
                else "runtime_execution_contract_missing_observation_t1_gate"
                if observation_t1_gate.get("state") != "present"
                else "gateway_attested_exchange_post_t5_missing"
                if live_clob_t5.get("state") != "verified"
                else "legacy_path_has_task_queue"
                if legacy_queue_path
                else "eligible_gateway_attested_t1_to_t5"
            ),
            "t1_to_t5_ms": t1_to_t5_ms,
            "t1_to_t5_slo_ms": 3000,
            "met": bool(p10_eligible and t1_to_t5_ms is not None and t1_to_t5_ms <= 3000),
        },
    }


def audit_v31_posterior_execution_latency(
    conn: sqlite3.Connection,
    *,
    limit: int = 200,
    latest_only: bool = False,
) -> dict[str, Any]:
    """Return read-only current-v3.1 timing evidence without claiming P10 success."""

    if not _table_exists(conn, "event_fact_history"):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": ["event_fact_history_missing"],
            "records": [],
        }
    records: list[dict[str, Any]] = []
    source_rows = _latest_fact_rows(conn, limit=limit) if latest_only else _fact_rows(conn, limit=limit)
    for row in source_rows:
        record = _record_for_fact(conn, row)
        if record is None:
            continue
        records.append(record)
        if latest_only:
            break
    latest_pointer_integrity = _latest_pointer_integrity(conn)
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(latest_pointer_integrity.get("ok")),
        "state": (
            "latest_pointer_integrity_violation"
            if latest_pointer_integrity.get("ok") is False
            else "observed" if records else "no_v31_fact_found"
        ),
        "read_only": True,
        "v31_composer_schema": V31_COMPOSER_SCHEMA,
        "record_count": len(records),
        "p10_live_slo_claimed": False,
        "latest_pointer_integrity": latest_pointer_integrity,
        "records": records,
    }


__all__ = [
    "SCHEMA_VERSION",
    "V31_COMPOSER_SCHEMA",
    "audit_v31_posterior_execution_latency",
]
