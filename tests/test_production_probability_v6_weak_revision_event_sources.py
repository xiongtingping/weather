import json
import sqlite3

import pytest

from production_probability_v6_weak_revision_event_runtime import (
    _assert_no_visible_unconsumed_event_before_revision,
    _changed_source_revision,
)


def _snapshot(*rows):
    return {"source_revisions_json": json.dumps(list(rows))}


def test_changed_source_revision_selects_latest_new_revision() -> None:
    old = _snapshot(
        {"source_id": "ecmwf", "source_revision_id": "e1"},
        {"source_id": "taf", "source_revision_id": "t1"},
    )
    changed, missing = _changed_source_revision(
        old,
        [
            {"source_id": "ecmwf", "source_revision_id": "e2", "available_at": "2026-07-16T10:00:00Z"},
            {"source_id": "taf", "source_revision_id": "t1", "available_at": "2026-07-16T09:00:00Z"},
            {"source_id": "gfs", "source_revision_id": "g1", "available_at": "2026-07-16T11:00:00Z"},
        ],
    )
    assert missing == []
    assert changed == {
        "source_id": "gfs",
        "source_revision_id": "g1",
        "available_at": "2026-07-16T11:00:00Z",
    }


def test_changed_source_revision_fails_closed_on_source_regression() -> None:
    old = _snapshot(
        {"source_id": "ecmwf", "source_revision_id": "e1"},
        {"source_id": "taf", "source_revision_id": "t1"},
    )
    changed, missing = _changed_source_revision(
        old,
        [{"source_id": "ecmwf", "source_revision_id": "e2", "available_at": "2026-07-16T10:00:00Z"}],
    )
    assert changed["source_revision_id"] == "e2"
    assert missing == ["taf"]


def test_changed_source_revision_ignores_decision_time_only_change() -> None:
    rows = [{"source_id": "ecmwf", "source_revision_id": "e1", "available_at": "2026-07-16T10:00:00Z"}]
    changed, missing = _changed_source_revision(_snapshot(*rows), rows)
    assert changed is None
    assert missing == []


def test_physical_revision_requires_visible_canonical_events_to_drain_first() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE canonical_observation_events("
        "canonical_observation_event_id TEXT,target_identity TEXT,station_id TEXT,"
        "selected_source_id TEXT,state TEXT,valid_time TEXT,first_actionable_seen_at TEXT,"
        "supersedes_event_id TEXT)"
    )
    conn.execute(
        "INSERT INTO canonical_observation_events VALUES(?,?,?,?,?,?,?,?)",
        (
            "event-visible-first",
            "physical_weather_day_v1:ZUUU:2026-07-19",
            "ZUUU",
            "zuuu_metar_raw_amsc",
            "accepted_primary",
            "2026-07-19T04:15:00Z",
            "2026-07-19T04:16:00Z",
            "",
        ),
    )
    with pytest.raises(ValueError, match="must_drain_first"):
        _assert_no_visible_unconsumed_event_before_revision(
            conn,
            target="physical_weather_day_v1:ZUUU:2026-07-19",
            checkpoint={"consumed_event_ids_json": "[]"},
            decision_as_of="2026-07-19T04:30:00Z",
        )
    _assert_no_visible_unconsumed_event_before_revision(
        conn,
        target="physical_weather_day_v1:ZUUU:2026-07-19",
        checkpoint={"consumed_event_ids_json": json.dumps(["event-visible-first"])},
        decision_as_of="2026-07-19T04:30:00Z",
    )
