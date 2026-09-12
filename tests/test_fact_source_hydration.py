import sqlite3
import math

from ui.server import (
    _apply_market_quote_fallback_for_side_in_place,
    _apply_display_probability_fields_in_place,
    _hydrate_distribution_market_fields_in_place,
    _load_latest_snapshot,
    _market_snapshot_payload_from_latest_ticks,
)


def test_apply_market_quote_fallback_for_side_in_place_uses_gamma_price() -> None:
    row = {
        "outcome": "35°C",
        "yes_price": 0.51,
        "no_price": 0.49,
        "q_round": 0.55,
        "q_raw": 0.55,
        "q_calibrated": 0.56,
        "time_trend": 0.4,
        "time_freshness": 1.0,
        "structure_factor": 1.0,
        "ask_missing_reason_yes": "orderbook_fetch_disabled",
        "ask_missing_reason_no": "orderbook_fetch_disabled",
    }

    assert _apply_market_quote_fallback_for_side_in_place(row, "YES") is True
    assert _apply_market_quote_fallback_for_side_in_place(row, "NO") is True
    assert row["best_bid"] == 0.51
    assert row["best_ask"] == 0.51
    assert row["spread"] == 0.0
    assert row["bid_depth_topN"] == 0.0
    assert row["ask_depth_topN"] == 0.0
    assert row["best_bid_no"] == 0.49
    assert row["best_ask_no"] == 0.49
    assert row["spread_no"] == 0.0
    assert row["ask_missing_reason_yes"] == "gamma_price_fallback"
    assert row["ask_missing_reason_no"] == "gamma_price_fallback"


def test_hydrate_distribution_market_fields_from_gamma_price() -> None:
    """The hydrator fills market fields; it stopped scoring edge at 7.16.

    ``signal_edge_round``/``edge_score_yes``/``edge_signal_lcb_yes`` left
    ui/server entirely in the 2026-07-16 refactor and live in
    scan_weather_market now, so this asserted four keys the hydrator has not
    written since.  What it does owe from a gamma price is the quote pair,
    the zero spread and the fallback reason -- on every row, through the
    distribution, which is what distinguishes it from
    _apply_market_quote_fallback_for_side_in_place above.
    """

    conn = sqlite3.connect(":memory:")
    distribution = [
        {
            "outcome": "35°C",
            "yes_price": 0.51,
            "no_price": 0.49,
            "q_round": 0.55,
            "q_raw": 0.55,
            "q_calibrated": 0.56,
            "time_trend": 0.4,
            "time_freshness": 1.0,
            "structure_factor": 1.0,
            "ask_missing_reason_yes": "orderbook_fetch_disabled",
            "ask_missing_reason_no": "orderbook_fetch_disabled",
        }
    ]

    changed = _hydrate_distribution_market_fields_in_place(
        conn,
        distribution,
        "test-slug",
        "2026-04-15",
        "2026-04-15",
    )

    row = distribution[0]
    assert changed is True
    assert row["best_bid"] == 0.51
    assert row["best_ask"] == 0.51
    assert row["best_bid_no"] == 0.49
    assert row["best_ask_no"] == 0.49
    assert row["spread"] == 0.0
    assert row["spread_no"] == 0.0
    assert row["ask_missing_reason_yes"] == "gamma_price_fallback"
    assert row["ask_missing_reason_no"] == "gamma_price_fallback"
    assert row.get("event_date") == "2026-04-15"
    assert row.get("run_date") == "2026-04-15"
    assert "signal_edge_round" not in row
    assert "edge_score_yes" not in row


def test_hydrate_distribution_leaves_quoted_rows_and_does_not_price_execution() -> None:
    """The hydrator fills missing quotes; it does not compute execution cost.

    This used to assert ``exec_entry_cost_no``/``exec_edge_entry_no`` off a
    top-N depth fallback.  That pricing left ui/server in the 2026-07-16
    refactor and is computed in scan_weather_market's analyze_event now, so
    the hydrator writes neither.  A row that already carries its own NO quote
    has nothing for the hydrator to fill, which is what ``changed is False``
    says.
    """

    conn = sqlite3.connect(":memory:")
    distribution = [
        {
            "outcome": "72-73°F",
            "best_bid_no": 0.69,
            "best_ask_no": 0.72,
            "spread_no": 0.03,
            "ask_missing_reason_no": "orderbook_fetch_disabled",
            "ask_depth_total_no": 0.0,
            "ask_depth_topN_no": 125.87,
            "entry_size_no": 10.0,
            "q_round": 0.29115020873921034,
            "q_raw": 0.29115020873921034,
            "q_calibrated": 0.29115020873921034,
            "time_trend": 0.4,
            "time_freshness": 1.0,
            "structure_factor": 1.0,
        }
    ]

    changed = _hydrate_distribution_market_fields_in_place(
        conn,
        distribution,
        "test-slug",
        "2026-04-20",
        "2026-04-18",
    )

    row = distribution[0]
    assert changed is False
    # The quotes it came in with, untouched.
    assert row["best_bid_no"] == 0.69
    assert row["best_ask_no"] == 0.72
    assert row["spread_no"] == 0.03
    assert row["ask_depth_total_no"] == 0.0
    assert row["ask_depth_topN_no"] == 125.87
    assert "exec_entry_cost_no" not in row
    assert "exec_edge_entry_no" not in row
    # Only the identity the hydrator stamps on every row it sees.
    assert row["event_date"] == "2026-04-20"
    assert row["run_date"] == "2026-04-18"


def test_display_probability_dead_yes_market_does_not_overwrite_q_calibrated() -> None:
    distribution = [
        {
            "outcome": "23°C or below",
            "best_bid": 0.0,
            "best_ask": 0.001,
            "q_calibrated": 0.1150604884000075,
            "q_raw": 0.21211483132270598,
        },
        {
            "outcome": "29°C",
            "best_bid": 0.001,
            "best_ask": 0.008,
            "q_calibrated": 0.09291102874486969,
            "q_raw": 0.07342436468974675,
        },
        {
            "outcome": "30°C",
            "best_bid": 0.0,
            "best_ask": 1.0,
            "q_calibrated": 0.13347561063664293,
            "q_raw": 0.21211483132270598,
        },
        {
            "outcome": "31°C",
            "best_bid": 0.24,
            "best_ask": 0.34,
            "q_calibrated": 0.11256598965890242,
            "q_raw": 0.19156618441236475,
        },
    ]

    changed = _apply_display_probability_fields_in_place(distribution)

    dead_row = distribution[0]
    near_dead_row = distribution[1]
    invalid_row = distribution[2]
    live_row = distribution[3]
    assert changed is True
    assert dead_row["display_probability_market_dead"] is True
    assert dead_row["display_probability_source"] == "dead_yes_market_zero"
    assert dead_row["display_probability"] == 0.0
    assert dead_row["display_probability_normalized_source"] == "display_probability"
    assert dead_row["q_calibrated"] == 0.1150604884000075
    assert near_dead_row["display_probability_market_dead"] is True
    assert near_dead_row["display_probability_source"] == "dead_yes_market_zero"
    assert near_dead_row["display_probability"] == 0.0
    assert near_dead_row["q_calibrated"] == 0.09291102874486969
    assert invalid_row["display_probability_market_invalid"] is True
    assert invalid_row["display_probability_source"] == "invalid_yes_market_quote"
    assert invalid_row["display_probability"] is None
    assert invalid_row["display_probability_na_reason"] == "yes_ask>=0.99,yes_bid<=0.001"
    assert invalid_row["display_probability_normalized"] is None
    assert invalid_row["display_probability_normalized_na_reason"] == "market_quote_invalid"
    assert invalid_row["q_calibrated"] == 0.13347561063664293
    assert live_row["display_probability_market_dead"] is False
    assert live_row["display_probability_market_invalid"] is False
    assert live_row["display_probability_source"] == "model_q_calibrated"
    assert live_row["display_probability"] == 0.11256598965890242
    norm_total = sum(
        row["display_probability_normalized"]
        for row in (dead_row, near_dead_row, live_row)
    )
    assert math.isclose(norm_total, 1.0, rel_tol=0.0, abs_tol=1e-12)
    assert dead_row["display_probability_normalized"] == 0.0
    assert near_dead_row["display_probability_normalized"] == 0.0
    assert live_row["display_probability_normalized"] == 1.0


def test_load_latest_snapshot_respects_run_date_ceiling() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES
        ('2026-04-14', '2026-04-15', 'slug-a', '{\"rows\": []}', '{}', '2026-04-14T12:00:00+00:00'),
        ('2026-04-15', '2026-04-15', 'slug-a', '{\"rows\": []}', '{}', '2026-04-15T12:00:00+00:00')
        """
    )

    payload, _meta, created_at, snap_id = _load_latest_snapshot(
        conn,
        "model_snapshots",
        "slug-a",
        "2026-04-15",
        run_date_max="2026-04-14",
    )

    assert payload == {"rows": []}
    assert created_at == "2026-04-14T12:00:00+00:00"
    assert snap_id == 1


def test_load_latest_snapshot_respects_created_at_window() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES
        ('2026-06-18', '2026-06-20', 'slug-a', '{\"rows\": [{\"outcome\":\"28°C\"}]}', '{}', '2026-06-18T01:00:00+00:00'),
        ('2026-06-19', '2026-06-20', 'slug-a', '{\"rows\": [{\"outcome\":\"29°C\"}]}', '{}', '2026-06-19T01:00:00+00:00')
        """
    )

    payload, _meta, created_at, snap_id = _load_latest_snapshot(
        conn,
        "model_snapshots",
        "slug-a",
        "2026-06-20",
        created_at_min="2026-06-17T16:00:00+00:00",
        created_at_max="2026-06-18T16:00:00+00:00",
    )

    assert payload == {"rows": [{"outcome": "28°C"}]}
    assert created_at == "2026-06-18T01:00:00+00:00"
    assert snap_id == 1


def test_market_tick_payload_respects_created_at_window() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_market_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_date TEXT,
            slug TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            spread REAL,
            bid_depth_topN REAL,
            ask_depth_topN REAL,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO event_market_ticks(event_date, slug, outcome, side, best_bid, best_ask, created_at)
        VALUES
        ('2026-06-20', 'slug-a', '29°C', 'YES', 0.11, 0.12, '2026-06-18T01:00:00+00:00'),
        ('2026-06-20', 'slug-a', '29°C', 'NO', 0.88, 0.89, '2026-06-18T01:00:00+00:00'),
        ('2026-06-20', 'slug-a', '29°C', 'YES', 0.91, 0.92, '2026-06-19T01:00:00+00:00'),
        ('2026-06-20', 'slug-a', '29°C', 'NO', 0.08, 0.09, '2026-06-19T01:00:00+00:00')
        """
    )

    payload, latest_at = _market_snapshot_payload_from_latest_ticks(
        conn,
        "slug-a",
        "2026-06-20",
        outcomes=["29°C"],
        created_at_min="2026-06-17T16:00:00+00:00",
        created_at_max="2026-06-18T16:00:00+00:00",
    )

    assert latest_at == "2026-06-18T01:00:00+00:00"
    assert payload is not None
    assert payload["rows"][0]["best_bid"] == 0.11
    assert payload["rows"][0]["best_ask_no"] == 0.89
