#!/usr/bin/env python3
"""Read-only operator runbook for legacy AWC signal-anchor decisions.

The runbook packages the legacy AWC anchor candidates, review evidence, decision
manifest templates, and recheck commands. It never writes archive.db, never
materializes a manifest file, never rebuilds signal ledgers, never fetches
weather sources, and never places orders.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import legacy_awc_signal_anchor_decision_packet as decision_packet  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "legacy_awc_signal_anchor_operator_runbook_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_stamp(value: str) -> str:
    return (
        str(value or "")
        .replace(":", "")
        .replace("-", "")
        .replace("+00:00", "Z")
        .replace("Z", "Z")
    )


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
        "writes_event_fact_summary_cache": False,
        "writes_signal_to_order_timing": False,
        "writes_auto_trade_decisions": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _review_reason(candidate: Mapping[str, Any]) -> str:
    parts = [
        "Operator reviewed legacy AWC lower-bound anchor candidate",
        f"fact_id={candidate.get('fact_id')}",
        f"event_date={candidate.get('event_date')}",
        f"source={candidate.get('source')}",
        f"constraint_mode={candidate.get('constraint_mode')}",
    ]
    target = candidate.get("target_bucket")
    if target not in (None, "", [], {}):
        parts.append(f"target_bucket={target}")
    value_c = candidate.get("value_c")
    if value_c not in (None, "", [], {}):
        parts.append(f"value_c={value_c}")
    return "; ".join(parts) + "; no automatic signal-ledger backfill is allowed."


def _candidate_rows(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in _as_sequence(packet.get("candidate_items")):
        item = dict(_as_mapping(raw))
        if not item:
            continue
        rows.append(
            {
                "decision_id": item.get("decision_id"),
                "fact_id": item.get("fact_id"),
                "slug": item.get("slug"),
                "event_date": item.get("event_date"),
                "source": item.get("source"),
                "constraint_mode": item.get("constraint_mode"),
                "target_bucket": item.get("target_bucket"),
                "value_c": item.get("value_c"),
                "value_at": item.get("value_at"),
                "latest_at": item.get("latest_at"),
                "fetched_at": item.get("fetched_at"),
                "signal_id": item.get("signal_id"),
                "hard_source_signal_expected": item.get("hard_source_signal_expected"),
                "no_apply_probe_command": item.get("no_apply_probe_command"),
                "allowed_operator_decisions": list(item.get("allowed_operator_decisions") or []),
                "recommended_first_review": "inspect_replay_probe_then_choose_reviewed_no_signal_or_dedicated_writer",
            }
        )
    return rows


def build_reviewed_no_signal_manifest(
    packet: Mapping[str, Any],
    *,
    operator: str = "",
    reviewed_at: str = "",
) -> dict[str, Any]:
    template = _as_mapping(packet.get("decision_manifest_template"))
    candidates = {str(row.get("decision_id") or ""): row for row in _candidate_rows(packet)}
    decisions = []
    for raw in _as_sequence(template.get("decisions")):
        row = dict(_as_mapping(raw))
        decision_id = str(row.get("decision_id") or "")
        candidate = candidates.get(decision_id, {})
        row["decision"] = decision_packet.DECISION_REVIEWED_NO_SIGNAL
        row["review_reason"] = _review_reason(candidate or row)
        row["source_evidence_ref"] = (
            candidate.get("no_apply_probe_command")
            or row.get("source_evidence_ref")
            or ""
        )
        row["operator"] = operator
        row["reviewed_at"] = reviewed_at
        decisions.append(row)
    return {
        "schema": decision_packet.MANIFEST_SCHEMA_VERSION,
        "read_only_template": True,
        "operator_review_required": True,
        "decision_count": len(decisions),
        "decisions": decisions,
        "boundary": "reviewed_no_signal_manifest_candidate_no_file_write_no_signal_ledger_write",
    }


def _manifest_output_path(db: Path, backup_dir: Path, generated_at: str) -> Path:
    return backup_dir / f"{db.name}.legacy-awc-signal-anchor-{_safe_stamp(generated_at)}.manifest.json"


def _scope_args(
    *,
    db: Path,
    since_days: int,
    since_date: str,
    slug: str,
    event_date: str,
    market_scope: str,
    limit: int,
    max_items_output: int,
) -> list[str]:
    args = [
        "--db",
        _q(db),
        "--since-days",
        str(int(since_days)),
        "--max-items-output",
        str(int(max_items_output)),
    ]
    if since_date:
        args.extend(["--since-date", _q(since_date)])
    if slug:
        args.extend(["--slug", _q(slug)])
    if event_date:
        args.extend(["--event-date", _q(event_date)])
    if market_scope:
        args.extend(["--market-scope", _q(market_scope)])
    if int(limit) != 100:
        args.extend(["--limit", str(int(limit))])
    return args


def build_operator_runbook_from_packet(
    packet: Mapping[str, Any],
    *,
    db_path: Path | str = DEFAULT_DB,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 20,
    existing_decision_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    generated = generated_at or _now_utc()
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    manifest_path = _manifest_output_path(db, backup_root, generated)
    rows = _candidate_rows(packet)
    reviewed_manifest = build_reviewed_no_signal_manifest(
        packet,
        reviewed_at=generated,
    )
    reviewed_audit = decision_packet.audit_decision_manifest(rows, reviewed_manifest)
    existing_audit = decision_packet.audit_decision_manifest(rows, existing_decision_manifest)
    scope_args = _scope_args(
        db=db,
        since_days=since_days,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
        max_items_output=max_items_output,
    )
    emit_reviewed_command = (
        "python3 scripts/legacy_awc_signal_anchor_operator_runbook.py "
        + " ".join([*scope_args, "--emit-reviewed-no-signal-manifest-json"])
    )
    if manifest_path:
        emit_reviewed_command = f"{emit_reviewed_command} > {_q(manifest_path)}"
    recheck_decision_command = (
        "python3 scripts/legacy_awc_signal_anchor_decision_packet.py "
        + " ".join([*scope_args, "--decision-manifest", _q(manifest_path), "--summary"])
    )
    recheck_preflight_command = (
        "python3 scripts/daily_review_production_apply_preflight_packet.py "
        + " ".join(
            [
                *scope_args,
                "--legacy-awc-anchor-decision-manifest",
                _q(manifest_path),
                "--summary",
            ]
        )
    )
    checklist = [
        {
            "order": 1,
            "step": "review_legacy_awc_anchor_candidates",
            "command": "inspect legacy_awc_signal_anchor_operator_runbook.candidate_review_table",
            "writes": False,
            "required": bool(rows),
        },
        {
            "order": 2,
            "step": "run_replay_probe_for_each_candidate",
            "command": [
                str(row.get("no_apply_probe_command") or "")
                for row in rows
                if str(row.get("no_apply_probe_command") or "")
            ],
            "writes": False,
            "required": bool(rows),
        },
        {
            "order": 3,
            "step": "materialize_reviewed_no_signal_manifest_if_operator_accepts",
            "command": emit_reviewed_command,
            "writes": True,
            "writes_archive_db": False,
            "writes_manifest_file": True,
            "required": bool(rows),
            "operator_review_required": True,
            "output": str(manifest_path),
        },
        {
            "order": 4,
            "step": "recheck_legacy_awc_decision_packet",
            "command": recheck_decision_command,
            "writes": False,
            "required": bool(rows),
        },
        {
            "order": 5,
            "step": "recheck_daily_review_production_preflight",
            "command": recheck_preflight_command,
            "writes": False,
            "required": bool(rows),
        },
    ]
    reviewed_would_clear = bool(
        rows
        and reviewed_audit.get("acceptance_exception_complete")
        and not reviewed_audit.get("hard_blockers")
    )
    existing_manifest_ready = bool(
        rows
        and existing_audit.get("acceptance_exception_complete")
        and not existing_audit.get("hard_blockers")
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "generated_at": generated,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "legacy_awc_signal_anchor_operator_runbook_no_apply",
        "db": str(db),
        "scope": {
            "since_days": int(since_days),
            "since_date": since_date,
            "slug": slug,
            "event_date": event_date,
            "market_scope": market_scope,
            "limit": int(limit),
            "max_items_output": int(max_items_output),
        },
        "candidate_count": len(rows),
        "candidate_review_table": rows,
        "undecided_manifest_template": packet.get("decision_manifest_template"),
        "reviewed_no_signal_manifest_candidate": reviewed_manifest,
        "reviewed_no_signal_manifest_audit": reviewed_audit,
        "existing_decision_manifest_audit": existing_audit,
        "reviewed_no_signal_would_clear_legacy_start_blocker": reviewed_would_clear,
        "existing_manifest_clears_legacy_start_blocker": existing_manifest_ready,
        "manifest_output_path": str(manifest_path),
        "manifest_output_exists": manifest_path.exists(),
        "checklist": checklist,
        "boundary": {
            "operator_review_required": True,
            "automatic_signal_ledger_rebuild_included": False,
            "reviewed_no_signal_manifest_is_candidate_only": True,
            "manual_rebuild_requires_dedicated_writer": True,
            "no_archive_db_write": True,
            "no_manifest_file_write": True,
        },
        "guards": _guards(),
        "next_boundary": (
            "operator_reviews_and_materializes_manifest_then_rechecks_preflight"
            if rows
            else "no_legacy_awc_signal_anchor_candidates"
        ),
    }


def compact_operator_runbook(runbook: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": runbook.get("schema"),
        "ok": bool(runbook.get("ok")),
        "read_only": bool(runbook.get("read_only")),
        "dry_run": bool(runbook.get("dry_run")),
        "execution_mode": "legacy_awc_signal_anchor_operator_runbook_summary_no_apply",
        "db": runbook.get("db"),
        "scope": dict(_as_mapping(runbook.get("scope"))),
        "candidate_count": int(runbook.get("candidate_count") or 0),
        "reviewed_no_signal_would_clear_legacy_start_blocker": bool(
            runbook.get("reviewed_no_signal_would_clear_legacy_start_blocker")
        ),
        "existing_manifest_clears_legacy_start_blocker": bool(
            runbook.get("existing_manifest_clears_legacy_start_blocker")
        ),
        "manifest_output_path": runbook.get("manifest_output_path"),
        "manifest_output_exists": bool(runbook.get("manifest_output_exists")),
        "reviewed_no_signal_manifest_audit": {
            "decision_counts": dict(
                _as_mapping(
                    _as_mapping(runbook.get("reviewed_no_signal_manifest_audit")).get("decision_counts")
                )
            ),
            "hard_blockers": list(
                _as_mapping(runbook.get("reviewed_no_signal_manifest_audit")).get("hard_blockers")
                or []
            ),
            "acceptance_exception_complete": bool(
                _as_mapping(runbook.get("reviewed_no_signal_manifest_audit")).get(
                    "acceptance_exception_complete"
                )
            ),
        },
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


def build_operator_runbook(
    db_path: Path | str = DEFAULT_DB,
    *,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 20,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
    decision_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    operation_max_items = max(1, int(limit), int(max_items_output))
    packet = decision_packet.build_decision_packet(
        db_path,
        since_days=since_days,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
        max_items_output=operation_max_items,
        decision_manifest=decision_manifest,
    )
    return build_operator_runbook_from_packet(
        packet,
        db_path=db_path,
        backup_dir=backup_dir,
        generated_at=generated_at,
        since_days=since_days,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
        max_items_output=max_items_output,
        existing_decision_manifest=decision_manifest,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since-days", type=int, default=45)
    parser.add_argument("--since-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--decision-manifest", type=Path, default=None)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--emit-undecided-manifest-json", action="store_true")
    parser.add_argument("--emit-reviewed-no-signal-manifest-json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    manifest = decision_packet.load_decision_manifest_file(args.decision_manifest)
    runbook = build_operator_runbook(
        args.db,
        since_days=int(args.since_days),
        since_date=args.since_date,
        slug=args.slug,
        event_date=args.event_date,
        market_scope=args.market_scope,
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        backup_dir=args.backup_dir,
        decision_manifest=manifest,
    )
    if args.emit_undecided_manifest_json:
        print(json.dumps(runbook.get("undecided_manifest_template") or {}, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0
    if args.emit_reviewed_no_signal_manifest_json:
        print(json.dumps(runbook.get("reviewed_no_signal_manifest_candidate") or {}, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0
    payload = compact_operator_runbook(runbook) if args.summary else runbook
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if runbook.get("read_only") else 1


if __name__ == "__main__":
    raise SystemExit(main())
