import json

from amsc_raw_history_archive import DAILY_COVERAGE_DIRECTORY, raw_text_sha256
from scripts import fetch_zuuu_metar_taf as fetcher
from scripts.fetch_zuuu_metar_taf import (
    build_amsc_fact_snapshot_write_plan,
    build_amsc_observation_bundle,
    filtered_report_texts,
    query_params,
    source_payloads_from_amsc_payload,
)
from metar_strong_likelihood_runtime import build_metar_strong_likelihood


SAMPLE_PAYLOAD = {
    "code": 200,
    "msg": "操作成功",
    "data": [
        {
            "cccc": "ZUUU",
            "airportName": "成都双流机场",
            "airportEnName": "CHENGDU",
            "order": 0,
            "content": "METAR ZUUU 051430Z 18003MPS 140V210 CAVOK 32/20 Q1001 NOSIG=",
        },
        {
            "cccc": "ZUUU",
            "airportName": "成都双流机场",
            "airportEnName": "CHENGDU",
            "order": 0,
            "content": (
                "TAF ZUUU 050904Z 0512/0612 20003MPS 6000 SCT040 "
                "TX36/0607Z TN27/0522Z TEMPO 0513/0516 FEW030CB SCT030="
            ),
        },
        {
            "cccc": "ZUUU",
            "content": "SPECI ZUUU 051431Z 18003MPS CAVOK 32/20 Q1001=",
        },
    ],
}


def test_explicit_chrome_profile_root_survives_daemon_home_boundary(tmp_path, monkeypatch):
    chrome_root = tmp_path / "owner-chrome"
    leveldb = chrome_root / "Default" / "Local Storage" / "leveldb"
    leveldb.mkdir(parents=True)
    token = "1234$$12345678-1234-1234-1234-123456789abc"
    (leveldb / "000001.log").write_bytes(b"https://www.amsc.net.cn sessionId " + token.encode("ascii"))
    monkeypatch.setenv("AMSC_CHROME_PROFILE_ROOT", str(chrome_root))
    monkeypatch.delenv("AMSC_SESSION_ID", raising=False)
    monkeypatch.delenv("AMSC_TOKEN", raising=False)

    candidates = fetcher.token_candidates()
    assert candidates[0] == token
    assert token in candidates


def test_filtered_report_texts_keeps_metar_speci_and_taf_prefixes():
    reports = filtered_report_texts(SAMPLE_PAYLOAD)

    assert len(reports) == 3
    assert reports[0].startswith("METAR ZUUU 051430Z")
    assert reports[1].startswith("TAF ZUUU 050904Z")
    assert reports[2].startswith("SPECI ZUUU 051431Z")


def test_amsc_query_requests_special_report_type_in_addition_to_regular_metar():
    assert "SP" in {value for value in query_params("token", 6)["tt"].split(",") if value}


def test_amsc_payloads_parse_metar_speci_and_taf_fact_layer_fields():
    sources = source_payloads_from_amsc_payload(
        SAMPLE_PAYLOAD,
        fetched_at="2026-07-05T14:31:00Z",
        duration_ms=321.0,
    )

    assert len(sources) == 3
    metar = sources[0]
    assert metar["source"] == "amsc_metar"
    assert metar["event_date"] == "2026-07-05"
    assert metar["valid_time_utc"] == "2026-07-05T14:30:00Z"
    assert metar["valid_time_local"] == "2026-07-05T22:30:00+08:00"
    assert metar["temperature_c"] == 32.0
    assert metar["dewpoint_c"] == 20.0
    assert metar["visibility_m"] == 9999.0
    assert metar["cloud_text"] == "CAVOK"
    assert metar["cloud_cover_pct"] == 5.0
    assert metar["low_cloud_cover_pct"] == 5.0
    assert metar["wind_dir_deg"] == 180.0
    assert metar["wind_speed_mps"] == 3.0
    assert metar["pressure_hpa"] == 1001.0
    assert metar["raw_ref"].startswith("METAR ZUUU")
    assert metar["report_type"] == "METAR"
    assert metar["first_seen_at"] == "2026-07-05T14:31:00Z"
    assert metar["as_of_eligible_at"] == "2026-07-05T14:31:00Z"
    assert metar["arrival_time_quality"] == "observed_first_seen"

    taf = sources[1]
    assert taf["source"] == "amsc_taf"
    assert taf["issue_time_utc"] == "2026-07-05T09:04:00Z"
    assert taf["valid_time_utc"] == "2026-07-06T07:00:00Z"
    assert taf["valid_time_local"] == "2026-07-06T15:00:00+08:00"
    assert taf["forecast_peak_c"] == 36.0
    assert taf["raw_ref"].startswith("TAF ZUUU")

    speci = sources[2]
    assert speci["source"] == "amsc_metar"
    assert speci["report_type"] == "SPECI"
    assert speci["valid_time_utc"] == "2026-07-05T14:31:00Z"
    assert speci["temperature_c"] == 32.0
    assert speci["raw_ref"].startswith("SPECI ZUUU")


def test_amsc_payloads_capture_taf_amd_as_weak_forecast_revision():
    sources = source_payloads_from_amsc_payload(
        {
            "data": [
                {
                    "cccc": "ZUUU",
                    "content": (
                        "TAF AMD ZUUU 150530Z 1506/1606 02004MPS 6000 SCT040 "
                        "TX37/1508Z TN26/1522Z="
                    ),
                }
            ]
        },
        fetched_at="2026-07-15T05:31:00Z",
    )
    assert len(sources) == 1
    taf = sources[0]
    assert taf["source_id"] == "zuuu_taf_forecast_amsc"
    assert taf["forecast_revision_kind"] == "amendment"
    assert taf["taf_modifier"] == "AMD"
    assert taf["forecast_peak_c"] == 37.0
    assert taf["source_revision_id"].startswith("tafrev_")
    assert taf["first_seen_at"] == "2026-07-15T05:31:00Z"


def test_metar_current_observation_is_separated_from_trend_and_keeps_all_fields():
    payload = {
        "code": 200,
        "data": [
            {
                "cccc": "ZUUU",
                "content": (
                    "METAR ZUUU 100600Z 01005G08MPS 330V040 7000 FEW040TCU "
                    "32/24 Q0998 BECMG TL0730 -TSRA FEW030CB="
                ),
            }
        ],
    }

    metar = source_payloads_from_amsc_payload(
        payload,
        fetched_at="2026-07-10T06:01:00Z",
        duration_ms=123.0,
    )[0]

    assert metar["cloud_text"] == "FEW040TCU"
    assert metar["weather_text"] == ""
    assert metar["metar_current_no_significant_weather"] is True
    assert metar["metar_trend_text"].startswith("BECMG TL0730")
    assert "TSRA" in metar["metar_trend_weather_text"]
    assert metar["metar_trend_cloud_text"] == "FEW030CB"
    assert metar["wind_dir_deg"] == 10.0
    assert metar["wind_speed_mps"] == 5.0
    assert metar["wind_gust_mps"] == 8.0
    assert metar["wind_variable"] is True
    assert metar["wind_variable_from_deg"] == 330.0
    assert metar["wind_variable_to_deg"] == 40.0
    assert metar["visibility_m"] == 7000.0
    assert metar["pressure_hpa"] == 998.0


def test_observation_bundle_uses_source_observation_contract_and_stays_read_only():
    bundle = build_amsc_observation_bundle(
        SAMPLE_PAYLOAD,
        fetched_at="2026-07-05T14:31:00Z",
        duration_ms=321.0,
        include_payload=True,
    )

    assert bundle["schema"] == "amsc_zuuu_metar_taf_observation_bundle_v1"
    assert bundle["read_only"] is True
    assert bundle["guards"]["writes_archive_db"] is False
    assert bundle["guards"]["writes_facts_sqlite"] is False
    assert bundle["source_payload_count"] == 3
    trace = bundle["source_acquisition_trace"]
    assert trace["schema"] == "weather_source_acquisition_trace_v1"
    assert trace["source_class_counts"]["hard_observation_candidate"] == 2
    assert trace["source_class_counts"]["forecast_weak_source"] == 1

    by_source = {row["source_id"]: row for row in bundle["source_observations"]}
    assert by_source["zuuu_metar_raw_amsc"]["source_observation_id"].startswith("srcobs_")
    assert by_source["zuuu_metar_raw_amsc"]["hard_eligible"] is True
    assert by_source["zuuu_metar_raw_amsc"]["temperature_c"] == 32.0
    assert by_source["zuuu_taf_forecast_amsc"]["hard_eligible"] is False
    assert by_source["zuuu_taf_forecast_amsc"]["forecast_peak_c"] == 36.0
    hard_report_types = {
        row["payload"]["report_type"]
        for row in bundle["source_observations"]
        if row["source_id"] == "zuuu_metar_raw_amsc"
    }
    assert hard_report_types == {"METAR", "SPECI"}


def test_observation_bundle_builds_fact_snapshot_writer_dry_run_plan_without_apply():
    bundle = build_amsc_observation_bundle(
        SAMPLE_PAYLOAD,
        fetched_at="2026-07-05T14:31:00Z",
        duration_ms=321.0,
        include_payload=True,
    )

    plan = build_amsc_fact_snapshot_write_plan(bundle)

    assert plan["schema"] == "weather_fact_snapshot_writer_plan_v1"
    assert plan["ok"] is True
    assert plan["read_only"] is True
    assert plan["dry_run"] is True
    assert plan["apply_supported"] is False
    assert plan["execution_mode"] == "no_apply_fact_snapshot_write_plan"
    assert plan["write_latest"] is False
    assert plan["slug"] == "highest-temperature-in-chengdu-on-july-5-2026"
    assert plan["event_date"] == "2026-07-05"
    assert plan["run_date"] == "2026-07-05"
    assert plan["fact_as_of"] == "2026-07-05T14:31:00Z"
    assert plan["source_observation_ids"] == bundle["source_observation_ids"]
    assert plan["source_acquisition_trace_summary"]["schema"] == "weather_source_acquisition_trace_v1"
    assert plan["source_acquisition_trace_summary"]["source_count"] == 3
    assert plan["source_acquisition_trace_summary"]["hard_observation_candidate_count"] == 2

    assert plan["db_write_guard"]["executes_sql"] is False
    assert plan["db_write_guard"]["writes_archive_db"] is False
    assert plan["db_write_guard"]["writes_facts_sqlite"] is False
    assert plan["db_write_guard"]["writes_event_fact_history"] is False
    assert plan["db_write_guard"]["writes_event_fact_latest"] is False
    assert plan["db_write_guard"]["historical_backfill"] is False
    assert plan["db_write_guard"]["places_orders"] is False
    assert plan["db_write_guard"]["restarts_launchd"] is False
    assert plan["guards"]["executes_sql"] is False
    assert plan["guards"]["writes_archive_db"] is False
    assert plan["guards"]["writes_facts_sqlite"] is False
    assert plan["guards"]["historical_backfill"] is False
    assert plan["guards"]["places_orders"] is False
    assert plan["guards"]["restarts_launchd"] is False

    actions = {row["action"]: row for row in plan["plan_actions"]}
    assert actions["append_event_fact_history"]["would_write"] is True
    assert actions["append_event_fact_history"]["apply_status"] == "blocked_no_apply"
    assert actions["repoint_event_fact_latest"]["would_write"] is False
    assert actions["repoint_event_fact_latest"]["block_reason"] == "latest_repoint_disabled_by_caller"

    payload = plan["effective_payload"]
    features = payload["weather_likelihood_features"]
    assert payload["schema"] == "amsc_zuuu_metar_taf_fact_payload_v1"
    assert payload["source_update_cadence_sec"] == 1800
    assert features["source_update_cadence_sec"] == 1800
    assert features["zuuu_metar_raw_amsc_latest_at"] == "2026-07-05T14:31:00Z"
    assert features["zuuu_metar_raw_amsc_latest_local"] == "2026-07-05T22:31:00+08:00"
    assert features["zuuu_metar_raw_amsc_fetched_at"] == "2026-07-05T14:31:00Z"
    assert features["zuuu_metar_raw_amsc_latest_fetched_at"] == "2026-07-05T14:31:00Z"
    assert features["zuuu_metar_raw_amsc_response_completed_at"] == "2026-07-05T14:31:00Z"
    assert features["zuuu_metar_raw_amsc_fetch_duration_ms"] == 321.0
    assert features["zuuu_metar_raw_amsc_latest_temp_c"] == 32.0
    assert features["visibility_m"] == 9999.0
    assert features["zuuu_metar_raw_amsc_latest_wind_speed_mps"] == 3.0
    assert features["zuuu_metar_raw_amsc_today_max_c"] == 32.0
    assert features["zuuu_metar_raw_amsc_today_max_fetched_at"] == "2026-07-05T14:31:00Z"
    assert features["taf_forecast_peak_c"] is None
    assert features["taf_forecast_peak_at"] == ""
    assert "expected_peak_hour_local" not in features
    assert payload["source_observation_ids"] == bundle["source_observation_ids"]


def test_amsc_fact_features_ignore_cross_day_taf_for_event_peak_window():
    payload = {
        "code": 200,
        "data": [
            {
                "cccc": "ZUUU",
                "content": "METAR ZUUU 080930Z VRB01MPS 9999 SCT040 38/21 Q0999 NOSIG=",
            },
            {
                "cccc": "ZUUU",
                "content": "TAF ZUUU 080702Z 0809/0818 22003MPS 6000 SCT040 TX38/0809Z TN26/0818Z=",
            },
            {
                "cccc": "ZUUU",
                "content": "TAF ZUUU 080902Z 0812/0912 22003MPS 6000 NSC TX38/0907Z TN26/0822Z=",
            },
        ],
    }
    bundle = build_amsc_observation_bundle(
        payload,
        fetched_at="2026-07-08T09:31:00Z",
        duration_ms=321.0,
        include_payload=True,
    )
    plan = build_amsc_fact_snapshot_write_plan(bundle)
    features = plan["effective_payload"]["weather_likelihood_features"]

    assert features["taf_forecast_peak_at"] == "2026-07-08T09:00:00Z"
    assert features["taf_forecast_peak_local"] == "2026-07-08T17:00:00+08:00"
    assert features["expected_peak_hour_local"] == 17.0
    assert features["expected_peak_window_start_hour_local"] == 16.0
    assert features["expected_peak_window_end_hour_local"] == 19.0


def test_amsc_fact_features_include_half_hour_temperature_path_and_peak_window():
    payload = {
        "code": 200,
        "data": [
            {
                "cccc": "ZUUU",
                "content": "METAR ZUUU 051330Z 18003MPS 9999 FEW020 31/21 Q1001 NOSIG=",
            },
            {
                "cccc": "ZUUU",
                "content": "METAR ZUUU 051400Z 18003MPS 9999 FEW020 32/20 Q1001 NOSIG=",
            },
            {
                "cccc": "ZUUU",
                "content": "METAR ZUUU 051430Z 18003MPS CAVOK 32/20 Q1001 NOSIG=",
            },
            {
                "cccc": "ZUUU",
                "content": (
                    "TAF ZUUU 050904Z 0512/0612 20003MPS 6000 SCT040 "
                    "TX36/0507Z TN27/0522Z="
                ),
            },
        ],
    }
    bundle = build_amsc_observation_bundle(
        payload,
        fetched_at="2026-07-05T14:31:00Z",
        duration_ms=321.0,
        include_payload=True,
    )
    plan = build_amsc_fact_snapshot_write_plan(bundle)
    features = plan["effective_payload"]["weather_likelihood_features"]

    points = features["zuuu_metar_raw_amsc_today_temperature_points"]
    assert [point["temperature_c"] for point in points] == [31.0, 32.0, 32.0]
    assert [point["at"] for point in points] == [
        "2026-07-05T13:30:00Z",
        "2026-07-05T14:00:00Z",
        "2026-07-05T14:30:00Z",
    ]
    assert all(point["fetched_at"] == "2026-07-05T14:31:00Z" for point in points)
    assert points[-1]["visibility_m"] == 9999.0
    assert points[-1]["cloud_text"] == "CAVOK"
    assert points[-1]["weather_text"] == ""
    assert points[-1]["wind_speed_mps"] == 3.0
    assert points[-1]["wind_dir_deg"] == 180.0
    assert points[-1]["pressure_hpa"] == 1001.0
    assert features["zuuu_metar_raw_amsc_temperature_point_count"] == 3
    assert features["zuuu_metar_raw_amsc_today_max_c"] == 32.0
    assert features["temperature_momentum_source"] == "zuuu_metar_raw_amsc"
    assert features["zuuu_metar_raw_amsc_temperature_momentum_30m_c_per_hour"] == 0.0
    assert features["zuuu_metar_raw_amsc_temperature_momentum_1h_c_per_hour"] == 1.0
    assert "zuuu_metar_raw_amsc_temperature_momentum_60m_c_per_hour" not in features
    assert "zuuu_metar_raw_amsc_temperature_momentum_45m_c_per_hour" not in features
    assert "temperature_momentum_45m_c_per_hour" not in features
    assert "temperature_momentum_15m_c_per_hour" not in features
    assert features["temperature_trajectory_realization_state"] == "platform"
    assert features["expected_peak_hour_local"] == 15.0
    assert features["expected_peak_window_start_hour_local"] == 14.0
    assert features["expected_peak_window_end_hour_local"] == 17.0
    assert features["expected_peak_window_basis"] == "taf_tx_time_expanded_not_single_point"


def test_amsc_momentum_adds_half_hour_nodes_without_renaming_hour_windows():
    payload = {
        "code": 200,
        "data": [
            {
                "cccc": "ZUUU",
                "content": "METAR ZUUU 051330Z 18003MPS 9999 FEW020 31/21 Q1001 NOSIG=",
            },
            {
                "cccc": "ZUUU",
                "content": "METAR ZUUU 051400Z 18003MPS 9999 FEW020 32/20 Q1001 NOSIG=",
            },
            {
                "cccc": "ZUUU",
                "content": "METAR ZUUU 051430Z 18003MPS CAVOK 33/20 Q1001 NOSIG=",
            },
            {
                "cccc": "ZUUU",
                "content": "METAR ZUUU 051500Z 18003MPS CAVOK 34/20 Q1001 NOSIG=",
            },
        ],
    }
    bundle = build_amsc_observation_bundle(
        payload,
        fetched_at="2026-07-05T15:01:00Z",
        duration_ms=321.0,
        include_payload=True,
    )
    plan = build_amsc_fact_snapshot_write_plan(bundle)
    features = plan["effective_payload"]["weather_likelihood_features"]

    assert features["temperature_momentum_30m_c_per_hour"] == 0.0
    assert features["temperature_momentum_1h_c_per_hour"] == 2.0
    assert features["temperature_momentum_90m_c_per_hour"] == 2.0
    assert "temperature_momentum_60m_c_per_hour" not in features
    assert "temperature_momentum_45m_c_per_hour" not in features
    assert features["temperature_momentum_available_horizons"] == "30m,1h,90m"


def test_amsc_half_hour_plus_one_c_is_quantization_noise_not_recent_confirmation():
    payload = {
        "code": 200,
        "data": [
            {
                "cccc": "ZUUU",
                "content": "METAR ZUUU 090400Z VRB01MPS 9999 FEW033 36/23 Q1001 NOSIG=",
            },
            {
                "cccc": "ZUUU",
                "content": "METAR ZUUU 090430Z VRB01MPS 9999 FEW033 37/23 Q1001 NOSIG=",
            },
            {
                "cccc": "ZUUU",
                "content": "TAF ZUUU 090904Z 0912/1012 VRB02MPS CAVOK TX40/0907Z TN27/0922Z=",
            },
        ],
    }
    bundle = build_amsc_observation_bundle(
        payload,
        fetched_at="2026-07-09T04:31:00Z",
        duration_ms=321.0,
        include_payload=True,
    )
    plan = build_amsc_fact_snapshot_write_plan(bundle)
    features = plan["effective_payload"]["weather_likelihood_features"]

    assert features["zuuu_metar_raw_amsc_temperature_momentum_30m_c_per_hour"] == 0.0
    assert features["temperature_momentum_30m_c_per_hour"] == 0.0
    assert "temperature_momentum_45m_c_per_hour" not in features

    result = build_metar_strong_likelihood(
        buckets=["36°C", "37°C", "38°C", "39°C", "40°C", "41°C or higher"],
        observation_inputs=features,
        as_of="2026-07-09T12:30:00+08:00",
        event_date="2026-07-09",
    )
    trajectory = result["temperature_trajectory"]
    assert trajectory["confirmed_windows"] == []
    assert trajectory["recent_confirmed_windows"] == []
    assert trajectory["gate_state"] != "confirmed"
    assert trajectory["response_scale"] < 1.0


def test_cli_write_plan_json_outputs_no_apply_plan(monkeypatch, capsys):
    monkeypatch.setattr(fetcher, "token_candidates", lambda: ["session-redacted"])
    monkeypatch.setattr(fetcher, "fetch_reports_with_transport", lambda token, count, timeout: (SAMPLE_PAYLOAD, {"response_started_at": "2026-07-05T14:30:59Z"}))
    monkeypatch.setattr(fetcher, "utc_now_iso", lambda: "2026-07-05T14:31:00Z")
    monkeypatch.setattr(
        fetcher.sys,
        "argv",
        ["fetch_zuuu_metar_taf.py", "--count", "1", "--write-plan-json"],
    )

    assert fetcher.main() == 0

    output = capsys.readouterr().out
    plan = json.loads(output)
    assert "session-redacted" not in output
    assert plan["schema"] == "weather_fact_snapshot_writer_plan_v1"
    assert plan["dry_run"] is True
    assert plan["apply_supported"] is False
    assert plan["db_write_guard"]["executes_sql"] is False
    assert plan["guards"]["writes_archive_db"] is False
    assert plan["effective_payload"]["weather_likelihood_features"]["zuuu_metar_raw_amsc_latest_temp_c"] == 32.0


def test_observation_bundle_can_enable_apply_contract_without_executing_sql():
    bundle = build_amsc_observation_bundle(
        SAMPLE_PAYLOAD,
        fetched_at="2026-07-05T14:31:00Z",
        duration_ms=321.0,
        include_payload=True,
    )

    plan = build_amsc_fact_snapshot_write_plan(
        bundle,
        enable_apply_contract=True,
        write_latest=True,
    )

    assert plan["dry_run"] is True
    assert plan["apply_supported"] is True
    assert plan["execution_mode"] == "explicit_apply_contract_ready_fact_snapshot_write_plan"
    assert plan["write_latest"] is False
    assert plan["caller"]["apply_contract_enabled"] is True
    assert plan["caller"]["write_latest_requested"] is True
    assert plan["caller"]["write_latest_effective"] is False
    assert plan["caller"]["write_latest_block_reason"] == fetcher.AMSC_LATEST_REPOINT_BLOCK_REASON
    assert plan["source_acquisition_latest_repoint_policy"] == {
        "requested": True,
        "effective": False,
        "block_reason": fetcher.AMSC_LATEST_REPOINT_BLOCK_REASON,
        "boundary": "source_acquisition_may_append_history_but_must_not_repoint_posterior_ssot_latest",
    }
    assert plan["db_write_guard"]["executes_sql"] is False
    assert plan["db_write_guard"]["writes_archive_db"] is False
    assert plan["guards"]["executes_sql"] is False

    actions = {row["action"]: row for row in plan["plan_actions"]}
    assert actions["append_event_fact_history"]["would_write"] is True
    assert actions["append_event_fact_history"]["apply_status"] == "ready_for_explicit_apply"
    assert actions["repoint_event_fact_latest"]["would_write"] is False
    assert actions["repoint_event_fact_latest"]["block_reason"] == "latest_repoint_disabled_by_caller"


def test_cli_write_plan_json_can_write_apply_contract_plan_to_file(monkeypatch, capsys, tmp_path):
    output_path = tmp_path / "write_plan_latest.json"
    monkeypatch.setattr(fetcher, "token_candidates", lambda: ["session-redacted"])
    monkeypatch.setattr(fetcher, "fetch_reports_with_transport", lambda token, count, timeout: (SAMPLE_PAYLOAD, {"response_started_at": "2026-07-05T14:30:59Z"}))
    monkeypatch.setattr(fetcher, "utc_now_iso", lambda: "2026-07-05T14:31:00Z")
    monkeypatch.setattr(
        fetcher.sys,
        "argv",
        [
            "fetch_zuuu_metar_taf.py",
            "--count",
            "1",
            "--write-plan-json",
            "--enable-apply-contract",
            "--write-latest",
            "--output-json",
            str(output_path),
        ],
    )

    assert fetcher.main() == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    output = output_path.read_text(encoding="utf-8")
    plan = json.loads(output)
    assert "session-redacted" not in output
    assert plan["dry_run"] is True
    assert plan["apply_supported"] is True
    assert plan["write_latest"] is False
    assert plan["caller"]["write_latest_requested"] is True
    assert plan["caller"]["write_latest_effective"] is False
    assert plan["caller"]["write_latest_block_reason"] == fetcher.AMSC_LATEST_REPOINT_BLOCK_REASON
    assert plan["db_write_guard"]["executes_sql"] is False


def test_cli_archive_dir_preserves_raw_reports_without_leaking_session(monkeypatch, capsys, tmp_path):
    archive_dir = tmp_path / "raw_history"
    monkeypatch.setattr(fetcher, "token_candidates", lambda: ["session-redacted"])
    monkeypatch.setattr(fetcher, "fetch_reports_with_transport", lambda token, count, timeout: (SAMPLE_PAYLOAD, {"response_started_at": "2026-07-05T14:30:59Z"}))
    monkeypatch.setattr(fetcher, "utc_now_iso", lambda: "2026-07-05T14:31:00Z")
    monkeypatch.setattr(
        fetcher.sys,
        "argv",
        [
            "fetch_zuuu_metar_taf.py",
            "--count",
            "1",
            "--archive-dir",
            str(archive_dir),
            "--write-plan-json",
        ],
    )

    assert fetcher.main() == 0

    output = capsys.readouterr().out
    assert "session-redacted" not in output
    plan = json.loads(output)
    archive = plan["effective_payload"]["amsc_raw_history_archive"]
    assert archive["schema"] == "amsc_raw_history_archive_result_v1"
    assert archive["raw_object_count"] == 3
    assert len(archive["coverage_manifest_refs"]) == 1
    assert archive["guards"]["credentials_persisted"] is False

    metar = next(
        row
        for row in plan["effective_payload"]["source_observations"]
        if row["source_id"] == "zuuu_metar_raw_amsc"
    )
    assert metar["raw_sha256"] == raw_text_sha256(SAMPLE_PAYLOAD["data"][0]["content"])
    assert metar["payload"]["raw_object_ref"].startswith("objects/")
    assert metar["payload"]["first_seen_at"] == "2026-07-05T14:31:00Z"
    assert metar["payload"]["arrival_time_quality"] == "observed_first_seen"

    speci = next(
        row
        for row in plan["effective_payload"]["source_observations"]
        if row["source_id"] == "zuuu_metar_raw_amsc" and row["payload"]["report_type"] == "SPECI"
    )
    assert speci["raw_sha256"] == raw_text_sha256(SAMPLE_PAYLOAD["data"][2]["content"])
    assert speci["payload"]["raw_object_ref"].startswith("objects/")

    manifests = list((archive_dir / "manifests").glob("**/*.json"))
    coverage_manifests = list((archive_dir / DAILY_COVERAGE_DIRECTORY).glob("**/*.json"))
    assert len(manifests) == 1
    assert len(coverage_manifests) == 1
    assert archive["coverage_manifest_refs"] == [
        str(coverage_manifests[0].relative_to(archive_dir))
    ]
    assert not (archive_dir / "coverage").exists()
    assert "session-redacted" not in manifests[0].read_text(encoding="utf-8")


def test_cli_can_defer_daily_coverage_outside_probability_hot_path(monkeypatch, capsys, tmp_path):
    archive_dir = tmp_path / "raw_history"
    monkeypatch.setattr(fetcher, "token_candidates", lambda: ["session-redacted"])
    monkeypatch.setattr(
        fetcher,
        "fetch_reports_with_transport",
        lambda token, count, timeout: (
            SAMPLE_PAYLOAD,
            {"response_started_at": "2026-07-05T14:30:59Z"},
        ),
    )
    monkeypatch.setattr(fetcher, "utc_now_iso", lambda: "2026-07-05T14:31:00Z")
    monkeypatch.setattr(
        fetcher.sys,
        "argv",
        [
            "fetch_zuuu_metar_taf.py",
            "--count",
            "1",
            "--archive-dir",
            str(archive_dir),
            "--defer-daily-coverage",
            "--write-plan-json",
        ],
    )

    assert fetcher.main() == 0

    plan = json.loads(capsys.readouterr().out)
    archive = plan["effective_payload"]["amsc_raw_history_archive"]
    assert archive["coverage_manifest_refs"] == []
    assert archive["daily_coverage_state"] == "deferred_after_probability_t3"
    assert list((archive_dir / "objects").glob("**/*.json.gz"))
    assert list((archive_dir / "manifests").glob("**/*.json"))
    assert not (archive_dir / DAILY_COVERAGE_DIRECTORY).exists()
