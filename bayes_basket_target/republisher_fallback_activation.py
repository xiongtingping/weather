"""Strict no-live activation/linkage for Open-Meteo fallback packets.

The retained ``republisher_no_live_fallback_binding_v1.json`` is an evidence
pointer, not a probability activation.  This adapter joins that pointer to a
same-station/same-day packet and an *independently recorded* cross-ensemble
activation.  It is intentionally stricter than the legacy bootstrap OOS lane:
all selected republisher systems must carry closed-AWC 20-train/8-holdout
calibration evidence, and the pointer-bound AIFS/ICON system must use the same
cohort dates.  No rows are written and no native/P1/formal authority is
created here.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .station_season import station_season_bin
from .republisher_fallback_binding import (
    DEFAULT_BINDING_PATH,
    REPUBLISHER_MODEL_SYSTEM_IDS,
    REPUBLISHER_SYSTEM_MODEL_IDS,
    RepublisherFallbackBindingSnapshot,
    load_republisher_fallback_binding,
    republisher_binding_rows_for_packet,
)


SCHEMA = "bayes_basket_target_republisher_no_live_fallback_activation_v1"
AUTHORITY_MODE = "PRODUCTION_NO_LIVE_REPUBLISHER_FALLBACK"
TRANSPORT_PROVIDER_ID = "OPEN_METEO"
SOURCE_ROLE = "aggregator_republisher"
TEMPORAL_SCOPE = "CLOSED_HISTORICAL_AWC_TRUTH"
MIN_TRAIN_DAYS = 20
MIN_HOLDOUT_DAYS = 8
AGGREGATOR_RUN_CYCLE = "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"


@dataclass(frozen=True)
class RepublisherFallbackActivation:
    """Validated pointer plus its explicit, non-native authority boundary."""

    binding: RepublisherFallbackBindingSnapshot

    @property
    def state(self) -> str:
        return "READY_NO_LIVE_FALLBACK_LINKAGE_PENDING_EXACT_CROSS_ACTIVATION"

    @property
    def content_sha256(self) -> str:
        return self.binding.content_sha256


def load_republisher_fallback_activation(
    path: str | None = None,
) -> RepublisherFallbackActivation:
    """Load the immutable pointer; DB activation is checked per packet."""

    return RepublisherFallbackActivation(
        binding=load_republisher_fallback_binding(path or DEFAULT_BINDING_PATH)
    )


def _season_bin(event_date: str) -> str:
    try:
        month = int(event_date[5:7])
    except (TypeError, ValueError, IndexError) as exc:
        raise ContractError("republisher_activation_event_date_invalid") from exc
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


def _timestamp(value: Any, *, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    return parsed.astimezone(timezone.utc)


def _lead_bucket(packet: Mapping[str, Any]) -> str:
    event_date = str(packet.get("event_date") or "")
    timezone_name = str(packet.get("settlement_timezone") or "")
    try:
        captured = _timestamp(
            packet.get("aggregator_snapshot_at_utc"),
            code="republisher_activation_capture_timestamp_invalid",
        ).astimezone(ZoneInfo(timezone_name))
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError(
            "republisher_activation_capture_timezone_invalid"
        ) from exc
    try:
        delta = datetime.fromisoformat(event_date).date() - captured.date()
    except ValueError as exc:
        raise ContractError("republisher_activation_event_date_invalid") from exc
    return f"d{delta.days}" if delta.days >= 0 else f"d_minus_{abs(delta.days)}"


def _json_mapping(value: Any, *, code: str) -> dict[str, str]:
    try:
        raw = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(code) from exc
    if not isinstance(raw, Mapping) or not raw:
        raise ContractError(code)
    normalized = {str(key): str(item) for key, item in raw.items()}
    if any(not key or not item for key, item in normalized.items()):
        raise ContractError(code)
    return normalized


def _strict_system_artifact(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    system_id: str,
    run_cycle: str,
    decision_at_utc: str,
    pointer_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Require one unambiguous 20+8 Open-Meteo system calibration row."""

    cycle_json = json.dumps(
        {system_id: run_cycle}, sort_keys=True, separators=(",", ":")
    )
    cycle_sha = content_sha256({system_id: run_cycle})
    rows = conn.execute(
        "SELECT a.activation_id,a.artifact_id,a.content_sha256,a.generation,"
        "r.train_start_date,r.train_end_date,r.holdout_start_date,"
        "r.holdout_end_date,r.reliability_parameters_json,r.content_sha256 "
        "FROM calibration_native_ensemble_activation a "
        "JOIN calibration_native_ensemble_system_artifact r "
        "ON r.system_artifact_id=a.artifact_id "
        "WHERE a.artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
        "AND a.station_id=? AND a.ensemble_system_id_or_star=? "
        "AND a.season_bin=? AND a.lead_bucket=? AND a.run_cycle=? "
        "AND a.available_system_set_hash_or_star='*' "
        "AND a.run_cycle_by_system_json=? "
        "AND a.run_cycle_by_system_sha256=? "
        "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "AND a.valid_from_utc<=? AND a.valid_until_utc>? "
        "AND r.valid_from_utc<=? AND r.valid_until_utc>? "
        "ORDER BY a.generation DESC,a.activation_id LIMIT 2",
        (
            station_id,
            system_id,
            season_bin,
            lead_bucket,
            run_cycle,
            cycle_json,
            cycle_sha,
            decision_at_utc,
            decision_at_utc,
            decision_at_utc,
            decision_at_utc,
        ),
    ).fetchall()
    if not rows or (len(rows) > 1 and int(rows[0][3]) == int(rows[1][3])):
        raise ContractError("republisher_activation_system_not_exact")
    row = rows[0]
    try:
        reliability = json.loads(str(row[8]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("republisher_activation_system_reliability_invalid") from exc
    if not isinstance(reliability, Mapping):
        raise ContractError("republisher_activation_system_reliability_invalid")
    try:
        future_event_count = int(reliability.get("future_event_count", 0) or 0)
        min_train_days = int(reliability.get("oos_min_train_days", 0) or 0)
        min_holdout_days = int(reliability.get("oos_min_holdout_days", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ContractError("republisher_activation_system_strict_oos_missing") from exc
    if (
        reliability.get("transport_provider_id") != TRANSPORT_PROVIDER_ID
        or reliability.get("holdout_source_class")
        != "HISTORICAL_ASOF_REPUBLISHER_RECEIPT"
        or reliability.get("oos_event_temporal_scope") != TEMPORAL_SCOPE
        or future_event_count != 0
        or min_train_days < MIN_TRAIN_DAYS
        or min_holdout_days < MIN_HOLDOUT_DAYS
        or reliability.get("formal_p0_eligible") is not False
    ):
        raise ContractError("republisher_activation_system_strict_oos_missing")
    if pointer_row is not None:
        for index, key in enumerate(
            ("train_start_date", "train_end_date", "holdout_start_date", "holdout_end_date")
        ):
            if str(row[4 + index]) != str(pointer_row.get(key) or ""):
                raise ContractError("republisher_activation_pointer_cohort_mismatch")
    return {
        "activation_id": str(row[0]),
        "artifact_id": str(row[1]),
        "activation_content_sha256": str(row[2]),
        "generation": int(row[3]),
        "artifact_content_sha256": str(row[9]),
        "reliability": dict(reliability),
    }


def preflight_republisher_fallback_activation(
    conn: sqlite3.Connection,
    *,
    activation: RepublisherFallbackActivation | Mapping[str, Any],
    packet: Mapping[str, Any],
    decision_at_utc: str,
    native_unavailable: bool,
) -> dict[str, Any]:
    """Return an immutable receipt or fail closed before P0 compilation."""

    if native_unavailable is not True:
        raise ContractError("republisher_activation_native_preferred")
    if not isinstance(activation, RepublisherFallbackActivation):
        raise ContractError("republisher_activation_snapshot_invalid")
    pointer_rows = republisher_binding_rows_for_packet(
        activation.binding,
        packet,
    )
    station_id = str(packet.get("station_id") or "").strip().upper()
    event_date = str(packet.get("event_date") or "").strip()
    if not station_id or not event_date:
        raise ContractError("republisher_activation_packet_scope_invalid")
    timezone_name = str(packet.get("settlement_timezone") or "")
    if not timezone_name:
        raise ContractError("republisher_activation_timezone_missing")
    # Ensure the market registry agrees with the packet scope before reading
    # any activation row.  This mirrors the P0 compiler's station-day guard.
    station_row = conn.execute(
        "SELECT settlement_definition FROM calibration_station_registry "
        "WHERE station_id=?",
        (station_id,),
    ).fetchone()
    if station_row is None:
        raise ContractError("republisher_activation_station_unregistered")
    market_rows = conn.execute(
        "SELECT DISTINCT settlement_timezone FROM basket_target_market_registry "
        "WHERE station_id=? AND event_date=? AND settlement_timezone<>''",
        (station_id, event_date),
    ).fetchall()
    if len(market_rows) != 1 or str(market_rows[0][0]) != timezone_name:
        raise ContractError("republisher_activation_market_timezone_mismatch")
    # Charter 62: the activation tables this lane queries are keyed by the
    # station-local vocabulary now; asking with the calendar trigram would
    # match nothing and fail this fallback closed for every packet.
    season_bin = station_season_bin(station_id, event_date)
    lead_bucket = _lead_bucket(packet)
    observed_systems = {
        str(system.get("ensemble_system_id") or "").strip()
        for system in packet.get("systems") or []
        if isinstance(system, Mapping)
    }
    if not observed_systems or "" in observed_systems:
        raise ContractError("republisher_activation_packet_systems_missing")
    activation_rows = conn.execute(
        "SELECT a.activation_id,a.artifact_id,a.content_sha256,a.generation,"
        "a.available_system_set_hash_or_star,a.run_cycle,"
        "a.run_cycle_by_system_json,a.run_cycle_by_system_sha256,"
        "r.ensemble_system_ids_json,r.ensemble_system_count,"
        "r.independent_family_count,r.settlement_definition,"
        "r.selection_evidence_sha256,r.content_sha256,"
        "r.run_cycle_by_system_json,r.run_cycle_by_system_sha256 "
        "FROM calibration_native_ensemble_activation a "
        "JOIN calibration_cross_native_ensemble_artifact r "
        "ON r.cross_ensemble_artifact_id=a.artifact_id "
        "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
        "AND a.station_id=? AND a.season_bin=? AND a.lead_bucket=? "
        "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "AND a.valid_from_utc<=? AND +a.valid_until_utc>? "
        "ORDER BY a.generation DESC,a.activation_id LIMIT 256",
        (station_id, season_bin, lead_bucket, decision_at_utc, decision_at_utc),
    )
    # Charter 186: the charter-185 generation index hands these over in the
    # ORDER BY (``+a.valid_until_utc`` leaves it the planner's choice), and
    # the walk reads them as they come and returns at the first that holds
    # -- the same read as ``_activated_lead_bucket`` (129 calls, 3-6s a
    # cycle read every valid activation and sorted it).  Same rows, same
    # order; the cursor is closed however the walk ends.
    try:
        for row in activation_rows:
            try:
                selected_ids_raw = json.loads(str(row[8]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ContractError(
                    "republisher_activation_selected_systems_invalid"
                ) from exc
            if not isinstance(selected_ids_raw, list):
                raise ContractError("republisher_activation_selected_systems_invalid")
            selected_ids = sorted(str(value) for value in selected_ids_raw)
            bound_system_ids = {
                REPUBLISHER_MODEL_SYSTEM_IDS[model_id]
                for model_id in pointer_rows
                if model_id in REPUBLISHER_MODEL_SYSTEM_IDS
            }
            if not bound_system_ids.issubset(set(selected_ids)):
                continue
            if not set(selected_ids).issubset(observed_systems):
                continue
            if str(row[5]) != AGGREGATOR_RUN_CYCLE:
                continue
            cycle_mapping = _json_mapping(
                row[14], code="republisher_activation_cycle_mapping_invalid"
            )
            if set(cycle_mapping) != set(selected_ids) or any(
                value != AGGREGATOR_RUN_CYCLE for value in cycle_mapping.values()
            ):
                continue
            if str(row[11]) != str(station_row[0]):
                continue
            selected_hash = content_sha256(selected_ids)
            if str(row[4]) != selected_hash:
                continue
            evaluation_rows = conn.execute(
                "SELECT evaluation_id,oos_state,selection_evidence_sha256,"
                "min_train_days,min_holdout_days,threshold_profile,evaluated_at_utc "
                "FROM calibration_cross_native_ensemble_oos_evaluation "
                "WHERE station_id=? AND season_bin=? AND lead_bucket=? "
                "AND run_cycle=? AND run_cycle_by_system_json=? "
                "AND run_cycle_by_system_sha256=? AND settlement_definition=? "
                "AND ensemble_system_ids_json=? AND available_system_set_hash=? "
                "AND evaluated_at_utc<=? ORDER BY evaluated_at_utc DESC,evaluation_id DESC LIMIT 2",
                (
                    station_id,
                    season_bin,
                    lead_bucket,
                    row[5],
                    row[14],
                    row[15],
                    row[11],
                    json.dumps(selected_ids, separators=(",", ":")),
                    row[4],
                    decision_at_utc,
                ),
            ).fetchall()
            if not evaluation_rows:
                continue
            evaluation = evaluation_rows[0]
            if (
                len(evaluation_rows) > 1
                and str(evaluation_rows[0][6]) == str(evaluation_rows[1][6])
            ):
                continue
            if (
                str(evaluation[1]) != "PRODUCTION_NO_LIVE_OOS_PASS"
                or str(evaluation[2]) != str(row[12])
                or int(evaluation[3]) < MIN_TRAIN_DAYS
                or int(evaluation[4]) < MIN_HOLDOUT_DAYS
                or "BOOTSTRAP" in str(evaluation[5] or "").upper()
            ):
                continue
            system_receipts: dict[str, Any] = {}
            try:
                for system_id in selected_ids:
                    system_receipts[system_id] = _strict_system_artifact(
                        conn,
                        station_id=station_id,
                        season_bin=season_bin,
                        lead_bucket=lead_bucket,
                        system_id=system_id,
                        run_cycle=AGGREGATOR_RUN_CYCLE,
                        decision_at_utc=decision_at_utc,
                        pointer_row=(
                            pointer_rows.get(REPUBLISHER_SYSTEM_MODEL_IDS.get(system_id, ""))
                        ),
                    )
            except ContractError:
                continue
            receipt_identity = {
                "schema": SCHEMA,
                "authority_mode": AUTHORITY_MODE,
                "transport_provider_id": TRANSPORT_PROVIDER_ID,
                "source_role": SOURCE_ROLE,
                "native_unavailable": True,
                "native_arrival_index_eligible": False,
                "native_formal_p0_eligible": False,
                "p1_eligible": False,
                "formal_live_authority": False,
                "station_id": station_id,
                "event_date": event_date,
                "season_bin": season_bin,
                "lead_bucket": lead_bucket,
                "pointer_content_sha256": activation.content_sha256,
                "pointer_binding_ids": sorted(
                    str(row.get("binding_id") or "") for row in pointer_rows.values()
                ),
                "cross_activation_id": str(row[0]),
                "cross_artifact_id": str(row[1]),
                "cross_activation_content_sha256": str(row[2]),
                "cross_artifact_content_sha256": str(row[13]),
                "selection_evidence_sha256": str(row[12]),
                "oos_evaluation_id": str(evaluation[0]),
                "selected_system_ids": selected_ids,
                "system_receipts": system_receipts,
            }
            return {
                **receipt_identity,
                "content_sha256": content_sha256(receipt_identity),
            }
    finally:
        activation_rows.close()
    raise ContractError("republisher_activation_exact_cross_activation_missing")


__all__ = [
    "AGGREGATOR_RUN_CYCLE",
    "AUTHORITY_MODE",
    "MIN_HOLDOUT_DAYS",
    "MIN_TRAIN_DAYS",
    "RepublisherFallbackActivation",
    "SCHEMA",
    "load_republisher_fallback_activation",
    "preflight_republisher_fallback_activation",
]
