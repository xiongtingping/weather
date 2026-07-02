#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "archive.db"
STATE_PATH = DATA_DIR / "daily_trade_sms_summary_state.json"
ENV_PATH = ROOT / ".env"


def _load_paper_validation_status() -> Optional[Dict[str, object]]:
    if not _env_bool("PM_SMS_DAILY_INCLUDE_PAPER_VALIDATION", True):
        return None
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts import paper_trading_validation
    except Exception as exc:
        return {"ok": False, "error": f"import_error:{type(exc).__name__}"}
    city = str(__import__("os").environ.get("PM_PAPER_VALIDATION_CITY", "") or "").strip()
    market_scope = str(
        __import__("os").environ.get("PM_PAPER_VALIDATION_MARKET_SCOPE", "highest_temperature")
        or "highest_temperature"
    ).strip()
    since_raw = str(__import__("os").environ.get("PM_PAPER_VALIDATION_SINCE_DAYS", "28") or "28").strip()
    try:
        since_days = max(1, int(since_raw))
    except Exception:
        since_days = 28
    try:
        report = paper_trading_validation.build_report(
            DB_PATH,
            since_days=since_days,
            city=city,
            market_scope=market_scope,
        )
    except Exception as exc:
        return {"ok": False, "error": f"report_error:{type(exc).__name__}"}
    status = report.get("paper_status") if isinstance(report, dict) else {}
    if not isinstance(status, dict):
        status = {}
    return {
        "ok": bool(report.get("ok")) if isinstance(report, dict) else False,
        "city": city,
        "market_scope": market_scope,
        "bayes_samples": int(status.get("bayes_samples") or 0),
        "coverage": status.get("bayes_decision_coverage"),
        "will_order_rows": int(status.get("will_order_rows") or 0),
        "tradeability_gap_paper_candidates": int(
            status.get("tradeability_gap_paper_candidates") or 0
        ),
        "planned_labels": int(status.get("planned_labels") or 0),
        "settlement_missing": int(status.get("settlement_missing") or 0),
        "reliability_brier": status.get("reliability_brier"),
        "reliability_ece": status.get("reliability_ece"),
        "reliability_bins_ready": bool(status.get("reliability_bins_ready")),
        "warnings": list(report.get("warnings") or [])[:4] if isinstance(report, dict) else [],
        "blockers": list(report.get("blockers") or [])[:4] if isinstance(report, dict) else [],
    }


def _load_small_capital_validation_status() -> Optional[Dict[str, object]]:
    if not _env_bool("PM_SMS_DAILY_INCLUDE_SMALL_CAPITAL_VALIDATION", True):
        return None
    pilot_raw = str(__import__("os").environ.get("PM_SMALL_CAPITAL_PILOT_CITIES", "") or "").strip()
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts import small_capital_validation_review
    except Exception as exc:
        return {"ok": False, "error": f"import_error:{type(exc).__name__}"}
    market_scope = str(
        __import__("os").environ.get("PM_SMALL_CAPITAL_MARKET_SCOPE", "highest_temperature")
        or "highest_temperature"
    ).strip()
    since_raw = str(__import__("os").environ.get("PM_SMALL_CAPITAL_VALIDATION_SINCE_DAYS", "42") or "42").strip()
    try:
        since_days = max(1, int(since_raw))
    except Exception:
        since_days = 42
    try:
        report = small_capital_validation_review.build_report(
            DB_PATH,
            pilot_cities=[pilot_raw] if pilot_raw else [],
            since_days=since_days,
            market_scope=market_scope,
        )
    except Exception as exc:
        return {"ok": False, "error": f"report_error:{type(exc).__name__}"}
    pilot_scope = report.get("pilot_scope") if isinstance(report, dict) else {}
    if not isinstance(pilot_scope, dict):
        pilot_scope = {}
    pilot_source = str(pilot_scope.get("source") or ("env" if pilot_raw else "missing"))
    caps = report.get("small_capital_caps") if isinstance(report, dict) else {}
    if not isinstance(caps, dict):
        caps = {}
    pilot_candidate_review = report.get("pilot_candidate_review") if isinstance(report, dict) else {}
    if not isinstance(pilot_candidate_review, dict):
        pilot_candidate_review = {}
    candidate_total = 0
    reverse_no_total = 0
    d0_settlement_total = 0
    by_city_compact: Dict[str, Dict[str, int]] = {}
    for city, item in (pilot_candidate_review.get("by_city") or {}).items():
        if not isinstance(item, dict):
            continue
        actions = item.get("by_review_action") if isinstance(item.get("by_review_action"), dict) else {}
        strategies = (
            item.get("by_candidate_strategy")
            if isinstance(item.get("by_candidate_strategy"), dict)
            else {}
        )
        obs = int(item.get("paper_observation_candidates") or 0)
        reverse_no = int(strategies.get("reverse_edge_buy_no_sell_yes") or 0)
        d0_settlement = int(strategies.get("d0_physical_settlement_hybrid") or 0)
        candidate_total += obs
        reverse_no_total += reverse_no
        d0_settlement_total += d0_settlement
        by_city_compact[str(city)] = {
            "obs": obs,
            "reverse_no": reverse_no,
            "d0_settlement": d0_settlement,
            "blocked_or_excluded": int(actions.get("exclude_until_tradeability_gap_fixed") or 0),
        }
    tracking_summary: Dict[str, object] = {}
    tracking_path = DATA_DIR / "reports" / "paper_observation_tracking_latest.json"
    try:
        if tracking_path.exists():
            tracking_payload = json.loads(tracking_path.read_text(encoding="utf-8"))
            if isinstance(tracking_payload, dict) and isinstance(tracking_payload.get("summary"), dict):
                tracking_summary = dict(tracking_payload.get("summary") or {})
    except Exception:
        tracking_summary = {}
    rolling_summary, rolling_blockers = _load_paper_observation_rolling_summary()
    observation_target_summary = _load_paper_observation_target_summary(
        pilot_cities=[str(city) for city in (pilot_scope.get("cities") or [])],
        market_scope=market_scope,
    )
    return {
        "ok": bool(report.get("ok")) if isinstance(report, dict) else False,
        "cities": list(pilot_scope.get("cities") or []),
        "pilot_city_source": pilot_source,
        "secondary_only": bool(pilot_scope.get("secondary_only")),
        "max_per_order_usdc": caps.get("max_per_order_usdc"),
        "max_daily_usdc": caps.get("max_daily_usdc"),
        "paper_observation_allowed": bool(pilot_candidate_review.get("paper_observation_allowed")),
        "small_capital_candidate_ready": bool(
            pilot_candidate_review.get("small_capital_candidate_ready")
        ),
        "pilot_candidate_total": candidate_total,
        "pilot_reverse_no_total": reverse_no_total,
        "pilot_d0_settlement_total": d0_settlement_total,
        "pilot_candidate_by_city": by_city_compact,
        "pilot_observation_report": str(
            DATA_DIR / "reports" / "pilot_observation_review_latest.md"
        ),
        "paper_observation_tracking_report": str(tracking_path),
        "paper_observation_tracking_summary": tracking_summary,
        "paper_observation_rolling_summary": rolling_summary,
        "paper_observation_rolling_blockers": rolling_blockers,
        "paper_observation_target_summary": observation_target_summary,
        "warnings": list(report.get("warnings") or [])[:4] if isinstance(report, dict) else [],
        "blockers": list(report.get("blockers") or [])[:4] if isinstance(report, dict) else [],
        "next_mainline_task": str(report.get("next_mainline_task") or "") if isinstance(report, dict) else "",
    }


def _load_paper_observation_rolling_summary() -> Tuple[Dict[str, object], List[str]]:
    rolling_path = DATA_DIR / "reports" / "paper_observation_rolling_validation_latest.json"
    try:
        if not rolling_path.exists():
            return {}, []
        payload = json.loads(rolling_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}, []
        overall = payload.get("overall") if isinstance(payload.get("overall"), dict) else {}
        blockers = list(payload.get("blockers") or [])[:4]
        return dict(overall), [str(x) for x in blockers]
    except Exception:
        return {}, []


def _load_paper_observation_target_summary(
    *,
    pilot_cities: List[str],
    market_scope: str,
) -> Dict[str, object]:
    if not _env_bool("PM_SMS_DAILY_INCLUDE_PAPER_OBSERVATION_TARGETS", True):
        return {}
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts import paper_observation_target_package
    except Exception:
        return {}
    try:
        package = paper_observation_target_package.build_package(
            DB_PATH,
            market_scope=market_scope,
            cities=pilot_cities,
            max_cities=2,
        )
    except Exception:
        return {}
    if not isinstance(package, dict):
        return {}
    return {
        "ok": bool(package.get("ok")),
        "next_mainline_task": str(package.get("next_mainline_task") or ""),
        "city_targets": list(package.get("city_targets") or [])[:2],
        "recommended_pilot_city_targets": list(
            package.get("recommended_pilot_city_targets") or []
        )[:3],
        "observation_expansion_city_targets": list(
            package.get("observation_expansion_city_targets") or []
        )[:3],
        "strategy_targets": list(package.get("strategy_targets") or [])[:3],
        "report": str(DATA_DIR / "reports" / "paper_observation_target_package_latest.md"),
    }


def _load_scale_up_validation_status() -> Optional[Dict[str, object]]:
    if not _env_bool("PM_SMS_DAILY_INCLUDE_SCALE_UP_VALIDATION", True):
        return None
    pilot_raw = str(__import__("os").environ.get("PM_SMALL_CAPITAL_PILOT_CITIES", "") or "").strip()
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts import scale_up_validation_review
    except Exception as exc:
        return {"ok": False, "error": f"import_error:{type(exc).__name__}"}
    market_scope = str(
        __import__("os").environ.get("PM_SMALL_CAPITAL_MARKET_SCOPE", "highest_temperature")
        or "highest_temperature"
    ).strip()
    since_raw = str(__import__("os").environ.get("PM_SCALE_UP_VALIDATION_SINCE_DAYS", "42") or "42").strip()
    try:
        since_days = max(1, int(since_raw))
    except Exception:
        since_days = 42
    try:
        report = scale_up_validation_review.build_report(
            DB_PATH,
            pilot_cities=[pilot_raw] if pilot_raw else [],
            since_days=since_days,
            market_scope=market_scope,
        )
    except Exception as exc:
        return {"ok": False, "error": f"report_error:{type(exc).__name__}"}
    caps = report.get("scale_up_caps") if isinstance(report, dict) else {}
    if not isinstance(caps, dict):
        caps = {}
    pilot_scope = report.get("pilot_scope") if isinstance(report, dict) else {}
    if not isinstance(pilot_scope, dict):
        pilot_scope = {}
    if not list(pilot_scope.get("cities") or []):
        return None
    rolling_summary, rolling_blockers = _load_paper_observation_rolling_summary()
    return {
        "ok": bool(report.get("ok")) if isinstance(report, dict) else False,
        "cities": list(pilot_scope.get("cities") or []),
        "secondary_only": bool(pilot_scope.get("secondary_only")),
        "max_per_order_usdc": caps.get("max_per_order_usdc"),
        "max_daily_usdc": caps.get("max_daily_usdc"),
        "paper_observation_rolling_summary": rolling_summary,
        "paper_observation_rolling_blockers": rolling_blockers,
        "warnings": list(report.get("warnings") or [])[:4] if isinstance(report, dict) else [],
        "blockers": list(report.get("blockers") or [])[:4] if isinstance(report, dict) else [],
        "next_mainline_task": str(report.get("next_mainline_task") or "") if isinstance(report, dict) else "",
    }


def _load_settlement_label_status() -> Optional[Dict[str, object]]:
    if not _env_bool("PM_SMS_DAILY_INCLUDE_SETTLEMENT_LABEL_STATUS", True):
        return None
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts import settlement_label_eligibility_report
    except Exception as exc:
        return {"ok": False, "error": f"import_error:{type(exc).__name__}"}
    city = str(__import__("os").environ.get("PM_SETTLEMENT_LABEL_CITY", "") or "").strip()
    market_scope = str(
        __import__("os").environ.get("PM_SETTLEMENT_LABEL_MARKET_SCOPE", "highest_temperature")
        or "highest_temperature"
    ).strip()
    since_raw = str(__import__("os").environ.get("PM_SETTLEMENT_LABEL_SINCE_DAYS", "30") or "30").strip()
    try:
        since_days = max(1, int(since_raw))
    except Exception:
        since_days = 30
    review_cmd = (
        f".venv/bin/python scripts/settlement_label_eligibility_report.py --since-days {since_days} "
        f"--market-scope {market_scope} --limit 25"
    )
    if city:
        review_cmd += f" --city {city}"
    try:
        report = settlement_label_eligibility_report.build_report(
            DB_PATH,
            since_days=since_days,
            city=city,
            market_scope=market_scope,
            limit=0,
        )
    except Exception as exc:
        return {"ok": False, "error": f"report_error:{type(exc).__name__}"}
    return {
        "ok": bool(report.get("ok")) if isinstance(report, dict) else False,
        "city": city,
        "market_scope": market_scope,
        "market_count": int(report.get("market_count") or 0) if isinstance(report, dict) else 0,
        "ready_market_count": int(report.get("ready_market_count") or 0) if isinstance(report, dict) else 0,
        "planned_labels": int(report.get("planned_labels") or 0) if isinstance(report, dict) else 0,
        "status_counts": dict(report.get("status_counts") or {}) if isinstance(report, dict) else {},
        "review_command": review_cmd,
        "apply_command": str(report.get("apply_command") or "") if isinstance(report, dict) else "",
        "next_mainline_task": str(report.get("next_mainline_task") or "") if isinstance(report, dict) else "",
    }


def _load_settlement_validation_pipeline_status() -> Optional[Dict[str, object]]:
    if not _env_bool("PM_SMS_DAILY_INCLUDE_SETTLEMENT_PIPELINE_STATUS", True):
        return None
    try:
        path = DATA_DIR / "reports" / "settlement_validation_pipeline_latest.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from scripts import settlement_validation_pipeline

            payload = settlement_validation_pipeline.build_report(
                DB_PATH,
                since_days=42,
                market_scope="highest_temperature",
                fetch_preview=False,
                simulate_after_actual_apply=True,
            )
    except Exception as exc:
        return {"ok": False, "error": f"pipeline_error:{type(exc).__name__}"}
    if not isinstance(payload, dict):
        return {}
    repair = payload.get("settlement_actual_repair") or {}
    apply = payload.get("settlement_actual_apply_dry_run") or {}
    labels = payload.get("settlement_label_dry_run") or {}
    simulation = payload.get("after_actual_apply_simulation") or {}
    flow_commands = payload.get("flow_commands") or {}
    post_validation = payload.get("post_label_apply_validation") or {}
    write_safety = payload.get("write_safety") or {}
    if not isinstance(repair, dict):
        repair = {}
    if not isinstance(apply, dict):
        apply = {}
    if not isinstance(labels, dict):
        labels = {}
    if not isinstance(simulation, dict):
        simulation = {}
    if not isinstance(flow_commands, dict):
        flow_commands = {}
    if not isinstance(post_validation, dict):
        post_validation = {}
    if not isinstance(write_safety, dict):
        write_safety = {}
    post_commands = post_validation.get("commands") or flow_commands.get("after_labels_applied") or {}
    if not isinstance(post_commands, dict):
        post_commands = {}
    return {
        "ok": bool(payload.get("ok")),
        "read_only": bool(payload.get("read_only")),
        "next_action": str(payload.get("next_action") or ""),
        "actual_missing": int(repair.get("missing_count") or 0),
        "actual_ready": int(apply.get("ready_count") or 0),
        "labels_planned": int(labels.get("planned_labels") or 0),
        "sim_labels_after_actual_apply": int(
            simulation.get("label_planned_after_actual_apply") or 0
        ),
        "sim_actual_inserted": int(simulation.get("actual_inserted_count") or 0),
        "simulation_db_mode": str(simulation.get("simulation_db_mode") or ""),
        "pre_apply_backup_required": bool(
            write_safety.get("pre_apply_backup_required", True)
        ),
        "backup_bypass_effect": str(
            write_safety.get("bypass_effect") or "confirmed_apply_blocked"
        ),
        "confirm_command": str(
            apply.get("flow_confirm_command")
            or flow_commands.get("apply_actuals")
            or apply.get("confirm_command")
            or ""
        ),
        "double_confirm_command": str(
            apply.get("flow_confirm_and_label_apply_command")
            or flow_commands.get("apply_actuals_and_labels")
            or ""
        ),
        "double_confirm_validate_command": str(
            apply.get("flow_confirm_label_apply_and_validate_command")
            or flow_commands.get("apply_actuals_labels_and_validate")
            or ""
        ),
        "post_label_rolling_command": str(post_commands.get("rolling_validation") or ""),
        "post_label_objective_command": str(post_commands.get("objective_audit") or ""),
    }


def _load_settlement_closed_loop_status() -> Optional[Dict[str, object]]:
    if not _env_bool("PM_SMS_DAILY_INCLUDE_SETTLEMENT_CLOSED_LOOP_STATUS", True):
        return None
    try:
        path = DATA_DIR / "reports" / "settlement_closed_loop_audit_latest.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from scripts import settlement_closed_loop_audit

            payload = settlement_closed_loop_audit.build_report(
                DB_PATH,
                since_days=42,
                market_scope="highest_temperature",
            )
    except Exception as exc:
        return {"ok": False, "error": f"closed_loop_error:{type(exc).__name__}"}
    if not isinstance(payload, dict):
        return {}
    return {
        "ok": bool(payload.get("ok")),
        "phase": str(payload.get("phase") or ""),
        "actual_ready": int(payload.get("actual_ready") or 0),
        "labels_planned": int(payload.get("labels_planned") or 0),
        "sim_labels_after_actual_apply": int(payload.get("sim_labels_after_actual_apply") or 0),
        "sim_label_apply_inserted": int(payload.get("sim_label_apply_inserted") or 0),
        "sim_label_apply_closed_loop_ok": bool(
            payload.get("sim_label_apply_closed_loop_ok")
        ),
        "actual_review_rows": int(
            ((payload.get("actual_review_packet") or {}).get("would_insert_event_run_rows"))
            or 0
        )
        if isinstance(payload.get("actual_review_packet"), dict)
        else 0,
        "blockers": list(payload.get("blockers") or [])[:4],
        "next_actions": list(payload.get("next_actions") or [])[:4],
    }


def _load_settlement_confirmation_packet_status() -> Optional[Dict[str, object]]:
    if not _env_bool("PM_SMS_DAILY_INCLUDE_SETTLEMENT_CONFIRMATION_PACKET", True):
        return None
    try:
        path = DATA_DIR / "reports" / "settlement_confirmation_packet_latest.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"settlement_confirm_error:{type(exc).__name__}"}
    if not isinstance(payload, dict):
        return {}
    sim = (
        payload.get("after_actual_apply_simulation")
        if isinstance(payload.get("after_actual_apply_simulation"), dict)
        else {}
    )
    return {
        "ok": bool(payload.get("ok")),
        "actual_ready": int(payload.get("actual_ready") or 0),
        "would_insert_event_run_rows": int(
            payload.get("would_insert_event_run_rows") or 0
        ),
        "sim_labels_after_actual_apply": int(
            sim.get("label_planned_after_actual_apply") or 0
        ),
        "sim_label_apply_inserted": int(sim.get("label_apply_inserted_count") or 0),
        "sim_label_apply_closed_loop_ok": bool(sim.get("label_apply_closed_loop_ok")),
        "actual_items": list(payload.get("actual_items") or [])[:3],
        "blockers": list(payload.get("blockers") or [])[:4],
        "next_action": str(payload.get("next_action") or ""),
        "report": str(DATA_DIR / "reports" / "settlement_confirmation_packet_latest.md"),
    }


def _load_recommended_pilot_wakeup_status() -> Optional[Dict[str, object]]:
    if not _env_bool("PM_SMS_DAILY_INCLUDE_RECOMMENDED_PILOT_WAKEUP", True):
        return None
    path = DATA_DIR / "reports" / "recommended_pilot_settlement_wakeup_latest.json"
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from scripts import recommended_pilot_settlement_wakeup

            payload = recommended_pilot_settlement_wakeup.build_report(
                DB_PATH,
                since_days=42,
                market_scope="highest_temperature",
                refresh=False,
            )
    except Exception as exc:
        return {"ok": False, "error": f"wakeup_error:{type(exc).__name__}"}
    if not isinstance(payload, dict):
        return {}
    return {
        "ok": bool(payload.get("ok")),
        "watched_market_count": int(payload.get("watched_market_count") or 0),
        "triggered_market_count": int(payload.get("triggered_market_count") or 0),
        "refresh_requested": bool(payload.get("refresh_requested")),
        "refresh_ran": bool(payload.get("refresh_ran")),
        "next_action": str(payload.get("next_action") or ""),
        "triggered_markets": list(payload.get("triggered_markets") or [])[:3],
        "watched_markets": list(payload.get("watched_markets") or [])[:3],
    }


def _load_recommended_pilot_gap_refresh_status() -> Optional[Dict[str, object]]:
    if not _env_bool("PM_SMS_DAILY_INCLUDE_RECOMMENDED_PILOT_GAP_REFRESH", True):
        return None
    path = DATA_DIR / "reports" / "recommended_pilot_gap_refresh_health_latest.json"
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"gap_refresh_error:{type(exc).__name__}"}
    if not isinstance(payload, dict):
        return None
    return {
        "ok": bool(payload.get("ok")),
        "queue_count": int(payload.get("queue_count") or 0),
        "priority_slug_count": int(payload.get("priority_slug_count") or 0),
        "total_priority_slug_count": int(payload.get("total_priority_slug_count") or 0),
        "blockers": list(payload.get("blockers") or [])[:4],
        "warnings": list(payload.get("warnings") or [])[:4],
        "rows": list(payload.get("rows") or [])[:4],
        "next_action": str(payload.get("next_action") or ""),
    }


def _load_recommended_pilot_gap_plan_status() -> Optional[Dict[str, object]]:
    if not _env_bool("PM_SMS_DAILY_INCLUDE_RECOMMENDED_PILOT_GAP_PLAN", True):
        return None
    path = DATA_DIR / "reports" / "recommended_pilot_gap_scan_plan_latest.json"
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"gap_plan_error:{type(exc).__name__}"}
    if not isinstance(payload, dict):
        return None
    return {
        "ok": bool(payload.get("ok")),
        "queue_count": int(payload.get("queue_count") or 0),
        "city_count": int(payload.get("city_count") or 0),
        "strategy_gap_count": int(payload.get("strategy_gap_count") or 0),
        "priority_slug_count": int(payload.get("priority_slug_count") or 0),
        "blockers": list(payload.get("blockers") or [])[:4],
        "priority_slugs": list(payload.get("priority_slugs") or [])[:4],
        "plan": list(payload.get("plan") or [])[:3],
        "next_action": str(payload.get("next_action") or ""),
    }


def _load_latest_targeted_candidate_status() -> Dict[str, object]:
    if not _env_bool("PM_SMS_DAILY_INCLUDE_TARGETED_CANDIDATE_STATUS", True):
        return {}
    path = DATA_DIR / "reports" / "strategy_objective_completion_audit_latest.json"
    dependency_paths = [
        DATA_DIR / "reports" / "paper_observation_target_package_latest.json",
        DATA_DIR / "reports" / "paper_observation_rolling_validation_latest.json",
    ]
    try:
        if not path.exists():
            return {}
        report_mtime = path.stat().st_mtime
        newest_dependency_mtime = max(
            [dep.stat().st_mtime for dep in dependency_paths if dep.exists()] or [0.0]
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        summary = payload.get("current_strategy_batch_summary")
        if not isinstance(summary, dict):
            return {}
        status = summary.get("targeted_candidate_status")
        if not isinstance(status, dict):
            return {}
        return {
            "status": str(status.get("status") or ""),
            "next_action": str(status.get("next_action") or ""),
            "candidate_target_rows": int(status.get("candidate_target_rows") or 0),
            "target_city_count": int(status.get("target_city_count") or 0),
            "target_strategy_count": int(status.get("target_strategy_count") or 0),
            "top_excluded_entry_reasons": list(status.get("top_excluded_entry_reasons") or [])[:2],
            "top_skipped_diagnostics": list(status.get("top_skipped_diagnostics") or [])[:2],
            "city_action_plan": list(status.get("city_action_plan") or [])[:3],
            "target_hunt_plan": list(status.get("target_hunt_plan") or [])[:3],
            "expansion_hunt_plan": list(status.get("expansion_hunt_plan") or [])[:3],
            "target_observation_tracking_summary": dict(
                status.get("target_observation_tracking_summary") or {}
            ),
            "expansion_observation_tracking_summary": dict(
                status.get("expansion_observation_tracking_summary") or {}
            ),
            "recommended_pilot_observation_tracking_by_city": list(
                status.get("recommended_pilot_observation_tracking_by_city") or []
            )[:3],
            "recommended_pilot_observation_validation": dict(
                status.get("recommended_pilot_observation_validation") or {}
            ),
            "pilot_city_recommendation": dict(status.get("pilot_city_recommendation") or {}),
            "recommended_pilot_city_diagnostics": dict(
                status.get("recommended_pilot_city_diagnostics") or {}
            ),
            "recommended_pilot_settlement_readiness": dict(
                status.get("recommended_pilot_settlement_readiness") or {}
            ),
            "generated_at": str(payload.get("generated_at") or ""),
            "cache_stale": newest_dependency_mtime > report_mtime + 1.0,
        }
    except Exception:
        return {}


def _load_strategy_batch_status(report_date_local: date) -> Optional[Dict[str, object]]:
    if not _env_bool("PM_SMS_DAILY_INCLUDE_STRATEGY_BATCH_STATUS", True):
        return None
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts import small_capital_validation_review, weather_strategy_batch
    except Exception as exc:
        return {"ok": False, "error": f"import_error:{type(exc).__name__}"}

    market_scope = str(
        __import__("os").environ.get("PM_AUTO_TRADE_ENTRY_MARKET_SCOPE", "highest_temperature")
        or "highest_temperature"
    ).strip()
    max_events_raw = str(
        __import__("os").environ.get("PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_MAX_EVENTS", "3") or "3"
    ).strip()
    min_entry_edge_raw = str(
        __import__("os").environ.get("PM_WEATHER_STRATEGY_BATCH_MIN_ENTRY_EDGE", "0.10") or "0.10"
    ).strip()
    days_ahead_raw = str(
        __import__("os").environ.get("PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_DAYS_AHEAD", "2") or "2"
    ).strip()
    try:
        max_events = max(1, int(max_events_raw))
    except Exception:
        max_events = 3
    try:
        min_entry_edge = max(0.0, float(min_entry_edge_raw))
    except Exception:
        min_entry_edge = 0.10
    try:
        days_ahead = max(0, int(days_ahead_raw))
    except Exception:
        days_ahead = 2
    event_dates = [
        (report_date_local + timedelta(days=i)).isoformat()
        for i in range(days_ahead + 1)
    ]
    recommendations = _csv_env(
        "PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_RECOMMENDATIONS",
        weather_strategy_batch.DEFAULT_RECOMMENDATIONS,
    )
    focus_lanes = _csv_env(
        "PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_FOCUS_LANES",
        weather_strategy_batch.DEFAULT_FOCUS_LANES,
    )
    pilot_cities = _csv_env("PM_SMALL_CAPITAL_PILOT_CITIES", ())
    if not pilot_cities:
        pilot_cities = small_capital_validation_review._default_pilot_cities(2)[0]
    resource_focus_policy = str(
        __import__("os").environ.get(
            "PM_RESOURCE_FOCUS_POLICY",
            weather_strategy_batch.DEFAULT_RESOURCE_FOCUS_POLICY,
        )
        or weather_strategy_batch.DEFAULT_RESOURCE_FOCUS_POLICY
    ).strip()
    try:
        report = weather_strategy_batch.build_strategy_batch(
            DB_PATH,
            event_dates,
            recommendations=recommendations,
            focus_lanes=focus_lanes,
            max_events=max_events,
            market_scope=market_scope,
            min_entry_edge=min_entry_edge,
            pilot_cities=pilot_cities,
            resource_focus_policy=resource_focus_policy,
        )
    except Exception as exc:
        return {"ok": False, "error": f"report_error:{type(exc).__name__}"}
    focus = report.get("selected_resource_focus") if isinstance(report, dict) else {}
    if not isinstance(focus, dict):
        focus = {}
    settlement_quality = report.get("selected_settlement_quality") if isinstance(report, dict) else {}
    if not isinstance(settlement_quality, dict):
        settlement_quality = {}
    physical_quality = report.get("selected_physical_edge_quality") if isinstance(report, dict) else {}
    if not isinstance(physical_quality, dict):
        physical_quality = {}
    return {
        "ok": bool(report.get("ok")) if isinstance(report, dict) else False,
        "event_dates": event_dates,
        "market_scope": market_scope,
        "max_events": max_events,
        "min_entry_edge": float(report.get("min_entry_edge") or min_entry_edge) if isinstance(report, dict) else min_entry_edge,
        "resource_focus_policy": str(report.get("resource_focus_policy") or resource_focus_policy),
        "candidate_events": int(report.get("candidate_events") or 0) if isinstance(report, dict) else 0,
        "selected_events": int(report.get("selected_events") or 0) if isinstance(report, dict) else 0,
        "skipped_recommendation_rows": int(report.get("skipped_recommendation_rows") or 0) if isinstance(report, dict) else 0,
        "skipped_recommendations": dict(report.get("skipped_recommendations") or {}) if isinstance(report, dict) else {},
        "skipped_diagnostics": dict(report.get("skipped_diagnostics") or {}) if isinstance(report, dict) else {},
        "excluded_entry_rows": int(report.get("excluded_entry_rows") or 0) if isinstance(report, dict) else 0,
        "excluded_entry_reasons": dict(report.get("excluded_entry_reasons") or {}) if isinstance(report, dict) else {},
        "selected_resource_focus": focus,
        "selected_settlement_quality": settlement_quality,
        "selected_physical_edge_quality": physical_quality,
        "targeted_candidate_status": _load_latest_targeted_candidate_status(),
        "warnings": list(report.get("warnings") or [])[:4] if isinstance(report, dict) else [],
        "blockers": list(report.get("blockers") or [])[:4] if isinstance(report, dict) else [],
    }


def _csv_env(key: str, defaults) -> List[str]:
    raw = str(__import__("os").environ.get(key, "") or "").strip()
    parts = raw.split(",") if raw else list(defaults)
    out: List[str] = []
    for part in parts:
        text = str(part or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in __import__("os").environ:
            __import__("os").environ[key] = value


def _env_bool(key: str, default: bool) -> bool:
    raw = __import__("os").environ.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _send_sms(message: str, recipient: str) -> bool:
    safe = _escape_applescript(str(message or "").strip())
    if not safe or not recipient:
        return False
    script = (
        'tell application "Messages"\n'
        f'  set targetBuddy to "{recipient}"\n'
        '  set targetService to missing value\n'
        '  try\n'
        '    set targetService to first service whose service type is SMS\n'
        '  end try\n'
        '  if targetService is missing value then\n'
        '    try\n'
        '      set targetService to first service whose service type is iMessage\n'
        '    end try\n'
        '  end if\n'
        '  if targetService is missing value then\n'
        '    error "no messaging service"\n'
        '  end if\n'
        '  send "'
        + safe
        + '" to buddy targetBuddy of targetService\n'
        'end tell'
    )
    try:
        res = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        print(f"[daily_sms] osascript error: {exc}")
        return False
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        print(f"[daily_sms] sms failed: {err}")
        return False
    return True


def _default_sms_recipient() -> str:
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scan_weather_market import SMS_RECIPIENT
    except Exception:
        return ""
    return str(SMS_RECIPIENT or "").strip()


def _fmt_usdc(value: float) -> str:
    if not math.isfinite(value):
        return "$0.00"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _parse_iso_ts(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _pick_tick_mark(mid: Optional[float], bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    for value in (mid,):
        if value is None:
            continue
        try:
            val = float(value)
        except Exception:
            continue
        if math.isfinite(val):
            return val
    if bid is not None and ask is not None:
        try:
            bid_val = float(bid)
            ask_val = float(ask)
        except Exception:
            bid_val = None
            ask_val = None
        if (
            bid_val is not None
            and ask_val is not None
            and math.isfinite(bid_val)
            and math.isfinite(ask_val)
        ):
            return (bid_val + ask_val) / 2.0
    for value in (bid, ask):
        if value is None:
            continue
        try:
            val = float(value)
        except Exception:
            continue
        if math.isfinite(val):
            return val
    return None


def _slug_to_city(slug: str) -> str:
    text = str(slug or "").strip().lower()
    prefix = "highest-temperature-in-"
    marker = "-on-"
    if text.startswith(prefix) and marker in text:
        city = text[len(prefix): text.index(marker)]
    else:
        city = text
    city = city.replace("-", " ").strip()
    if not city:
        return "unknown"
    return " ".join(part.capitalize() for part in city.split())


def _load_state(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, payload: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _local_tz_name() -> str:
    raw = str(__import__("os").environ.get("PM_SMS_DAILY_SUMMARY_TZ", "Asia/Shanghai") or "").strip()
    return raw or "Asia/Shanghai"


def _local_tz():
    tz_name = _local_tz_name()
    if ZoneInfo is None:
        return timezone(timedelta(hours=8))
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _resolve_report_date_local(date_arg: str) -> date:
    if date_arg:
        return datetime.strptime(date_arg, "%Y-%m-%d").date()
    now_local = datetime.now(_local_tz())
    return (now_local - timedelta(days=1)).date()


def _day_bounds_utc(day_local: date) -> Tuple[str, str]:
    tz_local = _local_tz()
    start_local = datetime(day_local.year, day_local.month, day_local.day, tzinfo=tz_local)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
        end_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
    )


@dataclass
class DailyStats:
    buy_fill_count: int = 0
    sell_fill_count: int = 0
    buy_manual_fill_count: int = 0
    sell_manual_fill_count: int = 0
    entry_manual_count: int = 0
    entry_order_submit_count: int = 0
    exit_order_submit_count: int = 0
    entry_order_bucket_count: int = 0
    exit_order_bucket_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    entry_lcb_count: int = 0
    entry_yes_top_count: int = 0
    entry_no_near_count: int = 0
    entry_no_far_count: int = 0
    entry_other_auto_count: int = 0
    open_positions_count: int = 0
    buy_notional: float = 0.0
    sell_notional: float = 0.0
    realized_pnl_total: float = 0.0
    realized_roi_day: Optional[float] = None
    realized_cost_day: float = 0.0
    report_cost_basis_total: float = 0.0
    unrealized_pnl_total: float = 0.0
    net_pnl_total: float = 0.0
    net_roi_total: Optional[float] = None
    lifetime_buy_notional: float = 0.0
    mark_missing_open_count: int = 0
    pnl_scope_event_count: int = 0
    event_counter: Counter = field(default_factory=Counter)


def _load_daily_stats(
    conn: sqlite3.Connection,
    start_utc: str,
    end_utc: str,
    *,
    valuation_cutoff_utc: Optional[str] = None,
    include_live_positions: bool = False,
) -> DailyStats:
    stats = DailyStats()
    cur = conn.cursor()
    window_end_utc = str(valuation_cutoff_utc or end_utc)
    entry_order_bucket_keys = set()
    exit_order_bucket_keys = set()
    manual_entry_keys = set()
    buy_bucket_keys = set()
    sell_bucket_keys = set()

    def _classify_auto_entry_strategy(strategy: str) -> str:
        strategy_l = str(strategy or "").strip().lower()
        if strategy_l in {"lcb", "main", "lcb_global", "lcb_global_main", "path_probability_follow", "hard_source_direct"}:
            return "path_probability_follow"
        return "other"

    query = """
    WITH decision_map AS (
      SELECT order_id, strategy_detail
      FROM (
        SELECT
          json_extract(details_json, '$.order_id') AS order_id,
          lower(trim(COALESCE(json_extract(details_json, '$.strategy'), ''))) AS strategy_detail,
          id,
          ROW_NUMBER() OVER (
            PARTITION BY json_extract(details_json, '$.order_id')
            ORDER BY id DESC
          ) AS rn
        FROM auto_trade_decisions
        WHERE action = 'entry'
          AND COALESCE(json_extract(details_json, '$.order_id'), '') <> ''
      ) t
      WHERE rn = 1
    )
    SELECT
      f.id,
      f.order_id,
      f.trade_id,
      f.trade_side,
      f.price,
      f.size,
      f.slug,
      f.event_date,
      f.outcome,
      f.outcome_side,
      lower(trim(COALESCE(ao.strategy, dm.strategy_detail, ''))) AS strategy
    FROM edge_trade_fills f
    LEFT JOIN auto_orders ao
      ON ao.order_id = f.order_id
    LEFT JOIN decision_map dm
      ON dm.order_id = f.order_id
    WHERE COALESCE(f.trade_time, f.created_at) >= ?
      AND COALESCE(f.trade_time, f.created_at) < ?
      AND f.slug LIKE 'highest-temperature-in-%'
    """
    rows = cur.execute(query, (start_utc, window_end_utc)).fetchall()
    for fill_id, order_id, trade_id, trade_side, price, size, slug, event_date, outcome, outcome_side, strategy in rows:
        trade_side_u = str(trade_side or "").strip().upper()
        strategy_l = str(strategy or "").strip().lower()
        slug_l = str(slug or "").strip()
        bucket_key = (
            slug_l,
            str(event_date or "").strip(),
            str(outcome or "").strip(),
            str(outcome_side or "").strip().upper(),
        )
        px = float(price) if price is not None else 0.0
        sz = float(size) if size is not None else 0.0
        notional = max(px, 0.0) * max(sz, 0.0)

        stats.event_counter[slug_l] += 1
        if trade_side_u == "BUY":
            stats.buy_fill_count += 1
            if not strategy_l:
                stats.buy_manual_fill_count += 1
                manual_key = str(order_id or "").strip() or str(trade_id or "").strip() or f"fill:{int(fill_id)}"
                manual_entry_keys.add(manual_key)
            buy_bucket_keys.add(bucket_key)
            stats.buy_notional += notional
        elif trade_side_u == "SELL":
            stats.sell_fill_count += 1
            if not strategy_l:
                stats.sell_manual_fill_count += 1
            sell_bucket_keys.add(bucket_key)
            stats.sell_notional += notional

    stats.buy_count = len(buy_bucket_keys)
    stats.sell_count = len(sell_bucket_keys)
    stats.entry_manual_count = len(manual_entry_keys)
    order_rows = cur.execute(
        """
        WITH decision_map AS (
          SELECT order_id, strategy_detail
          FROM (
            SELECT
              json_extract(details_json, '$.order_id') AS order_id,
                  lower(trim(COALESCE(json_extract(details_json, '$.strategy'), ''))) AS strategy_detail,
              id,
              ROW_NUMBER() OVER (
                PARTITION BY json_extract(details_json, '$.order_id')
                ORDER BY id DESC
              ) AS rn
            FROM auto_trade_decisions
            WHERE action = 'entry'
              AND COALESCE(json_extract(details_json, '$.order_id'), '') <> ''
          ) t
          WHERE rn = 1
        )
        SELECT
          ao.action,
          ao.slug,
          ao.event_date,
          ao.outcome,
          ao.side,
          lower(trim(COALESCE(ao.strategy, dm.strategy_detail, ''))) AS strategy
        FROM auto_orders ao
        LEFT JOIN decision_map dm
          ON dm.order_id = ao.order_id
        WHERE created_at >= ?
          AND created_at < ?
          AND ao.slug LIKE 'highest-temperature-in-%'
        """,
        (start_utc, window_end_utc),
    ).fetchall()
    for action, slug, event_date, outcome, side, strategy in order_rows:
        action_l = str(action or "").strip().lower()
        bucket_key = (
            str(slug or "").strip(),
            str(event_date or "").strip(),
            str(outcome or "").strip(),
            str(side or "").strip().upper(),
        )
        if not all(bucket_key):
            continue
        if action_l == "entry":
            stats.entry_order_submit_count += 1
            entry_order_bucket_keys.add(bucket_key)
            strategy_class = _classify_auto_entry_strategy(strategy)
            if strategy_class == "lcb":
                stats.entry_lcb_count += 1
            else:
                stats.entry_other_auto_count += 1
        elif action_l == "exit":
            stats.exit_order_submit_count += 1
            exit_order_bucket_keys.add(bucket_key)
    stats.entry_order_bucket_count = len(entry_order_bucket_keys)
    stats.exit_order_bucket_count = len(exit_order_bucket_keys)

    open_row = cur.execute(
        """
        SELECT COUNT(*)
        FROM edge_trades
        WHERE COALESCE(position_size, 0) > 1e-9
          AND slug LIKE 'highest-temperature-in-%'
        """
    ).fetchone()
    stats.open_positions_count = int(open_row[0] if open_row else 0)

    scope_events_set = {
        (str(slug or "").strip(), str(event_date or "").strip())
        for slug, event_date in cur.execute(
            """
            SELECT DISTINCT slug, event_date
            FROM edge_trade_fills
            WHERE COALESCE(trade_time, created_at) >= ?
              AND COALESCE(trade_time, created_at) < ?
              AND slug LIKE 'highest-temperature-in-%'
            """,
            (start_utc, window_end_utc),
        ).fetchall()
        if str(slug or "").strip() and str(event_date or "").strip()
    }
    if include_live_positions:
        for slug, event_date in cur.execute(
            """
            SELECT DISTINCT slug, event_date
            FROM edge_trades
            WHERE COALESCE(position_size, 0) > 1e-9
              AND slug LIKE 'highest-temperature-in-%'
            """
        ).fetchall():
            slug_key = str(slug or "").strip()
            event_date_key = str(event_date or "").strip()
            if slug_key and event_date_key:
                scope_events_set.add((slug_key, event_date_key))
    scope_events = sorted(scope_events_set)
    stats.pnl_scope_event_count = len(scope_events)

    if scope_events:
        start_dt_utc = datetime.fromisoformat(str(start_utc).replace("Z", "+00:00"))
        end_dt_utc = datetime.fromisoformat(str(window_end_utc).replace("Z", "+00:00"))
        values_sql = ",".join(["(?, ?)"] * len(scope_events))
        scope_params: List[str] = []
        for slug, event_date in scope_events:
            scope_params.extend([slug, event_date])

        fill_rows = cur.execute(
            f"""
            WITH scope_events(slug, event_date) AS (VALUES {values_sql})
            SELECT
              f.id,
              f.slug,
              f.event_date,
              f.outcome,
              f.outcome_side,
              f.trade_side,
              f.price,
              f.size,
              f.fee,
              COALESCE(f.trade_time, f.created_at) AS fill_ts
            FROM edge_trade_fills f
            JOIN scope_events s
              ON s.slug = f.slug
             AND s.event_date = f.event_date
            WHERE f.slug LIKE 'highest-temperature-in-%'
              AND COALESCE(f.trade_time, f.created_at) < ?
            ORDER BY datetime(COALESCE(f.trade_time, f.created_at)) ASC, f.id ASC
            """,
            (*scope_params, end_utc),
        ).fetchall()

        fills_by_contract: Dict[Tuple[str, str, str, str], List[Tuple[str, float, float, float, str]]] = {}
        for _, slug, event_date, outcome, outcome_side, trade_side, price, size, fee, fill_ts in fill_rows:
            trade_side_u = str(trade_side or "").strip().upper()
            if trade_side_u not in {"BUY", "SELL"}:
                continue
            px = float(price) if price is not None else None
            qty = float(size) if size is not None else None
            fee_val = float(fee) if fee is not None else 0.0
            if (
                px is None
                or qty is None
                or not math.isfinite(px)
                or not math.isfinite(qty)
                or px <= 0
                or qty <= 1e-9
            ):
                continue
            if not math.isfinite(fee_val) or fee_val < 0:
                fee_val = 0.0
            key = (
                str(slug or "").strip(),
                str(event_date or "").strip(),
                str(outcome or "").strip(),
                str(outcome_side or "").strip().upper(),
            )
            if all(key):
                fills_by_contract.setdefault(key, []).append(
                    (trade_side_u, float(px), float(qty), float(fee_val), str(fill_ts or ""))
                )

        live_positions_by_contract: Dict[Tuple[str, str, str, str], Tuple[float, float]] = {}
        if include_live_positions:
            live_rows = cur.execute(
                f"""
                WITH scope_events(slug, event_date) AS (VALUES {values_sql})
                SELECT t.slug, t.event_date, t.outcome, t.side, t.position_size, t.avg_price, t.entry_at, t.updated_at
                FROM edge_trades t
                JOIN scope_events s
                  ON s.slug = t.slug
                 AND s.event_date = t.event_date
                WHERE COALESCE(t.position_size, 0) > 1e-9
                  AND t.slug LIKE 'highest-temperature-in-%'
                """,
                scope_params,
            ).fetchall()
            for slug, event_date, outcome, side, position_size, avg_price, entry_at, updated_at in live_rows:
                pos_val = float(position_size) if position_size is not None else None
                avg_val = float(avg_price) if avg_price is not None else None
                if (
                    pos_val is None
                    or avg_val is None
                    or not math.isfinite(pos_val)
                    or not math.isfinite(avg_val)
                    or pos_val <= 1e-9
                    or avg_val <= 0
                ):
                    continue
                ts_text = str(entry_at or updated_at or "").strip()
                include_row = False
                if ts_text:
                    ts_dt = _parse_iso_ts(ts_text)
                    if ts_dt is not None:
                        if ts_dt.tzinfo is None:
                            ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                        ts_dt = ts_dt.astimezone(timezone.utc)
                        include_row = start_dt_utc <= ts_dt < end_dt_utc
                if not include_row and str(event_date or "").strip() == str(datetime.now(_local_tz()).date()):
                    include_row = True
                if not include_row:
                    continue
                key = (
                    str(slug or "").strip(),
                    str(event_date or "").strip(),
                    str(outcome or "").strip(),
                    str(side or "").strip().upper(),
                )
                if all(key):
                    live_positions_by_contract[key] = (float(pos_val), float(avg_val))

        realized_day = 0.0
        realized_cost_day = 0.0
        unrealized_total = 0.0
        sod_unrealized_total = 0.0
        mark_missing_open = 0
        lifetime_buy_notional = 0.0
        report_cost_basis_total = 0.0
        mark_cache: Dict[Tuple[Tuple[str, str, str, str], str], Optional[float]] = {}

        def _mark_before(contract_key: Tuple[str, str, str, str], cutoff_iso: str) -> Optional[float]:
            cache_key = (contract_key, cutoff_iso)
            cached = mark_cache.get(cache_key)
            if cache_key in mark_cache:
                return cached
            row = cur.execute(
                """
                SELECT mid, best_bid, best_ask
                FROM event_market_ticks
                WHERE slug = ?
                  AND event_date = ?
                  AND outcome = ?
                  AND side = ?
                  AND datetime(created_at) <= datetime(?)
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT 1
                """,
                (*contract_key, cutoff_iso),
            ).fetchone()
            mark_val = _pick_tick_mark(*(row or (None, None, None)))
            mark_cache[cache_key] = mark_val
            return mark_val

        for contract_key, contract_fills in fills_by_contract.items():
            pos = 0.0
            avg = 0.0
            sod_pos = 0.0
            sod_avg = 0.0
            day_buy_cost = 0.0
            for trade_side_u, px, qty, fee_val, fill_ts in contract_fills:
                fill_dt = _parse_iso_ts(fill_ts)
                if fill_dt is None:
                    continue
                if fill_dt.tzinfo is None:
                    fill_dt = fill_dt.replace(tzinfo=timezone.utc)
                fill_dt = fill_dt.astimezone(timezone.utc)
                if fill_dt >= start_dt_utc:
                    sod_pos = pos
                    sod_avg = avg
                    break
                if trade_side_u == "BUY":
                    lifetime_buy_notional += px * qty
                    new_pos = pos + qty
                    total_cost = (avg * pos) + (px * qty) + fee_val
                    pos = new_pos
                    avg = total_cost / new_pos if new_pos > 1e-9 else 0.0
                else:
                    if pos <= 1e-9:
                        continue
                    close_qty = min(qty, pos)
                    pos -= close_qty
                    if pos <= 1e-9:
                        pos = 0.0
                        avg = 0.0
            else:
                sod_pos = pos
                sod_avg = avg

            pos = sod_pos
            avg = sod_avg
            if sod_pos > 1e-9:
                sod_mark = _mark_before(contract_key, start_utc)
                if sod_mark is None:
                    sod_mark = sod_avg
                sod_unrealized_total += (float(sod_mark) - sod_avg) * sod_pos
                report_cost_basis_total += sod_avg * sod_pos

            for trade_side_u, px, qty, fee_val, fill_ts in contract_fills:
                fill_dt = _parse_iso_ts(fill_ts)
                if fill_dt is None:
                    continue
                if fill_dt.tzinfo is None:
                    fill_dt = fill_dt.replace(tzinfo=timezone.utc)
                fill_dt = fill_dt.astimezone(timezone.utc)
                if fill_dt < start_dt_utc or fill_dt >= end_dt_utc:
                    continue
                if trade_side_u == "BUY":
                    day_buy_cost += (px * qty) + fee_val
                    new_pos = pos + qty
                    total_cost = (avg * pos) + (px * qty) + fee_val
                    pos = new_pos
                    avg = total_cost / new_pos if new_pos > 1e-9 else 0.0
                else:
                    if pos <= 1e-9:
                        continue
                    close_qty = min(qty, pos)
                    fee_used = fee_val * (close_qty / qty) if qty > 1e-9 else 0.0
                    pnl_piece = (px - avg) * close_qty - fee_used
                    realized_day += pnl_piece
                    realized_cost_day += avg * close_qty
                    pos -= close_qty
                    if pos <= 1e-9:
                        pos = 0.0
                        avg = 0.0
            if pos > 1e-9:
                mark_val = _mark_before(contract_key, end_utc)
                if mark_val is None:
                    mark_missing_open += 1
                    mark_val = avg
                unrealized_total += (float(mark_val) - avg) * pos
            report_cost_basis_total += day_buy_cost

        for contract_key, (pos_val, avg_val) in live_positions_by_contract.items():
            if contract_key in fills_by_contract:
                continue
            mark_val = _mark_before(contract_key, window_end_utc)
            if mark_val is None:
                mark_missing_open += 1
                mark_val = avg_val
            unrealized_total += (float(mark_val) - avg_val) * pos_val
            report_cost_basis_total += avg_val * pos_val

        stats.realized_pnl_total = float(realized_day)
        stats.realized_cost_day = float(realized_cost_day)
        stats.report_cost_basis_total = float(report_cost_basis_total)
        if stats.realized_cost_day > 1e-9:
            stats.realized_roi_day = stats.realized_pnl_total / stats.realized_cost_day
        stats.unrealized_pnl_total = float(unrealized_total)
        stats.net_pnl_total = stats.realized_pnl_total + (stats.unrealized_pnl_total - float(sod_unrealized_total))
        stats.mark_missing_open_count = int(mark_missing_open)
        stats.lifetime_buy_notional = float(lifetime_buy_notional)
        if stats.report_cost_basis_total > 1e-9:
            stats.net_roi_total = stats.net_pnl_total / stats.report_cost_basis_total
    return stats


def _format_paper_validation_line(status: Optional[Dict[str, object]]) -> str:
    if not status:
        return ""
    if not bool(status.get("ok")) and status.get("blockers"):
        gap_text = ""
        gap_candidates = int(status.get("tradeability_gap_paper_candidates") or 0)
        if gap_candidates > 0:
            gap_text = f" | Gap纸候选{gap_candidates}"
        return "Paper: 阻断 " + ",".join(str(x) for x in (status.get("blockers") or [])[:3]) + gap_text
    coverage = status.get("coverage")
    coverage_text = "—"
    try:
        if coverage is not None:
            coverage_text = f"{float(coverage) * 100:.0f}%"
    except Exception:
        coverage_text = "—"
    scope_label = str(status.get("city") or "").strip()
    if not scope_label:
        scope_label = "最高温" if str(status.get("market_scope") or "") == "highest_temperature" else "全市场"
    reliability_text = ""
    try:
        brier = status.get("reliability_brier")
        ece = status.get("reliability_ece")
        if brier is not None and ece is not None:
            reliability_text = f" | Brier{float(brier):.3f}/ECE{float(ece):.3f}"
    except Exception:
        reliability_text = ""
    return (
        f"Paper: {scope_label} Bayes样本{int(status.get('bayes_samples') or 0)} "
        f"覆盖{coverage_text} | 待结算{int(status.get('settlement_missing') or 0)} "
        f"| 标签{int(status.get('planned_labels') or 0)} | 意图{int(status.get('will_order_rows') or 0)}"
        f" | Gap纸候选{int(status.get('tradeability_gap_paper_candidates') or 0)}"
        f"{reliability_text}"
    )


def _format_small_capital_validation_line(status: Optional[Dict[str, object]]) -> str:
    if not status:
        return ""
    if status.get("error"):
        return f"SmallCap: 错误 {status.get('error')}"
    cities = ",".join(str(x) for x in (status.get("cities") or [])) or "未设"
    candidate_text = ""
    obs = int(status.get("pilot_candidate_total") or 0)
    reverse_no = int(status.get("pilot_reverse_no_total") or 0)
    d0_settlement = int(status.get("pilot_d0_settlement_total") or 0)
    if obs > 0:
        candidate_text = f" | Obs{obs}/反NO{reverse_no}/D0Set{d0_settlement}"
        report_path = str(status.get("pilot_observation_report") or "").strip()
        if report_path:
            candidate_text += " | 表pilot_observation"
    tracking = (
        status.get("paper_observation_tracking_summary")
        if isinstance(status.get("paper_observation_tracking_summary"), dict)
        else {}
    )
    if tracking:
        candidate_text += (
            f" | Track结{int(tracking.get('resolved_rows') or 0)}"
            f"/待{int(tracking.get('pending_rows') or 0)}"
            f"/胜{int(tracking.get('wins') or 0)}"
        )
    rolling = (
        status.get("paper_observation_rolling_summary")
        if isinstance(status.get("paper_observation_rolling_summary"), dict)
        else {}
    )
    if rolling:
        hr = rolling.get("hit_rate")
        try:
            hr_text = f"{float(hr) * 100:.0f}%" if hr is not None else "—"
        except Exception:
            hr_text = "—"
        candidate_text += (
            f" | Roll结{int(rolling.get('resolved_rows') or 0)}"
            f"/待{int(rolling.get('pending_rows') or 0)}"
            f"/胜{int(rolling.get('wins') or 0)}"
            f"/HR{hr_text}"
        )
    target_summary = (
        status.get("paper_observation_target_summary")
        if isinstance(status.get("paper_observation_target_summary"), dict)
        else {}
    )
    strategy_targets = (
        target_summary.get("strategy_targets")
        if isinstance(target_summary.get("strategy_targets"), list)
        else []
    )
    target_parts = []
    for item in strategy_targets[:2]:
        if not isinstance(item, dict):
            continue
        strategy = str(item.get("target_strategy") or "")
        if strategy == "reverse_edge_buy_no_sell_yes":
            label = "反NO"
        elif strategy == "d0_physical_settlement_hybrid":
            label = "D0"
        elif strategy == "secondary_or_physical_edge":
            label = "次物"
        else:
            label = strategy[:8] or "策略"
        need_rows = int(item.get("needed_resolved_rows") or 0)
        need_wins = int(item.get("needed_additional_wins_for_hit_rate") or 0)
        if need_rows > 0:
            target_parts.append(f"{label}+{need_rows}")
        elif need_wins > 0:
            target_parts.append(f"{label}+{need_wins}胜")
    if target_parts:
        candidate_text += " | Target" + "/".join(target_parts)
    recommended_targets = (
        target_summary.get("recommended_pilot_city_targets")
        if isinstance(target_summary.get("recommended_pilot_city_targets"), list)
        else []
    )
    recommended_cities = [
        str(item.get("city") or "")
        for item in recommended_targets[:3]
        if isinstance(item, dict) and str(item.get("city") or "").strip()
    ]
    if recommended_cities:
        candidate_text += " | 推荐Pilot" + ",".join(recommended_cities)
    expansion_targets = (
        target_summary.get("observation_expansion_city_targets")
        if isinstance(target_summary.get("observation_expansion_city_targets"), list)
        else []
    )
    expansion_cities = [
        str(item.get("city") or "")
        for item in expansion_targets[:3]
        if isinstance(item, dict) and str(item.get("city") or "").strip()
    ]
    if expansion_cities:
        candidate_text += " | 扩展观察" + ",".join(expansion_cities)
    if not bool(status.get("ok")) and status.get("blockers"):
        return (
            "SmallCap: 阻断 "
            + ",".join(str(x) for x in (status.get("blockers") or [])[:3])
            + candidate_text
        )
    per_order = status.get("max_per_order_usdc")
    daily = status.get("max_daily_usdc")
    try:
        cap_text = f"单笔{float(per_order):.0f}/日{float(daily):.0f} USDC"
    except Exception:
        cap_text = "cap未定"
    ready_text = ""
    if obs > 0:
        ready_text = f" | ready={bool(status.get('small_capital_candidate_ready'))}"
    return (
        f"SmallCap: {cities} 次级={bool(status.get('secondary_only'))} | {cap_text}"
        f"{candidate_text}{ready_text}"
    )


def _format_scale_up_validation_line(status: Optional[Dict[str, object]]) -> str:
    if not status:
        return ""
    if status.get("error"):
        return f"ScaleUp: 错误 {status.get('error')}"
    cities = ",".join(str(x) for x in (status.get("cities") or [])) or "未设"
    rolling_text = ""
    rolling = (
        status.get("paper_observation_rolling_summary")
        if isinstance(status.get("paper_observation_rolling_summary"), dict)
        else {}
    )
    if rolling:
        hr = rolling.get("hit_rate")
        try:
            hr_text = f"{float(hr) * 100:.0f}%" if hr is not None else "—"
        except Exception:
            hr_text = "—"
        rolling_text = (
            f" | Roll结{int(rolling.get('resolved_rows') or 0)}"
            f"/待{int(rolling.get('pending_rows') or 0)}"
            f"/胜{int(rolling.get('wins') or 0)}"
            f"/HR{hr_text}"
        )
    if not bool(status.get("ok")) and status.get("blockers"):
        return (
            "ScaleUp: 阻断 "
            + ",".join(str(x) for x in (status.get("blockers") or [])[:3])
            + rolling_text
        )
    per_order = status.get("max_per_order_usdc")
    daily = status.get("max_daily_usdc")
    try:
        cap_text = f"单笔{float(per_order):.0f}/日{float(daily):.0f} USDC"
    except Exception:
        cap_text = "cap未定"
    return f"ScaleUp: {cities} 次级={bool(status.get('secondary_only'))} | {cap_text}{rolling_text}"


def _format_settlement_label_line(status: Optional[Dict[str, object]]) -> str:
    if not status:
        return ""
    if status.get("error"):
        return f"Settlement: 错误 {status.get('error')}"
    status_counts = status.get("status_counts") if isinstance(status.get("status_counts"), dict) else {}
    top_counts = sorted(
        ((str(k), int(v or 0)) for k, v in status_counts.items()),
        key=lambda item: (-item[1], item[0]),
    )[:3]
    status_text = ",".join(f"{k}:{v}" for k, v in top_counts) or "无市场"
    scope_label = str(status.get("city") or "").strip()
    if not scope_label:
        scope_label = "最高温" if str(status.get("market_scope") or "") == "highest_temperature" else "全市场"
    line = (
        f"Settlement: {scope_label} ready{int(status.get('ready_market_count') or 0)}/"
        f"{int(status.get('market_count') or 0)} | 标签{int(status.get('planned_labels') or 0)} "
        f"| {status_text}"
    )
    if int(status.get("planned_labels") or 0) > 0:
        review_command = str(status.get("review_command") or "").strip()
        apply_command = str(status.get("apply_command") or "").strip()
        if review_command:
            line += f" | 审阅: {review_command}"
        else:
            line += " | 审阅: settlement_label_eligibility_report"
        if apply_command:
            line += f" | apply: {apply_command}"
    return line


def _format_settlement_pipeline_line(status: Optional[Dict[str, object]]) -> str:
    if not status:
        return ""
    if status.get("error"):
        return f"SettlementPipe: 错误 {status.get('error')}"
    actual_missing = int(status.get("actual_missing") or 0)
    actual_ready = int(status.get("actual_ready") or 0)
    labels_planned = int(status.get("labels_planned") or 0)
    if actual_missing <= 0 and actual_ready <= 0 and labels_planned <= 0:
        return ""
    line = (
        "SettlementPipe: "
        f"actual ready{actual_ready}/{actual_missing} "
        f"| labels{labels_planned} "
        f"| read_only={bool(status.get('read_only'))} "
        f"| next={status.get('next_action') or '-'}"
    )
    sim_labels = int(status.get("sim_labels_after_actual_apply") or 0)
    if sim_labels > 0:
        line += (
            f" | sim_after_actual labels{sim_labels}"
            f"/actual{int(status.get('sim_actual_inserted') or 0)}"
        )
    confirm = str(status.get("confirm_command") or "").strip()
    if actual_ready > 0 and confirm:
        line += f" | actual_apply: {confirm}"
    if bool(status.get("pre_apply_backup_required")) and (actual_ready > 0 or labels_planned > 0):
        line += " | backup_required"
    double_confirm = str(status.get("double_confirm_command") or "").strip()
    if actual_ready > 0 and double_confirm:
        line += " | double_confirm_available"
    double_confirm_validate = str(status.get("double_confirm_validate_command") or "").strip()
    if actual_ready > 0 and double_confirm_validate:
        line += " | validate_available"
    rolling = str(status.get("post_label_rolling_command") or "").strip()
    objective = str(status.get("post_label_objective_command") or "").strip()
    if (labels_planned > 0 or sim_labels > 0) and rolling:
        line += f" | after_labels: {rolling}"
        if objective:
            line += " + objective_audit"
    return line


def _format_settlement_closed_loop_line(status: Optional[Dict[str, object]]) -> str:
    if not status:
        return ""
    if status.get("error"):
        return f"SettlementLoop: 错误 {status.get('error')}"
    actual_ready = int(status.get("actual_ready") or 0)
    labels_planned = int(status.get("labels_planned") or 0)
    sim_labels = int(status.get("sim_labels_after_actual_apply") or 0)
    phase = str(status.get("phase") or "").strip()
    if not phase and actual_ready <= 0 and labels_planned <= 0 and sim_labels <= 0:
        return ""
    line = (
        f"SettlementLoop: phase={phase or '-'} "
        f"| actual{actual_ready} labels{labels_planned} sim{sim_labels} "
        f"| ok={bool(status.get('ok'))}"
    )
    sim_inserted = int(status.get("sim_label_apply_inserted") or 0)
    if sim_inserted > 0:
        line += f" | sim_insert{sim_inserted}"
        if bool(status.get("sim_label_apply_closed_loop_ok")):
            line += "/closed"
    review_rows = int(status.get("actual_review_rows") or 0)
    if review_rows > 0:
        line += f" | review_rows{review_rows}"
    blockers = [str(item) for item in (status.get("blockers") or []) if str(item)]
    if blockers:
        line += " | 阻断 " + ",".join(blockers[:3])
    actions = [str(item) for item in (status.get("next_actions") or []) if str(item)]
    if actions:
        line += " | next " + ",".join(actions[:2])
    return line


def _format_settlement_confirmation_packet_line(
    status: Optional[Dict[str, object]],
) -> str:
    if not status:
        return ""
    if status.get("error"):
        return f"SettlementConfirm: 错误 {status.get('error')}"
    actual_ready = int(status.get("actual_ready") or 0)
    would_insert = int(status.get("would_insert_event_run_rows") or 0)
    sim_labels = int(status.get("sim_labels_after_actual_apply") or 0)
    sim_insert = int(status.get("sim_label_apply_inserted") or 0)
    if actual_ready <= 0 and would_insert <= 0 and sim_labels <= 0:
        return ""
    line = (
        f"SettlementConfirm: actual{actual_ready}/insert{would_insert} "
        f"| sim_labels{sim_labels}/insert{sim_insert} "
        f"| closed={bool(status.get('sim_label_apply_closed_loop_ok'))} "
        f"| ok={bool(status.get('ok'))}"
    )
    blockers = [str(item) for item in (status.get("blockers") or []) if str(item)]
    if blockers:
        line += " | 阻断 " + ",".join(blockers[:3])
    actual_bits = []
    for item in status.get("actual_items") or []:
        if not isinstance(item, dict):
            continue
        city = str(item.get("city") or "").strip()
        actual = item.get("actual")
        unit = str(item.get("market_unit") or "").strip()
        station = str(item.get("station_id") or "").strip()
        source_station = str(item.get("source_station_id") or "").strip()
        if city and actual is not None:
            actual_bits.append(
                f"{city}:{actual}{unit}/{station}={source_station}/"
                f"{'ok' if item.get('station_source_match') is True else 'bad'}"
            )
    if actual_bits:
        line += " | " + ";".join(actual_bits[:2])
    report = str(status.get("report") or "").strip()
    if report:
        line += " | 表settlement_confirm"
    return line


def _format_recommended_pilot_wakeup_line(status: Optional[Dict[str, object]]) -> str:
    if not status:
        return ""
    if status.get("error"):
        return f"Wakeup: 错误 {status.get('error')}"
    watched = int(status.get("watched_market_count") or 0)
    triggered = int(status.get("triggered_market_count") or 0)
    if watched <= 0 and triggered <= 0:
        return ""
    line = (
        f"Wakeup: 推荐Pilot结算 watch{watched}/trigger{triggered}"
        f" | refresh={'yes' if status.get('refresh_ran') else 'no'}"
    )
    if triggered > 0:
        parts = []
        for item in status.get("triggered_markets") or []:
            if not isinstance(item, dict):
                continue
            city = str(item.get("city") or "").strip()
            day = str(item.get("event_date") or "").strip()
            current = str(item.get("current_status") or "").strip()
            if city and day and current:
                parts.append(f"{city}:{day}->{current}")
        if parts:
            line += " | " + ";".join(parts[:2])
    else:
        watched_parts = []
        for item in status.get("watched_markets") or []:
            if not isinstance(item, dict):
                continue
            city = str(item.get("city") or "").strip()
            day = str(item.get("event_date") or "").strip()
            if city and day:
                watched_parts.append(f"{city}:{day}")
        if watched_parts:
            line += " | 等 " + ";".join(watched_parts[:2])
    return line


def _format_recommended_pilot_gap_refresh_line(
    status: Optional[Dict[str, object]],
) -> str:
    if not status:
        return ""
    if status.get("error"):
        return f"GapRefresh: 错误 {status.get('error')}"
    queue = int(status.get("queue_count") or 0)
    priority = int(status.get("priority_slug_count") or 0)
    total = int(status.get("total_priority_slug_count") or 0)
    if queue <= 0 and priority <= 0 and total <= 0:
        return ""
    line = f"GapRefresh: queue{queue} slugs{priority}/{total} ok={bool(status.get('ok'))}"
    blockers = [str(item) for item in (status.get("blockers") or []) if str(item)]
    warnings = [str(item) for item in (status.get("warnings") or []) if str(item)]
    if blockers:
        line += " | 阻断 " + ",".join(blockers[:2])
    elif warnings:
        line += " | 警告 " + ",".join(warnings[:2])
    rows = status.get("rows") if isinstance(status.get("rows"), list) else []
    targets = []
    for row in rows[:2]:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        tick = row.get("tick_age_sec")
        next_action = str(row.get("next_action") or "").strip()
        targets.append(f"{slug}:tick{tick if tick is not None else '-'}:{next_action or '-'}")
    if targets:
        line += " | " + ";".join(targets)
    next_action = str(status.get("next_action") or "").strip()
    if next_action and blockers:
        line += f" | next={next_action}"
    return line


def _format_recommended_pilot_gap_plan_line(
    status: Optional[Dict[str, object]],
) -> str:
    if not status:
        return ""
    if status.get("error"):
        return f"GapPlan: 错误 {status.get('error')}"
    queue = int(status.get("queue_count") or 0)
    cities = int(status.get("city_count") or 0)
    strategies = int(status.get("strategy_gap_count") or 0)
    slugs = int(status.get("priority_slug_count") or 0)
    if queue <= 0 and cities <= 0 and strategies <= 0 and slugs <= 0:
        return ""
    line = (
        f"GapPlan: queue{queue} city{cities} strat{strategies} "
        f"slugs{slugs} ok={bool(status.get('ok'))}"
    )
    plan = status.get("plan") if isinstance(status.get("plan"), list) else []
    targets = []
    for item in plan[:2]:
        if not isinstance(item, dict):
            continue
        city = str(item.get("city") or "").strip()
        if not city:
            continue
        need = int(item.get("needed_resolved_rows") or 0)
        resolved = int(item.get("resolved_rows") or 0)
        pending = int(item.get("pending_rows") or 0)
        gap_count = len(item.get("strategy_gaps") or [])
        targets.append(f"{city}+{need}结({resolved}/{pending},策{gap_count})")
    if targets:
        line += " | " + ";".join(targets)
    priority_slugs = [
        str(value).strip()
        for value in (status.get("priority_slugs") or [])[:2]
        if str(value or "").strip()
    ]
    if priority_slugs:
        line += " | slug " + ";".join(priority_slugs)
    blockers = [str(item) for item in (status.get("blockers") or []) if str(item)]
    if blockers:
        line += " | 阻断 " + ",".join(blockers[:2])
    return line


def _format_strategy_batch_line(status: Optional[Dict[str, object]]) -> str:
    if not status:
        return ""
    if status.get("error"):
        return f"Strategy: 错误 {status.get('error')}"
    focus = status.get("selected_resource_focus") if isinstance(status.get("selected_resource_focus"), dict) else {}
    settlement_quality = (
        status.get("selected_settlement_quality")
        if isinstance(status.get("selected_settlement_quality"), dict)
        else {}
    )
    physical_quality = (
        status.get("selected_physical_edge_quality")
        if isinstance(status.get("selected_physical_edge_quality"), dict)
        else {}
    )
    structural_share = focus.get("structural_edge_share") if isinstance(focus, dict) else None
    major_share = focus.get("major_settlement_share") if isinstance(focus, dict) else None

    def pct(value: object) -> str:
        try:
            if value is None:
                return "—"
            return f"{float(value) * 100:.0f}%"
        except Exception:
            return "—"

    policy = str(status.get("resource_focus_policy") or "warn")
    try:
        min_edge_value = float(status.get("min_entry_edge") or 0.10)
        min_edge_pct = min_edge_value * 100.0
        min_edge_text = (
            f"{min_edge_pct:.0f}%"
            if abs(min_edge_pct - round(min_edge_pct)) < 1e-9
            else f"{min_edge_pct:.1f}%"
        )
    except Exception:
        min_edge_text = "10%"
    selected = int(status.get("selected_events") or 0)
    candidates = int(status.get("candidate_events") or 0)
    excluded = int(status.get("excluded_entry_rows") or 0)
    excluded_reasons = status.get("excluded_entry_reasons") if isinstance(status.get("excluded_entry_reasons"), dict) else {}
    skipped_recommendations = (
        status.get("skipped_recommendations")
        if isinstance(status.get("skipped_recommendations"), dict)
        else {}
    )
    skipped_diagnostics = (
        status.get("skipped_diagnostics")
        if isinstance(status.get("skipped_diagnostics"), dict)
        else {}
    )
    target_met = focus.get("target_met") if isinstance(focus, dict) else None
    if not bool(status.get("ok")) and status.get("blockers"):
        prefix = "Strategy: 阻断 " + ",".join(str(x) for x in (status.get("blockers") or [])[:3])
    elif status.get("warnings"):
        prefix = "Strategy: 警告 " + ",".join(str(x) for x in (status.get("warnings") or [])[:3])
    else:
        prefix = "Strategy: 可执行" if bool(status.get("ok")) else "Strategy: 未就绪"
    target_text = "达标" if target_met is True else ("未达标" if target_met is False else "无样本")
    excluded_text = f"排除{excluded}"
    if excluded_reasons:
        top_excluded = sorted(
            ((str(k), int(v or 0)) for k, v in excluded_reasons.items()),
            key=lambda item: (-item[1], item[0]),
        )[:2]
        excluded_text += "(" + ",".join(f"{k}:{v}" for k, v in top_excluded) + ")"
    skipped_text = ""
    if skipped_recommendations:
        top_skipped = sorted(
            ((str(k), int(v or 0)) for k, v in skipped_recommendations.items()),
            key=lambda item: (-item[1], item[0]),
        )[:2]
        skipped_text = " | 非入场 " + ",".join(f"{k}:{v}" for k, v in top_skipped)
    diagnostic_text = ""
    if skipped_diagnostics:
        ranked_diagnostics = sorted(
            ((str(k), int(v or 0)) for k, v in skipped_diagnostics.items()),
            key=lambda item: (-item[1], item[0]),
        )
        top_diagnostics = ranked_diagnostics[:2]
        positive_raw_key = "physical_bayes_positive_raw_edge_tradeability_missing"
        if positive_raw_key in skipped_diagnostics and positive_raw_key not in {
            key for key, _ in top_diagnostics
        }:
            top_diagnostics = top_diagnostics[:1] + [
                (positive_raw_key, int(skipped_diagnostics.get(positive_raw_key) or 0))
            ]
        diagnostic_text = " | 分诊 " + ",".join(f"{k}:{v}" for k, v in top_diagnostics)
    targeted_status = (
        status.get("targeted_candidate_status")
        if isinstance(status.get("targeted_candidate_status"), dict)
        else {}
    )
    targeted_text = ""
    if targeted_status:
        target_label_map = {
            "target_candidates_available": "目标可评估",
            "non_target_candidates_only": "仅非目标",
            "target_candidates_blocked_by_tradeability_or_evidence": "目标被门槛挡",
            "no_target_candidate_in_selected_pilot_cities": "无目标候选",
            "no_open_observation_targets": "无目标缺口",
        }
        status_code = str(targeted_status.get("status") or "")
        target_label = target_label_map.get(status_code, status_code or "未知")
        targeted_text = (
            f" | TargetCand {target_label}"
            f" rows{int(targeted_status.get('candidate_target_rows') or 0)}"
            f"/city{int(targeted_status.get('target_city_count') or 0)}"
            f"/strat{int(targeted_status.get('target_strategy_count') or 0)}"
        )
        if bool(targeted_status.get("cache_stale")):
            targeted_text += "/缓存旧"
        reason_items = (
            targeted_status.get("top_excluded_entry_reasons")
            if isinstance(targeted_status.get("top_excluded_entry_reasons"), list)
            else []
        )
        if not reason_items:
            reason_items = (
                targeted_status.get("top_skipped_diagnostics")
                if isinstance(targeted_status.get("top_skipped_diagnostics"), list)
                else []
            )
        reason_parts = []
        for item in reason_items[:1]:
            if not isinstance(item, dict):
                continue
            reason = str(item.get("reason") or "").strip()
            if reason:
                reason_parts.append(f"{reason}:{int(item.get('count') or 0)}")
        if reason_parts:
            targeted_text += " " + ",".join(reason_parts)
        city_actions = (
            targeted_status.get("city_action_plan")
            if isinstance(targeted_status.get("city_action_plan"), list)
            else []
        )
        action_parts = []
        for item in city_actions[:2]:
            if not isinstance(item, dict):
                continue
            city = str(item.get("city") or "").strip()
            reason = str(item.get("primary_reason") or "").strip()
            action = str(item.get("recommended_action") or "").strip()
            if city and reason and action:
                action_parts.append(f"{city}:{reason}->{action}")
        if action_parts:
            targeted_text += " | 城市动作 " + ";".join(action_parts)
        hunt_plan = (
            targeted_status.get("target_hunt_plan")
            if isinstance(targeted_status.get("target_hunt_plan"), list)
            else []
        )
        hunt_parts = []
        for item in hunt_plan[:2]:
            if not isinstance(item, dict):
                continue
            city = str(item.get("city") or "").strip()
            strategy = str(item.get("target_strategy") or "").strip()
            rows_need = int(item.get("needed_resolved_rows") or 0)
            wins_need = int(item.get("needed_additional_wins_for_hit_rate") or 0)
            if not city or not strategy:
                continue
            suffix = []
            if rows_need > 0:
                suffix.append(f"+{rows_need}结")
            if wins_need > 0:
                suffix.append(f"+{wins_need}胜")
            status = str(item.get("hunt_status") or "").strip()
            status_suffix = f"[{status}]" if status else ""
            hunt_parts.append(f"{city}:{strategy}{''.join(suffix)}{status_suffix}")
        if hunt_parts:
            targeted_text += " | 目标猎取 " + ";".join(hunt_parts)
        expansion_plan = (
            targeted_status.get("expansion_hunt_plan")
            if isinstance(targeted_status.get("expansion_hunt_plan"), list)
            else []
        )
        expansion_parts = []
        for item in expansion_plan[:2]:
            if not isinstance(item, dict):
                continue
            city = str(item.get("city") or "").strip()
            rows = int(item.get("paper_observation_rows") or 0)
            status = str(item.get("hunt_status") or "").strip()
            if city and rows > 0:
                expansion_parts.append(f"{city}:paper{rows}[{status}]")
        if expansion_parts:
            targeted_text += " | 扩展观察 " + ";".join(expansion_parts)
        target_tracking = (
            targeted_status.get("target_observation_tracking_summary")
            if isinstance(targeted_status.get("target_observation_tracking_summary"), dict)
            else {}
        )
        expansion_tracking = (
            targeted_status.get("expansion_observation_tracking_summary")
            if isinstance(targeted_status.get("expansion_observation_tracking_summary"), dict)
            else {}
        )
        target_resolved = int(target_tracking.get("resolved_rows") or 0)
        target_pending = int(target_tracking.get("pending_rows") or 0)
        expansion_resolved = int(expansion_tracking.get("resolved_rows") or 0)
        expansion_pending = int(expansion_tracking.get("pending_rows") or 0)
        if target_resolved or target_pending or expansion_resolved or expansion_pending:
            targeted_text += (
                f" | 观察结算 T{target_resolved}/{target_pending}"
                f" E{expansion_resolved}/{expansion_pending}"
            )
        recommended_tracking = (
            targeted_status.get("recommended_pilot_observation_tracking_by_city")
            if isinstance(
                targeted_status.get("recommended_pilot_observation_tracking_by_city"),
                list,
            )
            else []
        )
        recommended_tracking_parts = []
        for item in recommended_tracking[:2]:
            if not isinstance(item, dict):
                continue
            city = str(item.get("city") or "").strip()
            resolved = int(item.get("resolved_rows") or 0)
            pending = int(item.get("pending_rows") or 0)
            wins = int(item.get("wins") or 0)
            if city and (resolved or pending):
                recommended_tracking_parts.append(
                    f"{city}:结{resolved}/待{pending}/胜{wins}"
                )
        if recommended_tracking_parts:
            targeted_text += " | 推荐Pilot观察 " + ";".join(recommended_tracking_parts)
        recommended_validation = (
            targeted_status.get("recommended_pilot_observation_validation")
            if isinstance(
                targeted_status.get("recommended_pilot_observation_validation"),
                dict,
            )
            else {}
        )
        validation_parts = []
        validation_thresholds = (
            recommended_validation.get("thresholds")
            if isinstance(recommended_validation.get("thresholds"), dict)
            else {}
        )
        min_rows = int(validation_thresholds.get("min_resolved_rows") or 0)
        for item in (recommended_validation.get("by_city") or [])[:2]:
            if not isinstance(item, dict):
                continue
            city = str(item.get("city") or "").strip()
            overall = item.get("overall") if isinstance(item.get("overall"), dict) else {}
            resolved = int(overall.get("resolved_rows") or 0)
            hit_rate = overall.get("hit_rate")
            try:
                hit_text = f"{float(hit_rate) * 100:.0f}%" if hit_rate is not None else "—"
            except Exception:
                hit_text = "—"
            status_text = "pass" if item.get("ok") is True else "wait"
            if city:
                validation_parts.append(
                    f"{city}:结{resolved}/{min_rows}/HR{hit_text}/{status_text}"
                )
        if validation_parts:
            targeted_text += " | Pilot验证 " + ";".join(validation_parts)
        gap_parts = []
        for item in (recommended_validation.get("sample_gap_queue") or [])[:2]:
            if not isinstance(item, dict):
                continue
            city = str(item.get("city") or "").strip()
            needed = int(item.get("needed_resolved_rows") or 0)
            strategy_gaps = (
                item.get("strategy_gaps")
                if isinstance(item.get("strategy_gaps"), list)
                else []
            )
            strategy_texts = []
            for gap in strategy_gaps[:2]:
                if not isinstance(gap, dict):
                    continue
                strategy = str(gap.get("target_strategy") or "").strip()
                need_strategy = int(gap.get("needed_resolved_rows") or 0)
                if strategy and need_strategy > 0:
                    label = "反NO" if strategy == "reverse_edge_buy_no_sell_yes" else strategy[:6]
                    strategy_texts.append(f"{label}+{need_strategy}")
            if city and (needed > 0 or strategy_texts):
                suffix = f"+{needed}结" if needed > 0 else ""
                if strategy_texts:
                    suffix += "/" + ",".join(strategy_texts)
                gap_parts.append(f"{city}:{suffix}")
        if gap_parts:
            targeted_text += " | Pilot缺口 " + ";".join(gap_parts)
        pilot_reco = (
            targeted_status.get("pilot_city_recommendation")
            if isinstance(targeted_status.get("pilot_city_recommendation"), dict)
            else {}
        )
        reco_action = str(pilot_reco.get("action") or "").strip()
        reco_cities = [
            str(city)
            for city in pilot_reco.get("recommended_pilot_cities") or []
            if str(city).strip()
        ]
        if reco_action and reco_cities and reco_action != "keep_current_pilot":
            targeted_text += f" | Pilot建议 {reco_action}:{','.join(reco_cities[:2])}"
        reco_diag = (
            targeted_status.get("recommended_pilot_city_diagnostics")
            if isinstance(targeted_status.get("recommended_pilot_city_diagnostics"), dict)
            else {}
        )
        diag_parts = []
        for city, item in list(reco_diag.items())[:2]:
            if not isinstance(item, dict):
                continue
            rows = int(item.get("scoped_plan_rows") or 0)
            cand = int(item.get("candidate_rows") or 0)
            primary_action = str(item.get("primary_action") or "").strip()
            skipped = item.get("skipped_diagnostics") if isinstance(item.get("skipped_diagnostics"), dict) else {}
            excluded = item.get("excluded_entry_reasons") if isinstance(item.get("excluded_entry_reasons"), dict) else {}
            top_reason = ""
            if skipped:
                top_reason = sorted(skipped.items(), key=lambda kv: (-int(kv[1] or 0), str(kv[0])))[0][0]
            elif excluded:
                top_reason = sorted(excluded.items(), key=lambda kv: (-int(kv[1] or 0), str(kv[0])))[0][0]
            if rows or cand or top_reason:
                action_text = primary_action or top_reason or "-"
                diag_parts.append(f"{city}:pre{rows}/post{cand}/{action_text}")
        if diag_parts:
            targeted_text += " | Pilot诊断 " + ";".join(diag_parts)
        settlement_ready = (
            targeted_status.get("recommended_pilot_settlement_readiness")
            if isinstance(targeted_status.get("recommended_pilot_settlement_readiness"), dict)
            else {}
        )
        planned = int(settlement_ready.get("total_planned_labels") or 0)
        scanned = int(settlement_ready.get("total_decisions_scanned") or 0)
        next_settle = str(settlement_ready.get("next_action") or "").strip()
        if planned or scanned:
            targeted_text += f" | Pilot标签 {planned}/{scanned}"
            if planned > 0 and next_settle:
                targeted_text += f"[{next_settle}]"
            by_city_ready = (
                settlement_ready.get("by_city")
                if isinstance(settlement_ready.get("by_city"), dict)
                else {}
            )
            status_bits = []
            for city, item in list(by_city_ready.items())[:1]:
                if not isinstance(item, dict):
                    continue
                counts = (
                    item.get("eligibility_status_counts")
                    if isinstance(item.get("eligibility_status_counts"), dict)
                    else {}
                )
                if counts:
                    top = sorted(counts.items(), key=lambda kv: (-int(kv[1] or 0), str(kv[0])))[0]
                    status_bits.append(f"{city}:{top[0]}{int(top[1] or 0)}")
            if status_bits:
                targeted_text += "[" + ";".join(status_bits) + "]"
    line = (
        f"{prefix} | 选{selected}/{candidates} {excluded_text} | "
        f"结构{pct(structural_share)} 主要Settlement{pct(major_share)} | "
        f"70/30 {target_text} | edge≥{min_edge_text} live | {policy}"
    )
    line += skipped_text
    line += diagnostic_text
    line += targeted_text
    settlement_count = int(settlement_quality.get("selected_settlement_count") or 0)
    if settlement_count > 0:
        quality_text = (
            "达标"
            if settlement_quality.get("target_met") is True
            else ("未达标" if settlement_quality.get("target_met") is False else "无样本")
        )
        line += (
            f" | SettlementQ {quality_text} "
            f"eligible{int(settlement_quality.get('settlement_eligible_count') or 0)}/{settlement_count} "
            f"station{int(settlement_quality.get('station_match_count') or 0)}/{settlement_count}"
        )
    d0_count = int(physical_quality.get("selected_d0_count") or 0)
    if d0_count > 0:
        phys_text = (
            "达标"
            if physical_quality.get("target_met") is True
            else ("缺证据" if physical_quality.get("target_met") is False else "无样本")
        )
        line += (
            f" | D0Phys {phys_text} "
            f"{int(physical_quality.get('physical_evidence_count') or 0)}/{d0_count} "
            f"bimodal{int(physical_quality.get('bimodal_risk_count') or 0)}"
        )
    return line


def _build_message(
    report_date_local: date,
    stats: DailyStats,
    paper_status: Optional[Dict[str, object]] = None,
    small_capital_status: Optional[Dict[str, object]] = None,
    scale_up_status: Optional[Dict[str, object]] = None,
    settlement_label_status: Optional[Dict[str, object]] = None,
    recommended_pilot_wakeup_status: Optional[Dict[str, object]] = None,
    strategy_batch_status: Optional[Dict[str, object]] = None,
    settlement_pipeline_status: Optional[Dict[str, object]] = None,
    settlement_closed_loop_status: Optional[Dict[str, object]] = None,
    recommended_pilot_gap_plan_status: Optional[Dict[str, object]] = None,
    recommended_pilot_gap_refresh_status: Optional[Dict[str, object]] = None,
    settlement_confirmation_packet_status: Optional[Dict[str, object]] = None,
) -> str:
    net_roi_text = "—"
    if stats.net_roi_total is not None and math.isfinite(stats.net_roi_total):
        net_roi_text = f"{stats.net_roi_total * 100:.2f}%"
    realized_roi_day_text = "—"
    if stats.realized_roi_day is not None and math.isfinite(stats.realized_roi_day):
        realized_roi_day_text = f"{stats.realized_roi_day * 100:.2f}%"
    entry_total_count = stats.entry_order_submit_count + stats.entry_manual_count
    lines = [
        f"【交易日报 {report_date_local.isoformat()} 本地】",
        f"进入: 总{entry_total_count} | 自动{stats.entry_order_submit_count} | 手动{stats.entry_manual_count}",
        (
            "自动策略: "
            f"lcb={stats.entry_lcb_count} | 其他={stats.entry_other_auto_count}"
        ),
        (
            f"持仓: {stats.open_positions_count} | 花费: {_fmt_usdc(stats.buy_notional)} "
            f"| 卖出: {_fmt_usdc(stats.sell_notional)}"
        ),
        (
            f"PnL: {_fmt_usdc(stats.net_pnl_total)} | ROI: {net_roi_text}"
            f" | 已实现: {_fmt_usdc(stats.realized_pnl_total)}"
        ),
        f"估值: {_fmt_usdc(stats.unrealized_pnl_total)} | 已实现ROI: {realized_roi_day_text}",
    ]
    if stats.mark_missing_open_count > 0:
        lines.append(f"估值缺口: {stats.mark_missing_open_count}")
    paper_line = _format_paper_validation_line(paper_status)
    if paper_line:
        lines.append(paper_line)
    small_capital_line = _format_small_capital_validation_line(small_capital_status)
    if small_capital_line:
        lines.append(small_capital_line)
    scale_up_line = _format_scale_up_validation_line(scale_up_status)
    if scale_up_line:
        lines.append(scale_up_line)
    settlement_line = _format_settlement_label_line(settlement_label_status)
    if settlement_line:
        lines.append(settlement_line)
    settlement_pipeline_line = _format_settlement_pipeline_line(settlement_pipeline_status)
    if settlement_pipeline_line:
        lines.append(settlement_pipeline_line)
    settlement_closed_loop_line = _format_settlement_closed_loop_line(
        settlement_closed_loop_status
    )
    if settlement_closed_loop_line:
        lines.append(settlement_closed_loop_line)
    settlement_confirm_line = _format_settlement_confirmation_packet_line(
        settlement_confirmation_packet_status
    )
    if settlement_confirm_line:
        lines.append(settlement_confirm_line)
    wakeup_line = _format_recommended_pilot_wakeup_line(
        recommended_pilot_wakeup_status
    )
    if wakeup_line:
        lines.append(wakeup_line)
    gap_plan_line = _format_recommended_pilot_gap_plan_line(
        recommended_pilot_gap_plan_status
    )
    if gap_plan_line:
        lines.append(gap_plan_line)
    gap_refresh_line = _format_recommended_pilot_gap_refresh_line(
        recommended_pilot_gap_refresh_status
    )
    if gap_refresh_line:
        lines.append(gap_refresh_line)
    strategy_batch_line = _format_strategy_batch_line(strategy_batch_status)
    if strategy_batch_line:
        lines.append(strategy_batch_line)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send daily weather trading SMS summary.")
    parser.add_argument("--date", default="", help="本地时区报告日期 YYYY-MM-DD（默认：本地昨天）。")
    parser.add_argument("--recipient", default="", help="Override SMS recipient.")
    parser.add_argument("--dry-run", action="store_true", help="Print message only.")
    parser.add_argument("--force", action="store_true", help="Send even if already sent for this ET date.")
    args = parser.parse_args()

    _load_env(ENV_PATH)
    sms_enabled = _env_bool("PM_SMS_ENABLED", True)
    daily_enabled = _env_bool("PM_SMS_DAILY_SUMMARY_ENABLED", True)
    recipient = str(
        args.recipient
        or __import__("os").environ.get("PM_SMS_RECIPIENT", "")
        or _default_sms_recipient()
    ).strip()
    report_date_local = _resolve_report_date_local(str(args.date or "").strip())
    report_date_key = report_date_local.isoformat()

    if not args.dry_run:
        if not daily_enabled:
            print("[daily_sms] disabled by PM_SMS_DAILY_SUMMARY_ENABLED=0")
            return 0
        if not sms_enabled:
            print("[daily_sms] PM_SMS_ENABLED=0, skip send")
            return 0
        if not recipient:
            print("[daily_sms] missing PM_SMS_RECIPIENT")
            return 1
        state = _load_state(STATE_PATH)
        last_sent_key = str(
            state.get("last_report_date_local")
            or state.get("last_report_date_et")
            or ""
        )
        if (not args.force) and last_sent_key == report_date_key:
            print(f"[daily_sms] already sent for {report_date_key}, skip")
            return 0

    if not DB_PATH.exists():
        print(f"[daily_sms] missing db: {DB_PATH}")
        return 1

    start_utc, end_utc = _day_bounds_utc(report_date_local)
    now_local_dt = datetime.now(_local_tz())
    include_live_positions = report_date_local == now_local_dt.date()
    valuation_cutoff_utc = end_utc
    if include_live_positions:
        valuation_cutoff_utc = min(
            end_utc,
            now_local_dt.astimezone(timezone.utc).isoformat(timespec="seconds"),
        )
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0)
    try:
        stats = _load_daily_stats(
            conn,
            start_utc,
            end_utc,
            valuation_cutoff_utc=valuation_cutoff_utc,
            include_live_positions=include_live_positions,
        )
    finally:
        conn.close()
    message = _build_message(
        report_date_local,
        stats,
        _load_paper_validation_status(),
        _load_small_capital_validation_status(),
        _load_scale_up_validation_status(),
        _load_settlement_label_status(),
        _load_recommended_pilot_wakeup_status(),
        _load_strategy_batch_status(report_date_local),
        _load_settlement_validation_pipeline_status(),
        _load_settlement_closed_loop_status(),
        _load_recommended_pilot_gap_plan_status(),
        _load_recommended_pilot_gap_refresh_status(),
        _load_settlement_confirmation_packet_status(),
    )

    if args.dry_run:
        print(message)
        return 0

    ok = _send_sms(message, recipient)
    if not ok:
        return 1

    now_local = datetime.now(_local_tz()).isoformat(timespec="seconds")
    _save_state(
        STATE_PATH,
        {
            "last_report_date_local": report_date_key,
            "last_sent_at_local": now_local,
            "last_recipient": recipient,
            "report_tz": _local_tz_name(),
        },
    )
    print(f"[daily_sms] sent report for local {report_date_key} ({_local_tz_name()}) to {recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
