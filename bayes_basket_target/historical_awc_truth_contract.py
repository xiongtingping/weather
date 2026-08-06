"""Build canonical closed-day truth from independently reverified AWC raw.

This is the PR02.A bridge between ``historical_awc_replay`` evidence and the
single canonical ingress.  It has no legacy adapter: an observation packet is
not accepted here, and missing station-days remain explicit gaps.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time as wall_time, timedelta, timezone
import math
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .canonical_contract import (
    CONTRACT_VERSION,
    CanonicalContract,
    SCHEMA as CONTRACT_SCHEMA,
)
from .contracts import ContractError, content_sha256
from .historical_awc_replay import (
    AUTHORITY_SCOPE,
    SCHEMA as EVIDENCE_SCHEMA,
    SOURCE_ROLE,
)


BUILD_SCHEMA = "bayes_basket_target_historical_awc_truth_contract_build_v1"
BUILDER_ID = "truth.awc_historical_replay.initial_snapshot.v1"
TRUTH_RULE = "max_temperature_from_valid_metar_and_speci_with_hourly_coverage_v2"
INITIAL_REPLAY_REVISION = 0
_HEX = frozenset("0123456789abcdef")


def _sha(value: Any, code: str) -> str:
    if not isinstance(value, str):
        raise ContractError(code)
    result = value.strip().lower()
    if len(result) != 64 or any(character not in _HEX for character in result):
        raise ContractError(code)
    return result


def _utc_z(value: Any, code: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value.strip().endswith("Z"):
        raise ContractError(code)
    try:
        parsed = datetime.fromisoformat(value.strip()[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(code) from exc
    normalized = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    return normalized, parsed.astimezone(timezone.utc)


def _day_end(event_date: str, timezone_name: str) -> str:
    try:
        local_date = date.fromisoformat(event_date)
        zone = ZoneInfo(timezone_name)
    except (ValueError, KeyError) as exc:
        raise ContractError("historical_awc_truth_scope_invalid") from exc
    value = datetime.combine(
        local_date + timedelta(days=1), wall_time.min, tzinfo=zone
    ).astimezone(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_evidence(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("schema") != EVIDENCE_SCHEMA:
        raise ContractError("historical_awc_truth_evidence_schema_invalid")
    supplied = _sha(
        raw.get("evidence_sha256"),
        "historical_awc_truth_evidence_sha256_invalid",
    )
    identity = {key: value for key, value in raw.items() if key != "evidence_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError("historical_awc_truth_evidence_sha256_mismatch")
    if (
        raw.get("source_lane") != "AWC"
        or raw.get("source_role") != SOURCE_ROLE
        or raw.get("authority_scope") != AUTHORITY_SCOPE
        or raw.get("historical_only") is not True
        or raw.get("canonical_ingress") is not True
    ):
        raise ContractError("historical_awc_truth_evidence_lineage_invalid")
    checks = raw.get("checks")
    required_checks = {
        "official_awc_raw_reverified",
        "raw_text_sha256_reverified",
        "station_timezone_exact",
        "closed_days_only",
        "metar_any_valid_minute_allowed",
        "speci_allowed",
        "reference_only_excluded",
        "station_day_awc_report_hour_coverage_explicit",
    }
    if not isinstance(checks, Mapping) or any(checks.get(key) is not True for key in required_checks):
        raise ContractError("historical_awc_truth_evidence_checks_invalid")
    boundary = raw.get("boundary")
    if not isinstance(boundary, Mapping) or any(
        boundary.get(key) is not False
        for key in (
            "writes_production_db",
            "calls_order_gateway",
            "places_orders",
            "live_arrival_evidence",
            "enters_realtime_truth",
            "enters_p1",
            "enters_hard_no",
        )
    ):
        raise ContractError("historical_awc_truth_evidence_boundary_invalid")
    station = raw.get("station_id")
    timezone_name = raw.get("target_timezone")
    if not isinstance(station, str) or not station.strip() or station != station.upper():
        raise ContractError("historical_awc_truth_station_invalid")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ContractError("historical_awc_truth_timezone_invalid")
    try:
        ZoneInfo(timezone_name)
        first = date.fromisoformat(str(raw.get("start_date") or ""))
        last = date.fromisoformat(str(raw.get("end_date") or ""))
    except (ValueError, KeyError) as exc:
        raise ContractError("historical_awc_truth_scope_invalid") from exc
    if first > last:
        raise ContractError("historical_awc_truth_scope_invalid")
    as_of_text, as_of = _utc_z(raw.get("as_of_utc"), "historical_awc_truth_as_of_invalid")
    observations = raw.get("observations")
    observed = raw.get("observed_dates")
    ready = raw.get("truth_ready_dates")
    blocked = raw.get("truth_blocked_dates")
    completeness = raw.get("station_day_completeness")
    missing_diagnostics = raw.get("missing_date_diagnostics")
    if (
        not isinstance(observations, list)
        or any(not isinstance(item, Mapping) for item in observations)
        or not isinstance(observed, list)
        or not isinstance(ready, list)
        or not isinstance(blocked, list)
        or not isinstance(completeness, list)
        or any(not isinstance(item, Mapping) for item in completeness)
        or not isinstance(missing_diagnostics, list)
        or any(not isinstance(item, Mapping) for item in missing_diagnostics)
    ):
        raise ContractError("historical_awc_truth_evidence_rows_invalid")
    expected: list[str] = []
    cursor = first
    while cursor <= last:
        expected.append(cursor.isoformat())
        cursor += timedelta(days=1)
    if any(sorted(set(values)) != list(values) for values in (observed, ready, blocked)):
        raise ContractError("historical_awc_truth_date_index_invalid")
    if sorted(set(ready) | set(blocked)) != expected or set(ready) & set(blocked):
        raise ContractError("historical_awc_truth_date_partition_invalid")
    if not set(ready).issubset(set(observed)):
        raise ContractError("historical_awc_truth_ready_date_unobserved")
    if [str(item.get("event_date") or "") for item in completeness] != expected:
        raise ContractError("historical_awc_truth_completeness_index_invalid")
    for item in completeness:
        event_date = str(item.get("event_date") or "")
        if item.get("truth_ready") is not (event_date in set(ready)):
            raise ContractError("historical_awc_truth_completeness_state_mismatch")
        expected_slots = item.get("expected_awc_report_hour_slots")
        observed_slots = item.get("observed_awc_report_hour_slots")
        missing_slots = item.get("missing_awc_report_hour_slots")
        conflicts = item.get("quarantined_metar_revision_slots")
        if any(not isinstance(value, list) for value in (expected_slots, observed_slots, missing_slots, conflicts)):
            raise ContractError("historical_awc_truth_completeness_slots_invalid")
        if sorted(set(expected_slots) - set(observed_slots)) != missing_slots:
            raise ContractError("historical_awc_truth_completeness_slots_mismatch")
        proven_slots = sorted({
            _utc_z(
                observation.get("valid_at_utc"),
                "historical_awc_truth_observation_time_invalid",
            )[1]
            .replace(minute=0, second=0, microsecond=0)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
            for observation in observations
            if observation.get("report_type") in {"METAR", "SPECI"}
            and observation.get("event_date") == event_date
        })
        if proven_slots != observed_slots:
            raise ContractError(
                "historical_awc_truth_completeness_observation_slots_mismatch"
            )
        if event_date in set(ready) and (missing_slots or conflicts):
            raise ContractError("historical_awc_truth_ready_date_incomplete")
    diagnostic_dates = [str(item.get("event_date") or "") for item in missing_diagnostics]
    if diagnostic_dates != list(blocked):
        raise ContractError("historical_awc_truth_missing_diagnostics_mismatch")
    for item in missing_diagnostics:
        if item.get("blockers") != ["DATA_MISSING", "OOS_SAMPLE_SHORT"]:
            raise ContractError("historical_awc_truth_missing_blockers_invalid")
        diagnostics = item.get("diagnostics")
        if not isinstance(diagnostics, list) or not diagnostics:
            raise ContractError("historical_awc_truth_missing_diagnostics_invalid")
        for diagnostic in diagnostics:
            if (
                not isinstance(diagnostic, Mapping)
                or not isinstance(diagnostic.get("reason"), str)
                or not diagnostic.get("reason")
                or isinstance(diagnostic.get("candidate_count"), bool)
                or not isinstance(diagnostic.get("candidate_count"), int)
                or diagnostic.get("candidate_count") < 0
            ):
                raise ContractError("historical_awc_truth_missing_diagnostic_invalid")
    return {
        "station_id": station,
        "timezone": timezone_name,
        "start_date": first.isoformat(),
        "end_date": last.isoformat(),
        "as_of_utc": as_of_text,
        "as_of": as_of,
        "evidence_sha256": supplied,
        "observations": observations,
        "observed_dates": list(observed),
        "truth_ready_dates": list(ready),
        "truth_blocked_dates": list(blocked),
        "station_day_completeness": [dict(item) for item in completeness],
        "missing_date_diagnostics": [dict(item) for item in missing_diagnostics],
    }


def _truth_facts(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    validated = _validate_evidence(evidence)
    station = validated["station_id"]
    timezone_name = validated["timezone"]
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_ids: set[str] = set()
    for raw in validated["observations"]:
        required = {
            "observation_id",
            "station_id",
            "source_lane",
            "source_role",
            "authority_scope",
            "settlement_timezone",
            "event_date",
            "evidence_role",
            "reference_only",
            "qc_status",
            "report_type",
            "valid_at_utc",
            "reported_temperature_c",
            "raw_sha256",
            "raw_object_path",
            "official_source_url",
            "source_poll_id",
            "content_sha256",
        }
        if set(raw) != required:
            raise ContractError("historical_awc_truth_observation_fields_invalid")
        identity = {key: value for key, value in raw.items() if key != "content_sha256"}
        if _sha(raw.get("content_sha256"), "historical_awc_truth_observation_sha_invalid") != content_sha256(identity):
            raise ContractError("historical_awc_truth_observation_sha_mismatch")
        observation_id = str(raw.get("observation_id") or "")
        if not observation_id or observation_id in seen_ids:
            raise ContractError("historical_awc_truth_observation_identity_invalid")
        seen_ids.add(observation_id)
        if (
            raw.get("station_id") != station
            or raw.get("source_lane") != "AWC"
            or raw.get("source_role") != SOURCE_ROLE
            or raw.get("authority_scope") != AUTHORITY_SCOPE
            or raw.get("settlement_timezone") != timezone_name
            or raw.get("evidence_role") != "FORMAL"
            or raw.get("reference_only") is not False
            or raw.get("qc_status") != "ACCEPTED"
            or raw.get("report_type") not in {"METAR", "SPECI"}
        ):
            raise ContractError("historical_awc_truth_observation_lineage_invalid")
        event_date = str(raw.get("event_date") or "")
        if event_date not in validated["observed_dates"]:
            raise ContractError("historical_awc_truth_observation_date_invalid")
        valid_text, valid_at = _utc_z(
            raw.get("valid_at_utc"), "historical_awc_truth_observation_time_invalid"
        )
        if valid_text != raw.get("valid_at_utc") or valid_at.astimezone(ZoneInfo(timezone_name)).date().isoformat() != event_date:
            raise ContractError("historical_awc_truth_observation_date_mismatch")
        temperature = raw.get("reported_temperature_c")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ContractError("historical_awc_truth_temperature_invalid")
        if not math.isfinite(float(temperature)):
            raise ContractError("historical_awc_truth_temperature_invalid")
        by_date[event_date].append(dict(raw))
    if sorted(by_date) != validated["observed_dates"]:
        raise ContractError("historical_awc_truth_observed_dates_unproven")

    facts: list[dict[str, Any]] = []
    for event_date in validated["truth_ready_dates"]:
        observations = sorted(
            by_date[event_date],
            key=lambda item: (item["valid_at_utc"], item["report_type"], item["observation_id"]),
        )
        day_end = _day_end(event_date, timezone_name)
        _day_end_text, day_end_dt = _utc_z(day_end, "historical_awc_truth_day_end_invalid")
        if day_end_dt > validated["as_of"]:
            raise ContractError("historical_awc_truth_day_not_closed")
        truth_evidence_identity = {
            "schema": "bayes_basket_target_historical_awc_daily_truth_evidence_v1",
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
            "truth_rule": TRUTH_RULE,
            "source_role": SOURCE_ROLE,
            "authority_scope": AUTHORITY_SCOPE,
            "replay_evidence_sha256": validated["evidence_sha256"],
            "observation_bindings": [
                {
                    "observation_id": item["observation_id"],
                    "content_sha256": item["content_sha256"],
                    "raw_sha256": item["raw_sha256"],
                }
                for item in observations
            ],
        }
        truth_content = content_sha256(truth_evidence_identity)
        fact_identity = {
            "truth_id": "awc-historical-truth-" + truth_content[:32],
            "station_id": station,
            "event_date": event_date,
            "truth_revision_no": INITIAL_REPLAY_REVISION,
            "truth_value": float(max(item["reported_temperature_c"] for item in observations)),
            "truth_content_sha256": truth_content,
            "canonical_committed_at_utc": validated["as_of_utc"],
            "station_day_end_utc": day_end,
            "closed_day": True,
            "source_lane": "AWC",
            "source_role": SOURCE_ROLE,
            "authority_scope": AUTHORITY_SCOPE,
            "settlement_timezone": timezone_name,
        }
        facts.append({**fact_identity, "content_sha256": content_sha256(fact_identity)})
    return facts


def build_historical_awc_truth_contract(
    evidences: Sequence[Mapping[str, Any]],
    *,
    as_of_utc: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not evidences:
        raise ContractError("historical_awc_truth_evidences_missing")
    normalized_as_of, _ = _utc_z(as_of_utc, "historical_awc_truth_bundle_as_of_invalid")
    facts: list[dict[str, Any]] = []
    stations: list[dict[str, Any]] = []
    seen_stations: set[str] = set()
    for evidence in evidences:
        validated = _validate_evidence(evidence)
        station = validated["station_id"]
        if station in seen_stations:
            raise ContractError("historical_awc_truth_station_duplicate")
        seen_stations.add(station)
        if validated["as_of_utc"] != normalized_as_of:
            raise ContractError("historical_awc_truth_as_of_mismatch")
        station_facts = _truth_facts(evidence)
        facts.extend(station_facts)
        stations.append(
            {
                "station_id": station,
                "evidence_sha256": validated["evidence_sha256"],
                "truth_fact_count": len(station_facts),
                "observed_dates": validated["observed_dates"],
                "truth_ready_dates": validated["truth_ready_dates"],
                "truth_blocked_dates": validated["truth_blocked_dates"],
                "missing_date_diagnostics": validated["missing_date_diagnostics"],
                "state": "READY" if not validated["truth_blocked_dates"] else "BLOCKED",
                "blockers": [] if not validated["truth_blocked_dates"] else ["DATA_MISSING", "OOS_SAMPLE_SHORT"],
            }
        )
    if not facts:
        raise ContractError("historical_awc_truth_contract_empty")
    contract_identity = {
        "schema": CONTRACT_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": normalized_as_of,
        "builder_id": BUILDER_ID,
        "model_facts": [],
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": sorted(
            facts, key=lambda item: (item["station_id"], item["event_date"], item["truth_revision_no"])
        ),
        "residual_facts": [],
        "activation_facts": [],
    }
    contract = {
        **contract_identity,
        "content_sha256": content_sha256(contract_identity),
    }
    contract = CanonicalContract.from_mapping(contract).to_mapping()
    build_identity = {
        "schema": BUILD_SCHEMA,
        "as_of_utc": normalized_as_of,
        "builder_id": BUILDER_ID,
        "canonical_contract_sha256": contract["content_sha256"],
        "truth_revision_authority": "INITIAL_IMMUTABLE_REPLAY_SNAPSHOT",
        "truth_revision_no": INITIAL_REPLAY_REVISION,
        "station_count": len(stations),
        "truth_fact_count": len(facts),
        "complete_station_count": sum(item["state"] == "READY" for item in stations),
        "blocked_station_count": sum(item["state"] == "BLOCKED" for item in stations),
        "stations": sorted(stations, key=lambda item: item["station_id"]),
        "production_no_live": True,
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    receipt = {**build_identity, "content_sha256": content_sha256(build_identity)}
    return contract, receipt


__all__ = [
    "BUILD_SCHEMA",
    "BUILDER_ID",
    "INITIAL_REPLAY_REVISION",
    "TRUTH_RULE",
    "build_historical_awc_truth_contract",
]
