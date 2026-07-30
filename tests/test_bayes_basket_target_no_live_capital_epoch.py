from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.atomic_ledger import (
    apply_target_ledger_schema,
    initialize_capital_pool,
)
from bayes_basket_target.execution_transition_runtime import (
    apply_execution_transition_schema,
)
from bayes_basket_target.no_live_capital_epoch import (
    NoLiveCapitalEpochError,
    ZERO_RESERVATION_RELEASE_REASON,
    migrate_target_no_live_capital_epoch,
)


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "config/bayes_basket_target/no_live_runtime_v1.json"
NOW = "2026-07-29T02:00:00Z"


def _db(*, balance: float = 0.0) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    apply_execution_transition_schema(conn)
    initialize_capital_pool(
        conn,
        balance_usdc=balance,
        capital_epoch_id="bayes-basket-target-production-no-live-bootstrap-v1",
        updated_at_utc=NOW,
    )
    return conn


def _zero_active_reservation(conn: sqlite3.Connection, suffix: str) -> None:
    conn.execute(
        "INSERT INTO basket_target_capital_reservations("
        "reservation_id,cycle_id,strategy_lane,amount_usdc,state,"
        "idempotency_key,reservation_sha256,release_reason,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            f"reservation-{suffix}",
            f"cycle-{suffix}",
            "basket",
            0.0,
            "active",
            f"idempotency-{suffix}",
            suffix * 64,
            None,
            NOW,
            NOW,
        ),
    )
    conn.commit()


def test_migration_aligns_target_budget_and_releases_zero_residue() -> None:
    conn = _db()
    _zero_active_reservation(conn, "a")
    result = migrate_target_no_live_capital_epoch(
        conn,
        runtime_config_path=RUNTIME,
        migrated_at_utc=NOW,
    )

    assert result["state"] == "COMMITTED"
    assert result["before"]["balance_usdc"] == 0.0
    assert result["after"]["balance_usdc"] == 20.0
    assert result["after"]["reserved_usdc"] == 0.0
    assert result["after"]["version"] == result["before"]["version"] + 1
    assert result["after"]["capital_epoch_id"].startswith("target-no-live-")
    assert result["zero_active_reservations_released"] == 1
    assert result["reads_shared_live_capital"] is False
    assert result["calls_order_gateway"] is False
    assert conn.execute(
        "SELECT state,release_reason FROM basket_target_capital_reservations"
    ).fetchone() == ("released", ZERO_RESERVATION_RELEASE_REASON)
    assert conn.execute(
        "SELECT migration_id,content_sha256 "
        "FROM basket_target_capital_epoch_migrations"
    ).fetchone() == (
        result["migration_id"],
        result["migration_content_sha256"],
    )


def test_replay_is_zero_write_even_with_current_epoch_reservation() -> None:
    conn = _db()
    first = migrate_target_no_live_capital_epoch(
        conn,
        runtime_config_path=RUNTIME,
        migrated_at_utc=NOW,
    )
    conn.execute(
        "UPDATE basket_target_capital_pool SET reserved_usdc=1,version=version+1"
    )
    conn.commit()
    before_changes = conn.total_changes
    replay = migrate_target_no_live_capital_epoch(
        conn,
        runtime_config_path=RUNTIME,
        migrated_at_utc="2026-07-29T02:01:00Z",
    )

    assert first["state"] == "COMMITTED"
    assert replay["state"] == "UNCHANGED_ZERO_WRITE"
    assert replay["rows_written"] == 0
    assert replay["after"]["reserved_usdc"] == 1.0
    assert conn.total_changes == before_changes


def test_first_migration_rejects_execution_residue_and_rolls_back() -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO basket_target_order_intents("
        "order_intent_id,decision_id,cycle_id,bucket,token_id,side,"
        "order_action,limit_price,requested_size,requested_notional_usdc,state,"
        "live_submit_authorized,target_execution_hard_deadline_at,idempotency_key,"
        "intent_json,intent_sha256,created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "intent-1",
            "decision-1",
            "cycle-1",
            "38",
            "token-1",
            "YES",
            "BUY",
            0.05,
            1.0,
            0.05,
            "prepared",
            0,
            None,
            "intent-idempotency-1",
            "{}",
            "b" * 64,
            NOW,
        ),
    )
    conn.commit()

    with pytest.raises(
        NoLiveCapitalEpochError,
        match="execution_residue_present",
    ):
        migrate_target_no_live_capital_epoch(
            conn,
            runtime_config_path=RUNTIME,
            migrated_at_utc=NOW,
        )
    assert conn.execute(
        "SELECT balance_usdc,reserved_usdc,capital_epoch_id,version "
        "FROM basket_target_capital_pool"
    ).fetchone() == (
        0.0,
        0.0,
        "bayes-basket-target-production-no-live-bootstrap-v1",
        0,
    )


def test_first_migration_rejects_nonzero_source_balance() -> None:
    conn = _db(balance=1.0)
    with pytest.raises(
        NoLiveCapitalEpochError,
        match="nonzero_source_balance",
    ):
        migrate_target_no_live_capital_epoch(
            conn,
            runtime_config_path=RUNTIME,
            migrated_at_utc=NOW,
        )


def test_verified_no_live_epoch_can_roll_forward_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_12 = tmp_path / "runtime-12.json"
    runtime_20 = tmp_path / "runtime-20.json"
    template = {
        "schema": "bayes_basket_target_no_live_runtime_v1",
        "state": "ENABLED_FOR_PRODUCTION_NO_LIVE",
        "execution_mode": "production_no_live",
        "target_only": True,
        "capital_scope": "target_owned_only",
        "live_submit_authorized": False,
        "places_orders": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "reads_shared_live_capital": False,
    }
    import bayes_basket_target.no_live_capital_epoch as module

    monkeypatch.setattr(
        module,
        "load_target_no_live_runtime_config",
        lambda path: {
            **template,
            "target_remaining_budget_usdc": (
                12.0 if Path(path) == runtime_12 else 20.0
            ),
        },
    )
    conn = _db()
    first = migrate_target_no_live_capital_epoch(
        conn,
        runtime_config_path=runtime_12,
        migrated_at_utc=NOW,
    )
    rollover = migrate_target_no_live_capital_epoch(
        conn,
        runtime_config_path=runtime_20,
        migrated_at_utc="2026-07-29T02:01:00Z",
    )
    replay = migrate_target_no_live_capital_epoch(
        conn,
        runtime_config_path=runtime_20,
        migrated_at_utc="2026-07-29T02:02:00Z",
    )

    assert first["after"]["balance_usdc"] == 12.0
    assert rollover["before"]["balance_usdc"] == 12.0
    assert rollover["after"]["balance_usdc"] == 20.0
    assert rollover["after"]["capital_epoch_id"] != first["after"][
        "capital_epoch_id"
    ]
    assert replay["state"] == "UNCHANGED_ZERO_WRITE"
    assert replay["rows_written"] == 0


def test_attached_database_is_rejected_before_mutation() -> None:
    conn = _db()
    conn.execute("ATTACH DATABASE ':memory:' AS shared")
    with pytest.raises(
        NoLiveCapitalEpochError,
        match="attached_database_forbidden",
    ):
        migrate_target_no_live_capital_epoch(
            conn,
            runtime_config_path=RUNTIME,
            migrated_at_utc=NOW,
        )
    assert conn.execute(
        "SELECT balance_usdc FROM basket_target_capital_pool"
    ).fetchone()[0] == 0.0
