"""The NOAA CONUS lane's arithmetic, with the network stubbed out.

Three things broke or nearly broke while the lane was built, and each is
pinned here: the idx parse must take the NBM's mean TMP line and not the
":ens std dev" one two lines later; the peak-coverage pre-filter must
refuse the runs whose published hours cannot reach a station's afternoon
(NBM 00Z against any US station, anything older than one day against
HRRR's f48 ceiling); and the local-day offset must be the zone's signed
offset read before UTC conversion zeroes it -- the MSM capture shipped
with ``utcoffset()`` of a converted instant, always zero, and quietly
captured UTC days for weeks.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import bayes_basket_target.noaa_conus_archive as noaa
from bayes_basket_target.jma_msm_archive import local_day_hours


def test_product_hours_shapes() -> None:
    run = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    assert noaa.product_hours("HRRR", run) == list(range(0, 49))
    nbm = noaa.product_hours("NBM", run)
    assert nbm[:36] == list(range(1, 37))
    assert nbm[36:] == [39, 42, 45, 48]


def test_peak_prefilter_matches_measured_coverage() -> None:
    # KATL, EDT: local 11:00-18:59 on the event day is f27-f34 of the
    # previous day's 12Z run.
    event = date(2026, 8, 28)
    cases = [
        ("HRRR", datetime(2026, 8, 27, 12, tzinfo=timezone.utc), True),
        ("HRRR", datetime(2026, 8, 27, 0, tzinfo=timezone.utc), True),
        # Two days back: the afternoon sits past f48.
        ("HRRR", datetime(2026, 8, 26, 12, tzinfo=timezone.utc), False),
        ("NBM", datetime(2026, 8, 27, 12, tzinfo=timezone.utc), True),
        # NBM 00Z reaches a US afternoon only through the three-hourly
        # stretch; refusing it here is what saves a 50 MB fetch per pass.
        ("NBM", datetime(2026, 8, 27, 0, tzinfo=timezone.utc), False),
    ]
    for product, run, expected in cases:
        assert noaa.run_covers_peak(
            product=product, run_init=run, event_date=event,
            utc_offset_hours=-4,
        ) is expected, (product, run)


def test_local_day_offset_sign_convention() -> None:
    # local_day_hours wants the zone's signed offset: +8 for Taipei puts
    # the local day's start eight hours BEFORE the UTC date line, -4 for
    # New York puts it four hours after.
    run = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    katl = local_day_hours(
        run_init=run, event_date=date(2026, 8, 28), utc_offset_hours=-4
    )
    assert katl[0] == 16   # local midnight EDT = 04Z = f16
    assert katl[11:19] == [27, 28, 29, 30, 31, 32, 33, 34]
    taipei = local_day_hours(
        run_init=run, event_date=date(2026, 8, 28), utc_offset_hours=8
    )
    assert taipei[0] == 4  # local midnight CST = 16Z the day before = f4
    # And the capture-side formula: the offset must be read off the
    # localized instant, not the UTC-converted one (which is always zero).
    for zone_name, expected in (
        ("America/New_York", -4), ("Asia/Taipei", 8),
        ("America/Los_Angeles", -7),
    ):
        local = datetime.combine(
            date(2026, 8, 28), datetime.min.time(),
            tzinfo=ZoneInfo(zone_name),
        )
        assert int(local.utcoffset().total_seconds() // 3600) == expected
        assert local.astimezone(timezone.utc).utcoffset().total_seconds() == 0


def test_idx_parse_takes_mean_not_std_dev(monkeypatch) -> None:
    idx = (
        "1:0:d=2026082712:REFC:entire atmosphere:30 hour fcst:\n"
        "2:1000:d=2026082712:TMP:2 m above ground:30 hour fcst:\n"
        "3:5000:d=2026082712:TMP:2 m above ground:30 hour fcst:ens std dev\n"
        "4:9000:d=2026082712:DPT:2 m above ground:30 hour fcst:\n"
    )
    monkeypatch.setattr(noaa, "_get", lambda url: idx.encode())
    assert noaa._field_range("https://example/blend.grib2") == (1000, 5000)


def test_idx_parse_last_field_open_ended(monkeypatch) -> None:
    idx = (
        "1:0:d=2026082712:REFC:entire atmosphere:30 hour fcst:\n"
        "2:1000:d=2026082712:TMP:2 m above ground:30 hour fcst:\n"
    )
    monkeypatch.setattr(noaa, "_get", lambda url: idx.encode())
    assert noaa._field_range("https://example/hrrr.grib2") == (1000, None)


def test_registered_transports_present_in_schema_source() -> None:
    from pathlib import Path
    source = (
        Path(noaa.__file__).resolve().parent / "ssot.py"
    ).read_text(encoding="utf-8")
    for transport in noaa.TRANSPORTS.values():
        # Twice: the storage allowlist and the narrower OOS-eligibility
        # list both must carry it, or capture round-trips a fetch to fail.
        assert source.count(f"'{transport}'") >= 2, transport


def test_point_system_registration() -> None:
    from bayes_basket_target.official_native_forward_packet import (
        POINT_FORECAST_SYSTEM_IDS, POINT_FORECAST_SYSTEMS,
    )
    for product, system_id in noaa.SYSTEM_IDS.items():
        registration = POINT_FORECAST_SYSTEMS[system_id]
        assert registration["expected_member_count"] == 1
        assert registration["independence_family_id"] == (
            noaa.FAMILY_IDS[product]
        )
        # Strict hourly grids: init hour IS the cycle, so neither may sit
        # in the waiver set that skips that check.
        assert system_id not in POINT_FORECAST_SYSTEM_IDS
