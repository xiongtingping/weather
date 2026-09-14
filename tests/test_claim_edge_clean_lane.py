"""The claimed-edge thermometer: the seated window scored against the truth.

Since 2026-09-14 this is the section's only reading.  The ledger version --
posted legs split by whether the paper book filled them -- was retired that
day: charter 129 made the book's fills conditional on the outcome, so its
filled side read the fill model's own assumption back, and its headline
pooled only the bands that cleared its leg floor.  This lane scores the
seated window against the truth -- no fill, no basket, no hurdle.

Pinned here: the band boundaries are the section's own, the unit of the
interval is the station-day and not the row, the interval is reproducible
across rebuilds and is not degenerate at any day count, the slope is
charter 128's form, the stage split is an exact partition beside the pooled
reading (user ruling 2026-09-02), and the main reading is hit - price
because hit / claim collapses with edge under any noise.
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


def _staged(spec):
    """(station, day, claim, price, win, lead, stage) from a compact spec."""

    out = []
    for station, day, claim, price, wins, total, stage in spec:
        for i in range(total):
            out.append((station, day, claim, price, 1 if i < wins else 0,
                        30.0, stage))
    return out


def test_each_stage_is_reported_beside_the_pooled_reading():
    """Twelve P0 days whose truth never lands and twelve P1 days where it
    always does, all at one claimed edge.  Pooled they read as half right --
    exactly the reading the split exists to take apart."""

    spec = [(f"A{i:02d}", "2026-09-08", 0.5, 0.1, 0, 10, "P0")
            for i in range(12)]
    spec += [(f"B{i:02d}", "2026-09-08", 0.5, 0.1, 10, 10, "P1")
             for i in range(12)]
    out = C._claim_edge_calibration(_staged(spec))
    assert out["stages"] == ["P0", "P1"]
    pooled = {b["band"]: b for b in out["bands"]}
    p0 = {b["band"]: b for b in out["by_stage"]["P0"]["bands"]}
    p1 = {b["band"]: b for b in out["by_stage"]["P1"]["bands"]}
    assert pooled[">=30%"]["hit_rate"] == 0.5
    assert p0[">=30%"]["hit_rate"] == 0.0
    assert p1[">=30%"]["hit_rate"] == 1.0


def test_the_stage_split_is_an_exact_partition_and_unstaged_rows_stay_pooled():
    staged = _staged([
        (f"S{i:02d}", "2026-09-08", 0.6 + 0.01 * i, 0.5, 4, 10,
         ("P0", "P0-M", "P1")[i % 3])
        for i in range(30)
    ])
    unstaged = [row[:6] for row in _staged(
        [("U00", "2026-09-08", 0.9, 0.5, 3, 10, "")]
    )]
    out = C._claim_edge_calibration(staged + unstaged)
    assert out["rows"] == 310
    assert sum(out["by_stage"][st]["rows"] for st in out["stages"]) == 300
    for band in out["bands"]:
        split = sum(
            b["n"] for st in out["stages"]
            for b in out["by_stage"][st]["bands"] if b["band"] == band["band"]
        )
        # The unstaged ten sit in the >=30% band and nowhere in the split.
        assert split == band["n"] - (10 if band["band"] == ">=30%" else 0)


def test_hit_minus_price_is_flat_where_the_ratio_collapses():
    """Truth that follows the price and ignores the claim: the ratio falls
    band by band, which is what the ledger arch drew as a fever; hit - price
    stays at zero in every band, which is what the claim is actually worth."""

    spec = []
    for i in range(40):
        claim = 0.30 + 0.015 * i            # edge from -0.2 to +0.385
        spec.append((f"S{i:02d}", f"2026-09-{1 + i % 20:02d}", claim, 0.50,
                     50, 100, "P0"))
    out = C._claim_edge_calibration(_staged(spec))
    ratios = [b["realised_x"] for b in out["bands"]]
    assert ratios == sorted(ratios, reverse=True) and ratios[0] > ratios[-1]
    assert all(abs(b["hit_minus_price"]) < 1e-9 for b in out["bands"])
    assert abs(out["beta"]) < 0.05
    assert out["beta_vs_half"] in ("below", None)


def test_a_power_of_two_day_count_still_resamples():
    """128 station-days once made every resample a permutation of all of
    them: the low bits of the page's hand-rolled LCG cycled with period 128,
    and the interval collapsed onto the point estimate."""

    spec = [
        (f"S{i:03d}", "2026-09-08", 0.7, 0.6, 3 + (i * 7) % 5, 8, "P0-M")
        for i in range(128)
    ]
    out = C._claim_edge_calibration(_staged(spec))
    band = out["by_stage"]["P0-M"]["bands"][0]
    assert band["station_days"] == 128
    assert band["hit_minus_price_lo"] < band["hit_minus_price_hi"]
    assert band["realised_lo"] < band["realised_hi"]


def test_verdicts_come_with_their_interval_and_not_without_one():
    sick = _staged([
        (f"S{i:02d}", "2026-09-08", 0.9, 0.5, 2 + i % 2, 10, "P0")
        for i in range(20)
    ])
    band = C._claim_edge_calibration(sick)["bands"][0]
    assert band["band"] == ">=30%"
    assert band["realised_verdict"] == "below"
    assert band["hit_minus_price_verdict"] == "below"
    thin = _staged([("S00", "2026-09-08", 0.9, 0.5, 2, 10, "P0")])
    band = C._claim_edge_calibration(thin)["bands"][0]
    assert band["realised_verdict"] is None
    assert band["hit_minus_price_verdict"] is None


def test_the_ledger_thermometer_is_retired():
    """Its filled side read charter 129's outcome-conditioned fills back as
    a fever; a reader that returns would print it again."""

    assert not hasattr(C, "collect_edge_band_health")
