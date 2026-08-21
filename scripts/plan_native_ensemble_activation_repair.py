#!/usr/bin/env python3
"""Build a read-only repair plan for cross-ensemble authority.

The manifest is the immutable source for a completed strict *historical*
OOS calculation.  Despite the legacy ``forward`` filename/schema retained
for compatibility, prospective/forward observations are never OOS evidence.
This command compares that source with the append-only
evaluation, artifact and activation tables and emits a content-addressed
plan.  It deliberately does not ingest the manifest, create an artifact,
create an activation, or touch the target database.  A later operator can
use the emitted pointer with the narrow append-only persistence command only
after the plan has been reviewed.

Older generations are not repaired by mutating or deleting history.  When a
newer activation already carries the latest exact selection, an older
selection-mismatch row is reported as ``SUPERSEDED``.  Missing exact
evaluations and latest rejections remain explicit blockers.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS,
    FORWARD_ACCUMULATION_MIN_TRAIN_DAYS,
    P0_PROBABILITY_PATHWAY,
    _candidate_cycle_mapping,
    _selection_identity,
)
from bayes_basket_target.native_ensemble_contract import (  # noqa: E402
    native_ensemble_family_set_hash,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    assert_process_memory_budget,
    assert_target_db_capacity,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)


SCHEMA = "native_ensemble_activation_repair_plan_v1"
FORWARD_MANIFEST_SCHEMA = "native_ensemble_forward_oos_manifest_v1"
# New producers may use the explicit historical name.  Keep accepting the
# legacy schema because existing manifests are immutable and must remain
# auditable; both schemas are subject to the same closed-day checks below.
HISTORICAL_MANIFEST_SCHEMA = "native_ensemble_historical_oos_manifest_v1"
ACCEPTED_MANIFEST_SCHEMAS = {
    FORWARD_MANIFEST_SCHEMA,
    HISTORICAL_MANIFEST_SCHEMA,
}
NO_LIVE_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
PASS = "PRODUCTION_NO_LIVE_OOS_PASS"
REJECTED = "OOS_REJECTED"
MAX_STATIONS = 45
MAX_MANIFEST_BYTES = MAX_JSON_INPUT_BYTES
MAX_ACTIVATION_ROWS = 2_048
# Bounded-read guard, not a business rule.  The table is append-only and its
# growth is structural: scopes multiplied when leads went from d0/d1/d2 to
# hour buckets and again when activations became per-cycle-map, so 2,048 --
# sized for the 2026-08-03 table -- was overtaken within ten days (2,261 rows
# on 2026-08-13) and the planner began refusing to plan at all, which froze
# pointer rebinding and with it every fresh P0 compile.  The guard exists to
# keep one run's memory bounded; 16,384 rows is still a trivial read.
MAX_SYSTEM_ACTIVATION_ROWS = 16_384
MAX_CANDIDATE_ROWS = 512
DEFAULT_VALID_DAYS = 30
OFFICIAL_NATIVE_TRANSPORTS = {
    "ECMWF_OPEN_DATA_S3",
    "NOAA_GEFS_PDS_S3",
    "ECCC_DD_WEATHER",
    # DWD ICON Global/EPS is a distinct official-native family.  Its current
    # retained cohort still fails the independent 20-train/8-holdout gate,
    # but the repair planner must not classify a future fully evidenced DWD
    # strict-OOS candidate as an aggregator solely because its transport was
    # absent from this allowlist.
    "DWD",
}
FORMAL_OOS_PROFILE = "FORMAL_PROMOTION_20_TRAIN_8_HOLDOUT"
BOOTSTRAP_OOS_PROFILE = "PRODUCTION_NO_LIVE_BOOTSTRAP_2_TRAIN_1_HISTORICAL_HOLDOUT"
# The previous profile label is accepted only as an immutable legacy label;
# its data still has to pass the historical-source checks.
LEGACY_BOOTSTRAP_OOS_PROFILE = "PRODUCTION_NO_LIVE_BOOTSTRAP_2_TRAIN_1_FORWARD_HOLDOUT"
HISTORICAL_REPUBLISHER_RECEIPT = "HISTORICAL_ASOF_REPUBLISHER_RECEIPT"
HISTORICAL_OOS_TEMPORAL_SCOPE = "CLOSED_HISTORICAL_AWC_TRUTH"
VALID_NATIVE_RUN_CYCLES = {"00Z", "06Z", "12Z", "18Z"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STRICT_HOLDOUT_SOURCE_CLASSES = {
    "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE",
    HISTORICAL_REPUBLISHER_RECEIPT,
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc(value: str, *, name: str) -> str:
    text = str(value or "").strip()
    if not text.endswith("Z"):
        raise ContractError(f"{name}_must_be_utc_z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_missing")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _connection(database: Path) -> sqlite3.Connection:
    requested = database.expanduser()
    if requested.is_symlink():
        raise ContractError("native_repair_database_symlink_forbidden")
    resolved = requested.resolve(strict=True)
    assert_target_db_capacity(resolved, projected_bytes=0)
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    result = str(conn.execute("PRAGMA quick_check(1)").fetchone()[0]).lower()
    if result != "ok":
        conn.close()
        raise ContractError("native_repair_database_quick_check_failed")
    return conn


def _manifest(path: Path) -> Mapping[str, Any]:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError("native_repair_manifest_symlink_forbidden")
    resolved = requested.resolve(strict=True)
    payload = load_bounded_json(resolved, limit=MAX_MANIFEST_BYTES)
    if not isinstance(payload, Mapping):
        raise ContractError("native_repair_manifest_not_object")
    identity = {key: value for key, value in payload.items() if key != "content_sha256"}
    if (
        payload.get("schema") not in ACCEPTED_MANIFEST_SCHEMAS
        or str(payload.get("content_sha256") or "") != content_sha256(identity)
        or payload.get("production_no_live") is not True
        or payload.get("calls_order_gateway") is not False
        or payload.get("live_submit_authorized") is not False
        or int(payload.get("orders_submitted") or 0) != 0
    ):
        raise ContractError("native_repair_manifest_contract_invalid")
    reports = payload.get("reports")
    if not isinstance(reports, list) or len(reports) != MAX_STATIONS:
        raise ContractError("native_repair_manifest_station_count_invalid")
    expected = set(coordinate_registry_manifest()["station_ids"])
    seen: set[str] = set()
    for report in reports:
        if not isinstance(report, Mapping) or report.get("schema") != "native_ensemble_forward_oos_station_report_v1":
            # The forward builder currently uses the common native OOS schema
            # for each station report.  Keep validation compatible with that
            # schema while still requiring an immutable report hash.
            if not isinstance(report, Mapping) or not str(report.get("schema") or ""):
                raise ContractError("native_repair_manifest_report_invalid")
        station = str(report.get("station_id") or "").strip().upper()
        if not station or station in seen or station not in expected:
            raise ContractError("native_repair_manifest_station_scope_invalid")
        seen.add(station)
        report_identity = {key: value for key, value in report.items() if key != "content_sha256"}
        if str(report.get("content_sha256") or "") != content_sha256(report_identity):
            raise ContractError(f"native_repair_manifest_report_sha_mismatch:{station}")
        _report_historical_source_check(report, station=station)
    if seen != expected:
        raise ContractError("native_repair_manifest_station_set_incomplete")
    return payload


def _report_historical_source_check(
    report: Mapping[str, Any],
    *,
    station: str,
) -> None:
    """Reject a manifest that smuggles prospective rows into OOS.

    The report/schema names from the first implementation contain
    ``forward`` for compatibility.  The temporal contract is independent of
    that name: every OOS candidate must explicitly state closed-day AWC
    truth, zero future events, and historical holdout lineage.  Open-Meteo is
    accepted only as a labelled historical republisher fallback and never as
    native authority.
    """

    candidates = report.get("cross_candidates") or []
    if not isinstance(candidates, list):
        raise ContractError(f"native_repair_report_candidates_invalid:{station}")
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ContractError(f"native_repair_candidate_invalid:{station}")
        try:
            future_count = int(candidate.get("future_event_count", 0))
        except (TypeError, ValueError):
            future_count = -1
        if future_count != 0:
            raise ContractError(f"native_repair_future_oos_event:{station}")
        transport_map = candidate.get("system_transport_provider_ids")
        evaluations = candidate.get("system_evaluations")
        if not isinstance(transport_map, Mapping) or not isinstance(evaluations, Mapping):
            raise ContractError(f"native_repair_historical_lineage_missing:{station}")
        # Older immutable native manifests predate the explicit temporal
        # field.  Their offline chronological archive class is itself a
        # historical assertion, so accept that narrow legacy shape.  Any
        # republisher candidate must carry the explicit contract scope.
        temporal_scope = str(candidate.get("oos_event_temporal_scope") or "")
        if not temporal_scope:
            legacy_native_only = all(
                str((raw_eval or {}).get("transport_provider_id") or transport_map.get(system_id) or "")
                != "OPEN_METEO"
                and str((raw_eval or {}).get("holdout_source_class") or "")
                == "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE"
                for system_id, raw_eval in evaluations.items()
                if isinstance(raw_eval, Mapping)
            )
            if not legacy_native_only:
                raise ContractError(f"native_repair_future_oos_scope:{station}")
        elif temporal_scope != HISTORICAL_OOS_TEMPORAL_SCOPE:
            raise ContractError(f"native_repair_future_oos_scope:{station}")
        source_roles = candidate.get("source_role_by_system")
        if source_roles is not None and not isinstance(source_roles, Mapping):
            raise ContractError(f"native_repair_source_role_map_invalid:{station}")
        for system_id, raw_eval in evaluations.items():
            if not isinstance(raw_eval, Mapping):
                raise ContractError(f"native_repair_system_evaluation_invalid:{station}")
            holdout_source = str(raw_eval.get("holdout_source_class") or "")
            if holdout_source not in _STRICT_HOLDOUT_SOURCE_CLASSES:
                raise ContractError(f"native_repair_forward_holdout_forbidden:{station}")
            provider = str(transport_map.get(system_id) or raw_eval.get("transport_provider_id") or "")
            if provider == "OPEN_METEO":
                if holdout_source != HISTORICAL_REPUBLISHER_RECEIPT:
                    raise ContractError(f"native_repair_republisher_holdout_lineage_invalid:{station}")
                if source_roles is not None and str(source_roles.get(system_id) or "") != "aggregator_republisher":
                    raise ContractError(f"native_repair_republisher_role_invalid:{station}")
                for prediction in raw_eval.get("predictions") or []:
                    if not isinstance(prediction, Mapping) or not prediction.get("historical_oos_contract_ids"):
                        raise ContractError(f"native_repair_republisher_contract_missing:{station}")
            elif holdout_source != "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE":
                raise ContractError(f"native_repair_native_holdout_lineage_invalid:{station}")


def _selected_ids(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    raw = candidate.get("selected_system_ids")
    if not isinstance(raw, list) or len(raw) < 2:
        raise ContractError("native_repair_candidate_system_mask_invalid")
    values = tuple(str(item) for item in raw)
    if tuple(sorted(values)) != values or len(set(values)) != len(values):
        raise ContractError("native_repair_candidate_system_mask_not_canonical")
    if str(candidate.get("available_system_set_hash") or "") != content_sha256(list(values)):
        raise ContractError("native_repair_candidate_system_mask_sha_mismatch")
    return values


def _selected_system_ids_filter(
    raw: Sequence[object] | str | None,
) -> tuple[str, ...] | None:
    """Normalize an optional exact system-mask filter.

    The repair planner normally evaluates every candidate in the immutable
    manifest.  A derived plan may instead select one exact mask (for example
    the retained ECMWF+NOAA GEFS pair) so that a duplicate ECCC-containing
    cohort cannot become a second pointer set.  Accept both a comma-separated
    CLI value and a sequence for callers of :func:`build_plan`, but always
    store/compare the canonical sorted tuple used by the OOS contract.
    """

    if raw is None:
        return None
    tokens: list[str] = []
    values: Sequence[object]
    if isinstance(raw, str):
        values = (raw,)
    else:
        values = raw
    for value in values:
        # Supporting comma-separated values here keeps the public CLI
        # backwards-friendly while also accepting ``nargs='+'`` input.
        tokens.extend(str(value).split(","))
    normalized = tuple(item.strip() for item in tokens if item.strip())
    if len(normalized) < 2:
        raise ContractError("native_repair_selected_system_mask_invalid")
    if tuple(sorted(normalized)) != normalized or len(set(normalized)) != len(normalized):
        raise ContractError("native_repair_selected_system_mask_not_canonical")
    return normalized


def _candidate_lineage(candidate: Mapping[str, Any], selected: tuple[str, ...]) -> dict[str, Any]:
    providers = candidate.get("system_transport_provider_ids")
    if not isinstance(providers, Mapping):
        return {"state": "BLOCKED_NON_NATIVE_LINEAGE", "reason": "transport_provider_map_missing"}
    missing = sorted(set(selected) - {str(key) for key in providers})
    if missing:
        return {"state": "BLOCKED_NON_NATIVE_LINEAGE", "reason": "transport_provider_map_missing:" + ",".join(missing)}
    values = {system: str(providers[system]) for system in selected}
    non_native = sorted(
        system for system, provider in values.items()
        if provider not in OFFICIAL_NATIVE_TRANSPORTS and provider != "OPEN_METEO"
    )
    if non_native:
        return {
            "state": "BLOCKED_NON_NATIVE_LINEAGE",
            "reason": "non_official_transport:" + ",".join(non_native),
            "providers": values,
        }
    republisher_systems = sorted(
        system for system, provider in values.items() if provider == "OPEN_METEO"
    )
    if not republisher_systems:
        return {"state": "OFFICIAL_NATIVE_LINEAGE", "providers": values}
    # Open-Meteo is permitted only as an explicit closed-day historical
    # fallback.  It must never be relabelled native or formal authority.
    if str(candidate.get("oos_event_temporal_scope") or "") not in {
        "",
        HISTORICAL_OOS_TEMPORAL_SCOPE,
    }:
        return {
            "state": "BLOCKED_NON_NATIVE_LINEAGE",
            "reason": "republisher_temporal_scope_invalid",
            "providers": values,
        }
    if int(candidate.get("future_event_count", 0) or 0) != 0:
        return {
            "state": "BLOCKED_NON_NATIVE_LINEAGE",
            "reason": "republisher_future_event_count_nonzero",
            "providers": values,
        }
    source_roles = candidate.get("source_role_by_system")
    if not isinstance(source_roles, Mapping) or any(
        str(source_roles.get(system) or "") != "aggregator_republisher"
        for system in republisher_systems
    ):
        return {
            "state": "BLOCKED_NON_NATIVE_LINEAGE",
            "reason": "republisher_source_role_missing_or_invalid",
            "providers": values,
        }
    return {
        "state": "HISTORICAL_REPUBLISHER_LINEAGE",
        "providers": values,
        "republisher_system_ids": republisher_systems,
        "formal_p0_eligible": False,
        "native_arrival_index_eligible": False,
    }


def _independent_family_audit(
    conn: sqlite3.Connection,
    candidate: Mapping[str, Any],
    selected: tuple[str, ...],
) -> dict[str, Any]:
    """Verify the candidate family claim against the immutable registry.

    The forward manifest contains the family mapping used to build the
    candidate, but a repair pointer must not rely on that claim alone.  The
    runtime compiler independently resolves ``calibration_native_ensemble_system``
    before it can materialize P0.  This read-only plan mirrors that check when
    the foundation table is available and records an explicit unverifiable
    state for small fixture databases that do not install the foundation
    schema.

    This function deliberately does not infer a family from a provider name:
    two systems from one provider can still be one statistical family.  Only
    the registered ``independence_family_id`` is authoritative.
    """

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='calibration_native_ensemble_system'"
    ).fetchone()
    if table is None:
        return {
            "state": "UNVERIFIED_SCHEMA_MISSING",
            "reason": "calibration_native_ensemble_system_missing",
            "selected_system_ids": list(selected),
            "registered_system_ids": [],
            "family_by_system": {},
            "independence_family_ids": [],
            "independent_family_count": 0,
        }
    placeholders = ",".join("?" for _ in selected)
    rows = conn.execute(
        "SELECT ensemble_system_id,independence_family_id "
        "FROM calibration_native_ensemble_system "
        f"WHERE ensemble_system_id IN ({placeholders})",
        tuple(selected),
    ).fetchall()
    family_by_system = {
        str(row[0]): str(row[1])
        for row in rows
        if str(row[0]).strip() and str(row[1]).strip()
    }
    missing = sorted(set(selected) - set(family_by_system))
    family_ids = sorted(set(family_by_system.values()))
    declared_mapping = candidate.get("system_family_ids")
    declared_mismatch: list[str] = []
    if isinstance(declared_mapping, Mapping):
        for system_id in selected:
            if str(declared_mapping.get(system_id) or "") != str(
                family_by_system.get(system_id) or ""
            ):
                declared_mismatch.append(system_id)
    declared_families = candidate.get("independence_family_ids")
    declared_family_mismatch = False
    if isinstance(declared_families, list):
        declared_family_mismatch = sorted({str(item) for item in declared_families}) != family_ids
    declared_family_hash = str(candidate.get("available_family_set_hash") or "")
    expected_family_hash = (
        native_ensemble_family_set_hash(family_ids)
        if family_ids
        else None
    )
    family_hash_mismatch = bool(
        declared_family_hash
        and expected_family_hash is not None
        and declared_family_hash != expected_family_hash
    )
    declared_count = int(candidate.get("independent_family_count") or 0)
    if missing:
        state = "BLOCKED_MISSING_REGISTRY_SYSTEM"
        reason = "missing_registered_system:" + ",".join(missing)
    elif len(family_ids) < 2:
        state = "BLOCKED_SINGLE_INDEPENDENT_FAMILY"
        reason = "registered_independent_family_count_below_2"
    elif declared_mismatch:
        state = "BLOCKED_CANDIDATE_FAMILY_MAPPING_MISMATCH"
        reason = "candidate_system_family_ids_differs_from_registry"
    elif declared_family_mismatch:
        state = "BLOCKED_CANDIDATE_FAMILY_SET_MISMATCH"
        reason = "candidate_independence_family_ids_differs_from_registry"
    elif family_hash_mismatch:
        state = "BLOCKED_CANDIDATE_FAMILY_HASH_MISMATCH"
        reason = "candidate_available_family_set_hash_differs_from_registry"
    elif declared_count and declared_count != len(family_ids):
        state = "BLOCKED_CANDIDATE_FAMILY_COUNT_MISMATCH"
        reason = "candidate_independent_family_count_differs_from_registry"
    else:
        state = "VERIFIED_INDEPENDENT_FAMILY_REGISTRY"
        reason = None
    return {
        "state": state,
        "reason": reason,
        "selected_system_ids": list(selected),
        "registered_system_ids": sorted(family_by_system),
        "family_by_system": dict(sorted(family_by_system.items())),
        "independence_family_ids": family_ids,
        "independent_family_count": len(family_ids),
        "declared_independent_family_count": declared_count,
        "declared_mapping_checked": isinstance(declared_mapping, Mapping),
        "declared_mapping_mismatch_system_ids": declared_mismatch,
        "declared_family_set_checked": isinstance(declared_families, list),
        "declared_family_set_mismatch": declared_family_mismatch,
        "declared_family_set_hash": declared_family_hash or None,
        "family_set_hash_mismatch": family_hash_mismatch,
    }


def _candidate_sha(candidate: Mapping[str, Any]) -> str:
    identity = {key: value for key, value in candidate.items() if key != "content_sha256"}
    return content_sha256(identity)


def _candidate_scope(candidate: Mapping[str, Any]) -> dict[str, Any]:
    selected = _selected_ids(candidate)
    mapping, mapping_sha, run_cycle, run_cycle_scope = _candidate_cycle_mapping(candidate)
    scope = {
        "station_id": str(candidate["station_id"]),
        "season_bin": str(candidate["season_bin"]),
        "lead_bucket": str(candidate["lead_bucket"]),
        "run_cycle": str(run_cycle),
        "run_cycle_by_system_json": _json(mapping),
        "run_cycle_by_system_sha256": str(mapping_sha),
        "settlement_definition": str(candidate["settlement_definition"]),
        "ensemble_system_ids_json": _json(list(selected)),
        "available_system_set_hash": str(candidate["available_system_set_hash"]),
        "run_cycle_scope": str(run_cycle_scope),
    }
    return scope


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _nonnegative_int(value: object) -> int:
    """Parse JSON integer evidence without allowing a malformed receipt to abort audit."""

    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0


def _valid_local_date(value: object) -> bool:
    try:
        date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _valid_pre_event_utc(
    value: object,
    *,
    event_date: str,
    settlement_timezone: str | None = None,
    allow_historical_republisher: bool = False,
) -> bool:
    """Require arrival/decision before the station-local event day.

    The prior implementation compared against UTC midnight, which wrongly
    rejected a valid pre-midnight ET arrival (and made the audit disagree
    with the station-day contract).  The fallback remains UTC when a legacy
    manifest has no settlement timezone.
    """

    try:
        normalized = _utc(str(value), name="strict_oos_decision_at_utc")
        decision = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        zone = timezone.utc
        if settlement_timezone:
            zone = ZoneInfo(str(settlement_timezone))
        boundary = datetime.combine(
            date.fromisoformat(event_date),
            time.min,
            tzinfo=zone,
        ).astimezone(timezone.utc)
    except (ContractError, TypeError, ValueError, KeyError):
        return False
    if allow_historical_republisher:
        # Open-Meteo retrospective packets are downloaded after the event by
        # design.  Their only OOS authority is the explicit immutable
        # CLOSED_HISTORICAL_AWC_TRUTH contract checked by the caller.  Do not
        # reinterpret this branch as native publication/arrival evidence.
        return True
    return decision < boundary


def _historical_republisher_timing_allowed(
    candidate: Mapping[str, Any],
    *,
    system_id: str,
    event_date: str,
) -> bool:
    """Return whether one late holdout is bound by the republisher contract.

    A late timestamp is not sufficient on its own.  This narrow helper keeps
    the timing exception tied to the same immutable lineage fields that make
    an Open-Meteo row historical OOS evidence: the closed-day temporal scope,
    zero future events, the explicit republisher source role/class, and a
    non-empty contract pointer for the exact holdout date.
    """

    if (
        str(candidate.get("oos_event_temporal_scope") or "")
        != HISTORICAL_OOS_TEMPORAL_SCOPE
        or _nonnegative_int(candidate.get("future_event_count")) != 0
    ):
        return False
    transports = candidate.get("system_transport_provider_ids")
    evaluations = candidate.get("system_evaluations")
    source_roles = candidate.get("source_role_by_system")
    if (
        not isinstance(transports, Mapping)
        or not isinstance(evaluations, Mapping)
        or not isinstance(source_roles, Mapping)
        or str(transports.get(system_id) or "") != "OPEN_METEO"
        or str(source_roles.get(system_id) or "") != "aggregator_republisher"
    ):
        return False
    evaluation = evaluations.get(system_id)
    if not isinstance(evaluation, Mapping):
        return False
    if str(evaluation.get("holdout_source_class") or "") != HISTORICAL_REPUBLISHER_RECEIPT:
        return False
    predictions = evaluation.get("predictions")
    if not isinstance(predictions, list):
        return False
    matching = [
        prediction
        for prediction in predictions
        if isinstance(prediction, Mapping)
        and str(prediction.get("event_date") or "") == event_date
    ]
    if len(matching) != 1:
        return False
    contract_ids = matching[0].get("historical_oos_contract_ids")
    return (
        isinstance(contract_ids, list)
        and bool(contract_ids)
        and all(str(contract_id).strip() for contract_id in contract_ids)
    )


def _historical_republisher_cross_timing_allowed(
    candidate: Mapping[str, Any],
    selected: tuple[str, ...],
    *,
    event_date: str,
    cross_decision_at_utc: object,
) -> bool:
    """Allow a late cross timestamp only when every late system is bound.

    Native systems in a mixed cohort retain their pre-event requirement.  The
    exception is therefore evaluated per system rather than by looking only
    at whether the candidate contains *some* Open-Meteo transport.
    """

    evaluations = candidate.get("system_evaluations")
    if not isinstance(evaluations, Mapping):
        return False
    settlement_timezone = str(candidate.get("settlement_timezone") or "") or None
    late_system_seen = False
    for system_id in selected:
        evaluation = evaluations.get(system_id)
        if not isinstance(evaluation, Mapping):
            return False
        predictions = evaluation.get("predictions")
        if not isinstance(predictions, list):
            return False
        matching = [
            prediction
            for prediction in predictions
            if isinstance(prediction, Mapping)
            and str(prediction.get("event_date") or "") == event_date
        ]
        if len(matching) != 1:
            return False
        decision = matching[0].get("decision_at_utc")
        if _valid_pre_event_utc(
            decision,
            event_date=event_date,
            settlement_timezone=settlement_timezone,
        ):
            continue
        late_system_seen = True
        if not _historical_republisher_timing_allowed(
            candidate,
            system_id=system_id,
            event_date=event_date,
        ):
            return False
    if not late_system_seen:
        return False
    # A cross prediction is derived from the system decisions.  Keep the
    # existing strict check for malformed/absent cross timestamps; this call
    # only supplies the explicit historical exception for a late one.
    return not _valid_pre_event_utc(
        cross_decision_at_utc,
        event_date=event_date,
        settlement_timezone=settlement_timezone,
    )


def _strict_oos_sample_evidence(
    candidate: Mapping[str, Any],
    selected: tuple[str, ...],
) -> dict[str, Any]:
    """Validate the manifest's irreducible 20-train/8-holdout evidence.

    The append-only evaluation table stores only the selection digest and
    aggregate thresholds.  A repair pointer must also prove that the signed
    manifest contains the actual per-system 20/8 chronological cohort and
    the member-run hashes bound into each cross-system holdout prediction.
    This function is deliberately manifest-only/read-only: it cannot invent
    a receipt, backfill a missing arrival, or turn a training-only archive
    row into a holdout.
    """

    reasons: list[str] = []
    min_train = _nonnegative_int(candidate.get("min_train_days"))
    min_holdout = _nonnegative_int(candidate.get("min_holdout_days"))
    if str(candidate.get("threshold_profile") or "") != FORMAL_OOS_PROFILE:
        reasons.append("strict_oos_threshold_profile_invalid")
    if min_train != 20 or min_holdout != 8:
        reasons.append("strict_oos_window_not_exact_20_train_8_holdout")
    if _nonnegative_int(candidate.get("formal_promotion_min_train_days")) != 20:
        reasons.append("formal_train_threshold_not_20")
    if _nonnegative_int(candidate.get("formal_promotion_min_holdout_days")) != 8:
        reasons.append("formal_holdout_threshold_not_8")
    temporal_scope = str(candidate.get("oos_event_temporal_scope") or "")
    future_event_count = _nonnegative_int(candidate.get("future_event_count"))
    if temporal_scope and temporal_scope != HISTORICAL_OOS_TEMPORAL_SCOPE:
        reasons.append("oos_temporal_scope_not_closed_historical")
    if future_event_count != 0:
        reasons.append("oos_future_event_count_nonzero")

    raw_cross_predictions = candidate.get("predictions")
    cross_predictions = (
        raw_cross_predictions
        if isinstance(raw_cross_predictions, list)
        else []
    )
    if len(cross_predictions) != min_holdout:
        reasons.append("cross_holdout_prediction_count_mismatch")
    cross_by_date: dict[str, Mapping[str, Any]] = {}
    for prediction in cross_predictions:
        if not isinstance(prediction, Mapping):
            reasons.append("cross_holdout_prediction_invalid")
            continue
        event_date = str(prediction.get("event_date") or "")
        if not _valid_local_date(event_date) or event_date in cross_by_date:
            reasons.append("cross_holdout_event_date_invalid_or_duplicate")
            continue
        cross_by_date[event_date] = prediction
        if not _valid_pre_event_utc(
            prediction.get("decision_at_utc"),
            event_date=event_date,
            settlement_timezone=str(candidate.get("settlement_timezone") or "") or None,
            allow_historical_republisher=(
                _historical_republisher_cross_timing_allowed(
                    candidate,
                    selected,
                    event_date=event_date,
                    cross_decision_at_utc=prediction.get("decision_at_utc"),
                )
            ),
        ):
            reasons.append("cross_holdout_decision_not_pre_event")
    cross_dates = tuple(sorted(cross_by_date))
    if len(cross_dates) != min_holdout:
        reasons.append("cross_holdout_unique_date_count_mismatch")
    if cross_dates:
        if (
            str(candidate.get("holdout_start_date") or "")
            != cross_dates[0]
            or str(candidate.get("holdout_end_date") or "")
            != cross_dates[-1]
        ):
            reasons.append("cross_holdout_boundary_mismatch")

    evaluations = candidate.get("system_evaluations")
    if not isinstance(evaluations, Mapping):
        evaluations = {}
        reasons.append("system_evaluations_missing")
    expected_systems = set(selected)
    actual_systems = {str(key) for key in evaluations}
    if actual_systems != expected_systems:
        reasons.append("system_evaluation_system_set_mismatch")
    transport_map = candidate.get("system_transport_provider_ids")
    transports = transport_map if isinstance(transport_map, Mapping) else {}
    system_summaries: list[dict[str, Any]] = []
    cross_train_end = str(candidate.get("train_end_date") or "")
    for system_id in selected:
        raw_evaluation = evaluations.get(system_id)
        evaluation = (
            raw_evaluation
            if isinstance(raw_evaluation, Mapping)
            else {}
        )
        system_reasons: list[str] = []
        if str(evaluation.get("state") or "") != "OOS_READY":
            system_reasons.append("system_oos_not_ready")
        if str(evaluation.get("system_id") or "") != system_id:
            system_reasons.append("system_id_mismatch")
        if (
            str(evaluation.get("transport_provider_id") or "")
            != str(transports.get(system_id) or "")
        ):
            system_reasons.append("system_transport_lineage_mismatch")
        train_n = _nonnegative_int(evaluation.get("train_n"))
        holdout_n = _nonnegative_int(evaluation.get("holdout_n"))
        if train_n < min_train:
            system_reasons.append("system_train_sample_below_20")
        if holdout_n != min_holdout:
            system_reasons.append("system_holdout_sample_not_exact_8")
        train_end = str(evaluation.get("train_end_date") or "")
        holdout_start = str(evaluation.get("holdout_start_date") or "")
        holdout_end = str(evaluation.get("holdout_end_date") or "")
        if not all(_valid_local_date(value) for value in (
            train_end, holdout_start, holdout_end,
        )) or not (train_end < holdout_start <= holdout_end):
            system_reasons.append("system_train_holdout_chronology_invalid")
        if cross_train_end and train_end > cross_train_end:
            system_reasons.append("system_train_end_after_cross_train_end")
        source_counts = evaluation.get("train_source_counts")
        if not isinstance(source_counts, Mapping):
            system_reasons.append("system_train_source_counts_missing")
        else:
            try:
                source_total = sum(int(value) for value in source_counts.values())
                if any(int(value) < 0 for value in source_counts.values()):
                    raise ValueError
            except (TypeError, ValueError):
                system_reasons.append("system_train_source_counts_invalid")
            else:
                if source_total != train_n:
                    system_reasons.append("system_train_source_count_mismatch")
        holdout_source_class = str(evaluation.get("holdout_source_class") or "")
        if holdout_source_class not in _STRICT_HOLDOUT_SOURCE_CLASSES:
            system_reasons.append("system_holdout_source_not_native_strict")
        provider = str(evaluation.get("transport_provider_id") or transports.get(system_id) or "")
        if provider == "OPEN_METEO" and holdout_source_class != HISTORICAL_REPUBLISHER_RECEIPT:
            system_reasons.append("republisher_holdout_contract_class_invalid")
        if provider != "OPEN_METEO" and holdout_source_class != "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE":
            system_reasons.append("native_holdout_contract_class_invalid")
        raw_predictions = evaluation.get("predictions")
        predictions = raw_predictions if isinstance(raw_predictions, list) else []
        if len(predictions) != holdout_n:
            system_reasons.append("system_holdout_prediction_count_mismatch")
        by_date: dict[str, Mapping[str, Any]] = {}
        for prediction in predictions:
            if not isinstance(prediction, Mapping):
                system_reasons.append("system_holdout_prediction_invalid")
                continue
            event_date = str(prediction.get("event_date") or "")
            if not _valid_local_date(event_date) or event_date in by_date:
                system_reasons.append("system_holdout_event_date_invalid_or_duplicate")
                continue
            by_date[event_date] = prediction
            member_set_sha = prediction.get("member_set_sha256")
            member_hashes = prediction.get("member_run_content_shas")
            if not _is_sha256(member_set_sha):
                system_reasons.append("system_member_set_hash_invalid")
            if (
                not isinstance(member_hashes, list)
                or not member_hashes
                or any(not _is_sha256(item) for item in member_hashes)
                or len(set(member_hashes)) != len(member_hashes)
            ):
                system_reasons.append("system_member_raw_hash_lineage_invalid")
            if not _valid_pre_event_utc(
                prediction.get("decision_at_utc"),
                event_date=event_date,
                settlement_timezone=str(candidate.get("settlement_timezone") or "") or None,
                allow_historical_republisher=(
                    _historical_republisher_timing_allowed(
                        candidate,
                        system_id=system_id,
                        event_date=event_date,
                    )
                ),
            ):
                system_reasons.append("system_holdout_arrival_not_pre_event")
            if provider == "OPEN_METEO" and not prediction.get("historical_oos_contract_ids"):
                system_reasons.append("republisher_historical_oos_contract_missing")
            cross = cross_by_date.get(event_date)
            if cross is None:
                system_reasons.append("system_holdout_not_in_cross_cohort")
            else:
                hashes = cross.get("system_member_set_shas")
                if (
                    not isinstance(hashes, Mapping)
                    or str(hashes.get(system_id) or "") != str(member_set_sha or "")
                ):
                    system_reasons.append("cross_member_set_hash_binding_mismatch")
        if tuple(sorted(by_date)) != cross_dates:
            system_reasons.append("system_holdout_dates_not_exact_cross_cohort")
        if system_reasons:
            reasons.extend(
                system_id + ":" + reason
                for reason in sorted(set(system_reasons))
            )
        system_summaries.append({
            "ensemble_system_id": system_id,
            "transport_provider_id": str(evaluation.get("transport_provider_id") or "") or None,
            "train_n": train_n,
            "holdout_n": holdout_n,
            "holdout_source_class": str(evaluation.get("holdout_source_class") or "") or None,
            "holdout_member_hash_lineage_complete": all(
                _is_sha256(item.get("member_set_sha256"))
                and isinstance(item.get("member_run_content_shas"), list)
                and bool(item.get("member_run_content_shas"))
                and all(_is_sha256(value) for value in item["member_run_content_shas"])
                for item in by_date.values()
            ),
            "reasons": sorted(set(system_reasons)),
        })
    return {
        "state": (
            "VERIFIED_STRICT_20_TRAIN_8_HOLDOUT_NATIVE_HASH_LINEAGE"
            if not reasons
            else "BLOCKED_STRICT_20_TRAIN_8_HOLDOUT_EVIDENCE"
        ),
        "reasons": sorted(set(reasons)),
        "cross_holdout_event_dates": list(cross_dates),
        "system_summaries": system_summaries,
    }


def _strict_candidate_check(
    candidate: Mapping[str, Any],
    *,
    family_audit: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    state = str(candidate.get("state") or "")
    if state not in {PASS, REJECTED}:
        raise ContractError("native_repair_candidate_state_invalid")
    selected = _selected_ids(candidate)
    scope = _candidate_scope(candidate)
    blockers: list[str] = []
    strict_oos_evidence = _strict_oos_sample_evidence(candidate, selected)
    if candidate.get("authority_scope") != NO_LIVE_SCOPE:
        blockers.append("authority_scope_invalid")
    if candidate.get("p0_exact_match_ready") is not True or candidate.get("p0_exact_match_blocker") is not None:
        blockers.append("exact_mask_not_ready")
    if _nonnegative_int(candidate.get("min_train_days")) < 20:
        blockers.append("train_threshold_below_20")
    if _nonnegative_int(candidate.get("min_holdout_days")) < 8:
        blockers.append("holdout_threshold_below_8")
    if _nonnegative_int(candidate.get("rolling_origin_fold_count")) < 8:
        blockers.append("holdout_fold_count_below_8")
    if float(candidate.get("effective_sample_n") or 0) < 8:
        blockers.append("effective_sample_below_8")
    if str(candidate.get("probability_pathway") or P0_PROBABILITY_PATHWAY) != P0_PROBABILITY_PATHWAY:
        blockers.append("probability_pathway_invalid")
    if int(candidate.get("independent_family_count") or 0) < 2:
        blockers.append("independent_family_count_below_2")
    if strict_oos_evidence["state"] != (
        "VERIFIED_STRICT_20_TRAIN_8_HOLDOUT_NATIVE_HASH_LINEAGE"
    ):
        blockers.extend(str(strict_oos_evidence_reason) for strict_oos_evidence_reason in strict_oos_evidence["reasons"])
    lineage = _candidate_lineage(candidate, selected)
    if lineage["state"] not in {
        "OFFICIAL_NATIVE_LINEAGE",
        "HISTORICAL_REPUBLISHER_LINEAGE",
    }:
        blockers.append(str(lineage.get("reason") or "non_official_lineage"))
    if (
        lineage["state"] == "HISTORICAL_REPUBLISHER_LINEAGE"
        and candidate.get("formal_p0_eligible") is True
    ):
        blockers.append("republisher_formal_p0_eligibility_invalid")
    if family_audit is not None:
        family_state = str(family_audit.get("state") or "")
        # A minimal unit-test fixture may intentionally omit the foundation
        # registry.  Real manifests always carry ``system_family_ids``; for
        # those candidates a missing registry is a hard blocker rather than a
        # pointer that merely looks independently audited.
        if family_state.startswith("BLOCKED_"):
            blockers.append(str(family_audit.get("reason") or family_state))
        elif (
            family_state == "UNVERIFIED_SCHEMA_MISSING"
            and isinstance(candidate.get("system_family_ids"), Mapping)
        ):
            blockers.append("independent_family_registry_unavailable")
    if blockers:
        return {
            "state": "BLOCKED_CANDIDATE_CONTRACT",
            "reasons": sorted(set(blockers)),
            "lineage": lineage,
            "independent_family_audit": dict(family_audit or {}),
            "strict_oos_evidence": strict_oos_evidence,
        }, selected
    return {
        "state": "STRICT_OOS_CANDIDATE_VALID",
        "lineage": lineage,
        "independent_family_audit": dict(family_audit or {}),
        "strict_oos_evidence": strict_oos_evidence,
    }, selected


def _latest_evaluation(conn: sqlite3.Connection, scope: Mapping[str, Any], *, as_of_utc: str) -> tuple[dict[str, Any] | None, bool]:
    rows = conn.execute(
        "SELECT evaluation_id,oos_state,selection_evidence_sha256,evaluated_at_utc,"
        "min_train_days,min_holdout_days,train_end_date,holdout_start_date,holdout_end_date,content_sha256 "
        "FROM calibration_cross_native_ensemble_oos_evaluation "
        "WHERE station_id=? AND season_bin=? AND lead_bucket=? AND run_cycle=? "
        "AND run_cycle_by_system_json=? AND run_cycle_by_system_sha256=? "
        "AND settlement_definition=? AND ensemble_system_ids_json=? "
        "AND available_system_set_hash=? AND evaluated_at_utc<=? "
        "ORDER BY evaluated_at_utc DESC,evaluation_id DESC LIMIT 2",
        (
            scope["station_id"], scope["season_bin"], scope["lead_bucket"], scope["run_cycle"],
            scope["run_cycle_by_system_json"], scope["run_cycle_by_system_sha256"],
            scope["settlement_definition"], scope["ensemble_system_ids_json"],
            scope["available_system_set_hash"], as_of_utc,
        ),
    ).fetchall()
    if not rows:
        return None, False
    row = rows[0]
    ambiguous = len(rows) > 1 and str(rows[0]["evaluated_at_utc"]) == str(rows[1]["evaluated_at_utc"])
    return dict(row), ambiguous


def _artifact_rows(conn: sqlite3.Connection, scope: Mapping[str, Any], selection_sha: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT x.cross_ensemble_artifact_id,x.selection_evidence_sha256,x.content_sha256 AS artifact_content_sha256,"
        "x.valid_from_utc AS artifact_valid_from_utc,x.valid_until_utc AS artifact_valid_until_utc,"
        "a.activation_id,a.generation,a.content_sha256 AS activation_content_sha256,"
        "a.valid_from_utc,a.valid_until_utc,a.activated_at_utc "
        "FROM calibration_cross_native_ensemble_artifact x "
        "LEFT JOIN calibration_native_ensemble_activation a "
        "ON a.artifact_kind='CROSS_NATIVE_ENSEMBLE' AND a.artifact_id=x.cross_ensemble_artifact_id "
        "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "WHERE x.station_id=? AND x.season_bin=? AND x.lead_bucket=? AND x.run_cycle=? "
        "AND x.run_cycle_by_system_json=? AND x.run_cycle_by_system_sha256=? "
        "AND x.settlement_definition=? AND x.ensemble_system_ids_json=? "
        "AND x.selection_evidence_sha256=? "
        "ORDER BY a.generation DESC,a.activation_id DESC",
        (
            scope["station_id"], scope["season_bin"], scope["lead_bucket"], scope["run_cycle"],
            scope["run_cycle_by_system_json"], scope["run_cycle_by_system_sha256"],
            scope["settlement_definition"], scope["ensemble_system_ids_json"], selection_sha,
        ),
    ).fetchall()
    return [dict(row) for row in rows]


def _scope_activations(conn: sqlite3.Connection, scope: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT a.activation_id,a.artifact_id,a.station_id,a.season_bin,a.lead_bucket,a.run_cycle,"
        "a.run_cycle_by_system_json,a.run_cycle_by_system_sha256,a.settlement_definition,"
        "a.available_system_set_hash_or_star,a.generation,a.valid_from_utc,a.valid_until_utc,"
        "a.activated_at_utc,a.content_sha256 AS activation_content_sha256,"
        "x.selection_evidence_sha256,x.content_sha256 AS artifact_content_sha256 "
        "FROM calibration_native_ensemble_activation a "
        "JOIN calibration_cross_native_ensemble_artifact x ON x.cross_ensemble_artifact_id=a.artifact_id "
        "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "AND a.station_id=? AND a.season_bin=? AND a.lead_bucket=? AND a.run_cycle=? "
        "AND a.run_cycle_by_system_json=? AND a.run_cycle_by_system_sha256=? "
        "AND a.settlement_definition=? AND a.available_system_set_hash_or_star=? "
        "ORDER BY a.generation DESC,a.activation_id DESC",
        (
            scope["station_id"], scope["season_bin"], scope["lead_bucket"], scope["run_cycle"],
            scope["run_cycle_by_system_json"], scope["run_cycle_by_system_sha256"],
            scope["settlement_definition"], scope["available_system_set_hash"],
        ),
    ).fetchall()
    return [dict(row) for row in rows]


def _expected_pointer(candidate: Mapping[str, Any], *, manifest_sha: str, report_sha: str, candidate_sha: str, activated_at_utc: str, generation: int) -> dict[str, Any]:
    scope = _candidate_scope(candidate)
    selected = list(_selected_ids(candidate))
    activated = datetime.fromisoformat(activated_at_utc.replace("Z", "+00:00"))
    valid_until = (activated + timedelta(days=DEFAULT_VALID_DAYS)).isoformat().replace("+00:00", "Z")
    cross_identity = {
        "station_id": scope["station_id"],
        "season_bin": scope["season_bin"],
        "lead_bucket": scope["lead_bucket"],
        "run_cycle": scope["run_cycle"],
        "run_cycle_by_system_json": scope["run_cycle_by_system_json"],
        "run_cycle_by_system_sha256": scope["run_cycle_by_system_sha256"],
        "settlement_definition": scope["settlement_definition"],
        "ensemble_system_ids_json": scope["ensemble_system_ids_json"],
        "ensemble_system_count": len(selected),
        "independent_family_count": int(candidate["independent_family_count"]),
        "supported_exact_masks_json": _json([selected]),
        "selection_evidence_sha256": content_sha256(_selection_identity(candidate)),
        "combination_method": (
            # Historical identity reproduction: this repair lane rebuilds
            # pre-bridge-era artifacts, and their content sha covers this
            # exact string.  The literal is the OLD rule's name on purpose;
            # the live builder writes the actual seat rule since 2026-08-21.
            "train_rmse_inverse_family_capped_v1::"
            + str(candidate["threshold_profile"])
            + "::cycle_scope=" + str(candidate.get("run_cycle_scope") or scope["run_cycle_scope"])
            + "::run_cycle_by_system_sha256=" + scope["run_cycle_by_system_sha256"]
        ),
        "family_weight_caps_json": _json(candidate["family_weight_caps"]),
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "valid_from_utc": activated_at_utc,
        "valid_until_utc": valid_until,
    }
    cross_sha = content_sha256(cross_identity)
    cross_id = "native-cross-artifact-" + cross_sha[:24]
    activation_identity = {
        "artifact_kind": "CROSS_NATIVE_ENSEMBLE",
        "artifact_id": cross_id,
        "station_id": scope["station_id"],
        "ensemble_system_id_or_star": "*",
        "season_bin": scope["season_bin"],
        "lead_bucket": scope["lead_bucket"],
        "run_cycle": scope["run_cycle"],
        "run_cycle_by_system_json": scope["run_cycle_by_system_json"],
        "run_cycle_by_system_sha256": scope["run_cycle_by_system_sha256"],
        "settlement_definition": scope["settlement_definition"],
        "available_system_set_hash_or_star": scope["available_system_set_hash"],
        "generation": int(generation),
        "valid_from_utc": activated_at_utc,
        "valid_until_utc": valid_until,
        "activated_at_utc": activated_at_utc,
        "authority_scope": NO_LIVE_SCOPE,
    }
    activation_sha = content_sha256(activation_identity)
    pointer_identity = {
        "schema": "native_ensemble_artifact_pointer_v1",
        "authority_scope": NO_LIVE_SCOPE,
        "source_manifest_sha256": manifest_sha,
        "source_report_sha256": report_sha,
        "source_candidate_sha256": candidate_sha,
        "selection_evidence_sha256": cross_identity["selection_evidence_sha256"],
        "cross_ensemble_artifact_id": cross_id,
        "cross_artifact_content_sha256": cross_sha,
        "activation_id": "native-activation-" + activation_sha[:24],
        "activation_identity_sha256": activation_sha,
        "valid_from_utc": activated_at_utc,
        "valid_until_utc": valid_until,
        "generation": int(generation),
        "write_policy": "append_only_no_live_only_review_required",
    }
    return {
        "pointer": {**pointer_identity, "pointer_sha256": content_sha256(pointer_identity)},
        "cross_artifact_identity_sha256": cross_sha,
        "cross_ensemble_artifact_id": cross_id,
        "activation_identity_sha256": activation_sha,
        "activation_id": pointer_identity["activation_id"],
        "generation": int(generation),
        "valid_from_utc": activated_at_utc,
        "valid_until_utc": valid_until,
    }


def _existing_pointer(
    *,
    manifest_sha: str,
    report_sha: str,
    candidate_sha: str,
    selection_sha: str,
    artifact: Mapping[str, Any],
    activation: Mapping[str, Any],
) -> dict[str, Any]:
    """Point at an already materialized immutable artifact/activation.

    The source manifest remains part of the pointer provenance, while the
    artifact and activation hashes are taken from the database row itself.
    This avoids inventing a second generation merely because the repair plan
    was produced at a different wall-clock time.
    """

    pointer_identity = {
        "schema": "native_ensemble_artifact_pointer_v1",
        "authority_scope": NO_LIVE_SCOPE,
        "source_manifest_sha256": manifest_sha,
        "source_report_sha256": report_sha,
        "source_candidate_sha256": candidate_sha,
        "selection_evidence_sha256": selection_sha,
        "cross_ensemble_artifact_id": str(artifact["cross_ensemble_artifact_id"]),
        "cross_artifact_content_sha256": str(artifact["artifact_content_sha256"]),
        "activation_id": str(activation["activation_id"]),
        "activation_identity_sha256": str(activation["activation_content_sha256"]),
        "valid_from_utc": str(activation["valid_from_utc"]),
        "valid_until_utc": str(activation["valid_until_utc"]),
        "generation": int(activation["generation"]),
        "write_policy": "existing_append_only_no_live_only_no_write",
    }
    pointer = {**pointer_identity, "pointer_sha256": content_sha256(pointer_identity)}
    return {
        "pointer": pointer,
        "cross_artifact_identity_sha256": str(artifact["artifact_content_sha256"]),
        "cross_ensemble_artifact_id": str(artifact["cross_ensemble_artifact_id"]),
        "activation_identity_sha256": str(activation["activation_content_sha256"]),
        "activation_id": str(activation["activation_id"]),
        "generation": int(activation["generation"]),
        "valid_from_utc": str(activation["valid_from_utc"]),
        "valid_until_utc": str(activation["valid_until_utc"]),
    }


def _candidate_plan(conn: sqlite3.Connection, candidate: Mapping[str, Any], *, report_sha: str, manifest_sha: str, activated_at_utc: str, as_of_utc: str) -> dict[str, Any]:
    selected = _selected_ids(candidate)
    family_audit = _independent_family_audit(conn, candidate, selected)
    validation, selected = _strict_candidate_check(
        candidate,
        family_audit=family_audit,
    )
    scope = _candidate_scope(candidate)
    selection_sha = content_sha256(_selection_identity(candidate))
    candidate_sha = _candidate_sha(candidate)
    latest, ambiguous = _latest_evaluation(conn, scope, as_of_utc=as_of_utc)
    latest_state = None if latest is None else str(latest["oos_state"])
    artifacts = _artifact_rows(conn, scope, selection_sha)
    artifact = artifacts[0] if artifacts else None
    activations = _scope_activations(conn, scope)
    exact_activation = next((row for row in activations if str(row["selection_evidence_sha256"]) == selection_sha), None)
    active_exact = None
    if exact_activation is not None:
        try:
            if str(exact_activation["valid_from_utc"]) <= as_of_utc < str(exact_activation["valid_until_utc"]):
                active_exact = exact_activation
        except Exception:
            active_exact = None
    # Two admission regimes share this planner, and they hold different
    # powers.  The strict 20-train/8-holdout recompute keeps its monopoly on
    # *writes*: every READY_* action below still requires it, because a
    # repair write mints authority from this plan alone.  Attestation of an
    # *existing* activation is a different act -- the governed resident lane
    # already wrote that activation under the ratified forward-accumulation
    # regime, and the live compiler will consume it the moment its latest
    # exact-mask evaluation is a PASS.  When this planner refused to attest
    # such rows for failing the retired historical profile, the pointer and
    # the compiler diverged, and every fresh multi-system packet died at
    # ``native_current_pointer_not_bound`` while its activation sat PASSing
    # in the database.  The pointer's job is to name what the compiler will
    # consume, so the attestation basis mirrors the compiler's own rule,
    # floored by the forward regime's ratified minimums.
    strict_valid = validation["state"] == "STRICT_OOS_CANDIDATE_VALID"
    family_state = str((family_audit or {}).get("state") or "")
    forward_floor_ok = (
        candidate.get("authority_scope") == NO_LIVE_SCOPE
        and _nonnegative_int(candidate.get("min_train_days"))
        >= FORWARD_ACCUMULATION_MIN_TRAIN_DAYS
        and _nonnegative_int(candidate.get("min_holdout_days"))
        >= FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS
        and int(candidate.get("independent_family_count") or 0) >= 2
        and not family_state.startswith("BLOCKED_")
        and family_state != "UNVERIFIED_SCHEMA_MISSING"
    )
    db_attested = (
        not ambiguous
        and latest is not None
        and latest_state == PASS
        and str(latest["selection_evidence_sha256"]) == selection_sha
        and active_exact is not None
        and artifact is not None
    )
    attestation_basis = None
    if db_attested and (strict_valid or forward_floor_ok):
        action = "NOOP_ALREADY_CONSUMABLE"
        attestation_basis = (
            "STRICT_HISTORICAL_MANIFEST"
            if strict_valid
            else "FORWARD_ACCUMULATION_DB_EVALUATION"
        )
    elif not strict_valid:
        action = "BLOCKED_CANDIDATE_CONTRACT"
    elif str(candidate.get("state")) == REJECTED:
        action = "BLOCKED_MANIFEST_REJECTED"
    elif ambiguous:
        action = "BLOCKED_AMBIGUOUS_LATEST_EVALUATION"
    elif latest is None:
        action = "READY_APPEND_EXACT_EVALUATION"
    elif latest_state != PASS:
        action = "BLOCKED_LATEST_REJECTED"
    elif str(latest["selection_evidence_sha256"]) != selection_sha:
        action = "BLOCKED_LATEST_SELECTION_MISMATCH"
    elif artifact is not None:
        action = "READY_REPAIR_ACTIVATION_ONLY"
    else:
        action = "READY_REPAIR_ARTIFACT_AND_ACTIVATION"
    generation = 1 + max((int(row["generation"]) for row in activations), default=0)
    pointer = None
    if action == "NOOP_ALREADY_CONSUMABLE" and artifact is not None and active_exact is not None:
        pointer = _existing_pointer(
            manifest_sha=manifest_sha,
            report_sha=report_sha,
            candidate_sha=candidate_sha,
            selection_sha=selection_sha,
            artifact=artifact,
            activation=active_exact,
        )
    elif action in {"READY_REPAIR_ACTIVATION_ONLY", "READY_REPAIR_ARTIFACT_AND_ACTIVATION"}:
        pointer = _expected_pointer(
            candidate,
            manifest_sha=manifest_sha,
            report_sha=report_sha,
            candidate_sha=candidate_sha,
            activated_at_utc=activated_at_utc,
            generation=generation,
        )
    return {
        "scope": scope,
        "selected_system_ids": list(selected),
        "candidate_state": str(candidate.get("state")),
        "candidate_sha256": candidate_sha,
        "selection_evidence_sha256": selection_sha,
        "manifest_report_sha256": report_sha,
        "validation": validation,
        "independent_family_audit": family_audit,
        "latest_exact_oos_evaluation": latest,
        "latest_evaluation_ambiguous": bool(ambiguous),
        "existing_exact_artifact": artifact,
        "existing_exact_activation": active_exact or exact_activation,
        "existing_scope_activation_count": len(activations),
        "action": action,
        "attestation_basis": attestation_basis,
        "artifact_pointer": None if pointer is None else pointer["pointer"],
        "expected_artifact": None if pointer is None else {
            "cross_ensemble_artifact_id": pointer["cross_ensemble_artifact_id"],
            "cross_artifact_content_sha256": pointer["cross_artifact_identity_sha256"],
            "activation_id": pointer["activation_id"],
            "activation_identity_sha256": pointer["activation_identity_sha256"],
            "generation": pointer["generation"],
        },
        "source": {
            "manifest_content_sha256": manifest_sha,
            "report_content_sha256": report_sha,
        },
    }


def _activation_plan(conn: sqlite3.Connection, row: Mapping[str, Any], *, as_of_utc: str) -> dict[str, Any]:
    scope = {
        "station_id": str(row["station_id"]),
        "season_bin": str(row["season_bin"]),
        "lead_bucket": str(row["lead_bucket"]),
        "run_cycle": str(row["run_cycle"]),
        "run_cycle_by_system_json": str(row["run_cycle_by_system_json"]),
        "run_cycle_by_system_sha256": str(row["run_cycle_by_system_sha256"]),
        "settlement_definition": str(row["settlement_definition"]),
        "ensemble_system_ids_json": str(row["ensemble_system_ids_json"]),
        "available_system_set_hash": str(row["available_system_set_hash_or_star"]),
    }
    latest, ambiguous = _latest_evaluation(conn, scope, as_of_utc=as_of_utc)
    latest_sha = None if latest is None else str(latest["selection_evidence_sha256"])
    current_rows = _scope_activations(conn, scope)
    matching = next((item for item in current_rows if str(item["selection_evidence_sha256"]) == latest_sha), None) if latest_sha else None
    active = str(row["valid_from_utc"]) <= as_of_utc < str(row["valid_until_utc"])
    artifact_sha = str(row["selection_evidence_sha256"])
    if ambiguous:
        action = "BLOCKED_AMBIGUOUS_LATEST_EVALUATION"
        state = "AMBIGUOUS_LATEST_EXACT_EVALUATION"
    elif latest is None:
        action = "BLOCKED_MISSING_EXACT_EVALUATION"
        state = "MISSING_EXACT_EVALUATION"
    elif str(latest["oos_state"]) != PASS:
        action = "BLOCKED_LATEST_REJECTED"
        state = "LATEST_EXACT_OOS_REJECTED"
    elif artifact_sha != latest_sha:
        if matching is not None:
            action = "NOOP_SUPERSEDED_SELECTION_MISMATCH"
            state = "SUPERSEDED_SELECTION_MISMATCH"
        else:
            action = "BLOCKED_SELECTION_MISMATCH_NO_CURRENT_PASS_ACTIVATION"
            state = "SELECTION_MISMATCH"
    elif matching is not None and str(matching["activation_id"]) != str(row["activation_id"]):
        action = "NOOP_SUPERSEDED_GENERATION"
        state = "SUPERSEDED_GENERATION"
    elif not active:
        action = "BLOCKED_ACTIVATION_NOT_CURRENT"
        state = "ACTIVATION_NOT_CURRENT"
    else:
        action = "NOOP_CURRENT_EXACT_PASS"
        state = "CURRENT_EXACT_PASS"
    return {
        "activation_id": str(row["activation_id"]),
        "artifact_id": str(row["artifact_id"]),
        "station_id": str(row["station_id"]),
        "generation": int(row["generation"]),
        "scope": scope,
        "activation_selection_evidence_sha256": artifact_sha,
        "latest_exact_oos_evaluation": latest,
        "latest_evaluation_ambiguous": bool(ambiguous),
        "superseded_by_activation_id": None if matching is None else str(matching["activation_id"]),
        "active_at_as_of": bool(active),
        "state": state,
        "action": action,
        "reason": {
            "MISSING_EXACT_EVALUATION": "exact_mask_evaluation_absent",
            "LATEST_EXACT_OOS_REJECTED": "latest_exact_mask_oos_rejected",
            "SUPERSEDED_SELECTION_MISMATCH": "newer_activation_matches_latest_selection",
            "SELECTION_MISMATCH": "latest_selection_has_no_current_activation",
        }.get(state),
    }


def _system_activation_repair_plan(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
) -> dict[str, Any]:
    """Audit per-system activation rows without creating system pointers.

    Cross-ensemble pointers are only useful when their per-system roots are
    formal, native, and strict-OOS compatible.  Historical bootstrap rows for
    AIFS/ICON are retained as append-only evidence, but their 2-train/1-holdout
    profile and unknown cycle must never be mistaken for a repairable native
    activation.  This helper makes that exclusion explicit in the same
    read-only plan as cross-ensemble repair evidence.
    """

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='calibration_native_ensemble_system_artifact'"
    ).fetchone()
    activation_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='calibration_native_ensemble_activation'"
    ).fetchone()
    if table is None or activation_table is None:
        return {
            "schema_state": "MISSING_FOUNDATION_TABLE",
            "activation_count": 0,
            "action_summary": {},
            "model_summary": {},
            "plans": [],
        }
    total = int(conn.execute(
        "SELECT COUNT(*) FROM calibration_native_ensemble_activation "
        "WHERE artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
        "AND authority_scope=?",
        (NO_LIVE_SCOPE,),
    ).fetchone()[0])
    if total > MAX_SYSTEM_ACTIVATION_ROWS:
        raise ContractError("native_repair_system_activation_bound_exceeded")
    rows = conn.execute(
        "SELECT a.activation_id,a.artifact_id,a.station_id,"
        "a.ensemble_system_id_or_star,a.season_bin,a.lead_bucket,a.run_cycle,"
        "a.run_cycle_by_system_json,a.run_cycle_by_system_sha256,"
        "a.generation,a.valid_from_utc,a.valid_until_utc,a.activated_at_utc,"
        "a.authority_scope,a.content_sha256 AS activation_content_sha256,"
        "s.system_artifact_id,s.train_start_date,s.train_end_date,"
        "s.holdout_start_date,s.holdout_end_date,s.rolling_origin_fold_count,"
        "s.effective_sample_n,s.reliability_parameters_json,"
        "s.member_qc_contract_sha256,s.content_sha256 AS artifact_content_sha256 "
        "FROM calibration_native_ensemble_activation a "
        "JOIN calibration_native_ensemble_system_artifact s "
        "ON s.system_artifact_id=a.artifact_id "
        "WHERE a.artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
        "AND a.authority_scope=? "
        "ORDER BY a.ensemble_system_id_or_star,a.station_id,a.generation,"
        "a.activation_id",
        (NO_LIVE_SCOPE,),
    ).fetchall()
    plans: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        reasons: list[str] = []
        try:
            reliability = json.loads(str(row["reliability_parameters_json"]))
        except (TypeError, json.JSONDecodeError):
            reliability = {}
            reasons.append("reliability_parameters_invalid")
        if not isinstance(reliability, Mapping):
            reliability = {}
            reasons.append("reliability_parameters_not_object")
        profile = str(reliability.get("oos_threshold_profile") or "")
        transport = str(reliability.get("transport_provider_id") or "")
        scopes = reliability.get("source_authority_scopes")
        scopes_tuple = tuple(sorted(str(item) for item in scopes)) if isinstance(scopes, list) else ()
        if profile != FORMAL_OOS_PROFILE:
            reasons.append(
                "bootstrap_profile"
                if profile in {BOOTSTRAP_OOS_PROFILE, LEGACY_BOOTSTRAP_OOS_PROFILE}
                else "strict_oos_profile_invalid"
            )
        if int(row["rolling_origin_fold_count"] or 0) < 8:
            reasons.append("holdout_fold_count_below_8")
        if float(row["effective_sample_n"] or 0) < 20:
            reasons.append("train_sample_below_20")
        republisher_fallback = transport == "OPEN_METEO"
        if transport not in OFFICIAL_NATIVE_TRANSPORTS and not republisher_fallback:
            reasons.append("official_native_transport_missing_or_invalid")
        if republisher_fallback:
            if str(reliability.get("holdout_source_class") or "") != HISTORICAL_REPUBLISHER_RECEIPT:
                reasons.append("republisher_historical_holdout_not_verified")
            if str(reliability.get("oos_event_temporal_scope") or "") != HISTORICAL_OOS_TEMPORAL_SCOPE:
                reasons.append("republisher_temporal_scope_not_closed_historical")
            if _nonnegative_int(reliability.get("future_event_count")) != 0:
                reasons.append("republisher_future_event_count_nonzero")
            if reliability.get("official_native_archive") is True:
                reasons.append("republisher_marked_official_native")
        elif reliability.get("official_native_archive") is not True:
            reasons.append("official_native_archive_not_verified")
        if "CALIBRATION_TRAIN_ONLY" not in scopes_tuple:
            reasons.append("calibration_train_scope_missing")
        if (
            str(row["run_cycle"] or "") not in VALID_NATIVE_RUN_CYCLES
            and not republisher_fallback
        ):
            reasons.append("native_run_cycle_missing_or_unknown")
        try:
            active = str(row["valid_from_utc"]) <= as_of_utc < str(row["valid_until_utc"])
        except Exception:
            active = False
        if not active:
            reasons.append("activation_not_current")
        state = "READY_FORMAL_SYSTEM_ARTIFACT" if not reasons else "BLOCKED_SYSTEM_ARTIFACT"
        plans.append({
            "activation_id": str(row["activation_id"]),
            "artifact_id": str(row["artifact_id"]),
            "system_artifact_id": str(row["system_artifact_id"]),
            "station_id": str(row["station_id"]),
            "ensemble_system_id": str(row["ensemble_system_id_or_star"]),
            "season_bin": str(row["season_bin"]),
            "lead_bucket": str(row["lead_bucket"]),
            "run_cycle": str(row["run_cycle"]),
            "generation": int(row["generation"]),
            "authority_scope": str(row["authority_scope"]),
            "active_at_as_of": bool(active),
            "oos_threshold_profile": profile,
            "transport_provider_id": transport or None,
            "source_authority_scopes": list(scopes_tuple),
            "official_native_archive": reliability.get("official_native_archive"),
            "rolling_origin_fold_count": int(row["rolling_origin_fold_count"] or 0),
            "effective_sample_n": float(row["effective_sample_n"] or 0),
            "state": state,
            "action": (
                "NOOP_FORMAL_SYSTEM_ROOT_PRESENT"
                if state == "READY_FORMAL_SYSTEM_ARTIFACT"
                else "BLOCKED_SYSTEM_ROOT_NOT_FORMAL"
            ),
            "reasons": sorted(set(reasons)),
            "pointer": None,
            "repair_policy": (
                "existing_formal_system_root_reused_only"
                if state == "READY_FORMAL_SYSTEM_ARTIFACT"
                else "never_activate_bootstrap_or_unknown_cycle_root"
            ),
        })
    action_summary = Counter(str(item["action"]) for item in plans)
    model_summary: dict[str, dict[str, Any]] = {}
    for item in plans:
        model = str(item["ensemble_system_id"])
        summary = model_summary.setdefault(model, {
            "activation_count": 0,
            "ready_formal_count": 0,
            "blocked_count": 0,
            "reason_counts": Counter(),
        })
        summary["activation_count"] += 1
        summary["ready_formal_count"] += int(item["state"] == "READY_FORMAL_SYSTEM_ARTIFACT")
        summary["blocked_count"] += int(item["state"] != "READY_FORMAL_SYSTEM_ARTIFACT")
        summary["reason_counts"].update(item["reasons"])
    for summary in model_summary.values():
        summary["reason_counts"] = dict(sorted(summary["reason_counts"].items()))
    return {
        "schema_state": "FOUNDATION_TABLES_PRESENT",
        "activation_count": len(plans),
        "action_summary": dict(sorted(action_summary.items())),
        "model_summary": dict(sorted(model_summary.items())),
        "plans": plans,
    }


def build_plan(
    database: Path,
    manifest_path: Path,
    *,
    as_of_utc: str,
    activated_at_utc: str,
    selected_system_ids: Sequence[object] | str | None = None,
) -> dict[str, Any]:
    as_of = _utc(as_of_utc, name="as_of_utc")
    activated = _utc(activated_at_utc, name="activated_at_utc")
    selected_mask = _selected_system_ids_filter(selected_system_ids)
    manifest = _manifest(manifest_path)
    manifest_sha = str(manifest["content_sha256"])
    reports = sorted((dict(item) for item in manifest["reports"]), key=lambda item: str(item["station_id"]))
    manifest_candidate_count = sum(len(report.get("cross_candidates") or []) for report in reports)
    if manifest_candidate_count > MAX_CANDIDATE_ROWS:
        raise ContractError("native_repair_candidate_bound_exceeded")
    assert_process_memory_budget(projected_bytes=MAX_JSON_OUTPUT_BYTES)
    with _connection(database) as conn:
        system_activation_plan = _system_activation_repair_plan(
            conn,
            as_of_utc=as_of,
        )
        candidate_plans: list[dict[str, Any]] = []
        for report in reports:
            candidates = report.get("cross_candidates") or []
            if not isinstance(candidates, list):
                raise ContractError("native_repair_report_candidates_invalid")
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    raise ContractError("native_repair_candidate_invalid")
                # The candidate's station must agree with its enclosing report;
                # otherwise a valid OOS packet could be rebound cross-station.
                if str(candidate.get("station_id") or "").upper() != str(report["station_id"]).upper():
                    raise ContractError("native_repair_candidate_station_mismatch")
                # A filtered plan is an exact-mask derivation, not a
                # best-effort subset: candidates carrying any other system
                # selection (including ECCC-containing duplicates) are not
                # allowed to produce pointers in this plan.
                if selected_mask is not None:
                    raw_selected = candidate.get("selected_system_ids")
                    if (
                        not isinstance(raw_selected, list)
                        or tuple(str(item) for item in raw_selected)
                        != selected_mask
                    ):
                        # Do not let malformed non-target cohorts abort an
                        # exact-mask derivation.  The selected candidate is
                        # still fully validated by _candidate_plan below.
                        continue
                candidate_plans.append(_candidate_plan(
                    conn,
                    candidate,
                    report_sha=str(report["content_sha256"]),
                    manifest_sha=manifest_sha,
                    activated_at_utc=activated,
                    as_of_utc=as_of,
                ))
        activation_query = (
            "SELECT a.activation_id,a.artifact_id,a.station_id,a.season_bin,a.lead_bucket,a.run_cycle,"
            "a.run_cycle_by_system_json,a.run_cycle_by_system_sha256,a.settlement_definition,"
            "a.available_system_set_hash_or_star,a.generation,a.valid_from_utc,a.valid_until_utc,"
            "a.activated_at_utc,a.content_sha256 AS activation_content_sha256,"
            "x.selection_evidence_sha256,x.ensemble_system_ids_json,x.content_sha256 AS artifact_content_sha256 "
            "FROM calibration_native_ensemble_activation a "
            "JOIN calibration_cross_native_ensemble_artifact x ON x.cross_ensemble_artifact_id=a.artifact_id "
            "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' AND a.authority_scope=? "
        )
        activation_params: list[object] = [NO_LIVE_SCOPE]
        if selected_mask is not None:
            activation_query += "AND x.ensemble_system_ids_json=? "
            activation_params.append(_json(list(selected_mask)))
        activation_query += "ORDER BY a.station_id,a.generation,a.activation_id LIMIT ?"
        activation_params.append(MAX_ACTIVATION_ROWS)
        activation_rows = conn.execute(
            activation_query,
            tuple(activation_params),
        ).fetchall()
        if len(activation_rows) >= MAX_ACTIVATION_ROWS:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM calibration_native_ensemble_activation WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE' AND authority_scope=?",
                (NO_LIVE_SCOPE,),
            ).fetchone()[0])
            if total > MAX_ACTIVATION_ROWS:
                raise ContractError("native_repair_activation_bound_exceeded")
        activation_plans = [_activation_plan(conn, dict(row), as_of_utc=as_of) for row in activation_rows]
        # The candidate manifest is one snapshot of one OOS run; the database
        # is the accumulation of every governed run, including the resident
        # forward lane's.  A consumable activation whose scope the manifest
        # did not re-evaluate (an older lead still current, a station whose
        # fresh candidates all failed) is still what the compiler will
        # consume, and leaving it unpointed splits the pointer view from the
        # DB view -- the exact-station-set check downstream then correctly
        # fails closed and the whole snapshot refuses to build.  So every
        # current exact-PASS activation not already covered by a candidate
        # becomes a DB-sourced binding entry; provenance marks it as such.
        candidate_scope_keys = {
            (
                str((item.get("scope") or {}).get("station_id") or ""),
                str((item.get("scope") or {}).get("season_bin") or ""),
                str((item.get("scope") or {}).get("lead_bucket") or ""),
                str((item.get("scope") or {}).get("run_cycle") or ""),
            )
            for item in candidate_plans
            if item.get("artifact_pointer") is not None
        }
        for row, activation_plan in zip(activation_rows, activation_plans):
            if activation_plan.get("action") != "NOOP_CURRENT_EXACT_PASS":
                continue
            row = dict(row)
            scope_key = (
                str(row["station_id"]),
                str(row["season_bin"]),
                str(row["lead_bucket"]),
                str(row["run_cycle"]),
            )
            if scope_key in candidate_scope_keys:
                continue
            candidate_scope_keys.add(scope_key)
            selection_sha = str(row["selection_evidence_sha256"])
            pointer = _existing_pointer(
                manifest_sha=manifest_sha,
                report_sha=str(row["activation_content_sha256"]),
                candidate_sha=str(row["activation_content_sha256"]),
                selection_sha=selection_sha,
                artifact={
                    "cross_ensemble_artifact_id": str(row["artifact_id"]),
                    "artifact_content_sha256": str(
                        row["artifact_content_sha256"]
                    ),
                },
                activation={
                    "activation_id": str(row["activation_id"]),
                    "activation_content_sha256": str(
                        row["activation_content_sha256"]
                    ),
                    "valid_from_utc": str(row["valid_from_utc"]),
                    "valid_until_utc": str(row["valid_until_utc"]),
                    "generation": int(row["generation"]),
                },
            )
            try:
                selected_ids = sorted(
                    str(item)
                    for item in json.loads(str(row["ensemble_system_ids_json"]))
                )
            except (TypeError, json.JSONDecodeError):
                continue
            candidate_plans.append({
                "scope": dict(activation_plan["scope"]),
                "selected_system_ids": selected_ids,
                "candidate_state": PASS,
                "candidate_sha256": str(row["activation_content_sha256"]),
                "selection_evidence_sha256": selection_sha,
                "manifest_report_sha256": str(
                    row["activation_content_sha256"]
                ),
                "validation": {
                    "state": "DB_CURRENT_EXACT_PASS_ATTESTED",
                    "reasons": [],
                },
                "independent_family_audit": {
                    "state": "DB_ATTESTED_NOT_REAUDITED",
                    "reason": None,
                },
                "latest_exact_oos_evaluation": activation_plan[
                    "latest_exact_oos_evaluation"
                ],
                "latest_evaluation_ambiguous": False,
                "existing_exact_artifact": None,
                "existing_exact_activation": None,
                "existing_scope_activation_count": 0,
                "action": "NOOP_ALREADY_CONSUMABLE",
                "attestation_basis": "FORWARD_ACCUMULATION_DB_EVALUATION",
                "artifact_pointer": pointer["pointer"],
                "expected_artifact": {
                    "cross_ensemble_artifact_id": pointer[
                        "cross_ensemble_artifact_id"
                    ],
                    "cross_artifact_content_sha256": pointer[
                        "cross_artifact_identity_sha256"
                    ],
                    "activation_id": pointer["activation_id"],
                    "activation_identity_sha256": pointer[
                        "activation_identity_sha256"
                    ],
                    "generation": pointer["generation"],
                },
                "source": {
                    "manifest_content_sha256": manifest_sha,
                    "db_activation_id": str(row["activation_id"]),
                },
            })
    candidate_summary = Counter(str(item["action"]) for item in candidate_plans)
    activation_summary = Counter(str(item["action"]) for item in activation_plans)
    family_audit_summary = Counter(
        str(item["independent_family_audit"].get("state") or "UNKNOWN")
        for item in candidate_plans
    )
    model_summary: dict[str, dict[str, Any]] = {}
    for item in candidate_plans:
        key = _json(item["selected_system_ids"])
        summary = model_summary.setdefault(key, {
            "candidate_count": 0,
            "station_ids": set(),
            "pass_count": 0,
            "rejected_count": 0,
            "official_native_lineage_count": 0,
            "historical_republisher_lineage_count": 0,
        })
        summary["candidate_count"] += 1
        summary["station_ids"].add(str(item["scope"]["station_id"]))
        summary["pass_count"] += int(str(item["candidate_state"]) == PASS)
        summary["rejected_count"] += int(str(item["candidate_state"]) == REJECTED)
        summary["official_native_lineage_count"] += int(
            item["validation"].get("lineage", {}).get("state") == "OFFICIAL_NATIVE_LINEAGE"
        )
        summary["historical_republisher_lineage_count"] += int(
            item["validation"].get("lineage", {}).get("state")
            == "HISTORICAL_REPUBLISHER_LINEAGE"
        )
    for summary in model_summary.values():
        summary["station_ids"] = sorted(summary["station_ids"])
        summary["station_count"] = len(summary["station_ids"])
    model_summary = dict(sorted(model_summary.items()))
    # One pointer per scope.  Two different system subsets can both hold a
    # PASSing activation for the same (station, season, lead, run_cycle) --
    # ZUUU's h12_24/MIXED carried {ECMWF,GEFS} and {ECCC,GEFS} side by side --
    # but the binding must name exactly one activation or the pointer is
    # ambiguous, which is the thing it exists to prevent.  Prefer what the
    # compiler itself prefers among compatible activations: higher generation,
    # then the richer system set, then the stable id.  The runner-up keeps its
    # NOOP action (it is still consumable authority in the database); only the
    # pointer is withheld.
    preferred_by_scope: dict[tuple[str, str, str, str], dict] = {}
    for item in candidate_plans:
        if item.get("artifact_pointer") is None:
            continue
        scope = item.get("scope") or {}
        key = (
            str(scope.get("station_id") or ""),
            str(scope.get("season_bin") or ""),
            str(scope.get("lead_bucket") or ""),
            str(scope.get("run_cycle") or ""),
        )
        rank = (
            int((item.get("expected_artifact") or {}).get("generation") or 0),
            len(item.get("selected_system_ids") or ()),
            str((item.get("expected_artifact") or {}).get("activation_id") or ""),
        )
        incumbent = preferred_by_scope.get(key)
        if incumbent is None or rank > incumbent["_scope_rank"]:
            if incumbent is not None:
                incumbent["artifact_pointer"] = None
                incumbent["pointer_withheld_reason"] = (
                    "scope_pointer_superseded_by_richer_activation"
                )
            item["_scope_rank"] = rank
            preferred_by_scope[key] = item
        else:
            item["artifact_pointer"] = None
            item["pointer_withheld_reason"] = (
                "scope_pointer_superseded_by_richer_activation"
            )
    for item in preferred_by_scope.values():
        item.pop("_scope_rank", None)
    pointer_count = sum(item["artifact_pointer"] is not None for item in candidate_plans)
    repair_ready_count = sum(str(item["action"]).startswith("READY_") for item in candidate_plans)
    identity = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "as_of_utc": as_of,
        "activated_at_utc": activated,
        "database": str(database.expanduser().resolve()),
        "manifest": str(manifest_path.expanduser().resolve()),
        "manifest_content_sha256": manifest_sha,
        "selected_system_ids_filter": (
            None if selected_mask is None else list(selected_mask)
        ),
        "manifest_candidate_count": manifest_candidate_count,
        "station_count": len(reports),
        "canonical_station_ids": sorted(str(report["station_id"]).upper() for report in reports),
        "candidate_count": len(candidate_plans),
        "filtered_out_candidate_count": manifest_candidate_count - len(candidate_plans),
        "activation_count": len(activation_plans),
        "candidate_action_summary": dict(sorted(candidate_summary.items())),
        "activation_action_summary": dict(sorted(activation_summary.items())),
        "independent_family_audit_summary": dict(sorted(family_audit_summary.items())),
        "system_activation_schema_state": system_activation_plan["schema_state"],
        "system_activation_count": system_activation_plan["activation_count"],
        "system_activation_action_summary": system_activation_plan["action_summary"],
        "system_model_summary": system_activation_plan["model_summary"],
        "model_mask_summary": model_summary,
        "artifact_pointer_count": pointer_count,
        "repair_ready_count": repair_ready_count,
        "safe_write_state": (
            "NO_SAFE_REPAIR_REQUIRED_CURRENT_PASS"
            if repair_ready_count == 0
            else "PLAN_ONLY_APPEND_REVIEW_REQUIRED"
        ),
        "automatic_write_authorized": False,
        "candidate_plans": candidate_plans,
        "activation_plans": activation_plans,
        "system_activation_plans": system_activation_plan["plans"],
        "boundary": {
            "database_mode": "read_only_query_only",
            "writes_target_database": False,
            "calls_network": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
            "raw_refetch": False,
            "append_only_history_mutated": False,
        },
        "repair_policy": {
            "strict_pass_pointer_reuse": "only exact latest PRODUCTION_NO_LIVE_OOS_PASS with closed historical lineage; official native preferred, Open-Meteo fallback allowed when explicitly contracted",
            "selection_mismatch": "superseded only when a newer exact activation already matches latest selection; never mutate old rows",
            "missing_exact_evaluation": "blocked until signed manifest evaluation is appended; no activation is inferred",
            "latest_rejected": "blocked; prior PASS cannot be revived",
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "output/audits/native_ensemble_forward_oos_readonly_20260802_native_activation.json")
    parser.add_argument("--db", type=Path, default=ROOT / "data/bayes_basket_target_ssot.db")
    parser.add_argument("--as-of-utc", default=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    parser.add_argument("--activated-at-utc", default=None, help="Pointer activation timestamp; defaults to the manifest as-of time.")
    parser.add_argument(
        "--selected-system-ids",
        nargs="+",
        default=None,
        metavar="SYSTEM_ID",
        help=(
            "Optional exact system mask for a derived plan. Accepts either "
            "comma-separated IDs or separate arguments; candidates and "
            "cross-ensemble activations with any other mask are excluded."
        ),
    )
    parser.add_argument("--output", type=Path, default=ROOT / "output/audits/native_ensemble_activation_repair_plan_20260802.json")
    args = parser.parse_args()
    manifest = _manifest(args.manifest.expanduser().resolve(strict=True))
    activated = args.activated_at_utc or str(manifest["as_of_utc"])
    plan = build_plan(
        args.db.expanduser().resolve(strict=True),
        args.manifest.expanduser().resolve(strict=True),
        as_of_utc=args.as_of_utc,
        activated_at_utc=activated,
        selected_system_ids=args.selected_system_ids,
    )
    wrote = write_bounded_json_if_changed(args.output.expanduser().resolve(), plan)
    print(_json({
        "schema": SCHEMA,
        "content_sha256": plan["content_sha256"],
        "station_count": plan["station_count"],
        "candidate_count": plan["candidate_count"],
        "selected_system_ids_filter": plan["selected_system_ids_filter"],
        "filtered_out_candidate_count": plan["filtered_out_candidate_count"],
        "activation_count": plan["activation_count"],
        "candidate_action_summary": plan["candidate_action_summary"],
        "activation_action_summary": plan["activation_action_summary"],
        "artifact_pointer_count": plan["artifact_pointer_count"],
        "repair_ready_count": plan["repair_ready_count"],
        "output": str(args.output.expanduser().resolve()),
        "wrote": wrote,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
