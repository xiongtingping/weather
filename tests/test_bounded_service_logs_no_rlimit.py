"""A service launcher must never bound logs with a process-wide rlimit.

`ulimit -f` sets RLIMIT_FSIZE, which the exec'd Python/Node child inherits and
which applies to every file the service writes -- including SQLite's WAL for
data/archive.db.  With the launcher's old `ulimit -f 131072` (bash on macOS
counts 1024-byte blocks) the resident ran with a 134217728-byte cap; the WAL
grew to exactly that size and every subsequent write failed with
``disk I/O error``, stalling canonical AWC ingress for a full event day.
"""

import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "lib" / "bounded_service_logs.sh"
LAUNCHERS = (
    ROOT / "scripts" / "weather_basket_yes_daemon.sh",
    ROOT / "scripts" / "clob_order_service.sh",
    ROOT / "scripts" / "market_ws_listener.sh",
)


def _executable_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def test_helper_sets_no_file_size_rlimit() -> None:
    for line in _executable_lines(HELPER):
        assert not re.search(r"\bulimit\s+-f\b", line), (
            f"bounded_service_logs.sh must not set RLIMIT_FSIZE: {line!r}"
        )


@pytest.mark.parametrize("launcher", LAUNCHERS, ids=lambda p: p.name)
def test_no_launcher_sets_a_file_size_rlimit(launcher: Path) -> None:
    for line in _executable_lines(launcher):
        assert not re.search(r"\bulimit\s+-f\b", line), (
            f"{launcher.name} must not set RLIMIT_FSIZE: {line!r}"
        )


def test_helper_still_truncates_logs_at_launch(tmp_path: Path) -> None:
    root = tmp_path
    (root / "data").mkdir()
    log = root / "data" / "service.log"
    log.write_text("stale content from the previous run\n", encoding="utf-8")

    script = textwrap.dedent(f"""
        ROOT={root}
        . {HELPER}
        weather_prepare_bounded_service_logs "$ROOT/data/service.log"
    """)
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    # The launch-time boundary must survive removal of the rlimit.
    assert log.stat().st_size == 0


def test_helper_rejects_paths_outside_the_repo_data_dir(tmp_path: Path) -> None:
    outside = tmp_path / "not_a_service.log"
    outside.write_text("keep me", encoding="utf-8")

    script = textwrap.dedent(f"""
        ROOT={tmp_path}/repo
        . {HELPER}
        weather_prepare_bounded_service_logs "{outside}"
    """)
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)

    assert result.returncode != 0
    assert outside.read_text(encoding="utf-8") == "keep me"


@pytest.mark.skipif(shutil.which("/bin/bash") is None, reason="bash required")
def test_a_file_size_rlimit_would_break_the_sqlite_wal(tmp_path: Path) -> None:
    """Pin the mechanism itself, so the reason for the ban stays legible."""

    db = tmp_path / "probe.db"
    program = tmp_path / "probe.py"
    program.write_text(
        textwrap.dedent(f"""
            import sqlite3
            c = sqlite3.connect({str(db)!r}, isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA wal_autocheckpoint=0")
            c.execute("CREATE TABLE t(x)")
            try:
                for _ in range(80):
                    c.execute("INSERT INTO t VALUES(?)", ("y" * 200_000,))
                print("NO_ERROR")
            except sqlite3.OperationalError as exc:
                print("SQLITE_ERROR", exc)
        """),
        encoding="utf-8",
    )
    runner = tmp_path / "run.sh"
    # 4096 blocks is a small stand-in for the launcher's old 131072.
    runner.write_text(
        f'ulimit -f 4096\nexec "{sys.executable}" "{program}"\n', encoding="utf-8"
    )
    result = subprocess.run(["/bin/bash", str(runner)], capture_output=True, text=True)

    assert "SQLITE_ERROR" in result.stdout, result.stdout + result.stderr
    assert "disk I/O error" in result.stdout
