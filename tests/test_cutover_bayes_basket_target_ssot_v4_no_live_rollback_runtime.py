from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_bayes_basket_target_ssot_v4_no_live.sh"


def _rollback_functions() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("restore_sqlite_database() {")
    end = text.index("\nwrite_cutover_failure_receipt() {", start)
    functions = text[start:end]
    return functions.replace(
        'sudo -u "$RUN_USER" "$PY"',
        '"$PY"',
    )


def _run_harness(
    tmp_path: Path,
    command: str,
) -> subprocess.CompletedProcess[str]:
    harness = tmp_path / "rollback_harness.sh"
    harness.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        f"PY={sys.executable!s}\n"
        f"ROOT={tmp_path!s}\n"
        f"{_rollback_functions()}\n"
        f"{command}\n",
        encoding="utf-8",
    )
    return subprocess.run(
        ["/bin/bash", str(harness)],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _value(path: Path) -> str:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        return str(connection.execute("SELECT value FROM proof").fetchone()[0])


def test_runtime_restore_replaces_mutated_sqlite_with_recovery_image(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    for path, value in ((source, "original"), (destination, "mutated")):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE proof(value TEXT NOT NULL)")
            connection.execute("INSERT INTO proof VALUES(?)", (value,))

    completed = _run_harness(
        tmp_path,
        (
            f'restore_sqlite_database "{source}" '
            f'"{destination}" 268435456'
        ),
    )

    assert completed.returncode == 0, completed.stderr
    assert _value(destination) == "original"
    assert not Path(str(destination) + "-wal").exists()
    assert not Path(str(destination) + "-shm").exists()


def test_runtime_restore_rejects_corrupt_image_without_changing_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    source.write_bytes(b"not sqlite")
    with sqlite3.connect(destination) as connection:
        connection.execute("CREATE TABLE proof(value TEXT NOT NULL)")
        connection.execute("INSERT INTO proof VALUES('unchanged')")

    completed = _run_harness(
        tmp_path,
        (
            f'restore_sqlite_database "{source}" '
            f'"{destination}" 268435456'
        ),
    )

    assert completed.returncode != 0
    assert _value(destination) == "unchanged"


def test_runtime_hard_no_rollback_restores_old_table(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    database = data / "archive.db"
    backup = data / "hard_no.backup.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE auto_trade_decisions("
            "decision_id TEXT PRIMARY KEY, settlement_timezone TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE "
            "auto_trade_decisions_legacy_no_settlement_timezone_v1("
            "decision_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO "
            "auto_trade_decisions_legacy_no_settlement_timezone_v1 "
            "VALUES('legacy')"
        )
    with sqlite3.connect(backup) as connection:
        connection.execute(
            "CREATE TABLE auto_trade_decisions_backup("
            "decision_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO auto_trade_decisions_backup VALUES('legacy')"
        )
        connection.execute(
            "CREATE TABLE backup_metadata("
            "key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO backup_metadata VALUES('row_count','1')"
        )

    completed = _run_harness(
        tmp_path,
        f'restore_hard_no_timezone_table "{backup}"',
    )

    assert completed.returncode == 0, completed.stderr
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        columns = [
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(auto_trade_decisions)"
            )
        ]
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert columns == ["decision_id"]
        assert (
            "auto_trade_decisions_legacy_no_settlement_timezone_v1"
            not in names
        )
        assert connection.execute(
            "SELECT decision_id FROM auto_trade_decisions"
        ).fetchone()[0] == "legacy"
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
