import json
import sqlite3

from scripts.calibrate_identifiable_physical_state_artifact import (
    collect_metar_transition_samples,
    collect_strict_asof_samples,
    fit_bounded_parameter_draws,
    _strictly_before,
)
from zuuu_metar_reporting_schedule import expected_regular_metar_slots_utc


def _samples(count: int):
    rows = []
    for index in range(count):
        rows.append(
            {
                "event_date": f"2026-01-{index + 1:02d}",
                "current_source_observation_id": f"current-{index}",
                "delta_hours": 0.5,
                "observed_tendency_c_per_hour": float((index % 9) - 4),
                "strict_asof": True,
                "previous_raw_sha256": f"prev-sha-{index}",
                "current_raw_sha256": f"current-sha-{index}",
            }
        )
    return rows


def test_calibration_fails_closed_when_unique_days_are_insufficient():
    result = fit_bounded_parameter_draws(
        _samples(12), minimum_training_days=10, minimum_holdout_days=5
    )
    assert result["ok"] is False
    assert result["state"] == "blocked_insufficient_strict_asof_metar_days"
    assert result["writes_artifact"] is False


def test_calibration_does_not_count_rows_without_strict_asof_proof():
    rows = _samples(12)
    rows[0]["strict_asof"] = False
    result = fit_bounded_parameter_draws(
        rows, minimum_training_days=10, minimum_holdout_days=2
    )
    assert result["ok"] is False
    assert result["available_unique_days"] == 11


def test_calibration_emits_transition_draws_with_chronological_holdout():
    result = fit_bounded_parameter_draws(
        _samples(20), minimum_training_days=12, minimum_holdout_days=4
    )
    assert result["ok"] is True
    assert result["training_end"] < result["holdout_start"]
    draws = result["bounded_parameter_draws"]
    assert sum(row["normalized_weight"] for row in draws) == 1.0
    assert [row["draw_id"] for row in draws] == [
        "transition_tendency_q10",
        "transition_tendency_q30",
        "transition_tendency_q50",
        "transition_tendency_q70",
        "transition_tendency_q90",
    ]
    assert all(row["latent_temperature_bias_c"] == 0.0 for row in draws)
    # One-step METAR changes are process uncertainty, not a persistent
    # multi-hour temperature drift. The fitted level is bounded and shared by
    # the parameter mixture.
    assert max(abs(row["net_temperature_tendency_bias_c_per_hour"]) for row in draws) <= 0.5
    assert len({row["net_temperature_tendency_bias_c_per_hour"] for row in draws}) == 1
    assert result["one_step_tendency_spread_consumed_as"] == "process_noise_not_repeated_drift"
    assert result["calibration_source"] == "official_amsc_metar_only"
    assert result["identifiability_boundary"]["forecast_peak_not_used"] is True
    assert result["holdout_transition_count"] == 4


def _calibration_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE source_observations (
          source_observation_id TEXT,
          source_id TEXT,
          source_class TEXT,
          station_id TEXT,
          event_date TEXT,
          valid_time TEXT,
          response_completed_at TEXT,
          temperature_c REAL,
          forecast_peak_c REAL,
          raw_sha256 TEXT,
          raw_ref TEXT,
          parser_version TEXT,
          role TEXT,
          quality_flags_json TEXT,
          observation_json TEXT
        );
        """
    )
    return conn


def _insert_metar_day(
    conn: sqlite3.Connection, *, event_date: str, values: list[float] | None = None
) -> None:
    slots = list(expected_regular_metar_slots_utc(event_date))[:6]
    temperatures = values or [30.0 + index for index in range(len(slots))]
    for index, valid_time in enumerate(slots):
        response = valid_time.replace("Z", "+00:00")
        conn.execute(
            "INSERT INTO source_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"metar-{event_date}-{index}",
                "zuuu_metar_raw_amsc",
                "hard_observation_candidate",
                "ZUUU",
                event_date,
                valid_time,
                response,
                temperatures[index],
                None,
                f"metar-sha-{event_date}-{index}",
                f"METAR ZUUU {index:02d} 15004MPS CAVOK {int(temperatures[index])}/22 Q1002=",
                "amsc_zuuu_metar_taf_fetcher_v1",
                "metar_hard_observation_primary",
                "[]",
                json.dumps({"temperature_c": temperatures[index]}),
            ),
        )


def _insert_taf_row(conn: sqlite3.Connection, *, event_date: str, peak: float) -> None:
    conn.execute(
        "INSERT INTO source_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"taf-{event_date}",
            "zuuu_taf_forecast_amsc",
            "forecast_weak_source",
            "ZUUU",
            event_date,
            f"{event_date}T07:00:00Z",
            f"{event_date}T00:01:00Z",
            None,
            peak,
            "taf-sha",
            "TAF ZUUU 091502Z 0918/1018 TX38/1007Z=",
            "amsc_zuuu_metar_taf_fetcher_v1",
            "taf_forecast_weak_likelihood_primary",
            "[]",
            json.dumps({"forecast_peak_c": peak, "payload": {"forecast_peak_c": peak}}),
        ),
    )


def test_collector_uses_only_first_seen_official_metar_transitions():
    conn = _calibration_conn()
    _insert_metar_day(conn, event_date="2026-07-10", values=[30, 31, 32, 33, 34, 35])
    # A later re-fetch with a deliberately impossible temperature must not
    # replace the first-seen report for the same valid time.
    conn.execute(
        "INSERT INTO source_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "metar-late-re-fetch",
            "zuuu_metar_raw_amsc",
            "hard_observation_candidate",
            "ZUUU",
            "2026-07-10",
            list(expected_regular_metar_slots_utc("2026-07-10"))[2],
            "2026-07-10T02:00:00Z",
            99.0,
            None,
            "late-sha",
            "METAR ZUUU 99 15004MPS CAVOK 99/22 Q1002=",
            "amsc_zuuu_metar_taf_fetcher_v1",
            "metar_hard_observation_primary",
            "[]",
            "{}",
        ),
    )
    _insert_taf_row(conn, event_date="2026-07-10", peak=38.0)
    samples = collect_metar_transition_samples(conn)
    assert len(samples) == 5
    assert samples[1]["current_temperature_c"] == 32.0
    assert all("peak" not in key.lower() for row in samples for key in row)
    assert all(row["strict_asof"] is True for row in samples)
    assert collect_strict_asof_samples(conn) == samples


def test_taf_peak_changes_do_not_change_metar_samples_or_calibration():
    conn = _calibration_conn()
    for index in range(20):
        day = f"2026-01-{index + 1:02d}"
        _insert_metar_day(conn, event_date=day, values=[20.0, 21.0, 20.0, 22.0, 21.0, 23.0])
        _insert_taf_row(conn, event_date=day, peak=35.0)
    before = collect_metar_transition_samples(conn)
    first = fit_bounded_parameter_draws(before, minimum_training_days=12, minimum_holdout_days=4)
    conn.execute(
        "UPDATE source_observations SET forecast_peak_c=999, observation_json=? "
        "WHERE source_id='zuuu_taf_forecast_amsc'",
        (json.dumps({"forecast_peak_c": 999, "payload": {"forecast_peak_c": 999}}),),
    )
    after = collect_metar_transition_samples(conn)
    second = fit_bounded_parameter_draws(after, minimum_training_days=12, minimum_holdout_days=4)
    assert before == after
    assert first["bounded_parameter_draws"] == second["bounded_parameter_draws"]
    assert first["sample_lineage_sha256"] == second["sample_lineage_sha256"]


def test_clear_cavok_is_a_clear_regime_observation():
    from identifiable_physical_state_bayes.adapters import canonical_observation_from_event

    observation = canonical_observation_from_event(
        {
            "canonical_observation_event_id": "event-cavok",
            "valid_time": "2026-07-23T00:00:00Z",
            "report_type": "METAR",
            "state": "accepted_primary",
            "parsed_observation_json": json.dumps(
                {
                    "temperature_c": 30.0,
                    "dewpoint_c": 20.0,
                    "pressure_hpa": 1000.0,
                    "cloud_text": "CAVOK",
                    "weather_text": "",
                }
            ),
        }
    )
    assert observation.cloud_fraction == 0.0
    assert observation.precipitation_present is False


def test_non_metar_report_is_not_a_transition_input():
    conn = _calibration_conn()
    _insert_metar_day(conn, event_date="2026-07-10")
    conn.execute(
        "INSERT INTO source_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "spe-ci",
            "zuuu_metar_raw_amsc",
            "hard_observation_candidate",
            "ZUUU",
            "2026-07-10",
            "2026-07-10T03:00:00Z",
            "2026-07-10T03:01:00Z",
            88.0,
            None,
            "speci-sha",
            "SPECI ZUUU 100300Z 15004MPS CAVOK 88/22 Q1002=",
            "amsc_zuuu_metar_taf_fetcher_v1",
            "metar_hard_observation_primary",
            "[]",
            "{}",
        ),
    )
    samples = collect_metar_transition_samples(conn)
    assert len(samples) == 5
    assert all(row["current_temperature_c"] < 88 for row in samples)


def test_strict_asof_timestamp_comparison_normalizes_offsets():
    assert _strictly_before(
        "2026-07-09T23:59:59+08:00", "2026-07-09T16:00:00Z"
    ) is True
    assert _strictly_before(
        "2026-07-10T00:00:00+08:00", "2026-07-09T16:00:00Z"
    ) is False
