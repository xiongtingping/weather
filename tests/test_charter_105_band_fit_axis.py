"""The band table can be FITTED on the axis charter 105 ruled it onto.

Charter 105 moved the band table's cells from the claimed probability to the
bucket's distance from the distribution's peak, and ``apply_band_reliability``
learned to read either.  The fitting lane did not: it keyed every cell on the
claim, so the axis existed at serving time and nothing could ever be scored
on it.  Every one of the 101 tables in the store reads ``axis='claim'``.

Two properties carry the change.  The quantity a rung's cell is read from
moves; the claimed mass and the realised count that go into the cell are
always the rung's own.  And the axis is part of a table's identity, because
a distance table whose factors happen to match a claim table would otherwise
collide with it and re-activate the wrong lookup under the right hash.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.band_reliability_store import (  # noqa: E402
    BAND_AXES,
    BAND_AXIS_CENTRE_DISTANCE,
    BAND_AXIS_CLAIM,
    BAND_DISTANCE_CELLS,
    BAND_TABLE_CELLS,
    _day_aggregates,
    _record_keys,
    band_lane_report,
    band_table_axis,
    band_table_sha256,
    cells_for_axis,
    ensure_band_table_schema,
    persist_and_activate_band_table,
)
from bayes_basket_target.cross_consensus import (  # noqa: E402
    _centre_distances,
)

# One station-day, coldest rung first -- which is the ordinal, and the only
# thing the distance axis needs that the fit does not already carry.
SYMMETRIC = [(0.10, 0), (0.20, 0), (0.40, 1), (0.20, 0), (0.10, 0)]


def test_the_cell_key_moves_and_the_mass_does_not():
    assert _record_keys(SYMMETRIC, BAND_AXIS_CLAIM) == [
        0.10, 0.20, 0.40, 0.20, 0.10
    ]
    assert _record_keys(SYMMETRIC, BAND_AXIS_CENTRE_DISTANCE) == [
        2.0, 1.0, 0.0, 1.0, 2.0
    ]
    by_day = {"2026-09-05": [SYMMETRIC]}
    cells = _day_aggregates(by_day, BAND_DISTANCE_CELLS, BAND_AXIS_CENTRE_DISTANCE)
    centre = cells["2026-09-05"][(0.0, 1.0)]
    one_out = cells["2026-09-05"][(1.0, 2.0)]
    two_out = cells["2026-09-05"][(2.0, 3.0)]
    # The centre cell holds the peak's own claim and its own hit.
    assert centre == [pytest.approx(0.40), pytest.approx(1.0)]
    # The neighbours pool, because that is the point of the axis.
    assert one_out == [pytest.approx(0.40), pytest.approx(0.0)]
    assert two_out == [pytest.approx(0.20), pytest.approx(0.0)]
    # Nothing is lost or double-counted across the partition.
    total = sum(cell[0] for cell in cells["2026-09-05"].values())
    assert total == pytest.approx(sum(p for p, _ in SYMMETRIC))


def test_the_fit_reads_the_peak_the_same_way_serving_does():
    """A table fitted on one definition of "the peak" and applied on another
    is a table applied through the wrong lookup.  Both take the argmax and
    break ties on the ordinal."""

    skewed = [(0.05, 0), (0.05, 0), (0.50, 1), (0.20, 0), (0.20, 0)]
    fit_keys = _record_keys(skewed, BAND_AXIS_CENTRE_DISTANCE)
    keys = [f"b{index}" for index in range(len(skewed))]
    serve = _centre_distances(
        {key: claim for key, (claim, _hit) in zip(keys, skewed)},
        {key: index for index, key in enumerate(keys)},
    )
    assert fit_keys == [serve[key] for key in keys]

    # A tie: two rungs at the peak.  Both lanes must pick the same one.
    tied = [(0.30, 0), (0.30, 1), (0.20, 0)]
    tied_keys = [f"t{index}" for index in range(len(tied))]
    assert _record_keys(tied, BAND_AXIS_CENTRE_DISTANCE) == [
        _centre_distances(
            {key: claim for key, (claim, _h) in zip(tied_keys, tied)},
            {key: index for index, key in enumerate(tied_keys)},
        )[key]
        for key in tied_keys
    ]


def test_the_partition_follows_the_axis():
    assert cells_for_axis(BAND_AXIS_CLAIM) == BAND_TABLE_CELLS
    assert cells_for_axis(BAND_AXIS_CENTRE_DISTANCE) == BAND_DISTANCE_CELLS
    assert len(BAND_TABLE_CELLS) == 11 and len(BAND_DISTANCE_CELLS) == 5


def test_the_axis_is_part_of_a_tables_identity_without_moving_the_old_ones():
    factors = [((0.0, 1.0), 1.2), ((1.0, 2.0), 0.9)]
    claim_sha = band_table_sha256(factors, stage="P0", axis=BAND_AXIS_CLAIM)
    distance_sha = band_table_sha256(
        factors, stage="P0", axis=BAND_AXIS_CENTRE_DISTANCE
    )
    assert claim_sha != distance_sha
    # Charter 60: every table already on the book keeps the sha the artifacts
    # pinned to it were gated against, so the shipped axis stays out of the
    # identity and only the new one enters it.
    assert band_table_sha256(factors, stage="P0") == claim_sha


def test_a_fitted_table_carries_its_axis_back_out_of_the_store():
    conn = sqlite3.connect(":memory:")
    ensure_band_table_schema(conn)
    factors = [
        ((0.0, 1.0), 1.20), ((1.0, 2.0), 1.05), ((2.0, 3.0), 0.90),
        ((3.0, 4.0), 0.80), ((4.0, 99.0), 0.60),
    ]
    receipt = persist_and_activate_band_table(
        conn, stage="P0", factors=factors, evidence={"schema": "test"},
        serving_day_count=20, axis=BAND_AXIS_CENTRE_DISTANCE,
    )
    assert receipt["axis"] == BAND_AXIS_CENTRE_DISTANCE
    assert band_table_axis(conn, stage="P0") == BAND_AXIS_CENTRE_DISTANCE
    assert band_table_axis(
        conn, table_sha256=receipt["band_table_sha256"],
    ) == BAND_AXIS_CENTRE_DISTANCE


def test_an_unknown_axis_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError):
        band_table_sha256([((0.0, 1.0), 1.0)], stage="P0", axis="mean_distance")
    conn = sqlite3.connect(":memory:")
    ensure_band_table_schema(conn)
    with pytest.raises(ValueError):
        persist_and_activate_band_table(
            conn, stage="P0", factors=[((0.0, 1.0), 1.0)],
            evidence={}, serving_day_count=20, axis="mean_distance",
        )
    assert set(BAND_AXES) == {BAND_AXIS_CLAIM, BAND_AXIS_CENTRE_DISTANCE}


def test_the_report_is_axis_agnostic_below_the_cell_key():
    """Everything under the cell key -- the by-day bootstrap, the two-halves
    sign check, the conservative factor and the clamp -- is untouched by the
    axis, which is why moving it is a change to one function."""

    by_day = {
        f"2026-09-{day:02d}": [SYMMETRIC, SYMMETRIC] for day in range(1, 15)
    }
    report = band_lane_report(
        by_day, bands=BAND_DISTANCE_CELLS, bootstrap_samples=50,
        axis=BAND_AXIS_CENTRE_DISTANCE,
    )
    assert set(report) <= set(BAND_DISTANCE_CELLS)
    for cell in report.values():
        assert {"ratio", "interval", "halves", "qualified",
                "conservative_factor"} <= set(cell)
    centre = report[(0.0, 1.0)]
    # Every day's peak settles here in this fixture, so the centre realises
    # 1/0.40 of its claim and the fit is being asked to lift it.
    assert centre["ratio"] == pytest.approx(2.5)


def test_the_axis_travels_with_the_table_to_the_serving_lanes():
    """Charter 105, the serving half.

    ``load_band_table`` keeps its two elements -- it has nine callers and
    widening it broke four of them the first time -- and a lane that must
    know which lookup to apply the table through asks for the axis with it.
    A lane that reads the factors and picks the lookup itself is a lane that
    can apply a distance-fitted table through the claim lookup, which is the
    one way this reaches an artifact it was never gated against.
    """

    from bayes_basket_target.band_reliability_store import (
        load_band_table,
        load_band_table_with_axis,
    )

    conn = sqlite3.connect(":memory:")
    ensure_band_table_schema(conn)
    factors = [
        ((0.0, 1.0), 1.20), ((1.0, 2.0), 1.05), ((2.0, 3.0), 0.90),
        ((3.0, 4.0), 0.80), ((4.0, 99.0), 0.60),
    ]
    receipt = persist_and_activate_band_table(
        conn, stage="P0", factors=factors, evidence={"schema": "t"},
        serving_day_count=20, axis=BAND_AXIS_CENTRE_DISTANCE,
    )
    assert len(load_band_table(conn, stage="P0")) == 2
    loaded = load_band_table_with_axis(conn, stage="P0")
    assert loaded is not None and len(loaded) == 3
    assert loaded[1] == receipt["band_table_sha256"]
    assert loaded[2] == BAND_AXIS_CENTRE_DISTANCE
    assert load_band_table_with_axis(conn, stage="P0-M") is None


def test_every_serving_lane_reads_the_tables_axis_not_its_own_preference():
    """Four apply sites, one rule.  Each passes ordinals if and only if the
    table it loaded says its cells are keyed on distance."""

    sources = {
        name: (ROOT / "bayes_basket_target" / name).read_text(encoding="utf-8")
        for name in (
            "materialization.py",
            "native_ensemble_p0.py",
            "native_ensemble_oos.py",
        )
    }
    for name, source in sources.items():
        assert "BAND_AXIS_CENTRE_DISTANCE" in source, name
        # The gate is on the TABLE's axis, and it is spelled the same way at
        # every site so a reader can grep for all four at once.
        assert "== BAND_AXIS_CENTRE_DISTANCE" in source, name
    # The production serving path skips rather than guessing when it has a
    # distance table and no ordering to key it by.
    assert "SKIPPED_NO_BUCKET_ORDINALS" in sources["materialization.py"]
    # And the holdout scores through the same lookup production will serve
    # through, or the table is gated on math nobody runs.
    assert sources["native_ensemble_oos.py"].count(
        "== BAND_AXIS_CENTRE_DISTANCE"
    ) == 2


def test_the_nightly_refit_keeps_each_stage_on_its_own_axis():
    """The gap that would have undone the switch on its own.

    ``refit_all_stages`` had no axis and fitted everything on the claim
    partition.  P0 moved to the distance axis at 2026-09-08T00:44Z and the
    calibration sweep runs every 24 hours, so the switch had about seven
    hours to live -- it would have been reverted by a scheduled job with no
    ruling, no journal entry saying so, and nothing to grep for afterwards.

    ``axis=None`` now means "each stage keeps the axis it is already
    serving".  Moving one is a decision somebody makes on purpose, and an
    explicit ``axis=`` is that decision.
    """

    import inspect

    from bayes_basket_target.band_reliability_store import refit_all_stages

    parameter = inspect.signature(refit_all_stages).parameters["axis"]
    assert parameter.default is None
    source = inspect.getsource(refit_all_stages)
    # Read per stage from the store, not chosen by the caller's default.
    assert "band_table_axis(conn, stage=stage)" in source
    # And carried into all three places that would otherwise silently
    # disagree: the fit, the identity, and the activation.
    assert "axis=stage_axis," in source
    assert source.count("axis=stage_axis") >= 3
    assert '"axis": stage_axis' in source or 'entry["axis"] = stage_axis' in source


def test_the_shadow_lane_refuses_a_pooled_table_it_cannot_read():
    """The shadow lane's cells are (position, claim band), so its base has to
    be a claim-band table.  A distance table's bounds are distances; read as
    claims every bucket falls in the first cell and the other four are never
    reached -- 0.00929 per bucket on a real vector, quietly wrong.  It stops
    by name instead."""

    import inspect

    from bayes_basket_target.band_shadow_store import fit_band_shadow_table

    source = inspect.getsource(fit_band_shadow_table)
    assert "load_band_table_with_axis" in source
    assert "pooled_table_axis_not_claim" in source
    # The refusal comes before the base is built, or it would be built from
    # the table it just refused.
    assert source.index("pooled_table_axis_not_claim") < source.index(
        "base_table"
    )
