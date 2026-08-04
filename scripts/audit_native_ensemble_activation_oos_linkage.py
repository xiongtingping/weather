#!/usr/bin/env python3
"""Read-only exact-OOS linkage audit for native-ensemble activations.

An activation is not authority by itself.  ``compile_native_ensemble_p0``
consumes it only when the *latest* exact-mask OOS evaluation at decision time
is a PASS for the artifact's immutable selection evidence.  This audit makes
that same condition observable without repairing append-only history, writing
an activation, or opening any order/service path.

When ``--current-repair-plan`` is supplied, its exact content-addressed
``NOOP_ALREADY_CONSUMABLE`` pointer set is an additional current-root gate.
Rows outside that set remain historical evidence but are reported as
unconsumable; the activation table is never mutated.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    assert_process_memory_budget,
    assert_target_db_capacity,
    load_bounded_json,
    write_bounded_json_if_changed,
)


SCHEMA = "native_ensemble_activation_oos_linkage_audit_v1"
MAX_ACTIVATION_ROWS = 2_048
MAX_UNCONSUMABLE_DETAILS = 256
PASS = "PRODUCTION_NO_LIVE_OOS_PASS"
NO_LIVE_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
P0_PATHWAY = "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE"
MIXED_NATIVE_CYCLES = "MIXED_NATIVE_CYCLES"
CURRENT_POINTER_NOT_BOUND = "UNCONSUMABLE_CURRENT_POINTER_NOT_BOUND"
REPAIR_PLAN_SCHEMA = "native_ensemble_activation_repair_plan_v1"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(value: str, *, name: str) -> str:
    text = str(value or "").strip()
    if not text.endswith("Z"):
        raise ValueError(f"{name}_must_be_utc_z")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name}_timezone_missing")
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _connection(database: Path) -> sqlite3.Connection:
    if database.is_symlink():
        raise ValueError("native_ensemble_linkage_audit_db_symlink_forbidden")
    bounded = assert_target_db_capacity(database, projected_bytes=0)
    connection = sqlite3.connect(
        f"file:{bounded}?mode=ro", uri=True, timeout=15.0
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=15000")
    return connection


def _canonical_mask(raw: str) -> tuple[list[str], str | None]:
    """Validate the exact ordered system mask used by the runtime compiler."""

    try:
        parsed = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return [], "ensemble_system_ids_json_invalid"
    if (
        not isinstance(parsed, list)
        or len(parsed) < 2
        or any(not isinstance(item, str) or not item for item in parsed)
    ):
        return [], "ensemble_system_ids_invalid"
    selected = sorted(parsed)
    if selected != parsed or len(set(selected)) != len(selected):
        return [], "ensemble_system_ids_not_canonical_unique"
    if _json(selected) != str(raw):
        return [], "ensemble_system_ids_json_not_canonical"
    return selected, None


def _canonical_cycle_mapping(
    raw: str | None,
    raw_sha: str | None,
    *,
    system_ids: list[str],
    run_cycle: str,
) -> str | None:
    try:
        parsed = json.loads(str(raw or ""))
    except (TypeError, json.JSONDecodeError):
        return "run_cycle_by_system_json_invalid"
    if not isinstance(parsed, dict):
        return "run_cycle_by_system_not_object"
    expected_keys = set(system_ids)
    if set(parsed) != expected_keys:
        return "run_cycle_by_system_keys_mismatch"
    canonical: dict[str, str] = {}
    for system_id in system_ids:
        cycle = parsed.get(system_id)
        if not isinstance(cycle, str) or not cycle:
            return "run_cycle_by_system_value_missing"
        canonical[system_id] = cycle
    canonical_json = _json(canonical)
    if canonical_json != str(raw):
        return "run_cycle_by_system_json_not_canonical"
    if content_sha256(canonical) != str(raw_sha or ""):
        return "run_cycle_by_system_sha_mismatch"
    expected_cycle = (
        next(iter(set(canonical.values())))
        if len(set(canonical.values())) == 1
        else MIXED_NATIVE_CYCLES
    )
    if str(run_cycle) != expected_cycle:
        return "run_cycle_not_derived_from_system_mapping"
    return None


def _identity_error(row: Mapping[str, Any]) -> str | None:
    """Check the immutable fields the runtime must be able to match exactly."""

    if any(
        str(row[f"activation_{field}"])
        != str(row[f"artifact_{field}"])
        for field in (
            "station_id",
            "season_bin",
            "lead_bucket",
            "run_cycle",
            "settlement_definition",
            "run_cycle_by_system_json",
            "run_cycle_by_system_sha256",
        )
    ):
        return "activation_artifact_scope_mismatch"
    if str(row["artifact_probability_pathway"]) != P0_PATHWAY:
        return "artifact_probability_pathway_invalid"
    if str(row["activation_authority_scope"]) != NO_LIVE_SCOPE:
        return "activation_authority_scope_invalid"
    system_ids, mask_error = _canonical_mask(
        str(row["artifact_ensemble_system_ids_json"])
    )
    if mask_error:
        return mask_error
    if int(row["artifact_ensemble_system_count"]) != len(system_ids):
        return "artifact_ensemble_system_count_mismatch"
    if int(row["artifact_independent_family_count"]) < 2:
        return "artifact_independent_family_count_invalid"
    if (
        content_sha256(system_ids)
        != str(row["activation_available_system_set_hash"])
    ):
        return "activation_available_system_set_hash_mismatch"
    return _canonical_cycle_mapping(
        row["artifact_run_cycle_by_system_json"],
        row["artifact_run_cycle_by_system_sha256"],
        system_ids=system_ids,
        run_cycle=str(row["artifact_run_cycle"]),
    )


def _latest_evaluations(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    as_of_utc: str,
) -> list[sqlite3.Row]:
    """Mirror the compiler's exact scope lookup (maximum two rows)."""

    return connection.execute(
        "SELECT evaluation_id,oos_state,selection_evidence_sha256,"
        "evaluated_at_utc FROM calibration_cross_native_ensemble_oos_evaluation "
        "WHERE station_id=? AND season_bin=? AND lead_bucket=? "
        "AND run_cycle=? AND run_cycle_by_system_json=? "
        "AND run_cycle_by_system_sha256=? AND settlement_definition=? "
        "AND ensemble_system_ids_json=? AND available_system_set_hash=? "
        "AND evaluated_at_utc<=? "
        "ORDER BY evaluated_at_utc DESC,evaluation_id DESC LIMIT 2",
        (
            row["artifact_station_id"],
            row["artifact_season_bin"],
            row["artifact_lead_bucket"],
            row["artifact_run_cycle"],
            row["artifact_run_cycle_by_system_json"],
            row["artifact_run_cycle_by_system_sha256"],
            row["artifact_settlement_definition"],
            row["artifact_ensemble_system_ids_json"],
            row["activation_available_system_set_hash"],
            as_of_utc,
        ),
    ).fetchall()


def _has_artifact_matching_pass(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    as_of_utc: str,
) -> bool:
    matched = connection.execute(
        "SELECT 1 FROM calibration_cross_native_ensemble_oos_evaluation "
        "WHERE station_id=? AND season_bin=? AND lead_bucket=? "
        "AND run_cycle=? AND run_cycle_by_system_json=? "
        "AND run_cycle_by_system_sha256=? AND settlement_definition=? "
        "AND ensemble_system_ids_json=? AND available_system_set_hash=? "
        "AND selection_evidence_sha256=? AND oos_state=? "
        "AND evaluated_at_utc<=? LIMIT 1",
        (
            row["artifact_station_id"],
            row["artifact_season_bin"],
            row["artifact_lead_bucket"],
            row["artifact_run_cycle"],
            row["artifact_run_cycle_by_system_json"],
            row["artifact_run_cycle_by_system_sha256"],
            row["artifact_settlement_definition"],
            row["artifact_ensemble_system_ids_json"],
            row["activation_available_system_set_hash"],
            row["artifact_selection_evidence_sha256"],
            PASS,
            as_of_utc,
        ),
    ).fetchone()
    return matched is not None


def _repair_plan_pointer_context(
    path: Path,
) -> tuple[set[str], str, list[str] | None]:
    """Load the bounded, content-addressed cutoff pointer set.

    The activation table is append-only and can contain an older, still-live
    row whose strict OOS window is no longer part of the current closed-day
    cutoff.  The repair plan's ``NOOP_ALREADY_CONSUMABLE`` rows are the sole
    current-pointer authority for this read-only audit.  We validate the
    plan and each pointer here instead of trusting station names alone.
    """

    requested = path.expanduser()
    if requested.is_symlink():
        raise ValueError("native_ensemble_linkage_audit_pointer_symlink_forbidden")
    resolved = requested.resolve(strict=True)
    payload = load_bounded_json(resolved, limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(payload, Mapping):
        raise ValueError("native_ensemble_linkage_audit_pointer_plan_invalid")
    declared = str(payload.get("content_sha256") or "").lower()
    identity = {
        key: value for key, value in payload.items() if key != "content_sha256"
    }
    if (
        str(payload.get("schema") or "") != REPAIR_PLAN_SCHEMA
        or len(declared) != 64
        or content_sha256(identity) != declared
    ):
        raise ValueError("native_ensemble_linkage_audit_pointer_plan_hash_invalid")
    plans = payload.get("candidate_plans")
    if not isinstance(plans, list):
        raise ValueError("native_ensemble_linkage_audit_pointer_plans_missing")
    raw_mask = payload.get("selected_system_ids_filter")
    selected_mask: list[str] | None = None
    if raw_mask is not None:
        if not isinstance(raw_mask, list):
            raise ValueError("native_ensemble_linkage_audit_selected_system_mask_invalid")
        selected_mask, mask_error = _canonical_mask(_json(raw_mask))
        if mask_error:
            raise ValueError(
                "native_ensemble_linkage_audit_selected_system_mask_invalid"
            )
    stations: set[str] = set()
    for plan in plans:
        if not isinstance(plan, Mapping):
            raise ValueError("native_ensemble_linkage_audit_pointer_plan_row_invalid")
        if str(plan.get("action") or "") != "NOOP_ALREADY_CONSUMABLE":
            continue
        if str(plan.get("candidate_state") or "") != PASS:
            raise ValueError("native_ensemble_linkage_audit_pointer_candidate_not_pass")
        scope = plan.get("scope")
        pointer = plan.get("artifact_pointer")
        expected = plan.get("expected_artifact")
        if not all(isinstance(value, Mapping) for value in (scope, pointer, expected)):
            raise ValueError("native_ensemble_linkage_audit_pointer_row_missing")
        if selected_mask is not None:
            if plan.get("selected_system_ids") != selected_mask:
                raise ValueError(
                    "native_ensemble_linkage_audit_selected_system_mask_mismatch"
                )
            scope_mask = str(scope.get("ensemble_system_ids_json") or "")
            parsed_scope_mask, scope_mask_error = _canonical_mask(scope_mask)
            if scope_mask_error or parsed_scope_mask != selected_mask:
                raise ValueError(
                    "native_ensemble_linkage_audit_selected_system_mask_mismatch"
                )
        station_id = str(scope.get("station_id") or "").strip().upper()
        selection_sha = str(plan.get("selection_evidence_sha256") or "").lower()
        if not station_id or len(selection_sha) != 64:
            raise ValueError("native_ensemble_linkage_audit_pointer_scope_invalid")
        pointer_generation_raw = pointer.get("generation")
        expected_generation_raw = expected.get("generation")
        generation_match = (
            pointer_generation_raw in (None, "")
            and expected_generation_raw in (None, "")
        )
        if not generation_match:
            try:
                generation_match = (
                    int(pointer_generation_raw) == int(expected_generation_raw)
                )
            except (TypeError, ValueError):
                raise ValueError(
                    "native_ensemble_linkage_audit_pointer_generation_invalid"
                )
        pointer_cross_sha = str(
            pointer.get("cross_artifact_content_sha256") or ""
        ).lower()
        expected_cross_sha = str(
            expected.get("cross_artifact_content_sha256") or ""
        ).lower()
        cross_sha_match = (
            not pointer_cross_sha and not expected_cross_sha
        ) or (
            len(pointer_cross_sha) == 64
            and len(expected_cross_sha) == 64
            and pointer_cross_sha == expected_cross_sha
        )
        pointer_activation_sha = str(
            pointer.get("activation_identity_sha256") or ""
        ).lower()
        expected_activation_sha = str(
            expected.get("activation_identity_sha256") or ""
        ).lower()
        activation_sha_match = (
            not pointer_activation_sha and not expected_activation_sha
        ) or (
            len(pointer_activation_sha) == 64
            and len(expected_activation_sha) == 64
            and pointer_activation_sha == expected_activation_sha
        )
        pointer_identity = {
            key: value for key, value in pointer.items()
            if key != "pointer_sha256"
        }
        if (
            str(pointer.get("authority_scope") or "") != NO_LIVE_SCOPE
            or str(pointer.get("selection_evidence_sha256") or "").lower()
            != selection_sha
            or str(pointer.get("cross_ensemble_artifact_id") or "")
            != str(expected.get("cross_ensemble_artifact_id") or "")
            or not cross_sha_match
            or str(pointer.get("activation_id") or "")
            != str(expected.get("activation_id") or "")
            or not activation_sha_match
            or not generation_match
            or str(pointer.get("pointer_sha256") or "").lower()
            != content_sha256(pointer_identity)
        ):
            raise ValueError("native_ensemble_linkage_audit_pointer_identity_invalid")
        if station_id in stations:
            raise ValueError("native_ensemble_linkage_audit_pointer_duplicate_station")
        stations.add(station_id)
    declared_count = int(payload.get("artifact_pointer_count") or 0)
    if declared_count != len(stations) or not stations:
        raise ValueError("native_ensemble_linkage_audit_pointer_count_mismatch")
    return stations, declared, selected_mask


def _repair_plan_pointer_station_ids(
    path: Path,
) -> tuple[set[str], str]:
    """Backward-compatible station-only view of the repair-plan context."""

    stations, declared, _selected_mask = _repair_plan_pointer_context(path)
    return stations, declared


def _classify(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    as_of_utc: str,
    current_pointer_station_ids: set[str] | None = None,
) -> dict[str, Any]:
    identity_error = _identity_error(row)
    result: dict[str, Any] = {
        "activation_id": str(row["activation_id"]),
        "artifact_id": str(row["artifact_id"]),
        "station_id": str(row["activation_station_id"]),
        "season_bin": str(row["activation_season_bin"]),
        "lead_bucket": str(row["activation_lead_bucket"]),
        "run_cycle": str(row["activation_run_cycle"]),
        "ensemble_system_ids_json": str(
            row["artifact_ensemble_system_ids_json"]
        ),
        "activation_generation": int(row["activation_generation"]),
        "activated_at_utc": str(row["activation_activated_at_utc"]),
        "valid_from_utc": str(row["activation_valid_from_utc"]),
        "valid_until_utc": str(row["activation_valid_until_utc"]),
        "active_at_as_of": (
            str(row["activation_valid_from_utc"]) <= as_of_utc
            and str(row["activation_valid_until_utc"]) > as_of_utc
        ),
        "artifact_matching_oos_pass_present": False,
        "latest_exact_oos_evaluation": None,
    }
    if identity_error is not None:
        result.update({
            "state": "UNCONSUMABLE_ACTIVATION_IDENTITY_INVALID",
            "reason": identity_error,
        })
        return result

    evaluations = _latest_evaluations(
        connection, row, as_of_utc=as_of_utc
    )
    result["artifact_matching_oos_pass_present"] = (
        _has_artifact_matching_pass(
            connection, row, as_of_utc=as_of_utc
        )
    )
    if not evaluations:
        result.update({
            "state": "UNCONSUMABLE_MISSING_EXACT_OOS_EVALUATION",
            "reason": "exact_mask_evaluation_absent",
        })
        return result

    latest = evaluations[0]
    result["latest_exact_oos_evaluation"] = {
        "evaluation_id": str(latest["evaluation_id"]),
        "oos_state": str(latest["oos_state"]),
        "selection_evidence_sha256": str(
            latest["selection_evidence_sha256"]
        ),
        "evaluated_at_utc": str(latest["evaluated_at_utc"]),
    }
    if (
        len(evaluations) > 1
        and str(evaluations[0]["evaluated_at_utc"])
        == str(evaluations[1]["evaluated_at_utc"])
    ):
        result.update({
            "state": "UNCONSUMABLE_EXACT_OOS_EVALUATION_AMBIGUOUS",
            "reason": "latest_evaluated_at_utc_not_unique",
        })
    elif str(latest["oos_state"]) != PASS:
        result.update({
            "state": "UNCONSUMABLE_LATEST_EXACT_OOS_NOT_PASS",
            "reason": "latest_exact_mask_oos_rejected",
        })
    elif (
        str(latest["selection_evidence_sha256"])
        != str(row["artifact_selection_evidence_sha256"])
    ):
        result.update({
            "state": "UNCONSUMABLE_LATEST_OOS_SELECTION_MISMATCH",
            "reason": "latest_exact_mask_selection_sha_differs_from_artifact",
        })
    else:
        result.update({
            "state": "CONSUMABLE_EXACT_OOS_PASS",
            "reason": None,
        })
    # A strict PASS is still not consumable when the immutable cutoff pointer
    # does not bind this station.  Keep the append-only activation/evaluation
    # history intact; this gate only prevents an older active row (for example
    # a scope that fell below 20 train days at the latest cutoff) from
    # entering the current P0 root set.
    if (
        result.get("state") == "CONSUMABLE_EXACT_OOS_PASS"
        and current_pointer_station_ids is not None
        and str(result["station_id"]).upper() not in current_pointer_station_ids
    ):
        result.update({
            "state": CURRENT_POINTER_NOT_BOUND,
            "reason": "station_not_in_unified_current_pointer",
        })
    return result


def _activation_rows(
    connection: sqlite3.Connection,
    *,
    selected_system_ids: list[str] | None = None,
) -> list[sqlite3.Row]:
    total = int(connection.execute(
        "SELECT COUNT(*) FROM calibration_native_ensemble_activation "
        "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE' "
        "AND authority_scope=?",
        (NO_LIVE_SCOPE,),
    ).fetchone()[0])
    if total > MAX_ACTIVATION_ROWS:
        raise ValueError("native_ensemble_linkage_audit_activation_bound_exceeded")
    query = (
        "SELECT "
        "a.activation_id,a.artifact_id,"
        "a.station_id AS activation_station_id,"
        "a.season_bin AS activation_season_bin,"
        "a.lead_bucket AS activation_lead_bucket,"
        "a.run_cycle AS activation_run_cycle,"
        "a.settlement_definition AS activation_settlement_definition,"
        "a.available_system_set_hash_or_star AS activation_available_system_set_hash,"
        "a.generation AS activation_generation,"
        "a.valid_from_utc AS activation_valid_from_utc,"
        "a.valid_until_utc AS activation_valid_until_utc,"
        "a.activated_at_utc AS activation_activated_at_utc,"
        "a.authority_scope AS activation_authority_scope,"
        "a.run_cycle_by_system_json AS activation_run_cycle_by_system_json,"
        "a.run_cycle_by_system_sha256 AS activation_run_cycle_by_system_sha256,"
        "x.station_id AS artifact_station_id,"
        "x.season_bin AS artifact_season_bin,"
        "x.lead_bucket AS artifact_lead_bucket,"
        "x.run_cycle AS artifact_run_cycle,"
        "x.settlement_definition AS artifact_settlement_definition,"
        "x.ensemble_system_ids_json AS artifact_ensemble_system_ids_json,"
        "x.ensemble_system_count AS artifact_ensemble_system_count,"
        "x.independent_family_count AS artifact_independent_family_count,"
        "x.selection_evidence_sha256 AS artifact_selection_evidence_sha256,"
        "x.probability_pathway AS artifact_probability_pathway,"
        "x.run_cycle_by_system_json AS artifact_run_cycle_by_system_json,"
        "x.run_cycle_by_system_sha256 AS artifact_run_cycle_by_system_sha256 "
        "FROM calibration_native_ensemble_activation a "
        "JOIN calibration_cross_native_ensemble_artifact x "
        "ON x.cross_ensemble_artifact_id=a.artifact_id "
        "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
        "AND a.authority_scope=? "
    )
    params: list[object] = [NO_LIVE_SCOPE]
    if selected_system_ids is not None:
        query += "AND x.ensemble_system_ids_json=? "
        params.append(_json(selected_system_ids))
    query += "ORDER BY a.activated_at_utc,a.activation_id LIMIT ?"
    params.append(MAX_ACTIVATION_ROWS)
    return connection.execute(query, tuple(params)).fetchall()


def build_report(
    database: Path,
    *,
    as_of_utc: str,
    current_pointer_station_ids: set[str] | None = None,
    current_pointer_selected_system_ids: list[str] | None = None,
    current_pointer_source_sha256: str | None = None,
    current_pointer_source_path: str | None = None,
) -> dict[str, Any]:
    """Return the bounded, read-only activation-to-OOS linkage report."""

    as_of = _utc(as_of_utc, name="as_of_utc")
    if current_pointer_station_ids is not None:
        current_pointer_station_ids = {
            str(station_id).strip().upper()
            for station_id in current_pointer_station_ids
            if str(station_id).strip()
        }
        if not current_pointer_station_ids:
            raise ValueError("native_ensemble_linkage_audit_current_pointer_empty")
    if current_pointer_selected_system_ids is not None:
        selected_mask, mask_error = _canonical_mask(
            _json(current_pointer_selected_system_ids)
        )
        if mask_error:
            raise ValueError(
                "native_ensemble_linkage_audit_selected_system_mask_invalid"
            )
        current_pointer_selected_system_ids = selected_mask
    assert_process_memory_budget(projected_bytes=MAX_JSON_OUTPUT_BYTES)
    with _connection(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = _activation_rows(
            connection,
            selected_system_ids=current_pointer_selected_system_ids,
        )
        records = [
            _classify(
                connection,
                row,
                as_of_utc=as_of,
                current_pointer_station_ids=current_pointer_station_ids,
            )
            for row in rows
        ]

    by_state: dict[str, dict[str, Any]] = {}
    by_mask: dict[str, dict[str, Any]] = {}
    state_stations: dict[str, set[str]] = defaultdict(set)
    mask_stations: dict[str, set[str]] = defaultdict(set)
    for record in records:
        state = str(record["state"])
        mask = str(record["ensemble_system_ids_json"])
        state_stations[state].add(str(record["station_id"]))
        mask_stations[mask].add(str(record["station_id"]))
        state_summary = by_state.setdefault(state, {
            "activation_count": 0,
            "active_activation_count": 0,
            "artifact_matching_oos_pass_absent_count": 0,
        })
        state_summary["activation_count"] += 1
        state_summary["active_activation_count"] += int(
            bool(record["active_at_as_of"])
        )
        state_summary["artifact_matching_oos_pass_absent_count"] += int(
            not bool(record["artifact_matching_oos_pass_present"])
        )
        mask_summary = by_mask.setdefault(mask, {
            "activation_count": 0,
            "active_activation_count": 0,
            "consumable_exact_oos_pass_count": 0,
            "unconsumable_count": 0,
        })
        mask_summary["activation_count"] += 1
        mask_summary["active_activation_count"] += int(
            bool(record["active_at_as_of"])
        )
        mask_summary["consumable_exact_oos_pass_count"] += int(
            state == "CONSUMABLE_EXACT_OOS_PASS"
        )
        mask_summary["unconsumable_count"] += int(
            state != "CONSUMABLE_EXACT_OOS_PASS"
        )

    for state, summary in by_state.items():
        summary["station_count"] = len(state_stations[state])
        summary["station_ids"] = sorted(state_stations[state])
    for mask, summary in by_mask.items():
        summary["station_count"] = len(mask_stations[mask])
        summary["station_ids"] = sorted(mask_stations[mask])

    consumable_active_stations = sorted({
        str(record["station_id"])
        for record in records
        if record["state"] == "CONSUMABLE_EXACT_OOS_PASS"
        and bool(record["active_at_as_of"])
    })
    consumable_active_station_set = set(consumable_active_stations)
    pointer_station_set_match = (
        current_pointer_station_ids is None
        or consumable_active_station_set == current_pointer_station_ids
    )
    unconsumable = [
        record for record in records
        if record["state"] != "CONSUMABLE_EXACT_OOS_PASS"
    ]
    if current_pointer_station_ids is not None and not pointer_station_set_match:
        audit_state = "FAIL_CLOSED_CURRENT_POINTER_STATION_SET_MISMATCH"
    else:
        audit_state = (
            "PASS_ALL_ACTIVATIONS_EXACT_OOS_CONSUMABLE"
            if not unconsumable
            else "FAIL_CLOSED_UNCONSUMABLE_ACTIVATIONS_PRESENT"
        )
    identity = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "as_of_utc": as_of,
        "database": str(database),
        "database_stat": {
            "size_bytes": database.stat().st_size,
            "mtime_ns": database.stat().st_mtime_ns,
        },
        "activation_count": len(records),
        "consumable_exact_oos_pass_activation_count": sum(
            record["state"] == "CONSUMABLE_EXACT_OOS_PASS"
            for record in records
        ),
        "unconsumable_activation_count": len(unconsumable),
        "active_exact_oos_consumable_activation_station_ids": (
            consumable_active_stations
        ),
        "state_summary": dict(sorted(by_state.items())),
        "system_mask_summary": dict(sorted(by_mask.items())),
        "unconsumable_records": unconsumable[:MAX_UNCONSUMABLE_DETAILS],
        "unconsumable_record_count_truncated": max(
            0, len(unconsumable) - MAX_UNCONSUMABLE_DETAILS
        ),
        "all_activation_status_evidence_sha256": content_sha256(records),
        "current_pointer": {
            "state": (
                "BOUND"
                if current_pointer_station_ids is not None
                else "NOT_SUPPLIED"
            ),
            "station_count": (
                None
                if current_pointer_station_ids is None
                else len(current_pointer_station_ids)
            ),
            "station_ids": (
                None
                if current_pointer_station_ids is None
                else sorted(current_pointer_station_ids)
            ),
            "station_set_sha256": (
                None
                if current_pointer_station_ids is None
                else content_sha256(sorted(current_pointer_station_ids))
            ),
            "active_station_ids": consumable_active_stations,
            "active_station_set_sha256": content_sha256(consumable_active_stations),
            "station_set_match": pointer_station_set_match,
            "source_plan_content_sha256": current_pointer_source_sha256,
            "source_plan_path": current_pointer_source_path,
            "selected_system_ids_filter": current_pointer_selected_system_ids,
        },
        "fail_closed_policy": {
            "runtime_rule": (
                "only latest exact-mask PRODUCTION_NO_LIVE_OOS_PASS with "
                "identical selection_evidence_sha256 is consumable"
            ),
            "unconsumable_activation_effect": (
                "compile_native_ensemble_p0 rejects it; no P0 is materialized"
            ),
            "historical_record_action": (
                "append-only records are neither backfilled, deleted, nor promoted"
            ),
        },
        "boundary": {
            "database_mode": "read_only_query_only",
            "writes_target_database": False,
            "calls_network": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        },
        "audit_state": audit_state,
    }
    report = {
        **identity,
        "content_sha256": content_sha256(identity),
    }
    assert_process_memory_budget()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path,
        default=ROOT / "data/bayes_basket_target_ssot.db",
    )
    parser.add_argument(
        "--as-of-utc",
        default=datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=(ROOT / "output/audits/"
                 "native_ensemble_activation_oos_linkage_20260802.json"),
    )
    parser.add_argument(
        "--current-repair-plan", type=Path, default=None,
        help=(
            "Optional immutable repair plan that defines the unified current "
            "pointer station set.  Without it, the audit mirrors DB-only "
            "activation/OOS linkage for backwards-compatible diagnostics."
        ),
    )
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    pointer_station_ids = None
    pointer_selected_system_ids = None
    pointer_sha = None
    pointer_path = None
    if args.current_repair_plan is not None:
        pointer_path_obj = args.current_repair_plan.expanduser().resolve(strict=True)
        (
            pointer_station_ids,
            pointer_sha,
            pointer_selected_system_ids,
        ) = _repair_plan_pointer_context(
            pointer_path_obj
        )
        pointer_path = str(pointer_path_obj)
    report = build_report(
        database,
        as_of_utc=args.as_of_utc,
        current_pointer_station_ids=pointer_station_ids,
        current_pointer_selected_system_ids=pointer_selected_system_ids,
        current_pointer_source_sha256=pointer_sha,
        current_pointer_source_path=pointer_path,
    )
    wrote = write_bounded_json_if_changed(args.output.resolve(), report)
    print(_json({
        "audit_state": report["audit_state"],
        "activation_count": report["activation_count"],
        "consumable_activation_count": report[
            "consumable_exact_oos_pass_activation_count"
        ],
        "unconsumable_activation_count": report[
            "unconsumable_activation_count"
        ],
        "output": str(args.output.resolve()),
        "wrote": wrote,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
