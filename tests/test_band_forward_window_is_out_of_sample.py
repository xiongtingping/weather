"""The band lane's forward check scores days its factors never saw.

Charter 60 admits a P0-M/P1 band cell only when it improves NLL on the
forward window.  Until 2026-09-13 the factor was fitted on every serving day
and scored on the trailing 40% of those same days (model-layer review R4:
all 8 "forward" days of a 20-day fit were fit days).  Now the forward check
fits the same rule on the days before the window and scores the window; the
factor served is still the full-sample one.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import band_reliability_store as band_store  # noqa: E402

CENTRE = (0.0, 1.0)
STATIONS = 40


def _day(centre_hits: int) -> list[list[tuple[float, int]]]:
    rows = []
    for index in range(STATIONS):
        if index < centre_hits:
            rows.append([(0.3, 0), (0.5, 1), (0.2, 0)])
        elif index % 2:
            rows.append([(0.3, 1), (0.5, 0), (0.2, 0)])
        else:
            rows.append([(0.3, 0), (0.5, 0), (0.2, 1)])
    return rows


def _fit(days, *, spies=None):
    station_days = {day: [f"S{i:02d}" for i in range(STATIONS)] for day in days}
    patches = [
        patch.object(band_store, "_truth", return_value={}),
        patch.object(
            band_store, "serving_lane_by_day", return_value=(days, station_days)
        ),
        patch.object(band_store, "load_band_table_with_axis", return_value=None),
        patch.object(band_store, "_first_decision_by_station_day", return_value={}),
        patch.object(band_store, "_active_band_activated_at", return_value=None),
    ]
    for spy in spies or []:
        patches.append(spy)
    for item in patches:
        item.start()
    try:
        return band_store.fit_band_reliability_table(
            sqlite3.connect(":memory:"), stage="P1", bootstrap_samples=200,
            axis=band_store.BAND_AXIS_CENTRE_DISTANCE, min_decision_at_utc="",
        )
    finally:
        for item in patches:
            item.stop()


def _days(front_hits: int, tail_hits: int) -> dict[str, list]:
    names = [f"2026-08-{i:02d}" for i in range(1, 21)]
    fit, score = band_store.forward_window_split(names)
    assert (len(fit), len(score)) == (12, 8)
    return {
        day: _day(front_hits if day in fit else tail_hits) for day in names
    }


def test_a_cell_the_fit_days_cannot_qualify_is_not_admitted_on_its_future() -> None:
    """Weak on the fit days, strong on the scored ones.

    The full sample qualifies the centre cell -- driven by the very days the
    check scores -- and the old in-sample check admitted it.  From the fit
    days alone the interval spans one, so there is no factor to test.
    """

    fitted = _fit(_days(front_hits=21, tail_hits=32))
    evidence = fitted["evidence"]
    assert evidence["serving_report"][str(list(CENTRE))]["qualified"] is True
    assert evidence["basis"][str(list(CENTRE))] == (
        "identity:forward_fit_not_qualified"
    )
    assert dict(fitted["factors"])[CENTRE] == 1.0


def test_a_cell_that_holds_forward_is_admitted_at_its_full_sample_factor() -> None:
    fitted = _fit(_days(front_hits=32, tail_hits=32))
    evidence = fitted["evidence"]
    assert evidence["basis"][str(list(CENTRE))] == "serving_lane"
    full = evidence["serving_report"][str(list(CENTRE))]["conservative_factor"]
    assert dict(fitted["factors"])[CENTRE] == full
    assert dict(
        (tuple(band), value) for band, value in evidence["forward_factors"]
    )[CENTRE] > 1.0


def test_no_forward_score_reads_a_day_its_factors_were_fitted_on() -> None:
    fitted_days: list[set[str]] = []
    scored: list[tuple[set[str], dict]] = []
    real_report = band_store.band_lane_report
    real_forward = band_store._forward_nll_delta

    def report_spy(data, **kwargs):
        fitted_days.append(set(data))
        return real_report(data, **kwargs)

    def forward_spy(data, **kwargs):
        scored.append((set(kwargs.get("score_days") or ()), kwargs))
        return real_forward(data, **kwargs)

    fitted = _fit(
        _days(front_hits=32, tail_hits=32),
        spies=[
            patch.object(band_store, "band_lane_report", side_effect=report_spy),
            patch.object(band_store, "_forward_nll_delta", side_effect=forward_spy),
        ],
    )
    evidence = fitted["evidence"]
    fit_days = set(evidence["forward_fit_days"])
    score_days = set(evidence["forward_score_days"])
    assert fit_days and score_days and not fit_days & score_days
    # One report is the full sample (what is served); the forward factors
    # come from a report over the fit days only.
    assert fit_days in fitted_days
    assert scored, "the forward check never ran"
    for days, _kwargs in scored:
        assert days == score_days
        assert not days & fit_days
