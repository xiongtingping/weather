"""Each day is projected with the trajectory artifact it could have had (v4).

v3 recomputed 08-01..09-25 with today's artifacts, refit around 09-01 on those
same days: the August record graded parameters on their own training days.
User ruling 2026-09-25, 「按当时参数重算」: a day uses the artifact already
available (``valid_from_utc``) when it is issued -- local noon until v5, noon
plus the arrival grace since -- chosen the way the planner always chose: the
preferred local-time bucket, then the newest training.  A day before any
artifact existed is not projected.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from bayes_basket_target import metar_physical_forward as mpf

UTC = timezone.utc
# ZUUU 2026-08-20 (Asia/Shanghai): local noon is 04:00Z.
NOON = datetime(2026, 8, 20, 4, tzinfo=UTC)


def _store(rows) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_evidence_artifact(station_id TEXT, scope_key TEXT,"
        " trained_through_utc TEXT, valid_from_utc TEXT, artifact_json TEXT, artifact_kind TEXT)"
    )
    for station, bucket, trained, available, marker in rows:
        body = {"content_sha256": marker}
        if marker != "empty":
            body["remaining_warming_distribution"] = {"0.0": 1.0}
        conn.execute(
            "INSERT INTO basket_probability_evidence_artifact VALUES(?,?,?,?,?,?)",
            (station, f"{station}|x|{bucket}", trained, available, json.dumps(body),
             "METAR_TRAJECTORY_LIKELIHOOD"),
        )
    return conn


def _chosen(conn, anchor=NOON):
    timeline = mpf._station_artifact_timeline(conn)
    artifact = mpf._artifact_as_of(conn, timeline.get("ZUUU", ()), anchor)
    return None if artifact is None else artifact["content_sha256"]


def test_an_artifact_that_arrived_after_noon_is_not_used() -> None:
    conn = _store([
        ("ZUUU", "AFTERNOON", "2026-08-17T15:59:59Z", "2026-08-19T08:00:00Z", "then"),
        ("ZUUU", "AFTERNOON", "2026-08-19T15:59:59Z", "2026-08-20T04:00:01Z", "later"),
    ])
    assert _chosen(conn) == "then"
    assert _chosen(conn, datetime(2026, 8, 21, 4, tzinfo=UTC)) == "later"


def test_a_day_before_any_artifact_is_not_projected() -> None:
    conn = _store([("ZUUU", "AFTERNOON", "2026-08-19T15:59:59Z", "2026-08-21T08:00:00Z", "later")])
    assert _chosen(conn) is None


def test_the_bucket_preference_comes_before_the_newest_training() -> None:
    conn = _store([
        ("ZUUU", "MORNING", "2026-08-18T15:59:59Z", "2026-08-19T08:00:00Z", "morning"),
        ("ZUUU", "AFTERNOON", "2026-08-16T15:59:59Z", "2026-08-19T09:00:00Z", "afternoon-old"),
        ("ZUUU", "AFTERNOON", "2026-08-17T15:59:59.000Z", "2026-08-19T10:00:00Z", "afternoon"),
    ])
    assert _chosen(conn) == "afternoon"
    # Before any afternoon artifact existed, the morning one serves; the
    # first afternoon one displaces it although its training is older.
    assert _chosen(conn, datetime(2026, 8, 19, 8, 30, tzinfo=UTC)) == "morning"
    assert _chosen(conn, datetime(2026, 8, 19, 9, 30, tzinfo=UTC)) == "afternoon-old"


def test_a_chosen_artifact_without_a_distribution_does_not_fall_back() -> None:
    conn = _store([
        ("ZUUU", "AFTERNOON", "2026-08-16T15:59:59Z", "2026-08-18T08:00:00Z", "usable"),
        ("ZUUU", "AFTERNOON", "2026-08-17T15:59:59Z", "2026-08-19T08:00:00Z", "empty"),
    ])
    assert _chosen(conn) is None

