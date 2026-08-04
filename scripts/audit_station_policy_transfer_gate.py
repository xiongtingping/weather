#!/usr/bin/env python3
"""Freeze the M8-12 station-policy transfer gate.

The receipt separates closed historical OOS readiness from the stricter
formal/live promotion evidence.  Forward scorecards are observational only:
they never enter an OOS or promotion gate.  Formal/live review requires the
closed-day strict-as-of direct utility/execution scorecard instead.  This is
an audit: it never promotes a candidate or touches production state.
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

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.promotion_evidence import validate_station_policy_forward_scorecard  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from bayes_basket_target.station_policy_candidate import assert_candidate_bundle_boundary  # noqa: E402


DEFAULT_BUNDLE = ROOT / "output/audits/station_policy_candidate_bundle_v1.json"
DEFAULT_TRANSFER = ROOT / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/derived/station_hierarchical_basket_transfer_v1.json"
DEFAULT_TRANSFER_VALIDATION = ROOT / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/derived/station_hierarchical_basket_transfer_validation_v1.json"
DEFAULT_STRICT_ASOF = ROOT / "output/audits/bayes_basket_target_strict_asof_replay.json"
DEFAULT_FORMULA_VALIDATION = ROOT / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/derived/global_station_executable_formula_v1/validation.json"
DEFAULT_OUTPUT = ROOT / "output/audits/station_policy_transfer_gate_m8_12.json"
DEFAULT_FORWARD_SCORECARD = ROOT / "output/audits/bayes_basket_target_station_policy_forward_scorecard.json"
DEFAULT_EXACT_STAGE_SCORECARD = ROOT / "output/audits/station_policy_exact_stage_scorecard_45station_utility_contract_v2_20260802.json"


def _utility_execution_evidence(
    path: Path | None,
) -> dict[str, Any]:
    """Validate the non-substitutable direct utility/execution gate.

    The exact-stage report may describe a versioned utility contract while
    every fact remains ``UNBOUND``.  A contract declaration, weather outcome,
    probability or quote is not a fill/utility outcome and therefore cannot
    satisfy the formal/live gate.
    """

    result: dict[str, Any] = {
        "state": "MISSING",
        "path": str(path) if path is not None else None,
        "valid": False,
    }
    if path is None:
        return result
    try:
        scorecard = _load(path)
        checks = scorecard.get("checks")
        scores = (scorecard.get("input_counts") or {}).get("score_rows")
        if not isinstance(checks, dict) or not isinstance(scores, dict):
            raise ValueError("station_policy_utility_scorecard_shape_invalid")
        score_rows_complete = all(
            isinstance(scores.get(stage), dict)
            and all(
                int(scores[stage].get(family, 0)) > 0
                for family in ("target", "reserve", "cadence")
            )
            for stage in ("P0", "P0-M", "P1")
        )
        historical_only = (
            checks.get("historical_utility_oos_closed_awc_only") is True
            and checks.get("observational_candidate_not_oos_blocks_production_no_live")
            is False
        )
        valid = (
            scorecard.get("schema") == "station_policy_exact_stage_scorecard_v1"
            and historical_only
            and checks.get("policy_utility_metric_binding_complete") is True
            and checks.get("policy_utility_score_rows_emitted") is True
            and score_rows_complete
        )
        result = {
            "state": "VALID" if valid else "INCOMPLETE",
            "path": str(path),
            "sha256": _sha(path),
            "valid": valid,
            "metric_binding_complete": (
                checks.get("policy_utility_metric_binding_complete") is True
            ),
            "historical_closed_awc_only": historical_only,
            "score_rows_emitted": (
                checks.get("policy_utility_score_rows_emitted") is True
            ),
            "all_stage_family_score_rows_present": score_rows_complete,
        }
    except Exception as exc:
        result = {
            "state": "INVALID",
            "path": str(path),
            "valid": False,
            "reason": f"{type(exc).__name__}:{exc}",
        }
    return result


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load(path: Path) -> dict[str, Any]:
    value = load_bounded_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"mapping_required:{path}")
    return value


def build_gate(
    *,
    bundle_path: Path = DEFAULT_BUNDLE,
    transfer_path: Path = DEFAULT_TRANSFER,
    transfer_validation_path: Path = DEFAULT_TRANSFER_VALIDATION,
    strict_asof_path: Path = DEFAULT_STRICT_ASOF,
    formula_validation_path: Path = DEFAULT_FORMULA_VALIDATION,
    forward_scorecard_path: Path | None = None,
    exact_stage_scorecard_path: Path | None = DEFAULT_EXACT_STAGE_SCORECARD,
) -> dict[str, Any]:
    bundle = _load(bundle_path)
    assert_candidate_bundle_boundary(bundle)
    transfer = _load(transfer_path)
    transfer_validation = _load(transfer_validation_path)
    strict_asof = _load(strict_asof_path)
    formula_validation = _load(formula_validation_path)

    expected_station_ids = sorted({
        str(row.get("station_id") or "").strip()
        for row in (bundle.get("activations") or [])
        if str(row.get("station_id") or "").strip()
    })
    station_scorecard: dict[str, Any] = {
        "state": "MISSING",
        "path": str(forward_scorecard_path) if forward_scorecard_path else None,
        "valid": False,
    }
    if forward_scorecard_path is not None:
        try:
            scorecard = _load(forward_scorecard_path)
            validation = validate_station_policy_forward_scorecard(
                scorecard,
                expected_station_ids=expected_station_ids,
                expected_bundle_sha256=str(bundle.get("bundle_sha256") or ""),
            )
            station_scorecard = {
                "state": "VALID" if validation["valid"] else "INVALID_CHECKS",
                "path": str(forward_scorecard_path),
                "sha256": _sha(forward_scorecard_path),
                "valid": bool(validation["valid"]),
                "checks": validation["checks"],
                "scope": validation["scope"],
                "families": validation["families"],
            }
        except Exception as exc:
            station_scorecard = {
                "state": "INVALID",
                "path": str(forward_scorecard_path),
                "valid": False,
                "reason": f"{type(exc).__name__}:{exc}",
            }
    station_scorecard_valid = station_scorecard.get("valid") is True
    utility_execution_evidence = _utility_execution_evidence(
        exact_stage_scorecard_path
    )
    utility_execution_evidence_valid = (
        utility_execution_evidence.get("valid") is True
    )

    strict_windows = strict_asof.get("windows", {}).get("reports", [])
    crossseason = strict_asof.get("crossseason", {})
    transfer_summary = transfer.get("non_shenzhen_summary", {})
    transfer_checks = transfer_validation.get("checks", {})
    formula_metrics = formula_validation.get("metrics", {})

    family_metrics = {
        "target": {
            "available_upstream_oos_rows": int(formula_metrics.get("target_policy_holdout", {}).get("rows", 0)),
            "available_station_metric": "station_weight_mae",
            "audit_fallback_triggered": bool(formula_metrics.get("module_activation", {}).get("target_policy", {}).get("audit_fallback_triggered")),
        },
        "reserve": {
            "available_upstream_oos_rows": int(formula_metrics.get("reserve_holdout", {}).get("rows", 0)),
            "available_station_metric": "station_mae",
            "audit_fallback_triggered": False,
        },
        "cadence": {
            "available_upstream_oos_rows": int(formula_metrics.get("cadence_holdout", {}).get("rows", 0)),
            "available_station_metric": "station_mae",
            "audit_fallback_triggered": False,
        },
    }
    families = {}
    for family, metrics in family_metrics.items():
        if utility_execution_evidence_valid:
            families[family] = {
                slice_name: {
                    "status": "PASS",
                    "family_specific": True,
                    "evidence": "closed-day historical strict-as-of direct utility scorecard",
                }
                for slice_name in (
                    "same_station_time_oos",
                    "leave_one_station_out",
                    "cross_season",
                    "windows_3_7_15_30",
                    "per_station_baseline_and_interval",
                    "forward_shadow_and_pnl",
                )
            }
            families[family]["upstream_oos_summary"] = metrics
            families[family]["promotion_permitted"] = True
            continue
        families[family] = {
            "same_station_time_oos": {
                "status": "PARTIAL",
                "family_specific": False,
                "evidence": "pooled formula holdout exists but is not the required closed-day direct utility gate",
            },
            "leave_one_station_out": {
                "status": "PARTIAL",
                "family_specific": False,
                "evidence": "pooled behavior transfer only; target/reserve/cadence are not separately scored",
            },
            "cross_season": {
                "status": "NOT_EVALUATED",
                "family_specific": False,
                "evidence": "strict-as-of cross-season input evidence is upstream weather/model evidence, not policy-family OOS",
            },
            "windows_3_7_15_30": {
                "status": "NOT_EVALUATED",
                "family_specific": False,
                "evidence": "strict-as-of windows exist but no target/reserve/cadence scorecard is joined to them",
            },
            "per_station_baseline_and_interval": {
                "status": "NOT_EVALUATED",
                "family_specific": False,
                "evidence": "no family-specific per-station random/market baseline with uncertainty interval",
            },
            "upstream_oos_summary": metrics,
            "promotion_permitted": False,
        }

    checks = {
        "candidate_bundle_contract_valid": True,
        "candidate_bundle_promoted_false": bundle.get("promotion_permitted") is False,
        "strict_asof_input_pass": strict_asof.get("status") == "PASS",
        "strict_asof_crossseason_lineage_complete": crossseason.get("lineage_complete") is True,
        "strict_asof_four_seasons": crossseason.get("all_four_seasons_present") is True,
        "strict_asof_four_windows": len(strict_windows) == 4,
        "pooled_transfer_receipt_pass": transfer_validation.get("status") == "PASS",
        "pooled_transfer_station_count_is_eight": len(transfer.get("stations", [])) == 8,
        "pooled_transfer_not_pnl_or_settlement": set(transfer.get("method", {}).get("not_measured", [])) >= {
            "strategy PnL", "settlement win rate"
        },
        "family_specific_target_gate": utility_execution_evidence_valid,
        "family_specific_reserve_gate": utility_execution_evidence_valid,
        "family_specific_cadence_gate": utility_execution_evidence_valid,
        "station_policy_scorecard_valid": station_scorecard_valid,
        # Forward observations are operational telemetry only: they are not
        # OOS and never participate in formal/live promotion authority.
        "forward_scorecard_observational_only": True,
        "forward_scorecard_not_used_for_oos_or_promotion": True,
        "utility_execution_evidence_required_for_formal_live_promotion": (
            utility_execution_evidence_valid
        ),
        "promotion_permitted": False,
        "orders_submitted_zero": True,
        "writes_production_db_false": True,
    }
    blockers: list[str] = []
    historical_oos_checks = (
        "strict_asof_input_pass",
        "strict_asof_crossseason_lineage_complete",
        "strict_asof_four_seasons",
        "strict_asof_four_windows",
        "pooled_transfer_receipt_pass",
        "pooled_transfer_station_count_is_eight",
        "pooled_transfer_not_pnl_or_settlement",
    )
    historical_oos_gate_passed = all(checks[name] for name in historical_oos_checks)
    historical_oos_blockers = [
        name for name in historical_oos_checks if checks[name] is not True
    ]

    formal_live_promotion_checks = (
        *historical_oos_checks,
        "family_specific_target_gate",
        "family_specific_reserve_gate",
        "family_specific_cadence_gate",
        "utility_execution_evidence_required_for_formal_live_promotion",
    )
    formal_live_promotion_gate_passed = all(
        checks[name] for name in formal_live_promotion_checks
    )

    if not utility_execution_evidence_valid:
        blockers.append(
            "utility_execution_evidence_unbound_formal_live_promotion_only"
        )
    if strict_asof.get("status") != "PASS":
        blockers.append("strict_asof_input_missing_or_blocked")
    if crossseason.get("lineage_complete") is not True:
        blockers.append("strict_asof_crossseason_lineage_incomplete")
    if crossseason.get("all_four_seasons_present") is not True:
        blockers.append("strict_asof_four_seasons_missing")
    if len(strict_windows) != 4:
        blockers.append("strict_asof_3_7_15_30_windows_missing")
    if transfer_validation.get("status") != "PASS":
        blockers.append("pooled_transfer_validation_missing")
    if len(transfer.get("stations", [])) != 8:
        blockers.append("pooled_transfer_scope_invalid")
    # Compatibility alias: this audit has always reported whether the
    # stricter promotion gate has passed.  Both gates consume historical
    # evidence only; forward telemetry is deliberately absent from them.
    evidence_gate_passed = formal_live_promotion_gate_passed
    status = (
        "EVIDENCE_COMPLETE_NO_LIVE_REVIEW"
        if formal_live_promotion_gate_passed
        else (
            "HISTORICAL_OOS_READY_FORMAL_LIVE_PROMOTION_PENDING"
            if historical_oos_gate_passed
            else "BLOCKED_HISTORICAL_OOS_EVIDENCE"
        )
    )
    identity = {
        "schema": "station_policy_transfer_gate_m8_12_v1",
        "status": status,
        "candidate_bundle_sha256": bundle["bundle_sha256"],
        "candidate_scope": bundle.get("scope"),
        "candidate_station_count": bundle.get("station_count"),
        "candidate_artifact_count": bundle.get("artifact_count"),
        "candidate_activation_count": bundle.get("activation_count"),
        "strict_asof": {
            "receipt_sha256": strict_asof.get("receipt_sha256"),
            "station_days": crossseason.get("station_days"),
            "stations": crossseason.get("stations"),
            "seasons": crossseason.get("seasons"),
            "window_count": len(strict_windows),
            "maturity_key": strict_asof.get("maturity_key"),
        },
        "pooled_transfer": {
            "receipt_sha256": _sha(transfer_path),
            "validation_sha256": _sha(transfer_validation_path),
            "stations": len(transfer.get("stations", [])),
            "dynamic_holdout_orders": transfer_summary.get("dynamic_holdout_orders"),
            "dynamic_top1": transfer_summary.get("dynamic_top1"),
            "dynamic_random_top1": transfer_summary.get("dynamic_random_top1"),
        },
        "families": families,
        "station_policy_scorecard": station_scorecard,
        "utility_execution_evidence": utility_execution_evidence,
        "checks": checks,
        "blockers": blockers,
        "historical_oos_hard_gate_checks": list(historical_oos_checks),
        "historical_oos_hard_gate_blockers": historical_oos_blockers,
        "historical_oos_hard_gate_passed": historical_oos_gate_passed,
        "production_no_live_historical_oos_permitted": historical_oos_gate_passed,
        "formal_live_promotion_checks": list(formal_live_promotion_checks),
        "formal_live_promotion_gate_passed": formal_live_promotion_gate_passed,
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
        "--forward-scorecard",
        type=Path,
        default=None,
        help=(
            "Optional target-owned station-policy forward scorecard; absent "
            "or invalid is observational only and never gates promotion."
        ),
    )
    parser.add_argument(
        "--exact-stage-scorecard",
        type=Path,
        default=DEFAULT_EXACT_STAGE_SCORECARD,
        help=(
            "Read-only direct utility/execution scorecard required for "
            "formal/live promotion."
        ),
    )
    args = parser.parse_args()
    receipt = build_gate(
        forward_scorecard_path=args.forward_scorecard,
        exact_stage_scorecard_path=args.exact_stage_scorecard,
    )
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
