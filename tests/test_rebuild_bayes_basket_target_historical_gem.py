from __future__ import annotations

from datetime import date

import pytest

from bayes_basket_target.resource_bounds import ResourceBoundError
from bayes_basket_target.model_sources import (
    model_id_for_provider,
    provider_role,
    provider_source_class,
)
from bayes_basket_target.strict_asof_replay import (
    lead_bucket_for_maturity,
    maturity_key_for_model_run,
)
from scripts.rebuild_bayes_basket_target_historical_gem import (
    _url,
    _window,
)
import scripts.rebuild_bayes_basket_target_historical_gem as historical_gem


def test_native_gem_provider_is_preferred_native_lineage() -> None:
    assert model_id_for_provider("gem_gdps_official") == "GEM"
    assert provider_role("gem_gdps_official") == "gem_global_open_meteo"
    assert provider_source_class("gem_gdps_official") == (
        "native_or_official_cloud_replica"
    )


def test_gem_12z_d2_is_stable_maturity_for_global_timezones() -> None:
    for timezone_name in (
        "America/Los_Angeles",
        "Asia/Singapore",
        "Pacific/Auckland",
    ):
        maturity = maturity_key_for_model_run(
            "2026-07-26T12:00:00Z",
            "2026-07-28",
            target_timezone=timezone_name,
        )
        assert lead_bucket_for_maturity(maturity) == "d2"


def test_gem_archive_url_is_date_and_cycle_addressable() -> None:
    from scripts.rebuild_bayes_basket_target_historical_gem import _run

    run = _run(date(2026, 7, 28), "12Z", 2)
    url = _url(run, 48)
    assert "/20260726/WXO-DD/model_gdps/15km/12/048/" in url
    assert url.endswith(
        "20260726T12Z_MSC_GDPS_AirTemp_AGL-2m_"
        "LatLon0.15_PT048H.grib2"
    )


def test_half_hour_timezone_window_remains_24_hours() -> None:
    start, end = _window(date(2026, 7, 28), "Asia/Kolkata")
    assert start.isoformat() == "2026-07-27T18:30:00+00:00"
    assert (end - start).total_seconds() == 24 * 3600


def test_fetch_rejects_aggregate_raw_overflow_before_creating_part(
    tmp_path,
    monkeypatch,
) -> None:
    raw_root = tmp_path / "model_arrivals" / "gem_gdps_official" / "raw"
    raw_root.mkdir(parents=True)
    destination = raw_root / "pending-048.grib2"
    calls = []

    def reject(root, *, bytes_needed=0, **_kwargs):
        calls.append((root, bytes_needed))
        raise ResourceBoundError(
            "target_raw_aggregate_retention_budget_exceeded"
        )

    monkeypatch.setattr(
        historical_gem,
        "assert_target_raw_retention_budget",
        reject,
    )
    monkeypatch.setattr(
        historical_gem,
        "_binary",
        lambda *_args, **_kwargs: pytest.fail(
            "network setup must not run after aggregate preflight failure"
        ),
    )

    with pytest.raises(
        ResourceBoundError,
        match="target_raw_aggregate_retention_budget_exceeded",
    ):
        historical_gem._fetch(
            "https://example.invalid/object.grib2",
            destination,
            cached={},
            aggregate_raw_root=tmp_path / "model_arrivals",
        )

    assert calls == [
        (tmp_path / "model_arrivals", historical_gem.MAX_RESPONSE_BYTES)
    ]
    assert not destination.with_suffix(".part").exists()
