"""Charter 284: the rebuilt SSOT is INCREMENTAL auto_vacuum and the cold
offload hands freed pages back after each batch."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts import rebuild_ssot_compact as rebuild
from scripts import ssot_cold_offload as offload


def _source(path: Path, rows: int = 400) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, payload TEXT)")
    conn.execute("CREATE INDEX t_payload ON t(payload)")
    conn.executemany("INSERT INTO t(payload) VALUES(?)", [("x" * 2000,) for _ in range(rows)])
    conn.commit()
    conn.close()


def test_build_creates_incremental_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(rebuild, "_residents_running", lambda: [])
    src, dst = tmp_path / "src.db", tmp_path / "dst.db"
    _source(src)
    built = rebuild.build(source=src, target=dst)
    assert built["state"] == "BUILT"
    conn = sqlite3.connect(str(dst))
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == rebuild.AUTO_VACUUM_CODE
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 400
    conn.close()
    checked = rebuild.verify(source=src, target=dst)
    assert checked["auto_vacuum"] == rebuild.AUTO_VACUUM_CODE


def test_return_free_pages_shrinks_incremental_store(tmp_path: Path) -> None:
    db = tmp_path / "inc.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, payload TEXT)")
    conn.executemany("INSERT INTO t(payload) VALUES(?)", [("x" * 2000,) for _ in range(400)])
    before = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.execute("DELETE FROM t WHERE id > 40")
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] > 0
    offload._return_free_pages(conn)
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert conn.execute("PRAGMA page_count").fetchone()[0] < before / 2
    conn.close()


def test_return_free_pages_leaves_non_incremental_store(tmp_path: Path) -> None:
    db = tmp_path / "none.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, payload TEXT)")
    conn.executemany("INSERT INTO t(payload) VALUES(?)", [("x" * 2000,) for _ in range(100)])
    conn.execute("DELETE FROM t")
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    offload._return_free_pages(conn)
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == free
    conn.close()
