#!/usr/bin/env python3
"""Read-only readiness gate for the Bayes × basket target production lane.

The target lane is intentionally ``production_no_live``.  This audit does
not install DDL, materialize facts, call a gateway, or change launchd.  It
joins the controlled migration plan, the file/DB activation pair, target
input counts, and the three-service domain probe into one evidence receipt.
An offline passing unit test or rehearsal is never treated as a real-library
readiness signal.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.activation import (  # noqa: E402
    assert_target_database_owner,
    load_target_activation,
)
from bayes_basket_target import (  # noqa: E402
    FORWARD_SCORECARD_SCHEMA,
    build_forward_scorecard,
)
from bayes_basket_target.database import (  # noqa: E402
    DEFAULT_TARGET_DB,
    DEFAULT_TARGET_WS_MANIFEST,
    is_isolated_target_database,
    resolve_target_database,
)
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.et_history import event_day_window  # noqa: E402
from bayes_basket_target.future_day_residue import audit_future_day_residue  # noqa: E402
from bayes_basket_target.market_ingress import (  # noqa: E402
    load_official_ws_quotes,
    load_official_ws_quotes_payload,
)
from bayes_basket_target.market_registry import load_started_registry_rows  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    inspect_target_resource_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.readiness_scope import (  # noqa: E402
    resolve_target_readiness_scopes,
)
from bayes_basket_target.ssot import PRODUCTION_TARGET_TABLES  # noqa: E402
from scripts.bayes_basket_target_production_ssot_apply import (  # noqa: E402
    PAUSE_MARKER_TOKEN,
    inspect_migration,
)


SCHEMA = "bayes_basket_target_production_readiness_v1"
DEFAULT_CALIBRATION_EVIDENCE = ROOT / "output/audits/bayes_basket_target_calibration_evidence.json"
DEFAULT_NATIVE_DECODE_EVIDENCE = ROOT / "output/audits/bayes_basket_target_native_model_arrival_decode.json"
DEFAULT_TARGET_RAW_ROOT = ROOT / "data/research/bayes_basket_target/model_arrivals"
# A station's holdout window ends on settled truth, which itself lands a day
# or two late, and the OOS scan walks one station at a time.  Two days of lag
# is therefore structural; five is the point past which a station has stopped
# being refreshed rather than merely waiting its turn.
DEFAULT_CALIBRATION_MAX_STALE_DAYS = 5
# The target lane must never use the resident WS manifest as an input.
# Keep the target evidence path separate from unrelated resident evidence.
TARGET_WS_MANIFEST = DEFAULT_TARGET_WS_MANIFEST
RESIDENT_WS_MANIFEST = ROOT / "data/market_ws_latest_quotes.json"
MULTI_SCOPE_WS_MANIFEST_SCHEMA = (
    "bayes_basket_target_resident_multi_scope_ws_manifest_v1"
)
MAX_TARGET_WS_SCOPES = 90
CORE_SERVICES = (
    "com.weather.weather_basket_yes",
    "com.weather.market_ws",
    "com.weather.clob_order_service",
)
FORBIDDEN_SERVICES = (
    "com.xiong.weather.ui",
    "com.weather.monitor.daemon",
    "com.weather.model_release_watcher",
    "com.weather.dynamic_three_leg_execution",
    "com.weather.target_realization_worker",
    "com.weather.auto_trade_watcher",
    "com.weather.trade_engine",
    "com.weather.auto_trade_worker",
    "com.weather.trade_sync_loop",
)
REQUIRED_SCOPE_KEYS = ("season_bin", "maturity_bin", "liquidity_bin", "strategy_version")
TARGET_INPUT_TABLES = (
    "basket_target_market_registry_snapshot",
    "basket_probability_envelope",
    "basket_p0_preselection_plan",
    "basket_official_ws_snapshot",
    "basket_confirmed_inventory_snapshot",
    "basket_order_state_snapshot",
    "basket_cash_phase_policy_artifact",
    "basket_cash_phase_authority_manifest",
    "basket_global_prior_artifact",
    "basket_station_policy_artifact",
    "basket_policy_activation",
    "calibration_model_artifact",
    "calibration_ensemble_artifact",
    "calibration_artifact_promotion",
    "calibration_activation",
)
OPTIONAL_MATERIALIZED_INPUT_TABLES = frozenset(
    {
        # P0 preselection is an early, non-authorizing plan.  A final P1
        # decision chain is valid without retaining a P0 preselection row.
        "basket_p0_preselection_plan",
        # Formal promotion is intentionally absent in the
        # PRODUCTION_NO_LIVE_ONLY calibration authority.
        "calibration_artifact_promotion",
        "calibration_activation",
    }
)
PROBABILITY_EVIDENCE_TABLES = (
    "basket_probability_evidence_artifact",
    "basket_probability_evidence_promotion",
    "basket_probability_evidence_activation",
    "basket_probability_update_proof",
)
PROBABILITY_BRIDGE_TABLES = (
    "basket_probability_bridge_history",
    "basket_probability_bridge_latest",
    "basket_probability_bridge_bucket_probability",
)
PROBABILITY_ACCEPTANCE_TABLES = (
    *PROBABILITY_EVIDENCE_TABLES,
    *PROBABILITY_BRIDGE_TABLES,
)
PROBABILITY_CANDIDATE_SCOPES = (
    "PRODUCTION_NO_LIVE_CANDIDATE",
    "CANDIDATE_ONLY",
)
MAX_PROBABILITY_PROOFS_INSPECTED = 512


def _probability_evidence_runtime_status(
    conn: sqlite3.Connection,
    *,
    station_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Separate installed schema, active artifacts, and resident consumption."""

    scope = tuple(
        sorted(
            {
                str(station_id).strip().upper()
                for station_id in station_ids
                if str(station_id).strip()
            }
        )
    )
    placeholders = ",".join("?" for _ in scope)
    artifact_scope_sql = (
        f" AND r.station_id IN ({placeholders})" if scope else ""
    )
    proof_scope_sql = (
        f" WHERE station_id IN ({placeholders})" if scope else ""
    )
    present = _table_names(conn)
    missing = sorted(set(PROBABILITY_EVIDENCE_TABLES) - present)
    missing_bridge_tables = sorted(set(PROBABILITY_BRIDGE_TABLES) - present)

    def probability_table_scope_counts() -> dict[str, dict[str, int | str]]:
        result: dict[str, dict[str, int | str]] = {}
        for table in PROBABILITY_ACCEPTANCE_TABLES:
            if table not in present:
                result[table] = {
                    "state": "MISSING",
                    "target": -1,
                    "non_target": -1,
                }
                continue
            columns = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})")
            }
            if table == "basket_probability_bridge_bucket_probability":
                if (
                    "history_id" not in columns
                    or "basket_probability_bridge_history" not in present
                ):
                    result[table] = {
                        "state": "STATION_JOIN_MISSING",
                        "target": -1,
                        "non_target": -1,
                    }
                    continue
                target = int(conn.execute(
                    "SELECT COUNT(*) FROM "
                    "basket_probability_bridge_bucket_probability b "
                    "JOIN basket_probability_bridge_history h "
                    "ON h.history_id=b.history_id WHERE h.station_id IN ("
                    + ",".join("?" for _ in scope)
                    + ")",
                    scope,
                ).fetchone()[0]) if scope else int(conn.execute(
                    "SELECT COUNT(*) FROM "
                    "basket_probability_bridge_bucket_probability"
                ).fetchone()[0])
                non_target = int(conn.execute(
                    "SELECT COUNT(*) FROM "
                    "basket_probability_bridge_bucket_probability b "
                    "JOIN basket_probability_bridge_history h "
                    "ON h.history_id=b.history_id "
                    "WHERE UPPER(COALESCE(h.station_id,'')) NOT IN ("
                    + ",".join("?" for _ in scope)
                    + ")",
                    scope,
                ).fetchone()[0]) if scope else 0
                result[table] = {
                    "state": "READY",
                    "target": target,
                    "non_target": non_target,
                }
                continue
            if "station_id" not in columns:
                result[table] = {
                    "state": "STATION_COLUMN_MISSING",
                    "target": -1,
                    "non_target": -1,
                }
                continue
            target_sql = (
                f"SELECT COUNT(*) FROM {table} "
                "WHERE UPPER(COALESCE(station_id,'')) IN ("
                + ",".join("?" for _ in scope)
                + ")"
            ) if scope else f"SELECT COUNT(*) FROM {table}"
            target = int(conn.execute(target_sql, scope).fetchone()[0])
            non_target = int(conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE UPPER(COALESCE(station_id,'')) NOT IN ("
                + ",".join("?" for _ in scope)
                + ")",
                scope,
            ).fetchone()[0]) if scope else 0
            result[table] = {
                "state": "READY",
                "target": target,
                "non_target": non_target,
            }
        return result

    base = {
        "schema": "bayes_basket_probability_evidence_runtime_status_v1",
            "missing_tables": missing,
            "missing_bridge_tables": missing_bridge_tables,
        "read_only": True,
        "production_no_live": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "station_ids": list(scope),
    }
    if missing:
        return {
            **base,
            "state": "SCHEMA_MISSING",
            "formal_activation_counts": {},
            "proof_counts": {},
            "formal_consumption_counts": {},
            "fallback_proof_counts": {},
            "continuous_core_proof_count": 0,
            "continuous_core_decision_count": 0,
            "direction_transition_decision_count": 0,
            "candidate_artifact_counts": {},
            "candidate_rejected_count": 0,
            "candidate_activation_count": 0,
            "candidate_activation_count_all": 0,
            "probability_table_scope_counts": (
                probability_table_scope_counts()
            ),
            "p0_historical_state": "SCHEMA_MISSING",
            "p1_trajectory_state": "SCHEMA_MISSING",
        }

    formal_activation_counts = {
        str(kind): int(count)
        for kind, count in conn.execute(
            "SELECT a.artifact_kind,COUNT(DISTINCT a.activation_id) "
            "FROM basket_probability_evidence_activation a "
            "JOIN basket_probability_evidence_artifact r "
            "ON r.artifact_kind=a.artifact_kind "
            "AND r.artifact_id=a.artifact_id "
            "JOIN basket_probability_evidence_promotion p "
            "ON p.artifact_kind=a.artifact_kind "
            "AND p.artifact_id=a.artifact_id "
            "AND p.decision='PROMOTED' "
            "WHERE a.authority_scope='FORMAL_PROMOTED' "
            "AND r.authority_scope='FORMAL_PROMOTED' "
            "AND r.oos_gate_passed=1 "
            + artifact_scope_sql
            +
            "GROUP BY a.artifact_kind"
            ,
            scope,
        ).fetchall()
    }
    candidate_artifact_counts = {
        str(kind): int(count)
        for kind, count in conn.execute(
            "SELECT r.artifact_kind,COUNT(DISTINCT r.artifact_id) "
            "FROM basket_probability_evidence_artifact r "
            "WHERE r.authority_scope='PRODUCTION_NO_LIVE_CANDIDATE'"
            + artifact_scope_sql
            + " GROUP BY r.artifact_kind",
            scope,
        ).fetchall()
    }
    candidate_rejected_count = int(conn.execute(
        "SELECT COUNT(*) FROM basket_probability_evidence_artifact r "
        "WHERE r.authority_scope='PRODUCTION_NO_LIVE_CANDIDATE'"
        + artifact_scope_sql
        + " AND EXISTS ("
        "SELECT 1 FROM basket_probability_evidence_promotion p "
        "WHERE p.artifact_kind=r.artifact_kind "
        "AND p.artifact_id=r.artifact_id "
        "AND p.decision_seq=("
        "SELECT MAX(p2.decision_seq) "
        "FROM basket_probability_evidence_promotion p2 "
        "WHERE p2.artifact_kind=r.artifact_kind "
        "AND p2.artifact_id=r.artifact_id"
        ") AND p.decision='REJECTED')",
        scope,
    ).fetchone()[0])
    candidate_activation_count = int(conn.execute(
        "SELECT COUNT(DISTINCT a.activation_id) "
        "FROM basket_probability_evidence_activation a "
        "JOIN basket_probability_evidence_artifact r "
        "ON r.artifact_kind=a.artifact_kind "
        "AND r.artifact_id=a.artifact_id "
        "WHERE r.authority_scope='PRODUCTION_NO_LIVE_CANDIDATE'"
        + artifact_scope_sql,
        scope,
    ).fetchone()[0])
    candidate_activation_count_all = int(conn.execute(
        "SELECT COUNT(DISTINCT a.activation_id) "
        "FROM basket_probability_evidence_activation a "
        "LEFT JOIN basket_probability_evidence_artifact r "
        "ON r.artifact_kind=a.artifact_kind AND r.artifact_id=a.artifact_id "
        "WHERE a.authority_scope IN (?,?) "
        "OR r.authority_scope IN (?,?)",
        (*PROBABILITY_CANDIDATE_SCOPES, *PROBABILITY_CANDIDATE_SCOPES),
    ).fetchone()[0])
    proof_counts: dict[str, int] = {}
    formal_consumption_counts: dict[str, int] = {}
    fallback_proof_counts: dict[str, int] = {}
    rows = conn.execute(
        "SELECT proof_kind,artifact_id_or_none,activation_id_or_none,"
        "length(CAST(proof_json AS BLOB)),proof_json "
        "FROM basket_probability_update_proof "
        + proof_scope_sql
        +
        "ORDER BY decision_at_utc DESC,proof_id DESC LIMIT ?",
        (*scope, MAX_PROBABILITY_PROOFS_INSPECTED + 1),
    ).fetchall()
    capped = len(rows) > MAX_PROBABILITY_PROOFS_INSPECTED
    if capped:
        rows = rows[:MAX_PROBABILITY_PROOFS_INSPECTED]
    continuous_core_proof_count = 0
    malformed_or_oversized_proof_count = 0
    fallback_contract_failures: list[str] = []
    for kind, artifact_id, activation_id, byte_length, raw_json in rows:
        proof_kind = str(kind)
        proof_counts[proof_kind] = proof_counts.get(proof_kind, 0) + 1
        formal = (
            str(artifact_id or "").upper() not in {"", "NONE"}
            and str(activation_id or "").upper() not in {"", "NONE"}
        )
        target = (
            formal_consumption_counts
            if formal else fallback_proof_counts
        )
        target[proof_kind] = target.get(proof_kind, 0) + 1
        try:
            size = int(byte_length)
            if size <= 0 or size > 1_048_576:
                raise ValueError
            proof = json.loads(str(raw_json))
            if not formal and proof_kind == "P0_HISTORICAL_TILT":
                if (
                    proof.get("theta_zero_fallback") is not True
                    or abs(float(proof.get("applied_theta"))) > 1e-12
                    or proof.get("probabilities_before")
                    != proof.get("probabilities_after")
                ):
                    fallback_contract_failures.append(
                        "p0_theta_zero_fallback_invalid"
                    )
            if not formal and proof_kind == "METAR_TRAJECTORY_UPDATE":
                likelihood = proof.get("likelihood_ratio_by_bucket")
                if (
                    abs(float(proof.get("applied_strength"))) > 1e-12
                    or not isinstance(likelihood, Mapping)
                    or not likelihood
                    or any(
                        abs(float(value) - 1.0) > 1e-12
                        for value in likelihood.values()
                    )
                ):
                    fallback_contract_failures.append(
                        "p1_identity_likelihood_fallback_invalid"
                    )
            core = proof.get("positive_core_buckets")
            if (
                isinstance(core, list)
                and core
                and all(
                    isinstance(bucket, str) and bucket.strip()
                    for bucket in core
                )
            ):
                continuous_core_proof_count += 1
        except (TypeError, ValueError, json.JSONDecodeError):
            malformed_or_oversized_proof_count += 1

    continuous_core_decision_count = 0
    direction_transition_decision_count = 0
    legacy_core_decision_count = 0
    malformed_or_oversized_decision_count = 0
    decision_cap_reached = False
    if "auto_trade_decisions" in present:
        decision_scope_sql = (
            f" AND station_id IN ({placeholders})" if scope else ""
        )
        decision_rows = conn.execute(
            "SELECT length(CAST(details_json AS BLOB)),details_json "
            "FROM auto_trade_decisions WHERE strategy_lane='basket'"
            + decision_scope_sql
            + " ORDER BY rowid DESC LIMIT ?",
            (*scope, MAX_PROBABILITY_PROOFS_INSPECTED + 1),
        ).fetchall()
        decision_cap_reached = (
            len(decision_rows) > MAX_PROBABILITY_PROOFS_INSPECTED
        )
        for byte_length, raw_json in decision_rows[
            :MAX_PROBABILITY_PROOFS_INSPECTED
        ]:
            try:
                size = int(byte_length)
                if size <= 0 or size > 1_048_576:
                    raise ValueError
                details = json.loads(str(raw_json))
                core = details.get("core_buckets")
                if core is None:
                    continue
                if (
                    not isinstance(core, list)
                    or not core
                    or any(
                        not isinstance(bucket, str) or not bucket.strip()
                        for bucket in core
                    )
                    or len(core) != len(set(core))
                ):
                    raise ValueError
                continuous_core_decision_count += 1
                direction = str(details.get("direction") or "").upper()
                transition = details.get("core_transition")
                if (
                    direction not in {"UP", "DOWN", "NEUTRAL"}
                    or not isinstance(transition, Mapping)
                    or str(
                        transition.get("direction_transition") or ""
                    ).upper()
                    not in {
                        "INITIALIZE",
                        "MIGRATE",
                        "NEUTRALIZE",
                        "KEEP",
                        "HOLD",
                    }
                ):
                    legacy_core_decision_count += 1
                    continue
                direction_transition_decision_count += 1
            except (TypeError, ValueError, json.JSONDecodeError):
                malformed_or_oversized_decision_count += 1

    def feature_state(
        artifact_kind: str,
        proof_kind: str,
        fallback_name: str,
    ) -> str:
        active = int(formal_activation_counts.get(artifact_kind, 0))
        consumed = int(formal_consumption_counts.get(proof_kind, 0))
        fallback = int(fallback_proof_counts.get(proof_kind, 0))
        if active and consumed:
            return "FORMAL_ACTIVE_AND_CONSUMED"
        if active:
            return "FORMAL_ACTIVE_AWAITING_RESIDENT_CONSUMPTION"
        if fallback:
            return fallback_name
        return "NO_FORMAL_ARTIFACT"

    p0_state = feature_state(
        "P0_HISTORICAL_META",
        "P0_HISTORICAL_TILT",
        "THETA_ZERO_FALLBACK_OBSERVED",
    )
    p1_state = feature_state(
        "METAR_TRAJECTORY_LIKELIHOOD",
        "METAR_TRAJECTORY_UPDATE",
        "IDENTITY_LIKELIHOOD_FALLBACK_OBSERVED",
    )
    if candidate_activation_count:
        state = "BLOCKED_CANDIDATE_ACTIVATION"
    elif fallback_contract_failures:
        state = "BLOCKED_FALLBACK_CONTRACT"
    elif (
        malformed_or_oversized_proof_count
        or malformed_or_oversized_decision_count
    ):
        state = "BLOCKED_PROOF_INTEGRITY"
    elif (
        p0_state == "FORMAL_ACTIVE_AND_CONSUMED"
        and p1_state == "FORMAL_ACTIVE_AND_CONSUMED"
    ):
        state = "RUNNING_FORMAL_EVIDENCE"
    elif rows:
        state = "RUNNING_WITH_GOVERNED_FALLBACKS"
    else:
        state = "SCHEMA_READY_AWAITING_RESIDENT_PROOF"
    return {
        **base,
        "state": state,
        "formal_activation_counts": dict(
            sorted(formal_activation_counts.items())
        ),
        "proof_counts": dict(sorted(proof_counts.items())),
        "formal_consumption_counts": dict(
            sorted(formal_consumption_counts.items())
        ),
        "fallback_proof_counts": dict(
            sorted(fallback_proof_counts.items())
        ),
        "proof_rows_inspected": len(rows),
        "proof_row_cap_reached": capped,
        "malformed_or_oversized_proof_count": (
            malformed_or_oversized_proof_count
        ),
        "fallback_contract_failures": sorted(
            set(fallback_contract_failures)
        ),
        "continuous_core_proof_count": continuous_core_proof_count,
        "continuous_core_decision_count": continuous_core_decision_count,
        "direction_transition_decision_count": (
            direction_transition_decision_count
        ),
        "legacy_core_decision_count": legacy_core_decision_count,
        "decision_row_cap_reached": decision_cap_reached,
        "malformed_or_oversized_decision_count": (
            malformed_or_oversized_decision_count
        ),
        "candidate_artifact_counts": dict(
            sorted(candidate_artifact_counts.items())
        ),
        "candidate_rejected_count": candidate_rejected_count,
        "candidate_activation_count": candidate_activation_count,
        "candidate_activation_count_all": candidate_activation_count_all,
        "missing_bridge_tables": missing_bridge_tables,
        "probability_table_scope_counts": probability_table_scope_counts(),
        "p0_historical_state": p0_state,
        "p1_trajectory_state": p1_state,
    }


def _blocked_forward_scorecard(
    *,
    as_of_utc: str,
    reason: str,
    required_station_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Return an explicit non-authorizing scorecard gate when it cannot run."""

    return {
        "schema": "bayes_basket_target_forward_scorecard_gate_v1",
        "state": "BLOCKED",
        "ready": False,
        "as_of_utc": as_of_utc,
        "required_station_ids": list(required_station_ids),
        "observed_station_ids": [],
        "missing_station_ids": list(required_station_ids),
        "ready_for_cash_scorecard": False,
        "ready_for_station_scorecard": False,
        "blockers": [reason],
        "receipt": None,
        "production_no_live": True,
        "read_only": True,
        "orders_submitted": 0,
        "calls_order_gateway": False,
    }


def _forward_scorecard_status(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    station_ids: Sequence[str] | None,
) -> dict[str, Any]:
    """Build an observational forward scorecard.

    Forward rows are retained only for arrival/interface and timing
    diagnostics.  They are never OOS, never a production_no_live gate, and
    never a formal-promotion input.  Only a violation of the no-live/order
    boundary is escalated to the caller; missing forward samples remain
    diagnostic state.
    """

    requested = _normalize_station_scope(station_ids)
    if requested:
        required = requested
    else:
        try:
            registry_rows = load_started_registry_rows(conn, as_of_utc=as_of_utc)
            required = tuple(sorted({
                str(row.get("station_id") or "").strip().upper()
                for row in registry_rows
                if str(row.get("station_id") or "").strip()
            }))
        except Exception as exc:
            return _blocked_forward_scorecard(
                as_of_utc=as_of_utc,
                reason=f"forward_scorecard_registry_scope_invalid:{type(exc).__name__}",
            )

    try:
        receipt = build_forward_scorecard(
            conn,
            as_of_utc=as_of_utc,
            station_allowlist=required or None,
        )
    except Exception as exc:
        return _blocked_forward_scorecard(
            as_of_utc=as_of_utc,
            reason=f"forward_scorecard_invalid:{type(exc).__name__}:{exc}",
            required_station_ids=required,
        )

    counts = receipt.get("counts") if isinstance(receipt.get("counts"), Mapping) else {}
    by_station = counts.get("by_station") if isinstance(counts.get("by_station"), Mapping) else {}
    observed = tuple(sorted({str(key).strip().upper() for key in by_station if str(key).strip()}))
    missing = tuple(sorted(set(required) - set(observed)))
    blockers = {
        str(item)
        for item in (receipt.get("blockers") or [])
        if str(item).strip()
    }
    if not int(counts.get("fact_rows_evaluated") or 0):
        blockers.add("forward_scorecard_training_facts_missing")
    if missing:
        blockers.add("forward_scorecard_station_samples_missing:" + ",".join(missing))
    cash_ready = receipt.get("ready_for_cash_scorecard") is True
    station_ready = receipt.get("ready_for_station_scorecard") is True
    if not cash_ready:
        blockers.add("forward_scorecard_cash_not_ready")
    if not station_ready:
        blockers.add("forward_scorecard_station_not_ready")
    if receipt.get("production_no_live") is not True:
        blockers.add("forward_scorecard_not_production_no_live")
    if receipt.get("read_only") is not True:
        blockers.add("forward_scorecard_not_read_only")
    if receipt.get("calls_order_gateway") is not False:
        blockers.add("forward_scorecard_gateway_boundary_invalid")
    if receipt.get("orders_submitted") != 0:
        blockers.add("forward_scorecard_order_boundary_invalid")
    ready = (
        receipt.get("schema") == FORWARD_SCORECARD_SCHEMA
        and cash_ready
        and station_ready
        and not blockers
        and not missing
        and receipt.get("production_no_live") is True
        and receipt.get("read_only") is True
        and receipt.get("calls_order_gateway") is False
        and receipt.get("orders_submitted") == 0
    )
    return {
        "schema": "bayes_basket_target_forward_scorecard_gate_v1",
        "state": "READY" if ready else "BLOCKED",
        "ready": ready,
        "as_of_utc": as_of_utc,
        "required_station_ids": list(required),
        "observed_station_ids": list(observed),
        "missing_station_ids": list(missing),
        "ready_for_cash_scorecard": cash_ready,
        "ready_for_station_scorecard": station_ready,
        "blockers": sorted(blockers),
        "receipt": receipt,
        "receipt_content_sha256": receipt.get("content_sha256"),
        "production_no_live": receipt.get("production_no_live") is True,
        "read_only": receipt.get("read_only") is True,
        "orders_submitted": receipt.get("orders_submitted", 0),
        "calls_order_gateway": receipt.get("calls_order_gateway"),
    }


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return None


def _latest_flag(conn: sqlite3.Connection, table: str, column: str, value: int) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (value,)).fetchone()[0])
    except sqlite3.Error:
        return 0


def _count_where(conn: sqlite3.Connection, table: str, predicate: str, params: tuple[Any, ...] = ()) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {predicate}", params).fetchone()[0])
    except sqlite3.Error:
        return 0


def _required_materialized_counts_ready(
    counts: Mapping[str, int | None],
) -> bool:
    return all(
        int(count or 0) > 0
        for table, count in counts.items()
        if table not in OPTIONAL_MATERIALIZED_INPUT_TABLES
    )


def _activation(path: Path) -> tuple[dict[str, Any], list[str]]:
    try:
        value = load_target_activation(path)
    except Exception as exc:
        return {}, [f"file_activation_invalid:{type(exc).__name__}:{exc}"]
    blockers: list[str] = []
    if value.get("execution_mode") != "production_no_live":
        blockers.append("file_activation_not_production_no_live")
    for key in ("live_decision_write_authorized", "live_submit_authorized", "places_orders", "calls_order_gateway"):
        if value.get(key) is not False:
            blockers.append(f"file_activation_{key}_must_be_false")
    scope = value.get("target_scope")
    if not isinstance(scope, Mapping):
        blockers.append("file_activation_target_scope_missing")
    else:
        blockers.extend(
            f"file_activation_target_scope_missing:{key}"
            for key in REQUIRED_SCOPE_KEYS
            if not str(scope.get(key) or "").strip()
        )
    return value, blockers


def _db_activation_alignment(conn: sqlite3.Connection, file_activation: Mapping[str, Any]) -> dict[str, Any]:
    """Compare the active DB row without assuming the target migration ran."""

    tables = _table_names(conn)
    if "basket_strategy_activation_state" not in tables:
        return {"state": "missing", "aligned": False, "reason": "db_activation_table_missing"}
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(basket_strategy_activation_state)")
    }
    contract_column = next(
        (name for name in ("activation_contract_sha256", "activation_sha256") if name in columns),
        None,
    )
    json_column = next(
        (name for name in ("details_json", "activation_json") if name in columns),
        None,
    )
    if contract_column is None or json_column is None:
        return {
            "state": "invalid_schema",
            "aligned": False,
            "reason": "db_activation_columns_missing",
            "columns": sorted(columns),
        }
    row = conn.execute(
        f"SELECT {contract_column},{json_column} FROM basket_strategy_activation_state "
        "WHERE state='enabled' ORDER BY generation DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {"state": "missing", "aligned": False, "reason": "db_activation_enabled_row_missing"}
    try:
        details = json.loads(str(row[1] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"state": "invalid", "aligned": False, "reason": "db_activation_details_invalid"}
    expected = {
        "activation_contract_sha256": str(file_activation.get("activation_contract_sha256") or ""),
        "execution_mode": str(file_activation.get("execution_mode") or ""),
        "live_submit_authorized": file_activation.get("live_submit_authorized"),
        "places_orders": file_activation.get("places_orders"),
        "calls_order_gateway": file_activation.get("calls_order_gateway"),
    }
    observed = {
            "activation_contract_sha256": str(row[0] or ""),
        "execution_mode": str(details.get("execution_mode") or ""),
        "live_submit_authorized": details.get("live_submit_authorized"),
        "places_orders": details.get("places_orders"),
        "calls_order_gateway": details.get("calls_order_gateway"),
    }
    mismatches = sorted(key for key in expected if expected[key] != observed[key])
    return {
        "state": "checked",
        "aligned": not mismatches,
        "mismatches": mismatches,
        "expected": expected,
        "observed": observed,
    }


def _event_day_row_counts(
    conn: sqlite3.Connection,
    table: str,
    *,
    as_of_utc: str,
) -> dict[str, int]:
    """Partition rows by each row's registry settlement timezone."""

    try:
        rows = conn.execute(
            f"SELECT event_date,settlement_timezone,COUNT(*) "
            f"FROM {table} GROUP BY event_date,settlement_timezone"
        ).fetchall()
    except sqlite3.Error:
        return {"total": 0, "started_settlement": 0, "future_settlement": 0, "invalid": 0}
    counts = {"total": 0, "started_settlement": 0, "future_settlement": 0, "invalid": 0}
    for event_date, settlement_timezone, row_count in rows:
        amount = int(row_count or 0)
        counts["total"] += amount
        try:
            state = event_day_window(
                str(event_date or ""),
                as_of_utc=as_of_utc,
                target_timezone=str(settlement_timezone or ""),
            )["state"]
        except Exception:
            counts["invalid"] += amount
            continue
        if state == "not_started":
            counts["future_settlement"] += amount
        else:
            counts["started_settlement"] += amount
    return counts


def _target_inputs(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str | None = None,
    policy_authority_scope: str = "PRODUCTION_NO_LIVE_CANDIDATE",
    target_scope: Mapping[str, Any] | None = None,
    station_ids: Sequence[str] = (),
) -> dict[str, Any]:
    decision_clock = as_of_utc or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tables = _table_names(conn)
    counts = {table: _count(conn, table) for table in TARGET_INPUT_TABLES}
    missing = sorted(set(TARGET_INPUT_TABLES) - tables)
    promoted = {
        "cash_phase": _latest_flag(conn, "basket_cash_phase_policy_artifact", "promoted", 1),
        "calibration_no_live_authority": _count_where(
            conn,
            "calibration_activation",
            "authority_scope='PRODUCTION_NO_LIVE_ONLY'",
        ),
        "station_policy_promotion": _count_where(
            conn, "basket_policy_promotion", "decision='PROMOTED'"
        ),
    }
    no_live_candidate_authority = (
        str(policy_authority_scope or "").strip().upper()
        == "PRODUCTION_NO_LIVE_CANDIDATE"
    )
    cash_authority_scope = (
        "PRODUCTION_NO_LIVE_CANDIDATE"
        if no_live_candidate_authority
        else "FORMAL_PROMOTED"
    )
    cash_authority_count = 0
    if {
        "basket_cash_phase_policy_artifact",
        "basket_cash_phase_authority_manifest",
    }.issubset(tables):
        promotion_predicate = "p.promoted=0" if no_live_candidate_authority else "p.promoted=1"
        cash_authority_count = int(conn.execute(
            "SELECT COUNT(*) FROM basket_cash_phase_authority_manifest m "
            "JOIN basket_cash_phase_policy_artifact p "
            "ON p.policy_version=m.policy_version "
            "AND p.content_sha256=m.artifact_sha256 "
            "AND p.station_id=m.station_id "
            "AND p.station_timezone=m.station_timezone "
            "AND p.valid_from_utc=m.valid_from_utc "
            "AND p.valid_until_utc=m.valid_until_utc "
            "WHERE m.authority_scope=? AND m.valid_from_utc<=? "
            "AND m.valid_until_utc>? AND " + promotion_predicate,
            (cash_authority_scope, decision_clock, decision_clock),
        ).fetchone()[0])
    policy_authority = {
        "cash_phase": cash_authority_count,
        "station_policy": (
            min(
                _count_where(
                    conn,
                    "basket_global_prior_artifact",
                    "global_prior_id LIKE 'target-no-live-global-prior-design-v%'",
                ),
                _count(conn, "basket_policy_activation") or 0,
            )
            if no_live_candidate_authority
            else promoted["station_policy_promotion"]
        ),
    }
    aggregate_ready = (
        not missing
        and _required_materialized_counts_ready(counts)
        and int(promoted["calibration_no_live_authority"] or 0) > 0
        and all(int(value or 0) > 0 for value in policy_authority.values())
    )
    try:
        exact_scope_readiness = resolve_target_readiness_scopes(
            conn,
            decision_at_utc=decision_clock,
            target_scope=target_scope or {},
            authority_scope=policy_authority_scope,
            station_ids=station_ids,
        )
    except Exception as exc:
        exact_scope_readiness = {
            "schema": "bayes_basket_target_exact_scope_readiness_v1",
            "ready": False,
            "scope_count": 0,
            "exact_scope_count": 0,
            "blocked_scope_count": 0,
            "items": [],
            "reason_codes": [f"{type(exc).__name__}:{exc}"],
            "boundary": {
                "execution_mode": "production_no_live",
                "read_only": True,
                "places_orders": False,
                "orders_submitted": 0,
                "calls_order_gateway": False,
            },
        }
    ready = aggregate_ready and exact_scope_readiness.get("ready") is True
    probability_stage_counts: dict[str, int] = {}
    probability_started_settlement_stage_counts: dict[str, int] = {}
    if "basket_probability_envelope" in tables:
        probability_stage_counts = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT stage,COUNT(*) FROM basket_probability_envelope "
                "GROUP BY stage"
            )
        }
        for stage, event_date, settlement_timezone in conn.execute(
            "SELECT stage,event_date,settlement_timezone "
            "FROM basket_probability_envelope"
        ):
            try:
                state = event_day_window(
                    str(event_date or ""),
                    as_of_utc=decision_clock,
                    target_timezone=str(settlement_timezone or ""),
                )["state"]
            except Exception:
                continue
            if state != "not_started":
                stage_name = str(stage)
                probability_started_settlement_stage_counts[stage_name] = (
                    probability_started_settlement_stage_counts.get(stage_name, 0)
                    + 1
                )
    return {
        "ready": ready,
        "aggregate_counts_ready": aggregate_ready,
        "exact_scope_readiness": exact_scope_readiness,
        "missing_tables": missing,
        "counts": counts,
        "promoted_or_activated_counts": promoted,
        "policy_authority_scope": (
            "PRODUCTION_NO_LIVE_CANDIDATE"
            if no_live_candidate_authority
            else "FORMAL_PROMOTED"
        ),
        "policy_authority_counts": policy_authority,
        "probability_materialized": int(counts.get("basket_probability_envelope") or 0) > 0,
        "probability_stage_counts": probability_stage_counts,
        "probability_started_settlement_stage_counts": probability_started_settlement_stage_counts,
        "p0_preselection_materialized": int(counts.get("basket_p0_preselection_plan") or 0) > 0,
        "official_ws_materialized": int(counts.get("basket_official_ws_snapshot") or 0) > 0,
        "market_registry_materialized": int(counts.get("basket_target_market_registry_snapshot") or 0) > 0,
        "q0_inventory_materialized": int(counts.get("basket_confirmed_inventory_snapshot") or 0) > 0,
        "order_state_materialized": int(counts.get("basket_order_state_snapshot") or 0) > 0,
        "cash_phase_promoted": int(promoted["cash_phase"] or 0) > 0,
        "station_policy_promoted": int(promoted["station_policy_promotion"] or 0) > 0,
        "cash_phase_authorized": int(policy_authority["cash_phase"] or 0) > 0,
        "station_policy_authorized": int(policy_authority["station_policy"] or 0) > 0,
        "calibration_no_live_active": int(promoted["calibration_no_live_authority"] or 0) > 0,
        "operational_counts": {
            "observation_facts": _count(conn, "basket_target_observation_facts"),
            "bound_facts": _count(conn, "basket_target_bound_facts"),
            "execution_cycles": _count(conn, "basket_target_execution_cycles"),
            "decisions": _count(conn, "auto_trade_decisions"),
            "non_blocked_decisions": _count_where(
                conn,
                "auto_trade_decisions",
                "decided_state<>'blocked' OR will_order<>0",
            ),
            "order_intents": _count(conn, "basket_target_order_intents"),
            "capital_reservations": _count(
                conn,
                "basket_target_capital_reservations",
            ),
            "order_events": _count(conn, "basket_target_order_events"),
            "reconciliations": _count(conn, "basket_target_order_reconciliations"),
        },
        "operational_event_day_counts": {
            "execution_cycles": _event_day_row_counts(
                conn, "basket_target_execution_cycles", as_of_utc=decision_clock
            ),
            "decisions": _event_day_row_counts(
                conn, "auto_trade_decisions", as_of_utc=decision_clock
            ),
        },
        "decision_clock_utc": decision_clock,
    }


def _main_chain_status(
    inputs: Mapping[str, Any],
    *,
    calibration_evidence: Mapping[str, Any],
    native_decode_evidence: Mapping[str, Any],
    ws_coverage: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the only executable target chain in dependency order.

    This is observability only: it never promotes an artifact, creates a
    decision, or treats evidence-cache rows as a materialized market input.
    The first blocked node is explicit so downstream zero counts cannot be
    mistaken for an uninstrumented success.
    """

    counts = inputs.get("counts") if isinstance(inputs.get("counts"), Mapping) else {}
    operational = inputs.get("operational_counts") if isinstance(inputs.get("operational_counts"), Mapping) else {}
    checks = calibration_evidence.get("checks") if isinstance(calibration_evidence.get("checks"), Mapping) else {}
    et_history = calibration_evidence.get("et_history_evidence")
    et_state = et_history.get("state") if isinstance(et_history, Mapping) else "MISSING"
    facts_ready = int(operational.get("observation_facts") or 0) > 0
    # A complete decoded curve is only provider-local evidence.  It must not
    # make the three-model stage ready when the exact provider set/run
    # contract is incomplete (for example, a forward ICON curve from a
    # different run generation).  Otherwise future/prospective evidence can
    # appear to have unlocked the historical chain.
    models_ready = (
        int(native_decode_evidence.get("complete_curve_count") or 0) > 0
        and checks.get("model_provider_set_complete") is True
    )
    calibration_ready = bool(inputs.get("calibration_no_live_active"))
    probability_any_ready = (
        calibration_ready
        and int(counts.get("basket_probability_envelope") or 0) > 0
    )
    current_stage_counts = (
        inputs.get("probability_started_settlement_stage_counts")
        if isinstance(
            inputs.get("probability_started_settlement_stage_counts"),
            Mapping,
        )
        else {}
    )
    current_p1_count = int(current_stage_counts.get("P1") or 0)
    probability_ready = probability_any_ready and current_p1_count > 0
    ws_incomplete_count = int(ws_coverage.get("incomplete_scope_count") or 0)
    ws_ready = (
        int(counts.get("basket_official_ws_snapshot") or 0) > 0
        and int(ws_coverage.get("complete_scope_count") or 0) > 0
        and ws_incomplete_count == 0
    )
    event_day_counts = inputs.get("operational_event_day_counts")
    cycle_day_counts = event_day_counts.get("execution_cycles") if isinstance(event_day_counts, Mapping) else {}
    decision_day_counts = event_day_counts.get("decisions") if isinstance(event_day_counts, Mapping) else {}
    cycle_started_count = (
        int(cycle_day_counts.get("started_settlement") or 0)
        if isinstance(cycle_day_counts, Mapping)
        else int(operational.get("execution_cycles") or 0)
    )
    decision_started_count = (
        int(decision_day_counts.get("started_settlement") or 0)
        if isinstance(decision_day_counts, Mapping)
        else int(operational.get("decisions") or 0)
    )
    # A retained/prepared cycle is an audit residue, not evidence that the
    # basket stage completed.  Require every L3/L4 input before projecting a
    # basket as ready; this prevents old blocked cycles (including future ET
    # rows) from masking the actual upstream blocker.
    exact_scope_readiness = (
        inputs.get("exact_scope_readiness")
        if isinstance(inputs.get("exact_scope_readiness"), Mapping)
        else {}
    )
    basket_inputs_ready = all(
        (
            exact_scope_readiness.get("ready") is True,
            bool(inputs.get("station_policy_authorized", inputs.get("station_policy_promoted"))),
            bool(inputs.get("cash_phase_authorized", inputs.get("cash_phase_promoted"))),
            bool(inputs.get("q0_inventory_materialized")),
            bool(inputs.get("order_state_materialized")),
        )
    )
    basket_ready = (
        probability_ready
        and ws_ready
        and basket_inputs_ready
        and cycle_started_count > 0
    )
    decision_ready = basket_ready and decision_started_count > 0
    no_downstream_reconciliation_required = (
        decision_started_count > 0
        and int(operational.get("non_blocked_decisions") or 0) == 0
        and int(operational.get("order_intents") or 0) == 0
        and int(operational.get("capital_reservations") or 0) == 0
        and int(operational.get("order_events") or 0) == 0
    )
    reconcile_ready = (
        int(operational.get("reconciliations") or 0) > 0
        or no_downstream_reconciliation_required
    )

    stages = [
        {
            "id": "OFFICIAL_FACTS",
            "label": "官方天气事实",
            "state": "READY_EVIDENCE" if facts_ready else "MISSING",
            "ready": facts_ready,
            "observed": {"observation_facts": operational.get("observation_facts", 0)},
        },
        {
            "id": "THREE_MODEL",
            "label": "ECMWF/GFS/ICON 三模型预测",
            "state": "READY_EVIDENCE" if models_ready else "MISSING",
            "ready": models_ready,
            "observed": {
                "complete_curve_count": native_decode_evidence.get("complete_curve_count", 0),
                "model_provider_set_complete": checks.get("model_provider_set_complete"),
            },
        },
        {
            "id": "SETTLEMENT_ALIGN",
            "label": "market_registry 结算日界 exact join",
            "state": "READY_FOR_STRICT_ASOF" if et_state == "READY_FOR_STRICT_ASOF" else "BLOCKED_EVIDENCE",
            "ready": et_state == "READY_FOR_STRICT_ASOF",
            "observed": {
                "state": et_state,
                "joined_station_days": et_history.get("joined_station_days", 0) if isinstance(et_history, Mapping) else 0,
            },
        },
        {
            "id": "CALIBRATION_ENSEMBLE",
            "label": "模型校准与组合",
            "state": "ACTIVE_NO_LIVE_ONLY" if calibration_ready else "BLOCKED",
            "ready": calibration_ready,
            "observed": {
                "calibration_model_artifact": counts.get("calibration_model_artifact", 0),
                "calibration_ensemble_artifact": counts.get("calibration_ensemble_artifact", 0),
                "calibration_activation": counts.get("calibration_activation", 0),
            },
        },
        {
            "id": "P0_P1",
            "label": "P0/P1 温度概率",
            "state": "MATERIALIZED" if probability_ready else "BLOCKED",
            "ready": probability_ready,
            "observed": {
                "basket_probability_envelope": counts.get(
                    "basket_probability_envelope",
                    0,
                ),
                "started_settlement_p1_count": current_p1_count,
                "stage_counts": inputs.get("probability_stage_counts", {}),
            },
        },
        {
            "id": "OFFICIAL_WS",
            "label": "官方 CLOB WS 盘口快照",
            "state": "MATERIALIZED" if ws_ready else "BLOCKED",
            "ready": ws_ready,
            "observed": {
                "basket_official_ws_snapshot": counts.get("basket_official_ws_snapshot", 0),
                "complete_scope_count": ws_coverage.get("complete_scope_count", 0),
                "incomplete_scope_count": ws_coverage.get("incomplete_scope_count", 0),
            },
        },
        {
            "id": "BASKET",
            "label": "篮子计算与持有计划",
            "state": "CANDIDATE_WRITTEN" if basket_ready else "BLOCKED",
            "ready": basket_ready,
            "observed": {
                "exact_scope_ready": exact_scope_readiness.get("ready"),
                "exact_scope_count": exact_scope_readiness.get(
                    "scope_count", 0
                ),
                "exact_scope_blocked_count": exact_scope_readiness.get(
                    "blocked_scope_count", 0
                ),
                "execution_cycles": operational.get("execution_cycles", 0),
                "execution_cycles_started_settlement": cycle_day_counts.get("started_settlement", 0)
                if isinstance(cycle_day_counts, Mapping) else operational.get("execution_cycles", 0),
                "execution_cycles_future_settlement": cycle_day_counts.get("future_settlement", 0)
                if isinstance(cycle_day_counts, Mapping) else 0,
            },
        },
        {
            "id": "DECISION_RECONCILE",
            "label": "no-live 决策与对账",
            "state": (
                "NO_RECONCILIATION_REQUIRED"
                if no_downstream_reconciliation_required
                else "WRITTEN"
                if decision_ready and reconcile_ready
                else "BLOCKED"
            ),
            "ready": (
                no_downstream_reconciliation_required
                or (decision_ready and reconcile_ready)
            ),
            "observed": {
                "decisions": operational.get("decisions", 0),
                "decisions_started_settlement": decision_day_counts.get("started_settlement", 0)
                if isinstance(decision_day_counts, Mapping) else operational.get("decisions", 0),
                "decisions_future_settlement": decision_day_counts.get("future_settlement", 0)
                if isinstance(decision_day_counts, Mapping) else 0,
                "non_blocked_decisions": operational.get("non_blocked_decisions", 0),
                "order_intents": operational.get("order_intents", 0),
                "capital_reservations": operational.get("capital_reservations", 0),
                "order_events": operational.get("order_events", 0),
                "reconciliations": operational.get("reconciliations", 0),
            },
        },
    ]
    blocked_at = next((stage["id"] for stage in stages if not stage["ready"]), None)
    p0_preselection_count = int(counts.get("basket_p0_preselection_plan") or 0)
    preliminary_p0_ready = (
        probability_any_ready and p0_preselection_count > 0
    )
    return {
        "schema": "bayes_basket_target_main_chain_status_v1",
        "order": [stage["id"] for stage in stages],
        "stages": stages,
        "blocked_at": blocked_at,
        "ready": blocked_at is None,
        "preliminary_p0_chain": {
            "ready": preliminary_p0_ready,
            "state": "PRELIMINARY_NO_LIVE" if preliminary_p0_ready else "BLOCKED",
            "preselection_plan_count": p0_preselection_count,
            "final_authority": "P1",
            "capital_authorized": False,
            "intents_written": 0,
            "reservations_written": 0,
            "orders_submitted": 0,
        },
        "final_p1_chain": {
            "ready": blocked_at is None,
            "blocked_at": blocked_at,
            "requires_auto_trade_decision": True,
        },
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
    }


def _unfinished_target_inputs(
    inputs: Mapping[str, Any],
    *,
    ws_coverage: Mapping[str, Any],
    calibration_evidence: Mapping[str, Any],
    native_decode_evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Expose each missing real-library gate instead of one opaque blocker."""

    counts = inputs.get("counts") if isinstance(inputs.get("counts"), Mapping) else {}
    items: list[dict[str, Any]] = []

    def add(
        code: str,
        requirement: str,
        tables: tuple[str, ...],
        *,
        observed: Any,
        evidence: str,
        state: str = "missing",
    ) -> None:
        items.append({
            "code": code,
            "requirement": requirement,
            "required_tables": list(tables),
            "observed": observed,
            "state": state,
            "evidence": evidence,
        })

    table_requirements = (
        (
            "probability_materialization",
            "真实严格 as-of P0/P1 probability envelope",
            ("basket_probability_envelope",),
            "probability_materialized",
        ),
        (
            "official_ws_materialization",
            "目标 scope 的官方 CLOB WS 逐桶 bid/ask 快照",
            ("basket_official_ws_snapshot",),
            "official_ws_materialized",
        ),
        (
            "q0_inventory_materialization",
            "与同一 market snapshot 对齐的 q=0 inventory snapshot",
            ("basket_confirmed_inventory_snapshot",),
            "q0_inventory_materialized",
        ),
        (
            "order_state_materialization",
            "production_no_live order-state snapshot",
            ("basket_order_state_snapshot",),
            "order_state_materialized",
        ),
        (
            "cash_phase_authority",
            "当前 policy_authority_scope 下唯一 exact manifest 绑定的 BasketCashPhasePolicyArtifact",
            (
                "basket_cash_phase_policy_artifact",
                "basket_cash_phase_authority_manifest",
            ),
            "cash_phase_authorized",
        ),
        (
            "station_policy_authority",
            "当前 policy_authority_scope 下可解析的 global prior + station policy activation",
            (
                "basket_global_prior_artifact",
                "basket_station_policy_artifact",
                "basket_policy_activation",
            ),
            "station_policy_authorized",
        ),
    )
    for code, requirement, tables, ready_key in table_requirements:
        ready_value = inputs.get(ready_key)
        if ready_value is None and ready_key.endswith("_authorized"):
            ready_value = inputs.get(ready_key.replace("_authorized", "_promoted"))
        if not bool(ready_value):
            observed = {table: counts.get(table) for table in tables}
            add(
                code,
                requirement,
                tables,
                observed=observed,
                evidence="target_ssot_counts_and_promotion_state",
            )
    current_stage_counts = (
        inputs.get("probability_started_settlement_stage_counts")
        if isinstance(
            inputs.get("probability_started_settlement_stage_counts"),
            Mapping,
        )
        else {}
    )
    if int(current_stage_counts.get("P1") or 0) <= 0:
        add(
            "started_settlement_p1_materialization",
            "当前已开始市场结算日的最终权威P1 probability envelope",
            ("basket_probability_envelope",),
            observed={
                "started_settlement_stage_counts": dict(current_stage_counts),
                "all_stage_counts": inputs.get(
                    "probability_stage_counts",
                    {},
                ),
            },
            evidence="target_ssot_stage_and_event_date_exact_count",
        )

    if not bool(inputs.get("calibration_no_live_active")):
        add(
            "calibration_no_live_authority",
            "三模型 calibration + ensemble artifact 以 PRODUCTION_NO_LIVE_ONLY 激活",
            (
                "calibration_model_artifact",
                "calibration_ensemble_artifact",
                "calibration_activation",
            ),
            observed={table: counts.get(table) for table in (
                "calibration_model_artifact",
                "calibration_ensemble_artifact",
                "calibration_activation",
            )},
            evidence=str(calibration_evidence.get("state") or "calibration_evidence_missing"),
        )
    et_history = calibration_evidence.get("et_history_evidence")
    if isinstance(et_history, Mapping) and et_history.get("state") != "READY_FOR_STRICT_ASOF":
        add(
            "et_history_compatibility",
            "固定 ET 日界下的三模型/AMSC-AWC canonical history exact join",
            (),
            observed={
                "state": et_history.get("state"),
                "joined_station_days": et_history.get("joined_station_days", 0),
                "receipt_sha256": et_history.get("receipt_sha256"),
            },
            evidence=str(et_history.get("path") or "bayes_basket_target_et_history_evidence"),
            state="blocked_evidence",
        )
    checks = calibration_evidence.get("checks")
    native_complete = int(native_decode_evidence.get("complete_curve_count") or 0)
    # A blocked historical calibration receipt may still carry a valid,
    # complete prospective native decode.  Do not turn that receipt-level
    # calibration blocker into a duplicate "arrival missing" blocker; the ET
    # exact-join item below remains the authoritative historical blocker.
    if (
        isinstance(checks, Mapping)
        and checks.get("model_provider_set_complete") is False
        and native_complete <= 0
    ):
        add(
            "three_model_arrival_evidence",
            "ECMWF/GFS/ICON 三模型 arrival 与 strict-as-of join 完整",
            (),
            observed={
                "model_provider_set_complete": checks.get("model_provider_set_complete"),
                "three_model_available": checks.get("three_model_available"),
            },
            evidence="bayes_basket_target_calibration_evidence",
            state="blocked_evidence",
        )

    # ``_native_decode_evidence`` deliberately returns a compact, validated
    # projection rather than the raw receipt.  Read the normalized fields
    # here; looking for a nested ``curves`` object silently converted a valid
    # partial decode (one complete curve) into an observed count of zero.
    native_points = int(native_decode_evidence.get("decoded_point_count") or 0)
    if native_complete <= 0:
        add(
            "native_model_curve_materialization",
            "三模型 native GRIB 解码并覆盖完整目标本地日曲线",
            (),
            observed={
                "decoded_point_count": native_points,
                "complete_curve_count": native_complete,
                "state": native_decode_evidence.get("state"),
            },
            evidence=str(native_decode_evidence.get("path") or "native_model_decode_evidence"),
            state="blocked_evidence" if native_decode_evidence.get("state") == "VALID_BLOCKED_NATIVE_DECODE_EVIDENCE" else "missing",
        )

    incomplete_scope_count = int(ws_coverage.get("incomplete_scope_count") or 0)
    if incomplete_scope_count:
        add(
            "official_ws_scope_coverage",
            "target registry scope 全部具备完整官方 WS bid/ask 证据",
            ("basket_target_market_registry_snapshot",),
            observed={
                "complete_scope_count": int(ws_coverage.get("complete_scope_count") or 0),
                "incomplete_scope_count": incomplete_scope_count,
                "pending_shards": list(ws_coverage.get("pending_shards") or []),
            },
            evidence="bayes_basket_target_official_ws_manifest.json + target registry",
            state="partial",
        )
    return items


def _capital_pool(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        rows = conn.execute(
            "SELECT pool_id,balance_usdc,reserved_usdc,capital_epoch_id,version FROM basket_target_capital_pool"
        ).fetchall()
    except sqlite3.Error:
        return {"ready": False, "state": "missing"}
    if len(rows) != 1:
        return {"ready": False, "state": "invalid_row_count", "row_count": len(rows)}
    row = rows[0]
    valid = (
        int(row[0]) == 1
        and float(row[1]) >= 0
        and 0 <= float(row[2]) <= float(row[1])
        and bool(str(row[3] or "").strip())
        and int(row[4]) >= 0
    )
    return {
        "ready": valid,
        "state": "ready" if valid else "invalid",
        "pool_id": row[0],
        "balance_usdc": row[1],
        "reserved_usdc": row[2],
        "capital_epoch_id_present": bool(str(row[3] or "").strip()),
        "version": row[4],
    }


def _calibration_evidence(path: Path) -> dict[str, Any]:
    """Read candidate evidence without allowing it to satisfy readiness."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return {"state": "missing", "path": str(resolved), "promotion_allowed": False}
    try:
        payload = load_bounded_json(resolved)
        if not isinstance(payload, Mapping):
            raise ValueError("mapping_required")
        supplied = str(payload.get("receipt_sha256") or "")
        identity = {key: value for key, value in payload.items() if key != "receipt_sha256"}
        if payload.get("schema") != "bayes_basket_target_calibration_evidence_v1":
            raise ValueError("schema_invalid")
        if supplied != content_sha256(identity):
            raise ValueError("receipt_sha256_mismatch")
        checks = payload.get("checks")
        boundary = payload.get("boundary")
        if not isinstance(checks, Mapping) or not isinstance(boundary, Mapping):
            raise ValueError("checks_boundary_missing")
        artifacts = list(payload.get("calibration_artifacts") or []) + list(payload.get("ensemble_artifacts") or [])
        decisions = list(payload.get("promotion_decisions") or [])
        candidate_only = bool(artifacts) and all(item.get("promoted") is False for item in artifacts)
        rejected_only = bool(decisions) and all(item.get("decision") == "REJECTED" for item in decisions)
        checks_ok = isinstance(checks, Mapping)
        blocked_without_candidates = (
            not artifacts
            and not decisions
            and checks_ok
            and (
                checks.get("residual_count_positive") is True
                # A strict three-model join is allowed to block before it can
                # produce residuals.  Keep this explicit so a missing model
                # cannot be mistaken for a valid calibration candidate.
                or checks.get("model_provider_set_complete") is False
            )
            and checks.get("candidate_artifact_count_positive") is False
            and checks.get("promotion_allowed") is False
        )
        safe_boundary = (
            checks.get("promotion_allowed") is False
            and checks.get("writes_production_db") is False
            and checks.get("calls_order_gateway") is False
            and boundary.get("live_order_authorized") is False
        )
        return {
            "state": (
                "VALID_CANDIDATE_EVIDENCE"
                if candidate_only and rejected_only and safe_boundary
                else "VALID_BLOCKED_EVIDENCE"
                if blocked_without_candidates and safe_boundary
                else "INVALID"
            ),
            "path": str(resolved),
            "receipt_sha256": supplied,
            "calibration_artifact_count": len(payload.get("calibration_artifacts") or []),
            "ensemble_artifact_count": len(payload.get("ensemble_artifacts") or []),
            "promotion_decision_count": len(decisions),
            "candidate_only": candidate_only,
            "rejected_only": rejected_only,
            "blocked_without_candidates": blocked_without_candidates,
            "promotion_allowed": checks.get("promotion_allowed"),
            "writes_production_db": checks.get("writes_production_db"),
            "calls_order_gateway": checks.get("calls_order_gateway"),
            "checks": {
                key: checks.get(key)
                for key in (
                    "model_provider_set_complete",
                    "three_model_available",
                    "published_receive_timestamps_complete",
                    "truth_lineage_complete",
                    "strict_asof_replay_pass",
                    "candidate_artifact_count_positive",
                    "oos_promotion_gate_complete",
                )
            },
            "et_history_evidence": dict(payload.get("et_history_evidence"))
            if isinstance(payload.get("et_history_evidence"), Mapping)
            else {"state": "missing", "promotion_allowed": False},
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "state": "INVALID",
            "path": str(resolved),
            "promotion_allowed": False,
            "reason": f"{type(exc).__name__}:{exc}",
        }


def _native_decode_evidence(path: Path) -> dict[str, Any]:
    """Read native point/curve evidence without treating it as promotion."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return {"state": "missing", "path": str(resolved), "promotion_allowed": False}
    try:
        payload = load_bounded_json(resolved)
        if not isinstance(payload, Mapping) or payload.get("schema") != "bayes_basket_target_native_model_arrival_decode_evidence_v1":
            raise ValueError("schema_invalid")
        supplied = str(payload.get("receipt_sha256") or "")
        identity = {key: value for key, value in payload.items() if key != "receipt_sha256"}
        if supplied != content_sha256(identity):
            raise ValueError("receipt_sha256_mismatch")
        checks = payload.get("checks")
        boundary = payload.get("boundary")
        curves = payload.get("curves")
        if not isinstance(checks, Mapping) or not isinstance(boundary, Mapping) or not isinstance(curves, Mapping):
            raise ValueError("checks_boundary_curves_missing")
        safe_boundary = (
            checks.get("promotion_allowed") is False
            and checks.get("writes_production_db") is False
            and checks.get("calls_order_gateway") is False
            and boundary.get("live_order_authorized") is False
        )
        complete_count = int(curves.get("complete_curve_count") or 0)
        return {
            "state": "VALID_NATIVE_DECODE_EVIDENCE" if safe_boundary and complete_count > 0 else "VALID_BLOCKED_NATIVE_DECODE_EVIDENCE" if safe_boundary else "INVALID",
            "path": str(resolved),
            "receipt_sha256": supplied,
            "decoded_point_count": int(payload.get("decoded_point_count") or 0),
            "complete_curve_count": complete_count,
            "curve_count": int(curves.get("curve_count") or 0),
            "promotion_allowed": checks.get("promotion_allowed"),
            "writes_production_db": checks.get("writes_production_db"),
            "calls_order_gateway": checks.get("calls_order_gateway"),
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "state": "INVALID",
            "path": str(resolved),
            "promotion_allowed": False,
            "reason": f"{type(exc).__name__}:{exc}",
        }


def _evidence_clock(path: Path) -> datetime:
    """Read the strict-as-of clock used to rank a validated receipt."""

    try:
        payload = load_bounded_json(path.expanduser().resolve())
        raw = str(payload.get("as_of_utc") or "") if isinstance(payload, Mapping) else ""
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(timezone.utc)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _resolve_latest_valid_evidence(
    path: Path,
    *,
    default_path: Path,
    glob_pattern: str,
    parser: Any,
    valid_states: set[str],
) -> Path:
    """Avoid stale default receipts while preserving explicit path choices.

    The refresh coordinator writes a stable default filename, while each
    repair/recheck writes a content-addressed dated receipt.  When the caller
    uses the default, choose the newest *validated* strict-as-of receipt.  A
    caller-supplied path remains authoritative and is never silently replaced.
    """

    resolved = path.expanduser().resolve()
    if resolved != default_path.expanduser().resolve():
        return resolved
    candidates = {resolved}
    try:
        candidates.update(ROOT.joinpath("output", "audits").glob(glob_pattern))
    except OSError:
        pass
    ranked: list[tuple[datetime, float, str, Path]] = []
    for candidate in candidates:
        candidate = candidate.expanduser().resolve()
        if not candidate.is_file():
            continue
        parsed = parser(candidate)
        if str(parsed.get("state") or "") not in valid_states:
            continue
        try:
            modified = float(candidate.stat().st_mtime)
        except OSError:
            modified = 0.0
        ranked.append((_evidence_clock(candidate), modified, str(candidate), candidate))
    if not ranked:
        return resolved
    return max(ranked, key=lambda item: (item[0], item[1], item[2]))[3]


def _normalize_station_scope(station_ids: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip().upper() for value in (station_ids or []) if str(value).strip()}))


def _calibration_staleness(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    station_ids: Sequence[str] | None = None,
    max_stale_days: int = DEFAULT_CALIBRATION_MAX_STALE_DAYS,
) -> dict[str, Any]:
    """Report stations whose model weights stopped being refreshed.

    The OOS gate is fail-closed on purpose: a station whose candidates keep
    losing to climatology goes on serving the last weights that did pass.
    That is the right execution behaviour and an invisible operational one --
    nothing else in this audit distinguishes a station refreshed yesterday
    from one frozen a fortnight ago, so a stalled station is only ever found
    by reading the table by hand.
    """

    scope = _normalize_station_scope(station_ids)
    try:
        rows = conn.execute(
            "SELECT station_id,MAX(holdout_end_date) FROM "
            "calibration_cross_native_ensemble_selection_evidence "
            "GROUP BY station_id"
        ).fetchall()
    except sqlite3.Error:
        return {
            "ready": False,
            "state": "selection_evidence_unavailable",
            "max_stale_days": max_stale_days,
            "stale_stations": [],
        }
    try:
        as_of = datetime.fromisoformat(
            str(as_of_utc).replace("Z", "+00:00")
        ).astimezone(timezone.utc).date()
    except ValueError:
        return {
            "ready": False,
            "state": "decision_clock_invalid",
            "max_stale_days": max_stale_days,
            "stale_stations": [],
        }

    observed: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for station_id, holdout_end in rows:
        station = str(station_id or "").strip().upper()
        if not station or (scope and station not in scope):
            continue
        try:
            end = date.fromisoformat(str(holdout_end or ""))
        except ValueError:
            unreadable.append(station)
            continue
        observed.append({
            "station_id": station,
            "holdout_end_date": end.isoformat(),
            "stale_days": (as_of - end).days,
        })
    stale = sorted(
        (row for row in observed if row["stale_days"] > max_stale_days),
        key=lambda row: (-int(row["stale_days"]), str(row["station_id"])),
    )
    missing = sorted(set(scope) - {row["station_id"] for row in observed})
    return {
        "ready": not stale and not unreadable and not missing,
        "state": (
            "fresh"
            if not stale and not unreadable and not missing
            else "stale"
        ),
        "max_stale_days": max_stale_days,
        "as_of_date": as_of.isoformat(),
        "station_count": len(observed),
        "stale_station_count": len(stale),
        "stale_stations": stale,
        "unreadable_holdout_stations": unreadable,
        "stations_without_selection_evidence": missing,
        "read_only": True,
    }


def _official_ws_scope_coverage(
    conn: sqlite3.Connection,
    manifest_path: Path,
    *,
    station_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Explain target-scope coverage without changing any state."""

    resolved = manifest_path.expanduser().resolve()
    station_scope = _normalize_station_scope(station_ids)
    if not resolved.is_file():
        return {
            "state": "missing",
            "path": str(resolved),
            "complete_scope_count": 0,
            "station_scope": {
                "mode": "allowlist" if station_scope else "full_target_registry",
                "station_ids": list(station_scope),
                "source_lane": "AWC_ONLY" if station_scope else "UNSCOPED_TARGET_REGISTRY",
            },
        }
    try:
        raw_root = load_bounded_json(resolved)
        if not isinstance(raw_root, Mapping):
            raise ValueError("official_ws_manifest_mapping_required")
        if raw_root.get("schema") == MULTI_SCOPE_WS_MANIFEST_SCHEMA:
            supplied_sha = str(raw_root.get("snapshot_sha256") or "").strip()
            expected_sha = content_sha256({
                key: value
                for key, value in raw_root.items()
                if key != "snapshot_sha256"
            })
            summaries = raw_root.get("scopes")
            nested_by_scope = raw_root.get("scope_manifests")
            if (
                raw_root.get("state") != "connected"
                or raw_root.get("source")
                != "official_polymarket_clob_websocket"
                or raw_root.get("evidence_only") is not True
                or raw_root.get("authorizes_order") is not False
                or raw_root.get("calls_order_gateway") is not False
                or raw_root.get("live_submit_authorized") is not False
                or int(raw_root.get("orders_submitted", -1)) != 0
                or not supplied_sha
                or supplied_sha != expected_sha
                or not isinstance(summaries, list)
                or not isinstance(nested_by_scope, Mapping)
                or not 1 <= len(summaries) <= MAX_TARGET_WS_SCOPES
                or len(nested_by_scope) != len(summaries)
            ):
                raise ValueError("official_ws_multi_scope_boundary_invalid")
            scope_quotes: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
            rows = []
            seen_scope_keys: set[str] = set()
            for summary in summaries:
                if not isinstance(summary, Mapping):
                    raise ValueError("official_ws_scope_summary_invalid")
                scope_key = str(summary.get("scope_key") or "").strip()
                station_id = str(summary.get("station_id") or "").strip().upper()
                event_date = str(summary.get("event_date") or "").strip()
                nested = nested_by_scope.get(scope_key)
                if (
                    not scope_key
                    or scope_key in seen_scope_keys
                    or scope_key != f"{station_id}|{event_date}"
                    or not isinstance(nested, Mapping)
                    or str(summary.get("scope_manifest_sha256") or "")
                    != str(nested.get("snapshot_sha256") or "")
                ):
                    raise ValueError("official_ws_scope_binding_invalid")
                seen_scope_keys.add(scope_key)
                if station_scope and station_id not in station_scope:
                    continue
                nested_root, nested_quotes = load_official_ws_quotes_payload(
                    nested
                )
                nested_stations = tuple(sorted({
                    str(value).strip().upper()
                    for value in (
                        nested_root.get("station_scope", {}).get(
                            "station_ids", []
                        )
                        if isinstance(
                            nested_root.get("station_scope"), Mapping
                        )
                        else []
                    )
                    if str(value).strip()
                }))
                if (
                    nested_stations != (station_id,)
                    or str(nested_root.get("target_local_date") or "")
                    != event_date
                ):
                    raise ValueError("official_ws_nested_scope_mismatch")
                scope_quotes[(station_id, event_date)] = nested_quotes
                rows.extend(load_started_registry_rows(
                    conn,
                    station_id=station_id,
                    event_date=event_date,
                ))
            root = dict(raw_root)
        else:
            root, quotes = load_official_ws_quotes(resolved)
            manifest_event_date = str(
                root.get("target_local_date") or ""
            ).strip()
            rows = [
                row
                for station_id in station_scope
                for row in load_started_registry_rows(
                    conn,
                    station_id=station_id,
                    event_date=manifest_event_date,
                )
            ] if station_scope else load_started_registry_rows(
                conn,
                event_date=manifest_event_date,
            )
            scope_quotes = {
                (str(row["station_id"]), str(row["event_date"])): quotes
                for row in rows
            }
    except Exception as exc:
        return {
            "state": "blocked",
            "path": str(resolved),
            "reason": f"{type(exc).__name__}:{exc}",
            "complete_scope_count": 0,
            "station_scope": {
                "mode": "allowlist" if station_scope else "full_target_registry",
                "station_ids": list(station_scope),
                "source_lane": "AWC_ONLY" if station_scope else "UNSCOPED_TARGET_REGISTRY",
            },
        }
    scopes: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        scopes.setdefault((str(row["station_id"]), str(row["event_date"])), []).append(str(row["yes_token"]))
    complete = {
        scope: tokens
        for scope, tokens in scopes.items()
        if all(
            token in scope_quotes.get(scope, {})
            for token in tokens
        )
    }
    missing = [
        {"scope": f"{station}|{day}", "missing_token_count": sum(token not in quotes for token in tokens)}
        for (station, day), tokens in sorted(scopes.items())
        if any(
            token not in scope_quotes.get((station, day), {})
            for token in tokens
        )
    ]
    return {
        "state": "loaded",
        "path": str(resolved),
        "manifest_state": root.get("state"),
        "evidence_only": root.get("evidence_only"),
        "authorizes_order": root.get("authorizes_order"),
        "loaded_quote_count": sum(
            len(quotes) for quotes in scope_quotes.values()
        ),
        "registry_scope_count": len(scopes),
        "registry_yes_token_count": sum(len(tokens) for tokens in scopes.values()),
        "complete_scope_count": len(complete),
        "complete_scope_token_count": sum(len(tokens) for tokens in complete.values()),
        "incomplete_scope_count": len(missing),
        "incomplete_scope_sample": missing[:12],
        "pending_shards": list(root.get("pending_shards") or []),
        "pending_materialized_token_count": int(root.get("pending_materialized_token_count") or 0),
        "unmaterialized_token_count": len(root.get("unmaterialized_tokens") or []),
        "station_scope": {
            "mode": "allowlist" if station_scope else "full_target_registry",
            "station_ids": list(station_scope),
            "source_lane": "AWC_ONLY" if station_scope else "UNSCOPED_TARGET_REGISTRY",
        },
    }
def _service_probe(*, uid: int | None = None) -> dict[str, Any]:
    uid = os.getuid() if uid is None else int(uid)
    domains = {"system": "system", "user": f"gui/{uid}"}
    result: dict[str, Any] = {"system": {}, "user": {}, "probe_errors": []}
    for domain_name, domain in domains.items():
        for label in CORE_SERVICES + FORBIDDEN_SERVICES:
            completed = subprocess.run(
                ["launchctl", "print", f"{domain}/{label}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            result[domain_name][label] = completed.returncode == 0
    return result


def audit(
    *,
    db: Path,
    activation_path: Path,
    manifest_path: Path,
    calibration_evidence_path: Path = DEFAULT_CALIBRATION_EVIDENCE,
    native_decode_evidence_path: Path = DEFAULT_NATIVE_DECODE_EVIDENCE,
    probe_services: bool = True,
    station_ids: Sequence[str] | None = None,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []
    formal_promotion_blockers: list[str] = []
    decision_clock = as_of_utc or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    selected_calibration_path = _resolve_latest_valid_evidence(
        calibration_evidence_path,
        default_path=DEFAULT_CALIBRATION_EVIDENCE,
        # Calibration is one SSOT receipt.  Dated repair packets are inputs
        # to the builder, not alternate readiness authorities; considering
        # them here allowed an old valid-looking packet to override the
        # canonical file.
        glob_pattern="bayes_basket_target_calibration_evidence.json",
        parser=_calibration_evidence,
        valid_states={"VALID_CANDIDATE_EVIDENCE", "VALID_BLOCKED_EVIDENCE"},
    )
    selected_native_decode_path = _resolve_latest_valid_evidence(
        native_decode_evidence_path,
        default_path=DEFAULT_NATIVE_DECODE_EVIDENCE,
        glob_pattern="bayes_basket_target_native_model_arrival_decode*.json",
        parser=_native_decode_evidence,
        valid_states={"VALID_NATIVE_DECODE_EVIDENCE", "VALID_BLOCKED_NATIVE_DECODE_EVIDENCE"},
    )
    calibration_evidence = _calibration_evidence(selected_calibration_path)
    native_decode_evidence = _native_decode_evidence(selected_native_decode_path)
    if calibration_evidence.get("state") == "INVALID":
        blockers.append("calibration_evidence_invalid")
    if native_decode_evidence.get("state") == "INVALID":
        blockers.append("native_decode_evidence_invalid")
    try:
        db_path = resolve_target_database(db)
    except ValueError as exc:
        forward_scorecard = _blocked_forward_scorecard(
            as_of_utc=decision_clock,
            reason="forward_scorecard_database_scope_invalid",
            required_station_ids=_normalize_station_scope(station_ids),
        )
        return {
            "schema": SCHEMA,
            "ok": False,
            "state": "blocked",
            "blockers": [str(exc)],
            "production_no_live_ready": False,
            "formal_promotion_ready": False,
            "formal_promotion_blockers": [],
            "database": str(db.expanduser().resolve()),
            "forward_scorecard": forward_scorecard,
            "boundary": {
                "execution_mode": "production_no_live",
                "live_submit_authorized": False,
                "places_orders": False,
                "calls_order_gateway": False,
                "writes_orders": False,
                "orders_submitted": 0,
                "read_only_audit": True,
            },
        }
    isolated_target = is_isolated_target_database(db_path)
    activation_file, activation_blockers = _activation(activation_path)
    blockers.extend(activation_blockers)
    if not db_path.exists() or db_path.is_symlink() or not db_path.is_file():
        blockers.append("production_database_path_invalid")
        migration = {"state": "not_checked"}
        inputs = {"ready": False, "state": "database_invalid"}
        capital = {"ready": False, "state": "database_invalid"}
        alignment = {"state": "not_checked", "aligned": False}
        ws_coverage = {"state": "database_invalid", "complete_scope_count": 0}
        calibration_staleness = {
            "ready": False,
            "state": "database_invalid",
            "max_stale_days": DEFAULT_CALIBRATION_MAX_STALE_DAYS,
            "stale_stations": [],
        }
        probability_evidence_runtime = {
            "schema": "bayes_basket_probability_evidence_runtime_status_v1",
            "state": "DATABASE_INVALID",
            "missing_tables": list(PROBABILITY_EVIDENCE_TABLES),
            "missing_bridge_tables": list(PROBABILITY_BRIDGE_TABLES),
            "read_only": True,
            "production_no_live": True,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "candidate_activation_count_all": 0,
        }
        future_day_residue = {"state": "database_invalid", "checks": {"future_residue_isolated": False}}
        resource_budget = inspect_target_resource_budget(db_path, raw_root=DEFAULT_TARGET_RAW_ROOT)
    else:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60.0) as conn:
            migration = inspect_migration(conn)
            inputs = _target_inputs(
                conn,
                as_of_utc=decision_clock,
                policy_authority_scope=str(
                    activation_file.get("policy_authority_scope")
                    or "PRODUCTION_NO_LIVE_CANDIDATE"
                ),
                target_scope=(
                    activation_file.get("target_scope")
                    if isinstance(
                        activation_file.get("target_scope"), Mapping
                    )
                    else {}
                ),
                station_ids=station_ids or (),
            )
            capital = _capital_pool(conn)
            alignment = _db_activation_alignment(conn, activation_file)
            ws_coverage = _official_ws_scope_coverage(conn, manifest_path, station_ids=station_ids)
            calibration_staleness = _calibration_staleness(
                conn,
                as_of_utc=decision_clock,
                station_ids=station_ids,
            )
            probability_evidence_runtime = (
                _probability_evidence_runtime_status(
                    conn,
                    station_ids=station_ids or (),
                )
            )
            if int(
                probability_evidence_runtime.get(
                    "candidate_activation_count_all", 0
                )
                or 0
            ) != 0:
                blockers.append("target_candidate_activation_not_zero")
            future_day_residue = audit_future_day_residue(conn, as_of_utc=decision_clock)
            forward_scorecard = _forward_scorecard_status(
                conn,
                as_of_utc=decision_clock,
                station_ids=station_ids,
            )
            try:
                assert_target_database_owner(conn)
            except ValueError:
                blockers.append("target_shared_live_activation_owner_conflict")
        resource_budget = inspect_target_resource_budget(db_path, raw_root=DEFAULT_TARGET_RAW_ROOT)
        if migration.get("legacy_decision_table_collision"):
            blockers.append("legacy_auto_trade_decisions_collision")
        if not migration.get("production_ssot_schema", {}).get("ok"):
            blockers.append("target_production_ssot_schema_not_installed")
        if not migration.get("target_ledger_schema", {}).get("ok"):
            blockers.append("target_m5_ledger_not_installed")
        if not migration.get("blocked_execution_vector_lineage", {}).get("ok"):
            blockers.append("target_execution_vector_lineage_incomplete")
        if not inputs.get("ready"):
            blockers.append("target_materialized_inputs_incomplete")
        for boundary_blocker in (
            "forward_scorecard_not_production_no_live",
            "forward_scorecard_not_read_only",
            "forward_scorecard_gateway_boundary_invalid",
            "forward_scorecard_order_boundary_invalid",
        ):
            if boundary_blocker in set(forward_scorecard.get("blockers") or []):
                blockers.append(boundary_blocker)
        if not capital.get("ready"):
            blockers.append("target_capital_epoch_not_initialized")
        if not alignment.get("aligned"):
            blockers.append("file_db_activation_drift")
        if not resource_budget.get("ok"):
            blockers.append("target_resource_budget_exceeded")
        if not calibration_staleness.get("ready"):
            blockers.append("target_calibration_weights_stale")
        if not future_day_residue.get("checks", {}).get("future_residue_isolated"):
            blockers.append("future_day_residue_not_isolated")
    if not db_path.exists() or db_path.is_symlink() or not db_path.is_file():
        forward_scorecard = _blocked_forward_scorecard(
            as_of_utc=decision_clock,
            reason="forward_scorecard_database_missing",
            required_station_ids=_normalize_station_scope(station_ids),
        )
    if not Path(manifest_path).is_file():
        blockers.append("official_ws_manifest_missing")
    pause_marker_path = (
        ROOT / "data" / "live_paused.flag"
        if not isolated_target
        else ROOT / "data" / "bayes_basket_target_migration_paused.flag"
    )
    pause_marker_exists = pause_marker_path.is_file()
    pause_marker_valid = False
    if pause_marker_exists:
        try:
            pause_marker_valid = PAUSE_MARKER_TOKEN in {
                line.strip() for line in pause_marker_path.read_text(encoding="utf-8").splitlines()
            }
        except OSError:
            pause_marker_valid = False
    if not pause_marker_exists and not isolated_target:
        blockers.append("controlled_migration_pause_marker_missing")
    elif not pause_marker_valid and not isolated_target:
        blockers.append("controlled_migration_pause_marker_invalid")

    services: dict[str, Any]
    if probe_services:
        services = _service_probe()
        for label in CORE_SERVICES:
            if not services["user"].get(label):
                blockers.append(f"core_service_not_loaded_user:{label}")
            if services["system"].get(label):
                blockers.append(f"core_service_loaded_system_domain:{label}")
        for label in FORBIDDEN_SERVICES:
            if services["system"].get(label) or services["user"].get(label):
                blockers.append(f"retired_service_still_loaded:{label}")
    else:
        services = {"state": "not_probed", "system": {}, "user": {}}

    unfinished_inputs = _unfinished_target_inputs(
        inputs,
        ws_coverage=ws_coverage,
        calibration_evidence=calibration_evidence,
        native_decode_evidence=native_decode_evidence,
    )
    main_chain = _main_chain_status(
        inputs,
        calibration_evidence=calibration_evidence,
        native_decode_evidence=native_decode_evidence,
        ws_coverage=ws_coverage,
    )
    result = {
        "schema": SCHEMA,
        "ok": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "production_no_live_ready": not blockers,
        "formal_promotion_ready": (
            not blockers and not formal_promotion_blockers
        ),
        "formal_promotion_blockers": sorted(
            set(formal_promotion_blockers)
        ),
        "database": str(db_path),
        "migration": migration,
        "target_inputs": inputs,
        "main_chain": main_chain,
        "unfinished_target_inputs": unfinished_inputs,
        "capital_pool": capital,
        "resource_budget": resource_budget,
        "activation": {
            "path": str(activation_path.expanduser().resolve()),
            "schema": activation_file.get("schema"),
            "contract_sha256": activation_file.get("activation_contract_sha256"),
            "execution_mode": activation_file.get("execution_mode"),
            "live_submit_authorized": activation_file.get("live_submit_authorized"),
            "places_orders": activation_file.get("places_orders"),
            "calls_order_gateway": activation_file.get("calls_order_gateway"),
            "validation_blockers": activation_blockers,
        },
        "db_activation_alignment": alignment,
        "services": services,
        "official_ws_manifest": {
            "path": str(manifest_path.expanduser().resolve()),
            "exists": Path(manifest_path).is_file(),
        },
        "official_ws_scope_coverage": ws_coverage,
        "calibration_staleness": calibration_staleness,
        "probability_evidence_runtime": probability_evidence_runtime,
        "station_scope": {
            "mode": "allowlist" if _normalize_station_scope(station_ids) else "full_target_registry",
            "station_ids": list(_normalize_station_scope(station_ids)),
            "source_lane": "AWC_ONLY" if _normalize_station_scope(station_ids) else "UNSCOPED_TARGET_REGISTRY",
        },
        "future_day_residue": future_day_residue,
        "calibration_evidence": calibration_evidence,
        "native_decode_evidence": native_decode_evidence,
        "forward_scorecard": forward_scorecard,
        "database_scope": "isolated_target_no_live" if isolated_target else "shared_live_database_explicit_override",
        "controlled_migration_pause_marker": str(pause_marker_path.resolve()),
        "controlled_migration_pause_marker_token": PAUSE_MARKER_TOKEN,
        "controlled_migration_pause_marker_valid": pause_marker_valid if not isolated_target else None,
        "controlled_migration_pause_marker_required": not isolated_target,
        "boundary": {
            "execution_mode": "production_no_live",
            "live_submit_authorized": False,
            "places_orders": False,
            "calls_order_gateway": False,
            "writes_orders": False,
            "orders_submitted": 0,
            "read_only_audit": True,
        },
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    # The target lane has its own immutable no-live contract.  The shared
    # basket_yes_v1.json belongs to the resident production owner
    # and may legitimately drift with that owner's activation; reading it
    # here would couple target readiness to (or tempt mutation of) another
    # production line.
    parser.add_argument(
        "--activation",
        type=Path,
        default=ROOT / "config/trading/bayes_basket_target_production_no_live_activation_v1.json",
    )
    parser.add_argument("--manifest", type=Path, default=TARGET_WS_MANIFEST)
    parser.add_argument("--calibration-evidence", type=Path, default=DEFAULT_CALIBRATION_EVIDENCE)
    parser.add_argument("--native-decode-evidence", type=Path, default=DEFAULT_NATIVE_DECODE_EVIDENCE)
    parser.add_argument(
        "--station-id",
        action="append",
        default=[],
        help="explicit station allowlist for scoped readiness; use only the intended source lane",
    )
    parser.add_argument("--no-service-probe", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "output/audits/bayes_basket_target_production_readiness.json")
    args = parser.parse_args()
    result = audit(
        db=args.db,
        activation_path=args.activation,
        manifest_path=args.manifest,
        calibration_evidence_path=args.calibration_evidence,
        native_decode_evidence_path=args.native_decode_evidence,
        probe_services=not args.no_service_probe,
        station_ids=args.station_id,
    )
    output = args.output.expanduser().resolve()
    write_bounded_json(output, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
