from __future__ import annotations

import sqlite3

from canonical_observation_runtime import canonicalize_observations
from canonical_observation_writer import persist_canonical_observation_proposal
from sequential_bayes_schema_contract import apply_forward_schema
from source_observation_runtime import build_source_observation


def _source(source_id: str) -> dict:
    return build_source_observation(
        source_id=source_id,
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T06:30:00Z",
        fetched_at="2026-07-03T06:31:00Z",
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref=f"fixture://{source_id}",
        raw_payload={"source": source_id},
        payload={"report_type": "METAR"},
    ).as_dict()


def _proposal() -> dict:
    return canonicalize_observations(
        [_source("zuuu_metar_raw_awc"), _source("zuuu_metar_raw_amsc")],
        target_identity="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        decision_as_of="2026-07-03T07:00:00Z",
    )


def test_writer_is_append_only_and_idempotent(tmp_path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        apply_forward_schema(conn)
        first = persist_canonical_observation_proposal(conn, _proposal())
        conn.commit()

    assert first["inserted_events"] == 1
    assert first["inserted_relations"] == 2
    assert first["updates_existing_rows"] is False
    with sqlite3.connect(db) as conn:
        selected = conn.execute(
            "SELECT selected_source_id, state FROM canonical_observation_events"
        ).fetchone()
        assert selected == ("zuuu_metar_raw_amsc", "accepted_primary")
        second = persist_canonical_observation_proposal(conn, _proposal())
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM canonical_observation_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM canonical_observation_event_sources").fetchone()[0] == 2

    assert second["inserted_events"] == 0
    assert second["existing_events"] == 1
    assert second["inserted_relations"] == 0
    assert second["existing_relations"] == 2
