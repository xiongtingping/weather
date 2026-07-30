#!/usr/bin/env python3
"""V6-native market Transaction-B drill; read-only against production data."""

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

from production_probability_v6_market_context_runtime import (  # noqa: E402
    resolve_active_v6_market_context,
)
from resolution_bridge_activation_runtime import (  # noqa: E402
    load_active_resolution_bridge_artifact,
)


SCHEMA_VERSION = "production_probability_v6_market_no_order_drill_v2"
RUNTIME_FILES = {
    "writer": ROOT / "production_probability_v6_market_transaction_writer.py",
    "bundle": ROOT / "production_probability_v6_market_bundle_runtime.py",
    "branch": ROOT / "production_probability_v6_transaction_branch_runtime.py",
    "commit": ROOT / "production_probability_v6_transaction_commit_runtime.py",
}
TESTS = (
    ROOT / "tests/test_resolution_bridge_runtime.py",
    ROOT / "tests/test_resolution_bridge_calibration_runtime.py",
    ROOT / "tests/test_resolution_bridge_operational_baseline_runtime.py",
    ROOT / "tests/test_resolution_bridge_promotion_runtime.py",
    ROOT / "tests/test_resolution_bridge_activation_runtime.py",
    ROOT / "tests/test_posterior_market_projection_runtime.py",
    ROOT / "tests/test_production_probability_v6_market_bundle_runtime.py",
    ROOT / "tests/test_production_probability_v6_market_context_runtime.py",
    ROOT / "tests/test_production_probability_v6_market_inputs_runtime.py",
    ROOT / "tests/test_production_probability_v6_transaction_branch_runtime.py",
    ROOT / "tests/test_production_probability_v6_transaction_commit_runtime.py",
    ROOT / "tests/test_production_probability_v6_market_transaction_writer.py",
    ROOT / "tests/test_production_probability_v6_strong_physical_projection_runtime.py",
)
EXECUTION_TABLES = (
    "auto_orders", "auto_trade_decisions", "posterior_execution_intents",
    "decision_intents", "edge_trades",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _counts(path: Path) -> tuple[dict[str, int | None], int, int]:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True) as conn:
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0] or 0)
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0] or 0)
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            if table in tables else None
            for table in EXECUTION_TABLES
        }
    return counts, schema_version, data_version


def run_drill(
    db: Path,
    *,
    bridge_activation: Path,
    market_context_activation: Path,
    event_date: str,
) -> dict[str, Any]:
    source = db.expanduser().resolve()
    missing = [str(path) for path in (*RUNTIME_FILES.values(), *TESTS) if not path.is_file()]
    if not source.is_file() or missing:
        return {
            "schema": SCHEMA_VERSION, "ok": False, "state": "blocked",
            "blockers": ["archive_or_v6_drill_source_missing"], "missing": missing,
            "places_orders": False,
        }
    started = time.perf_counter()
    try:
        bridge, artifact = load_active_resolution_bridge_artifact(
            activation_path=bridge_activation, root=ROOT
        )
        context_result = resolve_active_v6_market_context(
            activation_path=market_context_activation,
            root=ROOT,
            event_date=event_date,
        )
        before, schema_before, data_before = _counts(source)
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *[str(test) for test in TESTS]],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        after, schema_after, data_after = _counts(source)
        runtime_text = "\n".join(path.read_text(encoding="utf-8") for path in RUNTIME_FILES.values())
        forbidden = [token for token in (
            "import requests", "import httpx", "clob_order_service", "polymarket_order",
            "create_order", "post_order",
        ) if token in runtime_text]
        archive_unchanged = before == after and schema_before == schema_after and data_before == data_after
        bridge_bound = bridge.get("state") == "production_enabled" and artifact is not None
        context = context_result.get("market_context") if isinstance(context_result.get("market_context"), dict) else {}
        context_activation = context_result.get("activation") if isinstance(context_result.get("activation"), dict) else {}
        context_bound = context_result.get("market_projection_available") is True and bool(context)
        ok = bool(
            completed.returncode == 0
            and archive_unchanged
            and not forbidden
            and "BEGIN IMMEDIATE" in RUNTIME_FILES["writer"].read_text(encoding="utf-8")
            and "conn.rollback()" in RUNTIME_FILES["writer"].read_text(encoding="utf-8")
        )
        return {
            "schema": SCHEMA_VERSION,
            "ok": ok,
            "state": "accepted" if ok else "failed",
            "archive_db": str(source),
            "archive_open_mode": "read_only_immutable",
            "archive_unchanged": archive_unchanged,
            "execution_table_counts_before": before,
            "execution_table_counts_after": after,
            "pytest_returncode": completed.returncode,
            "pytest_stdout": completed.stdout.strip()[-1000:],
            "pytest_stderr": completed.stderr.strip()[-1000:],
            "runtime_sha256": {name: _sha(path) for name, path in RUNTIME_FILES.items()},
            "test_sha256": {str(test.relative_to(ROOT)): _sha(test) for test in TESTS},
            "generator_sha256": _sha(Path(__file__).resolve()),
            "bridge_activation_sha256": str(bridge.get("activation_sha256") or ""),
            "bridge_artifact_sha256": str((artifact or {}).get("artifact_sha256") or ""),
            "market_context_activation_sha256": str(context_activation.get("activation_sha256") or ""),
            "market_context_sha256": str(context.get("market_context_sha256") or ""),
            "activation_bound": bridge_bound and context_bound,
            "forbidden_execution_tokens": forbidden,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "external_disk_required": False,
            "writes_archive_db": False,
            "places_orders": False,
            "clob": False,
        }
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return {
            "schema": SCHEMA_VERSION, "ok": False, "state": "exception",
            "exception_type": type(exc).__name__, "exception_detail": str(exc)[:240],
            "external_disk_required": False, "writes_archive_db": False,
            "places_orders": False, "clob": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--bridge-activation", type=Path, default=ROOT / "config/resolution_bridge_production_activation.json")
    parser.add_argument("--market-context-activation", type=Path, default=ROOT / "config/trading/production_probability_v6_market_context_activation.json")
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_drill(
        args.db,
        bridge_activation=args.bridge_activation,
        market_context_activation=args.market_context_activation,
        event_date=args.event_date,
    )
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
