"""Charter 207: a settled day whose evidence has since grown is re-settled.

Until 2026-09-23 the resident settled a station-day once, at the close of its
window, and nothing looked again: MPMG 2026-09-21 settled 30C on a record
missing 16:00-19:00Z, the missing reports (31C at 19:00Z) landed at 07:52Z the
next morning, and the truth stayed 30C while the market paid 31C.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "settlement_truth_sweep_for_test", ROOT / "scripts" / "settlement_truth_sweep.py"
)
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)

ROLE = sweep.CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1
WINDOW = {"start_utc": "2026-09-21T05:00:00.000Z", "end_utc": "2026-09-22T05:00:00.000Z"}


def databases(*, received_at: str, archive_commits: list[str], registry: bool = True):
    target = sqlite3.connect(":memory:")
    target.executescript(
        """
        CREATE TABLE calibration_station_daily_high_truth(
          station_id TEXT, local_date TEXT, revision_no INTEGER, qc_status TEXT,
          source_role TEXT, received_at_utc TEXT, observation_ids_json TEXT);
        CREATE TABLE basket_target_market_registry(
          station_id TEXT, event_date TEXT, source_eligible INTEGER);
        """
    )
    target.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?)",
        ("MPMG", "2026-09-21", 0, "ACCEPTED", ROLE, received_at,
         json.dumps({"settlement_window": WINDOW, "observations": []})),
    )
    if registry:
        target.execute("INSERT INTO basket_target_market_registry VALUES('MPMG','2026-09-21',1)")
    archive = sqlite3.connect(":memory:")
    archive.execute(
        "CREATE TABLE reported_trajectory_observations("
        "station_id TEXT, source_lane TEXT, valid_at TEXT, canonical_committed_at TEXT)"
    )
    archive.executemany(
        "INSERT INTO reported_trajectory_observations VALUES('MPMG','awc',?,?)",
        [("2026-09-21T19:00:00.000Z", committed) for committed in archive_commits],
    )
    return target, archive


def test_a_report_that_landed_after_the_truth_makes_the_day_a_candidate() -> None:
    target, archive = databases(
        received_at="2026-09-22T01:24:50Z",
        archive_commits=["2026-09-22T01:24:50.119Z", "2026-09-22T07:52:24.500Z"],
    )
    assert sweep._grown_evidence(target, archive, "2026-08-24") == ([("MPMG", "2026-09-21")], 0)


def test_the_truths_own_newest_report_is_not_growth() -> None:
    # The truth row stamps whole seconds, the archive milliseconds; the same
    # report must not read as a newer one.
    target, archive = databases(
        received_at="2026-09-22T01:24:50Z", archive_commits=["2026-09-22T01:24:50.999Z"],
    )
    assert sweep._grown_evidence(target, archive, "2026-08-24") == ([], 0)


def test_a_day_the_registry_no_longer_carries_is_asked_and_counted() -> None:
    # Until 2026-09-24 such a day was only counted, so the eight 2026-09-09
    # station-days whose backfilled afternoon moved the maximum never got a
    # revision.  The writer now re-settles them on their standing lineage.
    target, archive = databases(
        received_at="2026-09-22T01:24:50Z",
        archive_commits=["2026-09-22T07:52:24.500Z"],
        registry=False,
    )
    assert sweep._grown_evidence(target, archive, "2026-08-24") == (
        [("MPMG", "2026-09-21")], 1,
    )


def test_the_scan_is_not_bounded_by_awcs_retention() -> None:
    # What grows is the archive; a backfill can reach any day it holds.
    target, archive = databases(
        received_at="2026-09-22T01:24:50Z",
        archive_commits=["2026-09-22T07:52:24.500Z"],
    )
    assert sweep._grown_evidence(target, archive) == ([("MPMG", "2026-09-21")], 0)


def test_a_re_settlement_holds_the_write_lock_before_the_writer_reads(monkeypatch, tmp_path) -> None:
    # A deferred transaction that reads and then upgrades while the resident
    # holds the lock fails at once in WAL mode; the pass must queue for the
    # lock first and let the writer stage into its transaction.
    target, archive = tmp_path / "ssot.db", tmp_path / "archive.db"
    for path in (target, archive):
        sqlite3.connect(path).close()
    seen: list[bool] = []

    def writer(conn, _source, **_kw):
        seen.append(conn.in_transaction)
        return {"state": "staged_in_caller_transaction", "wrote": True, "revision_no": 1}

    monkeypatch.setattr(sweep, "_backlog", lambda conn, start: [])
    monkeypatch.setattr(sweep, "_coverage", lambda conn, start: [])
    monkeypatch.setattr(sweep, "_grown_evidence", lambda t, s, since="": ([("MPMG", "2026-09-21")], 0))
    monkeypatch.setattr(sweep, "materialize_settled_station_truth", writer)
    report = sweep.sweep(target, archive, window_days=45, max_scopes=10, dry_run=False, verbose=False)
    assert seen == [True]
    assert report["revision_outcomes"] == {"revised": 1}
    assert report["revised"][0]["station_id"] == "MPMG"
