from __future__ import annotations

import sqlite3

from scripts.source_observation_append_only_audit import build_source_observation_append_only_audit


def _schema(conn: sqlite3.Connection, *, unique_observation_id: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE source_observations (
            source_event_id TEXT PRIMARY KEY,
            source_observation_id TEXT,
            source_id TEXT,
            event_date TEXT,
            valid_time TEXT,
            fetched_at TEXT,
            raw_ref TEXT,
            raw_sha256 TEXT,
            parser_version TEXT DEFAULT ''
        )
        """
    )
    if unique_observation_id:
        conn.execute(
            "CREATE UNIQUE INDEX ux_source_observation_id ON source_observations(source_observation_id) "
            "WHERE source_observation_id IS NOT NULL AND source_observation_id <> ''"
        )


def _insert(conn: sqlite3.Connection, *, valid_time: str = "2026-07-03T09:00:00Z") -> None:
    conn.execute(
        """
        INSERT INTO source_observations(
            source_event_id,source_observation_id,source_id,event_date,
            valid_time,fetched_at,raw_ref,raw_sha256
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            "srcevt-1",
            "srcobs-1",
            "zuuu_metar_raw_amsc",
            "2026-07-03",
            valid_time,
            "2026-07-03T09:01:00Z",
            "research://raw/1",
            "a" * 64,
        ),
    )


def test_append_only_audit_separates_structural_integrity_from_valid_time_replay_readiness(tmp_path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        _schema(conn)
        _insert(conn)

    ready = build_source_observation_append_only_audit(db)

    assert ready["ok"] is True
    assert ready["append_only_contract_ok"] is True
    assert ready["strict_asof_replay_ready"] is True
    assert ready["totals"]["row_count"] == 1

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE source_observations SET valid_time='' WHERE source_event_id='srcevt-1'")
    invalid_time = build_source_observation_append_only_audit(db)

    assert invalid_time["ok"] is False
    assert invalid_time["append_only_contract_ok"] is True
    assert "source_observation_valid_time_incomplete" in invalid_time["blockers"]

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE source_observations SET parser_version=? WHERE source_event_id='srcevt-1'",
            ("posterior_ssot_projection_fact_feature_source_adapter_v1",),
        )
    legacy_excluded = build_source_observation_append_only_audit(db)
    assert legacy_excluded["append_only_contract_ok"] is True
    assert legacy_excluded["totals"]["legacy_derived_excluded_count"] == 1
    assert legacy_excluded["totals"]["missing_valid_time_count"] == 0
    assert "source_observation_valid_time_incomplete" not in legacy_excluded["blockers"]


def test_append_only_audit_rejects_missing_unique_replay_anchor_or_required_schema(tmp_path) -> None:
    no_unique = tmp_path / "no-unique.db"
    with sqlite3.connect(no_unique) as conn:
        _schema(conn, unique_observation_id=False)
        _insert(conn)
    report = build_source_observation_append_only_audit(no_unique)
    assert report["ok"] is False
    assert "source_observation_id_unique_index_missing" in report["blockers"]

    missing = tmp_path / "missing.db"
    with sqlite3.connect(missing) as conn:
        conn.execute("CREATE TABLE source_observations (source_event_id TEXT PRIMARY KEY)")
    incomplete = build_source_observation_append_only_audit(missing)
    assert incomplete["ok"] is False
    assert "source_observations_required_columns_missing" in incomplete["blockers"]
