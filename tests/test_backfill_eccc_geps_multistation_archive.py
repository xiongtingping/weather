from __future__ import annotations

import json
import sqlite3

import pytest

from scripts import backfill_eccc_geps_multistation_archive as backfill


REGISTRY_DDL = """
CREATE TABLE basket_target_market_registry (
  snapshot_sha256 TEXT NOT NULL,
  slug TEXT NOT NULL,
  event_date TEXT NOT NULL,
  outcome TEXT NOT NULL,
  city_slug TEXT NOT NULL,
  station_id TEXT,
  settlement_timezone TEXT,
  settlement_unit TEXT,
  source_lane TEXT NOT NULL,
  source_eligible INTEGER NOT NULL,
  rules_sha256 TEXT NOT NULL,
  yes_token TEXT,
  registry_json TEXT NOT NULL,
  refreshed_at TEXT NOT NULL,
  PRIMARY KEY(snapshot_sha256,slug,event_date,outcome)
)
"""


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(REGISTRY_DDL)
    return conn


def _insert_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot_sha256: str,
    event_date: str,
    station_count: int,
) -> None:
    """Write one atomic registry snapshot covering ``station_count`` stations."""

    for index in range(station_count):
        station = f"T{index:03d}"
        conn.execute(
            "INSERT INTO basket_target_market_registry("
            "snapshot_sha256,slug,event_date,outcome,city_slug,station_id,"
            "settlement_timezone,settlement_unit,source_lane,source_eligible,"
            "rules_sha256,yes_token,registry_json,refreshed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot_sha256,
                f"{station.lower()}-{event_date}",
                event_date,
                "30C_OR_ABOVE",
                station.lower(),
                station,
                "Etc/UTC",
                "C",
                "amsc",
                1,
                "0" * 64,
                f"yes-{station}",
                json.dumps({
                    "market_id": f"mkt-{station}",
                    "lower": 30.0,
                    "upper": None,
                }),
                f"{event_date}T00:00:00Z",
            ),
        )
    conn.commit()


@pytest.fixture(autouse=True)
def _stub_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backfill,
        "station_coordinates",
        lambda _station: {"latitude": 1.0, "longitude": 2.0},
    )


def test_eccc_geps_scopes_select_widest_snapshot_not_latest() -> None:
    """A settled day's newest snapshot is its narrowest, so recency loses data.

    On a closed event date the last snapshot is taken as markets wind down and
    can hold a handful of stations, while an earlier snapshot the same day holds
    all 45.  Selecting by recency made every historical backfill fail the
    45-station requirement -- exactly the data an out-of-sample holdout needs.
    """

    conn = _connection()
    _insert_snapshot(
        conn,
        snapshot_sha256="a" * 64,
        event_date="2026-07-31",
        station_count=45,
    )
    _insert_snapshot(
        conn,
        snapshot_sha256="f" * 64,
        event_date="2026-07-31",
        station_count=3,
    )

    scopes = backfill._station_scopes(conn, event_date="2026-07-31")

    assert len(scopes) == 45
    assert {row["station_id"] for row in scopes} == {
        f"T{index:03d}" for index in range(45)
    }


def test_eccc_geps_scopes_fail_closed_when_event_date_is_not_45_stations() -> (
    None
):
    conn = _connection()
    _insert_snapshot(
        conn,
        snapshot_sha256="a" * 64,
        event_date="2026-07-31",
        station_count=44,
    )

    with pytest.raises(
        RuntimeError,
        match="eccc_geps_archive_45station_registry_required",
    ):
        backfill._station_scopes(conn, event_date="2026-07-31")


def test_eccc_geps_scopes_never_borrow_another_event_date() -> None:
    """Coverage on a neighbouring date must not satisfy the requested one."""

    conn = _connection()
    _insert_snapshot(
        conn,
        snapshot_sha256="a" * 64,
        event_date="2026-07-30",
        station_count=45,
    )
    _insert_snapshot(
        conn,
        snapshot_sha256="b" * 64,
        event_date="2026-07-31",
        station_count=44,
    )

    with pytest.raises(
        RuntimeError,
        match="eccc_geps_archive_45station_registry_required",
    ):
        backfill._station_scopes(conn, event_date="2026-07-31")


def test_eccc_geps_scopes_fail_closed_when_date_has_no_snapshot() -> None:
    conn = _connection()

    with pytest.raises(
        RuntimeError,
        match="eccc_geps_archive_registry_snapshot_missing",
    ):
        backfill._station_scopes(conn, event_date="2026-07-31")
