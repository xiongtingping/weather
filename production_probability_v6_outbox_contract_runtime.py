"""Production-native materializers for v6 posterior outbox contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


SCHEMA_VERSION = "production_probability_v6_outbox_contract_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_utc(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _iso_ms(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def materialize_v6_outbox_row(
    checkpoint_row: Mapping[str, Any],
    *,
    fact_snapshot_id: str,
    correlation_id: str,
    outbox_event_id: str,
) -> dict[str, Any]:
    checkpoint = _mapping(checkpoint_row)
    required = (
        outbox_event_id,
        checkpoint.get("posterior_run_id"),
        fact_snapshot_id,
        correlation_id,
        checkpoint.get("committed_at"),
    )
    if any(not _text(value) for value in required):
        raise ValueError("v6_outbox_identity_missing")
    return {
        "outbox_event_id": _text(outbox_event_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "fact_snapshot_id": _text(fact_snapshot_id),
        "correlation_id": _text(correlation_id),
        "state": "pending",
        "created_at": _text(checkpoint.get("committed_at")),
        "attempt_count": 0,
        "claim_generation": 0,
    }


def materialize_v6_outbox_execution_contract_row(
    *,
    outbox_row: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    canonical_observation_event: Mapping[str, Any],
    inference_decision_deadline_at: str,
) -> dict[str, Any]:
    outbox = _mapping(outbox_row)
    checkpoint = _mapping(checkpoint_row)
    event = _mapping(canonical_observation_event)
    first_actionable = _parse_utc(event.get("first_actionable_seen_at"))
    committed = _parse_utc(checkpoint.get("committed_at"))
    inference_deadline = _parse_utc(inference_decision_deadline_at)
    if first_actionable is None or committed is None or inference_deadline is None:
        raise ValueError("v6_outbox_execution_time_missing_or_invalid")
    hard_deadline = first_actionable + timedelta(milliseconds=3000)
    if inference_deadline != hard_deadline:
        raise ValueError("v6_outbox_inference_deadline_not_exactly_t1_plus_3000ms")
    eligible = committed <= hard_deadline
    return {
        "outbox_event_id": _text(outbox.get("outbox_event_id")),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "canonical_observation_event_id": _text(
            event.get("canonical_observation_event_id")
        ),
        "first_actionable_seen_at": _iso_ms(first_actionable),
        "observation_to_order_hard_deadline_at": _iso_ms(hard_deadline),
        "inference_decision_deadline_at": _iso_ms(inference_deadline),
        "transaction_b_committed_at": _iso_ms(committed),
        "execution_eligible_at_commit": 1 if eligible else 0,
        "execution_na_reason": (
            ""
            if eligible
            else "transaction_b_committed_after_observation_to_order_hard_deadline"
        ),
        "deadline_policy": "canonical_first_actionable_plus_3000ms",
        "created_at": _iso_ms(committed),
    }


__all__ = [
    "SCHEMA_VERSION",
    "materialize_v6_outbox_execution_contract_row",
    "materialize_v6_outbox_row",
]
