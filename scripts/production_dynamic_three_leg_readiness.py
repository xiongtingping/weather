#!/usr/bin/env python3
"""Read-only readiness audit for the unique dynamic-three-leg execution lane."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.production_probability_v6_live_acceptance import audit as audit_probability_runtime  # noqa: E402
from scripts.production_trade_gate_zero_audit import run_audit as run_trade_gate_audit  # noqa: E402


SCHEMA_VERSION = "production_dynamic_three_leg_readiness_v1"
REQUIRED_TABLES = {
    "dynamic_three_leg_transition_plans",
    "dynamic_three_leg_risk_reservations",
    "posterior_execution_intents",
    "dynamic_three_leg_position_lots",
    "dynamic_three_leg_position_lot_events",
    "dynamic_three_leg_continuation_events",
    "dynamic_three_leg_continuation_transition_plans",
    "market_alpha_take_profit_cycles",
    "posterior_outbox_execution_contracts",
    "posterior_checkpoints",
    "event_fact_history",
    "event_fact_latest",
    "auto_trade_decisions",
}
REQUIRED_INDEXES = {
    "idx_dynamic_three_leg_position_lots_open_event",
    "idx_dynamic_three_leg_position_lot_events_lot",
    "idx_dynamic_three_leg_continuation_ready",
    "idx_dynamic_three_leg_continuation_transition_state",
    "idx_market_alpha_take_profit_cycles_ready",
}
LIVE_FLAGS = {
    "PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED",
    "PM_DYNAMIC_THREE_LEG_DECISION_RESIDENT_ENABLED",
    "PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED",
    "PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED",
    "PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED",
    "PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _resident_deployment(root: Path) -> dict[str, Any]:
    source = root / "launchd/com.weather.dynamic_three_leg_execution.plist"
    installed = Path.home() / "Library/LaunchAgents/com.weather.dynamic_three_leg_execution.plist"
    source_sha, installed_sha = _sha(source), _sha(installed)
    config: dict[str, Any] = {}
    try:
        config = dict(plistlib.loads(installed.read_bytes()))
    except (OSError, ValueError, plistlib.InvalidFileException):
        config = {}
    environment = dict(config.get("EnvironmentVariables") or {})
    domain = f"gui/{os.getuid()}"
    loaded = subprocess.run(
        ["launchctl", "print", f"{domain}/com.weather.dynamic_three_leg_execution"],
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    ).returncode == 0
    disabled_output = subprocess.run(
        ["launchctl", "print-disabled", domain],
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    ).stdout
    persistent_disabled = (
        '"com.weather.dynamic_three_leg_execution" => disabled' in disabled_output
    )
    zero_flags = {key: _text(environment.get(key)) == "0" for key in sorted(LIVE_FLAGS)}
    return {
        "source_path": str(source),
        "installed_path": str(installed),
        "source_sha256": source_sha,
        "installed_sha256": installed_sha,
        "installed_matches_source": bool(source_sha and source_sha == installed_sha),
        "plist_disabled": config.get("Disabled") is True,
        "run_at_load": config.get("RunAtLoad"),
        "persistent_disabled": persistent_disabled,
        "loaded": loaded,
        "live_flags_zero": zero_flags,
        "all_live_flags_zero": all(zero_flags.values()),
        "controlled_python_launcher": (
            "resolve_project_python" in (root / "scripts/resident_dynamic_three_leg_execution.sh").read_text(
                encoding="utf-8", errors="ignore"
            )
            and "exec /usr/bin/python3" not in (root / "scripts/resident_dynamic_three_leg_execution.sh").read_text(
                encoding="utf-8", errors="ignore"
            )
        ),
    }


def _activation_state(root: Path) -> dict[str, Any]:
    dynamic = _json_file(root / "config/trading/dynamic_three_leg_production_activation.json")
    alpha = _json_file(root / "config/trading/market_alpha_take_profit_production_activation.json")
    return {
        "dynamic": dynamic,
        "market_alpha": alpha,
        "default_disabled": bool(
            dynamic.get("state") == "disabled"
            and alpha.get("state") == "disabled"
            and all(dynamic.get(key) is False for key in (
                "risk_reservation_write_authorized",
                "live_decision_write_authorized",
                "live_submit_authorized",
            ))
            and all(alpha.get(key) is False for key in (
                "live_decision_write_authorized", "live_submit_authorized",
            ))
        ),
    }


def _db_state(db: Path) -> dict[str, Any]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        counts: dict[str, Any] = {}
        for table in sorted(REQUIRED_TABLES & tables):
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        unresolved = 0
        if "posterior_execution_intents" in tables:
            unresolved = int(conn.execute(
                """SELECT COUNT(*) FROM posterior_execution_intents
                   WHERE state IN ('prepared','submitting','gateway_accepted','reconcile_required','partially_filled')"""
            ).fetchone()[0])
        t1_t5_rows = []
        if {"posterior_execution_intents", "posterior_outbox_execution_contracts"} <= tables:
            for row in conn.execute(
                """SELECT i.order_intent_id,i.posterior_run_id,i.order_action,i.reason_family,
                          x.first_actionable_seen_at,i.exchange_post_started_at,
                          ROUND((julianday(i.exchange_post_started_at)-julianday(x.first_actionable_seen_at))*86400000.0,3)
                     FROM posterior_execution_intents i
                     JOIN posterior_outbox_execution_contracts x
                       ON x.posterior_run_id=i.posterior_run_id
                    WHERE i.exchange_post_started_at IS NOT NULL
                    ORDER BY i.exchange_post_started_at DESC LIMIT 100"""
            ):
                t1_t5_rows.append({
                    "order_intent_id": _text(row[0]),
                    "posterior_run_id": _text(row[1]),
                    "order_action": _text(row[2]),
                    "reason_family": _text(row[3]),
                    "first_actionable_seen_at": _text(row[4]),
                    "exchange_post_started_at": _text(row[5]),
                    "t1_to_t5_ms": float(row[6]) if row[6] is not None else None,
                })
        latest_projection_available = False
        latest_projection_na_reason = "latest_public_fact_unavailable"
        latest_probability_event_date = ""
        if "posterior_checkpoints" in tables:
            latest_date = conn.execute(
                """SELECT substr(target_identity,-10)
                     FROM posterior_checkpoints
                    WHERE checkpoint_probability_schema='posterior_checkpoint_v4_physical_state_bayes_v6'
                      AND state='committed'
                      AND target_identity LIKE 'physical_weather_day_v1:ZUUU:%'
                    ORDER BY committed_at DESC,rowid DESC LIMIT 1"""
            ).fetchone()
            latest_probability_event_date = _text(latest_date[0] if latest_date else "")[:10]
        if {"event_fact_history", "event_fact_latest"} <= tables:
            latest = conn.execute(
                """SELECT h.fact_json FROM event_fact_latest l
                   JOIN event_fact_history h ON h.id=l.fact_id
                   WHERE h.composer_version='physical_state_bayes_v6_production'
                   ORDER BY h.id DESC LIMIT 1"""
            ).fetchone()
            try:
                fact = json.loads(_text(latest[0]) if latest else "{}")
            except ValueError:
                fact = {}
            public = fact.get("posterior_ssot_fact") if isinstance(fact, Mapping) else {}
            public = public if isinstance(public, Mapping) else {}
            probabilities = public.get("market_bucket_probabilities")
            latest_projection_available = isinstance(probabilities, Mapping) and bool(probabilities)
            latest_projection_na_reason = _text(
                public.get("market_projection_na_reason")
                or public.get("market_bucket_probabilities_na_reason")
            ) or ("" if latest_projection_available else "market_projection_unavailable")
    passed_samples = [
        row for row in t1_t5_rows
        if row["t1_to_t5_ms"] is not None and 0 <= row["t1_to_t5_ms"] <= 3000
    ]
    return {
        "missing_tables": sorted(REQUIRED_TABLES - tables),
        "missing_indexes": sorted(REQUIRED_INDEXES - indexes),
        "row_counts": counts,
        "unresolved_intent_count": unresolved,
        "gateway_attested_t1_to_t5": {
            "sample_count": len(t1_t5_rows),
            "passed_sample_count": len(passed_samples),
            "threshold_ms": 3000,
            "samples": t1_t5_rows,
        },
        "market_projection": {
            "available": latest_projection_available,
            "na_reason": latest_projection_na_reason,
            "blocks_physical_probability_live": False,
            "blocks_market_bucket_execution": not latest_projection_available,
        },
        "latest_probability_event_date": latest_probability_event_date,
    }


def build_readiness(db_path: Path | str, *, root: Path | str = ROOT) -> dict[str, Any]:
    db, root_path = Path(db_path).expanduser().resolve(), Path(root).expanduser().resolve()
    blockers: list[str] = []
    live_blockers: list[str] = []
    try:
        db_state = _db_state(db)
    except sqlite3.Error as exc:
        db_state = {"missing_tables": sorted(REQUIRED_TABLES), "missing_indexes": sorted(REQUIRED_INDEXES)}
        blockers.append(f"dynamic_three_leg_archive_unreadable:{type(exc).__name__}")
    activation = _activation_state(root_path)
    deployment = _resident_deployment(root_path)
    trade_gate = run_trade_gate_audit()
    probability_event_date = _text(db_state.get("latest_probability_event_date"))[:10]
    if probability_event_date:
        probability = audit_probability_runtime(
            db,
            activation_path=root_path / "config/production_probability_runtime_activation.json",
            event_date=probability_event_date,
        )
    else:
        probability = {
            "ok": False,
            "state": "blocked",
            "blockers": ["active_reported_x_public_ssot_missing"],
        }
    blockers.extend(f"dynamic_three_leg_table_missing:{name}" for name in db_state.get("missing_tables", []))
    blockers.extend(f"dynamic_three_leg_index_missing:{name}" for name in db_state.get("missing_indexes", []))
    if not activation["default_disabled"]:
        blockers.append("dynamic_three_leg_default_off_activation_invalid")
    if not deployment["installed_matches_source"]:
        blockers.append("dynamic_three_leg_resident_deployment_hash_mismatch")
    if not deployment["plist_disabled"] or not deployment["persistent_disabled"] or deployment["loaded"]:
        blockers.append("dynamic_three_leg_resident_not_persistently_unloaded_disabled")
    if not deployment["all_live_flags_zero"]:
        blockers.append("dynamic_three_leg_resident_live_flags_not_zero")
    if not deployment["controlled_python_launcher"]:
        blockers.append("dynamic_three_leg_resident_python_runtime_uncontrolled")
    if trade_gate.get("ok") is not True:
        blockers.append("production_trade_gate_zero_audit_failed")
    if probability.get("ok") is not True:
        blockers.append("production_probability_mainline_not_ready")

    live_blockers.extend(blockers)
    if activation["default_disabled"]:
        live_blockers.append("dynamic_three_leg_live_activation_not_accepted")
    if int(db_state.get("unresolved_intent_count") or 0) > 0:
        live_blockers.append("dynamic_three_leg_unresolved_intents_require_reconcile")
    projection = db_state.get("market_projection") or {}
    if projection.get("available") is not True:
        live_blockers.append("market_bucket_projection_unavailable")
    samples = db_state.get("gateway_attested_t1_to_t5") or {}
    if int(samples.get("passed_sample_count") or 0) < 1:
        live_blockers.append("production_gateway_attested_t1_to_t5_evidence_missing")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "default_off_ready" if not blockers else "default_off_blocked",
        "db": str(db),
        "default_off_blockers": sorted(set(blockers)),
        "live_activation_ready": not live_blockers,
        "live_activation_blockers": sorted(set(live_blockers)),
        "activation": activation,
        "resident_deployment": deployment,
        "database": db_state,
        "trade_gate_zero": trade_gate,
        "probability_mainline": {
            "ok": probability.get("ok") is True,
            "state": probability.get("state"),
            "event_date": probability.get("event_date") or probability_event_date,
            "blockers": probability.get("blockers", []),
        },
        "boundaries": {
            "read_only": True,
            "places_orders": False,
            "changes_activation": False,
            "training_samples_block_live": False,
            "strict_asof_oos_blocks_live": False,
            "bridge_blocks_physical_probability_live": False,
            "bridge_blocks_market_bucket_execution": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = build_readiness(args.db)
    if args.summary:
        result = {
            "schema": result["schema"],
            "ok": result["ok"],
            "state": result["state"],
            "default_off_blockers": result["default_off_blockers"],
            "live_activation_ready": result["live_activation_ready"],
            "live_activation_blockers": result["live_activation_blockers"],
            "resident_deployment": result["resident_deployment"],
            "database": result["database"],
            "probability_mainline": result["probability_mainline"],
            "boundaries": result["boundaries"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
