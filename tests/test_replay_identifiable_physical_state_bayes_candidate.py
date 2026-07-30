import json
import sqlite3

from scripts.replay_identifiable_physical_state_bayes_candidate import (
    _selected_generation_p0,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE production_initial_posterior_lineage (
          posterior_checkpoint_id TEXT,
          target_identity TEXT
        );
        CREATE TABLE posterior_checkpoints (
          posterior_checkpoint_id TEXT,
          target_identity TEXT,
          replay_generation INTEGER,
          forecast_evidence_snapshot_id TEXT,
          physical_state_posterior_json TEXT,
          created_at TEXT
        );
        CREATE TABLE weak_forecast_evidence_snapshots (
          forecast_evidence_snapshot_id TEXT,
          decision_as_of TEXT,
          forecast_scenario_paths_json TEXT,
          candidate_scenario_responsibilities_json TEXT
        );
        """
    )
    return conn


def _insert(conn: sqlite3.Connection, *, suffix: str, generation: int, created: str) -> None:
    target = "physical_weather_day_v1:ZUUU:2026-07-20"
    snapshot = f"snapshot-{suffix}"
    checkpoint = f"checkpoint-{suffix}"
    conn.execute(
        "INSERT INTO weak_forecast_evidence_snapshots VALUES (?,?,?,?)",
        (snapshot, created, json.dumps({"paths": [{"scenario_id": "s"}]}), json.dumps({"s": 1})),
    )
    conn.execute(
        "INSERT INTO posterior_checkpoints VALUES (?,?,?,?,?,?)",
        (checkpoint, target, generation, snapshot, "{\"components\": [1]}", created),
    )
    conn.execute(
        "INSERT INTO production_initial_posterior_lineage VALUES (?,?)",
        (checkpoint, target),
    )


def test_strict_oos_p0_selection_rejects_rebuilt_state_after_first_metar():
    conn = _conn()
    _insert(conn, suffix="old", generation=1, created="2026-07-19T09:00:00Z")
    _insert(conn, suffix="rebuilt", generation=2, created="2026-07-20T14:00:00Z")
    selected = _selected_generation_p0(
        conn,
        "physical_weather_day_v1:ZUUU:2026-07-20",
        as_of="2026-07-19T16:01:00Z",
    )
    assert selected is not None
    assert selected["posterior_checkpoint_id"] == "checkpoint-old"


def test_strict_oos_p0_selection_fails_closed_without_pre_event_state():
    conn = _conn()
    _insert(conn, suffix="rebuilt", generation=2, created="2026-07-20T14:00:00Z")
    assert (
        _selected_generation_p0(
            conn,
            "physical_weather_day_v1:ZUUU:2026-07-20",
            as_of="2026-07-19T16:01:00Z",
        )
        is None
    )
