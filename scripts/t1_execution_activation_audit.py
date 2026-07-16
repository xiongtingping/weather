#!/usr/bin/env python3
"""Read-only T1 execution-contract deployment acceptance audit."""

from __future__ import annotations

import json
import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from t1_execution_activation_audit import build_t1_execution_activation_audit  # noqa: E402
from v31_posterior_execution_latency_audit import (  # noqa: E402
    audit_v31_posterior_execution_latency,
)


def _process_rows() -> list[dict[str, Any]]:
    def parse_etime_seconds(value: str) -> float:
        text = str(value or "").strip()
        if not text:
            return 0.0
        days = 0
        if "-" in text:
            day_text, text = text.split("-", 1)
            try:
                days = int(day_text)
            except ValueError:
                return 0.0
        try:
            parts = [int(item) for item in text.split(":")]
        except ValueError:
            return 0.0
        if len(parts) == 3:
            hours, minutes, seconds = parts
        elif len(parts) == 2:
            hours, minutes, seconds = 0, parts[0], parts[1]
        elif len(parts) == 1:
            hours, minutes, seconds = 0, 0, parts[0]
        else:
            return 0.0
        return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)

    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,etime=,command="],
            cwd=str(ROOT),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except Exception:
        return []
    now = time.time()
    rows: list[dict[str, Any]] = []
    for raw in (completed.stdout or "").splitlines():
        parts = raw.strip().split(None, 2)
        if len(parts) != 3:
            continue
        elapsed = parse_etime_seconds(parts[1])
        if elapsed <= 0.0:
            continue
        rows.append(
            {
                "pid": parts[0],
                "elapsed_seconds": elapsed,
                "started_at_epoch": now - elapsed,
                "command": parts[2],
            }
        )
    return rows


def _required_code_mtime() -> float:
    paths = (
        ROOT / "sequential_bayes_execution_contract.py",
        ROOT / "scripts" / "hard_source_direct_poller.py",
        ROOT / "t1_execution_activation_audit.py",
    )
    return max((path.stat().st_mtime for path in paths if path.exists()), default=0.0)


def _records(db_path: Path) -> list[dict[str, Any]]:
    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        return list(
            audit_v31_posterior_execution_latency(conn, latest_only=True).get("records") or []
        )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-only",
        action="store_true",
        help="verify only the loaded source contract; do not inspect DB/process state",
    )
    args = parser.parse_args()
    if args.source_only:
        from t1_execution_activation_audit import source_t1_contract_probe  # noqa: E402

        source_probe = source_t1_contract_probe()
        report = {
            "schema": "t1_execution_source_contract_probe_v1",
            "ok": source_probe.get("ok") is True,
            "read_only": True,
            "source_probe": source_probe,
            "guards": {
                "writes_archive_db": False,
                "places_orders": False,
                "restarts_launchd": False,
                "changes_parameters": False,
            },
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0 if report["ok"] else 1
    db_path = ROOT / "data" / "archive.db"
    report = build_t1_execution_activation_audit(
        execution_records=_records(db_path),
        process_rows=_process_rows(),
        required_code_mtime_epoch=_required_code_mtime(),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
