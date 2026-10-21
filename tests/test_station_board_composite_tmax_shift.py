"""The station board's composite ΔT card (user 2026-10-21): the M3 arm-⑥ line
read on the board's own hourly rows, parts summing to the total."""

from bayes_basket_target import station_day_board as b


def _rows(sat_until: int, report_until: int, warm: float = 2.0) -> list[dict]:
    rows = []
    for h in range(24):
        fc = {"sw_d1": 400.0 if 6 <= h <= 17 else 0.0, "temp_c_d1": 20.0 + (h == 15) * 4.0,
              "dew_c_d1": 10.0, "rh_pct_d1": 50.0, "wind_mps_d1": 3.0}
        obs = None
        if 6 <= h <= report_until:
            obs = {"temp_c": fc["temp_c_d1"] + warm, "dew_c": 9.0, "rh_pct": 45.0, "wind_mps": 3.5, "precip": False}
        rows.append({"hour": h, "forecast": fc, "obs": obs,
                     "sat_sw_wm2": (500.0 if 6 <= h <= 17 else 0.0) if h <= sat_until else None})
    return rows


def test_parts_and_bias_add_up_to_the_total() -> None:
    got = b._composite_tmax_shift(_rows(13, 13), "ZUUU")
    assert got["state"] == "ok" and got["hour"] == 13 and got["station_fit"]
    assert abs(got["station_bias_c"] + sum(got["parts_c"].values()) - got["total_c"]) < 0.05
    assert got["x"]["tdev"] == 2.0 and got["parts_c"]["tdev"] > 0
    assert got["forecast_tmax_d1_c"] == 24.0


def test_reads_the_newest_hour_the_satellite_covers() -> None:
    got = b._composite_tmax_shift(_rows(11, 13), "ZUUU")
    assert got["state"] == "ok" and got["hour"] == 11


def test_too_early_and_waiting() -> None:
    assert b._composite_tmax_shift(_rows(8, 8), "ZUUU")["state"] == "too_early"
    assert b._composite_tmax_shift(_rows(7, 10), "ZUUU")["state"] == "waiting_satellite"


def test_a_station_without_its_own_line_uses_the_fleet() -> None:
    got = b._composite_tmax_shift(_rows(13, 13), "VILK")
    assert got["state"] == "ok" and not got["station_fit"] and got["station_bias_c"] == 0.0


def test_the_series_reads_each_hour_as_of_itself() -> None:
    rows = _rows(13, 13)
    got = b._composite_tmax_shift(rows, "ZUUU")
    assert [p["hour"] for p in got["series"]] == [9, 10, 11, 12, 13]
    assert got["series"][-1]["total_c"] == got["total_c"]
    # Hour 10 must not see the 11..13 reports: making them colder leaves it as it was.
    colder = _rows(13, 13)
    for r in colder:
        if r["hour"] > 10 and r["obs"]:
            r["obs"]["temp_c"] -= 5.0
    again = b._composite_tmax_shift(colder, "ZUUU")
    assert again["series"][1] == got["series"][1]
    assert again["series"][-1]["total_c"] < got["series"][-1]["total_c"]
