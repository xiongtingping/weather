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


WALKS = (
    "SELECT a.activation_id,a.valid_from_utc,a.valid_until_utc",
    "SELECT evaluation_id,evaluated_at_utc",
)
ROWS_WHOLE = "SELECT a.activation_id,a.artifact_id,a.content_sha256"


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", True)
    P0.forget_admitted_scopes()
    yield
    P0.forget_admitted_scopes()


def _statements(conn: sqlite3.Connection, at: str) -> tuple[dict, list[str]]:
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        compiled = _compile(conn, at)
    finally:
        conn.set_trace_callback(None)
    return compiled, [" ".join(statement.split()) for statement in statements]


def _count(flat: list[str], *heads: str) -> int:
    return sum(statement.startswith(heads) for statement in flat)


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


def test_inside_the_horizon_with_no_row_added_nothing_is_walked_or_read_again(
    tmp_path, monkeypatch
) -> None:
    conn, _ = _store(tmp_path)
    first, flat_first = _statements(conn, "2026-07-01T13:00:00Z")
    # The evidence test's two lists (the activation list is also the one
    # the lead bucket's horizon is read from, once per candidate).
    assert _count(flat_first, WALKS[1]) == 1 and _count(flat_first, WALKS[0]) >= 1
    assert _count(flat_first, ROWS_WHOLE) == 1
    later, flat_later = _statements(conn, "2026-07-01T13:07:00Z")
    assert _count(flat_later, *WALKS) == 0
    assert _count(flat_later, EVIDENCE_TEST) == 0
    assert _count(flat_later, ROWS_WHOLE) == 0
    monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", False)
    assert later == _compile(conn, "2026-07-01T13:07:00Z")


APPENDED_EVALUATIONS = "SELECT rowid,run_cycle,run_cycle_by_system_json"
APPENDED_ACTIVATIONS = "SELECT a.rowid,a.activation_id"


def _full_tests(flat: list[str]) -> int:
    """Evidence tests over the whole scope (the increment's own scan names
    the pairings it re-tests)."""

    return sum(
        statement.startswith(EVIDENCE_TEST) and " IN (" not in statement
        for statement in flat
    )


def test_a_row_stamped_ahead_is_stepped_not_walked_and_seen_at_its_stamp(
    tmp_path, monkeypatch
) -> None:
    conn, report = _store(tmp_path)
    _compile(conn, "2026-07-01T13:00:00Z")
    # Stamped after both instants: the scope's rows grew, but nothing new is
    # in view.  The appended rows are read by rowid; nothing is walked.
    _reject(conn, report, "2026-07-01T13:20:00Z")
    _, flat = _statements(conn, "2026-07-01T13:07:00Z")
    assert _count(flat, WALKS[1]) == 0 and _full_tests(flat) == 0
    assert _count(flat, APPENDED_EVALUATIONS) == 1
    # The row ahead holds the base back: read again, still not walked.
    _, flat = _statements(conn, "2026-07-01T13:08:00Z")
    assert _count(flat, WALKS[1]) == 0 and _full_tests(flat) == 0
    assert _count(flat, APPENDED_EVALUATIONS) == 1
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    with pytest.raises(ContractError, match="native_cross_oos_evaluation_not_passing"):
        _compile(conn, "2026-07-01T13:20:00Z")
    conn.set_trace_callback(None)
    flat_at = [" ".join(statement.split()) for statement in statements]
    assert _count(flat_at, WALKS[1]) == 0 and _full_tests(flat_at) == 0
    monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", False)
    with pytest.raises(ContractError, match="native_cross_oos_evaluation_not_passing"):
        _compile(conn, "2026-07-01T13:20:00Z")


def _re_mint(
    conn: sqlite3.Connection, *, suffix: str, valid_from: str,
    valid_until: str | None = None,
) -> None:
    """A re-mint of the kept scope's pairing: the same artifact, a new
    activation one generation up (what the OOS rotation writes)."""

    columns = [c[1] for c in conn.execute(
        "PRAGMA table_info(calibration_native_ensemble_activation)"
    )]
    row = dict(zip(columns, conn.execute(
        "SELECT * FROM calibration_native_ensemble_activation "
        "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE' "
        "ORDER BY generation DESC LIMIT 1"
    ).fetchone()))
    row.update(
        activation_id=f"native-activation-re-mint-{suffix}",
        content_sha256=content_sha256({"re_mint": suffix}),
        valid_from_utc=valid_from,
        activated_at_utc=valid_from,
        generation=int(row["generation"]) + 1,
    )
    if valid_until is not None:
        row["valid_until_utc"] = valid_until
    conn.execute(
        f"INSERT INTO calibration_native_ensemble_activation({','.join(columns)}) "
        f"VALUES({','.join('?' * len(columns))})",
        tuple(row[c] for c in columns),
    )
    conn.commit()


def _answers(conn: sqlite3.Connection, at: str):
    try:
        return ("ok", _compile(conn, at))
    except ContractError as exc:
        return ("refused", str(exc))


def test_a_re_mint_of_this_scope_is_stepped_to_the_walk_s_answer(
    tmp_path, monkeypatch
) -> None:
    """2026-10-15: every re-mint walked the scope whole again -- ~6,000
    activations an h00_12 scope, 10-25 s each, 100-620 s in the cycles the
    rotation bunched in."""

    conn, report = _store(tmp_path)
    _compile(conn, "2026-07-01T13:00:00Z")
    _re_mint(conn, suffix="a", valid_from="2026-07-01T13:01:00Z")
    stepped, flat = _statements(conn, "2026-07-01T13:07:00Z")
    assert _count(flat, WALKS[1]) == 0 and _full_tests(flat) == 0
    assert _count(flat, APPENDED_ACTIVATIONS) == 1
    kept = next(iter(P0._ADMITTED_SCOPE_MEMO.values()))
    assert "native-activation-re-mint-a" in kept[1]
    # Minted ahead of the instant: held back, then admitted at its opening.
    _re_mint(conn, suffix="b", valid_from="2026-07-01T13:30:00Z")
    held, flat = _statements(conn, "2026-07-01T13:08:00Z")
    assert _count(flat, WALKS[1]) == 0 and _full_tests(flat) == 0
    opened, flat = _statements(conn, "2026-07-01T13:30:00Z")
    assert _count(flat, WALKS[1]) == 0 and _full_tests(flat) == 0
    kept = next(iter(P0._ADMITTED_SCOPE_MEMO.values()))
    assert "native-activation-re-mint-b" in kept[1]
    # A rejection in view now: every activation of the pairing is tested
    # again, by the scan that names it.
    _reject(conn, report, "2026-07-01T13:31:00Z")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    rejected = _answers(conn, "2026-07-01T13:32:00Z")
    conn.set_trace_callback(None)
    flat = [" ".join(statement.split()) for statement in statements]
    assert _count(flat, WALKS[1]) == 0 and _full_tests(flat) == 0
    assert sum(
        s.startswith(EVIDENCE_TEST) and " IN (" in s for s in flat
    ) == 1
    assert rejected[0] == "refused"
    monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", False)
    assert stepped == _compile(conn, "2026-07-01T13:07:00Z")
    assert held == _compile(conn, "2026-07-01T13:08:00Z")
    assert opened == _compile(conn, "2026-07-01T13:30:00Z")
    assert rejected == _answers(conn, "2026-07-01T13:32:00Z")


def test_an_admitted_row_closing_leaves_without_a_walk(tmp_path, monkeypatch) -> None:
    conn, _ = _store(tmp_path)
    _re_mint(
        conn, suffix="late", valid_from="2026-07-01T12:00:00Z",
        valid_until="2026-07-01T14:00:00Z",
    )
    _compile(conn, "2026-07-01T13:00:00Z")
    kept = next(iter(P0._ADMITTED_SCOPE_MEMO.values()))
    assert len(kept[1]) >= 2
    closes = min(kept[5][3].values())
    later = max(kept[5][3].values())
    assert closes < later
    closed, flat = _statements(conn, closes)
    assert _count(flat, WALKS[1]) == 0 and _full_tests(flat) == 0
    kept = next(iter(P0._ADMITTED_SCOPE_MEMO.values()))
    assert all(until > closes for until in kept[5][3].values())
    monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", False)
    assert closed == _compile(conn, closes)


def test_an_instant_before_the_one_kept_walks_the_lists(tmp_path, monkeypatch) -> None:
    conn, _ = _store(tmp_path)
    _compile(conn, "2026-07-01T13:07:00Z")
    earlier, flat = _statements(conn, "2026-07-01T13:00:00Z")
    assert _count(flat, WALKS[1]) == 1 and _count(flat, WALKS[0]) >= 1
    monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", False)
    assert earlier == _compile(conn, "2026-07-01T13:00:00Z")


def test_a_selection_evidence_read_once_is_not_read_again(tmp_path) -> None:
    conn, _ = _store(tmp_path)
    sha, text = conn.execute(
        "SELECT content_sha256,selected_weights_json "
        "FROM calibration_cross_native_ensemble_selection_evidence LIMIT 1"
    ).fetchone()
    absent = "0" * 64
    statements: list[str] = []
    first = P0._selection_weights_by_sha(conn, [sha, absent])
    conn.set_trace_callback(statements.append)
    second = P0._selection_weights_by_sha(conn, [sha])
    assert not [s for s in statements if "selection_evidence" in s]
    third = P0._selection_weights_by_sha(conn, [sha, absent])
    conn.set_trace_callback(None)
    # The sha that was not there is asked for again; the one that was is not.
    asked = [s for s in statements if "selection_evidence" in s]
    assert len(asked) == 1 and absent in asked[0] and sha not in asked[0]
    assert first == second == third and set(first) == {sha}
    assert first[sha] == {k: float(v) for k, v in __import__("json").loads(text).items()}
    second[sha]["x"] = 1.0  # a caller's copy is its own
    assert "x" not in P0._selection_weights_by_sha(conn, [sha])[sha]


LEAD_WALK = "SELECT r.ensemble_system_ids_json,a.run_cycle_by_system_json"
SYSTEM_READ = "SELECT activation_id,artifact_id,content_sha256,generation,"


def test_the_lead_bucket_and_the_system_fits_are_not_asked_again_inside_the_horizon(
    tmp_path, monkeypatch
) -> None:
    conn, report = _store(tmp_path)
    first, flat_first = _statements(conn, "2026-07-01T13:00:00Z")
    assert _count(flat_first, LEAD_WALK) >= 1 and _count(flat_first, SYSTEM_READ) >= 2
    later, flat_later = _statements(conn, "2026-07-01T13:07:00Z")
    assert _count(flat_later, LEAD_WALK) == 0 and _count(flat_later, SYSTEM_READ) == 0
    # An evaluation added moves neither read: they are of activations.
    _reject(conn, report, "2026-07-01T13:20:00Z")
    _, flat = _statements(conn, "2026-07-01T13:08:00Z")
    assert _count(flat, LEAD_WALK) == 0 and _count(flat, SYSTEM_READ) == 0
    monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", False)
    plain, flat_plain = _statements(conn, "2026-07-01T13:07:00Z")
    assert _count(flat_plain, LEAD_WALK) >= 1 and _count(flat_plain, SYSTEM_READ) >= 2
    assert later == plain


def test_an_activation_reaching_its_edge_is_read_again(tmp_path, monkeypatch) -> None:
    conn, _ = _store(tmp_path)
    _compile(conn, "2026-07-01T13:00:00Z")
    kept = dict(P0._SYSTEM_ACTIVATION_MEMO)
    assert kept and P0._LEAD_BUCKET_MEMO
    edges = {entry[1] for entry in kept.values()} | {
        entry[1] for entry in P0._LEAD_BUCKET_MEMO.values()
    }
    closes = conn.execute(
        "SELECT MIN(valid_until_utc) FROM calibration_native_ensemble_activation"
    ).fetchone()[0]
    # Every horizon kept is a window edge of the store, none past the first
    # to close; at that instant the reads are made again.
    assert all(edge <= closes or edge == P0._NO_HORIZON for edge in edges)
    assert closes in edges
    reasons = []
    for enabled in (True, False):
        monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", enabled)
        try:
            reasons.append(("ok", _compile(conn, closes)))
        except ContractError as exc:
            reasons.append(("refused", str(exc)))
    assert reasons[0] == reasons[1]


def test_the_window_horizon_is_the_first_edge_ahead() -> None:
    at = "2026-07-01T13:00:00Z"
    assert P0._window_horizon([], at) == P0._NO_HORIZON
    assert P0._window_horizon(
        [
            ("2026-06-01T00:00:00Z", "2026-07-20T00:00:00Z"),  # valid: closes
            ("2026-06-01T00:00:00Z", "2026-07-09T00:00:00Z"),  # valid: closes first
            ("2026-07-05T00:00:00Z", "2026-08-05T00:00:00Z"),  # minted ahead: opens
        ],
        at,
    ) == "2026-07-05T00:00:00Z"


def test_a_re_mint_elsewhere_does_not_walk_a_scope_whose_ids_are_unchanged(
    tmp_path, monkeypatch
) -> None:
    """2026-10-03 15:20-16:00Z: the OOS rotation re-minted a batch every ~20
    minutes, every batch moved the table marks, and every compile walked its
    lead bucket again -- 108-140 walks, 15-28 s a cycle -- though a batch
    touches a few scopes of the fleet."""

    conn, _ = _store(tmp_path)
    first = _compile(conn, "2026-07-01T13:00:00Z")
    # A re-mint of another station's scope: the activation table grows.
    row = conn.execute(
        "SELECT * FROM calibration_native_ensemble_activation "
        "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE' LIMIT 1"
    ).fetchone()
    columns = [c[1] for c in conn.execute("PRAGMA table_info(calibration_native_ensemble_activation)")]
    other = dict(zip(columns, row))
    other.update(
        activation_id="native-activation-elsewhere", station_id="ELSEWHERE",
        content_sha256="e" * 64,
        run_cycle_by_system_sha256=other.get("run_cycle_by_system_sha256"),
    )
    conn.execute(
        f"INSERT INTO calibration_native_ensemble_activation({','.join(columns)}) "
        f"VALUES({','.join('?' * len(columns))})",
        tuple(other[c] for c in columns),
    )
    conn.commit()
    later, flat = _statements(conn, "2026-07-01T13:07:00Z")
    assert _count(flat, LEAD_WALK) == 0 and _count(flat, SYSTEM_READ) == 0
    # Not even the id lists: the rows appended are read once, by rowid, and
    # none of them is this scope's.
    assert _count(flat, *WALKS) == 0 and _count(flat, EVIDENCE_TEST) == 0
    assert _count(flat, "SELECT activation_id,valid_from_utc,valid_until_utc") == 0
    assert _count(flat, "SELECT rowid,artifact_kind,station_id") == 1
    monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", False)
    assert later == _compile(conn, "2026-07-01T13:07:00Z")


def test_a_re_mint_of_this_scope_is_looked_at_again(tmp_path, monkeypatch) -> None:
    conn, _ = _store(tmp_path)
    _compile(conn, "2026-07-01T13:00:00Z")
    columns = [c[1] for c in conn.execute("PRAGMA table_info(calibration_native_ensemble_activation)")]
    # A system fit the compile actually read, and a cross row of its scope.
    _db, station, system, season, lead, cycle = next(
        key for key, kept in P0._SYSTEM_ACTIVATION_MEMO.items() if kept[2]
    )
    asked = {
        "NATIVE_ENSEMBLE_SYSTEM_CALIBRATION": (
            "AND station_id=? AND ensemble_system_id_or_star=? AND season_bin=? "
            "AND lead_bucket=? AND run_cycle=?",
            (station, system, season, lead, cycle),
        ),
        "CROSS_NATIVE_ENSEMBLE": ("AND station_id=?", (station,)),
    }
    for kind in ("CROSS_NATIVE_ENSEMBLE", "NATIVE_ENSEMBLE_SYSTEM_CALIBRATION"):
        where, params = asked[kind]
        row = dict(zip(columns, conn.execute(
            "SELECT * FROM calibration_native_ensemble_activation WHERE artifact_kind=? "
            + where + " ORDER BY rowid LIMIT 1", (kind, *params),
        ).fetchone()))
        # The same scope, a window that has not opened yet: the answer does
        # not change, but the scope was touched and has to be looked at.
        row.update(
            activation_id=f"native-activation-again-{kind}",
            content_sha256=(kind[0] * 64)[:64],
            valid_from_utc="2026-07-01T20:00:00Z",
            generation=int(row["generation"]) + 100,
        )
        conn.execute(
            f"INSERT INTO calibration_native_ensemble_activation({','.join(columns)}) "
            f"VALUES({','.join('?' * len(columns))})",
            tuple(row[c] for c in columns),
        )
    conn.commit()
    later, flat = _statements(conn, "2026-07-01T13:07:00Z")
    assert _count(flat, WALKS[0]) >= 1
    assert _count(flat, "SELECT activation_id,valid_from_utc,valid_until_utc") >= 1
    monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", False)
    assert later == _compile(conn, "2026-07-01T13:07:00Z")


def test_the_appended_rows_are_read_by_rowid_not_by_scope_or_kind(tmp_path) -> None:
    """The scope and kind indexes would walk every row of the scope (~6,000)
    or of the kind (~900,000) to find the few appended; the unary + leaves the
    planner only the rowid range."""

    conn, report = _store(tmp_path)
    _compile(conn, "2026-07-01T13:00:00Z")
    _re_mint(conn, suffix="p", valid_from="2026-07-01T13:01:00Z")
    _reject(conn, report, "2026-07-01T13:02:00Z")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    _answers(conn, "2026-07-01T13:07:00Z")
    conn.set_trace_callback(None)
    appended = [
        s for s in statements
        if s.startswith((APPENDED_EVALUATIONS, APPENDED_ACTIVATIONS))
    ]
    assert len(appended) == 2
    for sql in appended:
        plan = " | ".join(
            str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql)
        )
        assert plan.split(" | ")[0].endswith(
            "USING INTEGER PRIMARY KEY (rowid>? AND rowid<?)"
        ), plan


def test_a_walk_the_rotation_wrote_into_still_keeps_its_base(
    tmp_path, monkeypatch
) -> None:
    """2026-10-15 16:08Z: the rotation wrote ~800 activations while the first
    cycle's walks ran, every such walk kept nothing, and the next cycle walked
    the same 14 scopes again.  The base read before the walk is still sound:
    the next increment reads whatever arrived past it."""

    conn, _ = _store(tmp_path)
    digest = P0._admitted_scope_digest

    def digest_then_re_mint(*args, **kwargs):
        answer = digest(*args, **kwargs)
        if not getattr(digest_then_re_mint, "done", False):
            digest_then_re_mint.done = True
            _re_mint(conn, suffix="during", valid_from="2026-07-01T12:59:00Z")
        return answer

    monkeypatch.setattr(P0, "_admitted_scope_digest", digest_then_re_mint)
    _compile(conn, "2026-07-01T13:00:00Z")
    kept = next(iter(P0._ADMITTED_SCOPE_MEMO.values()))
    assert kept[0] == b"" and kept[4] is None and kept[5] is not None
    stepped, flat = _statements(conn, "2026-07-01T13:07:00Z")
    assert _count(flat, WALKS[1]) == 0 and _full_tests(flat) == 0
    kept = next(iter(P0._ADMITTED_SCOPE_MEMO.values()))
    assert "native-activation-re-mint-during" in kept[1]
    monkeypatch.setattr(P0, "ADMITTED_SCOPE_MEMO_ENABLED", False)
    assert stepped == _compile(conn, "2026-07-01T13:07:00Z")


def test_each_walk_names_why_the_kept_answer_was_declined(tmp_path) -> None:
    """The cycle history's span report says what still sends scopes to the
    walk (2026-10-16: 66-163 s spikes with no way to tell which fallback)."""

    from bayes_basket_target import atomic_ledger

    conn, _ = _store(tmp_path)
    atomic_ledger.reset_ledger_spans()
    _compile(conn, "2026-07-01T13:00:00Z")
    _re_mint(conn, suffix="s", valid_from="2026-07-01T13:01:00Z")
    _compile(conn, "2026-07-01T13:07:00Z")
    _compile(conn, "2026-07-01T13:00:00Z")
    spans = atomic_ledger.ledger_span_profile()
    atomic_ledger.reset_ledger_spans()
    assert spans["p0_evidence_walk_cold"]["count"] == 1
    assert spans["p0_evidence_step"]["count"] >= 1
    assert spans["p0_evidence_walk_earlier_instant"]["count"] == 1
