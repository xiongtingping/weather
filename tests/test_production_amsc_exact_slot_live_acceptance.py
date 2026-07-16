from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from scripts.production_amsc_exact_slot_live_acceptance import audit


def test_exact_slot_acceptance_attests_t1_to_t3_without_claiming_t1_to_t5(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE canonical_observation_events (
              canonical_observation_event_id TEXT, target_identity TEXT, valid_time TEXT,
              report_type TEXT, state TEXT, revision_no INTEGER, first_actionable_seen_at TEXT
            );
            CREATE TABLE posterior_checkpoints (
              posterior_checkpoint_id TEXT, target_identity TEXT, posterior_run_id TEXT,
              replay_generation INTEGER, last_canonical_event_id TEXT, state TEXT,
              checkpoint_probability_schema TEXT, committed_at TEXT, reported_x_lower_bound_c INTEGER
            );
            CREATE TABLE posterior_execution_intents (posterior_run_id TEXT);
            CREATE TABLE posterior_commit_outbox (posterior_run_id TEXT);
            """
        )
        target = "physical_weather_day_v1:ZUUU:2026-07-16"
        conn.execute(
            "INSERT INTO canonical_observation_events VALUES (?,?,?,?,?,?,?)",
            ("event-1", target, "2026-07-16T07:30:00Z", "METAR", "accepted_primary", 0, "2026-07-16T07:30:35Z"),
        )
        conn.execute(
            "INSERT INTO posterior_checkpoints VALUES (?,?,?,?,?,?,?,?,?)",
            ("checkpoint-1", target, "run-1", 2, "event-1", "committed", "posterior_checkpoint_v4_physical_state_bayes_v6", "2026-07-16T07:30:35.250Z", 36),
        )
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({
        "probability": {"outcomes": [{"event_id": "event-1"}]},
        "latency_stages_ms": {"total_ingress_ms": 250.0},
        "probability_ssot_3000ms_slo_met": True,
    }), encoding="utf-8")

    result = audit(
        db=db,
        event_date="2026-07-16",
        slot_valid_at="2026-07-16T07:30:00Z",
        receipt_path=receipt,
        expect_no_order=True,
    )

    assert result["ok"] is True
    assert result["evidence"]["t1_to_t3_ms"] == 250.0
    assert result["boundary"]["attests_t1_to_t5_exchange_post"] is False
