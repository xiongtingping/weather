"""The board's morning-sunshine -> maximum reading (user 2026-10-13 「前端显式：
上午日照偏差 → 最高温相对预报的偏差，显示度数」): the fitted two-slope line in
config/ui/sun_tmax_shift_v2.json, applied on the day-ahead forecast basis.
Since 2026-10-17 (「改用 W/m² 的绝对差重新拟合这条线」) the line reads the mean
W/m² difference over the hours 05..H, with no sunshine gate."""
from __future__ import annotations

import json

from bayes_basket_target import station_day_board as board


def _rows(sat: float, fc_d1: float, hours=range(6, 13)) -> tuple[list[dict], list[dict]]:
    rows = [{"hour": h, "sat_sw_wm2": sat if h in hours else None,
             "forecast": {"sw": 999.0, "sw_d1": fc_d1, "temp_c_d1": 20.0 + h / 10}} for h in range(24)]
    return rows, [r for r in rows if r["sat_sw_wm2"] is not None]


def test_the_committed_fit_has_a_line_for_every_display_hour() -> None:
    fit = json.loads(board.SUN_TMAX_SHIFT_PATH.read_text(encoding="utf-8"))
    assert fit["schema"] == "sun_tmax_shift_v2" and fit["x_clip_wm2"] > 0
    for hour, line in fit["by_last_hour"].items():
        assert line["dark_c_per_100wm2"] > 0 and line["bright_c_per_100wm2"] > 0, hour
        assert line["dark_ci95"][0] <= line["dark_c_per_100wm2"] <= line["dark_ci95"][1]
        # the reading earns its place out of sample against no reading
        cmp = fit["versus_v1"][hour]["all_sample"]
        assert cmp["mse_v2_abs"] < cmp["mse_mean_only"], hour


def test_brighter_than_the_day_ahead_forecast_warms_darker_cools() -> None:
    rows, measured = _rows(sat=600.0, fc_d1=500.0)          # +100 W/m² to 12:00
    up = board._sun_tmax_shift(rows, measured)
    assert up["state"] == "ok" and up["last_hour"] == 12 and up["wm2_d1"] == 100.0
    assert 0.3 < up["shift_c"] < 1.5 and up["range_c"][0] <= up["shift_c"] <= up["range_c"][1]
    assert up["forecast_tmax_d1_c"] == 22.3                   # the day-ahead basis, not "sw"
    rows, measured = _rows(sat=400.0, fc_d1=500.0)          # -100 W/m²
    down = board._sun_tmax_shift(rows, measured)
    assert down["shift_c"] < 0


def test_a_dim_day_ahead_forecast_is_read_not_refused() -> None:
    """2026-10-17 ZUUU: the day-ahead forecast gave the morning ~52 W/m² and
    the satellite ~108; as a percentage that read +106% and was refused."""

    sat = {8: 36.0, 9: 216.0, 10: 394.0}
    fc = {8: 21.0, 9: 58.0, 10: 234.0}
    rows = [{"hour": h, "sat_sw_wm2": sat.get(h, 0.0) if h <= 10 else None,
             "forecast": {"sw": 0.0, "sw_d1": fc.get(h, 0.0), "temp_c_d1": 22.6}} for h in range(24)]
    out = board._sun_tmax_shift(rows, [r for r in rows if r["sat_sw_wm2"] is not None])
    assert out["state"] == "ok" and out["wm2_d1"] == 55.5      # (646 - 313) / 6 hours, 05..10
    assert 0.0 < out["shift_c"] < 1.5 and out["forecast_tmax_d1_c"] == 22.6


def test_too_early_and_no_satellite() -> None:
    rows, measured = _rows(sat=300.0, fc_d1=300.0, hours=range(6, 8))
    early = board._sun_tmax_shift(rows, measured)
    assert early["state"] == "too_early" and early["forecast_tmax_d1_c"] == 22.3
    assert board._sun_tmax_shift(rows, []) is None
