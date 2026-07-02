#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import settlement_validation_pipeline  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "settlement_closed_loop_audit_latest.json"
DEFAULT_MARKET_SCOPE = "highest_temperature"
DEFAULT_ROLLING_REPORT = ROOT / "data" / "reports" / "paper_observation_rolling_validation_latest.json"
DEFAULT_OBJECTIVE_REPORT = ROOT / "data" / "reports" / "strategy_objective_completion_audit_latest.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _command_map(pipeline: Mapping[str, Any]) -> dict[str, str]:
    apply_actual = pipeline.get("settlement_actual_apply_dry_run")
    label = pipeline.get("settlement_label_dry_run")
    post = pipeline.get("post_label_apply_validation")
    flow = pipeline.get("flow_commands")
    if not isinstance(apply_actual, Mapping):
        apply_actual = {}
    if not isinstance(label, Mapping):
        label = {}
    if not isinstance(post, Mapping):
        post = {}
    if not isinstance(flow, Mapping):
        flow = {}
    post_commands = post.get("commands")
    if not isinstance(post_commands, Mapping):
        post_commands = {}
    return {
        "apply_actuals": str(
            apply_actual.get("flow_confirm_command")
            or flow.get("apply_actuals")
            or apply_actual.get("confirm_command")
            or ""
        ),
        "apply_labels": str(label.get("flow_apply_command") or flow.get("apply_labels") or label.get("apply_command") or ""),
        "apply_actuals_and_labels": str(
            apply_actual.get("flow_confirm_and_label_apply_command")
            or flow.get("apply_actuals_and_labels")
            or ""
        ),
        "apply_actuals_labels_and_validate": str(
            apply_actual.get("flow_confirm_label_apply_and_validate_command")
            or flow.get("apply_actuals_labels_and_validate")
            or ""
        ),
        "rolling_validation": str(post_commands.get("rolling_validation") or ""),
        "objective_audit": str(post_commands.get("objective_audit") or ""),
    }


def _phase_for(
    *,
    actual_ready: int,
    labels_planned: int,
    sim_labels_after_actual: int,
    rolling_ok: bool | None,
    objective_ok: bool | None,
) -> str:
    if actual_ready > 0:
        return "waiting_actual_confirmation"
    if labels_planned > 0:
        return "waiting_label_confirmation"
    if sim_labels_after_actual > 0:
        return "waiting_actual_confirmation_then_label_confirmation"
    if rolling_ok is False:
        return "waiting_paper_observation_rolling_validation"
    if objective_ok is False:
        return "waiting_objective_audit_completion"
    if rolling_ok is True and objective_ok is True:
        return "settlement_closed_loop_validated"
    return "collecting_or_waiting_for_settlement_samples"


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    since_days: int = 42,
    market_scope: str = DEFAULT_MARKET_SCOPE,
    rolling_report_path: Path = DEFAULT_ROLLING_REPORT,
    objective_report_path: Path = DEFAULT_OBJECTIVE_REPORT,
    simulate_after_actual_apply: bool = True,
) -> dict[str, Any]:
    pipeline = settlement_validation_pipeline.build_report(
        db_path,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        fetch_preview=False,
        simulate_after_actual_apply=bool(simulate_after_actual_apply),
    )
    actual = pipeline.get("settlement_actual_apply_dry_run")
    label = pipeline.get("settlement_label_dry_run")
    simulation = pipeline.get("after_actual_apply_simulation")
    if not isinstance(actual, Mapping):
        actual = {}
    if not isinstance(label, Mapping):
        label = {}
    if not isinstance(simulation, Mapping):
        simulation = {}

    rolling = _load_json(rolling_report_path)
    objective = _load_json(objective_report_path)
    rolling_ok = bool(rolling.get("ok")) if rolling else None
    objective_ok = bool(objective.get("ok")) if objective else None
    actual_ready = _as_int(actual.get("ready_count"))
    labels_planned = _as_int(label.get("planned_labels"))
    sim_labels = _as_int(simulation.get("label_planned_after_actual_apply"))
    phase = _phase_for(
        actual_ready=actual_ready,
        labels_planned=labels_planned,
        sim_labels_after_actual=sim_labels,
        rolling_ok=rolling_ok,
        objective_ok=objective_ok,
    )
    blockers: list[str] = []
    if actual_ready > 0:
        blockers.append("settlement_actuals_waiting_confirmation")
    if labels_planned > 0:
        blockers.append("settlement_labels_waiting_confirmation")
    if sim_labels > 0 and actual_ready > 0:
        blockers.append("settlement_labels_predicted_after_actual_apply")
    if rolling_ok is False:
        blockers.append("paper_observation_rolling_validation_not_passed")
    if objective_ok is False:
        blockers.append("strategy_objective_audit_not_passed")

    commands = _command_map(pipeline)
    write_safety = pipeline.get("write_safety")
    if not isinstance(write_safety, Mapping):
        write_safety = {}
    review_packet = actual.get("review_packet")
    if not isinstance(review_packet, Mapping):
        review_packet = {}
    next_actions: list[str] = []
    if actual_ready > 0 and commands.get("apply_actuals"):
        next_actions.append("review_and_confirm_settlement_actual_apply")
    if actual_ready > 0 and commands.get("apply_actuals_and_labels"):
        next_actions.append("optional_double_confirm_actuals_and_labels_apply")
    if actual_ready > 0 and commands.get("apply_actuals_labels_and_validate"):
        next_actions.append("optional_double_confirm_apply_and_refresh_validation")
    if labels_planned > 0 and commands.get("apply_labels"):
        next_actions.append("review_and_confirm_settlement_label_apply")
    if sim_labels > 0 and commands.get("rolling_validation"):
        next_actions.append("after_label_apply_run_rolling_validation")
        next_actions.append("after_label_apply_run_objective_audit")
    if not next_actions:
        next_actions.append(str(pipeline.get("next_action") or "continue_collecting_paper_observation_samples"))

    return {
        "ok": not blockers and phase == "settlement_closed_loop_validated",
        "read_only": True,
        "generated_at": _utc_now(),
        "db_path": str(db_path),
        "since_days": max(1, int(since_days)),
        "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
        "phase": phase,
        "pipeline_next_action": str(pipeline.get("next_action") or ""),
        "actual_ready": actual_ready,
        "labels_planned": labels_planned,
        "sim_labels_after_actual_apply": sim_labels,
        "sim_actual_inserted": _as_int(simulation.get("actual_inserted_count")),
        "sim_label_apply_inserted": _as_int(
            simulation.get("label_apply_inserted_count")
        ),
        "sim_label_planned_after_label_apply": _as_int(
            simulation.get("label_planned_after_label_apply")
        ),
        "sim_label_apply_closed_loop_ok": bool(
            simulation.get("label_apply_closed_loop_ok")
        ),
        "actual_review_packet": {
            "ready_count": _as_int(review_packet.get("ready_count")),
            "would_insert_event_run_rows": _as_int(
                review_packet.get("would_insert_event_run_rows")
            ),
            "requires_manual_review": bool(review_packet.get("requires_manual_review")),
            "items": list(review_packet.get("items") or [])[:8],
            "confirm_command": str(review_packet.get("confirm_command") or commands.get("apply_actuals") or ""),
        },
        "rolling_validation": {
            "path": str(rolling_report_path),
            "loaded": bool(rolling),
            "ok": rolling_ok,
            "blockers": list(rolling.get("blockers") or [])[:8] if rolling else [],
            "next_mainline_task": str(rolling.get("next_mainline_task") or "") if rolling else "",
        },
        "objective_audit": {
            "path": str(objective_report_path),
            "loaded": bool(objective),
            "ok": objective_ok,
            "blockers": list(objective.get("blockers") or [])[:8] if objective else [],
        },
        "commands": commands,
        "write_safety": {
            "pre_apply_backup_required": bool(
                write_safety.get("pre_apply_backup_required", True)
            ),
            "backup_default": str(write_safety.get("backup_default") or "enabled"),
            "bypass_effect": str(
                write_safety.get("bypass_effect") or "confirmed_apply_blocked"
            ),
        },
        "blockers": blockers,
        "next_actions": next_actions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only settlement actual -> label -> validation closed-loop audit.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since-days", type=int, default=42)
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE)
    parser.add_argument("--rolling-report", default=str(DEFAULT_ROLLING_REPORT))
    parser.add_argument("--objective-report", default=str(DEFAULT_OBJECTIVE_REPORT))
    parser.add_argument("--no-simulate-after-actual-apply", action="store_true")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(
        Path(args.db),
        since_days=int(args.since_days),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        rolling_report_path=Path(args.rolling_report),
        objective_report_path=Path(args.objective_report),
        simulate_after_actual_apply=not bool(args.no_simulate_after_actual_apply),
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
