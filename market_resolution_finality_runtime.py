"""Build a raw-rule-bound market-finality attestation for one target day.

The attestation certifies only that a market has resolved under an immutable
rule snapshot.  It never reads a resolved bucket/outcome and cannot contribute
weather evidence or a trading signal.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

from resolution_authority_contract_runtime import validate_resolution_authority_contract
from resolution_target_contract_runtime import validate_market_resolution_target_contract_proposal
from source_observation_runtime import raw_payload_sha256
from market_resolution_finality_parser import SCHEMA_VERSION as FINALITY_PARSER_SCHEMA_VERSION, parse_market_resolution_finality


SCHEMA_VERSION = "market_resolution_finality_attestation_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _sha256(value: bytes | str) -> str:
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_market_resolution_finality_attestation(
    *,
    authority_contract: Mapping[str, Any],
    target_identity: str,
    public_slug: str,
    event_date: str,
    station_id: str,
    resolution_source: str,
    market_status: str,
    resolved_at: str,
    attested_at: str,
    market_rule_raw_payload: bytes | str,
    market_rule_raw_ref: str,
    market_rule_raw_sha256: str = "",
) -> dict[str, Any]:
    """Return an attestation only when identity, source, rule raw and state close."""

    authority = validate_resolution_authority_contract(authority_contract)
    reasons: list[str] = []
    if authority.get("ok") is not True:
        reasons.append("market_finality_authority_contract_invalid")
    day = _text(event_date)[:10]
    try:
        datetime.fromisoformat(day)
    except ValueError:
        reasons.append("market_finality_event_date_invalid")
    if not _text(target_identity) or not _text(public_slug):
        reasons.append("market_finality_target_identity_or_slug_missing")
    if _text(station_id).upper() != "ZUUU":
        reasons.append("market_finality_station_invalid")
    settlement = _mapping(authority_contract).get("settlement_s")
    expected_source = _text(_mapping(settlement).get("source_url_base"))
    if _text(resolution_source).rstrip("/") != expected_source.rstrip("/"):
        reasons.append("market_finality_resolution_source_not_authority_bound")
    if _text(market_status).lower() != "resolved":
        reasons.append("market_finality_market_not_resolved")
    resolved = _parse_time(resolved_at)
    attested = _parse_time(attested_at)
    if resolved is None or attested is None or attested < resolved:
        reasons.append("market_finality_timestamps_invalid")
    if not _text(market_rule_raw_ref):
        reasons.append("market_finality_rule_raw_ref_missing")
    if not isinstance(market_rule_raw_payload, (bytes, bytearray, str)) or not market_rule_raw_payload:
        reasons.append("market_finality_rule_raw_payload_missing")
    actual_rule_sha = raw_payload_sha256(market_rule_raw_payload)
    declared_rule_sha = _text(market_rule_raw_sha256).lower()
    if declared_rule_sha and declared_rule_sha != actual_rule_sha:
        reasons.append("market_finality_rule_raw_sha256_binding_mismatch")
    if reasons:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(reasons)),
            "read_only": True,
            "writes_archive_db": False,
            "places_orders": False,
        }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "finality_attested",
        "market_resolution_final": True,
        "target_identity": _text(target_identity),
        "public_slug": _text(public_slug),
        "event_date": day,
        "station_id": "ZUUU",
        "resolution_source": expected_source,
        "resolved_at": _text(resolved_at),
        "attested_at": _text(attested_at),
        "market_rule_snapshot_sha256": actual_rule_sha,
        "market_rule_raw_ref": _text(market_rule_raw_ref),
        "boundary": {
            "does_not_read_market_outcome": True,
            "does_not_read_market_price": True,
            "does_not_build_weather_posterior": True,
            "does_not_place_orders": True,
        },
        "read_only": True,
    }


def build_market_resolution_finality_attestation_from_target_contract(
    *,
    authority_contract: Mapping[str, Any],
    target_contract: Mapping[str, Any],
    market_rule_raw_payload: bytes | str,
    market_status: str,
    resolved_at: str,
    attested_at: str,
) -> dict[str, Any]:
    """Attest finality only through a persisted L0 target-rule contract.

    The target contract binds the raw rule object captured at target creation.
    This wrapper still never reads a market outcome, price, order-book or P&L.
    """

    contract = _mapping(target_contract)
    validation = validate_market_resolution_target_contract_proposal(contract)
    snapshot = _mapping(contract.get("market_rule_snapshot"))
    raw = market_rule_raw_payload if isinstance(market_rule_raw_payload, (bytes, bytearray, str)) else b""
    blockers: list[str] = []
    if validation.get("ok") is not True:
        blockers.append("market_finality_target_contract_invalid")
    raw_sha = raw_payload_sha256(raw) if raw else ""
    if not raw or raw_sha != _text(snapshot.get("raw_sha256")).lower():
        blockers.append("market_finality_target_contract_raw_binding_invalid")
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(blockers)),
            "read_only": True,
            "writes_archive_db": False,
            "places_orders": False,
        }
    result = build_market_resolution_finality_attestation(
        authority_contract=authority_contract,
        target_identity=_text(contract.get("target_identity")),
        public_slug=_text(contract.get("public_slug")),
        event_date=_text(contract.get("resolution_local_date")),
        station_id=_text(contract.get("target_station_id")),
        resolution_source=_text(contract.get("resolution_source")),
        market_status=market_status,
        resolved_at=resolved_at,
        attested_at=attested_at,
        market_rule_raw_payload=raw,
        market_rule_raw_ref=_text(snapshot.get("raw_ref")),
        market_rule_raw_sha256=_text(snapshot.get("raw_sha256")),
    )
    if result.get("ok") is not True:
        return result
    return {
        **result,
        "target_contract_id": _text(contract.get("target_contract_id")),
        "target_contract_sha256": _text(contract.get("target_contract_sha256")),
    }


def build_market_resolution_finality_attestation_from_finality_capture(
    *,
    authority_contract: Mapping[str, Any],
    target_contract: Mapping[str, Any],
    market_rule_raw_payload: bytes | str,
    finality_raw_capture: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a target contract to one immutable Gamma resolved-state receipt.

    ``resolved_at`` in the underlying attestation is the provider response's
    *observed fully-resolved state* time, never an inferred on-chain timestamp.
    """

    contract = _mapping(target_contract)
    capture = _mapping(finality_raw_capture)
    payload = capture.get("raw_payload")
    raw = payload if isinstance(payload, (bytes, bytearray, str)) else b""
    blockers: list[str] = []
    raw_ref = _text(capture.get("raw_ref"))
    raw_sha = raw_payload_sha256(raw) if raw else ""
    if capture.get("ok") is not True or not raw_ref:
        blockers.append("market_finality_capture_missing_or_unverified")
    if _text(capture.get("raw_sha256")).lower() != raw_sha:
        blockers.append("market_finality_capture_raw_sha256_binding_invalid")
    parsed = parse_market_resolution_finality(
        raw,
        public_slug=_text(contract.get("public_slug")),
        resolution_local_date=_text(contract.get("resolution_local_date")),
        response_completed_at=_text(capture.get("response_completed_at")),
    )
    if parsed.get("ok") is not True:
        blockers.extend(f"market_finality_capture_parser:{reason}" for reason in parsed.get("block_reasons") or ["blocked"])
    if parsed.get("ok") is True and _text(parsed.get("resolution_source")).rstrip("/") != _text(contract.get("resolution_source")).rstrip("/"):
        blockers.append("market_finality_capture_resolution_source_mismatch")
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(blockers)),
            "read_only": True,
            "writes_archive_db": False,
            "places_orders": False,
        }
    attestation = build_market_resolution_finality_attestation_from_target_contract(
        authority_contract=authority_contract,
        target_contract=contract,
        market_rule_raw_payload=market_rule_raw_payload,
        market_status="resolved",
        resolved_at=_text(parsed.get("resolution_state_observed_at")),
        attested_at=_text(parsed.get("resolution_state_observed_at")),
    )
    if attestation.get("ok") is not True:
        return attestation
    return {
        **attestation,
        "resolution_state_observed_at": _text(parsed.get("resolution_state_observed_at")),
        "market_finality_raw_ref": raw_ref,
        "market_finality_raw_sha256": raw_sha,
        "market_finality_parser_schema": FINALITY_PARSER_SCHEMA_VERSION,
        "boundary": {
            **_mapping(attestation.get("boundary")),
            "does_not_read_market_outcome": True,
            "does_not_read_market_price": True,
        },
    }


__all__ = [
    "SCHEMA_VERSION",
    "build_market_resolution_finality_attestation",
    "build_market_resolution_finality_attestation_from_target_contract",
    "build_market_resolution_finality_attestation_from_finality_capture",
]
