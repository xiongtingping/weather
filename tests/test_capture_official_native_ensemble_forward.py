from __future__ import annotations

import json

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from scripts import capture_official_native_ensemble_forward as capture


def _station_ids() -> list[str]:
    return [f"T{index:03d}" for index in range(45)]


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

    assert len(scopes) == 45
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
