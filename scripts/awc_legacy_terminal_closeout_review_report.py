#!/usr/bin/env python3
"""Read-only review report for AWC legacy terminal closeout candidates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import awc_legacy_terminal_closeout_decision_packet as decision_packet  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "awc_legacy_terminal_closeout_review_report_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _guards() -> dict[str, bool]:
    return {
        "executes_sql_writes": False,
        "writes_archive_db": False,
        "writes_awc_hard_source_signals": False,
        "writes_signal_to_order_timing": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def _timing_summary(timing: Mapping[str, Any]) -> dict[str, Any]:
    timing = _as_mapping(timing)
    keys = (
        "source_id",
        "source_event_id",
        "awc_valid_time",
        "awc_hard_source_signal_at",
        "awc_pre_signal_deadline_at",
        "fact_written_at",
        "posterior_written_at",
        "decision_written_at",
        "job_enqueued_at",
        "worker_started_at",
        "order_submitted_at",
        "terminal_at",
        "terminal_state",
        "terminal_reason",
        "signal_to_order_slo_met",
        "signal_to_order_slo_violation_reason",
    )
    return {key: timing.get(key) for key in keys if timing.get(key) not in (None, "")}


def _evidence_strength(candidate: Mapping[str, Any]) -> str:
    state = str(candidate.get("current_terminal_state") or "")
    if state in decision_packet.FINAL_TERMINAL_STATES:
        return "final_terminal_evidence"
    if state == "fact_written":
        return "fact_only_non_final_evidence"
    if state:
        return "non_final_terminal_evidence"
    return "missing_terminal_evidence"


def _review_instruction(candidate: Mapping[str, Any]) -> str:
    if not bool(candidate.get("requires_operator_review")):
        return "可采用 recommended_decision 物化既有 final terminal；仍需 operator 审阅 manifest 后进入 apply contract。"
    return (
        "必须人工判断：若确认 legacy accepted signal 只写 fact 且没有决策/订单输出，选择 "
        "mark_reviewed_no_action；若应视为超时/过期选择 mark_reviewed_stale；若代表异常选择 "
        "mark_reviewed_error；证据不足则保持 undecided 或 wait_for_more_evidence。"
    )


def _review_item(candidate: Mapping[str, Any]) -> dict[str, Any]:
    best = _as_mapping(candidate.get("best_evidence"))
    timing = _as_mapping(best.get("timing") or candidate.get("best_timing"))
    meta = _as_mapping(best.get("meta"))
    return {
        "signal_id": candidate.get("signal_id"),
        "source_event_id": candidate.get("source_event_id"),
        "awc_valid_time": candidate.get("awc_valid_time"),
        "awc_hard_source_signal_at": candidate.get("awc_hard_source_signal_at"),
        "recommended_decision": candidate.get("recommended_decision"),
        "requires_operator_review": bool(candidate.get("requires_operator_review")),
        "current_terminal_state": candidate.get("current_terminal_state"),
        "current_terminal_reason": candidate.get("current_terminal_reason"),
        "best_evidence_source": candidate.get("best_evidence_source"),
        "source_evidence_ref": candidate.get("source_evidence_ref"),
        "evidence_strength": _evidence_strength(candidate),
        "evidence_meta": {
            key: meta.get(key)
            for key in (
                "decision_row_id",
                "fact_id",
                "fact_snapshot_id",
                "posterior_run_id",
                "created_at",
                "ssot_target_bucket",
            )
            if meta.get(key) not in (None, "")
        },
        "timing": _timing_summary(timing),
        "allowed_decisions": list(decision_packet.ALLOWED_DECISIONS),
        "review_instruction": _review_instruction(candidate),
    }


def build_draft_manifest(packet: Mapping[str, Any]) -> dict[str, Any]:
    manifest = dict(_as_mapping(packet.get("decision_manifest_template")))
    decisions = []
    candidates_by_id = {
        str(_as_mapping(candidate).get("signal_id") or ""): _as_mapping(candidate)
        for candidate in _as_sequence(packet.get("candidates"))
    }
    for raw in _as_sequence(manifest.get("decisions")):
        row = dict(_as_mapping(raw))
        candidate = candidates_by_id.get(str(row.get("signal_id") or ""), {})
        if (
            candidate
            and candidate.get("recommended_decision") == decision_packet.DECISION_MATERIALIZE_EXISTING_FINAL
            and not bool(candidate.get("requires_operator_review"))
        ):
            row["decision"] = decision_packet.DECISION_MATERIALIZE_EXISTING_FINAL
        else:
            row["decision"] = decision_packet.DECISION_UNDECIDED
        row.setdefault("review_reason", "")
        row.setdefault("operator", "")
        row.setdefault("reviewed_at", "")
        decisions.append(row)
    manifest["decisions"] = decisions
    manifest["boundary"] = "operator_review_required_before_apply_no_archive_write"
    manifest["draft_policy"] = (
        "Only final-terminal evidence is prefilled; operator-review candidates remain undecided."
    )
    return manifest


def build_review_report(
    db_path: Path | str = DEFAULT_DB,
    *,
    since_hours: float = 24.0 * 90.0,
    limit: int = 500,
    max_items_output: int = 20,
    closeout_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    packet = dict(
        closeout_packet
        or decision_packet.build_packet(
            db_path,
            since_hours=float(since_hours),
            limit=int(limit),
            max_items_output=int(max_items_output),
        )
    )
    candidates = [_as_mapping(candidate) for candidate in _as_sequence(packet.get("candidates"))]
    review_items = [_review_item(candidate) for candidate in candidates]
    draft_manifest = build_draft_manifest(packet)
    review_required = [item["signal_id"] for item in review_items if item.get("requires_operator_review")]
    materialize_ready = [
        item["signal_id"]
        for item in review_items
        if item.get("recommended_decision") == decision_packet.DECISION_MATERIALIZE_EXISTING_FINAL
        and not item.get("requires_operator_review")
    ]
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "awc_legacy_terminal_closeout_review_report_read_only",
        "db": str(db_path),
        "candidate_count": int(packet.get("candidate_count") or 0),
        "recommended_materialize_count": len(materialize_ready),
        "operator_review_required_count": len(review_required),
        "review_required_signal_ids": review_required,
        "materialize_ready_signal_ids": materialize_ready,
        "review_items": review_items[: max(0, int(max_items_output))],
        "review_items_truncated": len(review_items) > max(0, int(max_items_output)),
        "draft_manifest": draft_manifest,
        "next_boundary": (
            "operator_completes_awc_terminal_closeout_manifest"
            if review_required
            else "audit_awc_terminal_closeout_draft_manifest_with_apply_contract"
        ),
        "guards": _guards(),
        "closeout_packet_summary": decision_packet.compact_packet(packet),
    }


def compact_review_report(report: Mapping[str, Any], *, max_items_output: int = 8) -> dict[str, Any]:
    visible = max(0, int(max_items_output))
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": report.get("schema"),
        "ok": bool(report.get("ok")),
        "read_only": bool(report.get("read_only")),
        "dry_run": bool(report.get("dry_run")),
        "execution_mode": "awc_legacy_terminal_closeout_review_report_summary_read_only",
        "db": report.get("db"),
        "candidate_count": int(report.get("candidate_count") or 0),
        "recommended_materialize_count": int(report.get("recommended_materialize_count") or 0),
        "operator_review_required_count": int(report.get("operator_review_required_count") or 0),
        "review_required_signal_ids": list(report.get("review_required_signal_ids") or [])[:visible],
        "materialize_ready_signal_ids": list(report.get("materialize_ready_signal_ids") or [])[:visible],
        "review_items": _as_sequence(report.get("review_items"))[:visible],
        "review_items_truncated": bool(report.get("review_items_truncated")),
        "draft_manifest_decision_count": int(_as_mapping(report.get("draft_manifest")).get("decision_count") or 0),
        "next_boundary": report.get("next_boundary"),
        "guards": dict(_as_mapping(report.get("guards"))),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since-hours", type=float, default=24.0 * 90.0)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--emit-draft-manifest", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_review_report(
        args.db,
        since_hours=float(args.since_hours),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
    )
    if args.emit_draft_manifest:
        payload: Mapping[str, Any] = _as_mapping(report.get("draft_manifest"))
    elif args.summary:
        payload = compact_review_report(report, max_items_output=int(args.max_items_output))
    else:
        payload = report
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
