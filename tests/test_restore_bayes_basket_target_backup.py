from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.restore_bayes_basket_target_backup import (
    TargetBackupRestoreError,
    restore_target_backup,
)


def _database(path: Path, values: list[str]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE proof("
            "ordinal INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO proof(ordinal, value) VALUES(?, ?)",
            enumerate(values),
        )


def _values(path: Path) -> list[str]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT value FROM proof ORDER BY ordinal"
            )
        ]


def test_restore_replaces_mutated_target_with_exact_backup(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    _database(backup, ["original-a", "original-b"])
    _database(target, ["mutated-a", "mutated-b"])

    receipt = restore_target_backup(
        backup_path=backup,
        db_path=target,
    )

    assert receipt["state"] == "RESTORED_EXACT"
    assert receipt["quick_check"] == "ok"
    assert receipt["wal_bytes_after"] == 0
    assert receipt["calls_order_gateway"] is False
    assert receipt["live_submit_authorized"] is False
    assert receipt["orders_submitted"] == 0
    assert _values(target) == ["original-a", "original-b"]


def test_restore_distinguishes_same_row_count_different_content(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    _database(backup, ["cold", "warm"])
    _database(target, ["wrong", "values"])

    receipt = restore_target_backup(
        backup_path=backup,
        db_path=target,
    )

    assert receipt["logical_fingerprint"]["line_count"] > 0
    assert _values(target) == ["cold", "warm"]


def test_restore_rejects_corrupt_backup_without_mutating_target(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    backup.write_bytes(b"not-a-sqlite-database")
    _database(target, ["retained"])

    with pytest.raises(
        TargetBackupRestoreError,
        match="source_quick_check_failed|sqlite_failed",
    ):
        restore_target_backup(
            backup_path=backup,
            db_path=target,
        )

    assert _values(target) == ["retained"]


def test_restore_rejects_cross_directory_and_symlink_paths(
    tmp_path: Path,
) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    backup = left / "backup.db"
    target = right / "target.db"
    _database(backup, ["original"])
    _database(target, ["target"])

    with pytest.raises(
        TargetBackupRestoreError,
        match="path_invalid",
    ):
        restore_target_backup(
            backup_path=backup,
            db_path=target,
        )

    linked = left / "linked.db"
    linked.symlink_to(backup)
    left_target = left / "target.db"
    _database(left_target, ["target"])
    with pytest.raises(
        TargetBackupRestoreError,
        match="path_invalid",
    ):
        restore_target_backup(
            backup_path=linked,
            db_path=left_target,
        )
