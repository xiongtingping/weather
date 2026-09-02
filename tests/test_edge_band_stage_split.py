"""The claimed-edge thermometer, split by the stage that made the claim.

A leg claiming a 30% edge at P0 has only the model behind it; the same
claim at P1 has hours of the day's own observations behind it.  Pooling
them reported the >=30% band as realising 0.25x, which was 0.07x for P0 and
0.35x for P1 blended -- a number true of neither lane (measured against the
book on 2026-09-02; user ruling: split it).

It matters most in this section of all, because the section exists to watch
the charter 62 calibration repair land, and stages that repair at different
rates are exactly what a pooled column hides.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


def _book(tmp_path: Path, rows, settlements) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "book.db")
    conn.execute(
        "CREATE TABLE paper_trade_position (station_id TEXT, event_date TEXT,"
        " probabilities_json TEXT, prices_json TEXT, leg_shares_json TEXT,"
        " stage TEXT)"
    )
    conn.execute(
        "CREATE TABLE paper_trade_settlement (station_id TEXT,"
        " event_date TEXT, settled_bucket TEXT)"
    )
    conn.executemany(
        "INSERT INTO paper_trade_position VALUES(?,?,?,?,?,?)", rows
    )
    conn.executemany(
        "INSERT INTO paper_trade_settlement VALUES(?,?,?)", settlements
    )
    conn.commit()
    return conn


def _legs(stage, count, *, claim, ask, wins, day="2026-08-20"):
    """``count`` single-leg positions at one stage, ``wins`` of them right."""

    rows, settlements = [], []
    for index in range(count):
        station = f"{stage.replace('-', '')}{index:03d}"
        rows.append((
            station, day,
            json.dumps({"20°C": claim}), json.dumps({"20°C": ask}),
            json.dumps({"20°C": 1.0}), stage,
        ))
        settlements.append(
            (station, day, "20°C" if index < wins else "21°C")
        )
    return rows, settlements


def test_each_stage_is_reported_beside_the_pooled_reading(tmp_path) -> None:
    """Twelve P0 legs that never land and twelve P1 legs that always do,
    all claiming the same edge.  Pooled they read as half right; that is
    exactly the reading the split has to take apart."""

    p0_rows, p0_set = _legs("P0", 12, claim=0.5, ask=0.1, wins=0)
    p1_rows, p1_set = _legs("P1", 12, claim=0.5, ask=0.1, wins=12)
    conn = _book(tmp_path, p0_rows + p1_rows, p0_set + p1_set)
    try:
        out = C.collect_edge_band_health(conn)
    finally:
        conn.close()
    band = next(b for b in out["bands"] if b["band"] == ">=30%")
    assert band["all"]["n"] == 24
    assert band["all"]["realize_filled_x"] == pytest.approx(1.0)
    assert band["by_stage"]["P0"]["realize_filled_x"] == pytest.approx(0.0)
    assert band["by_stage"]["P1"]["realize_filled_x"] == pytest.approx(2.0)
    assert out["stages"] == ["P0", "P1"]
    assert out["stage_totals"] == {"P0": 12, "P1": 12}


def test_a_stage_below_the_ten_leg_floor_reports_no_ratio(tmp_path) -> None:
    """P0-M carries three to twelve legs a band on the real book.  A ratio
    off nine legs is noise wearing a number's clothes, and the section's
    own floor already says so -- the split must not route around it."""

    rows, settlements = _legs("P0-M", 9, claim=0.5, ask=0.1, wins=9)
    conn = _book(tmp_path, rows, settlements)
    try:
        out = C.collect_edge_band_health(conn)
    finally:
        conn.close()
    band = next(b for b in out["bands"] if b["band"] == ">=30%")
    assert band["by_stage"]["P0-M"]["n"] == 9
    assert band["by_stage"]["P0-M"]["realize_filled_x"] is None


def test_the_stage_legs_add_up_to_the_pooled_legs(tmp_path) -> None:
    """Whatever the split says, it must be a partition of the same legs --
    a column that quietly drops or double-counts a stage would make the
    pooled column and its parts tell different stories."""

    p0_rows, p0_set = _legs("P0", 11, claim=0.5, ask=0.1, wins=3)
    p1_rows, p1_set = _legs("P1", 7, claim=0.2, ask=0.19, wins=2)
    conn = _book(tmp_path, p0_rows + p1_rows, p0_set + p1_set)
    try:
        out = C.collect_edge_band_health(conn)
    finally:
        conn.close()
    for band in out["bands"]:
        split = sum(cell["n"] for cell in band["by_stage"].values())
        assert split == band["all"]["n"], band["band"]
    assert sum(out["stage_totals"].values()) == out["totals"]["legs"]


def test_a_stage_with_no_legs_is_not_given_a_column(tmp_path) -> None:
    rows, settlements = _legs("P0", 11, claim=0.5, ask=0.1, wins=1)
    conn = _book(tmp_path, rows, settlements)
    try:
        out = C.collect_edge_band_health(conn)
    finally:
        conn.close()
    assert out["stages"] == ["P0"]
    assert set(out["bands"][0]["by_stage"]) == {"P0"}


def test_an_empty_book_still_names_the_stage_key(tmp_path) -> None:
    """The page reads ``E.stages`` to build its header; leaving the key out
    on an empty book would throw before the "no leg data" card renders."""

    conn = _book(tmp_path, [], [])
    try:
        out = C.collect_edge_band_health(conn)
    finally:
        conn.close()
    assert out["stages"] == []
    assert out["bands"] == []
