"""Lambda is fitted on one band vintage at a time.

The band table changes every claim, and the claim is the denominator of
``a = price / claim`` -- the axis lambda itself is keyed on.  Measured
2026-09-08 on 757 live candidate windows, moving P0 to the distance axis
shifted ``a`` on 100% of them and carried 6.1% across a band boundary into
a different lambda value.  A fit pooling both vintages fits one curve to two
different x axes.

The partition key is the AXIS, not the table.  Band tables are content
addressed and refitted daily, so the sha moves whenever a factor shifts in
its third decimal: P0's 662 scored windows carry 28 distinct shas, the
largest holding 61 -- fewer than the eight lambda bands need at twelve seats
each -- and P1's 77 carry 21 shas of which exactly one clears the threshold.
Partitioning on the sha would not make the fit purer, it would end it.  The
sha is kept for audit so drift inside an axis stays visible.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import paper_trading_ledger as ledger  # noqa: E402
from bayes_basket_target.effective_edge_store import _fit_axis  # noqa: E402


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ledger.ensure_schema(conn)
    return conn


def _candidate(conn, cid, *, axis=None, sha=None, stage="P0", bucket="30°C"):
    conn.execute(
        "INSERT INTO paper_trade_candidate_window("
        " candidate_id, station_id, event_date, stage, decision_at_utc,"
        " buckets_json, window_size, win_probability, all_in_price,"
        " admissible, seated, band_axis, band_table_sha256, hit)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, "ZSQD", "2026-09-05", stage, "2026-09-05T00:00:00Z",
         json.dumps([bucket]), 1, 0.4, 0.3, 1, 0, axis, sha, 1),
    )


def test_the_table_carries_the_partition_key_and_the_audit_column():
    conn = _db()
    columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(paper_trade_candidate_window)"
        )
    }
    assert {"band_axis", "band_table_sha256"} <= columns


def test_an_existing_table_is_widened_rather_than_left_behind():
    """CREATE TABLE IF NOT EXISTS will not add a column, and every row
    already recorded predates both of these."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_trade_candidate_window("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT NOT NULL,"
        " event_date TEXT NOT NULL,"
        " stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),"
        " decision_at_utc TEXT NOT NULL, buckets_json TEXT NOT NULL,"
        " window_size INTEGER NOT NULL, win_probability REAL NOT NULL,"
        " all_in_price REAL NOT NULL)"
    )
    ledger.ensure_schema(conn)
    columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(paper_trade_candidate_window)"
        )
    }
    assert {"band_axis", "band_table_sha256"} <= columns


def test_the_fit_reads_one_axis_and_counts_what_it_left_out():
    """Both halves matter.  Excluding the other vintage is the point; saying
    how much was excluded is what stops a halved sample looking like a
    sample that was always that size."""

    import inspect

    from bayes_basket_target import effective_edge_store as store

    source = inspect.getsource(store.fit_lambda_table)
    assert "band_axis IS ?" in source
    assert "band_axis IS NOT ?" in source
    assert '"excluded_other_band_axis"' in source
    assert '"band_axis": axis' in source


def test_an_unknown_vintage_is_excluded_rather_than_assumed_current():
    """Charter 104 forbids silently borrowing a basis for a claim; a
    partition key gets the same rule.  ``IS ?`` rather than ``= ?`` so the
    NULL case is explicit either way."""

    conn = _db()
    # Different buckets: the row's identity is (station, date, stage,
    # decision, buckets), so two rows on one decision have to differ there.
    _candidate(conn, "known", axis="claim", sha="a" * 64, bucket="30°C")
    _candidate(conn, "unknown", bucket="31°C")
    conn.commit()
    got = conn.execute(
        "SELECT COUNT(*) FROM paper_trade_candidate_window"
        " WHERE hit IS NOT NULL AND band_axis IS ?", ("claim",),
    ).fetchone()[0]
    left_out = conn.execute(
        "SELECT COUNT(*) FROM paper_trade_candidate_window"
        " WHERE hit IS NOT NULL AND band_axis IS NOT ?", ("claim",),
    ).fetchone()[0]
    assert (got, left_out) == (1, 1)


def test_the_axis_comes_from_what_the_stages_serve_not_from_the_caller():
    from bayes_basket_target.band_reliability_store import (
        ensure_band_table_schema,
        persist_and_activate_band_table,
    )

    conn = sqlite3.connect(":memory:")
    ensure_band_table_schema(conn)
    # No table at all is not a vintage: partitioning on the store's
    # historical ``claim`` default would exclude every row in a fixture or a
    # replay extract that never had band lineage to record.
    assert _fit_axis(conn, ("P0",)) is None, "no table, no partition"
    persist_and_activate_band_table(
        conn, stage="P0", factors=[((0.0, 1.0), 1.1)],
        evidence={}, serving_day_count=20, axis="centre_distance",
    )
    assert _fit_axis(conn, ("P0",)) == "centre_distance"
    # Stages on two different axes cannot be pooled into one fit, and the
    # honest answer is no axis rather than one of them.
    persist_and_activate_band_table(
        conn, stage="P1", factors=[((0.0, 0.5), 0.9)],
        evidence={}, serving_day_count=20, axis="claim",
    )
    assert _fit_axis(conn, ("P0", "P1")) is None


def test_the_backfill_resolves_a_vintage_through_the_store_not_a_guess():
    """The receipt only started carrying ``axis`` on 2026-09-07, so every
    historical row resolves by sha through the band store.  A sha the store
    no longer holds stays NULL."""

    import inspect

    source = inspect.getsource(ledger._backfill_candidate_band_lineage)
    assert "calibration_band_reliability_table" in source
    assert "COALESCE(axis,'claim')" in source
    assert '"sha_not_in_store"' in source
    assert '"no_band_receipt"' in source
    # Called where its sibling is, so a migration cannot add the column
    # without filling it.
    schema = inspect.getsource(ledger.ensure_schema)
    assert "_backfill_candidate_band_lineage(conn)" in schema
    # And it runs inside that schema step, so a ledger-only database -- a
    # replay extract, a fixture -- must not be failed by it.  Both sources
    # absent means no vintage to resolve, not an error.
    assert "band_lineage_sources_absent" in source
    conn = sqlite3.connect(":memory:")
    ledger.ensure_schema(conn)
    assert ledger._backfill_candidate_band_lineage(conn)["skipped"] == (
        "band_lineage_sources_absent"
    )


# --- Charter 110 extended (2026-09-08): the claim transform is a second key --


def test_the_two_claim_paths_carry_different_fingerprints() -> None:
    """A centre-holding window and an off-centre one are not one vintage.

    They go through different tables -- charter 81's lift or the off-centre
    deflation -- so one fingerprint over the union would retire a row for a
    change to a table it never read.
    """
    from bayes_basket_target.basket_kernel.delta_v2 import (
        claim_transform_fingerprint,
    )

    assert claim_transform_fingerprint(
        holds_centre=True
    ) != claim_transform_fingerprint(holds_centre=False)


def test_moving_the_off_centre_table_leaves_centre_holding_rows_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason the key is split, stated as a test.

    Measured 2026-09-08: all 1,493 candidate windows in the book verify
    against the centre-holding path and none against the off-centre one --
    charter 49's ladder floor has left the fit no off-centre rows at all.  A
    union key would therefore have taken the whole lambda sample to zero to
    record a change to a table none of those rows read.
    """
    import bayes_basket_target.basket_kernel.delta_v2 as kernel

    before_centre = kernel.claim_transform_fingerprint(holds_centre=True)
    before_off = kernel.claim_transform_fingerprint(holds_centre=False)
    monkeypatch.setattr(
        kernel,
        "OFF_CENTER_CLAIM_BANDS",
        ((0.05, 1.15), (0.10, 0.85), (0.20, 0.88), (1.01, 0.5)),
    )
    assert kernel.claim_transform_fingerprint(holds_centre=True) == before_centre
    assert kernel.claim_transform_fingerprint(holds_centre=False) != before_off


def test_moving_charter_81_retires_the_centre_holding_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And it still fails closed where it must: 1,321 of those rows read
    charter 81's lift, and moving it really does change their claims."""
    import bayes_basket_target.basket_kernel.delta_v2 as kernel

    before = kernel.claim_transform_fingerprint(holds_centre=True)
    monkeypatch.setattr(
        kernel, "CENTER_WINDOW_CLAIM_BANDS", ((0.35, 1.0), (1.01, 1.5)),
    )
    assert kernel.claim_transform_fingerprint(holds_centre=True) != before


def test_the_fit_excludes_rows_from_another_claim_vintage() -> None:
    """A row whose claim came from other tables is not evidence about a."""
    from bayes_basket_target.basket_kernel.delta_v2 import (
        claim_transform_fingerprint,
    )
    from bayes_basket_target.effective_edge_store import fit_lambda_table

    conn = _db()
    current = claim_transform_fingerprint(holds_centre=True)
    for index in range(6):
        _candidate(
            conn, f"cand-current-{index}", bucket=f"3{index}°C",
        )
        conn.execute(
            "UPDATE paper_trade_candidate_window"
            " SET claim_transform_sha256=? WHERE candidate_id=?",
            (current, f"cand-current-{index}"),
        )
    for index in range(6):
        _candidate(
            conn, f"cand-stale-{index}", bucket=f"4{index}°C",
        )
        conn.execute(
            "UPDATE paper_trade_candidate_window"
            " SET claim_transform_sha256=? WHERE candidate_id=?",
            ("0" * 64, f"cand-stale-{index}"),
        )
    conn.execute(
        "INSERT INTO paper_trade_settlement("
        " settlement_id, position_id, station_id, event_date, stage,"
        " settled_tmax_c, settled_bucket, hit, stake_usdc, payout_usdc,"
        " pnl_usdc, claimed_win_probability, settled_at_utc)"
        " VALUES('s1','p1','ZSQD','2026-09-05','P0',30.0,'30°C',1,"
        " 1.0,2.0,1.0,0.5,'2026-09-05T12:00:00Z')"
    )
    fit = fit_lambda_table(conn, stage_scope="P0")
    assert fit["claim_transform_sha256"] is not None
    assert fit["excluded_other_claim_transform"] == 6
    conn.close()
