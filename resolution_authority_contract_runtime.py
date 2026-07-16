"""Validation for the accepted ZUUU X/S source-authority contract.

This module fixes source semantics only.  It has no access to production facts,
posteriors, execution decisions, or orders.  A closed X->S->B bridge remains a
separate evidence and OOS gate.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from zuuu_metar_reporting_schedule import EXPECTED_REGULAR_SLOT_COUNT, LOCAL_TIMEZONE, SCHEDULE_ID, SCHEDULE_SCHEMA


SCHEMA_VERSION = "resolution_authority_contract_audit_v1"
PHYSICAL_ROLE = "physical_daily_high_final_truth"
SETTLEMENT_ROLE = "resolution_settlement_final"
ACCEPTED_AUTHORITY_STATE = "accepted_shadow_only"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_resolution_authority_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the one accepted ZUUU authority without granting live use."""

    record = dict(_mapping(contract))
    blockers: list[str] = []
    if _text(record.get("schema")) != "resolution_authority_contract_v1":
        blockers.append("resolution_authority_contract_schema_invalid")
    if _text(record.get("authority_state")) != ACCEPTED_AUTHORITY_STATE:
        blockers.append("resolution_authority_contract_must_be_accepted_shadow_only")
    target = _mapping(record.get("target"))
    if _text(target.get("station_id")).upper() != "ZUUU":
        blockers.append("resolution_authority_target_station_must_be_zuuu")
    if _text(target.get("temperature_unit")).upper() != "C":
        blockers.append("resolution_authority_temperature_unit_must_be_c")
    if _text(target.get("target_day_basis")) != "market_resolution_local_date":
        blockers.append("resolution_authority_target_day_basis_invalid")

    physical = _mapping(record.get("physical_x"))
    if _text(physical.get("role")) != PHYSICAL_ROLE:
        blockers.append("physical_x_role_invalid")
    if _text(record.get("contract_id")) != "zuuu_amsc_wunderground_daily_high_v4":
        blockers.append("resolution_authority_contract_id_invalid")
    if _text(record.get("supersedes_contract_id")) != "zuuu_amsc_wunderground_daily_high_v3":
        blockers.append("resolution_authority_superseded_contract_id_invalid")
    if _text(physical.get("source_id")) != "amsc_zuuu_scheduled_metar_daily_max_v2":
        blockers.append("physical_x_source_id_invalid")
    if _text(physical.get("source_family")).lower() != "amsc":
        blockers.append("physical_x_source_family_invalid")
    if _text(physical.get("raw_endpoint_id")) != "amsc_packet_retrieval_message_retrieval_sort":
        blockers.append("physical_x_raw_endpoint_invalid")
    if list(physical.get("report_types") or []) != ["METAR"]:
        blockers.append("physical_x_must_use_amsc_metar_only")
    if _text(physical.get("reporting_schedule_schema")) != SCHEDULE_SCHEMA:
        blockers.append("physical_x_reporting_schedule_schema_invalid")
    if _text(physical.get("reporting_schedule_id")) != SCHEDULE_ID:
        blockers.append("physical_x_reporting_schedule_id_invalid")
    if _text(physical.get("reporting_schedule_timezone")) != LOCAL_TIMEZONE:
        blockers.append("physical_x_reporting_schedule_timezone_invalid")
    if physical.get("expected_regular_metar_slot_count") != EXPECTED_REGULAR_SLOT_COUNT:
        blockers.append("physical_x_expected_regular_metar_slot_count_invalid")
    if _text(physical.get("aggregation")) != "maximum_reported_temperature_c":
        blockers.append("physical_x_aggregation_invalid")
    if physical.get("requires_immutable_raw_manifest_t1") is not True:
        blockers.append("physical_x_raw_manifest_t1_required")
    if physical.get("requires_target_day_closed") is not True:
        blockers.append("physical_x_target_day_closed_required")
    if physical.get("requires_complete_scheduled_coverage") is not True:
        blockers.append("physical_x_complete_scheduled_coverage_required")
    if _text(physical.get("finality_policy")) != "target_day_closed_complete_scheduled_coverage":
        blockers.append("physical_x_finality_policy_invalid")

    settlement = _mapping(record.get("settlement_s"))
    if _text(settlement.get("role")) != SETTLEMENT_ROLE:
        blockers.append("settlement_s_role_invalid")
    if _text(settlement.get("source_id")) != "wunderground_zuuu_historical_observation_daily_high_final_v3":
        blockers.append("settlement_s_source_id_invalid")
    if _text(settlement.get("source_family")).lower() != "wunderground":
        blockers.append("settlement_s_source_family_invalid")
    settlement_source_url_base = _text(settlement.get("source_url_base"))
    if settlement_source_url_base != "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU":
        blockers.append("settlement_s_source_url_invalid")
    if _text(settlement.get("capture_url_prefix")) != "https://api.weather.com/v1/location/ZUUU%3A9%3ACN/observations/historical.json":
        blockers.append("settlement_s_capture_url_prefix_invalid")
    if _text(settlement.get("capture_query_contract")) != "units=m&startDate={YYYYMMDD}&endDate={YYYYMMDD}":
        blockers.append("settlement_s_capture_query_contract_invalid")
    if _text(settlement.get("station_id")).upper() != "ZUUU":
        blockers.append("settlement_s_station_invalid")
    if _text(settlement.get("metric")) != "maximum_target_local_day_historical_observation_temperature_c":
        blockers.append("settlement_s_metric_invalid")
    if _text(settlement.get("parser_id")) != "wunderground_station_history_observation_parser_v2":
        blockers.append("settlement_s_parser_id_invalid")
    if settlement.get("requires_immutable_raw_capture") is not True:
        blockers.append("settlement_s_immutable_raw_capture_required")
    if settlement.get("requires_market_resolution_finality_attestation") is not True:
        blockers.append("settlement_s_finality_attestation_required")
    if _text(settlement.get("finality_policy")) != "market_resolution_attested_immutable_station_history_capture":
        blockers.append("settlement_s_finality_policy_invalid")
    if _text(settlement.get("revision_policy")) != "append_only_superseding_contract_revision":
        blockers.append("settlement_s_revision_policy_invalid")

    bucket = _mapping(record.get("market_bucket_b"))
    if _text(bucket.get("derivation")) != "frozen_market_bucket_contract_from_s":
        blockers.append("market_bucket_b_derivation_invalid")

    bridge = _mapping(record.get("x_to_s_to_b_bridge"))
    if _text(bridge.get("state")) != "data_accumulation":
        blockers.append("x_to_s_to_b_bridge_must_remain_data_accumulation")
    if bridge.get("closed") is not False:
        blockers.append("x_to_s_to_b_bridge_must_not_be_claimed_closed")

    ok = not blockers
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "state": ACCEPTED_AUTHORITY_STATE if ok else "blocked",
        "contract_id": _text(record.get("contract_id")),
        "contract_sha256": _canonical_sha256(record),
        "authority_fixed": ok,
        "final_label_auto_ready": False,
        "posterior_or_execution_authorized": False,
        "blockers": sorted(set(blockers)),
        "boundary": {
            "fixes_x_source": "AMSC ZUUU schedule-bound routine-METAR daily maximum",
            "fixes_s_source": "Wunderground ZUUU daily history final scalar",
            "b_only_from_frozen_market_contract": True,
            "does_not_close_x_s_b_bridge": True,
            "does_not_write_production": True,
            "does_not_place_orders": True,
        },
    }


def load_resolution_authority_contract(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved = Path(path)
    contract = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(contract, Mapping):
        raise ValueError("resolution_authority_contract_not_mapping")
    return dict(contract), validate_resolution_authority_contract(contract)


__all__ = [
    "ACCEPTED_AUTHORITY_STATE",
    "PHYSICAL_ROLE",
    "SCHEMA_VERSION",
    "SETTLEMENT_ROLE",
    "load_resolution_authority_contract",
    "validate_resolution_authority_contract",
]
