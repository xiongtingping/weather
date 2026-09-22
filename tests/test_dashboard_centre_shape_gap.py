"""The P0 gap to the market, split into a centre reading and a shape reading
(2026-09-22 P0 downstream audit, finding 1).

The split moves the model's distribution onto the market's mean with
production's own width-layer transform and calls what that removes the
centre part.  These tests pin the two ends a reader checks the split
against: a model that is the market's distribution moved sideways has a gap
that is all centre, and a model on the market's mean with a different width
has a gap that is all shape.  Then the transform's own entitlement (a move
larger than it may make is bounded and counted), the station-day weighting
and the by-day intervals, and the page registration.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

# A wide closed ladder, so a move of a few rungs never piles mass on an edge.
KEYS = [f"{t}°C" for t in range(10, 31)]
INTERVALS = {f"{t}°C": [t - 0.5, t + 0.5] for t in range(10, 31)}


def _vector(masses: dict[int, float]) -> dict[str, float]:
    return {key: float(masses.get(int(key[:-2]), 0.0)) for key in KEYS}


MARKET = _vector({19: 0.2, 20: 0.5, 21: 0.3})


def test_a_model_that_is_the_market_moved_sideways_is_all_centre():
    """One rung cold with the market's own shape: moving it onto the market's
    mean gives back the market's vector, so the shape part is zero."""

    model = _vector({18: 0.2, 19: 0.5, 20: 0.3})
    split = C.centre_shape_row(model, MARKET, "20°C", ordered_keys=KEYS, intervals=INTERVALS)
    assert split["rps_model"] > split["rps_market"]
    assert split["rps_at_market_mean"] == pytest.approx(split["rps_market"], abs=1e-12)
    assert split["model_minus_market_mean_c"] == pytest.approx(-1.0)
    assert split["clamped"] is False
    # The same rung the other way is worse than not moving at all.
    assert split["rps_random_direction"] > split["rps_at_market_mean"]


def test_a_model_on_the_markets_mean_with_another_width_is_all_shape():
    model = _vector({18: 0.1, 19: 0.2, 20: 0.3, 21: 0.25, 22: 0.15})
    market_mean = sum(MARKET[k] * (int(k[:-2])) for k in KEYS)
    model_mean = sum(model[k] * (int(k[:-2])) for k in KEYS)
    split = C.centre_shape_row(
        model, MARKET, "20°C", ordered_keys=KEYS, intervals=INTERVALS)
    # Means differ only by rounding of the construction; the move is tiny and
    # the gap it leaves is the width difference.
    assert split["model_minus_market_mean_c"] == pytest.approx(model_mean - market_mean)
    gap = split["rps_model"] - split["rps_market"]
    shape = split["rps_at_market_mean"] - split["rps_market"]
    assert gap > 0
    assert shape == pytest.approx(gap, abs=0.02)


def test_a_move_beyond_the_transforms_entitlement_is_bounded_and_counted():
    """apply_center_width_calibration refuses a shift beyond MAX_ABS_SHIFT_C;
    the split moves by the bound and says it did."""

    model = _vector({14: 0.2, 15: 0.5, 16: 0.3})
    split = C.centre_shape_row(model, MARKET, "20°C", ordered_keys=KEYS, intervals=INTERVALS)
    assert split["clamped"] is True
    assert split["model_minus_market_mean_c"] == pytest.approx(-5.0)
    assert split["rps_market"] < split["rps_at_market_mean"] < split["rps_model"]


def _scored(station, day, gap, shape, *, control=0.05, shift=-1.0, clamped=False):
    return {
        "station": station, "date": day, "tier": "12-24", "at": f"{day}T00:00:00Z",
        "rps_model": 0.30 + gap, "rps_market": 0.30,
        "rps_at_market_mean": 0.30 + shape,
        "rps_random_direction": 0.30 + gap + control,
        "model_minus_market_mean_c": shift, "clamped": clamped,
    }


def test_station_days_count_once_and_the_share_is_centre_over_gap():
    rows = [
        # KMIA carries two decisions in the band on one day; together they
        # weigh what KDAL's one decision weighs.
        _scored("KMIA", "2026-09-15", 0.20, 0.00),
        _scored("KMIA", "2026-09-15", 0.00, 0.00),
        _scored("KDAL", "2026-09-15", 0.10, 0.10, clamped=True),
    ]
    reading = C.centre_shape_reading(rows, draws=50, seed=3)
    assert reading["station_days"] == 2 and reading["days"] == 1
    # KMIA averages gap 0.10 / shape 0; KDAL gap 0.10 / shape 0.10.
    assert reading["gap"] == pytest.approx(0.10)
    assert reading["shape"] == pytest.approx(0.05)
    assert reading["centre"] == pytest.approx(0.05)
    assert reading["centre_share"] == pytest.approx(0.5)
    assert reading["control"] == pytest.approx(0.05)
    assert reading["clamped_share"] == pytest.approx(1 / 3)
    # One event day: nothing to resample, so no interval is claimed.
    assert reading["gap_ci"] is None


def test_the_day_bootstrap_brackets_the_point():
    rows = []
    for i in range(12):
        day = f"2026-09-{i + 1:02d}"
        rows.append(_scored("KMIA", day, 0.05 + 0.02 * i, 0.01 * (i % 3)))
        rows.append(_scored("KDAL", day, 0.08, 0.02))
    reading = C.centre_shape_reading(rows, draws=200, seed=11)
    for name in ("gap", "shape", "centre"):
        lo, hi = reading[name + "_ci"]
        assert lo <= reading[name] <= hi


def test_an_empty_sample_reads_as_no_sample_not_as_no_gap():
    reading = C.centre_shape_reading([], draws=10, seed=1)
    assert reading["station_days"] == 0
    assert reading["gap"] is None and reading["centre_share"] is None


def test_the_collector_reads_nothing_from_a_store_without_labels():
    out = C.collect_centre_shape(
        sqlite3.connect(":memory:"), floor_day=None, as_of_event_date=None,
        served_anchor_utc=None, draws=10, seed=1,
    )
    assert out["available"] is False
    assert out["samples"]["stable_pool"]["gap"] is None


def test_the_section_is_registered_in_the_acceptance_box_and_rides_the_ev_cache():
    page = C.render_html({"generated_at_utc": "x"}, started_at=0.0)
    assert "function centreShapeGap()" in page
    assert '[centreShapeGap,"模型对市场差距"],[charterEv,"宪章 EV 恒等式"]' in page
    assert '"模型对市场差距": {layer:"边际层 · ① 验收"' in page
    # A payload drawn before the field existed says so rather than "no gap".
    assert "下一次整库渲染后出现" in page
    assert "charter_ev" in C.SECTION_CACHE_TTL_SEC
