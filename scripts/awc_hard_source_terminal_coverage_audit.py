#!/usr/bin/env python3
"""Read-only production coverage audit for AWC hard-source terminal states.

This wraps ``ssot_chain_replay_audit`` and focuses on one release criterion:
every accepted production AWC hard-source signal must end in a final terminal
state: submitted, blocked, stale, skipped, no_action, or error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import ssot_chain_replay_audit as replay_audit  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "awc_hard_source_terminal_coverage_audit_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_signal_to_order_timing": False,
        "fetches_external_weather_sources": False,
        "fetches_orderbook": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def build_audit(
    db_path: Path | str = DEFAULT_DB,
    *,
    since_hours: float = 24.0 * 90.0,
    limit: int = 1000,
    max_items_output: int = 20,
    require_signal_sample: bool = False,
    fail_legacy_artifacts: bool = False,
    replay_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    replay = dict(
        replay_report
        or replay_audit.audit(
            db_path=db_path,
            since_hours=float(since_hours),
            limit=int(limit),
            max_signals_output=int(max_items_output),
            include_details=False,
            fail_legacy_artifacts=bool(fail_legacy_artifacts),
        )
    )
    coverage = _as_mapping(replay.get("production_coverage"))
    terminal_coverage = _as_mapping(coverage.get("terminal_coverage"))
    blockers: list[str] = []
    warnings: list[str] = []

    accepted_count = int(coverage.get("accepted_signal_count") or 0)
    if require_signal_sample and accepted_count <= 0:
        blockers.append("awc_terminal_coverage_no_accepted_signal_sample")
    elif accepted_count <= 0:
        warnings.append("awc_terminal_coverage_no_accepted_signal_sample")

    coverage_blockers = [str(item) for item in _as_list(coverage.get("coverage_blockers")) if str(item)]
    for blocker in coverage_blockers:
        if blocker in {
            "accepted_signal_terminal_coverage_incomplete",
            "accepted_signal_missing_signal_to_order_ledger",
            "accepted_signal_base_timing_incomplete",
            "accepted_signal_preparation_coverage_incomplete",
        }:
            blockers.append(blocker)

    replay_blockers = [str(item) for item in _as_list(replay.get("blockers")) if str(item)]
    for blocker in replay_blockers:
        if blocker in {
            "signal_terminal_missing",
            "signal_terminal_non_final",
            "job_without_auto_trade_decision",
            "accepted_signal_missing_auto_trade_decision",
            "submitted_without_auto_trade_decision",
            "execution_chain_missing_decision_id",
        }:
            blockers.append(blocker)
        elif blocker == "no_awc_hard_source_signals_found" and require_signal_sample:
            blockers.append(blocker)

    non_final_ids = [
        str(item)
        for item in _as_list(terminal_coverage.get("non_final_terminal_signal_ids"))
        if str(item)
    ]
    missing_terminal_ids = [
        str(item)
        for item in _as_list(terminal_coverage.get("missing_terminal_signal_ids"))
        if str(item)
    ]
    missing_reason_ids = [
        str(item)
        for item in _as_list(terminal_coverage.get("missing_terminal_reason_signal_ids"))
        if str(item)
    ]
    missing_ledger_ids = [
        str(item)
        for item in _as_list(_as_mapping(coverage.get("ledger_coverage")).get("missing_ledger_signal_ids"))
        if str(item)
    ]

    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "awc_hard_source_terminal_coverage_read_only",
        "db": str(db_path),
        "since_hours": float(since_hours),
        "limit": int(limit),
        "accepted_signal_count": accepted_count,
        "accepted_signal_ids": _as_list(coverage.get("accepted_signal_ids")),
        "submitted_signal_count": int(coverage.get("submitted_signal_count") or 0),
        "terminal_state_counts": dict(_as_mapping(terminal_coverage.get("terminal_state_counts"))),
        "final_terminal_states": list(terminal_coverage.get("final_terminal_states") or []),
        "non_final_terminal_signal_ids": non_final_ids,
        "missing_terminal_signal_ids": missing_terminal_ids,
        "missing_terminal_reason_signal_ids": missing_reason_ids,
        "missing_ledger_signal_ids": missing_ledger_ids,
        "coverage_blockers": coverage_blockers,
        "replay_blockers": replay_blockers,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings + [str(item) for item in _as_list(replay.get("warnings")) if str(item)])),
        "guards": _guards(),
        "replay_summary": {
            "schema": replay.get("schema"),
            "ok": bool(replay.get("ok")),
            "signal_count": int(replay.get("signal_count") or 0),
            "candidate_scope_counts": dict(_as_mapping(replay.get("candidate_scope_counts"))),
            "terminal_state_counts": dict(_as_mapping(replay.get("terminal_state_counts"))),
            "source_counts": dict(_as_mapping(replay.get("source_counts"))),
            "blocker_counts": dict(_as_mapping(replay.get("blocker_counts"))),
            "warning_counts": dict(_as_mapping(replay.get("warning_counts"))),
        },
        "next_boundary": (
            "awc_hard_source_terminal_coverage_ready"
            if not blockers
            else "repair_or_accept_legacy_awc_terminal_gaps_before_marking_complete"
        ),
    }


def compact_audit(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": report.get("schema"),
        "ok": bool(report.get("ok")),
        "read_only": bool(report.get("read_only")),
        "execution_mode": "awc_hard_source_terminal_coverage_summary_read_only",
        "db": report.get("db"),
        "since_hours": report.get("since_hours"),
        "accepted_signal_count": int(report.get("accepted_signal_count") or 0),
        "submitted_signal_count": int(report.get("submitted_signal_count") or 0),
        "terminal_state_counts": dict(_as_mapping(report.get("terminal_state_counts"))),
        "non_final_terminal_signal_ids": list(report.get("non_final_terminal_signal_ids") or []),
        "missing_terminal_signal_ids": list(report.get("missing_terminal_signal_ids") or []),
        "missing_terminal_reason_signal_ids": list(report.get("missing_terminal_reason_signal_ids") or []),
        "missing_ledger_signal_ids": list(report.get("missing_ledger_signal_ids") or []),
        "coverage_blockers": list(report.get("coverage_blockers") or []),
        "replay_blockers": list(report.get("replay_blockers") or []),
        "blockers": list(report.get("blockers") or []),
        "warnings": list(report.get("warnings") or []),
        "replay_summary": dict(_as_mapping(report.get("replay_summary"))),
        "guards": dict(_as_mapping(report.get("guards"))),
        "next_boundary": report.get("next_boundary"),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since-hours", type=float, default=24.0 * 90.0)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--require-signal-sample", action="store_true")
    parser.add_argument("--fail-legacy-artifacts", action="store_true")
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_audit(
        args.db,
        since_hours=float(args.since_hours),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        require_signal_sample=bool(args.require_signal_sample),
        fail_legacy_artifacts=bool(args.fail_legacy_artifacts),
    )
    payload = compact_audit(report) if args.summary else report
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
