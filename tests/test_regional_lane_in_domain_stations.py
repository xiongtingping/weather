"""Stations added to a lane already running, on the criterion charter 95 set.

The MSM took ZSPD and ZSQD on 2026-09-07 with the reasoning this file pins:
they fall inside the model's own domain, the real transport returns a local
day for each, and capture is not admission -- the serving gate rules per
system, separately, and a station never captured can never be measured at all.

Applied to the whole roster on the same day: fifteen of the forty-five live
stations carried no regional lane, and exactly three of them sat inside the
domain of a lane already running.  EGLC and LIMC are in ICON-EU's; CYYZ is
under the CONUS grids, which run well north of the border.  All three were
verified by running the capture rather than by reading a domain spec: EGLC
returned 19.72C for 2026-09-08, LIMC 31.28C, and CYYZ landed rows in both
NOAA products.

The other twelve need a model this system does not run.  CMA GRAPES is global
and could reach them, and by operator ruling that lane stays on the mainland
China roster it was built for.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import cma_grapes_point_archive as CMA
from bayes_basket_target import dwd_icon_eu_point_archive as ICON
from bayes_basket_target import noaa_conus_archive as NOAA

ROOT = Path(__file__).resolve().parents[1]


def test_icon_eu_took_the_two_european_stations_inside_its_domain() -> None:
    captured = {station for station, _, _, _ in ICON.CAPTURE_STATIONS}
    assert {"EGLC", "LIMC"} <= captured
    by_id = {s[0]: s for s in ICON.CAPTURE_STATIONS}
    # ICON-EU runs 23.5W-62.5E, 29.5N-70.5N.  Both sit well inside it, and
    # the settlement timezone is the market registry's, never
    # calibration_station_registry.timezone_name -- that column says
    # America/New_York for both of these.
    assert by_id["EGLC"][3] == "Europe/London"
    assert by_id["LIMC"][3] == "Europe/Rome"
    for station in ("EGLC", "LIMC"):
        _, lat, lon, _ = by_id[station]
        assert 29.5 < lat < 70.5 and -23.5 < lon < 62.5


def test_the_conus_grids_reach_toronto_and_the_us_list_stays_true() -> None:
    us = {station for station, _, _, _ in NOAA._US_STATIONS}
    assert "CYYZ" not in us, "the name of that tuple has to stay true"
    for product in ("HRRR", "NBM"):
        captured = {s[0] for s in NOAA.CAPTURE_STATIONS[product]}
        assert "CYYZ" in captured, product
    by_id = {s[0]: s for s in NOAA.CAPTURE_STATIONS["NBM"]}
    assert by_id["CYYZ"][3] == "America/Toronto"


def test_the_cma_lane_stays_on_the_roster_it_was_built_for() -> None:
    """Operator ruling 2026-09-07: CN only.

    GRAPES Global is a global model at 0.125 degrees and nothing in its
    domain stops it reaching the twelve stations that still carry no
    regional lane.  It stays where it was built anyway; this pins that as a
    decision rather than an oversight, so the next person who notices the
    domain does not read it as one.
    """

    captured = sorted(station for station, _, _, _ in CMA.CAPTURE_STATIONS)
    assert captured == [
        "ZBAA", "ZGGG", "ZGSZ", "ZHHH", "ZSPD", "ZSQD", "ZUCK", "ZUUU",
    ]
    assert all(
        station.startswith("Z") and not station.startswith(("ZK", "ZM"))
        for station in captured
    )


def test_every_added_station_actually_returned_a_row() -> None:
    """The criterion is the transport's answer, not a domain spec."""

    db = ROOT / "data" / "bayes_basket_target_ssot.db"
    if not db.exists():
        pytest.skip("no local ssot database")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        ("calibration_regional_point_run",),
    ).fetchone():
        pytest.skip("lane table not built here")
    for station, system in (
        ("EGLC", "DWD_ICON_EU_POINT"),
        ("LIMC", "DWD_ICON_EU_POINT"),
        ("CYYZ", "NCEP_HRRR_POINT"),
        ("CYYZ", "NCEP_NBM_POINT"),
    ):
        rows = conn.execute(
            "SELECT COUNT(*) FROM calibration_regional_point_run"
            " WHERE station_id=? AND ensemble_system_id=?",
            (station, system),
        ).fetchone()[0]
        assert rows > 0, f"{station} never returned a row from {system}"
