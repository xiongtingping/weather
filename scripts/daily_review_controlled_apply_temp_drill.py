#!/usr/bin/env python3
"""Run the daily review controlled apply chain against a temporary DB copy.

This drill copies archive.db with SQLite's backup API, then runs the production
controlled apply functions on the temporary copy:

1. Phase0 forward schema apply.
2. Daily review lineage repair.
3. Daily review persistence apply.
4. Post-apply acceptance audit.

It never writes the production database, never creates a production backup,
never changes parameters, never restarts launchd, and never places orders.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import daily_review_lineage_repair_writer  # noqa: E402
from scripts import daily_review_controlled_apply_confirmation_packet as master_packet  # noqa: E402
from scripts import daily_review_persistence_apply  # noqa: E402
from scripts import daily_review_persistence_batch_plan as batch_plan  # noqa: E402
from scripts import daily_review_post_apply_acceptance_audit  # noqa: E402
from scripts import phase0_schema_apply  # noqa: E402


SCHEMA_VERSION = "daily_review_controlled_apply_temp_drill_v1"
DEFAULT_DB = ROOT / "data" / "archive.db"


def _copy_sqlite_db_to_temp(db_path: Path) -> tuple[Path, Path]:
    source = Path(db_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(str(source))
    tmp_dir = Path(tempfile.mkdtemp(prefix="weather_daily_review_apply_drill_"))
    tmp_db = tmp_dir / source.name
    src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(tmp_db)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return tmp_dir, tmp_db


def _copy_sqlite_backup(source: Path, backup: Path) -> str:
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        backup.unlink()
    try:
        subprocess.run(
            ["cp", "-c", str(source), str(backup)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if backup.exists() and backup.stat().st_size > 0:
            return "apfs_clonefile"
        backup.unlink(missing_ok=True)
    except Exception:
        backup.unlink(missing_ok=True)
    src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(backup)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return "sqlite_backup"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _table_count(db: Path, table: str) -> int | None:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
        if row is None:
            return None
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return int(count or 0)


def _production_counts(db: Path) -> dict[str, int | None]:
    return {
        "daily_weather_reviews": _table_count(db, "daily_weather_reviews"),
        "daily_calibration_records": _table_count(db, "daily_calibration_records"),
        "bias_correction_candidates": _table_count(db, "bias_correction_candidates"),
        "parameter_change_events": _table_count(db, "parameter_change_events"),
    }


def _timer() -> float:
    return time.perf_counter()


def _elapsed_ms(start: float) -> int:
    return int(round((time.perf_counter() - start) * 1000))


def _operation_max_items_output(*, limit: int, max_items_output: int) -> int:
    """Keep display truncation from narrowing the controlled apply scope."""
    return max(1, int(limit), int(max_items_output))


def _compact_phase0(result: Mapping[str, Any]) -> dict[str, Any]:
    apply_result = _as_mapping(result.get("apply_result"))
    return {
        "schema": result.get("schema"),
        "ok": bool(apply_result.get("ok")),
        "applied": bool(result.get("applied")),
        "execution_mode": result.get("execution_mode"),
        "ddl_summary": dict(_as_mapping(result.get("ddl_summary"))),
        "applied_action_count": len(_as_sequence(apply_result.get("applied_actions"))),
        "schema_surface_ready": bool(apply_result.get("schema_surface_ready")),
        "phase0_ready": bool(apply_result.get("phase0_ready")),
        "remaining_next_actions": list(apply_result.get("remaining_next_actions") or [])[:20],
    }


def _compact_lineage(result: Mapping[str, Any], risk: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": result.get("schema"),
        "ok": bool(result.get("ok")),
        "applied": bool(result.get("applied")),
        "execution_mode": result.get("execution_mode"),
        "repair_item_count": result.get("repair_item_count"),
        "applied_item_count": len(_as_sequence(result.get("applied_items"))),
        "skipped_item_count": len(_as_sequence(result.get("skipped_items"))),
        "risk_level": risk.get("risk_level"),
        "risk_blockers": list(risk.get("blockers") or []),
        "lineage_blocker_expected_removed_count": risk.get("lineage_blocker_expected_removed_count"),
        "post_repair_lineage_apply_blocker_counts": dict(plan.get("lineage_apply_blocker_counts") or {}),
        "post_repair_can_apply_after_schema_and_confirmation": bool(
            plan.get("can_apply_after_schema_and_confirmation")
        ),
        "post_repair_apply_blockers": list(plan.get("apply_blockers") or []),
    }


def _compact_persistence(result: Mapping[str, Any]) -> dict[str, Any]:
    contract = _as_mapping(result.get("contract"))
    return {
        "schema": result.get("schema"),
        "ok": bool(result.get("ok")),
        "applied": bool(result.get("applied")),
        "execution_mode": result.get("execution_mode"),
        "aggregate_row_counts": dict(_as_mapping(result.get("aggregate_row_counts"))),
        "applied_result_count": len(_as_sequence(result.get("applied_results"))),
        "contract_apply_blockers": list(contract.get("apply_blockers") or []),
        "contract_batch_apply_blockers": list(contract.get("batch_apply_blockers") or []),
        "parameter_events_apply_enabled": bool(result.get("parameter_events_apply_enabled")),
    }


def _compact_acceptance(report: Mapping[str, Any]) -> dict[str, Any]:
    coverage = _as_mapping(report.get("coverage"))
    blocker_counts: dict[str, int] = {
        str(key): int(value or 0)
        for key, value in _as_mapping(coverage.get("item_blocker_counts")).items()
        if str(key or "").strip()
    }
    blocker_samples: list[dict[str, Any]] = []
    for raw in _as_sequence(report.get("item_blocker_samples")):
        sample = _as_mapping(raw)
        blockers = [str(value) for value in _as_sequence(sample.get("blockers")) if str(value or "").strip()]
        if blockers and len(blocker_samples) < 20:
            blocker_samples.append(
                {
                    "slug": sample.get("slug"),
                    "event_date": sample.get("event_date"),
                    "review_id": sample.get("review_id"),
                    "first_bad_link": sample.get("first_bad_link"),
                    "blockers": blockers,
                    "review_fact_snapshot_id": sample.get("review_fact_snapshot_id"),
                    "review_posterior_run_id": sample.get("review_posterior_run_id"),
                    "latest_fact": dict(_as_mapping(sample.get("latest_fact"))),
                    "posterior_lineage": dict(_as_mapping(sample.get("posterior_lineage"))),
                    "review_replay": dict(_as_mapping(sample.get("review_replay"))),
                }
            )
    for raw in _as_sequence(report.get("items")):
        item = _as_mapping(raw)
        item_blockers = [str(value) for value in _as_sequence(item.get("blockers")) if str(value or "").strip()]
        for blocker in item_blockers:
            blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
        if item_blockers and len(blocker_samples) < 20:
            latest_fact = _as_mapping(item.get("latest_fact"))
            blocker_samples.append(
                {
                    "slug": item.get("slug"),
                    "event_date": item.get("event_date"),
                    "review_id": item.get("review_id"),
                    "first_bad_link": item.get("first_bad_link"),
                    "blockers": item_blockers,
                    "review_fact_snapshot_id": item.get("review_fact_snapshot_id"),
                    "latest_fact_id": latest_fact.get("fact_id"),
                    "latest_fact_snapshot_id": latest_fact.get("fact_snapshot_id"),
                    "latest_fact_posterior_ssot_fact_present": bool(
                        latest_fact.get("posterior_ssot_fact_present")
                    ),
                }
            )
    return {
        "schema": report.get("schema"),
        "ok": bool(report.get("ok")),
        "read_only": bool(report.get("read_only")),
        "daily_weather_reviews_row_count": report.get("daily_weather_reviews_row_count"),
        "coverage": {
            "raw_settled_event_total_count": coverage.get("raw_settled_event_total_count"),
            "settled_event_count": coverage.get("settled_event_count"),
            "excluded_settled_event_count": coverage.get("excluded_settled_event_count"),
            "reviewed_event_count": coverage.get("reviewed_event_count"),
            "missing_review_count": coverage.get("missing_review_count"),
            "coverage_ratio": coverage.get("coverage_ratio"),
        },
        "blockers": list(report.get("blockers") or []),
        "item_blocker_counts": dict(sorted(blocker_counts.items())),
        "blocker_samples": blocker_samples,
        "warnings": list(report.get("warnings") or [])[:20],
    }


def run_drill(
    *,
    db_path: Path = DEFAULT_DB,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 100,
    missing_fact_decision: str = master_packet.DEFAULT_MISSING_FACT_DECISION,
    keep_temp: bool = False,
) -> dict[str, Any]:
    source_db = Path(db_path).expanduser().resolve()
    operation_max_items = _operation_max_items_output(limit=limit, max_items_output=max_items_output)
    production_counts_before = _production_counts(source_db)
    stage_durations_ms: dict[str, int] = {}
    stage_start = _timer()
    tmp_dir, tmp_db = _copy_sqlite_db_to_temp(source_db)
    stage_durations_ms["copy_source_to_temp"] = _elapsed_ms(stage_start)
    retained = bool(keep_temp)
    backup_path = tmp_dir / f"{tmp_db.name}.controlled-apply-drill.bak"
    try:
        stage_start = _timer()
        master = master_packet.build_confirmation_packet(
            tmp_db,
            since_days=since_days,
            since_date=since_date,
            slug=slug,
            event_date=event_date,
            market_scope=market_scope,
            limit=limit,
            max_items_output=operation_max_items,
            backup_dir=tmp_dir,
            missing_fact_decision=missing_fact_decision,
        )
        stage_durations_ms["build_master_packet"] = _elapsed_ms(stage_start)
        manifest = dict(_as_mapping(master.get("proposed_manifest_json")))
        manifest_decisions = dict(_as_mapping(master.get("proposed_manifest_decision_counts")))
        stage_start = _timer()
        temp_backup_mode = _copy_sqlite_backup(tmp_db, backup_path)
        stage_durations_ms["create_temp_backup"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        phase0_result = phase0_schema_apply.apply_schema_contract(
            tmp_db,
            allow_apply=True,
            confirmation_phrase=phase0_schema_apply.CONFIRMATION_PHRASE,
            backup_path=backup_path,
            writer_quiescent=True,
        )
        stage_durations_ms["apply_phase0_schema"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        lineage_plan_before = batch_plan.build_batch_plan(
            tmp_db,
            since_days=since_days,
            since_date=since_date,
            slug=slug,
            event_date=event_date,
            market_scope=market_scope,
            limit=limit,
            max_items_output=operation_max_items,
            include_ui_snapshot_audit=False,
            include_replay_audit=False,
            lineage_decision_manifest=manifest,
        )
        stage_durations_ms["build_lineage_plan_before_repair"] = _elapsed_ms(stage_start)
        stage_start = _timer()
        lineage_contract = batch_plan.build_lineage_repair_apply_contract(
            lineage_plan_before,
            lineage_decision_manifest=manifest,
            backup_path=backup_path,
            allow_apply=True,
            confirmation_phrase=batch_plan.LINEAGE_REPAIR_CONFIRMATION_PHRASE,
            writer_quiescent=True,
        )
        stage_durations_ms["build_lineage_repair_contract"] = _elapsed_ms(stage_start)
        stage_start = _timer()
        with sqlite3.connect(tmp_db, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            lineage_risk = daily_review_lineage_repair_writer.preview_lineage_repair_contract(
                conn,
                lineage_contract,
            )
            lineage_result = daily_review_lineage_repair_writer.apply_lineage_repair_contract(
                conn,
                lineage_contract,
                allow_apply=True,
                confirmation_phrase=daily_review_lineage_repair_writer.CONFIRMATION_PHRASE,
                backup_path=backup_path,
                writer_quiescent=True,
            )
            conn.commit()
        stage_durations_ms["apply_lineage_repair"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        lineage_plan_after = batch_plan.build_batch_plan(
            tmp_db,
            since_days=since_days,
            since_date=since_date,
            slug=slug,
            event_date=event_date,
            market_scope=market_scope,
            limit=limit,
            max_items_output=operation_max_items,
            include_ui_snapshot_audit=False,
            include_replay_audit=False,
            lineage_decision_manifest=manifest,
        )
        stage_durations_ms["build_lineage_plan_after_repair"] = _elapsed_ms(stage_start)
        stage_start = _timer()
        persistence_result = daily_review_persistence_apply.apply_daily_review_persistence_batch(
            tmp_db,
            since_days=since_days,
            since_date=since_date,
            slug=slug,
            event_date=event_date,
            market_scope=market_scope,
            limit=limit,
            max_items_output=operation_max_items,
            lineage_decision_manifest=manifest,
            backup_path=backup_path,
            allow_apply=True,
            confirmation_phrase=daily_review_persistence_apply.CONFIRMATION_PHRASE,
            writer_quiescent=True,
        )
        stage_durations_ms["apply_daily_review_persistence"] = _elapsed_ms(stage_start)
        stage_start = _timer()
        acceptance = daily_review_post_apply_acceptance_audit.build_report(
            tmp_db,
            since_days=since_days,
            since_date=since_date,
            slug=slug,
            event_date=event_date,
            market_scope=market_scope,
            limit=limit,
            max_items_output=operation_max_items,
            include_ui_snapshot_audit=False,
            lineage_decision_manifest=manifest,
        )
        stage_durations_ms["post_apply_acceptance_audit"] = _elapsed_ms(stage_start)
        production_counts_after = _production_counts(source_db)
        production_unchanged = production_counts_before == production_counts_after
        phase0_ok = bool(_as_mapping(phase0_result.get("apply_result")).get("ok"))
        lineage_ok = bool(lineage_result.get("ok"))
        persistence_ok = bool(persistence_result.get("ok"))
        acceptance_ok = bool(acceptance.get("ok"))
        apply_chain_ok = bool(phase0_ok and lineage_ok and persistence_ok and production_unchanged)
        all_ok = bool(apply_chain_ok and acceptance_ok)
        acceptance_blockers = list(acceptance.get("blockers") or [])
        acceptance_coverage = _as_mapping(acceptance.get("coverage"))
        blocking_summary = {
            "phase0_apply_ok": phase0_ok,
            "lineage_repair_ok": lineage_ok,
            "daily_review_persistence_ok": persistence_ok,
            "apply_chain_ok": apply_chain_ok,
            "post_apply_acceptance_ok": acceptance_ok,
            "production_counts_unchanged": production_unchanged,
            "post_apply_acceptance_blockers": acceptance_blockers,
            "acceptance_missing_review_count": acceptance_coverage.get("missing_review_count"),
            "acceptance_reviewed_event_count": acceptance_coverage.get("reviewed_event_count"),
            "acceptance_settled_event_count": acceptance_coverage.get("settled_event_count"),
            "requires_missing_fact_snapshot_resolution": "daily_review_missing" in acceptance_blockers,
            "requires_posterior_ssot_legacy_repair": any(
                blocker
                in {
                    "posterior_lineage_failed",
                    "review_posterior_lineage_not_from_posterior_ssot_fact",
                    "latest_fact_posterior_ssot_fact_missing",
                }
                for blocker in acceptance_blockers
            ),
            "requires_replay_anchor_repair": any(
                blocker in {"review_replay_failed", "review_replay_fact_missing"}
                for blocker in acceptance_blockers
            ),
        }
        return {
            "ok": all_ok,
            "schema": SCHEMA_VERSION,
            "read_only_production": True,
            "dry_run_production": True,
            "execution_mode": "daily_review_controlled_apply_temp_drill",
            "db": str(source_db),
            "temp_db": str(tmp_db),
            "temp_db_retained": retained,
            "backup_path": str(backup_path),
            "backup_exists": backup_path.exists(),
            "temp_backup_mode": temp_backup_mode,
            "scope": {
                "since_days": int(since_days),
                "since_date": since_date,
                "slug": slug,
                "event_date": event_date,
                "market_scope": market_scope,
                "limit": int(limit),
                "max_items_output": int(max_items_output),
                "operation_max_items_output": int(operation_max_items),
                "missing_fact_decision": missing_fact_decision,
            },
            "production_counts_before": production_counts_before,
            "production_counts_after": production_counts_after,
            "production_counts_unchanged": production_unchanged,
            "apply_chain_ok": apply_chain_ok,
            "post_apply_acceptance_ok": acceptance_ok,
            "blocking_summary": blocking_summary,
            "stage_durations_ms": stage_durations_ms,
            "manifest_decision_counts": manifest_decisions,
            "phase0": _compact_phase0(phase0_result),
            "lineage_repair": _compact_lineage(lineage_result, lineage_risk, lineage_plan_after),
            "daily_review_persistence": _compact_persistence(persistence_result),
            "post_apply_acceptance": _compact_acceptance(acceptance),
            "guards": {
                "writes_production_archive_db": False,
                "writes_temp_archive_db": True,
                "creates_production_backup": False,
                "creates_temp_backup": True,
                "historical_backfill_production": False,
                "fetches_external_weather_sources": False,
                "places_orders": False,
                "restarts_launchd": False,
                "changes_parameters": False,
            },
            "next_boundary": (
                "review_temp_drill_then_operator_confirm_production_controlled_apply"
                if all_ok
                else "fix_temp_drill_blockers_before_production_controlled_apply"
            ),
        }
    finally:
        if not retained:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since-days", type=int, default=45)
    parser.add_argument("--since-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=100)
    parser.add_argument(
        "--missing-fact-decision",
        choices=master_packet.lineage_runbook.confirmation_packet.MISSING_FACT_DECISIONS,
        default=master_packet.DEFAULT_MISSING_FACT_DECISION,
    )
    parser.add_argument("--keep-temp", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = run_drill(
            db_path=args.db,
            since_days=int(args.since_days),
            since_date=args.since_date,
            slug=args.slug,
            event_date=args.event_date,
            market_scope=args.market_scope,
            limit=int(args.limit),
            max_items_output=int(args.max_items_output),
            missing_fact_decision=args.missing_fact_decision,
            keep_temp=bool(args.keep_temp),
        )
    except Exception as exc:
        result = {
            "ok": False,
            "schema": SCHEMA_VERSION,
            "read_only_production": True,
            "dry_run_production": True,
            "error": f"{exc.__class__.__name__}: {exc}",
            "guards": {
                "writes_production_archive_db": False,
                "places_orders": False,
                "restarts_launchd": False,
                "changes_parameters": False,
            },
            "next_boundary": "fix_temp_drill_exception_before_production_controlled_apply",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
