import sqlite3
from pathlib import Path

from scripts import weather_basket_yes_daemon as daemon


def _wal_database(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE fact(id INTEGER PRIMARY KEY, payload TEXT)")
    return db_path, conn


def test_truncating_checkpoint_returns_the_wal_to_zero(tmp_path: Path) -> None:
    db_path, conn = _wal_database(tmp_path)
    conn.executemany(
        "INSERT INTO fact(payload) VALUES(?)",
        [("x" * 512,) for _ in range(2000)],
    )
    wal = Path(str(db_path) + "-wal")
    assert wal.stat().st_size > 0

    result = daemon.archive_wal_checkpoint_maintenance(conn)

    assert result["ok"] is True
    assert result["state"] == "checkpointed"
    assert result["mode"] == "TRUNCATE"
    assert result["busy"] == 0
    # A passive checkpoint would leave the file at its high-water mark.  The
    # whole point of this maintenance is to reclaim the space.
    assert wal.stat().st_size == 0
    conn.close()


def test_a_busy_reader_is_reported_not_raised(tmp_path: Path) -> None:
    db_path, conn = _wal_database(tmp_path)
    conn.execute("INSERT INTO fact(payload) VALUES('pinned')")

    reader = sqlite3.connect(db_path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM fact").fetchone()
    try:
        result = daemon.archive_wal_checkpoint_maintenance(conn)
    finally:
        reader.close()
        conn.close()

    # Readers pinning an old snapshot are the normal steady state for this
    # daemon.  Maintenance must degrade to a retry, never to a stalled lane.
    assert result["busy"] == 1
    assert result["ok"] is False
    assert result["state"] == "busy_readers_retry_next_interval"


def test_checkpoint_failure_is_contained(tmp_path: Path) -> None:
    db_path, conn = _wal_database(tmp_path)
    conn.close()

    # A closed connection stands in for any SQLite-level fault: WAL maintenance
    # must never become a new way to stop ingest.
    result = daemon.archive_wal_checkpoint_maintenance(conn)

    assert result["ok"] is False
    assert result["state"] == "checkpoint_failed"
    assert "ProgrammingError" in result["reason"]
