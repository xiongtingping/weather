import sqlite3

from regime_risk_runtime import build_post_cool_rebound_risk_features, load_post_cool_rebound_risk_context


def _warming_features(**overrides):
    features = {
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "forecast_peak_temp_c": 32.0,
        "observed_temp_c": 32.0,
        "observed_today_max_c": 32.0,
        "observation_hour_local": 13.5,
        "expected_peak_hour_local": 16.0,
        "temperature_momentum_effective_c_per_hour": 0.75,
        "temperature_momentum_30m_c_per_hour": 0.7,
        "temperature_momentum_1h_c_per_hour": 0.65,
        "peak_estimator": {
            "enabled": True,
            "mu_c": 32.1,
            "extrapolation_c": 33.0,
            "sigma_c": 0.8,
            "hours_to_peak_window": 2.5,
        },
    }
    features.update(overrides)
    return features


def test_post_cool_rebound_lifts_only_after_intraday_rebound_confirmation():
    risk = build_post_cool_rebound_risk_features(
        target_event_date="2026-06-19",
        weather_features=_warming_features(),
        recent_daily_tmax=[
            {"event_date": "2026-06-18", "actual_c": 28.0},
            {"event_date": "2026-06-17", "actual_c": 30.0},
        ],
        as_of="2026-06-19T05:30:00+00:00",
    )

    assert risk["state"] == "lift"
    assert risk["enabled"] is True
    assert risk["cool_gate"] is True
    assert risk["observed_rebound_gate"] is True
    assert risk["warming_rate_gate"] is True
    assert risk["target_peak_c"] >= 33.0
    assert risk["regime_prior"]["state"] == "prior"
    assert risk["regime_prior"]["enabled"] is True
    assert risk["regime_prior"]["role"] == "conditional_climate_prior"
    assert risk["intraday_likelihood"]["state"] == "lift"
    assert risk["intraday_likelihood"]["role"] == "intraday_likelihood"
    assert risk["provenance"]["guards"][-1] == "hard_sources_override_later_in_bayes"


def test_post_cool_rebound_is_blocked_after_peak_window():
    risk = build_post_cool_rebound_risk_features(
        target_event_date="2026-06-19",
        weather_features=_warming_features(
            peak_estimator={
                "enabled": True,
                "mu_c": 32.1,
                "extrapolation_c": 33.0,
                "hours_to_peak_window": -0.5,
            }
        ),
        recent_daily_tmax=[
            {"event_date": "2026-06-18", "actual_c": 28.0},
            {"event_date": "2026-06-17", "actual_c": 30.0},
        ],
        as_of="2026-06-19T09:30:00+00:00",
    )

    assert risk["state"] == "off"
    assert risk["enabled"] is False
    assert risk["status"] == "after_peak_window"
    assert risk["after_peak_gate"] is True
    assert risk["regime_prior"]["enabled"] is False
    assert risk["intraday_likelihood"]["enabled"] is False


def test_post_cool_rebound_uses_prior_day_observed_tmax_when_final_settlement_late(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE station_observations (
            id INTEGER PRIMARY KEY,
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
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO station_observations(
            run_date, event_date, station_id, source_kind, observed_at, value, unit, market_var, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "2026-06-21",
                "2026-06-21",
                "ZUUU",
                "wunderground_history",
                "2026-06-21T02:00:00+00:00",
                22.0,
                "C",
                "temperature_2m_max",
                "2026-06-21T02:08:53+00:00",
            ),
            (
                "2026-06-21",
                "2026-06-21",
                "ZUUU",
                "wunderground_history",
                "2026-06-21T08:00:00+00:00",
                24.0,
                "C",
                "temperature_2m_max",
                "2026-06-21T15:59:44+00:00",
            ),
        ],
    )
    conn.commit()
    conn.close()

    context = load_post_cool_rebound_risk_context(
        db_path=db_path,
        target_event_date="2026-06-22",
        as_of="2026-06-22T04:29:28+00:00",
        city="Chengdu",
        station_id="ZUUU",
        market_var="temperature_2m_max",
        market_unit="C",
        weather_features=_warming_features(
            forecast_peak_temp_c=30.1,
            observed_temp_c=29.4,
            observed_today_max_c=29.4,
            observation_hour_local=12.5,
            expected_peak_hour_local=16.0,
            temperature_momentum_effective_c_per_hour=0.55,
            temperature_momentum_30m_c_per_hour=0.7,
            temperature_momentum_1h_c_per_hour=0.5,
        ),
    )

    intraday = context["intraday_likelihood"]
    assert context["recent_daily_tmax"][0]["event_date"] == "2026-06-21"
    assert context["recent_daily_tmax"][0]["actual_c"] == 24.0
    assert context["recent_daily_tmax"][0]["provisional"] is True
    assert intraday["cool_trough_c"] == 24.0
    assert intraday["state"] == "lift"
    assert intraday["enabled"] is True
    assert intraday["observed_rebound_c"] > 5.0


def test_post_cool_rebound_observed_breakthrough_can_confirm_after_peak():
    risk = build_post_cool_rebound_risk_features(
        target_event_date="2026-06-22",
        weather_features=_warming_features(
            forecast_peak_temp_c=30.0,
            observed_temp_c=33.0,
            observed_today_max_c=33.0,
            observation_hour_local=17.5,
            expected_peak_hour_local=16.0,
            temperature_momentum_effective_c_per_hour=-0.6,
            temperature_momentum_30m_c_per_hour=-0.4,
            temperature_momentum_1h_c_per_hour=-0.7,
            peak_estimator={
                "enabled": True,
                "mu_c": 30.0,
                "extrapolation_c": 30.0,
                "hours_to_peak_window": -1.5,
            },
        ),
        recent_daily_tmax=[
            {"event_date": "2026-06-21", "actual_c": 24.0},
            {"event_date": "2026-06-20", "actual_c": 28.0},
        ],
        as_of="2026-06-22T09:36:56+00:00",
    )

    assert risk["state"] == "lift"
    assert risk["status"] == "lift_observed_breakthrough"
    assert risk["enabled"] is True
    assert risk["observed_breakthrough_gate"] is True
    assert risk["warming_rate_gate"] is False
    assert risk["warming_rate_gate_relaxed_by_observed_breakthrough"] is True
    assert risk["after_peak_gate"] is True
    assert risk["after_peak_gate_relaxed_by_observed_breakthrough"] is True
