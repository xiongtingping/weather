from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from canonical_observation_runtime import canonicalize_observations
from canonical_observation_writer import persist_canonical_observation_proposal
from observation_fast_lane_contract import build_observation_fast_lane_contract
from observation_fast_lane_shadow_telemetry_writer import (
    append_observation_fast_lane_shadow_latency_packet,
)
from sequential_bayes_public_ssot_shadow_writer import (
    initialize_isolated_public_ssot_shadow_schema,
)
from sequential_bayes_schema_contract import apply_forward_schema
from source_observation_runtime import build_source_observation


ROOT = Path(__file__).resolve().parents[1]


def test_shadow_db_slo_audit_reads_only_isolated_packets(tmp_path: Path) -> None:
    db_path = tmp_path / "p10-shadow.db"
    conn = sqlite3.connect(db_path)
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    source = build_source_observation(
        source_id="zuuu_metar_raw_amsc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T06:30:00Z",
        fetched_at="2026-07-03T06:31:00Z",
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref="fixture://shadow-db-audit",
        raw_payload={"source": "amsc"},
        payload={"report_type": "METAR"},
    ).as_dict()
    proposal = canonicalize_observations(
        [source],
        target_identity="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        decision_as_of="2026-07-03T06:31:01Z",
    )
    persist_canonical_observation_proposal(conn, proposal)
    event_id = proposal["events"][0]["canonical_observation_event_id"]
    fast_lane = build_observation_fast_lane_contract(
        correlation_id="corr_shadow_db_audit_123456",
        timestamps={
            "first_actionable_seen_at": "2026-07-03T06:31:00.000Z",
            "canonicalized_at": "2026-07-03T06:31:00.050Z",
            "transaction_b_committed_at": "2026-07-03T06:31:00.400Z",
            "decision_intent_durable_at": "2026-07-03T06:31:00.700Z",
        },
        topology={
            "release_delay_ms": 0,
            "synchronous_forecast_refresh_on_hot_path": False,
            "generic_task_queue_on_hot_path": False,
            "resident_canonical_and_inference_worker": True,
            "transaction_b_atomic_before_intent": True,
            "posterior_commit_outbox_durable_before_intent": True,
            "decision_reads_committed_ssot": True,
            "signer_connection_pool_resident": True,
        },
        mode="shadow",
    )
    append_observation_fast_lane_shadow_latency_packet(
        conn,
        packet={
            "correlation_id": "corr_shadow_db_audit_123456",
            "canonical_observation_event_id": event_id,
            "event_date": "2026-07-03",
            "posterior_run_id": "post_shadow_db_audit_123456",
            "fact_snapshot_id": "factsnap_shadow_db_audit_123456",
            "fast_lane": fast_lane,
            "created_at": "2026-07-03T06:31:00.700Z",
        },
    )
    conn.close()

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/observation_fast_lane_shadow_db_slo_audit.py"), "--db", str(db_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    report = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert report["ok"] is True
    assert report["overall"]["t1_to_t3"]["p99_ms"] == 400.0
    assert report["input"]["execution_mode"] == "isolated_shadow_only_read_only"
