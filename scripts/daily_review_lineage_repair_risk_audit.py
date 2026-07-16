#!/usr/bin/env python3
"""Read-only risk audit for daily review lineage repair contracts.

The default mode proposes `confirm_repair` for the current lineage repair
candidates only to preview the exact merge-fill diff. Passing
`--lineage-decision-manifest` audits the operator-reviewed manifest instead.
This script never writes archive.db, never creates a manifest or backup, and
never applies the repair.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daily_review_lineage_repair_writer import preview_lineage_repair_contract  # noqa: E402
from scripts import daily_review_persistence_batch_plan as batch_plan  # noqa: E402


SCHEMA_VERSION = "daily_review_lineage_repair_risk_audit_report_v1"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _proposed_confirm_decisions(candidates: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        repair_id = str(candidate.get("repair_correlation_id") or "").strip()
        if not repair_id:
            continue
        out[repair_id] = {
            "decision": "confirm_repair",
            "reason": "proposed_for_read_only_risk_audit_confirm_legacy_fact_snapshot_id_merge_fill",
        }
    return out


def _guard() -> dict[str, bool]:
    return {
        "executes_sql": False,
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_daily_weather_reviews": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def build_lineage_repair_risk_audit_report(
    db_path: Path | str = batch_plan.DEFAULT_DB,
    *,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 100,
    lineage_decision_manifest: Mapping[str, Any] | None = None,
    propose_confirm_repair: bool = True,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated = generated_at or _now_utc()
    db = Path(db_path).expanduser().resolve()
    manifest: Mapping[str, Any] | None = lineage_decision_manifest if lineage_decision_manifest else None
    manifest_source = "operator_lineage_decision_manifest" if manifest else "none"

    if manifest is None and propose_confirm_repair:
        initial_plan = batch_plan.build_batch_plan(
            db,
            since_days=int(since_days),
            since_date=since_date,
            slug=slug,
            event_date=event_date,
            market_scope=market_scope,
            limit=int(limit),
            max_items_output=max(int(max_items_output), int(limit)),
            include_ui_snapshot_audit=False,
            include_replay_audit=False,
        )
        candidates = list(initial_plan.get("lineage_repair_candidates") or [])
        manifest = batch_plan.build_lineage_repair_decision_manifest(
            candidates,
            decisions=_proposed_confirm_decisions(candidates),
            decided_by="risk_audit_proposed_only",
            decided_at=generated,
        )
        manifest_source = "proposed_confirm_repair_for_current_candidates"

    plan = batch_plan.build_batch_plan(
        db,
        since_days=int(since_days),
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=int(limit),
        max_items_output=max(int(max_items_output), int(limit)),
        include_ui_snapshot_audit=False,
        include_replay_audit=False,
        lineage_decision_manifest=manifest,
    )
    contract = batch_plan.build_lineage_repair_apply_contract(
        plan,
        lineage_decision_manifest=manifest,
        allow_apply=False,
        confirmation_phrase="",
        writer_quiescent=False,
    )
    with _connect_readonly(db) as conn:
        risk_audit = preview_lineage_repair_contract(conn, contract)
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(risk_audit.get("ok")),
        "generated_at": generated,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "lineage_repair_risk_audit_report_no_apply",
        "db": str(db),
        "manifest_source": manifest_source,
        "candidate_count": plan.get("lineage_repair_candidate_count"),
        "effective_lineage_repair_candidate_count": plan.get("effective_lineage_repair_candidate_count"),
        "lineage_decision_manifest_audit": dict(plan.get("lineage_decision_manifest_audit") or {}),
        "lineage_repair_apply_contract": contract,
        "risk_audit": risk_audit,
        "guards": _guard(),
        "blocked_operations": [
            "automatic_manifest_file_write",
            "automatic_backup_creation",
            "automatic_lineage_repair_apply",
            "daily_review_persistence",
            "historical_backfill",
            "launchd_restart",
            "order_or_trade_decision_write",
            "parameter_change",
        ],
        "next_boundary": risk_audit.get("next_boundary"),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=batch_plan.DEFAULT_DB)
    parser.add_argument("--since-days", type=int, default=45)
    parser.add_argument("--since-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=100)
    parser.add_argument("--lineage-decision-manifest", type=Path, default=None)
    parser.add_argument(
        "--no-propose-confirm-repair",
        action="store_true",
        help="Without a manifest, audit an empty apply contract instead of proposing confirm_repair for candidates.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    manifest = batch_plan.load_lineage_decision_manifest_file(args.lineage_decision_manifest)
    report = build_lineage_repair_risk_audit_report(
        args.db,
        since_days=int(args.since_days),
        since_date=args.since_date,
        slug=args.slug,
        event_date=args.event_date,
        market_scope=args.market_scope,
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        lineage_decision_manifest=manifest,
        propose_confirm_repair=not bool(args.no_propose_confirm_repair),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
