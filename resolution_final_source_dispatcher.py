"""Pure raw Wunderground capture → parser → final-S source proposal chain."""

from __future__ import annotations

from typing import Any, Mapping

from resolution_final_source_runtime import build_wunderground_settlement_final_source_observation
from wunderground_daily_settlement_parser import parse_wunderground_daily_high


SCHEMA_VERSION = "resolution_final_source_dispatcher_v1"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def build_wunderground_final_settlement_source_proposal(
    *,
    authority_contract: Mapping[str, Any],
    raw_capture: Mapping[str, Any],
    parser_completed_at: str,
    market_finality_attestation: Mapping[str, Any],
) -> dict[str, Any]:
    """Produce S only through the retained raw bytes and finality evidence."""

    capture = _mapping(raw_capture)
    attestation = _mapping(market_finality_attestation)
    event_date = _text(capture.get("event_date"))[:10]
    parse = parse_wunderground_daily_high(
        capture.get("raw_payload", b""),
        event_date=event_date,
        source_url=_text(capture.get("source_url")),
    )
    blockers: list[str] = []
    if capture.get("ok") is not True:
        blockers.append("settlement_dispatch_raw_capture_not_verified")
    if parse.get("ok") is not True:
        blockers.extend(f"settlement_dispatch_parser:{reason}" for reason in parse.get("block_reasons") or ["blocked"])
    if attestation.get("ok") is not True or attestation.get("market_resolution_final") is not True:
        blockers.append("settlement_dispatch_market_finality_not_attested")
    if _text(attestation.get("event_date"))[:10] != event_date:
        blockers.append("settlement_dispatch_finality_event_date_mismatch")
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(blockers)),
            "raw_capture": {key: value for key, value in capture.items() if key != "raw_payload"},
            "parser": parse,
            "read_only": True,
            "writes_archive_db": False,
            "places_orders": False,
        }
    source_result = build_wunderground_settlement_final_source_observation(
        event_date=event_date,
        authority_contract=authority_contract,
        capture={
            "station_id": capture.get("station_id"),
            "event_date": event_date,
            "source_url": capture.get("source_url"),
            "raw_payload": capture.get("raw_payload"),
            "raw_ref": capture.get("raw_ref"),
            "raw_sha256": capture.get("raw_sha256"),
            "fetched_at": capture.get("fetched_at"),
            "response_completed_at": capture.get("response_completed_at"),
            "parsed_at": parser_completed_at,
            "daily_high_c": parse.get("daily_high_c"),
            "extractor_version": parse.get("extractor_version"),
            "extraction_evidence": parse.get("extraction_evidence"),
            "market_resolution_finality_attestation": attestation,
        },
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": source_result.get("ok") is True,
        "state": "settlement_final_source_ready" if source_result.get("ok") is True else "blocked",
        "block_reasons": list(source_result.get("block_reasons") or []),
        "source_result": source_result,
        "parser": parse,
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "build_wunderground_final_settlement_source_proposal"]
