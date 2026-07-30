from __future__ import annotations

import copy
import sqlite3

import pytest

from bayes_basket_target.atomic_ledger import (
    AtomicLedgerError,
    InjectedAtomicFault,
    commit_target_atomic_bundle,
)
from bayes_basket_target.execution_deadline import (
    TargetExecutionDeadlineError,
    TargetExecutionHardDeadline,
    derive_target_execution_hard_deadline,
)
from tests.test_bayes_basket_target_atomic_ledger import _bundle, _db


def _without_deadline(bundle: dict) -> dict:
    value = copy.deepcopy(bundle)
    value["decision"].pop("target_execution_hard_deadline_at", None)
    for intent in value["intents"]:
        intent.pop("target_execution_hard_deadline_at", None)
        intent["intent_json"].pop("target_execution_hard_deadline_at", None)
    return value


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "target_execution_hard_deadline_missing"),
        ("2026-07-23T05:00:03", "target_execution_hard_deadline_timezone_required"),
        ("not-a-time", "target_execution_hard_deadline_invalid_iso8601"),
    ],
)
def test_atomic_writer_rejects_missing_naive_and_malformed_deadline_before_any_write(
    value: str | None, reason: str
) -> None:
    with _db() as conn:
        bundle = _without_deadline(_bundle(index=3000))
        if value is not None:
            bundle["decision"]["target_execution_hard_deadline_at"] = value
            for intent in bundle["intents"]:
                intent["target_execution_hard_deadline_at"] = value
                intent["intent_json"]["target_execution_hard_deadline_at"] = value
        with pytest.raises(AtomicLedgerError, match=reason):
            commit_target_atomic_bundle(conn, bundle=bundle)
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM basket_target_order_intents").fetchone()[0] == 0


def test_atomic_writer_rejects_just_expired_deadline_and_accepts_future_deadline() -> None:
    with _db() as conn:
        expired = _bundle(index=3001)
        expired["decision"]["target_execution_hard_deadline_at"] = "2026-07-23T03:59:59.999Z"
        for intent in expired["intents"]:
            intent["target_execution_hard_deadline_at"] = "2026-07-23T03:59:59.999Z"
            intent["intent_json"]["target_execution_hard_deadline_at"] = "2026-07-23T03:59:59.999Z"
        with pytest.raises(AtomicLedgerError, match="target_execution_hard_deadline_expired"):
            commit_target_atomic_bundle(conn, bundle=expired)

        future = _bundle(index=3002)
        receipt = commit_target_atomic_bundle(conn, bundle=future)
        assert receipt["state"] == "committed"
        decision_deadline = conn.execute(
            "SELECT target_execution_hard_deadline_at FROM auto_trade_decisions WHERE decision_id=?",
            (future["decision"]["decision_id"],),
        ).fetchone()[0]
        intent_deadline = conn.execute(
            "SELECT target_execution_hard_deadline_at FROM basket_target_order_intents WHERE order_intent_id=?",
            (future["intents"][0]["order_intent_id"],),
        ).fetchone()[0]
        assert decision_deadline == intent_deadline == "2026-07-23T04:00:03.000Z"


def test_deadline_is_atomic_with_decision_and_intent_and_rolls_back_on_fault() -> None:
    with _db() as conn:
        bundle = _bundle(index=3003)
        with pytest.raises(InjectedAtomicFault, match="intent"):
            commit_target_atomic_bundle(conn, bundle=bundle, fault_after="intent")
        for table in (
            "basket_target_observation_facts",
            "basket_target_execution_cycles",
            "auto_trade_decisions",
            "basket_target_order_intents",
            "basket_target_capital_reservations",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_deadline_commit_is_idempotent_and_replay_cannot_drift() -> None:
    with _db() as conn:
        bundle = _bundle(index=3004)
        first = commit_target_atomic_bundle(conn, bundle=bundle)
        second = commit_target_atomic_bundle(conn, bundle=copy.deepcopy(bundle))
        assert first["state"] == "committed"
        assert second["state"] == "already_committed"
        assert second["idempotent"] is True
        drift = copy.deepcopy(bundle)
        drift["decision"]["target_execution_hard_deadline_at"] = "2026-07-23T04:00:04.000Z"
        for intent in drift["intents"]:
            intent["target_execution_hard_deadline_at"] = "2026-07-23T04:00:04.000Z"
            intent["intent_json"]["target_execution_hard_deadline_at"] = "2026-07-23T04:00:04.000Z"
        with pytest.raises(AtomicLedgerError):
            commit_target_atomic_bundle(conn, bundle=drift)


def test_typed_deadline_derivation_caps_to_quote_expiry() -> None:
    assert derive_target_execution_hard_deadline(
        "2026-07-23T00:00:00+09:00",
        quote_expires_at="2026-07-22T15:00:01Z",
    ) == "2026-07-22T15:00:01.000Z"
    with pytest.raises(TargetExecutionDeadlineError, match="timezone_required"):
        TargetExecutionHardDeadline.from_value("2026-07-23T00:00:03")
    with pytest.raises(TargetExecutionDeadlineError, match="expired"):
        TargetExecutionHardDeadline.from_value(
            "2026-07-23T00:00:03Z"
        ).assert_not_expired("2026-07-23T00:00:03Z")

