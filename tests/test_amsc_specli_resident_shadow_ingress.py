from __future__ import annotations

import sqlite3

from amsc_specli_resident_arrival_contract import (
    build_amsc_specli_resident_arrival_envelope,
)
from amsc_specli_resident_shadow_ingress import (
    AmscSpecliResidentShadowIngress,
    validate_amsc_specli_resident_ingress,
)
from observation_fast_lane_resident_shadow_worker import ResidentObservationFastLaneShadowWorker
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_schema_contract import apply_forward_schema
from source_observation_runtime import build_source_observation


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"


def _topology() -> dict:
    return {
        "release_delay_ms": 0,
        "synchronous_forecast_refresh_on_hot_path": False,
        "generic_task_queue_on_hot_path": False,
        "resident_canonical_and_inference_worker": True,
        "transaction_b_atomic_before_intent": True,
        "posterior_commit_outbox_durable_before_intent": True,
        "decision_reads_committed_ssot": True,
        "signer_connection_pool_resident": True,
    }


def _prewarmed() -> dict:
    return {
        "forecast_snapshot": {"ready": True, "forecast_snapshot_id": "forecast_ingress", "artifact_sha256": "forecast_artifact_ingress", "valid_until": "2026-07-03T07:30:00Z"},
        "prior_context": {"ready": True, "prior_context_id": "prior_ingress", "artifact_sha256": "prior_artifact_ingress", "valid_until": "2026-07-03T07:30:00Z"},
        "strong_lr_artifact": {"ready": True, "approved": True, "strong_lr_artifact_sha256": "strong_ingress", "valid_until": "2026-07-03T07:30:00Z"},
        "market_context": {"ready": True, "market_snapshot_id": "market_ingress", "fresh_until": "2026-07-03T07:30:00Z"},
        "resident_signer_connection_pool": {"ready": True, "pool_id": "pool_ingress", "valid_until": "2026-07-03T07:30:00Z"},
    }


def _source(*, report_type: str = "METAR", source_id: str = "zuuu_metar_raw_amsc") -> dict:
    return build_source_observation(
        source_id=source_id,
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T06:30:00Z",
        fetched_at="2026-07-03T06:31:00Z",
        response_completed_at="2026-07-03T06:31:00Z",
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref=f"fixture://{report_type}",
        raw_payload={"report_type": report_type},
        payload={
            "report_type": report_type,
            "arrival_time_quality": "observed_first_seen",
            "first_seen_at": "2026-07-03T06:31:00Z",
            "as_of_eligible_at": "2026-07-03T06:31:00Z",
        },
    ).as_dict()


def _ingress(calls: list[dict]) -> AmscSpecliResidentShadowIngress:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)

    def runner(_conn, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "state": "shadow_committed_within_slo"}

    worker = ResidentObservationFastLaneShadowWorker(
        conn,
        topology=_topology(),
        prewarmed_components=_prewarmed(),
        runner=runner,
        timestamp_observer=lambda: "2026-07-03T06:31:00.050Z",
    )
    return AmscSpecliResidentShadowIngress(worker)


def _dispatch() -> dict:
    return {
        "target_identity": TARGET,
        "event_date": "2026-07-03",
        "decision_as_of": "2026-07-03T06:31:01Z",
        "canonicalized_at": "2026-07-03T06:31:00.050Z",
        "posterior_transaction_inputs": {"shadow_only": True, "correlation_id": "corr_amsc_ingress"},
        "decision_cycle_inputs": {"outbox_event_id": "outbox_amsc_ingress"},
    }


def test_primary_metar_and_speci_dispatch_without_awc_wait_or_generic_queue() -> None:
    calls: list[dict] = []
    ingress = _ingress(calls)

    first = ingress.process_arrival_envelope(
        arrival_envelope=build_amsc_specli_resident_arrival_envelope(
            _source(report_type="METAR")
        ),
        **_dispatch(),
    )
    second = ingress.process_arrival_envelope(
        arrival_envelope=build_amsc_specli_resident_arrival_envelope(
            _source(report_type="SPECI")
        ),
        **_dispatch(),
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert len(calls) == 2
    assert first["ingress"]["boundary"]["no_awc_wait"] is True
    assert first["ingress"]["boundary"]["no_generic_task_queue"] is True
    assert first["arrival_envelope"]["first_actionable_seen_at"] == "2026-07-03T06:31:00Z"
    assert first["places_orders"] is False


def test_ingress_three_second_hard_boundary_is_not_relaxable() -> None:
    source = _source()
    boundary = validate_amsc_specli_resident_ingress(
        source_observation=source,
        first_actionable_seen_at="2026-07-03T06:31:00Z",
        decision_as_of="2026-07-03T06:31:03Z",
        canonicalized_at="2026-07-03T06:31:00.050Z",
    )
    expired = validate_amsc_specli_resident_ingress(
        source_observation=source,
        first_actionable_seen_at="2026-07-03T06:31:00Z",
        decision_as_of="2026-07-03T06:31:03.001Z",
        canonicalized_at="2026-07-03T06:31:00.050Z",
    )

    assert boundary["ok"] is True
    assert boundary["boundary"]["hard_t1_to_dispatch_ms"] == 3000
    assert expired["ok"] is False
    assert "amsc_specli_ingress_observation_hard_deadline_exceeded" in expired["blockers"]


def test_reference_source_or_missing_real_actionable_time_blocks_before_canonical_write() -> None:
    calls: list[dict] = []
    ingress = _ingress(calls)

    reference = ingress.process_received_observation(
        source_observation=_source(source_id="zuuu_metar_raw_awc"),
        first_actionable_seen_at="2026-07-03T06:31:00Z",
        **_dispatch(),
    )
    missing_time = ingress.process_received_observation(
        source_observation=_source(),
        first_actionable_seen_at="",
        **_dispatch(),
    )

    assert reference["state"] == "blocked_before_canonical_write"
    assert "amsc_specli_ingress_primary_source_required" in reference["ingress"]["blockers"]
    assert missing_time["state"] == "blocked_before_canonical_write"
    assert "amsc_specli_ingress_first_actionable_seen_at_missing_or_invalid" in missing_time["ingress"]["blockers"]
    assert calls == []


def test_arrival_envelope_cannot_reset_t1_for_a_refetched_old_report() -> None:
    calls: list[dict] = []
    ingress = _ingress(calls)
    source = _source()
    source["response_completed_at"] = "2026-07-03T06:40:00Z"
    source["fetched_at"] = "2026-07-03T06:40:00Z"
    envelope = build_amsc_specli_resident_arrival_envelope(source)
    dispatch = _dispatch()
    dispatch["decision_as_of"] = "2026-07-03T06:40:01Z"
    dispatch["canonicalized_at"] = "2026-07-03T06:40:00.050Z"

    result = ingress.process_arrival_envelope(
        arrival_envelope=envelope,
        **dispatch,
    )

    assert result["state"] == "blocked_before_canonical_write"
    assert "amsc_specli_ingress_observation_hard_deadline_exceeded" in result["ingress"]["blockers"]
    assert calls == []
