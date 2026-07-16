import json
import sqlite3
from pathlib import Path

from scripts import daily_review_persistence_batch_plan as batch_plan


SLUG = "highest-temperature-in-chengdu-on-july-4-2026"


def _create_db(
    tmp_path: Path,
    *,
    review_tables: bool = False,
    fact: bool = True,
    fact_snapshot_id: str = "fact_batch_1",
) -> Path:
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
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            decided_state TEXT,
            will_order INTEGER,
            reason_codes_json TEXT,
            reasons_json TEXT,
            details_json TEXT,
            created_at TEXT
        );
        CREATE TABLE auto_orders (
            id INTEGER PRIMARY KEY,
            order_id TEXT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            price REAL,
            size REAL,
            strategy TEXT,
            created_at TEXT,
            order_status TEXT,
            order_checked_at TEXT
        );
        """
    )
    if review_tables:
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
        (SLUG,),
    )
    if fact:
        fact_payload = {
            "slug": SLUG,
            "event_date": "2026-07-04",
            "run_date": "2026-07-04",
            "source_observations": [{"source_event_id": "srcevt_batch_1"}],
            "posterior_ssot_fact": {
                "schema": "bayes_posterior_ssot_fact_v1",
                "state": "available",
                "fact_snapshot_id": fact_snapshot_id,
                "posterior_run_id": "post_batch_1",
                "target_bucket": "30",
                "target_probability": 0.8,
                "bucket_probabilities": [
                    {"outcome": "30", "probability": 0.8},
                    {"outcome": "31", "probability": 0.2},
                ],
            },
            "distribution": [
                {"outcome": "30", "q_temperature_bucket_posterior": 0.8},
                {"outcome": "31", "q_temperature_bucket_posterior": 0.2},
            ],
            "posterior_target_bucket_selection": {
                "target_bucket": "30",
                "target_probability": 0.8,
            },
        }
        if fact_snapshot_id:
            fact_payload["lineage_state"] = {"fact_snapshot_id": fact_snapshot_id}
        conn.execute(
            """
            INSERT INTO event_fact_history
                (id, run_date, slug, event_date, fact_as_of, fact_json, created_at)
            VALUES(1, '2026-07-04', ?, '2026-07-04', '2026-07-04T09:05:38Z', ?, '2026-07-04T09:05:39Z')
            """,
            (SLUG, json.dumps(fact_payload)),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
                (id, run_id, run_date, slug, event_date, outcome, side, action,
                 decided_state, will_order, reason_codes_json, reasons_json, details_json, created_at)
            VALUES(1, 7, '2026-07-04', ?, '2026-07-04', '30', 'YES', 'entry',
                   'entry', 1, '[]', '[]', '{}', '2026-07-04T09:05:39Z')
            """,
            (SLUG,),
        )
        conn.execute(
            """
            INSERT INTO auto_orders
                (id, order_id, slug, event_date, outcome, side, action, price,
                 size, strategy, created_at, order_status)
            VALUES(1, 'ord_batch_1', ?, '2026-07-04', '30', 'YES', 'entry',
                   0.5, 1.0, 'hard_source_direct', '2026-07-04T09:05:40Z', 'submitted')
            """,
            (SLUG,),
        )
    conn.commit()
    conn.close()
    return db_path


def _add_good_settled_event(db_path: Path) -> None:
    slug = "highest-temperature-in-chengdu-on-july-5-2026"
    fact_payload = {
        "slug": slug,
        "event_date": "2026-07-05",
        "run_date": "2026-07-05",
        "source_observations": [{"source_event_id": "srcevt_batch_2"}],
        "lineage_state": {"fact_snapshot_id": "fact_batch_2"},
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "fact_snapshot_id": "fact_batch_2",
            "posterior_run_id": "post_batch_2",
            "target_bucket": "31",
            "target_probability": 0.7,
            "bucket_probabilities": [
                {"outcome": "31", "probability": 0.7},
                {"outcome": "32", "probability": 0.3},
            ],
        },
        "distribution": [
            {"outcome": "31", "q_temperature_bucket_posterior": 0.7},
            {"outcome": "32", "q_temperature_bucket_posterior": 0.3},
        ],
        "posterior_target_bucket_selection": {
            "target_bucket": "31",
            "target_probability": 0.7,
        },
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO event_runs
                (id, run_date, slug, event_date, city, station_id, settlement_actual,
                 rounding_rule, market_closed, event_end_utc, created_at)
            VALUES(2, '2026-07-05', ?, '2026-07-05', 'chengdu', 'ZUUU', 31.0,
                   'whole_degrees', 1, '2026-07-05T16:00:00Z', '2026-07-06T00:10:00Z')
            """,
            (slug,),
        )
        conn.execute(
            """
            INSERT INTO event_fact_history
                (id, run_date, slug, event_date, fact_as_of, fact_json, created_at)
            VALUES(2, '2026-07-05', ?, '2026-07-05', '2026-07-05T09:05:38Z', ?, '2026-07-05T09:05:39Z')
            """,
            (slug, json.dumps(fact_payload)),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
                (id, run_id, run_date, slug, event_date, outcome, side, action,
                 decided_state, will_order, reason_codes_json, reasons_json, details_json, created_at)
            VALUES(2, 8, '2026-07-05', ?, '2026-07-05', '31', 'YES', 'entry',
                   'entry', 1, '[]', '[]', '{}', '2026-07-05T09:05:39Z')
            """,
            (slug,),
        )
        conn.execute(
            """
            INSERT INTO auto_orders
                (id, order_id, slug, event_date, outcome, side, action, price,
                 size, strategy, created_at, order_status)
            VALUES(2, 'ord_batch_2', ?, '2026-07-05', '31', 'YES', 'entry',
                   0.5, 1.0, 'hard_source_direct', '2026-07-05T09:05:40Z', 'submitted')
            """,
            (slug,),
        )


def test_batch_plan_previews_reviews_when_schema_missing(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path, review_tables=False)

    result = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["execution_mode"] == "batch_no_apply_persistence_plan"
    assert result["settled_event_count"] == 1
    assert result["planned_review_count"] == 1
    assert result["planned_calibration_record_count"] == 1
    assert result["aggregate_row_counts"]["daily_weather_reviews"] == 1
    assert result["aggregate_row_counts"]["daily_calibration_records"] == 1
    assert result["lineage_apply_blocker_counts"] == {}
    assert result["lineage_readiness_state"]["ready_for_confirmed_apply"] is True
    assert result["lineage_repair_candidate_count"] == 0
    assert result["lineage_repair_candidates"] == []
    assert result["lineage_repair_operator_packet"]["candidate_count"] == 0
    assert result["lineage_repair_operator_packet"]["daily_review_persistence_apply_gate"]["blocked"] is False
    assert result["can_apply_now"] is False
    assert result["can_apply_after_schema_and_confirmation"] is False
    assert "daily_review_persistence_schema_missing" in result["apply_blockers"]
    assert result["daily_review_persistence_schema_status"]["tables"]["daily_weather_reviews"]["exists"] is False
    readiness = result["lineage_decision_apply_readiness"]
    assert readiness["schema"] == "daily_review_lineage_decision_apply_readiness_v1"
    assert readiness["read_only"] is True
    assert readiness["raw_scope"]["settled_event_count"] == 1
    assert readiness["raw_scope"]["planned_calibration_record_count"] == 1
    assert readiness["effective_scope"]["settled_event_count"] == 1
    assert readiness["effective_scope"]["planned_calibration_record_count"] == 1
    assert readiness["daily_review_persistence"]["ready_after_explicit_confirmation"] is False
    assert readiness["daily_review_persistence"]["planned_calibration_record_count_matches_effective_scope"] is True
    assert "daily_review_persistence_schema_missing" in readiness["daily_review_persistence"]["remaining_hard_blockers"]
    assert readiness["gates"][0]["gate"] == "daily_review_persistence_schema"
    assert readiness["gates"][0]["blocked"] is True
    assert readiness["guards"]["writes_archive_db"] is False
    assert result["guards"]["writes_archive_db"] is False
    item = result["items"][0]
    assert item["first_bad_link"] == "none"
    assert item["target_hit"] is True
    assert item["lineage_apply_blockers"] == []
    assert item["lineage_ready_for_apply"] is True
    assert item["lineage_repair_required"] is False
    assert item["persistence_plan_ok"] is True
    assert item["persistence_row_counts"]["daily_weather_reviews"] == 1
    assert item["persistence_row_counts"]["daily_calibration_records"] == 1


def test_batch_plan_is_ready_for_confirmed_apply_when_schema_present(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path, review_tables=True)

    result = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )

    assert result["ok"] is True
    assert result["daily_review_persistence_schema_status"]["ok"] is True
    assert result["planned_review_count"] == 1
    assert result["planned_calibration_record_count"] == 1
    assert result["lineage_apply_blocker_counts"] == {}
    assert result["lineage_readiness_state"]["ready_for_confirmed_apply"] is True
    assert result["can_apply_now"] is False
    assert result["can_apply_after_schema_and_confirmation"] is True
    assert result["apply_blockers"] == ["explicit_user_confirmation_required"]
    readiness = result["lineage_decision_apply_readiness"]
    assert readiness["daily_review_persistence"]["ready_after_explicit_confirmation"] is True
    assert readiness["daily_review_persistence"]["planned_calibration_record_count_matches_effective_scope"] is True
    assert readiness["daily_review_persistence"]["remaining_hard_blockers"] == []
    assert readiness["gates"][-1]["gate"] == "daily_review_persistence_apply"
    assert readiness["gates"][-1]["ready"] is True
    assert readiness["next_boundary"] == "controlled_daily_review_persistence_apply_after_confirmation"
    assert result["review_ids"]


def test_batch_plan_blocks_when_settled_event_has_no_fact(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path, review_tables=True, fact=False)

    result = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )

    assert result["ok"] is False
    assert result["settled_event_count"] == 1
    assert result["planned_review_count"] == 0
    assert result["blockers"] == ["fact_snapshot_not_found"]
    assert "batch_plan_has_blockers" in result["apply_blockers"]
    assert "batch_plan_incomplete_review_rows" in result["apply_blockers"]
    assert "batch_plan_lineage_incomplete" in result["apply_blockers"]
    assert result["lineage_readiness_state"]["ready_for_confirmed_apply"] is False
    assert result["lineage_repair_candidate_count"] == 1
    assert result["lineage_repair_candidates"][0]["repair_type"] == "missing_fact_snapshot_for_settled_event"
    assert result["lineage_repair_candidates"][0]["guards"]["writes_archive_db"] is False
    operator_packet = result["lineage_repair_operator_packet"]
    assert operator_packet["candidate_count"] == 1
    assert operator_packet["daily_review_persistence_apply_gate"]["blocked"] is True
    item = operator_packet["items"][0]
    assert item["repair_type"] == "missing_fact_snapshot_for_settled_event"
    assert "exclude_sample" in item["allowed_decisions"]
    assert "wait_for_new_fact" in item["allowed_decisions"]
    assert "manual_rebuild_fact_snapshot" in item["allowed_decisions"]
    assert "confirm_repair" not in item["allowed_decisions"]


def test_batch_plan_blocks_apply_and_proposes_no_apply_lineage_repair_for_legacy_fact(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path, review_tables=True, fact_snapshot_id="")

    result = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )

    assert result["ok"] is True
    assert result["planned_review_count"] == 1
    assert result["lineage_apply_blocker_counts"] == {"fact_snapshot_id_missing": 1}
    assert result["lineage_readiness_state"]["ready_for_confirmed_apply"] is False
    assert result["lineage_readiness_state"]["hard_stop_reason"] == "fact_snapshot_id_missing_blocks_daily_review_persistence"
    assert result["lineage_repair_candidate_count"] == 1
    assert result["lineage_repair_type_counts"] == {"legacy_fact_snapshot_lineage_repair": 1}
    assert result["lineage_readiness_state"]["operator_decision_required"] is True
    assert result["lineage_readiness_state"]["operator_packet_schema"] == "daily_review_lineage_repair_operator_packet_v1"
    assert result["can_apply_after_schema_and_confirmation"] is False
    assert "batch_plan_lineage_incomplete" in result["apply_blockers"]
    assert result["next_boundary"] == "review_lineage_repair_candidates_before_daily_review_apply"
    readiness = result["lineage_decision_apply_readiness"]
    assert readiness["operator_decision_counts"]["undecided"] == 1
    assert readiness["operator_decision_counts"]["missing"] == 0
    assert readiness["decision_manifest"]["manifest_provided"] is False
    assert readiness["decision_manifest"]["operator_decisions_recorded"] is False
    assert readiness["gates"][1]["gate"] == "lineage_decision_manifest"
    assert readiness["gates"][1]["blocked"] is True
    assert "batch_plan_lineage_incomplete" in readiness["daily_review_persistence"]["remaining_hard_blockers"]

    item = result["items"][0]
    assert item["lineage_apply_blockers"] == ["fact_snapshot_id_missing"]
    assert item["lineage_ready_for_apply"] is False
    assert item["lineage_repair_required"] is True
    candidate = item["lineage_repair_candidate"]
    assert candidate["repair_type"] == "legacy_fact_snapshot_lineage_repair"
    assert candidate["state"] == "proposed_no_apply"
    assert candidate["target_table"] == "event_fact_history"
    assert candidate["target_row_id"] == "1"
    assert candidate["candidate_fact_snapshot_id"].startswith("fact_")
    assert candidate["requires_explicit_user_confirmation"] is True
    assert candidate["writes_archive_db_if_confirmed"] is True
    assert candidate["guards"]["writes_archive_db"] is False
    operator_packet = result["lineage_repair_operator_packet"]
    assert operator_packet["schema"] == "daily_review_lineage_repair_operator_packet_v1"
    assert operator_packet["candidate_count"] == 1
    assert operator_packet["operator_decision_states"]["undecided"] == 1
    assert operator_packet["daily_review_persistence_apply_gate"]["blocked"] is True
    operator_item = operator_packet["items"][0]
    assert operator_item["must_decide_before_daily_review_persistence_apply"] is True
    assert operator_item["allowed_decisions"] == [
        "confirm_repair",
        "exclude_sample",
        "wait_for_new_fact",
        "manual_rebuild_fact_snapshot",
    ]
    decisions = {
        option["decision"]: option
        for option in operator_item["decision_options"]
    }
    assert decisions["confirm_repair"]["unblocks_daily_review_persistence_after_apply"] is True
    assert decisions["confirm_repair"]["requires_archive_db_backup"] is True
    assert decisions["exclude_sample"]["unblocks_daily_review_persistence_after_apply"] is True
    assert decisions["wait_for_new_fact"]["unblocks_daily_review_persistence_after_apply"] is False
    assert decisions["manual_rebuild_fact_snapshot"]["requires_source_lineage_review"] is True
    for option in decisions.values():
        assert option["guards"]["writes_archive_db"] is False


def test_lineage_decision_manifest_excludes_sample_from_current_apply_scope(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path, review_tables=True, fact_snapshot_id="")
    _add_good_settled_event(db_path)
    first_pass = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )
    repair_id = first_pass["lineage_repair_candidates"][0]["repair_correlation_id"]
    manifest = batch_plan.build_lineage_repair_decision_manifest(
        first_pass["lineage_repair_candidates"],
        decisions={
            repair_id: {
                "decision": "exclude_sample",
                "reason": "legacy sample excluded from first controlled persistence apply",
                "operator": "test",
            }
        },
        decided_at="2026-07-06T00:00:00Z",
    )

    result = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        lineage_decision_manifest=manifest,
    )

    assert manifest["schema"] == "daily_review_lineage_repair_decision_manifest_v1"
    assert result["settled_event_count"] == 2
    assert result["effective_settled_event_count"] == 1
    assert result["excluded_settled_event_count"] == 1
    assert result["raw_planned_review_count"] == 2
    assert result["planned_review_count"] == 1
    assert result["lineage_repair_candidate_count"] == 1
    assert result["effective_lineage_repair_candidate_count"] == 0
    assert result["lineage_apply_blocker_counts"] == {}
    assert result["raw_lineage_apply_blocker_counts"] == {"fact_snapshot_id_missing": 1}
    assert result["lineage_decision_manifest_audit"]["excluded_candidate_count"] == 1
    assert result["lineage_decision_manifest_audit"]["blocked_candidate_count"] == 0
    assert result["lineage_readiness_state"]["ready_for_confirmed_apply"] is True
    assert result["lineage_readiness_state"]["operator_decision_required"] is False
    assert result["can_apply_after_schema_and_confirmation"] is True
    assert result["apply_blockers"] == ["explicit_user_confirmation_required"]
    assert result["lineage_repair_apply_contract"]["repair_item_count"] == 0
    readiness = result["lineage_decision_apply_readiness"]
    assert readiness["raw_scope"]["settled_event_count"] == 2
    assert readiness["effective_scope"]["settled_event_count"] == 1
    assert readiness["effective_scope"]["excluded_settled_event_count"] == 1
    assert readiness["operator_decision_counts"]["exclude_sample"] == 1
    assert readiness["decision_manifest"]["operator_decisions_recorded"] is True
    assert readiness["decision_manifest"]["excluded_event_run_ids"] == [1]
    assert readiness["daily_review_persistence"]["ready_after_explicit_confirmation"] is True
    assert result["excluded_items"][0]["event_run_id"] == 1
    assert result["items"][0]["event_run_id"] == 2


def test_lineage_decision_manifest_confirm_repair_still_blocks_until_repair_applied(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path, review_tables=True, fact_snapshot_id="")
    first_pass = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )
    repair_id = first_pass["lineage_repair_candidates"][0]["repair_correlation_id"]
    manifest = batch_plan.build_lineage_repair_decision_manifest(
        first_pass["lineage_repair_candidates"],
        decisions={repair_id: "confirm_repair"},
    )

    result = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        lineage_decision_manifest=manifest,
    )

    audit = result["lineage_decision_manifest_audit"]
    assert audit["pending_resolution_count"] == 1
    assert audit["items"][0]["state"] == "confirmed_repair_pending_lineage_apply"
    assert result["effective_settled_event_count"] == 1
    assert result["effective_lineage_repair_candidate_count"] == 1
    assert result["can_apply_after_schema_and_confirmation"] is False
    assert "batch_plan_lineage_incomplete" in result["apply_blockers"]
    assert "lineage_decision_manifest_pending_resolution" in result["apply_blockers"]
    readiness = result["lineage_decision_apply_readiness"]
    assert readiness["operator_decision_counts"]["confirm_repair"] == 1
    assert readiness["decision_manifest"]["operator_decisions_recorded"] is True
    assert readiness["lineage_repair"]["repair_item_count"] == 1
    assert readiness["lineage_repair"]["repair_preflight_ready"] is True
    assert readiness["lineage_repair"]["repair_apply_ready_now"] is False
    assert "apply_flag" in readiness["lineage_repair"]["repair_apply_blockers"]
    assert "lineage_decision_manifest_pending_resolution" in readiness["daily_review_persistence"]["remaining_hard_blockers"]
    assert readiness["next_action"] == "run_controlled_lineage_repair_apply_then_rerun_batch_plan"


def test_lineage_decision_manifest_invalid_decision_is_a_hard_blocker(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path, review_tables=True, fact_snapshot_id="")
    first_pass = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )
    repair_id = first_pass["lineage_repair_candidates"][0]["repair_correlation_id"]
    manifest = {
        "schema": "daily_review_lineage_repair_decision_manifest_v1",
        "decisions": [
            {
                "repair_correlation_id": repair_id,
                "decision": "exclude_samples",
                "reason": "typo should not pass",
            }
        ],
    }

    result = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        lineage_decision_manifest=manifest,
    )

    audit = result["lineage_decision_manifest_audit"]
    assert audit["invalid_decision_count"] == 1
    assert audit["items"][0]["state"] == "invalid_decision"
    assert result["can_apply_after_schema_and_confirmation"] is False
    assert "lineage_decision_manifest_incomplete_or_invalid" in result["apply_blockers"]


def test_confirm_repair_manifest_generates_no_apply_lineage_repair_contract(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path, review_tables=True, fact_snapshot_id="")
    first_pass = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )
    repair_id = first_pass["lineage_repair_candidates"][0]["repair_correlation_id"]
    manifest = batch_plan.build_lineage_repair_decision_manifest(
        first_pass["lineage_repair_candidates"],
        decisions={
            repair_id: {
                "decision": "confirm_repair",
                "reason": "operator reviewed deterministic preview",
            }
        },
    )

    result = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        lineage_decision_manifest=manifest,
    )

    contract = result["lineage_repair_apply_contract"]
    assert contract["schema"] == "daily_review_lineage_repair_apply_contract_v1"
    assert contract["read_only"] is True
    assert contract["dry_run"] is True
    assert contract["apply_supported"] is True
    assert contract["confirmation_phrase_required"] == "APPLY_DAILY_REVIEW_LINEAGE_REPAIR"
    assert contract["repair_item_count"] == 1
    assert "apply_flag" in contract["apply_blockers"]
    assert "confirmation_phrase" in contract["apply_blockers"]
    assert "fresh_archive_db_backup" in contract["apply_blockers"]
    assert "writer_quiescence" in contract["apply_blockers"]
    item = contract["repair_items"][0]
    assert item["repair_correlation_id"] == repair_id
    assert item["target_table"] == "event_fact_history"
    assert item["target_row_id"] == "1"
    assert item["candidate_fact_snapshot_id"].startswith("fact_")
    assert item["expected_merge_fill"]["mode"] == "merge_fill_only_if_missing"
    paths = {path["json_path"]: path for path in item["expected_merge_fill"]["paths"]}
    assert paths["fact_snapshot_id"]["overwrite_allowed"] is False
    assert paths["lineage_state.fact_snapshot_id"]["value"] == item["candidate_fact_snapshot_id"]
    assert item["sql_shape_preview"]["literal_sql_included"] is False
    assert item["guards"]["writes_archive_db"] is False
    assert contract["guards"]["writes_event_fact_history"] is False
    assert contract["boundary"]["bulk_history_backfill_included"] is False


def test_cli_emits_lineage_repair_apply_contract_for_confirm_repair_manifest(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = _create_db(tmp_path, review_tables=True, fact_snapshot_id="")
    first_pass = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )
    repair_id = first_pass["lineage_repair_candidates"][0]["repair_correlation_id"]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "daily_review_lineage_repair_decision_manifest_v1",
                "decisions": [
                    {
                        "repair_correlation_id": repair_id,
                        "decision": "confirm_repair",
                        "reason": "operator reviewed deterministic preview",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    code = batch_plan.main(
        [
            "--db",
            str(db_path),
            "--since-date",
            "2026-07-01",
            "--lineage-decision-manifest",
            str(manifest_path),
            "--emit-lineage-repair-apply-contract",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["schema"] == "daily_review_lineage_repair_apply_contract_v1"
    assert output["repair_item_count"] == 1
    assert output["repair_items"][0]["repair_correlation_id"] == repair_id
    assert output["guards"]["executes_sql"] is False


def test_cli_emits_lineage_decision_manifest_template(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = _create_db(tmp_path, review_tables=True, fact_snapshot_id="")

    code = batch_plan.main(
        [
            "--db",
            str(db_path),
            "--since-date",
            "2026-07-01",
            "--max-items-output",
            "1",
            "--emit-lineage-decision-manifest-template",
            "--decided-by",
            "operator",
            "--decided-at",
            "2026-07-06T00:00:00Z",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["schema"] == "daily_review_lineage_repair_decision_manifest_v1"
    assert output["template_complete"] is True
    assert output["candidate_count"] == 1
    assert output["decision_count"] == 1
    assert output["decisions"][0]["decision"] == "undecided"
    assert output["decisions"][0]["decided_by"] == "operator"
    assert output["decisions"][0]["decided_at"] == "2026-07-06T00:00:00Z"
    assert output["template_context"]["lineage_repair_candidate_count"] == 1
    assert output["template_context"]["candidate_source_truncated"] is False
    assert output["operator_instructions"]["edit_field"] == "decisions[].decision"
    assert "exclude_sample" in output["operator_instructions"]["allowed_decisions"]
    assert output["guards"]["writes_archive_db"] is False
    assert output["guards"]["writes_manifest_file"] is False


def test_cli_prefills_lineage_decision_manifest_template_from_existing_manifest(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = _create_db(tmp_path, review_tables=True, fact_snapshot_id="")
    first_pass = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )
    repair_id = first_pass["lineage_repair_candidates"][0]["repair_correlation_id"]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "daily_review_lineage_repair_decision_manifest_v1",
                "decisions": [
                    {
                        "repair_correlation_id": repair_id,
                        "decision": "exclude_sample",
                        "reason": "exclude in prefilled template",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    code = batch_plan.main(
        [
            "--db",
            str(db_path),
            "--since-date",
            "2026-07-01",
            "--lineage-decision-manifest",
            str(manifest_path),
            "--emit-lineage-decision-manifest-template",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["decisions"][0]["repair_correlation_id"] == repair_id
    assert output["decisions"][0]["decision"] == "exclude_sample"
    assert output["decisions"][0]["reason"] == "exclude in prefilled template"
