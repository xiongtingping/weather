"""The board's one-station panel: shape statistics on a ladder (user 2026-10-05)."""

from __future__ import annotations

import math

from bayes_basket_target.station_day_board import bucket_of, ladder_axis, vector_stats


def _axis():
    ordinals = {"20°C or below": 0, "21°C": 1, "22°C": 2, "23°C or higher": 3}
    intervals = {"20°C or below": [None, 20.5], "21°C": [20.5, 21.5],
                 "22°C": [21.5, 22.5], "23°C or higher": [22.5, None]}
    return ladder_axis(ordinals, intervals)


def test_open_edges_sit_half_a_rung_inside() -> None:
    axis = _axis()
    assert [r["centre_c"] for r in axis] == [20.0, 21.0, 22.0, 23.0]
    assert bucket_of(axis, 21.49) == "21°C"
    assert bucket_of(axis, 30.0) == "23°C or higher"


def test_mean_variance_mode_and_spread() -> None:
    axis = _axis()
    s = vector_stats({"20°C or below": 0.1, "21°C": 0.4, "22°C": 0.4, "23°C or higher": 0.1}, axis)
    assert math.isclose(s["mean_c"], 21.5)
    assert math.isclose(s["var_c2"], 0.65, abs_tol=1e-9)
    # A tie goes to the lower rung, as the market mode does (charter 249).
    assert s["mode"] == "21°C" and s["mean_bucket"] == "22°C"
    assert s["interval80"] == ["20°C or below", "22°C"]
    assert math.isclose(s["effective_rungs"], 2 ** s["entropy_bits"], rel_tol=1e-2)


def test_unnormalised_vectors_are_normalised() -> None:
    s = vector_stats({"21°C": 2.0, "22°C": 2.0}, _axis())
    assert math.isclose(s["mode_p"], 0.5)


def test_today_is_past_its_peak_only_once_the_reading_turns_down(monkeypatch) -> None:
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    import bayes_basket_target.station_day_board as B

    zone = ZoneInfo("Asia/Shanghai")
    monkeypatch.setattr(B, "_model_peak", lambda *a: {"peak_at_utc": "2026-10-05T06:00:00Z"})

    def reports(*temps):
        return lambda *a: [{"valid_at_utc": at, "temp_c": t, "local": ""} for at, t in temps]

    now = datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(B, "_metar", reports(("2026-10-05T06:00:00Z", 21.0), ("2026-10-05T10:00:00Z", 20.0)))
    assert B.past_peak(None, "ZUUU", "2026-10-05", zone, now)["past"] is True
    # Still at the high after the peak hour: the day may yet warm.
    monkeypatch.setattr(B, "_metar", reports(("2026-10-05T06:00:00Z", 20.0), ("2026-10-05T10:00:00Z", 21.0)))
    assert B.past_peak(None, "ZUUU", "2026-10-05", zone, now)["past"] is False
    # Before the peak hour nothing is hidden.
    early = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(B, "_metar", reports(("2026-10-05T01:00:00Z", 19.0), ("2026-10-05T03:00:00Z", 18.0)))
    assert B.past_peak(None, "ZUUU", "2026-10-05", zone, early)["past"] is False


def test_clear_sky_radiation_follows_the_sun() -> None:
    from datetime import datetime, timezone

    import bayes_basket_target.station_day_board as B

    noon = B._clear_sky_ghi(30.576, 103.95, datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc))   # 13:00 local
    night = B._clear_sky_ghi(30.576, 103.95, datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc))
    assert 700 < noon < 900 and night == 0.0


def test_goes_hours_zero_the_night_skip_the_unpublished_and_place_each_reading(monkeypatch) -> None:
    """User 2026-10-09「全部都用卫星实测日照」: the Americas read NOAA GOES DSR.
    Hour h is the scan nearest h-1:30 local; night hours are zero without a
    read; a scan not public yet is not asked for; readings land on their hour."""

    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from bayes_basket_target import goes_surface_radiation as goes
    from bayes_basket_target import station_day_board as board

    asked: list[datetime] = []

    def fake_measured(station, lat, lon, instants):
        asked.extend(instants)
        # 12:00 local (EDT) is the scan nearest 11:30 local = 15:30Z.
        return {datetime(2026, 10, 8, 15, 30, tzinfo=timezone.utc).isoformat(): 640.0}

    monkeypatch.setattr(goes, "measured", fake_measured)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 8, 17, 0, tzinfo=timezone.utc)  # 13:00 EDT

    monkeypatch.setattr(board, "datetime", Clock)
    out = board._goes_hours("KLGA", 40.77945, -73.88027, "2026-10-08", ZoneInfo("America/New_York"))
    values = out["values"]
    assert values[2] == 0.0 and values[22] is None  # night zero; the evening not reached
    assert values[12] == 640.0 and values[11] is None  # placed on its hour; one still pending
    assert all(instant <= datetime(2026, 10, 8, 16, 35, tzinfo=timezone.utc) for instant in asked)
    assert out["pending_hours"] >= 1


def test_madis_five_minute_rain_lands_on_the_hour_its_accumulation_closes(monkeypatch) -> None:
    """MADIS precipAccum is the rain since the last routine report (reset just
    after ~:53): a record before :53 belongs to the hour ending at the next
    top of the hour, one from :53 on to the hour after; VC is not the station."""

    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from bayes_basket_target import madis_precip_board as madis

    utc = lambda h, m: datetime(2026, 10, 8, h, m, tzinfo=timezone.utc)  # noqa: E731
    rows = [(utc(7, 40), "-RA BR", 0.25), (utc(7, 50), "-RA", 0.51), (utc(7, 55), "-RA", 0.0),
            (utc(8, 20), "VCSH", 0.0), (utc(8, 45), "", 0.0)]
    fresh = madis.time.monotonic()
    for hour in range(0, 24):
        name = f"20261008_{hour:02d}00"
        madis._OBJECTS[name] = (fresh, {"KLGA": [r for r in rows if r[0].hour == hour]})
    monkeypatch.setitem(madis.__dict__, "_PENDING", set())

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(madis, "datetime", Clock)
    monkeypatch.setattr(madis, "_WORKER", type("Alive", (), {"is_alive": lambda self: True})())
    madis._STATIONS.add("KLGA")
    out = madis.station_day("KLGA", "2026-10-08", ZoneInfo("America/New_York"))  # EDT: 07:40Z is 03:40
    assert out["available"]
    assert out["hourly"]["04"]["mm"] == 0.51 and out["hourly"]["04"]["wet"]   # 03:40, 03:50 -> hour ending 04:00
    assert out["hourly"]["05"]["mm"] == 0.0 and out["hourly"]["05"]["wet"]    # 03:55 opens the next window
    assert out["hourly"]["05"]["wx"] == ["-RA"]                                # 04:20 VCSH is not rain here
    assert out["latest"] == {"local": "04:45", "wx": "", "accum_mm": 0.0, "wet": False}
    for hour in range(24):
        madis._OBJECTS.pop(f"20261008_{hour:02d}00", None)
