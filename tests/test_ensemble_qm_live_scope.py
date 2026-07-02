import json
import sqlite3

import scan_weather_market as swm


def test_chengdu_wu_static_bias_is_owned_by_source_aligned_qm_target(monkeypatch, tmp_path) -> None:
    report_path = tmp_path / "chengdu_wu_bias.json"
    report_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-12T00:00:00+00:00",
                "bias_correction": {
                    "recommended_scope": "wu_only",
                    "basis": "test",
                    "sample_quality": {"market_day_count": 8},
                    "wu_only": {
                        "stats_by_model": {
                            "gfs_seamless": {"recommended_temp_shift_c": 1.25},
                            "icon_seamless": {"recommended_temp_shift_c": 0.75},
                        }
                    },
                },
            }
        )
    )
    monkeypatch.setenv("PM_CHENGDU_WU_MODEL_BIAS_ENABLED", "1")
    monkeypatch.setenv("PM_CHENGDU_WU_MODEL_BIAS_REPORT_PATH", str(report_path))
    monkeypatch.setenv("PM_ENSEMBLE_QM_BUCKET_ENABLED", "1")
    monkeypatch.setenv("PM_ENSEMBLE_QM_BUCKET_APPLY_TO_Q_RAW", "1")
    monkeypatch.setenv("PM_ENSEMBLE_QM_CALIBRATION_MODE", "raw_seasonal_bias")
    monkeypatch.setenv("PM_ENSEMBLE_QM_CITY_ALLOWLIST", "chengdu")
    monkeypatch.setenv("PM_ENSEMBLE_QM_STATION_ALLOWLIST", "ZUUU")

    corrected, meta = swm.apply_chengdu_wu_model_temperature_bias(
        {
            "gfs_seamless": [28.0, 29.0],
            "icon_seamless": [29.0, 30.0],
        },
        market_var="temperature_2m_max",
        market_unit="C",
        slug="highest-temperature-in-chengdu-on-june-12-2026",
        title="Highest temperature in Chengdu on June 12, 2026",
        station_id="ZUUU",
        resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
    )

    assert meta["applied"] is False
    assert meta["reason"] == "owned_by_source_aligned_ensemble_qm_training_target"
    assert meta["owned_by"] == "ensemble_qm_training_target_wu_twc_final_settlement"
    assert meta["pre_qm_static_bias_applied"] is False
    assert meta["would_have_applied_shifts_c"]["gfs_seamless"] == 1.25
    assert meta["target_shifts_c"]["icon_seamless"] == 0.75
    assert corrected["gfs_seamless"].tolist() == [28.0, 29.0]
    assert corrected["icon_seamless"].tolist() == [29.0, 30.0]


def test_ensemble_qm_scope_allowlist_keeps_chengdu_zuuu_only(monkeypatch) -> None:
    monkeypatch.setenv("PM_ENSEMBLE_QM_CITY_ALLOWLIST", "chengdu")
    monkeypatch.setenv("PM_ENSEMBLE_QM_STATION_ALLOWLIST", "ZUUU")

    assert swm._ensemble_qm_scope_allowed(
        slug="highest-temperature-in-chengdu-on-june-12-2026",
        title="Highest temperature in Chengdu on June 12, 2026",
        city="",
        station_id="ZUUU",
        resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
    )

    assert not swm._ensemble_qm_scope_allowed(
        slug="highest-temperature-in-new-york-on-june-12-2026",
        title="Highest temperature in New York on June 12, 2026",
        city="new york",
        station_id="KNYC",
        resolution_source="https://www.wunderground.com/history/daily/us/ny/new-york/KNYC",
    )


def test_insert_event_run_clears_open_settlement_actual() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            market_closed INTEGER,
            settlement_actual REAL,
            payload_json TEXT,
            created_at TEXT
        )
        """
    )

    swm._insert_event_run_with_payload(
        conn,
        {
            "slug": "highest-temperature-in-chengdu-on-june-12-2026",
            "event_date": "2026-06-12",
            "market_closed": 0,
            "settlement_actual": 30.0,
        },
        '{"rows":[]}',
        "2026-06-11T14:00:00+00:00",
    )
    swm._insert_event_run_with_payload(
        conn,
        {
            "slug": "highest-temperature-in-chengdu-on-june-11-2026",
            "event_date": "2026-06-11",
            "market_closed": 1,
            "settlement_actual": 27.7777777778,
        },
        '{"rows":[]}',
        "2026-06-11T14:01:00+00:00",
    )

    rows = conn.execute(
        "SELECT market_closed, settlement_actual FROM event_runs ORDER BY id"
    ).fetchall()
    assert rows == [(0, None), (1, 27.7777777778)]


def test_model_refresh_event_run_prefers_incoming_qm_over_stale_payload() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            event_url TEXT,
            title TEXT,
            lat REAL,
            lon REAL,
            tz TEXT,
            market_closed INTEGER,
            settlement_actual REAL,
            payload_json TEXT,
            suggestions_json TEXT,
            created_at TEXT
        )
        """
    )
    slug = "highest-temperature-in-chengdu-on-june-13-2026"
    stale_payload = {
        "slug": slug,
        "rows": [
            {
                "outcome": "30",
                "q_raw": 0.2,
                "q_round": 0.2,
                "q_ensemble_qm_enabled": False,
                "q_ensemble_qm_reason": "disabled",
                "best_ask": 0.42,
            }
        ],
    }
    conn.execute(
        """
        INSERT INTO event_runs (
            run_date, event_date, slug, event_url, title, lat, lon, tz,
            market_closed, settlement_actual, payload_json, suggestions_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-11",
            "2026-06-13",
            slug,
            f"https://polymarket.com/event/{slug}",
            "Highest temperature in Chengdu on June 13, 2026",
            30.5583,
            103.946,
            "Asia/Shanghai",
            0,
            None,
            json.dumps(stale_payload),
            "[]",
            "2026-06-11T13:00:00+00:00",
        ),
    )

    incoming_rows = [
        {
            "outcome": "30",
            "q_entry": 0.61,
            "q_raw": 0.61,
            "q_round": 0.61,
            "q_calibrated": 0.61,
            "q_ensemble_qm_bucket": 0.61,
            "q_ensemble_qm_enabled": True,
            "q_ensemble_qm_reason": "applied",
        }
    ]
    model_payload = {
        "slug": slug,
        "ensemble_qm_bucket": {
            "reason": "applied",
            "mode": "raw_seasonal_bias",
        },
        "rows": incoming_rows,
    }

    swm._save_event_run_from_market_refresh(
        conn,
        "2026-06-11",
        "2026-06-13",
        slug,
        incoming_rows,
        "2026-06-11T14:00:00+00:00",
        event_url=f"https://polymarket.com/event/{slug}",
        title="Highest temperature in Chengdu on June 13, 2026",
        lat=30.5583,
        lon=103.946,
        tz="Asia/Shanghai",
        model_payload=model_payload,
    )

    payload_raw = conn.execute(
        "SELECT payload_json FROM event_runs ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()[0]
    payload = json.loads(payload_raw)
    row = payload["rows"][0]

    assert payload["ensemble_qm_bucket"]["reason"] == "applied"
    assert payload["ensemble_qm_bucket"]["mode"] == "raw_seasonal_bias"
    assert row["q_ensemble_qm_enabled"] is True
    assert row["q_ensemble_qm_reason"] == "applied"
    assert row["q_raw"] == 0.61
    assert row["best_ask"] == 0.42


def test_ensemble_qm_d0_mean_shift_guard_blocks_large_shift(monkeypatch) -> None:
    monkeypatch.setenv("PM_ENSEMBLE_QM_D0_MEAN_SHIFT_GUARD_ENABLED", "1")
    monkeypatch.setenv("PM_ENSEMBLE_QM_D0_MAX_MEAN_SHIFT_C", "1.25")

    guard = swm._ensemble_qm_member_mean_shift_guard(
        {"gfs_seamless": [28.0, 29.0], "ecmwf_ifs025": [27.5, 28.5]},
        {"gfs_seamless": [31.0, 32.0], "ecmwf_ifs025": [28.0, 29.0]},
        event_date="2000-01-01",
        tz_name="Asia/Shanghai",
        market_unit="C",
    )

    assert guard["block_apply_to_q_raw"] is True
    assert guard["reason"] == "d0_qm_mean_shift_exceeds_cap"
    assert guard["max_abs_shift_model"] == "gfs_seamless"


def test_ensemble_qm_future_large_shift_remains_diagnostic_only_guard_clear(monkeypatch) -> None:
    monkeypatch.setenv("PM_ENSEMBLE_QM_D0_MEAN_SHIFT_GUARD_ENABLED", "1")
    monkeypatch.setenv("PM_ENSEMBLE_QM_D0_MAX_MEAN_SHIFT_C", "1.25")

    guard = swm._ensemble_qm_member_mean_shift_guard(
        {"gfs_seamless": [28.0, 29.0]},
        {"gfs_seamless": [31.0, 32.0]},
        event_date="2999-01-01",
        tz_name="Asia/Shanghai",
        market_unit="C",
    )

    assert guard["block_apply_to_q_raw"] is False
    assert guard["reason"] == "not_d0"


def test_wunderground_temperature_settlement_uses_history_metrics_not_daily_fallback_by_default(monkeypatch) -> None:
    monkeypatch.delenv("PM_WU_ALLOW_DAILY_TEMP_SETTLEMENT_FALLBACK", raising=False)
    monkeypatch.delenv("PM_WU_ALLOW_PWS_SETTLEMENT_AUX", raising=False)
    monkeypatch.setattr(swm, "fetch_wunderground_history_metrics", lambda *args, **kwargs: {})
    monkeypatch.setattr(swm, "fetch_wunderground_max_temp", lambda *args, **kwargs: 29.0)

    actual = swm.fetch_settlement_actual(
        "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        "2026-06-27",
        "temperature_2m_max",
        "C",
        station_id="ZUUU",
    )

    assert actual is None


def test_wunderground_temperature_settlement_daily_fallback_requires_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("PM_WU_ALLOW_DAILY_TEMP_SETTLEMENT_FALLBACK", "1")
    monkeypatch.delenv("PM_WU_ALLOW_PWS_SETTLEMENT_AUX", raising=False)
    monkeypatch.setattr(swm, "fetch_wunderground_history_metrics", lambda *args, **kwargs: {})
    monkeypatch.setattr(swm, "fetch_wunderground_max_temp", lambda *args, **kwargs: 29.0)

    actual = swm.fetch_settlement_actual(
        "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        "2026-06-27",
        "temperature_2m_max",
        "C",
        station_id="ZUUU",
    )

    assert actual == 29.0
