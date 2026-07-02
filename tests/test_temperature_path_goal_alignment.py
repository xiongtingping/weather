from __future__ import annotations

from scripts import check_temperature_path_goal_alignment as audit


def test_rebuild_inflight_snapshot_is_temporary_not_missing_path() -> None:
    payload = {
        "ok": True,
        "busy": True,
        "cache_rebuild_in_flight": True,
        "facts": {
            "items": [],
            "source": "ui_snapshot_rebuild_in_flight",
        },
    }

    result = audit._check_snapshot(payload, "runtime_api")

    assert result["ok"] is False
    assert result["temporary"] is True
    assert result["blockers"] == ["ui_snapshot_rebuild_in_flight"]
    assert "no_temperature_path_distribution_items" not in result["blockers"]


def test_trade_follow_allowed_must_meet_effective_lcb() -> None:
    item = {
        "slug": "chengdu-test",
        "event_date": "2026-06-21",
        "temperature_path_distribution": {
            "target_bucket": "25°C or below",
            "target_bucket_posterior": 0.42,
            "target_bucket_path_probability": 0.64,
            "target_bucket_path_probability_lcb": 0.49,
            "second_target_bucket": "26°C",
            "second_target_bucket_posterior": 0.28,
            "second_target_bucket_path_probability": 0.15,
            "bucket_distribution": [
                {
                    "bucket": "25°C or below",
                    "posterior": 0.42,
                    "path_probability": 0.64,
                    "path_probability_available": True,
                    "path_probability_source": "temperature_path_distribution",
                },
                {
                    "bucket": "26°C",
                    "posterior": 0.28,
                    "path_probability": 0.15,
                    "path_probability_available": True,
                    "path_probability_source": "temperature_path_distribution",
                },
            ],
            "freshness": {"source_stale": False},
            "reliability": {"state": "high"},
            "error_band": {"sigma_c": 1.52},
            "risk_explanation": "risk",
            "trade_follow": {
                "allowed": True,
                "min_target_path_lcb": 0.35,
                "min_target_path_lcb_effective": 0.50,
                "min_target_posterior": 0.15,
                "calibration_risk_adjustment": {
                    "applied": True,
                    "base_min_target_path_lcb": 0.35,
                },
            },
        },
        "target_realization_capital_plan": {
            "target_bucket": "25°C or below",
            "target_posterior": 0.42,
            "second_target_bucket": "26°C",
            "second_target_posterior": 0.28,
            "temperature_path_trade_follow_allowed": True,
            "legs": [
                {
                    "role": "target_yes",
                    "target_realization_path_probability": 0.64,
                },
                {
                    "role": "second_target_yes",
                    "target_realization_path_probability": 0.15,
                },
            ],
            "ui_risk_explanation": {
                "target_posterior": 0.42,
                "second_target_posterior": 0.28,
                "target_realization_probability": 0.64,
                "second_target_realization_probability": 0.15,
            },
        },
    }

    result = audit._check_item(item, 0)

    assert result["ok"] is False
    assert "trade_follow_allowed_below_effective_lcb" in result["blockers"]


def test_trade_follow_block_requires_yes_legs_to_hold() -> None:
    item = {
        "slug": "chengdu-test",
        "event_date": "2026-06-21",
        "temperature_path_distribution": {
            "target_bucket": "25°C or below",
            "target_bucket_posterior": 0.42,
            "target_bucket_path_probability": 0.30,
            "target_bucket_path_probability_lcb": 0.24,
            "second_target_bucket": "26°C",
            "second_target_bucket_posterior": 0.28,
            "second_target_bucket_path_probability": 0.48,
            "bucket_distribution": [
                {
                    "bucket": "25°C or below",
                    "posterior": 0.42,
                    "path_probability": 0.30,
                    "path_probability_available": True,
                    "path_probability_source": "temperature_path_distribution",
                },
                {
                    "bucket": "26°C",
                    "posterior": 0.28,
                    "path_probability": 0.48,
                    "path_probability_available": True,
                    "path_probability_source": "temperature_path_distribution",
                },
            ],
            "freshness": {"source_stale": False},
            "reliability": {"state": "medium"},
            "error_band": {"sigma_c": 1.52},
            "risk_explanation": "risk",
            "trade_follow": {
                "allowed": False,
                "reason": "blocked_second_target_path_risk_high",
                "min_target_path_lcb": 0.35,
                "min_target_path_lcb_effective": 0.35,
                "min_target_posterior": 0.15,
            },
        },
        "target_realization_capital_plan": {
            "target_bucket": "25°C or below",
            "target_posterior": 0.42,
            "second_target_bucket": "26°C",
            "second_target_posterior": 0.28,
            "temperature_path_trade_follow_allowed": False,
            "temperature_path_trade_follow_block_yes": True,
            "legs": [
                {
                    "role": "target_yes",
                    "action": "add",
                    "block_reason": "",
                    "delta_notional": 12.0,
                    "target_realization_path_probability": 0.30,
                },
                {
                    "role": "second_target_yes",
                    "action": "hold",
                    "block_reason": "temperature_path_trade_follow_not_allowed",
                    "delta_notional": 0.0,
                    "target_realization_path_probability": 0.48,
                },
            ],
            "ui_risk_explanation": {
                "target_posterior": 0.42,
                "second_target_posterior": 0.28,
                "target_realization_probability": 0.30,
                "second_target_realization_probability": 0.48,
            },
        },
    }

    result = audit._check_item(item, 0)

    assert result["ok"] is False
    assert "target_yes_allows_add_when_trade_follow_blocked" in result["blockers"]
    assert "target_yes_missing_temperature_path_block_reason" in result["blockers"]
    assert "target_yes_has_positive_add_when_trade_follow_blocked" in result["blockers"]


def test_observed_max_floor_blocks_target_bucket_below_reached_temperature() -> None:
    item = {
        "slug": "chengdu-test",
        "event_date": "2026-06-22",
        "target_bucket_outlook": {
            "target_bucket": "32°C",
            "final_bucket": "32°C",
            "hard_source_direct": {
                "source": "hard_source_direct",
                "posterior_floor_constraint_applied": True,
                "observed_max_c": 33.0,
            },
        },
        "temperature_path_distribution": {
            "target_bucket": "32°C",
            "target_bucket_posterior": 0.40,
            "target_bucket_path_probability": 1.0,
            "target_bucket_path_probability_lcb": 0.90,
            "second_target_bucket": "33°C",
            "second_target_bucket_posterior": 0.35,
            "second_target_bucket_path_probability": 1.0,
            "observed_today_max_c": 33.0,
            "bucket_distribution": [
                {
                    "bucket": "32°C",
                    "posterior": 0.40,
                    "path_probability": 1.0,
                    "path_probability_available": True,
                    "path_probability_source": "hard_observed_max_reached_lower_edge",
                },
                {
                    "bucket": "33°C",
                    "posterior": 0.35,
                    "path_probability": 1.0,
                    "path_probability_available": True,
                    "path_probability_source": "hard_observed_max_reached_lower_edge",
                },
            ],
            "freshness": {"source_stale": False},
            "reliability": {"state": "high"},
            "error_band": {"sigma_c": 1.52},
            "risk_explanation": "risk",
            "trade_follow": {
                "allowed": True,
                "min_target_path_lcb": 0.35,
                "min_target_path_lcb_effective": 0.35,
                "min_target_posterior": 0.15,
            },
        },
        "target_realization_capital_plan": {
            "target_bucket": "32°C",
            "target_posterior": 0.40,
            "second_target_bucket": "33°C",
            "second_target_posterior": 0.35,
            "temperature_path_trade_follow_allowed": True,
            "legs": [
                {
                    "role": "target_yes",
                    "target_realization_path_probability": 1.0,
                    "target_realization_observed_max_c": 33.0,
                },
                {
                    "role": "second_target_yes",
                    "target_realization_path_probability": 1.0,
                    "target_realization_observed_max_c": 33.0,
                },
            ],
            "ui_risk_explanation": {
                "target_bucket": "32°C",
                "target_posterior": 0.40,
                "second_target_bucket": "33°C",
                "second_target_posterior": 0.35,
                "target_realization_probability": 1.0,
                "second_target_realization_probability": 1.0,
                "hard_source_state": {"observed_max_c": 33.0},
            },
        },
    }

    result = audit._check_item(item, 0)

    assert result["ok"] is False
    assert "temperature_path_distribution.target_bucket_below_observed_max_floor" in result["blockers"]
    assert "target_realization_capital_plan.target_bucket_below_observed_max_floor" in result["blockers"]
    assert "target_bucket_outlook.final_bucket_below_observed_max_floor" in result["blockers"]
