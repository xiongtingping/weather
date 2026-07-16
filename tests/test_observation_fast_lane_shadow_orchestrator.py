from __future__ import annotations

import sqlite3

import observation_fast_lane_shadow_orchestrator as runtime


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


def test_physical_only_public_ssot_is_success_and_never_requires_market_outbox(monkeypatch) -> None:
    correlation_id = "corr_physical_only_123456"

    def fake_posterior(_conn, **_kwargs):
        return {
            "ok": True,
            "state": "public_ssot_shadow_physical_only_committed",
            "canonical_observation_event_id": "canobs_physical_only_123456",
            "checkpoint": {"posterior_run_id": "post_physical_only_123456"},
            "public_fact_row": {
                "state": "physical_only_market_projection_na",
                "correlation_id": correlation_id,
                "fact_snapshot_id": "factsnap_physical_only_123456",
            },
        }

    monkeypatch.setattr(runtime, "run_canonical_event_public_ssot_shadow_update", fake_posterior)
    monkeypatch.setattr(
        runtime,
        "append_observation_fast_lane_shadow_latency_packet",
        lambda _conn, *, packet: {"ok": True, "state": "appended", "packet": packet},
    )

    class ForbiddenOutboxWorker:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("physical posterior must not instantiate an outbox consumer")

    monkeypatch.setattr(runtime, "ResidentPosteriorCommitOutboxShadowWorker", ForbiddenOutboxWorker)
    timestamps = iter(("2026-07-03T06:31:00.100Z", "2026-07-03T06:31:00.300Z"))

    result = runtime.run_observation_fast_lane_shadow(
        sqlite3.connect(":memory:"),
        first_actionable_seen_at="2026-07-03T06:31:00.000Z",
        canonicalized_at="2026-07-03T06:31:00.050Z",
        topology=_topology(),
        posterior_transaction_inputs={
            "shadow_only": True,
            "correlation_id": correlation_id,
            "public_event_date": "2026-07-03",
        },
        decision_cycle_inputs={},
        commit_timestamp_observer=lambda: next(timestamps),
    )

    assert result["ok"] is True
    assert result["state"] == "physical_posterior_ssot_committed_market_projection_na"
    assert result["decision"]["state"] == "not_applicable"
    assert result["outbox_dispatch"]["state"] == "not_applicable"
    assert result["boundary"]["creates_trade_outbox"] is False
    assert result["boundary"]["market_projection_na_does_not_block_probability_ssot"] is True
