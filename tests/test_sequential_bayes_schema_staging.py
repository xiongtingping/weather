from __future__ import annotations

import sqlite3

from scripts.sequential_bayes_schema_staging import build_in_memory_p1_staging_proof
from sequential_bayes_schema_contract import INDEX_DDL, TABLE_DDL


def test_in_memory_p1_staging_proves_additive_compatibility_without_changing_source_db(tmp_path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE event_daily_temps (event_date TEXT, settlement_actual REAL)")
        conn.execute("INSERT INTO event_daily_temps VALUES ('2026-07-03', 30.0)")
        conn.execute("CREATE TABLE source_observations (source_id TEXT, raw_sha256 TEXT)")
        conn.execute("INSERT INTO source_observations VALUES ('zuuu_metar_raw_amsc', 'abc')")

    proof = build_in_memory_p1_staging_proof(db)

    assert proof["ok"] is True, proof
    assert proof["staging_backend"] == "sqlite_memory_p1_namespace_skeleton_only"
    assert len(proof["first_apply"]["applied"]) == len(TABLE_DDL) + len(INDEX_DDL)
    assert proof["second_apply"]["applied"] == []
    assert proof["preexisting_p1_surface_unchanged"] is True
    assert proof["guards"]["writes_archive_db"] is False
    with sqlite3.connect(db) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "canonical_observation_events" not in names
        assert conn.execute("SELECT COUNT(*) FROM event_daily_temps").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 1


def test_in_memory_p1_staging_fails_closed_for_missing_or_unreadable_db(tmp_path) -> None:
    proof = build_in_memory_p1_staging_proof(tmp_path / "missing.db")

    assert proof["ok"] is False
    assert proof["state"] == "blocked"
    assert proof["guards"]["writes_archive_db"] is False
