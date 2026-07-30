#!/usr/bin/env python3
"""Independent copy-follow line control: enable, disable, status, or run once."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copy_follow_production import CopyFollowProductionEngine, DEFAULT_CONFIG_PATH, DEFAULT_PAUSE_FILE


def _load() -> dict:
    return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


def _write(value: dict) -> None:
    temporary = DEFAULT_CONFIG_PATH.with_name(f".{DEFAULT_CONFIG_PATH.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, DEFAULT_CONFIG_PATH)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("enable", "disable", "status", "once"))
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    args = parser.parse_args()
    config = _load()
    if args.command == "enable":
        config["state"] = "enabled"
        _write(config)
        try:
            DEFAULT_PAUSE_FILE.unlink()
        except FileNotFoundError:
            pass
        print(json.dumps({"state": "enabled", "pause_file": False}, ensure_ascii=False))
        return 0
    if args.command == "disable":
        config["state"] = "disabled"
        _write(config)
        DEFAULT_PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_PAUSE_FILE.write_text("disabled\n", encoding="utf-8")
        print(json.dumps({"state": "disabled", "pause_file": True}, ensure_ascii=False))
        return 0
    if args.command == "status":
        print(json.dumps({"state": config.get("state"), "pause_file": DEFAULT_PAUSE_FILE.exists()}, ensure_ascii=False))
        return 0
    conn = sqlite3.connect(args.db, timeout=60.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        result = CopyFollowProductionEngine(conn).run_once()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
