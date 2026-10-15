"""Charter 183: an empty scan is not repeated; an append-only aggregate is not recomputed.

The forward flush (the production wrapper -- the validation core it calls
runs only in probe mode) scanned every decision since the last ELIGIBLE one, each
details_json read for a LIKE, and its store cursor moves only on a written
observation -- so an empty scan was the same scan again next pass, a little
longer (2 passes, 16.4s at 11:40Z).  The OOS lane grouped all ~82k system
artifacts every cycle for each station's newest vintage (1.4s warm, 2-7s in
the cycle).  Both tables are append-only by trigger.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import bayes_basket_target.production_worker as pw
from bayes_basket_target.contracts import ContractError

ELIGIBLE = json.dumps({"forward_observation_status": "ELIGIBLE"}, separators=(",", ":"))


def _decisions(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE auto_trade_decisions(cycle_id TEXT, decision_id TEXT,"
        " decision_sha256 TEXT, reason_codes_json TEXT, details_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_policy_training_fact(cycle_id TEXT, decision_id TEXT,"
        " policy_version_vector_json TEXT, feature_json TEXT)"
    )
    return conn


def _decide(conn, n: int, details: str = "{}") -> None:
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES(?,?,?,?,?)",
        (f"c{n}", f"d{n}", "s" * 64, "[]", details),
    )
    conn.commit()


def _worker(conn, tmp_path) -> pw.TargetProductionWorker:
    worker = object.__new__(pw.TargetProductionWorker)
    worker.commit_conn = conn
    worker.cash_phase_forward_store_path = tmp_path / "forward.sqlite"
    worker.cash_phase_forward_store_root = tmp_path
    return worker


def _scan_starts(conn, call) -> list:
    params: list = []
    conn.set_trace_callback(params.append)
    try:
        call()
    finally:
        conn.set_trace_callback(None)
    return [s for s in params if "FROM auto_trade_decisions d WHERE d.rowid>" in " ".join(s.split())]


def test_an_empty_scan_marks_where_it_read_to_and_the_next_starts_there(tmp_path) -> None:
    conn = _decisions(tmp_path / "ssot.db")
    for n in range(1, 6):
        _decide(conn, n)
    worker = _worker(conn, tmp_path)
    first = worker._flush_cash_phase_forward_observations()
    assert first["state"] == "already_current"
    assert worker._forward_flush_scanned == (str(tmp_path / "forward.sqlite"), 0, 5)
    _decide(conn, 6)
    scans = _scan_starts(conn, worker._flush_cash_phase_forward_observations)
    assert scans and "d.rowid>5 " in " ".join(scans[0].split()) + " "
    assert worker._forward_flush_scanned[2] == 6


def test_a_new_eligible_decision_past_the_mark_is_still_found(tmp_path) -> None:
    conn = _decisions(tmp_path / "ssot.db")
    for n in range(1, 4):
        _decide(conn, n)
    worker = _worker(conn, tmp_path)
    worker._flush_cash_phase_forward_observations()
    _decide(conn, 4, ELIGIBLE)
    # found: whatever the fixture's incomplete row then earns, the pass no
    # longer reports that nothing was there
    try:
        result = worker._flush_cash_phase_forward_observations()
    except ContractError:
        result = {"state": "refused"}
    assert result.get("state") != "already_current"


def test_a_cursor_that_moved_back_drops_the_mark(tmp_path) -> None:
    conn = _decisions(tmp_path / "ssot.db")
    for n in range(1, 4):
        _decide(conn, n)
    worker = _worker(conn, tmp_path)
    worker._forward_flush_scanned = (str(tmp_path / "forward.sqlite"), 10, 50)
    scans = _scan_starts(conn, worker._flush_cash_phase_forward_observations)
    assert "d.rowid>0 " in " ".join(scans[0].split()) + " "


def test_another_store_does_not_borrow_the_mark(tmp_path) -> None:
    conn = _decisions(tmp_path / "ssot.db")
    _decide(conn, 1)
    worker = _worker(conn, tmp_path)
    worker._forward_flush_scanned = (str(tmp_path / "other.sqlite"), 0, 50)
    scans = _scan_starts(conn, worker._flush_cash_phase_forward_observations)
    assert "d.rowid>0 " in " ".join(scans[0].split()) + " "


# --- the artifact vintage ----------------------------------------------------------------

def test_the_vintage_is_remembered_until_an_artifact_is_added(tmp_path) -> None:
    pw._ARTIFACT_VINTAGE_MEMO.clear()
    conn = sqlite3.connect(tmp_path / "ssot.db")
    # The read answers off the (system, station, ..., valid_from) index
    # (query-plan guard, 2026-10-15), so the fixture carries it.
    conn.execute(
        "CREATE TABLE calibration_native_ensemble_system_artifact(ensemble_system_id TEXT"
        " DEFAULT 'ECMWF', station_id TEXT, season_bin TEXT, lead_bucket TEXT, run_cycle TEXT,"
        " valid_from_utc TEXT, valid_until_utc TEXT)"
    )
    conn.execute(
        "CREATE INDEX calibration_native_system_artifact_scope_idx ON"
        " calibration_native_ensemble_system_artifact(ensemble_system_id,station_id,season_bin,"
        "lead_bucket,run_cycle,valid_from_utc,valid_until_utc)"
    )
    conn.execute("CREATE TABLE calibration_native_ensemble_system(ensemble_system_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO calibration_native_ensemble_system VALUES ('ECMWF')")
    conn.execute("CREATE TABLE calibration_station_registry(station_id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO calibration_station_registry VALUES (?)", [("ZUUU",), ("KORD",)])
    conn.executemany(
        "INSERT INTO calibration_native_ensemble_system_artifact(station_id, valid_from_utc) VALUES(?,?)",
        [("ZUUU", "2026-09-19T00:00:00Z"), ("ZUUU", "2026-09-20T00:00:00Z"), ("KORD", "2026-09-18T00:00:00Z")],
    )
    conn.commit()
    first = pw._native_ensemble_artifact_vintage_by_station(conn)
    assert first == {"ZUUU": "2026-09-20T00:00:00Z", "KORD": "2026-09-18T00:00:00Z"}
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    assert pw._native_ensemble_artifact_vintage_by_station(conn) == first
    assert not [s for s in statements if "GROUP BY" in s]
    conn.set_trace_callback(None)
    conn.execute(
        "INSERT INTO calibration_native_ensemble_system_artifact(station_id, valid_from_utc)"
        " VALUES('KORD','2026-09-21T00:00:00Z')"
    )
    conn.commit()
    assert pw._native_ensemble_artifact_vintage_by_station(conn)["KORD"] == "2026-09-21T00:00:00Z"
    # callers get a copy
    pw._native_ensemble_artifact_vintage_by_station(conn)["KORD"] = "x"
    assert pw._native_ensemble_artifact_vintage_by_station(conn)["KORD"] == "2026-09-21T00:00:00Z"
    pw._ARTIFACT_VINTAGE_MEMO.clear()


def test_a_store_without_the_table_reads_empty() -> None:
    conn = sqlite3.connect(":memory:")
    assert pw._native_ensemble_artifact_vintage_by_station(conn) == {}
