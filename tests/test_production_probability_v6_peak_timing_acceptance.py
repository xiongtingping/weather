import json
import sqlite3

from scripts.production_probability_v6_peak_timing_acceptance import audit_connection


def test_acceptance_binds_public_timing_to_same_update_and_checkpoint() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE event_fact_history (
          id INTEGER PRIMARY KEY, event_date TEXT, composer_version TEXT,
          fact_json TEXT, posterior_run_id TEXT
        );
        CREATE TABLE event_fact_latest (event_date TEXT, fact_id INTEGER);
        CREATE TABLE posterior_checkpoints (
          posterior_checkpoint_id TEXT, posterior_run_id TEXT,
          last_canonical_event_id TEXT, last_strong_update_id TEXT
        );
        CREATE TABLE strong_observation_updates (
          strong_observation_update_id TEXT, path_auxiliary_json TEXT
        );
        """
    )
    timing = {
        "schema": "peak_timing_state_l3_v1",
        "ok": True,
        "state": "na",
        "na_reason": "forecast_supported_future_peak_slot_missing",
        "canonical_observation_event_id": "event-1",
        "strong_observation_update_id": "update-1",
        "forecast_evidence_snapshot_id": "forecast-1",
        "entry_peak_at": "",
        "does_not_update_physical_x": True,
        "read_only": True,
    }
    ssot = {
        "posterior_checkpoint_id": "checkpoint-1",
        "last_canonical_event_id": "event-1",
        "last_strong_update_id": "update-1",
        "peak_timing_state": timing,
        "reported_physical_x_distribution": {"37": 0.4, "38": 0.6},
    }
    conn.execute(
        "INSERT INTO event_fact_history VALUES(1,?,?,?,?)",
        (
            "2026-07-16",
            "physical_state_bayes_v6_production",
            json.dumps({"posterior_ssot_fact": ssot}),
            "posterior-1",
        ),
    )
    conn.execute("INSERT INTO event_fact_latest VALUES(?,1)", ("2026-07-16",))
    conn.execute(
        "INSERT INTO posterior_checkpoints VALUES(?,?,?,?)",
        ("checkpoint-1", "posterior-1", "event-1", "update-1"),
    )
    conn.execute(
        "INSERT INTO strong_observation_updates VALUES(?,?)",
        ("update-1", json.dumps({"peak_timing_state": timing})),
    )

    result = audit_connection(conn, event_date="2026-07-16")

    assert result["ok"] is True
    assert result["state"] == "accepted"
    assert result["peak_timing"]["entry_peak_at_na_reason"] == (
        "forecast_supported_future_peak_slot_missing"
    )
