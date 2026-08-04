from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone

from daily_regime_snapshot_materializer import materialize_daily_regime_snapshot
from weather_basket_yes_schema import apply_weather_basket_yes_schema
from ui.server import _china_board_regime_snapshot


def test_materializes_taf_regime_and_ui_reads_the_persisted_projection() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_weather_basket_yes_schema(conn)
    conn.execute(
        """INSERT INTO amsc_taf_snapshots(
          source_revision_id,event_date,station_id,source_lane,forecast_revision_kind,
          taf_modifier,issue_time_utc,first_system_seen_at,validity_token,peak_c,
          peak_valid_time_utc,forecast_weather_regime,forecast_peaks_json,raw_sha256,
          raw_text,source_report_time,lineage_json,state,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "taf-zbaa-test", "2026-07-22", "ZBAA", "amsc_taf", "original", "",
            "2026-07-21T03:00:00Z", "2026-07-21T03:00:00Z", "2206/2306", 32.0,
            "2026-07-22T06:00:00Z", "clear_dry", "[]", "raw-sha", "TAF", "",
            "{}", "available", "2026-07-21T03:00:00Z",
        ),
    )
    conn.commit()

    result = materialize_daily_regime_snapshot(
        conn, station_id="ZBAA", event_date="2026-07-22"
    )
    view = _china_board_regime_snapshot(
        conn,
        station_id="ZBAA",
        event_date="2026-07-22",
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert result["state"] == "materialized"
    assert result["primary"] == "clear_dry"
    assert view["source"] == "archive.db.daily_regime_snapshots"
    assert view["forecast_weather_regime"] == "clear_dry"
    assert view["state"] == "available"
    conn.close()


def test_nwp_regime_uses_direct_physical_fields_not_temperature_peak() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_weather_basket_yes_schema(conn)
    conn.execute(
        """INSERT INTO official_nwp_station_snapshots(
          snapshot_id,provider_id,station_id,loc_key,event_date,run_at_utc,
          valid_time_utc,temperature_2m_c,normalized_features_json,raw_object_ref,
          raw_sha256,source_snapshot_sha256,lineage_json,state,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "nwp-zggg-test", "gfs", "ZGGG", "23.3924,113.2988", "2026-07-22",
            "2026-07-21T00:00:00Z", "2026-07-22T06:00:00Z", 48.0,
            '{"relative_humidity_850_pct":40,"relative_humidity_700_pct":35,"u_wind_850_mps":9,"v_wind_850_mps":0,"u_wind_700_mps":8,"v_wind_700_mps":0}',
            "/raw", "raw", "snap", "{}", "available", "2026-07-21T01:00:00Z",
        ),
    )
    conn.commit()

    result = materialize_daily_regime_snapshot(
        conn, station_id="ZGGG", event_date="2026-07-22"
    )

    assert result["state"] == "materialized"
    assert result["primary"] == "well_ventilated"
    conn.close()


def test_target_day_official_metar_overrides_wet_forecast_regime() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_weather_basket_yes_schema(conn)
    conn.executemany(
        """INSERT INTO reported_trajectory_observations(
             observation_id,source_lane,station_id,report_type,valid_at,
             reported_temperature_c,raw_sha256,raw_text,first_seen_at,
             parsed_at,canonical_committed_at,fetch_started_at,
             fetch_completed_at,source_poll_id,correction_suspected
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                "obs-0700", "amsc", "ZUUU", "METAR", "2026-07-21T07:00:00Z", 38,
                "sha-0700", "METAR ZUUU 210700Z CAVOK 38/17 Q1003 NOSIG",
                "2026-07-21T07:00:40Z", "2026-07-21T07:00:40Z", "2026-07-21T07:00:40Z",
                "2026-07-21T07:00:00Z", "2026-07-21T07:00:40Z", "poll-1", 0,
            ),
            (
                "obs-1330", "amsc", "ZUUU", "METAR", "2026-07-21T13:30:00Z", 31,
                "sha-1330", "METAR ZUUU 211330Z CAVOK 31/20 Q1003 NOSIG",
                "2026-07-21T13:30:40Z", "2026-07-21T13:30:40Z", "2026-07-21T13:30:40Z",
                "2026-07-21T13:30:00Z", "2026-07-21T13:30:40Z", "poll-2", 0,
            ),
        ],
    )
    # A forecast curve predicts a small accumulated shower.  It must remain
    # visible as lineage but cannot override the observed clear day.
    conn.executemany(
        """INSERT INTO official_nwp_station_snapshots(
             snapshot_id,provider_id,station_id,loc_key,event_date,run_at_utc,
             valid_time_utc,temperature_2m_c,normalized_features_json,
             raw_object_ref,raw_sha256,source_snapshot_sha256,lineage_json,
             state,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                "nwp-r1", "gfs", "ZUUU", "30.5583,103.9460", "2026-07-21",
                "2026-07-20T12:00:00Z", "2026-07-21T06:00:00Z", 38.0,
                json.dumps({"total_precipitation_mm": 0.0, "total_precipitation_accumulation_hours": 6.0, "total_cloud_cover_pct": 5.0}),
                "/raw-1", "raw-1", "snap-1", "{}", "available", "2026-07-21T09:00:00Z",
            ),
            (
                "nwp-r2", "gfs", "ZUUU", "30.5583,103.9460", "2026-07-21",
                "2026-07-20T12:00:00Z", "2026-07-21T09:00:00Z", 39.0,
                json.dumps({"total_precipitation_mm": 1.0, "total_precipitation_accumulation_hours": 9.0, "total_cloud_cover_pct": 80.0}),
                "/raw-2", "raw-2", "snap-2", "{}", "available", "2026-07-21T09:00:00Z",
            ),
        ],
    )
    conn.commit()

    result = materialize_daily_regime_snapshot(
        conn, station_id="ZUUU", event_date="2026-07-21", now_utc="2026-07-21T15:40:00Z"
    )
    conn.execute(
        """INSERT INTO daily_regime_snapshots(
             regime_snapshot_id,station_id,event_date,source_kind,
             source_revisions_json,regime_json,source_sha256,as_of,created_at,state
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "old-forecast", "ZUUU", "2026-07-21", "official_amsc_taf_and_nwp",
            "[]", json.dumps({"primary": "wet_precipitation", "forecast_weather_regime": "wet_precipitation"}),
            "old-sha", "2026-07-21T15:00:00Z", "2026-07-21T15:01:00Z", "available",
        ),
    )
    conn.commit()
    view = _china_board_regime_snapshot(
        conn,
        station_id="ZUUU",
        event_date="2026-07-21",
        now=datetime(2026, 7, 21, 15, 40, tzinfo=timezone.utc),
    )

    assert result["primary"] == "clear_dry"
    assert result["primary_source"] == "official_amsc_metar_observation"
    assert view["primary"] == "clear_dry"
    assert view["primary_source"] == "official_amsc_metar_observation"
    assert view["observed_weather_regime"] == "clear_dry"
    assert view["source_kind"] == "official_amsc_metar_taf_and_nwp"
    conn.close()
