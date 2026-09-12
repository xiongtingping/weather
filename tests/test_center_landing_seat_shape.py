"""The landing panel's seat window is charter 131's, read off the runtime.

Until 2026-09-12 ``collect_center_landing`` hard-coded the mean bucket
+-1 as "the seat window" (charter 49/79, both retired), so on a skewed
day its seat columns described a window production never seats.  The
panel now asks the runtime's own centre, skew and shape functions, and
these tests pin that the answer is the ruled shape -- and that it is the
runtime's answer, not a copy of it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target.basket_kernel.support import (  # noqa: E402
    SKEW_STATE_LEFT,
    SKEW_STATE_RIGHT,
    SKEW_STATE_SYMMETRIC,
)
from bayes_basket_target.delta_v2_runtime import (  # noqa: E402
    seat_shape_windows_v1,
)

KEYS = ["26C", "27C", "28C", "29C", "30C", "31C"]


def _dist(*masses: float) -> dict[str, float]:
    return dict(zip(KEYS, masses))


def test_right_skewed_day_seats_the_tail_and_leaves_the_mode_out():
    # Mode at 27C, mean ordinal 2.09 (28C): gap 1.09 buckets against a
    # 0.35 sigma gate of 0.43 -- read RIGHT_SKEWED.
    probabilities = _dist(0.02, 0.40, 0.25, 0.18, 0.10, 0.05)
    seat = C._center_landing_seat(
        stage="P0", keys=KEYS, probabilities=probabilities
    )
    assert seat is not None
    assert seat["state"] == SKEW_STATE_RIGHT
    assert seat["centre"] == "28C"
    assert seat["seat_keys"] == ["28C", "29C", "30C"]
    assert "27C" not in seat["seat_keys"]


def test_left_skewed_day_mirrors():
    probabilities = _dist(0.05, 0.10, 0.18, 0.25, 0.40, 0.02)
    seat = C._center_landing_seat(
        stage="P0", keys=KEYS, probabilities=probabilities
    )
    assert seat is not None
    assert seat["state"] == SKEW_STATE_LEFT
    assert seat["centre"] == "29C"
    assert seat["seat_keys"] == ["27C", "28C", "29C"]


def test_symmetric_day_seats_the_mean_bucket_and_both_neighbours():
    probabilities = _dist(0.05, 0.15, 0.60, 0.15, 0.05, 0.0)
    seat = C._center_landing_seat(
        stage="P0", keys=KEYS, probabilities=probabilities
    )
    assert seat is not None
    assert seat["state"] == SKEW_STATE_SYMMETRIC
    assert seat["seat_keys"] == ["27C", "28C", "29C"]


def test_the_panel_reads_the_runtime_not_a_copy():
    probabilities = _dist(0.02, 0.40, 0.25, 0.18, 0.10, 0.05)
    seat = C._center_landing_seat(
        stage="P0", keys=KEYS, probabilities=probabilities
    )
    assert seat is not None
    ordinals = {key: index for index, key in enumerate(KEYS)}
    _shapes, audit = seat_shape_windows_v1(
        centre_ordinal=ordinals[seat["centre"]],
        sign=seat["sign"],
        stage="P0",
        probability_by_ordinal={
            ordinals[key]: probabilities[key] for key in KEYS
        },
        live_ordinals=set(ordinals.values()),
    )
    widest = audit["allowed_ordinals"][0]
    assert [ordinals[key] for key in seat["seat_keys"]] == widest


def test_a_dead_tail_rung_drops_the_seat_to_the_two_rung_shape():
    # Charter 125/131: an observed-dead rung is not live, so the three-rung
    # tail window cannot be seated and the ruled two-rung subset is.
    probabilities = _dist(0.02, 0.40, 0.25, 0.18, 0.10, 0.05)
    seat = C._center_landing_seat(
        stage="P0-M", keys=KEYS, probabilities=probabilities,
        excluded=("30C",),
    )
    assert seat is not None
    assert seat["seat_keys"] == ["28C", "29C"]


def test_p1_with_one_live_rung_seats_that_rung_alone():
    probabilities = _dist(0.05, 0.15, 0.60, 0.15, 0.05, 0.0)
    seat = C._center_landing_seat(
        stage="P1", keys=KEYS, probabilities=probabilities,
        excluded=("26C", "27C", "29C", "30C", "31C"),
    )
    assert seat is not None
    assert seat["seat_keys"] == ["28C"]


def test_no_live_rung_means_no_seat():
    assert C._center_landing_seat(
        stage="P0", keys=KEYS, probabilities=_dist(0, 0, 0, 0, 0, 0)
    ) is None


def test_group_row_carries_the_three_window_accounts():
    members = [
        {
            "error": 0.5, "event_date": "2026-09-10", "sign": 1,
            "seat_claim": 0.6, "seat_hit": 1, "same_window": False,
            "mode_claim": 0.7, "mode_hit": 0,
            "legacy_claim": 0.65, "legacy_hit": 0,
        },
        {
            "error": -0.5, "event_date": "2026-09-11", "sign": 0,
            "seat_claim": None, "seat_hit": None, "same_window": None,
            "mode_claim": 0.5, "mode_hit": 1,
            "legacy_claim": 0.5, "legacy_hit": 1,
        },
    ]
    row = C._center_landing_group(members, tier="mid", label="中")
    assert row["cells"] == 2
    assert row["skewed_share"] == 0.5
    # The unseated cell lands in the bias columns but not the seat account.
    assert row["seat_cells"] == 1
    assert row["seat_cover"] == 1.0
    assert row["mode_cells"] == 2 and row["mode_cover"] == 0.5
    assert row["legacy_cells"] == 2 and row["legacy_cover"] == 0.5
    assert row["same_window_share"] == 0.0


def test_cache_salt_names_the_rule_constants():
    salt = C._center_landing_salt()
    rule = C._center_landing_rule()
    assert rule["seat_shape_rule"] in salt
    assert str(rule["skew_threshold_sigma"]) in salt
    assert "def _center_landing_seat" in salt
