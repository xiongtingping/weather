"""The resident records who holds the SSOT while its WAL climbs toward the
writer's bound (2026-10-16): each storm so far was over before anyone could
look, and blaming whatever ran at the same time named the wrong process."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _daemon():
    spec = importlib.util.spec_from_file_location(
        "_wal_forensics_daemon", ROOT / "scripts" / "weather_basket_yes_daemon.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_record_names_holders_the_checkpoint_and_our_own_statements(tmp_path) -> None:
    daemon = _daemon()
    db = tmp_path / "ssot.db"
    holder = sqlite3.connect(db)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("CREATE TABLE t(x)")
    holder.commit()
    out = tmp_path / "forensics.jsonl"
    record = daemon.capture_ssot_wal_forensics(
        db, wal_bytes=123, probe=lambda: {"in_flight": [{"sql": "SELECT 1"}]},
        out_path=out,
    )
    assert record["wal_bytes"] == 123
    assert any("python" in line.lower() for line in record["holders"]), record
    assert len(record["passive_checkpoint"]) == 3
    assert record["own_statements"] == {"in_flight": [{"sql": "SELECT 1"}]}
    assert json.loads(out.read_text().splitlines()[-1])["wal_bytes"] == 123
    holder.close()


def test_the_file_is_bounded(tmp_path, monkeypatch) -> None:
    daemon = _daemon()
    monkeypatch.setattr(daemon, "WAL_FORENSICS_MAX_LINES", 3)
    db = tmp_path / "ssot.db"
    sqlite3.connect(db).close()
    out = tmp_path / "forensics.jsonl"
    for i in range(5):
        daemon.capture_ssot_wal_forensics(db, wal_bytes=i, out_path=out)
    assert [json.loads(l)["wal_bytes"] for l in out.read_text().splitlines()] == [2, 3, 4]


def test_the_read_pins_name_the_process_on_an_old_snapshot(tmp_path) -> None:
    import subprocess
    import sys
    import time

    daemon = _daemon()
    db = tmp_path / "ssot.db"
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t(x)")
    writer.execute("INSERT INTO t VALUES (1)")
    reader = subprocess.Popen(
        [sys.executable, "-c",
         "import sqlite3,sys,time\n"
         f"c=sqlite3.connect({str(db)!r}, isolation_level=None)\n"
         "c.execute('BEGIN'); c.execute('SELECT COUNT(*) FROM t').fetchone()\n"
         "print('ready', flush=True); time.sleep(30)\n"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert reader.stdout.readline().strip() == "ready"
        for i in range(50):  # frames past the reader's snapshot
            writer.execute("INSERT INTO t VALUES (?)", (i,))
        pins = daemon.ssot_wal_read_pins(db)
        held = [s for s in pins["slots"] if s["holder_pid"] == reader.pid]
        assert held, pins
        assert held[0]["mark"] < pins["mx_frame"], pins
    finally:
        reader.kill()
        reader.wait()
        writer.close()
