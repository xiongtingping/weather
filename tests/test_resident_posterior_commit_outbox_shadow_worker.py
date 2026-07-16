from __future__ import annotations

import sqlite3

import pytest

from dynamic_three_leg_shadow_decision_writer import initialize_isolated_dynamic_three_leg_shadow_decision_schema
from dynamic_three_leg_shadow_decision_writer import claim_isolated_posterior_commit_outbox
from resident_posterior_commit_outbox_shadow_worker import (
    ResidentPosteriorCommitOutboxShadowWorker,
)
from sequential_bayes_public_ssot_shadow_writer import (
    initialize_isolated_public_ssot_shadow_schema,
)
from sequential_bayes_schema_contract import apply_forward_schema
from settlement_window_runtime import load_approved_settlement_window_policy


def _conn_with_latest_outbox() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn)
    for suffix, is_latest in (("old", False), ("current", True)):
        fact_snapshot_id = f"fact_{suffix}"
        posterior_run_id = f"post_{suffix}"
        conn.execute(
            """
            INSERT INTO event_fact_history(
                run_date,slug,event_date,fact_as_of,composer_version,fact_json,created_at,
                correlation_id,fact_snapshot_id,posterior_run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "2026-07-12",
                "highest-temperature-in-chengdu-on-july-12-2026",
                "2026-07-12",
                "2026-07-12T00:00:00Z",
                "v31",
                "{}",
                "2026-07-12T00:00:00Z",
                f"corr_{suffix}",
                fact_snapshot_id,
                posterior_run_id,
            ),
        )
        fact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        if is_latest:
            conn.execute(
                """
                INSERT INTO event_fact_latest(
                    slug,event_date,fact_id,updated_at,fact_snapshot_id,correlation_id
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    "highest-temperature-in-chengdu-on-july-12-2026",
                    "2026-07-12",
                    fact_id,
                    "2026-07-12T00:00:00Z",
                    fact_snapshot_id,
                    f"corr_{suffix}",
                ),
            )
        conn.execute(
            """
            INSERT INTO posterior_commit_outbox(
                outbox_event_id,posterior_run_id,fact_snapshot_id,correlation_id,state,
                created_at,attempt_count,claim_generation
            ) VALUES(?,?,?,?,?,?,0,0)
            """,
            (
                f"outbox_{suffix}",
                posterior_run_id,
                fact_snapshot_id,
                f"corr_{suffix}",
                "pending",
                "2026-07-12T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO posterior_outbox_execution_contracts(
                outbox_event_id,posterior_run_id,canonical_observation_event_id,
                first_actionable_seen_at,observation_to_order_hard_deadline_at,
                inference_decision_deadline_at,transaction_b_committed_at,
                execution_eligible_at_commit,execution_na_reason,deadline_policy,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"outbox_{suffix}", posterior_run_id, f"canobs_{suffix}",
                "2026-07-12T00:00:00.000Z", "2026-07-12T00:00:03.000Z",
                "2026-07-12T00:00:03.000Z", "2026-07-12T00:00:00.100Z",
                1, "", "canonical_first_actionable_plus_3000ms", "2026-07-12T00:00:00.100Z",
            ),
        )
    conn.commit()
    return conn


def _decision_inputs(outbox: dict) -> dict:
    return {
        "slug": outbox["slug"],
        "event_date": outbox["event_date"],
        "market_snapshot": {"market_snapshot_id": "market_current"},
        "capital_plan": {"capital_snapshot_id": "capital_current"},
        "settlement_window_policy": load_approved_settlement_window_policy(),
        "strategy_artifact_sha256": "artifact_current",
        "top_yes_wave_entry_plan": {"policy": "top_yes_only"},
    }


def test_resident_outbox_worker_dispatches_only_latest_ssot_without_generic_queue() -> None:
    conn = _conn_with_latest_outbox()
    calls: list[dict] = []

    def runner(_conn: sqlite3.Connection, **kwargs: object) -> dict:
        calls.append(dict(kwargs))
        return {"ok": True, "state": "shadow_committed", "shadow_only": True}

    worker = ResidentPosteriorCommitOutboxShadowWorker(
        conn,
        lease_owner="resident-shadow-1",
        runner=runner,
    )
    result = worker.process_ready_latest_outbox(
        decision_as_of="2026-07-12T00:00:01Z",
        decision_inputs_for=_decision_inputs,
        max_events=2,
    )

    assert result["ok"] is True
    assert result["state"] == "dispatched"
    assert result["boundary"] == {
        "dedicated_posterior_outbox_only": True,
        "generic_task_queue": False,
        "network_acquisition": False,
        "signer_or_clob": False,
        "production_archive_write": False,
    }
    assert len(calls) == 1
    assert calls[0]["outbox_event_id"] == "outbox_current"
    assert calls[0]["lease_until"] == "2026-07-12T00:00:02.000+00:00"
    assert conn.execute(
        "SELECT state FROM posterior_commit_outbox WHERE outbox_event_id='outbox_old'"
    ).fetchone()[0] == "pending"


def test_resident_outbox_worker_validates_frozen_inputs_before_claim() -> None:
    conn = _conn_with_latest_outbox()
    worker = ResidentPosteriorCommitOutboxShadowWorker(conn, lease_owner="resident-shadow-1")

    result = worker.process_ready_latest_outbox(
        decision_as_of="2026-07-12T00:00:01Z",
        decision_inputs_for=lambda outbox: {
            "slug": outbox["slug"],
            "event_date": outbox["event_date"],
        },
    )

    assert result["ok"] is False
    assert result["state"] == "partial_or_blocked"
    assert result["outcomes"][0]["state"] == "blocked_before_claim"
    assert conn.execute(
        "SELECT state FROM posterior_commit_outbox WHERE outbox_event_id='outbox_current'"
    ).fetchone()[0] == "pending"


def test_resident_outbox_input_provider_exception_cannot_claim_outbox() -> None:
    conn = _conn_with_latest_outbox()
    worker = ResidentPosteriorCommitOutboxShadowWorker(conn, lease_owner="resident-shadow-1")

    result = worker.process_ready_latest_outbox(
        decision_as_of="2026-07-12T00:00:01Z",
        decision_inputs_for=lambda _outbox: (_ for _ in ()).throw(RuntimeError("market_not_ready")),
    )

    assert result["ok"] is False
    assert result["outcomes"][0]["state"] == "input_provider_exception_before_claim"
    assert conn.execute(
        "SELECT state FROM posterior_commit_outbox WHERE outbox_event_id='outbox_current'"
    ).fetchone()[0] == "pending"


def test_resident_outbox_worker_refuses_nonisolated_db() -> None:
    with pytest.raises(ValueError, match="resident_outbox_shadow_manifest_missing"):
        ResidentPosteriorCommitOutboxShadowWorker(
            sqlite3.connect(":memory:"),
            lease_owner="resident-shadow-1",
        )


def test_resident_outbox_worker_never_dispatches_after_observation_hard_deadline() -> None:
    conn = _conn_with_latest_outbox()
    calls: list[dict] = []
    worker = ResidentPosteriorCommitOutboxShadowWorker(
        conn,
        lease_owner="resident-shadow-1",
        runner=lambda _conn, **kwargs: calls.append(dict(kwargs)) or {"ok": True},
    )

    result = worker.process_ready_latest_outbox(
        decision_as_of="2026-07-12T00:00:03.001Z",
        decision_inputs_for=_decision_inputs,
    )

    assert result["state"] == "idle_no_latest_outbox_ready"
    assert calls == []
    assert conn.execute(
        "SELECT state FROM posterior_commit_outbox WHERE outbox_event_id='outbox_current'"
    ).fetchone()[0] == "pending"


def test_direct_claim_cannot_bypass_observation_hard_deadline() -> None:
    conn = _conn_with_latest_outbox()

    result = claim_isolated_posterior_commit_outbox(
        conn,
        outbox_event_id="outbox_current",
        lease_owner="direct-caller",
        claimed_at="2026-07-12T00:00:03.001Z",
        lease_until="2026-07-12T00:00:04.000Z",
    )

    assert result["ok"] is False
    assert result["reason"] == "dynamic_three_leg_shadow_observation_to_order_deadline_expired"
    assert conn.execute(
        "SELECT state, claim_generation FROM posterior_commit_outbox WHERE outbox_event_id='outbox_current'"
    ).fetchone() == ("pending", 0)
