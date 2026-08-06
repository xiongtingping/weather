from pathlib import Path

from scripts import weather_basket_yes_daemon as daemon


def _archive(tmp_path: Path, *, wal_bytes: int) -> Path:
    db_path = tmp_path / "archive.db"
    db_path.write_bytes(b"\x00")
    Path(str(db_path) + "-wal").write_bytes(b"\x00" * wal_bytes)
    return db_path


def test_growing_wal_warns_without_blocking(tmp_path: Path) -> None:
    db_path = _archive(tmp_path, wal_bytes=daemon.WARN_ARCHIVE_WAL_BYTES + 1)

    guard = daemon.archive_storage_guard(db_path)

    # The warning exists so that WAL growth is visible before it becomes an I/O
    # fault.  It must not gate a lane -- only the fail-closed ceiling does that.
    assert guard["ok"] is True
    assert guard["state"] == "within_budget_low_headroom"
    assert "archive_wal_bytes_growing" in guard["warnings"]
    assert guard["violations"] == []


def test_wal_above_the_ceiling_still_fails_closed(tmp_path: Path) -> None:
    db_path = _archive(tmp_path, wal_bytes=daemon.MAX_ARCHIVE_WAL_BYTES + 1)

    guard = daemon.archive_storage_guard(db_path)

    assert guard["ok"] is False
    assert guard["state"] == "blocked_fail_closed"
    assert "archive_wal_bytes_exceeded" in guard["violations"]
    # A blocked guard reports the violation only; warnings would be noise.
    assert guard["warnings"] == []


def test_quiet_archive_reports_plain_within_budget(tmp_path: Path) -> None:
    db_path = _archive(tmp_path, wal_bytes=1024)

    guard = daemon.archive_storage_guard(db_path)

    assert guard["ok"] is True
    assert guard["violations"] == []
    # Free-space headroom depends on the real test filesystem, so assert only
    # the WAL dimension this test controls.
    assert "archive_wal_bytes_growing" not in guard["warnings"]
