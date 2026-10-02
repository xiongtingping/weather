"""The backend board-readings job (user rulings 2026-10-02 「只读是读后端算出
来的」 / 「新开一个后端读数任务」): it writes what the board shows, reads the
store read-only, and a reading that fails keeps its previous file."""

from __future__ import annotations

import json
import sqlite3

from bayes_basket_target import board_readings as BR


def _db(tmp_path):
    path = tmp_path / "ssot.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    return path


def test_a_reading_round_trips_and_a_foreign_file_is_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(BR, "READINGS_DIR", tmp_path)
    BR.write_reading("l4_lane", {"forward": {}}, code="c", build_sec=2.0)
    got = BR.load_reading("l4_lane")
    assert got["value"] == {"forward": {}} and got["code_sha256"] == "c"
    assert got["computed_at_utc"].endswith("Z")
    (tmp_path / "centre_shape.json").write_text(json.dumps({"schema": "other"}))
    assert BR.load_reading("centre_shape") is None
    assert BR.load_reading("seat_windows") is None


def test_the_store_is_opened_read_only_and_a_failure_keeps_the_last_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(BR, "READINGS_DIR", tmp_path / "readings")
    db = _db(tmp_path)

    def writes(conn):
        conn.execute("INSERT INTO t VALUES (1)")
        return {"never": True}

    def reads(conn):
        return {"rows": conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]}

    monkeypatch.setitem(BR._READINGS, "l4_lane", (reads, (reads,)))
    assert BR.run(db, ["l4_lane"])[0].startswith("l4_lane ")
    monkeypatch.setitem(BR._READINGS, "l4_lane", (writes, (writes,)))
    receipt = BR.run(db, ["l4_lane"])[0]
    assert receipt.startswith("l4_lane failed: OperationalError"), receipt
    assert BR.load_reading("l4_lane")["value"] == {"rows": 0}, "the last good file stays"
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
