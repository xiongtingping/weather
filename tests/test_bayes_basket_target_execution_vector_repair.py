from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.contracts import canonical_json, content_sha256
from bayes_basket_target.execution_vector_repair import (
    ExecutionVectorRepairError,
    inspect_legacy_blocked_execution_vector_repair,
    repair_legacy_blocked_execution_vectors,
)
from bayes_basket_target.execution_version_vector import (
    build_no_execution_version_policy,
)
from bayes_basket_target.ssot import (
    apply_production_ssot_schema,
    apply_target_ssot_schema,
)


def _database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    apply_target_ssot_schema(conn)
    apply_production_ssot_schema(conn)
    return conn


def _insert_blocked(
    conn: sqlite3.Connection,
    *,
    suffix: str,
    decision_sha_override: str | None = None,
    target_execution_hard_deadline_at: str | None = None,
) -> tuple[str, str, list[str]]:
    cycle_id = f"cycle-{suffix}"
    decision_id = f"decision-{suffix}"
    plan_id = f"plan-{suffix}"
    reasons = [f"blocked-{suffix}"]
    # Minted the way the ledger mints it since 2026-09-02: from what the
    # decision is made of, with the v2 schema tag.  The deadline is still
    # written to its column below but is no longer part of the address, so
    # passing one here changes the row and not the sha -- which is the
    # point of the parameter now.
    from bayes_basket_target.atomic_ledger import (  # noqa: PLC0415
        DECISION_SCHEMA_V2,
        economic_decision_identity,
    )

    identity = economic_decision_identity(
        station_id="RKPK",
        event_date="2026-07-27",
        settlement_timezone="Asia/Seoul",
        strategy_lane="basket",
        decided_state="blocked",
        will_order=0,
        reason_codes=reasons,
        details={},
        decision_scope_key=None,
    )
    decision_sha = decision_sha_override or content_sha256(
        {"schema": DECISION_SCHEMA_V2, **identity}
    )
    created_at = "2026-07-27T00:00:00Z"
    conn.execute(
        "INSERT INTO basket_target_execution_cycles("
        "cycle_id,plan_id,station_id,event_date,settlement_timezone,strategy_lane,"
        "plan_sha256,requested_reserve_usdc,state,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,0,'prepared',?,?)",
        (
            cycle_id,
            plan_id,
            "RKPK",
            "2026-07-27",
            "Asia/Seoul",
            "basket",
            "a" * 64,
            created_at,
            created_at,
        ),
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions("
        "decision_id,cycle_id,plan_id,station_id,event_date,settlement_timezone,"
        "strategy_lane,decided_state,will_order,target_execution_hard_deadline_at,"
        "reason_codes_json,details_json,decision_sha256,created_at"
        ") VALUES(?,?,?,?,?,?,?,'blocked',0,?,?,?,?,?)",
        (
            decision_id,
            cycle_id,
            plan_id,
            "RKPK",
            "2026-07-27",
            "Asia/Seoul",
            "basket",
            target_execution_hard_deadline_at,
            canonical_json(reasons),
            "{}",
            decision_sha,
            created_at,
        ),
    )
    return cycle_id, decision_id, reasons


def test_inspector_validates_deadline_bound_decision_content_address(
    tmp_path: Path,
) -> None:
    with _database(tmp_path / "target.db") as conn:
        _insert_blocked(
            conn,
            suffix="deadline",
            target_execution_hard_deadline_at="2026-07-27T00:05:00Z",
        )
        conn.commit()

        result = inspect_legacy_blocked_execution_vector_repair(conn)

        assert result["invalid_count"] == 0
        assert result["repair_candidate_count"] == 1
        assert result["blockers"] == ["legacy_blocked_execution_vectors_missing"]


def test_inspector_accepts_exact_blocked_training_fact_binding(
    tmp_path: Path,
) -> None:
    with _database(tmp_path / "target.db") as conn:
        deadline = "2026-07-27T00:05:00Z"
        cycle_id, decision_id, reasons = _insert_blocked(
            conn,
            suffix="training-fact",
            target_execution_hard_deadline_at=deadline,
        )
        decision_sha = conn.execute(
            "SELECT decision_sha256 FROM auto_trade_decisions WHERE decision_id=?",
            (decision_id,),
        ).fetchone()[0]
        fact_id = "fact-training"
        values = {
            "fact_id": fact_id,
            "station_id": "RKPK",
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
            "decision_at_utc": "2026-07-27T00:00:00Z",
            "decision_scope_key": "scope-training",
            "captured_at_utc": "2026-07-27T00:00:00Z",
            "source_sequence": 1,
            "quote_observed_at": "2026-07-27T00:00:00Z",
            "quote_expires_at": deadline,
            "quote_freshness_policy_sha256": "1" * 64,
            "stage": "P1",
            "probability_envelope_id": "probability-training",
            "posterior_version": "posterior-training",
            "posterior_sha256": "2" * 64,
            "official_exclusion_mask_sha256": "3" * 64,
            "ws_snapshot_id": "ws-training",
            "quote_version": "quote-training",
            "quote_sha256": "4" * 64,
            "inventory_snapshot_id": "inventory-training",
            "inventory_version": "inventory-version-training",
            "inventory_sha256": "5" * 64,
            "order_state_snapshot_id": "order-state-training",
            "order_state_version": "order-state-version-training",
            "order_state_sha256": "6" * 64,
            "cycle_id": cycle_id,
            "decision_id": decision_id,
            "decision_sha256": decision_sha,
            "plan_sha256": "7" * 64,
            "intent_count": 0,
            "intent_set_sha256": "8" * 64,
            "policy_version_vector_json": "{}",
            "feature_json": "{}",
            "partition_key": "partition-training",
            "authority_scope": "PRODUCTION_NO_LIVE_FORWARD",
            "created_at_utc": "2026-07-27T00:00:00Z",
            "content_sha256": "9" * 64,
        }
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        conn.execute(
            f"INSERT INTO basket_policy_training_fact({columns}) "
            f"VALUES({placeholders})",
            tuple(values.values()),
        )
        policy = build_no_execution_version_policy(
            {"policy": "NO_EXECUTION", "reason_codes": reasons}
        )
        conn.execute(
            "INSERT INTO basket_target_execution_version_vectors("
            "order_intent_id,cycle_id,decision_id,training_fact_id,vector_policy,"
            "target_price_id,quote_observed_at,quote_expires_at,vector_json,"
            "vector_sha256,created_at"
            ") VALUES(NULL,?,?,?,'NO_EXECUTION',NULL,NULL,NULL,?,?,?)",
            (
                cycle_id,
                decision_id,
                fact_id,
                canonical_json(policy.to_dict()),
                policy.policy_sha256,
                "2026-07-27T00:00:00Z",
            ),
        )
        conn.commit()

        result = inspect_legacy_blocked_execution_vector_repair(conn)

        assert result["ok"] is True
        assert result["covered_count"] == 1
        assert result["invalid_count"] == 0


def test_repair_appends_exact_no_execution_vector_and_is_idempotent(
    tmp_path: Path,
) -> None:
    with _database(tmp_path / "target.db") as conn:
        cycle_id, decision_id, reasons = _insert_blocked(conn, suffix="legacy")
        conn.commit()

        before = inspect_legacy_blocked_execution_vector_repair(conn)
        assert before["repair_candidate_count"] == 1
        assert before["invalid_count"] == 0

        first = repair_legacy_blocked_execution_vectors(conn, commit=True)
        second = repair_legacy_blocked_execution_vectors(conn, commit=True)

        assert first["state"] == "repaired"
        assert first["inserted_count"] == 1
        assert second["state"] == "already_complete"
        assert second["inserted_count"] == 0
        row = conn.execute(
            "SELECT order_intent_id,cycle_id,decision_id,training_fact_id,"
            "vector_policy,target_price_id,quote_observed_at,quote_expires_at,"
            "probability_sha256,official_ws_quote_sha256,inventory_sha256,"
            "order_state_sha256,station_policy_sha256,cash_policy_decision_sha256,"
            "strict_delta_artifact_sha256,vector_json,vector_sha256 "
            "FROM basket_target_execution_version_vectors"
        ).fetchone()
        policy = build_no_execution_version_policy(
            {"policy": "NO_EXECUTION", "reason_codes": reasons}
        )
        assert row[1:5] == (cycle_id, decision_id, None, "NO_EXECUTION")
        assert row[0] is None
        assert all(value is None for value in row[5:15])
        assert row[15] == canonical_json(policy.to_dict())
        assert row[16] == policy.policy_sha256


def test_repair_fails_whole_batch_on_invalid_decision_hash(
    tmp_path: Path,
) -> None:
    with _database(tmp_path / "target.db") as conn:
        _insert_blocked(conn, suffix="valid")
        _insert_blocked(
            conn,
            suffix="invalid",
            decision_sha_override="f" * 64,
        )
        conn.commit()

        with pytest.raises(
            ExecutionVectorRepairError,
            match="blocked_execution_vector_repair_precheck_failed",
        ):
            repair_legacy_blocked_execution_vectors(conn, commit=True)

        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors"
        ).fetchone()[0] == 0


def test_repair_rejects_downstream_intent_without_partial_write(
    tmp_path: Path,
) -> None:
    with _database(tmp_path / "target.db") as conn:
        cycle_id, decision_id, _ = _insert_blocked(conn, suffix="downstream")
        conn.execute(
            "INSERT INTO basket_target_order_intents("
            "order_intent_id,decision_id,cycle_id,bucket,token_id,side,order_action,"
            "limit_price,requested_size,requested_notional_usdc,state,"
            "live_submit_authorized,idempotency_key,intent_json,intent_sha256,"
            "created_at,target_execution_hard_deadline_at"
            ") VALUES('intent-1',?,?, '30','token','YES','BUY',0.5,1,0.5,"
            "'prepared',0,'idem','{}',?,'2026-07-27T00:00:00Z',NULL)",
            (decision_id, cycle_id, "b" * 64),
        )
        conn.commit()

        with pytest.raises(ExecutionVectorRepairError):
            repair_legacy_blocked_execution_vectors(conn, commit=True)

        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors"
        ).fetchone()[0] == 0
