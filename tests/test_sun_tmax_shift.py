"""The board's morning-sunshine -> maximum reading (user 2026-10-13 「前端显式：
上午日照偏差 → 最高温相对预报的偏差，显示度数」): the fitted two-slope line in
config/ui/sun_tmax_shift_v1.json, applied on the day-ahead forecast basis."""
from __future__ import annotations

import json

from bayes_basket_target import station_day_board as board


def _rows(sat: float, fc_d1: float, hours=range(6, 13)) -> tuple[list[dict], list[dict]]:
    rows = [{"hour": h, "sat_sw_wm2": sat if h in hours else None,
             "forecast": {"sw": 999.0, "sw_d1": fc_d1, "temp_c_d1": 20.0 + h / 10}} for h in range(24)]
    return rows, [r for r in rows if r["sat_sw_wm2"] is not None]


def test_the_committed_fit_has_a_line_for_every_display_hour() -> None:
    fit = json.loads(board.SUN_TMAX_SHIFT_PATH.read_text(encoding="utf-8"))
    assert fit["schema"] == "sun_tmax_shift_v1"
    for hour, line in fit["by_last_hour"].items():
        assert line["dark_c_per_10pct"] > 0 and line["bright_c_per_10pct"] > 0, hour
        assert line["dark_ci95"][0] <= line["dark_c_per_10pct"] <= line["dark_ci95"][1]


def test_brighter_than_the_day_ahead_forecast_warms_darker_cools_more() -> None:
    rows, measured = _rows(sat=605.0, fc_d1=500.0)          # +21% to 12:00
    up = board._sun_tmax_shift(rows, measured)
    assert up["state"] == "ok" and up["last_hour"] == 12 and up["pct_d1"] == 21.0
    assert 0.1 < up["shift_c"] < 0.6 and up["range_c"][0] <= up["shift_c"] <= up["range_c"][1]
    assert up["forecast_tmax_d1_c"] == 22.3                   # the day-ahead basis, not "sw"
    rows, measured = _rows(sat=395.0, fc_d1=500.0)          # -21%
    down = board._sun_tmax_shift(rows, measured)
    assert down["shift_c"] < 0 and abs(down["shift_c"]) > abs(up["shift_c"])


def test_too_early_little_sun_and_no_satellite() -> None:
    rows, measured = _rows(sat=300.0, fc_d1=300.0, hours=range(6, 8))
    assert board._sun_tmax_shift(rows, measured)["state"] == "too_early"
    rows, measured = _rows(sat=30.0, fc_d1=20.0)
    assert board._sun_tmax_shift(rows, measured)["state"] == "little_sun"
    assert board._sun_tmax_shift(rows, []) is None
