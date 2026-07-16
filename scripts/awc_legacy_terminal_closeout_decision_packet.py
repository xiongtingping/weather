#!/usr/bin/env python3
"""Read-only closeout packet for legacy AWC hard-source terminal gaps.

This packet classifies accepted legacy AWC hard-source signals that did not
persist a final terminal ledger row. It emits operator decisions and no-apply
operations only; it never writes archive.db, fetches sources, restarts launchd,
or places orders.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import ssot_chain_replay_audit as replay_audit  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "awc_legacy_terminal_closeout_decision_packet_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"
MANIFEST_SCHEMA_VERSION = "awc_legacy_terminal_closeout_decision_manifest_v1"

FINAL_TERMINAL_STATES = {"submitted", "blocked", "stale", "skipped", "no_action", "error"}
TERMINAL_RANK = {
    "": 0,
    "pending": 10,
    "fact_written": 20,
    "posterior_written": 25,
    "decision_written": 30,
    "job_enqueued": 40,
    "worker_started": 50,
    "blocked": 90,
    "stale": 90,
    "skipped": 90,
    "no_action": 90,
    "error": 90,
    "submitted": 100,
}

DECISION_UNDECIDED = "undecided"
DECISION_MATERIALIZE_EXISTING_FINAL = "materialize_existing_final_terminal"
DECISION_REVIEWED_NO_ACTION = "mark_reviewed_no_action"
DECISION_REVIEWED_STALE = "mark_reviewed_stale"
DECISION_REVIEWED_ERROR = "mark_reviewed_error"
DECISION_WAIT = "wait_for_more_evidence"
ALLOWED_DECISIONS = (
    DECISION_UNDECIDED,
    DECISION_MATERIALIZE_EXISTING_FINAL,
    DECISION_REVIEWED_NO_ACTION,
    DECISION_REVIEWED_STALE,
    DECISION_REVIEWED_ERROR,
    DECISION_WAIT,
)
REVIEWED_DECISION_TO_STATE = {
    DECISION_REVIEWED_NO_ACTION: "no_action",
    DECISION_REVIEWED_STALE: "stale",
    DECISION_REVIEWED_ERROR: "error",
}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _safe_json(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _parse_iso(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _guards() -> dict[str, bool]:
    return {
        "executes_sql_writes": False,
        "writes_archive_db": False,
        "writes_awc_hard_source_signals": False,
        "writes_signal_to_order_timing": False,
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def _terminal_rank(state: Any) -> int:
    return int(TERMINAL_RANK.get(str(state or "").strip(), 0))


def _timing_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    timing = payload.get("signal_to_order_timing")
    if isinstance(timing, Mapping):
        return dict(timing)
    hard = payload.get("hard_source_direct")
    if isinstance(hard, Mapping) and isinstance(hard.get("signal_to_order_timing"), Mapping):
        return dict(hard["signal_to_order_timing"])
    return {}


def _candidate_signal_ids_from_replay(replay: Mapping[str, Any]) -> list[str]:
    coverage = _as_mapping(replay.get("production_coverage"))
    terminal = _as_mapping(coverage.get("terminal_coverage"))
    ledger = _as_mapping(coverage.get("ledger_coverage"))
    ids: list[str] = []
    for source in (
        terminal.get("non_final_terminal_signal_ids"),
        terminal.get("missing_terminal_signal_ids"),
        terminal.get("missing_terminal_reason_signal_ids"),
        ledger.get("missing_ledger_signal_ids"),
    ):
        for item in _as_list(source):
            sid = str(item or "").strip()
            if sid and sid not in ids:
                ids.append(sid)
    return ids


def _best_timing_evidence(
    signal_id: str,
    replay: Mapping[str, Any],
    decision_evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    evidences: list[dict[str, Any]] = []
    for item in _as_list(replay.get("signal_details")):
        item_map = _as_mapping(item)
        if str(item_map.get("signal_id") or "") != signal_id:
            continue
        for timing_item in _as_list(item_map.get("timings")):
            timing_map = _as_mapping(timing_item)
            timing = dict(_as_mapping(timing_map.get("timing")))
            if not timing:
                continue
            terminal = str(timing.get("terminal_state") or "").strip()
            evidences.append(
                {
                    "source": str(timing_map.get("source") or ""),
                    "rank": _terminal_rank(terminal),
                    "terminal_state": terminal,
                    "terminal_reason": str(timing.get("terminal_reason") or timing.get("signal_to_order_slo_violation_reason") or ""),
                    "timing": timing,
                    "meta": dict(_as_mapping(timing_map.get("meta"))),
                }
            )
    for row in decision_evidence:
        timing = dict(_as_mapping(row.get("timing")))
        terminal = str(timing.get("terminal_state") or "").strip()
        evidences.append(
            {
                "source": "auto_trade_decisions",
                "rank": _terminal_rank(terminal),
                "terminal_state": terminal,
                "terminal_reason": str(timing.get("terminal_reason") or timing.get("signal_to_order_slo_violation_reason") or ""),
                "timing": timing,
                "meta": {"decision_row_id": row.get("decision_row_id"), "created_at": row.get("created_at")},
            }
        )
    if not evidences:
        return {}
    return max(evidences, key=lambda item: int(item.get("rank") or 0))


def _signal_query_hint(signal_id: str, replay: Mapping[str, Any]) -> dict[str, str]:
    for item in _as_list(replay.get("signal_details")):
        item_map = _as_mapping(item)
        if str(item_map.get("signal_id") or "") != signal_id:
            continue
        for timing_item in _as_list(item_map.get("timings")):
            timing = _as_mapping(_as_mapping(timing_item).get("timing"))
            if not timing:
                continue
            awc_at = str(timing.get("awc_hard_source_signal_at") or "").strip()
            valid_time = str(timing.get("awc_valid_time") or "").strip()
            event_date = str(timing.get("event_date") or "").strip()
            if not event_date:
                event_date = (valid_time or awc_at)[:10]
            return {
                "awc_hard_source_signal_at": awc_at,
                "awc_valid_time": valid_time,
                "event_date": event_date,
            }
    return {}


def _query_decision_evidence(
    db_path: Path | str,
    signal_ids: Sequence[str],
    *,
    since: str,
    replay: Mapping[str, Any] | None = None,
    limit_per_signal: int = 20,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {str(sid): [] for sid in signal_ids}
    if not signal_ids:
        return out
    uri = f"file:{Path(db_path)}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        for signal_id in signal_ids:
            sid = str(signal_id or "").strip()
            if not sid:
                continue
            hint = _signal_query_hint(sid, replay or {})
            awc_dt = _parse_iso(hint.get("awc_hard_source_signal_at"))
            if awc_dt is not None:
                start = _iso_z(awc_dt - timedelta(minutes=2))
                end = _iso_z(awc_dt + timedelta(minutes=30))
            else:
                start = str(since or "")
                end = "9999-12-31T23:59:59Z"
            event_date = str(hint.get("event_date") or "").strip()
            rows = []
            for action in ("entry", "exit"):
                if event_date:
                    query_rows = conn.execute(
                        """
                        SELECT id, created_at, details_json
                        FROM auto_trade_decisions
                        WHERE action = ?
                          AND event_date = ?
                          AND created_at >= ?
                          AND created_at <= ?
                          AND details_json LIKE ?
                        ORDER BY id DESC
                        LIMIT ?
                        """,
                        (action, event_date, start, end, f"%{sid}%", int(limit_per_signal)),
                    ).fetchall()
                else:
                    query_rows = conn.execute(
                        """
                        SELECT id, created_at, details_json
                        FROM auto_trade_decisions
                        WHERE action = ?
                          AND created_at >= ?
                          AND created_at <= ?
                          AND details_json LIKE ?
                        ORDER BY id DESC
                        LIMIT ?
                        """,
                        (action, start, end, f"%{sid}%", int(limit_per_signal)),
                    ).fetchall()
                rows.extend(query_rows)
            for row in rows:
                payload = _safe_json(row["details_json"])
                if not isinstance(payload, Mapping):
                    continue
                timing = _timing_from_payload(payload)
                if str(timing.get("signal_id") or "").strip() != sid:
                    continue
                out[sid].append(
                    {
                        "decision_row_id": row["id"],
                        "created_at": row["created_at"],
                        "timing": timing,
                        "action": payload.get("action"),
                        "outcome": payload.get("outcome"),
                    }
                )
    return out


def build_decision_manifest(candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    decisions = []
    for candidate in candidates:
        item = _as_mapping(candidate)
        decisions.append(
            {
                "signal_id": item.get("signal_id"),
                "recommended_decision": item.get("recommended_decision"),
                "decision": DECISION_UNDECIDED,
                "allowed_decisions": list(ALLOWED_DECISIONS),
                "review_reason": "",
                "source_evidence_ref": item.get("source_evidence_ref") or "",
                "operator": "",
                "reviewed_at": "",
            }
        )
    return {
        "schema": MANIFEST_SCHEMA_VERSION,
        "read_only_template": True,
        "decision_count": len(decisions),
        "decisions": decisions,
        "boundary": "operator_manifest_only_no_archive_write",
    }


def load_manifest_file(path: Path | str | None) -> Mapping[str, Any]:
    if not path:
        return {}
    manifest_path = Path(path).expanduser()
    if not manifest_path.exists():
        raise FileNotFoundError(str(manifest_path))
    with manifest_path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return payload if isinstance(payload, Mapping) else {}


def _manifest_decision_by_signal(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = [_as_mapping(row) for row in _as_list(manifest.get("decisions"))]
    out: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        signal_id = str(row.get("signal_id") or "").strip()
        if signal_id and signal_id not in out:
            out[signal_id] = row
    return out


def _operation_from_decision(candidate: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any] | None:
    decision = str(row.get("decision") or "").strip()
    evidence = _as_mapping(candidate.get("best_evidence"))
    timing = dict(_as_mapping(evidence.get("timing") or candidate.get("best_timing")))
    if decision == DECISION_MATERIALIZE_EXISTING_FINAL:
        terminal_state = str(evidence.get("terminal_state") or timing.get("terminal_state") or "").strip()
        terminal_reason = str(evidence.get("terminal_reason") or timing.get("terminal_reason") or "").strip()
        if terminal_state not in FINAL_TERMINAL_STATES:
            return None
    elif decision in REVIEWED_DECISION_TO_STATE:
        terminal_state = REVIEWED_DECISION_TO_STATE[decision]
        terminal_reason = str(row.get("review_reason") or "").strip()
        timing["terminal_state"] = terminal_state
        timing["terminal_reason"] = terminal_reason
        timing["terminal_at"] = str(row.get("reviewed_at") or timing.get("terminal_at") or "")
        timing["signal_to_order_slo_violation_reason"] = terminal_reason
    else:
        return None
    timing["terminal_state"] = terminal_state
    timing["terminal_reason"] = terminal_reason
    timing.setdefault("signal_id", candidate.get("signal_id"))
    timing.setdefault("awc_hard_source_signal_at", candidate.get("awc_hard_source_signal_at"))
    payload = {"signal_id": candidate.get("signal_id"), "signal_to_order_timing": timing}
    return {
        "signal_id": candidate.get("signal_id"),
        "terminal_state": terminal_state,
        "terminal_reason": terminal_reason,
        "payload": payload,
        "write_targets": ["awc_hard_source_signals", "signal_to_order_timing"],
        "uses_writers": ["upsert_awc_hard_source_signal", "upsert_signal_to_order_timing"],
    }


def audit_manifest(
    candidates: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    manifest_provided = bool(manifest)
    decision_by_signal = _manifest_decision_by_signal(manifest)
    blockers: list[str] = []
    operations: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    missing = []
    invalid = []
    undecided = []
    review_evidence_missing = []
    materialize_without_final = []
    for candidate in candidates:
        signal_id = str(_as_mapping(candidate).get("signal_id") or "").strip()
        row = decision_by_signal.get(signal_id, {})
        decision = str(row.get("decision") or DECISION_UNDECIDED).strip()
        if not manifest_provided or signal_id not in decision_by_signal:
            missing.append(signal_id)
            decision = DECISION_UNDECIDED
        if decision not in ALLOWED_DECISIONS:
            invalid.append(signal_id)
        elif decision == DECISION_UNDECIDED:
            undecided.append(signal_id)
        elif decision == DECISION_MATERIALIZE_EXISTING_FINAL:
            state = str(_as_mapping(candidate.get("best_evidence")).get("terminal_state") or "").strip()
            if state not in FINAL_TERMINAL_STATES:
                materialize_without_final.append(signal_id)
            else:
                op = _operation_from_decision(candidate, row)
                if op:
                    operations.append(op)
        elif decision in REVIEWED_DECISION_TO_STATE:
            if not str(row.get("review_reason") or "").strip():
                review_evidence_missing.append(signal_id)
            else:
                op = _operation_from_decision(candidate, row)
                if op:
                    operations.append(op)
        items.append({"signal_id": signal_id, "decision": decision})
    if not manifest_provided:
        blockers.append("awc_legacy_terminal_closeout_manifest_missing")
    if missing:
        blockers.append("awc_legacy_terminal_closeout_decision_missing")
    if invalid:
        blockers.append("awc_legacy_terminal_closeout_decision_invalid")
    if undecided:
        blockers.append("awc_legacy_terminal_closeout_decision_pending")
    if review_evidence_missing:
        blockers.append("awc_legacy_terminal_closeout_review_reason_missing")
    if materialize_without_final:
        blockers.append("awc_legacy_terminal_closeout_existing_final_evidence_missing")
    return {
        "schema": "awc_legacy_terminal_closeout_manifest_audit_v1",
        "manifest_provided": manifest_provided,
        "ok": not blockers,
        "candidate_count": len(candidates),
        "operation_count": len(operations),
        "missing_decision_signal_ids": missing,
        "invalid_decision_signal_ids": invalid,
        "pending_decision_signal_ids": undecided,
        "review_evidence_missing_signal_ids": review_evidence_missing,
        "materialize_without_final_signal_ids": materialize_without_final,
        "items": items,
        "operations": operations,
        "blockers": sorted(set(blockers)),
    }


def build_packet(
    db_path: Path | str = DEFAULT_DB,
    *,
    since_hours: float = 24.0 * 90.0,
    limit: int = 1000,
    max_items_output: int = 20,
    decision_manifest: Mapping[str, Any] | None = None,
    replay_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    replay = dict(
        replay_report
        or replay_audit.audit(
            db_path=db_path,
            since_hours=float(since_hours),
            limit=int(limit),
            max_signals_output=max(int(max_items_output), 100),
            include_details=True,
            fail_legacy_artifacts=False,
        )
    )
    signal_ids = _candidate_signal_ids_from_replay(replay)
    decision_evidence = _query_decision_evidence(
        db_path,
        signal_ids,
        since=str(replay.get("since") or ""),
        replay=replay,
    )
    candidates: list[dict[str, Any]] = []
    for signal_id in signal_ids:
        best = _best_timing_evidence(signal_id, replay, decision_evidence.get(signal_id, []))
        timing = dict(_as_mapping(best.get("timing")))
        terminal_state = str(best.get("terminal_state") or "").strip()
        recommended = (
            DECISION_MATERIALIZE_EXISTING_FINAL
            if terminal_state in FINAL_TERMINAL_STATES
            else DECISION_UNDECIDED
        )
        candidates.append(
            {
                "signal_id": signal_id,
                "awc_hard_source_signal_at": timing.get("awc_hard_source_signal_at"),
                "awc_valid_time": timing.get("awc_valid_time"),
                "source_event_id": timing.get("source_event_id"),
                "current_terminal_state": terminal_state,
                "current_terminal_reason": best.get("terminal_reason"),
                "best_evidence_source": best.get("source"),
                "best_evidence": best,
                "best_timing": timing,
                "recommended_decision": recommended,
                "source_evidence_ref": (
                    f"auto_trade_decisions.id={_as_mapping(best.get('meta')).get('decision_row_id')}"
                    if best.get("source") == "auto_trade_decisions"
                    else f"{best.get('source')}:{signal_id}"
                ),
                "requires_operator_review": recommended == DECISION_UNDECIDED,
            }
        )
    manifest_template = build_decision_manifest(candidates)
    manifest_audit = audit_manifest(candidates, decision_manifest or {})
    hard_blockers = list(manifest_audit.get("blockers") or [])
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(candidates) and not hard_blockers,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "awc_legacy_terminal_closeout_decision_packet_read_only",
        "db": str(db_path),
        "candidate_count": len(candidates),
        "recommended_materialize_count": sum(
            1 for item in candidates if item.get("recommended_decision") == DECISION_MATERIALIZE_EXISTING_FINAL
        ),
        "operator_review_required_count": sum(1 for item in candidates if item.get("requires_operator_review")),
        "candidates": candidates[: max(0, int(max_items_output))],
        "candidate_ids": [item["signal_id"] for item in candidates],
        "decision_manifest_template": manifest_template,
        "decision_manifest_audit": manifest_audit,
        "hard_blockers": hard_blockers,
        "guards": _guards(),
        "_replay_report": replay,
        "next_boundary": (
            "awc_legacy_terminal_closeout_apply_contract_ready"
            if candidates and not hard_blockers
            else "review_awc_legacy_terminal_closeout_manifest_before_apply_contract"
        ),
    }


def compact_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    audit = _as_mapping(packet.get("decision_manifest_audit"))
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": packet.get("schema"),
        "ok": bool(packet.get("ok")),
        "read_only": bool(packet.get("read_only")),
        "dry_run": bool(packet.get("dry_run")),
        "execution_mode": "awc_legacy_terminal_closeout_decision_packet_summary_read_only",
        "candidate_count": int(packet.get("candidate_count") or 0),
        "recommended_materialize_count": int(packet.get("recommended_materialize_count") or 0),
        "operator_review_required_count": int(packet.get("operator_review_required_count") or 0),
        "candidate_ids": list(packet.get("candidate_ids") or []),
        "manifest_provided": bool(audit.get("manifest_provided")),
        "operation_count": int(audit.get("operation_count") or 0),
        "hard_blockers": list(packet.get("hard_blockers") or []),
        "guards": dict(_as_mapping(packet.get("guards"))),
        "next_boundary": packet.get("next_boundary"),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since-hours", type=float, default=24.0 * 90.0)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--decision-manifest", type=Path, default=None)
    parser.add_argument("--emit-decision-manifest-template", action="store_true")
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = load_manifest_file(args.decision_manifest)
    packet = build_packet(
        args.db,
        since_hours=float(args.since_hours),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        decision_manifest=manifest,
    )
    if args.emit_decision_manifest_template:
        payload = packet["decision_manifest_template"]
    else:
        payload = compact_packet(packet) if args.summary else packet
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if packet.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
