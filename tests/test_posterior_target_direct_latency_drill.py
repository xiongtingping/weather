import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "posterior_target_direct_latency_drill.py"
spec = importlib.util.spec_from_file_location("posterior_target_direct_latency_drill", SCRIPT_PATH)
drill_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(drill_mod)


def _create_drill_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            model_snapshot_id INTEGER,
            market_snapshot_id INTEGER,
            composer_version TEXT,
            market_updated_at TEXT,
            model_updated_at TEXT,
            market_stale_sec INTEGER,
            model_cache_age_sec INTEGER,
            best_side TEXT,
            best_outcome TEXT,
            best_edge_score REAL,
            best_signal_edge_entry REAL,
            fact_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_latest (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (slug, event_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_summary_cache (
            fact_id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            run_date TEXT,
            fact_as_of TEXT,
            summary_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE market_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            yes_token TEXT,
            no_token TEXT,
            market_active INTEGER,
            market_closed INTEGER,
            market_archived INTEGER,
            market_accepting_orders INTEGER,
            market_enable_orderbook INTEGER,
            updated_at TEXT
        )
        """
    )
    conn.execute("CREATE TABLE auto_trade_jobs (source TEXT)")
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
            run_id INTEGER,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            title TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            decided_state TEXT,
            will_order INTEGER,
            reason_codes_json TEXT,
            reasons_json TEXT,
            details_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE edge_trades (
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            state TEXT,
            entry_at TEXT,
            last_order_id TEXT,
            entry_price REAL,
            entry_size REAL,
            last_action TEXT,
            last_action_price REAL,
            last_action_size REAL,
            last_action_order_type TEXT,
            last_action_at TEXT,
            last_status TEXT,
            position_size REAL,
            avg_price REAL,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_market_ticks (
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            mid REAL,
            spread REAL,
            bid_depth_topN REAL,
            ask_depth_topN REAL,
            source TEXT,
            source_detail TEXT,
            created_at TEXT
        )
        """
    )
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-3-2026",
        "event_date": "2026-07-03",
        "run_date": "2026-07-03",
        "fact_as_of": "2026-07-03T08:55:00Z",
        "prior_distribution": {"30": 0.5, "31": 0.5},
        "prior_na_reason": "",
        "prior_meta": {
            "schema": "temperature_prior_meta_v1",
            "state": "available",
            "source": "unit_test_settled_history_prior",
            "boundary": "historical_climate_date_site_terrain_weather_type_only_no_forecast_no_metar_no_market",
            "sample_count": 10,
            "shrinkage_target": "broader_settled_history",
            "date_window_days": 15,
            "prior_na_reason": "",
        },
        "distribution": [
            {"outcome": "30", "q_temperature_bucket_posterior": 0.58, "edge_score_yes": 0.03},
            {"outcome": "31", "q_temperature_bucket_posterior": 0.42, "edge_score_yes": 0.02},
        ],
    }
    cur = conn.execute(
        """
        INSERT INTO event_fact_history(
            run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-07-03",
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03",
            "2026-07-03T08:55:00Z",
            "fixture",
            json.dumps(payload),
            "2026-07-03T08:55:00Z",
        ),
    )
    fact_id = cur.lastrowid
    conn.execute(
        "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES (?, ?, ?, ?)",
        (
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03",
            fact_id,
            "2026-07-03T08:55:00Z",
        ),
    )
    conn.commit()
    conn.close()


def test_posterior_target_drill_fact_prepare_uses_writer_append_not_update(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_drill_db(db_path)

    result = drill_mod._prepare_posterior_target_scenario(
        db_path,
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        target_bucket="31",
        old_bucket="30",
        target_probability=0.99,
        previous_probability=0.42,
        target_ask=0.88,
        old_bid=0.39,
    )

    assert result["ok"] is True
    assert result["write_owner"] == "weather_fact_snapshot_writer"
    assert result["fact_id"] != result["previous_fact_id"]
    assert result["latest_repointed"] is True
    assert result["summary_cache_written"] is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 2
    latest = conn.execute("SELECT fact_id FROM event_fact_latest").fetchone()["fact_id"]
    assert latest == result["fact_id"]
    original = conn.execute(
        "SELECT fact_json FROM event_fact_history WHERE id = ?",
        (result["previous_fact_id"],),
    ).fetchone()
    original_payload = json.loads(original["fact_json"])
    assert original_payload["distribution"][1]["q_temperature_bucket_posterior"] == 0.42
    inserted = conn.execute(
        "SELECT fact_json FROM event_fact_history WHERE id = ?",
        (result["fact_id"],),
    ).fetchone()
    inserted_payload = json.loads(inserted["fact_json"])
    assert inserted_payload["posterior_target_direct_latency_drill_state"]["temporary_db_copy"] is True
    assert inserted_payload["posterior_ssot_fact"]["state"] == "available"
    assert inserted_payload["posterior_ssot_fact"]["target_bucket"] == "31"
    assert inserted_payload["posterior_ssot_fact"]["target_probability"] == 0.99
    assert inserted_payload["posterior_target_bucket_selection"]["target_bucket"] == "31"
    assert inserted_payload["posterior_target_bucket_selection"]["ssot_source"] == "posterior_ssot_fact"
    assert (
        inserted_payload["posterior_target_bucket_selection"]["target_probability_field"]
        == "posterior_ssot_fact.target_probability"
    )
    previous_decision = conn.execute(
        "SELECT details_json FROM auto_trade_decisions WHERE details_json LIKE '%drill_previous_state%'"
    ).fetchone()
    assert previous_decision is not None
    previous_details = json.loads(previous_decision["details_json"])
    assert previous_details["decision_ssot"] == "posterior_ssot_fact"
    assert previous_details["posterior_ssot_fact_used"] is True
    observations = inserted_payload["source_observations"]
    assert observations[0]["source_class"] == "diagnostic_current_source"
    assert inserted_payload["source_observation_ids"]
    assert observations[0]["source_observation_id"] == inserted_payload["source_observation_ids"][0]
    assert inserted_payload["source_latency_trace"][0]["source_observation_id"] == inserted_payload["source_observation_ids"][0]
    assert observations[0]["hard_eligible"] is False
    assert conn.execute(
        "SELECT COUNT(*) FROM event_fact_summary_cache WHERE fact_id = ?",
        (result["fact_id"],),
    ).fetchone()[0] == 1
    conn.close()


def test_posterior_target_drill_boundary_declares_no_live_writes() -> None:
    boundary = drill_mod.build_posterior_target_direct_latency_drill_boundary(
        source_db="/live/archive.db",
        tmp_db="/tmp/archive.db",
    )

    assert boundary["execution_mode"] == "temporary_db_copy_fake_live_drill_not_production_writer"
    assert boundary["measurement_scope"] == "synthetic_fact_signal_to_order_local_path_only"
    assert boundary["valid_p10_t1_to_t5_evidence"] is False
    assert boundary["guards"]["writes_archive_db"] is False
    assert boundary["guards"]["writes_temporary_archive_db_copy"] is True
    assert boundary["guards"]["writes_auto_trade_decisions"] is False
    assert boundary["guards"]["writes_temporary_auto_trade_decisions"] is True
    assert boundary["fact_writer_integration"]["write_owner"] == "weather_fact_snapshot_writer"


def test_posterior_target_drill_defaults_to_audit_only_without_temporary_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_drill_db(db_path)
    args = drill_mod.build_parser().parse_args(["--db", str(db_path)])

    result = drill_mod.run_drill(args)

    assert result["ok"] is True
    assert result["state"] == "audit_only_no_temporary_apply"
    assert result["boundary"]["guards"]["writes_temporary_archive_db_copy"] is True
    assert result["boundary"]["guards"]["writes_archive_db"] is False
