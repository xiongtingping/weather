#!/usr/bin/env python3
"""Build the frozen, orderless activation for the sole probability mainline.

Read-only by default.  Apply is permitted only after the monitor/writers have
been paused and the maintenance-window no-order drill has passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_activation_contract import (  # noqa: E402
    BAYES_FORMULA_ID,
    CHECKPOINT_SCHEMA,
    COMPOSER_SCHEMA,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION as ACTIVATION_SCHEMA,
    STRONG_UPDATE_SCHEMA,
    validate_production_probability_activation,
)
from production_probability_p0_runtime import build_production_p0_inputs  # noqa: E402
from production_probability_runtime_manifest import (  # noqa: E402
    RETIREMENT_FILES,
    RUNTIME_FILES,
    build_current_retirement_receipt,
    build_current_runtime_manifest,
)
from scripts.production_transaction_b_no_order_drill import run_drill  # noqa: E402


SCHEMA_VERSION = "materialize_production_probability_activation_v1"
CONFIRMATION = "ACTIVATE_SEQUENTIAL_BAYES_PROBABILITY_MAINLINE"
REQUIRED_TABLES = (
    "production_initial_posterior_lineage",
    "strong_observation_updates",
    "posterior_checkpoints",
    "weak_forecast_evidence_snapshots",
    "weak_forecast_revision_updates",
    "event_fact_history",
    "event_fact_latest",
)
def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _table_state(db: Path) -> dict[str, bool]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return {
            table: conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone() is not None
            for table in REQUIRED_TABLES
        }


def build_activation_plan(
    *,
    db: Path,
    backup: Path,
    event_date: str,
    decision_as_of: str,
    root: Path = ROOT,
    installed_monitor_path: Path = Path("/Library/LaunchDaemons/com.weather.monitor.daemon.plist"),
) -> dict[str, Any]:
    root = root.resolve()
    runtime_manifest = build_current_runtime_manifest(root)
    runtime_files = dict(runtime_manifest["files"])
    retirement_receipt = build_current_retirement_receipt(root)
    legacy_absent = bool(retirement_receipt["legacy_probability_writer_absent"])
    production_enabled_by_deployment = bool(
        retirement_receipt["production_probability_ingress_enabled_by_deployment"]
    )
    tables = _table_state(db)
    repo_monitor_sha = hashlib.sha256(
        (root / "launchd/com.weather.monitor.daemon.plist").read_bytes()
    ).hexdigest()
    try:
        installed_monitor_sha = hashlib.sha256(installed_monitor_path.read_bytes()).hexdigest()
    except OSError:
        installed_monitor_sha = ""
    installed_monitor_matches_repo = bool(
        installed_monitor_sha and installed_monitor_sha == repo_monitor_sha
    )
    inputs = build_production_p0_inputs(event_date=event_date, decision_as_of=decision_as_of)
    no_order = run_drill(backup)
    artifacts = dict(inputs.get("runtime_artifacts") or {})
    blockers: list[str] = []
    if not all(tables.values()):
        blockers.append("production_probability_schema_incomplete")
    if not legacy_absent:
        blockers.append("legacy_probability_writer_not_retired")
    if not production_enabled_by_deployment:
        blockers.append("production_probability_ingress_not_enabled_by_deployment")
    if not installed_monitor_matches_repo:
        blockers.append("installed_monitor_plist_not_current_probability_deployment")
    if inputs.get("ok") is not True:
        blockers.extend(str(reason) for reason in (inputs.get("block_reasons") or ["production_p0_inputs_blocked"]))
    if no_order.get("ok") is not True:
        blockers.append("production_transaction_b_no_order_drill_failed")
    activation = {
        "schema": ACTIVATION_SCHEMA,
        "state": "production_enabled",
        "production_transaction_b_authorized": True,
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "no_order_validation_accepted": True,
        "cutover_approval_id": "ADR-0002-sequential-bayes-production-2026-07-14",
        "activation_effective_at": str(decision_as_of),
        "prior_model_artifact_sha256": str(artifacts.get("prior_model_artifact_sha256") or ""),
        "weak_likelihood_artifact_sha256": str(artifacts.get("weak_likelihood_artifact_sha256") or ""),
        "strong_lr_artifact_sha256": str(artifacts.get("strong_lr_artifact_sha256") or ""),
        "runtime_manifest_sha256": _sha(runtime_manifest),
        "legacy_writer_retirement_receipt_sha256": _sha(retirement_receipt),
        "bayes_formula_id": BAYES_FORMULA_ID,
        "composer_schema": COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
        "places_orders": False,
        "authorizes_clob": False,
    }
    if not blockers:
        validate_production_probability_activation(activation)
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "activation": activation,
        "runtime_manifest": runtime_manifest,
        "legacy_writer_retirement_receipt": retirement_receipt,
        "table_state": tables,
        "installed_monitor_deployment": {
            "path": str(installed_monitor_path),
            "repo_sha256": repo_monitor_sha,
            "installed_sha256": installed_monitor_sha,
            "matches_repo": installed_monitor_matches_repo,
        },
        "p0_inputs": {"event_date": event_date, "runtime_artifacts": artifacts},
        "no_order_drill": no_order,
        "places_orders": False,
        "authorizes_clob": False,
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temp.write_bytes(_canonical(value) + b"\n")
    os.replace(temp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data/archive.db"))
    parser.add_argument("--backup", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--decision-as-of", required=True)
    parser.add_argument("--output", default=str(ROOT / "config/production_probability_runtime_activation.json"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    plan = build_activation_plan(
        db=Path(args.db).expanduser().resolve(),
        backup=Path(args.backup).expanduser().resolve(),
        event_date=str(args.event_date)[:10],
        decision_as_of=str(args.decision_as_of),
    )
    if args.apply:
        if (
            args.confirm != CONFIRMATION
            or not args.writer_quiescent
            or plan.get("ok") is not True
        ):
            plan["ok"] = False
            plan["state"] = "apply_blocked"
            plan.setdefault("blockers", []).append("confirmation_writer_quiescence_or_plan_required")
        else:
            _atomic_json(Path(args.output).expanduser().resolve(), plan["activation"])
            plan["state"] = "activation_written"
            plan["activation_path"] = str(Path(args.output).expanduser().resolve())
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
    return 0 if plan.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
