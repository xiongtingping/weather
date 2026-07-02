#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import apply_settlement_actual_repair_plan  # noqa: E402
from scripts import build_settlement_calibration_labels  # noqa: E402
from scripts import settlement_validation_pipeline  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "settlement_validation_apply_flow_latest.json"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
DEFAULT_MARKET_SCOPE = "highest_temperature"
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


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _actual_apply_confirmation_ok(apply_actuals: bool, confirm_actuals: str) -> bool:
    return bool(apply_actuals) and (
        str(confirm_actuals or "").strip()
        == apply_settlement_actual_repair_plan.APPLY_CONFIRM_PHRASE
    )


def _label_apply_confirmation_ok(apply_labels: bool, confirm_labels: str) -> bool:
    return bool(apply_labels) and (
        str(confirm_labels or "").strip()
        == build_settlement_calibration_labels.APPLY_CONFIRM_PHRASE
    )


def _pre_apply_backup(
    db_path: Path,
    *,
    backup_before_apply: bool,
    backup_dir: Path,
    actual_apply_allowed_by_args: bool,
    label_apply_allowed_by_args: bool,
) -> dict[str, Any]:
    requested = bool(actual_apply_allowed_by_args or label_apply_allowed_by_args)
    if not requested:
        return {
            "requested": False,
            "created": False,
            "path": "",
            "reason": "no_confirmed_apply_requested",
        }
    if not backup_before_apply:
        return {
            "requested": True,
            "created": False,
            "path": "",
            "reason": "disabled_by_no_backup_before_apply",
        }
    if not db_path.exists():
        return {
            "requested": True,
            "created": False,
            "path": "",
            "reason": "source_db_missing",
        }

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"archive.before-settlement-flow.{stamp}.db"
    suffix = 1
    while backup_path.exists():
        backup_path = backup_dir / f"archive.before-settlement-flow.{stamp}.{suffix}.db"
        suffix += 1

    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(backup_path), timeout=30)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    return {
        "requested": True,
        "created": True,
        "path": str(backup_path),
        "reason": "created_before_confirmed_apply",
    }


def _label_apply_result(
    db_path: Path,
    label_plan: Mapping[str, Any],
    *,
    apply_labels: bool,
    confirm_labels: str,
) -> dict[str, Any]:
    confirmation_ok = (
        str(confirm_labels or "").strip()
        == build_settlement_calibration_labels.APPLY_CONFIRM_PHRASE
    )
    if not apply_labels:
        return {
            "apply_requested": False,
            "apply_allowed": False,
            "confirmation_ok": confirmation_ok,
            "inserted": 0,
            "skipped_existing": 0,
            "status": "dry_run_only",
            "required_confirm_apply": build_settlement_calibration_labels.APPLY_CONFIRM_PHRASE,
        }
    if not confirmation_ok:
        return {
            "apply_requested": True,
            "apply_allowed": False,
            "confirmation_ok": False,
            "inserted": 0,
            "skipped_existing": 0,
            "status": "missing_apply_confirmation",
            "required_confirm_apply": build_settlement_calibration_labels.APPLY_CONFIRM_PHRASE,
        }
    result = build_settlement_calibration_labels.apply_label_plan(
        db_path,
        label_plan.get("labels") or [],
    )
    return {
        "apply_requested": True,
        "apply_allowed": True,
        "confirmation_ok": True,
        "inserted": _as_int(result.get("inserted")),
        "skipped_existing": _as_int(result.get("skipped_existing")),
        "status": "labels_applied",
        "required_confirm_apply": build_settlement_calibration_labels.APPLY_CONFIRM_PHRASE,
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _post_validation_result(
    db_path: Path,
    *,
    since_days: int,
    market_scope: str,
    run_post_validation: bool,
    labels_applied: bool,
) -> dict[str, Any]:
    if not run_post_validation:
        return {
            "run_requested": False,
            "run_allowed": False,
            "status": "not_requested",
            "reports_written": [],
        }
    if not labels_applied:
        return {
            "run_requested": True,
            "run_allowed": False,
            "status": "not_run_until_labels_applied",
            "reports_written": [],
        }
    paper_observation_rolling_validation = importlib.import_module(
        "scripts.paper_observation_rolling_validation"
    )
    strategy_objective_completion_audit = importlib.import_module(
        "scripts.strategy_objective_completion_audit"
    )

    rolling = paper_observation_rolling_validation.build_report(
        db_path,
        pilot_cities=[],
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    _write_json(paper_observation_rolling_validation.DEFAULT_OUT, rolling)
    objective = strategy_objective_completion_audit.build_report(
        db_path,
        event_dates=[],
        pilot_cities=[],
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        since_days=max(1, int(since_days)),
    )
    _write_json(strategy_objective_completion_audit.DEFAULT_OUT, objective)
    return {
        "run_requested": True,
        "run_allowed": True,
        "status": "post_validation_refreshed",
        "reports_written": [
            str(paper_observation_rolling_validation.DEFAULT_OUT),
            str(strategy_objective_completion_audit.DEFAULT_OUT),
        ],
        "rolling_ok": bool(rolling.get("ok")),
        "rolling_blockers": list(rolling.get("blockers") or [])[:8],
        "objective_ok": bool(objective.get("ok")),
        "objective_blockers": list(objective.get("blockers") or [])[:8],
    }


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    since_days: int = 42,
    market_scope: str = DEFAULT_MARKET_SCOPE,
    refresh_preview: bool = False,
    apply_actuals: bool = False,
    confirm_actuals: str = "",
    apply_labels: bool = False,
    confirm_labels: str = "",
    run_post_validation: bool = False,
    backup_before_apply: bool = True,
    backup_dir: Optional[Path] = None,
) -> dict[str, Any]:
    actual_apply_allowed_by_args = _actual_apply_confirmation_ok(apply_actuals, confirm_actuals)
    label_apply_allowed_by_args = _label_apply_confirmation_ok(apply_labels, confirm_labels)
    pre_apply_backup = _pre_apply_backup(
        db_path,
        backup_before_apply=bool(backup_before_apply),
        backup_dir=Path(backup_dir) if backup_dir is not None else DEFAULT_BACKUP_DIR,
        actual_apply_allowed_by_args=actual_apply_allowed_by_args,
        label_apply_allowed_by_args=label_apply_allowed_by_args,
    )
    confirmed_apply_requested = bool(
        actual_apply_allowed_by_args or label_apply_allowed_by_args
    )
    backup_gate_allows_apply = (
        not confirmed_apply_requested
        or bool(pre_apply_backup.get("created"))
    )
    effective_apply_actuals = bool(apply_actuals) and (
        not actual_apply_allowed_by_args or backup_gate_allows_apply
    )
    effective_apply_labels = bool(apply_labels) and (
        not label_apply_allowed_by_args or backup_gate_allows_apply
    )
    actual_report = apply_settlement_actual_repair_plan.build_report(
        db_path,
        refresh_preview=bool(refresh_preview),
        apply=bool(effective_apply_actuals),
        confirm_apply=str(confirm_actuals or ""),
        require_pre_apply_backup=False,
    )
    label_plan = build_settlement_calibration_labels.build_label_plan(
        db_path,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        bayes_only=True,
        allow_open_events=False,
        strict_resolution_source=True,
    )
    label_apply = _label_apply_result(
        db_path,
        label_plan,
        apply_labels=bool(effective_apply_labels),
        confirm_labels=str(confirm_labels or ""),
    )
    rolling_validation_command = ROLLING_VALIDATION_COMMAND.format(
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    objective_audit_command = OBJECTIVE_AUDIT_COMMAND.format(
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    post_label_validation = {
        "required": bool(label_apply.get("apply_allowed")),
        "status": (
            "run_after_labels_applied"
            if bool(label_apply.get("apply_allowed"))
            else "not_required_until_labels_apply"
        ),
        "commands": {
            "rolling_validation": rolling_validation_command,
            "objective_audit": objective_audit_command,
        },
        "purpose": "prove_new_settlement_labels_feed_paper_validation_and_strategy_objective_audit",
    }
    simulation = {}
    if (
        not bool(apply_actuals)
        and not bool(apply_labels)
        and _as_int(actual_report.get("ready_count")) > 0
    ):
        try:
            simulation_report = settlement_validation_pipeline.build_report(
                db_path,
                since_days=max(1, int(since_days)),
                market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
                fetch_preview=False,
                simulate_after_actual_apply=True,
            )
            simulation = dict(simulation_report.get("after_actual_apply_simulation") or {})
        except Exception as exc:
            simulation = {
                "ok": False,
                "simulated": False,
                "error": f"{type(exc).__name__}:{exc}",
            }
    actual_allowed = bool(actual_report.get("apply_allowed"))
    label_allowed = bool(label_apply.get("apply_allowed"))
    post_validation_run = _post_validation_result(
        db_path,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        run_post_validation=bool(run_post_validation),
        labels_applied=bool(label_allowed),
    )
    blocking_statuses: list[str] = []
    if confirmed_apply_requested and not backup_gate_allows_apply:
        blocking_statuses.append("pre_apply_backup_not_created")
    if bool(apply_actuals) and not actual_allowed:
        blocking_statuses.append("actual_apply_not_allowed")
    if bool(apply_labels) and not label_allowed:
        blocking_statuses.append("label_apply_not_allowed")
    if label_allowed:
        next_action = (
            "review_post_validation_results"
            if bool(post_validation_run.get("run_allowed"))
            else "refresh_rolling_validation_and_objective_audit"
        )
    elif _as_int(actual_report.get("inserted_count")) > 0:
        next_action = "review_label_plan_then_apply_labels"
    elif _as_int(label_plan.get("planned_labels")) > 0 and not label_allowed:
        next_action = "review_and_apply_settlement_calibration_labels"
    elif _as_int(actual_report.get("ready_count")) > 0:
        next_action = "confirm_and_apply_settlement_actuals"
    else:
        next_action = "continue_collecting_paper_observation_samples"
    return {
        "ok": not blocking_statuses,
        "read_only": not (actual_allowed or label_allowed),
        "generated_at": _utc_now(),
        "db_path": str(db_path),
        "since_days": max(1, int(since_days)),
        "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
        "actual_apply": {
            "apply_requested": bool(apply_actuals),
            "apply_allowed": actual_allowed,
            "confirmation_ok": bool(actual_report.get("apply_confirmation_ok")),
            "candidate_count": _as_int(actual_report.get("candidate_count")),
            "ready_count": _as_int(actual_report.get("ready_count")),
            "inserted_count": _as_int(actual_report.get("inserted_count")),
            "next_action": actual_report.get("next_action"),
            "results": list(actual_report.get("results") or [])[:5],
            "review_packet": dict(actual_report.get("review_packet") or {}),
            "required_confirm_apply": apply_settlement_actual_repair_plan.APPLY_CONFIRM_PHRASE,
        },
        "label_plan": {
            "planned_labels": _as_int(label_plan.get("planned_labels")),
            "decisions_scanned": _as_int(label_plan.get("decisions_scanned")),
            "skipped": dict(label_plan.get("skipped") or {}),
            "settlement_skipped": dict(label_plan.get("settlement_skipped") or {}),
            "labels_preview": list(label_plan.get("labels") or [])[:5],
        },
        "label_apply": label_apply,
        "post_label_apply_validation": post_label_validation,
        "post_validation_run": post_validation_run,
        "after_actual_apply_simulation": simulation,
        "pre_apply_backup": pre_apply_backup,
        "blockers": blocking_statuses,
        "next_action": next_action,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Two-confirmation settlement actual + label apply flow. Default is dry-run."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since-days", type=int, default=42)
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE)
    parser.add_argument("--refresh-preview", action="store_true")
    parser.add_argument("--apply-actuals", action="store_true")
    parser.add_argument(
        "--confirm-actuals",
        default="",
        help=(
            "Required with --apply-actuals. Must equal "
            f"{apply_settlement_actual_repair_plan.APPLY_CONFIRM_PHRASE}."
        ),
    )
    parser.add_argument("--apply-labels", action="store_true")
    parser.add_argument(
        "--confirm-labels",
        default="",
        help=(
            "Required with --apply-labels. Must equal "
            f"{build_settlement_calibration_labels.APPLY_CONFIRM_PHRASE}."
        ),
    )
    parser.add_argument(
        "--run-post-validation",
        action="store_true",
        help="After confirmed label apply, refresh rolling validation and objective audit reports.",
    )
    parser.add_argument(
        "--no-backup-before-apply",
        action="store_true",
        help="Disable the automatic SQLite backup that normally runs before confirmed applies.",
    )
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(
        Path(args.db),
        since_days=int(args.since_days),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        refresh_preview=bool(args.refresh_preview),
        apply_actuals=bool(args.apply_actuals),
        confirm_actuals=str(args.confirm_actuals or ""),
        apply_labels=bool(args.apply_labels),
        confirm_labels=str(args.confirm_labels or ""),
        run_post_validation=bool(args.run_post_validation),
        backup_before_apply=not bool(args.no_backup_before_apply),
        backup_dir=Path(args.backup_dir),
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
