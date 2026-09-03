from __future__ import annotations

import json

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from scripts import capture_official_native_ensemble_forward as capture
from bayes_basket_target.fleet import expected_station_count


def _station_ids() -> list[str]:
    return [f"T{index:03d}" for index in range(expected_station_count())]


def _registry(*, missing_station: str | None = None) -> dict[str, object]:
    rows = []
    for station_id in _station_ids():
        if station_id == missing_station:
            continue
        rows.append({
            "station_id": station_id,
            "event_date": "2026-08-01",
            "market_active": 1,
            "source_eligible": 1,
            "settlement_timezone": "Etc/UTC",
            "outcome": f"{station_id}-bucket",
        })
    return {"rows": rows}


def _install_station_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    stations = _station_ids()
    monkeypatch.setattr(
        capture,
        "coordinate_registry_manifest",
        lambda: {"station_ids": stations},
    )
    monkeypatch.setattr(
        capture,
        "station_coordinates",
        lambda _station: {"latitude": 1.0, "longitude": 2.0},
    )


def test_build_station_scopes_requires_all_45_canonical_eligible_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_station_registry(monkeypatch)
    scopes = capture.build_station_scopes(
        _registry(), event_date="2026-08-01"
    )

    assert len(scopes) == expected_station_count()
    assert {scope["station_id"] for scope in scopes} == set(_station_ids())
    assert all(scope["settlement_timezone"] == "Etc/UTC" for scope in scopes)


def test_build_station_scopes_fails_closed_on_a_missing_canonical_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_station_registry(monkeypatch)

    with pytest.raises(ContractError, match="registry_scope_missing"):
        capture.build_station_scopes(
            _registry(missing_station="T017"), event_date="2026-08-01"
        )


def test_retained_archive_is_reused_only_when_scope_and_hash_match(tmp_path) -> None:
    stations = _station_ids()
    identity = {
        "schema": "noaa_gefs_official_historical_multistation_members_v2",
        "event_date": "2026-08-01",
        "station_count": len(stations),
        "station_ids": stations,
        "captures": [{} for _ in stations],
        "training_only": True,
        "holdout_eligible": False,
    }
    path = tmp_path / "gefs.json"
    path.write_text(
        json.dumps(
            {**identity, "content_sha256": content_sha256(identity)},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    assert capture._load_retained_archive(
        path,
        event_date="2026-08-01",
        station_ids=stations,
        expected_schema=identity["schema"],
    ) is not None
    assert capture._load_retained_archive(
        path,
        event_date="2026-08-02",
        station_ids=stations,
        expected_schema=identity["schema"],
    ) is None


def test_forward_retained_archive_reuse_is_bounded_by_six_hour_ttl(tmp_path) -> None:
    stations = _station_ids()
    identity = {
        "schema": "noaa_gefs_official_historical_multistation_members_v2",
        "event_date": "2026-08-01",
        "archive_query_at_utc": "2026-08-01T00:00:00Z",
        "station_count": len(stations),
        "station_ids": stations,
        "captures": [{} for _ in stations],
        "training_only": True,
        "holdout_eligible": False,
    }
    path = tmp_path / "gefs.json"
    path.write_text(
        json.dumps(
            {**identity, "content_sha256": content_sha256(identity)},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    assert capture._load_retained_archive(
        path,
        event_date="2026-08-01",
        station_ids=stations,
        expected_schema=identity["schema"],
        as_of_utc="2026-08-01T05:59:59Z",
        max_reuse_age_seconds=capture.FORWARD_REUSE_TTL_SECONDS,
    ) is not None
    assert capture._load_retained_archive(
        path,
        event_date="2026-08-01",
        station_ids=stations,
        expected_schema=identity["schema"],
        as_of_utc="2026-08-01T06:00:01Z",
        max_reuse_age_seconds=capture.FORWARD_REUSE_TTL_SECONDS,
    ) is None


def test_a_family_is_banked_before_the_next_family_is_fetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A pass killed mid-way must leave its finished families on disk.

    The archives used to be written only after the last family, so the
    caller's deadline (2400s until 2026-09-03) discarded whole passes: the
    2026-09-03 d1 pass banked GEFS 2h23m after the other three, on a second
    attempt, and no 2026-09-04 d2 pass ever finished.  With each family
    written as it lands, the retry reuses it through ``_load_retained_archive``
    and a long pass converges across attempts.
    """
    _install_station_registry(monkeypatch)
    monkeypatch.setattr(
        capture, "load_market_registry_snapshot", lambda _path: _registry()
    )
    gefs_output = tmp_path / "gefs.json"
    ecmwf_output = tmp_path / "ecmwf.json"
    banked = {"schema": "noaa_gefs_official_historical_multistation_members_v2",
              "captures": []}
    monkeypatch.setattr(
        capture, "capture_gefs_station_days", lambda **_kw: dict(banked)
    )
    seen: dict[str, bool] = {}

    def ecmwf_fails_after_looking(**_kw):
        seen["gefs_on_disk_when_ecmwf_ran"] = gefs_output.is_file()
        raise RuntimeError("proxy stalled")

    monkeypatch.setattr(
        capture, "capture_ecmwf_station_days", ecmwf_fails_after_looking
    )
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    with pytest.raises(
        ContractError, match="official_native_forward_required_family_unavailable"
    ):
        capture.capture_official_native_forward(
            registry_path=registry_path,
            event_date="2026-08-01",
            gefs_output=gefs_output,
            ecmwf_output=ecmwf_output,
            inbox_output=tmp_path / "inbox.json",
            timeout_seconds=30.0,
        )
    assert seen == {"gefs_on_disk_when_ecmwf_ran": True}
    assert json.loads(gefs_output.read_text(encoding="utf-8")) == banked


def test_d1_18z_run_covers_eastern_stations_under_station_local_same_day_relief() -> None:
    """A run issued after the station's local midnight is same-day for it.

    2026-09-02 18Z is 03:00 Tokyo on 2026-09-03: it can seal six 3-hourly
    points, the same relief a same-day 06Z run gets.  Judged by UTC date it
    was held to the full-day bar and the d1 18Z rung failed for the whole
    fleet every night, while the 12Z rung behind it captured 45 stations.
    """

    from datetime import datetime, timezone

    scopes = [
        {"station_id": "RJTT", "settlement_timezone": "Asia/Tokyo"},
        {"station_id": "NZWN", "settlement_timezone": "Pacific/Auckland"},
        {"station_id": "EGLC", "settlement_timezone": "Europe/London"},
        {"station_id": "KLAX", "settlement_timezone": "America/Los_Angeles"},
    ]
    covered, skipped = capture._run_coverable_scopes(
        scopes,
        event_date="2026-09-03",
        run_init=datetime(2026, 9, 2, 18, tzinfo=timezone.utc),
    )
    assert [scope["station_id"] for scope in covered] == [
        "RJTT", "NZWN", "EGLC", "KLAX",
    ]
    assert skipped == []

    # The same-day floor is a floor: a run issued after the station's day
    # has nearly ended still cannot seal, and is still excluded up front.
    covered, skipped = capture._run_coverable_scopes(
        scopes[:1],
        event_date="2026-09-03",
        run_init=datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
    )
    assert covered == []
    assert skipped[0]["reason"] == "run_cannot_cover_station_day"
