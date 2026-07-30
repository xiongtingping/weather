from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from scripts.accept_delta_basket_score_v2_no_live import accept


def _db(path: Path, *, order_events: int = 0) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE basket_target_order_events(event_id TEXT);
            CREATE TABLE auto_trade_decisions(
              decision_id TEXT,
              station_id TEXT,
              decided_state TEXT,
              will_order INTEGER,
              details_json TEXT,
              created_at TEXT,
              reason_codes_json TEXT
            );
            CREATE TABLE basket_policy_training_fact(
              fact_id TEXT,
              station_id TEXT,
              decision_id TEXT,
              policy_version_vector_json TEXT,
              feature_json TEXT,
              created_at_utc TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO basket_target_order_events VALUES(?)",
            [(f"event-{index}",) for index in range(order_events)],
        )
        conn.execute(
            "INSERT INTO auto_trade_decisions VALUES(?,?,?,?,?,?,?)",
            (
                "decision-v2",
                "RKPK",
                "blocked",
                0,
                json.dumps(
                    {
                        "delta_v2_authorized": True,
                        "execution_mode": "production_no_live",
                        "live_submit_authorized": False,
                    }
                ),
                "2026-07-28T13:30:00Z",
                "[]",
            ),
        )
        conn.execute(
            "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?,?)",
            (
                "d2fact_123",
                "RKPK",
                "decision-v2",
                json.dumps(
                    {
                        "schema": (
                            "delta_basket_score_policy_version_vector_v2"
                        ),
                        "delta_v2_artifact_sha256": "a" * 64,
                    }
                ),
                json.dumps(
                    {
                        "schema": (
                            "delta_basket_score_forward_feature_vector_v2"
                        ),
                        "execution_version_vectors": [],
                    }
                ),
                "2026-07-28T13:30:00Z",
            ),
        )


def test_accepts_exact_v2_fact_and_no_live_decision(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    _db(db)

    result = accept(
        db_path=db,
        baseline_decision_rowid=0,
        baseline_fact_rowid=0,
        baseline_order_event_count=0,
        timeout_seconds=1,
    )

    assert result["state"] == "PASSED"
    assert result["fact_id"] == "d2fact_123"
    assert result["order_event_count_unchanged"] is True
    assert result["live_submit_authorized"] is False


def test_fails_closed_when_order_event_count_changes(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    _db(db, order_events=1)

    with pytest.raises(RuntimeError, match="order_event_write_detected"):
        accept(
            db_path=db,
            baseline_decision_rowid=0,
            baseline_fact_rowid=0,
            baseline_order_event_count=0,
            timeout_seconds=1,
        )


def test_accepts_already_current_v2_scope_as_deterministic_zero_write(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _db(db)

    result = accept(
        db_path=db,
        baseline_decision_rowid=1,
        baseline_fact_rowid=1,
        baseline_order_event_count=0,
        timeout_seconds=1,
        allow_already_current=True,
    )

    assert result["state"] == "ACTIVATED_ALREADY_CURRENT_ZERO_WRITE"
    assert result["fact_id"] == "d2fact_123"
    assert result["resident_zero_write_replay"] is True
    assert result["orders_submitted"] == 0


@pytest.mark.parametrize(
    "reason",
    [
        "official_ws_evidence_complete_scope_missing",
        "production_quote_expired",
    ],
)
def test_accepts_exact_market_input_gap_when_authorized(
    tmp_path: Path,
    reason: str,
) -> None:
    db = tmp_path / "target.db"
    _db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM basket_policy_training_fact")
        conn.execute(
            "UPDATE auto_trade_decisions SET details_json=?,reason_codes_json=?",
            (
                json.dumps(
                    {
                        "execution_mode": "production_no_live",
                        "no_orders_submitted": True,
                    }
                ),
                json.dumps([reason]),
            ),
        )

    result = accept(
        db_path=db,
        baseline_decision_rowid=0,
        baseline_fact_rowid=0,
        baseline_order_event_count=0,
        timeout_seconds=1,
        allow_waiting_input=True,
    )

    assert result["state"] == "ACTIVATED_WAITING_INPUT_FAIL_CLOSED"
    assert result["reason"] == reason
    assert result["orders_submitted"] == 0
