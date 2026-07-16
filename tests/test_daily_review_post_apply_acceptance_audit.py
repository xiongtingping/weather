import json
import sqlite3
from pathlib import Path

from daily_weather_review_runtime import CHAIN_NODE_ORDER
from scripts import daily_review_post_apply_acceptance_audit as audit


SLUG = "highest-temperature-in-chengdu-on-july-4-2026"
EVENT_DATE = "2026-07-04"


def _chain_node_trace() -> dict:
    return {
        "schema": "daily_review_chain_node_trace_v1",
        "node_order": list(CHAIN_NODE_ORDER),
        "node_count": len(CHAIN_NODE_ORDER),
        "nodes": [
            {
                "schema": "daily_review_chain_node_v1",
                "node_id": node_id,
                "input_refs": {"fixture": "input"},
                "output_refs": {"fixture": "output"},
                "timestamps": {"created_at": "2026-07-05T00:15:00Z"},
                "terminal_state": "ok",
                "terminal_reason": "fixture_available",
                "ok": True,
            }
            for node_id in CHAIN_NODE_ORDER
        ],
        "first_bad_link": "none",
        "first_bad_link_node_id": "",
        "missing_node_ids": [],
        "blocked_node_ids": [],
        "all_required_nodes_present": True,
        "read_only": True,
    }


def _complete_timing() -> dict:
    return {
        "schema": "signal_to_order_timing_v1",
        "signal_id": "sig_accept",
        "source_event_id": "srcevt_accept",
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


def _create_db(tmp_path: Path, *, with_review_tables: bool) -> Path:
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
        VALUES(1, ?, ?, ?, 'chengdu', 'ZUUU', 30.0, 'whole_degrees', 1,
               '2026-07-04T16:00:00Z', '2026-07-05T00:10:00Z')
        """,
        (EVENT_DATE, SLUG, EVENT_DATE),
    )
    timing = _complete_timing()
    posterior_ssot = {
        "schema": "bayes_posterior_ssot_fact_v1",
        "state": "available",
        "fact_snapshot_id": "fact_accept",
        "posterior_run_id": "post_accept",
        "target_bucket": "30°C",
        "target_probability": 0.8,
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "phase": "D0",
        "beta_weak": 0.4,
        "beta_strong": 0.8,
        "phase_reason": "acceptance_fixture",
        "hard_mask_reason": "none",
    }
    fact_payload = {
        "slug": SLUG,
        "event_date": EVENT_DATE,
        "run_date": EVENT_DATE,
        "lineage_state": {
            "fact_snapshot_id": "fact_accept",
            "posterior_run_id": "post_accept",
            "source_event_ids": ["srcevt_accept"],
        },
        "source_observations": [{"source_event_id": "srcevt_accept", "source_id": "zuuu_metar_raw_awc"}],
        "distribution": [
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.8},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.2},
        ],
        "posterior_ssot_fact": posterior_ssot,
        "posterior_target_bucket_selection": {
            "schema": "posterior_target_bucket_selection_v1",
            "ssot_source": "posterior_ssot_fact",
            "target_bucket": "30°C",
            "target_probability": 0.8,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "fact_snapshot_id": "fact_accept",
        },
        "settlement_actual": 30.0,
        "signal_to_order_timing": timing,
    }
    conn.execute(
        """
        INSERT INTO event_fact_history
            (id, run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES(1, ?, ?, ?, '2026-07-04T09:05:38Z', ?, '2026-07-04T09:05:39Z')
        """,
        (EVENT_DATE, SLUG, EVENT_DATE, json.dumps(fact_payload)),
    )
    conn.execute(
        """
        INSERT INTO auto_trade_decisions
            (id, run_id, slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES(1, 7, ?, ?, '30°C', 'YES', 'entry', 1, ?, '2026-07-04T09:05:39Z')
        """,
        (SLUG, EVENT_DATE, json.dumps({"source": "hard_source_direct", "signal_to_order_timing": timing})),
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
                    "include_event_keys": [{"slug": SLUG, "event_date": EVENT_DATE}],
                    "hard_source_direct": {
                        "slug": SLUG,
                        "event_date": EVENT_DATE,
                        "target_bucket": "30°C",
                        "signal_to_order_timing": timing,
                    },
                    "entry_expected_targets": [
                        {
                            "slug": SLUG,
                            "event_date": EVENT_DATE,
                            "outcome": "30°C",
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
            "review_id": "rev_accept",
            "slug": SLUG,
            "event_date": EVENT_DATE,
            "fact_snapshot_id": "fact_accept",
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
                "fact_snapshot_id": "fact_accept",
                "posterior_run_id": "post_accept",
                "posterior_ssot_fact_fact_snapshot_id": "fact_accept",
                "posterior_ssot_fact_posterior_run_id": "post_accept",
                "target_bucket": "30°C",
                "target_probability": 0.8,
                "target_probability_field": "posterior_ssot_fact.target_probability",
                "phase": "D0",
                "beta_weak": 0.4,
                "beta_strong": 0.8,
                "phase_reason": "acceptance_fixture",
                "hard_mask_reason": "none",
                "issues": [],
                "warnings": [],
            },
            "calibration_metrics": {
                "actual_tmax_c": 30.0,
                "settlement_bucket": "30°C",
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
            "chain_node_trace": _chain_node_trace(),
            "daily_calibration_record": {"daily_calibration_record_id": "dcal_accept", "state": "no_apply"},
            "bias_correction_candidates": [
                {
                    "calibration_candidate_id": "cand_accept",
                    "candidate_type": "noop_review_marker",
                    "state": "proposed",
                    "impact_layer": "daily_review",
                    "trigger_sample": {
                        "review_id": "rev_accept",
                        "fact_snapshot_id": "fact_accept",
                        "posterior_run_id": "post_accept",
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
            VALUES('rev_accept', ?, ?, ?, 'fact_accept', 'none', ?, '2026-07-05T00:15:00Z')
            """,
            (EVENT_DATE, SLUG, EVENT_DATE, json.dumps(review_json)),
        )
    conn.commit()
    conn.close()
    return db_path


def _ui_payload(*, fact_snapshot_id: str = "fact_accept", posterior_run_id: str = "post_accept") -> dict:
    posterior_ssot = {
        "schema": "bayes_posterior_ssot_fact_v1",
        "state": "available",
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": posterior_run_id,
        "target_bucket": "30°C",
        "target_probability": 0.8,
        "target_probability_field": "posterior_ssot_fact.target_probability",
    }
    selection = {
        "schema": "posterior_target_bucket_selection_v1",
        "ssot_source": "posterior_ssot_fact",
        "target_bucket": "30°C",
        "target_probability": 0.8,
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "fact_snapshot_id": fact_snapshot_id,
    }
    return {
        "ok": True,
        "snapshot_id": "uisnap_accept",
        "snapshot_etag": 'W/"uisnap_accept"',
        "run_date": EVENT_DATE,
        "snapshot_freshness_state": "fresh",
        "source_fact_ids": [1],
        "fact_snapshot_ids": [fact_snapshot_id],
        "posterior_run_ids": [posterior_run_id],
        "lineage_state": {
            "schema": "ui_snapshot_lineage_state_v1",
            "placeholder": False,
            "fact_lineage_complete": True,
            "complete": True,
            "source_fact_ids": [1],
            "fact_snapshot_ids": [fact_snapshot_id],
            "posterior_run_ids": [posterior_run_id],
            "source_event_ids": ["srcevt_accept"],
            "items": [
                {
                    "slug": SLUG,
                    "event_date": EVENT_DATE,
                    "fact_id": 1,
                    "fact_snapshot_id": fact_snapshot_id,
                    "posterior_run_id": posterior_run_id,
                    "source_event_ids": ["srcevt_accept"],
                    "signal_id": "sig_accept",
                }
            ],
        },
        "facts": {
            "items": [
                {
                    "slug": SLUG,
                    "event_date": EVENT_DATE,
                    "fact_id": 1,
                    "fact_snapshot_id": fact_snapshot_id,
                    "posterior_run_id": posterior_run_id,
                    "posterior_ssot_fact": posterior_ssot,
                    "posterior_target_bucket_selection": selection,
                    "target_bucket_selection": dict(selection),
                }
            ]
        },
    }


def test_post_apply_acceptance_blocks_missing_review_table(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path, with_review_tables=False)

    result = audit.build_report(
        db_path,
        since_date="2026-07-01",
        include_ui_snapshot_audit=False,
    )

    assert result["ok"] is False
    assert result["read_only"] is True
    assert result["daily_weather_reviews_row_count"] is None
    assert "daily_weather_reviews_missing_planned_table" in result["blockers"]
    assert result["coverage"]["item_blocker_counts"]["daily_weather_reviews_missing_planned_table"] == 1
    assert result["item_blocker_samples"][0]["slug"] == SLUG
    assert result["item_blocker_samples"][0]["event_date"] == EVENT_DATE
    assert "daily_weather_reviews_missing_planned_table" in result["item_blocker_samples"][0]["blockers"]
    assert result["item_blocker_samples"][0]["latest_fact"]["fact_snapshot_id"] == "fact_accept"
    assert result["gates"][0]["gate"] == "daily_review_coverage_gate"
    assert result["gates"][0]["ok"] is False
    assert result["guards"]["writes_archive_db"] is False


def test_post_apply_acceptance_verifies_review_fact_posterior_and_ui_lineage(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path, with_review_tables=True)
    payload_path = tmp_path / "ui_payload.json"
    payload_path.write_text(json.dumps(_ui_payload()), encoding="utf-8")

    result = audit.build_report(
        db_path,
        since_date="2026-07-01",
        ui_payload_json=payload_path,
        require_ui_visible=True,
    )

    assert result["ok"] is True
    assert result["daily_weather_reviews_row_count"] == 1
    assert result["coverage"]["settled_event_total_count"] == 1
    assert result["coverage"]["settled_event_scanned_count"] == 1
    assert result["coverage"]["coverage_scope_complete"] is True
    assert result["coverage"]["coverage_scope_truncated_count"] == 0
    assert result["coverage"]["coverage_ratio"] == 1.0
    assert {gate["gate"]: gate["ok"] for gate in result["gates"]} == {
        "daily_review_coverage_gate": True,
        "review_to_ssot_replay_gate": True,
        "single_fact_snapshot_lineage_gate": True,
        "ui_snapshot_lineage_gate": True,
    }
    lineage_gate = next(gate for gate in result["gates"] if gate["gate"] == "single_fact_snapshot_lineage_gate")
    assert lineage_gate["summary"]["items"][0]["fact_snapshot_id"] == "fact_accept"
    assert lineage_gate["summary"]["items"][0]["posterior_run_id"] == "post_accept"
    ui_gate = next(gate for gate in result["gates"] if gate["gate"] == "ui_snapshot_lineage_gate")
    assert ui_gate["summary"]["matched_review_count"] == 1
    assert ui_gate["summary"]["join_results"][0]["ok"] is True


def test_post_apply_acceptance_blocks_when_coverage_limit_truncates_scope(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path, with_review_tables=True)
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
        include_ui_snapshot_audit=False,
    )

    assert result["ok"] is False
    assert "daily_review_coverage_scope_truncated" in result["blockers"]
    assert result["coverage"]["requested_limit"] == 1
    assert result["coverage"]["settled_event_total_count"] == 2
    assert result["coverage"]["settled_event_scanned_count"] == 1
    assert result["coverage"]["coverage_scope_complete"] is False
    coverage_gate = next(gate for gate in result["gates"] if gate["gate"] == "daily_review_coverage_gate")
    assert coverage_gate["ok"] is False
    assert coverage_gate["summary"]["settled_event_total_count"] == 2
    assert coverage_gate["summary"]["coverage_scope_truncated_count"] == 1


def test_post_apply_acceptance_uses_lineage_manifest_effective_scope(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path, with_review_tables=True)
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
        include_ui_snapshot_audit=False,
        lineage_decision_manifest=manifest,
    )

    assert result["ok"] is True
    assert result["coverage"]["raw_settled_event_total_count"] == 2
    assert result["coverage"]["settled_event_total_count"] == 1
    assert result["coverage"]["excluded_settled_event_count"] == 1
    assert result["coverage"]["reviewed_event_count"] == 1
    assert result["coverage"]["missing_review_count"] == 0
    assert result["coverage"]["lineage_decision_manifest_audit"]["manifest_provided"] is True
    coverage_gate = next(gate for gate in result["gates"] if gate["gate"] == "daily_review_coverage_gate")
    assert coverage_gate["ok"] is True
    assert coverage_gate["summary"]["raw_settled_event_total_count"] == 2
    assert coverage_gate["summary"]["settled_event_total_count"] == 1
    assert coverage_gate["summary"]["excluded_settled_event_count"] == 1
    assert "daily_review_missing" not in result["blockers"]


def test_post_apply_acceptance_blocks_ui_review_lineage_mismatch(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path, with_review_tables=True)
    payload_path = tmp_path / "ui_payload.json"
    payload_path.write_text(json.dumps(_ui_payload(fact_snapshot_id="fact_ui_other")), encoding="utf-8")

    result = audit.build_report(
        db_path,
        since_date="2026-07-01",
        ui_payload_json=payload_path,
        require_ui_visible=True,
    )

    assert result["ok"] is False
    assert "ui_snapshot_fact_snapshot_id_mismatch" in result["blockers"]
    assert "ui_snapshot_review_fact_snapshot_id_mismatch" in result["blockers"]


def test_review_replay_gate_does_not_require_signal_source_when_fact_anchor_is_available() -> None:
    gate = audit._review_replay_gate(
        [
            {
                "review_id": "rev_no_signal",
                "review_replay": {
                    "ok": True,
                    "source_counts": {},
                    "anchor_state": {
                        "fact_anchor_available": True,
                        "hard_source_signal_expected": False,
                    },
                    "warnings": ["anchor_has_no_awc_signal_expected"],
                    "blockers": [],
                },
            }
        ]
    )

    assert gate["ok"] is True
    assert gate["blockers"] == []
    assert gate["warnings"] == ["rev_no_signal:anchor_has_no_awc_signal_expected"]
