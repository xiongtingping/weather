from __future__ import annotations

import sqlite3

import pytest

from production_posterior_commit_outbox_worker import (
    ProductionPosteriorCommitOutboxWorker,
    claim_production_posterior_commit_outbox,
)
from sequential_bayes_schema_contract import apply_forward_schema


SLUG = "highest-temperature-in-chengdu-on-july-13-2026"
DATE = "2026-07-13"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    conn.executescript(
        """
        CREATE TABLE event_fact_history(
          id INTEGER PRIMARY KEY AUTOINCREMENT,slug TEXT,event_date TEXT,
          fact_snapshot_id TEXT,posterior_run_id TEXT
        );
        CREATE TABLE event_fact_latest(slug TEXT,event_date TEXT,fact_id INTEGER);
        CREATE TABLE auto_trade_decisions(id INTEGER PRIMARY KEY);
        """
    )
    for suffix, latest in (("old", False), ("current", True)):
        conn.execute(
            "INSERT INTO event_fact_history(slug,event_date,fact_snapshot_id,posterior_run_id) VALUES(?,?,?,?)",
            (SLUG, DATE, f"fact-{suffix}", f"post-{suffix}"),
        )
        fact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        if latest:
            conn.execute("INSERT INTO event_fact_latest VALUES(?,?,?)", (SLUG, DATE, fact_id))
        conn.execute(
            """INSERT INTO posterior_commit_outbox(
              outbox_event_id,posterior_run_id,fact_snapshot_id,correlation_id,state,created_at,
              attempt_count,claim_generation
            ) VALUES(?,?,?,?,?,?,0,0)""",
            (f"outbox-{suffix}", f"post-{suffix}", f"fact-{suffix}", f"corr-{suffix}",
             "pending", "2026-07-13T00:00:00.100Z"),
        )
        conn.execute(
            """INSERT INTO posterior_outbox_execution_contracts(
              outbox_event_id,posterior_run_id,canonical_observation_event_id,
              first_actionable_seen_at,observation_to_order_hard_deadline_at,
              inference_decision_deadline_at,transaction_b_committed_at,
              execution_eligible_at_commit,execution_na_reason,deadline_policy,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (f"outbox-{suffix}", f"post-{suffix}", f"canobs-{suffix}",
             "2026-07-13T00:00:00.000Z", "2026-07-13T00:00:03.000Z",
             "2026-07-13T00:00:03.000Z", "2026-07-13T00:00:00.100Z", 1, "",
             "canonical_first_actionable_plus_3000ms", "2026-07-13T00:00:00.100Z"),
        )
    conn.commit()
    return conn


def _inputs(outbox: dict) -> dict:
    return {
        "slug": outbox["slug"], "event_date": outbox["event_date"],
        "market_snapshot": {"market_snapshot_id": "market-current"},
        "desired_trading_plan": {
            "posterior_run_id": outbox["posterior_run_id"],
            "fact_snapshot_id": outbox["fact_snapshot_id"],
            "trading_plan_id": "plan-current", "market_snapshot_id": "market-current",
        },
        "transition_plan": {"posterior_run_id": outbox["posterior_run_id"], "trading_plan_id": "plan-current"},
        "capital_snapshot": {"capital_snapshot_id": "capital-current"},
        "risk_snapshot": {"risk_snapshot_id": "risk-current"},
        "production_activation": {"state": "enabled"},
    }


def test_worker_selects_only_latest_and_validates_all_frozen_inputs_before_claim() -> None:
    conn = _conn()
    calls: list[dict] = []

    def runner(db: sqlite3.Connection, **kwargs: object) -> dict:
        calls.append(dict(kwargs))
        db.execute("UPDATE posterior_commit_outbox SET state='completed' WHERE outbox_event_id=?", (kwargs["outbox_event_id"],))
        return {"ok": True, "state": "committed", "places_orders": False}

    worker = ProductionPosteriorCommitOutboxWorker(conn, lease_owner="production-worker-test", runner=runner)
    result = worker.process_ready_latest_outbox(
        decision_as_of="2026-07-13T00:00:01.000Z", frozen_inputs_for=_inputs, max_events=2,
    )

    assert result["ok"] is True
    assert result["boundary"]["frozen_inputs_before_lease"] is True
    assert result["boundary"]["claim_decision_intent_outbox_single_transaction"] is True
    assert len(calls) == 1
    assert calls[0]["outbox_event_id"] == "outbox-current"
    assert conn.execute("SELECT state FROM posterior_commit_outbox WHERE outbox_event_id='outbox-old'").fetchone()[0] == "pending"


def test_worker_never_claims_when_frozen_bundle_is_incomplete() -> None:
    conn = _conn()
    worker = ProductionPosteriorCommitOutboxWorker(conn, lease_owner="production-worker-test")
    result = worker.process_ready_latest_outbox(
        decision_as_of="2026-07-13T00:00:01.000Z",
        frozen_inputs_for=lambda outbox: {"slug": outbox["slug"], "event_date": outbox["event_date"]},
    )
    assert result["ok"] is False
    assert result["outcomes"][0]["state"] == "blocked_before_claim"
    assert conn.execute("SELECT state,claim_generation FROM posterior_commit_outbox WHERE outbox_event_id='outbox-current'").fetchone() == ("pending", 0)


def test_direct_claim_rejects_superseded_outbox_even_if_it_is_pending() -> None:
    conn = _conn()
    result = claim_production_posterior_commit_outbox(
        conn,
        outbox={"outbox_event_id": "outbox-old", "posterior_run_id": "post-old", "fact_snapshot_id": "fact-old"},
        lease_owner="production-worker-test", claimed_at="2026-07-13T00:00:01.000Z",
        lease_until="2026-07-13T00:00:02.000Z",
    )
    assert result["ok"] is False
    assert conn.execute("SELECT state FROM posterior_commit_outbox WHERE outbox_event_id='outbox-old'").fetchone()[0] == "pending"


def test_worker_deadline_expiry_never_claims() -> None:
    conn = _conn()
    worker = ProductionPosteriorCommitOutboxWorker(conn, lease_owner="production-worker-test")
    result = worker.process_ready_latest_outbox(
        decision_as_of="2026-07-13T00:00:03.001Z", frozen_inputs_for=_inputs,
    )
    assert result["state"] == "idle_no_latest_outbox_ready"
    assert conn.execute("SELECT state FROM posterior_commit_outbox WHERE outbox_event_id='outbox-current'").fetchone()[0] == "pending"


def test_runner_block_rolls_back_lease_and_any_partial_write() -> None:
    conn = _conn()

    def blocked_runner(db: sqlite3.Connection, **_kwargs: object) -> dict:
        db.execute("CREATE TABLE temporary_worker_probe(value TEXT)")
        db.execute("INSERT INTO temporary_worker_probe VALUES('must_rollback')")
        return {"ok": False, "state": "blocked", "places_orders": False}

    worker = ProductionPosteriorCommitOutboxWorker(
        conn, lease_owner="production-worker-test", runner=blocked_runner,
    )
    result = worker.process_ready_latest_outbox(
        decision_as_of="2026-07-13T00:00:01.000Z", frozen_inputs_for=_inputs,
    )

    assert result["outcomes"][0]["state"] == "runner_blocked"
    assert conn.execute("SELECT state,claim_generation FROM posterior_commit_outbox WHERE outbox_event_id='outbox-current'").fetchone() == ("pending", 0)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='temporary_worker_probe'").fetchone() is None


def test_runner_exception_rolls_back_lease() -> None:
    conn = _conn()

    def exploding_runner(db: sqlite3.Connection, **_kwargs: object) -> dict:
        db.execute("UPDATE posterior_commit_outbox SET terminal_reason='must_rollback' WHERE outbox_event_id='outbox-current'")
        raise RuntimeError("injected")

    worker = ProductionPosteriorCommitOutboxWorker(
        conn, lease_owner="production-worker-test", runner=exploding_runner,
    )
    result = worker.process_ready_latest_outbox(
        decision_as_of="2026-07-13T00:00:01.000Z", frozen_inputs_for=_inputs,
    )

    assert result["outcomes"][0]["state"] == "runner_exception_rolled_back"
    assert conn.execute("SELECT state,claim_generation,terminal_reason FROM posterior_commit_outbox WHERE outbox_event_id='outbox-current'").fetchone() == ("pending", 0, None)


def test_worker_refuses_shadow_database() -> None:
    conn = _conn()
    conn.execute("CREATE TABLE v2_shadow_runtime_manifest(singleton INTEGER)")
    with pytest.raises(ValueError, match="refuses_shadow_manifest"):
        ProductionPosteriorCommitOutboxWorker(conn, lease_owner="production-worker-test")
