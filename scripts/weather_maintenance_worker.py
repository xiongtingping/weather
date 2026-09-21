#!/usr/bin/env python3
"""Charter 189 (user, 2026-09-21: 「乙」): the maintenance worker.

The native-ensemble OOS pass -- measurement, isolated from probability and
execution by its own receipt's word -- ran inside every production cycle,
12.6s median and 17% of the cycle (12:04-12:44Z).  This process runs it
instead, one station per step, beside the cycle: the reads and the Python of
scoring a station no longer lengthen a decision cycle, and only the short
persist meets the cycle, at SQLite's write lock (taken up front, see
``run_maintenance_oos_step``).  A separate process rather than a thread: a
CPU-bound thread shares the resident's GIL, and on this machine that
multiplies every row the cycle fetches.

It announces the lane it serves in a heartbeat file; the cycle hands the
lane back to itself whenever that heartbeat is missing, stale, from a dead
pid, or withdrawn -- which this worker does after consecutive failed steps --
so the lane is never left to nobody.  A station whose cross activation
passes is handed to the cycle, which replays its P0: serving is written by
the production process only.

The resident starts and supervises it; it exits when its parent does, holds
a single-instance lock, and runs niced.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.production_worker import (  # noqa: E402
    DEFAULT_TARGET_ACTIVATION,
    MAINTENANCE_WORKER_HEARTBEAT_PATH,
    TargetProductionWorker,
    open_target_writer_if_enabled,
    utc_now,
)

LOCK_PATH = ROOT / "data" / "runtime" / "maintenance_worker.lock"
DEFAULT_ARCHIVE_DB = ROOT / "data" / "archive.db"
LANES: tuple[str, ...] = ("native_ensemble_oos",)
# One station per step, no closer than the cycle's own floor for the pass.
STEP_MIN_INTERVAL_SEC = float(
    os.environ.get("PM_MAINTENANCE_OOS_INTERVAL_SEC", "") or 60.0
)
HEARTBEAT_EVERY_SEC = 20.0
# After this many failed steps in a row the lane goes back to the cycle
# until a step succeeds again.
RELEASE_AFTER_FAILURES = 3
# Waits for the write lock this long, then fails the step (the next step
# tries again); the cycle's own budget is far longer.
BUSY_TIMEOUT_MS = 30_000
NICE = 10


def write_heartbeat(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".maintenance_hb.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(dict(payload), stream, ensure_ascii=False, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _step_failed(receipt: Mapping[str, Any]) -> bool:
    return str(receipt.get("state") or "") in {"blocked_fail_closed", "not_attempted"}


class MaintenanceLoop:
    """The worker's loop, apart from process wiring so it can be tested."""

    def __init__(
        self,
        worker: Any,
        *,
        heartbeat_path: Path = MAINTENANCE_WORKER_HEARTBEAT_PATH,
        interval_sec: float = STEP_MIN_INTERVAL_SEC,
        parent_pid: int | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.worker = worker
        self.heartbeat_path = Path(heartbeat_path)
        self.interval_sec = max(1.0, float(interval_sec))
        self.parent_pid = parent_pid
        self.clock = clock
        self.sleep = sleep
        self.stop = False
        self.consecutive_failures = 0
        self.steps = 0
        self.last: dict[str, Any] = {}

    def serving(self) -> tuple[str, ...]:
        return () if self.consecutive_failures >= RELEASE_AFTER_FAILURES else LANES

    def orphaned(self) -> bool:
        return self.parent_pid is not None and os.getppid() != self.parent_pid

    def beat(self, state: str) -> None:
        write_heartbeat(self.heartbeat_path, {
            "schema": "maintenance_worker_heartbeat_v1",
            "pid": os.getpid(),
            "parent_pid": self.parent_pid,
            "updated_epoch": time.time(),
            "updated_at_utc": utc_now(),
            "state": state,
            "lanes": list(self.serving()) if state != "stopped" else [],
            "steps": self.steps,
            "consecutive_failures": self.consecutive_failures,
            "last_state": str(self.last.get("state") or ""),
            "last_station": str(self.last.get("station_id") or ""),
            "last_reason": str(self.last.get("reason") or "")[:300],
            "last_duration_sec": self.last.get("duration_sec"),
            "last_finished_at_utc": self.last.get("finished_at_utc"),
        })

    def step(self) -> dict[str, Any]:
        started = self.clock()
        self.beat("running")
        receipt = dict(self.worker.run_maintenance_oos_step(now_utc=utc_now()))
        receipt["duration_sec"] = round(self.clock() - started, 3)
        receipt["finished_at_utc"] = utc_now()
        self.steps += 1
        self.consecutive_failures = (
            self.consecutive_failures + 1 if _step_failed(receipt) else 0
        )
        self.last = receipt
        self.beat("idle")
        return receipt

    def rest_after(self, duration: float) -> float:
        """How long to rest after a step that took ``duration``.

        At least the interval, and at least as long as the step itself: the
        worker shares an 8GB machine's page cache and disk with the cycle,
        and on its first hour a step took 67s, longer than the interval, so
        steps ran back to back (2026-09-21 13:38-13:50Z).  Resting as long
        as it worked keeps it under half the machine's time.
        """

        return max(self.interval_sec, float(duration))

    def run(self, *, max_steps: int | None = None) -> None:
        try:
            while not self.stop and not self.orphaned():
                started = self.clock()
                self.step()
                if max_steps is not None and self.steps >= max_steps:
                    break
                rest = self.rest_after(self.clock() - started)
                rest_started = self.clock()
                while not self.stop and not self.orphaned():
                    left = rest - (self.clock() - rest_started)
                    if left <= 0:
                        break
                    self.sleep(min(left, HEARTBEAT_EVERY_SEC))
                    self.beat("idle")
        finally:
            self.beat("stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-db", type=Path, default=DEFAULT_ARCHIVE_DB)
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({"state": "already_running"}))
        return 0
    try:
        os.nice(NICE)
    except OSError:
        pass

    commit_conn = open_target_writer_if_enabled(
        DEFAULT_TARGET_DB, activation_path=DEFAULT_TARGET_ACTIVATION,
    )
    if commit_conn is None:
        # No writer in this environment: claim nothing, so the cycle keeps
        # the lane.
        write_heartbeat(MAINTENANCE_WORKER_HEARTBEAT_PATH, {
            "schema": "maintenance_worker_heartbeat_v1",
            "pid": os.getpid(), "updated_epoch": time.time(),
            "updated_at_utc": utc_now(), "state": "writer_disabled", "lanes": [],
        })
        return 0
    commit_conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    worker = TargetProductionWorker(
        DEFAULT_TARGET_DB,
        commit_conn=commit_conn,
        commit_error=None,
        canonical_source_db_path=args.archive_db,
    )
    loop = MaintenanceLoop(worker, parent_pid=args.parent_pid)

    def stop(_signum, _frame) -> None:
        loop.stop = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        loop.run(max_steps=1 if args.once else None)
    finally:
        try:
            worker.close()
        except Exception:  # noqa: BLE001
            pass
    print(json.dumps({"state": "stopped", "steps": loop.steps, "last": loop.last.get("state")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
