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
    settlement_source_check,
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
from bayes_basket_target.production import PRODUCTION_EXECUTION_MODE
from asos_argmax_taf_reference import materialize_argmax_taf_references
from asos_p1_slot_materializer import materialize_scheduled_asos_p1_observations
from bayes_basket_target import serving_arithmetic
from bayes_basket_target.database import DEFAULT_TARGET_DB
from bayes_basket_target.pass_deadline import (
    HOSTED_SWEEP_WRITE_BLOCKED_MARKER,
    WRITER_LEASE_ENV,
)
from bayes_basket_target.market_registry import (
    export_shared_live_registry,
    load_active_registry_rows,
)
from bayes_basket_target.settlement_time import settlement_day_window
from bayes_basket_target.strategy_lanes import (
    LANE_CERTAIN_WIN_RECYCLE,
    LANE_MIS_BOUGHT_LEG_EXIT,
    LANE_P1_CONSENSUS,
    LANE_P1_TAIL,
    LANE_TIEBREAK,
    lane_enabled,
)
from bayes_basket_target import nwp_arrival_probe
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
    MAX_TARGET_WAL_BYTES,
    assert_connection_target_capacity,
    load_bounded_json,
)
from bayes_basket_target.taf_rotation_state import (
    MAX_SELECTED_SCOPES as MAX_TAF_SCOPES_PER_POLL,
    build_catalog as build_taf_rotation_catalog,
    build_rotation_state as build_taf_rotation_state,
    held_scope_prefixes as taf_rotation_held_scope_prefixes,
    load_rotation_state as load_taf_rotation_state,
    PRIORITY_GENERAL as TAF_PRIORITY_GENERAL,
    BULLETIN_OVERDUE_SEC as TAF_BULLETIN_OVERDUE_SEC,
    capture_lag_summary as taf_rotation_capture_lag,
    capture_age_seconds as taf_rotation_capture_age_seconds,
    latest_issued_at_by_scope as taf_rotation_latest_issued_at,
    newest_capture_at_by_scope as taf_rotation_newest_capture_at,
    station_issuance_cadence_sec as taf_rotation_issuance_cadence,
    tx_bearing_station_ids as taf_rotation_tx_station_ids,
    persist_rotation_state as persist_taf_rotation_state,
    scope_key as taf_rotation_scope_key,
    select_scopes as select_taf_rotation_scopes,
)
from scripts.capture_bayes_basket_target_awc_taf_revision import (
    awc_newer_taf_stations,
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
# Resource bounds that a later cycle can find cleared, and which therefore
# must not survive as a permanent state.
#
# The writer is opened once, before the loop, and a failure there sets
# ``commit_error`` on the worker for the life of the process: every cycle
# afterwards publishes ``blocked_fail_closed`` and nothing retries.  Measured
# 2026-09-01: free disk dipped under the 512 MiB floor at 12:13Z, the open
# failed, and production stayed fail-closed for twenty-six minutes with 23 GB
# free and the guard passing when asked directly.  Only a restart cleared it.
#
# Each of these clears on its own: free space is released, a checkpoint
# truncates the WAL, and the byte ceiling clears when it is raised -- which
# happened twice on 2026-09-01 and needed a restart each time.
# ``target_resource_guard_shared_live_database`` is deliberately absent: it is
# a misconfiguration, never transient, and retrying it would bury the one
# blocker an operator has to see.
TARGET_RESOURCE_REOPEN_REASONS = (
    "target_database_min_free_space_guard",
    "target_database_wal_bytes_exceeded",
    "target_database_bytes_exceeded",
)
# A store that was not ready when the writer opened and became ready later
# is the same shape as a resource bound that cleared (user ruling 2026-09-23
# 「加」).  That day a restart at 13:17Z loaded another session's mid-edit
# ``ssot.py``, which had put ``calibration_native_ensemble_certificate_
# renewal`` among the required tables before the lane that installs it had
# run.  The lane created the table minutes later; the writer, opened once
# before the loop, stayed ``target_writer_schema_not_ready`` for seventeen
# minutes -- the store answering ``ok`` to a direct status probe the whole
# time -- until a second restart.  Retrying costs one schema-status
# recompute per cycle while the store stays unready, and keeps the same
# reason on the status file, which is the right price.  A store that is not
# in WAL mode, or that is the shared live database, is a misconfiguration
# and is deliberately not here.
TARGET_SCHEMA_REOPEN_REASONS = (
    "target_writer_schema_not_ready",
)
TARGET_TAF_POLL_INTERVAL_SEC = 60.0
# How long one TAF round may hold the shared writer turn.
#
# The interval above is a floor, not a cadence: the poll runs inside the
# forecast prefetch, which takes its turn behind the production cycle and the
# hosted sweeps, so a round actually comes round every twenty-odd minutes.  The
# lap is therefore paid for in scopes per round, and the round was widened from
# four to twelve to bring a ninety-scope lap from twelve hours down to four.
#
# Ninety seconds bounds what that widening can cost the other two writers.
# Each capture is already bounded at 12s by capture_forward_awc_taf, but the
# measured per-capture spend is long-tailed -- 2s median, 9s at p90, 89s at p99
# over 858 samples since 2026-08-28 -- so twelve at the tail is minutes, not
# the seventy the mean predicts.  Past the budget the round returns what it
# has; the unattempted scopes are never stamped as served and so lead the next
# round.  A wide round on a good day, a short one on a bad day, and the writer
# turn stays bounded either way.
TARGET_TAF_ROUND_BUDGET_SEC = 90.0
# The fast TAF round (user, 2026-09-19: 「极限压低信息到来到bayes概率更新的延迟」).
# The rotation above runs inside the prefetch lane, which takes its writer
# turn behind a ~200s production cycle -- so a bulletin AWC published during a
# cycle waited for the cycle to end before anyone asked.  The fast round needs
# no writer turn (it reads the SSOT ``mode=ro`` and writes only the inbox
# file), so it runs on its own thread every twenty seconds: one bulk request
# asks who holds something newer, and only those stations are captured.
FAST_TAF_ROUND_INTERVAL_SEC = 20.0
# The inbox is rewritten whole by both rounds; one writer at a time.
_TAF_INBOX_LOCK = threading.Lock()
# Charter 195: when each model run is actually on the server we download from.
# Its own thread, like the fast TAF round: HEAD requests and one small state
# file, no writer turn and no SSOT.
NWP_ARRIVAL_PROBE_INTERVAL_SEC = nwp_arrival_probe.PROBE_INTERVAL_SEC
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
# The one arithmetic that governs this table.
#
# ``_EXCLUSIVE_WRITE_DOMAINS`` admits one SSOT writer at a time, and the
# production floor below admits the sweeps lane at most once between
# production slots -- deliberately, because without it the lane is due at
# every instant and served sweeps for two straight hours while production got
# none.  So the SSOT queue drains at one member per production cycle, and its
# supply is 3600/cycle_seconds runs an hour: about 10.6 at the 340s cycle
# measured 2026-09-01, about 6 at 600s.
#
# Demand is the sum of 3600/interval over every SSOT member.  On 2026-09-01 it
# was 18.0 an hour against a supply of 10.6 -- oversubscribed 1.7x at the best
# cycle time of the day and 4.3x at the worst -- so *every* member was
# permanently late and the ordering only decided who.  That is why this lane
# kept needing another fix: the ordering was being tuned while the queue was
# over capacity.  ``test_the_ssot_sweep_queue_fits_its_drain_rate`` is what
# stops that recurring; it fails when a member is added or an interval
# shortened past what the lane can drain.
#
# Supply cannot be raised without re-opening the starvation the production
# floor exists to prevent, so every contract below is sized against demand.
# Live-blocking work does not belong here at all -- it belongs in the cycle,
# which is why pr12_awc_observation is a backstop and no longer the lane.
HOSTED_SWEEPS: tuple[tuple[str, str, float, float], ...] = (
    # (name, script, interval_sec, timeout_sec)
    # Fifteen minutes, not five.  An interval is a claim on the SSOT queue --
    # that domain admits one writer per turn and the ordering ranks by how
    # many of its own intervals a member is late, so the shortest contract in
    # the table accumulates priority fastest.  Five minutes was the shortest,
    # and it belonged to the shadow probe of a concluded maker campaign, which
    # therefore outbid the identity lane that gates the live stage two turns
    # to one.  Measured 2026-09-01 08:27: the probe took the SSOT slot at 29.0
    # rounds late while pr12_awc_observation waited at 5.8.
    #
    # What it costs is sample density on ``maker_shadow_order`` -- a table with
    # 3.99M rows, no retention, and one consumer that reads its whole history
    # -- and the campaign it serves is closed. Operator ruling 2026-09-01.
    ("maker_shadow_probe", "scripts/maker_shadow_probe_sweep.sh", 10800.0, 900.0),
    # The backstop, not the lane.  The production cycle now runs this same
    # script itself, before the probability chain that consumes it -- it is
    # the one loop with a heartbeat and a watchdog, it comes round more often
    # than this contract, and it cannot be outbid for a turn because it is the
    # turn.  Held here at ten minutes the lane competed with nine other SSOT
    # members for one slot per cycle and went eight hours without completing a
    # twenty-three second job, which left every P1 in that window permanently
    # without its truth-revision sha.
    #
    # Half-hourly is what a backstop needs: it covers the rounds where the
    # cycle itself is fail-closed before it reaches the call, and its
    # single-instance guard is what keeps the two from overlapping.
    ("pr12_awc_observation", "scripts/pr12_awc_observation_sweep.sh", 10800.0, 900.0),
    # The paper ledger through production's own entries (2026-09-25): daily,
    # an hour and a half of reads over a week of event days.
    ("paper_ledger_aligned", "scripts/paper_ledger_aligned_sweep.sh", 86400.0, 5400.0),
    ("settlement_truth", "scripts/settlement_truth_sweep.sh", 10800.0, 900.0),
    # The two served calibration layers (band reliability, centre/width)
    # used to refit only from the 02:00 crontab, and macOS cron silently
    # skips the minute when the machine sleeps through it -- the fit
    # journal shows no cron round on 2026-08-16 or 2026-08-19, two silent
    # losses in ten days, each stalling both evidence clocks for a day.
    # Due-not-scheduled fixes that class; both refits are
    # content-addressed and admission-gated, so an early or doubled run
    # commits nothing new.
    ("calibration_refit", "scripts/calibration_refit_sweep.sh", 86_400.0, 900.0),
    # Charter 150 (2026-09-19): the two stage likelihoods -- P1's joint
    # observation likelihood (with its per-hours-bin tempering and incumbent
    # veto) and P0-M's TAF likelihood (with its walk-forward serve gate) --
    # refitted daily on a rolling 45-day sample window.  Reads both
    # databases read-only and writes three files; the serving loaders follow
    # the files by modification time.  ~4 minutes measured.
    ("p1_likelihood_refit", "scripts/p1_likelihood_refit_sweep.sh", 86_400.0, 900.0),
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
    # The registry is rewritten whole on every refresh and nothing removed
    # the previous copy: 1,176 copies by 2026-09-07, 1,471,712 rows for
    # about 2,810 markets, 2.58 GB, 270-320 MB a day -- the largest single
    # consumer of a database that was 6.1 days from its ceiling, where
    # every writer fail-closes.
    #
    # Hourly, and bounded to 120k rows a pass (2.8 seconds measured), so
    # the retention can never be the thing holding the single write lock.
    # A pass with nothing stale plans and returns without opening a
    # transaction at all.
    (
        "market_registry_retention",
        "scripts/market_registry_retention_sweep.sh",
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
    # The bar and release policy of 2026-08-30 was written to run
    # automatically and then ran only when somebody invoked it -- the same
    # failure the correction-review sweep two entries up was built to end.  A
    # station that went bad stayed live until a person looked, and one that
    # recovered stayed barred for the same reason.  Hourly matches the
    # cadence of what it reads: settlements arrive through the ledger and
    # truth sweeps above, and a verdict that moves inside an hour is a
    # verdict resting on a single settlement.  Alone among these it writes no
    # SSOT row -- two aggregates in, a small JSON config out -- so it is here
    # for the scheduling rather than for the write lock.
    (
        "auto_station_exclusions",
        "scripts/auto_station_exclusions_sweep.sh",
        3600.0,
        600.0,
    ),
    # The rule-binding test refuses a fork in the rules; nothing watched for
    # a fork in the outcomes.  On 2026-09-16 that test was green while the
    # ledger and the live book were buying different legs on the same
    # station-day -- same rules, different books -- and the divergence was
    # three weeks old before it was measured, with no record that could date
    # it.  Daily, because the four readings aggregate over a fourteen-day
    # window and do not move inside a day; what they move with is a rule
    # change, and the selection fingerprint rides on every row so that the
    # two causes stay tellable apart.  0.7 seconds and one small row.
    (
        "ledger_alignment_guard",
        "scripts/ledger_production_alignment_guard_sweep.sh",
        86_400.0,
        600.0,
    ),
    # The dated authority every candidate resolves -- the fill batch and the
    # policy generations -- kept from ending.  Both were minted with thirty
    # days by scripts nothing ran on a clock, so on 2026-09-17 the fill batch
    # had 4.5 days left and the policies 10.6 and 14.1, each end a fleet-wide
    # refusal.  Daily for the renewal and re-pin checks, which are indexed
    # reads when nothing is due; the fill refit inside it is gated to weekly
    # because one mint is ~160k rows.
    (
        "delta_v2_authority",
        "scripts/delta_v2_authority_sweep.sh",
        86_400.0,
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
    # Aligned to its own upstream cadence (2026-09-09).  These lanes ran
    # hourly against models that publish far less often, and the surplus
    # passes were not free: every one took the exclusive SSOT write domain
    # for a turn to discover there was no new run.  Demand on that one
    # writer was 7.75/h against a drain of 6.00 -- a 29% oversubscription
    # that ``test_the_ssot_sweep_queue_fits_its_drain_rate`` had been red
    # about -- and it was the standing overdraft that turned one stuck
    # member into a 72-hour outage on 2026-09-06..09.
    #
    # Freshness is unchanged: the interval still covers the run cycle in
    # ``regional_point_lane.RUN_CYCLE_HOURS``, and every cycle publishes
    # hours after its nominal time anyway (PUBLICATION_LAG_HOURS).  What
    # was cut is the empty passes.  NOAA keeps its hour: HRRR and NBM
    # really do issue every hour, and it is the only one of the four that
    # was named honestly.
        "capture_jma_msm_forward",
        "scripts/capture_jma_msm_forward_sweep.sh",
        10800.0,
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
    # New markets are listed days before anyone notices them, and the join
    # between discovery and onboarding used to be a person: Istanbul,
    # Moscow and Tel Aviv were listed with resolved stations and full
    # timezones from 2026-08-22 and the fleet stayed at forty-five.  Daily
    # because listings arrive days ahead and every stage is idempotent; the
    # pass reports unless PM_STATION_ONBOARDING_COMMIT is set, because the
    # write it would make changes the fleet's own identity.
    (
        "station_onboarding",
        "scripts/station_onboarding_sweep.sh",
        86_400.0,
        900.0,
    ),
    # CMA GRAPES Global for the eight mainland Chinese stations, which had
    # no regional coverage of any kind: HRRR and the NBM are CONUS, ICON-EU
    # stops far to their west, and MSM's domain begins at 120E.  Since
    # 2026-09-19 the lane asks for cycles by name (the aggregator's
    # single-run endpoint keeps the recent ones), so a missed pass is made
    # up by the next one within the day.  Capture only: the rows reach P1
    # as covariates of the joint observation likelihood (charter 150), like
    # every regional lane's.
    #
    # Half its own cadence, not equal to it (2026-09-16).  At 21600s a
    # six-hourly product got exactly one attempt per cycle, so one missed
    # pass lost that cycle for good -- and passes were missed, because the
    # queue ranks by intervals late and this was the longest interval on
    # it: twelve minutes past due scored 0.037 where an hourly member
    # scored 0.25, so it sat behind the short lanes for hours.  Measured
    # over five days the lane fired every 7 to 19.5 hours against a
    # 6-hour contract, and 2026-09-15 lost a cycle outright.  Halving buys
    # a second attempt inside each cycle window AND reaches any given rank
    # twice as fast.  The empty pass is genuinely cheap here: a stored
    # cycle is refused by ``plan_p1`` before any request, and an unpublished
    # one costs a single request and no write lock.
    (
        "capture_cma_grapes_forward",
        "scripts/capture_cma_grapes_forward_sweep.sh",
        10800.0,
        900.0,
    ),
    # The CMA capture's doorbell (2026-09-19).  One request, SSOT read-only,
    # no write domain: is the newest cycle the lane wants out yet?  When it
    # is, ``honour_cma_probe`` makes the capture due at once instead of up
    # to three hours later, so the SSOT queue pays for the four cycles a day
    # that exist and not for the asking.
    (
        "probe_cma_named_runs",
        "scripts/probe_cma_named_runs_sweep.sh",
        1200.0,
        120.0,
    ),
    # Charter 201 (2026-09-23): the deterministic-forecast family's global
    # members -- AIFS Single from ECMWF open data, IFS 9 km and UKMO 10 km
    # through the Open-Meteo relay -- at every roster station, every current
    # run, D-1 .. D+2.  Half a six-hourly cadence for CMA's reason, and due
    # at once when its doorbell sees a run land.  Capture only: no row
    # reaches P1 (pairs frozen) and none is an ensemble member.
    (
        "capture_deterministic_forward",
        "scripts/capture_deterministic_forward_sweep.sh",
        10800.0,
        1800.0,
    ),
    # Its doorbell: one request a product, SSOT read-only, no write domain.
    (
        "probe_deterministic_runs",
        "scripts/probe_deterministic_runs_sweep.sh",
        1200.0,
        120.0,
    ),
    # Charter 201 step 2: the P0 deterministic-feature gate, fitted on settled
    # days (rolling k, pre-registered readings, each station's candidate).
    # SSOT ``mode=ro``, one JSON artifact the P0 compile reads; daily because
    # a station-day settles once a day.
    (
        "fit_deterministic_feature_gate",
        "scripts/fit_deterministic_feature_gate_sweep.sh",
        86400.0,
        1800.0,
    ),
    # Charter 197 (2026-09-22): the supply watch.  2026-09-21 16:04Z the proxy
    # tunnel stopped carrying traffic, every upstream failed together, and by
    # 19:42Z no D+1 P0 was fresh anywhere -- until 08:55Z the next morning,
    # with every light on the board green, because each one measured a lane
    # moving and none the thing it moves arriving.  This asks both directly:
    # one HEAD per upstream through the production proxy, and fresh served
    # P0/P0-M per listed future date by primary key.  SSOT ``mode=ro``, one
    # JSON out, a few seconds; five minutes so an outage is red inside ten.
    (
        "supply_watch",
        "scripts/supply_watch_sweep.sh",
        300.0,
        120.0,
    ),
    # ICON-EU deterministic, run-scoped: one whole-domain download serves
    # seven European stations and two event days.  Hourly against a
    # six-hourly product because the DWD archive keeps no history -- a
    # missed run is permanent, a wasted pass is two directory probes.
    (
        "capture_dwd_icon_eu_forward",
        "scripts/capture_dwd_icon_eu_forward_sweep.sh",
        10800.0,
        1800.0,
    ),
    # Charter 199 (2026-09-23): the settled station-days the P0 fusion blend
    # trains on and the forward shadow record is scored on.  SSOT ``mode=ro``;
    # writes only data/fusion_blend_training.db, append-only, a few seconds a
    # pass.  Hourly: a station-day is usable from the moment its truth is
    # known, and an hour is well inside the day-ahead decisions that read it.
    (
        "fusion_blend_training",
        "scripts/fusion_blend_training_sweep.sh",
        3600.0,
        900.0,
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
#   paper_ledger - writes its own paper-ledger file only; the SSOT is opened
#                ``mode=ro``.  data/paper_ledger.db until 2026-09-25, then
#                data/paper_ledger_aligned.db.  No writer lane writes that
#                file, so it excludes nothing.
# A name missing from this map is treated as an SSOT writer, so a sweep added
# without a decision here loses throughput rather than the invariant.
HOSTED_SWEEP_WRITE_DOMAIN: dict[str, str] = {
    # Its two tables left the SSOT for their own file on 2026-09-21 (charter
    # 169): 5.26M intents and 1.33M settlements, 1.34 GiB, none of it on the
    # decision path -- production reads the FITTED artifact, never these
    # rows.  Writing them made the probe an SSOT writer and cost production
    # an exclusive turn every three hours for a book nothing downstream
    # waits on.
    "maker_shadow_probe": "maker_shadow",
    "pr12_awc_observation": "ssot",
    # Its four tables left the SSOT for their own file on 2026-09-19: the
    # pass is almost all reading (7,419 production cycles re-walked per
    # pass) and it held the SSOT writer for its whole lease regardless --
    # 62 production cycles waited on it, 4.25 hours of the prior 28.9.
    # Reads the SSOT and the archive ``mode=ro``; writes only
    # data/paper_ledger_aligned.db, which nothing else writes, so it excludes
    # no writer lane (2026-09-25).  The retired paper_trading_ledger held
    # this slot.
    "paper_ledger_aligned": "paper_ledger",
    "settlement_truth": "ssot",
    "calibration_refit": "ssot",
    "settled_day_cycle_close": "ssot",
    "correction_review_auto_clear": "archive",
    "forward_packet_retention": "none",
    # Reads the SSOT and the archive ``mode=ro``; writes the samples DB and
    # two JSON artifacts under data/runtime, none of them a shared database.
    "p1_likelihood_refit": "none",
    # Reads the SSOT ``mode=ro`` and writes only JSON config files, so it
    # holds no database lock and can overlap anything.
    "auto_station_exclusions": "none",
    # One small row a day.  It is an SSOT writer by the letter of this map,
    # and it is the cheapest one here: the pass measured 0.7 seconds and its
    # insert waits on ``busy_timeout`` rather than making production wait.
    "ledger_alignment_guard": "ssot",
    "delta_v2_authority": "ssot",
    "capture_jma_msm_forward": "ssot",
    "capture_noaa_conus_forward": "ssot",
    "capture_dwd_icon_eu_forward": "ssot",
    "capture_cma_grapes_forward": "ssot",
    "capture_deterministic_forward": "ssot",
    # Reads the SSOT ``mode=ro`` and writes one small JSON file.
    "probe_cma_named_runs": "none",
    "probe_deterministic_runs": "none",
    # Charter 201 step 2: SSOT ``mode=ro``, one JSON artifact.
    "fit_deterministic_feature_gate": "none",
    # Charter 197: SSOT ``mode=ro``, HEAD probes, one small JSON file.
    "supply_watch": "none",
    # Charter 199: SSOT ``mode=ro``; writes data/fusion_blend_training.db
    # only, a file no other lane writes.
    "fusion_blend_training": "fusion_blend",
    # Writes station_registry.py and the two SSOT identity rows.
    "station_onboarding": "ssot",
    "market_registry_retention": "ssot",
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


# Capture lanes that write the SSOT but never across a download.  Each one
# fetches a run, writes it, and commits before it fetches the next, so the
# SQLite lock is held for the ingest seconds and the network time holds
# nothing.  The writer turn nonetheless counted the whole subprocess as
# holding the writer.  Measured 2026-09-13: capture_noaa_conus_forward ran
# 1140s and production waited 1132s behind it, almost all of it HRRR/NBM
# downloads.  The regional_point_lane docstring had already said as much on
# 2026-09-09 -- "those thirty minutes are thirty minutes in which the
# production cycle cannot start" -- and bounded the seconds instead.
#
# Exempt from the writer gates, and (2026-09-14) from holding the SSOT queue
# while they download.  A running capture used to claim the ssot domain for
# its whole subprocess, so no other SSOT sweep could start: on 2026-09-14
# capture_noaa_conus_forward held it 07:41-08:05Z, the paper ledger went nine
# hours without completing, and settlement_truth eleven.  They still never
# START beside another SSOT sweep in one slot, and none starts while a
# long-transaction SSOT sweep runs (that sweep holds the writer, so no slot
# opens); what a running capture holds now is the other captures.  Its
# ingest meets a later SSOT sweep the ordinary way, at SQLite -- a commit may
# wait out busy_timeout behind that sweep's write, fail that pass, and be
# retried on the lane's next one.  The exemption rests on the
# commit-before-fetch shape, which
# tests/test_capture_lanes_release_the_writer_turn.py holds: a lane that
# wraps a fetch in an open transaction turns that test red, and does not
# belong in this set.
WRITER_TURN_EXEMPT_SWEEPS: frozenset[str] = frozenset({
    "capture_noaa_conus_forward",
    "capture_jma_msm_forward",
    "capture_dwd_icon_eu_forward",
    "capture_cma_grapes_forward",
    "capture_deterministic_forward",
})


# The admission token a running capture holds: the other captures, not the
# SSOT queue.  See WRITER_TURN_EXEMPT_SWEEPS.
CAPTURE_ADMISSION_CLAIM = "ssot_capture"


def sweep_finish_serves_writer_turn(name: str) -> bool:
    """Does a sweep finishing count as the sweeps lane having been served?

    A lane that held the writer was served when it let go of it, and the
    stamp keeps the sweeps lane from claiming a fresh turn while its slower
    members still run.  A download capture never held the writer, so its
    finish is not a serve: stamped anyway, capture_noaa_conus_forward ending
    at 08:05:49Z on 2026-09-14 made the sweeps lane read as just served,
    and no sweep slot opened for the next forty-five minutes while the
    ledger, settlement_truth and a ten-minute retention lane all waited.
    """

    return str(name) not in WRITER_TURN_EXEMPT_SWEEPS


def writer_excluding_sweeps(running_names: "Iterable[str]") -> list[str]:
    """Running sweeps that write a database the writer lanes also write.

    Production, the forecast prefetch and WAL maintenance write the SSOT and
    the archive, so a sweep excludes them only if it writes one of those.  A
    sweep in the ``none`` domain writes no database at all -- that is what the
    domain map says -- yet every writer gate used to test for *any* running
    sweep, so forward_packet_retention on its ten-minute clock and
    auto_station_exclusions on its hourly one each held production out for as
    long as they ran.  The sweep-to-sweep admission rule already read the
    domain; the writer gates now read it too.
    """

    return sorted(
        name for name in running_names
        if hosted_sweep_write_domain(name) in _EXCLUSIVE_WRITE_DOMAINS
        and name not in WRITER_TURN_EXEMPT_SWEEPS
    )


# Where each sweep's last completion is remembered across restarts.  The
# schedule used to live only in ``next_hosted_sweep`` -- monotonic-seeded,
# in-memory -- so every restart forgot who was overdue and re-ran the boot
# stagger from scratch.  On 2026-08-30 five deploy restarts did exactly
# that in sequence and the settlement-truth sweep, fourth in the stagger
# behind a seventeen-minute ledger round, went 6.8 hours unserved while
# every generation started the queue over.  Persisting the completion
# clock makes a restart harmless to the queue: the boot seed below puts a
# genuinely overdue member's due-time in the past by its true overdue
# amount, so the most-starved member wins the first slot its domain frees.
HOSTED_SWEEP_CLOCKS_PATH = ROOT / "data" / "hosted_sweep_clocks.json"


def seed_hosted_sweep_clock(
    *,
    now_mono: float,
    now_wall_utc: str,
    completed_at_utc: str | None,
    interval_sec: float,
    stagger_sec: float,
    attempted_at_utc: str | None = None,
) -> float:
    """One member's boot due-time, honouring its persisted last completion.

    No record keeps the old boot stagger.  A record seeds the due-time
    where an uninterrupted daemon would have it: ``completion + interval``
    -- in the past by the true overdue amount when the member is starved
    (a small boot grace keeps the first slots from racing startup), in the
    future by the genuine remainder when it is not.

    ``attempted_at_utc`` is the fix for what that reasoning missed
    (2026-09-09): a member that FAILS never advances its completion, so on
    every restart it is maximally overdue, ranks first, and takes its slot
    again.  ``capture_noaa_conus_forward`` had been in that loop for 72
    hours -- 1,800 seconds of the exclusive SSOT write domain per attempt,
    then ``timeout_killed``, then first in line again at the next restart --
    and it was starving six other SSOT members behind it (settlement truth,
    the paper ledger and the maker shadow probe among them, all 12+ hours
    late on a 3-hour interval).

    So the retry cadence reads the later of the two stamps while the
    COMPLETION stamp still says, truthfully, that this member has not
    succeeded since.  A failing member retries on its own interval instead
    of on every restart; nothing pretends it worked.
    """

    stamps = [value for value in (completed_at_utc, attempted_at_utc) if value]
    if not stamps:
        return now_mono + stagger_sec
    try:
        completed = max(
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            for value in stamps
        )
        now_wall = datetime.fromisoformat(
            str(now_wall_utc).replace("Z", "+00:00")
        )
    except ValueError:
        return now_mono + stagger_sec
    since = (now_wall - completed).total_seconds()
    if since < 0:
        # A completion stamped in the future is clock damage, not a
        # schedule; fall back to the stagger rather than trusting it.
        return now_mono + stagger_sec
    overdue = since - float(interval_sec)
    if overdue >= 0:
        return max(now_mono - overdue, now_mono - 86_400.0) + 60.0
    return now_mono - overdue


CMA_NAMED_RUN_PROBE_PATH = ROOT / "data" / "runtime" / "cma_named_run_probe.json"


def honour_cma_probe(
    next_due: "dict[str, float]",
    *,
    now_mono: float,
    honoured: "dict[str, str]",
    path: Path = CMA_NAMED_RUN_PROBE_PATH,
) -> str | None:
    """Make the CMA capture due now when its probe saw a new cycle published.

    Once per cycle: ``honoured`` remembers the cycle already acted on, so a
    capture that is refused or fails is retried on its own interval rather
    than on every loop.  Returns the cycle it acted on, else None.
    """

    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    run = str((state or {}).get("newest_wanted_run_utc") or "")
    if not run or not (state or {}).get("published"):
        return None
    if honoured.get("capture_cma_grapes_forward") == run:
        return None
    if "capture_cma_grapes_forward" not in next_due:
        return None
    next_due["capture_cma_grapes_forward"] = min(
        next_due["capture_cma_grapes_forward"], now_mono
    )
    honoured["capture_cma_grapes_forward"] = run
    return run


DETERMINISTIC_RUN_PROBE_PATH = (
    ROOT / "data" / "runtime" / "deterministic_run_probe.json"
)
# Charter 201: which capture a product's doorbell makes due.  Only the
# family's new global members for now; the P1 lanes (HRRR/NBM/ICON-EU/MSM)
# keep their clocks, so what P1 reads and when is unchanged.
DETERMINISTIC_PROBE_SWEEPS = {
    "AIFS": "capture_deterministic_forward",
    "IFS9KM": "capture_deterministic_forward",
    "UKMO10": "capture_deterministic_forward",
}


def honour_deterministic_probe(
    next_due: "dict[str, float]",
    *,
    now_mono: float,
    honoured: "dict[str, str]",
    path: Path = DETERMINISTIC_RUN_PROBE_PATH,
) -> list[str]:
    """Make a family capture due now when its doorbell saw a new run land.

    Once per (product, run), as ``honour_cma_probe`` does for CMA: a capture
    that is refused or fails is retried on its own interval, not every loop.
    Returns the products acted on.
    """

    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    acted: list[str] = []
    for product, entry in sorted(((state or {}).get("products") or {}).items()):
        sweep = DETERMINISTIC_PROBE_SWEEPS.get(str(product))
        run = str((entry or {}).get("newest_wanted_run_utc") or "")
        if not sweep or not run or not (entry or {}).get("published"):
            continue
        key = f"{sweep}:{product}"
        if honoured.get(key) == run or sweep not in next_due:
            continue
        next_due[sweep] = min(next_due[sweep], now_mono)
        honoured[key] = run
        acted.append(str(product))
    return acted


def due_hosted_sweeps(
    *,
    now_mono: float,
    next_due: "Mapping[str, float]",
    skip_names: "Iterable[str]" = (),
    sweeps: "Sequence[tuple[str, str, float, float]]" = (),
) -> "list[tuple[float, int, str, str, float]]":
    """Every member past its due time, ranked by intervals late.

    Intervals, not seconds.  Those are different questions and only the first
    is about the contract a member was given: a lane on a ten-minute clock
    that is eight hours behind has missed forty-eight rounds, while an hourly
    capture eight hours behind has missed eight, and in raw seconds the two
    look alike.  The SSOT is an exclusive write domain and ten of the thirteen
    members write it, so the queue drains one per turn and this ordering is
    what decides who waits.
    """

    return [
        (
            (now_mono - next_due[name]) / max(float(interval_sec), 1.0),
            index,
            name,
            script,
            timeout_sec,
        )
        for index, (name, script, interval_sec, timeout_sec)
        in enumerate(sweeps or HOSTED_SWEEPS)
        if name in next_due
        and now_mono >= next_due[name]
        and name not in set(skip_names)
    ]


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

    ``overdue`` is whatever the caller ranks by; the scheduler passes how many
    of the member's own intervals it is late, so a ten-minute lane and an
    hourly one are compared on their contracts rather than on raw seconds.
    Nothing here depends on the unit.
    """

    # Materialized once: the caller may pass a view or a generator, and
    # consuming it twice would silently read the second use as empty.
    running = list(running_names)
    claimed: set[str] = set()
    for name in running:
        if name in WRITER_TURN_EXEMPT_SWEEPS:
            # Downloading, not holding the database: blocks the other
            # captures only (2026-09-14).
            claimed.add(CAPTURE_ADMISSION_CLAIM)
            continue
        domain = hosted_sweep_write_domain(name)
        if domain in _EXCLUSIVE_WRITE_DOMAINS:
            claimed.add(domain)
    inflight = len(running)
    chosen: list[tuple[float, int, str, str, float]] = []
    for item in sorted(due, key=lambda entry: (-entry[0], entry[1])):
        if inflight + len(chosen) >= max_concurrency:
            break
        name = item[2]
        domain = hosted_sweep_write_domain(name)
        if name in WRITER_TURN_EXEMPT_SWEEPS:
            if CAPTURE_ADMISSION_CLAIM in claimed or "ssot" in claimed:
                continue
            # Started in this slot, it still keeps every other SSOT sweep
            # from starting beside it.
            claimed.update({CAPTURE_ADMISSION_CLAIM, "ssot"})
            chosen.append(item)
            continue
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


# Charter 189 (user, 2026-09-21: 「乙」): the native-ensemble OOS pass runs in
# a separate maintenance worker, which this resident starts and keeps
# running.  See scripts/weather_maintenance_worker.py; the cycle takes the
# lane back whenever the worker's heartbeat does not claim it.
MAINTENANCE_WORKER_SCRIPT = ROOT / "scripts" / "weather_maintenance_worker.py"
MAINTENANCE_WORKER_LOG = ROOT / "data" / "logs" / "maintenance_worker.log"
MAINTENANCE_WORKER_RESTART_BACKOFF_SEC = 60.0
MAINTENANCE_WORKER_LOG_MAX_BYTES = 5 * 1024 * 1024


class MaintenanceWorkerSupervisor:
    """Start the maintenance worker, restart it after a backoff, stop it."""

    def __init__(
        self,
        *,
        archive_db: Path,
        script: Path = MAINTENANCE_WORKER_SCRIPT,
        log_path: Path = MAINTENANCE_WORKER_LOG,
        python: str = sys.executable,
        popen: Any = subprocess.Popen,
        clock: Any = time.monotonic,
        enabled: bool | None = None,
    ) -> None:
        self.archive_db = Path(archive_db)
        self.script = Path(script)
        self.log_path = Path(log_path)
        self.python = python
        self.popen = popen
        self.clock = clock
        self.enabled = (
            str(os.environ.get("PM_MAINTENANCE_WORKER", "1")).strip()
            not in {"0", "false", "off"}
            if enabled is None else bool(enabled)
        )
        self.proc: Any = None
        self.next_start = 0.0
        self.starts = 0
        self.last_exit: int | None = None

    def tick(self) -> dict[str, Any]:
        if not self.enabled:
            return {"state": "disabled"}
        if self.proc is not None:
            code = self.proc.poll()
            if code is None:
                return {"state": "running", "pid": self.proc.pid, "starts": self.starts}
            self.last_exit = int(code)
            self.proc = None
            self.next_start = self.clock() + MAINTENANCE_WORKER_RESTART_BACKOFF_SEC
        if self.clock() < self.next_start:
            return {
                "state": "backing_off", "starts": self.starts,
                "last_exit": self.last_exit,
            }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if (
                self.log_path.exists()
                and self.log_path.stat().st_size > MAINTENANCE_WORKER_LOG_MAX_BYTES
            ):
                self.log_path.write_bytes(b"")
            log = open(self.log_path, "ab")
            try:
                self.proc = self.popen(
                    [
                        self.python, str(self.script),
                        "--parent-pid", str(os.getpid()),
                        "--archive-db", str(self.archive_db),
                    ],
                    cwd=str(ROOT),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
            finally:
                log.close()
        except Exception as exc:  # noqa: BLE001 -- the cycle keeps the lane
            self.proc = None
            self.next_start = self.clock() + MAINTENANCE_WORKER_RESTART_BACKOFF_SEC
            return {"state": "start_failed", "reason": f"{type(exc).__name__}:{exc}"[:200]}
        self.starts += 1
        return {"state": "started", "pid": self.proc.pid, "starts": self.starts}

    def stop(self, timeout: float = 15.0) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001 -- shutdown is best effort
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


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


# A member that stopped on its own budget with work left says so on its
# stdout with this line, and comes due again after the delay below instead of
# its whole interval.  The paper ledger is the first to need it: a rule change
# re-derives the whole book in slices sized to one pass, and on its 10800s
# interval the book sat partial for two intervals -- long enough for the next
# rule change to start it over.  Only a clean exit counts: a killed or
# failed run keeps its interval, which is what stops a failing member from
# holding its write domain every few minutes.
HOSTED_SWEEP_CONTINUE_MARKER = "hosted_sweep:continue"
HOSTED_SWEEP_CONTINUATION_DELAY_SEC = 300.0
# A capture that could not take the SSOT write lock gets its own state, not
# ``failed``, and comes due again after the same short delay.
#
# It is neither of the two states it would otherwise be squeezed into.  It
# is not ``ok``: the pass stored nothing and the completion stamp has to go
# on saying so, or a lane blocked for an hour renders green while it
# captures nothing -- the shape ``collect_hosted_sweeps`` exists to catch.
# And waiting a whole interval, which is what ``failed`` buys, is what made
# it worth naming: on 2026-09-15 a contended INSERT ended
# capture_jma_msm_forward after 955 seconds of ranged reads and
# capture_cma_grapes_forward after 115, and the next attempt was three and
# six hours away -- long enough, for a product whose transport serves only
# the run it currently holds, to lose the cycle for good.
#
# The short retry is safe for these lanes and would not be for the others:
# a capture is writer-turn exempt, so its retry takes no turn from
# production, and it stops at its first blocked write, so a pass that
# retries into a still-locked file costs one fetch, not a full round.
HOSTED_SWEEP_WRITE_BLOCKED_STATE = "write_blocked"

# The lanes that take the writer in turn.  Production is what the process
# exists to run; the hosted sweeps are everything that must also write the
# SSOT.  The forecast prefetch used to be the third lane, on a measurement
# from 2026-08-26 when it still ingested TAF rows itself.  It has not
# written the SSOT from its own thread since: the NWP rotation, the TAF
# round and the calibration audit read the store and write JSON files, and
# the captures it starts already commit beside production from their own
# subprocesses.  Yet it still took every other writer slot -- 1,875 of the
# 2,389 seconds production waited between 04:58Z and 06:57Z on 2026-09-15
# were behind it.  It now runs on its own clock, beside production.
WRITER_LANES: tuple[str, ...] = ("production", "sweeps")

# What one sweep slot may cost production.  Between production cycles the
# writer goes to a sweep, and the sweep holds it for as long as its pass
# runs; every pass budget so far was the lane's own constant (900s, 1200s,
# 1500s), sized after the incident that lane caused, and the paper ledger
# alone kept a due cycle waiting 1,025s on 2026-09-15.  The resident now
# grants each sweep this lease in its environment
# (``pass_deadline.WRITER_LEASE_ENV``); a sliced lane clamps its pass to it
# and asks for the next pass with the continuation marker, so a production
# cycle -- 300s target, 720s overdue -- waits at most a lease plus the
# slice a sweep had already started.  A sweep the lease cannot bound is
# declared as such below, with the interval that keeps it rare.
WRITER_LEASE_SEC = 240.0

# How each SSOT-holding sweep honours the lease.  ``sliced``: the lane reads
# the lease, stops between units of work and prints the continuation marker
# when work is left.  ``one_shot``: the pass cannot stop part-way, and its
# declared bound says how far past the lease it may run; one past the lease
# must be on an interval of six hours or more, so production pays for it a
# few times a day at most.  A download capture is not here: it never holds
# the writer (WRITER_TURN_EXEMPT_SWEEPS), and neither is a sweep whose
# domain excludes no writer lane.
HOSTED_SWEEP_LEASE_CONTRACT: dict[str, tuple[str, float]] = {
    "settlement_truth": ("sliced", WRITER_LEASE_SEC),
    "settled_day_cycle_close": ("sliced", WRITER_LEASE_SEC),
    "pr12_awc_observation": ("one_shot", 120.0),
    "market_registry_retention": ("one_shot", 120.0),
    "correction_review_auto_clear": ("one_shot", 60.0),
    # Three fits in one wrapper; measured 7m37s on 2026-09-06, once a day.
    "calibration_refit": ("one_shot", 600.0),
    "station_onboarding": ("one_shot", 300.0),
    # Four aggregates in, one row out; the pass measured 0.7s on 2026-09-16.
    # Sixty seconds is slack, not a budget -- well inside the lease, so the
    # interval rule below never has to bite.
    "ledger_alignment_guard": ("one_shot", 60.0),
    # A re-pin check most days (~25s of index reads); once a week the fill
    # refit streams ~5M shadow rows (~70s) and writes ~160k in 20k-row
    # commits, then the re-pin.  Daily, so the overrun is paid once a week.
    "delta_v2_authority": ("one_shot", 600.0),
}


def writer_turn_for(
    candidates: "Sequence[str]",
    last_served: "Mapping[str, float]",
    last_slot: str,
) -> str:
    """Which writer lane takes the slot: the one served longest ago, except
    that a maintenance lane never takes two slots in a row while production
    is due (the production floor, 2026-08-29)."""

    turn = min(
        candidates,
        key=lambda lane: last_served[lane],
        default="",
    )
    if (
        "production" in candidates
        and turn != "production"
        and last_slot != "production"
    ):
        return "production"
    return turn


def hosted_sweep_clock_entry(
    previous: "Mapping[str, Any] | None",
    result: "Mapping[str, Any]",
    *,
    name: str,
    now_utc: str,
    lease_sec: float = WRITER_LEASE_SEC,
) -> dict[str, Any]:
    """The clocks-file entry for a sweep that just finished.

    Every attempt advances the attempt stamp; only success advances the
    completion stamp (see ``seed_hosted_sweep_clock``).  The duration and how
    far it ran past the lease are kept too, because the lease is a contract
    the scheduler cannot enforce by force -- killing a sweep loses its work
    -- so the record is how a lane that outgrows its slice gets noticed.
    """

    entry = dict(previous) if isinstance(previous, Mapping) else {}
    state = str(result.get("state") or "")
    entry["attempted_at_utc"] = now_utc
    entry["last_state"] = state
    if state == "ok":
        entry["completed_at_utc"] = now_utc
    entry["continue_requested"] = bool(
        state == "ok" and result.get("continue")
    )
    duration = result.get("duration_sec")
    if isinstance(duration, (int, float)):
        entry["last_duration_sec"] = round(float(duration), 1)
        holds_writer = (
            hosted_sweep_write_domain(name) in _EXCLUSIVE_WRITE_DOMAINS
            and name not in WRITER_TURN_EXEMPT_SWEEPS
        )
        entry["lease_overrun_sec"] = (
            round(max(0.0, float(duration) - float(lease_sec)), 1)
            if holds_writer else 0.0
        )
    return entry


def hosted_sweep_boot_outcome(entry: Any) -> dict[str, Any]:
    """The last run's outcome, as the boot seed must read it from the clocks.

    The seed used to rebuild it as ``{"state": "ok", ...}`` whatever the
    file said, so a restart turned a ``write_blocked`` pass's five-minute
    retry back into a whole interval from its attempt.  Measured 2026-09-18:
    capture_cma_grapes_forward was blocked at 00:43Z, the resident restarted
    at 01:03Z and three more times before 02:46Z, and each boot seeded the
    lane at attempt + 3h -- so the 18Z GRAPES cycle, on a transport that
    serves only the run it currently holds, sat uncaptured for five hours
    while every other lane moved.  The persisted state is read verbatim;
    ``failed`` and ``timeout_killed`` still wait their full interval.
    """

    if not isinstance(entry, Mapping):
        return {}
    return {
        "state": str(entry.get("last_state") or "ok"),
        "continue": entry.get("continue_requested"),
    }


def hosted_sweep_interval_after(
    result: "Mapping[str, Any]", interval_sec: float
) -> float:
    """How long until this member is due again, given how its run ended."""

    if result.get("state") == "ok" and result.get("continue"):
        return min(float(interval_sec), HOSTED_SWEEP_CONTINUATION_DELAY_SEC)
    if result.get("state") == HOSTED_SWEEP_WRITE_BLOCKED_STATE:
        return min(float(interval_sec), HOSTED_SWEEP_CONTINUATION_DELAY_SEC)
    return float(interval_sec)


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
    continues = False
    try:
        process = subprocess.Popen(
            ["/bin/bash", script_path],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env={**os.environ, WRITER_LEASE_ENV: f"{WRITER_LEASE_SEC:.0f}"},
        )
        with _HOSTED_SWEEP_PROCESS_LOCK:
            _HOSTED_SWEEP_PROCESSES.add(process)
        try:
            out, err = process.communicate(timeout=timeout_sec)
            returncode: int | None = process.returncode
            tail = (_text(out) + _text(err))[-600:]
            state_text = "ok" if returncode == 0 else "failed"
            markers = {line.strip() for line in _text(out).splitlines()}
            continues = (
                returncode == 0 and HOSTED_SWEEP_CONTINUE_MARKER in markers
            )
            # A clean exit that says it was blocked is not a success: the
            # pass ran, reported, and stored nothing.  Read off stdout for
            # the same reason the continue marker is -- the exit code has
            # room for "ran" and "died" and nothing in between.
            if (
                returncode == 0
                and HOSTED_SWEEP_WRITE_BLOCKED_MARKER in markers
            ):
                state_text = HOSTED_SWEEP_WRITE_BLOCKED_STATE
                continues = False
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
        "continue": continues,
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
# Budgeting four hours was too slow for a catalog this size, so the sweep is
# given one.
#
# The ceiling that used to hold this to 20 was written when advancing cost
# downloads: retained archives keyed on the exact station set, so no scope was
# ever served from another scope's archive and every one of them paid for the
# same ~340MiB of global fields again.  That keying was the bug and is gone --
# the capture keys on the event date alone, and one capture writes every
# station of that day (45 packets, one arrival stamp).  A poll that advances
# further therefore buys no extra fetches at all: with three open event dates a
# whole sweep can spend at most three captures however many scopes it steps
# through.
#
# Holding the ceiling anyway bought nothing and cost the sweep its budget.  At
# 381 scopes the budget asks for 64 a poll; 20 stretched the sweep to 3.2h
# nominal, and the forecast lane only polls when it wins the writer turn
# against the production cycle, which put the observed sweep near 5.5h against
# a 6h P0 freshness TTL.  On 2026-09-09 that margin ran out: 2026-09-10's
# packets were captured at 02:42Z, expired at 08:42Z, and their scopes' next
# turn in the rotation was still an hour away at 12:20Z -- 45 stations
# fail-closed on native_ensemble_snapshot_stale for the whole morning while
# every lane reported itself healthy.  The wall-clock advance budget below is
# what actually bounds a poll, and it always was.
TARGET_NWP_SWEEP_BUDGET_SEC = 1.0 * 3600.0
# A sanity bound, not the governor.  What actually bounds a poll is
# TARGET_NWP_ADVANCE_BUDGET_SEC below -- wall clock, checked between scopes --
# and the comment there has said so since it was written.
#
# This constant was 20, sized when capture paid one download per scope, and
# capping TARGET_NWP_SWEEP_BUDGET_SEC 3.2x long after capture became keyed by
# event day.  Raised to 64 on 2026-09-09 for a 381-scope catalogue, and the
# catalogue was 402 by that afternoon: 402 needs 67 a poll to finish inside
# the hour budget, so the ceiling was capping the budget again within hours of
# being raised to match it.  A ceiling that has to be re-derived every time
# the fleet grows is a ceiling that will be wrong again, so it is now set
# clear of any near-term catalogue and the budget governs, which is what the
# budget is for.
TARGET_NWP_MAX_SCOPES_PER_POLL = 256
# Wall clock, not a blocker count, is what the advance loop actually has to
# respect: a poll that overruns its interval delays the next one and the sweep
# falls behind the P0 TTL.  Guarding the clock directly lets the loop step over
# a scope whose own source is down instead of treating "blocked" as a proxy for
# "expensive" -- most blocked scopes fail on a single fast HTTP response.
TARGET_NWP_ADVANCE_BUDGET_SEC = TARGET_NWP_POLL_INTERVAL_SEC * 0.5
# How many poll gaps the achieved-cadence reading is taken over.  The median
# of these, not the declared interval, is what the sweep is judged against --
# a single gap is whichever turn the lane happened to win.
NWP_POLL_INTERVAL_SAMPLES = 12


def nwp_sweep_health_for(worker: Any) -> dict[str, Any]:
    """The sweep reading for whichever receipt is being written.

    It has to be attached on every path, not just the one that runs a poll.
    Added on 2026-09-09 to ``run_target_forecast_refresh_cycle`` only, it was
    absent from the in-flight receipt at the other call site -- and a peer
    session reading the status file saw the missing key, concluded the
    forecast lane had taken no writer turn for fifty-five minutes, and
    reported the starvation as getting worse.  The rotation state file said
    otherwise the whole time: generation 36 at 14:05Z, 64 at 14:45Z, advancing
    throughout.

    Third instance of one shape today: a reading published on some paths and
    not others is read as bad news rather than as no news.
    """

    try:
        from bayes_basket_target.production_worker import (  # noqa: PLC0415
            NATIVE_P0_COMPILE_FRESH_SECONDS,
        )

        return nwp_sweep_health(
            catalog_scope_count=len(
                (getattr(worker, "_nwp_rotation_state", None) or {}).get(
                    "catalog_scope_keys"
                ) or ()
            ),
            poll_intervals_sec=getattr(
                worker, "_official_nwp_poll_intervals", ()
            ),
            freshness_sec=NATIVE_P0_COMPILE_FRESH_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 -- telemetry, never a blocker
        # Named, not swallowed.  The first version of this was a bare
        # ``except: pass`` and an empty reading is exactly what got
        # misdiagnosed as a stalled lane.
        return {"state": "unavailable", "reason": f"{type(exc).__name__}:{exc}"}


def nwp_sweep_health(
    *,
    catalog_scope_count: int,
    poll_intervals_sec: Sequence[float],
    freshness_sec: float,
) -> dict[str, Any]:
    """Whether the rotation returns to an event date before its packets expire.

    Measured, not declared.  A date's official-native packets are re-captured
    only when the cursor lands on one of that date's scopes, so the sweep
    period IS the re-capture period -- and the sweep period is the number of
    polls in a lap times the interval the lane actually achieves, which is
    not the interval it asks for.  On 2026-09-09 the declaration was 600s,
    the achieved gap was 27 minutes, and 2026-09-10 went dark for a morning:
    captured 02:42Z, expired 08:42Z, next turn still not reached at 12:20Z,
    45 stations fail-closed on native_ensemble_snapshot_stale while every
    constant in the file read as correct.

    Reported rather than enforced.  The lane cannot make itself win turns,
    so the honest thing is to say how long its lap has actually become and
    let that be visible before a date goes dark, not after.
    """

    scopes = max(0, int(catalog_scope_count or 0))
    per_poll = _nwp_scopes_per_poll(scopes)
    polls = -(-scopes // per_poll) if scopes else 0
    observed = sorted(
        float(v) for v in poll_intervals_sec or () if float(v) > 0.0
    )
    achieved_interval = (
        observed[len(observed) // 2] if observed
        else float(TARGET_NWP_POLL_INTERVAL_SEC)
    )
    nominal_sweep = polls * float(TARGET_NWP_POLL_INTERVAL_SEC)
    achieved_sweep = polls * achieved_interval
    return {
        "schema": "bayes_basket_target_nwp_sweep_health_v1",
        "catalog_scope_count": scopes,
        "scopes_per_poll": per_poll,
        "polls_per_sweep": polls,
        "poll_interval_nominal_sec": float(TARGET_NWP_POLL_INTERVAL_SEC),
        "poll_interval_achieved_sec": round(achieved_interval, 1),
        "poll_interval_sample_count": len(observed),
        "sweep_nominal_hours": round(nominal_sweep / 3600.0, 2),
        "sweep_achieved_hours": round(achieved_sweep / 3600.0, 2),
        "p0_freshness_hours": round(float(freshness_sec) / 3600.0, 2),
        # Half the window, so a lap the lane loses a turn in still leaves a
        # whole re-capture opportunity inside the TTL.
        "fits_freshness_window": achieved_sweep <= float(freshness_sec) / 2.0,
        "contention_factor": round(
            achieved_interval / float(TARGET_NWP_POLL_INTERVAL_SEC), 2
        ),
    }


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
# The WAL line the capacity card warns from, and the line the daemon's own
# truncating checkpoint is attempted from -- one number, because a WAL at
# or past it means that attempt is not landing (some reader holds it).
TARGET_SSOT_WARN_WAL_BYTES = 256 * 1024 * 1024
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
        # A writer that could not be opened at all is the same class of fault
        # and has the same remedy -- rebuild the handles -- but it arrives by
        # a different door: the open happens once, before the loop, so nothing
        # in the cycle ever retries it.  Only the transient bounds qualify;
        # see TARGET_RESOURCE_REOPEN_REASONS.
        if "open_failed" in reason and any(
            marker in reason
            for marker in TARGET_RESOURCE_REOPEN_REASONS + TARGET_SCHEMA_REOPEN_REASONS
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


#: How few days of headroom is a warning, whatever the byte count says.
#:
#: Two days is a working day plus a night: long enough that the alarm is
#: not noise, short enough that someone can still choose what to retire
#: rather than having every writer fail closed on them.
TARGET_SSOT_WARN_DAYS_TO_CEILING = 2.0
#: Where the last size reading is kept, so the rate survives a restart.
#: Held in memory it would be unknown for the first pass after every
#: restart, and this process is restarted several times an hour.
TARGET_SSOT_GROWTH_PATH = ROOT / "data" / "runtime" / "target_ssot_growth.json"
#: Where the writer lanes' turn is kept, so a restart does not hand every
#: lane back its tie.  See the seeding comment in the run loop.
WRITER_TURN_PATH = ROOT / "data" / "runtime" / "writer_turn.json"
#: When this generation of the process began.  Stamped at import because
#: that IS the generation: the daemon is a fresh interpreter on every
#: restart, and the growth estimator needs to know whether the window it
#: is about to measure has a restart inside it.
_PROCESS_STARTED_AT_UTC = (
    datetime.now(timezone.utc).isoformat(timespec="seconds")
    .replace("+00:00", "Z")
)


def _resident_started_at_utc() -> str:
    """The start of this generation, for windows that must not span one."""

    return _PROCESS_STARTED_AT_UTC


def _target_ssot_live_bytes(database: Path) -> tuple[int | None, int]:
    """The bytes this database is actually carrying, and its free pages.

    A SQLite file does not shrink when rows are deleted and does not grow
    while free pages are left to reuse, so its SIZE is a step function
    over the thing that matters.  ``(page_count - freelist_count) *
    page_size`` is the thing that matters: it rises with every row written
    whether or not the file moves, and it is what will consume the
    remaining headroom once the free pages are gone.

    Read-only, with a short timeout, and every failure returns "unknown"
    rather than raising: this is a gauge on the resident's hot path.
    """

    try:
        conn = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=2.0
        )
    except sqlite3.Error:
        return None, 0
    try:
        conn.execute("PRAGMA busy_timeout=2000")
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    except (sqlite3.Error, TypeError, ValueError):
        return None, 0
    finally:
        conn.close()
    return max(0, page_count - freelist) * page_size, freelist * page_size


def _target_ssot_growth(
    db_bytes: int,
    *,
    live_bytes: int | None = None,
    free_page_bytes: int = 0,
    generation_started_at: str | None = None,
) -> dict[str, Any]:
    """Bytes per day, and the days that leaves, from two readings.

    Deliberately the crudest possible estimator: this reading against the
    last one on record.  A smoothed rate would be steadier and would also
    have hidden 2026-09-07, when the growth was ordinary for weeks and the
    headroom was not -- what matters is the answer being present at all,
    not its third digit.

    Measured on the LIVE bytes, not on the file.  The first version watched
    the file, and the file is a step function: between 2026-09-08 and
    2026-09-11 it sat at exactly 38,184,861,696 bytes for three days while
    1.4 GB of new rows went into free pages left behind by the retention
    contracts.  ``grew > 0`` was therefore never true, the rate was never
    computed, ``days_to_ceiling`` stayed null for three days -- and the
    two-day warning this function exists to raise cannot fire on a null.
    The gauge was blank for exactly the stretch it was built for.

    It was also frozen: the baseline is only rewritten when a rate is
    computed, so a flat file kept a three-day-old reading forever and the
    first real measurement would have been an average over all of it.  The
    baseline now moves on every reading an hour apart, whether or not it
    produced a rate.

    Headroom counts the free pages, because new rows land there before the
    file has to grow.  The ceiling itself stays on the file size: that is
    what ``MAX_TARGET_DB_BYTES`` gates and what fail-closes every writer.

    A window that spans a restart yields no rate.  Coming up costs a burst
    -- replays, re-ingest, whatever queued while the process was down --
    and this process restarts several times an hour on a deploy day, so
    that burst would be most of what the estimator ever sees.  Measured
    2026-09-10: the first window after two restarts read 2,393 MB/day and
    put the store 5.6 days from its ceiling, while 25 clean minutes on the
    same afternoon read 829 MB/day and 16 days.  A gauge that triples
    after every deploy is one nobody believes on the day it is right.
    """

    now = datetime.now(timezone.utc)
    previous: dict[str, Any] = {}
    try:
        previous = json.loads(TARGET_SSOT_GROWTH_PATH.read_text())
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    # The axis to measure on.  Live bytes when the database could be read,
    # the file when it could not -- an unreadable database still gets the
    # old behaviour rather than no gauge at all.
    measure_now = live_bytes if live_bytes is not None else db_bytes
    measure_key = "live_bytes" if live_bytes is not None else "database_bytes"
    bytes_per_day: float | None = None
    baseline_is_old = False
    try:
        then = datetime.fromisoformat(
            str(previous["observed_at_utc"]).replace("Z", "+00:00")
        )
        elapsed_days = (now - then).total_seconds() / 86400.0
        baseline_is_old = elapsed_days >= 1.0 / 24.0
        # A baseline written before this function measured live bytes has
        # only the file size in it; fall back to that axis for the one
        # comparison rather than reading a missing key as zero growth.
        measure_then = previous.get(measure_key)
        if measure_then is None:
            measure_then = previous.get("database_bytes")
            grew = db_bytes - int(measure_then)
        else:
            grew = measure_now - int(measure_then)
        # Under an hour is not a measurement, it is rounding; a shrinking
        # database is a retention pass, not a rate to extrapolate from;
        # and a window the process restarted inside is a catch-up burst.
        # Compared by identity, not by clock: the baseline records which
        # generation wrote it, so a mismatch IS a restart.  A baseline from
        # before this field existed says nothing about generations and must
        # not be read as claiming one -- unknown is not a restart.
        previous_generation = previous.get("generation_started_at")
        spans_restart = bool(
            generation_started_at
            and previous_generation
            and str(previous_generation) != str(generation_started_at)
        )
        if baseline_is_old and grew > 0 and not spans_restart:
            bytes_per_day = grew / elapsed_days
        restart_in_window = spans_restart
    except (KeyError, TypeError, ValueError):
        bytes_per_day = None
        baseline_is_old = not previous
        restart_in_window = False
    fresh_rate = bytes_per_day
    # Free pages are headroom: new rows land there before the file has to
    # grow.  The ceiling stays on the file, which is what fail-closes the
    # writers, so this only ever adds the slack that genuinely exists.
    remaining = MAX_TARGET_DB_BYTES - db_bytes + max(0, int(free_page_bytes))
    days_to_ceiling = (
        round(remaining / bytes_per_day, 2)
        if bytes_per_day and bytes_per_day > 0 and remaining > 0
        else None
    )
    # Carry the last answer forward.
    #
    # The rate can only be computed against a reading an hour old, and
    # computing it moves the baseline to now -- so the first version
    # reported a number once an hour and ``null`` for the fifty-nine
    # minutes between, which is a gauge that is blank almost always.  The
    # last measurement stands, stamped, until a newer one replaces it: a
    # rate from forty minutes ago is a usable answer and "unknown" is not.
    measured_from = previous.get("observed_at_utc")
    if bytes_per_day is None:
        carried = previous.get("last_growth_bytes_per_day")
        if isinstance(carried, (int, float)) and carried > 0:
            bytes_per_day = float(carried)
            measured_from = previous.get("last_growth_measured_at_utc")
            remaining_now = remaining
            days_to_ceiling = (
                round(remaining_now / bytes_per_day, 2)
                if remaining_now > 0 else None
            )
    # Move the baseline on any reading an hour apart, not only on one that
    # produced a rate.  Rewriting it only on success is what froze the
    # 2026-09-08 reading in place for three days: a flat file yields no
    # rate, no rate meant no write, and no write meant the next comparison
    # was against a baseline that kept getting older.
    if fresh_rate is not None or baseline_is_old or not previous:
        try:
            TARGET_SSOT_GROWTH_PATH.parent.mkdir(parents=True, exist_ok=True)
            TARGET_SSOT_GROWTH_PATH.write_text(json.dumps({
                "schema": "target_ssot_growth_v2",
                "observed_at_utc": now.isoformat(
                    timespec="seconds"
                ).replace("+00:00", "Z"),
                "database_bytes": int(db_bytes),
                "generation_started_at": (
                    str(generation_started_at)
                    if generation_started_at else None
                ),
                "live_bytes": (
                    int(live_bytes) if live_bytes is not None else None
                ),
                "free_page_bytes": int(max(0, free_page_bytes)),
                "last_growth_bytes_per_day": (
                    round(fresh_rate, 1) if fresh_rate is not None
                    else previous.get("last_growth_bytes_per_day")
                ),
                "last_growth_measured_at_utc": (
                    now.isoformat(timespec="seconds").replace("+00:00", "Z")
                    if fresh_rate is not None
                    else previous.get("last_growth_measured_at_utc")
                ),
            }))
        except OSError:  # noqa: PERF203 -- telemetry, never a blocker
            pass
    return {
        "growth_bytes_per_day": (
            round(bytes_per_day, 1) if bytes_per_day is not None else None
        ),
        "days_to_ceiling": days_to_ceiling,
        "growth_measured_from_utc": measured_from,
        # Published so a reader can tell a flat file from a flat database.
        "live_bytes": live_bytes,
        "free_page_bytes": int(max(0, free_page_bytes)),
        "growth_axis": measure_key,
        # Named, because "no rate" and "no rate yet, the process restarted"
        # are different states and only one of them clears on its own.
        "growth_window_spans_restart": bool(restart_in_window),
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

    Bytes were not enough.  On 2026-09-07 this card read
    ``within_budget``, 4.78 GB remaining, no warning -- and the database
    was growing 780 MB a day, which is 6.1 days from the ceiling and about
    two days from the warning line.  "Remaining" answers how much room is
    left; nobody can act on that without also knowing how fast it is
    going, and the growth rate was not published anywhere.  So the probe
    remembers its own last reading and reports the rate and the days it
    implies.  A budget with no time axis is a fuel gauge with no speed.
    """

    database = db_path.expanduser().resolve()
    db_bytes = int(database.stat().st_size) if database.is_file() else 0
    wal = Path(str(database) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    live_bytes, free_page_bytes = _target_ssot_live_bytes(database)
    # The violation stays on the file: ``MAX_TARGET_DB_BYTES`` is what the
    # writers fail closed against, and free pages inside the file do not
    # move it.  Only the time axis below counts them.
    remaining = MAX_TARGET_DB_BYTES - db_bytes
    violations: list[str] = []
    warnings: list[str] = []
    if remaining <= 0:
        violations.append("target_database_bytes_exceeded")
    elif remaining < TARGET_SSOT_WARN_REMAINING_BYTES:
        warnings.append("target_database_headroom_low")
    # The WAL is disk the ceiling above does not see (2026-09-23, user
    # ruling 「加」): a render held its read snapshot for five hours, the WAL
    # grew from its 64 MB limit to 14.9 GB, the disk filled to 285 MiB and
    # the fleet stopped -- and this card read within_budget the whole way,
    # because it measured the file the ceiling gates and nothing else.  Past
    # ``MAX_TARGET_WAL_BYTES`` any writer (re)open fails closed on the
    # truncating checkpoint it cannot take, so that is a violation; from the
    # maintenance trigger up it is a warning that the truncate this daemon
    # attempts every interval is not landing.
    if wal_bytes > MAX_TARGET_WAL_BYTES:
        violations.append("target_wal_bytes_exceeded")
    elif wal_bytes >= TARGET_SSOT_WARN_WAL_BYTES:
        warnings.append("target_wal_bytes_growing")
    growth = _target_ssot_growth(
        db_bytes,
        live_bytes=live_bytes,
        free_page_bytes=free_page_bytes,
        generation_started_at=_resident_started_at_utc(),
    )
    days_left = growth["days_to_ceiling"]
    if (
        days_left is not None
        and days_left <= TARGET_SSOT_WARN_DAYS_TO_CEILING
        and not violations
    ):
        warnings.append("target_database_days_to_ceiling_low")
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
        "warn_wal_bytes": TARGET_SSOT_WARN_WAL_BYTES,
        "max_wal_bytes": MAX_TARGET_WAL_BYTES,
        **growth,
        "warn_days_to_ceiling": TARGET_SSOT_WARN_DAYS_TO_CEILING,
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
# The six-hourly steps of the forecast turn remember their last attempt across
# restarts.  Their due times lived only on the worker, so every restart made
# them due at once and the first forecast turn of every generation ran the
# physics capture (108-178s) and the calibration audit (32-46s) while holding
# the resident's single writer -- six generations by 06:18Z on 2026-09-15, each
# adding about three minutes to a production wait.  Kept beside the rotation
# state under the worker's runtime root; a worker without one keeps the old
# in-memory behaviour and writes nothing.
FORECAST_LANE_CLOCKS_FILENAME = "forecast_lane_clocks.json"


def _forecast_lane_clocks_path(worker: Any) -> Path | None:
    root = getattr(worker, "nwp_runtime_root", None)
    return Path(root) / FORECAST_LANE_CLOCKS_FILENAME if root else None


def forecast_lane_due_mono(
    worker: Any,
    *,
    attr: str,
    clock_name: str,
    interval_sec: float,
) -> float:
    """This step's monotonic due time, seeded from its last persisted attempt."""

    due = getattr(worker, attr, None)
    if due is not None:
        return float(due or 0.0)
    seeded = 0.0
    path = _forecast_lane_clocks_path(worker)
    if path is not None:
        try:
            clocks = json.loads(path.read_text(encoding="utf-8"))
            attempted = str(
                (clocks.get(clock_name) or {}).get("attempted_at_utc") or ""
            )
            if attempted:
                since = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(attempted.replace("Z", "+00:00"))
                ).total_seconds()
                if 0.0 <= since < float(interval_sec):
                    seeded = time.monotonic() + (float(interval_sec) - since)
        except (OSError, ValueError, TypeError, AttributeError):
            seeded = 0.0
    setattr(worker, attr, seeded)
    return seeded


def record_forecast_lane_attempt(worker: Any, *, clock_name: str) -> None:
    """Persist that this step was started now; best effort."""

    path = _forecast_lane_clocks_path(worker)
    if path is None:
        return
    try:
        clocks = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(clocks, dict):
            clocks = {}
    except (OSError, ValueError):
        clocks = {}
    clocks[clock_name] = {"attempted_at_utc": utc_now()}
    try:
        _atomic_json(path, clocks)
    except OSError:
        pass


def forecast_physics_refresh_mode() -> str:
    """``on`` in the resident; a test session sets ``off`` (tests/conftest.py).

    Read at call time rather than at import, because the suite loads this
    module under several names and at different moments, and the switch has
    to hold for all of them.
    """

    return (
        os.environ.get("PM_FORECAST_PHYSICS_REFRESH", "on").strip().lower()
        or "on"
    )
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
    due = forecast_lane_due_mono(
        worker,
        attr="_next_calibration_health_mono",
        clock_name="calibration_health",
        interval_sec=CALIBRATION_HEALTH_INTERVAL_SECONDS,
    )
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
    record_forecast_lane_attempt(worker, clock_name="calibration_health")
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

    # 2026-09-14: this launched the capture script with no ``--db``, so it
    # wrote whatever the script defaults to -- the production SSOT -- from
    # whoever called it.  Three test files drive the forecast refresh cycle
    # with a stand-in worker; each run started a real forward capture against
    # the live store, outside the resident's writer schedule (caught holding
    # the 45GB database open under a pytest process).  The database is now
    # the worker's own, and a worker that has none gets no capture.
    if forecast_physics_refresh_mode() == "off":
        return {
            "state": "disabled",
            "reason": "PM_FORECAST_PHYSICS_REFRESH=off",
            "calls_order_gateway": False,
            "places_orders": False,
        }
    database = getattr(worker, "db_path", None)
    if not database:
        return {
            "state": "skipped_worker_has_no_database",
            "calls_order_gateway": False,
            "places_orders": False,
        }
    now_mono = time.monotonic()
    due = forecast_lane_due_mono(
        worker,
        attr="_next_forecast_physics_mono",
        clock_name="forecast_physics",
        interval_sec=FORECAST_PHYSICS_REFRESH_SECONDS,
    )
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
    record_forecast_lane_attempt(worker, clock_name="forecast_physics")
    started = utc_now()
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "capture_station_forecast_physics.py"),
                "--db", str(database),
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
    # Where the turn's seconds went.  The turn holds the resident's single
    # writer while production waits, and until 2026-09-15 the board could say
    # only "占住" -- not whether the rotation, the TAF round, the physics
    # capture or the calibration audit was holding it.
    step_timings_ms: dict[str, float] = {}
    step_started = time.perf_counter()
    nwp_state: dict[str, Any] = {
        "state": "not_due",
        "calls_order_gateway": False,
        "places_orders": False,
    }
    # Published on every pass, due or not: the reading that matters is how
    # long the lap has become, and a lane that has stopped winning turns is
    # exactly the one whose polls are not due.
    nwp_state["sweep_health"] = nwp_sweep_health_for(worker)
    previous_nwp = getattr(worker, "_last_official_nwp_state", None)
    if isinstance(previous_nwp, Mapping):
        nwp_state["last_attempt"] = dict(previous_nwp)
    next_nwp = float(
        getattr(worker, "_next_official_nwp_poll_mono", 0.0)
    )
    # Charter 195 (二): a run the arrival probe has seen land starts its
    # capture now, on this thread (the rotation's own), not at the next
    # 10-minute NWP poll.
    arrival_state: dict[str, Any] = {"state": "not_run"}
    arrival = getattr(worker, "run_arrival_triggered_captures", None)
    if callable(arrival) and not bool(getattr(worker, "skip_nwp_refresh", False)):
        try:
            arrival_state = dict(arrival(as_of_utc=utc_now()))
        except Exception as exc:  # noqa: BLE001 -- the rotation is the fallback
            arrival_state = {
                "state": "failed",
                "reason": f"{type(exc).__name__}:{exc}"[:200],
            }
    # Charter 195 (四): days that failed on the network are re-queued the
    # moment the egress answers -- required families first.
    recovery_state: dict[str, Any] = {"state": "not_run"}
    recovery = getattr(worker, "run_network_recovery_recaptures", None)
    if callable(recovery) and not bool(getattr(worker, "skip_nwp_refresh", False)):
        try:
            recovery_state = dict(recovery(as_of_utc=utc_now()))
        except Exception as exc:  # noqa: BLE001 -- the rotation is the fallback
            recovery_state = {
                "state": "failed",
                "reason": f"{type(exc).__name__}:{exc}"[:200],
            }
    arrival_state["network_recovery"] = recovery_state
    step_timings_ms["nwp_arrival_triggers"] = round(
        (time.perf_counter() - step_started) * 1000.0, 1
    )
    step_started = time.perf_counter()
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
        # What the interval turned out to be, not what it was set to.
        #
        # The 2026-09-09 outage was diagnosed off the declared 600s and the
        # guard written against it, and the declared number is not the one
        # that failed: this branch is reached only when the forecast lane
        # wins its turn against the production cycle and the hosted sweeps,
        # and the observed gap between two polls that day was 27 minutes --
        # 2.7x the declaration.  A sweep sized against 600s therefore passes
        # every test and still misses the freshness window on the machine.
        last_poll_mono = getattr(worker, "_last_official_nwp_poll_mono", None)
        if isinstance(last_poll_mono, (int, float)):
            observed = float(now_mono) - float(last_poll_mono)
            if 0.0 < observed < 6.0 * 3600.0:
                history = list(
                    getattr(worker, "_official_nwp_poll_intervals", ())
                )
                history.append(round(observed, 1))
                setattr(
                    worker,
                    "_official_nwp_poll_intervals",
                    history[-NWP_POLL_INTERVAL_SAMPLES:],
                )
        setattr(worker, "_last_official_nwp_poll_mono", now_mono)
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
                # One poll's worth of the call's segment spans (``_nwp_lap``).
                setattr(worker, "_stage_spans", {})
                for _ in range(scopes_this_poll):
                    call_started = time.perf_counter()
                    advanced.append(refresh(as_of_utc=utc_now()))
                    add_span = getattr(worker, "_add_span", None)
                    if callable(add_span):
                        add_span(
                            "nwp_call_total",
                            (time.perf_counter() - call_started) * 1000.0,
                        )
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
                nwp_state["call_spans_ms"] = {
                    str(name): {
                        "count": int(row.get("count") or 0),
                        "total_ms": round(float(row.get("total_ms") or 0.0), 1),
                        "max_ms": round(float(row.get("max_ms") or 0.0), 1),
                    }
                    for name, row in sorted(
                        (getattr(worker, "_stage_spans", None) or {}).items()
                    )
                    if isinstance(row, Mapping)
                }
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
    step_timings_ms["resident_nwp"] = round(
        (time.perf_counter() - step_started) * 1000.0, 1
    )
    step_started = time.perf_counter()
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
                # The fast round works off this catalogue between rotations.
                setattr(worker, "_taf_catalog_cache", list(catalog))
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
                # Charter 85 (b) (2026-09-06): the station-days the book
                # holds first, then tomorrow at the TX-issuing stations, then
                # the general least-recently-served queue.
                rotation_held = taf_rotation_held_scope_prefixes(taf_scope_conn)
                rotation_tx_stations = taf_rotation_tx_station_ids(
                    taf_scope_conn, as_of_utc=now
                )
                # When a bulletin last ARRIVED for each scope, which is a
                # different question from when the scope was last handed a
                # slot.  The served ledger answers the second and reported
                # perfect rotation while the first was hours stale: on
                # 2026-09-09 the held pool filled all eight priority slots
                # every round, and the stations outside the classes fell from
                # ten bulletins a day to one.
                rotation_arrivals = taf_rotation_newest_capture_at(
                    taf_scope_conn
                )
                # A TAF is published on the station's schedule, not ours.
                # Measured 2026-08-20..09-09 over 1,108 issuances: 36 of the
                # 45 run a strict six-hourly cadence and eight amend every
                # two to three hours, so a rotation that spreads its turns
                # evenly asks a six-hourly station four times inside one of
                # its cycles and gets the bulletin it already holds three of
                # those times.  Each station's own median gap decides when it
                # is worth asking again.
                rotation_issued = taf_rotation_latest_issued_at(taf_scope_conn)
                rotation_cadence = taf_rotation_issuance_cadence(
                    taf_scope_conn, as_of_utc=now
                )
                selected, next_cursor, catalog_sha = (
                    select_taf_rotation_scopes(
                        catalog,
                        previous=previous_rotation,
                        limit=MAX_TAF_SCOPES_PER_POLL,
                        held_scope_prefixes=rotation_held,
                        tx_station_ids=rotation_tx_stations,
                        as_of_utc=now,
                        newest_capture_at=rotation_arrivals,
                        latest_issued_at=rotation_issued,
                        issuance_cadence_sec=rotation_cadence,
                    )
                )
                # A new bulletin leads the round (user, 2026-09-19:
                # 「taf更新了，没有抓取吗？要提高轮询抓取，快速反应」).  One
                # request asks AWC who holds something newer than we do; those
                # stations' scopes go first, captured the usual way (their own
                # request, their own receipt).  The rotation above still fills
                # the rest.  Measured: LTAC's 22:40Z bulletin sat at AWC for
                # over an hour waiting for the lap.
                bulk_forced = getattr(worker, "_taf_bulk_forced", None)
                if not isinstance(bulk_forced, dict):
                    bulk_forced = {}
                    setattr(worker, "_taf_bulk_forced", bulk_forced)
                held_by_station: dict[str, str] = {}
                for scope_prefix, issued_at in (rotation_issued or {}).items():
                    station_key = str(scope_prefix).split("|", 1)[0].upper()
                    if str(issued_at) > held_by_station.get(station_key, ""):
                        held_by_station[station_key] = str(issued_at)
                bulk_newer, bulk_receipt = awc_newer_taf_stations(
                    [scope.get("station_id") for scope in catalog],
                    held_issued_at=held_by_station,
                    already_forced=bulk_forced,
                )
                if bulk_newer:
                    lead = [
                        scope for scope in catalog
                        if str(scope.get("station_id") or "").upper() in bulk_newer
                    ]
                    lead_keys = {taf_rotation_scope_key(scope) for scope in lead}
                    selected = lead + [
                        scope for scope in selected
                        if taf_rotation_scope_key(scope) not in lead_keys
                    ]
                    bulk_forced.update(bulk_newer)
                captures: list[dict[str, Any]] = []
                capture_failures: list[dict[str, str]] = []
                outcomes: list[dict[str, str]] = []
                # Only the scopes actually attempted get stamped as served, so
                # a round cut short by the budget leaves its remainder at the
                # front of the next one rather than skipping it.  Selecting
                # twelve and attempting four would otherwise be the old
                # starvation wearing a bigger number.
                attempted: list[Mapping[str, Any]] = []
                round_deadline = (
                    time.monotonic() + TARGET_TAF_ROUND_BUDGET_SEC
                )
                # One request per station per round; a station's other days
                # are parsed from the same response.  See
                # ``capture_forward_awc_taf``.
                station_responses: dict[str, object] = {}
                for scope in selected:
                    # Always attempt the first, whatever the clock says: a
                    # budget that can refuse every scope makes no progress at
                    # all, which is worse than a long turn.
                    if attempted and time.monotonic() >= round_deadline:
                        break
                    attempted.append(scope)
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
                                response_cache=station_responses,
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
                if captures:
                    with _TAF_INBOX_LOCK:
                        inbox = refresh_forward_taf_inbox(
                            captures=captures,
                            inbox_path=worker.taf_inbox_path,
                        )
                else:
                    inbox = None
                rotation = build_taf_rotation_state(
                    catalog=catalog,
                    selected=attempted,
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
                    # What the round did, then what it was offered.  A round
                    # the budget cut short says so here rather than reporting
                    # a full sweep it did not run.
                    "selected_scope_count": len(attempted),
                    "selected_scope_keys": [
                        taf_rotation_scope_key(scope)
                        for scope in attempted
                    ],
                    "nominated_scope_count": len(selected),
                    "deferred_scope_count": len(selected) - len(attempted),
                    # Charter 85 (b): how this lap was filled.
                    "priority_classes": {
                        taf_rotation_scope_key(scope): int(
                            scope.get(
                                "rotation_priority_class",
                                TAF_PRIORITY_GENERAL,
                            )
                        )
                        for scope in attempted
                    },
                    # How long each catalogued scope has gone without a
                    # bulletin.  Nothing measured this until 2026-09-09, and
                    # the absence is why a lane that had stopped delivering
                    # for sixteen of forty-three stations -- KLGA ten hours
                    # silent, KMIA nine and a half against its own roughly
                    # three-hourly issuance -- showed up nowhere except as a
                    # probability-stage refusal two layers downstream.
                    #
                    # Measured after the round, with its own captures folded
                    # in: the maps above were read before it, and the capture
                    # table only learns of this round's bulletins at the next
                    # production ingest, so a receipt read off them alone
                    # reported the round's own catches as still missing.
                    # The one-request probe that let new bulletins lead.
                    "bulk_probe": bulk_receipt,
                    "capture_freshness": _taf_capture_freshness(
                        catalog,
                        newest_capture_at=_fold_round_captures(
                            rotation_arrivals, captures,
                            field="local_received_at_utc",
                        ),
                        latest_issued_at=_fold_round_captures(
                            rotation_issued, captures,
                            field="issued_at_utc",
                        ),
                        capture_lag=taf_rotation_capture_lag(
                            taf_scope_conn, as_of_utc=now
                        ),
                        as_of_utc=now,
                    ),
                    "held_scope_count": len(rotation_held),
                    "tx_station_count": len(rotation_tx_stations),
                    "round_budget_sec": TARGET_TAF_ROUND_BUDGET_SEC,
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
    step_timings_ms["forward_taf"] = round(
        (time.perf_counter() - step_started) * 1000.0, 1
    )
    step_started = time.perf_counter()
    forecast_physics_state = _refresh_forecast_physics(worker)
    step_timings_ms["forecast_physics"] = round(
        (time.perf_counter() - step_started) * 1000.0, 1
    )
    step_started = time.perf_counter()
    # Same isolated off-path lane, same contract: read-only, bounded, and
    # unable to block anything it looks at.
    calibration_health_state = _audit_calibration_health(worker)
    step_timings_ms["calibration_health"] = round(
        (time.perf_counter() - step_started) * 1000.0, 1
    )
    return {
        "schema": "reported_trajectory_forecast_prefetch_v1",
        "step_timings_ms": step_timings_ms,
        "resident_nwp_capture": nwp_state,
        "nwp_arrival_triggers": arrival_state,
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


def run_nwp_arrival_probe_round(
    *, runtime_root: Path = DEFAULT_TARGET_NWP_RUNTIME_ROOT
) -> dict[str, Any]:
    """Charter 195: one probe round; published, never raised."""

    if not nwp_arrival_probe.probe_enabled():
        return {"state": "disabled"}
    try:
        receipt = nwp_arrival_probe.probe_round(runtime_root=runtime_root)
    except Exception as exc:  # noqa: BLE001 -- a probe failure is a receipt
        return {"state": "failed", "reason": f"{type(exc).__name__}:{exc}"[:200]}
    return {
        "state": "ok",
        **{key: value for key, value in receipt.items() if key != "new_rows"},
        "new_ready_keys": [
            f"{row.get('family')}|{row.get('source')}|{row.get('run_init_utc')}"
            f"|{row.get('event_date')}"
            for row in receipt.get("new_rows") or ()
        ],
    }


def run_fast_taf_round(worker: Any, *, ssot_path: Path) -> dict[str, Any]:
    """Capture only what AWC holds newer than we do; no writer turn needed.

    Reads the catalogue the rotation round last built (``_taf_catalog_cache``
    on the same worker) and the SSOT ``mode=ro`` for what we hold, sends one
    bulk request, captures the stations it names -- each with its own
    request and receipt, as the rotation does -- and appends them to the
    inbox under the lock both rounds share.  An issue time is forced once
    (the rotation's probe and this round share the record).  Never raises.
    """

    started = time.perf_counter()
    receipt: dict[str, Any] = {
        "schema": "bayes_basket_target_fast_taf_round_v1",
        "started_at_utc": utc_now(),
        "calls_order_gateway": False,
        "places_orders": False,
    }
    catalog = list(getattr(worker, "_taf_catalog_cache", None) or [])
    if not catalog:
        receipt["state"] = "no_catalog_yet"
        return receipt
    forced = getattr(worker, "_taf_bulk_forced", None)
    if not isinstance(forced, dict):
        forced = {}
        setattr(worker, "_taf_bulk_forced", forced)
    try:
        conn = sqlite3.connect(f"file:{ssot_path}?mode=ro", uri=True, timeout=5.0)
        try:
            issued = taf_rotation_latest_issued_at(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        receipt.update({"state": "ssot_unreadable", "reason": f"{type(exc).__name__}:{exc}"[:160]})
        return receipt
    held: dict[str, str] = {}
    for scope_prefix, issued_at in (issued or {}).items():
        station = str(scope_prefix).split("|", 1)[0].upper()
        if str(issued_at) > held.get(station, ""):
            held[station] = str(issued_at)
    newer, probe = awc_newer_taf_stations(
        [scope.get("station_id") for scope in catalog],
        held_issued_at=held,
        already_forced=forced,
    )
    receipt["bulk_probe"] = probe
    if not newer:
        receipt.update({"state": "nothing_new",
                        "duration_sec": round(time.perf_counter() - started, 3)})
        return receipt
    forced.update(newer)          # claimed before capture: one attempt per issue
    captures: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    responses: dict[str, object] = {}
    for scope in catalog:
        station = str(scope.get("station_id") or "").upper()
        if station not in newer:
            continue
        try:
            captures.append(capture_forward_awc_taf(
                station_id=station,
                event_date=str(scope["event_date"]),
                settlement_timezone=str(scope["settlement_timezone"]),
                response_cache=responses,
            ))
        except Exception as exc:  # noqa: BLE001 -- one scope's failure is a line
            failures.append({"station_id": station, "event_date": str(scope.get("event_date")),
                             "reason": f"{type(exc).__name__}:{exc}"[:160]})
    if captures:
        with _TAF_INBOX_LOCK:
            refresh_forward_taf_inbox(captures=captures, inbox_path=worker.taf_inbox_path)
    receipt.update({
        "state": "captured" if captures else "nothing_captured",
        "stations": sorted(newer),
        "capture_count": len(captures),
        "failures": failures[:8],
        "duration_sec": round(time.perf_counter() - started, 3),
    })
    return receipt


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
            "sweep_health": nwp_sweep_health_for(worker),
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
        "sweep_health": nwp_sweep_health_for(worker),
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
# Where an overdue cycle's thread stacks are written, and how often.  The
# statement probe names the SQL a wedged thread is inside, and that is not
# always the question: on 2026-09-21 three processes in a row (00:27Z,
# 01:23Z and 04:40Z starts) never closed a cycle, and the probe showed only
# the PRAGMA each cycle thread opened its connection with -- the thread was
# running ten short statements a second, none of them slow, so no statement
# was ever the answer.  py-spy needs root on this host and the logs are
# truncated on restart, so the one reader that can see Python frames is this
# process itself.  Only once a cycle is overdue, and at most every thirty
# seconds: a healthy cycle has nothing to name.
PRODUCTION_OVERDUE_STACKS_PATH = (
    ROOT / "data" / "runtime" / "production_overdue_stacks.json"
)
PRODUCTION_OVERDUE_STACKS_EVERY_SEC = 30.0


def _thread_stacks_snapshot(*, max_frames: int = 30) -> dict[str, Any]:
    """Every live thread's innermost Python frames, read in-process."""

    import traceback  # noqa: PLC0415 -- only on the overdue path

    names = {thread.ident: thread.name for thread in threading.enumerate()}
    threads = []
    for ident, frame in sys._current_frames().items():
        frames = traceback.extract_stack(frame)[-max_frames:]
        threads.append({
            "thread_ident": ident,
            "thread_name": names.get(ident, ""),
            "frames": [
                f"{Path(item.filename).name}:{item.lineno} {item.name}"
                for item in frames
            ],
        })
    return {"schema": "production_overdue_stacks_v1", "threads": threads}
# Where the stage timings of recent cycles outlive the next cycle.  The status
# file keeps one production receipt, so a slow cycle's breakdown is gone the
# moment the next one finishes: on 2026-09-15 the board showed a 1330s cycle
# (probability_p0m 469s against a usual 18-23s) that had already been
# overwritten by a 459s one, and whether it was the first cycle after the
# 01:06Z restart or a regression could not be read back from anywhere.
PRODUCTION_CYCLE_HISTORY_PATH = (
    ROOT / "data" / "runtime" / "production_cycle_history.jsonl"
)
PRODUCTION_CYCLE_HISTORY_LINES = 200


def production_cycle_history_row(
    result: "Mapping[str, Any]",
    *,
    cycle_index: int,
    process_started_at_utc: str,
    completed_at_utc: str,
    wait: "Mapping[str, Any] | None" = None,
) -> "dict[str, Any] | None":
    """One finished cycle, as the history keeps it; None without timings."""

    timings = result.get("stage_timings_ms")
    if not isinstance(timings, Mapping):
        return None
    total = timings.get("cycle_total")
    if not isinstance(total, (int, float)):
        return None
    spans = result.get("stage_span_ms")
    top_spans = (
        sorted(
            (
                (str(name), round(float(row.get("total_ms") or 0.0), 1))
                for name, row in spans.items()
                if isinstance(row, Mapping)
            ),
            key=lambda item: -item[1],
        )[:10]
        if isinstance(spans, Mapping) else []
    )
    return {
        "completed_at_utc": completed_at_utc,
        "process_started_at_utc": process_started_at_utc,
        # 1 is the first cycle this process ran: its in-memory caches were
        # empty, which is a different fault from a cycle that grew.
        "cycle_index_since_start": int(cycle_index),
        "state": str(result.get("state") or ""),
        "cycle_total_ms": round(float(total), 1),
        "stages_ms": {
            str(name): round(float(value), 1)
            for name, value in timings.items()
            if name != "cycle_total" and isinstance(value, (int, float))
        },
        "top_spans_ms": dict(top_spans),
        # Charter 148 diagnostics.  ``stages_ms`` is written with sorted
        # keys, so the order the stages ran in was lost and a reader summing
        # it summed an alphabet; the sequence keeps it.  And a span's total
        # alone cannot say whether 595s was one slow call or forty ordinary
        # ones -- the mid-loop quote refresh restates a whole event day each
        # time a station's lease lapses, and the per-cycle count is exactly
        # the number the 2026-09-18 spikes could not be attributed without.
        "stage_sequence": [
            str(name)
            for name in timings
            if name not in ("cycle_total", "unattributed")
            and isinstance(timings.get(name), (int, float))
        ],
        "top_span_detail": (
            {
                str(name): {
                    "count": int(spans[name].get("count") or 0),
                    "max_ms": round(float(spans[name].get("max_ms") or 0.0), 1),
                    "slowest": str(spans[name].get("slowest") or "")[:80],
                }
                for name, _total in top_spans
                if isinstance(spans.get(name), Mapping)
            }
            if isinstance(spans, Mapping) else {}
        ),
        # Charter 177: the SQL the cycle thread spent its time in, costliest
        # first (execute + fetch per statement text).  The spans say which
        # stage was slow; this says which statement inside it.
        "sql_top": [
            {
                "sql": str(row.get("sql") or "")[:240],
                "count": int(row.get("count") or 0),
                "total_ms": round(float(row.get("total_ms") or 0.0), 1),
                "fetch_ms": round(float(row.get("fetch_ms") or 0.0), 1),
            }
            for row in (result.get("statement_ledger_top") or [])[:15]
            if isinstance(row, Mapping)
        ],
        # Charter 180: where the Python time went, sampled all cycle long.
        "py_top": (
            dict(result.get("python_sample_top") or {})
            if isinstance(result.get("python_sample_top"), Mapping)
            else None
        ),
        "waited_sec": (
            wait.get("waited_sec") if isinstance(wait, Mapping) else None
        ),
        "wait_blocked_by": (
            list(wait.get("blocked_by") or [])
            if isinstance(wait, Mapping) else []
        ),
    }


def append_production_cycle_history(
    path: Path,
    row: "Mapping[str, Any]",
    *,
    keep: int = PRODUCTION_CYCLE_HISTORY_LINES,
) -> None:
    """Append one row and keep only the newest ``keep``, replaced atomically."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
    lines = [line for line in lines if line.strip()][-int(keep):]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
# What the cycle is *for*, as opposed to when to alarm about it.
#
# Until now the only number in the file was the overdue bound above, which is
# a failure line.  So the cycle length was emergent -- it re-arms 15 seconds
# after it starts and simply takes as long as the work takes -- and a drift
# from 350s to 850s produced no signal at all until it hit 720.  That drift
# happened on 2026-09-01 and cost the day: at 846s the compile loop outran a
# 60-second quote lease and 21 of 28 scopes died on production_quote_expired.
#
# 300 seconds, from four measurements rather than a preference:
#
#   * the SSOT sweep queue drains one member per production cycle, and its
#     declared demand is 5.71 runs an hour, so anything above 630s starves it
#     -- which is how a ten-minute identity lane went eight hours unserved;
#   * the compile loop's own budget is six quote refreshes of 60 seconds per
#     event day, about 360s, and it is the tail of the cycle -- so the work
#     ahead of it has to fit in what is left;
#   * P1 re-rolls hourly off arriving observations, so 300s is twelve turns
#     inside the cadence it serves;
#   * the WS publisher republishes every 60-100s, so cycling faster than that
#     only re-reads the same book.
#
# Measured floor on 2026-09-01 was 409s, of which probability_p0m was 135.
# The number is a target to be closed, not a claim that it is met.
PRODUCTION_CYCLE_TARGET_SEC = 300.0
# Halfway to the failure line.  Above the target is ordinary variance; above
# this is drift that someone should look at while there is still room.
PRODUCTION_CYCLE_WARN_SEC = 450.0


def run_target_production_cycle(
    worker: TargetProductionWorker,
) -> dict[str, Any]:
    """Consume only durable inbox/SSOT evidence and compile no-live output."""

    started_at_utc = utc_now()
    started_monotonic = time.monotonic()
    # 「秤砣线」at the HEAD of the cycle too (user ruling 2026-09-18 "把优先级
    # 提高"): the lane's retries and any first decision the previous cycle
    # wrote after its tail run are judged now, on the previous cycle's facts,
    # instead of waiting the ten to thirty minutes this cycle takes.  Same
    # writer connection (autocommit), same lane switch, reported under its
    # own key; the tail run below stays.  A failure here is a receipt, and
    # never stops the cycle that follows.
    head_receipt: dict[str, Any] | None = None
    head_connection = getattr(worker, "commit_conn", None)
    if (
        PRODUCTION_EXECUTION_MODE == "production_live"
        and head_connection is not None
        and lane_enabled(LANE_TIEBREAK)
    ):
        try:
            from bayes_basket_target.exit_orders import (
                default_transport as _head_transport,
            )
            from bayes_basket_target.tiebreak_lane import (
                execute_tiebreak_entries as _head_execute,
            )
            head_receipt = _head_execute(
                head_connection,
                getattr(worker, "canonical_source_conn", None),
                transport=_head_transport,
                now_utc=utc_now(),
                live_submit_authorized=True,
            )
        except Exception as exc:
            head_receipt = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    # Charter 166: the tail lane at the head too, for the P1 the previous
    # cycle's last checkpoint wrote.  Its own freshness window (five minutes,
    # the edge's own half-life) decides whether that envelope is still worth
    # acting on; running it here only means the question is asked as early as
    # it can be.  A failure is a receipt, never a stop.
    p1_tail_head_receipt: dict[str, Any] | None = None
    if (
        PRODUCTION_EXECUTION_MODE == "production_live"
        and head_connection is not None
        and lane_enabled(LANE_P1_TAIL)
    ):
        try:
            from bayes_basket_target.exit_orders import (
                default_transport as _tail_head_transport,
            )
            from bayes_basket_target.p1_tail_lane import (
                execute_p1_tail_entries as _tail_head_execute,
            )
            p1_tail_head_receipt = _tail_head_execute(
                head_connection,
                getattr(worker, "canonical_source_conn", None),
                transport=_tail_head_transport,
                now_utc=utc_now(),
                live_submit_authorized=True,
            )
        except Exception as exc:
            p1_tail_head_receipt = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    # Charter 203: the consensus lane at the head too, on the P1 decisions the
    # previous cycle wrote after its tail run.  Not a speed trade -- its own
    # freshness window is twenty minutes -- so the head and the tail are
    # enough.  A failure is a receipt, never a stop.
    p1_consensus_head_receipt: dict[str, Any] | None = None
    if (
        PRODUCTION_EXECUTION_MODE == "production_live"
        and head_connection is not None
        and lane_enabled(LANE_P1_CONSENSUS)
    ):
        try:
            from bayes_basket_target.exit_orders import (
                default_transport as _consensus_head_transport,
            )
            from bayes_basket_target.p1_consensus_lane import (
                execute_p1_consensus_entries as _consensus_head_execute,
            )
            p1_consensus_head_receipt = _consensus_head_execute(
                head_connection,
                getattr(worker, "canonical_source_conn", None),
                transport=_consensus_head_transport,
                now_utc=utc_now(),
                live_submit_authorized=True,
            )
        except Exception as exc:
            p1_consensus_head_receipt = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
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
        # Published, not looked up.  The board reads what the *running*
        # daemon targets rather than what a checked-out constant says, so the
        # two can never drift apart -- which is the same failure as the
        # hard-coded "600s（常态约 200-350）" this replaces, one layer up.
        "target_seconds": PRODUCTION_CYCLE_TARGET_SEC,
        "warn_seconds": PRODUCTION_CYCLE_WARN_SEC,
        "overdue_seconds": PRODUCTION_CYCLE_OVERDUE_SEC,
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
    # The recycle lane (operator ruling 2026-08-31; ruled again 2026-09-18:
    # 「一直挂」, 「改为0.99」): every held leg of a market still trading
    # carries a resting 0.99 sell, which a standing bid at or above it fills
    # at once; the freed capital re-enters the budget.  Runs after the main cycle on the same writer
    # connection, and a failure here is reported on the receipt rather than
    # raised -- a take-profit convenience must not be able to stop the lane
    # that produces the positions it profits from.
    recycle_receipt: dict[str, Any] | None = None
    canonical = getattr(worker, "canonical_source_conn", None)
    if PRODUCTION_EXECUTION_MODE == "production_live" and not lane_enabled(
        LANE_CERTAIN_WIN_RECYCLE
    ):
        # Charter 146: paused by the operator's lane switch, not retired.
        recycle_receipt = {"state": "lane_paused", "lane": LANE_CERTAIN_WIN_RECYCLE}
    elif PRODUCTION_EXECUTION_MODE == "production_live":
        if canonical is not None:
            try:
                from bayes_basket_target.certain_win_recycle import (
                    execute_certain_win_recycles,
                )
                from bayes_basket_target.exit_orders import (
                    default_transport as _sell_transport,
                )
                recycle_receipt = execute_certain_win_recycles(
                    connection,
                    canonical,
                    transport=_sell_transport,
                    now_utc=utc_now(),
                    live_submit_authorized=True,
                )
            except Exception as exc:
                recycle_receipt = {
                    "ok": False,
                    "error": f"{type(exc).__name__}:{exc}",
                }
    # The mis-bought-leg exit (user ruling 2026-09-17: "卖", then "不符合缺腿
    # 条件的全部卖出"): the shares the completion rule bought outside its own
    # definition are sold into the bid, whatever the bid is.  Same writer
    # connection, same sell transport, reported on the receipt rather than
    # raised.
    exit_receipt: dict[str, Any] | None = None
    if PRODUCTION_EXECUTION_MODE == "production_live" and not lane_enabled(
        LANE_MIS_BOUGHT_LEG_EXIT
    ):
        exit_receipt = {"state": "lane_paused", "lane": LANE_MIS_BOUGHT_LEG_EXIT}
    elif PRODUCTION_EXECUTION_MODE == "production_live":
        try:
            from bayes_basket_target.exit_orders import (
                default_transport as _exit_transport,
            )
            from bayes_basket_target.mis_bought_leg_exit import (
                execute_mis_bought_leg_exits,
            )
            exit_receipt = execute_mis_bought_leg_exits(
                connection,
                transport=_exit_transport,
                now_utc=utc_now(),
                live_submit_authorized=True,
            )
        except Exception as exc:
            exit_receipt = {
                "ok": False,
                "error": f"{type(exc).__name__}:{exc}",
            }
    # 「秤砣线」-- the tiebreak lane (charter 146, user ruling 2026-09-18;
    # charter 163, user ruling 2026-09-20): at a stage's first decision, when
    # the market is a coin toss between two adjacent buckets and the model's
    # mode is the second one, bid for the model's bucket -- one resting
    # post-only rung at the best bid plus one tick, the stake's worth of
    # shares, once per station-day (it crossed at the ask until 09-20).  Runs
    # after the main cycle on the same writer connection; reads the fact
    # rows that cycle wrote, so it sees exactly what the decision saw.  A
    # failure is reported on the receipt rather than raised, as the two
    # sell lanes are.
    tiebreak_receipt: dict[str, Any] | None = None
    if PRODUCTION_EXECUTION_MODE == "production_live" and not lane_enabled(
        LANE_TIEBREAK
    ):
        tiebreak_receipt = {"state": "lane_paused", "lane": LANE_TIEBREAK}
    elif PRODUCTION_EXECUTION_MODE == "production_live":
        try:
            from bayes_basket_target.exit_orders import (
                default_transport as _buy_transport,
            )
            from bayes_basket_target.tiebreak_lane import (
                execute_tiebreak_entries,
            )
            tiebreak_receipt = execute_tiebreak_entries(
                connection,
                canonical,
                transport=_buy_transport,
                now_utc=utc_now(),
                live_submit_authorized=True,
            )
        except Exception as exc:
            tiebreak_receipt = {
                "ok": False,
                "error": f"{type(exc).__name__}:{exc}",
            }
    # Charter 166: the tail lane, where P1 is freshest -- the decision loop's
    # own checkpoints (charter 165) have just written it.
    p1_tail_receipt: dict[str, Any] | None = None
    if PRODUCTION_EXECUTION_MODE == "production_live" and not lane_enabled(
        LANE_P1_TAIL
    ):
        p1_tail_receipt = {"state": "lane_paused", "lane": LANE_P1_TAIL}
    elif PRODUCTION_EXECUTION_MODE == "production_live":
        try:
            from bayes_basket_target.exit_orders import (
                default_transport as _tail_transport,
            )
            from bayes_basket_target.p1_tail_lane import execute_p1_tail_entries
            p1_tail_receipt = execute_p1_tail_entries(
                connection,
                canonical,
                transport=_tail_transport,
                now_utc=utc_now(),
                live_submit_authorized=True,
            )
        except Exception as exc:
            p1_tail_receipt = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    # Charter 203: the consensus lane, on the P1 decisions this cycle wrote.
    p1_consensus_receipt: dict[str, Any] | None = None
    if PRODUCTION_EXECUTION_MODE == "production_live" and not lane_enabled(
        LANE_P1_CONSENSUS
    ):
        p1_consensus_receipt = {"state": "lane_paused", "lane": LANE_P1_CONSENSUS}
    elif PRODUCTION_EXECUTION_MODE == "production_live":
        try:
            from bayes_basket_target.exit_orders import (
                default_transport as _consensus_transport,
            )
            from bayes_basket_target.p1_consensus_lane import (
                execute_p1_consensus_entries,
            )
            p1_consensus_receipt = execute_p1_consensus_entries(
                connection,
                canonical,
                transport=_consensus_transport,
                now_utc=utc_now(),
                live_submit_authorized=True,
            )
        except Exception as exc:
            p1_consensus_receipt = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    return {
        **result,
        "post_cycle_resource_guard": {
            "state": "PASS",
            "database": None if guard is None else str(guard),
        },
        "target_cycle_runtime": runtime,
        **({"certain_win_recycle": recycle_receipt} if recycle_receipt else {}),
        **({"mis_bought_leg_exit": exit_receipt} if exit_receipt else {}),
        **({"tiebreak_lane_head": head_receipt} if head_receipt else {}),
        **({"tiebreak_lane": tiebreak_receipt} if tiebreak_receipt else {}),
        **({"p1_tail_lane_head": p1_tail_head_receipt} if p1_tail_head_receipt else {}),
        **({"p1_tail_lane": p1_tail_receipt} if p1_tail_receipt else {}),
        **(
            {"p1_consensus_lane_head": p1_consensus_head_receipt}
            if p1_consensus_head_receipt else {}
        ),
        **({"p1_consensus_lane": p1_consensus_receipt} if p1_consensus_receipt else {}),
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
    # Charter 205: the same descriptions the rows were just built from, read
    # against the verified settlement baseline.  Pure, no network, no write:
    # the blocking half already landed in the rows' block reasons above.
    settlement_source = settlement_source_check(
        [(event_date, event) for _slug, event_date, event in fetched],
        config=config,
    )
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
        "settlement_source": {**settlement_source, "checked_at": refreshed_at},
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
    """Charter 170: ask again on the arrival windows, not on a release clock.

    The burst used to be a 90-second window after :00 and after :30, on the
    premise that those are when reports are released.  Measured over seven
    days of fleet arrivals, that pair of windows covers 11% of what lands:
    the international roster observes at :00/:30 but arrives :03-:07 and
    :33-:37, and the contiguous-US roster observes :51-:58 and arrives
    :52-:59 -- neither peak is inside the old window.  So the burst now
    follows ``awc_fast_lane_plan``, the same three windows the fast lane
    keeps.

    This lane is still bounded by the main loop's turn whatever it returns
    here; charter 170's fast lane is what reaches the network on these
    windows.  Aligning the number keeps the slow lane's fallback pointed at
    the right minutes rather than at :00/:30.  (How long that turn actually
    is was never measured: the 58.6s once cited for it came from the poll
    journal's sixty-second heartbeat, not from the loop -- see the note above
    ``FAST_LANE_US_WINDOW``.)
    """

    policy = config.get("poll_policy", {})
    window, _ = awc_fast_lane_plan(now, policy)
    if window == "cold":
        return max(0.5, float(policy.get("awc_background_poll_sec", 2.0)))
    return max(0.1, float(policy.get("awc_release_burst_retry_sec", 0.25)))


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


# Charter 207: an AWC outage used to lose every report older than the first
# poll back could reach.  The live request asks for all 45 stations at once
# and AWC caps a response at 400 reports, so that poll reaches about 5.5 hours
# back whatever ``hours`` says.  Measured 2026-09-21: AWC went quiet from
# ~15:55Z to 01:24Z, the first poll back ingested exactly 401 reports reaching
# 19:55Z, and KORD's 16:51-19:51Z METARs (19C at 17:51Z) never arrived.  The
# truth lane settled KORD on 17.2C = 63F; the market paid 66-67F.  Same shape
# on 2026-09-09 (16 station-days) -- 35 settled station-days short of their
# afternoon inside AWC's retention alone.
#
# So when the last poll AWC answered is older than a single capped response
# can be trusted to reach, the stations are re-asked in groups small enough
# that no group's answer can hit the cap, over the whole hole.  Same fetch,
# same canonical ingress, same deduplication; a hole of ordinary length (a
# restart, a slow minute) never triggers it.
AWC_CATCHUP_TRIGGER = timedelta(hours=4)
AWC_CATCHUP_MAX_HOURS = 30.0
AWC_RESPONSE_REPORT_CAP = 400
# Routine reports plus SPECI, per station-hour, on the densest day observed
# (RCSS/LTAC ~3.5 at peak); a group is sized so its worst case stays under
# the cap with room, and a group that still comes back full is split.
AWC_CATCHUP_REPORTS_PER_STATION_HOUR = 3.5
AWC_CATCHUP_SAFE_REPORTS = 350


def _last_successful_awc_poll_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute(
        """SELECT response_completed_at FROM reported_trajectory_source_polls
            WHERE source_lane='awc' AND report_count>0
              AND COALESCE(error_code,'')=''
            ORDER BY response_completed_at DESC LIMIT 1"""
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
    except ValueError:
        return None


def _stamp_awc_poll(poll: dict[str, Any], stations: Sequence[str]) -> dict[str, Any]:
    poll["source_lane"] = "awc"
    poll["transport"] = str(poll.get("transport") or "official_awc")
    poll["fallback_reasons"] = {}
    # Charter 162: name the door the reports actually came through, so a
    # TGFTP poll is not filed under the AWC relay that was unreachable.
    provider_id = (
        "global_noaa_tgftp_relay"
        if poll["transport"].startswith("official_noaa_tgftp_")
        else "global_noaa_awc_relay"
    )
    poll["provider_states"] = [{
        "provider_id": provider_id,
        "stations": sorted(stations),
        "state": (
            "noaa_tgftp_fallback"
            if provider_id == "global_noaa_tgftp_relay"
            else "awc_global_primary"
        ),
        "report_count": len(poll.get("reports") or []),
        "transport": poll["transport"],
    }]
    poll["router_duration_ms"] = None
    return poll


def awc_outage_catchup(
    ingress: ReportedTrajectoryAwcIngress,
    stations: Sequence[str],
    *,
    previous_success_at: datetime | None,
    main_poll: Mapping[str, Any],
    now: datetime | None = None,
    fetch: Any = None,
) -> dict[str, Any]:
    """Re-ask AWC for a hole longer than one capped response can reach."""

    now = now or datetime.now(timezone.utc)
    if previous_success_at is None:
        return {"state": "no_previous_poll"}
    hole = now - previous_success_at
    if hole <= AWC_CATCHUP_TRIGGER:
        return {"state": "not_needed", "hole_minutes": round(hole.total_seconds() / 60.0, 1)}
    if not main_poll.get("reports") or main_poll.get("station_errors"):
        # AWC is still not answering cleanly; the hole is re-measured from
        # the same previous success on the next poll.
        return {"state": "deferred_source_unhealthy"}
    fetch = fetch or fetch_awc_temperature_reports
    hours = min(AWC_CATCHUP_MAX_HOURS, math.ceil(hole.total_seconds() / 3600.0) + 1.0)
    per_group = max(1, int(AWC_CATCHUP_SAFE_REPORTS // (hours * AWC_CATCHUP_REPORTS_PER_STATION_HOUR)))
    queue = [list(stations[index:index + per_group]) for index in range(0, len(stations), per_group)]
    requests = new_observations = reports = 0
    errors: list[str] = []
    while queue:
        group = queue.pop(0)
        requests += 1
        started = utc_now()
        try:
            answer = _stamp_awc_poll(dict(fetch(group, hours=hours, timeout=20.0)), group)
        except Exception as exc:
            errors.append(f"{','.join(group)}:{type(exc).__name__}")
            continue
        got = answer.get("reports") or []
        if len(got) >= AWC_RESPONSE_REPORT_CAP - 1 and len(group) > 1:
            # Full answer: the cap cut it.  Ask again in halves.
            half = len(group) // 2
            queue[:0] = [group[:half], group[half:]]
            continue
        reports += len(got)
        try:
            ingested = ingress.process_source_poll(
                answer, request_started_at=started, response_completed_at=utc_now(),
            )
        except Exception as exc:
            errors.append(f"{','.join(group)}:ingest:{type(exc).__name__}")
            continue
        new_observations += int(ingested.get("new_observation_count") or 0)
    return {
        "state": "caught_up" if not errors else "partial",
        "previous_success_at": previous_success_at.isoformat(),
        "hole_minutes": round(hole.total_seconds() / 60.0, 1),
        "requested_hours": hours,
        "stations_per_request": per_group,
        "request_count": requests,
        "report_count": reports,
        "new_observation_count": new_observations,
        "errors": errors,
    }


def run_once(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    ingress: ReportedTrajectoryAwcIngress,
    lane: str,
    *,
    stations_override: list[str] | None = None,
    continuity: dict[str, Any] | None = None,
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
    # Charter 207: when this lane last had AWC's whole answer.  The lane
    # keeps its own clock because the METAR fast lane (charter 170) writes
    # receipts for a few stations over a short window, and one of those
    # landing first after an outage must not read as the hole being closed.
    # Only a fresh lane (a restart, ``--once``) falls back to the receipts,
    # read before this poll writes its own.
    previous_success_at = (
        continuity.get("main_lane_ok_at") if continuity else None
    ) or _last_successful_awc_poll_at(conn)
    poll = _stamp_awc_poll(
        dict(fetch_awc_temperature_reports(stations, hours=awc_hours, timeout=8.0)),
        stations,
    )
    response_completed_at = utc_now()
    result = ingress.process_source_poll(
        poll,
        request_started_at=request_started_at,
        response_completed_at=response_completed_at,
    )
    awc_catchup = awc_outage_catchup(
        ingress,
        stations,
        previous_success_at=previous_success_at,
        main_poll=poll,
    )
    if (
        continuity is not None
        and poll.get("reports")
        and not poll.get("station_errors")
        and awc_catchup.get("state") in {"not_needed", "caught_up", "no_previous_poll"}
    ):
        # A partial catch-up leaves the clock where it was, so the next poll
        # asks for the whole hole again rather than for what is left of it.
        continuity["main_lane_ok_at"] = datetime.now(timezone.utc)
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
        "awc_outage_catchup": awc_catchup,
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


# ---------------------------------------------------------------------------
# Charter 170: the METAR fast lane (user ruling 2026-09-20).
#
# The AWC fetch used to reach the network only when the main loop came round
# to it, and this lane was built on a reading that said that wait was most
# of the latency.  **That reading was wrong, and the correction is recorded
# here rather than quietly dropped** (measured 2026-09-21):
# ``reported_trajectory_source_polls`` is not a poll log.  Its writer keeps
# a row only when a poll brings a new report, or a station errors, or sixty
# seconds have passed since the last row (``persist_poll`` in
# ``reported_trajectory_awc_ingress``).  So the "median interval 58.6s" this
# lane was justified with is that sixty-second heartbeat: 39.6% of 4,769
# pre-lane gaps sit in 55-65s.  The slow lane was already polling at its
# configured pace; the wait it was accused of did not exist, and the "about
# 29s of the 94s" that followed from it is withdrawn.
#
# What the lane measurably does: 270 rounds/hour against a design of 287,
# ingress lag p90 0.114s, zero errors over 8 hours -- and no change in the
# observed-to-stored median (US roster 1.42 -> 1.44 min).  The latency is
# upstream: AWC publishes a US report about 72s after its observation time
# (KLGA's 16:51Z report was first servable at 16:52:12Z), and that is the
# part no polling clock can reach.  The lane is kept for what it does buy --
# a fetch that does not stop when the main loop is busy, and a window that
# follows the roster instead of a guess -- at intervals set so it costs
# about what the old cadence cost.  See
# docs/research/NOAA_DOOR_LATENCY_2026-09-20.md for the door comparison that
# closed off the other exit.
#
# The clock follows
# the observing conventions rather than a flat interval.  Measured over seven
# days of fleet arrivals (12,807 reports, ``first_seen_at`` by minute):
#
#   observed :51-:58, arrives :52-:59  the contiguous-US ASOS roster --
#                                      KLGA/KORD :51, KATL :52, KSFO :56,
#                                      KBKF :58, and :53 for KMIA KLAX KSEA
#                                      KDAL KHOU KAUS
#   observed :00/:30, arrives :03-:07  most of the international roster
#                        and :33-:37  (this is the "six-seven minutes after
#                                      the hour" of the 2026-08-29 ruling)
#   observed :20/:50, arrives :22-:26  EDDM EFHK EGLC LIMC LLBG LTAC LTFM
#                        and :52-:56
#
# Only 113 of those 12,807 reports (0.9%) landed in :09-:18 or :39-:48, so
# those two stretches stay cold and the slow lane covers them.
#
# One request carries the whole roster, so there is nothing to gain by asking
# for the US stations alone: splitting them out would be a second round trip,
# not a shorter one.  What their convention buys is the tightest interval of
# the three windows, over the ten minutes their reports actually land.
# The windows below are the fallback, used until the roster's own arrivals
# have been read.  They are the three conventions above, expressed as minutes
# of the hour, and they are what a cold start or an empty archive gets.
FAST_LANE_US_WINDOW = (50, 59)
FAST_LANE_FLEET_WINDOWS = ((0, 8), (19, 27), (29, 38))
# Learned windows are refreshed on this period; a station's filing convention
# is a published schedule, not something that moves within a day.
FAST_LANE_LEARN_REFRESH_SEC = 3600.0
FAST_LANE_LEARN_DAYS = 7
# How much of each station's arrivals the learned minute set must cover.  The
# tail past this is the late/corrected filings, and chasing it would turn the
# window into the whole hour.
FAST_LANE_LEARN_COVERAGE = 0.95
# A station filing hourly lands on three to six minutes once widened, and a
# half-hourly one on six to twelve.  Past this it has no schedule to speak of
# -- KBKF spread over 31 minutes of the hour in the 2026-09-20 reading -- and
# one such station would otherwise drag the whole fleet's window open.  Those
# stations are simply not what the hot windows are for; the cold interval and
# the slow lane still carry them.
FAST_LANE_LEARN_MAX_STATION_MINUTES = 14
# And if even the disciplined stations add up to more than this, the reading
# says nothing useful and the fallback windows stand.  Forty-eight scheduled
# stations across four conventions covered 49 minutes of the hour in the
# 2026-09-20 reading, which is simply what a fleet this spread out looks like
# -- the ceiling is here to catch a degenerate read, not to hold the window
# to some prior idea of how wide it should be.
FAST_LANE_LEARN_MAX_HOT_MINUTES = 56
# The US roster gets the tightest interval, so its minute set is read at a
# tighter coverage too: the 0.95 tail picks up KATL's and KORD's occasional
# :24-:26 filings, and there is no reason to hold the whole fleet at the
# five-second interval for a station's off-schedule minority.
FAST_LANE_LEARN_US_COVERAGE = 0.90


def learn_awc_arrival_minutes(
    conn: sqlite3.Connection,
    stations: Sequence[str],
    *,
    days: int = FAST_LANE_LEARN_DAYS,
    coverage: float = FAST_LANE_LEARN_COVERAGE,
) -> dict[str, Any]:
    """Charter 170: the minutes each station's reports actually land on.

    Every station files on its own published schedule and the fleet holds at
    least four of them (:51-:58 for the contiguous-US ASOS roster, :00/:30,
    :20/:50, and :00 alone), so a hand-written window is a guess that goes
    stale the moment a station is added or moves its filing time.  This reads
    the schedule back off ``first_seen_at`` -- arrival, not observation, so
    the publisher's own delay is already inside the number and no delay has
    to be assumed.

    One index seek per station on
    ``idx_reported_trajectory_observations_station_time``; a week of one
    station's METARs is a few hundred rows.
    """

    floor = (
        datetime.now(timezone.utc) - timedelta(days=int(days))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    per_station: dict[str, list[int]] = {}
    us_station_minutes: dict[str, list[int]] = {}
    for station in sorted({str(s).strip().upper() for s in stations if str(s).strip()}):
        try:
            rows = conn.execute(
                "SELECT CAST(substr(first_seen_at,15,2) AS INTEGER) AS minute,"
                " COUNT(*) FROM reported_trajectory_observations"
                " WHERE source_lane='awc' AND report_type='METAR'"
                "   AND station_id=? AND valid_at>=?"
                " GROUP BY minute ORDER BY COUNT(*) DESC",
                (station, floor),
            ).fetchall()
        except sqlite3.Error:
            continue
        total = sum(int(count) for _, count in rows)
        if total < 24:  # fewer than a day of hourly filings says nothing yet
            continue
        def _cover(target: float) -> list[int]:
            kept: list[int] = []
            running = 0
            for minute, count in rows:
                kept.append(int(minute))
                running += int(count)
                if running >= target * total:
                    break
            # One minute either side: a report that usually lands on :54 lands
            # on :53 or :55 often enough, and the cost of a spare minute is one
            # extra request in that minute.
            return sorted({(m + off) % 60 for m in kept for off in (-1, 0, 1)})

        per_station[station] = _cover(coverage)
        if icao_to_iem_station(station) is not None:
            us_station_minutes[station] = _cover(FAST_LANE_LEARN_US_COVERAGE)
    if not per_station:
        return {"available": False, "stations": {}, "hot_minutes": [], "us_minutes": []}
    hot: set[int] = set()
    us: set[int] = set()
    unscheduled = sorted(
        station for station, minutes in per_station.items()
        if len(minutes) > FAST_LANE_LEARN_MAX_STATION_MINUTES
    )
    for station, minutes in per_station.items():
        if station in set(unscheduled):
            continue
        hot.update(minutes)
        # The contiguous-US ASOS roster is the one that files in the last ten
        # minutes of the hour; it gets the tightest interval because the user
        # named it (ruling 2026-09-20), and ``icao_to_iem_station`` is already
        # the roster test the MADIS and ASOS lanes use.
        if station in us_station_minutes:
            us.update(us_station_minutes[station])
    if not hot or len(hot) > FAST_LANE_LEARN_MAX_HOT_MINUTES:
        return {
            "available": False,
            "reason": "learned_window_too_wide" if hot else "no_scheduled_station",
            "learned_minute_count": len(hot),
            "stations": {k: v for k, v in sorted(per_station.items())},
            "unscheduled_stations": unscheduled,
            "hot_minutes": [],
            "us_minutes": [],
        }
    return {
        "available": True,
        "learned_at_utc": utc_now(),
        "days": int(days),
        "coverage": float(coverage),
        "station_count": len(per_station),
        "scheduled_station_count": len(per_station) - len(unscheduled),
        "stations": {k: v for k, v in sorted(per_station.items())},
        # Named, not hidden: a station that files on no schedule is covered by
        # the cold interval and the slow lane, and the board should say which
        # ones those are rather than let them look like they are being chased.
        "unscheduled_stations": unscheduled,
        "hot_minutes": sorted(hot),
        "us_minutes": sorted(us),
    }
# Two hours is the slow lane's own floor.  The fast lane never carries the
# settlement-depth lookback (up to 30h): that walk is what the slow lane's
# turn is for, and a fast round asking for it would trade the latency it just
# won for a response thirty times the size.
FAST_LANE_LOOKBACK_HOURS = 2.0


def awc_fast_lane_plan(
    now_utc: datetime,
    policy: Mapping[str, Any],
    learned: Mapping[str, Any] | None = None,
) -> tuple[str, float]:
    """Charter 170: which arrival window we are in, and how soon to look again.

    ``learned`` is ``learn_awc_arrival_minutes``' reading of the roster's own
    filing schedules.  Without it -- a cold start, an empty archive, or the
    slow lane, which has no connection to hand here -- the three fallback
    windows stand.
    """

    minute = int(now_utc.minute)
    us_sec = max(1.0, float(policy.get("awc_fast_us_window_sec", 5.0)))
    fleet_sec = max(1.0, float(policy.get("awc_fast_fleet_window_sec", 15.0)))
    cold_sec = max(5.0, float(policy.get("awc_fast_cold_sec", 60.0)))
    if learned and learned.get("available"):
        if minute in set(learned.get("us_minutes") or ()):
            return "us_hot", us_sec
        if minute in set(learned.get("hot_minutes") or ()):
            return "fleet_hot", fleet_sec
        return "cold", cold_sec
    low, high = FAST_LANE_US_WINDOW
    if low <= minute <= high:
        return "us_hot", us_sec
    for low, high in FAST_LANE_FLEET_WINDOWS:
        if low <= minute <= high:
            return "fleet_hot", fleet_sec
    return "cold", cold_sec


def run_awc_fast_round(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    ingress: ReportedTrajectoryAwcIngress,
    stations: list[str],
    *,
    window: str,
    hours: float = FAST_LANE_LOOKBACK_HOURS,
) -> dict[str, Any]:
    """Charter 170: the AWC fetch and its ingress, and nothing else.

    No MADIS, no ASOS, no soft-carrier materialization, no settlement-depth
    lookback -- those keep the slow lane's turn.  Ingress rows are
    ``INSERT OR IGNORE`` keyed on the raw hash, so a report both lanes see is
    written once, by whichever got there first.
    """

    if not stations:
        return {"ok": True, "state": "no_active_stations", "fast_lane": True}
    if "awc" not in set(config.get("production_source_allowlist") or []):
        raise ValueError("production_source_lane_not_allowlisted")
    request_started_at = utc_now()
    poll = dict(fetch_awc_temperature_reports(stations, hours=hours, timeout=8.0))
    poll["source_lane"] = "awc"
    poll["transport"] = str(poll.get("transport") or "official_awc")
    poll["fallback_reasons"] = {}
    # Charter 162: name the door the reports actually came through, exactly as
    # the slow lane does, so the dashboard lamp reads one vocabulary.
    provider_id = (
        "global_noaa_tgftp_relay"
        if poll["transport"].startswith("official_noaa_tgftp_")
        else "global_noaa_awc_relay"
    )
    poll["provider_states"] = [{
        "provider_id": provider_id,
        "stations": sorted(stations),
        "state": (
            "noaa_tgftp_fallback"
            if provider_id == "global_noaa_tgftp_relay"
            else "awc_global_primary"
        ),
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
    new_count = int(result.get("new_observation_count") or 0)
    # The selector only has something new to decide when a report actually
    # landed.  At five seconds a round through the US window, re-running it on
    # every empty round would spend the whole latency win on bookkeeping.
    selected = 0
    if new_count:
        selector = AsosOneMinuteIngress(conn)
        placeholders = ",".join("?" for _ in stations)
        scopes: dict[tuple[str, str], None] = {}
        for station_id, event_date in conn.execute(
            f"""SELECT DISTINCT station_id,event_date
                  FROM reported_trajectory_market_registry
                 WHERE source_lane='awc' AND source_eligible=1
                   AND market_active=1 AND market_accepting_orders=1
                   AND station_id IN ({placeholders})""",
            stations,
        ):
            scopes[(str(station_id), str(event_date))] = None
        for station_id, event_date in scopes:
            selector.select_canonical_bound(
                station_id=station_id,
                event_date=event_date,
                as_of_utc=response_completed_at,
            )
            selected += 1
        if selected:
            conn.commit()
    return {
        "ok": True,
        "fast_lane": True,
        "window": window,
        "lookback_hours": hours,
        "station_count": len(stations),
        "report_count": len(poll.get("reports") or []),
        "new_observation_count": new_count,
        "canonical_scopes_reselected": selected,
        "transport": poll["transport"],
        "provider_id": provider_id,
        "completed_at": response_completed_at,
    }


class AwcFastLaneWorker:
    """Charter 170: the AWC fetch on its own thread, off the main loop's turn.

    Its own connection, because the slow lane's connection belongs to the main
    loop's thread and SQLite connections are not shared across threads.  The
    thread never touches the order gateway and never writes outside the
    observation tables the ingress already owns.
    """

    def __init__(self, db_path: Path, config: dict[str, Any]) -> None:
        self.db_path = db_path
        self.config = config
        self.conn: sqlite3.Connection | None = None
        self.ingress: ReportedTrajectoryAwcIngress | None = None
        self.stations: list[str] = []
        self.next_station_refresh = 0.0
        self.learned: dict[str, Any] = {"available": False}
        self.next_learn_refresh = 0.0
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {
            "schema": "awc_metar_fast_lane_v1",
            "charter": 170,
            "state": "not_started",
            "rounds": 0,
            "new_observation_count": 0,
            "errors": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.state)

    def take_wake_signal(self) -> bool:
        """True once after a round that landed a new observation.

        The slow lane wakes the entry side lane in-line when its poll returns
        new rows.  This lane is on another thread, so it leaves a flag and the
        main loop picks it up on its next turn -- still bounded by that turn,
        but it no longer also waits out the entry lane's own interval.
        """

        with self.lock:
            pending = bool(self.state.get("wake_pending"))
            if pending:
                self.state["wake_pending"] = False
            return pending

    def _publish(self, **fields: Any) -> None:
        with self.lock:
            self.state.update(fields)

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(
            target=self._run, name="reported-trajectory-awc-fast", daemon=True
        )
        self._publish(state="starting", started_at=utc_now())
        self.thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)
            self.thread = None
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None
        self._publish(state="stopped", stopped_at=utc_now())

    def _ensure_conn(self) -> None:
        if self.conn is None:
            self.conn = _open_runtime(self.db_path)
            self.ingress = ReportedTrajectoryAwcIngress(self.conn, config=self.config)
            self.stations = []
            self.next_station_refresh = 0.0

    def _run(self) -> None:
        policy = self.config.get("poll_policy") or {}
        consecutive_errors = 0
        self._publish(state="running")
        while not self.stop_event.is_set():
            window, interval = awc_fast_lane_plan(
                datetime.now(timezone.utc), policy, self.learned
            )
            started = time.monotonic()
            try:
                self._ensure_conn()
                assert self.conn is not None and self.ingress is not None
                if started >= self.next_station_refresh or not self.stations:
                    self.stations = active_stations(self.conn, "awc")
                    self.next_station_refresh = started + 60.0
                if started >= self.next_learn_refresh and self.stations:
                    self.learned = learn_awc_arrival_minutes(self.conn, self.stations)
                    self.next_learn_refresh = started + FAST_LANE_LEARN_REFRESH_SEC
                    self._publish(arrival_schedule=self.learned)
                    # Re-plan immediately: the first round of a restart would
                    # otherwise spend its interval on the fallback window.
                    window, interval = awc_fast_lane_plan(
                        datetime.now(timezone.utc), policy, self.learned
                    )
                result = run_awc_fast_round(
                    self.conn, self.config, self.ingress, self.stations, window=window,
                )
                consecutive_errors = 0
                with self.lock:
                    self.state["state"] = "running"
                    self.state["rounds"] = int(self.state.get("rounds") or 0) + 1
                    self.state["new_observation_count"] = int(
                        self.state.get("new_observation_count") or 0
                    ) + int(result.get("new_observation_count") or 0)
                    self.state["window"] = window
                    self.state["interval_sec"] = interval
                    self.state["last_round"] = result
                    self.state["last_round_at"] = utc_now()
                    self.state["round_duration_sec"] = round(
                        time.monotonic() - started, 3
                    )
                    # A round slower than its interval is an upstream
                    # statement, not a lane fault; the lamp says so rather
                    # than reading green on a lane that cannot keep its pace.
                    self.state["upstream_slower_than_interval"] = (
                        time.monotonic() - started
                    ) > interval
                    if int(result.get("new_observation_count") or 0) > 0:
                        self.state["wake_pending"] = True
                        self.state["last_new_rows_at"] = utc_now()
            except Exception as exc:  # noqa: BLE001 -- published, never raised
                consecutive_errors += 1
                with self.lock:
                    self.state["errors"] = int(self.state.get("errors") or 0) + 1
                    self.state["last_error"] = f"{type(exc).__name__}:{exc}"[:200]
                    self.state["last_error_at"] = utc_now()
                    self.state["state"] = "degraded"
                # A SQLite I/O error leaves the connection unusable; the slow
                # lane rebuilds its own on the same failure (``reopen``).
                if isinstance(exc, sqlite3.Error) and self.conn is not None:
                    try:
                        self.conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self.conn = None
                    self.ingress = None
                # Back off on a run of failures so a dead upstream is not
                # hammered at five-second intervals.
                interval = min(120.0, interval * min(8, consecutive_errors + 1))
            # Never less than half the interval, even when the round overran
            # it.  A round that takes longer than its own interval means the
            # upstream is slower than the pace we asked for -- AWC sat at 10
            # to 18 seconds a request on 2026-09-20 against a median of 0.13
            # -- and subtracting the overrun would leave the old floor of
            # half a second, i.e. hammering a service that is already
            # struggling with no gap at all.  Backing off to half the
            # interval keeps the pace honest when upstream is healthy and
            # halves it when it is not.
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(interval * 0.5, interval - elapsed))
        self._publish(state="stopped")


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
        self.awc_continuity: dict[str, Any] = {}

    def poll(self) -> dict[str, Any]:
        now = time.monotonic()
        if now >= self.next_station_refresh or not self.stations:
            self.stations = active_stations(self.conn, self.lane)
            self.next_station_refresh = now + 60.0
        return run_once(
            self.conn, self.config, self.ingress, self.lane,
            stations_override=self.stations,
            continuity=self.awc_continuity,
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

    It calls no order gateway.  Until 2026-08-31 it did -- it was the one
    resident component that placed orders, on its own strategy ids and its own
    budget -- and that made it a second execution lane in fact, whatever the
    charter said about the pure YES basket being the only one.  The operator
    settled it: one mainline, the three-stage P0 / P0-M / P1 basket ladder.

    It is kept off the AWC canonical main line on purpose, and that reason
    still stands for the evidence it produces: a main-line fault must block
    only the main line.  Before this worker existed the lane ran at the tail
    of the AWC ingress transaction, and a single recurring ``disk I/O error``
    in AWC ingress silently suppressed every entry fact for the whole event
    day.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.conn = _open_runtime(db_path)

    def run(self) -> dict[str, Any]:
        # Entry facts only.  Operator ruling 2026-08-31: there is no separate
        # ASOS order lane -- the system has one mainline, the three-stage
        # P0 / P0-M / P1 basket ladder.  The order step this worker used to
        # run (``process_pending_argmax_orders``) is not called any more, and
        # the gateway bars its three strategy ids outright, so the ruling does
        # not depend on this call site alone.
        #
        # What ASOS keeps is its real job and the reason this worker exists:
        # it is a canonical observation source feeding P1, partitioned off the
        # AWC main line so an AWC ingress fault cannot silently suppress a
        # whole event day's entry facts the way it did before.
        entry = materialize_argmax_entry_facts(
            self.conn, sorted(ASOS_ARGMAX_LIVE_STATIONS)
        )
        return {
            "ok": bool(entry.get("ok", True)),
            "state": "entry_facts_only_order_lane_retired",
            "entry": entry,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
        }

    def close(self) -> None:
        self.conn.close()

    def reopen(self) -> None:
        self.conn.close()
        self.conn = _open_runtime(self.db_path)


def _execution_mode_status() -> dict[str, Any]:
    """What this resident is actually configured to do, read from the contract.

    The mode comes from the one constant that also selects the activation
    file, so the board cannot report a mode the worker is not validating
    against.  The two boundaries come from the runtime config the mode
    selects; if it cannot be read, they fail closed to False rather than
    claiming an authority nobody proved.
    """

    live = PRODUCTION_EXECUTION_MODE == "production_live"
    submit = gateway = False
    try:
        from bayes_basket_target.runtime_config import (
            load_target_live_runtime_config,
            load_target_no_live_runtime_config,
        )
        config = (
            load_target_live_runtime_config() if live
            else load_target_no_live_runtime_config()
        )
        submit = config.get("live_submit_authorized") is True
        gateway = config.get("calls_order_gateway") is True
    except Exception:
        pass
    return {
        "execution_mode": PRODUCTION_EXECUTION_MODE,
        "live_submit_authorized": submit,
        "calls_order_gateway": gateway,
    }


def _fold_round_captures(
    newest: Mapping[str, str],
    captures: Sequence[Mapping[str, Any]],
    *,
    field: str,
) -> dict[str, str]:
    """``STATION|DATE`` -> newest stamp, with this round's captures included."""

    folded = dict(newest)
    for capture in captures:
        if not isinstance(capture, Mapping):
            continue
        key = (
            f"{str(capture.get('station_id') or '').strip().upper()}"
            f"|{str(capture.get('event_date') or '').strip()}"
        )
        revision = capture.get("forecast_revision")
        stamp = str(
            capture.get(field)
            or (revision.get(field) if isinstance(revision, Mapping) else "")
            or ""
        ).strip()
        if key.startswith("|") or key.endswith("|") or not stamp:
            continue
        if stamp > str(folded.get(key) or ""):
            folded[key] = stamp
    return folded


def _taf_capture_freshness(
    catalog: Sequence[Mapping[str, Any]],
    *,
    newest_capture_at: Mapping[str, str],
    as_of_utc: str,
    latest_issued_at: Mapping[str, str] | None = None,
    capture_lag: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-scope bulletin age, and the worst of it named.

    The rotation already reports which scopes it selected and how it classed
    them, which describes the poll rather than the supply.  A lane can select
    perfectly and still be delivering nothing -- that is exactly what happened
    on 2026-09-09 -- so this reports arrivals: how many tradeable scopes are
    missing a bulletin that should exist, and which ones are worst.

    Age is measured from the newest held bulletin's ISSUE time when that is
    known, and a scope is stale once it passes one scheduled six-hour cycle
    plus an hour (``BULLETIN_OVERDUE_SEC``).  It used to be receipt age against
    three hours, which reads a perfectly served six-hourly station as stale
    for half of every cycle -- the card said "57 of 64 over three hours" on
    2026-09-14 for a lane that was slow, not silent.  Slow is a different
    number, and it is reported beside this one: ``capture_lag``.
    """

    age_map = newest_capture_at
    age_basis = "received"
    if latest_issued_at is not None:
        age_map = {**dict(newest_capture_at), **dict(latest_issued_at)}
        age_basis = "issued"
    ages: list[tuple[str, float]] = []
    for scope in catalog:
        key = (
            f"{str(scope.get('station_id') or '').strip().upper()}"
            f"|{str(scope.get('event_date') or '').strip()}"
        )
        ages.append((
            key,
            taf_rotation_capture_age_seconds(
                age_map,
                station_id=str(scope.get("station_id") or ""),
                event_date=str(scope.get("event_date") or ""),
                as_of_utc=as_of_utc,
            ),
        ))
    finite = sorted(age for _key, age in ages if age != float("inf"))
    # Stale means "had bulletins and stopped getting them".  A scope with no
    # age at all is a different fact and gets its own count below.
    #
    # ``capture_age_seconds`` returns ``inf`` for never-captured, which every
    # threshold comparison reads as stale -- correct for the SCHEDULER, which
    # wants those polled first, and wrong for a report, which was then
    # printing them as "gone more than three hours without a bulletin".
    # Measured 2026-09-10: the card read "88/111 个 scope 超过 3 小时没有新
    # 报文（中位 0.5h，最久 1.1h）；88 个从未收到过" -- a sentence that
    # contradicts itself in its own parentheses, and turned the lane red at
    # 88/111 while the true count of scopes that had gone three hours without
    # a bulletin was ZERO.  Seventy of the eighty-eight were tomorrow and the
    # day after, which have never been captured by definition.
    stale = [
        item for item in ages
        if item[1] != float("inf")
        and item[1] >= TAF_BULLETIN_OVERDUE_SEC
    ]
    never_by_date: dict[str, int] = {}
    scopes_by_date: dict[str, int] = {}
    for key, age in ages:
        event_date = key.split("|", 1)[1] if "|" in key else ""
        scopes_by_date[event_date] = scopes_by_date.get(event_date, 0) + 1
        if age == float("inf"):
            never_by_date[event_date] = never_by_date.get(event_date, 0) + 1
    return {
        "schema": "bayes_basket_target_taf_capture_freshness_v1",
        "scope_count": len(ages),
        "never_captured_count": sum(
            1 for _key, age in ages if age == float("inf")
        ),
        # Which days those are.  A day entering the catalogue is entirely
        # never-captured and says nothing about supply; the day being traded
        # having none is the thing worth a light.
        "never_captured_by_event_date": dict(sorted(never_by_date.items())),
        # The denominator that goes with it, so a reader can say "14 of 29"
        # rather than "14 of something".
        "scope_count_by_event_date": dict(sorted(scopes_by_date.items())),
        # The denominator the stale share belongs over: scopes that have an
        # age at all.  Sharing a denominator with the never-captured made
        # the share track how many future days the catalogue held.
        "measured_scope_count": len(finite),
        "stale_scope_count": len(stale),
        "stale_after_hours": round(TAF_BULLETIN_OVERDUE_SEC / 3600.0, 2),
        # What the ages below are measured from: the newest held bulletin's
        # issue time, or -- where that is not known -- our receipt of it.
        "age_basis": age_basis,
        "capture_lag": dict(capture_lag or {}),
        "median_age_hours": (
            round(finite[len(finite) // 2] / 3600.0, 2) if finite else None
        ),
        "max_age_hours": (
            round(finite[-1] / 3600.0, 2) if finite else None
        ),
        # Named, not counted: "sixteen scopes are stale" sends the reader to
        # a table, and the station is the thing that gets fixed.
        #
        # Never-captured scopes are listed apart rather than sorted to the
        # top.  A day entering the catalogue has never been captured by
        # definition -- 36 of 95 scopes on 2026-09-09, all of them tomorrow --
        # and an infinite age at the head of the worst list buries the thing
        # this is for: a scope that used to arrive and stopped.
        "worst_scopes": [
            {"scope": key, "age_hours": round(age / 3600.0, 2)}
            for key, age in sorted(
                (item for item in ages if item[1] != float("inf")),
                key=lambda item: -item[1],
            )[:8]
        ],
        "never_captured_scopes": [
            key for key, age in sorted(ages) if age == float("inf")
        ][:8],
    }


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
        # Read, not asserted.  These three were literals, and after the
        # 2026-08-30 go-live migration the status board went on reporting
        # production_no_live at a system running live -- a board that states
        # what the code once was rather than what it is now is worse than no
        # board, because it is the first thing anybody checks.
        **_execution_mode_status(),
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
            # A cycle in flight is asked to stop at its next scope boundary.
            # Without this, every restart (nineteen on 2026-09-02, all from
            # concurrent sessions redeploying) waited on ``executor.shutdown``
            # for a cycle that runs seven minutes, launchd's exit timeout
            # expired first, and the process died on SIGKILL -- mid-write,
            # hosted sweeps orphaned on the write lock, status never
            # finalised.  The workers are bound later in this scope; a signal
            # that lands before they exist has nothing to interrupt.
            for worker_name in ("production_worker", "forecast_worker"):
                try:
                    resident_worker = locals_workers.get(worker_name)
                except Exception:  # noqa: BLE001 -- signal context
                    resident_worker = None
                if resident_worker is not None:
                    try:
                        resident_worker.request_stop()
                    except Exception:  # noqa: BLE001 -- signal context
                        pass

        locals_workers: dict[str, Any] = {}
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
        # The fast TAF round: network and one file, no writer turn.
        fast_taf_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="reported-trajectory-taf-fast",
        )
        fast_taf_future: Any = None
        next_fast_taf = 0.0
        # Charter 195: the arrival probe, on its own thread.
        nwp_probe_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="reported-trajectory-nwp-arrival-probe",
        )
        nwp_probe_future: Any = None
        next_nwp_probe = 0.0
        argmax_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="reported-trajectory-asos-argmax",
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
        # Charter 170: the METAR fetch off this loop's turn entirely.  The slow
        # lane keeps its turn, its settlement-depth lookback, and the MADIS and
        # ASOS polls that ride with it.
        awc_fast_worker = (
            AwcFastLaneWorker(args.db, config)
            if "awc" in enabled_lanes
            and str(
                (config.get("poll_policy") or {}).get("awc_fast_lane", "on")
            ).strip().lower() not in {"off", "0", "false", "no"}
            else None
        )
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
        locals_workers["production_worker"] = production_worker
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
        locals_workers["forecast_worker"] = forecast_worker
        lane_futures: dict[str, Any] = {}
        lane_recovery_attempts = {lane: 0 for lane in lane_workers}
        production_future: Any = None
        # Initialised beside the future so the in-flight publisher below can
        # read them on the very first pass.
        production_submitted_mono: float = time.monotonic()
        # When an overdue cycle last had its thread stacks written.
        overdue_stacks_written_mono: float = 0.0
        production_submitted_at: str = ""
        # The wait BEFORE a cycle, which nothing measured until 2026-09-09.
        # Stage timings answer "where did the 533 seconds go"; they cannot
        # answer "why were the cycles 40 minutes apart", and that is the
        # larger number.  ``None`` means production is not waiting: either it
        # is running, or it is not due yet.
        production_due_since_mono: float | None = None
        production_wait_blockers: list[str] = []
        forecast_future: Any = None
        registry_future: Any = None
        argmax_future: Any = None
        argmax_entry_future: Any = None
        production_recovery_attempts = 0
        production_lock_contention_attempts = 0
        production_cycles_completed = 0
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
        # turn it is when more than one is due.
        #
        # Seeded from disk, not from the clock.  All-equal-at-boot meant
        # every restart handed the lanes back their tie, and a lane that
        # had been waiting hours started level with the one that had just
        # run.  Measured 2026-09-07: ten of fifteen hosted sweeps had not
        # completed for 17 to 32 hours, across six restarts in an
        # afternoon -- the sweeps' *clocks* were persisted in 2026-08-30
        # for exactly this reason, but their *turn* was not, so the fix
        # only ever covered which member ran, never whether the lane got
        # a slot.  Same judgement as the listing turn (charter 48).
        #
        # Kept as an age in seconds rather than a monotonic stamp, because
        # monotonic clocks do not survive a process.
        writer_last_served = {lane: now_mono for lane in WRITER_LANES}
        try:
            _turn_ages = json.loads(WRITER_TURN_PATH.read_text())
            _served_at = datetime.fromisoformat(
                str(_turn_ages["observed_at_utc"]).replace("Z", "+00:00")
            )
            _off = (
                datetime.now(timezone.utc) - _served_at
            ).total_seconds()
            for _lane, _age in (_turn_ages.get("age_seconds") or {}).items():
                if _lane in writer_last_served:
                    # A lane last served N seconds before the file was
                    # written is now N + (time since) seconds ago.  Bounded
                    # to a day so a long outage cannot make one lane
                    # infinitely privileged forever after.
                    writer_last_served[_lane] = now_mono - min(
                        86_400.0, max(0.0, float(_age) + _off)
                    )
        except (OSError, ValueError, KeyError, TypeError):
            pass

        def _persist_writer_turn() -> None:
            """Record how long ago each lane ran, so a restart resumes."""

            reference = time.monotonic()
            try:
                WRITER_TURN_PATH.parent.mkdir(parents=True, exist_ok=True)
                WRITER_TURN_PATH.write_text(json.dumps({
                    "schema": "writer_turn_v1",
                    "observed_at_utc": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ).replace("+00:00", "Z"),
                    "age_seconds": {
                        lane: round(reference - served, 1)
                        for lane, served in writer_last_served.items()
                    },
                }))
            except OSError:  # noqa: PERF203 -- a turn, not a fact
                pass
        # Which lane took the most recent writer slot, for the production
        # floor below: a maintenance lane may not take two in a row while
        # production is due.
        last_writer_slot = ""
        next_storage_probe = 0.0
        next_wal_maintenance = 0.0
        next_target_wal_maintenance = 0.0
        # Hosted sweeps: seeded from each member's persisted last completion
        # when one exists, so a restart no longer resets who is overdue; a
        # member without a record keeps the staggered first run so boot does
        # not fire the whole roster into the first idle gap at once.
        try:
            _sweep_clocks = json.loads(
                HOSTED_SWEEP_CLOCKS_PATH.read_text()
            )
            if not isinstance(_sweep_clocks, dict):
                _sweep_clocks = {}
        except (OSError, ValueError):
            _sweep_clocks = {}
        cma_probe_honoured: dict[str, str] = {}
        next_hosted_sweep = {
            name: seed_hosted_sweep_clock(
                now_mono=now_mono,
                now_wall_utc=utc_now(),
                completed_at_utc=(
                    (_sweep_clocks.get(name) or {}).get("completed_at_utc")
                    if isinstance(_sweep_clocks.get(name), dict) else None
                ),
                attempted_at_utc=(
                    (_sweep_clocks.get(name) or {}).get("attempted_at_utc")
                    if isinstance(_sweep_clocks.get(name), dict) else None
                ),
                interval_sec=hosted_sweep_interval_after(
                    hosted_sweep_boot_outcome(_sweep_clocks.get(name)),
                    interval_sec,
                ),
                stagger_sec=60.0 + 90.0 * index,
            )
            for index, (name, _, interval_sec, _) in enumerate(HOSTED_SWEEPS)
        }
        # Keyed by sweep name so several may be in flight at once, at most one
        # per write domain.  ``running`` stays a list for the same reason.
        hosted_sweep_futures: "dict[str, Any]" = {}
        state["hosted_sweeps"] = {
            "schema": "resident_hosted_sweeps_v2",
            "running": [],
            "results": {},
            # When each running sweep was handed its slot (2026-09-14), so
            # the board can say how far a running lane is into its timeout
            # instead of only that it is running.
            "started_at": {},
        }
        storage_guard: dict[str, Any] = {
            "ok": False,
            "state": "storage_probe_not_run_fail_closed",
        }
        # Charter 170: start the METAR fast lane before the first turn of the
        # loop, because the whole point is that its clock is not this one.
        if awc_fast_worker is not None:
            awc_fast_worker.start()
            state["awc_fast_lane"] = awc_fast_worker.snapshot()
        maintenance_supervisor = (
            MaintenanceWorkerSupervisor(archive_db=Path(args.db))
            if production_worker.commit_error is None
            and getattr(production_worker, "commit_conn", None) is not None
            else None
        )
        next_maintenance_tick = 0.0
        while not stopping:
            now_mono = time.monotonic()
            if (
                maintenance_supervisor is not None
                and now_mono >= next_maintenance_tick
            ):
                state["maintenance_worker"] = maintenance_supervisor.tick()
                next_maintenance_tick = now_mono + 5.0
            if awc_fast_worker is not None:
                state["awc_fast_lane"] = awc_fast_worker.snapshot()
                # A report the fast lane landed may be the station-day high
                # that authorizes an entry.  The slow lane wakes the entry side
                # lane in-line for exactly this; the fast lane leaves a flag
                # and it is honoured here.
                if awc_fast_worker.take_wake_signal():
                    next_argmax_entry = min(next_argmax_entry, time.monotonic())
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
                    ) >= TARGET_SSOT_WARN_WAL_BYTES
                    and now_mono >= next_target_wal_maintenance
                    and production_future is None
                    and not writer_excluding_sweeps(hosted_sweep_futures)
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
                    if (
                        time.monotonic() - overdue_stacks_written_mono
                        >= PRODUCTION_OVERDUE_STACKS_EVERY_SEC
                    ):
                        overdue_stacks_written_mono = time.monotonic()
                        try:
                            stacks = _thread_stacks_snapshot()
                            stacks["captured_at_utc"] = utc_now()
                            stacks["cycle_submitted_at_utc"] = (
                                production_submitted_at
                            )
                            stacks["elapsed_sec"] = state[
                                "production_inflight"
                            ]["elapsed_sec"]
                            _atomic_json(PRODUCTION_OVERDUE_STACKS_PATH, stacks)
                            state["production_inflight"]["stacks_path"] = str(
                                PRODUCTION_OVERDUE_STACKS_PATH
                            )
                        except Exception as exc:  # diagnosis must not stop a cycle
                            state["production_inflight"]["stacks_error"] = (
                                f"{type(exc).__name__}:{exc}"[:160]
                            )
            elif production_future is None:
                state.pop("production_inflight", None)
                # Not running is not the same as nothing to report.  A cycle
                # that cannot start is invisible on a card that only times
                # the cycles that did.
                if production_due_since_mono is not None:
                    state["production_waiting"] = {
                        "waiting_sec": round(
                            time.monotonic() - production_due_since_mono, 1
                        ),
                        "blocked_by": list(production_wait_blockers),
                    }
                else:
                    state.pop("production_waiting", None)
            # The model-capture lane's verdict.  Every poll
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
            # Every open date's newest capture outcome, not only the scope
            # the last poll happened to end on.  ``official_nwp_cycle`` above
            # is one receipt per poll, and the worker used to keep one slot
            # for all dates, so a pass that died for one date was invisible
            # the moment another date's pass finished (2026-09-05, the
            # 2026-09-07 d2 pass).  Read under a copy: the rotation thread
            # mutates the mapping while this thread publishes it.
            try:
                capture_by_date = dict(
                    getattr(
                        forecast_worker,
                        "_official_native_capture_last_result_by_date",
                        None,
                    ) or {}
                )
            except RuntimeError:
                capture_by_date = {}
            if capture_by_date:
                state["official_native_capture_by_date"] = {
                    str(day): dict(result)
                    for day, result in sorted(capture_by_date.items())
                    if isinstance(result, Mapping)
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
                    production_cycles_completed += 1
                    history_row = production_cycle_history_row(
                        production_result,
                        cycle_index=production_cycles_completed,
                        process_started_at_utc=str(state.get("started_at") or ""),
                        completed_at_utc=utc_now(),
                        wait=state.get("production_wait"),
                    )
                    if history_row is not None:
                        try:
                            append_production_cycle_history(
                                PRODUCTION_CYCLE_HISTORY_PATH, history_row
                            )
                        except OSError:
                            pass
                except Exception as exc:
                    state["production"] = {"ok": False, "state": "blocked_fail_closed", "reason": f"{type(exc).__name__}:{exc}", "places_orders": False, "calls_order_gateway": False, "checked_at": utc_now()}
                production_future = None
                production_submitted_mono = time.monotonic()
                production_submitted_at = ""
                next_production = max(
                    next_production, time.monotonic() + WRITER_YIELD_SEC
                )
                writer_last_served["production"] = time.monotonic()
                _persist_writer_turn()
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
                and production_future is None
                and not writer_excluding_sweeps(hosted_sweep_futures)
            )
            writer_candidates = []
            if writer_idle:
                if now_mono >= next_production:
                    writer_candidates.append("production")
                if sweeps_due:
                    writer_candidates.append("sweeps")
            # Ties keep this order, so the choice stays deterministic.  The
            # production floor (2026-08-29) is inside ``writer_turn_for``:
            # "longest ago" is fair between lanes, but sweeps are ONE lane
            # with ten-plus members, and once their combined due-rate
            # outruns the drain rate the lane is due at every instant --
            # generation 72534 served sweeps for two straight hours while
            # production got zero slots.  So a maintenance lane never takes
            # two consecutive slots while production is due.
            writer_turn = writer_turn_for(
                writer_candidates, writer_last_served, last_writer_slot
            )
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
            # The dashboard render moved to launchd on 2026-09-09
            # (com.weather.dashboard_render, the same 1800s interval).
            #
            # It was submitted here with a 2400s timeout, and the timeout was
            # the reason the page went stale rather than a guard against it: a
            # render that needs longer is SIGTERMed before it writes, so it
            # writes nothing at all.  Measured that day, a run started
            # 09:24:29Z was killed at 2408s having produced no file, while the
            # page still carried a build from 07:52Z.  It is self-reinforcing
            # too -- a killed render refreshes no cached section, so the next
            # one starts colder and is killed sooner in its own work.
            #
            # A render is a reader.  It holds no write lock, nothing in this
            # loop waits on it, and there is no deadline it can miss that
            # killing it does not miss worse.  Hosting it here bought the
            # three-services charter a fourth timer it did not need; launchd
            # imposes no timeout and the script's own single_instance guard
            # is what keeps a slow render from stacking.
            if (
                storage_ok
                and now_mono >= next_argmax_entry
                and argmax_entry_future is None
            ):
                argmax_entry_future = argmax_entry_executor.submit(
                    argmax_entry_worker.run
                )
                next_argmax_entry = time.monotonic() + argmax_entry_interval_sec
            if storage_ok and now_mono >= next_fast_taf and (
                fast_taf_future is None or fast_taf_future.done()
            ):
                if fast_taf_future is not None:
                    try:
                        state["forward_taf_fast"] = fast_taf_future.result()
                    except Exception as exc:  # noqa: BLE001 -- published, never raised
                        state["forward_taf_fast"] = {
                            "state": "failed", "reason": f"{type(exc).__name__}:{exc}"[:160],
                        }
                fast_taf_future = fast_taf_executor.submit(
                    run_fast_taf_round, forecast_worker, ssot_path=DEFAULT_TARGET_DB,
                )
                next_fast_taf = time.monotonic() + FAST_TAF_ROUND_INTERVAL_SEC
            if storage_ok and now_mono >= next_nwp_probe and (
                nwp_probe_future is None or nwp_probe_future.done()
            ):
                if nwp_probe_future is not None:
                    try:
                        state["nwp_arrival_probe"] = nwp_probe_future.result()
                    except Exception as exc:  # noqa: BLE001 -- published, never raised
                        state["nwp_arrival_probe"] = {
                            "state": "failed", "reason": f"{type(exc).__name__}:{exc}"[:160],
                        }
                nwp_probe_future = nwp_probe_executor.submit(
                    run_nwp_arrival_probe_round,
                )
                next_nwp_probe = time.monotonic() + NWP_ARRIVAL_PROBE_INTERVAL_SEC
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
                            time.monotonic()
                            + hosted_sweep_interval_after(
                                sweep_result, interval_sec
                            )
                        )
                # Only a completed attempt advances the COMPLETION stamp: a
                # member whose run was killed or failed has not succeeded,
                # and the record must keep saying so.  But every attempt --
                # including a killed one -- advances the ATTEMPT stamp, and
                # the boot seed reads the later of the two.  Without that a
                # repeatedly-failing member is maximally overdue at every
                # restart, ranks first, and takes the exclusive write domain
                # again; see ``seed_hosted_sweep_clock`` for the 72-hour
                # instance of exactly that.
                try:
                    _clocks = json.loads(
                        HOSTED_SWEEP_CLOCKS_PATH.read_text()
                    )
                    if not isinstance(_clocks, dict):
                        _clocks = {}
                except (OSError, ValueError):
                    _clocks = {}
                # A restart mid-repair keeps the short delay too
                # (``continue_requested``), and the lease overrun is what
                # names a lane that outgrew its slice.
                _clocks[done_name] = hosted_sweep_clock_entry(
                    _clocks.get(done_name),
                    sweep_result,
                    name=done_name,
                    now_utc=utc_now(),
                )
                try:
                    _atomic_json(HOSTED_SWEEP_CLOCKS_PATH, _clocks)
                except OSError:
                    pass
                del hosted_sweep_futures[done_name]
                state["hosted_sweeps"]["started_at"].pop(done_name, None)
                # Served when a sweep FINISHES, as before: the rotation ranks
                # lanes by how long ago each last completed, so stamping this
                # per finisher keeps the sweeps lane from claiming a fresh
                # turn while its own slower members are still running.  Not
                # for a download capture, which never held the writer.
                if sweep_finish_serves_writer_turn(done_name):
                    writer_last_served["sweeps"] = time.monotonic()
                    _persist_writer_turn()
            state["hosted_sweeps"]["running"] = sorted(hosted_sweep_futures)
            if (
                storage_ok
                # A slot may still admit a sweep whose database nothing running
                # is writing.  The exclusion against production and the
                # prefetch is unchanged: they write the same databases, and
                # whichever starts first owns the writer until it finishes.
                and len(hosted_sweep_futures) < HOSTED_SWEEP_MAX_CONCURRENCY
                and production_future is None
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
                # Ranked by how many of its own intervals a member is late,
                # not by how many seconds.  Those are different questions and
                # only the first is about the contract: a lane on a ten-minute
                # clock that is eight hours behind has missed forty-eight
                # rounds, while an hourly capture eight hours behind has
                # missed eight, and in absolute seconds they look alike.  The
                # SSOT is an exclusive write domain and ten of the thirteen
                # members write it, so the queue drains one per turn and the
                # ordering decides who waits.
                #
                # Measured 2026-09-01: pr12_awc_observation, whose whole job
                # is to canonically identify a METAR so P1 can root on it,
                # went from 2026-08-31T23:25 to 07:19 without completing --
                # eight hours on a ten-minute contract, 48 rounds -- while
                # hourly capture lanes sat 7.4 and 6.8 rounds late and ranked
                # ahead of it on raw seconds. Every P1 committed in that
                # window is permanently without its truth-revision sha,
                # because the envelope table is append-only, and its
                # station-day is refused by the atomic-fact gate until a
                # newer P1 carries one: fifteen stations blocked on
                # target_atomic_lineage_incomplete in a single cycle.
                #
                # The rest of the rule is untouched -- list order still breaks
                # ties, and admission is still by write domain.
                # A CMA cycle the probe saw published makes its capture due
                # now rather than at the end of the capture's interval.
                honour_cma_probe(
                    next_hosted_sweep, now_mono=now_mono,
                    honoured=cma_probe_honoured,
                )
                # Charter 201: the family's global members, the same way.
                honour_deterministic_probe(
                    next_hosted_sweep, now_mono=now_mono,
                    honoured=cma_probe_honoured,
                )
                due = due_hosted_sweeps(
                    now_mono=now_mono,
                    next_due=next_hosted_sweep,
                    skip_names=hosted_sweep_futures,
                )
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
                    state["hosted_sweeps"]["started_at"][name] = utc_now()
                if admitted:
                    state["hosted_sweeps"]["running"] = sorted(
                        hosted_sweep_futures
                    )
            # Who is holding the writer, named while it is still holding it.
            # The board's cycle card reports latency inside the cycle and
            # says nothing about being blocked from starting one, so a lane
            # that took the writer for its whole 1800s subprocess timeout
            # showed up only as a staleness note that named nobody.
            # Measured 2026-09-09: ``capture_noaa_conus_forward`` was killed
            # at 1800s for the third consecutive day and production ran 35 to
            # 53 minutes apart against a 300s target.
            production_ready = (
                storage_ok
                and now_mono >= next_production
                and production_future is None
            )
            production_blockers: list[str] = []
            if production_ready:
                production_blockers.extend(
                    f"sweep:{name}"
                    for name in writer_excluding_sweeps(hosted_sweep_futures)
                )
                if not production_blockers and writer_turn != "production":
                    production_blockers.append(
                        f"writer_turn:{writer_turn or 'none'}"
                    )
            if production_ready and production_blockers:
                if production_due_since_mono is None:
                    production_due_since_mono = now_mono
                # Every blocker seen during this wait, first seen first --
                # the last iteration's alone named the hand-off, not the
                # sweep that held the writer for the previous seven minutes.
                for blocker in production_blockers:
                    if blocker not in production_wait_blockers:
                        production_wait_blockers.append(blocker)
            elif not production_ready:
                production_due_since_mono = None
                production_wait_blockers = []
            if (
                storage_ok
                and now_mono >= next_production
                and production_future is None
                and not writer_excluding_sweeps(hosted_sweep_futures)
                and writer_turn == "production"
            ):
                last_writer_slot = "production"
                production_future = executor.submit(
                    run_target_production_cycle,
                    production_worker,
                )
                production_submitted_mono = time.monotonic()
                production_submitted_at = utc_now()
                state["production_wait"] = {
                    "waited_sec": (
                        0.0
                        if production_due_since_mono is None
                        else round(now_mono - production_due_since_mono, 1)
                    ),
                    "blocked_by": list(production_wait_blockers),
                    "cycle_started_at_utc": production_submitted_at,
                }
                production_due_since_mono = None
                production_wait_blockers = []
                # A cycle that has started is not waiting.  Leaving the live
                # marker behind would have the card report a stall over a
                # round that is running.
                state.pop("production_waiting", None)
                next_production = time.monotonic() + 15.0
            # Its own clock, not a writer slot.  The prefetch writes no SSOT
            # from this thread (see WRITER_LANES); what it starts writes from
            # its own subprocesses and meets production at SQLite, as the
            # capture lanes do.
            if (
                storage_ok
                and now_mono >= next_forecast
                and forecast_future is None
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
        # Charter 189: the maintenance worker goes with its resident (it
        # would also notice the parent is gone, a step later).
        supervisor = locals().get("maintenance_supervisor")
        if supervisor is not None:
            try:
                supervisor.stop()
            except Exception:  # noqa: BLE001 -- shutdown is best effort
                pass
        for executor_name in (
            "executor",
            "forecast_executor",
            "fast_taf_executor",
            "nwp_probe_executor",
            "registry_executor",
            "argmax_executor",
            "sweep_executor",
            "argmax_entry_executor",
        ):
            resident_executor = locals().get(executor_name)
            if resident_executor is not None:
                resident_executor.shutdown(
                    wait=True,
                    cancel_futures=False,
                )
        # Charter 170: stop the fast lane's thread before its connection is
        # torn down with everything else.  Discovered defensively like the
        # rest of this block, because the failure that brought us here may
        # have happened before it was constructed.
        fast_lane = locals().get("awc_fast_worker")
        if fast_lane is not None:
            try:
                fast_lane.stop()
            except Exception:  # noqa: BLE001 -- shutdown is best effort
                pass
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
