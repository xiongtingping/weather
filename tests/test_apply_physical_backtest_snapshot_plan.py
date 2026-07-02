import json
import sqlite3

from scripts import apply_physical_backtest_snapshot_plan as apply_plan


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            event_url TEXT,
            title TEXT,
            city TEXT,
            lat REAL,
            lon REAL,
            tz TEXT,
            location_basis TEXT,
            resolution_source TEXT,
            station_id TEXT,
            rounding_rule TEXT,
            market_var TEXT,
            market_unit TEXT,
            settlement_actual REAL,
            payload_json TEXT,
            suggestions_json TEXT,
            created_at TEXT,
            event_end_utc TEXT,
            market_closed INTEGER
        )
        """
    )
    conn.commit()
    conn.close()


def _snapshot_payload():
    return {
        "market_unit": "F",
        "market_var": "temperature_2m_max",
        "weather_likelihood_features": {
            "observed_temp": 81.0,
            "observed_today_max_c": 27.2,
            "temperature_rise_rate_c_per_hour": 1.0,
            "solar_radiation_observed": 640.0,
            "cloud_cover_low_pct": 10.0,
            "cloud_cover_mid_pct": 20.0,
            "observed_precip_rate_mm_per_h": 0.0,
            "precipitation_rate_mm_per_h": 0.0,
            "wind_speed_mps": 4.0,
            "wind_direction_deg": 180.0,
            "pressure_hpa": 1010.0,
            "dewpoint_c": 18.0,
        },
        "weather_likelihood_feature_groups": {
            "temperature_trajectory": {"available": True},
            "radiation": {"available": True},
            "cloud_shading": {"available": True},
            "precipitation_evaporative_cooling": {"available": True},
            "airmass_mixing": {"available": True},
            "wet_bulb_constraint": {"available": True},
        },
        "weather_likelihood_feature_quality": "complete",
        "rows": [
            {"outcome": "80°F or below", "q_raw": 0.4, "q_raw_static": 0.4},
            {"outcome": "81°F", "q_raw": 0.6, "q_raw_static": 0.6},
        ],
    }


def _plan_file(path, *, candidate_actual=81.0):
    row = {
        "slug": "highest-temperature-in-atlanta-on-june-2-2026",
        "event_date": "2026-06-02",
        "title": "Highest temperature in Atlanta on June 2?",
        "action": "append_candidate_actual_and_feature_snapshot",
        "candidate_settlement_actual": candidate_actual,
        "settlement_actual_source": "weather_com_history_api",
        "station_id": "KATL",
        "lat": 33.6,
        "lon": -84.4,
        "tz": "America/New_York",
        "resolution_source": "https://www.wunderground.com/history/daily/us/atlanta/KATL",
        "market_var": "temperature_2m_max",
        "market_unit": "F",
        "yes_price_count": 2,
        "feature_quality": "complete",
        "review_checksum": "abc123",
        "snapshot_payload_digest": "payload123",
        "snapshot_payload": _snapshot_payload(),
    }
    payload = {
        "candidate_projection": {
            "physical_backtest_apply_plan": {
                "write_policy": "append_only_event_runs_candidate_settlement_and_physical_feature_snapshot_rows",
                "rows": [row],
            }
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return row


def _count_rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM event_runs").fetchone()[0]
    finally:
        conn.close()


def test_physical_snapshot_apply_dry_run_does_not_write(tmp_path) -> None:
    db = tmp_path / "archive.db"
    plan = tmp_path / "plan.json"
    _make_db(db)
    _plan_file(plan)

    report = apply_plan.build_report(db, plan_path=plan)

    assert report["read_only"] is True
    assert report["writes_performed"] is False
    assert report["ready_count"] == 1
    assert _count_rows(db) == 0


def test_physical_snapshot_apply_requires_confirmation(tmp_path) -> None:
    db = tmp_path / "archive.db"
    plan = tmp_path / "plan.json"
    _make_db(db)
    _plan_file(plan)

    report = apply_plan.build_report(db, plan_path=plan, apply=True, confirm_apply="")

    assert report["apply_allowed"] is False
    assert report["inserted_count"] == 0
    assert report["results"][0]["status"] == "missing_apply_confirmation"
    assert _count_rows(db) == 0


def test_physical_snapshot_apply_confirmed_appends_row(tmp_path) -> None:
    db = tmp_path / "archive.db"
    plan = tmp_path / "plan.json"
    _make_db(db)
    _plan_file(plan)

    report = apply_plan.build_report(
        db,
        plan_path=plan,
        apply=True,
        confirm_apply=apply_plan.APPLY_CONFIRM_PHRASE,
        require_pre_apply_backup=False,
    )

    assert report["apply_allowed"] is True
    assert report["inserted_count"] == 1
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT settlement_actual, payload_json FROM event_runs"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 81.0
    payload = json.loads(row[1])
    assert payload["weather_likelihood_feature_quality"] == "complete"
    assert payload["physical_backtest_snapshot_meta"]["review_checksum"] == "abc123"


def test_physical_snapshot_apply_preserves_existing_actual(tmp_path) -> None:
    db = tmp_path / "archive.db"
    plan = tmp_path / "plan.json"
    _make_db(db)
    _plan_file(plan, candidate_actual=99.0)
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO event_runs(event_date, slug, settlement_actual, payload_json, created_at)
        VALUES('2026-06-02', 'highest-temperature-in-atlanta-on-june-2-2026', 82.0, '{}', '2026-06-03T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()

    report = apply_plan.build_report(
        db,
        plan_path=plan,
        apply=True,
        confirm_apply=apply_plan.APPLY_CONFIRM_PHRASE,
        require_pre_apply_backup=False,
    )

    assert report["inserted_count"] == 1
    conn = sqlite3.connect(db)
    try:
        actuals = [
            row[0]
            for row in conn.execute(
                "SELECT settlement_actual FROM event_runs ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert actuals == [82.0, 82.0]
