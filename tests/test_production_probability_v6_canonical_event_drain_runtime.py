from __future__ import annotations

import json
import sqlite3

from production_probability_v6_canonical_event_drain_runtime import _event, _initial_joint


def test_v6_initial_joint_is_prior_times_weak_times_latent_state() -> None:
    inputs = {
        "prior_context": {
            "continuous_distribution": {
                "grid": [{"temp_c": 34.0, "mass": 0.5}, {"temp_c": 38.0, "mass": 0.5}]
            },
            "latent_state_prior_by_temperature": {
                "34": {"warming": 0.5, "weather_suppressed": 0.5},
                "38": {"warming": 0.5, "weather_suppressed": 0.5},
            },
        },
        "weak_likelihood": {
            "joint_weak_likelihood": {
                "joint_forecast_weak_likelihood": [
                    {"temperature_c": 34.0, "likelihood": 0.2},
                    {"temperature_c": 38.0, "likelihood": 0.8},
                ]
            }
        },
    }
    rows = _initial_joint(inputs)
    marginal = {}
    for row in rows:
        marginal[row.y_c] = marginal.get(row.y_c, 0.0) + row.probability
    assert marginal == {34.0: 0.2, 38.0: 0.8}
    assert abs(sum(row.probability for row in rows) - 1.0) < 1e-12


def test_canonical_drain_marks_only_frozen_amsc_metar_slots_as_contract_slots() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE canonical_observation_events("
        "canonical_observation_event_id TEXT PRIMARY KEY,target_identity TEXT,"
        "station_id TEXT,selected_source_id TEXT,report_type TEXT,valid_time TEXT,"
        "parsed_observation_json TEXT)"
    )
    rows = (
        (
            "scheduled",
            "physical_weather_day_v1:ZUUU:2026-07-19",
            "ZUUU",
            "zuuu_metar_raw_amsc",
            "METAR",
                "2026-07-19T14:00:00Z",
            json.dumps({"temperature_c": 34}),
        ),
        (
            "unscheduled",
            "physical_weather_day_v1:ZUUU:2026-07-19",
            "ZUUU",
            "zuuu_metar_raw_amsc",
            "METAR",
            "2026-07-19T14:15:00Z",
            json.dumps({"temperature_c": 35}),
        ),
        (
            "speci-at-slot",
            "physical_weather_day_v1:ZUUU:2026-07-19",
            "ZUUU",
            "zuuu_metar_raw_amsc",
            "SPECI",
                "2026-07-19T14:30:00Z",
            json.dumps({"temperature_c": 36}),
        ),
    )
    conn.executemany(
        "INSERT INTO canonical_observation_events VALUES(?,?,?,?,?,?,?)", rows
    )
    assert _event(conn, "scheduled")["is_contract_report_slot"] is True
    assert _event(conn, "unscheduled")["is_contract_report_slot"] is False
    assert _event(conn, "speci-at-slot")["is_contract_report_slot"] is False
