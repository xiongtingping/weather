import json
import sqlite3
from pathlib import Path

from daily_weather_review_runtime import CHAIN_NODE_ORDER
from scripts import daily_review_coverage_audit as audit


def _chain_node_trace_fixture() -> dict:
    nodes = [
        {
            "schema": "daily_review_chain_node_v1",
            "node_id": node_id,
            "input_refs": {"fixture": "input"},
            "output_refs": {"fixture": "output"},
            "timestamps": {"created_at": "2026-07-04T09:05:40Z"},
            "timestamp_status": "available",
            "terminal_state": "ok",
            "terminal_reason": "fixture_available",
            "ok": True,
        }
        for node_id in CHAIN_NODE_ORDER
    ]
    return {
        "schema": "daily_review_chain_node_trace_v1",
        "node_order": list(CHAIN_NODE_ORDER),
        "node_count": len(nodes),
        "nodes": nodes,
        "first_bad_link": "none",
        "first_bad_link_node_id": "",
        "missing_node_ids": [],
        "blocked_node_ids": [],
        "all_required_nodes_present": True,
        "read_only": True,
    }


def _create_base_db(tmp_path: Path, *, with_review_tables: bool) -> Path:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            city TEXT,
            station_id TEXT,
            settlement_actual REAL,
            rounding_rule TEXT,
            market_closed INTEGER,
            event_end_utc TEXT,
            created_at TEXT
        );
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            fact_json TEXT,
            created_at TEXT
        );
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY,
            run_id INTEGER,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            will_order INTEGER,
            details_json TEXT,
            created_at TEXT
        );
        CREATE TABLE auto_trade_jobs (
            id INTEGER PRIMARY KEY,
            run_id INTEGER,
            source TEXT,
            state TEXT,
            payload_json TEXT,
            created_at TEXT,
            started_at TEXT,
            ended_at TEXT
        );
        """
    )
    if with_review_tables:
        conn.executescript(
            """
            CREATE TABLE daily_weather_reviews (
                review_id TEXT PRIMARY KEY,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                fact_snapshot_id TEXT,
                first_bad_link TEXT,
                review_json TEXT,
                created_at TEXT
            );
            CREATE TABLE bias_correction_candidates (
                calibration_candidate_id TEXT PRIMARY KEY,
                review_id TEXT,
                candidate_type TEXT,
                state TEXT,
                proposal_json TEXT,
                rollback_plan_json TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE daily_calibration_records (
                daily_calibration_record_id TEXT PRIMARY KEY,
                review_id TEXT,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                fact_snapshot_id TEXT,
                state TEXT,
                calibration_metrics_json TEXT,
                error_attribution_json TEXT,
                strata_json TEXT,
                record_json TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE parameter_change_events (
                id INTEGER PRIMARY KEY,
                calibration_candidate_id TEXT,
                state TEXT,
                changed_by TEXT,
                change_json TEXT,
                created_at TEXT
            );
            """
        )
    conn.execute(
        """
        INSERT INTO event_runs
            (id, run_date, slug, event_date, city, station_id, settlement_actual,
             rounding_rule, market_closed, event_end_utc, created_at)
        VALUES(1, '2026-07-04', ?, '2026-07-04', 'chengdu', 'ZUUU', 30.0,
               'whole_degrees', 1, '2026-07-04T16:00:00Z', '2026-07-05T00:10:00Z')
        """,
        ("highest-temperature-in-chengdu-on-july-4-2026",),
    )
    timing = {
        "schema": "signal_to_order_timing_v1",
        "signal_id": "sig_review_cov",
        "source_event_id": "srcevt_review_cov",
        "awc_hard_source_signal_at": "2026-07-04T09:05:38Z",
        "pre_signal_prepare_state": "prepared_before_deadline",
        "pre_signal_prepare_reason": "market_prefetch_refreshed",
        "pre_signal_prepare_complete": True,
        "pre_signal_prepare_missing_components": [],
        "pre_signal_prepare_required_components": [
            "market_prefetch",
            "market_mapping",
            "orderbook",
            "open_orders",
            "positions",
            "capital_caps",
        ],
        "pre_signal_prepare_components": {
            "market_prefetch": {"ok": True, "state": "prepared"},
            "market_mapping": {"ok": True, "state": "prepared"},
            "orderbook": {"ok": True, "state": "prepared"},
            "open_orders": {"ok": True, "state": "prepared"},
            "positions": {"ok": True, "state": "prepared"},
            "capital_caps": {"ok": True, "state": "prepared"},
        },
        "pre_signal_prepared_before_deadline": True,
        "terminal_state": "submitted",
        "terminal_reason": "order_submitted",
        "orderbook_checked_at": "2026-07-04T09:05:38.200Z",
        "risk_checked_at": "2026-07-04T09:05:38.500Z",
        "idempotency_checked_at": "2026-07-04T09:05:38.700Z",
        "submit_requested_at": "2026-07-04T09:05:39.000Z",
        "exchange_ack_at": "2026-07-04T09:05:40Z",
        "order_submitted_at": "2026-07-04T09:05:40Z",
        "signal_to_order_slo_met": True,
    }
    fact_payload = {
        "slug": "highest-temperature-in-chengdu-on-july-4-2026",
        "event_date": "2026-07-04",
        "run_date": "2026-07-04",
        "lineage_state": {
            "fact_snapshot_id": "fact_review_cov",
            "posterior_run_id": "post_review_cov",
        },
        "source_observations": [{"source_event_id": "srcevt_review_cov", "source_id": "zuuu_metar_raw_awc"}],
        "distribution": [
            {"outcome": "30", "q_temperature_bucket_posterior": 0.8},
            {"outcome": "31", "q_temperature_bucket_posterior": 0.2},
        ],
        "posterior_target_bucket_selection": {
            "target_bucket": "30",
            "target_probability": 0.8,
        },
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "fact_snapshot_id": "fact_review_cov",
            "posterior_run_id": "post_review_cov",
            "target_bucket": "30",
            "target_probability": 0.8,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "phase": "D0",
            "beta_weak": 0.4,
            "beta_strong": 0.8,
            "phase_reason": "coverage_fixture",
            "hard_mask_reason": "none",
        },
        "settlement_actual": 30.0,
        "signal_to_order_timing": timing,
    }
    conn.execute(
        """
        INSERT INTO event_fact_history
            (id, run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES(1, '2026-07-04', ?, '2026-07-04', '2026-07-04T09:05:38Z', ?, '2026-07-04T09:05:39Z')
        """,
        ("highest-temperature-in-chengdu-on-july-4-2026", json.dumps(fact_payload)),
    )
    conn.execute(
        """
        INSERT INTO auto_trade_decisions
            (id, run_id, slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES(1, 7, ?, '2026-07-04', '30', 'YES', 'entry', 1, ?, '2026-07-04T09:05:39Z')
        """,
        (
            "highest-temperature-in-chengdu-on-july-4-2026",
            json.dumps({"source": "hard_source_direct", "signal_to_order_timing": timing}),
        ),
    )
    conn.execute(
        """
        INSERT INTO auto_trade_jobs
            (id, run_id, source, state, payload_json, created_at, started_at, ended_at)
        VALUES(1, 7, 'hard_source_direct', 'success', ?, '2026-07-04T09:05:39Z',
               '2026-07-04T09:05:39Z', '2026-07-04T09:05:40Z')
        """,
        (
            json.dumps(
                {
                    "include_event_keys": [
                        {
                            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
                            "event_date": "2026-07-04",
                        }
                    ],
                    "hard_source_direct": {
                        "slug": "highest-temperature-in-chengdu-on-july-4-2026",
                        "event_date": "2026-07-04",
                        "target_bucket": "30",
                        "signal_to_order_timing": timing,
                    },
                    "entry_expected_targets": [
                        {
                            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
                            "event_date": "2026-07-04",
                            "outcome": "30",
                            "side": "YES",
                            "signal_to_order_timing": timing,
                        }
                    ],
                }
            ),
        ),
    )
    if with_review_tables:
        review_json = {
            "schema": "daily_weather_review_json_v1",
            "slug": "highest-temperature-in-chengdu-on-july-4-2026",
            "event_date": "2026-07-04",
            "fact_snapshot_id": "fact_review_cov",
            "first_bad_link": "none",
            "first_bad_link_reason": "chain_ok",
            "settlement_actual": 30.0,
            "target_hit": True,
            "decision_summary": {"decision_count": 1, "will_order_count": 1},
            "execution_summary": {"order_count": 1, "submitted_order_count": 1},
            "posterior_row_count": 2,
            "posterior_lineage": {
                "schema": "daily_review_posterior_lineage_v1",
                "ok": True,
                "status": "posterior_ssot_fact_present",
                "posterior_target_source": "posterior_ssot_fact",
                "posterior_ssot_fact_present": True,
                "posterior_ssot_fact_source": "fact_snapshot",
                "fact_snapshot_id": "fact_review_cov",
                "posterior_run_id": "post_review_cov",
                "posterior_ssot_fact_fact_snapshot_id": "fact_review_cov",
                "posterior_ssot_fact_posterior_run_id": "post_review_cov",
                "target_bucket": "30",
                "target_probability": 0.8,
                "target_probability_field": "posterior_ssot_fact.target_probability",
                "phase": "D0",
                "beta_weak": 0.4,
                "beta_strong": 0.8,
                "phase_reason": "coverage_fixture",
                "hard_mask_reason": "none",
                "issues": [],
                "warnings": [],
            },
            "calibration_metrics": {
                "actual_tmax_c": 30.0,
                "settlement_bucket": "30",
                "target_hit": True,
                "posterior_actual_bucket_probability": 0.8,
                "posterior_brier": 0.0,
                "posterior_log_loss": 0.22314355131420976,
                "calibration_bin": "0.8-0.9",
                "posterior_rank_of_actual_bucket": 1,
                "hard_constraint_violation_count": 0,
                "D1_to_D0_probability_delta": 0.1,
                "source_latency_ms_by_source": {"zuuu_metar_raw_awc": 120.0},
                "decision_miss_reason": "",
                "execution_miss_reason": "",
            },
            "error_attribution": {
                "primary_link": "none",
                "components": [{"link": "none", "reason": "chain_ok", "primary": True}],
            },
            "chain_node_trace": _chain_node_trace_fixture(),
            "daily_calibration_record": {
                "daily_calibration_record_id": "dcal_review_cov",
                "state": "no_apply",
            },
            "bias_correction_candidates": [
                {
                    "calibration_candidate_id": "cand_review_cov",
                    "candidate_type": "noop_review_marker",
                    "state": "proposed",
                    "impact_layer": "daily_review",
                    "trigger_sample": {
                        "review_id": "rev_review_cov",
                        "fact_snapshot_id": "fact_review_cov",
                        "posterior_run_id": "post_review_cov",
                    },
                    "expected_improvement_metric": "source_lineage_completeness",
                    "possible_side_effects": [
                        "single_day_candidate_may_overfit",
                        "requires_review_before_parameter_change",
                    ],
                    "requires_historical_recompute": False,
                }
            ],
        }
        conn.execute(
            """
            INSERT INTO daily_weather_reviews
                (review_id, run_date, slug, event_date, fact_snapshot_id, first_bad_link, review_json, created_at)
            VALUES('rev_review_cov', '2026-07-04', ?, '2026-07-04', 'fact_review_cov',
                   'none', ?, '2026-07-05T00:15:00Z')
            """,
            ("highest-temperature-in-chengdu-on-july-4-2026", json.dumps(review_json)),
        )
    conn.commit()
    conn.close()
    return db_path


def test_daily_review_coverage_audit_reports_planned_table_missing(tmp_path: Path) -> None:
    db_path = _create_base_db(tmp_path, with_review_tables=False)

    result = audit.build_report(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )

    assert result["ok"] is False
    assert result["read_only"] is True
    assert result["settled_event_count"] == 1
    assert result["reviewed_event_count"] == 0
    assert result["missing_review_count"] == 1
    assert "daily_weather_reviews_missing_planned_table" in result["blockers"]
    assert result["guards"]["writes_archive_db"] is False
    assert result["daily_review_persistence_schema_status"]["tables"]["daily_weather_reviews"]["exists"] is False
    matrix = result["review_field_capability_matrix"]
    assert matrix["schema"] == "daily_review_field_capability_matrix_v1"
    assert matrix["settled_event_count"] == 1
    assert matrix["fields"]["first_bad_link"]["status"] == "blocked"
    assert matrix["fields"]["first_bad_link"]["blocked_count"] == 1
    assert matrix["fields"]["posterior_brier"]["reason_counts"] == {
        "daily_weather_reviews_missing_planned_table": 1
    }
    assert result["items"][0]["review_field_capability"]["fields"]["first_bad_link"]["status"] == "blocked"


def test_daily_review_coverage_audit_verifies_schema_present_review_chain(tmp_path: Path) -> None:
    db_path = _create_base_db(tmp_path, with_review_tables=True)

    result = audit.build_report(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )

    assert result["ok"] is True
    assert result["requested_limit"] == 100
    assert result["settled_event_total_count"] == 1
    assert result["settled_event_count"] == 1
    assert result["settled_event_scanned_count"] == 1
    assert result["coverage_scope_complete"] is True
    assert result["coverage_scope_truncated_count"] == 0
    assert result["reviewed_event_count"] == 1
    assert result["missing_review_count"] == 0
    assert result["coverage_ratio"] == 1.0
    assert result["chain_ok_count"] == 1
    assert result["chain_blocked_count"] == 0
    assert result["daily_review_persistence_schema_status"]["ok"] is True
    item = result["items"][0]
    assert item["ok"] is True
    assert item["review_id"] == "rev_review_cov"
    assert item["review_fact_snapshot_matches_latest"] is True
    assert item["review_posterior_run_id"] == "post_review_cov"
    assert item["latest_fact_posterior_run_id"] == "post_review_cov"
    assert item["review_posterior_run_matches_latest"] is True
    assert item["posterior_lineage"]["posterior_target_source"] == "posterior_ssot_fact"
    assert item["first_bad_link"] == "none"
    assert item["review_replay"]["ok"] is True
    assert item["review_replay"]["source_counts"] == {
        "event_fact_history": 1,
        "auto_trade_decisions": 1,
        "auto_trade_jobs": 1,
    }
    matrix = result["review_field_capability_matrix"]
    assert matrix["fields"]["first_bad_link"]["status"] == "available"
    assert matrix["fields"]["posterior_brier"]["available_count"] == 1
    assert matrix["fields"]["posterior_log_loss"]["available_count"] == 1
    assert matrix["fields"]["decision_miss_reason"]["available_count"] == 1
    assert matrix["fields"]["execution_miss_reason"]["available_count"] == 1
    assert matrix["fields"]["chain_node_trace"]["available_count"] == 1
    assert matrix["fields"]["chain_node_trace_nodes"]["available_count"] == 1
    assert matrix["fields"]["bias_candidate_state"]["available_count"] == 1
    assert matrix["fields"]["bias_candidate_trigger_sample"]["available_count"] == 1
    assert matrix["fields"]["bias_candidate_impact_layer"]["available_count"] == 1
    assert matrix["fields"]["bias_candidate_expected_improvement_metric"]["available_count"] == 1
    assert matrix["fields"]["bias_candidate_possible_side_effects"]["available_count"] == 1
    assert matrix["fields"]["bias_candidate_requires_historical_recompute"]["available_count"] == 1
    assert matrix["missing_or_blocked_field_count"] == 0
    item_capability = item["review_field_capability"]
    assert item_capability["category_counts"]["chain_node_trace"]["available"] == 3
    assert item_capability["category_counts"]["bias_correction_candidates"]["available"] == 7
    assert item_capability["category_counts"]["calibration_metrics"]["available"] >= 10
    assert item_capability["fields"]["D1_to_D0_probability_delta"]["status"] == "available"


def test_daily_review_coverage_audit_historical_settled_date_regression_passes(
    tmp_path: Path,
) -> None:
    db_path = _create_base_db(tmp_path, with_review_tables=True)

    result = audit.build_report(
        db_path,
        event_date="2026-07-04",
        limit=1,
        max_items_output=5,
        require_historical_regression=True,
    )

    assert result["ok"] is True
    regression = result["historical_settled_date_regression"]
    assert regression["schema"] == "historical_settled_date_regression_v1"
    assert regression["enabled"] is True
    assert regression["ok"] is True
    assert regression["event_date"] == "2026-07-04"
    assert regression["raw_settled_event_total_count"] == 1
    assert regression["settled_event_count"] == 1
    assert regression["coverage_scope_complete"] is True
    assert regression["reviewed_event_count"] == 1
    assert regression["missing_review_count"] == 0
    assert regression["chain_blocked_count"] == 0
    assert regression["missing_required_fields"] == []
    assert regression["guards"]["writes_archive_db"] is False
    assert regression["guards"]["historical_backfill"] is False


def test_daily_review_coverage_audit_historical_regression_requires_explicit_anchor(
    tmp_path: Path,
) -> None:
    db_path = _create_base_db(tmp_path, with_review_tables=True)

    result = audit.build_report(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        require_historical_regression=True,
    )

    assert result["ok"] is False
    regression = result["historical_settled_date_regression"]
    assert regression["enabled"] is True
    assert regression["ok"] is False
    assert "historical_regression_requires_slug_or_event_date" in regression["blockers"]
    assert "historical_regression_requires_slug_or_event_date" in result["blockers"]


def test_daily_review_coverage_audit_blocks_incomplete_bias_candidate_contract(tmp_path: Path) -> None:
    db_path = _create_base_db(tmp_path, with_review_tables=True)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT review_json FROM daily_weather_reviews WHERE review_id = 'rev_review_cov'").fetchone()
        review_json = json.loads(row[0])
        candidate = review_json["bias_correction_candidates"][0]
        candidate.pop("trigger_sample", None)
        candidate.pop("expected_improvement_metric", None)
        conn.execute(
            "UPDATE daily_weather_reviews SET review_json = ? WHERE review_id = 'rev_review_cov'",
            (json.dumps(review_json),),
        )
        conn.commit()

    result = audit.build_report(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )

    assert result["ok"] is False
    assert "bias_candidate_trigger_sample_missing" in result["blockers"]
    assert "bias_candidate_expected_improvement_metric_missing" in result["blockers"]
    matrix = result["review_field_capability_matrix"]
    assert matrix["fields"]["bias_candidate_trigger_sample"]["status"] == "partial"
    assert matrix["fields"]["bias_candidate_expected_improvement_metric"]["status"] == "partial"


def test_daily_review_coverage_audit_blocks_when_limit_truncates_settled_scope(
    tmp_path: Path,
) -> None:
    db_path = _create_base_db(tmp_path, with_review_tables=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO event_runs
                (id, run_date, slug, event_date, city, station_id, settlement_actual,
                 rounding_rule, market_closed, event_end_utc, created_at)
            VALUES(2, '2026-07-03', 'highest-temperature-in-chengdu-on-july-3-2026',
                   '2026-07-03', 'chengdu', 'ZUUU', 31.0, 'whole_degrees', 1,
                   '2026-07-03T16:00:00Z', '2026-07-04T00:10:00Z')
            """
        )
        conn.commit()

    result = audit.build_report(
        db_path,
        since_date="2026-07-01",
        limit=1,
        max_items_output=5,
    )

    assert result["ok"] is False
    assert result["requested_limit"] == 1
    assert result["settled_event_total_count"] == 2
    assert result["settled_event_count"] == 1
    assert result["settled_event_scanned_count"] == 1
    assert result["coverage_scope_complete"] is False
    assert result["coverage_scope_truncated_count"] == 1
    assert "daily_review_coverage_scope_truncated" in result["blockers"]
    assert "daily_review_coverage_limit_below_settled_scope" in result["warnings"]
    assert "fact_snapshot_not_found" not in result["blockers"]


def test_daily_review_coverage_audit_excludes_manifest_sample_from_effective_scope(
    tmp_path: Path,
) -> None:
    db_path = _create_base_db(tmp_path, with_review_tables=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO event_runs
                (id, run_date, slug, event_date, city, station_id, settlement_actual,
                 rounding_rule, market_closed, event_end_utc, created_at)
            VALUES(2, '2026-07-03', 'highest-temperature-in-chengdu-on-july-3-2026',
                   '2026-07-03', 'chengdu', 'ZUUU', 31.0, 'whole_degrees', 1,
                   '2026-07-03T16:00:00Z', '2026-07-04T00:10:00Z')
            """
        )
        conn.commit()
    manifest = {
        "schema": "daily_review_lineage_repair_decision_manifest_v1",
        "decisions": [
            {
                "repair_correlation_id": "missing_fact_snapshot:2",
                "decision": "exclude_sample",
                "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                "event_date": "2026-07-03",
                "event_run_id": 2,
                "repair_type": "missing_fact_snapshot_for_settled_event",
                "reason": "legacy sample excluded from current controlled apply scope",
            }
        ],
    }

    result = audit.build_report(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        lineage_decision_manifest=manifest,
    )

    assert result["ok"] is True
    assert result["raw_settled_event_total_count"] == 2
    assert result["settled_event_total_count"] == 1
    assert result["settled_event_count"] == 1
    assert result["excluded_settled_event_count"] == 1
    assert result["reviewed_event_count"] == 1
    assert result["missing_review_count"] == 0
    assert result["coverage_ratio"] == 1.0
    assert result["lineage_decision_manifest_audit"]["manifest_provided"] is True
    assert result["lineage_decision_manifest_audit"]["excluded_event_run_ids"] == [2]
    assert result["excluded_samples"][0]["filter_reason"] == "operator_manifest_exclude_sample"
    assert result["items"][0]["slug"] == "highest-temperature-in-chengdu-on-july-4-2026"
    assert "daily_review_missing" not in result["blockers"]


def test_daily_review_coverage_treats_no_hard_source_signal_as_replay_na_when_fact_anchored(
    tmp_path: Path,
) -> None:
    db_path = _create_base_db(tmp_path, with_review_tables=True)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT fact_json FROM event_fact_history WHERE id = 1").fetchone()
        payload = json.loads(row[0])
        payload.pop("signal_to_order_timing", None)
        payload["source_observations"] = []
        conn.execute(
            "UPDATE event_fact_history SET fact_json = ? WHERE id = 1",
            (json.dumps(payload),),
        )
        conn.execute("DELETE FROM auto_trade_decisions")
        conn.execute("DELETE FROM auto_trade_jobs")
        conn.commit()

    result = audit.build_report(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )

    assert result["ok"] is True
    item = result["items"][0]
    assert item["ok"] is True
    assert item["review_replay"]["ok"] is True
    assert item["review_replay"]["source_counts"] == {}
    assert item["review_replay"]["anchor_state"]["fact_anchor_available"] is True
    assert item["review_replay"]["anchor_state"]["hard_source_signal_expected"] is False
    assert item["review_replay"]["warnings"] == ["anchor_has_no_awc_signal_expected"]
    assert "review_replay_fact_missing" not in result["blockers"]
    assert "review_replay_fact_missing" not in item["blockers"]
