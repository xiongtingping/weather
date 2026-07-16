import json
import sqlite3

from scripts import ssot_chain_replay_audit as audit
from signal_to_order_ledger_writer import SIGNAL_TO_ORDER_TIMING_COLUMNS


def test_ssot_chain_replay_input_contract_declares_fork_assertions_and_3s_slo():
    contract = audit.ssot_chain_replay_input_contract()

    assert contract["schema"] == "ssot_chain_replay_input_contract_v1"
    assert contract["read_only"] is True
    assert contract["guards"]["writes_archive_db"] is False
    assert "fact_snapshot_id" in contract["input_keys"]
    assert "source_observation_id" in contract["input_keys"]
    assert "auto_trade_decisions" in contract["output_sections"]
    assert "fork_assertions" in contract["output_sections"]
    assert "production_coverage" in contract["output_sections"]
    assert "blocker_source_is_auto_trade_decisions" in contract["fork_assertions"]
    assert "accepted_signal_has_final_terminal_state" in contract["fork_assertions"]
    assert "submitted_signal_has_pre_submit_orderbook_risk_idempotency_checks" in contract["fork_assertions"]
    assert "consumer_lineage_references_same_fact_snapshot_id_and_posterior_run_id" in contract["fork_assertions"]
    assert "lineage_consistency" in contract["output_sections"]
    assert contract["awc_signal_to_order_slo"]["start_field"] == "awc_hard_source_signal_at"
    assert contract["awc_signal_to_order_slo"]["end_field"] == "order_submitted_at"
    assert contract["awc_signal_to_order_slo"]["slo_ms"] == 3000
    assert "submitted" in contract["awc_signal_to_order_slo"]["terminal_states"]
    assert contract["amsc_source_to_posterior_surface"]["field"] == "source_to_posterior_timing"
    assert (
        contract["amsc_source_to_posterior_surface"]["applicability"]
        == "source_to_posterior_freshness_only_not_order_signal"
    )
    assert (
        "source_fact_to_posterior_apply_start_ms"
        in contract["amsc_source_to_posterior_surface"]["required_timing_keys"]
    )
    required_timing_keys = set(contract["awc_signal_to_order_slo"]["required_timing_keys"])
    assert {
        "orderbook_checked_at",
        "risk_checked_at",
        "idempotency_checked_at",
        "submit_requested_at",
        "exchange_ack_at",
    } <= required_timing_keys
    assert contract["missing_policy"]["submitted_missing_orderbook_check"] == "blocker"
    assert contract["missing_policy"]["submitted_precheck_after_submit_request"] == "blocker"
    assert contract["missing_policy"]["signal_to_order_timing_table_missing"] == "coverage_blocker"


def _create_schema(conn: sqlite3.Connection) -> None:
    timing_columns = []
    for column in SIGNAL_TO_ORDER_TIMING_COLUMNS:
        suffix = " PRIMARY KEY" if column == "signal_id" else ""
        timing_columns.append(f"{column} TEXT{suffix}")
    conn.executescript(
        f"""
        CREATE TABLE signal_to_order_timing ({', '.join(timing_columns)});
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            fact_json TEXT,
            created_at TEXT
        );
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            source TEXT,
            state TEXT,
            payload_json TEXT,
            created_at TEXT,
            started_at TEXT,
            ended_at TEXT
        );
        CREATE TABLE auto_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            correlation_id TEXT,
            decision_id TEXT,
            signal_id TEXT,
            order_intent_id TEXT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            created_at TEXT
        );
        """
    )


def _complete_timing() -> dict:
    return {
        "schema": "signal_to_order_timing_v1",
        "hard_source_runtime_contract": {
            "schema": "hard_source_runtime_contract_v1",
            "contract_version": audit._current_hard_source_runtime_contract_version(),
        },
        "hard_source_runtime_contract_version": audit._current_hard_source_runtime_contract_version(),
        "signal_id": "sig_complete",
        "source_event_id": "src_awc_complete",
        "source_observation_id": "srcobs_awc_complete",
        "fact_snapshot_id": "fact_complete",
        "posterior_run_id": "post_complete",
        "awc_valid_time": "2026-07-03T09:00:00Z",
        "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
        "preparation_started_at": "2026-07-03T09:00:00Z",
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
        "pre_signal_prepared_at": "2026-07-03T09:04:50Z",
        "awc_pre_signal_deadline_at": "2026-07-03T09:05:00Z",
        "pre_signal_prepared_before_deadline": True,
        "fact_written_at": "2026-07-03T09:05:38.100Z",
        "posterior_written_at": "2026-07-03T09:05:38.200Z",
        "decision_written_at": "2026-07-03T09:05:38.250Z",
        "job_enqueued_at": "2026-07-03T09:05:38.260Z",
        "worker_started_at": "2026-07-03T09:05:38.280Z",
        "terminal_state": "submitted",
        "terminal_reason": "order_submitted",
        "orderbook_checked_at": "2026-07-03T09:05:38.300Z",
        "orderbook_check_source": "unit_test_orderbook",
        "orderbook_check_result": "pass",
        "risk_checked_at": "2026-07-03T09:05:38.900Z",
        "risk_check_source": "unit_test_risk",
        "risk_check_result": "pass",
        "idempotency_checked_at": "2026-07-03T09:05:39.100Z",
        "idempotency_key": "clobsvc:test-complete",
        "submit_requested_at": "2026-07-03T09:05:39.100Z",
        "exchange_ack_at": "2026-07-03T09:05:39.700Z",
        "order_ack_at": "2026-07-03T09:05:39.700Z",
        "order_ack_status": "submitted",
        "order_submitted_at": "2026-07-03T09:05:40Z",
        "terminal_at": "2026-07-03T09:05:40Z",
        "signal_to_order_slo_met": True,
    }


def _insert_decision_with_timing(
    conn: sqlite3.Connection,
    timing: dict,
    *,
    slug: str = "highest-temperature-in-chengdu-on-july-3-2026",
    event_date: str = "2026-07-03",
    outcome: str = "32°C",
    side: str = "YES",
    action: str = "entry",
    will_order: int = 1,
) -> int:
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slug,
            event_date,
            outcome,
            side,
            action,
            will_order,
            json.dumps(
                {
                    "source": "hard_source_direct",
                    "hard_source_direct": True,
                    "signal_to_order_timing": timing,
                }
            ),
            timing.get("decision_written_at")
            or timing.get("awc_hard_source_signal_at")
            or "2026-07-03T09:05:38Z",
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0] or 0)


def test_ssot_chain_replay_accepts_complete_signal(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    conn.execute(
        """
        INSERT INTO signal_to_order_timing(
            signal_id, awc_hard_source_signal_at, order_submitted_at,
            signal_to_order_slo_violation_reason, timing_json, created_at
        ) VALUES(?, ?, ?, '', ?, ?)
        """,
        (
            timing["signal_id"],
            timing["awc_hard_source_signal_at"],
            timing["order_submitted_at"],
            json.dumps(timing),
            "2026-07-03T09:05:40Z",
        ),
    )
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-3-2026",
        "event_date": "2026-07-03",
        "signal_to_order_timing": timing,
        "ssot_target_bucket": "32°C",
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', ?, '2026-07-03', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T09:05:38Z",
            json.dumps(payload),
            "2026-07-03T09:05:38Z",
        ),
    )
    details = {
        "hard_source_direct": True,
        "outcome": "32°C",
        "signal_to_order_timing": timing,
    }
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES(?, '2026-07-03', '32°C', 'YES', 'entry', 1, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            json.dumps(details),
            "2026-07-03T09:05:38Z",
        ),
    )
    job_payload = {
        "hard_source_direct": {
            "target_bucket": "32°C",
            "signal_to_order_timing": timing,
        },
        "entry_expected_targets": [
            {
                "outcome": "32°C",
                "side": "YES",
                "signal_to_order_timing": timing,
            }
        ],
    }
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(source, state, payload_json, created_at, started_at, ended_at)
        VALUES('hard_source_direct', 'success', ?, ?, ?, ?)
        """,
        (
            json.dumps(job_payload),
            "2026-07-03T09:05:38Z",
            "2026-07-03T09:05:39Z",
            "2026-07-03T09:05:40Z",
        ),
    )
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["blockers"] == []
    assert result["signal_count"] == 1
    assert result["candidate_scope_counts"] == {"accepted_awc_hard_source_signal": 1}
    assert result["terminal_state_counts"] == {"submitted": 1}
    coverage = result["production_coverage"]
    assert coverage["ok"] is True
    assert coverage["coverage_blockers"] == []
    assert coverage["accepted_signal_count"] == 1
    assert coverage["submitted_signal_count"] == 1
    assert coverage["terminal_coverage"]["ok"] is True
    assert coverage["base_timing_coverage"]["ok"] is True
    assert coverage["preparation_coverage"]["ok"] is True
    assert coverage["safety_gate_coverage"]["ok"] is True
    assert coverage["ledger_coverage"]["ok"] is True
    assert result["lineage_consistency"]["ok"] is True
    assert result["lineage_consistency"]["fact_snapshot_ids"] == ["fact_complete"]
    assert result["lineage_consistency"]["posterior_run_ids"] == ["post_complete"]
    signal = result["signals"][0]
    assert signal["final_terminal_state"] == "submitted"
    assert signal["pre_signal_prepare_state"] == "prepared_before_deadline"
    assert signal["lineage_consistency"]["fact_snapshot_ids"] == ["fact_complete"]
    assert signal["lineage_consistency"]["posterior_run_ids"] == ["post_complete"]
    assert "timings" not in signal

    detailed = audit.audit(
        db_path,
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        since="2026-07-03T00:00:00+00:00",
        include_details=True,
    )
    assert detailed["signal_details"][0]["timings"]


def _insert_complete_signal_with_optional_review(conn: sqlite3.Connection, *, with_review: bool = True) -> None:
    timing = _complete_timing()
    conn.execute(
        """
        INSERT INTO signal_to_order_timing(
            signal_id, awc_hard_source_signal_at, order_submitted_at,
            signal_to_order_slo_violation_reason, timing_json, created_at
        ) VALUES(?, ?, ?, '', ?, ?)
        """,
        (
            timing["signal_id"],
            timing["awc_hard_source_signal_at"],
            timing["order_submitted_at"],
            json.dumps(timing),
            "2026-07-03T09:05:40Z",
        ),
    )
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', ?, '2026-07-03', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T09:05:38Z",
            json.dumps(
                {
                    "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                    "event_date": "2026-07-03",
                    "lineage_state": {
                        "fact_snapshot_id": "fact_complete",
                        "posterior_run_id": "post_complete",
                    },
                    "source_observations": [
                        {
                            "source_event_id": "src_awc_complete",
                            "source_observation_id": "srcobs_awc_complete",
                            "source_id": "zuuu_metar_raw_awc",
                        }
                    ],
                    "signal_to_order_timing": timing,
                    "posterior_ssot_fact": {
                        "schema": "bayes_posterior_ssot_fact_v1",
                        "state": "available",
                        "fact_snapshot_id": "fact_complete",
                        "posterior_run_id": "post_complete",
                        "target_bucket": "32°C",
                        "target_probability": 1.0,
                    },
                    "distribution": [{"outcome": "32°C", "probability": 1.0}],
                }
            ),
            "2026-07-03T09:05:38Z",
        ),
    )
    _insert_decision_with_timing(conn, timing)
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(source, state, payload_json, created_at, started_at, ended_at)
        VALUES('hard_source_direct', 'success', ?, ?, ?, ?)
        """,
        (
            json.dumps(
                {
                    "hard_source_direct": {
                        "target_bucket": "32°C",
                        "signal_to_order_timing": timing,
                    },
                    "entry_expected_targets": [
                        {
                            "outcome": "32°C",
                            "side": "YES",
                            "signal_to_order_timing": timing,
                        }
                    ],
                }
            ),
            "2026-07-03T09:05:38Z",
            "2026-07-03T09:05:39Z",
            "2026-07-03T09:05:40Z",
        ),
    )
    if with_review:
        conn.execute(
            """
            CREATE TABLE daily_weather_reviews (
                review_id TEXT PRIMARY KEY,
                slug TEXT,
                event_date TEXT,
                fact_snapshot_id TEXT,
                review_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO daily_weather_reviews(review_id, slug, event_date, fact_snapshot_id, review_json, created_at)
            VALUES('rev_complete_chain', ?, '2026-07-03', 'fact_complete', ?, ?)
            """,
            (
                "highest-temperature-in-chengdu-on-july-3-2026",
                json.dumps(
                    {
                        "review_id": "rev_complete_chain",
                        "fact_snapshot_id": "fact_complete",
                        "posterior_lineage": {
                            "fact_snapshot_id": "fact_complete",
                            "posterior_run_id": "post_complete",
                        },
                    }
                ),
                "2026-07-04T00:00:00Z",
            ),
        )


def test_ssot_chain_replay_requires_awc_full_chain_from_signal_to_review(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    _insert_complete_signal_with_optional_review(conn, with_review=True)
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        signal_id="sig_complete",
        since="2026-07-03T00:00:00+00:00",
        require_awc_full_chain=True,
    )

    assert result["ok"] is True
    full_chain = result["awc_full_chain_replay"]
    assert full_chain["schema"] == "awc_signal_full_chain_replay_contract_v1"
    assert full_chain["enabled"] is True
    assert full_chain["ok"] is True
    assert full_chain["required_chain"] == [
        "source",
        "fact",
        "posterior",
        "decision",
        "execution",
        "ui_snapshot",
        "review",
    ]
    assert full_chain["missing_nodes"] == []
    assert full_chain["nodes"]["source"]["source_event_id"] == "src_awc_complete"
    assert full_chain["nodes"]["fact"]["fact_snapshot_ids"] == ["fact_complete"]
    assert full_chain["nodes"]["posterior"]["posterior_run_ids"] == ["post_complete"]
    assert full_chain["nodes"]["decision"]["decision_ids"]
    assert full_chain["nodes"]["execution"]["terminal_state"] == "submitted"
    assert full_chain["nodes"]["ui_snapshot"]["reason"] == "ui_snapshot_is_rendered_from_fact_snapshot"
    assert full_chain["nodes"]["review"]["review_id"] == "rev_complete_chain"
    assert full_chain["guards"]["writes_archive_db"] is False


def test_ssot_chain_replay_requires_awc_full_chain_blocks_missing_review(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    _insert_complete_signal_with_optional_review(conn, with_review=False)
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        signal_id="sig_complete",
        since="2026-07-03T00:00:00+00:00",
        require_awc_full_chain=True,
    )

    assert result["ok"] is False
    assert "awc_full_chain_review_missing" in result["blockers"]
    assert result["awc_full_chain_replay"]["ok"] is False
    assert result["awc_full_chain_replay"]["missing_nodes"] == ["review"]


def test_ssot_chain_replay_blocks_fact_snapshot_lineage_conflict(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    conn.execute(
        """
        INSERT INTO signal_to_order_timing(
            signal_id, awc_hard_source_signal_at, order_submitted_at,
            signal_to_order_slo_violation_reason, timing_json, created_at
        ) VALUES(?, ?, ?, '', ?, ?)
        """,
        (
            timing["signal_id"],
            timing["awc_hard_source_signal_at"],
            timing["order_submitted_at"],
            json.dumps(timing),
            "2026-07-03T09:05:40Z",
        ),
    )
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', ?, '2026-07-03', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T09:05:38Z",
            json.dumps(
                {
                    "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                    "event_date": "2026-07-03",
                    "signal_to_order_timing": timing,
                    "ssot_target_bucket": "32°C",
                }
            ),
            "2026-07-03T09:05:38Z",
        ),
    )
    decision_timing = dict(timing)
    decision_timing["fact_snapshot_id"] = "fact_other"
    _insert_decision_with_timing(conn, decision_timing)
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is False
    assert "lineage_fact_snapshot_id_conflict" in result["blockers"]
    assert result["lineage_consistency"]["ok"] is False
    assert sorted(result["lineage_consistency"]["fact_snapshot_ids"]) == ["fact_complete", "fact_other"]
    assert result["signals"][0]["lineage_consistency"]["blockers"] == ["lineage_fact_snapshot_id_conflict"]


def test_ssot_chain_replay_flags_partial_preparation_and_non_final_terminal(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = {
        "signal_id": "sig_partial",
        "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
        "pre_signal_prepare_state": "preparation_partial",
        "pre_signal_prepare_reason": "missing_required_components:orderbook",
        "pre_signal_prepare_complete": False,
        "pre_signal_prepare_missing_components": ["orderbook"],
        "pre_signal_prepared_before_deadline": True,
        "terminal_state": "decision_written",
        "terminal_reason": "awaiting_job_enqueue_or_execution",
    }
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES(?, '2026-07-03', '32°C', 'YES', 'entry', 1, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            json.dumps({"signal_to_order_timing": timing}),
            "2026-07-03T09:05:38Z",
        ),
    )
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is False
    assert "pre_signal_preparation_incomplete" in result["blockers"]
    assert "signal_terminal_non_final" in result["blockers"]
    assert result["blocker_counts"]["pre_signal_preparation_incomplete"] == 1
    assert result["blocker_counts"]["signal_terminal_non_final"] == 1
    coverage = result["production_coverage"]
    assert coverage["ok"] is False
    assert "accepted_signal_terminal_coverage_incomplete" in coverage["coverage_blockers"]
    assert "accepted_signal_base_timing_incomplete" in coverage["coverage_blockers"]
    assert "accepted_signal_preparation_coverage_incomplete" in coverage["coverage_blockers"]
    assert coverage["terminal_coverage"]["non_final_terminal_signal_ids"] == ["sig_partial"]
    assert coverage["preparation_coverage"]["incomplete_preparation_signal_ids"] == ["sig_partial"]
    assert result["signals"][0]["pre_signal_prepare_missing_components"] == ["orderbook"]


def test_ssot_chain_replay_blocks_missing_pre_signal_component_contract(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    timing["signal_id"] = "sig_missing_component_contract"
    timing["pre_signal_prepare_components"] = dict(timing["pre_signal_prepare_components"])
    timing["pre_signal_prepare_components"].pop("open_orders")
    conn.execute(
        """
        INSERT INTO signal_to_order_timing(
            signal_id, awc_hard_source_signal_at, order_submitted_at,
            signal_to_order_slo_violation_reason, timing_json, created_at
        ) VALUES(?, ?, ?, '', ?, ?)
        """,
        (
            timing["signal_id"],
            timing["awc_hard_source_signal_at"],
            timing["order_submitted_at"],
            json.dumps(timing),
            "2026-07-03T09:05:40Z",
        ),
    )
    _insert_decision_with_timing(conn, timing)
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is False
    assert "pre_signal_required_component_contract_incomplete" in result["blockers"]
    signal = result["signals"][0]
    assert signal["pre_signal_required_component_contract_missing"] == ["open_orders"]
    assert result["production_coverage"]["preparation_coverage"][
        "required_component_incomplete_signal_ids"
    ] == ["sig_missing_component_contract"]


def test_ssot_chain_replay_classifies_legacy_nonaccepted_job_as_warning(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = {
        "signal_id": "sig_legacy_rejected",
        "source_event_id": "srcevt_legacy_rejected",
        "acceptance_state": "rejected",
        "acceptance_reason": "signal_seen_after_stale_deadline",
        "signal_eligible": False,
        "preparation_started_at": "2026-07-03T09:00:00Z",
        "awc_pre_signal_deadline_at": "2026-07-03T09:05:00Z",
        "terminal_state": "job_enqueued",
        "terminal_reason": "awaiting_order_submit",
    }
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(source, state, payload_json, created_at, started_at, ended_at)
        VALUES('hard_source_direct', 'success', ?, ?, ?, ?)
        """,
        (
            json.dumps(
                {
                    "hard_source_direct": {
                        "target_bucket": "32°C",
                        "signal_to_order_timing": timing,
                    }
                }
            ),
            "2026-07-03T09:06:00Z",
            "2026-07-03T09:06:01Z",
            "2026-07-03T09:06:02Z",
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(audit, "_current_hard_source_runtime_contract_version", lambda: "current_contract_v1")

    result = audit.audit(db_path, since="2026-07-03T00:00:00+00:00")

    assert result["ok"] is True
    assert result["blockers"] == []
    assert result["warnings"] == ["legacy_non_accepted_candidate_has_job_artifact"]
    assert result["warning_counts"]["legacy_non_accepted_candidate_has_job_artifact"] == 1
    signal = result["signals"][0]
    assert signal["candidate_scope"] == "non_accepted_candidate"
    assert signal["current_runtime_nonaccepted_artifact"] is False


def test_ssot_chain_replay_blocks_current_contract_nonaccepted_job(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = {
        "signal_id": "sig_current_rejected",
        "source_event_id": "srcevt_current_rejected",
        "acceptance_state": "rejected",
        "acceptance_reason": "signal_seen_after_stale_deadline",
        "signal_eligible": False,
        "terminal_state": "job_enqueued",
        "terminal_reason": "awaiting_order_submit",
    }
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(source, state, payload_json, created_at, started_at, ended_at)
        VALUES('hard_source_direct', 'success', ?, ?, ?, ?)
        """,
        (
            json.dumps(
                {
                    "hard_source_direct": {
                        "target_bucket": "32°C",
                        "hard_source_runtime_contract_version": "current_contract_v1",
                        "signal_to_order_timing": timing,
                    }
                }
            ),
            "2026-07-03T09:06:00Z",
            "2026-07-03T09:06:01Z",
            "2026-07-03T09:06:02Z",
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(audit, "_current_hard_source_runtime_contract_version", lambda: "current_contract_v1")

    result = audit.audit(db_path, since="2026-07-03T00:00:00+00:00")

    assert result["ok"] is False
    assert result["blockers"] == ["non_accepted_candidate_has_job_artifact"]
    assert result["runtime_contract_version_counts"] == {"current_contract_v1": 1}
    signal = result["signals"][0]
    assert signal["current_runtime_nonaccepted_artifact"] is True
    assert signal["hard_source_runtime_contract_versions"] == ["current_contract_v1"]


def test_ssot_chain_replay_marks_old_contract_accepted_signal_gaps_as_legacy(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = {
        "signal_id": "sig_old_contract_fact_only",
        "source_event_id": "srcevt_old_contract_fact_only",
        "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
        "acceptance_state": "accepted",
        "acceptance_reason": "awc_hard_source_signal_accepted",
        "signal_eligible": True,
        "terminal_state": "fact_written",
        "terminal_reason": "awaiting_decision_write",
        "terminal_at": "2026-07-03T09:05:39Z",
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', ?, '2026-07-03', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T09:05:39Z",
            json.dumps(
                {
                    "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                    "event_date": "2026-07-03",
                    "hard_source_runtime_contract_version": "old_contract_v1",
                    "signal_to_order_timing": timing,
                }
            ),
            "2026-07-03T09:05:39Z",
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(audit, "_current_hard_source_runtime_contract_version", lambda: "current_contract_v2")

    result = audit.audit(db_path, since="2026-07-03T00:00:00+00:00")

    assert result["ok"] is True
    assert result["blockers"] == []
    assert result["warnings"] == [
        "legacy_accepted_signal_terminal_non_final",
        "legacy_pre_signal_preparation_missing",
    ]
    signal = result["signals"][0]
    assert signal["legacy_runtime_record"] is True
    assert signal["hard_source_runtime_contract_versions"] == ["old_contract_v1"]

    strict = audit.audit(
        db_path,
        since="2026-07-03T00:00:00+00:00",
        fail_legacy_artifacts=True,
    )
    assert strict["ok"] is False
    assert "pre_signal_preparation_missing" in strict["blockers"]
    assert "signal_terminal_non_final" in strict["blockers"]


def test_ssot_chain_replay_backfills_decisions_for_existing_signal_records_when_limit_misses(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    conn.execute(
        """
        INSERT INTO signal_to_order_timing (
            signal_id, awc_hard_source_signal_at, fact_snapshot_written_at,
            posterior_written_at, auto_trade_decision_written_at,
            signal_to_order_slo_violation_reason, timing_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, '', ?, ?)
        """,
        (
            timing["signal_id"],
            timing["awc_hard_source_signal_at"],
            timing["fact_written_at"],
            timing["posterior_written_at"],
            timing["decision_written_at"],
            json.dumps(timing),
            "2026-07-03T09:05:42Z",
        ),
    )
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES('other', '2026-07-03', '31°C', 'YES', 'entry', 0, '{}', '2026-07-03T09:06:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES('slug', '2026-07-03', '32°C', 'YES', 'entry', 1, ?, '2026-07-03T09:05:40Z')
        """,
        (json.dumps({"signal_to_order_timing": timing}),),
    )
    conn.commit()
    conn.close()

    result = audit.audit(db_path, since="2026-07-03T00:00:00+00:00", limit=1)

    assert result["ok"] is True
    assert result["source_counts"]["auto_trade_decisions"] == 1
    assert result["signals"][0]["execution_chain_has_auto_trade_decision"] is True


def test_existing_signal_decision_backfill_uses_bounded_json_chunks_without_signal_id_column(monkeypatch, tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            outcome TEXT,
            side TEXT,
            will_order INTEGER,
            details_json TEXT,
            created_at TEXT
        )
        """
    )
    records = {
        f"sig_{idx:02d}": {
            "signal_id": f"sig_{idx:02d}",
            "sources": ["signal_to_order_timing"],
            "timings": [],
            "terminal_states": [],
            "target_outcomes": [],
            "decision_ids": [],
            "job_ids": [],
            "order_intent_ids": [],
            "hard_source_runtime_contract_versions": [],
            "blockers": [],
            "warnings": [],
        }
        for idx in range(25)
    }
    timing = _complete_timing()
    timing["signal_id"] = "sig_24"
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(action, outcome, side, will_order, details_json, created_at)
        VALUES('entry', '37°C', 'YES', 1, ?, '2026-07-03T09:05:40Z')
        """,
        (json.dumps({"signal_to_order_timing": timing}),),
    )
    conn.commit()
    select_count = 0

    def trace(sql: str) -> None:
        nonlocal select_count
        if "FROM auto_trade_decisions" in sql:
            select_count += 1

    conn.set_trace_callback(trace)
    monkeypatch.setenv("SSOT_CHAIN_REPLAY_DECISION_BACKFILL_CHUNK_SIZE", "10")
    monkeypatch.setenv("SSOT_CHAIN_REPLAY_DECISION_BACKFILL_SIGNAL_MULTIPLIER", "100")

    result = audit._collect_decisions_for_existing_records(
        conn,
        records,
        since="2026-07-03T00:00:00+00:00",
        limit=1,
    )

    assert result["scan_reason"] == "existing_signal_id_bounded_json_decision_backfill"
    assert result["query_count"] == 3
    assert select_count == 3
    assert result["appended_rows"] == 1
    assert "auto_trade_decisions" in records["sig_24"]["sources"]


def test_ssot_chain_replay_allows_reviewed_no_action_without_decision(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = {
        "schema": "signal_to_order_timing_v1",
        "signal_id": "sig_reviewed_no_action",
        "source_event_id": "srcevt_reviewed_no_action",
        "awc_valid_time": "2026-07-03T09:00:00Z",
        "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
        "acceptance_state": "accepted",
        "acceptance_reason": "awc_hard_source_signal_accepted",
        "signal_eligible": True,
        "fact_written_at": "2026-07-03T09:05:39Z",
        "posterior_written_at": "2026-07-03T09:05:39Z",
        "terminal_at": "2026-07-03T09:05:40Z",
        "terminal_state": "no_action",
        "terminal_reason": "Reviewed legacy fact/posterior only; no auto_trade_decision/order evidence exists.",
    }
    conn.execute(
        """
        INSERT INTO signal_to_order_timing (
            signal_id, awc_hard_source_signal_at, fact_snapshot_written_at,
            posterior_written_at, signal_to_order_slo_violation_reason,
            timing_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timing["signal_id"],
            timing["awc_hard_source_signal_at"],
            timing["fact_written_at"],
            timing["posterior_written_at"],
            timing["terminal_reason"],
            json.dumps(timing),
            "2026-07-03T09:05:40Z",
        ),
    )
    conn.commit()
    conn.close()

    result = audit.audit(db_path, since="2026-07-03T00:00:00+00:00")

    assert result["ok"] is True
    assert "accepted_signal_missing_auto_trade_decision" not in result["blockers"]
    assert result["production_coverage"]["timing_required_signal_count"] == 0


def test_ssot_chain_replay_order_anchor_without_awc_signal_is_not_applicable(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    conn.execute(
        """
        INSERT INTO auto_orders(order_id, slug, event_date, outcome, side, action, created_at)
        VALUES(?, ?, '2026-07-03', '31°C', 'YES', 'entry', ?)
        """,
        (
            "ord_posterior_only",
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T13:30:34Z",
        ),
    )
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES(?, '2026-07-03', '31°C', 'YES', 'entry', 1, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            json.dumps(
                {
                    "source": "posterior_target_direct",
                    "posterior_target_direct": True,
                    "order_id": "ord_posterior_only",
                    "fact_snapshot_id": "fact_posterior_only",
                    "posterior_run_id": "post_posterior_only",
                    "posterior_ssot_fact": {
                        "target_bucket": "31°C",
                        "target_probability": 0.61,
                        "fact_snapshot_id": "fact_posterior_only",
                        "posterior_run_id": "post_posterior_only",
                    },
                }
            ),
            "2026-07-03T13:30:34Z",
        ),
    )
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        order_id="ord_posterior_only",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["signal_count"] == 0
    assert result["warnings"] == ["anchor_has_no_awc_signal_expected"]
    assert result["warning_counts"] == {"anchor_has_no_awc_signal_expected": 1}
    assert result["effective_slug"] == "highest-temperature-in-chengdu-on-july-3-2026"
    assert result["effective_event_date"] == "2026-07-03"
    assert result["effective_posterior_run_id"] == "post_posterior_only"
    assert result["anchor_trace"]["resolved"]["fact_snapshot_id"] == "fact_posterior_only"
    assert result["anchor_trace"]["resolved"]["posterior_run_id"] == "post_posterior_only"
    assert result["anchor_trace"]["anchors"]["order"]["available"] is True
    assert result["anchor_trace"]["anchors"]["order"]["decision_anchor"]["available"] is True
    assert result["anchor_trace"]["anchors"]["order"]["posterior_target_lineage_source"] == "top_level"


def test_ssot_chain_replay_decision_anchor_without_awc_signal_carries_posterior_lineage(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES(?, '2026-07-03', '31°C', 'YES', 'entry', 1, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            json.dumps(
                {
                    "source": "posterior_target_direct",
                    "posterior_target_direct": True,
                    "posterior_ssot_fact": {
                        "target_bucket": "31°C",
                        "target_probability": 0.61,
                        "fact_snapshot_id": "fact_decision_only",
                        "posterior_run_id": "post_decision_only",
                    },
                }
            ),
            "2026-07-03T13:30:34Z",
        ),
    )
    decision_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        decision_id=str(decision_id),
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["signal_count"] == 0
    assert result["warnings"] == ["anchor_has_no_awc_signal_expected"]
    assert result["effective_posterior_run_id"] == "post_decision_only"
    assert result["anchor_trace"]["resolved"]["fact_snapshot_id"] == "fact_decision_only"
    assert result["anchor_trace"]["resolved"]["posterior_run_id"] == "post_decision_only"
    assert result["anchor_trace"]["anchors"]["decision"]["posterior_ssot_fact_present"] is True


def test_ssot_chain_replay_job_anchor_without_awc_signal_carries_posterior_lineage(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(source, state, payload_json, created_at, started_at, ended_at)
        VALUES('posterior_target_direct', 'success', ?, ?, ?, ?)
        """,
        (
            json.dumps(
                {
                    "include_event_keys": [
                        {
                            "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                            "event_date": "2026-07-03",
                        }
                    ],
                    "posterior_target_direct": {
                        "target_bucket": "31°C",
                        "fact_snapshot_id": "fact_job_only",
                        "posterior_run_id": "post_job_only",
                    },
                    "posterior_ssot_fact": {
                        "target_bucket": "31°C",
                        "target_probability": 0.61,
                        "fact_snapshot_id": "fact_job_only",
                        "posterior_run_id": "post_job_only",
                    },
                }
            ),
            "2026-07-03T13:30:34Z",
            "2026-07-03T13:30:34Z",
            "2026-07-03T13:30:35Z",
        ),
    )
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        job_id=str(job_id),
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["signal_count"] == 0
    assert result["warnings"] == ["anchor_has_no_awc_signal_expected"]
    assert result["effective_slug"] == "highest-temperature-in-chengdu-on-july-3-2026"
    assert result["effective_event_date"] == "2026-07-03"
    assert result["effective_posterior_run_id"] == "post_job_only"
    assert result["anchor_trace"]["resolved"]["fact_snapshot_id"] == "fact_job_only"
    assert result["anchor_trace"]["resolved"]["posterior_run_id"] == "post_job_only"
    assert result["anchor_trace"]["anchors"]["job"]["posterior_ssot_fact_present"] is True


def test_ssot_chain_replay_order_anchor_resolves_to_signal(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    conn.execute(
        """
        INSERT INTO auto_orders(order_id, slug, event_date, outcome, side, action, created_at)
        VALUES(?, ?, '2026-07-03', '32°C', 'YES', 'entry', ?)
        """,
        (
            "ord_hard_source",
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T09:05:40Z",
        ),
    )
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES(?, '2026-07-03', '32°C', 'YES', 'entry', 1, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            json.dumps(
                {
                    "source": "hard_source_direct",
                    "hard_source_direct": True,
                    "order_id": "ord_hard_source",
                    "signal_to_order_timing": timing,
                }
            ),
            "2026-07-03T09:05:40Z",
        ),
    )
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        order_id="ord_hard_source",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["signal_count"] == 1
    assert result["effective_signal_id"] == "sig_complete"
    assert result["anchor_trace"]["resolved"]["hard_source_signal_expected"] is True
    assert result["signals"][0]["final_terminal_state"] == "submitted"


def test_ssot_chain_replay_order_anchor_prefers_structured_lineage(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES(?, '2026-07-03', '32°C', 'YES', 'entry', 1, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            json.dumps(
                {
                    "source": "hard_source_direct",
                    "hard_source_direct": True,
                    "signal_to_order_timing": timing,
                }
            ),
            "2026-07-03T09:05:40Z",
        ),
    )
    decision_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO auto_orders(
            order_id, decision_id, signal_id, order_intent_id,
            slug, event_date, outcome, side, action, created_at
        )
        VALUES(?, ?, ?, ?, ?, '2026-07-03', '32°C', 'YES', 'entry', ?)
        """,
        (
            "ord_structured_lineage",
            str(decision_id),
            "sig_complete",
            "intent-structured",
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T09:05:40Z",
        ),
    )
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        order_id="ord_structured_lineage",
        since="2026-07-03T00:00:00+00:00",
    )

    order_anchor = result["anchor_trace"]["anchors"]["order"]
    assert result["ok"] is True
    assert result["effective_signal_id"] == "sig_complete"
    assert order_anchor["decision_id"] == str(decision_id)
    assert order_anchor["signal_id"] == "sig_complete"
    assert order_anchor["order_intent_id"] == "intent-structured"
    assert order_anchor["decision_anchor"]["decision_id"] == str(decision_id)


def test_ssot_chain_replay_decision_anchor_collects_jobs_by_run_id(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing_decision = dict(_complete_timing())
    timing_decision["terminal_state"] = "decision_written"
    timing_decision["terminal_reason"] = "awaiting_job_enqueue_or_execution"
    timing_job = dict(_complete_timing())
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(run_id, slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES(77, ?, '2026-07-03', '32°C', 'YES', 'entry', 1, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            json.dumps(
                {
                    "source": "hard_source_direct",
                    "hard_source_direct": True,
                    "signal_to_order_timing": timing_decision,
                }
            ),
            "2026-07-03T09:05:38Z",
        ),
    )
    decision_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(run_id, source, state, payload_json, created_at, started_at, ended_at)
        VALUES(77, 'hard_source_direct', 'success', ?, ?, ?, ?)
        """,
        (
            json.dumps(
                {
                    "hard_source_direct": {
                        "target_bucket": "32°C",
                        "signal_to_order_timing": timing_job,
                    }
                }
            ),
            "2026-07-03T09:05:39Z",
            "2026-07-03T09:05:39Z",
            "2026-07-03T09:05:40Z",
        ),
    )
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        decision_id=str(decision_id),
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["anchor_trace"]["resolved"]["run_id"] == "77"
    assert result["source_scans"]["auto_trade_jobs"]["scan_reason"] == "run_id_anchor"
    assert result["signals"][0]["sources"] == ["auto_trade_decisions", "auto_trade_jobs"]
    assert result["signals"][0]["final_terminal_state"] == "submitted"


def test_ssot_chain_replay_fact_anchor_resolves_signal(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', ?, '2026-07-03', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T09:05:38Z",
            json.dumps(
                {
                    "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                    "event_date": "2026-07-03",
                    "signal_to_order_timing": timing,
                    "distribution": [{"outcome": "32°C", "q": 1.0}],
                    "source_acquisition_trace": {"source_observations": [{"source_id": "zuuu_metar_raw_awc"}]},
                }
            ),
            "2026-07-03T09:05:38Z",
        ),
    )
    fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    _insert_decision_with_timing(conn, timing)
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        fact_id=str(fact_id),
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["effective_signal_id"] == "sig_complete"
    assert result["anchor_trace"]["anchors"]["fact"]["posterior_present"] is True
    assert result["anchor_trace"]["anchors"]["fact"]["source_trace_present"] is True
    assert result["source_scans"]["auto_trade_jobs"]["scan_reason"] == "json_payload_scan"
    assert result["signals"][0]["sources"] == ["event_fact_history", "auto_trade_decisions"]
    assert result["signals"][0]["execution_chain_has_auto_trade_decision"] is True
    assert result["signals"][0]["decision_ids"]


def test_ssot_chain_replay_blocks_submitted_signal_without_auto_trade_decision(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', ?, '2026-07-03', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T09:05:38Z",
            json.dumps(
                {
                    "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                    "event_date": "2026-07-03",
                    "signal_to_order_timing": timing,
                    "distribution": [{"outcome": "32°C", "q": 1.0}],
                }
            ),
            "2026-07-03T09:05:38Z",
        ),
    )
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is False
    assert "accepted_signal_missing_auto_trade_decision" in result["blockers"]
    assert "submitted_without_auto_trade_decision" in result["blockers"]
    assert "execution_chain_missing_decision_id" in result["blockers"]
    assert result["signals"][0]["execution_chain_has_auto_trade_decision"] is False


def test_ssot_chain_replay_blocks_job_without_auto_trade_decision(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(source, state, payload_json, created_at, started_at, ended_at)
        VALUES('hard_source_direct', 'success', ?, ?, ?, ?)
        """,
        (
            json.dumps(
                {
                    "hard_source_direct": {
                        "target_bucket": "32°C",
                        "signal_to_order_timing": timing,
                    }
                }
            ),
            "2026-07-03T09:05:38Z",
            "2026-07-03T09:05:39Z",
            "2026-07-03T09:05:40Z",
        ),
    )
    conn.commit()
    conn.close()

    result = audit.audit(db_path, since="2026-07-03T00:00:00+00:00")

    assert result["ok"] is False
    assert "accepted_signal_missing_auto_trade_decision" in result["blockers"]
    assert "job_without_auto_trade_decision" in result["blockers"]
    assert "submitted_without_auto_trade_decision" in result["blockers"]
    assert result["signals"][0]["sources"] == ["auto_trade_jobs"]


def test_ssot_chain_replay_blocks_submitted_signal_without_pre_submit_checks(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    for key in (
        "orderbook_checked_at",
        "orderbook_refreshed_at",
        "risk_checked_at",
        "idempotency_checked_at",
        "submit_requested_at",
        "exchange_ack_at",
        "order_ack_at",
    ):
        timing.pop(key, None)
    _insert_decision_with_timing(conn, timing)
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is False
    assert "submitted_missing_orderbook_check" in result["blockers"]
    assert "submitted_missing_risk_check" in result["blockers"]
    assert "submitted_missing_idempotency_check" in result["blockers"]
    assert "submitted_missing_submit_request" in result["blockers"]
    assert "submitted_missing_exchange_ack" in result["blockers"]
    coverage = result["production_coverage"]
    assert coverage["ok"] is False
    assert "submitted_signal_execution_safety_coverage_incomplete" in coverage["coverage_blockers"]
    assert coverage["safety_gate_coverage"]["missing_by_signal"]["sig_complete"] == [
        "orderbook_checked_at",
        "risk_checked_at",
        "idempotency_checked_at",
        "submit_requested_at",
        "exchange_ack_at",
    ]
    assert result["signals"][0]["execution_chain_has_auto_trade_decision"] is True


def test_ssot_chain_replay_blocks_precheck_after_submit_request(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    timing["orderbook_checked_at"] = "2026-07-03T09:05:39.900Z"
    timing["submit_requested_at"] = "2026-07-03T09:05:39.100Z"
    _insert_decision_with_timing(conn, timing)
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is False
    assert result["blockers"] == ["submitted_precheck_after_submit_request"]
    assert result["signals"][0]["stage_timestamps"]["orderbook_checked_at"] == "2026-07-03T09:05:39.900Z"


def test_ssot_chain_replay_source_event_anchor_resolves_fact_signal(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', ?, '2026-07-03', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T09:05:38Z",
            json.dumps(
                {
                    "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                    "event_date": "2026-07-03",
                    "source_observations": [
                        {"source_event_id": "src_awc_complete", "source_id": "zuuu_metar_raw_awc"}
                    ],
                    "signal_to_order_timing": timing,
                    "distribution": [{"outcome": "32°C", "q": 1.0}],
                }
            ),
            "2026-07-03T09:05:38Z",
        ),
    )
    _insert_decision_with_timing(conn, timing)
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        source_event_id="src_awc_complete",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["effective_source_event_id"] == "src_awc_complete"
    assert result["effective_signal_id"] == "sig_complete"
    assert result["source_scans"]["event_fact_history"]["scan_reason"] == "fact_id_anchor"
    assert result["signals"][0]["sources"] == ["event_fact_history", "auto_trade_decisions"]
    assert result["signals"][0]["execution_chain_has_auto_trade_decision"] is True


def test_ssot_chain_replay_source_observation_anchor_resolves_fact_signal(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', ?, '2026-07-03', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T09:05:38Z",
            json.dumps(
                {
                    "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                    "event_date": "2026-07-03",
                    "source_observations": [
                        {
                            "source_observation_id": "srcobs_awc_complete",
                            "source_event_id": "src_awc_complete",
                            "source_id": "zuuu_metar_raw_awc",
                        }
                    ],
                    "source_acquisition_trace": {
                        "source_observation_ids": ["srcobs_awc_complete"],
                    },
                    "signal_to_order_timing": timing,
                    "distribution": [{"outcome": "32°C", "q": 1.0}],
                }
            ),
            "2026-07-03T09:05:38Z",
        ),
    )
    _insert_decision_with_timing(conn, timing)
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        source_observation_id="srcobs_awc_complete",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["effective_source_observation_id"] == "srcobs_awc_complete"
    assert result["effective_source_event_id"] == "src_awc_complete"
    assert result["effective_signal_id"] == "sig_complete"
    assert result["anchor_trace"]["anchors"]["source_observation"]["available"] is True
    assert result["anchor_trace"]["resolved"]["source_observation_ids"] == ["srcobs_awc_complete"]
    assert result["source_scans"]["event_fact_history"]["scan_reason"] == "fact_id_anchor"
    assert result["signals"][0]["source_observation_id"] == "srcobs_awc_complete"
    assert result["signals"][0]["sources"] == ["event_fact_history", "auto_trade_decisions"]
    assert result["signals"][0]["execution_chain_has_auto_trade_decision"] is True


def test_ssot_chain_replay_source_observation_anchor_does_not_treat_writer_placeholder_as_awc_signal(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', ?, '2026-07-03', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T09:05:38Z",
            json.dumps(
                {
                    "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                    "event_date": "2026-07-03",
                    "source_observations": [
                        {
                            "source_observation_id": "srcobs_writer_placeholder",
                            "source_event_id": "src_writer_placeholder",
                            "source_id": "awc_cache_metars_csv_gz",
                        }
                    ],
                    "source_observation_ids": ["srcobs_writer_placeholder"],
                    "signal_to_order_timing": {
                        "schema": "signal_to_order_timing_placeholder_v1",
                        "state": "not_applicable",
                        "source_observation_ids": ["srcobs_writer_placeholder"],
                        "source_to_posterior_timing": {
                            "schema": "source_to_posterior_timing_v1",
                            "state": "available",
                            "surface": "amsc_hard_source_to_posterior_materialization",
                            "latest_source_fact_as_of": "2026-07-03T09:05:30Z",
                            "posterior_apply_started_at": "2026-07-03T09:05:38Z",
                            "source_fact_to_posterior_apply_start_ms": 8000,
                            "order_signal_applicability": "not_applicable_no_order_signal",
                        },
                    },
                    "distribution": [{"outcome": "32°C", "q_temperature_bucket_posterior": 1.0}],
                }
            ),
            "2026-07-03T09:05:38Z",
        ),
    )
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        source_observation_id="srcobs_writer_placeholder",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["effective_source_observation_id"] == "srcobs_writer_placeholder"
    assert result["effective_source_event_id"] == "src_writer_placeholder"
    assert result["signal_count"] == 0
    assert result["blockers"] == []
    assert result["warnings"] == ["anchor_has_no_awc_signal_expected"]
    assert result["anchor_trace"]["resolved"]["hard_source_signal_expected"] is False
    assert "anchor_signal_id_missing" not in result["blocker_counts"]


def test_ssot_chain_replay_settlement_final_lock_override_does_not_require_awc_signal(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', ?, '2026-07-03', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T16:05:00Z",
            json.dumps(
                {
                    "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                    "event_date": "2026-07-03",
                    "lineage_state": {
                        "fact_snapshot_id": "fact_final_lock",
                        "posterior_run_id": "post_final_lock",
                    },
                    "hard_source_posterior_override": {
                        "source": "event_runs.settlement_actual",
                        "final_bucket_lock": True,
                        "constraint_mode": "final_bucket_lock",
                        "final_lock_reason": "settlement_actual_known",
                        "target_bucket": "37°C",
                    },
                    "distribution": [{"outcome": "37°C", "q_temperature_bucket_posterior": 1.0}],
                }
            ),
            "2026-07-03T16:05:00Z",
        ),
    )
    fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        fact_id=str(fact_id),
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["signal_count"] == 0
    assert result["warnings"] == ["anchor_has_no_awc_signal_expected"]
    assert result["anchor_trace"]["resolved"]["hard_source_signal_expected"] is False
    assert "anchor_signal_id_missing" not in result["blocker_counts"]


def test_ssot_chain_replay_posterior_run_anchor_resolves_fact_signal(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    timing = _complete_timing()
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', ?, '2026-07-03', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T09:05:38Z",
            json.dumps(
                {
                    "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                    "event_date": "2026-07-03",
                    "lineage_state": {
                        "fact_snapshot_id": "fact_complete",
                        "posterior_run_id": "post_complete",
                    },
                    "signal_to_order_timing": timing,
                    "distribution": [{"outcome": "32°C", "q": 1.0}],
                }
            ),
            "2026-07-03T09:05:38Z",
        ),
    )
    _insert_decision_with_timing(conn, timing)
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        posterior_run_id="post_complete",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["effective_posterior_run_id"] == "post_complete"
    assert result["effective_signal_id"] == "sig_complete"
    assert result["signals"][0]["sources"] == ["event_fact_history", "auto_trade_decisions"]
    assert result["signals"][0]["execution_chain_has_auto_trade_decision"] is True


def test_ssot_chain_replay_review_anchor_missing_table_is_na(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        review_id="rev_missing_table",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["signal_count"] == 0
    assert result["warnings"] == ["daily_weather_reviews_missing_planned_table"]
    assert result["warning_counts"] == {"daily_weather_reviews_missing_planned_table": 1}
    assert result["anchor_trace"]["resolved"]["anchor_not_applicable"] is True


def test_ssot_chain_replay_review_anchor_does_not_treat_writer_placeholder_as_signal(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    conn.execute(
        """
        CREATE TABLE daily_weather_reviews (
            review_id TEXT PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            fact_snapshot_id TEXT,
            review_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', ?, '2026-07-03', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T15:05:00Z",
            json.dumps(
                {
                    "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                    "event_date": "2026-07-03",
                    "lineage_state": {
                        "fact_snapshot_id": "fact_review_no_signal",
                        "posterior_run_id": "post_review_no_signal",
                    },
                    "source_observations": [
                        {
                            "source_event_id": "srcevt_review_awc",
                            "source_observation_id": "srcobs_review_awc",
                            "source_id": "zuuu_metar_raw_awc",
                            "source_class": "hard_observation_candidate",
                            "role": "metar_hard_observation",
                        }
                    ],
                    "source_observation_ids": ["srcobs_review_awc"],
                    "hard_source_posterior_override": {
                        "source": "zuuu_metar_raw_awc",
                        "target_bucket": "32°C",
                    },
                    "signal_to_order_timing": {
                        "schema": "signal_to_order_timing_placeholder_v1",
                        "state": "not_applicable",
                        "state_reason": "fact_snapshot_write_does_not_submit_orders",
                        "source_observation_ids": ["srcobs_review_awc"],
                    },
                    "distribution": [{"outcome": "32°C", "q_temperature_bucket_posterior": 1.0}],
                }
            ),
            "2026-07-03T15:05:00Z",
        ),
    )
    conn.execute(
        """
        INSERT INTO daily_weather_reviews(review_id, slug, event_date, fact_snapshot_id, review_json, created_at)
        VALUES('rev_review_no_signal', ?, '2026-07-03', 'fact_review_no_signal', ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            json.dumps({"review_id": "rev_review_no_signal", "fact_snapshot_id": "fact_review_no_signal"}),
            "2026-07-04T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        review_id="rev_review_no_signal",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["signal_count"] == 0
    assert result["warnings"] == ["anchor_has_no_awc_signal_expected"]
    assert result["anchor_trace"]["resolved"]["hard_source_signal_expected"] is False
    assert "anchor_signal_id_missing" not in result["blocker_counts"]


def test_ssot_chain_replay_review_anchor_resolves_fact_snapshot_signal(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    conn.execute(
        """
        CREATE TABLE daily_weather_reviews (
            review_id TEXT PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            fact_snapshot_id TEXT,
            review_json TEXT,
            created_at TEXT
        )
        """
    )
    timing = _complete_timing()
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', ?, '2026-07-03', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03T09:05:38Z",
            json.dumps(
                {
                    "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                    "event_date": "2026-07-03",
                    "lineage_state": {
                        "fact_snapshot_id": "fact_complete",
                        "posterior_run_id": "post_complete",
                    },
                    "signal_to_order_timing": timing,
                    "distribution": [{"outcome": "32°C", "q": 1.0}],
                }
            ),
            "2026-07-03T09:05:38Z",
        ),
    )
    conn.execute(
        """
        INSERT INTO daily_weather_reviews(review_id, slug, event_date, fact_snapshot_id, review_json, created_at)
        VALUES('rev_complete', ?, '2026-07-03', 'fact_complete', ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            json.dumps({"review_id": "rev_complete", "fact_snapshot_id": "fact_complete"}),
            "2026-07-04T00:00:00Z",
        ),
    )
    decision_timing = dict(timing)
    decision_timing["terminal_state"] = "decision_written"
    decision_timing["terminal_reason"] = "awaiting_job_enqueue_or_execution"
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES(?, '2026-07-03', '32°C', 'YES', 'entry', 1, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            json.dumps(
                {
                    "source": "hard_source_direct",
                    "hard_source_direct": True,
                    "signal_to_order_timing": decision_timing,
                }
            ),
            "2026-07-03T09:05:38Z",
        ),
    )
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(source, state, payload_json, created_at, started_at, ended_at)
        VALUES('hard_source_direct', 'success', ?, ?, ?, ?)
        """,
        (
            json.dumps(
                {
                    "include_event_keys": [
                        {
                            "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                            "event_date": "2026-07-03",
                        }
                    ],
                    "hard_source_direct": {
                        "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                        "event_date": "2026-07-03",
                        "target_bucket": "32°C",
                        "signal_to_order_timing": timing,
                    },
                    "entry_expected_targets": [
                        {
                            "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                            "event_date": "2026-07-03",
                            "outcome": "32°C",
                            "side": "YES",
                            "signal_to_order_timing": timing,
                        }
                    ],
                }
            ),
            "2026-07-03T09:05:38Z",
            "2026-07-03T09:05:39Z",
            "2026-07-03T09:05:40Z",
        ),
    )
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        review_id="rev_complete",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["effective_review_id"] == "rev_complete"
    assert result["effective_posterior_run_id"] == "post_complete"
    assert result["effective_signal_id"] == "sig_complete"
    assert result["anchor_trace"]["anchors"]["review"]["fact_anchor"]["available"] is True
    assert result["source_counts"] == {
        "event_fact_history": 1,
        "auto_trade_decisions": 1,
        "auto_trade_jobs": 1,
    }
    assert result["source_scans"]["auto_trade_decisions"]["scan_reason"] == "json_or_event_scan"
    assert result["source_scans"]["auto_trade_jobs"]["scan_reason"] == "json_payload_scan"
    assert result["chain_coverage"]["review_available"] is True
    assert result["signals"][0]["sources"] == [
        "event_fact_history",
        "auto_trade_decisions",
        "auto_trade_jobs",
    ]
    assert result["signals"][0]["final_terminal_state"] == "submitted"


def test_ssot_chain_replay_reports_no_signals(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        since="2026-07-03T00:00:00+00:00",
    )

    assert result["ok"] is False
    assert result["blockers"] == ["no_awc_hard_source_signals_found"]
    assert result["blocker_counts"] == {"no_awc_hard_source_signals_found": 1}


def test_ssot_chain_replay_slug_event_latest_fact_without_expected_awc_signal_is_empty_state(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-06', ?, '2026-07-06', ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-6-2026",
            "2026-07-06T06:12:55Z",
            json.dumps(
                {
                    "slug": "highest-temperature-in-chengdu-on-july-6-2026",
                    "event_date": "2026-07-06",
                    "lineage_state": {
                        "fact_snapshot_id": "fact_no_awc_expected",
                        "posterior_run_id": "post_no_awc_expected",
                    },
                    "posterior_ssot_fact": {
                        "schema": "bayes_posterior_ssot_fact_v1",
                        "state": "available",
                        "target_bucket": "40°C",
                        "target_probability": 0.24,
                        "fact_snapshot_id": "fact_no_awc_expected",
                        "posterior_run_id": "post_no_awc_expected",
                    },
                    "source_acquisition_trace": {
                        "source_observations": [
                            {
                                "source_id": "zuuu_metar_raw_amsc",
                                "source_observation_id": "srcobs_amsc_no_awc_signal",
                                "role": "metar_observation_strong_likelihood",
                            }
                        ]
                    },
                    "distribution": [{"outcome": "40°C", "q_temperature_bucket_posterior": 0.24}],
                }
            ),
            "2026-07-06T06:12:55Z",
        ),
    )
    conn.commit()
    conn.close()

    result = audit.audit(
        db_path,
        slug="highest-temperature-in-chengdu-on-july-6-2026",
        event_date="2026-07-06",
        since="2026-07-04T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["signal_count"] == 0
    assert result["blockers"] == []
    assert result["warnings"] == ["anchor_has_no_awc_signal_expected"]
    assert result["warning_counts"] == {"anchor_has_no_awc_signal_expected": 1}
    assert result["effective_slug"] == "highest-temperature-in-chengdu-on-july-6-2026"
    assert result["effective_event_date"] == "2026-07-06"
    assert result["effective_posterior_run_id"] == "post_no_awc_expected"
    assert result["anchor_trace"]["anchor_provided"] is True
    assert result["anchor_trace"]["resolved"]["hard_source_signal_expected"] is False
    assert result["anchor_trace"]["anchors"]["latest_fact_for_resolved_event"]["available"] is True
