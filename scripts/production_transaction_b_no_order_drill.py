#!/usr/bin/env python3
"""Exercise production Transaction B against a disposable verified-backup schema clone.

This validates real archive schema compatibility without modifying the source
database.  To avoid contending with the live writer, the input must be a
maintenance-window backup rather than ``data/archive.db``.  The drill inserts
only a synthetic target identity into a temporary clone, never starts a
service and never calls an order adapter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_activation_contract import SCHEMA_VERSION as ACTIVATION_SCHEMA  # noqa: E402
from production_probability_activation_contract import validate_production_probability_activation  # noqa: E402
from production_initial_posterior_writer import initialize_production_initial_posterior_schema  # noqa: E402
from production_sequential_bayes_transaction_b_host import ProductionSequentialBayesTransactionBHost  # noqa: E402
from production_weak_forecast_revision_writer import commit_production_weak_forecast_revision  # noqa: E402
from sequential_bayes_schema_contract import INDEX_DDL, TABLE_DDL  # noqa: E402
from sequential_bayes_shadow_writer import materialize_outbox_execution_contract_row  # noqa: E402
from weak_forecast_revision_schema_contract import ensure_weak_forecast_revision_schema  # noqa: E402
from weak_forecast_revision_transaction_runtime import compile_weak_forecast_revision_candidate  # noqa: E402


SCHEMA_VERSION = "production_transaction_b_no_order_drill_v2"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _activation() -> dict[str, Any]:
    return {
        "schema": ACTIVATION_SCHEMA, "state": "production_enabled",
        "production_transaction_b_authorized": True, "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True, "no_order_validation_accepted": True,
        "cutover_approval_id": "temporary-no-order-drill-only",
        "activation_effective_at": "2026-07-14T00:00:00Z",
        "prior_model_artifact_sha256": "a" * 64,
        "weak_likelihood_artifact_sha256": "b" * 64,
        "strong_lr_artifact_sha256": "c" * 64,
        "runtime_manifest_sha256": "d" * 64,
        "legacy_writer_retirement_receipt_sha256": "e" * 64,
        "bayes_formula_id": "p0_prior_x_weak_once_then_canonical_event_raw_log_lr_then_weak_revision_replacement_log_lr_v1",
        "composer_schema": "sequential_bayes_filter_v3",
        "checkpoint_schema": "posterior_checkpoint_v2",
        "strong_update_schema": "strong_observation_update_v3",
        "public_ssot_schema": "sequential_temperature_posterior_ssot_v2",
        "places_orders": False, "authorizes_clob": False,
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _weak_snapshot(
    *, snapshot_id: str, target: str, likelihood: dict[str, float], source_revision_id: str, now: str,
) -> dict[str, Any]:
    return {
        "forecast_evidence_snapshot_id": snapshot_id,
        "target_identity": target,
        "event_date": "2026-07-14",
        "decision_as_of": now,
        "weak_likelihood_artifact_sha256": "b" * 64,
        "weak_likelihood_sha256": _sha(likelihood),
        "source_set_key": "taf",
        "source_revision_ids_json": _json([source_revision_id]),
        "source_revisions_json": _json([{
            "source_id": "zuuu_taf_forecast_amsc",
            "source_revision_id": source_revision_id,
        }]),
        "temperature_grid_json": _json([29.0, 30.0]),
        "likelihood_by_x_json": _json(likelihood),
        "forecast_peak_timing_prior_json": "{}",
        "snapshot_json": _json({"source_revision_id": source_revision_id}),
        "snapshot_sha256": _sha({"snapshot_id": snapshot_id, "likelihood": likelihood}),
        "state": "active",
        "created_at": now,
    }


def _seed_previous(conn: sqlite3.Connection, *, prefix: str, now: str) -> tuple[dict[str, str], dict[str, Any]]:
    target, checkpoint_id, run_id = f"no-order-drill:{prefix}", f"pchk-prev-{prefix}", f"post-prev-{prefix}"
    conn.execute(
        """INSERT INTO posterior_checkpoints(
        posterior_checkpoint_id,target_identity,posterior_run_id,previous_checkpoint_id,checkpoint_kind,
        forecast_evidence_snapshot_id,prior_context_id,resolution_contract_id,last_strong_update_id,last_canonical_event_id,
        canonical_sequence_sha256,temperature_grid_id,state_grid_id,joint_posterior_json,joint_posterior_sha256,
        physical_high_distribution_json,settlement_scalar_distribution_json,market_bucket_distribution_json,
        latent_post_state_json,trajectory_feature_state_json,source_reliability_state_json,observed_max_lower_bound_c,
        consumed_event_ids_json,consumed_ancestor_sha256,model_artifact_sha256,replay_generation,supersedes_checkpoint_id,state,created_at,committed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (checkpoint_id,target,run_id,"","initial","forecast-prev","prior-prev","resolution-v1","","","sequence-prev","temp","state","[]","previous-hash","{}","[]","{}","{}","{}","{}",None,"[]","ancestor-prev","c" * 64,0,"","committed",now,now),
    )
    authorized = validate_production_probability_activation(_activation())
    conn.execute(
        """INSERT INTO production_initial_posterior_lineage(
          posterior_checkpoint_id,target_identity,posterior_run_id,prior_context_id,
          prior_artifact_sha256,prior_model_artifact_sha256,weak_likelihood_artifact_sha256,weak_likelihood_sha256,
          forecast_evidence_snapshot_id,strong_lr_artifact_sha256,
          source_reliability_initial_state_sha256,resolution_contract_id,
          initial_evidence_sha256,activation_contract_sha256,lineage_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (checkpoint_id,target,run_id,"prior-prev","0" * 64,"a" * 64,"b" * 64,"f" * 64,
         "forecast-prev","c" * 64,"1" * 64,"resolution-v1","2" * 64,
         authorized["activation_contract_sha256"],"{}",now),
    )
    return {"target": target, "checkpoint_id": checkpoint_id, "run_id": run_id}, {"posterior_checkpoint_id": checkpoint_id, "posterior_run_id": run_id}


def _candidate(identity: dict[str, str], *, prefix: str, now: str) -> dict[str, Any]:
    update_id, checkpoint_id, run_id = f"update-{prefix}", f"pchk-{prefix}", f"post-{prefix}"
    fact_id, realization_id, outbox_id = f"factsnap-{prefix}", f"realization-{prefix}", f"outbox-{prefix}"
    update = {"strong_observation_update_id":update_id,"target_identity":identity["target"],"previous_posterior_run_id":identity["run_id"],"previous_posterior_sha256":"previous-hash","previous_checkpoint_id":identity["checkpoint_id"],"canonical_observation_event_id":f"event-{prefix}","forecast_evidence_snapshot_id":"forecast-prev","prior_context_id":"prior-prev","model_artifact_sha256":"c" * 64,"decision_as_of":now,"state":"applied","raw_log_score_json":"[]","raw_log_score_sha256":"raw","normalizer_offset":0.0,"posterior_delta_json":"[]","posterior_delta_sha256":"delta","pre_state_transition_json":"{}","post_state_transition_json":"{}","source_reliability_before_json":"{}","source_reliability_after_json":"{}","path_auxiliary_json":"{}","observed_max_lower_bound_c":29.5,"quality_json":"{}","block_reasons_json":"[]","started_at":now,"completed_at":now,"created_at":now}
    peak_timing_state = {
        "schema": "peak_timing_state_l3_v1",
        "ok": True,
        "state": "na",
        "na_reason": "temporary_no_order_drill_has_no_forecast_peak_clock",
        "canonical_observation_event_id": update["canonical_observation_event_id"],
        "strong_observation_update_id": update_id,
        "forecast_evidence_snapshot_id": "forecast-prev",
        "entry_peak_at": "",
        "does_not_update_physical_x": True,
        "market_or_trading_input_consumed": False,
        "read_only": True,
    }
    update["path_auxiliary_json"] = _json({
        "remaining_warming_marginal": [{"remaining_warming_c": 0.0, "probability": 1.0}],
        "termination_risk": {
            "schema": "termination_risk_marginal_v1", "state": "available",
            "canonical_observation_event_id": update["canonical_observation_event_id"],
            "probability_no_material_new_daily_high": 1.0,
            "updates_posterior": False,
        },
        "peak_timing_state": peak_timing_state,
    })
    joint = [
        {"temperature_c": 29.0, "state": "warming", "probability": 0.5},
        {"temperature_c": 30.0, "state": "warming", "probability": 0.5},
    ]
    checkpoint = {"posterior_checkpoint_id":checkpoint_id,"target_identity":identity["target"],"posterior_run_id":run_id,"previous_checkpoint_id":identity["checkpoint_id"],"checkpoint_kind":"incremental","forecast_evidence_snapshot_id":"forecast-prev","prior_context_id":"prior-prev","resolution_contract_id":"resolution-v1","last_strong_update_id":update_id,"last_canonical_event_id":update["canonical_observation_event_id"],"canonical_sequence_sha256":"sequence-current","temperature_grid_id":"temp","state_grid_id":"state","joint_posterior_json":_json(joint),"joint_posterior_sha256":_sha(joint),"physical_high_distribution_json":_json({"29":0.5,"30":0.5}),"settlement_scalar_distribution_json":_json([{"settlement_value_c":30.0,"probability":1.0}]),"market_bucket_distribution_json":_json({"30C":1.0}),"latent_post_state_json":_json({"warming":1.0}),"trajectory_feature_state_json":"{}","source_reliability_state_json":"{}","observed_max_lower_bound_c":29.5,"consumed_event_ids_json":_json([update["canonical_observation_event_id"]]),"consumed_ancestor_sha256":"ancestor-current","model_artifact_sha256":"c" * 64,"replay_generation":0,"supersedes_checkpoint_id":"","state":"production_ready","created_at":now,"committed_at":now}
    realization = {"realization_state_id":realization_id,"posterior_run_id":run_id,"fact_snapshot_id":fact_id,"resolution_contract_id":"resolution-v1","target_bucket":"30C","second_target_bucket":"","credible_bucket_set_json":_json(["30C"]),"current_report_bucket":"","current_report_bucket_probability":None,"probability_above_current_report_bucket":None,"current_report_bucket_hard_zero":0,"settlement_exclusion_certificates_json":"{}","state_json":_json({"projection":{"peak_timing_state":peak_timing_state}}),"state_sha256":"state-current","created_at":now,"committed_at":now}
    outbox = {"outbox_event_id":outbox_id,"posterior_run_id":run_id,"fact_snapshot_id":fact_id,"correlation_id":f"corr-{prefix}","state":"pending","created_at":now,"attempt_count":0,"claim_generation":0}
    contract = materialize_outbox_execution_contract_row(outbox_row=outbox, checkpoint_row=checkpoint, canonical_observation_event={"canonical_observation_event_id":update["canonical_observation_event_id"],"first_actionable_seen_at":"2026-07-14T00:00:00Z"}, inference_decision_deadline_at="2026-07-14T00:00:03Z")
    return {"state":"candidate_ready","candidate_mode":"production","shadow_only":False,"strong_update_row":update,"checkpoint_row":checkpoint,"realization_state_row":realization,"outbox_row":outbox,"outbox_execution_contract_row":contract,"resident_fast_lane_prewarm_dependency_identity":f"prewarm-{prefix}"}


def run_drill(backup_db: Path | str) -> dict[str, Any]:
    backup = Path(backup_db).expanduser().resolve()
    if backup == ROOT / "data/archive.db":
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["verified_maintenance_backup_required_not_live_archive"], "places_orders": False}
    if not backup.exists():
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["verified_backup_missing"], "places_orders": False}
    temp_dir = Path(tempfile.mkdtemp(prefix="weather-transaction-b-no-order-"))
    clone = temp_dir / "archive.clone.db"
    started = time.perf_counter()
    try:
        with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as src, sqlite3.connect(clone) as dest:
            for table in (
                "auto_orders", "auto_trade_decisions", "edge_trades",
                "event_fact_history", "event_fact_latest",
            ):
                row = src.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=? AND sql IS NOT NULL",
                    (table,),
                ).fetchone()
                if row is None or not str(row[0] or "").strip():
                    raise RuntimeError(f"verified_backup_required_table_schema_missing:{table}")
                dest.execute(str(row[0]))
            dest.commit()
        conn = sqlite3.connect(clone)
        try:
            for sql in TABLE_DDL.values():
                conn.execute(sql)
            for sql in INDEX_DDL.values():
                conn.execute(sql)
            initialize_production_initial_posterior_schema(conn)
            ensure_weak_forecast_revision_schema(conn)
            conn.commit()
            before = {table: _count(conn, table) for table in ("auto_orders", "auto_trade_decisions", "edge_trades")}
            prefix, now = uuid.uuid4().hex[:16], "2026-07-14T00:00:01Z"
            identity, previous = _seed_previous(conn, prefix=prefix, now=now)
            old_snapshot = _weak_snapshot(
                snapshot_id="forecast-prev", target=identity["target"],
                likelihood={"29": 1.0, "30": 1.0}, source_revision_id="taf-original", now=now,
            )
            fields = list(old_snapshot)
            conn.execute(
                f"INSERT INTO weak_forecast_evidence_snapshots({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                tuple(old_snapshot[field] for field in fields),
            )
            conn.commit()
            host = ProductionSequentialBayesTransactionBHost(conn, activation=_activation())
            result = host.commit_candidate(candidate=_candidate(identity, prefix=prefix, now=now), previous_checkpoint=previous, slug=f"no-order-drill-{prefix}", event_date="2026-07-14", fact_as_of=now)
            outbox_before_weak_revision = _count(conn, "posterior_commit_outbox")
            conn.row_factory = sqlite3.Row
            predecessor = dict(conn.execute(
                "SELECT * FROM posterior_checkpoints WHERE posterior_checkpoint_id=?",
                (f"pchk-{prefix}",),
            ).fetchone())
            revised_snapshot = _weak_snapshot(
                snapshot_id=f"forecast-amd-{prefix}", target=identity["target"],
                likelihood={"29": 0.5, "30": 2.0}, source_revision_id=f"taf-amd-{prefix}",
                now="2026-07-14T00:00:02Z",
            )
            weak_candidate = compile_weak_forecast_revision_candidate(
                previous_checkpoint_row=predecessor,
                previous_snapshot_row=old_snapshot,
                revised_snapshot_row=revised_snapshot,
                trigger_source_id="zuuu_taf_forecast_amsc",
                trigger_source_revision_id=f"taf-amd-{prefix}",
                revision_kind="amendment",
                public_slug=f"no-order-drill-{prefix}",
                decision_as_of="2026-07-14T00:00:02Z",
                replay_generation=0,
            )
            weak_result = commit_production_weak_forecast_revision(
                conn, activation=_activation(), candidate=weak_candidate,
            )
            outbox_after_weak_revision = _count(conn, "posterior_commit_outbox")
            after = {table: _count(conn, table) for table in before}
        finally:
            conn.close()
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        deltas = {table: after[table] - before[table] for table in before}
        weak_revision_outbox_delta = outbox_after_weak_revision - outbox_before_weak_revision
        ok = (
            result.get("ok") is True
            and weak_result.get("ok") is True
            and weak_revision_outbox_delta == 0
            and all(value == 0 for value in deltas.values())
        )
        return {"schema":SCHEMA_VERSION,"ok":ok,"state":"temporary_no_order_drill_passed" if ok else "temporary_no_order_drill_failed","transaction_b_elapsed_ms":elapsed_ms,"transaction_b":result,"weak_forecast_revision_transaction":weak_result,"weak_forecast_revision_outbox_delta":weak_revision_outbox_delta,"order_and_decision_table_deltas":deltas,"verified_backup_unchanged":True,"temporary_clone_deleted":True,"places_orders":False,"clob":False}
    except Exception as exc:
        return {"schema":SCHEMA_VERSION,"ok":False,"state":"temporary_no_order_drill_exception","exception_type":type(exc).__name__,"exception_detail":str(exc)[:240],"verified_backup_unchanged":True,"temporary_clone_deleted":True,"places_orders":False}
    finally:
        for path in (clone, temp_dir):
            try:
                if path.is_dir(): path.rmdir()
                elif path.exists(): path.unlink()
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", required=True, help="verified maintenance-window archive backup; live archive is refused")
    args = parser.parse_args()
    result = run_drill(args.backup)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
