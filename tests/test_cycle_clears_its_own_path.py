"""The production cycle clears its own path: corrections and truth.

Systemic contract (user directive, 2026-08-30): work whose absence blocks
P1 or leaves settled days without truth must not depend on timer-driven
sweeps in a contended maintenance lane.  The cycle detects and does that
work itself, bounded, every cycle; the sweeps remain backstops.  These
tests pin the detection query, the in-cycle truth increment's transaction
discipline, and the board's grading of staleness versus actual harm.
"""

from __future__ import annotations

import json
import sqlite3
import types

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target import production_worker as pw


def _archive_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reported_trajectory_observations ("
        " observation_id TEXT PRIMARY KEY, station_id TEXT,"
        " correction_suspected INTEGER, canonical_committed_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE reported_trajectory_station_halt_reviews ("
        " review_id TEXT PRIMARY KEY, triggering_observation_id TEXT)"
    )
    return conn


def test_pending_count_sees_an_unreviewed_correction() -> None:
    conn = _archive_conn()
    conn.execute(
        "INSERT INTO reported_trajectory_observations VALUES"
        " ('rtobs_a','KMIA',1,'2026-08-30T01:00:00Z')"
    )
    assert pw.pending_correction_review_count(
        conn, as_of_utc="2026-08-30T02:00:00Z"
    ) == 1


def test_pending_count_ignores_reviewed_old_and_clean_rows() -> None:
    conn = _archive_conn()
    conn.executemany(
        "INSERT INTO reported_trajectory_observations VALUES (?,?,?,?)",
        [
            ("rtobs_reviewed", "KMIA", 1, "2026-08-30T01:00:00Z"),
            ("rtobs_old", "KMIA", 1, "2026-08-20T01:00:00Z"),
            ("rtobs_clean", "KMIA", 0, "2026-08-30T01:00:00Z"),
        ],
    )
    conn.execute(
        "INSERT INTO reported_trajectory_station_halt_reviews VALUES"
        " ('rev1','rtobs_reviewed')"
    )
    assert pw.pending_correction_review_count(
        conn, as_of_utc="2026-08-30T02:00:00Z"
    ) == 0


def test_pending_count_tolerates_a_fresh_archive() -> None:
    conn = sqlite3.connect(":memory:")
    assert pw.pending_correction_review_count(
        conn, as_of_utc="2026-08-30T02:00:00Z"
    ) == 0


def _worker_stub() -> pw.TargetProductionWorker:
    worker = object.__new__(pw.TargetProductionWorker)
    return worker


def test_preclear_is_a_noop_when_nothing_pends(monkeypatch) -> None:
    worker = _worker_stub()
    worker.canonical_source_conn = _archive_conn()
    called = []
    monkeypatch.setattr(
        pw.subprocess, "run",
        lambda *a, **k: called.append(a) or pytest.fail("must not spawn"),
    )
    receipt = worker._preclear_pending_corrections(
        as_of_utc="2026-08-30T02:00:00Z"
    )
    assert receipt["state"] == "NOTHING_PENDING"
    assert not called


def test_preclear_runs_the_policy_and_stamps_the_throttle(monkeypatch) -> None:
    worker = _worker_stub()
    conn = _archive_conn()
    conn.execute(
        "INSERT INTO reported_trajectory_observations VALUES"
        " ('rtobs_a','KMIA',1,'2026-08-30T01:00:00Z')"
    )
    worker.canonical_source_conn = conn
    worker.canonical_source_db_path = "archive.db"
    worker.db_path = "target.db"

    summary = json.dumps({
        "cleared_scope_count": 1, "cleared_observation_count": 1,
        "skipped_count": 0,
    })
    monkeypatch.setattr(
        pw.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout=summary + "\n", stderr="",
        ),
    )
    receipt = worker._preclear_pending_corrections(
        as_of_utc="2026-08-30T02:00:00Z"
    )
    assert receipt["state"] == "CLEARED"
    assert receipt["pending"] == 1
    assert receipt["cleared_scope_count"] == 1
    # A completed pre-clear stamps the six-hour pass's throttle so the two
    # engines do not double-run the same window.
    assert getattr(worker, "_correction_auto_clear_last_mono") is not None
    # And never authorizes anything.
    assert receipt["orders_submitted"] == 0
    assert receipt["calls_order_gateway"] is False


def _target_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    # The registry as production holds it: rows under a snapshot, with the
    # settlement timezone the increment reads to set aside days still open.
    conn.execute(
        "CREATE TABLE basket_target_market_registry_snapshot ("
        " snapshot_sha256 TEXT PRIMARY KEY)"
    )
    conn.execute(
        # ``refreshed_at`` is not decoration here: the increment asks the
        # registry's latest word on a scope, not whether any row ever said
        # eligible, so a fixture without it is a fixture the probe cannot read.
        "CREATE TABLE basket_target_market_registry ("
        " snapshot_sha256 TEXT, station_id TEXT, event_date TEXT,"
        " settlement_timezone TEXT, source_eligible INTEGER, refreshed_at TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot VALUES ('s1')"
    )
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth ("
        " station_id TEXT, local_date TEXT, qc_status TEXT)"
    )
    return conn


def test_truth_increment_materializes_only_the_missing(monkeypatch) -> None:
    worker = _worker_stub()
    target = _target_conn()
    target.executemany(
        "INSERT INTO basket_target_market_registry"
        " VALUES ('s1',?,?,'America/New_York',1,'2026-08-30T00:00:00Z')",
        [("KMIA", "2026-08-29"), ("ZBAA", "2026-08-29")],
    )
    target.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES"
        " ('ZBAA','2026-08-29','ACCEPTED')"
    )
    worker.commit_conn = target
    worker.canonical_source_conn = sqlite3.connect(":memory:")
    calls = []

    def _materialize(conn, source, *, station_id, event_date, as_of_utc):
        calls.append((station_id, event_date))
        return {"state": "committed", "wrote": True}

    monkeypatch.setattr(
        pw, "materialize_settled_station_truth", _materialize
    )
    receipt = worker._materialize_truth_increment(
        as_of_utc="2026-08-30T05:00:00Z"
    )
    assert calls == [("KMIA", "2026-08-29")]
    assert receipt["state"] == "COMMITTED"
    assert receipt["committed_scopes"] == ["KMIA|2026-08-29"]


def test_truth_increment_refusal_does_not_end_the_pass(monkeypatch) -> None:
    worker = _worker_stub()
    target = _target_conn()
    target.executemany(
        "INSERT INTO basket_target_market_registry"
        " VALUES ('s1',?,?,'America/New_York',1,'2026-08-30T00:00:00Z')",
        [("AAAA", "2026-08-29"), ("BBBB", "2026-08-29")],
    )
    worker.commit_conn = target
    worker.canonical_source_conn = sqlite3.connect(":memory:")

    def _materialize(conn, source, *, station_id, event_date, as_of_utc):
        if station_id == "AAAA":
            raise ContractError(
                "settlement_truth_corrections_unresolved:rtobs_x"
            )
        return {"state": "skipped_open_settlement_day", "wrote": False}

    monkeypatch.setattr(
        pw, "materialize_settled_station_truth", _materialize
    )
    receipt = worker._materialize_truth_increment(
        as_of_utc="2026-08-30T05:00:00Z"
    )
    assert receipt["refusals"] == {
        "settlement_truth_corrections_unresolved": 1
    }
    assert receipt["outcomes"]["skipped_open_settlement_day"] == 1
    assert receipt["state"] == "COMMITTED"


def test_truth_increment_is_bounded_per_cycle(monkeypatch) -> None:
    worker = _worker_stub()
    target = _target_conn()
    target.executemany(
        "INSERT INTO basket_target_market_registry"
        " VALUES ('s1',?,?,'America/New_York',1,'2026-08-30T00:00:00Z')",
        [(f"S{i:03d}", "2026-08-29") for i in range(40)],
    )
    worker.commit_conn = target
    worker.canonical_source_conn = sqlite3.connect(":memory:")
    calls = []
    monkeypatch.setattr(
        pw, "materialize_settled_station_truth",
        lambda conn, source, **kw: calls.append(1) or {
            "state": "committed", "wrote": True,
        },
    )
    worker._materialize_truth_increment(as_of_utc="2026-08-30T05:00:00Z")
    assert len(calls) == pw.TRUTH_INCREMENT_MAX_SCOPES


def _board(tmp_path, monkeypatch, *, sweep, worker_status):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "cal_dashboard",
        Path(__file__).resolve().parents[1]
        / "scripts" / "calibration_dashboard.py",
    )
    board = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(board)
    sweep_path = tmp_path / "sweep.json"
    status_path = tmp_path / "status.json"
    if sweep is not None:
        sweep_path.write_text(json.dumps(sweep))
    status_path.write_text(json.dumps(worker_status))
    monkeypatch.setattr(board, "TRUTH_SWEEP_STATUS", sweep_path)
    monkeypatch.setattr(board, "STATUS_JSON", status_path)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_market_registry ("
        " station_id TEXT, event_date TEXT, source_eligible INTEGER)"
    )
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth ("
        " station_id TEXT, local_date TEXT, qc_status TEXT)"
    )
    return board.collect_truth_materialization(conn)


def test_board_stale_engines_with_full_coverage_is_amber(
    tmp_path, monkeypatch
) -> None:
    # The deploy-day shape that cried wolf: sweep 6.8h stale, nothing
    # actually missing.  Amber, and the note names both engines.
    result = _board(
        tmp_path, monkeypatch,
        sweep={"swept_at_utc": "2026-08-29T22:01:24Z", "backlog_after": 0},
        worker_status={"production": {}},
    )
    assert result["health"] == "amber"
    assert "覆盖完整" in result["note"]


def test_board_fresh_cycle_receipt_keeps_it_green(
    tmp_path, monkeypatch
) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = _board(
        tmp_path, monkeypatch,
        sweep={"swept_at_utc": "2026-08-29T22:01:24Z", "backlog_after": 0},
        worker_status={"production": {"truth_materialization": {
            "checked_at_utc": now,
        }}},
    )
    assert result["health"] == "green"


def test_board_real_gaps_with_dead_engines_is_red(
    tmp_path, monkeypatch
) -> None:
    result = _board(
        tmp_path, monkeypatch,
        sweep={"swept_at_utc": "2026-08-29T10:00:00Z", "backlog_after": 7},
        worker_status={"production": {}},
    )
    assert result["health"] == "red"
    assert "两台重试引擎" in result["note"]


def test_board_treats_a_just_restarted_resident_as_a_fresh_engine(
    tmp_path, monkeypatch
) -> None:
    """The deploy window: no receipt yet, because no cycle has finished.

    The status document is rebuilt at boot and the production block lands
    in it only when the first cycle completes, so for the first ten-odd
    minutes after a restart the cycle engine has nothing to show.  On
    2026-09-02 20:30 the board rendered five minutes after a restart, read
    no receipt, and graded a complete book amber on the sweep's clock
    alone.  A resident that has just started is fresh as of its start.
    """

    from datetime import datetime, timedelta, timezone
    boot = datetime.now(timezone.utc) - timedelta(minutes=5)
    result = _board(
        tmp_path, monkeypatch,
        sweep={"swept_at_utc": "2026-08-29T22:01:24Z", "backlog_after": 0},
        worker_status={
            "started_at": boot.isoformat().replace("+00:00", "Z"),
            # No production block at all: the first cycle is still running.
        },
    )
    assert result["health"] == "green"
    assert result["cycle_age_hours"] is None
    assert result["resident_boot_age_hours"] == pytest.approx(
        5 / 60, abs=0.02
    )


def test_board_does_not_let_an_old_boot_stand_in_for_a_receipt(
    tmp_path, monkeypatch
) -> None:
    """A resident up for hours with no receipt is an engine that stopped.

    The boot clock is only a stand-in while the first cycle is pending; it
    ages like any other clock, so a resident that never completes a cycle
    turns amber on its own once the sweep is stale too.
    """

    result = _board(
        tmp_path, monkeypatch,
        sweep={"swept_at_utc": "2026-08-29T22:01:24Z", "backlog_after": 0},
        worker_status={"started_at": "2026-08-29T20:00:00Z", "production": {}},
    )
    assert result["health"] == "amber"
    assert "覆盖完整" in result["note"]
    assert "首轮未到" in result["note"]
