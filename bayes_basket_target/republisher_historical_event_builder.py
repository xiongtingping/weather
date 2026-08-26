"""Canonical model-OOS events from fixed-lead Open-Meteo history.

This adapter accepts only retained Previous Runs evidence whose value at each
valid hour is explicitly the forecast made 24 or 48 hours earlier.  It does
not treat retrospective retrieval as native publication or arrival.  Its
only output is the existing canonical ``model_oos`` contract plus typed
terminals; cohort selection remains owned by ``model_oos_builder``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .canonical_contract import CanonicalTruthFact
from .model_oos_builder import build_model_oos_contract
from .model_oos_contract import (
    HISTORICAL_MODEL_RECEIPT_SCHEMA,
    historical_asof_basis_sha256,
)
from .model_product_identity import build_model_product_identity_receipt
from .native_daily_max_manifest import expected_et_hourly_slots
from .pr12_authority_scope_projection import validate_pr12_authority_scope_projection


SCHEMA = "bayes_basket_target_republisher_historical_event_bundle_v1"
TERMINAL_SCHEMA = "bayes_basket_target_republisher_historical_event_terminal_v1"
BUILDER_ID = "pr10.republisher_historical_fixed_lead.v1"
EVIDENCE_SCHEMA = "rkpk_open_meteo_five_model_historical_oos_v1"
SETTLEMENT_TIMEZONE = "America/New_York"
PRODUCT_BY_MODEL = {
    "CMA": "CMA",
    "ECMWF": "ECMWF",
    "GEM": "GEM",
    "GFS": "GFS",
    "ICON": "ICON",
}


def _seal(raw: Mapping[str, Any]) -> dict[str, Any]:
    identity = dict(raw)
    return {**identity, "content_sha256": content_sha256(identity)}


def _utc(value: Any, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"republisher_historical_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"republisher_historical_{label}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sha(value: Any, label: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"republisher_historical_{label}_invalid")
    return result


def _evidence(raw: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(raw)
    supplied = _sha(value.pop("content_sha256", ""), "evidence_content_sha256")
    if supplied != content_sha256(value):
        raise ContractError("republisher_historical_evidence_hash_mismatch")
    value["content_sha256"] = supplied
    lead_days = value.get("lead_days")
    if (
        value.get("schema") != EVIDENCE_SCHEMA
        or lead_days not in {1, 2}
        or value.get("source_contract", {}).get("endpoint")
        != "https://previous-runs-api.open-meteo.com/v1/forecast"
        or value.get("source_contract", {}).get("fixed_lead_semantics")
        != f"predicted_{24 * int(lead_days)}_hours_before_each_valid_time"
        or value.get("source_contract", {}).get(
            "native_publication_or_arrival_claimed"
        )
        is not False
    ):
        raise ContractError("republisher_historical_evidence_semantics_invalid")
    try:
        ZoneInfo(str(value.get("settlement_timezone") or ""))
    except Exception as exc:
        raise ContractError(
            "republisher_historical_source_timezone_invalid"
        ) from exc
    return value


def _coverage_index(
    rows: Sequence[Mapping[str, Any]], *, lead_bucket: str
) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("lead_bucket") != lead_bucket:
            continue
        key = (str(row.get("product_id") or ""), str(row.get("station_id") or "").upper())
        if key in result:
            raise ContractError("republisher_historical_coverage_scope_duplicate")
        result[key] = dict(row)
    return result


def _hourly_index(capture: Mapping[str, Any]) -> dict[str, float]:
    rows = capture.get("hourly_temperature_rows")
    if not isinstance(rows, list) or not rows:
        raise ContractError("republisher_historical_hourly_rows_missing")
    result: dict[str, float] = {}
    for raw in rows:
        if not isinstance(raw, Mapping) or set(raw) != {
            "valid_time_utc", "temperature_2m_c"
        }:
            raise ContractError("republisher_historical_hourly_row_invalid")
        stamp = _utc(raw["valid_time_utc"], "valid_time_utc")
        if stamp in result:
            raise ContractError("republisher_historical_hourly_slot_duplicate")
        try:
            temperature = float(raw["temperature_2m_c"])
        except (TypeError, ValueError) as exc:
            raise ContractError("republisher_historical_temperature_invalid") from exc
        if not -100.0 <= temperature <= 70.0:
            raise ContractError("republisher_historical_temperature_invalid")
        result[stamp] = temperature
    return result


def _terminal(
    *, station_id: str, event_date: str, model_id: str, lead_bucket: str,
    blocker: str, diagnostic: str, evidence_sha: str, as_of_utc: str,
) -> dict[str, Any]:
    return _seal({
        "schema": TERMINAL_SCHEMA,
        "builder_id": BUILDER_ID,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "model_id": model_id,
        "lead_bucket": lead_bucket,
        "blocker": blocker,
        "diagnostic": diagnostic,
        "source_evidence_content_sha256": evidence_sha,
        "as_of_utc": as_of_utc,
        "state": "BLOCKED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "consumable": False,
    })


def _station_settlement_timezone(conn: Any, station_id: str) -> str:
    """The zone this station's market listing settles on.

    Doctrine, 2026-08-26 canonical-day 口径换代: the listing decides, per
    station.  The binding table stands behind it for a station the current
    registry snapshot does not cover.
    """

    zone = ""
    for sql in (
        "SELECT settlement_timezone,COUNT(*) AS n FROM basket_target_market_registry "
        "WHERE station_id=? AND source_eligible=1 "
        "GROUP BY settlement_timezone ORDER BY n DESC LIMIT 1",
        "SELECT settlement_timezone FROM "
        "calibration_station_market_timezone_binding WHERE station_id=?",
    ):
        try:
            row = conn.execute(sql, (station_id,)).fetchone()
        except sqlite3.Error:
            # A store without the listing tables cannot say what day this
            # station settles on, and guessing is the thing that produced the
            # mislabelling.  Fall through to the fail-closed raise.
            continue
        if row and row[0]:
            zone = str(row[0])
            break
    if not zone:
        raise ContractError(
            "republisher_historical_settlement_timezone_missing:" + str(station_id)
        )
    return zone


def _effective_truth_index(
    conn: Any, *, station_id: str, as_of_utc: str,
) -> dict[str, dict[str, Any]]:
    """Truth this station actually settles on, by its own zone.

    This selected on ``settlement_timezone == "America/New_York"``, which for
    the 42 stations that do not settle on an Eastern day selected exactly the
    facts that were mislabelled to get past the old Eastern gate, and excluded
    the correctly labelled ones.  Measured 2026-08-26: 84 truth facts carry a
    zone that is not their station's, all ingested on 2026-08-05, and for
    RJTT and RKSI -- 13 hours from Eastern -- 12 of them carry a maximum taken
    over the wrong day, out by 1 to 4 degrees (RKSI 2026-07-17 holds 31.0C
    where its own day gives 27.0C).  Asking for the station's own zone leaves
    those facts unselected without touching the append-only store.
    """

    settlement_timezone = _station_settlement_timezone(conn, station_id)
    result: dict[str, dict[str, Any]] = {}
    for (raw_json,) in conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth' ORDER BY scope_key"
    ):
        try:
            raw = json.loads(str(raw_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(
                "republisher_historical_truth_edge_json_invalid"
            ) from exc
        truth = CanonicalTruthFact.from_mapping(raw)
        if (
            truth.station_id == station_id
            and truth.settlement_timezone == settlement_timezone
            and truth.source_lane == "AWC"
            and truth.closed_day
            and _utc(
                truth.canonical_committed_at_utc,
                "truth_canonical_committed_at_utc",
            ) <= as_of_utc
        ):
            if truth.event_date in result:
                raise ContractError("republisher_historical_truth_scope_duplicate")
            result[truth.event_date] = truth.to_mapping()
    return dict(sorted(result.items()))


def build_republisher_historical_event_bundle(
    conn: Any,
    *,
    evidences: Sequence[Mapping[str, Any]],
    evidence_paths: Sequence[Path | str],
    coverage_rows: Sequence[Mapping[str, Any]],
    authority_scope_projection: Mapping[str, Any],
    as_of_utc: str,
) -> dict[str, Any]:
    """Build exact event contracts and terminals without writing SSOT."""

    if len(evidences) != len(evidence_paths) or not evidences:
        raise ContractError("republisher_historical_evidence_path_count_mismatch")
    cutoff = _utc(as_of_utc, "as_of_utc")
    projection = validate_pr12_authority_scope_projection(
        authority_scope_projection
    )
    projection_stations = {str(row["station_id"]) for row in projection["rows"]}
    contracts: list[dict[str, Any]] = []
    terminals: list[dict[str, Any]] = []
    seen_evidence: set[str] = set()
    expected_scope_keys: set[tuple[str, str, str, str, str]] = set()
    for raw_evidence, supplied_path in zip(evidences, evidence_paths, strict=True):
        evidence = _evidence(raw_evidence)
        evidence_sha = evidence["content_sha256"]
        if evidence_sha in seen_evidence:
            raise ContractError("republisher_historical_evidence_duplicate")
        seen_evidence.add(evidence_sha)
        station = str(evidence.get("station_id") or "").upper()
        if station not in projection_stations:
            raise ContractError("republisher_historical_station_authority_missing")
        # This lane seals SETTLEMENT_TIMEZONE into the terminal records and
        # the model-input hash, and the contract that ingests them asserts
        # the Eastern value, so for a station that settles elsewhere it can
        # only produce records labelled with a day that station does not
        # have.  That is how 84 truth facts came to carry the wrong zone on
        # 2026-08-05.  The lane is retired -- neither entry script is
        # scheduled or referenced -- so it refuses those stations rather
        # than being half-migrated into a generation nothing will accept.
        if _station_settlement_timezone(conn, station) != SETTLEMENT_TIMEZONE:
            raise ContractError(
                "republisher_historical_non_eastern_station_retired_lane:" + station
            )
        lead_days = int(evidence["lead_days"])
        lead_bucket = f"d{lead_days}"
        run_cycle = f"PREVIOUS_DAY{lead_days}"
        fixed_hours = 24 * lead_days
        coverage = _coverage_index(coverage_rows, lead_bucket=lead_bucket)
        path = Path(supplied_path).expanduser().resolve(strict=True)
        try:
            persisted_evidence = _evidence(json.loads(path.read_text()))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ContractError(
                "republisher_historical_evidence_path_invalid"
            ) from exc
        if persisted_evidence["content_sha256"] != evidence_sha:
            raise ContractError("republisher_historical_evidence_path_mismatch")
        captures = evidence.get("captures")
        if not station or not isinstance(captures, list):
            raise ContractError("republisher_historical_evidence_scope_invalid")
        truth_by_date = _effective_truth_index(
            conn, station_id=station, as_of_utc=cutoff,
        )
        capture_by_model: dict[str, Mapping[str, Any]] = {}
        for capture in captures:
            if not isinstance(capture, Mapping):
                raise ContractError("republisher_historical_capture_mapping_required")
            model = str(capture.get("model_id") or "").upper()
            if model not in PRODUCT_BY_MODEL:
                raise ContractError("republisher_historical_model_unknown")
            if model in capture_by_model:
                raise ContractError("republisher_historical_capture_model_duplicate")
            capture_by_model[model] = capture
        for model, product_id in PRODUCT_BY_MODEL.items():
            capture = capture_by_model.get(model)
            for event_date in truth_by_date:
                key = (station, event_date, product_id, lead_bucket, run_cycle)
                if key in expected_scope_keys:
                    raise ContractError("republisher_historical_expected_scope_duplicate")
                expected_scope_keys.add(key)
            if capture is None:
                terminals.extend(
                    _terminal(
                        station_id=station, event_date=event_date, model_id=model,
                        lead_bucket=lead_bucket, blocker="DATA_MISSING",
                        diagnostic="FIXED_LEAD_CAPTURE_MISSING",
                        evidence_sha=evidence_sha, as_of_utc=cutoff,
                    )
                    for event_date in truth_by_date
                )
                continue
            expected_semantics = f"value_predicted_{fixed_hours}_hours_before_valid_time"
            if (
                capture.get("source_class") != "aggregator_republisher"
                or capture.get("previous_run_semantics") != expected_semantics
                or capture.get("formal_lineage_eligible") is not False
            ):
                raise ContractError("republisher_historical_capture_lineage_invalid")
            coverage_row = coverage.get((product_id, station))
            if coverage_row is None:
                terminals.extend(
                    _terminal(
                        station_id=station, event_date=event_date, model_id=model,
                        lead_bucket=lead_bucket, blocker="LINEAGE_MISMATCH",
                        diagnostic="MODEL_PRODUCT_IDENTITY_MISSING",
                        evidence_sha=evidence_sha, as_of_utc=cutoff,
                    )
                    for event_date in truth_by_date
                )
                continue
            identity, identity_gaps = build_model_product_identity_receipt(coverage_row)
            if identity is None or identity_gaps:
                raise ContractError("republisher_historical_product_identity_invalid")
            if (
                identity["model_id"] != model
                or identity["provider_id"] != capture.get("provider_id")
                or identity["run_cycle"] != run_cycle
            ):
                raise ContractError("republisher_historical_product_identity_mismatch")
            hourly = _hourly_index(capture)
            event_inputs: list[dict[str, Any]] = []
            for event_date, truth in truth_by_date.items():
                expected_slots = expected_et_hourly_slots(event_date)
                if any(slot not in hourly for slot in expected_slots):
                    terminals.append(_terminal(
                        station_id=station, event_date=event_date, model_id=model,
                        lead_bucket=lead_bucket, blocker="DATA_MISSING",
                        diagnostic="FIXED_LEAD_ET_HOURLY_SLOT_MISSING",
                        evidence_sha=evidence_sha, as_of_utc=cutoff,
                    ))
                    continue
                points = [
                    {"valid_time_utc": slot, "temperature_2m_c": hourly[slot]}
                    for slot in expected_slots
                ]
                valid_times = [
                    datetime.fromisoformat(slot.replace("Z", "+00:00"))
                    for slot in expected_slots
                ]
                decision = max(valid_times) - timedelta(hours=fixed_hours)
                decision_utc = _utc(decision.isoformat(), "fixed_lead_decision")
                vector_sha = content_sha256(expected_slots)
                model_input_sha = content_sha256({
                    "station_id": station,
                    "event_date": event_date,
                    "settlement_timezone": SETTLEMENT_TIMEZONE,
                    "model_id": model,
                    "provider_id": identity["provider_id"],
                    "provider_product_id": identity["provider_product_id"],
                    "lead_bucket": lead_bucket,
                    "run_cycle": run_cycle,
                    "fixed_lead_hours": fixed_hours,
                    "points": points,
                })
                receipt_identity = {
                    "schema": HISTORICAL_MODEL_RECEIPT_SCHEMA,
                    "station_id": station,
                    "model_id": model,
                    "provider_id": identity["provider_id"],
                    "provider_product_id": identity["provider_product_id"],
                    "source_class": "aggregator_republisher",
                    "event_date": event_date,
                    "run_init_utc": None,
                    "lead_bucket": lead_bucket,
                    "run_cycle": run_cycle,
                    "model_available_at_utc": decision_utc,
                    "forecast_decision_at_utc": decision_utc,
                    "retrieved_at_utc": _utc(evidence["fetched_at_utc"], "fetched_at_utc"),
                    "availability_clock_source": "REPUBLISHER_HISTORICAL_FIXED_LEAD",
                    "raw_object_sha256": _sha(
                        capture.get("response_sha256"), "response_sha256"
                    ),
                    "raw_object_ref": f"{path}#capture:{model}",
                    "model_input_content_sha256": model_input_sha,
                    "parent_evidence_sha256": evidence_sha,
                    "historical_asof_basis_sha256": "",
                    "historical_asof_method": "FIXED_VALID_TIME_OFFSET",
                    "fixed_lead_hours": fixed_hours,
                    "valid_time_vector_sha256": vector_sha,
                    "source_url": str(capture.get("source_url") or ""),
                    "native_arrival_indexed": False,
                    "p1_eligible": False,
                    "formal_live_authority": False,
                }
                receipt_identity["historical_asof_basis_sha256"] = (
                    historical_asof_basis_sha256(receipt_identity)
                )
                receipt = _seal(receipt_identity)
                event_inputs.append({
                    **{
                        field: identity[field]
                        for field in (
                            "station_id", "model_id", "ensemble_system_id",
                            "model_family", "provider_id", "provider_product_id",
                            "independence_family_id", "source_class",
                            "settlement_timezone", "lead_bucket",
                            "lead_bucket_scope", "run_cycle", "run_cycle_scope",
                            "event_oos_protocol_version",
                        )
                    },
                    "event_date": event_date,
                    "run_init_utc": None,
                    "forecast_value_c": max(hourly[slot] for slot in expected_slots),
                    "canonical_truth_fact_content_sha256": truth["content_sha256"],
                    "model_receipt": receipt,
                })
            if event_inputs:
                contracts.append(build_model_oos_contract(
                    conn,
                    as_of_utc=cutoff,
                    event_inputs=event_inputs,
                    cohort_spec=None,
                ))
    identity = {
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": cutoff,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "source_class": "aggregator_republisher",
        "state": "MATERIALIZED_UNPUBLISHED" if contracts else "BLOCKED",
        "release_state": "BLOCKED",
        "evidence_count": len(evidences),
        "expected_scope_count": len(expected_scope_keys),
        "contract_count": len(contracts),
        "event_count": sum(len(packet["event_facts"]) for packet in contracts),
        "cohort_count": 0,
        "terminal_count": len(terminals),
        "blocker_counts": {
            blocker: sum(row["blocker"] == blocker for row in terminals)
            for blocker in sorted({row["blocker"] for row in terminals})
        },
        "contracts": contracts,
        "terminals": terminals,
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "orders_submitted": 0,
        "consumable": False,
    }
    if identity["event_count"] + identity["terminal_count"] != identity[
        "expected_scope_count"
    ]:
        raise ContractError("republisher_historical_scope_coverage_mismatch")
    return _seal(identity)


__all__ = ["BUILDER_ID", "build_republisher_historical_event_bundle"]
