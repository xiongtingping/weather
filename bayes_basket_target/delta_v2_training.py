"""Forward training facts for the production DeltaBasketScore v2 lane."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .forward_training_validation import (
    build_validated_forward_fact_delta_v2,
    delta_v2_decision_scope_key as validated_delta_v2_decision_scope_key,
)


FACT_SCHEMA = "delta_basket_score_training_fact_v2"

FACT_COLUMNS = (
    "fact_id", "station_id", "event_date", "settlement_timezone",
    "decision_at_utc", "decision_scope_key", "captured_at_utc",
    "source_sequence", "quote_observed_at", "quote_expires_at",
    "quote_freshness_policy_sha256", "stage", "probability_envelope_id",
    "posterior_version", "posterior_sha256", "ws_snapshot_id",
    "official_exclusion_mask_sha256", "quote_version", "quote_sha256",
    "inventory_snapshot_id", "inventory_version", "inventory_sha256",
    "order_state_snapshot_id", "order_state_version", "order_state_sha256",
    "cycle_id", "decision_id", "decision_sha256", "plan_sha256",
    "intent_count", "intent_set_sha256", "policy_version_vector_json",
    "feature_json", "partition_key", "authority_scope", "created_at_utc",
    "content_sha256",
)


def delta_v2_decision_scope_key(
    *,
    compiled_plan: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
) -> str:
    return validated_delta_v2_decision_scope_key(
        compiled_plan=compiled_plan,
        market_snapshot=market_snapshot,
    )


def build_delta_v2_training_fact(
    conn: sqlite3.Connection,
    *,
    compiled_plan: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    atomic_bundle: Mapping[str, Any],
    created_at_utc: str,
) -> dict[str, Any]:
    """Build native v2 evidence from the exact production decision inputs."""

    base = build_validated_forward_fact_delta_v2(
        conn,
        compiled_plan=compiled_plan,
        market_snapshot=market_snapshot,
        atomic_bundle=atomic_bundle,
        created_at_utc=created_at_utc,
    )
    identity = {
        key: value
        for key, value in base.items()
        if key not in {"fact_id", "content_sha256"}
    }
    identity["decision_scope_key"] = delta_v2_decision_scope_key(
        compiled_plan=compiled_plan,
        market_snapshot=market_snapshot,
    )
    fact_sha = content_sha256({"schema": FACT_SCHEMA, **identity})
    return {
        "schema": FACT_SCHEMA,
        "fact_id": f"d2fact_{fact_sha[:40]}",
        **identity,
        "content_sha256": fact_sha,
    }


def commit_delta_v2_training_fact(
    conn: sqlite3.Connection,
    *,
    fact: Mapping[str, Any],
) -> dict[str, Any]:
    if fact.get("schema") != FACT_SCHEMA:
        raise ContractError("delta_v2_training_fact_schema_invalid")
    supplied = {name: fact.get(name) for name in FACT_COLUMNS}
    identity = {
        name: supplied[name]
        for name in FACT_COLUMNS
        if name not in {"fact_id", "content_sha256"}
    }
    expected_sha = content_sha256({"schema": FACT_SCHEMA, **identity})
    expected_id = f"d2fact_{expected_sha[:40]}"
    if (
        supplied["fact_id"] != expected_id
        or supplied["content_sha256"] != expected_sha
    ):
        raise ContractError("delta_v2_training_fact_content_address_mismatch")
    existing_scope = conn.execute(
        "SELECT fact_id,content_sha256 FROM basket_policy_training_fact "
        "WHERE decision_scope_key=?",
        (supplied["decision_scope_key"],),
    ).fetchone()
    if existing_scope is not None:
        if (
            str(existing_scope[0]) != expected_id
            or str(existing_scope[1]) != expected_sha
        ):
            raise ContractError("delta_v2_training_fact_scope_conflict")
        return {
            "schema": FACT_SCHEMA,
            "state": "already_committed",
            "fact_id": expected_id,
            "idempotent": True,
        }
    conn.execute(
        "INSERT INTO basket_policy_training_fact("
        + ",".join(FACT_COLUMNS)
        + ") VALUES("
        + ",".join("?" for _ in FACT_COLUMNS)
        + ")",
        tuple(supplied[name] for name in FACT_COLUMNS),
    )
    return {
        "schema": FACT_SCHEMA,
        "state": "committed",
        "fact_id": expected_id,
        "idempotent": False,
    }


__all__ = [
    "FACT_SCHEMA",
    "build_delta_v2_training_fact",
    "commit_delta_v2_training_fact",
    "delta_v2_decision_scope_key",
]
