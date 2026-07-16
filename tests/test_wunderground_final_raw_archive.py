from __future__ import annotations

import json
from pathlib import Path

from resolution_authority_contract_runtime import validate_resolution_authority_contract
from wunderground_final_raw_archive import (
    archive_wunderground_daily_raw_capture,
    build_wunderground_daily_capture_url,
    load_wunderground_daily_raw_capture,
)


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = json.loads((ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_text())


def test_authority_uses_station_history_api_with_redacted_key_and_metric_units() -> None:
    assert validate_resolution_authority_contract(AUTHORITY)["ok"] is True
    assert build_wunderground_daily_capture_url(
        authority_contract=AUTHORITY, event_date="2026-07-03"
    ) == "https://api.weather.com/v1/location/ZUUU%3A9%3ACN/observations/historical.json?apiKey=REDACTED&units=m&startDate=20260703&endDate=20260703"


def test_raw_capture_is_content_addressed_and_strips_sensitive_headers(tmp_path) -> None:
    result = archive_wunderground_daily_raw_capture(
        archive_dir=tmp_path,
        event_date="2026-07-03",
        source_url="https://api.weather.com/v1/location/ZUUU%3A9%3ACN/observations/historical.json?apiKey=REDACTED&units=m&startDate=20260703&endDate=20260703",
        raw_payload=b'{"observations":[]}',
        requested_at="2026-07-04T00:00:00Z",
        response_started_at="2026-07-04T00:00:01Z",
        response_completed_at="2026-07-04T00:00:02Z",
        response_status=200,
        response_headers={"Content-Type": "text/html", "Set-Cookie": "secret=value"},
    )
    loaded = load_wunderground_daily_raw_capture(archive_dir=tmp_path, manifest_ref=result["manifest_ref"])
    manifest = json.loads((tmp_path / result["manifest_ref"]).read_text())
    assert result["ok"] is True
    assert loaded["raw_sha256"] == result["raw_sha256"]
    assert loaded["raw_payload"].startswith(b'{"observations"')
    assert "Set-Cookie" not in manifest["response_headers"]
    retry = archive_wunderground_daily_raw_capture(
        archive_dir=tmp_path,
        event_date="2026-07-03",
        source_url="https://api.weather.com/v1/location/ZUUU%3A9%3ACN/observations/historical.json?apiKey=REDACTED&units=m&startDate=20260703&endDate=20260703",
        raw_payload=loaded["raw_payload"],
        requested_at="2026-07-04T00:00:00Z",
        response_started_at="2026-07-04T00:00:01Z",
        response_completed_at="2026-07-04T00:00:02Z",
        response_status=200,
    )
    assert retry["raw_object_reused"] is True
