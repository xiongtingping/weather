#!/usr/bin/env python3
"""Plan a read-only repair for the 45-station AWC P1 trajectory lane.

The 45-station trajectory audit already runs the real strict-as-of training
packet builder and the conditional hourly-curve trainer.  This command only
indexes that bounded evidence, checks the append-only probability-evidence
tables, and emits content-addressed pointers for candidate artifacts that
are genuinely present.  It never promotes a candidate, writes either
database, fetches a report, or turns a candidate into ``FORMAL_PROMOTED``.

The current audit has no complete v3 future-curve folds, so the normal result
is a precise blocker ledger and an empty usable-pointer set.  That is
intentional: a pointer without a complete artifact payload or strict OOS
receipt would be an unverifiable production input.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.probability_evidence_store import (  # noqa: E402
    metar_trajectory_scope_key,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    assert_process_memory_budget,
    assert_target_db_capacity,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


SCHEMA = "bayes_basket_target_p1_trajectory_repair_plan_v1"
AUDIT_SCHEMA = "bayes_basket_target_p1_trajectory_45station_audit_v1"
HOURLY_CURVE_AUDIT_SCHEMA = (
    "bayes_basket_target_p1_hourly_curve_receipt_audit_45station_v1"
)
ARTIFACT_KIND = "METAR_TRAJECTORY_LIKELIHOOD"
NO_LIVE_CANDIDATE = "PRODUCTION_NO_LIVE_CANDIDATE"
FORMAL_PROMOTED = "FORMAL_PROMOTED"
MAX_SCOPES = expected_station_count() * 4
MAX_AUDIT_BYTES = MAX_JSON_INPUT_BYTES
DEFAULT_HOURLY_CURVE_AUDIT = (
    ROOT / "output/audits/p1_hourly_curve_receipts_45station_20260802_latest.json"
)

BLOCKER_ACTIONS: dict[str, dict[str, Any]] = {
    "data_missing": {
        "action": "REBUILD_AWC_FORMAL_PREFIX",
        "required_evidence": "AWC official METAR/SPECI prefix at bucket start",
        "automatic": False,
    },
    "arrival_lineage_missing": {
        "action": "COMPLETE_NATIVE_FUTURE_CURVE_LINEAGE",
        "required_evidence": "same-capture official-native hourly future curve bound to P0/P0-M root",
        "automatic": False,
    },
    "truth_missing": {
        "action": "WAIT_FOR_CLOSED_AWC_TRUTH",
        "required_evidence": "closed official AWC daily Tmax truth and revision chain",
        "automatic": False,
    },
    "revision_unreviewed": {
        "action": "REVIEW_AWC_CORRECTION_OR_QUARANTINE_DAY",
        "required_evidence": "manual COR/AMD review receipt or station-day quarantine",
        "automatic": False,
    },
    "sample_insufficient": {
        "action": "ACCUMULATE_NATURAL_STRICT_OOS_FOLDS",
        "required_evidence": "at least 20 complete strict-as-of training folds",
        "automatic": False,
    },
    "trajectory_oos_not_ready": {
        "action": "RERUN_TRAJECTORY_STRICT_OOS_TRAINER",
        "required_evidence": "complete v3 hourly-curve folds and stable OOS gate",
        "automatic": False,
    },
    "trajectory_artifact_missing": {
        "action": "MATERIALIZE_CANDIDATE_ONLY_ARTIFACT",
        "required_evidence": "content-addressed candidate artifact after strict OOS",
        "automatic": False,
    },
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
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _load_audit(path: Path) -> Mapping[str, Any]:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError("p1_repair_audit_symlink_forbidden")
    resolved = requested.resolve(strict=True)
    payload = load_bounded_json(resolved, limit=MAX_AUDIT_BYTES)
    if not isinstance(payload, Mapping):
        raise ContractError("p1_repair_audit_not_object")
    identity = {key: value for key, value in payload.items() if key != "content_sha256"}
    if (
        str(payload.get("schema") or "") != AUDIT_SCHEMA
        or str(payload.get("content_sha256") or "") != content_sha256(identity)
        or str(payload.get("execution_mode") or "") != "production_no_live"
    ):
        raise ContractError("p1_repair_audit_contract_invalid")
    station_scope = payload.get("station_scope")
    canonical = set(coordinate_registry_manifest()["station_ids"])
    requested_stations = set((station_scope or {}).get("requested_station_ids") or ())
    if requested_stations != canonical:
        raise ContractError("p1_repair_audit_station_scope_invalid")
    scopes = payload.get("scopes")
    if not isinstance(scopes, list) or len(scopes) != MAX_SCOPES:
        raise ContractError("p1_repair_audit_scope_count_invalid")
    seen: set[tuple[str, str]] = set()
    for scope in scopes:
        if not isinstance(scope, Mapping):
            raise ContractError("p1_repair_audit_scope_invalid")
        key = (
            str(scope.get("station_id") or "").upper(),
            str(scope.get("local_time_bucket") or "").upper(),
        )
        if key[0] not in canonical or key[1] not in {"NIGHT", "MORNING", "AFTERNOON", "EVENING"} or key in seen:
            raise ContractError("p1_repair_audit_scope_identity_invalid")
        seen.add(key)
    if len(seen) != MAX_SCOPES:
        raise ContractError("p1_repair_audit_scope_set_incomplete")
    return payload


def _load_hourly_curve_audit(path: Path | None) -> Mapping[str, Any] | None:
    """Load an optional, read-only provenance audit without inventing curves."""

    if path is None:
        return None
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError("p1_repair_hourly_curve_audit_symlink_forbidden")
    if not requested.is_file():
        return None
    payload = load_bounded_json(requested.resolve(strict=True), limit=MAX_AUDIT_BYTES)
    if not isinstance(payload, Mapping):
        raise ContractError("p1_repair_hourly_curve_audit_not_object")
    identity = {key: value for key, value in payload.items() if key != "content_sha256"}
    if (
        str(payload.get("schema") or "") != HOURLY_CURVE_AUDIT_SCHEMA
        or str(payload.get("content_sha256") or "") != content_sha256(identity)
        or int(payload.get("canonical_station_count") or 0) != 45
    ):
        raise ContractError("p1_repair_hourly_curve_audit_contract_invalid")
    entries = payload.get("entries")
    if not isinstance(entries, list) or len(entries) > expected_station_count() * 31:
        raise ContractError("p1_repair_hourly_curve_audit_entries_invalid")
    return payload


def _hourly_curve_by_station(
    audit: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Collapse immutable daily receipt evidence without claiming eligibility."""

    result: dict[str, dict[str, Any]] = {}
    if audit is None:
        return result
    for raw in audit.get("entries") or ():
        if not isinstance(raw, Mapping):
            continue
        station = str(raw.get("station_id") or "").upper()
        if not station:
            continue
        bucket = result.setdefault(station, {
            "station_day_count": 0,
            "complete_native_curve_station_day_count": 0,
            "strict_asof_bindable_station_day_count": 0,
            "root_curve_bound_station_day_count": 0,
            "archive_query_after_station_day_count": 0,
        })
        bucket["station_day_count"] += 1
        bucket["complete_native_curve_station_day_count"] += int(
            int(raw.get("independence_family_count") or 0) >= 2
            and int(raw.get("minimum_family_curve_point_count") or 0) >= 4
        )
        bucket["strict_asof_bindable_station_day_count"] += int(
            raw.get("strict_asof_bindable") is True
        )
        bucket["root_curve_bound_station_day_count"] += int(
            int(raw.get("root_curve_bound_count") or 0) > 0
        )
        bucket["archive_query_after_station_day_count"] += int(
            "archive_query_after_station_day_not_strict_asof"
            in set(raw.get("blockers") or ())
        )
    return result


def _open_db(path: Path) -> sqlite3.Connection:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError("p1_repair_database_symlink_forbidden")
    resolved = requested.resolve(strict=True)
    assert_target_db_capacity(resolved, projected_bytes=0)
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _table_present(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _existing_evidence(conn: sqlite3.Connection, *, station_id: str, scope_key: str, as_of_utc: str) -> dict[str, Any]:
    if not _table_present(conn, "basket_probability_evidence_artifact"):
        return {"artifact_rows": [], "activation_rows": [], "schema_missing": True}
    artifact_rows = conn.execute(
        "SELECT artifact_id,artifact_kind,station_id,scope_key,trained_through_utc,"
        "valid_from_utc,valid_until_utc,oos_gate_passed,authority_scope,content_sha256 "
        "FROM basket_probability_evidence_artifact WHERE artifact_kind=? AND station_id=? AND scope_key=? "
        "ORDER BY valid_from_utc DESC,artifact_id DESC",
        (ARTIFACT_KIND, station_id, scope_key),
    ).fetchall()
    activation_rows: list[sqlite3.Row] = []
    if _table_present(conn, "basket_probability_evidence_activation"):
        activation_rows = conn.execute(
            "SELECT a.activation_id,a.artifact_id,a.station_id,a.scope_key,a.generation,"
            "a.valid_from_utc,a.valid_until_utc,a.activated_at_utc,a.authority_scope,a.content_sha256 "
            "FROM basket_probability_evidence_activation a WHERE a.artifact_kind=? AND a.station_id=? "
            "AND a.scope_key=? ORDER BY a.generation DESC,a.activation_id DESC",
            (ARTIFACT_KIND, station_id, scope_key),
        ).fetchall()
    return {
        "artifact_rows": [dict(row) for row in artifact_rows],
        "activation_rows": [dict(row) for row in activation_rows],
        "schema_missing": False,
    }


def _pointer(*, audit_sha: str, source_manifest_sha: str, station_id: str, scope_key: str, artifact: Mapping[str, Any], activation: Mapping[str, Any] | None) -> dict[str, Any]:
    identity = {
        "schema": "metar_trajectory_artifact_pointer_v1",
        "artifact_kind": ARTIFACT_KIND,
        "authority_scope": str(artifact["authority_scope"]),
        "station_id": station_id,
        "scope_key": scope_key,
        "artifact_id": str(artifact["artifact_id"]),
        "artifact_content_sha256": str(artifact["content_sha256"]),
        "source_audit_sha256": audit_sha,
        "source_manifest_sha256": source_manifest_sha,
        "activation_id": None if activation is None else str(activation["activation_id"]),
        "activation_content_sha256": None if activation is None else str(activation["content_sha256"]),
        "write_policy": "existing_append_only_no_live_only_no_write",
    }
    return {**identity, "pointer_sha256": content_sha256(identity)}


def _scope_plan(
    scope: Mapping[str, Any], *, audit_sha: str, conn: sqlite3.Connection,
    as_of_utc: str, hourly_curve_summary: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    station = str(scope["station_id"]).upper()
    bucket = str(scope["local_time_bucket"]).upper()
    scope_key = str((scope.get("artifact") or {}).get("scope_key") or metar_trajectory_scope_key(
        source_lane="awc",
        season_bin="JJA",
        local_time_bucket=bucket,
        maturity_bin="D0",
        settlement_definition="official_daily_tmax_c",
    ))
    source_manifest = scope.get("source_manifest") or {}
    source_manifest_sha = str(source_manifest.get("content_sha256") or "")
    evidence = _existing_evidence(conn, station_id=station, scope_key=scope_key, as_of_utc=as_of_utc)
    artifact_rows = evidence["artifact_rows"]
    candidate_rows = [row for row in artifact_rows if str(row["authority_scope"]) == NO_LIVE_CANDIDATE]
    formal_rows = [row for row in artifact_rows if str(row["authority_scope"]) == FORMAL_PROMOTED]
    active_candidate = next((row for row in candidate_rows if str(row["valid_from_utc"]) <= as_of_utc < str(row["valid_until_utc"])), None)
    active_formal = next((row for row in formal_rows if str(row["valid_from_utc"]) <= as_of_utc < str(row["valid_until_utc"])), None)
    activation_by_artifact = {
        str(row["artifact_id"]): row
        for row in evidence["activation_rows"]
        if str(row["valid_from_utc"]) <= as_of_utc < str(row["valid_until_utc"])
    }
    candidate_summary = scope.get("training", {}).get("candidate") if isinstance(scope.get("training"), Mapping) else None
    blockers = [str(item) for item in (scope.get("blockers") or ())]
    pointer = None
    if active_formal is not None:
        action = "NOOP_FORMAL_ARTIFACT_ALREADY_PRESENT"
        state = "FORMAL_PRESENT"
        pointer = _pointer(
            audit_sha=audit_sha,
            source_manifest_sha=source_manifest_sha,
            station_id=station,
            scope_key=scope_key,
            artifact=active_formal,
            activation=activation_by_artifact.get(str(active_formal["artifact_id"])),
        )
    elif active_candidate is not None:
        action = "NOOP_CANDIDATE_ARTIFACT_ALREADY_PRESENT"
        state = "CANDIDATE_PRESENT"
        pointer = _pointer(
            audit_sha=audit_sha,
            source_manifest_sha=source_manifest_sha,
            station_id=station,
            scope_key=scope_key,
            artifact=active_candidate,
            activation=activation_by_artifact.get(str(active_candidate["artifact_id"])),
        )
    elif candidate_summary and not blockers:
        # The audit intentionally keeps only a summary of a candidate.  Do
        # not emit an ingestible pointer without the immutable artifact JSON
        # and content SHA; this is an actionable payload gap, not a promotion.
        action = "BLOCKED_CANDIDATE_PAYLOAD_INCOMPLETE"
        state = "CANDIDATE_SUMMARY_ONLY"
    elif blockers:
        action = "BLOCKED_EVIDENCE"
        state = "BLOCKED_EVIDENCE"
    else:
        action = "READY_BUILD_CANDIDATE_ONLY_ARTIFACT"
        state = "NO_BLOCKER_REPORTED_ARTIFACT_MISSING"
    repair_steps = [dict(BLOCKER_ACTIONS[code], blocker=code) for code in blockers if code in BLOCKER_ACTIONS]
    retained_hourly_curves = dict(hourly_curve_summary.get(station) or {})
    if (
        "arrival_lineage_missing" in blockers
        and int(retained_hourly_curves.get("archive_query_after_station_day_count") or 0) > 0
    ):
        # The retained curves are structurally complete but their archive
        # receipt arrived after the evaluated station day.  State the exact
        # corrective action rather than suggesting that those bytes can be
        # rebound into P0/P1 retroactively.
        repair_steps = [
            step for step in repair_steps
            if step.get("blocker") != "arrival_lineage_missing"
        ]
        repair_steps.insert(0, {
            "blocker": "arrival_lineage_missing",
            "action": "CAPTURE_FORWARD_NATIVE_HOURLY_CURVE_WITH_ARRIVAL",
            "required_evidence": "same-capture official-native publication/arrival receipt at or before P0/P1 cut-off; archive-query time is not arrival",
            "automatic": False,
            "retained_archive_curve_station_day_count": int(
                retained_hourly_curves["archive_query_after_station_day_count"]
            ),
        })
    if action == "READY_BUILD_CANDIDATE_ONLY_ARTIFACT":
        repair_steps.append(dict(BLOCKER_ACTIONS["trajectory_artifact_missing"], blocker="trajectory_artifact_missing"))
    pointer_usable = bool(pointer is not None and not blockers)
    return {
        "station_id": station,
        "local_time_bucket": bucket,
        "scope_key": scope_key,
        "settlement_timezone": str(scope.get("settlement_timezone") or ""),
        "source_counts": scope.get("source_counts") or {},
        "source_manifest": {
            "content_sha256": source_manifest_sha,
            "eligible_fold_count": int(source_manifest.get("eligible_fold_count") or 0),
            "v3_eligible_fold_count": int(source_manifest.get("v3_eligible_fold_count") or 0),
            "v3_incomplete_future_curve_fold_count": int(source_manifest.get("v3_incomplete_future_curve_fold_count") or 0),
        },
        "hourly_curve_receipts": retained_hourly_curves,
        "correction_review": scope.get("correction_review") or {},
        "training": scope.get("training") or {},
        "formal_eligible_from_audit": bool(scope.get("formal_eligible")),
        "blockers": blockers,
        "repair_steps": repair_steps,
        "existing_evidence": {
            "schema_missing": bool(evidence["schema_missing"]),
            "candidate_artifact_count": len(candidate_rows),
            "formal_artifact_count": len(formal_rows),
            "activation_count": len(evidence["activation_rows"]),
        },
        "action": action,
        "state": state,
        "artifact_pointer": pointer,
        "artifact_pointer_usable": pointer_usable,
        "boundary": {
            "database_mode": "read_only_query_only",
            "writes_target_database": False,
            "writes_canonical_database": False,
            "calls_network": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
            "formal_promoted_created": False,
        },
    }


def build_plan(
    *, audit_path: Path, database: Path, as_of_utc: str,
    hourly_curve_audit_path: Path | None = None,
) -> dict[str, Any]:
    as_of = _utc(as_of_utc, name="as_of_utc")
    audit = _load_audit(audit_path)
    # The audit's fold eligibility, correction state and source manifests are
    # all point-in-time assertions.  Looking up an artifact/activation at a
    # later (or earlier) instant would combine two different evidence cuts and
    # could emit a seemingly usable pointer that the audited OOS proof never
    # evaluated.  A repair plan therefore has one exact evidence boundary.
    audit_as_of = _utc(str(audit.get("as_of_utc") or ""), name="audit_as_of_utc")
    if as_of != audit_as_of:
        raise ContractError("p1_repair_as_of_must_match_audit")
    audit_sha = str(audit["content_sha256"])
    hourly_curve_audit = _load_hourly_curve_audit(hourly_curve_audit_path)
    hourly_curve_summary = _hourly_curve_by_station(hourly_curve_audit)
    assert_process_memory_budget(projected_bytes=MAX_JSON_OUTPUT_BYTES)
    with _open_db(database) as conn:
        scopes = [
            _scope_plan(
                scope,
                audit_sha=audit_sha,
                conn=conn,
                as_of_utc=as_of,
                hourly_curve_summary=hourly_curve_summary,
            )
            for scope in audit["scopes"]
        ]
    action_summary = Counter(str(item["action"]) for item in scopes)
    blocker_summary = Counter(code for item in scopes for code in item["blockers"])
    pointer_count = sum(item["artifact_pointer"] is not None for item in scopes)
    usable_candidate_pointer_count = sum(
        item["artifact_pointer"] is not None
        and item["artifact_pointer"].get("authority_scope") == NO_LIVE_CANDIDATE
        and bool(item.get("artifact_pointer_usable"))
        for item in scopes
    )
    identity = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "as_of_utc": as_of,
        "audit": str(audit_path.expanduser().resolve()),
        "audit_content_sha256": audit_sha,
        "database": str(database.expanduser().resolve()),
        "hourly_curve_receipt_audit": (
            None if hourly_curve_audit is None else {
                "path": str(hourly_curve_audit_path.expanduser().resolve()),
                "content_sha256": str(hourly_curve_audit["content_sha256"]),
                "summary": hourly_curve_audit.get("summary") or {},
            }
        ),
        "station_count": 45,
        "scope_count": len(scopes),
        "formal_eligible_scope_count": sum(bool(item["formal_eligible_from_audit"]) for item in scopes),
        "candidate_pointer_count": pointer_count,
        "usable_candidate_pointer_count": usable_candidate_pointer_count,
        "action_summary": dict(sorted(action_summary.items())),
        "blocker_summary": dict(sorted((key, int(value)) for key, value in blocker_summary.items())),
        "scopes": scopes,
        "safe_write_state": "NO_SAFE_FORMAL_WRITE_REQUIRED" if usable_candidate_pointer_count == 0 else "CANDIDATE_POINTERS_ONLY_NO_WRITE",
        "automatic_write_authorized": False,
        "repair_policy": {
            "candidate_scope": "PRODUCTION_NO_LIVE_CANDIDATE only; never infer FORMAL_PROMOTED",
            "future_curve": "reuse existing native P0/P0-M lineage; do not refetch or substitute an aggregator",
            "truth_and_revision": "wait for closed AWC truth and manually review COR/AMD before training",
            "sample_gate": "retain the strict 20-fold/effective-sample gate; no replay or threshold reduction",
            "pointer_use": "only immutable artifact rows with content SHA are usable; summary-only candidates remain blocked",
        },
        "boundary": {
            "database_mode": "read_only_query_only",
            "writes_target_database": False,
            "writes_canonical_database": False,
            "calls_network": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
            "formal_promoted_created": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=ROOT / "output/audits/p1_trajectory_45station_audit_20260802.json")
    parser.add_argument("--hourly-curve-audit", type=Path, default=DEFAULT_HOURLY_CURVE_AUDIT)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--as-of-utc",
        default=None,
        help="must exactly match the selected audit; omitted derives that audit's as_of_utc",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "output/audits/p1_trajectory_repair_plan_20260802.json")
    args = parser.parse_args()
    audit_path = args.audit.expanduser().resolve(strict=True)
    plan_as_of = args.as_of_utc
    if plan_as_of is None:
        plan_as_of = str(_load_audit(audit_path).get("as_of_utc") or "")
    plan = build_plan(
        audit_path=audit_path,
        database=args.db.expanduser().resolve(strict=True),
        as_of_utc=plan_as_of,
        hourly_curve_audit_path=args.hourly_curve_audit,
    )
    wrote = write_bounded_json_if_changed(args.output.expanduser().resolve(), plan)
    print(_json({
        "schema": SCHEMA,
        "content_sha256": plan["content_sha256"],
        "station_count": plan["station_count"],
        "scope_count": plan["scope_count"],
        "formal_eligible_scope_count": plan["formal_eligible_scope_count"],
        "candidate_pointer_count": plan["candidate_pointer_count"],
        "usable_candidate_pointer_count": plan["usable_candidate_pointer_count"],
        "action_summary": plan["action_summary"],
        "blocker_summary": plan["blocker_summary"],
        "safe_write_state": plan["safe_write_state"],
        "output": str(args.output.expanduser().resolve()),
        "wrote": wrote,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
