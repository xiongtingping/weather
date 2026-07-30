"""Build the minimal unpromoted policy packet for target no-live evidence."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from .cash_phase import BasketCashPhasePolicyArtifact
from .cash_phase_authority import CashPhaseAuthorityManifest
from .contracts import ContractError, content_sha256
from .policy import GlobalBasketPriorArtifact, StationBasketPolicyActivation
from .station_policy_candidate import assert_candidate_bundle_boundary


SCHEMA = "bayes_basket_target_no_live_candidate_artifacts_v1"
INGRESS_SCHEMA = "bayes_basket_target_artifact_ingress_v1"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_CANDIDATE"


def _station_timezones(
    conn: sqlite3.Connection,
    station_ids: set[str],
) -> dict[str, str]:
    if not station_ids:
        raise ContractError("candidate_policy_station_scope_empty")
    placeholders = ",".join("?" for _ in station_ids)
    rows = conn.execute(
        "SELECT station_id,settlement_timezone "
        "FROM basket_target_market_registry "
        f"WHERE station_id IN ({placeholders}) "
        "GROUP BY station_id,settlement_timezone ORDER BY station_id",
        tuple(sorted(station_ids)),
    ).fetchall()
    by_station: dict[str, set[str]] = {}
    for station_id, timezone_name in rows:
        by_station.setdefault(str(station_id), set()).add(
            str(timezone_name or "").strip()
        )
    mixed = sorted(
        station_id
        for station_id, values in by_station.items()
        if len(values) != 1 or not next(iter(values))
    )
    if mixed:
        raise ContractError(
            "candidate_policy_market_settlement_timezone_not_unique:"
            + ",".join(mixed)
        )
    result = {
        station_id: next(iter(values))
        for station_id, values in by_station.items()
    }
    missing = sorted(station_ids - set(result))
    if missing:
        raise ContractError("candidate_policy_station_timezone_missing:" + ",".join(missing))
    return result


def build_no_live_candidate_artifact_packet(
    conn: sqlite3.Connection,
    *,
    candidate_bundle: Mapping[str, Any],
    station_ids: set[str],
    stages_by_station: Mapping[str, set[str]] | None = None,
    maturity_bins_by_station_stage: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    """Bind candidate policy and self-owned cash clips to formal station rows.

    Cash amounts are conservative design-candidate clip ceilings. They are
    not source-wallet parameters, cannot modify P0/P1 or candidate support,
    and remain unpromoted until own forward/OOS evidence exists.
    """

    assert_candidate_bundle_boundary(candidate_bundle)
    timezones = _station_timezones(conn, station_ids)
    artifacts = [
        dict(row) for row in candidate_bundle["artifacts"]
        if str(row.get("station_id") or "") in station_ids
    ]
    activations = [
        dict(row) for row in candidate_bundle["activations"]
        if str(row.get("station_id") or "") in station_ids
    ]
    if len(artifacts) != 3 * len(station_ids) or len(activations) != len(station_ids):
        raise ContractError("candidate_policy_station_family_coverage_incomplete")
    source_prior = dict(candidate_bundle["global_prior"])
    global_prior = GlobalBasketPriorArtifact.create(
        # v3 removes the superseded fixed role-allocation fields.  Reusing
        # the v2 identity would conflict with append-only artifact lineage.
        global_prior_id="target-no-live-global-prior-design-v3",
        strategy_version=str(source_prior["strategy_version"]),
        family_parameters={"target": {}, "reserve": {}, "cadence": {}},
        default_policy={
            "total_budget_usdc": 12.0,
            "basket_downside_lambda_per_usdc": 0.02,
            "basket_directional_coverage_lambda_usdc": 0.05,
            "basket_concentration_lambda_usdc": 0.25,
            "disagreement_concentration_gain_per_layer": 0.50,
            "maximum_bucket_budget_fraction": 0.75,
            "basket_optimizer_max_iterations": 4000,
            "basket_optimizer_tolerance": 1e-8,
            "clip_min_notional_usdc": 1.0,
            "clip_max_notional_usdc": 3.0,
        },
        valid_from_utc=str(source_prior["valid_from_utc"]),
        valid_until_utc=str(source_prior["valid_until_utc"]),
        promoted=False,
    )
    activations = [
        StationBasketPolicyActivation.create(
            **{
                **{
                    key: value
                    for key, value in row.items()
                    if key not in {
                        "schema", "content_sha256", "activation_id",
                        "global_prior_id", "generation", "promoted"
                    }
                },
                "activation_id": str(row["activation_id"]) + "-no-live-v3",
                "global_prior_id": global_prior.global_prior_id,
                "generation": int(row["generation"]) + 3,
                "promoted": False,
            }
        ).to_dict()
        for row in activations
    ]
    source_activation_by_station = {
        str(row["station_id"]): row for row in activations
    }
    fallback_activations: list[dict[str, Any]] = []
    for station_id in sorted(station_ids):
        source = source_activation_by_station[station_id]
        requested_scopes = {
            (
                stage,
                str(
                    (maturity_bins_by_station_stage or {}).get(
                        (station_id, stage),
                        source["maturity_bin"],
                    )
                ),
            )
            for stage in (stages_by_station or {}).get(station_id, set())
        }
        for stage, maturity_bin in sorted(requested_scopes):
            if (
                stage == str(source["stage"])
                and maturity_bin == str(source["maturity_bin"])
            ):
                continue
            scope_token = f"{stage}-{maturity_bin}".replace("_", "-")
            fallback_activations.append(
                StationBasketPolicyActivation.create(
                    activation_id=(
                        "target-no-live-global-fallback-v4-"
                        f"{station_id}-{scope_token}"
                    ),
                    station_id=station_id,
                    stage=stage,
                    season_bin=str(source["season_bin"]),
                    maturity_bin=maturity_bin,
                    liquidity_bin=str(source["liquidity_bin"]),
                    strategy_version=str(source["strategy_version"]),
                    global_prior_id=global_prior.global_prior_id,
                    target_artifact_id=None,
                    reserve_artifact_id=None,
                    cadence_artifact_id=None,
                    fallback_families=["target", "reserve", "cadence"],
                    generation=2,
                    valid_from_utc=str(source["valid_from_utc"]),
                    valid_until_utc=str(source["valid_until_utc"]),
                    activated_at_utc=str(source["activated_at_utc"]),
                    promoted=False,
                ).to_dict()
            )
    activations.extend(fallback_activations)
    cash_policies = []
    cash_phase_authority_manifests = []
    for station_id in sorted(station_ids):
        policy = BasketCashPhasePolicyArtifact.create(
            policy_version=(
                "source-clip-clock-v20260716-17-partial-"
                f"candidate-{station_id}"
            ),
            station_id=station_id,
            station_timezone=timezones[station_id],
            heavy_to_standard_local_time="19:54",
            standard_to_tail_local_time="09:58",
            # The current source version has no observed HEAVY sample.
            # Omitting it makes evaluation fail closed instead of inventing
            # a cash template before the STANDARD boundary.
            cash_by_phase={"STANDARD": 10.77, "TAIL": 0.25},
            valid_from_utc=str(candidate_bundle["global_prior"]["valid_from_utc"]),
            valid_until_utc=str(candidate_bundle["global_prior"]["valid_until_utc"]),
            evidence_status="DESIGN_CANDIDATE",
            promoted=False,
        )
        cash_policies.append(policy.to_dict())
        cash_phase_authority_manifests.append(
            CashPhaseAuthorityManifest.create(
                station_id=station_id,
                policy_version=policy.policy_version,
                artifact_sha256=policy.content_sha256,
                station_timezone=policy.station_timezone,
                valid_from_utc=policy.valid_from_utc,
                valid_until_utc=policy.valid_until_utc,
                authority_scope=AUTHORITY_SCOPE,
            ).to_dict()
        )
    identity = {
        "schema": INGRESS_SCHEMA,
        "producer_schema": SCHEMA,
        "policy_authority_scope": AUTHORITY_SCOPE,
        "execution_mode": "production_no_live",
        "candidate_bundle_sha256": str(candidate_bundle["bundle_sha256"]),
        "station_ids": sorted(station_ids),
        "global_priors": [global_prior.to_dict()],
        "policy_artifacts": artifacts,
        "policy_activations": activations,
        "cash_phase_policies": cash_policies,
        "cash_phase_authority_manifests": cash_phase_authority_manifests,
        "policy_promotions": [],
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [],
        "ensemble_artifacts": [],
        "calibration_activations": [],
        "calibration_promotions": [],
        "promoted": False,
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "cash_phase_boundary": {
            "controls_clip_ceiling_only": True,
            "changes_probability": False,
            "changes_support_set": False,
            "changes_target_shares": False,
            "source_wallet_parameters_used": False,
        },
        "research_parameter_boundary": {
            "source_candidate_bundle_retained_as_lineage": True,
            "unsupported_target_coefficients_not_executed": True,
            "unsupported_cadence_coefficients_not_executed": True,
            "research_coefficients_promoted": False,
            "self_owned_baseline_prior": global_prior.global_prior_id,
        },
    }
    return {**identity, "packet_sha256": content_sha256(identity)}


__all__ = [
    "AUTHORITY_SCOPE",
    "INGRESS_SCHEMA",
    "SCHEMA",
    "build_no_live_candidate_artifact_packet",
]
