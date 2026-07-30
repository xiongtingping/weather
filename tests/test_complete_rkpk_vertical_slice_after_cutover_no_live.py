from __future__ import annotations

import json
import sqlite3

import pytest

from scripts.complete_rkpk_vertical_slice_after_cutover_no_live import (
    TAF_ARTIFACT,
    _chain_materialized,
    _inspect_scope_authority,
    _validate_delta_v2_policy_rows,
)


def _current_rows() -> list[
    tuple[str, str, str, int, int, str | None]
]:
    return [
        (
            "target-v1",
            "d1",
            "delta-v2-policy-rkpk-summer-liquid-v1-d1",
            1,
            0,
            "SUPERSEDED",
        ),
        (
            "target-v1",
            "d2_fallback",
            "delta-v2-policy-rkpk-summer-liquid-v1-d2_fallback",
            1,
            0,
            "SUPERSEDED",
        ),
        (
            "target-v2",
            "d1",
            "delta-v2-policy-rkpk-summer-liquid-v2-d1",
            1,
            0,
            "SUPERSEDED",
        ),
        (
            "target-v2",
            "d2",
            "delta-v2-policy-rkpk-summer-liquid-v2-d2",
            1,
            0,
            "SUPERSEDED",
        ),
        (
            "target-v2",
            "d2_fallback",
            "delta-v2-policy-rkpk-summer-liquid-v2-d2_fallback",
            1,
            0,
            "SUPERSEDED",
        ),
        (
            "target-v2",
            "d1",
            "delta-v2-policy-rkpk-summer-liquid-v3-d1",
            2,
            0,
            "NO_LIVE_ACTIVATED",
        ),
        (
            "target-v2",
            "d2",
            "delta-v2-policy-rkpk-summer-liquid-v3-d2",
            2,
            0,
            "NO_LIVE_ACTIVATED",
        ),
        (
            "target-v2",
            "d2_fallback",
            "delta-v2-policy-rkpk-summer-liquid-v3-d2_fallback",
            2,
            0,
            "NO_LIVE_ACTIVATED",
        ),
    ]


def test_accepts_target_v2_when_target_v1_is_terminally_superseded() -> None:
    _validate_delta_v2_policy_rows(_current_rows())


@pytest.mark.parametrize(
    "replacement",
    [
        (
            "target-v1",
            "d1",
            "delta-v2-policy-rkpk-summer-liquid-v1-d1",
            1,
            0,
            "NO_LIVE_ACTIVATED",
        ),
        (
            "target-v1",
            "d1",
            "delta-v2-policy-rkpk-summer-liquid-v1-d1",
            1,
            0,
            None,
        ),
        (
            "target-v2",
            "d1",
            "delta-v2-policy-rkpk-summer-liquid-v2-d1",
            1,
            0,
            "NO_LIVE_ACTIVATED",
        ),
        (
            "unknown",
            "d1",
            "unknown-artifact",
            1,
            0,
            "SUPERSEDED",
        ),
    ],
)
def test_rejects_active_or_unknown_competing_activation(
    replacement: tuple[str, str, str, int, int, str | None],
) -> None:
    rows = _current_rows()
    rows[0] = replacement
    with pytest.raises(RuntimeError):
        _validate_delta_v2_policy_rows(rows)


def test_rejects_missing_target_v2_scope() -> None:
    with pytest.raises(
        RuntimeError,
        match="rkpk_completion_delta_v2_activation_missing",
    ):
        _validate_delta_v2_policy_rows(_current_rows()[:-1])


def test_rkpk_taf_artifact_uses_market_day_validity() -> None:
    artifact = json.loads(TAF_ARTIFACT.read_text())
    assert artifact["settlement_timezone"] == "Asia/Seoul"
    assert artifact["valid_from_utc"] == "2026-07-28T15:00:00Z"
    assert artifact["trained_through_utc"] < artifact["valid_from_utc"]


def _authority_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE basket_target_execution_cycles (
          cycle_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,
          settlement_timezone TEXT, state TEXT,
          requested_reserve_usdc REAL
        );
        CREATE TABLE auto_trade_decisions (
          decision_id TEXT PRIMARY KEY, cycle_id TEXT,
          settlement_timezone TEXT, decided_state TEXT, created_at TEXT
        );
        CREATE TABLE basket_target_capital_reservations (
          reservation_id TEXT PRIMARY KEY, cycle_id TEXT, amount_usdc REAL,
          state TEXT, created_at TEXT
        );
        CREATE TABLE basket_target_order_intents (
          order_intent_id TEXT PRIMARY KEY, cycle_id TEXT,
          requested_notional_usdc REAL, live_submit_authorized INTEGER,
          created_at TEXT
        );
        CREATE TABLE basket_policy_training_fact (
          fact_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,
          decision_id TEXT, decision_scope_key TEXT
        );
        CREATE TABLE basket_target_order_events (
          event_id TEXT PRIMARY KEY, order_intent_id TEXT
        );
        CREATE TABLE basket_target_execution_receipts (
          receipt_id TEXT PRIMARY KEY, cycle_id TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO basket_target_execution_cycles VALUES"
        "('c1','RKPK','2026-07-29','Asia/Seoul','prepared',0.5)"
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES"
        "('d1','c1','Asia/Seoul','prepared','2026-07-29T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO basket_target_capital_reservations VALUES"
        "('r1','c1',0.5,'active','2026-07-29T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO basket_target_order_intents VALUES"
        "('i1','c1',0.5,0,'2026-07-29T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO basket_policy_training_fact VALUES"
        "('f1','RKPK','2026-07-29','d1','scope-1')"
    )
    return conn


def test_scope_authority_requires_one_coherent_no_live_chain() -> None:
    conn = _authority_connection()
    result = _inspect_scope_authority(
        conn,
        station_id="RKPK",
        event_date="2026-07-29",
    )
    assert result["materialized_authority_ok"] is True
    assert result["nonblocked_prepared_count"] == 1
    assert result["active_positive_reservation_count"] == 1
    assert result["intent_notional_usdc"] == pytest.approx(0.5)
    assert result["order_event_count"] == 0
    assert result["execution_receipt_count"] == 0


def test_scope_authority_keeps_blocked_cycles_as_noncompeting_audit() -> None:
    conn = _authority_connection()
    conn.execute(
        "INSERT INTO basket_target_execution_cycles VALUES"
        "('c0','RKPK','2026-07-29','Asia/Seoul','prepared',0.0)"
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES"
        "('d0','c0','Asia/Seoul','blocked','2026-07-28T23:59:00Z')"
    )
    result = _inspect_scope_authority(
        conn,
        station_id="RKPK",
        event_date="2026-07-29",
    )
    assert result["materialized_authority_ok"] is True
    assert result["blocked_cycle_audit_count"] == 1


def test_scope_authority_accepts_latest_no_live_hold_with_training_fact() -> None:
    conn = _authority_connection()
    conn.execute(
        "UPDATE basket_target_execution_cycles "
        "SET requested_reserve_usdc=0.0 WHERE cycle_id='c1'"
    )
    conn.execute("DELETE FROM basket_target_capital_reservations")
    conn.execute("DELETE FROM basket_target_order_intents")
    conn.execute(
        "UPDATE auto_trade_decisions SET decided_state='blocked' "
        "WHERE decision_id='d1'"
    )

    result = _inspect_scope_authority(
        conn,
        station_id="RKPK",
        event_date="2026-07-29",
    )

    assert result["materialized_authority_ok"] is True
    assert result["authority_mode"] == "HOLD"
    assert result["intent_count"] == 0
    assert result["active_positive_reservation_count"] == 0


def test_chain_materialized_accepts_hold_but_rejects_hold_reservation() -> None:
    chain = {
        "p0": {},
        "p0m": {},
        "p1": {},
        "training_fact": {},
        "decision": {
            "decided_state": "blocked",
            "will_order": False,
            "reason_codes": ["complete_basket_delta_not_positive"],
            "intents": [],
            "reservation": None,
        },
        "authority_uniqueness": {"materialized_authority_ok": True},
    }
    assert _chain_materialized(chain) is True
    chain["decision"]["reservation"] = {
        "amount_usdc": 0.1,
        "state": "active",
    }
    assert _chain_materialized(chain) is False


def test_scope_authority_rejects_competing_prepared_candidate() -> None:
    conn = _authority_connection()
    conn.execute(
        "INSERT INTO basket_target_execution_cycles VALUES"
        "('c2','RKPK','2026-07-29','Asia/Seoul','prepared',0.5)"
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES"
        "('d2','c2','Asia/Seoul','prepared','2026-07-29T00:01:00Z')"
    )
    with pytest.raises(
        RuntimeError,
        match="rkpk_completion_competing_prepared_authority",
    ):
        _inspect_scope_authority(
            conn,
            station_id="RKPK",
            event_date="2026-07-29",
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE basket_target_capital_reservations SET amount_usdc=0.4",
        "UPDATE basket_target_order_intents SET requested_notional_usdc=0.4",
        "UPDATE basket_target_order_intents SET live_submit_authorized=1",
        "DELETE FROM basket_policy_training_fact",
        "INSERT INTO basket_target_order_events VALUES('e1','i1')",
        "INSERT INTO basket_target_execution_receipts VALUES('x1','c1')",
    ],
)
def test_scope_authority_fails_closed_on_atomic_chain_drift(
    mutation: str,
) -> None:
    conn = _authority_connection()
    conn.execute(mutation)
    result = _inspect_scope_authority(
        conn,
        station_id="RKPK",
        event_date="2026-07-29",
    )
    assert result["materialized_authority_ok"] is False
