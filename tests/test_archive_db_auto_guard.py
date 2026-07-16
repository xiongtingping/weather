import argparse
import sqlite3
import subprocess
from pathlib import Path

import pytest

from scripts import archive_db_auto_guard as guard


def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t(value) VALUES ('x')")
        conn.commit()


def test_guard_blocks_compact_build_when_candidate_disk_preflight_fails(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    rebuild_dir = tmp_path / "archive_rebuilds"
    _make_db(db)

    monkeypatch.setattr(guard, "DB", db)
    monkeypatch.setattr(guard, "REBUILD_DIR", rebuild_dir)
    monkeypatch.setenv("PM_ARCHIVE_GUARD_MAX_DB_BYTES", "1")
    monkeypatch.setenv("PM_ARCHIVE_GUARD_MIN_BUILD_FREE_BYTES", str(10**18))

    result = guard.run_guard(
        argparse.Namespace(
            force=False,
            candidate="",
            skip_sync_delta=False,
        )
    )

    assert result["ok"] is True
    assert result["mode"] == "compact_cutover"
    assert result["blocked"] == "insufficient_disk_for_candidate_build"
    assert result["skipped"] == "insufficient_disk_for_candidate_build"
    assert result["next_boundary"] == "free_disk_or_move_backups_before_archive_compact_candidate_build"
    assert result["candidate_build_disk_preflight"]["ok"] is False
    assert list(rebuild_dir.glob("archive.compact_guard_*.db*")) == []


def test_build_candidate_removes_partial_output_on_failure(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    rebuild_dir = tmp_path / "archive_rebuilds"
    report_dir = tmp_path / "reports"
    _make_db(db)

    monkeypatch.setattr(guard, "DB", db)
    monkeypatch.setattr(guard, "REBUILD_DIR", rebuild_dir)
    monkeypatch.setattr(guard, "REPORT_DIR", report_dir)

    def fake_run(cmd, *, timeout=60, check=False):
        output = Path(cmd[cmd.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("partial")
        Path(str(output) + "-wal").write_text("partial wal")
        return subprocess.CompletedProcess(cmd, 2, "", "failed")

    monkeypatch.setattr(guard, "_run", fake_run)

    with pytest.raises(RuntimeError, match="build_candidate_failed"):
        guard.build_candidate("20260707T000000Z")

    assert list(rebuild_dir.glob("archive.compact_guard_*.db*")) == []
