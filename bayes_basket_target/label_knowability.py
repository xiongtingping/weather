"""When a settled label became knowable -- one answer for every claim lane.

Charter 132 (audit item 47).  A walk-forward fit standing at moment T may read
a station-day's label only if it was knowable at T.  The label comes from one
truth revision (``truth_revision_no``), and that revision became knowable when
the truth lane committed it; ``refresh_settlement_labels`` appends a label row
per revision with that commit time as ``known_at_utc``.  A later revision that
moved the bucket is not knowable earlier than its own commit, so the bound is
exact for the label read and never early.

Charter 140: the labels are the truth lane's own -- no paper table in the
path.
"""
from __future__ import annotations

import sqlite3
from typing import Any

SOURCE_TRUTH_COMMIT = "truth_revision_commit"


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM pragma_table_list WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def knowable_labels(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """``(station_id, event_date) -> {bucket, known_at, source, revision}``.

    The standing revision's label per station-day, at that revision's commit
    time.
    """

    from .candidate_window_evidence import LABEL_TABLE  # noqa: PLC0415

    return _truth_lane_labels(conn, LABEL_TABLE)


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


__all__ = ["SOURCE_TRUTH_COMMIT", "knowable_labels"]
