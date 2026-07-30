from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)
from scripts.checkpoint_bayes_basket_target_wal import (
    TargetWalCheckpointError,
    checkpoint_target_wal,
)


def _database(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO facts(value) VALUES('retained')")
        conn.commit()
    return path


def test_checkpoint_is_target_only_bounded_and_preserves_facts(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path / "target.db")
    receipt = synthetic_writer_quiescence_receipt(db)

    result = checkpoint_target_wal(
        db_path=db,
        quiescence_receipt=receipt,
    )

    assert result["state"] == "CHECKPOINTED"
    assert result["mode"] == "TRUNCATE"
    assert result["busy"] == 0
    assert result["wal_bytes_after"] == 0
    assert result[
        "requires_fresh_post_checkpoint_quiescence_receipt"
    ] is True
    assert result["boundary"] == {
        "target_database_only": True,
        "writer_quiescence_required": True,
        "vacuum_performed": False,
        "database_replaced": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
        "execution_mode": "production_no_live",
    }
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT value FROM facts").fetchall() == [
            ("retained",)
        ]


def test_checkpoint_rejects_receipt_for_other_database(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path / "target.db")
    other = _database(tmp_path / "other.db")

    with pytest.raises(
        TargetWalCheckpointError,
        match="writer_quiescence_receipt_database_mismatch",
    ):
        checkpoint_target_wal(
            db_path=db,
            quiescence_receipt=synthetic_writer_quiescence_receipt(other),
        )


def test_checkpoint_rejects_tampered_quiescence_receipt(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path / "target.db")
    receipt = synthetic_writer_quiescence_receipt(db)
    receipt["quiescent"] = False

    with pytest.raises(
        TargetWalCheckpointError,
        match="writer_quiescence_receipt_hash_invalid",
    ):
        checkpoint_target_wal(
            db_path=db,
            quiescence_receipt=receipt,
        )


def test_checkpoint_rejects_symlink_database(tmp_path: Path) -> None:
    db = _database(tmp_path / "target.db")
    link = tmp_path / "target-link.db"
    link.symlink_to(db)

    with pytest.raises(
        (TargetWalCheckpointError, ValueError),
        match="symlink|database_path",
    ):
        checkpoint_target_wal(
            db_path=link,
            quiescence_receipt=synthetic_writer_quiescence_receipt(db),
        )
