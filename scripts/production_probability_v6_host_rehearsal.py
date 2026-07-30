#!/usr/bin/env python3
"""Run the real v6 P0+canonical drain host on an in-memory production schema."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_v6_activation_contract import (
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    validate_v6_activation,
)
from joint_weak_live_evidence_runtime import DEFAULT_CURVE_ROOT
from production_probability_v6_canonical_event_drain_runtime import (
    drain_v6_canonical_events,
    ensure_v6_daily_p0,
    pending_v6_canonical_event_ids,
)
from scripts.production_probability_v6_zero_order_drill import ORDER_TABLES, _clone_schema, _counts
from scripts.production_probability_v6_peak_timing_acceptance import (
    audit_connection as audit_peak_timing_connection,
)


SCHEMA_VERSION = "production_probability_v6_host_rehearsal_v1"


def _copy_current_canonical(source_db: Path, target: sqlite3.Connection, target_identity: str) -> int:
    with sqlite3.connect(f"file:{source_db.resolve()}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        rows = source.execute(
            "SELECT * FROM canonical_observation_events WHERE target_identity=? AND state='accepted_primary' "
            "ORDER BY valid_time,revision_no,canonical_observation_event_id",
            (target_identity,),
        ).fetchall()
    for raw in rows:
        row = dict(raw)
        fields = list(row)
        target.execute(
            f"INSERT INTO canonical_observation_events({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            tuple(row[field] for field in fields),
        )
    target.commit()
    return len(rows)


def audit(root: Path, source_db: Path, event_date: str) -> dict[str, object]:
    activation = validate_v6_activation(
        json.loads((root / "config/production_probability_runtime_activation.v6_candidate.json").read_text(encoding="utf-8"))
    )
    target_identity = f"physical_weather_day_v1:ZUUU:{event_date[:10]}"
    conn = _clone_schema(source_db)
    try:
        canonical_count = _copy_current_canonical(source_db, conn, target_identity)
        if canonical_count == 0:
            raise ValueError("v6_host_rehearsal_canonical_sequence_empty")
        orders_before = _counts(conn)
        decision_as_of = datetime.now(ZoneInfo("UTC")).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        started = time.monotonic()
        p0 = ensure_v6_daily_p0(
            conn,
            activation=activation,
            event_date=event_date,
            decision_as_of=decision_as_of,
            curve_root=DEFAULT_CURVE_ROOT,
        )
        p0_elapsed_ms = round((time.monotonic() - started) * 1000.0, 3)
        if p0.get("ok") is not True:
            raise ValueError(
                "v6_host_rehearsal_p0_unavailable:"
                + json.dumps(p0, ensure_ascii=False, sort_keys=True, default=str)
            )
        drain_started = time.monotonic()
        drain = (
            drain_v6_canonical_events(
                conn,
                activation=activation,
                event_date=event_date,
                max_events=4096,
            )
            if p0.get("ok") is True
            else {"ok": False, "state": "blocked", "reason": "v6_p0_unavailable"}
        )
        drain_elapsed_ms = round((time.monotonic() - drain_started) * 1000.0, 3)
        next_day = (datetime.fromisoformat(event_date[:10]).date() + timedelta(days=1)).isoformat()
        next_p0_started = time.monotonic()
        next_day_p0 = ensure_v6_daily_p0(
            conn,
            activation=activation,
            event_date=next_day,
            decision_as_of=decision_as_of,
            curve_root=DEFAULT_CURVE_ROOT,
        )
        next_day_p0_elapsed_ms = round((time.monotonic() - next_p0_started) * 1000.0, 3)
        lineage = int(
            conn.execute(
                "SELECT COUNT(*) FROM production_initial_posterior_lineage WHERE target_identity=? "
                "AND activation_contract_sha256=?",
                (target_identity, activation["activation_contract_sha256"]),
            ).fetchone()[0]
        )
        generation_row = conn.execute(
            "SELECT p.replay_generation FROM production_initial_posterior_lineage l "
            "JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id "
            "WHERE l.target_identity=? AND l.activation_contract_sha256=?",
            (target_identity, activation["activation_contract_sha256"]),
        ).fetchone()
        generation = int(generation_row[0]) if generation_row else -1
        rehearsal_lineage_rows = conn.execute(
            "SELECT l.target_identity,l.activation_contract_sha256,l.posterior_checkpoint_id "
            "FROM production_initial_posterior_lineage l ORDER BY l.target_identity,l.created_at"
        ).fetchall()
        current_lineage_rows = [
            tuple(row)
            for row in rehearsal_lineage_rows
            if str(row[0]) == target_identity
            and str(row[1]) == activation["activation_contract_sha256"]
        ]
        if len(current_lineage_rows) != 1:
            raise ValueError(
                "v6_host_rehearsal_current_lineage_not_unique:"
                + json.dumps([tuple(row) for row in rehearsal_lineage_rows], sort_keys=True)
            )
        pending = len(
            pending_v6_canonical_event_ids(
                conn,
                activation=activation,
                event_date=event_date,
            )
        )
        conn.row_factory = sqlite3.Row
        latest = conn.execute(
            "SELECT * FROM posterior_checkpoints WHERE target_identity=? AND replay_generation=? "
            "AND state='committed' ORDER BY rowid DESC LIMIT 1",
            (target_identity, generation),
        ).fetchone()
        latest_value = dict(latest) if latest else {}
        latest_reported_bound = latest_value.get("reported_x_lower_bound_c")
        if activation.get("composer_schema") == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA:
            latest_final_locked_incompatible_mass = 0.0
        else:
            latest_final_locked_incompatible_mass = sum(
                float(row.get("probability") or 0.0)
                for row in json.loads(latest_value.get("joint_posterior_json") or "[]")
                if latest_reported_bound is not None
                and str(row.get("state") or "") == "final_locked"
                and int(math.floor(float(row.get("continuous_y_c")) + 0.5))
                != int(latest_reported_bound)
            )
        public = conn.execute(
            "SELECT h.composer_version,h.posterior_run_id,h.fact_json FROM event_fact_latest l "
            "JOIN event_fact_history h ON h.id=l.fact_id WHERE h.event_date=? ORDER BY h.id DESC LIMIT 1",
            (event_date[:10],),
        ).fetchone()
        public_identity = {}
        if public:
            public_identity = (
                json.loads(public["fact_json"]).get("posterior_ssot_fact", {}).get("probability_runtime_identity", {})
            )
        orders_after = _counts(conn)
        peak_timing_acceptance = (
            {
                "schema": "production_probability_v6_peak_timing_acceptance_v1",
                "ok": True,
                "state": "not_applicable_identifiable_physical_state_space",
                "does_not_update_physical_x": True,
                "places_orders": False,
                "read_only": True,
            }
            if activation.get("composer_schema") == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
            else audit_peak_timing_connection(conn, event_date=event_date)
        )
        gates = {
            "real_current_canonical_sequence_nonempty": canonical_count > 0,
            "daily_p0_committed": p0.get("ok") is True,
            "next_day_p0_can_be_prepared_off_hot_path": next_day_p0.get("ok") is True,
            "all_current_canonical_events_drained": drain.get("ok") is True and pending == 0,
            "each_probability_transaction_under_3000ms": float(
                drain.get("max_probability_transaction_elapsed_ms") or 0.0
            ) < 3000.0,
            "one_activation_bound_p0_lineage": lineage == 1,
            "latest_checkpoint_is_v6": latest_value.get("checkpoint_probability_schema") == activation["checkpoint_schema"],
            "latest_final_locked_cells_match_observed_reported_x": (
                latest_final_locked_incompatible_mass <= 1e-12
            ),
            "public_ssot_is_v6": bool(
                public
                and public["composer_version"]
                == (
                    "identifiable_physical_state_bayes_production"
                    if activation.get("composer_schema") == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
                    else "physical_state_bayes_v6_production"
                )
                and public["posterior_run_id"] == latest_value.get("posterior_run_id")
                and public_identity.get("activation_contract_sha256") == activation["activation_contract_sha256"]
            ),
            "no_order_or_outbox_rows_created": orders_before == orders_after == {table: 0 for table in ORDER_TABLES},
            "public_peak_timing_bound_to_same_event_update": peak_timing_acceptance.get("ok") is True,
        }
        return {
            "schema": SCHEMA_VERSION,
            "ok": all(gates.values()),
            "state": "production_host_rehearsal_accepted" if all(gates.values()) else "rejected",
            "gates": gates,
            "canonical_event_count": canonical_count,
            "pending_canonical_event_count": pending,
            "activation_contract_sha256": activation["activation_contract_sha256"],
            "runtime_manifest_sha256": activation["runtime_manifest_sha256"],
            "transition_artifact_sha256": activation.get("transition_artifact_sha256", ""),
            "observation_artifact_sha256": activation.get("observation_artifact_sha256", ""),
            "physical_state_model_artifact_sha256": activation.get("physical_state_model_artifact_sha256", ""),
            "physical_observation_artifact_sha256": activation.get("physical_observation_artifact_sha256", ""),
            "physical_reporting_operator_sha256": activation.get("physical_reporting_operator_sha256", ""),
            "latest_checkpoint_id": latest_value.get("posterior_checkpoint_id"),
            "latest_reported_x_lower_bound_c": latest_value.get("reported_x_lower_bound_c"),
            "latest_final_locked_reported_x_incompatible_mass": (
                latest_final_locked_incompatible_mass
            ),
            "p0_elapsed_ms": p0_elapsed_ms,
            "next_day_p0_elapsed_ms": next_day_p0_elapsed_ms,
            "next_day_p0_state": next_day_p0.get("state"),
            "canonical_drain_elapsed_ms": drain_elapsed_ms,
            "max_probability_transaction_elapsed_ms": drain.get("max_probability_transaction_elapsed_ms"),
            "p95_probability_transaction_elapsed_ms": drain.get("p95_probability_transaction_elapsed_ms"),
            "peak_timing_acceptance": peak_timing_acceptance,
            "uses_in_memory_clone_of_production_schema": True,
            "reads_live_archive_db": True,
            "writes_live_archive_db": False,
            "places_orders": False,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--event-date", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
    parser.add_argument("--output", type=Path, default=ROOT / "output/audits/physical_state_bayes_v6_host_rehearsal.json")
    args = parser.parse_args()
    result = audit(ROOT, args.db, args.event_date)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
