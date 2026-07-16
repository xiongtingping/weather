from daily_weather_review_runtime import build_daily_weather_review, daily_review_input_contract


def _fact_snapshot():
    return {
        "fact_snapshot_id": "fact_1234567890abcdef",
        "lineage_state": {
            "fact_snapshot_id": "fact_1234567890abcdef",
            "posterior_run_id": "post_1234567890abcdef",
        },
        "source_observations": [
            {
                "source_event_id": "srcevt_1234567890abcdef",
                "source_id": "zuuu_metar_raw_awc",
                "source_class": "hard_observation_candidate",
            }
        ],
        "posterior_target_bucket_selection": {
            "target_bucket": "30",
            "target_probability": 0.74,
        },
        "distribution": [
            {"outcome": "29", "q_temperature_bucket_posterior": 0.20},
            {"outcome": "30", "q_temperature_bucket_posterior": 0.74},
            {"outcome": "31", "q_temperature_bucket_posterior": 0.06},
        ],
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "fact_snapshot_id": "fact_1234567890abcdef",
            "posterior_run_id": "post_1234567890abcdef",
            "target_bucket": "30",
            "target_probability": 0.74,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "phase": "D0",
            "beta_weak": 0.4,
            "beta_strong": 0.8,
            "phase_reason": "unit_test_d0",
            "hard_mask_reason": "none",
        },
    }


def test_daily_review_input_contract_lists_layers_and_no_write_guards() -> None:
    contract = daily_review_input_contract()

    assert contract["schema"] == "daily_weather_review_input_contract_v1"
    assert contract["read_only"] is True
    assert contract["guards"]["writes_archive_db"] is False
    assert contract["guards"]["changes_parameters"] is False
    layers = [row["layer"] for row in contract["required_input_layers"]]
    assert layers == [
        "source",
        "fact",
        "posterior",
        "decision",
        "execution",
        "settlement",
        "audit_attachments",
    ]
    assert "source_acquisition" in contract["first_bad_link_order"]
    assert "daily_calibration_record" in contract["output_records"]
    posterior_layer = next(row for row in contract["required_input_layers"] if row["layer"] == "posterior")
    assert "posterior_ssot_fact" in posterior_layer["required_fields"]
    assert "posterior_ssot_fact_or_distribution" not in posterior_layer["required_fields"]


def test_daily_weather_review_no_bad_link_when_chain_hits() -> None:
    review = build_daily_weather_review(
        run_date="2026-07-03",
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_snapshot=_fact_snapshot(),
        decisions=[{"will_order": 1, "decided_state": "entry"}],
        orders=[{"order_id": "ord_1", "order_status": "submitted"}],
        settlement={"settlement_actual": 30.0},
        source_health={"ok": True},
        timing={"signal_to_order_ms": 2100},
    )

    assert review["read_only"] is True
    assert review["first_bad_link"] == "none"
    assert review["posterior_run_id"] == "post_1234567890abcdef"
    assert review["review_json"]["target_hit"] is True
    lineage = review["review_json"]["posterior_lineage"]
    assert lineage["status"] == "posterior_ssot_fact_present"
    assert lineage["posterior_target_source"] == "posterior_ssot_fact"
    assert lineage["fact_snapshot_id"] == "fact_1234567890abcdef"
    assert lineage["posterior_run_id"] == "post_1234567890abcdef"
    assert lineage["phase"] == "D0"
    assert lineage["issues"] == []
    chain = review["review_json"]["chain_node_trace"]
    assert chain["schema"] == "daily_review_chain_node_trace_v1"
    assert chain["node_order"] == [
        "acquisition",
        "parsing",
        "fact_write",
        "prior",
        "weak",
        "phase_weight",
        "strong",
        "posterior",
        "decision",
        "execution",
        "settlement",
    ]
    assert chain["all_required_nodes_present"] is True
    nodes = {item["node_id"]: item for item in chain["nodes"]}
    assert nodes["acquisition"]["output_refs"]["source_event_ids"] == ["srcevt_1234567890abcdef"]
    assert nodes["posterior"]["output_refs"]["posterior_run_id"] == "post_1234567890abcdef"
    assert nodes["decision"]["terminal_state"] == "ok"
    assert nodes["execution"]["terminal_state"] == "ok"
    assert nodes["settlement"]["terminal_state"] == "ok"
    assert review["review_json"]["calibration_metrics"]["posterior_brier"] is not None
    assert review["review_json"]["calibration_metrics"]["posterior_log_loss"] is not None
    assert review["review_json"]["calibration_metrics"]["posterior_rank_of_actual_bucket"] == 1
    assert review["review_json"]["error_attribution"]["primary_link"] == "none"
    assert review["daily_calibration_record"]["state"] == "no_apply"
    assert review["daily_calibration_record"]["guards"]["changes_parameters"] is False
    assert review["rolling_calibration_summary"]["windows"]["7d"]["record_count"] == 1
    assert review["parameter_change_state"]["state"] == "no_candidates"
    assert review["parameter_change_state"]["guards"]["changes_parameters"] is False
    assert review["calibration_candidates"] == []
    assert review["guards"]["writes_daily_weather_reviews"] is False
    assert review["guards"]["places_orders"] is False


def test_daily_weather_review_prefers_posterior_ssot_fact_target_over_legacy_selection() -> None:
    fact = _fact_snapshot()
    fact["posterior_target_bucket_selection"] = {
        "target_bucket": "31",
        "target_probability": 0.99,
    }

    review = build_daily_weather_review(
        run_date="2026-07-03",
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_snapshot=fact,
        decisions=[],
        orders=[],
        settlement={"settlement_actual": 30.0},
        source_health={"ok": True},
    )

    assert review["first_bad_link"] == "none"
    assert review["review_json"]["target_bucket"] == "30"
    assert review["review_json"]["target_probability"] == 0.74
    assert review["review_json"]["posterior_lineage"]["posterior_target_source"] == "posterior_ssot_fact"


def test_daily_weather_review_blocks_missing_posterior_ssot_fact_without_legacy_target_fallback() -> None:
    fact = _fact_snapshot()
    fact.pop("posterior_ssot_fact", None)
    fact["posterior_target_bucket_selection"] = {
        "target_bucket": "31",
        "target_probability": 0.99,
    }

    review = build_daily_weather_review(
        run_date="2026-07-03",
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_snapshot=fact,
        decisions=[],
        orders=[],
        settlement={"settlement_actual": 30.0},
        source_health={"ok": True},
    )

    assert review["first_bad_link"] == "posterior"
    assert review["first_bad_link_reason"] == "posterior_ssot_fact_missing"
    assert review["review_json"]["target_bucket"] == ""
    assert review["review_json"]["target_probability"] is None
    assert review["review_json"]["top_bucket"] == "30"
    lineage = review["review_json"]["posterior_lineage"]
    assert lineage["ok"] is False
    assert lineage["status"] == "posterior_ssot_fact_missing"
    assert lineage["posterior_target_source"] == "missing_posterior_ssot_fact"
    assert lineage["issues"] == ["posterior_ssot_fact_missing"]
    assert lineage["warnings"] == []
    nodes = {item["node_id"]: item for item in review["review_json"]["chain_node_trace"]["nodes"]}
    assert nodes["posterior"]["terminal_state"] == "blocked"
    assert nodes["posterior"]["terminal_reason"] == "posterior_ssot_fact_missing"


def test_daily_weather_review_posterior_lineage_mismatch_is_posterior_bad_link() -> None:
    fact = _fact_snapshot()
    fact["posterior_ssot_fact"]["fact_snapshot_id"] = "fact_other"

    review = build_daily_weather_review(
        run_date="2026-07-03",
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_snapshot=fact,
        decisions=[],
        orders=[],
        settlement={"settlement_actual": 30.0},
        source_health={"ok": True},
    )

    assert review["first_bad_link"] == "posterior"
    assert review["first_bad_link_reason"] == "posterior_ssot_fact_snapshot_id_mismatch"
    lineage = review["review_json"]["posterior_lineage"]
    assert lineage["ok"] is False
    assert lineage["issues"] == ["posterior_ssot_fact_snapshot_id_mismatch"]
    assert review["bias_correction_candidates"][0]["impact_layer"] == "posterior"
    assert review["bias_correction_candidates"][0]["posterior_run_id"] == "post_1234567890abcdef"


def test_daily_weather_review_missed_target_proposes_calibration_candidate() -> None:
    review = build_daily_weather_review(
        run_date="2026-07-03",
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_snapshot=_fact_snapshot(),
        decisions=[{"will_order": 0, "decided_state": "blocked"}],
        orders=[],
        settlement={"settlement_actual": 32.0},
        source_health={"ok": True},
    )

    assert review["first_bad_link"] == "posterior_calibration"
    assert review["first_bad_link_reason"] == "target_bucket_missed_settlement"
    assert review["review_json"]["target_hit"] is False
    assert len(review["calibration_candidates"]) == 1
    candidate = review["calibration_candidates"][0]
    assert candidate["candidate_type"] == "posterior_calibration_review"
    assert candidate["state"] == "proposed"
    assert candidate["impact_layer"] == "posterior"
    assert candidate["requires_historical_recompute"] is True
    assert candidate["expected_improvement_metric"] == "posterior_brier"
    assert candidate["proposal_json"]["no_auto_apply"] is True
    parameter_state = review["parameter_change_state"]
    assert parameter_state["state"] == "proposed"
    assert parameter_state["requests"][0]["governance_state"]["can_apply"] is False
    assert parameter_state["requests"][0]["guards"]["changes_parameters"] is False
    metrics = review["review_json"]["calibration_metrics"]
    assert metrics["actual_tmax_c"] == 32.0
    assert metrics["settlement_bucket"] == "32°C"
    assert metrics["posterior_rank_of_actual_bucket"] is None
    assert metrics["na_reasons"]["posterior_brier"] == "actual_bucket_not_in_posterior_distribution"
    assert review["review_json"]["error_attribution"]["primary_link"] == "posterior_calibration"


def test_daily_weather_review_execution_bad_link_when_order_missing() -> None:
    review = build_daily_weather_review(
        run_date="2026-07-03",
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_snapshot=_fact_snapshot(),
        decisions=[{"will_order": 1, "decided_state": "entry"}],
        orders=[],
        settlement={"settlement_actual": 30.0},
        source_health={"ok": True},
    )

    assert review["first_bad_link"] == "execution"
    assert review["first_bad_link_reason"] == "will_order_without_submitted_order"
    assert review["review_json"]["decision_summary"]["will_order_count"] == 1
    assert review["review_json"]["execution_summary"]["submitted_order_count"] == 0
    nodes = {item["node_id"]: item for item in review["review_json"]["chain_node_trace"]["nodes"]}
    assert nodes["execution"]["terminal_state"] == "blocked"
    assert nodes["execution"]["terminal_reason"] == "will_order_without_submitted_order"
    assert review["review_json"]["chain_node_trace"]["blocked_node_ids"] == ["execution"]
    assert review["review_json"]["calibration_metrics"]["execution_miss_reason"] == "will_order_without_submitted_order"
    assert review["bias_correction_candidates"][0]["candidate_type"] == "execution_slo_review"
    assert review["bias_correction_candidates"][0]["requires_historical_recompute"] is False
    assert review["parameter_change_state"]["requests"][0]["change_type"] == "execution_slo_policy_review"
    assert review["parameter_change_state"]["requests"][0]["measurement_plan"]["windows_days"] == [7, 30, 90]


def test_daily_weather_review_keeps_secondary_posterior_attribution_after_execution_failure() -> None:
    review = build_daily_weather_review(
        run_date="2026-07-03",
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_snapshot=_fact_snapshot(),
        decisions=[{"will_order": 1, "decided_state": "entry"}],
        orders=[],
        settlement={"settlement_actual": 32.0},
        source_health={"ok": True},
    )

    attribution = review["review_json"]["error_attribution"]
    assert attribution["primary_link"] == "execution"
    assert any(
        item["link"] == "posterior_calibration" and item["primary"] is False
        for item in attribution["components"]
    )


def test_daily_weather_review_source_bad_link_precedes_posterior() -> None:
    fact = _fact_snapshot()
    fact.pop("source_observations")

    review = build_daily_weather_review(
        run_date="2026-07-03",
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_snapshot=fact,
        decisions=[],
        orders=[],
        settlement={"settlement_actual": 32.0},
        source_health={"ok": True},
    )

    assert review["first_bad_link"] == "source_acquisition"
    assert review["first_bad_link_reason"] == "source_lineage_missing"


def test_daily_weather_review_ui_lineage_attachment_failure_is_fact_write_bad_link() -> None:
    review = build_daily_weather_review(
        run_date="2026-07-03",
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_snapshot=_fact_snapshot(),
        decisions=[],
        orders=[],
        settlement={"settlement_actual": 30.0},
        source_health={"ok": True},
        audit_attachments={
            "ui_snapshot_lineage": {
                "schema": "ui_snapshot_lineage_audit_v1",
                "ok": False,
                "read_only": True,
                "blockers": ["ui_snapshot_fact_snapshot_id_mismatch"],
                "warnings": [],
            }
        },
    )

    assert review["first_bad_link"] == "fact_write"
    assert review["first_bad_link_reason"] == "ui_snapshot_lineage_audit_failed"
    summary = review["review_json"]["audit_attachment_summary"]
    assert summary["all_ok"] is False
    assert summary["failed_attachment_count"] == 1
    assert summary["blockers"] == ["ui_snapshot_lineage:ui_snapshot_fact_snapshot_id_mismatch"]
