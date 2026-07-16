#!/usr/bin/env python3
"""Read-only operator runbook for duplicate fact_snapshot_id repair.

The runbook packages duplicate fact_snapshot groups, reviewed manifest
candidates, audit commands, apply-contract commands, and the controlled apply
command. It never writes archive.db, never creates a manifest file, never
creates a backup, and never restarts services.
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

from scripts import production_identity_duplicate_fact_snapshot_apply as duplicate_apply  # noqa: E402
from scripts import production_identity_duplicate_fact_snapshot_apply_contract as apply_contract  # noqa: E402
from scripts import production_identity_duplicate_fact_snapshot_packet as packet_runtime  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "production_identity_duplicate_fact_snapshot_operator_runbook_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"


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
        "writes_manifest_file": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
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
    return backup_dir / f"{db.name}.production-duplicate-fact-snapshot-{_safe_stamp(generated_at)}.manifest.json"


def _backup_path(db: Path, backup_dir: Path, generated_at: str) -> Path:
    return backup_dir / f"{db.name}.production-duplicate-fact-snapshot-{_safe_stamp(generated_at)}.bak"


def _candidate_review_rows(packet: Mapping[str, Any], *, max_items_output: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for decision in _as_sequence(_as_mapping(packet.get("operator_manifest_template")).get("decisions")):
        item = dict(_as_mapping(decision))
        if not item:
            continue
        rows.append(
            {
                "row_id": item.get("row_id"),
                "decision": item.get("decision"),
                "current_fact_snapshot_id": item.get("current_fact_snapshot_id"),
                "proposed_fact_snapshot_id": item.get("proposed_fact_snapshot_id"),
                "requires_payload_update": bool(item.get("requires_payload_update")),
                "requires_table_column_update": bool(item.get("requires_table_column_update")),
                "blockers": list(item.get("blockers") or []),
                "operator_review_focus": (
                    "confirm_noncanonical_unique_identity"
                    if item.get("requires_payload_update")
                    else "confirm_canonical_owner"
                ),
            }
        )
    return rows[: max(0, int(max_items_output))]


def build_reviewed_manifest_candidate(
    packet: Mapping[str, Any],
    *,
    latest_anchor_policy: str = "",
    operator: str = "",
    reviewed_at: str = "",
) -> dict[str, Any]:
    template = dict(_as_mapping(packet.get("operator_manifest_template")))
    required = bool(template.get("latest_anchor_policy_required"))
    policy = str(latest_anchor_policy or "").strip()
    if not policy and required:
        policy = "manual_review_required"
    elif policy and policy not in packet_runtime.LATEST_ANCHOR_POLICIES:
        policy = "manual_review_required"
    decisions = []
    for raw in _as_sequence(template.get("decisions")):
        row = dict(_as_mapping(raw))
        row["operator"] = operator
        row["reviewed_at"] = reviewed_at
        row["operator_review_required"] = True
        decisions.append(row)
    return {
        "schema": packet_runtime.MANIFEST_SCHEMA_VERSION,
        "read_only_template": True,
        "operator_review_required": True,
        "latest_anchor_policy": policy,
        "latest_anchor_policy_required": required,
        "allowed_latest_anchor_policies": list(packet_runtime.LATEST_ANCHOR_POLICIES),
        "decision_count": len(decisions),
        "decisions": decisions,
        "boundary": "duplicate_fact_snapshot_reviewed_manifest_candidate_no_file_write_no_archive_write",
    }


def _decision_counts(manifest: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in _as_sequence(manifest.get("decisions")):
        decision = str(_as_mapping(row).get("decision") or "missing")
        counts[decision] = counts.get(decision, 0) + 1
    return dict(sorted(counts.items()))


def build_operator_runbook(
    db_path: Path | str = DEFAULT_DB,
    *,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
    max_items_output: int = 20,
    decision_manifest: Mapping[str, Any] | None = None,
    latest_anchor_policy: str = "",
) -> dict[str, Any]:
    generated = generated_at or _now_utc()
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    manifest_path = _manifest_output_path(db, backup_root, generated)
    backup_path = _backup_path(db, backup_root, generated)
    packet = packet_runtime.build_packet(
        db,
        immutable=True,
        max_items_output=1000000,
        decision_manifest=decision_manifest,
    )
    reviewed_manifest = build_reviewed_manifest_candidate(
        packet,
        latest_anchor_policy=latest_anchor_policy,
        reviewed_at=generated,
    )
    reviewed_contract = apply_contract.build_contract(
        db,
        decision_manifest=reviewed_manifest,
        immutable=True,
        max_items_output=max_items_output,
    )
    existing_contract = apply_contract.build_contract(
        db,
        decision_manifest=decision_manifest,
        immutable=True,
        max_items_output=max_items_output,
    )
    template_command = (
        "python3 scripts/production_identity_duplicate_fact_snapshot_operator_runbook.py "
        f"--db {_q(db)} --emit-reviewed-manifest-json --latest-anchor-policy <policy> "
        f"> {_q(manifest_path)}"
    )
    audit_command = (
        "python3 scripts/production_identity_duplicate_fact_snapshot_packet.py "
        f"--db {_q(db)} --summary --max-items-output {int(max_items_output)} "
        f"--decision-manifest {_q(manifest_path)}"
    )
    contract_command = (
        "python3 scripts/production_identity_duplicate_fact_snapshot_apply_contract.py "
        f"--db {_q(db)} --summary --max-items-output {int(max_items_output)} "
        f"--decision-manifest {_q(manifest_path)}"
    )
    backup_command = f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup_path)}\""
    writer_quiescence_command = (
        f"python3 scripts/controlled_apply_maintenance_window_packet.py --db {_q(db)} "
        f"--summary --max-items-output {int(max_items_output)}"
    )
    apply_command = (
        "python3 scripts/production_identity_duplicate_fact_snapshot_apply.py "
        f"--db {_q(db)} --decision-manifest {_q(manifest_path)} --apply "
        f"--confirm {duplicate_apply.CONFIRMATION_PHRASE} --backup {_q(backup_path)} --writer-quiescent"
    )
    merge_fill_recheck_command = (
        "python3 scripts/production_identity_merge_fill_plan.py "
        f"--db {_q(db)} --summary --limit 1000000 --max-items-output {int(max_items_output)} "
        f"--duplicate-decision-manifest {_q(manifest_path)}"
    )
    coverage_command = (
        "python3 scripts/production_identity_coverage_audit.py "
        f"--db {_q(db)} --summary --max-items-output {int(max_items_output)}"
    )
    reviewed_ready = bool(reviewed_contract.get("ok"))
    existing_ready = bool(existing_contract.get("ok"))
    checklist = [
        {
            "order": 1,
            "step": "review_duplicate_fact_snapshot_groups",
            "command": "inspect operator_runbook.candidate_review_table",
            "writes": False,
            "required": bool(packet.get("duplicate_row_count")),
        },
        {
            "order": 2,
            "step": "choose_latest_anchor_policy_if_required",
            "command": "select one allowed_latest_anchor_policy; never leave manual_review_required for apply",
            "writes": False,
            "required": bool(_as_mapping(packet.get("operator_manifest_template")).get("latest_anchor_policy_required")),
            "allowed_latest_anchor_policies": list(packet_runtime.LATEST_ANCHOR_POLICIES),
        },
        {
            "order": 3,
            "step": "materialize_reviewed_manifest_if_operator_accepts",
            "command": template_command,
            "writes": True,
            "writes_archive_db": False,
            "writes_manifest_file": True,
            "required": bool(packet.get("duplicate_row_count")),
            "operator_review_required": True,
            "output": str(manifest_path),
        },
        {
            "order": 4,
            "step": "audit_reviewed_manifest",
            "command": audit_command,
            "writes": False,
            "required": bool(packet.get("duplicate_row_count")),
        },
        {
            "order": 5,
            "step": "review_apply_contract",
            "command": contract_command,
            "writes": False,
            "required": bool(packet.get("duplicate_row_count")),
        },
        {
            "order": 6,
            "step": "create_fresh_backup",
            "command": backup_command,
            "writes": True,
            "writes_archive_db": False,
            "creates_backup": True,
            "required": bool(packet.get("duplicate_row_count")),
            "output": str(backup_path),
        },
        {
            "order": 7,
            "step": "verify_writer_quiescence",
            "command": writer_quiescence_command,
            "writes": False,
            "required": bool(packet.get("duplicate_row_count")),
        },
        {
            "order": 8,
            "step": "apply_duplicate_fact_snapshot_repair",
            "command": apply_command,
            "writes": True,
            "writes_archive_db": True,
            "required": reviewed_ready,
            "blocked_by_runbook": bool(packet.get("duplicate_row_count")) and not reviewed_ready,
            "confirmation_phrase": duplicate_apply.CONFIRMATION_PHRASE,
        },
        {
            "order": 9,
            "step": "recheck_identity_merge_fill_plan",
            "command": merge_fill_recheck_command,
            "writes": False,
            "required": bool(packet.get("duplicate_row_count")),
        },
        {
            "order": 10,
            "step": "recheck_identity_coverage",
            "command": coverage_command,
            "writes": False,
            "required": True,
        },
    ]
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "generated_at": generated,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "production_identity_duplicate_fact_snapshot_operator_runbook_no_apply",
        "db": str(db),
        "duplicate_group_count": int(packet.get("duplicate_group_count") or 0),
        "duplicate_row_count": int(packet.get("duplicate_row_count") or 0),
        "candidate_review_table": _candidate_review_rows(packet, max_items_output=max_items_output),
        "candidate_review_table_truncated": int(packet.get("duplicate_row_count") or 0) > int(max_items_output),
        "undecided_manifest_template": packet.get("operator_manifest_template"),
        "reviewed_manifest_candidate": reviewed_manifest,
        "reviewed_manifest_decision_counts": _decision_counts(reviewed_manifest),
        "reviewed_apply_contract_summary": apply_contract.compact_contract(
            reviewed_contract,
            max_items_output=max_items_output,
        ),
        "existing_apply_contract_summary": apply_contract.compact_contract(
            existing_contract,
            max_items_output=max_items_output,
        ),
        "reviewed_manifest_would_clear_duplicate_apply_contract": reviewed_ready,
        "existing_manifest_clears_duplicate_apply_contract": existing_ready,
        "manifest_output_path": str(manifest_path),
        "manifest_output_exists": manifest_path.exists(),
        "backup_path": str(backup_path),
        "backup_exists": backup_path.exists(),
        "checklist": checklist,
        "boundary": {
            "operator_review_required": True,
            "reviewed_manifest_candidate_only": True,
            "no_archive_db_write": True,
            "no_manifest_file_write": True,
            "no_backup_creation": True,
            "controlled_apply_requires_separate_apply_entrypoint": True,
        },
        "guards": _guards(),
        "next_boundary": (
            "operator_reviews_manifest_policy_then_runs_controlled_duplicate_apply"
            if packet.get("duplicate_row_count")
            else "duplicate_fact_snapshot_apply_not_required"
        ),
    }


def compact_operator_runbook(runbook: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": runbook.get("schema"),
        "ok": bool(runbook.get("ok")),
        "read_only": bool(runbook.get("read_only")),
        "dry_run": bool(runbook.get("dry_run")),
        "execution_mode": "production_identity_duplicate_fact_snapshot_operator_runbook_summary_no_apply",
        "db": runbook.get("db"),
        "duplicate_group_count": int(runbook.get("duplicate_group_count") or 0),
        "duplicate_row_count": int(runbook.get("duplicate_row_count") or 0),
        "reviewed_manifest_would_clear_duplicate_apply_contract": bool(
            runbook.get("reviewed_manifest_would_clear_duplicate_apply_contract")
        ),
        "existing_manifest_clears_duplicate_apply_contract": bool(
            runbook.get("existing_manifest_clears_duplicate_apply_contract")
        ),
        "manifest_output_path": runbook.get("manifest_output_path"),
        "manifest_output_exists": bool(runbook.get("manifest_output_exists")),
        "backup_path": runbook.get("backup_path"),
        "backup_exists": bool(runbook.get("backup_exists")),
        "reviewed_apply_contract_summary": dict(_as_mapping(runbook.get("reviewed_apply_contract_summary"))),
        "checklist": [
            {
                "order": row.get("order"),
                "step": row.get("step"),
                "writes": bool(row.get("writes")),
                "writes_archive_db": bool(row.get("writes_archive_db")),
                "writes_manifest_file": bool(row.get("writes_manifest_file")),
                "required": bool(row.get("required")),
                "command": row.get("command"),
            }
            for row in _as_sequence(runbook.get("checklist"))
        ],
        "boundary": dict(_as_mapping(runbook.get("boundary"))),
        "guards": dict(_as_mapping(runbook.get("guards"))),
        "next_boundary": runbook.get("next_boundary"),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--decision-manifest", type=Path, default=None)
    parser.add_argument("--latest-anchor-policy", default="")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--emit-undecided-manifest-json", action="store_true")
    parser.add_argument("--emit-reviewed-manifest-json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    existing_manifest = _load_json_file(args.decision_manifest)
    runbook = build_operator_runbook(
        args.db,
        backup_dir=args.backup_dir,
        max_items_output=int(args.max_items_output),
        decision_manifest=existing_manifest,
        latest_anchor_policy=str(args.latest_anchor_policy or ""),
    )
    if args.emit_undecided_manifest_json:
        print(json.dumps(runbook.get("undecided_manifest_template") or {}, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0
    if args.emit_reviewed_manifest_json:
        print(json.dumps(runbook.get("reviewed_manifest_candidate") or {}, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0
    payload = compact_operator_runbook(runbook) if args.summary else runbook
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if runbook.get("read_only") else 1


if __name__ == "__main__":
    raise SystemExit(main())
