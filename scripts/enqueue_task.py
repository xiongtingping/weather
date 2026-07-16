#!/usr/bin/env python3
"""
任务入队工具
用于将任务添加到队列中
"""

import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime

ROOT_DIR = Path(__file__).parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from archive_db_write_queue import connect_archive_db_rw  # noqa: E402

DB_PATH = ROOT_DIR / "data" / "archive.db"

def enqueue_task(task_type, priority=0, payload=None, max_retries=3):
    """将任务添加到队列"""
    try:
        with connect_archive_db_rw(
            DB_PATH,
            owner="enqueue_task",
            timeout_sec=10.0,
            busy_timeout_ms=10000,
            row_factory=False,
        ) as conn:
            cur = conn.cursor()

            payload_json = json.dumps(payload) if payload else None

            cur.execute("""
                INSERT INTO task_queue (task_type, priority, payload_json, max_retries)
                VALUES (?, ?, ?, ?)
            """, (task_type, priority, payload_json, max_retries))

            task_id = cur.lastrowid
            conn.commit()

        print(f"Task #{task_id} enqueued: {task_type} (priority={priority})")
        return task_id

    except Exception as e:
        print(f"Error enqueuing task: {e}", file=sys.stderr)
        return None

def main():
    if len(sys.argv) < 2:
        print("Usage: enqueue_task.py <task_type> [priority] [payload_json]")
        print("\nAvailable task types:")
        print("  - model_scan")
        print("  - model_scan_hourly")
        print("  - daily_scan")
        print("  - market_refresh")
        print("  - market_refresh_urgent")
        print("  - feedback_loop")
        print("  - amsc_posterior_materialize")
        sys.exit(1)

    task_type = sys.argv[1]
    priority = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    payload = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None

    task_id = enqueue_task(task_type, priority, payload)
    sys.exit(0 if task_id else 1)

if __name__ == "__main__":
    main()
