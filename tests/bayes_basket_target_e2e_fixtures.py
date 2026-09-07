#!/usr/bin/env python3
"""Shared immutable fixtures for DeltaBasketScore v2 end-to-end tests.

This deliberately uses synthetic, immutable envelopes and an in-memory boundary.
It does not open a production database, create an order gateway, or submit an order.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import (  # noqa: E402
    BasketCashPhaseInput,
    BasketCashPhasePolicyArtifact,
    GlobalBasketPriorArtifact,
    PolicyArtifact,
    ProbabilityEnvelope,
    StationBasketPolicyActivation,
)


DECISION = "2026-07-23T04:30:00Z"
VALID_FROM = "2026-07-01T00:00:00Z"
VALID_UNTIL = "2026-08-01T00:00:00Z"



def _envelope(stage: str, checkpoint: str) -> ProbabilityEnvelope:
    lineage = {
        "source": "e2e_zero_order_fixture",
        "stage": stage,
        "model_envelope_lower_bucket": "37",
        "model_envelope_upper_bucket": "39",
        "model_envelope_span_c": "2",
        # ZUUU settles in Celsius, so its ladder steps 1C.  A dispersion is
        # only spendable against the ladder it was measured on, and every
        # produced envelope carries this geometry.
        "bucket_intervals_c_json": (
            '{"37":[36.5,37.5],"38":[37.5,38.5],"39":[38.5,39.5]}'
        ),
    }
    if stage == "P1":
        lineage.update({
            "reported_high_bucket": "37",
            "reported_high_ordinal": "37",
            "canonical_observation_event_id": "e2e-observation-1",
            "canonical_observation_sha256": "a" * 64,
            "canonical_observation_committed_at": DECISION,
            "hard_bound_id": "e2e-hard-bound-1",
            "source_lane": "AMSC",
            "report_type": "METAR",
            "hard_excluded_buckets": "NONE",
        })
    return ProbabilityEnvelope.from_mapping({
        "stage": stage,
        "checkpoint_id": checkpoint,
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "Asia/Shanghai",
        "decision_at_utc": "2026-07-23T00:00:00Z",
        "fresh_until_utc": VALID_UNTIL,
        "bucket_ordinals": {"37": 37, "38": 38, "39": 39},
        "probabilities": {"37": 0.2, "38": 0.6, "39": 0.2},
        "posterior_version": "p1",
        "posterior_sha256": "c" * 64,
        "bridge_sha256": "b" * 64,
        "lineage": lineage,
    })


def _policy_inputs() -> tuple[
    GlobalBasketPriorArtifact,
    list[PolicyArtifact],
    list[StationBasketPolicyActivation],
]:
    prior = GlobalBasketPriorArtifact.create(
        global_prior_id="e2e-prior-v1",
        strategy_version="target-v1",
        family_parameters={"target": {}, "reserve": {"price_margin": 0.0}, "cadence": {}},
        default_policy={
            "total_budget_usdc": 12.0,
            "base_coverage": 0.5,
            "maximum_coverage": 0.7,
            "entropy_coverage_gain": 0.0,
            "disagreement_concentration_gain_per_layer": 0.50,
            "clip_min_notional_usdc": 0.5,
            "minimum_expected_pnl_after_usdc": -12.0,
            "protected_floor_usdc": -12.0,
            "max_outside_loss_usdc": 12.0,
        },
        valid_from_utc=VALID_FROM,
        valid_until_utc=VALID_UNTIL,
    )
    # Both maturities, because the scope match is exact equality on all five
    # dimensions and the cycle's maturity is DERIVED from the probability
    # rather than passed in -- deliberately, so a static config cannot
    # override it.  A fixture seeded only at d1 therefore fails closed on
    # every same-day cycle, which is what thirty-six tests in the
    # production-io suite were doing: FAIL_CLOSED_NO_STATION_POLICY before
    # any of them reached an assertion.  Seeding the other bin is additive;
    # a test that wants d1 still resolves d1.
    artifacts = [
        PolicyArtifact.create(
            artifact_id=(
                f"e2e-policy-{family}" if maturity == "d1"
                else f"e2e-policy-{family}-{maturity}"
            ),
            family=family,
            station_id="ZUUU",
            stage="P1",
            season_bin="summer",
            maturity_bin=maturity,
            liquidity_bin="liquid",
            strategy_version="target-v1",
            theta_local=(
                {"price_margin": 0.0}
                if family == "reserve"
                else {"gate_seconds_intercept": 300.0}
                if family == "cadence"
                else {}
            ),
            effective_sample_n=10.0,
            kappa=10.0,
            trained_through_utc="2026-07-22T00:00:00Z",
            valid_from_utc=VALID_FROM,
            valid_until_utc=VALID_UNTIL,
        )
        for maturity in ("d1", "d0")
        for family in ("target", "reserve", "cadence")
    ]
    activations = [
        StationBasketPolicyActivation.create(
            activation_id=(
                "e2e-policy-activation-v1" if maturity == "d1"
                else f"e2e-policy-activation-v1-{maturity}"
            ),
            station_id="ZUUU",
            stage="P1",
            season_bin="summer",
            maturity_bin=maturity,
            liquidity_bin="liquid",
            strategy_version="target-v1",
            global_prior_id=prior.global_prior_id,
            target_artifact_id=(
                "e2e-policy-target" if maturity == "d1"
                else f"e2e-policy-target-{maturity}"
            ),
            reserve_artifact_id=(
                "e2e-policy-reserve" if maturity == "d1"
                else f"e2e-policy-reserve-{maturity}"
            ),
            cadence_artifact_id=(
                "e2e-policy-cadence" if maturity == "d1"
                else f"e2e-policy-cadence-{maturity}"
            ),
            fallback_families=[],
            generation=1,
            valid_from_utc=VALID_FROM,
            valid_until_utc=VALID_UNTIL,
            activated_at_utc="2026-06-30T00:00:00Z",
        )
        for maturity in ("d1", "d0")
    ]
    return prior, artifacts, activations


def _cash_policy(version: str, cash: float) -> BasketCashPhasePolicyArtifact:
    return BasketCashPhasePolicyArtifact.create(
        policy_version=version,
        station_id="ZUUU",
        station_timezone="Asia/Shanghai",
        heavy_to_standard_local_time="19:54",
        standard_to_tail_local_time="09:58",
        cash_by_phase={"HEAVY": cash, "STANDARD": cash, "TAIL": cash},
        valid_from_utc=VALID_FROM,
        valid_until_utc=VALID_UNTIL,
        evidence_status="DESIGN_CANDIDATE",
    )


def _cash_input(policy: BasketCashPhasePolicyArtifact) -> BasketCashPhaseInput:
    return BasketCashPhaseInput.from_mapping({
        "policy_version": policy.policy_version,
        "station_id": policy.station_id,
        "current_phase": "HEAVY",
        "event_date_local": "2026-07-23",
        "observed_at_utc": DECISION,
        "station_timezone": policy.station_timezone,
        "checkpoint_id": f"{policy.policy_version}-checkpoint",
    })


def _market_snapshot() -> dict[str, Any]:
    return {
        "snapshot_id": "e2e-market-snapshot-v1",
        "sequence": 1,
        "observed_at": DECISION,
        "market_id": "e2e-market-v1",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "Asia/Shanghai",
        "quote_version": "quote-v1",
        "quote_observed_at": DECISION,
        "quote_expires_at": "2026-07-23T04:31:00Z",
        "quote_freshness_policy_sha256": "7" * 64,
        "quote_source": "OFFICIAL_CLOB_WS",
        "official_ws_quote_sha256": "4" * 64,
        "inventory_version": "inventory-v1",
        "inventory_sha256": "5" * 64,
        "order_state_version": "orders-v1",
        "order_state_sha256": "6" * 64,
        "remaining_budget_usdc": 12.0,
        "buckets": [
            {"bucket": "37", "bid_price": 0.10, "ask_levels": [{"price": 0.12, "shares": 100.0}], "maker_queue_ahead_shares": 1.0, "maker_queue_known": True},
            {"bucket": "38", "bid_price": 0.20, "ask_levels": [{"price": 0.22, "shares": 100.0}], "maker_queue_ahead_shares": 1.0, "maker_queue_known": True},
            {"bucket": "39", "bid_price": 0.10, "ask_levels": [{"price": 0.12, "shares": 100.0}], "maker_queue_ahead_shares": 1.0, "maker_queue_known": True},
        ],
        "checkpoint_changed": True,
        "previous_cadence_snapshot": {
            "observed_at_utc": "2026-07-23T03:55:00Z",
            "order_state": "FLAT",
            "hazard_score": 0.67,
            "safety_gates_passed": True,
            "candidate_present": True,
            "checkpoint_version": "checkpoint-0",
            "quote_version": "quote-v0",
            "fill_version": "inventory-v0",
            "time_gate_version": "time-v0",
        },
        "cadence_snapshot": {
            "observed_at_utc": DECISION,
            "order_state": "FLAT",
            "hazard_score": 0.67,
            "safety_gates_passed": True,
            "candidate_present": True,
            "checkpoint_version": "checkpoint-1",
            "quote_version": "quote-v1",
            "fill_version": "inventory-v1",
            "time_gate_version": "time-v1",
        },
    }

