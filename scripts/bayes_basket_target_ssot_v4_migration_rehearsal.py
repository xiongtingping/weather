#!/usr/bin/env python3
"""Rehearse the exact SSOT v4 migration on a bounded disposable DB clone."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.artifact_ingress import ingest_artifact_bundle  # noqa: E402
from bayes_basket_target.migration_quiescence import (  # noqa: E402
    synthetic_writer_quiescence_receipt,
)
from bayes_basket_target.probability_bridge import (  # noqa: E402
    backfill_probability_bridges,
)
from bayes_basket_target.probability_evidence_store import (  # noqa: E402
    resolve_active_probability_evidence_artifact,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_DB_BYTES,
    MAX_TARGET_WAL_BYTES,
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.ssot import production_ssot_schema_status  # noqa: E402
from scripts.bayes_basket_target_production_ssot_apply import (  # noqa: E402
    PAUSE_MARKER_TOKEN,
    apply_controlled_migration,
    inspect_cash_phase_authority_manifest_schema,
    inspect_migration,
)
from scripts.ingest_bayes_basket_probability_evidence import (  # noqa: E402
    ingest as ingest_probability_evidence_packet,
)


SCHEMA = "bayes_basket_target_ssot_v4_migration_rehearsal_v1"
DEFAULT_OUTPUT = (
    ROOT / "output/audits/bayes_basket_target_ssot_v4_migration_rehearsal.json"
)
COPY_CHUNK_BYTES = 1024 * 1024


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_TARGET_DB_BYTES:
                raise RuntimeError("v4_rehearsal_source_database_budget_exceeded")
            digest.update(chunk)
    return digest.hexdigest()


def _source_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": _file_sha256(path),
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    names = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if table not in names:
        return 0
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _quick_check(conn: sqlite3.Connection) -> None:
    if [str(row[0]) for row in conn.execute("PRAGMA quick_check")] != ["ok"]:
        raise RuntimeError("v4_rehearsal_database_quick_check_failed")


def run_rehearsal(
    *,
    source_db: Path,
    artifact_packet: Path | None = None,
    probability_evidence_packet: Path | None = None,
) -> dict[str, Any]:
    source = source_db.expanduser().resolve()
    if not source.exists() or source.is_symlink() or not source.is_file():
        raise RuntimeError("v4_rehearsal_source_database_path_invalid")
    if source.stat().st_size > MAX_TARGET_DB_BYTES:
        raise RuntimeError("v4_rehearsal_source_database_budget_exceeded")
    if os.stat(source).st_dev != os.stat("/private/tmp").st_dev:
        raise RuntimeError("v4_rehearsal_source_not_on_local_device")
    free_bytes = int(os.statvfs("/private/tmp").f_bavail) * int(
        os.statvfs("/private/tmp").f_frsize
    )
    required_free = MIN_TARGET_FREE_BYTES + 3 * int(source.stat().st_size)
    if free_bytes < required_free:
        raise RuntimeError("v4_rehearsal_working_space_insufficient")
    assert_process_memory_budget(projected_bytes=8 * 1024 * 1024)

    source_before = _source_identity(source)
    with tempfile.TemporaryDirectory(
        prefix="bayes-target-v4-rehearsal-", dir="/private/tmp"
    ) as temp:
        temp_root = Path(temp)
        trial_db = temp_root / "target-v4-trial.db"
        backup_db = temp_root / "target-v4-before.db"
        second_backup = temp_root / "must-not-be-created.db"
        pause_file = temp_root / "target-paused.flag"
        pause_file.write_text(
            f"{PAUSE_MARKER_TOKEN}\nisolated-v4-rehearsal\n",
            encoding="utf-8",
        )

        with sqlite3.connect(
            f"file:{source}?mode=ro", uri=True, timeout=60.0
        ) as source_conn:
            source_conn.execute("PRAGMA busy_timeout=60000")
            _quick_check(source_conn)
            source_plan_before = inspect_migration(source_conn)
            source_manifest_schema_before = inspect_cash_phase_authority_manifest_schema(
                source_conn
            )
            source_counts_before = {
                "decisions": _count(source_conn, "auto_trade_decisions"),
                "training_facts": _count(
                    source_conn, "basket_policy_training_fact"
                ),
                "training_labels": _count(
                    source_conn, "basket_policy_training_label"
                ),
                "intents": _count(
                    source_conn, "basket_target_order_intents"
                ),
                "order_events": _count(
                    source_conn, "basket_target_order_events"
                ),
                "cash_phase_authority_manifests": _count(
                    source_conn, "basket_cash_phase_authority_manifest"
                ),
            }
            with sqlite3.connect(trial_db, timeout=60.0) as trial_conn:
                source_conn.backup(trial_conn)
                trial_conn.commit()

        if trial_db.stat().st_size > MAX_TARGET_DB_BYTES:
            raise RuntimeError("v4_rehearsal_trial_database_budget_exceeded")

        with sqlite3.connect(trial_db, timeout=60.0) as trial_conn:
            trial_conn.execute("PRAGMA busy_timeout=60000")
            before_plan = inspect_migration(trial_conn)
            manifest_schema_before = inspect_cash_phase_authority_manifest_schema(
                trial_conn
            )
            snapshot_counts = {
                "decisions": _count(trial_conn, "auto_trade_decisions"),
                "training_facts": _count(
                    trial_conn, "basket_policy_training_fact"
                ),
                "training_labels": _count(
                    trial_conn, "basket_policy_training_label"
                ),
                "intents": _count(
                    trial_conn, "basket_target_order_intents"
                ),
                "order_events": _count(
                    trial_conn, "basket_target_order_events"
                ),
                "cash_phase_authority_manifests": _count(
                    trial_conn, "basket_cash_phase_authority_manifest"
                ),
            }
            first = apply_controlled_migration(
                trial_conn,
                db_path=trial_db,
                backup_path=backup_db,
                pause_file=pause_file,
                legacy_archive_table=(
                    "auto_trade_decisions_legacy_v4_rehearsal"
                ),
                confirmed=True,
                writer_quiescence_receipt=(
                    synthetic_writer_quiescence_receipt(trial_db)
                ),
            )
            probability_bridge_backfill = backfill_probability_bridges(
                trial_conn
            )
            _quick_check(trial_conn)
            after_status = production_ssot_schema_status(trial_conn)
            manifest_schema_after = inspect_cash_phase_authority_manifest_schema(
                trial_conn
            )
            if not manifest_schema_after.get("ok", False):
                raise RuntimeError("v4_rehearsal_manifest_schema_not_ready")
            after_counts = {
                "decisions": _count(trial_conn, "auto_trade_decisions"),
                "training_facts": _count(
                    trial_conn, "basket_policy_training_fact"
                ),
                "training_labels": _count(
                    trial_conn, "basket_policy_training_label"
                ),
                "intents": _count(
                    trial_conn, "basket_target_order_intents"
                ),
                "order_events": _count(
                    trial_conn, "basket_target_order_events"
                ),
                "cash_phase_authority_manifests": _count(
                    trial_conn, "basket_cash_phase_authority_manifest"
                ),
            }
            if after_status.get("ok") is not True:
                raise RuntimeError("v4_rehearsal_post_schema_not_ready")
            required_probability_tables = {
                "basket_probability_evidence_artifact",
                "basket_probability_evidence_promotion",
                "basket_probability_evidence_activation",
                "basket_probability_update_proof",
                "basket_probability_bridge_history",
                "basket_probability_bridge_latest",
                "basket_probability_bridge_bucket_probability",
            }
            schema_objects = {
                (str(row[0]), str(row[1]))
                for row in trial_conn.execute(
                    "SELECT type,name FROM sqlite_master "
                    "WHERE type IN ('table','index','trigger')"
                )
            }
            missing_probability_tables = sorted(
                table
                for table in required_probability_tables
                if ("table", table) not in schema_objects
            )
            required_probability_triggers = {
                "basket_probability_evidence_artifact_no_update",
                "basket_probability_evidence_artifact_no_delete",
                "basket_probability_evidence_promotion_no_update",
                "basket_probability_evidence_promotion_no_delete",
                "basket_probability_evidence_activation_no_update",
                "basket_probability_evidence_activation_no_delete",
                "basket_probability_update_proof_no_update",
                "basket_probability_update_proof_no_delete",
                "basket_probability_bridge_history_no_update",
                "basket_probability_bridge_history_no_delete",
                "basket_probability_bridge_bucket_no_update",
                "basket_probability_bridge_bucket_no_delete",
            }
            missing_probability_triggers = sorted(
                trigger
                for trigger in required_probability_triggers
                if ("trigger", trigger) not in schema_objects
            )
            if missing_probability_tables or missing_probability_triggers:
                raise RuntimeError(
                    "v4_rehearsal_probability_evidence_schema_incomplete"
                )
            probability_schema_rehearsal = {
                "state": "ready",
                "tables": sorted(required_probability_tables),
                "missing_tables": missing_probability_tables,
                "missing_triggers": missing_probability_triggers,
            }
            if after_counts != snapshot_counts:
                raise RuntimeError("v4_rehearsal_fact_counts_changed")
            if artifact_packet is None:
                artifact_packet_validation = {
                    "state": "not_requested",
                }
            else:
                packet = load_bounded_json(
                    artifact_packet.expanduser().resolve()
                )
                if not isinstance(packet, dict):
                    raise RuntimeError(
                        "v4_rehearsal_artifact_packet_mapping_required"
                    )
                packet_sha256 = str(
                    packet.get("packet_sha256") or ""
                )
                if len(packet_sha256) != 64:
                    raise RuntimeError(
                        "v4_rehearsal_artifact_packet_sha256_invalid"
                    )
                preview = ingest_artifact_bundle(
                    trial_conn,
                    packet,
                    commit=False,
                )
                declared_counts = {
                    field: len(packet.get(field) or [])
                    for field in (
                        "calibration_artifacts",
                        "ensemble_selection_evidence",
                        "ensemble_artifacts",
                        "calibration_activations",
                        "cash_phase_policies",
                        "cash_phase_authority_manifests",
                    )
                }
                if (
                    preview.get("state") != "VALIDATED_NO_WRITE"
                    or sum(
                        int(declared_counts[field])
                        for field in declared_counts
                    ) <= 0
                ):
                    raise RuntimeError(
                        "v4_rehearsal_artifact_packet_validation_failed"
                    )
                committed = ingest_artifact_bundle(
                    trial_conn,
                    packet,
                    commit=True,
                )
                idempotent = ingest_artifact_bundle(
                    trial_conn,
                    packet,
                    commit=True,
                )
                if (
                    committed.get("state") != "COMMITTED"
                    or idempotent.get("state") != "COMMITTED"
                    or any(
                        int(value or 0) != 0
                        for value in (idempotent.get("counts") or {}).values()
                    )
                ):
                    raise RuntimeError(
                        "v4_rehearsal_artifact_packet_idempotent_commit_failed"
                    )
                artifact_packet_validation = {
                    "state": (
                        "COMMITTED_IDEMPOTENT_ON_DISPOSABLE_CLONE"
                        if any(
                            int(value or 0) > 0
                            for value in (
                                committed.get("counts") or {}
                            ).values()
                        )
                        else
                        "ALREADY_MATERIALIZED_IDEMPOTENT_ON_DISPOSABLE_CLONE"
                    ),
                    "packet_path": str(
                        artifact_packet.expanduser().resolve()
                    ),
                    "packet_sha256": packet_sha256,
                    "station_count": len(packet.get("station_ids") or []),
                    "cash_policy_count": len(
                        packet.get("cash_phase_policies") or []
                    ),
                    "cash_manifest_count": len(
                        packet.get(
                            "cash_phase_authority_manifests"
                        )
                        or []
                    ),
                    "declared_counts": declared_counts,
                    "preview_counts": preview["counts"],
                    "preview_rolled_back": True,
                    "commit_counts": committed["counts"],
                    "idempotent_second_commit_counts": idempotent["counts"],
                }
            if probability_evidence_packet is None:
                probability_evidence_packet_validation = {
                    "state": "not_requested",
                }
            else:
                probability_packet = load_bounded_json(
                    probability_evidence_packet.expanduser().resolve()
                )
                if not isinstance(probability_packet, dict):
                    raise RuntimeError(
                        "v4_rehearsal_probability_evidence_packet_mapping_required"
                    )
                preview = ingest_probability_evidence_packet(
                    trial_conn,
                    probability_packet,
                    commit=False,
                )
                committed = ingest_probability_evidence_packet(
                    trial_conn,
                    probability_packet,
                    commit=True,
                )
                idempotent = ingest_probability_evidence_packet(
                    trial_conn,
                    probability_packet,
                    commit=True,
                )
                if (
                    preview.get("state") != "VALIDATED_NO_WRITE"
                    or committed.get("state") != "COMMITTED"
                    or idempotent.get("state") != "COMMITTED"
                    or any(
                        receipt.get("wrote") is not False
                        for key in (
                            "artifact_receipts",
                            "promotion_receipts",
                            "activation_receipts",
                        )
                        for receipt in idempotent.get(key, [])
                    )
                ):
                    raise RuntimeError(
                        "v4_rehearsal_probability_evidence_ingress_not_idempotent"
                    )
                resolved_activations = []
                for activation in probability_packet.get("activations") or []:
                    resolved = resolve_active_probability_evidence_artifact(
                        trial_conn,
                        artifact_kind=str(activation["artifact_kind"]),
                        station_id=str(activation["station_id"]),
                        scope_key=str(activation["scope_key"]),
                        as_of_utc=str(activation["activated_at_utc"]),
                        required_authority_scope=str(
                            activation["authority_scope"]
                        ),
                    )
                    if (
                        resolved is None
                        or resolved.get("artifact_id")
                        != activation.get("artifact_id")
                    ):
                        raise RuntimeError(
                            "v4_rehearsal_probability_evidence_reader_replay_failed"
                        )
                    resolved_activations.append({
                        "activation_id": str(activation["activation_id"]),
                        "artifact_id": str(resolved["artifact_id"]),
                        "artifact_content_sha256": str(
                            resolved["content_sha256"]
                        ),
                    })
                probability_evidence_packet_validation = {
                    "state": (
                        "COMMITTED_IDEMPOTENT_READER_REPLAYED_ON_DISPOSABLE_CLONE"
                    ),
                    "packet_path": str(
                        probability_evidence_packet.expanduser().resolve()
                    ),
                    "packet_sha256": str(
                        probability_packet.get("packet_sha256") or ""
                    ),
                    "row_count": int(committed.get("row_count") or 0),
                    "activation_count": len(
                        probability_packet.get("activations") or []
                    ),
                    "resolved_activations": resolved_activations,
                    "preview_rolled_back": True,
                }

            second = apply_controlled_migration(
                trial_conn,
                db_path=trial_db,
                backup_path=second_backup,
                pause_file=pause_file,
                legacy_archive_table=(
                    "auto_trade_decisions_legacy_v4_rehearsal_second"
                ),
                confirmed=True,
                writer_quiescence_receipt=(
                    synthetic_writer_quiescence_receipt(trial_db)
                ),
            )
            if second.get("state") != "already_ready":
                raise RuntimeError("v4_rehearsal_idempotency_failed")
            if second_backup.exists():
                raise RuntimeError("v4_rehearsal_idempotency_created_backup")
            second_probability_bridge_backfill = (
                backfill_probability_bridges(trial_conn)
            )
            if (
                second_probability_bridge_backfill.get(
                    "materialized_count"
                )
                != 0
            ):
                raise RuntimeError(
                    "v4_rehearsal_probability_bridge_not_idempotent"
                )
            trial_conn.commit()
            trial_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        if not backup_db.is_file():
            raise RuntimeError("v4_rehearsal_pre_migration_backup_missing")
        with sqlite3.connect(backup_db, timeout=60.0) as backup_conn:
            _quick_check(backup_conn)
            backup_plan = inspect_migration(backup_conn)
            if backup_plan != before_plan:
                raise RuntimeError("v4_rehearsal_backup_plan_drift")
            backup_manifest_schema = inspect_cash_phase_authority_manifest_schema(
                backup_conn
            )
            if backup_manifest_schema != manifest_schema_before:
                raise RuntimeError("v4_rehearsal_backup_manifest_schema_drift")

        trial_bytes = int(trial_db.stat().st_size)
        trial_wal = trial_db.with_name(trial_db.name + "-wal")
        trial_wal_bytes = (
            int(trial_wal.stat().st_size) if trial_wal.is_file() else 0
        )
        backup_bytes = int(backup_db.stat().st_size)
        if trial_bytes > MAX_TARGET_DB_BYTES:
            raise RuntimeError("v4_rehearsal_trial_database_budget_exceeded")
        if trial_wal_bytes > MAX_TARGET_WAL_BYTES:
            raise RuntimeError("v4_rehearsal_trial_wal_budget_exceeded")
        if backup_bytes > MAX_TARGET_DB_BYTES:
            raise RuntimeError("v4_rehearsal_backup_database_budget_exceeded")

    source_after = _source_identity(source)
    concurrent_source_drift_observed = source_after != source_before
    return {
        "schema": SCHEMA,
        "state": "passed",
        "source_database": str(source),
        "source_before": source_before,
        "source_after": source_after,
        "source_plan_before": source_plan_before,
        "source_manifest_schema_before": source_manifest_schema_before,
        "source_counts_before": source_counts_before,
        "concurrent_source_drift_observed": concurrent_source_drift_observed,
        "snapshot_counts": snapshot_counts,
        "before_plan": before_plan,
        "manifest_schema_before": manifest_schema_before,
        "manifest_schema_after": manifest_schema_after,
        "backup_manifest_schema": backup_manifest_schema,
        "migration_receipt": first,
        "probability_bridge_backfill": probability_bridge_backfill,
        "second_probability_bridge_backfill": (
            second_probability_bridge_backfill
        ),
        "probability_schema_rehearsal": probability_schema_rehearsal,
        "after_schema": after_status,
        "after_counts": after_counts,
        "artifact_packet_validation": artifact_packet_validation,
        "probability_evidence_packet_validation": (
            probability_evidence_packet_validation
        ),
        "idempotent_second_state": second["state"],
        "temporary_trial_database_removed": not trial_db.exists(),
        "temporary_backup_removed": not backup_db.exists(),
        "temporary_working_directory_removed": not temp_root.exists(),
        "resource_budget": {
            "max_database_bytes": MAX_TARGET_DB_BYTES,
            "max_wal_bytes": MAX_TARGET_WAL_BYTES,
            "minimum_free_bytes": MIN_TARGET_FREE_BYTES,
            "free_bytes_before": free_bytes,
            "required_free_bytes": required_free,
            "trial_database_bytes": trial_bytes,
            "trial_wal_bytes": trial_wal_bytes,
            "backup_database_bytes": backup_bytes,
        },
        "boundary": {
            "source_opened_read_only": True,
            "concurrent_source_writer_permitted_for_online_backup": True,
            "concurrent_source_drift_observed": (
                concurrent_source_drift_observed
            ),
            "writes_source_database": False,
            "writes_production_database": False,
            "writes_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "execution_mode": "production_no_live",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--artifact-packet", type=Path)
    parser.add_argument("--probability-evidence-packet", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_rehearsal(
        source_db=args.source_db,
        artifact_packet=args.artifact_packet,
        probability_evidence_packet=args.probability_evidence_packet,
    )
    output = args.output.expanduser().resolve()
    write_bounded_json(output, result)
    print(
        json.dumps(
            {
                "schema": result["schema"],
                "state": result["state"],
                "snapshot_counts": result["snapshot_counts"],
                "schema_ready_after": result["after_schema"]["ok"],
                "idempotent_second_state": result[
                    "idempotent_second_state"
                ],
                "orders_submitted": 0,
                "output": str(output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
