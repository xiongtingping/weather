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


def _raw(conn, index, *, claim, price, hit, station=None, stage="P0",
         day="2026-09-01"):
    """A row whose stored claim is exactly ``claim``, so ``price/claim`` is
    the agreement the guard will band on with no lift arithmetic in the way."""

    conn.execute(
        "INSERT INTO paper_trade_candidate_window"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"r{index}", station or f"S{index}", day, stage,
            f"{day}T00:00:00Z", '["b"]', claim, price, hit, 1, 0,
        ),
    )


def _seed_active(conn, bands, sha="0" * 64, scope="P0"):
    import json as _json
    store.ensure_schema(conn)
    conn.execute(
        "INSERT INTO calibration_effective_edge_lambda("
        " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
        " window_days, seat_count, activated, activated_at_utc, stage_scope)"
        " VALUES(?,?,?,?,?,?,1,?,?)",
        (
            sha,
            _json.dumps({"bands": [
                {"upper_bound": None if b == float("inf") else b,
                 "lambda": lam} for b, lam in bands
            ]}),
            "[]", "2026-09-01T00:00:00", 28, 24, "2026-09-01T00:00:00", scope,
        ),
    )
    conn.commit()


# a = 0.40 for the cheap winners, a = 0.92 for the dear losers.
TIGHT = [(0.5, 0.45), (float("inf"), 0.50)]
LOOSE = [(0.5, 0.45), (float("inf"), 0.95)]


def _two_bands(conn):
    for i in range(12):
        _raw(conn, i, claim=0.50, price=0.20, hit=1)
    for i in range(12, 24):
        _raw(conn, i, claim=0.50, price=0.46, hit=0)


def _fit(sha, bands):
    return {
        "content_sha256": sha, "stage_scope": "P0", "bands": bands,
        "evidence": [], "accumulating_bands": [], "seat_count": 24,
        "table": {
            "window_days": 28, "stages": ["P0"],
            "fit_floor_event_date": "2026-08-01",
            "bands": [
                {"upper_bound": None if b == float("inf") else b,
                 "lambda": lam}
                for b, lam in bands
            ],
        },
    }


def test_a_table_that_would_have_bought_worse_is_not_served():
    """The guard the 2026-09-07 fit needed and did not have.

    ``lambda`` is a band AVERAGE and the gate uses it as a THRESHOLD, so a
    table admits precisely the members of each band whose own agreement sits
    closest to the average -- the dearest ones, whose own realisation is
    furthest below it.  Every band's estimate can improve while every
    purchase gets worse, and on the real book that is what happened: the
    newly fitted P0 table read better evidence in every band -- 12 windows
    where there had been none at 0.80, 22 at 0.95, 148 at the top against 58
    seats for the whole table -- and would have taken the book from 64
    windows at +9.2% to 169 at -2.4%.
    """

    conn = _book()
    _two_bands(conn)
    tight = store.admitted_return(conn, TIGHT, ("P0",), "2026-08-01")
    loose = store.admitted_return(conn, LOOSE, ("P0",), "2026-08-01")
    assert tight["admitted"] == 12 and tight["roi"] > 0
    assert loose["admitted"] == 24 and loose["roi"] < tight["roi"]

    _seed_active(conn, TIGHT)
    receipt = store.persist_and_activate(conn, _fit("b" * 64, LOOSE))
    assert receipt["activation_guard"]["blocked"] is True
    assert receipt["activated_at_utc"] is None
    # The fit is journalled anyway -- refusing to serve a table is not the
    # same as refusing to have measured it.
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_effective_edge_fit_journal"
    ).fetchone()[0] == 1
    served = store.load_active_lambda(conn, stage_scope="P0")
    assert [round(lam, 4) for _b, lam in served][-1] == 0.5


def test_a_table_that_would_have_bought_better_is_served():
    """The guard must not be a freeze: an improvement still promotes."""

    conn = _book()
    _two_bands(conn)
    _seed_active(conn, LOOSE)
    receipt = store.persist_and_activate(conn, _fit("d" * 64, TIGHT))
    assert receipt["activation_guard"]["blocked"] is False
    assert receipt["activated_at_utc"] is not None
    served = store.load_active_lambda(conn, stage_scope="P0")
    assert [round(lam, 4) for _b, lam in served][-1] == 0.5


def test_the_guard_reads_the_gate_own_admission_rule():
    """``lambda(a) > a``, strictly, and priced at what the window cost."""

    conn = _book()
    _raw(conn, 0, claim=0.50, price=0.20, hit=1)     # a = 0.40
    exactly = store.admitted_return(
        conn, [(float("inf"), 0.40)], ("P0",), "2026-08-01",
    )
    assert exactly["admitted"] == 0, "equal is not greater"
    just_over = store.admitted_return(
        conn, [(float("inf"), 0.401)], ("P0",), "2026-08-01",
    )
    assert just_over["admitted"] == 1
    assert just_over["cost"] == pytest.approx(0.20)
    assert just_over["payout"] == pytest.approx(1.0)
    assert just_over["roi"] == pytest.approx(4.0)
    # A table that buys nothing earns nothing -- a real comparison, not an
    # undefined one, or the guard could never refuse anything.
    assert exactly["roi"] == 0.0


def test_splitting_the_top_band_moves_nothing_the_day_it_lands():
    """Charter 96's migration, reused: a split only adds boundaries, so
    every new band inherits the value of the old one that contained it and
    the gate reads the same number at every agreement it read before."""

    from bayes_basket_target.basket_kernel.delta_v2 import (
        EFFECTIVE_EDGE_LAMBDA, effective_edge_lambda,
    )
    bounds = [b for b, _lam in EFFECTIVE_EDGE_LAMBDA]
    assert 1.00 in bounds and 1.10 in bounds, "the open top band is split"
    above = {lam for bound, lam in EFFECTIVE_EDGE_LAMBDA if bound > 0.95}
    assert len(above) == 1
    stored = [(0.5, 0.159), (0.7, 0.3677), (0.8, 0.7947), (0.85, 0.6861),
              (0.95, 0.6503), (float("inf"), 0.9792)]
    projected = store._project_onto_partition(stored, EFFECTIVE_EDGE_LAMBDA)
    for agreement in (0.3, 0.6, 0.75, 0.83, 0.9, 0.97, 1.05, 1.3, 9.0):
        assert (
            projected[store._band_index(agreement, projected)][1]
            == stored[store._band_index(agreement, stored)][1]
        ), agreement
    assert effective_edge_lambda(0.97) == effective_edge_lambda(1.3)
