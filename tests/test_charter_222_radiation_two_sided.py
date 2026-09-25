"""Charter 222: P0's radiation coefficient has a warm side and a cold side.

User ruling 2026-09-25, 「把辐射系数拆成升温、降温两侧」.  A day forecast
clearer than the station's own last 180 days (Kt z > 0) is pushed warm at the
warm side's coefficient, a cloudier one (z < 0) cold at the cold side's.  The
clearness index has a ceiling and a long overcast tail, so the one symmetric
coefficient served its largest shifts on the cold side -- the side the
residual does not follow (warm 0.124 [0.099, 0.148], cold 0.045 [0.024,
0.069] on charter 210's objective).

The pair is centred on the station's own history: z averages zero there, but
a pair bends at zero, so uncentred it would move every station's mean warm
(z > 0 on 59% of days) -- bias, which charter 160 keeps out of this layer.

Five things have to hold: the shift picks its side by the sign of z and says
so on the receipt; it moves the station's own days by nothing on average;
the holdout scores each day with the same centred pair serving will use; the
artifact pins both sides and serving reads the pin back as that pair; and
serving itself applies the fleet pair to every artifact (charter 210).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest

from bayes_basket_target import native_ensemble_p0 as p0
from bayes_basket_target.cross_consensus import (
    RADIATION_CLEARNESS_METHOD_TAG,
    RADIATION_SHIFT_SIGMA_COLD,
    RADIATION_SHIFT_SIGMA_WARM,
    RADIATION_SIDE_COLD,
    RADIATION_SIDE_NEUTRAL,
    RADIATION_SIDE_WARM,
    RADIATION_TWO_SIDED_METHOD_TAG,
    extraterrestrial_radiation_mj,
    forecast_clearness_shift_sigma,
    forecast_radiation_shift_sigma,
    pinned_radiation_shift_sigmas,
    radiation_side,
    radiation_shift_sigmas_for_lead,
    served_radiation_shift_sigmas,
)

FLEET = (RADIATION_SHIFT_SIGMA_WARM, RADIATION_SHIFT_SIGMA_COLD)
HISTORY = [20.0 + (index % 11) - 5 for index in range(60)]


def test_the_fleet_pair_is_the_fitted_one_rounded() -> None:
    assert FLEET == (0.12, 0.04)
    for lead in ("h00_12", "h12_24", "h24_48", "h48_plus", None):
        assert radiation_shift_sigmas_for_lead(lead) == FLEET
        assert served_radiation_shift_sigmas(str(lead)) == FLEET


def test_the_side_is_the_sign_of_z() -> None:
    assert radiation_side(0.8) == RADIATION_SIDE_WARM
    assert radiation_side(-0.8) == RADIATION_SIDE_COLD
    assert radiation_side(0.0) == RADIATION_SIDE_NEUTRAL


@pytest.mark.parametrize(
    "radiation_sum, side, coefficient",
    [(26.0, RADIATION_SIDE_WARM, 0.12), (14.0, RADIATION_SIDE_COLD, 0.04)],
)
def test_a_day_is_shifted_at_its_own_side(radiation_sum, side, coefficient) -> None:
    shift, receipt = forecast_radiation_shift_sigma(
        radiation_sum=radiation_sum, station_history=HISTORY,
        shift_sigma_per_unit=0.12, cold_shift_sigma_per_unit=0.04,
    )
    assert receipt["radiation_side"] == side
    assert receipt["shift_sigma_per_unit"] == coefficient
    assert receipt["shift_sigma_per_unit_warm"] == 0.12
    assert receipt["shift_sigma_per_unit_cold"] == 0.04
    # The history's usual day is itself a little clear, so the centre is
    # positive and every day gives it back.
    assert receipt["side_centre_sigma"] > 0.0
    assert shift == pytest.approx(
        coefficient * receipt["standardised"] - receipt["side_centre_sigma"], abs=1e-12
    )
    assert receipt["shift_sigma"] == shift


def _shift(radiation_sum: float) -> float:
    shift, _ = forecast_radiation_shift_sigma(
        radiation_sum=radiation_sum, station_history=HISTORY,
        shift_sigma_per_unit=0.12, cold_shift_sigma_per_unit=0.04,
    )
    return shift


def test_equal_distance_either_side_moves_three_times_as_far_warm() -> None:
    """The asymmetry the ruling serves, in one line: the same |z| is worth
    0.12 sigma per unit clear and 0.04 per unit cloudy.  Measured from the
    history's mean day, so the centre (the same for all three) cancels."""

    mean = sum(HISTORY) / len(HISTORY)
    warm, middle, cold = _shift(mean + 4.0), _shift(mean), _shift(mean - 4.0)
    assert warm > 0.0 > cold
    assert (warm - middle) / (middle - cold) == pytest.approx(3.0, rel=1e-9)


def test_the_station_s_own_days_move_by_nothing_on_average() -> None:
    """Centred: over the history it is scored against the term averages
    zero, as the symmetric coefficient does by construction.  Uncentred it
    would average (0.12 - 0.04) x mean(z+) and move the station's mean."""

    shifts = [_shift(value) for value in HISTORY]
    assert sum(shifts) / len(shifts) == pytest.approx(0.0, abs=1e-12)
    uncentred = [
        shift + forecast_radiation_shift_sigma(
            radiation_sum=value, station_history=HISTORY,
            shift_sigma_per_unit=0.12, cold_shift_sigma_per_unit=0.04,
        )[1]["side_centre_sigma"]
        for shift, value in zip(shifts, HISTORY)
    ]
    assert sum(uncentred) / len(uncentred) > 0.01
    mean = sum(HISTORY) / len(HISTORY)
    _, receipt = forecast_radiation_shift_sigma(
        radiation_sum=mean, station_history=HISTORY,
        shift_sigma_per_unit=0.12, cold_shift_sigma_per_unit=0.04,
    )
    assert receipt["radiation_side"] == RADIATION_SIDE_NEUTRAL


def test_one_coefficient_is_the_same_on_both_sides() -> None:
    """How a caller standardises (a unit coefficient), and how every pin
    written before this charter reads."""

    for radiation_sum in (26.0, 14.0):
        shift, receipt = forecast_radiation_shift_sigma(
            radiation_sum=radiation_sum, station_history=HISTORY,
            shift_sigma_per_unit=1.0,
        )
        assert shift == pytest.approx(receipt["standardised"], abs=1e-12)
        assert receipt["shift_sigma_per_unit_warm"] == receipt["shift_sigma_per_unit_cold"] == 1.0
        # Exactly zero, not a float residue: a symmetric shift stays
        # bit-identical to the one served before this charter.
        assert receipt["side_centre_sigma"] == 0.0
        assert shift == 1.0 * receipt["standardised"]


def _clear_history(latitude: float, event_date: str, kt: float = 0.5) -> list[tuple[str, float]]:
    """Sixty days before ``event_date`` whose clearness alternates around ``kt``."""

    event = date.fromisoformat(event_date)
    rows = []
    for i in range(1, 61):
        day = (event - timedelta(days=i)).isoformat()
        rows.append((day, (kt + (0.1 if i % 2 else -0.1)) * extraterrestrial_radiation_mj(latitude, day)))
    return rows


@pytest.mark.parametrize("kt, side", [(0.65, RADIATION_SIDE_WARM), (0.35, RADIATION_SIDE_COLD)])
def test_the_clearness_form_picks_its_side_the_same_way(kt, side) -> None:
    today = kt * extraterrestrial_radiation_mj(30.6, "2026-09-20")
    shift, receipt = forecast_clearness_shift_sigma(
        radiation_sum=today, event_date="2026-09-20", latitude_deg=30.6,
        station_history=_clear_history(30.6, "2026-09-20"),
        shift_sigma_per_unit=0.12, cold_shift_sigma_per_unit=0.04,
    )
    assert receipt["state"] == "RESOLVED"
    assert receipt["method_tag"] == RADIATION_CLEARNESS_METHOD_TAG
    assert receipt["radiation_side"] == side
    coefficient = 0.12 if side == RADIATION_SIDE_WARM else 0.04
    assert shift == pytest.approx(
        coefficient * receipt["standardised"] - receipt["side_centre_sigma"], abs=1e-12
    )


@pytest.mark.parametrize("day_value", [19.0, 9.0])
def test_the_holdout_scores_each_day_with_the_served_centred_pair(tmp_path, day_value) -> None:
    """Centred, the shift is linear in the pair: shift(w, c) = w a + c b,
    with a = shift(1, 0), b = shift(0, 1) and a + b = shift(1, 1) = z.  So
    the holdout serves the centred pair exactly when those hold -- and the
    side the day is not on still enters, through the centre alone."""

    from bayes_basket_target import native_ensemble_oos as OOS
    from tests.test_radiation_asof import _holdout_db, _predictions

    conn = _holdout_db(tmp_path, day_value=day_value, day_captured="2026-09-17T18:00:00Z")
    predictions = _predictions({"NOAA_GEFS_05": 25.0, "ECMWF_IFS_ENS_OPEN_PF50": 25.0})

    def shift(pair):
        return OOS._holdout_conditioning_signals(
            conn, station_id="ZUUU", holdout_days=["2026-09-19"],
            predictions_by_system=predictions, radiation_shift_sigmas=pair,
        )["2026-09-19"]["synoptic_cooling_shift_sigma"]

    z = shift((1.0, 1.0))
    a, b = shift((1.0, 0.0)), shift((0.0, 1.0))
    assert a + b == pytest.approx(z, abs=1e-12)
    served = shift(FLEET)
    assert served == pytest.approx(0.12 * a + 0.04 * b, abs=1e-12)
    # 19.0 is brighter than the fixture's 12-15 history, 9.0 darker.
    if day_value > 15.0:
        assert z > 0.0 and served > 0.0 and b > 0.0
    else:
        assert z < 0.0 and served < 0.0 and a < 0.0


@pytest.fixture
def _fixture_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mixed-cycle fixture's own premises (tests/test_native_ensemble_p0)."""

    from bayes_basket_target import native_ensemble_oos
    from bayes_basket_target import station_season

    monkeypatch.setattr(native_ensemble_oos, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setattr(p0, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setitem(station_season._SEASON_ANCHOR_MONTH, "TEST", 7)


SEEN: dict[str, str] = {}


def _compile(monkeypatch: pytest.MonkeyPatch, kt_today: float | None):
    """The real OOS build, persist and P0 compile, with the radiation readers
    answering for a station at 30.6N: sixty days around Kt 0.5 and today's
    Kt as given (None: no radiation for the day)."""

    from tests.test_native_ensemble_p0 import _install_mixed_cycle_calibrations, _mixed_packet

    def radiation_sum(conn, *, station_id, event_date):
        SEEN["event_date"] = event_date
        if kt_today is None:
            return None
        return kt_today * extraterrestrial_radiation_mj(30.6, event_date)

    monkeypatch.setattr(p0, "_forecast_radiation_sum", radiation_sum)
    monkeypatch.setattr(
        p0, "_forecast_radiation_history_dated",
        lambda conn, *, station_id, event_date: _clear_history(30.6, event_date),
    )
    monkeypatch.setattr(
        p0, "_forecast_radiation_history",
        lambda conn, *, station_id, event_date: [v for _d, v in _clear_history(30.6, event_date)],
    )
    monkeypatch.setattr(p0, "station_coordinates", lambda station_id: {"latitude": 30.6})
    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    compiled = p0.compile_native_ensemble_p0(
        conn, forward_packet=_mixed_packet(), decision_at_utc="2026-07-01T13:00:00Z",
    )
    return conn, compiled["packet"]["native_probability_envelope"]


@pytest.mark.usefixtures("_fixture_scope")
def test_the_artifact_pins_both_sides_and_serving_applies_its_side(monkeypatch) -> None:
    conn, clear = _compile(monkeypatch, 0.65)
    (method,) = conn.execute(
        "SELECT combination_method FROM calibration_cross_native_ensemble_artifact"
    ).fetchone()
    assert "::radiation_shift_sigma_warm=0.12::radiation_shift_sigma_cold=0.04::" in method
    assert RADIATION_TWO_SIDED_METHOD_TAG in method.split("::")
    assert pinned_radiation_shift_sigmas(method) == FLEET

    _, cloudy = _compile(monkeypatch, 0.35)
    _, none = _compile(monkeypatch, None)
    receipts = {
        name: json.loads(envelope["lineage"]["synoptic_cooling_json"])
        for name, envelope in (("clear", clear), ("cloudy", cloudy), ("none", none))
    }
    for name, side, coefficient, kt in (
        ("clear", RADIATION_SIDE_WARM, 0.12, 0.65), ("cloudy", RADIATION_SIDE_COLD, 0.04, 0.35),
    ):
        receipt = receipts[name]
        assert receipt["state"] == "RESOLVED"
        assert receipt["method_tag"] == RADIATION_CLEARNESS_METHOD_TAG
        assert receipt["radiation_side"] == side
        assert receipt["shift_sigma_per_unit"] == coefficient
        assert receipt["shift_sigma"] == pytest.approx(
            coefficient * receipt["standardised"] - receipt["side_centre_sigma"], abs=1e-12
        )
        assert receipt["artifact_pinned_sigmas"] == {"warm": 0.12, "cold": 0.04}
        # The shadow arm is the absolute form at the pin, sided the same way.
        # The shadow arm is the absolute form at the pin, centred the same
        # way: recomputed here from the same history the reader answered.
        event_date = SEEN["event_date"]
        expected, expected_receipt = forecast_radiation_shift_sigma(
            radiation_sum=kt * extraterrestrial_radiation_mj(30.6, event_date),
            station_history=[v for _d, v in _clear_history(30.6, event_date)],
            shift_sigma_per_unit=0.12, cold_shift_sigma_per_unit=0.04,
        )
        legacy = receipt["legacy_v1"]
        assert legacy["state"] == "RESOLVED"
        assert legacy["shift_sigma"] == pytest.approx(expected, abs=1e-12)
        assert legacy["side_centre_sigma"] == pytest.approx(
            expected_receipt["side_centre_sigma"], abs=1e-12
        )
    assert receipts["none"].get("shift_c") is None

    # The same pooled distribution under both, so net of the centre the
    # served shift per unit of z is the side coefficient: three to one.
    def per_z(receipt):
        spread = receipt["shift_c"] / receipt["shift_sigma"]
        return (receipt["shift_c"] + receipt["side_centre_sigma"] * spread) / receipt["standardised"]

    assert per_z(receipts["clear"]) / per_z(receipts["cloudy"]) == pytest.approx(3.0, rel=1e-9)
    assert receipts["clear"]["side_centre_sigma"] == pytest.approx(
        receipts["cloudy"]["side_centre_sigma"], abs=1e-12
    )

    def mean(envelope):
        intervals = json.loads(envelope["lineage"]["bucket_intervals_c_json"])
        centers = {k: (float(v[0]) + float(v[1])) / 2.0 for k, v in intervals.items()
                   if v[0] is not None and v[1] is not None}
        mass = {k: float(p) for k, p in envelope["probabilities"].items() if k in centers}
        return sum(centers[k] * p for k, p in mass.items()) / sum(mass.values())

    assert mean(clear) > mean(none) > mean(cloudy)


def test_serving_reads_the_fleet_pair_whatever_the_pin_says() -> None:
    """Charter 210's rule is unchanged: the pin is recorded, not served."""

    for method in (
        "x::radiation_shift_sigma=0.07::" + RADIATION_CLEARNESS_METHOD_TAG,
        "x::radiation_shift_sigma_warm=0.3::radiation_shift_sigma_cold=0.01::"
        + RADIATION_TWO_SIDED_METHOD_TAG,
        "",
    ):
        assert served_radiation_shift_sigmas(method) == FLEET
