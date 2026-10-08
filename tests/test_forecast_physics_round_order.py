"""A forward round reaches the stalest stations first and hands its receipt back.

2026-10-08 03:31Z: the resident's 300 s kill came after 19 of 48 stations on
a loaded host, the receipt went with it, and since every round started at
CYYZ the 29 stations from LFPB on still showed the 10-07 09:31Z forecast.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture_station_forecast_physics.py"


def _module():
    spec = importlib.util.spec_from_file_location("capture_physics_round_order", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(module, db: Path, last_forward: dict[str, str]) -> None:
    conn = sqlite3.connect(db)
    module.ensure_schema(conn)
    conn.execute(
        "CREATE TABLE calibration_station_market_timezone_binding"
        "(station_id TEXT, settlement_timezone TEXT)"
    )
    for station in ("AAAA", "BBBB", "CCCC", "DDDD"):
        conn.execute(
            "INSERT INTO calibration_station_market_timezone_binding VALUES(?,?)",
            (station, "UTC"),
        )
    for station, stamp in last_forward.items():
        conn.execute(
            f"INSERT INTO {module.TABLE} VALUES"
            "(?,?,?,1,1,1,0,0,0,'UTC','forward',?,1,'x')",
            (station, "2026-10-07", module.SOURCE_ID, stamp),
        )
    conn.commit()
    conn.close()


def test_never_captured_then_oldest_first_and_ties_keep_their_order(tmp_path):
    module = _module()
    db = tmp_path / "ssot.db"
    _seed(module, db, {"AAAA": "2026-10-08T03:32Z", "BBBB": "2026-10-07T09:31Z",
                       "DDDD": "2026-10-07T09:31Z"})
    conn = sqlite3.connect(db)
    assert module.stalest_first(conn, ["AAAA", "BBBB", "CCCC", "DDDD"]) == [
        "CCCC", "BBBB", "DDDD", "AAAA"]


def test_the_round_stops_at_its_deadline_and_reports_what_it_did_not_reach(
    tmp_path, monkeypatch, capsys
):
    module = _module()
    db = tmp_path / "ssot.db"
    _seed(module, db, {"AAAA": "2026-10-08T03:32Z", "BBBB": "2026-10-07T09:31Z"})
    fetched: list[str] = []

    def fake_fetch(*, station_id, **_kwargs):
        fetched.append(station_id)
        return {"station_id": station_id}

    def fake_write(conn, *, station_id, **_kwargs):
        return {"station_id": station_id, "rows": 0, "versions": 0}

    ticks = itertools.count(0, 100)             # each look at the clock: +100 s
    monkeypatch.setattr(module, "fetch_station", fake_fetch)
    monkeypatch.setattr(module, "write_station", fake_write)
    monkeypatch.setattr(module, "REQUEST_SPACING_SECONDS", 0.0)
    monkeypatch.setattr(module.time, "monotonic", lambda: float(next(ticks)))
    assert module.main([
        "--db", str(db), "--mode", "forward", "--deadline-seconds", "350",
        "--station", "AAAA", "--station", "BBBB", "--station", "CCCC", "--station", "DDDD",
    ]) == 0
    receipt = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert fetched == ["CCCC", "DDDD", "BBBB"]
    assert receipt["captured"] == 3
    assert receipt["deferred_past_deadline"] == ["AAAA"]


def test_one_transaction_for_the_round_and_a_bad_station_rolls_back_alone(tmp_path):
    module = _module()
    db = tmp_path / "ssot.db"
    _seed(module, db, {})
    conn = sqlite3.connect(db)

    def write(conn, *, station_id, **_kwargs):
        conn.execute(
            f"INSERT INTO {module.TABLE} VALUES"
            "(?,?,?,1,1,1,0,0,0,'UTC','forward','2026-10-08T04:00Z',1,'x')",
            (station_id, "2026-10-09", module.SOURCE_ID),
        )
        if station_id == "BBBB":
            raise ValueError("bad payload")
        return {"station_id": station_id}

    module.write_station = write
    receipts = module.write_fetched(
        conn, [(s, "UTC", {}) for s in ("AAAA", "BBBB", "CCCC")], mode="forward")
    assert [r["state"] for r in receipts] == ["CAPTURED", "FAILED", "CAPTURED"]
    assert not conn.in_transaction
    assert sorted(r[0] for r in conn.execute(
        f"SELECT station_id FROM {module.TABLE} WHERE local_date='2026-10-09'")) == ["AAAA", "CCCC"]


def test_a_write_lock_that_never_comes_fails_every_fetched_station(tmp_path):
    module = _module()
    db = tmp_path / "ssot.db"
    _seed(module, db, {})
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    conn = sqlite3.connect(db, timeout=0.05)
    receipts = module.write_fetched(conn, [("AAAA", "UTC", {}), ("BBBB", "UTC", {})], mode="forward")
    holder.execute("ROLLBACK")
    assert [r["state"] for r in receipts] == ["FAILED", "FAILED"]
    assert "locked" in receipts[0]["reason"]
