"""When a settled label became knowable -- one answer for every claim lane.

Charter 132 (audit item 47).  A walk-forward fit standing at moment T may read
a station-day's label only if it was knowable at T.  The ledger's
``paper_trade_candidate_window.settled_at_utc`` is not that moment: it is when
the ledger wrote the settlement, and after the 2026-09-12 re-derive every row
carries a write time of 2026-09-07 or later -- a label for 2026-08-15 reads as
knowable 23 days after the fact.  Used as the bound it leaks nothing but it
starves every early fold, which is how the first four-arm run held every stage
"accumulating" until 2026-09-08.

The label came from one truth revision (``truth_revision_no``), and that
revision became knowable when the truth lane committed it
(``calibration_station_daily_high_truth.canonical_committed_at_utc``).  The
bucket is a deterministic function of that revision and the contract, so the
label was knowable from that commit.  A later revision that moved the bucket
is not knowable earlier than its own commit, and an earlier revision is not
used at all -- the label is the ledger's -- so the bound is exact for the
label read and never early.

Where the truth lane is absent (a fixture, a research extract) the ledger's
write time is the bound: later than the truth, so conservative, and stated.
"""
from __future__ import annotations

import sqlite3
from typing import Any

SOURCE_TRUTH_COMMIT = "truth_revision_commit"
SOURCE_LEDGER_WRITE = "ledger_settlement_write"


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return conn.execute(
            # Every schema (the paper ledger is an attached file since 2026-09-19).
            "SELECT 1 FROM pragma_table_list WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return any(
            str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})")
        )
    except sqlite3.Error:
        return False


def knowable_labels(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """``(station_id, event_date) -> {bucket, known_at, source, revision}``.

    Charter 140: on production evidence (the rule) the labels are the truth
    lane's own, appended per revision by ``refresh_settlement_labels`` with
    the revision's commit time as the knowable moment -- no paper table in
    the path.  The ledger source reads the paper candidate table's
    settlement as before, one row per station-day.
    """

    from .candidate_window_evidence import (  # noqa: PLC0415
        EVIDENCE_SOURCE,
        LABEL_TABLE,
    )

    if EVIDENCE_SOURCE == "production":
        return _truth_lane_labels(conn, LABEL_TABLE)
    return _ledger_labels(conn)


def _truth_lane_labels(
    conn: sqlite3.Connection, table: str
) -> dict[tuple[str, str], dict[str, Any]]:
    if not _has_table(conn, table):
        return {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        rows = conn.execute(
            "SELECT station_id, event_date, settled_bucket, known_at_utc,"
            " truth_revision_no FROM " + table
        ).fetchall()
    except sqlite3.Error:
        return {}
    for station, day, bucket, known_at, revision in rows:
        key = (str(station), str(day))
        rank = int(revision)
        current = out.get(key)
        # The standing revision's label, at that revision's commit time.
        if current is None or rank > current["revision_rank"]:
            out[key] = {
                "bucket": str(bucket), "known_at": str(known_at),
                "source": SOURCE_TRUTH_COMMIT,
                "revision": rank, "revision_rank": rank,
            }
    return out


def _ledger_labels(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """The paper candidate table's settlement (the pre-charter-140 reading)."""

    if not _has_table(conn, "paper_trade_candidate_window"):
        return {}
    if not _has_column(conn, "paper_trade_candidate_window", "settled_at_utc"):
        return {}
    has_revision = _has_column(conn, "paper_trade_candidate_window", "truth_revision_no")
    joinable = has_revision and _has_table(conn, "calibration_station_daily_high_truth")
    if joinable:
        query = (
            "SELECT w.station_id, w.event_date, w.settled_bucket, w.settled_at_utc,"
            "       w.truth_revision_no, MIN(t.canonical_committed_at_utc)"
            " FROM paper_trade_candidate_window w"
            " LEFT JOIN calibration_station_daily_high_truth t"
            "   ON t.station_id = w.station_id AND t.local_date = w.event_date"
            "  AND t.revision_no = w.truth_revision_no AND t.qc_status = 'ACCEPTED'"
            " WHERE w.settled_bucket IS NOT NULL AND w.settled_at_utc IS NOT NULL"
            " GROUP BY w.station_id, w.event_date, w.settled_bucket,"
            "          w.settled_at_utc, w.truth_revision_no"
        )
    else:
        query = (
            "SELECT station_id, event_date, settled_bucket, settled_at_utc,"
            + (" truth_revision_no," if has_revision else " NULL,")
            + " NULL FROM paper_trade_candidate_window"
            " WHERE settled_bucket IS NOT NULL AND settled_at_utc IS NOT NULL"
        )
    out: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        rows = conn.execute(query).fetchall()
    except sqlite3.Error:
        return {}
    for station, day, bucket, written_at, revision, committed_at in rows:
        key = (str(station), str(day))
        if committed_at:
            known_at, source = str(committed_at), SOURCE_TRUTH_COMMIT
        else:
            known_at, source = str(written_at), SOURCE_LEDGER_WRITE
        current = out.get(key)
        # A station-day re-settled on a later revision keeps the latest
        # revision's label and that revision's knowable time.
        rank = -1 if revision is None else int(revision)
        if current is None or rank > current["revision_rank"]:
            out[key] = {
                "bucket": str(bucket), "known_at": known_at, "source": source,
                "revision": revision, "revision_rank": rank,
            }
    return out


__all__ = ["SOURCE_TRUTH_COMMIT", "SOURCE_LEDGER_WRITE", "knowable_labels"]
