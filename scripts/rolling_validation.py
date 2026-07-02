#!/usr/bin/env python3
"""Read-only rolling validation for the weather auto-trade mainline."""

from __future__ import annotations

import argparse
import json
import math
import os
import plistlib
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import check_trade_readiness
from scripts import bayesian_readiness_audit
from scripts import entry_counterfactual_review
from scripts import weather_decision_core_audit


DEFAULT_DB = ROOT / "data" / "archive.db"
MONITOR_DAEMON_LABEL = os.environ.get("PM_WEATHER_MONITOR_LABEL", "com.weather.monitor.daemon").strip()
MONITOR_DAEMON_DOMAIN_LABEL = (
    MONITOR_DAEMON_LABEL if MONITOR_DAEMON_LABEL.startswith("system/") else f"system/{MONITOR_DAEMON_LABEL}"
)
MONITOR_DAEMON_NAME = MONITOR_DAEMON_DOMAIN_LABEL.rsplit("/", 1)[-1]
MONITOR_DAEMON_PLIST = Path("/Library/LaunchDaemons") / f"{MONITOR_DAEMON_NAME}.plist"
EXPECTED_NEXT_AFTER_ROLLING = "paper_trading_validation"
IGNORED_READINESS_WARNINGS = {
    "clob_preflight_skipped",
    "collateral_balance_allowance_below_min_order",
    "no_recent_entry_order_intent",
}
WATCHER_LABELS = (MONITOR_DAEMON_DOMAIN_LABEL,)


def _json_loads(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _json_dict(raw: Any) -> dict[str, Any]:
    parsed = _json_loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _rate(num: int, den: int) -> float | None:
    return (int(num) / int(den)) if int(den) else None


def _configure_read_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA query_only=ON")


def _launchd_snapshot() -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    warnings: list[str] = []
    plist_info: dict[str, Any] = {
        "path": str(MONITOR_DAEMON_PLIST),
        "exists": MONITOR_DAEMON_PLIST.exists(),
    }
    if MONITOR_DAEMON_PLIST.exists():
        try:
            with MONITOR_DAEMON_PLIST.open("rb") as handle:
                plist = plistlib.load(handle) or {}
        except Exception as exc:
            plist = {}
            warnings.append(f"monitor_plist_read_error:{type(exc).__name__}")
        program_args = plist.get("ProgramArguments") if isinstance(plist, dict) else None
        program_text = (
            " ".join(str(item) for item in program_args)
            if isinstance(program_args, list)
            else str((plist or {}).get("Program") or "")
        )
        working_directory = str((plist or {}).get("WorkingDirectory") or "").strip()
        plist_info.update(
            {
                "working_directory": working_directory,
                "program": program_text,
                "root_ok": str(ROOT) in program_text and working_directory == str(ROOT),
                "program_ok": "scripts/polymarket_monitor_daemon.sh" in program_text,
            }
        )
        if not plist_info["root_ok"]:
            warnings.append("monitor_plist_root_drift")
        if not plist_info["program_ok"]:
            warnings.append("monitor_plist_program_drift")
    else:
        warnings.append("monitor_plist_missing")
    for label in WATCHER_LABELS:
        proc = subprocess.run(
            ["launchctl", "print", label],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
        )
        if proc.returncode != 0:
            missing.append(label)
            continue
        state = ""
        pid = ""
        for raw in proc.stdout.splitlines():
            text = raw.strip()
            if text.startswith("state = "):
                state = text.split("=", 1)[1].strip()
            elif text.startswith("pid = "):
                pid = text.split("=", 1)[1].strip()
        if state != "running":
            missing.append(label)
            rows[label] = {"pid": pid, "status": state or "unknown", "label": label}
            continue
        rows[label] = {"pid": pid, "status": state, "label": label}
    return {
        "ok": not missing,
        "read_only": True,
        "missing_labels": missing,
        "warnings": warnings,
        "labels": rows,
        "plist": plist_info,
        "repair_command": (
            "sudo bash scripts/install_monitor_launchdaemon.sh && "
            f"sudo launchctl kickstart -k {MONITOR_DAEMON_DOMAIN_LABEL}"
        )
        if missing or warnings
        else "",
    }


def _latest_run_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT MAX(run_id) FROM auto_trade_decisions WHERE action = 'entry'"
    ).fetchone()
    run_id = _as_int(row[0] if row else 0)
    if run_id <= 0:
        return {"ok": False, "run_id": 0, "blockers": ["no_entry_run"]}
    rows = conn.execute(
        """
        SELECT will_order, reason_codes_json, details_json, created_at
        FROM auto_trade_decisions
        WHERE action = 'entry' AND run_id = ?
        ORDER BY id ASC
        """,
        (run_id,),
    ).fetchall()
    entry_rows = len(rows)
    hard_reason_rows = 0
    exec_edge_base_rows = 0
    missing_entry_usable_edge_rows = 0
    missing_strategy_family_rows = 0
    will_order_rows = 0
    latest_created_at = ""
    for row in rows:
        latest_created_at = str(row["created_at"] or latest_created_at)
        details = _json_dict(row["details_json"])
        codes = _json_loads(row["reason_codes_json"])
        if not isinstance(codes, list):
            codes = []
        will_order = bool(row["will_order"])
        if will_order:
            will_order_rows += 1
        if (not will_order) and codes:
            hard_reason_rows += 1
        if str(details.get("entry_usable_edge_base_source") or "").strip() == "exec_edge_entry":
            exec_edge_base_rows += 1
        if details.get("entry_usable_edge") is None and not str(
            details.get("entry_usable_edge_na_reason") or ""
        ).strip():
            missing_entry_usable_edge_rows += 1
        if not str(details.get("entry_strategy_family") or "").strip():
            missing_strategy_family_rows += 1
    blockers = []
    if entry_rows <= 0:
        blockers.append("latest_run_empty")
    if hard_reason_rows > 0:
        blockers.append("latest_run_hard_reason_rows")
    if exec_edge_base_rows > 0:
        blockers.append("latest_run_exec_edge_base_rows")
    if missing_entry_usable_edge_rows > 0:
        blockers.append("latest_run_missing_entry_usable_edge")
    if missing_strategy_family_rows > 0:
        blockers.append("latest_run_missing_strategy_family")
    return {
        "ok": not blockers,
        "run_id": run_id,
        "entry_rows": entry_rows,
        "will_order_rows": will_order_rows,
        "latest_created_at": latest_created_at,
        "hard_reason_rows": hard_reason_rows,
        "exec_edge_base_rows": exec_edge_base_rows,
        "missing_entry_usable_edge_rows": missing_entry_usable_edge_rows,
        "missing_strategy_family_rows": missing_strategy_family_rows,
        "hard_reason_rate": _rate(hard_reason_rows, entry_rows),
        "blockers": blockers,
    }


def build_rolling_validation(
    *,
    db_path: Path = DEFAULT_DB,
    since_minutes: float = 30.0,
    limit: int = 5000,
    skip_clob: bool = False,
) -> dict[str, Any]:
    since_dt = datetime.now(timezone.utc) - timedelta(minutes=max(1.0, float(since_minutes)))
    since_iso = since_dt.isoformat(timespec="seconds")
    blockers: list[str] = []
    warnings: list[str] = []

    launchd = _launchd_snapshot()
    if not launchd.get("ok"):
        blockers.append("launchd_weather_not_ready")

    latest_run: dict[str, Any]
    if not db_path.exists():
        latest_run = {"ok": False, "blockers": ["archive_db_missing"]}
        blockers.append("archive_db_missing")
    else:
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30) as conn:
                _configure_read_connection(conn)
                latest_run = _latest_run_snapshot(conn)
                if not latest_run.get("ok"):
                    blockers.extend(str(code) for code in latest_run.get("blockers") or [])
        except sqlite3.Error as exc:
            latest_run = {
                "ok": False,
                "blockers": ["archive_db_read_error"],
                "error": f"{type(exc).__name__}:{exc}",
            }
            blockers.append("archive_db_read_error")

    try:
        decision_audit = weather_decision_core_audit.run_audit(
            argparse.Namespace(
                db=str(db_path),
                since=since_iso,
                since_hours=float(since_minutes) / 60.0,
                limit=int(limit),
                scan_terms=True,
                exit_policy_only=False,
                entry_only_realizable=None,
                dry_run=None,
                feedback_snapshot=False,
            )
        )
    except sqlite3.Error as exc:
        decision_audit = {
            "ok": False,
            "violations": {"archive_db_read_error": f"{type(exc).__name__}:{exc}"},
            "latest_exit_policy_coverage": {},
        }
        blockers.append("archive_db_read_error")
    if not decision_audit.get("ok"):
        blockers.append("decision_core_audit_not_ok")

    readiness = check_trade_readiness.build_live_readiness(
        db_path=db_path,
        since=since_iso,
        since_minutes=float(since_minutes),
        limit=200,
        live_cutover=False,
        skip_clob=bool(skip_clob),
    )
    readiness_blockers = [str(code) for code in readiness.get("blockers") or []]
    blockers.extend(readiness_blockers)
    readiness_warnings = [str(code) for code in readiness.get("warnings") or []]
    unexpected_readiness_warnings = [
        code for code in readiness_warnings if code not in IGNORED_READINESS_WARNINGS
    ]
    warnings.extend(unexpected_readiness_warnings)

    bayesian_since_days = max(1, int(math.ceil(float(since_minutes) / 1440.0)))
    bayesian_city = str(os.environ.get("PM_BAYES_READINESS_CITY") or "").strip()
    bayesian_market_scope = str(os.environ.get("PM_BAYES_READINESS_MARKET_SCOPE") or "highest_temperature").strip()
    try:
        bayesian = bayesian_readiness_audit.build_report(
            db_path,
            since_days=bayesian_since_days,
            city=bayesian_city,
            market_scope=bayesian_market_scope,
        )
    except sqlite3.Error as exc:
        bayesian = {
            "ok": False,
            "city_filter": bayesian_city,
            "market_scope": bayesian_market_scope,
            "since_days": bayesian_since_days,
            "blockers": ["bayesian_readiness_db_error"],
            "warnings": [],
            "error": f"{type(exc).__name__}:{exc}",
        }
        warnings.append("bayesian_readiness_unavailable")
    else:
        warnings.extend(
            f"bayesian_readiness_blocker:{code}" for code in (bayesian.get("blockers") or [])
        )
        warnings.extend(
            f"bayesian_readiness_warning:{code}" for code in (bayesian.get("warnings") or [])
        )

    try:
        counterfactual = entry_counterfactual_review.build_report(db_path, days=7)
    except sqlite3.Error as exc:
        counterfactual = {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "actionable_summary": {"next_mainline_task": "database_health_check"},
            "current_runtime": {},
        }
        blockers.append("archive_db_read_error")
    if (
        not counterfactual.get("ok")
        and str(counterfactual.get("scope") or "").strip() == "archive_db_read_error"
    ):
        blockers.append("archive_db_read_error")
    actionable = (
        counterfactual.get("actionable_summary")
        if isinstance(counterfactual.get("actionable_summary"), Mapping)
        else {}
    )
    next_mainline_task = str(actionable.get("next_mainline_task") or "").strip()
    if next_mainline_task and next_mainline_task != "rolling_validation":
        warnings.append(f"counterfactual_next_mainline:{next_mainline_task}")

    deduped_blockers = sorted(set(blockers))
    deduped_warnings = sorted(set(warnings))
    ok = not deduped_blockers
    return {
        "ok": bool(ok),
        "read_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since_iso,
        "db": str(db_path),
        "blockers": deduped_blockers,
        "warnings": deduped_warnings,
        "next_mainline_task": EXPECTED_NEXT_AFTER_ROLLING if ok else "rolling_validation",
        "latest_run": latest_run,
        "launchd": launchd,
        "decision_core": {
            "ok": bool(decision_audit.get("ok")),
            "violations": decision_audit.get("violations") or {},
            "latest_exit_policy_coverage": decision_audit.get("latest_exit_policy_coverage") or {},
        },
        "readiness": {
            "ok": bool(readiness.get("ok")),
            "conclusion": readiness.get("conclusion"),
            "blockers": readiness.get("blockers") or [],
            "warnings": readiness_warnings,
            "mode": readiness.get("mode") or {},
        },
        "bayesian_readiness": {
            "ok": bool(bayesian.get("ok")),
            "city_filter": bayesian.get("city_filter") or bayesian_city,
            "market_scope": bayesian.get("market_scope") or bayesian_market_scope,
            "since_days": bayesian.get("since_days") or bayesian_since_days,
            "readiness": bayesian.get("readiness") or {},
            "blockers": bayesian.get("blockers") or [],
            "warnings": bayesian.get("warnings") or [],
            "settlement_label_plan": bayesian.get("settlement_label_plan") or {},
            "error": bayesian.get("error"),
        },
        "counterfactual": {
            "ok": bool(counterfactual.get("ok")),
            "next_mainline_task": next_mainline_task,
            "overall": actionable.get("overall") if isinstance(actionable.get("overall"), Mapping) else {},
            "current_runtime": counterfactual.get("current_runtime") or {},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only rolling validation for weather auto-trading.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since-minutes", type=float, default=30.0)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--skip-clob", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    payload = build_rolling_validation(
        db_path=Path(args.db),
        since_minutes=float(args.since_minutes),
        limit=int(args.limit),
        skip_clob=bool(args.skip_clob),
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
