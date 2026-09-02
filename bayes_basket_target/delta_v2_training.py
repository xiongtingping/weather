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


def build_stage_zero_delta_v2_training_fact(
    conn: sqlite3.Connection,
    *,
    compiled_plan: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    atomic_bundle: Mapping[str, Any],
    created_at_utc: str,
) -> dict[str, Any]:
    """Build a pre-final, no-execution fact from exact P0/P0-M evidence.

    A stage-zero decision has no canonical METAR/SPECI observation or hard
    bound.  It must therefore use its own NO_EXECUTION atomic bundle rather
    than borrowing the P1/M5 writer's weather facts.  The common validator
    already enforces the exact probability, official WS, inventory,
    order-state and policy lineage for all supported probability stages.
    """

    envelope = compiled_plan.get("probability_envelope")
    if not isinstance(envelope, Mapping):
        raise ContractError("stage_zero_delta_v2_probability_mapping_required")
    stage = str(envelope.get("stage") or "").strip().upper()
    if stage not in {"P0", "P0-M"}:
        raise ContractError("stage_zero_delta_v2_probability_stage_invalid")
    return build_delta_v2_training_fact(
        conn,
        compiled_plan=compiled_plan,
        market_snapshot=market_snapshot,
        atomic_bundle=atomic_bundle,
        created_at_utc=created_at_utc,
    )


#: The fact columns that are inputs to ``decision_scope_key`` itself.
#:
#: Everything else in the row either derives from a wall clock, names a
#: snapshot generation, or is an instance identity of an answer the key has
#: already compared in full -- none of which the key treats as a new
#: decision.  These, by contrast, cannot differ between two rows sharing a
#: key unless the key collided or the row is corrupt, which is what the guard
#: below exists to catch.
SCOPE_DETERMINING_COLUMNS: tuple[str, ...] = (
    "station_id",
    "event_date",
    "settlement_timezone",
    "stage",
    "probability_envelope_id",
    "posterior_sha256",
    "official_exclusion_mask_sha256",
    "quote_freshness_policy_sha256",
)


def committed_scope_fact_id(
    conn: sqlite3.Connection,
    *,
    fact: Mapping[str, Any],
) -> str | None:
    """Return the ``fact_id`` already holding this fact's decision scope.

    Read-only, and the single owner of the scope-divergence rule: a caller
    that must decide something *before* it writes gets the identical answer
    from the identical code, rather than a second copy of the comparison
    that can drift out of agreement with the writer.

    Raises the same named conflict the writer raises when a stored row
    disagrees on a scope-defining field.
    """

    supplied = {name: fact.get(name) for name in FACT_COLUMNS}
    existing_scope = conn.execute(
        "SELECT " + ",".join(SCOPE_DETERMINING_COLUMNS) + ",fact_id "
        "FROM basket_policy_training_fact WHERE decision_scope_key=?",
        (supplied["decision_scope_key"],),
    ).fetchone()
    if existing_scope is None:
        return None
    stored = dict(zip(SCOPE_DETERMINING_COLUMNS, existing_scope))
    divergent = sorted(
        name
        for name in SCOPE_DETERMINING_COLUMNS
        if str(stored[name] or "") != str(supplied[name] or "")
    )
    if divergent:
        raise ContractError(
            "delta_v2_training_fact_scope_conflict:" + ",".join(divergent)
        )
    return str(existing_scope[-1])


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
    held_fact_id = committed_scope_fact_id(conn, fact=supplied)
    if held_fact_id is not None:
        # The key is the idempotence test, not the content hash.
        #
        # ``decision_scope_key`` addresses one economically distinct decision:
        # the probability, the position book, the promoted policy artifacts,
        # and -- verbatim, unrounded -- what the objective did with the edge:
        # ``selected_score``, ``final_support``, ``selected_buckets``, and
        # every action's ``limit_price``, ``maximum_shares`` and ``cash_clip``.
        #
        # The raw price is deliberately absent, but the edge is not: a key
        # match means the edge curve did not move the objective's answer by
        # one bit.  That is structural rather than statistical -- the sizing
        # instruction is inside the hash -- and the data agrees: over the 145
        # adjacent fact pairs since 2026-08-28 whose belief was unchanged, the
        # edge curve moved in 82 and a sizing output moved in all 82 of them.
        #
        # What the fact's own ``content_sha256`` covers, on top of all that,
        # is the quote generation, the snapshot ids and the plan sha, which
        # turn over every cycle whether or not anything was decided.  So the
        # two halves disagreed about what "the same decision" means, and the
        # comparison half won by raising.
        #
        # That was rare while P0 re-minted an envelope every few minutes,
        # because the key rarely repeated.  Suppressing those restatements on
        # 2026-09-01 made the key stable and the conflict routine: from single
        # digits an hour to 50 and 63, and it became the largest single
        # stage-open blocker on the forward day -- 126 of 200 sampled, on the
        # very day the ladder exists to open early.  Verified on twelve of
        # them: the stored fact pointed at the current newest P0 envelope in
        # all twelve, and the WS snapshot had turned over in all eight checked.
        #
        # One sample per belief state is also the right training set.  A
        # repeated observation of an unchanged belief against a jiggling book
        # is not new information about the belief, and weighting it by how
        # long the scope happened to live is a bias, not a signal.
        #
        # What is still compared is the scope's own defining fields.  They are
        # inputs to the key, so they cannot differ without a hash collision or
        # a corrupt row -- which is the fault this guard is actually for.
        return {
            "schema": FACT_SCHEMA,
            "state": "already_committed",
            "fact_id": held_fact_id,
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
    "committed_scope_fact_id",
    "build_delta_v2_training_fact",
    "build_stage_zero_delta_v2_training_fact",
    "commit_delta_v2_training_fact",
    "delta_v2_decision_scope_key",
]
