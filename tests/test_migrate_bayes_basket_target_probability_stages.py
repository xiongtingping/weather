from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.ssot import production_ssot_schema_status
from scripts.initialize_bayes_basket_target_ssot import initialize
from scripts.migrate_bayes_basket_target_probability_stages import (
    CONFIRMATION,
    migrate,
)


ROOT = Path(__file__).resolve().parents[1]
ACTIVATION = (
    ROOT
    / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
)
NOW = "2026-07-26T16:00:00Z"


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "target.db"
    initialize(db=path, activation_path=ACTIVATION)
    return path


def _insert_preliminary_rows(conn: sqlite3.Connection, count: int = 22) -> None:
    for index in range(count):
        conn.execute(
            "INSERT INTO basket_p0_preselection_plan VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"pre-{index}",
                "RKPK",
                "2026-07-27",
                f"legacy-p0m-{index}",
                "P0-M",
                NOW,
                NOW,
                "a" * 64,
                "policy-1",
                "PRODUCTION_NO_LIVE_CANDIDATE",
                "{}",
                f"{index:064x}",
                "PRELIMINARY_NO_LIVE",
                "P1",
                0,
                0,
                0,
                NOW,
            ),
        )


def _insert_preserved_decision_and_cycle(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO basket_target_execution_cycles VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "cycle-1",
            "plan-1",
            "RKPK",
            "2026-07-27",
            "Asia/Seoul",
            "basket",
            "b" * 64,
            0.0,
            "prepared",
            NOW,
            NOW,
        ),
    )
    conn.execute(
        # Named columns.  The positional form here had drifted by more than
        # one column -- it fed None to reason_codes_json, which is NOT NULL --
        # and stayed that way for the 40 days after the table grew
        # decision_scope_key.  The two nullable trailing columns are left
        # unset on purpose: this fixture is about the migration, not about
        # them.
        """
        INSERT INTO auto_trade_decisions(
          decision_id, cycle_id, plan_id, station_id, event_date,
          settlement_timezone, strategy_lane, decided_state, will_order,
          reason_codes_json, details_json, decision_sha256, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "decision-1",
            "cycle-1",
            "plan-1",
            "RKPK",
            "2026-07-27",
            "Asia/Seoul",
            "basket",
            "blocked",
            0,
            '["production_no_live"]',
            "{}",
            "c" * 64,
            NOW,
        ),
    )


def test_migration_requires_exact_confirmation(tmp_path: Path) -> None:
    with pytest.raises(
        ContractError, match="probability_stage_migration_confirmation_invalid"
    ):
        migrate(
            db_path=_db(tmp_path),
            activation_path=ACTIVATION,
            confirm="wrong",
        )


def test_migration_rejects_nonempty_probability_table(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "env-1",
                "P0",
                "p0-1",
                "RKPK",
                "2026-07-27",
                "Asia/Seoul",
                NOW,
                "2026-07-26T18:00:00Z",
                "[33,34]",
                "[0.5,0.5]",
                "p0-v1",
                "d" * 64,
                "e" * 64,
                "{}",
                "f" * 64,
                "COMMITTED",
                NOW,
            ),
        )
    with pytest.raises(
        ContractError,
        match="probability_stage_migration_requires_empty_probability_table",
    ):
        migrate(
            db_path=db,
            activation_path=ACTIVATION,
            confirm=CONFIRMATION,
        )


@pytest.mark.parametrize(
    "table,sql,values",
    [
        (
            "basket_target_order_intents",
            "INSERT INTO basket_target_order_intents VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "intent-1",
                "decision-1",
                "cycle-1",
                "36",
                "token-1",
                "YES",
                "BUY",
                0.5,
                1.0,
                0.5,
                "prepared",
                0,
                None,
                "intent-key-1",
                "{}",
                "1" * 64,
                NOW,
            ),
        ),
        (
            "basket_target_capital_reservations",
            "INSERT INTO basket_target_capital_reservations VALUES"
            "(?,?,?,?,?,?,?,?,?,?)",
            (
                "reservation-1",
                "cycle-1",
                "basket",
                0.0,
                "active",
                "reservation-key-1",
                "2" * 64,
                None,
                NOW,
                NOW,
            ),
        ),
        (
            "basket_target_order_events",
            "INSERT INTO basket_target_order_events VALUES"
            "(?,?,?,?,?,?,?,?,?)",
            (
                "event-1",
                "intent-1",
                None,
                "POST_ACK",
                NOW,
                0.0,
                0.0,
                "{}",
                "3" * 64,
            ),
        ),
    ],
)
def test_migration_rejects_execution_residue(
    tmp_path: Path,
    table: str,
    sql: str,
    values: tuple[object, ...],
) -> None:
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(sql, values)
    with pytest.raises(
        ContractError, match="probability_stage_migration_execution_state_not_empty"
    ):
        migrate(
            db_path=db,
            activation_path=ACTIVATION,
            confirm=CONFIRMATION,
        )
    with sqlite3.connect(db) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_migration_removes_only_incompatible_preliminary_rows_and_preserves_ledger(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        _insert_preliminary_rows(conn)
        _insert_preserved_decision_and_cycle(conn)

    result = migrate(
        db_path=db,
        activation_path=ACTIVATION,
        confirm=CONFIRMATION,
    )

    assert result["removed_incompatible_preliminary_rows"] == 22
    assert result["stage_contract"] == ["P0", "P0-M", "P1"]
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_p0_preselection_plan"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_cycles"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions"
        ).fetchone()[0] == 1
        status = production_ssot_schema_status(conn)
        assert status["ok"] is True
        assert status["probability_stage_contract_ok"] is True
