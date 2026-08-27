"""The next-day fleet regime shift: guards, ladder, and composition.

The layer has no fitted parameter, so what has to be tested is not a fit but
the things that keep an unfitted correction honest: that it cannot see a day
production could not have seen, that it stands down rather than guessing when
its evidence is thin or stale, that a run is a run and not a coincidence of
sign, and that it spends only its own entitlement when it composes with the
fitted scalars beside it.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import fleet_regime_shift as F  # noqa: E402
from bayes_basket_target.center_width_calibration import (  # noqa: E402
    MAX_ABS_REGIME_SHIFT_C,
    apply_center_width_calibration,
)


def _row(day: str, residual: float, stations: int = 40) -> dict:
    return {
        "event_date": day,
        "residual_c_mean": residual,
        "station_count": stations,
        "cell_count": stations * 3,
        "truth_visible_at_utc": f"{day}T23:00:00Z",
    }


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    F.ensure_fleet_regime_schema(connection)
    yield connection
    connection.close()


# --------------------------------------------------------------------------
# What the layer may see
# --------------------------------------------------------------------------
def test_a_day_is_invisible_until_its_truth_has_arrived(conn) -> None:
    F.upsert_daily_residual(
        conn, stage="P0", event_date="2026-08-20", residual_c_mean=0.6,
        station_count=40, cell_count=120,
        truth_visible_at_utc="2026-08-21T04:00:00Z",
        now_utc="2026-08-21T04:00:00Z",
    )
    before = F.visible_daily_series(
        conn, stage="P0", as_of_utc="2026-08-21T03:59:00Z"
    )
    after = F.visible_daily_series(
        conn, stage="P0", as_of_utc="2026-08-21T04:01:00Z"
    )
    assert before == []
    assert [row["event_date"] for row in after] == ["2026-08-20"]


def test_a_signal_dated_after_the_day_being_served_is_refused() -> None:
    receipt = F.derive_offset([_row("2026-08-28", 0.9)], event_date="2026-08-27")
    assert receipt["state"] == "STOOD_DOWN"
    assert receipt["reason"] == "signal_after_event_date"
    assert receipt["regime_delta_c"] == 0.0


def test_a_stale_signal_stands_the_correction_down_but_still_alerts() -> None:
    receipt = F.derive_offset(
        [_row("2026-08-20", 0.9)], event_date="2026-08-27"
    )
    assert receipt["state"] == "STOOD_DOWN"
    assert receipt["reason"] == "signal_stale"
    assert receipt["regime_delta_c"] == 0.0
    # The whole point of separating the two: the day the correction refuses to
    # act is the day somebody should be looking.
    assert receipt["alert"] == "RED"


def test_a_collapsed_truth_lane_stands_the_correction_down() -> None:
    receipt = F.derive_offset(
        [_row("2026-08-26", 0.9, stations=F.MIN_STATION_COUNT - 1)],
        event_date="2026-08-27",
    )
    assert receipt["state"] == "STOOD_DOWN"
    assert receipt["reason"] == "station_count_below_floor"


def test_no_evidence_at_all_is_identity_not_a_guess() -> None:
    receipt = F.derive_offset([], event_date="2026-08-27")
    assert receipt["state"] == "IDENTITY"
    assert receipt["regime_delta_c"] == 0.0
    assert receipt["alert"] == "GREEN"


# --------------------------------------------------------------------------
# The escalation ladder
# --------------------------------------------------------------------------
def test_one_day_of_evidence_buys_only_the_low_tier() -> None:
    receipt = F.derive_offset([_row("2026-08-26", 0.6)], event_date="2026-08-27")
    assert receipt["tier"] == "LOW"
    assert receipt["regime_delta_c"] == pytest.approx(F.LOW_LAMBDA * 0.6)


def test_a_three_day_run_escalates_and_two_days_do_not() -> None:
    two = F.derive_offset(
        [_row("2026-08-25", 0.5), _row("2026-08-26", 0.6)],
        event_date="2026-08-27",
    )
    three = F.derive_offset(
        [_row("2026-08-24", 0.4), _row("2026-08-25", 0.5),
         _row("2026-08-26", 0.6)],
        event_date="2026-08-27",
    )
    assert (two["tier"], two["run_length"]) == ("LOW", 2)
    assert (three["tier"], three["run_length"]) == ("HIGH", 3)
    assert three["regime_delta_c"] > two["regime_delta_c"]


def test_a_sign_flip_breaks_the_run_so_a_reversal_is_met_at_low_strength() -> None:
    # This is the 2026-08-17 case: two cold days, then the regime turns hot.
    # The ladder must not arrive at the flip already escalated.
    receipt = F.derive_offset(
        [_row("2026-08-24", -0.5), _row("2026-08-25", -0.6),
         _row("2026-08-26", 0.4)],
        event_date="2026-08-27",
    )
    assert receipt["run_length"] == 1
    assert receipt["tier"] == "LOW"


def test_a_calendar_gap_breaks_the_run() -> None:
    receipt = F.derive_offset(
        [_row("2026-08-22", 0.5), _row("2026-08-25", 0.5),
         _row("2026-08-26", 0.6)],
        event_date="2026-08-27",
    )
    assert receipt["run_length"] == 2
    assert receipt["tier"] == "LOW"


def test_a_quiet_day_cannot_promote_the_tier_but_still_corrects() -> None:
    quiet = F.ARM_THRESHOLD_C / 2.0
    receipt = F.derive_offset(
        [_row("2026-08-24", quiet), _row("2026-08-25", quiet),
         _row("2026-08-26", quiet)],
        event_date="2026-08-27",
    )
    assert receipt["run_length"] == 0
    assert receipt["tier"] == "LOW"
    assert receipt["regime_delta_c"] == pytest.approx(F.LOW_LAMBDA * quiet)


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------
@pytest.mark.parametrize("residual", [3.0, -3.0, 12.5])
def test_the_cap_binds_however_extreme_the_day(residual: float) -> None:
    receipt = F.derive_offset(
        [_row("2026-08-24", residual), _row("2026-08-25", residual),
         _row("2026-08-26", residual)],
        event_date="2026-08-27",
    )
    assert abs(receipt["regime_delta_c"]) <= F.HIGH_CAP_C
    assert abs(receipt["regime_delta_c"]) <= MAX_ABS_REGIME_SHIFT_C


def test_no_derivable_offset_can_breach_the_entitlement_the_composer_checks() -> None:
    # The caps are what actually bounds the served shift; this is the wall
    # behind them.  If a cap is ever raised past it, the failure would be a
    # ValueError deep in the serving path rather than a stood-down day.
    assert F.HIGH_CAP_C <= MAX_ABS_REGIME_SHIFT_C
    assert F.LOW_CAP_C <= MAX_ABS_REGIME_SHIFT_C


# --------------------------------------------------------------------------
# Composition with the fitted layer
# --------------------------------------------------------------------------
_KEYS = ["28C", "29C", "30C", "31C", "32C"]
_CENTERS = [28.0, 29.0, 30.0, 31.0, 32.0]
_PROB = {"28C": 0.10, "29C": 0.20, "30C": 0.40, "31C": 0.20, "32C": 0.10}


def _mean(dist) -> float:
    return sum(dist[key] * center for key, center in zip(_KEYS, _CENTERS))


def test_a_warm_shift_moves_the_distribution_warm() -> None:
    shifted, receipt = apply_center_width_calibration(
        _PROB, delta_c=0.0, width_lambda=1.0,
        ordered_keys=_KEYS, key_centers_c=_CENTERS, regime_delta_c=0.5,
    )
    assert _mean(shifted) > _mean(_PROB)
    assert receipt["regime_delta_c"] == 0.5
    assert receipt["fitted_delta_c"] == 0.0


def test_the_two_shifts_add_and_stay_separable_in_the_receipt() -> None:
    _both, receipt = apply_center_width_calibration(
        _PROB, delta_c=-0.2, width_lambda=1.0,
        ordered_keys=_KEYS, key_centers_c=_CENTERS, regime_delta_c=0.5,
    )
    assert receipt["delta_c"] == pytest.approx(0.3)
    assert receipt["fitted_delta_c"] == pytest.approx(-0.2)
    assert receipt["regime_delta_c"] == pytest.approx(0.5)


def test_zero_regime_shift_is_byte_identical_to_not_passing_one() -> None:
    without, receipt_a = apply_center_width_calibration(
        _PROB, delta_c=0.3, width_lambda=0.9,
        ordered_keys=_KEYS, key_centers_c=_CENTERS,
    )
    with_zero, receipt_b = apply_center_width_calibration(
        _PROB, delta_c=0.3, width_lambda=0.9,
        ordered_keys=_KEYS, key_centers_c=_CENTERS, regime_delta_c=0.0,
    )
    assert without == with_zero
    assert receipt_a["delta_c"] == receipt_b["delta_c"]


def test_a_shift_past_the_entitlement_is_refused_not_clamped() -> None:
    with pytest.raises(ValueError, match="regime_shift_out_of_range"):
        apply_center_width_calibration(
            _PROB, delta_c=0.0, width_lambda=1.0,
            ordered_keys=_KEYS, key_centers_c=_CENTERS,
            regime_delta_c=MAX_ABS_REGIME_SHIFT_C + 0.1,
        )


def test_each_layer_spends_only_its_own_entitlement() -> None:
    # Composed past either layer's individual bound is legitimate; neither
    # layer borrowing the other's is not.
    apply_center_width_calibration(
        _PROB, delta_c=1.4, width_lambda=1.0,
        ordered_keys=_KEYS, key_centers_c=_CENTERS, regime_delta_c=0.9,
    )
    with pytest.raises(ValueError):
        apply_center_width_calibration(
            _PROB, delta_c=0.0, width_lambda=1.0,
            ordered_keys=_KEYS, key_centers_c=_CENTERS, regime_delta_c=2.3,
        )


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def test_a_revised_day_restates_itself_and_never_appends(conn) -> None:
    for residual, seen in ((0.7, "2026-08-24T02:00:00Z"),
                           (0.1, "2026-08-25T09:00:00Z")):
        F.upsert_daily_residual(
            conn, stage="P0", event_date="2026-08-23",
            residual_c_mean=residual, station_count=42, cell_count=126,
            truth_visible_at_utc=seen, now_utc=seen,
        )
    rows = conn.execute(
        "SELECT residual_c_mean, truth_visible_at_utc"
        " FROM calibration_fleet_regime_daily WHERE stage='P0'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == pytest.approx(0.1)


def test_each_stage_keeps_its_own_book(conn) -> None:
    # Measured 2026-08-27 on production: over 2026-08-19..22 P0 ran +0.46 to
    # +0.63 while P1 ran -0.42 to -0.06, because P1's hard bound has already
    # pushed it up.  Pooling the stages would cancel a real signal on both.
    F.upsert_daily_residual(
        conn, stage="P0", event_date="2026-08-26", residual_c_mean=0.6,
        station_count=40, cell_count=120,
        truth_visible_at_utc="2026-08-26T23:00:00Z",
        now_utc="2026-08-27T00:00:00Z",
    )
    F.upsert_daily_residual(
        conn, stage="P1", event_date="2026-08-26", residual_c_mean=-0.5,
        station_count=40, cell_count=120,
        truth_visible_at_utc="2026-08-26T23:00:00Z",
        now_utc="2026-08-27T00:00:00Z",
    )
    p0 = F.load_regime_shift(
        conn, stage="P0", event_date="2026-08-27",
        as_of_utc="2026-08-27T00:00:00Z",
    )
    p1 = F.load_regime_shift(
        conn, stage="P1", event_date="2026-08-27",
        as_of_utc="2026-08-27T00:00:00Z",
    )
    # The signals stay opposite, which is the point: one number for both
    # stages would have cancelled a real effect on each.  P1's is withheld
    # rather than served, so the separation is asserted on what each stage
    # derived, not on what it was allowed to do with it.
    assert p0["signal_c"] > 0.0
    assert p1["signal_c"] < 0.0
    # Asserted on what each stage derived rather than on what it was allowed
    # to serve: the separation is a property of the measurement, and it has
    # to keep holding while every stage is dark.
    def derived(receipt):
        return (
            receipt["regime_delta_c"] if receipt.get("withheld_delta_c") is None
            else receipt["withheld_delta_c"]
        )

    assert derived(p0) > 0.0
    assert derived(p1) < 0.0


def test_the_loader_creates_nothing_on_a_database_without_the_table() -> None:
    # The serving path calls this from inside the resident's write
    # transaction; a lazy CREATE and its commit would end that transaction
    # early.  A missing table is identity, not an error and not a schema.
    bare = sqlite3.connect(":memory:")
    try:
        receipt = F.load_regime_shift(
            bare, stage="P0", event_date="2026-08-27",
            as_of_utc="2026-08-27T00:00:00Z",
        )
        assert receipt["regime_delta_c"] == 0.0
        tables = bare.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert tables == []
    finally:
        bare.close()


# --------------------------------------------------------------------------
# The residual itself
# --------------------------------------------------------------------------
def test_the_residual_is_truth_minus_the_distribution_mean_in_celsius() -> None:
    cell = {
        "station_id": "ZBAA",
        "event_date": "2026-08-26",
        "probabilities": dict(_PROB),
        "ordered_keys": list(_KEYS),
        "key_centers_c": list(_CENTERS),
    }
    assert F.cell_residual_c(cell, 31.0) == pytest.approx(31.0 - _mean(_PROB))
    assert F.cell_residual_c(cell, 29.0) == pytest.approx(29.0 - _mean(_PROB))


def test_a_busy_station_does_not_outvote_a_quiet_one() -> None:
    def cell(station: str, probs: dict) -> dict:
        return {
            "station_id": station,
            "event_date": "2026-08-26",
            "probabilities": probs,
            "ordered_keys": list(_KEYS),
            "key_centers_c": list(_CENTERS),
        }

    hot = {"28C": 0.0, "29C": 0.0, "30C": 0.0, "31C": 0.0, "32C": 1.0}
    cold = {"28C": 1.0, "29C": 0.0, "30C": 0.0, "31C": 0.0, "32C": 0.0}
    cells = [cell("AAAA", hot)] * 9 + [cell("BBBB", cold)]
    truth = {("AAAA", "2026-08-26"): 32.0, ("BBBB", "2026-08-26"): 30.0}
    summary = F.summarise_day(cells, truth)
    assert summary["station_count"] == 2
    # Station-first: AAAA contributes 0.0 once, BBBB contributes +2.0 once.
    assert summary["residual_c_mean"] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# Which stages actually serve it
# --------------------------------------------------------------------------
def test_a_dark_stage_measures_and_alerts_but_serves_nothing(conn) -> None:
    # P1's measured series alternates sign almost daily, so reacting to
    # yesterday made it worse (+7% closed-loop over 2026-08-17..26).  It is
    # still recorded and still lights the alert; it just does not correct.
    assert "P1" not in F.FLEET_REGIME_SERVED_STAGES
    F.upsert_daily_residual(
        conn, stage="P1", event_date="2026-08-26", residual_c_mean=-0.9,
        station_count=40, cell_count=120,
        truth_visible_at_utc="2026-08-26T23:00:00Z",
        now_utc="2026-08-27T00:00:00Z",
    )
    receipt = F.load_regime_shift(
        conn, stage="P1", event_date="2026-08-27",
        as_of_utc="2026-08-27T00:00:00Z",
    )
    assert receipt["regime_delta_c"] == 0.0
    assert receipt["state"] == "NOT_SERVED_FOR_STAGE"
    # The number it would have served is kept, so the dark stage stays
    # auditable against the day it might earn its way in.
    assert receipt["withheld_delta_c"] == pytest.approx(
        F.LOW_LAMBDA * -0.9
    )
    assert receipt["alert"] == "RED"


def test_every_stage_is_dark_until_one_is_shown_to_help_the_claim(
    conn,
) -> None:
    # The ledger replay measured the served arms as costing 7.3 points of
    # return for no coverage, so nothing is served today.  This asserts the
    # gate rather than the emptiness: whatever the tuple holds, a stage in it
    # serves its shift and a stage outside it withholds one.
    F.upsert_daily_residual(
        conn, stage="P0", event_date="2026-08-26", residual_c_mean=0.9,
        station_count=40, cell_count=120,
        truth_visible_at_utc="2026-08-26T23:00:00Z",
        now_utc="2026-08-27T00:00:00Z",
    )
    receipt = F.load_regime_shift(
        conn, stage="P0", event_date="2026-08-27",
        as_of_utc="2026-08-27T00:00:00Z",
    )
    if "P0" in F.FLEET_REGIME_SERVED_STAGES:
        assert receipt["state"] == "APPLIED"
        assert receipt["regime_delta_c"] > 0.0
        assert "withheld_delta_c" not in receipt
    else:
        assert receipt["state"] == "NOT_SERVED_FOR_STAGE"
        assert receipt["regime_delta_c"] == 0.0
        assert receipt["withheld_delta_c"] > 0.0


def test_every_served_stage_is_a_known_stage() -> None:
    for stage in F.FLEET_REGIME_SERVED_STAGES:
        assert stage in F.FLEET_REGIME_STAGES
