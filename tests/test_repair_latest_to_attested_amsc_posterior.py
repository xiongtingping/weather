from __future__ import annotations

import json
import sqlite3

from posterior_materialization_attestation_runtime import append_posterior_materialization_commit_attestation
from scripts.repair_latest_to_attested_amsc_posterior import (
    CONFIRMATION,
    apply_repair,
    build_repair_plan,
)


def _payload(snapshot: str, run: str) -> str:
    return json.dumps(
        {
            "fact_snapshot_id": snapshot,
            "posterior_run_id": run,
            "posterior_ssot_fact": {
                "posterior_composer_schema": "sequential_remaining_warming_bayes_runtime_v31"
            },
            "source_to_posterior_timing": {"schema": "source_to_posterior_timing_v1"},
        }
    )


def _db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE event_fact_history(
            id INTEGER PRIMARY KEY,slug TEXT,event_date TEXT,fact_as_of TEXT,
            composer_version TEXT,fact_json TEXT)"""
    )
    conn.execute(
        """CREATE TABLE event_fact_latest(
            slug TEXT,event_date TEXT,fact_id INTEGER,updated_at TEXT,
            fact_snapshot_id TEXT,correlation_id TEXT,
            PRIMARY KEY(slug,event_date))"""
    )
    conn.execute(
        "INSERT INTO event_fact_history VALUES(1,'slug','2026-07-14','2026-07-14T00:00:00Z','amsc_zuuu_metar_taf_fetcher_v1','{}')"
    )
    conn.execute(
        "INSERT INTO event_fact_history VALUES(2,'slug','2026-07-14','2026-07-14T00:00:00Z','writer',?)",
        (_payload("fact_attested", "post_attested"),),
    )
    conn.execute(
        "INSERT INTO event_fact_history VALUES(3,'slug','2026-07-14','2026-07-14T00:01:00Z','legacy',?)",
        (_payload("fact_copy", "post_copy"),),
    )
    conn.execute(
        "INSERT INTO event_fact_latest VALUES('slug','2026-07-14',3,'2026-07-14T00:01:00Z','fact_copy','corr')"
    )
    append_posterior_materialization_commit_attestation(
        conn,
        materialized_fact_id=2,
        posterior_committed_at="2026-07-14T00:00:00.100Z",
        fact_snapshot_id="fact_attested",
        posterior_run_id="post_attested",
    )
    conn.close()


def _backup(source, target) -> None:
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dest:
        src.backup(dest)


def test_repair_repoints_only_to_same_event_attested_amsc_posterior(tmp_path) -> None:
    db = tmp_path / "archive.db"
    _db(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    plan = build_repair_plan(conn, slug="slug", event_date="2026-07-14")
    conn.close()
    assert plan["state"] == "ready_to_repoint"
    assert plan["current_fact_id"] == 3
    assert plan["candidate_fact_id"] == 2

    backup = tmp_path / "archive.before-repair.db"
    _backup(db, backup)
    result = apply_repair(
        db, slug="slug", event_date="2026-07-14", confirm=CONFIRMATION,
        writer_quiescent=True, backup_path=backup,
    )

    assert result["state"] == "repointed"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT fact_id FROM event_fact_latest").fetchone()[0] == 2
    conn.close()


def test_repair_blocks_when_newer_amsc_source_requires_materialization(tmp_path) -> None:
    db = tmp_path / "archive.db"
    _db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO event_fact_history VALUES(4,'slug','2026-07-14','2026-07-14T00:02:00Z','amsc_zuuu_metar_taf_fetcher_v1','{}')"
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    plan = build_repair_plan(conn, slug="slug", event_date="2026-07-14")
    conn.close()

    assert plan["state"] == "blocked"
    assert plan["blockers"] == ["newer_amsc_source_fact_requires_fresh_materialization"]
