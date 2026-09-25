"""Production's claim-lane relations, for the estimator suites' fixtures.

Charter 140: the lambda, beta, valuation and posterior lanes read their
population from ``basket_candidate_window_scored`` and their labels from
``basket_station_day_settlement_label``.  In production the first is a view
over the evidence table joined to the labels; a fixture creates it as a plain
table holding the columns its suite needs, because the estimators only read
it.  The label table is created here in the columns the readers select.
"""

from __future__ import annotations

import sqlite3

from bayes_basket_target.candidate_window_evidence import (
    CANDIDATE_SOURCE,
    LABEL_TABLE,
)

__all__ = ["CANDIDATE_SOURCE", "LABEL_TABLE", "add_label", "create_label_table"]


def create_label_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {LABEL_TABLE} ("
        " station_id TEXT NOT NULL, event_date TEXT NOT NULL,"
        " truth_revision_no INTEGER NOT NULL, settled_bucket TEXT NOT NULL,"
        " known_at_utc TEXT NOT NULL)"
    )


def add_label(
    conn: sqlite3.Connection,
    station_id: str,
    event_date: str,
    *,
    settled_bucket: str = "",
    known_at_utc: str | None = None,
    truth_revision_no: int = 0,
) -> None:
    """One truth revision's label; knowable at the end of the event day
    unless the fixture says when."""

    create_label_table(conn)
    conn.execute(
        f"INSERT INTO {LABEL_TABLE}(station_id, event_date, truth_revision_no,"
        " settled_bucket, known_at_utc) VALUES (?,?,?,?,?)",
        (
            str(station_id), str(event_date), int(truth_revision_no),
            str(settled_bucket), known_at_utc or f"{event_date}T23:59:59Z",
        ),
    )
