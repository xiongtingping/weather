"""Commit one v6 candidate through the physical or approved market branch."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from production_probability_v6_market_transaction_writer import (
    commit_v6_market_transaction_b,
)
from production_probability_v6_public_ssot_writer import (
    commit_v6_public_physical_transaction,
)
from production_probability_v6_transaction_branch_runtime import (
    build_v6_transaction_branch_bundle,
)


SCHEMA_VERSION = "production_probability_v6_transaction_commit_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sha(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_dt(value: Any) -> datetime:
    text = _text(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def commit_v6_candidate_transaction(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    previous_checkpoint: Mapping[str, Any],
    candidate: Mapping[str, Any],
    canonical_observation_event: Mapping[str, Any],
    market_inputs: Mapping[str, Any] | None,
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
) -> dict[str, Any]:
    previous = _mapping(previous_checkpoint)
    event = _mapping(canonical_observation_event)
    inputs = _mapping(market_inputs)
    context_result = _mapping(inputs.get("market_context_result"))
    if not context_result:
        context_result = {
            "ok": True,
            "state": "disabled",
            "market_projection_available": False,
            "na_reason": _text(inputs.get("market_projection_na_reason"))
            or "market_projection_context_not_prewarmed",
            "blocks_physical_x": False,
        }
    trigger = _parse_dt(event.get("first_actionable_seen_at"))
    outbox_event_id = "pout_" + _sha(
        {"fact_snapshot_id": fact_snapshot_id, "correlation_id": correlation_id}
    )[:24]
    branch = build_v6_transaction_branch_bundle(
        activation=activation,
        previous_checkpoint=previous,
        candidate=candidate,
        canonical_observation_event=event,
        market_context_result=context_result,
        bridge_activation=_mapping(inputs.get("bridge_activation")),
        bridge_artifact=_mapping(inputs.get("bridge_artifact")),
        physical_resolution_contract=_mapping(
            inputs.get("physical_resolution_contract")
        ),
        realization_policy=_mapping(inputs.get("realization_policy")),
        fact_snapshot_id=fact_snapshot_id,
        slug=slug,
        event_date=event_date,
        fact_as_of=fact_as_of,
        correlation_id=correlation_id,
        outbox_event_id=outbox_event_id,
        inference_decision_deadline_at=_iso(trigger + timedelta(seconds=3)),
    )
    common = {
        "activation": activation,
        "expected_previous_checkpoint_id": _text(
            previous.get("posterior_checkpoint_id")
        ),
        "expected_previous_posterior_run_id": _text(previous.get("posterior_run_id")),
        "strong_update_row": _mapping(branch.get("strong_update_row")),
        "checkpoint_row": _mapping(branch.get("checkpoint_row")),
        "public_fact_row": _mapping(branch.get("public_fact_row")),
    }
    if _text(branch.get("writer_route")) == "market_transaction_b":
        committed = commit_v6_market_transaction_b(
            conn,
            **common,
            bridge_activation=_mapping(inputs.get("bridge_activation")),
            realization_state_row=_mapping(branch.get("realization_state_row")),
            outbox_row=_mapping(branch.get("outbox_row")),
            outbox_execution_contract_row=_mapping(
                branch.get("outbox_execution_contract_row")
            ),
        )
    else:
        committed = commit_v6_public_physical_transaction(conn, **common)
    return {
        "schema": SCHEMA_VERSION,
        "ok": committed.get("ok") is True,
        "state": _text(committed.get("state")),
        "writer_route": _text(branch.get("writer_route")),
        "market_projection_available": bool(
            branch.get("market_projection_available")
        ),
        "market_projection_na_reason": _text(
            branch.get("market_projection_na_reason")
        ),
        "market_block_reasons": list(branch.get("market_block_reasons") or []),
        "commit": committed,
        "blocks_physical_x": False,
        "places_orders": False,
        "clob": False,
    }


__all__ = ["SCHEMA_VERSION", "commit_v6_candidate_transaction"]
