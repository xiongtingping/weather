"""Strict, read-only P1 basket-chain smoke validation.

This is deliberately a compact acceptance projection over the existing
durable SSOT replay.  It does not construct a plan, emit an intent, mutate
SQLite, or talk to a gateway.  The only admissible proof is an already
materialized P1 training-fact chain whose every cycle is explicitly
``NO_EXECUTION``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from .contracts import ContractError, content_sha256
from .target_ssot_replay import MAX_FACTS, replay_durable_target_chain


SCHEMA = "bayes_basket_target_p1_atomic_chain_smoke_v1"
MAX_CANDIDATE_DAYS = 64


class P1AtomicChainSmokeError(ContractError):
    """Raised when no complete P1-only, NO_EXECUTION proof is available."""


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise P1AtomicChainSmokeError(f"p1_atomic_chain_{name}_missing")
    return result


def _candidate_rows(
    conn: sqlite3.Connection,
    *,
    station_id: str | None,
    event_date: str | None,
) -> list[sqlite3.Row]:
    clauses = ["authority_scope='PRODUCTION_NO_LIVE_FORWARD'"]
    values: list[Any] = []
    if station_id is not None:
        clauses.append("station_id=?")
        values.append(station_id)
    if event_date is not None:
        clauses.append("event_date=?")
        values.append(event_date)
    rows = conn.execute(
        "SELECT station_id,event_date,settlement_timezone,COUNT(*) AS fact_count,"
        "SUM(CASE WHEN stage='P1' THEN 1 ELSE 0 END) AS p1_count,"
        "SUM(CASE WHEN intent_count=0 THEN 1 ELSE 0 END) AS no_execution_count "
        "FROM basket_policy_training_fact WHERE " + " AND ".join(clauses) + " "
        "GROUP BY station_id,event_date,settlement_timezone "
        "HAVING COUNT(*) BETWEEN 1 AND ? "
        "ORDER BY fact_count DESC,event_date DESC,station_id ASC LIMIT ?",
        tuple(values + [MAX_FACTS, MAX_CANDIDATE_DAYS + 1]),
    ).fetchall()
    if len(rows) > MAX_CANDIDATE_DAYS:
        raise P1AtomicChainSmokeError(
            "p1_atomic_chain_candidate_scan_limit_exceeded"
        )
    return rows


def _qualified_candidates(
    rows: list[sqlite3.Row],
) -> list[tuple[str, str, str, int]]:
    candidates: list[tuple[str, str, str, int]] = []
    for raw in rows:
        fact_count = int(raw[3])
        if int(raw[4]) != fact_count:
            continue
        if int(raw[5]) != fact_count:
            continue
        candidates.append((
            _text("station_id", raw[0]).upper(),
            _text("event_date", raw[1]),
            _text("settlement_timezone", raw[2]),
            fact_count,
        ))
    return candidates


def _compact_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "fact_id",
            "fact_sha256",
            "decision_at_utc",
            "source_sequence",
            "probability_sha256",
            "official_ws_sha256",
            "inventory_sha256",
            "order_state_sha256",
            "cycle_id",
            "decision_id",
            "decision_sha256",
            "row_sha256",
        )
    }


def _validate_replay_rows(
    replay: Mapping[str, Any],
    *,
    expected_count: int,
) -> list[Mapping[str, Any]]:
    rows = replay.get("rows")
    if (
        replay.get("state") != "PASS"
        or not isinstance(rows, list)
        or len(rows) != expected_count
    ):
        raise P1AtomicChainSmokeError("p1_atomic_chain_replay_incomplete")
    for row in rows:
        if not isinstance(row, Mapping):
            raise P1AtomicChainSmokeError("p1_atomic_chain_replay_row_invalid")
        if row.get("stage") != "P1":
            raise P1AtomicChainSmokeError("p1_atomic_chain_stage_not_p1")
        if row.get("execution_policy") != "NO_EXECUTION":
            raise P1AtomicChainSmokeError(
                "p1_atomic_chain_execution_vector_not_no_execution"
            )
        if (
            row.get("training_fact_present") is not True
            or int(row.get("intent_count") or 0) != 0
            or float(row.get("reserve_amount_usdc") or 0.0) != 0.0
            or row.get("decision_state") != "blocked"
        ):
            raise P1AtomicChainSmokeError(
                "p1_atomic_chain_no_execution_semantics_invalid"
            )
    return rows


def audit_p1_atomic_chain(
    conn: sqlite3.Connection,
    *,
    station_id: str | None = None,
    event_date: str | None = None,
) -> dict[str, Any]:
    """Validate one fully materialized P1-only no-execution chain.

    A caller may pin a station-day.  Without a pin, a bounded deterministic
    search selects the largest complete proof.  The underlying replay checks
    exact content addresses, official CLOB WS, inventory, order-state,
    decision, version-vector, reservation, and time ordering.
    """

    if (station_id is None) != (event_date is None):
        raise P1AtomicChainSmokeError(
            "p1_atomic_chain_station_and_event_date_must_be_paired"
        )
    if conn.row_factory is not sqlite3.Row:
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    station = _text("station_id", station_id).upper() if station_id else None
    day = _text("event_date", event_date) if event_date else None
    candidates = _qualified_candidates(
        _candidate_rows(conn, station_id=station, event_date=day)
    )
    if not candidates:
        raise P1AtomicChainSmokeError(
            "p1_atomic_chain_complete_p1_no_execution_candidate_missing"
        )
    replay: Mapping[str, Any] | None = None
    rows: list[Mapping[str, Any]] | None = None
    selected_station = selected_day = selected_tz = ""
    expected_count = 0
    rejected: list[dict[str, str]] = []
    for candidate_station, candidate_day, candidate_tz, candidate_count in candidates:
        try:
            candidate_replay = replay_durable_target_chain(
                conn,
                station_id=candidate_station,
                event_date=candidate_day,
                max_facts=candidate_count,
            )
            candidate_rows = _validate_replay_rows(
                candidate_replay, expected_count=candidate_count
            )
        except ContractError as exc:
            rejected.append({
                "station_id": candidate_station,
                "event_date": candidate_day,
                "reason": str(exc),
            })
            if station is not None:
                raise P1AtomicChainSmokeError(str(exc)) from exc
            continue
        selected_station = candidate_station
        selected_day = candidate_day
        selected_tz = candidate_tz
        expected_count = candidate_count
        replay = candidate_replay
        rows = candidate_rows
        break
    if replay is None or rows is None:
        reasons = ",".join(item["reason"] for item in rejected[:4])
        raise P1AtomicChainSmokeError(
            "p1_atomic_chain_no_replayable_candidate:" + reasons
        )
    core = {
        "schema": SCHEMA,
        "state": "PASS",
        "station_id": selected_station,
        "event_date": selected_day,
        "settlement_timezone": selected_tz,
        "replayed_fact_count": expected_count,
        "p1_only": True,
        "no_execution_only": True,
        "candidate_scan": {
            "qualified_candidate_count": len(candidates),
            "rejected_before_pass_count": len(rejected),
            "rejected_before_pass": rejected[:8],
        },
        "first_chain_row": _compact_row(rows[0]),
        "last_chain_row": _compact_row(rows[-1]),
        "chain_sha256": replay["continuation"]["chain_sha256"],
        "registry_set_sha256": replay["registry_set_sha256"],
        "validation": {
            "probability_stage": "P1",
            "official_ws": "OFFICIAL_CLOB_WS",
            "inventory_and_order_state": "exact_content_addressed",
            "decision_and_version_vector": "exact_content_addressed",
            "training_fact": "exact_content_addressed",
            "time_order": "strict_monotonic",
        },
        "boundary": {
            "read_only": True,
            "query_only": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
            "network_access": False,
        },
    }
    return {**core, "content_sha256": content_sha256(core)}


__all__ = [
    "MAX_CANDIDATE_DAYS",
    "P1AtomicChainSmokeError",
    "SCHEMA",
    "audit_p1_atomic_chain",
]
