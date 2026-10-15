"""The delta-v2 label page finds its candidates before reading each fact's scope.

``authority_scope`` sits after the fact row's two JSON documents, so testing it
at the fact scan read every row's overflow pages: 37.6s on 2026-09-14 for a
page of the 30,611 live facts that found nothing, and the retry lane wraps to
the start of the table.  The page now filters on the leading columns, the
truth join and the label anti-join first.  The answer must be the old one,
row for row and in order.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target import delta_v2_settlement_label as lab
from bayes_basket_target.canonical_contract import FORMAL_AWC_TRUTH_ROLE

AS_OF = "2026-09-10T00:00:00Z"
ROLE = FORMAL_AWC_TRUTH_ROLE
SCOPE = lab.FACT_AUTHORITY_SCOPE

OLD_PAGE_SQL = """
SELECT f.rowid,f.fact_id,f.station_id,f.event_date,f.settlement_timezone,f.probability_envelope_id,
       length(CAST(f.policy_version_vector_json AS BLOB)),length(CAST(f.feature_json AS BLOB)),
       t.truth_id,t.revision_no,t.official_tmax_c,t.canonical_committed_at_utc,t.settlement_definition,
       t.authority_scope,t.content_sha256
FROM basket_policy_training_fact f
JOIN calibration_station_daily_high_truth t ON t.station_id=f.station_id AND t.local_date=f.event_date
WHERE {where}
  AND f.fact_id LIKE 'd2fact_%' AND f.authority_scope=?
  AND t.source_lane='AWC' AND t.source_role=? AND t.qc_status='ACCEPTED' AND t.timestamps_complete=1
  AND t.canonical_committed_at_utc<=?
  AND t.revision_no=(SELECT MAX(t2.revision_no) FROM calibration_station_daily_high_truth t2
                     WHERE t2.station_id=t.station_id AND t2.local_date=t.local_date
                       AND t2.source_lane='AWC' AND t2.source_role=? AND t2.canonical_committed_at_utc<=?)
  AND NOT EXISTS(SELECT 1 FROM basket_policy_training_label l WHERE l.fact_id=f.fact_id AND l.truth_id=t.truth_id)
ORDER BY f.rowid LIMIT ?
"""


@pytest.fixture(scope="module")
def conn():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE basket_policy_training_fact(fact_id TEXT UNIQUE, station_id TEXT, event_date TEXT,"
        " settlement_timezone TEXT, probability_envelope_id TEXT, policy_version_vector_json TEXT,"
        # The live table CHECKs authority_scope to the one forward scope;
        # the candidate page's LIMIT rests on it (2026-10-15).
        " feature_json TEXT, authority_scope TEXT CHECK(authority_scope='%s'))" % SCOPE
    )
    db.execute(
        "CREATE TABLE calibration_station_daily_high_truth(truth_id TEXT UNIQUE, station_id TEXT, local_date TEXT,"
        " settlement_definition TEXT, revision_no INTEGER, source_lane TEXT, source_role TEXT, qc_status TEXT,"
        " timestamps_complete INTEGER, canonical_committed_at_utc TEXT, official_tmax_c REAL,"
        " authority_scope TEXT, content_sha256 TEXT, UNIQUE(station_id, local_date, settlement_definition, revision_no))"
    )
    db.execute("CREATE TABLE basket_policy_training_label(fact_id TEXT, truth_id TEXT, UNIQUE(fact_id, truth_id))")
    days = [("KMIA", "2026-09-01"), ("KMIA", "2026-09-02"), ("RJTT", "2026-09-01"), ("EGLC", "2026-09-03"), ("ZUUU", "2026-09-09")]
    truth_rows = []
    for station, day in days:
        for definition in ("daily_high", "daily_high_alt"):
            for revision, committed in ((1, "2026-09-05T00:00:00Z"), (2, "2026-09-06T00:00:00Z"), (3, "2026-09-11T00:00:00Z")):
                truth_rows.append((
                    f"truth-{station}-{day}-{definition}-{revision}", station, day, definition, revision,
                    "AWC", ROLE, "ACCEPTED" if (station, revision) != ("EGLC", 2) else "REJECTED",
                    1, committed, 30.0 + revision, "FORMAL", "c" * 64,
                ))
    db.executemany("INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", truth_rows)
    facts = []
    for index in range(90):
        station, day = days[index % len(days)]
        fact_id = ("d2fact_" if index % 7 else "legacy_") + f"{index:04d}"
        facts.append((
            fact_id, station, day, "UTC", f"env-{index}", json.dumps({"v": "x" * (index % 5000)}),
            json.dumps({"f": "y" * 4000}), SCOPE,
        ))
    db.executemany("INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?,?,?,?)", facts)
    labelled = [
        (f"d2fact_{i:04d}", f"truth-{days[i % len(days)][0]}-{days[i % len(days)][1]}-daily_high-2")
        for i in range(0, 90, 3)
    ]
    db.executemany("INSERT OR IGNORE INTO basket_policy_training_label VALUES(?,?)", labelled)
    db.commit()
    return db


@pytest.mark.parametrize(
    "where, params, limit",
    [
        ("f.rowid>?", (0,), 65),
        ("f.rowid>?", (0,), 7),
        ("f.rowid>?", (40,), 65),
        ("f.rowid>? AND f.rowid<=?", (0, 50), 65),
        ("f.rowid>? AND f.rowid<=?", (13, 61), 3),
        ("f.rowid>? AND f.rowid<=?", (0, 90), 1000),
    ],
)
def test_the_page_is_the_old_page_row_for_row(conn, where, params, limit) -> None:
    old = conn.execute(
        OLD_PAGE_SQL.format(where=where), (*params, SCOPE, ROLE, AS_OF, ROLE, AS_OF, limit)
    ).fetchall()
    new = lab._label_candidate_page(conn, rowid_where=where, rowid_params=params, as_of=AS_OF, limit=limit)
    assert new == old
    assert old, "the fixture must leave rows owed a label"


def test_the_candidates_are_materialized_before_the_scope_is_read(conn) -> None:
    """The plan, not a clock: the candidate set is built as its own step and
    the fact rows are revisited by rowid for their scope and sizes."""

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    lab._label_candidate_page(conn, rowid_where="f.rowid>?", rowid_params=(0,), as_of=AS_OF, limit=5)
    conn.set_trace_callback(None)
    (sql,) = [text for text in statements if "basket_policy_training_fact" in text]
    plan = [
        str(row[-1])
        for row in conn.execute("EXPLAIN QUERY PLAN " + sql)
    ]
    assert "MATERIALIZE candidate" in plan, plan
    assert any("USING INTEGER PRIMARY KEY (rowid=?)" in step for step in plan), plan
