"""Lambda is fitted on the windows that were offered, not the ones it let through.

A seat exists because ``lambda(a) > a`` held.  Fitting lambda on seats
therefore conditions the sample on the estimate, and not benignly: within a
band the admitted windows are the cheapest ones, cheap is where the market
disagrees with us most, and that is measurably where this model is worst.
The estimate comes out low, the gate tightens, the next fortnight's sample
is drawn from a cheaper slice still.  A ratchet.

It had turned.  Over 923 day-ahead station-days the seated basis reads
0.159/0.368/0.795/0.686/0.650/0.979 -- not even monotone in the axis it is
banded on -- while the same days' enumerated windows read
0.237/0.599/0.764/0.812/0.925/1.231: low in every band, and by 40-50% in
two of them.  A window the gate refused still resolves, and it still says
what a claim of that size at that agreement was worth.

These tests pin the three things that make the new population honest: that a
refused window counts, that nesting cannot be mistaken for evidence, and
that the claim is not lifted twice on its way in.
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
    center_window_claim_factor,
)


def _book() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_trade_candidate_window("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, buckets_json TEXT,"
        " win_probability REAL, all_in_price REAL, hit INTEGER,"
        " admissible INTEGER, seated INTEGER)"
    )
    conn.execute(
        "CREATE TABLE paper_trade_settlement("
        " position_id TEXT, stage TEXT, event_date TEXT, hit INTEGER)"
    )
    conn.execute(
        "INSERT INTO paper_trade_settlement VALUES('p','P0','2026-09-01',0)"
    )
    return conn


def _window(
    conn, index, *, station, day="2026-09-01", claim, price, hit,
    admissible=1, seated=0, stage="P0",
):
    """One enumerated window.  ``claim`` raw in, lifted on disk: the
    enumeration multiplies by charter 81's centre lift before the gate
    divides the price by it, so the stored column is already lifted."""

    conn.execute(
        "INSERT INTO paper_trade_candidate_window"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"c{index}", station, day, stage, f"{day}T00:00:00Z", '["b"]',
            claim * center_window_claim_factor(claim), price, hit,
            admissible, seated,
        ),
    )


def test_a_window_the_gate_refused_is_still_evidence():
    """The whole point, in one fixture.

    One seated window that missed, twenty refused ones that hit, all at the
    same agreement.  On the seated basis the band reads zero and the floor
    clamps it; on the offered basis it reads what the claims were worth.
    """

    conn = _book()
    _window(conn, 0, station="A0", claim=0.5, price=0.15, hit=0,
            admissible=1, seated=1)
    for index in range(1, 21):
        _window(conn, index, station=f"A{index}", claim=0.5, price=0.15,
                hit=1, admissible=0, seated=0)
    fit = store.fit_lambda_table(conn, as_of="2026-09-01")
    band = fit["evidence"][0]
    assert band["seats"] == 21, "the refused twenty are in the sample"
    assert band["hits"] == 20
    assert band["accumulating"] is False
    # 20 hits over 21 lifted claims of 0.5 -- nowhere near the floor a fit
    # on the single seated miss would have produced.
    assert fit["bands"][0][1] > 0.9
    assert fit["bands"][0][1] > store.LAMBDA_FLOOR * 10


def test_neither_admissible_nor_seated_may_filter_the_fit():
    """Stated as a property, because the two columns exist and are one
    ``WHERE`` clause away from putting the ratchet back."""

    source = (
        ROOT / "bayes_basket_target" / "effective_edge_store.py"
    ).read_text(encoding="utf-8")
    for clause in ("seated = 1", "seated=1", "admissible = 1", "admissible=1"):
        assert clause not in source, clause
    assert "FROM paper_trade_candidate_window" in source


def test_twelve_nested_windows_on_two_days_are_not_twelve_observations():
    """Windows nest: one book cut six ways against one outcome is one
    observation wearing six hats.  The row count alone would clear the
    hurdle here; the station-day count is what refuses it."""

    conn = _book()
    index = 0
    for station in ("A", "B"):
        for _ in range(6):
            _window(conn, index, station=station, claim=0.5, price=0.15,
                    hit=1)
            index += 1
    fit = store.fit_lambda_table(conn, as_of="2026-09-01")
    band = fit["evidence"][0]
    assert band["seats"] == 12 >= store.MIN_SEATS_PER_BAND
    assert band["station_days"] == 2
    assert band["accumulating"] is True, "two days is not twelve observations"
    assert fit["bands"][0][1] == band["incumbent_lambda"]
    # The same twelve rows spread over twelve station-days do clear it.
    spread = _book()
    for index in range(12):
        _window(spread, index, station=f"S{index}", claim=0.5, price=0.15,
                hit=1)
    fit = store.fit_lambda_table(spread, as_of="2026-09-01")
    assert fit["evidence"][0]["station_days"] == 12
    assert fit["evidence"][0]["accumulating"] is False


def test_the_recorded_claim_is_not_lifted_a_second_time():
    """``_support_windows`` returns the lifted claim because that is what
    the gate divides the price by.  A fit that lifts it again bands every
    window at an agreement no gate ever looked up."""

    conn = _book()
    claim, price = 0.5, 0.40
    for index in range(12):
        _window(conn, index, station=f"S{index}", claim=claim, price=price,
                hit=1)
    fit = store.fit_lambda_table(conn, as_of="2026-09-01")
    lifted = claim * center_window_claim_factor(claim)
    assert lifted != pytest.approx(claim), "fixture would not detect it"
    agreement = price / lifted
    landed = [
        index for index, band in enumerate(fit["evidence"])
        if band["seats"] == 12
    ]
    assert len(landed) == 1
    bounds = [b for b, _lam in store._incumbent_bands(conn, "P0")]
    lower = 0.0 if landed[0] == 0 else bounds[landed[0] - 1]
    assert lower <= agreement <= bounds[landed[0]]
    # Claim mass is the lifted claim, again unlifted a second time.
    assert fit["evidence"][landed[0]]["claim_mass"] == pytest.approx(
        12 * lifted, rel=1e-6
    )
