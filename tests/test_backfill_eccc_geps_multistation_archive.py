from __future__ import annotations

import sqlite3

import pytest

from scripts import backfill_eccc_geps_multistation_archive as backfill


def _registry_rows(event_date: str, count: int) -> list[dict[str, object]]:
    return [
        {
            "station_id": f"T{index:03d}",
            "event_date": event_date,
            "settlement_timezone": "Etc/UTC",
        }
        for index in range(count)
    ]


def test_eccc_geps_scopes_bind_exact_event_date_and_require_all_45(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    def _active(
        _conn: sqlite3.Connection,
        *,
        event_date: str = "",
        **_kwargs: object,
    ) -> list[dict[str, object]]:
        requested.append(event_date)
        return _registry_rows(event_date, 45)

    monkeypatch.setattr(backfill, "load_active_registry_rows", _active)
    monkeypatch.setattr(
        backfill,
        "station_coordinates",
        lambda _station: {"latitude": 1.0, "longitude": 2.0},
    )

    scopes = backfill._station_scopes(
        sqlite3.connect(":memory:"),
        event_date="2026-07-31",
    )

    assert requested == ["2026-07-31"]
    assert len(scopes) == 45
    assert {row["station_id"] for row in scopes} == {
        f"T{index:03d}" for index in range(45)
    }


def test_eccc_geps_scopes_fail_closed_when_event_date_is_not_45_stations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backfill,
        "load_active_registry_rows",
        lambda _conn, **_kwargs: _registry_rows("2026-07-31", 44),
    )
    monkeypatch.setattr(
        backfill,
        "station_coordinates",
        lambda _station: {"latitude": 1.0, "longitude": 2.0},
    )

    with pytest.raises(
        RuntimeError,
        match="eccc_geps_archive_45station_registry_required",
    ):
        backfill._station_scopes(
            sqlite3.connect(":memory:"),
            event_date="2026-07-31",
        )
