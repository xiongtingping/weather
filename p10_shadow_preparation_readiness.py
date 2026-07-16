"""Read-only readiness audit for a real resident P10 shadow prepared cycle.

This audit does not invent a prior checkpoint, generate an LR artifact, or
turn a source inbox file into a training label.  Its purpose is to make the
remaining P10 activation prerequisites explicit before any persistent shadow
P8/P9 work is attempted.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from sequential_bayes_public_ssot_shadow_writer import SHADOW_MANIFEST_STATE, SHADOW_MANIFEST_TABLE
from strong_observation_lr_artifact_contract import (
    validate_strong_observation_lr_artifact,
    validate_strong_observation_lr_runtime_artifact,
)
from strong_observation_lr_runtime_registry import (
    ACTIVATION_TABLE as STRONG_LR_ACTIVATION_TABLE,
    load_active_strong_lr_runtime_artifact,
)


SCHEMA_VERSION = "p10_shadow_preparation_readiness_audit_v1"
PREPARED_CYCLE_TABLE = "resident_p10_prepared_cycles_shadow"
P1_REQUIRED_TABLES = (
    "canonical_observation_events",
    "posterior_checkpoints",
    "strong_observation_updates",
    "realization_states",
    "posterior_commit_outbox",
    "posterior_outbox_execution_contracts",
    "dynamic_three_leg_transition_plans",
    "posterior_execution_intents",
    "event_fact_history",
    "event_fact_latest",
    "auto_trade_decisions",
    "decision_intents",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _count(conn: sqlite3.Connection, table: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)


def _target_count(conn: sqlite3.Connection, table: str, target_identity: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    target = _text(target_identity)
    if not target:
        return _count(conn, table)
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE target_identity=?",
            (target,),
        ).fetchone()[0]
        or 0
    )


def _artifact_status(conn: sqlite3.Connection, *, target_identity: str) -> dict[str, Any]:
    target = _text(target_identity)
    if not target and _table_exists(conn, STRONG_LR_ACTIVATION_TABLE):
        targets = [
            _text(row[0])
            for row in conn.execute(
                f"SELECT DISTINCT target_identity FROM {STRONG_LR_ACTIVATION_TABLE} ORDER BY target_identity"
            ).fetchall()
            if _text(row[0])
        ]
        if len(targets) == 1:
            target = targets[0]
        elif len(targets) > 1:
            return {
                "configured": False,
                "runtime": {"ok": False, "issues": ["strong_lr_registry_target_identity_required"]},
                "offline_artifact_packet": {"ok": False, "issues": ["strong_lr_registry_target_identity_required"]},
                "offline_promotion": {"ok": False, "issues": ["strong_lr_registry_target_identity_required"]},
            }
    try:
        active = load_active_strong_lr_runtime_artifact(conn, target_identity=target)
    except ValueError:
        return {
            "configured": False,
            "runtime": {"ok": False, "issues": ["strong_lr_active_registry_artifact_not_configured"]},
            "offline_artifact_packet": {"ok": False, "issues": ["strong_lr_active_registry_artifact_not_configured"]},
            "offline_promotion": {"ok": False, "issues": ["strong_lr_active_registry_artifact_not_configured"]},
        }
    value = dict(active.get("artifact") or {})
    packet = validate_strong_observation_lr_artifact(value)
    activation_basis = _text(active.get("activation_basis"))
    activation_evidence = dict(active.get("activation_evidence") or {})
    activation_evidence_sha = _text(active.get("activation_evidence_sha256"))
    offline = (
        {
            "ok": True,
            "state": "activation_registry_attested_promotion",
            "activation_ref": _text(active.get("activation_ref")),
            "activation_evidence_sha256": activation_evidence_sha,
            "promotion_gate_sha256": _text(
                dict(activation_evidence.get("promotion_gate") or {}).get("promotion_gate_sha256")
            ),
        }
        if activation_basis == "promoted_candidate"
        else {
            "ok": True,
            "state": "not_applicable_approved_runtime_baseline",
            "activation_ref": _text(active.get("activation_ref")),
            "activation_evidence_sha256": activation_evidence_sha,
        }
    )
    return {
        "configured": True,
        "source": "strong_lr_runtime_active_registry",
        "target_identity": target,
        "activation_id": _text(active.get("activation_id")),
        "activation_basis": activation_basis,
        "artifact_sha256": _text(active.get("artifact_sha256")),
        "activation_evidence_sha256": activation_evidence_sha,
        "runtime": validate_strong_observation_lr_runtime_artifact(value),
        "offline_artifact_packet": packet,
        "offline_promotion": offline,
    }


def _source_inbox_status(
    inbox_dir: Path | None,
    status_path: Path | None,
    raw_archive_dir: Path | None,
) -> dict[str, Any]:
    inbox_files = []
    if inbox_dir is not None and inbox_dir.is_dir():
        inbox_files = sorted(path.name for path in inbox_dir.glob("*.json") if path.is_file())
    observed_status: dict[str, Any] = {}
    if status_path is not None and status_path.is_file():
        try:
            value = json.loads(status_path.read_text(encoding="utf-8"))
            observed_status = dict(value) if isinstance(value, Mapping) else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            observed_status = {"state": "unreadable"}
    expected_raw_archive_dir = str(raw_archive_dir.resolve()) if raw_archive_dir is not None else ""
    observed_raw_archive_dir = _text(observed_status.get("raw_archive_dir"))
    status_bound_to_raw_archive = bool(
        observed_status
        and expected_raw_archive_dir
        and observed_raw_archive_dir == expected_raw_archive_dir
    )
    return {
        "inbox_directory": str(inbox_dir) if inbox_dir is not None else "",
        "inbox_file_count": len(inbox_files),
        "inbox_files": inbox_files[-20:],
        "source_status_path": str(status_path) if status_path is not None else "",
        "expected_raw_archive_dir": expected_raw_archive_dir,
        "observed_status_raw_archive_dir": observed_raw_archive_dir,
        "source_status_bound_to_raw_archive": status_bound_to_raw_archive,
        # A status from another raw archive is historical diagnostics only,
        # never evidence about the P10 transport/T1 chain.
        "source_status": observed_status if status_bound_to_raw_archive else {},
        "unbound_source_status": observed_status if observed_status and not status_bound_to_raw_archive else {},
    }


def audit_p10_shadow_preparation_readiness(
    conn: sqlite3.Connection,
    *,
    target_identity: str = "",
    final_label_db_path: Path | None = None,
    inbox_dir: Path | None = None,
    source_status_path: Path | None = None,
    source_raw_archive_dir: Path | None = None,
) -> dict[str, Any]:
    """Return exact no-write blockers for the next P10 prepared cycle."""

    blockers: list[str] = []
    manifest = None
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        blockers.append("p10_preparation_shadow_manifest_missing")
    else:
        manifest = conn.execute(
            f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
        ).fetchone()
        if manifest is None or _text(manifest[0]) != SHADOW_MANIFEST_STATE:
            blockers.append("p10_preparation_shadow_manifest_invalid")
    missing_tables = [table for table in P1_REQUIRED_TABLES if not _table_exists(conn, table)]
    if missing_tables:
        blockers.append("p10_preparation_p1_tables_missing")
    checkpoint_count = _target_count(conn, "posterior_checkpoints", target_identity)
    if checkpoint_count == 0:
        blockers.append("p10_preparation_previous_checkpoint_missing")
    cycle_count = _target_count(conn, PREPARED_CYCLE_TABLE, target_identity)
    if cycle_count is None:
        blockers.append("p10_preparation_cycle_registry_missing")
    # A prepared cycle is the output of this readiness boundary, not an input
    # to it.  Treating zero rows as a precondition creates an impossible
    # self-dependency: a clean P10 DB could never become ready *to prepare*.
    # The status remains explicit for the separate host-start readiness check.
    prepared_cycle_status = (
        "registry_missing"
        if cycle_count is None
        else "prepared_cycle_missing"
        if cycle_count == 0
        else "prepared_cycle_present"
    )

    artifact = _artifact_status(conn, target_identity=target_identity)
    if artifact["runtime"].get("ok") is not True:
        blockers.append("p10_preparation_runtime_strong_lr_artifact_unavailable")
    # This check is intentionally reported separately.  It is an offline
    # promotion prerequisite, never a live order gate.
    promotion_blockers = []
    if artifact["offline_promotion"].get("ok") is not True:
        promotion_blockers.append("p10_preparation_offline_strong_lr_promotion_evidence_missing")

    final_labels: dict[str, Any] = {"path": str(final_label_db_path) if final_label_db_path else "", "count": None}
    if final_label_db_path is None or not final_label_db_path.is_file():
        final_labels["state"] = "unavailable"
        promotion_blockers.append("p10_preparation_final_resolution_labels_unavailable")
    else:
        try:
            label_conn = sqlite3.connect(f"file:{final_label_db_path.resolve()}?mode=ro", uri=True)
            try:
                final_labels["count"] = _count(label_conn, "resolution_final_labels")
                if final_labels["count"] is None:
                    final_labels["state"] = "missing_table"
                else:
                    final_labels["state"] = "ready" if final_labels["count"] else "empty"
            finally:
                label_conn.close()
            if final_labels["state"] == "missing_table":
                promotion_blockers.append("p10_preparation_final_resolution_labels_table_missing")
            elif final_labels["count"] == 0:
                promotion_blockers.append("p10_preparation_final_resolution_labels_empty")
        except sqlite3.Error:
            final_labels["state"] = "unreadable"
            promotion_blockers.append("p10_preparation_final_resolution_labels_unreadable")

    source_inbox = _source_inbox_status(inbox_dir, source_status_path, source_raw_archive_dir)
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": (
            "blocked"
            if blockers
            else "ready_with_prepared_cycle"
            if cycle_count
            else "ready_for_prepared_cycle"
        ),
        "blockers": sorted(set(blockers)),
        "offline_promotion_blockers": sorted(set(promotion_blockers)),
        "shadow_db": {
            "isolated_manifest": manifest is not None and _text(manifest[0]) == SHADOW_MANIFEST_STATE,
            "missing_p1_tables": missing_tables,
            "canonical_observation_event_count": _count(conn, "canonical_observation_events"),
            "previous_checkpoint_count": checkpoint_count,
            "strong_observation_update_count": _count(conn, "strong_observation_updates"),
            "prepared_cycle_count": cycle_count,
            "prepared_cycle_status": prepared_cycle_status,
            "latency_packet_count": _count(conn, "observation_fast_lane_latency_packets"),
        },
        "strong_lr_artifact": artifact,
        "final_resolution_labels": final_labels,
        "source_inbox": source_inbox,
        "boundary": {
            "read_only": True,
            "creates_checkpoint": False,
            "creates_prepared_cycle": False,
            "prepared_cycle_is_output_not_precondition": True,
            "promotes_artifact": False,
            "places_orders": False,
            "strict_asof_oos_are_offline_promotion_only": True,
        },
    }


__all__ = ["SCHEMA_VERSION", "audit_p10_shadow_preparation_readiness"]
