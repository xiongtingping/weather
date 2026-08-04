from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from bayes_basket_target.p1_atomic_chain_smoke import (
    P1AtomicChainSmokeError,
    audit_p1_atomic_chain,
)


TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
from test_bayes_basket_target_durable_ssot_replay import (  # noqa: E402
    DAY,
    STATION,
    _append_hold,
    _seed,
)


def test_p1_atomic_chain_smoke_is_read_only_and_binds_full_no_execution_chain(
    tmp_path: Path,
) -> None:
    conn, path = _seed(tmp_path)
    _append_hold(
        conn, ordinal=1, stage="P1", decision_at="2026-07-28T15:01:00Z"
    )
    _append_hold(
        conn, ordinal=2, stage="P1", decision_at="2026-07-28T15:02:00Z"
    )
    conn.commit()
    conn.close()
    before = path.read_bytes()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as read_conn:
        read_conn.row_factory = sqlite3.Row
        receipt = audit_p1_atomic_chain(
            read_conn, station_id=STATION, event_date=DAY
        )
    assert path.read_bytes() == before
    assert receipt["state"] == "PASS"
    assert receipt["replayed_fact_count"] == 2
    assert receipt["p1_only"] is True
    assert receipt["no_execution_only"] is True
    assert receipt["boundary"]["writes_production_db"] is False
    assert receipt["boundary"]["orders_submitted"] == 0


def test_p1_atomic_chain_rejects_training_fact_hash_mismatch(
    tmp_path: Path,
) -> None:
    conn, _path = _seed(tmp_path)
    _append_hold(
        conn, ordinal=1, stage="P1", decision_at="2026-07-28T15:01:00Z"
    )
    conn.execute("DROP TRIGGER basket_training_fact_no_update")
    conn.execute(
        "UPDATE basket_policy_training_fact SET content_sha256=?", ("f" * 64,)
    )
    conn.commit()
    with pytest.raises(P1AtomicChainSmokeError, match="training_fact_content_drift"):
        audit_p1_atomic_chain(conn, station_id=STATION, event_date=DAY)


def test_p1_atomic_chain_rejects_time_inversion(
    tmp_path: Path,
) -> None:
    conn, _path = _seed(tmp_path)
    _append_hold(
        conn, ordinal=1, stage="P1", decision_at="2026-07-28T15:02:00Z"
    )
    _append_hold(
        conn, ordinal=2, stage="P1", decision_at="2026-07-28T15:01:00Z"
    )
    conn.commit()
    with pytest.raises(P1AtomicChainSmokeError, match="time_moved_backwards"):
        audit_p1_atomic_chain(conn, station_id=STATION, event_date=DAY)


def test_p1_atomic_chain_rejects_non_p1_candidate(tmp_path: Path) -> None:
    conn, _path = _seed(tmp_path)
    _append_hold(
        conn, ordinal=1, stage="P0", decision_at="2026-07-28T15:01:00Z"
    )
    conn.commit()
    with pytest.raises(P1AtomicChainSmokeError, match="candidate_missing"):
        audit_p1_atomic_chain(conn, station_id=STATION, event_date=DAY)


def test_p1_atomic_chain_rejects_execution_vector_even_when_replay_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, _path = _seed(tmp_path)
    _append_hold(
        conn, ordinal=1, stage="P1", decision_at="2026-07-28T15:01:00Z"
    )
    conn.commit()

    def _execution_replay(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "state": "PASS",
            "rows": [{
                "stage": "P1",
                "execution_policy": "EXECUTION",
                "training_fact_present": True,
                "intent_count": 1,
                "reserve_amount_usdc": 1.0,
                "decision_state": "prepared",
            }],
            "continuation": {"chain_sha256": "a" * 64},
            "registry_set_sha256": "b" * 64,
        }

    monkeypatch.setattr(
        "bayes_basket_target.p1_atomic_chain_smoke.replay_durable_target_chain",
        _execution_replay,
    )
    with pytest.raises(P1AtomicChainSmokeError, match="vector_not_no_execution"):
        audit_p1_atomic_chain(conn, station_id=STATION, event_date=DAY)
