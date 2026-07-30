from __future__ import annotations

import sqlite3

from scripts.reported_trajectory_no_daemon import _open


def test_daemon_open_uses_named_rows(tmp_path) -> None:
    conn = _open(tmp_path / "archive.db")
    try:
        row = conn.execute("SELECT 1 AS sample").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["sample"] == 1
    finally:
        conn.close()
