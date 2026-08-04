import json
import sqlite3

from amsc_taf_revision_runtime import parse_taf_revision
from weather_basket_yes_schema import apply_weather_basket_yes_schema
from ui.server import (
    _attach_production_posterior_display_contract,
    _build_china_airport_board_payload,
    _build_city_data_lineage_health_payload,
    _normalize_city_from_fact,
)


def test_generic_taf_parser_supports_station_and_24_hour_group():
    parsed = parse_taf_revision(
        "TAF ZBAA 190301Z 1906/2006 18004MPS CAVOK TX31/1906Z TN25/2000Z=",
        first_seen_at="2026-07-19T03:01:00Z",
        station_id="ZBAA",
    )
    assert parsed["station_id"] == "ZBAA"
    assert parsed["forecast_peaks"][0]["event_date_local"] == "2026-07-19"


def test_multicity_board_prefers_persisted_taf_and_reports_health(tmp_path):
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
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
            "tafrev_test", "2026-07-20", "ZBAA", "amsc_taf", "original", "",
            "2026-07-19T03:01:00Z", "2026-07-19T03:01:00Z", "1906/2006", 31.0,
            "2026-07-19T06:00:00Z", "clear_dry", "[]", "sha", "TAF ZBAA ...", "",
            json.dumps({"source": "AMSC"}), "available", "2026-07-19T03:01:00Z",
        ),
    )
    conn.commit()
    board = _build_china_airport_board_payload(conn, event_date="2026-07-20", include_catalog=False)
    beijing = next(item for item in board["stations"] if item["station_id"] == "ZBAA")
    assert beijing["taf"]["peak_c"] == 31.0
    assert beijing["taf"]["source"] == "archive.db.amsc_taf_snapshots"
    health = _build_city_data_lineage_health_payload(conn, event_date="2026-07-20")
    assert health["schema"] == "city_data_lineage_health_v1"
    assert {item["field"] for item in health["source_checks"]} >= {"live", "taf", "gfs", "ssot", "basket"}
    assert {row["id"] for row in health["lineage_contract"]["branches"]} >= {
        "official_amsc_taf",
        "official_nwp_projection",
        "legacy_weak_forecast_fallback",
    }
    assert set(health["nwp_source_products"]) >= {"gfs", "icon", "ecmwf"}
    conn.close()


def test_multicity_posterior_identity_is_promoted_from_nested_ssot():
    payload = {
        "posterior_ssot_fact": {
            "schema": "sequential_temperature_posterior_ssot_v4_reported_x",
            "state": "physical_only_market_projection_na",
            "target_identity": "physical_weather_day_v1:ZBAA:2026-07-22",
            "station_id": "ZBAA",
            "city_posterior_scope": "independent_station_day_production",
            "city_probability_not_copied_from_zuuu": True,
            "reported_physical_x_distribution": [
                {"temperature_c": 32.0, "probability": 0.5}
            ],
            "market_bucket_probabilities_na_reason": "resolution_bridge_not_closed",
        }
    }
    _attach_production_posterior_display_contract(payload)
    assert payload["city"] == "Beijing"
    assert payload["station_id"] == "ZBAA"
    assert payload["posterior_state"] == "materialized_physical_posterior"
    assert payload["posterior_materialization"]["state"] == "materialized"
    assert payload["production_consumer_eligible"] is False
    assert _normalize_city_from_fact(payload) == "Beijing"
