#!/usr/bin/env python3
"""Prove offline sample/OOS/bridge code is not a production live gate."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "live_offline_evidence_isolation_audit_v1"
LIVE_PYTHON_FILES = (
    "scripts/materialize_amsc_posterior_task.py",
    "posterior_ssot_projection_runtime.py",
    "weather_fact_snapshot_writer.py",
    "target_realization_v3_runtime.py",
    "strong_posterior_three_leg_strategy.py",
    "scripts/auto_trade_watcher.py",
)
OFFLINE_MODULE_TOKENS = (
    "strict_asof_forecast_capture_readiness",
    "v2_training_data_readiness_audit",
    "weather_rebuild_readiness_audit",
    "resolution_bridge_pair_coverage",
    "joint_forecast_weak_likelihood_oos",
    "strong_observation_oos_evaluation",
)


def _imported_modules(path: Path) -> tuple[list[str], str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [], f"python_source_unreadable_or_invalid:{type(exc).__name__}"
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules, ""


def audit_live_offline_evidence_isolation(files: Iterable[Path] | None = None) -> dict:
    paths = list(files) if files is not None else [ROOT / item for item in LIVE_PYTHON_FILES]
    items: list[dict] = []
    violations: list[dict] = []
    for path in paths:
        modules, error = _imported_modules(path)
        imports = sorted(set(modules))
        matches = sorted(
            token for token in OFFLINE_MODULE_TOKENS if any(token in module for module in imports)
        )
        item = {"path": str(path), "imports": imports, "offline_gate_imports": matches, "error": error}
        items.append(item)
        if error or matches:
            violations.append(item)
    return {
        "schema": SCHEMA,
        "ok": not violations,
        "state": "isolated" if not violations else "offline_evidence_leaks_into_live_surface",
        "live_surface_count": len(paths),
        "violations": violations,
        "items": items,
        "boundary": {
            "strict_asof_samples_are_promotion_only": True,
            "oos_is_promotion_only": True,
            "resolution_bridge_samples_are_promotion_only": True,
            "live_amsc_ingest_not_blocked_by_offline_evidence": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", action="append", default=[], help="override live file set; repeatable")
    args = parser.parse_args()
    files = [Path(value).expanduser().resolve() for value in args.file] or None
    report = audit_live_offline_evidence_isolation(files)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
