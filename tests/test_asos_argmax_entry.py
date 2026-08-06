from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

import pytest

from asos_argmax_entry import materialize_argmax_entry_facts
from weather_basket_yes_schema import apply_weather_basket_yes_schema


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(conn)
    for outcome, token in (
        ("79°F or below", "yes-low"),
        ("80-81°F", "yes-mid"),
        ("82°F or higher", "yes-high"),
    ):
        conn.execute(
            """INSERT INTO reported_trajectory_market_registry(
                 slug,event_date,outcome,city_slug,station_id,settlement_timezone,
                 settlement_unit,source_lane,source_eligible,source_block_reason,
                 resolution_url,rules_sha256,yes_token,no_token,market_active,
                 market_accepting_orders,registry_json,refreshed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "atlanta-2026-08-05", "2026-08-05", outcome, "atlanta", "KATL",
                "America/New_York", "F", "awc", 1, None, "https://example.invalid",
                _sha(outcome), token, "no-" + token, 1, 1, "{}", "2026-08-05T04:00:00Z",
            ),
        )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_windows(
             window_id,station_id,event_date,settlement_timezone,provider_id,
             source_class,forecast_generated_at_utc,fetched_at_utc,
             argmax_window_start_utc,argmax_window_end_utc,argmax_temperature_c,
             argmax_periods_json,points_receipt_sha256,forecast_receipt_sha256,
             raw_object_ref,coordinate_registry_sha256,content_sha256,state,
             timing_only,provides_temperature_bucket,provides_probability,enters_p0,enters_p0m
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'FROZEN',1,0,0,0,0)""",
        (
            "window-1", "KATL", "2026-08-05", "America/New_York",
            "NOAA_NWS_GRIDPOINT_HOURLY", "official_forecast_product",
            "2026-08-05T00:00:00Z", "2026-08-05T01:00:00Z",
            "2026-08-05T13:00:00Z", "2026-08-05T15:00:00Z", 30.0,
            '[{"start_utc":"2026-08-05T13:00:00Z","end_utc":"2026-08-05T15:00:00Z"}]',
            "1" * 64, "2" * 64, "/raw", "3" * 64, "4" * 64,
        ),
    )
    for hour in range(4, 14):
        hour_text = f"2026-08-05T{hour:02d}:00:00.000Z"
        object_sha = _sha("object" + hour_text)
        conn.execute(
            """INSERT INTO reported_trajectory_madis_hfmetar_objects(
                 compressed_sha256,object_hour_utc,object_url,compressed_bytes,retained_path,
                 source_lane,source_class,receipt_sha256,fetch_started_at_utc,fetch_completed_at_utc
               ) VALUES(?,?,?,?,?,'MADIS_HFMETAR_ASOS','official_republisher',?,?,?)""",
            (object_sha, hour_text, "https://example.invalid", 1, "/raw", _sha("receipt" + hour_text), hour_text, hour_text),
        )
    return conn


def _observation(conn, *, at: str, temperature_c: float, committed: str) -> None:
    object_hour = at[:13] + ":00:00.000Z"
    object_sha = conn.execute(
        "SELECT compressed_sha256 FROM reported_trajectory_madis_hfmetar_objects WHERE object_hour_utc=?",
        (object_hour,),
    ).fetchone()[0]
    identity = _sha(at + str(temperature_c))
    conn.execute(
        """INSERT INTO reported_trajectory_madis_hfmetar_observations(
             content_sha256,compressed_sha256,station_id,provider_station_id,
             observed_at_utc,temperature_c,temperature_qc_summary,temperature_sensor_status,
             station_type,data_provider,measurement_semantics,raw_message_sha256,
             qc_eligible,canonical_committed_at_utc
           ) VALUES(?,?,'KATL','KATL',?,?,'S',0,'HF-METAR','HF-METAR',
                    'ASOS_RUNNING_5_MINUTE_AVERAGE_DISSEMINATED_EVERY_5_MINUTES',?,1,?)""",
        (identity, object_sha, at, temperature_c, _sha("raw" + at), committed),
    )


def test_backfill_only_arms_then_future_new_high_materializes_one_fact() -> None:
    conn = _db()
    _observation(
        conn, at="2026-08-05T13:00:00.000Z", temperature_c=25.0,
        committed="2026-08-05T13:05:00.000Z",
    )
    armed = materialize_argmax_entry_facts(
        conn, ["KATL"], now_utc=datetime(2026, 8, 5, 13, 6, tzinfo=timezone.utc)
    )
    assert armed["scopes"][0]["state"] == "armed"
    assert conn.execute(
        "SELECT COUNT(*) FROM reported_trajectory_asos_argmax_entry_facts"
    ).fetchone()[0] == 0

    _observation(
        conn, at="2026-08-05T13:10:00.000Z", temperature_c=28.0,
        committed="2026-08-05T13:10:05.000Z",
    )
    ready = materialize_argmax_entry_facts(
        conn, ["KATL"], now_utc=datetime(2026, 8, 5, 13, 11, tzinfo=timezone.utc)
    )
    assert ready["inserted_entry_fact_count"] == 1
    row = conn.execute(
        """SELECT outcome,yes_token,settlement_scalar,gateway_submitted,reason,
                  argmax_window_start_utc
             FROM reported_trajectory_asos_argmax_entry_facts"""
    ).fetchone()
    assert row == (
        "82°F or higher", "yes-high", 82, 0,
        "ASOS_NEW_HIGH_RECEIVED_DURING_ARGMAX_PERIOD",
        "2026-08-05T13:00:00.000Z",
    )


def test_same_or_lower_temperature_advances_state_with_zero_fact_write() -> None:
    conn = _db()
    _observation(conn, at="2026-08-05T13:00:00.000Z", temperature_c=25.0, committed="2026-08-05T13:01:00.000Z")
    materialize_argmax_entry_facts(
        conn, ["KATL"], now_utc=datetime(2026, 8, 5, 13, 2, tzinfo=timezone.utc)
    )
    _observation(conn, at="2026-08-05T13:05:00.000Z", temperature_c=25.0, committed="2026-08-05T13:05:05.000Z")
    result = materialize_argmax_entry_facts(
        conn, ["KATL"], now_utc=datetime(2026, 8, 5, 13, 6, tzinfo=timezone.utc)
    )
    assert result["inserted_entry_fact_count"] == 0
    assert conn.execute("SELECT COUNT(*) FROM reported_trajectory_asos_argmax_entry_facts").fetchone()[0] == 0


def test_empty_pre_window_contract_is_rebuilt_to_exact_period_contract() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE reported_trajectory_asos_argmax_entry_facts(
             entry_fact_id TEXT PRIMARY KEY,
             reason TEXT CHECK(reason='ASOS_NEW_HIGH_RECEIVED_BEFORE_ARGMAX_WINDOW')
           )"""
    )
    conn.execute(
        "CREATE TABLE reported_trajectory_asos_argmax_order_intents(order_intent_id TEXT)"
    )

    apply_weather_basket_yes_schema(conn)

    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='reported_trajectory_asos_argmax_entry_facts'"
    ).fetchone()[0]
    assert "ASOS_NEW_HIGH_RECEIVED_DURING_ARGMAX_PERIOD" in table_sql
    assert "ASOS_NEW_HIGH_RECEIVED_BEFORE_ARGMAX_WINDOW" not in table_sql


def test_nonempty_pre_window_contract_migration_fails_closed() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE reported_trajectory_asos_argmax_entry_facts(
             entry_fact_id TEXT PRIMARY KEY,
             reason TEXT CHECK(reason='ASOS_NEW_HIGH_RECEIVED_BEFORE_ARGMAX_WINDOW')
           )"""
    )
    conn.execute(
        "INSERT INTO reported_trajectory_asos_argmax_entry_facts VALUES(?,?)",
        ("old", "ASOS_NEW_HIGH_RECEIVED_BEFORE_ARGMAX_WINDOW"),
    )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="argmax_period_contract_migration_nonempty_fail_closed",
    ):
        apply_weather_basket_yes_schema(conn)
