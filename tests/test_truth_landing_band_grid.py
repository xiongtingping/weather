"""The truth-landing table's ruler: one plain 5% grid, no extra cut.

Section "真值落位（价格 × 概率）" asks where truth actually lives -- at which
market price, at which claimed probability -- and the answer is only as
readable as the ruler it is measured with.  From 2026-08-25 that ruler had
an extra cut at the 0.03 leg floor, so the first cell was 0-3% and 3-5%
instead of one 0-5%; the user withdrew that cut on 2026-09-11 and the grid
is now a plain five-percent ruler, the same shape charter 60 fits the band
table on.

Pinned here because the grid is a module-level constant three different
renderings read -- both margins and both axes of the cross-tab -- so a cut
put back in one place and not the others would leave the page comparing a
row against a column that were measured differently.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


def test_every_band_below_the_cap_is_five_points_wide() -> None:
    for low, high in C.TRUTH_LANDING_PRICE_BANDS[:-1]:
        assert round(high - low, 10) == 0.05, (
            f"[{low}, {high}) is not a 5% cell -- the user's 2026-09-11"
            " ruling is one plain 5% ruler for this table"
        )


def test_the_grid_starts_at_zero_and_leaves_no_gap() -> None:
    bands = C.TRUTH_LANDING_PRICE_BANDS
    assert bands[0][0] == 0.0
    for (_, prev_high), (next_low, _) in zip(bands, bands[1:]):
        assert prev_high == next_low, (
            "a gap or an overlap between cells makes the margins stop"
            " summing to the population they are drawn from"
        )


def test_the_top_cell_is_open_above_a_half() -> None:
    """Everything at half or more is one cell, as it is in the fitted table.

    Not a 5% cell on purpose: above 50% the book is thin and splitting it
    buys ten cells nobody can read.
    """

    low, high = C.TRUTH_LANDING_PRICE_BANDS[-1]
    assert (low, high) == (0.50, 1.01)


def test_the_cross_tab_reads_the_same_ruler_as_the_margins() -> None:
    """Rows, columns and both margins have to be the same cells.

    The cross-tab's whole claim is a comparison between the two axes --
    below the diagonal the model is the optimist, above it the market is --
    and that reading is only true while a row and a column with the same
    label mean the same interval.
    """

    assert C.TRUTH_LANDING_COARSE == C.TRUTH_LANDING_PRICE_BANDS


def test_the_first_cell_no_longer_carries_the_leg_floor_cut() -> None:
    """The withdrawn cut, named so putting it back is a deliberate act.

    0-3% was the penny zone the 0.03 leg floor refuses to buy, and the
    table carried that boundary for two and a half weeks.  It is gone (user
    ruling, 2026-09-11); the floor is still ruled on elsewhere, it just no
    longer shapes this ruler.
    """

    edges = {low for low, _ in C.TRUTH_LANDING_PRICE_BANDS}
    assert 0.03 not in edges
    assert C.TRUTH_LANDING_PRICE_BANDS[0] == (0.0, 0.05)
