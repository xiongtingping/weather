from __future__ import annotations

import json
from pathlib import Path

from market_resolution_finality_runtime import build_market_resolution_finality_attestation
from market_resolution_finality_runtime import (
    build_market_resolution_finality_attestation_from_finality_capture,
    build_market_resolution_finality_attestation_from_target_contract,
)
from resolution_target_contract_runtime import (
    EXTRACTION_SCHEMA_VERSION,
    EXTRACTOR_VERSION,
    build_market_resolution_target_contract_proposal,
)


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = json.loads((ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_text())


def _args() -> dict:
    return {
        "authority_contract": AUTHORITY,
        "target_identity": "highest-temperature-in-chengdu-on-july-3-2026",
        "public_slug": "highest-temperature-in-chengdu-on-july-3-2026",
        "event_date": "2026-07-03",
        "station_id": "ZUUU",
        "resolution_source": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        "market_status": "resolved",
        "resolved_at": "2026-07-03T18:00:00Z",
        "attested_at": "2026-07-03T18:01:00Z",
        "market_rule_raw_payload": b"immutable market rule snapshot",
        "market_rule_raw_ref": "market-rules/2026-07-03/target.json",
    }


def test_finality_attestation_requires_resolved_market_and_raw_rule_snapshot() -> None:
    result = build_market_resolution_finality_attestation(**_args())
    assert result["ok"] is True
    assert result["market_resolution_final"] is True
    assert len(result["market_rule_snapshot_sha256"]) == 64
    assert result["boundary"]["does_not_read_market_outcome"] is True

    blocked_args = _args()
    blocked_args["market_status"] = "open"
    blocked = build_market_resolution_finality_attestation(**blocked_args)
    assert blocked["ok"] is False
    assert "market_finality_market_not_resolved" in blocked["block_reasons"]


def test_finality_attestation_rejects_invalid_event_date() -> None:
    args = _args()
    args["event_date"] = "not-a-date"
    result = build_market_resolution_finality_attestation(**args)
    assert result["ok"] is False
    assert "market_finality_event_date_invalid" in result["block_reasons"]


def test_finality_can_bind_to_l0_target_contract_without_reading_outcome() -> None:
    raw = (
        b'{"slug":"highest-temperature-in-chengdu-on-july-3-2026",'
        b'"event_date":"2026-07-03","station":"ZUUU",'
        b'"resolution_source":"https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",'
        b'"unit":"C","rounding":"nearest-whole","buckets":["31C"]}'
    )
    target = build_market_resolution_target_contract_proposal(
        authority_contract=AUTHORITY,
        target_identity="market_resolution_target_v1:highest-temperature-in-chengdu-on-july-3-2026",
        public_slug="highest-temperature-in-chengdu-on-july-3-2026",
        resolution_local_date="2026-07-03",
        station_id="ZUUU",
        resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        market_rule_raw_payload=raw,
        market_rule_raw_ref="market-rules/rule.json",
        market_rule_extraction={
            "schema": EXTRACTION_SCHEMA_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "public_slug": "highest-temperature-in-chengdu-on-july-3-2026",
            "resolution_local_date": "2026-07-03",
            "station_id": "ZUUU",
            "resolution_source": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
            "temperature_unit": "C",
            "rounding_rule": "nearest-whole",
            "bucket_contract": {"31C": {"lower_inclusive": 30.5, "upper_exclusive": 31.5}},
            "extraction_evidence_fragments": [raw.decode("utf-8")],
        },
    )
    result = build_market_resolution_finality_attestation_from_target_contract(
        authority_contract=AUTHORITY,
        target_contract=target,
        market_rule_raw_payload=raw,
        market_status="resolved",
        resolved_at="2026-07-03T18:00:00Z",
        attested_at="2026-07-03T18:01:00Z",
    )
    assert result["ok"] is True
    assert result["target_contract_sha256"] == target["target_contract_sha256"]
    assert result["boundary"]["does_not_read_market_outcome"] is True


def test_finality_capture_records_resolved_state_receipt_without_outcome() -> None:
    initial_raw = (
        b'{"slug":"highest-temperature-in-chengdu-on-july-3-2026",'
        b'"event_date":"2026-07-03","station":"ZUUU",'
        b'"resolution_source":"https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",'
        b'"unit":"C","rounding":"nearest-whole","buckets":["31C"]}'
    )
    target = build_market_resolution_target_contract_proposal(
        authority_contract=AUTHORITY,
        target_identity="market_resolution_target_v1:highest-temperature-in-chengdu-on-july-3-2026",
        public_slug="highest-temperature-in-chengdu-on-july-3-2026",
        resolution_local_date="2026-07-03",
        station_id="ZUUU",
        resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        market_rule_raw_payload=initial_raw,
        market_rule_raw_ref="market-rules/initial.json",
        market_rule_extraction={
            "schema": EXTRACTION_SCHEMA_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "public_slug": "highest-temperature-in-chengdu-on-july-3-2026",
            "resolution_local_date": "2026-07-03",
            "station_id": "ZUUU",
            "resolution_source": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
            "temperature_unit": "C",
            "rounding_rule": "nearest-whole",
            "bucket_contract": {"31C": {"lower_inclusive": 30.5, "upper_exclusive": 31.5}},
            "extraction_evidence_fragments": [initial_raw.decode("utf-8")],
        },
    )
    finality_raw = (
        b'[{"slug":"highest-temperature-in-chengdu-on-july-3-2026",'
        b'"eventDate":"2026-07-03","closed":true,'
        b'"resolutionSource":"https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",'
        b'"markets":[{"groupItemTitle":"31C","umaResolutionStatus":"resolved",'
        b'"outcomes":["Yes","No"],"outcomePrices":[1,0]}]}]'
    )
    finality = build_market_resolution_finality_attestation_from_finality_capture(
        authority_contract=AUTHORITY,
        target_contract=target,
        market_rule_raw_payload=initial_raw,
        finality_raw_capture={
            "ok": True,
            "raw_ref": "market-finality/2026-07-03.json",
            "raw_sha256": __import__("hashlib").sha256(finality_raw).hexdigest(),
            "raw_payload": finality_raw,
            "response_completed_at": "2026-07-03T18:00:00Z",
        },
    )
    assert finality["ok"] is True
    assert finality["market_finality_raw_ref"] == "market-finality/2026-07-03.json"
    assert finality["resolution_state_observed_at"] == "2026-07-03T18:00:00Z"
    assert finality["boundary"]["does_not_read_market_outcome"] is True
