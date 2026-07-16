#!/usr/bin/env python3
"""Read-only acceptance for one fully replayed reported-X probability epoch."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_successor_activation_contract import (  # noqa: E402
    PUBLIC_SSOT_SCHEMA,
    TARGET_TYPE,
    load_successor_activation,
)
from production_probability_p0_runtime import build_zuuu_physical_public_slug  # noqa: E402
from production_probability_successor_runtime_manifest import (  # noqa: E402
    successor_runtime_manifest_sha256,
)
from reported_x_successor_checkpoint_runtime import validate_successor_checkpoint_row  # noqa: E402
from reported_x_successor_schema_contract import verify_schema  # noqa: E402


SCHEMA_VERSION = "production_reported_x_successor_acceptance_v1"
ZERO_DELTA_TABLES = (
    "posterior_commit_outbox",
    "posterior_execution_intents",
    "auto_trade_decisions",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)


def audit(*, db: Path, baseline_db: Path | None, activation_path: Path, event_date: str) -> dict[str, Any]:
    activation = load_successor_activation(activation_path)
    day = _text(event_date)[:10]
    target = f"physical_weather_day_v1:ZUUU:{day}"
    blockers: list[str] = []
    evidence: dict[str, Any] = {}
    if _text(activation.get("runtime_manifest_sha256")) != successor_runtime_manifest_sha256():
        blockers.append("successor_runtime_manifest_drift")
    with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        schema = verify_schema(conn)
        if schema.get("ok") is not True:
            blockers.append("reported_x_schema_unavailable")
        lineages = conn.execute(
            """SELECT p.replay_generation FROM production_initial_posterior_lineage l
               JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id
               WHERE l.target_identity=? AND l.activation_contract_sha256=?""",
            (target, _text(activation.get("activation_contract_sha256"))),
        ).fetchall()
        if len(lineages) != 1:
            blockers.append("successor_p0_lineage_not_unique")
            generation = -1
        else:
            generation = int(lineages[0][0] or 0)
        checkpoints = conn.execute(
            """SELECT * FROM posterior_checkpoints WHERE target_identity=?
               AND replay_generation=? AND state='committed' ORDER BY rowid""",
            (target, generation),
        ).fetchall() if generation >= 0 else []
        invalid = []
        for row in checkpoints:
            result = validate_successor_checkpoint_row(dict(row))
            if result.get("ok") is not True:
                invalid.append({"posterior_checkpoint_id": row["posterior_checkpoint_id"], "issues": result.get("issues") or []})
        if invalid:
            blockers.append("successor_checkpoint_validation_failed")
        if _text(activation.get("strong_update_schema")) == "strong_observation_update_v5_cumulative_replacement_speci_soft":
            checkpoint_by_id = {row["posterior_checkpoint_id"]: row for row in checkpoints}
            for checkpoint in checkpoints:
                update_id = _text(checkpoint["last_strong_update_id"])
                if not update_id:
                    continue
                update = conn.execute(
                    "SELECT * FROM strong_observation_updates WHERE strong_observation_update_id=?",
                    (update_id,),
                ).fetchone()
                if update is None:
                    invalid.append({"posterior_checkpoint_id": checkpoint["posterior_checkpoint_id"], "issues": ["strong_update_missing"]})
                    continue
                auxiliary = _mapping(json.loads(_text(update["path_auxiliary_json"]) or "{}"))
                identity = _mapping(auxiliary.get("strong_likelihood_update_identity"))
                issues = []
                if _text(identity.get("schema")) != "strong_likelihood_cumulative_replacement_identity_v1":
                    issues.append("replacement_identity_schema_invalid")
                if _text(identity.get("likelihood_update_kind")) != "cumulative_evidence_replacement_delta_log_lr_v1":
                    issues.append("replacement_update_kind_invalid")
                if _text(identity.get("current_cumulative_raw_log_score_sha256")) != _text(update["raw_log_score_sha256"]):
                    issues.append("current_cumulative_score_sha_mismatch")
                if _text(identity.get("previous_checkpoint_id")) != _text(update["previous_checkpoint_id"]):
                    issues.append("previous_checkpoint_identity_mismatch")
                current_rows = json.loads(_text(update["raw_log_score_json"]) or "[]")
                previous_checkpoint = checkpoint_by_id.get(_text(update["previous_checkpoint_id"]))
                previous_rows = []
                previous_sha = ""
                if previous_checkpoint is not None and _text(previous_checkpoint["last_strong_update_id"]):
                    previous_update = conn.execute(
                        "SELECT raw_log_score_json,raw_log_score_sha256 FROM strong_observation_updates WHERE strong_observation_update_id=?",
                        (_text(previous_checkpoint["last_strong_update_id"]),),
                    ).fetchone()
                    if previous_update is None:
                        issues.append("previous_cumulative_score_missing")
                    else:
                        previous_rows = json.loads(_text(previous_update["raw_log_score_json"]) or "[]")
                        previous_sha = _text(previous_update["raw_log_score_sha256"])
                if _text(identity.get("previous_cumulative_raw_log_score_sha256")) != previous_sha:
                    issues.append("previous_cumulative_score_sha_mismatch")
                previous_by_cell = {
                    (float(row["temperature_c"]), _text(row.get("pre_state"))): float(row["raw_log_score"])
                    for row in previous_rows
                }
                innovation = []
                innovation_by_cell = {}
                for row in current_rows:
                    key = (float(row["temperature_c"]), _text(row.get("pre_state")))
                    score = float(row["raw_log_score"]) - previous_by_cell.get(key, 0.0)
                    innovation.append({"temperature_c": float(row["temperature_c"]), "pre_state": key[1], "log_lr": score})
                    innovation_by_cell[key] = score
                if _text(identity.get("conditional_innovation_log_score_sha256")) != _sha(innovation):
                    issues.append("conditional_innovation_score_sha_mismatch")
                normalizer = float(update["normalizer_offset"])
                for delta in json.loads(_text(update["posterior_delta_json"]) or "[]"):
                    if delta.get("hard_condition_allowed") is not True:
                        if delta.get("posterior_delta") is not None:
                            issues.append("hard_mask_delta_not_null")
                        continue
                    key = (float(delta["temperature_c"]), _text(delta.get("pre_state")))
                    expected = innovation_by_cell.get(key)
                    if expected is None or abs(float(delta["posterior_delta"]) - (expected + normalizer)) > 1e-9:
                        issues.append("posterior_delta_not_from_conditional_innovation")
                        break
                if issues:
                    invalid.append({"posterior_checkpoint_id": checkpoint["posterior_checkpoint_id"], "issues": sorted(set(issues))})
            if invalid:
                blockers.append("successor_cumulative_replacement_audit_failed")
        canonical = conn.execute(
            """SELECT canonical_observation_event_id,report_type,valid_time
               FROM canonical_observation_events WHERE target_identity=?
                 AND station_id='ZUUU' AND selected_source_id='zuuu_metar_raw_amsc'
                 AND state='accepted_primary'
               ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id""",
            (target,),
        ).fetchall()
        consumed = []
        if checkpoints:
            consumed = json.loads(_text(checkpoints[-1]["consumed_event_ids_json"]) or "[]")
        canonical_ids = [row["canonical_observation_event_id"] for row in canonical]
        if consumed != canonical_ids:
            blockers.append("successor_canonical_replay_incomplete_or_out_of_order")
        latest = conn.execute(
            """SELECT h.fact_json,h.composer_version FROM event_fact_latest l
               JOIN event_fact_history h ON h.id=l.fact_id
               WHERE l.slug=? AND l.event_date=?""",
            (build_zuuu_physical_public_slug(day), day),
        ).fetchone()
        ssot: Mapping[str, Any] = {}
        if latest is None:
            blockers.append("successor_public_ssot_missing")
        else:
            try:
                ssot = _mapping(json.loads(_text(latest["fact_json"])).get("posterior_ssot_fact"))
            except (TypeError, ValueError):
                blockers.append("successor_public_ssot_json_invalid")
            distribution = _mapping(ssot.get("reported_physical_x_distribution"))
            try:
                normalized = abs(sum(float(value) for value in distribution.values()) - 1.0) <= 1e-8
                integer_support = bool(distribution) and all(float(key).is_integer() for key in distribution)
            except (TypeError, ValueError):
                normalized = False
                integer_support = False
            if (
                _text(ssot.get("schema")) != PUBLIC_SSOT_SCHEMA
                or _text(ssot.get("probability_target_type")) != TARGET_TYPE
                or _text(latest["composer_version"]) != "sequential_bayes_v3_reported_x_production"
                or not normalized
                or not integer_support
                or "physical_high_distribution" in ssot
                or "latent_scheduled_slot_high_distribution" in ssot
                or ssot.get("latent_probability_public") is not False
            ):
                blockers.append("successor_public_ssot_contract_invalid")
        speci_checks = []
        running_regular_max: int | None = None
        by_event = {row["last_canonical_event_id"]: row for row in checkpoints if _text(row["last_canonical_event_id"])}
        for event in canonical:
            checkpoint = by_event.get(event["canonical_observation_event_id"])
            if checkpoint is None:
                continue
            parsed = json.loads(_text(conn.execute(
                "SELECT parsed_observation_json FROM canonical_observation_events WHERE canonical_observation_event_id=?",
                (event["canonical_observation_event_id"],),
            ).fetchone()[0]))
            if event["report_type"] == "METAR":
                reported = int(round(float(parsed["temperature_c"])))
                running_regular_max = reported if running_regular_max is None else max(running_regular_max, reported)
            else:
                ok = checkpoint["reported_x_lower_bound_c"] == running_regular_max
                speci_checks.append({"canonical_observation_event_id": event["canonical_observation_event_id"], "ok": ok})
                if not ok:
                    blockers.append("speci_mutated_reported_x_hard_bound")
        latest_checkpoint_lower = (
            checkpoints[-1]["reported_x_lower_bound_c"] if checkpoints else None
        )
        latest_ssot_lower = ssot.get("reported_x_lower_bound_c")
        if running_regular_max is not None:
            if latest_checkpoint_lower != running_regular_max:
                blockers.append("successor_checkpoint_reported_x_lower_bound_not_regular_metar_max")
            if latest_ssot_lower != running_regular_max:
                blockers.append("successor_public_ssot_reported_x_lower_bound_not_regular_metar_max")
        evidence.update(
            {
                "replay_generation": generation,
                "checkpoint_count": len(checkpoints),
                "canonical_event_count": len(canonical),
                "metar_count": sum(1 for row in canonical if row["report_type"] == "METAR"),
                "speci_count": sum(1 for row in canonical if row["report_type"] == "SPECI"),
                "invalid_checkpoints": invalid,
                "latest_reported_x_lower_bound_c": ssot.get("reported_x_lower_bound_c"),
                "latest_checkpoint_reported_x_lower_bound_c": latest_checkpoint_lower,
                "accepted_regular_metar_reported_max_c": running_regular_max,
                "reported_x_lower_bound_matches_accepted_regular_metar_max": bool(
                    running_regular_max is not None
                    and latest_checkpoint_lower == running_regular_max
                    and latest_ssot_lower == running_regular_max
                ),
                "latest_reported_x_distribution": dict(_mapping(ssot.get("reported_physical_x_distribution"))),
                "speci_hard_bound_checks": speci_checks,
            }
        )
        current_counts = {table: _count(conn, table) for table in ZERO_DELTA_TABLES}
    deltas: dict[str, int] = {}
    if baseline_db is not None:
        with sqlite3.connect(f"file:{baseline_db.resolve()}?mode=ro", uri=True) as baseline:
            deltas = {table: current_counts[table] - _count(baseline, table) for table in ZERO_DELTA_TABLES}
        if any(value != 0 for value in deltas.values()):
            blockers.append("successor_probability_epoch_created_execution_or_decision_rows")
    else:
        blockers.append("baseline_db_required_for_zero_order_proof")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "accepted" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "event_date": day,
        "activation_contract_sha256": _text(activation.get("activation_contract_sha256")),
        "runtime_manifest_sha256": _text(activation.get("runtime_manifest_sha256")),
        "db": str(db.resolve()),
        "baseline_db": str(baseline_db.resolve()) if baseline_db else "",
        "zero_order_table_deltas": deltas,
        "evidence": evidence,
        "read_only": True,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--baseline-db", required=True)
    parser.add_argument("--activation", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = audit(
        db=Path(args.db).expanduser(),
        baseline_db=Path(args.baseline_db).expanduser(),
        activation_path=Path(args.activation).expanduser(),
        event_date=args.event_date,
    )
    if args.summary:
        result = {key: result[key] for key in ("schema", "ok", "state", "blockers", "event_date", "activation_contract_sha256", "runtime_manifest_sha256", "zero_order_table_deltas", "evidence", "read_only", "places_orders")}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
