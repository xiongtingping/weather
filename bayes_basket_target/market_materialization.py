"""Append-only materialization of official WS, inventory and order-state facts."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from typing import Any, Mapping

from .cash_phase import BasketCashPhaseInput
from .contracts import ContractError, content_sha256
from .et_history import assert_event_day_started
from .ssot import production_ssot_schema_status
from .resource_bounds import assert_connection_target_capacity
from .settlement_time import settlement_day_window


MARKET_MATERIALIZATION_SCHEMA = "bayes_basket_market_materialization_v1"


def _text(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{name}_missing")
    return text


def _timestamp(name: str, value: Any) -> str:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{name}_timezone_required")
    return text


def _number(name: str, value: Any, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(number) or (nonnegative and number < 0.0):
        raise ContractError(f"{name}_invalid")
    return number


def _sha256(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise ContractError(f"{name}_invalid_sha256")
    return result


def _record_sha(schema: str, row: Mapping[str, Any]) -> str:
    return content_sha256({
        "schema": schema,
        **{key: value for key, value in row.items() if key != "content_sha256"},
    })


def _normalize_buckets(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ContractError("official_ws_buckets_missing")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordinals: set[int] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ContractError("official_ws_bucket_invalid")
        bucket = _text("bucket", item.get("bucket"))
        ordinal = int(item.get("ordinal", -1))
        if ordinal < 0 or bucket in seen or ordinal in ordinals:
            raise ContractError("official_ws_bucket_identity_invalid")
        if "shard_generation" in item:
            try:
                generation = int(item["shard_generation"])
            except (TypeError, ValueError) as exc:
                raise ContractError("official_ws_shard_generation_invalid") from exc
            if generation < 1:
                raise ContractError("official_ws_shard_generation_invalid")
        asks_raw = item.get("ask_levels")
        if not isinstance(asks_raw, list):
            raise ContractError("official_ws_ask_depth_invalid")
        asks: list[dict[str, float]] = []
        previous_price = -1.0
        for level in asks_raw:
            if not isinstance(level, Mapping):
                raise ContractError("official_ws_ask_level_invalid")
            price = _number("ask_price", level.get("price"), nonnegative=True)
            shares = _number("ask_shares", level.get("shares"), nonnegative=False)
            if not 0.0 < price <= 1.0 or shares <= 0.0 or price <= previous_price:
                raise ContractError("official_ws_ask_levels_not_strictly_increasing")
            asks.append({"price": price, "shares": shares})
            previous_price = price
        # Pre-v1 materialized ask-only rows omitted ``bid_levels``.  Treat
        # absence as the same explicit empty side; never synthesize depth.
        bids_raw = item.get("bid_levels", [])
        if not isinstance(bids_raw, list):
            raise ContractError("official_ws_bid_depth_invalid")
        ask_available = bool(item.get("ask_available", bool(asks)))
        bid_available = bool(item.get("bid_available", bool(bids_raw)))
        if ask_available != bool(asks):
            raise ContractError("official_ws_ask_availability_mismatch")
        if bid_available != bool(bids_raw):
            raise ContractError("official_ws_bid_availability_mismatch")
        if not ask_available and not bid_available:
            raise ContractError("official_ws_book_empty")
        result.append({
            **dict(item),
            "bucket": bucket,
            "ordinal": ordinal,
            "ask_levels": asks,
            "ask_available": ask_available,
            "bid_available": bid_available,
        })
        seen.add(bucket)
        ordinals.add(ordinal)
    return result


def build_official_ws_snapshot(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and content-address one complete official WS snapshot."""

    if not isinstance(raw, Mapping):
        raise ContractError("official_ws_snapshot_mapping_required")
    station = _text("station_id", raw.get("station_id"))
    event_date = _text("event_date", raw.get("event_date"))
    settlement_timezone = _text(
        "settlement_timezone", raw.get("settlement_timezone")
    )
    settlement_day_window(event_date, settlement_timezone)
    cash_input = BasketCashPhaseInput.from_mapping(raw.get("cash_phase_input"))
    if cash_input.station_id != station or cash_input.event_date_local != event_date:
        raise ContractError("cash_phase_input_scope_mismatch")
    if cash_input.station_timezone != settlement_timezone:
        raise ContractError(
            "cash_phase_input_settlement_timezone_mismatch"
        )
    if raw.get("quote_source") != "OFFICIAL_CLOB_WS":
        raise ContractError("official_ws_source_required")
    row = {
        "snapshot_id": _text("snapshot_id", raw.get("snapshot_id")),
        "market_id": _text("market_id", raw.get("market_id")),
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "sequence": int(raw.get("sequence", -1)),
        "observed_at": _timestamp("observed_at", raw.get("observed_at")),
        "quote_version": _text("quote_version", raw.get("quote_version")),
        "quote_observed_at": _timestamp("quote_observed_at", raw.get("quote_observed_at")),
        "quote_expires_at": _timestamp(
            "quote_expires_at", raw.get("quote_expires_at")
        ),
        "quote_freshness_policy_sha256": _sha256(
            "quote_freshness_policy_sha256",
            raw.get("quote_freshness_policy_sha256"),
        ),
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": _text("inventory_version", raw.get("inventory_version")),
        "order_state_version": _text("order_state_version", raw.get("order_state_version")),
        "remaining_budget_usdc": _number("remaining_budget_usdc", raw.get("remaining_budget_usdc")),
        "buckets": _normalize_buckets(raw.get("buckets")),
        "cash_phase_input": cash_input.to_dict(),
        "state": str(raw.get("state") or "COMMITTED"),
        "created_at": _timestamp("created_at", raw.get("created_at") or raw.get("observed_at")),
    }
    if row["sequence"] < 0 or row["state"] not in {"COMMITTED", "QUARANTINED"}:
        raise ContractError("official_ws_snapshot_state_invalid")
    observed = datetime.fromisoformat(
        row["quote_observed_at"].replace("Z", "+00:00")
    )
    expires = datetime.fromisoformat(
        row["quote_expires_at"].replace("Z", "+00:00")
    )
    if expires <= observed:
        raise ContractError(
            "official_ws_quote_expiry_must_follow_observation"
        )
    assert_event_day_started(
        event_date,
        as_of_utc=row["observed_at"],
        target_timezone=settlement_timezone,
    )
    row["buckets_json"] = row.pop("buckets")
    row["cash_phase_input_json"] = row.pop("cash_phase_input")
    row["content_sha256"] = _record_sha("basket_official_ws_snapshot_v1", row)
    return row


def _build_state_snapshot(
    raw: Mapping[str, Any],
    *,
    kind: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ContractError(f"{kind}_snapshot_mapping_required")
    station = _text("station_id", raw.get("station_id"))
    event_date = _text("event_date", raw.get("event_date"))
    settlement_timezone = _text(
        "settlement_timezone", raw.get("settlement_timezone")
    )
    settlement_day_window(event_date, settlement_timezone)
    if kind == "inventory":
        snapshot_id_key, version_key, known_key, json_key = (
            "inventory_snapshot_id", "inventory_version", "complete", "inventory_json"
        )
        schema = "basket_confirmed_inventory_snapshot_v1"
    else:
        snapshot_id_key, version_key, known_key, json_key = (
            "order_state_snapshot_id", "order_state_version", "known", "order_state_json"
        )
        schema = "basket_order_state_snapshot_v1"
    data = raw.get(json_key)
    if not isinstance(data, Mapping):
        raise ContractError(f"{kind}_json_invalid")
    if kind == "inventory":
        values = data.get("filled_by_bucket")
    else:
        values = data.get("pending_by_bucket")
    if not isinstance(values, Mapping):
        raise ContractError(f"{kind}_bucket_state_missing")
    normalized_values = {str(bucket): _number(f"{kind}_shares", value) for bucket, value in values.items()}
    normalized_data = {**dict(data), "filled_by_bucket" if kind == "inventory" else "pending_by_bucket": normalized_values}
    row = {
        snapshot_id_key: _text(snapshot_id_key, raw.get(snapshot_id_key)),
        "station_id": station,
        "event_date": event_date,
        version_key: _text(version_key, raw.get(version_key)),
        "observed_at": _timestamp("observed_at", raw.get("observed_at")),
        known_key: int(raw.get(known_key, 0)),
        json_key: normalized_data,
        "state": str(raw.get("state") or "COMMITTED"),
        "created_at": _timestamp("created_at", raw.get("created_at") or raw.get("observed_at")),
    }
    if row[known_key] not in ({0, 1} if kind == "order_state" else {0, 1}) or row["state"] not in {"COMMITTED", "QUARANTINED"}:
        raise ContractError(f"{kind}_snapshot_state_invalid")
    if kind == "inventory" and row[known_key] != 1:
        raise ContractError("confirmed_inventory_incomplete")
    if kind == "order_state" and row[known_key] != 1:
        raise ContractError("order_state_unknown")
    assert_event_day_started(
        event_date,
        as_of_utc=row["observed_at"],
        target_timezone=settlement_timezone,
    )
    row["content_sha256"] = _record_sha(schema, row)
    return row


def build_confirmed_inventory_snapshot(raw: Mapping[str, Any]) -> dict[str, Any]:
    return _build_state_snapshot(raw, kind="inventory")


def build_order_state_snapshot(raw: Mapping[str, Any]) -> dict[str, Any]:
    return _build_state_snapshot(raw, kind="order_state")


def _insert_once(conn: sqlite3.Connection, table: str, key: str, row: Mapping[str, Any]) -> bool:
    existing = conn.execute(f"SELECT content_sha256 FROM {table} WHERE {key}=?", (row[key],)).fetchone()
    if existing is not None:
        if str(existing[0]) != str(row["content_sha256"]):
            raise ContractError(f"{table}_content_conflict")
        return False
    columns = tuple(row)
    values = []
    for column in columns:
        value = row[column]
        if column in {
            "buckets", "buckets_json", "cash_phase_input", "cash_phase_input_json",
            "inventory_json", "order_state_json",
        }:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        values.append(value)
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
        tuple(values),
    )
    return True


def insert_market_evidence_bundle(
    conn: sqlite3.Connection,
    quote: Mapping[str, Any],
    inventory: Mapping[str, Any],
    order_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one quote/inventory/order-state version vector atomically."""

    status = production_ssot_schema_status(conn)
    if not status["ok"]:
        raise ContractError("production_ssot_schema_incomplete")
    assert_connection_target_capacity(conn)
    quote_row = build_official_ws_snapshot(quote)
    inventory_row = build_confirmed_inventory_snapshot(inventory)
    order_row = build_order_state_snapshot(order_state)
    if quote_row["station_id"] != inventory_row["station_id"] or quote_row["station_id"] != order_row["station_id"]:
        raise ContractError("market_evidence_station_mismatch")
    if quote_row["event_date"] != inventory_row["event_date"] or quote_row["event_date"] != order_row["event_date"]:
        raise ContractError("market_evidence_event_date_mismatch")
    if quote_row["inventory_version"] != inventory_row["inventory_version"]:
        raise ContractError("market_evidence_inventory_version_mismatch")
    if quote_row["order_state_version"] != order_row["order_state_version"]:
        raise ContractError("market_evidence_order_state_version_mismatch")
    quote_buckets = {str(row["bucket"]) for row in quote_row["buckets_json"]}
    inventory_buckets = set(inventory_row["inventory_json"]["filled_by_bucket"])
    order_buckets = set(order_row["order_state_json"]["pending_by_bucket"])
    if inventory_buckets != quote_buckets or order_buckets != quote_buckets:
        raise ContractError("market_evidence_bucket_coverage_mismatch")
    for row in (inventory_row, order_row):
        if row["observed_at"] > quote_row["observed_at"]:
            raise ContractError("market_evidence_not_strict_asof")
    latest_row = conn.execute(
        "SELECT sequence,quote_observed_at,buckets_json FROM basket_official_ws_snapshot "
        "WHERE station_id=? AND event_date=? ORDER BY sequence DESC LIMIT 1",
        (quote_row["station_id"], quote_row["event_date"]),
    ).fetchone()
    if latest_row is not None and quote_row["sequence"] < int(latest_row[0]):
        raise ContractError("official_ws_sequence_rewind")
    if latest_row is not None:
        try:
            current_observed = datetime.fromisoformat(str(quote_row["quote_observed_at"]).replace("Z", "+00:00"))
            previous_observed = datetime.fromisoformat(str(latest_row[1]).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ContractError("official_ws_observed_at_invalid") from exc
        if current_observed.tzinfo is None or previous_observed.tzinfo is None:
            raise ContractError("official_ws_observed_at_timezone_required")
        if current_observed < previous_observed:
            raise ContractError("official_ws_quote_observed_at_rewind")
        try:
            previous_buckets = json.loads(str(latest_row[2]))
        except (TypeError, ValueError) as exc:
            raise ContractError("official_ws_previous_buckets_invalid") from exc
        if not isinstance(previous_buckets, list):
            raise ContractError("official_ws_previous_buckets_invalid")
        previous_generations = {
            str(item.get("token_id")): int(item["shard_generation"])
            for item in previous_buckets
            if isinstance(item, Mapping)
            and item.get("token_id") is not None
            and item.get("shard_generation") is not None
        }
        for item in quote_row["buckets_json"]:
            token_id = str(item.get("token_id") or "")
            generation = item.get("shard_generation")
            if token_id and generation is not None and token_id in previous_generations:
                if int(generation) < previous_generations[token_id]:
                    raise ContractError("official_ws_generation_rewind")
    wrote_quote = _insert_once(
        conn,
        "basket_official_ws_snapshot",
        "snapshot_id",
        quote_row,
    )
    wrote_inventory = _insert_once(
        conn,
        "basket_confirmed_inventory_snapshot",
        "inventory_snapshot_id",
        inventory_row,
    )
    wrote_order = _insert_once(
        conn,
        "basket_order_state_snapshot",
        "order_state_snapshot_id",
        order_row,
    )
    return {
        "schema": MARKET_MATERIALIZATION_SCHEMA,
        "state": "COMMITTED",
        "wrote_quote": wrote_quote,
        "wrote_inventory": wrote_inventory,
        "wrote_order_state": wrote_order,
        "quote_snapshot_id": quote_row["snapshot_id"],
        "inventory_snapshot_id": inventory_row["inventory_snapshot_id"],
        "order_state_snapshot_id": order_row["order_state_snapshot_id"],
        "places_orders": False,
        "calls_order_gateway": False,
    }


__all__ = [
    "MARKET_MATERIALIZATION_SCHEMA",
    "build_confirmed_inventory_snapshot",
    "build_official_ws_snapshot",
    "build_order_state_snapshot",
    "insert_market_evidence_bundle",
]
