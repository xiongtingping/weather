from __future__ import annotations

import sqlite3

import pytest

from amsc_specli_resident_shadow_ingress import AmscSpecliResidentShadowIngress
from observation_fast_lane_resident_shadow_worker import ResidentObservationFastLaneShadowWorker
from resident_amsc_specli_arrival_journal_shadow import (
    JOURNAL_TABLE,
    ResidentAmscSpecliArrivalJournalShadow,
)
from sequential_bayes_public_ssot_shadow_writer import (
    initialize_isolated_public_ssot_shadow_schema,
)
from sequential_bayes_schema_contract import apply_forward_schema
from source_observation_runtime import build_source_observation


class _Ingress:
    def __init__(self, *, outcome: str = "ok") -> None:
        self.calls: list[dict] = []
        self.outcome = outcome

    def process_arrival_envelope(self, **kwargs):
        self.calls.append(kwargs)
        if self.outcome == "deadline":
            return {
                "ok": False,
                "state": "blocked_before_canonical_write",
                "ingress": {
                    "blockers": ["amsc_specli_ingress_observation_hard_deadline_exceeded"]
                },
            }
        if self.outcome == "retryable":
            return {"ok": False, "state": "canonical_or_fast_lane_blocked", "ingress": {"blockers": ["artifact_missing"]}}
        return {"ok": True, "state": "dispatched", "ingress": {"blockers": []}}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    return conn


def _source(*, fetched_at: str = "2026-07-03T06:31:00Z") -> dict:
    return build_source_observation(
        source_id="zuuu_metar_raw_amsc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T06:30:00Z",
        fetched_at=fetched_at,
        response_completed_at=fetched_at,
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref="fixture://resident-arrival-journal",
        raw_payload={"report_type": "METAR"},
        payload={
            "report_type": "METAR",
            "arrival_time_quality": "observed_first_seen",
            "first_seen_at": fetched_at,
            "as_of_eligible_at": fetched_at,
        },
    ).as_dict()


def _dispatch(host, source, *, decision_at: str = "2026-07-03T06:31:01Z"):
    return host.process_source_observation(
        source_observation=source,
        target_identity="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        decision_as_of=decision_at,
        canonicalized_at="2026-07-03T06:31:00.050Z",
        posterior_transaction_inputs={"shadow_only": True, "correlation_id": "corr_arrival_journal"},
        decision_cycle_inputs={"outbox_event_id": "outbox_arrival_journal"},
    )


def test_first_seen_is_durable_and_repeated_physical_fetch_is_suppressed_after_restart() -> None:
    conn = _conn()
    ingress = _Ingress()
    first_host = ResidentAmscSpecliArrivalJournalShadow(conn, ingress=ingress)

    first = _dispatch(first_host, _source())
    second_host = ResidentAmscSpecliArrivalJournalShadow(conn, ingress=ingress)
    repeated = _dispatch(second_host, _source(fetched_at="2026-07-03T06:40:00Z"), decision_at="2026-07-03T06:40:01Z")

    assert first["ok"] is True
    assert repeated["ok"] is True
    assert repeated["state"] == "duplicate_physical_arrival_suppressed"
    assert repeated["first_actionable_seen_at"] == "2026-07-03T06:31:00Z"
    assert len(ingress.calls) == 1
    row = conn.execute(
        f"SELECT first_seen_at,state,dispatch_count FROM {JOURNAL_TABLE}"
    ).fetchone()
    assert row == ("2026-07-03T06:31:00Z", "dispatched", 1)


def test_retryable_downstream_block_reuses_original_t1_but_deadline_is_terminal() -> None:
    conn = _conn()
    retryable = _Ingress(outcome="retryable")
    host = ResidentAmscSpecliArrivalJournalShadow(conn, ingress=retryable)

    first = _dispatch(host, _source())
    second = _dispatch(host, _source(fetched_at="2026-07-03T06:31:02Z"), decision_at="2026-07-03T06:31:03Z")

    assert first["state"] == "ingress_blocked"
    assert second["state"] == "ingress_blocked"
    assert len(retryable.calls) == 2
    assert all(
        call["arrival_envelope"]["first_actionable_seen_at"] == "2026-07-03T06:31:00Z"
        for call in retryable.calls
    )

    deadline = _Ingress(outcome="deadline")
    deadline_host = ResidentAmscSpecliArrivalJournalShadow(conn, ingress=deadline)
    blocked = _dispatch(deadline_host, _source(fetched_at="2026-07-03T06:40:00Z"), decision_at="2026-07-03T06:40:01Z")
    suppressed = _dispatch(deadline_host, _source(fetched_at="2026-07-03T06:41:00Z"), decision_at="2026-07-03T06:41:01Z")

    assert blocked["journal"]["state"] == "deadline_blocked"
    assert suppressed["state"] == "duplicate_physical_arrival_suppressed"
    assert len(deadline.calls) == 1


def test_journal_refuses_nonisolated_database() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    with pytest.raises(ValueError, match="resident_arrival_journal_shadow_manifest_missing"):
        ResidentAmscSpecliArrivalJournalShadow(conn, ingress=_Ingress())


def test_journal_and_real_resident_ingress_share_one_isolated_connection() -> None:
    conn = _conn()
    runner_calls: list[dict] = []

    def runner(_conn, **kwargs):
        runner_calls.append(kwargs)
        return {"ok": True, "state": "shadow_committed_within_slo"}

    worker = ResidentObservationFastLaneShadowWorker(
        conn,
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
        prewarmed_components={
            "forecast_snapshot": {"ready": True, "forecast_snapshot_id": "forecast_journal", "artifact_sha256": "forecast_artifact_journal", "valid_until": "2026-07-03T07:30:00Z"},
            "prior_context": {"ready": True, "prior_context_id": "prior_journal", "artifact_sha256": "prior_artifact_journal", "valid_until": "2026-07-03T07:30:00Z"},
            "strong_lr_artifact": {"ready": True, "approved": True, "strong_lr_artifact_sha256": "strong_journal", "valid_until": "2026-07-03T07:30:00Z"},
            "market_context": {"ready": True, "market_snapshot_id": "market_journal", "fresh_until": "2026-07-03T07:30:00Z"},
            "resident_signer_connection_pool": {"ready": True, "pool_id": "pool_journal", "valid_until": "2026-07-03T07:30:00Z"},
            },
            runner=runner,
            timestamp_observer=lambda: "2026-07-03T06:31:00.050Z",
        )
    host = ResidentAmscSpecliArrivalJournalShadow(
        conn,
        ingress=AmscSpecliResidentShadowIngress(worker),
    )

    result = _dispatch(host, _source())

    assert result["ok"] is True
    assert result["state"] == "dispatched"
    assert len(runner_calls) == 1
    assert runner_calls[0]["first_actionable_seen_at"] == "2026-07-03T06:31:00Z"
