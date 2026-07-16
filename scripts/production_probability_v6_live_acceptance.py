#!/usr/bin/env python3
"""Read-only acceptance of the active production v6 epoch and current SSOT."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from physical_state_bayes_v6.schema_contract import verify_schema
from production_probability_v6_activation_contract import CHECKPOINT_SCHEMA, validate_v6_activation
from production_probability_v6_canonical_event_drain_runtime import pending_v6_canonical_event_ids
from production_probability_v6_live_route_contract import audit_v6_live_routes
from production_probability_v6_runtime_manifest import v6_runtime_manifest_sha256


SCHEMA_VERSION = "production_probability_v6_live_acceptance_v1"


def audit(db: Path, activation_path: Path, event_date: str) -> dict[str, object]:
    blockers: list[str] = []
    try:
        activation = validate_v6_activation(json.loads(activation_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        activation = {}
        blockers.append(f"active_v6_activation_invalid:{type(exc).__name__}")
    if activation and activation.get("runtime_manifest_sha256") != v6_runtime_manifest_sha256():
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
                if public[1] != "physical_state_bayes_v6_production":
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
                "public_probability_runtime_identity": public_identity,
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
