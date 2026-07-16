from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from wunderground_daily_settlement_parser import SCHEMA_VERSION, parse_wunderground_daily_high


URL = "https://api.weather.com/v1/location/ZUUU%3A9%3ACN/observations/historical.json?apiKey=REDACTED&units=m&startDate=20260703&endDate=20260703"


def _epoch(local: str) -> int:
    return int(datetime.fromisoformat(local).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


def test_parser_computes_target_local_day_max_from_metric_observations() -> None:
    raw = json.dumps({"observations": [
        {"valid_time_gmt": _epoch("2026-07-03T13:00:00"), "temp": 30},
        {"valid_time_gmt": _epoch("2026-07-03T14:00:00"), "temp": 31},
    ]}, separators=(",", ":")).encode()
    result = parse_wunderground_daily_high(raw, event_date="2026-07-03", source_url=URL)
    assert result["ok"] is True
    assert result["schema"] == SCHEMA_VERSION
    assert result["daily_high_c"] == 31.0
    assert result["source_unit"] == "C"
    assert result["observation_count"] == 2
    assert result["extraction_evidence"] in raw.decode()


def test_parser_excludes_other_local_days() -> None:
    raw = json.dumps({"observations": [
        {"valid_time_gmt": _epoch("2026-07-02T23:30:00"), "temp": 40},
        {"valid_time_gmt": _epoch("2026-07-03T14:00:00"), "temp": 31},
    ]}, separators=(",", ":")).encode()
    result = parse_wunderground_daily_high(raw, event_date="2026-07-03", source_url=URL)
    assert result["ok"] is True
    assert result["daily_high_c"] == 31.0


def test_parser_rejects_rendered_forecast_arrays_and_unbound_units() -> None:
    raw = b'{"calendarDay":["2026-07-03"],"calendarDayTemperatureMax":[99]}'
    result = parse_wunderground_daily_high(raw, event_date="2026-07-03", source_url="")
    assert result["ok"] is False
    assert result["block_reasons"] == ["station_history_request_contract_invalid_or_unit_unbound"]
