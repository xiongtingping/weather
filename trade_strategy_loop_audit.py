#!/usr/bin/env python3
"""Read-only audit for reported-X -> bridge -> dynamic three-leg execution.

The retired AWC accepted-signal replay is not a production trading authority.
This audit composes the current probability identity, X->S->B bridge readiness,
dynamic main/second YES plus physically excluded NO lane, runtime policy, and
signal-to-order safety without placing orders or recalculating probabilities.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "trade_strategy_loop_audit_v2_reported_x_dynamic_three_leg"
SAFE_FAIL_CLOSED_PROBABILITY_PROMOTION_BLOCKERS = frozenset(
    {"reported_x_successor_runtime_manifest_drift"}
)


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_auto_orders": False,
        "fetches_external_weather_sources": False,
        "fetches_orderbook": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "executes_sql": False,
    }


def _extract_json(stdout: str) -> dict[str, Any]:
    text = str(stdout or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return {"ok": False, "blockers": ["subprocess_json_missing"]}
    try:
        parsed = json.loads(text[start : end + 1])
    except Exception as exc:
        return {"ok": False, "blockers": [f"subprocess_json_parse_failed:{type(exc).__name__}"]}
    return parsed if isinstance(parsed, dict) else {"ok": False, "blockers": ["subprocess_json_not_object"]}


def _run_json(cmd: Sequence[str], *, timeout_sec: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(1, int(timeout_sec)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "blockers": ["subprocess_timeout"],
            "stdout_preview": str(exc.stdout or "")[:500],
            "stderr_preview": str(exc.stderr or "")[:500],
        }
    report = _extract_json(proc.stdout)
    report.setdefault("returncode", proc.returncode)
    if proc.stderr:
        report.setdefault("stderr_preview", proc.stderr[:500])
    if proc.returncode != 0:
        report["ok"] = False
        report["blockers"] = list(report.get("blockers") or []) + ["subprocess_returncode_nonzero"]
    return report


def _blocker_codes(items: Any) -> list[str]:
    codes: list[str] = []
    for item in items or []:
        if isinstance(item, Mapping):
            code = str(item.get("blocker") or item.get("reason") or "").strip()
        else:
            code = str(item or "").strip()
        if code and code != "subprocess_returncode_nonzero":
            codes.append(code)
    return codes


def build_report(
    *,
    probability_report: Mapping[str, Any],
    market_report: Mapping[str, Any],
    execution_report: Mapping[str, Any],
    policy_report: Mapping[str, Any],
    safety_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Separate unsafe architecture defects from expected promotion blockers."""

    safety_blockers: list[Any] = []
    promotion_blockers: list[str] = []
    if probability_report.get("ok") is not True:
        probability_blockers = _blocker_codes(probability_report.get("blockers"))
        promotion_blockers.extend(
            code
            for code in probability_blockers
            if code in SAFE_FAIL_CLOSED_PROBABILITY_PROMOTION_BLOCKERS
        )
        unsafe_probability_blockers = [
            code
            for code in probability_blockers
            if code not in SAFE_FAIL_CLOSED_PROBABILITY_PROMOTION_BLOCKERS
        ]
        if unsafe_probability_blockers:
            safety_blockers.append(
                {"probability_mainline_safety_blockers": unsafe_probability_blockers}
            )
    promotion_blockers.extend(_blocker_codes(market_report.get("promotion_blockers")))
    promotion_blockers.extend(_blocker_codes(execution_report.get("live_activation_blockers")))
    if market_report.get("safety_blockers"):
        safety_blockers.append(
            {"market_safety_blockers": list(market_report.get("safety_blockers") or [])}
        )
    resident = execution_report.get("resident_deployment") if isinstance(
        execution_report.get("resident_deployment"), Mapping
    ) else {}
    database = execution_report.get("database") if isinstance(
        execution_report.get("database"), Mapping
    ) else {}
    unresolved = int(database.get("unresolved_intent_count") or 0)
    execution_fail_closed = bool(
        resident.get("all_live_flags_zero") is True
        and resident.get("persistent_disabled") is True
        and resident.get("loaded") is False
        and unresolved == 0
    )
    if not execution_fail_closed and execution_report.get("live_activation_ready") is not True:
        safety_blockers.append(
            {
                "execution_not_safely_fail_closed": {
                    "resident_deployment": dict(resident),
                    "unresolved_intent_count": unresolved,
                }
            }
        )
    if policy_report.get("ok") is not True:
        safety_blockers.append(
            {"runtime_policy_blockers": list(policy_report.get("blockers") or [])}
        )
    if safety_report.get("ok") is not True:
        safety_blockers.append(
            {"signal_to_order_safety_blockers": list(safety_report.get("blockers") or [])}
        )
    safety_checks = safety_report.get("checks") if isinstance(safety_report.get("checks"), Mapping) else {}
    live_ready = bool(
        not safety_blockers
        and not promotion_blockers
        and probability_report.get("ok") is True
        and market_report.get("production_market_projection_ready") is True
        and execution_report.get("live_activation_ready") is True
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": not safety_blockers,
        "state": "live_ready" if live_ready else "safe_fail_closed" if not safety_blockers else "unsafe_blocked",
        "live_ready": live_ready,
        "read_only": True,
        "checks": {
            "reported_x_probability_ready": probability_report.get("ok") is True,
            "market_bridge_safety_ok": not bool(market_report.get("safety_blockers")),
            "market_projection_ready": market_report.get("production_market_projection_ready") is True,
            "dynamic_three_leg_live_ready": execution_report.get("live_activation_ready") is True,
            "dynamic_three_leg_safe_fail_closed": execution_fail_closed,
            "runtime_policy_has_no_ev_edge_gate": policy_report.get("ok") is True,
            "signal_to_order_safety_ok": safety_report.get("ok") is True,
            "stale_decision_blocks": safety_checks.get("stale_decision_blocks") is True,
            "duplicate_order_blocks": safety_checks.get("duplicate_order_blocks") is True,
            "idempotency_contract_present": safety_checks.get("idempotency_contract_present") is True,
        },
        "safety_blockers": safety_blockers,
        "promotion_blockers": sorted(set(promotion_blockers)),
        "warnings": list(probability_report.get("warnings") or [])[:10]
        + list(market_report.get("warnings") or [])[:10]
        + list(safety_report.get("warnings") or [])[:10],
        "guards": _guards(),
        "next_boundary": (
            "dynamic_three_leg_live_ready"
            if live_ready
            else "resolve_promotion_blockers_without_weakening_fail_closed"
            if not safety_blockers
            else "fix_execution_safety_before_any_activation"
        ),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--event-date",
        default=datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
    )
    parser.add_argument("--timeout-sec", type=int, default=240)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    probability = _run_json(
        [
            sys.executable,
            "scripts/production_chain_uniqueness_audit.py",
            "--db", str(args.db),
            "--summary",
        ],
        timeout_sec=args.timeout_sec,
    )
    market = _run_json(
        [
            sys.executable,
            "scripts/production_reported_x_market_readiness.py",
            "--db", str(args.db),
            "--event-date", str(args.event_date),
        ],
        timeout_sec=args.timeout_sec,
    )
    execution = _run_json(
        [
            sys.executable,
            "scripts/production_dynamic_three_leg_readiness.py",
            "--db", str(args.db),
            "--summary",
        ],
        timeout_sec=args.timeout_sec,
    )
    policy = _run_json(
        [sys.executable, "scripts/production_trade_gate_zero_audit.py"],
        timeout_sec=args.timeout_sec,
    )
    safety = _run_json(
        [sys.executable, "scripts/signal_to_order_safety_contract_audit.py", "--summary"],
        timeout_sec=args.timeout_sec,
    )
    report = build_report(
        probability_report=probability,
        market_report=market,
        execution_report=execution,
        policy_report=policy,
        safety_report=safety,
    )
    if args.summary:
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "checks": report["checks"],
            "state": report["state"],
            "live_ready": report["live_ready"],
            "safety_blockers": report["safety_blockers"],
            "promotion_blockers": report["promotion_blockers"],
            "warnings": report["warnings"][:10],
            "guards": report["guards"],
            "next_boundary": report["next_boundary"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
