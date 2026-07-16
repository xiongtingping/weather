#!/usr/bin/env python3
"""Run the successor X->S->B Transaction-B drill without touching production.

The supplied database must be a maintenance backup, never ``archive.db``.
The executable path under test uses an in-memory database; the backup is used
only as an immutable before/after sentinel for the real execution-ledger
schemas and row counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolution_bridge_activation_runtime import (  # noqa: E402
    load_active_resolution_bridge_artifact,
)
TESTS = (
    ROOT / "tests/test_resolution_bridge_calibration_runtime.py",
    ROOT / "tests/test_resolution_bridge_activation_runtime.py",
    ROOT / "tests/test_resolution_bridge_promotion_runtime.py",
    ROOT / "tests/test_posterior_market_projection_runtime.py",
    ROOT / "tests/test_reported_x_market_realization_runtime.py",
    ROOT / "tests/test_production_reported_x_market_ssot_runtime.py",
    ROOT / "tests/test_production_reported_x_market_transaction_writer.py",
)
WRITER = ROOT / "production_reported_x_market_transaction_writer.py"
SCHEMA_VERSION = "production_reported_x_market_no_order_drill_v1"
EXECUTION_TABLES = (
    "auto_orders",
    "auto_trade_decisions",
    "posterior_execution_intents",
    "decision_intents",
    "edge_trades",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _counts(path: Path) -> tuple[dict[str, int | None], list[str]]:
    counts: dict[str, int | None] = {}
    missing: list[str] = []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0] or 0)
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
        if schema_version <= 0 or page_count <= 0:
            raise RuntimeError("maintenance_backup_header_invalid")
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in EXECUTION_TABLES:
            if table in tables:
                counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            else:
                counts[table] = None
                missing.append(table)
    return counts, missing


def run_drill(
    backup: Path,
    *,
    bridge_activation: Path | None = None,
) -> dict[str, Any]:
    source = backup.expanduser().resolve()
    live = (ROOT / "data/archive.db").resolve()
    if source == live:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": ["maintenance_backup_required_live_archive_refused"],
            "places_orders": False,
        }
    if not source.is_file() or any(not test.is_file() for test in TESTS) or not WRITER.is_file():
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": ["backup_or_drill_source_missing"],
            "places_orders": False,
        }
    started = time.perf_counter()
    try:
        activation_path = (
            bridge_activation.expanduser().resolve()
            if bridge_activation is not None
            else ROOT / "config/resolution_bridge_production_activation.json"
        )
        activation, artifact = load_active_resolution_bridge_artifact(
            activation_path=activation_path,
            root=ROOT,
        )
        before, missing = _counts(source)
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *[str(test) for test in TESTS]],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        after, missing_after = _counts(source)
        deltas = {
            table: None if before[table] is None else int(after[table] or 0) - int(before[table] or 0)
            for table in EXECUTION_TABLES
        }
        source_text = WRITER.read_text(encoding="utf-8")
        forbidden = [
            token
            for token in (
                "import requests",
                "import httpx",
                "clob_order_service",
                "polymarket_order",
                "create_order",
                "post_order",
            )
            if token in source_text
        ]
        ok = (
            completed.returncode == 0
            and before == after
            and not forbidden
            and "BEGIN IMMEDIATE" in source_text
            and "places_orders\": False" in source_text
        )
        return {
            "schema": SCHEMA_VERSION,
            "ok": ok,
            "state": "accepted" if ok else "failed",
            "backup": str(source),
            "backup_unchanged": before == after,
            "execution_table_counts_before": before,
            "execution_table_counts_after": after,
            "execution_table_deltas": deltas,
            "optional_missing_execution_tables": sorted(set(missing + missing_after)),
            "pytest_returncode": completed.returncode,
            "pytest_stdout": completed.stdout.strip()[-1000:],
            "pytest_stderr": completed.stderr.strip()[-1000:],
            "writer_sha256": _sha(WRITER),
            "test_sha256": {str(test.relative_to(ROOT)): _sha(test) for test in TESTS},
            "generator_sha256": _sha(Path(__file__).resolve()),
            "bridge_activation_state": str(activation.get("state") or ""),
            "bridge_activation_sha256": str(activation.get("activation_sha256") or ""),
            "bridge_artifact_sha256": str((artifact or {}).get("artifact_sha256") or ""),
            "activation_bound": activation.get("state") == "production_enabled" and artifact is not None,
            "forbidden_execution_tokens": forbidden,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "places_orders": False,
            "clob": False,
        }
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "exception",
            "exception_type": type(exc).__name__,
            "exception_detail": str(exc)[:240],
            "places_orders": False,
            "clob": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--bridge-activation",
        type=Path,
        default=ROOT / "config/resolution_bridge_production_activation.json",
    )
    args = parser.parse_args()
    result = run_drill(args.backup, bridge_activation=args.bridge_activation)
    result["receipt_sha256"] = hashlib.sha256(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        args.output.expanduser().resolve().write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
