from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.metar_path_state import (
    compile_conditional_metar_path_state,
    condition_remaining_warming_distribution,
    parse_metar_physical_state,
)


def _row(observed: str, temperature: float, raw: str) -> dict[str, object]:
    return {
        "observation_id": observed,
        "observed_at_utc": observed,
        "temperature_c": temperature,
        "raw_text": raw,
    }


def test_physical_parser_extracts_global_metar_units_and_weather() -> None:
    metric = parse_metar_physical_state(
        "METAR LEMD 011200Z 21008G19KT 9999 -RA BKN030 36/02 Q1015",
        expected_temperature_c=36.0,
    )
    assert metric.complete is True
    assert metric.dewpoint_c == 2.0
    assert metric.precipitation_present is True
    assert metric.cloud_cover_fraction == 0.875
    assert metric.wind_speed_mps == pytest.approx(8 * 0.514444)
    assert metric.wind_gust_mps == pytest.approx(19 * 0.514444)
    assert metric.pressure_hpa == 1015.0

    imperial = parse_metar_physical_state(
        "METAR KSEA 011200Z 16009KT 10SM SCT016 16/13 A3008",
        expected_temperature_c=16.0,
    )
    assert imperial.complete is True
    assert imperial.pressure_hpa == pytest.approx(1018.6257, abs=0.01)


def test_path_state_uses_future_time_momentum_physics_and_model_root() -> None:
    rows = [
        _row(
            "2026-08-01T09:00:00Z",
            28.0,
            "METAR RKPK 010900Z 18005KT CAVOK 28/18 Q1008",
        ),
        _row(
            "2026-08-01T10:00:00Z",
            29.0,
            "METAR RKPK 011000Z 19007KT FEW020 29/18 Q1007",
        ),
        _row(
            "2026-08-01T11:00:00Z",
            31.0,
            "METAR RKPK 011100Z 20009G15KT SCT025 31/19 Q1006",
        ),
    ]
    state = compile_conditional_metar_path_state(
        rows,
        as_of_utc="2026-08-01T11:05:00Z",
        settlement_timezone="Asia/Seoul",
        root_probabilities={"31C": 0.2, "32C": 0.5, "33C": 0.3},
        bucket_centers_c={"31C": 31.0, "32C": 32.0, "33C": 33.0},
    )
    assert state.complete is True
    assert state.running_high_c == 31.0
    assert state.momentum_c_per_hour == 2.0
    assert state.acceleration_c_per_hour2 == 1.0
    assert state.root_expected_tmax_c == pytest.approx(32.1)
    assert state.root_expected_remaining_warming_c == pytest.approx(1.1)
    assert state.cloud_cover_fraction == 0.5
    assert state.minutes_remaining_in_settlement_day == pytest.approx(235.0)


def test_path_state_marks_short_or_physically_incomplete_prefix_incomplete() -> None:
    state = compile_conditional_metar_path_state(
        [
            _row(
                "2026-08-01T10:00:00Z",
                29.0,
                "METAR RKPK 011000Z 19007KT 9999 29/XX Q1007",
            ),
            _row(
                "2026-08-01T11:00:00Z",
                30.0,
                "METAR RKPK 011100Z 20009KT 9999 30/XX Q1006",
            ),
        ],
        as_of_utc="2026-08-01T11:05:00Z",
        settlement_timezone="Asia/Seoul",
        root_probabilities={"30C": 0.5, "31C": 0.5},
        bucket_centers_c={"30C": 30.0, "31C": 31.0},
    )
    assert state.complete is False


def test_conditioned_distribution_moves_beyond_current_high_when_path_rises() -> None:
    coefficients = {
        "base_distribution_mean": 0.3,
        "root_expected_remaining_warming": 0.5,
        "momentum_c_per_hour": 0.35,
        "acceleration_c_per_hour2": 0.1,
        "drawdown_from_high_c": -0.5,
        "dewpoint_depression_c": 0.01,
        "cloud_cover_fraction": -0.8,
        "precipitation_present": -1.0,
        "wind_speed_mps": -0.02,
        "remaining_hours": 0.02,
    }
    rising = compile_conditional_metar_path_state(
        [
            _row("2026-08-01T07:00:00Z", 27.0, "METAR RKPK 010700Z 18005KT CAVOK 27/18 Q1008"),
            _row("2026-08-01T08:00:00Z", 28.0, "METAR RKPK 010800Z 18005KT CAVOK 28/18 Q1008"),
            _row("2026-08-01T09:00:00Z", 30.0, "METAR RKPK 010900Z 18005KT CAVOK 30/18 Q1008"),
        ],
        as_of_utc="2026-08-01T09:05:00Z",
        settlement_timezone="Asia/Seoul",
        root_probabilities={"30C": 0.2, "31C": 0.4, "32C": 0.4},
        bucket_centers_c={"30C": 30.0, "31C": 31.0, "32C": 32.0},
    )
    conditioned = condition_remaining_warming_distribution(
        {0.0: 1 / 3, 1.0: 1 / 3, 2.0: 1 / 3},
        rising,
        coefficients=coefficients,
        sigma_c=0.8,
    )
    assert conditioned[2.0] > conditioned[0.0]

    cooling = compile_conditional_metar_path_state(
        [
            _row("2026-08-01T07:00:00Z", 30.0, "METAR RKPK 010700Z 18010KT OVC020 30/23 Q1008"),
            _row("2026-08-01T08:00:00Z", 29.0, "METAR RKPK 010800Z 18010KT OVC020 -RA 29/23 Q1008"),
            _row("2026-08-01T09:00:00Z", 28.0, "METAR RKPK 010900Z 18010KT OVC020 -RA 28/23 Q1008"),
        ],
        as_of_utc="2026-08-01T09:05:00Z",
        settlement_timezone="Asia/Seoul",
        root_probabilities={"30C": 0.6, "31C": 0.3, "32C": 0.1},
        bucket_centers_c={"30C": 30.0, "31C": 31.0, "32C": 32.0},
    )
    cooled = condition_remaining_warming_distribution(
        {0.0: 1 / 3, 1.0: 1 / 3, 2.0: 1 / 3},
        cooling,
        coefficients=coefficients,
        sigma_c=0.8,
    )
    assert cooled[0.0] > cooled[2.0]


def test_physical_parser_fails_closed_on_temperature_mismatch() -> None:
    with pytest.raises(ContractError, match="metar_path_temperature_mismatch"):
        parse_metar_physical_state(
            "METAR RKPK 011200Z 18005KT CAVOK 30/18 Q1008",
            expected_temperature_c=32.0,
        )


def test_a_report_missing_one_group_carries_the_last_stated_vector() -> None:
    """An omitted group is silence about the vector, not a broken path.

    KDAL 2026-09-02: the 19:53Z report was filed without its wind group, the
    COR two minutes later supplied it, and a COR is reference-only by review
    so the P1 reader drops it.  The walk stopped at the windless report,
    ``complete`` went false, and the whole station-day's P1 chain stopped
    there -- P1 froze at a 35.0 high while the station went on to 37.8.
    Fifteen such reports on twelve station-days in the fleet's week from
    2026-08-27, nine missing wind and six the cloud amount.

    The sky does not change report to report any more than it changes
    carrier to carrier, which is the rule this module already applied to
    bodyless rows.  One report supplies the whole vector; present weather
    still comes from the newest parsable body, which is where it read from.
    """

    state = compile_conditional_metar_path_state(
        [
            _row(
                "2026-09-02T17:53:00Z",
                34.4,
                "METAR KDAL 021753Z 09008KT 10SM FEW050 34/20 A2997",
            ),
            _row(
                "2026-09-02T18:53:00Z",
                35.0,
                "METAR KDAL 021853Z VRB03KT 10SM FEW060 35/19 A2994",
            ),
            _row(
                "2026-09-02T19:53:00Z",
                36.7,
                "METAR KDAL 021953Z 10SM -RA FEW060 37/19 A2990",
            ),
        ],
        as_of_utc="2026-09-02T19:55:00Z",
        settlement_timezone="America/Chicago",
        root_probabilities={"36C": 0.4, "37C": 0.6},
        bucket_centers_c={"36C": 36.0, "37C": 37.0},
    )

    assert state.complete is True
    assert state.running_high_c == 36.7
    # The vector is 18:53's, whole and from one report.
    assert state.wind_speed_mps == pytest.approx(3 * 0.514444)
    assert state.cloud_cover_fraction == 0.25
    assert state.pressure_hpa == pytest.approx(1013.9, abs=0.1)
    assert state.dewpoint_depression_c == pytest.approx(36.7 - 19.0)
    # Present weather is the current report's own statement, not the
    # carried one's: 19:53 says -RA and 18:53 said nothing.
    assert state.precipitation_present is True


def test_a_vector_older_than_the_carry_window_is_incomplete_again() -> None:
    state = compile_conditional_metar_path_state(
        [
            _row(
                "2026-09-02T16:00:00Z",
                33.0,
                "METAR KDAL 021600Z 09008KT 10SM FEW050 33/20 A2997",
            ),
            _row(
                "2026-09-02T16:30:00Z",
                34.0,
                "METAR KDAL 021630Z 09008KT 10SM FEW050 34/20 A2997",
            ),
            _row(
                "2026-09-02T19:53:00Z",
                36.7,
                "METAR KDAL 021953Z 10SM FEW060 37/19 A2990",
            ),
        ],
        as_of_utc="2026-09-02T19:55:00Z",
        settlement_timezone="America/Chicago",
        root_probabilities={"36C": 0.4, "37C": 0.6},
        bucket_centers_c={"36C": 36.0, "37C": 37.0},
    )

    assert state.complete is False
