from __future__ import annotations

import sqlite3

import pytest

from observation_fast_lane_resident_shadow_worker import ResidentObservationFastLaneShadowWorker
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_schema_contract import apply_forward_schema
from source_observation_runtime import build_source_observation


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"


def _topology(**overrides) -> dict:
    value = {
        "release_delay_ms": 0,
        "synchronous_forecast_refresh_on_hot_path": False,
        "generic_task_queue_on_hot_path": False,
        "resident_canonical_and_inference_worker": True,
        "transaction_b_atomic_before_intent": True,
        "posterior_commit_outbox_durable_before_intent": True,
        "decision_reads_committed_ssot": True,
        "signer_connection_pool_resident": True,
    }
    value.update(overrides)
    return value


def _prewarmed() -> dict:
    return {
        "forecast_snapshot": {
            "ready": True,
            "forecast_snapshot_id": "forecast_snapshot_fixture",
            "artifact_sha256": "forecast-artifact-fixture",
            "valid_until": "2026-07-03T07:30:00Z",
        },
        "prior_context": {
            "ready": True,
            "prior_context_id": "prior_context_fixture",
            "artifact_sha256": "prior-artifact-fixture",
            "valid_until": "2026-07-03T07:30:00Z",
        },
        "strong_lr_artifact": {
            "ready": True,
            "approved": True,
            "strong_lr_artifact_sha256": "strong-artifact-fixture",
            "valid_until": "2026-07-03T07:30:00Z",
        },
        "market_context": {
            "ready": True,
            "market_snapshot_id": "market_snapshot_fixture",
            "fresh_until": "2026-07-03T07:30:00Z",
        },
        "resident_signer_connection_pool": {
            "ready": True,
            "pool_id": "signer_pool_fixture",
            "valid_until": "2026-07-03T07:30:00Z",
        },
    }


def _source(*, report_type: str = "METAR") -> dict:
    return build_source_observation(
        source_id="zuuu_metar_raw_amsc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T06:30:00Z",
        fetched_at="2026-07-03T06:31:00Z",
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref=f"fixture://resident-amsc-{report_type.lower()}",
        raw_payload={"source": "amsc", "report_type": report_type},
        payload={"report_type": report_type},
    ).as_dict()


def _worker(
    conn: sqlite3.Connection,
    calls: list[dict],
    *,
    timestamp_observer=None,
) -> ResidentObservationFastLaneShadowWorker:
    timestamp_observer = timestamp_observer or (lambda: "2026-07-03T06:31:00.050Z")
    def runner(_conn, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "state": "shadow_committed_within_slo", "shadow_only": True, "places_orders": False}

    return ResidentObservationFastLaneShadowWorker(
        conn,
        topology=_topology(),
        prewarmed_components=_prewarmed(),
        runner=runner,
        timestamp_observer=timestamp_observer,
    )


def _process(worker: ResidentObservationFastLaneShadowWorker) -> dict:
    return worker.process_source_observation(
        source_observation=_source(),
        target_identity=TARGET,
        event_date="2026-07-03",
        decision_as_of="2026-07-03T06:59:59Z",
        canonicalized_at="2026-07-03T06:31:00.050Z",
        posterior_transaction_inputs={"shadow_only": True, "correlation_id": "corr_resident_001"},
        decision_cycle_inputs={"outbox_event_id": "outbox_resident_001"},
    )


def test_resident_worker_persists_canonical_fact_then_dispatches_same_process_fast_lane() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    calls: list[dict] = []
    worker = _worker(
        conn,
        calls,
        timestamp_observer=lambda: "2026-07-03T06:31:00.050Z",
    )

    result = _process(worker)

    assert result["ok"] is True
    assert result["state"] == "shadow_fast_lane_committed"
    assert conn.execute("SELECT COUNT(*) FROM canonical_observation_events").fetchone()[0] == 1
    assert len(calls) == 1
    assert calls[0]["posterior_transaction_inputs"]["canonical_observation_event_id"] == result["canonical_observation_event_id"]
    assert len(calls[0]["posterior_transaction_inputs"]["resident_fast_lane_prewarm_dependency_identity"]) == 64
    assert calls[0]["first_actionable_seen_at"] == "2026-07-03T06:31:00Z"
    assert calls[0]["canonicalized_at"] == "2026-07-03T06:31:00.050Z"
    assert result["canonicalized_at"] == "2026-07-03T06:31:00.050Z"
    assert result["boundary"]["caller_canonicalized_at_not_used_for_p10_telemetry"] is True
    assert result["boundary"]["no_generic_task_queue"] is True
    assert result["places_orders"] is False


def test_resident_worker_never_dispatches_a_completed_event_twice() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    calls: list[dict] = []
    worker = _worker(conn, calls)

    first = _process(worker)
    second = _process(worker)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["state"] == "canonical_event_already_materialized"
    assert len(calls) == 1


def test_resident_worker_retains_canonical_fact_when_fast_lane_is_blocked_for_retry() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    calls: list[dict] = []

    def blocked_runner(_conn, **kwargs):
        calls.append(kwargs)
        return {"ok": False, "state": "blocked", "reason": "artifact_not_approved", "shadow_only": True}

    worker = ResidentObservationFastLaneShadowWorker(
        conn,
        topology=_topology(),
        prewarmed_components=_prewarmed(),
        runner=blocked_runner,
        timestamp_observer=lambda: "2026-07-03T06:31:00.050Z",
    )

    result = _process(worker)

    assert result["ok"] is False
    assert result["state"] == "canonical_persisted_fast_lane_blocked"
    assert conn.execute("SELECT COUNT(*) FROM canonical_observation_events").fetchone()[0] == 1
    assert len(calls) == 1


def test_resident_worker_does_not_let_stale_market_context_block_probability_transaction() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    calls: list[dict] = []
    prewarm = _prewarmed()
    prewarm["market_context"]["fresh_until"] = "2026-07-03T06:30:30Z"
    worker = ResidentObservationFastLaneShadowWorker(
        conn,
        topology=_topology(),
        prewarmed_components=prewarm,
        runner=lambda _conn, **kwargs: calls.append(kwargs) or {"ok": True},
        timestamp_observer=lambda: "2026-07-03T06:31:00.050Z",
    )

    result = _process(worker)

    assert result["ok"] is True
    assert result["state"] == "shadow_fast_lane_committed"
    assert conn.execute("SELECT COUNT(*) FROM canonical_observation_events").fetchone()[0] == 1
    assert len(calls) == 1


def test_resident_worker_owns_canonicalization_cutoff_and_t1_deadline_not_caller_times() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    calls: list[dict] = []
    worker = _worker(
        conn,
        calls,
        timestamp_observer=lambda: "2026-07-03T06:31:00.050Z",
    )

    result = worker.process_source_observation(
        source_observation=_source(),
        target_identity=TARGET,
        event_date="2026-07-03",
        decision_as_of="2099-01-01T00:00:00Z",
        canonicalized_at="2099-01-01T00:00:00Z",
        posterior_transaction_inputs={"shadow_only": True, "correlation_id": "corr_resident_clock_001"},
        decision_cycle_inputs={"outbox_event_id": "outbox_resident_clock_001"},
    )

    assert result["ok"] is True
    assert result["dispatch_observed_at"] == "2026-07-03T06:31:00.050Z"
    assert result["boundary"]["caller_decision_as_of_not_used_for_canonicalization_cutoff"] is True

    late_calls: list[dict] = []
    late_worker = _worker(
        conn,
        late_calls,
        timestamp_observer=lambda: "2026-07-03T06:31:03.001Z",
    )
    late = late_worker.process_source_observation(
        source_observation=_source(report_type="SPECI"),
        target_identity=TARGET,
        event_date="2026-07-03",
        decision_as_of="2026-07-03T06:31:00.001Z",
        canonicalized_at="2026-07-03T06:31:00.001Z",
        posterior_transaction_inputs={"shadow_only": True, "correlation_id": "corr_resident_clock_002"},
        decision_cycle_inputs={"outbox_event_id": "outbox_resident_clock_002"},
    )

    assert late["state"] == "canonical_persisted_fast_lane_blocked"
    assert late["reason"] == "resident_fast_lane_observation_hard_deadline_exceeded_before_transaction_b"
    assert late_calls == []


def test_resident_worker_rejects_bad_topology_or_nonisolated_database_before_canonical_write() -> None:
    normal = sqlite3.connect(":memory:")
    apply_forward_schema(normal)
    with pytest.raises(ValueError, match="resident_fast_lane_shadow_manifest_missing"):
        _worker(normal, [])

    isolated = sqlite3.connect(":memory:")
    apply_forward_schema(isolated)
    initialize_isolated_public_ssot_shadow_schema(isolated)
    with pytest.raises(ValueError, match="resident_fast_lane_topology_invalid"):
        ResidentObservationFastLaneShadowWorker(
            isolated,
            topology=_topology(generic_task_queue_on_hot_path=True),
            prewarmed_components=_prewarmed(),
        )
    assert isolated.execute("SELECT COUNT(*) FROM canonical_observation_events").fetchone()[0] == 0
