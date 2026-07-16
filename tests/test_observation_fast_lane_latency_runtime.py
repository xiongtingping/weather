from __future__ import annotations

from datetime import datetime, timedelta, timezone

from observation_fast_lane_contract import build_observation_fast_lane_contract
from observation_fast_lane_latency_runtime import build_observation_fast_lane_latency_slo_report


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


def _packet(*, correlation_id: str, t3_ms: int, t4_ms: int, t5_ms: int | None, report_type: str = "SPECI") -> dict:
    base = datetime(2026, 7, 3, 6, 30, tzinfo=timezone.utc)

    def at(offset_ms: int) -> str:
        return (base + timedelta(milliseconds=offset_ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    timestamps = {
        "first_actionable_seen_at": at(0),
        "canonicalized_at": at(50),
        "transaction_b_committed_at": at(t3_ms),
        "decision_intent_durable_at": at(t4_ms),
    }
    mode = "shadow"
    if t5_ms is not None:
        timestamps["order_submitted_at"] = at(t5_ms)
        mode = "live"
    return {
        "event_date": "2026-07-03",
        "event_type": report_type,
        "correlation_id": correlation_id,
        "fast_lane": build_observation_fast_lane_contract(
            correlation_id=correlation_id,
            timestamps=timestamps,
            topology=_topology(),
            mode=mode,
        ),
    }


def test_latency_report_keeps_shadow_t1_to_t4_separate_from_live_t1_to_t5() -> None:
    records = [
        _packet(correlation_id=f"corr_live_{index}", t3_ms=t3, t4_ms=700, t5_ms=t5)
        for index, (t3, t5) in enumerate(((100, 1000), (200, 1500), (300, 2000), (400, 2500), (450, 3500)))
    ]
    records.append(_packet(correlation_id="corr_shadow_1", t3_ms=300, t4_ms=700, t5_ms=None, report_type="METAR"))

    report = build_observation_fast_lane_latency_slo_report(records, require_live_t1_to_t5=True)

    assert report["ok"] is False
    assert "fast_lane_t1_to_t5_p99_slo_exceeded" in report["blockers"]
    assert report["overall"]["t1_to_t3"]["p50_ms"] == 300.0
    assert report["overall"]["t1_to_t3"]["p99_ms"] == 450.0
    assert report["overall"]["shadow_t1_to_t4"]["sample_count"] == 1
    assert report["overall"]["live_t1_to_t5"]["sample_count"] == 5
    assert report["overall"]["live_t1_to_t5"]["p99_ms"] == 3500.0
    assert len(report["by_event_date_and_type"]) == 2


def test_latency_report_rejects_unbound_or_mismatched_correlation_without_hiding_it() -> None:
    packet = _packet(correlation_id="corr_valid", t3_ms=300, t4_ms=700, t5_ms=None)
    packet["correlation_id"] = "corr_wrapper_mismatch"

    report = build_observation_fast_lane_latency_slo_report([packet])

    assert report["ok"] is False
    assert report["overall"]["record_count"] == 0
    assert report["rejected_records"][0]["reason"] == "fast_lane_correlation_id_wrapper_mismatch"
    assert "fast_lane_latency_malformed_records_present" in report["blockers"]


def test_latency_report_requires_actual_live_post_timestamp_when_live_gate_requested() -> None:
    report = build_observation_fast_lane_latency_slo_report(
        [_packet(correlation_id="corr_shadow_only", t3_ms=300, t4_ms=700, t5_ms=None)],
        require_live_t1_to_t5=True,
    )

    assert report["ok"] is False
    assert "fast_lane_live_t1_to_t5_samples_missing" in report["blockers"]
