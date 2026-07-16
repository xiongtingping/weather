import json
import sqlite3

from scripts import hard_source_direct_acceptance_audit as audit


def _create_minimal_job_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE auto_trade_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            source TEXT,
            state TEXT,
            priority INTEGER,
            payload_json TEXT,
            lease_until TEXT,
            retry_count INTEGER,
            max_retry INTEGER,
            next_retry_at TEXT,
            last_error TEXT,
            started_at TEXT,
            ended_at TEXT,
            run_id INTEGER,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            fact_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_fact_latest (
            slug TEXT,
            event_date TEXT,
            fact_id INTEGER,
            updated_at TEXT,
            PRIMARY KEY(slug, event_date)
        );
        """
    )


def test_acceptance_audit_ignores_legacy_hard_source_job_without_ssot_semantics(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_minimal_job_schema(conn)
    legacy_payload = {
        "include_event_keys": [
            {
                "slug": "highest-temperature-in-chengdu-on-june-19-2026",
                "event_date": "2026-06-19",
            }
        ],
        "hard_source_direct": {
            "source": "zuuu_metar_raw_awc",
            "target_bucket": "29°C or below",
        },
    }
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(
            id, run_date, source, state, priority, payload_json, run_id,
            created_at, started_at, ended_at, updated_at
        ) VALUES(1, ?, 'hard_source_direct', 'success', 20000, ?, 99, ?, ?, ?, ?)
        """,
        (
            "2026-06-19",
            json.dumps(legacy_payload),
            "2026-06-19T07:00:00+00:00",
            "2026-06-19T07:00:00+00:00",
            "2026-06-19T07:00:01+00:00",
            "2026-06-19T07:00:01+00:00",
        ),
    )
    conn.commit()
    conn.close()

    payload = audit.audit(
        db_path,
        slug="highest-temperature-in-chengdu-on-june-19-2026",
        event_date="2026-06-19",
        since="2026-06-19T00:00:00+00:00",
    )

    assert payload["ok"] is False
    assert payload["blockers"] == ["hard_source_ssot_job_missing"]
    assert payload["warnings"] == ["legacy_hard_source_job_ignored"]
    assert payload["details"]["legacy_hard_source_job_ignored"]["target_bucket"] == "29°C or below"


def test_acceptance_audit_reports_current_decision_window_from_latest_fact(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-19-2026"
    event_date = "2026-06-19"
    conn = sqlite3.connect(db_path)
    _create_minimal_job_schema(conn)
    fact_payload = {
        "slug": slug,
        "event_date": event_date,
        "event_tz": "Asia/Shanghai",
        "weather_likelihood_features": {
            "wu_forecast_peak_at": "2026-06-19T17:00:00+08:00",
            "wu_forecast_peak_time_source": "wunderground_hourly_forecast",
        },
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            event_date,
            slug,
            event_date,
            "2026-06-19T07:00:00+00:00",
            json.dumps(fact_payload),
            "2026-06-19T07:00:00+00:00",
        ),
    )
    fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES(?, ?, ?, ?)
        """,
        (slug, event_date, fact_id, "2026-06-19T07:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_MODE", "wu_forecast")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_PEAK_LEAD_HOURS", "1.0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_PEAK_POST_HOURS", "1.5")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE", event_date)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "15.5")

    payload = audit.audit(db_path, slug=slug, event_date=event_date)

    window = payload["details"]["current_decision_window"]
    assert payload["ok"] is False
    assert window["available"] is True
    assert window["allowed"] is False
    assert window["reason"] == "outside_peak_window"
    assert window["peak_hour_local"] == 17.0
    assert window["start_local_hour"] == 16.0
    assert window["end_local_hour"] == 18.5


def test_acceptance_audit_reports_latest_market_prefetch_runtime(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    log_path = tmp_path / "market_prefetch.log"
    conn = sqlite3.connect(db_path)
    _create_minimal_job_schema(conn)
    conn.commit()
    conn.close()
    log_path.write_text(
        "\n".join(
            [
                "[market_refresh_event_timing] rows=3/6",
                json.dumps(
                    {
                        "ok": True,
                        "status": "market_prefetch_refreshed",
                        "slug": "highest-temperature-in-chengdu-on-june-19-2026",
                        "event_date": "2026-06-19",
                        "count": 3,
                        "created_at": "2026-06-19T08:00:00+00:00",
                        "error": "",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(audit, "MARKET_PREFETCH_LOG", log_path)

    payload = audit.audit(
        db_path,
        slug="highest-temperature-in-chengdu-on-june-19-2026",
        event_date="2026-06-19",
    )

    runtime = payload["details"]["market_prefetch_runtime"]
    assert runtime["available"] is True
    assert runtime["ok"] is True
    assert runtime["status"] == "market_prefetch_refreshed"
    assert runtime["count"] == 3


def test_market_prefetch_runtime_prefers_current_db_ticks(tmp_path):
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-19-2026"
    event_date = "2026-06-19"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_market_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    for outcome, side in [("32°C", "YES"), ("33°C", "YES"), ("33°C", "NO")]:
        conn.execute(
            """
            INSERT INTO event_market_ticks(
                run_date, event_date, slug, outcome, side, best_bid, best_ask,
                source, created_at
            ) VALUES(?, ?, ?, ?, ?, 0.1, 0.2, 'test', ?)
            """,
            (event_date, event_date, slug, outcome, side, "2026-06-19T08:00:00+00:00"),
        )
    conn.commit()

    runtime = audit._latest_market_prefetch_runtime_from_db(
        conn,
        slug=slug,
        event_date=event_date,
    )

    assert runtime["available"] is True
    assert runtime["ok"] is True
    assert runtime["source"] == "event_market_ticks"
    assert runtime["status"] == "market_prefetch_ticks_present"
    assert runtime["count"] == 3
    assert runtime["outcome_count"] == 2
    assert runtime["outcome_side_count"] == 3


def test_selected_runtime_config_keeps_hard_source_worker_prefetch_separate(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "export PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS=1",
                "export PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS=16",
                "export PM_AUTO_TRADE_ENTRY_PREFETCH_BUDGET_SEC=24",
            ]
        ),
        encoding="utf-8",
    )
    for name in audit.RUNTIME_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)

    config = audit._selected_runtime_config(env_path)

    assert config["general_worker_prefetch"] == {
        "market_ticks_enabled": True,
        "max_attempts": "16",
        "budget_sec": "24",
    }
    assert config["inline_worker_prefetch"]["market_ticks_enabled"] is False
    assert config["inline_worker_prefetch"]["max_attempts"] == "0"
    assert config["inline_worker_prefetch"]["budget_sec"] == "0"


def test_selected_runtime_config_reports_metar_pws_polling(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "PM_HARD_SOURCE_METAR_PWS_ENABLED=1",
                "PM_HARD_SOURCE_METAR_IEM_PEAK_POLL_SEC=31",
                "PM_HARD_SOURCE_METAR_IEM_NORMAL_POLL_SEC=301",
                "PM_HARD_SOURCE_METAR_AVWX_PEAK_POLL_SEC=16",
                "PM_HARD_SOURCE_METAR_AVWX_NORMAL_POLL_SEC=61",
                "PM_HARD_SOURCE_METAR_CHECKWX_PEAK_POLL_SEC=17",
                "PM_HARD_SOURCE_METAR_CHECKWX_NORMAL_POLL_SEC=62",
                "PM_HARD_SOURCE_TWC_PWS_RAPID_PEAK_POLL_SEC=11",
                "PM_HARD_SOURCE_TWC_PWS_RAPID_NORMAL_POLL_SEC=63",
            ]
        ),
        encoding="utf-8",
    )
    for name in audit.RUNTIME_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)

    config = audit._selected_runtime_config(env_path)
    polling = config["metar_pws_multi_source_polling"]

    assert polling["enabled"] is True
    assert polling["iem_noaaport"]["peak_poll_sec"] == "31"
    assert polling["iem_noaaport"]["normal_poll_sec"] == "301"
    assert polling["avwx"]["peak_poll_sec"] == "16"
    assert polling["avwx"]["normal_poll_sec"] == "61"
    assert polling["checkwx"]["peak_poll_sec"] == "17"
    assert polling["checkwx"]["normal_poll_sec"] == "62"
    assert polling["twc_pws_rapid"]["peak_poll_sec"] == "11"
    assert polling["twc_pws_rapid"]["normal_poll_sec"] == "63"


def test_hard_source_lock_consistency_contract_declares_final_lock_boundary():
    contract = audit.hard_source_lock_consistency_contract()

    assert contract["schema"] == "hard_source_lock_consistency_contract_v1"
    assert contract["read_only"] is True
    assert contract["scope"] == "awc_hard_source_final_bucket_lock"
    assert "target_probability_is_one_hot" in contract["final_lock_requirements"]
    assert "non-final hard-source observations are lower-bound constraints" in contract["non_final_boundary"]
    assert contract["guards"]["writes_archive_db"] is False
    assert contract["guards"]["places_orders"] is False
    assert contract["guards"]["upgrades_weak_sources_to_hard_lock"] is False


def test_hard_source_lock_consistency_rejects_final_lock_that_is_not_one_hot():
    payload = {
        "hard_source_posterior_override_meta": {"final_bucket_lock": True},
        "distribution": [
            {"outcome": "30°C", "q_bayes_posterior": 0.0},
            {
                "outcome": "31°C",
                "q_bayes_posterior": 0.8,
                "hard_source_posterior_override_final_bucket_lock": True,
            },
            {"outcome": "32°C", "q_bayes_posterior": 0.2},
        ],
    }

    status = audit._hard_source_lock_consistency_status(payload, "31°C")

    assert status["ok"] is False
    assert status["state"] == "final_locked"
    assert status["reason"] == "final_bucket_lock_not_one_hot"
    assert "target_probability_is_one_hot" in status["missing_requirements"]
    assert "all_non_target_buckets_zero" in status["missing_requirements"]


def test_hard_source_lock_consistency_does_not_one_hot_non_final_observation():
    payload = {
        "distribution": [
            {"outcome": "30°C", "q_bayes_posterior": 0.0},
            {"outcome": "31°C", "q_bayes_posterior": 0.4},
            {"outcome": "32°C", "q_bayes_posterior": 0.6},
        ],
    }

    status = audit._hard_source_lock_consistency_status(payload, "32°C")

    assert status["ok"] is True
    assert status["state"] == "not_final_locked"
    assert status["reason"] == "non_final_hard_source_lower_bound_constraint_audited_separately"
