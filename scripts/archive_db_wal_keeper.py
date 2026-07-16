#!/usr/bin/env python3
"""Hold one read-only archive.db connection so fast-lane close cannot checkpoint.

SQLite normally checkpoints and removes WAL sidecars when the final database
connection closes. AMSC probability ingress runs in short-lived children, so
that final-close work can add multi-second latency on a large archive. This
process is supervised by the sole weather monitor and holds only a read-only
connection. It never starts a transaction, mutates SQLite, or places orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import time
from typing import Any


SCHEMA_VERSION = "archive_db_wal_read_keeper_v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--heartbeat-sec", type=float, default=5.0)
    args = parser.parse_args(argv)

    db = args.db.expanduser().resolve()
    status = args.status.expanduser().resolve()
    stop = False

    def request_stop(*_args: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    opened_at = _now()
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0] or "").lower()
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        if journal_mode != "wal":
            raise RuntimeError(f"archive_db_wal_keeper_requires_wal:{journal_mode or 'unknown'}")
        while not stop:
            _atomic_json(
                status,
                {
                    "schema": SCHEMA_VERSION,
                    "state": "running",
                    "pid": os.getpid(),
                    "db": str(db),
                    "journal_mode": journal_mode,
                    "opened_at": opened_at,
                    "heartbeat_at": _now(),
                    "read_only": True,
                    "holds_transaction": False,
                    "writes_archive_db": False,
                    "places_orders": False,
                    "purpose": "prevent_last_connection_wal_checkpoint_on_latency_critical_child_exit",
                },
            )
            time.sleep(max(0.2, min(float(args.heartbeat_sec), 60.0)))
    except Exception as exc:
        _atomic_json(
            status,
            {
                "schema": SCHEMA_VERSION,
                "state": "failed",
                "pid": os.getpid(),
                "db": str(db),
                "failed_at": _now(),
                "error": f"{type(exc).__name__}:{exc}",
                "read_only": True,
                "writes_archive_db": False,
                "places_orders": False,
            },
        )
        return 1
    finally:
        if conn is not None:
            conn.close()
    _atomic_json(
        status,
        {
            "schema": SCHEMA_VERSION,
            "state": "stopped",
            "pid": os.getpid(),
            "db": str(db),
            "opened_at": opened_at,
            "stopped_at": _now(),
            "read_only": True,
            "writes_archive_db": False,
            "places_orders": False,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
