import json
import sqlite3
from pathlib import Path

from scripts import auto_order_lineage_merge_fill_apply_contract as contract
from tests.test_auto_order_lineage_merge_fill_plan import _create_schema, _insert_order


def test_auto_order_lineage_apply_contract_blocks_default_unresolved_plan(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.commit()
    conn.close()

    report = contract.build_contract(db)

    assert report["ok"] is False
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert "candidate_resolution_required_before_apply_contract" in report["blockers"]
    assert report["operation_count"] == 0


def test_auto_order_lineage_apply_contract_projects_safe_operations(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(
            id, decision_id, signal_id, order_intent_id, correlation_id, source, state, created_at
        )
        VALUES(20, 'dec_job', 'sig_job', 'intent_1', 'corr_job', 'hard_source_direct', 'done', '2026-07-03T09:05:39Z')
        """
    )
    conn.commit()
    conn.close()

    report = contract.build_contract(db, resolve_candidates=True)

    assert report["ok"] is True
    assert report["operation_count"] == 1
    op = report["operations"][0]
    assert op["table"] == "auto_orders"
    assert op["primary_key"] == {"id": 1}
    assert op["set_fields"] == {
        "decision_id": {"old_value_policy": "must_be_empty", "new_value": "dec_job"},
        "signal_id": {"old_value_policy": "must_be_empty", "new_value": "sig_job"},
    }
    assert op["where_guard"]["empty_fields_required"] == ["decision_id", "signal_id"]
    assert report["operator_contract"]["confirmation_phrase"] == contract.CONFIRMATION_PHRASE
    assert report["operator_contract"]["requires_writer_quiescence"] is True


def test_auto_order_lineage_apply_contract_blocks_ambiguous_candidates(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    for row_id in (10, 11):
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
                id, decision_id, signal_id, order_intent_id, correlation_id,
                details_json, slug, event_date, outcome, side, action, created_at
            )
            VALUES(?, ?, ?, 'intent_1', 'corr', ?, 'slug', '2026-07-03', '32°C', 'YES', 'entry', '2026-07-03T09:05:39Z')
            """,
            (row_id, f"dec_{row_id}", f"sig_{row_id}", json.dumps({"order_id": "ord_1"})),
        )
    conn.commit()
    conn.close()

    report = contract.build_contract(
        db,
        resolve_candidates=True,
        enable_decision_order_id_lookup=True,
    )

    assert report["ok"] is False
    assert "blocked_candidates_present" in report["blockers"]
    assert report["operation_count"] == 0


def test_auto_order_lineage_apply_contract_blocks_scope_truncation(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn, order_id="ord_1", intent="intent_1")
    conn.execute(
        """
        INSERT INTO auto_orders(
            id, order_id, correlation_id, decision_id, signal_id, order_intent_id,
            slug, event_date, outcome, side, action, created_at
        )
        VALUES(2, 'ord_2', 'corr_order_2', '', '', 'intent_2',
            'slug', '2026-07-03', '33°C', 'YES', 'entry', '2026-07-03T09:06:40Z')
        """
    )
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(
            id, decision_id, signal_id, order_intent_id, correlation_id, source, state, created_at
        )
        VALUES(20, 'dec_job', 'sig_job', 'intent_1', 'corr_job', 'hard_source_direct', 'done', '2026-07-03T09:05:39Z')
        """
    )
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(
            id, decision_id, signal_id, order_intent_id, correlation_id, source, state, created_at
        )
        VALUES(21, 'dec_job_2', 'sig_job_2', 'intent_2', 'corr_job_2', 'hard_source_direct', 'done', '2026-07-03T09:06:39Z')
        """
    )
    conn.commit()
    conn.close()

    report = contract.build_contract(db, resolve_candidates=True, limit=1)

    assert report["ok"] is False
    assert "candidate_scope_truncated" in report["blockers"]
    assert report["operation_count"] == 1


def test_auto_order_lineage_apply_contract_compact_preserves_operation_count(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(
            id, decision_id, signal_id, order_intent_id, correlation_id, source, state, created_at
        )
        VALUES(20, 'dec_job', 'sig_job', 'intent_1', 'corr_job', 'hard_source_direct', 'done', '2026-07-03T09:05:39Z')
        """
    )
    conn.commit()
    conn.close()

    report = contract.build_contract(db, resolve_candidates=True, max_items_output=1)
    compact = contract.compact_contract(report, max_items_output=0)

    assert report["operation_count"] == 1
    assert compact["operation_count"] == 1
    assert compact["operations"] == []
    assert compact["operations_truncated"] is True


def test_auto_order_lineage_apply_contract_noops_for_ignored_legacy_scope(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.execute("UPDATE auto_orders SET created_at='2026-07-07T03:59:59+00:00' WHERE id=1")
    conn.commit()
    conn.close()

    report = contract.build_contract(
        db,
        resolve_candidates=True,
        enable_decision_order_id_lookup=True,
        ignore_legacy_orders=True,
        ignore_orders_before="2026-07-07",
    )

    assert report["ok"] is True
    assert report["operation_count"] == 0
    assert report["missing_order_count"] == 0
    assert report["plan_summary"]["legacy_ignored_count"] == 1
    assert "--ignore-orders-before 2026-07-07" in report["operator_contract"]["apply_command_template"]
    assert "--enable-decision-order-id-lookup" in report["operator_contract"]["apply_command_template"]
    assert all("2026-07-07" in command for command in report["operator_contract"]["post_apply_verification_commands"])
