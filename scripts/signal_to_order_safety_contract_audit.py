#!/usr/bin/env python3
"""Read-only safety contract audit for AWC signal-to-order execution.

The audit proves the fast path still treats freshness and duplicate-order
guards as blockers. It does not read archive.db, fetch orderbooks, place orders,
write ledgers, or restart services.
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

import scan_weather_market as swm  # noqa: E402
from scripts import hard_source_direct_poller as poller  # noqa: E402


SCHEMA_VERSION = "signal_to_order_safety_contract_audit_v1"


REQUIRED_STALE_CASES = {
    "stale_decision": ("decision_stale", "decision freshness expired"),
    "stale_posterior": ("posterior_expired", "posterior freshness expired"),
    "stale_orderbook": ("orderbook_expired", "orderbook stale before submit"),
}

REQUIRED_DUPLICATE_BLOCK_REASONS = (
    "duplicate_open_order",
    "entry_submit_dedup_blocked",
    "same_outcome_open_order",
    "open_order_pending",
)

REQUIRED_ORDERBOOK_BLOCK_REASONS = (
    "orderbook_missing",
    "book_missing",
    "orderbook_unavailable",
    "entry_orderbook_missing",
)

REQUIRED_CAP_BLOCK_REASONS = (
    "event_exposure_cap",
    "event_position_cap",
    "bucket_exposure_cap",
    "adjacent_bucket_exposure_cap",
)


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_auto_orders": False,
        "writes_signal_to_order_timing": False,
        "fetches_external_weather_sources": False,
        "fetches_orderbook": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def _source_slice(path: Path, start_marker: str, end_marker: str = "") -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(start_marker)
    if end_marker:
        end = text.index(end_marker, start + len(start_marker))
        return text[start:end]
    return text[start:]


def build_audit(*, root: Path = ROOT) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []

    stale_results: dict[str, dict[str, Any]] = {}
    for name, (reason_code, reason_text) in REQUIRED_STALE_CASES.items():
        terminal_state = swm._classify_signal_to_order_terminal_state(reason_code, reason_text)
        ok = terminal_state == "stale"
        stale_results[name] = {
            "reason_code": reason_code,
            "reason_text": reason_text,
            "terminal_state": terminal_state,
            "expected_terminal_state": "stale",
            "ok": ok,
        }
        if not ok:
            blockers.append(f"{name}_does_not_classify_as_stale")

    duplicate_results: dict[str, dict[str, Any]] = {}
    aliases = getattr(swm, "ENTRY_HARD_REASON_ALIASES", {})
    hard_reasons = getattr(swm, "ENTRY_HARD_REASON_CODES", set())
    for reason in REQUIRED_DUPLICATE_BLOCK_REASONS:
        normalized = aliases.get(reason, reason) if isinstance(aliases, Mapping) else reason
        terminal_state = swm._classify_signal_to_order_terminal_state(reason, "duplicate order guard")
        hard_reason_ok = normalized == "duplicate_open_order" and normalized in hard_reasons
        terminal_ok = terminal_state == "blocked"
        duplicate_results[reason] = {
            "normalized_reason": normalized,
            "terminal_state": terminal_state,
            "hard_reason_ok": hard_reason_ok,
            "terminal_ok": terminal_ok,
            "ok": bool(hard_reason_ok and terminal_ok),
        }
        if not hard_reason_ok:
            blockers.append(f"{reason}_not_mapped_to_duplicate_open_order_hard_reason")
        if not terminal_ok:
            blockers.append(f"{reason}_does_not_classify_as_blocked")

    candidate = {
        "signal_id": "sig_contract_stale",
        "source_event_id": "src_awc_contract_stale",
        "source_id": "zuuu_metar_raw_awc",
        "acceptance_state": "accepted",
        "acceptance_reason": "awc_hourly_signal_accepted",
        "signal_eligible": True,
        "awc_valid_time": "2026-07-03T09:00:00Z",
        "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
    }
    stale_timing = poller._signal_to_order_timing_from_candidate(
        candidate,
        decision_written_at="2026-07-03T09:05:39Z",
        terminal_state="stale",
        terminal_reason="posterior_expired",
        terminal_at="2026-07-03T09:05:39Z",
    )
    timing_contract = {
        "terminal_state": stale_timing.get("terminal_state"),
        "terminal_reason": stale_timing.get("terminal_reason"),
        "order_submitted_at": stale_timing.get("order_submitted_at"),
        "signal_to_order_slo_met": stale_timing.get("signal_to_order_slo_met"),
        "signal_to_order_slo_violation_reason": stale_timing.get(
            "signal_to_order_slo_violation_reason"
        ),
        "guards": dict(stale_timing.get("guards") or {}),
    }
    if timing_contract["terminal_state"] != "stale":
        blockers.append("stale_timing_terminal_state_not_stale")
    if timing_contract["order_submitted_at"]:
        blockers.append("stale_timing_has_order_submitted_at")
    if timing_contract["signal_to_order_slo_met"] is not False:
        blockers.append("stale_timing_slo_not_false")
    if timing_contract["signal_to_order_slo_violation_reason"] != "posterior_expired":
        blockers.append("stale_timing_violation_reason_not_preserved")
    for key in (
        "recomputes_weather_probability",
        "bypasses_auto_trade_decisions",
        "bypasses_orderbook_or_risk",
    ):
        if timing_contract["guards"].get(key) is not False:
            blockers.append(f"stale_timing_guard_{key}_not_false")

    execution_details = {
        "hard_source_direct": True,
        "signal_id": "sig_contract_execution_segments",
        "source_event_id": "src_awc_contract_execution_segments",
        "awc_valid_time": "2026-07-03T09:00:00Z",
        "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
        "signal_to_order_slo_ms": 3000,
        "signal_to_order_timing": {
            "schema": "signal_to_order_timing_v1",
            "signal_id": "sig_contract_execution_segments",
            "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
            "terminal_state": "worker_started",
        },
    }
    swm._apply_signal_to_order_execution_segments(
        execution_details,
        orderbook_refresh_started_at="2026-07-03T09:05:38.050Z",
        orderbook_refreshed_at="2026-07-03T09:05:38.250Z",
        orderbook_checked_at="2026-07-03T09:05:38.300Z",
        orderbook_check_source="contract_orderbook_refresh",
        orderbook_check_result="pass",
        risk_checked_at="2026-07-03T09:05:38.900Z",
        risk_check_source="contract_risk_guard",
        risk_check_result="pass",
        idempotency_key="clobsvc:contract-segments",
        idempotency_checked_at="2026-07-03T09:05:39.000Z",
        submit_requested_at="2026-07-03T09:05:39.100Z",
        exchange_ack_at="2026-07-03T09:05:39.700Z",
        order_submitted_at="2026-07-03T09:05:40Z",
        order_ack_status="submitted",
    )
    execution_timing = dict(execution_details.get("signal_to_order_timing") or {})
    execution_segment_contract = {
        "orderbook_refresh_started_at": execution_timing.get("orderbook_refresh_started_at"),
        "orderbook_refreshed_at": execution_timing.get("orderbook_refreshed_at"),
        "orderbook_checked_at": execution_timing.get("orderbook_checked_at"),
        "orderbook_check_source": execution_timing.get("orderbook_check_source"),
        "orderbook_check_result": execution_timing.get("orderbook_check_result"),
        "risk_checked_at": execution_timing.get("risk_checked_at"),
        "risk_check_source": execution_timing.get("risk_check_source"),
        "risk_check_result": execution_timing.get("risk_check_result"),
        "idempotency_checked_at": execution_timing.get("idempotency_checked_at"),
        "idempotency_key": execution_timing.get("idempotency_key"),
        "submit_requested_at": execution_timing.get("submit_requested_at"),
        "exchange_ack_at": execution_timing.get("exchange_ack_at"),
        "order_submitted_at": execution_timing.get("order_submitted_at"),
        "signal_to_orderbook_ms": execution_timing.get("signal_to_orderbook_ms"),
        "orderbook_to_risk_check_ms": execution_timing.get("orderbook_to_risk_check_ms"),
        "risk_check_to_submit_ms": execution_timing.get("risk_check_to_submit_ms"),
        "submit_request_to_exchange_ack_ms": execution_timing.get("submit_request_to_exchange_ack_ms"),
    }
    for key in (
        "orderbook_refresh_started_at",
        "orderbook_refreshed_at",
        "orderbook_checked_at",
        "risk_checked_at",
        "idempotency_checked_at",
        "submit_requested_at",
        "exchange_ack_at",
        "order_submitted_at",
    ):
        if not execution_segment_contract.get(key):
            blockers.append(f"execution_segment_{key}_missing")
    for key in (
        "signal_to_orderbook_ms",
        "orderbook_to_risk_check_ms",
        "risk_check_to_submit_ms",
        "submit_request_to_exchange_ack_ms",
    ):
        if execution_segment_contract.get(key) is None:
            blockers.append(f"execution_segment_{key}_missing")

    orderbook_results: dict[str, dict[str, Any]] = {}
    for reason in REQUIRED_ORDERBOOK_BLOCK_REASONS:
        normalized = aliases.get(reason, reason) if isinstance(aliases, Mapping) else reason
        terminal_state = swm._classify_signal_to_order_terminal_state(reason, "orderbook guard")
        hard_reason_ok = normalized == "orderbook_missing" and normalized in hard_reasons
        terminal_ok = terminal_state == "blocked"
        orderbook_results[reason] = {
            "normalized_reason": normalized,
            "terminal_state": terminal_state,
            "hard_reason_ok": hard_reason_ok,
            "terminal_ok": terminal_ok,
            "ok": bool(hard_reason_ok and terminal_ok),
        }
        if not hard_reason_ok:
            blockers.append(f"{reason}_not_mapped_to_orderbook_missing_hard_reason")
        if not terminal_ok:
            blockers.append(f"{reason}_does_not_classify_as_blocked")

    cap_results: dict[str, dict[str, Any]] = {}
    for reason in REQUIRED_CAP_BLOCK_REASONS:
        normalized = aliases.get(reason, reason) if isinstance(aliases, Mapping) else reason
        terminal_state = swm._classify_signal_to_order_terminal_state(reason, "risk cap guard")
        expected = "event_exposure_cap" if "event" in reason or "position" in reason else "bucket_exposure_cap"
        hard_reason_ok = normalized == expected and normalized in hard_reasons
        terminal_ok = terminal_state == "blocked"
        cap_results[reason] = {
            "normalized_reason": normalized,
            "expected_reason": expected,
            "terminal_state": terminal_state,
            "hard_reason_ok": hard_reason_ok,
            "terminal_ok": terminal_ok,
            "ok": bool(hard_reason_ok and terminal_ok),
        }
        if not hard_reason_ok:
            blockers.append(f"{reason}_not_mapped_to_{expected}_hard_reason")
        if not terminal_ok:
            blockers.append(f"{reason}_does_not_classify_as_blocked")

    scan_path = Path(root) / "scan_weather_market.py"
    service_path = Path(root) / "scripts" / "clob_order_service.mjs"
    static_contract: dict[str, Any] = {}
    try:
        classifier_source = _source_slice(
            scan_path,
            "def _classify_signal_to_order_terminal_state(",
            "def _apply_signal_to_order_terminal(",
        )
        order_submit_policy_source = _source_slice(
            scan_path,
            "def _clob_order_service_idempotency_key(",
            "def _clob_order_service_direct_target_idempotency_key(",
        )
        execution_segment_source = _source_slice(
            scan_path,
            "def _apply_signal_to_order_execution_segments(",
            "def _classify_signal_to_order_terminal_state(",
        )
        run_node_order_source = _source_slice(
            scan_path,
            "def _run_node_order(",
            "def _classify_mirror_order_failure(",
        )
        primary_fast_path_source = _source_slice(
            scan_path,
            "def _posterior_target_primary_entry_fast_path(",
            "    def _is_effectively_binary_extreme(",
        )
        clob_source = service_path.read_text(encoding="utf-8")
        static_contract = {
            "classifier_has_stale_markers": all(
                marker in classifier_source
                for marker in ("posterior_expired", "decision_expired", "orderbook_expired")
            ),
            "classifier_has_duplicate_markers": all(
                marker in classifier_source
                for marker in ("duplicate", "open_order", "same_outcome_open_order")
            ),
            "python_idempotency_key_excludes_audit_metadata": all(
                field in order_submit_policy_source
                for field in (
                    "execution_domain",
                    "order_execution_domain",
                    "order_execution_source",
                    "order_execution_context_version",
                    "order_execution_reason",
                    "order_intent_action",
                    "manual_confirm_source",
                )
            ),
            "clob_service_has_idempotent_replay": "idempotent_replay" in clob_source,
            "clob_service_has_inflight_guard": "idempotent_inflight" in clob_source,
            "clob_service_exposes_order_status": "/order-status" in clob_source,
            "execution_segments_preserve_orderbook_refresh": all(
                field in execution_segment_source
                for field in (
                    "orderbook_refresh_started_at",
                    "orderbook_refreshed_at",
                    "orderbook_checked_at",
                    "orderbook_to_risk_check_ms",
                )
            ),
            "primary_fast_path_checks_orderbook_before_submit": (
                "orderbook_checked_at=utc_now_iso_ms()" in primary_fast_path_source
                and "orderbook_check_source=\"posterior_target_primary_fast_path_candidate_quote\"" in primary_fast_path_source
                and primary_fast_path_source.find("orderbook_checked_at=utc_now_iso_ms()")
                < primary_fast_path_source.find("_run_node_order(")
            ),
            "primary_fast_path_runs_risk_guards_before_submit": all(
                marker in primary_fast_path_source
                for marker in (
                    "_entry_buy_balance_guard(",
                    "risk_check_source=\"entry_pre_submit_guards\"",
                )
            )
            and primary_fast_path_source.find("risk_check_source=\"entry_pre_submit_guards\"")
            < primary_fast_path_source.find("_run_node_order("),
            "primary_fast_path_persists_decision_before_submit": all(
                marker in primary_fast_path_source
                for marker in (
                    'decided_state="pending_submit"',
                    'reason_codes=["order_pending_submit"]',
                    "_flush_decisions()",
                    "conn.commit()",
                    'order_context_payload["order_intent_id"] = order_intent_id',
                )
            )
            and primary_fast_path_source.find('decided_state="pending_submit"')
            < primary_fast_path_source.find("_run_node_order("),
            "run_node_order_applies_buy_notional_cap": all(
                marker in run_node_order_source
                for marker in (
                    "_entry_order_notional_cap_usdc()",
                    "order_cap_meta",
                    "payload.update(order_cap_meta)",
                    "size_cap = float(buy_notional_cap_usdc) / float(price_for_size)",
                )
            ),
        }
    except Exception as exc:
        blockers.append("static_contract_scan_failed")
        warnings.append(f"static_contract_scan_error:{type(exc).__name__}:{exc}")

    for key, value in static_contract.items():
        if value is not True:
            blockers.append(f"static_contract_{key}_missing")

    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "signal_to_order_safety_contract_audit_read_only",
        "checks": {
            "stale_decision_blocks": stale_results["stale_decision"]["ok"],
            "stale_posterior_blocks": stale_results["stale_posterior"]["ok"],
            "stale_orderbook_blocks": stale_results["stale_orderbook"]["ok"],
            "duplicate_order_blocks": all(item["ok"] for item in duplicate_results.values()),
            "orderbook_missing_blocks": all(item["ok"] for item in orderbook_results.values()),
            "risk_cap_blocks": all(item["ok"] for item in cap_results.values()),
            "orderbook_refresh_segments_recorded": not any(
                blocker.startswith("execution_segment_orderbook") for blocker in blockers
            ),
            "risk_guard_segments_recorded": not any(
                blocker.startswith("execution_segment_risk") for blocker in blockers
            ),
            "pre_submit_refresh_and_cap_static_contract_present": bool(
                static_contract.get("execution_segments_preserve_orderbook_refresh")
                and static_contract.get("primary_fast_path_checks_orderbook_before_submit")
                and static_contract.get("primary_fast_path_runs_risk_guards_before_submit")
                and static_contract.get("primary_fast_path_persists_decision_before_submit")
                and static_contract.get("run_node_order_applies_buy_notional_cap")
            ),
            "stale_terminal_does_not_submit": not any(
                blocker.startswith("stale_timing_") for blocker in blockers
            ),
            "idempotency_contract_present": bool(
                static_contract.get("clob_service_has_idempotent_replay")
                and static_contract.get("clob_service_has_inflight_guard")
                and static_contract.get("clob_service_exposes_order_status")
            ),
        },
        "stale_results": stale_results,
        "duplicate_results": duplicate_results,
        "orderbook_results": orderbook_results,
        "cap_results": cap_results,
        "timing_contract": timing_contract,
        "execution_segment_contract": execution_segment_contract,
        "static_contract": static_contract,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": _guards(),
        "next_boundary": (
            "signal_to_order_safety_contract_ready"
            if not blockers
            else "fix_signal_to_order_safety_contract_before_release"
        ),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_audit(root=Path(args.root))
    if args.summary:
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "execution_mode": report["execution_mode"],
            "checks": report["checks"],
            "blockers": report["blockers"],
            "warnings": report["warnings"],
            "guards": report["guards"],
            "next_boundary": report["next_boundary"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
