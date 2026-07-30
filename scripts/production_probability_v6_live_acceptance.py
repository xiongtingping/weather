#!/usr/bin/env python3
"""Read-only acceptance of the active production v6 epoch and current SSOT."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from physical_state_bayes_v6.schema_contract import verify_schema
from production_probability_v6_activation_contract import (
    CHECKPOINT_SCHEMA,
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    validate_v6_activation,
)
from production_probability_v6_canonical_event_drain_runtime import pending_v6_canonical_event_ids
from production_probability_v6_live_route_contract import audit_v6_live_routes
from production_probability_v6_runtime_manifest import v6_runtime_manifest_sha256
from joint_weak_live_evidence_runtime import PRODUCTION_FORECAST_SOURCE_IDS
from independent_weak_live_source_runtime import load_live_independent_weak_source_observations


SCHEMA_VERSION = "production_probability_v6_live_acceptance_v1"


def _instant(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _weak_source_id(value: object) -> str:
    source = str(value or "").strip().lower()
    return "taf" if source == "zuuu_taf_forecast_amsc" else source


def _audit_weak_source_freshness(
    conn: sqlite3.Connection,
    *,
    checkpoint: dict[str, object],
    event_date: str,
    independent_observations: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    # Freshness is an acceptance property of the production weak-source set,
    # not of every forecast artifact present in the archive.  GFS remains
    # available for diagnostics/research but is intentionally excluded from
    # PRODUCTION_FORECAST_SOURCE_IDS and must not block the basket posterior.
    production_sources = set(PRODUCTION_FORECAST_SOURCE_IDS)
    snapshot_id = str(checkpoint.get("forecast_evidence_snapshot_id") or "")
    snapshot = conn.execute(
        "SELECT decision_as_of,source_revisions_json FROM weak_forecast_evidence_snapshots "
        "WHERE forecast_evidence_snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if snapshot is None:
        return {"ok": False, "state": "snapshot_missing", "stale_sources": []}
    try:
        consumed_rows = json.loads(snapshot[1] or "[]")
    except (TypeError, json.JSONDecodeError):
        return {"ok": False, "state": "snapshot_source_revisions_invalid", "stale_sources": []}
    consumed = {
        _weak_source_id(row.get("source_id")): row
        for row in consumed_rows
        if isinstance(row, dict) and _weak_source_id(row.get("source_id"))
    }
    latest: dict[str, dict[str, object]] = {}

    def add_candidate(row: dict[str, object]) -> None:
        source = _weak_source_id(row.get("source_id"))
        available = _instant(row.get("available_at") or row.get("fetched_at"))
        if source not in production_sources or available is None:
            return
        previous = latest.get(source)
        if previous is None or available > _instant(previous.get("available_at")):
            latest[source] = {**row, "source_id": source, "available_at": available.isoformat().replace("+00:00", "Z")}

    for row in conn.execute(
        "SELECT source_id,source_event_id,fetched_at FROM source_observations "
        "WHERE event_date=? AND source_class='forecast_weak_source'",
        (event_date[:10],),
    ).fetchall():
        add_candidate({"source_id": row[0], "source_revision_id": row[1], "fetched_at": row[2]})
    external = independent_observations
    if external is None:
        loaded = load_live_independent_weak_source_observations(event_date=event_date[:10])
        external = [dict(row) for row in loaded.get("source_observations") or [] if isinstance(row, dict)]
    for row in external:
        add_candidate(row)

    snapshot_decision_at = _instant(snapshot[0])
    temporal_violations = []
    if snapshot_decision_at is None:
        temporal_violations.append("weak_snapshot_decision_as_of_invalid")
    for source, row in sorted(consumed.items()):
        available_at = _instant(row.get("available_at"))
        if available_at is None:
            temporal_violations.append(f"consumed_source_available_at_invalid:{source}")
        elif snapshot_decision_at is not None and available_at > snapshot_decision_at:
            temporal_violations.append(f"consumed_source_after_snapshot_decision_as_of:{source}")

    stale = []
    for source, candidate in sorted(latest.items()):
        prior = consumed.get(source)
        prior_at = _instant(prior.get("available_at")) if isinstance(prior, dict) else None
        candidate_at = _instant(candidate.get("available_at"))
        if prior_at is None or (candidate_at is not None and candidate_at > prior_at):
            stale.append(
                {
                    "source_id": source,
                    "snapshot_available_at": prior_at.isoformat().replace("+00:00", "Z") if prior_at else "",
                    "latest_available_at": candidate.get("available_at"),
                    "latest_source_revision_id": candidate.get("source_revision_id") or candidate.get("source_event_id"),
                }
            )
    latest_source_times = [
        parsed
        for row in latest.values()
        if (parsed := _instant(row.get("available_at"))) is not None
    ]
    latest_consumed_times = [
        parsed
        for row in consumed.values()
        if (parsed := _instant(row.get("available_at"))) is not None
    ]
    latest_source_at = max(latest_source_times, default=None)
    latest_consumed_at = max(latest_consumed_times, default=None)
    ok = not stale and not temporal_violations
    state = (
        "current"
        if ok
        else "snapshot_asof_order_invalid"
        if temporal_violations
        else "stale_newer_revision_not_consumed"
    )
    return {
        "ok": ok,
        "state": state,
        "snapshot_id": snapshot_id,
        "snapshot_decision_as_of": snapshot[0],
        "checked_source_count": len(latest),
        "latest_source_time": latest_source_at.isoformat().replace("+00:00", "Z") if latest_source_at else "",
        "latest_consumed_source_time": latest_consumed_at.isoformat().replace("+00:00", "Z") if latest_consumed_at else "",
        "stale_sources": stale,
        "temporal_violations": temporal_violations,
    }


def _audit_latest_source_time_bound(
    conn: sqlite3.Connection,
    *,
    checkpoint: dict[str, object],
    target_identity: str,
    weak_source_freshness: dict[str, object],
) -> dict[str, object]:
    """Prove every latest production source existed by posterior commit time."""

    posterior_text = str(checkpoint.get("committed_at") or "")
    posterior_at = _instant(posterior_text)
    source_times: list[tuple[str, str, datetime]] = []
    violations: list[str] = []

    weak_text = str(weak_source_freshness.get("latest_source_time") or "")
    if weak_text:
        weak_at = _instant(weak_text)
        if weak_at is None:
            violations.append("latest_weak_source_time_invalid")
        else:
            source_times.append(("forecast_weak_source", weak_text, weak_at))

    for row in conn.execute(
        "SELECT canonical_observation_event_id,first_actionable_seen_at "
        "FROM canonical_observation_events WHERE target_identity=? AND state='accepted_primary'",
        (target_identity,),
    ).fetchall():
        source_text = str(row[1] or "")
        source_at = _instant(source_text)
        if source_at is None:
            violations.append(f"canonical_source_time_invalid:{row[0]}")
        else:
            source_times.append((f"canonical:{row[0]}", source_text, source_at))

    if posterior_at is None:
        violations.append("posterior_snapshot_time_invalid")
    if not source_times:
        violations.append("latest_source_time_missing")
        latest_kind = ""
        latest_text = ""
        latest_at = None
    else:
        latest_kind, latest_text, latest_at = max(source_times, key=lambda item: item[2])
        if posterior_at is not None and latest_at > posterior_at:
            violations.append("latest_source_time_after_posterior_snapshot_time")

    return {
        "ok": not violations,
        "state": "source_time_bound" if not violations else "source_time_bound_violated",
        "condition": "latest_source_time <= posterior_snapshot_time",
        "latest_source_kind": latest_kind,
        "latest_source_time": latest_text,
        "posterior_snapshot_time": posterior_text,
        "delta_ms": (
            round((posterior_at - latest_at).total_seconds() * 1000.0, 3)
            if posterior_at is not None and latest_at is not None
            else None
        ),
        "checked_source_time_count": len(source_times),
        "violations": violations,
    }


def audit(db: Path, activation_path: Path, event_date: str) -> dict[str, object]:
    blockers: list[str] = []
    try:
        activation = validate_v6_activation(json.loads(activation_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        activation = {}
        blockers.append(f"active_v6_activation_invalid:{type(exc).__name__}")
    runtime_matches = bool(
        activation
        and activation.get("runtime_manifest_sha256") == v6_runtime_manifest_sha256()
    )
    if activation and not runtime_matches:
        blockers.append("active_v6_runtime_manifest_drift")
    routes = audit_v6_live_routes(ROOT)
    if routes.get("ok") is not True:
        blockers.append("active_v6_live_routes_invalid")
    target = f"physical_weather_day_v1:ZUUU:{event_date[:10]}"
    evidence: dict[str, object] = {}
    try:
        with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            schema = verify_schema(conn)
            if schema.get("ok") is not True:
                blockers.append("active_v6_schema_invalid")
            lineage = conn.execute(
                "SELECT p.replay_generation FROM production_initial_posterior_lineage l "
                "JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id "
                "WHERE l.target_identity=? AND l.activation_contract_sha256=?",
                (target, str(activation.get("activation_contract_sha256") or "")),
            ).fetchall()
            if len(lineage) != 1:
                blockers.append("active_v6_p0_lineage_not_unique")
                generation = -1
            else:
                generation = int(lineage[0][0] or 0)
            checkpoint = conn.execute(
                "SELECT * FROM posterior_checkpoints WHERE target_identity=? AND replay_generation=? "
                "AND state='committed' ORDER BY rowid DESC LIMIT 1",
                (target, generation),
            ).fetchone()
            if checkpoint is None:
                blockers.append("active_v6_checkpoint_missing")
                checkpoint_value = {}
            else:
                checkpoint_value = dict(checkpoint)
                if checkpoint_value.get("checkpoint_probability_schema") != CHECKPOINT_SCHEMA:
                    blockers.append("active_v6_checkpoint_schema_mismatch")
            weak_source_freshness = (
                _audit_weak_source_freshness(
                    conn,
                    checkpoint=checkpoint_value,
                    event_date=event_date,
                )
                if checkpoint_value
                else {"ok": False, "state": "checkpoint_missing", "stale_sources": []}
            )
            if weak_source_freshness.get("ok") is not True:
                blockers.append("active_v6_weak_source_snapshot_stale")
            source_time_freshness = (
                _audit_latest_source_time_bound(
                    conn,
                    checkpoint=checkpoint_value,
                    target_identity=target,
                    weak_source_freshness=weak_source_freshness,
                )
                if checkpoint_value
                else {
                    "ok": False,
                    "state": "checkpoint_missing",
                    "condition": "latest_source_time <= posterior_snapshot_time",
                    "violations": ["posterior_checkpoint_missing"],
                }
            )
            if source_time_freshness.get("ok") is not True:
                blockers.append("active_v6_source_time_freshness_invalid")
            reported_bound = checkpoint_value.get("reported_x_lower_bound_c")
            raw_joint_posterior = json.loads(
                checkpoint_value.get("joint_posterior_json") or "[]"
            )
            joint_rows = (
                raw_joint_posterior
                if isinstance(raw_joint_posterior, list)
                else list(raw_joint_posterior.values())
                if isinstance(raw_joint_posterior, dict)
                else []
            )
            final_locked_incompatible_mass = sum(
                float(row.get("probability") or 0.0)
                for row in joint_rows
                if isinstance(row, dict)
                if reported_bound is not None
                and str(row.get("state") or "") == "final_locked"
                and int(math.floor(float(row.get("continuous_y_c")) + 0.5))
                != int(reported_bound)
            )
            if runtime_matches and final_locked_incompatible_mass > 1e-12:
                blockers.append("active_v6_final_locked_reported_x_incompatible_mass")
            pending = (
                len(
                    pending_v6_canonical_event_ids(
                        conn,
                        activation=activation,
                        event_date=event_date,
                    )
                )
                if generation >= 0
                else -1
            )
            if pending != 0:
                blockers.append("active_v6_canonical_events_pending")
            slug = f"highest-temperature-in-chengdu-on-{datetime.fromisoformat(event_date[:10]).strftime('%B').lower()}-{int(event_date[8:10])}-{event_date[:4]}"
            public = conn.execute(
                "SELECT h.fact_json,h.composer_version,h.posterior_run_id FROM event_fact_latest l "
                "JOIN event_fact_history h ON h.id=l.fact_id WHERE l.slug=? AND l.event_date=?",
                (slug, event_date[:10]),
            ).fetchone()
            if public is None:
                blockers.append("active_v6_public_ssot_missing")
                public_identity = {}
            else:
                try:
                    payload = json.loads(public[0])
                    ssot = payload.get("posterior_ssot_fact") or {}
                    public_identity = ssot.get("probability_runtime_identity") or {}
                except (TypeError, json.JSONDecodeError):
                    public_identity = {}
                expected_composer = (
                    "identifiable_physical_state_bayes_production"
                    if activation.get("composer_schema") == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
                    else "physical_state_bayes_v6_production"
                )
                if public[1] != expected_composer:
                    blockers.append("active_v6_public_composer_mismatch")
                if public_identity.get("activation_contract_sha256") != activation.get("activation_contract_sha256"):
                    blockers.append("active_v6_public_activation_mismatch")
                if checkpoint_value and public[2] != checkpoint_value.get("posterior_run_id"):
                    blockers.append("active_v6_public_checkpoint_not_latest")
            evidence = {
                "target_identity": target,
                "replay_generation": generation,
                "latest_checkpoint_id": checkpoint_value.get("posterior_checkpoint_id"),
                "pending_canonical_event_count": pending,
                "final_locked_reported_x_incompatible_mass": final_locked_incompatible_mass,
                "public_probability_runtime_identity": public_identity,
                "weak_source_freshness": weak_source_freshness,
                "source_time_freshness": source_time_freshness,
            }
    except sqlite3.Error as exc:
        blockers.append(f"active_v6_db_audit_failed:{type(exc).__name__}")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "production_v6_live_accepted" if not blockers else "blocked",
        "blockers": blockers,
        "evidence": evidence,
        "live_routes": routes,
        "v5_fallback": False,
        "read_only": True,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--activation", type=Path, default=ROOT / "config/production_probability_runtime_activation.json")
    parser.add_argument("--event-date", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
    args = parser.parse_args()
    result = audit(args.db, args.activation, args.event_date)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
