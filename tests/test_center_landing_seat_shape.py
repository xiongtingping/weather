"""The landing panel's seat window: charter 131's, computed by the backend.

Until 2026-09-12 ``collect_center_landing`` hard-coded the mean bucket +-1
as "the seat window"; until 2026-10-02 the board ran charter 131's seat rule
itself on every served cell.  User rulings 2026-10-02 (「只读是读后端算出来
的」, 「新开一个后端读数任务」): the backend board-readings job computes the
window with the runtime's own functions (``board_readings.seat_window``) for
every served cell and writes it; the panel only reads that file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target import board_readings as BR  # noqa: E402
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
    seat = BR.seat_window(
        stage="P0", keys=KEYS, probabilities=probabilities
    )
    assert seat is not None
    assert seat["state"] == SKEW_STATE_RIGHT
    assert seat["centre"] == "28C"
    assert seat["seat_keys"] == ["28C", "29C", "30C"]
    assert "27C" not in seat["seat_keys"]


def test_left_skewed_day_mirrors():
    probabilities = _dist(0.05, 0.10, 0.18, 0.25, 0.40, 0.02)
    seat = BR.seat_window(
        stage="P0", keys=KEYS, probabilities=probabilities
    )
    assert seat is not None
    assert seat["state"] == SKEW_STATE_LEFT
    assert seat["centre"] == "29C"
    assert seat["seat_keys"] == ["27C", "28C", "29C"]


def test_symmetric_day_seats_the_mean_bucket_and_both_neighbours():
    probabilities = _dist(0.05, 0.15, 0.60, 0.15, 0.05, 0.0)
    seat = BR.seat_window(
        stage="P0", keys=KEYS, probabilities=probabilities
    )
    assert seat is not None
    assert seat["state"] == SKEW_STATE_SYMMETRIC
    assert seat["seat_keys"] == ["27C", "28C", "29C"]


def test_the_backend_reads_the_runtime_not_a_copy():
    probabilities = _dist(0.02, 0.40, 0.25, 0.18, 0.10, 0.05)
    seat = BR.seat_window(
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
    seat = BR.seat_window(
        stage="P0-M", keys=KEYS, probabilities=probabilities,
        excluded=("30C",),
    )
    assert seat is not None
    assert seat["seat_keys"] == ["28C", "29C"]


def test_p1_with_one_live_rung_seats_that_rung_alone():
    probabilities = _dist(0.05, 0.15, 0.60, 0.15, 0.05, 0.0)
    seat = BR.seat_window(
        stage="P1", keys=KEYS, probabilities=probabilities,
        excluded=("26C", "27C", "29C", "30C", "31C"),
    )
    assert seat is not None
    assert seat["seat_keys"] == ["28C"]


def test_no_live_rung_means_no_seat():
    assert BR.seat_window(
        stage="P0", keys=KEYS, probabilities=_dist(0, 0, 0, 0, 0, 0)
    ) is None


def _write_seats(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(BR, "READINGS_DIR", tmp_path)
    BR.write_reading("seat_windows", {"seats": {
        "KAAA|2026-09-20|2026-09-19T06:00:00Z": {
            "state": "RIGHT_SKEWED", "sign": 1, "seat_keys": ["29C", "30C", "31C"],
        },
        "KAAA|2026-09-20|2026-09-19T03:00:00Z": None,
    }}, code="x", build_sec=1.0)


def _cell(decided: str) -> dict:
    probabilities = dict(zip(KEYS, (0.02, 0.08, 0.2, 0.4, 0.2, 0.1)))
    return {
        "station_id": "KAAA", "event_date": "2026-09-20",
        "decision_at_utc": decided, "probabilities": probabilities,
        "ordered_keys": KEYS,
        "key_centers_c": [26.0 + index for index in range(len(KEYS))],
        "settled_bucket": "30C", "excluded_buckets": [],
    }


def test_the_board_reads_the_backends_seat_for_its_own_cell_and_no_other(tmp_path, monkeypatch) -> None:
    _write_seats(tmp_path, monkeypatch)
    seats = C._stored_seat_shapes()
    assert list(seats) == [("KAAA", "2026-09-20", "2026-09-19T06:00:00Z")]
    truth = {("KAAA", "2026-09-20"): 29.6}
    decided = C._center_landing_entry("P0", _cell("2026-09-19T06:00:00Z"), truth, seats)
    assert decided["state"] == "RIGHT_SKEWED" and decided["sign"] == 1
    assert abs(decided["seat_claim"] - 0.7) < 1e-12 and decided["seat_hit"] == 1
    # A cell the job has not reached (or that seats nothing) has no seat.
    other = C._center_landing_entry("P0", _cell("2026-09-19T09:00:00Z"), truth, seats)
    assert other["state"] is None and other["seat_claim"] is None
    assert other["mode_claim"] is not None, "the mode window is plain arithmetic and stays"


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
    # Skew is the seat audit's, so it is read among the decided cells only.
    assert row["skewed_share"] == 1.0
    # The unseated cell lands in the bias columns but not the seat account.
    assert row["seat_cells"] == 1
    assert row["seat_cover"] == 1.0
    assert row["mode_cells"] == 2 and row["mode_cover"] == 0.5
    assert row["legacy_cells"] == 2 and row["legacy_cover"] == 0.5
    assert row["same_window_share"] == 0.0


def test_cache_salt_names_the_rule_constants_and_the_reader():
    salt = C._center_landing_salt()
    rule = C._center_landing_rule()
    assert rule["seat_shape_rule"] in salt
    assert str(rule["skew_threshold_sigma"]) in salt
    assert "def _stored_seat_shapes" in salt
