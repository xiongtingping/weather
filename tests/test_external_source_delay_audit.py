import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "external_source_delay_audit.py"
spec = importlib.util.spec_from_file_location("external_source_delay_audit", SCRIPT_PATH)
audit = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(audit)


def test_parse_wu_display_local_to_utc():
    parsed = audit.parse_wu_display_local("5:30 PM CST on July 3, 2026 (GMT +8)")

    assert audit.dt_iso(parsed) == "2026-07-03T09:30:00Z"


def test_analyze_samples_computes_lead_vs_awc_cache():
    sample = {
        "sample_started_at": "2026-07-03T09:37:20.000Z",
        "sample_completed_at": "2026-07-03T09:37:31.000Z",
        "station_id": "ZUUU",
        "event_date": "2026-07-03",
        "timezone": "Asia/Shanghai",
        "sources": [
            {
                "source": "twc_current_icao_metric",
                "ok": True,
                "valid_time_utc": "2026-07-03T09:35:39Z",
                "valid_time_local": "2026-07-03T17:35:39+0800",
                "temperature_c": 29,
                "transport": {"request_completed_at": "2026-07-03T09:37:27Z", "duration_ms": 4327},
            },
            {
                "source": "awc_cache_metars_csv_gz",
                "ok": True,
                "valid_time_utc": "2026-07-03T09:00:00Z",
                "valid_time_local": "2026-07-03T17:00:00+08:00",
                "temperature_c": 30,
                "transport": {"request_completed_at": "2026-07-03T09:37:31Z", "duration_ms": 3181},
            },
        ],
    }

    result = audit.analyze_samples([sample])
    rows = {row["source"]: row for row in result["samples"][0]["rows"]}

    assert result["samples"][0]["leader_source"] == "twc_current_icao_metric"
    assert rows["twc_current_icao_metric"]["lead_sec_vs_awc_cache"] == 2139.0
    assert rows["awc_cache_metars_csv_gz"]["lead_sec_vs_awc_cache"] == 0.0
    assert rows["twc_current_icao_metric"]["source_age_sec_at_response"] == 108.0


def test_build_report_guard_is_read_only(tmp_path):
    evidence = tmp_path / "evidence.ndjson"
    evidence.write_text(
        '{"schema":"peak_window_evidence_chain_v1","sample_started_at":"2026-07-03T09:00:00Z",'
        '"sources":[]}\n',
        encoding="utf-8",
    )

    args = audit.parse_args(["--input", str(evidence), "--no-write"])
    report = audit.build_report(args)

    assert report["guards"]["writes_archive_db"] is False
    assert report["guards"]["writes_facts_sqlite"] is False
    assert report["guards"]["writes_auto_trade_decisions"] is False
    assert report["guards"]["places_orders"] is False
