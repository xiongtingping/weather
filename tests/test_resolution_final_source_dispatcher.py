from __future__ import annotations

import json
from pathlib import Path

from market_resolution_finality_runtime import build_market_resolution_finality_attestation
from resolution_final_source_dispatcher import build_wunderground_final_settlement_source_proposal


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = json.loads((ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_text())
DAY = "2026-07-03"


def _finality() -> dict:
    return build_market_resolution_finality_attestation(
        authority_contract=AUTHORITY,
        target_identity="highest-temperature-in-chengdu-on-july-3-2026",
        public_slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date=DAY,
        station_id="ZUUU",
        resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        market_status="resolved",
        resolved_at="2026-07-03T18:00:00Z",
        attested_at="2026-07-03T18:01:00Z",
        market_rule_raw_payload=b"rule",
        market_rule_raw_ref="market-rules/rule.json",
    )


def _capture() -> dict:
    raw = b'{"observations":[{"valid_time_gmt":1783054800,"temp":30},{"valid_time_gmt":1783058400,"temp":31}]}'
    return {
        "ok": True,
        "event_date": DAY,
        "station_id": "ZUUU",
        "source_url": "https://api.weather.com/v1/location/ZUUU%3A9%3ACN/observations/historical.json?apiKey=REDACTED&units=m&startDate=20260703&endDate=20260703",
        "raw_ref": "objects/aa/history.json.gz",
        "raw_sha256": __import__("hashlib").sha256(raw).hexdigest(),
        "raw_payload": raw,
        "fetched_at": "2026-07-03T18:02:00Z",
        "response_completed_at": "2026-07-03T18:01:59Z",
    }


def test_dispatcher_binds_raw_parser_and_finality_before_s_proposal() -> None:
    result = build_wunderground_final_settlement_source_proposal(
        authority_contract=AUTHORITY,
        raw_capture=_capture(),
        parser_completed_at="2026-07-03T18:02:01Z",
        market_finality_attestation=_finality(),
    )
    assert result["ok"] is True
    observation = result["source_result"]["source_observation"]
    assert observation["payload"]["settlement_scalar_c"] == 31.0
    assert observation["role"] == "resolution_settlement_final"


def test_dispatcher_rejects_finality_from_another_event_day() -> None:
    finality = _finality()
    finality["event_date"] = "2026-07-04"
    result = build_wunderground_final_settlement_source_proposal(
        authority_contract=AUTHORITY,
        raw_capture=_capture(),
        parser_completed_at="2026-07-03T18:02:01Z",
        market_finality_attestation=finality,
    )
    assert result["ok"] is False
    assert "settlement_dispatch_finality_event_date_mismatch" in result["block_reasons"]
