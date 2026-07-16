#!/usr/bin/env python3
"""Controlled lineage repair apply entrypoint for daily review legacy facts.

Defaults to no-apply. The only supported write is targeted merge-fill of
`event_fact_history.fact_json` lineage fields for rows listed by a
`confirm_repair` decision manifest and its derived apply contract.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daily_review_lineage_repair_writer import (  # noqa: E402
    CONFIRMATION_PHRASE,
    apply_lineage_repair_contract,
)
from scripts import daily_review_persistence_batch_plan as batch_plan  # noqa: E402


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
    parser.add_argument("--lineage-decision-manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="", help=f"Required exact phrase for --apply: {CONFIRMATION_PHRASE}")
    parser.add_argument("--backup", type=Path, default=None)
    parser.add_argument("--writer-quiescent", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    manifest = batch_plan.load_lineage_decision_manifest_file(args.lineage_decision_manifest)
    plan = batch_plan.build_batch_plan(
        args.db,
        since_days=int(args.since_days),
        since_date=args.since_date,
        slug=args.slug,
        event_date=args.event_date,
        market_scope=args.market_scope,
        limit=int(args.limit),
        max_items_output=max(int(args.max_items_output), int(args.limit)),
        include_ui_snapshot_audit=False,
        include_replay_audit=False,
        lineage_decision_manifest=manifest,
    )
    contract = batch_plan.build_lineage_repair_apply_contract(
        plan,
        lineage_decision_manifest=manifest,
        backup_path=args.backup,
        allow_apply=bool(args.apply),
        confirmation_phrase=str(args.confirm or ""),
        writer_quiescent=bool(args.writer_quiescent),
    )
    if not args.apply:
        result = {
            "schema": "daily_review_lineage_repair_apply_cli_result_v1",
            "ok": True,
            "applied": False,
            "read_only": True,
            "dry_run": True,
            "execution_mode": "lineage_repair_apply_cli_no_apply",
            "contract": contract,
            "guards": {
                "executes_sql": False,
                "writes_archive_db": False,
                "writes_event_fact_history": False,
                "historical_backfill": False,
                "fetches_external_weather_sources": False,
                "places_orders": False,
                "restarts_launchd": False,
                "changes_parameters": False,
            },
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0

    with sqlite3.connect(Path(args.db).expanduser().resolve(), timeout=30) as conn:
        apply_result = apply_lineage_repair_contract(
            conn,
            contract,
            allow_apply=True,
            confirmation_phrase=str(args.confirm or ""),
            backup_path=args.backup,
            writer_quiescent=bool(args.writer_quiescent),
        )
    result = {
        "schema": "daily_review_lineage_repair_apply_cli_result_v1",
        "ok": bool(apply_result.get("ok")),
        "applied": bool(apply_result.get("applied")),
        "read_only": False,
        "dry_run": False,
        "execution_mode": "lineage_repair_apply_cli_apply",
        "contract": contract,
        "apply_result": apply_result,
        "guards": dict(apply_result.get("guards") or {}),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if apply_result.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
