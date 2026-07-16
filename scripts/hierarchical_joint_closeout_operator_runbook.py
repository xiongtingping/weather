#!/usr/bin/env python3
"""Read-only master runbook for the remaining hierarchical joint closeout blockers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import awc_legacy_terminal_closeout_operator_runbook as awc_runbook  # noqa: E402
from scripts import production_identity_closeout_operator_runbook as identity_runbook  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "hierarchical_joint_closeout_operator_runbook_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _load_json_mapping(path: Path | str) -> dict[str, Any]:
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise ValueError(f"expected JSON object in {path}")
    return dict(payload)


def _guards() -> dict[str, bool]:
    return {
        "executes_sql_writes": False,
        "writes_archive_db": False,
        "writes_awc_hard_source_signals": False,
        "writes_signal_to_order_timing": False,
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


def _step_by_name(runbook: Mapping[str, Any], step: str) -> dict[str, Any]:
    for row in _as_sequence(runbook.get("operator_sequence")):
        row_map = _as_mapping(row)
        if row_map.get("step") == step:
            return row_map
    return {}


def _clone_step(
    source: Mapping[str, Any],
    *,
    order: int,
    phase: str | None = None,
    step: str | None = None,
    component: str,
    blocked_until: Sequence[str] | None = None,
) -> dict[str, Any]:
    row = dict(source)
    row["order"] = int(order)
    row["component"] = component
    if phase is not None:
        row["phase"] = phase
    if step is not None:
        row["step"] = step
    if blocked_until is not None:
        row["blocked_until"] = [str(item) for item in blocked_until if str(item)]
    return row


def _maintenance_step(runbook: Mapping[str, Any], *, step_name: str) -> dict[str, Any]:
    return _step_by_name(runbook, step_name)


def _union(values: Sequence[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in out:
            out.append(text)
    return out


def _build_master_sequence(
    *,
    awc: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> list[dict[str, Any]]:
    awc_steps = {
        name: _step_by_name(awc, name)
        for name in (
            "emit_review_report",
            "emit_decision_manifest_template",
            "operator_reviews_decision_manifest",
            "audit_reviewed_decision_manifest",
            "create_fresh_backup",
            "apply_awc_legacy_terminal_closeout",
            "verify_awc_terminal_coverage",
        )
    }
    identity_steps = {
        name: _step_by_name(identity, name)
        for name in (
            "materialize_reviewed_duplicate_manifest",
            "audit_reviewed_duplicate_manifest",
            "review_duplicate_apply_contract",
            "review_identity_audit_index_contract",
            "run_identity_audit_index_temp_drill",
            "run_identity_closeout_temp_drill",
            "create_shared_fresh_backup",
            "apply_identity_audit_indexes",
            "apply_duplicate_fact_snapshot_repair",
            "verify_duplicate_repair",
            "review_identity_merge_fill_contract_after_duplicate_repair",
            "apply_identity_merge_fill",
            "verify_identity_coverage",
            "verify_full_joint_gate",
        )
    }
    pause = _maintenance_step(awc, step_name="pause_writer_services") or _maintenance_step(
        identity, step_name="pause_writer_services"
    )
    verify_quiescence = _maintenance_step(awc, step_name="verify_writer_quiescence") or _maintenance_step(
        identity, step_name="verify_writer_quiescence"
    )
    resume = _maintenance_step(awc, step_name="resume_writer_services") or _maintenance_step(
        identity, step_name="resume_writer_services"
    )
    ordered_sources = [
        ("awc", awc_steps["emit_review_report"], "review", None),
        ("awc", awc_steps["emit_decision_manifest_template"], "review", None),
        ("awc", awc_steps["operator_reviews_decision_manifest"], "review", None),
        ("awc", awc_steps["audit_reviewed_decision_manifest"], "preflight", None),
        ("identity", identity_steps["materialize_reviewed_duplicate_manifest"], "preflight", None),
        ("identity", identity_steps["audit_reviewed_duplicate_manifest"], "preflight", None),
        ("identity", identity_steps["review_duplicate_apply_contract"], "preflight", None),
        ("identity", identity_steps["review_identity_audit_index_contract"], "preflight", None),
        ("identity", identity_steps["run_identity_audit_index_temp_drill"], "preflight", None),
        ("identity", identity_steps["run_identity_closeout_temp_drill"], "preflight", None),
        ("system", pause, "maintenance", ["reviewed_manifests_and_contracts_pass"]),
        ("system", verify_quiescence, "maintenance", ["writer_services_paused"]),
        ("awc", awc_steps["create_fresh_backup"], "backup", ["writer_quiescence_passes"]),
        (
            "awc",
            awc_steps["apply_awc_legacy_terminal_closeout"],
            "apply",
            ["awc_reviewed_manifest_audit_passes", "awc_fresh_backup_exists", "writer_quiescence_passes"],
        ),
        ("awc", awc_steps["verify_awc_terminal_coverage"], "verify", ["awc_terminal_closeout_apply_completed"]),
        ("identity", identity_steps["create_shared_fresh_backup"], "backup", ["writer_quiescence_passes"]),
        (
            "identity",
            identity_steps["apply_identity_audit_indexes"],
            "apply",
            ["identity_audit_index_contract_passes", "identity_shared_backup_exists", "writer_quiescence_passes"],
        ),
        (
            "identity",
            identity_steps["apply_duplicate_fact_snapshot_repair"],
            "apply",
            [
                "identity_audit_index_apply_completed",
                "identity_duplicate_contract_passes",
                "identity_shared_backup_exists",
                "writer_quiescence_passes",
            ],
        ),
        ("identity", identity_steps["verify_duplicate_repair"], "verify", ["duplicate_repair_apply_completed"]),
        (
            "identity",
            identity_steps["review_identity_merge_fill_contract_after_duplicate_repair"],
            "preflight",
            ["duplicate_repair_verification_passes"],
        ),
        (
            "identity",
            identity_steps["apply_identity_merge_fill"],
            "apply",
            ["identity_merge_fill_contract_passes", "identity_shared_backup_exists", "writer_quiescence_passes"],
        ),
        ("identity", identity_steps["verify_identity_coverage"], "verify", ["identity_merge_fill_apply_completed"]),
        (
            "identity",
            identity_steps["verify_full_joint_gate"],
            "verify",
            ["awc_terminal_coverage_passes", "identity_coverage_passes"],
        ),
        ("system", resume, "restore", ["post_apply_verification_complete_or_operator_aborts_after_pause"]),
    ]
    sequence: list[dict[str, Any]] = []
    for index, (component, source, phase, blocked_until) in enumerate(ordered_sources, start=1):
        if not source:
            sequence.append(
                {
                    "order": index,
                    "component": component,
                    "phase": phase,
                    "step": "missing_source_step",
                    "category": "missing",
                    "command": "",
                    "required": True,
                    "read_only_command": True,
                    "writes_archive_db": False,
                    "writes_manifest_file": False,
                    "creates_backup": False,
                    "restarts_launchd": False,
                    "requires_operator_confirmation_phrase": False,
                    "confirmation_phrase": "",
                    "blocked_until": list(blocked_until or []),
                    "execution_allowed_now": False,
                }
            )
            continue
        sequence.append(
            _clone_step(
                source,
                order=index,
                phase=phase,
                component=component,
                blocked_until=blocked_until,
            )
        )
    return sequence


def build_master_runbook(
    db_path: Path | str = DEFAULT_DB,
    *,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
    awc_since_hours: float = 24.0 * 90.0,
    awc_limit: int = 500,
    identity_limit: int = 1000000,
    max_items_output: int = 8,
    precomputed_awc: Mapping[str, Any] | None = None,
    precomputed_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    awc = (
        dict(precomputed_awc)
        if isinstance(precomputed_awc, Mapping)
        else awc_runbook.build_operator_runbook(
            db_path,
            backup_dir=backup_dir,
            generated_at=generated_at,
            since_hours=float(awc_since_hours),
            limit=int(awc_limit),
            max_items_output=int(max_items_output),
        )
    )
    identity = (
        dict(precomputed_identity)
        if isinstance(precomputed_identity, Mapping)
        else identity_runbook.build_operator_runbook(
            db_path,
            backup_dir=backup_dir,
            generated_at=generated_at or None,
            max_items_output=int(max_items_output),
            limit=int(identity_limit),
        )
    )
    return build_master_runbook_from_components(
        db_path=db_path,
        awc=awc,
        identity=identity,
    )


def build_master_runbook_from_components(
    *,
    db_path: Path | str = DEFAULT_DB,
    awc: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    sequence = _build_master_sequence(awc=awc, identity=identity)
    missing_steps = [row for row in sequence if row.get("category") == "missing"]
    start_blockers = _union(
        [
            *_as_sequence(awc.get("start_blockers")),
            *_as_sequence(identity.get("start_blockers")),
            *(["master_runbook_missing_source_steps"] if missing_steps else []),
        ]
    )
    archive_steps = [row.get("step") for row in sequence if bool(row.get("writes_archive_db"))]
    confirmation_phrases = {
        "awc_terminal_closeout": _as_mapping(awc.get("confirmation_phrases")).get(
            "apply_awc_legacy_terminal_closeout"
        ),
        "production_identity_audit_indexes": _as_mapping(identity.get("confirmation_phrases")).get(
            "identity_audit_indexes"
        ),
        "production_duplicate_repair": _as_mapping(identity.get("confirmation_phrases")).get(
            "duplicate_fact_snapshot_repair"
        ),
        "production_identity_merge_fill": _as_mapping(identity.get("confirmation_phrases")).get(
            "identity_merge_fill"
        ),
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": not missing_steps,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "hierarchical_joint_closeout_operator_runbook_no_apply",
        "db": str(Path(db_path).expanduser().resolve()),
        "ready_for_controlled_apply_now": bool(
            awc.get("ready_for_controlled_apply_now")
            and identity.get("ready_for_controlled_apply_now")
            and not missing_steps
        ),
        "start_blockers": start_blockers,
        "next_boundary": (
            "run_master_closeout_operator_sequence"
            if not start_blockers
            else "resolve_awc_and_identity_closeout_start_blockers"
        ),
        "source_runbooks": {
            "awc_terminal_closeout": awc_runbook.compact_operator_runbook(awc),
            "production_identity_closeout": identity_runbook.compact_operator_runbook(identity),
        },
        "operator_sequence": sequence,
        "impact_summary": {
            "archive_db_apply_steps": archive_steps,
            "manifest_write_steps": [
                row.get("step") for row in sequence if bool(row.get("writes_manifest_file"))
            ],
            "backup_steps": [row.get("step") for row in sequence if bool(row.get("creates_backup"))],
            "service_control_steps": [
                row.get("step") for row in sequence if bool(row.get("restarts_launchd"))
            ],
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "changes_parameters": False,
            "historical_backfill": False,
        },
        "confirmation_phrases": confirmation_phrases,
        "guards": _guards(),
    }


def compact_master_runbook(runbook: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": runbook.get("schema"),
        "ok": bool(runbook.get("ok")),
        "read_only": bool(runbook.get("read_only")),
        "dry_run": bool(runbook.get("dry_run")),
        "execution_mode": "hierarchical_joint_closeout_operator_runbook_summary_no_apply",
        "db": runbook.get("db"),
        "ready_for_controlled_apply_now": bool(runbook.get("ready_for_controlled_apply_now")),
        "start_blockers": list(runbook.get("start_blockers") or []),
        "next_boundary": runbook.get("next_boundary"),
        "impact_summary": dict(_as_mapping(runbook.get("impact_summary"))),
        "confirmation_phrases": dict(_as_mapping(runbook.get("confirmation_phrases"))),
        "operator_sequence": [
            {
                "order": row.get("order"),
                "component": row.get("component"),
                "phase": row.get("phase"),
                "step": row.get("step"),
                "category": row.get("category"),
                "command": row.get("command"),
                "writes_archive_db": bool(row.get("writes_archive_db")),
                "writes_manifest_file": bool(row.get("writes_manifest_file")),
                "creates_backup": bool(row.get("creates_backup")),
                "restarts_launchd": bool(row.get("restarts_launchd")),
                "requires_operator_confirmation_phrase": bool(row.get("requires_operator_confirmation_phrase")),
                "confirmation_phrase": row.get("confirmation_phrase") or "",
                "blocked_until": list(row.get("blocked_until") or []),
                "execution_allowed_now": bool(row.get("execution_allowed_now")),
            }
            for row in _as_sequence(runbook.get("operator_sequence"))
        ],
        "guards": dict(_as_mapping(runbook.get("guards"))),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--generated-at", default="")
    parser.add_argument("--awc-since-hours", type=float, default=24.0 * 90.0)
    parser.add_argument("--awc-limit", type=int, default=500)
    parser.add_argument("--identity-limit", type=int, default=1000000)
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--awc-runbook-json", type=Path, default=None)
    parser.add_argument("--identity-runbook-json", type=Path, default=None)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    runbook = build_master_runbook(
        args.db,
        backup_dir=args.backup_dir,
        generated_at=str(args.generated_at or "") or None,
        awc_since_hours=float(args.awc_since_hours),
        awc_limit=int(args.awc_limit),
        identity_limit=int(args.identity_limit),
        max_items_output=int(args.max_items_output),
        precomputed_awc=(
            _load_json_mapping(args.awc_runbook_json) if args.awc_runbook_json else None
        ),
        precomputed_identity=(
            _load_json_mapping(args.identity_runbook_json) if args.identity_runbook_json else None
        ),
    )
    payload: Mapping[str, Any] = compact_master_runbook(runbook) if args.summary else runbook
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if runbook.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
