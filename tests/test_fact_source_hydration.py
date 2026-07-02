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


def test_hydrate_distribution_market_fields_recomputes_edge_from_gamma_price() -> None:
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
    assert row["best_ask"] == 0.51
    assert row["best_ask_no"] == 0.49
    assert math.isclose(row["signal_edge_round"], 0.04, rel_tol=0.0, abs_tol=1e-9)
    assert math.isclose(row["signal_edge_round_no"], -0.04, rel_tol=0.0, abs_tol=1e-9)
    assert row.get("edge_score_yes") is not None
    assert row.get("exec_edge_score_yes") is not None
    assert row.get("edge_signal_lcb_yes") is not None
    assert row.get("event_date") == "2026-04-15"
    assert row.get("run_date") == "2026-04-15"


def test_hydrate_distribution_uses_topn_depth_when_total_is_zero() -> None:
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
    assert changed is True
    assert math.isclose(row["exec_entry_cost_no"], 0.0105, rel_tol=0.0, abs_tol=1e-9)
    assert math.isclose(row["exec_edge_entry_no"], -0.021650208739210353, rel_tol=0.0, abs_tol=1e-9)


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
