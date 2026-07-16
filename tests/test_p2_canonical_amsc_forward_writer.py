from __future__ import annotations

import sqlite3

import pytest
import p2_canonical_amsc_forward_writer as writer

from p2_canonical_amsc_forward_writer import (
    apply_p2_canonical_amsc_forward_append,
    build_p2_canonical_amsc_forward_append_plan,
    build_physical_weather_day_target_identity,
)
from sequential_bayes_schema_contract import apply_forward_schema
from reported_x_successor_schema_contract import COLUMNS as REPORTED_X_COLUMNS
from tests.test_strict_amsc_hard_observation_reader import (
    _initialize_source_observations,
    _insert,
    _strict_primary,
)


def _ready_db(tmp_path) -> tuple[sqlite3.Connection, object]:
    raw = tmp_path / "raw"
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)
    apply_forward_schema(conn)
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(posterior_checkpoints)")}
    for column, sql_type in REPORTED_X_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE posterior_checkpoints ADD COLUMN {column} {sql_type}")
    _insert(conn, _strict_primary(raw))
    conn.commit()
    return conn, raw


def test_target_identity_is_physical_station_day_not_market_or_resolution() -> None:
    assert build_physical_weather_day_target_identity(
        station_id="zuuu", event_date="2026-07-12"
    ) == "physical_weather_day_v1:ZUUU:2026-07-12"
    with pytest.raises(ValueError, match="station_id_invalid"):
        build_physical_weather_day_target_identity(station_id="chengdu", event_date="2026-07-12")


def test_forward_plan_reads_strict_primary_and_refuses_market_target_identity(tmp_path) -> None:
    conn, raw = _ready_db(tmp_path)
    try:
        ready = build_p2_canonical_amsc_forward_append_plan(
            conn,
            event_date="2026-07-12",
            raw_archive_dir=raw,
            decision_as_of="2026-07-12T01:00:00Z",
        )
        assert ready["ok"] is True, ready
        assert ready["target_identity"] == "physical_weather_day_v1:ZUUU:2026-07-12"
        assert ready["proposal_summary"]["accepted_primary_event_count"] == 1
        assert ready["write_scope"]["creates_posterior"] is False
        assert ready["write_scope"]["awc_primary_or_substitute"] is False

        blocked = build_p2_canonical_amsc_forward_append_plan(
            conn,
            event_date="2026-07-12",
            raw_archive_dir=raw,
            decision_as_of="2026-07-12T01:00:00Z",
            target_identity="highest-temperature-in-chengdu-on-july-12-2026",
        )
        assert blocked["ok"] is False
        assert "p2_canonical_target_identity_must_be_physical_weather_day" in blocked["blockers"]
    finally:
        conn.close()


def test_forward_apply_is_atomic_idempotent_and_does_not_write_posterior_surfaces(tmp_path) -> None:
    conn, raw = _ready_db(tmp_path)
    try:
        first = apply_p2_canonical_amsc_forward_append(
            conn,
            event_date="2026-07-12",
            raw_archive_dir=raw,
            decision_as_of="2026-07-12T01:00:00Z",
        )
        assert first["ok"] is True, first
        assert first["persistence"]["inserted_events"] == 1
        assert first["persistence"]["inserted_relations"] == 1
        assert first["writes"]["forbidden_table_changes"] == {}
        row = conn.execute(
            """
            SELECT target_identity,report_type,first_transport_seen_at,first_actionable_seen_at,state
            FROM canonical_observation_events
            """
        ).fetchone()
        assert row == (
            "physical_weather_day_v1:ZUUU:2026-07-12",
            "METAR",
            "2026-07-11T16:00:55Z",
            "2026-07-11T16:00:55Z",
            "accepted_primary",
        )
        assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0

        second = apply_p2_canonical_amsc_forward_append(
            conn,
            event_date="2026-07-12",
            raw_archive_dir=raw,
            decision_as_of="2026-07-12T01:00:00Z",
        )
        assert second["persistence"]["inserted_events"] == 0
        # Existing physical identity is resolved in canonicalization; the
        # writer only receives the already-bound confirmation relation.
        assert second["persistence"]["existing_events"] == 0
        assert second["persistence"]["inserted_relations"] == 0
        assert second["persistence"]["existing_relations"] == 1
    finally:
        conn.close()


def test_forward_apply_refuses_an_outer_transaction(tmp_path) -> None:
    conn, raw = _ready_db(tmp_path)
    try:
        conn.execute("BEGIN")
        with pytest.raises(RuntimeError, match="outer_transaction_forbidden"):
            apply_p2_canonical_amsc_forward_append(
                conn,
                event_date="2026-07-12",
                raw_archive_dir=raw,
                decision_as_of="2026-07-12T01:00:00Z",
            )
        conn.rollback()
    finally:
        conn.close()


def test_forward_apply_rejects_forbidden_write_before_execution(tmp_path, monkeypatch) -> None:
    conn, raw = _ready_db(tmp_path)

    def attempt_forbidden_write(connection, proposal):
        connection.execute("INSERT INTO strong_observation_updates DEFAULT VALUES")
        raise AssertionError("SQLite authorizer must deny before execution")

    monkeypatch.setattr(writer, "persist_canonical_observation_proposal", attempt_forbidden_write)
    try:
        with pytest.raises(RuntimeError, match="forbidden_write_rejected"):
            apply_p2_canonical_amsc_forward_append(
                conn,
                event_date="2026-07-12",
                raw_archive_dir=raw,
                decision_as_of="2026-07-12T01:00:00Z",
            )
        assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
        assert conn.in_transaction is False
    finally:
        conn.close()
