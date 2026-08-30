#!/usr/bin/env python3
"""User-domain resident daemon for global AWC canonical truth ingress."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import math
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_runtime_network_env() -> None:
    """Make direct ``python3`` invocations use the same transport as launchd."""

    path = Path(os.environ.get("PM_SHARED_POLY_ENV_PATH") or ROOT / ".env")
    if not path.is_file():
        return
    # launchd binds the target execution boundary.  A shared .env file is a
    # transport/config source, never an authority that may flip the resident
    # back to live submission or disable the explicitly requested target
    # writer.  Preserve values already injected by the service domain.
    launchd_bound = {
        key: os.environ.get(key)
        for key in ("PM_TARGET_EXECUTION_MODE", "PM_TARGET_NO_LIVE", "PM_TARGET_PRODUCTION_WRITER")
        if os.environ.get(key) is not None
    }
    allowed_proxy = {
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
        "GLOBAL_AGENT_HTTP_PROXY",
    }
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not (key.startswith("PM_") or key in allowed_proxy):
            continue
        if key in launchd_bound:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


_load_runtime_network_env()

from reported_trajectory_market_registry import (
    commit_registry_rows,
    discovered_slugs,
    fetch_gamma_event,
    fetch_gamma_events_window,
    load_config,
    materialize_event_registry,
)
from reported_trajectory_awc_ingress import ReportedTrajectoryAwcIngress
from weather_basket_yes_schema import (
    apply_weather_basket_yes_schema,
    weather_basket_yes_schema_status,
)
from reported_trajectory_sources import fetch_awc_temperature_reports, utc_now
from asos_one_minute_ingress import (
    AsosOneMinuteIngress,
    fetch_asos_one_minute_temperature,
    icao_to_iem_station,
)
from madis_hfmetar import (
    decode_madis_hfmetar_object,
    fetch_madis_hfmetar_object,
    ingest_madis_hfmetar_object,
)
from asos_argmax_window import (
    LIVE_STATIONS as ASOS_ARGMAX_LIVE_STATIONS,
    materialize_missing_argmax_windows,
)
from asos_argmax_entry import materialize_argmax_entry_facts
from asos_argmax_execution import process_pending_argmax_orders
from asos_argmax_taf_reference import materialize_argmax_taf_references
from asos_p1_slot_materializer import materialize_scheduled_asos_p1_observations
from bayes_basket_target import serving_arithmetic
from bayes_basket_target.database import DEFAULT_TARGET_DB
from bayes_basket_target.market_registry import (
    export_shared_live_registry,
    load_active_registry_rows,
)
from bayes_basket_target.settlement_time import settlement_day_window
from bayes_basket_target.production_worker import (
    DEFAULT_TARGET_ACTIVATION,
    DEFAULT_TARGET_MARKET_MANIFEST,
    DEFAULT_TARGET_NWP_RUNTIME_ROOT,
    DEFAULT_TARGET_TAF_INBOX,
    TargetProductionWorker,
    open_target_writer_if_enabled,
    statement_probe_snapshot,
)
from bayes_basket_target.resource_bounds import (
    MAX_TARGET_DB_BYTES,
    assert_connection_target_capacity,
    load_bounded_json,
)
from bayes_basket_target.taf_rotation_state import (
    MAX_SELECTED_SCOPES as MAX_TAF_SCOPES_PER_POLL,
    build_catalog as build_taf_rotation_catalog,
    build_rotation_state as build_taf_rotation_state,
    load_rotation_state as load_taf_rotation_state,
    persist_rotation_state as persist_taf_rotation_state,
    scope_key as taf_rotation_scope_key,
    select_scopes as select_taf_rotation_scopes,
)
from scripts.capture_bayes_basket_target_awc_taf_revision import (
    capture_forward_awc_taf,
    refresh_forward_taf_inbox,
)
# The unique target worker owns the compile_production_cycle_from_ssot route;
# the resident daemon only schedules that worker and never reimplements it.


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_STATUS = ROOT / "data" / "weather_basket_yes_status.json"
SCHEMA_VERSION = "reported_trajectory_awc_ingress_daemon_v1"
SQLITE_IO_RECOVERY_BACKOFF_SEC = 5.0
# The target lane is a single resident writer.  A concurrent maintenance
# writer must not consume a whole production cadence in SQLite's default
# minute-long busy wait: classify it, retain the last committed SSOT state,
# and retry on the scheduler's bounded backoff instead.
#
# 5s was that bound, and it was written when "a concurrent maintenance
# writer" was the only thing expected to hold the lock.  It is not: the
# resident contends with itself, because every lane commits through one
# ``BEGIN IMMEDIATE`` on one SSOT file.  Measured 2026-08-16, a single
# ``commit_blocked_scope_decision`` holds it for up to 4,073ms -- and 4,989ms
# the cycle before -- so the margin before another lane is refused was under
# a second, and any one slow transaction failed that lane closed for the
# round.  That is what the four post-restart ``database is locked`` cycles
# were.  Six times the observed worst hold leaves room for the transaction
# that is actually running without pretending the contention is external;
# the cycle it rides inside is ~600s, so waiting 30s is strictly cheaper
# than losing the round's forward book, which no later cycle can refetch.
# The right end state is a shorter transaction, and this bound should come
# back down when it lands.
#
# Raised to 120s on 2026-08-24.  The 30s bound was sized against a healthy
# WAL; with checkpointing starved by a leaked dashboard reader the WAL sat
# at 417MB, every writer slowed, and the ledger sweep's ordinary bursts held
# the lock past 30s -- P1 then failed closed for 45 minutes across three
# consecutive restarts.  Waiting stretches the cycle; failing closed loses
# the round.  The reader leak is fixed alongside this, so the long waits
# should be rare -- but a lock timeout must be sized for the bad day, not
# the good one.
TARGET_SQLITE_LOCK_TIMEOUT_MS = 120_000
TARGET_SQLITE_LOCK_BACKOFF_SEC = 5.0
TARGET_SQLITE_LOCK_MAX_BACKOFF_SEC = 60.0
TARGET_WAL_REOPEN_REASONS = (
    "target_database_wal_checkpoint_failed",
    "target_database_wal_checkpoint_busy",
    "target_database_wal_bytes_exceeded_after_checkpoint",
)
TARGET_TAF_POLL_INTERVAL_SEC = 60.0
TARGET_NWP_POLL_INTERVAL_SEC = 600.0

# How long a writer lane stands down after finishing, before its own gate can
# open again.
#
# ``next_production``/``next_forecast`` are armed at submission, so a cycle
# that outlives its own interval leaves the gate already open when the future
# clears -- and because the production gate is evaluated before the forecast
# gate in the same pass, production reclaimed the single writer on the very
# iteration it released it.  From 2026-08-26T06:21Z, when the forecast
# prefetch became the third gated writer, that meant the prefetch never ran
# once: no NWP rotation step, no rotation_state write, no model capture, and
# all four ensemble families ageing past their publication cycle while
# production cycled normally.  One second is enough -- the other lanes' gates
# are evaluated before this one comes due again -- and it is taken from a
# cycle of several hundred seconds, so the cadence cost is not measurable.
WRITER_YIELD_SEC = 1.0

# Sweep-class SSOT writers, hosted by this daemon instead of launchd.
#
# SQLite has one writer at a time, and four independently-timed launchd jobs
# plus this resident made five processes contending for it.  On 2026-08-24 an
# ordinary ledger burst against a checkpoint-starved WAL held the lock past
# the resident's own bound and P1 failed closed for 45 minutes -- the
# maintenance writers were not misbehaving, they were merely uncoordinated.
# Hosting them here serialises every routine writer through one scheduler: a
# sweep runs only while no production cycle is in flight, one sweep at a
# time, and a production cycle is not submitted while a sweep runs.  The
# scripts themselves are unchanged and keep their single-instance guards, so
# a manual run beside the resident stays safe during any transition.
#
# Intervals are the launchd cadences they replace.  A sweep becomes due, not
# scheduled: with production idle-gating the realised cadence stretches by
# the cycle length, which trades probe density for never fail-closing the
# stage that prices the book.
HOSTED_SWEEPS: tuple[tuple[str, str, float, float], ...] = (
    # (name, script, interval_sec, timeout_sec)
    ("maker_shadow_probe", "scripts/maker_shadow_probe_sweep.sh", 300.0, 900.0),
    ("pr12_awc_observation", "scripts/pr12_awc_observation_sweep.sh", 600.0, 900.0),
    ("paper_trading_ledger", "scripts/paper_trading_ledger_sweep.sh", 900.0, 1800.0),
    ("settlement_truth", "scripts/settlement_truth_sweep.sh", 1800.0, 900.0),
    # The two served calibration layers (band reliability, centre/width)
    # used to refit only from the 02:00 crontab, and macOS cron silently
    # skips the minute when the machine sleeps through it -- the fit
    # journal shows no cron round on 2026-08-16 or 2026-08-19, two silent
    # losses in ten days, each stalling both evidence clocks for a day.
    # Due-not-scheduled fixes that class; both refits are
    # content-addressed and admission-gated, so an early or doubled run
    # commits nothing new.
    ("calibration_refit", "scripts/calibration_refit_sweep.sh", 86_400.0, 900.0),
    # Capital that only ever leaves the pool.  A prepared cycle holds its
    # reservation until a newer decision on the same station-day supersedes
    # it, and a day whose settlement window has closed can never compile one
    # -- so the last cycle of every day that traded strands its reservation.
    # 161.32 USDC on 54 station-days as of 2026-08-26 against a Tier-1 cap of
    # 180.00, and crossing that cap zeroes all forty-five stations at once.
    # Hourly because the cost of a pass with nothing to do is one indexed
    # scan, and the cost of being late is a fleet-wide halt.
    (
        "settled_day_cycle_close",
        "scripts/settled_day_cycle_close_sweep.sh",
        3600.0,
        900.0,
    ),
    # A corrected report halts its station's P1 until a review receipt exists,
    # and the policy that writes one only ever ran by hand.  Measured
    # 2026-08-26: an on-hour COR for RKPK at 08:00Z left P1 fail-closed on
    # ``correction_review_required`` for the rest of the day.  Ten minutes
    # because the halt costs a station its live stage from the moment the
    # correction lands, and a pass with nothing pending is one indexed scan.
    (
        "correction_review_auto_clear",
        "scripts/correction_review_auto_clear_sweep.sh",
        600.0,
        900.0,
    ),
    # The next-day fleet regime series.  Every other calibration layer here
    # refits on a daily clock because it fits parameters and has to earn a
    # forward window first; this one fits nothing and exists to react to
    # yesterday, so a daily cadence would defeat the only thing it is for.
    # Half-hourly matches the truth sweep that feeds it, and a pass whose
    # truth has not moved skips the bridge scan and costs one indexed read.
    (
        "fleet_regime_refresh",
        "scripts/fleet_regime_refresh_sweep.sh",
        1800.0,
        1800.0,
    ),
    # The inbox a P0 compile reads is overwritten every cycle, so the input to
    # any past compile is gone and no upstream parameter has ever been
    # scorable on the book -- the ledger reads compiled envelopes and cannot
    # produce them.  This keeps each packet once, content-keyed, so a later
    # recompile can be handed exactly what production was handed.  Ten minutes
    # because a packet that is overwritten before it is seen is the one case
    # this cannot recover, and a pass over an unchanged inbox writes nothing.
    (
        "forward_packet_retention",
        "scripts/forward_packet_retention_sweep.sh",
        600.0,
        600.0,
    ),
    # The regional source RCSS earned.  It is the only station of twenty-six
    # candidates whose regional model cleared the dispersion line, all three
    # admission gates, and the drop-the-best-days check -- and RCSS is the
    # station the bias layer has failed at all month, +5.75C applied with
    # +1.4C still standing, because a 25 km cell cannot see the Taipei basin.
    # Hourly: only two of MSM's eight daily runs are captured, so there is
    # seldom anything new within an hour, and a pass with nothing new exits
    # on a handful of indexed reads.  The timeout is generous because a
    # station-day is 86 seconds against a mirror that sometimes times out.
    (
        "capture_jma_msm_forward",
        "scripts/capture_jma_msm_forward_sweep.sh",
        3600.0,
        1800.0,
    ),
    # The CONUS pair from the same 46-pair sweep that widened the MSM lane:
    # HRRR and the NBM cleared the three admission gates at more roster
    # stations than every other regional source combined (NBM at ten of
    # eleven US stations).  Hourly for the MSM lane's reason -- only the
    # 00Z/12Z cycles are captured and the peak-coverage pre-filter prunes
    # arithmetically, so most passes exit on indexed reads.  The timeout
    # covers six runs of ranged S3 reads at 30-50 MB each.
    (
        "capture_noaa_conus_forward",
        "scripts/capture_noaa_conus_forward_sweep.sh",
        3600.0,
        1800.0,
    ),
    # ICON-EU deterministic, run-scoped: one whole-domain download serves
    # seven European stations and two event days.  Hourly against a
    # six-hourly product because the DWD archive keeps no history -- a
    # missed run is permanent, a wasted pass is two directory probes.
    (
        "capture_dwd_icon_eu_forward",
        "scripts/capture_dwd_icon_eu_forward_sweep.sh",
        3600.0,
        1800.0,
    ),
)


# Which database each sweep WRITES.  Concurrency is granted across domains and
# never within one, so "one routine writer per database at a time" -- the whole
# reason these were taken off launchd on 2026-08-24, after four ledger rounds
# ran at once, three held the SSOT write lock, and P1 was fail-closed for
# forty-five minutes -- survives verbatim.  Read-only access does not count:
# WAL readers never block a writer, which is why the correction pass, which
# opens the SSOT ``mode=ro`` and commits only to the archive, is an archive
# writer here.
#
# Verified per script rather than assumed, since a wrong entry here is exactly
# the 2026-08-24 outage:
#   ssot       - the sweep opens data/bayes_basket_target_ssot.db writable
#   archive    - writes data/archive.db only (SSOT connection is mode=ro)
#   none       - touches no database at all (packet retention is gzip files on
#                disk, deliberately: the SSOT sits near its size ceiling)
# A name missing from this map is treated as an SSOT writer, so a sweep added
# without a decision here loses throughput rather than the invariant.
HOSTED_SWEEP_WRITE_DOMAIN: dict[str, str] = {
    "maker_shadow_probe": "ssot",
    "pr12_awc_observation": "ssot",
    "paper_trading_ledger": "ssot",
    "settlement_truth": "ssot",
    "calibration_refit": "ssot",
    "settled_day_cycle_close": "ssot",
    "correction_review_auto_clear": "archive",
    "fleet_regime_refresh": "ssot",
    "forward_packet_retention": "none",
    "capture_jma_msm_forward": "ssot",
    "capture_noaa_conus_forward": "ssot",
    "capture_dwd_icon_eu_forward": "ssot",
}
# Domains that name a database hold one sweep at a time; "none" writes nothing
# and is bounded only by the total cap below.
_EXCLUSIVE_WRITE_DOMAINS = frozenset({"ssot", "archive"})
# Total sweeps in flight at once.  Concurrency here costs production nothing:
# a slot already excludes production, so running k sweeps in one slot takes
# max(duration) where k slots took the sum, and production's share of turns is
# unchanged -- it is the sweep queue that drains k times faster.  Four bounds
# the subprocess fan-out on a machine that is also running the cycle.
HOSTED_SWEEP_MAX_CONCURRENCY = 4


def hosted_sweep_write_domain(name: str) -> str:
    """The database a sweep writes; unknown names are assumed to write the SSOT."""

    return HOSTED_SWEEP_WRITE_DOMAIN.get(name, "ssot")


def admissible_hosted_sweeps(
    due: "list[tuple[float, int, str, str, float]]",
    *,
    running_names: "Iterable[str]",
    max_concurrency: int = HOSTED_SWEEP_MAX_CONCURRENCY,
) -> "list[tuple[float, int, str, str, float]]":
    """Pick the due sweeps that may start now, most overdue first.

    Admission is by write domain, not by name: a sweep may start when nothing
    already running writes the database it writes.  ``due`` carries the same
    ``(overdue, index, name, script, timeout)`` shape the scheduler sorts on,
    so the ordering rule -- longest overdue wins, list order breaks ties --
    stays in one place.
    """

    # Materialized once: the caller may pass a view or a generator, and
    # consuming it twice would silently read the second use as empty.
    running = list(running_names)
    claimed = {
        hosted_sweep_write_domain(name) for name in running
    } & _EXCLUSIVE_WRITE_DOMAINS
    inflight = len(running)
    chosen: list[tuple[float, int, str, str, float]] = []
    for item in sorted(due, key=lambda entry: (-entry[0], entry[1])):
        if inflight + len(chosen) >= max_concurrency:
            break
        domain = hosted_sweep_write_domain(item[2])
        if domain in claimed:
            continue
        if domain in _EXCLUSIVE_WRITE_DOMAINS:
            claimed.add(domain)
        chosen.append(item)
    return chosen


# Every hosted sweep currently running, so shutdown can end them instead of
# leaving them behind.
#
# ``run_hosted_sweep`` gives each sweep its own session, which is what lets a
# timeout kill the whole tree -- and equally what lets the tree outlive this
# daemon.  On shutdown the executor is drained with ``wait=True``, so a
# paper-ledger round with twenty minutes left to run holds the exit open past
# launchd's grace, the daemon is SIGKILLed, and the sweep becomes an orphan
# holding the single SQLite writer with nothing supervising it.  Measured
# 2026-08-26: a restart during a ``paper_trading_ledger --backfill`` left it
# reparented to init and the replacement resident sat blocked on the write
# lock for eleven minutes without a heartbeat -- exactly the production stall
# a deploy is meant to fix.
_HOSTED_SWEEP_PROCESSES: "set[subprocess.Popen[str]]" = set()
_HOSTED_SWEEP_PROCESS_LOCK = threading.Lock()


def _terminate_inflight_hosted_sweeps() -> list[int]:
    """End every running sweep so none of them outlives this process."""

    with _HOSTED_SWEEP_PROCESS_LOCK:
        running = list(_HOSTED_SWEEP_PROCESSES)
    ended: list[int] = []
    for process in running:
        if process.poll() is not None:
            continue
        ended.append(process.pid)
        _terminate_process_group(process)
    return ended


def _terminate_process_group(process: "subprocess.Popen[str]") -> None:
    """Kill a sweep's whole process group, then confirm it is gone.

    SIGTERM first so a wrapper can unwind its own lock file, SIGKILL after a
    short grace for anything that ignores it.  Signalling the group is the
    point: the shell is only ever a launcher.
    """

    try:
        group = os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()
        return
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, signal_number)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            process.wait(timeout=5.0)
            return
        except subprocess.TimeoutExpired:
            continue


def _stamp_serving_arithmetic(
    commit_conn: "sqlite3.Connection | None", *, started_at_utc: str
) -> dict[str, Any]:
    """Record when each stage's serving arithmetic began actually serving.

    The boundary the centre/width evidence floor needs is not when the code
    was committed and not when a refit next happened to look -- it is when a
    process that serves probabilities started running it.  Decisions taken
    between a commit and the restart that picked it up were produced by the
    old arithmetic, so a commit timestamp would admit evidence from a
    distribution nobody was serving yet, which is the one direction that
    floor exists to prevent.

    Stamping here makes this resident's own start that boundary.  It is the
    sole long-lived compiler of P0/P0-M/P1 -- everything else that compiles
    them is a one-shot script running whatever is on disk at the time -- so
    the first start carrying a fingerprint is the first moment it served.
    ``INSERT OR IGNORE`` on (stage, fingerprint) keeps that first stamp, so
    restarts on unchanged code cost nothing.

    Before this, the stamp came from whichever refit first noticed, and the
    refit runs daily: the 2026-08-25 charter batch landed at 13:49:50Z and
    was stamped 17:02:34Z, three hours and thirteen minutes of the eight-day
    wait spent on nothing but when the observer woke up.

    The fingerprint reads source from disk, so a deploy landing between this
    resident's imports and this call would be mis-stamped.  That window is
    sub-second and the standing practice is to restart after committing, but
    it is a window, not a proof.
    """

    if commit_conn is None:
        return {"state": "no_writer", "stages": {}}
    stamped: dict[str, Any] = {}
    for stage in ("P0", "P0-M", "P1"):
        try:
            stamped[stage] = serving_arithmetic.record(
                commit_conn, stage=stage, now_utc=started_at_utc
            )
        except sqlite3.Error as exc:
            commit_conn.rollback()
            stamped[stage] = f"error:{type(exc).__name__}"
    try:
        commit_conn.commit()
    except sqlite3.Error:
        commit_conn.rollback()
        return {"state": "commit_failed", "stages": stamped}
    return {"state": "ok", "at_utc": started_at_utc, "stages": stamped}


def run_hosted_sweep(script_path: str, timeout_sec: float) -> dict[str, Any]:
    """Run one sweep script to completion and report what it did.

    The subprocess boundary is deliberate: each sweep keeps its own
    connections, pragmas and crash domain, so a sweep that dies cannot take
    the resident's commit connection or its process down with it.

    The sweep gets its own process group so a timeout can kill the whole
    tree.  Every wrapper here is a shell that execs python, and killing only
    the direct child leaves that python running: measured 2026-08-26, a
    ``refit_center_width.py --commit`` outlived its sweep's timeout_killed
    receipt by thirty-seven minutes, still holding the write lock, while the
    resident had already moved on and the single-instance guard reported
    reclaiming a lock from a pid nobody was tracking.  Orphans accumulate
    one per timeout, and each one is a writer competing with the stage that
    prices the book.
    """

    def _text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value or "")

    started_at = utc_now()
    started = time.monotonic()
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            ["/bin/bash", script_path],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        with _HOSTED_SWEEP_PROCESS_LOCK:
            _HOSTED_SWEEP_PROCESSES.add(process)
        try:
            out, err = process.communicate(timeout=timeout_sec)
            returncode: int | None = process.returncode
            tail = (_text(out) + _text(err))[-600:]
            state_text = "ok" if returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            # Signal the group, not the shell.  Every wrapper execs python,
            # and reaping only the direct child leaves that python running
            # against the same database.
            _terminate_process_group(process)
            out, err = process.communicate()
            returncode = None
            tail = (_text(out) + _text(err))[-600:]
            state_text = "timeout_killed"
    except Exception as exc:  # noqa: BLE001 - the receipt is the contract
        if process is not None:
            _terminate_process_group(process)
        returncode = None
        tail = f"{type(exc).__name__}:{exc}"
        state_text = "spawn_failed"
    finally:
        if process is not None:
            with _HOSTED_SWEEP_PROCESS_LOCK:
                _HOSTED_SWEEP_PROCESSES.discard(process)
    return {
        "state": state_text,
        "returncode": returncode,
        "started_at_utc": started_at,
        "duration_sec": round(time.monotonic() - started, 3),
        "output_tail": tail.strip(),
        "calls_order_gateway": False,
        "places_orders": False,
    }
# Scopes advanced per poll.  The full sweep must finish inside the P0 freshness
# TTL (~6h) or scopes expire faster than the rotation can refresh them; with 77
# scopes and a 10-minute poll, three per poll brings the sweep to ~4.3h.
TARGET_NWP_SCOPES_PER_POLL = 3
# The catalog is not a fixed size: it grows with the number of open event days,
# and a registry that has recovered its future dates can carry 90+ scopes where
# the constant above assumed 77.  A fixed three per poll then stretches the
# sweep past the TTL and every scope expires before its turn comes round again.
#
# Raising it without a ceiling is equally wrong, but for the opposite reason.
# The rotation itself no longer blocks on capture -- that was the original
# stall, fixed by moving capture to a subprocess pool -- yet the pool is still
# bounded at OFFICIAL_NATIVE_CAPTURE_MAX_CONCURRENCY.  Advancing the cursor
# faster than the pool drains just skips scopes into their 30-minute retry
# floor, which makes coverage slower rather than faster.
# Budgeting four hours was too slow for a catalog this size, so the sweep is
# given one.  What the budget must not assume is that most scopes are cheap:
# retained archives key on the exact station set, and this rotation dispatches
# one station at a time, so no scope is ever served from another scope's
# archive.  Every scope pays for its own download of the same global fields --
# ~340MiB at the time of writing -- and a "twelve real downloads per ninety
# scopes" estimate is off by the station count.
#
# The ceiling exists for that reason.  Captures go through a pool bounded at
# OFFICIAL_NATIVE_CAPTURE_MAX_CONCURRENCY; overshooting it does not stall the
# rotation -- capture runs off-thread -- but it pushes unserved scopes into
# their 30-minute retry floor, so coverage gets slower, not faster.
TARGET_NWP_SWEEP_BUDGET_SEC = 1.0 * 3600.0
TARGET_NWP_MAX_SCOPES_PER_POLL = 20
# Wall clock, not a blocker count, is what the advance loop actually has to
# respect: a poll that overruns its interval delays the next one and the sweep
# falls behind the P0 TTL.  Guarding the clock directly lets the loop step over
# a scope whose own source is down instead of treating "blocked" as a proxy for
# "expensive" -- most blocked scopes fail on a single fast HTTP response.
TARGET_NWP_ADVANCE_BUDGET_SEC = TARGET_NWP_POLL_INTERVAL_SEC * 0.5


def _nwp_scopes_per_poll(catalog_scope_count: int) -> int:
    """Scopes to advance this poll, sized to finish a sweep inside the budget.

    Bounded below by the historical constant so a small or unknown catalog
    behaves exactly as before, and above by what the bounded capture pool can
    actually drain in one poll interval.
    """

    try:
        scopes = int(catalog_scope_count)
    except (TypeError, ValueError):
        return TARGET_NWP_SCOPES_PER_POLL
    if scopes <= 0:
        return TARGET_NWP_SCOPES_PER_POLL
    polls_in_budget = max(
        1.0, TARGET_NWP_SWEEP_BUDGET_SEC / TARGET_NWP_POLL_INTERVAL_SEC
    )
    needed = math.ceil(scopes / polls_in_budget)
    return max(
        TARGET_NWP_SCOPES_PER_POLL,
        min(TARGET_NWP_MAX_SCOPES_PER_POLL, needed),
    )
MAX_ARCHIVE_WAL_BYTES = 512 * 1024 * 1024
MIN_ARCHIVE_FREE_BYTES = 1024 * 1024 * 1024
# Warning headroom sits well above the fail-closed floor above.  The floor is
# the last line of defence and must stay low, because tripping it blocks every
# lane; but a 20+GB archive on a filesystem with single-digit free GB was
# already producing recurring ``disk I/O error`` while the guard still reported
# ``within_budget``.  These thresholds make that approach visible in the status
# file without stopping a system that is still able to commit.
WARN_ARCHIVE_FREE_BYTES = 16 * 1024 * 1024 * 1024
WARN_ARCHIVE_WAL_BYTES = 64 * 1024 * 1024
# The target SSOT grew roughly 250MiB/day over the fortnight before it hit its
# ceiling, so a gigabyte of remaining headroom is about four days' notice --
# enough to prune or raise the limit deliberately rather than discovering it
# from a pipeline that has already stopped writing.
TARGET_SSOT_WARN_REMAINING_BYTES = 1024 * 1024 * 1024
# The cash-phase authority window lapsed at 2026-08-23T00:00:00Z and took the
# whole opening chain with it, silently: the resolver's own error names a
# duplicate, the ingress reported it as a market fault, and nothing counted
# down to it beforehand.  Fourteen days is long enough that a renewal is a
# scheduled decision rather than an incident.
CASH_PHASE_AUTHORITY_WARN_REMAINING_SEC = 14 * 24 * 3600
# The three stages are one chain; a station whose stages answer to different
# policy lineages is a split book, not a degraded one.
STAGE_POLICY_LINEAGE_STAGES = ("P0", "P0-M", "P1")
# This archive's WAL stops growing at exactly 128MiB and every write then fails
# with ``disk I/O error``; the fail-closed ceiling above is never reached
# because ingest dies first.  Maintenance therefore triggers at half of the
# observed stall point, which at ~9MB/min leaves roughly seven minutes of slack
# for a busy checkpoint to be retried before the WAL can reach it.  Never raise
# this to the stall point itself: a threshold sitting on the fault is a
# threshold that can never fire in time.
WAL_MAINTENANCE_TRIGGER_BYTES = 64 * 1024 * 1024
WAL_MAINTENANCE_INTERVAL_SEC = 60.0
# Mirrors WEATHER_SERVICE_LOG_MAX_BYTES in scripts/lib/bounded_service_logs.sh.
SERVICE_LOG_MAX_BYTES = int(os.environ.get("WEATHER_SERVICE_LOG_MAX_BYTES") or 67108864)
SERVICE_LOG_PATHS = (
    ROOT / "data" / "logs" / "weather_basket_yes.out.log",
    ROOT / "data" / "logs" / "weather_basket_yes.err.log",
)
MAX_MADIS_BACKFILL_OBJECTS_PER_POLL = 12
MAX_MADIS_BACKFILL_COMPRESSED_BYTES = 96 * 1024 * 1024
_SLUG_DATE_RE = re.compile(r"-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$", re.I)


def _parse_utc(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("resident_datetime_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("resident_datetime_timezone_missing")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def is_recoverable_sqlite_io_error(exc: BaseException) -> bool:
    """Return whether a lane connection should be rebuilt after a transient I/O fault."""

    return isinstance(exc, sqlite3.OperationalError) and "disk i/o error" in str(exc).lower()


def is_target_sqlite_lock_error(exc: BaseException) -> bool:
    """Identify SQLite lock contention without treating it as an I/O fault."""

    if not isinstance(exc, sqlite3.OperationalError):
        return False
    reason = str(exc).lower()
    return any(marker in reason for marker in (
        "database is locked",
        "database table is locked",
        "database schema is locked",
    ))


def target_result_has_sqlite_lock_contention(result: Mapping[str, Any]) -> bool:
    """Inspect only current-cycle result fields for a transient DB lock.

    Durable historical blocked decisions are deliberately excluded: a past
    lock is audit evidence, not a reason to keep delaying a healthy worker.
    """

    paths = (
        ("reason",),
        ("market_ingress", "reason"),
        ("post_fill_recompute", "reason"),
        ("post_cycle_resource_guard", "reason"),
    )
    for path in paths:
        value: Any = result
        for key in path:
            if not isinstance(value, Mapping):
                value = None
                break
            value = value.get(key)
        if is_target_sqlite_lock_error(sqlite3.OperationalError(str(value or ""))):
            return True
    return False


def configure_target_worker_sqlite_lock_budget(
    worker: TargetProductionWorker,
) -> dict[str, Any]:
    """Set a resident-only busy budget; this performs no schema mutation.

    The worker already owns the sole writable target connection.  Applying
    this after construction also keeps connection configuration out of the
    target SSOT and avoids changing the reusable offline worker defaults.
    """

    configured: list[str] = []
    seen: set[int] = set()
    for name in ("conn", "commit_conn"):
        connection = getattr(worker, name, None)
        if connection is None or id(connection) in seen:
            continue
        seen.add(id(connection))
        connection.execute(f"PRAGMA busy_timeout={TARGET_SQLITE_LOCK_TIMEOUT_MS}")
        configured.append(name)
    return {
        "busy_timeout_ms": TARGET_SQLITE_LOCK_TIMEOUT_MS,
        "configured_connections": configured,
        "schema_mutation": False,
    }


def target_result_has_recoverable_sqlite_io_error(
    result: Mapping[str, Any],
    *,
    forecast: bool = False,
) -> bool:
    """Recognize a serialized target-worker I/O fault without scanning history.

    The production worker normally catches the SQLite exception and returns a
    fail-closed receipt, so the executor future itself does not raise.  Only
    current-cycle reason fields are inspected; durable historical decisions
    embedded for audit must not trigger endless connection rebuilds.
    """

    paths = (
        ("resident_nwp_capture", "reason"),
        ("forward_taf_capture", "reason"),
    ) if forecast else (
        ("reason",),
        ("market_ingress", "reason"),
        ("post_fill_recompute", "reason"),
    )
    for path in paths:
        value: Any = result
        for key in path:
            if not isinstance(value, Mapping):
                value = None
                break
            value = value.get(key)
        reason = str(value or "").lower()
        if (
            "disk i/o error" in reason
            or any(marker in reason for marker in TARGET_WAL_REOPEN_REASONS)
        ):
            return True
    return False


def archive_storage_guard(db_path: Path) -> dict[str, Any]:
    """Fail closed before archive WAL or filesystem exhaustion."""

    database = db_path.expanduser().resolve()
    wal = Path(str(database) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    free_bytes = int(shutil.disk_usage(database.parent).free)
    violations: list[str] = []
    if wal_bytes > MAX_ARCHIVE_WAL_BYTES:
        violations.append("archive_wal_bytes_exceeded")
    if free_bytes < MIN_ARCHIVE_FREE_BYTES:
        violations.append("archive_free_bytes_below_minimum")
    # Warnings never gate a lane.  They exist so that shrinking headroom is
    # visible before it becomes an I/O fault, instead of the guard reporting
    # "within_budget" while ingest is already failing.
    warnings: list[str] = []
    if not violations:
        if free_bytes < WARN_ARCHIVE_FREE_BYTES:
            warnings.append("archive_free_bytes_low_headroom")
        if wal_bytes >= WARN_ARCHIVE_WAL_BYTES:
            warnings.append("archive_wal_bytes_growing")
    if violations:
        state = "blocked_fail_closed"
    elif warnings:
        state = "within_budget_low_headroom"
    else:
        state = "within_budget"
    return {
        "ok": not violations,
        "database": str(database),
        "wal_bytes": wal_bytes,
        "max_wal_bytes": MAX_ARCHIVE_WAL_BYTES,
        "warn_wal_bytes": WARN_ARCHIVE_WAL_BYTES,
        "free_bytes": free_bytes,
        "min_free_bytes": MIN_ARCHIVE_FREE_BYTES,
        "warn_free_bytes": WARN_ARCHIVE_FREE_BYTES,
        "violations": violations,
        "warnings": warnings,
        "state": state,
    }


def target_ssot_capacity_guard(db_path: Path) -> dict[str, Any]:
    """Headroom against the target SSOT's own size ceiling.

    ``archive_storage_guard`` watches the archive's WAL and the filesystem,
    which is a different database and a different limit.  Nothing published
    the one boundary that actually stops the pipeline: on 2026-08-17 the
    target SSOT passed ``MAX_TARGET_DB_BYTES`` by six megabytes, every writer
    fail-closed on ``target_database_bytes_exceeded``, and the status file
    still said ``storage_guard: ok`` -- because it was reading the archive.
    A ceiling that gates ingest has to be visible while there is still room
    to act, so this reports remaining bytes on every probe and warns before
    the limit rather than after.
    """

    database = db_path.expanduser().resolve()
    db_bytes = int(database.stat().st_size) if database.is_file() else 0
    wal = Path(str(database) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    remaining = MAX_TARGET_DB_BYTES - db_bytes
    violations: list[str] = []
    warnings: list[str] = []
    if remaining <= 0:
        violations.append("target_database_bytes_exceeded")
    elif remaining < TARGET_SSOT_WARN_REMAINING_BYTES:
        warnings.append("target_database_headroom_low")
    if violations:
        state = "blocked_fail_closed"
    elif warnings:
        state = "within_budget_low_headroom"
    else:
        state = "within_budget"
    return {
        "ok": not violations,
        "database": str(database),
        "database_bytes": db_bytes,
        "max_database_bytes": MAX_TARGET_DB_BYTES,
        "remaining_bytes": remaining,
        "warn_remaining_bytes": TARGET_SSOT_WARN_REMAINING_BYTES,
        "wal_bytes": wal_bytes,
        "violations": violations,
        "warnings": warnings,
        "state": state,
    }


def cash_phase_authority_window_guard(db_path: Path) -> dict[str, Any]:
    """Time left on the cash-phase authority window every station resolves through.

    ``target_ssot_capacity_guard`` above exists because a ceiling that gates
    ingest has to be visible while there is still room to act.  This is the
    same lesson applied to a deadline instead of a size: the window that
    authorises every station's cash phase expired at 2026-08-23T00:00:00Z,
    thirty-one days after the only generation the table had ever held, and the
    first sign of it was the entire opening chain fail-closed on
    ``cash_phase_authority_manifest_not_unique`` -- a name that describes two
    rows, not the zero rows that were actually there.

    Reports the narrowest window across stations, because one lapsed station
    is one station that cannot open.
    """

    database = db_path.expanduser().resolve()
    if not database.is_file():
        return {"ok": False, "state": "cash_phase_authority_database_missing"}
    now = datetime.now(timezone.utc)
    now_utc = now.isoformat().replace("+00:00", "Z")
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = conn.execute(
            "SELECT station_id,authority_scope,valid_until_utc"
            " FROM basket_cash_phase_authority_manifest"
            " WHERE valid_from_utc <= ? AND valid_until_utc > ?",
            (now_utc, now_utc),
        ).fetchall()
    finally:
        conn.close()

    by_station: dict[str, str] = {}
    duplicated: list[str] = []
    for station_id, _scope, valid_until_utc in rows:
        station = str(station_id)
        if station in by_station:
            duplicated.append(station)
            continue
        by_station[station] = str(valid_until_utc)

    violations: list[str] = []
    warnings: list[str] = []
    if not by_station:
        violations.append("cash_phase_authority_window_lapsed")
    if duplicated:
        violations.append("cash_phase_authority_window_ambiguous")

    remaining_sec: float | None = None
    expires_at_utc = ""
    expires_station = ""
    if by_station:
        expires_station, expires_at_utc = min(
            by_station.items(), key=lambda item: item[1]
        )
        try:
            expiry = datetime.fromisoformat(expires_at_utc.replace("Z", "+00:00"))
        except ValueError:
            violations.append("cash_phase_authority_window_unreadable")
        else:
            remaining_sec = (expiry - now).total_seconds()
            if remaining_sec < CASH_PHASE_AUTHORITY_WARN_REMAINING_SEC:
                warnings.append("cash_phase_authority_window_expiring")

    if violations:
        state = "blocked_fail_closed"
    elif warnings:
        state = "within_window_expiring"
    else:
        state = "within_window"
    return {
        "ok": not violations,
        "database": str(database),
        "station_count": len(by_station),
        "earliest_expiry_station_id": expires_station,
        "earliest_expiry_utc": expires_at_utc,
        "remaining_seconds": remaining_sec,
        "remaining_days": (
            round(remaining_sec / 86400.0, 2) if remaining_sec is not None else None
        ),
        "warn_remaining_seconds": CASH_PHASE_AUTHORITY_WARN_REMAINING_SEC,
        "violations": violations,
        "warnings": warnings,
        "state": state,
        "renewal_command": (
            "scripts/renew_cash_phase_authority_window.py"
            " --valid-from <old valid_until> --valid-until <new end>"
        ),
    }


def _fleet_regime_status_probe() -> dict[str, Any]:
    """The fleet drift light, read on its own read-only connection.

    Read-only and separate from the resident's commit connection on purpose:
    this is a status probe on the same five-second cadence as the capacity
    guards, and nothing about publishing a light should be able to touch the
    transaction that prices the book.
    """

    from bayes_basket_target.fleet_regime_shift import (  # noqa: PLC0415
        fleet_regime_status,
    )

    database = DEFAULT_TARGET_DB.expanduser().resolve()
    if not database.is_file():
        return {"ok": False, "state": "fleet_regime_database_missing"}
    now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn = sqlite3.connect(
        f"file:{database}?mode=ro", uri=True, timeout=30.0
    )
    try:
        status = fleet_regime_status(conn, now_utc=now_utc)
    finally:
        conn.close()
    status["ok"] = True
    return status


def stage_policy_lineage_guard(db_path: Path) -> dict[str, Any]:
    """Whether a station's three stages still share one policy lineage.

    The three stages are one chain and one book, so they have to be governed by
    one strategy version and one generation of the same prior.  Nothing enforced
    that: the activation table has no cross-stage constraint,
    ``resolve_station_policy`` looks up one stage at a time and never glances at
    its siblings, and the ingress does not check that a packet carried all three.
    So the 2026-07-30 target-v2 rollout landed P0 and P1, left P0-M on target-v1,
    and the chain ran split for the better part of a month -- silently, because a
    station whose P0-M envelope arrived before its preselection simply produced
    no plan at all.  Four to eight stations a day, twenty-one on 2026-08-23, and
    the totals still read like a healthy 41 of 45.

    A split is reported as a violation rather than a warning.  There is no safe
    way to keep opening on a chain whose middle stage answers to a different
    policy than the ends.
    """

    database = db_path.expanduser().resolve()
    if not database.is_file():
        return {"ok": False, "state": "stage_policy_database_missing"}
    now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = conn.execute(
            "SELECT station_id,stage,strategy_version,generation,global_prior_id"
            " FROM basket_policy_activation"
            " WHERE valid_from_utc <= ? AND valid_until_utc > ?",
            (now_utc, now_utc),
        ).fetchall()
    finally:
        conn.close()

    # The resolver takes the highest generation among the rows that match, so
    # comparing anything else would report a disagreement it would never act on.
    winner: dict[tuple[str, str], tuple[str, int, str]] = {}
    for station_id, stage, strategy_version, generation, global_prior_id in rows:
        key = (str(station_id), str(stage))
        candidate = (str(strategy_version), int(generation), str(global_prior_id))
        current = winner.get(key)
        if current is None or candidate[1] > current[1]:
            winner[key] = candidate

    stations = sorted({key[0] for key in winner})
    split: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    for station_id in stations:
        lineages = {}
        missing = []
        for stage in STAGE_POLICY_LINEAGE_STAGES:
            found = winner.get((station_id, stage))
            if found is None:
                missing.append(stage)
            else:
                lineages[stage] = found
        if missing:
            incomplete.append({"station_id": station_id, "missing_stages": missing})
            continue
        if len(set(lineages.values())) != 1:
            split.append(
                {
                    "station_id": station_id,
                    "by_stage": {
                        stage: {
                            "strategy_version": value[0],
                            "generation": value[1],
                            "global_prior_id": value[2],
                        }
                        for stage, value in lineages.items()
                    },
                }
            )

    violations: list[str] = []
    if not stations:
        violations.append("stage_policy_lineage_absent")
    if incomplete:
        violations.append("stage_policy_lineage_incomplete")
    if split:
        violations.append("stage_policy_lineage_split")
    return {
        "ok": not violations,
        "database": str(database),
        "station_count": len(stations),
        "stages": list(STAGE_POLICY_LINEAGE_STAGES),
        "split_station_count": len(split),
        "incomplete_station_count": len(incomplete),
        # Bounded: the shape of a split is the same for every station that has
        # it, and the count above is what says how wide it is.
        "split_examples": split[:5],
        "incomplete_examples": incomplete[:5],
        "violations": violations,
        "state": "blocked_fail_closed" if violations else "one_lineage",
    }


def bounded_service_log_maintenance(log_paths: Sequence[Path]) -> dict[str, Any]:
    """Bound the resident's own logs without an rlimit.

    This replaces the `ulimit -f` that the launcher used to set.  RLIMIT_FSIZE
    applied to every file the process wrote, so it capped SQLite's WAL at the
    same 128MiB and turned ordinary ingest into `disk I/O error`.  Trimming the
    specific log paths here keeps the same size boundary while leaving database
    writes unconstrained.
    """

    trimmed: list[str] = []
    for log_path in log_paths:
        try:
            if not log_path.is_file():
                continue
            if log_path.stat().st_size <= SERVICE_LOG_MAX_BYTES:
                continue
            # Truncate in place rather than rotating: launchd holds these fds
            # open, so a rename would leave the service writing to an unlinked
            # inode and the visible log permanently empty.
            with log_path.open("r+b") as handle:
                handle.truncate(0)
            trimmed.append(str(log_path))
        except OSError:
            # Log hygiene must never stop ingest.
            continue
    return {"ok": True, "trimmed": trimmed, "max_bytes": SERVICE_LOG_MAX_BYTES}


def archive_wal_checkpoint_maintenance(conn: sqlite3.Connection) -> dict[str, Any]:
    """Reclaim the archive WAL before it grows into a hard I/O fault.

    ``wal_autocheckpoint`` cannot keep this database bounded.  The resident
    holds several long-lived connections against a 20+GB archive, so a reader
    almost always pins an old snapshot and the passive checkpointer can never
    recycle the head of the WAL -- it only ever appends.  Observed growth is
    roughly 14MB/minute, which reaches the fail-closed ceiling in well under an
    hour and, before that, produced a recurring ``disk I/O error`` that stalled
    every lane until the WAL was truncated by hand.

    ``TRUNCATE`` is attempted rather than ``PASSIVE`` because only a truncating
    checkpoint returns the file to zero; a passive one leaves the file at its
    high-water mark.  A busy result is an expected, non-fatal outcome: readers
    are active, so the attempt is simply retried on the next interval.  Failures
    are reported, never raised, because WAL maintenance must not become a new
    way to stop ingest.
    """

    started = time.monotonic()
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "state": "checkpoint_failed",
            "mode": "TRUNCATE",
            "reason": f"{type(exc).__name__}:{exc}",
            "at": utc_now(),
        }
    busy, log_frames, checkpointed_frames = (
        (int(row[0]), int(row[1]), int(row[2])) if row is not None else (1, -1, -1)
    )
    return {
        # A busy checkpoint reclaimed nothing but broke nothing either.
        "ok": busy == 0,
        "state": "checkpointed" if busy == 0 else "busy_readers_retry_next_interval",
        "mode": "TRUNCATE",
        "busy": busy,
        "log_frames": log_frames,
        "checkpointed_frames": checkpointed_frames,
        "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
        "at": utc_now(),
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
    last_error: OSError | None = None
    for attempt in range(4):
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_error = exc
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt < 3:
                time.sleep(0.05 * (attempt + 1))
    # Status is telemetry, not an execution authorization boundary.  A
    # transient disk/atomic-replace error must not take down the market/source
    # workers; the next heartbeat retries the write.
    print(f"[weather_basket_yes] status_write_deferred:{type(last_error).__name__}:{last_error}", file=sys.stderr)


FORECAST_PHYSICS_REFRESH_SECONDS = 6 * 60 * 60
CALIBRATION_HEALTH_INTERVAL_SECONDS = 6 * 60 * 60


def _audit_calibration_health(
    worker: TargetProductionWorker,
) -> dict[str, Any]:
    """Re-read what the two served tables claim, at most once per interval.

    Serving is a lookup and a lookup cannot notice that its own row went
    stale.  Every calibration defect found on 2026-08-15 -- a spread pinned
    at a bound already raised, a weight row six days behind the correction
    rows beside it -- was invisible until somebody asked by hand.  This is
    that question, asked on a timer.

    Pure diagnosis: read-only handle, findings recorded on the status file,
    nothing blocked and nothing written.  A failure here is reported, never
    raised, because an audit must not be able to stop the thing it audits.
    """

    now_mono = time.monotonic()
    due = float(getattr(worker, "_next_calibration_health_mono", 0.0) or 0.0)
    if now_mono < due:
        previous = getattr(worker, "_last_calibration_health_state", None)
        return dict(previous) if isinstance(previous, Mapping) else {
            "state": "not_due",
            "next_in_seconds": round(due - now_mono, 1),
            "calls_order_gateway": False,
            "places_orders": False,
        }
    setattr(
        worker,
        "_next_calibration_health_mono",
        now_mono + CALIBRATION_HEALTH_INTERVAL_SECONDS,
    )
    started = utc_now()
    try:
        from scripts.audit_native_calibration_health import (
            check_bias_stability,
            check_spread_bound,
            check_table_freshness,
            _live_activations,
        )

        conn = sqlite3.connect(f"file:{DEFAULT_TARGET_DB}?mode=ro", uri=True)
        try:
            live = _live_activations(conn, started)
            findings = (
                check_spread_bound(live)
                + check_table_freshness(conn, live)
                + check_bias_stability(conn, live)
            )
        finally:
            conn.close()
        by_check: dict[str, int] = {}
        for row in findings:
            key = str(row.get("check") or "unknown")
            by_check[key] = by_check.get(key, 0) + 1
        state = {
            "schema": "native_calibration_health_v1",
            "state": "audited",
            "checked_at_utc": started,
            "live_scope_count": sum(
                1 for key in live if key[3] in ("h00_12", "h12_24", "h24_48")
            ),
            "finding_count": len(findings),
            "by_check": by_check,
            # Bounded on the status file; the full list is one CLI run away.
            "findings": findings[:40],
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
        }
    except Exception as exc:  # noqa: BLE001 -- an audit never blocks
        state = {
            "schema": "native_calibration_health_v1",
            "state": "audit_failed",
            "checked_at_utc": started,
            "reason": f"{type(exc).__name__}:{str(exc)[:200]}",
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
        }
    setattr(worker, "_last_calibration_health_state", state)
    return state


def _refresh_forecast_physics(
    worker: TargetProductionWorker,
) -> dict[str, Any]:
    """Capture the models' forward radiation/wind, at most once per interval.

    Bounded, isolated and best effort: it writes only the reference-covariate
    table, never truth, never a probability authority, and a failure is
    recorded rather than raised so the rest of this cycle proceeds.
    """

    now_mono = time.monotonic()
    due = float(getattr(worker, "_next_forecast_physics_mono", 0.0) or 0.0)
    if now_mono < due:
        previous = getattr(worker, "_last_forecast_physics_state", None)
        return dict(previous) if isinstance(previous, Mapping) else {
            "state": "not_due",
            "next_in_seconds": round(due - now_mono, 1),
            "calls_order_gateway": False,
            "places_orders": False,
        }
    setattr(
        worker,
        "_next_forecast_physics_mono",
        now_mono + FORECAST_PHYSICS_REFRESH_SECONDS,
    )
    started = utc_now()
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "capture_station_forecast_physics.py"),
                "--mode", "forward",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
        if completed.returncode != 0:
            raise OSError(
                "forecast_physics_exit_"
                f"{completed.returncode}:"
                + completed.stderr.decode("utf-8", "replace")[:200]
            )
        receipt = json.loads(
            completed.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
        )
        state = {
            "state": "captured",
            "started_at_utc": started,
            "completed_at_utc": utc_now(),
            "station_count": receipt.get("station_count"),
            "captured": receipt.get("captured"),
            "table_rows": receipt.get("table_rows"),
            "reference_only": True,
            "writes_truth_table": False,
            "calls_order_gateway": False,
            "places_orders": False,
        }
    except Exception as exc:  # noqa: BLE001 -- observational lane
        state = {
            "state": "failed_isolated",
            "started_at_utc": started,
            "reason": f"{type(exc).__name__}:{str(exc)[:200]}",
            "calls_order_gateway": False,
            "places_orders": False,
        }
    setattr(worker, "_last_forecast_physics_state", dict(state))
    return state


def run_target_forecast_refresh_cycle(
    worker: TargetProductionWorker,
) -> dict[str, Any]:
    """Refresh bounded forecast inboxes outside the decision critical path.

    This remains an internal task of the existing system-domain service; it
    is not a fourth daemon or a legacy model scanner.  Network latency may
    delay this refresh receipt, but can never block P1, settlement truth,
    labels, WS, or no-live decisions.
    """

    now_mono = time.monotonic()
    nwp_state: dict[str, Any] = {
        "state": "not_due",
        "calls_order_gateway": False,
        "places_orders": False,
    }
    previous_nwp = getattr(worker, "_last_official_nwp_state", None)
    if isinstance(previous_nwp, Mapping):
        nwp_state["last_attempt"] = dict(previous_nwp)
    next_nwp = float(
        getattr(worker, "_next_official_nwp_poll_mono", 0.0)
    )
    if bool(getattr(worker, "skip_nwp_refresh", False)):
        nwp_state = {
            "state": "skipped_exact_scope_reuse",
            "reason": "reuse_materialized_probability_requested",
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
    elif now_mono >= next_nwp:
        setattr(
            worker,
            "_next_official_nwp_poll_mono",
            now_mono + TARGET_NWP_POLL_INTERVAL_SEC,
        )
        try:
            refresh = getattr(worker, "refresh_resident_nwp", None)
            if callable(refresh):
                # One call advances the rotation by exactly one scope.  At one
                # scope per poll the sweep took `scope_count * poll_interval`
                # -- 77 scopes x 10min = 12.8h against a P0 freshness TTL of
                # ~6h, so every scope's probability expired before its turn
                # came round again and the mainline fail-closed on
                # `resident_target_ws_actionable_scope_missing`.  Advancing
                # several scopes per poll shortens the sweep without raising
                # the poll frequency, which keeps the request cadence against
                # the external archives unchanged.
                advanced: list[dict[str, Any]] = []
                rotation = getattr(worker, "_nwp_rotation_state", None) or {}
                scopes_this_poll = _nwp_scopes_per_poll(
                    len(rotation.get("catalog_scope_keys") or ())
                )
                # Advance past a blocked scope rather than ending the poll on
                # it.  Stopping at the first blocker silently reinstated the
                # one-scope-per-poll sweep this budget exists to prevent, and
                # it did so through the fallback source: each scope tries its
                # official native packet first and only pays Open-Meteo when
                # that is unavailable, so an exhausted Open-Meteo daily quota
                # blocked the first such scope and ended every poll there.
                # Official captures are kicked off inside each scope's own
                # turn, so the scopes behind the blocker never even submitted
                # theirs -- on 2026-08-10 the official inbox went unwritten for
                # six hours, every packet aged past its 6h freshness, and P0
                # and P1 both fail-closed with the root cause three layers up.
                #
                # The budget below is the real constraint: a poll must not
                # overrun its interval.
                advance_deadline = (
                    time.monotonic() + TARGET_NWP_ADVANCE_BUDGET_SEC
                )
                blocked_scope_count = 0
                for _ in range(scopes_this_poll):
                    advanced.append(refresh(as_of_utc=utc_now()))
                    if str(advanced[-1].get("state") or "").startswith(
                        "blocked"
                    ):
                        blocked_scope_count += 1
                    if time.monotonic() >= advance_deadline:
                        break
                # Once the loop steps over blockers, the last receipt is an
                # arbitrary scope: reporting it would flap between blocked and
                # committed on the luck of the draw.  Represent the poll by its
                # last scope that got somewhere, so a blocked state means the
                # whole poll achieved nothing -- the condition worth paging on.
                # The per-poll counts below keep the blocked scopes visible.
                nwp_state = next(
                    (
                        receipt
                        for receipt in reversed(advanced)
                        if not str(receipt.get("state") or "").startswith(
                            "blocked"
                        )
                    ),
                    advanced[-1],
                )
                nwp_state["scopes_advanced_this_poll"] = len(advanced)
                nwp_state["scopes_per_poll_target"] = scopes_this_poll
                nwp_state["blocked_scope_count_this_poll"] = (
                    blocked_scope_count
                )
            else:
                nwp_state = {
                    "state": "not_configured",
                    "reason": "resident_nwp_refresh_missing",
                    "calls_order_gateway": False,
                    "places_orders": False,
                }
        except Exception as exc:
            nwp_state = {
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
                "calls_order_gateway": False,
                "places_orders": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        setattr(worker, "_last_official_nwp_state", dict(nwp_state))
    next_poll = float(
        getattr(worker, "_next_forward_taf_poll_mono", 0.0)
    )
    capture_state: dict[str, Any] = {
        "state": "not_due",
        "calls_order_gateway": False,
        "places_orders": False,
    }
    previous_taf = getattr(worker, "_last_forward_taf_state", None)
    if isinstance(previous_taf, Mapping):
        # ``_last_forward_taf_state`` already contains a ``last_attempt``
        # pointer after a completed poll.  Reusing the whole aggregate object
        # here would recursively nest that pointer on every not-due heartbeat
        # and eventually inflate the status receipt.  Keep only the bounded
        # attempt payload visible to acceptance and pending-worker telemetry.
        previous_attempt = previous_taf.get("last_attempt")
        if not isinstance(previous_attempt, Mapping):
            previous_attempt = previous_taf
        capture_state["last_attempt"] = dict(previous_attempt)
    if now_mono >= next_poll:
        setattr(
            worker,
            "_next_forward_taf_poll_mono",
            now_mono + TARGET_TAF_POLL_INTERVAL_SEC,
        )
        try:
            taf_scope_conn = (
                worker.commit_conn
                if getattr(worker, "commit_conn", None) is not None
                else getattr(worker, "conn", None)
            )
            if taf_scope_conn is None:
                capture_state = {
                    "state": "not_configured",
                    "calls_order_gateway": False,
                    "places_orders": False,
                }
            else:
                now = utc_now()
                scope_source = "target_local_active_market_registry"
                try:
                    registry_rows = load_active_registry_rows(taf_scope_conn)
                except Exception:
                    canonical_source = Path(
                        getattr(worker, "canonical_source_db_path", "") or ""
                    )
                    registry_rows = _canonical_live_taf_registry_rows(
                        canonical_source
                    )
                    scope_source = "canonical_live_market_registry_read_only"
                catalog = build_taf_rotation_catalog(
                    registry_rows,
                    as_of_utc=now,
                    station_ids=tuple(
                        getattr(worker, "station_allowlist", ()) or ()
                    ),
                )
                if not catalog and scope_source == "target_local_active_market_registry":
                    canonical_source = Path(
                        getattr(worker, "canonical_source_db_path", "") or ""
                    )
                    if canonical_source.expanduser().resolve().is_file():
                        canonical_rows = _canonical_live_taf_registry_rows(
                            canonical_source
                        )
                        canonical_catalog = build_taf_rotation_catalog(
                            canonical_rows,
                            as_of_utc=now,
                            station_ids=tuple(
                                getattr(worker, "station_allowlist", ()) or ()
                            ),
                        )
                        if canonical_catalog:
                            catalog = canonical_catalog
                            scope_source = "canonical_live_market_registry_read_only"
                catalog = sorted(
                    catalog,
                    key=lambda item: (
                        0 if str(item.get("station_id") or "").upper()
                        in ASOS_ARGMAX_LIVE_STATIONS else 1,
                        0 if str(item.get("day_state") or "") == "not_started" else 1,
                        str(item.get("event_date") or ""),
                        str(item.get("station_id") or ""),
                        str(item.get("settlement_timezone") or ""),
                    ),
                )
                rotation_path = Path(
                    getattr(
                        worker,
                        "taf_rotation_state_path",
                        worker.taf_inbox_path.with_name(
                            "taf_rotation_state.json"
                        ),
                    )
                )
                previous_rotation = load_taf_rotation_state(
                    rotation_path
                )
                selected, next_cursor, catalog_sha = (
                    select_taf_rotation_scopes(
                        catalog,
                        previous=previous_rotation,
                        limit=MAX_TAF_SCOPES_PER_POLL,
                    )
                )
                captures: list[dict[str, Any]] = []
                capture_failures: list[dict[str, str]] = []
                outcomes: list[dict[str, str]] = []
                for scope in selected:
                    station = scope["station_id"]
                    event_date = scope["event_date"]
                    settlement_timezone = scope[
                        "settlement_timezone"
                    ]
                    key = taf_rotation_scope_key(scope)
                    try:
                        captures.append(
                            capture_forward_awc_taf(
                                station_id=str(station),
                                event_date=str(event_date),
                                settlement_timezone=str(
                                    settlement_timezone
                                ),
                            )
                        )
                        outcomes.append({
                            "scope_key": key,
                            "state": "captured",
                            "reason": "",
                        })
                    except Exception as exc:
                        failure = {
                            "station_id": str(station),
                            "event_date": str(event_date),
                            "reason": f"{type(exc).__name__}:{exc}",
                        }
                        capture_failures.append(failure)
                        outcomes.append({
                            "scope_key": key,
                            "state": "failed_isolated",
                            "reason": failure["reason"],
                        })
                inbox = (
                    refresh_forward_taf_inbox(
                        captures=captures,
                        inbox_path=worker.taf_inbox_path,
                    )
                    if captures else None
                )
                rotation = build_taf_rotation_state(
                    catalog=catalog,
                    selected=selected,
                    next_cursor=next_cursor,
                    catalog_sha256=catalog_sha,
                    outcomes=outcomes,
                    previous=previous_rotation,
                    updated_at_utc=now,
                )
                rotation_wrote = persist_taf_rotation_state(
                    rotation_path,
                    rotation,
                )
                capture_state = {
                    "state": (
                        "captured_with_scope_failures"
                        if captures and capture_failures
                        else "captured"
                        if captures
                        else "blocked_fail_closed"
                        if capture_failures
                        else "no_active_registry_scope"
                    ),
                    "scope_source": scope_source,
                    "catalog_scope_count": len(catalog),
                    "selected_scope_count": len(selected),
                    "selected_scope_keys": [
                        taf_rotation_scope_key(scope)
                        for scope in selected
                    ],
                    "capture_count": len(captures),
                    "failure_count": len(capture_failures),
                    "failures": capture_failures,
                    "rotation_state_path": str(rotation_path),
                    "rotation_state_wrote": rotation_wrote,
                    "rotation_content_sha256": str(
                        rotation.get("content_sha256") or ""
                    ),
                    "inbox_content_sha256": str(
                        (inbox or {}).get("content_sha256") or ""
                    ),
                    "calls_order_gateway": False,
                    "places_orders": False,
                }
        except Exception as exc:
            # TAF is optional incremental evidence. Its capture failure must
            # not suppress P0-M, canonical P1, or the disjoint basket
            # hard-fact exclusion projection.
            capture_state = {
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
                "capture_count": 0,
                "failure_count": 0,
                "calls_order_gateway": False,
                "places_orders": False,
            }
        # Persist the attempt itself alongside the aggregate lane state.  The
        # resident publishes a short-lived ``pending`` marker while this
        # bounded worker is in flight; without an explicit receipt here, a
        # completed poll could briefly erase the previous ``last_attempt``
        # and make the read-only acceptance check race with the writer.
        attempt_receipt = dict(capture_state)
        capture_state["last_attempt"] = attempt_receipt
        setattr(worker, "_last_forward_taf_state", dict(capture_state))
    # The physical covariate the cooling shift reads.  It rides this same
    # bounded off-path worker rather than a separate timer: it is one bounded
    # HTTPS read per station, it must land before the day it describes is
    # traded, and a failure here must not touch P1, truth, labels, WS or any
    # decision -- exactly the contract this cycle already carries.  Once a
    # day is enough; the forecast for a day two days out does not move
    # faster than the capture window.
    forecast_physics_state = _refresh_forecast_physics(worker)
    # Same isolated off-path lane, same contract: read-only, bounded, and
    # unable to block anything it looks at.
    calibration_health_state = _audit_calibration_health(worker)
    return {
        "schema": "reported_trajectory_forecast_prefetch_v1",
        "resident_nwp_capture": nwp_state,
        "forecast_physics_capture": forecast_physics_state,
        "calibration_health": calibration_health_state,
        # ICON had its own receipt here while it had its own capture pass.  It
        # is now one of the four families the native capture reads in a single
        # run, so ``resident_nwp_capture`` below is its instrument; a separate
        # key would report an empty dict forever and read as a broken lane.
        "forward_taf_capture": capture_state,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def resident_nwp_in_flight_receipt(
    worker: TargetProductionWorker,
    *,
    scheduled_at_utc: str,
) -> dict[str, Any]:
    """Publish a real persisted rotation while bounded network work is active.

    A launchd restart must not erase scheduler admission evidence until a
    potentially slow provider request returns.  The rotation was validated
    when ``TargetProductionWorker`` loaded it; this receipt never claims that
    the new capture completed.
    """

    rotation = getattr(worker, "_nwp_rotation_state", None)
    configured = list(getattr(worker, "station_allowlist", ()) or ())
    if not isinstance(rotation, Mapping):
        return {
            "state": "in_flight_no_prior_rotation",
            "scheduled_at_utc": scheduled_at_utc,
            "station_scope_mode": "canonical_allowlist",
            "configured_station_ids": configured,
            "discovered_station_ids": [],
            "scope_count": 0,
            "selected_scope_count": 0,
            "successful_scope_count": 0,
            "candidate_prefetch_attempt_count": 0,
            "candidate_prefetch_scope_limit": 1,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
    catalog_keys = [
        str(value)
        for value in rotation.get("catalog_scope_keys") or ()
    ]
    discovered = sorted({
        value.split("|", 1)[0].strip().upper()
        for value in catalog_keys
        if "|" in value and value.split("|", 1)[0].strip()
    })
    outcomes = [
        value
        for value in rotation.get("latest_outcomes") or ()
        if isinstance(value, Mapping)
    ]
    candidate_attempts = min(
        1,
        sum(bool(value.get("candidate_only")) for value in outcomes),
    )
    successful = sum(
        str(value.get("state") or "").upper()
        not in {"", "BLOCKED_FAIL_CLOSED"}
        for value in outcomes
    )
    return {
        "state": "in_flight_previous_rotation",
        "scheduled_at_utc": scheduled_at_utc,
        "completion_claimed": False,
        "station_scope_mode": "canonical_allowlist",
        "configured_station_ids": configured,
        "discovered_station_ids": discovered,
        "scope_count": int(rotation.get("catalog_scope_count") or 0),
        "scope_catalog_sha256": str(
            rotation.get("catalog_sha256") or ""
        ),
        "selected_scope_count": len(
            rotation.get("latest_selected_scope_keys") or ()
        ),
        "successful_scope_count": successful,
        "candidate_prefetch_attempt_count": candidate_attempts,
        "candidate_prefetch_scope_limit": 1,
        "rotation_state": dict(rotation),
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def _canonical_live_taf_registry_rows(source_db: Path) -> list[dict[str, Any]]:
    """Project read-only AWC live scopes for optional forward TAF capture."""

    source = source_db.expanduser().resolve()
    if not source.is_file():
        return []
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_conn:
        return [
            {
                "slug": str(row[0]),
                "event_date": str(row[1]),
                "station_id": str(row[2]),
                # Carry the registry's own market settlement timezone.  This
                # projection feeds mainline forward TAF capture, whose
                # `settlement_timezone` means "the timezone this market settles
                # its daily high in" -- not the canonical ET event day.
                #
                # Overriding the ASOS argmax stations to America/New_York here
                # leaked that side lane's ET clock into mainline scope: KSEA,
                # KSFO and KLAX settle in America/Los_Angeles and KAUS, KDAL,
                # KHOU and KORD in America/Chicago, so every captured revision
                # for them failed `forward_taf_market_scope_invalid` and blocked
                # the whole production mainline.  (KATL, KLGA and KMIA settle in
                # ET anyway, which is why the override looked harmless.)  The
                # side lane keeps its own ET constant in asos_argmax_window.
                "settlement_timezone": str(row[3]),
                "settlement_unit": str(row[4]),
                "outcome": str(row[5]),
                "yes_token": str(row[6]),
                "registry_json": str(row[7]),
                "source_lane": "awc",
            }
            for row in source_conn.execute(
                """SELECT slug,event_date,station_id,settlement_timezone,
                          settlement_unit,outcome,yes_token,registry_json
                     FROM reported_trajectory_market_registry
                    WHERE source_lane='awc' AND source_eligible=1
                      AND market_active=1 AND market_accepting_orders=1
                 ORDER BY station_id,event_date,slug,outcome"""
            )
        ]


# A production cycle is submitted every 15 seconds; anything still running
# after this has missed several turns and is worth seeing as a number rather
# than as an absence.
# The stuck-cycle detector, not a performance target.  Written on
# 2026-08-16 at two minutes, when that was eight times the fifteen-second
# re-arm and a cycle that had not returned was plainly wedged.  The cycle
# has since grown into real work and now takes 440-620s end to end --
# measured 2026-08-29: forward_p0_loop 172s and probability_p0m 131s are
# 68% of a 443s round -- so 120s fired on every healthy cycle and the
# board's card sat amber permanently.  An alarm that is always on is not
# an alarm; anyone reading it stops reading it.
#
# Raised to twelve minutes: above anything a legitimate round has taken,
# below the board's own red line at 900s, so amber still means "longer
# than this cycle has ever needed" and red still means "stop waiting".
# What this number must NOT become is a place to hide cycle growth --
# the 443->620s spread is itself worth watching, and it is watched by the
# stage timings the receipt publishes, not by this bound.
PRODUCTION_CYCLE_OVERDUE_SEC = 720.0


def run_target_production_cycle(
    worker: TargetProductionWorker,
) -> dict[str, Any]:
    """Consume only durable inbox/SSOT evidence and compile no-live output."""

    started_at_utc = utc_now()
    started_monotonic = time.monotonic()
    try:
        result = worker.run()
    except sqlite3.OperationalError as exc:
        if not is_target_sqlite_lock_error(exc):
            raise
        return {
            "ok": False,
            "state": "blocked_fail_closed",
            "reason": f"sqlite_lock_contention:{type(exc).__name__}:{exc}",
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
            "target_cycle_runtime": {
                "started_at_utc": started_at_utc,
                "completed_at_utc": utc_now(),
                "elapsed_ms": round(
                    (time.monotonic() - started_monotonic) * 1000.0,
                    3,
                ),
                "sqlite_lock_contention": True,
            },
        }
    connection = getattr(worker, "commit_conn", None)
    if connection is None:
        return result
    runtime = {
        "started_at_utc": started_at_utc,
        "completed_at_utc": utc_now(),
        "elapsed_ms": round(
            (time.monotonic() - started_monotonic) * 1000.0,
            3,
        ),
        "sqlite_lock_contention": target_result_has_sqlite_lock_contention(
            result
        ),
    }
    try:
        guard = assert_connection_target_capacity(
            connection,
            recover_oversized_wal=True,
        )
    except Exception as exc:
        return {
            **result,
            "ok": False,
            "state": "blocked_fail_closed",
            "reason": f"{type(exc).__name__}:{exc}",
            "post_cycle_resource_guard": {
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
            },
            "target_cycle_runtime": runtime,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
    return {
        **result,
        "post_cycle_resource_guard": {
            "state": "PASS",
            "database": None if guard is None else str(guard),
        },
        "target_cycle_runtime": runtime,
    }


def _open(path: Path, *, ensure_schema: bool = True) -> sqlite3.Connection:
    """Open an archive connection.

    ``ensure_schema=True`` is retained for isolated tools/tests that create a
    fresh database. The resident production path uses ``_open_runtime`` below:
    schema installation belongs to the controlled cutover, never to service
    bootstrap. This is important because DDL against a shared WAL database can
    turn a harmless launchd restart into ``Bootstrap failed: 5``.
    """

    conn = sqlite3.connect(path, timeout=60.0, isolation_level=None, check_same_thread=False)
    # Copy-follow consumes named columns from plan/order rows.  Keep the
    # daemon's shared connection row shape identical to the standalone
    # copy-follow tests and workers.
    conn.row_factory = sqlite3.Row
    # Set the busy handler before any pragma that might touch the database.
    conn.execute("PRAGMA busy_timeout=60000")
    if ensure_schema:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        apply_weather_basket_yes_schema(conn)
    else:
        # The runtime already uses the cutover-installed journal mode. Do not
        # change it during bootstrap; only read the schema catalog.
        conn.execute("PRAGMA synchronous=FULL")
        status = weather_basket_yes_schema_status(conn)
        if not status["ok"]:
            missing = ",".join(str(item) for item in status["missing_tables"])
            conn.close()
            raise sqlite3.OperationalError(
                f"weather_basket_yes_schema_incomplete:{missing}"
            )
    return conn


def _open_runtime(path: Path) -> sqlite3.Connection:
    """Open a production connection without schema DDL or WAL reconfiguration."""

    return _open(path, ensure_schema=False)


def refresh_registry(conn: sqlite3.Connection, config: dict[str, Any]) -> dict[str, Any]:
    policy = config["poll_policy"]
    today = datetime.now(timezone.utc).date() - timedelta(days=1)
    discovered = discovered_slugs(
        conn,
        today=today,
        horizon_days=int(policy["market_date_horizon_days"]) + 1,
    )
    fetched: list[tuple[str, str, dict[str, Any]]] = []
    errors: list[str] = []
    discovered_by_slug = {slug: event_date for slug, event_date in discovered}

    def event_date_for_slug(slug: str, event: Mapping[str, Any]) -> str:
        known = str(discovered_by_slug.get(slug) or "").strip()[:10]
        if known:
            return known
        match = _SLUG_DATE_RE.search(slug)
        if match is not None:
            try:
                return datetime.strptime(
                    f"{match.group('month')} {match.group('day')} {match.group('year')}",
                    "%B %d %Y",
                ).date().isoformat()
            except ValueError:
                pass
        return str(event.get("endDate") or "").strip()[:10]
    end = today + timedelta(days=int(policy["market_date_horizon_days"]) + 1)
    bulk_failed = False
    try:
        try:
            gamma_timeout = float(os.environ.get("PM_GAMMA_TIMEOUT_SEC", "8"))
        except (TypeError, ValueError):
            gamma_timeout = 8.0
        try:
            configured_pages = int(
                os.environ.get(
                    "PM_REPORTED_TRAJECTORY_GAMMA_MAX_PAGES",
                    str(policy.get("market_registry_gamma_max_pages", 20)),
                )
            )
        except (TypeError, ValueError):
            configured_pages = 20
        official_events = fetch_gamma_events_window(
            start_date=today,
            end_date=end,
            timeout=max(1.0, gamma_timeout),
            # Discovery may be empty or stale on a date rollover.  Scan a
            # bounded but sufficiently wide window so newly published cities
            # cannot be hidden behind the first Gamma page.
            max_pages=min(
                4,
                max(1, configured_pages, math.ceil(len(discovered) / 100)),
            ),
        )
    except Exception as exc:
        official_events = []
        bulk_failed = True
        errors.append(f"gamma_bulk:{type(exc).__name__}")

    # Gamma pagination is best-effort.  If it failed completely, recover the
    # already-known slugs individually.  If it partially succeeded, recover
    # only the missing known slugs.  This keeps a single bad page from hiding
    # the new/previously discovered city while leaving the old registry intact
    # when all transports are unavailable.
    official_by_slug = {
        str(event.get("slug") or "").strip(): event
        for event in official_events
        if str(event.get("slug") or "").strip()
    }
    missing_slugs = [slug for slug, _event_date in discovered if slug not in official_by_slug]
    if official_events and missing_slugs or bulk_failed and discovered:
        fallback_targets = missing_slugs if official_events else [slug for slug, _event_date in discovered]

        def fetch_known(item: tuple[str, str]) -> tuple[str, str, dict[str, Any] | None, str | None]:
            slug, known_date = item
            try:
                event = fetch_gamma_event(slug, timeout=max(1.0, gamma_timeout))
                return slug, known_date, event, None
            except Exception as exc:
                return slug, known_date, None, f"{slug}:gamma_slug:{type(exc).__name__}"

        targets = [(slug, discovered_by_slug[slug]) for slug in fallback_targets]
        with ThreadPoolExecutor(max_workers=max(1, min(12, len(targets)))) as fallback_executor:
            fallback_results = list(fallback_executor.map(fetch_known, targets))
        for slug, _known_date, event, error in fallback_results:
            if error:
                errors.append(error)
            elif event is not None:
                official_by_slug[slug] = event

    official_events = [official_by_slug[slug] for slug in sorted(official_by_slug)]
    if not official_events:
        raise RuntimeError(
            "market_registry_official_fetch_empty"
            + (f":{','.join(errors)}" if errors else "")
        )
    for event in official_events:
        slug = str(event.get("slug") or "")
        event_date = event_date_for_slug(slug, event)
        if event_date:
            fetched.append((slug, event_date, event))
    rows: list[dict[str, Any]] = []
    for _slug, event_date, event in fetched:
        try:
            rows.extend(materialize_event_registry(event, event_date=event_date, config=config))
        except Exception as exc:
            errors.append(f"{_slug}:materialize:{type(exc).__name__}")
    if not rows or not any(row.get("source_eligible") for row in rows):
        raise RuntimeError("market_registry_materialization_empty_or_ineligible")
    refreshed_at = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS weather_market_discovery (
                slug TEXT NOT NULL,
                event_date TEXT NOT NULL,
                title TEXT,
                market_open_at TEXT,
                first_seen_at TEXT NOT NULL,
                first_tokenized_at TEXT,
                token_count INTEGER NOT NULL DEFAULT 0,
                status TEXT,
                details_json TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (slug,event_date)
            )"""
        )
        committed = commit_registry_rows(conn, rows, refreshed_at=refreshed_at)
        for slug, event_date, event in fetched:
            market_rows = [row for row in event.get("markets") or [] if isinstance(row, dict)]
            token_count = sum(
                bool(str(row.get("conditionId") or "").strip())
                and bool(row.get("clobTokenIds"))
                for row in market_rows
            )
            conn.execute(
                """INSERT INTO weather_market_discovery(
                    slug,event_date,title,market_open_at,first_seen_at,first_tokenized_at,
                    token_count,status,details_json,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(slug,event_date) DO UPDATE SET
                    title=COALESCE(NULLIF(excluded.title,''),weather_market_discovery.title),
                    market_open_at=COALESCE(excluded.market_open_at,weather_market_discovery.market_open_at),
                    first_tokenized_at=CASE WHEN excluded.token_count>0
                        THEN COALESCE(weather_market_discovery.first_tokenized_at,excluded.first_tokenized_at)
                        ELSE weather_market_discovery.first_tokenized_at END,
                    token_count=MAX(COALESCE(weather_market_discovery.token_count,0),excluded.token_count),
                    status=excluded.status,details_json=excluded.details_json,updated_at=excluded.updated_at
                  WHERE weather_market_discovery.title IS NOT excluded.title
                     OR weather_market_discovery.market_open_at
                        IS NOT excluded.market_open_at
                     OR COALESCE(weather_market_discovery.token_count,0)
                        IS NOT excluded.token_count
                     OR weather_market_discovery.status IS NOT excluded.status
                     OR weather_market_discovery.details_json
                        IS NOT excluded.details_json""",
                (
                    slug,
                    event_date,
                    str(event.get("title") or slug),
                    str(event.get("startDate") or event.get("createdAt") or ""),
                    refreshed_at,
                    refreshed_at if token_count else None,
                    token_count,
                    "tokenized" if token_count else "missing_tokens",
                    json.dumps({"source": "weather_basket_yes_registry_refresh", "active": event.get("active"), "closed": event.get("closed")}, sort_keys=True),
                    refreshed_at,
                ),
            )
        if "awc" not in config.get("production_source_allowlist", []):
            # AWC rows are retained as historical provenance, but when the
            # lane is disabled they must not remain production-eligible.
            conn.execute(
                """UPDATE reported_trajectory_market_registry
                   SET source_eligible=0,
                       source_block_reason=CASE
                         WHEN COALESCE(source_block_reason,'')='' THEN 'awc_production_disabled'
                         WHEN instr(','||source_block_reason||',', ',awc_production_disabled,')=0
                           THEN source_block_reason||',awc_production_disabled'
                         ELSE source_block_reason
                       END
                   WHERE source_lane='awc' AND source_eligible=1"""
            )
        markets = {(row["slug"], row["event_date"]) for row in rows}
        eligible = {
            (row["slug"], row["event_date"])
            for row in rows
            if row.get("source_eligible")
        }
        semantic_sha256 = hashlib.sha256(
            json.dumps(
                sorted(
                    (
                        row["slug"],
                        row["event_date"],
                        row["outcome"],
                        row["rules_sha256"],
                        row.get("yes_token") or "",
                        row.get("no_token") or "",
                        bool(row.get("market_active")),
                        bool(row.get("market_accepting_orders")),
                        bool(row.get("source_eligible")),
                    )
                    for row in rows
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        conn.execute(
            """INSERT INTO reported_trajectory_market_registry_refresh_state(
                 singleton,last_successful_refresh_at,semantic_sha256,
                 official_market_count,eligible_market_count,registry_row_count
               ) VALUES(1,?,?,?,?,?)
               ON CONFLICT(singleton) DO UPDATE SET
                 last_successful_refresh_at=excluded.last_successful_refresh_at,
                 semantic_sha256=excluded.semantic_sha256,
                 official_market_count=excluded.official_market_count,
                 eligible_market_count=excluded.eligible_market_count,
                 registry_row_count=excluded.registry_row_count""",
            (
                refreshed_at,
                semantic_sha256,
                len(markets),
                len(eligible),
                len(rows),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "refreshed_at": refreshed_at,
        "discovered_market_count": len(discovered),
        "official_market_count": len(markets),
        "eligible_market_count": len(eligible),
        "registry_row_count": len(rows),
        "changed_registry_row_count": committed,
        "semantic_sha256": semantic_sha256,
        "amsc_market_count": len({(row["slug"], row["event_date"]) for row in rows if row.get("source_eligible") and row.get("source_lane") == "amsc"}),
        "awc_market_count": len({(row["slug"], row["event_date"]) for row in rows if row.get("source_eligible") and row.get("source_lane") == "awc"}),
        "blocked_market_count": len(markets - eligible),
        "errors": errors,
    }


def current_registry_summary(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT MAX(refreshed_at),
                  COUNT(DISTINCT slug||'|'||event_date),
                  COUNT(DISTINCT CASE WHEN source_eligible=1 THEN slug||'|'||event_date END),
                  COUNT(DISTINCT CASE WHEN source_eligible=1 AND source_lane='amsc' THEN slug||'|'||event_date END),
                  COUNT(DISTINCT CASE WHEN source_eligible=1 AND source_lane='awc' THEN slug||'|'||event_date END)
           FROM reported_trajectory_market_registry"""
    ).fetchone()
    if row is None or not row[0] or int(row[1] or 0) <= 0:
        return None
    refresh_row = None
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='reported_trajectory_market_registry_refresh_state'"
    ).fetchone():
        refresh_row = conn.execute(
            "SELECT last_successful_refresh_at,semantic_sha256 "
            "FROM reported_trajectory_market_registry_refresh_state "
            "WHERE singleton=1"
        ).fetchone()
    refreshed_at = str(refresh_row[0] if refresh_row else row[0])
    try:
        age_sec = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(refreshed_at.replace("Z", "+00:00"))
        ).total_seconds()
    except ValueError:
        return None
    return {
        "refreshed_at": refreshed_at,
        "registry_age_sec": round(age_sec, 3),
        "official_market_count": int(row[1] or 0),
        "eligible_market_count": int(row[2] or 0),
        "amsc_market_count": int(row[3] or 0),
        "awc_market_count": int(row[4] or 0),
        "semantic_sha256": str(refresh_row[1]) if refresh_row else "",
        "source": (
            "automatic_registry_refresh_state"
            if refresh_row
            else "existing_registry_startup_snapshot"
        ),
    }


def active_stations(conn: sqlite3.Connection, lane: str) -> list[str]:
    now = datetime.now(timezone.utc)
    dates = ((now - timedelta(days=1)).date().isoformat(), (now + timedelta(days=2)).date().isoformat())
    return [
        str(row[0])
        for row in conn.execute(
            """SELECT DISTINCT station_id FROM reported_trajectory_market_registry
               WHERE source_lane=? AND source_eligible=1 AND market_active=1
                 AND market_accepting_orders=1 AND event_date BETWEEN ? AND ?
                 AND COALESCE(station_id,'')<>'' ORDER BY station_id""",
            (lane, *dates),
        )
    ]


def awc_settlement_lookback_hours(
    conn: sqlite3.Connection,
    station_ids: list[str],
    *,
    now_utc: datetime | None = None,
    minimum_hours: float = 2.0,
    margin_hours: float = 1.0,
    maximum_hours: float = 30.0,
) -> float:
    """Cover the active market day without allowing an unbounded AWC query.

    Future market days do not widen the request.  For a market day that has
    started, the request reaches back to its registry-bound settlement-day
    start plus one release margin.  The official AWC response remains bounded
    independently by ``AWC_MAX_RESPONSE_BYTES``.
    """

    stations = sorted({str(value or "").strip().upper() for value in station_ids if str(value or "").strip()})
    minimum = max(2.0, float(minimum_hours))
    maximum = max(minimum, min(48.0, float(maximum_hours)))
    margin = max(0.0, min(2.0, float(margin_hours)))
    if not stations:
        return minimum
    placeholders = ",".join("?" for _ in stations)
    rows = conn.execute(
        f"""SELECT DISTINCT station_id,event_date,settlement_timezone
              FROM reported_trajectory_market_registry
             WHERE source_lane='awc' AND source_eligible=1
               AND market_active=1 AND market_accepting_orders=1
               AND station_id IN ({placeholders})
             ORDER BY station_id,event_date""",
        stations,
    ).fetchall()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    required = minimum
    for row in rows:
        window = settlement_day_window(str(row[1]), str(row[2] or ""))
        start = datetime.fromisoformat(window["target_day_start_utc"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(window["target_day_end_utc"].replace("Z", "+00:00"))
        if now < start or now > end + timedelta(hours=margin):
            continue
        elapsed_hours = (now - start).total_seconds() / 3600.0
        required = max(required, math.ceil(elapsed_hours + margin))
    return float(min(maximum, required))


def awc_poll_interval(config: dict[str, Any], now: datetime) -> float:
    """Burst at :00/:30 while network calls remain provider-rate-gated."""

    policy = config.get("poll_policy", {})
    phase = (now.minute % 30) * 60 + now.second + now.microsecond / 1_000_000
    burst_window = float(policy.get("awc_release_burst_window_sec", 90.0))
    if phase <= burst_window:
        return max(0.1, float(policy.get("awc_release_burst_retry_sec", 0.25)))
    return max(0.5, float(policy.get("awc_background_poll_sec", 2.0)))


def poll_asos_priority_source(
    conn: sqlite3.Connection,
    station_ids: list[str],
    *,
    now_utc: datetime | None = None,
    minimum_interval_sec: float = 900.0,
) -> dict[str, Any]:
    """Ingest bounded ASOS minute windows before canonical source selection.

    The market registry remains a station/day scope registry, not a source
    identity registry.  Only unambiguous K-prefix stations enter this lane.
    IEM remains explicitly marked as an NCEI republisher.
    """

    stations = sorted({
        str(value).strip().upper()
        for value in station_ids
        if icao_to_iem_station(str(value)) is not None
    })
    if not stations:
        return {"ok": True, "state": "no_asos_station", "scopes": []}
    latest = conn.execute(
        "SELECT MAX(updated_at) FROM reported_trajectory_asos_station_day_coverage"
    ).fetchone()
    if latest is not None and latest[0]:
        age = (
            (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
            - _parse_utc(str(latest[0]))
        ).total_seconds()
        if age < max(60.0, float(minimum_interval_sec)):
            return {
                "ok": True,
                "state": "rate_limited_cached",
                "source_age_sec": round(max(0.0, age), 3),
                "scopes": [],
            }
    placeholders = ",".join("?" for _ in stations)
    rows = conn.execute(
        f"""SELECT DISTINCT station_id,event_date,settlement_timezone
              FROM reported_trajectory_market_registry
             WHERE source_lane='awc' AND source_eligible=1
               AND market_active=1 AND market_accepting_orders=1
               AND station_id IN ({placeholders})
             ORDER BY station_id,event_date""",
        stations,
    ).fetchall()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    grouped: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for station, event_date, settlement_timezone in rows:
        scope_timezone = (
            "America/New_York"
            if str(station).upper() in ASOS_ARGMAX_LIVE_STATIONS
            else str(settlement_timezone)
        )
        window = settlement_day_window(str(event_date), scope_timezone)
        start = datetime.fromisoformat(
            str(window["target_day_start_utc"]).replace("Z", "+00:00")
        )
        end = datetime.fromisoformat(
            str(window["target_day_end_utc"]).replace("Z", "+00:00")
        )
        if now < start or now > end + timedelta(hours=30):
            continue
        through = min(now, end)
        grouped.setdefault((_iso(start), _iso(through)), []).append(
            (str(station), str(event_date), scope_timezone)
        )
    ingress = AsosOneMinuteIngress(conn)
    scope_results: list[dict[str, Any]] = []
    errors: list[str] = []
    for (start_text, end_text), scopes in sorted(grouped.items()):
        if end_text <= start_text:
            continue
        try:
            poll = fetch_asos_one_minute_temperature(
                [scope[0] for scope in scopes],
                start_utc=_parse_utc(start_text),
                end_utc=_parse_utc(end_text),
                timeout=12.0,
            )
            if poll.get("source_receipt_sha256"):
                ingested = ingress.ingest(poll)
                for station, event_date, settlement_timezone in scopes:
                    selected = ingress.refresh_station_day(
                        station_id=station,
                        event_date=event_date,
                        settlement_timezone=settlement_timezone,
                        as_of_utc=end_text,
                        source_receipt_sha256=str(poll["source_receipt_sha256"]),
                    )
                    scope_results.append({
                        "station_id": station,
                        "event_date": event_date,
                        **selected,
                    })
                conn.commit()
                scope_results.append({"ingress": ingested})
        except Exception as exc:
            conn.rollback()
            errors.append(f"{','.join(scope[0] for scope in scopes)}:{type(exc).__name__}:{exc}")
    return {
        "ok": not errors,
        "state": "available" if scope_results else "data_missing",
        "scopes": scope_results,
        "errors": errors,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }


def poll_madis_hfmetar_source(
    conn: sqlite3.Connection,
    station_ids: list[str],
    *,
    now_utc: datetime | None = None,
    minimum_interval_sec: float = 300.0,
) -> dict[str, Any]:
    """Fetch the current NOAA MADIS HF-METAR object and retain exact lineage."""

    stations = sorted({
        str(value).strip().upper()
        for value in station_ids
        if icao_to_iem_station(str(value)) is not None
    })
    if not stations:
        return {"ok": True, "state": "no_asos_station"}
    latest = conn.execute(
        "SELECT MAX(fetch_completed_at_utc) FROM reported_trajectory_madis_hfmetar_objects"
    ).fetchone()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    et_start = now.astimezone(ZoneInfo("America/New_York")).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    desired_hours: list[datetime] = []
    cursor = et_start
    while cursor <= current_hour:
        desired_hours.append(cursor)
        cursor += timedelta(hours=1)
    existing = {
        str(row[0]) for row in conn.execute(
            "SELECT DISTINCT object_hour_utc FROM reported_trajectory_madis_hfmetar_objects"
        )
    }
    missing = [hour for hour in desired_hours if _iso(hour) not in existing]
    latest_age = None
    if latest is not None and latest[0]:
        latest_age = (now - _parse_utc(str(latest[0]))).total_seconds()
    due_current_refresh = latest_age is None or latest_age >= max(
        60.0, float(minimum_interval_sec)
    )
    fetch_hours = missing[:MAX_MADIS_BACKFILL_OBJECTS_PER_POLL]
    if due_current_refresh and current_hour not in fetch_hours:
        if len(fetch_hours) >= MAX_MADIS_BACKFILL_OBJECTS_PER_POLL:
            fetch_hours[-1] = current_hour
        else:
            fetch_hours.append(current_hour)
    # An hour already seen is never revisited by the missing-hours walk, but
    # its :55 row can be published upstream only around or after the top of
    # the next hour -- exactly the row the KSFO :56 slot races for.  Refetch
    # the previous hour's object briefly after the hour turns so a
    # late-published carrier row still lands; content addressing makes an
    # unchanged object a no-op.
    if due_current_refresh and now.minute <= 5:
        previous_hour = current_hour - timedelta(hours=1)
        if previous_hour >= et_start and previous_hour not in fetch_hours:
            fetch_hours.append(previous_hour)
    if not fetch_hours:
        return {
            "ok": True, "state": "rate_limited_cached",
            "source_age_sec": round(max(0.0, float(latest_age or 0.0)), 3),
            "missing_object_hours": 0,
        }
    raw_root = ROOT / "data" / "raw" / "madis_hfmetar"
    raw_root.mkdir(parents=True, exist_ok=True)
    total_compressed = 0
    decoded_count = 0
    inserted_count = 0
    fetched_hours: list[str] = []
    errors: list[str] = []
    for hour in fetch_hours:
        try:
            fetched = fetch_madis_hfmetar_object(hour, timeout=30.0)
            payload = bytes(fetched.pop("payload"))
            total_compressed += len(payload)
            if total_compressed > MAX_MADIS_BACKFILL_COMPRESSED_BYTES:
                raise RuntimeError("madis_hfmetar_backfill_compressed_limit_exceeded")
            rows = decode_madis_hfmetar_object(payload, wanted_station_ids=stations)
            retained = raw_root / f"{fetched['compressed_sha256']}.gz"
            if not retained.exists():
                temporary = retained.with_name(f".{retained.name}.tmp.{os.getpid()}")
                temporary.write_bytes(payload)
                os.replace(temporary, retained)
            result = ingest_madis_hfmetar_object(
                conn, fetched, rows, retained_path=str(retained), committed_at_utc=utc_now()
            )
            conn.commit()
            fetched_hours.append(str(fetched["object_hour_utc"]))
            decoded_count += len(rows)
            inserted_count += int(result.get("inserted_observation_count") or 0)
        except Exception as exc:
            conn.rollback()
            errors.append(f"{_iso(hour)}:{type(exc).__name__}:{exc}")
    remaining = max(0, len(missing) - len({hour for hour in fetch_hours if hour in missing}))
    return {
        "ok": not errors,
        "state": "available" if fetched_hours else "source_error",
        "object_hours": fetched_hours,
        "decoded_observation_count": decoded_count,
        "inserted_observation_count": inserted_count,
        "compressed_bytes": total_compressed,
        "remaining_missing_object_hours": remaining,
        "errors": errors,
    }


def run_once(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    ingress: ReportedTrajectoryAwcIngress,
    lane: str,
    *,
    stations_override: list[str] | None = None,
) -> dict[str, Any]:
    stations = list(stations_override) if stations_override is not None else active_stations(conn, lane)
    if not stations:
        return {"ok": True, "source_lane": lane, "state": "no_active_stations"}
    request_started_at = utc_now()
    if lane not in set(config.get("production_source_allowlist") or []):
        raise ValueError("production_source_lane_not_allowlisted")
    if lane != "awc":
        raise ValueError("awc_canonical_ingress_source_lane_invalid")
    policy = config.get("poll_policy") or {}
    awc_hours = awc_settlement_lookback_hours(
        conn,
        stations,
        minimum_hours=float(policy.get("awc_settlement_lookback_min_hours", 2.0)),
        margin_hours=float(policy.get("awc_settlement_lookback_margin_hours", 1.0)),
        maximum_hours=float(policy.get("awc_settlement_lookback_max_hours", 30.0)),
    )
    # The :50/:55 carrier rows are the slot readings P1 races to consume
    # before the METAR lands six-seven minutes after the hour (user ruling
    # 2026-08-29).  Poll the hourly object fast while those rows are being
    # published upstream; objects run ~0.6MB compressed, so the hot window
    # costs on the order of 150MB/day.
    _madis_now_minute = datetime.now(timezone.utc).minute
    madis_priority = poll_madis_hfmetar_source(
        conn,
        stations,
        minimum_interval_sec=float(
            policy.get("madis_hfmetar_hot_poll_sec", 45.0)
            if _madis_now_minute >= 49 or _madis_now_minute <= 5
            else policy.get("madis_hfmetar_poll_interval_sec", 300.0)
        ),
    )
    asos_priority = poll_asos_priority_source(
        conn,
        stations,
        minimum_interval_sec=float(policy.get("asos_poll_interval_sec", 900.0)),
    )
    poll = dict(fetch_awc_temperature_reports(stations, hours=awc_hours, timeout=8.0))
    poll["source_lane"] = "awc"
    poll["transport"] = str(poll.get("transport") or "official_awc")
    poll["fallback_reasons"] = {}
    poll["provider_states"] = [{
        "provider_id": "global_noaa_awc_relay",
        "stations": sorted(stations),
        "state": "awc_global_primary",
        "report_count": len(poll.get("reports") or []),
        "transport": poll["transport"],
    }]
    poll["router_duration_ms"] = None
    response_completed_at = utc_now()
    result = ingress.process_source_poll(
        poll,
        request_started_at=request_started_at,
        response_completed_at=response_completed_at,
    )
    # Re-run the single selector after the AWC transaction so an incomplete
    # ASOS scope deterministically falls back to the just-materialized AWC
    # bound instead of retaining a pre-AWC blocked result.
    selector = AsosOneMinuteIngress(conn)
    post_awc_selection: list[dict[str, Any]] = []
    selection_scopes: dict[tuple[str, str], Mapping[str, Any]] = {}
    for scope in asos_priority.get("scopes") or ():
        if isinstance(scope, Mapping) and scope.get("station_id") and scope.get("event_date"):
            selection_scopes[(str(scope["station_id"]), str(scope["event_date"]))] = scope
    placeholders = ",".join("?" for _ in stations)
    for station_id, event_date in conn.execute(
        f"""SELECT DISTINCT station_id,event_date
              FROM reported_trajectory_market_registry
             WHERE source_lane='awc' AND source_eligible=1
               AND market_active=1 AND market_accepting_orders=1
               AND station_id IN ({placeholders})""",
        stations,
    ):
        selection_scopes[(str(station_id), str(event_date))] = {
            "station_id": str(station_id), "event_date": str(event_date)
        }
    for scope in selection_scopes.values():
        if not isinstance(scope, Mapping) or not scope.get("station_id"):
            continue
        selected = selector.select_canonical_bound(
            station_id=str(scope["station_id"]),
            event_date=str(scope["event_date"]),
            as_of_utc=response_completed_at,
        )
        post_awc_selection.append({
            "station_id": scope["station_id"],
            "event_date": scope["event_date"],
            **selected,
        })
    if post_awc_selection:
        conn.commit()
    # After the AWC transaction, materialize the open day's qc-eligible MADIS
    # carrier rows as SOFT_PATH evidence for P1 (soft in, hard out; user
    # ruling 2026-08-29).  Canonical capture on the main line -- the resident
    # P1 ingress reads them through the same loader as every AWC report, the
    # trajectory layer feeds them to the path state only -- and it never
    # touches the order gateway.
    try:
        asos_p1_substitutes = materialize_scheduled_asos_p1_observations(
            conn,
            stations,
        )
    except Exception as exc:
        conn.rollback()
        asos_p1_substitutes = {
            "ok": False,
            "state": "error",
            "inserted_observation_count": 0,
            "scopes": [],
            "errors": [f"{type(exc).__name__}:{exc}"],
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
    # The 10-station ASOS argmax side lane is deliberately absent here.  Its
    # entry facts and order processing own a separate worker, connection and
    # failure boundary (``AsosArgmaxEntryWorker``) so that an AWC transport or
    # SQLite fault on this canonical main line can never suppress the lane, and
    # so that the lane can never call the order gateway from the main line's
    # transaction.  Partition isolation is a charter requirement, not a
    # scheduling convenience.
    return {
        "schema": SCHEMA_VERSION,
        "ok": not poll.get("station_errors"),
        "source_lane": "awc",
        "transport": poll.get("transport"),
        "router_duration_ms": poll.get("router_duration_ms"),
        "provider_states": poll.get("provider_states") or [],
        "fallback_reasons": poll.get("fallback_reasons") or {},
        "source_deferred": bool(poll.get("source_deferred")),
        "awc_requested_hours": awc_hours,
        "asos_priority_source": asos_priority,
        "madis_hfmetar_source": madis_priority,
        "asos_p1_slot_substitutes": asos_p1_substitutes,
        "canonical_surface_selection": post_awc_selection,
        "new_observation_count": int(result.get("new_observation_count") or 0),
        "station_day_fact_updates": list(result.get("station_day_fact_updates") or []),
        "canonical_ingress": result,
        "request_started_at": request_started_at,
        "response_completed_at": response_completed_at,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
    }


def run_once_isolated(db_path: Path, config: dict[str, Any], lane: str) -> dict[str, Any]:
    """Give canonical source ingress its own connection/failure boundary."""

    conn = _open_runtime(db_path)
    try:
        return run_once(conn, config, ReportedTrajectoryAwcIngress(conn, config=config), lane)
    finally:
        conn.close()


class LaneWorker:
    """One durable SQLite connection and canonical ingress per source lane."""

    def __init__(self, db_path: Path, config: dict[str, Any], lane: str) -> None:
        self.db_path = db_path
        self.conn = _open_runtime(db_path)
        self.config = config
        self.lane = lane
        self.ingress = ReportedTrajectoryAwcIngress(self.conn, config=config)
        self.stations: list[str] = []
        self.next_station_refresh = 0.0

    def poll(self) -> dict[str, Any]:
        now = time.monotonic()
        if now >= self.next_station_refresh or not self.stations:
            self.stations = active_stations(self.conn, self.lane)
            self.next_station_refresh = now + 60.0
        return run_once(
            self.conn, self.config, self.ingress, self.lane,
            stations_override=self.stations,
        )

    def close(self) -> None:
        self.conn.close()

    def reopen(self) -> None:
        """Rebuild this lane's connection after a recoverable SQLite I/O error."""

        self.conn.close()
        self.conn = _open_runtime(self.db_path)
        self.ingress = ReportedTrajectoryAwcIngress(self.conn, config=self.config)
        self.stations = []
        self.next_station_refresh = 0.0


class RegistryRefreshWorker:
    """Refresh canonical metadata and independently publish target registry.

    Registry publication must not wait for a model release or an NWP capture
    cadence.  This worker only writes the bounded, atomically replaced JSON
    handoff; the sole target production worker remains the only target-SSOT
    writer and ingests the handoff on its next cycle.
    """

    def __init__(
        self,
        db_path: Path,
        config: dict[str, Any],
        *,
        target_registry_path: Path | None = None,
    ) -> None:
        self.db_path = db_path
        self.config = config
        self.conn = _open_runtime(db_path)
        self.target_registry_path = (
            Path(target_registry_path).expanduser().resolve()
            if target_registry_path is not None
            else (
                DEFAULT_TARGET_NWP_RUNTIME_ROOT
                / "market_registry_forward.json"
            ).resolve()
        )

    def run(self) -> dict[str, Any]:
        refreshed = refresh_registry(self.conn, self.config)
        exported_at = utc_now()
        target_export = export_shared_live_registry(
            source_db=self.db_path,
            output_path=self.target_registry_path,
            as_of_utc=exported_at,
            include_not_started_for_p0=True,
            current_catalog_only=True,
        )
        return {
            **refreshed,
            "target_registry_export": target_export,
            "target_registry_export_decoupled_from_nwp": True,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
        }

    def close(self) -> None:
        self.conn.close()

    def reopen(self) -> None:
        self.conn.close()
        self.conn = _open_runtime(self.db_path)


class AsosArgmaxWindowWorker:
    """Independent official forecast timing worker for the 10-station lane."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.conn = _open_runtime(db_path)

    def run(self) -> dict[str, Any]:
        nws = materialize_missing_argmax_windows(
            self.conn,
            sorted(ASOS_ARGMAX_LIVE_STATIONS),
            raw_root=ROOT / "data" / "raw" / "nws_hourly_argmax",
            minimum_retry_seconds=300.0,
            timeout=10.0,
        )
        try:
            taf_reference = materialize_argmax_taf_references(
                self.conn,
                inbox_path=DEFAULT_TARGET_TAF_INBOX,
                station_ids=sorted(ASOS_ARGMAX_LIVE_STATIONS),
            )
        except Exception as exc:
            taf_reference = {
                "ok": False,
                "state": "optional_reference_failed_isolated",
                "reason": f"{type(exc).__name__}:{exc}",
                "modifies_nws_window": False,
                "blocks_entry": False,
            }
        return {**nws, "taf_tx_reference": taf_reference}

    def close(self) -> None:
        self.conn.close()

    def reopen(self) -> None:
        self.conn.close()
        self.conn = _open_runtime(self.db_path)


class AsosArgmaxEntryWorker:
    """Independent entry-fact and order worker for the 10-station ASOS lane.

    This is the only resident component allowed to call the order gateway.  It
    is kept off the AWC canonical main line on purpose: the charter requires the
    side lane's state, budget, intents and receipts to be partitioned, so that a
    main-line fault blocks only the main line.  Before this worker existed the
    lane ran at the tail of the AWC ingress transaction, and a single recurring
    ``disk I/O error`` in AWC ingress silently suppressed every entry fact and
    every order for the whole event day.

    Entry materialization and order processing stay in one worker because they
    are one causal step: a newly materialized entry fact must reach the gateway
    within the same frozen argmax period that authorized it.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.conn = _open_runtime(db_path)

    def run(self) -> dict[str, Any]:
        entry = materialize_argmax_entry_facts(
            self.conn, sorted(ASOS_ARGMAX_LIVE_STATIONS)
        )
        # Order processing must still run when no new entry fact appeared this
        # cycle: prepared intents, unresolved submissions and exits from an
        # earlier cycle have to keep converging toward a reconciled state.
        execution = process_pending_argmax_orders(self.conn, root=ROOT)
        submitted = int(execution.get("submission_attempt_count") or 0)
        return {
            "ok": bool(entry.get("ok", True)) and bool(execution.get("ok", True)),
            "state": "processed",
            "entry": entry,
            "execution": execution,
            "calls_order_gateway": int(execution.get("gateway_call_count") or 0) > 0,
            "places_orders": submitted > 0,
            "orders_submitted": submitted,
        }

    def close(self) -> None:
        self.conn.close()

    def reopen(self) -> None:
        self.conn.close()
        self.conn = _open_runtime(self.db_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument(
        "--once",
        choices=("registry", "awc", "argmax", "asos-lane", "all", "production"),
        default="",
    )
    args = parser.parse_args()
    config = load_config()
    conn = _open_runtime(args.db)
    ingress = ReportedTrajectoryAwcIngress(conn, config=config)
    state: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "pid": os.getpid(),
        "started_at": utc_now(),
        "strategy_id": "awc_canonical_truth_ingress_v1",
        "production_source_allowlist": list(config.get("production_source_allowlist") or []),
        "taf_requested": True,
        "bayes_consumed": True,
        "weak_evidence_taf_capture_isolated": True,
        "weak_evidence_capture_activation_independent": True,
        "basket_consumes_materialized_probability": True,
        "basket_strategy_id": "weather_basket_yes_v2",
        "hard_no_strategy_removed": True,
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "source_results": {},
    }
    try:
        existing_registry = current_registry_summary(conn)
        # Startup never performs network I/O synchronously.  The resident
        # scheduler below refreshes Gamma in a dedicated bounded worker while
        # all hot-path consumers continue reading the last atomic registry
        # generation.
        registry = (
            refresh_registry(conn, config)
            if args.once == "registry"
            else existing_registry or {
                "state": "materialized_registry_missing_fail_closed",
                "official_market_count": 0,
                "eligible_market_count": 0,
            }
        )
        state["registry"] = registry
        if args.once:
            if args.once == "argmax":
                argmax_worker = AsosArgmaxWindowWorker(args.db)
                try:
                    state["asos_argmax_window"] = argmax_worker.run()
                finally:
                    argmax_worker.close()
                state["completed_at"] = utc_now()
                _atomic_json(args.status, state)
                print(json.dumps(state, ensure_ascii=False, sort_keys=True))
                return 0 if state["asos_argmax_window"].get("ok") else 1
            if args.once == "asos-lane":
                # This is the one diagnostic path that may reach the order
                # gateway.  `--once awc` deliberately no longer can.
                entry_worker = AsosArgmaxEntryWorker(args.db)
                try:
                    state["asos_argmax_lane"] = entry_worker.run()
                finally:
                    entry_worker.close()
                state["completed_at"] = utc_now()
                _atomic_json(args.status, state)
                print(json.dumps(state, ensure_ascii=False, sort_keys=True))
                return 0 if state["asos_argmax_lane"].get("ok") else 1
            if args.once == "production":
                commit_error = None
                try:
                    commit_conn = open_target_writer_if_enabled(
                        DEFAULT_TARGET_DB,
                        activation_path=DEFAULT_TARGET_ACTIVATION,
                    )
                except Exception as exc:
                    commit_conn = None
                    commit_error = f"target_writer_open_failed:{type(exc).__name__}:{exc}"
                worker = TargetProductionWorker(
                    DEFAULT_TARGET_DB,
                    commit_conn=commit_conn,
                    commit_error=commit_error,
                    canonical_source_db_path=args.db,
                )
                if worker.commit_error is None:
                    try:
                        configure_target_worker_sqlite_lock_budget(worker)
                    except sqlite3.Error as exc:
                        worker.commit_error = (
                            "target_writer_lock_budget_config_failed:"
                            f"{type(exc).__name__}:{exc}"
                        )
                try:
                    state["production"] = run_target_production_cycle(
                        worker
                    )
                finally:
                    worker.close()
                state["completed_at"] = utc_now()
                _atomic_json(args.status, state)
                print(json.dumps(state, ensure_ascii=False, sort_keys=True))
                return 0 if state["production"].get("ok") else 1
            lanes = tuple(config.get("production_source_allowlist") or []) if args.once == "all" else (args.once,) if args.once in {"amsc", "awc"} else ()
            for lane in lanes:
                try:
                    state["source_results"][lane] = run_once(conn, config, ingress, lane)
                except Exception as exc:
                    state["source_results"][lane] = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
            state["completed_at"] = utc_now()
            _atomic_json(args.status, state)
            print(json.dumps(state, ensure_ascii=False, sort_keys=True))
            return 0 if all(item.get("ok") for item in state["source_results"].values()) and not registry.get("errors") else 1

        stopping = False

        def stop(_signum: int, _frame: Any) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        enabled_lanes = tuple(config.get("production_source_allowlist") or [])
        executor = ThreadPoolExecutor(
            max_workers=max(4, len(enabled_lanes) * 2 + 1),
            thread_name_prefix="reported-trajectory-worker",
        )
        # Forecast network I/O is deliberately isolated from the source and
        # decision pool. A slow model publication endpoint must never consume
        # the worker needed to drain durable observations or compile a
        # production_no_live decision.
        forecast_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="reported-trajectory-forecast-prefetch",
        )
        registry_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="reported-trajectory-market-registry",
        )
        argmax_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="reported-trajectory-asos-argmax",
        )
        # The dashboard render is a READER on its own clock (2026-08-29).
        # It lived inside the hosted paper-ledger sweep from 2026-08-16 and
        # had grown to twenty minutes of read-only work holding a WRITER
        # slot; with ten-plus hosted members the queue starved production
        # for two hours on 2026-08-28.  Its own single-worker pool keeps it
        # off the writer rotation entirely, and the three-services charter
        # is preserved by hosting it here rather than as a fourth agent.
        render_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="reported-trajectory-dashboard-render",
        )
        # Hosted sweeps get their own pool now that several run at once.  The
        # shared executor above is sized for the source lanes and the
        # production cycle -- four workers with one lane enabled -- so putting
        # a concurrent sweep fan-out through it would let maintenance consume
        # the worker a decision needs.  Same reasoning that moved the dashboard
        # render onto its own pool.
        sweep_executor = ThreadPoolExecutor(
            max_workers=HOSTED_SWEEP_MAX_CONCURRENCY,
            thread_name_prefix="reported-trajectory-hosted-sweep",
        )
        # The entry/order lane gets its own single-worker pool so that a slow
        # NWS forecast fetch in the window worker can never delay an entry fact
        # that must reach the gateway inside its frozen argmax period.
        argmax_entry_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="reported-trajectory-asos-argmax-entry",
        )
        lane_workers = {lane: LaneWorker(args.db, config, lane) for lane in enabled_lanes}
        registry_worker = RegistryRefreshWorker(args.db, config)
        argmax_worker = AsosArgmaxWindowWorker(args.db)
        argmax_entry_worker = AsosArgmaxEntryWorker(args.db)
        commit_error = None
        try:
            commit_conn = open_target_writer_if_enabled(
                DEFAULT_TARGET_DB,
                activation_path=DEFAULT_TARGET_ACTIVATION,
            )
        except Exception as exc:
            commit_conn = None
            commit_error = f"target_writer_open_failed:{type(exc).__name__}:{exc}"
        production_worker = TargetProductionWorker(
            DEFAULT_TARGET_DB,
            commit_conn=commit_conn,
            commit_error=commit_error,
            canonical_source_db_path=args.db,
        )
        if production_worker.commit_error is None:
            try:
                configure_target_worker_sqlite_lock_budget(production_worker)
            except sqlite3.Error as exc:
                production_worker.commit_error = (
                    "target_writer_lock_budget_config_failed:"
                    f"{type(exc).__name__}:{exc}"
                )
        state["serving_arithmetic"] = _stamp_serving_arithmetic(
            commit_conn, started_at_utc=str(state["started_at"])
        )
        # Forecast prefetch owns network/file work only.  It receives no
        # writable SSOT handle; the production worker above is the sole
        # target database writer and ingests the atomically published
        # registry snapshot on its next cycle.
        forecast_worker = TargetProductionWorker(
            DEFAULT_TARGET_DB,
            commit_conn=None,
            commit_error=None,
            canonical_source_db_path=args.db,
            # Use the worker's frozen canonical production station scope.
            # Forward OOS is observational for production_no_live and must
            # never narrow resident acquisition to a single station.
        )
        lane_futures: dict[str, Any] = {}
        lane_recovery_attempts = {lane: 0 for lane in lane_workers}
        production_future: Any = None
        # Initialised beside the future so the in-flight publisher below can
        # read them on the very first pass.
        production_submitted_mono: float = time.monotonic()
        production_submitted_at: str = ""
        forecast_future: Any = None
        registry_future: Any = None
        argmax_future: Any = None
        argmax_entry_future: Any = None
        production_recovery_attempts = 0
        production_lock_contention_attempts = 0
        forecast_recovery_attempts = 0
        registry_recovery_attempts = 0
        argmax_recovery_attempts = 0
        argmax_entry_recovery_attempts = 0
        now_mono = time.monotonic()
        registry_refresh_sec = max(
            60.0,
            float(config["poll_policy"]["market_registry_refresh_sec"]),
        )
        registry_retry_sec = max(
            10.0,
            min(
                registry_refresh_sec,
                float(config["poll_policy"]["market_registry_retry_sec"]),
            ),
        )
        materialized_registry_poll_sec = 60.0
        next_materialized_registry_read = now_mono
        next_registry_refresh = now_mono
        next_argmax_refresh = now_mono
        next_render_refresh = now_mono + 120.0
        render_future = None
        # A MADIS HF-METAR slot lands every five minutes and an exact ASOS
        # minute observation can land at any time.  Poll the lane far more often
        # than either cadence so a new station-day high reaches the gateway well
        # inside its frozen argmax period.
        argmax_entry_interval_sec = 30.0
        next_argmax_entry = now_mono
        next_source = {lane: now_mono for lane in enabled_lanes}
        next_status_write = now_mono
        next_production = now_mono
        next_forecast = now_mono
        # Last time each writer lane finished, which is what decides whose
        # turn it is when more than one is due.  All equal at boot, so the
        # first round follows the deterministic tie order.
        writer_last_served = {
            "forecast": now_mono,
            "production": now_mono,
            "sweeps": now_mono,
        }
        # Which lane took the most recent writer slot, for the production
        # floor below: a maintenance lane may not take two in a row while
        # production is due.
        last_writer_slot = ""
        next_storage_probe = 0.0
        next_wal_maintenance = 0.0
        next_target_wal_maintenance = 0.0
        # Hosted sweeps: staggered first runs so boot does not fire all four
        # into the first idle gap at once.
        next_hosted_sweep = {
            name: now_mono + 60.0 + 90.0 * index
            for index, (name, _, _, _) in enumerate(HOSTED_SWEEPS)
        }
        # Keyed by sweep name so several may be in flight at once, at most one
        # per write domain.  ``running`` stays a list for the same reason.
        hosted_sweep_futures: "dict[str, Any]" = {}
        state["hosted_sweeps"] = {
            "schema": "resident_hosted_sweeps_v2",
            "running": [],
            "results": {},
        }
        storage_guard: dict[str, Any] = {
            "ok": False,
            "state": "storage_probe_not_run_fail_closed",
        }
        while not stopping:
            now_mono = time.monotonic()
            if now_mono >= next_storage_probe:
                try:
                    storage_guard = archive_storage_guard(args.db)
                except Exception as exc:
                    storage_guard = {
                        "ok": False,
                        "state": "storage_probe_failed_fail_closed",
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                state["storage_guard"] = storage_guard
                # Published beside it, never merged into it: the archive guard
                # gates this daemon's own ingest, while this one is the ceiling
                # the target writers hit.  Folding them into a single "ok"
                # would leave the same blind spot in a different shape.
                try:
                    state["target_ssot_capacity"] = target_ssot_capacity_guard(
                        DEFAULT_TARGET_DB
                    )
                except Exception as exc:
                    state["target_ssot_capacity"] = {
                        "ok": False,
                        "state": "target_capacity_probe_failed",
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                # A deadline gates the opening chain exactly the way the
                # ceiling above gates its writers, so it is published on the
                # same probe rather than left to be discovered as a market
                # fault after it has already fired.
                try:
                    state["cash_phase_authority_window"] = (
                        cash_phase_authority_window_guard(DEFAULT_TARGET_DB)
                    )
                except Exception as exc:
                    state["cash_phase_authority_window"] = {
                        "ok": False,
                        "state": "cash_phase_authority_probe_failed",
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                try:
                    state["stage_policy_lineage"] = stage_policy_lineage_guard(
                        DEFAULT_TARGET_DB
                    )
                except Exception as exc:
                    state["stage_policy_lineage"] = {
                        "ok": False,
                        "state": "stage_policy_lineage_probe_failed",
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                # The next-day drift light.  Published on the same probe as
                # the other guards because it answers the same class of
                # question -- is something wrong right now that nobody would
                # otherwise be told about -- and because the alert has to
                # appear even on the days the correction itself stands down.
                try:
                    state["fleet_regime"] = _fleet_regime_status_probe()
                except Exception as exc:
                    state["fleet_regime"] = {
                        "ok": False,
                        "state": "fleet_regime_probe_failed",
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                next_storage_probe = now_mono + 5.0
                # Trigger on the measured WAL size rather than on the warning
                # list: warnings are a reporting concern and are suppressed once
                # the guard blocks, but maintenance is most needed exactly then.
                if (
                    int(storage_guard.get("wal_bytes") or 0) >= WAL_MAINTENANCE_TRIGGER_BYTES
                    and now_mono >= next_wal_maintenance
                ):
                    wal_maintenance = archive_wal_checkpoint_maintenance(conn)
                    state["archive_wal_maintenance"] = wal_maintenance
                    state["service_log_maintenance"] = bounded_service_log_maintenance(
                        SERVICE_LOG_PATHS
                    )
                    next_wal_maintenance = now_mono + WAL_MAINTENANCE_INTERVAL_SEC
                # The target SSOT gets the same hygiene (2026-08-29).  A day
                # of killed rebuilds left its WAL at 1.2GB with no live
                # reader pinning it -- SQLite never shrinks a WAL without a
                # truncating checkpoint -- and every DB-bound stage slowed
                # threefold while probability_bridge_resource_gate_failed
                # stamped 34 stations at once.  Best effort at a moment the
                # writer is idle; busy readers make it a no-op retried next
                # interval, exactly like the archive's.
                if (
                    int(
                        (state.get("target_ssot_capacity") or {})
                        .get("wal_bytes") or 0
                    ) >= 256 * 1024 * 1024
                    and now_mono >= next_target_wal_maintenance
                    and production_future is None
                    and not hosted_sweep_futures
                    and forecast_future is None
                ):
                    try:
                        _twal = sqlite3.connect(str(DEFAULT_TARGET_DB), timeout=5)
                        try:
                            _twal.execute("PRAGMA busy_timeout=5000")
                            _row = _twal.execute(
                                "PRAGMA wal_checkpoint(TRUNCATE)"
                            ).fetchone()
                            state["target_wal_maintenance"] = {
                                "ok": bool(_row and int(_row[0]) == 0),
                                "result": list(_row or ()),
                                "at": utc_now(),
                            }
                        finally:
                            _twal.close()
                    except sqlite3.Error as exc:
                        state["target_wal_maintenance"] = {
                            "ok": False,
                            "reason": f"{type(exc).__name__}:{exc}",
                            "at": utc_now(),
                        }
                    next_target_wal_maintenance = (
                        now_mono + WAL_MAINTENANCE_INTERVAL_SEC
                    )
            if now_mono >= next_materialized_registry_read:
                try:
                    materialized_registry = current_registry_summary(conn)
                    if materialized_registry is None:
                        raise RuntimeError("materialized_registry_missing")
                    state["registry"] = materialized_registry
                    state.pop("registry_error", None)
                    registry_healthy = (
                        int(materialized_registry.get("official_market_count") or 0) > 0
                        and int(materialized_registry.get("eligible_market_count") or 0) > 0
                    )
                except Exception as exc:
                    state["registry_error"] = f"{type(exc).__name__}:{exc}"
                    registry_healthy = False
                state["registry_watchdog"] = {
                    "healthy": registry_healthy,
                    "mode": "automatic_official_gamma_registry",
                    "network_refresh_enabled": True,
                    "refresh_in_flight": (
                        registry_future is not None
                        and not registry_future.done()
                    ),
                    "refresh_sec": registry_refresh_sec,
                    "retry_sec": registry_retry_sec,
                    "poll_sec": materialized_registry_poll_sec,
                }
                next_materialized_registry_read = (
                    now_mono + materialized_registry_poll_sec
                )
            for lane, future in list(lane_futures.items()):
                if not future.done():
                    continue
                try:
                    lane_result = future.result()
                    state["source_results"][lane] = lane_result
                    lane_recovery_attempts[lane] = 0
                    # A newly materialized observation may be the station-day
                    # high that authorizes an entry.  Wake the side lane now
                    # instead of spending up to a full poll interval waiting.
                    if int(lane_result.get("new_observation_count") or 0) > 0:
                        next_argmax_entry = min(next_argmax_entry, time.monotonic())
                except Exception as exc:
                    result = {"ok": False, "error": f"{type(exc).__name__}:{exc}", "at": utc_now()}
                    if is_recoverable_sqlite_io_error(exc):
                        attempt = lane_recovery_attempts.get(lane, 0) + 1
                        lane_recovery_attempts[lane] = attempt
                        try:
                            lane_workers[lane].reopen()
                            result["connection_recovery"] = {
                                "ok": True,
                                "attempt": attempt,
                                "backoff_sec": SQLITE_IO_RECOVERY_BACKOFF_SEC,
                                "reason": "sqlite_disk_io_error",
                            }
                            next_source[lane] = time.monotonic() + SQLITE_IO_RECOVERY_BACKOFF_SEC
                        except Exception as recovery_exc:
                            result["connection_recovery"] = {
                                "ok": False,
                                "attempt": attempt,
                                "reason": "sqlite_disk_io_error",
                                "error": f"{type(recovery_exc).__name__}:{recovery_exc}",
                            }
                    state["source_results"][lane] = result
                del lane_futures[lane]
            if registry_future is not None and registry_future.done():
                try:
                    registry_result = registry_future.result()
                    registry_recovery_attempts = 0
                    changed_registry_rows = int(
                        registry_result.get("changed_registry_row_count") or 0
                    )
                    live_pipeline_woken = changed_registry_rows > 0
                    if live_pipeline_woken:
                        # A newly tokenized market must not wait for the
                        # independent 15-minute argmax cadence or the cached
                        # 60-second station scope.  WS catalog discovery is
                        # already content-marker driven and probes each second.
                        next_argmax_refresh = min(
                            next_argmax_refresh, time.monotonic()
                        )
                        for lane_worker in lane_workers.values():
                            lane_worker.next_station_refresh = 0.0
                    state["registry_refresh"] = {
                        "ok": True,
                        **registry_result,
                        "live_pipeline_woken": live_pipeline_woken,
                        "calls_order_gateway": False,
                        "places_orders": False,
                    }
                    next_registry_refresh = time.monotonic() + registry_refresh_sec
                except Exception as exc:
                    recovery: dict[str, Any] | None = None
                    if is_recoverable_sqlite_io_error(exc):
                        registry_recovery_attempts += 1
                        try:
                            registry_worker.reopen()
                            recovery = {
                                "ok": True,
                                "attempt": registry_recovery_attempts,
                                "reason": "sqlite_disk_io_error",
                            }
                        except Exception as recovery_exc:
                            recovery = {
                                "ok": False,
                                "attempt": registry_recovery_attempts,
                                "reason": "sqlite_disk_io_error",
                                "error": (
                                    f"{type(recovery_exc).__name__}:"
                                    f"{recovery_exc}"
                                ),
                            }
                    state["registry_refresh"] = {
                        "ok": False,
                        "state": "refresh_failed_previous_generation_retained",
                        "error": f"{type(exc).__name__}:{exc}",
                        "retry_sec": registry_retry_sec,
                        "calls_order_gateway": False,
                        "places_orders": False,
                        **({"connection_recovery": recovery} if recovery else {}),
                    }
                    next_registry_refresh = time.monotonic() + registry_retry_sec
                registry_future = None
            if render_future is not None and render_future.done():
                try:
                    state["dashboard_render"] = render_future.result()
                except Exception as exc:
                    state["dashboard_render"] = {
                        "state": "failed",
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                render_future = None
            if argmax_future is not None and argmax_future.done():
                try:
                    argmax_result = argmax_future.result()
                    argmax_recovery_attempts = 0
                    state["asos_argmax_window"] = argmax_result
                except Exception as exc:
                    argmax_recovery_attempts += 1
                    state["asos_argmax_window"] = {
                        "ok": False,
                        "state": "blocked",
                        "reason": f"{type(exc).__name__}:{exc}",
                        "attempt": argmax_recovery_attempts,
                        "calls_order_gateway": False,
                        "orders_submitted": 0,
                    }
                    try:
                        argmax_worker.reopen()
                    except Exception:
                        pass
                argmax_future = None
            if argmax_entry_future is not None and argmax_entry_future.done():
                try:
                    state["asos_argmax_lane"] = argmax_entry_future.result()
                    argmax_entry_recovery_attempts = 0
                except Exception as exc:
                    argmax_entry_recovery_attempts += 1
                    state["asos_argmax_lane"] = {
                        "ok": False,
                        "state": "blocked",
                        "reason": f"{type(exc).__name__}:{exc}",
                        "attempt": argmax_entry_recovery_attempts,
                        "calls_order_gateway": False,
                        "places_orders": False,
                        "orders_submitted": 0,
                    }
                    # Rebuild the lane connection on any fault, including a
                    # transient SQLite I/O error, so the side lane recovers on
                    # its own schedule instead of waiting for a main-line cycle.
                    try:
                        argmax_entry_worker.reopen()
                    except Exception:
                        pass
                argmax_entry_future = None
            # A cycle that raises lands in state["production"] below.  One
            # that simply does not return lands nowhere: the key stays absent
            # and the daemon looks idle while a worker thread is wedged.
            # Measured 2026-08-15: eighteen minutes with a fresh heartbeat, no
            # "production" key at all, and no way to tell a slow cycle from a
            # stuck one.  Publish the wait itself.
            if production_future is not None and not production_future.done():
                production_overdue = (
                    time.monotonic() - production_submitted_mono
                ) > PRODUCTION_CYCLE_OVERDUE_SEC
                state["production_inflight"] = {
                    "submitted_at_utc": production_submitted_at,
                    "elapsed_sec": round(
                        time.monotonic() - production_submitted_mono, 1
                    ),
                    "overdue": production_overdue,
                    "overdue_after_sec": PRODUCTION_CYCLE_OVERDUE_SEC,
                }
                # Publishing the wait told us a cycle was wedged but never on
                # what.  Twice the answer took rounds of ``sample`` plus
                # hand-timed candidate queries, because a native stack says
                # only that SQLite is reading pages.  The cycle thread is
                # inside one statement while this thread keeps writing, so
                # ask the probe which one.  Only once the cycle is already
                # overdue: a healthy cycle has nothing to name.
                if production_overdue:
                    try:
                        state["production_inflight"]["statement_probe"] = (
                            statement_probe_snapshot()
                        )
                    except Exception as exc:  # diagnosis must not stop a cycle
                        state["production_inflight"]["statement_probe"] = {
                            "error": f"{type(exc).__name__}:{exc}",
                        }
            elif production_future is None:
                state.pop("production_inflight", None)
            # The P0-M training lane's verdict, which is the only thing that
            # can end that stage's silence.  It runs every cycle for one due
            # station and says exactly what it is short of; without this it
            # said so only to a receipt file nobody reads.
            # ...off the worker that actually runs it.  The lane lives in
            # ``refresh_resident_nwp``, which this daemon submits against
            # ``forecast_worker``, not the decision-path one -- so reading the
            # attribute from ``production_worker`` found nothing every cycle
            # and the key stayed absent, which is the same silence this was
            # written to end.
            taf_training = getattr(
                forecast_worker, "_taf_revision_training_state", None
            ) or getattr(
                forecast_worker, "_taf_revision_last_result", None
            )
            if taf_training:
                state["taf_revision_training"] = taf_training
            # And the model-capture lane's, for the same reason.  Every poll
            # decides which published run a scope should hold, whether the
            # packet in hand is superseded, and what the capture returned --
            # then stored the answer on the worker and published none of it.
            # "The four models are twelve hours old" was therefore a question
            # the page could not answer: the rotation counters it did carry
            # say a scope was visited, not which run it came back with.
            nwp_cycle = getattr(
                forecast_worker, "_last_official_nwp_state", None
            )
            if nwp_cycle:
                state["official_nwp_cycle"] = {
                    key: nwp_cycle[key]
                    for key in (
                        "state",
                        "reason",
                        "station_id",
                        "event_date",
                        "source_role",
                        "official_probe_reason",
                        "official_native_capture",
                        "official_packet_reused",
                        "scopes_advanced_this_poll",
                        "blocked_scope_count_this_poll",
                    )
                    if key in nwp_cycle
                }
            if production_future is not None and production_future.done():
                try:
                    production_result = production_future.result()
                    if target_result_has_recoverable_sqlite_io_error(
                        production_result
                    ):
                        production_recovery_attempts += 1
                        try:
                            production_worker.reopen_target_connections()
                            configure_target_worker_sqlite_lock_budget(
                                production_worker
                            )
                            production_result["connection_recovery"] = {
                                "ok": True,
                                "attempt": production_recovery_attempts,
                                "backoff_sec": SQLITE_IO_RECOVERY_BACKOFF_SEC,
                                "reason": "sqlite_disk_io_error",
                            }
                            next_production = (
                                time.monotonic()
                                + SQLITE_IO_RECOVERY_BACKOFF_SEC
                            )
                        except Exception as recovery_exc:
                            production_result["connection_recovery"] = {
                                "ok": False,
                                "attempt": production_recovery_attempts,
                                "reason": "sqlite_disk_io_error",
                                "error": (
                                    f"{type(recovery_exc).__name__}:"
                                    f"{recovery_exc}"
                                ),
                            }
                    else:
                        production_recovery_attempts = 0
                        if target_result_has_sqlite_lock_contention(
                            production_result
                        ):
                            production_lock_contention_attempts += 1
                            backoff_sec = min(
                                TARGET_SQLITE_LOCK_MAX_BACKOFF_SEC,
                                TARGET_SQLITE_LOCK_BACKOFF_SEC
                                * (2 ** min(
                                    production_lock_contention_attempts - 1,
                                    3,
                                )),
                            )
                            next_production = max(
                                next_production,
                                time.monotonic() + backoff_sec,
                            )
                            production_result["sqlite_lock_backoff"] = {
                                "attempt": production_lock_contention_attempts,
                                "backoff_sec": backoff_sec,
                                "reason": "sqlite_lock_contention",
                            }
                        else:
                            production_lock_contention_attempts = 0
                    state["production"] = production_result
                except Exception as exc:
                    state["production"] = {"ok": False, "state": "blocked_fail_closed", "reason": f"{type(exc).__name__}:{exc}", "places_orders": False, "calls_order_gateway": False, "checked_at": utc_now()}
                production_future = None
                production_submitted_mono = time.monotonic()
                production_submitted_at = ""
                next_production = max(
                    next_production, time.monotonic() + WRITER_YIELD_SEC
                )
                writer_last_served["production"] = time.monotonic()
            if forecast_future is not None and forecast_future.done():
                try:
                    forecast_result = forecast_future.result()
                    if target_result_has_recoverable_sqlite_io_error(
                        forecast_result,
                        forecast=True,
                    ):
                        forecast_recovery_attempts += 1
                        try:
                            forecast_worker.reopen_target_connections()
                            forecast_result["connection_recovery"] = {
                                "ok": True,
                                "attempt": forecast_recovery_attempts,
                                "backoff_sec": SQLITE_IO_RECOVERY_BACKOFF_SEC,
                                "reason": "sqlite_disk_io_error",
                            }
                            next_forecast = (
                                time.monotonic()
                                + SQLITE_IO_RECOVERY_BACKOFF_SEC
                            )
                        except Exception as recovery_exc:
                            forecast_result["connection_recovery"] = {
                                "ok": False,
                                "attempt": forecast_recovery_attempts,
                                "reason": "sqlite_disk_io_error",
                                "error": (
                                    f"{type(recovery_exc).__name__}:"
                                    f"{recovery_exc}"
                                ),
                            }
                    else:
                        forecast_recovery_attempts = 0
                    state["forecast_prefetch"] = forecast_result
                except Exception as exc:
                    state["forecast_prefetch"] = {
                        "schema": "reported_trajectory_forecast_prefetch_v1",
                        "state": "blocked_fail_closed",
                        "reason": f"{type(exc).__name__}:{exc}",
                        "places_orders": False,
                        "calls_order_gateway": False,
                        "live_submit_authorized": False,
                        "orders_submitted": 0,
                        "checked_at": utc_now(),
                    }
                forecast_future = None
                next_forecast = max(
                    next_forecast, time.monotonic() + WRITER_YIELD_SEC
                )
                writer_last_served["forecast"] = time.monotonic()
            storage_ok = storage_guard.get("ok") is True
            # Whose turn it is at the single writer.
            #
            # The three lanes are gated against each other, and their gates
            # are evaluated in source order -- sweeps, production, prefetch.
            # Order alone decided everything, because every lane re-arms on a
            # timer far shorter than its own run: whoever is listed first is
            # due again the instant it releases the writer.  So the lane at
            # the bottom never ran at all.  Measured 2026-08-26: the day the
            # prefetch became the third gated writer the NWP rotation stopped
            # dead -- no capture, no rotation_state write, four ensemble
            # families ageing past their publication cycle -- while
            # production and the sweeps alternated normally.
            #
            # Ranking the lanes instead just moves the starvation: put the
            # prefetch first and production waits behind a pass that is
            # hundreds of seconds, not the milliseconds it is when nothing is
            # due (its TAF poll comes due every 60s, so almost every pass has
            # real work).  What is wanted is not a ranking but a turn: among
            # the lanes that are due, serve the one served longest ago.  No
            # lane can then run twice while another due lane waits, whatever
            # the relative length of their passes.
            sweeps_due = any(
                now_mono >= next_hosted_sweep[name]
                for name, _, _, _ in HOSTED_SWEEPS
            )
            writer_idle = (
                storage_ok
                and forecast_future is None
                and production_future is None
                and not hosted_sweep_futures
            )
            writer_candidates = []
            if writer_idle:
                if now_mono >= next_forecast:
                    writer_candidates.append("forecast")
                if now_mono >= next_production:
                    writer_candidates.append("production")
                if sweeps_due:
                    writer_candidates.append("sweeps")
            # Ties keep this order, so the choice stays deterministic.
            writer_turn = min(
                writer_candidates,
                key=lambda lane: writer_last_served[lane],
                default="",
            )
            # The production floor (2026-08-29).  "Longest ago" is fair
            # between lanes, but sweeps are ONE lane with ten-plus members:
            # once their combined due-rate outruns the drain rate the lane
            # is due at every instant, and generation 72534 served sweeps
            # for two straight hours while production -- the thing this
            # process exists to run -- got zero slots.  So a maintenance
            # lane may never take two consecutive slots while production is
            # due: the alternation production/sweep/production/... is the
            # worst case, whatever the queue holds.
            if (
                "production" in writer_candidates
                and writer_turn != "production"
                and last_writer_slot != "production"
            ):
                writer_turn = "production"
            if (
                storage_ok
                and now_mono >= next_registry_refresh
                and registry_future is None
            ):
                registry_future = registry_executor.submit(
                    registry_worker.run
                )
            if (
                storage_ok
                and now_mono >= next_argmax_refresh
                and argmax_future is None
            ):
                argmax_future = argmax_executor.submit(argmax_worker.run)
                next_argmax_refresh = time.monotonic() + 300.0
            if (
                storage_ok
                and now_mono >= next_render_refresh
                and render_future is None
            ):
                render_future = render_executor.submit(
                    run_hosted_sweep,
                    str(ROOT / "scripts/dashboard_render_sweep.sh"),
                    2400.0,
                )
                next_render_refresh = time.monotonic() + 1800.0
            if (
                storage_ok
                and now_mono >= next_argmax_entry
                and argmax_entry_future is None
            ):
                argmax_entry_future = argmax_entry_executor.submit(
                    argmax_entry_worker.run
                )
                next_argmax_entry = time.monotonic() + argmax_entry_interval_sec
            if storage_ok and "awc" in lane_workers and now_mono >= next_source.get("awc", now_mono) and "awc" not in lane_futures:
                lane_futures["awc"] = executor.submit(lane_workers["awc"].poll)
                next_source["awc"] = time.monotonic() + awc_poll_interval(config, datetime.now(timezone.utc))
            # Hosted sweeps and production cycles still exclude each other:
            # that mutual gate is the whole point of hosting them, so a sweep
            # burst can never again hold the lock into a production write and
            # fail P1 closed.  What relaxed (2026-08-30) is sweep against
            # sweep -- they may overlap, but only across write domains, so
            # "one routine writer per database" is unchanged.
            for done_name in [
                name
                for name, future in hosted_sweep_futures.items()
                if future.done()
            ]:
                try:
                    sweep_result = hosted_sweep_futures[done_name].result()
                except Exception as exc:  # noqa: BLE001
                    sweep_result = {
                        "state": "future_failed",
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                state["hosted_sweeps"]["results"][done_name] = sweep_result
                for name, _, interval_sec, _ in HOSTED_SWEEPS:
                    if name == done_name:
                        next_hosted_sweep[name] = (
                            time.monotonic() + interval_sec
                        )
                del hosted_sweep_futures[done_name]
                # Served when a sweep FINISHES, as before: the rotation ranks
                # lanes by how long ago each last completed, so stamping this
                # per finisher keeps the sweeps lane from claiming a fresh
                # turn while its own slower members are still running.
                writer_last_served["sweeps"] = time.monotonic()
            state["hosted_sweeps"]["running"] = sorted(hosted_sweep_futures)
            if (
                storage_ok
                # A slot may still admit a sweep whose database nothing running
                # is writing.  The exclusion against production and the
                # prefetch is unchanged: they write the same databases, and
                # whichever starts first owns the writer until it finishes.
                and len(hosted_sweep_futures) < HOSTED_SWEEP_MAX_CONCURRENCY
                and production_future is None
                # The prefetch is the third writer of the same databases, so
                # the exclusion has to name it here too.  Gating only against
                # production let a sweep start beside a running prefetch --
                # the exact overlap the mutual gate exists to remove.
                and forecast_future is None
                and writer_turn == "sweeps"
            ):
                # Serve whichever sweep has been due longest, not whichever
                # sits earliest in the tuple.  Strict list order gives the
                # head absolute priority, so an entry at the tail only runs
                # in an instant when nothing above it is due -- and with a
                # 300s probe at index 0 and an 1104s ledger round at index 2,
                # those instants are rare.  Measured on 2026-08-26:
                # settled_day_cycle_close came due at boot+510s and had still
                # not run 36 minutes later while index 0 ran repeatedly.
                # Ties keep list order, so the choice stays deterministic.
                due = [
                    (now_mono - next_hosted_sweep[name], index, name, script, timeout_sec)
                    for index, (name, script, _interval_sec, timeout_sec)
                    in enumerate(HOSTED_SWEEPS)
                    if now_mono >= next_hosted_sweep[name]
                    and name not in hosted_sweep_futures
                ]
                # One slot now starts every due sweep whose database is free,
                # not just the most overdue one.  Twelve members become due at
                # about seven per ten minutes while the lane served one per
                # turn, so the queue was permanently behind and a 600s member
                # waited a measured 43 minutes at the median.  Draining several
                # at once costs production no turns -- the slot already
                # excludes it, and k sweeps in one slot take max(duration)
                # where k slots took the sum.
                admitted = admissible_hosted_sweeps(
                    due,
                    running_names=hosted_sweep_futures.keys(),
                )
                for _overdue, _index, name, sweep_script, sweep_timeout in admitted:
                    last_writer_slot = "sweeps"
                    hosted_sweep_futures[name] = sweep_executor.submit(
                        run_hosted_sweep,
                        str(ROOT / sweep_script),
                        sweep_timeout,
                    )
                if admitted:
                    state["hosted_sweeps"]["running"] = sorted(
                        hosted_sweep_futures
                    )
            if (
                storage_ok
                and now_mono >= next_production
                and production_future is None
                and not hosted_sweep_futures
                # Symmetric with the prefetch gate below: whichever of the
                # two starts first owns the writer until it finishes.  The
                # prefetch is bounded by its own 300s subprocess timeout,
                # so the worst this costs production is one deferred slot,
                # against a cycle that currently fails closed outright when
                # the two overlap.
                and forecast_future is None
                and writer_turn == "production"
            ):
                last_writer_slot = "production"
                production_future = executor.submit(
                    run_target_production_cycle,
                    production_worker,
                )
                production_submitted_mono = time.monotonic()
                production_submitted_at = utc_now()
                next_production = time.monotonic() + 15.0
            if (
                storage_ok
                and now_mono >= next_forecast
                and forecast_future is None
                # The third writer.  Production and the hosted sweeps were
                # serialised against each other on 2026-08-24, but the
                # forecast prefetch was left ungated -- and it writes the
                # same ``calibration_forward_taf_capture`` rows the
                # production cycle writes.  Measured 2026-08-26: every
                # ingress in the cycle reported
                # ``OperationalError:database is locked``, the failing
                # statement was that table's INSERT, and the resident sat
                # BLOCKED_FAIL_CLOSED across cycles while the prefetch ran
                # beside it.  One writer at a time means all three lanes,
                # not two of them.
                and production_future is None
                and not hosted_sweep_futures
                and writer_turn == "forecast"
            ):
                # Keep the last completed bounded TAF receipt visible while a
                # new worker is in flight.  Replacing the whole prefetch
                # object with a bare ``pending`` marker used to erase the
                # previous successful attempt, making the read-only
                # production acceptance check fail during every poll window.
                previous_prefetch = state.get("forecast_prefetch")
                previous_taf_attempt = None
                if isinstance(previous_prefetch, Mapping):
                    previous_taf = previous_prefetch.get("forward_taf_capture")
                    if isinstance(previous_taf, Mapping):
                        candidate_attempt = previous_taf.get("last_attempt")
                        if isinstance(candidate_attempt, Mapping):
                            previous_taf_attempt = dict(candidate_attempt)
                pending_taf_capture = {
                    "state": "pending_same_bounded_worker",
                    "calls_order_gateway": False,
                    "places_orders": False,
                }
                if previous_taf_attempt is not None:
                    pending_taf_capture["last_attempt"] = previous_taf_attempt
                state["forecast_prefetch"] = {
                    "schema": "reported_trajectory_forecast_prefetch_v1",
                    "resident_nwp_capture": resident_nwp_in_flight_receipt(
                        forecast_worker,
                        scheduled_at_utc=utc_now(),
                    ),
                    "forward_taf_capture": pending_taf_capture,
                    "calls_order_gateway": False,
                    "places_orders": False,
                    "live_submit_authorized": False,
                    "orders_submitted": 0,
                }
                last_writer_slot = "forecast"
                forecast_future = forecast_executor.submit(
                    run_target_forecast_refresh_cycle,
                    forecast_worker,
                )
                next_forecast = time.monotonic() + 15.0
            if now_mono >= next_status_write:
                state["heartbeat_at"] = utc_now()
                _atomic_json(args.status, state)
                next_status_write = now_mono + 30.0
            time.sleep(0.01)
        state["stopped_at"] = utc_now()
        _atomic_json(args.status, state)
        return 0
    finally:
        # Cover normal SIGTERM and unexpected exceptions with the same bounded
        # ownership cleanup.  Objects are discovered defensively because
        # --once paths return before resident resources are constructed.
        #
        # Sweeps first, and before the executors are drained: their threads
        # are blocked in ``communicate`` and ``shutdown(wait=True)`` would
        # hold this exit open for the rest of the sweep's timeout, which is
        # how a restart leaves one orphaned on the write lock.
        _terminate_inflight_hosted_sweeps()
        for executor_name in (
            "executor",
            "forecast_executor",
            "registry_executor",
            "argmax_executor",
            "render_executor",
            "sweep_executor",
            "argmax_entry_executor",
        ):
            resident_executor = locals().get(executor_name)
            if resident_executor is not None:
                resident_executor.shutdown(
                    wait=True,
                    cancel_futures=False,
                )
        for worker_group_name in ("lane_workers",):
            worker_group = locals().get(worker_group_name)
            if isinstance(worker_group, Mapping):
                for resident_worker in worker_group.values():
                    resident_worker.close()
        for worker_name in (
            "production_worker",
            "forecast_worker",
            "registry_worker",
            "argmax_worker",
            "argmax_entry_worker",
        ):
            resident_worker = locals().get(worker_name)
            if resident_worker is not None:
                resident_worker.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
