from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from archive_db_write_queue import archive_db_write_gate, connect_archive_db_rw
import archive_db_write_queue as queue


def test_zombie_pid_is_not_treated_as_live_writer_owner(monkeypatch) -> None:
    monkeypatch.setattr(queue.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(
        queue.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="Z+\n"),
    )

    assert queue._pid_alive(12345) is False


def test_uninspectable_signalled_pid_remains_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(queue.os, "kill", lambda _pid, _signal: None)

    def fail(*args, **kwargs):
        raise OSError("ps unavailable")

    monkeypatch.setattr(queue.subprocess, "run", fail)

    assert queue._pid_alive(12345) is True


def test_permission_denied_signal_means_foreign_uid_writer_is_alive(monkeypatch) -> None:
    def deny(_pid, _signal):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(queue.os, "kill", deny)

    assert queue._pid_alive(12345) is True


def test_empty_or_torn_lock_is_reclaimed_after_short_initialization_grace(tmp_path) -> None:
    lock = tmp_path / "db_writer.lock"
    lock.mkdir()
    # Simulate a process crash after creating the lock directory but before
    # it managed to write a usable PID or owner record.
    (lock / "pid").write_text("", encoding="utf-8")
    old = time.time() - 5.0
    os.utime(lock, (old, old))

    with archive_db_write_gate(
        tmp_path / "archive.db",
        owner="test",
        lock_dir=lock,
        wait_sec=0.2,
        stale_sec=3600,
        uninitialized_stale_sec=1,
    ):
        assert (lock / "pid").read_text(encoding="utf-8").strip() == str(os.getpid())

    assert not lock.exists()


def test_live_pid_without_owner_is_reclaimed_after_short_initialization_grace(tmp_path) -> None:
    lock = tmp_path / "db_writer.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    old = time.time() - 5.0
    os.utime(lock, (old, old))

    with archive_db_write_gate(
        tmp_path / "archive.db",
        owner="test",
        lock_dir=lock,
        wait_sec=0.2,
        stale_sec=3600,
        uninitialized_stale_sec=1,
    ):
        assert '"owner": "test"' in (lock / "owner.json").read_text(encoding="utf-8")

    assert not lock.exists()


def test_live_pid_with_complete_owner_is_not_reclaimed(tmp_path) -> None:
    lock = tmp_path / "db_writer.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (lock / "owner.json").write_text(
        '{"schema":"archive_db_write_queue_lock_v1","pid":%d,"owner":"active","acquired_at":"2026-07-15T00:00:00Z"}\n'
        % os.getpid(),
        encoding="utf-8",
    )
    old = time.time() - 5.0
    os.utime(lock, (old, old))
    with pytest.raises(TimeoutError, match="archive_db_write_queue_timeout"):
        with archive_db_write_gate(
            tmp_path / "archive.db", owner="test", lock_dir=lock,
            wait_sec=0.05, stale_sec=3600, uninitialized_stale_sec=1,
        ):
            raise AssertionError("must not acquire")


def test_reused_live_pid_is_reclaimed_when_process_birth_identity_differs(tmp_path, monkeypatch) -> None:
    lock = tmp_path / "db_writer.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (lock / "owner.json").write_text(
        '{"pid":%d,"process_start_identity":"old-process-birth"}\n' % os.getpid(),
        encoding="utf-8",
    )
    monkeypatch.setattr(queue, "_process_start_identity", lambda _pid: "current-process-birth")

    with archive_db_write_gate(
        tmp_path / "archive.db",
        owner="replacement",
        lock_dir=lock,
        wait_sec=0.2,
    ):
        owner = (lock / "owner.json").read_text(encoding="utf-8")
        assert '"owner": "replacement"' in owner

    assert not lock.exists()


def test_new_lock_persists_process_birth_identity(tmp_path, monkeypatch) -> None:
    lock = tmp_path / "db_writer.lock"
    queue._CURRENT_PROCESS_START_IDENTITIES.clear()
    monkeypatch.setattr(queue, "_process_start_identity", lambda _pid: "stable-process-birth")

    with archive_db_write_gate(
        tmp_path / "archive.db",
        owner="identity-test",
        lock_dir=lock,
        wait_sec=0.2,
    ):
        assert '"process_start_identity": "stable-process-birth"' in (
            lock / "owner.json"
        ).read_text(encoding="utf-8")


def test_current_process_birth_identity_is_cached_across_gate_acquisitions(
    tmp_path, monkeypatch
) -> None:
    queue._CURRENT_PROCESS_START_IDENTITIES.clear()
    calls: list[int] = []

    def resolve(pid: int) -> str:
        calls.append(pid)
        return "cached-process-birth"

    monkeypatch.setattr(queue, "_process_start_identity", resolve)
    for suffix in ("one", "two"):
        with archive_db_write_gate(
            tmp_path / "archive.db",
            owner=f"identity-cache-{suffix}",
            lock_dir=tmp_path / f"db_writer_{suffix}.lock",
            wait_sec=0.2,
        ):
            pass

    assert calls == [os.getpid()]


def test_connect_can_override_queue_claim_wait_without_changing_global_policy(tmp_path, monkeypatch) -> None:
    lock = tmp_path / "db_writer.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (lock / "owner.json").write_text(
        '{"schema":"archive_db_write_queue_lock_v1","pid":%d,"owner":"active","acquired_at":"2026-07-15T00:00:00Z"}\n'
        % os.getpid(),
        encoding="utf-8",
    )
    monkeypatch.setenv("PM_ARCHIVE_DB_WRITE_LOCK_DIR", str(lock))

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="archive_db_write_queue_timeout"):
        with connect_archive_db_rw(tmp_path / "archive.db", owner="fast_claim", queue_wait_sec=0.05):
            raise AssertionError("must not acquire")

    assert time.monotonic() - started < 0.5


def test_connect_can_move_wal_autocheckpoint_out_of_latency_critical_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "PM_ARCHIVE_DB_WRITE_LOCK_DIR",
        str(tmp_path / "db_writer.lock"),
    )

    with connect_archive_db_rw(
        tmp_path / "archive.db",
        owner="latency_critical",
        journal_mode="WAL",
        wal_autocheckpoint_pages=0,
    ) as conn:
        assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 0
