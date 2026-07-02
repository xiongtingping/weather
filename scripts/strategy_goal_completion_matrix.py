#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = ROOT / "data" / "reports"
DEFAULT_OUT = DEFAULT_REPORT_DIR / "strategy_goal_completion_matrix_latest.json"

REQUIRED_OBJECTIVE_REQUIREMENTS = [
    "hybrid_edge_and_settlement_strategy",
    "reverse_edge_buy_no_sell_yes",
    "adaptive_lcb_edge_floor_after_fees",
    "resource_focus_70_structural_30_major_settlement",
    "d0_physical_likelihood_gate",
    "resolution_source_station_strict_check",
    "seasonal_size_scale_contract",
    "entry_exit_edge_contract",
    "settlement_write_safety_backup_gate",
    "launchd_monitor_24_7_path",
]

NON_BLOCKING_OBJECTIVE_PARTIALS = {
    "resource_focus_70_structural_30_major_settlement",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _status(ok: Any, blockers: list[str] | None = None) -> str:
    if blockers:
        return "blocked"
    if ok is True:
        return "proved"
    if ok is False:
        return "blocked"
    return "unverified"


def _objective_rows(objective: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_name: dict[str, Mapping[str, Any]] = {}
    for item in objective.get("requirements") or []:
        if isinstance(item, Mapping) and str(item.get("name") or ""):
            by_name[str(item.get("name"))] = item

    rows: list[dict[str, Any]] = []
    for name in REQUIRED_OBJECTIVE_REQUIREMENTS:
        item = by_name.get(name)
        if not item:
            rows.append(
                {
                    "key": name,
                    "name": name,
                    "status": "stale_or_missing",
                    "ok": False,
                    "blockers": ["requirement_missing_from_latest_objective_audit"],
                }
            )
            continue
        blockers = [str(x) for x in item.get("blockers") or []]
        source_status = str(item.get("status") or "").strip()
        partial_is_report_only = (
            name in NON_BLOCKING_OBJECTIVE_PARTIALS
            and source_status == "partial"
            and bool(item.get("ok"))
            and not blockers
        )
        if source_status and source_status != "proved" and not blockers and not partial_is_report_only:
            blockers.append(f"objective_requirement_not_proved:{source_status}")
        status = source_status or _status(item.get("ok"), blockers)
        rows.append(
            {
                "key": name,
                "name": name,
                "status": status,
                "ok": bool(item.get("ok"))
                and not blockers
                and (status == "proved" or partial_is_report_only),
                "blockers": blockers,
                "warnings": [str(x) for x in item.get("warnings") or []],
                "evidence": dict(item.get("evidence") or {})
                if isinstance(item.get("evidence"), Mapping)
                else {},
            }
        )
    return rows


def build_report(report_dir: Path = DEFAULT_REPORT_DIR) -> dict[str, Any]:
    objective_path = report_dir / "strategy_objective_completion_audit_latest.json"
    closed_loop_path = report_dir / "settlement_closed_loop_audit_latest.json"
    pipeline_path = report_dir / "settlement_validation_pipeline_latest.json"
    gap_scan_path = report_dir / "recommended_pilot_gap_scan_plan_latest.json"
    gap_refresh_path = report_dir / "recommended_pilot_gap_refresh_health_latest.json"
    target_package_path = report_dir / "paper_observation_target_package_latest.json"
    apply_flow_path = report_dir / "settlement_validation_apply_flow_latest.json"
    confirmation_packet_path = report_dir / "settlement_confirmation_packet_latest.json"

    objective = _load_json(objective_path)
    closed_loop = _load_json(closed_loop_path)
    pipeline = _load_json(pipeline_path)
    gap_scan = _load_json(gap_scan_path)
    gap_refresh = _load_json(gap_refresh_path)
    target_package = _load_json(target_package_path)
    apply_flow = _load_json(apply_flow_path)
    confirmation_packet = _load_json(confirmation_packet_path)

    objective_rows = _objective_rows(objective)
    paper_source_row = next(
        (
            item
            for item in objective.get("requirements") or []
            if isinstance(item, Mapping)
            and item.get("name") == "paper_small_cap_scale_validation_before_scaling"
        ),
        {},
    )
    if not paper_source_row:
        automation_reports = objective.get("automation_reports")
        if isinstance(automation_reports, Mapping):
            paper_source = automation_reports.get("paper_small_cap_scale_validation_before_scaling")
            if isinstance(paper_source, Mapping):
                paper_source_row = paper_source
    paper_samples_needed = bool(paper_source_row) and not bool(paper_source_row.get("ok"))
    write_safety = pipeline.get("write_safety") if isinstance(pipeline, Mapping) else {}
    if not isinstance(write_safety, Mapping):
        write_safety = {}
    write_safety_ok = (
        bool(write_safety.get("pre_apply_backup_required"))
        and str(write_safety.get("bypass_effect") or "") == "confirmed_apply_blocked"
    )
    write_safety_status = "proved" if write_safety_ok else "stale_or_missing"

    settlement_blockers = [str(x) for x in closed_loop.get("blockers") or []]
    settlement_row = {
        "key": "real_settlement_label_closed_loop",
        "name": "real_settlement_label_closed_loop",
        "goal_blocking": False,
        "status": _status(closed_loop.get("ok"), settlement_blockers),
        "ok": bool(closed_loop.get("ok")) and not settlement_blockers,
        "phase": str(closed_loop.get("phase") or ""),
        "actual_ready": int(closed_loop.get("actual_ready") or 0),
        "labels_planned": int(closed_loop.get("labels_planned") or 0),
        "sim_labels_after_actual_apply": int(
            closed_loop.get("sim_labels_after_actual_apply") or 0
        ),
        "sim_label_apply_closed_loop_ok": bool(
            closed_loop.get("sim_label_apply_closed_loop_ok")
        ),
        "blockers": settlement_blockers,
    }
    safety_row = {
        "key": "latest_pipeline_write_safety_schema",
        "name": "latest_pipeline_write_safety_schema",
        "status": write_safety_status,
        "ok": write_safety_ok,
        "blockers": [] if write_safety_ok else ["write_safety_missing_or_not_proved"],
        "write_safety": dict(write_safety),
    }
    gap_refresh_blockers = [str(x) for x in gap_refresh.get("blockers") or []]
    gap_refresh_warnings = [str(x) for x in gap_refresh.get("warnings") or []]
    gap_refresh_row = {
        "key": "recommended_pilot_gap_refresh_health",
        "name": "recommended_pilot_gap_refresh_health",
        "goal_blocking": False,
        "status": (
            "stale_or_missing" if not gap_refresh else _status(gap_refresh.get("ok"), gap_refresh_blockers)
        ),
        "ok": bool(gap_refresh.get("ok")) and not gap_refresh_blockers,
        "queue_count": int(gap_refresh.get("queue_count") or 0),
        "priority_slug_count": int(gap_refresh.get("priority_slug_count") or 0),
        "total_priority_slug_count": int(gap_refresh.get("total_priority_slug_count") or 0),
        "blockers": (
            gap_refresh_blockers
            if gap_refresh
            else ["recommended_pilot_gap_refresh_health_missing"]
        ),
        "warnings": gap_refresh_warnings,
    }
    gap_scan_blockers: list[str] = []
    if paper_samples_needed:
        if not gap_scan:
            gap_scan_blockers.append("paper_observation_gap_scan_plan_missing")
        else:
            latest_objective_generated_at = str(objective.get("generated_at") or "")
            gap_audit_generated_at = str(gap_scan.get("audit_generated_at") or "")
            if (
                objective
                and latest_objective_generated_at
                and gap_audit_generated_at != latest_objective_generated_at
            ):
                gap_scan_blockers.append(
                    "paper_observation_gap_scan_plan_stale_for_latest_objective_audit"
                )
            latest_target_generated_at = str(target_package.get("generated_at") or "")
            gap_target_generated_at = str(gap_scan.get("target_package_generated_at") or "")
            if (
                target_package
                and latest_target_generated_at
                and gap_target_generated_at != latest_target_generated_at
            ):
                gap_scan_blockers.append(
                    "paper_observation_gap_scan_plan_stale_for_latest_target_package"
                )
            if _as_int(gap_scan.get("queue_count")) <= 0:
                gap_scan_blockers.append("paper_observation_gap_scan_queue_missing")
            elif _as_int(gap_scan.get("priority_slug_count")) <= 0:
                gap_scan_blockers.append("paper_observation_gap_priority_slugs_missing")
    gap_scan_plan = gap_scan.get("plan") if isinstance(gap_scan, Mapping) else []
    if not isinstance(gap_scan_plan, list):
        gap_scan_plan = []
    gap_scan_row = {
        "key": "paper_observation_gap_scan_plan_ready",
        "name": "paper_observation_gap_scan_plan_ready",
        "goal_blocking": False,
        "status": _status(
            (not paper_samples_needed) or bool(gap_scan and not gap_scan_blockers),
            gap_scan_blockers,
        ),
        "ok": (not paper_samples_needed) or bool(gap_scan and not gap_scan_blockers),
        "paper_samples_needed": paper_samples_needed,
        "queue_count": _as_int(gap_scan.get("queue_count")),
        "city_count": _as_int(gap_scan.get("city_count")),
        "strategy_gap_count": _as_int(gap_scan.get("strategy_gap_count")),
        "priority_slug_count": _as_int(gap_scan.get("priority_slug_count")),
        "audit_generated_at": str(gap_scan.get("audit_generated_at") or ""),
        "latest_objective_audit_generated_at": str(objective.get("generated_at") or ""),
        "target_package_generated_at": str(gap_scan.get("target_package_generated_at") or ""),
        "latest_target_package_generated_at": str(target_package.get("generated_at") or ""),
        "priority_slugs_preview": [
            str(value)
            for value in (gap_scan.get("priority_slugs") or [])[:5]
            if str(value or "").strip()
        ],
        "plan_preview": [
            {
                "city": str(item.get("city") or ""),
                "needed_resolved_rows": _as_int(item.get("needed_resolved_rows")),
                "resolved_rows": _as_int(item.get("resolved_rows")),
                "pending_rows": _as_int(item.get("pending_rows")),
                "strategy_gap_count": len(item.get("strategy_gaps") or []),
            }
            for item in gap_scan_plan[:3]
            if isinstance(item, Mapping)
        ],
        "blockers": gap_scan_blockers,
    }
    confirmation_blockers = [str(x) for x in confirmation_packet.get("blockers") or []]
    if confirmation_packet:
        if not apply_flow:
            confirmation_blockers.append("settlement_validation_apply_flow_missing")
        else:
            flow_generated_at = str(apply_flow.get("generated_at") or "")
            packet_flow_generated_at = str(confirmation_packet.get("flow_generated_at") or "")
            if not flow_generated_at or packet_flow_generated_at != flow_generated_at:
                confirmation_blockers.append(
                    "settlement_confirmation_packet_stale_for_latest_apply_flow"
                )
            actual_apply = apply_flow.get("actual_apply")
            if not isinstance(actual_apply, Mapping):
                actual_apply = {}
            review_packet = actual_apply.get("review_packet")
            if not isinstance(review_packet, Mapping):
                review_packet = {}
            if _as_int(confirmation_packet.get("actual_ready")) != _as_int(
                actual_apply.get("ready_count")
            ):
                confirmation_blockers.append(
                    "settlement_confirmation_packet_actual_ready_mismatch"
                )
            if _as_int(
                confirmation_packet.get("would_insert_event_run_rows")
            ) != _as_int(review_packet.get("would_insert_event_run_rows")):
                confirmation_blockers.append(
                    "settlement_confirmation_packet_insert_rows_mismatch"
                )
    confirmation_row = {
        "key": "settlement_confirmation_packet_ready",
        "name": "settlement_confirmation_packet_ready",
        "goal_blocking": False,
        "status": (
            "stale_or_missing"
            if not confirmation_packet
            else _status(confirmation_packet.get("ok"), confirmation_blockers)
        ),
        "ok": bool(confirmation_packet.get("ok")) and not confirmation_blockers,
        "actual_ready": int(confirmation_packet.get("actual_ready") or 0),
        "would_insert_event_run_rows": int(
            confirmation_packet.get("would_insert_event_run_rows") or 0
        ),
        "blockers": (
            confirmation_blockers
            if confirmation_packet
            else ["settlement_confirmation_packet_missing"]
        ),
        "flow_generated_at": str(confirmation_packet.get("flow_generated_at") or ""),
        "latest_apply_flow_generated_at": str(apply_flow.get("generated_at") or ""),
    }

    rows = objective_rows + [
        safety_row,
        gap_scan_row,
        gap_refresh_row,
        confirmation_row,
        settlement_row,
    ]
    blockers = [
        f"{row['name']}:{code}"
        for row in rows
        if row.get("goal_blocking", True)
        for code in row.get("blockers") or []
    ]
    goal_rows = [row for row in rows if row.get("goal_blocking", True)]
    ok = not blockers and all(row.get("ok") is True for row in goal_rows)
    return {
        "ok": bool(ok),
        "read_only": True,
        "generated_at": _utc_now(),
        "report_dir": str(report_dir),
        "inputs": {
            "objective_audit": str(objective_path),
            "settlement_closed_loop": str(closed_loop_path),
            "settlement_pipeline": str(pipeline_path),
            "recommended_pilot_gap_scan_plan": str(gap_scan_path),
            "recommended_pilot_gap_refresh_health": str(gap_refresh_path),
            "paper_observation_target_package": str(target_package_path),
            "settlement_validation_apply_flow": str(apply_flow_path),
            "settlement_confirmation_packet": str(confirmation_packet_path),
        },
        "objective_ok": bool(objective.get("ok")) if objective else None,
        "settlement_phase": str(closed_loop.get("phase") or ""),
        "rows": rows,
        "blockers": blockers,
        "report_only_rows": [
            str(row.get("name") or "")
            for row in rows
            if not row.get("goal_blocking", True)
        ],
        "next_mainline_task": (
            "refresh_latest_audits_to_current_schema"
            if any(
                row["status"] == "stale_or_missing"
                for row in rows
                if row.get("goal_blocking", True)
            )
            else (
                "strategy_goal_completion_review"
                if ok
                else "inspect_goal_blocking_requirements"
            )
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only matrix for the hybrid edge + settlement strategy goal."
    )
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_report(Path(args.report_dir))
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
