import sqlite3
import math

from ui.server import (
    _apply_market_quote_fallback_for_side_in_place,
    _hydrate_distribution_market_fields_in_place,
    _load_latest_snapshot,
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
