#!/usr/bin/env python3
"""Bounded, read-only daily acceptance for the target no-live lane.

M10-04 is an evidence command, not a migration or a resident repair command.
It opens only the isolated target database in SQLite read-only mode, samples
bounded latest windows, and writes one bounded JSON receipt.  It never runs
DDL, checkpoints WAL, calls a gateway, or changes a service.

Forward OOS is deliberately reported as formal-promotion evidence.  It is
never a blocker for ``production_no_live`` readiness.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote as urlquote


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.database import (  # noqa: E402
    DEFAULT_TARGET_DB,
    is_isolated_target_database,
    resolve_target_database,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    ResourceBoundError,
    assert_process_memory_budget,
    inspect_target_resource_budget,
    write_bounded_json,
)


SCHEMA = "bayes_basket_target_daily_acceptance_no_live_v1"
CORE_SERVICES = (
    "com.weather.weather_basket_yes",
    "com.weather.market_ws",
    "com.weather.clob_order_service",
)
TARGET_DB_ENV = "BAYES_BASKET_TARGET_DB"
MAX_LATEST_ROWS = 512
MAX_JSON_BYTES = 1024 * 1024
MAX_SERVICE_PROBE_SECONDS = 10
MAX_SQL_ROWS_PROJECTED_BYTES = 16 * 1024 * 1024
UTC = timezone.utc


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("timestamp_missing")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_timezone_missing")
    return parsed.astimezone(UTC)


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdefABCDEF" for char in value
    )


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _count(conn: sqlite3.Connection, table: str, predicate: str = "1", params: Sequence[Any] = ()) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {predicate}", tuple(params)
        ).fetchone()[0]
    )


def _bounded_fetch(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] = (),
    *,
    limit: int | None = None,
) -> tuple[list[tuple[Any, ...]], bool]:
    """Fetch at most ``limit`` rows and explicitly detect truncation."""

    effective_limit = MAX_LATEST_ROWS if limit is None else int(limit)
    if not 1 <= effective_limit <= MAX_LATEST_ROWS:
        raise ResourceBoundError("daily_acceptance_row_limit_invalid")
    rows = conn.execute(sql, (*params, effective_limit + 1)).fetchall()
    truncated = len(rows) > effective_limit
    bounded = rows[:effective_limit]
    projected = min(
        len(bounded) * 4096,
        MAX_SQL_ROWS_PROJECTED_BYTES,
    )
    assert_process_memory_budget(projected_bytes=projected)
    return bounded, truncated


def _bounded_json(raw: Any, field: str) -> Mapping[str, Any]:
    if raw is None:
        raise ValueError(f"{field}_missing")
    encoded = str(raw).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise ResourceBoundError(f"{field}_bytes_exceeded")
    value = json.loads(encoded)
    if not isinstance(value, Mapping):
        raise ValueError(f"{field}_mapping_required")
    return value


def _service_pids() -> dict[str, int]:
    """Probe exactly the three user-domain services and nothing else."""

    result: dict[str, int] = {}
    domain = f"gui/{os.getuid()}"
    for label in CORE_SERVICES:
        try:
            completed = subprocess.run(
                ["/bin/launchctl", "print", f"{domain}/{label}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=MAX_SERVICE_PROBE_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"daily_acceptance_service_probe_failed:{label}:{type(exc).__name__}"
            ) from exc
        if completed.returncode != 0:
            raise RuntimeError(f"daily_acceptance_service_missing:{label}")
        pids = []
        for line in completed.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("pid = "):
                candidate = stripped.removeprefix("pid = ").strip()
                if candidate.isdigit():
                    pids.append(int(candidate))
        if len(pids) != 1 or pids[0] <= 0:
            raise RuntimeError(f"daily_acceptance_service_pid_invalid:{label}")
        result[label] = pids[0]
    return result


def _audit_services(probe: Callable[[], Any]) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    try:
        raw = probe()
    except Exception as exc:  # fail closed while retaining the cause in JSON
        return (
            {
                "domain": "user",
                "expected_services": list(CORE_SERVICES),
                "observed": {},
                "state": "PROBE_FAILED",
                "error": f"{type(exc).__name__}:{exc}",
            },
            ["user_service_probe_failed"],
        )
    if isinstance(raw, Mapping) and "services" in raw:
        raw = raw.get("services")
    observed: dict[str, int] = {}
    if isinstance(raw, Mapping):
        for label, pid in raw.items():
            try:
                observed[str(label)] = int(pid)
            except (TypeError, ValueError):
                blockers.append(f"service_pid_invalid:{label}")
    else:
        blockers.append("service_probe_mapping_required")
    expected = set(CORE_SERVICES)
    actual = set(observed)
    if actual != expected:
        blockers.append("user_service_set_drift")
        for label in sorted(expected - actual):
            blockers.append(f"user_service_missing:{label}")
        for label in sorted(actual - expected):
            blockers.append(f"user_service_unexpected:{label}")
    for label in CORE_SERVICES:
        if label in observed and observed[label] <= 0:
            blockers.append(f"user_service_pid_invalid:{label}")
    return (
        {
            "domain": "user",
            "expected_services": list(CORE_SERVICES),
            "observed": {key: observed[key] for key in sorted(observed)},
            "count": len(observed),
            "exactly_three": actual == expected and len(observed) == 3,
            "state": "PASS" if not blockers else "BLOCKED",
        },
        blockers,
    )


def _activation_status(conn: sqlite3.Connection, tables: set[str]) -> dict[str, Any]:
    if "basket_strategy_activation_state" not in tables:
        return {"state": "MISSING", "ready": False, "blockers": ["activation_state_missing"]}
    rows, truncated = _bounded_fetch(
        conn,
        "SELECT generation,state,activation_json,updated_at "
        "FROM basket_strategy_activation_state "
        "ORDER BY generation DESC,rowid DESC LIMIT ?",
        limit=2,
    )
    blockers: list[str] = []
    if truncated:
        blockers.append("activation_history_row_cap_exceeded")
    if not rows:
        blockers.append("activation_state_empty")
        return {"state": "MISSING", "ready": False, "blockers": blockers}
    generation, state, raw_json, updated_at = rows[0]
    try:
        details = _bounded_json(raw_json, "activation_json")
    except (ValueError, ResourceBoundError, json.JSONDecodeError) as exc:
        blockers.append(f"activation_json_invalid:{type(exc).__name__}")
        details = {}
    if str(state).lower() not in {"enabled", "paused"}:
        blockers.append("activation_state_not_enabled_or_paused")
    for key in (
        "live_decision_write_authorized",
        "live_submit_authorized",
        "places_orders",
        "calls_order_gateway",
    ):
        if details.get(key) is not False:
            blockers.append(f"activation_{key}_must_be_false")
    return {
        "state": "PASS" if not blockers else "BLOCKED",
        "ready": not blockers,
        "generation": generation,
        "activation_state": state,
        "updated_at": updated_at,
        "details": {
            key: details.get(key)
            for key in (
                "execution_mode",
                "live_decision_write_authorized",
                "live_submit_authorized",
                "places_orders",
                "calls_order_gateway",
            )
        },
        "blockers": blockers,
    }


def _ws_status(
    conn: sqlite3.Connection,
    tables: set[str],
    station_id: str,
    event_date: str,
    as_of_utc: str,
) -> dict[str, Any]:
    blockers: list[str] = []
    if "basket_official_ws_snapshot" not in tables:
        return {"state": "MISSING", "ready": False, "blockers": ["official_ws_table_missing"]}
    rows, truncated = _bounded_fetch(
        conn,
        "SELECT snapshot_id,event_date,settlement_timezone,sequence,observed_at,"
        "quote_observed_at,quote_expires_at,quote_source,state,content_sha256 "
        "FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? "
        "ORDER BY observed_at DESC,sequence DESC,rowid DESC LIMIT ?",
        (station_id, event_date),
    )
    if truncated:
        blockers.append("official_ws_row_cap_exceeded")
    if not rows:
        blockers.append("official_ws_evidence_missing")
        return {"state": "MISSING", "ready": False, "blockers": blockers}
    row = rows[0]
    (
        snapshot_id,
        event_date,
        settlement_timezone,
        sequence,
        observed_at,
        quote_observed_at,
        quote_expires_at,
        quote_source,
        state,
        digest,
    ) = row
    try:
        as_of = _parse_utc(as_of_utc)
        observed = _parse_utc(observed_at)
        quote_observed = _parse_utc(quote_observed_at)
        expires = _parse_utc(quote_expires_at)
        if observed > as_of or quote_observed > as_of:
            blockers.append("official_ws_timestamp_in_future")
        if expires <= as_of:
            blockers.append("official_ws_evidence_stale")
    except ValueError as exc:
        blockers.append(f"official_ws_timestamp_invalid:{exc}")
    if str(quote_source) != "OFFICIAL_CLOB_WS":
        blockers.append("official_ws_source_not_official_clob_ws")
    if str(state) != "COMMITTED":
        blockers.append("official_ws_state_not_committed")
    if not str(settlement_timezone or "").strip():
        blockers.append("official_ws_settlement_timezone_missing")
    if not _sha256(str(digest)):
        blockers.append("official_ws_content_hash_invalid")
    return {
        "state": "PASS" if not blockers else "BLOCKED",
        "ready": not blockers,
        "latest": {
            "snapshot_id": snapshot_id,
            "event_date": event_date,
            "settlement_timezone": settlement_timezone,
            "sequence": sequence,
            "observed_at": observed_at,
            "quote_observed_at": quote_observed_at,
            "quote_expires_at": quote_expires_at,
            "quote_source": quote_source,
            "state": state,
        },
        "blockers": blockers,
    }


def _probability_status(
    conn: sqlite3.Connection,
    tables: set[str],
    station_id: str,
    as_of_utc: str,
) -> dict[str, Any]:
    blockers: list[str] = []
    if "basket_probability_envelope" not in tables:
        return {"state": "MISSING", "ready": False, "stage_counts": {}, "blockers": ["probability_envelope_missing"]}
    rows, truncated = _bounded_fetch(
        conn,
        "SELECT envelope_id,stage,event_date,settlement_timezone,decision_at_utc,"
        "fresh_until_utc,posterior_version,posterior_sha256,bridge_sha256,lineage_json,state "
        "FROM basket_probability_envelope WHERE station_id=? "
        "ORDER BY decision_at_utc DESC,rowid DESC LIMIT ?",
        (station_id,),
    )
    if truncated:
        blockers.append("probability_row_cap_exceeded")
    latest_event_date = str(rows[0][2]) if rows else ""
    rows = [row for row in rows if str(row[2]) == latest_event_date]
    stage_counts: dict[str, int] = {}
    valid_rows = 0
    latest: dict[str, Any] = {}
    authority_stage = next(
        (
            stage
            for stage in ("P1", "P0-M", "P0")
            if any(str(row[1]) == stage for row in rows)
        ),
        None,
    )
    authority_checked = False
    for row in rows:
        (
            envelope_id,
            stage,
            event_date,
            settlement_timezone,
            decision_at,
            fresh_until,
            posterior_version,
            posterior_digest,
            bridge_digest,
            raw_lineage,
            state,
        ) = row
        stage_name = str(stage)
        stage_counts[stage_name] = stage_counts.get(stage_name, 0) + 1
        row_blockers: list[str] = []
        if state != "COMMITTED":
            row_blockers.append("state_not_committed")
        if stage_name not in {"P0", "P0-M", "P1"}:
            row_blockers.append("stage_invalid")
        if not str(posterior_version or "").strip():
            row_blockers.append("posterior_version_missing")
        if not _sha256(str(posterior_digest)):
            row_blockers.append("posterior_sha256_invalid")
        if not _sha256(str(bridge_digest)):
            row_blockers.append("bridge_sha256_invalid")
        try:
            lineage = _bounded_json(raw_lineage, "probability_lineage")
            if not lineage:
                row_blockers.append("probability_lineage_empty")
        except (ValueError, ResourceBoundError, json.JSONDecodeError) as exc:
            row_blockers.append(f"probability_lineage_invalid:{type(exc).__name__}")
            lineage = {}
        try:
            if _parse_utc(decision_at) > _parse_utc(as_of_utc):
                row_blockers.append("probability_decision_in_future")
            if (
                stage_name == authority_stage
                and not authority_checked
                and _parse_utc(fresh_until) <= _parse_utc(as_of_utc)
            ):
                row_blockers.append("probability_evidence_stale")
        except ValueError as exc:
            row_blockers.append(f"probability_timestamp_invalid:{exc}")
        if not str(settlement_timezone or "").strip():
            row_blockers.append("probability_settlement_timezone_missing")
        if not row_blockers:
            valid_rows += 1
        if stage_name not in latest:
            if stage_name == authority_stage:
                authority_checked = True
            latest[stage_name] = {
                "envelope_id": envelope_id,
                "event_date": event_date,
                "settlement_timezone": settlement_timezone,
                "decision_at_utc": decision_at,
                "fresh_until_utc": fresh_until,
                "posterior_version": posterior_version,
                "lineage_keys": sorted(str(key) for key in lineage),
                "state": state,
                "row_blockers": row_blockers,
            }
        blockers.extend(
            f"probability_{stage_name}:{code}" for code in row_blockers
        )
    if not rows:
        blockers.append("probability_materialization_missing")
    if valid_rows == 0 and rows:
        blockers.append("probability_lineage_materialization_invalid")
    # Historical P0/P0-M rows remain lineage, not parallel current
    # authorities. Only the latest available stage must remain fresh.
    if "P0" not in stage_counts:
        blockers.append("probability_p0_missing")
    if not ({"P0-M", "P1"} & set(stage_counts)):
        blockers.append("probability_current_revision_missing")
    return {
        "state": "PASS" if not blockers else "BLOCKED",
        "ready": not blockers,
        "stage_counts": stage_counts,
        "latest_by_stage": latest,
        "latest_event_date": latest_event_date,
        "current_authority_stage": authority_stage,
        "valid_rows": valid_rows,
        "blockers": sorted(set(blockers)),
    }


def _ledger_status(
    conn: sqlite3.Connection,
    tables: set[str],
    station_id: str,
    event_date: str,
) -> tuple[dict[str, Any], dict[str, int], list[str]]:
    required = {
        "basket_target_execution_cycles",
        "auto_trade_decisions",
        "basket_target_execution_version_vectors",
    }
    missing = sorted(required - tables)
    if missing:
        return (
            {"state": "MISSING", "ready": False, "missing_tables": missing},
            {},
            [f"ledger_table_missing:{table}" for table in missing],
        )
    blockers: list[str] = []
    rows, truncated = _bounded_fetch(
        conn,
        "SELECT c.cycle_id,c.plan_id,c.event_date,c.state,d.decision_id,"
        "d.decided_state,d.will_order,d.decision_sha256,"
        "EXISTS(SELECT 1 FROM basket_target_execution_version_vectors v "
        "WHERE v.cycle_id=c.cycle_id) AS has_vector "
        "FROM basket_target_execution_cycles c "
        "LEFT JOIN auto_trade_decisions d ON d.cycle_id=c.cycle_id "
        "WHERE c.station_id=? AND c.event_date=? "
        "AND c.strategy_lane='basket' "
        "ORDER BY c.updated_at DESC,c.rowid DESC LIMIT ?",
        (station_id, event_date),
    )
    if truncated:
        blockers.append("ledger_row_cap_exceeded")
    if not rows:
        blockers.append("ledger_decision_missing")
    decision_count = _count(
        conn,
        "auto_trade_decisions",
        "station_id=? AND event_date=? AND strategy_lane='basket'",
        (station_id, event_date),
    )
    no_order = _count(
        conn,
        "auto_trade_decisions",
        "station_id=? AND event_date=? "
        "AND strategy_lane='basket' AND will_order<>0",
        (station_id, event_date),
    )
    malformed = 0
    vector_policies = {
        str(policy): int(count)
        for policy, count in conn.execute(
            "SELECT v.vector_policy,COUNT(*) "
            "FROM basket_target_execution_version_vectors v "
            "JOIN basket_target_execution_cycles c ON c.cycle_id=v.cycle_id "
            "WHERE c.station_id=? AND c.event_date=? "
            "AND c.strategy_lane='basket' "
            "GROUP BY v.vector_policy",
            (station_id, event_date),
        )
    }
    invalid_vector_policies = sorted(
        set(vector_policies) - {"NO_EXECUTION", "EXECUTION"}
    )
    if invalid_vector_policies:
        blockers.append(
            "ledger_execution_vector_policy_invalid:"
            + ",".join(invalid_vector_policies)
        )
    for row in rows:
        (
            cycle_id,
            plan_id,
            event_date,
            cycle_state,
            decision_id,
            decision_state,
            will_order,
            digest,
            has_vector,
        ) = row
        if not decision_id:
            blockers.append(f"ledger_decision_missing_for_cycle:{cycle_id}")
        if not plan_id or cycle_state not in {"prepared", "blocked", "reconcile_required", "settled"}:
            malformed += 1
        if decision_state not in {"prepared", "blocked", "reconcile_required", "settled"}:
            malformed += 1
        if int(will_order or 0) != 0:
            blockers.append(f"ledger_will_order_nonzero:{cycle_id}")
        if not _sha256(str(digest)):
            malformed += 1
        if int(has_vector or 0) != 1:
            blockers.append(f"ledger_execution_vector_missing:{cycle_id}")
    if no_order:
        blockers.append("ledger_will_order_count_nonzero")
    if malformed:
        blockers.append("ledger_row_invalid")
    if decision_count != len(rows) and not truncated:
        blockers.append("ledger_latest_window_not_complete")
    counts = {
        "cycles": decision_count,
        "decisions": decision_count,
        "will_order_nonzero": no_order,
    }
    return (
        {
            "state": "PASS" if not blockers else "BLOCKED",
            "ready": not blockers,
            "latest_cycles_inspected": len(rows),
            "vector_policies": vector_policies,
            "blockers": sorted(set(blockers)),
        },
        counts,
        blockers,
    )


def _execution_boundary_status(
    conn: sqlite3.Connection,
    tables: set[str],
    station_id: str,
) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    counts: dict[str, int] = {}
    required_tables = {
        "auto_trade_decisions",
        "basket_target_order_intents",
        "basket_target_order_events",
        "basket_target_execution_receipts",
        "basket_target_order_reconciliations",
    }
    for table in sorted(required_tables - tables):
        blockers.append(f"execution_table_missing:{table}")
    if "auto_trade_decisions" in tables:
        counts["will_order_nonzero"] = _count(
            conn, "auto_trade_decisions", "station_id=? AND will_order<>0", (station_id,)
        )
        counts["submitted_decisions"] = _count(
            conn,
            "auto_trade_decisions",
            "station_id=? AND decided_state='submitted'",
            (station_id,),
        )
    else:
        blockers.append("execution_decision_authority_missing")
    for table, key, predicate in (
        ("basket_target_order_intents", "live_submit_authorized_nonzero", "station_id=? AND live_submit_authorized<>0"),
        ("basket_target_order_events", "order_events", "1"),
        ("basket_target_execution_receipts", "execution_receipts", "1"),
        ("basket_target_order_reconciliations", "unknown_reconciliations", "state='unknown'"),
    ):
        if table not in tables:
            counts[key] = -1
            continue
        columns = _columns(conn, table)
        if "station_id" in columns and "station_id=?" in predicate:
            counts[key] = _count(conn, table, predicate, (station_id,))
        elif "station_id=?" in predicate:
            counts[key] = _count(
                conn,
                table,
                predicate.replace("station_id=? AND ", ""),
            )
        else:
            counts[key] = _count(conn, table, predicate)
    if counts.get("will_order_nonzero", 0):
        blockers.append("execution_will_order_nonzero")
    if counts.get("live_submit_authorized_nonzero", 0):
        blockers.append("execution_live_submit_authorized_nonzero")
    for key in ("order_events", "execution_receipts", "submitted_decisions", "unknown_reconciliations"):
        if counts.get(key, 0):
            blockers.append(f"execution_{key}_nonzero")
    return (
        {
            "state": "PASS" if not blockers else "BLOCKED",
            "ready": not blockers,
            "production_no_live": True,
            "live_submit_authorized": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "counts": counts,
            "blockers": sorted(set(blockers)),
        },
        blockers,
    )


def _training_status(
    conn: sqlite3.Connection,
    tables: set[str],
    station_id: str,
    event_date: str,
) -> dict[str, Any]:
    blockers: list[str] = []
    required = {"basket_policy_training_fact", "basket_policy_training_label"}
    missing = sorted(required - tables)
    if missing:
        return {
            "state": "MISSING",
            "ready": False,
            "missing_tables": missing,
            "labels_state": "UNKNOWN",
            "blockers": [f"training_table_missing:{table}" for table in missing],
        }
    facts, truncated = _bounded_fetch(
        conn,
        "SELECT fact_id,event_date,stage,decision_id,content_sha256,authority_scope "
        "FROM basket_policy_training_fact "
        "WHERE station_id=? AND event_date=? "
        "ORDER BY decision_at_utc DESC,rowid DESC LIMIT ?",
        (station_id, event_date),
    )
    if truncated:
        blockers.append("training_fact_row_cap_exceeded")
    if not facts:
        blockers.append("training_fact_missing")
    fact_ids = [str(row[0]) for row in facts]
    invalid_facts = 0
    for fact_id, event_date, stage, decision_id, digest, authority_scope in facts:
        if stage not in {"P0", "P0-M", "P1"} or not decision_id or not _sha256(str(digest)):
            invalid_facts += 1
        if authority_scope != "PRODUCTION_NO_LIVE_FORWARD":
            invalid_facts += 1
    if invalid_facts:
        blockers.append("training_fact_invalid")
    labels = []
    if fact_ids:
        placeholders = ",".join("?" for _ in fact_ids)
        labels = conn.execute(
            "SELECT label_id,fact_id,truth_id,truth_sha256,content_sha256 "
            f"FROM basket_policy_training_label WHERE fact_id IN ({placeholders}) "
            "ORDER BY labeled_at_utc DESC LIMIT ?",
            (*fact_ids, MAX_LATEST_ROWS + 1),
        ).fetchall()
    labels_truncated = len(labels) > MAX_LATEST_ROWS
    if labels_truncated:
        blockers.append("training_label_row_cap_exceeded")
        labels = labels[:MAX_LATEST_ROWS]
    invalid_labels = 0
    for label_id, fact_id, truth_id, truth_digest, label_digest in labels:
        if fact_id not in fact_ids or not truth_id or not _sha256(str(truth_digest)) or not _sha256(str(label_digest)):
            invalid_labels += 1
    if invalid_labels:
        blockers.append("training_label_invalid")
    return {
        "state": "PASS" if not blockers else "BLOCKED",
        "ready": not blockers,
        "fact_count": len(facts),
        "label_count": len(labels),
        "labels_state": "MATERIALIZED" if labels else "PENDING_SETTLEMENT",
        "invalid_fact_count": invalid_facts,
        "invalid_label_count": invalid_labels,
        "blockers": sorted(set(blockers)),
    }


def _historical_oos_status(conn: sqlite3.Connection, tables: set[str]) -> dict[str, Any]:
    """Report strict historical OOS evidence without gating no-live.

    Future/forward rows are intentionally reported as observational candidates
    only.  This status is a formal-promotion diagnostic; it never turns an
    open event into an OOS sample.
    """

    observed = {
        table: _count(conn, table)
        for table in sorted(tables)
        if table.startswith("forward_") or "oos" in table.lower()
    }
    return {
        "state": "HISTORICAL_ONLY_REPORTED",
        "evidence_policy": "CLOSED_HISTORICAL_STRICT_ASOF",
        "promotion_scope": "FORMAL_PROMOTION_ONLY",
        "future_data_evidence_role": "observational_candidate_not_oos",
        "future_data_counts_as_oos": False,
        "blocks_production_no_live": False,
        "blocks_parameter_or_weight_promotion": False,
        "historical_strict_oos_required_for_parameter_or_weight_promotion": True,
        "observed_table_counts": observed,
    }


def audit(
    db: Path | str | None = None,
    *,
    station_id: str = "RKPK",
    as_of_utc: str | None = None,
    raw_root: Path | str | None = None,
    service_probe: Callable[[], Any] | None = None,
    resource_probe: Callable[..., Mapping[str, Any]] = inspect_target_resource_budget,
) -> dict[str, Any]:
    """Run the M10-04 audit without any database or service mutation."""

    if service_probe is None:
        service_probe = _service_pids
    station = str(station_id).strip().upper()
    if not station:
        raise ValueError("station_id_required")
    clock = as_of_utc or _now()
    _parse_utc(clock)  # fail before opening the database when caller is invalid
    blockers: list[str] = []
    services, service_blockers = _audit_services(service_probe)
    blockers.extend(service_blockers)
    try:
        database = resolve_target_database(db or os.environ.get(TARGET_DB_ENV) or DEFAULT_TARGET_DB)
        isolated = is_isolated_target_database(database)
    except (OSError, ValueError) as exc:
        database = Path(db or "").expanduser().resolve()
        isolated = False
        blockers.append(f"target_database_invalid:{type(exc).__name__}:{exc}")
    if not isolated:
        blockers.append("target_database_not_isolated")
    if not database.is_file():
        blockers.append("target_database_missing")
    try:
        resources = dict(resource_probe(database, raw_root=raw_root))
    except Exception as exc:
        resources = {
            "state": "PROBE_FAILED",
            "ok": False,
            "violations": [f"{type(exc).__name__}:{exc}"],
        }
        blockers.append("target_resource_probe_failed")
    if resources.get("ok") is not True:
        blockers.extend(
            f"resource:{item}" for item in (resources.get("violations") or ["resource_budget_invalid"])
        )
    checks: dict[str, Any] = {}
    if database.is_file() and isolated:
        try:
            encoded = urlquote(str(database), safe="/")
            with sqlite3.connect(
                f"file:{encoded}?mode=ro",
                uri=True,
                timeout=5,
            ) as conn:
                conn.execute("PRAGMA query_only=ON")
                quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
                checks["quick_check"] = quick_check
                if quick_check != "ok":
                    blockers.append("target_database_quick_check_failed")
                tables = _table_names(conn)
                activation = _activation_status(conn, tables)
                probability = _probability_status(conn, tables, station, clock)
                daily_event_date = str(
                    probability.get("latest_event_date") or ""
                )
                if not daily_event_date:
                    blockers.append("daily_event_date_unresolved")
                ws = _ws_status(
                    conn,
                    tables,
                    station,
                    daily_event_date,
                    clock,
                )
                ledger, ledger_counts, ledger_blockers = _ledger_status(
                    conn,
                    tables,
                    station,
                    daily_event_date,
                )
                execution, execution_blockers = _execution_boundary_status(conn, tables, station)
                training = _training_status(
                    conn,
                    tables,
                    station,
                    daily_event_date,
                )
                historical_oos = _historical_oos_status(conn, tables)
                # Keep a read-only compatibility projection for older audit
                # readers, but make its gate values explicitly non-blocking;
                # the canonical object is ``historical_oos`` above.
                forward_oos_alias = {
                    **historical_oos,
                    "state": "REPORTED_ONLY",
                    "blocks_parameter_or_weight_promotion": False,
                }
                checks.update({
                    "tables": {"count": len(tables)},
                    "daily_event_date": daily_event_date,
                    "activation": activation,
                    "official_ws": ws,
                    "probability": probability,
                    "atomic_ledger": ledger,
                    "execution_boundary": execution,
                    "training": training,
                    "historical_oos": historical_oos,
                    "forward_oos": forward_oos_alias,
                    "ledger_counts": ledger_counts,
                })
                blockers.extend(activation.get("blockers") or [])
                blockers.extend(ws.get("blockers") or [])
                blockers.extend(probability.get("blockers") or [])
                blockers.extend(ledger_blockers)
                blockers.extend(execution_blockers)
                blockers.extend(training.get("blockers") or [])
        except (OSError, sqlite3.Error, ResourceBoundError) as exc:
            checks["sqlite"] = {
                "state": "READ_FAILED",
                "error": f"{type(exc).__name__}:{exc}",
            }
            blockers.append("target_database_read_failed")
    state = "PASS" if not blockers else "BLOCKED_FAIL_CLOSED"
    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "as_of_utc": clock,
        "database": str(database),
        "services": services,
        "resources": resources,
        "checks": checks,
        "production_no_live": True,
        "read_only": True,
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "blockers": sorted(set(str(item) for item in blockers)),
        "state": state,
        "ready_for_production_no_live": not blockers,
    }
    result = {
        **identity,
        "content_sha256": content_sha256(identity),
        "captured_at_utc": _now(),
    }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--station-id", default="RKPK")
    parser.add_argument("--as-of-utc", default=None)
    parser.add_argument("--raw-root", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = audit(
        args.db,
        station_id=args.station_id,
        as_of_utc=args.as_of_utc,
        raw_root=args.raw_root,
    )
    output = args.output.expanduser().resolve()
    write_bounded_json(output, result)
    print(json.dumps({
        "state": result["state"],
        "ready_for_production_no_live": result["ready_for_production_no_live"],
        "content_sha256": result["content_sha256"],
        "output": str(output),
    }, sort_keys=True))
    return 0 if result["state"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
