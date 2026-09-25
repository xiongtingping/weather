"""The market-move axis of charter 68's table lives in shadow.

Measured 2026-09-06: the direction the market moved a window over the
hours before a decision separates realised/claimed by 40-50 hit points
unstratified and by 8-9 once the current agreement ``a`` is held fixed.
The lane exists to accumulate that residual on real seats, per band; what
these tests pin is that it classifies the move off what the system held at
both ends, applies the same admission rule as the served table, and that
nothing it writes can reach a seat.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    center_window_claim_factor,
)
from bayes_basket_target import effective_edge_store as store  # noqa: E402
from tests.claim_lane_tables import add_label  # noqa: E402
import pytest  # noqa: E402


WINDOW = ["31°C", "32°C", "33°C"]


def _book(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "CREATE TABLE basket_candidate_window_scored ("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, buckets_json TEXT,"
        " win_probability REAL, all_in_price REAL, hit INTEGER)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history ("
        " history_id TEXT, station_id TEXT, event_date TEXT, stage TEXT,"
        " decision_at_utc TEXT, state TEXT, checkpoint_id TEXT,"
        # Production's table carries the commit clock (NOT NULL); the
        # convergence loader reads it as the knowable time.
        " created_at_utc TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_official_ws_snapshot ("
        " station_id TEXT, event_date TEXT, observed_at TEXT, state TEXT,"
        " buckets_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_bridge_bucket_probability ("
        " history_id TEXT, bucket TEXT, probability REAL)"
    )
    return conn


def _quotes(asks: dict[str, float]) -> str:
    return json.dumps([
        {"bucket": bucket, "ask_levels": [{"price": price, "shares": 100.0}]}
        for bucket, price in asks.items()
    ])


def _seat(conn, pid, *, station, day, decided, claim, asks, hit, stage="P0"):
    """One enumerated window, scored.  ``claim`` goes in raw and is stored
    lifted, because the enumeration lifts before the gate divides."""

    conn.execute(
        "INSERT INTO basket_candidate_window_scored VALUES(?,?,?,?,?,?,?,?,?)",
        (pid, station, day, stage, decided, json.dumps(WINDOW),
         claim * center_window_claim_factor(claim),
         sum(asks[b] for b in WINDOW), hit),
    )
    add_label(conn, station, day, settled_bucket=WINDOW[0] if hit else "x")


def _prior(conn, station, day, decided_prior, book_at, asks):
    history_id = f"h-{station}-{decided_prior}"
    conn.execute(
        "INSERT INTO basket_probability_bridge_history VALUES(?,?,?,?,?,?,?,?)",
        (history_id, station, day, "P0", decided_prior, "COMMITTED", "cp",
         decided_prior),
    )
    for bucket in WINDOW:
        conn.execute(
            "INSERT INTO basket_probability_bridge_bucket_probability VALUES(?,?,?)",
            (history_id, bucket, 0.2),
        )
    conn.execute(
        "INSERT INTO basket_official_ws_snapshot VALUES(?,?,?,?,?)",
        (station, day, book_at, "COMMITTED", _quotes(asks)),
    )


def test_the_move_is_read_off_what_the_system_held_at_both_ends(tmp_path):
    """Earlier end: the latest checkpoint at least H hours before the seat,
    priced on the first committed book after it.  Not the book before the
    decision, not a checkpoint inside the horizon."""

    conn = _book(tmp_path / "m.db")
    then = {"31°C": 0.20, "32°C": 0.30, "33°C": 0.20}      # 0.70 then
    now_cheaper = {"31°C": 0.15, "32°C": 0.25, "33°C": 0.15}  # 0.55 now
    _prior(conn, "ZGSZ", "2026-09-01", "2026-08-31T04:00:00.000Z",
           "2026-08-31T04:05:00.000Z", then)
    # A book inside the horizon must not be the earlier end.
    conn.execute(
        "INSERT INTO basket_official_ws_snapshot VALUES(?,?,?,?,?)",
        ("ZGSZ", "2026-09-01", "2026-08-31T13:00:00.000Z", "COMMITTED",
         _quotes({"31°C": 0.9, "32°C": 0.9, "33°C": 0.9})),
    )
    move = store.window_price_move(
        conn, station_id="ZGSZ", event_date="2026-09-01",
        decision_at_utc="2026-08-31T14:00:00.000Z", window=WINDOW,
        price_now=sum(now_cheaper.values()), horizon_hours=6.0, threshold=0.05,
    )
    assert move["tier"] == "down"
    assert abs(move["dq"] - (-0.15)) < 1e-9
    assert move["book_at"] == "2026-08-31T04:05:00.000Z"

    up = store.window_price_move(
        conn, station_id="ZGSZ", event_date="2026-09-01",
        decision_at_utc="2026-08-31T14:00:00.000Z", window=WINDOW,
        price_now=0.80, horizon_hours=6.0, threshold=0.05,
    )
    assert up["tier"] == "up"
    # An opening seat has nothing before it: no_prior, not a fabricated move.
    opening = store.window_price_move(
        conn, station_id="ZGSZ", event_date="2026-09-01",
        decision_at_utc="2026-08-31T04:01:00.000Z", window=WINDOW,
        price_now=0.70, horizon_hours=6.0, threshold=0.05,
    )
    assert opening["tier"] == "no_prior"
    conn.close()


def test_a_cell_is_fitted_only_past_the_served_tables_own_hurdle(tmp_path):
    """Fourteen 'down' seats in one band fit; three 'steady' ones report
    their count and no number, and the band's served lambda stands in."""

    conn = _book(tmp_path / "f.db")
    day, prior_at, book_at = "2026-09-01", "2026-08-31T02:00:00.000Z", "2026-08-31T02:01:00.000Z"
    then = {"31°C": 0.22, "32°C": 0.26, "33°C": 0.22}  # 0.70 then
    for index in range(14):
        station = f"S{index:02d}"
        _prior(conn, station, day, prior_at, book_at, then)
        # Claim 0.60 (charter 81's lift is identity since 2026-09-12); price
        # 0.555 puts a = 0.925 in the [0.85, 0.95) band.  The window fell
        # 0.145 from 0.70: tier down.
        _seat(conn, f"d{index}", station=station, day=day,
              decided="2026-08-31T12:00:00.000Z", claim=0.60,
              asks={"31°C": 0.15, "32°C": 0.255, "33°C": 0.15}, hit=index % 2)
    # Charter 96 split the top band at 0.95, so the steady group's old
    # a = 1.08 would now land in a different band from the down group and
    # this test would stop comparing two tiers of ONE band, which is what
    # it is for.  Its prior and its book are both 0.555 -- unmoved, so the
    # tier is still steady -- and a = 0.925 puts it beside the down group.
    steady_then = {"31°C": 0.15, "32°C": 0.255, "33°C": 0.15}  # 0.555 then
    for index in range(3):
        station = f"T{index:02d}"
        _prior(conn, station, day, prior_at, book_at, steady_then)
        # Same price as then: tier steady, a = 0.925, band [0.85, 0.95).
        _seat(conn, f"s{index}", station=station, day=day,
              decided="2026-08-31T12:00:00.000Z", claim=0.60,
              asks={"31°C": 0.15, "32°C": 0.255, "33°C": 0.15}, hit=1)
    fit = store.fit_lambda_table_dq_shadow(conn, stage_scope="P0")
    # By bound, not by position: the partition is a rule that has moved
    # once and may move again, and a positional read would go quiet
    # instead of failing when it does.
    top = next(
        cell for cell in fit["evidence"] if cell["band_upper_bound"] == 0.95
    )
    down, steady = top["tiers"]["down"], top["tiers"]["steady"]
    assert down["seats"] == 14 and down["admissible"]
    assert abs(down["raw_lambda"] - (7 / (14 * 0.60 * center_window_claim_factor(0.60)))) < 1e-3  # charter 81 lift (identity since retirement)
    assert down["fitted_lambda"] is not None
    assert steady["seats"] == 3 and not steady["admissible"]
    assert steady["fitted_lambda"] is None
    assert fit["table"]["bands"][top["band_index"]]["tiers"]["steady"] is None
    assert (top["band_index"], "down") in fit["admitted_cells"]
    assert fit["seat_count"] == 17
    conn.close()


def test_nothing_the_shadow_writes_can_reach_a_seat(tmp_path):
    """The shadow journals to its own table.  The served table is not
    touched, the active lookup stays None, and re-fitting the served table
    reproduces the same sha with the shadow row present."""

    conn = _book(tmp_path / "s.db")
    for index in range(13):
        # Price 0.555 over the claim 0.60 (no lift since charter 81 retired):
        # a = 0.925, the [0.85, 0.95) band.
        _seat(conn, f"p{index}", station=f"S{index:02d}", day="2026-09-01",
              decided="2026-08-31T12:00:00.000Z", claim=0.60,
              asks={"31°C": 0.15, "32°C": 0.255, "33°C": 0.15}, hit=index % 2)
    served_before = store.fit_lambda_table(conn, stage_scope="P0")["content_sha256"]
    shadow = store.fit_lambda_table_dq_shadow(conn, stage_scope="P0")
    receipt = store.persist_dq_shadow(conn, shadow)
    assert receipt["shadow_sha256"] == shadow["content_sha256"]
    assert store.load_active_lambda(conn, stage_scope="P0") is None
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_effective_edge_lambda"
    ).fetchone()[0] == 0
    loaded = store.load_dq_shadow(conn, stage_scope="P0")
    assert loaded["shadow_sha256"] == shadow["content_sha256"]
    # Every seat here is an opening seat: the lane says no_prior, not a move.
    banded = next(
        cell for cell in loaded["evidence"] if cell["band_upper_bound"] == 0.95
    )
    assert banded["tiers"]["no_prior"]["seats"] == 13
    assert store.fit_lambda_table(conn, stage_scope="P0")["content_sha256"] == served_before
    columns = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(calibration_effective_edge_lambda_dq_shadow)"
        )
    }
    assert "activated" not in columns
    conn.close()


def test_the_daily_refit_carries_the_shadow_and_the_solver_does_not():
    """Binding: the refit CLI runs the shadow fit next to the served one;
    no compile-chain module imports it."""

    cli = (ROOT / "scripts" / "refit_effective_edge_lambda.py").read_text()
    assert "fit_lambda_table_dq_shadow" in cli and "persist_dq_shadow" in cli
    for module in ("delta_v2_runtime.py", "production_io.py", "p0_preselection.py"):
        text = (ROOT / "bayes_basket_target" / module).read_text()
        assert "dq_shadow" not in text, module
