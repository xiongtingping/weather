#!/usr/bin/env python3
"""Read-only 45-station P1 trajectory/artifact readiness audit.

This command deliberately stops at the evidence boundary.  It reuses the
existing strict-as-of METAR trajectory fold builder for every canonical
station and local-time bucket, records the AWC correction/review state, and
checks whether a formally promoted trajectory activation exists.  It never
writes either database, never changes an artifact authority scope, and has no
market or order dependencies.

The report is useful before running the P1 materializer: a missing formal
artifact is separated from a missing AWC truth/prefix, an unreviewed COR/AMD,
an incomplete native arrival/future-curve lineage, and an insufficient OOS
sample.  Candidate packets are evidence only; no candidate is treated as
``FORMAL_PROMOTED``.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, SHARED_LIVE_DB  # noqa: E402
from bayes_basket_target.probability_evidence_store import (  # noqa: E402
    metar_trajectory_scope_key,
    resolve_active_probability_evidence_artifact,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402
from scripts.build_bayes_basket_target_metar_trajectory_training_packet import (  # noqa: E402
    BUCKET_START_HOUR,
    _read_observations,
    _read_probability_roots,
    _read_truth,
    build_training_packet,
)
from bayes_basket_target.metar_trajectory_training import (  # noqa: E402
    train_metar_trajectory_artifact,
)


SCHEMA = "bayes_basket_target_p1_trajectory_45station_audit_v1"
EXECUTION_MODE = "production_no_live"
MINIMUM_PRIOR_TRUTHS = 3
MINIMUM_EFFECTIVE_SAMPLE_N = 20.0
DEFAULT_SEASON_BIN = "JJA"
DEFAULT_MATURITY_BIN = "D0"
DEFAULT_SETTLEMENT_DEFINITION = "official_daily_tmax_c"
MAX_OUTPUT_BYTES = 8 * 1024 * 1024

# The names are intentionally stable because the unified OOS gap ledger uses
# the same blocker vocabulary.  Keep this list separate from human-language
# presentation so downstream audits can count it without parsing prose.
BLOCKER_CODES = (
    "data_missing",
    "arrival_lineage_missing",
    "truth_missing",
    "revision_unreviewed",
    "sample_insufficient",
    "trajectory_artifact_missing",
    "trajectory_oos_not_ready",
)


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("p1_trajectory_audit_as_of_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("p1_trajectory_audit_as_of_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _station_timezones(target_db: Path, stations: Sequence[str]) -> dict[str, str]:
    """Resolve one immutable market-registry timezone per canonical station."""

    requested = tuple(sorted({str(item).strip().upper() for item in stations if str(item).strip()}))
    result: dict[str, str] = {}
    with sqlite3.connect(f"file:{target_db.resolve()}?mode=ro", uri=True, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        for station in requested:
            row = conn.execute(
                "SELECT settlement_timezone FROM basket_target_market_registry "
                "WHERE station_id=? AND source_eligible=1 "
                "ORDER BY event_date DESC,refreshed_at DESC LIMIT 1",
                (station,),
            ).fetchone()
            if row is not None and str(row[0] or "").strip():
                result[station] = str(row[0]).strip()
    return result


def _correction_review_inventory(
    archive_db: Path,
    *,
    station_id: str,
    settlement_timezone: str,
    as_of_utc: str,
) -> dict[str, Any]:
    """Read AWC correction/review evidence without changing its state.

    A correction halts its own station day.  It must not be silently turned
    into an indefinite station-wide historical block: the strict fold builder
    already removes correction days before fitting.  Preserve the local event
    dates here so the audit can prove that no unreviewed day escaped into a
    candidate fold.  Runtime P1 still independently blocks an active halt on
    the current station day.
    """

    as_of = _stamp(_utc(as_of_utc))
    with sqlite3.connect(f"file:{archive_db.resolve()}?mode=ro", uri=True, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT observation_id,valid_at,correction_suspected "
            "FROM reported_trajectory_observations "
            "WHERE source_lane='awc' AND station_id=? "
            "AND julianday(canonical_committed_at)<=julianday(?) "
            "ORDER BY canonical_committed_at,observation_id",
            (station_id, as_of),
        ).fetchall()
        zone = ZoneInfo(settlement_timezone)
        correction_rows = [
            row for row in rows if int(row[2] or 0) == 1
        ]
        correction_ids = [str(row[0]) for row in correction_rows]
        correction_dates_by_id: dict[str, str] = {}
        unresolved_correction_ids: list[str] = []
        for row in correction_rows:
            observation_id = str(row[0])
            try:
                correction_dates_by_id[observation_id] = _utc(
                    str(row[1] or "")
                ).astimezone(zone).date().isoformat()
            except ContractError:
                # Do not guess a day from a malformed timestamp.  The
                # reachability check below will retain a hard blocker.
                unresolved_correction_ids.append(observation_id)
        review_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='reported_trajectory_station_halt_reviews'"
        ).fetchone() is not None
        reviewed_clear = 0
        reviewed_quarantine = 0
        unreviewed_ids: list[str] = []
        if review_table:
            for observation_id in correction_ids:
                review = conn.execute(
                    "SELECT decision FROM reported_trajectory_station_halt_reviews "
                    "WHERE source_lane='awc' AND station_id=? "
                    "AND triggering_observation_id=? "
                    "AND julianday(reviewed_at)<=julianday(?) "
                    "ORDER BY reviewed_at DESC,review_id DESC LIMIT 1",
                    (station_id, observation_id, as_of),
                ).fetchone()
                decision = str(review[0] if review is not None else "").upper()
                if decision == "CLEAR_REFERENCE_ONLY":
                    reviewed_clear += 1
                elif decision == "QUARANTINE_STATION_DAY":
                    reviewed_quarantine += 1
                else:
                    unreviewed_ids.append(observation_id)
        else:
            unreviewed_ids = list(correction_ids)
        active_halts = conn.execute(
            "SELECT station_id,event_date,triggering_observation_id,reason_code "
            "FROM reported_trajectory_station_halts "
            "WHERE source_lane='awc' AND station_id=? AND state='active' "
            "ORDER BY event_date,triggering_observation_id",
            (station_id,),
        ).fetchall()
    return {
        "correction_count": len(correction_ids),
        "reviewed_clear_count": reviewed_clear,
        "reviewed_quarantine_count": reviewed_quarantine,
        "unreviewed_correction_count": len(unreviewed_ids),
        "unreviewed_observation_ids": unreviewed_ids[:16],
        "unreviewed_event_dates": sorted({
            correction_dates_by_id[observation_id]
            for observation_id in unreviewed_ids
            if observation_id in correction_dates_by_id
        }),
        "unresolved_correction_observation_ids": sorted(set(
            unresolved_correction_ids
        ))[:16],
        "event_date_resolution_complete": not unresolved_correction_ids,
        "active_halt_count": len(active_halts),
        "active_halts": [dict(row) for row in active_halts[:16]],
        "review_schema_present": review_table,
    }


def _scope_correction_inventory(
    correction_inventory: Mapping[str, Any],
    *,
    folds: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind correction quarantine to the exact historical folds under audit.

    A correction can block an artifact only if its station-day survives into
    that artifact's training folds.  The fold builder excludes
    ``correction_suspected`` days, so this assertion turns a broad inventory
    into a precise, fail-closed reachability proof rather than relaxing the
    correction rule.
    """

    result = dict(correction_inventory)
    fold_dates = {
        str(fold.get("target_local_date") or "")
        for fold in folds
        if isinstance(fold, Mapping) and str(fold.get("target_local_date") or "")
    }
    correction_dates = {
        str(value)
        for value in result.get("unreviewed_event_dates", ())
        if str(value)
    }
    active_halt_dates = {
        str(item.get("event_date") or "")
        for item in result.get("active_halts", ())
        if isinstance(item, Mapping) and str(item.get("event_date") or "")
    }
    reachable = sorted(fold_dates & (correction_dates | active_halt_dates))
    result.update({
        "eligible_fold_event_date_count": len(fold_dates),
        "unreviewed_reachable_event_dates": reachable,
        "unreviewed_reachable_event_date_count": len(reachable),
        "unreviewed_excluded_event_dates": sorted(
            (correction_dates | active_halt_dates) - fold_dates
        ),
    })
    return result


def _artifact_state(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    local_time_bucket: str,
    as_of_utc: str,
) -> dict[str, Any]:
    scope_key = metar_trajectory_scope_key(
        source_lane="awc",
        season_bin=DEFAULT_SEASON_BIN,
        local_time_bucket=local_time_bucket,
        maturity_bin=DEFAULT_MATURITY_BIN,
        settlement_definition=DEFAULT_SETTLEMENT_DEFINITION,
    )
    states: dict[str, str] = {}
    for authority in ("FORMAL_PROMOTED", "PRODUCTION_NO_LIVE_CANDIDATE"):
        try:
            row = resolve_active_probability_evidence_artifact(
                conn,
                artifact_kind="METAR_TRAJECTORY_LIKELIHOOD",
                station_id=station_id,
                scope_key=scope_key,
                as_of_utc=as_of_utc,
                required_authority_scope=authority,
            )
        except (ContractError, sqlite3.Error) as exc:
            states[authority] = f"ERROR:{type(exc).__name__}:{exc}"
            continue
        states[authority] = (
            str(row.get("authority_scope") or authority) if row else "MISSING"
        )
    return {"scope_key": scope_key, "authority": states}


def classify_blockers(
    *,
    truth_row_count: int,
    observation_row_count: int,
    correction_inventory: Mapping[str, Any],
    source_manifest: Mapping[str, Any] | None,
    training_error: str = "",
    formal_artifact_state: str = "MISSING",
) -> list[str]:
    """Return deterministic, ledger-compatible blocker codes."""

    blockers: list[str] = []
    if truth_row_count <= 0:
        blockers.append("truth_missing")
    if observation_row_count <= 0:
        blockers.append("data_missing")
    # New audits explicitly prove correction-day reachability.  Keep the
    # conservative historical behaviour for callers that do not supply that
    # proof (including independently authored audit payloads).
    reachable_count = correction_inventory.get(
        "unreviewed_reachable_event_date_count"
    )
    correction_date_resolution_complete = bool(
        correction_inventory.get("event_date_resolution_complete", False)
    )
    if reachable_count is None:
        revision_blocked = (
            int(correction_inventory.get("unreviewed_correction_count") or 0) > 0
            or int(correction_inventory.get("active_halt_count") or 0) > 0
        )
    else:
        revision_blocked = (
            not correction_date_resolution_complete
            or int(reachable_count or 0) > 0
        )
    if revision_blocked:
        blockers.append("revision_unreviewed")
    manifest = source_manifest or {}
    eligible = int(manifest.get("eligible_fold_count") or 0)
    excluded_reasons = Counter(
        str(item.get("reason") or "")
        for item in (manifest.get("excluded") or ())
        if isinstance(item, Mapping)
    )
    # A complete historical table can still have no usable prefix at a
    # bucket boundary (for example the local night bucket).  Count that as a
    # data/prefix gap in this bucket, rather than hiding it behind the generic
    # sample gate.
    if eligible == 0 and excluded_reasons.get(
        "canonical_prefix_missing_at_bucket_start", 0
    ) > 0:
        blockers.append("data_missing")
    if eligible < int(MINIMUM_EFFECTIVE_SAMPLE_N):
        blockers.append("sample_insufficient")
    incomplete_curve = int(manifest.get("v3_incomplete_future_curve_fold_count") or 0)
    v3_eligible = int(manifest.get("v3_eligible_fold_count") or 0)
    if incomplete_curve > 0 or (eligible > 0 and v3_eligible < eligible):
        blockers.append("arrival_lineage_missing")
    if training_error:
        # The trainer's strict v3 errors are evidence failures, not reasons to
        # fall back to the legacy identity-LR replay path.
        blockers.append("trajectory_oos_not_ready")
    if formal_artifact_state == "MISSING":
        blockers.append("trajectory_artifact_missing")
    elif formal_artifact_state != "FORMAL_PROMOTED":
        blockers.append("trajectory_oos_not_ready")
    return list(dict.fromkeys(blockers))


def _audit_station_bucket(
    *,
    station_id: str,
    settlement_timezone: str,
    local_time_bucket: str,
    target_db: Path,
    archive_db: Path,
    as_of_utc: str,
    correction_inventory: Mapping[str, Any],
    target_conn: sqlite3.Connection,
) -> dict[str, Any]:
    truths = _read_truth(target_db, station_id=station_id)
    observations = _read_observations(archive_db, station_id=station_id)
    roots = _read_probability_roots(target_db, station_id=station_id)
    source_manifest: dict[str, Any] | None = None
    training_error = ""
    candidate_artifact: dict[str, Any] | None = None
    packet: Mapping[str, Any] | None = None
    try:
        packet = build_training_packet(
            truth_rows=truths,
            observation_rows=observations,
            probability_rows=roots,
            station_id=station_id,
            settlement_timezone=settlement_timezone,
            local_time_bucket=local_time_bucket,
            minimum_prior_truths=MINIMUM_PRIOR_TRUTHS,
            minimum_effective_sample_n=MINIMUM_EFFECTIVE_SAMPLE_N,
            decided_at_utc=as_of_utc,
            valid_from_utc=as_of_utc,
            valid_until_utc="2100-01-01T00:00:00Z",
            generation=1,
            anchor_to_first_report=True,
            require_source_poll_receipt=True,
        )
        source_manifest = dict(packet.get("source_manifest") or {})
        try:
            result = train_metar_trajectory_artifact(
                folds=packet.get("folds") or (),
                smoothing_candidates=packet.get("smoothing_candidates") or (),
                minimum_training_folds=int(packet.get("minimum_training_folds") or 0),
                minimum_effective_sample_n=float(packet.get("minimum_effective_sample_n") or 0),
                artifact_parameters=packet.get("artifact_parameters") or {},
            )
            artifact = result.get("artifact")
            evidence = result.get("training_evidence")
            if isinstance(artifact, Mapping):
                candidate_artifact = {
                    "artifact_id": str(artifact.get("artifact_id") or ""),
                    "promotion_state": str(artifact.get("promotion_state") or ""),
                    "activated": bool(artifact.get("activated")),
                    "sample_n": int(artifact.get("sample_n") or 0),
                    "effective_sample_n": float(artifact.get("effective_sample_n") or 0.0),
                    "oos_gate_passed": bool(
                        evidence.get("oos_gate_passed")
                        if isinstance(evidence, Mapping)
                        else False
                    ),
                    "formal_probability_oos_available": bool(
                        evidence.get("formal_probability_oos_available")
                        if isinstance(evidence, Mapping)
                        else False
                    ),
                }
        except (ContractError, ValueError, TypeError) as exc:
            training_error = f"{type(exc).__name__}:{exc}"
    except (ContractError, ValueError, TypeError) as exc:
        training_error = f"{type(exc).__name__}:{exc}"
    artifact = _artifact_state(
        target_conn,
        station_id=station_id,
        local_time_bucket=local_time_bucket,
        as_of_utc=as_of_utc,
    )
    formal_state = str(
        artifact.get("authority", {}).get("FORMAL_PROMOTED") or "MISSING"
    )
    if formal_state.startswith("ERROR:"):
        formal_state = "MISSING"
    scoped_correction_inventory = _scope_correction_inventory(
        correction_inventory,
        folds=(packet.get("folds") or ()) if isinstance(packet, Mapping) else (),
    )
    blockers = classify_blockers(
        truth_row_count=len(truths),
        observation_row_count=len(observations),
        correction_inventory=scoped_correction_inventory,
        source_manifest=source_manifest,
        training_error=training_error,
        formal_artifact_state=formal_state,
    )
    return {
        "station_id": station_id,
        "settlement_timezone": settlement_timezone,
        "local_time_bucket": local_time_bucket,
        "source_counts": {
            "truth_row_count": len(truths),
            "observation_row_count": len(observations),
            "probability_root_row_count": len(roots),
        },
        "correction_review": scoped_correction_inventory,
        "source_manifest": source_manifest or {
            "eligible_fold_count": 0,
            "v3_eligible_fold_count": 0,
            "v3_incomplete_future_curve_fold_count": 0,
        },
        "training": {
            "state": "CANDIDATE_BUILT" if candidate_artifact else "BLOCKED_EVIDENCE",
            "error": training_error,
            "candidate": candidate_artifact,
        },
        "artifact": artifact,
        "blockers": blockers,
        "formal_eligible": not blockers,
        "boundary": {
            "execution_mode": EXECUTION_MODE,
            "writes_target_database": False,
            "writes_canonical_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        },
    }


def audit_45station(
    *,
    target_db: Path = DEFAULT_TARGET_DB,
    archive_db: Path = SHARED_LIVE_DB,
    as_of_utc: str,
    station_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build one bounded, read-only report for all canonical stations."""

    as_of = _stamp(_utc(as_of_utc))
    canonical = tuple(coordinate_registry_manifest()["station_ids"])
    requested = tuple(sorted({str(item).strip().upper() for item in (station_ids or canonical) if str(item).strip()}))
    unknown = sorted(set(requested) - set(canonical))
    if unknown:
        raise ContractError("p1_trajectory_audit_station_not_canonical:" + ",".join(unknown))
    timezones = _station_timezones(target_db, requested)
    scopes: list[dict[str, Any]] = []
    with sqlite3.connect(f"file:{target_db.resolve()}?mode=ro", uri=True, timeout=30.0) as target_conn:
        target_conn.row_factory = sqlite3.Row
        for station in requested:
            timezone_name = timezones.get(station, "")
            if not timezone_name:
                scopes.append({
                    "station_id": station,
                    "settlement_timezone": "",
                    "state": "BLOCKED_EVIDENCE",
                    "blockers": ["data_missing"],
                    "reason": "market_registry_timezone_missing",
                    "boundary": {
                        "execution_mode": EXECUTION_MODE,
                        "writes_target_database": False,
                        "writes_canonical_database": False,
                        "calls_order_gateway": False,
                        "places_orders": False,
                        "orders_submitted": 0,
                        "live_submit_authorized": False,
                    },
                })
                continue
            correction = _correction_review_inventory(
                archive_db,
                station_id=station,
                settlement_timezone=timezone_name,
                as_of_utc=as_of,
            )
            for bucket in BUCKET_START_HOUR:
                scope = _audit_station_bucket(
                    station_id=station,
                    settlement_timezone=timezone_name,
                    local_time_bucket=bucket,
                    target_db=target_db,
                    archive_db=archive_db,
                    as_of_utc=as_of,
                    correction_inventory=correction,
                    target_conn=target_conn,
                )
                scope["state"] = "FORMAL_ELIGIBLE" if scope["formal_eligible"] else "BLOCKED_EVIDENCE"
                scopes.append(scope)
    blocker_counts = Counter(
        blocker for scope in scopes for blocker in (scope.get("blockers") or [])
    )
    result_identity = {
        "schema": SCHEMA,
        "execution_mode": EXECUTION_MODE,
        "as_of_utc": as_of,
        "station_scope": {
            "requested_station_ids": list(requested),
            "station_count": len(requested),
            "canonical_station_count": len(canonical),
            "canonical_registry_sha256": coordinate_registry_manifest()["registry_sha256"],
        },
        "minimums": {
            "minimum_prior_truths": MINIMUM_PRIOR_TRUTHS,
            "minimum_effective_sample_n": MINIMUM_EFFECTIVE_SAMPLE_N,
            "method_version": "canonical_conditional_hourly_future_path_v3",
        },
        "scopes": scopes,
        "scope_count": len(scopes),
        "formal_eligible_scope_count": sum(
            1 for scope in scopes if scope.get("state") == "FORMAL_ELIGIBLE"
        ),
        "blocked_scope_count": sum(
            1 for scope in scopes if scope.get("state") == "BLOCKED_EVIDENCE"
        ),
        "blocker_counts": {code: int(blocker_counts.get(code, 0)) for code in BLOCKER_CODES},
        "boundary": {
            "execution_mode": EXECUTION_MODE,
            "writes_target_database": False,
            "writes_canonical_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        },
    }
    return {**result_identity, "content_sha256": content_sha256(result_identity)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--archive-db", type=Path, default=SHARED_LIVE_DB)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = audit_45station(
        target_db=args.target_db.expanduser().resolve(),
        archive_db=args.archive_db.expanduser().resolve(),
        as_of_utc=args.as_of_utc,
        station_ids=args.station_id or None,
    )
    write_bounded_json(args.output.expanduser().resolve(), result, limit=MAX_OUTPUT_BYTES)
    print(result["content_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BLOCKER_CODES",
    "SCHEMA",
    "audit_45station",
    "classify_blockers",
]
