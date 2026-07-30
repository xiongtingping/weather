from datetime import datetime, timezone

from scripts.collect_dwd_icon_zuuu import _icon_urls


def test_icon_request_plan_is_the_official_normalized_field_surface() -> None:
    urls = _icon_urls(datetime(2026, 7, 12, 0, tzinfo=timezone.utc), 6)

    # The collector must request every field declared by the accepted ICON
    # normalized snapshot contract; the former eleven-file assertion covered
    # only the original temperature/wind subset and allowed silent schema
    # drift.
    assert len(urls) == 20
    assert all(url.startswith("https://opendata.dwd.de/weather/nwp/icon/grib/00/") for url in urls)
    assert any(url.endswith("_T_2M.grib2.bz2") for url in urls)
    assert any(url.endswith("_500_FI.grib2.bz2") for url in urls)
    assert any(url.endswith("_850_RELHUM.grib2.bz2") for url in urls)
