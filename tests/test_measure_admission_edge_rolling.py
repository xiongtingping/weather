"""A selection rule that may look at the day it is scored on proves nothing.

This lane picks cells of admitted windows on the past and scores them on the
future, which is the only construction that can answer "is there a subset we
could have known to hold".  Its one fatal failure mode is a peek, so that is
what these fixtures are built to catch: a cell that is good before a day and
bad on it must be selected and must then score badly.
"""
from __future__ import annotations

import ast
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import measure_admission_edge_rolling as lane  # noqa: E402


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE paper_trade_candidate_window (
          candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,
          stage TEXT, decision_at_utc TEXT, buckets_json TEXT,
          win_probability REAL, all_in_price REAL, admissible INTEGER,
          seated INTEGER, settled_bucket TEXT, hit INTEGER,
          band_axis TEXT);
        CREATE TABLE calibration_station_market_timezone_binding (
          station_id TEXT, settlement_timezone TEXT);
        """
    )
    conn.execute(
        "INSERT INTO calibration_station_market_timezone_binding"
        " VALUES ('KMIA','UTC')"
    )
    return conn


def _window(
    conn: sqlite3.Connection,
    *,
    cid: str,
    date: str,
    won: bool,
    price: float = 0.50,
    claim: float = 0.52,
    admissible: int = 1,
    seated: int = 0,
    station: str = "KMIA",
    stage: str = "P0",
    at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO paper_trade_candidate_window VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, station, date, stage, at or (date + "T00:00:00Z"),
         json.dumps(["30C", "31C"]), claim, price, admissible, seated,
         "30C" if won else "35C", 1 if won else 0, "claim"),
    )


def test_a_cell_is_picked_on_the_past_and_scored_on_the_day() -> None:
    conn = _database()
    # Three past days where this cell always won, then a day it loses.
    for index, date in enumerate(
        ("2026-09-01", "2026-09-02", "2026-09-03")
    ):
        for k in range(4):
            _window(conn, cid=f"p{index}{k}", date=date, won=True)
    for k in range(4):
        _window(conn, cid=f"t{k}", date="2026-09-04", won=False)
    conn.commit()

    report = lane.measure(
        conn, since="", min_cell_n=4, min_edge=0.0, draws=50, seed=1
    )
    picks = report["picks_by_day"]
    # Picked on 09-04, because the past said so.
    assert picks["2026-09-04"], picks
    # And scored on what 09-04 actually did, which was lose everything.
    # Asserted on that day's own row: the pool accumulates every day the
    # cell qualified, which from a four-row minimum is 09-02 onward.
    today = next(p for p in report["per_day"] if p["date"] == "2026-09-04")
    assert today["cells_picked"] == 1
    assert today["today_windows"] == 4
    assert today["today_wins"] == 0
    assert today["today_edge_per_dollar"] == -1.0
    # Nothing was held on the first day, which has no past.
    first = next(p for p in report["per_day"] if p["date"] == "2026-09-01")
    assert first["cells_picked"] == 0 and first["today_windows"] == 0
    assert report["references"]["all_admitted"]["windows"] == 16


def test_the_first_day_can_never_be_selected() -> None:
    """No history, no pick -- and no silent inclusion either."""

    conn = _database()
    for k in range(8):
        _window(conn, cid=f"a{k}", date="2026-09-01", won=True)
    conn.commit()

    report = lane.measure(
        conn, since="", min_cell_n=4, min_edge=0.0, draws=10, seed=1
    )
    assert report["per_day"][0]["days_of_history"] == 0
    assert report["per_day"][0]["cells_picked"] == 0
    assert report["references"]["rolling_selected"]["windows"] == 0
    # Whereas the in-sample table sees them, and says so in its name.
    assert report["in_sample_cell_count"] == 1


def test_a_thin_cell_is_not_picked_however_good_it_looks() -> None:
    conn = _database()
    for k in range(3):
        _window(conn, cid=f"p{k}", date="2026-09-01", won=True)
    _window(conn, cid="t0", date="2026-09-02", won=True)
    conn.commit()

    report = lane.measure(
        conn, since="", min_cell_n=4, min_edge=0.0, draws=10, seed=1
    )
    assert report["picks_by_day"]["2026-09-02"] == []


def test_only_admitted_windows_are_selectable() -> None:
    """A window the gate refused is not a thing we could have held."""

    conn = _database()
    for k in range(8):
        _window(conn, cid=f"p{k}", date="2026-09-01", won=True, admissible=0)
    for k in range(4):
        _window(conn, cid=f"t{k}", date="2026-09-02", won=True, admissible=0)
    conn.commit()

    report = lane.measure(
        conn, since="", min_cell_n=4, min_edge=0.0, draws=10, seed=1
    )
    assert report["references"]["all_admitted"]["windows"] == 0
    assert report["references"]["rolling_selected"]["windows"] == 0


def test_the_realised_payout_is_the_window_not_the_recorded_hit() -> None:
    """One share of each leg pays 1 when the truth lands inside the window.

    Read off the window and the settled bucket rather than the ``hit``
    column, which answers a different question -- whether the BOOK's basket
    paid -- and is the column a fill model moves.  This step must not be
    able to inherit that.
    """

    conn = _database()
    conn.execute(
        "INSERT INTO paper_trade_candidate_window VALUES"
        " ('x','KMIA','2026-09-01','P0','2026-09-01T00:00:00Z',"
        "  '[\"30C\",\"31C\"]',0.52,0.50,1,1,'31C',0,'claim')"
    )
    conn.commit()
    rows = lane._rows(conn, "")
    assert rows[0]["won"] is True
    assert rows[0]["recorded_hit"] == 0


def test_the_agreement_partition_is_productions_own() -> None:
    from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: PLC0415
        EFFECTIVE_EDGE_LAMBDA,
    )

    assert lane.EFFECTIVE_EDGE_LAMBDA is EFFECTIVE_EDGE_LAMBDA
    # Every band boundary in production's table is a boundary here.
    for upper, _lambda in EFFECTIVE_EDGE_LAMBDA:
        if upper == float("inf"):
            continue
        below = lane._agreement_band(float(upper) - 1e-9)
        at = lane._agreement_band(float(upper))
        assert below != at, upper


def test_the_bootstrap_clusters_on_the_day() -> None:
    """A day is one weather draw, so rows inside it are not independent."""

    source = (
        ROOT / "scripts" / "measure_admission_edge_rolling.py"
    ).read_text(encoding="utf-8")
    boot = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "_bootstrap"
    )
    body = ast.unparse(boot)
    assert "by_day" in body and "generator.choice(days)" in body
    # Resampling rows rather than days would be the error.
    assert "generator.choice(rows)" not in body


def test_the_lane_never_reads_a_settled_column_for_selection() -> None:
    """Selection may read the past's outcomes; it may not read today's.

    Pinned on the structure that enforces it: the only comparison that puts
    a row in the selectable pool is its date against the day being scored.
    """

    source = (
        ROOT / "scripts" / "measure_admission_edge_rolling.py"
    ).read_text(encoding="utf-8")
    measure = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "measure"
    )
    body = ast.unparse(measure)
    assert "r['date'] < today" in body
    assert "r['date'] == today" in body
    # And the two pools are never the same expression.
    assert "r['date'] <= today" not in body
