"""The bucket-scope lambda lane: a fit for the table that never had one.

Charter 68's per-rung arm read four constants hand-set on 2026-09-03 while
the per-window arm read a nightly refit, so the two were never comparable in
the way "same rule, different scope" implies.  Measured 2026-09-09 the arm
lost 6.6 points, and 68% of the rungs in the seats it wrongly opened sat in
its un-split open top band -- the same pathology charter 96 had already found
and split on the window table.

This pins the lane: a row is a RUNG, the estimator and the hurdles are the
window lane's unchanged, the two tables cannot retire each other, and the
split top band can now fit its sub-bands apart.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target import effective_edge_store as store
from bayes_basket_target.basket_kernel.delta_v2 import (
    EFFECTIVE_EDGE_BUCKET_LAMBDA,
    effective_edge_bucket_lambda_for_stage,
)

# Charter 140: these fixtures build the paper candidate table, and the estimator
# under test does not depend on the source; see ``ledger_evidence_source`` in
# conftest.
pytestmark = pytest.mark.usefixtures("ledger_evidence_source")


def _book() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_trade_candidate_window("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, buckets_json TEXT,"
        " win_probability REAL, all_in_price REAL, hit INTEGER,"
        " settled_bucket TEXT, leg_claim_cost_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE paper_trade_settlement("
        " settlement_id TEXT PRIMARY KEY, event_date TEXT)"
    )
    conn.execute(
        "INSERT INTO paper_trade_settlement VALUES('s','2026-09-08')"
    )
    store.ensure_schema(conn)
    return conn


def _window(conn, *, index, station, day, legs, settled, stage="P0"):
    """One enumerated window, carrying its rungs' own claims and asks."""

    conn.execute(
        "INSERT INTO paper_trade_candidate_window VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"cand-{index}", station, day, stage, f"{day}T00:00:00Z",
            json.dumps(sorted(legs)),
            sum(c for c, _p in legs.values()),
            sum(p for _c, p in legs.values()),
            1 if settled in legs else 0,
            settled,
            json.dumps({b: [c, p] for b, (c, p) in legs.items()}),
        ),
    )


def _fit(conn, **kwargs):
    # The nightly form: no ``as_of``, so the window ends at the newest
    # settlement (2026-09-08 here).  An explicit ``as_of`` has meant a
    # walk-forward question since charter 132 and needs label times this
    # book does not carry -- which is not what these tests pin.
    return store.fit_lambda_table(
        conn, window_days=30, stage_scope="P0", **kwargs
    )


def test_a_row_is_a_rung_and_a_rung_is_counted_once() -> None:
    conn = _book()
    # The same three rungs, drawn as three overlapping windows at one
    # decision.  A fit that counted per window would weight each rung by how
    # many windows the enumeration happened to draw around it.
    legs = {"a": (0.30, 0.24), "b": (0.25, 0.22), "c": (0.10, 0.09)}
    for index, span in enumerate((("a", "b"), ("b", "c"), ("a", "b", "c"))):
        _window(
            conn, index=index, station="S1", day="2026-09-01",
            legs={k: legs[k] for k in span}, settled="b",
        )

    fit = _fit(conn, claim_scope="bucket")
    seats = sum(band["seats"] for band in fit["evidence"])
    assert seats == 3, "three rungs at one decision, not seven leg-in-window rows"
    # And the claim mass is each rung's own claim, counted once.
    assert sum(band["claim_mass"] for band in fit["evidence"]) == pytest.approx(
        0.65
    )
    assert sum(band["hits"] for band in fit["evidence"]) == 1


def test_the_estimator_is_the_window_lanes_unchanged() -> None:
    conn = _book()
    # Fourteen station-days over eight event days so the hurdles clear, every
    # rung at agreement 0.9 -- inside the band the split created at
    # (0.85, 0.95].
    for index in range(14):
        _window(
            conn, index=index, station=f"S{index}",
            day=f"2026-09-{index % 8 + 1:02d}",
            legs={"a": (0.40, 0.36), "b": (0.40, 0.36)},
            settled="a" if index < 7 else "z",
        )

    fit = _fit(conn, claim_scope="bucket")
    band = next(
        b for b in fit["evidence"] if b["band_upper_bound"] == 0.95
    )
    # hits / claim mass, which is what the window lane computes on seats.
    assert band["seats"] == 28
    assert band["claim_mass"] == pytest.approx(11.2)
    assert band["hits"] == 7
    assert band["raw_lambda"] == pytest.approx(7 / 11.2, rel=1e-6)
    assert band["served_lambda"] == pytest.approx(7 / 11.2, rel=1e-6)


def test_the_split_top_band_fits_its_pieces_apart() -> None:
    # The whole point of charter 96's split, applied to the bucket table: an
    # open band cannot be told apart from its own tail.  Two populations that
    # would have pooled into one number now land in two.
    bounds = [b for b, _lam in EFFECTIVE_EDGE_BUCKET_LAMBDA]
    assert 0.95 in bounds and 1.00 in bounds and 1.10 in bounds
    # Splitting alone moves nothing: every new band inherits the value of the
    # old band that contained it.
    assert {
        lam for bound, lam in EFFECTIVE_EDGE_BUCKET_LAMBDA if bound > 0.85
    } == {1.38}

    conn = _book()
    for index in range(14):
        # a = 0.9, lands in (0.85, 0.95]
        _window(
            conn, index=index, station=f"S{index}", day="2026-09-01",
            legs={"a": (0.40, 0.36)}, settled="a" if index < 4 else "z",
        )
    for index in range(14, 28):
        # a = 1.05, lands in (1.00, 1.10]
        _window(
            conn, index=index, station=f"S{index}", day="2026-09-02",
            legs={"b": (0.40, 0.42)}, settled="b" if index < 26 else "z",
        )

    fit = _fit(conn, claim_scope="bucket")
    low = next(b for b in fit["evidence"] if b["band_upper_bound"] == 0.95)
    high = next(b for b in fit["evidence"] if b["band_upper_bound"] == 1.10)
    assert low["raw_lambda"] is not None and high["raw_lambda"] is not None
    assert low["raw_lambda"] != pytest.approx(high["raw_lambda"])


def test_the_two_lanes_do_not_retire_each_other() -> None:
    conn = _book()
    for index in range(14):
        _window(
            conn, index=index, station=f"S{index}", day="2026-09-01",
            legs={"a": (0.40, 0.36), "b": (0.40, 0.36)},
            settled="a" if index < 7 else "z",
        )

    # A window table already serving.  Written directly rather than through
    # a fit, because whether THIS fixture's window fit would clear the
    # activation guard is a different question and not this test's.
    conn.execute(
        "INSERT INTO calibration_effective_edge_lambda("
        " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
        " window_days, seat_count, activated, activated_at_utc, stage_scope,"
        " claim_scope) VALUES(?,?,?,?,?,?,1,?,?,?)",
        (
            "w" * 64,
            json.dumps({"bands": [
                {"upper_bound": b if b != float("inf") else None,
                 "lambda": lam}
                for b, lam in ((0.5, 0.11), (None, 0.22))
            ]}),
            "{}", "2026-09-08T00:00:00", 30, 99, "2026-09-08T00:00:00",
            "P0", "window",
        ),
    )
    bucket_fit = _fit(conn, claim_scope="bucket")
    receipt = store.persist_and_activate(conn, bucket_fit)

    # The window table is untouched: activating -- or refusing to activate --
    # a bucket table stood nothing down.  That is the property that lets the
    # two lanes coexist, and it holds whichever way the guard rules, which is
    # why this reads the rows rather than the guard's verdict.
    assert store.load_active_lambda(conn, stage_scope="P0") is not None
    rows = dict(conn.execute(
        "SELECT COALESCE(claim_scope,'window'), COUNT(*)"
        " FROM calibration_effective_edge_lambda WHERE activated=1"
        " GROUP BY 1"
    ).fetchall())
    assert rows.get("window") == 1
    # The bucket fit is stored under its own scope either way; whether it is
    # SERVING is the guard's business and this fixture is too small to earn
    # it, which is the guard working rather than a defect.
    stored = conn.execute(
        "SELECT COUNT(*) FROM calibration_effective_edge_lambda"
        " WHERE COALESCE(claim_scope,'window')='bucket'"
    ).fetchone()[0]
    assert stored == 1
    assert receipt is not None

    # The dispatch reads the fitted table when one is serving and the kernel
    # constant when none is -- the same relationship the window path has.
    tables = store.load_effective_edge_tables(conn)
    assert store.BUCKET_TABLE_KEY in tables
    served_bucket = store.load_active_lambda(
        conn, stage_scope="P0", claim_scope="bucket"
    )
    assert tables[store.BUCKET_TABLE_KEY] == served_bucket
    if served_bucket is None:
        assert effective_edge_bucket_lambda_for_stage(
            "P0", 0.9, bucket_table=None
        ) == pytest.approx(
            effective_edge_bucket_lambda_for_stage("P0", 0.9)
        )
    else:
        assert len(served_bucket) == len(EFFECTIVE_EDGE_BUCKET_LAMBDA)


def _served_row(conn, *, sha, claim_scope, fitted_at, vintage):
    conn.execute(
        "INSERT INTO calibration_effective_edge_lambda("
        " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
        " window_days, seat_count, activated, activated_at_utc, stage_scope,"
        " claim_scope) VALUES(?,?,?,?,?,?,1,?,?,?)",
        (
            sha,
            json.dumps({
                "bands": [{"upper_bound": None, "lambda": 0.5}],
                "claim_transform_sha256": vintage,
            }),
            "{}", fitted_at, 30, 1, fitted_at, "P0", claim_scope,
        ),
    )
    conn.execute(
        "INSERT INTO calibration_effective_edge_serving_interval("
        " stage_scope, lambda_table_sha256, served_from_utc, served_until_utc)"
        " VALUES('P0',?,?,NULL)",
        (sha, fitted_at),
    )


def test_the_served_vintage_is_read_from_its_own_claim_scope() -> None:
    """2026-09-14: P0's window fit read the bucket table's vintage.

    The bucket row goes in first, so a lookup that neither filters on the
    scope nor orders would return it -- which is what production did, and
    why the window table fell back to the kernel constant every night.
    """

    conn = _book()
    _served_row(conn, sha="b" * 64, claim_scope="bucket",
                fitted_at="2026-09-11T00:17:45+00:00", vintage=["old", "old"])
    _served_row(conn, sha="w" * 64, claim_scope="window",
                fitted_at="2026-09-13T08:37:04+00:00", vintage=["new", "new"])
    assert store._active_claim_vintage(conn, "P0") == ["new", "new"]
    assert store._active_claim_vintage(conn, "P0", "bucket") == ["old", "old"]


def test_one_scope_never_closes_the_other_scopes_serving_interval(monkeypatch) -> None:
    conn = _book()
    _served_row(conn, sha="w" * 64, claim_scope="window",
                fitted_at="2026-09-08T00:00:00+00:00", vintage=None)
    legs = {"a": (0.30, 0.24), "b": (0.25, 0.22)}
    for day in range(1, 8):
        _window(conn, index=day, station=f"S{day}", day=f"2026-09-0{day}",
                legs=legs, settled="a")
    # Activate the bucket table whatever the fixture's returns say: the
    # property is about the interval book, not about earning the seat.
    monkeypatch.setattr(store, "ACTIVATION_RETURN_GUARD", "off")
    store.persist_and_activate(conn, _fit(conn, claim_scope="bucket"))

    def open_intervals(scope):
        return [
            row[0] for row in conn.execute(
                "SELECT COALESCE(t.claim_scope,'window')"
                " FROM calibration_effective_edge_serving_interval i"
                " JOIN calibration_effective_edge_lambda t"
                "   ON t.lambda_table_sha256=i.lambda_table_sha256"
                " WHERE i.stage_scope='P0' AND i.served_until_utc IS NULL"
            ) if row[0] == scope
        ]

    assert open_intervals("window") == ["window"]
    assert open_intervals("bucket") == ["bucket"]

    from bayes_basket_target.forward_serving_score import forward_serving_score

    window_score = forward_serving_score(conn, stage_scope="P0")
    bucket_score = forward_serving_score(conn, stage_scope="P0", claim_scope="bucket")
    assert [i["lambda_table_sha256"] for i in window_score["intervals"]] == ["w" * 64]
    assert window_score["intervals"][0]["still_serving"] is True
    assert len(bucket_score["intervals"]) == 1
    assert bucket_score["intervals"][0]["lambda_table_sha256"] != "w" * 64

    # Retiring the bucket table leaves the window table serving, too.
    store.retire_lambda_table(conn, stage_scope="P0", claim_scope="bucket")
    assert open_intervals("window") == ["window"]
    assert open_intervals("bucket") == []
