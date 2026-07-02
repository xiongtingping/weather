#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "archive.db"


def _fmt_exc(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {str(exc)[:200]}"


def _quick_check(timeout_sec: float) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["sqlite3", str(DB), "PRAGMA quick_check;"],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
            check=False,
        )
        return {
            "ok": proc.returncode == 0 and proc.stdout.strip().splitlines()[:1] == ["ok"],
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip()[:500],
            "stderr": proc.stderr.strip()[:500],
        }
    except subprocess.TimeoutExpired:
        return {"ok": None, "timeout": True, "timeout_sec": timeout_sec}
    except Exception as exc:
        return {"ok": False, "error": _fmt_exc(exc)}


def main() -> int:
    blockers: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {"db": str(DB)}

    if not DB.exists():
        blockers.append("archive_db_missing")
    else:
        try:
            with sqlite3.connect(str(DB), timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")
                page_size = int(conn.execute("PRAGMA page_size").fetchone()[0] or 0)
                page_count = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
                free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)
                journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0] or "")
                wal_row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                details.update(
                    {
                        "page_size": page_size,
                        "page_count": page_count,
                        "freelist_count": free_pages,
                        "free_bytes": page_size * free_pages,
                        "journal_mode": journal_mode,
                        "wal_checkpoint_passive": list(wal_row) if wal_row else None,
                    }
                )
                if journal_mode.lower() != "wal":
                    warnings.append("archive_db_not_wal_mode")
        except sqlite3.DatabaseError as exc:
            blockers.append("archive_db_database_error")
            details["db_error"] = _fmt_exc(exc)
        except Exception as exc:
            warnings.append("archive_db_probe_failed")
            details["db_probe_error"] = _fmt_exc(exc)

    usage = shutil.disk_usage(ROOT / "data")
    details["disk_free_bytes"] = usage.free
    details["disk_total_bytes"] = usage.total
    details["disk_free_ratio"] = usage.free / usage.total if usage.total else None
    if usage.free < 5 * 1024**3:
        blockers.append("disk_free_below_5g")
    elif usage.free < 15 * 1024**3:
        warnings.append("disk_free_below_15g")

    quick_timeout_raw = str(os.environ.get("PM_STORAGE_DB_QUICK_CHECK_TIMEOUT_SEC", "0") or "0").strip()
    try:
        quick_timeout = float(quick_timeout_raw)
    except Exception:
        quick_timeout = 0.0
    if quick_timeout > 0:
        qc = _quick_check(quick_timeout)
        details["quick_check"] = qc
        if qc.get("ok") is False:
            blockers.append("archive_db_quick_check_failed")
        elif qc.get("timeout"):
            warnings.append("archive_db_quick_check_timeout")
    else:
        details["quick_check"] = {"skipped": True, "reason": "disabled_by_default_for_large_live_db"}

    output = {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "details": details,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
