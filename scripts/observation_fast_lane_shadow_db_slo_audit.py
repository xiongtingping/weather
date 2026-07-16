#!/usr/bin/env python3
"""Read isolated P10 shadow packets from SQLite and produce the SLO report."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from observation_fast_lane_latency_runtime import (
    build_observation_fast_lane_latency_slo_report,
)
from observation_fast_lane_shadow_telemetry_writer import PACKET_TABLE
from sequential_bayes_public_ssot_shadow_writer import (
    SHADOW_MANIFEST_STATE,
    SHADOW_MANIFEST_TABLE,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _read_packets(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        sentinel = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (SHADOW_MANIFEST_TABLE,),
        ).fetchone()
        if sentinel is None:
            raise ValueError("fast_lane_shadow_db_manifest_table_missing")
        row = conn.execute(
            f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
        ).fetchone()
        if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
            raise ValueError("fast_lane_shadow_db_manifest_invalid")
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (PACKET_TABLE,),
        ).fetchone()
        if table is None:
            raise ValueError("fast_lane_shadow_db_packets_missing")
        records: list[dict[str, Any]] = []
        for (packet_json,) in conn.execute(
            f"SELECT packet_json FROM {PACKET_TABLE} ORDER BY created_at, correlation_id"
        ):
            try:
                parsed = json.loads(_text(packet_json))
            except json.JSONDecodeError as exc:
                raise ValueError("fast_lane_shadow_db_packet_json_invalid") from exc
            if not isinstance(parsed, dict):
                raise ValueError("fast_lane_shadow_db_packet_not_object")
            records.append(parsed)
        return records
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only P10 isolated-shadow SQLite SLO audit")
    parser.add_argument("--db", required=True, help="isolated shadow SQLite DB; archive.db is forbidden")
    parser.add_argument("--require-live-t1-to-t5", action="store_true")
    args = parser.parse_args()
    path = Path(args.db).expanduser().resolve()
    if path.name == "archive.db":
        print(json.dumps({"ok": False, "state": "blocked", "error": "production_archive_db_forbidden", "read_only": True}))
        return 2
    try:
        records = _read_packets(path)
        report = build_observation_fast_lane_latency_slo_report(
            records,
            require_live_t1_to_t5=bool(args.require_live_t1_to_t5),
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"ok": False, "state": "blocked", "error": str(exc), "read_only": True}))
        return 2
    report["input"] = {
        "db": str(path),
        "packet_table": PACKET_TABLE,
        "execution_mode": "isolated_shadow_only_read_only",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
