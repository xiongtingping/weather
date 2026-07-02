import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ui.server as server
from ui.server import (
    _build_precipitation_evidence_coverage,
    _compose_fact_from_snapshots,
    _snapshot_run_date_ceiling_for_event,
    query_latest_event_run_for_run_date,
)


def test_snapshot_run_date_ceiling_promotes_current_day_event() -> None:
    assert (
        _snapshot_run_date_ceiling_for_event(
            "2026-06-21",
            "2026-06-22",
            today_key="2026-06-22",
        )
        == "2026-06-22"
    )
    assert (
        _snapshot_run_date_ceiling_for_event(
            "2026-06-21",
            "2026-06-22",
            today_key="2026-06-23",
        )
        == "2026-06-21"
    )


def test_compose_fact_from_snapshots_returns_none_when_snapshot_times_mismatch() -> None:
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
        CREATE TABLE market_snapshots (
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
        VALUES ('2026-04-15', '2026-04-15', 'slug-a', '{"rows":[{"outcome":"35°C","q_round":0.55,"q_raw":0.55,"q_calibrated":0.56}]}', '{}', '2026-04-15T15:00:00+00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES ('2026-04-14', '2026-04-15', 'slug-a', '{"rows":[{"outcome":"35°C","best_bid":0.50,"best_ask":0.51}]}', '{}', '2026-04-15T02:00:00+00:00')
        """
    )

    composed = _compose_fact_from_snapshots(
        conn,
        "2026-04-15",
        "slug-a",
        "2026-04-15",
        {},
        {},
        {},
        {},
        platt=None,
    )

    assert composed is None


def test_compose_fact_from_snapshots_falls_back_to_synced_market_snapshot() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        """
    )
    slug = "highest-temperature-in-chengdu-on-june-21-2026"
    event_date = "2026-06-21"
    model_payload = {
        "title": "Highest temperature in Chengdu on June 21?",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "rounding_rule": "nearest",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "event_end_utc": "2026-06-21T12:00:00Z",
        "rows": [{"outcome": "25°C or below", "q_round": 0.7, "q_raw": 0.7, "q_bayes_posterior": 0.7}],
    }
    meta = {
        "title": "Highest temperature in Chengdu on June 21?",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "rounding_rule": "nearest",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "event_end_utc": "2026-06-21T12:00:00Z",
    }
    conn.execute(
        """
        INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("2026-06-20", event_date, slug, json.dumps(model_payload), json.dumps(meta), "2026-06-20T03:45:04+00:00"),
    )
    for created_at, bid in (
        ("2026-06-20T05:32:52+00:00", 0.51),
        ("2026-06-20T11:53:39+00:00", 0.22),
    ):
        conn.execute(
            """
            INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-20",
                event_date,
                slug,
                json.dumps({"rows": [{"outcome": "25°C or below", "best_bid": bid, "best_ask": bid + 0.02}]}),
                json.dumps(meta),
                created_at,
            ),
        )

    composed = _compose_fact_from_snapshots(
        conn,
        "2026-06-20",
        slug,
        event_date,
        {},
        {},
        {},
        {},
        platt=None,
        snapshot_created_at_min="2026-06-19T16:00:00+00:00",
        snapshot_created_at_max="2026-06-20T16:00:00+00:00",
        allow_event_run_model_candidate=False,
        allow_latest_fact_overlay=False,
        allow_settlement_status_overlay=False,
    )

    assert composed is not None
    item, run_payload, as_of = composed
    assert as_of == "2026-06-20T05:32:52+00:00"
    assert run_payload["score_composer"]["market_snapshot_at"] == "2026-06-20T05:32:52+00:00"
    assert item["distribution"][0]["best_bid"] == 0.51


def test_query_latest_event_run_for_run_date_uses_exact_run_date() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            created_at TEXT,
            payload_json TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO event_runs(run_date, event_date, slug, created_at, payload_json)
        VALUES
        ('2026-04-14', '2026-04-15', 'slug-a', '2026-04-15T02:00:00+00:00', '{}'),
        ('2026-04-15', '2026-04-15', 'slug-a', '2026-04-15T15:00:00+00:00', '{}')
        """
    )

    row = query_latest_event_run_for_run_date(conn, "slug-a", "2026-04-15", "2026-04-15")

    assert row is not None
    assert row["run_date"] == "2026-04-15"
    assert row["created_at"] == "2026-04-15T15:00:00+00:00"


def test_upsert_event_fact_advances_fact_as_of_from_market_tick() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-24-2026"

    fact_id = server.upsert_event_fact_from_run_item(
        conn,
        run_date="2026-06-23",
        slug=slug,
        event_date="2026-06-24",
        run_created_at="2026-06-23T00:55:45+00:00",
        run_payload={"score_composer": {"model_snapshot_id": 1039, "market_snapshot_id": None}},
        item={
            "slug": slug,
            "event_date": "2026-06-24",
            "run_date": "2026-06-23",
            "city": "Chengdu",
            "station_id": "ZUUU",
            "market_updated_at": "2026-06-23T10:12:44+00:00",
            "market_updated_source": "tick",
            "distribution": [
                {
                    "outcome": "29°C",
                    "q_round": 0.42,
                    "q_bayes_posterior": 0.42,
                    "best_bid": 0.31,
                    "best_ask": 0.33,
                }
            ],
        },
    )

    assert fact_id
    row = conn.execute(
        """
        SELECT h.fact_as_of, h.market_updated_at, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, "2026-06-24"),
    ).fetchone()
    payload = json.loads(row["fact_json"])
    assert row["fact_as_of"] == "2026-06-23T10:12:44+00:00"
    assert row["market_updated_at"] == "2026-06-23T10:12:44+00:00"
    assert payload["fact_provenance"]["fact_as_of_advanced_by"] == "market_updated_at"
    assert payload["fact_provenance"]["fact_as_of_base"] == "2026-06-23T00:55:45+00:00"


def test_materialize_first_prediction_lead_day_facts_appends_history_only() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    conn.executescript(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_daily_temps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            title TEXT,
            city TEXT,
            station_id TEXT,
            settlement_actual REAL,
            created_at TEXT
        );
        """
    )
    slug = "highest-temperature-in-chengdu-on-june-20-2026"
    event_date = "2026-06-20"
    model_payload = {
        "rows": [
            {
                "outcome": "29°C",
                "q_round": 0.7,
                "q_raw": 0.7,
                "q_bayes_posterior": 0.7,
                "external_archive": True,
                "history_only": True,
                "model_snapshot_source": "open_meteo_single_runs",
                "per_model_q": {
                    "gfs_seamless": {"q_round": 0.7, "dir_value_c": 29.0},
                },
            },
            {
                "outcome": "30°C",
                "q_round": 0.3,
                "q_raw": 0.3,
                "q_bayes_posterior": 0.3,
                "external_archive": True,
                "history_only": True,
                "model_snapshot_source": "open_meteo_single_runs",
                "per_model_q": {
                    "gfs_seamless": {"q_round": 0.3, "dir_value_c": 30.0},
                },
            },
        ],
        "title": "Highest temperature in Chengdu on June 20?",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "rounding_rule": "nearest",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "event_end_utc": "2026-06-20T16:00:00+00:00",
    }
    meta = {
        "title": "Highest temperature in Chengdu",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "rounding_rule": "nearest",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "event_end_utc": "2026-06-20T16:00:00+00:00",
    }
    market_payload = {
        "rows": [
            {"outcome": "29°C", "best_bid": 0.6, "best_ask": 0.62, "best_bid_no": 0.38, "best_ask_no": 0.4},
            {"outcome": "30°C", "best_bid": 0.2, "best_ask": 0.22, "best_bid_no": 0.78, "best_ask_no": 0.8},
        ]
    }
    conn.execute(
        """
        INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-19",
            event_date,
            slug,
            json.dumps(model_payload),
            json.dumps(meta),
            "2026-06-19T01:00:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-19",
            event_date,
            slug,
            json.dumps(market_payload),
            json.dumps(meta),
            "2026-06-19T01:05:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO event_daily_temps(run_date, event_date, slug, title, city, station_id, settlement_actual, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-21",
            event_date,
            slug,
            "Highest temperature in Chengdu",
            "Chengdu",
            "ZUUU",
            29.0,
            "2026-06-21T01:00:00+00:00",
        ),
    )

    preview = server.materialize_first_prediction_lead_day_facts(
        conn,
        start_date=event_date,
        end_date=event_date,
        lead_days=("D1",),
        apply=False,
    )
    applied = server.materialize_first_prediction_lead_day_facts(
        conn,
        start_date=event_date,
        end_date=event_date,
        lead_days=("D1",),
        apply=True,
    )

    assert preview["dry_run"] is True
    assert preview["stats"]["would_insert"] == 1
    assert applied["dry_run"] is False
    assert applied["stats"]["inserted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_fact_latest").fetchone()[0] == 0
    fact = conn.execute("SELECT run_date, fact_as_of, fact_json FROM event_fact_history").fetchone()
    payload = json.loads(fact["fact_json"])
    assert fact["run_date"] == "2026-06-19"
    assert fact["fact_as_of"] == "2026-06-19T01:05:00+00:00"
    assert payload["market_closed"] == 0
    assert payload.get("settlement_actual") is None
    assert payload["fact_provenance"]["historical_lead_day_materialization"]["lead_day"] == "D1"

    with server._FIRST_PREDICTION_HIT_RATE_CACHE_LOCK:
        server._FIRST_PREDICTION_HIT_RATE_CACHE.clear()
    stats = server.first_prediction_hit_rate_stats(conn, days=0, scope="chengdu_zuuu")
    assert stats["coverage"]["eligible_events"] == 1
    assert stats["by_source"]["gfs_seamless"]["lead_days"]["D1"]["count"] == 1
    lead_d1 = stats["by_source"]["gfs_seamless"]["lead_days"]["D1"]
    assert lead_d1["history_only_count"] == 1
    assert lead_d1["external_archive_count"] == 1
    assert lead_d1["sample_mix"]["external_history_count"] == 1
    assert lead_d1["sample_mix"]["items"][0]["role"] == "external_history_reconstruction"
    assert "contains_external_history_reconstruction" in lead_d1["diagnostic"]["codes"]
    assert lead_d1["diagnostic"]["sample_mix"]["has_external_history"] is True
    assert lead_d1["provenance_counts"]["external_history_reconstruction"]["count"] == 1
    assert lead_d1["provenance_counts"]["external_history_reconstruction"]["label"] == "外部历史重建"
    assert stats["coverage"]["sample_provenance_counts"]["external_history_reconstruction"]["lead_day_samples"] == 1
    assert stats["coverage"]["sample_provenance_mix"]["external_history_count"] == 1


def test_first_prediction_hit_rate_scans_past_post_cutoff_tail() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    conn.executescript(
        """
        CREATE TABLE event_daily_temps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            title TEXT,
            city TEXT,
            station_id TEXT,
            settlement_actual REAL,
            created_at TEXT
        );
        """
    )
    old_scan_limit = server.FIRST_PREDICTION_HIT_RATE_SCAN_ROWS_PER_LEAD
    server.FIRST_PREDICTION_HIT_RATE_SCAN_ROWS_PER_LEAD = 32
    try:
        slug = "highest-temperature-in-chengdu-on-june-20-2026"
        event_date = "2026-06-20"
        base_fact = {
            "slug": slug,
            "event_date": event_date,
            "title": "Highest temperature in Chengdu on June 20?",
            "city": "Chengdu",
            "station_id": "ZUUU",
            "rounding_rule": "nearest",
            "market_var": "temperature_2m_max",
            "market_unit": "C",
            "event_end_utc": "2026-06-20T12:00:00+00:00",
            "distribution": [
                {
                    "outcome": "29°C",
                    "q_round": 0.7,
                    "q_bayes_posterior": 0.7,
                    "per_model_q": {"gfs_seamless": {"q_round": 0.7, "dir_value_c": 29.0}},
                },
                {
                    "outcome": "30°C",
                    "q_round": 0.3,
                    "q_bayes_posterior": 0.3,
                    "per_model_q": {"gfs_seamless": {"q_round": 0.3, "dir_value_c": 30.0}},
                },
            ],
        }
        conn.execute(
            """
            INSERT INTO event_daily_temps(run_date, event_date, slug, title, city, station_id, settlement_actual, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-21",
                event_date,
                slug,
                "Highest temperature in Chengdu",
                "Chengdu",
                "ZUUU",
                29.0,
                "2026-06-21T00:00:00+00:00",
            ),
        )
        good_fact = dict(base_fact, market_closed=0, settlement_actual=None)
        conn.execute(
            """
            INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_date,
                slug,
                event_date,
                "2026-06-20T11:00:00+00:00",
                "test",
                json.dumps(good_fact),
                "2026-06-20T11:00:00+00:00",
            ),
        )
        for idx in range(20):
            bad_fact = dict(base_fact, market_closed=1, settlement_actual=29.0)
            ts = f"2026-06-20T12:{idx:02d}:00+00:00"
            conn.execute(
                """
                INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (event_date, slug, event_date, ts, "test", json.dumps(bad_fact), ts),
            )
        with server._FIRST_PREDICTION_HIT_RATE_CACHE_LOCK:
            server._FIRST_PREDICTION_HIT_RATE_CACHE.clear()
        stats = server.first_prediction_hit_rate_stats(conn, days=0, scope="chengdu_zuuu")
        assert stats["coverage"]["eligible_events"] == 1
        assert stats["by_source"]["gfs_seamless"]["lead_days"]["D0"]["count"] == 1
        assert stats["by_source"]["gfs_seamless"]["lead_days"]["D0"]["hit"] == 1
    finally:
        server.FIRST_PREDICTION_HIT_RATE_SCAN_ROWS_PER_LEAD = old_scan_limit
        with server._FIRST_PREDICTION_HIT_RATE_CACHE_LOCK:
            server._FIRST_PREDICTION_HIT_RATE_CACHE.clear()


def test_normalize_distribution_rows_applies_observed_hard_zero_posterior_constraints() -> None:
    rows = server.normalize_distribution_rows(
        [
            {
                "outcome": "24°C or below",
                "q_temperature_bucket_posterior": 0.24,
                "q_bayes_posterior": 0.24,
                "q_bayes_physical_likelihood_hard_zero": True,
                "q_bayes_physical_likelihood_hard_reasons": ["observed_today_max_above_bucket"],
                "q_bayes_physical_likelihood_observed_prob": 1e-12,
            },
            {
                "outcome": "25°C",
                "q_temperature_bucket_posterior": 0.22,
                "q_bayes_posterior": 0.22,
                "q_bayes_physical_likelihood_observed_prob": 0.99,
            },
            {
                "outcome": "26°C",
                "q_temperature_bucket_posterior": 0.20,
                "q_bayes_posterior": 0.20,
                "q_bayes_physical_likelihood_observed_prob": 0.01,
            },
        ]
    )

    by_label = {row["outcome"]: row for row in rows}
    assert by_label["24°C or below"]["q_temperature_bucket_posterior"] == 0.0
    assert by_label["24°C or below"]["q_bayes_posterior"] == 0.0
    assert by_label["24°C or below"]["q_temperature_bucket_posterior_pre_observed_constraints"] == 0.24
    assert by_label["24°C or below"]["q_posterior_observed_constraints_hard_zero"] is True
    assert by_label["25°C"]["q_temperature_bucket_posterior"] > 0.99


def test_precipitation_evidence_coverage_keeps_open_meteo_as_proxy() -> None:
    panel = _build_precipitation_evidence_coverage(
        {
            "weather_likelihood_features": {
                "observed_precip_rate_mm_per_h": 0.1,
                "precipitation_rate_mm_per_h": 0.1,
                "is_raining": True,
                "radar_qpe_proxy_source": "open_meteo_current_hourly_precip",
            },
            "weather_source_delay_deformation_alert": {
                "awc_latest_at": "2026-06-13T02:00:00+00:00",
            },
            "weather_source_lineage": {
                "zuuu_metar_raw_awc": {"provider": "NOAA AviationWeather"},
            },
        },
        [],
        fallback_updated_at="2026-06-13T02:10:00+00:00",
    )

    assert panel["strong_available"] is False
    assert panel["source_covered_count"] == 1
    assert panel["strong_source_count"] == 0
    awc = next(row for row in panel["sources"] if row["key"] == "awc_metar")
    assert awc["source_available"] is True
    assert awc["strong_available"] is False
    assert "real_radar_qpe_metar_or_verified_precip_missing" in awc["na_reason"]


def test_precipitation_evidence_coverage_marks_mrms_qpe_zero_as_strong() -> None:
    panel = _build_precipitation_evidence_coverage(
        {
            "weather_likelihood_features": {
                "radar_precip_rate_mm_per_h": 0.0,
                "qpe_precip_rate_mm_per_h": 0.0,
                "radar_qpe_proxy_source": "mrms_qpe",
                "radar_qpe_available": True,
                "radar_qpe_latest_at": "2026-05-31T13:02:00+00:00",
                "radar_qpe_value_source": "noaa_mrms_precip_rate_grib2_png",
            }
        },
        [],
        fallback_updated_at="2026-05-31T13:03:00+00:00",
    )

    assert panel["strong_available"] is True
    assert panel["strong_source_count"] == 1
    assert panel["no_precip_confirmed"] is True
    assert panel["strong_no_precip_count"] == 1
    assert panel["state"] == "strong_no_precip"
    mrms = next(row for row in panel["sources"] if row["key"] == "mrms_qpe")
    assert mrms["source_available"] is True
    assert mrms["strong_available"] is True
    assert mrms["no_precip_confirmed"] is True
    assert mrms["state"] == "strong_no_precip"
    assert mrms["rate_mm_per_h"] == 0.0


def test_precipitation_evidence_coverage_marks_awc_cavok_as_strong_no_precip() -> None:
    panel = _build_precipitation_evidence_coverage(
        {
            "weather_likelihood_features": {
                "source": "zuuu_metar_raw_awc",
                "zuuu_metar_raw_awc_available": True,
                "zuuu_metar_raw_awc_latest_at": "2026-06-13T02:00:00+00:00",
                "metar_no_precip_observed": True,
                "awc_metar_no_precip_observed": True,
                "metar_cavok_observed": True,
                "observed_precip_rate_mm_per_h": 0.0,
                "precipitation_rate_mm_per_h": 0.0,
                "radar_qpe_proxy_source": "zuuu_metar_raw_awc_cavok_no_precip",
            },
            "weather_source_lineage": {
                "zuuu_metar_raw_awc": {"provider": "NOAA AviationWeather"},
            },
        },
        [],
        fallback_updated_at="2026-06-13T02:10:00+00:00",
    )

    assert panel["strong_available"] is True
    assert panel["no_precip_confirmed"] is True
    assert panel["precip_confirmed"] is False
    assert panel["strong_source_count"] == 1
    assert panel["strong_no_precip_count"] == 1
    assert panel["state"] == "strong_no_precip"
    awc = next(row for row in panel["sources"] if row["key"] == "awc_metar")
    assert awc["source_available"] is True
    assert awc["strong_available"] is True
    assert awc["no_precip_confirmed"] is True
    assert awc["precip_present"] is False
    assert awc["state"] == "strong_no_precip"
    assert awc["source_field"] == "awc_metar_no_precip_observed"
    assert awc["rate_mm_per_h"] == 0.0


def test_compose_fact_uses_newer_event_run_model_payload_with_latest_market_snapshot() -> None:
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
        CREATE TABLE market_snapshots (
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
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            created_at TEXT,
            payload_json TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (
            '2026-04-15',
            '2026-04-15',
            'slug-a',
            '{"rows":[{"outcome":"29°C","q_round":0.20,"q_raw":0.20,"q_calibrated":0.21,"q_bayes_posterior":0.20}]}',
            '{"title":"Old model"}',
            '2026-04-15T10:00:00+00:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (
            '2026-04-15',
            '2026-04-15',
            'slug-a',
            '{"rows":[{"outcome":"29°C","best_bid":0.40,"best_ask":0.41,"best_bid_no":0.59,"best_ask_no":0.60}]}',
            '{}',
            '2026-04-15T10:06:00+00:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO event_runs(run_date, event_date, slug, created_at, payload_json)
        VALUES (
            '2026-04-15',
            '2026-04-15',
            'slug-a',
            '2026-04-15T10:05:00+00:00',
            '{"score_composer":{"model_snapshot_id":1,"model_snapshot_at":"2026-04-15T10:00:00+00:00"},"rows":[{"outcome":"29°C","q_round":0.70,"q_raw":0.70,"q_calibrated":0.71,"q_bayes_posterior":0.70,"best_bid":0.01,"best_ask":0.02,"best_bid_no":0.98,"best_ask_no":0.99}]}'
        )
        """
    )

    composed = _compose_fact_from_snapshots(
        conn,
        "2026-04-15",
        "slug-a",
        "2026-04-15",
        {},
        {},
        {},
        {},
        platt=None,
    )

    assert composed is not None
    item, _run_payload, as_of = composed
    row = item["distribution"][0]
    assert as_of == "2026-04-15T10:06:00+00:00"
    assert row["q_bayes_posterior"] == 0.70
    assert row["q_calibrated"] == 0.71
    assert row["best_bid"] == 0.40
    assert row["best_ask"] == 0.41
    assert item["fact_provenance"]["model_source"] == "event_runs"
    assert item["fact_provenance"]["event_run_id"] == 1
    assert item["fact_provenance"]["model_snapshot_id"] == 1


def test_compose_fact_from_snapshots_uses_current_day_snapshot_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(server, "_chengdu_today_date_key", lambda: "2026-06-22")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES
        ('2026-06-21', '2026-06-22', 'slug-a',
         '{"rows":[{"outcome":"33°C","q_round":0.03,"q_raw":0.03,"q_calibrated":0.03}]}',
         '{}', '2026-06-21T10:00:00+00:00'),
        ('2026-06-22', '2026-06-22', 'slug-a',
         '{"rows":[{"outcome":"33°C","q_round":0.42,"q_raw":0.42,"q_calibrated":0.43}]}',
         '{}', '2026-06-22T05:04:39+00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (
            '2026-06-22',
            '2026-06-22',
            'slug-a',
            '{"rows":[{"outcome":"33°C","best_bid":0.13,"best_ask":0.15}]}',
            '{}',
            '2026-06-22T05:05:00+00:00'
        )
        """
    )

    composed = _compose_fact_from_snapshots(
        conn,
        "2026-06-21",
        "slug-a",
        "2026-06-22",
        {},
        {},
        {},
        {},
        platt=None,
    )

    assert composed is not None
    item, run_payload, _as_of = composed
    row = item["distribution"][0]
    assert row["q_calibrated"] == 0.43
    assert row["best_ask"] == 0.15
    assert run_payload["score_composer"]["model_snapshot_id"] == 2
    assert run_payload["score_composer"]["snapshot_run_date_ceiling"] == "2026-06-22"


def test_compose_fact_from_snapshots_overlays_latest_weather_features_into_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            fact_json TEXT
        );
        CREATE TABLE event_fact_latest (
            slug TEXT,
            event_date TEXT,
            fact_id INTEGER,
            updated_at TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (
            '2026-04-15',
            '2026-04-15',
            'slug-a',
            '{"weather_likelihood_features":{"observed_today_max_c":29.0,"weather_likelihood_refreshed_at":"2026-04-15T10:00:00+00:00"},"rows":[{"outcome":"33°C","q_round":0.03,"q_raw":0.03,"q_calibrated":0.03,"observed_today_max_c":29.0}]}',
            '{}',
            '2026-04-15T10:00:00+00:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (
            '2026-04-15',
            '2026-04-15',
            'slug-a',
            '{"rows":[{"outcome":"33°C","best_bid":0.04,"best_ask":0.05}]}',
            '{}',
            '2026-04-15T10:01:00+00:00'
        )
        """
    )
    fact_json = {
        "weather_likelihood_features": {
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "temperature_momentum_effective_c_per_hour": 2.0,
            "temperature_momentum_realization_state": "sustained_warming",
            "zuuu_metar_raw_awc_latest_at": "2026-04-15T11:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "weather_likelihood_refreshed_at": "2026-04-15T11:05:00+00:00",
            "source": "zuuu_metar_raw_awc",
        },
        "distribution": [{"outcome": "33°C", "q_round": 0.03}],
    }
    cur = conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "2026-04-15",
            "slug-a",
            "2026-04-15",
            "2026-04-15T11:05:00+00:00",
            json.dumps(fact_json),
        ),
    )
    conn.execute(
        "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES (?, ?, ?, ?)",
        ("slug-a", "2026-04-15", int(cur.lastrowid), "2026-04-15T11:05:00+00:00"),
    )

    composed = _compose_fact_from_snapshots(
        conn,
        "2026-04-15",
        "slug-a",
        "2026-04-15",
        {},
        {},
        {},
        {},
        platt=None,
    )

    assert composed is not None
    item, run_payload, _as_of = composed
    row = item["distribution"][0]
    assert row["q_round"] == 0.03
    assert row["observed_today_max_c"] == 31.0
    assert row["observed_today_max_source"] == "zuuu_metar_raw_awc"
    assert row["temperature_momentum_effective_c_per_hour"] == 2.0
    assert row["weather_feature_overlay_applied"] is True
    assert item["weather_likelihood_features_compact"]["observed_today_max_c"] == 31.0
    assert run_payload["score_composer"]["weather_feature_overlay_applied"] is True


def test_compose_fact_from_snapshots_overlays_station_observations_into_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE station_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            station_id TEXT,
            resolution_source TEXT,
            source_kind TEXT,
            observed_at TEXT,
            published_at TEXT,
            value REAL,
            unit TEXT,
            market_var TEXT,
            trust_tier TEXT,
            source_confidence REAL,
            raw_json TEXT,
            created_at TEXT
        );
        """
    )
    slug = "highest-temperature-in-chengdu-on-june-19-2026"
    event_date = "2026-06-19"
    model_payload = {
        "title": "Highest temperature in Chengdu on June 19?",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "rounding_rule": "nearest",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rows": [
            {"outcome": "32°C", "q_round": 0.35, "q_raw": 0.35, "q_calibrated": 0.35},
            {"outcome": "34°C", "q_round": 0.12, "q_raw": 0.12, "q_calibrated": 0.12},
        ],
    }
    meta = {
        "title": "Highest temperature in Chengdu on June 19?",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "rounding_rule": "nearest",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
    }
    conn.execute(
        """
        INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("2026-06-19", event_date, slug, json.dumps(model_payload), json.dumps(meta), "2026-06-19T08:00:00+00:00"),
    )
    conn.execute(
        """
        INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-19",
            event_date,
            slug,
            json.dumps({"rows": [{"outcome": "32°C", "best_bid": 0.30, "best_ask": 0.32}, {"outcome": "34°C", "best_bid": 0.10, "best_ask": 0.12}]}),
            json.dumps(meta),
            "2026-06-19T08:01:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO station_observations(
            run_date, event_date, station_id, resolution_source, source_kind,
            observed_at, published_at, value, unit, market_var, trust_tier,
            source_confidence, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-19",
            event_date,
            "ZUUU",
            "https://example.test/history/ZUUU",
            "wunderground_history",
            "2026-06-19T08:00:00+00:00",
            "2026-06-19T08:00:00+00:00",
            34.0,
            "C",
            "temperature_2m_max",
            "settlement_source_nowcast",
            0.85,
            "2026-06-19T08:08:01+00:00",
        ),
    )

    composed = _compose_fact_from_snapshots(
        conn,
        "2026-06-19",
        slug,
        event_date,
        {},
        {},
        {},
        {},
        platt=None,
    )

    assert composed is not None
    item, run_payload, _as_of = composed
    row = item["distribution"][0]
    assert row["observed_today_max_c"] == 34.0
    assert row["observed_today_max_source"] == "station_observations.wunderground_history"
    assert item["weather_likelihood_features"]["station_observation_triggered"] is True
    assert item["weather_likelihood_features"]["wu_twc_observed_intraday_today_max_c"] == 34.0
    assert run_payload["score_composer"]["station_observation_overlay_applied"] is True


def test_compose_fact_from_snapshots_merge_fills_regime_fields_to_fact_top_level() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        """
    )
    model_payload = {
        "title": "Highest temperature in Chengdu on June 24?",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "rounding_rule": "whole_degrees",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "climo_regime": {"features": {"source": "unit_test_climo"}},
        "climo_regime_features": {
            "basin_heat_mechanism_state": "watch",
            "basin_heat_mechanism_model": {
                "state": "watch",
                "status": "partial_mechanism_support",
                "heat_uplift_index": 0.4625,
            },
        },
        "post_cool_rebound_regime_prior": {
            "state": "prior",
            "status": "regime_prior",
        },
        "post_cool_rebound_intraday_likelihood": {
            "state": "off",
            "status": "missing_observed_rebound",
        },
        "q_bayes_climo_regime_explanation": {
            "summary": {
                "basin_heat_mechanism_model": {
                    "state": "watch",
                    "status": "partial_mechanism_support",
                }
            }
        },
        "q_bayes_post_cool_rebound_regime_prior_state": "prior",
        "rows": [
            {
                "outcome": "30°C",
                "q_round": 0.42,
                "q_raw": 0.42,
                "q_calibrated": 0.42,
                "q_bayes_posterior": 0.42,
            }
        ],
    }
    market_payload = {"rows": [{"outcome": "30°C", "best_bid": 0.4, "best_ask": 0.43}]}
    conn.execute(
        """
        INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-24",
            "2026-06-24",
            "highest-temperature-in-chengdu-on-june-24-2026",
            json.dumps(model_payload),
            "{}",
            "2026-06-24T08:10:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-24",
            "2026-06-24",
            "highest-temperature-in-chengdu-on-june-24-2026",
            json.dumps(market_payload),
            "{}",
            "2026-06-24T08:11:00+00:00",
        ),
    )

    composed = _compose_fact_from_snapshots(
        conn,
        "2026-06-24",
        "highest-temperature-in-chengdu-on-june-24-2026",
        "2026-06-24",
        {},
        {},
        {},
        {},
        platt=None,
    )

    assert composed is not None
    item, _run_payload, _as_of = composed
    assert item["climo_regime_features"]["basin_heat_mechanism_state"] == "watch"
    assert item["climo_regime_features"]["basin_heat_mechanism_model"]["heat_uplift_index"] == 0.4625
    assert item["post_cool_rebound_regime_prior"]["state"] == "prior"
    assert item["q_bayes_post_cool_rebound_regime_prior_state"] == "prior"
    assert item["q_bayes_climo_regime_explanation"]["summary"]["basin_heat_mechanism_model"]["state"] == "watch"
    assert "climo_regime_features" in item["fact_provenance"]["regime_fields_merge_fill"]["fields"]


def test_regime_fact_passthrough_is_merge_fill_only() -> None:
    item = {
        "climo_regime_features": {
            "basin_heat_mechanism_state": "active",
            "existing_only": True,
        }
    }
    model_payload = {
        "climo_regime_features": {
            "basin_heat_mechanism_state": "watch",
            "basin_heat_mechanism_status": "partial_mechanism_support",
        },
        "post_cool_rebound_regime_prior": {"state": "prior"},
    }

    server._merge_fill_regime_fields_from_model_payload(item, model_payload)

    assert item["climo_regime_features"]["basin_heat_mechanism_state"] == "active"
    assert item["climo_regime_features"]["existing_only"] is True
    assert item["climo_regime_features"]["basin_heat_mechanism_status"] == "partial_mechanism_support"
    assert item["post_cool_rebound_regime_prior"]["state"] == "prior"
    assert item["fact_provenance"]["regime_fields_merge_fill"]["policy"] == "merge_fill_only_no_overwrite"


def test_compose_fact_from_snapshots_uses_event_runs_settlement_for_final_lock() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            settlement_actual REAL,
            market_closed INTEGER,
            created_at TEXT
        );
        """
    )
    model_payload = {
        "rounding_rule": "whole_degrees",
        "market_var": "temperature_2m_max",
        "weather_likelihood_features": {"market_var": "temperature_2m_max", "event_date": "2026-06-22"},
        "rows": [
            {"outcome": "32°C", "q_round": 0.80, "q_raw": 0.80, "q_bayes_posterior": 0.80},
            {"outcome": "33°C", "q_round": 0.15, "q_raw": 0.15, "q_bayes_posterior": 0.15},
            {"outcome": "34°C", "q_round": 0.05, "q_raw": 0.05, "q_bayes_posterior": 0.05},
        ],
    }
    market_payload = {
        "rows": [
            {"outcome": "32°C", "best_bid": 0.1, "best_ask": 0.2},
            {"outcome": "33°C", "best_bid": 0.9, "best_ask": 1.0},
            {"outcome": "34°C", "best_bid": 0.0, "best_ask": 0.1},
        ]
    }
    conn.execute(
        """
        INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-22",
            "2026-06-22",
            "slug-a",
            json.dumps(model_payload),
            json.dumps({"rounding_rule": "whole_degrees", "market_var": "temperature_2m_max", "tz": "Asia/Shanghai"}),
            "2026-06-22T09:00:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-22",
            "2026-06-22",
            "slug-a",
            json.dumps(market_payload),
            "{}",
            "2026-06-22T09:01:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO event_runs(run_date, event_date, slug, payload_json, meta_json, settlement_actual, market_closed, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-22",
            "2026-06-22",
            "slug-a",
            "{}",
            "{}",
            33.0,
            1,
            "2026-06-22T09:02:00+00:00",
        ),
    )

    composed = _compose_fact_from_snapshots(
        conn,
        "2026-06-22",
        "slug-a",
        "2026-06-22",
        {},
        {},
        {},
        {},
        platt=None,
    )

    assert composed is not None
    item, run_payload, _as_of = composed
    rows = {row["outcome"]: row for row in item["distribution"]}
    assert item["settlement_actual"] == 33.0
    assert item["market_closed"] == 1
    assert item["model_top_label"] == "33°C"
    assert rows["33°C"]["q_bayes_posterior"] == 1.0
    assert rows["32°C"]["q_bayes_posterior"] == 0.0
    assert item["hard_source_posterior_override"]["final_bucket_lock"] is True
    assert item["final_bucket_lock"] is True
    assert item["final_bucket"] == "33°C"
    assert item["hard_source_final_bucket_lock"] is True
    assert item["weather_likelihood_features"]["settlement_actual"] == 33.0
    assert item["weather_likelihood_features"]["final_bucket_lock"] is True
    assert item["weather_likelihood_features"]["final_bucket"] == "33°C"
    assert run_payload["score_composer"]["settlement_status_source"] == "event_runs.settlement_actual"


def test_compose_fact_from_snapshots_applies_latest_tick_overlay() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_market_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            spread REAL,
            bid_depth_topN REAL,
            ask_depth_topN REAL,
            created_at TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (
            '2026-04-15',
            '2026-04-15',
            'slug-a',
            '{"rows":[{"outcome":"33°C","q_round":0.70,"q_raw":0.70,"q_calibrated":0.71,"best_bid":0.01,"best_ask":0.02}]}',
            '{}',
            '2026-04-15T10:00:00+00:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (
            '2026-04-15',
            '2026-04-15',
            'slug-a',
            '{"rows":[{"outcome":"33°C","best_bid":0.04,"best_ask":0.05,"best_bid_no":0.94,"best_ask_no":0.95}]}',
            '{}',
            '2026-04-15T10:01:00+00:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO event_market_ticks(
            slug, event_date, outcome, side,
            best_bid, best_ask, spread, bid_depth_topN, ask_depth_topN, created_at
        )
        VALUES
        ('slug-a', '2026-04-15', '33°C', 'YES', 0.12, 0.15, 0.03, 10.0, 11.0, '2026-04-15T10:02:00+00:00'),
        ('slug-a', '2026-04-15', '33°C', 'NO', 0.83, 0.88, 0.05, 12.0, 13.0, '2026-04-15T10:02:00+00:00')
        """
    )

    composed = _compose_fact_from_snapshots(
        conn,
        "2026-04-15",
        "slug-a",
        "2026-04-15",
        {},
        {},
        {},
        {},
        platt=None,
    )

    assert composed is not None
    item, run_payload, as_of = composed
    row = item["distribution"][0]
    assert as_of == "2026-04-15T10:02:00+00:00"
    assert row["best_bid"] == 0.12
    assert row["best_ask"] == 0.15
    assert row["best_bid_no"] == 0.83
    assert row["best_ask_no"] == 0.88
    assert row["market_tick_override_yes"] is True
    assert row["market_tick_quote_overrode_existing_yes"] is True
    assert item["fact_provenance"]["market_source"] == "event_market_ticks"
    assert item["fact_provenance"]["market_tick_overlay_applied"] is True
    assert run_payload["score_composer"]["market_tick_overlay_at"] == "2026-04-15T10:02:00+00:00"


def test_snapshot_key_rows_for_run_date_uses_market_ticks_as_materialization_trigger() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_market_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            created_at TEXT
        );
        INSERT INTO event_market_ticks(run_date, slug, event_date, outcome, side, best_bid, best_ask, created_at)
        VALUES (
            '2026-06-23',
            'highest-temperature-in-chengdu-on-june-24-2026',
            '2026-06-24',
            '29°C',
            'YES',
            0.31,
            0.32,
            '2026-06-23T11:57:55+00:00'
        );
        """
    )

    rows = server._snapshot_key_rows_for_run_date(conn, "2026-06-23")

    assert (
        "highest-temperature-in-chengdu-on-june-24-2026",
        "2026-06-24",
        "2026-06-23",
    ) in rows


def test_compose_fact_from_snapshots_allows_fresh_tick_overlay_with_bounded_model_age(monkeypatch) -> None:
    monkeypatch.setenv("PM_SCORE_COMPOSE_WINDOW_SEC", "10800")
    monkeypatch.setenv("PM_SCORE_COMPOSE_TICK_MODEL_MAX_AGE_SEC", "86400")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_market_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            spread REAL,
            bid_depth_topN REAL,
            ask_depth_topN REAL,
            created_at TEXT
        );
        INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (
            '2026-06-23',
            '2026-06-24',
            'highest-temperature-in-chengdu-on-june-24-2026',
            '{"rows":[{"outcome":"29°C","q_round":0.42,"q_raw":0.42,"q_calibrated":0.43,"best_bid":0.02,"best_ask":0.03}]}',
            '{"title":"Highest temperature in Chengdu on June 24?","station_id":"ZUUU","market_var":"temperature_2m_max","market_unit":"C","rounding_rule":"whole_degrees"}',
            '2026-06-23T00:00:00+00:00'
        );
        INSERT INTO event_market_ticks(
            run_date, slug, event_date, outcome, side,
            best_bid, best_ask, spread, bid_depth_topN, ask_depth_topN, created_at
        )
        VALUES
        (
            '2026-06-23',
            'highest-temperature-in-chengdu-on-june-24-2026',
            '2026-06-24',
            '29°C',
            'YES',
            0.31,
            0.34,
            0.03,
            8.0,
            9.0,
            '2026-06-23T12:00:00+00:00'
        ),
        (
            '2026-06-23',
            'highest-temperature-in-chengdu-on-june-24-2026',
            '2026-06-24',
            '29°C',
            'NO',
            0.65,
            0.69,
            0.04,
            10.0,
            11.0,
            '2026-06-23T12:00:00+00:00'
        );
        """
    )

    composed = _compose_fact_from_snapshots(
        conn,
        "2026-06-23",
        "highest-temperature-in-chengdu-on-june-24-2026",
        "2026-06-24",
        {},
        {},
        {},
        {},
        platt=None,
    )

    assert composed is not None
    item, run_payload, as_of = composed
    row = item["distribution"][0]
    assert as_of == "2026-06-23T12:00:00+00:00"
    assert row["best_bid"] == 0.31
    assert row["best_ask"] == 0.34
    assert row["best_bid_no"] == 0.65
    assert row["best_ask_no"] == 0.69
    assert row["market_tick_override_yes"] is True
    assert item["fact_provenance"]["market_source"] == "event_market_ticks"
    assert run_payload["score_composer"]["market_tick_overlay_at"] == "2026-06-23T12:00:00+00:00"


def test_compose_live_snapshot_items_for_ui_marks_live_snapshot_source() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    old_keys = server._snapshot_key_rows_for_run_date
    old_trades = server.query_edge_trades
    old_fills = server.query_trade_fills
    old_orders = server.query_auto_orders
    old_ticks = server.query_market_tick_latest_for_keys
    old_compose = server._compose_fact_from_snapshots
    try:
        server._snapshot_key_rows_for_run_date = lambda _conn, _run_date: [
            ("slug-a", "2026-04-15", "2026-04-15")
        ]
        server.query_edge_trades = lambda _conn: {}
        server.query_trade_fills = lambda _conn: {}
        server.query_auto_orders = lambda _conn: {}
        server.query_market_tick_latest_for_keys = lambda _conn, _keys: {
            ("slug-a", "2026-04-15"): "2026-04-15T15:00:10+00:00"
        }

        def fake_compose(*_args, **_kwargs):
            return (
                {
                    "slug": "slug-a",
                    "event_date": "2026-04-15",
                    "distribution": [
                        {"outcome": "35°C", "best_bid": 0.50, "best_ask": 0.51}
                    ],
                    "fact_provenance": {"market_source": "event_market_ticks"},
                },
                {},
                "2026-04-15T15:00:10+00:00",
            )

        server._compose_fact_from_snapshots = fake_compose

        items = server._compose_live_snapshot_items_for_ui(
            conn,
            "2026-04-15",
            10,
            None,
            {("slug-a", "2026-04-15"): "2026-04-15T15:00:00+00:00"},
        )
    finally:
        server._snapshot_key_rows_for_run_date = old_keys
        server.query_edge_trades = old_trades
        server.query_trade_fills = old_fills
        server.query_auto_orders = old_orders
        server.query_market_tick_latest_for_keys = old_ticks
        server._compose_fact_from_snapshots = old_compose

    assert len(items) == 1
    assert items[0]["distribution"][0]["best_bid"] == 0.50
    assert items[0]["distribution"][0]["best_ask"] == 0.51
    assert items[0]["fact_provenance"]["ui_snapshot_source"] == "live_snapshot_compose"
    assert items[0]["fact_provenance"]["live_snapshot_composed"] is True


def test_data_source_health_treats_market_snapshots_as_legacy_when_ticks_exist(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        conn.executescript(
            """
            CREATE TABLE event_fact_latest (
                slug TEXT,
                event_date TEXT,
                fact_id INTEGER,
                updated_at TEXT
            );
            CREATE TABLE event_fact_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                fact_as_of TEXT,
                market_updated_at TEXT,
                model_cache_age_sec REAL,
                fact_json TEXT
            );
            CREATE TABLE event_market_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                outcome TEXT,
                side TEXT,
                created_at TEXT
            );
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                payload_json TEXT,
                meta_json TEXT,
                created_at TEXT
            );
            """
        )
        payload = {
            "slug": "highest-temperature-in-chengdu-on-june-14-2026",
            "event_date": "2026-06-14",
            "run_date": "2026-06-14",
            "city": "chengdu",
            "station_id": "ZUUU",
            "distribution": [{"outcome": "32°C", "q_calibrated": 0.42}],
            "fact_provenance": {},
        }
        cur = conn.execute(
            """
            INSERT INTO event_fact_history(
                run_date, slug, event_date, fact_as_of, market_updated_at,
                model_cache_age_sec, fact_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-14",
                payload["slug"],
                payload["event_date"],
                now_iso,
                now_iso,
                0.0,
                json.dumps(payload),
            ),
        )
        fact_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES (?, ?, ?, ?)",
            (payload["slug"], payload["event_date"], fact_id, now_iso),
        )
        conn.execute(
            """
            INSERT INTO event_market_ticks(run_date, event_date, slug, outcome, side, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("2026-06-14", "2026-06-14", payload["slug"], "32°C", "YES", now_iso),
        )
        conn.execute(
            """
            INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-14",
                "2026-06-14",
                payload["slug"],
                "{}",
                "{}",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(
        server,
        "_WEATHER_EVENTS_CACHE_FILES",
        (tmp_path / "weather_events_cache.json", tmp_path / "weather_events_cache.good.json"),
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "model_update_stats",
        lambda limit=120: {"models": [], "expected_models": [], "missing_models": [], "updated_at": now_iso},
    )
    server._DATA_SOURCE_HEALTH_CACHE.clear()

    health = server.data_source_health()
    market_items = {
        item["key"]: item
        for group in health.get("groups", [])
        if group.get("key") == "facts_market"
        for item in group.get("items", [])
    }

    assert health["summary"]["local_lag"] == 0
    assert market_items["event_market_ticks"]["status"] == "ok"
    assert market_items["market_snapshots"]["status"] == "ok"
    assert market_items["market_snapshots"]["ttl_sec"] is None
    assert "legacy 表" in market_items["market_snapshots"]["detail"]


def test_data_source_health_uses_market_ws_heartbeat_for_tick_lag_type(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    heartbeat_path = tmp_path / "market_ws_heartbeat.json"
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    old_iso = (now - timedelta(seconds=240)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE event_fact_latest (
                slug TEXT,
                event_date TEXT,
                fact_id INTEGER,
                updated_at TEXT
            );
            CREATE TABLE event_fact_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                fact_as_of TEXT,
                market_updated_at TEXT,
                model_cache_age_sec REAL,
                fact_json TEXT
            );
            CREATE TABLE event_market_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                outcome TEXT,
                side TEXT,
                created_at TEXT
            );
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                payload_json TEXT,
                meta_json TEXT,
                created_at TEXT
            );
            """
        )
        payload = {
            "slug": "highest-temperature-in-chengdu-on-june-18-2026",
            "event_date": "2026-06-18",
            "run_date": "2026-06-18",
            "city": "chengdu",
            "station_id": "ZUUU",
            "distribution": [{"outcome": "32°C", "q_calibrated": 0.42}],
            "fact_provenance": {"market_source": "event_market_ticks"},
        }
        cur = conn.execute(
            """
            INSERT INTO event_fact_history(
                run_date, slug, event_date, fact_as_of, market_updated_at,
                model_cache_age_sec, fact_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["run_date"],
                payload["slug"],
                payload["event_date"],
                now_iso,
                old_iso,
                0.0,
                json.dumps(payload),
            ),
        )
        conn.execute(
            "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES (?, ?, ?, ?)",
            (payload["slug"], payload["event_date"], int(cur.lastrowid), now_iso),
        )
        conn.execute(
            """
            INSERT INTO event_market_ticks(run_date, event_date, slug, outcome, side, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (payload["run_date"], payload["event_date"], payload["slug"], "32°C", "YES", old_iso),
        )
        conn.commit()
    finally:
        conn.close()

    heartbeat_path.write_text(json.dumps({"ok": True, "updated_at": now_iso}), encoding="utf-8")
    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "MARKET_WS_HEARTBEAT_PATH", heartbeat_path)
    monkeypatch.setattr(server, "SCAN_TIMING_PATH", tmp_path / "scan_timing.json")
    monkeypatch.setattr(server, "MODEL_SCAN_FACTS_YIELD_MATERIALIZE_STAMP_PATH", tmp_path / "yield_ts")
    monkeypatch.setattr(server, "MODEL_SCAN_LOG_PATH", tmp_path / "model_scan.log")
    monkeypatch.setattr(server, "MODEL_SCAN_START_PATH", tmp_path / "model_scan.last_start")
    monkeypatch.setattr(server, "MARKET_REFRESH_LOG_PATH", tmp_path / "market_refresh.log")
    monkeypatch.setattr(
        server,
        "_WEATHER_EVENTS_CACHE_FILES",
        (tmp_path / "weather_events_cache.json", tmp_path / "weather_events_cache.good.json"),
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "model_update_stats",
        lambda limit=120: {"models": [], "expected_models": [], "missing_models": [], "updated_at": now_iso},
    )
    server._DATA_SOURCE_HEALTH_CACHE.clear()

    health = server.data_source_health()
    market_items = {
        item["key"]: item
        for group in health.get("groups", [])
        if group.get("key") == "facts_market"
        for item in group.get("items", [])
    }

    assert market_items["event_market_ticks"]["status"] == "ok"
    assert market_items["event_market_ticks"]["source_status"] == "warn"
    assert market_items["event_market_ticks"]["source_update_suppressed"] is True
    assert market_items["event_market_ticks"]["lag_type"] == "source_not_updated"
    assert market_items["event_market_ticks"]["checked_at"]
    assert health["summary"]["local_lag"] == 0


def test_backfill_facts_if_needed_refreshes_existing_run_date_from_event_runs() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    rows = [object()]
    calls = []

    old_db_path = server.DB_PATH
    old_facts_exist = server._facts_exist_for_run_date
    old_select_rows = server.select_event_run_rows
    old_backfill = server._backfill_facts_from_event_run_rows
    old_materialize = server._materialize_facts_from_snapshots_for_run_date
    try:
        server.DB_PATH = Path(__file__)
        server._facts_exist_for_run_date = lambda _conn, _date_key: True
        server.select_event_run_rows = lambda _conn, _date_key, include_history: rows
        server._backfill_facts_from_event_run_rows = lambda _conn, selected: calls.append(selected)
        server._materialize_facts_from_snapshots_for_run_date = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not materialize"))
        )

        server._backfill_facts_if_needed(conn, "2026-04-21", include_history=False)
    finally:
        server.DB_PATH = old_db_path
        server._facts_exist_for_run_date = old_facts_exist
        server.select_event_run_rows = old_select_rows
        server._backfill_facts_from_event_run_rows = old_backfill
        server._materialize_facts_from_snapshots_for_run_date = old_materialize

    assert calls == [rows]
