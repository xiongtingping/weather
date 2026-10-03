"""Two cycle reads that re-read everything to find almost nothing new.

2026-10-03, a 197 s production cycle:

* the regime stage loaded every fresh station-day whole (1,957 envelopes,
  14 s) to build companions for the 1-23 envelopes committed since the pass
  before -- envelopes and companions are append-only and a companion is a
  pure function of its envelope;
* the correction pre-clear counted flagged reports with a statement that
  could seek nothing and read all 218,000 archived reports, 2.9 s warm,
  twice a cycle -- the archive only inserts reports.

Both now read what was added since they last looked.  These tests pin what
each still owes: the first pass, a scope with a blocker, a pass that did
not finish; a flagged report added, a review added, another archive.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from bayes_basket_target import production_worker as pw
from bayes_basket_target.production_worker import TargetProductionWorker

A, B, C = ("KAAA", "2026-10-03"), ("KBBB", "2026-10-03"), ("KCCC", "2026-10-04")


def _stage(rows: list[tuple[str, str]]) -> SimpleNamespace:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_probability_envelope (station_id TEXT, event_date TEXT)")
    conn.executemany("INSERT INTO basket_probability_envelope VALUES (?,?)", rows)
    return SimpleNamespace(conn=conn)


def _ceiling(stage: SimpleNamespace) -> int:
    return int(stage.conn.execute(
        "SELECT COALESCE(MAX(rowid),0) FROM basket_probability_envelope"
    ).fetchone()[0])


owed = TargetProductionWorker._regime_scopes_owed
note = TargetProductionWorker._regime_note_pass


def test_the_first_pass_of_a_process_owes_every_fresh_scope() -> None:
    stage = _stage([A, B, C])
    assert owed(stage, [A, B, C], ceiling=_ceiling(stage)) == [A, B, C]


def test_a_finished_pass_leaves_only_the_scopes_an_envelope_landed_on() -> None:
    stage = _stage([A, B, C])
    note(stage, [A, B, C], [], full_pass_ceiling=_ceiling(stage))
    assert owed(stage, [A, B, C], ceiling=_ceiling(stage)) == []
    stage.conn.execute("INSERT INTO basket_probability_envelope VALUES (?,?)", B)
    assert owed(stage, [A, B, C], ceiling=_ceiling(stage)) == [B]
    # Fresh scopes only: an envelope on a scope no longer fresh is not read.
    assert owed(stage, [A, C], ceiling=_ceiling(stage)) == []


def test_a_scope_whose_read_had_a_blocker_is_owed_until_a_read_has_none() -> None:
    stage = _stage([A, B, C])
    blocker = [{"station_id": B[0], "event_date": B[1], "reason": "ContractError:x"}]
    note(stage, [A, B, C], blocker, full_pass_ceiling=_ceiling(stage))
    assert owed(stage, [A, B, C], ceiling=_ceiling(stage)) == [B]
    # A catch-up that reads it clean settles it; one that reads another
    # scope with a blocker adds that one.
    note(stage, [B], [], full_pass_ceiling=None)
    note(stage, [C], [{"station_id": C[0], "event_date": C[1]}], full_pass_ceiling=None)
    assert owed(stage, [A, B, C], ceiling=_ceiling(stage)) == [C]


def test_a_catch_up_does_not_stand_in_for_the_stage_pass() -> None:
    stage = _stage([A, B])
    note(stage, [A, B], [], full_pass_ceiling=_ceiling(stage))
    stage.conn.execute("INSERT INTO basket_probability_envelope VALUES (?,?)", A)
    # The catch-up read A, but only a finished stage pass moves the mark: a
    # catch-up that died half way would otherwise leave A unread for good.
    note(stage, [A], [], full_pass_ceiling=None)
    assert owed(stage, [A, B], ceiling=_ceiling(stage)) == [A]
    note(stage, [A], [], full_pass_ceiling=_ceiling(stage))
    assert owed(stage, [A, B], ceiling=_ceiling(stage)) == []


def test_a_mark_past_the_table_owes_everything(monkeypatch) -> None:
    stage = _stage([A, B])
    stage._regime_full_done_mark = 99  # not this table
    assert owed(stage, [A, B], ceiling=_ceiling(stage)) == [A, B]
    stage._regime_full_done_mark = _ceiling(stage)
    monkeypatch.setattr(pw, "REGIME_STAGE_INCREMENTAL", False)
    assert owed(stage, [A, B], ceiling=_ceiling(stage)) == [A, B]


def test_the_stage_pass_asks_the_helper_and_notes_what_it_read() -> None:
    import inspect

    source = inspect.getsource(TargetProductionWorker._materialize_regime_ssot)
    assert "self._regime_scopes_owed(" in source
    assert source.index("self._regime_note_pass(") > source.index("regime_artifact_build")
    assert "full_pass_ceiling if only_scopes is None else None" in source


# ---- the correction count ---------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_counts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pw, "CORRECTION_COUNT_INCREMENTAL", True)
    pw.forget_correction_suspects()
    yield
    pw.forget_correction_suspects()


def _archive(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
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


def _report(conn, observation_id, flagged, at) -> None:
    conn.execute(
        "INSERT INTO reported_trajectory_observations VALUES (?,?,?,?)",
        (observation_id, "KMIA", flagged, at),
    )


def _both(conn, as_of: str, monkeypatch) -> int:
    kept = pw.pending_correction_review_count(conn, as_of_utc=as_of)
    monkeypatch.setattr(pw, "CORRECTION_COUNT_INCREMENTAL", False)
    asked = pw.pending_correction_review_count(conn, as_of_utc=as_of)
    monkeypatch.setattr(pw, "CORRECTION_COUNT_INCREMENTAL", True)
    assert kept == asked
    return kept


def test_the_count_follows_reports_and_reviews_as_they_are_added(tmp_path, monkeypatch) -> None:
    conn = _archive(tmp_path / "archive.db")
    _report(conn, "old", 1, "2026-08-20T01:00:00Z")
    _report(conn, "clean", 0, "2026-08-30T01:00:00Z")
    _report(conn, "a", 1, "2026-08-30T01:00:00Z")
    assert _both(conn, "2026-08-30T02:00:00Z", monkeypatch) == 1
    _report(conn, "b", 1, "2026-08-30T01:30:00Z")
    assert _both(conn, "2026-08-30T02:00:00Z", monkeypatch) == 2
    conn.execute("INSERT INTO reported_trajectory_station_halt_reviews VALUES ('r','a')")
    assert _both(conn, "2026-08-30T02:00:00Z", monkeypatch) == 1
    # The window is the statement's: three days back from the instant asked.
    assert _both(conn, "2026-09-03T02:00:00Z", monkeypatch) == 0
    assert _both(conn, "2026-08-21T00:00:00Z", monkeypatch) == 3 - 1


def test_a_later_call_reads_only_the_reports_added_since(tmp_path) -> None:
    conn = _archive(tmp_path / "archive.db")
    for index in range(50):
        _report(conn, f"r{index}", int(index == 7), "2026-08-30T01:00:00Z")
    assert pw.pending_correction_review_count(conn, as_of_utc="2026-08-30T02:00:00Z") == 1
    _report(conn, "new", 1, "2026-08-30T01:40:00Z")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    assert pw.pending_correction_review_count(conn, as_of_utc="2026-08-30T02:00:00Z") == 2
    conn.set_trace_callback(None)
    reads = [s for s in statements if "FROM reported_trajectory_observations" in s and "rowid>" in s]
    assert len(reads) == 1 and "rowid>50 AND rowid<=51" in " ".join(reads[0].split())
    assert not [s for s in statements if "NOT EXISTS" in s]


def test_two_archives_do_not_answer_for_each_other(tmp_path) -> None:
    first = _archive(tmp_path / "a.db")
    second = _archive(tmp_path / "b.db")
    _report(first, "a", 1, "2026-08-30T01:00:00Z")
    assert pw.pending_correction_review_count(first, as_of_utc="2026-08-30T02:00:00Z") == 1
    assert pw.pending_correction_review_count(second, as_of_utc="2026-08-30T02:00:00Z") == 0


def test_a_store_with_no_file_asks_the_statement(monkeypatch) -> None:
    conn = _archive(":memory:")
    _report(conn, "a", 1, "2026-08-30T01:00:00Z")
    assert pw.pending_correction_review_count(conn, as_of_utc="2026-08-30T02:00:00Z") == 1
    assert pw._CORRECTION_SUSPECTS == {}
    assert pw.pending_correction_review_count(
        sqlite3.connect(":memory:"), as_of_utc="2026-08-30T02:00:00Z"
    ) == 0
