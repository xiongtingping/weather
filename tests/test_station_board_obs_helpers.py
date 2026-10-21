"""Helpers the station board keeps after the per-variable ΔT lines were
retired (2026-10-21): the report's humidity and the wind's polar component."""

from bayes_basket_target import station_day_board as board


def test_polar_wind_component_is_positive_from_the_pole_in_either_hemisphere() -> None:
    """2026-10-19 「北风好像才降温？」→ 南北分量: northerly in the north and
    southerly in the south positive; calm 0; a variable direction with wind None."""

    assert board.polar_wind_component(0, 5.0, 30.6) == 5.0            # northerly, ZUUU
    assert board.polar_wind_component(180, 5.0, 30.6) == -5.0
    assert board.polar_wind_component(180, 5.0, -34.8) == 5.0         # southerly, SAEZ
    assert abs(board.polar_wind_component(90, 5.0, 30.6)) < 1e-9      # easterly
    assert board.polar_wind_component(None, 0.0, 30.6) == 0.0         # calm
    assert board.polar_wind_component(None, 3.0, 30.6) is None        # VRB


def test_relative_humidity_from_the_reports_own_pair() -> None:
    assert board.relative_humidity(15, 15) == 100.0 and board.relative_humidity(21, 15) == 68.6
    assert board.relative_humidity(None, 15) is None
