#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import apply_settlement_actual_repair_plan  # noqa: E402
from scripts import build_settlement_calibration_labels  # noqa: E402
from scripts import settlement_actual_repair_plan  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "settlement_validation_pipeline_latest.json"
DEFAULT_MARKET_SCOPE = "highest_temperature"
ACTUAL_APPLY_COMMAND = (
    ".venv/bin/python scripts/apply_settlement_actual_repair_plan.py "
    "--apply --confirm-apply APPLY_SETTLEMENT_ACTUALS"
)
LABEL_DRY_RUN_COMMAND = (
    ".venv/bin/python scripts/build_settlement_calibration_labels.py "
    "--since-days {since_days} --market-scope {market_scope}"
)
LABEL_APPLY_COMMAND = LABEL_DRY_RUN_COMMAND + " --apply --confirm-apply APPLY_SETTLEMENT_LABELS"
FLOW_DRY_RUN_COMMAND = (
    ".venv/bin/python scripts/apply_settlement_validation_flow.py "
    "--since-days {since_days} --market-scope {market_scope}"
)
FLOW_APPLY_ACTUALS_COMMAND = (
    FLOW_DRY_RUN_COMMAND + " --apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS"
)
FLOW_APPLY_LABELS_COMMAND = (
    FLOW_DRY_RUN_COMMAND + " --apply-labels --confirm-labels APPLY_SETTLEMENT_LABELS"
)
FLOW_APPLY_ACTUALS_AND_LABELS_COMMAND = (
    FLOW_DRY_RUN_COMMAND
    + " --apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS"
    + " --apply-labels --confirm-labels APPLY_SETTLEMENT_LABELS"
)
FLOW_APPLY_ACTUALS_LABELS_AND_VALIDATE_COMMAND = (
    FLOW_APPLY_ACTUALS_AND_LABELS_COMMAND + " --run-post-validation"
)
ROLLING_VALIDATION_COMMAND = (
    ".venv/bin/python scripts/paper_observation_rolling_validation.py "
    "--since-days {since_days} --market-scope {market_scope} --json"
)
OBJECTIVE_AUDIT_COMMAND = (
    ".venv/bin/python scripts/strategy_objective_completion_audit.py "
    "--since-days {since_days} --market-scope {market_scope} --json"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _backup_policy() -> dict[str, Any]:
    return {
        "pre_apply_backup_required": True,
        "backup_default": "enabled",
        "bypass_flag": "--no-backup-before-apply",
        "bypass_effect": "confirmed_apply_blocked",
        "flow_backup_dir_default": "data/backups",
        "direct_apply_backup_dir_default": "data/backups",
    }


def _top_results(rows: Any, limit: int = 5) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in list(rows or [])[: max(1, int(limit or 1))]:
        if not isinstance(row, Mapping):
            continue
        out.append(
            {
                "city": row.get("city"),
                "slug": row.get("slug"),
                "event_date": row.get("event_date"),
                "status": row.get("status") or row.get("current_status"),
                "actual": row.get("actual")
                if row.get("actual") is not None
                else (
                    (row.get("fetch_preview") or {}).get("candidate_settlement_actual")
                    if isinstance(row.get("fetch_preview"), Mapping)
                    else None
                ),
                "blockers": row.get("blockers") or row.get("apply_blockers") or [],
            }
        )
    return out


def _copy_sqlite_db(src: Path, dst: Path) -> None:
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5.0)
    try:
        dst_conn = sqlite3.connect(dst, timeout=5.0)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.Error:
        return []


def _create_table_like(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> bool:
    row = src.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None or not row[0]:
        return False
    dst.execute(str(row[0]))
    return True


def _insert_rows(dst: sqlite3.Connection, table: str, rows: list[sqlite3.Row]) -> int:
    if not rows:
        return 0
    cols = [str(key) for key in rows[0].keys()]
    col_sql = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    dst.executemany(
        f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
        [[row[col] for col in cols] for row in rows],
    )
    return len(rows)


def _copy_relevant_sqlite_subset(
    src_path: Path,
    dst_path: Path,
    *,
    relevant_rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    keys = [
        (
            str(row.get("slug") or "").strip(),
            str(row.get("event_date") or "").split("T", 1)[0],
        )
        for row in relevant_rows
        if str(row.get("slug") or "").strip()
    ]
    keys = [(slug, day) for slug, day in dict.fromkeys(keys) if slug]
    if not keys:
        return {"ok": False, "error": "no_relevant_slugs"}
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=5.0)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(dst_path, timeout=5.0)
    try:
        copied: dict[str, int] = {}
        for table in (
            "event_runs",
            "auto_trade_decisions",
            "calibration_samples",
            "calibration_labels",
        ):
            _create_table_like(src, dst, table)
        slugs = sorted({slug for slug, _day in keys})
        slug_placeholders = ", ".join("?" for _ in slugs)
        event_cols = _table_columns(src, "event_runs")
        if event_cols:
            event_rows = src.execute(
                f"""
                SELECT *
                FROM event_runs
                WHERE slug IN ({slug_placeholders})
                """,
                slugs,
            ).fetchall()
            copied["event_runs"] = _insert_rows(dst, "event_runs", list(event_rows))
        decision_cols = _table_columns(src, "auto_trade_decisions")
        decision_ids: list[int] = []
        if decision_cols:
            decision_rows = src.execute(
                f"""
                SELECT *
                FROM auto_trade_decisions
                WHERE slug IN ({slug_placeholders})
                """,
                slugs,
            ).fetchall()
            copied["auto_trade_decisions"] = _insert_rows(
                dst,
                "auto_trade_decisions",
                list(decision_rows),
            )
            if "id" in decision_cols:
                decision_ids = [
                    int(row["id"])
                    for row in decision_rows
                    if row["id"] is not None
                ]
        sample_cols = _table_columns(src, "calibration_samples")
        if sample_cols and "decision_id" in sample_cols and decision_ids:
            placeholders = ", ".join("?" for _ in decision_ids)
            sample_rows = src.execute(
                f"""
                SELECT *
                FROM calibration_samples
                WHERE decision_id IN ({placeholders})
                """,
                decision_ids,
            ).fetchall()
            copied["calibration_samples"] = _insert_rows(
                dst,
                "calibration_samples",
                list(sample_rows),
            )
        else:
            copied["calibration_samples"] = 0
        label_cols = _table_columns(src, "calibration_labels")
        if label_cols and "decision_id" in label_cols and decision_ids:
            placeholders = ", ".join("?" for _ in decision_ids)
            label_rows = src.execute(
                f"""
                SELECT *
                FROM calibration_labels
                WHERE decision_id IN ({placeholders})
                """,
                decision_ids,
            ).fetchall()
            copied["calibration_labels"] = _insert_rows(
                dst,
                "calibration_labels",
                list(label_rows),
            )
        else:
            copied["calibration_labels"] = 0
        dst.commit()
        return {"ok": True, "mode": "subset", "copied": copied, "slugs": slugs}
    finally:
        dst.close()
        src.close()


def _simulate_after_actual_apply(
    db_path: Path,
    *,
    since_days: int,
    market_scope: str,
    fetch_preview: bool,
    max_db_mb: float,
    relevant_rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "ok": False,
            "read_only": True,
            "simulated": False,
            "error": "db_missing",
        }
    db_size_mb = db_path.stat().st_size / (1024 * 1024)
    with tempfile.TemporaryDirectory(prefix="settlement-pipeline-") as tmpdir:
        tmp_db = Path(tmpdir) / "archive.sim.sqlite"
        subset_meta: dict[str, Any] | None = None
        if max_db_mb > 0 and db_size_mb > float(max_db_mb):
            subset_meta = _copy_relevant_sqlite_subset(
                db_path,
                tmp_db,
                relevant_rows=relevant_rows,
            )
            if not subset_meta.get("ok"):
                return {
                    "ok": False,
                    "read_only": True,
                    "simulated": False,
                    "error": subset_meta.get("error") or "subset_copy_failed",
                    "db_size_mb": round(db_size_mb, 2),
                    "max_db_mb": float(max_db_mb),
                }
        else:
            _copy_sqlite_db(db_path, tmp_db)
            subset_meta = {"ok": True, "mode": "full_copy", "copied": {}}
        apply_result = apply_settlement_actual_repair_plan.build_report(
            tmp_db,
            refresh_preview=bool(fetch_preview),
            apply=True,
            confirm_apply=apply_settlement_actual_repair_plan.APPLY_CONFIRM_PHRASE,
        )
        label_plan = build_settlement_calibration_labels.build_label_plan(
            tmp_db,
            since_days=max(1, int(since_days)),
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
            bayes_only=True,
            allow_open_events=False,
            strict_resolution_source=True,
        )
        label_apply = build_settlement_calibration_labels.apply_label_plan(
            tmp_db,
            label_plan.get("labels") or [],
        )
        label_plan_after_label_apply = build_settlement_calibration_labels.build_label_plan(
            tmp_db,
            since_days=max(1, int(since_days)),
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
            bayes_only=True,
            allow_open_events=False,
            strict_resolution_source=True,
        )
        return {
            "ok": True,
            "read_only": True,
            "simulated": True,
            "simulation_db_mode": subset_meta.get("mode") if subset_meta else "unknown",
            "source_db_size_mb": round(db_size_mb, 2),
            "subset_copy": subset_meta,
            "tmp_db_removed": True,
            "actual_inserted_count": int(apply_result.get("inserted_count") or 0),
            "actual_results": _top_results(apply_result.get("results")),
            "label_planned_after_actual_apply": int(label_plan.get("planned_labels") or 0),
            "label_decisions_scanned_after_actual_apply": int(
                label_plan.get("decisions_scanned") or 0
            ),
            "label_skipped_after_actual_apply": dict(label_plan.get("skipped") or {}),
            "label_apply_command_after_actual_apply": label_plan.get("apply_command")
            or LABEL_APPLY_COMMAND.format(
                since_days=max(1, int(since_days)),
                market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
            ),
            "label_apply_inserted_count": int(label_apply.get("inserted") or 0),
            "label_apply_skipped_existing_count": int(
                label_apply.get("skipped_existing") or 0
            ),
            "label_planned_after_label_apply": int(
                label_plan_after_label_apply.get("planned_labels") or 0
            ),
            "label_decisions_scanned_after_label_apply": int(
                label_plan_after_label_apply.get("decisions_scanned") or 0
            ),
            "label_apply_closed_loop_ok": int(label_apply.get("inserted") or 0) > 0
            and int(label_plan_after_label_apply.get("planned_labels") or 0) == 0,
        }


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    since_days: int = 42,
    market_scope: str = DEFAULT_MARKET_SCOPE,
    fetch_preview: bool = True,
    simulate_after_actual_apply: bool = False,
    simulate_max_db_mb: float = 512.0,
) -> dict[str, Any]:
    repair = settlement_actual_repair_plan.build_report(
        db_path,
        fetch_preview=bool(fetch_preview),
    )
    apply_dry_run = apply_settlement_actual_repair_plan.build_report(
        db_path,
        refresh_preview=bool(fetch_preview),
    )
    label_plan = build_settlement_calibration_labels.build_label_plan(
        db_path,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        bayes_only=True,
        allow_open_events=False,
        strict_resolution_source=True,
    )
    ready_actuals = int(apply_dry_run.get("ready_count") or 0)
    planned_labels = int(label_plan.get("planned_labels") or 0)
    missing_actuals = int(repair.get("missing_settlement_actual_count") or 0)
    label_dry_run_command = LABEL_DRY_RUN_COMMAND.format(
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    label_apply_command = LABEL_APPLY_COMMAND.format(
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    flow_dry_run_command = FLOW_DRY_RUN_COMMAND.format(
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    flow_apply_actuals_command = FLOW_APPLY_ACTUALS_COMMAND.format(
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    flow_apply_labels_command = FLOW_APPLY_LABELS_COMMAND.format(
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    flow_apply_actuals_and_labels_command = FLOW_APPLY_ACTUALS_AND_LABELS_COMMAND.format(
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    flow_apply_actuals_labels_and_validate_command = (
        FLOW_APPLY_ACTUALS_LABELS_AND_VALIDATE_COMMAND.format(
            since_days=max(1, int(since_days)),
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        )
    )
    rolling_validation_command = ROLLING_VALIDATION_COMMAND.format(
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    objective_audit_command = OBJECTIVE_AUDIT_COMMAND.format(
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    if ready_actuals:
        next_action = "confirm_and_apply_settlement_actuals_then_rebuild_labels"
    elif planned_labels:
        next_action = "review_and_apply_settlement_calibration_labels"
    elif missing_actuals:
        next_action = "repair_settlement_actual_metadata_or_fetch_preview"
    else:
        next_action = "continue_collecting_paper_observation_samples"
    report = {
        "ok": ready_actuals == 0 and planned_labels == 0 and missing_actuals == 0,
        "read_only": True,
        "generated_at": _utc_now(),
        "db_path": str(db_path),
        "since_days": max(1, int(since_days)),
        "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
        "fetch_preview_requested": bool(fetch_preview),
        "write_safety": _backup_policy(),
        "settlement_actual_repair": {
            "missing_count": missing_actuals,
            "next_action": repair.get("next_action"),
            "rows": _top_results(repair.get("rows")),
        },
        "settlement_actual_apply_dry_run": {
            "candidate_count": int(apply_dry_run.get("candidate_count") or 0),
            "ready_count": ready_actuals,
            "read_only": bool(apply_dry_run.get("read_only")),
            "next_action": apply_dry_run.get("next_action"),
            "results": _top_results(apply_dry_run.get("results")),
            "review_packet": dict(apply_dry_run.get("review_packet") or {}),
            "confirm_command": (
                ACTUAL_APPLY_COMMAND if ready_actuals else ""
            ),
            "flow_confirm_command": flow_apply_actuals_command if ready_actuals else "",
            "flow_confirm_and_label_apply_command": (
                flow_apply_actuals_and_labels_command if ready_actuals else ""
            ),
            "flow_confirm_label_apply_and_validate_command": (
                flow_apply_actuals_labels_and_validate_command if ready_actuals else ""
            ),
            "pre_apply_backup_required": True,
            "backup_policy": _backup_policy(),
            "after_apply_label_dry_run_command": (
                label_dry_run_command if ready_actuals else ""
            ),
            "after_apply_label_apply_command": label_apply_command if ready_actuals else "",
        },
        "settlement_label_dry_run": {
            "planned_labels": planned_labels,
            "decisions_scanned": int(label_plan.get("decisions_scanned") or 0),
            "skipped": dict(label_plan.get("skipped") or {}),
            "dry_run_command": label_dry_run_command,
            "apply_command": label_plan.get("apply_command") or (
                label_apply_command if planned_labels else ""
            ),
            "flow_apply_command": flow_apply_labels_command if planned_labels else "",
            "pre_apply_backup_required": True,
            "backup_policy": _backup_policy(),
            "after_apply_validation_commands": {
                "rolling_validation": rolling_validation_command if planned_labels else "",
                "objective_audit": objective_audit_command if planned_labels else "",
            },
        },
        "flow_commands": {
            "dry_run": flow_dry_run_command,
            "apply_actuals": flow_apply_actuals_command if ready_actuals else "",
            "apply_labels": flow_apply_labels_command if planned_labels else "",
            "apply_actuals_and_labels": (
                flow_apply_actuals_and_labels_command
                if ready_actuals or planned_labels
                else ""
            ),
            "apply_actuals_labels_and_validate": (
                flow_apply_actuals_labels_and_validate_command
                if ready_actuals or planned_labels
                else ""
            ),
            "pre_apply_backup_required": True,
            "backup_policy": _backup_policy(),
            "after_labels_applied": {
                "rolling_validation": rolling_validation_command,
                "objective_audit": objective_audit_command,
            },
        },
        "post_label_apply_validation": {
            "required": bool(planned_labels),
            "commands": {
                "rolling_validation": rolling_validation_command,
                "objective_audit": objective_audit_command,
            },
            "purpose": "prove_new_settlement_labels_feed_paper_validation_and_strategy_objective_audit",
        },
        "next_action": next_action,
    }
    if simulate_after_actual_apply:
        report["after_actual_apply_simulation"] = _simulate_after_actual_apply(
            db_path,
            since_days=max(1, int(since_days)),
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
            fetch_preview=bool(fetch_preview),
            max_db_mb=float(simulate_max_db_mb),
            relevant_rows=[
                row for row in apply_dry_run.get("results") or [] if isinstance(row, Mapping)
            ],
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only settlement validation pipeline summary."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since-days", type=int, default=42)
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE)
    parser.add_argument("--no-fetch-preview", action="store_true")
    parser.add_argument("--simulate-after-actual-apply", action="store_true")
    parser.add_argument(
        "--simulate-max-db-mb",
        type=float,
        default=512.0,
        help="Maximum DB size for temp-copy simulation. Use 0 to disable this guard.",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(
        Path(args.db),
        since_days=int(args.since_days),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        fetch_preview=not bool(args.no_fetch_preview),
        simulate_after_actual_apply=bool(args.simulate_after_actual_apply),
        simulate_max_db_mb=float(args.simulate_max_db_mb),
    )
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=not args.json))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
