"""The claimed-edge reading taken where no fill model can reach it.

The ledger version of this section takes the posted leg as its unit and
splits it by whether the market sold it.  That split stopped being an
observation on 2026-09-11: charter 129 made the paper book's fills
conditional on the outcome, so the filled side is enriched in losers by
construction, and a fever read off it is partly the fill model's own
assumption read back.  This lane scores the seated window against the
truth -- no fill, no basket, no hurdle -- so the same question has an
answer that does not pass through the execution model.

Pinned here: the band boundaries are the section's own, the unit of the
interval is the station-day and not the row, the interval is reproducible
across rebuilds, and the slope is charter 128's form rather than a second
vocabulary for it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


def _rows(spec):
    """(station, day, claim, price, win) tuples from a compact spec."""

    out = []
    for station, day, claim, price, wins, total in spec:
        for i in range(total):
            out.append((station, day, claim, price, 1 if i < wins else 0))
    return out


def test_empty_says_so_rather_than_inventing_bands():
    assert C._claim_edge_calibration([])["available"] is False


def test_the_bands_are_cut_on_claim_minus_price():
    rows = _rows([
        ("A", "2026-09-08", 0.50, 0.60, 5, 10),   # edge -0.10 -> "<0"
        ("B", "2026-09-08", 0.62, 0.60, 5, 10),   # +0.02 -> "2-5%" boundary
        ("C", "2026-09-08", 0.61, 0.60, 5, 10),   # +0.01 -> "0-2%"
        ("D", "2026-09-08", 0.95, 0.60, 5, 10),   # +0.35 -> ">=30%"
    ])
    got = {b["band"]: b["n"] for b in C._claim_edge_calibration(rows)["bands"]}
    assert got == {"<0": 10, "0-2%": 10, "2-5%": 10, ">=30%": 10}


def test_the_interval_counts_station_days_not_rows():
    """Twenty cycles of two days cannot buy a twenty-sample interval."""

    few = C._claim_edge_calibration(_rows([
        ("A", "2026-09-08", 0.5, 0.4, 10, 20),
        ("A", "2026-09-09", 0.5, 0.4, 10, 20),
    ]))
    assert few["bands"][0]["n"] == 40
    assert few["bands"][0]["station_days"] == 2
    # Under the ten-day floor the block bootstrap declines to speak.
    assert few["bands"][0]["realised_lo"] is None


def test_the_interval_is_the_same_on_every_rebuild():
    rows = _rows([
        (f"S{i:02d}", "2026-09-08", 0.8, 0.7, 6 + i % 3, 9)
        for i in range(20)
    ])
    first = C._claim_edge_calibration(rows)
    second = C._claim_edge_calibration(rows)
    assert first == second
    assert first["bands"][0]["realised_lo"] is not None


def test_the_slope_is_charter_128s_and_reads_one_when_claims_deliver():
    """A lane whose disagreement realises in full has beta 1."""

    spec = []
    for i in range(24):
        claim = 0.50 + 0.02 * i
        # truth lands exactly as often as the claim says, price fixed.
        spec.append((f"S{i:02d}", "2026-09-08", claim, 0.50,
                     round(claim * 100), 100))
    out = C._claim_edge_calibration(_rows(spec))
    assert out["beta"] is not None
    assert abs(out["beta"] - 1.0) < 0.05


def test_a_lane_whose_disagreement_is_noise_reads_zero():
    spec = []
    for i in range(24):
        claim = 0.50 + 0.02 * i
        # truth ignores the claim entirely and lands at the price.
        spec.append((f"S{i:02d}", "2026-09-08", claim, 0.50, 50, 100))
    out = C._claim_edge_calibration(_rows(spec))
    assert abs(out["beta"]) < 0.05


def test_the_subset_cuts_report_their_own_multiplicity():
    """Both axes come back, and a thin slice is not cut at all."""

    spec = []
    for i in range(20):
        # Twenty stations, each claiming a spread of things around one
        # price and each landing exactly at the price: pure noise, but a
        # slope is defined because the claim varies.
        for k in range(4):
            spec.append((f"S{i:02d}", "2026-09-08", 0.60 + 0.1 * k, 0.7,
                         35, 50))
    rows = [
        (sid, day, claim, price, win, 30.0)
        for sid, day, claim, price, win in _rows(spec)
    ]
    out = C._claim_edge_calibration(rows)
    assert out["stations_cut"] == 20
    assert [c["key"] for c in out["by_lead"]] == ["24-36h"]
    # A station under the row floor is not reported at all rather than
    # reported with an interval nobody could read.
    thin = [
        (sid, day, claim, price, win, 30.0)
        for sid, day, claim, price, win in _rows(
            [("T00", "2026-09-08", 0.8, 0.7, 50, 100)]
        )
    ]
    assert C._claim_edge_calibration(thin)["by_station"] == []


def test_a_row_without_a_lead_is_kept_for_the_bands_and_dropped_by_lead():
    """The lead axis is physical, so a row that cannot carry it sits out."""

    rows = [
        (sid, day, claim, price, win, None)
        for sid, day, claim, price, win in _rows(
            [(f"S{i:02d}", "2026-09-08", 0.8, 0.7, 6, 10) for i in range(15)]
        )
    ]
    out = C._claim_edge_calibration(rows)
    assert out["rows"] == 150
    assert out["by_lead"] == []
    assert out["by_station"] == []          # 10 rows each, under the floor
    assert out["bands"][0]["n"] == 150
