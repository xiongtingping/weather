"""Mainline forward TAF scope must carry the market's own settlement timezone.

`settlement_timezone` on a TAF capture means "the timezone this market settles
its daily high in".  It is not the canonical ET event day, and it is not the
ASOS argmax side lane's clock.  A previous version of the projection rewrote
the ten ASOS argmax stations to America/New_York; because KSEA/KSFO/KLAX settle
in America/Los_Angeles and KAUS/KDAL/KHOU/KORD in America/Chicago, every TAF
revision captured for them tripped `forward_taf_market_scope_invalid` in
native_ensemble_training_facts and fail-closed the whole production mainline.
"""

import sqlite3
from pathlib import Path

import pytest

from scripts import weather_basket_yes_daemon as daemon


ROWS = (
    # station, its real market settlement timezone
    ("KSEA", "America/Los_Angeles"),
    ("KSFO", "America/Los_Angeles"),
    ("KLAX", "America/Los_Angeles"),
    ("KAUS", "America/Chicago"),
    ("KDAL", "America/Chicago"),
    ("KHOU", "America/Chicago"),
    ("KORD", "America/Chicago"),
    ("KATL", "America/New_York"),
    ("KLGA", "America/New_York"),
    ("KMIA", "America/New_York"),
    ("WSSS", "Asia/Singapore"),
)


@pytest.fixture
def source_db(tmp_path: Path) -> Path:
    path = tmp_path / "archive.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE reported_trajectory_market_registry("
        "slug TEXT,event_date TEXT,station_id TEXT,settlement_timezone TEXT,"
        "settlement_unit TEXT,outcome TEXT,yes_token TEXT,registry_json TEXT,"
        "source_lane TEXT,source_eligible INTEGER,market_active INTEGER,"
        "market_accepting_orders INTEGER)"
    )
    for station, timezone_name in ROWS:
        conn.execute(
            "INSERT INTO reported_trajectory_market_registry VALUES("
            "?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{station.lower()}-high-2026-08-06", "2026-08-06", station,
                timezone_name, "C", "yes", f"token-{station}", "{}",
                "awc", 1, 1, 1,
            ),
        )
    conn.commit()
    conn.close()
    return path


def test_projection_preserves_each_market_settlement_timezone(source_db: Path) -> None:
    projected = {
        row["station_id"]: row["settlement_timezone"]
        for row in daemon._canonical_live_taf_registry_rows(source_db)
    }

    assert projected == dict(ROWS)


@pytest.mark.parametrize(
    "station,expected",
    [("KSEA", "America/Los_Angeles"), ("KAUS", "America/Chicago")],
    ids=["pacific", "central"],
)
def test_asos_argmax_stations_are_not_rewritten_to_et(
    source_db: Path, station: str, expected: str
) -> None:
    projected = {
        row["station_id"]: row["settlement_timezone"]
        for row in daemon._canonical_live_taf_registry_rows(source_db)
    }

    assert station in daemon.ASOS_ARGMAX_LIVE_STATIONS
    assert projected[station] == expected
    assert projected[station] != "America/New_York"


def test_et_stations_keep_et_because_that_is_their_market_timezone(
    source_db: Path,
) -> None:
    projected = {
        row["station_id"]: row["settlement_timezone"]
        for row in daemon._canonical_live_taf_registry_rows(source_db)
    }

    # These three are ET in the registry itself, not by override -- which is
    # exactly why the old override went unnoticed.
    for station in ("KATL", "KLGA", "KMIA"):
        assert projected[station] == "America/New_York"


def test_non_argmax_station_is_unaffected(source_db: Path) -> None:
    projected = {
        row["station_id"]: row["settlement_timezone"]
        for row in daemon._canonical_live_taf_registry_rows(source_db)
    }

    assert projected["WSSS"] == "Asia/Singapore"
