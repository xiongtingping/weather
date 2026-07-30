from __future__ import annotations

import sqlite3

from ui import server


def test_ui_reported_trajectory_overlay_prefers_latest_persisted_amsc_report(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE reported_trajectory_observations (
            observation_id TEXT PRIMARY KEY,
            source_lane TEXT NOT NULL,
            station_id TEXT NOT NULL,
            report_type TEXT NOT NULL,
            valid_at TEXT NOT NULL,
            reported_temperature_c INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            fetch_completed_at TEXT NOT NULL,
            raw_text TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """INSERT INTO reported_trajectory_observations VALUES
        ('rtobs_2130','amsc','ZUUU','METAR','2026-07-19T13:30:00.000Z',30,
         '2026-07-19T13:30:39.364Z','2026-07-19T13:30:39.364Z',
         'METAR ZUUU 191330Z VRB01MPS CAVOK 30/18 Q1003 NOSIG')"""
    )
    conn.execute(
        """INSERT INTO reported_trajectory_observations VALUES
        ('rtobs_1800','amsc','ZUUU','METAR','2026-07-19T10:00:00.000Z',37,
         '2026-07-19T10:00:39.364Z','2026-07-19T10:00:39.364Z',
         'METAR ZUUU 191000Z 24003MPS CAVOK 37/15 Q1002 NOSIG')"""
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(server, "DB_PATH", db_path)
    with server._REPORTED_TRAJECTORY_UI_LATEST_CACHE_LOCK:
        server._REPORTED_TRAJECTORY_UI_LATEST_CACHE.clear()

    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-19-2026",
        "event_date": "2026-07-19",
        "weather_likelihood_features_compact": {
            "zuuu_metar_raw_amsc_latest_local": "21:00",
            "zuuu_metar_raw_amsc_latest_temp_c": 30.0,
        },
    }
    server._sync_ui_snapshot_amsc_observation_ssot_fields(payload)
    features = payload["weather_likelihood_features_compact"]

    assert features["zuuu_metar_raw_amsc_latest_local"] == "21:30"
    assert features["zuuu_metar_raw_amsc_latest_valid_at"] == "2026-07-19T13:30:00.000Z"
    assert features["zuuu_metar_raw_amsc_latest_temp_c"] == 30.0
    assert features["zuuu_metar_raw_amsc_today_max_c"] == 37.0
    assert payload["reported_trajectory_ui_latest"]["read_only"] is True

