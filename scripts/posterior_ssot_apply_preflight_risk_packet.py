#!/usr/bin/env python3
"""Read-only preflight and risk packet for posterior SSOT repair apply.

This is the last no-apply gate before an operator chooses to run a controlled
apply command. It consumes the posterior SSOT confirmation packet, rebuilds the
candidate final-lock writer plan in no-apply mode, and reports expected writes,
blockers, rollback commands, and post-apply verification commands.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
for import_path in (ROOT, SCRIPT_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

import posterior_ssot_apply_confirmation_packet as confirmation_packet


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "posterior_ssot_apply_preflight_risk_packet_v1"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_stamp(value: str) -> str:
    return str(value or "").replace("-", "").replace(":", "").replace(".", "").replace("+00:00", "Z")


def _q(value: Path | str) -> str:
    return shlex.quote(str(value))


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_json(raw: Any) -> Any:
    try:
        parsed = json.loads(str(raw or ""))
    except Exception:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_fact_ids(value: str | Sequence[Any] | None = None) -> list[int]:
    values: Sequence[Any]
    if value is None:
        values = []
    elif isinstance(value, str):
        values = value.replace(";", ",").split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        values = [value]
    out: list[int] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            fact_id = int(text)
        except Exception:
            continue
        if fact_id > 0 and fact_id not in out:
            out.append(fact_id)
    return out


def _load_fact_row(conn: sqlite3.Connection, fact_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        FROM event_fact_history
        WHERE id = ?
        """,
        (int(fact_id),),
    ).fetchone()


def _latest_fact_id(conn: sqlite3.Connection, *, slug: str, event_date: str) -> int:
    row = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug=? AND event_date=?",
        (slug, event_date),
    ).fetchone()
    return int(row["fact_id"] or 0) if row else 0


def _history_count(conn: sqlite3.Connection, *, slug: str, event_date: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM event_fact_history WHERE slug=? AND event_date=?",
        (slug, event_date),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def _plan_actions(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for action in plan.get("plan_actions") or []:
        item = _as_mapping(action)
        if not item:
            continue
        out.append(
            {
                "action": str(item.get("action") or ""),
                "target_table": str(item.get("target_table") or ""),
                "would_write": bool(item.get("would_write")),
                "apply_status": str(item.get("apply_status") or ""),
                "block_reason": str(item.get("block_reason") or ""),
            }
        )
    return out


def _route_projection_for_candidate(
    conn: sqlite3.Connection,
    candidate: Mapping[str, Any],
    *,
    db_path: Path,
    generated_at: str,
) -> dict[str, Any]:
    from posterior_ssot_projection_runtime import build_fact_projection_no_apply

    fact_id = int(candidate.get("fact_id") or 0)
    row = _load_fact_row(conn, fact_id)
    if row is None:
        return {
            "ok": False,
            "fact_id": fact_id,
            "blockers": ["candidate_fact_row_missing"],
            "expected_actions": [],
        }
    payload = _safe_json(row["fact_json"])
    if not isinstance(payload, Mapping):
        return {
            "ok": False,
            "fact_id": fact_id,
            "blockers": ["candidate_fact_json_invalid"],
            "expected_actions": [],
        }
    slug = str(row["slug"] or payload.get("slug") or "")
    event_date = str(row["event_date"] or payload.get("event_date") or "")
    projection = build_fact_projection_no_apply(
        slug=slug,
        event_date=event_date,
        run_date=str(payload.get("run_date") or row["run_date"] or event_date),
        fact_as_of=str(payload.get("fact_as_of") or row["fact_as_of"] or generated_at),
        payload=payload,
        current_fact_id=int(row["id"] or 0),
        current_fact_as_of=str(row["fact_as_of"] or ""),
        composer_version=str(row["composer_version"] or ""),
        db_path=db_path,
        write_reason="posterior_ssot_apply_preflight_risk_packet",
        enable_apply_contract=True,
        write_latest=True,
    )
    plan = _as_mapping(projection.get("plan"))
    contract = _as_mapping(plan.get("posterior_ssot_apply_contract"))
    actions = _plan_actions(plan)
    blockers: list[str] = []
    if plan.get("ok") is not True:
        blockers.append("posterior_projection_writer_plan_not_ok")
    if plan.get("apply_supported") is not True:
        blockers.append("posterior_projection_writer_plan_apply_contract_not_enabled")
    if contract.get("ok") is not True:
        blockers.append("posterior_ssot_apply_contract_not_ok")
    required_ready = {"append_event_fact_history", "repoint_event_fact_latest"}
    ready_actions = {
        str(action.get("action") or "")
        for action in actions
        if action.get("would_write") and action.get("apply_status") == "ready_for_explicit_apply"
    }
    missing_ready = sorted(required_ready - ready_actions)
    if missing_ready:
        blockers.append("posterior_projection_writer_plan_missing_ready_actions")
    for action in actions:
        if action.get("would_write") and action.get("apply_status") != "ready_for_explicit_apply":
            blockers.append("posterior_projection_writer_plan_has_blocked_write_action")
    return {
        "ok": not blockers,
        "fact_id": fact_id,
        "slug": slug,
        "event_date": event_date,
        "current_latest_fact_id": _latest_fact_id(
            conn,
            slug=slug,
            event_date=event_date,
        ),
        "current_history_count": _history_count(
            conn,
            slug=slug,
            event_date=event_date,
        ),
        "writer_plan_schema": plan.get("schema"),
        "writer_plan_ok": bool(plan.get("ok")),
        "writer_plan_apply_supported": bool(plan.get("apply_supported")),
        "writer_plan_write_latest": bool(plan.get("write_latest")),
        "writer_projection_mode": str(projection.get("projection_mode") or ""),
        "writer_projection_context": dict(_as_mapping(projection.get("context"))),
        "writer_projection_issues": list(projection.get("issues") or []),
        "posterior_ssot_apply_contract": dict(contract),
        "expected_actions": actions,
        "expected_write_tables": sorted(
            {
                str(action.get("target_table") or "")
                for action in actions
                if action.get("would_write")
            }
        ),
        "blockers": blockers,
    }


def _risk_level(blockers: Sequence[str], candidate_count: int, expected_write_tables: Sequence[str]) -> str:
    if blockers:
        return "blocked"
    if candidate_count != 1:
        return "high"
    allowed_tables = {"event_fact_history", "event_fact_latest", "event_fact_summary_cache"}
    if set(str(table) for table in expected_write_tables) - allowed_tables:
        return "high"
    return "controlled_low_after_operator_confirmation"


def _backup_path(db: Path, backup_dir: Path, generated_at: str) -> Path:
    return backup_dir / f"{db.name}.posterior-ssot-preflight-{_safe_stamp(generated_at)}.bak"


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_event_fact_summary_cache": False,
        "writes_auto_trade_decisions": False,
        "writes_daily_weather_reviews": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "restarts_launchd": False,
        "executes_sql_writes": False,
    }


def build_preflight_risk_packet(
    db_path: Path | str = DEFAULT_DB,
    *,
    fact_id: str = "",
    fact_ids: str | Sequence[int] | None = None,
    slug: str = "",
    event_date: str = "",
    since_hours: float = 168.0,
    limit: int = 100,
    latest_only: bool = True,
    max_items_output: int = 20,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated = generated_at or _now_utc()
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    backup = _backup_path(db, backup_root, generated)
    explicit_fact_ids = _parse_fact_ids([*_parse_fact_ids(fact_id), *_parse_fact_ids(fact_ids)])
    confirmation = confirmation_packet.build_confirmation_packet(
        db_path=db,
        fact_id=fact_id,
        fact_ids=explicit_fact_ids,
        slug=slug,
        event_date=event_date,
        since_hours=float(since_hours),
        limit=int(limit),
        latest_only=bool(latest_only),
        max_items_output=int(max_items_output),
        backup_dir=backup_root,
        generated_at=generated,
    )
    candidates = [dict(_as_mapping(item)) for item in confirmation.get("candidates") or []]
    blockers = [str(item) for item in confirmation.get("packet_blockers") or []]
    if confirmation.get("ok") is not True:
        blockers.append("confirmation_packet_not_ok")
    if len(candidates) != 1:
        blockers.append("expected_exactly_one_repair_candidate")

    route_projections: list[dict[str, Any]] = []
    with _connect_ro(db) as conn:
        for candidate in candidates:
            route = _as_mapping(candidate.get("generic_posterior_repair_route")) or _as_mapping(
                candidate.get("final_bucket_lock_route")
            )
            if route.get("available") is not True:
                blockers.append("posterior_repair_route_not_available")
            route_projection = _route_projection_for_candidate(
                conn,
                candidate,
                db_path=db,
                generated_at=generated,
            )
            route_projections.append(route_projection)
            blockers.extend(str(item) for item in route_projection.get("blockers") or [])

    action_counts: Counter[str] = Counter()
    write_tables: set[str] = set()
    for route_projection in route_projections:
        for action in route_projection.get("expected_actions") or []:
            action_map = _as_mapping(action)
            action_counts[str(action_map.get("action") or "unknown")] += 1
            if action_map.get("would_write"):
                write_tables.add(str(action_map.get("target_table") or ""))

    blockers = sorted(set(blockers))
    risk_level = _risk_level(blockers, len(candidates), sorted(write_tables))
    backup_cmd = f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup)}\""
    maintenance_cmd = (
        "python3 scripts/controlled_apply_maintenance_window_packet.py --summary "
        f"--db {_q(db)} --max-items-output {int(max_items_output)}"
    )
    apply_cmd = ""
    if candidates and not blockers:
        candidate = candidates[0]
        route = _as_mapping(candidate.get("generic_posterior_repair_route")) or _as_mapping(
            candidate.get("final_bucket_lock_route")
        )
        apply_cmd = str(route.get("apply_command") or "")
    release_gate_cmd = (
        "python3 scripts/hierarchical_probability_release_gate.py --summary "
        f"--limit {int(limit)} --max-items-output 3 --skip-production-uniqueness"
    )
    coverage_cmd = (
        "python3 scripts/posterior_ssot_fact_coverage_audit.py --latest-only --project-forward "
        f"--limit {int(limit)} --max-items-output {int(max_items_output)}"
    )
    review_args = ["--db", _q(db), "--limit", str(int(limit)), "--max-items-output", str(int(max_items_output))]
    if explicit_fact_ids:
        review_args.extend(["--fact-ids", _q(",".join(str(value) for value in explicit_fact_ids))])
    elif fact_id:
        review_args.extend(["--fact-id", _q(fact_id)])
    if slug:
        review_args.extend(["--slug", _q(slug)])
    if event_date:
        review_args.extend(["--event-date", _q(event_date)])
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "generated_at": generated,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "posterior_ssot_apply_preflight_risk_no_apply",
        "db": str(db),
        "scope": {
            "fact_id": str(fact_id or ""),
            "fact_ids": explicit_fact_ids,
            "slug": str(slug or ""),
            "event_date": str(event_date or ""),
            "since_hours": float(since_hours),
            "limit": int(limit),
            "latest_only": bool(latest_only),
        },
        "guards": _guards(),
        "risk_level": risk_level,
        "blockers": blockers,
        "candidate_count": len(candidates),
        "confirmation_packet": {
            "schema": confirmation.get("schema"),
            "ok": bool(confirmation.get("ok")),
            "candidate_count": confirmation.get("candidate_count"),
            "backup_path": confirmation.get("backup_path"),
            "confirmation_phrase": confirmation.get("confirmation_phrase"),
        },
        "route_projections": route_projections,
        "expected_write_summary": {
            "append_event_fact_history_count": int(action_counts.get("append_event_fact_history") or 0),
            "repoint_event_fact_latest_count": int(action_counts.get("repoint_event_fact_latest") or 0),
            "upsert_event_fact_summary_cache_count": int(action_counts.get("upsert_event_fact_summary_cache") or 0),
            "tables": sorted(write_tables),
            "does_not_overwrite_event_fact_history": True,
            "does_not_touch_orders_or_decisions": True,
        },
        "operator_preflight": [
            {
                "step": "review_confirmation_packet",
                "command": " ".join(["python3", "scripts/posterior_ssot_apply_confirmation_packet.py", *review_args]),
                "writes": False,
                "required": True,
            },
            {
                "step": "create_fresh_archive_backup",
                "command": backup_cmd,
                "writes": True,
                "writes_database": False,
                "required": True,
            },
            {
                "step": "assert_writer_quiescence",
                "command": maintenance_cmd,
                "writes": False,
                "required": True,
                "pass_condition": "writer_quiescent == true",
            },
            {
                "step": "apply_posterior_repair_route_after_confirmation",
                "command": apply_cmd,
                "writes": True,
                "required": bool(apply_cmd and not blockers),
                "confirmation_phrase": confirmation_packet.CONFIRMATION_PHRASE,
                "writer_quiescent_flag_required": True,
                "maintenance_window_hard_check": True,
                "maintenance_window_block_reason": "maintenance_window_not_quiescent",
            },
            {
                "step": "post_apply_forward_coverage",
                "command": coverage_cmd,
                "writes": False,
                "required": True,
            },
            {
                "step": "post_apply_release_gate",
                "command": release_gate_cmd,
                "writes": False,
                "required": True,
            },
        ],
        "rollback": {
            "backup_path": str(backup),
            "restore_command": f"cp {_q(backup)} {_q(db)}",
            "requires_operator_shutdown_of_writers_first": True,
        },
        "next_boundary": (
            "operator_can_review_and_confirm_posterior_repair_apply_route"
            if not blockers
            else "fix_preflight_blockers_before_any_apply"
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--fact-id", default="")
    parser.add_argument("--fact-ids", default="", help="Comma-separated event_fact_history ids; preflight still requires exactly one candidate.")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--since-hours", type=float, default=168.0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--all-history", action="store_true")
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    args = parser.parse_args(argv)
    packet = build_preflight_risk_packet(
        db_path=args.db,
        fact_id=str(args.fact_id or ""),
        fact_ids=_parse_fact_ids(str(args.fact_ids or "")),
        slug=str(args.slug or ""),
        event_date=str(args.event_date or ""),
        since_hours=float(args.since_hours),
        limit=int(args.limit),
        latest_only=not bool(args.all_history),
        max_items_output=int(args.max_items_output),
        backup_dir=args.backup_dir,
    )
    print(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if packet.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
