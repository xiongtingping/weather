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


def test_the_shadow_lane_follows_the_pooled_table_it_is_scored_against():
    """SUPERSEDES ``..._refuses_a_pooled_table_it_cannot_read`` (2026-09-09).

    The refusal was right for as long as the lane could only key on claims:
    a distance table read as claims puts every bucket in the first cell,
    0.00929 per bucket on a real vector, quietly wrong.  Stopping was better
    than that.  But it is not better than keying correctly, and it stalled
    P0's candidate layer for two days -- the lane FOLLOWS the pooled axis
    now, and the sample it collects is keyed on the same axis.

    The old assertion (``pooled_table_axis_not_claim`` appears before
    ``base_table``) is deliberately gone rather than inverted: the refusal
    branch does not exist, so there is nothing left to order.
    """

    import inspect

    from bayes_basket_target.band_shadow_store import (
        collect_shadow_sample,
        fit_band_shadow_table,
    )

    source = inspect.getsource(fit_band_shadow_table)
    assert "load_band_table_with_axis" in source
    assert "pooled_table_axis_not_claim" not in source
    # The axis read off the pooled table is the axis the sample is keyed on,
    # and it is read before the base is built.
    assert "axis=pooled_axis" in source
    assert source.index("pooled_axis") < source.index("base_table")
    # And the fallback partition follows it too: a stage with no pooled table
    # yet must not fall back to eleven claim cells on a distance lane.
    assert "cells_for_axis(pooled_axis)" in source
    collected = inspect.getsource(collect_shadow_sample)
    assert "BAND_AXIS_CENTRE_DISTANCE" in collected
    assert "abs(offset)" in collected


def test_every_lookup_in_the_module_follows_the_axis():
    """The defect this file exists to stop, found the second time.

    ``_day_aggregates`` was made axis-aware and ``_forward_nll_delta`` was
    not, and it holds the module's OTHER copy of the same lookup.  A distance
    table scored there against distance-shaped bounds using claim values puts
    every bucket in the first cell -- all claims are under 1 and the cells run
    0-1, 1-2, 2-3, 3-4, 4+ -- so the other four are never reached and changing
    any one of them moves the total by exactly zero.

    Measured on the live sample 2026-09-08: every qualified cell of both
    stages that run this check read +-0.0000, which is not "did not improve
    enough" but "was not being measured".  Keyed correctly, P1 admits five of
    five and P0-M two.  It went unnoticed because P0 -- the stage that moved
    first -- is exempt from this check by ``requires_forward_check``.

    So the property is asserted over the module rather than over one
    function: every place that walks a factor table looks up the key
    ``_record_keys`` produced, and no place walks it against a raw claim.
    """

    import inspect

    from bayes_basket_target import band_reliability_store as store

    for name in ("_day_aggregates", "_forward_nll_delta"):
        source = inspect.getsource(getattr(store, name))
        assert "_record_keys(" in source, name
        assert "axis" in inspect.signature(getattr(store, name)).parameters, name
        # The give-away shape of the old bug: comparing a cell bound to the
        # claim itself rather than to the axis's key.
        assert "<= probability <" not in source, name

    # And every caller inside the fit hands its own axis down; a default
    # would silently reinstate the claim lookup for a distance table.
    fit = inspect.getsource(store.fit_band_reliability_table)
    assert fit.count("_forward_nll_delta(") == 2
    assert fit.count("axis=axis_id,") >= 4


def test_the_forward_check_can_actually_separate_distance_cells():
    """A behavioural check, because the assertions above are structural.

    Two synthetic days where the far cell is badly over-claimed: keyed on
    distance the far factor improves the window, keyed on the claim it cannot
    move at all because every bucket sits in the first cell.
    """

    from bayes_basket_target.band_reliability_store import (
        _forward_nll_delta,
    )

    # Nine rungs with the peak at one end, so distances run 0..8 and the
    # far cell is actually reachable -- five rungs peaked in the middle only
    # reach distance two, which is how the first draft of this test passed
    # for the wrong reason.  The centre wins every day; the far rungs claim
    # mass they never realise.
    day = [[
        (0.40, 1), (0.20, 0), (0.12, 0), (0.08, 0),
        (0.05, 0), (0.05, 0), (0.04, 0), (0.03, 0), (0.03, 0),
    ]]
    by_day = {f"2026-09-{d:02d}": list(day) for d in range(1, 11)}
    base = {c: 1.0 for c in BAND_DISTANCE_CELLS}
    cut = dict(base)
    cut[(4.0, 99.0)] = 0.5

    on_axis = _forward_nll_delta(
        by_day, base_factors=base, candidate_factors=cut,
        axis=BAND_AXIS_CENTRE_DISTANCE,
    )
    on_claim = _forward_nll_delta(
        by_day, base_factors=base, candidate_factors=cut,
        axis=BAND_AXIS_CLAIM,
    )
    assert on_axis > 0.0, "cutting the far cell has to help, keyed correctly"
    assert on_claim == pytest.approx(0.0), (
        "keyed on the claim the far cell is unreachable and the check is blind"
    )


def test_the_journal_files_a_fit_under_the_sha_the_served_table_carries():
    """The trail has to point at the row it is the trail of.

    ``journal_band_fit`` computed the sha without the axis, so from
    2026-09-07 -- the day P0's table moved to the centre-distance axis --
    every P0 journal entry named a sha no row in
    ``calibration_band_reliability_table`` held.  The dashboard joins the
    two on exactly that sha to decide whether a journal row describes the
    serving table, so P0's evidence panel fell back to the active row's own
    frozen counts on every fit.  Both writers see the same axis or the
    journal is not a trail.
    """

    from bayes_basket_target.band_reliability_store import journal_band_fit

    conn = sqlite3.connect(":memory:")
    ensure_band_table_schema(conn)
    factors = [
        ((0.0, 1.0), 1.02), ((1.0, 2.0), 1.05), ((2.0, 3.0), 1.0),
        ((3.0, 4.0), 0.96), ((4.0, 99.0), 0.68),
    ]
    receipt = persist_and_activate_band_table(
        conn, stage="P0", factors=factors, evidence={"schema": "test"},
        serving_day_count=40, axis=BAND_AXIS_CENTRE_DISTANCE,
    )
    fit_id = journal_band_fit(
        conn,
        stage="P0",
        fitted={
            "factors": factors,
            "evidence": {"serving_station_day_count": 1445},
            "serving_day_count": 40,
        },
        outcome="ACTIVATED",
        axis=BAND_AXIS_CENTRE_DISTANCE,
    )
    journalled = conn.execute(
        "SELECT band_table_sha256 FROM calibration_band_reliability_fit_journal"
        " WHERE fit_id=?",
        (fit_id,),
    ).fetchone()[0]
    assert journalled == receipt["band_table_sha256"]

    # A caller that does not pass the axis reads the one the stage serves
    # rather than assuming the shipped one -- the same failure by omission.
    fit_id_default = journal_band_fit(
        conn,
        stage="P0",
        fitted={
            "factors": factors,
            "evidence": {"serving_station_day_count": 1445},
            "serving_day_count": 40,
        },
        outcome="UNCHANGED",
    )
    assert conn.execute(
        "SELECT band_table_sha256 FROM calibration_band_reliability_fit_journal"
        " WHERE fit_id=?",
        (fit_id_default,),
    ).fetchone()[0] == receipt["band_table_sha256"]
