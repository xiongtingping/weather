from __future__ import annotations

import sqlite3

import pytest

from canonical_observation_runtime import canonicalize_observations
from canonical_observation_writer import persist_canonical_observation_proposal
from observation_fast_lane_contract import build_observation_fast_lane_contract
from observation_fast_lane_shadow_telemetry_writer import (
    PACKET_TABLE,
    append_observation_fast_lane_shadow_latency_packet,
)
from sequential_bayes_public_ssot_shadow_writer import (
    initialize_isolated_public_ssot_shadow_schema,
)
from sequential_bayes_schema_contract import apply_forward_schema
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError
from source_observation_runtime import build_source_observation


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"


def _conn(*, isolated: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    if isolated:
        initialize_isolated_public_ssot_shadow_schema(conn)
    return conn


def _canonical_event(conn: sqlite3.Connection) -> str:
    source = build_source_observation(
        source_id="zuuu_metar_raw_amsc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T06:30:00Z",
        fetched_at="2026-07-03T06:31:00Z",
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref="fixture://telemetry",
        raw_payload={"source": "amsc"},
        payload={"report_type": "METAR"},
    ).as_dict()
    proposal = canonicalize_observations(
        [source],
        target_identity=TARGET,
        event_date="2026-07-03",
        decision_as_of="2026-07-03T06:31:01Z",
    )
    persist_canonical_observation_proposal(conn, proposal)
    return str(proposal["events"][0]["canonical_observation_event_id"])


def _packet(event_id: str) -> dict:
    fast_lane = build_observation_fast_lane_contract(
        correlation_id="corr_telemetry_123456",
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
    return {
        "correlation_id": "corr_telemetry_123456",
        "canonical_observation_event_id": event_id,
        "event_date": "2026-07-03",
        "posterior_run_id": "post_telemetry_123456",
        "fact_snapshot_id": "factsnap_telemetry_123456",
        "fast_lane": fast_lane,
        "latency_timestamp_sources": {
            "transaction_b_committed_at": "post_transaction_b_return_observer",
        },
        "created_at": "2026-07-03T06:31:00.700Z",
    }


def test_shadow_telemetry_is_append_only_and_exact_retry_is_idempotent() -> None:
    conn = _conn()
    packet = _packet(_canonical_event(conn))

    first = append_observation_fast_lane_shadow_latency_packet(conn, packet=packet)
    retry = append_observation_fast_lane_shadow_latency_packet(conn, packet=packet)

    assert first["state"] == "appended"
    assert retry["state"] == "idempotent"
    assert conn.execute(f"SELECT COUNT(*) FROM {PACKET_TABLE}").fetchone()[0] == 1
    changed = dict(packet)
    changed["created_at"] = "2026-07-03T06:31:00.701Z"
    with pytest.raises(ShadowTransactionIntegrityError, match="existing_packet_conflict"):
        append_observation_fast_lane_shadow_latency_packet(conn, packet=changed)


def test_shadow_telemetry_refuses_nonisolated_database() -> None:
    conn = _conn(isolated=False)
    with pytest.raises(ShadowTransactionIntegrityError, match="shadow_manifest_missing"):
        append_observation_fast_lane_shadow_latency_packet(
            conn,
            packet=_packet("canobs_missing"),
        )
