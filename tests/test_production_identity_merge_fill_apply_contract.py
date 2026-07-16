import json
import sqlite3
from pathlib import Path

from scripts import production_identity_merge_fill_apply_contract as contract
from tests.test_production_identity_merge_fill_plan import _create_fixture


def test_identity_merge_fill_apply_contract_projects_safe_operations(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    conn = sqlite3.connect(db)
    payload = json.dumps(
        {
            "fact_snapshot_id": "fact_merge_fill",
            "posterior_run_id": "post_merge_fill",
            "source_event_ids": ["srcevt_merge_fill"],
        }
    )
    conn.execute(
        "UPDATE event_fact_history SET fact_snapshot_id='', correlation_id='', posterior_run_id='', source_event_id='', fact_json=? WHERE id=1",
        (payload,),
    )
    conn.commit()
    conn.close()

    report = contract.build_contract(db, limit=100, max_items_output=10)

    assert report["ok"] is True
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["operation_count"] == 1
    op = report["operations"][0]
    assert op["table"] == "event_fact_history"
    assert op["primary_key"] == {"id": 1}
    assert sorted(op["set_fields"]) == ["correlation_id", "fact_snapshot_id", "posterior_run_id", "source_event_id"]
    assert op["where_guard"]["empty_fields_required"] == [
        "correlation_id",
        "fact_snapshot_id",
        "posterior_run_id",
        "source_event_id",
    ]
    assert report["operator_contract"]["confirmation_phrase"] == contract.CONFIRMATION_PHRASE


def test_identity_merge_fill_apply_contract_blocks_duplicate_precondition(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    duplicate_payload = json.dumps(
        {
            "fact_snapshot_id": "fact_duplicate",
            "posterior_run_id": "post_duplicate",
            "source_event_ids": ["srcevt_duplicate"],
        }
    )
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE event_fact_history SET fact_snapshot_id='', correlation_id='', posterior_run_id='', source_event_id='', fact_json=? WHERE id=1",
        (duplicate_payload,),
    )
    conn.execute(
        """
        INSERT INTO event_fact_history (
            id, run_date, slug, event_date, fact_as_of, fact_json, created_at,
            correlation_id, source_event_id, fact_snapshot_id, posterior_run_id, signal_id, review_id
        )
        VALUES (2, '2026-07-06', 'slug2', '2026-07-06', '2026-07-06T01:00:00Z', ?, 'now',
            '', '', '', '', '', '')
        """,
        (duplicate_payload,),
    )
    conn.commit()
    conn.close()

    report = contract.build_contract(db, limit=100, max_items_output=10)

    assert report["ok"] is False
    assert "duplicate_fact_snapshot_precondition_not_ready" in report["blockers"]
    assert "blocked_candidates_present" in report["blockers"]
    assert report["duplicate_fact_snapshot_precondition"]["duplicate_row_count"] == 2


def test_identity_merge_fill_apply_contract_blocks_scope_truncation(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE auto_trade_decisions SET decision_id='', correlation_id='' WHERE id=1")
    conn.execute(
        """
        INSERT INTO auto_trade_decisions (
            id, run_date, slug, event_date, outcome, side, action, will_order,
            details_json, created_at, correlation_id, fact_snapshot_id, posterior_run_id,
            decision_id, signal_id, order_intent_id
        )
        VALUES (2, '2026-07-06', 'slug', '2026-07-06', '37°C', 'YES', 'entry', 0,
            '{}', 'later', '', '', '', '', '', '')
        """
    )
    conn.commit()
    conn.close()

    report = contract.build_contract(db, limit=1, max_items_output=10)

    assert report["read_mode"] == "wal_safe_read_only"
    assert report["ok"] is False
    assert "candidate_scope_truncated" in report["blockers"]
    assert report["operation_count"] == 1
    assert report["operations"][0]["table"] == "auto_trade_decisions"


def test_identity_merge_fill_apply_contract_keeps_full_operations_when_output_limited(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE auto_trade_decisions SET decision_id='', correlation_id='' WHERE id=1")
    conn.execute("UPDATE auto_trade_jobs SET order_intent_id='', correlation_id='' WHERE id=1")
    conn.commit()
    conn.close()

    report = contract.build_contract(db, limit=100, max_items_output=1)
    compact = contract.compact_contract(report, max_items_output=1)

    assert report["ok"] is True
    assert report["operation_count"] == 2
    assert len(report["operations"]) == 2
    assert compact["operation_count"] == 2
    assert len(compact["operations"]) == 1
    assert compact["operations_truncated"] is True


def test_identity_merge_fill_apply_contract_allows_noop_when_candidates_have_no_set_fields(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)

    report = contract.build_contract(db, limit=100, max_items_output=1)

    assert report["ok"] is True
    assert report["operation_count"] == 0
    assert report["next_boundary"] == "production_identity_merge_fill_not_required"


def test_identity_merge_fill_apply_contract_short_circuits_when_coverage_is_complete(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    coverage_report = {
        "schema": "production_identity_coverage_audit_v1",
        "ok": True,
        "gap_tables": [],
        "tables": {
            "auto_trade_decisions": {
                "empty_required_field_counts": {
                    "decision_id": 0,
                    "correlation_id": 0,
                }
            }
        },
    }

    report = contract.build_contract(
        db,
        coverage_report=coverage_report,
        limit=1,
        max_items_output=1,
    )

    assert report["ok"] is True
    assert report["read_mode"] == "coverage_report_short_circuit"
    assert report["operation_count"] == 0
    assert report["plan_summary"]["short_circuited"] is True
    assert report["duplicate_fact_snapshot_precondition"]["ok"] is True


def test_identity_merge_fill_apply_contract_default_coverage_short_circuits_before_plan(
    tmp_path: Path,
    monkeypatch,
):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    coverage_report = {
        "schema": "production_identity_coverage_audit_v1",
        "ok": True,
        "gap_tables": [],
        "tables": {
            "auto_trade_decisions": {
                "empty_required_field_counts": {
                    "decision_id": 0,
                    "correlation_id": 0,
                }
            }
        },
    }
    monkeypatch.setattr(
        contract.merge_plan.production_identity_coverage_audit,
        "audit",
        lambda *_args, **_kwargs: coverage_report,
    )
    monkeypatch.setattr(
        contract.merge_plan,
        "build_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("complete coverage should short-circuit before merge-fill plan")
        ),
    )

    report = contract.build_contract(db, limit=1, max_items_output=1)

    assert report["ok"] is True
    assert report["read_mode"] == "coverage_report_short_circuit"
    assert report["operation_count"] == 0
    assert report["next_boundary"] == "production_identity_merge_fill_not_required"


def test_identity_merge_fill_apply_contract_short_circuits_when_coverage_not_ready(
    tmp_path: Path,
):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    coverage_report = {
        "schema": "production_identity_coverage_gate_v1",
        "ok": False,
        "gap_tables": ["auto_trade_decisions"],
        "blockers": [
            "production_identity_coverage_not_ok",
            "production_identity_required_field_unverified_large_table",
        ],
    }

    report = contract.build_contract(
        db,
        coverage_report=coverage_report,
        limit=1,
        max_items_output=1,
    )

    assert report["ok"] is False
    assert report["read_mode"] == "coverage_report_dependency_blocked"
    assert report["operation_count"] == 0
    assert report["candidate_count"] == 0
    assert "production_identity_coverage_not_ready" in report["blockers"]
    assert report["operator_contract"]["blocked_until"] == "production_identity_coverage_ready"
