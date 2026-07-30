#!/usr/bin/env python3
"""Materialize the 45-station target/reserve/cadence shadow candidate bundle.

The source formula is public-behavior evidence, not a live authority.  The
generated bundle is written to an audit path and every artifact remains
``promoted=false`` because the source formula's final audit includes a target
fallback and is not a production activation.
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
from bayes_basket_target.policy import GlobalBasketPriorArtifact, StationBasketPolicyActivation  # noqa: E402
from bayes_basket_target.station_policy_candidate import (  # noqa: E402
    SCHEMA,
    assert_candidate_bundle_boundary,
    build_activation_promotion_decision,
    build_global_prior_promotion_decision,
    build_station_policy_candidate,
)


FORMULA = ROOT / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/derived/global_station_executable_formula_v1/formula_coefficients.json"
DEFAULT_OUTPUT = ROOT / "output/audits/station_policy_candidate_bundle_v1.json"
VALID_FROM = "2026-07-23T00:00:00Z"
VALID_UNTIL = "2026-08-23T00:00:00Z"
TRAINED_THROUGH = "2026-07-17T23:59:59Z"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _float_map(value: dict[str, Any]) -> dict[str, float]:
    return {str(key): float(item) for key, item in value.items()}


def build_bundle(
    *,
    formula_path: Path = FORMULA,
    stage: str = "P0-M",
    season_bin: str = "summer",
    maturity_bin: str = "d1",
    liquidity_bin: str = "liquid",
    strategy_version: str = "target-v1",
) -> dict[str, Any]:
    payload = json.loads(formula_path.read_text(encoding="utf-8"))
    formula_sha256 = _file_sha256(formula_path)
    formula_lineage = {
        "source_formula_sha256": formula_sha256,
        "source_formula_schema": str(payload["schema"]),
        "source_scope_sha256": content_sha256(payload["scope"]),
        "source_evidence_boundary_sha256": content_sha256(payload["evidence_boundary"]),
        "train_boundary": str(payload["scope"]["train_boundary"]),
        "holdout_boundary": str(payload["scope"]["holdout_boundary"]),
        "promotion_slice": str(payload["scope"]["promotion_slice"]),
        "final_audit_slice": str(payload["scope"]["final_audit_slice"]),
    }
    formula = payload["formula"]
    target = formula["target_policy"]
    reserve = formula["reserve_price"]
    cadence = formula["cadence"]
    stations = sorted(target["station_slope_artifacts"])

    global_prior = GlobalBasketPriorArtifact.create(
        global_prior_id="global-station-formula-v1-shadow-prior",
        strategy_version=strategy_version,
        family_parameters={
            "target": _float_map(target["coefficients"]),
            "reserve": {"price_margin": float(reserve["global_median_discount_usdc"])},
            "cadence": {"gate_seconds_intercept": float(cadence["intercept"])},
        },
        default_policy={
            "total_budget_usdc": 12.0,
            "allocation_mode": "candidate_only",
            "station_formula_source": payload["schema"],
        },
        valid_from_utc=VALID_FROM,
        valid_until_utc=VALID_UNTIL,
        promoted=False,
    )
    global_prior_promotion_decision = build_global_prior_promotion_decision(
        prior=global_prior,
        oos_checks={
            "feature_spec_frozen": True,
            "strict_asof_oos_complete": False,
            "forward_shadow_oos_complete": False,
            "station_transfer_by_family_complete": False,
        },
        lineage={**formula_lineage, "artifact_role": "global_prior"},
        promotion_requested=False,
    )

    candidates: list[dict[str, Any]] = []
    activation_rows: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    activation_promotion_decisions: list[dict[str, Any]] = []
    for station_id in stations:
        target_station = target["station_slope_artifacts"][station_id]
        target_theta = {
            key: float(value)
            for key, value in target_station["posterior_coefficients"].items()
        }
        reserve_station = reserve["station_intercepts"][station_id]
        cadence_station = cadence["station_intercepts"][station_id]
        family_inputs = (
            (
                "target",
                target_theta,
                float(target_station["n_eff_events"]),
                float(target_station["kappa"]),
                {"direction_preserved": True, "target_audit_fallback_absent": False},
            ),
            (
                "reserve",
                {"price_margin": float(reserve["global_median_discount_usdc"]) + float(reserve_station["posterior_offset"])},
                float(reserve_station["n_eff"]),
                float(reserve_station["n_eff"] * (1.0 - reserve_station["rho"]) / reserve_station["rho"]),
                {"math_cap_min_preserved": True, "b14_breakthroughs_zero": True},
            ),
            (
                "cadence",
                {"gate_seconds_intercept": float(cadence["intercept"]) + float(cadence_station["posterior_offset"])},
                float(cadence_station["n_eff"]),
                float(cadence_station["n_eff"] * (1.0 - cadence_station["rho"]) / cadence_station["rho"]),
                {"opens_time_gate_only": True, "b13_b14_bypass": False},
            ),
        )
        ids: dict[str, str] = {}
        artifact_content_shas: dict[str, str] = {}
        for family, theta_local, sample_n, kappa, checks in family_inputs:
            artifact_id = f"station-formula-v1-{station_id}-{family}"
            candidate = build_station_policy_candidate(
                candidate_id=f"candidate-{artifact_id}",
                artifact_id=artifact_id,
                family=family,
                station_id=station_id,
                stage=stage,
                season_bin=season_bin,
                maturity_bin=maturity_bin,
                liquidity_bin=liquidity_bin,
                strategy_version=strategy_version,
                theta_local=theta_local,
                effective_sample_n=sample_n,
                kappa=kappa,
                trained_through_utc=TRAINED_THROUGH,
                valid_from_utc=VALID_FROM,
                valid_until_utc=VALID_UNTIL,
                oos_checks=checks,
                lineage={
                    **formula_lineage,
                    "artifact_role": f"station_{family}",
                    "station_id": station_id,
                },
                promotion_requested=False,
            )
            ids[family] = artifact_id
            candidates.append(candidate["artifact"].to_dict())
            decisions.append(candidate["promotion_decision"])
            artifact_content_shas[family] = candidate["artifact"].content_sha256
        activation = StationBasketPolicyActivation.create(
            activation_id=f"station-formula-v1-{station_id}-activation",
            station_id=station_id,
            stage=stage,
            season_bin=season_bin,
            maturity_bin=maturity_bin,
            liquidity_bin=liquidity_bin,
            strategy_version=strategy_version,
            global_prior_id=global_prior.global_prior_id,
            target_artifact_id=ids["target"],
            reserve_artifact_id=ids["reserve"],
            cadence_artifact_id=ids["cadence"],
            fallback_families=[],
            generation=1,
            valid_from_utc=VALID_FROM,
            valid_until_utc=VALID_UNTIL,
            activated_at_utc=VALID_FROM,
            promoted=False,
        )
        activation_rows.append(activation.to_dict())
        activation_promotion_decision = build_activation_promotion_decision(
            activation=activation,
            global_prior_content_sha256=global_prior.content_sha256,
            artifact_content_sha256_by_family=artifact_content_shas,
            oos_checks={
                "exact_three_family_binding": True,
                "global_prior_lineage_bound": True,
                "forward_shadow_oos_complete": False,
                "station_transfer_by_family_complete": False,
            },
            lineage={**formula_lineage, "artifact_role": "station_activation", "station_id": station_id},
            promotion_requested=False,
        )
        activation_promotion_decisions.append(activation_promotion_decision)

    bundle_identity = {
        "schema": SCHEMA,
        "source_formula_schema": payload["schema"],
        "source_formula_sha256": formula_sha256,
        "lineage": formula_lineage,
        "scope": {
            "stage": stage,
            "season_bin": season_bin,
            "maturity_bin": maturity_bin,
            "liquidity_bin": liquidity_bin,
            "strategy_version": strategy_version,
        },
        "station_count": len(stations),
        "family_count": 3,
        "artifact_count": len(candidates),
        "activation_count": len(activation_rows),
        "global_prior": global_prior.to_dict(),
        "global_prior_promotion_decision": global_prior_promotion_decision,
        "artifacts": candidates,
        "activations": activation_rows,
        "promotion_decisions": decisions,
        "activation_promotion_decisions": activation_promotion_decisions,
        "promotion_permitted": False,
        "promotion_blockers": [
            "candidate_bundle_default_off",
            "target_final_audit_fallback_triggered",
            "forward_shadow_oos_not_complete",
            "production_activation_not_requested",
        ],
        "live_order_authorized": False,
        "production_mutation": False,
        "orders_submitted": 0,
        "writes_production_db": False,
    }
    bundle = {**bundle_identity, "bundle_sha256": content_sha256(bundle_identity)}
    assert_candidate_bundle_boundary(bundle)
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formula", type=Path, default=FORMULA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stage", default="P0-M")
    parser.add_argument("--season-bin", default="summer")
    parser.add_argument("--maturity-bin", default="d1")
    parser.add_argument("--liquidity-bin", default="liquid")
    parser.add_argument("--strategy-version", default="target-v1")
    args = parser.parse_args()
    bundle = build_bundle(
        formula_path=args.formula,
        stage=args.stage,
        season_bin=args.season_bin,
        maturity_bin=args.maturity_bin,
        liquidity_bin=args.liquidity_bin,
        strategy_version=args.strategy_version,
    )
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "schema": bundle["schema"],
        "bundle_sha256": bundle["bundle_sha256"],
        "station_count": bundle["station_count"],
        "artifact_count": bundle["artifact_count"],
        "activation_count": bundle["activation_count"],
        "promotion_permitted": bundle["promotion_permitted"],
        "orders_submitted": bundle["orders_submitted"],
        "writes_production_db": bundle["writes_production_db"],
        "output": str(output),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
