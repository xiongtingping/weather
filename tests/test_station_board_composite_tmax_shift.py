"""The station board's composite ΔT card (user 2026-10-21): the M3 arm-⑥ line
read on the board's own hourly rows, parts summing to the total."""

import pytest

from bayes_basket_target import station_day_board as b


@pytest.fixture(autouse=True)
def _committed_fit_only(tmp_path, monkeypatch):
    # The daily refresh's refit under data/runtime must not leak into tests.
    monkeypatch.setattr(b, "COMPOSITE_TMAX_SHIFT_RUNTIME", tmp_path / "absent.json")
    monkeypatch.setattr(b, "COMPOSITE_FORWARD_PATH", tmp_path / "absent_forward.json")


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


def test_no_report_no_reading_and_a_lagging_satellite_stops_at_six() -> None:
    quiet = _rows(13, 5)
    for r in quiet:
        r["obs"] = None
    assert b._composite_tmax_shift(quiet, "ZUUU") is None
    # Reports to 10 h, no satellite hour yet: the night hours (to 6 h, M10) need none.
    got = b._composite_tmax_shift(_rows(5, 10), "ZUUU")
    assert got["state"] == "ok" and got["hour"] == 6


def test_a_station_without_its_own_line_uses_the_fleet() -> None:
    got = b._composite_tmax_shift(_rows(13, 13), "XXXX")
    assert got["state"] == "ok" and not got["station_fit"] and got["station_bias_c"] == 0.0


def test_the_series_reads_each_hour_as_of_itself() -> None:
    rows = _rows(13, 13)
    got = b._composite_tmax_shift(rows, "ZUUU")
    assert [p["hour"] for p in got["series"]] == list(range(0, 14))
    assert got["series"][-1]["total_c"] == got["total_c"]
    # Hour 10 must not see the 11..13 reports: making them colder leaves it as it was.
    colder = _rows(13, 13)
    for r in colder:
        if r["hour"] > 10 and r["obs"]:
            r["obs"]["temp_c"] -= 5.0
    again = b._composite_tmax_shift(colder, "ZUUU")
    assert again["series"][10] == got["series"][10]
    assert again["series"][-1]["total_c"] < got["series"][-1]["total_c"]


def test_bucket_probabilities_on_the_days_ladder() -> None:
    axis = [{"bucket": "<=24", "lo_c": None, "hi_c": 24.5}] + [
        {"bucket": str(k), "lo_c": k - 0.5, "hi_c": k + 0.5} for k in range(25, 30)] + [
        {"bucket": ">=30", "lo_c": 29.5, "hi_c": None}]
    t = b.composite_bucket_probabilities(axis, 27.1, 1.0, 5)
    n = b.composite_bucket_probabilities(axis, 27.1, 1.0, None)
    assert abs(sum(x["p"] for x in t) - 1.0) < 1e-3
    assert max(t, key=lambda x: x["p"])["bucket"] == "27"
    # the t's heavier tails put more on the edges than the normal at the same variance
    assert t[-1]["p"] > n[-1]["p"] and t[0]["p"] >= n[0]["p"]
    assert min(x["p"] for x in t) >= 0.0009


def test_a_night_hour_reads_reports_from_midnight_and_no_sunshine() -> None:
    import json

    fit = json.loads(b.COMPOSITE_TMAX_SHIFT_PATH.read_text())
    fit["by_hour"]["3"] = fit["by_hour"]["9"]
    rows = _rows(2, 3)
    for r in rows:
        if r["hour"] <= 3:
            r["obs"] = {"temp_c": 15.0, "dew_c": 9.0, "rh_pct": 45.0, "wind_mps": 3.5, "precip": r["hour"] == 1}
    by_hour = {r["hour"]: r for r in rows}
    got = b._composite_at(by_hour, "ZUUU", fit, 3)
    assert got["x"]["sw"] == 0.0 and got["x"]["phys"] == 0.0
    assert got["x"]["tdev"] == -5.0 and got["x"]["rain"] == 1.0


def test_an_evening_hour_reads_the_running_maximum() -> None:
    import json

    fit = json.loads(b.COMPOSITE_TMAX_SHIFT_PATH.read_text())
    assert "rmax" in fit["by_hour"]["20"]["features"]
    rows = _rows(17, 17)
    for r in rows:
        if r["hour"] <= 20:
            r["obs"] = {"temp_c": 20.0 + (r["hour"] == 15) * 6.0, "dew_c": 9.0, "rh_pct": 45.0, "wind_mps": 3.5, "precip": False}
    got = b._composite_at({r["hour"]: r for r in rows}, "ZUUU", fit, 20)
    assert got["x"]["rmax"] == 2.0          # 26.0 seen at 15 h against a day-ahead maximum of 24.0
    assert 1.8 < got["parts_c"]["rmax"] < 2.2


def test_six_oclock_counts_as_night() -> None:
    import json

    fit = json.loads(b.COMPOSITE_TMAX_SHIFT_PATH.read_text())
    fit["by_hour"]["6"] = fit["by_hour"]["9"]
    rows = _rows(6, 6)
    got = b._composite_at({r["hour"]: r for r in rows}, "ZUUU", fit, 6)
    assert got["x"]["sw"] == 0.0 and got["x"]["phys"] == 0.0 and got["x"]["tdev"] == 2.0


def test_the_projection_carries_todays_trend_and_fades_it() -> None:
    rows = _rows(10, 10)
    by_hour = {r["hour"]: r for r in rows}
    trend = b._composite_trend(by_hour, 10)
    assert abs(trend["temp_c"] - 2.0) < 1e-9 and abs(trend["sw"] - 100.0) < 1e-9 and trend["rain"] == 0.0
    got = b._composite_tmax_shift(rows, "ZUUU")
    proj = got["projection"]["points"]
    assert proj and proj[0]["hour"] == 11 and proj[-1]["hour"] == 23
    assert got["series"][-1]["hour"] == 10
    # a warm, sunny morning keeps the dashed line on the warm side
    assert proj[0]["total_c"] > 0
