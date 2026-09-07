from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from scripts import archive_db_wal_keeper_audit


ROOT = Path(__file__).resolve().parents[1]
KEEPER = ROOT / "scripts" / "archive_db_wal_keeper.py"
AUDIT = ROOT / "scripts" / "archive_db_wal_keeper_audit.py"


def _wait_for_state(path: Path, state: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    value: dict = {}
    while time.monotonic() < deadline:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
        if value.get("state") == state:
            return value
        time.sleep(0.05)
    raise AssertionError(f"keeper state did not become {state}: {value}")


def test_read_only_keeper_prevents_last_writer_close_from_removing_wal(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    status = tmp_path / "keeper.json"
    setup = sqlite3.connect(db)
    assert setup.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    setup.execute("CREATE TABLE sample(value INTEGER)")
    setup.commit()
    setup.close()

    process = subprocess.Popen(
        [
            sys.executable,
            str(KEEPER),
            "--db",
            str(db),
            "--status",
            str(status),
            "--heartbeat-sec",
            "0.2",
        ]
    )
    try:
        running = _wait_for_state(status, "running")
        assert running["read_only"] is True
        assert running["writes_archive_db"] is False
        audited = subprocess.run(
            [sys.executable, str(AUDIT), "--db", str(db), "--status", str(status)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert audited.returncode == 0, audited.stdout + audited.stderr
        assert json.loads(audited.stdout)["archive_handle_access_modes"] == ["r"]
        writer = sqlite3.connect(db)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO sample VALUES(1)")
        writer.commit()
        writer.close()
        assert db.with_name(db.name + "-wal").exists()
        reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            assert reader.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 1
        finally:
            reader.close()
    finally:
        process.terminate()
        process.wait(timeout=5)


# Retired 2026-09-07 with the script it asserted on.
#
# test_monitor_supervises_keeper_before_amsc_children_and_cleans_it_up read
# scripts/polymarket_monitor_daemon.sh and checked the keeper was started
# before the AMSC watchers and killed in cleanup.  That script was deleted
# on 2026-08-04 and nothing supervises the keeper from a shell any more --
# no .sh in the tree mentions start_archive_wal_keeper, and no launch agent
# runs the monitor.  The test could only ever raise FileNotFoundError, which
# is a red that says nothing about the code, so it goes with the component
# rather than being kept as a monument to it.


def test_audit_reports_cross_user_inspection_permission_without_claiming_pid_died(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = tmp_path / "archive.db"
    db.touch()
    status = tmp_path / "keeper.json"
    now = archive_db_wal_keeper_audit.datetime.now(
        archive_db_wal_keeper_audit.timezone.utc
    ).isoformat().replace("+00:00", "Z")
    status.write_text(
        json.dumps(
            {
                "schema": "archive_db_wal_read_keeper_v1",
                "state": "running",
                "db": str(db),
                "pid": 12345,
                "heartbeat_at": now,
                "read_only": True,
                "writes_archive_db": False,
                "holds_transaction": False,
                "places_orders": False,
            }
        ),
        encoding="utf-8",
    )

    def deny_pid_inspection(pid: int, signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(archive_db_wal_keeper_audit.os, "kill", deny_pid_inspection)
    monkeypatch.setattr(
        archive_db_wal_keeper_audit, "_lsof_access_modes", lambda pid, path: []
    )
    rc = archive_db_wal_keeper_audit.main(
        ["--db", str(db), "--status", str(status)]
    )
    result = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert result["pid_inspection_permission_denied"] is True
    assert "wal_keeper_pid_not_alive" not in result["blockers"]
    assert result["blockers"] == [
        "wal_keeper_archive_handle_inspection_permission_denied"
    ]
