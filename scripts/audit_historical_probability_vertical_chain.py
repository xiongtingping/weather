#!/usr/bin/env python3
"""Read-only proof of a historical NWP P0 -> TAF P0-M -> AWC P1 chain.

This is deliberately a narrow evidence command.  It does not materialize a
probability, mutate the target SSOT, contact a weather provider, open a market
connection, or submit an order.  It follows the append-only checkpoint graph
for each latest P1 scope and distinguishes a complete *structural* historical
chain from a chain whose P0 root is eligible under the current native-ensemble
contract.  That distinction prevents the former RKPK replay from being
mistaken for a formal 45-station no-live rollout.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.station_registry import coordinate_registry_manifest


SCHEMA = "historical_probability_vertical_chain_no_live_v1"
MAX_ENVELOPES = 4_096
MAX_OBSERVATIONS = 4_096
MAX_TRUTH_ROWS = 4_096
MAX_JSON_BYTES = 1_048_576
UTC = timezone.utc
NATIVE_P0_PATHWAY = "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE"
NATIVE_SCOPES = frozenset({"PRODUCTION_NO_LIVE_ONLY", "FORMAL_PROMOTED"})


def _sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value.lower())
    )


def _utc(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("timestamp_missing")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp_timezone_required")
    return parsed.astimezone(UTC)


def _json_mapping(raw: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError(f"{field}_size_invalid")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{field}_mapping_required")
    return parsed


def _json_list(raw: Any) -> list[Any]:
    try:
        parsed = json.loads(str(raw or ""))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _reason(code: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code}
    if details:
        result["details"] = details
    return result


def _read_limited(
    conn: sqlite3.Connection,
    *,
    table: str,
    columns: str,
    where: str = "1",
    params: Sequence[Any] = (),
    limit: int,
) -> tuple[list[sqlite3.Row], list[dict[str, Any]]]:
    """Count first so no evidence table can be consumed without a cap."""

    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", tuple(params)
        ).fetchone()[0]
    )
    if total > limit:
        return [], [_reason("read_limit_exceeded", table=table, total=total, limit=limit)]
    rows = conn.execute(
        f"SELECT {columns} FROM {table} WHERE {where} ORDER BY rowid", tuple(params)
    ).fetchall()
    if len(rows) != total:
        return [], [_reason("read_count_changed", table=table)]
    return rows, []


def _envelope_rows(
    conn: sqlite3.Connection,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    rows, blockers = _read_limited(
        conn,
        table="basket_probability_envelope",
        columns=(
            "stage,checkpoint_id,station_id,event_date,settlement_timezone,"
            "decision_at_utc,content_sha256,lineage_json"
        ),
        where="state='COMMITTED'",
        limit=MAX_ENVELOPES,
    )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        checkpoint = str(row["checkpoint_id"] or "").strip()
        try:
            lineage = _json_mapping(row["lineage_json"], field="lineage_json")
            _utc(row["decision_at_utc"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(_reason("envelope_invalid", checkpoint_id=checkpoint, error=str(exc)))
            continue
        if (
            checkpoint in result
            or str(row["stage"] or "") not in {"P0", "P0-M", "P1"}
            or not _sha(row["content_sha256"])
        ):
            blockers.append(_reason("envelope_identity_invalid", checkpoint_id=checkpoint))
            continue
        result[checkpoint] = {
            "stage": str(row["stage"]),
            "checkpoint_id": checkpoint,
            "station_id": str(row["station_id"] or "").upper(),
            "event_date": str(row["event_date"] or ""),
            "settlement_timezone": str(row["settlement_timezone"] or ""),
            "decision_at_utc": str(row["decision_at_utc"]),
            "content_sha256": str(row["content_sha256"]),
            "lineage": lineage,
        }
    return result, blockers


def _lookup_facts(
    conn: sqlite3.Connection,
    envelopes: Mapping[str, Mapping[str, Any]],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[str, str], list[dict[str, Any]]],
    list[dict[str, Any]],
]:
    # The canonical fact tables are intentionally much larger than a single
    # vertical proof.  Fetch only the content-addressed observations and
    # station-days referenced by already bounded envelopes; a full-table scan
    # would itself violate the resource contract and wrongly hide valid P1.
    p1_rows = [item for item in envelopes.values() if item["stage"] == "P1"]
    observation_ids = sorted({
        str(item["lineage"].get("canonical_observation_event_id") or "").strip()
        for item in p1_rows
        if str(item["lineage"].get("canonical_observation_event_id") or "").strip()
    })
    scopes = sorted({(str(item["station_id"]), str(item["event_date"])) for item in p1_rows})
    blockers: list[dict[str, Any]] = []
    if len(observation_ids) > MAX_OBSERVATIONS:
        blockers.append(_reason("referenced_observation_limit_exceeded", total=len(observation_ids), limit=MAX_OBSERVATIONS))
        observation_rows: list[sqlite3.Row] = []
    elif observation_ids:
        placeholders = ",".join("?" for _ in observation_ids)
        observation_rows = conn.execute(
            "SELECT observation_id,station_id,event_date,source_lane,report_type,"
            "raw_sha256,canonical_committed_at,content_sha256 "
            "FROM basket_target_observation_facts "
            f"WHERE observation_id IN ({placeholders}) ORDER BY observation_id",
            observation_ids,
        ).fetchall()
    else:
        observation_rows = []
    if len(scopes) > MAX_TRUTH_ROWS:
        blockers.append(_reason("referenced_truth_scope_limit_exceeded", total=len(scopes), limit=MAX_TRUTH_ROWS))
        truth_rows: list[sqlite3.Row] = []
    elif scopes:
        placeholders = ",".join("(?,?)" for _ in scopes)
        flattened = [value for scope in scopes for value in scope]
        truth_rows = conn.execute(
            "SELECT station_id,local_date,source_lane,authority_scope,qc_status,"
            "timestamps_complete,source_payload_sha256 "
            "FROM calibration_station_daily_high_truth "
            f"WHERE (station_id,local_date) IN ({placeholders}) ORDER BY station_id,local_date,rowid",
            flattened,
        ).fetchall()
    else:
        truth_rows = []
    observations: dict[str, dict[str, Any]] = {}
    for row in observation_rows:
        observation_id = str(row["observation_id"] or "").strip()
        try:
            _utc(row["canonical_committed_at"])
        except (TypeError, ValueError) as exc:
            blockers.append(_reason("observation_timestamp_invalid", observation_id=observation_id, error=str(exc)))
            continue
        if observation_id in observations or not _sha(row["raw_sha256"]):
            blockers.append(_reason("observation_identity_invalid", observation_id=observation_id))
            continue
        observations[observation_id] = {
            "observation_id": observation_id,
            "station_id": str(row["station_id"] or "").upper(),
            "event_date": str(row["event_date"] or ""),
            "source_lane": str(row["source_lane"] or "").upper(),
            "report_type": str(row["report_type"] or "").upper(),
            "raw_sha256": str(row["raw_sha256"]),
            "canonical_committed_at": str(row["canonical_committed_at"]),
        }
    truth: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in truth_rows:
        truth[(str(row["station_id"] or "").upper(), str(row["local_date"] or ""))].append(dict(row))
    return observations, truth, blockers


def _p0_native_status(conn: sqlite3.Connection, p0: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Check current P0 authority, including content-addressed DB references."""

    lineage = p0["lineage"]
    blockers: list[dict[str, Any]] = []
    if lineage.get("p0_probability_pathway") != NATIVE_P0_PATHWAY:
        return [_reason("p0_not_current_native_pathway")]
    if str(lineage.get("native_authority_scope") or "") not in NATIVE_SCOPES:
        blockers.append(_reason("p0_native_authority_scope_invalid"))
    if str(lineage.get("native_transport_provider") or "") != "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT":
        blockers.append(_reason("p0_not_official_native_transport"))
    if str(lineage.get("aggregator_fallback_p0_eligible") or "").lower() != "false":
        blockers.append(_reason("p0_aggregator_fallback_not_excluded"))
    systems = [str(item) for item in _json_list(lineage.get("ensemble_system_ids_json"))]
    families = [str(item) for item in _json_list(lineage.get("independence_family_ids_json"))]
    if len(set(systems)) < 2 or len(set(families)) < 2:
        blockers.append(_reason("p0_independent_native_systems_insufficient"))
    activation_sha = str(lineage.get("native_cross_activation_sha256") or "")
    artifact_sha = str(lineage.get("native_cross_ensemble_artifact_sha256") or "")
    if not _sha(activation_sha) or not _sha(artifact_sha):
        blockers.append(_reason("p0_native_reference_sha_missing"))
        return blockers
    activation = conn.execute(
        "SELECT artifact_kind,artifact_id,station_id,authority_scope,valid_from_utc,valid_until_utc "
        "FROM calibration_native_ensemble_activation WHERE content_sha256=?",
        (activation_sha,),
    ).fetchone()
    artifact = conn.execute(
        "SELECT cross_ensemble_artifact_id,station_id,ensemble_system_count,"
        "independent_family_count,probability_pathway,valid_from_utc,valid_until_utc "
        "FROM calibration_cross_native_ensemble_artifact WHERE content_sha256=?",
        (artifact_sha,),
    ).fetchone()
    if activation is None or artifact is None:
        blockers.append(_reason("p0_native_reference_unresolved"))
        return blockers
    try:
        active_at = _utc(p0["decision_at_utc"])
        activation_window = _utc(activation["valid_from_utc"]) <= active_at < _utc(activation["valid_until_utc"])
        artifact_window = _utc(artifact["valid_from_utc"]) <= active_at < _utc(artifact["valid_until_utc"])
    except (TypeError, ValueError) as exc:
        blockers.append(_reason("p0_native_reference_timestamp_invalid", error=str(exc)))
        return blockers
    if (
        activation["artifact_kind"] != "CROSS_NATIVE_ENSEMBLE"
        or activation["artifact_id"] != artifact["cross_ensemble_artifact_id"]
        or activation["station_id"] != p0["station_id"]
        or activation["authority_scope"] not in NATIVE_SCOPES
        or not activation_window
    ):
        blockers.append(_reason("p0_native_activation_binding_invalid"))
    if (
        artifact["station_id"] != p0["station_id"]
        or int(artifact["ensemble_system_count"]) < 2
        or int(artifact["independent_family_count"]) < 2
        or artifact["probability_pathway"] != NATIVE_P0_PATHWAY
        or not artifact_window
    ):
        blockers.append(_reason("p0_native_artifact_binding_invalid"))
    return blockers


def _walk_to_p0m(
    final_p1: Mapping[str, Any], by_checkpoint: Mapping[str, Mapping[str, Any]]
) -> tuple[list[Mapping[str, Any]], Mapping[str, Any] | None, list[dict[str, Any]]]:
    """Follow P1 predecessors backward until the preceding P0-M is found."""

    p1_nodes: list[Mapping[str, Any]] = []
    current: Mapping[str, Any] = final_p1
    seen: set[str] = set()
    while True:
        checkpoint = str(current["checkpoint_id"])
        if checkpoint in seen:
            return p1_nodes, None, [_reason("p1_predecessor_cycle")]
        seen.add(checkpoint)
        if current["stage"] != "P1":
            return p1_nodes, None, [_reason("p1_predecessor_stage_invalid")]
        p1_nodes.append(current)
        predecessor_id = str(current["lineage"].get("predecessor_checkpoint_id") or "")
        predecessor = by_checkpoint.get(predecessor_id)
        if predecessor is None:
            return p1_nodes, None, [_reason("p1_predecessor_missing", checkpoint_id=predecessor_id)]
        if predecessor["stage"] == "P1":
            current = predecessor
            continue
        if predecessor["stage"] != "P0-M":
            return p1_nodes, None, [_reason("p1_not_rooted_in_p0m")]
        return p1_nodes, predecessor, []


def _walk_p0m_to_p0(
    p0m: Mapping[str, Any], by_checkpoint: Mapping[str, Mapping[str, Any]]
) -> tuple[list[Mapping[str, Any]], Mapping[str, Any] | None, list[dict[str, Any]]]:
    nodes: list[Mapping[str, Any]] = []
    current: Mapping[str, Any] = p0m
    seen: set[str] = set()
    while True:
        checkpoint = str(current["checkpoint_id"])
        if checkpoint in seen:
            return nodes, None, [_reason("p0m_predecessor_cycle")]
        seen.add(checkpoint)
        if current["stage"] != "P0-M":
            return nodes, None, [_reason("p0m_predecessor_stage_invalid")]
        nodes.append(current)
        predecessor_id = str(current["lineage"].get("predecessor_checkpoint_id") or "")
        predecessor = by_checkpoint.get(predecessor_id)
        if predecessor is None:
            return nodes, None, [_reason("p0m_predecessor_missing", checkpoint_id=predecessor_id)]
        if predecessor["stage"] == "P0-M":
            current = predecessor
            continue
        if predecessor["stage"] != "P0":
            return nodes, None, [_reason("p0m_not_rooted_in_p0")]
        return nodes, predecessor, []


def _monotonic(nodes: Sequence[Mapping[str, Any]]) -> bool:
    try:
        values = [_utc(item["decision_at_utc"]) for item in nodes]
    except (TypeError, ValueError):
        return False
    return all(left < right for left, right in zip(values, values[1:]))


def _p0m_status(
    conn: sqlite3.Connection, nodes: Sequence[Mapping[str, Any]], p0: Mapping[str, Any]
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    root_checkpoint = str(p0["checkpoint_id"])
    root_sha = str(p0["content_sha256"])
    for node in nodes:
        lineage = node["lineage"]
        if (
            lineage.get("p0_root_checkpoint_id") != root_checkpoint
            or lineage.get("p0_root_content_sha256") != root_sha
            or str(lineage.get("taf_hard_exclusion") or "") != "FORBIDDEN"
        ):
            blockers.append(_reason("p0m_root_or_hard_exclusion_invalid", checkpoint_id=node["checkpoint_id"]))
        required_shas = (
            "forecast_source_object_sha256",
            "taf_revision_artifact_sha256",
            "taf_revision_proof_sha256",
            "taf_physical_features_sha256",
        )
        if any(not _sha(str(lineage.get(key) or "")) for key in required_shas):
            blockers.append(_reason("p0m_taf_lineage_sha_missing", checkpoint_id=node["checkpoint_id"]))
        if not str(lineage.get("taf_physical_confirmation_state") or "").strip():
            blockers.append(_reason("p0m_physical_confirmation_missing", checkpoint_id=node["checkpoint_id"]))
        revision_id = str(lineage.get("forecast_revision_id") or "")
        source_sha = str(lineage.get("forecast_source_object_sha256") or "")
        capture = conn.execute(
            "SELECT station_id,event_date,source_object_sha256,issued_at_utc,provider_available_at_utc,"
            "source_class,authority_scope FROM calibration_forward_taf_capture "
            "WHERE revision_id=? AND source_object_sha256=?",
            (revision_id, source_sha),
        ).fetchone()
        if capture is None:
            blockers.append(_reason("p0m_forward_taf_capture_unresolved", checkpoint_id=node["checkpoint_id"]))
            continue
        try:
            time_ok = (
                _utc(capture["issued_at_utc"])
                <= _utc(capture["provider_available_at_utc"])
                <= _utc(node["decision_at_utc"])
            )
        except (TypeError, ValueError):
            time_ok = False
        if (
            capture["station_id"] != node["station_id"]
            or capture["event_date"] != node["event_date"]
            or capture["source_object_sha256"] != source_sha
            or capture["source_class"] != "official_forward_native_receipt"
            or capture["authority_scope"] != "PRODUCTION_NO_LIVE_ONLY"
            or not time_ok
        ):
            blockers.append(_reason("p0m_forward_taf_capture_invalid", checkpoint_id=node["checkpoint_id"]))
    return blockers


def _p1_awc_status(
    final: Mapping[str, Any], observations: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    lineage = final["lineage"]
    observation_id = str(lineage.get("canonical_observation_event_id") or "")
    observation = observations.get(observation_id)
    if observation is None:
        return [_reason("p1_canonical_observation_missing")]
    try:
        time_ok = _utc(observation["canonical_committed_at"]) <= _utc(final["decision_at_utc"])
    except (TypeError, ValueError):
        time_ok = False
    if (
        str(lineage.get("source_lane") or "").upper() != "AWC"
        or str(lineage.get("report_type") or "").upper() not in {"METAR", "SPECI"}
        or observation["source_lane"] != "AWC"
        or observation["report_type"] != str(lineage.get("report_type") or "").upper()
        or observation["station_id"] != final["station_id"]
        or observation["event_date"] != final["event_date"]
        or observation["raw_sha256"] != lineage.get("canonical_observation_sha256")
        or observation["canonical_committed_at"] != lineage.get("canonical_observation_committed_at")
        or not time_ok
    ):
        return [_reason("p1_awc_observation_binding_invalid")]
    return []


def _truth_status(
    scope: tuple[str, str], truth: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]]
) -> list[dict[str, Any]]:
    rows = truth.get(scope, ())
    eligible = [
        row for row in rows
        if str(row.get("source_lane") or "").upper() == "AWC"
        and str(row.get("authority_scope") or "") == "FORMAL_NATIVE_ELIGIBLE"
        and str(row.get("qc_status") or "") == "ACCEPTED"
        and int(row.get("timestamps_complete") or 0) == 1
        and _sha(str(row.get("source_payload_sha256") or ""))
    ]
    return [] if eligible else [_reason("same_day_awc_truth_missing")]


def _chain_report(
    conn: sqlite3.Connection,
    final_p1: Mapping[str, Any],
    by_checkpoint: Mapping[str, Mapping[str, Any]],
    observations: Mapping[str, Mapping[str, Any]],
    truth: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    scope = (str(final_p1["station_id"]), str(final_p1["event_date"]))
    blockers: list[dict[str, Any]] = []
    p1_reverse, p0m_tip, errors = _walk_to_p0m(final_p1, by_checkpoint)
    blockers.extend(errors)
    p0m_reverse: list[Mapping[str, Any]] = []
    p0: Mapping[str, Any] | None = None
    if p0m_tip is not None:
        p0m_reverse, p0, errors = _walk_p0m_to_p0(p0m_tip, by_checkpoint)
        blockers.extend(errors)
    p1_nodes = list(reversed(p1_reverse))
    p0m_nodes = list(reversed(p0m_reverse))
    if p0 is not None:
        expected_root = str(p0["checkpoint_id"])
        for node in [*p0m_nodes, *p1_nodes]:
            if node["lineage"].get("p0_root_checkpoint_id") != expected_root:
                blockers.append(_reason("p0_root_checkpoint_binding_invalid", checkpoint_id=node["checkpoint_id"]))
        if not _monotonic([p0, *p0m_nodes, *p1_nodes]):
            blockers.append(_reason("stage_decision_time_not_strictly_monotonic"))
        blockers.extend(_p0m_status(conn, p0m_nodes, p0))
        native_blockers = _p0_native_status(conn, p0)
    else:
        native_blockers = [_reason("p0_root_missing")]
    blockers.extend(native_blockers)
    blockers.extend(_p1_awc_status(final_p1, observations))
    blockers.extend(_truth_status(scope, truth))
    structural_blockers = [
        item for item in blockers
        if item["code"] not in {
            "p0_not_current_native_pathway",
            "p0_native_authority_scope_invalid",
            "p0_not_official_native_transport",
            "p0_aggregator_fallback_not_excluded",
            "p0_independent_native_systems_insufficient",
            "p0_native_reference_sha_missing",
            "p0_native_reference_unresolved",
            "p0_native_reference_timestamp_invalid",
            "p0_native_activation_binding_invalid",
            "p0_native_artifact_binding_invalid",
        }
    ]
    if not blockers:
        status = "FORMAL_NO_LIVE_CHAIN_PASS"
    elif not structural_blockers and native_blockers:
        status = "CANDIDATE_CHAIN_ONLY_LEGACY_P0"
    else:
        status = "BLOCKED_FAIL_CLOSED"
    stages = {"P0": p0, "P0-M": p0m_nodes[-1] if p0m_nodes else None, "P1": final_p1}
    return {
        "station_id": scope[0],
        "event_date": scope[1],
        "status": status,
        "stage_chain": ["P0", "P0-M", "P1"] if p0 is not None and p0m_nodes else [],
        "stage_lineage": {
            stage: (
                {
                    "checkpoint_id": value["checkpoint_id"],
                    "content_sha256": value["content_sha256"],
                    "decision_at_utc": value["decision_at_utc"],
                }
                if value is not None
                else None
            )
            for stage, value in stages.items()
        },
        "p0m_revision_count": len(p0m_nodes),
        "p1_observation_count": len(p1_nodes),
        "blockers": blockers,
    }


def _station_expansion(
    *,
    stations: Sequence[str],
    envelopes: Mapping[str, Mapping[str, Any]],
    observations: Mapping[str, Mapping[str, Any]],
    chains: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    native_p0_stations: set[str] = set()
    p0m_native_root_stations: set[str] = set()
    awc_p1_stations: set[str] = set()
    checkpoint_map = envelopes
    for envelope in envelopes.values():
        station = str(envelope["station_id"])
        if envelope["stage"] == "P0" and envelope["lineage"].get("p0_probability_pathway") == NATIVE_P0_PATHWAY:
            native_p0_stations.add(station)
        if envelope["stage"] == "P0-M":
            root = checkpoint_map.get(str(envelope["lineage"].get("p0_root_checkpoint_id") or ""))
            if root is not None and root["lineage"].get("p0_probability_pathway") == NATIVE_P0_PATHWAY:
                p0m_native_root_stations.add(station)
        if envelope["stage"] == "P1":
            obs = observations.get(str(envelope["lineage"].get("canonical_observation_event_id") or ""))
            if (
                str(envelope["lineage"].get("source_lane") or "").upper() == "AWC"
                and obs is not None
                and obs["source_lane"] == "AWC"
            ):
                awc_p1_stations.add(station)
    formal = {str(item["station_id"]) for item in chains if item["status"] == "FORMAL_NO_LIVE_CHAIN_PASS"}
    candidate = {str(item["station_id"]) for item in chains if item["status"] == "CANDIDATE_CHAIN_ONLY_LEGACY_P0"}
    per_station: list[dict[str, Any]] = []
    for station in stations:
        blockers: list[str] = []
        if station not in native_p0_stations:
            blockers.append("native_p0_not_materialized")
        if station not in p0m_native_root_stations:
            blockers.append("native_p0_rooted_p0m_not_materialized")
        if station not in awc_p1_stations:
            blockers.append("awc_p1_not_materialized")
        if station not in formal:
            blockers.append("formal_three_stage_chain_not_materialized")
        per_station.append({
            "station_id": station,
            "native_p0_materialized": station in native_p0_stations,
            "native_p0_rooted_p0m_materialized": station in p0m_native_root_stations,
            "awc_p1_materialized": station in awc_p1_stations,
            "formal_three_stage_chain": station in formal,
            "legacy_structural_chain_only": station in candidate,
            "blockers": blockers,
        })
    return {
        "canonical_station_count": len(stations),
        "native_p0_station_count": len(native_p0_stations & set(stations)),
        "native_p0_rooted_p0m_station_count": len(p0m_native_root_stations & set(stations)),
        "awc_p1_station_count": len(awc_p1_stations & set(stations)),
        "formal_three_stage_station_count": len(formal & set(stations)),
        "legacy_structural_chain_station_count": len(candidate & set(stations)),
        "stations": per_station,
    }


def audit_historical_probability_vertical_chain(
    db_path: str | Path = DEFAULT_TARGET_DB,
) -> dict[str, Any]:
    """Return a bounded, read-only historical stage-chain receipt."""

    db = resolve_target_database(db_path)
    if not db.is_file():
        return {
            "schema": SCHEMA,
            "status": "BLOCKED_FAIL_CLOSED",
            "read_only": True,
            "writes_target_ssot": False,
            "blockers": [_reason("database_missing")],
        }
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {
            "basket_probability_envelope",
            "basket_target_observation_facts",
            "calibration_station_daily_high_truth",
            "calibration_forward_taf_capture",
            "calibration_native_ensemble_activation",
            "calibration_cross_native_ensemble_artifact",
        }
        missing = sorted(required - tables)
        if missing:
            return {
                "schema": SCHEMA,
                "status": "BLOCKED_FAIL_CLOSED",
                "read_only": True,
                "query_only": True,
                "writes_target_ssot": False,
                "blockers": [_reason("required_table_missing", tables=missing)],
            }
        envelopes, blockers = _envelope_rows(conn)
        observations, truth, fact_blockers = _lookup_facts(conn, envelopes)
        blockers.extend(fact_blockers)
        p1_by_scope: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for envelope in envelopes.values():
            if envelope["stage"] == "P1":
                p1_by_scope[(envelope["station_id"], envelope["event_date"])].append(envelope)
        chain_reports: list[dict[str, Any]] = []
        for scope in sorted(p1_by_scope):
            final = max(
                p1_by_scope[scope],
                key=lambda item: (item["decision_at_utc"], item["checkpoint_id"]),
            )
            report = _chain_report(conn, final, envelopes, observations, truth)
            if report["stage_chain"]:
                chain_reports.append(report)
        station_manifest = coordinate_registry_manifest()
        stations = tuple(station_manifest["station_ids"])
        expansion = _station_expansion(
            stations=stations,
            envelopes=envelopes,
            observations=observations,
            chains=chain_reports,
        )
        formal_count = sum(item["status"] == "FORMAL_NO_LIVE_CHAIN_PASS" for item in chain_reports)
        candidate_count = sum(item["status"] == "CANDIDATE_CHAIN_ONLY_LEGACY_P0" for item in chain_reports)
        status = "PASS" if formal_count else "PARTIAL_FAIL_CLOSED"
        identity = {
            "schema": SCHEMA,
            "status": status,
            "read_only": True,
            "query_only": True,
            "writes_target_ssot": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
            "db_path": str(db),
            "limits": {
                "max_envelopes": MAX_ENVELOPES,
                "max_observations": MAX_OBSERVATIONS,
                "max_truth_rows": MAX_TRUTH_ROWS,
            },
            "counts": {
                "envelopes_loaded": len(envelopes),
                "observations_loaded": len(observations),
                "p1_scopes_seen": len(p1_by_scope),
                "three_stage_scopes": len(chain_reports),
                "formal_no_live_chain_scopes": formal_count,
                "legacy_p0_candidate_chain_scopes": candidate_count,
            },
            "chains": chain_reports,
            "45station_expansion": expansion,
            "blockers": blockers,
        }
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {**identity, "content_sha256": digest}
    except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {
            "schema": SCHEMA,
            "status": "BLOCKED_FAIL_CLOSED",
            "read_only": True,
            "writes_target_ssot": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "blockers": [_reason("audit_exception", error=f"{type(exc).__name__}:{exc}")],
        }
    finally:
        if conn is not None:
            conn.close()


def write_receipt(receipt: Mapping[str, Any], output: str | Path) -> Path:
    """Write only a bounded audit receipt; this never opens SQLite."""

    target = Path(output).expanduser()
    payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(payload.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("receipt_size_limit_exceeded")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    receipt = audit_historical_probability_vertical_chain(args.db)
    if args.output is not None:
        write_receipt(receipt, args.output)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if receipt.get("status") in {"PASS", "PARTIAL_FAIL_CLOSED"} else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
