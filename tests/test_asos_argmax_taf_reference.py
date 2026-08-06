from __future__ import annotations

import hashlib
import json
import sqlite3

from asos_argmax_taf_reference import materialize_argmax_taf_references
from bayes_basket_target.contracts import content_sha256
from weather_basket_yes_schema import apply_weather_basket_yes_schema


def _capture(raw_taf: str, revision_id: str) -> dict:
    value = {
        "source_class": "official_forward_native_receipt",
        "provider": "NOAA Aviation Weather Center",
        "station_id": "KATL",
        "event_date": "2026-08-05",
        "local_received_at_utc": "2026-08-05T12:00:00Z",
        "raw_taf": raw_taf,
        "raw_taf_sha256": hashlib.sha256(raw_taf.encode()).hexdigest(),
        "forecast_revision": {
            "revision_id": revision_id,
            "issued_at_utc": "2026-08-05T05:00:00Z",
        },
        "boundary": {"native_local_receipt": True},
    }
    return {**value, "content_sha256": content_sha256(value)}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(conn)
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_windows(
             window_id,station_id,event_date,settlement_timezone,provider_id,
             source_class,forecast_generated_at_utc,fetched_at_utc,
             argmax_window_start_utc,argmax_window_end_utc,argmax_temperature_c,
             argmax_periods_json,points_receipt_sha256,forecast_receipt_sha256,
             raw_object_ref,coordinate_registry_sha256,content_sha256,state,
             timing_only,provides_temperature_bucket,provides_probability,enters_p0,enters_p0m
           ) VALUES('window','KATL','2026-08-05','America/New_York',
                    'NOAA_NWS_GRIDPOINT_HOURLY','official_forecast_product',
                    '2026-08-05T05:00:00Z','2026-08-05T06:00:00Z',
                    '2026-08-05T18:00:00Z','2026-08-05T20:00:00Z',30,?,
                    ?,?,'/raw',?,?, 'FROZEN',1,0,0,0,0)""",
        (
            json.dumps([{
                "start_utc": "2026-08-05T18:00:00Z",
                "end_utc": "2026-08-05T20:00:00Z",
            }]),
            "1" * 64, "2" * 64, "3" * 64, "4" * 64,
        ),
    )
    return conn


def test_taf_tx_is_audited_without_modifying_nws_window(tmp_path) -> None:
    conn = _db()
    captures = [
        _capture("TAF KATL 050500Z 0506/0612 00000KT CAVOK TX30/0518Z=", "rev-confirm"),
        _capture("TAF KATL 050500Z 0506/0612 00000KT CAVOK TX30/0522Z=", "rev-conflict"),
    ]
    inbox = {
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": captures,
    }
    path = tmp_path / "taf.json"
    path.write_text(json.dumps(inbox))

    result = materialize_argmax_taf_references(conn, inbox_path=path)

    assert result["inserted"] == 2
    rows = conn.execute(
        """SELECT comparison_state,modifies_nws_window,blocks_entry
             FROM reported_trajectory_asos_argmax_taf_references
         ORDER BY comparison_state"""
    ).fetchall()
    assert rows == [
        ("CONFIRMS_NWS_ARGMAX", 0, 0),
        ("CONFLICTS_WITH_NWS_ARGMAX", 0, 0),
    ]
    window = conn.execute(
        "SELECT argmax_window_start_utc,argmax_window_end_utc FROM reported_trajectory_asos_argmax_windows"
    ).fetchone()
    assert window == ("2026-08-05T18:00:00Z", "2026-08-05T20:00:00Z")


def test_taf_without_tx_is_ignored_and_never_blocks(tmp_path) -> None:
    conn = _db()
    path = tmp_path / "taf.json"
    path.write_text(json.dumps({
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": [_capture(
            "TAF KATL 050500Z 0506/0612 00000KT CAVOK=", "rev-no-tx"
        )],
    }))

    result = materialize_argmax_taf_references(conn, inbox_path=path)

    assert result["inserted"] == 0
    assert result["missing_tx_capture_count"] == 1
    assert result["blocks_entry"] is False
