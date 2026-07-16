#!/usr/bin/env python3
"""Prove strict-as-of/OOS research tokens cannot enter live trade modules."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "live_trade_research_gate_isolation_audit_v1"
LIVE_TRADE_PATHS = (
    "bot/main.py",
    "scan_weather_market.py",
    "weather_strategy_runtime.py",
    "target_realization_v3_runtime.py",
    "scripts/auto_trade_watcher.py",
    "scripts/auto_trade_worker.sh",
    "scripts/clob_order_service.mjs",
    "scripts/polymarket_order.mjs",
)
RESEARCH_GATE_PATTERN = re.compile(r"strict[_-]?asof|\boos(?:_|\b)|out[_-]?of[_-]?sample", re.IGNORECASE)


def build_live_trade_research_gate_isolation_audit(*, root: Path = ROOT) -> dict[str, Any]:
    """Static fail-closed audit of direct live decision/execution source files.

    It intentionally does not scan posterior-model provenance.  A released
    posterior may have been evaluated offline; the invariant is that neither
    strict training evidence nor OOS status appears as a live order gate.
    """

    base = Path(root).resolve()
    violations: list[dict[str, Any]] = []
    checked: list[str] = []
    for relative in LIVE_TRADE_PATHS:
        path = base / relative
        if not path.is_file():
            violations.append({"path": relative, "reason": "live_trade_source_file_missing"})
            continue
        checked.append(relative)
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in RESEARCH_GATE_PATTERN.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            violations.append({"path": relative, "line": line, "token": match.group(0), "reason": "research_gate_token_in_live_trade_module"})
    return {
        "schema": SCHEMA_VERSION,
        "ok": not violations,
        "state": "isolated" if not violations else "blocked",
        "checked_live_trade_paths": checked,
        "violations": violations,
        "boundary": {
            "strict_asof_training_is_offline_only": True,
            "oos_is_offline_only": True,
            "live_runtime_may_still_require_current_ssot_risk_execution_freshness": True,
        },
        "read_only": True,
        "guards": {"writes_archive_db": False, "places_orders": False, "restarts_launchd": False},
    }


def main() -> int:
    report = build_live_trade_research_gate_isolation_audit()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
