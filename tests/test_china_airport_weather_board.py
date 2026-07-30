import json
import sqlite3
from datetime import datetime, timezone

from ui.server import _build_china_airport_board_payload


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE reported_trajectory_observations(
          observation_id TEXT PRIMARY KEY, source_lane TEXT, station_id TEXT,
          report_type TEXT, valid_at TEXT, reported_temperature_c INTEGER,
          raw_sha256 TEXT, raw_text TEXT, first_seen_at TEXT, parsed_at TEXT,
          canonical_committed_at TEXT, fetch_started_at TEXT, fetch_completed_at TEXT,
          source_poll_id TEXT, correction_suspected INTEGER DEFAULT 0
        );
        CREATE TABLE model_member_cache(
          id INTEGER PRIMARY KEY, model TEXT, loc_key TEXT, event_date TEXT,
          members_json TEXT, created_at TEXT, official_updated_at TEXT
        );
        CREATE TABLE weak_forecast_evidence_snapshots(
          forecast_evidence_snapshot_id TEXT PRIMARY KEY, target_identity TEXT,
          event_date TEXT, decision_as_of TEXT, source_revisions_json TEXT,
          snapshot_json TEXT, created_at TEXT, state TEXT
        );
        """
    )
    return conn


def test_china_airport_board_projects_live_and_forecast_lineage():
    conn = _db()
    conn.execute(
        """INSERT INTO reported_trajectory_observations VALUES
        ('obs-1','amsc','ZBAA','METAR','2026-07-20T01:00:00Z',31,'sha',
         'METAR ZBAA 200100Z CAVOK 31/22 Q1000','2026-07-20T01:01:00Z',
         '2026-07-20T01:01:00Z','2026-07-20T01:01:00Z','2026-07-20T01:00:00Z',
         '2026-07-20T01:01:00Z','poll',0)"""
    )
    conn.executemany(
        "INSERT INTO model_member_cache(model,loc_key,event_date,members_json,created_at,official_updated_at) VALUES(?,?,?,?,?,?)",
        [
            ("gfs_seamless", "30.5583,103.9460", "2026-07-20", json.dumps([33.0, 35.5]), "2026-07-19T09:00:00Z", "2026-07-19T08:59:00Z"),
            ("icon_seamless", "30.5583,103.9460", "2026-07-20", json.dumps([36.0, 34.5]), "2026-07-19T09:00:00Z", "2026-07-19T08:58:00Z"),
            ("ecmwf_ifs025", "30.5583,103.9460", "2026-07-20", json.dumps([37.0, 35.0]), "2026-07-19T09:00:00Z", "2026-07-19T08:57:00Z"),
        ],
    )
    revisions = [
        {"source_id": "gfs", "source_revision_id": "gfs-1", "available_at": "2026-07-19T08:00:00Z", "forecast_peak_c": 33.0, "forecast_peak_valid_time": "2026-07-20T08:00:00Z"},
        {"source_id": "taf", "source_revision_id": "taf-1", "available_at": "2026-07-19T09:00:00Z", "forecast_peak_c": 39.0, "forecast_peak_valid_time": "2026-07-20T07:00:00Z"},
    ]
    conn.execute(
        "INSERT INTO weak_forecast_evidence_snapshots VALUES(?,?,?,?,?,?,?,?)",
        (
            "snap-1",
            "physical_weather_day_v1:ZUUU:2026-07-20",
            "2026-07-20",
            "2026-07-19T09:01:00Z",
            json.dumps(revisions),
            json.dumps({
                "forecast_background_context": {
                    "forecast_weather_regime": "wet_precipitation",
                    "regime_probability_vector": {
                        "wet_precipitation": 0.61,
                        "clear_dry": 0.39,
                    },
                }
            }),
            "2026-07-19T09:01:00Z",
            "active",
        ),
    )
    conn.commit()

    payload = _build_china_airport_board_payload(
        conn,
        event_date="2026-07-20",
        now=datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc),
    )
    assert payload["schema"] == "china_airport_weather_board_v1"
    assert payload["station_count"] == 9
    by_station = {row["station_id"]: row for row in payload["stations"]}
    assert by_station["ZBAA"]["live"]["temperature_c"] == 31
    assert by_station["ZUUU"]["taf"]["peak_c"] == 39.0
    assert by_station["ZUUU"]["models"]["gfs"]["peak_c"] == 35.5
    assert by_station["ZUUU"]["regime"]["state"] == "available"
    assert by_station["ZUUU"]["regime"]["primary"] == "wet_precipitation"
    assert by_station["ZUUU"]["regime"]["probability_vector"]["wet_precipitation"] == 0.61
    assert by_station["ZUUU"]["regime"]["event_date"] == "2026-07-20"
    assert by_station["ZBAA"]["regime"]["state"] == "missing"
    assert by_station["ZGGG"]["live"]["state"] == "missing"
    conn.close()


def test_china_airport_board_marks_daily_regime_stale_but_keeps_primary_state():
    conn = _db()
    conn.execute(
        "INSERT INTO weak_forecast_evidence_snapshots VALUES(?,?,?,?,?,?,?,?)",
        (
            "snap-stale",
            "physical_weather_day_v1:ZBAA:2026-07-20",
            "2026-07-20",
            "2026-07-18T00:00:00Z",
            "[]",
            json.dumps({"forecast_weather_regime": "clear_dry"}),
            "2026-07-18T00:00:00Z",
            "active",
        ),
    )
    conn.commit()
    payload = _build_china_airport_board_payload(
        conn,
        event_date="2026-07-20",
        now=datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc),
    )
    beijing = next(row for row in payload["stations"] if row["station_id"] == "ZBAA")
    assert beijing["regime"]["state"] == "stale"
    assert beijing["regime"]["primary"] == "clear_dry"
    assert beijing["regime"]["reason"] == "regime_snapshot_stale"
    conn.close()


def test_china_airport_board_uses_probability_vector_over_conflicting_source_label():
    conn = _db()
    conn.execute(
        "INSERT INTO weak_forecast_evidence_snapshots VALUES(?,?,?,?,?,?,?,?)",
        (
            "snap-conflict",
            "physical_weather_day_v1:ZUUU:2026-07-20",
            "2026-07-20",
            "2026-07-20T01:00:00Z",
            "[]",
            json.dumps({
                "forecast_weather_regime": "wet_precipitation",
                "regime_probability_vector": {
                    "clear_dry": 0.58,
                    "wet_precipitation": 0.21,
                },
            }),
            "2026-07-20T01:00:00Z",
            "active",
        ),
    )
    conn.commit()
    payload = _build_china_airport_board_payload(
        conn,
        event_date="2026-07-20",
        now=datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc),
    )
    zuuu = next(row for row in payload["stations"] if row["station_id"] == "ZUUU")
    assert zuuu["regime"]["primary"] == "clear_dry"
    assert zuuu["regime"]["declared_forecast_weather_regime"] == "wet_precipitation"
    assert zuuu["regime"]["primary_selection"] == "regime_probability_vector_argmax"
    assert zuuu["regime"]["regime_consistency"] == "declared_label_conflicts_with_probability_argmax"
    assert zuuu["direction_bias"]["gate_state"] == "UP"
    conn.close()
