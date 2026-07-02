from __future__ import annotations

from pathlib import Path
from unittest.mock import call
from unittest.mock import patch

from scripts import strategy_objective_completion_audit as audit
from scripts.strategy_objective_completion_audit import _csv_values, _status


def test_status_treats_warnings_as_partial() -> None:
    assert _status(True, warnings=["no_selected_events_to_measure_share"]) == "partial"


def test_status_treats_blockers_as_blocked() -> None:
    assert _status(True, blockers=["paper_validation_not_passed"]) == "blocked"


def test_csv_values_dedupes_comma_and_repeat_inputs() -> None:
    assert _csv_values(["chongqing,toronto", "chongqing"]) == ["chongqing", "toronto"]


def test_seasonal_sizing_contract_proves_autumn_lower_size() -> None:
    status = audit._seasonal_sizing_contract(
        {
            "selected_rows": [
                {
                    "slug": "highest-temperature-in-melbourne-on-april-1-2026",
                    "outcome": "25C",
                    "weather_strategy_season": "autumn",
                    "weather_strategy_season_size_scale": 0.40,
                    "entry_usable_edge_size_scale_before_season": 1.0,
                    "entry_usable_edge_size_scale_adjusted": 0.40,
                }
            ]
        }
    )

    assert status["ok"] is True
    assert status["blockers"] == []
    assert status["evidence"]["autumn_size_scale"] == 0.40
    assert status["evidence"]["selected_autumn_rows"] == 1


def test_seasonal_sizing_contract_blocks_uncapped_autumn_selected_row() -> None:
    status = audit._seasonal_sizing_contract(
        {
            "selected_rows": [
                {
                    "slug": "highest-temperature-in-melbourne-on-april-1-2026",
                    "outcome": "25C",
                    "weather_strategy_season": "autumn",
                    "weather_strategy_season_size_scale": 1.0,
                    "entry_usable_edge_size_scale_before_season": 1.0,
                    "entry_usable_edge_size_scale_adjusted": 1.0,
                }
            ]
        }
    )

    assert status["ok"] is False
    assert "selected_row_seasonal_size_scale_violation" in status["blockers"]
    assert status["evidence"]["violation_count"] == 1


def test_build_report_auto_selects_pilot_city_from_matrix_when_not_configured() -> None:
    captured_small_cap: dict[str, object] = {}
    captured_rolling: dict[str, object] = {}
    captured_scale_up: dict[str, object] = {}

    def fake_decision_core(*args: object, **kwargs: object) -> dict:
        return {
            "ok": True,
            "violations": {
                "source_station_mismatch_rows": 0,
                "missing_entry_usable_edge_rows": 0,
                "entry_unusable_edge_order_violations": [],
                "exit_policy_missing_rows": 0,
                "exit_policy_edge_source_violations": [],
            },
            "latest_exit_policy_coverage": {},
        }

    def fake_small_cap(*args: object, **kwargs: object) -> dict:
        captured_small_cap.update(kwargs)
        return {"ok": False, "blockers": ["paper_validation_not_passed:chongqing"], "warnings": []}

    def fake_rolling(*args: object, **kwargs: object) -> dict:
        captured_rolling.update(kwargs)
        return {"ok": False, "blockers": ["paper_observation_resolved_rows_below_min"], "warnings": []}

    def fake_scale_up(*args: object, **kwargs: object) -> dict:
        captured_scale_up.update(kwargs)
        return {"ok": False, "blockers": ["paper_observation_validation_not_passed"], "warnings": []}

    batch = {
        "ok": True,
        "selected_events": 0,
        "candidate_events": 0,
        "include_event_keys": [],
        "selected_rows": [],
        "selected_resource_focus": {"target_met": None},
        "selected_physical_edge_quality": {},
        "selected_settlement_quality": {},
        "excluded_entry_reasons": {},
        "skipped_diagnostics": {},
        "min_entry_edge": 0.10,
        "protected_focus_min_share": 0.7,
        "major_settlement_max_share": 0.3,
        "require_d0_physical_evidence": True,
        "observation_target_focus": {"enabled": True, "package_ok": True},
    }
    matrix_all = {
        "ok": False,
        "city_count": 2,
        "next_mainline_task": "paper_observation_rolling_validation",
        "cities": [
            {"city": "london", "blockers": ["hit_rate_missing"]},
            {"city": "chongqing", "blockers": ["resolved_rows_below_min"], "resolved_rows": 5},
            {"city": "nyc", "blockers": ["resolved_rows_below_min"], "resolved_rows": 4},
            {"city": "seattle", "blockers": ["hit_rate_below_min"], "resolved_rows": 2},
            {"city": "buenos aires", "blockers": ["resolved_rows_below_min"], "resolved_rows": 1},
        ],
    }
    matrix_filtered = {
        **matrix_all,
        "cities": [
            {"city": "chongqing", "blockers": ["resolved_rows_below_min"], "resolved_rows": 5},
            {"city": "buenos aires", "blockers": ["resolved_rows_below_min"], "resolved_rows": 1},
        ],
    }

    with (
        patch.dict(audit.os.environ, {}, clear=True),
        patch.object(audit.weather_strategy_batch, "build_strategy_batch", return_value=batch),
        patch.object(audit, "_decision_core_audit", side_effect=fake_decision_core),
        patch.object(audit.paper_trading_validation, "build_report", return_value={"ok": True, "blockers": []}),
        patch.object(audit.small_capital_validation_review, "build_report", side_effect=fake_small_cap),
        patch.object(audit.paper_observation_rolling_validation, "build_report", side_effect=fake_rolling),
        patch.object(audit.scale_up_validation_review, "build_report", side_effect=fake_scale_up),
        patch.object(
            audit.pilot_city_validation_matrix,
            "build_report",
            side_effect=[matrix_all, matrix_filtered],
        ) as matrix_build,
        patch.object(
            audit.paper_observation_target_package,
            "build_package",
            return_value={"ok": False, "city_targets": [], "strategy_targets": []},
        ),
        patch.object(audit, "_launchd_monitor_status", return_value={"ok": True, "uses_auto_trade_watcher": True, "working_directory_weather": True}),
    ):
        report = audit.build_report(
            Path("/tmp/archive.db"),
            event_dates=["2026-05-31"],
            pilot_cities=[],
        )

    assert report["pilot_cities"] == ["chongqing", "buenos aires"]
    assert report["pilot_city_source"] == "auto_matrix"
    assert captured_small_cap["pilot_cities"] == ["chongqing", "buenos aires"]
    assert captured_rolling["pilot_cities"] == ["chongqing", "buenos aires"]
    assert captured_scale_up["pilot_cities"] == ["chongqing", "buenos aires"]
    assert matrix_build.call_args_list[0] == call(
        Path("/tmp/archive.db"),
        event_dates=["2026-05-31"],
        market_scope="highest_temperature",
        cities=[],
    )
    assert matrix_build.call_args_list[1] == call(
        Path("/tmp/archive.db"),
        event_dates=["2026-05-31"],
        market_scope="highest_temperature",
        cities=["chongqing", "buenos aires"],
    )


def test_auto_pilot_city_selection_skips_paper_validation_failures() -> None:
    matrix = {
        "cities": [
            {"city": "chongqing", "blockers": ["resolved_rows_below_min"]},
            {"city": "buenos aires", "blockers": ["resolved_rows_below_min"]},
        ]
    }
    raw_cities = audit._auto_pilot_cities_from_matrix(matrix)

    def fake_paper_report(db_path: Path, *, city: str = "", **kwargs: object) -> dict:
        return {"ok": city == "chongqing", "blockers": [] if city == "chongqing" else ["ece_high"]}

    with patch.object(audit.paper_trading_validation, "build_report", side_effect=fake_paper_report):
        filtered = audit._filter_auto_pilot_cities_by_paper_validation(
            Path("/tmp/archive.db"),
            raw_cities,
            since_days=42,
            market_scope="highest_temperature",
        )

    assert raw_cities == ["chongqing", "buenos aires"]
    assert filtered == ["chongqing"]


def test_auto_pilot_city_selection_keeps_hit_rate_missing_for_observation() -> None:
    matrix = {
        "cities": [
            {"city": "ankara", "blockers": ["resolved_rows_below_min"]},
            {"city": "atlanta", "blockers": ["hit_rate_missing"]},
            {"city": "toronto", "blockers": ["hit_rate_missing"]},
            {"city": "nyc", "blockers": ["hit_rate_missing"]},
            {"city": "seattle", "blockers": ["hit_rate_below_min"]},
        ]
    }

    assert audit._auto_pilot_cities_from_matrix(matrix, limit=4) == [
        "ankara",
        "atlanta",
        "toronto",
    ]


def test_build_report_passes_core_since_to_decision_audit() -> None:
    captured: dict[str, object] = {}

    def fake_decision_core(db_path: Path, *, since_hours: float, limit: int, since: str = "") -> dict:
        captured["db_path"] = db_path
        captured["since_hours"] = since_hours
        captured["limit"] = limit
        captured["since"] = since
        return {
            "ok": True,
            "violations": {
                "source_station_mismatch_rows": 0,
                "missing_entry_usable_edge_rows": 0,
                "entry_unusable_edge_order_violations": [],
                "exit_policy_missing_rows": 0,
                "exit_policy_edge_source_violations": [],
            },
            "latest_exit_policy_coverage": {},
        }

    batch = {
        "ok": True,
        "selected_events": 0,
        "candidate_events": 0,
        "include_event_keys": [],
        "selected_rows": [],
        "selected_resource_focus": {"target_met": None},
        "selected_physical_edge_quality": {},
        "selected_settlement_quality": {},
        "excluded_entry_reasons": {},
        "skipped_diagnostics": {},
        "min_entry_edge": 0.10,
        "protected_focus_min_share": 0.7,
        "major_settlement_max_share": 0.3,
        "require_d0_physical_evidence": True,
        "observation_scope_cities": ["buenos-aires", "chongqing"],
        "observation_target_focus": {
            "enabled": True,
            "package_ok": True,
            "candidate_target_rows": 0,
            "candidate_expansion_rows": 0,
            "expansion_city_keys": ["buenos-aires"],
        },
    }
    validation_ok = {"ok": True, "blockers": []}
    matrix_ok = {
        "ok": True,
        "city_count": 1,
        "next_mainline_task": "strategy_objective_completion_audit_for_best_pilot_city",
        "cities": [
            {
                "city": "chongqing",
                "ok": True,
                "resolved_rows": 10,
                "pending_rows": 0,
                "hit_rate": 0.7,
                "blockers": [],
            }
        ],
    }
    target_package = {
        "ok": False,
        "next_mainline_task": "collect_targeted_paper_observation_samples",
        "city_targets": [
            {
                "city": "chongqing",
                "needed_resolved_rows": 5,
                "needed_additional_wins_for_hit_rate": 0,
                "blockers": ["resolved_rows_below_min"],
            }
        ],
        "strategy_targets": [
            {
                "city": "chongqing",
                "target_strategy": "reverse_edge_buy_no_sell_yes",
                "needed_resolved_rows": 2,
                "needed_additional_wins_for_hit_rate": 0,
                "recommended_observation_action": "prioritize_reverse_no_tail_candidates",
            }
        ],
        "observation_expansion_city_targets": [
            {
                "city": "buenos aires",
                "needed_resolved_rows": 9,
                "needed_additional_wins_for_hit_rate": 0,
                "pending_rows": 1,
                "hit_rate": 1.0,
                "blockers": ["resolved_rows_below_min"],
            }
        ],
    }

    captured_batch: dict[str, object] = {}
    settlement_pipeline = {
        "ok": False,
        "read_only": True,
        "next_action": "confirm_and_apply_settlement_actuals_then_rebuild_labels",
        "settlement_actual_missing_count": 2,
        "settlement_actual_ready_count": 2,
        "settlement_label_planned_count": 0,
        "simulated_label_planned_after_actual_apply": 88,
        "simulated_actual_inserted_count": 2,
        "simulated_label_apply_inserted_count": 88,
        "simulated_label_planned_after_label_apply": 0,
        "simulated_label_apply_closed_loop_ok": True,
        "simulation_db_mode": "subset",
        "settlement_label_skipped": {"settlement_missing": 1778},
        "settlement_actual_apply_confirm_command": (
            ".venv/bin/python scripts/apply_settlement_actual_repair_plan.py "
            "--apply --confirm-apply APPLY_SETTLEMENT_ACTUALS"
        ),
        "pre_apply_backup_required": True,
        "backup_bypass_effect": "confirmed_apply_blocked",
    }

    def fake_strategy_batch(db_path: Path, event_dates: list[str], **kwargs: object) -> dict:
        captured_batch["db_path"] = db_path
        captured_batch["event_dates"] = event_dates
        captured_batch.update(kwargs)
        return batch

    with (
        patch.object(audit.weather_strategy_batch, "build_strategy_batch", side_effect=fake_strategy_batch),
        patch.object(audit, "_decision_core_audit", side_effect=fake_decision_core),
        patch.object(audit.paper_trading_validation, "build_report", return_value=validation_ok),
        patch.object(audit.small_capital_validation_review, "build_report", return_value=validation_ok),
        patch.object(audit.paper_observation_rolling_validation, "build_report", return_value=validation_ok),
        patch.object(audit.scale_up_validation_review, "build_report", return_value=validation_ok),
        patch.object(audit.pilot_city_validation_matrix, "build_report", return_value=matrix_ok),
        patch.object(audit.paper_observation_target_package, "build_package", return_value=target_package),
        patch.object(
            audit,
            "_settlement_validation_pipeline_snapshot",
            return_value=settlement_pipeline,
        ),
        patch.object(
            audit,
            "_launchd_monitor_status",
            return_value={
                "ok": True,
                "uses_auto_trade_watcher": True,
                "working_directory_weather": True,
            },
        ),
    ):
        report = audit.build_report(
            Path("/tmp/archive.db"),
            event_dates=["2026-05-31"],
            pilot_cities=["chongqing"],
            since_hours=24,
            core_since="2026-05-31T10:20:27+00:00",
        )

    assert captured["since"] == "2026-05-31T10:20:27+00:00"
    assert captured["since_hours"] == 24
    assert captured_batch["observation_target_focus"] is True
    assert captured_batch["observation_target_package"] is target_package
    assert captured_batch["pilot_cities"] == ["chongqing"]
    assert report["core_since"] == "2026-05-31T10:20:27+00:00"
    assert report["pilot_city_validation_matrix_summary"]["top_cities"][0]["city"] == "chongqing"
    assert (
        report["paper_observation_target_package_summary"]["strategy_targets"][0]["target_strategy"]
        == "reverse_edge_buy_no_sell_yes"
    )
    validation_report = report["automation_reports"]["paper_small_cap_scale_validation_before_scaling"]
    assert validation_report["pilot_city_matrix_top"][0]["resolved_rows"] == 10
    assert validation_report["settlement_validation_pipeline"] == settlement_pipeline
    safety_req = [
        item for item in report["requirements"] if item["name"] == "settlement_write_safety_backup_gate"
    ][0]
    assert safety_req["ok"] is True
    assert safety_req["evidence"]["backup_bypass_effect"] == "confirmed_apply_blocked"
    assert report["settlement_validation_pipeline_summary"] == settlement_pipeline
    assert validation_report["observation_strategy_targets"][0]["needed_resolved_rows"] == 2
    assert (
        validation_report["observation_expansion_city_targets"][0]["city"]
        == "buenos aires"
    )
    assert validation_report["strategy_batch_observation_target_focus"]["enabled"] is True
    assert report["current_strategy_batch_summary"]["observation_target_focus"]["package_ok"] is True
    assert report["current_strategy_batch_summary"]["observation_scope_cities"] == [
        "buenos-aires",
        "chongqing",
    ]
    assert (
        report["paper_observation_target_package_summary"]["observation_expansion_city_targets"][0]["city"]
        == "buenos aires"
    )
    status = report["current_strategy_batch_summary"]["targeted_candidate_status"]
    assert status["status"] == "no_target_candidate_in_selected_pilot_cities"
    assert status["next_action"] == "wait_for_new_market_or_settlement_label_in_selected_pilot_cities"
    assert status["candidate_expansion_rows"] == 0
    assert status["expansion_city_count"] == 1
    assert status["observation_scope_cities"] == ["buenos-aires", "chongqing"]


def test_targeted_candidate_status_reports_tradeability_or_evidence_block() -> None:
    batch = {
        "selected_events": 0,
        "candidate_events": 0,
        "excluded_entry_reasons": {"entry_usable_edge_nonpositive": 2},
        "excluded_entry_reasons_by_city": {
            "chongqing": {"entry_usable_edge_nonpositive": 2}
        },
        "skipped_diagnostics": {"d0_physical_evidence_missing": 1},
        "skipped_diagnostics_by_city": {
            "buenos-aires": {"d0_physical_evidence_missing": 1}
        },
        "observation_target_focus": {
            "enabled": True,
            "package_ok": True,
            "candidate_target_rows": 0,
            "candidate_expansion_rows": 0,
        },
        "observation_scope_cities": ["buenos-aires", "chongqing"],
    }
    targets = {
        "city_targets": [{"city": "chongqing", "needed_resolved_rows": 5}],
        "strategy_targets": [
            {
                "city": "chongqing",
                "target_strategy": "reverse_edge_buy_no_sell_yes",
                "needed_resolved_rows": 2,
                "needed_additional_wins_for_hit_rate": 0,
                "recommended_observation_action": "prioritize_reverse_no_tail_candidates",
            }
        ],
        "observation_expansion_city_targets": [
            {"city": "buenos aires", "needed_resolved_rows": 9}
        ],
    }

    status = audit._targeted_candidate_status(
        batch,
        targets,
        pilot_cities=["chongqing"],
        pilot_city_source="auto_matrix",
    )

    assert status["status"] == "target_candidates_blocked_by_tradeability_or_evidence"
    assert status["next_action"] == "keep_scanning_until_edge_physical_source_or_liquidity_gates_turn_positive"
    assert status["candidate_target_rows"] == 0
    assert status["candidate_expansion_rows"] == 0
    assert status["target_city_count"] == 1
    assert status["expansion_city_count"] == 1
    assert status["target_strategy_count"] == 1
    assert status["observation_scope_cities"] == ["buenos-aires", "chongqing"]
    assert status["top_excluded_entry_reasons"] == [
        {"reason": "entry_usable_edge_nonpositive", "count": 2}
    ]
    assert status["top_skipped_diagnostics"] == [
        {"reason": "d0_physical_evidence_missing", "count": 1}
    ]
    assert status["top_excluded_entry_reasons_by_city"] == [
        {
            "city": "chongqing",
            "count": 2,
            "top_reasons": [{"reason": "entry_usable_edge_nonpositive", "count": 2}],
        }
    ]
    assert status["top_skipped_diagnostics_by_city"] == [
        {
            "city": "buenos-aires",
            "count": 1,
            "top_reasons": [{"reason": "d0_physical_evidence_missing", "count": 1}],
        }
    ]
    assert status["city_action_plan"] == [
        {
            "city": "chongqing",
            "count": 2,
            "primary_reason": "entry_usable_edge_nonpositive",
            "primary_count": 2,
            "recommended_action": "keep_blocked_until_risk_discounted_edge_turns_positive",
            "top_reasons": [{"reason": "entry_usable_edge_nonpositive", "count": 2}],
        },
        {
            "city": "buenos-aires",
            "count": 1,
            "primary_reason": "d0_physical_evidence_missing",
            "primary_count": 1,
            "recommended_action": "wait_for_metar_radiation_or_physical_likelihood_evidence",
            "top_reasons": [{"reason": "d0_physical_evidence_missing", "count": 1}],
        },
    ]
    assert status["target_hunt_plan"] == [
        {
            "city": "chongqing",
            "target_strategy": "reverse_edge_buy_no_sell_yes",
            "needed_resolved_rows": 2,
            "needed_additional_wins_for_hit_rate": 0,
            "recommended_observation_action": "prioritize_reverse_no_tail_candidates",
            "scan_directive": "scan_open_tail_no_reverse_edge_candidates",
            "hunt_status": "target_city_seen_blocked_or_nonentry",
            "hunt_status_detail": "entry_usable_edge_nonpositive",
            "hunt_blocked_action": "keep_blocked_until_risk_discounted_edge_turns_positive",
        }
    ]


def test_city_action_plan_marks_settled_or_past_as_wait_for_next_event() -> None:
    status = audit._targeted_candidate_status(
        {
            "selected_events": 0,
            "candidate_events": 0,
            "excluded_entry_reasons": {
                "market_settled_or_past_no_new_entry": 3,
            },
            "excluded_entry_reasons_by_city": {
                "chongqing": {"market_settled_or_past_no_new_entry": 3}
            },
            "skipped_diagnostics": {},
            "skipped_diagnostics_by_city": {},
            "observation_target_focus": {
                "enabled": True,
                "package_ok": True,
                "candidate_target_rows": 0,
            },
            "observation_scope_cities": ["chongqing"],
        },
        {"city_targets": [{"city": "chongqing"}], "strategy_targets": []},
        pilot_cities=["chongqing"],
        pilot_city_source="cli",
    )

    assert status["city_action_plan"][0]["primary_reason"] == (
        "market_settled_or_past_no_new_entry"
    )
    assert status["city_action_plan"][0]["recommended_action"] == (
        "wait_for_next_open_event_date_or_use_settlement_review_only"
    )


def test_city_action_plan_marks_settlement_watch_as_review_only() -> None:
    status = audit._targeted_candidate_status(
        {
            "selected_events": 0,
            "candidate_events": 0,
            "excluded_entry_reasons": {},
            "excluded_entry_reasons_by_city": {},
            "skipped_diagnostics": {"settlement_watch_not_entry": 4},
            "skipped_diagnostics_by_city": {
                "chongqing": {"settlement_watch_not_entry": 4}
            },
            "observation_target_focus": {
                "enabled": True,
                "package_ok": True,
                "candidate_target_rows": 0,
            },
            "observation_scope_cities": ["chongqing"],
        },
        {"city_targets": [{"city": "chongqing"}], "strategy_targets": []},
        pilot_cities=["chongqing"],
        pilot_city_source="cli",
    )

    assert status["city_action_plan"][0]["primary_reason"] == "settlement_watch_not_entry"
    assert status["city_action_plan"][0]["recommended_action"] == (
        "settlement_review_only_do_not_open_new_edge"
    )


def test_target_hunt_plan_distinguishes_available_from_not_seen() -> None:
    targets = {
        "city_targets": [{"city": "chongqing"}],
        "strategy_targets": [
            {
                "city": "chongqing",
                "target_strategy": "reverse_edge_buy_no_sell_yes",
                "needed_resolved_rows": 2,
            },
            {
                "city": "istanbul",
                "target_strategy": "d0_physical_settlement_hybrid",
                "needed_additional_wins_for_hit_rate": 1,
            },
        ],
    }

    status = audit._targeted_candidate_status(
        {
            "selected_events": 1,
            "candidate_events": 1,
            "excluded_entry_reasons": {},
            "excluded_entry_reasons_by_city": {},
            "skipped_diagnostics": {},
            "skipped_diagnostics_by_city": {},
            "observation_target_focus": {
                "enabled": True,
                "package_ok": True,
                "candidate_target_rows": 1,
                "candidate_target_rows_by_strategy": {
                    "reverse_edge_buy_no_sell_yes": 1,
                },
                "candidate_target_rows_by_city": {"chongqing": 1},
            },
            "observation_scope_cities": ["chongqing", "istanbul"],
        },
        targets,
        pilot_cities=["chongqing"],
        pilot_city_source="cli",
    )

    by_strategy = {
        item["target_strategy"]: item for item in status["target_hunt_plan"]
    }
    assert by_strategy["reverse_edge_buy_no_sell_yes"]["hunt_status"] == (
        "target_candidate_available"
    )
    assert by_strategy["d0_physical_settlement_hybrid"]["hunt_status"] == (
        "target_not_seen_in_current_scan_scope"
    )


def test_target_hunt_plan_marks_paper_observation_available() -> None:
    targets = {
        "city_targets": [{"city": "chongqing"}],
        "strategy_targets": [
            {
                "city": "chongqing",
                "target_strategy": "reverse_edge_buy_no_sell_yes",
                "needed_resolved_rows": 2,
            },
        ],
    }

    status = audit._targeted_candidate_status(
        {
            "selected_events": 0,
            "candidate_events": 0,
            "excluded_entry_reasons": {},
            "excluded_entry_reasons_by_city": {},
            "skipped_diagnostics": {
                "edge_lcb_too_thin_after_fees": 1,
            },
            "skipped_diagnostics_by_city": {
                "chongqing": {"edge_lcb_too_thin_after_fees": 1}
            },
            "observation_target_focus": {
                "enabled": True,
                "package_ok": True,
                "candidate_target_rows": 0,
            },
            "observation_scope_cities": ["chongqing"],
        },
        targets,
        pilot_cities=["chongqing"],
        pilot_city_source="cli",
        paper_observation_availability={
            "chongqing|reverse_edge_buy_no_sell_yes": {
                "paper_observation_rows": 1,
                "paper_observation_actions": {"paper_track_reverse_edge_no_side": 1},
                "paper_observation_slugs": [
                    "chongqing-high-temperature-june-1"
                ],
            }
        },
    )

    hunt = status["target_hunt_plan"][0]
    assert hunt["hunt_status"] == "target_paper_observe_available"
    assert hunt["hunt_status_detail"] == "paper_track_candidate_available_but_not_entry"
    assert hunt["paper_observation_rows"] == 1
    assert hunt["paper_observation_actions"] == {"paper_track_reverse_edge_no_side": 1}
    assert hunt["paper_observation_slugs"] == [
        "chongqing-high-temperature-june-1"
    ]


def test_paper_observation_availability_from_gap_review_groups_by_city_strategy() -> None:
    availability = audit._paper_observation_availability_from_gap_review(
        {
            "rows": [
                {
                    "slug": "highest-temperature-in-chongqing-on-june-1",
                    "candidate_strategy": "reverse_edge_buy_no_sell_yes",
                    "review_action": "paper_track_reverse_edge_no_side",
                },
                {
                    "slug": "highest-temperature-in-chongqing-on-june-2",
                    "candidate_strategy": "reverse_edge_buy_no_sell_yes",
                    "review_action": "exclude_until_tradeability_gap_fixed",
                },
            ]
        }
    )

    assert availability == {
        "chongqing|reverse_edge_buy_no_sell_yes": {
            "city": "chongqing",
            "target_strategy": "reverse_edge_buy_no_sell_yes",
            "paper_observation_rows": 1,
            "paper_observation_actions": {"paper_track_reverse_edge_no_side": 1},
            "paper_observation_slugs": ["highest-temperature-in-chongqing-on-june-1"],
        }
    }


def test_expansion_hunt_plan_surfaces_non_pilot_observation_rows() -> None:
    plan = audit._expansion_hunt_plan(
        {
            "observation_expansion_city_targets": [
                {"city": "toronto", "needed_resolved_rows": 10, "pending_rows": 4},
                {"city": "madrid", "needed_resolved_rows": 10, "pending_rows": 0},
            ]
        },
        paper_observation_availability={
            "toronto|reverse_edge_buy_no_sell_yes": {
                "target_strategy": "reverse_edge_buy_no_sell_yes",
                "paper_observation_rows": 2,
                "paper_observation_actions": {"paper_track_reverse_edge_no_side": 2},
            },
            "toronto|d0_physical_settlement_hybrid": {
                "target_strategy": "d0_physical_settlement_hybrid",
                "paper_observation_rows": 1,
                "paper_observation_actions": {"paper_track_d0_physical_or_settlement": 1},
            },
        },
    )

    assert plan[0] == {
        "city": "toronto",
        "needed_resolved_rows": 10,
        "needed_additional_wins_for_hit_rate": 0,
        "pending_rows": 4,
        "paper_observation_rows": 3,
        "paper_observation_strategies": [
            {
                "target_strategy": "reverse_edge_buy_no_sell_yes",
                "paper_observation_rows": 2,
                "paper_observation_actions": {"paper_track_reverse_edge_no_side": 2},
            },
            {
                "target_strategy": "d0_physical_settlement_hybrid",
                "paper_observation_rows": 1,
                "paper_observation_actions": {"paper_track_d0_physical_or_settlement": 1},
            },
        ],
        "hunt_status": "expansion_paper_observe_available",
    }
    assert plan[1]["city"] == "madrid"
    assert plan[1]["hunt_status"] == "expansion_wait_for_open_market_matching_scope"


def test_observation_tracking_focus_summarizes_target_and_expansion_rows() -> None:
    focus = audit._observation_tracking_focus(
        {
            "strategy_targets": [
                {
                    "city": "chongqing",
                    "target_strategy": "reverse_edge_buy_no_sell_yes",
                }
            ],
            "observation_expansion_city_targets": [
                {"city": "toronto"},
            ],
            "recommended_pilot_city_targets": [
                {"city": "buenos aires"},
            ],
        },
        {
            "rows": [
                {
                    "city": "chongqing",
                    "candidate_strategy": "reverse_edge_buy_no_sell_yes",
                    "tracking_status": "resolved_from_settlement",
                    "label": 1,
                },
                {
                    "city": "toronto",
                    "candidate_strategy": "d0_physical_settlement_hybrid",
                    "tracking_status": "pending_settlement_label",
                    "label": None,
                },
                {
                    "city": "buenos-aires",
                    "candidate_strategy": "reverse_edge_buy_no_sell_yes",
                    "tracking_status": "resolved_from_settlement",
                    "label": 1,
                },
            ]
        },
    )

    assert focus["target_tracking_summary"]["resolved_rows"] == 1
    assert focus["target_tracking_summary"]["wins"] == 1
    assert focus["expansion_tracking_summary"]["candidate_rows"] == 1
    assert focus["expansion_tracking_summary"]["pending_rows"] == 1
    assert focus["expansion_tracking_by_city"][0]["city"] == "toronto"
    assert focus["recommended_pilot_tracking_summary"]["resolved_rows"] == 1
    assert focus["recommended_pilot_tracking_summary"]["wins"] == 1
    assert focus["recommended_pilot_tracking_by_city"][0]["city"] == "buenos-aires"
    validation = focus["recommended_pilot_observation_validation"]
    assert validation["ok"] is False
    assert validation["by_city"][0]["city"] == "buenos-aires"
    assert "recommended_pilot_resolved_rows_below_min" in validation["by_city"][0]["blockers"]
    assert "recommended_pilot_strategy_rows_below_min:reverse_edge_buy_no_sell_yes" in (
        validation["by_city"][0]["blockers"]
    )
    assert validation["sample_gap_queue"][0]["city"] == "buenos-aires"
    assert validation["sample_gap_queue"][0]["needed_resolved_rows"] == 9
    assert validation["sample_gap_queue"][0]["strategy_gaps"][0] == {
        "target_strategy": "reverse_edge_buy_no_sell_yes",
        "resolved_rows": 1,
        "needed_resolved_rows": 2,
        "scan_directive": "scan_open_tail_no_reverse_edge_candidates",
    }


def test_recommended_pilot_observation_validation_passes_with_enough_rows() -> None:
    rows = [
        {
            "city": "buenos-aires",
            "candidate_strategy": "reverse_edge_buy_no_sell_yes",
            "tracking_status": "labeled",
            "label": 1 if idx < 8 else 0,
        }
        for idx in range(10)
    ]

    validation = audit._recommended_pilot_observation_validation(
        {
            "recommended_pilot_city_targets": [
                {"city": "buenos aires", "observation_mode": "paper_only_not_small_capital"},
            ],
        },
        rows,
    )

    assert validation["ok"] is True
    assert validation["blockers"] == []
    assert validation["sample_gap_queue"] == []
    assert validation["by_city"][0]["overall"]["hit_rate"] == 0.8


def test_pilot_city_recommendation_adds_second_city_from_expansion_pool() -> None:
    recommendation = audit._pilot_city_recommendation(
        pilot_cities=["chongqing"],
        pilot_city_source="auto_matrix",
        observation_tracking_focus={
            "target_tracking_summary": {
                "candidate_rows": 5,
                "resolved_rows": 5,
                "pending_rows": 0,
                "wins": 3,
                "hit_rate": 0.6,
            },
            "expansion_tracking_by_city": [
                {
                    "city": "toronto",
                    "candidate_rows": 4,
                    "resolved_rows": 1,
                    "pending_rows": 3,
                    "wins": 1,
                    "hit_rate": 1.0,
                }
            ],
        },
        )

    assert recommendation["action"] == "add_second_pilot_city_for_paper_observation"
    assert recommendation["recommended_pilot_cities"] == ["chongqing", "toronto"]
    assert recommendation["best_expansion_candidate"]["city"] == "toronto"


def test_pilot_city_recommendation_can_suggest_switch_after_resolved_review() -> None:
    recommendation = audit._pilot_city_recommendation(
        pilot_cities=["chongqing", "buenos-aires"],
        pilot_city_source="auto_matrix",
        observation_tracking_focus={
            "target_tracking_summary": {
                "candidate_rows": 5,
                "resolved_rows": 3,
                "pending_rows": 0,
                "wins": 1,
                "hit_rate": 0.333,
            },
            "expansion_tracking_by_city": [
                {
                    "city": "toronto",
                    "candidate_rows": 6,
                    "resolved_rows": 4,
                    "pending_rows": 1,
                    "wins": 3,
                    "hit_rate": 0.75,
                }
            ],
        },
        max_pilot_cities=2,
    )

    assert recommendation["action"] == (
        "switch_pilot_candidate_after_resolved_validation_review"
    )
    assert recommendation["recommended_pilot_cities"] == ["toronto"]


def test_recommended_pilot_settlement_readiness_surfaces_planned_labels(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_label_plan(*args: object, **kwargs: object) -> dict:
        assert kwargs["city"] == "buenos-aires"
        assert kwargs["strict_resolution_source"] is True
        assert kwargs["allow_open_events"] is False
        return {
            "ok": True,
            "decisions_scanned": 4,
            "planned_labels": 2,
            "label_counts": {"1": 1, "0": 1},
            "skipped": {"already_labeled": 1},
            "settlement_skipped": {},
            "strict_resolution_source": True,
            "labels": [{"decision_id": 10}, {"decision_id": 11}],
        }

    def fake_eligibility(*args: object, **kwargs: object) -> dict:
        assert kwargs["city"] == "buenos-aires"
        return {
            "ok": True,
            "market_count": 2,
            "ready_market_count": 1,
            "status_counts": {"label_ready": 1, "closed_without_settlement_actual": 1},
            "markets": [
                {"slug": "ready", "status": "label_ready"},
                {"slug": "missing", "status": "closed_without_settlement_actual"},
            ],
        }

    monkeypatch.setattr(
        audit.build_settlement_calibration_labels,
        "build_label_plan",
        fake_label_plan,
    )
    monkeypatch.setattr(
        audit.settlement_label_eligibility_report,
        "build_report",
        fake_eligibility,
    )

    readiness = audit._recommended_pilot_settlement_readiness(
        tmp_path / "archive.db",
        {
            "buenos-aires": {
                "primary_action": "wait_for_settlement_label_or_review_only_no_new_entry"
            }
        },
        since_days=42,
        market_scope="highest_temperature",
    )

    city = readiness["by_city"]["buenos-aires"]
    assert readiness["total_planned_labels"] == 2
    assert readiness["total_decisions_scanned"] == 4
    assert readiness["next_action"] == (
        "review_settlement_label_plan_then_apply_after_confirmation"
    )
    assert city["next_action"] == (
        "review_settlement_label_plan_then_apply_after_confirmation"
    )
    assert city["eligibility_status_counts"] == {
        "label_ready": 1,
        "closed_without_settlement_actual": 1,
    }
    assert city["eligibility_markets_preview"] == [
        {"slug": "ready", "status": "label_ready"},
        {"slug": "missing", "status": "closed_without_settlement_actual"},
    ]
    assert city["labels_preview"] == [{"decision_id": 10}, {"decision_id": 11}]


def test_recommended_pilot_settlement_readiness_waits_for_missing_settlement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        audit.build_settlement_calibration_labels,
        "build_label_plan",
        lambda *args, **kwargs: {
            "ok": True,
            "decisions_scanned": 3,
            "planned_labels": 0,
            "label_counts": {},
            "skipped": {"settlement_missing": 3},
            "settlement_skipped": {},
            "strict_resolution_source": True,
            "labels": [],
        },
    )
    monkeypatch.setattr(
        audit.settlement_label_eligibility_report,
        "build_report",
        lambda *args, **kwargs: {
            "ok": True,
            "market_count": 1,
            "ready_market_count": 0,
            "status_counts": {"event_run_missing": 1},
            "markets": [{"slug": "missing", "status": "event_run_missing"}],
        },
    )

    readiness = audit._recommended_pilot_settlement_readiness(
        tmp_path / "archive.db",
        {
            "buenos-aires": {
                "primary_action": "wait_for_settlement_label_or_review_only_no_new_entry"
            },
            "toronto": {
                "primary_action": (
                    "wait_for_new_metar_or_new_market_until_physical_bayes_edge_turns_positive"
                )
            },
        },
        since_days=42,
        market_scope="highest_temperature",
    )

    assert list(readiness["by_city"]) == ["buenos-aires"]
    assert readiness["total_planned_labels"] == 0
    assert readiness["by_city"]["buenos-aires"]["next_action"] == (
        "wait_for_settlement_actual_then_rebuild_label_plan"
    )
    assert readiness["by_city"]["buenos-aires"]["eligibility_status_counts"] == {
        "event_run_missing": 1
    }
