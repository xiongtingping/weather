"""The native P0 compile keeps a scope's evidence test between compiles.

2026-10-03: the test walked every valid activation of the scope and sought
each one's artifact (1,400-2,800 random index pages), then asked a hundred
evaluation lookups -- 108 s of a 511 s production cycle -- for an answer that
moves when a refit mints or a batch expires, once or twice a day a scope.

The answer is a function of two sets, the activations valid at the decision
instant and the evaluations visible at it, and the three tables are
append-only by trigger, so equal id lists are equal sets.  These tests pin
that premise and every way the sets can move: a row arriving, the instant
passing a row's stamp, the window closing.
"""

from __future__ import annotations

import copy
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import native_ensemble_p0 as P0
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.native_ensemble_oos import persist_native_ensemble_oos
from bayes_basket_target.native_ensemble_p0 import compile_native_ensemble_p0
from bayes_basket_target.ssot import apply_production_ssot_schema

from tests.test_native_ensemble_p0 import (  # noqa: F401  (autouse fixtures)
    _contest_era_membership,
    _install_mixed_cycle_calibrations,
    _map_the_fixture_station_season,
    _mixed_packet,
)

EVIDENCE_TEST = "SELECT a.activation_id,r.selection_evidence_sha256"
LOOKUP = (
    "SELECT oos_state,selection_evidence_sha256,evaluated_at_utc "
    "FROM calibration_cross_native_ensemble_oos_evaluation"
)


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", True)
    P0._ADMITTED_SCOPE_MEMO.clear()
    P0._MEMO_TEXT.clear()
    yield
    P0._ADMITTED_SCOPE_MEMO.clear()
    P0._MEMO_TEXT.clear()


def _store(tmp_path: Path) -> tuple[sqlite3.Connection, dict]:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    report = _install_mixed_cycle_calibrations(conn)
    conn.commit()
    return conn, report


def _compile(conn: sqlite3.Connection, at: str = "2026-07-01T13:00:00Z") -> dict:
    return compile_native_ensemble_p0(
        conn, forward_packet=_mixed_packet(), decision_at_utc=at
    )


def _traced(conn: sqlite3.Connection, at: str = "2026-07-01T13:00:00Z"):
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        compiled = _compile(conn, at)
    finally:
        conn.set_trace_callback(None)
    flat = [" ".join(statement.split()) for statement in statements]
    return (
        compiled,
        sum(statement.startswith(EVIDENCE_TEST) for statement in flat),
        sum(statement.startswith(LOOKUP) for statement in flat),
    )


def _reject(conn: sqlite3.Connection, report: dict, at: str) -> None:
    rejected = copy.deepcopy(report)
    for candidate in rejected["cross_candidates"]:
        candidate["state"] = "OOS_REJECTED"
    rejected["content_sha256"] = content_sha256(
        {key: value for key, value in rejected.items() if key != "content_sha256"}
    )
    persisted = persist_native_ensemble_oos(conn, rejected, activated_at_utc=at)
    assert persisted["state"] == "COMMITTED_EVALUATIONS_ONLY"
    conn.commit()


def test_the_three_tables_are_append_only_which_is_what_the_memo_rests_on(tmp_path) -> None:
    conn, _ = _store(tmp_path)
    for table in (
        "calibration_native_ensemble_activation",
        "calibration_cross_native_ensemble_artifact",
        "calibration_cross_native_ensemble_oos_evaluation",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
        for statement in (
            f"UPDATE {table} SET station_id=station_id",
            f"DELETE FROM {table}",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="append_only"):
                conn.execute(statement)


def test_both_id_lists_are_read_off_covering_indexes() -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    P0._admitted_scope_digest(
        conn,
        activation_parameters=("S", "PEAK", "h12_24", "t", "t"),
        decision_at_utc="t",
    )
    conn.set_trace_callback(None)
    plans = [
        " | ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql))
        for sql in statements
    ]
    assert len(plans) == 2
    assert "COVERING INDEX calibration_native_activation_evidence_idx" in plans[0], plans
    assert (
        "COVERING INDEX calibration_cross_native_oos_evaluation_scope_idx" in plans[1]
    ), plans


def test_the_second_compile_of_a_scope_does_not_repeat_the_evidence_test(
    tmp_path, monkeypatch
) -> None:
    conn, _ = _store(tmp_path)
    first, tests_first, lookups_first = _traced(conn)
    second, tests_second, lookups_second = _traced(conn)
    assert (tests_first, tests_second) == (1, 0)
    # What is left is the selected activation's own confirmation, asked live
    # after the loop whether or not the scope was kept.
    assert lookups_first > lookups_second == 1
    assert len(P0._ADMITTED_SCOPE_MEMO) == 1
    monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", False)
    plain, tests_plain, _ = _traced(conn)
    assert tests_plain == 1
    assert first == second == plain


def test_a_rejection_arriving_after_the_scope_was_kept_is_seen(tmp_path) -> None:
    conn, report = _store(tmp_path)
    _compile(conn)
    assert len(P0._ADMITTED_SCOPE_MEMO) == 1
    _reject(conn, report, "2026-07-01T12:00:00Z")
    with pytest.raises(ContractError, match="native_cross_oos_evaluation_not_passing"):
        _compile(conn)


def test_a_rejection_stamped_after_the_instant_is_seen_once_the_instant_passes_it(
    tmp_path,
) -> None:
    conn, report = _store(tmp_path)
    _reject(conn, report, "2026-07-01T12:30:00Z")
    # Not yet visible at 12:10: the scope serves, and is kept for that set.
    before = _compile(conn, "2026-07-01T12:10:00Z")
    assert before["packet"]["native_probability_envelope"]
    assert len(P0._ADMITTED_SCOPE_MEMO) == 1
    # Nothing was written since; only the instant moved past the stamp.
    with pytest.raises(ContractError, match="native_cross_oos_evaluation_not_passing"):
        _compile(conn, "2026-07-01T13:00:00Z")


def test_a_window_that_closed_is_not_served_from_what_was_kept(tmp_path, monkeypatch) -> None:
    conn, _ = _store(tmp_path)
    _compile(conn)
    closes = conn.execute(
        "SELECT MAX(valid_until_utc) FROM calibration_native_ensemble_activation "
        "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE'"
    ).fetchone()[0]
    reasons = []
    for enabled in (True, False):
        monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", enabled)
        with pytest.raises(ContractError) as raised:
            _compile(conn, closes)
        reasons.append(str(raised.value))
    assert reasons[0] == reasons[1]


def test_a_store_with_no_file_keeps_nothing() -> None:
    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    _, tests_first, _ = _traced(conn)
    _, tests_second, _ = _traced(conn)
    assert (tests_first, tests_second) == (1, 1)
    assert P0._ADMITTED_SCOPE_MEMO == {}


def test_two_stores_do_not_answer_for_each_other(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, _ = _store(tmp_path / "a")
    second, report = _store(tmp_path / "b")
    _compile(first)
    _reject(second, report, "2026-07-01T12:00:00Z")
    with pytest.raises(ContractError, match="native_cross_oos_evaluation_not_passing"):
        _compile(second)
    assert _compile(first)["packet"]["native_probability_envelope"]
