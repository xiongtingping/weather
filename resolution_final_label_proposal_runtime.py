"""Strict adapter from immutable final source observations to an X→S→B label.

This is intentionally a proposal builder.  It does not scrape a provider,
infer daily truth from intraday METAR, repair old scalar rows, or write any
database.  A separately configured final physical source and final resolution
source must first be captured as source observations with raw lineage.
"""

from __future__ import annotations

from typing import Any, Mapping

from resolution_bridge_runtime import build_final_resolution_label


SCHEMA_VERSION = "resolution_final_label_proposal_v1"
PHYSICAL_FINAL_SOURCE_ROLE = "physical_daily_high_final_truth"
SETTLEMENT_FINAL_SOURCE_ROLE = "resolution_settlement_final"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _value_from_payload(row: Mapping[str, Any], *keys: str) -> Any:
    payload = _mapping(row.get("payload"))
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _source_to_final_label_surface(
    observation: Mapping[str, Any],
    *,
    role: str,
    scalar_keys: tuple[str, ...],
) -> tuple[dict[str, Any], list[str]]:
    row = _mapping(observation)
    issues: list[str] = []
    if _text(row.get("source_class")) != "settlement_source":
        issues.append(f"{role}_source_class_not_settlement_source")
    expected_role = PHYSICAL_FINAL_SOURCE_ROLE if role == "physical_high" else SETTLEMENT_FINAL_SOURCE_ROLE
    if _text(row.get("role")) != expected_role:
        issues.append(f"{role}_source_role_invalid")
    scalar = _value_from_payload(row, *scalar_keys)
    if scalar is None:
        issues.append(f"{role}_final_scalar_missing")
    finality = _text(_value_from_payload(row, "label_finality", "finality"))
    if finality != "final":
        issues.append(f"{role}_payload_finality_not_final")
    observed_at = _text(row.get("fetched_at") or row.get("response_completed_at"))
    surface = {
        scalar_keys[0]: scalar,
        "source_id": _text(row.get("source_id")),
        "source_observation_ids": [_text(row.get("source_observation_id"))],
        "raw_sha256": _text(row.get("raw_sha256")),
        "raw_object_ref": _text(row.get("raw_ref")),
        "observed_at": observed_at,
        "finality": finality,
    }
    return surface, issues


def build_final_resolution_label_proposal(
    *,
    target_identity: str,
    event_date: str,
    physical_final_observation: Mapping[str, Any],
    settlement_final_observation: Mapping[str, Any],
    resolution_contract: Mapping[str, Any],
    label_available_at: str,
    asserted_market_bucket: str | None = None,
) -> dict[str, Any]:
    """Build a persistable label proposal from two final source observations.

    Ordinary intraday hard observations and legacy ``event_daily_temps`` rows
    are deliberately rejected: neither carries an explicit final-source role
    plus immutable raw capture provenance.
    """

    physical_observation = _mapping(physical_final_observation)
    settlement_observation = _mapping(settlement_final_observation)
    contract = _mapping(resolution_contract)
    physical_surface, issues = _source_to_final_label_surface(
        physical_observation,
        role="physical_high",
        scalar_keys=("final_physical_high_c", "physical_high_c"),
    )
    settlement_surface, settlement_issues = _source_to_final_label_surface(
        settlement_observation,
        role="settlement",
        scalar_keys=("settlement_scalar_c", "settlement_actual"),
    )
    issues.extend(settlement_issues)
    day = _text(event_date)[:10]
    for observation, role in ((physical_observation, "physical_high"), (settlement_observation, "settlement")):
        if _text(observation.get("event_date"))[:10] != day:
            issues.append(f"{role}_event_date_mismatch")
    expected_station = _text(contract.get("target_station_id"))
    if expected_station:
        for observation, role in ((physical_observation, "physical_high"), (settlement_observation, "settlement")):
            if _text(observation.get("station_id")).upper() != expected_station.upper():
                issues.append(f"{role}_station_id_mismatch")
    contract_target = _text(contract.get("target_identity"))
    if contract_target and contract_target != _text(target_identity):
        issues.append("resolution_contract_target_identity_mismatch")
    if issues:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(issues)),
            "read_only": True,
            "writes_archive_db": False,
            "places_orders": False,
        }
    label = build_final_resolution_label(
        target_identity=target_identity,
        event_date=event_date,
        physical_high_label=physical_surface,
        settlement_label=settlement_surface,
        resolution_contract=contract,
        label_available_at=label_available_at,
        asserted_market_bucket=asserted_market_bucket,
    )
    if label.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": [f"resolution_final_label_blocked:{reason}" for reason in label.get("block_reasons") or []],
            "resolution_final_label": label,
            "read_only": True,
            "writes_archive_db": False,
            "places_orders": False,
        }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "proposal_ready",
        "resolution_final_label": label,
        "target_identity": _text(target_identity),
        "event_date": day,
        "physical_source_observation_id": _text(physical_observation.get("source_observation_id")),
        "settlement_source_observation_id": _text(settlement_observation.get("source_observation_id")),
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


__all__ = [
    "PHYSICAL_FINAL_SOURCE_ROLE",
    "SCHEMA_VERSION",
    "SETTLEMENT_FINAL_SOURCE_ROLE",
    "build_final_resolution_label_proposal",
]
