"""唯一的 Bayes × basket target ``production_no_live`` worker。

该 worker 是 resident system-domain daemon 的 target 内部组件，不是第四个
launchd 服务。它只读取 target SSOT，使用唯一 compiler，必要时把候选写入
隔离的 target ledger；任何路径都不调用 gateway、不接收 live submit 授权。

``prepared`` 候选是 no-live 的非交易所事实。按站点×目标日最多保留一个未
结算 prepared cycle，避免 resident 每次盘口变化都追加无用 intent/reservation
而耗尽资本或磁盘。已有 submitting/submitted/reconcile_required 则直接
fail closed，等待人工/对账流程结束。
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .activation import assert_target_database_owner, load_target_activation
from .atomic_ledger import (
    AtomicLedgerError,
    commit_blocked_scope_decision,
    commit_preliminary_scope_decision,
)
from .band_reliability_store import load_band_table
from .contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
    timestamp_eq,
)
from .cash_phase_forward_observation import (
    build_forward_observation_packet,
)
from .event_contracts import ForecastRevision, validate_forecast_revision_chain
from .database import (
    DEFAULT_TARGET_DB,
    DEFAULT_TARGET_WS_MANIFEST,
    ROOT,
    resolve_target_database,
    resolve_target_ws_manifest,
)
from .et_history import event_day_window
from .production import (
    commit_production_candidate_atomic,
    commit_production_stage_zero_execution_atomic,
)
from .execution_reconciliation import (
    validate_persisted_candidate_consumption_cas,
)
from .execution_version_vector import (
    SCHEMA as EXECUTION_VERSION_VECTOR_SCHEMA,
    VERSION_FIELDS as EXECUTION_VERSION_FIELDS,
)
from .production_io import (
    compile_production_cycle_from_ssot,
    load_materialized_probability_envelopes,
    load_post_fill_recompute_requests,
    target_atomic_facts_from_cycle,
    target_registry_source_lane,
    target_stage_zero_execution_anchor_from_cycle,
)
from .quote_freshness import QuoteFreshnessPolicy
from .market_materialization import insert_market_evidence_bundle, build_confirmed_inventory_snapshot, build_order_state_snapshot
from .market_ingress import materialize_official_ws_evidence_cache
from .materialization import materialize_p0m
from .probability_bridge import materialize_probability_bridge
from .metar_trajectory import (
    METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
    MetarTrajectoryLikelihoodArtifact,
)
from .p1_compiler import (
    compile_station_p1_packets,
    p1_trajectory_mode as p1_trajectory_mode_for_artifact,
    select_p1_replay_base,
)
from .resident_canonical import (
    attach_truth_observation_revisions,
    load_resident_formal_observations,
)
from .resident_market_ingress import materialize_resident_no_live_market_evidence
from .resource_bounds import (
    ResourceBoundError,
    assert_connection_target_capacity,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
    write_bounded_json_if_changed,
)
from .performance_budget import load_performance_budget
from .nwp_rotation_state import (
    build_rotation_state,
    load_rotation_state,
    persist_rotation_state,
    scope_key as nwp_rotation_scope_key,
)
from .runtime_config import (
    DEFAULT_TARGET_NO_LIVE_RUNTIME_CONFIG,
    load_target_no_live_runtime_config,
)
from .market_registry import (
    export_shared_live_registry,
    ingest_target_market_registry,
    load_started_registry_rows,
)
from .official_nwp_capture_scheduler import (
    MAX_DISCOVERED_SCOPES,
    MAX_RUN_AGE_DAYS,
    registry_capture_scopes,
)
from .open_meteo_ensemble import (
    SCHEMA as OPEN_METEO_FORWARD_PACKET_SCHEMA,
    capture_scope as capture_open_meteo_ensemble_scope,
)
from .official_native_forward_packet import (
    SCHEMA as OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
)
from .native_ensemble_contract import P0_PROBABILITY_PATHWAY
from .native_ensemble_oos import (
    DEFAULT_MIN_HOLDOUT_DAYS,
    DEFAULT_MIN_TRAIN_DAYS,
    FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS,
    FORWARD_ACCUMULATION_MIN_TRAIN_DAYS,
    build_native_ensemble_oos,
    persist_native_ensemble_oos,
)
from .native_ensemble_p0 import (
    DEFAULT_FRESH_SECONDS as NATIVE_P0_COMPILE_FRESH_SECONDS,
    compile_native_ensemble_p0,
    persist_native_ensemble_forward_prediction,
)
from .native_ensemble_authority import (
    DEFAULT_NATIVE_OOS_SNAPSHOT_MANIFEST,
    NativeOosSnapshot,
    load_native_oos_snapshot_manifest,
)
from .native_ensemble_store import ingest_forward_packet
from .metar_physical_forward import project_metar_physical_member_runs
from .native_ensemble_training_facts import ingest_forward_taf_inbox
from .portfolio_inventory import (
    build_portfolio_inventory,
    load_portfolio_capital_policy,
    persist_portfolio_inventory,
)
from .portfolio_allocator import (
    allocate_portfolio_quota,
    derive_minimum_bankroll,
    load_regional_blocks,
    persist_portfolio_allocation,
)
from .shadow_objective import (
    build_shadow_objective_score,
    persist_shadow_objective_score,
)
from .p0_preselection import (
    UNPERSISTED_MARKET_SNAPSHOT_KEY,
    commit_p0_preselection,
    compile_p0_preselection,
    latest_visible_p0_station_ids,
    load_market_registry_snapshot,
    load_official_ws_quotes,
)
from .probability_packet import build_probability_envelope
from .probability_evidence_store import (
    insert_probability_update_proof,
    metar_trajectory_scope_key,
    resolve_active_probability_evidence_artifact,
)
from .regime_materialization import (
    build_regime_artifact,
    insert_regime_artifact,
)
from .ssot import production_ssot_schema_status
from .settlement_truth import materialize_settled_training_truths
from .settlement_time import settlement_day_window
from .station_registry import coordinate_registry_manifest, station_coordinates
from .taf_artifact_registry import (
    SCHEMA as TAF_ARTIFACT_REGISTRY_SCHEMA,
    validate_taf_artifact_registry,
)
from .taf_revision import (
    TafRevisionArtifact,
    compile_taf_revision_proof,
    taf_scope_lead_bucket,
)
from .delta_v2_training import FACT_SCHEMA, delta_v2_decision_scope_key
from .delta_v2_settlement_label import (
    FACT_AUTHORITY_SCOPE,
    LABEL_BATCH_SCHEMA,
    MAX_LABEL_BATCH,
    materialize_delta_v2_settled_training_labels,
)
from .forward_observation_store import (
    DEFAULT_STORE_PATH,
    DEFAULT_STORE_ROOT,
    MAX_BATCH_ROWS as MAX_FORWARD_STORE_BATCH_ROWS,
    append_forward_settlement_labels,
    commit_forward_observation_batch,
    quarantine_forward_observation_batch,
    read_forward_store_state,
    resolve_forward_store_path,
)


SCHEMA = "bayes_basket_target_production_worker_v2"
PRODUCTION_EXECUTION_MODE = "production_no_live"
MAX_PREPARED_CYCLES_PER_SCOPE = 1
# Measured on 1022 observed station-days: the daily high landed inside these
# local hours 80% of the time.  It peaks at 15:00 but only 17.6% there, so
# the useful signal is the window rather than a distance from any one hour.
# The measured distribution of when the daily high is actually set, as the
# share of 1022 observed station-days whose high landed in each local hour.
# It is not flat and it is not symmetric: 15:00 carries seven times 10:00's
# share, and the afternoon tail is fatter than the morning approach.  Using
# it directly is what keeps stations inside the window ordered against each
# other, instead of tying at a plateau and falling through to the alphabet.
_WARM_HOUR_SHARE: tuple[tuple[float, float], ...] = (
    (0.0, 0.034), (1.0, 0.006), (7.0, 0.004), (8.0, 0.008),
    (9.0, 0.023), (10.0, 0.026), (11.0, 0.043), (12.0, 0.093),
    (13.0, 0.127), (14.0, 0.158), (15.0, 0.176), (16.0, 0.106),
    (17.0, 0.118), (18.0, 0.044), (19.0, 0.017), (20.0, 0.004),
    (21.0, 0.001), (22.0, 0.003), (23.0, 0.010),
)
_WARM_HOUR_PEAK_SHARE = max(share for _hour, share in _WARM_HOUR_SHARE)


def _warm_hour_share(hour: float) -> float:
    """Interpolate the measured share of daily highs at a local hour."""

    clamped = min(max(float(hour), 0.0), 24.0)
    previous_hour, previous_share = _WARM_HOUR_SHARE[0]
    for current_hour, current_share in _WARM_HOUR_SHARE:
        if clamped <= current_hour:
            if current_hour == previous_hour:
                return current_share
            span = current_hour - previous_hour
            weight = (clamped - previous_hour) / span
            return previous_share + weight * (current_share - previous_share)
        previous_hour, previous_share = current_hour, current_share
    return previous_share
# How far a scope's score may be lifted by waiting.  Twelve hours is enough
# for the quietest station to outrank a busy one, and bounded so that a scope
# left behind by an outage cannot accumulate unbounded priority and then
# monopolise the rotation when service resumes.
ROTATION_AGEING_CAP_HOURS = 12.0
# No open event day may go longer than this without a capture.  The paper
# ledger refuses a book more than thirty minutes older than the decision it
# prices, so a day starved past that window produces decisions nothing can
# be filled against; half of it leaves room for the cycle to land.
EVENT_DAY_CAPTURE_BOUND_SEC = 900.0

MAX_FORWARD_P0_STATIONS = 45
# Packets kept in the republisher forward inbox.  This has to stay under what
# ``MAX_JSON_OUTPUT_BYTES`` permits, not merely be "a large number": each
# packet carries every registered ensemble system, ~65KiB of JSON and up to
# 95KiB, so the previous cap of 256 allowed roughly 16.6MiB against a 16MiB
# write limit.  The two bounds disagreed and the byte bound won by throwing --
# the inbox filled to 99.7% of the limit and then every capture failed with
# ``target_json_output_bytes_exceeded``.
#
# 45 stations x three open event days is what the horizon prune below leaves
# behind, and at the observed packet size that is ~13MiB with headroom.
MAX_REPUBLISHER_INBOX_PACKETS = 45 * 3
# Capture states that mean an official packet is on its way for this scope.
#
# The republisher is a fallback for scopes the official archives cannot serve,
# not a hedge against one being slow.  Official capture is asynchronous: the
# cycle that starts it returns SUBMITTED and the packet lands minutes later, at
# which point it takes precedence and the republisher packet it raced is
# discarded.  Paying for that packet anyway is what exhausted the aggregator's
# daily quota -- the ensemble endpoint bills per member, so one republisher
# capture costs ~409 units against a 10,000/day allowance, and the catalogue is
# 270 scopes.
#
# THROTTLED counts as pending: that state exists because an attempt was made
# within the retry window, so the answer for this scope is already being
# decided.  The cost is that a scope whose official capture keeps failing gets
# no aggregator fallback until its throttle expires, which is the intended
# trade -- official first, and the fallback reserved for scopes officially
# unavailable rather than officially late.
OFFICIAL_NATIVE_CAPTURE_PENDING_STATES = frozenset({
    "SUBMITTED",
    "IN_FLIGHT",
    "DEFERRED_CAPTURE_SLOTS_BUSY",
    "THROTTLED",
})
# Open scopes are station-*days*, not stations.  Several event days are open at
# once -- discovery admits a day from 72h before its local start until it
# closes -- so a station appears once per open day.  Bounding them at the
# station count made "more than one day is open" an error, which is how the
# lane came to depend on something upstream pinning it to a single date.
MAX_FORWARD_P0_STATION_DAYS = MAX_FORWARD_P0_STATIONS * 4
MAX_RESIDENT_NWP_SCOPES_PER_CYCLE = 4
MAX_NATIVE_ENSEMBLE_TRUTH_CASES_PER_STATION = 64
MAX_RESIDENT_CANDIDATE_PREFETCH_SCOPES_PER_CYCLE = 1
MAX_POST_FILL_RECOMPUTES_PER_PASS = 128
# A single resident pass can legitimately expose multiple committed envelope
# generations for a station/day (P0/P1 corrections and replay-safe lineage).
# The 45-station scope currently expands to 265 envelopes; keep a bounded
# ceiling above that real shape while retaining a hard upper bound for the
# resource guard.
MAX_REGIME_ARTIFACTS_PER_PASS = 512
MAX_FORWARD_DECISIONS_PER_PASS = MAX_FORWARD_STORE_BATCH_ROWS
MAX_FORWARD_DECISION_JSON_BYTES = 262_144
MAX_FORWARD_SCAN_BYTES = 16 * 1024 * 1024
DELTA_V2_LABEL_SCAN_INTERVAL_SECONDS = 900.0
MAX_DELTA_V2_LABEL_PAGES_PER_PASS = 8
TARGET_SCOPE_KEYS = ("season_bin", "maturity_bin", "liquidity_bin", "strategy_version")
MULTI_SCOPE_WS_MANIFEST_SCHEMA = (
    "bayes_basket_target_resident_multi_scope_ws_manifest_v1"
)
# Three concurrent event days per station is the steady state now that the
# catalog subscribes at listing: the listing day, today and tomorrow all
# carry scopes at once.  The publisher's own cap (MAX_TARGET_SCOPES) moved to
# 45x3 for the same reason; a reader stricter than the writer turns a bigger
# catalog into a global WS-path outage.
MAX_RESIDENT_WS_SCOPES = 135


def insert_probability_envelope(
    conn: sqlite3.Connection,
    envelope: ProbabilityEnvelope,
    *,
    created_at_utc: str,
) -> dict[str, Any]:
    """Single resident write seam: envelope and X-S-B projection atomically."""

    return materialize_probability_bridge(
        conn,
        envelope,
        created_at_utc=created_at_utc,
    )


def _stored_probability_envelope(
    conn: sqlite3.Connection,
    *,
    checkpoint_id: str,
) -> ProbabilityEnvelope:
    row = conn.execute(
        "SELECT stage,checkpoint_id,station_id,event_date,"
        "settlement_timezone,decision_at_utc,fresh_until_utc,"
        "bucket_ordinals_json,probabilities_json,posterior_version,"
        "posterior_sha256,bridge_sha256,lineage_json "
        "FROM basket_probability_envelope WHERE checkpoint_id=?",
        (checkpoint_id,),
    ).fetchone()
    if row is None:
        raise ContractError("resident_p0_existing_checkpoint_missing")
    return ProbabilityEnvelope.from_mapping({
        "stage": row[0],
        "checkpoint_id": row[1],
        "station_id": row[2],
        "event_date": row[3],
        "settlement_timezone": row[4],
        "decision_at_utc": row[5],
        "fresh_until_utc": row[6],
        "bucket_ordinals": json.loads(str(row[7])),
        "probabilities": json.loads(str(row[8])),
        "posterior_version": row[9],
        "posterior_sha256": row[10],
        "bridge_sha256": row[11],
        "lineage": json.loads(str(row[12])),
    })


def _p0_bridge_only_semantic_equivalent(
    existing: ProbabilityEnvelope,
    candidate: ProbabilityEnvelope,
) -> bool:
    """Recognize the one-time catalog-root to scope-bridge correction."""

    return (
        existing.stage == candidate.stage == "P0"
        and existing.station_id == candidate.station_id
        and existing.event_date == candidate.event_date
        and existing.settlement_timezone == candidate.settlement_timezone
        and existing.decision_at_utc == candidate.decision_at_utc
        and existing.fresh_until_utc == candidate.fresh_until_utc
        and existing.bucket_ordinals == candidate.bucket_ordinals
        and set(existing.probabilities) == set(candidate.probabilities)
        and all(
            abs(
                float(existing.probabilities[bucket])
                - float(candidate.probabilities[bucket])
            )
            <= 1e-12
            for bucket in existing.probabilities
        )
        and existing.posterior_version == candidate.posterior_version
        and existing.lineage == candidate.lineage
        and existing.bridge_sha256 != candidate.bridge_sha256
    )


@contextmanager
def _writer_transaction(
    conn: sqlite3.Connection,
    *,
    savepoint: str,
) -> Iterator[None]:
    """Use one fail-closed transaction without disturbing an outer writer."""

    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
        if own_transaction:
            conn.commit()
        else:
            conn.execute(f"RELEASE {savepoint}")
    except Exception:
        if own_transaction:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        raise

DEFAULT_TARGET_ACTIVATION = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "trading"
    / "bayes_basket_target_production_no_live_activation_v1.json"
)
DEFAULT_TARGET_MARKET_MANIFEST = DEFAULT_TARGET_WS_MANIFEST
DEFAULT_TARGET_NATIVE_OOS_SNAPSHOT_MANIFEST = DEFAULT_NATIVE_OOS_SNAPSHOT_MANIFEST
OFFICIAL_NATIVE_FORWARD_INBOX_SCHEMA = "native_ensemble_forward_inbox_v1"
# One rotation step captures one scope.  A station whose cycle simply has not
# published yet must not re-download on every cycle, and the retained archives
# are shared and reused within their own TTL, so a modest retry floor is enough
# to keep the whole 45-station rotation moving within an event day.
OFFICIAL_NATIVE_CAPTURE_RETRY_SEC = 1800.0
# Bounded by the capture command's own MIN/MAX_TIMEOUT_SECONDS contract.
OFFICIAL_NATIVE_CAPTURE_TIMEOUT_SEC = 45.0
# A capture downloads whole GEFS/ECMWF archives and takes minutes, while the
# rotation must sweep ~77 scopes well inside the P0 freshness TTL (~6h) or every
# scope goes stale and the mainline fail-closes on
# `resident_target_ws_actionable_scope_missing`.  Capturing on every rotation
# step did exactly that: it monopolised the single forecast worker and the
# cursor advanced roughly one scope per six hours.  A global floor keeps the
# fast republisher path sweeping and lets native coverage accrue in the gaps.
#
# Deliberately not a multiple of the NWP poll interval: at exactly one poll
# period the floor expires on the way into every poll, so each poll paid the
# capture deadline again.  A prime-ish offset lets most polls run pure
# republisher and spreads captures across different scopes over time.
# Captures run off the rotation thread, so the limit that matters is how
# many archive fetches may be in flight at once -- not how rarely one may
# start.  Official coverage is what gates P0, so this is sized to grow it
# quickly while staying bounded against the external archives.
OFFICIAL_NATIVE_CAPTURE_MAX_CONCURRENCY = 4
ECMWF_GCS_REPLICA_BASE_URL = "https://storage.googleapis.com/ecmwf-open-data"

# Hard wall-clock ceiling on one capture, enforced by killing the child.  The
# command's own ``--timeout-seconds`` bounds individual archive requests only;
# total runtime is unbounded.
#
# A capture is latency-bound: ~1300 bounded range requests at a measured 2.4-4.6s
# round trip.  With the archive download pools at 24 workers that lands near
# ~140s, so this leaves roughly a 1.7x margin.  It sits on the background capture
# pool and no longer blocks the rotation, which is what makes a ceiling this
# large safe; a wedged child still gets killed and frees its slot.
# Four archives share one capture, and that capture now reads a whole model run
# for every station the day lists rather than one station at a time.  The bytes
# barely move -- global fields are downloaded once either way, and only the
# union of local-day forecast steps widens -- but the per-station extraction and
# decode do add up across forty-five stations, and a run that is captured once
# has no second attempt to fall back on within the cycle.  Sized for the whole
# pass to finish rather than be killed just short of it; a wedged child still
# gets killed and frees its pool slot.
OFFICIAL_NATIVE_CAPTURE_DEADLINE_SEC = 2400.0
# METAR trajectory training is fold-driven: a station gains at most one fold
# per settled day, so sweeping more often than a few times a day only reruns a
# gate against unchanged evidence.  The chain stays as offline scripts --
# packet build, OOS fit, gated persist -- and this pass merely runs them when
# a station's newest settled day is not yet covered by an artifact.  Nothing
# here can promote: the OOS gate inside the fit decides, and a rejected
# candidate is persisted as evidence without ever becoming resolvable
# authority.
# Floor between two OOS station scans.  Deliberately far below the cycle it
# rides inside: the cycle is the real pacing, and this only stops a very fast
# cycle from re-scanning the same station back to back.
NATIVE_ENSEMBLE_OOS_SCAN_MIN_INTERVAL_SEC = 60.0
METAR_TRAJECTORY_TRAINING_RETRY_SEC = 6 * 3600.0
METAR_TRAJECTORY_TRAINING_DEADLINE_SEC = 600.0
# NIGHT is deliberately absent: a fold anchored at the bucket start would
# require a report that cannot exist before local midnight, and the
# anchor-to-first-report variant is a separate decision.
METAR_TRAJECTORY_TRAINING_BUCKETS = ("MORNING", "AFTERNOON", "EVENING")
METAR_TRAJECTORY_ARTIFACT_VALID_DAYS = 14
# TAF revision training is the same shape as the trajectory pass -- one due
# station per cycle, offline scripts, a gate this worker does not get to
# override -- but its evidence is the rolling strict-OOS native series rather
# than settled folds.  The chain writes no SSOT row at all: its only product is
# the registry file the resident P0-M stage reads, assembled by the validated
# builder from artifacts that already carry a bound live-compatibility proof.
TAF_REVISION_TRAINING_RETRY_SEC = 6 * 3600.0
TAF_REVISION_TRAINING_DEADLINE_SEC = 900.0
TAF_REVISION_ARTIFACT_VALID_DAYS = 30
# The AWC date-addressed endpoint serves 30 days and no more; past that it
# answers 400 with "Data is available for up to 30 days for date".  A day not
# captured while it is inside that window is gone for good, so this cannot be
# a backfill run when someone needs the evidence -- it has to be a standing
# daily pass.
#
# One anchor per day is also not one decision per day.  A TAF confirms a
# prediction only if it arrived after it, and predictions are made against the
# same day at several leads, so a single anchor answers only for decisions made
# before it.  These four are the settlement-relative offsets that match TAF's
# roughly six-hourly issue cycle.
# Corrected reports arrive continuously, and each one raises a halt that the
# settled-truth gate will not clear without a review receipt.  The policy that
# resolves them has existed since the last outage but was only ever run by
# hand, so the halts simply accumulated again -- six stations stopped settling
# and nothing said why.  Running it on a schedule is what makes the clearing
# automatic rather than a thing someone has to remember.
CORRECTION_AUTO_CLEAR_RETRY_SEC = 6 * 3600.0
CORRECTION_AUTO_CLEAR_DEADLINE_SEC = 900.0
# Far enough back to pick up a halt raised while the pass was down, without
# reopening months of settled history on every run.
CORRECTION_AUTO_CLEAR_WINDOW_DAYS = 21
HISTORICAL_TAF_CAPTURE_RETRY_SEC = 24 * 3600.0
HISTORICAL_TAF_CAPTURE_DEADLINE_SEC = 3600.0
HISTORICAL_TAF_DECISION_ANCHOR_HOURS = (1, 7, 13, 19)
# Two days back, not one: the settlement day has to be closed in the station's
# own timezone before its truth can be read, and the capture refuses an open
# day outright.
HISTORICAL_TAF_CAPTURE_LAG_DAYS = 2
# Enough overlap that a few days of downtime still heal, while staying far
# inside the retention window.
HISTORICAL_TAF_CAPTURE_WINDOW_DAYS = 7
# Open-Meteo supplies UKMO, GOOGLE_WEATHERNEXT and NOAA_AIGEFS -- three
# independence families with no official native archive, so this republisher is
# their only route to a d2 member set.  Like ICON it is forward-only: members
# are retained about five days and a backward query returns the current run's
# hindcast rather than the d2 forecast issued then, so a missed day is lost.
# 135 small JSON requests, far lighter than ICON's ~543MiB of GRIB.
OPEN_METEO_D2_CAPTURE_DEADLINE_SEC = 600.0
OPEN_METEO_D2_CAPTURE_RETRY_SEC = 900.0
OPEN_METEO_D2_CAPTURE_LEAD_DAYS = 2
OPEN_METEO_D2_SYSTEM_IDS = (
    "UKMO_GLOBAL_ENS", "GOOGLE_WEATHERNEXT2_ENS", "NOAA_AIGEFS_025",
)
# Markets for a future date are listed progressively, so a snapshot must be
# redone often enough to pick up stations added after it was taken.
REGISTRY_EXPORT_MAX_AGE_SEC = 1800.0
OPEN_METEO_D2_EXTRACTION_VERSION = (
    "open_meteo_forward_d2_daily_tmax_member_v1"
)
CANONICAL_TARGET_STATIONS = tuple(
    coordinate_registry_manifest()["station_ids"]
)
DEFAULT_TARGET_TAF_ARTIFACT = (
    Path(__file__).resolve().parents[1]
    / "config" / "artifacts" / "bayes_basket_target_taf_revision_active.json"
)
DEFAULT_TARGET_TAF_INBOX = (
    Path(__file__).resolve().parents[1]
    / "data" / "bayes_basket_target_taf_revision_inbox.json"
)
# Bound artifacts are kept beside the registry they are assembled into, not in
# the worker's runtime scratch: the registry is a lossless envelope around
# them, so losing the members would leave a file no builder could rebuild.
DEFAULT_TARGET_TAF_BOUND_ARTIFACT_ROOT = (
    Path(__file__).resolve().parents[1]
    / "config" / "artifacts" / "bayes_basket_target_taf_revision_bound"
)
DEFAULT_TARGET_P0_INBOX = (
    Path(__file__).resolve().parents[1]
    / "data" / "bayes_basket_target_p0_inbox.json"
)
DEFAULT_TARGET_NWP_RUNTIME_ROOT = (
    Path(__file__).resolve().parents[1]
    / "data" / "runtime" / "bayes_basket_target_nwp"
)
DEFAULT_CASH_PHASE_FORWARD_OBSERVATIONS = DEFAULT_STORE_PATH


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def open_target_read_only(path: Path) -> sqlite3.Connection:
    """Open target SSOT in SQLite read-only mode; never create a database."""

    resolved = resolve_target_database(path)
    conn = sqlite3.connect(
        f"file:{resolved}?mode=ro",
        uri=True,
        timeout=60.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def open_target_writer_if_enabled(path: Path, *, activation_path: Path) -> sqlite3.Connection | None:
    """Open the isolated M5 writer only for the explicit no-live deployment.

    The environment variable is a service-domain wiring flag, not a live
    authorization. It is accepted only when the resident execution mode is
    exactly ``production_no_live`` and the file activation contract proves
    every live boundary false.
    """

    enabled = str(os.environ.get("PM_TARGET_PRODUCTION_WRITER", "") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    mode = str(os.environ.get("PM_TARGET_EXECUTION_MODE", "") or "").strip()
    if mode and mode != PRODUCTION_EXECUTION_MODE:
        raise RuntimeError("target_writer_execution_mode_not_production_no_live")
    activation = load_target_activation(activation_path)
    if activation.get("execution_mode") != PRODUCTION_EXECUTION_MODE:
        raise RuntimeError("target_writer_activation_not_production_no_live")
    if any(activation.get(key) is not False for key in (
        "live_decision_write_authorized",
        "live_order_authorized",
        "live_submit_authorized",
        "places_orders",
        "writes_production_db",
        "calls_order_gateway",
        "network_access",
    )):
        raise RuntimeError("target_writer_activation_live_boundary_invalid")
    resolved = resolve_target_database(path)
    conn = sqlite3.connect(
        resolved,
        timeout=60.0,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA synchronous=FULL")
        if resolved == DEFAULT_TARGET_DB.resolve():
            journal_mode = str(
                conn.execute("PRAGMA journal_mode").fetchone()[0] or ""
            ).strip().lower()
            if journal_mode != "wal":
                raise RuntimeError(
                    "target_writer_journal_mode_not_wal:"
                    f"{journal_mode or 'unknown'}"
                )
        assert_connection_target_capacity(
            conn,
            recover_oversized_wal=True,
        )
        status = production_ssot_schema_status(conn)
        if status.get("ok") is not True or status.get("ledger_schema_ok") is not True:
            raise RuntimeError(
                "target_writer_schema_not_ready:"
                + json.dumps(status, ensure_ascii=False, sort_keys=True)
            )
        assert_target_database_owner(conn)
        return conn
    except Exception:
        conn.close()
        raise


def _blocked(reason: str, *, station_id: str | None = None, event_date: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "ok": False,
        "state": "blocked_fail_closed",
        "reason": reason,
        "execution_mode": PRODUCTION_EXECUTION_MODE,
        "places_orders": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    if station_id:
        result["station_id"] = station_id
    if event_date:
        result["event_date"] = event_date
    return result


_ENSEMBLE_CANDIDATE_MEMBER_TABLES = (
    "calibration_republisher_ensemble_member_run",
    # Retained pre-migration Open-Meteo rows stay explicitly republisher
    # lineage.  They are read here to finish closed-day historical OOS
    # evidence and may serve the separate no-live P0 fallback contract; native
    # arrival/P1 authority remains independently fail-closed.
    "calibration_native_ensemble_member_run",
)

_ENSEMBLE_CANDIDATE_BUCKET_TABLES = (
    "calibration_republisher_ensemble_member_bucket",
    "calibration_native_ensemble_member_bucket",
)


def _existing_ensemble_candidate_tables(
    conn: sqlite3.Connection,
    *,
    tables: Sequence[str],
) -> tuple[str, ...]:
    """Return only installed candidate tables from the fixed SSOT allowlist."""

    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    return tuple(table for table in tables if table in present)


def _candidate_ensemble_station_ids(
    conn: sqlite3.Connection,
    *,
    limit: int,
) -> list[str]:
    """List bounded OOS candidates from both republisher and legacy lanes."""

    tables = _existing_ensemble_candidate_tables(
        conn,
        tables=_ENSEMBLE_CANDIDATE_MEMBER_TABLES,
    )
    if not tables:
        return []
    source = " UNION ".join(
        "SELECT station_id FROM " + table
        + " WHERE authority_scope='PRODUCTION_NO_LIVE_CANDIDATE'"
        for table in tables
    )
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT station_id FROM ("
            + source
            + ") ORDER BY station_id LIMIT ?",
            (limit,),
        )
    ]


def _candidate_ensemble_scopes_without_truth(
    conn: sqlite3.Connection,
    *,
    limit: int,
) -> list[tuple[str, str, str]]:
    """Return bounded candidate station-days still lacking accepted AWC truth."""

    tables = _existing_ensemble_candidate_tables(
        conn,
        tables=_ENSEMBLE_CANDIDATE_MEMBER_TABLES,
    )
    if not tables:
        return []
    source = " UNION ".join(
        "SELECT station_id,target_local_date FROM " + table
        + " WHERE authority_scope='PRODUCTION_NO_LIVE_CANDIDATE'"
        for table in tables
    )
    rows = conn.execute(
        "WITH candidate_scopes AS ("
        + source
        + ") SELECT DISTINCT m.station_id,m.target_local_date,"
        "r.settlement_timezone FROM candidate_scopes m "
        "JOIN basket_target_market_registry r "
        "ON r.station_id=m.station_id "
        "AND r.event_date=m.target_local_date "
        "AND r.source_eligible=1 "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM calibration_station_daily_high_truth t "
        "WHERE t.station_id=m.station_id "
        "AND t.local_date=m.target_local_date "
        "AND t.source_lane='AWC' "
        "AND t.qc_status='ACCEPTED'"
        ") ORDER BY m.target_local_date,m.station_id LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        (str(station), str(event_date), str(settlement_timezone))
        for station, event_date, settlement_timezone in rows
    ]


def _open_meteo_packet_bucket_exists(
    conn: sqlite3.Connection,
    *,
    ensemble_system_id: str,
    station_id: str,
    event_date: str,
    member_set_sha256: str,
    bucket_contract_sha256: str,
) -> bool:
    """Check new republisher storage first, then immutable legacy rows."""

    for table in _existing_ensemble_candidate_tables(
        conn,
        tables=_ENSEMBLE_CANDIDATE_BUCKET_TABLES,
    ):
        exists = conn.execute(
            "SELECT 1 FROM " + table + " "
            "WHERE ensemble_system_id=? AND station_id=? "
            "AND target_local_date=? AND member_set_sha256=? "
            "AND bucket_contract_sha256=? "
            "AND source_role='aggregator_republisher' "
            "AND authority_scope='PRODUCTION_NO_LIVE_CANDIDATE' LIMIT 1",
            (
                ensemble_system_id,
                station_id,
                event_date,
                member_set_sha256,
                bucket_contract_sha256,
            ),
        ).fetchone()
        if exists is not None:
            return True
    return False


def _preferred_forward_packets_for_p0(
    packets: Sequence[Mapping[str, Any]],
) -> tuple[
    dict[tuple[str, str], Mapping[str, Any]],
    dict[tuple[str, str], Mapping[str, Any]],
    dict[tuple[str, str], Mapping[str, Any]],
]:
    """Choose official packets per scope, retaining governed fallback scope.

    This function only resolves input precedence.  It does not grant the
    Open-Meteo packet P0 authority: ``compile_native_ensemble_p0`` later
    requires its own closed-AWC historical strict-OOS proof and activation.
    """

    official_by_scope: dict[tuple[str, str], Mapping[str, Any]] = {}
    republisher_by_scope: dict[tuple[str, str], Mapping[str, Any]] = {}
    for packet in packets:
        schema = str(packet.get("schema") or "")
        if schema not in {
            OPEN_METEO_FORWARD_PACKET_SCHEMA,
            OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
        }:
            continue
        scope = (
            str(packet.get("station_id") or "").strip().upper(),
            str(packet.get("event_date") or ""),
        )
        if not all(scope):
            raise ContractError("native_forward_scope_invalid")
        target = (
            official_by_scope
            if schema == OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA
            else republisher_by_scope
        )
        current = target.get(scope)
        if current is not None and current != packet:
            raise ContractError(
                "native_forward_scope_content_conflict:"
                + ("official" if target is official_by_scope else "republisher")
            )
        target[scope] = packet
    # An official packet always wins its station-day.  A republisher packet is
    # retained only for scopes with no native packet at this evidence instant.
    selected = dict(republisher_by_scope)
    selected.update(official_by_scope)
    return selected, official_by_scope, republisher_by_scope


def _forward_packet_in_cycle_scope(
    packet: Mapping[str, Any],
    *,
    station_allowlist: Sequence[str] = (),
    event_date_allowlist: Sequence[str] = (),
) -> bool:
    """Keep only packets owned by the current resident cycle scope."""

    station = str(packet.get("station_id") or "").strip().upper()
    event_date = str(packet.get("event_date") or "").strip()
    allowed_stations = {
        str(value or "").strip().upper()
        for value in station_allowlist
        if str(value or "").strip()
    }
    allowed_event_dates = {
        str(value or "").strip()
        for value in event_date_allowlist
        if str(value or "").strip()
    }
    return (
        bool(station)
        and bool(event_date)
        and (
            not allowed_stations
            or station in allowed_stations
        )
        and (
            not allowed_event_dates
            or event_date in allowed_event_dates
        )
    )


def _compile_native_ensemble_p0_runtime(
    conn: sqlite3.Connection,
    *,
    forward_packet: Mapping[str, Any],
    decision_at_utc: str,
    native_oos_snapshot: NativeOosSnapshot | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile P0 with the immutable native pointer when production enables it.

    The optional keyword is omitted for isolated unit/research callers so
    existing compiler fixtures remain independent of the production cutoff
    file.  The resident production writer always supplies a validated
    snapshot (or an invalid sentinel, which the compiler rejects fail closed).
    """

    kwargs: dict[str, Any] = {
        "forward_packet": forward_packet,
        "decision_at_utc": decision_at_utc,
    }
    if native_oos_snapshot is not None:
        kwargs["native_oos_snapshot"] = native_oos_snapshot
    return compile_native_ensemble_p0(conn, **kwargs)


def _compile_native_p0_with_republisher_fallback(
    conn: sqlite3.Connection,
    *,
    scope: tuple[str, str],
    preferred_packet: Mapping[str, Any],
    official_packet: Mapping[str, Any] | None,
    republisher_packet: Mapping[str, Any] | None,
    decision_at_utc: str,
    native_oos_snapshot: NativeOosSnapshot | Mapping[str, Any] | None = None,
    republisher_fallback_activation: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    """Compile only the official-native P0 candidate for one scope.

    Republisher cross-selection is historical no-live evidence, not a native
    forward packet or P0 authority.  The retired fallback argument remains a
    typed no-op only until all historical callers are deleted; it can never
    route Open-Meteo data into the native compiler.
    """

    # Prefer the separately indexed official packet rather than trusting the
    # caller's convenience ``preferred_packet``.  This matters when a caller
    # has already tentatively selected an Open-Meteo packet: a fresh official
    # packet for the same station-day must still compile first.  Conversely,
    # a stale official packet is intentionally attempted and recorded before
    # the governed republisher retry; it must not be treated as fresh merely
    # because this function is running now.
    preferred_schema = str(preferred_packet.get("schema") or "")
    official_candidate = official_packet
    if (
        official_candidate is None
        and preferred_schema == OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA
    ):
        official_candidate = preferred_packet
    del republisher_packet, republisher_fallback_activation

    candidates: list[tuple[str, Mapping[str, Any]]] = []

    def append_candidate(
        source_class: str,
        packet: Mapping[str, Any] | None,
    ) -> None:
        if packet is None or any(
            existing_packet == packet
            for _existing_source, existing_packet in candidates
        ):
            return
        candidates.append((source_class, packet))

    append_candidate("official_native", official_candidate)
    if not candidates:
        return None, [{
            "station_id": scope[0],
            "event_date": scope[1],
            "source_class": (
                "aggregator_republisher"
                if preferred_schema == OPEN_METEO_FORWARD_PACKET_SCHEMA
                else "official_native"
            ),
            "schema": preferred_schema,
            "reason": "republisher_no_live_selection_not_forward_p0_authority",
        }], ""

    failures: list[dict[str, Any]] = []
    for source_class, packet in candidates:
        try:
            compiled = _compile_native_ensemble_p0_runtime(
                conn,
                forward_packet=packet,
                decision_at_utc=decision_at_utc,
                native_oos_snapshot=native_oos_snapshot,
            )
            if (
                not isinstance(compiled, Mapping)
                or compiled.get("schema")
                != "calibrated_native_cross_ensemble_p0_packet_v1"
            ):
                raise ContractError("native_p0_compiler_result_invalid")
            return dict(compiled), failures, source_class
        except Exception as exc:
            failures.append({
                "station_id": scope[0],
                "event_date": scope[1],
                "source_class": source_class,
                "schema": str(packet.get("schema") or ""),
                "reason": f"{type(exc).__name__}:{exc}",
            })
    return None, failures, ""


def _native_ensemble_truth_signatures(
    conn: sqlite3.Connection,
    *,
    max_stations: int = MAX_FORWARD_P0_STATIONS,
    max_cases_per_station: int = (
        MAX_NATIVE_ENSEMBLE_TRUTH_CASES_PER_STATION
    ),
) -> dict[str, str]:
    """Hash accepted truth identity for every native or republisher scope."""

    tables = _existing_ensemble_candidate_tables(
        conn,
        tables=_ENSEMBLE_CANDIDATE_MEMBER_TABLES,
    )
    if not tables:
        return {}
    source = " UNION ".join(
        "SELECT station_id,target_local_date FROM " + table
        + " WHERE authority_scope='PRODUCTION_NO_LIVE_CANDIDATE'"
        for table in tables
    )

    rows = conn.execute(
        "WITH candidate_scopes AS ("
        + source
        + "), matched AS ("
        "SELECT DISTINCT m.station_id,t.local_date,t.content_sha256 "
        "FROM candidate_scopes m "
        "JOIN calibration_station_daily_high_truth t "
        "ON t.station_id=m.station_id "
        "AND t.local_date=m.target_local_date "
        "AND t.source_lane='AWC' "
        "AND t.qc_status='ACCEPTED'"
        "), selected_stations AS ("
        "SELECT DISTINCT station_id FROM matched "
        "ORDER BY station_id LIMIT ?"
        "), ranked AS ("
        "SELECT matched.station_id,matched.local_date,"
        "matched.content_sha256,"
        "ROW_NUMBER() OVER ("
        "PARTITION BY matched.station_id "
        "ORDER BY matched.local_date DESC,matched.content_sha256"
        ") AS truth_rank "
        "FROM matched JOIN selected_stations "
        "ON selected_stations.station_id=matched.station_id"
        ") "
        "SELECT station_id,local_date,content_sha256 "
        "FROM ranked WHERE truth_rank<=? "
        "ORDER BY station_id,local_date,content_sha256",
        (max_stations, max_cases_per_station),
    ).fetchall()
    identities_by_station: dict[str, list[dict[str, str]]] = {}
    for station_id, local_date, truth_sha in rows:
        station = str(station_id)
        identities_by_station.setdefault(station, []).append({
            "local_date": str(local_date),
            "truth_content_sha256": str(truth_sha),
        })
    return {
        station: content_sha256({
            "station_id": station,
            "accepted_truths": identities,
        })
        for station, identities in sorted(
            identities_by_station.items()
        )
    }


def _upsert_native_p0_inbox(
    path: Path,
    *,
    compiled_packets: Sequence[Mapping[str, Any]],
    generated_at_utc: str,
) -> dict[str, Any]:
    """Merge bounded compiled native P0 packets by station-day identity."""

    # This is a transient handoff inbox, not a historical evidence store.
    # Retaining an expired packet makes a later, otherwise admissible
    # station-day P0 fail as a whole.  Official native packets and the
    # separately-labelled, strict-OOS republisher fallback are retained.
    # Legacy/unknown minimal packets without an envelope remain usable in
    # no-op replay tests; ``materialize_resident_p0`` is the strict validator
    # before SSOT writes.  The fallback never inherits native authority.
    generated_at = datetime.fromisoformat(
        generated_at_utc.replace("Z", "+00:00")
    ).astimezone(timezone.utc)

    def retain_existing(row: Mapping[str, Any]) -> bool:
        packet = row.get("packet")
        if not isinstance(packet, Mapping):
            return False
        envelope = packet.get("native_probability_envelope")
        if not isinstance(envelope, Mapping):
            return True
        lineage = envelope.get("lineage")
        if not isinstance(lineage, Mapping):
            return False
        provider = str(lineage.get("native_transport_provider") or "")
        if provider in {
            "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT",
            "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT",
        }:
            pass
        elif (
            provider == "OPEN_METEO"
            and str(
                lineage.get("aggregator_fallback_p0_eligible") or ""
            ).lower() == "true"
            and str(
                lineage.get("republisher_fallback_p0_eligible") or ""
            ).lower() == "true"
            and str(lineage.get("transport_lineage_class") or "")
            == "aggregator_republisher"
            and str(
                lineage.get("native_arrival_index_eligible") or ""
            ).lower() == "false"
            and str(lineage.get("native_formal_p0_eligible") or "")
            .lower() == "false"
        ):
            pass
        else:
            return False
        fresh_until_raw = str(envelope.get("fresh_until_utc") or "")
        if not fresh_until_raw:
            # Test/replay fixtures without a real envelope timestamp remain
            # subject to the downstream validator.  Real compiled packets
            # always carry this field and are pruned above on expiry.
            return True
        try:
            fresh_until = datetime.fromisoformat(
                fresh_until_raw.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return False
        return fresh_until > generated_at

    existing_rows: list[dict[str, Any]] = []
    if path.is_file():
        existing = load_bounded_json(path)
        identity = {
            key: value
            for key, value in existing.items()
            if key != "content_sha256"
        }
        if (
            existing.get("schema")
            != "bayes_basket_target_p0_inbox_v1"
            or not isinstance(existing.get("packets"), list)
            or str(existing.get("content_sha256") or "")
            != content_sha256(identity)
        ):
            raise ContractError("native_p0_inbox_invalid")
        existing_rows = [
            dict(item)
            for item in existing["packets"]
            if isinstance(item, Mapping)
            and item.get("source_class")
            == "calibrated_native_cross_ensemble"
            and retain_existing(item)
        ]
    merged_by_scope = {
        (
            str((row.get("packet") or {}).get("station_id") or "")
            .strip()
            .upper(),
            str((row.get("packet") or {}).get("event_date") or ""),
        ): row
        for row in existing_rows
    }
    def source_priority(row: Mapping[str, Any]) -> int:
        packet = row.get("packet")
        if not isinstance(packet, Mapping):
            return 0
        envelope = packet.get("native_probability_envelope")
        if not isinstance(envelope, Mapping):
            # Keep the deliberately minimal legacy handoff fixture usable;
            # real packets always carry an envelope and are validated below
            # before any SSOT write.
            return 2 if "native_probability_envelope" not in packet else 0
        lineage = envelope.get("lineage")
        if not isinstance(lineage, Mapping):
            return 0
        provider = lineage.get("native_transport_provider")
        if provider in {
            "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT",
            "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT",
        }:
            return 2
        if (
            provider == "OPEN_METEO"
            and str(
                lineage.get("aggregator_fallback_p0_eligible") or ""
            ).lower() == "true"
            and str(
                lineage.get("republisher_fallback_p0_eligible") or ""
            ).lower() == "true"
            and str(lineage.get("transport_lineage_class") or "")
            == "aggregator_republisher"
            and str(
                lineage.get("native_arrival_index_eligible") or "")
            .lower() == "false"
            and str(lineage.get("native_formal_p0_eligible") or "")
            .lower() == "false"
        ):
            return 1
        return 0

    accepted_count = 0
    for compiled in compiled_packets:
        if compiled.get("schema") != (
            "calibrated_native_cross_ensemble_p0_packet_v1"
        ):
            continue
        packet = compiled.get("packet")
        if not isinstance(packet, Mapping):
            raise ContractError("native_p0_compiled_packet_invalid")
        scope = (
            str(packet.get("station_id") or "").strip().upper(),
            str(packet.get("event_date") or ""),
        )
        if not all(scope):
            raise ContractError("native_p0_compiled_scope_missing")
        candidate_row = {
            "source_class": compiled["source_class"],
            "packet_content_sha256": compiled[
                "packet_content_sha256"
            ],
            "packet": dict(packet),
        }
        if source_priority(candidate_row) == 0:
            # Aggregator/re-publisher data remains excluded unless the
            # compiler attached an activated strict-OOS fallback proof.  Such
            # a packet is still lower authority than official native data and
            # is never admitted to hard-NO/P1 or native-arrival indexes.
            continue
        current = merged_by_scope.get(scope)

        def _decided_at(row: Mapping[str, Any]) -> str:
            envelope = (row.get("packet") or {}).get(
                "native_probability_envelope"
            )
            if not isinstance(envelope, Mapping):
                return ""
            return str(envelope.get("decision_at_utc") or "")

        if (
            current is None
            or source_priority(candidate_row) > source_priority(current)
            or (
                # SAME and MIXED are one official authority tier.  Within the
                # tier the newer decision wins: a replayed older packet
                # carries an older decision instant and stays out, which is
                # all the previous keep-the-incumbent tie rule was protecting
                # -- but that rule also discarded genuinely newer compiles,
                # so a P0-M confirm could never supersede the P0 it confirms.
                source_priority(candidate_row)
                == source_priority(current)
                and _decided_at(candidate_row) > _decided_at(current)
            )
        ):
            merged_by_scope[scope] = candidate_row
        accepted_count += 1
    rows = [
        merged_by_scope[key]
        for key in sorted(
            merged_by_scope,
            key=lambda item: (item[1], item[0]),
        )
    ][-256:]
    inbox_identity = {
        "schema": "bayes_basket_target_p0_inbox_v1",
        "generated_at_utc": generated_at_utc,
        "packets": rows,
    }
    wrote = write_bounded_json_if_changed(path, {
        **inbox_identity,
        "content_sha256": content_sha256(inbox_identity),
    })
    return {
        "state": "COMMITTED" if accepted_count else "NO_COMPILED_P0",
        "accepted_count": accepted_count,
        "packet_count": len(rows),
        "wrote": wrote,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


def _load_native_forward_inbox(
    path: Path,
    *,
    error_code: str,
) -> list[Mapping[str, Any]]:
    inbox = load_bounded_json(path)
    identity = {
        key: value
        for key, value in inbox.items()
        if key != "content_sha256"
    }
    if (
        inbox.get("schema") != "native_ensemble_forward_inbox_v1"
        or not isinstance(inbox.get("packets"), list)
        or len(inbox["packets"]) > 256
        or str(inbox.get("content_sha256") or "")
        != content_sha256(identity)
    ):
        raise ContractError(error_code)
    return [
        packet
        for packet in inbox["packets"]
        if isinstance(packet, Mapping)
    ]


def _p0_preselection_scope_version(market_snapshot: Mapping[str, Any]) -> str:
    """The inventory/order-state version for one preselection quote.

    Keyed to the quote, because the evidence it describes is keyed to the
    quote: a later quote observes a later instant and is a different row.
    A version shared across quotes made the reader's lookup ambiguous by
    construction.
    """

    base = str(market_snapshot.get("inventory_version") or "").strip()
    quote_version = str(market_snapshot.get("quote_version") or "").strip()
    if not quote_version:
        return base
    return f"{base}:{quote_version}" if base else quote_version


def _resolve_target_remaining_budget() -> float:
    """Resolve only the target-owned no-live budget.

    An explicit target environment override is allowed for a controlled
    rehearsal.  With no override, only the target-owned runtime config is
    authoritative.  The resident daemon config and activation ``basket``
    fields are intentionally not fallback sources: doing so could borrow a
    live-line bankroll into the target lane.
    """

    raw_budget = os.environ.get("PM_TARGET_REMAINING_BUDGET_USDC", "").strip()
    if raw_budget:
        try:
            remaining_budget = float(raw_budget)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("target_remaining_budget_usdc_invalid") from exc
        if not math.isfinite(remaining_budget) or remaining_budget < 0.0:
            raise RuntimeError("target_remaining_budget_usdc_invalid")
        return remaining_budget
    runtime_config = load_target_no_live_runtime_config(DEFAULT_TARGET_NO_LIVE_RUNTIME_CONFIG)
    return float(runtime_config["target_remaining_budget_usdc"])


def _merge_scope_manifests(
    manifests: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Fuse one event day's scope manifests into a single ingress document.

    The ingress has always taken a station list; what forced one station per
    cycle was this bridge handing it one scope at a time.  Merging is a
    union of the per-token books and registries with the station allowlist
    widened -- no quote is fetched, recomputed or reinterpreted, so a station
    reads exactly as it would have on its own turn.

    Refuses to merge across event days.  Downstream reads a single
    ``event_date`` off the merged document and would silently attribute one
    day's books to another; a mixed set is a programming error here, not
    something to paper over.
    """

    usable = [dict(item) for item in manifests if isinstance(item, Mapping)]
    if not usable:
        raise ContractError("resident_target_ws_sweep_manifest_empty")
    if len(usable) == 1:
        return usable[0]
    event_dates = {str(item.get("event_date") or "") for item in usable}
    day_states = {str(item.get("event_day_state") or "") for item in usable}
    if len(event_dates) != 1 or len(day_states) != 1:
        raise ContractError("resident_target_ws_sweep_scope_not_one_day")

    base = dict(usable[0])
    stations: list[str] = []
    quotes: dict[str, Any] = {}
    side_state: dict[str, Any] = {}
    markets: dict[str, Any] = {}
    scope_keys: list[str] = []
    for item in usable:
        for station in _manifest_station_allowlist(item):
            if station not in stations:
                stations.append(station)
        scope_keys.append(str(item.get("scope_key") or ""))
        for key, value in (item.get("quotes_by_token") or {}).items():
            quotes[str(key)] = value
        for key, value in (
            item.get("quote_side_state_by_token") or {}
        ).items():
            side_state[str(key)] = value
        registry = item.get("market_registry_scope")
        if isinstance(registry, Mapping):
            for key, value in (
                registry.get("markets_by_token") or {}
            ).items():
                markets[str(key)] = value

    base["station_scope"] = {
        **(base.get("station_scope") or {}),
        "mode": "allowlist",
        "station_ids": sorted(stations),
    }
    base["quotes_by_token"] = quotes
    base["quote_side_state_by_token"] = side_state
    base["market_registry_scope"] = {
        **(base.get("market_registry_scope") or {}),
        "markets_by_token": markets,
    }
    base["subscribed_token_count"] = len(quotes)
    base["materialized_token_count"] = len(quotes)
    base["received_book_token_count"] = len(quotes)
    # The merged document is a different thing from any scope it came from,
    # so it says so rather than wearing one member's identity.
    base["scope_key"] = "SWEEP|" + str(base.get("event_date") or "")
    base["swept_scope_keys"] = sorted(key for key in scope_keys if key)
    base.pop("target_timezone", None)
    base.pop("snapshot_sha256", None)
    base["snapshot_sha256"] = content_sha256({
        key: value
        for key, value in base.items()
        if key != "snapshot_sha256"
    })
    return base


def _manifest_station_allowlist(root: Mapping[str, Any]) -> tuple[str, ...]:
    """Preserve an explicit one-shot WS station scope during materialization."""

    raw = root.get("station_scope")
    if not isinstance(raw, Mapping) or str(raw.get("mode") or "") != "allowlist":
        return ()
    values = raw.get("station_ids")
    if not isinstance(values, list):
        raise RuntimeError("target_market_manifest_station_allowlist_invalid")
    stations = tuple(sorted({
        str(value or "").strip().upper()
        for value in values
        if str(value or "").strip()
    }))
    if not stations:
        raise RuntimeError("target_market_manifest_station_allowlist_empty")
    return stations


def _manifest_content_sha_matches(value: Mapping[str, Any]) -> bool:
    supplied = str(value.get("snapshot_sha256") or "").strip()
    return bool(supplied) and supplied == content_sha256({
        key: item
        for key, item in value.items()
        if key != "snapshot_sha256"
    })


def _active_forward_scopes(path: Path, *, horizon: date) -> set[tuple[str, str]]:
    """Return the still-open (station, event_date) scopes held in one inbox."""

    if not path.is_file():
        return set()
    try:
        root = load_bounded_json(path)
    except Exception:
        return set()
    if not isinstance(root, Mapping) or not isinstance(root.get("packets"), list):
        return set()
    scopes: set[tuple[str, str]] = set()
    for packet in root["packets"]:
        if not isinstance(packet, Mapping):
            continue
        event_date = str(packet.get("event_date") or "").strip()
        parsed = _event_date_or_none(event_date)
        if parsed is not None and parsed < horizon:
            continue
        station_id = str(packet.get("station_id") or "").strip().upper()
        if station_id and event_date:
            scopes.add((station_id, event_date))
    return scopes


def _active_registry_scopes(path: Path, *, horizon: date) -> set[tuple[str, str]]:
    """Return the still-open (station, event_date) scopes the market lists.

    The forward market registry is the authority on which scopes exist at all,
    so it is the correct bound on the official inbox.  Deriving that bound from
    another inbox instead makes coverage self-limiting: a scope missing from
    that inbox can never accept the very packet meant to cover it.
    """

    if not path.is_file():
        return set()
    try:
        root = load_bounded_json(path)
    except Exception:
        return set()
    if not isinstance(root, Mapping) or not isinstance(root.get("rows"), list):
        return set()
    scopes: set[tuple[str, str]] = set()
    for row in root["rows"]:
        if not isinstance(row, Mapping):
            continue
        event_date = str(row.get("event_date") or "").strip()
        parsed = _event_date_or_none(event_date)
        if parsed is not None and parsed < horizon:
            continue
        station_id = str(row.get("station_id") or "").strip().upper()
        if station_id and event_date:
            scopes.add((station_id, event_date))
    return scopes


def _fitted_lead_buckets(
    conn: sqlite3.Connection | None, *, as_of_utc: str
) -> set[tuple[str, str]] | None:
    """``(station, lead_bucket)`` pairs that currently have fitted weights.

    None when unavailable, which the caller reads as "do not filter".
    """

    if conn is None:
        return None
    try:
        return {
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                "SELECT DISTINCT station_id,lead_bucket "
                "FROM calibration_native_ensemble_activation "
                "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE' "
                "AND authority_scope='PRODUCTION_NO_LIVE_ONLY' "
                "AND valid_from_utc<=? AND valid_until_utc>?",
                (as_of_utc, as_of_utc),
            )
        }
    except Exception:
        return None


def _merge_official_native_inbox(
    inbox_path: Path,
    *,
    staging_path: Path,
    as_of_utc: str,
    allowed_scopes: set[tuple[str, str]] | None = None,
    fitted_lead_buckets: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Merge a freshly captured packet into the shared official-native inbox.

    The capture command emits an inbox holding only the scope it fetched.  This
    replaces the matching ``(station_id, event_date)`` entry and keeps every
    other packet, so rotating through stations accumulates coverage instead of
    overwriting it.  Packets whose event day is certainly over everywhere are
    dropped, which bounds the file without needing a separate retention job.
    """

    staged = load_bounded_json(staging_path)
    if not isinstance(staged, Mapping):
        raise ContractError("official_native_staging_inbox_invalid")
    staged_packets = staged.get("packets")
    if not isinstance(staged_packets, list) or not staged_packets:
        raise ContractError("official_native_staging_packets_missing")

    existing_packets: list[Any] = []
    if inbox_path.is_file():
        existing = load_bounded_json(inbox_path)
        if isinstance(existing, Mapping) and isinstance(existing.get("packets"), list):
            existing_packets = list(existing["packets"])

    horizon = _utc_date(as_of_utc) - timedelta(days=1)

    def _scope_of(packet: Any) -> tuple[str, str] | None:
        if not isinstance(packet, Mapping):
            return None
        return (
            str(packet.get("station_id") or "").strip().upper(),
            str(packet.get("event_date") or "").strip(),
        )

    def _freshness_of(packet: Any) -> tuple[int, float]:
        """How fresh this packet's evidence is; unreadable counts as oldest.

        Captures run concurrently, so a slow older-run fetch can finish after
        a fresher one for the same day.  The inbox holds one packet per
        station-day, so without this the later-finishing capture would win on
        arrival order and quietly replace fresher evidence with an older run.

        Freshness is the newest run the packet carries, not its lead label.
        The label used to be a calendar day count and now is an hour bucket,
        and a comparator parsing the old spelling read every new packet as
        "oldest" -- a freshly captured 06Z packet lost the merge to the 00Z
        packet it had just superseded.  The run identity is spelling-proof and
        is the same fact the supersedence probe already compares.  Packets
        stating no run fall back to the calendar label, so republisher-era
        entries keep their old ordering among themselves.
        """

        if not isinstance(packet, Mapping):
            return (0, float("-inf"))
        run_init = _native_packet_run_init(packet)
        if run_init is not None:
            # Tier 1: a stated run always outranks a mere label.
            return (1, run_init.timestamp())
        bucket = str(packet.get("lead_bucket") or "").strip().lower()
        if bucket.startswith("d") and bucket[1:].isdigit():
            # Smaller lead = fresher; negate so bigger-is-fresher throughout.
            return (0, -float(int(bucket[1:])))
        return (0, float("-inf"))

    def _servable(packet: Any) -> bool:
        """Whether P0 has fitted weights for this packet's own training group."""

        if fitted_lead_buckets is None:
            return True
        if not isinstance(packet, Mapping):
            return False
        return (
            str(packet.get("station_id") or "").strip().upper(),
            str(packet.get("lead_bucket") or "").strip(),
        ) in fitted_lead_buckets

    staged_rank_by_scope: dict[
        tuple[str, str], tuple[bool, tuple[int, float]]
    ] = {}
    for packet in staged_packets:
        scope = _scope_of(packet)
        if scope is None:
            continue
        rank = (_servable(packet), _freshness_of(packet))
        held = staged_rank_by_scope.get(scope)
        if held is None or rank > held:
            staged_rank_by_scope[scope] = rank
    merged: list[Any] = []
    pruned = 0
    superseded: set[tuple[str, str]] = set()
    for packet in existing_packets:
        scope = _scope_of(packet)
        if scope is None:
            continue
        event_day = _event_date_or_none(scope[1])
        if event_day is not None and event_day < horizon:
            pruned += 1
            continue
        staged_rank = staged_rank_by_scope.get(scope)
        if staged_rank is not None:
            # Servable first, then freshest.  The lead bucket is per station
            # and derived from the run, so following a new publication cycle
            # can land a station in a training group its own history never
            # populated -- reading 06Z for the first time moved thirteen
            # American station-days into ``h12_24``, where nothing had ever
            # been fitted because 06Z had never been read.  A fresher run
            # there serves no P0 at all, so freshness cannot be the outer key.
            # The archive still ingests every run, so the empty group fills
            # and this preference dissolves on its own once weights exist.
            #
            # Ties go to the staged packet: a fresh capture of the same run is
            # a re-read whose members may have been incomplete before.
            if staged_rank >= (_servable(packet), _freshness_of(packet)):
                continue
            superseded.add(scope)
        merged.append(packet)
    merged.extend(
        packet
        for packet in staged_packets
        if _scope_of(packet) not in superseded
    )
    if allowed_scopes is not None:
        # An official packet upgrades an already-active scope from republisher
        # to native evidence; it must not widen the scope set.  P0 compilation
        # caps the union of both inboxes at MAX_FORWARD_P0_STATIONS
        # (station-day pairs, not stations), so accumulating official scopes
        # the republisher never had silently pushes that union over the bound
        # and fail-closes the whole mainline.
        merged = [
            packet
            for packet in merged
            if not isinstance(packet, Mapping)
            or _scope_of(packet) is None
            or _scope_of(packet) in allowed_scopes
        ]
    merged.sort(
        key=lambda packet: (
            str((packet or {}).get("event_date") or ""),
            str((packet or {}).get("station_id") or ""),
        )
        if isinstance(packet, Mapping)
        else ("", "")
    )

    identity = {"schema": OFFICIAL_NATIVE_FORWARD_INBOX_SCHEMA, "packets": merged}
    write_bounded_json(
        inbox_path,
        {**identity, "content_sha256": content_sha256(identity)},
    )
    return {"packet_count": len(merged), "pruned": pruned}


def _utc_date(value: str) -> date:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    ).date()


def _official_native_run_ladder(
    *, event_date: str, as_of_utc: str
) -> list[tuple[int, int | None, datetime]]:
    """Published runs that could serve this event date, newest first.

    The scope names an event; which run serves it is decided at capture time,
    by freshness.  The lead label on the scope used to decide the run outright
    -- d1 meant "the run day's 00Z" no matter what had been published since --
    so a rotation could serve a P0 from a 30-hour-old field while a 6-hour-old
    one sat unread.  This walks the runs the lanes publish, newest first,
    skipping any not yet due by the clock; the capture's own grid-coverage
    check still rejects a run that cannot cover a station's local day, so
    falling through the ladder is exactly the "newest covering run" the
    training side already speaks (its hour buckets never cared about d-labels).
    A failure on the freshest rung is ordinary -- the newest run is the last to
    finish uploading -- and the next rung answers instead.

    Resolved before any download, so the caller can ask the cheaper question
    first: whether the newest published run is one this date already holds.
    """

    event_day = date.fromisoformat(event_date)
    now_dt = datetime.fromisoformat(
        as_of_utc.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    ladder: list[tuple[int, int | None, datetime]] = []
    # Six-hourly, matching what ECMWF and GEFS actually publish.  The old
    # 00Z/18Z-only rungs followed half their cycles: 06Z and 12Z published
    # daily and were never read, which capped every station-day's training
    # archive at roughly two runs when the publishers were issuing four.
    # ECCC and ICON ignore the rung's cycle -- the capture resolves their run
    # from their own per-lead contracts -- so rungs their publishers do not
    # serve cost them nothing.
    #
    # Same-day rungs past 00Z serve whoever they can still cover.  The batch
    # is all-or-nothing across its stations, and a same-day 06Z rung once
    # died whole on ``native_family_temperature_curve_point_count_invalid``
    # because the easternmost stations had two 3-hourly points left; the
    # capture now excludes structurally uncoverable stations up front
    # (deterministic from run time and settlement timezone, before any
    # download), so a 06Z/12Z/18Z rung captures the western remainder of the
    # network instead of failing on its eastern edge.  Excluded stations
    # keep their previous packet -- the freshest run that can actually cover
    # them -- and the training archive accumulates every (run, station-day)
    # pair a cycle can legitimately describe.
    # The three deep d2 rungs exist for the easternmost stations.  The lead
    # bucket is measured to the station's local day start, so the same d2 18Z
    # run that lands at 28-40h (h24_48) for Europe and the Americas is only
    # 22h (h12_24) at UTC+8 -- east of roughly UTC+3, no rung above reaches
    # the 24-48h band at all, which left ZUUU's h24_48 training bucket with
    # no ECMWF/GEFS material ever.  Publication order gives each rung its own
    # capture window: a d2 06Z run is the newest published run for a few
    # hours, is captured then, and the fresher-run filter retires it once
    # 12Z lands.  Western stations read the same captures as extra samples in
    # buckets they already serve; coverage is never at risk on a deep rung
    # (the farthest case, a UTC-8 day end from a d2 00Z init, is 56h, inside
    # every family's horizon).
    for rung_lead, rung_cycle in (
        (0, 18),
        (0, 12),
        (0, 6),
        (0, None),
        (1, 18),
        (1, 12),
        (1, 6),
        (1, None),
        (2, None),
        (2, 12),
        (2, 6),
        (2, 0),
    ):
        cycle_hour = (
            {2: 18, 1: 0, 0: 0}[rung_lead]
            if rung_cycle is None else rung_cycle
        )
        run_init = datetime.combine(
            event_day - timedelta(days=rung_lead),
            dt_time(hour=cycle_hour),
            tzinfo=timezone.utc,
        )
        if run_init < now_dt:
            ladder.append((rung_lead, rung_cycle, run_init))
    ladder.sort(key=lambda rung: rung[2], reverse=True)
    # Bounded, but wide enough to always reach a servable rung.  At six-hourly
    # cycles with a ~7.5h publication delay, up to two of the newest rungs are
    # initialised but not yet published, and a same-day 06Z rung can still fail
    # coverage for the easternmost stations -- three misses ahead of the rung
    # that works.  A bound of three could spend itself entirely on those misses
    # and lock a scope out of the run it can actually have.
    return ladder[:4]


def _native_forward_packet_is_stale(
    packet: Mapping[str, Any],
    *,
    as_of_utc: str,
) -> bool:
    """Whether the compiler would reject this packet for age.

    Mirrors the compiler's own window against the same capture timestamp, so
    the probe and the compile agree on what is usable.  An unreadable or
    missing timestamp is not called stale here: the compiler owns that
    judgement and rejects it with a precise reason, which is more useful than
    this quietly deciding to re-download.
    """

    captured_at = str(packet.get("received_at_utc") or "").strip()
    if not captured_at:
        return False
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        decision = datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if captured.tzinfo is None or decision.tzinfo is None:
        return False
    age_seconds = (
        decision.astimezone(timezone.utc) - captured.astimezone(timezone.utc)
    ).total_seconds()
    return age_seconds >= NATIVE_P0_COMPILE_FRESH_SECONDS


def _packet_taf_superseded(
    conn: sqlite3.Connection,
    packet: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
) -> bool:
    """Whether a newer TAF issuance exists than the one this packet carries.

    The arrival wake (``_taf_arrival_pending``) is process memory: a restart
    between the ingest and the rebuild drops it, and re-ingesting the same
    rows writes nothing, so the wake never fires again for that issuance.
    Measured 2026-08-14 on ZUUU: the 15:02Z TAF (TX 35) was received
    16:48:45Z under the previous daemon; two restarts later every compile
    through 17:09Z still served the 09:01Z issuance (TX 37) -- a 21.6%-weight
    member sitting two degrees warm -- and would have until the packet aged
    out six hours after build.  This derives the same judgement from durable
    state instead: the packet names the issuance it holds in
    ``run_init_utc_by_system``, and the capture table knows the newest one.

    Fail-open on any read problem: a broken probe must degrade to the old
    behaviour (reuse until TTL), never block the lane.
    """

    run_init = packet.get("run_init_utc_by_system")
    if not isinstance(run_init, Mapping):
        return False
    packet_taf = str(run_init.get("OFFICIAL_AIRPORT_TAF_TX") or "").strip()
    if not packet_taf:
        return False
    try:
        row = conn.execute(
            "SELECT MAX(issued_at_utc) FROM calibration_forward_taf_capture "
            "WHERE station_id=? AND event_date=? AND tx_available=1",
            (str(station_id), str(event_date)),
        ).fetchone()
    except sqlite3.Error:
        return False
    latest = str(row[0] or "").strip() if row else ""
    return bool(latest) and latest > packet_taf


def _is_republisher_daily_quota_exhausted(exc: BaseException) -> bool:
    """Whether this failure means the republisher is spent for the UTC day.

    A per-request 429 is transient and worth retrying on the next scope; a
    daily cap is not, and re-asking once per scope for the rest of the day is
    both pointless and rude to the provider.  Matched on the provider's own
    wording because the transport surfaces it verbatim and the two cases share
    a status code.
    """

    text = f"{exc}"
    return (
        "open_meteo_ensemble_rate_limited" in text
        and "daily api request limit exceeded" in text.lower()
    )


def _event_date_or_none(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _native_packet_run_init(packet: Mapping[str, Any]) -> datetime | None:
    """The run this packet's members came from, or None if it does not say.

    Packets are built at one shared cycle (``run_cycle_scope`` is
    ``SAME_NATIVE_CYCLE``), so the systems agree; the newest is taken anyway so
    a mixed packet is judged by the freshest evidence it holds rather than by
    its most stale.  A packet that names no run is treated as unjudgeable, not
    as superseded -- that keeps an unreadable field from forcing a download.
    """

    raw = packet.get("run_init_utc_by_system")
    if not isinstance(raw, Mapping) or not raw:
        return None
    parsed: list[datetime] = []
    for value in raw.values():
        try:
            parsed.append(
                datetime.fromisoformat(
                    str(value).replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            )
        except (TypeError, ValueError):
            return None
    return max(parsed) if parsed else None


def _resident_open_event_dates(
    path: Path,
    *,
    as_of_utc: str | None = None,
) -> tuple[str, ...]:
    """Return the current open station-day dates from the resident WS handoff.

    The forward NWP inbox is intentionally append-only and may retain several
    observational dates.  A production cycle must therefore get its event-day
    scope from the resident market handoff, not from whichever dates happen to
    be present in that inbox.  This helper is deliberately conservative: a
    missing, malformed, or multi-date open handoff yields no inferred scope and
    lets the normal downstream fail-closed gates report the actual blocker.

    ``event_day_state`` inside the handoff is frozen at write time, so a stale
    manifest keeps advertising a day that closed long ago.  That is not a
    harmless staleness: the inferred date becomes the NWP capture event-date
    allowlist, every registry scope is filtered out, capture fail-closes, and
    without new model data there is no new P0, no new envelope, and no new
    manifest -- the handoff can never refresh itself.  Dates that are already
    closed in every timezone on earth are therefore dropped here regardless of
    what the snapshot claims, which lets the scope fall back to live registry
    discovery instead of deadlocking on a frozen date.
    """

    if not path.is_file():
        return ()
    try:
        root = load_bounded_json(path)
    except Exception:
        return ()
    if not isinstance(root, Mapping):
        return ()
    schema = str(root.get("schema") or "").strip()
    dates: set[str] = set()
    if schema == MULTI_SCOPE_WS_MANIFEST_SCHEMA:
        summaries = root.get("scopes")
        if not isinstance(summaries, list):
            return ()
        for summary in summaries:
            if not isinstance(summary, Mapping):
                continue
            if str(summary.get("event_day_state") or "").strip() != "open":
                continue
            event_date = str(summary.get("event_date") or "").strip()
            if event_date:
                dates.add(event_date)
    else:
        # Legacy resident handoffs carry one explicit target date.
        event_date = str(root.get("target_local_date") or "").strip()
        if event_date and str(root.get("event_day_state") or "open").strip() == "open":
            dates.add(event_date)
    # UTC offsets span UTC-12..UTC+14, so an event day is certainly over once
    # the clock is more than one day past it.  This needs no per-station
    # timezone -- which the handoff summaries do not carry anyway -- and only
    # ever discards dates, never invents one.
    reference = _utc_date(as_of_utc) if as_of_utc else datetime.now(timezone.utc).date()
    horizon = reference - timedelta(days=1)
    dates = {
        event_date
        for event_date in dates
        if _event_date_or_none(event_date) is None
        or _event_date_or_none(event_date) >= horizon
    }
    # Reported, never used to narrow scope.  The manifest lists scopes that
    # already have a probability, so it can only ever name dates the pipeline
    # has already processed.  Every version of "drop the manifest's date when
    # it looks stale" has been a special case that the next stale date walked
    # around: first "closed everywhere on earth", which is a day too late, then
    # "earlier than today", which does nothing when the frozen date *is* today
    # -- observed on 2026-08-08, where a manifest naming only that day held
    # capture to 16 stations while the registry already carried 45 for
    # 2026-08-09.
    #
    # The loop closes because a day with no P0 never appears in the manifest,
    # never enters the capture allowlist, never gets model data, and so never
    # gets a P0.  No filter on the manifest can open it, because the manifest
    # is downstream of the thing it would be gating.  Live registry discovery
    # is upstream and already bounded, so it owns the scope.
    return tuple(sorted(dates))


class TargetProductionWorker:
    """Resident target compiler and target-local no-live ledger writer."""

    def __init__(
        self,
        db_path: Path,
        *,
        commit_conn: sqlite3.Connection | None = None,
        commit_error: str | None = None,
        activation_path: Path | None = None,
        market_manifest_path: Path | None = None,
        native_oos_snapshot_path: Path | None = None,
        canonical_source_db_path: Path | None = None,
        taf_artifact_path: Path | None = None,
        taf_inbox_path: Path | None = None,
        taf_bound_artifact_root: Path | None = None,
        p0_inbox_path: Path | None = None,
        nwp_runtime_root: Path | None = None,
        cash_phase_forward_store_path: Path | None = None,
        station_allowlist: Sequence[str] | None = None,
        event_date_allowlist: Sequence[str] | None = None,
    ) -> None:
        self.db_path = resolve_target_database(db_path)
        self.activation_path = Path(
            activation_path
            or os.environ.get("PM_TARGET_ACTIVATION", str(DEFAULT_TARGET_ACTIVATION))
        ).expanduser().resolve()
        self.market_manifest_path = resolve_target_ws_manifest(
            market_manifest_path
            or os.environ.get("PM_TARGET_MARKET_MANIFEST", str(DEFAULT_TARGET_MARKET_MANIFEST))
        )
        self.native_oos_snapshot_path = Path(
            native_oos_snapshot_path
            or os.environ.get(
                "PM_TARGET_NATIVE_OOS_SNAPSHOT_MANIFEST",
                str(DEFAULT_TARGET_NATIVE_OOS_SNAPSHOT_MANIFEST),
            )
        ).expanduser()
        self.native_oos_snapshot: NativeOosSnapshot | Mapping[str, Any] | None = None
        self.native_oos_snapshot_error: str | None = None
        writer_requested = str(
            os.environ.get("PM_TARGET_PRODUCTION_WRITER", "") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        production_no_live_runtime = (
            writer_requested
            or str(os.environ.get("PM_TARGET_EXECUTION_MODE", "") or "").strip()
            == PRODUCTION_EXECUTION_MODE
        )
        # A pointer snapshot only speaks for the stations it attests.  This
        # one is a hand-generated audit artifact: it froze 5 of 45 stations on
        # 2026-08-03, and every selection sha has moved since, so it rejected
        # the 40 stations it never covered outright and mis-bound the 5 it
        # did.  Fail-closed is right when the snapshot is the authority; it is
        # not an authority once it has stopped tracking the fleet, and the
        # append-only activation rows it was guarding carry their own OOS gate,
        # authority scope and generation order.  Opt in by path, so a replay
        # can still pin a snapshot deliberately and nothing loads a stale one
        # by default.
        snapshot_requested = bool(
            native_oos_snapshot_path
            or os.environ.get("PM_TARGET_NATIVE_OOS_SNAPSHOT_MANIFEST")
        )
        if production_no_live_runtime and snapshot_requested:
            try:
                self.native_oos_snapshot = load_native_oos_snapshot_manifest(
                    self.native_oos_snapshot_path
                )
            except Exception as exc:
                # Preserve a non-None invalid sentinel so every official
                # compiler attempt fails closed instead of silently falling
                # back to the newest append-only DB activation.
                self.native_oos_snapshot_error = (
                    f"{type(exc).__name__}:{exc}"
                )
                self.native_oos_snapshot = {
                    "schema": "invalid_native_oos_snapshot",
                    "load_error": self.native_oos_snapshot_error,
                }
        self.conn: sqlite3.Connection | None = None
        self.open_error: str | None = None
        try:
            self.conn = open_target_read_only(self.db_path)
        except Exception as exc:
            self.open_error = f"{type(exc).__name__}:{exc}"
        self.taf_artifact_path = Path(
            taf_artifact_path
            or os.environ.get(
                "PM_TARGET_TAF_ARTIFACT",
                str(DEFAULT_TARGET_TAF_ARTIFACT),
            )
        ).expanduser().resolve()
        self.taf_inbox_path = Path(
            taf_inbox_path
            or os.environ.get(
                "PM_TARGET_TAF_INBOX",
                str(DEFAULT_TARGET_TAF_INBOX),
            )
        ).expanduser().resolve()
        self.taf_bound_artifact_root = Path(
            taf_bound_artifact_root
            or os.environ.get(
                "PM_TARGET_TAF_BOUND_ARTIFACT_ROOT",
                str(DEFAULT_TARGET_TAF_BOUND_ARTIFACT_ROOT),
            )
        ).expanduser().resolve()
        self.p0_inbox_path = Path(
            p0_inbox_path
            or os.environ.get(
                "PM_TARGET_P0_INBOX",
                str(DEFAULT_TARGET_P0_INBOX),
            )
        ).expanduser().resolve()
        self.nwp_runtime_root = Path(
            nwp_runtime_root
            or os.environ.get(
                "PM_TARGET_NWP_RUNTIME_ROOT",
                str(DEFAULT_TARGET_NWP_RUNTIME_ROOT),
            )
        ).expanduser().resolve()
        raw_station_allowlist: Sequence[str]
        if station_allowlist is None:
            raw_station_allowlist = tuple(
                value
                for value in str(
                    os.environ.get("PM_TARGET_STATION_ALLOWLIST", "")
                    or ""
                ).split(",")
                if value.strip()
            )
            if (
                not raw_station_allowlist
                and self.db_path == DEFAULT_TARGET_DB.resolve()
            ):
                # Production scope is the frozen canonical 45-station
                # registry.  A station-specific vertical slice must be
                # passed explicitly and can never become the resident
                # default through an empty environment variable.
                raw_station_allowlist = CANONICAL_TARGET_STATIONS
        else:
            raw_station_allowlist = station_allowlist
        self.station_allowlist = tuple(
            sorted(
                {
                    str(value or "").strip().upper()
                    for value in raw_station_allowlist
                    if str(value or "").strip()
                }
            )
        )
        if len(self.station_allowlist) > 45:
            raise RuntimeError("target_station_allowlist_cap_exceeded")
        raw_event_dates: Sequence[str]
        if event_date_allowlist is None:
            raw_event_dates = tuple(
                value
                for value in str(
                    os.environ.get("PM_TARGET_EVENT_DATE_ALLOWLIST", "")
                    or ""
                ).split(",")
                if value.strip()
            )
        else:
            raw_event_dates = event_date_allowlist
        event_date_scope_source = "explicit" if raw_event_dates else "unconfigured"
        # No manifest-derived fallback.  An unset allowlist means "whatever the
        # live registry says is open", which is what a resident worker should
        # follow; deriving it from the handoff instead pinned the lane to the
        # days that already had a probability and starved every new one.
        if not raw_event_dates and production_no_live_runtime:
            event_date_scope_source = "live_registry_discovery"
            self.manifest_open_event_dates = _resident_open_event_dates(
                self.market_manifest_path
            )
        else:
            self.manifest_open_event_dates = ()
        self.event_date_allowlist = tuple(
            sorted(
                {
                    str(value or "").strip()
                    for value in raw_event_dates
                    if str(value or "").strip()
                }
            )
        )
        if len(self.event_date_allowlist) > 7:
            raise RuntimeError("target_event_date_allowlist_cap_exceeded")
        self.event_date_scope_source = event_date_scope_source
        self.nwp_rotation_state_path = (
            self.nwp_runtime_root / "rotation_state.json"
        )
        self.taf_rotation_state_path = (
            self.nwp_runtime_root / "taf_rotation_state.json"
        )
        self._nwp_rotation_state: dict[str, Any] | None = None
        self._nwp_rotation_state_load_error: str | None = None
        try:
            self._nwp_rotation_state = load_rotation_state(
                self.nwp_rotation_state_path,
                station_ids=self.station_allowlist,
            )
            if self._nwp_rotation_state is not None:
                self._resident_nwp_scope_cursor_by_priority = {
                    int(key): int(value)
                    for key, value in (
                        self._nwp_rotation_state.get(
                            "cursor_by_priority"
                        ) or {}
                    ).items()
                }
        except Exception as exc:
            self._nwp_rotation_state_load_error = (
                f"{type(exc).__name__}:{exc}"
            )
        self._resident_open_scope_cursor = 0
        self._native_ensemble_scope_cursor = int(
            getattr(
                self,
                "_resident_nwp_scope_cursor_by_priority",
                {},
            ).get(0, 0)
        )
        self.skip_nwp_refresh = str(
            os.environ.get("PM_TARGET_SKIP_NWP_REFRESH", "") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.cash_phase_forward_store_root = DEFAULT_STORE_ROOT
        self.cash_phase_forward_store_path = resolve_forward_store_path(
            cash_phase_forward_store_path
            or os.environ.get(
                "PM_TARGET_CASH_PHASE_FORWARD_STORE",
                str(DEFAULT_CASH_PHASE_FORWARD_OBSERVATIONS),
            ),
            allowed_root=DEFAULT_STORE_ROOT,
        )
        self.commit_conn = commit_conn
        self.commit_error = commit_error
        self._last_delta_v2_truth_scan_monotonic: float | None = None
        self._last_delta_v2_label_scan_monotonic: float | None = None
        # Where the next forward-day sweep starts.  A pass that does not
        # survive to the end of the catalog must not hand the next pass the
        # same head of the alphabet, or the stations behind the truncation
        # point are the same ones every cycle.
        self._forward_p0_rotation_cursor: str = ""
        self._delta_v2_truth_fact_cursor = 0
        self._delta_v2_truth_cursor_initialized = False
        self._delta_v2_label_fact_cursor = 0
        self._delta_v2_label_cursor_initialized = False
        self.canonical_source_conn: sqlite3.Connection | None = None
        self.canonical_source_db_path: Path | None = None
        self.canonical_source_error: str | None = None
        if canonical_source_db_path is not None:
            source_path = Path(
                canonical_source_db_path
            ).expanduser().resolve()
            self.canonical_source_db_path = source_path
            try:
                self.canonical_source_conn = sqlite3.connect(
                    f"file:{source_path}?mode=ro",
                    uri=True,
                    timeout=30.0,
                    isolation_level=None,
                    check_same_thread=False,
                )
                self.canonical_source_conn.row_factory = sqlite3.Row
                self.canonical_source_conn.execute(
                    "PRAGMA busy_timeout=30000"
                )
                self.canonical_source_conn.execute(
                    "PRAGMA query_only=ON"
                )
            except Exception as exc:
                self.canonical_source_error = (
                    f"{type(exc).__name__}:{exc}"
                )

    def reopen_target_connections(self) -> None:
        """Rebuild target SSOT handles after a recoverable SQLite I/O fault.

        SQLite marks a connection unusable after some ``SQLITE_IOERR``
        variants.  Keeping that handle resident turns a transient filesystem
        fault into a permanent fail-closed loop even when a fresh connection
        can read and write the same database.  Reopen both target handles as
        one worker-owned unit; canonical truth remains on its independent
        read-only connection.
        """

        for connection in (self.conn, self.commit_conn):
            if connection is None:
                continue
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self.conn = None
        self.commit_conn = None
        self.open_error = None
        self.commit_error = None
        try:
            self.conn = open_target_read_only(self.db_path)
            self.commit_conn = open_target_writer_if_enabled(
                self.db_path,
                activation_path=self.activation_path,
            )
            if self.commit_conn is None:
                raise RuntimeError("target_atomic_writer_not_enabled")
        except Exception as exc:
            self.open_error = f"{type(exc).__name__}:{exc}"
            self.commit_error = (
                "target_writer_reopen_failed:"
                f"{type(exc).__name__}:{exc}"
            )
            for connection in (self.conn, self.commit_conn):
                if connection is None:
                    continue
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            self.conn = None
            self.commit_conn = None
            raise

    def _capture_official_native_packet(
        self,
        *,
        station_id: str,
        event_date: str,
        as_of_utc: str,
        inbox_path: Path,
        lead_days: int = 2,
    ) -> dict[str, Any]:
        """Start a background capture for one scope; never block the caller.

        Official-native coverage is what actually gates P0: a republisher packet
        is filtered out of the P0 inbox by design, so a scope without an
        official packet yields no envelope at all.  Coverage therefore has to
        grow fast, but one capture takes minutes.

        Running it inline on the forecast worker was the wrong trade in both
        directions: uncapped it stalled the rotation at roughly one scope per
        six hours, and once throttled hard enough to protect the rotation, the
        coverage crawled.  Captures now run on their own small pool, so several
        scopes fetch concurrently while the rotation keeps sweeping at full
        speed.  The work is subprocess-bound, so these threads are almost
        entirely idle wait.

        This returns immediately with the scope's admission state.  The packet
        lands in the shared inbox when its capture finishes, and the rotation
        picks it up on a later pass.
        """


        throttle: dict[tuple[str, ...], float] = getattr(
            self, "_official_native_capture_attempts", None
        ) or {}
        self._official_native_capture_attempts = throttle
        inflight: dict[tuple[str, ...], Any] = getattr(
            self, "_official_native_capture_inflight", None
        )
        if inflight is None:
            inflight = {}
            self._official_native_capture_inflight = inflight
        # Which run each event date has already been captured from.  These
        # families publish on a fixed daily cycle, so a run that has been read
        # once has nothing further to give: re-reading it re-downloads the same
        # immutable objects.  The clock-based retry floor below only governs
        # *failed* attempts; a success is retired by run identity instead, and
        # the next download waits for the next cycle to publish.
        captured_runs: dict[str, str] = getattr(
            self, "_official_native_capture_runs", None
        )
        if captured_runs is None:
            captured_runs = {}
            self._official_native_capture_runs = captured_runs
        if getattr(self, "_official_native_capture_pool", None) is None:
            # Small, bounded, and lazily created so a worker that never
            # captures (tests, --once paths) starts no threads at all.  The
            # work is subprocess-bound, so these threads sit in wait.
            self._official_native_capture_pool = ThreadPoolExecutor(
                max_workers=OFFICIAL_NATIVE_CAPTURE_MAX_CONCURRENCY,
                thread_name_prefix="official-native-capture",
            )
            self._official_native_capture_merge_lock = threading.Lock()
        # What gets downloaded is a model run, not a station.  Every family
        # here publishes whole global fields on a fixed cycle; a station is an
        # extraction from those fields, so forty-five stations of one event
        # date cost one download, not forty-five.  Keying this on the station
        # was what made them cost forty-five: retained archives are matched on
        # the exact station set, so one station's archive never served the
        # next, and each scope re-fetched the same objects.
        #
        # The key is therefore the event date alone.  The lead does not belong
        # in it either: the run ladder resolves all three lead-labelled scopes
        # of a day to the same "newest covering run", so keying them apart
        # would download that run three times.
        scope_key = (event_date,)
        now_mono = time.monotonic()

        # Reap finished captures so their scopes free a concurrency slot, and
        # keep the newest outcome visible for the status receipt.
        for key in [key for key, fut in inflight.items() if fut.done()]:
            finished = inflight.pop(key, None)
            try:
                outcome = finished.result()
            except Exception as exc:
                outcome = {
                    "state": "FAILED",
                    "reason": f"{type(exc).__name__}:{str(exc)[:200]}",
                }
            self._official_native_capture_last_result = {
                "event_date": key[0],
                **outcome,
            }
            if str(outcome.get("state") or "") == "CAPTURED":
                captured_runs[key[0]] = str(
                    outcome.get("captured_run_init_utc") or ""
                )

        if scope_key in inflight:
            return {
                "state": "IN_FLIGHT",
                "station_id": station_id,
                "event_date": event_date,
                "calls_order_gateway": False,
                "places_orders": False,
            }
        if len(inflight) >= OFFICIAL_NATIVE_CAPTURE_MAX_CONCURRENCY:
            return {
                "state": "DEFERRED_CAPTURE_SLOTS_BUSY",
                "station_id": station_id,
                "event_date": event_date,
                "in_flight": len(inflight),
                "calls_order_gateway": False,
                "places_orders": False,
            }
        # Resolve the run before deciding to spend anything.  These families
        # publish on a fixed cycle, so "is there a newer run than the one this
        # date already holds" is the whole question -- if there is not, the
        # answer is to download nothing at all and wait for the next cycle.
        run_ladder = _official_native_run_ladder(
            event_date=event_date, as_of_utc=as_of_utc
        )
        if not run_ladder:
            return {
                "state": "SKIPPED",
                "reason": "no_published_run_covers_event_date",
                "station_id": station_id,
                "event_date": event_date,
                "calls_order_gateway": False,
                "places_orders": False,
            }
        newest_run_init = (
            run_ladder[0][2].isoformat().replace("+00:00", "Z")
        )
        if captured_runs.get(event_date) == newest_run_init:
            return {
                "state": "RUN_ALREADY_CAPTURED",
                "station_id": station_id,
                "event_date": event_date,
                "captured_run_init_utc": newest_run_init,
                "calls_order_gateway": False,
                "places_orders": False,
            }
        # Only rungs newer than what this date already holds are worth trying.
        # The freshest run is the last to finish uploading, so an attempt on it
        # failing is ordinary -- but without this the ladder would then fall
        # back onto the run already captured and download it a second time,
        # which is the opposite of what the fallback is for.  Falling through
        # to nothing is the correct outcome: the date keeps what it has and
        # retries after its floor.
        held_run_init = captured_runs.get(event_date)
        if held_run_init:
            try:
                held = datetime.fromisoformat(
                    held_run_init.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except ValueError:
                held = None
            if held is not None:
                run_ladder = [
                    rung for rung in run_ladder if rung[2] > held
                ]
                if not run_ladder:
                    return {
                        "state": "RUN_ALREADY_CAPTURED",
                        "station_id": station_id,
                        "event_date": event_date,
                        "captured_run_init_utc": held_run_init,
                        "calls_order_gateway": False,
                        "places_orders": False,
                    }
        last_attempt = throttle.get(scope_key)
        if (
            last_attempt is not None
            and now_mono - last_attempt < OFFICIAL_NATIVE_CAPTURE_RETRY_SEC
        ):
            return {
                "state": "THROTTLED",
                "station_id": station_id,
                "event_date": event_date,
                "retry_after_sec": round(
                    OFFICIAL_NATIVE_CAPTURE_RETRY_SEC - (now_mono - last_attempt), 1
                ),
                "calls_order_gateway": False,
                "places_orders": False,
            }
        throttle[scope_key] = now_mono

        registry_path = self.nwp_runtime_root / "market_registry_forward.json"
        if not registry_path.is_file():
            return {
                "state": "SKIPPED",
                "reason": "forward_market_registry_missing",
                "station_id": station_id,
                "event_date": event_date,
                "calls_order_gateway": False,
                "places_orders": False,
            }
        capture_root = self.nwp_runtime_root / "official_native_capture"
        capture_root.mkdir(parents=True, exist_ok=True)
        staging_inbox = capture_root / f"run_{event_date}_inbox.json"
        # The capture runs as a child process, not in-process.  Its per-request
        # timeouts do not bound total wall time -- several archive reads plus an
        # inbox build ran well past two minutes in production -- and only a
        # child can be killed outright when it overruns; a thread cannot.
        base_command = [
            sys.executable,
            str(ROOT / "scripts" / "capture_official_native_ensemble_forward.py"),
            "--event-date", event_date,
            "--market-registry", str(registry_path),
            # The whole day, not the one station the rotation happens to be
            # pointing at.  Every family below ships global fields on a fixed
            # cycle, and a station is an extraction from them: measured on this
            # lane, forty-five stations cost 517MiB of ECMWF against 259MiB for
            # one, and 70MiB of GEFS against 41MiB.  Twice the bytes for
            # forty-five times the coverage.  Asking per station instead turned
            # that into forty-five separate full downloads, because retained
            # archives are matched on the exact station set and one station's
            # archive never satisfied the next.
            #
            # Stations whose market has not opened for the day are skipped
            # rather than fatal: an event date three days out legitimately
            # lists 29 of 45, and the run still has to be read for those 29.
            "--registry-scope-stations",
            #
            # ECCC is captured on the same rotation rather than left to operator
            # commands.  Its history is what admits a third independent family
            # to the cross-ensemble candidate set; a system whose archive stops
            # growing can never cover the holdout cohort and is silently
            # excluded, which is how per-station model selection ended up with a
            # candidate set of exactly two.
            "--inbox-output", str(staging_inbox),
            "--timeout-seconds", str(OFFICIAL_NATIVE_CAPTURE_TIMEOUT_SEC),
            # Every capture also lands in the historical member table.  The
            # rotation was downloading these systems daily and then discarding
            # the member values once the inbox packet was built, so the
            # training archive only ever advanced when someone ran a manual
            # backfill -- ECMWF, GEFS and ECCC all froze at 2026-08-07, the
            # last day a person did.  ICON never froze because its daily lane
            # commits, and this makes the other three behave like it.
            "--commit",
        ]
        horizon = _utc_date(as_of_utc) - timedelta(days=1)
        republisher_inbox = (
            self.nwp_runtime_root / "native_ensemble_forward_inbox.json"
        )

        # ECMWF's S3 endpoint throttles sustained parallel range requests with
        # `503 Slow Down` and is therefore held to three workers; its GCS
        # replica serves the same official objects without that limit and is
        # allowed sixteen.  Since ECMWF dominates a capture (51 members), the
        # replica is what keeps four archives inside one deadline.
        capture_env = {
            **os.environ,
            "ECMWF_ARCHIVE_TRANSPORT_BASE_URL": ECMWF_GCS_REPLICA_BASE_URL,
        }

        def _run_capture() -> dict[str, Any]:
            captured_run_init: str | None = None
            try:
                completed = None
                for rung_lead, rung_cycle, rung_run_init in run_ladder:
                    attempt = base_command + [
                        "--lead-days", str(rung_lead),
                        "--gefs-output", str(
                            capture_root / f"gefs_{event_date}_d{rung_lead}.json"
                        ),
                        "--ecmwf-output", str(
                            capture_root / f"ecmwf_{event_date}_d{rung_lead}.json"
                        ),
                        "--eccc-output", str(
                            capture_root / f"eccc_{event_date}_d{rung_lead}.json"
                        ),
                        # ICON belongs with the others now.  It was held out
                        # while this lane asked per station, because its objects
                        # are whole global fields and a per-station rotation
                        # pulled the same ~543MiB once for each of them.  That
                        # was a property of the dispatch, not of the family:
                        # the pass now reads one run for every station the day
                        # lists, which is exactly what the separate daily lane
                        # was doing, so there is no longer a second lane to be
                        # separate from.
                        "--icon-output", str(
                            capture_root / f"icon_{event_date}_d{rung_lead}.json"
                        ),
                    ]
                    if rung_cycle is not None:
                        attempt += ["--run-cycle-hour", str(rung_cycle)]
                    completed = subprocess.run(
                        attempt,
                        cwd=str(ROOT),
                        capture_output=True,
                        text=True,
                        timeout=OFFICIAL_NATIVE_CAPTURE_DEADLINE_SEC,
                        env=capture_env,
                    )
                    if completed.returncode == 0:
                        captured_run_init = (
                            rung_run_init.isoformat().replace("+00:00", "Z")
                        )
                        break
                if completed is None or completed.returncode != 0:
                    return {
                        "state": "FAILED",
                        "reason": (
                            "run_ladder_exhausted:"
                            f"capture_exit_{completed.returncode if completed is not None else 'none'}:"
                            f"{((completed.stderr if completed is not None else '') or '').strip()[:200]}"
                        ),
                    }
            except subprocess.TimeoutExpired:
                # Overrunning is an ordinary outcome, not an error: the scope is
                # skipped and retried after its own floor.
                return {
                    "state": "DEADLINE_EXCEEDED",
                    "reason": (
                        "capture_exceeded_"
                        f"{OFFICIAL_NATIVE_CAPTURE_DEADLINE_SEC:.0f}s"
                    ),
                }
            except Exception as exc:
                return {
                    "state": "FAILED",
                    "reason": f"{type(exc).__name__}:{str(exc)[:200]}",
                }
            # Captures finish concurrently and all merge into one shared file,
            # so serialise the read-modify-write.
            try:
                with self._official_native_capture_merge_lock:
                    merged = _merge_official_native_inbox(
                        inbox_path,
                        staging_path=staging_inbox,
                        as_of_utc=as_of_utc,
                        # The scope set is bounded by the live market registry,
                        # not by whatever the republisher inbox happens to hold.
                        # Gating on the republisher meant a scope it had not yet
                        # covered could never accept its official packet: ZUUU
                        # 2026-08-07 was captured successfully and then silently
                        # dropped at merge, leaving the station with no P0 at
                        # all.  Official capture exists precisely to cover what
                        # the republisher cannot.
                        allowed_scopes=_active_registry_scopes(
                            registry_path,
                            horizon=horizon,
                        ),
                        # Read here rather than inside the merge so the merge
                        # stays a pure function of what it is handed.
                        fitted_lead_buckets=_fitted_lead_buckets(
                            self.conn, as_of_utc=as_of_utc
                        ),
                    )
            except Exception as exc:
                return {
                    "state": "MERGE_FAILED",
                    "reason": f"{type(exc).__name__}:{str(exc)[:200]}",
                }
            return {
                "state": "CAPTURED",
                "inbox_packet_count": merged["packet_count"],
                "pruned_packet_count": merged["pruned"],
                # Which run this scope actually got, so a reader can tell a
                # fresh rung from a fallback without re-deriving the ladder.
                "captured_run_init_utc": captured_run_init,
            }

        future = self._official_native_capture_pool.submit(_run_capture)
        inflight[scope_key] = future
        return {
            "state": "SUBMITTED",
            "station_id": station_id,
            "event_date": event_date,
            "in_flight": len(inflight),
            "last_completed": dict(
                getattr(self, "_official_native_capture_last_result", {}) or {}
            ),
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }

    def _metar_trajectory_training_due(
        self, *, station_id: str
    ) -> tuple[bool, str, str]:
        """Whether this station's newest settled day lacks artifact coverage.

        Returns ``(due, settlement_timezone, latest_truth_day)``.  Coverage is
        judged against ``trained_through_utc`` of the newest trajectory
        artifact: folds only exist for settled days, so an artifact trained
        through the latest settled day's close cannot gain evidence until the
        next day settles, and rerunning the gate before then is a no-op by
        construction.
        """

        # The read-only handle: this runs on the network-prefetch worker, which
        # deliberately holds no write connection to the target SSOT.  Requiring
        # ``commit_conn`` here silently reported NOT_CONFIGURED on every cycle
        # and the pass never fired.  All queries in this method are reads; the
        # only write in the chain is the fit subprocess's own gated commit.
        read_conn = getattr(self, "conn", None) or self.commit_conn
        if read_conn is None:
            return False, "", ""
        row = read_conn.execute(
            "SELECT settlement_timezone, MAX(local_date) "
            "FROM calibration_station_daily_high_truth "
            "WHERE station_id=? AND qc_status='ACCEPTED'",
            (station_id,),
        ).fetchone()
        if row is None or not row[0] or not row[1]:
            return False, "", ""
        timezone_name, latest_day = str(row[0]), str(row[1])
        day_end = settlement_day_window(latest_day, timezone_name)[
            "target_day_end_utc"
        ]
        covered = read_conn.execute(
            "SELECT MAX(trained_through_utc) "
            "FROM basket_probability_evidence_artifact "
            "WHERE station_id=? AND artifact_kind='METAR_TRAJECTORY_LIKELIHOOD'",
            (station_id,),
        ).fetchone()
        newest = str((covered or [None])[0] or "")
        if not newest:
            return True, timezone_name, latest_day
        # Parsed, not lexicographic: trained_through carries milliseconds and
        # the settlement window does not, and "…00.000Z" sorts before "…00Z"
        # as text -- a covered station would look forever due.
        try:
            covered_at = datetime.fromisoformat(newest.replace("Z", "+00:00"))
            closes_at = datetime.fromisoformat(
                str(day_end).replace("Z", "+00:00")
            )
        except ValueError:
            return True, timezone_name, latest_day
        return covered_at < closes_at, timezone_name, latest_day

    def _run_metar_trajectory_training(
        self, *, as_of_utc: str
    ) -> dict[str, Any]:
        """Run the packet->fit->gated-persist chain for one due station.

        One station per cycle, rotated, on its own single-worker pool -- the
        same shape as the ICON daily pass.  The subprocesses read the target
        SSOT and canonical archive and only the fit's ``--commit`` step writes,
        through the evidence store's validated inserts.
        """

        boundary = {
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
        if (
            (getattr(self, "conn", None) is None and self.commit_conn is None)
            or self.canonical_source_db_path is None
            or not self.station_allowlist
        ):
            return {"state": "NOT_CONFIGURED", **boundary}
        inflight = getattr(self, "_metar_trajectory_inflight", None)
        if inflight is not None and not inflight.done():
            return {"state": "IN_FLIGHT", **boundary}
        cursor = int(getattr(self, "_metar_trajectory_station_cursor", 0))
        throttle: dict[str, float] = getattr(
            self, "_metar_trajectory_attempts", None
        ) or {}
        self._metar_trajectory_attempts = throttle
        now_mono = time.monotonic()
        chosen: tuple[str, str, str] | None = None
        for offset in range(len(self.station_allowlist)):
            station = self.station_allowlist[
                (cursor + offset) % len(self.station_allowlist)
            ]
            last = throttle.get(station)
            if (
                last is not None
                and now_mono - last < METAR_TRAJECTORY_TRAINING_RETRY_SEC
            ):
                continue
            try:
                due, timezone_name, latest_day = (
                    self._metar_trajectory_training_due(station_id=station)
                )
            except Exception:
                continue
            if due:
                chosen = (station, timezone_name, latest_day)
                self._metar_trajectory_station_cursor = (
                    cursor + offset + 1
                ) % len(self.station_allowlist)
                break
        if chosen is None:
            self._metar_trajectory_station_cursor = (
                cursor + 1
            ) % len(self.station_allowlist)
            return {"state": "NOT_DUE", **boundary}
        station, timezone_name, latest_day = chosen
        throttle[station] = now_mono
        generation = 1 + int(
            (getattr(self, "conn", None) or self.commit_conn).execute(
                "SELECT COUNT(*) FROM basket_probability_evidence_artifact "
                "WHERE station_id=? "
                "AND artifact_kind='METAR_TRAJECTORY_LIKELIHOOD'",
                (station,),
            ).fetchone()[0]
        )
        target_db = str(self.db_path)
        archive_db = str(self.canonical_source_db_path)
        work_root = self.nwp_runtime_root / "metar_trajectory_training"
        work_root.mkdir(parents=True, exist_ok=True)
        valid_until = (
            datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
            + timedelta(days=METAR_TRAJECTORY_ARTIFACT_VALID_DAYS)
        ).astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")

        def _train() -> dict[str, Any]:
            outcomes: list[dict[str, Any]] = []
            for bucket in METAR_TRAJECTORY_TRAINING_BUCKETS:
                prefix = work_root / f"{station}_{bucket.lower()}"
                packet_path = Path(f"{prefix}_packet.json")
                build = subprocess.run(
                    [
                        sys.executable,
                        str(
                            ROOT
                            / "scripts"
                            / "build_bayes_basket_target_metar_trajectory_training_packet.py"
                        ),
                        "--target-db", target_db,
                        "--archive-db", archive_db,
                        "--station-id", station,
                        "--settlement-timezone", timezone_name,
                        "--local-time-bucket", bucket,
                        "--decided-at-utc", as_of_utc,
                        "--valid-from-utc", as_of_utc,
                        "--valid-until-utc", valid_until,
                        "--generation", str(generation),
                        # Fold cutoffs anchor to the first report after the
                        # bucket start, matching how runtime P1 is actually
                        # evaluated -- and without it every sparse-archive day
                        # (5 reports/day before 2026-08-01) fails the
                        # bucket-start prefix requirement outright.
                        "--anchor-to-first-report",
                        "--output", str(packet_path),
                    ],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=METAR_TRAJECTORY_TRAINING_DEADLINE_SEC,
                )
                if build.returncode != 0:
                    outcomes.append({
                        "local_time_bucket": bucket,
                        "state": "PACKET_FAILED",
                        "reason": (build.stderr or "").strip()[-200:],
                    })
                    continue
                fit = subprocess.run(
                    [
                        sys.executable,
                        str(
                            ROOT
                            / "scripts"
                            / "build_bayes_basket_target_metar_trajectory_oos.py"
                        ),
                        "--input", str(packet_path),
                        "--output", str(Path(f"{prefix}_artifact.json")),
                        "--packet-output", str(Path(f"{prefix}_oos.json")),
                        "--db", target_db,
                        "--commit",
                    ],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=METAR_TRAJECTORY_TRAINING_DEADLINE_SEC,
                )
                if fit.returncode != 0:
                    # A nonzero exit covers two very different situations, and
                    # the packet output tells them apart: BLOCKED_EVIDENCE is
                    # the gate saying "not enough curve-complete folds yet",
                    # which is the expected state for every station whose
                    # recent days have not settled -- an error label there
                    # would make the receipt cry wolf on all 45 stations.
                    blocked_reason = ""
                    try:
                        blocked = json.loads(
                            Path(f"{prefix}_oos.json").read_text(
                                encoding="utf-8"
                            )
                        )
                        if blocked.get("state") == "BLOCKED_EVIDENCE":
                            blocked_reason = str(blocked.get("reason") or "")
                    except Exception:
                        pass
                    outcomes.append({
                        "local_time_bucket": bucket,
                        "state": (
                            "BLOCKED_EVIDENCE"
                            if blocked_reason
                            else "FIT_FAILED"
                        ),
                        "reason": (
                            blocked_reason
                            or (fit.stderr or "").strip()[-200:]
                        ),
                    })
                    continue
                summary: dict[str, Any] = {}
                try:
                    summary = json.loads(
                        (fit.stdout or "").strip().splitlines()[-1]
                    )
                except Exception:
                    pass
                outcomes.append({
                    "local_time_bucket": bucket,
                    "state": (
                        "PROMOTED"
                        if summary.get("oos_gate_passed")
                        else "GATE_REJECTED"
                    ),
                    "artifact_id": summary.get("artifact_id"),
                    "ingress": summary.get("ingress"),
                })
            result = {
                "station_id": station,
                "latest_truth_day": latest_day,
                "generation": generation,
                "outcomes": outcomes,
            }
            self._metar_trajectory_last_result = result
            return result

        pool = getattr(self, "_metar_trajectory_pool", None)
        if pool is None:
            self._metar_trajectory_pool = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="metar-trajectory-training",
            )
            pool = self._metar_trajectory_pool
        self._metar_trajectory_inflight = pool.submit(_train)
        return {
            "state": "SUBMITTED",
            "station_id": station,
            "latest_truth_day": latest_day,
            "last_completed": dict(
                getattr(self, "_metar_trajectory_last_result", {}) or {}
            ),
            **boundary,
        }

    def _run_correction_auto_clear(self, *, as_of_utc: str) -> dict[str, Any]:
        """Resolve pending correction halts by standing policy, on a schedule.

        The decision each correction gets follows from the report itself and is
        checked against the stored evidence, so this schedules an existing
        policy rather than deciding anything here.  It runs as a subprocess
        because the receipts belong in the canonical archive, which this worker
        holds read-only by design.
        """

        boundary = {
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
        if self.canonical_source_db_path is None:
            return {"state": "NOT_CONFIGURED", **boundary}
        inflight = getattr(self, "_correction_auto_clear_inflight", None)
        if inflight is not None and not inflight.done():
            return {"state": "IN_FLIGHT", **boundary}
        now_mono = time.monotonic()
        last_attempt = getattr(self, "_correction_auto_clear_last_mono", None)
        if (
            last_attempt is not None
            and now_mono - last_attempt < CORRECTION_AUTO_CLEAR_RETRY_SEC
        ):
            return {
                "state": "THROTTLED",
                "retry_in_seconds": round(
                    CORRECTION_AUTO_CLEAR_RETRY_SEC - (now_mono - last_attempt),
                    1,
                ),
                **boundary,
            }
        self._correction_auto_clear_last_mono = now_mono
        since = (
            datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .date()
            - timedelta(days=CORRECTION_AUTO_CLEAR_WINDOW_DAYS)
        ).isoformat()
        command = [
            sys.executable,
            str(
                ROOT / "scripts" / "auto_clear_pending_correction_reviews.py"
            ),
            "--archive-db", str(self.canonical_source_db_path),
            "--target-db", str(self.db_path),
            "--since", since,
            "--as-of-utc", as_of_utc,
            "--commit",
        ]

        def _run() -> dict[str, Any]:
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=CORRECTION_AUTO_CLEAR_DEADLINE_SEC,
                )
            except Exception as exc:
                result = {
                    "state": "FAILED",
                    "reason": f"{type(exc).__name__}:{str(exc)[:200]}",
                }
                self._correction_auto_clear_last_result = result
                return result
            summary: dict[str, Any] = {}
            try:
                summary = json.loads(
                    (completed.stdout or "").strip().splitlines()[-1]
                )
            except Exception:
                pass
            result = {
                "state": "CLEARED" if completed.returncode == 0 else "PARTIAL",
                "since": since,
                "candidate_scope_count": summary.get("candidate_scope_count"),
                "cleared_scope_count": summary.get("cleared_scope_count"),
                "cleared_observation_count": summary.get(
                    "cleared_observation_count"
                ),
                "skipped_count": summary.get("skipped_count"),
                "stderr": (completed.stderr or "").strip()[-200:],
            }
            self._correction_auto_clear_last_result = result
            return result

        pool = getattr(self, "_correction_auto_clear_pool", None)
        if pool is None:
            self._correction_auto_clear_pool = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="correction-auto-clear",
            )
            pool = self._correction_auto_clear_pool
        self._correction_auto_clear_inflight = pool.submit(_run)
        return {
            "state": "SUBMITTED",
            "since": since,
            "last_completed": dict(
                getattr(self, "_correction_auto_clear_last_result", {}) or {}
            ),
            **boundary,
        }

    def _historical_taf_capture_stations(self) -> list[str]:
        """The allowlisted stations whose TAF has ever carried a TX group.

        ``TX``/``TN`` are optional in a TAF and a large part of the network --
        every US station among them -- simply never publishes them.  Those
        stations cannot produce a calibration sample no matter how often they
        are captured, and the AWC response budget is shared, so spending it on
        them takes it from the stations that can.

        Derived from what the archive has actually yielded rather than from a
        hardcoded list, so a station that starts publishing TX is picked up.
        Before anything has been captured the question cannot be answered yet,
        and the whole allowlist is tried once.
        """

        allowlist = list(getattr(self, "station_allowlist", ()) or ())
        read_conn = getattr(self, "conn", None) or self.commit_conn
        if read_conn is None or not allowlist:
            return allowlist
        try:
            rows = read_conn.execute(
                "SELECT DISTINCT station_id "
                "FROM calibration_historical_taf_capture "
                "WHERE tx_c IS NOT NULL"
            ).fetchall()
        except Exception:
            return allowlist
        yielded = {str(row[0]).strip().upper() for row in rows}
        if not yielded:
            return allowlist
        return [station for station in allowlist if station in yielded]

    def _capture_historical_taf_daily(
        self, *, as_of_utc: str
    ) -> dict[str, Any]:
        """Re-capture the closed days still inside AWC's 30-day window.

        The capture subprocess writes bounded archive files and a receipt;
        the pass then ingests them into the calibration table, batched under
        the ingest CLI's archive cap.  Ingest is a frozen-fact replay, so the
        rolling window's unchanged days verify rather than duplicate.  No
        order gateway is ever touched.
        """

        boundary = {
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
        stations = self._historical_taf_capture_stations()
        if not stations:
            return {"state": "NOT_CONFIGURED", **boundary}
        inflight = getattr(self, "_historical_taf_capture_inflight", None)
        if inflight is not None and not inflight.done():
            return {"state": "IN_FLIGHT", **boundary}
        now_mono = time.monotonic()
        last_attempt = getattr(self, "_historical_taf_capture_last_mono", None)
        if (
            last_attempt is not None
            and now_mono - last_attempt < HISTORICAL_TAF_CAPTURE_RETRY_SEC
        ):
            return {
                "state": "THROTTLED",
                "retry_in_seconds": round(
                    HISTORICAL_TAF_CAPTURE_RETRY_SEC
                    - (now_mono - last_attempt),
                    1,
                ),
                **boundary,
            }
        self._historical_taf_capture_last_mono = now_mono
        as_of_day = datetime.fromisoformat(
            as_of_utc.replace("Z", "+00:00")
        ).astimezone(timezone.utc).date()
        end_date = as_of_day - timedelta(days=HISTORICAL_TAF_CAPTURE_LAG_DAYS)
        start_date = end_date - timedelta(
            days=HISTORICAL_TAF_CAPTURE_WINDOW_DAYS - 1
        )
        capture_root = self.nwp_runtime_root / "historical_taf_multianchor"
        capture_root.mkdir(parents=True, exist_ok=True)
        receipt_path = capture_root / "receipt.json"
        command = [
            sys.executable,
            str(
                ROOT
                / "scripts"
                / "backfill_awc_taf_calibration_multistation.py"
            ),
            "--db", str(self.db_path),
            "--start-date", start_date.isoformat(),
            "--end-date", end_date.isoformat(),
            "--output-dir", str(capture_root),
            # Days already complete are kept and only the transport failures
            # are retried, so the overlapping window costs little after the
            # first pass.
            "--resume",
            "--max-workers", "2",
            "--request-interval-sec", "0.5",
            "--timeout-sec", "30",
            "--receipt", str(receipt_path),
        ]
        for station in stations:
            command.extend(["--station-id", station])
        for anchor in HISTORICAL_TAF_DECISION_ANCHOR_HOURS:
            command.extend(["--decision-anchor-hour", str(anchor)])
        # AWC is reached directly.  The service is public, and the ambient
        # proxy this host exports is for a different lane entirely -- routed
        # through it the capture dies in CONNECT tunnel failures rather than
        # returning bulletins.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {
                "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                "AWC_HTTP_PROXY",
            }
        }

        def _run_capture() -> dict[str, Any]:
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=HISTORICAL_TAF_CAPTURE_DEADLINE_SEC,
                    env=environment,
                )
            except subprocess.TimeoutExpired:
                return {
                    "state": "DEADLINE_EXCEEDED",
                    "reason": (
                        "historical_taf_capture_exceeded_"
                        f"{HISTORICAL_TAF_CAPTURE_DEADLINE_SEC:.0f}s"
                    ),
                }
            except Exception as exc:
                return {
                    "state": "FAILED",
                    "reason": f"{type(exc).__name__}:{str(exc)[:200]}",
                }
            summary: dict[str, Any] = {}
            try:
                summary = json.loads(
                    (completed.stdout or "").strip().splitlines()[-1]
                )
            except Exception:
                pass
            # Captured archives are ingested in the same pass.  This lane
            # originally left committing "a separate, explicit step", which is
            # the same shape that froze every other system's training archive
            # at the last day a person ran the manual step; an archive on disk
            # that never reaches the table does not exist as far as the fit is
            # concerned.  Ingest is a frozen-fact replay, so re-ingesting the
            # rolling window's unchanged days verifies rather than duplicates.
            archives = sorted(capture_root.glob("AWC_TAF_*.json"))
            ingested = 0
            ingest_failures: list[str] = []
            for start in range(0, len(archives), 40):
                batch = archives[start:start + 40]
                ingest_command = [
                    sys.executable,
                    str(ROOT / "scripts" / "ingest_awc_taf_calibration.py"),
                    "--db", str(self.db_path),
                    "--commit",
                ]
                for archive_path in batch:
                    ingest_command.extend(["--archive", str(archive_path)])
                try:
                    ingest_run = subprocess.run(
                        ingest_command,
                        cwd=str(ROOT),
                        capture_output=True,
                        text=True,
                        timeout=HISTORICAL_TAF_CAPTURE_DEADLINE_SEC,
                    )
                except Exception as exc:
                    ingest_failures.append(
                        f"{type(exc).__name__}:{str(exc)[:120]}"
                    )
                    continue
                if ingest_run.returncode == 0:
                    ingested += len(batch)
                else:
                    ingest_failures.append(
                        (ingest_run.stderr or "").strip()[-120:]
                    )
            # A nonzero exit here means some station-anchor run failed, which
            # on this lane is ordinary: AWC drops connections and the next
            # day's pass resumes the same window.
            result = {
                "state": "CAPTURED" if completed.returncode == 0 else "PARTIAL",
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "station_count": len(stations),
                "decision_anchor_offset_hours": list(
                    HISTORICAL_TAF_DECISION_ANCHOR_HOURS
                ),
                "sample_count": summary.get("sample_count"),
                "completed_run_count": summary.get("completed_run_count"),
                "failed_run_count": summary.get("failed_run_count"),
                "ingested_archive_count": ingested,
                "ingest_failures": ingest_failures[:3],
                "stderr": (completed.stderr or "").strip()[-200:],
            }
            self._historical_taf_capture_last_result = result
            return result

        pool = getattr(self, "_historical_taf_capture_pool", None)
        if pool is None:
            self._historical_taf_capture_pool = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="historical-taf-capture",
            )
            pool = self._historical_taf_capture_pool
        self._historical_taf_capture_inflight = pool.submit(_run_capture)
        return {
            "state": "SUBMITTED",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "station_count": len(stations),
            "last_completed": dict(
                getattr(self, "_historical_taf_capture_last_result", {}) or {}
            ),
            **boundary,
        }

    def _taf_registry_artifacts(self) -> list[TafRevisionArtifact]:
        """Read the registry the resident P0-M stage selects from.

        Unreadable is treated as uncovered rather than raised: this is the
        training lane's coverage question, and a registry that cannot be parsed
        is exactly the case where a station needs an artifact built.  The
        stage's own read still fails closed on the same file.
        """

        try:
            raw = load_bounded_json(self.taf_artifact_path)
        except Exception:
            return []
        if not isinstance(raw, Mapping):
            return []
        try:
            if raw.get("schema") == TAF_ARTIFACT_REGISTRY_SCHEMA:
                return validate_taf_artifact_registry(raw)
            return [TafRevisionArtifact.from_mapping(raw)]
        except Exception:
            return []

    def _taf_revision_training_due(
        self, *, station_id: str, as_of_utc: str
    ) -> tuple[bool, dict[str, str], str]:
        """Whether this station's live P0 scope has no active TAF artifact.

        Coverage is judged against the scope the live P0 root actually stamps,
        because that is the equality the resident stage selects on: an artifact
        for any other season, lead or cycle is not a weaker match, it is no
        match at all.  Unlike the trajectory lane this cannot ask for coverage
        through the newest settled day -- the artifact is trained through the
        end of its fit window and the transport window that qualifies it sits
        after that, so demanding it would make every station due forever.  An
        artifact is current while its own validity window covers now.
        """

        read_conn = getattr(self, "conn", None) or self.commit_conn
        if read_conn is None:
            return False, {}, "target_read_connection_missing"
        row = read_conn.execute(
            "SELECT settlement_timezone, lineage_json "
            "FROM basket_probability_envelope "
            "WHERE station_id=? AND stage='P0' AND state='COMMITTED' "
            "ORDER BY decision_at_utc DESC LIMIT 1",
            (station_id,),
        ).fetchone()
        if row is None:
            return False, {}, "no_committed_p0_root"
        try:
            lineage = json.loads(str(row[1] or ""))
        except (TypeError, ValueError):
            return False, {}, "p0_root_lineage_unreadable"
        if not isinstance(lineage, Mapping):
            return False, {}, "p0_root_lineage_unreadable"
        scope = {
            "station_id": station_id,
            "settlement_timezone": str(row[0] or ""),
            "season_bin": str(lineage.get("season_bin") or ""),
            # The training grouping when the root carries one: that is what
            # the rolling series is keyed by, and what the resident stage
            # selects an artifact on.
            "lead_bucket": taf_scope_lead_bucket(lineage),
            "calendar_lead_bucket": str(lineage.get("lead_bucket") or ""),
            "run_cycle": str(lineage.get("run_cycle") or ""),
            "settlement_definition": str(
                lineage.get("settlement_definition") or ""
            ),
        }
        if not all(scope.values()):
            return False, {}, "p0_root_scope_incomplete"
        # The systems this root is actually mixing.  A TAF artifact carries a
        # live-compatibility fingerprint bound to the serving model set, so an
        # artifact fitted on any other pair is refused at serve time no matter
        # how well it scored -- which makes the serving pair the only one whose
        # accumulation shortfall describes when P0-M returns.
        scope["live_ensemble_system_ids"] = ",".join(sorted(
            str(value)
            for value in (
                json.loads(str(lineage.get("ensemble_system_ids_json") or "[]"))
                if str(lineage.get("ensemble_system_ids_json") or "").strip()
                else []
            )
        ))
        truth = read_conn.execute(
            "SELECT MAX(local_date) FROM calibration_station_daily_high_truth "
            "WHERE station_id=? AND qc_status='ACCEPTED'",
            (station_id,),
        ).fetchone()
        scope["latest_truth_day"] = str((truth or [""])[0] or "")
        # Every scope this station is serving right now, not just the newest
        # one.  The DB's latest root and the live inbox are two views of a
        # scope that moves: a station runs today's day and tomorrow's at
        # different lead buckets, and the ladder rotates the cycle under both,
        # so a station takes three or four distinct scopes in a day.  Chasing
        # only the instantaneous newest meant the lane fitted for whichever
        # scope happened to be live when it woke, and the transport gate --
        # which reads the inbox, not the table -- then asked for a live root
        # at that scope and found none.  ZGSZ fitted twice on 2026-08-16 and
        # both were rejected with ``taf_native_live_exact_scope_not_unique``
        # while a perfectly good root for its ready scope sat in the inbox.
        # Offer the gate's own view, so a ready series is pursued at a scope
        # the gate can still see.
        candidates: list[tuple[str, str, str]] = []
        for candidate in self._taf_live_inbox_scopes(station_id):
            if candidate not in candidates:
                candidates.append(candidate)
        live = (
            scope["season_bin"],
            scope["lead_bucket"],
            scope["run_cycle"],
        )
        if live not in candidates:
            candidates.append(live)
        covered: set[tuple[str, str, str]] = set()
        known: set[tuple[str, str, str]] = set()
        for artifact in self._taf_registry_artifacts():
            if (artifact.station_id, artifact.settlement_timezone) != (
                station_id,
                scope["settlement_timezone"],
            ):
                continue
            key = (
                artifact.season_bin,
                artifact.lead_bucket,
                artifact.run_cycle,
            )
            known.add(key)
            if artifact.valid_from_utc <= as_of_utc < artifact.valid_until_utc:
                covered.add(key)
        uncovered = [item for item in candidates if item not in covered]
        scope["candidate_scopes"] = [list(item) for item in candidates]
        scope["uncovered_scopes"] = [list(item) for item in uncovered]
        if not uncovered:
            return False, scope, "active_artifact_covers_live_scope"
        # Present the first uncovered scope as the headline so a receipt read
        # at a glance still names one thing; ``_train`` picks from the whole
        # uncovered set by which of them has evidence.
        head = uncovered[0]
        scope["season_bin"], scope["lead_bucket"], scope["run_cycle"] = head
        # A scope that once had an artifact and outlived it is a refit; one
        # that never had one is a first fit.  Both are due, and the wait they
        # describe is different enough to keep saying which.
        return (
            True,
            scope,
            "artifact_validity_window_lapsed"
            if head in known
            else "no_artifact_for_live_p0_scope",
        )

    def _taf_live_inbox_scopes(
        self, station_id: str
    ) -> list[tuple[str, str, str]]:
        """(season, lead, cycle) for every P0 root the live inbox serves.

        This is the same file the transport gate resolves against, so a scope
        named here is one the gate can bind a fingerprint to.  Unreadable or
        absent yields nothing rather than raising: the caller falls back to
        the table's newest root, which is the pre-existing behaviour.
        """

        try:
            inbox = load_bounded_json(self.p0_inbox_path)
            rows = inbox.get("packets") if isinstance(inbox, Mapping) else None
        except Exception:
            return []
        if not isinstance(rows, list):
            return []
        scopes: list[tuple[str, str, str]] = []
        for raw in rows:
            packet = raw.get("packet") if isinstance(raw, Mapping) else None
            if not isinstance(packet, Mapping):
                continue
            if str(packet.get("station_id") or "") != station_id:
                continue
            if str(packet.get("stage") or "") != "P0":
                continue
            envelope = packet.get("native_probability_envelope")
            lineage = (
                envelope.get("lineage")
                if isinstance(envelope, Mapping)
                else None
            )
            if not isinstance(lineage, Mapping):
                continue
            item = (
                str(lineage.get("season_bin") or ""),
                taf_scope_lead_bucket(lineage),
                str(lineage.get("run_cycle") or ""),
            )
            if all(item) and item not in scopes:
                scopes.append(item)
        return scopes

    def _taf_revision_cursor_path(self) -> Path:
        return (
            self.nwp_runtime_root
            / "taf_revision_training"
            / "rotation_cursor.json"
        )

    def _taf_revision_cursor(self, *, as_of_utc: str) -> int:
        """Where the station rotation left off, across restarts.

        The rotation used to live only on the instance, so every restart sent
        it back to the first station -- and a pass takes about 45 minutes,
        longer than this service usually goes between deploys.  The resident
        is restarted whenever its code changes, which on a working day is
        often enough that the tail of the alphabet was never reached: on
        2026-08-16 both stations with evidence ready to fit sat in that tail
        and neither had been visited all day.

        The restarts are not a fault to be fixed elsewhere -- there is no
        timer and no crash, only SIGTERM from a deploy -- so the rotation has
        to survive them rather than wait for them to stop.
        """

        cached = getattr(self, "_taf_revision_station_cursor", None)
        if cached is not None:
            return int(cached)
        resume = ""
        try:
            state = load_bounded_json(self._taf_revision_cursor_path())
            if isinstance(state, Mapping):
                resume = str(state.get("next_station_id") or "")
        except Exception:
            resume = ""
        # By name, not by index: the allowlist is not a fixed-length thing,
        # and resuming a stale index would silently skip or repeat a station
        # whenever the roster changed.
        cursor = (
            self.station_allowlist.index(resume)
            if resume in self.station_allowlist
            else 0
        )
        self._taf_revision_station_cursor = cursor
        return cursor

    def _advance_taf_revision_cursor(self, cursor: int) -> None:
        self._taf_revision_station_cursor = cursor
        if not self.station_allowlist:
            return
        try:
            path = self._taf_revision_cursor_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            write_bounded_json_if_changed(
                path,
                {
                    "schema": (
                        "bayes_basket_target_taf_revision_rotation_v1"
                    ),
                    "next_station_id": self.station_allowlist[
                        cursor % len(self.station_allowlist)
                    ],
                },
            )
        except Exception:
            # A rotation that cannot be written is a slower rotation, never a
            # stopped one.  The in-memory cursor still advances this run.
            pass

    @staticmethod
    def _taf_rolling_accumulation(
        rolling_path: Path,
        *,
        season_bin: str,
        lead_bucket: str,
        live_system_ids: str = "",
    ) -> dict[str, Any]:
        """The serving system pair's shortfall at the live scope.

        Qualifying first is not the same as being able to serve: the artifact
        carries a live-compatibility fingerprint bound to the P0 root's model
        set, so a pair the root does not mix is refused at serve time however
        far ahead its evidence runs.  Report the serving pair when the builder
        offers it, and say so when falling back to the best-covered one, so a
        shortfall in the receipt is a wait for P0-M rather than for an artifact
        that could never be used.
        """

        try:
            rolling = load_bounded_json(rolling_path)
        except Exception:
            return {}
        best: Mapping[str, Any] | None = None
        serving: Mapping[str, Any] | None = None
        offered: set[str] = set()
        serving_pair_leads: set[str] = set()
        for item in (rolling or {}).get("accumulating") or []:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("season_bin") or "") != season_bin:
                continue
            offered.add(str(item.get("lead_bucket") or ""))
            item_pair = ",".join(sorted(
                str(value)
                for value in (item.get("selected_system_ids") or [])
            ))
            if live_system_ids and item_pair == live_system_ids:
                serving_pair_leads.add(str(item.get("lead_bucket") or ""))
            if str(item.get("lead_bucket") or "") != lead_bucket:
                continue
            plan = item.get("plan")
            if not isinstance(plan, Mapping):
                continue
            if live_system_ids and item_pair == live_system_ids:
                serving = item
            if best is None or int(plan.get("common_case_count") or 0) > int(
                (best.get("plan") or {}).get("common_case_count") or 0
            ):
                best = item
        best = serving or best
        if best is None:
            # Training groups runs by lead in hours while P0 lineage still
            # names calendar-day leads, so a station can have plenty of
            # evidence under a bucket no live root will ever ask for.  Say
            # which buckets exist rather than reporting an absence.
            return {
                "reason": (
                    "live_lead_bucket_absent_from_training_vocabulary"
                    if offered
                    else "no_system_pair_at_live_scope"
                ),
                "live_lead_bucket": lead_bucket,
                "training_lead_buckets": sorted(offered),
            }
        plan = best["plan"]
        return {
            "is_live_serving_pair": bool(serving is not None),
            "live_ensemble_system_ids": live_system_ids,
            # A shortfall counted against a pair the root does not mix is not
            # the wait for P0-M.  When the serving pair has series under other
            # lead buckets only, the block is the lead vocabulary rather than
            # missing days, and no number of settled days will clear it --
            # another pair holding an entry at the live bucket would otherwise
            # hide that behind a plausible-looking countdown.
            **(
                {
                    "serving_pair_reason": (
                        "serving_pair_only_under_other_lead_buckets"
                    ),
                    "serving_pair_lead_buckets": sorted(serving_pair_leads),
                }
                if serving is None and serving_pair_leads
                else {}
            ),
            "selected_system_ids": list(best.get("selected_system_ids") or []),
            "common_case_count": int(plan.get("common_case_count") or 0),
            "required_common_case_count": int(
                plan.get("required_common_case_count") or 0
            ),
            "missing_common_case_count": int(
                plan.get("missing_common_case_count") or 0
            ),
        }

    def _run_taf_revision_training(
        self, *, as_of_utc: str
    ) -> dict[str, Any]:
        """Run the rolling-OOS -> fit -> gate -> bind -> registry chain once.

        One due station per cycle, rotated, on its own single-worker pool --
        the same shape as the trajectory pass.  Every step is an existing
        offline script run as a subprocess; the only durable products are the
        bound artifact files and the registry the builder assembles from them.
        This lane writes no SSOT row and reaches no order gateway.
        """

        boundary = {
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
        if (
            (getattr(self, "conn", None) is None and self.commit_conn is None)
            or not self.station_allowlist
            or not self.p0_inbox_path.is_file()
        ):
            return {"state": "NOT_CONFIGURED", **boundary}
        inflight = getattr(self, "_taf_revision_inflight", None)
        if inflight is not None and not inflight.done():
            return {"state": "IN_FLIGHT", **boundary}
        cursor = self._taf_revision_cursor(as_of_utc=as_of_utc)
        throttle: dict[str, float] = getattr(
            self, "_taf_revision_attempts", None
        ) or {}
        self._taf_revision_attempts = throttle
        now_mono = time.monotonic()
        chosen: tuple[str, dict[str, str]] | None = None
        for offset in range(len(self.station_allowlist)):
            station = self.station_allowlist[
                (cursor + offset) % len(self.station_allowlist)
            ]
            last = throttle.get(station)
            if (
                last is not None
                and now_mono - last < TAF_REVISION_TRAINING_RETRY_SEC
            ):
                continue
            try:
                due, scope, _reason = self._taf_revision_training_due(
                    station_id=station,
                    as_of_utc=as_of_utc,
                )
            except Exception:
                continue
            if due:
                chosen = (station, scope)
                self._advance_taf_revision_cursor(
                    (cursor + offset + 1) % len(self.station_allowlist)
                )
                break
        if chosen is None:
            self._advance_taf_revision_cursor(
                (cursor + 1) % len(self.station_allowlist)
            )
            return {"state": "NOT_DUE", **boundary}
        station, scope = chosen
        throttle[station] = now_mono
        target_db = str(self.db_path)
        live_inbox = str(self.p0_inbox_path)
        work_root = self.nwp_runtime_root / "taf_revision_training"
        work = work_root / station
        work.mkdir(parents=True, exist_ok=True)
        bound_root = Path(self.taf_bound_artifact_root)
        bound_root.mkdir(parents=True, exist_ok=True)
        registry_path = self.taf_artifact_path

        def _run(script: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, str(ROOT / "scripts" / script), *arguments],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=TAF_REVISION_TRAINING_DEADLINE_SEC,
            )

        def _summary(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
            try:
                return json.loads((completed.stdout or "").strip().splitlines()[-1])
            except Exception:
                return {}

        def _train() -> dict[str, Any]:
            steps: list[dict[str, Any]] = []
            result: dict[str, Any] = {
                "schema": "bayes_basket_target_taf_revision_training_receipt_v1",
                "station_id": station,
                "as_of_utc": as_of_utc,
                "live_p0_scope": dict(scope),
                "state": "BLOCKED_EVIDENCE",
                "steps": steps,
                "registry_path": str(registry_path),
                "writes_target_ssot": False,
                **boundary,
            }

            def _finish(state: str) -> dict[str, Any]:
                result["state"] = state
                self._taf_revision_last_result = result
                try:
                    write_bounded_json_if_changed(work / "receipt.json", result)
                except Exception as exc:
                    result["receipt_write_error"] = f"{type(exc).__name__}:{exc}"
                return result

            rolling = _run(
                "build_native_ensemble_rolling_taf_oos.py",
                [
                    "--db", target_db,
                    "--station-id", station,
                    "--output-dir", str(work),
                ],
            )
            # Exit 2 is this builder's "not enough disjoint blocks yet", which
            # is the expected state for every station whose settled days have
            # not caught up.  Only a missing manifest is a real failure.
            steps.append({
                "step": "rolling_native_taf_oos",
                "returncode": rolling.returncode,
                "summary": _summary(rolling),
                "stderr": (rolling.stderr or "").strip()[-200:],
            })
            try:
                manifest = load_bounded_json(work / "manifest.json")
            except Exception as exc:
                steps[-1]["state"] = "ROLLING_FAILED"
                steps[-1]["reason"] = f"{type(exc).__name__}:{exc}"
                return _finish("BLOCKED_EVIDENCE")
            reports = [
                item
                for item in (manifest.get("reports") or [])
                if isinstance(item, Mapping)
                and str(item.get("station_id") or "") == station
            ]
            splits = [
                item
                for report in reports
                for item in (report.get("strict_splits") or [])
                if isinstance(item, Mapping)
            ]
            # Any scope this station is serving and has no artifact for, in
            # the order the due check offered them.  Restricting this to the
            # single newest scope threw away ready evidence: a series reaches
            # READY at the scope it accumulated under, and by the time the
            # lane wakes the ladder has usually moved the newest root
            # somewhere else.
            wanted = [
                tuple(str(part) for part in item)
                for item in (scope.get("uncovered_scopes") or [])
            ] or [(
                scope["season_bin"],
                scope["lead_bucket"],
                scope["run_cycle"],
            )]
            exact = [
                item
                for item in splits
                if (
                    str(item.get("season_bin") or ""),
                    str(item.get("lead_bucket") or ""),
                    str(item.get("run_cycle") or ""),
                ) in wanted
            ]
            ready = [item for item in exact if item.get("state") == "READY"]
            if ready:
                # The rest of the pipeline -- fit arguments, gate arguments,
                # bound filename -- is scoped to one artifact, so commit to
                # the ready one before any of it runs.
                selected_scope = ready[0]
                scope["season_bin"] = str(selected_scope.get("season_bin") or "")
                scope["lead_bucket"] = str(selected_scope.get("lead_bucket") or "")
                scope["run_cycle"] = str(selected_scope.get("run_cycle") or "")
            steps[-1]["state"] = "READY" if ready else "ACCUMULATING"
            steps[-1]["series_scopes"] = [
                {
                    "season_bin": str(item.get("season_bin") or ""),
                    "lead_bucket": str(item.get("lead_bucket") or ""),
                    "run_cycle": str(item.get("run_cycle") or ""),
                    "state": str(item.get("state") or ""),
                    "series_sample_count": int(item.get("series_sample_count") or 0),
                }
                for item in splits
            ]
            if not ready:
                # Name the two ways this ends short: no series at the live
                # scope at all, or a series that has not reached one fit plus
                # one strictly later transport window.
                steps[-1]["reason"] = (
                    "rolling_series_missing_for_live_p0_scope"
                    if not exact
                    else "rolling_series_below_fit_plus_transport_minimum"
                )
                if exact:
                    steps[-1]["missing_sample_count"] = int(
                        exact[0].get("missing_sample_count") or 0
                    )
                else:
                    # Without a single READY block there is no series to
                    # describe, and the receipt would say only that nothing
                    # matched.  Carry the builder's own arithmetic instead, so
                    # the wait is a number of settled days rather than a
                    # mystery.
                    steps[-1]["accumulation"] = self._taf_rolling_accumulation(
                        work / f"{station}_rolling_native_oos.json",
                        season_bin=scope["season_bin"],
                        lead_bucket=scope["lead_bucket"],
                        live_system_ids=scope.get(
                            "live_ensemble_system_ids", ""
                        ),
                    )
                return _finish("BLOCKED_EVIDENCE")
            # Two ready series at *different* scopes is now the ordinary case
            # and simply means the next run has something to do.  Two at the
            # same scope is still a duplicate the builder should never emit.
            at_selected = [
                item
                for item in ready
                if (
                    str(item.get("season_bin") or ""),
                    str(item.get("lead_bucket") or ""),
                    str(item.get("run_cycle") or ""),
                ) == (
                    scope["season_bin"],
                    scope["lead_bucket"],
                    scope["run_cycle"],
                )
            ]
            if len(at_selected) != 1:
                steps[-1]["state"] = "AMBIGUOUS"
                steps[-1]["reason"] = "rolling_series_exact_scope_ambiguous"
                return _finish("BLOCKED_EVIDENCE")
            steps[-1]["selected_scope"] = {
                "season_bin": scope["season_bin"],
                "lead_bucket": scope["lead_bucket"],
                "run_cycle": scope["run_cycle"],
            }
            steps[-1]["ready_scope_count"] = len(ready)
            selected = at_selected[0]
            fit_path = Path(str(selected["fit_output"]))
            transport_path = Path(str(selected["transport_output"]))
            try:
                transport_join = load_bounded_json(transport_path)
                fit_join = load_bounded_json(fit_path)
                valid_from = max(
                    str(row["settlement_window"]["end_utc"])
                    for row in transport_join["samples"]
                )
                fit_sha = str(fit_join["content_sha256"])
            except Exception as exc:
                steps.append({
                    "step": "strict_split_read",
                    "state": "FAILED",
                    "reason": f"{type(exc).__name__}:{exc}",
                })
                return _finish("BLOCKED_EVIDENCE")
            # Anchored to the evidence rather than to the clock, so rerunning
            # an unchanged station rebuilds an identical artifact and the
            # registry write is a no-op instead of six-hourly churn.
            valid_until = (
                datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
                + timedelta(days=TAF_REVISION_ARTIFACT_VALID_DAYS)
            ).astimezone(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            artifact_id = "-".join([
                "taf",
                station.lower(),
                scope["season_bin"].lower(),
                scope["lead_bucket"].lower(),
                scope["run_cycle"].lower(),
                fit_sha[:12],
            ])
            fitted_path = work / f"{artifact_id}_artifact.json"
            fit = _run(
                "fit_bayes_basket_target_taf_revision.py",
                [
                    "--dataset", str(fit_path),
                    "--artifact-output", str(fitted_path),
                    "--scorecard-output", str(work / f"{artifact_id}_scorecard.json"),
                    "--artifact-id", artifact_id,
                    "--activation-id", f"{artifact_id}-activation",
                    "--station-id", station,
                    "--season-bin", scope["season_bin"],
                    "--lead-bucket", scope["lead_bucket"],
                    "--run-cycle", scope["run_cycle"],
                    "--settlement-definition", scope["settlement_definition"],
                    "--valid-from-utc", valid_from,
                    "--valid-until-utc", valid_until,
                ],
            )
            steps.append({
                "step": "fit_taf_revision",
                "state": "FITTED" if fit.returncode == 0 else "GATE_REJECTED",
                "returncode": fit.returncode,
                "artifact_id": artifact_id,
                "summary": _summary(fit),
                "stderr": (fit.stderr or "").strip()[-200:],
            })
            if fit.returncode != 0:
                return _finish("BLOCKED_EVIDENCE")
            report_path = work / f"{artifact_id}_transport_oos.json"
            gate = _run(
                "build_taf_native_ensemble_oos.py",
                [
                    "--exact-join", str(transport_path),
                    "--live-p0-inbox", live_inbox,
                    "--taf-artifact", str(fitted_path),
                    "--station-id", station,
                    "--season-bin", scope["season_bin"],
                    "--lead-bucket", scope["lead_bucket"],
                    "--run-cycle", scope["run_cycle"],
                    "--output", str(report_path),
                ],
            )
            gate_summary = _summary(gate)
            steps.append({
                "step": "native_ensemble_transport_oos",
                "state": str(gate_summary.get("state") or "FAILED"),
                "returncode": gate.returncode,
                "validated": bool(gate_summary.get("validated")),
                "sample_n": gate_summary.get("sample_n"),
                "stderr": (gate.stderr or "").strip()[-200:],
            })
            if gate.returncode != 0 or not gate_summary.get("validated"):
                return _finish("BLOCKED_EVIDENCE")
            # One file per exact scope: a refit replaces its own scope in
            # place, which is what keeps the registry free of the duplicate
            # scopes its builder rejects.
            bound_path = bound_root / (
                "_".join([
                    station,
                    scope["season_bin"],
                    scope["lead_bucket"],
                    scope["run_cycle"],
                ]).replace("/", "-")
                + ".json"
            )
            bind = _run(
                "bind_bayes_basket_target_taf_live_compatibility.py",
                [
                    "--taf-artifact", str(fitted_path),
                    "--transport-oos-report", str(report_path),
                    "--output", str(bound_path),
                ],
            )
            steps.append({
                "step": "bind_live_compatibility",
                "state": "BOUND" if bind.returncode == 0 else "FAILED",
                "returncode": bind.returncode,
                "summary": _summary(bind),
                "stderr": (bind.stderr or "").strip()[-200:],
            })
            if bind.returncode != 0:
                return _finish("BLOCKED_EVIDENCE")
            # Only artifacts whose own validity still covers now.  The registry
            # is capped at 45 members, so carrying every scope a station has
            # ever run under would eventually make each build fail and freeze
            # the file -- and an expired member is one the P0-M stage would
            # reject anyway.  Lapsed files stay on disk as the audit trail.
            members: list[Path] = []
            lapsed = 0
            for path in sorted(bound_root.glob("*.json")):
                if not path.is_file():
                    continue
                try:
                    member = TafRevisionArtifact.from_mapping(
                        load_bounded_json(path)
                    )
                except Exception:
                    lapsed += 1
                    continue
                if member.valid_from_utc <= as_of_utc < member.valid_until_utc:
                    members.append(path)
                else:
                    lapsed += 1
            arguments: list[str] = []
            for path in members:
                arguments.extend(["--artifact", str(path)])
            arguments.extend([
                "--output", str(registry_path),
                "--receipt-output", str(work / "registry_receipt.json"),
            ])
            registry = _run(
                "build_bayes_basket_target_taf_artifact_registry.py",
                arguments,
            )
            registry_summary = _summary(registry)
            steps.append({
                "step": "build_artifact_registry",
                "state": str(registry_summary.get("state") or "FAILED"),
                "returncode": registry.returncode,
                "artifact_count": registry_summary.get("artifact_count"),
                "member_count": len(members),
                "lapsed_member_count": lapsed,
                "stderr": (registry.stderr or "").strip()[-200:],
            })
            if registry.returncode != 0:
                return _finish("BLOCKED_EVIDENCE")
            result["artifact_id"] = artifact_id
            result["bound_artifact_path"] = str(bound_path)
            return _finish("PROMOTED")

        def _train_guarded() -> dict[str, Any]:
            # Nothing retrieves this future's result, so an exception raised
            # inside the chain would be discarded silently and the lane would
            # look merely quiet.  The receipt on disk is the only place anyone
            # sees this pass at all -- launchd captures nothing -- so a crash
            # has to land there too.
            try:
                return _train()
            except Exception as exc:
                failure = {
                    "schema": "bayes_basket_target_taf_revision_training_receipt_v1",
                    "station_id": station,
                    "as_of_utc": as_of_utc,
                    "live_p0_scope": dict(scope),
                    "state": "FAILED",
                    "reason": f"{type(exc).__name__}:{exc}",
                    "steps": [],
                    "writes_target_ssot": False,
                    **boundary,
                }
                self._taf_revision_last_result = failure
                try:
                    write_bounded_json_if_changed(work / "receipt.json", failure)
                except Exception:
                    pass
                return failure

        pool = getattr(self, "_taf_revision_pool", None)
        if pool is None:
            self._taf_revision_pool = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="taf-revision-training",
            )
            pool = self._taf_revision_pool
        self._taf_revision_inflight = pool.submit(_train_guarded)
        return {
            "state": "SUBMITTED",
            "station_id": station,
            "live_p0_scope": dict(scope),
            "last_completed": dict(
                getattr(self, "_taf_revision_last_result", {}) or {}
            ),
            **boundary,
        }

    def _registry_export_is_stale(
        self, path: Path, *, as_of_utc: str
    ) -> bool:
        """Whether the published registry snapshot is old enough to redo.

        Markets for a future event date are listed progressively, so a snapshot
        taken early carries a station count that is merely how far Polymarket
        had got at that moment.  Holding it indefinitely makes that partial
        count permanent for the date.
        """

        try:
            snapshot = load_market_registry_snapshot(path)
        except Exception:
            return True
        taken = str(snapshot.get("as_of_utc") or "").strip()
        if not taken:
            return True
        try:
            def _at(value: str) -> datetime:
                return datetime.fromisoformat(
                    str(value).replace("Z", "+00:00")
                ).astimezone(timezone.utc)

            age = (_at(as_of_utc) - _at(taken)).total_seconds()
        except Exception:
            return True
        return age >= REGISTRY_EXPORT_MAX_AGE_SEC or age < 0

    def _open_meteo_d2_already_captured(self, *, target_date: str) -> bool:
        """Whether all three republisher families already hold this target day.

        Keyed on the extraction version so a change in how members are read
        does not let a superseded capture satisfy the day.
        """

        if self.conn is None:
            return False
        try:
            row = self.conn.execute(
                "SELECT COUNT(DISTINCT ensemble_system_id) "
                "FROM calibration_republisher_ensemble_historical_member_run "
                "WHERE target_local_date=? AND archive_lead_bucket='d2' "
                "  AND extraction_version=? "
                f"  AND ensemble_system_id IN ({
                    ','.join('?' * len(OPEN_METEO_D2_SYSTEM_IDS))
                })",
                (
                    target_date,
                    OPEN_METEO_D2_EXTRACTION_VERSION,
                    *OPEN_METEO_D2_SYSTEM_IDS,
                ),
            ).fetchone()
        except Exception:
            return False
        return int((row or [0])[0]) >= len(OPEN_METEO_D2_SYSTEM_IDS)

    def _capture_open_meteo_d2_daily(self, *, as_of_utc: str) -> dict[str, Any]:
        """Take today's republisher d2 members for all stations, once."""

        target_date = (
            _utc_date(as_of_utc)
            + timedelta(days=OPEN_METEO_D2_CAPTURE_LEAD_DAYS)
        ).isoformat()
        if self._open_meteo_d2_already_captured(target_date=target_date):
            return {
                "state": "ALREADY_CAPTURED",
                "target_local_date": target_date,
                "orders_submitted": 0,
                "calls_order_gateway": False,
            }
        inflight = getattr(self, "_open_meteo_d2_capture_inflight", None)
        if inflight is not None and not inflight.done():
            return {
                "state": "IN_FLIGHT",
                "target_local_date": target_date,
                "orders_submitted": 0,
                "calls_order_gateway": False,
            }
        now_mono = time.monotonic()
        last_attempt = getattr(self, "_open_meteo_d2_capture_last_mono", None)
        if (
            last_attempt is not None
            and now_mono - last_attempt < OPEN_METEO_D2_CAPTURE_RETRY_SEC
        ):
            return {
                "state": "THROTTLED",
                "target_local_date": target_date,
                "orders_submitted": 0,
                "calls_order_gateway": False,
            }
        self._open_meteo_d2_capture_last_mono = now_mono

        capture_root = self.nwp_runtime_root / "open_meteo_d2_capture"
        capture_root.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(ROOT / "scripts" / "capture_open_meteo_d2_ensemble_daily.py"),
            "--output", str(capture_root / f"om_d2_{target_date}.json"),
            "--commit",
        ]

        def _run() -> dict[str, Any]:
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=OPEN_METEO_D2_CAPTURE_DEADLINE_SEC,
                )
            except subprocess.TimeoutExpired:
                return {"state": "DEADLINE_EXCEEDED"}
            except Exception as exc:
                return {
                    "state": "FAILED",
                    "reason": f"{type(exc).__name__}:{str(exc)[:200]}",
                }
            if completed.returncode != 0:
                return {
                    "state": "FAILED",
                    "reason": (
                        f"open_meteo_d2_exit_{completed.returncode}:"
                        f"{(completed.stderr or '').strip()[:200]}"
                    ),
                }
            return {"state": "CAPTURED", "target_local_date": target_date}

        pool = getattr(self, "_official_native_capture_pool", None)
        if pool is None:
            self._official_native_capture_pool = ThreadPoolExecutor(
                max_workers=OFFICIAL_NATIVE_CAPTURE_MAX_CONCURRENCY,
                thread_name_prefix="official-native-capture",
            )
            pool = self._official_native_capture_pool
        self._open_meteo_d2_capture_inflight = pool.submit(_run)
        return {
            "state": "SUBMITTED",
            "target_local_date": target_date,
            "orders_submitted": 0,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        }

    def _drain_fresh_official_native_arrivals(
        self,
        read_conn: sqlite3.Connection,
        *,
        as_of_utc: str,
    ) -> dict[str, Any]:
        """Compile every fresh, unconsumed official packet before the cursor.

        The packet TTL and the rotation period are independent quantities, and
        nothing kept them ordered: a packet that lands just after the cursor
        passes its scope waits a full sweep, and when the sweep is longer than
        the TTL the packet dies in the inbox without ever becoming a P0.  This
        drain restores the invariant the architecture already claims -- P0 is
        driven by each model update -- by consuming arrivals on the cycle after
        they land, regardless of where the cursor happens to be.

        Consumption is deduplicated on the packet's ``snapshot_content_sha256``:
        a compile is content-addressed and its persistence idempotent, so the
        in-memory ledger only avoids repeat work, never correctness.  Failures
        are recorded and the sha is *not* marked consumed, so a transient
        rejection is retried on the next cycle while the packet is still
        fresh.
        """

        consumed: dict[tuple[str, str], str] = getattr(
            self, "_official_native_arrival_consumed", None
        ) or {}
        self._official_native_arrival_consumed = consumed
        official_inbox_path = (
            self.nwp_runtime_root
            / "official_native_ensemble_forward_inbox.json"
        )
        receipt: dict[str, Any] = {
            "schema": "official_native_arrival_drain_v1",
            "compiled_count": 0,
            "rejected": [],
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
        if not official_inbox_path.is_file():
            return receipt
        try:
            packets = _load_native_forward_inbox(
                official_inbox_path,
                error_code="official_native_forward_inbox_invalid",
            )
        except Exception as exc:
            receipt["rejected"].append(
                f"inbox:{type(exc).__name__}:{str(exc)[:120]}"
            )
            return receipt
        compiled_packets: list[dict[str, Any]] = []
        for candidate in packets:
            if (
                str(candidate.get("schema") or "")
                != OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA
            ):
                continue
            station = str(
                candidate.get("station_id") or ""
            ).strip().upper()
            event_date = str(candidate.get("event_date") or "")
            snapshot_sha = str(
                candidate.get("snapshot_content_sha256") or ""
            )
            if not station or not event_date or not snapshot_sha:
                continue
            if consumed.get((station, event_date)) == snapshot_sha:
                continue
            if _native_forward_packet_is_stale(
                candidate, as_of_utc=as_of_utc
            ):
                continue
            # Compiling from a packet whose TAF issuance has been superseded
            # would republish the stale forecaster reading; skipping sends
            # the scope through the capture lane, which reuses the retained
            # model archives and refreshes only the TAF entry.
            if _packet_taf_superseded(
                read_conn,
                candidate,
                station_id=station,
                event_date=event_date,
            ):
                continue
            try:
                compiled = _compile_native_ensemble_p0_runtime(
                    read_conn,
                    forward_packet=candidate,
                    decision_at_utc=as_of_utc,
                    native_oos_snapshot=self.native_oos_snapshot,
                )
            except Exception as exc:
                receipt["rejected"].append(
                    f"{station}|{event_date}:"
                    f"{type(exc).__name__}:{str(exc)[:120]}"
                )
                continue
            if not (
                isinstance(compiled, Mapping)
                and compiled.get("schema")
                == "calibrated_native_cross_ensemble_p0_packet_v1"
            ):
                receipt["rejected"].append(
                    f"{station}|{event_date}:compile_rejected"
                )
                continue
            compiled = dict(compiled)
            if self.commit_conn is not None:
                compiled[
                    "forward_prediction_persistence"
                ] = persist_native_ensemble_forward_prediction(
                    self.commit_conn,
                    compiled,
                )
            compiled_packets.append(
                (station, event_date, snapshot_sha, compiled)
            )
            receipt["compiled_count"] += 1
        if compiled_packets and self.commit_conn is not None:
            merge = _upsert_native_p0_inbox(
                self.p0_inbox_path,
                compiled_packets=[item[3] for item in compiled_packets],
                generated_at_utc=as_of_utc,
            )
            receipt["p0_inbox_merge"] = merge
            # Consumption follows delivery, not compilation.  A packet the
            # inbox declines -- a P0-M confirm the P0 inbox has no seat for,
            # say -- must stay unconsumed so the next cycle retries or routes
            # it, instead of evaporating behind a dedupe mark.
            if int(merge.get("accepted_count") or 0) == len(
                compiled_packets
            ):
                for station, event_date, snapshot_sha, _packet in (
                    compiled_packets
                ):
                    consumed[(station, event_date)] = snapshot_sha
        elif compiled_packets:
            for station, event_date, snapshot_sha, _packet in (
                compiled_packets
            ):
                consumed[(station, event_date)] = snapshot_sha
        return receipt

    def _persist_nwp_rotation(
        self,
        *,
        station_ids: Sequence[str],
        scopes: Sequence[Mapping[str, Any]],
        selected_scopes: Sequence[Mapping[str, Any]],
        outcomes: Sequence[Mapping[str, Any]],
        as_of_utc: str,
    ) -> dict[str, Any]:
        """Write the rotation cursor and scope catalogue, whatever the outcome.

        Every exit that advanced the cursor has to persist it.  Writing on only
        one of them froze the file the moment the lane stopped taking that
        branch: once packets existed the cycle returned ``official_native_reused``
        instead, so the last write stayed at 2026-08-08T03:04Z while capture
        went on for another fourteen hours.

        Two things go wrong quietly when that happens.  The cursor resumes from
        a stale catalogue after a restart, pointing into a scope list that no
        longer describes the open days; and the in-flight status receipt
        republishes the same stale catalogue, so ``catalog_scope_count`` -- the
        one number worth trusting when station coverage looks short -- reports
        a two-day-old sweep as if it were current.
        """

        rotation_state = build_rotation_state(
            station_ids=tuple(station_ids),
            scopes=list(scopes),
            selected_scopes=list(selected_scopes),
            cursor_by_priority={
                0: self._native_ensemble_scope_cursor,
            },
            outcomes=list(outcomes),
            previous=getattr(self, "_nwp_rotation_state", None),
            updated_at_utc=as_of_utc,
        )
        rotation_state_path = getattr(
            self,
            "nwp_rotation_state_path",
            self.nwp_runtime_root / "rotation_state.json",
        )
        try:
            wrote = persist_rotation_state(rotation_state_path, rotation_state)
        except Exception as exc:
            # Rotation state is scheduler telemetry and a restart hint, not an
            # authorization boundary.  A failed write must not turn a completed
            # capture into a fail-closed cycle.
            self._nwp_rotation_state = rotation_state
            return {
                "rotation_state_path": str(rotation_state_path),
                "rotation_state_wrote": False,
                "rotation_state_error": f"{type(exc).__name__}:{exc}",
                "rotation_generation": rotation_state["generation"],
                "next_scope_cursor": self._native_ensemble_scope_cursor,
            }
        self._nwp_rotation_state = rotation_state
        return {
            "rotation_state_path": str(rotation_state_path),
            "rotation_state_wrote": bool(wrote),
            "rotation_generation": rotation_state["generation"],
            "next_scope_cursor": self._native_ensemble_scope_cursor,
        }

    def refresh_resident_nwp(self, *, as_of_utc: str) -> dict[str, Any]:
        """Capture official models and refresh the bounded P0 inbox.

        The method belongs to this existing worker so there is still one
        resident owner and no fourth service.  Registry export is read-only
        against the canonical database; only the isolated target registry and
        P0 inbox may change.
        """

        # ICON no longer has a lane of its own.  It was given one because its
        # objects are whole global fields and the native capture asked per
        # station, which would have pulled the same run once for each of them;
        # that capture now reads one run for every station the day lists, so
        # the separate pass was downloading a second copy of what the shared
        # one already had.  Its irreplaceability -- DWD keeps one run, and a
        # skipped day is gone -- now rests on the same capture the other three
        # families depend on.
        open_meteo_d2 = self._capture_open_meteo_d2_daily(as_of_utc=as_of_utc)
        # Same fire-and-forget shape: the trajectory chain runs for at most one
        # due station per cycle and persists through its own gated commit.
        metar_trajectory_training = self._run_metar_trajectory_training(
            as_of_utc=as_of_utc
        )
        del metar_trajectory_training
        # And the same for the TAF revision chain, which is what the resident
        # P0-M stage has been waiting on: it consumes only the registry file
        # this lane's builder writes.
        # Kept rather than discarded.  P0-M has published nothing since
        # 2026-07-30 and this lane is the only thing that can end that, but
        # its verdict was dropped on the floor every cycle -- so "why is
        # there no P0-M" could only be answered by reading receipts off
        # disk.  It answers that question every cycle; let it be read.
        self._taf_revision_training_state = self._run_taf_revision_training(
            as_of_utc=as_of_utc
        )
        # And the evidence that lane trains on.  AWC serves 30 days, so a
        # closed day not captured while it is still inside that window can
        # never be captured at all -- this belongs with ICON among the passes
        # whose skipped day is lost rather than merely late.
        historical_taf_capture = self._capture_historical_taf_daily(
            as_of_utc=as_of_utc
        )
        del historical_taf_capture
        # Corrected reports halt settled truth until reviewed, and the review
        # policy only ever ran by hand -- so the halts piled up again and six
        # stations quietly stopped settling.  Scheduling it is the difference
        # between a policy that exists and one that runs.
        correction_auto_clear = self._run_correction_auto_clear(
            as_of_utc=as_of_utc
        )
        del correction_auto_clear

        read_conn = getattr(self, "commit_conn", None) or getattr(
            self, "conn", None
        )
        rotation_state_load_error = getattr(
            self,
            "_nwp_rotation_state_load_error",
            None,
        )
        if rotation_state_load_error:
            return {
                "schema": "bayes_basket_target_resident_nwp_cycle_v1",
                "state": "blocked_fail_closed",
                "reason": (
                    "resident_nwp_rotation_state_invalid:"
                    + rotation_state_load_error
                ),
                "station_scope_mode": "canonical_allowlist",
                "configured_station_ids": list(
                    getattr(self, "station_allowlist", ())
                ),
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        if read_conn is None:
            return {
                "state": "not_attempted",
                "reason": "target_read_only_connection_missing",
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
            }
        if self.canonical_source_db_path is None:
            return {
                "state": "not_configured",
                "reason": "canonical_registry_source_missing",
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
            }
        assert_process_memory_budget()
        registry_path = self.nwp_runtime_root / "market_registry_forward.json"
        station_allowlist = tuple(
            getattr(self, "station_allowlist", ())
        )
        event_date_allowlist = tuple(
            getattr(self, "event_date_allowlist", ())
        )
        # The resident registry worker owns ongoing publication immediately
        # after each bounded Gamma metadata refresh.  NWP may bootstrap a
        # missing handoff for one-shot/rehearsal use, but it must never
        # overwrite the multi-station catalog with its narrower acquisition
        # allowlist or couple target registry freshness to model cadence.
        try:
            existing_registry = (
                load_market_registry_snapshot(registry_path)
                if registry_path.is_file()
                else None
            )
        except Exception:
            existing_registry = None
        # The comment above assigns ongoing publication to a resident registry
        # worker, but no such writer exists -- this is the only code in the
        # repository that writes the file.  Reusing it whenever it was present
        # therefore froze the catalog at whatever the first bootstrap saw: the
        # snapshot went two days stale, so a new event date was registered with
        # the station count it happened to have at bootstrap time, and every
        # downstream capture scope inherited that stale bound.
        #
        # Refreshing is still refused while a narrowing allowlist is in force,
        # which is what the original guard was protecting: a single-station
        # acquisition run must never overwrite the multi-station catalog.
        stale = self._registry_export_is_stale(
            registry_path, as_of_utc=as_of_utc
        )
        # Narrowing means exactly what the export call below treats as
        # narrowing: a single station or a single event date, which is what
        # makes the export smaller than the catalog.  A full 45-station
        # allowlist is the normal resident configuration, so testing merely for
        # a non-empty allowlist suppressed every refresh.
        narrowed = (
            len(station_allowlist) == 1 or len(event_date_allowlist) == 1
        )
        if existing_registry is None or (stale and not narrowed):
            export = export_shared_live_registry(
                source_db=self.canonical_source_db_path,
                output_path=registry_path,
                station_id=(
                    station_allowlist[0]
                    if len(station_allowlist) == 1
                    else ""
                ),
                event_date=(
                    event_date_allowlist[0]
                    if len(event_date_allowlist) == 1
                    else ""
                ),
                as_of_utc=as_of_utc,
                include_not_started_for_p0=True,
                current_catalog_only=True,
            )
        else:
            export = {
                "schema": str(existing_registry.get("schema") or ""),
                "state": "REUSED_RESIDENT_REGISTRY_EXPORT",
                "snapshot_sha256": str(
                    existing_registry.get("snapshot_sha256") or ""
                ),
                "row_count": int(
                    existing_registry.get("row_count") or 0
                ),
                "output": str(registry_path),
                "source_database": str(
                    existing_registry.get("source_database_path") or ""
                ),
                "boundary": {
                    "reads_shared_live_database": False,
                    "writes_shared_live_database": False,
                    "writes_target_database": False,
                    "calls_order_gateway": False,
                    "places_orders": False,
                },
                "decoupled_from_nwp": True,
            }
        if getattr(self, "commit_conn", None) is None:
            # Network prefetch is deliberately read-only against the target
            # SSOT.  The sole production worker ingests this atomically
            # replaced registry snapshot at the start of its next cycle.
            registry_ingress = {
                "state": "deferred_to_production_writer",
                "wrote_rows": 0,
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
            }
        else:
            registry_ingress = ingest_target_market_registry(
                self.commit_conn,
                snapshot_path=registry_path,
                commit=True,
                as_of_utc=as_of_utc,
            )
        registry = load_market_registry_snapshot(registry_path)
        scopes = registry_capture_scopes(
            registry,
            as_of_utc=as_of_utc,
            max_scopes=MAX_DISCOVERED_SCOPES,
        )
        if station_allowlist:
            allowed = set(station_allowlist)
            scopes = [
                item
                for item in scopes
                if str(item["station_ids"][0]).strip().upper()
                in allowed
            ]
        if event_date_allowlist:
            allowed_dates = set(event_date_allowlist)
            scopes = [
                item
                for item in scopes
                if str(item["event_date"]).strip() in allowed_dates
            ]
        if not scopes:
            return {
                "schema": "bayes_basket_target_resident_nwp_cycle_v1",
                "state": (
                    "blocked_fail_closed"
                    if station_allowlist or event_date_allowlist
                    else "no_due_registry_scope"
                ),
                "reason": (
                    "resident_nwp_exact_station_scope_missing"
                    if station_allowlist or event_date_allowlist
                    else "no_due_registry_scope"
                ),
                "station_allowlist": list(station_allowlist),
                "event_date_allowlist": list(event_date_allowlist),
                "station_scope_mode": "canonical_allowlist",
                "configured_station_ids": list(self.station_allowlist),
                "discovered_station_ids": [],
                "scope_count": 0,
                "registry_export": export,
                "registry_ingress": registry_ingress,
                "writes_target_database": bool(
                    registry_ingress.get("wrote_rows")
                ),
                "calls_order_gateway": False,
                "places_orders": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        # P0 authority was replaced by calibrated native-member ensembles.
        # Reuse an already retained official-native packet first.  Only when
        # no usable native packet exists (missing, invalid, stale, or rejected
        # by its own activation/OOS contract) do we capture the same scope from
        # Open-Meteo as an explicit ``aggregator_republisher`` no-live
        # fallback.  This prevents duplicate ECMWF/native downloads while
        # preserving strict lineage and historical-OOS gates.
        #
        # Fresh arrivals are drained before the cursor moves.  A captured
        # packet used to wait in the inbox for the rotation to come back
        # around -- but a sweep is 270 scopes at 20 a poll, and the packet's
        # own compile window is six hours, so tail-of-catalog stations kept
        # losing the race: ZUUU 2026-08-13's 00Z packet landed at 00:54Z and
        # expired unconsumed at 06:54Z while every P1 of the day stayed
        # chained to a two-system root a day and a half old.  A model update
        # drives P0; it must not also have to win a queue position.
        arrival_drain = self._drain_fresh_official_native_arrivals(
            read_conn,
            as_of_utc=as_of_utc,
        )
        self._official_native_arrival_drain_last = arrival_drain
        native_scope_index = (
            int(getattr(self, "_native_ensemble_scope_cursor", 0))
            % len(scopes)
        )
        native_scope = scopes[native_scope_index]
        self._native_ensemble_scope_cursor = (
            native_scope_index + 1
        ) % len(scopes)
        native_station = str(
            native_scope["station_ids"][0]
        ).strip().upper()
        native_event_date = str(native_scope["event_date"])
        official_inbox_path = (
            self.nwp_runtime_root
            / "official_native_ensemble_forward_inbox.json"
        )
        official_packet: Mapping[str, Any] | None = None
        # A usable packet whose run a newer cycle has superseded.  Kept so the
        # scope can keep compiling from it while the fresher capture is still
        # in flight; see where it is consumed below.
        superseded_packet: Mapping[str, Any] | None = None
        official_probe_reason = "official_native_packet_missing"
        if official_inbox_path.is_file():
            try:
                official_packets = _load_native_forward_inbox(
                    official_inbox_path,
                    error_code="official_native_forward_inbox_invalid",
                )
                for candidate in official_packets:
                    if not (
                        str(candidate.get("schema") or "")
                        == OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA
                        and str(candidate.get("station_id") or "")
                        .strip()
                        .upper()
                        == native_station
                        and str(candidate.get("event_date") or "")
                        == native_event_date
                    ):
                        continue
                    # Presence is not usability.  A packet the compiler will
                    # reject as stale still satisfied this probe, so the scope
                    # took the reuse path, failed to compile, and never asked
                    # for a fresh capture -- leaving the inbox to age out with
                    # nothing able to replace it.  Treating a too-old packet as
                    # absent is what lets the capture below run.
                    if _native_forward_packet_is_stale(
                        candidate, as_of_utc=as_of_utc
                    ):
                        official_probe_reason = (
                            "official_native_packet_stale"
                        )
                        continue
                    # A fresh TAF issuance arrived for this scope after the
                    # retained packet was built.  Reusing the packet would
                    # serve the superseded issuance for hours; treating it
                    # as absent sends the scope through the capture below,
                    # which reuses the retained model archives and only
                    # refreshes the TAF entry.  The in-memory wake is the
                    # fast path; the durable comparison is what survives a
                    # restart between the ingest and the rebuild.
                    if (native_station, native_event_date) in getattr(
                        self, "_taf_arrival_pending", ()
                    ) or _packet_taf_superseded(
                        read_conn,
                        candidate,
                        station_id=native_station,
                        event_date=native_event_date,
                    ):
                        official_probe_reason = (
                            "taf_arrival_recompile_pending"
                        )
                        continue
                    # A newer cycle has published since this packet was built.
                    # Staleness alone did not catch that: these families issue
                    # on a fixed schedule, and a packet from the 00Z run stays
                    # inside its freshness window for hours after 06Z lands, so
                    # the scope would sit on superseded fields until the clock
                    # -- not the publisher -- decided otherwise.  Treating a
                    # superseded packet as absent is what makes the lane follow
                    # publication instead of expiry.  The capture keys on run
                    # identity, so the first scope of the day pays for the new
                    # run and its siblings ride the same pass.
                    packet_run_init = _native_packet_run_init(candidate)
                    newest_rung = _official_native_run_ladder(
                        event_date=native_event_date, as_of_utc=as_of_utc
                    )
                    if (
                        packet_run_init is not None
                        and newest_rung
                        and newest_rung[0][2] > packet_run_init
                    ):
                        # Held, not discarded.  The capture runs off-thread and
                        # takes minutes, so treating this packet as simply
                        # absent would leave every scope of the date with no
                        # official evidence between the new cycle publishing
                        # and its capture landing -- trading a stale-by-minutes
                        # packet for none at all.  It is superseded, which is a
                        # reason to fetch, not a reason to stop reading.
                        superseded_packet = candidate
                        official_probe_reason = (
                            "official_native_packet_superseded_by_newer_run"
                        )
                        continue
                    official_packet = candidate
                    official_probe_reason = "official_native_packet_found"
                    break
            except Exception as exc:
                official_probe_reason = (
                    "official_native_packet_unavailable:"
                    f"{type(exc).__name__}:{exc}"
                )
        official_capture: dict[str, Any] | None = None
        if official_packet is None:
            # The official-native inbox used to be filled only by an operator
            # command, so it went stale and every scope silently degraded to the
            # Open-Meteo republisher -- which can never be forward P0 authority.
            # Capture the one scope this rotation step owns, then re-probe.  The
            # bounded per-scope throttle below keeps a station that genuinely has
            # no published cycle from re-downloading on every cycle.
            official_capture = self._capture_official_native_packet(
                station_id=native_station,
                event_date=native_event_date,
                as_of_utc=as_of_utc,
                inbox_path=official_inbox_path,
                # The lead is advisory now: the capture's run ladder picks the
                # newest published run that covers the local day, so scopes
                # that only differ in lead label share one capture.
                lead_days=(
                    2
                    if native_scope.get("lead_days") is None
                    else int(native_scope["lead_days"])
                ),
            )
            if official_capture.get("state") in {
                "CAPTURED", "SUBMITTED", "IN_FLIGHT",
            }:
                # The refresh is on its way (or done): the pending flag has
                # served its purpose.  Deferred/throttled admissions keep the
                # flag so the next pass retries the arrival-triggered refresh.
                getattr(self, "_taf_arrival_pending", set()).discard(
                    (native_station, native_event_date)
                )
            if official_capture.get("state") == "CAPTURED":
                try:
                    for candidate in _load_native_forward_inbox(
                        official_inbox_path,
                        error_code="official_native_forward_inbox_invalid",
                    ):
                        if (
                            str(candidate.get("schema") or "")
                            == OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA
                            and str(candidate.get("station_id") or "")
                            .strip()
                            .upper()
                            == native_station
                            and str(candidate.get("event_date") or "")
                            == native_event_date
                        ):
                            official_packet = candidate
                            official_probe_reason = (
                                "official_native_packet_captured"
                            )
                            break
                except Exception as exc:
                    official_probe_reason = (
                        "official_native_packet_unavailable:"
                        f"{type(exc).__name__}:{exc}"
                    )
        if official_packet is None and superseded_packet is not None:
            # The fresher run was asked for above and is still on its way.  Its
            # arrival is what this scope wants, but not at the cost of having
            # no official evidence until it lands: the packet in hand is the
            # previous cycle's, which is exactly what the scope would have been
            # using had the new cycle not published yet.  The compiler's own
            # staleness gate still governs how old is too old.
            official_packet = superseded_packet
            official_probe_reason = (
                "official_native_packet_superseded_run_capture_pending"
            )
        # A capture performed inside this cycle finishes *after* the cycle's
        # ``as_of_utc`` was taken, and the compiler rejects a packet captured
        # later than the decision instant (`native_ensemble_p0_future_capture_
        # forbidden`).  Re-read the clock so the decision instant genuinely
        # follows the capture; every other consumer keeps the cycle's as-of.
        native_decision_at_utc = (
            utc_now()
            if official_capture is not None
            and official_capture.get("state") == "CAPTURED"
            else as_of_utc
        )
        if official_packet is not None:
            try:
                compiled_official = _compile_native_ensemble_p0_runtime(
                    read_conn,
                    forward_packet=official_packet,
                    decision_at_utc=native_decision_at_utc,
                    native_oos_snapshot=self.native_oos_snapshot,
                )
                if (
                    isinstance(compiled_official, Mapping)
                    and compiled_official.get("schema")
                    == "calibrated_native_cross_ensemble_p0_packet_v1"
                ):
                    compiled_official = dict(compiled_official)
                    if self.commit_conn is not None:
                        compiled_official[
                            "forward_prediction_persistence"
                        ] = persist_native_ensemble_forward_prediction(
                            self.commit_conn,
                            compiled_official,
                        )
                        p0_inbox_merge = _upsert_native_p0_inbox(
                            self.p0_inbox_path,
                            compiled_packets=[compiled_official],
                            generated_at_utc=as_of_utc,
                        )
                    else:
                        p0_inbox_merge = {
                            "wrote": False,
                            "accepted_count": 0,
                            "packet_count": 0,
                        }
                    rotation_receipt = self._persist_nwp_rotation(
                        station_ids=station_allowlist,
                        scopes=scopes,
                        selected_scopes=[native_scope],
                        outcomes=[{
                            "scope_key": nwp_rotation_scope_key(native_scope),
                            "state": "OFFICIAL_NATIVE_REUSED",
                            "candidate_only": False,
                            "reason": "official_native_precedence",
                        }],
                        as_of_utc=as_of_utc,
                    )
                    return {
                        "schema": "bayes_basket_target_resident_nwp_cycle_v1",
                        "state": "official_native_reused",
                        "reason": "official_native_precedence",
                        "arrival_drain": arrival_drain,
                        **rotation_receipt,
                        "station_id": native_station,
                        "event_date": native_event_date,
                        "source_role": "official_native",
                        "official_packet_reused": True,
                        "republisher_fallback_used": False,
                        "official_probe_reason": official_probe_reason,
                        "official_native_capture": official_capture,
                        "native_p0_compile": compiled_official,
                        "p0_inbox_path": str(self.p0_inbox_path),
                        "p0_inbox_wrote": bool(p0_inbox_merge.get("wrote")),
                        "writes_target_database": False,
                        "calls_order_gateway": False,
                        "places_orders": False,
                        "live_submit_authorized": False,
                        "orders_submitted": 0,
                    }
                official_probe_reason = "official_native_compile_rejected"
            except Exception as exc:
                official_probe_reason = (
                    "official_native_compile_rejected:"
                    f"{type(exc).__name__}:{exc}"
                )
        official_capture_state = str(
            (official_capture or {}).get("state") or ""
        )
        if (
            official_packet is None
            and official_capture_state
            in OFFICIAL_NATIVE_CAPTURE_PENDING_STATES
        ):
            # Official first: this scope's own packet is already being fetched,
            # so buying an aggregator one now pays full price for evidence that
            # will be superseded before it is used.
            rotation_receipt = self._persist_nwp_rotation(
                station_ids=station_allowlist,
                scopes=scopes,
                selected_scopes=[native_scope],
                outcomes=[{
                    "scope_key": nwp_rotation_scope_key(native_scope),
                    "state": "AWAITING_OFFICIAL_NATIVE",
                    "candidate_only": False,
                    "reason": (
                        "official_native_capture_"
                        f"{official_capture_state.lower()}"
                    ),
                }],
                as_of_utc=as_of_utc,
            )
            return {
                "schema": "bayes_basket_target_resident_nwp_cycle_v1",
                "state": "awaiting_official_native",
                "reason": (
                    "official_native_precedence_no_republisher_spend"
                ),
                "arrival_drain": arrival_drain,
                **rotation_receipt,
                "station_id": native_station,
                "event_date": native_event_date,
                "source_role": "official_native",
                "official_packet_reused": False,
                "republisher_fallback_used": False,
                "official_probe_reason": official_probe_reason,
                "official_native_capture": official_capture,
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        native_rows = [
            row
            for row in registry.get("rows") or []
            if isinstance(row, Mapping)
            and str(row.get("station_id") or "").strip().upper()
            == native_station
            and str(row.get("event_date") or "")
            == native_event_date
        ]
        if (
            getattr(self, "_republisher_quota_exhausted_utc_date", None)
            == _utc_date(as_of_utc)
        ):
            # Skip the spend, keep the turn.  The scope still advances and its
            # official native capture was already kicked off above; only the
            # republisher fallback is withheld, and only until the quota day
            # rolls over.  This is not a blocked state: nothing failed here,
            # and reporting it as blocked would hide whether anything else is
            # wrong with the sweep.
            rotation_receipt = self._persist_nwp_rotation(
                station_ids=station_allowlist,
                scopes=scopes,
                selected_scopes=[native_scope],
                outcomes=[{
                    "scope_key": nwp_rotation_scope_key(native_scope),
                    "state": "REPUBLISHER_QUOTA_EXHAUSTED",
                    "candidate_only": False,
                    "reason": "republisher_daily_quota_exhausted",
                }],
                as_of_utc=as_of_utc,
            )
            return {
                "schema": (
                    "bayes_basket_target_resident_native_ensemble_cycle_v1"
                ),
                "state": "republisher_quota_exhausted",
                "reason": "republisher_daily_quota_exhausted",
                "station_id": native_station,
                "event_date": native_event_date,
                "source_role": "official_native",
                "official_packet_reused": False,
                "republisher_fallback_used": False,
                "official_probe_reason": official_probe_reason,
                "official_native_capture": official_capture,
                **rotation_receipt,
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        try:
            coordinates = station_coordinates(native_station)
            native_packet = capture_open_meteo_ensemble_scope(
                station_id=native_station,
                event_date=native_event_date,
                latitude=float(coordinates["latitude"]),
                longitude=float(coordinates["longitude"]),
                settlement_timezone=str(
                    native_scope["settlement_timezone"]
                ),
                registry_rows=native_rows,
                captured_at_utc=as_of_utc,
            )
            native_inbox_path = (
                self.nwp_runtime_root
                / "native_ensemble_forward_inbox.json"
            )
            existing_packets: list[dict[str, Any]] = []
            if native_inbox_path.is_file():
                existing_inbox = load_bounded_json(
                    native_inbox_path
                )
                if (
                    existing_inbox.get("schema")
                    != "native_ensemble_forward_inbox_v1"
                    or not isinstance(
                        existing_inbox.get("packets"), list
                    )
                ):
                    raise ContractError(
                        "native_ensemble_forward_inbox_invalid"
                    )
                existing_packets = [
                    dict(item)
                    for item in existing_inbox["packets"]
                    if isinstance(item, Mapping)
                ]
            same_snapshot = next(
                (
                    item
                    for item in existing_packets
                    if str(item.get("station_id") or "")
                    == native_station
                    and str(item.get("event_date") or "")
                    == native_event_date
                    and str(
                        item.get("snapshot_content_sha256") or ""
                    )
                    == str(
                        native_packet[
                            "snapshot_content_sha256"
                        ]
                    )
                ),
                None,
            )
            if same_snapshot is None:
                merged_packets = [
                    item
                    for item in existing_packets
                    if not (
                        str(item.get("station_id") or "")
                        == native_station
                        and str(item.get("event_date") or "")
                        == native_event_date
                    )
                ]
                merged_packets.append(native_packet)
                # Drop packets for days that are over in every timezone on
                # earth.  A forward packet describes a forecast for a day that
                # has not settled; once it has, the packet is neither evidence
                # nor a candidate, and keeping it only consumes the budget the
                # open days need.  The official inbox has always pruned this
                # way; this one never did, so it accumulated seven days of
                # closed scopes.
                horizon = _utc_date(as_of_utc) - timedelta(days=1)
                merged_packets = [
                    item
                    for item in merged_packets
                    if (
                        _event_date_or_none(
                            str(item.get("event_date") or "")
                        )
                        is None
                        or _event_date_or_none(
                            str(item.get("event_date") or "")
                        )
                        >= horizon
                    )
                ]
                merged_packets.sort(
                    key=lambda item: (
                        str(item.get("event_date") or ""),
                        str(item.get("station_id") or ""),
                    )
                )
                if len(merged_packets) > MAX_REPUBLISHER_INBOX_PACKETS:
                    merged_packets = merged_packets[
                        -MAX_REPUBLISHER_INBOX_PACKETS:
                    ]
                inbox_identity = {
                    "schema": "native_ensemble_forward_inbox_v1",
                    "packets": merged_packets,
                }
                native_inbox = {
                    **inbox_identity,
                    "content_sha256": content_sha256(
                        inbox_identity
                    ),
                }
                inbox_wrote = write_bounded_json_if_changed(
                    native_inbox_path,
                    native_inbox,
                )
            else:
                native_packet = same_snapshot
                inbox_wrote = False
            native_p0_compile: dict[str, Any]
            try:
                if native_packet.get("schema") != OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA:
                    raise ContractError(
                        "republisher_no_live_selection_not_forward_p0_authority"
                    )
                native_p0_compile = _compile_native_ensemble_p0_runtime(
                    read_conn,
                    forward_packet=native_packet,
                    decision_at_utc=as_of_utc,
                    native_oos_snapshot=self.native_oos_snapshot,
                )
            except Exception as exc:
                native_p0_compile = {
                    "state": "FAIL_CLOSED_NO_P0",
                    "reason": f"{type(exc).__name__}:{exc}",
                    "station_id": native_station,
                    "event_date": native_event_date,
                    "calls_order_gateway": False,
                    "orders_submitted": 0,
                    "live_submit_authorized": False,
                }
            p0_inbox_merge = _upsert_native_p0_inbox(
                self.p0_inbox_path,
                compiled_packets=(
                    [native_p0_compile]
                    if native_p0_compile.get("schema")
                    == "calibrated_native_cross_ensemble_p0_packet_v1"
                    else []
                ),
                generated_at_utc=as_of_utc,
            )
            p0_inbox_wrote = bool(p0_inbox_merge["wrote"])
            rotation_receipt = self._persist_nwp_rotation(
                station_ids=station_allowlist,
                scopes=scopes,
                selected_scopes=[native_scope],
                outcomes=[{
                    "scope_key": nwp_rotation_scope_key(native_scope),
                    "state": "CAPTURED_CANDIDATE",
                    "candidate_only": True,
                    "reason": (
                        "native_ensemble_calibration_and_cross_family_oos_required"
                    ),
                }],
                as_of_utc=as_of_utc,
            )
            return {
                "schema": "bayes_basket_target_resident_native_ensemble_cycle_v1",
                "state": "candidate_captured_no_p0",
                "reason": (
                    "native_ensemble_calibration_and_cross_family_oos_required"
                ),
                "station_id": native_station,
                "event_date": native_event_date,
                "available_product_count": len(
                    native_packet.get("systems") or []
                ),
                "source_role": "aggregator_republisher",
                "official_packet_reused": False,
                "republisher_fallback_used": True,
                "official_probe_reason": official_probe_reason,
                "official_native_capture": official_capture,
                "unavailable_product_count": len(
                    native_packet.get("unavailable_products") or []
                ),
                "cross_model_gate": native_packet.get(
                    "cross_model_gate"
                ),
                "native_p0_compile": native_p0_compile,
                "p0_inbox_path": str(self.p0_inbox_path),
                "p0_inbox_wrote": p0_inbox_wrote,
                "p0_inbox_packet_count": p0_inbox_merge[
                    "packet_count"
                ],
                "snapshot_content_sha256": native_packet.get(
                    "snapshot_content_sha256"
                ),
                "inbox_path": str(native_inbox_path),
                "inbox_wrote": inbox_wrote,
                **rotation_receipt,
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        except Exception as exc:
            # A daily quota is a property of the day, not of this scope.  Once
            # the republisher says the day is spent, remember it so the rest of
            # the sweep stops paying a network round trip per scope to be told
            # again -- the scopes still advance, which is what lets their
            # official native captures start.
            if _is_republisher_daily_quota_exhausted(exc):
                self._republisher_quota_exhausted_utc_date = _utc_date(
                    as_of_utc
                )
            # The cursor advanced before this scope failed.  Persisting here is
            # what stops the next sweep from replaying the same failing scope
            # forever after a restart.
            rotation_receipt = self._persist_nwp_rotation(
                station_ids=station_allowlist,
                scopes=scopes,
                selected_scopes=[native_scope],
                outcomes=[{
                    "scope_key": nwp_rotation_scope_key(native_scope),
                    "state": "BLOCKED_FAIL_CLOSED",
                    "candidate_only": False,
                    "reason": f"{type(exc).__name__}:{exc}"[:200],
                }],
                as_of_utc=as_of_utc,
            )
            return {
                "schema": "bayes_basket_target_resident_native_ensemble_cycle_v1",
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
                "station_id": native_station,
                "event_date": native_event_date,
                **rotation_receipt,
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }

    def _assert_db_activation(self, activation: Mapping[str, Any]) -> None:
        if self.commit_conn is None:
            return
        assert_target_database_owner(self.commit_conn)
        row = self.commit_conn.execute(
            "SELECT state,activation_sha256,activation_json "
            "FROM basket_strategy_activation_state WHERE singleton=1"
        ).fetchone()
        expected = str(activation.get("activation_contract_sha256") or "")
        if row is None or str(row[0]) != "enabled" or str(row[1] or "") != expected:
            raise RuntimeError("target_activation_state_drift")
        try:
            details = json.loads(str(row[2] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("target_activation_state_json_invalid") from exc
        if not isinstance(details, Mapping) or details.get("execution_mode") != PRODUCTION_EXECUTION_MODE:
            raise RuntimeError("target_activation_state_mode_drift")
        for key in ("live_decision_write_authorized", "live_submit_authorized", "places_orders", "calls_order_gateway"):
            if details.get(key) is not False:
                raise RuntimeError(f"target_activation_state_{key}_drift")

    def _scope_cycle_guard(
        self,
        *,
        station_id: str,
        event_date: str,
        candidate: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Return a no-write decision when an unclosed target candidate exists."""

        if self.commit_conn is None:
            return None
        has_training_facts = self.commit_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='basket_policy_training_fact'"
        ).fetchone() is not None
        candidate_scope_key = ""
        if has_training_facts:
            market_snapshot = candidate.get("production_market_snapshot")
            if not isinstance(market_snapshot, Mapping):
                raise ContractError(
                    "target_authority_zero_write_market_snapshot_missing"
                )
            candidate_scope_key = delta_v2_decision_scope_key(
                compiled_plan=candidate,
                market_snapshot=market_snapshot,
            )
        latest_fact = (
            self.commit_conn.execute(
                "SELECT fact_id,cycle_id,decision_id,decision_scope_key "
                "FROM basket_policy_training_fact "
                "WHERE station_id=? AND event_date=? "
                "ORDER BY decision_at_utc DESC,rowid DESC LIMIT 1",
                (station_id, event_date),
            ).fetchone()
            if has_training_facts
            else None
        )
        if (
            latest_fact is not None
            and str(latest_fact[3] or "") == candidate_scope_key
        ):
            return {
                "schema": SCHEMA,
                "ok": True,
                "state": "retained_existing_authority_decision",
                "reason": "target_authority_input_zero_write_guard",
                "cycle_id": str(latest_fact[1] or ""),
                "decision_id": str(latest_fact[2] or ""),
                "training_fact_id": str(latest_fact[0] or ""),
                "decision_scope_key": candidate_scope_key,
                "writes_target_ledger": False,
                "execution_mode": PRODUCTION_EXECUTION_MODE,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        has_decisions = self.commit_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_trade_decisions'"
        ).fetchone() is not None
        if has_decisions:
            rows = self.commit_conn.execute(
                "SELECT c.cycle_id,c.plan_id,c.state FROM basket_target_execution_cycles c "
                "LEFT JOIN auto_trade_decisions d ON d.cycle_id=c.cycle_id "
                "WHERE c.station_id=? AND c.event_date=? AND c.state IN "
                "('prepared','submitting','submitted','reconcile_required') "
                "AND (d.decided_state IS NULL OR d.decided_state<>'blocked') "
                "ORDER BY c.created_at DESC,c.rowid DESC LIMIT 2",
                (station_id, event_date),
            ).fetchall()
        else:
            # Small in-memory fixtures from the guard tests predate the M5
            # decision table.  Preserve their strict retention semantics.
            rows = self.commit_conn.execute(
                "SELECT cycle_id,plan_id,state FROM basket_target_execution_cycles "
                "WHERE station_id=? AND event_date=? AND state IN "
                "('prepared','submitting','submitted','reconcile_required') "
                "ORDER BY created_at DESC, rowid DESC LIMIT 2",
                (station_id, event_date),
            ).fetchall()
        if not rows:
            return None
        if len(rows) > MAX_PREPARED_CYCLES_PER_SCOPE:
            return _blocked(
                "target_prepared_cycle_retention_violation",
                station_id=station_id,
                event_date=event_date,
            )
        current_plan = ""
        plan = candidate.get("plan")
        if isinstance(plan, Mapping):
            current_plan = str(plan.get("plan_id") or "")
        latest = rows[0]
        state = str(latest[2] or "")
        if state != "prepared":
            return _blocked(
                "target_existing_cycle_not_quiescent",
                station_id=station_id,
                event_date=event_date,
            )
        # Full production facts can atomically retire a purely local no-live
        # prepared candidate when newer P1/WS authority changes the scope.
        # Legacy/minimal fixtures lack the facts required to prove this and
        # therefore retain the conservative zero-write behavior.
        if (
            has_training_facts
            and latest_fact is not None
            and candidate_scope_key
            and str(latest_fact[1] or "") == str(latest[0])
        ):
            return {
                "schema": SCHEMA,
                "ok": True,
                "state": "supersede_existing_prepared_candidate",
                "reason": "target_authority_input_superseded",
                "cycle_id": str(latest[0]),
                "decision_id": str(latest_fact[2] or ""),
                "training_fact_id": str(latest_fact[0] or ""),
                "old_decision_scope_key": str(latest_fact[3] or ""),
                "new_decision_scope_key": candidate_scope_key,
                "writes_target_ledger": True,
                "execution_mode": PRODUCTION_EXECUTION_MODE,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        return {
            "schema": SCHEMA,
            "ok": True,
            "state": "retained_existing_prepared_candidate",
            "reason": "target_prepared_cycle_retention_guard",
            "cycle_id": str(latest[0]),
            "existing_plan_id": str(latest[1]),
            "candidate_plan_id": current_plan,
            "max_prepared_cycles_per_scope": MAX_PREPARED_CYCLES_PER_SCOPE,
            "writes_target_ledger": False,
            "execution_mode": PRODUCTION_EXECUTION_MODE,
            "places_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }

    @staticmethod
    def _event_day_started(
        event_date: str,
        *,
        settlement_timezone: str,
        as_of_utc: str,
    ) -> bool:
        """Allow only registry settlement days that have started.

        A future market must not enter the decision scope before the event
        day starts in the fixed ET timezone.
        """

        try:
            state = event_day_window(
                event_date,
                as_of_utc=as_of_utc,
                target_timezone=settlement_timezone,
            )["state"]
        except Exception:
            # A malformed target identity is not a reason to manufacture a
            # blocked decision row.  Leave it to the owning ingress audit.
            return False
        return state in {"open", "closed"}

    @staticmethod
    def _event_day_open(
        event_date: str,
        *,
        settlement_timezone: str,
        as_of_utc: str,
    ) -> bool:
        """Allow decisions only during the registry settlement day.

        A closed day remains available to offline truth reconstruction,
        calibration and audit, but it is no longer an executable target
        scope.  Keeping this distinction at the resident worker boundary
        prevents every poll after the ET close from appending another
        historical no-live decision.
        """

        try:
            state = event_day_window(
                event_date,
                as_of_utc=as_of_utc,
                target_timezone=settlement_timezone,
            )["state"]
        except Exception:
            return False
        return state == "open"

    def _decision_scopes(
        self,
        *,
        as_of_utc: str | None = None,
        include_not_started: bool = True,
    ) -> list[tuple[str, str]]:
        """Discover open scopes plus pre-event P0/P0-M no-live scopes.

        A not-started market is actionable only for the preliminary
        zero-execution lane and only while its committed probability authority
        is P0/P0-M.  Future P1 remains invalid and WS cache rows alone can
        never manufacture a future decision identity.
        """

        if self.conn is None:
            return []
        decision_clock = as_of_utc or utc_now()
        scopes: set[tuple[str, str]] = set()
        try:
            for item in load_started_registry_rows(self.conn, as_of_utc=decision_clock):
                station = str(item.get("station_id") or "").strip()
                day = str(item.get("event_date") or "").strip()
                settlement_timezone = str(
                    item.get("settlement_timezone") or ""
                ).strip()
                if (
                    station
                    and day
                    and settlement_timezone
                    and self._event_day_open(
                        day,
                        settlement_timezone=settlement_timezone,
                        as_of_utc=decision_clock,
                    )
                ):
                    scopes.add((station, day))
        except Exception:
            # The registry is evidence, not a reason to invent an identity. A
            # missing registry leaves the worker globally blocked; when a
            # target-owned cache or probability row does identify a scope we
            # still record that concrete blocker below.
            pass
        try:
            probability_columns = {
                str(row[1])
                for row in self.conn.execute(
                    "PRAGMA table_info(basket_probability_envelope)"
                ).fetchall()
            }
            stage_projection = (
                "stage" if "stage" in probability_columns else "'' AS stage"
            )
            probability_rows = self.conn.execute(
                "SELECT station_id,event_date,settlement_timezone,"
                + stage_projection
                + " FROM basket_probability_envelope "
                "WHERE station_id<>'' AND event_date<>'' "
                "AND state='COMMITTED'"
            ).fetchall()
        except sqlite3.Error:
            probability_rows = []
        probability_scopes: dict[
            tuple[str, str, str], set[str]
        ] = {}
        for station, day, settlement_timezone, stage in probability_rows:
            identity = (
                str(station or "").strip(),
                str(day or "").strip(),
                str(settlement_timezone or "").strip(),
            )
            if not all(identity):
                continue
            probability_scopes.setdefault(identity, set()).add(
                str(stage or "").strip()
            )
        for (
            station_text,
            day_text,
            settlement_timezone,
        ), stages in probability_scopes.items():
            try:
                day_state = str(event_day_window(
                    day_text,
                    as_of_utc=decision_clock,
                    target_timezone=settlement_timezone,
                )["state"])
            except Exception:
                continue
            if day_state == "open":
                scopes.add((station_text, day_text))
            elif (
                include_not_started
                and
                day_state == "not_started"
                and "P1" not in stages
                and bool(stages.intersection({"P0", "P0-M"}))
            ):
                scopes.add((station_text, day_text))
        try:
            ws_rows = self.conn.execute(
                "SELECT DISTINCT station_id,event_date,settlement_timezone "
                "FROM basket_official_ws_evidence_cache "
                "WHERE station_id<>'' AND event_date<>''"
            ).fetchall()
        except sqlite3.Error:
            ws_rows = []
        for station, day, settlement_timezone in ws_rows:
            station_text = str(station or "").strip()
            day_text = str(day or "").strip()
            timezone_text = str(settlement_timezone or "").strip()
            if (
                station_text
                and day_text
                and timezone_text
                and self._event_day_open(
                    day_text,
                    settlement_timezone=timezone_text,
                    as_of_utc=decision_clock,
                )
            ):
                scopes.add((station_text, day_text))
        station_allowlist = set(
            getattr(self, "station_allowlist", ())
        )
        event_date_allowlist = set(
            getattr(self, "event_date_allowlist", ())
        )
        if station_allowlist:
            scopes = {
                (station, day)
                for station, day in scopes
                if station.upper() in station_allowlist
            }
        if event_date_allowlist:
            scopes = {
                (station, day)
                for station, day in scopes
                if day in event_date_allowlist
            }
        return sorted(scopes)

    @staticmethod
    def _blocker_reason_codes(*values: Any) -> list[str]:
        """Extract stable reason codes and keep volatile exception text in details."""

        reasons: set[str] = set()
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            if ":" in text:
                text = text.split(":", 1)[1].strip() or text.split(":", 1)[0].strip()
            if text:
                reasons.add(text)
        return sorted(reasons) or ["target_candidate_blocked"]

    def _standing_blocker_matches(
        self,
        *,
        station_id: str,
        event_date: str,
        reason_codes: Sequence[str],
    ) -> str | None:
        """When this exact blocker started standing, or None if it is new.

        Compares against the scope's most recent decision only.  Anything
        else -- a blocker that lifted and returned, a different set of codes,
        a decision that was not blocked -- is a transition and has to be
        recorded, so only an unbroken repeat of the standing blocker is
        suppressed.

        Read failures return None, which writes.  Losing a transition costs
        the audit trail; writing one extra row costs a row.
        """

        try:
            row = self.commit_conn.execute(
                "SELECT decided_state,reason_codes_json,created_at "
                "FROM auto_trade_decisions "
                "WHERE station_id=? AND event_date=? "
                "ORDER BY created_at DESC LIMIT 1",
                (station_id, event_date),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None or str(row[0]) != "blocked":
            return None
        try:
            standing = json.loads(str(row[1]))
        except (TypeError, ValueError):
            return None
        if not isinstance(standing, list):
            return None
        if [str(value) for value in standing] != [
            str(value) for value in reason_codes
        ]:
            return None
        return str(row[2])

    def _commit_blocked_scope(
        self,
        *,
        station_id: str,
        event_date: str,
        reason_codes: list[str],
        details: Mapping[str, Any] | None,
        created_at_utc: str,
    ) -> dict[str, Any]:
        """Write only the authoritative blocker; never manufacture inputs.

        A blocker that has not changed is not a new fact.  Every swept scope
        used to append a cycle, a decision and a version vector whatever it
        concluded, so 29,566 of 29,910 cycles -- 98.85% -- were empty shells
        recording that nothing had happened again, and the decision table
        alone reached 0.365 GiB of them.  That write amplification, not the
        quote snapshots, is what put the store into its size ceiling and
        stopped the whole chain today.

        So the first occurrence of a blocker is written, every change of
        blocker is written, and a repeat of the one already standing is not.
        What that costs is the ability to say what the book looked like on
        the 47th identical scan; what it keeps is every transition, which is
        what an audit of "when did this start, and why" actually reads --
        today's own three outages were diagnosed from exactly that sequence.
        The sweep receipt below records that the scope was evaluated at all,
        so "scanned and unchanged" stays distinguishable from "never
        scanned", which it was not before.
        """

        if self.commit_conn is None:
            return {
                "state": "not_attempted",
                "reason": "target_atomic_writer_not_injected",
                "writes_target_ledger": False,
                "orders_submitted": 0,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
            }
        try:
            timezone_rows = self.commit_conn.execute(
                "SELECT DISTINCT settlement_timezone "
                "FROM basket_target_market_registry "
                "WHERE station_id=? AND event_date=? "
                "UNION SELECT DISTINCT settlement_timezone "
                "FROM basket_probability_envelope "
                "WHERE station_id=? AND event_date=?",
                (station_id, event_date, station_id, event_date),
            ).fetchall()
            settlement_timezones = {
                str(row[0] or "").strip()
                for row in timezone_rows
                if str(row[0] or "").strip()
            }
            if len(settlement_timezones) != 1:
                raise RuntimeError(
                    "target_scope_settlement_timezone_not_unique"
                )
            unchanged = self._standing_blocker_matches(
                station_id=station_id,
                event_date=event_date,
                reason_codes=reason_codes,
            )
            if unchanged:
                return {
                    "state": "unchanged_blocker_not_rewritten",
                    "reason": "auto_trade_decisions_blocked_authority",
                    "writes_target_ledger": False,
                    "standing_blocker": list(reason_codes),
                    "standing_since_utc": unchanged,
                    "intent_count": 0,
                    "reserved_amount_usdc": 0.0,
                    "orders_submitted": 0,
                    "places_orders": False,
                    "calls_order_gateway": False,
                    "live_submit_authorized": False,
                }
            receipt = commit_blocked_scope_decision(
                self.commit_conn,
                station_id=station_id,
                event_date=event_date,
                settlement_timezone=next(iter(settlement_timezones)),
                strategy_lane="basket",
                reason_codes=reason_codes,
                details=details or {},
                created_at_utc=created_at_utc,
            )
        except Exception as exc:
            return {
                "state": "blocked_fail_closed",
                "reason": f"target_blocked_decision_write_failed:{type(exc).__name__}:{exc}",
                "writes_target_ledger": False,
                "orders_submitted": 0,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
            }
        receipt_state = str(receipt.get("state") or "committed")
        return {
            "state": receipt_state,
            "reason": "auto_trade_decisions_blocked_authority",
            "writes_target_ledger": True,
            "intent_count": 0,
            "reserved_amount_usdc": 0.0,
            "receipt": receipt,
            "orders_submitted": 0,
            "places_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
        }

    def _commit_blocked_scopes(
        self,
        *,
        scopes: list[tuple[str, str]],
        reason_codes: list[str],
        details: Mapping[str, Any] | None,
        created_at_utc: str,
    ) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        for station_id, event_date in scopes:
            receipts.append({
                "station_id": station_id,
                "event_date": event_date,
                **self._commit_blocked_scope(
                    station_id=station_id,
                    event_date=event_date,
                    reason_codes=reason_codes,
                    details=details,
                    created_at_utc=created_at_utc,
                ),
            })
        return receipts

    @staticmethod
    def _blocked_receipt_summary(receipts: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "station_id": str(item.get("station_id") or ""),
                "event_date": str(item.get("event_date") or ""),
                "state": str(item.get("state") or ""),
                "reason": str(item.get("reason") or ""),
                "cycle_id": str((item.get("receipt") or {}).get("cycle_id") or "")
                if isinstance(item.get("receipt"), Mapping) else "",
                "decision_id": str((item.get("receipt") or {}).get("decision_id") or "")
                if isinstance(item.get("receipt"), Mapping) else "",
                "idempotent": bool((item.get("receipt") or {}).get("idempotent", False))
                if isinstance(item.get("receipt"), Mapping) else False,
            }
            for item in receipts
        ]

    def _commit_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        station_id: str,
        event_date: str,
        created_at_utc: str,
    ) -> dict[str, Any]:
        if self.commit_conn is None:
            return {
                "state": "not_attempted",
                "reason": "target_atomic_writer_not_injected",
                "writes_target_ledger": False,
                "orders_submitted": 0,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
            }
        production_inputs = candidate.get("production_inputs")
        if isinstance(production_inputs, Mapping):
            delta_v2_blocker = str(
                production_inputs.get("delta_v2_resolution_blocker") or ""
            ).strip()
            if delta_v2_blocker:
                # Missing/unpromoted/OOS-failed v2 authority is not
                # an optimizer HOLD. It is an incomplete decision contract.
                # Preserve the exact resolver reason and do not let the
                # downstream atomic-fact adapter mask it as a probability error
                # or materialize a false forward training fact.
                raise ContractError(delta_v2_blocker)
        retained = self._scope_cycle_guard(
            station_id=station_id,
            event_date=event_date,
            candidate=candidate,
        )
        supersession = None
        if (
            retained is not None
            and retained.get("state")
            == "supersede_existing_prepared_candidate"
        ):
            supersession = retained
        elif retained is not None:
            return retained
        # P0 opens on the model ensemble and P0-M re-confirms on the TAF
        # revision.  Neither has an observed METAR to root on, so the P1-shaped
        # observation facts simply do not exist for them.  The ledger already
        # picks its chain head from the stage, so hand it nothing here rather
        # than a hollow observation that would let a forecast pose as a
        # settled fact.
        plan = candidate.get("plan")
        plan_lineage = plan.get("lineage") if isinstance(plan, Mapping) else None
        candidate_stage = (
            str(plan_lineage.get("probability_stage") or "").strip().upper()
            if isinstance(plan_lineage, Mapping)
            else ""
        )
        facts = (
            {
                "awc_observation_contract": None,
                "truth_observation_revision_content_sha256": "",
                "bound": None,
            }
            if candidate_stage in {"P0", "P0-M"}
            else target_atomic_facts_from_cycle(candidate)
        )
        market_snapshot = candidate.get("production_market_snapshot")
        if not isinstance(market_snapshot, Mapping):
            raise RuntimeError("production_market_snapshot_missing_for_atomic_commit")
        if str(market_snapshot.get("station_id") or "") != station_id or str(market_snapshot.get("event_date") or "") != event_date:
            raise RuntimeError("production_market_snapshot_scope_mismatch_for_atomic_commit")
        with _writer_transaction(
            self.commit_conn,
            savepoint="candidate_persistence_cas",
        ):
            committed = commit_production_candidate_atomic(
                self.commit_conn,
                compiled_plan=candidate,
                market_snapshot=market_snapshot,
                awc_observation_contract=facts["awc_observation_contract"],
                truth_observation_revision_content_sha256=facts[
                    "truth_observation_revision_content_sha256"
                ],
                bound=facts["bound"],
                created_at_utc=created_at_utc,
                supersede_prepared_cycle_id=(
                    str(supersession["cycle_id"])
                    if supersession is not None
                    else None
                ),
                new_decision_scope_key=(
                    str(supersession["new_decision_scope_key"])
                    if supersession is not None
                    else None
                ),
            )
            atomic_receipt = committed.get("receipt")
            if not isinstance(atomic_receipt, Mapping):
                raise ContractError(
                    "candidate_atomic_receipt_mapping_required"
                )
            cycle_id = str(
                atomic_receipt.get("cycle_id") or ""
            ).strip()
            if not cycle_id:
                raise ContractError("candidate_atomic_cycle_id_missing")
            vector_columns = ",".join(
                "v." + field for field in EXECUTION_VERSION_FIELDS
            )
            vector_rows = self.commit_conn.execute(
                "SELECT i.order_intent_id,"
                + vector_columns
                + ",v.vector_sha256 "
                "FROM basket_target_order_intents i "
                "JOIN basket_target_execution_version_vectors v "
                "ON v.order_intent_id=i.order_intent_id "
                "WHERE i.cycle_id=? ORDER BY i.order_intent_id",
                (cycle_id,),
            ).fetchall()
            current_vectors = {
                str(row[0]): {
                    "schema": EXECUTION_VERSION_VECTOR_SCHEMA,
                    **{
                        field: row[index + 1]
                        for index, field in enumerate(
                            EXECUTION_VERSION_FIELDS
                        )
                    },
                    "vector_sha256": row[
                        len(EXECUTION_VERSION_FIELDS) + 1
                    ],
                }
                for row in vector_rows
            }
            consumption_cas = (
                validate_persisted_candidate_consumption_cas(
                    self.commit_conn,
                    cycle_id=cycle_id,
                    current_vectors_by_intent=current_vectors,
                    at_utc=created_at_utc,
                )
            )
            if (
                consumption_cas.get("authorizes_order") is not False
                or consumption_cas.get("evidence_only") is not True
                or consumption_cas.get("live_submit_authorized") is not False
                or int(consumption_cas.get("orders_submitted", -1)) != 0
            ):
                raise ContractError(
                    "candidate_consumption_no_live_boundary_invalid"
                )
            return {
                **committed,
                "candidate_consumption_cas": consumption_cas,
                "authorizes_order": False,
                "evidence_only": True,
            }

    def _commit_stage_zero_execution_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        station_id: str,
        event_date: str,
        created_at_utc: str,
    ) -> dict[str, Any]:
        """Persist an open-day no-execution evaluation without fact writes."""
        if self.commit_conn is None:
            return {
                "state": "not_attempted",
                "reason": "target_atomic_writer_not_injected",
                "writes_target_ledger": False,
                "intent_count": 0,
                "reserved_amount_usdc": 0.0,
                "orders_submitted": 0,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
            }
        anchor = target_stage_zero_execution_anchor_from_cycle(candidate)
        return commit_production_stage_zero_execution_atomic(
            self.commit_conn,
            compiled_plan=candidate,
            anchor=anchor,
            created_at_utc=created_at_utc,
        )

    def _commit_current_stage_no_execution_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        station_id: str,
        event_date: str,
        created_at_utc: str,
    ) -> dict[str, Any]:
        """Route every materialized probability stage to NO_EXECUTION only."""

        probability = candidate.get("probability_envelope")
        stage = (
            str(probability.get("stage") or "").strip().upper()
            if isinstance(probability, Mapping)
            else ""
        )
        if stage not in {"P0", "P0-M", "P1"}:
            raise ContractError(
                "resident_current_scope_probability_stage_invalid"
            )
        # Stage-zero is intentionally limited to pre-final P0/P0-M.  Once
        # official METAR/SPECI materializes P1, the final-authority path must
        # atomically persist the canonical observation/bound together with
        # the blocked no-live decision.  The ledger rejects a P1 stage-zero
        # anchor by contract, so routing P1 through that helper would turn a
        # healthy no-live candidate into a false fail-closed blocker.
        if stage == "P1":
            return self._commit_candidate(
                candidate,
                station_id=station_id,
                event_date=event_date,
                created_at_utc=created_at_utc,
            )
        return self._commit_stage_zero_execution_candidate(
            candidate,
            station_id=station_id,
            event_date=event_date,
            created_at_utc=created_at_utc,
        )

    def _flush_cash_phase_forward_observations_probe(self) -> dict[str, Any]:
        """Materialize atomically bound receipts into the bounded OOS store.

        The durable source is ``auto_trade_decisions.details_json``.  Scanning
        it by an append-only rowid cursor makes a store-write failure retryable
        and avoids an outbox or a second decision authority.

        This method is the validation core.  The public wrapper below invokes
        it in a one-row, no-write probe mode so a poison row can be quarantined
        without losing the valid prefix that precedes it.
        """

        if self.commit_conn is None:
            return {
                "state": "not_attempted",
                "reason": "target_atomic_writer_not_injected",
                "observation_count": 0,
                "written": False,
            }
        store_path = getattr(self, "cash_phase_forward_store_path", None)
        if store_path is None:
            raise ContractError("cash_phase_forward_store_path_missing")
        store_root = getattr(
            self,
            "cash_phase_forward_store_root",
            Path(store_path).parent,
        )
        store_state = read_forward_store_state(
            store_path,
            allowed_root=store_root,
        )
        source_cursor = int(
            getattr(
                self,
                "_forward_flush_source_cursor_override",
                store_state["last_source_rowid"],
            )
        )
        probe_rowid = getattr(self, "_forward_flush_probe_rowid", None)
        probe_mode = bool(getattr(self, "_forward_flush_probe", False))
        scan_limit = int(
            getattr(
                self,
                "_forward_flush_scan_limit",
                MAX_FORWARD_DECISIONS_PER_PASS,
            )
        )
        if not 1 <= scan_limit <= MAX_FORWARD_DECISIONS_PER_PASS:
            raise ContractError("cash_phase_forward_scan_limit_invalid")
        if probe_rowid is not None:
            try:
                probe_rowid = int(probe_rowid)
            except (TypeError, ValueError) as exc:
                raise ContractError("cash_phase_forward_probe_rowid_invalid") from exc
            if probe_rowid <= source_cursor:
                raise ContractError("cash_phase_forward_probe_cursor_order_invalid")
            header_sql = (
                "SELECT d.rowid,d.decision_id,d.decision_sha256,"
                "length(CAST(d.reason_codes_json AS BLOB)),"
                "length(CAST(d.details_json AS BLOB)),"
                "(SELECT COUNT(*) FROM basket_policy_training_fact f "
                " WHERE f.cycle_id=d.cycle_id AND f.decision_id=d.decision_id),"
                "(SELECT length(CAST(f.policy_version_vector_json AS BLOB)) "
                " FROM basket_policy_training_fact f "
                " WHERE f.cycle_id=d.cycle_id AND f.decision_id=d.decision_id "
                " LIMIT 1),"
                "(SELECT length(CAST(f.feature_json AS BLOB)) "
                " FROM basket_policy_training_fact f "
                " WHERE f.cycle_id=d.cycle_id AND f.decision_id=d.decision_id "
                " LIMIT 1) "
                "FROM auto_trade_decisions d "
                "WHERE d.rowid=? AND d.details_json LIKE "
                "'%\"forward_observation_status\":\"ELIGIBLE\"%' "
                "LIMIT 1"
            )
            header_params: tuple[Any, ...] = (probe_rowid,)
        else:
            header_sql = (
                "SELECT d.rowid,d.decision_id,d.decision_sha256,"
                "length(CAST(d.reason_codes_json AS BLOB)),"
                "length(CAST(d.details_json AS BLOB)),"
                "(SELECT COUNT(*) FROM basket_policy_training_fact f "
                " WHERE f.cycle_id=d.cycle_id AND f.decision_id=d.decision_id),"
                "(SELECT length(CAST(f.policy_version_vector_json AS BLOB)) "
                " FROM basket_policy_training_fact f "
                " WHERE f.cycle_id=d.cycle_id AND f.decision_id=d.decision_id "
                " LIMIT 1),"
                "(SELECT length(CAST(f.feature_json AS BLOB)) "
                " FROM basket_policy_training_fact f "
                " WHERE f.cycle_id=d.cycle_id AND f.decision_id=d.decision_id "
                " LIMIT 1) "
                "FROM auto_trade_decisions d "
                "WHERE d.rowid>? AND d.details_json LIKE "
                "'%\"forward_observation_status\":\"ELIGIBLE\"%' "
                "ORDER BY d.rowid LIMIT ?"
            )
            header_params = (source_cursor, scan_limit + 1)
        header_rows = self.commit_conn.execute(
            header_sql,
            header_params,
        ).fetchall()
        more_pending = (
            False
            if probe_rowid is not None
            else len(header_rows) > MAX_FORWARD_DECISIONS_PER_PASS
        )
        header_rows = header_rows[:scan_limit]
        if not header_rows:
            return {
                "state": "already_current",
                "reason": "no_new_eligible_atomic_receipts",
                "observation_count": int(
                    store_state.get("observation_count") or 0
                ),
                "written": False,
                "generation": int(store_state.get("generation") or 0),
            }
        decision_ids: list[str] = []
        total_json_bytes = 0
        for header in header_rows:
            decision_id = str(header[1] or "").strip()
            field_sizes = (
                int(header[3] or 0),
                int(header[4] or 0),
                int(header[6] or 0),
                int(header[7] or 0),
            )
            if (
                not decision_id
                or int(header[0] or 0) <= source_cursor
                or int(header[5] or 0) != 1
                or any(
                    size <= 0 or size > MAX_FORWARD_DECISION_JSON_BYTES
                    for size in field_sizes
                )
            ):
                raise ContractError(
                    "cash_phase_forward_record_cardinality_or_size_invalid"
                )
            total_json_bytes += sum(field_sizes)
            if total_json_bytes > MAX_FORWARD_SCAN_BYTES:
                raise ContractError(
                    "cash_phase_forward_decision_scan_bytes_exceeded"
                )
            decision_ids.append(decision_id)
        assert_process_memory_budget(projected_bytes=total_json_bytes * 3)

        full_row_sql = (
            "SELECT d.cycle_id,d.decision_id,d.plan_id,d.station_id,"
            "d.event_date,d.settlement_timezone,d.strategy_lane,"
            "d.decided_state,d.will_order,d.target_execution_hard_deadline_at,"
            "d.reason_codes_json,d.details_json,d.decision_sha256,d.created_at,"
            "c.plan_sha256,c.plan_id,c.station_id,c.event_date,"
            "c.settlement_timezone,c.strategy_lane,"
            "f.decision_at_utc,f.probability_envelope_id,f.posterior_sha256,"
            "f.quote_observed_at,f.quote_expires_at,f.quote_sha256,"
            "f.inventory_sha256,f.order_state_sha256,"
            "f.policy_version_vector_json,f.feature_json,"
            "f.decision_sha256,f.plan_sha256,f.stage,"
            "length(CAST(d.details_json AS BLOB)),"
            "f.ws_snapshot_id,f.quote_version,"
            "f.inventory_snapshot_id,f.inventory_version,"
            "f.order_state_snapshot_id,f.order_state_version,"
            "f.fact_id,f.content_sha256,f.authority_scope,d.rowid "
            "FROM auto_trade_decisions d "
            "JOIN basket_target_execution_cycles c ON c.cycle_id=d.cycle_id "
            "LEFT JOIN basket_policy_training_fact f "
            "ON f.cycle_id=d.cycle_id AND f.decision_id=d.decision_id "
            "WHERE d.decision_id=? "
            "AND d.details_json LIKE "
            "'%\"forward_observation_status\":\"ELIGIBLE\"%' LIMIT 2"
        )
        rows: list[tuple[Any, ...]] = []
        for decision_id in decision_ids:
            matches = self.commit_conn.execute(
                full_row_sql,
                (decision_id,),
            ).fetchall()
            if len(matches) != 1:
                raise ContractError(
                    "cash_phase_forward_record_exact_row_missing"
                )
            rows.append(tuple(matches[0]))
        eligible = 0
        store_records: list[dict[str, Any]] = []
        for row in rows:
            try:
                reasons = json.loads(str(row[10] or "[]"))
                details = json.loads(str(row[11] or "{}"))
                policy_vector = json.loads(str(row[28] or "{}"))
                feature = json.loads(str(row[29] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ContractError(
                    "cash_phase_forward_decision_json_invalid"
                ) from exc
            if (
                not isinstance(reasons, list)
                or not isinstance(details, Mapping)
                or not isinstance(policy_vector, Mapping)
                or not isinstance(feature, Mapping)
            ):
                raise ContractError(
                    "cash_phase_forward_decision_json_shape_invalid"
                )
            binding = details.get("forward_evidence_binding")
            if not isinstance(binding, Mapping):
                raise ContractError(
                    "cash_phase_forward_binding_mapping_required"
                )
            if details.get("forward_observation_status") != "ELIGIBLE":
                raise ContractError(
                    "cash_phase_forward_status_binding_mismatch"
                )
            probability_stage = str(
                binding.get("probability_stage") or ""
            ).strip().upper()
            stage_zero_pre_final = probability_stage in {"P0", "P0-M"}
            if (
                str(row[6]) != "basket"
                or int(row[8]) != 0
                or str(row[7]) not in {"prepared", "blocked"}
                or details.get("execution_mode") != PRODUCTION_EXECUTION_MODE
                or details.get("live_submit_authorized") is not False
                or details.get("delta_v2_authorized") is not True
            ):
                raise ContractError(
                    "cash_phase_forward_no_live_decision_boundary_invalid"
                )
            if stage_zero_pre_final:
                # Pre-final P0/P0-M decisions use the deliberately smaller
                # blocked-scope ledger receipt.  Its decision hash therefore
                # excludes an execution deadline (no intent can exist before
                # P1).  Require its full marker set before applying that
                # exact identity rule; a generic blocked/P1 receipt cannot
                # borrow the alternate schema.
                if (
                    str(row[7]) != "blocked"
                    or row[9] is not None
                    or details.get("stage_zero_execution_version") != 1
                    or details.get("final_authority") != "P1"
                    or details.get("capital_authorized") is not False
                    or type(details.get("intents_written")) is not int
                    or details.get("intents_written") != 0
                    or type(details.get("reservations_written")) is not int
                    or details.get("reservations_written") != 0
                    or type(details.get("training_facts_written")) is not int
                    or details.get("training_facts_written") != 1
                ):
                    raise ContractError(
                        "cash_phase_forward_stage_zero_receipt_invalid"
                    )
            decision_identity = {
                "cycle_id": str(row[0]),
                "plan_id": str(row[2]),
                "station_id": str(row[3]),
                "event_date": str(row[4]),
                "settlement_timezone": str(row[5]),
                "strategy_lane": str(row[6]),
                "decided_state": str(row[7]),
                "will_order": int(row[8]),
                "reason_codes": reasons,
                "details": dict(details),
            }
            if not stage_zero_pre_final:
                decision_identity["target_execution_hard_deadline_at"] = row[9]
            expected_decision_sha = content_sha256(
                {
                    "schema": "basket_target_decision_fact_v1",
                    **decision_identity,
                }
            )
            expected_decision_id = (
                "decision_" + content_sha256(decision_identity)[:40]
            )
            if (
                str(row[12]) != expected_decision_sha
                or str(row[1]) != expected_decision_id
            ):
                raise ContractError(
                    "cash_phase_forward_decision_content_address_mismatch"
                )
            cycle_identity = {
                "plan_id": str(row[15]),
                "station_id": str(row[16]),
                "event_date": str(row[17]),
                "settlement_timezone": str(row[18]),
                "strategy_lane": str(row[19]),
            }
            if (
                str(row[0])
                != "cycle_" + content_sha256(cycle_identity)[:40]
                or str(row[2]) != str(row[15])
                or str(row[3]) != str(row[16])
                or str(row[4]) != str(row[17])
                or str(row[5]) != str(row[18])
                or str(row[6]) != str(row[19])
            ):
                raise ContractError(
                    "cash_phase_forward_cycle_content_address_mismatch"
                )
            if (
                row[20] is None
                or str(row[20]) != str(row[13])
                or str(row[30]) != str(row[12])
                or str(row[31]) != str(row[14])
            ):
                raise ContractError(
                    "cash_phase_forward_training_fact_binding_mismatch"
                )
            if (
                str(binding.get("station_id") or "") != str(row[3])
                or str(binding.get("event_date") or "") != str(row[4])
                or str(binding.get("settlement_timezone") or "")
                != str(row[5])
                or not timestamp_eq(
                    str(binding.get("decision_at_utc") or ""),
                    str(row[13]),
                )
                or str(binding.get("probability_stage") or "")
                != str(row[32])
            ):
                raise ContractError(
                    "cash_phase_forward_binding_scope_or_clock_mismatch"
                )
            envelope_rows = self.commit_conn.execute(
                "SELECT decision_at_utc,content_sha256,posterior_sha256,"
                "settlement_timezone,station_id,event_date,stage "
                "FROM basket_probability_envelope "
                "WHERE envelope_id=? AND state='COMMITTED' LIMIT 2",
                (str(row[21]),),
            ).fetchall()
            if len(envelope_rows) != 1:
                raise ContractError(
                    "cash_phase_forward_probability_exact_row_missing"
                )
            envelope = envelope_rows[0]
            if (
                not timestamp_eq(
                    str(
                        binding.get(
                            "probability_decision_at_utc"
                        )
                        or ""
                    ),
                    str(envelope[0]),
                )
                or str(binding.get("probability_artifact_sha256") or "")
                != str(envelope[1])
                or str(binding.get("probability_lineage_sha256") or "")
                != str(envelope[2])
                or str(binding.get("settlement_timezone") or "")
                != str(envelope[3])
                or str(row[22]) != str(envelope[2])
                or str(envelope[4]) != str(row[3])
                or str(envelope[5]) != str(row[4])
                or str(envelope[6]) != str(row[32])
            ):
                raise ContractError(
                    "cash_phase_forward_probability_binding_drift"
                )
            quote_rows = self.commit_conn.execute(
                "SELECT quote_observed_at,quote_expires_at,"
                "settlement_timezone,quote_source,buckets_json,"
                "cash_phase_input_json,station_id,event_date,snapshot_id,"
                "market_id,quote_version FROM basket_official_ws_snapshot "
                "WHERE content_sha256=? AND state='COMMITTED' LIMIT 2",
                (str(row[25]),),
            ).fetchall()
            if len(quote_rows) != 1:
                raise ContractError(
                    "cash_phase_forward_ws_exact_row_missing"
                )
            quote = quote_rows[0]
            try:
                quote_buckets = json.loads(str(quote[4] or "[]"))
                cash_input = json.loads(str(quote[5] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ContractError(
                    "cash_phase_forward_ws_payload_invalid"
                ) from exc
            if (
                not isinstance(quote_buckets, list)
                or not isinstance(cash_input, Mapping)
                or not timestamp_eq(
                    str(binding.get("ws_quote_observed_at_utc") or ""),
                    str(quote[0]),
                )
                or not timestamp_eq(
                    str(binding.get("ws_quote_expires_at_utc") or ""),
                    str(quote[1]),
                )
                or str(binding.get("settlement_timezone") or "")
                != str(quote[2])
                or str(quote[3]) != "OFFICIAL_CLOB_WS"
                or int(binding.get("ws_quote_count") or 0)
                != len(quote_buckets)
                or str(binding.get("ws_snapshot_sha256") or "")
                != str(row[25])
                or not timestamp_eq(
                    str(binding.get("cash_observed_at_utc") or ""),
                    str(cash_input.get("observed_at_utc") or ""),
                )
                or str(binding.get("cash_checkpoint_id") or "")
                != str(cash_input.get("checkpoint_id") or "")
                or str(quote[6]) != str(row[3])
                or str(quote[7]) != str(row[4])
                or str(quote[8]) != str(row[34])
                or not str(quote[9] or "").strip()
                or str(quote[10]) != str(row[35])
            ):
                raise ContractError(
                    "cash_phase_forward_ws_or_cash_clock_binding_drift"
                )
            plan_feature = feature.get("plan")
            plan_lineage = (
                plan_feature.get("lineage")
                if isinstance(plan_feature, Mapping)
                else None
            )
            plan_delta_v2_audit = (
                plan_feature.get("delta_v2_audit")
                if isinstance(plan_feature, Mapping)
                else None
            )
            station_policy = policy_vector.get("station_policy")
            if (
                not isinstance(plan_lineage, Mapping)
                or not isinstance(plan_delta_v2_audit, Mapping)
                or not isinstance(station_policy, Mapping)
                or plan_delta_v2_audit.get("authorized") is not True
                or str(binding.get("cash_decision_sha256") or "")
                != str(
                    plan_lineage.get("cash_phase_decision_sha256") or ""
                )
                or str(binding.get("cash_policy_sha256") or "")
                != str(policy_vector.get("cash_phase_policy_sha256") or "")
                or str(binding.get("station_policy_sha256") or "")
                != str(station_policy.get("policy_sha256") or "")
                or str(binding.get("delta_v2_artifact_sha256") or "")
                != str(
                    policy_vector.get("delta_v2_artifact_sha256") or ""
                )
                or str(binding.get("delta_v2_evaluation_sha256") or "")
                != str(plan_delta_v2_audit.get("content_sha256") or "")
                or str(binding.get("inventory_sha256") or "")
                != str(row[26])
                or str(binding.get("order_state_sha256") or "")
                != str(row[27])
            ):
                raise ContractError(
                    "cash_phase_forward_policy_or_state_binding_drift"
                )
            inventory_rows = self.commit_conn.execute(
                "SELECT station_id,event_date,inventory_version,complete,"
                "content_sha256 FROM basket_confirmed_inventory_snapshot "
                "WHERE inventory_snapshot_id=? AND state='COMMITTED' LIMIT 2",
                (str(row[36]),),
            ).fetchall()
            if (
                len(inventory_rows) != 1
                or str(inventory_rows[0][0]) != str(row[3])
                or str(inventory_rows[0][1]) != str(row[4])
                or str(inventory_rows[0][2]) != str(row[37])
                or int(inventory_rows[0][3]) != 1
                or str(inventory_rows[0][4]) != str(row[26])
            ):
                raise ContractError(
                    "cash_phase_forward_inventory_ssot_drift"
                )
            order_state_rows = self.commit_conn.execute(
                "SELECT station_id,event_date,order_state_version,known,"
                "content_sha256 FROM basket_order_state_snapshot "
                "WHERE order_state_snapshot_id=? AND state='COMMITTED' LIMIT 2",
                (str(row[38]),),
            ).fetchall()
            if (
                len(order_state_rows) != 1
                or str(order_state_rows[0][0]) != str(row[3])
                or str(order_state_rows[0][1]) != str(row[4])
                or str(order_state_rows[0][2]) != str(row[39])
                or int(order_state_rows[0][3]) != 1
                or str(order_state_rows[0][4]) != str(row[27])
            ):
                raise ContractError(
                    "cash_phase_forward_order_state_ssot_drift"
                )
            fact_cursor = self.commit_conn.execute(
                "SELECT * FROM basket_policy_training_fact "
                "WHERE fact_id=? LIMIT 2",
                (str(row[40]),),
            )
            fact_rows = fact_cursor.fetchall()
            if len(fact_rows) != 1:
                raise ContractError(
                    "cash_phase_forward_training_fact_exact_row_missing"
                )
            fact_columns = [
                str(column[0]) for column in fact_cursor.description or ()
            ]
            fact = dict(zip(fact_columns, tuple(fact_rows[0])))
            fact_identity = {
                key: value
                for key, value in fact.items()
                if key not in {"fact_id", "content_sha256"}
            }
            expected_fact_sha = content_sha256(
                {"schema": FACT_SCHEMA, **fact_identity}
            )
            if (
                str(fact.get("fact_id") or "")
                != "d2fact_" + expected_fact_sha[:40]
                or str(fact.get("content_sha256") or "")
                != expected_fact_sha
                or str(row[41]) != expected_fact_sha
                or str(fact.get("authority_scope") or "")
                != FACT_AUTHORITY_SCOPE
                or str(row[42]) != FACT_AUTHORITY_SCOPE
                or str(fact.get("station_id") or "") != str(row[3])
                or str(fact.get("event_date") or "") != str(row[4])
                or str(fact.get("settlement_timezone") or "") != str(row[5])
            ):
                raise ContractError(
                    "cash_phase_forward_training_fact_content_address_mismatch"
                )
            receipt = {
                "schema": "bayes_basket_target_atomic_forward_cycle_receipt_v1",
                "cycle_id": str(row[0]),
                "decision_id": str(row[1]),
                "plan_id": str(row[2]),
                "plan_sha256": str(row[14]),
                "decision_sha256": str(row[12]),
                "decision_at_utc": str(row[13]),
                "forward_evidence_binding": dict(binding),
                "execution_mode": PRODUCTION_EXECUTION_MODE,
                "writes_target_ledger": True,
                "orders_submitted": 0,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "authorizes_order": False,
                "evidence_only": True,
            }
            receipt["receipt_sha256"] = content_sha256(receipt)
            station_id = str(binding.get("station_id") or "").strip()
            event_date = str(binding.get("event_date") or "").strip()
            registry_rows = self.commit_conn.execute(
                "SELECT station_id,event_date,settlement_timezone "
                "FROM basket_target_market_registry "
                "WHERE station_id=? AND event_date=? "
                "ORDER BY outcome,yes_token",
                (station_id, event_date),
            ).fetchall()
            registry = {
                "rows": [
                    {
                        "station_id": str(item[0]),
                        "event_date": str(item[1]),
                        "settlement_timezone": str(item[2]),
                    }
                    for item in registry_rows
                ]
            }
            packet = build_forward_observation_packet(
                cycle_receipt=receipt,
                registry=registry,
            )
            packet_rows = packet.get("observations")
            if (
                not isinstance(packet_rows, list)
                or len(packet_rows) != 1
                or not isinstance(packet_rows[0], Mapping)
            ):
                raise ContractError(
                    "cash_phase_forward_single_observation_expected"
                )
            store_records.append({
                "observation": dict(packet_rows[0]),
                "decision_id": str(row[1]),
                "decision_sha256": str(row[12]),
                "source_rowid": int(row[43]),
            })
            eligible += 1
        if probe_mode:
            if not store_records:
                raise ContractError(
                    "cash_phase_forward_probe_record_missing"
                )
            return {
                "schema": "bayes_basket_target_forward_observation_probe_v1",
                "state": "validated",
                "records": store_records,
                "last_source_rowid": max(
                    int(record["source_rowid"]) for record in store_records
                ),
                "eligible_atomic_receipt_count": eligible,
                "more_pending": more_pending,
                "orders_submitted": 0,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
            }
        materialized = commit_forward_observation_batch(
            store_path,
            records=store_records,
            expected_generation=int(store_state["generation"]),
            last_source_rowid=max(
                int(record["source_rowid"]) for record in store_records
            ),
            materialized_at_utc=utc_now(),
            allowed_root=store_root,
        )
        return {
            **materialized,
            "state": (
                "committed_more_pending"
                if more_pending
                else str(materialized["state"])
            ),
            "eligible_atomic_receipt_count": eligible,
            "written": int(materialized.get("inserted") or 0) > 0,
            "more_pending": more_pending,
            "orders_submitted": 0,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
        }

    def _flush_cash_phase_forward_observations(self) -> dict[str, Any]:
        """Validate a bounded source prefix, then commit or quarantine it.

        A poison source row is not allowed to pin the append-only cursor.  The
        valid prefix is committed first; the first poison row is then handed
        to the storage API as identity-only quarantine metadata.  Rows after
        that poison row intentionally remain for the next pass so a failure
        between the two storage calls cannot lose valid observations.

        Canonical storage API::

            quarantine_forward_observation_batch(
                store_path, *, records, expected_generation,
                quarantined_at_utc, allowed_root
            ) -> Mapping

        The API must atomically append only ``decision_id``, source SHA,
        source rowid and error code, and update the store generation/cursor.
        It must reject identity conflicts and resource-limit violations.
        """

        if self.commit_conn is None:
            return {
                "state": "not_attempted",
                "reason": "target_atomic_writer_not_injected",
                "observation_count": 0,
                "written": False,
                "promotion_allowed": False,
            }
        store_path = getattr(self, "cash_phase_forward_store_path", None)
        if store_path is None:
            raise ContractError("cash_phase_forward_store_path_missing")
        store_root = getattr(
            self,
            "cash_phase_forward_store_root",
            Path(store_path).parent,
        )
        store_state = read_forward_store_state(
            store_path,
            allowed_root=store_root,
        )
        source_cursor = int(store_state.get("last_source_rowid") or 0)
        header_rows = self.commit_conn.execute(
            "SELECT d.rowid,d.decision_id,d.decision_sha256,"
            "length(CAST(d.reason_codes_json AS BLOB)),"
            "length(CAST(d.details_json AS BLOB)),"
            "(SELECT COUNT(*) FROM basket_policy_training_fact f "
            " WHERE f.cycle_id=d.cycle_id AND f.decision_id=d.decision_id),"
            "(SELECT length(CAST(f.policy_version_vector_json AS BLOB)) "
            " FROM basket_policy_training_fact f "
            " WHERE f.cycle_id=d.cycle_id AND f.decision_id=d.decision_id "
            " LIMIT 1),"
            "(SELECT length(CAST(f.feature_json AS BLOB)) "
            " FROM basket_policy_training_fact f "
            " WHERE f.cycle_id=d.cycle_id AND f.decision_id=d.decision_id "
            " LIMIT 1) "
            "FROM auto_trade_decisions d "
            "WHERE d.rowid>? AND d.details_json LIKE "
            "'%\"forward_observation_status\":\"ELIGIBLE\"%' "
            "ORDER BY d.rowid LIMIT ?",
            (source_cursor, MAX_FORWARD_DECISIONS_PER_PASS + 1),
        ).fetchall()
        more_pending = len(header_rows) > MAX_FORWARD_DECISIONS_PER_PASS
        header_rows = header_rows[:MAX_FORWARD_DECISIONS_PER_PASS]
        if not header_rows:
            return {
                "state": "already_current",
                "reason": "no_new_eligible_atomic_receipts",
                "observation_count": int(store_state.get("observation_count") or 0),
                "written": False,
                "generation": int(store_state.get("generation") or 0),
                "promotion_allowed": True,
                "orders_submitted": 0,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
            }

        def _sha256(value: Any) -> bool:
            text = str(value or "").strip()
            return len(text) == 64 and all(
                char in "0123456789abcdefABCDEF" for char in text
            )

        def _identity(header: tuple[Any, ...]) -> dict[str, Any] | None:
            try:
                rowid = int(header[0] or 0)
            except (TypeError, ValueError):
                return None
            decision_id = str(header[1] or "").strip()
            source_sha = str(header[2] or "").strip()
            if rowid <= source_cursor or not decision_id or not _sha256(source_sha):
                return None
            return {
                "decision_id": decision_id,
                "source_decision_sha256": source_sha,
                "source_rowid": rowid,
            }

        def _stable_error_code(exc: Exception) -> str:
            base = str(exc).split(":", 1)[0].strip().lower()
            normalized = re.sub(r"[^a-z0-9_.:-]+", "_", base).strip("_")
            return (normalized or "cash_phase_forward_record_invalid")[:128]

        total_header_bytes = 0
        valid_records: list[dict[str, Any]] = []
        poison: dict[str, Any] | None = None
        for header in header_rows:
            identity = _identity(tuple(header))
            if identity is None:
                return {
                    "state": "blocked_fail_closed",
                    "reason": "cash_phase_forward_identity_unstable",
                    "promotion_allowed": False,
                    "cursor_advanced": False,
                    "quarantined_count": 0,
                    "written": False,
                    "observation_count": int(store_state.get("observation_count") or 0),
                    "orders_submitted": 0,
                    "calls_order_gateway": False,
                    "live_submit_authorized": False,
                }
            sizes = (
                int(header[3] or 0),
                int(header[4] or 0),
                int(header[6] or 0),
                int(header[7] or 0),
            )
            total_header_bytes += sum(sizes)
            if total_header_bytes > MAX_FORWARD_SCAN_BYTES:
                return {
                    "state": "blocked_fail_closed",
                    "reason": "cash_phase_forward_decision_scan_bytes_exceeded",
                    "promotion_allowed": False,
                    "cursor_advanced": False,
                    "quarantined_count": 0,
                    "written": False,
                    "observation_count": int(store_state.get("observation_count") or 0),
                    "orders_submitted": 0,
                    "calls_order_gateway": False,
                    "live_submit_authorized": False,
                }
            if (
                int(header[5] or 0) != 1
                or any(size <= 0 or size > MAX_FORWARD_DECISION_JSON_BYTES for size in sizes)
            ):
                poison = {
                    **identity,
                    "error_code": "cash_phase_forward_record_cardinality_or_size_invalid",
                }
                break

            previous = {
                "_forward_flush_probe": getattr(self, "_forward_flush_probe", None),
                "_forward_flush_probe_rowid": getattr(self, "_forward_flush_probe_rowid", None),
                "_forward_flush_source_cursor_override": getattr(
                    self, "_forward_flush_source_cursor_override", None
                ),
                "_forward_flush_scan_limit": getattr(self, "_forward_flush_scan_limit", None),
            }
            try:
                self._forward_flush_probe = True
                self._forward_flush_probe_rowid = identity["source_rowid"]
                self._forward_flush_source_cursor_override = source_cursor
                self._forward_flush_scan_limit = 1
                probe = self._flush_cash_phase_forward_observations_probe()
            except Exception as exc:
                error_text = str(exc)
                # Resource failures and unexpected implementation/database
                # failures are not poison evidence: quarantine cannot prove a
                # stable identity in those cases, so the cursor stays put.
                if (
                    not isinstance(exc, ContractError)
                    or not error_text.startswith("cash_phase_forward_")
                ):
                    return {
                        "state": "blocked_fail_closed",
                        "reason": f"{type(exc).__name__}:{exc}",
                        "promotion_allowed": False,
                        "cursor_advanced": False,
                        "quarantined_count": 0,
                        "written": False,
                        "observation_count": int(store_state.get("observation_count") or 0),
                        "orders_submitted": 0,
                        "calls_order_gateway": False,
                        "live_submit_authorized": False,
                    }
                poison = {
                    **identity,
                    "error_code": _stable_error_code(exc),
                }
                break
            finally:
                for name, value in previous.items():
                    if value is None:
                        self.__dict__.pop(name, None)
                    else:
                        setattr(self, name, value)
            probe_records = probe.get("records") if isinstance(probe, Mapping) else None
            if (
                not isinstance(probe_records, list)
                or len(probe_records) != 1
                or not isinstance(probe_records[0], Mapping)
            ):
                return {
                    "state": "blocked_fail_closed",
                    "reason": "cash_phase_forward_probe_result_invalid",
                    "promotion_allowed": False,
                    "cursor_advanced": False,
                    "quarantined_count": 0,
                    "written": False,
                    "observation_count": int(store_state.get("observation_count") or 0),
                    "orders_submitted": 0,
                    "calls_order_gateway": False,
                    "live_submit_authorized": False,
                }
            valid_records.append(dict(probe_records[0]))

        def _blocked_result(reason: str, *, cursor_advanced: bool = False, quarantined_count: int = 0, **extra: Any) -> dict[str, Any]:
            return {
                "state": "blocked_fail_closed",
                "reason": reason,
                "promotion_allowed": False,
                "cursor_advanced": cursor_advanced,
                "quarantined_count": quarantined_count,
                "orders_submitted": 0,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                **extra,
            }

        materialized: dict[str, Any] | None = None
        if valid_records:
            try:
                assert_process_memory_budget(projected_bytes=total_header_bytes * 3)
                materialized = commit_forward_observation_batch(
                    store_path,
                    records=valid_records,
                    expected_generation=int(store_state["generation"]),
                    last_source_rowid=max(
                        int(record["source_rowid"]) for record in valid_records
                    ),
                    materialized_at_utc=utc_now(),
                    allowed_root=store_root,
                )
            except Exception as exc:
                return _blocked_result(
                    f"{type(exc).__name__}:{exc}",
                    cursor_advanced=False,
                    observation_count=int(store_state.get("observation_count") or 0),
                    written=False,
                )

        if poison is not None:
            try:
                after_valid = read_forward_store_state(
                    store_path,
                    allowed_root=store_root,
                )
                quarantined = quarantine_forward_observation_batch(
                    store_path,
                    records=[{
                        "decision_id": poison["decision_id"],
                        "source_decision_sha256": poison["source_decision_sha256"],
                        "source_rowid": int(poison["source_rowid"]),
                        "error_code": str(poison["error_code"])[:128],
                    }],
                    expected_generation=int(after_valid["generation"]),
                    quarantined_at_utc=utc_now(),
                    allowed_root=store_root,
                )
            except Exception as exc:
                return _blocked_result(
                    f"{type(exc).__name__}:{exc}",
                    cursor_advanced=bool(materialized),
                    poison_cursor_advanced=False,
                    observation_count=int((materialized or {}).get("observation_count") or store_state.get("observation_count") or 0),
                    written=bool(materialized and materialized.get("inserted")),
                )
            return _blocked_result(
                "cash_phase_forward_poison_quarantined",
                cursor_advanced=True,
                poison_cursor_advanced=True,
                quarantined_count=int(
                    quarantined.get("quarantined", quarantined.get("inserted", 1))
                    if isinstance(quarantined, Mapping)
                    else 1
                ),
                written=bool(materialized and materialized.get("inserted")),
                observation_count=int(
                    quarantined.get("observation_count", (materialized or {}).get("observation_count", 0))
                    if isinstance(quarantined, Mapping)
                    else (materialized or {}).get("observation_count", 0)
                ),
                more_pending=more_pending,
            )

        if materialized is None:
            return _blocked_result(
                "cash_phase_forward_no_valid_records",
                cursor_advanced=False,
                observation_count=int(store_state.get("observation_count") or 0),
                written=False,
            )
        return {
            **materialized,
            "state": (
                "committed_more_pending"
                if more_pending
                else str(materialized.get("state") or "committed")
            ),
            "promotion_allowed": True,
            "cursor_advanced": bool(valid_records),
            "eligible_atomic_receipt_count": len(valid_records),
            "written": int(materialized.get("inserted") or 0) > 0,
            "more_pending": more_pending,
            "orders_submitted": 0,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
        }

    def _consume_post_fill_recompute_requests(
        self,
        *,
        activation: Mapping[str, Any],
        scope_config: Mapping[str, Any],
        as_of_utc: str,
    ) -> dict[str, Any]:
        """Recompile every authoritative full or terminal partial fill.

        Receipt ingress only records the authoritative transition and a
        content-addressed request.  The resident worker owns the next
        probability -> quote -> inventory -> objective evaluation.  A request
        is acknowledged only in the same writer transaction that commits the
        replacement candidate; compile/commit failures therefore leave it
        pending for a bounded retry.
        """

        if self.commit_conn is None:
            return {
                "schema": "bayes_basket_target_post_fill_recompute_batch_v1",
                "state": "not_attempted",
                "reason": "target_atomic_writer_not_injected",
                "request_count": 0,
                "applied_count": 0,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        requests = load_post_fill_recompute_requests(self.commit_conn)
        if len(requests) > MAX_POST_FILL_RECOMPUTES_PER_PASS:
            raise RuntimeError("post_fill_recompute_request_cap_exceeded")
        results: list[dict[str, Any]] = []
        blockers: list[dict[str, str]] = []
        seen_request_ids: set[str] = set()
        for request in requests:
            try:
                if not isinstance(request, Mapping):
                    raise ContractError("post_fill_recompute_request_mapping_required")
                request_id = str(request.get("request_id") or "").strip()
                if not request_id or request_id in seen_request_ids:
                    raise ContractError("post_fill_recompute_request_identity_invalid")
                seen_request_ids.add(request_id)
                request_scope = request.get("scope")
                if not isinstance(request_scope, Mapping):
                    raise ContractError("post_fill_recompute_scope_missing")
                station = str(request_scope.get("station_id") or "").strip().upper()
                day = str(request_scope.get("event_date") or "").strip()
                settlement_timezone = str(
                    request_scope.get("settlement_timezone") or ""
                ).strip()
                if not station or not day or not settlement_timezone:
                    raise ContractError("post_fill_recompute_scope_incomplete")
                from .execution_transition_runtime import (
                    post_fill_recompute_binding_sha256,
                )

                source_binding_sha = post_fill_recompute_binding_sha256(
                    request
                )
                if not self._event_day_open(
                    day,
                    settlement_timezone=settlement_timezone,
                    as_of_utc=as_of_utc,
                ):
                    raise ContractError("post_fill_recompute_event_day_not_open")
                transition = self.commit_conn.execute(
                    "SELECT state,inventory_version,order_state_version,"
                    "evidence_json,content_sha256,filled_size,"
                    "filled_notional_usdc "
                    "FROM basket_target_execution_transition_versions "
                    "WHERE order_intent_id=?",
                    (str(request.get("order_intent_id") or ""),),
                ).fetchone()
                if transition is None:
                    raise ContractError("post_fill_recompute_transition_missing")
                if str(transition[0]) not in {
                    "FULL_FILLED",
                    "PARTIAL_FILLED",
                }:
                    raise ContractError(
                        "post_fill_recompute_transition_state_not_recomputable"
                    )
                if (
                    str(transition[1]) != str(request.get("inventory_version") or "")
                    or str(transition[2]) != str(request.get("order_state_version") or "")
                ):
                    raise ContractError("post_fill_recompute_transition_version_drift")
                try:
                    transition_evidence = json.loads(
                        str(transition[3] or "")
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ContractError(
                        "post_fill_recompute_transition_evidence_invalid"
                    ) from exc
                if not isinstance(transition_evidence, Mapping):
                    raise ContractError(
                        "post_fill_recompute_transition_evidence_invalid"
                    )
                if (
                    transition_evidence.get("version_vector")
                    != request.get("source_execution_version_vector")
                    or int(transition_evidence.get("quote_sequence", -1))
                    != int(request.get("quote_sequence", -2))
                    or str(transition_evidence.get("scope_sha256") or "")
                    != str(request.get("source_scope_sha256") or "")
                    or str(transition[4] or "")
                    != str(request.get("source_transition_sha256") or "")
                ):
                    raise ContractError(
                        "post_fill_recompute_transition_full_vector_drift"
                    )
                candidate = compile_production_cycle_from_ssot(
                    self.conn,
                    station_id=station,
                    event_date=day,
                    evaluation_at_utc=as_of_utc,
                    season_bin=str(scope_config["season_bin"]),
                    maturity_bin=str(scope_config["maturity_bin"]),
                    liquidity_bin=str(scope_config["liquidity_bin"]),
                    strategy_version=str(scope_config["strategy_version"]),
                    policy_authority_scope=str(
                        activation["policy_authority_scope"]
                    ),
                    base_policy=(
                        scope_config.get("base_policy")
                        if isinstance(scope_config.get("base_policy"), Mapping)
                        else None
                    ),
                )
                self._assert_post_fill_candidate_projection(
                    candidate=candidate,
                    request=request,
                    transition_content_sha256=str(transition[4] or ""),
                    transition_filled_size=float(transition[5] or 0.0),
                    transition_filled_notional_usdc=float(
                        transition[6] or 0.0
                    ),
                )
                post_fill_market = candidate.get(
                    "production_market_snapshot"
                )
                if not isinstance(post_fill_market, Mapping):
                    raise ContractError(
                        "post_fill_recompute_market_snapshot_missing"
                    )
                completion = {
                    "schema": "bayes_basket_target_post_fill_recompute_result_v1",
                    "request_id": request_id,
                    "order_intent_id": str(request.get("order_intent_id") or ""),
                    "source_cycle_id": str(request.get("cycle_id") or ""),
                    "station_id": station,
                    "event_date": day,
                    "settlement_timezone": settlement_timezone,
                    "recomputed_at_utc": as_of_utc,
                    "source_request_binding_sha256": source_binding_sha,
                    "production_cycle_sha256": str(
                        candidate.get("production_cycle_sha256") or ""
                    ),
                    "post_fill_inventory_sha256": str(
                        post_fill_market.get("inventory_sha256") or ""
                    ),
                    "post_fill_order_state_sha256": str(
                        post_fill_market.get("order_state_sha256") or ""
                    ),
                    "post_fill_transition_sha256": str(
                        transition[4] or ""
                    ),
                    "status": str(candidate.get("status") or ""),
                    "execution_mode": PRODUCTION_EXECUTION_MODE,
                    "places_orders": False,
                    "calls_order_gateway": False,
                    "live_submit_authorized": False,
                    "orders_submitted": 0,
                }
                if not completion["production_cycle_sha256"]:
                    raise ContractError("post_fill_recompute_candidate_sha_missing")
                from .execution_transition_runtime import (
                    complete_post_fill_recompute_request,
                )

                with _writer_transaction(
                    self.commit_conn,
                    savepoint="post_fill_recompute_scope",
                ):
                    atomic_commit = self._commit_candidate(
                        candidate,
                        station_id=station,
                        event_date=day,
                        created_at_utc=as_of_utc,
                    )
                    if atomic_commit.get("writes_target_ledger") is not True:
                        raise ContractError(
                            "post_fill_recompute_candidate_not_committed"
                        )
                    completion["atomic_commit"] = atomic_commit
                    completed = complete_post_fill_recompute_request(
                        self.commit_conn,
                        request_id=request_id,
                        scope=request_scope,
                        inventory_version=str(request["inventory_version"]),
                        order_state_version=str(request["order_state_version"]),
                        result=completion,
                        result_content_sha256=content_sha256(completion),
                        completed_at_utc=as_of_utc,
                    )
                results.append({
                    "request_id": request_id,
                    "station_id": station,
                    "event_date": day,
                    "candidate": {
                        **candidate,
                        "atomic_commit": atomic_commit,
                        "post_fill_recompute_request_id": request_id,
                    },
                    "completion": completed,
                })
            except Exception as exc:
                blockers.append({
                    "request_id": str(
                        request.get("request_id") or ""
                    ) if isinstance(request, Mapping) else "",
                    "station_id": str(
                        (request.get("scope") or {}).get("station_id") or ""
                    ) if isinstance(request, Mapping) and isinstance(
                        request.get("scope"), Mapping
                    ) else "",
                    "event_date": str(
                        (request.get("scope") or {}).get("event_date") or ""
                    ) if isinstance(request, Mapping) and isinstance(
                        request.get("scope"), Mapping
                    ) else "",
                    "reason": f"{type(exc).__name__}:{exc}",
                })
            assert_process_memory_budget()
        return {
            "schema": "bayes_basket_target_post_fill_recompute_batch_v1",
            "state": "BLOCKED_FAIL_CLOSED" if blockers else "COMMITTED",
            "request_count": len(requests),
            "applied_count": len(results),
            "results": results,
            "blockers": blockers,
            "places_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }

    def _assert_post_fill_candidate_projection(
        self,
        *,
        candidate: Mapping[str, Any],
        request: Mapping[str, Any],
        transition_content_sha256: str,
        transition_filled_size: float,
        transition_filled_notional_usdc: float,
    ) -> None:
        """Prove that a recompute consumed the authoritative post-fill q.

        The receipt transition versions are event-level identities.  Resident
        market ingress folds all current transition rows into aggregate
        inventory and order-state snapshots, whose versions intentionally
        differ from the event-level versions.  A recompute is therefore
        admissible only when both aggregate snapshots retain the exact
        transition hash in lineage and the projected bucket position includes
        at least the authoritative fill.
        """

        if self.commit_conn is None:
            raise ContractError(
                "post_fill_recompute_projection_writer_missing"
            )
        market = candidate.get("production_market_snapshot")
        if not isinstance(market, Mapping):
            raise ContractError(
                "post_fill_recompute_market_snapshot_missing"
            )
        inventory_sha = str(market.get("inventory_sha256") or "")
        order_state_sha = str(market.get("order_state_sha256") or "")
        if not inventory_sha or not order_state_sha:
            raise ContractError(
                "post_fill_recompute_state_content_address_missing"
            )
        inventory_row = self.commit_conn.execute(
            "SELECT inventory_json FROM "
            "basket_confirmed_inventory_snapshot WHERE content_sha256=?",
            (inventory_sha,),
        ).fetchone()
        order_state_row = self.commit_conn.execute(
            "SELECT order_state_json FROM basket_order_state_snapshot "
            "WHERE content_sha256=?",
            (order_state_sha,),
        ).fetchone()
        if inventory_row is None or order_state_row is None:
            raise ContractError(
                "post_fill_recompute_projected_state_missing"
            )
        try:
            inventory = json.loads(str(inventory_row[0] or ""))
            order_state = json.loads(str(order_state_row[0] or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(
                "post_fill_recompute_projected_state_invalid"
            ) from exc
        if not isinstance(inventory, Mapping) or not isinstance(
            order_state, Mapping
        ):
            raise ContractError(
                "post_fill_recompute_projected_state_invalid"
            )
        source_transition = str(transition_content_sha256 or "").strip()
        inventory_lineage = inventory.get("transition_content_sha256")
        order_state_lineage = order_state.get(
            "transition_content_sha256"
        )
        if (
            not source_transition
            or not isinstance(inventory_lineage, list)
            or source_transition
            not in {str(value) for value in inventory_lineage}
            or not isinstance(order_state_lineage, list)
            or source_transition
            not in {str(value) for value in order_state_lineage}
        ):
            raise ContractError(
                "post_fill_recompute_transition_not_materialized"
            )
        scope = request.get("scope")
        if not isinstance(scope, Mapping):
            raise ContractError("post_fill_recompute_scope_missing")
        bucket = str(scope.get("bucket") or "")
        filled_by_bucket = inventory.get("filled_by_bucket")
        filled_cost_by_bucket = inventory.get("filled_cost_by_bucket")
        if (
            not bucket
            or not isinstance(filled_by_bucket, Mapping)
            or not isinstance(filled_cost_by_bucket, Mapping)
        ):
            raise ContractError(
                "post_fill_recompute_inventory_projection_invalid"
            )
        try:
            projected_size = float(filled_by_bucket.get(bucket, 0.0))
            projected_notional = float(
                filled_cost_by_bucket.get(bucket, 0.0)
            )
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "post_fill_recompute_inventory_projection_invalid"
            ) from exc
        if projected_size + 1e-9 < float(transition_filled_size):
            raise ContractError(
                "post_fill_recompute_filled_size_not_projected"
            )
        if projected_notional + 1e-9 < float(
            transition_filled_notional_usdc
        ):
            raise ContractError(
                "post_fill_recompute_filled_notional_not_projected"
            )

    def _materialize_market_evidence(self, activation: Mapping[str, Any]) -> dict[str, Any]:
        return self._materialize_market_evidence_from_manifest(
            activation,
            manifest_path=self.market_manifest_path,
        )

    def _materialize_market_evidence_from_manifest(
        self,
        activation: Mapping[str, Any],
        *,
        manifest_path: Path,
        full_materialization: bool = True,
    ) -> dict[str, Any]:
        if self.commit_conn is None:
            return {
                "state": "not_attempted",
                "reason": "target_atomic_writer_not_injected",
                "writes_target_ssot": False,
            }
        remaining_budget = _resolve_target_remaining_budget()
        manifest_path = Path(manifest_path).expanduser().resolve()
        manifest_root, _manifest_quotes = load_official_ws_quotes(
            manifest_path
        )
        station_allowlist = _manifest_station_allowlist(manifest_root)
        manifest_event_date = (
            str(manifest_root.get("target_local_date") or "").strip()
            if station_allowlist
            else ""
        )
        if station_allowlist and not manifest_event_date:
            raise RuntimeError("target_market_manifest_event_date_missing")
        evidence_cache_receipts = [
            materialize_official_ws_evidence_cache(
                self.commit_conn,
                manifest_path=manifest_path,
                station_id=station_id,
                event_date=manifest_event_date,
                commit=True,
            )
            for station_id in station_allowlist
        ] if station_allowlist else [
            materialize_official_ws_evidence_cache(
                self.commit_conn,
                manifest_path=manifest_path,
                commit=True,
            )
        ]
        evidence_cache = {
            "state": "committed",
            "station_allowlist": list(station_allowlist),
            "receipts": evidence_cache_receipts,
        }
        if not full_materialization:
            return {
                "state": "evidence_cache_only",
                "writes_target_ssot": True,
                "full_materialization": False,
                "station_allowlist": list(station_allowlist),
                "event_date": manifest_event_date,
                "evidence_cache": evidence_cache,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        try:
            resident_receipts = [
                materialize_resident_no_live_market_evidence(
                    self.commit_conn,
                    manifest_path=manifest_path,
                    activation_path=self.activation_path,
                    remaining_budget_usdc=remaining_budget,
                    policy_authority_scope=str(activation["policy_authority_scope"]),
                    station_id=station_id,
                    event_date=manifest_event_date,
                    materialize_ws_cache=False,
                )
                for station_id in station_allowlist
            ] if station_allowlist else [
                materialize_resident_no_live_market_evidence(
                    self.commit_conn,
                    manifest_path=manifest_path,
                    activation_path=self.activation_path,
                    remaining_budget_usdc=remaining_budget,
                    policy_authority_scope=str(activation["policy_authority_scope"]),
                    materialize_ws_cache=False,
                )
            ]
            result = {
                "state": "committed",
                "station_allowlist": list(station_allowlist),
                "receipts": resident_receipts,
            }
        except Exception as exc:
            return {
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
                "writes_target_ssot": True,
                # Preserve the already registry-verified scope even when a
                # downstream readiness gate (for example cash authority)
                # fails.  Dropping it here caused the outer worker to replace
                # the real blocker with target_market_station_allowlist_scope_required.
                "station_allowlist": list(station_allowlist),
                "event_date": manifest_event_date,
                "evidence_cache": evidence_cache,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        return {
            "state": "committed",
            "writes_target_ssot": True,
            "station_allowlist": list(station_allowlist),
            "event_date": manifest_event_date,
            "market_evidence": result,
            "evidence_cache": evidence_cache,
        }

    @staticmethod
    def _ws_scope_catalog_summary(
        catalog: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Name every scope the actionable filter drops, and why.

        The filter that selects actionable scopes is a silent one: a scope
        that fails it simply does not appear downstream, so a station whose
        probability expired is indistinguishable from one that was never
        published.  This is the only place those scopes are reported.
        """

        blocked: list[dict[str, str]] = []
        for item in catalog:
            fresh = item.get("probability_evidence_fresh") is True
            blocker = str(item.get("scope_blocker") or "")
            if fresh and not blocker:
                continue
            blocked.append({
                "scope_key": str(item.get("scope_key") or ""),
                "station_id": str(item.get("station_id") or ""),
                "event_date": str(item.get("event_date") or ""),
                "event_day_state": str(item.get("event_day_state") or ""),
                "probability_stage": str(item.get("probability_stage") or ""),
                "reason": (
                    blocker or "resident_target_ws_probability_stale"
                ),
            })
        return {
            "scope_count": len(catalog),
            "actionable_scope_count": len(catalog) - len(blocked),
            "blocked_scope_count": len(blocked),
            "blocked_scopes": sorted(
                blocked, key=lambda row: row["scope_key"]
            ),
        }

    def _resident_ws_scope_catalog(self) -> list[dict[str, Any]]:
        """Verify and expose each bounded resident WS scope in memory."""

        root = load_bounded_json(self.market_manifest_path)
        if not isinstance(root, Mapping):
            raise ContractError("resident_target_ws_manifest_mapping_required")
        if str(root.get("schema") or "") != MULTI_SCOPE_WS_MANIFEST_SCHEMA:
            # Legacy one-scope manifests remain readable during the cutover.
            manifest = dict(root)
            station_allowlist = _manifest_station_allowlist(manifest)
            event_date = str(
                manifest.get("target_local_date") or ""
            ).strip()
            if not station_allowlist or not event_date:
                raise ContractError(
                    "resident_target_ws_legacy_scope_invalid"
                )
            return [{
                "scope_key": f"{station_allowlist[0]}|{event_date}",
                "station_id": station_allowlist[0],
                "event_date": event_date,
                "event_day_state": "open",
                "probability_stage": "",
                "manifest": manifest,
            }]
        if (
            root.get("state") != "connected"
            or root.get("source")
            != "official_polymarket_clob_websocket"
            or root.get("evidence_only") is not True
            or root.get("authorizes_order") is not False
            or root.get("calls_order_gateway") is not False
            or root.get("live_submit_authorized") is not False
            or int(root.get("orders_submitted", -1)) != 0
            or not _manifest_content_sha_matches(root)
        ):
            raise ContractError(
                "resident_target_ws_multi_scope_boundary_invalid"
            )
        summaries = root.get("scopes")
        nested_by_scope = root.get("scope_manifests")
        if (
            not isinstance(summaries, list)
            or not isinstance(nested_by_scope, Mapping)
            or not 1 <= len(summaries) <= MAX_RESIDENT_WS_SCOPES
            or len(nested_by_scope) != len(summaries)
        ):
            raise ContractError(
                "resident_target_ws_multi_scope_cardinality_invalid"
            )
        catalog: list[dict[str, Any]] = []
        seen: set[str] = set()
        for summary in summaries:
            if not isinstance(summary, Mapping):
                raise ContractError(
                    "resident_target_ws_scope_summary_invalid"
                )
            scope_key = str(summary.get("scope_key") or "").strip()
            station_id = str(summary.get("station_id") or "").strip()
            event_date = str(summary.get("event_date") or "").strip()
            day_state = str(
                summary.get("event_day_state") or ""
            ).strip()
            stage = str(
                summary.get("probability_stage") or ""
            ).strip()
            probability_evidence_fresh = summary.get(
                "probability_evidence_fresh"
            )
            nested = nested_by_scope.get(scope_key)
            nested_stations = (
                _manifest_station_allowlist(nested)
                if isinstance(nested, Mapping)
                else ()
            )
            if (
                not scope_key
                or scope_key in seen
                or scope_key != f"{station_id}|{event_date}"
                or day_state not in {"open", "not_started"}
                or not isinstance(nested, Mapping)
                or nested.get("schema")
                != "clob_ws_latest_quote_snapshot_v1"
                or nested.get("state") != "connected"
                or nested.get("source")
                != "official_polymarket_clob_websocket"
                or nested.get("evidence_only") is not True
                or nested.get("authorizes_order") is not False
                or nested.get("calls_order_gateway") is not False
                or nested.get("live_submit_authorized") is not False
                or int(nested.get("orders_submitted", -1)) != 0
                or str(nested.get("scope_key") or "") != scope_key
                or str(nested.get("event_day_state") or "") != day_state
                or str(nested.get("target_local_date") or "")
                != event_date
                or nested_stations != (station_id,)
                or str(
                    (nested.get("probability_authority") or {}).get(
                        "stage"
                    )
                ) != stage
                or probability_evidence_fresh not in {True, False}
                or (
                    nested.get("probability_authority") or {}
                ).get("evidence_fresh") is not probability_evidence_fresh
                or str(summary.get("scope_manifest_sha256") or "")
                != str(nested.get("snapshot_sha256") or "")
                or not _manifest_content_sha_matches(nested)
            ):
                raise ContractError(
                    "resident_target_ws_scope_binding_invalid"
                )
            if day_state == "not_started" and stage not in {"P0", "P0-M", ""}:
                raise ContractError(
                    "resident_target_ws_future_stage_forbidden"
                )
            scope_blocker = ""
            if day_state == "not_started" and not stage:
                # A rootless listing-day scope: subscribed at listing (charter
                # 48) so its book records from the first quote, while no
                # probability claim exists yet to preselect from.  It must be
                # observational, not fatal -- treating it as a forbidden stage
                # killed this whole catalog on the first rootless scope, which
                # silenced every healthy scope's preselection and open-day
                # decision for as long as one unrooted listing existed.
                scope_blocker = "resident_target_ws_future_scope_rootless"
            elif day_state == "not_started" and probability_evidence_fresh is not True:
                # A stale future scope is observational only.  Keep it in the
                # catalog for auditability, but do not let one station-day
                # suppress healthy open scopes or create a future P0 action.
                scope_blocker = "resident_target_ws_future_probability_stale"
            if day_state == "open" and stage not in {"P0", "P0-M", "P1", ""}:
                raise ContractError(
                    "resident_target_ws_open_scope_probability_stage_invalid"
                )
            if day_state == "open" and not stage:
                # A rootless open scope: the market trades but no probability
                # root ever compiled for it (a listing-day subscription whose
                # station never landed a packet).  Same doctrine as the
                # rootless future scope above -- keep recording its book,
                # block decisions on it alone, never let it kill the catalog.
                scope_blocker = "resident_target_ws_open_scope_rootless"
            if (
                day_state == "open"
                and stage in {"P0", "P0-M"}
                and probability_evidence_fresh is not True
            ):
                # Keep the blocker attached to this station-day.  The worker
                # below excludes it from the actionable scope set while
                # allowing other fresh scopes to proceed independently.
                scope_blocker = (
                    "resident_target_ws_open_scope_pre_p1_probability_stale"
                )
            if (
                day_state == "open"
                and stage == "P1"
                and probability_evidence_fresh is not True
            ):
                # A stale open P1 is excluded by the freshness test either
                # way, but without a blocker it left no trace at all: it is
                # not a not-started scope, so it raises no preliminary
                # decision, and the actionable filter drops it silently.  A
                # station whose P1 expired then looks identical to one that
                # was never scanned.
                scope_blocker = (
                    "resident_target_ws_open_scope_p1_probability_stale"
                )
            seen.add(scope_key)
            catalog.append({
                "scope_key": scope_key,
                "station_id": station_id,
                "event_date": event_date,
                "event_day_state": day_state,
                "probability_stage": stage,
                "probability_evidence_fresh": probability_evidence_fresh,
                "scope_blocker": scope_blocker,
                "manifest": dict(nested),
            })
        return sorted(catalog, key=lambda item: item["scope_key"])

    @staticmethod
    def _ws_scope_manifest_age_sec(
        scope: Mapping[str, Any],
        *,
        now: datetime,
    ) -> float:
        """How old this scope's book is, by the clock the compile will use.

        ``compile_p0_preselection`` rejects a manifest whose
        ``connection_observed_at`` is further than the performance budget's
        ``max_ws_snapshot_age_sec`` from the evaluation instant.  Measuring
        the same quantity here is what lets the sweep re-read *before* that
        check can fail rather than discovering it forty scopes in.

        An unreadable or unclocked stamp returns infinity: re-read rather
        than compile against a manifest whose age cannot be established.
        """

        nested = scope.get("manifest")
        observed = ""
        if isinstance(nested, Mapping):
            observed = str(nested.get("connection_observed_at") or "")
        if not observed:
            return float("inf")
        try:
            parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        except ValueError:
            return float("inf")
        if parsed.tzinfo is None:
            return float("inf")
        return (now - parsed).total_seconds()

    @staticmethod
    def _forward_preselection_order(
        scope_keys: Sequence[str],
        *,
        resume_at: str,
    ) -> list[str]:
        """The catalog's own order, rotated to where the last pass stopped.

        Every scope still appears exactly once -- this moves where a pass
        begins, not what it covers.  It matters only when a pass does not
        reach the end: without it the survivors are always the same head of
        the alphabet, and the stations behind the truncation point are
        starved every cycle rather than served on the next one.
        """

        order = list(scope_keys)
        if resume_at in order:
            start = order.index(resume_at)
            order = order[start:] + order[:start]
        return order

    @staticmethod
    def _forward_preselection_scope_skipped(scope: Any) -> bool:
        """Whether this scope has no forward preselection to make right now."""

        return (
            not isinstance(scope, Mapping)
            or scope.get("event_day_state") != "not_started"
            or not scope.get("probability_evidence_fresh")
            or bool(scope.get("scope_blocker"))
        )

    @staticmethod
    def _scope_input_fingerprint(scope: Mapping[str, Any]) -> tuple[str, str]:
        """What this scope's answer depends on: its probability and its book.

        Returned as a pair so the two can be compared separately -- a new
        envelope changes the answer, a moved book only may -- rather than as
        one hash that would make every quote tick look like new evidence.
        """

        nested = scope.get("manifest")
        if not isinstance(nested, Mapping):
            return ("", "")
        envelope_id = str(
            (nested.get("probability_authority") or {}).get("envelope_id")
            or ""
        )
        observed = ""
        quotes = nested.get("quotes_by_token")
        if isinstance(quotes, Mapping):
            observed = max(
                (
                    str(quote.get("book_observed_at") or "")
                    for quote in quotes.values()
                    if isinstance(quote, Mapping)
                ),
                default="",
            )
        return (envelope_id, observed)

    @staticmethod
    def _warm_window_weight(
        scope: Mapping[str, Any],
        *,
        as_of_utc: str,
    ) -> float:
        """How likely this station-day's high is still being set right now.

        Measured, not assumed: across 1022 observed station-days the daily
        high landed between 10:00 and 17:00 local 80% of the time, peaking at
        15:00 but only 17.6% there -- so the signal is the window, not a
        distance from any one hour.  Inside it an arriving observation can
        still change the answer and the weight is 1; outside it decays with
        hours from the nearest edge and reaches almost nothing overnight.

        An unreadable timezone weighs low rather than raising: the rotation
        orders work, and refusing to order one scope would hold up every
        scope behind it.
        """

        nested = scope.get("manifest")
        timezone_name = (
            str(nested.get("target_timezone") or "")
            if isinstance(nested, Mapping)
            else ""
        )
        if not timezone_name:
            return 0.05
        try:
            local = datetime.fromisoformat(
                str(as_of_utc).replace("Z", "+00:00")
            ).astimezone(ZoneInfo(timezone_name))
        except (ValueError, KeyError, ZoneInfoNotFoundError):
            return 0.05
        hour = local.hour + local.minute / 60.0
        # Not a flat window.  A plateau gave every station between 10:00 and
        # 17:00 the identical score, so thirty-one of them tied at 1.0 and
        # the ordering fell through to the tie-break, which is the scope key
        # -- the alphabet, again, with a station at 10:09 ahead of one at
        # 15:09.  The measured distribution is not flat either: of 1022
        # observed station-days the high landed at 15:00 seven times more
        # often than at 10:00, so the hour's own share of highs is the
        # weight, interpolated between the measured points and floored so a
        # quiet station still has a score for ageing to lift.
        share = _warm_hour_share(hour)
        return max(0.05, share / _WARM_HOUR_PEAK_SHARE)

    @classmethod
    def _scope_rotation_key(
        cls,
        scope: Mapping[str, Any],
        *,
        as_of_utc: str,
        last_served: Mapping[str, Any] | None,
        quota_usdc: float | None,
    ) -> tuple[int, float, float, str]:
        """Order scopes by what recompiling one now is actually worth.

        The rotation hands out scarce compute, and every serve costs a
        compile plus the SQLite writes behind it -- the store crossed its
        size ceiling once already today -- so the ordering is by expected
        value of the recompute, not by name.  It was the scope key, which is
        the ICAO code: on a live catalog all thirteen stations then inside
        their warm window sorted to positions 13-27 because Asia's codes
        begin with R, W and Z, while the small hours took positions 0-8.

        One ordering above the score, because it is not tradable against
        anything: the event day under way precedes the day not started.  A
        day being decided now cannot wait for one being planned.

        A market never compiled is not a tier -- see below -- it is maximal
        new evidence, which is what it actually is.

        Within a tier, the score multiplies what would be gained:

        - *new evidence*: a fresh probability envelope changes the answer, a
          moved book may, and neither means the recompute reproduces the plan
          it already has.  This is the term that matters most, because it is
          the one that makes the rotation skip stations with nothing to say.
        - *warm window*: whether the high is still being set locally.
        - *actionable capital*: a station the allocator funded can act on
          what it finds; one it could not fund to a whole basket cannot.

        Ties break to the longest unserved, which is also why the caller
        tracks serve times per station rather than advancing a cursor over
        positions -- the ordering is recomputed every cycle, so a positional
        cursor visits positions, not stations.
        """

        day_state = str(scope.get("event_day_state") or "")
        # The day being decided leads the day being planned; that is the only
        # ordering strong enough to override the score.  "Never compiled" is
        # deliberately *not* a tier of its own: at a cold start every scope
        # is unseen, so a tier would flatten them all and hand the ordering
        # straight back to the scope key -- the alphabetical behaviour this
        # replaced.  It enters the score instead, as maximal new evidence,
        # which is exactly what it is.
        tier = 0 if day_state == "open" else 1

        envelope_id, observed_at = cls._scope_input_fingerprint(scope)
        if last_served is None:
            evidence = 1.0
        elif envelope_id and envelope_id != str(
            last_served.get("envelope_id") or ""
        ):
            evidence = 1.0
        elif observed_at and observed_at > str(
            last_served.get("book_observed_at") or ""
        ):
            evidence = 0.5
        else:
            # Nothing moved.  Recompiling reproduces the same plan and pays
            # the full write cost for it; keep it possible, not preferred.
            evidence = 0.1

        actionable = (
            1.0
            if quota_usdc is None or float(quota_usdc) > 0.0
            else 0.2
        )
        # Ageing is multiplicative, and it is what makes "nobody starves" a
        # guarantee rather than a hope.  Breaking ties by serve time does
        # nothing here because the scores are continuous and almost never
        # equal: a station inside its warm window scored 0.1 against 0.027
        # for one at dawn, and simply kept winning.  Growing the score with
        # time waited means a quiet station rises until it is served -- in
        # roughly three hours at that ratio -- so a position there is still
        # revisited even on a day when nothing about it changes.
        waited_hours = 0.0
        served_at = str((last_served or {}).get("served_at_utc") or "")
        if served_at:
            try:
                waited_hours = max(
                    0.0,
                    (
                        datetime.fromisoformat(
                            str(as_of_utc).replace("Z", "+00:00")
                        )
                        - datetime.fromisoformat(
                            served_at.replace("Z", "+00:00")
                        )
                    ).total_seconds()
                    / 3600.0,
                )
            except ValueError:
                waited_hours = 0.0
        ageing = 1.0 + min(waited_hours, ROTATION_AGEING_CAP_HOURS)
        score = (
            evidence
            * cls._warm_window_weight(scope, as_of_utc=as_of_utc)
            * actionable
            * ageing
        )
        # Ascending sort: lower tier first, then higher score.  The scope key
        # only ever settles an exact tie, so the ordering stays deterministic
        # without the alphabet deciding anything that matters.
        return (tier, -score, 0.0, str(scope.get("scope_key") or ""))

    @staticmethod
    def _starved_event_dates(
        scopes: Sequence[Mapping[str, Any]],
        *,
        served: Mapping[str, Mapping[str, Any]],
        as_of_utc: str,
    ) -> set[str]:
        """Open event days whose last capture is older than the bound.

        A day never captured at all counts as starved from the moment it has
        a scope, which is the case this exists for: a day opens, its P1 stage
        starts speaking, and the ordering has no reason to prefer it because
        every one of its stations is at local midnight.
        """

        try:
            now = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
        except ValueError:
            return set()
        latest: dict[str, str] = {}
        present: set[str] = set()
        for scope in scopes:
            day = str(scope.get("event_date") or "")
            if not day or str(scope.get("event_day_state") or "") != "open":
                continue
            present.add(day)
            stamp = str(
                (served.get(str(scope.get("scope_key") or "")) or {})
                .get("served_at_utc") or ""
            )
            if stamp and stamp > latest.get(day, ""):
                latest[day] = stamp
        starved: set[str] = set()
        for day in present:
            stamp = latest.get(day)
            if not stamp:
                starved.add(day)
                continue
            try:
                served_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                starved.add(day)
                continue
            if (now - served_at).total_seconds() > EVENT_DAY_CAPTURE_BOUND_SEC:
                starved.add(day)
        return starved

    def _sweep_scope_manifests(
        self,
        open_scopes: Sequence[Mapping[str, Any]],
        *,
        selected: Mapping[str, Any],
        as_of_utc: str,
    ) -> list[Mapping[str, Any]]:
        """The scope manifests this cycle should ingest, best first.

        All of them, in the priority order, bounded only by the sweep cap.
        The cap is not a throughput choice -- 45 stations of inline books is
        a bounded document either way -- it is the same fail-closed habit as
        every other resource guard here: a catalog that somehow grew past the
        station-day ceiling should stop, not swell the write.

        Priority still decides, but now it decides who is ingested first
        within one cycle rather than who waits minutes for a turn.  The
        selected scope leads regardless, because the code downstream reads
        its stage and freshness to decide how much to materialize.
        """

        ordered = [dict(selected)]
        seen = {str(selected.get("scope_key") or "")}
        quotas = (
            getattr(self, "_portfolio_allocation_last", None) or {}
        ).get("quota_usdc_by_station")
        served = getattr(self, "_resident_scope_served", None) or {}
        for scope in sorted(
            (dict(item) for item in open_scopes),
            key=lambda item: self._scope_rotation_key(
                item,
                as_of_utc=as_of_utc,
                last_served=served.get(str(item.get("scope_key") or "")),
                quota_usdc=(
                    quotas.get(str(item.get("station_id") or ""))
                    if isinstance(quotas, Mapping)
                    else None
                ),
            ),
        ):
            key = str(scope.get("scope_key") or "")
            if key in seen:
                continue
            if str(scope.get("event_date") or "") != str(
                selected.get("event_date") or ""
            ):
                # One event day per ingress; a scope from another day waits
                # for a cycle whose selection lands on that day.
                continue
            seen.add(key)
            ordered.append(scope)
            if len(ordered) >= MAX_FORWARD_P0_STATION_DAYS:
                break
        return [
            item["manifest"]
            for item in ordered
            if isinstance(item.get("manifest"), Mapping)
        ]

    def _select_resident_open_ws_scope(
        self,
        open_scopes: Sequence[Mapping[str, Any]],
        *,
        as_of_utc: str | None = None,
    ) -> dict[str, Any] | None:
        """Serve the station-day closest to deciding, without starving any.

        The rotation used to be the scope key's alphabetical order, which is
        the ICAO code, which has nothing to do with when a station-day is
        settled.  Measured on a live catalog it was actively perverse: every
        one of the thirteen stations then within three hours of its warmest
        hour sorted into positions 13-27, because they are the Asian codes and
        Asia begins with R, W and Z -- while positions 0-8 went to stations at
        one and two in the morning, thirteen hours from anything happening.

        The basket is a bet on the day's high, so information arrives while a
        station is warming and is nearly worthless before that.  Scopes are
        therefore ordered by how close local time is to the warm hours, and
        the cursor still advances through the whole ordering, so a station
        that is far from its peak is served later rather than never.
        """

        if not open_scopes:
            return None
        if len(open_scopes) > MAX_FORWARD_P0_STATION_DAYS:
            raise ContractError(
                "resident_target_ws_open_scope_station_day_cap_exceeded"
            )
        as_of = as_of_utc or utc_now()
        served = getattr(self, "_resident_scope_served", None)
        if served is None:
            served = {}
            self._resident_scope_served = served
        quotas = (
            getattr(self, "_portfolio_allocation_last", None) or {}
        ).get("quota_usdc_by_station")
        # One ingress serves one event day (the sweep filters to the selected
        # day), so a day that never wins the ordering is not merely served
        # late -- it is not captured at all.  That is what happened on
        # 2026-08-15: through the 16:00Z hour every one of the 136 captures
        # went to 08-15 and 08-16 got none, which is precisely the hour
        # 08-16's local day opens across Asia and its P1 stage first speaks.
        # The ordering was right to prefer 08-15 -- 08-16's stations were at
        # their own local midnight, the coldest point of the warm-window
        # curve -- but the P1 decisions landed in that hour regardless, and
        # with no book within thirty minutes of them, twelve stations
        # produced no basket for the whole day.
        #
        # So the guarantee is at the day level, where the starvation is, and
        # it only fires once a day has actually been starved.  Scoring is
        # untouched: on any cycle where no open day is overdue this picks
        # exactly what it picked before.
        candidates = [dict(item) for item in open_scopes]
        overdue = self._starved_event_dates(
            candidates, served=served, as_of_utc=as_of
        )
        eligible = [
            item for item in candidates
            if str(item.get("event_date") or "") in overdue
        ] or candidates
        selected = min(
            eligible,
            key=lambda item: self._scope_rotation_key(
                item,
                as_of_utc=as_of,
                last_served=served.get(str(item.get("scope_key") or "")),
                quota_usdc=(
                    quotas.get(str(item.get("station_id") or ""))
                    if isinstance(quotas, Mapping)
                    else None
                ),
            ),
        )
        envelope_id, observed_at = self._scope_input_fingerprint(selected)
        # Recorded per scope, not as a position: the ordering is rebuilt from
        # current time and current evidence every cycle, so a positional
        # cursor would march through positions while the stations under them
        # changed.  Remembering what each scope was last served with is what
        # makes "nothing moved since" answerable at all.
        served[str(selected.get("scope_key") or "")] = {
            "served_at_utc": as_of,
            "envelope_id": envelope_id,
            "book_observed_at": observed_at,
        }
        if len(served) > MAX_FORWARD_P0_STATION_DAYS * 2:
            for key in sorted(
                served, key=lambda k: str(served[k].get("served_at_utc") or "")
            )[: len(served) - MAX_FORWARD_P0_STATION_DAYS]:
                served.pop(key, None)
        return selected

    @contextmanager
    def _temporary_scope_manifest(
        self,
        value: Mapping[str, Any],
    ) -> Iterator[Path]:
        """Bridge one verified in-memory scope to existing path consumers."""

        assert_process_memory_budget()
        with tempfile.TemporaryDirectory(
            prefix="bayes-target-ws-scope-",
        ) as temporary_root:
            path = Path(temporary_root) / "manifest.json"
            write_bounded_json(path, dict(value))
            try:
                yield path
            finally:
                # TemporaryDirectory removes the only duplicate immediately;
                # no quote history or failed fragment is retained.
                assert_process_memory_budget()

    def _materialize_resident_p1(
        self,
        *,
        as_of_utc: str,
        freshness_seconds: int = 3600,
    ) -> dict[str, Any]:
        """Append resident official new-high P1 through the unique compiler."""

        if self.canonical_source_conn is None:
            return {
                "state": "not_configured",
                "reason": self.canonical_source_error
                or "resident_canonical_source_not_configured",
                "writes_target_ssot": False,
            }
        if self.commit_conn is None:
            return {
                "state": "not_attempted",
                "reason": "target_atomic_writer_not_injected",
                "writes_target_ssot": False,
            }
        assert_process_memory_budget()
        rows = load_started_registry_rows(
            self.commit_conn,
            as_of_utc=as_of_utc,
        )
        probability_scopes = {
            (
                str(row[0] or "").strip().upper(),
                str(row[1] or "").strip(),
            )
            for row in self.commit_conn.execute(
                "SELECT DISTINCT station_id,event_date "
                "FROM basket_probability_envelope "
                "WHERE state='COMMITTED' AND decision_at_utc<=? "
                "AND stage IN ('P0','P0-M','P1')",
                (as_of_utc,),
            ).fetchall()
            if str(row[0] or "").strip()
            and str(row[1] or "").strip()
        }
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            station = str(
                row.get("station_id") or ""
            ).strip().upper()
            day = str(row.get("event_date") or "").strip()
            timezone_name = str(
                row.get("settlement_timezone") or ""
            ).strip()
            if (
                station
                and day
                and timezone_name
                and (station, day) in probability_scopes
                and self._event_day_open(
                    day,
                    settlement_timezone=timezone_name,
                    as_of_utc=as_of_utc,
                )
            ):
                grouped.setdefault((station, day), []).append(dict(row))
        # Keyed by (station, day), so this counts station-days.  Capping it at
        # the station count made the whole P1 pass fail the moment a second
        # event day was open.
        if len(grouped) > MAX_FORWARD_P0_STATION_DAYS:
            raise RuntimeError("resident_p1_station_day_scope_cap_exceeded")
        receipts: list[dict[str, Any]] = []
        blockers: list[dict[str, str]] = []
        for (station, day), registry_rows in sorted(grouped.items()):
            try:
                expected_source_lane = target_registry_source_lane(
                    self.commit_conn,
                    station_id=station,
                    event_date=day,
                )
                observations = load_resident_formal_observations(
                    self.canonical_source_conn,
                    registry_rows=registry_rows,
                    station_id=station,
                    event_date=day,
                    as_of_utc=as_of_utc,
                )
                # The observations come from the read-only canonical archive,
                # but the revision facts P1's lineage must name live in the
                # target SSOT.  Resolved here, against the same connection the
                # neighbouring registry lookup uses, rather than by handing the
                # loader a second database: the loader's job is the strict
                # as-of read of the archive, and nothing else on that path
                # needs SSOT access.
                observations = attach_truth_observation_revisions(
                    self.commit_conn,
                    observations,
                    station_id=station,
                    as_of_utc=as_of_utc,
                )
                if not observations:
                    continue
                envelopes = load_materialized_probability_envelopes(
                    self.commit_conn,
                    station_id=station,
                    event_date=day,
                    evaluation_at_utc=as_of_utc,
                    expected_source_lane=expected_source_lane,
                )
                prediction_root, predecessor, consumed, rebasing = (
                    select_p1_replay_base(
                    envelopes,
                    as_of_utc=as_of_utc,
                    )
                )
                source_lanes = {
                    str(item.get("source_lane") or "").strip().lower()
                    for item in observations
                    if str(item.get("source_lane") or "").strip()
                }
                if len(source_lanes) != 1:
                    raise ContractError(
                        "resident_p1_source_lane_ambiguous"
                    )
                if source_lanes != {expected_source_lane}:
                    raise ContractError(
                        "resident_p1_source_lane_registry_mismatch"
                    )
                local_hour = datetime.fromisoformat(
                    as_of_utc.replace("Z", "+00:00")
                ).astimezone(
                    ZoneInfo(prediction_root.settlement_timezone)
                ).hour
                local_time_bucket = (
                    "NIGHT"
                    if local_hour < 6
                    else "MORNING"
                    if local_hour < 12
                    else "AFTERNOON"
                    if local_hour < 18
                    else "EVENING"
                )
                maturity_bin = "D0"
                trajectory_row = (
                    resolve_active_probability_evidence_artifact(
                        self.commit_conn,
                        artifact_kind=(
                            "METAR_TRAJECTORY_LIKELIHOOD"
                        ),
                        station_id=station,
                        scope_key=metar_trajectory_scope_key(
                            source_lane=next(iter(source_lanes)),
                            season_bin=str(
                                prediction_root.lineage.get(
                                    "season_bin"
                                )
                                or ""
                            ),
                            local_time_bucket=local_time_bucket,
                            maturity_bin=maturity_bin,
                            settlement_definition=str(
                                prediction_root.lineage.get(
                                    "settlement_definition"
                                )
                                or ""
                            ),
                        ),
                        as_of_utc=as_of_utc,
                        required_authority_scope="FORMAL_PROMOTED",
                    )
                )
                trajectory_artifact = (
                    None
                    if trajectory_row is None
                    else MetarTrajectoryLikelihoodArtifact.from_mapping(
                        trajectory_row["artifact"]
                    )
                )
                # A missing or pre-curve artifact degrades P1, it does not
                # stop it.  Requiring the hourly-curve successor before any
                # P1 could run inverted the codebase's own cutover rule --
                # the incumbent stays active until the successor passes its
                # gate -- and the arithmetic made the outage open-ended: the
                # gate needs 20 effective folds, curve-bearing roots exist
                # only from 2026-08-03, and folds need settled truth, so no
                # station could promote before late August.  P1 emitted
                # nothing after 2026-08-01.
                #
                # The cost was not just the learned update.  P1 is the only
                # writer of the reported-high hard bound into the atomic
                # chain, so while it was dark the system kept pricing buckets
                # the day's own observations had already excluded.  The
                # compiler's identity-LR fallback prices nothing it cannot
                # defend -- likelihood one everywhere, official running high
                # still hard -- which is exactly the incumbent production ran
                # on through 2026-08-01.  A promoted curve artifact takes
                # over the moment one exists.
                p1_trajectory_mode = p1_trajectory_mode_for_artifact(
                    trajectory_artifact,
                    hourly_curve_method_version=(
                        METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION
                    ),
                )
                packets = compile_station_p1_packets(
                    predecessor=predecessor,
                    prediction_root=prediction_root,
                    observations=observations,
                    registry_rows=registry_rows,
                    compilation_at_utc=as_of_utc,
                    freshness_seconds=freshness_seconds,
                    decision_floor_at_utc=(
                        str(
                            self.commit_conn.execute(
                                "SELECT MAX(decision_at_utc) "
                                "FROM basket_probability_envelope "
                                "WHERE station_id=? AND event_date=? "
                                "AND stage='P1' AND state='COMMITTED' "
                                "AND decision_at_utc<=?",
                                (station, day, as_of_utc),
                            ).fetchone()[0]
                            or ""
                        )
                        or None
                    ),
                    consumed_observation_ids=consumed,
                    trajectory_artifact=trajectory_artifact,
                    maturity_bin=maturity_bin,
                )
                if not packets:
                    # No new reported high, so the distribution has nothing
                    # to change -- but the *staleness gate* measures time
                    # since evidence was last examined, not time since the
                    # answer last moved.  On a hot night the midnight report
                    # can stand as the day's high for ten hours (ZUUU
                    # 2026-08-13: 31C at 00:06 local, ten morning reports
                    # under it), and in hard-bound-only mode nothing else
                    # re-rolls P1, so a perfectly current envelope aged past
                    # its grace and the event morning went unservable.  When
                    # the newest P1 has lapsed its own cadence and newer
                    # observations were examined without beating the high,
                    # append a reaffirmation: identical probabilities, new
                    # decision clock, lineage naming what was examined.  At
                    # most one per freshness window, and only ever off real
                    # observations -- an empty morning reaffirms nothing.
                    reaffirmed = False
                    if (
                        predecessor is not None
                        and getattr(predecessor, "stage", None) == "P1"
                        and observations
                        and not predecessor.is_fresh(as_of_utc)
                    ):
                        base = predecessor.to_dict()
                        newest_obs = max(
                            str(row.get("observation_id") or "")
                            for row in observations
                        )
                        lineage = {
                            **base.get("lineage", {}),
                            "reaffirmation_of_checkpoint_id": (
                                predecessor.checkpoint_id
                            ),
                            "reaffirmed_observation_count": str(
                                len(observations)
                            ),
                            "reaffirmed_through_observation_id": newest_obs,
                        }
                        fresh_until = (
                            datetime.fromisoformat(
                                as_of_utc.replace("Z", "+00:00")
                            )
                            + timedelta(seconds=freshness_seconds)
                        ).isoformat(timespec="milliseconds").replace(
                            "+00:00", "Z"
                        )
                        checkpoint = "p1_" + content_sha256({
                            "reaffirmation_of": predecessor.checkpoint_id,
                            "through_observation": newest_obs,
                            "decision_at_utc": as_of_utc,
                        })[:32]
                        envelope = ProbabilityEnvelope.from_mapping({
                            **base,
                            "checkpoint_id": checkpoint,
                            "decision_at_utc": as_of_utc,
                            "fresh_until_utc": fresh_until,
                            "lineage": lineage,
                        })
                        with _writer_transaction(
                            self.commit_conn,
                            savepoint="resident_p1_reaffirm",
                        ):
                            write = insert_probability_envelope(
                                self.commit_conn,
                                envelope,
                                created_at_utc=as_of_utc,
                            )
                        reaffirmed = bool(write.get("wrote"))
                    receipts.append({
                        "station_id": station,
                        "event_date": day,
                        "state": (
                            "REAFFIRMED_NO_NEW_HIGH"
                            if reaffirmed
                            else "NO_NEW_REPORTED_HIGH"
                        ),
                        "observation_count": len(observations),
                        "p1_update_count": 0,
                        "trajectory_mode": p1_trajectory_mode,
                    })
                    continue
                final_envelope = ProbabilityEnvelope.from_mapping(
                    packets[-1]["expected_envelope"]
                )
                # A delayed official new-high remains part of the immutable
                # P1 replay history even when its decision-time freshness has
                # already elapsed.  Refusing this append leaves the same
                # observation perpetually unconsumed and blocks every later
                # cycle.  Persist the deterministic cursor; the independent
                # stage selector still rejects stale P1 for basket decisions
                # and cannot silently downgrade to P0.
                current_consumable = final_envelope.is_servable(as_of_utc)
                with _writer_transaction(
                    self.commit_conn,
                    savepoint="resident_p1_scope",
                ):
                    writes = []
                    for packet in packets:
                        envelope = ProbabilityEnvelope.from_mapping(
                            packet["expected_envelope"]
                        )
                        proof = dict(packet["trajectory_proof"])
                        insert_probability_update_proof(
                            self.commit_conn,
                            {
                                "proof_id": proof["proof_id"],
                                "proof_kind": (
                                    "METAR_TRAJECTORY_UPDATE"
                                ),
                                "station_id": station,
                                "event_date": day,
                                "settlement_timezone": (
                                    envelope.settlement_timezone
                                ),
                                "prediction_root_checkpoint_id": (
                                    prediction_root.checkpoint_id
                                ),
                                "artifact_id_or_none": (
                                    proof["artifact_id"]
                                ),
                                "activation_id_or_none": (
                                    proof["activation_id"]
                                ),
                                "canonical_sequence_sha256_or_none": (
                                    proof[
                                        "canonical_observation_sequence_sha256"
                                    ]
                                ),
                                "decision_at_utc": (
                                    envelope.decision_at_utc
                                ),
                                "proof": proof,
                                "content_sha256": (
                                    proof["content_sha256"]
                                ),
                            },
                        )
                        writes.append(
                            insert_probability_envelope(
                                self.commit_conn,
                                envelope,
                                created_at_utc=str(
                                    packet["created_at_utc"]
                                ),
                            )
                        )
                write_count = sum(
                    1 for write in writes if write.get("wrote") is True
                )
                receipts.append({
                    "station_id": station,
                    "event_date": day,
                    "state": (
                        "COMMITTED"
                        if write_count
                        else "ALREADY_COMMITTED"
                    ),
                    "observation_count": len(observations),
                    "p1_update_count": write_count,
                    "trajectory_mode": p1_trajectory_mode,
                    "p0_rebase": rebasing,
                    "current_consumable": current_consumable,
                    "replay_cursor_only": not current_consumable,
                    "p1_checkpoint_ids": [
                        str(
                            packet["expected_envelope"]["checkpoint_id"]
                        )
                        for packet in packets
                    ],
                    "writes": writes,
                })
            except Exception as exc:
                blockers.append({
                    "station_id": station,
                    "event_date": day,
                    "reason": f"{type(exc).__name__}:{exc}",
                })
            assert_process_memory_budget()
        return {
            "schema": "bayes_basket_target_resident_p1_ingress_v1",
            # A correction/revision blocker halts only that station-day.  If
            # other scopes committed successfully, preserve their P1 chain
            # and surface the isolated halt as PARTIAL_FAIL_CLOSED; a wholly
            # blocked pass remains globally blocked.
            "state": (
                "PARTIAL_FAIL_CLOSED"
                if blockers and receipts
                else "BLOCKED_FAIL_CLOSED"
                if blockers
                else "COMMITTED"
            ),
            "scope_count": len(grouped),
            "probability_scope_count": len(probability_scopes),
            "updated_scope_count": sum(
                1
                for item in receipts
                if item["state"] == "COMMITTED"
            ),
            "p1_update_count": sum(
                int(item.get("p1_update_count") or 0)
                for item in receipts
            ),
            "receipts": receipts,
            "blockers": blockers,
            "writes_target_ssot": any(
                int(item.get("p1_update_count") or 0) > 0
                for item in receipts
            ),
            "places_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }

    def _materialize_resident_p0m(
        self,
        *,
        as_of_utc: str,
    ) -> dict[str, Any]:
        """Consume a bounded forward-only TAF inbox before resident P1."""

        if self.commit_conn is None:
            return {
                "state": "not_attempted",
                "reason": "target_atomic_writer_not_injected",
                "writes_target_ssot": False,
            }
        if not self.taf_artifact_path.is_file() or not self.taf_inbox_path.is_file():
            return {
                "state": "not_configured",
                "reason": "resident_taf_artifact_or_inbox_missing",
                "writes_target_ssot": False,
            }
        artifact_raw = load_bounded_json(self.taf_artifact_path)
        inbox = load_bounded_json(self.taf_inbox_path)
        if not isinstance(artifact_raw, Mapping) or not isinstance(inbox, Mapping):
            raise ContractError("resident_taf_input_invalid")
        if inbox.get("schema") != "bayes_basket_target_taf_revision_inbox_v1":
            raise ContractError("resident_taf_inbox_schema_invalid")
        try:
            taf_fact_ingress = ingest_forward_taf_inbox(
                self.commit_conn,
                inbox,
            )
            # A genuinely new issuance should reach the basket now, not at
            # the next scheduled capture.  Clearing the capture throttle for
            # the named scopes lets the next production pass rebuild their
            # packets immediately -- the model archives are reused from the
            # retained files within TTL, so the rebuild only refreshes the
            # TAF entry and recompiles P0 on it.
            taf_arrival_wakes = []
            capture_attempts = getattr(
                self, "_official_native_capture_attempts", None
            )
            pending = getattr(self, "_taf_arrival_pending", None)
            if pending is None:
                pending = set()
                self._taf_arrival_pending = pending
            for scope in taf_fact_ingress.get("fresh_scopes") or ():
                scope_key = (
                    str(scope.get("station_id") or ""),
                    str(scope.get("event_date") or ""),
                )
                pending.add(scope_key)
                taf_arrival_wakes.append(scope_key)
                if capture_attempts is not None:
                    capture_attempts.pop(scope_key, None)
            taf_fact_ingress = {
                **taf_fact_ingress,
                "arrival_recompile_wakes": [
                    {"station_id": station, "event_date": day}
                    for station, day in taf_arrival_wakes
                ],
            }
            # The sibling point system rides the same stage.  Its lane is
            # forward-only accumulation -- each pass projects whichever noon
            # anchors have newly passed, and settled days become OOS folds on
            # their own -- so a failure here is one missed pass, never a
            # reason to block the TAF ingest it shares the stage with.
            try:
                if self.canonical_source_conn is None:
                    projection_receipt: dict[str, Any] = {
                        "state": "not_attempted",
                        "reason": "canonical_source_conn_missing",
                    }
                else:
                    with _writer_transaction(
                        self.commit_conn,
                        savepoint="metar_physical_projection",
                    ):
                        projection_receipt = (
                            project_metar_physical_member_runs(
                                self.commit_conn,
                                archive_conn=self.canonical_source_conn,
                                as_of_utc=as_of_utc,
                            )
                        )
            except Exception as exc:  # noqa: BLE001 - stage isolation
                projection_receipt = {
                    "state": "failed",
                    "reason": f"{type(exc).__name__}:{exc}"[:200],
                }
            taf_fact_ingress = {
                **taf_fact_ingress,
                "metar_physical_projection": projection_receipt,
            }
        except ContractError as exc:
            if str(exc) != "forward_taf_capture_content_conflict":
                raise
            return {
                "schema": "bayes_basket_target_resident_taf_ingress_v1",
                "state": "BLOCKED_FAIL_CLOSED",
                "taf_fact_ingress": {
                    "state": "BLOCKED_FAIL_CLOSED",
                    "reason": f"ContractError:{exc}",
                    "wrote_count": 0,
                },
                "receipts": [],
                "blockers": [{
                    "reason": f"ContractError:{exc}",
                }],
                "writes_target_ssot": False,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        if artifact_raw.get("schema") == TAF_ARTIFACT_REGISTRY_SCHEMA:
            # Registry-v1 is a canonical, nonempty envelope around exact-scope
            # activated artifacts.  An empty or ambiguous registry must never
            # turn real TAF receipts into a silent no-op.
            artifacts = validate_taf_artifact_registry(artifact_raw)
        else:
            # Legacy one-artifact files remain valid for their exact station
            # only; they must never become an implicit fallback for peers.
            artifacts = [TafRevisionArtifact.from_mapping(artifact_raw)]
        invalid_compatibility = [
            artifact
            for artifact in artifacts
            if (
                artifact.nwp_live_compatibility_contract_version is None
                or artifact.nwp_live_compatibility_fingerprint_sha256 is None
                or artifact.nwp_live_compatibility_evidence_sha256 is None
                or not artifact.nwp_live_compatibility_validated
            )
        ]
        if invalid_compatibility:
            return {
                "schema": "bayes_basket_target_resident_taf_ingress_v1",
                "state": "BLOCKED_FAIL_CLOSED",
                "receipts": [],
                "blockers": [
                    {
                        "station_id": artifact.station_id,
                        "event_date": "",
                        "reason": (
                            "ContractError:"
                            "resident_taf_nwp_live_compatibility_bridge_missing"
                        ),
                    }
                    for artifact in invalid_compatibility
                ],
                "taf_fact_ingress": taf_fact_ingress,
                "writes_target_ssot": False,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        raw_captures = inbox.get("captures")
        if not isinstance(raw_captures, list) or len(raw_captures) > 256:
            raise ContractError("resident_taf_inbox_capture_count_invalid")
        captures_by_scope: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for raw_capture in raw_captures:
            if not isinstance(raw_capture, Mapping):
                raise ContractError("resident_taf_capture_invalid")
            supplied_sha = str(raw_capture.get("content_sha256") or "")
            identity = {
                key: value
                for key, value in raw_capture.items()
                if key != "content_sha256"
            }
            if supplied_sha != content_sha256(identity):
                raise ContractError("resident_taf_capture_content_sha_mismatch")
            boundary = raw_capture.get("boundary")
            if (
                raw_capture.get("source_class")
                != "official_forward_native_receipt"
                or not isinstance(boundary, Mapping)
                or boundary.get("native_local_receipt") is not True
                or boundary.get("retrospective_replay_only") is not False
            ):
                raise ContractError("resident_taf_forward_receipt_required")
            revision = ForecastRevision.from_mapping(
                raw_capture.get("forecast_revision")
            )
            if revision.received_at_utc > as_of_utc:
                continue
            captures_by_scope.setdefault(
                (revision.station_id, revision.event_date),
                [],
            ).append(raw_capture)
        receipts: list[dict[str, Any]] = []
        blockers: list[dict[str, str]] = []
        ignored_inactive_scope_count = 0
        ignored_no_artifact_scope_count = 0
        for (station, day), captures in sorted(captures_by_scope.items()):
            try:
                station_artifacts = [
                    item
                    for item in artifacts
                    if item.station_id == station
                ]
                if not station_artifacts:
                    ignored_no_artifact_scope_count += 1
                    receipts.append({
                        "station_id": station,
                        "event_date": day,
                        "state": "IGNORED_NO_EXACT_TAF_ARTIFACT",
                        "write_count": 0,
                        "writes": [],
                    })
                    continue
                scope_exists = self.commit_conn.execute(
                    "SELECT 1 FROM basket_probability_envelope "
                    "WHERE station_id=? AND event_date=? "
                    "AND state='COMMITTED' AND stage='P0' "
                    "AND decision_at_utc<=? LIMIT 1",
                    (station, day, as_of_utc),
                ).fetchone()
                if scope_exists is None:
                    ignored_inactive_scope_count += 1
                    receipts.append({
                        "station_id": station,
                        "event_date": day,
                        "state": "IGNORED_NO_P0_SCOPE",
                        "write_count": 0,
                        "writes": [],
                    })
                    continue
                envelopes = load_materialized_probability_envelopes(
                    self.commit_conn,
                    station_id=station,
                    event_date=day,
                    evaluation_at_utc=as_of_utc,
                )
                roots = [
                    item for item in envelopes
                    if item.stage == "P0" and item.is_fresh(as_of_utc)
                ]
                if not roots:
                    ignored_inactive_scope_count += 1
                    receipts.append({
                        "station_id": station,
                        "event_date": day,
                        "state": "IGNORED_STALE_P0_SCOPE",
                        "write_count": 0,
                        "writes": [],
                    })
                    continue
                # A stale historical P0 scope is a no-op and must not need a
                # current market registry shard.  Once a fresh P0 can feed a
                # P0-M write, resolve the authoritative lane and re-read the
                # complete chain through the P1 gate before using it.
                expected_source_lane = target_registry_source_lane(
                    self.commit_conn,
                    station_id=station,
                    event_date=day,
                )
                envelopes = load_materialized_probability_envelopes(
                    self.commit_conn,
                    station_id=station,
                    event_date=day,
                    evaluation_at_utc=as_of_utc,
                    expected_source_lane=expected_source_lane,
                )
                roots = [
                    item for item in envelopes
                    if item.stage == "P0" and item.is_fresh(as_of_utc)
                ]
                if not roots:
                    raise ContractError("resident_p0_scope_changed_during_lane_gate")
                root = roots[-1]
                exact_artifacts = [
                    item
                    for item in station_artifacts
                    if (
                        root.settlement_timezone
                        == item.settlement_timezone
                        and str(root.lineage.get("season_bin") or "")
                        == item.season_bin
                        and taf_scope_lead_bucket(root.lineage)
                        == item.lead_bucket
                        and str(root.lineage.get("run_cycle") or "")
                        == item.run_cycle
                    )
                ]
                if not exact_artifacts:
                    ignored_no_artifact_scope_count += 1
                    receipts.append({
                        "station_id": station,
                        "event_date": day,
                        "state": "IGNORED_NO_EXACT_TAF_ARTIFACT",
                        "write_count": 0,
                        "writes": [],
                    })
                    continue
                if len(exact_artifacts) != 1:
                    raise ContractError(
                        "resident_taf_artifact_exact_scope_ambiguous"
                    )
                artifact = exact_artifacts[0]
                centers_raw = json.loads(
                    str(root.lineage.get("bucket_centers_c_json") or "")
                )
                if not isinstance(centers_raw, Mapping):
                    raise ContractError("resident_taf_bucket_centers_missing")
                revisions = validate_forecast_revision_chain(
                    capture["forecast_revision"] for capture in captures
                )
                capture_by_id = {
                    str(capture["forecast_revision"]["revision_id"]): capture
                    for capture in captures
                }
                taf_confirmed = [
                    item for item in envelopes
                    if item.stage == "P0-M"
                    and str(
                        item.lineage.get("p0_root_checkpoint_id") or ""
                    ) == root.checkpoint_id
                ]
                applied = {
                    str(item.lineage.get("forecast_revision_id") or "")
                    for item in taf_confirmed
                }
                applied_by_revision: dict[str, ProbabilityEnvelope] = {}
                for item in taf_confirmed:
                    revision_id = str(
                        item.lineage.get("forecast_revision_id") or ""
                    )
                    if not revision_id:
                        raise ContractError(
                            "resident_taf_revision_lineage_missing"
                        )
                    previous_item = applied_by_revision.get(revision_id)
                    if previous_item is not None and (
                        previous_item.content_sha256 != item.content_sha256
                    ):
                        raise ContractError(
                            "resident_taf_revision_content_conflict"
                        )
                    applied_by_revision[revision_id] = item
                predecessor = taf_confirmed[-1] if taf_confirmed else root
                pending_envelopes: list[
                    tuple[ProbabilityEnvelope, str]
                ] = []
                for revision in revisions:
                    capture = capture_by_id[revision.revision_id]
                    if revision.revision_id in applied:
                        existing_item = applied_by_revision[revision.revision_id]
                        fields = capture.get("taf_fields")
                        if not isinstance(fields, Mapping):
                            raise ContractError("resident_taf_fields_invalid")
                        try:
                            wind_normalized = min(
                                1.0,
                                float(fields["wind_speed_mps"])
                                / artifact.wind_speed_cap_mps,
                            )
                            fingerprint = {
                                "source_object_sha256": revision.source_object_sha256,
                                "tx_c": float(fields["tx_c"]),
                                "cloud_cover_fraction": float(
                                    fields["cloud_cover_fraction"]
                                ),
                                "precipitation_probability": float(
                                    fields["precipitation_probability"]
                                ),
                                "wind_speed_normalized": wind_normalized,
                            }
                        except (KeyError, TypeError, ValueError) as exc:
                            raise ContractError(
                                "resident_taf_fields_invalid"
                            ) from exc
                        existing_fingerprint = {
                            "source_object_sha256": str(
                                existing_item.lineage.get(
                                    "forecast_source_object_sha256"
                                )
                                or ""
                            ),
                            "tx_c": float(
                                existing_item.lineage.get("taf_tx_c") or "nan"
                            ),
                            "cloud_cover_fraction": float(
                                existing_item.lineage.get(
                                    "taf_cloud_cover_fraction"
                                )
                                or "nan"
                            ),
                            "precipitation_probability": float(
                                existing_item.lineage.get(
                                    "taf_precipitation_probability"
                                )
                                or "nan"
                            ),
                            "wind_speed_normalized": float(
                                existing_item.lineage.get(
                                    "taf_wind_speed_normalized"
                                )
                                or "nan"
                            ),
                        }
                        if any(
                            not math.isfinite(float(value))
                            for value in existing_fingerprint.values()
                            if isinstance(value, (int, float))
                        ) or (
                            fingerprint["source_object_sha256"]
                            != existing_fingerprint["source_object_sha256"]
                        ) or any(
                            abs(
                                float(fingerprint[key])
                                - float(existing_fingerprint[key])
                            )
                            > 1e-12
                            for key in (
                                "tx_c",
                                "cloud_cover_fraction",
                                "precipitation_probability",
                                "wind_speed_normalized",
                            )
                        ):
                            raise ContractError(
                                "resident_taf_revision_content_conflict"
                            )
                        continue
                    previous_time = datetime.fromisoformat(
                        predecessor.decision_at_utc.replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                    received_time = datetime.fromisoformat(
                        revision.received_at_utc.replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                    decision_time = max(previous_time, received_time)
                    decision_time = decision_time.replace(microsecond=0)
                    if decision_time <= previous_time:
                        decision_time = previous_time.replace(microsecond=0)
                        decision_time = decision_time + timedelta(seconds=1)
                    decision_at = decision_time.isoformat().replace("+00:00", "Z")
                    if decision_at > as_of_utc:
                        continue
                    proof = compile_taf_revision_proof(
                        predecessor,
                        p0_root=root,
                        forecast_revision=revision,
                        artifact=artifact,
                        bucket_centers_c=centers_raw,
                        taf_fields=capture["taf_fields"],
                        decision_at_utc=decision_at,
                    )
                    envelope = materialize_p0m(
                        predecessor,
                        p0_root=root,
                        taf_revision_artifact=artifact,
                        taf_revision_proof=proof,
                        decision_at_utc=decision_at,
                        fresh_until_utc=root.fresh_until_utc,
                        bridge_sha256=root.bridge_sha256,
                        revision=predecessor.stage == "P0-M",
                        posterior_version="p0m_taf_physical_confirmation_v3",
                        # This stage's own residual table; identity until its
                        # settled history clears the hurdles, and identity
                        # leaves the posterior byte-identical.
                        band_table=load_band_table(conn, stage="P0-M"),
                    )
                    pending_envelopes.append((envelope, decision_at))
                    predecessor = envelope
                writes = []
                with _writer_transaction(
                    self.commit_conn,
                    savepoint="resident_taf_scope",
                ):
                    for envelope, decision_at in pending_envelopes:
                        writes.append(insert_probability_envelope(
                            self.commit_conn,
                            envelope,
                            created_at_utc=decision_at,
                        ))
                write_count = sum(
                    1 for write in writes if write.get("wrote") is True
                )
                receipts.append({
                    "station_id": station,
                    "event_date": day,
                    "state": (
                        "COMMITTED"
                        if write_count
                        else "NO_NEW_TAF_REVISION"
                    ),
                    "write_count": write_count,
                    "writes": writes,
                })
            except Exception as exc:
                blockers.append({
                    "station_id": station,
                    "event_date": day,
                    "reason": f"{type(exc).__name__}:{exc}",
                })
            assert_process_memory_budget()
        return {
            "schema": "bayes_basket_target_resident_taf_ingress_v1",
            "state": "BLOCKED_FAIL_CLOSED" if blockers else "COMMITTED",
            "taf_fact_ingress": taf_fact_ingress,
            "receipts": receipts,
            "blockers": blockers,
            "ignored_inactive_scope_count": ignored_inactive_scope_count,
            "ignored_no_artifact_scope_count":
                ignored_no_artifact_scope_count,
            "writes_target_ssot": any(
                item["write_count"] > 0 for item in receipts
            ),
            "places_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }

    def materialize_resident_p0(
        self,
        *,
        as_of_utc: str,
    ) -> dict[str, Any]:
        """Append fresh forward NWP bundles from the bounded P0 inbox."""

        if self.commit_conn is None:
            return {
                "state": "not_attempted",
                "reason": "target_atomic_writer_not_injected",
                "writes_target_ssot": False,
            }
        if not self.p0_inbox_path.is_file():
            return {
                "state": "not_configured",
                "reason": "resident_p0_inbox_missing",
                "writes_target_ssot": False,
            }
        inbox = load_bounded_json(self.p0_inbox_path)
        if (
            not isinstance(inbox, Mapping)
            or inbox.get("schema")
            != "bayes_basket_target_p0_inbox_v1"
        ):
            raise ContractError("resident_p0_inbox_schema_invalid")
        rows = inbox.get("packets")
        if not isinstance(rows, list) or len(rows) > 256:
            raise ContractError("resident_p0_inbox_packet_count_invalid")
        if not rows:
            return {
                "schema": "bayes_basket_target_resident_p0_ingress_v1",
                "state": "BLOCKED_FAIL_CLOSED",
                "reason": "FAIL_CLOSED_NO_P0:"
                "native_cross_ensemble_activation_missing",
                "receipts": [],
                "blockers": [{
                    "reason": "FAIL_CLOSED_NO_P0:"
                    "native_cross_ensemble_activation_missing",
                }],
                "writes_target_ssot": False,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        receipts: list[dict[str, Any]] = []
        blockers: list[dict[str, str]] = []
        pending_envelopes: list[ProbabilityEnvelope] = []
        pending_proofs: dict[str, dict[str, Any]] = {}
        pending_by_scope_clock: dict[
            tuple[str, str, str], ProbabilityEnvelope
        ] = {}
        for raw in rows:
            try:
                if not isinstance(raw, Mapping):
                    raise ContractError("resident_p0_inbox_row_invalid")
                packet = raw.get("packet")
                if (
                    raw.get("source_class")
                    != "calibrated_native_cross_ensemble"
                    or not isinstance(packet, Mapping)
                ):
                    raise ContractError(
                        "resident_p0_calibrated_native_cross_ensemble_required"
                    )
                if str(packet.get("stage") or "") != "P0":
                    # A confirm-stage (P0-M) compile shares the inbox but
                    # belongs to the P0-M lane, which supplies its own
                    # predecessor and TAF revision chain.  This lane refusing
                    # to skip it turned one confirm packet into a network-wide
                    # P0 materialization outage: the whole ingress fail-closed
                    # on ``materialization_predecessor_missing`` and healthy
                    # P0 packets behind it never materialized.
                    receipts.append({
                        "station_id": str(packet.get("station_id") or ""),
                        "event_date": str(packet.get("event_date") or ""),
                        "state": "SKIPPED_NON_P0_STAGE",
                        "stage": str(packet.get("stage") or ""),
                    })
                    continue
                if (
                    packet.get("probability_pathway")
                    != P0_PROBABILITY_PATHWAY
                ):
                    raise ContractError(
                        "resident_p0_probability_pathway_invalid"
                    )
                if str(raw.get("packet_content_sha256") or "") != content_sha256(packet):
                    raise ContractError(
                        "resident_p0_packet_content_sha_mismatch"
                    )
                envelope = build_probability_envelope(packet)
                lineage = envelope.lineage
                provider = str(
                    lineage.get("native_transport_provider") or ""
                )
                if provider in {
                    "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT",
                    "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT",
                }:
                    pass
                elif (
                    provider == "OPEN_METEO"
                    and str(lineage.get(
                        "aggregator_fallback_p0_eligible"
                    ) or "").lower() == "true"
                    and str(lineage.get(
                        "republisher_fallback_p0_eligible"
                    ) or "").lower() == "true"
                    and str(lineage.get("transport_lineage_class") or "")
                    == "aggregator_republisher"
                    and str(lineage.get(
                        "native_arrival_index_eligible"
                    ) or "").lower() == "false"
                    and str(lineage.get(
                        "native_formal_p0_eligible"
                    ) or "").lower() == "false"
                ):
                    pass
                else:
                    raise ContractError(
                        "resident_p0_native_transport_required"
                    )
                settlement_end = (
                    datetime.fromisoformat(envelope.event_date)
                    .replace(
                        tzinfo=ZoneInfo(
                            envelope.settlement_timezone
                        )
                    )
                    + timedelta(days=1)
                ).astimezone(timezone.utc)
                if settlement_end <= datetime.fromisoformat(
                    as_of_utc.replace("Z", "+00:00")
                ).astimezone(timezone.utc):
                    receipts.append({
                        "station_id": envelope.station_id,
                        "event_date": envelope.event_date,
                        "state": "IGNORED_CLOSED_SETTLEMENT_SCOPE",
                        "wrote": False,
                    })
                    continue
                proof_raw = packet.get("p0_historical_tilt_proof")
                if (
                    not isinstance(proof_raw, Mapping)
                    and envelope.posterior_version
                    == "p0_nwp_historical_meta_anchor_v4"
                ):
                    raise ContractError(
                        "resident_p0_historical_tilt_proof_required"
                    )
                if envelope.stage != "P0":
                    raise ContractError("resident_p0_stage_invalid")
                if envelope.decision_at_utc > as_of_utc:
                    raise ContractError(
                        "resident_p0_future_decision_forbidden"
                    )
                if not envelope.is_fresh(as_of_utc):
                    raise ContractError("resident_p0_stale_forbidden")
                scope_clock = (
                    envelope.station_id,
                    envelope.event_date,
                    envelope.decision_at_utc,
                )
                previous_pending = pending_by_scope_clock.get(scope_clock)
                if previous_pending is not None:
                    if (
                        previous_pending.content_sha256
                        != envelope.content_sha256
                    ):
                        raise ContractError(
                            "resident_p0_scope_content_conflict"
                        )
                    continue
                existing = self.commit_conn.execute(
                    "SELECT checkpoint_id,content_sha256 FROM basket_probability_envelope "
                    "WHERE station_id=? AND event_date=? AND stage='P0' "
                    "AND state='COMMITTED' ORDER BY decision_at_utc DESC "
                    "LIMIT 1",
                    (envelope.station_id, envelope.event_date),
                ).fetchone()
                if existing is not None:
                    existing_checkpoint = str(existing[0] or "")
                    existing_content = str(existing[1] or "")
                    existing_row = self.commit_conn.execute(
                        "SELECT decision_at_utc FROM basket_probability_envelope "
                        "WHERE checkpoint_id=?",
                        (existing_checkpoint,),
                    ).fetchone()
                    existing_decision = str(
                        existing_row[0] or ""
                    ) if existing_row is not None else ""
                    if existing_decision == envelope.decision_at_utc:
                        if existing_content != envelope.content_sha256:
                            existing_envelope = (
                                _stored_probability_envelope(
                                    self.commit_conn,
                                    checkpoint_id=existing_checkpoint,
                                )
                            )
                            if not _p0_bridge_only_semantic_equivalent(
                                existing_envelope,
                                envelope,
                            ):
                                raise ContractError(
                                    "resident_p0_scope_content_conflict"
                                )
                            receipts.append({
                                "station_id": envelope.station_id,
                                "event_date": envelope.event_date,
                                "state": (
                                    "ALREADY_COMMITTED_EQUIVALENT_"
                                    "BRIDGE_UPGRADE"
                                ),
                                "wrote": False,
                                "existing_checkpoint_id": (
                                    existing_checkpoint
                                ),
                            })
                            continue
                        receipts.append({
                            "station_id": envelope.station_id,
                            "event_date": envelope.event_date,
                            "state": "ALREADY_COMMITTED",
                            "wrote": False,
                        })
                        continue
                    if existing_decision > envelope.decision_at_utc:
                        receipts.append({
                            "station_id": envelope.station_id,
                            "event_date": envelope.event_date,
                            "state": "STALE_MODEL_BUNDLE",
                            "wrote": False,
                        })
                        continue
                pending_by_scope_clock[scope_clock] = envelope
                pending_envelopes.append(envelope)
                if isinstance(proof_raw, Mapping):
                    pending_proofs[envelope.checkpoint_id] = dict(
                        proof_raw
                    )
            except Exception as exc:
                blockers.append({
                    "reason": f"{type(exc).__name__}:{exc}",
                })
            assert_process_memory_budget()
        # Validate the complete bounded inbox before its first write.  One
        # malformed packet must not leave unrelated P0 roots committed.
        if not blockers:
            try:
                committed_receipts: list[dict[str, Any]] = []
                with _writer_transaction(
                    self.commit_conn,
                    savepoint="resident_p0_inbox",
                ):
                    for envelope in pending_envelopes:
                        proof = pending_proofs.get(
                            envelope.checkpoint_id
                        )
                        if proof is not None:
                            insert_probability_update_proof(
                                self.commit_conn,
                                {
                                "proof_id": proof["proof_id"],
                                "proof_kind": "P0_HISTORICAL_TILT",
                                "station_id": envelope.station_id,
                                "event_date": envelope.event_date,
                                "settlement_timezone": (
                                    envelope.settlement_timezone
                                ),
                                "prediction_root_checkpoint_id": (
                                    envelope.checkpoint_id
                                ),
                                "artifact_id_or_none": proof[
                                    "meta_artifact_id"
                                ],
                                "activation_id_or_none": proof[
                                    "meta_activation_id"
                                ],
                                "canonical_sequence_sha256_or_none": (
                                    "NONE"
                                ),
                                "decision_at_utc": (
                                    envelope.decision_at_utc
                                ),
                                "proof": proof,
                                "content_sha256": proof[
                                    "content_sha256"
                                ],
                                },
                            )
                        write = insert_probability_envelope(
                            self.commit_conn,
                            envelope,
                            created_at_utc=envelope.decision_at_utc,
                        )
                        committed_receipts.append({
                            "station_id": envelope.station_id,
                            "event_date": envelope.event_date,
                            "state": "COMMITTED",
                            "wrote": bool(write.get("wrote")),
                            "checkpoint_id": envelope.checkpoint_id,
                        })
                receipts.extend(committed_receipts)
            except Exception as exc:
                blockers.append({
                    "reason": f"{type(exc).__name__}:{exc}",
                })
        return {
            "schema": "bayes_basket_target_resident_p0_ingress_v1",
            "state": "BLOCKED_FAIL_CLOSED" if blockers else "COMMITTED",
            "receipts": receipts,
            "blockers": blockers,
            "writes_target_ssot": any(
                item.get("wrote") is True for item in receipts
            ),
            "places_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }

    def _materialize_resident_probability_chain(
        self,
        *,
        as_of_utc: str,
    ) -> dict[str, Any]:
        """Run the only resident probability event chain in stage order.

        The daemon may refresh bounded NWP/TAF inboxes before this method is
        called.  This method alone owns their SSOT handoff order:
        NWP/P0 -> TAF physical confirmation/P0-M -> METAR/SPECI/P1.  Each
        stage remains its own atomic append transaction, while the returned
        receipt makes the order and no-live boundary explicit to the caller.
        """

        p0 = self.materialize_resident_p0(as_of_utc=as_of_utc)
        p0m = self._materialize_resident_p0m(as_of_utc=as_of_utc)
        p1 = self._materialize_resident_p1(as_of_utc=as_of_utc)
        blocked_stages = [
            stage
            for stage, receipt in (("P0", p0), ("P0-M", p0m), ("P1", p1))
            if str(receipt.get("state") or "").upper()
            == "BLOCKED_FAIL_CLOSED"
        ]
        return {
            "schema": "bayes_basket_target_resident_probability_chain_v1",
            "stage_order": ["P0", "P0-M", "P1"],
            "state": (
                "BLOCKED_FAIL_CLOSED" if blocked_stages else "COMMITTED"
            ),
            "blocked_stages": blocked_stages,
            "as_of_utc": as_of_utc,
            "p0": p0,
            "p0m": p0m,
            "p1": p1,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }

    def _materialize_regime_ssot(
        self,
        *,
        as_of_utc: str,
    ) -> dict[str, Any]:
        """Append exact probability companions before any basket compile.

        Regime is derived only from a committed probability envelope and its
        model-envelope lineage. Missing disagreement lineage is a blocker; no
        extrema or direction is synthesized at this boundary.
        """

        if self.conn is None or self.commit_conn is None:
            return {
                "schema": "bayes_basket_target_regime_batch_v1",
                "state": "BLOCKED_FAIL_CLOSED",
                "reason": "target_atomic_writer_not_injected",
                "receipts": [],
                "blockers": [],
                "calls_order_gateway": False,
                "orders_submitted": 0,
            }
        scopes = self.conn.execute(
            "SELECT DISTINCT station_id,event_date "
            "FROM basket_probability_envelope "
            "WHERE state='COMMITTED' AND decision_at_utc<=? "
            "AND fresh_until_utc>? ORDER BY station_id,event_date",
            (as_of_utc, as_of_utc),
        ).fetchall()
        envelopes: list[ProbabilityEnvelope] = []
        blockers: list[dict[str, str]] = []
        for station_id, event_date in scopes:
            try:
                expected_source_lane = target_registry_source_lane(
                    self.conn,
                    station_id=str(station_id),
                    event_date=str(event_date),
                )
                envelopes.extend(load_materialized_probability_envelopes(
                    self.conn,
                    station_id=str(station_id),
                    event_date=str(event_date),
                    evaluation_at_utc=as_of_utc,
                    expected_source_lane=expected_source_lane,
                ))
            except Exception as exc:
                blockers.append({
                    "station_id": str(station_id),
                    "event_date": str(event_date),
                    "reason": f"{type(exc).__name__}:{exc}",
                })
        receipts: list[dict[str, Any]] = []
        # Keep the resident pass resource-bounded without turning a healthy
        # multi-stage 45-station SSOT into a global blocker once the number
        # of committed envelopes exceeds the per-batch guard.  Each chunk is
        # idempotent and transaction-fenced; a later cycle can safely replay
        # the same rows if a transient fault interrupts a chunk.
        for chunk_start in range(
            0,
            len(envelopes),
            MAX_REGIME_ARTIFACTS_PER_PASS,
        ):
            chunk = envelopes[
                chunk_start : chunk_start + MAX_REGIME_ARTIFACTS_PER_PASS
            ]
            assert_process_memory_budget()
            for envelope in chunk:
                try:
                    artifact = build_regime_artifact(envelope)
                    with _writer_transaction(
                        self.commit_conn,
                        savepoint="basket_target_regime_artifact",
                    ):
                        receipt = insert_regime_artifact(
                            self.commit_conn,
                            artifact,
                            created_at_utc=as_of_utc,
                        )
                    receipts.append({
                        **receipt,
                        "station_id": envelope.station_id,
                        "event_date": envelope.event_date,
                        "stage": envelope.stage,
                        "checkpoint_id": envelope.checkpoint_id,
                        "probability_sha256": envelope.content_sha256,
                    })
                except Exception as exc:
                    blockers.append({
                        "station_id": envelope.station_id,
                        "event_date": envelope.event_date,
                        "stage": envelope.stage,
                        "checkpoint_id": envelope.checkpoint_id,
                        "reason": f"{type(exc).__name__}:{exc}",
                    })
        return {
            "schema": "bayes_basket_target_regime_batch_v1",
            "state": "BLOCKED_FAIL_CLOSED" if blockers else "COMMITTED",
            "receipt_count": len(receipts),
            "envelope_count": len(envelopes),
            "chunk_count": (
                (len(envelopes) + MAX_REGIME_ARTIFACTS_PER_PASS - 1)
                // MAX_REGIME_ARTIFACTS_PER_PASS
            ),
            "written_count": sum(
                1 for item in receipts if item.get("wrote") is True
            ),
            "receipts": receipts,
            "blockers": blockers,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }

    def _replay_root_mismatch_reason(
        self,
        *,
        station_id: str,
        event_date: str,
        as_of_utc: str,
    ) -> str | None:
        """Reject consumption of a stale higher-stage branch after P0 refresh."""

        if self.conn is None:
            return "resident_probability_read_only_connection_missing"
        expected_source_lane = target_registry_source_lane(
            self.conn,
            station_id=station_id,
            event_date=event_date,
        )
        envelopes = load_materialized_probability_envelopes(
            self.conn,
            station_id=station_id,
            event_date=event_date,
            evaluation_at_utc=as_of_utc,
            expected_source_lane=expected_source_lane,
        )
        p0_rows = [row for row in envelopes if row.stage == "P0"]
        if not p0_rows:
            return "resident_p0_root_missing"
        latest_p0_at = max(row.decision_at_utc for row in p0_rows)
        latest_p0_rows = [
            row for row in p0_rows if row.decision_at_utc == latest_p0_at
        ]
        if len(latest_p0_rows) != 1:
            return "resident_p0_scope_ambiguous"
        latest_p0 = latest_p0_rows[0]
        # P1 is the final SSOT. Once a current P1 is bound to the latest P0
        # root, an older optional P0-M branch is irrelevant and must not
        # block the decision. Only inspect P0-M when P1 has not taken over.
        for stage, root_key, reason in (
            ("P1", "p0_root_checkpoint_id", "resident_p1_replay_required"),
            ("P0-M", "p0_root_checkpoint_id", "resident_p0m_replay_required"),
        ):
            rows = [row for row in envelopes if row.stage == stage]
            if not rows:
                continue
            latest_at = max(row.decision_at_utc for row in rows)
            latest_rows = [row for row in rows if row.decision_at_utc == latest_at]
            if len(latest_rows) != 1:
                return f"resident_{stage.lower().replace('-', '')}_scope_ambiguous"
            root_id = str(latest_rows[0].lineage.get(root_key) or "")
            if root_id != latest_p0.checkpoint_id:
                return reason
            if stage == "P1":
                return None
        return None

    def _portfolio_quota(
        self,
        *,
        stations: Sequence[str],
        fallback_budget_usdc: float,
        as_of_utc: str,
    ) -> dict[str, float]:
        """Per-station quota from the one bankroll, or the old flat budget.

        Each station's edge and curve spread are measured from its own most
        recent committed plan -- what a dollar bought it last time -- and the
        atomic basket minimum comes from the activated clip grid.  A station
        with no such history is left out of the split and keeps the flat
        budget, so introducing the allocator cannot silently stop a station
        that has never traded from trying.

        Failure is not fatal here: the receipt is evidence, not authority,
        and a book that cannot be split still plans at the configured budget.
        """

        try:
            blocks_by_station, blocks_sha = load_regional_blocks()
            policy, _policy_sha = load_portfolio_capital_policy()
            # What the book already holds decides whether it may hold more.
            # A breached Tier-1 bound stops new baskets and leaves open ones
            # to settle; closing them would realize the loss it is reacting
            # to, at whatever price the book happens to show.
            inventory = build_portfolio_inventory(
                self.commit_conn,
                as_of_utc=as_of_utc,
                blocks_by_station=blocks_by_station,
                policy=policy,
            )
            persist_portfolio_inventory(
                self.commit_conn, inventory, created_at_utc=as_of_utc
            )
            self._portfolio_inventory_last = {
                "open_notional_usdc": inventory["open_notional_usdc"],
                "portfolio_hhi": inventory["portfolio_hhi"],
                "breaches": inventory["breaches"],
                "new_intents_allowed": inventory["new_intents_allowed"],
                "drawdown": inventory["drawdown"],
            }
            if not inventory["new_intents_allowed"]:
                self._portfolio_allocation_last = {
                    "state": "PORTFOLIO_BOUND_BREACHED",
                    "breaches": inventory["breaches"],
                    "new_intents_allowed": False,
                }
                return {station: 0.0 for station in stations}
            pool = self.commit_conn.execute(
                "SELECT balance_usdc FROM basket_target_capital_pool "
                "ORDER BY pool_id LIMIT 1"
            ).fetchone()
            bankroll = float(pool[0]) if pool else 0.0
            clip_row = self.conn.execute(
                "SELECT artifact_json FROM basket_delta_v2_policy_artifact "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            minimum_clip = min(
                json.loads(str(clip_row[0]))["cash_clip_grid"]
            ) if clip_row else 0.0
            edges = self._station_edge_evidence(
                stations=stations, minimum_clip_usdc=float(minimum_clip)
            )
            if not edges or bankroll <= 0.0:
                return {}
            allocation = allocate_portfolio_quota(
                station_edges=edges,
                blocks_by_station=blocks_by_station,
                bankroll_usdc=bankroll,
                risk_fraction=float(policy["portfolio_risk_fraction"]),
                station_cap_fraction=float(policy["station_cap_fraction"]),
            )
            persist_portfolio_allocation(
                self.commit_conn,
                allocation,
                as_of_utc=as_of_utc,
                blocks_content_sha256=blocks_sha,
                created_at_utc=as_of_utc,
            )
            self._portfolio_allocation_last = {
                "funded_station_count": allocation["funded_station_count"],
                "station_count": allocation["station_count"],
                "allocated_usdc": allocation["allocated_usdc"],
                "bankroll_usdc": allocation["bankroll_usdc"],
                "portfolio_sigma_usdc": allocation["portfolio_sigma_usdc"],
                "naive_sigma_usdc": allocation["naive_sigma_usdc"],
                "minimum_bankroll": derive_minimum_bankroll(
                    station_edges=edges
                ),
            }
            return dict(allocation["quota_usdc_by_station"])
        except (ContractError, sqlite3.Error, ValueError, KeyError) as exc:
            self._portfolio_allocation_last = {
                "state": "ALLOCATION_UNAVAILABLE",
                "reason": f"{type(exc).__name__}:{exc}",
                "fallback_budget_usdc": float(fallback_budget_usdc),
            }
            return {}

    def _station_edge_evidence(
        self,
        *,
        stations: Sequence[str],
        minimum_clip_usdc: float,
    ) -> list[dict[str, Any]]:
        """What a dollar bought each station, from its own last plan."""

        rows: list[dict[str, Any]] = []
        for station_id in stations:
            record = self.conn.execute(
                "SELECT plan_json FROM basket_p0_preselection_plan "
                "WHERE station_id=? ORDER BY rowid DESC LIMIT 1",
                (station_id,),
            ).fetchone()
            if record is None:
                continue
            compiled = (
                json.loads(str(record[0])).get("compiled_plan") or {}
            )
            plan = compiled.get("plan") or {}
            solution = plan.get("target_solution") or {}
            probabilities = (
                compiled.get("probability_envelope") or {}
            ).get("probabilities") or {}
            shares = solution.get("target_shares_by_bucket") or {}
            support = plan.get("final_support") or []
            cost = sum(
                float(value)
                for value in (
                    solution.get("target_cost_by_bucket") or {}
                ).values()
            )
            if not shares or not probabilities or not support or cost <= 0.0:
                continue
            scenario = {
                bucket: float(shares.get(bucket) or 0.0) - cost
                for bucket in probabilities
            }
            mean = sum(
                float(probabilities[bucket]) * value
                for bucket, value in scenario.items()
            )
            variance = sum(
                float(probabilities[bucket]) * (value - mean) ** 2
                for bucket, value in scenario.items()
            )
            rows.append({
                "station_id": station_id,
                "edge_per_usdc": mean / cost,
                "sigma_per_usdc": (variance ** 0.5) / cost,
                "minimum_basket_usdc": minimum_clip_usdc * len(support),
            })
        return rows

    def materialize_forward_p0_preselections(
        self,
        *,
        registry_snapshot_path: Path,
        ws_manifest_path: Path,
        event_date: str,
        evaluation_at_utc: str,
        station_id: str | None = None,
    ) -> dict[str, Any]:
        """Compile and append future-day P0 plans through the sole worker.

        This path atomically writes ``basket_p0_preselection_plan`` and its
        zero-execution ``auto_trade_decisions`` authority row.  It never
        creates an intent, reservation, order, or gateway call.
        """

        if self.conn is None:
            return _blocked(
                f"target_ssot_read_only_open_failed:{self.open_error or 'unknown'}",
                event_date=event_date,
            )
        if self.commit_conn is None:
            return _blocked(
                self.commit_error or "target_atomic_writer_not_injected",
                event_date=event_date,
            )
        activation = load_target_activation(self.activation_path)
        self._assert_db_activation(activation)
        scope = activation.get("target_scope")
        if not isinstance(scope, Mapping):
            raise RuntimeError("target_scope_config_missing")
        missing = [key for key in TARGET_SCOPE_KEYS if not str(scope.get(key) or "").strip()]
        if missing:
            raise RuntimeError("target_scope_missing:" + ",".join(missing))
        registry_path = Path(registry_snapshot_path).expanduser().resolve()
        manifest_path = Path(ws_manifest_path).expanduser().resolve()
        if manifest_path == self.market_manifest_path:
            raise RuntimeError("forward_p0_manifest_must_not_be_resident_manifest")
        registry = load_market_registry_snapshot(registry_path)
        manifest, quotes = load_official_ws_quotes(manifest_path)
        stations = latest_visible_p0_station_ids(
            self.conn,
            event_date=event_date,
            evaluation_at_utc=evaluation_at_utc,
        )
        if station_id is not None:
            requested_station = str(station_id or "").strip().upper()
            if not requested_station:
                raise RuntimeError(
                    "forward_p0_station_scope_invalid"
                )
            stations = [
                value
                for value in stations
                if value == requested_station
            ]
        if not stations:
            raise RuntimeError("forward_p0_probability_scope_empty")
        if len(stations) > MAX_FORWARD_P0_STATIONS:
            raise RuntimeError("forward_p0_station_scope_cap_exceeded")
        assert_process_memory_budget()
        planning_budget_usdc = _resolve_target_remaining_budget()
        # The bankroll is one book.  Handing every station the whole of it --
        # which is what the configured budget did, forty-five times over the
        # same 20 USDC -- only failed to over-commit because almost nothing
        # traded.  The allocator splits it, charging stations under one air
        # mass as a single position, and a station it cannot fund to a whole
        # atomic basket gets nothing rather than an amount that buys nothing.
        quota_by_station = self._portfolio_quota(
            stations=stations,
            fallback_budget_usdc=planning_budget_usdc,
            as_of_utc=evaluation_at_utc,
        )
        compiled = []
        for station_id in stations:
            station_budget = float(
                quota_by_station.get(station_id, planning_budget_usdc)
            )
            # An unfunded station is compiled at zero budget rather than
            # skipped.  The two things this call produces are not the same
            # kind of thing: the plan is a trading decision, but the market
            # snapshot it commits is *evidence*, and evidence must be
            # recorded whether or not the book can afford to act on it.
            #
            # Skipping cost us the whole forward-day book.  Measured
            # 2026-08-15: the pool holds 20 USDC, the allocator's risk
            # fraction leaves 7 to deploy, and the smallest whole atomic
            # basket needs 11.43 -- so every station returned zero, every
            # station was skipped, and not one 2026-08-16 quote reached
            # basket_official_ws_snapshot even though the publisher had all
            # 319 tokens live.  Quotes are ephemeral; a forward day dropped
            # here is gone for good, and with it the ability to audit or
            # replay what the listing window actually offered.
            #
            # Zero budget is a legal planning budget (only negative or
            # non-finite is rejected), so the trading side stays fail-closed
            # by arithmetic: nothing is affordable, no intent is written.
            compiled.append(compile_p0_preselection(
                self.conn,
                station_id=station_id,
                event_date=event_date,
                evaluation_at_utc=evaluation_at_utc,
                registry_snapshot=registry,
                ws_manifest=manifest,
                ws_quotes=quotes,
                season_bin=str(scope["season_bin"]),
                maturity_bin=str(scope["maturity_bin"]),
                liquidity_bin=str(scope["liquidity_bin"]),
                strategy_version=str(scope["strategy_version"]),
                planning_budget_usdc=max(0.0, station_budget),
            ))
            assert_process_memory_budget()
        assert_connection_target_capacity(
            self.commit_conn,
            recover_oversized_wal=True,
        )
        with _writer_transaction(
            self.commit_conn,
            savepoint="basket_target_p0_worker_batch",
        ):
            receipts = []
            decision_receipts = []
            for item in compiled:
                preselection_receipt = commit_p0_preselection(
                    self.commit_conn,
                    item,
                    created_at_utc=evaluation_at_utc,
                )
                receipts.append(preselection_receipt)
                # Shadow-score the approved log-wealth objective next to the
                # committed plan: same buckets, same prices, same budget, no
                # authority.  Its own savepoint -- a scoring defect must never
                # cost a preselection.
                try:
                    self.commit_conn.execute(
                        "SAVEPOINT shadow_objective_score"
                    )
                    try:
                        shadow_compiled = item["compiled_plan"]
                        bankroll_row = self.commit_conn.execute(
                            "SELECT balance_usdc FROM "
                            "basket_target_capital_pool LIMIT 1"
                        ).fetchone()
                        shadow_score = build_shadow_objective_score(
                            station_id=str(item["station_id"]),
                            event_date=str(item["event_date"]),
                            probability_stage=str(
                                item["probability_stage"]
                            ),
                            checkpoint_id=str(item["checkpoint_id"]),
                            decision_at_utc=str(item["decision_at_utc"]),
                            candidate_rows=(
                                shadow_compiled.get(
                                    "order_sizing_decisions"
                                ) or []
                            ),
                            probabilities_by_bucket=(
                                shadow_compiled.get(
                                    "probability_envelope", {}
                                ).get("probabilities") or {}
                            ),
                            budget_usdc=float(
                                item.get("planning_budget_usdc") or 0.0
                            ),
                            bankroll_usdc=float(
                                bankroll_row[0]
                            ) if bankroll_row else 0.0,
                            created_at_utc=evaluation_at_utc,
                        )
                        if shadow_score is not None:
                            persist_shadow_objective_score(
                                self.commit_conn, shadow_score
                            )
                            preselection_receipt["shadow_objective"] = {
                                "growth_actual": shadow_score[
                                    "actual_curve"
                                ]["expected_log_growth"],
                                "growth_shadow": shadow_score[
                                    "shadow_curve"
                                ]["expected_log_growth"],
                            }
                        self.commit_conn.execute(
                            "RELEASE shadow_objective_score"
                        )
                    except Exception as exc:  # noqa: BLE001 -- observational
                        self.commit_conn.execute(
                            "ROLLBACK TO shadow_objective_score"
                        )
                        self.commit_conn.execute(
                            "RELEASE shadow_objective_score"
                        )
                        preselection_receipt["shadow_objective"] = {
                            "state": "SCORING_FAILED",
                            "error": f"{type(exc).__name__}:{exc}",
                        }
                except sqlite3.Error:
                    pass
                # P0 is the opening stage: a forward day is precisely when the
                # ensemble has something to say that the tape does not yet.
                # The quotes here come from the resident official WS book
                # sliced to this scope, so an opening intent is priced against
                # the live market, not a stale forward cache.  Plans that are
                # not compile-ready keep the preliminary no-execution record.
                market_snapshot = item.get(
                    UNPERSISTED_MARKET_SNAPSHOT_KEY
                )
                compiled_plan = item.get("compiled_plan")
                opened = None
                # Charter 46: the preliminary record is only ever allowed
                # *after* a named stage-open failure, never as a gate that
                # runs instead of one.  When the inputs stage-open needs are
                # absent it cannot be attempted, and that absence is itself
                # the blocker -- naming it here is what keeps the record
                # below a receipt rather than a prior refusal.
                if not isinstance(market_snapshot, Mapping):
                    preselection_receipt["stage_open_blocker"] = (
                        "stage_open_not_attempted:market_snapshot_missing"
                    )
                elif not isinstance(compiled_plan, Mapping):
                    preselection_receipt["stage_open_blocker"] = (
                        "stage_open_not_attempted:compiled_plan_missing"
                    )
                if isinstance(market_snapshot, Mapping) and isinstance(
                    compiled_plan, Mapping
                ):
                    # Two savepoints, because two different kinds of thing are
                    # written here.  The evidence bundle is the book as it
                    # stood; the stage open is a trading decision about it.
                    # They used to share one, so every stage-open refusal
                    # rolled the quote back out with it -- and a station whose
                    # opens all refuse then has no book at all.  Measured
                    # 2026-08-16: KORD, KSEA and ZUUU each refused every
                    # 2026-08-17 open on
                    # ``stage_evidence_intent_outside_bound`` -- a legitimate
                    # refusal, the intent sat outside the hard bound -- and
                    # between them wrote zero rows to
                    # basket_official_ws_snapshot for that day while the
                    # publisher held all 11 tokens of each live.  The forward
                    # book is ephemeral: the quote that was rolled back cannot
                    # be fetched again, so the refusal cost the audit, the
                    # replay and the paper ledger a day of market history
                    # apiece.  Same rule the zero-budget compile above already
                    # follows: evidence is recorded whether or not the book can
                    # act on it.
                    #
                    # The stage open still binds to *persisted* rows -- the
                    # forward validation joins the quote by snapshot_id and the
                    # inventory/order state by version -- so the bundle has to
                    # be committed before it, not merely attempted beside it.
                    # A forward day has no resident ingress writing that trio,
                    # and _insert_once makes the write idempotent across
                    # recompiles of the same quote version.
                    quote_raw = dict(market_snapshot)
                    quote_raw[
                        "quote_freshness_policy_sha256"
                    ] = QuoteFreshnessPolicy.from_mapping(
                        activation.get("quote_freshness_policy")
                    ).content_sha256
                    zero_by_bucket = {
                        str(entry.get("bucket") or ""): 0.0
                        for entry in (
                            market_snapshot.get("buckets") or ()
                        )
                    }
                    evidence_scope = {
                        "station_id": str(
                            market_snapshot.get("station_id") or ""
                        ),
                        "event_date": str(
                            market_snapshot.get("event_date") or ""
                        ),
                        "settlement_timezone": str(
                            market_snapshot.get("settlement_timezone") or ""
                        ),
                        "observed_at": str(
                            market_snapshot.get("quote_observed_at") or ""
                        ),
                    }
                    inventory_raw = {
                        **evidence_scope,
                        "inventory_snapshot_id": (
                            "p0presel-inv-"
                            + str(market_snapshot.get("quote_version") or "")
                        ),
                        # The snapshot id already varies per quote -- a new
                        # quote is a new observation, with its own observed_at
                        # -- but the version did not, so every quote added a
                        # row that the reader, which resolves by (station, day,
                        # version), then found two of.  That is the ambiguity
                        # that blocked eleven of thirteen forward scopes; the
                        # version has to move with the observation it names.
                        "inventory_version": _p0_preselection_scope_version(
                            market_snapshot
                        ),
                        "complete": 1,
                        "inventory_json": {
                            "filled_by_bucket": zero_by_bucket,
                        },
                    }
                    order_state_raw = {
                        **evidence_scope,
                        "order_state_snapshot_id": (
                            "p0presel-ord-"
                            + str(market_snapshot.get("quote_version") or "")
                        ),
                        # Same reasoning as the inventory version above: keyed
                        # to the quote it observes.
                        "order_state_version": (
                            _p0_preselection_scope_version(market_snapshot)
                        ),
                        "known": 1,
                        "order_state_json": {
                            "pending_by_bucket": zero_by_bucket,
                        },
                    }
                    # The quote row names the version the reader will look the
                    # inventory up by, so it has to carry the same quote-keyed
                    # value the rows were written under, not the shared one it
                    # was copied with.
                    quote_raw["inventory_version"] = (
                        _p0_preselection_scope_version(market_snapshot)
                    )
                    quote_raw["order_state_version"] = (
                        _p0_preselection_scope_version(market_snapshot)
                    )
                    # Evidence first, in a savepoint of its own.  Set before
                    # the bundle is written, not after: the bundle refuses a
                    # quote whose version does not match the inventory row
                    # beside it, and the assignment sitting below the call left
                    # every scope failing that check.
                    evidence_written = False
                    try:
                        with _writer_transaction(
                            self.commit_conn,
                            savepoint="basket_target_p0_market_evidence",
                        ):
                            insert_market_evidence_bundle(
                                self.commit_conn,
                                quote_raw,
                                inventory_raw,
                                order_state_raw,
                            )
                        evidence_written = True
                    except (ContractError, AtomicLedgerError) as exc:
                        preselection_receipt[
                            "market_evidence_blocker"
                        ] = f"{type(exc).__name__}:{exc}"
                    # The forward binding names the persisted rows by content
                    # hash; recompute them through the same builders the insert
                    # used.
                    quote_raw["inventory_sha256"] = (
                        build_confirmed_inventory_snapshot(
                            inventory_raw
                        )["content_sha256"]
                    )
                    quote_raw["order_state_sha256"] = (
                        build_order_state_snapshot(
                            order_state_raw
                        )["content_sha256"]
                    )
                    if not evidence_written:
                        # The open binds to persisted rows, so without them it
                        # cannot be attempted -- and that absence is the
                        # blocker, named per charter 46 rather than left to the
                        # preliminary record to imply.
                        preselection_receipt["stage_open_blocker"] = (
                            "stage_open_not_attempted:"
                            "market_evidence_not_persisted"
                        )
                    else:
                        # Its own savepoint: a plan that fails partway through
                        # must leave no half-written cycle behind for the
                        # preliminary record that follows it -- and now no
                        # longer takes the book down with it.
                        try:
                            with _writer_transaction(
                                self.commit_conn,
                                savepoint="basket_target_p0_stage_open",
                            ):
                                opened = commit_production_candidate_atomic(
                                    self.commit_conn,
                                    compiled_plan=compiled_plan,
                                    market_snapshot=quote_raw,
                                    awc_observation_contract=None,
                                    truth_observation_revision_content_sha256=(
                                        ""
                                    ),
                                    bound=None,
                                    created_at_utc=evaluation_at_utc,
                                )
                        except (ContractError, AtomicLedgerError) as exc:
                            opened = None
                            preselection_receipt[
                                "stage_open_blocker"
                            ] = f"{type(exc).__name__}:{exc}"
                if opened is not None:
                    # The submission receipt is the only record of what
                    # happened at the exchange boundary -- which intents were
                    # gated, which reached the send and stopped there, and the
                    # payload hash a live run would have to reproduce byte for
                    # byte.  It rides the preselection receipt so it is
                    # readable from the status file without opening the
                    # ledger.
                    submission = opened.get("order_submission")
                    if isinstance(submission, Mapping):
                        preselection_receipt["order_submission"] = {
                            "cycle_id": submission.get("cycle_id"),
                            "intent_count": submission.get("intent_count"),
                            "orders_submitted": submission.get(
                                "orders_submitted"
                            ),
                            "live_submit_authorized": submission.get(
                                "live_submit_authorized"
                            ),
                            "calls_order_gateway": submission.get(
                                "calls_order_gateway"
                            ),
                            "submissions": [
                                {
                                    key: entry.get(key)
                                    for key in (
                                        "order_intent_id",
                                        "state",
                                        "reason",
                                        "payload_sha256",
                                        "exchange_post_started",
                                    )
                                    if key in entry
                                }
                                for entry in (
                                    submission.get("submissions") or ()
                                )
                                if isinstance(entry, Mapping)
                            ][:8],
                        }
                    decision_receipts.append(opened)
                    continue
                decision_receipts.append(
                    commit_preliminary_scope_decision(
                        self.commit_conn,
                        # Every path to here is a stage-open that did not
                        # open, and each one has to say which: raised (the
                        # except branch named it), never attempted (the
                        # guard above named it), or attempted and returned
                        # nothing -- the last had no name, and since the
                        # record now refuses to be written without one, an
                        # unnamed case aborted the whole production cycle
                        # rather than logging a blocked scope.
                        stage_open_blocker=(
                            str(
                                preselection_receipt.get(
                                    "stage_open_blocker"
                                )
                                or ""
                            ).strip()
                            or "stage_open_returned_no_candidate"
                        ),
                        preselection_id=str(item["preselection_id"]),
                        station_id=str(item["station_id"]),
                        event_date=str(item["event_date"]),
                        settlement_timezone=str(
                            item["settlement_timezone"]
                        ),
                        checkpoint_id=str(item["checkpoint_id"]),
                        probability_stage=str(item["probability_stage"]),
                        quote_manifest_sha256=str(
                            preselection_receipt[
                                "quote_manifest_sha256"
                            ]
                        ),
                        preselection_plan_sha256=str(
                            item["plan_sha256"]
                        ),
                        created_at_utc=evaluation_at_utc,
                    )
                )
        return {
            "schema": "bayes_basket_target_worker_p0_batch_v1",
            "state": "COMMITTED",
            "event_date": event_date,
            "portfolio_allocation": getattr(
                self, "_portfolio_allocation_last", None
            ),
            "portfolio_inventory": getattr(
                self, "_portfolio_inventory_last", None
            ),
            "station_count": len(compiled),
            "written_count": sum(1 for item in receipts if item["wrote"]),
            "receipts": receipts,
            "preliminary_decision_count": len(decision_receipts),
            "preliminary_decisions": decision_receipts,
            "write_authority": "TargetProductionWorker",
            "final_authority": "P1",
            "intents_written": 0,
            "reservations_written": 0,
            "places_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }

    def _materialize_delta_v2_labels(
        self,
        *,
        as_of_utc: str,
        enforce_interval: bool = True,
    ) -> dict[str, Any]:
        """Run bounded, independently throttled post-settlement label pages."""

        if self.commit_conn is None:
            return {
                "state": "not_attempted",
                "reason": "target_atomic_writer_not_injected",
                "appended_count": 0,
            }
        if enforce_interval:
            now_monotonic = time.monotonic()
            if (
                self._last_delta_v2_label_scan_monotonic is not None
                and now_monotonic
                - self._last_delta_v2_label_scan_monotonic
                < DELTA_V2_LABEL_SCAN_INTERVAL_SECONDS
            ):
                return {
                    "state": "throttled",
                    "reason": "delta_v2_label_scan_interval",
                    "appended_count": 0,
                }
            self._last_delta_v2_label_scan_monotonic = now_monotonic
        assert_process_memory_budget()
        source_cursor = (
            int(getattr(self, "_delta_v2_label_fact_cursor", 0))
            if bool(
                getattr(
                    self,
                    "_delta_v2_label_cursor_initialized",
                    False,
                )
            )
            else 0
        )
        committed_cursor = source_cursor
        committed_cursor_state = dict(
            getattr(self, "_delta_v2_label_cursor_state", {})
            or {
                "schema": "delta_v2_label_cursor_v2",
                "scan_after_rowid": source_cursor,
                "retry_after_rowid": 0,
                "next_lane": "retry",
            }
        )
        if int(
            committed_cursor_state.get("scan_after_rowid", -1)
        ) != source_cursor:
            committed_cursor_state = {
                "schema": "delta_v2_label_cursor_v2",
                "scan_after_rowid": source_cursor,
                "retry_after_rowid": 0,
                "next_lane": "retry",
            }
        page_results: list[dict[str, Any]] = []
        try:
            with _writer_transaction(
                self.commit_conn,
                savepoint="delta_v2_settlement_labels",
            ):
                for _page_number in range(
                    MAX_DELTA_V2_LABEL_PAGES_PER_PASS
                ):
                    assert_process_memory_budget()
                    page = materialize_delta_v2_settled_training_labels(
                        self.commit_conn,
                        as_of_utc=as_of_utc,
                        after_fact_rowid=committed_cursor,
                        cursor_state=committed_cursor_state,
                        max_facts=MAX_LABEL_BATCH,
                    )
                    page_results.append(dict(page))
                    committed_cursor = int(
                        page.get("source_cursor_after")
                        or committed_cursor
                    )
                    next_cursor_state = page.get("cursor_state_after")
                    if isinstance(next_cursor_state, Mapping):
                        committed_cursor_state = dict(next_cursor_state)
                    else:
                        committed_cursor_state = {
                            "schema": "delta_v2_label_cursor_v2",
                            "scan_after_rowid": committed_cursor,
                            "retry_after_rowid": 0,
                            "next_lane": "retry",
                        }
                    if (
                        page.get("state") == "blocked_fail_closed"
                        or int(page.get("candidate_count") or 0) == 0
                        or not bool(page.get("has_more"))
                    ):
                        break
            self._delta_v2_label_fact_cursor = committed_cursor
            self._delta_v2_label_cursor_state = committed_cursor_state
            self._delta_v2_label_cursor_initialized = True
        except Exception as exc:
            return {
                "schema": LABEL_BATCH_SCHEMA,
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
                "appended_count": 0,
                "forward_settlement_label_sync": {
                    "state": "blocked_fail_closed",
                    "reason": f"{type(exc).__name__}:{exc}",
                },
                "calls_order_gateway": False,
                "orders_submitted": 0,
                "live_submit_authorized": False,
            }
        result = {
            "schema": LABEL_BATCH_SCHEMA,
            "state": (
                "blocked_fail_closed"
                if any(
                    page.get("state") == "blocked_fail_closed"
                    for page in page_results
                )
                else (
                    "committed"
                    if sum(
                        int(page.get("appended_count") or 0)
                        for page in page_results
                    )
                    else "no_new_final_labels"
                )
            ),
            "candidate_count": sum(
                int(page.get("candidate_count") or 0)
                for page in page_results
            ),
            "appended_count": sum(
                int(page.get("appended_count") or 0)
                for page in page_results
            ),
            "blockers": [
                blocker
                for page in page_results
                for blocker in (
                    page.get("blockers")
                    if isinstance(page.get("blockers"), list)
                    else []
                )
            ],
            "source_cursor_before": source_cursor,
            "source_cursor_after": committed_cursor,
            "page_count": len(page_results),
            "page_limit": MAX_DELTA_V2_LABEL_PAGES_PER_PASS,
            "has_more": bool(
                page_results
                and page_results[-1].get("has_more")
                and len(page_results)
                >= MAX_DELTA_V2_LABEL_PAGES_PER_PASS
            ),
            "skipped_open_count": sum(
                int(page.get("skipped_open_count") or 0)
                for page in page_results
            ),
        }
        # The target label table is the append-only authority; the separate
        # v2 store is a replayable projection.  Commit the authority first.
        # A projection failure leaves a retryable lag, never an orphaned store
        # label copied from a source transaction that later rolled back.
        try:
            store_path = (
                DEFAULT_STORE_PATH
                if getattr(self, "cash_phase_forward_store_path", None) is None
                else self.cash_phase_forward_store_path
            )
            store_root = getattr(
                self,
                "cash_phase_forward_store_root",
                DEFAULT_STORE_ROOT,
            )
            store_state = read_forward_store_state(
                store_path,
                allowed_root=store_root,
            )
            sync_pages: list[dict[str, Any]] = []
            for _page_number in range(
                MAX_DELTA_V2_LABEL_PAGES_PER_PASS
            ):
                label_sync = append_forward_settlement_labels(
                    store_path,
                    source_conn=self.commit_conn,
                    expected_generation=int(store_state["generation"]),
                    as_of_utc=as_of_utc,
                    allowed_root=store_root,
                )
                if not isinstance(label_sync, Mapping):
                    raise ContractError(
                        "forward_store_settlement_label_sync_result_invalid"
                    )
                sync_pages.append(dict(label_sync))
                if not bool(label_sync.get("more_pending")):
                    break
                store_state = read_forward_store_state(
                    store_path,
                    allowed_root=store_root,
                )
            label_sync = {
                "state": (
                    "committed"
                    if any(
                        int(page.get("ingested") or 0) > 0
                        or page.get("state") == "committed"
                        for page in sync_pages
                    )
                    else "already_current"
                ),
                "page_count": len(sync_pages),
                "page_limit": MAX_DELTA_V2_LABEL_PAGES_PER_PASS,
                "inserted": sum(
                    int(page.get("inserted") or 0)
                    for page in sync_pages
                ),
                "skipped_ineligible": sum(
                    int(page.get("skipped_ineligible") or 0)
                    for page in sync_pages
                ),
                "ingested": sum(
                    int(page.get("ingested") or 0)
                    for page in sync_pages
                ),
                "more_pending": bool(
                    sync_pages
                    and sync_pages[-1].get("more_pending")
                    and len(sync_pages)
                    >= MAX_DELTA_V2_LABEL_PAGES_PER_PASS
                ),
                "generation": (
                    sync_pages[-1].get("generation")
                    if sync_pages
                    else store_state.get("generation")
                ),
            }
        except Exception as exc:
            return {
                **result,
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
                "source_labels_committed": True,
                "forward_settlement_label_sync": {
                    "state": "blocked_fail_closed",
                    "reason": f"{type(exc).__name__}:{exc}",
                },
                "calls_order_gateway": False,
                "orders_submitted": 0,
                "live_submit_authorized": False,
            }
        return {
            **result,
            "source_labels_committed": True,
            "forward_settlement_label_sync": dict(label_sync),
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        }

    def _materialize_delta_v2_truths(
        self,
        *,
        as_of_utc: str,
    ) -> dict[str, Any]:
        """Freeze bounded registry-timezone truth before label materialization."""

        if self.commit_conn is None:
            return {
                "state": "not_attempted",
                "reason": "target_atomic_writer_not_injected",
                "written_count": 0,
            }
        if self.canonical_source_conn is None:
            return {
                "state": "blocked_fail_closed",
                "reason": self.canonical_source_error
                or "resident_canonical_source_not_configured",
                "written_count": 0,
                "calls_order_gateway": False,
                "orders_submitted": 0,
                "live_submit_authorized": False,
            }
        now_monotonic = time.monotonic()
        last_truth_scan = getattr(
            self,
            "_last_delta_v2_truth_scan_monotonic",
            None,
        )
        if (
            last_truth_scan is not None
            and now_monotonic - last_truth_scan
            < DELTA_V2_LABEL_SCAN_INTERVAL_SECONDS
        ):
            return {
                "state": "throttled",
                "reason": "delta_v2_settlement_scan_interval",
                "written_count": 0,
                "calls_order_gateway": False,
                "orders_submitted": 0,
                "live_submit_authorized": False,
            }
        self._last_delta_v2_truth_scan_monotonic = now_monotonic
        assert_process_memory_budget()
        committed_cursor: int | None = None
        committed_initialization = bool(
            getattr(
                self,
                "_delta_v2_truth_cursor_initialized",
                False,
            )
        )
        try:
            with _writer_transaction(
                self.commit_conn,
                savepoint="delta_v2_settlement_truths",
            ):
                if committed_initialization:
                    source_cursor = int(
                        getattr(
                            self,
                            "_delta_v2_truth_fact_cursor",
                            0,
                        )
                    )
                else:
                    # A resident restart must not spend its first bounded
                    # pass on the oldest historical scopes while the current
                    # 45-station page waits behind them.  Start immediately
                    # before the newest bounded page, then use the normal
                    # end-of-keyset wrap to revisit history.
                    newest_page = self.commit_conn.execute(
                        "SELECT min(source_rowid) FROM ("
                        "SELECT max(rowid) AS source_rowid "
                        "FROM basket_policy_training_fact "
                        "GROUP BY station_id,event_date "
                        "ORDER BY source_rowid DESC LIMIT ?"
                        ")",
                        (MAX_FORWARD_P0_STATIONS,),
                    ).fetchone()
                    first_source_rowid = int(
                        newest_page[0] or 0
                    ) if newest_page is not None else 0
                    source_cursor = max(0, first_source_rowid - 1)
                additional_scopes: list[tuple[str, str]] = []
                candidate_scopes = _candidate_ensemble_scopes_without_truth(
                    self.commit_conn,
                    limit=45 * 4,
                )
                if candidate_scopes:
                    # Bootstrap truth directly from bounded forward member
                    # scope identities.  Without this bridge the first strict
                    # OOS P0 is circular: basket facts require P0, while truth
                    # discovery previously required basket facts.  This is
                    # intentionally source-neutral at the storage boundary:
                    # both tables remain candidate-only until the later
                    # closed-AWC strict-OOS validator accepts their evidence.
                    as_of = datetime.fromisoformat(
                        as_of_utc.replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                    timezone_by_scope: dict[
                        tuple[str, str], set[str]
                    ] = {}
                    for row in candidate_scopes:
                        scope = (
                            str(row[0] or "").strip().upper(),
                            str(row[1] or "").strip(),
                        )
                        timezone_by_scope.setdefault(
                            scope,
                            set(),
                        ).add(str(row[2] or "").strip())
                    settled_scopes: list[tuple[str, str]] = []
                    for scope, timezone_names in sorted(
                        timezone_by_scope.items()
                    ):
                        if len(timezone_names) != 1 or "" in timezone_names:
                            raise ContractError(
                                "native_truth_scope_timezone_not_unique"
                            )
                        window = settlement_day_window(
                            scope[1],
                            next(iter(timezone_names)),
                        )
                        closed_at = datetime.fromisoformat(
                            str(
                                window["target_day_end_utc"]
                            ).replace("Z", "+00:00")
                        ).astimezone(timezone.utc)
                        if closed_at <= as_of:
                            settled_scopes.append(scope)
                    # Rotate rather than always taking the first page of a
                    # station-sorted list.  The candidate set is routinely
                    # larger than one page, and cutting it at a fixed offset
                    # meant the same alphabetical prefix was served every
                    # cycle: stations late in the alphabet were never asked
                    # about at all.  They did not fail any gate -- a read-only
                    # replay of their scopes reaches the write -- they simply
                    # never got a turn, which is indistinguishable from a
                    # source outage in every receipt.
                    if settled_scopes:
                        page = min(45, len(settled_scopes))
                        start = int(
                            getattr(
                                self,
                                "_delta_v2_truth_bootstrap_cursor",
                                0,
                            )
                        ) % len(settled_scopes)
                        additional_scopes = [
                            settled_scopes[
                                (start + offset) % len(settled_scopes)
                            ]
                            for offset in range(page)
                        ]
                        self._delta_v2_truth_bootstrap_cursor = (
                            start + page
                        ) % len(settled_scopes)
                truth_kwargs: dict[str, Any] = {
                    "as_of_utc": as_of_utc,
                    "max_scopes": 45,
                    "after_fact_rowid": source_cursor,
                }
                if additional_scopes:
                    truth_kwargs["additional_scopes"] = additional_scopes
                result = materialize_settled_training_truths(
                    self.commit_conn,
                    self.canonical_source_conn,
                    **truth_kwargs,
                )
                if (
                    result.get("state") == "blocked_fail_closed"
                    and not result.get("advance_safe")
                ):
                    blockers = [
                        item
                        for item in (result.get("blockers") or [])
                        if isinstance(item, Mapping)
                    ]
                    # Name the blocker that actually stopped the batch, not
                    # whichever happened to be first.  An all-availability
                    # batch no longer reaches here, so what remains is a trust
                    # failure -- and reporting an unrelated availability gap
                    # instead sends the reader looking for a missing report
                    # when the real problem is a contradiction.
                    consistency = [
                        item
                        for item in blockers
                        if item.get("blocker_class") != "AVAILABILITY"
                    ]
                    culprit = (consistency or blockers or [{}])[0]
                    raise ContractError(
                        "delta_v2_settlement_truth_blocked:"
                        + str(culprit.get("reason") or "unknown")
                        + ":"
                        + str(culprit.get("station_id") or "?")
                        + "|"
                        + str(culprit.get("event_date") or "?")
                        + f":consistency={len(consistency)}"
                        + f"/total={len(blockers)}"
                    )
                # A batch in which every scope was merely unsettleable is not a
                # system failure, and refusing to advance past it is what turned
                # a source gap into a standstill: 126 station-days with no
                # eligible hourly report filled the batch window from
                # 2026-08-02, every batch came back all-failed, the cursor was
                # never committed, and the same doomed scopes were reselected
                # forever.  No later station-day was ever reached, so settled
                # truth stopped for all 45 stations and P1 with it.
                #
                # The original caution above still holds for the case it was
                # written for -- a transaction that may yet fail must not move
                # the cursor past a page it could roll back.  An all-unavailable
                # batch wrote nothing, so there is no such page.
                committed_cursor = int(
                    result.get("source_cursor_after") or 0
                )
            # The in-memory cursor is only a post-commit optimization.  It
            # must never move while the database transaction can still fail,
            # otherwise a failed COMMIT would skip the rolled-back page.
            self._delta_v2_truth_fact_cursor = int(committed_cursor or 0)
            self._delta_v2_truth_cursor_initialized = True
        except Exception as exc:
            # A failed truth scan must never become a "safe throttle" on the
            # next resident cycle; otherwise an older accepted truth could
            # reach the label writer while the unresolved blocker is merely
            # being rate-limited.
            self._last_delta_v2_truth_scan_monotonic = None
            return {
                "schema": (
                    "bayes_basket_target_resident_settlement_truth_batch_v1"
                ),
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
                "written_count": 0,
                "calls_order_gateway": False,
                "orders_submitted": 0,
                "live_submit_authorized": False,
            }
        return result

    def _run_delta_v2_settlement_maintenance(
        self,
        *,
        as_of_utc: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run closed-day truth/label work outside the current quote lease."""

        truth: dict[str, Any] | None = None
        try:
            truth = self._materialize_delta_v2_truths(
                as_of_utc=as_of_utc,
            )
            if truth.get("state") == "blocked_fail_closed":
                return truth, {
                    "state": "blocked_fail_closed",
                    "reason": "delta_v2_truth_required_before_label",
                    "appended_count": 0,
                    "calls_order_gateway": False,
                    "orders_submitted": 0,
                    "live_submit_authorized": False,
                }
            labels = self._materialize_delta_v2_labels(
                as_of_utc=as_of_utc,
                enforce_interval=True,
            )
            return truth, labels
        except Exception as exc:
            reason = f"{type(exc).__name__}:{exc}"
            return (
                truth
                or {
                    "state": "blocked_fail_closed",
                    "reason": reason,
                    "written_count": 0,
                    "calls_order_gateway": False,
                    "orders_submitted": 0,
                    "live_submit_authorized": False,
                },
                {
                    "state": "blocked_fail_closed",
                    "reason": reason,
                    "appended_count": 0,
                    "calls_order_gateway": False,
                    "orders_submitted": 0,
                    "live_submit_authorized": False,
                },
            )

    def run(self) -> dict[str, Any]:
        if self.conn is None:
            return _blocked(f"target_ssot_read_only_open_failed:{self.open_error or 'unknown'}")
        if self.commit_error is not None:
            return _blocked(self.commit_error)
        if self.commit_conn is None:
            # A writer that was never enabled is not an error, so it leaves
            # ``commit_error`` unset and slips past the guard above with a
            # None connection -- which then reaches the first of a dozen
            # bare ``self.commit_conn.execute`` calls a hundred lines later
            # and surfaces as ``AttributeError: 'NoneType' object has no
            # attribute 'execute'``, naming neither the connection nor the
            # switch.  It cost a wrong lead during the 2026-08-15 stall
            # diagnosis.  Same reason string the forward preselection path
            # already uses for the same condition.
            return _blocked("target_atomic_writer_not_injected")
        try:
            activation = load_target_activation(self.activation_path)
            self._assert_db_activation(activation)
        except Exception as exc:
            return _blocked(f"target_activation_invalid:{type(exc).__name__}:{exc}")
        scope = activation.get("target_scope")
        if not isinstance(scope, Mapping):
            return _blocked("target_scope_config_missing")
        missing = [key for key in TARGET_SCOPE_KEYS if not str(scope.get(key) or "").strip()]
        if missing:
            return _blocked("target_scope_missing:" + ",".join(missing))
        try:
            cash_phase_forward_observations = (
                self._flush_cash_phase_forward_observations()
            )
        except Exception as exc:
            cash_phase_forward_observations = {
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
                "observation_count": 0,
                "written": False,
                "orders_submitted": 0,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
            }
        now = utc_now()
        market_ingress: dict[str, Any] | None = None
        resident_p0_ingress: dict[str, Any] | None = None
        resident_p0m_ingress: dict[str, Any] | None = None
        resident_p1_ingress: dict[str, Any] | None = None
        resident_probability_chain: dict[str, Any] | None = None
        regime_ingress: dict[str, Any] | None = None
        delta_v2_truth_ingress: dict[str, Any] | None = None
        delta_v2_label_ingress: dict[str, Any] | None = None
        post_fill_recompute: dict[str, Any] | None = None
        resident_registry_ingress: dict[str, Any] | None = None
        native_ensemble_ingress: dict[str, Any] | None = None
        native_ensemble_oos_ingress: dict[str, Any] | None = None
        forward_p0_preselections: list[dict[str, Any]] = []
        try:
            registry_path = (
                self.nwp_runtime_root / "market_registry_forward.json"
            )
            if registry_path.is_file():
                resident_registry_ingress = ingest_target_market_registry(
                    self.commit_conn,
                    snapshot_path=registry_path,
                    commit=True,
                    as_of_utc=now,
                )
            else:
                resident_registry_ingress = {
                    "state": "not_available",
                    "reason": "resident_registry_snapshot_missing",
                    "wrote_rows": 0,
                    "writes_target_database": False,
                    "calls_order_gateway": False,
                    "places_orders": False,
                }
            native_inbox_path = (
                self.nwp_runtime_root
                / "native_ensemble_forward_inbox.json"
            )
            official_inbox_path = (
                self.nwp_runtime_root
                / "official_native_ensemble_forward_inbox.json"
            )
            if (
                native_inbox_path.is_file()
                or official_inbox_path.is_file()
            ):
                # The coordinator freezes the current station×event-date
                # scope in the environment.  Resident inboxes may retain
                # older/future observational packets, but those packets must
                # not be ingested or replayed in this cycle: mixing dates
                # turns the 45-station cap into a station-day cap and can
                # fail the whole chain closed.  Empty allowlists preserve the
                # bounded standalone/research worker behavior.
                allowed_stations = tuple(
                    getattr(self, "station_allowlist", ())
                )
                allowed_event_dates = tuple(
                    getattr(self, "event_date_allowlist", ())
                )

                raw_native_packets: list[Mapping[str, Any]] = []
                if native_inbox_path.is_file():
                    raw_native_packets.extend(
                        _load_native_forward_inbox(
                            native_inbox_path,
                            error_code=(
                                "native_ensemble_forward_inbox_invalid"
                            ),
                        )
                    )
                if official_inbox_path.is_file():
                    raw_native_packets.extend(
                        _load_native_forward_inbox(
                            official_inbox_path,
                            error_code=(
                                "official_native_forward_inbox_invalid"
                            ),
                        )
                    )
                if len(raw_native_packets) > 256:
                    raise ContractError(
                        "combined_native_forward_inbox_limit_exceeded"
                    )
                if any(
                    packet.get("schema")
                    not in {
                        OPEN_METEO_FORWARD_PACKET_SCHEMA,
                        OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
                    }
                    for packet in raw_native_packets
                ):
                    raise ContractError(
                        "native_forward_packet_schema_invalid"
                    )
                valid_native_packets = [
                    packet
                    for packet in raw_native_packets
                    if _forward_packet_in_cycle_scope(
                        packet,
                        station_allowlist=allowed_stations,
                        event_date_allowlist=allowed_event_dates,
                    )
                ]
                pending_native_packets: list[Mapping[str, Any]] = []
                for packet in valid_native_packets:
                    if packet.get("schema") != (
                        OPEN_METEO_FORWARD_PACKET_SCHEMA
                    ):
                        continue
                    station_id = str(
                        packet.get("station_id") or ""
                    ).strip().upper()
                    event_date = str(
                        packet.get("event_date") or ""
                    )
                    bucket_contract_sha = str(
                        packet.get("bucket_contract_sha256") or ""
                    )
                    for system in packet.get("systems") or []:
                        if not isinstance(system, Mapping):
                            continue
                        if not _open_meteo_packet_bucket_exists(
                            self.commit_conn,
                            ensemble_system_id=str(
                                system.get("ensemble_system_id") or ""
                            ),
                            station_id=station_id,
                            event_date=event_date,
                            member_set_sha256=str(
                                system.get("member_set_sha256") or ""
                            ),
                            bucket_contract_sha256=bucket_contract_sha,
                        ):
                            pending_native_packets.append(packet)
                            break
                selected_native_packets = pending_native_packets[
                    :MAX_RESIDENT_NWP_SCOPES_PER_CYCLE
                ]
                native_receipts = [
                    ingest_forward_packet(
                        self.commit_conn,
                        packet,
                    )
                    for packet in selected_native_packets
                ]
                native_ensemble_ingress = {
                    "schema": "native_ensemble_forward_ingress_batch_v1",
                    "state": "COMMITTED",
                    "packet_count": len(native_receipts),
                    "inbox_packet_count": len(valid_native_packets),
                    "raw_inbox_packet_count": len(raw_native_packets),
                    "filtered_out_of_scope_packet_count": (
                        len(raw_native_packets) - len(valid_native_packets)
                    ),
                    "station_allowlist": sorted(allowed_stations),
                    "event_date_allowlist": sorted(allowed_event_dates),
                    "event_date_scope_source": str(
                        getattr(self, "event_date_scope_source", "unconfigured")
                    ),
                    "pending_packet_count_before": len(
                        pending_native_packets
                    ),
                    "remaining_packet_count_after": max(
                        0,
                        len(pending_native_packets)
                        - len(selected_native_packets),
                    ),
                    "max_packets_per_cycle":
                        MAX_RESIDENT_NWP_SCOPES_PER_CYCLE,
                    "receipts": native_receipts,
                    "writes_target_database": any(
                        sum(receipt["counts"].values()) > 0
                        for receipt in native_receipts
                    ),
                    "calls_order_gateway": False,
                    "orders_submitted": 0,
                    "live_submit_authorized": False,
                }
                active_native_stations = {
                    str(row[0]).strip().upper()
                    for row in self.commit_conn.execute(
                        "SELECT DISTINCT station_id FROM "
                        "calibration_native_ensemble_activation "
                        "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE' "
                        "AND authority_scope='PRODUCTION_NO_LIVE_ONLY' "
                        "AND valid_from_utc<=? AND valid_until_utc>? "
                        "ORDER BY station_id LIMIT ?",
                        (now, now, MAX_FORWARD_P0_STATIONS),
                    )
                }
                # Official native remains first choice for each station-day.
                # If it is absent at this evidence instant, retain the
                # explicitly labelled Open-Meteo packet for the compiler's
                # independently enforced, closed-day strict-OOS no-live
                # fallback gate.  This selection never upgrades transport
                # lineage or lets a republisher replace a native packet.
                (
                    preferred_p0_packets,
                    official_by_scope,
                    republisher_by_scope,
                ) = _preferred_forward_packets_for_p0(valid_native_packets)
                active_replay_packets = []
                for _scope, packet in sorted(
                    preferred_p0_packets.items(),
                    key=lambda item: (item[0][1], item[0][0]),
                    reverse=True,
                ):
                    station = str(
                        packet.get("station_id") or ""
                    ).strip().upper()
                    if station not in active_native_stations:
                        continue
                    try:
                        window = settlement_day_window(
                            str(packet.get("event_date") or ""),
                            str(
                                packet.get("settlement_timezone")
                                or ""
                            ),
                        )
                        closes_at = datetime.fromisoformat(
                            str(
                                window["target_day_end_utc"]
                            ).replace("Z", "+00:00")
                        ).astimezone(timezone.utc)
                    except Exception:
                        continue
                    if closes_at <= datetime.fromisoformat(
                        now.replace("Z", "+00:00")
                    ).astimezone(timezone.utc):
                        continue
                    active_replay_packets.append(packet)
                # One packet per station-day, so several open days put this
                # above the station count without anything being wrong.
                if len(active_replay_packets) > MAX_FORWARD_P0_STATION_DAYS:
                    raise ContractError("official_native_forward_scope_bound_exceeded")
                active_replay_compiled = []
                active_replay_failures = []
                active_replay_fallback_attempts = []
                active_republisher_fallback_used = 0
                for replay_packet in active_replay_packets:
                    replay_scope = (
                        str(replay_packet.get("station_id") or "")
                        .strip()
                        .upper(),
                        str(replay_packet.get("event_date") or ""),
                    )
                    compiled, failures, source_class = (
                        _compile_native_p0_with_republisher_fallback(
                            self.commit_conn,
                            scope=replay_scope,
                            preferred_packet=replay_packet,
                            official_packet=official_by_scope.get(replay_scope),
                            republisher_packet=republisher_by_scope.get(
                                replay_scope
                            ),
                            decision_at_utc=now,
                            native_oos_snapshot=self.native_oos_snapshot,
                        )
                    )
                    if compiled is None:
                        active_replay_failures.extend(failures)
                        continue
                    try:
                        compiled["forward_prediction_persistence"] = (
                            persist_native_ensemble_forward_prediction(
                                self.commit_conn,
                                compiled,
                            )
                        )
                    except Exception as exc:
                        active_replay_failures.append({
                            "station_id": replay_scope[0],
                            "event_date": replay_scope[1],
                            "source_class": source_class,
                            "reason": f"{type(exc).__name__}:{exc}",
                        })
                        continue
                    if source_class == "aggregator_republisher":
                        active_replay_fallback_attempts.extend(failures)
                        active_republisher_fallback_used += 1
                    active_replay_compiled.append(compiled)
                active_activation_p0_replay = (
                    _upsert_native_p0_inbox(
                        self.p0_inbox_path,
                        compiled_packets=active_replay_compiled,
                        generated_at_utc=now,
                    )
                    if active_replay_compiled
                    else {
                        "state": "NO_COMPILED_P0",
                        "accepted_count": 0,
                        "packet_count": 0,
                        "wrote": False,
                        "calls_order_gateway": False,
                        "orders_submitted": 0,
                        "live_submit_authorized": False,
                    }
                )
                active_activation_p0_replay["active_station_count"] = (
                    len(active_native_stations)
                )
                active_activation_p0_replay["official_packet_count"] = (
                    len(official_by_scope)
                )
                active_activation_p0_replay[
                    "republisher_fallback_packet_count"
                ] = len(republisher_by_scope)
                active_activation_p0_replay[
                    "republisher_fallback_used_count"
                ] = active_republisher_fallback_used
                active_activation_p0_replay[
                    "republisher_fallback_attempts"
                ] = active_replay_fallback_attempts
                active_activation_p0_replay["failures"] = (
                    active_replay_failures
                )
                native_ensemble_ingress[
                    "active_activation_p0_replay"
                ] = active_activation_p0_replay
                now_mono = time.monotonic()
                last_oos_scan = getattr(
                    self,
                    "_last_native_ensemble_oos_scan_monotonic",
                    None,
                )
                new_oos_stations = sorted({
                    str(receipt["station_id"])
                    for receipt in native_receipts
                    if sum(receipt["counts"].values()) > 0
                })
                all_oos_stations = _candidate_ensemble_station_ids(
                    self.commit_conn,
                    limit=MAX_FORWARD_P0_STATIONS,
                )
                truth_signatures = (
                    _native_ensemble_truth_signatures(
                        self.commit_conn,
                    )
                )
                previous_truth_signatures = dict(getattr(
                    self,
                    "_native_ensemble_oos_truth_signature_by_station",
                    {},
                ))
                changed_truth_stations = [
                    station
                    for station, signature
                    in sorted(truth_signatures.items())
                    if previous_truth_signatures.get(station)
                    != signature
                ]
                new_oos_stations = sorted(set(
                    new_oos_stations + changed_truth_stations
                ))
                # One station per production cycle, throttled only enough to
                # keep a fast cycle from spinning the sweep.  At 600s the
                # sweep took 45 x 10min = 7.5 hours to carry a calibration
                # change fleet-wide, while the work itself is 13-19s per
                # station -- a 4% duty cycle, and the reason 41 weight rows
                # sat 3 to 13 days stale with nothing wrong except the
                # clock.  The real floor is the production cycle it rides
                # inside (~175s), so this bound stops being the binding
                # constraint and the sweep lands near 2 hours.
                oos_due = (
                    bool(new_oos_stations)
                    or last_oos_scan is None
                    or now_mono - float(last_oos_scan)
                    >= NATIVE_ENSEMBLE_OOS_SCAN_MIN_INTERVAL_SEC
                )
                if oos_due and all_oos_stations:
                    if new_oos_stations:
                        oos_station = new_oos_stations[0]
                    else:
                        cursor = int(getattr(
                            self,
                            "_native_ensemble_oos_station_cursor",
                            0,
                        )) % len(all_oos_stations)
                        oos_station = all_oos_stations[cursor]
                        self._native_ensemble_oos_station_cursor = (
                            cursor + 1
                        ) % len(all_oos_stations)
                    self._last_native_ensemble_oos_scan_monotonic = (
                        now_mono
                    )
                    if oos_station in truth_signatures:
                        previous_truth_signatures[oos_station] = (
                            truth_signatures[oos_station]
                        )
                    self._native_ensemble_oos_truth_signature_by_station = (
                        previous_truth_signatures
                    )
                    try:
                        # Forward-accumulation window (user decision): the
                        # 20/8 profile ejected any family with one hole in
                        # its latest eight settled days, which kept ECCC out
                        # on thirty days of history and priced ICON three
                        # weeks away.  Training still uses every settled day;
                        # only the gate's holdout shrinks, and thin evidence
                        # is priced by the weight ramp instead of barred.
                        oos_report = build_native_ensemble_oos(
                            self.commit_conn,
                            station_id=oos_station,
                            as_of_utc=now,
                            min_train_days=(
                                FORWARD_ACCUMULATION_MIN_TRAIN_DAYS
                            ),
                            min_holdout_days=(
                                FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS
                            ),
                        )
                        oos_persist = persist_native_ensemble_oos(
                            self.commit_conn,
                            oos_report,
                            activated_at_utc=now,
                        )
                    except Exception as exc:
                        native_ensemble_oos_ingress = {
                            "state": "blocked_fail_closed",
                            "reason": f"{type(exc).__name__}:{exc}",
                            "station_id": oos_station,
                            "isolated_from_probability_and_execution":
                                True,
                            "calls_order_gateway": False,
                            "orders_submitted": 0,
                            "live_submit_authorized": False,
                        }
                    else:
                        activation_replay_compiled = []
                        activation_replay_failures = []
                        if any(
                            row.get("state")
                            == "PRODUCTION_NO_LIVE_OOS_PASS"
                            for row in oos_report["cross_candidates"]
                        ):
                            replay_packets = sorted(
                                (
                                    packet
                                    for packet in valid_native_packets
                                    if str(
                                        packet.get("station_id") or ""
                                    ).strip().upper()
                                    == oos_station
                                ),
                                key=lambda packet: str(
                                    packet.get("event_date") or ""
                                ),
                                reverse=True,
                            )[:MAX_RESIDENT_NWP_SCOPES_PER_CYCLE]
                            for replay_packet in replay_packets:
                                try:
                                    if replay_packet.get("schema") != OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA:
                                        raise ContractError(
                                            "republisher_no_live_selection_not_forward_p0_authority"
                                        )
                                    compiled = (
                                        _compile_native_ensemble_p0_runtime(
                                            self.commit_conn,
                                            forward_packet=replay_packet,
                                            decision_at_utc=now,
                                            native_oos_snapshot=self.native_oos_snapshot,
                                        )
                                    )
                                except Exception as exc:
                                    activation_replay_failures.append({
                                        "station_id": oos_station,
                                        "event_date": str(
                                            replay_packet.get(
                                                "event_date"
                                            )
                                            or ""
                                        ),
                                        "reason": (
                                            f"{type(exc).__name__}:{exc}"
                                        ),
                                    })
                                else:
                                    compiled[
                                        "forward_prediction_persistence"
                                    ] = (
                                        persist_native_ensemble_forward_prediction(
                                            self.commit_conn,
                                            compiled,
                                        )
                                    )
                                    activation_replay_compiled.append(
                                        compiled
                                    )
                        activation_replay = (
                            _upsert_native_p0_inbox(
                                self.p0_inbox_path,
                                compiled_packets=(
                                    activation_replay_compiled
                                ),
                                generated_at_utc=now,
                            )
                            if activation_replay_compiled
                            else {
                                "state": "NO_COMPILED_P0",
                                "accepted_count": 0,
                                "packet_count": 0,
                                "wrote": False,
                                "calls_order_gateway": False,
                                "orders_submitted": 0,
                                "live_submit_authorized": False,
                            }
                        )
                        activation_replay["failures"] = (
                            activation_replay_failures
                        )
                        native_ensemble_oos_ingress = {
                            "schema": oos_report["schema"],
                            "state": (
                                "OOS_READY"
                                if oos_report["cross_candidates"]
                                else "ACCUMULATING_STRICT_OOS"
                            ),
                            "station_id": oos_station,
                            "registered_system_count": oos_report[
                                "registered_system_count"
                            ],
                            "threshold_profile": oos_report[
                                "threshold_profile"
                            ],
                            "min_train_days": oos_report[
                                "min_train_days"
                            ],
                            "min_holdout_days": oos_report[
                                "min_holdout_days"
                            ],
                            "formal_p0_eligible": oos_report[
                                "formal_p0_eligible"
                            ],
                            "readiness": oos_report["readiness"],
                            "cross_candidate_states": [
                                {
                                    "season_bin": row["season_bin"],
                                    "lead_bucket": row["lead_bucket"],
                                    "run_cycle": row["run_cycle"],
                                    "state": row["state"],
                                    "selected_system_count": row[
                                        "selected_system_count"
                                    ],
                                    "independent_family_count": row[
                                        "independent_family_count"
                                    ],
                                    "oos_metrics": row["oos_metrics"],
                                }
                                for row in oos_report[
                                    "cross_candidates"
                                ]
                            ],
                            "persist": oos_persist,
                            "activation_p0_replay": activation_replay,
                            "report_content_sha256": oos_report[
                                "content_sha256"
                            ],
                            "calls_order_gateway": False,
                            "orders_submitted": 0,
                            "live_submit_authorized": False,
                        }
                else:
                    native_ensemble_oos_ingress = {
                        "state": "throttled",
                        "reason": "native_ensemble_oos_scan_interval",
                        "calls_order_gateway": False,
                        "orders_submitted": 0,
                        "live_submit_authorized": False,
                    }
            else:
                native_ensemble_ingress = {
                    "state": "not_available",
                    "reason": (
                        "native_ensemble_forward_inbox_missing"
                    ),
                    "writes_target_database": False,
                    "calls_order_gateway": False,
                    "orders_submitted": 0,
                    "live_submit_authorized": False,
                }
                native_ensemble_oos_ingress = {
                    "state": "not_available",
                    "reason": "native_ensemble_forward_inbox_missing",
                    "calls_order_gateway": False,
                    "orders_submitted": 0,
                    "live_submit_authorized": False,
                }
            resident_probability_chain = self._materialize_resident_probability_chain(
                as_of_utc=now,
            )
            resident_p0_ingress = resident_probability_chain["p0"]
            resident_p0m_ingress = resident_probability_chain["p0m"]
            resident_p1_ingress = resident_probability_chain["p1"]
            # A correction/revision halt belongs to one station-day.  It must
            # remain fail-closed for that scope, but must not suppress healthy
            # stations from reaching their own WS/decision path.  Only a
            # malformed unscoped P1 blocker is a global chain blocker.
            if (
                str(resident_p1_ingress.get("state") or "").upper()
                == "BLOCKED_FAIL_CLOSED"
            ):
                p1_blockers = resident_p1_ingress.get("blockers")
                if not isinstance(p1_blockers, list) or any(
                    not isinstance(item, Mapping)
                    or not str(item.get("station_id") or "").strip()
                    or not str(item.get("event_date") or "").strip()
                    for item in p1_blockers
                ):
                    raise ContractError(
                        "resident_p1_blocked_before_current_scope_market"
                    )
            regime_ingress = self._materialize_regime_ssot(as_of_utc=now)
            ws_scope_catalog = (
                self._resident_ws_scope_catalog()
                if self.market_manifest_path.is_file()
                else []
            )
            registry_snapshot_path = (
                self.nwp_runtime_root / "market_registry_forward.json"
            )
            # The catalog was snapshotted at loop entry, but each scope
            # evaluates at its own now(): a compile costs seconds, so forty-odd
            # scopes in, the snapshot's connection clock is past the
            # strict-as-of age budget and every remaining scope dies
            # manifest_stale.  Measured 2026-08-16: scopes 0-19 committed and
            # LTAC onward -- twenty-two of forty-three -- all raised
            # p0_preselection_manifest_stale, losing those stations' forward
            # book for the day, because a forward quote missed is not
            # refetchable.
            #
            # A re-read every sixty seconds was already here and did not fix
            # it: it rebound only the single scope in hand and discarded the
            # rest of the refreshed catalog, so exactly one late scope per
            # minute survived.  That signature is visible in the same
            # measurement -- RJTT, and only RJTT, committed after the
            # truncation point.
            #
            # Re-read against the budget the compile actually enforces, and
            # keep the answer for every scope behind this one.  A read costs
            # ~360ms against a ~4s compile.
            #
            # Measured 2026-08-16, because it sizes the threshold: the
            # publisher republishes every 60-100s, not every few seconds, so
            # the manifest's own age sawtooths most of the way to the 120s
            # budget on its own.  A re-read therefore often returns the same
            # document -- that is fine and cheap -- and when the publisher is
            # genuinely behind, the scope fails closed on a book that really
            # is too old.  What must not happen again is one stale read
            # condemning every scope behind it.
            try:
                max_ws_snapshot_age_sec = float(
                    load_performance_budget()["limits"][
                        "max_ws_snapshot_age_sec"
                    ]
                )
            except Exception:
                max_ws_snapshot_age_sec = 120.0
            # Half the budget: the age is re-measured at the far end of a
            # compile, so the refresh has to leave room for one to run.
            preselection_manifest_max_age_sec = max(
                15.0, max_ws_snapshot_age_sec / 2.0
            )
            preselection_catalog_by_key = {
                item["scope_key"]: item for item in ws_scope_catalog
            }
            # Rotate the start so a truncated pass does not starve the same
            # tail every cycle.  The order is otherwise the catalog's, and
            # every scope is still visited exactly once per pass -- this moves
            # where the pass begins, not which scopes it covers.
            preselection_order = self._forward_preselection_order(
                [item["scope_key"] for item in ws_scope_catalog],
                resume_at=self._forward_p0_rotation_cursor,
            )
            for position, scope_key in enumerate(preselection_order):
                # Advance before the work, not after: whatever ends this pass
                # -- an escaping exception, a lock, a restart -- the next one
                # resumes at the scope this one did not finish.
                self._forward_p0_rotation_cursor = preselection_order[
                    (position + 1) % len(preselection_order)
                ]
                ws_scope = preselection_catalog_by_key.get(scope_key)
                if self._forward_preselection_scope_skipped(ws_scope):
                    continue
                if (
                    self._ws_scope_manifest_age_sec(
                        ws_scope, now=datetime.now(timezone.utc)
                    )
                    > preselection_manifest_max_age_sec
                ):
                    try:
                        preselection_catalog_by_key = {
                            item["scope_key"]: item
                            for item in self._resident_ws_scope_catalog()
                        }
                    except (ContractError, ResourceBoundError):
                        # A torn or rejected read is not this sweep's to fail
                        # on: keep the catalog in hand and let the compile's
                        # own freshness check refuse the scope.  Letting it
                        # escape here would abort the pass and lose every
                        # scope behind it, which is the failure being fixed.
                        pass
                    ws_scope = preselection_catalog_by_key.get(scope_key)
                    if self._forward_preselection_scope_skipped(ws_scope):
                        continue
                try:
                    preselection_evaluation_at = utc_now()
                    with self._temporary_scope_manifest(
                        ws_scope["manifest"]
                    ) as scope_manifest_path:
                        forward_p0_preselections.append(
                            self.materialize_forward_p0_preselections(
                                registry_snapshot_path=(
                                    registry_snapshot_path
                                ),
                                ws_manifest_path=scope_manifest_path,
                                event_date=str(ws_scope["event_date"]),
                                evaluation_at_utc=(
                                    preselection_evaluation_at
                                ),
                                station_id=str(
                                    ws_scope["station_id"]
                                ),
                            )
                        )
                except Exception as exc:
                    forward_p0_preselections.append({
                        **_blocked(
                            f"{type(exc).__name__}:{exc}",
                            station_id=str(ws_scope["station_id"]),
                            event_date=str(ws_scope["event_date"]),
                        ),
                        # A bare exception name is undiagnosable from the
                        # status file, and this receipt is the only trace a
                        # failed forward scope leaves.  Keep the innermost
                        # frames so the site is readable without reproducing
                        # the exact drain race that triggered it.
                        "blocked_traceback_tail": [
                            line.strip()
                            for line in traceback.format_exception(exc)[-4:]
                        ],
                        "preliminary_decision_count": 0,
                        "intents_written": 0,
                        "reservations_written": 0,
                    })
            open_ws_scopes = [
                item
                for item in ws_scope_catalog
                if (
                    item["event_day_state"] == "open"
                    and item["probability_evidence_fresh"] is True
                    and not item.get("scope_blocker")
                )
            ]
            if ws_scope_catalog and not open_ws_scopes:
                if not forward_p0_preselections:
                    raise ContractError(
                        "resident_target_ws_actionable_scope_missing"
                    )
                preliminary_ok = all(
                    item.get("state") == "COMMITTED"
                    for item in forward_p0_preselections
                )
                return {
                    "schema": SCHEMA,
                    "ok": preliminary_ok,
                    "state": (
                        "preliminary_no_live"
                        if preliminary_ok
                        else "blocked_fail_closed"
                    ),
                    "execution_mode": PRODUCTION_EXECUTION_MODE,
                    "candidate_count": 0,
                    "candidates": [],
                    "forward_p0_preselections": (
                        forward_p0_preselections
                    ),
                    "ws_scope_catalog": self._ws_scope_catalog_summary(
                        ws_scope_catalog
                    ),
                    "places_orders": False,
                    "calls_order_gateway": False,
                    "live_submit_authorized": False,
                    "orders_submitted": 0,
                    "resident_registry_ingress": (
                        resident_registry_ingress
                    ),
                    "native_ensemble_ingress": (
                        native_ensemble_ingress
                    ),
                    "native_ensemble_oos_ingress": (
                        native_ensemble_oos_ingress
                    ),
                    "resident_probability_chain": (
                        resident_probability_chain
                    ),
                    "regime_ingress": regime_ingress,
                    "worker_checked_at": now,
                }
            # Probability is the sole weather authority.  Commit every
            # newly available official-observation P1 before binding the
            # market snapshot so downstream selection can prove that its
            # quote is at or after the current checkpoint.  Reversing these
            # calls creates a stale P0-era market snapshot immediately
            # before a P1 update and breaks the canonical
            # P1 -> official WS -> basket ordering.
            selected_open_scope = self._select_resident_open_ws_scope(
                open_ws_scopes,
                as_of_utc=now,
            )
            if not ws_scope_catalog or not selected_open_scope or not str(
                selected_open_scope["probability_stage"]
            ):
                market_ingress = self._materialize_market_evidence(
                    activation
                )
            else:
                # Every open scope of this event day, not one of them.
                # Serving a single scope per cycle meant a full pass over 33
                # stations took five to eight minutes, while the quote lease
                # it depends on is sixty seconds -- so a station's book had
                # expired several times over before its turn came round
                # again, which is where the recurring
                # ``production_quote_expired`` came from.  P1 is the dynamic
                # stage; at one station per cycle it was not dynamic, it was
                # occasional.
                #
                # Nothing new is fetched to do this: the publisher already
                # holds every scope's book in the manifest it rewrites every
                # two seconds, and the ingress below has always taken a
                # station list.  The single-scope bridge was the only thing
                # making it one at a time.
                sweep_scopes = self._sweep_scope_manifests(
                    open_ws_scopes,
                    selected=selected_open_scope,
                    as_of_utc=now,
                )
                with self._temporary_scope_manifest(
                    _merge_scope_manifests(sweep_scopes)
                ) as current_scope_manifest_path:
                    market_ingress = (
                        self._materialize_market_evidence_from_manifest(
                            activation,
                            manifest_path=current_scope_manifest_path,
                            full_materialization=bool(
                                selected_open_scope[
                                    "probability_evidence_fresh"
                                ]
                            ),
                        )
                    )
            station_allowlist = {
                str(value or "").strip().upper()
                for value in market_ingress.get("station_allowlist", [])
                if str(value or "").strip()
            }
            manifest_event_date = str(
                market_ingress.get("event_date") or ""
            ).strip()
            if not station_allowlist or not manifest_event_date:
                raise ContractError(
                    "target_market_station_allowlist_scope_required"
                )
            # The market bundle is materialized after the cycle's initial
            # probability clock.  Compile against a clock captured after that
            # atomic write; otherwise the new quote can be invisible while
            # its inventory/order versions are already visible, producing a
            # false cross-generation drift.
            now = utc_now()
            post_fill_recompute = self._consume_post_fill_recompute_requests(
                activation=activation,
                scope_config=scope,
                as_of_utc=now,
            )
            if post_fill_recompute.get("state") == "BLOCKED_FAIL_CLOSED":
                raise ContractError("post_fill_recompute_blocked")
            recomputed_scopes = {
                (
                    str(item.get("station_id") or ""),
                    str(item.get("event_date") or ""),
                )
                for item in post_fill_recompute.get("results", [])
                if isinstance(item, Mapping)
            }
            all_rows = self.conn.execute(
                "SELECT DISTINCT station_id,event_date,settlement_timezone FROM basket_probability_envelope "
                "WHERE state='COMMITTED' ORDER BY station_id,event_date"
            ).fetchall()
            rows = [
                (station_id, event_date)
                for station_id, event_date, settlement_timezone in all_rows
                if str(station_id).upper() in station_allowlist
                and str(event_date) == manifest_event_date
                and self._event_day_open(
                    str(event_date),
                    settlement_timezone=str(settlement_timezone or ""),
                    as_of_utc=now,
                )
            ]
            if not rows:
                # Preliminary future scopes have their own zero-execution
                # decision chain above.  A current-scope blocker must never
                # be copied into a not-started day or wait for that day to
                # open/settle.
                scopes = self._decision_scopes(
                    as_of_utc=now,
                    include_not_started=False,
                )
                reason_codes = self._blocker_reason_codes(
                    "materialized_probability_envelope_missing",
                    market_ingress.get("reason") if isinstance(market_ingress, Mapping) else None,
                )
                details = {
                    "blocked_scope_decision_version": 1,
                    "probability_envelope_count": 0,
                    "market_ingress_state": str((market_ingress or {}).get("state") or "unknown"),
                    "market_ingress_reason_code": self._blocker_reason_codes(
                        (market_ingress or {}).get("reason") if isinstance(market_ingress, Mapping) else None
                    )[0],
                    "no_market_snapshot_written": True,
                    "no_intents_written": True,
                    "no_capital_reserved": True,
                    "no_orders_submitted": True,
                    "execution_mode": PRODUCTION_EXECUTION_MODE,
                }
                receipts = self._commit_blocked_scopes(
                    scopes=scopes,
                    reason_codes=reason_codes,
                    details=details,
                    created_at_utc=now,
                )
                return {
                    **_blocked("materialized_probability_envelope_missing"),
                    "worker_checked_at": now,
                    "candidate_count": 0,
                    "scope_count": len(scopes),
                    "durable_blocked_decision_count": sum(
                        1 for item in receipts if item.get("writes_target_ledger") is True
                    ),
                    "durable_blocked_decisions": self._blocked_receipt_summary(receipts),
                    "market_ingress": market_ingress,
                    "resident_p0_ingress": resident_p0_ingress,
                    "resident_p0m_ingress": resident_p0m_ingress,
                    "resident_p1_ingress": resident_p1_ingress,
                    "regime_ingress": regime_ingress,
                    "delta_v2_truth_ingress": delta_v2_truth_ingress,
                    "delta_v2_label_ingress": delta_v2_label_ingress,
                    "post_fill_recompute": post_fill_recompute,
                }
            results: list[dict[str, Any]] = [
                dict(item["candidate"])
                for item in post_fill_recompute.get("results", [])
                if isinstance(item, Mapping)
                and isinstance(item.get("candidate"), Mapping)
            ]
            for station_id, event_date in rows:
                station = str(station_id)
                day = str(event_date)
                if (station, day) in recomputed_scopes:
                    continue
                try:
                    replay_blocker = self._replay_root_mismatch_reason(
                        station_id=station,
                        event_date=day,
                        as_of_utc=now,
                    )
                    if replay_blocker is not None:
                        raise ContractError(replay_blocker)
                    candidate = compile_production_cycle_from_ssot(
                        self.conn,
                        station_id=station,
                        event_date=day,
                        evaluation_at_utc=now,
                        season_bin=str(scope["season_bin"]),
                        maturity_bin=str(scope["maturity_bin"]),
                        liquidity_bin=str(scope["liquidity_bin"]),
                        strategy_version=str(scope["strategy_version"]),
                        policy_authority_scope=str(activation["policy_authority_scope"]),
                        base_policy=scope.get("base_policy") if isinstance(scope.get("base_policy"), Mapping) else None,
                    )
                    # A complete current-stage + WS compile is a valid
                    # no-live decision point. Every stage commits only the
                    # content-addressed NO_EXECUTION authority; dry-run must
                    # not materialize P1 observation/bound/training facts.
                    candidate = {
                        **candidate,
                        "atomic_commit": self._commit_current_stage_no_execution_candidate(
                            candidate,
                            station_id=station,
                            event_date=day,
                            created_at_utc=now,
                        ),
                    }
                    results.append(candidate)
                except Exception as exc:
                    reason = f"{type(exc).__name__}:{exc}"
                    blocked_commit = self._commit_blocked_scope(
                        station_id=station,
                        event_date=day,
                        reason_codes=self._blocker_reason_codes(reason),
                        details={
                            "blocked_scope_decision_version": 1,
                            "exception_type": type(exc).__name__,
                            "exception_reason": str(exc),
                            "no_market_snapshot_written": True,
                            "no_intents_written": True,
                            "no_capital_reserved": True,
                            "no_orders_submitted": True,
                            "execution_mode": PRODUCTION_EXECUTION_MODE,
                        },
                        created_at_utc=now,
                    )
                    results.append({
                        **_blocked(reason, station_id=station, event_date=day),
                        "atomic_commit": blocked_commit,
                    })
            # Closed-day truth and labels are maintenance work, not inputs to
            # the current action.  Running their scans before market ingress
            # consumed the strict quote lease and could turn a fresh official
            # WS generation stale before DeltaBasketScore compiled.  Keep the
            # settlement lane independent and non-blocking after every current
            # scope has completed its current-stage -> JIT WS -> decision path.
            (
                delta_v2_truth_ingress,
                delta_v2_label_ingress,
            ) = self._run_delta_v2_settlement_maintenance(
                as_of_utc=utc_now(),
            )
            try:
                cash_phase_forward_observations = (
                    self._flush_cash_phase_forward_observations()
                )
            except Exception as exc:
                cash_phase_forward_observations = {
                    "state": "blocked_fail_closed",
                    "reason": f"{type(exc).__name__}:{exc}",
                    "observation_count": 0,
                    "written": False,
                    "orders_submitted": 0,
                    "calls_order_gateway": False,
                    "live_submit_authorized": False,
                }
            return {
                "schema": SCHEMA,
                "ok": bool(results) and all(item.get("status") in {"ready_no_live", "hold"} for item in results),
                "state": "compiled_no_live" if results and all(item.get("ok", True) for item in results) else "blocked_fail_closed",
                "execution_mode": PRODUCTION_EXECUTION_MODE,
                "candidate_count": len(results),
                "candidates": results,
                "ws_scope_catalog": self._ws_scope_catalog_summary(
                    ws_scope_catalog
                ),
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
                "market_ingress": market_ingress,
                "resident_registry_ingress": resident_registry_ingress,
                "native_ensemble_ingress": native_ensemble_ingress,
                "native_ensemble_oos_ingress": (
                    native_ensemble_oos_ingress
                ),
                "resident_p0_ingress": resident_p0_ingress,
                "resident_p0m_ingress": resident_p0m_ingress,
                "resident_p1_ingress": resident_p1_ingress,
                "resident_probability_chain": resident_probability_chain,
                "regime_ingress": regime_ingress,
                "delta_v2_truth_ingress": delta_v2_truth_ingress,
                "delta_v2_label_ingress": delta_v2_label_ingress,
                "post_fill_recompute": post_fill_recompute,
                "forward_p0_preselections": forward_p0_preselections,
                "cash_phase_forward_observations": (
                    cash_phase_forward_observations
                ),
                "worker_checked_at": now,
            }
        except Exception as exc:
            reason = f"{type(exc).__name__}:{exc}"
            if (
                delta_v2_truth_ingress is None
                and delta_v2_label_ingress is None
            ):
                (
                    delta_v2_truth_ingress,
                    delta_v2_label_ingress,
                ) = self._run_delta_v2_settlement_maintenance(
                    as_of_utc=utc_now(),
                )
            try:
                cash_phase_forward_observations = (
                    self._flush_cash_phase_forward_observations()
                )
            except Exception as forward_exc:
                cash_phase_forward_observations = {
                    "state": "blocked_fail_closed",
                    "reason": (
                        f"{type(forward_exc).__name__}:{forward_exc}"
                    ),
                    "observation_count": 0,
                    "written": False,
                    "orders_submitted": 0,
                    "calls_order_gateway": False,
                    "live_submit_authorized": False,
                }
            # Future P0/P0-M is already materialized by the preliminary
            # no-live lane.  The ordinary atomic ledger is open-day only, so
            # do not turn a current-scope exception into a false
            # future_event_day_not_allowed blocker.
            scopes = self._decision_scopes(
                as_of_utc=now,
                include_not_started=False,
            )
            receipts = self._commit_blocked_scopes(
                scopes=scopes,
                reason_codes=self._blocker_reason_codes(reason),
                details={
                    "blocked_scope_decision_version": 1,
                    "exception_type": type(exc).__name__,
                    "exception_reason": str(exc),
                    "no_market_snapshot_written": True,
                    "no_intents_written": True,
                    "no_capital_reserved": True,
                    "no_orders_submitted": True,
                    "execution_mode": PRODUCTION_EXECUTION_MODE,
                },
                created_at_utc=now,
            )
            payload = {
                **_blocked(reason),
                "worker_checked_at": now,
                "scope_count": len(scopes),
                "durable_blocked_decision_count": sum(
                    1 for item in receipts if item.get("writes_target_ledger") is True
                ),
                "durable_blocked_decisions": self._blocked_receipt_summary(receipts),
            }
            payload["market_ingress"] = market_ingress or {
                "state": "blocked_fail_closed",
                "reason": reason,
                "writes_target_ssot": False,
            }
            payload["resident_registry_ingress"] = (
                resident_registry_ingress
                or {
                    "state": "blocked_fail_closed",
                    "reason": reason,
                    "wrote_rows": 0,
                    "writes_target_database": False,
                    "calls_order_gateway": False,
                    "places_orders": False,
                }
            )
            payload["native_ensemble_ingress"] = (
                native_ensemble_ingress
                or {
                    "state": "blocked_fail_closed",
                    "reason": reason,
                    "writes_target_database": False,
                    "calls_order_gateway": False,
                    "orders_submitted": 0,
                    "live_submit_authorized": False,
                }
            )
            payload["native_ensemble_oos_ingress"] = (
                native_ensemble_oos_ingress
                or {
                    "state": "blocked_fail_closed",
                    "reason": reason,
                    "calls_order_gateway": False,
                    "orders_submitted": 0,
                    "live_submit_authorized": False,
                }
            )
            payload["resident_p0_ingress"] = resident_p0_ingress or {
                "state": "blocked_fail_closed",
                "reason": reason,
                "writes_target_ssot": False,
            }
            payload["resident_p0m_ingress"] = resident_p0m_ingress or {
                "state": "blocked_fail_closed",
                "reason": reason,
                "writes_target_ssot": False,
            }
            payload["resident_p1_ingress"] = resident_p1_ingress or {
                "state": "blocked_fail_closed",
                "reason": reason,
                "writes_target_ssot": False,
            }
            payload["resident_probability_chain"] = resident_probability_chain or {
                "schema": "bayes_basket_target_resident_probability_chain_v1",
                "stage_order": ["P0", "P0-M", "P1"],
                "state": "BLOCKED_FAIL_CLOSED",
                "reason": reason,
                "calls_order_gateway": False,
                "places_orders": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
            payload["regime_ingress"] = regime_ingress or {
                "schema": "bayes_basket_target_regime_batch_v1",
                "state": "BLOCKED_FAIL_CLOSED",
                "reason": reason,
                "receipts": [],
                "blockers": [],
                "calls_order_gateway": False,
                "orders_submitted": 0,
            }
            payload["delta_v2_label_ingress"] = (
                delta_v2_label_ingress
                or {
                    "state": "blocked_fail_closed",
                    "reason": reason,
                    "appended_count": 0,
                }
            )
            payload["delta_v2_truth_ingress"] = (
                delta_v2_truth_ingress
                or {
                    "state": "blocked_fail_closed",
                    "reason": reason,
                    "written_count": 0,
                }
            )
            payload["post_fill_recompute"] = post_fill_recompute or {
                "schema": "bayes_basket_target_post_fill_recompute_batch_v1",
                "state": "blocked_fail_closed",
                "reason": reason,
                "request_count": 0,
                "applied_count": 0,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
            payload["forward_p0_preselections"] = (
                forward_p0_preselections
            )
            payload["cash_phase_forward_observations"] = (
                cash_phase_forward_observations
            )
            return payload

    def close(self) -> None:
        pool = getattr(self, "_official_native_capture_pool", None)
        if pool is not None:
            # Let in-flight captures finish: each is already hard-bounded by
            # its own subprocess deadline, and a half-written staging file
            # would otherwise be merged on the next start.
            pool.shutdown(wait=True, cancel_futures=True)
            self._official_native_capture_pool = None
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self.canonical_source_conn is not None:
            self.canonical_source_conn.close()
            self.canonical_source_conn = None
        if self.commit_conn is not None:
            self.commit_conn.close()
            self.commit_conn = None


__all__ = [
    "DEFAULT_TARGET_ACTIVATION",
    "DEFAULT_TARGET_DB",
    "DEFAULT_TARGET_MARKET_MANIFEST",
    "MAX_PREPARED_CYCLES_PER_SCOPE",
    "PRODUCTION_EXECUTION_MODE",
    "SCHEMA",
    "TargetProductionWorker",
    "open_target_read_only",
    "open_target_writer_if_enabled",
    "utc_now",
]
