"""Read-only SLO aggregation for canonical-observation fast-lane attempts.

Shadow attempts end at T4 (durable decision intent) and must never be counted
as T5 (CLOB POST).  Live T1→T5 is therefore measured only from records with
an actual immutable POST-start timestamp and the same correlation id carried
by the fast-lane contract.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from observation_fast_lane_contract import (
    SCHEMA_VERSION as FAST_LANE_SCHEMA_VERSION,
    SEGMENT_BUDGET_MS,
    TOTAL_SLO_MS,
)


SCHEMA_VERSION = "observation_fast_lane_latency_slo_report_v1"
# Derive aggregate gates from the single segment contract.  This prevents a
# future segment change from silently contradicting a parent P10 SLO.
T1_TO_T3_BUDGET_MS = (
    int(SEGMENT_BUDGET_MS["canonicalization"])
    + int(SEGMENT_BUDGET_MS["strong_inference_and_transaction_b"])
)
T1_TO_T5_BUDGET_MS = int(TOTAL_SLO_MS)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _elapsed_ms(start: Any, end: Any) -> float | None:
    started = _parse_time(start)
    completed = _parse_time(end)
    if started is None or completed is None or completed < started:
        return None
    return (completed - started).total_seconds() * 1000.0


def _nearest_rank(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = max(1, math.ceil(float(percentile) * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _metric(values: list[float], *, budget_ms: int | None) -> dict[str, Any]:
    count = len(values)
    return {
        "sample_count": count,
        "min_ms": min(values) if values else None,
        "max_ms": max(values) if values else None,
        "p50_ms": _nearest_rank(values, 0.50),
        "p95_ms": _nearest_rank(values, 0.95),
        "p99_ms": _nearest_rank(values, 0.99),
        "budget_ms": budget_ms,
        "within_budget_count": (
            sum(1 for value in values if value <= budget_ms) if budget_ms is not None else None
        ),
        "within_budget_ratio": (
            sum(1 for value in values if value <= budget_ms) / count
            if budget_ms is not None and count
            else None
        ),
    }


def _normalized_record(raw: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    contract = _mapping(raw.get("fast_lane")) or raw
    if _text(contract.get("schema")) != FAST_LANE_SCHEMA_VERSION:
        return None, "fast_lane_contract_schema_invalid"
    correlation_id = _text(contract.get("correlation_id"))
    outer_correlation = _text(raw.get("correlation_id"))
    if not correlation_id:
        return None, "fast_lane_correlation_id_missing"
    if outer_correlation and outer_correlation != correlation_id:
        return None, "fast_lane_correlation_id_wrapper_mismatch"
    timestamps = _mapping(contract.get("timestamps"))
    t1 = _text(timestamps.get("first_actionable_seen_at"))
    t3 = _text(timestamps.get("transaction_b_committed_at"))
    if not t1 or not t3:
        return None, "fast_lane_t1_or_t3_timestamp_missing"
    t1_t3 = _elapsed_ms(t1, t3)
    if t1_t3 is None:
        return None, "fast_lane_t1_to_t3_timestamp_invalid"
    t4 = _text(timestamps.get("decision_intent_durable_at"))
    t5 = _text(timestamps.get("order_submitted_at"))
    return {
        "correlation_id": correlation_id,
        "event_date": _text(raw.get("event_date")) or "unknown_event_date",
        "event_type": _text(raw.get("event_type") or raw.get("report_type")) or "unknown_event_type",
        "mode": _text(contract.get("mode")) or "shadow",
        "contract_ok": bool(contract.get("ok")),
        "contract_blockers": list(contract.get("blockers") or []),
        "t1_to_t3_ms": t1_t3,
        "t1_to_t4_ms": _elapsed_ms(t1, t4) if t4 else None,
        "t1_to_t5_ms": _elapsed_ms(t1, t5) if t5 else None,
        "has_t5": bool(t5),
    }, ""


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    t1_t3 = [float(row["t1_to_t3_ms"]) for row in rows]
    shadow_t1_t4 = [
        float(row["t1_to_t4_ms"])
        for row in rows
        if row["mode"] == "shadow" and row["t1_to_t4_ms"] is not None
    ]
    live_rows = [row for row in rows if row["mode"] == "live"]
    live_t1_t5 = [float(row["t1_to_t5_ms"]) for row in live_rows if row["t1_to_t5_ms"] is not None]
    return {
        "record_count": len(rows),
        "contract_failed_count": sum(1 for row in rows if not row["contract_ok"]),
        "t1_to_t3": _metric(t1_t3, budget_ms=T1_TO_T3_BUDGET_MS),
        "shadow_t1_to_t4": _metric(shadow_t1_t4, budget_ms=None),
        "live_t1_to_t5": {
            **_metric(live_t1_t5, budget_ms=T1_TO_T5_BUDGET_MS),
            "expected_live_count": len(live_rows),
            "missing_t5_count": len(live_rows) - len(live_t1_t5),
        },
    }


def build_observation_fast_lane_latency_slo_report(
    records: Iterable[Mapping[str, Any]],
    *,
    require_live_t1_to_t5: bool = False,
) -> dict[str, Any]:
    """Aggregate immutable P10 packets without writing, clock synthesis, or orders."""

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            rejected.append({"index": str(index), "reason": "fast_lane_record_not_mapping"})
            continue
        normalized, reason = _normalized_record(raw)
        if normalized is None:
            rejected.append(
                {
                    "index": str(index),
                    "correlation_id": _text(_mapping(raw.get("fast_lane")).get("correlation_id") or raw.get("correlation_id")),
                    "reason": reason,
                }
            )
            continue
        accepted.append(normalized)

    overall = _aggregate(accepted)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in accepted:
        groups.setdefault((row["event_date"], row["event_type"]), []).append(row)
    grouped = [
        {"event_date": event_date, "event_type": event_type, **_aggregate(rows)}
        for (event_date, event_type), rows in sorted(groups.items())
    ]
    blockers: list[str] = []
    if not accepted:
        blockers.append("fast_lane_latency_no_usable_records")
    if rejected:
        blockers.append("fast_lane_latency_malformed_records_present")
    if overall["contract_failed_count"]:
        blockers.append("fast_lane_latency_contract_failure_present")
    t1_t3 = _mapping(overall.get("t1_to_t3"))
    if t1_t3.get("sample_count") and float(t1_t3.get("p99_ms") or 0) > T1_TO_T3_BUDGET_MS:
        blockers.append("fast_lane_t1_to_t3_p99_slo_exceeded")
    live_t1_t5 = _mapping(overall.get("live_t1_to_t5"))
    if require_live_t1_to_t5:
        if not live_t1_t5.get("expected_live_count"):
            blockers.append("fast_lane_live_t1_to_t5_samples_missing")
        elif live_t1_t5.get("missing_t5_count"):
            blockers.append("fast_lane_live_t1_to_t5_timestamp_missing")
        elif float(live_t1_t5.get("p99_ms") or 0) > T1_TO_T5_BUDGET_MS:
            blockers.append("fast_lane_t1_to_t5_p99_slo_exceeded")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "within_slo" if not blockers else "blocked",
        "require_live_t1_to_t5": bool(require_live_t1_to_t5),
        "overall": overall,
        "by_event_date_and_type": grouped,
        "rejected_records": rejected,
        "blockers": sorted(set(blockers)),
        "quantile_method": "nearest_rank",
        "read_only": True,
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "places_orders": False,
            "restarts_launchd": False,
            "synthesizes_timestamps": False,
        },
    }


__all__ = [
    "SCHEMA_VERSION",
    "T1_TO_T3_BUDGET_MS",
    "T1_TO_T5_BUDGET_MS",
    "build_observation_fast_lane_latency_slo_report",
]
