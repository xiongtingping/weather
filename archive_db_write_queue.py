#!/usr/bin/env python3
"""Process-local archive.db writer gate.

This module serializes writable archive.db handles through the existing
``data/db_writer.user.lock`` directory protocol.  It is intentionally filesystem
only: no schema changes, no background daemon, and no historical backfill.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_LOCK_DIR = ROOT / "data" / "db_writer.user.lock"

_HELD_LOCK_COUNTS: dict[str, int] = {}
_HELD_LOCK_THREADS: dict[str, int] = {}
_CURRENT_PROCESS_START_IDENTITIES: dict[int, str] = {}
_CURRENT_PROCESS_START_IDENTITIES_GUARD = threading.Lock()


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = float(default)
    if not math.isfinite(value):
        return float(default)
    return float(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        # A writer owned by another uid (the production monitor runs as root)
        # is alive even though this process cannot signal it.  Treating EPERM
        # as dead can misclassify a live production writer as a stale lease.
        return True
    except OSError:
        return False
    # kill(0) also succeeds for a zombie. A terminated writer waiting to be
    # reaped cannot own SQLite or release its completed lock directory; treat
    # it as dead so maintenance and crash recovery can reclaim the lock. If
    # process inspection is unavailable, remain fail-closed.
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    state = str(proc.stdout or "").strip().upper()
    return not (proc.returncode == 0 and state.startswith("Z"))


def _read_pid(lock_dir: Path) -> Optional[int]:
    try:
        text = (lock_dir / "pid").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    return int(text)


def _read_owner_pid(lock_dir: Path) -> Optional[int]:
    """Read a PID from owner metadata when the small pid file is unavailable."""

    try:
        value = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    raw = value.get("pid") if isinstance(value, dict) else None
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _read_owner_metadata(lock_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _process_start_identity(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return str(proc.stdout or "").strip() if proc.returncode == 0 else ""


def _current_process_start_identity() -> str:
    """Resolve this process birth marker once, including after a fork.

    The writer gate may be acquired many times by a resident process.  A
    fresh ``ps`` subprocess on every acquisition adds avoidable latency to
    the AMSC observation fast lane; the PID-keyed cache preserves PID-reuse
    safety while removing that repeated work.
    """

    pid = os.getpid()
    with _CURRENT_PROCESS_START_IDENTITIES_GUARD:
        if pid not in _CURRENT_PROCESS_START_IDENTITIES:
            _CURRENT_PROCESS_START_IDENTITIES[pid] = _process_start_identity(pid)
        return _CURRENT_PROCESS_START_IDENTITIES[pid]


def _pid_still_matches_owner(lock_dir: Path, pid: int) -> bool:
    """Return false only when a stored birth identity proves PID reuse."""

    expected = str(_read_owner_metadata(lock_dir).get("process_start_identity") or "").strip()
    if not expected:
        # Legacy locks remain fail-closed: a live PID without a birth marker
        # cannot be safely declared stale.
        return True
    actual = _process_start_identity(pid)
    return True if not actual else actual == expected


def _lock_initialization_incomplete(lock_dir: Path) -> bool:
    """Detect a creator crash before a durable, attributable owner record.

    A PID file alone is not ownership proof.  In particular, a process killed
    between ``mkdir`` and ``owner.json`` can remain as a live zombie until its
    parent reaps it; protecting that torn lock forever deadlocks maintenance.
    """

    owner = _read_owner_metadata(lock_dir)
    try:
        owner_pid = int(owner.get("pid"))
    except (TypeError, ValueError):
        owner_pid = 0
    return not bool(
        owner.get("schema") == "archive_db_write_queue_lock_v1"
        and owner_pid > 0
        and str(owner.get("owner") or "").strip()
        and str(owner.get("acquired_at") or "").strip()
    )


def _lock_age_sec(lock_dir: Path) -> Optional[float]:
    try:
        return max(0.0, time.time() - float(lock_dir.stat().st_mtime))
    except OSError:
        return None


def _cleanup_stale_lock(lock_dir: Path) -> bool:
    stale_name = f"{lock_dir.name}.stale.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    stale_path = lock_dir.with_name(stale_name)
    try:
        lock_dir.rename(stale_path)
        shutil.rmtree(stale_path, ignore_errors=True)
        return True
    except OSError:
        try:
            shutil.rmtree(lock_dir, ignore_errors=True)
            return not lock_dir.exists()
        except OSError:
            return False


def _lock_dir_from_env() -> Path:
    raw = os.environ.get("PM_ARCHIVE_DB_WRITE_LOCK_DIR", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_LOCK_DIR


@contextmanager
def archive_db_write_gate(
    db_path: Path | str = DEFAULT_DB,
    *,
    owner: str,
    wait_sec: Optional[float] = None,
    poll_sec: Optional[float] = None,
    stale_sec: Optional[float] = None,
    uninitialized_stale_sec: Optional[float] = None,
    lock_dir: Path | str | None = None,
) -> Iterator[None]:
    """Acquire the shared archive.db writer queue lock.

    Reentrant in the same process so helper functions can share the gate
    without deadlocking themselves.
    """

    if not _env_bool("PM_ARCHIVE_DB_WRITE_QUEUE_ENABLED", True):
        yield
        return

    lock_path = Path(lock_dir).expanduser() if lock_dir is not None else _lock_dir_from_env()
    lock_key = str(lock_path.resolve())
    thread_id = threading.get_ident()
    if _HELD_LOCK_COUNTS.get(lock_key, 0) > 0 and _HELD_LOCK_THREADS.get(lock_key) == thread_id:
        _HELD_LOCK_COUNTS[lock_key] += 1
        try:
            yield
        finally:
            next_count = max(0, _HELD_LOCK_COUNTS.get(lock_key, 1) - 1)
            if next_count:
                _HELD_LOCK_COUNTS[lock_key] = next_count
            else:
                _HELD_LOCK_COUNTS.pop(lock_key, None)
                _HELD_LOCK_THREADS.pop(lock_key, None)
        return

    max_wait = _env_float("PM_ARCHIVE_DB_WRITE_QUEUE_WAIT_SEC", 30.0) if wait_sec is None else float(wait_sec)
    poll = _env_float("PM_ARCHIVE_DB_WRITE_QUEUE_POLL_SEC", 0.1) if poll_sec is None else float(poll_sec)
    stale = _env_float("PM_ARCHIVE_DB_WRITE_QUEUE_STALE_SEC", 21600.0) if stale_sec is None else float(stale_sec)
    uninitialized_stale = (
        _env_float("PM_ARCHIVE_DB_WRITE_QUEUE_UNINITIALIZED_STALE_SEC", 1.0)
        if uninitialized_stale_sec is None
        else float(uninitialized_stale_sec)
    )
    max_wait = max(0.0, max_wait)
    poll = max(0.02, min(max(0.02, poll), 2.0))
    stale = max(30.0, stale)
    # A valid owner PID is protected by the much longer stale window.  The
    # short window only recovers a crash in the tiny `mkdir -> pid` gap; it
    # must be long enough that a normal creator cannot be stolen mid-write.
    uninitialized_stale = max(1.0, min(uninitialized_stale, 60.0))

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    acquired = False
    pid = os.getpid()
    # Resolve the birth identity before mkdir.  No slow subprocess is allowed
    # in the recoverable mkdir -> durable owner-record critical section.
    process_start_identity = _current_process_start_identity()
    while True:
        try:
            lock_path.mkdir()
            meta = {
                "schema": "archive_db_write_queue_lock_v1",
                "owner": str(owner or "unknown"),
                "pid": pid,
                "ppid": os.getppid(),
                "db_path": str(db_path),
                "acquired_at": _now_iso(),
                "waited_sec": round(time.monotonic() - start, 3),
            }
            if process_start_identity:
                meta["process_start_identity"] = process_start_identity
            # owner.json is the ownership proof and is written before the
            # compatibility pid file.  A crash before this write is recovered
            # by the short initialization grace; a complete owner remains
            # protected by PID + birth identity.
            owner_tmp = lock_path / f".owner.json.tmp.{pid}"
            owner_tmp.write_text(
                json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(owner_tmp, lock_path / "owner.json")
            (lock_path / "pid").write_text(f"{pid}\n", encoding="utf-8")
            acquired = True
            _HELD_LOCK_COUNTS[lock_key] = 1
            _HELD_LOCK_THREADS[lock_key] = thread_id
            break
        except FileExistsError:
            holder_pid = _read_pid(lock_path) or _read_owner_pid(lock_path)
            age = _lock_age_sec(lock_path)
            initialization_incomplete = _lock_initialization_incomplete(lock_path)
            if initialization_incomplete and age is not None and age >= uninitialized_stale:
                if _cleanup_stale_lock(lock_path):
                    continue
            elif holder_pid is not None:
                holder_is_original_process = (
                    _pid_alive(holder_pid)
                    and _pid_still_matches_owner(lock_path, holder_pid)
                )
                if not holder_is_original_process and _cleanup_stale_lock(lock_path):
                    continue
            elif age is not None:
                threshold = uninitialized_stale if initialization_incomplete else stale
                if age >= threshold and _cleanup_stale_lock(lock_path):
                    continue
            elapsed = time.monotonic() - start
            if elapsed >= max_wait:
                holder_meta = _read_owner_metadata(lock_path)
                raise TimeoutError(
                    f"archive_db_write_queue_timeout owner={owner} waited={elapsed:.2f}s "
                    f"holder_pid={holder_pid or ''} "
                    f"holder_owner={str(holder_meta.get('owner') or '')} "
                    f"holder_acquired_at={str(holder_meta.get('acquired_at') or '')} "
                    f"lock={lock_path}"
                )
            time.sleep(min(poll, max(0.0, max_wait - elapsed)))

    try:
        yield
    finally:
        current = max(0, _HELD_LOCK_COUNTS.get(lock_key, 1) - 1)
        if current:
            _HELD_LOCK_COUNTS[lock_key] = current
            return
        _HELD_LOCK_COUNTS.pop(lock_key, None)
        _HELD_LOCK_THREADS.pop(lock_key, None)
        if acquired:
            try:
                shutil.rmtree(lock_path)
            except OSError:
                pass


@contextmanager
def connect_archive_db_rw(
    db_path: Path | str = DEFAULT_DB,
    *,
    owner: str,
    timeout_sec: float = 10.0,
    busy_timeout_ms: int = 5000,
    row_factory: bool = True,
    journal_mode: Optional[str] = None,
    wal_autocheckpoint_pages: Optional[int] = None,
    queue_wait_sec: Optional[float] = None,
    queue_poll_sec: Optional[float] = None,
) -> Iterator[sqlite3.Connection]:
    with archive_db_write_gate(
        db_path,
        owner=owner,
        wait_sec=queue_wait_sec,
        poll_sec=queue_poll_sec,
    ):
        conn = sqlite3.connect(str(db_path), timeout=float(timeout_sec))
        try:
            if row_factory:
                conn.row_factory = sqlite3.Row
            try:
                if journal_mode:
                    conn.execute(f"PRAGMA journal_mode={journal_mode}")
                if wal_autocheckpoint_pages is not None:
                    conn.execute(
                        f"PRAGMA wal_autocheckpoint={int(max(0, wal_autocheckpoint_pages))}"
                    )
                conn.execute(f"PRAGMA busy_timeout={int(max(0, busy_timeout_ms))}")
            except sqlite3.Error:
                pass
            yield conn
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass
