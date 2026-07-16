#!/usr/bin/env python3
"""Threshold-based archive.db cleanup and compact cutover guard."""

from __future__ import annotations

import argparse
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo
from datetime import datetime


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "archive.db"
REBUILD_DIR = ROOT / "data" / "archive_rebuilds"
BACKUP_DIR = ROOT / "data" / "archive_backups"
RESTORE_BACKUP_DIR = ROOT / "data" / "archive_restore_backups"
REPORT_DIR = ROOT / "data" / "reports"
LOG_DIR = ROOT / "data" / "logs"
LOCK_PATH = ROOT / "data" / ".archive_db_auto_guard.lock"
STATE_PATH = ROOT / "data" / "archive_db_auto_guard_state.json"
SYSTEM_MONITOR_DAEMON = "system/com.weather.monitor.daemon"

USER_LABELS = [
    "com.weather.task_queue_worker",
    "com.weather.market_ws",
    "com.weather.market_refresh_1m",
    "com.weather.model_scan",
    "com.weather.model_scan_hourly",
    "com.weather.auto_trade_entry_1m",
    "com.weather.trade_engine",
    "com.weather.q_calibrated_writeback_hourly",
    "com.weather.feedback_loop_hourly",
    "com.weather.daily_trade_sms_summary",
    "com.weather.archive_db_governance_offpeak",
    "com.xiong.weather.ui",
    "com.xiong.weather.auto_trade_watcher",
    "com.xiong.weather.trade_sync_loop",
    "com.xiong.weather.scan_15m",
    "com.weather.scan_15m",
]

WEATHER_PROCESS_PATTERNS = [
    "/Users/xiong/weather/scripts/hard_source_direct_poller.py",
    "/Users/xiong/weather/scripts/auto_trade_watcher.py",
    "/Users/xiong/weather/scripts/task_queue_worker.py",
    "/Users/xiong/weather/scripts/weather_market_anomaly_monitor.py",
    "/Users/xiong/weather/scripts/market_ws_listener.py",
    "/Users/xiong/weather/scripts/market_refresh_1m.sh",
    "/Users/xiong/weather/scripts/discover_weather_markets_fast.py",
    "/Users/xiong/weather/scripts/weather_strategy_batch.py",
    "/Users/xiong/weather/scripts/model_release_watcher.py",
    "/Users/xiong/weather/scripts/daily_review_coverage_audit.py",
    "/Users/xiong/weather/scripts/daily_review_post_apply_acceptance_audit.py",
    "/Users/xiong/weather/scripts/weather_decision_core_audit.py",
    "/Users/xiong/weather/scripts/trade_engine_loop.sh",
    "/Users/xiong/weather/scan_weather_market.py",
    "/Users/xiong/weather/ui/server.py",
    "-m ui.server",
]

INCREMENTAL_ID_TABLES = [
    "event_fact_history",
    "event_market_ticks",
    "event_runs",
    "market_snapshots",
    "model_snapshots",
    "station_observations",
    "auto_trade_decisions",
    "auto_orders",
    "edge_trade_fills",
    "scan_skips",
    "auto_trade_runs",
    "fact_builds",
]

REPLACE_TABLES = [
    "event_fact_latest",
    "auto_trade_jobs",
    "edge_trades",
    "market_tokens",
    "weather_market_discovery",
    "model_member_cache",
    "model_scores",
    "model_weights",
    "model_request_cooldown",
    "net_rate_limits",
    "edge_notifications",
]


def _now_utc() -> str:
    return datetime.now(ZoneInfo("UTC")).replace(microsecond=0).isoformat()


def _stamp() -> str:
    return datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    text = raw.strip().lower()
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    try:
        if text[-1:] in units:
            return int(float(text[:-1]) * units[text[-1]])
        return int(float(text))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _fmt_bytes(value: int | float) -> str:
    num = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0 or unit == "TiB":
            return f"{num:.2f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024.0
    return f"{int(value or 0)}B"


def candidate_build_disk_preflight(
    *,
    source_db: Path = DB,
    rebuild_dir: Path = REBUILD_DIR,
) -> dict[str, Any]:
    """Check whether building a compact candidate can safely allocate disk.

    Low free disk is one trigger for this guard, but candidate construction
    itself needs scratch space. If that scratch space is unavailable, the guard
    must stop after auxiliary cleanup instead of making the full disk problem
    worse with a partial candidate DB.
    """

    try:
        db_bytes = int(source_db.stat().st_size)
    except OSError:
        db_bytes = 0
    wal = Path(str(source_db) + "-wal")
    try:
        wal_bytes = int(wal.stat().st_size)
    except OSError:
        wal_bytes = 0
    margin = _env_int("PM_ARCHIVE_GUARD_BUILD_FREE_MARGIN_BYTES", 8 * 1024**3)
    min_build_free = _env_int(
        "PM_ARCHIVE_GUARD_MIN_BUILD_FREE_BYTES",
        db_bytes + wal_bytes + margin,
    )
    usage_root = rebuild_dir.parent if rebuild_dir.parent.exists() else source_db.parent
    free_bytes = int(shutil.disk_usage(usage_root).free)
    ok = free_bytes >= min_build_free
    return {
        "schema": "archive_db_auto_guard_candidate_build_disk_preflight_v1",
        "ok": ok,
        "source_db": str(source_db),
        "rebuild_dir": str(rebuild_dir),
        "usage_root": str(usage_root),
        "db_bytes": db_bytes,
        "wal_bytes": wal_bytes,
        "free_bytes": free_bytes,
        "required_free_bytes": int(min_build_free),
        "margin_bytes": int(margin),
        "block_reason": "" if ok else "insufficient_disk_for_candidate_build",
        "boundary": "preflight_before_archive_rebuild_candidate_creation",
    }


def _run(cmd: list[str], *, timeout: int = 60, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"command_failed rc={proc.returncode} cmd={cmd} stderr={proc.stderr[:500]}")
    return proc


def _write_state(payload: Mapping[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safe_unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _safe_remove_tree(path: Path) -> bool:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _tail_truncate(path: Path, keep_bytes: int) -> tuple[bool, int]:
    try:
        size = path.stat().st_size
    except OSError:
        return False, 0
    if size <= keep_bytes:
        return False, 0
    marker = (
        f"\n[archive_db_auto_guard retained_tail_bytes={keep_bytes} "
        f"original_bytes={size} at={_now_utc()}]\n"
    ).encode("utf-8")
    try:
        with path.open("rb+") as handle:
            handle.seek(max(0, size - keep_bytes))
            tail = handle.read()
            handle.seek(0)
            handle.truncate(0)
            handle.write(marker + tail)
        return True, max(0, size - path.stat().st_size)
    except OSError:
        return False, 0


def _log_keep_bytes(path: Path) -> int:
    if path.name == "hard_source_direct_poller.log":
        return _env_int("PM_ARCHIVE_GUARD_HARD_SOURCE_LOG_KEEP_BYTES", 64 * 1024**2)
    if path.name in {"trade_engine.log", "auto_trade_entry_1m.log", "feedback_loop_hourly.out.log"}:
        return _env_int("PM_ARCHIVE_GUARD_CORE_LOG_KEEP_BYTES", 32 * 1024**2)
    return _env_int("PM_ARCHIVE_GUARD_LOG_KEEP_BYTES", 16 * 1024**2)


def cleanup_aux_files() -> dict[str, int]:
    removed = {
        "weather_anomaly_reports": 0,
        "rotated_logs": 0,
        "old_guard_candidates": 0,
        "old_restore_backups": 0,
        "old_guard_reports": 0,
        "trimmed_logs": 0,
        "trimmed_log_bytes": 0,
    }
    anomaly_dir = REPORT_DIR / "weather_anomaly"
    cutoff = time.time() - _env_int("PM_ARCHIVE_GUARD_REPORT_TTL_SEC", 6 * 3600)
    if anomaly_dir.exists():
        for path in anomaly_dir.glob("*.json"):
            if "state" in path.name:
                continue
            try:
                if path.stat().st_mtime < cutoff and _safe_unlink(path):
                    removed["weather_anomaly_reports"] += 1
            except OSError:
                pass
    log_cutoff = time.time() - _env_int("PM_ARCHIVE_GUARD_ROTATED_LOG_TTL_SEC", 24 * 3600)
    if LOG_DIR.exists():
        for path in LOG_DIR.glob("*.rotated.gz"):
            try:
                if path.stat().st_mtime < log_cutoff and _safe_unlink(path):
                    removed["rotated_logs"] += 1
            except OSError:
                pass
    stale_cutoff = time.time() - _env_int("PM_ARCHIVE_GUARD_STALE_CANDIDATE_TTL_SEC", 12 * 3600)
    if REBUILD_DIR.exists():
        for path in REBUILD_DIR.glob("archive.compact_guard_*.db*"):
            try:
                if path.stat().st_mtime < stale_cutoff and _safe_unlink(path):
                    removed["old_guard_candidates"] += 1
            except OSError:
                pass
    restore_cutoff = time.time() - _env_int("PM_ARCHIVE_GUARD_RESTORE_BACKUP_TTL_SEC", 24 * 3600)
    if RESTORE_BACKUP_DIR.exists():
        for path in RESTORE_BACKUP_DIR.iterdir():
            try:
                if path.stat().st_mtime < restore_cutoff and _safe_remove_tree(path):
                    removed["old_restore_backups"] += 1
            except OSError:
                pass
    report_cutoff = time.time() - _env_int("PM_ARCHIVE_GUARD_BUILD_REPORT_TTL_SEC", 24 * 3600)
    if REPORT_DIR.exists():
        for pattern in ("archive_db_auto_guard_build_*.json", "archive_db_auto_guard_build_*.md"):
            for path in REPORT_DIR.glob(pattern):
                try:
                    if path.stat().st_mtime < report_cutoff and _safe_unlink(path):
                        removed["old_guard_reports"] += 1
                except OSError:
                    pass
    log_threshold = _env_int("PM_ARCHIVE_GUARD_LOG_TRIM_THRESHOLD_BYTES", 128 * 1024**2)
    if LOG_DIR.exists():
        for path in LOG_DIR.glob("*.log"):
            keep_bytes = _log_keep_bytes(path)
            try:
                if path.stat().st_size <= max(log_threshold, keep_bytes):
                    continue
            except OSError:
                continue
            trimmed, released = _tail_truncate(path, keep_bytes)
            if trimmed:
                removed["trimmed_logs"] += 1
                removed["trimmed_log_bytes"] += released
    return removed


def db_metrics(db: Path) -> dict[str, Any]:
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    metrics: dict[str, Any] = {
        "db_bytes": db.stat().st_size if db.exists() else 0,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "shm_bytes": shm.stat().st_size if shm.exists() else 0,
        "disk_free_bytes": shutil.disk_usage(db.parent).free if db.parent.exists() else 0,
    }
    try:
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)) as conn:
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0] or 0)
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
            freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)
        metrics.update(
            {
                "page_size": page_size,
                "page_count": page_count,
                "freelist_count": freelist_count,
                "freelist_bytes": page_size * freelist_count,
            }
        )
    except sqlite3.Error as exc:
        metrics["sqlite_error"] = str(exc)
    return metrics


def checkpoint(db: Path) -> dict[str, Any]:
    try:
        with closing(sqlite3.connect(str(db), timeout=30)) as conn:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return {"ok": True, "row": tuple(row) if row else None}
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}


def trigger_reasons(metrics: Mapping[str, Any], *, force: bool) -> list[str]:
    if force:
        return ["force"]
    reasons: list[str] = []
    max_db = _env_int("PM_ARCHIVE_GUARD_MAX_DB_BYTES", 18 * 1024**3)
    min_free = _env_int("PM_ARCHIVE_GUARD_MIN_FREE_BYTES", 24 * 1024**3)
    max_wal = _env_int("PM_ARCHIVE_GUARD_MAX_WAL_BYTES", 256 * 1024**2)
    max_freelist = _env_int("PM_ARCHIVE_GUARD_MAX_FREELIST_BYTES", 4 * 1024**3)
    if int(metrics.get("db_bytes") or 0) >= max_db:
        reasons.append("db_size_over_threshold")
    if int(metrics.get("disk_free_bytes") or 0) <= min_free:
        reasons.append("disk_free_below_threshold")
    if int(metrics.get("wal_bytes") or 0) >= max_wal:
        reasons.append("wal_size_over_threshold")
    if int(metrics.get("freelist_bytes") or 0) >= max_freelist:
        reasons.append("freelist_over_threshold")
    return reasons


def build_candidate(stamp: str) -> tuple[Path, dict[str, Any]]:
    REBUILD_DIR.mkdir(parents=True, exist_ok=True)
    output = REBUILD_DIR / f"archive.compact_guard_{stamp}.db"
    report_json = REPORT_DIR / f"archive_db_auto_guard_build_{stamp}.json"
    report_md = REPORT_DIR / f"archive_db_auto_guard_build_{stamp}.md"
    cmd = [
        sys.executable,
        "scripts/archive_db_rebuild_build.py",
        "--source-db",
        str(DB),
        "--output",
        str(output),
        "--fast-plan",
        "--keep-facts-days",
        str(_env_int("PM_ARCHIVE_GUARD_KEEP_FACTS_DAYS", 0)),
        "--keep-runs-days",
        str(_env_int("PM_ARCHIVE_GUARD_KEEP_RUNS_DAYS", 2)),
        "--keep-snapshots-days",
        str(_env_int("PM_ARCHIVE_GUARD_KEEP_SNAPSHOTS_DAYS", 2)),
        "--keep-jobs-days",
        str(_env_int("PM_ARCHIVE_GUARD_KEEP_JOBS_DAYS", 2)),
        "--keep-ticks-days",
        str(_env_int("PM_ARCHIVE_GUARD_KEEP_TICKS_DAYS", 2)),
        "--batch-size",
        str(_env_int("PM_ARCHIVE_GUARD_REBUILD_BATCH_SIZE", 300)),
        "--report-json",
        str(report_json),
        "--report-md",
        str(report_md),
        "--print-json",
    ]
    try:
        proc = _run(cmd, timeout=_env_int("PM_ARCHIVE_GUARD_BUILD_TIMEOUT_SEC", 7200))
        if proc.returncode != 0:
            raise RuntimeError(f"build_candidate_failed rc={proc.returncode} stderr={proc.stderr[-1000:]}")
        payload = json.loads(report_json.read_text(encoding="utf-8"))
        if not payload.get("ok") or not (payload.get("checks") or {}).get("ok"):
            raise RuntimeError(f"candidate_checks_failed report={report_json}")
        return output, payload
    except Exception:
        for suffix in ("", "-wal", "-shm"):
            _safe_unlink(Path(str(output) + suffix))
        raise


def user_uid() -> str:
    return str(os.getuid())


def bootout_user_labels() -> None:
    uid = user_uid()
    for label in USER_LABELS:
        _run(["launchctl", "bootout", f"gui/{uid}/{label}"], timeout=10)


def bootstrap_user_labels() -> None:
    uid = user_uid()
    for label in USER_LABELS:
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        if not plist.exists():
            continue
        _run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], timeout=10)
        _run(["launchctl", "enable", f"gui/{uid}/{label}"], timeout=10)


def _process_stat(pid: int) -> str:
    proc = _run(["ps", "-o", "stat=", "-p", str(pid)], timeout=10)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def monitor_daemon_pid() -> int | None:
    proc = _run(["launchctl", "print", SYSTEM_MONITOR_DAEMON], timeout=10)
    if proc.returncode != 0:
        return None
    match = re.search(r"^\s*pid = (\d+)\s*$", proc.stdout, flags=re.MULTILINE)
    if not match:
        return None
    try:
        pid = int(match.group(1))
    except ValueError:
        return None
    return pid if pid > 0 else None


def pause_monitor_daemon() -> list[dict[str, Any]]:
    if not _env_bool("PM_ARCHIVE_GUARD_PAUSE_MONITOR_DAEMON", True):
        return []
    pid = monitor_daemon_pid()
    if not pid or pid == os.getpid():
        return []
    stat = _process_stat(pid)
    entry: dict[str, Any] = {
        "label": SYSTEM_MONITOR_DAEMON,
        "pid": pid,
        "stat_before": stat,
        "stopped_by_guard": False,
    }
    if "T" in stat:
        entry["already_stopped"] = True
        return [entry]
    try:
        os.kill(pid, signal.SIGSTOP)
        entry["stopped_by_guard"] = True
    except (ProcessLookupError, PermissionError) as exc:
        entry["error"] = f"{exc.__class__.__name__}: {exc}"
    return [entry]


def resume_monitor_daemons(paused: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    resumed: list[dict[str, Any]] = []
    for entry in paused:
        if not entry.get("stopped_by_guard"):
            continue
        pid = int(entry.get("pid") or 0)
        if not pid:
            continue
        result: dict[str, Any] = {"label": entry.get("label"), "pid": pid, "resumed": False}
        try:
            os.kill(pid, signal.SIGCONT)
            result["resumed"] = True
            result["stat_after"] = _process_stat(pid)
        except (ProcessLookupError, PermissionError) as exc:
            result["error"] = f"{exc.__class__.__name__}: {exc}"
        resumed.append(result)
    return resumed


def matching_weather_pids() -> list[int]:
    proc = _run(["pgrep", "-fl", "/Users/xiong/weather"], timeout=10)
    if proc.returncode not in (0, 1):
        return []
    self_pid = os.getpid()
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == self_pid:
            continue
        command = parts[1]
        if any(pattern in command for pattern in WEATHER_PROCESS_PATTERNS):
            pids.append(pid)
    return sorted(set(pids))


def terminate_weather_processes(*, force: bool = False) -> None:
    sig = signal.SIGKILL if force else signal.SIGTERM
    for pid in matching_weather_pids():
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass


def lsof_db(db: Path) -> str:
    paths = [str(db), str(db) + "-wal", str(db) + "-shm"]
    proc = _run(["lsof", *paths], timeout=15)
    return proc.stdout.strip()


def wait_for_quiescent(db: Path, *, timeout_sec: int) -> bool:
    deadline = time.time() + max(1, timeout_sec)
    bootout_user_labels()
    while time.time() < deadline:
        terminate_weather_processes(force=False)
        time.sleep(1)
        if not lsof_db(db):
            return True
        if deadline - time.time() < 5:
            terminate_weather_processes(force=True)
        time.sleep(1)
    return not lsof_db(db)


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, db_name: str, table: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {db_name}.sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, db_name: str, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA {db_name}.table_info({_q(table)})")]


def _has_id(conn: sqlite3.Connection, db_name: str, table: str) -> bool:
    return "id" in _columns(conn, db_name, table)


def sync_small_delta(candidate: Path) -> dict[str, Any]:
    max_delta = _env_int("PM_ARCHIVE_GUARD_MAX_SYNC_DELTA_ROWS", 5000)
    summary: dict[str, Any] = {"incremental": {}, "replace": {}}
    with closing(sqlite3.connect(str(candidate), timeout=60)) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute(f"ATTACH DATABASE 'file:{DB}?mode=ro' AS src")
        deltas: dict[str, int] = {}
        for table in INCREMENTAL_ID_TABLES:
            if not (_table_exists(conn, "main", table) and _table_exists(conn, "src", table)):
                continue
            if not (_has_id(conn, "main", table) and _has_id(conn, "src", table)):
                continue
            dest_max = int(conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM main.{_q(table)}").fetchone()[0] or 0)
            src_max = int(conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM src.{_q(table)}").fetchone()[0] or 0)
            delta = max(0, src_max - dest_max)
            deltas[table] = delta
            if delta > max_delta:
                raise RuntimeError(f"sync_delta_too_large table={table} delta={delta} max={max_delta}")
        conn.execute("BEGIN")
        for table, delta in deltas.items():
            if delta <= 0:
                summary["incremental"][table] = 0
                continue
            cols = _columns(conn, "main", table)
            col_list = ",".join(_q(col) for col in cols)
            dest_max = int(conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM main.{_q(table)}").fetchone()[0] or 0)
            before = conn.total_changes
            conn.execute(
                f"INSERT OR REPLACE INTO main.{_q(table)} ({col_list}) "
                f"SELECT {col_list} FROM src.{_q(table)} WHERE id > ?",
                (dest_max,),
            )
            summary["incremental"][table] = conn.total_changes - before
        for table in REPLACE_TABLES:
            if not (_table_exists(conn, "main", table) and _table_exists(conn, "src", table)):
                continue
            cols = _columns(conn, "main", table)
            col_list = ",".join(_q(col) for col in cols)
            before = conn.total_changes
            conn.execute(f"DELETE FROM main.{_q(table)}")
            conn.execute(f"INSERT INTO main.{_q(table)} ({col_list}) SELECT {col_list} FROM src.{_q(table)}")
            summary["replace"][table] = conn.total_changes - before
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return summary


def validate_candidate(candidate: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(str(candidate), timeout=60)) as conn:
        latest_missing = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM event_fact_latest l
                LEFT JOIN event_fact_history h ON h.id = l.fact_id
                WHERE h.id IS NULL
                """
            ).fetchone()[0]
            or 0
        )
        latest_compacted = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM event_fact_latest l
                JOIN event_fact_history h ON h.id = l.fact_id
                WHERE h.fact_json LIKE '%"history_compacted": true%'
                """
            ).fetchone()[0]
            or 0
        )
        quick = str(conn.execute("PRAGMA quick_check").fetchone()[0] or "")
    return {
        "latest_missing": latest_missing,
        "latest_compacted": latest_compacted,
        "quick_check": quick,
        "ok": latest_missing == 0 and latest_compacted == 0 and quick == "ok",
    }


def swap_candidate(candidate: Path, stamp: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"archive.db.pre_guard_{stamp}"
    checkpoint(DB)
    if lsof_db(DB):
        raise RuntimeError("source_db_not_quiescent_before_swap")
    DB.rename(backup)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(DB) + suffix)
        if sidecar.exists():
            sidecar.rename(BACKUP_DIR / f"archive.db{suffix}.pre_guard_{stamp}")
    candidate.rename(DB)
    for suffix in ("-wal", "-shm"):
        _safe_unlink(Path(str(candidate) + suffix))
    return backup


def post_checks() -> dict[str, Any]:
    ssot = _run([sys.executable, "scripts/check_ssot_alignment.py"], timeout=120)
    audit = _run([sys.executable, "scripts/weather_decision_core_audit.py"], timeout=180)
    latest = validate_candidate(DB)
    return {
        "latest": latest,
        "ssot_returncode": ssot.returncode,
        "ssot_stdout": ssot.stdout[-2000:],
        "ssot_stderr": ssot.stderr[-1000:],
        "audit_returncode": audit.returncode,
        "audit_stdout": audit.stdout[-2000:],
        "audit_stderr": audit.stderr[-1000:],
        "ok": latest.get("ok") and ssot.returncode == 0 and audit.returncode == 0,
    }


def rollback(backup: Path) -> None:
    failed = DB.with_name(f"archive.db.failed_guard_{_stamp()}")
    if DB.exists():
        DB.rename(failed)
    backup.rename(DB)


def run_guard(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    stamp = _stamp()
    aux = cleanup_aux_files()
    before = db_metrics(DB)
    checkpoint_result = checkpoint(DB) if before.get("wal_bytes", 0) else {"ok": True, "skipped": True}
    after_checkpoint = db_metrics(DB)
    reasons = trigger_reasons(after_checkpoint, force=bool(args.force))
    payload: dict[str, Any] = {
        "ok": True,
        "generated_at_utc": _now_utc(),
        "mode": "check",
        "aux_cleanup": aux,
        "before": before,
        "checkpoint": checkpoint_result,
        "after_checkpoint": after_checkpoint,
        "trigger_reasons": reasons,
    }
    if not reasons:
        return payload

    payload["mode"] = "compact_cutover"
    reuse_candidate = str(getattr(args, "candidate", "") or "").strip()
    if bool(getattr(args, "skip_sync_delta", False)) and not reuse_candidate:
        raise RuntimeError("--skip-sync-delta requires --candidate")
    if reuse_candidate:
        candidate = Path(str(args.candidate)).expanduser().resolve()
        if not candidate.exists():
            raise RuntimeError(f"candidate_missing path={candidate}")
        initial_checks = validate_candidate(candidate)
        if not initial_checks.get("ok"):
            raise RuntimeError(f"candidate_initial_checks_failed path={candidate} checks={initial_checks}")
        payload["candidate"] = {"path": str(candidate), "build": {"status": "reused"}, "checks": initial_checks}
    else:
        disk_preflight = candidate_build_disk_preflight()
        payload["candidate_build_disk_preflight"] = disk_preflight
        if not disk_preflight.get("ok"):
            payload.update(
                {
                    "ok": True,
                    "skipped": disk_preflight.get("block_reason"),
                    "blocked": disk_preflight.get("block_reason"),
                    "next_boundary": "free_disk_or_move_backups_before_archive_compact_candidate_build",
                }
            )
            return payload
        candidate, build_payload = build_candidate(stamp)
        payload["candidate"] = {
            "path": str(candidate),
            "build": build_payload.get("build"),
            "checks": build_payload.get("checks"),
        }
    paused_monitors = pause_monitor_daemon()
    if paused_monitors:
        payload["paused_monitor_daemons"] = paused_monitors
    try:
        if not wait_for_quiescent(DB, timeout_sec=_env_int("PM_ARCHIVE_GUARD_QUIESCE_TIMEOUT_SEC", 60)):
            payload.update({"ok": False, "blocked": "source_db_not_quiescent", "lsof": lsof_db(DB)})
            return payload
        if bool(getattr(args, "skip_sync_delta", False)):
            payload["sync_delta"] = {
                "skipped": True,
                "reason": "skip_sync_delta_requested_for_validated_candidate_recovery",
                "source_db_metrics": db_metrics(DB),
            }
        else:
            payload["sync_delta"] = sync_small_delta(candidate)
        candidate_checks = validate_candidate(candidate)
        payload["candidate"]["post_sync_checks"] = candidate_checks
        if not candidate_checks.get("ok"):
            payload.update({"ok": False, "blocked": "candidate_post_sync_checks_failed"})
            return payload
        if not wait_for_quiescent(DB, timeout_sec=_env_int("PM_ARCHIVE_GUARD_QUIESCE_TIMEOUT_SEC", 60)):
            payload.update({"ok": False, "blocked": "source_db_not_quiescent_after_sync", "lsof": lsof_db(DB)})
            return payload
        backup = swap_candidate(candidate, stamp)
        payload["backup"] = str(backup)
        checks = post_checks()
        payload["post_checks"] = checks
        if not checks.get("ok"):
            rollback(backup)
            payload.update({"ok": False, "blocked": "post_checks_failed_rolled_back"})
            return payload
        if not _env_bool("PM_ARCHIVE_GUARD_KEEP_BACKUP", False):
            _safe_unlink(backup)
            for suffix in ("-wal", "-shm"):
                _safe_unlink(BACKUP_DIR / f"archive.db{suffix}.pre_guard_{stamp}")
            payload["backup_deleted"] = True
        payload["after"] = db_metrics(DB)
        payload["elapsed_sec"] = round(time.monotonic() - started, 3)
    finally:
        resumed_monitors = resume_monitor_daemons(paused_monitors)
        if resumed_monitors:
            payload["resumed_monitor_daemons"] = resumed_monitors
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Run compact rebuild even if thresholds are not crossed.")
    parser.add_argument("--candidate", default="", help="Reuse an existing checked candidate DB and run sync/cutover.")
    parser.add_argument(
        "--skip-sync-delta",
        action="store_true",
        help=(
            "Skip source-to-candidate delta sync after validating --candidate. "
            "Use only for recovery when the source archive DB cannot be opened normally."
        ),
    )
    parser.add_argument("--print-json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            payload = {"ok": True, "skipped": "already_running", "generated_at_utc": _now_utc()}
            _write_state(payload)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
        payload: dict[str, Any]
        try:
            payload = run_guard(args)
        except Exception as exc:
            payload = {
                "ok": False,
                "generated_at_utc": _now_utc(),
                "error": f"{exc.__class__.__name__}: {exc}",
                "traceback": traceback.format_exc(limit=12),
            }
        finally:
            bootstrap_user_labels()
        _write_state(payload)
        if args.print_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(json.dumps({
                "ok": payload.get("ok"),
                "mode": payload.get("mode"),
                "trigger_reasons": payload.get("trigger_reasons"),
                "before_db": _fmt_bytes(int((payload.get("before") or {}).get("db_bytes") or 0)),
                "after_db": _fmt_bytes(int((payload.get("after") or payload.get("after_checkpoint") or {}).get("db_bytes") or 0)),
            }, ensure_ascii=False, sort_keys=True))
        return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
