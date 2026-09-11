"""The shadow band lane reads anchored evidence and prices its stations.

Two things the pooled lane fixed on 2026-09-10/11 that the shadow lane,
fitted nightly on the same evidence right after it, had not:

* it read the lane unbounded, pooling 41 days across the four in which
  thirty-two stations were served at half width -- a cell fitted to a
  retired shape, and an increment measured over a pooled base that was
  itself fitted on the anchored sample;
* its interval resampled days only, so a position cell made of forty-five
  stations priced as if the stations were one.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from bayes_basket_target import band_shadow_store
from bayes_basket_target.band_shadow_store import (
    _bootstrap_draws,
    _cell_statistics,
    _marginalise,
    collect_shadow_sample,
    fit_band_shadow_table,
)
from bayes_basket_target.band_reliability_store import _truth
from tests.test_band_shadow_store import _synthetic_lane


def test_the_bound_excludes_station_days_decided_before_it() -> None:
    conn = sqlite3.connect(":memory:")
    _synthetic_lane(conn, days=24, stations=6)
    truth = _truth(conn)
    unbounded = collect_shadow_sample(conn, truth, stage="P0")
    bounded = collect_shadow_sample(
        conn, truth, stage="P0",
        min_decision_at_utc="2026-08-13T00:00:00Z",
    )
    assert len(unbounded["days"]) == 24
    assert bounded["days"] == [d for d in unbounded["days"] if d >= "2026-08-13"]
    assert bounded["min_decision_at_utc"] == "2026-08-13T00:00:00Z"
    assert unbounded["min_decision_at_utc"] is None


def test_the_fit_defaults_to_the_stage_anchor_and_records_it(monkeypatch) -> None:
    """None means "ask the stage", the empty string means unbounded --
    the pooled lane's convention, so a caller cannot get the old behaviour
    by forgetting an argument."""

    conn = sqlite3.connect(":memory:")
    _synthetic_lane(conn, days=24, stations=6)
    import bayes_basket_target.center_width_store as cw

    monkeypatch.setattr(
        cw, "served_arithmetic_anchor",
        lambda conn, *, stage, known_floor=None: "2026-08-13T00:00:00Z",
    )
    anchored = fit_band_shadow_table(conn, stage="P0")
    assert anchored["evidence"]["min_decision_at_utc"] == "2026-08-13T00:00:00Z"
    assert anchored["serving_day_count"] == 12
    assert anchored["evidence"]["interval_resampling"] == "day_then_station"

    unbounded = fit_band_shadow_table(conn, stage="P0", min_decision_at_utc="")
    assert unbounded["evidence"]["min_decision_at_utc"] is None
    assert unbounded["serving_day_count"] == 24


def _panel_cell(days, stations, *, claimed=0.6, hit_every=3):
    """A position-style cell: the same over-claim at every station, so the
    day view and the station view carry the same totals."""

    by_day = {}
    by_day_station = {}
    index = 0
    for day in days:
        per_station = {}
        for station in stations:
            hit = 1 if index % hit_every == 0 else 0
            per_station[station] = (claimed, hit)
            index += 1
        by_day_station[day] = per_station
        by_day[day] = (
            sum(v[0] for v in per_station.values()),
            sum(v[1] for v in per_station.values()),
        )
    return {
        "by_day": by_day,
        "by_day_station": by_day_station,
        "rows": len(days) * len(stations) * 3,
    }


def test_a_pooled_cell_prices_its_stations_and_a_single_station_cell_does_not() -> None:
    days = [f"2026-08-{d:02d}" for d in range(1, 13)]
    stations = [f"ST{i:02d}" for i in range(40)]
    draws = _bootstrap_draws(days)

    pooled = _panel_cell(days, stations)
    day_only = dict(pooled)
    day_only.pop("by_day_station")
    two_way = _cell_statistics(pooled, days=days, draws=draws)
    legacy = _cell_statistics(day_only, days=days, draws=draws)
    assert two_way["ratio"] == pytest.approx(legacy["ratio"])
    assert (two_way["ci_high"] - two_way["ci_low"]) >= (
        legacy["ci_high"] - legacy["ci_low"]
    ) - 1e-9

    single = _panel_cell(days, ["ST00"])
    single_day_only = dict(single)
    single_day_only.pop("by_day_station")
    with_view = _cell_statistics(single, days=days, draws=draws)
    without = _cell_statistics(single_day_only, days=days, draws=draws)
    assert with_view["ci_low"] == pytest.approx(without["ci_low"])
    assert with_view["ci_high"] == pytest.approx(without["ci_high"])


def test_the_station_resample_is_reproducible_and_conserves_mass() -> None:
    days = [f"2026-08-{d:02d}" for d in range(1, 9)]
    stations = [f"ST{i:02d}" for i in range(12)]
    draws = _bootstrap_draws(days)
    cell = _panel_cell(days, stations)
    first = _cell_statistics(cell, days=days, draws=draws)
    second = _cell_statistics(cell, days=days, draws=draws)
    assert first["ci_low"] == second["ci_low"]
    assert first["ci_high"] == second["ci_high"]
    # Every draw resamples exactly as many station-days as the day draw
    # allots to it, so the expected sampled mass is the observed mass.
    claimed = np.array([cell["by_day"][d][0] for d in days])
    hits = np.array([cell["by_day"][d][1] for d in days])
    sampled_claimed, sampled_hits = band_shadow_store._two_way_resample(
        cell, days=days, draws=draws, claimed=claimed, hits=hits,
    )
    assert sampled_claimed.mean() == pytest.approx(claimed.sum(), rel=0.02)
    assert sampled_hits.mean() == pytest.approx(hits.sum(), rel=0.05)


def test_marginalise_keeps_each_stations_mass_apart() -> None:
    conn = sqlite3.connect(":memory:")
    _synthetic_lane(conn, days=10, stations=4)
    sample = collect_shadow_sample(conn, _truth(conn), stage="P0")
    finest = sample["cells"]
    for key, cell in finest.items():
        for day, stations in cell["by_day_station"].items():
            assert list(stations) == [key[0]]
    position = _marginalise(finest, level="position")
    for key, cell in position.items():
        for day, stations in cell["by_day_station"].items():
            assert sum(v[0] for v in stations.values()) == pytest.approx(
                cell["by_day"][day][0]
            )
            assert len(stations) <= 4
