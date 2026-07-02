from __future__ import annotations

from datetime import date
import sqlite3
import types

from scripts.daily_trade_sms_summary import (
    DailyStats,
    _build_message,
    _format_paper_validation_line,
    _format_scale_up_validation_line,
    _format_recommended_pilot_wakeup_line,
    _format_recommended_pilot_gap_plan_line,
    _format_recommended_pilot_gap_refresh_line,
    _format_settlement_label_line,
    _format_settlement_closed_loop_line,
    _format_settlement_confirmation_packet_line,
    _format_settlement_pipeline_line,
    _format_small_capital_validation_line,
    _format_strategy_batch_line,
    _load_daily_stats,
)


def test_default_sms_recipient_falls_back_to_trade_module(monkeypatch) -> None:
    from scripts import daily_trade_sms_summary as sms
    import sys

    monkeypatch.setitem(
        sys.modules,
        "scan_weather_market",
        types.SimpleNamespace(SMS_RECIPIENT="+10000000000"),
    )

    assert sms._default_sms_recipient() == "+10000000000"


def test_daily_summary_includes_paper_validation_status() -> None:
    stats = DailyStats(entry_order_submit_count=1, buy_notional=2.5)
    message = _build_message(
        date(2026, 5, 31),
        stats,
        {
            "ok": True,
            "city": "chengdu",
            "bayes_samples": 62,
            "coverage": 1.0,
            "will_order_rows": 0,
            "tradeability_gap_paper_candidates": 10,
            "planned_labels": 0,
            "settlement_missing": 62,
            "reliability_brier": 0.18,
            "reliability_ece": 0.04,
        },
    )

    assert "Paper: chengdu Bayes样本62 覆盖100% | 待结算62 | 标签0 | 意图0 | Gap纸候选10 | Brier0.180/ECE0.040" in message


def test_paper_validation_line_surfaces_blockers() -> None:
    line = _format_paper_validation_line(
        {
            "ok": False,
            "blockers": ["no_bayesian_paper_samples", "event_run_bayes_coverage_below_paper_gate"],
        }
    )

    assert line == "Paper: 阻断 no_bayesian_paper_samples,event_run_bayes_coverage_below_paper_gate"


def test_daily_summary_includes_small_capital_status() -> None:
    stats = DailyStats(entry_order_submit_count=1, buy_notional=2.5)
    message = _build_message(
        date(2026, 5, 31),
        stats,
        None,
        {
            "ok": True,
            "cities": ["chengdu"],
            "secondary_only": True,
            "max_per_order_usdc": 5.0,
            "max_daily_usdc": 25.0,
            "pilot_candidate_total": 4,
            "pilot_reverse_no_total": 2,
            "pilot_d0_settlement_total": 2,
            "small_capital_candidate_ready": False,
            "pilot_observation_report": "data/reports/pilot_observation_review_latest.md",
            "paper_observation_tracking_summary": {
                "resolved_rows": 3,
                "pending_rows": 2,
                "wins": 0,
            },
            "paper_observation_rolling_summary": {
                "resolved_rows": 3,
                "pending_rows": 2,
                "wins": 0,
                "hit_rate": 0.0,
            },
        },
    )

    assert "SmallCap: chengdu 次级=True | 单笔5/日25 USDC | Obs4/反NO2/D0Set2 | 表pilot_observation | Track结3/待2/胜0 | Roll结3/待2/胜0/HR0% | ready=False" in message


def test_load_small_capital_validation_status_auto_selects_pilot(monkeypatch) -> None:
    from scripts import daily_trade_sms_summary as sms
    from scripts import pilot_city_validation_matrix
    from scripts import small_capital_validation_review
    from scripts import strategy_objective_completion_audit

    monkeypatch.delenv("PM_SMALL_CAPITAL_PILOT_CITIES", raising=False)
    monkeypatch.delenv("PM_PAPER_VALIDATION_CITY", raising=False)
    monkeypatch.setattr(
        pilot_city_validation_matrix,
        "build_report",
        lambda *args, **kwargs: {"cities": [{"city": "chongqing", "blockers": []}]},
    )
    monkeypatch.setattr(
        strategy_objective_completion_audit,
        "_auto_pilot_cities_from_matrix",
        lambda matrix: ["chongqing"],
    )
    monkeypatch.setattr(
        strategy_objective_completion_audit,
        "_filter_auto_pilot_cities_by_paper_validation",
        lambda *args, **kwargs: ["chongqing"],
    )
    monkeypatch.setattr(
        small_capital_validation_review,
        "build_report",
        lambda *args, **kwargs: {
            "ok": True,
            "pilot_scope": {
                "cities": ["chongqing", "ankara"],
                "secondary_only": True,
                "source": "objective_audit_latest",
            },
            "small_capital_caps": {"max_per_order_usdc": 5.0, "max_daily_usdc": 25.0},
            "pilot_candidate_review": {"by_city": {}},
            "blockers": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr(sms, "_load_paper_observation_rolling_summary", lambda: ({}, []))
    monkeypatch.setattr(sms, "_load_paper_observation_target_summary", lambda **kwargs: {})

    status = sms._load_small_capital_validation_status()

    assert status is not None
    assert status["cities"] == ["chongqing", "ankara"]
    assert status["pilot_city_source"] == "objective_audit_latest"


def test_small_capital_line_surfaces_blockers() -> None:
    line = _format_small_capital_validation_line(
        {
            "ok": False,
            "blockers": ["paper_validation_not_passed:chengdu", "pilot_city_not_secondary"],
            "pilot_candidate_total": 3,
            "pilot_reverse_no_total": 2,
            "pilot_d0_settlement_total": 1,
            "pilot_observation_report": "data/reports/pilot_observation_review_latest.md",
            "paper_observation_tracking_summary": {
                "resolved_rows": 1,
                "pending_rows": 2,
                "wins": 1,
            },
            "paper_observation_rolling_summary": {
                "resolved_rows": 1,
                "pending_rows": 2,
                "wins": 1,
                "hit_rate": 1.0,
            },
        }
    )

    assert line == (
        "SmallCap: 阻断 paper_validation_not_passed:chengdu,pilot_city_not_secondary"
        " | Obs3/反NO2/D0Set1 | 表pilot_observation | Track结1/待2/胜1"
        " | Roll结1/待2/胜1/HR100%"
    )


def test_small_capital_line_surfaces_observation_targets() -> None:
    line = _format_small_capital_validation_line(
        {
            "ok": True,
            "cities": ["chongqing"],
            "secondary_only": True,
            "max_per_order_usdc": 5.0,
            "max_daily_usdc": 25.0,
            "pilot_candidate_total": 5,
            "pilot_reverse_no_total": 1,
            "pilot_d0_settlement_total": 4,
            "paper_observation_rolling_summary": {
                "resolved_rows": 5,
                "pending_rows": 0,
                "wins": 3,
                "hit_rate": 0.6,
            },
            "paper_observation_target_summary": {
                "strategy_targets": [
                    {
                        "target_strategy": "reverse_edge_buy_no_sell_yes",
                        "needed_resolved_rows": 2,
                        "needed_additional_wins_for_hit_rate": 0,
                    },
                    {
                        "target_strategy": "secondary_or_physical_edge",
                        "needed_resolved_rows": 2,
                        "needed_additional_wins_for_hit_rate": 0,
                    },
                ],
                "observation_expansion_city_targets": [
                    {"city": "buenos aires"},
                    {"city": "ankara"},
                ],
                "recommended_pilot_city_targets": [
                    {"city": "buenos aires"},
                ],
            },
        }
    )

    assert "Target反NO+2/次物+2" in line
    assert "推荐Pilotbuenos aires" in line
    assert "扩展观察buenos aires,ankara" in line


def test_small_capital_line_surfaces_d0_observation_target_label() -> None:
    line = _format_small_capital_validation_line(
        {
            "ok": True,
            "cities": ["ankara"],
            "secondary_only": True,
            "max_per_order_usdc": 5.0,
            "max_daily_usdc": 25.0,
            "paper_observation_target_summary": {
                "strategy_targets": [
                    {
                        "target_strategy": "d0_physical_settlement_hybrid",
                        "needed_resolved_rows": 0,
                        "needed_additional_wins_for_hit_rate": 1,
                    }
                ],
            },
        }
    )

    assert "TargetD0+1胜" in line


def test_daily_summary_includes_scale_up_status() -> None:
    stats = DailyStats(entry_order_submit_count=1, buy_notional=2.5)
    message = _build_message(
        date(2026, 5, 31),
        stats,
        None,
        None,
        {
            "ok": True,
            "cities": ["istanbul"],
            "secondary_only": True,
            "max_per_order_usdc": 25.0,
            "max_daily_usdc": 100.0,
            "paper_observation_rolling_summary": {
                "resolved_rows": 12,
                "pending_rows": 0,
                "wins": 8,
                "hit_rate": 8 / 12,
            },
        },
    )

    assert "ScaleUp: istanbul 次级=True | 单笔25/日100 USDC | Roll结12/待0/胜8/HR67%" in message


def test_scale_up_line_surfaces_blockers() -> None:
    line = _format_scale_up_validation_line(
        {
            "ok": False,
            "blockers": [
                "small_capital_validation_not_passed",
                "scale_up_requires_secondary_pilot_only",
            ],
            "paper_observation_rolling_summary": {
                "resolved_rows": 3,
                "pending_rows": 2,
                "wins": 0,
                "hit_rate": 0.0,
            },
        }
    )

    assert line == (
        "ScaleUp: 阻断 small_capital_validation_not_passed,scale_up_requires_secondary_pilot_only"
        " | Roll结3/待2/胜0/HR0%"
    )


def test_load_scale_up_status_uses_auto_pilot_defaults(monkeypatch) -> None:
    from scripts import daily_trade_sms_summary as sms
    from scripts import scale_up_validation_review

    monkeypatch.delenv("PM_SMALL_CAPITAL_PILOT_CITIES", raising=False)
    captured: dict[str, object] = {}

    def fake_build_report(*args, **kwargs):
        captured.update(kwargs)
        return {
            "ok": False,
            "pilot_scope": {
                "cities": ["chongqing", "ankara"],
                "secondary_only": True,
                "source": "objective_audit_latest",
            },
            "scale_up_caps": {"max_per_order_usdc": 25.0, "max_daily_usdc": 100.0},
            "blockers": ["small_capital_validation_not_passed"],
            "warnings": [],
        }

    monkeypatch.setattr(scale_up_validation_review, "build_report", fake_build_report)
    monkeypatch.setattr(sms, "_load_paper_observation_rolling_summary", lambda: ({}, []))

    status = sms._load_scale_up_validation_status()

    assert captured["pilot_cities"] == []
    assert status is not None
    assert status["cities"] == ["chongqing", "ankara"]


def test_daily_summary_includes_settlement_label_status() -> None:
    stats = DailyStats(entry_order_submit_count=1, buy_notional=2.5)
    message = _build_message(
        date(2026, 5, 31),
        stats,
        None,
        None,
        None,
        {
            "ok": True,
            "market_scope": "highest_temperature",
            "market_count": 13,
            "ready_market_count": 2,
            "planned_labels": 44,
            "status_counts": {
                "label_ready": 2,
                "settlement_not_closed": 11,
            },
            "review_command": ".venv/bin/python scripts/settlement_label_eligibility_report.py --since-days 30 --market-scope highest_temperature --limit 25",
            "apply_command": ".venv/bin/python scripts/build_settlement_calibration_labels.py --since-days 30 --market-scope highest_temperature --apply --confirm-apply APPLY_SETTLEMENT_LABELS",
        },
    )

    assert (
        "Settlement: 最高温 ready2/13 | 标签44 | settlement_not_closed:11,label_ready:2 "
        "| 审阅: .venv/bin/python scripts/settlement_label_eligibility_report.py --since-days 30 --market-scope highest_temperature --limit 25 "
        "| apply: .venv/bin/python scripts/build_settlement_calibration_labels.py --since-days 30 --market-scope highest_temperature --apply --confirm-apply APPLY_SETTLEMENT_LABELS"
    ) in message


def test_settlement_label_line_omits_review_command_without_ready_labels() -> None:
    line = _format_settlement_label_line(
        {
            "ok": True,
            "market_scope": "highest_temperature",
            "market_count": 13,
            "ready_market_count": 0,
            "planned_labels": 0,
            "status_counts": {"settlement_not_closed": 13},
            "review_command": ".venv/bin/python scripts/settlement_label_eligibility_report.py --since-days 30 --market-scope highest_temperature --limit 25",
        }
    )

    assert line == "Settlement: 最高温 ready0/13 | 标签0 | settlement_not_closed:13"


def test_settlement_label_line_surfaces_error() -> None:
    line = _format_settlement_label_line({"ok": False, "error": "report_error:RuntimeError"})

    assert line == "Settlement: 错误 report_error:RuntimeError"


def test_settlement_pipeline_line_surfaces_ready_actuals() -> None:
    line = _format_settlement_pipeline_line(
        {
            "ok": False,
            "read_only": True,
            "actual_missing": 2,
            "actual_ready": 2,
            "labels_planned": 0,
            "sim_labels_after_actual_apply": 88,
            "sim_actual_inserted": 2,
            "pre_apply_backup_required": True,
            "next_action": "confirm_and_apply_settlement_actuals_then_rebuild_labels",
            "confirm_command": ".venv/bin/python scripts/apply_settlement_validation_flow.py --since-days 42 --market-scope highest_temperature --apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS",
            "double_confirm_command": ".venv/bin/python scripts/apply_settlement_validation_flow.py --since-days 42 --market-scope highest_temperature --apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS --apply-labels --confirm-labels APPLY_SETTLEMENT_LABELS",
            "double_confirm_validate_command": ".venv/bin/python scripts/apply_settlement_validation_flow.py --since-days 42 --market-scope highest_temperature --apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS --apply-labels --confirm-labels APPLY_SETTLEMENT_LABELS --run-post-validation",
            "post_label_rolling_command": ".venv/bin/python scripts/paper_observation_rolling_validation.py --since-days 42 --market-scope highest_temperature --json",
            "post_label_objective_command": ".venv/bin/python scripts/strategy_objective_completion_audit.py --since-days 42 --market-scope highest_temperature --json",
        }
    )

    assert line == (
        "SettlementPipe: actual ready2/2 | labels0 | read_only=True "
        "| next=confirm_and_apply_settlement_actuals_then_rebuild_labels "
        "| sim_after_actual labels88/actual2 "
        "| actual_apply: .venv/bin/python scripts/apply_settlement_validation_flow.py --since-days 42 --market-scope highest_temperature --apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS "
        "| backup_required "
        "| double_confirm_available | validate_available "
        "| after_labels: .venv/bin/python scripts/paper_observation_rolling_validation.py --since-days 42 --market-scope highest_temperature --json + objective_audit"
    )


def test_daily_summary_includes_settlement_pipeline_status() -> None:
    stats = DailyStats(entry_order_submit_count=1, buy_notional=2.5)
    message = _build_message(
        date(2026, 5, 31),
        stats,
        settlement_pipeline_status={
            "ok": False,
            "read_only": True,
            "actual_missing": 2,
            "actual_ready": 2,
            "labels_planned": 0,
            "sim_labels_after_actual_apply": 88,
            "sim_actual_inserted": 2,
            "next_action": "confirm_and_apply_settlement_actuals_then_rebuild_labels",
            "confirm_command": ".venv/bin/python scripts/apply_settlement_validation_flow.py --since-days 42 --market-scope highest_temperature --apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS",
            "post_label_rolling_command": ".venv/bin/python scripts/paper_observation_rolling_validation.py --since-days 42 --market-scope highest_temperature --json",
        },
    )

    assert "SettlementPipe: actual ready2/2 | labels0 | read_only=True" in message
    assert "sim_after_actual labels88/actual2" in message
    assert "after_labels: .venv/bin/python scripts/paper_observation_rolling_validation.py" in message


def test_settlement_closed_loop_line_surfaces_phase_and_next_actions() -> None:
    line = _format_settlement_closed_loop_line(
        {
            "ok": False,
            "phase": "waiting_actual_confirmation",
            "actual_ready": 2,
            "labels_planned": 0,
            "sim_labels_after_actual_apply": 88,
            "sim_label_apply_inserted": 88,
            "sim_label_apply_closed_loop_ok": True,
            "actual_review_rows": 2,
            "blockers": [
                "settlement_actuals_waiting_confirmation",
                "settlement_labels_predicted_after_actual_apply",
            ],
            "next_actions": [
                "review_and_confirm_settlement_actual_apply",
                "after_label_apply_run_rolling_validation",
            ],
        }
    )

    assert line == (
        "SettlementLoop: phase=waiting_actual_confirmation | actual2 labels0 sim88 | ok=False "
        "| sim_insert88/closed | review_rows2 "
        "| 阻断 settlement_actuals_waiting_confirmation,settlement_labels_predicted_after_actual_apply "
        "| next review_and_confirm_settlement_actual_apply,after_label_apply_run_rolling_validation"
    )


def test_daily_summary_includes_settlement_closed_loop_status() -> None:
    stats = DailyStats(entry_order_submit_count=1, buy_notional=2.5)
    message = _build_message(
        date(2026, 5, 31),
        stats,
        settlement_closed_loop_status={
            "ok": False,
            "phase": "waiting_actual_confirmation",
            "actual_ready": 2,
            "labels_planned": 0,
            "sim_labels_after_actual_apply": 88,
            "blockers": ["settlement_actuals_waiting_confirmation"],
            "next_actions": ["review_and_confirm_settlement_actual_apply"],
        },
    )

    assert "SettlementLoop: phase=waiting_actual_confirmation" in message
    assert "actual2 labels0 sim88" in message


def test_settlement_confirmation_packet_line_surfaces_ready_packet() -> None:
    line = _format_settlement_confirmation_packet_line(
        {
            "ok": True,
            "actual_ready": 2,
            "would_insert_event_run_rows": 2,
            "sim_labels_after_actual_apply": 88,
            "sim_label_apply_inserted": 88,
            "sim_label_apply_closed_loop_ok": True,
            "actual_items": [
                {
                    "city": "nyc",
                    "actual": 77.8,
                    "market_unit": "F",
                    "station_id": "KLGA",
                    "source_station_id": "KLGA",
                    "station_source_match": True,
                },
                {
                    "city": "toronto",
                    "actual": 24.4,
                    "market_unit": "C",
                    "station_id": "CYYZ",
                    "source_station_id": "CYYZ",
                    "station_source_match": True,
                },
            ],
            "report": "data/reports/settlement_confirmation_packet_latest.md",
        }
    )

    assert line == (
        "SettlementConfirm: actual2/insert2 | sim_labels88/insert88 "
        "| closed=True | ok=True | nyc:77.8F/KLGA=KLGA/ok;"
        "toronto:24.4C/CYYZ=CYYZ/ok | 表settlement_confirm"
    )


def test_daily_summary_includes_settlement_confirmation_packet_status() -> None:
    stats = DailyStats(entry_order_submit_count=1, buy_notional=2.5)
    message = _build_message(
        date(2026, 5, 31),
        stats,
        settlement_confirmation_packet_status={
            "ok": True,
            "actual_ready": 2,
            "would_insert_event_run_rows": 2,
            "sim_labels_after_actual_apply": 88,
            "sim_label_apply_inserted": 88,
            "sim_label_apply_closed_loop_ok": True,
        },
    )

    assert "SettlementConfirm: actual2/insert2 | sim_labels88/insert88" in message


def test_recommended_pilot_wakeup_line_surfaces_waiting_market() -> None:
    line = _format_recommended_pilot_wakeup_line(
        {
            "ok": True,
            "watched_market_count": 1,
            "triggered_market_count": 0,
            "refresh_ran": False,
            "watched_markets": [
                {"city": "buenos-aires", "event_date": "2026-06-01"},
            ],
        }
    )

    assert line == (
        "Wakeup: 推荐Pilot结算 watch1/trigger0 | refresh=no | 等 buenos-aires:2026-06-01"
    )


def test_recommended_pilot_wakeup_line_surfaces_triggered_market() -> None:
    line = _format_recommended_pilot_wakeup_line(
        {
            "ok": True,
            "watched_market_count": 1,
            "triggered_market_count": 1,
            "refresh_ran": True,
            "triggered_markets": [
                {
                    "city": "buenos-aires",
                    "event_date": "2026-06-01",
                    "current_status": "label_ready",
                },
            ],
        }
    )

    assert line == (
        "Wakeup: 推荐Pilot结算 watch1/trigger1 | refresh=yes | buenos-aires:2026-06-01->label_ready"
    )


def test_daily_summary_includes_recommended_pilot_wakeup_status() -> None:
    stats = DailyStats(entry_order_submit_count=1, buy_notional=2.5)
    message = _build_message(
        date(2026, 5, 31),
        stats,
        recommended_pilot_wakeup_status={
            "ok": True,
            "watched_market_count": 1,
            "triggered_market_count": 0,
            "refresh_ran": False,
            "watched_markets": [
                {"city": "buenos-aires", "event_date": "2026-06-01"},
            ],
        },
    )

    assert "Wakeup: 推荐Pilot结算 watch1/trigger0 | refresh=no" in message


def test_recommended_pilot_gap_refresh_line_surfaces_stale_priority_slug() -> None:
    line = _format_recommended_pilot_gap_refresh_line(
        {
            "ok": False,
            "queue_count": 2,
            "priority_slug_count": 4,
            "total_priority_slug_count": 4,
            "blockers": [
                "highest-temperature-in-atlanta-on-june-2-2026:tick_missing",
                "highest-temperature-in-ankara-on-june-2-2026:tick_stale",
            ],
            "rows": [
                {
                    "slug": "highest-temperature-in-atlanta-on-june-2-2026",
                    "tick_age_sec": None,
                    "next_action": "refresh_market_ticks_for_priority_gap_slug",
                }
            ],
            "next_action": "refresh_recommended_pilot_gap_priority_slugs",
        }
    )

    assert line == (
        "GapRefresh: queue2 slugs4/4 ok=False "
        "| 阻断 highest-temperature-in-atlanta-on-june-2-2026:tick_missing,"
        "highest-temperature-in-ankara-on-june-2-2026:tick_stale "
        "| highest-temperature-in-atlanta-on-june-2-2026:tick-:"
        "refresh_market_ticks_for_priority_gap_slug "
        "| next=refresh_recommended_pilot_gap_priority_slugs"
    )


def test_recommended_pilot_gap_plan_line_surfaces_sample_targets() -> None:
    line = _format_recommended_pilot_gap_plan_line(
        {
            "ok": False,
            "queue_count": 2,
            "city_count": 2,
            "strategy_gap_count": 3,
            "priority_slug_count": 4,
            "priority_slugs": [
                "highest-temperature-in-atlanta-on-june-1-2026",
                "highest-temperature-in-ankara-on-june-1-2026",
            ],
            "plan": [
                {
                    "city": "atlanta",
                    "needed_resolved_rows": 10,
                    "resolved_rows": 0,
                    "pending_rows": 1,
                    "strategy_gaps": [{"target_strategy": "secondary_or_physical_edge"}],
                },
                {
                    "city": "ankara",
                    "needed_resolved_rows": 7,
                    "resolved_rows": 3,
                    "pending_rows": 0,
                    "strategy_gaps": [
                        {"target_strategy": "reverse_edge_buy_no_sell_yes"},
                        {"target_strategy": "secondary_or_physical_edge"},
                    ],
                },
            ],
        }
    )

    assert line == (
        "GapPlan: queue2 city2 strat3 slugs4 ok=False "
        "| atlanta+10结(0/1,策1);ankara+7结(3/0,策2) "
        "| slug highest-temperature-in-atlanta-on-june-1-2026;"
        "highest-temperature-in-ankara-on-june-1-2026"
    )


def test_daily_summary_includes_recommended_pilot_gap_refresh_status() -> None:
    stats = DailyStats(entry_order_submit_count=1, buy_notional=2.5)
    message = _build_message(
        date(2026, 5, 31),
        stats,
        recommended_pilot_gap_refresh_status={
            "ok": False,
            "queue_count": 2,
            "priority_slug_count": 4,
            "total_priority_slug_count": 4,
            "blockers": ["highest-temperature-in-atlanta-on-june-2-2026:tick_missing"],
        },
    )

    assert "GapRefresh: queue2 slugs4/4 ok=False" in message


def test_daily_summary_includes_recommended_pilot_gap_plan_status() -> None:
    stats = DailyStats(entry_order_submit_count=1, buy_notional=2.5)
    message = _build_message(
        date(2026, 5, 31),
        stats,
        recommended_pilot_gap_plan_status={
            "ok": False,
            "queue_count": 1,
            "city_count": 1,
            "strategy_gap_count": 2,
            "priority_slug_count": 2,
            "plan": [
                {
                    "city": "ankara",
                    "needed_resolved_rows": 7,
                    "resolved_rows": 3,
                    "pending_rows": 0,
                    "strategy_gaps": [
                        {"target_strategy": "reverse_edge_buy_no_sell_yes"},
                        {"target_strategy": "secondary_or_physical_edge"},
                    ],
                }
            ],
        },
    )

    assert "GapPlan: queue1 city1 strat2 slugs2 ok=False" in message


def test_daily_summary_includes_strategy_batch_resource_focus() -> None:
    stats = DailyStats(entry_order_submit_count=1, buy_notional=2.5)
    message = _build_message(
        date(2026, 5, 31),
        stats,
        strategy_batch_status={
            "ok": True,
            "resource_focus_policy": "warn",
            "min_entry_edge": 0.10,
            "candidate_events": 2,
            "selected_events": 2,
            "excluded_entry_rows": 23,
            "warnings": ["selected_resource_focus_target_not_met"],
            "selected_resource_focus": {
                "structural_edge_share": 0.5,
                "major_settlement_share": 0.5,
                "target_met": False,
            },
            "selected_settlement_quality": {
                "selected_settlement_count": 1,
                "settlement_eligible_count": 1,
                "station_match_count": 1,
                "target_met": True,
            },
            "selected_physical_edge_quality": {
                "selected_d0_count": 2,
                "physical_evidence_count": 2,
                "bimodal_risk_count": 1,
                "target_met": True,
            },
            "targeted_candidate_status": {
                "status": "target_candidates_blocked_by_tradeability_or_evidence",
                "candidate_target_rows": 0,
                "target_city_count": 1,
                "target_strategy_count": 2,
                "recommended_pilot_observation_tracking_by_city": [
                    {
                        "city": "buenos-aires",
                        "resolved_rows": 1,
                        "pending_rows": 0,
                        "wins": 1,
                        }
                    ],
                    "recommended_pilot_observation_validation": {
                        "thresholds": {"min_resolved_rows": 10},
                        "by_city": [
                            {
                                "city": "buenos-aires",
                                "ok": False,
                                "overall": {
                                    "resolved_rows": 1,
                                    "hit_rate": 1.0,
                                },
                            }
                        ],
                        "sample_gap_queue": [
                            {
                                "city": "buenos-aires",
                                "needed_resolved_rows": 9,
                                "strategy_gaps": [
                                    {
                                        "target_strategy": "reverse_edge_buy_no_sell_yes",
                                        "needed_resolved_rows": 2,
                                    }
                                ],
                            }
                        ],
                    },
                },
            },
        )

    assert (
        "Strategy: 警告 selected_resource_focus_target_not_met | 选2/2 排除23 | "
        "结构50% 主要Settlement50% | 70/30 未达标 | edge≥10% live | warn"
    ) in message
    assert "推荐Pilot观察 buenos-aires:结1/待0/胜1" in message
    assert "Pilot验证 buenos-aires:结1/10/HR100%/wait" in message
    assert "Pilot缺口 buenos-aires:+9结/反NO+2" in message
    assert "SettlementQ 达标 eligible1/1 station1/1 | D0Phys 达标 2/2 bimodal1" in message


def test_strategy_batch_line_surfaces_enforce_blocker() -> None:
    line = _format_strategy_batch_line(
        {
            "ok": False,
            "resource_focus_policy": "enforce",
            "min_entry_edge": 0.10,
            "candidate_events": 2,
            "selected_events": 2,
            "excluded_entry_rows": 23,
            "blockers": ["selected_resource_focus_target_not_met"],
            "selected_resource_focus": {
                "structural_edge_share": 0.5,
                "major_settlement_share": 0.5,
                "target_met": False,
            },
            "selected_settlement_quality": {
                "selected_settlement_count": 1,
                "settlement_eligible_count": 0,
                "station_match_count": 0,
                "target_met": False,
            },
            "selected_physical_edge_quality": {
                "selected_d0_count": 2,
                "physical_evidence_count": 0,
                "bimodal_risk_count": 0,
                "target_met": False,
            },
        }
    )

    assert line == (
        "Strategy: 阻断 selected_resource_focus_target_not_met | 选2/2 排除23 | "
        "结构50% 主要Settlement50% | 70/30 未达标 | edge≥10% live | enforce "
        "| SettlementQ 未达标 eligible0/1 station0/1 | D0Phys 缺证据 0/2 bimodal0"
    )


def test_strategy_batch_line_surfaces_skipped_diagnostics() -> None:
    line = _format_strategy_batch_line(
        {
            "ok": True,
            "resource_focus_policy": "warn",
            "min_entry_edge": 0.10,
            "candidate_events": 0,
            "selected_events": 0,
            "excluded_entry_rows": 26,
            "excluded_entry_reasons": {"entry_usable_edge_nonpositive": 21},
            "skipped_recommendations": {"blocked_or_missing_edge": 241, "shadow_only": 167},
            "skipped_diagnostics": {
                "physical_bayes_positive_raw_edge_tradeability_missing": 12,
                "edge_lcb_too_thin_after_fees": 188,
            },
            "selected_resource_focus": {"target_met": None},
            "selected_settlement_quality": {"selected_settlement_count": 0},
            "selected_physical_edge_quality": {"selected_d0_count": 0},
        }
    )

    assert (
        "Strategy: 可执行 | 选0/0 排除26(entry_usable_edge_nonpositive:21) | "
        "结构— 主要Settlement— | 70/30 无样本 | edge≥10% live | warn "
        "| 非入场 blocked_or_missing_edge:241,shadow_only:167 "
        "| 分诊 edge_lcb_too_thin_after_fees:188,physical_bayes_positive_raw_edge_tradeability_missing:12"
    ) == line


def test_strategy_batch_line_surfaces_targeted_candidate_status() -> None:
    line = _format_strategy_batch_line(
        {
            "ok": True,
            "resource_focus_policy": "warn",
            "min_entry_edge": 0.10,
            "candidate_events": 0,
            "selected_events": 0,
            "excluded_entry_rows": 1,
            "excluded_entry_reasons": {"entry_usable_edge_nonpositive": 1},
            "selected_resource_focus": {"target_met": None},
            "selected_settlement_quality": {"selected_settlement_count": 0},
            "selected_physical_edge_quality": {"selected_d0_count": 0},
            "targeted_candidate_status": {
                "status": "target_candidates_blocked_by_tradeability_or_evidence",
                "candidate_target_rows": 0,
                "target_city_count": 1,
                "target_strategy_count": 2,
                "cache_stale": True,
                "top_excluded_entry_reasons": [
                    {"reason": "entry_usable_edge_nonpositive", "count": 1}
                ],
                "city_action_plan": [
                    {
                        "city": "chongqing",
                        "primary_reason": "entry_usable_edge_nonpositive",
                        "recommended_action": (
                            "keep_blocked_until_risk_discounted_edge_turns_positive"
                        ),
                    }
                ],
                "target_hunt_plan": [
                    {
                        "city": "chongqing",
                        "target_strategy": "reverse_edge_buy_no_sell_yes",
                        "needed_resolved_rows": 2,
                        "needed_additional_wins_for_hit_rate": 0,
                    },
                    {
                        "city": "chongqing",
                        "target_strategy": "d0_physical_settlement_hybrid",
                        "needed_resolved_rows": 0,
                        "needed_additional_wins_for_hit_rate": 1,
                    },
                ],
            },
        }
    )

    assert (
        " | TargetCand 目标被门槛挡 rows0/city1/strat2"
        "/缓存旧 entry_usable_edge_nonpositive:1"
    ) in line
    assert (
        " | 城市动作 chongqing:entry_usable_edge_nonpositive"
        "->keep_blocked_until_risk_discounted_edge_turns_positive"
    ) in line
    assert (
        " | 目标猎取 chongqing:reverse_edge_buy_no_sell_yes+2结;"
        "chongqing:d0_physical_settlement_hybrid+1胜"
    ) in line


def test_load_daily_stats_accepts_empty_trade_tables(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE auto_trade_decisions (
              id INTEGER PRIMARY KEY,
              action TEXT,
              details_json TEXT
            );
            CREATE TABLE edge_trade_fills (
              id INTEGER PRIMARY KEY,
              order_id TEXT,
              trade_id TEXT,
              trade_side TEXT,
              price REAL,
              size REAL,
              slug TEXT,
              event_date TEXT,
              outcome TEXT,
              outcome_side TEXT,
              trade_time TEXT,
              created_at TEXT
            );
            CREATE TABLE auto_orders (
              id INTEGER PRIMARY KEY,
              order_id TEXT,
              action TEXT,
              slug TEXT,
              event_date TEXT,
              outcome TEXT,
              side TEXT,
              strategy TEXT,
              created_at TEXT
            );
            CREATE TABLE edge_trades (
              id INTEGER PRIMARY KEY,
              slug TEXT,
              event_date TEXT,
              outcome TEXT,
              side TEXT,
              state TEXT,
              position_size REAL,
              avg_price REAL,
              entry_at TEXT,
              updated_at TEXT
            );
            CREATE TABLE event_market_ticks (
              id INTEGER PRIMARY KEY,
              slug TEXT,
              event_date TEXT,
              outcome TEXT,
              side TEXT,
              mid REAL,
              best_bid REAL,
              best_ask REAL,
              created_at TEXT
            );
            """
        )
        stats = _load_daily_stats(
            conn,
            "2026-05-30T16:00:00+00:00",
            "2026-05-31T16:00:00+00:00",
        )
    finally:
        conn.close()

    assert stats.buy_fill_count == 0
    assert stats.entry_order_submit_count == 0
