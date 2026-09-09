"""P1 under the effective-edge machinery: its own table, its own fit, one dispatch.

Charter 68 exempted P1 by leaving it out of a stage set.  The 2026-09-02 ruling
turns that absence into a fitted row: P1 reads a table of its own through the
same function P0 reads its table through, and the store fits that table on P1's
own settled seats, never pooled with P0's.  Three things must hold for that to
be a safe change on an armed book, and each is pinned here: today's P1 table is
identity, so nothing P1 sizes moves; a P1 table cannot reach a P0 seat nor a P0
table a P1 seat; and the store's two scopes cannot see each other's rows or
retire each other's activation.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    EFFECTIVE_EDGE_LAMBDA,
    EFFECTIVE_EDGE_P0M_LAMBDA,
    EFFECTIVE_EDGE_P1_LAMBDA,
    center_window_claim_factor,
    effective_edge_lambda,
    effective_edge_lambda_for_stage,
)
from bayes_basket_target import effective_edge_store as store  # noqa: E402


def test_p1_table_is_identity_today_so_nothing_p1_sizes_moves() -> None:
    """The exemption survives as a row of ones until a band is promoted."""

    assert all(lam == 1.0 for _bound, lam in EFFECTIVE_EDGE_P1_LAMBDA)
    for agreement in (0.1, 0.45, 0.6, 0.8, 0.95, 3.0):
        assert effective_edge_lambda_for_stage("P1", agreement) == 1.0


def test_p0_and_p0m_read_the_charter_68_table_exactly_as_before() -> None:
    """P0-M's own table starts equal to P0's (user ruling, 2026-09-05), so
    the split changes no served number until P0-M earns its own bands."""
    for stage in ("P0", "P0-M"):
        for agreement in (0.1, 0.45, 0.6, 0.8, 0.95, 3.0):
            assert effective_edge_lambda_for_stage(stage, agreement) == (
                effective_edge_lambda(agreement)
            )
    assert EFFECTIVE_EDGE_P0M_LAMBDA == EFFECTIVE_EDGE_LAMBDA
    # Handing P1's table to a P0 seat must not reach it.
    assert effective_edge_lambda_for_stage(
        "P0", 0.3, p1_table=((float("inf"), 0.5),)
    ) == effective_edge_lambda(0.3)


def test_a_p1_table_handed_in_governs_p1_only() -> None:
    """A replay arm's per-day fit reaches P1 and nothing else."""

    banded = ((0.5, 0.8), (0.7, 0.9), (0.85, 0.95), (float("inf"), 1.0))
    assert effective_edge_lambda_for_stage("P1", 0.3, p1_table=banded) == 0.8
    assert effective_edge_lambda_for_stage("P1", 0.6, p1_table=banded) == 0.9
    assert effective_edge_lambda_for_stage("P1", 0.9, p1_table=banded) == 1.0
    # Same table offered as the P0 override does nothing to P1.
    assert effective_edge_lambda_for_stage("P1", 0.3, table=banded) == 1.0


def test_an_unfitted_stage_reads_one() -> None:
    """An unfitted shrink is a guess wearing a number."""

    assert effective_edge_lambda_for_stage("P2", 0.3) == 1.0
    assert effective_edge_lambda_for_stage("", 0.3) == 1.0



def _candidate_table(conn: sqlite3.Connection) -> None:
    """The population the fit reads: one row per enumerated window."""

    conn.execute(
        "CREATE TABLE paper_trade_candidate_window("
        "candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, buckets_json TEXT,"
        " win_probability REAL, all_in_price REAL, hit INTEGER)"
    )


def _candidate(conn, index, stage, day, claim, cost, hit, station=None):
    """One scored window.  ``claim`` raw in, lifted on disk -- the
    enumeration lifts before the gate divides, so the fit must not."""

    conn.execute(
        "INSERT INTO paper_trade_candidate_window VALUES(?,?,?,?,?,?,?,?,?)",
        (
            f"cand-{index}", station or f"S{index}", day, stage,
            f"{day}T00:00:00Z", '["b"]',
            claim * center_window_claim_factor(claim), cost, hit,
        ),
    )

def _book(rows) -> sqlite3.Connection:
    """The two ledger tables the store reads, with one seat per row."""

    conn = sqlite3.connect(":memory:")
    _candidate_table(conn)
    conn.execute(
        "CREATE TABLE paper_trade_settlement("
        "settlement_id TEXT PRIMARY KEY, position_id TEXT, stage TEXT,"
        " event_date TEXT, hit INTEGER)"
    )
    for index, (stage, claim, cost, hit) in enumerate(rows):
        _candidate(conn, index, stage, "2026-09-01", claim, cost, hit)
        conn.execute(
            "INSERT INTO paper_trade_settlement VALUES(?,?,?,?,?)",
            (f"set-{index}", f"pos-{index}", stage, "2026-09-01", hit),
        )
    return conn


def test_the_store_fits_p1_on_p1_rows_alone(monkeypatch) -> None:
    """Twenty P1 seats that all hit at a = 0.3 read 1.0 for P1 and change
    nothing in P0's fit, however loudly they would have voted in a pool."""

    import bayes_basket_target.basket_kernel.delta_v2 as K
    monkeypatch.setattr(K, "CENTER_WINDOW_CLAIM_BANDS", ())  # charter 81 off: raw arithmetic
    p1_rows = [("P1", 0.5, 0.15, 1) for _ in range(20)]      # a = 0.30
    p0_rows = [("P0", 0.5, 0.15, 0) for _ in range(20)]      # a = 0.30, all miss
    conn = _book(p1_rows + p0_rows)
    p1 = store.fit_lambda_table(conn, stage_scope="P1")
    p0 = store.fit_lambda_table(conn, stage_scope="P0")
    assert p1["table"]["stages"] == ["P1"]
    assert p0["table"]["stages"] == ["P0"]
    # P1's first band: 20 hits on claim mass 10 -> raw 2.0, capped at one.
    assert p1["bands"][0][1] == 1.0
    assert p1["evidence"][0]["raw_lambda"] == pytest.approx(2.0)
    # P0's first band: 0 hits on claim mass 10 -> raw 0, floored, not lifted
    # by the P1 seats sitting in the same agreement band.
    assert p0["bands"][0][1] == store.LAMBDA_FLOOR
    assert p0["evidence"][0]["seats"] == 20


def test_the_p1_ceiling_is_one_where_p0s_is_not() -> None:
    assert store.P1_LAMBDA_CEILING == 1.0
    assert store.LAMBDA_CEILING > 1.0


def test_a_thin_p1_band_keeps_the_identity_incumbent() -> None:
    """Below the seat floor the band is reported as accumulating and served at
    the incumbent -- which for P1 is one, the exemption standing."""

    conn = _book([("P1", 0.5, 0.45, 0) for _ in range(3)])   # a = 0.9, 3 seats
    fit = store.fit_lambda_table(conn, stage_scope="P1")
    assert fit["bands"][-1][1] == 1.0
    assert fit["evidence"][-1]["accumulating"] is True


def test_activating_one_scope_does_not_retire_the_other() -> None:
    conn = _book(
        [("P1", 0.5, 0.15, 1) for _ in range(20)]
        + [("P0", 0.5, 0.15, 1) for _ in range(20)]
    )
    store.persist_and_activate(conn, store.fit_lambda_table(conn, stage_scope="P0"))
    store.persist_and_activate(conn, store.fit_lambda_table(conn, stage_scope="P1"))
    active = conn.execute(
        "SELECT stage_scope, COUNT(*) FROM calibration_effective_edge_lambda"
        " WHERE activated=1 GROUP BY stage_scope ORDER BY stage_scope"
    ).fetchall()
    assert active == [("P0", 1), ("P1", 1)]
    assert store.load_active_lambda(conn, stage_scope="P0") is not None
    assert store.load_active_lambda(conn, stage_scope="P1") is not None
    # Re-activating P0 retires only P0's earlier row.
    store.persist_and_activate(conn, store.fit_lambda_table(conn, stage_scope="P0", window_days=7))
    active = dict(conn.execute(
        "SELECT stage_scope, COUNT(*) FROM calibration_effective_edge_lambda"
        " WHERE activated=1 GROUP BY stage_scope"
    ).fetchall())
    assert active == {"P0": 1, "P1": 1}


def test_rows_written_before_scopes_existed_read_as_p0() -> None:
    """The column arrives with a default, so an old activated row is P0's."""

    conn = sqlite3.connect(":memory:")
    conn.execute(store.TABLE_DDL)
    conn.execute(store.JOURNAL_DDL)
    conn.execute(
        "INSERT INTO calibration_effective_edge_lambda("
        " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
        " window_days, seat_count, activated, activated_at_utc)"
        " VALUES('a'*1, ?, '[]', '2026-09-01T00:00:00', 28, 100, 1,"
        " '2026-09-01T00:00:00')",
        (json.dumps({"bands": [{"upper_bound": None, "lambda": 0.7}]}),),
    )
    store.ensure_schema(conn)
    assert store.load_active_lambda(conn, stage_scope="P0") == (
        (float("inf"), 0.7),
    )
    assert store.load_active_lambda(conn, stage_scope="P1") is None


def test_a_p0m_table_handed_in_governs_p0m_only() -> None:
    """Each stage reads its own table (user ruling, 2026-09-05)."""
    banded = ((0.5, 0.3), (0.7, 0.4), (0.85, 0.5), (float("inf"), 0.6))
    assert effective_edge_lambda_for_stage("P0-M", 0.3, p0m_table=banded) == 0.3
    assert effective_edge_lambda_for_stage("P0-M", 0.9, p0m_table=banded) == 0.6
    # P0-M's table reaches neither P0 nor P1.
    assert effective_edge_lambda_for_stage("P0", 0.3, p0m_table=banded) == (
        effective_edge_lambda(0.3)
    )
    assert effective_edge_lambda_for_stage("P1", 0.3, p0m_table=banded) == 1.0
    # And P0's override does not reach P0-M.
    assert effective_edge_lambda_for_stage(
        "P0-M", 0.3, table=((float("inf"), 0.01),)
    ) == effective_edge_lambda(0.3)


def test_the_store_fits_p0m_on_p0m_rows_alone() -> None:
    """Twenty P0-M misses at a = 0.3 floor P0-M's first band and leave P0's
    fit -- twenty hits in the same band -- untouched."""
    conn = _book(
        [("P0-M", 0.5, 0.15, 0) for _ in range(20)]
        + [("P0", 0.5, 0.15, 1) for _ in range(20)]
    )
    p0m = store.fit_lambda_table(conn, stage_scope="P0-M")
    p0 = store.fit_lambda_table(conn, stage_scope="P0")
    assert p0m["table"]["stages"] == ["P0-M"]
    assert p0["table"]["stages"] == ["P0"]
    assert p0m["bands"][0][1] == store.LAMBDA_FLOOR
    assert p0["bands"][0][1] == store.LAMBDA_CEILING
    assert p0m["evidence"][0]["incumbent_lambda"] == EFFECTIVE_EDGE_P0M_LAMBDA[0][1]


def test_a_fixture_without_receipts_counts_every_seat_and_says_so() -> None:
    """A book with no envelopes cannot be filtered by band partition; the
    fit counts all of it and records that the filter did not apply."""
    conn = _book([("P0", 0.5, 0.15, 1) for _ in range(20)])
    fit = store.fit_lambda_table(conn, stage_scope="P0")
    assert fit["band_filter"]["applied"] is False
    assert fit["evidence"][0]["seats"] == 20


def test_only_seats_banded_under_the_current_partition_are_fitted() -> None:
    """Lambda is fitted on the served claim, and the served claim is what the
    band table made of it (user ruling, 2026-09-05).  A seat banded by the
    retired five-cell table, or never banded, is a residual of arithmetic
    nobody serves and is left out -- and counted, so the exclusion is
    visible rather than silent."""
    conn = sqlite3.connect(":memory:")
    _candidate_table(conn)
    conn.execute(
        "CREATE TABLE paper_trade_settlement("
        "settlement_id TEXT PRIMARY KEY, position_id TEXT, stage TEXT,"
        " event_date TEXT, hit INTEGER)"
    )
    conn.execute(
        "CREATE TABLE calibration_band_reliability_table("
        "band_table_sha256 TEXT PRIMARY KEY, factors_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history("
        "history_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, checkpoint_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "envelope_id TEXT PRIMARY KEY, lineage_json TEXT)"
    )
    eleven = json.dumps([[[i * 0.05, i * 0.05 + 0.05], 1.0] for i in range(11)])
    five = json.dumps([[[0, 0.02], 1.0]] * 5)
    conn.execute(
        "INSERT INTO calibration_band_reliability_table VALUES('new', ?)",
        (eleven,),
    )
    conn.execute(
        "INSERT INTO calibration_band_reliability_table VALUES('old', ?)",
        (five,),
    )
    rows = []
    for index in range(36):
        kind = ("new", "old", None)[index % 3]
        pid, hid, eid = f"pos-{index}", f"hist-{index}", f"env-{index}"
        rows.append(kind)
        _candidate(
            conn, index, "P0", "2026-09-01", 0.5, 0.15, 1, station="ZUUU",
        )
        conn.execute(
            "UPDATE paper_trade_candidate_window SET decision_at_utc=?"
            " WHERE candidate_id=?", (f"t{index}", f"cand-{index}"),
        )
        conn.execute(
            "INSERT INTO paper_trade_settlement VALUES(?,?,?,?,?)",
            (f"set-{index}", pid, "P0", "2026-09-01", 1),
        )
        conn.execute(
            "INSERT INTO basket_probability_bridge_history VALUES(?,?,?,?,?,?)",
            (hid, "ZUUU", "2026-09-01", "P0", f"t{index}", eid),
        )
        receipt = (
            json.dumps({"band_reliability_json": json.dumps(
                {"band_table_sha256": kind, "state": "APPLIED"}
            )}) if kind else json.dumps({})
        )
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES(?,?)",
            (eid, receipt),
        )
    fit = store.fit_lambda_table(conn, stage_scope="P0")
    assert fit["band_filter"] == {
        "cells": store.BAND_PARTITION_CELLS,
        "applied": True,
        "excluded_old_partition": 12,
        "excluded_no_receipt": 12,
    }
    # Only the twelve current-partition seats were counted.
    assert fit["evidence"][0]["seats"] == 12
    assert fit["table"]["band_partition_cells"] == store.BAND_PARTITION_CELLS


# ---------------------------------------------------------------------------
# Charter 68(c), 2026-09-05: the rolling fit promotes itself.
# ---------------------------------------------------------------------------


def test_the_dispatch_reads_the_served_tables_and_falls_back_per_scope() -> None:
    """One unpacking for both lanes: a scope's table when it has one, the
    kernel constant when it does not."""

    from bayes_basket_target.basket_kernel.delta_v2 import (
        effective_edge_lambda_from_tables,
    )

    served = {
        "P0": ((0.5, 0.3), (0.7, 0.4), (0.85, 0.5), (float("inf"), 0.6)),
        "P0-M": None,
        "P1": ((float("inf"), 0.9),),
    }
    assert effective_edge_lambda_from_tables("P0", 0.3, served) == 0.3
    assert effective_edge_lambda_from_tables("P0", 0.9, served) == 0.6
    assert effective_edge_lambda_from_tables("P1", 0.3, served) == 0.9
    # P0-M has no table yet: the kernel constant, which equals P0's.
    assert effective_edge_lambda_from_tables("P0-M", 0.3, served) == (
        effective_edge_lambda(0.3)
    )
    # No tables at all is the bootstrap for every scope.
    assert effective_edge_lambda_from_tables("P0", 0.3, None) == (
        effective_edge_lambda(0.3)
    )


def _store_with_journal() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    return conn


def _journal(conn, *, scope, sha, fitted_at, lambdas) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO calibration_effective_edge_lambda("
        " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
        " window_days, seat_count, activated, activated_at_utc, stage_scope)"
        " VALUES(?,?,?,?,28,10,0,?,?)",
        (
            sha,
            json.dumps({"bands": [
                {"upper_bound": b, "lambda": l}
                for b, l in zip((0.5, 0.7, 0.85, None), lambdas)
            ]}),
            "[]", fitted_at, fitted_at, scope,
        ),
    )
    conn.execute(
        "INSERT INTO calibration_effective_edge_fit_journal("
        " lambda_table_sha256, fitted_at_utc, admitted, accumulating_bands,"
        " evidence_json, stage_scope) VALUES(?,?,4,'[]','[]',?)",
        (sha, fitted_at, scope),
    )


def test_as_of_reads_the_journal_so_a_reactivated_sha_keeps_its_history() -> None:
    """The table's activated_at is bumped every time the same sha is
    re-activated; the journal keeps the sequence.  A decision on the 2nd
    must read the 1st's table, and one before any fit reads nothing."""

    conn = _store_with_journal()
    _journal(conn, scope="P0", sha="a" * 64, fitted_at="2026-09-01T01:00:00",
             lambdas=(0.1, 0.2, 0.3, 0.4))
    _journal(conn, scope="P0", sha="b" * 64, fitted_at="2026-09-03T01:00:00",
             lambdas=(0.5, 0.6, 0.7, 0.8))
    # The same first sha re-journaled later, as a re-activation would be.
    _journal(conn, scope="P0", sha="a" * 64, fitted_at="2026-09-05T01:00:00",
             lambdas=(0.1, 0.2, 0.3, 0.4))
    conn.execute(
        "UPDATE calibration_effective_edge_lambda SET activated=1,"
        " activated_at_utc='2026-09-05T01:00:00'"
        " WHERE lambda_table_sha256=?", ("a" * 64,),
    )
    as_of = lambda t: store.load_lambda_as_of(conn, stage_scope="P0", as_of_utc=t)
    assert as_of("2026-08-31T00:00:00") is None
    assert [l for _, l in as_of("2026-09-02T00:00:00")] == [0.1, 0.2, 0.3, 0.4]
    assert [l for _, l in as_of("2026-09-04T00:00:00")] == [0.5, 0.6, 0.7, 0.8]
    assert [l for _, l in as_of("2026-09-06T00:00:00")] == [0.1, 0.2, 0.3, 0.4]
    # The mapping the dispatch reads, per scope; P1 has no fit here.
    #
    # Four keys since 2026-09-09, and the fourth is deliberately not a stage:
    # the bucket lane fits one table across P0 and P0-M -- what the kernel's
    # own per-rung dispatch reads -- so it travels beside the per-stage window
    # tables rather than inside them.  A reader iterating stages cannot pick
    # it up by accident, and one that wants it has to name it.
    tables = store.load_effective_edge_tables(conn, as_of_utc="2026-09-04T00:00:00")
    assert set(tables) == {"P0", "P0-M", "P1", store.BUCKET_TABLE_KEY}
    # No bucket fit in this fixture, so the dispatch falls to the kernel
    # constant exactly as a stage without a window fit does.
    assert tables[store.BUCKET_TABLE_KEY] is None
    assert tables["P1"] is None and tables["P0-M"] is None


def test_a_thin_band_keeps_the_last_activated_value_not_the_constant() -> None:
    """Self-promotion has to be sticky: a band that cleared the hurdle
    yesterday and is thin today serves yesterday's number, not the
    hand-fitted bootstrap it was promoted away from."""

    conn = _store_with_journal()
    _candidate_table(conn)
    conn.execute(
        "CREATE TABLE paper_trade_settlement("
        "settlement_id TEXT PRIMARY KEY, position_id TEXT, stage TEXT,"
        " event_date TEXT, hit INTEGER)"
    )
    # An activated table whose first band was promoted to 0.33.
    _journal(conn, scope="P0", sha="c" * 64, fitted_at="2026-09-01T01:00:00",
             lambdas=(0.33, 0.6, 0.87, 0.82))
    conn.execute(
        "UPDATE calibration_effective_edge_lambda SET activated=1"
        " WHERE lambda_table_sha256=?", ("c" * 64,),
    )
    # Three seats in the first band today: below the hurdle.
    for index in range(3):
        _candidate(conn, index, "P0", "2026-09-02", 0.5, 0.15, 1)
        conn.execute("INSERT INTO paper_trade_settlement VALUES(?,?,?,?,?)",
                     (f"s{index}", f"p{index}", "P0", "2026-09-02", 1))
    fit = store.fit_lambda_table(conn, stage_scope="P0")
    assert fit["evidence"][0]["accumulating"] is True
    assert fit["bands"][0][1] == 0.33
    assert fit["evidence"][0]["incumbent_lambda"] == 0.33
    # And before any activation the bootstrap is the constant.
    fresh = _store_with_journal()
    assert store._incumbent_bands(fresh, "P0") == [
        (float(b), float(l)) for b, l in EFFECTIVE_EDGE_LAMBDA
    ]
