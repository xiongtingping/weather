"""Read-only deterministic replay of the durable target SSOT chain.

The replay starts from append-only ``basket_policy_training_fact`` anchors
and resolves every referenced production object by exact id and SHA.  It is
an acceptance/audit path only: it never writes SQLite, calls a gateway,
submits orders, waits for settlement labels, or consults forward OOS as a
``production_no_live`` gate.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from .atomic_ledger import DECISION_SCHEMA
from .contracts import ContractError, ProbabilityEnvelope, content_sha256
from .delta_v2_training import FACT_COLUMNS, FACT_SCHEMA
from .resource_bounds import assert_process_memory_budget


SCHEMA = "bayes_basket_target_durable_ssot_replay_v1"
CONTINUATION_SCHEMA = "bayes_basket_target_durable_ssot_replay_continuation_v1"
MAX_FACTS = 512
MAX_JSON_BYTES = 262_144
STAGE_RANK = {"P0": 0, "P0-M": 1, "P1": 2}

_REQUIRED_TABLE_COLUMNS = {
    "basket_policy_training_fact": set(FACT_COLUMNS),
    "basket_probability_envelope": {
        "envelope_id", "stage", "checkpoint_id", "station_id", "event_date",
        "settlement_timezone", "decision_at_utc", "fresh_until_utc",
        "bucket_ordinals_json", "probabilities_json", "posterior_version",
        "posterior_sha256", "bridge_sha256", "lineage_json",
        "content_sha256", "state",
    },
    "basket_official_ws_snapshot": {
        "snapshot_id", "station_id", "event_date", "settlement_timezone",
        "sequence", "observed_at", "quote_version", "quote_observed_at",
        "quote_expires_at", "quote_source", "inventory_version",
        "order_state_version", "content_sha256", "state",
    },
    "basket_confirmed_inventory_snapshot": {
        "inventory_snapshot_id", "station_id", "event_date",
        "inventory_version", "observed_at", "complete", "content_sha256",
        "state",
    },
    "basket_order_state_snapshot": {
        "order_state_snapshot_id", "station_id", "event_date",
        "order_state_version", "observed_at", "known", "content_sha256",
        "state",
    },
    "basket_target_execution_cycles": {
        "cycle_id", "plan_id", "station_id", "event_date",
        "settlement_timezone", "strategy_lane", "plan_sha256", "state",
    },
    "auto_trade_decisions": {
        "decision_id", "cycle_id", "plan_id", "station_id", "event_date",
        "settlement_timezone", "strategy_lane", "decided_state",
        "will_order", "reason_codes_json", "details_json", "decision_sha256",
        "target_execution_hard_deadline_at",
    },
    "basket_target_order_intents": {
        "order_intent_id", "decision_id", "cycle_id", "live_submit_authorized",
        "intent_sha256",
    },
    "basket_target_execution_version_vectors": {
        "order_intent_id", "cycle_id", "decision_id", "vector_policy",
        "vector_sha256",
    },
    "basket_target_capital_reservations": {
        "cycle_id", "amount_usdc", "state",
    },
    "basket_target_market_registry": {
        "station_id", "event_date", "settlement_timezone",
        "source_eligible",
    },
}


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"durable_replay_{name}_missing")
    return result


def _utc(name: str, value: Any) -> datetime:
    raw = _text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"durable_replay_{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"durable_replay_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(name: str, value: Any, *, mapping: bool | None = None) -> Any:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_JSON_BYTES:
        raise ContractError(f"durable_replay_{name}_invalid_or_oversized")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(f"durable_replay_{name}_invalid_json") from exc
    if mapping is True and not isinstance(parsed, Mapping):
        raise ContractError(f"durable_replay_{name}_mapping_required")
    if mapping is False and not isinstance(parsed, list):
        raise ContractError(f"durable_replay_{name}_list_required")
    return parsed


def _row(conn: sqlite3.Connection, sql: str, values: tuple[Any, ...], name: str) -> dict[str, Any]:
    raw = conn.execute(sql, values).fetchone()
    if raw is None:
        raise ContractError(f"durable_replay_{name}_missing")
    if not isinstance(raw, sqlite3.Row):
        raise ContractError("durable_replay_row_factory_required")
    return {key: raw[key] for key in raw.keys()}


def _schema_gate(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table, required in _REQUIRED_TABLE_COLUMNS.items():
        if table not in tables:
            raise ContractError(f"durable_replay_table_missing:{table}")
        columns = {
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')
        }
        missing = sorted(required - columns)
        if missing:
            raise ContractError(
                f"durable_replay_schema_incomplete:{table}:" + ",".join(missing)
            )


def _verify_fact_content(fact: Mapping[str, Any]) -> None:
    identity = {
        name: fact.get(name)
        for name in FACT_COLUMNS
        if name not in {"fact_id", "content_sha256"}
    }
    expected = content_sha256({"schema": FACT_SCHEMA, **identity})
    if (
        fact.get("content_sha256") != expected
        or fact.get("fact_id") != f"d2fact_{expected[:40]}"
    ):
        raise ContractError("durable_replay_training_fact_content_drift")


def _probability(conn: sqlite3.Connection, fact: Mapping[str, Any]) -> ProbabilityEnvelope:
    row = _row(
        conn,
        "SELECT * FROM basket_probability_envelope WHERE envelope_id=?",
        (fact["probability_envelope_id"],),
        "probability_envelope",
    )
    if row["state"] != "COMMITTED":
        raise ContractError("durable_replay_probability_not_committed")
    raw = {
        "stage": row["stage"],
        "checkpoint_id": row["checkpoint_id"],
        "station_id": row["station_id"],
        "event_date": row["event_date"],
        "settlement_timezone": row["settlement_timezone"],
        "decision_at_utc": row["decision_at_utc"],
        "fresh_until_utc": row["fresh_until_utc"],
        "bucket_ordinals": _json(
            "probability_bucket_ordinals", row["bucket_ordinals_json"], mapping=True
        ),
        "probabilities": _json(
            "probability_vector", row["probabilities_json"], mapping=True
        ),
        "posterior_version": row["posterior_version"],
        "posterior_sha256": row["posterior_sha256"],
        "bridge_sha256": row["bridge_sha256"],
        "lineage": _json(
            "probability_lineage", row["lineage_json"], mapping=True
        ),
    }
    envelope = ProbabilityEnvelope.from_mapping(raw)
    if (
        row["envelope_id"] != envelope.checkpoint_id
        or row["content_sha256"] != envelope.content_sha256
        or fact["stage"] != envelope.stage
        or fact["posterior_version"] != envelope.posterior_version
        or fact["posterior_sha256"] != envelope.posterior_sha256
    ):
        raise ContractError("durable_replay_probability_binding_drift")
    return envelope


def _decision_sha(row: Mapping[str, Any]) -> str:
    identity = {
        "cycle_id": row["cycle_id"],
        "plan_id": row["plan_id"],
        "station_id": row["station_id"],
        "event_date": row["event_date"],
        "settlement_timezone": row["settlement_timezone"],
        "strategy_lane": row["strategy_lane"],
        "decided_state": row["decided_state"],
        "will_order": bool(row["will_order"]),
        "reason_codes": _json(
            "decision_reason_codes", row["reason_codes_json"], mapping=False
        ),
        "details": _json("decision_details", row["details_json"], mapping=True),
        "target_execution_hard_deadline_at": row[
            "target_execution_hard_deadline_at"
        ],
    }
    return content_sha256({"schema": DECISION_SCHEMA, **identity})


def _scope(
    value: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    name: str,
) -> None:
    if (
        str(value.get("station_id") or "") != station_id
        or str(value.get("event_date") or "") != event_date
        or (
            "settlement_timezone" in value
            and str(value.get("settlement_timezone") or "")
            != settlement_timezone
        )
    ):
        raise ContractError(f"durable_replay_{name}_scope_mismatch")


def _string_list(name: str, value: Any) -> list[str] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        raise ContractError(f"durable_replay_{name}_invalid")
    return [str(item) for item in value]


def _ws_economic_semantics(raw: Any) -> tuple[str, dict[str, Any]]:
    buckets = _json("official_ws_buckets", raw, mapping=False)
    if not buckets:
        raise ContractError("durable_replay_official_ws_buckets_empty")
    projected: list[dict[str, Any]] = []
    for item in buckets:
        if not isinstance(item, Mapping):
            raise ContractError("durable_replay_official_ws_bucket_invalid")
        bucket = _text("official_ws_bucket", item.get("bucket"))
        ask_levels = item.get("ask_levels") or []
        bid_levels = item.get("bid_levels") or []
        if not isinstance(ask_levels, list) or not isinstance(bid_levels, list):
            raise ContractError("durable_replay_official_ws_levels_invalid")
        projected.append({
            "bucket": bucket,
            "ordinal": item.get("ordinal"),
            "token_id": item.get("token_id"),
            "ask_available": bool(item.get("ask_available", bool(ask_levels))),
            "ask_levels": ask_levels,
            "bid_available": bool(item.get("bid_available", bool(bid_levels))),
            "bid_levels": bid_levels,
            "price_tick_size": item.get("price_tick_size"),
            "maker_queue_ahead_shares": item.get(
                "maker_queue_ahead_shares"
            ),
            "maker_queue_known": item.get("maker_queue_known"),
        })
    identity = {
        "schema": "bayes_basket_target_official_ws_economic_semantics_v1",
        "buckets": projected,
    }
    return content_sha256(identity), identity


def _core_direction_projection(
    feature: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    plan = feature.get("plan")
    if not isinstance(plan, Mapping):
        return {
            "contract_state": "LEGACY_NA",
            "plan_state": None,
            "core_buckets": None,
            "current_core_buckets": None,
            "direction": None,
            "current_direction": None,
            "final_support": None,
        }
    core = _string_list("core_buckets", plan.get("core_buckets"))
    current_core = _string_list(
        "current_core_buckets", plan.get("current_core_buckets")
    )
    final_support = _string_list(
        "final_support", plan.get("final_support")
    )
    direction_raw = plan.get("direction")
    direction = (
        str(direction_raw).strip().upper()
        if direction_raw not in (None, "")
        else None
    )
    current_direction_raw = plan.get("current_direction")
    current_direction = (
        str(current_direction_raw).strip().upper()
        if current_direction_raw not in (None, "")
        else None
    )
    if direction is not None and direction not in {"UP", "DOWN", "NEUTRAL"}:
        raise ContractError("durable_replay_direction_invalid")
    if (
        current_direction is not None
        and current_direction not in {"UP", "DOWN", "NEUTRAL"}
    ):
        raise ContractError("durable_replay_current_direction_invalid")
    explicit = core is not None or current_core is not None
    if explicit and (core is None or current_core is None or direction is None):
        raise ContractError("durable_replay_core_direction_contract_partial")
    state = str(plan.get("state") or "").strip().upper() or None
    return {
        "contract_state": "EXPLICIT_V1" if explicit else "LEGACY_NA",
        "plan_state": state,
        "decision_state": str(decision.get("decided_state") or ""),
        "core_buckets": core,
        "current_core_buckets": current_core,
        "direction": direction,
        "current_direction": current_direction,
        # Final support is deliberately separate from the core.  It must
        # never be used as a fallback core reconstruction.
        "final_support": final_support,
    }


def _validate_event(
    conn: sqlite3.Connection,
    fact: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
) -> dict[str, Any]:
    _verify_fact_content(fact)
    _scope(
        fact,
        station_id=station_id,
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        name="training_fact",
    )
    if fact["authority_scope"] != "PRODUCTION_NO_LIVE_FORWARD":
        raise ContractError("durable_replay_training_fact_authority_invalid")
    envelope = _probability(conn, fact)
    _scope(
        envelope.to_dict(),
        station_id=station_id,
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        name="probability",
    )

    ws = _row(
        conn,
        "SELECT * FROM basket_official_ws_snapshot WHERE snapshot_id=?",
        (fact["ws_snapshot_id"],),
        "official_ws_snapshot",
    )
    _scope(
        ws,
        station_id=station_id,
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        name="official_ws",
    )
    if (
        ws["state"] != "COMMITTED"
        or ws["quote_source"] != "OFFICIAL_CLOB_WS"
        or ws["quote_version"] != fact["quote_version"]
        or ws["content_sha256"] != fact["quote_sha256"]
        or int(ws["sequence"]) != int(fact["source_sequence"])
    ):
        raise ContractError("durable_replay_official_ws_binding_drift")
    ws_semantic_sha256, _ws_semantics = _ws_economic_semantics(
        ws["buckets_json"]
    )
    quote_at = _utc("quote_observed_at", ws["quote_observed_at"])
    quote_expires = _utc("quote_expires_at", ws["quote_expires_at"])
    decision_at = _utc("decision_at_utc", fact["decision_at_utc"])
    if (
        quote_at >= quote_expires
        or decision_at >= quote_expires
        or fact["quote_observed_at"] != ws["quote_observed_at"]
        or fact["quote_expires_at"] != ws["quote_expires_at"]
    ):
        raise ContractError("durable_replay_quote_freshness_drift")

    inventory = _row(
        conn,
        "SELECT * FROM basket_confirmed_inventory_snapshot "
        "WHERE inventory_snapshot_id=?",
        (fact["inventory_snapshot_id"],),
        "inventory_snapshot",
    )
    _scope(
        inventory,
        station_id=station_id,
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        name="inventory",
    )
    if (
        inventory["state"] != "COMMITTED"
        or int(inventory["complete"]) != 1
        or inventory["inventory_version"] != fact["inventory_version"]
        or inventory["inventory_version"] != ws["inventory_version"]
        or inventory["content_sha256"] != fact["inventory_sha256"]
    ):
        raise ContractError("durable_replay_inventory_binding_drift")

    order_state = _row(
        conn,
        "SELECT * FROM basket_order_state_snapshot "
        "WHERE order_state_snapshot_id=?",
        (fact["order_state_snapshot_id"],),
        "order_state_snapshot",
    )
    _scope(
        order_state,
        station_id=station_id,
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        name="order_state",
    )
    if (
        order_state["state"] != "COMMITTED"
        or int(order_state["known"]) != 1
        or order_state["order_state_version"] != fact["order_state_version"]
        or order_state["order_state_version"] != ws["order_state_version"]
        or order_state["content_sha256"] != fact["order_state_sha256"]
    ):
        raise ContractError("durable_replay_order_state_binding_drift")

    cycle = _row(
        conn,
        "SELECT * FROM basket_target_execution_cycles WHERE cycle_id=?",
        (fact["cycle_id"],),
        "execution_cycle",
    )
    _scope(
        cycle,
        station_id=station_id,
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        name="cycle",
    )
    if (
        cycle["strategy_lane"] != "basket"
        or cycle["plan_sha256"] != fact["plan_sha256"]
    ):
        raise ContractError("durable_replay_cycle_binding_drift")

    decision = _row(
        conn,
        "SELECT * FROM auto_trade_decisions WHERE decision_id=?",
        (fact["decision_id"],),
        "decision",
    )
    _scope(
        decision,
        station_id=station_id,
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        name="decision",
    )
    decision_hash_matches_current_contract = (
        decision["decision_sha256"] == _decision_sha(decision)
    )
    if (
        decision["cycle_id"] != fact["cycle_id"]
        or decision["plan_id"] != cycle["plan_id"]
        or decision["strategy_lane"] != "basket"
        or decision["decision_sha256"] != fact["decision_sha256"]
    ):
        raise ContractError("durable_replay_decision_binding_drift")

    intents = conn.execute(
        "SELECT order_intent_id,live_submit_authorized,intent_sha256 "
        "FROM basket_target_order_intents WHERE cycle_id=? "
        "ORDER BY rowid",
        (fact["cycle_id"],),
    ).fetchall()
    if len(intents) != int(fact["intent_count"]):
        raise ContractError("durable_replay_intent_count_drift")
    if any(int(row[1]) != 0 for row in intents):
        raise ContractError("durable_replay_live_intent_forbidden")
    intent_refs = sorted(
        (str(row[0]), str(row[2]))
        for row in intents
    )
    if content_sha256(intent_refs) != fact["intent_set_sha256"]:
        raise ContractError("durable_replay_intent_set_drift")

    vectors = conn.execute(
        "SELECT order_intent_id,decision_id,vector_policy,vector_sha256 "
        "FROM basket_target_execution_version_vectors WHERE cycle_id=? "
        "ORDER BY rowid",
        (fact["cycle_id"],),
    ).fetchall()
    feature = _json("training_feature", fact["feature_json"], mapping=True)
    boundary = feature.get("no_live_boundary")
    if not isinstance(boundary, Mapping) or (
        boundary.get("execution_mode") != "production_no_live"
        or boundary.get("live_submit_authorized") is not False
        or boundary.get("calls_order_gateway") is not False
        or int(boundary.get("orders_submitted") or 0) != 0
    ):
        raise ContractError("durable_replay_no_live_boundary_drift")
    if intents:
        if (
            len(vectors) != len(intents)
            or any(
                row[2] != "EXECUTION"
                or row[0] != intents[index][0]
                or row[1] != fact["decision_id"]
                for index, row in enumerate(vectors)
            )
        ):
            raise ContractError("durable_replay_execution_vector_drift")
        feature_vectors = feature.get("execution_version_vectors")
        if not isinstance(feature_vectors, list) or [
            item.get("vector_sha256") for item in feature_vectors
            if isinstance(item, Mapping)
        ] != [str(row[3]) for row in vectors]:
            raise ContractError("durable_replay_feature_vector_drift")
        execution_policy = "EXECUTION"
    else:
        no_execution = feature.get("no_execution_policy")
        if (
            len(vectors) != 1
            or vectors[0][0] is not None
            or vectors[0][1] != fact["decision_id"]
            or vectors[0][2] != "NO_EXECUTION"
            or not isinstance(no_execution, Mapping)
            or no_execution.get("policy") != "NO_EXECUTION"
            or no_execution.get("policy_sha256") != vectors[0][3]
        ):
            raise ContractError("durable_replay_no_execution_vector_drift")
        execution_policy = "NO_EXECUTION"

    reservations = conn.execute(
        "SELECT amount_usdc,state FROM basket_target_capital_reservations "
        "WHERE cycle_id=?",
        (fact["cycle_id"],),
    ).fetchall()
    if len(reservations) != 1:
        raise ContractError("durable_replay_capital_reservation_missing")
    reserve_amount = float(reservations[0][0])
    if reserve_amount < 0.0 or (not intents and reserve_amount != 0.0):
        raise ContractError("durable_replay_capital_reservation_drift")
    reason_codes = _json(
        "decision_reason_codes", decision["reason_codes_json"], mapping=False
    )
    if (
        int(decision["will_order"]) != 0
        or (
            not intents
            and (
                decision["decided_state"] != "blocked"
                or not reason_codes
                or execution_policy != "NO_EXECUTION"
            )
        )
    ):
        raise ContractError("durable_replay_no_live_decision_state_drift")
    core_direction = _core_direction_projection(feature, decision)

    row = {
        "rowid": int(fact["rowid"]),
        "fact_id": fact["fact_id"],
        "fact_sha256": fact["content_sha256"],
        "decision_at_utc": fact["decision_at_utc"],
        "source_sequence": int(fact["source_sequence"]),
        "stage": fact["stage"],
        "probability_sha256": envelope.content_sha256,
        "official_ws_sha256": ws["content_sha256"],
        "official_ws_economic_sha256": ws_semantic_sha256,
        "inventory_sha256": inventory["content_sha256"],
        "order_state_sha256": order_state["content_sha256"],
        "cycle_id": cycle["cycle_id"],
        "decision_id": decision["decision_id"],
        "decision_sha256": decision["decision_sha256"],
        # Historical append-only decisions may predate the current deadline
        # field's hash contract.  The fact-to-decision SHA binding is the
        # durable authority; current-contract recomputation is diagnostic and
        # must not reinterpret or rewrite those immutable rows.
        "decision_hash_matches_current_contract": (
            decision_hash_matches_current_contract
        ),
        "execution_policy": execution_policy,
        "decision_state": decision["decided_state"],
        "reason_codes": reason_codes,
        "intent_count": len(intents),
        "reserve_amount_usdc": reserve_amount,
        "core_direction": core_direction,
        "training_fact_present": True,
        "label_required": False,
    }
    return {
        **row,
        "row_sha256": content_sha256(
            {"schema": "bayes_basket_target_durable_replay_row_v1", **row}
        ),
    }


def replay_durable_target_chain(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str | None = None,
    through_rowid: int | None = None,
    max_facts: int = MAX_FACTS,
    initial_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay one station-day from real SSOT with a resumable continuation."""

    assert_process_memory_budget()
    if conn.row_factory is not sqlite3.Row:
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    _schema_gate(conn)
    station = _text("station_id", station_id).upper()
    day = _text("event_date", event_date)
    as_of = _utc("as_of_utc", as_of_utc) if as_of_utc else None
    if (
        isinstance(max_facts, bool)
        or not isinstance(max_facts, int)
        or not 1 <= max_facts <= MAX_FACTS
    ):
        raise ContractError("durable_replay_max_facts_invalid")
    if (
        through_rowid is not None
        and (
            isinstance(through_rowid, bool)
            or not isinstance(through_rowid, int)
            or through_rowid <= 0
        )
    ):
        raise ContractError("durable_replay_through_rowid_invalid")

    registry_rows = conn.execute(
        "SELECT settlement_timezone,snapshot_sha256,slug,outcome,"
        "rules_sha256,yes_token,no_token,refreshed_at "
        "FROM basket_target_market_registry "
        "WHERE station_id=? AND event_date=? AND source_eligible=1 "
        + ("AND refreshed_at<=? " if as_of is not None else "")
        + "ORDER BY snapshot_sha256,slug,outcome",
        (
            (station, day, _utc_text(as_of))
            if as_of is not None
            else (station, day)
        ),
    ).fetchall()
    timezones = {
        str(row[0]) for row in registry_rows if str(row[0] or "").strip()
    }
    if len(timezones) != 1:
        raise ContractError("durable_replay_registry_timezone_not_unique")
    settlement_timezone = next(iter(timezones))
    registry_set_sha256 = content_sha256({
        "schema": "bayes_basket_target_replay_registry_set_v1",
        "station_id": station,
        "event_date": day,
        "rows": [list(row) for row in registry_rows],
    })

    prior = dict(initial_state or {})
    if prior:
        if (
            prior.get("schema") != CONTINUATION_SCHEMA
            or prior.get("station_id") != station
            or prior.get("event_date") != day
            or prior.get("settlement_timezone") != settlement_timezone
            or (
                prior.get("as_of_utc") not in (None, "")
                and prior.get("as_of_utc")
                != (_utc_text(as_of) if as_of is not None else None)
            )
            or prior.get("registry_set_sha256") not in (
                None,
                registry_set_sha256,
            )
        ):
            raise ContractError("durable_replay_continuation_scope_mismatch")
    last_rowid = int(prior.get("last_rowid", 0))
    last_decision_at = (
        _utc("continuation_last_decision_at_utc", prior["last_decision_at_utc"])
        if prior.get("last_decision_at_utc")
        else None
    )
    last_sequence = int(prior.get("last_source_sequence", -1))
    last_stage_rank = int(prior.get("last_stage_rank", -1))
    last_core_buckets = prior.get("last_core_buckets")
    if last_core_buckets is not None:
        last_core_buckets = _string_list(
            "continuation_last_core_buckets", last_core_buckets
        )
    last_direction = prior.get("last_direction")
    if last_direction not in (None, "UP", "DOWN", "NEUTRAL"):
        raise ContractError("durable_replay_continuation_direction_invalid")
    explicit_core_seen = bool(prior.get("explicit_core_seen", False))
    recent_ws_transitions = [
        str(value)
        for value in prior.get("recent_ws_transition_sha256s", [])
    ]
    if len(recent_ws_transitions) > 2:
        raise ContractError("durable_replay_ws_transition_state_invalid")
    last_ws_semantic_sha256 = prior.get("last_ws_semantic_sha256")
    ws_transition_count = int(prior.get("ws_transition_count", 0))
    ws_return_transition_count = int(
        prior.get("ws_return_transition_count", 0)
    )
    ws_keepalive_duplicate_count = int(
        prior.get("ws_keepalive_duplicate_count", 0)
    )
    ws_semantic_chain_sha256 = str(
        prior.get("ws_semantic_chain_sha256")
        or content_sha256({
            "schema": "bayes_basket_target_ws_semantic_chain_seed_v1",
            "station_id": station,
            "event_date": day,
            "settlement_timezone": settlement_timezone,
        })
    )
    chain_sha = str(prior.get("chain_sha256") or content_sha256({
        "schema": "bayes_basket_target_durable_replay_chain_seed_v1",
        "station_id": station,
        "event_date": day,
        "settlement_timezone": settlement_timezone,
    }))

    fact_query = (
        "SELECT rowid," + ",".join(FACT_COLUMNS) + " "
        "FROM basket_policy_training_fact "
        "WHERE station_id=? AND event_date=? AND rowid>? "
        + ("AND created_at_utc<=? " if as_of is not None else "")
        + ("AND rowid<=? " if through_rowid is not None else "")
        + "ORDER BY rowid LIMIT ?"
    )
    fact_values: list[Any] = [station, day, last_rowid]
    if as_of is not None:
        fact_values.append(_utc_text(as_of))
    if through_rowid is not None:
        fact_values.append(through_rowid)
    fact_values.append(max_facts + 1)
    selected = conn.execute(
        fact_query,
        tuple(fact_values),
    ).fetchall()
    if len(selected) > max_facts:
        raise ContractError("durable_replay_fact_limit_exceeded")
    if not selected and not prior:
        raise ContractError("durable_replay_training_facts_missing")

    rows: list[dict[str, Any]] = []
    ws_economic_transitions: list[dict[str, Any]] = []
    for raw in selected:
        fact = {key: raw[key] for key in raw.keys()}
        row = _validate_event(
            conn,
            fact,
            station_id=station,
            event_date=day,
            settlement_timezone=settlement_timezone,
        )
        observed = _utc("decision_at_utc", row["decision_at_utc"])
        if last_decision_at is not None and observed < last_decision_at:
            raise ContractError("durable_replay_time_moved_backwards")
        if row["source_sequence"] <= last_sequence:
            raise ContractError("durable_replay_sequence_not_increasing")
        rank = STAGE_RANK[row["stage"]]
        if rank < last_stage_rank:
            raise ContractError("durable_replay_probability_stage_downgrade")
        core = row["core_direction"]
        if core["contract_state"] == "EXPLICIT_V1":
            if explicit_core_seen:
                if (
                    core["current_core_buckets"] != last_core_buckets
                    or core["current_direction"] != last_direction
                ):
                    raise ContractError(
                        "durable_replay_core_direction_predecessor_drift"
                    )
                if (
                    core["plan_state"] == "HOLD"
                    and (
                        core["core_buckets"] != last_core_buckets
                        or core["direction"] != last_direction
                    )
                ):
                    raise ContractError(
                        "durable_replay_hold_mutated_core_or_direction"
                    )
            elif (
                core["current_core_buckets"] not in (None, [])
                or core["current_direction"] is not None
            ):
                raise ContractError(
                    "durable_replay_initial_core_predecessor_not_empty"
                )
            explicit_core_seen = True
            last_core_buckets = list(core["core_buckets"])
            last_direction = core["direction"]
        elif explicit_core_seen:
            raise ContractError("durable_replay_core_contract_downgrade")

        semantic_sha = row["official_ws_economic_sha256"]
        semantic_changed = semantic_sha != last_ws_semantic_sha256
        semantic_return = False
        if semantic_changed:
            semantic_return = (
                len(recent_ws_transitions) >= 2
                and semantic_sha == recent_ws_transitions[-2]
                and semantic_sha != recent_ws_transitions[-1]
            )
            transition = {
                "rowid": row["rowid"],
                "from_sha256": last_ws_semantic_sha256,
                "to_sha256": semantic_sha,
                "is_low_high_low_return": semantic_return,
            }
            transition["content_sha256"] = content_sha256({
                "schema": "bayes_basket_target_ws_semantic_transition_v1",
                **transition,
            })
            ws_economic_transitions.append(transition)
            ws_transition_count += 1
            if semantic_return:
                ws_return_transition_count += 1
            ws_semantic_chain_sha256 = content_sha256({
                "schema": "bayes_basket_target_ws_semantic_chain_step_v1",
                "previous_chain_sha256": ws_semantic_chain_sha256,
                "transition_sha256": transition["content_sha256"],
            })
            recent_ws_transitions = (
                recent_ws_transitions + [semantic_sha]
            )[-2:]
            last_ws_semantic_sha256 = semantic_sha
        row["official_ws_economic_changed"] = semantic_changed
        row["official_ws_low_high_low_return"] = semantic_return
        if not semantic_changed:
            ws_keepalive_duplicate_count += 1
        row["row_sha256"] = content_sha256({
            "schema": "bayes_basket_target_durable_replay_row_v1",
            **{key: value for key, value in row.items() if key != "row_sha256"},
        })
        chain_sha = content_sha256({
            "schema": "bayes_basket_target_durable_replay_chain_step_v1",
            "previous_chain_sha256": chain_sha,
            "row_sha256": row["row_sha256"],
        })
        rows.append(row)
        last_rowid = row["rowid"]
        last_decision_at = observed
        last_sequence = row["source_sequence"]
        last_stage_rank = rank

    continuation = {
        "schema": CONTINUATION_SCHEMA,
        "station_id": station,
        "event_date": day,
        "settlement_timezone": settlement_timezone,
        "as_of_utc": _utc_text(as_of) if as_of is not None else None,
        "registry_set_sha256": registry_set_sha256,
        "last_rowid": last_rowid,
        "last_decision_at_utc": (
            last_decision_at.isoformat().replace("+00:00", "Z")
            if last_decision_at
            else None
        ),
        "last_source_sequence": last_sequence,
        "last_stage_rank": last_stage_rank,
        "explicit_core_seen": explicit_core_seen,
        "last_core_buckets": last_core_buckets,
        "last_direction": last_direction,
        "last_ws_semantic_sha256": last_ws_semantic_sha256,
        "recent_ws_transition_sha256s": recent_ws_transitions,
        "ws_transition_count": ws_transition_count,
        "ws_return_transition_count": ws_return_transition_count,
        "ws_keepalive_duplicate_count": ws_keepalive_duplicate_count,
        "ws_semantic_chain_sha256": ws_semantic_chain_sha256,
        "chain_sha256": chain_sha,
    }
    label_count = int(conn.execute(
        "SELECT COUNT(*) FROM basket_policy_training_label l "
        "JOIN basket_policy_training_fact f ON f.fact_id=l.fact_id "
        "WHERE f.station_id=? AND f.event_date=?",
        (station, day),
    ).fetchone()[0])
    core = {
        "schema": SCHEMA,
        "state": "PASS",
        "station_id": station,
        "event_date": day,
        "settlement_timezone": settlement_timezone,
        "as_of_utc": _utc_text(as_of) if as_of is not None else None,
        "registry_set_sha256": registry_set_sha256,
        "replayed_fact_count": len(rows),
        "rows": rows,
        "ws_economic_transitions": ws_economic_transitions,
        "ws_economic_summary": {
            "transition_count": ws_transition_count,
            "adjacent_keepalive_duplicate_count": (
                ws_keepalive_duplicate_count
            ),
            "low_high_low_return_count": ws_return_transition_count,
            "semantic_chain_sha256": ws_semantic_chain_sha256,
        },
        "continuation": continuation,
        "label_status": {
            "materialized_label_count": label_count,
            "required_for_production_no_live_replay": False,
        },
        "historical_oos": {
            "consulted": False,
            "future_data_counts_as_oos": False,
            "blocks_production_no_live": False,
            "blocks_formal_parameter_promotion_or_live": False,
            "historical_strict_oos_required_for_formal_parameter_promotion_or_live": True,
        },
        "boundary": {
            "read_only": True,
            "query_only": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
            "network_access": False,
        },
    }
    # Compatibility projection for older replay readers.  Forward data is
    # explicitly non-OOS and non-blocking; the canonical status is above.
    core["forward_oos"] = {
        "consulted": False,
        "future_data_counts_as_oos": False,
        "blocks_production_no_live": False,
        "blocks_formal_parameter_promotion_or_live": False,
    }
    return {**core, "content_sha256": content_sha256(core)}


__all__ = [
    "CONTINUATION_SCHEMA",
    "MAX_FACTS",
    "SCHEMA",
    "replay_durable_target_chain",
]
