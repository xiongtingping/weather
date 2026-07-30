from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from bayes_basket_target.migration_quiescence import (
    build_writer_quiescence_receipt,
    isolated_target_writer_quiescence_receipt,
    synthetic_writer_quiescence_receipt,
    transaction_fenced_writer_quiescence_receipt,
    validate_writer_quiescence_receipt,
)
import bayes_basket_target.migration_quiescence as migration_quiescence


def test_synthetic_quiescence_receipt_is_content_addressed_and_validates(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.touch()
    receipt = synthetic_writer_quiescence_receipt(db)
    assert receipt["quiescent"] is True
    assert validate_writer_quiescence_receipt(receipt, db_path=db)["quiescent"] is True


def test_quiescence_receipt_tampering_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.touch()
    receipt = synthetic_writer_quiescence_receipt(db)
    receipt["writer_process_hits"] = ["unexpected-writer"]
    with pytest.raises(ValueError, match="hash_invalid"):
        validate_writer_quiescence_receipt(receipt, db_path=db)


def test_quiescence_receipt_expires_after_bounded_window(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.touch()
    receipt = synthetic_writer_quiescence_receipt(db)
    observed = datetime.fromisoformat(
        receipt["observed_at_utc"].replace("Z", "+00:00")
    )
    with pytest.raises(ValueError, match="receipt_expired"):
        validate_writer_quiescence_receipt(
            receipt,
            db_path=db,
            now_utc=observed + timedelta(seconds=121),
        )


def test_quiescence_receipt_rejects_database_file_drift(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.touch()
    receipt = synthetic_writer_quiescence_receipt(db)
    db.write_bytes(b"changed-after-probe")
    with pytest.raises(ValueError, match="database_identity_mismatch"):
        validate_writer_quiescence_receipt(
            receipt,
            db_path=db,
            now_utc=datetime.now(timezone.utc),
            require_current_database_identity=True,
        )


def test_probe_can_run_without_launchd_mutation(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.touch()
    receipt = build_writer_quiescence_receipt(db_path=db, probe_launchd=False)
    assert receipt["boundary"]["read_only_probe"] is True
    assert receipt["boundary"]["stops_services"] is False
    assert receipt["boundary"]["writes_production_db"] is False


def test_isolated_target_receipt_does_not_block_on_unrelated_resident_writers(tmp_path: Path) -> None:
    db = tmp_path / "bayes_basket_target_ssot.db"
    db.touch()
    receipt = isolated_target_writer_quiescence_receipt(db)
    assert receipt["probe_mode"] == "isolated_target_database_scope"
    assert receipt["quiescent"] is True
    assert receipt["writer_process_hits"] == []
    assert validate_writer_quiescence_receipt(receipt, db_path=db)["quiescent"] is True


def test_isolated_target_receipt_detects_open_target_database_handle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "bayes_basket_target_ssot.db"
    db.touch()

    def fake_probe(command: list[str]) -> tuple[bool, str]:
        if command[0].endswith("lsof"):
            return True, f"p42\ncPython\nf7\nn{db}\n"
        return False, ""

    monkeypatch.setattr(migration_quiescence, "_probe", fake_probe)
    receipt = isolated_target_writer_quiescence_receipt(db)
    assert receipt["quiescent"] is False
    assert receipt["writer_process_hits"] == ["Python:42"]
    assert receipt["boundary"]["target_database_handles_absent"] is False


def test_transaction_fence_allows_resident_processes_but_blocks_db_writers(
    tmp_path: Path,
) -> None:
    db = tmp_path / "bayes_basket_target_ssot.db"
    with sqlite3.connect(db, isolation_level=None) as conn:
        conn.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY)")
        conn.execute("BEGIN IMMEDIATE")
        receipt = transaction_fenced_writer_quiescence_receipt(
            conn,
            db_path=db,
        )
        assert receipt["probe_mode"] == "sqlite_begin_immediate_fence"
        assert receipt["boundary"]["stops_services"] is False
        assert validate_writer_quiescence_receipt(
            receipt,
            db_path=db,
            require_current_database_identity=True,
        )["quiescent"] is True
        conn.rollback()

    with sqlite3.connect(db, isolation_level=None) as contender:
        contender.execute("BEGIN IMMEDIATE")
        contender.rollback()


def test_transaction_fence_receipt_requires_owned_transaction(
    tmp_path: Path,
) -> None:
    db = tmp_path / "bayes_basket_target_ssot.db"
    with sqlite3.connect(db, isolation_level=None) as conn:
        conn.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY)")
        with pytest.raises(
            ValueError,
            match="transaction_fence_missing",
        ):
            transaction_fenced_writer_quiescence_receipt(
                conn,
                db_path=db,
            )


def test_probe_detects_writer_process_without_line_identity_dependency(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    db.touch()

    def fake_probe(command: list[str]) -> tuple[bool, str]:
        if command[0].endswith("pgrep") and command[-1] == "clob_order_service.mjs":
            return True, "12345 node scripts/clob_order_service.mjs"
        return False, ""

    monkeypatch.setattr(migration_quiescence, "_probe", fake_probe)
    receipt = build_writer_quiescence_receipt(db_path=db, probe_launchd=False)
    assert receipt["quiescent"] is False
    assert receipt["writer_process_hits"] == ["clob_order_service.mjs:12345"]
