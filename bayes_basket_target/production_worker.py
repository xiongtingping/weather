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
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

from .activation import assert_target_database_owner, load_target_activation
from .atomic_ledger import (
    commit_blocked_scope_decision,
    commit_preliminary_scope_decision,
)
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
from .market_ingress import materialize_official_ws_evidence_cache
from .materialization import materialize_p0m
from .probability_bridge import materialize_probability_bridge
from .metar_trajectory import (
    METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
    MetarTrajectoryLikelihoodArtifact,
)
from .p1_compiler import (
    compile_station_p1_packets,
    select_p1_replay_base,
)
from .resident_canonical import load_resident_formal_observations
from .resident_market_ingress import materialize_resident_no_live_market_evidence
from .resource_bounds import (
    assert_connection_target_capacity,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
    write_bounded_json_if_changed,
)
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
    build_native_ensemble_oos,
    persist_native_ensemble_oos,
)
from .native_ensemble_p0 import (
    compile_native_ensemble_p0,
    persist_native_ensemble_forward_prediction,
)
from .native_ensemble_authority import (
    DEFAULT_NATIVE_OOS_SNAPSHOT_MANIFEST,
    NativeOosSnapshot,
    load_native_oos_snapshot_manifest,
)
from .native_ensemble_store import ingest_forward_packet
from .native_ensemble_training_facts import ingest_forward_taf_inbox
from .republisher_fallback_activation import (
    RepublisherFallbackActivation,
    load_republisher_fallback_activation,
    preflight_republisher_fallback_activation,
)
from .p0_preselection import (
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
MAX_FORWARD_P0_STATIONS = 45
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
MAX_RESIDENT_WS_SCOPES = 90


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
    republisher_fallback_activation: RepublisherFallbackActivation
    | Mapping[str, Any]
    | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    """Compile official P0 first, then retry the same scope via republisher.

    Packet presence is not the same as native availability: an official
    packet can be incomplete, stale, or fail its exact activation/transport
    checks.  In that case a separately captured Open-Meteo packet is a valid
    *candidate* for the compiler, but only ``compile_native_ensemble_p0`` can
    admit it after the historical strict-OOS and lineage gates.  Keeping the
    retry here (rather than weakening ``_preferred_forward_packets_for_p0``)
    preserves official precedence while closing the unavailable-native gap.
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
    republisher_candidate = republisher_packet
    if (
        republisher_candidate is None
        and preferred_schema == OPEN_METEO_FORWARD_PACKET_SCHEMA
    ):
        republisher_candidate = preferred_packet

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
    append_candidate("aggregator_republisher", republisher_candidate)
    if not candidates:
        # Keep malformed/legacy callers fail-closed through the compiler,
        # while preserving truthful source labeling in the failure receipt.
        append_candidate(
            (
                "aggregator_republisher"
                if preferred_schema == OPEN_METEO_FORWARD_PACKET_SCHEMA
                else "official_native"
            ),
            preferred_packet,
        )

    failures: list[dict[str, Any]] = []
    for source_class, packet in candidates:
        fallback_receipt: Mapping[str, Any] | None = None
        if (
            source_class == "aggregator_republisher"
            and republisher_fallback_activation is not None
        ):
            # The official attempt above is the native-availability probe.
            # A republisher packet is never sent to the native compiler until
            # the independent pointer/cross-activation linkage has passed.
            try:
                fallback_receipt = preflight_republisher_fallback_activation(
                    conn,
                    activation=republisher_fallback_activation,
                    packet=packet,
                    decision_at_utc=decision_at_utc,
                    native_unavailable=True,
                )
            except Exception as exc:
                failures.append({
                    "station_id": scope[0],
                    "event_date": scope[1],
                    "source_class": source_class,
                    "schema": str(packet.get("schema") or ""),
                    "reason": f"{type(exc).__name__}:{exc}",
                })
                continue
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
            if fallback_receipt is not None:
                # Keep the proof beside the compiled handoff without
                # mutating the compiler's content-addressed P0 packet.
                compiled = dict(compiled)
                compiled["republisher_fallback_activation"] = dict(
                    fallback_receipt
                )
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
        if (
            current is None
            # SAME and MIXED are one official authority tier.  Keep the
            # already selected official packet on a tie so a replay of an
            # older SAME-cycle packet cannot replace a current MIXED packet.
            or source_priority(candidate_row) > source_priority(current)
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


def _resident_open_event_dates(path: Path) -> tuple[str, ...]:
    """Return the current open station-day dates from the resident WS handoff.

    The forward NWP inbox is intentionally append-only and may retain several
    observational dates.  A production cycle must therefore get its event-day
    scope from the resident market handoff, not from whichever dates happen to
    be present in that inbox.  This helper is deliberately conservative: a
    missing, malformed, or multi-date open handoff yields no inferred scope and
    lets the normal downstream fail-closed gates report the actual blocker.
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
    # Mixing open station-days would turn the 45-station cap into a
    # station-day cap.  Require the coordinator to pass an explicit scope in
    # that case instead of guessing.
    return tuple(sorted(dates)) if len(dates) == 1 else ()


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
        self.republisher_fallback_activation: (
            RepublisherFallbackActivation | Mapping[str, Any] | None
        ) = None
        self.republisher_fallback_activation_error: str | None = None
        writer_requested = str(
            os.environ.get("PM_TARGET_PRODUCTION_WRITER", "") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        production_no_live_runtime = (
            writer_requested
            or str(os.environ.get("PM_TARGET_EXECUTION_MODE", "") or "").strip()
            == PRODUCTION_EXECUTION_MODE
        )
        if production_no_live_runtime:
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
            try:
                self.republisher_fallback_activation = (
                    load_republisher_fallback_activation(
                        os.environ.get(
                            "PM_TARGET_REPUBLISHER_FALLBACK_BINDING"
                        )
                        or None
                    )
                )
            except Exception as exc:
                # An invalid/missing fallback pointer must only disable the
                # Open-Meteo retry.  Official native packets remain eligible
                # and are attempted exactly as before.
                self.republisher_fallback_activation_error = (
                    f"{type(exc).__name__}:{exc}"
                )
                self.republisher_fallback_activation = {
                    "schema": "invalid_republisher_fallback_activation",
                    "load_error": self.republisher_fallback_activation_error,
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
        if not raw_event_dates and production_no_live_runtime:
            inferred_event_dates = _resident_open_event_dates(
                self.market_manifest_path
            )
            if inferred_event_dates:
                raw_event_dates = inferred_event_dates
                event_date_scope_source = "resident_ws_manifest_open_scope"
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

    def refresh_resident_nwp(self, *, as_of_utc: str) -> dict[str, Any]:
        """Capture official models and refresh the bounded P0 inbox.

        The method belongs to this existing worker so there is still one
        resident owner and no fourth service.  Registry export is read-only
        against the canonical database; only the isolated target registry and
        P0 inbox may change.
        """

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
        if existing_registry is None:
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
        official_probe_reason = "official_native_packet_missing"
        if official_inbox_path.is_file():
            try:
                official_packets = _load_native_forward_inbox(
                    official_inbox_path,
                    error_code="official_native_forward_inbox_invalid",
                )
                for candidate in official_packets:
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
                        official_probe_reason = "official_native_packet_found"
                        break
            except Exception as exc:
                official_probe_reason = (
                    "official_native_packet_unavailable:"
                    f"{type(exc).__name__}:{exc}"
                )
        if official_packet is not None:
            try:
                compiled_official = _compile_native_ensemble_p0_runtime(
                    read_conn,
                    forward_packet=official_packet,
                    decision_at_utc=as_of_utc,
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
                    return {
                        "schema": "bayes_basket_target_resident_nwp_cycle_v1",
                        "state": "official_native_reused",
                        "reason": "official_native_precedence",
                        "station_id": native_station,
                        "event_date": native_event_date,
                        "source_role": "official_native",
                        "official_packet_reused": True,
                        "republisher_fallback_used": False,
                        "official_probe_reason": official_probe_reason,
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
        native_rows = [
            row
            for row in registry.get("rows") or []
            if isinstance(row, Mapping)
            and str(row.get("station_id") or "").strip().upper()
            == native_station
            and str(row.get("event_date") or "")
            == native_event_date
        ]
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
                merged_packets.sort(
                    key=lambda item: (
                        str(item.get("event_date") or ""),
                        str(item.get("station_id") or ""),
                    )
                )
                if len(merged_packets) > 256:
                    merged_packets = merged_packets[-256:]
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
                republisher_fallback_activation = getattr(
                    self,
                    "republisher_fallback_activation",
                    None,
                )
                if republisher_fallback_activation is not None:
                    fallback_receipt = preflight_republisher_fallback_activation(
                        read_conn,
                        activation=republisher_fallback_activation,
                        packet=native_packet,
                        decision_at_utc=as_of_utc,
                        native_unavailable=True,
                    )
                else:
                    fallback_receipt = None
                native_p0_compile = _compile_native_ensemble_p0_runtime(
                    read_conn,
                    forward_packet=native_packet,
                    decision_at_utc=as_of_utc,
                    native_oos_snapshot=self.native_oos_snapshot,
                )
                if fallback_receipt is not None:
                    native_p0_compile = dict(native_p0_compile)
                    native_p0_compile[
                        "republisher_fallback_activation"
                    ] = dict(fallback_receipt)
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
            rotation_state = build_rotation_state(
                station_ids=station_allowlist,
                scopes=scopes,
                selected_scopes=[native_scope],
                cursor_by_priority={
                    0: self._native_ensemble_scope_cursor,
                },
                outcomes=[{
                    "scope_key": nwp_rotation_scope_key(native_scope),
                    "state": "CAPTURED_CANDIDATE",
                    "candidate_only": True,
                    "reason": (
                        "native_ensemble_calibration_and_cross_family_oos_required"
                    ),
                }],
                previous=getattr(
                    self,
                    "_nwp_rotation_state",
                    None,
                ),
                updated_at_utc=as_of_utc,
            )
            rotation_state_path = getattr(
                self,
                "nwp_rotation_state_path",
                self.nwp_runtime_root / "rotation_state.json",
            )
            rotation_state_wrote = persist_rotation_state(
                rotation_state_path,
                rotation_state,
            )
            self._nwp_rotation_state = rotation_state
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
                "rotation_state_path": str(rotation_state_path),
                "rotation_state_wrote": rotation_state_wrote,
                "rotation_generation": rotation_state["generation"],
                "next_scope_cursor": self._native_ensemble_scope_cursor,
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        except Exception as exc:
            return {
                "schema": "bayes_basket_target_resident_native_ensemble_cycle_v1",
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
                "station_id": native_station,
                "event_date": native_event_date,
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

    def _commit_blocked_scope(
        self,
        *,
        station_id: str,
        event_date: str,
        reason_codes: list[str],
        details: Mapping[str, Any] | None,
        created_at_utc: str,
    ) -> dict[str, Any]:
        """Write only the authoritative blocker; never manufacture inputs."""

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
        facts = target_atomic_facts_from_cycle(candidate)
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
                observation=facts["observation"],
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
            if day_state == "not_started" and stage not in {"P0", "P0-M"}:
                raise ContractError(
                    "resident_target_ws_future_stage_forbidden"
                )
            scope_blocker = ""
            if day_state == "not_started" and probability_evidence_fresh is not True:
                # A stale future scope is observational only.  Keep it in the
                # catalog for auditability, but do not let one station-day
                # suppress healthy open scopes or create a future P0 action.
                scope_blocker = "resident_target_ws_future_probability_stale"
            if day_state == "open" and stage not in {"P0", "P0-M", "P1"}:
                raise ContractError(
                    "resident_target_ws_open_scope_probability_stage_invalid"
                )
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

    def _select_resident_open_ws_scope(
        self,
        open_scopes: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any] | None:
        """Round-robin one open scope per cycle without starving stations."""

        if not open_scopes:
            return None
        if len(open_scopes) > MAX_FORWARD_P0_STATIONS:
            raise ContractError(
                "resident_target_ws_open_scope_station_cap_exceeded"
            )
        ordered = sorted(
            (dict(item) for item in open_scopes),
            key=lambda item: str(item.get("scope_key") or ""),
        )
        selected = ordered[
            self._resident_open_scope_cursor % len(ordered)
        ]
        self._resident_open_scope_cursor = (
            self._resident_open_scope_cursor + 1
        ) % len(ordered)
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
        if len(grouped) > MAX_FORWARD_P0_STATIONS:
            raise RuntimeError("resident_p1_station_scope_cap_exceeded")
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
                if trajectory_artifact is None:
                    raise ContractError(
                        "resident_p1_formal_metar_trajectory_artifact_missing"
                    )
                if (
                    trajectory_artifact.method_version
                    != METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION
                ):
                    raise ContractError(
                        "resident_p1_conditional_hourly_curve_artifact_required"
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
                    receipts.append({
                        "station_id": station,
                        "event_date": day,
                        "state": "NO_NEW_REPORTED_HIGH",
                        "observation_count": len(observations),
                        "p1_update_count": 0,
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
                current_consumable = final_envelope.is_fresh(as_of_utc)
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
                        and str(root.lineage.get("lead_bucket") or "")
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
        compiled = []
        for station_id in stations:
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
                planning_budget_usdc=planning_budget_usdc,
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
                decision_receipts.append(
                    commit_preliminary_scope_decision(
                        self.commit_conn,
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
                            additional_scopes.append(scope)
                        if len(additional_scopes) >= 45:
                            break
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
                if result.get("state") == "blocked_fail_closed":
                    blockers = result.get("blockers")
                    first = (
                        blockers[0].get("reason")
                        if isinstance(blockers, list)
                        and blockers
                        and isinstance(blockers[0], Mapping)
                        else "unknown"
                    )
                    raise ContractError(
                        f"delta_v2_settlement_truth_blocked:{first}"
                    )
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
                if len(active_replay_packets) > MAX_FORWARD_P0_STATIONS:
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
                            republisher_fallback_activation=(
                                getattr(
                                    self,
                                    "republisher_fallback_activation",
                                    None,
                                )
                            ),
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
                oos_due = (
                    bool(new_oos_stations)
                    or last_oos_scan is None
                    or now_mono - float(last_oos_scan) >= 600.0
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
                        oos_report = build_native_ensemble_oos(
                            self.commit_conn,
                            station_id=oos_station,
                            as_of_utc=now,
                            min_train_days=(
                                DEFAULT_MIN_TRAIN_DAYS
                            ),
                            min_holdout_days=(
                                DEFAULT_MIN_HOLDOUT_DAYS
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
                                    fallback_receipt = None
                                    if (
                                        replay_packet.get("schema")
                                        == OPEN_METEO_FORWARD_PACKET_SCHEMA
                                        and getattr(
                                            self,
                                            "republisher_fallback_activation",
                                            None,
                                        )
                                        is not None
                                    ):
                                        fallback_receipt = (
                                            preflight_republisher_fallback_activation(
                                                self.commit_conn,
                                                activation=(
                                                    getattr(
                                                        self,
                                                        "republisher_fallback_activation",
                                                        None,
                                                    )
                                                ),
                                                packet=replay_packet,
                                                decision_at_utc=now,
                                                native_unavailable=True,
                                            )
                                        )
                                    compiled = (
                                        _compile_native_ensemble_p0_runtime(
                                            self.commit_conn,
                                            forward_packet=replay_packet,
                                            decision_at_utc=now,
                                            native_oos_snapshot=self.native_oos_snapshot,
                                        )
                                    )
                                    if fallback_receipt is not None:
                                        compiled = dict(compiled)
                                        compiled[
                                            "republisher_fallback_activation"
                                        ] = dict(fallback_receipt)
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
            for ws_scope in ws_scope_catalog:
                if (
                    ws_scope["event_day_state"] != "not_started"
                    or not ws_scope["probability_evidence_fresh"]
                    or ws_scope.get("scope_blocker")
                ):
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
                open_ws_scopes
            )
            if not ws_scope_catalog or not selected_open_scope or not str(
                selected_open_scope["probability_stage"]
            ):
                market_ingress = self._materialize_market_evidence(
                    activation
                )
            else:
                with self._temporary_scope_manifest(
                    selected_open_scope["manifest"]
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
