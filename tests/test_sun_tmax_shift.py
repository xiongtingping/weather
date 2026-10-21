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


def test_cloud_shift_reads_the_cards_own_basis_and_carries_the_out_of_sample_verdict() -> None:
    """2026-10-17 「保持口径一致」: the report's cover against the latest
    forecast's low cloud -- the card's headline basis -- mean over 05..H in
    tenths; the line's out-of-sample verdict rides along.  Since 2026-10-19
    (「报文云只算云底 ≤ 3 km」→「重新拟合」) the report's LOW layers, not its
    thickest layer at any height."""

    rows = [{"hour": h, "past": h <= 11, "forecast": {"cloud_low": 40.0, "cloud_d1": 90.0},
             "sat_cloud": {"state": "ok", "cover": 0.3}, "obs": {"cloud_cover": 1.0, "cloud_cover_low": 0.9}} for h in range(24)]
    out = board._cloud_tmax_shift(rows)
    assert out["state"] == "ok" and out["source"] == "report" and out["tenths"] == 5.0
    assert out["shift_c"] < out["range_c"][1] + 1e-9 and isinstance(out["out_of_sample_significant"], bool)


def test_matching_the_forecast_reads_zero() -> None:
    """2026-10-17 「可以」: ΔT is the observation's own part, slope x
    difference; the lines' intercepts (basis offsets, not weather) are left
    out, so a morning exactly as forecast reads 0."""

    rows, measured = _rows(sat=500.0, fc_d1=500.0)
    assert board._sun_tmax_shift(rows, measured)["shift_c"] == 0.0
    cloud = [{"hour": h, "past": h <= 11, "forecast": {"cloud_low": 50.0}, "obs": {"cloud_cover": 0.9, "cloud_cover_low": 0.5}} for h in range(24)]
    assert board._cloud_tmax_shift(cloud)["shift_c"] == 0.0


def test_dew_shift_reads_the_newest_hour_and_drier_reads_warmer() -> None:
    """2026-10-19 「露点 · 实况对比预报：这个也拟合」: the card's basis, the newest
    hour's report dew point against the latest forecast's; a drier hour than
    forecast reads warmer, matching reads zero, before 09 h it is too early."""

    def rows(last, obs_dew, fc_dew=15.0):
        return [{"hour": h, "past": h <= last, "forecast": {"dew_c": fc_dew},
                 "obs": {"dew_c": obs_dew} if h <= last else None} for h in range(24)]
    dry = board._dew_tmax_shift(rows(13, 13.0))
    assert dry["state"] == "ok" and dry["hour"] == 13 and dry["diff_c"] == -2.0 and dry["shift_c"] > 0
    assert dry["range_c"][0] <= dry["shift_c"] <= dry["range_c"][1] and isinstance(dry["out_of_sample_significant"], bool)
    assert board._dew_tmax_shift(rows(13, 15.0))["shift_c"] == 0.0
    assert board._dew_tmax_shift(rows(7, 12.0)) == {"state": "too_early", "from_hour": 9}
    assert board._dew_tmax_shift(rows(20, 13.0))["line_hour"] == 17


def test_wind_shift_reads_more_wind_than_forecast_as_warmer() -> None:
    """2026-10-19 「风 · 实况对比预报：这个也拟合一下」: the newest hour's report
    wind against the latest forecast's; windier reads warmer, calmer cooler,
    matching reads zero."""

    def rows(last, obs, fc=3.0):
        return [{"hour": h, "past": h <= last, "forecast": {"wind_mps": fc},
                 "obs": {"wind_mps": obs} if h <= last else None} for h in range(24)]
    windy, calm = board._wind_tmax_shift(rows(15, 5.0)), board._wind_tmax_shift(rows(15, 1.0))
    assert windy["state"] == calm["state"] == "ok" and windy["diff"] == 2.0 and calm["diff"] == -2.0
    # The calmer side's slope interval spans zero at 15 h, so it reads 0 (熊先生 2026-10-20 「1」).
    assert windy["shift_c"] > 0 == calm["shift_c"]
    assert board._wind_tmax_shift(rows(15, 3.0))["shift_c"] == 0.0
    assert board._wind_tmax_shift(rows(8, 5.0))["state"] == "too_early"


def test_polar_wind_component_is_positive_from_the_pole_in_either_hemisphere() -> None:
    """2026-10-19 「北风好像才降温？」→ 南北分量: northerly in the north and
    southerly in the south positive; calm 0; a variable direction with wind None."""

    assert board.polar_wind_component(0, 5.0, 30.6) == 5.0            # northerly, ZUUU
    assert board.polar_wind_component(180, 5.0, 30.6) == -5.0
    assert board.polar_wind_component(180, 5.0, -34.8) == 5.0         # southerly, SAEZ
    assert abs(board.polar_wind_component(90, 5.0, 30.6)) < 1e-9      # easterly
    assert board.polar_wind_component(None, 0.0, 30.6) == 0.0         # calm
    assert board.polar_wind_component(None, 3.0, 30.6) is None        # VRB


def test_relative_humidity_and_its_card_before_the_line_is_fitted(monkeypatch, tmp_path) -> None:
    """2026-10-19 「卡片增加湿度并拟合」: the report's humidity from its own
    temperature and dew point (Magnus); with no fitted line the card reads the
    plain difference (state unfitted), with one it reads ΔT, drier warmer."""

    assert board.relative_humidity(15, 15) == 100.0 and board.relative_humidity(21, 15) == 68.6
    assert board.relative_humidity(None, 15) is None
    rows = [{"hour": h, "past": h <= 14, "forecast": {"rh_pct": 70.0},
             "obs": {"rh_pct": 50.0} if h <= 14 else None} for h in range(24)]
    monkeypatch.setattr(board, "RH_TMAX_SHIFT_PATH", tmp_path / "missing.json")
    assert board._rh_tmax_shift(rows) == {"state": "unfitted"}
    line = {"drier_c_per_pp": -0.02, "drier_ci95": [-0.03, -0.01], "moister_c_per_pp": 0.0, "moister_ci95": [-0.01, 0.01],
            "cv_vs_mean_only": [-0.01, -0.02, -0.001], "r2": 0.02, "n": 1000}
    (tmp_path / "rh.json").write_text(json.dumps({"x_clip_pp": 20, "by_hour": {str(h): line for h in range(9, 18)}}))
    monkeypatch.setattr(board, "RH_TMAX_SHIFT_PATH", tmp_path / "rh.json")
    out = board._rh_tmax_shift(rows)
    assert out["state"] == "ok" and out["diff"] == -20.0 and out["shift_c"] == 0.4 and out["out_of_sample_significant"]


def test_side_whose_slope_interval_spans_zero_reads_zero() -> None:
    """熊先生 2026-10-20 「1」: low cloud 3% under the forecast had read
    -0.01 °C off a clear-side slope of the wrong sign whose 95% interval spans
    zero; that side now reads 0, a separated side keeps slope x difference."""

    assert board._side_shift(0.106, [-0.013, 0.25], -0.3) == 0.0
    assert board._side_shift(-0.089, [-0.156, -0.03], 2.0) == -0.089 * 2.0

    def rows(obs_low, fc_low):
        return [{"hour": h, "past": h <= 15, "forecast": {"cloud_low": fc_low},
                 "obs": {"cloud_cover_low": obs_low} if h <= 15 else None} for h in range(24)]
    clearer = board._cloud_tmax_shift(rows(0.18, 21.0))
    cloudier = board._cloud_tmax_shift(rows(0.60, 21.0))
    assert clearer["state"] == cloudier["state"] == "ok"
    assert clearer["shift_c"] == 0.0 and cloudier["shift_c"] < 0
