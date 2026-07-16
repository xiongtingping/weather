from __future__ import annotations

from dynamic_three_leg_trading_plan_runtime import build_dynamic_three_leg_trading_plan
from settlement_window_runtime import build_settlement_window_state, load_approved_settlement_window_policy
from second_yes_entry_window_runtime import build_second_yes_entry_window_state
from top_yes_wave_reentry_runtime import build_top_yes_wave_reentry_state


def _state(**overrides) -> dict:
    value = {
        "posterior_run_id": "post_1234567890abcdef",
        "realization_state_id": "rstate_1234567890abcdef",
        "market_bucket_probabilities": {
            "29C": 0.03,
            "30C": 0.05,
            "31C": 0.07,
            "32C": 0.25,
            "33C": 0.60,
        },
        "peak_timing_state": {
            "schema": "peak_timing_state_l3_v1",
            "ok": True,
            "state": "available",
            "peak_timing_state_id": "peaktiming_1234567890abcdef",
            "entry_peak_at": "2026-07-14T13:15:00Z",
        },
    }
    value.update(overrides)
    if "strong_physical_evidence" not in overrides:
        top_bucket = max(value["market_bucket_probabilities"], key=value["market_bucket_probabilities"].get)
        value["strong_physical_evidence"] = {
            "joint_feature_model_applied": True,
            "top_bucket": top_bucket,
            "top_bucket_joint_strong_support": True,
            "safe_no_amsc_observation": True,
            "safe_no_observation_source": "zuuu_metar_raw_amsc",
            "safe_no_monotonic_daily_high_floor": True,
            "safe_no_observed_max_c": 31.0,
            "bucket_exclusions": [
                {
                    "bucket": "30C",
                    "excluded": True,
                    "kind": "observed_daily_high_exceeded",
                    "composer_joint_strong_model": True,
                    "confidence": 1.0,
                    "settlement_exclusion_certificate_state": "valid",
                }
            ],
        }
    return value


def _market() -> dict:
    return {
        "market_snapshot_id": "market_1234567890abcdef",
        "buckets": {
            bucket: {
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "yes_token_id": f"yes-{bucket}",
                "no_token_id": f"no-{bucket}",
            }
            for bucket, yes_ask, no_ask in (
                ("29C", 0.02, 0.99),
                ("30C", 0.04, 0.97),
                ("31C", 0.08, 0.93),
                ("32C", 0.30, 0.71),
                ("33C", 0.999, 0.01),
            )
        },
    }


def _capital() -> dict:
    return {
        "capital_snapshot_id": "capital_1234567890abcdef",
        "risk_snapshot_id": "risk_1234567890abcdef",
        "capital_epoch_id": "capital_epoch_1234567890abcdef",
        "risk_reservation_id": "risk_reservation_1234567890abcdef",
        "available_cash_usdc": 100.0,
        "notional_by_leg": {
            "top_yes_wave": 100.0,
            "second_yes": 100.0,
            "strongly_excluded_no": 100.0,
        },
    }


def _window(*, decision_as_of: str = "2026-07-14T13:00:00Z") -> dict:
    return build_settlement_window_state(
        resolution_local_date="2026-07-14",
        decision_as_of=decision_as_of,
        policy=load_approved_settlement_window_policy(),
    )


def _reentry(*, posterior_run_id: str = "post_1234567890abcdef", completed=None) -> dict:
    return build_top_yes_wave_reentry_state(
        posterior_run_id=posterior_run_id,
        completed_cycles=completed or [],
    )


def _plan(**overrides) -> dict:
    values = {
        "realization_state": _state(),
        "settlement_window_state": _window(),
        "top_yes_wave_reentry_state": _reentry(),
        "market_snapshot": _market(),
        "capital_plan": _capital(),
        "strategy_artifact_sha256": "strategy-artifact-three-leg-1",
    }
    values.update(overrides)
    if "second_yes_entry_window_state" not in values:
        values["second_yes_entry_window_state"] = build_second_yes_entry_window_state(
            realization_state=values["realization_state"],
            decision_as_of=values["settlement_window_state"]["decision_as_of"],
        )
    return build_dynamic_three_leg_trading_plan(**values)


def test_dynamic_plan_emits_only_current_three_roles_with_caps() -> None:
    plan = _plan()

    assert plan["ok"] is True
    assert [(row["strategy_leg"], row["bucket"], row["side"]) for row in plan["candidates"]] == [
        ("top_yes_wave", "33C", "YES"),
        ("second_yes", "32C", "YES"),
        ("strongly_excluded_no", "30C", "NO"),
    ]
    assert [row["notional"] for row in plan["candidates"]] == [20.0, 4.99, 100.0]
    assert all(row["uses_ev_edge_or_fair_value_gate"] is False for row in plan["candidates"])
    assert plan["boundary"]["remote_no_retired"] is True
    assert plan["boundary"]["trajectory_no_retired"] is True
    assert plan["boundary"]["market_alpha_cycle_not_created"] is True
    assert plan["boundary"]["settlement_window_not_read_from_realization_state"] is True


def test_strongly_excluded_no_waits_for_independent_settlement_window_state() -> None:
    plan = _plan(settlement_window_state=_window(decision_as_of="2026-07-14T12:59:59Z"))

    assert [(row["strategy_leg"], row["side"]) for row in plan["candidates"]] == [
        ("top_yes_wave", "YES"),
        ("second_yes", "YES"),
    ]
    skipped = {row["strategy_leg"]: row["reason"] for row in plan["skipped"]}
    assert skipped["strongly_excluded_no"] == "strongly_excluded_no_settlement_window_not_open"


def test_each_new_posterior_reselects_top_and_second_without_frozen_plan() -> None:
    first = _plan()
    second = _plan(
        realization_state=_state(
            posterior_run_id="post_next",
            realization_state_id="rstate_next",
            market_bucket_probabilities={"29C": 0.03, "30C": 0.05, "31C": 0.07, "32C": 0.65, "33C": 0.20},
        ),
        top_yes_wave_reentry_state=_reentry(posterior_run_id="post_next"),
    )

    assert [row["bucket"] for row in first["candidates"][:2]] == ["33C", "32C"]
    assert [row["bucket"] for row in second["candidates"][:2]] == ["32C", "33C"]
    assert first["trading_plan_id"] != second["trading_plan_id"]
    assert all("wave_cycle_id" not in row for row in second["candidates"])


def test_completed_wave_suppresses_only_same_posterior_top_reentry() -> None:
    completed = [{
        "market_alpha_cycle_id": "macycle-completed",
        "posterior_run_id": "post_1234567890abcdef",
        "bucket": "33C",
        "state": "completed",
    }]
    same = _plan(top_yes_wave_reentry_state=_reentry(completed=completed))
    next_run = _plan(
        realization_state=_state(posterior_run_id="post_next", realization_state_id="rstate_next"),
        top_yes_wave_reentry_state=_reentry(posterior_run_id="post_next"),
    )

    assert [row["strategy_leg"] for row in same["candidates"]] == ["second_yes", "strongly_excluded_no"]
    assert any(row["reason"] == "top_yes_wave_completed_under_same_posterior" for row in same["skipped"])
    assert next_run["candidates"][0]["strategy_leg"] == "top_yes_wave"


def test_missing_composer_owned_physical_evidence_fails_closed() -> None:
    plan = _plan(realization_state=_state(strong_physical_evidence={}))

    assert plan["ok"] is False
    assert "three_leg_l3_strong_physical_evidence_missing" in plan["block_reasons"]


def test_no_entry_price_cap_or_ev_gate_is_reintroduced() -> None:
    plan = _plan()

    top = plan["candidates"][0]
    assert top["price"] == 0.999
    assert top["strategy_leg"] == "top_yes_wave"


def test_second_yes_window_closed_retains_role_but_emits_no_buy_candidate() -> None:
    state = _state()
    closed = build_second_yes_entry_window_state(
        realization_state=state,
        decision_as_of="2026-07-14T12:44:59Z",
    )
    plan = _plan(
        realization_state=state,
        second_yes_entry_window_state=closed,
    )

    assert "second_yes" not in {row["strategy_leg"] for row in plan["candidates"]}
    retention = next(row for row in plan["retention_candidates"] if row["strategy_leg"] == "second_yes")
    assert retention["entry_allowed"] is False
    assert retention["notional"] == 4.99
