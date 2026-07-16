#!/usr/bin/env python3
"""Read-only AWC signal timing contract audit.

This audit narrows the signal-to-order replay contract to the hour-update
timing chain: AWC valid time, signal detection/fetch, SSOT materialization,
decision, job, execution prechecks, submit, and exchange ack. It uses only
in-memory fixtures plus the replay contract; it never writes archive.db, fetches
orderbooks, places orders, or restarts services.
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


SCHEMA_VERSION = "awc_signal_timing_contract_audit_v1"

REQUIRED_CONTRACT_TIMING_KEYS = (
    "awc_valid_time",
    "awc_hard_source_signal_at",
    "pre_signal_prepared_at",
    "awc_pre_signal_deadline_at",
    "fact_written_at",
    "posterior_written_at",
    "decision_written_at",
    "job_enqueued_at",
    "worker_started_at",
    "orderbook_checked_at",
    "risk_checked_at",
    "idempotency_checked_at",
    "submit_requested_at",
    "exchange_ack_at",
    "order_submitted_at",
)

REQUIRED_MISSING_POLICY_KEYS = (
    "accepted_signal_terminal_coverage_incomplete",
    "accepted_signal_base_timing_incomplete",
    "accepted_signal_preparation_coverage_incomplete",
    "submitted_signal_execution_safety_coverage_incomplete",
    "submitted_missing_orderbook_check",
    "submitted_missing_risk_check",
    "submitted_missing_idempotency_check",
    "submitted_missing_submit_request",
    "submitted_missing_exchange_ack",
    "submitted_precheck_after_submit_request",
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


def _required_components() -> dict[str, dict[str, Any]]:
    return {
        component: {"ok": True, "prepared_at": "2026-07-03T09:04:50Z"}
        for component in replay_audit.PRE_SIGNAL_REQUIRED_COMPONENTS
    }


def _full_timing_fixture() -> dict[str, Any]:
    return {
        "schema": "signal_to_order_timing_v1",
        "signal_id": "sig_awc_timing_contract",
        "decision_id": "decision_awc_timing_contract",
        "order_intent_id": "intent_awc_timing_contract",
        "source_event_id": "source_awc_timing_contract",
        "source_observation_id": "obs_awc_timing_contract",
        "fact_snapshot_id": "fact_awc_timing_contract",
        "posterior_run_id": "posterior_awc_timing_contract",
        "acceptance_state": "accepted",
        "acceptance_reason": "awc_hard_source_signal_accepted",
        "signal_eligible": True,
        "awc_valid_time": "2026-07-03T09:00:00Z",
        "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
        "preparation_started_at": "2026-07-03T09:00:00Z",
        "pre_signal_prepared_at": "2026-07-03T09:04:50Z",
        "awc_pre_signal_deadline_at": "2026-07-03T09:05:00Z",
        "pre_signal_prepared_before_deadline": True,
        "pre_signal_prepare_state": "prepared",
        "pre_signal_prepare_reason": "all_required_components_ready",
        "pre_signal_prepare_complete": True,
        "pre_signal_prepare_missing_components": [],
        "pre_signal_prepare_required_components": list(replay_audit.PRE_SIGNAL_REQUIRED_COMPONENTS),
        "pre_signal_prepare_components": _required_components(),
        "fact_written_at": "2026-07-03T09:05:38.100Z",
        "posterior_written_at": "2026-07-03T09:05:38.120Z",
        "decision_written_at": "2026-07-03T09:05:38.200Z",
        "job_enqueued_at": "2026-07-03T09:05:38.250Z",
        "worker_started_at": "2026-07-03T09:05:38.280Z",
        "orderbook_checked_at": "2026-07-03T09:05:38.300Z",
        "risk_checked_at": "2026-07-03T09:05:38.900Z",
        "idempotency_checked_at": "2026-07-03T09:05:39.100Z",
        "submit_requested_at": "2026-07-03T09:05:39.100Z",
        "exchange_ack_at": "2026-07-03T09:05:39.700Z",
        "order_ack_at": "2026-07-03T09:05:39.700Z",
        "order_ack_status": "submitted",
        "order_submitted_at": "2026-07-03T09:05:40Z",
        "terminal_state": "submitted",
        "terminal_reason": "exchange_ack_submitted",
        "terminal_at": "2026-07-03T09:05:40Z",
        "signal_to_order_slo_ms": 3000,
        "signal_to_order_slo_met": True,
    }


def _record_for_timing(timing: Mapping[str, Any]) -> dict[str, Any]:
    signal_id = str(timing.get("signal_id") or "sig_awc_timing_contract")
    return {
        "signal_id": signal_id,
        "sources": [
            "signal_to_order_timing",
            "event_fact_history",
            "auto_trade_decisions",
            "auto_trade_jobs",
        ],
        "timings": [{"timing": dict(timing), "source": "signal_to_order_timing"}],
        "terminal_states": [
            {
                "state": str(timing.get("terminal_state") or ""),
                "reason": str(timing.get("terminal_reason") or ""),
                "rank": replay_audit.TERMINAL_RANK.get(str(timing.get("terminal_state") or ""), 0),
            }
        ],
        "awc_hard_source_signal_at": str(timing.get("awc_hard_source_signal_at") or ""),
        "acceptance_state": str(timing.get("acceptance_state") or "accepted"),
        "signal_eligible": timing.get("signal_eligible", True),
        "decision_ids": [str(timing.get("decision_id") or "decision_awc_timing_contract")],
        "target_outcomes": ["37°C"],
        "hard_source_runtime_contract_versions": [],
    }


def timing_fixture_report(timing: Mapping[str, Any]) -> dict[str, Any]:
    finalized = replay_audit._finalize_signal(  # type: ignore[attr-defined]
        _record_for_timing(timing),
        current_runtime_contract_version="",
        fail_legacy_artifacts=True,
    )
    production_coverage = replay_audit._signal_to_order_production_coverage(  # type: ignore[attr-defined]
        [finalized],
        source_scans={"signal_to_order_timing": {"available": True, "rows": 1}},
        chain_coverage={"tables": {"signal_to_order_timing": True}},
    )
    required_contract = replay_audit._coverage_for_timing_keys(  # type: ignore[attr-defined]
        [finalized],
        REQUIRED_CONTRACT_TIMING_KEYS,
    )
    blockers_set = set(finalized.get("blockers") or []) | set(
        production_coverage.get("coverage_blockers") or []
    )
    if not required_contract.get("ok"):
        blockers_set.add("awc_signal_timing_required_keys_incomplete")
    blockers = sorted(blockers_set)
    return {
        "ok": not blockers,
        "blockers": blockers,
        "finalized_signal": {
            "signal_id": finalized.get("signal_id"),
            "final_terminal_state": finalized.get("final_terminal_state"),
            "blockers": finalized.get("blockers") or [],
            "warnings": finalized.get("warnings") or [],
        },
        "required_timing_coverage": required_contract,
        "production_coverage": production_coverage,
    }


def _without_keys(*keys: str) -> dict[str, Any]:
    timing = _full_timing_fixture()
    for key in keys:
        timing.pop(key, None)
    return timing


def build_audit(*, root: Path = ROOT) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []

    try:
        contract = replay_audit.ssot_chain_replay_input_contract()
    except Exception as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "read_only": True,
            "execution_mode": "awc_signal_timing_contract_audit_read_only",
            "blockers": ["ssot_chain_replay_contract_exception"],
            "warnings": [],
            "exception_type": type(exc).__name__,
            "exception": str(exc),
            "guards": _guards(),
        }

    slo = contract.get("awc_signal_to_order_slo") if isinstance(contract, Mapping) else {}
    slo_map = slo if isinstance(slo, Mapping) else {}
    contract_keys = {
        str(item or "")
        for item in (slo_map.get("required_timing_keys") or [])
        if str(item or "")
    }
    missing_contract_keys = [
        key for key in REQUIRED_CONTRACT_TIMING_KEYS if key not in contract_keys
    ]
    if missing_contract_keys:
        blockers.append("awc_timing_contract_required_keys_missing")

    missing_policy = contract.get("missing_policy") if isinstance(contract, Mapping) else {}
    policy_map = missing_policy if isinstance(missing_policy, Mapping) else {}
    missing_policy_keys = [
        key for key in REQUIRED_MISSING_POLICY_KEYS if key not in policy_map
    ]
    if missing_policy_keys:
        blockers.append("awc_timing_missing_policy_keys_missing")

    full_chain = timing_fixture_report(_full_timing_fixture())
    if not full_chain.get("ok"):
        blockers.append("awc_timing_full_fixture_does_not_pass")

    missing_valid_time = timing_fixture_report(_without_keys("awc_valid_time"))
    if missing_valid_time.get("ok"):
        blockers.append("missing_awc_valid_time_not_blocked")
    missing_ssot = timing_fixture_report(_without_keys("fact_written_at", "posterior_written_at"))
    if missing_ssot.get("ok"):
        blockers.append("missing_ssot_materialization_time_not_blocked")
    missing_decision = timing_fixture_report(_without_keys("decision_written_at"))
    if missing_decision.get("ok"):
        blockers.append("missing_decision_time_not_blocked")
    missing_job = timing_fixture_report(_without_keys("job_enqueued_at"))
    if missing_job.get("ok"):
        blockers.append("missing_job_time_not_blocked")
    missing_order = timing_fixture_report(
        _without_keys("orderbook_checked_at", "submit_requested_at", "exchange_ack_at", "order_submitted_at")
    )
    if missing_order.get("ok"):
        blockers.append("missing_order_execution_time_not_blocked")

    checks = {
        "contract_has_awc_valid_time": "awc_valid_time" in contract_keys,
        "contract_has_signal_detection_time": "awc_hard_source_signal_at" in contract_keys,
        "contract_has_ssot_times": {"fact_written_at", "posterior_written_at"}.issubset(contract_keys),
        "contract_has_decision_job_times": {"decision_written_at", "job_enqueued_at"}.issubset(contract_keys),
        "contract_has_order_times": {
            "orderbook_checked_at",
            "submit_requested_at",
            "exchange_ack_at",
            "order_submitted_at",
        }.issubset(contract_keys),
        "missing_policy_covers_aggregate_blockers": not missing_policy_keys,
        "full_chain_fixture_passes": bool(full_chain.get("ok")),
        "missing_awc_valid_time_blocks": not bool(missing_valid_time.get("ok")),
        "missing_ssot_materialization_time_blocks": not bool(missing_ssot.get("ok")),
        "missing_decision_time_blocks": not bool(missing_decision.get("ok")),
        "missing_job_time_blocks": not bool(missing_job.get("ok")),
        "missing_order_execution_time_blocks": not bool(missing_order.get("ok")),
    }

    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "awc_signal_timing_contract_audit_read_only",
        "required_contract_timing_keys": list(REQUIRED_CONTRACT_TIMING_KEYS),
        "missing_contract_timing_keys": missing_contract_keys,
        "required_missing_policy_keys": list(REQUIRED_MISSING_POLICY_KEYS),
        "missing_policy_keys": missing_policy_keys,
        "checks": checks,
        "fixture_reports": {
            "full_chain": full_chain,
            "missing_awc_valid_time": missing_valid_time,
            "missing_ssot_materialization_time": missing_ssot,
            "missing_decision_time": missing_decision,
            "missing_job_time": missing_job,
            "missing_order_execution_time": missing_order,
        },
        "blockers": sorted(set(blockers)),
        "warnings": warnings,
        "guards": _guards(),
        "next_boundary": "awc_signal_timing_contract_ready" if not blockers else "fix_awc_signal_timing_contract",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    report = build_audit(root=Path(args.root))
    if args.summary:
        report = {
            key: report.get(key)
            for key in (
                "schema",
                "ok",
                "read_only",
                "execution_mode",
                "checks",
                "blockers",
                "warnings",
                "guards",
                "next_boundary",
            )
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
