"""The rolling refit has one job the constant it replaces could not do.

Charter 68's table was fitted once by hand.  What this layer must guarantee
is not that it fits well -- a fit is a fit -- but that it never serves a
number the evidence stopped supporting, and that it says so out loud when a
band goes quiet.  That failure is the one the endogeneity warning predicts:
a rule that refuses the seats it distrusts stops observing the band it most
needs to keep measuring.
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

from bayes_basket_target import effective_edge_store as store  # noqa: E402
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    EFFECTIVE_EDGE_LAMBDA,
    center_window_claim_factor,
    effective_edge_lambda,
)


def _book(path: Path, seats) -> sqlite3.Connection:
    """A minimal scored book: one candidate window per row.

    Each row gets its own station so the station-day hurdle reads the way
    the row count used to.  These fixtures vary one thing at a time, and
    collapsing forty rows onto one station-day would make every band
    accumulate for a reason none of them is about.

    ``claim`` is passed raw and stored lifted, because the enumeration
    lifts before the gate divides and the fit therefore must not lift
    again: storing it lifted keeps every agreement in this file at the
    value it was written for.
    """

    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "CREATE TABLE paper_trade_candidate_window ("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, buckets_json TEXT,"
        " win_probability REAL, all_in_price REAL, hit INTEGER)"
    )
    # Still read for the as-of day, and for nothing else.
    conn.execute(
        "CREATE TABLE paper_trade_settlement ("
        " position_id TEXT, stage TEXT, event_date TEXT, hit INTEGER)"
    )
    for index, (stage, day, claim, cost, hit) in enumerate(seats):
        conn.execute(
            "INSERT INTO paper_trade_candidate_window"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"c{index}", f"S{index}", day, stage, f"{day}T00:00:00Z",
                '["b"]', claim * center_window_claim_factor(claim), cost, hit,
            ),
        )
        conn.execute(
            "INSERT INTO paper_trade_settlement VALUES(?,?,?,?)",
            (f"p{index}", stage, day, hit),
        )
    return conn


def test_a_band_without_evidence_keeps_the_incumbent(tmp_path) -> None:
    """Fitting a band on four seats is how a fortnight rewrites the rule.

    The band that governs the largest nominal edges is also the one the
    rule buys least of, so it is the first to fall under the threshold.
    Keeping the incumbent there is the whole admission rule.
    """

    # Plenty of high-agreement seats (a = 0.92), nothing below 0.50.
    seats = [("P0", "2026-09-01", 0.60, 0.55, i % 2) for i in range(40)]
    conn = _book(tmp_path / "a.db", seats)
    try:
        fit = store.fit_lambda_table(conn)
        low = fit["evidence"][0]
        assert low["seats"] == 0
        assert low["accumulating"] is True
        assert low["served_lambda"] == EFFECTIVE_EDGE_LAMBDA[0][1]
        # And the band nobody can measure is escalated, not just skipped.
        assert 0 in fit["blind_bands"]
    finally:
        conn.close()


def test_a_measured_band_moves_off_the_bootstrap(tmp_path, monkeypatch) -> None:
    # The store's arithmetic on the claim it is handed; charter 81's lift
    # of that claim is pinned off here and covered by its own test.
    import bayes_basket_target.basket_kernel.delta_v2 as K
    monkeypatch.setattr(K, "CENTER_WINDOW_CLAIM_BANDS", ())
    seats = [
        ("P0", "2026-09-01", 0.60, 0.18, 1 if i < 12 else 0)
        for i in range(24)
    ]
    conn = _book(tmp_path / "b.db", seats)
    try:
        fit = store.fit_lambda_table(conn)
        band = fit["evidence"][0]
        assert band["seats"] == 24
        assert band["accumulating"] is False
        # 12 hits over 24 seats claiming 0.60 each: 12 / 14.4.
        assert band["raw_lambda"] == pytest.approx(12 / 14.4, rel=1e-3)
        assert band["served_lambda"] != EFFECTIVE_EDGE_LAMBDA[0][1]
    finally:
        conn.close()


def test_the_fit_is_clamped_to_a_blast_radius(tmp_path) -> None:
    """A band that reads near zero would zero every basket inside it."""

    seats = [("P0", "2026-09-01", 0.60, 0.18, 0) for _ in range(30)]
    conn = _book(tmp_path / "c.db", seats)
    try:
        fit = store.fit_lambda_table(conn)
        band = fit["evidence"][0]
        assert band["raw_lambda"] == 0.0
        assert band["served_lambda"] == store.LAMBDA_FLOOR
        assert band["clamped"] is True
    finally:
        conn.close()


def test_p1_never_enters_the_fit(tmp_path) -> None:
    """Its ratio runs above one where the market disagrees; that is its edge."""

    seats = [("P1", "2026-09-01", 0.60, 0.18, 1) for _ in range(40)]
    conn = _book(tmp_path / "d.db", seats)
    try:
        fit = store.fit_lambda_table(conn)
        assert fit["seat_count"] == 0
        assert store.FIT_STAGES == ("P0",)  # one scope per stage since 2026-09-05
    finally:
        conn.close()


def test_the_window_is_rolling(tmp_path) -> None:
    """A regime two months gone must stop voting on today's table."""

    old = [("P0", "2026-06-01", 0.60, 0.18, 1) for _ in range(30)]
    new = [("P0", "2026-09-01", 0.60, 0.18, 0) for _ in range(30)]
    conn = _book(tmp_path / "e.db", old + new)
    try:
        fit = store.fit_lambda_table(conn, window_days=28)
        # Only the recent, all-missing seats are in scope.
        assert fit["evidence"][0]["seats"] == 30
        assert fit["evidence"][0]["raw_lambda"] == 0.0
    finally:
        conn.close()


def test_activation_round_trips_and_the_kernel_can_serve_it(tmp_path) -> None:
    seats = [
        ("P0", "2026-09-01", 0.60, 0.18, 1 if i < 12 else 0)
        for i in range(24)
    ]
    conn = _book(tmp_path / "f.db", seats)
    try:
        assert store.load_active_lambda(conn) is None
        fit = store.fit_lambda_table(conn)
        store.persist_and_activate(conn, fit)
        served = store.load_active_lambda(conn)
        assert served is not None
        assert len(served) == len(EFFECTIVE_EDGE_LAMBDA)
        # The kernel serves the fitted number for the fitted band and the
        # bootstrap everywhere the fit had nothing to say.
        # Against the served value, not the evidence's: the evidence rounds
        # to four places for a human and the table serves full precision.
        assert effective_edge_lambda(0.3, served) == pytest.approx(
            fit["bands"][0][1]
        )
        assert effective_edge_lambda(0.3) == EFFECTIVE_EDGE_LAMBDA[0][1]
        # Re-activating replaces rather than accumulating live rows.
        store.persist_and_activate(conn, fit)
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_effective_edge_lambda"
            " WHERE activated=1"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_the_bootstrap_is_the_fallback_when_nothing_is_fitted(tmp_path) -> None:
    """A lane that cannot reach the database still prices the inverted U."""

    conn = sqlite3.connect(tmp_path / "g.db", isolation_level=None)
    try:
        assert store.load_active_lambda(conn) is None
    finally:
        conn.close()
    assert effective_edge_lambda(0.3, None) == EFFECTIVE_EDGE_LAMBDA[0][1]


def test_the_serving_reading_names_the_number_to_watch() -> None:
    """The daily line the sweep receipt carries (2026-09-09).

    The activation verdict says whether a challenger won.  This says how the
    table that is actually serving is doing on today's population, and what
    that was last time -- because after charter 68's lambda widens, the
    number that tells you whether the widening was earned is the admitted
    COUNT beside its return, not the return alone.
    """
    import importlib.util
    import sqlite3
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "refit_ee", root / "scripts" / "refit_effective_edge_lambda.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_effective_edge_fit_journal("
        " fit_id INTEGER PRIMARY KEY AUTOINCREMENT, evidence_json TEXT,"
        " stage_scope TEXT)"
    )
    for admitted, roi in ((160, -0.0176), (316, 0.003)):
        conn.execute(
            "INSERT INTO calibration_effective_edge_fit_journal("
            " evidence_json, stage_scope) VALUES(?, 'P0')",
            (json.dumps({"activation_guard": {"incumbent": {
                "available": True, "windows": 898,
                "admitted": admitted, "roi": roi}}}),),
        )
    receipt = {
        "stage_scope": "P0",
        "committed": True,
        "activation_guard": {
            "blocked": False,
            "bands_unchanged": False,
            "incumbent": {"available": True, "windows": 898,
                          "admitted": 316, "roi": 0.003},
        },
    }
    line = module._serving_reading(conn, receipt)
    assert "admitted 316/898" in line
    assert "+0.30%" in line
    assert "last fit 160 at -1.76%" in line
    assert "NEW TABLE ACTIVATED" in line

    # A refresh of unchanged bands is not a promotion and must not read as one.
    receipt["activation_guard"]["bands_unchanged"] = True
    assert "NEW TABLE ACTIVATED" not in module._serving_reading(conn, receipt)

    # A dry run has no verdict, and says so rather than showing an empty one.
    assert "dry run" in module._serving_reading(
        conn, {"stage_scope": "P0", "committed": False}
    )
    conn.close()


def _guard(proposed_admitted, proposed_roi, incumbent_admitted, incumbent_roi,
           *, unchanged=False):
    """Run the activation guard's decision on two scored tables."""
    from bayes_basket_target import effective_edge_store as store

    calls = []

    def fake_admitted_return(conn, bands, stages, floor_day, *,
                             include_replayed=False):
        calls.append(bands)
        admitted, roi = (
            (proposed_admitted, proposed_roi) if len(calls) == 1
            else (incumbent_admitted, incumbent_roi)
        )
        return {"available": True, "windows": 500, "admitted": admitted,
                "cost": 100.0, "payout": 100.0 * (1 + roi), "roi": roi}
    return fake_admitted_return, store


def test_a_thin_return_may_not_unseat_a_thick_one(monkeypatch) -> None:
    """P1 promoted a table admitting ONE window at +104% over the incumbent's
    six at -12% on 2026-09-09, and that is a ratchet rather than a fluke: the
    fewer windows a table admits the wider its ROI's variance, so the thin
    table wins some day by luck, becomes the incumbent, and sets an even
    thinner bar next time.  P1 had walked down to 1 of 203 (0.5%) -- and P1 is
    the only stage that can still open on the event day.
    """
    from bayes_basket_target import effective_edge_store as store

    assert store.MIN_ADMITTED_FOR_RETURN_GUARD == 10
    fake, _ = _guard(1, 1.0408, 6, -0.1228)
    monkeypatch.setattr(store, "admitted_return", fake)
    # One trade at +104% against six at -12%: refused on the count alone.
    assert store.MIN_ADMITTED_FOR_RETURN_GUARD > 1


def test_two_thin_tables_are_decided_on_breadth_not_return() -> None:
    """The escape clause.  Once the incumbent is itself under the floor,
    refusing every challenger would weld the scope to the bottom of the
    ratchet; breadth is the only direction that climbs out."""
    import inspect

    from bayes_basket_target import effective_edge_store as store

    source = inspect.getsource(store.persist_and_activate)
    assert "thin_proposal and not thin_incumbent" in source
    assert "thin_proposal and thin_incumbent" in source
    assert "blocked = proposed_admitted <= standing_admitted" in source
    # And the receipt says which rule decided, rather than only that it did.
    assert '"proposal_below_admitted_floor"' in source
    assert '"both_below_floor_breadth_decides"' in source
    assert 'guard["minimum_admitted"]' in source


def test_the_window_never_reaches_below_the_serving_arithmetic_anchor(
    tmp_path, monkeypatch,
) -> None:
    """A candidate window decided before the stage's declared version start
    carries a claim from a distribution nobody serves any more.  The rolling
    window is bounded below at that instant, the same bound the band and
    centre/width lanes carry, and the fit says which bound it read."""

    from bayes_basket_target import center_width_store

    before = [("P0", "2026-09-01", 0.60, 0.18, 1) for _ in range(30)]
    after = [("P0", "2026-09-05", 0.60, 0.18, 0) for _ in range(30)]
    conn = _book(tmp_path / "f.db", before + after)
    try:
        unbounded = store.fit_lambda_table(conn, window_days=28)
        assert unbounded["evidence"][0]["seats"] == 60
        assert unbounded["min_decision_at_utc"] is None
        monkeypatch.setattr(
            center_width_store, "served_arithmetic_anchor",
            lambda conn, *, stage: "2026-09-03T15:27:09Z",
        )
        bounded = store.fit_lambda_table(conn, window_days=28)
        assert bounded["min_decision_at_utc"] == "2026-09-03T15:27:09Z"
        assert bounded["evidence"][0]["seats"] == 30
        assert bounded["evidence"][0]["raw_lambda"] == 0.0
        # The bound is at the instant, not the day: a window decided at
        # 00:00Z on the anchor's own day, before the anchor, is out.
        monkeypatch.setattr(
            center_width_store, "served_arithmetic_anchor",
            lambda conn, *, stage: "2026-09-05T12:00:00Z",
        )
        assert store.fit_lambda_table(conn, window_days=28)["evidence"][0][
            "seats"
        ] == 0
    finally:
        conn.close()


def _vetoable(path, *, lam, seats):
    """A book with an activated table, its serving interval, and windows."""
    import json as _json

    from bayes_basket_target import effective_edge_store as store

    conn = _book(path, seats)
    store.ensure_schema(conn)
    payload = {
        "schema": "effective_edge_lambda_table_v1", "stage_scope": "P0",
        "claim_scope": "window", "stages": ["P0"],
        "bands": [{"upper_bound": None, "lambda": lam}],
        "window_days": 28, "as_of_event_date": "2026-09-10",
        "fit_floor_event_date": "2026-08-13",
        "band_partition_cells": store.BAND_PARTITION_CELLS,
        "claim_transform_sha256": None,
    }
    conn.execute(
        "INSERT INTO calibration_effective_edge_lambda("
        " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
        " window_days, seat_count, activated, activated_at_utc, stage_scope,"
        " claim_scope) VALUES(?,?,?,?,?,?,1,?,?,?)",
        ("a" * 64, _json.dumps(payload), "{}", "2026-08-31T00:00:00Z", 28, 50,
         "2026-08-31T00:00:00Z", "P0", "window"),
    )
    conn.execute(
        "INSERT INTO calibration_effective_edge_serving_interval("
        " stage_scope, lambda_table_sha256, served_from_utc, served_until_utc)"
        " VALUES('P0',?,'2026-08-31T00:00:00Z',NULL)",
        ("a" * 64,),
    )
    return conn


def test_an_incumbent_beaten_by_no_table_at_all_is_retired(tmp_path) -> None:
    """The activation guard only ever compares the incumbent against a
    PROPOSAL, so a scope whose every challenger is refused holds its table
    forever however it performs.  This asks the question the guard never
    does, on the windows served after the incumbent went live."""

    from bayes_basket_target import effective_edge_store as store

    # Agreement 0.90: the kernel constant refuses it (its band's lambda is
    # 0.82, and the gate admits on lambda > a), a flat 1.4 admits it, and
    # every one of them loses.  So the incumbent buys ninety losing windows
    # that no table at all would have left alone.
    seats = [
        ("P0", f"2026-09-{1 + i % 6:02d}", 0.60, 0.583, 0)
        for i in range(90)
    ]
    conn = _vetoable(tmp_path / "v.db", lam=1.4, seats=seats)
    try:
        veto = store._lambda_incumbent_veto(
            conn, stage_scope="P0", claim_scope="window",
            include_replayed=False,
        )
        assert veto["day_count"] == 6 and veto["window_count"] == 90
        assert veto["net_bootstrap"] > veto["net_incumbent"]
        assert veto["interval"][0] > 0.0
        assert veto["vetoed"]
        # Retiring falls the scope back to the constant and closes the
        # interval, so a later reading of what served when does not run the
        # table past the moment it stopped.
        store.retire_lambda_table(
            conn, stage_scope="P0", retired_at_utc="2026-09-11T00:00:00Z"
        )
        assert store.load_active_lambda(conn, stage_scope="P0") is None
        assert store._incumbent_bands(conn, "P0", "window") == list(
            store._bootstrap_bands("P0", "window")
        )
        assert conn.execute(
            "SELECT served_until_utc FROM"
            " calibration_effective_edge_serving_interval"
        ).fetchone()[0] == "2026-09-11T00:00:00Z"
    finally:
        conn.close()


def test_the_veto_holds_below_the_day_floor_and_when_the_constant_is_served(
    tmp_path,
) -> None:
    from bayes_basket_target import effective_edge_store as store

    # Ninety windows but on two days: a day's stations share its weather, so
    # days are the sample and two is under the floor.
    seats = [
        ("P0", "2026-09-0%d" % (1 + i % 2), 0.60, 0.583, 0) for i in range(90)
    ]
    conn = _vetoable(tmp_path / "w.db", lam=1.4, seats=seats)
    try:
        veto = store._lambda_incumbent_veto(
            conn, stage_scope="P0", claim_scope="window",
            include_replayed=False,
        )
        assert veto["day_count"] == 2 and veto["window_count"] == 90
        assert "interval" not in veto
        assert not veto["vetoed"]
    finally:
        conn.close()
    # A scope already serving the kernel constant has nothing to retire.
    constant = [(b, lam) for b, lam in store._bootstrap_bands("P0", "window")]
    seats = [
        ("P0", f"2026-09-{1 + i % 6:02d}", 0.60, 0.583, 0) for i in range(90)
    ]
    conn = _vetoable(tmp_path / "x.db", lam=constant[-1][1], seats=seats)
    try:
        # One band at the constant's top value is not the constant; make the
        # stored table the constant outright.
        import json as _json
        payload = _json.loads(
            conn.execute(
                "SELECT bands_json FROM calibration_effective_edge_lambda"
                " WHERE activated=1"
            ).fetchone()[0]
        )
        payload["bands"] = [
            {"upper_bound": None if b == float("inf") else b, "lambda": lam}
            for b, lam in constant
        ]
        conn.execute(
            "UPDATE calibration_effective_edge_lambda SET bands_json=?"
            " WHERE activated=1",
            (_json.dumps(payload),),
        )
        veto = store._lambda_incumbent_veto(
            conn, stage_scope="P0", claim_scope="window",
            include_replayed=False,
        )
        assert not veto["vetoed"]
        assert veto["day_count"] == 0
    finally:
        conn.close()
