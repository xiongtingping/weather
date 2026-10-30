#!/usr/bin/env python3
"""Freeze a read-only production-facts baseline for the target lane.

This receipt is deliberately narrower than a cutover packet.  It records the
target activation/database/runtime closure and verifies the pure-YES,
``production_no_live`` boundary.  It never opens the shared live database,
calls launchctl, contacts a gateway, or writes any production state.  Process
ids, wall-clock observations, and the intentionally mutable evidence-only WS
payload stay outside the deterministic ``facts_sha256`` so normal evidence
refreshes do not create false production drift.  The raw WS file hash is
still retained in the before/after observations; its existence and stable
safety contract are checked, while payload churn is allowed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.database import PROTECTED_RESIDENT_WS_MANIFEST  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_target_db_capacity,
    load_bounded_json,
    write_bounded_json,
)


SCHEMA = "bayes_basket_target_production_facts_baseline_v1"
TARGET_DB = Path("data/bayes_basket_target_ssot.db")
TARGET_ACTIVATION = Path("config/trading/bayes_basket_target_production_no_live_activation_v1.json")
PROTECTED_FILES = (
    Path("config/trading/basket_yes_v1.json"),
    Path("data/weather_basket_yes_status.json"),
    PROTECTED_RESIDENT_WS_MANIFEST,
    Path("config/trading/settlement_floor_basket_policy_v1.json"),
)
# These files are live runtime snapshots owned by protected processes.  Their
# payloads (WS quotes and heartbeat/pid fields) are expected to change between
# audit invocations.  Their stable safety contracts are hashed through
# ``_boundary_snapshot``; raw file hashes remain in the receipt for the
# within-audit protection check.
VOLATILE_PROTECTED_FILES = frozenset({
    PROTECTED_RESIDENT_WS_MANIFEST,
    Path("data/weather_basket_yes_status.json"),
})
RUNTIME_CLOSURE_FILES = (
    Path("bayes_basket_target/production_worker.py"),
    Path("bayes_basket_target/runtime.py"),
    Path("bayes_basket_target/ssot.py"),
    Path("bayes_basket_target/activation.py"),
    Path("bayes_basket_target/market_ingress.py"),
    Path("bayes_basket_target/resource_bounds.py"),
    Path("scripts/weather_basket_yes_daemon.py"),
    Path("scripts/market_ws_listener.py"),
    Path("scripts/clob_order_service.mjs"),
    Path("scripts/targetctl.sh"),
    TARGET_ACTIVATION,
)
PROCESS_MARKERS = {
    "market_ws": "scripts/market_ws_listener.py",
    "clob_order_service": "scripts/clob_order_service.mjs",
    "weather_basket_yes": "scripts/weather_basket_yes_daemon.py",
}
RETIRED_PROCESS_MARKERS = {
    "reported_trajectory_no": "scripts/reported_trajectory_no_daemon.py",
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(root: Path, relative: Path, *, include_sha: bool = True) -> dict[str, Any]:
    path = (root / relative).resolve()
    record: dict[str, Any] = {
        "path": str(relative),
        "exists": path.is_file(),
        "size_bytes": int(path.stat().st_size) if path.is_file() else None,
    }
    record["sha256"] = _sha256(path) if include_sha and path.is_file() else None
    return record


def _stable_file_record(relative: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the file facts that are safe to include in ``facts_sha256``."""

    if relative in VOLATILE_PROTECTED_FILES:
        return {
            "path": str(relative),
            "exists": bool(record.get("exists")),
            "volatile_runtime_payload": True,
        }
    return dict(record)


def _process_snapshot(markers: Mapping[str, str] | None = None) -> dict[str, Any]:
    selected_markers = PROCESS_MARKERS if markers is None else markers
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        text=True,
        capture_output=True,
        check=False,
    )
    rows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    snapshot: dict[str, Any] = {}
    for name, marker in selected_markers.items():
        matches = [line for line in rows if marker in line]
        commands = sorted({line.split(None, 1)[1] if len(line.split(None, 1)) == 2 else line for line in matches})
        pids: list[int] = []
        for line in matches:
            try:
                pids.append(int(line.split(None, 1)[0]))
            except (IndexError, TypeError, ValueError):
                continue
        snapshot[name] = {
            "present": bool(matches),
            "pid_count": len(pids),
            "pids": sorted(pids),
            "command_sha256": content_sha256(commands),
        }
    return snapshot


def _target_db_snapshot(root: Path) -> dict[str, Any]:
    path = (root / TARGET_DB).resolve()
    assert_target_db_capacity(path)
    before = _sha256(path) if path.is_file() else None
    tables: dict[str, int] = {}
    user_version: int | None = None
    if path.is_file():
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            names = [
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
            for table in names:
                tables[table] = int(conn.execute(f"SELECT COUNT(*) FROM \"{table}\"").fetchone()[0])
    after = _sha256(path) if path.is_file() else None
    return {
        "path": str(TARGET_DB),
        "exists": path.is_file(),
        "size_bytes": int(path.stat().st_size) if path.is_file() else None,
        "sha256": after,
        "stable_during_read": before == after,
        "user_version": user_version,
        "table_counts": tables,
    }


def _boundary_snapshot(root: Path) -> dict[str, Any]:
    target = load_bounded_json(root / TARGET_ACTIVATION)
    basket_yes = load_bounded_json(root / PROTECTED_FILES[0])
    status = load_bounded_json(root / PROTECTED_FILES[1])
    ws = load_bounded_json(root / PROTECTED_FILES[2])
    floor = load_bounded_json(root / PROTECTED_FILES[3])
    floor_boundary = floor.get("execution_boundary") if isinstance(floor, Mapping) else {}
    target_checks = {
        "execution_mode_production_no_live": target.get("execution_mode") == "production_no_live",
        "target_only": target.get("target_only") is True,
        "live_submit_authorized_false": target.get("live_submit_authorized") is False,
        "places_orders_false": target.get("places_orders") is False,
        "calls_order_gateway_false": target.get("calls_order_gateway") is False,
        "writes_production_db_false": target.get("writes_production_db") is False,
    }
    basket_yes_checks = {
        "protected_live_config_not_a_runtime_entry": (
            "production_no_live=true"
            in (root / "scripts/targetctl.sh").read_text(encoding="utf-8")
            and "rm -f \"$PAUSE\"" not in (
                root / "scripts/targetctl.sh"
            ).read_text(encoding="utf-8")
        ),
        "basket_yes_enabled": basket_yes.get("state") == "enabled",
        "basket_yes_only": basket_yes.get("production_strategy_allowlist") == ["basket_yes"],
        "basket_yes_production_no_live": basket_yes.get("execution_mode") == "production_no_live",
        "basket_yes_live_decision_write_false": basket_yes.get("live_decision_write_authorized") is False,
        "basket_yes_live_submit_false": basket_yes.get("live_submit_authorized") is False,
        "basket_yes_places_orders_false": basket_yes.get("places_orders") is False,
        "basket_yes_calls_gateway_false": basket_yes.get("calls_order_gateway") is False,
    }
    ws_checks = {
        "connected": ws.get("state") == "connected",
        "evidence_only": ws.get("evidence_only") is True,
        "authorizes_order_false": ws.get("authorizes_order") is False,
    }
    status_checks = {
        "production_no_live": status.get("execution_mode") == "production_no_live",
        "live_submit_authorized_false": status.get("live_submit_authorized") is False,
        "calls_order_gateway_false": status.get("calls_order_gateway") is False,
        "places_orders_false": status.get("yes_orders_allowed") is False,
    }
    floor_checks = {
        "calculation_only": floor_boundary.get("calculation_only") is True,
        "submits_orders_false": floor_boundary.get("submits_orders") is False,
        "does_not_read_weather_or_probability": floor_boundary.get("does_not_read_weather_or_probability") is True,
    }
    return {
        "target_activation": {
            "schema": target.get("schema"),
            "state": target.get("state"),
            "activation_contract_sha256": target.get("activation_contract_sha256"),
            "checks": target_checks,
        },
        "basket_yes_no_live": {
            "guard_name": "basket_yes_no_live_enabled",
            "activation_schema": basket_yes.get("schema"),
            "strategy_id": basket_yes.get("strategy_id"),
            "activation_contract_sha256": basket_yes.get("activation_contract_sha256"),
            "checks": basket_yes_checks,
        },
        "market_ws": {
            "schema": ws.get("schema"),
            "state": ws.get("state"),
            "checks": ws_checks,
        },
        "weather_basket_yes": {
            "schema": status.get("schema"),
            "execution_mode": status.get("execution_mode"),
            "checks": status_checks,
        },
        "settlement_floor": {
            "schema": floor.get("schema"),
            "state": floor.get("state"),
            "checks": floor_checks,
        },
    }


def audit(*, project_root: Path = ROOT) -> dict[str, Any]:
    root = project_root.expanduser().resolve()
    protected_before = [_file_record(root, path) for path in PROTECTED_FILES]
    runtime = [_file_record(root, path) for path in RUNTIME_CLOSURE_FILES]
    boundary = _boundary_snapshot(root)
    database = _target_db_snapshot(root)
    processes = _process_snapshot()
    retired_processes = _process_snapshot(RETIRED_PROCESS_MARKERS)
    protected_after = [_file_record(root, path) for path in PROTECTED_FILES]
    protected_unchanged = all(
        (
            before.get("sha256") == after.get("sha256")
            if path not in VOLATILE_PROTECTED_FILES
            else before.get("exists") == after.get("exists") and bool(after.get("exists"))
        )
        for path, before, after in zip(PROTECTED_FILES, protected_before, protected_after)
    )
    checks = {
        "target_activation_boundary": all(boundary["target_activation"]["checks"].values()),
        "basket_yes_no_live_boundary": all(boundary["basket_yes_no_live"]["checks"].values()),
        "market_ws_evidence_only_boundary": all(boundary["market_ws"]["checks"].values()),
        "weather_basket_yes_no_live_boundary": all(boundary["weather_basket_yes"]["checks"].values()),
        "settlement_floor_calculation_only_boundary": all(boundary["settlement_floor"]["checks"].values()),
        "target_database_exists_and_stable": database["exists"] and database["stable_during_read"],
        "runtime_closure_complete": all(item["exists"] and bool(item["sha256"]) for item in runtime),
        "protected_files_unchanged_during_audit": protected_unchanged,
        "retired_reported_trajectory_no_absent": not any(
            item["present"] for item in retired_processes.values()
        ),
    }
    deterministic = {
        "schema": SCHEMA,
        "target": {"activation": boundary["target_activation"], "database": database},
        "protected_boundary": boundary,
        "protected_file_facts_before": [
            _stable_file_record(path, record)
            for path, record in zip(PROTECTED_FILES, protected_before)
        ],
        "protected_file_facts_after": [
            _stable_file_record(path, record)
            for path, record in zip(PROTECTED_FILES, protected_after)
        ],
        "runtime_closure": runtime,
        "retired_processes": retired_processes,
        "checks": checks,
        "boundary": {
            "read_only": True,
            "reads_shared_live_database": False,
            "writes_shared_live_database": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "restarts_services": False,
            "changes_activation": False,
        },
    }
    return {
        **deterministic,
        # Keep the full before/after observations visible to operators, but
        # deliberately do not feed the mutable WS payload into facts_sha256.
        "protected_files_before": protected_before,
        "protected_files_after": protected_after,
        "captured_at_utc": _now_utc(),
        "process_snapshot": processes,
        "retired_process_snapshot": retired_processes,
        "ok": all(checks.values()),
        "facts_sha256": content_sha256(deterministic),
        "receipt_sha256": content_sha256({**deterministic, "ok": all(checks.values())}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "output/audits/bayes_basket_target_production_facts_baseline.json")
    args = parser.parse_args()
    try:
        result = audit(project_root=args.root)
        write_bounded_json(args.output, result)
    except Exception as exc:
        result = {
            "schema": SCHEMA,
            "ok": False,
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "boundary": {
                "read_only": True,
                "writes_shared_live_database": False,
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "restarts_services": False,
            },
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps({
        "schema": result["schema"],
        "ok": result["ok"],
        "facts_sha256": result["facts_sha256"],
        "receipt_sha256": result["receipt_sha256"],
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
