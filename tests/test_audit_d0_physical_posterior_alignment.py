from scripts.audit_d0_physical_posterior_alignment import (
    _audit_payload,
    _latest_chengdu_d0_key,
    main,
    _posterior_with_physical,
    _posterior_without_physical,
)


class Row(dict):
    def __getitem__(self, key):
        return self.get(key)


def test_component_based_physical_delta_is_zero_when_s_phys_is_gated():
    rows = [
        {
            "outcome": "31°C",
            "q_bayes_physical_likelihood_enabled": False,
            "q_bayes_physical_likelihood_evidence": 0.0,
            "q_temperature_bucket_score_components": {
                "history": 1.0,
                "model": 2.0,
                "independent_physics_residual": 0.0,
            },
        },
        {
            "outcome": "32°C",
            "q_bayes_physical_likelihood_enabled": False,
            "q_bayes_physical_likelihood_evidence": 0.0,
            "q_temperature_bucket_score_components": {
                "history": 2.0,
                "model": 1.0,
                "independent_physics_residual": 0.0,
            },
        },
    ]

    assert _posterior_with_physical(rows) == _posterior_without_physical(rows)


def test_audit_marks_aligned_when_enabled_physical_warms_with_warming_trajectory():
    payload = {
        "weather_likelihood_features": {
            "observed_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "temperature_rise_rate_c_per_hour": 0.8,
            "forecast_peak_temp_c": 32.0,
            "observation_hour_local": 12.0,
        },
        "rows": [
            {
                "outcome": "31°C",
                "q_bayes_physical_likelihood_enabled": True,
                "q_bayes_physical_likelihood_evidence": 4.0,
                "q_temperature_bucket_score_components": {
                    "history": 1.0,
                    "model": 2.0,
                    "independent_physics_residual": 0.0,
                },
            },
            {
                "outcome": "33°C",
                "q_bayes_physical_likelihood_enabled": True,
                "q_bayes_physical_likelihood_evidence": 4.0,
                "q_temperature_bucket_score_components": {
                    "history": 1.0,
                    "model": 2.0,
                    "independent_physics_residual": 2.0,
                },
            },
        ],
    }
    row = Row(
        id=1,
        slug="highest-temperature-in-chengdu-on-june-13-2026",
        event_date="2026-06-13",
        run_date="2026-06-13",
        created_at="2026-06-13T04:00:00+00:00",
        payload_json=__import__("json").dumps(payload),
    )

    audit = _audit_payload(row)

    assert audit is not None
    assert audit["s_phys_enabled"] is True
    assert audit["temperature_trajectory_direction"] == "warming"
    assert audit["physical_impact_direction"] == "warming"
    assert audit["consistency"] == "aligned"


def test_audit_flags_physical_warming_when_temperature_trajectory_unrealized():
    payload = {
        "weather_likelihood_features": {
            "observed_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "temperature_rise_rate_c_per_hour": 0.0,
            "settlement_source_forecast_peak_c": 33.0,
            "forecast_peak_temp_c": 33.0,
            "observation_hour_local": 13.0,
        },
        "rows": [
            {
                "outcome": "29°C",
                "q_bayes_physical_likelihood_enabled": True,
                "q_bayes_physical_likelihood_evidence": 4.0,
                "q_temperature_bucket_score_components": {
                    "history": 2.0,
                    "model": 2.0,
                    "independent_physics_residual": 0.0,
                },
            },
            {
                "outcome": "33°C",
                "q_bayes_physical_likelihood_enabled": True,
                "q_bayes_physical_likelihood_evidence": 4.0,
                "q_temperature_bucket_score_components": {
                    "history": 1.0,
                    "model": 2.0,
                    "independent_physics_residual": 3.0,
                },
            },
        ],
    }
    row = Row(
        id=2,
        slug="highest-temperature-in-chengdu-on-june-13-2026",
        event_date="2026-06-13",
        run_date="2026-06-13",
        created_at="2026-06-13T05:00:00+00:00",
        payload_json=__import__("json").dumps(payload),
    )

    audit = _audit_payload(row)

    assert audit is not None
    assert audit["temperature_trajectory_direction"] == "weak_or_unrealized_warming"
    assert audit["physical_impact_direction"] == "warming"
    assert audit["consistency"] == "diverged_physical_potential_without_trajectory_realization"


def test_latest_chengdu_d0_key_ignores_non_d0_and_other_cities(tmp_path):
    import sqlite3

    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_runs(
            slug TEXT,
            event_date TEXT,
            run_date TEXT,
            created_at TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO event_runs(slug,event_date,run_date,created_at) VALUES(?,?,?,?)",
        [
            ("highest-temperature-in-chengdu-on-june-14-2026", "2026-06-14", "2026-06-13", "2026-06-13T09:00:00+00:00"),
            ("highest-temperature-in-paris-on-june-13-2026", "2026-06-13", "2026-06-13", "2026-06-13T10:00:00+00:00"),
            ("highest-temperature-in-chengdu-on-june-13-2026", "2026-06-13", "2026-06-13", "2026-06-13T08:00:00+00:00"),
        ],
    )

    assert _latest_chengdu_d0_key(conn) == (
        "highest-temperature-in-chengdu-on-june-13-2026",
        "2026-06-13",
        "2026-06-13",
    )


def test_cli_require_enabled_returns_2_until_s_phys_enters(tmp_path, monkeypatch, capsys):
    import json
    import sqlite3

    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE event_runs(
            id INTEGER,
            slug TEXT,
            event_date TEXT,
            run_date TEXT,
            created_at TEXT,
            payload_json TEXT
        )
        """
    )
    payload = {
        "weather_likelihood_features": {"temperature_rise_rate_c_per_hour": 0.5},
        "rows": [
            {
                "outcome": "31°C",
                "q_bayes_physical_likelihood_enabled": False,
                "q_bayes_physical_likelihood_evidence": 0.0,
                "q_temperature_bucket_score_components": {"model": 1.0, "independent_physics_residual": 0.0},
            },
            {
                "outcome": "32°C",
                "q_bayes_physical_likelihood_enabled": False,
                "q_bayes_physical_likelihood_evidence": 0.0,
                "q_temperature_bucket_score_components": {"model": 1.0, "independent_physics_residual": 0.0},
            },
        ],
    }
    conn.execute(
        "INSERT INTO event_runs VALUES(?,?,?,?,?,?)",
        (1, "highest-temperature-in-chengdu-on-june-13-2026", "2026-06-13", "2026-06-13", "2026-06-13T00:00:00+00:00", json.dumps(payload)),
    )
    conn.commit()
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit",
            "--db",
            str(db),
            "--latest-chengdu-d0",
            "--require-enabled",
            "--json",
        ],
    )

    assert main() == 2
    assert "waiting_for_s_phys_to_enter_posterior" in capsys.readouterr().out


def test_cli_fail_on_divergence_returns_3(tmp_path, monkeypatch):
    import json
    import sqlite3

    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE event_runs(
            id INTEGER,
            slug TEXT,
            event_date TEXT,
            run_date TEXT,
            created_at TEXT,
            payload_json TEXT
        )
        """
    )
    payload = {
        "weather_likelihood_features": {
            "observed_temp_c": 28.0,
            "temperature_rise_rate_c_per_hour": 0.8,
            "forecast_peak_temp_c": 32.0,
            "observation_hour_local": 12.0,
        },
        "rows": [
            {
                "outcome": "30°C",
                "q_bayes_physical_likelihood_enabled": True,
                "q_bayes_physical_likelihood_evidence": 4.0,
                "q_temperature_bucket_score_components": {
                    "model": 1.0,
                    "independent_physics_residual": 3.0,
                },
            },
            {
                "outcome": "33°C",
                "q_bayes_physical_likelihood_enabled": True,
                "q_bayes_physical_likelihood_evidence": 4.0,
                "q_temperature_bucket_score_components": {
                    "model": 3.0,
                    "independent_physics_residual": 0.0,
                },
            },
        ],
    }
    conn.execute(
        "INSERT INTO event_runs VALUES(?,?,?,?,?,?)",
        (1, "highest-temperature-in-chengdu-on-june-13-2026", "2026-06-13", "2026-06-13", "2026-06-13T00:00:00+00:00", json.dumps(payload)),
    )
    conn.commit()
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit",
            "--db",
            str(db),
            "--latest-chengdu-d0",
            "--fail-on-divergence",
            "--json",
        ],
    )

    assert main() == 3
