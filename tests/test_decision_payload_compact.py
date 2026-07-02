import json

import pytest

from scan_weather_market import (
    _apply_explicit_ssot_entry_block_context,
    _compact_decision_details,
    _merge_decision_details,
    _soft_forecast_posterior_status,
)


def test_soft_forecast_posterior_status_marks_captured_not_in_main_posterior() -> None:
    status = _soft_forecast_posterior_status(
        peak_c=32.2,
        evidence=0.0,
        status="zero_scaled",
        source="wunderground_forecast_daily_peak",
    )

    assert status["captured"] is True
    assert status["posterior_included"] is False
    assert status["posterior_status"] == "captured_not_in_main_posterior"
    assert status["posterior_label"] == "已抓取但未进入主后验"
    assert status["not_in_posterior_reason"] == "created_at_safe_walk_forward_weight_zero"


def test_merge_decision_details_module_fallback_is_available_for_trade_sync() -> None:
    merged = _merge_decision_details(
        "29°C",
        "YES",
        "exit",
        {"source": "trade_sync_auto_order", "strategy": "exit"},
    )

    assert merged["outcome"] == "29°C"
    assert merged["side"] == "YES"
    assert merged["action"] == "exit"
    assert merged["source"] == "trade_sync_auto_order"


def test_soft_forecast_posterior_status_marks_included_when_evidence_is_nonzero() -> None:
    status = _soft_forecast_posterior_status(
        peak_c=32.0,
        evidence=4.0,
        status="active",
        source="zuuu_taf_awc_tx_peak",
    )

    assert status["captured"] is True
    assert status["posterior_included"] is True
    assert status["posterior_status"] == "included_in_main_posterior"
    assert status["posterior_label"] == "已进入主后验"
    assert status["not_in_posterior_reason"] == ""


def test_compact_entry_payload_limits_size_and_keeps_core_fields() -> None:
    details = {
        "ask": 0.18,
        "bid": 0.2,
        "q_entry": 0.45,
        "q_quote": 0.55,
        "spread": 0.02,
        "strategy": "lcb_global",
        "edge_tradeability_pass": True,
        "edge_tradeability_mode": "enforced",
        "edge_tradeability_profit_value": 0.11,
        "edge_tradeability_edge_lcb": 0.07,
        "edge_realizable": 0.2,
        "entry_usable_edge": -0.01,
        "entry_original_reason_codes": ["market_not_tradeable"],
        "entry_soft_reason_codes": ["strategy_size_ratio_zero"],
        "entry_soft_no_order": True,
        "entry_strategy_family": "exploration_canary",
        "entry_edge_root_cause": "risk_block",
        "exec_edge_entry": 0.2,
        "wu_forecast_peak_posterior_included": False,
        "wu_forecast_peak_posterior_status": "captured_not_in_main_posterior",
        "wu_forecast_peak_posterior_label": "已抓取但未进入主后验",
        "wu_forecast_peak_not_in_posterior_reason": "created_at_safe_walk_forward_weight_zero",
        "taf_forecast_peak_posterior_included": False,
        "taf_forecast_peak_posterior_status": "captured_not_in_main_posterior",
        "taf_forecast_peak_posterior_label": "已抓取但未进入主后验",
        "order_id": "ord-001",
        "auto_order_id": "ord-auto-001",
        "location_key": "test_loc",
        "weather_strategy_consensus_label": "25C",
        "kelly_reason": "applied",
        "lcb_consensus_yes_top_auto_label": "25C",
        **{f"noise_{idx}": "x" * 80 for idx in range(80)},
    }

    compacted = _compact_decision_details(details, action="entry", will_order=True)
    compact_text = json.dumps(
        compacted,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    assert len(compact_text) <= 1024
    assert compacted["ask"] == 0.18
    assert compacted["bid"] == 0.2
    assert compacted["q_entry"] == 0.45
    assert compacted["q_quote"] == 0.55
    assert compacted["spread"] == 0.02
    assert compacted["strategy"] == "lcb_global"
    assert compacted["entry_usable_edge"] == -0.01
    assert compacted["entry_soft_reason_codes"] == ["strategy_size_ratio_zero"]
    assert compacted.get("exec_edge_entry") == 0.2
    assert compacted["wu_forecast_peak_posterior_label"] == "已抓取但未进入主后验"
    assert compacted["taf_forecast_peak_posterior_status"] == "captured_not_in_main_posterior"
    assert "weather_strategy_consensus_label" not in compacted
    assert "lcb_consensus_yes_top_auto_label" not in compacted
    assert compacted.get("order_id") == "ord-001"


def test_compact_entry_place_order_failed_keeps_raw_exec_output(monkeypatch) -> None:
    monkeypatch.setenv("PM_DECISION_DETAILS_COMPACT_MAX_BYTES", "256")
    stderr = "exec stderr: " + ("stderr-line " * 24)
    stdout = "exec stdout: " + ("stdout-line " * 18)
    details = {
        "ask": 1.0,
        "bid": 0.997,
        "q_entry": 0.65,
        "q_quote": 0.65,
        "strategy": "posterior_settlement",
        "posterior_target_direct": True,
        "posterior_target_job_id": 3583,
        "token_id": "token-32-yes",
        "order_price": 0.999,
        "order_size": 10.0,
        "order_type": "GTC",
        "status": "exec_nonzero",
        "order_error": "exec_nonzero",
        "order_error_detail": "CLOB place-order failed before JSON payload",
        "order_stderr": stderr,
        "order_stdout": stdout,
        "order_status": "exec_nonzero",
        "direct_target_order_timeout_sec": 6.0,
        "q_bayes_model_evidence": "model-evidence " * 80,
        "target_realization_reliability_source": "posterior_target_direct " * 80,
    }

    compacted = _compact_decision_details(details, action="entry", will_order=True)

    assert compacted["order_error"] == "exec_nonzero"
    assert compacted["order_error_detail"] == "CLOB place-order failed before JSON payload"
    assert compacted["order_stderr"] == stderr
    assert compacted["order_stdout"] == stdout
    assert compacted["order_status"] == "exec_nonzero"
    assert compacted["direct_target_order_timeout_sec"] == 6.0


def test_compact_entry_payload_keeps_usable_edge_audit_when_oversized() -> None:
    details = {
        "ask": 0.985,
        "bid": 0.96,
        "q_entry": 0.06,
        "q_quote": 0.07,
        "q_raw_static": 0.0,
        "strategy": "lcb_global",
        "edge_tradeability_profit_value": -0.59,
        "entry_usable_edge_na_reason": "edge_tradeability_blocked:edge_lcb_below_min",
        "entry_usable_edge_base_source": "edge_tradeability_missing",
        "entry_usable_edge_size_scale": 0.0,
        "entry_usable_edge_block_nonpositive": True,
        "entry_usable_edge_version": "audit_v1_2026_05_05",
        **{f"q_bayes_big_{idx}": "x" * 200 for idx in range(20)},
        **{f"weather_likelihood_big_{idx}": "y" * 200 for idx in range(20)},
    }

    compacted = _compact_decision_details(details, action="entry", will_order=False)

    assert compacted["entry_usable_edge_na_reason"] == (
        "edge_tradeability_blocked:edge_lcb_below_min"
    )
    assert compacted["entry_usable_edge_base_source"] == "edge_tradeability_missing"
    assert compacted["entry_usable_edge_size_scale"] == 0.0
    assert compacted["entry_usable_edge_block_nonpositive"] is True


def test_explicit_ssot_entry_block_context_overrides_generic_edge_missing() -> None:
    details = {
        "entry_usable_edge_na_reason": "edge_tradeability_missing",
        "weather_strategy_entry_block_reason": None,
    }
    source = {
        "expected_entry_ok": False,
        "auto_trade_ssot_diagnostic": True,
        "target_realization_batch_reason": "auto_trade_ssot_blocked_main_candidate",
        "weather_strategy_entry_block_reason": "remote_bucket_realization_prob_above_max",
        "target_bucket": "29°C",
        "target_posterior": 0.4217,
        "remote_bucket_realization_prob": 0.2848,
        "ssot_entry_reasons": [
            "posterior_settlement阻断(remote_bucket_realization_prob_above_max)"
        ],
    }

    out = _apply_explicit_ssot_entry_block_context(details, source)

    assert out["expected_entry_ok"] is False
    assert out["auto_trade_ssot_diagnostic"] is True
    assert out["entry_usable_edge_na_reason"] == "remote_bucket_realization_prob_above_max"
    assert out["weather_strategy_entry_block_reason"] == "remote_bucket_realization_prob_above_max"
    assert out["target_realization_batch_reason"] == "auto_trade_ssot_blocked_main_candidate"
    assert out["target_bucket"] == "29°C"
    assert out["remote_bucket_realization_prob"] == 0.2848


def test_compact_entry_payload_keeps_explicit_ssot_block_context_when_oversized() -> None:
    details = {
        "ask": 0.67,
        "bid": 0.66,
        "q_entry": 0.715,
        "q_quote": 0.715,
        "strategy": "main",
        "expected_entry_ok": False,
        "auto_trade_ssot_diagnostic": True,
        "target_realization_batch_reason": "auto_trade_ssot_blocked_main_candidate",
        "entry_usable_edge_na_reason": "remote_bucket_realization_prob_above_max",
        "weather_strategy_entry_block_reason": "remote_bucket_realization_prob_above_max",
        "target_bucket": "29°C",
        "target_posterior": 0.4217,
        "remote_bucket_realization_prob": 0.2848,
        "ssot_entry_reasons": [
            "posterior_settlement阻断(remote_bucket_realization_prob_above_max)"
        ],
        **{f"q_bayes_big_{idx}": "x" * 200 for idx in range(20)},
        **{f"weather_likelihood_big_{idx}": "y" * 200 for idx in range(20)},
    }

    compacted = _compact_decision_details(details, action="entry", will_order=False)

    assert compacted["expected_entry_ok"] is False
    assert compacted["auto_trade_ssot_diagnostic"] is True
    assert compacted["target_realization_batch_reason"] == "auto_trade_ssot_blocked_main_candidate"
    assert compacted["entry_usable_edge_na_reason"] == "remote_bucket_realization_prob_above_max"
    assert compacted["weather_strategy_entry_block_reason"] == "remote_bucket_realization_prob_above_max"
    assert compacted["target_bucket"] == "29°C"
    assert compacted["target_posterior"] == 0.4217
    assert compacted["remote_bucket_realization_prob"] == 0.2848


def test_compact_entry_payload_keeps_target_realization_risk_explanation() -> None:
    details = {
        "ask": 0.53,
        "bid": 0.52,
        "strategy": "path_probability_follow",
        "target_realization_ui_risk_explanation_text": (
            "目标 25°C or below posterior 26.12%，路径可兑现 42.0%。"
        ),
        "target_realization_ui_risk_codes": ["second_target_risk_high"],
        "target_realization_trade_follow_allowed": False,
        "target_realization_trade_follow_reason": "blocked_second_target_path_risk_high",
        "target_realization_path_probability": 0.42,
        "target_realization_path_probability_source": "temperature_path_distribution",
        "target_realization_path_p_reach_yes": 0.42,
        "target_realization_reliability": 0.64,
        "target_realization_path_reliability_state": "medium",
        "target_realization_observation_age_minutes": 4.5,
        **{f"noise_{idx}": "x" * 160 for idx in range(40)},
    }

    compacted = _compact_decision_details(details, action="entry", will_order=False)

    assert "路径可兑现" in compacted["target_realization_ui_risk_explanation_text"]
    assert compacted["target_realization_ui_risk_codes"] == ["second_target_risk_high"]
    assert compacted["target_realization_trade_follow_allowed"] is False
    assert compacted["target_realization_trade_follow_reason"] == "blocked_second_target_path_risk_high"
    assert compacted["target_realization_path_probability"] == 0.42
    assert compacted["target_realization_path_probability_source"] == "temperature_path_distribution"
    assert compacted["target_realization_path_p_reach_yes"] == 0.42
    assert compacted["target_realization_reliability"] == 0.64
    assert compacted["target_realization_path_reliability_state"] == "medium"
    assert compacted["target_realization_observation_age_minutes"] == 4.5


def test_compact_entry_payload_keeps_temperature_path_trade_follow_gate() -> None:
    details = {
        "ask": 0.53,
        "bid": 0.52,
        "strategy": "path_probability_follow",
        "posterior_settlement_entry_allowed": False,
        "posterior_settlement_entry_block_reason": "temperature_path_trade_follow_not_allowed",
        "target_realization_trade_follow_allowed": False,
        "target_realization_trade_follow_reason": "blocked_near_close_calibration_early_window_lcb",
        "temperature_path_trade_follow_allowed": False,
        "temperature_path_trade_follow_block_yes": True,
        "temperature_path_trade_follow_reason": "blocked_near_close_calibration_early_window_lcb",
        "temperature_path_trade_follow_gate_source": "temperature_path_distribution.trade_follow",
        **{f"noise_{idx}": "x" * 180 for idx in range(80)},
    }

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("PM_DECISION_DETAILS_COMPACT", "1")
        monkeypatch.setenv("PM_DECISION_DETAILS_COMPACT_MAX_BYTES", "640")
        compacted = _compact_decision_details(details, action="entry", will_order=False)

    assert compacted["posterior_settlement_entry_allowed"] is False
    assert compacted["posterior_settlement_entry_block_reason"] == "temperature_path_trade_follow_not_allowed"
    assert compacted["target_realization_trade_follow_allowed"] is False
    assert (
        compacted["target_realization_trade_follow_reason"]
        == "blocked_near_close_calibration_early_window_lcb"
    )
    assert compacted["temperature_path_trade_follow_allowed"] is False
    assert compacted["temperature_path_trade_follow_block_yes"] is True
    assert (
        compacted["temperature_path_trade_follow_reason"]
        == "blocked_near_close_calibration_early_window_lcb"
    )
    assert (
        compacted["temperature_path_trade_follow_gate_source"]
        == "temperature_path_distribution.trade_follow"
    )


@pytest.mark.parametrize(
    "action,required_key",
    [("entry", "entry_soft_no_order"), ("exit", "exit_skip_reason")],
)
def test_compact_required_hard_fields_survive(action: str, required_key: str) -> None:
    details = {
        "ask": 0.18,
        "bid": 0.2,
        "q_entry": 0.45,
        "q_quote": 0.55,
        "spread": 0.02,
        "strategy": "lcb_global",
        "edge_tradeability_pass": True,
        "edge_tradeability_mode": "enforced",
        "edge_tradeability_profit_value": 0.11,
        "edge_tradeability_edge_lcb": 0.07,
        "edge_realizable": 0.2,
        "exit_skip_reason": "risk_close",
        "exit_skip_detail": "signal_not_confirmed",
        "exit_reason_code": "risk_rule",
        "exit_reason_name": "风险规则",
        "entry_usable_edge": 0.3,
        "entry_soft_no_order": True,
        "exec_edge_entry": 0.21,
        "exec_edge_exit": 0.18,
        "order_id": "ord-001",
        "auto_order_id": "ord-auto-001",
    }

    compacted = _compact_decision_details(details, action=action, will_order=True)
    compact_text = json.dumps(
        compacted,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    assert len(compact_text) <= 1024
    assert required_key in compacted
    assert compacted["ask"] == 0.18
    assert compacted["bid"] == 0.2
    assert compacted["q_entry"] == 0.45
    assert compacted["q_quote"] == 0.55
    assert compacted["spread"] == 0.02
    assert compacted["order_id"] == "ord-001"


def test_compact_exit_payload_keeps_exit_policy_audit_fields_when_oversized() -> None:
    details = {
        "ask": 1.0,
        "bid": 0.999,
        "q_entry": 0.8953478708199714,
        "q_quote": 0.31096031681541725,
        "spread": 0.001,
        "strategy": "exit",
        "exec_edge_exit": 0.04484787081997138,
        "hold_edge_realizable": 0.04484787081997138,
        "hold_edge_realizable_trigger": 0.04484787081997138,
        "prev_hold_edge_realizable": 0.05,
        "exit_hold_edge_realizable_min": 0.02,
        "exit_edge_primary_source": "hold_edge_realizable",
        "exit_policy_primary_edge_source": "hold_edge_realizable",
        "exit_policy_rule": "hold_edge_realizable_plus_risk_constraints",
        "exit_policy_version": "exit_policy_v1_2026_05_05",
        "exit_policy_edge_min": 0.02,
        "exit_policy_edge_value": 0.04484787081997138,
        "order_id": "ord-exit-001",
        **{f"q_bayes_big_{idx}": "x" * 200 for idx in range(30)},
        **{f"weather_likelihood_big_{idx}": "y" * 200 for idx in range(30)},
    }

    compacted = _compact_decision_details(details, action="exit", will_order=True)

    assert compacted["exit_edge_primary_source"] == "hold_edge_realizable"
    assert compacted["exit_policy_primary_edge_source"] == "hold_edge_realizable"
    assert compacted["exit_policy_rule"] == "hold_edge_realizable_plus_risk_constraints"
    assert compacted["exit_policy_version"] == "exit_policy_v1_2026_05_05"
    assert compacted["exit_policy_edge_min"] == 0.02
    assert compacted["exit_hold_edge_realizable_min"] == 0.02
    assert compacted["hold_edge_realizable"] == 0.04484787081997138
    assert compacted["order_id"] == "ord-exit-001"


def test_compact_exit_payload_keeps_hard_zero_path_context_when_oversized() -> None:
    details = {
        "strategy": "path_probability_follow",
        "exec_edge_exit": -0.2,
        "target_realization_hard_zero_exit": True,
        "target_realization_hard_zero_exit_meta": {
            "target_realization_hard_zero_exit": True,
            "hard_zero_reasons": [
                "observed_today_max_above_bucket",
                "hard_source_lower_bound_constraint",
            ],
            "target_realization_hard_zero_exit_context": {
                "ssot_target_bucket": "31°C",
                "second_target_bucket": "32°C",
                "target_posterior": 0.58,
                "second_target_posterior": 0.31,
                "target_realization_path_p_reach_yes": 0.74,
                "target_realization_path_reliability_state": "medium",
                "target_realization_observation_age_minutes": 3.5,
                "target_realization_ui_risk_explanation_text": (
                    "目标 31°C posterior 58.0%，路径可兑现 74.0%。"
                ),
                "target_realization_hard_source_state": {
                    "available": True,
                    "observed_max_c": 31.1,
                    "source": "zuuu_metar_raw_awc",
                },
            },
        },
        **{f"noise_{idx}": "x" * 200 for idx in range(40)},
    }

    compacted = _compact_decision_details(details, action="exit", will_order=False)

    meta = compacted["target_realization_hard_zero_exit_meta"]
    context = meta["target_realization_hard_zero_exit_context"]
    assert context["ssot_target_bucket"] == "31°C"
    assert context["second_target_bucket"] == "32°C"
    assert context["target_posterior"] == 0.58
    assert context["second_target_posterior"] == 0.31
    assert context["target_realization_path_p_reach_yes"] == 0.74
    assert context["target_realization_path_reliability_state"] == "medium"
    assert "路径可兑现" in context["target_realization_ui_risk_explanation_text"]
