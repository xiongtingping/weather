import sqlite3
from pathlib import Path

from scripts import auto_order_lineage_merge_fill_apply as apply_script
from tests.test_auto_order_lineage_merge_fill_plan import _create_schema, _insert_order


def _backup(db: Path, backup: Path) -> None:
    source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dest = sqlite3.connect(backup)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()


def _order_identity(db: Path, row_id: int = 1) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT decision_id, signal_id FROM auto_orders WHERE id=?",
        (row_id,),
    ).fetchone()
    conn.close()
    return dict(row)


def _seed_job_anchor(db: Path, *, intent: str = "intent_1", decision_id: str = "dec_job", signal_id: str = "sig_job") -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(
            id, decision_id, signal_id, order_intent_id, correlation_id, source, state, created_at
        )
        VALUES(20, ?, ?, ?, 'corr_job', 'hard_source_direct', 'done', '2026-07-03T09:05:39Z')
        """,
        (decision_id, signal_id, intent),
    )
    conn.commit()
    conn.close()


def test_auto_order_lineage_apply_default_no_apply_does_not_write(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.commit()
    conn.close()
    _seed_job_anchor(db)

    result = apply_script.build_cli_result(
        db_path=db,
        backup_path=None,
        apply=False,
        confirm="",
        writer_quiescent=False,
        limit=100,
        max_items_output=10,
        resolve_candidates=True,
        enable_decision_order_id_lookup=False,
        max_small_table_scan_rows=5000,
        ignore_legacy_orders=False,
        order_since_hours=0.0,
        ignore_orders_before="",
    )

    assert result["ok"] is True
    assert result["applied"] is False
    assert result["read_only"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["contract"]["operation_count"] == 1
    assert _order_identity(db) == {"decision_id": "", "signal_id": ""}


def test_auto_order_lineage_apply_blocks_without_confirmation(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.commit()
    conn.close()
    _seed_job_anchor(db)

    result = apply_script.build_cli_result(
        db_path=db,
        backup_path=None,
        apply=True,
        confirm="wrong",
        writer_quiescent=True,
        limit=100,
        max_items_output=10,
        resolve_candidates=True,
        enable_decision_order_id_lookup=False,
        max_small_table_scan_rows=5000,
        ignore_legacy_orders=False,
        order_since_hours=0.0,
        ignore_orders_before="",
        maintenance_packet={"ok": True, "writer_quiescent": True},
    )

    assert result["ok"] is False
    assert result["block_reason"] == "confirmation_phrase_missing_or_mismatched"


def test_auto_order_lineage_apply_blocks_when_maintenance_not_quiescent(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.commit()
    conn.close()
    _seed_job_anchor(db)
    backup = tmp_path / "archive.bak"
    _backup(db, backup)

    result = apply_script.build_cli_result(
        db_path=db,
        backup_path=backup,
        apply=True,
        confirm=apply_script.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        limit=100,
        max_items_output=10,
        resolve_candidates=True,
        enable_decision_order_id_lookup=False,
        max_small_table_scan_rows=5000,
        ignore_legacy_orders=False,
        order_since_hours=0.0,
        ignore_orders_before="",
        maintenance_packet={"ok": False, "writer_quiescent": False, "blockers": ["writer"]},
    )

    assert result["ok"] is False
    assert result["block_reason"] == "maintenance_window_not_quiescent"


def test_auto_order_lineage_apply_updates_empty_fields_in_transaction(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.commit()
    conn.close()
    _seed_job_anchor(db)
    backup = tmp_path / "archive.bak"
    _backup(db, backup)

    result = apply_script.build_cli_result(
        db_path=db,
        backup_path=backup,
        apply=True,
        confirm=apply_script.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        limit=100,
        max_items_output=10,
        resolve_candidates=True,
        enable_decision_order_id_lookup=False,
        max_small_table_scan_rows=5000,
        ignore_legacy_orders=False,
        order_since_hours=0.0,
        ignore_orders_before="",
        maintenance_packet={"ok": True, "writer_quiescent": True, "blockers": []},
    )

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["apply_result"]["operation_count"] == 1
    assert result["guards"]["writes_auto_orders"] is True
    assert _order_identity(db) == {"decision_id": "dec_job", "signal_id": "sig_job"}


def test_auto_order_lineage_apply_rolls_back_when_guard_mismatch(tmp_path: Path) -> None:
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
        VALUES
            (20, 'dec_job_1', 'sig_job_1', 'intent_1', 'corr_job_1', 'hard_source_direct', 'done', '2026-07-03T09:05:39Z'),
            (21, 'dec_job_2', 'sig_job_2', 'intent_2', 'corr_job_2', 'hard_source_direct', 'done', '2026-07-03T09:06:39Z')
        """
    )
    conn.commit()
    conn.close()
    backup = tmp_path / "archive.bak"
    _backup(db, backup)

    contract = apply_script.apply_contract.build_contract(db, resolve_candidates=True)
    # Simulate a concurrent writer filling the second row after contract projection.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE auto_orders SET decision_id='already_filled' WHERE id=2")
    conn.commit()
    conn.close()

    with apply_script._connect_rw(db) as conn:
        apply_result = apply_script.apply_auto_order_lineage_merge_fill_contract(conn, contract)

    assert apply_result["ok"] is False
    assert apply_result["block_reason"] == "operation_failed"
    assert _order_identity(db, 1) == {"decision_id": "", "signal_id": ""}
    assert _order_identity(db, 2) == {"decision_id": "already_filled", "signal_id": ""}
