"""The rolling refit has one job the constant it replaces could not do.

Charter 68's table was fitted once by hand.  What this layer must guarantee
is not that it fits well -- a fit is a fit -- but that it never serves a
number the evidence stopped supporting, and that it says so out loud when a
band goes quiet.  That failure is the one the endogeneity warning predicts:
a rule that refuses the seats it distrusts stops observing the band it most
needs to keep measuring.
"""

from __future__ import annotations

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
    effective_edge_lambda,
)


def _book(path: Path, seats) -> sqlite3.Connection:
    """A minimal settled book: one position and one settlement per seat."""

    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "CREATE TABLE paper_trade_position ("
        " position_id TEXT PRIMARY KEY, win_probability REAL,"
        " all_in_cost REAL)"
    )
    conn.execute(
        "CREATE TABLE paper_trade_settlement ("
        " position_id TEXT, stage TEXT, event_date TEXT, hit INTEGER)"
    )
    for index, (stage, day, claim, cost, hit) in enumerate(seats):
        pid = f"p{index}"
        conn.execute(
            "INSERT INTO paper_trade_position VALUES(?,?,?)",
            (pid, claim, cost),
        )
        conn.execute(
            "INSERT INTO paper_trade_settlement VALUES(?,?,?,?)",
            (pid, stage, day, hit),
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
        assert store.FIT_STAGES == ("P0", "P0-M")
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
