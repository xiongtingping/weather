"""Terminalize retained native-ensemble packets against canonical ET truth.

The retained PF50/GEFS/GEPS packets contain station-local member daily maxima.
Those maxima cannot be relabelled as America/New_York maxima unless the two
day windows are exactly identical.  Even an identical window remains strict
OOS-ineligible when native publication/received/arrival evidence is absent.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timezone
import math
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .model_product_identity import (
    PRODUCT_BY_ID,
    REGISTRY_CONTENT_SHA256,
    REGISTRY_VERSION,
)
from .truth_authority_supersession import (
    CanonicalTruthAuthoritySupersessionContract,
)
from .fleet import expected_station_count


SCHEMA = "bayes_basket_target_pr06_pr09_native_ensemble_et_gap_bundle_v1"
TERMINAL_SCHEMA = "bayes_basket_target_pr06_pr09_native_ensemble_et_terminal_v1"
BUILDER_ID = "pr06_pr09.native_ensemble.et_gap.v1"
SETTLEMENT_TIMEZONE = "America/New_York"
PACKET_SPECS = {
    "ecmwf_ifs_open_pf50_historical_multistation_members_v1": {
        "product_id": "ECMWF_IFS_ENS_OPEN_PF50",
        "ensemble_system_id": "ECMWF_IFS_ENS_OPEN_PF50",
        "member_count": 50,
        "run_cycle": "18Z",
    },
    "noaa_gefs_official_historical_multistation_members_v2": {
        "product_id": "NOAA_GEFS_05",
        "ensemble_system_id": "NOAA_GEFS_05",
        "member_count": 31,
        "run_cycle": "18Z",
    },
    "eccc_geps_official_historical_multistation_members_v1": {
        "product_id": "ECCC_GEM_GLOBAL_ENS",
        "ensemble_system_id": "ECCC_GEM_GLOBAL_ENS",
        "member_count": 21,
        "run_cycle": "00Z",
    },
}


def _sha(value: Any, label: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"native_ensemble_et_{label}_invalid")
    return result


def _verified_hash(raw: Mapping[str, Any], label: str) -> str:
    supplied = _sha(raw.get("content_sha256"), label)
    if supplied != content_sha256({
        key: value for key, value in raw.items() if key != "content_sha256"
    }):
        raise ContractError(f"native_ensemble_et_{label}_mismatch")
    return supplied


def _utc(value: Any, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"native_ensemble_et_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"native_ensemble_et_{label}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _seal(raw: Mapping[str, Any]) -> dict[str, Any]:
    identity = dict(raw)
    return {**identity, "content_sha256": content_sha256(identity)}


def _window(event_date: str, timezone_name: str) -> tuple[str, str]:
    target = date.fromisoformat(event_date)
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(target, time(), zone).astimezone(timezone.utc)
    end = datetime.combine(date.fromordinal(target.toordinal() + 1), time(), zone).astimezone(
        timezone.utc
    )
    return _utc(start.isoformat(), "window_start"), _utc(end.isoformat(), "window_end")


def _validate_capture(
    capture: Mapping[str, Any], *, packet: Mapping[str, Any], spec: Mapping[str, Any]
) -> None:
    _verified_hash(capture, "capture_content_sha256")
    if (
        capture.get("ensemble_system_id") != spec["ensemble_system_id"]
        or capture.get("event_date") != packet.get("event_date")
        or capture.get("run_cycle") != spec["run_cycle"]
        or capture.get("run_init_utc") != packet.get("run_init_utc")
        or capture.get("source_class") != "OFFICIAL_NATIVE_ARCHIVE"
        or capture.get("native_run_init_known") is not True
        or capture.get("native_publication_at_known") is not False
        or capture.get("offline_chronological_oos_eligible") is not True
        or capture.get("training_only") is not True
        or capture.get("holdout_eligible") is not False
        or capture.get("calls_order_gateway") is not False
        or capture.get("live_submit_authorized") is not False
        or int(capture.get("orders_submitted") or 0) != 0
    ):
        raise ContractError("native_ensemble_et_capture_semantics_invalid")
    members = capture.get("members")
    receipts = capture.get("field_receipts")
    if (
        not isinstance(members, list)
        or len(members) != spec["member_count"]
        or not isinstance(receipts, list)
        or not receipts
        or int(capture.get("field_receipt_count") or -1) != len(receipts)
        or _sha(capture.get("field_receipt_set_sha256"), "receipt_set_sha256")
        != content_sha256(receipts)
    ):
        raise ContractError("native_ensemble_et_capture_vector_invalid")
    member_identity = []
    for member in members:
        if not isinstance(member, Mapping) or isinstance(member.get("raw_tmax_c"), bool):
            raise ContractError("native_ensemble_et_member_invalid")
        try:
            value = float(member.get("raw_tmax_c"))
        except (TypeError, ValueError) as exc:
            raise ContractError("native_ensemble_et_member_invalid") from exc
        if not math.isfinite(value):
            raise ContractError("native_ensemble_et_member_invalid")
        member_identity.append({
            "member_id": member.get("member_id"),
            "member_role": member.get("member_role"),
            "raw_tmax_c": member.get("raw_tmax_c"),
        })
    if _sha(capture.get("member_set_sha256"), "member_set_sha256") != content_sha256(
        member_identity
    ):
        raise ContractError("native_ensemble_et_member_set_mismatch")


def build_native_ensemble_et_gap_bundle(
    *, packets: Sequence[Mapping[str, Any]],
    truth_supersession_contract: Mapping[str, Any], as_of_utc: str,
) -> dict[str, Any]:
    cutoff = _utc(as_of_utc, "as_of_utc")
    truth = CanonicalTruthAuthoritySupersessionContract.from_mapping(
        truth_supersession_contract
    )
    if truth.as_of_utc > cutoff:
        raise ContractError("native_ensemble_et_truth_after_asof")
    bounds = {
        (item.raw["station_id"], item.raw["event_date"]): item.raw
        for item in truth.awc_observation_contract.day_bounds_facts
    }
    expected_packet_keys = {
        (schema, event_date)
        for schema in PACKET_SPECS
        for event_date in ("2026-07-28", "2026-07-29", "2026-07-30")
    }
    packet_keys: set[tuple[str, str]] = set()
    terminals: list[dict[str, Any]] = []
    source_packet_shas: list[str] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ContractError("native_ensemble_et_packet_mapping_required")
        schema = str(packet.get("schema") or "")
        spec = PACKET_SPECS.get(schema)
        event_date = str(packet.get("event_date") or "")
        key = (schema, event_date)
        if spec is None or key not in expected_packet_keys or key in packet_keys:
            raise ContractError("native_ensemble_et_packet_scope_invalid")
        packet_keys.add(key)
        packet_sha = _verified_hash(packet, "packet_content_sha256")
        source_packet_shas.append(packet_sha)
        product = PRODUCT_BY_ID[spec["product_id"]]
        if (
            product["ensemble_system_id"] != spec["ensemble_system_id"]
            or product["model_family"] != "global_ensemble"
            or product["source_class"] != "native_official"
            or packet.get("ensemble_system_id") != spec["ensemble_system_id"]
            or packet.get("run_cycle") != spec["run_cycle"]
            or int(packet.get("station_count") or -1) != expected_station_count()
            or packet.get("training_only") is not True
            or packet.get("calls_order_gateway") is not False
            or packet.get("live_submit_authorized") is not False
            or int(packet.get("orders_submitted") or 0) != 0
        ):
            raise ContractError("native_ensemble_et_packet_semantics_invalid")
        captures = packet.get("captures")
        if not isinstance(captures, list) or len(captures) != expected_station_count():
            raise ContractError("native_ensemble_et_packet_exact45_required")
        seen: set[str] = set()
        for capture in captures:
            if not isinstance(capture, Mapping):
                raise ContractError("native_ensemble_et_capture_mapping_required")
            _validate_capture(capture, packet=packet, spec=spec)
            station = str(capture.get("station_id") or "").upper()
            if not station or station in seen:
                raise ContractError("native_ensemble_et_capture_station_duplicate")
            seen.add(station)
            bound = bounds.get((station, event_date))
            if bound is None:
                continue
            source_timezone = str(capture.get("settlement_timezone") or "")
            source_start, source_end = _window(event_date, source_timezone)
            receipts = capture["field_receipts"]
            raw_entity_pointer_present = any(
                isinstance(receipt, Mapping)
                and any(
                    receipt.get(field) not in (None, "")
                    for field in ("raw_object_ref", "raw_path", "local_raw_path")
                )
                for receipt in receipts
            )
            native_received_at_known = any(
                isinstance(receipt, Mapping)
                and receipt.get("received_at_utc") not in (None, "")
                for receipt in receipts
            )
            common = {
                "station_id": station,
                "event_date": event_date,
                "settlement_timezone": SETTLEMENT_TIMEZONE,
                "source_settlement_timezone": source_timezone,
                "source_day_start_utc": source_start,
                "source_day_end_utc": source_end,
                "canonical_day_start_utc": bound["day_start_utc"],
                "canonical_day_end_utc": bound["day_end_utc"],
                "product_id": spec["product_id"],
                "ensemble_system_id": spec["ensemble_system_id"],
                "model_family": product["model_family"],
                "provider_id": product["provider_id"],
                "provider_product_id": product["provider_product_id"],
                "independence_family_id": product["independence_family_id"],
                "source_class": product["source_class"],
                "run_init_utc": packet["run_init_utc"],
                "run_cycle": spec["run_cycle"],
                "lead_bucket": "d2",
                "member_count": spec["member_count"],
                "source_packet_content_sha256": packet_sha,
                "source_capture_content_sha256": capture["content_sha256"],
                "field_receipt_set_sha256": capture["field_receipt_set_sha256"],
                "member_set_sha256": capture["member_set_sha256"],
                "raw_entity_pointer_present": raw_entity_pointer_present,
                "native_publication_at_known": capture[
                    "native_publication_at_known"
                ],
                "native_received_at_known": native_received_at_known,
                "native_arrival_indexed": False,
                "truth_day_bounds_content_sha256": bound["content_sha256"],
                "truth_observation_revision_vector_sha256": bound[
                    "observation_revision_vector_sha256"
                ],
                "model_product_registry_version": REGISTRY_VERSION,
                "model_product_registry_content_sha256": REGISTRY_CONTENT_SHA256,
            }
            if source_start != bound["day_start_utc"] or source_end != bound["day_end_utc"]:
                blocker = "TIMEZONE_MISMATCH"
                diagnostic = "MEMBER_DAILY_MAX_WINDOW_NOT_CANONICAL_ET"
            else:
                blocker = "LINEAGE_MISMATCH"
                diagnostic = "NATIVE_PUBLICATION_RECEIVED_ARRIVAL_UNVERIFIED"
            terminals.append(_seal({
                "schema": TERMINAL_SCHEMA,
                "builder_id": BUILDER_ID,
                "as_of_utc": cutoff,
                "state": "BLOCKED",
                "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                "blocker": blocker,
                "diagnostic": diagnostic,
                **common,
                "writes_database": False,
                "publishes_pr04_pointer": False,
                "starts_services": False,
                "releases_live_pause": False,
                "orders_submitted": 0,
                "consumable": False,
            }))
    if packet_keys != expected_packet_keys:
        raise ContractError("native_ensemble_et_packet_matrix_incomplete")
    blockers = Counter(item["blocker"] for item in terminals)
    diagnostics = Counter(item["diagnostic"] for item in terminals)
    identity = {
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": cutoff,
        "state": "BLOCKED",
        "release_state": "BLOCKED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "source_packet_count": len(packets),
        "source_packet_vector_sha256": content_sha256(source_packet_shas),
        "expected_scope_count": len(terminals),
        "fact_count": 0,
        "terminal_count": len(terminals),
        "blocker_counts": dict(sorted(blockers.items())),
        "diagnostic_counts": dict(sorted(diagnostics.items())),
        "terminals": sorted(
            terminals,
            key=lambda item: (item["event_date"], item["ensemble_system_id"], item["station_id"]),
        ),
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "orders_submitted": 0,
        "production_no_live": True,
        "consumable": False,
    }
    return _seal(identity)


__all__ = ["BUILDER_ID", "build_native_ensemble_et_gap_bundle"]
