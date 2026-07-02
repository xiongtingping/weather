#!/usr/bin/env python3
"""Read-only audit for hard-source posterior override -> SSOT -> order path."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _parse_iso(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _latency_ms(start: Any, end: Any) -> Optional[float]:
    start_dt = _parse_iso(start)
    end_dt = _parse_iso(end)
    if start_dt is None or end_dt is None:
        return None
    return round(max(0.0, (end_dt - start_dt).total_seconds()) * 1000.0, 3)


def _to_float(raw: Any) -> Optional[float]:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value


def _utc_cutoff(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=float(hours))).isoformat(timespec="seconds")


def _reason_codes(raw: Any) -> List[str]:
    parsed = _safe_json(raw)
    if not isinstance(parsed, list):
        return []
    return [str(item or "").strip() for item in parsed if str(item or "").strip()]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _latest_hard_source_job(
    conn: sqlite3.Connection,
    *,
    since: str,
    slug: str = "",
    event_date: str = "",
    require_ssot_semantics: bool = True,
) -> Optional[sqlite3.Row]:
    clauses = [
        "(source = 'hard_source_direct' OR payload_json LIKE '%hard_source_direct%')",
        "created_at >= ?",
    ]
    params: List[Any] = [since]
    if slug:
        clauses.append("payload_json LIKE ?")
        params.append(f"%{slug}%")
    if event_date:
        clauses.append("payload_json LIKE ?")
        params.append(f"%{event_date}%")
    rows = conn.execute(
        f"""
        SELECT *
        FROM auto_trade_jobs
        WHERE {' AND '.join(clauses)}
        ORDER BY CASE WHEN state = 'success' THEN 0 ELSE 1 END, created_at DESC, id DESC
        LIMIT 50
        """,
        tuple(params),
    ).fetchall()
    for row in rows:
        if not require_ssot_semantics:
            return row
        payload = _safe_json(row["payload_json"])
        if not isinstance(payload, Mapping):
            continue
        hard_block = payload.get("hard_source_direct")
        if isinstance(hard_block, Mapping) and hard_block.get("decision_semantics") == "ssot_posterior_constraint":
            return row
        for item in payload.get("event_payloads") or []:
            if isinstance(item, Mapping) and item.get("snapshot_semantics") == "ssot_posterior_constraint":
                return row
    return None


def _legacy_job_detail(job: Optional[sqlite3.Row]) -> Dict[str, Any]:
    if job is None:
        return {}
    payload = _safe_json(job["payload_json"])
    hard_block = payload.get("hard_source_direct") if isinstance(payload, Mapping) else {}
    if not isinstance(hard_block, Mapping):
        hard_block = {}
    return {
        "id": job["id"],
        "state": job["state"],
        "source": job["source"],
        "run_id": job["run_id"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "ended_at": job["ended_at"],
        "decision_semantics": hard_block.get("decision_semantics"),
        "target_bucket": hard_block.get("target_bucket"),
    }


def _job_event_key(payload: Mapping[str, Any]) -> Tuple[str, str]:
    keys = payload.get("include_event_keys")
    if isinstance(keys, list) and keys and isinstance(keys[0], Mapping):
        return str(keys[0].get("slug") or ""), str(keys[0].get("event_date") or "")
    for item in payload.get("event_payloads") or []:
        if isinstance(item, Mapping):
            return str(item.get("slug") or ""), str(item.get("event_date") or "")
    return "", ""


def _event_snapshot_payload(payload: Mapping[str, Any], slug: str, event_date: str) -> Dict[str, Any]:
    for item in payload.get("event_payloads") or []:
        if not isinstance(item, Mapping):
            continue
        if slug and str(item.get("slug") or "") != slug:
            continue
        if event_date and str(item.get("event_date") or "") != event_date:
            continue
        nested = item.get("payload")
        if isinstance(nested, Mapping):
            return dict(nested)
    return {}


def _rows(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    raw = payload.get("rows") or payload.get("distribution") or []
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _target_from_job(payload: Mapping[str, Any], hard_block: Mapping[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    targets = payload.get("entry_expected_targets")
    if isinstance(targets, list):
        for target in targets:
            if not isinstance(target, Mapping):
                continue
            if bool(target.get("hard_source_direct") or target.get("hard_source_posterior_override")):
                return str(target.get("outcome") or ""), str(target.get("side") or "YES").upper(), dict(target)
    return str(hard_block.get("target_bucket") or ""), "YES", {}


def _load_decisions(
    conn: sqlite3.Connection,
    *,
    run_id: Any,
    slug: str,
    event_date: str,
    since: str,
) -> List[Dict[str, Any]]:
    clauses = ["created_at >= ?"]
    params: List[Any] = [since]
    if run_id not in (None, ""):
        clauses.append("run_id = ?")
        params.append(run_id)
    if slug:
        clauses.append("slug = ?")
        params.append(slug)
    if event_date:
        clauses.append("event_date = ?")
        params.append(event_date)
    rows = conn.execute(
        f"""
        SELECT *
        FROM auto_trade_decisions
        WHERE {' AND '.join(clauses)}
        ORDER BY id
        """,
        tuple(params),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["reason_codes"] = _reason_codes(item.get("reason_codes_json"))
        details = _safe_json(item.get("details_json"))
        item["details"] = details if isinstance(details, dict) else {}
        out.append(item)
    return out


def _matching_entry(decisions: Sequence[Mapping[str, Any]], outcome: str, side: str) -> Optional[Mapping[str, Any]]:
    for item in reversed(decisions):
        details = item.get("details") if isinstance(item.get("details"), Mapping) else {}
        codes = set(item.get("reason_codes") or [])
        if str(item.get("action") or "") != "entry":
            continue
        if str(item.get("outcome") or "") != outcome or str(item.get("side") or "").upper() != side:
            continue
        if not bool(details.get("hard_source_direct") or details.get("hard_source_posterior_override")):
            continue
        if "order_submitted" in codes or bool(item.get("will_order")):
            return item
    return None


def _matching_exits(decisions: Sequence[Mapping[str, Any]], target_outcome: str) -> List[Mapping[str, Any]]:
    out: List[Mapping[str, Any]] = []
    for item in decisions:
        if str(item.get("action") or "") != "exit":
            continue
        if str(item.get("outcome") or "") == target_outcome:
            continue
        if str(item.get("side") or "").upper() != "YES":
            continue
        details = item.get("details") if isinstance(item.get("details"), Mapping) else {}
        codes = set(item.get("reason_codes") or [])
        detail_codes = set(details.get("exit_reason_codes") or [])
        if not bool(details.get("hard_source_direct") or details.get("hard_source_posterior_override")):
            continue
        if (
            "hard_source_posterior_constraint" in codes
            or "target_realization_switch_buy_exit_old" in detail_codes
            or "target_realization_exit_clear" in detail_codes
            or "order_submitted" in codes
            or "exit_marketable_immediate_confirmed" in codes
        ):
            out.append(item)
    return out


def _has_legacy_bypass(details: Mapping[str, Any]) -> bool:
    return "bypass_bayes" in details or "bypass_fact_recompute" in details


def _require_timing(
    blockers: List[str],
    details: Mapping[str, Any],
    keys: Iterable[str],
    *,
    prefix: str,
) -> Dict[str, Any]:
    found: Dict[str, Any] = {}
    for key in keys:
        val = details.get(key)
        found[key] = val
        if val in (None, ""):
            blockers.append(f"{prefix}:{key}_missing")
    return found


def _max_ms(args: argparse.Namespace, name: str, default: float) -> Optional[float]:
    raw = getattr(args, name, default)
    value = _to_float(raw)
    if value is None or value <= 0:
        return None
    return float(value)


def _check_sla(
    blockers: List[str],
    observed: Dict[str, Any],
    *,
    key: str,
    value: Any,
    max_ms: Optional[float],
    prefix: str,
) -> None:
    numeric = _to_float(value)
    observed[key] = {
        "value_ms": numeric,
        "max_ms": max_ms,
    }
    if value in (None, ""):
        blockers.append(f"{prefix}:{key}_missing")
        return
    if numeric is None:
        blockers.append(f"{prefix}:{key}_invalid")
        return
    if max_ms is not None and numeric > max_ms:
        blockers.append(f"{prefix}:{key}_over_sla:{numeric:.3f}>{max_ms:.3f}")


def run_audit(args: argparse.Namespace) -> Dict[str, Any]:
    db_path = Path(args.db)
    since = str(args.since or "").strip() or _utc_cutoff(float(args.since_hours))
    blockers: List[str] = []
    warnings: List[str] = []
    sla_limits = {
        "fetch_to_ssot_ms": _max_ms(args, "max_fetch_to_ssot_ms", 1000.0),
        "ssot_to_job_enqueue_ms": _max_ms(args, "max_ssot_to_job_enqueue_ms", 1000.0),
        "job_enqueue_to_worker_ms": _max_ms(args, "max_job_enqueue_to_worker_ms", 5000.0),
        "ssot_to_order_ms": _max_ms(args, "max_ssot_to_order_ms", 10000.0),
        "job_enqueue_to_order_ms": _max_ms(args, "max_job_enqueue_to_order_ms", 10000.0),
        "worker_to_order_ms": _max_ms(args, "max_worker_to_order_ms", 5000.0),
    }
    details: Dict[str, Any] = {"db": str(db_path), "since": since, "sla_limits_ms": sla_limits}
    if not db_path.exists():
        return {"ok": False, "blockers": ["archive_db_missing"], "warnings": [], "details": details}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "blockers": ["archive_db_open_failed"],
            "warnings": [],
            "details": {**details, "error": f"{type(exc).__name__}:{exc}"},
        }
    with conn:
        for table in ("auto_trade_jobs", "auto_trade_decisions", "event_fact_history"):
            if not _table_exists(conn, table):
                blockers.append(f"{table}_missing")
        if blockers:
            return {"ok": False, "blockers": blockers, "warnings": warnings, "details": details}

        job = _latest_hard_source_job(
            conn,
            since=since,
            slug=args.slug or "",
            event_date=args.event_date or "",
            require_ssot_semantics=True,
        )
        if job is None:
            legacy = _latest_hard_source_job(
                conn,
                since=since,
                slug=args.slug or "",
                event_date=args.event_date or "",
                require_ssot_semantics=False,
            )
            legacy_detail = _legacy_job_detail(legacy)
            if legacy_detail:
                warnings.append("legacy_hard_source_job_ignored")
                details["legacy_hard_source_job_ignored"] = legacy_detail
            return {
                "ok": False,
                "blockers": ["hard_source_ssot_job_missing"],
                "warnings": warnings,
                "details": details,
            }
        job_payload = _safe_json(job["payload_json"])
        if not isinstance(job_payload, dict):
            return {
                "ok": False,
                "blockers": ["hard_source_job_payload_invalid"],
                "warnings": warnings,
                "details": {**details, "job_id": job["id"]},
            }
        hard_block = job_payload.get("hard_source_direct")
        if not isinstance(hard_block, Mapping):
            blockers.append("job_hard_source_direct_block_missing")
            hard_block = {}
        slug, event_date = _job_event_key(job_payload)
        if args.slug:
            slug = str(args.slug)
        if args.event_date:
            event_date = str(args.event_date)
        target_outcome, target_side, expected_target = _target_from_job(job_payload, hard_block)
        details.update(
            {
                "job": {
                    "id": job["id"],
                    "state": job["state"],
                    "source": job["source"],
                    "run_id": job["run_id"],
                    "created_at": job["created_at"],
                    "started_at": job["started_at"],
                    "ended_at": job["ended_at"],
                },
                "event": {"slug": slug, "event_date": event_date},
                "target": {"outcome": target_outcome, "side": target_side},
                "hard_source_direct": dict(hard_block),
            }
        )
        if str(job["source"] or "") != "hard_source_direct":
            blockers.append("job_source_not_hard_source_direct")
        if str(job["state"] or "") != "success":
            blockers.append(f"job_not_success:{job['state']}")
        if hard_block.get("decision_semantics") != "ssot_posterior_constraint":
            blockers.append("job_decision_semantics_not_ssot_posterior_constraint")
        if not bool(hard_block.get("posterior_override")):
            blockers.append("job_posterior_override_missing")
        if not target_outcome:
            blockers.append("target_outcome_missing")
        if expected_target:
            if not bool(expected_target.get("hard_source_direct") or expected_target.get("hard_source_posterior_override")):
                blockers.append("expected_target_hard_source_marker_missing")
            if not bool(expected_target.get("posterior_override")):
                blockers.append("expected_target_posterior_override_missing")
        else:
            blockers.append("expected_target_missing")
        job_sla: Dict[str, Any] = {}
        _check_sla(
            blockers,
            job_sla,
            key="fetch_to_ssot_ms",
            value=hard_block.get("fetch_to_ssot_ms") or expected_target.get("hard_source_fetch_to_ssot_ms"),
            max_ms=sla_limits["fetch_to_ssot_ms"],
            prefix="job",
        )
        _check_sla(
            blockers,
            job_sla,
            key="ssot_to_job_enqueue_ms",
            value=hard_block.get("ssot_to_job_enqueue_ms") or expected_target.get("hard_source_ssot_to_job_enqueue_ms"),
            max_ms=sla_limits["ssot_to_job_enqueue_ms"],
            prefix="job",
        )
        job_enqueue_to_worker_ms = (
            hard_block.get("job_enqueue_to_worker_ms")
            or expected_target.get("hard_source_job_enqueue_to_worker_ms")
            or _latency_ms(job["created_at"], job["started_at"])
        )
        _check_sla(
            blockers,
            job_sla,
            key="job_enqueue_to_worker_ms",
            value=job_enqueue_to_worker_ms,
            max_ms=sla_limits["job_enqueue_to_worker_ms"],
            prefix="job",
        )
        details["job_sla"] = job_sla

        snapshot = _event_snapshot_payload(job_payload, slug, event_date)
        snapshot_rows = _rows(snapshot)
        if not snapshot:
            blockers.append("job_event_payload_snapshot_missing")
        if not snapshot_rows:
            blockers.append("job_event_payload_rows_missing")
        target_row = next((row for row in snapshot_rows if str(row.get("outcome") or row.get("label") or "") == target_outcome), {})
        target_index = next(
            (
                idx for idx, row in enumerate(snapshot_rows)
                if str(row.get("outcome") or row.get("label") or "") == target_outcome
            ),
            None,
        )
        hard_zero_rows = [
            row for row in snapshot_rows
            if bool(row.get("q_posterior_observed_constraints_hard_zero"))
            and float(row.get("q_bayes_posterior") or 0.0) == 0.0
        ]
        details["snapshot"] = {
            "row_count": len(snapshot_rows),
            "target_q_bayes_posterior": target_row.get("q_bayes_posterior") if target_row else None,
            "hard_zero_count": len(hard_zero_rows),
            "hard_zero_required": bool(target_index is None or target_index > 0),
            "hard_source_direct_snapshot": snapshot.get("hard_source_direct_snapshot"),
            "hard_source_direct_ssot_write": snapshot.get("hard_source_direct_ssot_write"),
        }
        if not bool(snapshot.get("hard_source_direct_snapshot")):
            blockers.append("job_snapshot_not_marked_hard_source_direct")
        if not bool(snapshot.get("hard_source_direct_ssot_write")):
            blockers.append("snapshot_ssot_write_marker_missing")
        snapshot_meta = snapshot.get("hard_source_posterior_override_meta")
        if not isinstance(snapshot_meta, Mapping):
            snapshot_meta = {}
        final_bucket_lock = bool(snapshot_meta.get("final_bucket_lock") or target_row.get("hard_source_posterior_override_final_bucket_lock"))
        details["snapshot"]["final_bucket_lock"] = final_bucket_lock
        if final_bucket_lock and target_row and float(target_row.get("q_bayes_posterior") or 0.0) <= 0.99:
            blockers.append("target_posterior_not_final_locked")
        if bool(target_index is None or target_index > 0) and not hard_zero_rows:
            blockers.append("hard_zero_rows_missing")

        decisions = _load_decisions(
            conn,
            run_id=job["run_id"],
            slug=slug,
            event_date=event_date,
            since=since,
        )
        if not decisions and job["run_id"] not in (None, ""):
            warnings.append("job_run_id_has_no_matching_decisions_fallback_to_event_window")
            decisions = _load_decisions(
                conn,
                run_id=None,
                slug=slug,
                event_date=event_date,
                since=since,
            )
        entry = _matching_entry(decisions, target_outcome, target_side)
        exits = _matching_exits(decisions, target_outcome)
        details["decisions"] = {
            "count": len(decisions),
            "entry_found": bool(entry),
            "exit_count": len(exits),
        }
        if entry is None:
            blockers.append("target_entry_decision_missing")
        else:
            entry_details = entry.get("details") if isinstance(entry.get("details"), Mapping) else {}
            details["entry"] = {
                "id": entry.get("id"),
                "state": entry.get("decided_state"),
                "will_order": entry.get("will_order"),
                "reason_codes": entry.get("reason_codes"),
                "timing": _require_timing(
                    blockers,
                    entry_details,
                    (
                        "hard_source_ssot_written_at",
                        "hard_source_job_enqueued_at",
                        "hard_source_worker_started_at",
                        "hard_source_order_submitted_at",
                        "hard_source_ssot_to_order_ms",
                        "hard_source_job_enqueue_to_order_ms",
                        "hard_source_worker_to_order_ms",
                    ),
                    prefix="entry",
                ),
            }
            _check_sla(
                blockers,
                details["entry"]["timing"],
                key="hard_source_ssot_to_order_ms",
                value=entry_details.get("hard_source_ssot_to_order_ms"),
                max_ms=sla_limits["ssot_to_order_ms"],
                prefix="entry",
            )
            _check_sla(
                blockers,
                details["entry"]["timing"],
                key="hard_source_job_enqueue_to_order_ms",
                value=entry_details.get("hard_source_job_enqueue_to_order_ms"),
                max_ms=sla_limits["job_enqueue_to_order_ms"],
                prefix="entry",
            )
            _check_sla(
                blockers,
                details["entry"]["timing"],
                key="hard_source_worker_to_order_ms",
                value=entry_details.get("hard_source_worker_to_order_ms"),
                max_ms=sla_limits["worker_to_order_ms"],
                prefix="entry",
            )
            if _has_legacy_bypass(entry_details):
                blockers.append("entry_uses_legacy_bypass_semantics")
        if exits:
            details["exits"] = []
            for item in exits:
                exit_details = item.get("details") if isinstance(item.get("details"), Mapping) else {}
                details["exits"].append(
                    {
                        "id": item.get("id"),
                        "outcome": item.get("outcome"),
                        "state": item.get("decided_state"),
                        "will_order": item.get("will_order"),
                        "reason_codes": item.get("reason_codes"),
                        "timing": _require_timing(
                            blockers,
                            exit_details,
                            (
                                "hard_source_ssot_written_at",
                                "hard_source_worker_started_at",
                                "hard_source_order_submitted_at",
                                "hard_source_ssot_to_order_ms",
                                "hard_source_job_enqueue_to_order_ms",
                                "hard_source_worker_to_order_ms",
                            ),
                            prefix=f"exit:{item.get('outcome')}",
                        ),
                    }
                )
                exit_timing = details["exits"][-1]["timing"]
                _check_sla(
                    blockers,
                    exit_timing,
                    key="hard_source_ssot_to_order_ms",
                    value=exit_details.get("hard_source_ssot_to_order_ms"),
                    max_ms=sla_limits["ssot_to_order_ms"],
                    prefix=f"exit:{item.get('outcome')}",
                )
                _check_sla(
                    blockers,
                    exit_timing,
                    key="hard_source_job_enqueue_to_order_ms",
                    value=exit_details.get("hard_source_job_enqueue_to_order_ms"),
                    max_ms=sla_limits["job_enqueue_to_order_ms"],
                    prefix=f"exit:{item.get('outcome')}",
                )
                _check_sla(
                    blockers,
                    exit_timing,
                    key="hard_source_worker_to_order_ms",
                    value=exit_details.get("hard_source_worker_to_order_ms"),
                    max_ms=sla_limits["worker_to_order_ms"],
                    prefix=f"exit:{item.get('outcome')}",
                )
                if _has_legacy_bypass(exit_details):
                    blockers.append(f"exit:{item.get('outcome')}:uses_legacy_bypass_semantics")
        else:
            warnings.append("old_bucket_exit_decision_missing_or_no_open_old_bucket")

        if hard_block.get("ssot_to_job_enqueue_ms") is None and expected_target.get("hard_source_ssot_to_job_enqueue_ms") is None:
            blockers.append("ssot_to_job_enqueue_ms_missing")
        if (
            hard_block.get("job_enqueue_to_worker_ms") is None
            and expected_target.get("hard_source_job_enqueue_to_worker_ms") is None
            and _latency_ms(job["created_at"], job["started_at"]) is None
        ):
            blockers.append("job_enqueue_to_worker_ms_missing")

    return {
        "ok": not blockers,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "details": details,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since", default="")
    parser.add_argument("--since-hours", type=float, default=48.0)
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--max-fetch-to-ssot-ms", type=float, default=1000.0)
    parser.add_argument("--max-ssot-to-job-enqueue-ms", type=float, default=1000.0)
    parser.add_argument("--max-job-enqueue-to-worker-ms", type=float, default=5000.0)
    parser.add_argument("--max-ssot-to-order-ms", type=float, default=10000.0)
    parser.add_argument("--max-job-enqueue-to-order-ms", type=float, default=10000.0)
    parser.add_argument("--max-worker-to-order-ms", type=float, default=5000.0)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_audit(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
