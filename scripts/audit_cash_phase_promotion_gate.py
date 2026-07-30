#!/usr/bin/env python3
"""Freeze the M8-13 cash-phase promotion gate offline.

The source-wallet replay and the system's own zero-order mapping are separate
evidence classes.  This receipt records both and refuses promotion until a
forward scorecard covers price, candidate fills, attribution, PnL and fault
boundaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.promotion_evidence import validate_cash_phase_forward_scorecard
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json


SOURCE_HEADLINE = ROOT / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/derived/recent_7d_clip_policy_versions_v2/headline.json"
SOURCE_VALIDATION = ROOT / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/derived/recent_7d_clip_policy_versions_v2/validation.json"
SOURCE_PURE = ROOT / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/derived/recent_7d_clip_policy_versions_v2/pure_function_validation.json"
SOURCE_OLD_PURE = ROOT / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/derived/recent_7d_basket_phase_thresholds_v1/pure_function_validation.json"
OWN_PRODUCTION_NO_LIVE = ROOT / "output/audits/bayes_basket_target_production_no_live_acceptance.json"
DEFAULT_OUTPUT = ROOT / "output/audits/cash_phase_promotion_gate_m8_13.json"
DEFAULT_FORWARD_SCORECARD = ROOT / "output/audits/bayes_basket_target_cash_phase_forward_scorecard.json"


def _load(path: Path) -> dict[str, Any]:
    value = load_bounded_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"mapping_required:{path}")
    return value


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_gate(*, own_forward_scorecard_path: Path | None = None) -> dict[str, Any]:
    headline = _load(SOURCE_HEADLINE)
    validation = _load(SOURCE_VALIDATION)
    pure = _load(SOURCE_PURE)
    old_pure = _load(SOURCE_OLD_PURE)
    own_production_no_live = _load(OWN_PRODUCTION_NO_LIVE)
    source_checks = validation.get("checks", {})
    pure_checks = pure.get("checks", {})
    own_checks = own_production_no_live.get("checks", {})

    own_forward_scorecard: dict[str, Any] = {
        "state": "MISSING",
        "path": str(own_forward_scorecard_path) if own_forward_scorecard_path else None,
        "valid": False,
    }
    if own_forward_scorecard_path is not None:
        try:
            scorecard = _load(own_forward_scorecard_path)
            validation = validate_cash_phase_forward_scorecard(scorecard)
            own_forward_scorecard = {
                "state": "VALID" if validation["valid"] else "INVALID_CHECKS",
                "path": str(own_forward_scorecard_path),
                "sha256": _sha(own_forward_scorecard_path),
                "valid": bool(validation["valid"]),
                "checks": validation["checks"],
                "scope": validation["scope"],
            }
        except Exception as exc:
            own_forward_scorecard = {
                "state": "INVALID",
                "path": str(own_forward_scorecard_path),
                "valid": False,
                "reason": f"{type(exc).__name__}:{exc}",
            }
    own_forward_valid = own_forward_scorecard.get("valid") is True

    checks = {
        "source_behavior_validation_pass": validation.get("status") == "PASS",
        "source_behavior_19_of_19": validation.get("passed") == 19 and validation.get("total") == 19,
        "source_old_policy_pure_33_of_33": old_pure.get("passed") == 33 and old_pure.get("total") == 33,
        "source_new_policy_pure_27_of_27": pure.get("passed") == 27 and pure.get("total") == 27,
        "source_heavy_unobserved_fail_closed": source_checks.get("heavy_phase_explicitly_unobserved") is True
        and pure_checks.get("new_heavy_phase_fails_closed") is True,
        "own_zero_order_production_no_live_mapping_pass": own_production_no_live.get("schema") == "bayes_basket_target_production_no_live_acceptance_v1"
        and own_checks.get("orders_submitted_zero") is True
        and own_checks.get("writes_production_db_false") is True,
        "own_forward_price_coverage_scorecard": own_forward_valid,
        "own_forward_candidate_fill_scorecard": own_forward_valid,
        "own_forward_pnl_attribution_scorecard": own_forward_valid,
        "own_forward_fault_boundary_scorecard": own_forward_valid,
        "own_forward_phase_coverage_scorecard": own_forward_valid,
        "own_forward_scorecard_valid": own_forward_valid,
        "promotion_permitted": False,
        "orders_submitted_zero": True,
        "writes_production_db_false": True,
    }
    blockers = [
        "source_wallet_replay_is_not_own_policy_evidence",
    ]
    if not own_forward_valid:
        blockers.extend([
            "own_forward_scorecard_missing_or_invalid",
            "own_forward_price_coverage_missing",
            "own_forward_candidate_fill_missing",
            "own_forward_pnl_attribution_missing",
            "own_forward_fault_scorecard_missing",
            "new_policy_heavy_phase_unobserved",
        ])
    evidence_checks = (
        "own_forward_price_coverage_scorecard",
        "own_forward_candidate_fill_scorecard",
        "own_forward_pnl_attribution_scorecard",
        "own_forward_fault_boundary_scorecard",
        "own_forward_phase_coverage_scorecard",
        "own_forward_scorecard_valid",
    )
    evidence_gate_passed = all(checks[name] for name in evidence_checks)
    status = "EVIDENCE_COMPLETE_NO_LIVE_REVIEW" if evidence_gate_passed else "BLOCKED_EVIDENCE"
    identity = {
        "schema": "cash_phase_promotion_gate_m8_13_v1",
        "status": status,
        "source_evidence": {
            "headline_sha256": _sha(SOURCE_HEADLINE),
            "validation_sha256": _sha(SOURCE_VALIDATION),
            "pure_validation_sha256": _sha(SOURCE_PURE),
            "old_pure_validation_sha256": _sha(SOURCE_OLD_PURE),
            "scope": headline.get("scope"),
            "orders": headline.get("scope", {}).get("maker_yes_buy_orders"),
            "stations": headline.get("scope", {}).get("stations"),
            "observed_templates": headline.get("new_partial_policy"),
        },
        "own_production_no_live_evidence": {
            "acceptance_sha256": _sha(OWN_PRODUCTION_NO_LIVE),
            "schema": own_production_no_live.get("schema"),
            "orders_submitted": own_production_no_live.get("orders_submitted", 0),
            "writes_production_db": own_production_no_live.get("writes_production_db", False),
        },
        "own_forward_scorecard": own_forward_scorecard,
        "checks": checks,
        "blockers": blockers,
        "evidence_gate_passed": evidence_gate_passed,
        "promotion_permitted": False,
        "live_order_authorized": False,
        "orders_submitted": 0,
        "writes_production_db": False,
        "calls_order_gateway": False,
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--own-forward-scorecard",
        type=Path,
        default=None,
        help="Optional target-owned forward scorecard; absent means fail closed.",
    )
    args = parser.parse_args()
    receipt = build_gate(own_forward_scorecard_path=args.own_forward_scorecard)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    write_bounded_json(output, receipt)
    print(json.dumps({
        "schema": receipt["schema"],
        "status": receipt["status"],
        "receipt_sha256": receipt["receipt_sha256"],
        "promotion_permitted": receipt["promotion_permitted"],
        "orders_submitted": receipt["orders_submitted"],
        "output": str(output),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
