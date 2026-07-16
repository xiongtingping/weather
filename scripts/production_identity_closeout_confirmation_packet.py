#!/usr/bin/env python3
"""Read-only master packet for production identity closeout.

This packet stitches the already-reviewed building blocks into one operator
sequence: identity audit indexes, duplicate fact_snapshot repair, identity
merge-fill, then coverage/joint verification. It never writes archive.db,
creates a backup, writes a manifest, stops services, fetches weather sources,
or places orders.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import controlled_apply_maintenance_window_packet as maintenance_window  # noqa: E402
from scripts import production_identity_duplicate_fact_snapshot_apply as duplicate_apply  # noqa: E402
from scripts import production_identity_duplicate_fact_snapshot_operator_runbook as duplicate_runbook  # noqa: E402
from scripts import production_identity_index_apply as index_apply  # noqa: E402
from scripts import production_identity_index_apply_contract as index_contract  # noqa: E402
from scripts import production_identity_merge_fill_apply as merge_fill_apply  # noqa: E402
from scripts import production_identity_merge_fill_apply_contract as merge_fill_contract  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "production_identity_closeout_confirmation_packet_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"
DEFAULT_LATEST_ANCHOR_POLICY = "regenerate_latest_row_fact_snapshot_id_and_keep_latest_anchor"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_stamp(value: str) -> str:
    return str(value or "").replace(":", "").replace("-", "").replace("+00:00", "Z")


def _q(value: Path | str) -> str:
    return shlex.quote(str(value))


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _guards() -> dict[str, bool]:
    return {
        "executes_sql_writes": False,
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_auto_orders": False,
        "writes_source_observations": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "fetches_external_weather_sources": False,
        "historical_backfill": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _load_json_file(path: Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _manifest_output_path(db: Path, backup_dir: Path, generated_at: str) -> Path:
    return backup_dir / f"{db.name}.production-identity-closeout-{_safe_stamp(generated_at)}.duplicate-manifest.json"


def _backup_path(db: Path, backup_dir: Path, generated_at: str) -> Path:
    return backup_dir / f"{db.name}.production-identity-closeout-{_safe_stamp(generated_at)}.bak"


def _maintenance_summary(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": packet.get("schema"),
        "ok": bool(packet.get("ok")),
        "writer_quiescent": bool(packet.get("writer_quiescent")),
        "required_stop_labels": list(packet.get("required_stop_labels") or []),
        "blockers": list(packet.get("blockers") or [])[:20],
        "reader_warnings": list(packet.get("reader_warnings") or [])[:20],
        "operator_commands": dict(_as_mapping(packet.get("operator_commands"))),
        "stop_authorization": dict(_as_mapping(packet.get("stop_authorization"))),
        "counts": dict(_as_mapping(packet.get("counts"))),
        "next_boundary": packet.get("next_boundary"),
    }


def _step(
    *,
    order: int,
    phase: str,
    step: str,
    command: str,
    writes: bool,
    required: bool = True,
    writes_archive_db: bool = False,
    writes_manifest_file: bool = False,
    creates_backup: bool = False,
    confirmation_phrase: str = "",
    blocked_until: str = "",
    pass_condition: str = "",
) -> dict[str, Any]:
    return {
        "order": int(order),
        "phase": phase,
        "step": step,
        "command": command,
        "writes": bool(writes),
        "writes_archive_db": bool(writes_archive_db),
        "writes_manifest_file": bool(writes_manifest_file),
        "creates_backup": bool(creates_backup),
        "required": bool(required),
        "confirmation_phrase": confirmation_phrase,
        "blocked_until": blocked_until,
        "pass_condition": pass_condition,
    }


def build_confirmation_packet(
    db_path: Path | str = DEFAULT_DB,
    *,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
    max_items_output: int = 8,
    limit: int = 1000000,
    duplicate_decision_manifest: Mapping[str, Any] | None = None,
    latest_anchor_policy: str = DEFAULT_LATEST_ANCHOR_POLICY,
    precomputed_merge_fill_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    generated = generated_at or _now_utc()
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    manifest_path = _manifest_output_path(db, backup_root, generated)
    backup_path = _backup_path(db, backup_root, generated)
    policy = str(latest_anchor_policy or DEFAULT_LATEST_ANCHOR_POLICY).strip()

    duplicate_operator = duplicate_runbook.build_operator_runbook(
        db,
        backup_dir=backup_root,
        generated_at=generated,
        max_items_output=int(max_items_output),
        decision_manifest=duplicate_decision_manifest,
        latest_anchor_policy=policy,
    )
    reviewed_manifest = dict(_as_mapping(duplicate_operator.get("reviewed_manifest_candidate")))
    effective_manifest = dict(duplicate_decision_manifest or reviewed_manifest)
    duplicate_contract = _as_mapping(duplicate_operator.get("reviewed_apply_contract_summary"))
    index_apply_contract = index_contract.build_contract(db, backup_dir=backup_root)
    index_apply_contract_summary = index_contract.compact_contract(
        index_apply_contract,
        max_items_output=int(max_items_output),
    )
    if isinstance(precomputed_merge_fill_contract, Mapping) and precomputed_merge_fill_contract:
        merge_contract_summary = dict(precomputed_merge_fill_contract)
        merge_contract = merge_contract_summary
    else:
        merge_contract = merge_fill_contract.build_contract(
            db,
            duplicate_decision_manifest=effective_manifest,
            limit=int(limit),
            max_items_output=int(max_items_output),
        )
        merge_contract_summary = merge_fill_contract.compact_contract(
            merge_contract,
            max_items_output=int(max_items_output),
        )
    maintenance = maintenance_window.build_maintenance_window_packet(
        db,
        max_items_output=int(max_items_output),
    )

    manifest_command = (
        "python3 scripts/production_identity_duplicate_fact_snapshot_operator_runbook.py "
        f"--db {_q(db)} --latest-anchor-policy {_q(policy)} --emit-reviewed-manifest-json "
        f"> {_q(manifest_path)}"
    )
    audit_manifest_command = (
        "python3 scripts/production_identity_duplicate_fact_snapshot_packet.py "
        f"--db {_q(db)} --summary --max-items-output {int(max_items_output)} "
        f"--decision-manifest {_q(manifest_path)}"
    )
    duplicate_contract_command = (
        "python3 scripts/production_identity_duplicate_fact_snapshot_apply_contract.py "
        f"--db {_q(db)} --summary --max-items-output {int(max_items_output)} "
        f"--decision-manifest {_q(manifest_path)}"
    )
    merge_contract_command = (
        "python3 scripts/production_identity_merge_fill_apply_contract.py "
        f"--db {_q(db)} --summary --limit {int(limit)} --max-items-output {int(max_items_output)} "
        f"--duplicate-decision-manifest {_q(manifest_path)}"
    )
    index_contract_command = (
        "python3 scripts/production_identity_index_apply_contract.py "
        f"--db {_q(db)} --summary --max-items-output {int(max_items_output)}"
    )
    index_temp_drill_command = (
        "python3 scripts/production_identity_index_temp_drill.py "
        f"--db {_q(db)} --summary --row-limit 1000"
    )
    temp_drill_command = (
        "python3 scripts/production_identity_merge_fill_temp_drill.py "
        f"--db {_q(db)} --summary --limit {int(limit)} --max-items-output {int(max_items_output)} "
        "--copy-mode focused"
    )
    backup_command = f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup_path)}\""
    verify_quiescence_command = (
        "python3 scripts/controlled_apply_maintenance_window_packet.py "
        f"--db {_q(db)} --summary --max-items-output {int(max_items_output)}"
    )
    duplicate_apply_command = (
        "python3 scripts/production_identity_duplicate_fact_snapshot_apply.py "
        f"--db {_q(db)} --decision-manifest {_q(manifest_path)} --apply "
        f"--confirm {duplicate_apply.CONFIRMATION_PHRASE} "
        f"--backup {_q(backup_path)} --writer-quiescent"
    )
    index_apply_command = (
        "python3 scripts/production_identity_index_apply.py "
        f"--db {_q(db)} --apply --confirm {index_apply.CONFIRMATION_PHRASE} "
        f"--backup {_q(backup_path)} --writer-quiescent"
    )
    merge_apply_command = (
        "python3 scripts/production_identity_merge_fill_apply.py "
        f"--db {_q(db)} --duplicate-decision-manifest {_q(manifest_path)} --limit {int(limit)} --apply "
        f"--confirm {merge_fill_apply.CONFIRMATION_PHRASE} "
        f"--backup {_q(backup_path)} --writer-quiescent"
    )
    post_duplicate_command = (
        "python3 scripts/production_identity_duplicate_fact_snapshot_packet.py "
        f"--db {_q(db)} --summary --max-items-output {int(max_items_output)} "
        f"--decision-manifest {_q(manifest_path)}"
    )
    coverage_command = (
        "python3 scripts/production_identity_coverage_audit.py "
        f"--db {_q(db)} --summary --max-items-output {int(max_items_output)}"
    )
    joint_command = (
        "python3 scripts/hierarchical_joint_plan_coverage_audit.py "
        f"--summary --limit 1000000 --max-items-output {int(max_items_output)}"
    )

    duplicate_ready = bool(duplicate_contract.get("ok"))
    index_ready = not bool(index_apply_contract.get("blockers"))
    merge_ready = bool(merge_contract.get("ok"))
    writer_quiescent = bool(maintenance.get("writer_quiescent"))
    index_needs_apply = bool(index_apply_contract.get("needs_apply"))
    index_ddl_count = int(index_apply_contract.get("ddl_count") or 0)
    manifest_decision_count = int(effective_manifest.get("decision_count") or 0)
    duplicate_row_count = int(duplicate_operator.get("duplicate_row_count") or 0)
    reviewed_manifest_ready = bool(
        manifest_decision_count >= duplicate_row_count
        and str(effective_manifest.get("latest_anchor_policy") or "") != "manual_review_required"
    )

    control_sequence = [
        _step(
            order=1,
            phase="preflight",
            step="pause_writer_services",
            command=" && ".join(_as_sequence(_as_mapping(maintenance.get("operator_commands")).get("suggested_pause_commands")))
            or "no_writer_services_to_pause",
            writes=False,
            required=not writer_quiescent,
            pass_condition="maintenance packet writer_quiescent becomes true",
        ),
        _step(
            order=2,
            phase="preflight",
            step="materialize_reviewed_duplicate_manifest",
            command=manifest_command,
            writes=True,
            writes_manifest_file=True,
            required=bool(duplicate_row_count),
            pass_condition="operator has reviewed every duplicate decision and latest anchor policy",
        ),
        _step(
            order=3,
            phase="preflight",
            step="audit_reviewed_duplicate_manifest",
            command=audit_manifest_command,
            writes=False,
            required=bool(duplicate_row_count),
            pass_condition="decision_manifest_audit has no hard blockers",
        ),
        _step(
            order=4,
            phase="preflight",
            step="review_duplicate_apply_contract",
            command=duplicate_contract_command,
            writes=False,
            required=bool(duplicate_row_count),
            pass_condition="duplicate apply contract ok and operation count matches reviewed manifest",
        ),
        _step(
            order=5,
            phase="preflight",
            step="review_identity_audit_index_contract",
            command=index_contract_command,
            writes=False,
            required=bool(index_needs_apply),
            pass_condition="identity audit index contract has no blockers; needs_apply may be true before controlled apply",
        ),
        _step(
            order=6,
            phase="preflight",
            step="run_identity_audit_index_temp_drill",
            command=index_temp_drill_command,
            writes=False,
            required=bool(index_needs_apply),
            pass_condition="identity audit index temp drill ok and production db unchanged",
        ),
        _step(
            order=7,
            phase="preflight",
            step="run_identity_closeout_temp_drill",
            command=temp_drill_command,
            writes=False,
            required=True,
            pass_condition="temp drill ok, duplicate repair applies on temp, merge-fill applies on temp, post missing total == 0",
        ),
        _step(
            order=8,
            phase="preflight",
            step="create_shared_fresh_backup",
            command=backup_command,
            writes=True,
            creates_backup=True,
            required=True,
            pass_condition="backup exists and is non-empty before either apply",
        ),
        _step(
            order=9,
            phase="preflight",
            step="verify_writer_quiescence",
            command=verify_quiescence_command,
            writes=False,
            required=True,
            pass_condition="writer_quiescent == true and packet ok == true",
        ),
        _step(
            order=10,
            phase="apply",
            step="apply_identity_audit_indexes",
            command=index_apply_command,
            writes=True,
            writes_archive_db=True,
            required=bool(index_needs_apply),
            confirmation_phrase=index_apply.CONFIRMATION_PHRASE,
            blocked_until="index contract and temp drill pass, backup exists, writer quiescent, operator confirms",
        ),
        _step(
            order=11,
            phase="apply",
            step="apply_duplicate_fact_snapshot_repair",
            command=duplicate_apply_command,
            writes=True,
            writes_archive_db=True,
            required=bool(duplicate_row_count),
            confirmation_phrase=duplicate_apply.CONFIRMATION_PHRASE,
            blocked_until="steps 2-8 pass and operator confirms",
        ),
        _step(
            order=12,
            phase="verify",
            step="verify_duplicate_repair",
            command=post_duplicate_command,
            writes=False,
            required=bool(duplicate_row_count),
            pass_condition="duplicate_row_count == 0",
        ),
        _step(
            order=13,
            phase="preflight",
            step="review_identity_merge_fill_contract_after_duplicate_repair",
            command=merge_contract_command,
            writes=False,
            required=True,
            blocked_until="duplicate repair apply and post-duplicate verification pass",
            pass_condition="merge-fill contract ok after duplicate fact_snapshot repair has been applied",
        ),
        _step(
            order=14,
            phase="apply",
            step="apply_identity_merge_fill",
            command=merge_apply_command,
            writes=True,
            writes_archive_db=True,
            required=True,
            confirmation_phrase=merge_fill_apply.CONFIRMATION_PHRASE,
            blocked_until="duplicate repair apply and post-duplicate verification pass",
        ),
        _step(
            order=15,
            phase="verify",
            step="verify_identity_coverage",
            command=coverage_command,
            writes=False,
            required=True,
            pass_condition="production_row_identity_ready == true",
        ),
        _step(
            order=16,
            phase="verify",
            step="verify_full_joint_gate",
            command=joint_command,
            writes=False,
            required=True,
            pass_condition="production_identity_coverage_gate == true and downstream_single_ssot_gate == true",
        ),
    ]
    hard_blockers: list[str] = []
    if not duplicate_ready and duplicate_row_count:
        hard_blockers.append("duplicate_apply_contract_not_ready_from_reviewed_manifest")
    if not index_ready:
        hard_blockers.append("identity_audit_index_contract_blocked")
    if not reviewed_manifest_ready and duplicate_row_count:
        hard_blockers.append("reviewed_duplicate_manifest_not_ready")
    if not writer_quiescent:
        hard_blockers.append("writer_quiescence_not_satisfied")

    return {
        "schema": SCHEMA_VERSION,
        "ok": not hard_blockers,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "production_identity_closeout_confirmation_packet_no_apply",
        "generated_at": generated,
        "db": str(db),
        "backup_dir": str(backup_root),
        "shared_backup_path": str(backup_path),
        "shared_backup_exists": backup_path.exists(),
        "duplicate_manifest_path": str(manifest_path),
        "duplicate_manifest_exists": manifest_path.exists(),
        "latest_anchor_policy": policy,
        "reviewed_duplicate_manifest_summary": {
            "schema": effective_manifest.get("schema"),
            "decision_count": manifest_decision_count,
            "latest_anchor_policy": effective_manifest.get("latest_anchor_policy"),
            "latest_anchor_policy_required": bool(effective_manifest.get("latest_anchor_policy_required")),
            "operator_review_required": bool(effective_manifest.get("operator_review_required", True)),
        },
        "duplicate": {
            "group_count": duplicate_operator.get("duplicate_group_count"),
            "row_count": duplicate_row_count,
            "reviewed_apply_contract_ok": duplicate_ready,
            "reviewed_apply_operation_count": duplicate_contract.get("operation_count"),
            "reviewed_apply_blockers": list(duplicate_contract.get("blockers") or []),
        },
        "identity_audit_indexes": {
            "contract_ok": bool(index_ready),
            "already_satisfied": bool(index_apply_contract.get("ok")),
            "needs_apply": index_needs_apply,
            "ddl_count": index_ddl_count,
            "contract_blocker_count": len(list(index_apply_contract.get("blockers") or [])),
            "blockers": list(index_apply_contract.get("blockers") or []),
            "apply_blockers": list(index_apply_contract.get("apply_blockers") or []),
            "contract_summary": index_apply_contract_summary,
        },
        "merge_fill": {
            "contract_ok": merge_ready,
            "contract_stage": "pre_duplicate_repair_diagnostic",
            "expected_to_be_ready_after_duplicate_repair_apply": bool(duplicate_ready and reviewed_manifest_ready),
            "operation_count": int(merge_contract.get("operation_count") or 0),
            "candidate_count": int(merge_contract.get("candidate_count") or 0),
            "safe_candidate_count": int(merge_contract.get("safe_candidate_count") or 0),
            "blocked_candidate_count": int(merge_contract.get("blocked_candidate_count") or 0),
            "blockers": list(merge_contract.get("blockers") or []),
            "contract_summary": merge_contract_summary,
        },
        "maintenance_window": _maintenance_summary(maintenance),
        "control_sequence": control_sequence,
        "confirmation_phrases": {
            "identity_audit_indexes": index_apply.CONFIRMATION_PHRASE,
            "duplicate_fact_snapshot_repair": duplicate_apply.CONFIRMATION_PHRASE,
            "identity_merge_fill": merge_fill_apply.CONFIRMATION_PHRASE,
        },
        "hard_blockers": hard_blockers,
        "boundary": {
            "packet_executes_apply": False,
            "packet_creates_backup": False,
            "packet_writes_manifest": False,
            "packet_stops_or_restarts_services": False,
            "duplicate_apply_must_precede_identity_merge_fill": True,
            "identity_audit_index_apply_must_precede_identity_coverage_verification": True,
            "single_shared_backup_for_all_identity_apply_stages": True,
            "temp_drill_must_precede_production_apply": True,
            "production_apply_requires_operator_confirmation": True,
        },
        "guards": _guards(),
        "next_boundary": (
            "operator_can_run_control_sequence_in_writer_quiescent_window"
            if not hard_blockers
            else "resolve_identity_closeout_preflight_blockers_before_apply"
        ),
    }


def compact_confirmation_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    duplicate = _as_mapping(packet.get("duplicate"))
    indexes = _as_mapping(packet.get("identity_audit_indexes"))
    merge_fill = _as_mapping(packet.get("merge_fill"))
    maintenance = _as_mapping(packet.get("maintenance_window"))
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": packet.get("schema"),
        "ok": bool(packet.get("ok")),
        "read_only": bool(packet.get("read_only")),
        "dry_run": bool(packet.get("dry_run")),
        "execution_mode": "production_identity_closeout_confirmation_packet_summary_no_apply",
        "db": packet.get("db"),
        "shared_backup_path": packet.get("shared_backup_path"),
        "shared_backup_exists": bool(packet.get("shared_backup_exists")),
        "duplicate_manifest_path": packet.get("duplicate_manifest_path"),
        "duplicate_manifest_exists": bool(packet.get("duplicate_manifest_exists")),
        "latest_anchor_policy": packet.get("latest_anchor_policy"),
        "duplicate": {
            "group_count": duplicate.get("group_count"),
            "row_count": duplicate.get("row_count"),
            "reviewed_apply_contract_ok": bool(duplicate.get("reviewed_apply_contract_ok")),
            "reviewed_apply_operation_count": duplicate.get("reviewed_apply_operation_count"),
            "reviewed_apply_blockers": list(duplicate.get("reviewed_apply_blockers") or []),
        },
        "identity_audit_indexes": {
            "contract_ok": bool(indexes.get("contract_ok")),
            "already_satisfied": bool(indexes.get("already_satisfied")),
            "needs_apply": bool(indexes.get("needs_apply")),
            "ddl_count": int(indexes.get("ddl_count") or 0),
            "contract_blocker_count": int(indexes.get("contract_blocker_count") or 0),
            "blockers": list(indexes.get("blockers") or []),
            "apply_blockers": list(indexes.get("apply_blockers") or []),
        },
        "merge_fill": {
            "contract_ok": bool(merge_fill.get("contract_ok")),
            "contract_stage": merge_fill.get("contract_stage"),
            "expected_to_be_ready_after_duplicate_repair_apply": bool(
                merge_fill.get("expected_to_be_ready_after_duplicate_repair_apply")
            ),
            "operation_count": int(merge_fill.get("operation_count") or 0),
            "candidate_count": int(merge_fill.get("candidate_count") or 0),
            "safe_candidate_count": int(merge_fill.get("safe_candidate_count") or 0),
            "blocked_candidate_count": int(merge_fill.get("blocked_candidate_count") or 0),
            "blockers": list(merge_fill.get("blockers") or []),
        },
        "maintenance_window": {
            "ok": bool(maintenance.get("ok")),
            "writer_quiescent": bool(maintenance.get("writer_quiescent")),
            "required_stop_labels": list(maintenance.get("required_stop_labels") or []),
            "blockers": list(maintenance.get("blockers") or []),
            "next_boundary": maintenance.get("next_boundary"),
        },
        "control_sequence": [
            {
                "order": row.get("order"),
                "phase": row.get("phase"),
                "step": row.get("step"),
                "writes": bool(row.get("writes")),
                "writes_archive_db": bool(row.get("writes_archive_db")),
                "writes_manifest_file": bool(row.get("writes_manifest_file")),
                "creates_backup": bool(row.get("creates_backup")),
                "required": bool(row.get("required")),
                "confirmation_phrase": row.get("confirmation_phrase"),
                "command": row.get("command"),
            }
            for row in _as_sequence(packet.get("control_sequence"))
        ],
        "confirmation_phrases": dict(_as_mapping(packet.get("confirmation_phrases"))),
        "hard_blockers": list(packet.get("hard_blockers") or []),
        "boundary": dict(_as_mapping(packet.get("boundary"))),
        "guards": dict(_as_mapping(packet.get("guards"))),
        "next_boundary": packet.get("next_boundary"),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--generated-at", default="")
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--limit", type=int, default=1000000)
    parser.add_argument("--duplicate-decision-manifest", type=Path, default=None)
    parser.add_argument("--latest-anchor-policy", default=DEFAULT_LATEST_ANCHOR_POLICY)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    packet = build_confirmation_packet(
        args.db,
        backup_dir=args.backup_dir,
        generated_at=str(args.generated_at or "") or None,
        max_items_output=int(args.max_items_output),
        limit=int(args.limit),
        duplicate_decision_manifest=_load_json_file(args.duplicate_decision_manifest),
        latest_anchor_policy=str(args.latest_anchor_policy or DEFAULT_LATEST_ANCHOR_POLICY),
    )
    payload: Mapping[str, Any] = compact_confirmation_packet(packet) if args.summary else packet
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if packet.get("read_only") else 1


if __name__ == "__main__":
    raise SystemExit(main())
