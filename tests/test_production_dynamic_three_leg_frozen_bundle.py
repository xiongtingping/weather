from __future__ import annotations

from production_dynamic_three_leg_frozen_bundle import (
    build_production_dynamic_three_leg_frozen_bundle,
)
from settlement_window_runtime import load_approved_settlement_window_policy
from top_yes_wave_reentry_runtime import build_top_yes_wave_reentry_state


NOW = "2026-07-13T00:00:00.500Z"


def _outbox() -> dict:
    return {"slug": "highest-temperature-in-chengdu-on-july-13-2026", "event_date": "2026-07-13", "posterior_run_id": "post-current", "fact_snapshot_id": "fact-current"}


def _realization() -> dict:
    return {
        "posterior_run_id": "post-current", "fact_snapshot_id": "fact-current",
        "realization_state_id": "realization-current",
        "market_bucket_probabilities": {"33C": 0.6, "34C": 0.3, "31C": 0.1},
        "strong_physical_evidence": {
            "joint_feature_model_applied": True, "top_bucket": "33C",
            "top_bucket_joint_strong_support": True,
            "bucket_exclusions": [{"bucket": "31C", "excluded": True,
                                   "kind": "remaining_warming_unattainable",
                                   "composer_joint_strong_model": True, "confidence": 0.99}],
        },
    }


def _market() -> dict:
    return {
        "market_snapshot_id": "market-current", "as_of": "2026-07-13T00:00:00.300Z", "expires_at": "2026-07-13T00:00:01.000Z",
        "buckets": {
            "31C": {"yes_token_id": "31y", "no_token_id": "31n", "yes_ask": 0.02, "no_ask": 0.98},
            "33C": {"yes_token_id": "33y", "no_token_id": "33n", "yes_ask": 0.50, "no_ask": 0.50},
            "34C": {"yes_token_id": "34y", "no_token_id": "34n", "yes_ask": 0.20, "no_ask": 0.80},
        },
    }


def _capital() -> dict:
    return {
        "capital_snapshot_id": "capital-current", "risk_snapshot_id": "risk-current",
        "capital_epoch_id": "epoch-current", "risk_reservation_id": "reserve-current",
        "available_cash_usdc": 124.99, "notional_by_leg": {"top_yes_wave": 20.0, "second_yes": 4.99, "strongly_excluded_no": 100.0},
        "expires_at": "2026-07-13T00:00:03.000Z",
    }


def _risk() -> dict:
    return {"risk_snapshot_id": "risk-current", "capital_epoch_id": "epoch-current", "maximum_new_buy_notional_usdc": 30.0, "new_buy_authorized": True, "expires_at": "2026-07-13T00:00:03.000Z"}


def _positions() -> dict:
    return {"position_snapshot_id": "positions-current", "as_of": "2026-07-13T00:00:00.000Z", "complete": True, "positions": [], "unresolved_intents": []}


def test_frozen_bundle_composes_exactly_one_bound_three_leg_plan() -> None:
    result = build_production_dynamic_three_leg_frozen_bundle(
        outbox=_outbox(), realization_state=_realization(), market_snapshot=_market(),
        capital_snapshot=_capital(), risk_snapshot=_risk(), position_snapshot=_positions(),
        settlement_window_policy=load_approved_settlement_window_policy(),
        top_yes_wave_reentry_state=build_top_yes_wave_reentry_state(posterior_run_id="post-current", completed_cycles=[]),
        strategy_artifact_sha256="artifact-current", production_activation={"state": "enabled"},
        decision_as_of=NOW,
    )
    assert result["ok"] is True
    assert result["desired_trading_plan"]["posterior_run_id"] == "post-current"
    assert result["transition_plan"]["phase"] == "BUY_NEW"
    assert result["boundary"]["reservation_created_only_inside_transaction_c"] is True
    assert result["settlement_window_state"]["settlement_window_open"] is False


def test_frozen_bundle_rejects_cross_posterior_before_any_decision_input_exists() -> None:
    realization = _realization()
    realization["posterior_run_id"] = "post-other"
    result = build_production_dynamic_three_leg_frozen_bundle(
        outbox=_outbox(), realization_state=realization, market_snapshot=_market(),
        capital_snapshot=_capital(), risk_snapshot=_risk(), position_snapshot=_positions(),
        settlement_window_policy=load_approved_settlement_window_policy(),
        top_yes_wave_reentry_state=build_top_yes_wave_reentry_state(posterior_run_id="post-other", completed_cycles=[]),
        strategy_artifact_sha256="artifact-current", production_activation={}, decision_as_of=NOW,
    )
    assert result["ok"] is False
    assert "production_frozen_bundle_realization_posterior_mismatch" in result["block_reasons"]
