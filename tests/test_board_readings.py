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


def test_seat_windows_read_again_only_the_cells_that_can_still_change(tmp_path, monkeypatch) -> None:
    """2026-10-02: re-reading 45 days of cells every hour was the job's whole
    cost, inside production's cycle.  A run reads from SEAT_REREAD_HOURS
    before the last run began and keeps every older cell as written."""

    from datetime import datetime, timezone

    from bayes_basket_target import center_width_store as store

    monkeypatch.setattr(BR, "READINGS_DIR", tmp_path)
    now = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(BR, "_now", lambda: now)
    monkeypatch.setattr(store, "_truth", lambda _conn: {})
    asked: list[str] = []
    keys = ["20C", "21C", "22C"]

    def cells(_conn, _truth, *, stage, min_decision_at_utc):
        asked.append(min_decision_at_utc)
        if stage != "P0":
            return {}
        return {"2026-10-02": [{
            "station_id": "KAAA", "event_date": "2026-10-02",
            "decision_at_utc": "2026-10-02T13:00:00Z", "ordered_keys": keys,
            "probabilities": {"20C": 0.2, "21C": 0.6, "22C": 0.2},
            "excluded_buckets": [],
        }]}

    monkeypatch.setattr(store, "serving_cells_by_day", cells)
    old_key = "KAAA|2026-09-30|2026-09-29T06:00:00Z"
    BR.write_reading("seat_windows", {"seats": {old_key: {"state": "SYMMETRIC", "sign": 0,
                     "seat_keys": ["20C", "21C", "22C"]}}},
                     code=BR._code_sha(BR.seat_window), build_sec=0.0)
    previous = BR.load_reading("seat_windows")["computed_at_utc"]
    value = BR.seat_windows(None)
    assert set(asked) == {value["reread_from_utc"]}
    assert value["reread_from_utc"] > value["floor_utc"]
    assert value["reread_from_utc"] > "2026-10-02T07"
    assert old_key in value["seats"], "an older cell is kept as written"
    assert "KAAA|2026-10-02|2026-10-02T13:00:00Z" in value["seats"]
    assert value["computed"] == 1
