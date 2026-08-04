#!/usr/bin/env python3
"""Build the six deterministic cash-phase OOS boundary-fault candidates.

The resulting JSON is consumable by ``build_bayes_basket_target_cash_phase_oos_evidence``
as its ``--fault-cases`` input.  It proves only deterministic evaluator
boundaries.  It is not a market OOS observation, a fill record, HEAVY-phase
fact, training cutoff, promotion, or live authority.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.cash_phase import (  # noqa: E402
    BasketCashPhasePolicyArtifact,
)
from bayes_basket_target.cash_phase_oos import (  # noqa: E402
    REQUIRED_FAULT_CASES,
    build_cash_phase_oos_scorecard,
)
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


SCHEMA = "bayes_basket_target_cash_phase_fault_cases_v1"
EVIDENCE_CLASS = "FAULT_CASE_CANDIDATE"
AS_OF_UTC = "2026-08-01T10:06:21Z"
EVALUATION_START_UTC = "2026-07-28T00:00:00Z"
EVALUATION_END_UTC = "2026-08-01T00:00:00Z"
STATION_ID = "RKPK"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_cash_phase_fault_cases.json"
MAX_OUTPUT_BYTES = 512 * 1024


def _utc(value: str, *, name: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name}_timezone_required")
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _policy() -> BasketCashPhasePolicyArtifact:
    return BasketCashPhasePolicyArtifact.create(
        policy_version="cash-fault-candidate-rkpk-v1",
        station_id=STATION_ID,
        station_timezone="UTC",
        heavy_to_standard_local_time="12:00",
        standard_to_tail_local_time="18:00",
        cash_by_phase={"HEAVY": 3.0, "STANDARD": 2.0, "TAIL": 1.0},
        valid_from_utc="2026-07-01T00:00:00Z",
        valid_until_utc="2026-08-31T00:00:00Z",
        evidence_status="DESIGN_CANDIDATE",
        promoted=False,
    )


def _row(
    phase: str,
    *,
    event_id: str | None = None,
    observation_id: str | None = None,
    decision_at_utc: str | None = None,
) -> dict[str, Any]:
    clocks = {
        "HEAVY": "2026-07-28T00:00:00Z",
        "STANDARD": "2026-07-29T13:00:00Z",
        "TAIL": "2026-07-30T19:00:00Z",
    }
    decision = decision_at_utc or clocks[phase]
    identity = event_id or f"cash-fault-{phase.lower()}"
    policy = _policy()
    return {
        "event_id": identity,
        "observation_id": observation_id or f"obs-{identity}-{decision}",
        "station_id": STATION_ID,
        "station_timezone": "UTC",
        "phase": phase,
        "current_phase": phase,
        "event_date_local": "2026-07-31",
        "decision_at_utc": decision,
        "checkpoint_id": f"checkpoint-{identity}-{decision}",
        "policy_version": policy.policy_version,
        "policy_sha256": policy.content_sha256,
        "evidence_owner": "bayes_basket_target",
        "production_mode": "production_no_live",
        "evaluation_split": "OOS",
        "probabilities": {"winner": 1.0},
        "all_in_yes_ask": 0.20,
        "candidate_authorized": True,
        "fill_status": "NO_EXECUTION",
        "no_execution_policy": {"orders_submitted": 0},
        "execution_cost_usdc": 0.0,
        "realized_pnl_usdc": 0.0,
        "settlement_pnl_usdc": 0.0,
        "failure_cost_usdc": 0.0,
        "labels": [{
            "truth_revision_no": 1,
            "labeled_at_utc": "2026-08-01T01:00:00Z",
            "settlement_closed_at_utc": "2026-08-01T01:00:00Z",
            "winning_bucket": "winner",
            "truth_sha256": "a" * 64,
        }],
    }


def _valid_rows() -> list[dict[str, Any]]:
    return [_row(phase) for phase in ("HEAVY", "STANDARD", "TAIL")]


def _scorecard(rows: list[dict[str, Any]]) -> dict[str, Any]:
    policy = _policy()
    return build_cash_phase_oos_scorecard(
        rows,
        as_of_utc=AS_OF_UTC,
        policy_version=policy.policy_version,
        policy_promoted=policy.promoted,
        policy_evidence_status=policy.evidence_status,
        expected_station_id=STATION_ID,
        expected_station_timezone="UTC",
        evaluation_start_utc=EVALUATION_START_UTC,
        evaluation_end_utc=EVALUATION_END_UTC,
        policy_training_cutoff_utc=EVALUATION_START_UTC,
        minimum_unique_events_per_phase=1,
        fault_cases=[],
        strict_target_owned=True,
    )


def _half_open_start_included() -> tuple[bool, dict[str, Any]]:
    rows = _valid_rows()
    rows[0] = _row("HEAVY", decision_at_utc=EVALUATION_START_UTC)
    scorecard = _scorecard(rows)
    result = {
        "start_utc": EVALUATION_START_UTC,
        "decision_count": scorecard["scope"]["decision_count"],
        "heavy_observation_count": scorecard["phase_observations"]["HEAVY"]["count"],
        "blockers": scorecard["blockers"],
    }
    return (
        result["decision_count"] == 3
        and result["heavy_observation_count"] == 1
        and "no_asof_observations" not in result["blockers"],
        result,
    )


def _half_open_end_excluded() -> tuple[bool, dict[str, Any]]:
    rows = _valid_rows()
    rows.append(_row(
        "TAIL",
        event_id="exclusive-end",
        observation_id="obs-exclusive-end",
        decision_at_utc=EVALUATION_END_UTC,
    ))
    scorecard = _scorecard(rows)
    result = {
        "end_utc": EVALUATION_END_UTC,
        "decision_count": scorecard["scope"]["decision_count"],
        "tail_observation_count": scorecard["phase_observations"]["TAIL"]["count"],
        "blockers": scorecard["blockers"],
    }
    return (
        result["decision_count"] == 3
        and result["tail_observation_count"] == 1,
        result,
    )


def _future_label_excluded() -> tuple[bool, dict[str, Any]]:
    rows = _valid_rows()
    rows[0]["labels"] = [{
        "truth_revision_no": 1,
        "labeled_at_utc": "2026-08-02T00:00:00Z",
        "settlement_closed_at_utc": "2026-08-02T00:00:00Z",
        "winning_bucket": "winner",
        "truth_sha256": "b" * 64,
    }]
    scorecard = _scorecard(rows)
    result = {
        "as_of_utc": AS_OF_UTC,
        "blockers": scorecard["blockers"],
        "promotion_eligible": scorecard["checks"]["promotion_eligible"],
    }
    return (
        "future_label_excluded" in result["blockers"]
        and result["promotion_eligible"] is False,
        result,
    )


def _timezone_mismatch_rejected() -> tuple[bool, dict[str, Any]]:
    rows = _valid_rows()
    rows[0]["station_timezone"] = "Asia/Seoul"
    scorecard = _scorecard(rows)
    result = {
        "expected_timezone": "UTC",
        "observed_timezone": "Asia/Seoul",
        "blockers": scorecard["blockers"],
        "promotion_eligible": scorecard["checks"]["promotion_eligible"],
    }
    return (
        "station_timezone_mismatch" in result["blockers"]
        and result["promotion_eligible"] is False,
        result,
    )


def _phase_rewind_rejected() -> tuple[bool, dict[str, Any]]:
    rows = _valid_rows()
    rows.extend([
        _row(
            "TAIL",
            event_id="phase-rewind",
            observation_id="obs-phase-rewind-tail",
            decision_at_utc="2026-07-29T14:00:00Z",
        ),
        _row(
            "STANDARD",
            event_id="phase-rewind",
            observation_id="obs-phase-rewind-standard",
            decision_at_utc="2026-07-30T17:00:00Z",
        ),
    ])
    scorecard = _scorecard(rows)
    result = {
        "blockers": scorecard["blockers"],
        "promotion_eligible": scorecard["checks"]["promotion_eligible"],
    }
    return (
        "phase_nonmonotonic" in result["blockers"]
        and result["promotion_eligible"] is False,
        result,
    )


def _duplicate_conflict_rejected() -> tuple[bool, dict[str, Any]]:
    rows = _valid_rows()
    duplicate = deepcopy(rows[0])
    duplicate["observation_id"] = "obs-conflicting-copy"
    duplicate["all_in_yes_ask"] = 0.21
    rows.append(duplicate)
    scorecard = _scorecard(rows)
    result = {
        "blockers": scorecard["blockers"],
        "promotion_eligible": scorecard["checks"]["promotion_eligible"],
    }
    return (
        "duplicate_event_content_conflict" in result["blockers"]
        and result["promotion_eligible"] is False,
        result,
    )


_RUNNERS: Mapping[str, Callable[[], tuple[bool, dict[str, Any]]]] = {
    "half_open_start_included": _half_open_start_included,
    "half_open_end_excluded": _half_open_end_excluded,
    "future_label_excluded": _future_label_excluded,
    "timezone_mismatch_rejected": _timezone_mismatch_rejected,
    "phase_rewind_rejected": _phase_rewind_rejected,
    "duplicate_conflict_rejected": _duplicate_conflict_rejected,
}


def build_fault_cases(*, as_of_utc: str = AS_OF_UTC) -> dict[str, Any]:
    """Run exactly the six boundary contracts required by cash-phase OOS."""

    as_of = _utc(as_of_utc, name="as_of_utc")
    if as_of != AS_OF_UTC:
        raise ValueError("cash_phase_fault_case_as_of_must_match_fixture")
    if set(_RUNNERS) != REQUIRED_FAULT_CASES:
        raise RuntimeError("cash_phase_fault_case_runner_set_mismatch")
    rows: list[dict[str, Any]] = []
    for name in sorted(REQUIRED_FAULT_CASES):
        passed, result = _RUNNERS[name]()
        evidence = {
            "name": name,
            "passed": bool(passed),
            "evidence_owner": "bayes_basket_target",
            "evidence_class": EVIDENCE_CLASS,
            "result": result,
            "market_oos_evidence": False,
            "heavy_phase_observation": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
        rows.append({**evidence, "evidence_sha256": content_sha256(evidence)})
    core: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "PASS" if all(row["passed"] for row in rows) else "FAIL",
        "as_of_utc": as_of,
        "fault_cases": rows,
        "fault_case_count": len(rows),
        "required_fault_cases": sorted(REQUIRED_FAULT_CASES),
        "evidence_class": EVIDENCE_CLASS,
        "scope": {
            "deterministic_evaluator_only": True,
            "market_oos_evidence": False,
            "heavy_phase_observation": False,
            "promotion_evidence": False,
        },
        "formal_promotion_allowed": False,
        "live_activation_allowed": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**core, "content_sha256": content_sha256(core)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of-utc", default=AS_OF_UTC)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    artifact = build_fault_cases(as_of_utc=args.as_of_utc)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    write_bounded_json(output, artifact, limit=MAX_OUTPUT_BYTES)
    print(json.dumps({
        "schema": artifact["schema"],
        "status": artifact["status"],
        "fault_case_count": artifact["fault_case_count"],
        "content_sha256": artifact["content_sha256"],
        "formal_promotion_allowed": artifact["formal_promotion_allowed"],
        "orders_submitted": artifact["orders_submitted"],
        "output": str(output),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if artifact["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
