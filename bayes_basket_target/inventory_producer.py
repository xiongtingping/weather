"""Build the target-lane confirmed-inventory ingress from official positions.

This adapter is deliberately read-only.  It consumes only the complete local
position manifest published by the market WS lane and the production market
registry.  It never queries an exchange, reads legacy execution tables, or
writes the target SSOT.  A token-only manifest is rejected because token
presence cannot prove share quantity or cost.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .et_history import assert_event_day_started, event_day_window
from .market_registry import load_active_registry_rows, load_started_registry_rows
from .resource_bounds import assert_process_memory_budget, load_bounded_json


SCHEMA = "bayes_basket_confirmed_inventory_producer_v1"
SOURCE_SCHEMA = "reported_trajectory_held_yes_inventory_v1"
INGRESS_SCHEMA = "bayes_basket_confirmed_inventory_ingress_v1"
EPS = 1e-9
MAX_EXECUTION_TRANSITION_ROWS = 4096
_TRANSITION_STATES = frozenset({
    "POST_ACKED", "FOK_SUBMITTED", "CANCEL_REQUESTED",
    "CANCEL_CONFIRMED", "FULL_FILLED", "PARTIAL_FILLED",
    "RECONCILE_REQUIRED",
})
_RECONCILE_STATE_MARKERS = frozenset({
    "PARTIAL", "UNKNOWN", "LATE_FILL", "CANCEL_FILL_RACE", "FOK_FAILED",
})


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _number(name: str, value: Any, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(result) or (nonnegative and result < 0.0):
        raise ContractError(f"{name}_invalid")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def _timestamp_utc(name: str, value: Any) -> datetime:
    result = _timestamp(name, value)
    parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def _valid_sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid_sha256")
    return result


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = load_bounded_json(path)
    except Exception as exc:
        raise ContractError("official_position_manifest_invalid") from exc
    if not isinstance(value, Mapping):
        raise ContractError("official_position_manifest_mapping_required")
    result = dict(value)
    if result.get("schema") != SOURCE_SCHEMA:
        raise ContractError("official_position_manifest_schema_invalid")
    if result.get("source") != "official_polymarket_data_api_complete_positions":
        raise ContractError("official_position_manifest_source_invalid")
    if result.get("complete") is not True:
        raise ContractError("official_position_manifest_incomplete")
    _timestamp("observed_at", result.get("observed_at"))
    rows = result.get("position_rows")
    if not isinstance(rows, list):
        raise ContractError("official_position_details_missing")
    return result


def _position_rows(manifest: Mapping[str, Any]) -> dict[str, dict[str, float | int]]:
    """Aggregate official YES position rows by asset token."""

    result: dict[str, dict[str, float | int]] = {}
    for raw in manifest.get("position_rows") or []:
        if not isinstance(raw, Mapping):
            raise ContractError("official_position_row_invalid")
        token_id = _text("position_asset", raw.get("asset") or raw.get("token_id"))
        outcome = str(raw.get("outcome") or "YES").strip().upper()
        if outcome != "YES":
            raise ContractError("official_position_non_yes_row")
        size = _number("position_size", raw.get("size"))
        if size <= EPS:
            continue
        average = raw.get("avg_price", raw.get("avgPrice"))
        if average in (None, ""):
            raise ContractError("official_position_average_price_missing")
        average_price = _number("position_average_price", average)
        if average_price > 1.0:
            raise ContractError("official_position_average_price_invalid")
        current = raw.get("current_price", raw.get("curPrice"))
        current_price = None if current in (None, "") else _number("position_current_price", current)
        if current_price is not None and current_price > 1.0:
            raise ContractError("official_position_current_price_invalid")
        current_row = result.setdefault(token_id, {"size": 0.0, "cost_usdc": 0.0, "position_count": 0})
        current_row["size"] = float(current_row["size"]) + size
        current_row["cost_usdc"] = float(current_row["cost_usdc"]) + size * average_price
        current_row["position_count"] = int(current_row["position_count"]) + 1
    return result


def _registry_scopes(
    conn: sqlite3.Connection,
    *,
    station_id: str = "",
    event_date: str = "",
    include_listing_days: bool = False,
) -> dict[tuple[str, str], list[dict[str, str]]]:
    # Charter 48: a listed day's inventory is the opening inventory -- zero
    # everywhere -- and the snapshot the stage-open binds to has to carry
    # it.  Started-day callers keep the view they always had.
    rows = (
        load_active_registry_rows(conn, station_id=station_id, event_date=event_date)
        if include_listing_days
        else load_started_registry_rows(conn, station_id=station_id, event_date=event_date)
    )
    scopes: dict[tuple[str, str], list[dict[str, str]]] = {}
    seen_tokens: dict[str, tuple[str, str, str]] = {}
    for row in rows:
        scope = (_text("station_id", row["station_id"]), _text("event_date", row["event_date"]))
        bucket = _text("bucket", row["outcome"])
        token_id = _text("yes_token", row["yes_token"])
        previous = seen_tokens.get(token_id)
        identity = (*scope, bucket)
        if previous is not None and previous != identity:
            raise ContractError("official_position_registry_token_scope_conflict")
        seen_tokens[token_id] = identity
        scopes.setdefault(scope, []).append({
            "bucket": bucket,
            "yes_token": token_id,
            "settlement_timezone": _text(
                "settlement_timezone", row["settlement_timezone"]
            ),
        })
    if not scopes:
        raise ContractError("official_position_registry_scope_empty")
    return scopes


def _execution_transition_rows(
    conn: sqlite3.Connection,
    *,
    observed_at_utc: str | None = None,
    station_id: str = "",
    event_date: str = "",
) -> list[dict[str, Any]]:
    """Read bounded current transition versions and validate their lineage."""

    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='basket_target_execution_transition_versions'"
    ).fetchone()
    if table_exists is None:
        # A fresh isolated no-live rehearsal has no transition ledger yet;
        # that is the only schema-less path that is allowed to remain q=0.
        return []
    # Do not install or migrate the execution schema from a read projection.
    # The resident writer owns that schema; a missing table is the fresh q=0
    # case handled above, while an existing table is read exactly as-is.
    where: list[str] = []
    params: list[str] = []
    if station_id:
        where.append("station_id=?")
        params.append(station_id)
    if event_date:
        where.append("event_date=?")
        params.append(event_date)
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = conn.execute(
        "SELECT order_intent_id,station_id,event_date,settlement_timezone,bucket,token_id,"
        "transition_seq,state,confirmed_pending_size,confirmed_pending_notional_usdc,"
        "filled_size,filled_notional_usdc,inventory_version,order_state_version,"
        "content_sha256,evidence_json,transition_json,updated_at "
        "FROM basket_target_execution_transition_versions" + clause
        + " ORDER BY station_id,event_date,bucket,transition_seq,order_intent_id LIMIT ?",
        (*params, MAX_EXECUTION_TRANSITION_ROWS + 1),
    ).fetchall()
    if len(rows) > MAX_EXECUTION_TRANSITION_ROWS:
        raise ContractError("execution_transition_row_limit_exceeded")
    if not rows:
        return []
    assert_process_memory_budget(projected_bytes=min(len(rows) * 2048, 16 * 1024 * 1024))
    as_of = None if observed_at_utc is None else _timestamp_utc(
        "observed_at_utc", observed_at_utc
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        state = _text("execution_transition_state", row[7])
        if state not in _TRANSITION_STATES:
            if state in _RECONCILE_STATE_MARKERS or "PARTIAL" in state or "UNKNOWN" in state:
                raise ContractError("execution_transition_reconcile_required")
            raise ContractError("execution_transition_state_unknown")
        if state == "RECONCILE_REQUIRED":
            raise ContractError("execution_transition_reconcile_required")
        content_sha = _valid_sha("execution_transition_content_sha256", row[14])
        try:
            evidence = json.loads(str(row[15] or "{}"))
            transition = json.loads(str(row[16] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("execution_transition_lineage_json_invalid") from exc
        if not isinstance(evidence, Mapping) or not isinstance(transition, Mapping):
            raise ContractError("execution_transition_lineage_mapping_required")
        if content_sha256({
            "schema": "basket_target_execution_transition_version_v1",
            "transition": transition,
            "evidence": evidence,
        }) != content_sha:
            raise ContractError("execution_transition_content_hash_mismatch")
        updated_at = _timestamp("execution_transition_updated_at", row[17])
        if as_of is not None and _timestamp_utc(
            "execution_transition_updated_at", updated_at
        ) > as_of:
            raise ContractError("execution_transition_after_quote_asof")
        pending_size = _number("execution_transition_pending_size", row[8])
        pending_notional = _number("execution_transition_pending_notional", row[9])
        filled_size = _number("execution_transition_filled_size", row[10])
        filled_notional = _number("execution_transition_filled_notional", row[11])
        if state in {"FULL_FILLED", "PARTIAL_FILLED"} and filled_size <= EPS:
            raise ContractError("execution_transition_terminal_fill_size_invalid")
        if state in {"FULL_FILLED", "PARTIAL_FILLED"} and pending_size > EPS:
            raise ContractError(
                "execution_transition_terminal_fill_pending_conflict"
            )
        result.append({
            "order_intent_id": _text("execution_transition_order_intent_id", row[0]),
            "station_id": _text("execution_transition_station_id", row[1]),
            "event_date": _text("execution_transition_event_date", row[2]),
            "settlement_timezone": _text("execution_transition_settlement_timezone", row[3]),
            "bucket": _text("execution_transition_bucket", row[4]),
            "token_id": _text("execution_transition_token_id", row[5]),
            "transition_seq": int(row[6]),
            "state": state,
            "confirmed_pending_size": pending_size,
            "confirmed_pending_notional_usdc": pending_notional,
            "filled_size": filled_size,
            "filled_notional_usdc": filled_notional,
            "inventory_version": _text("execution_transition_inventory_version", row[12]),
            "order_state_version": _text("execution_transition_order_state_version", row[13]),
            "content_sha256": content_sha,
            "updated_at": updated_at,
        })
    return result


_LEDGER_FILL_STATES = ("resolved_filled", "resolved_partial")
MAX_LEDGER_FILL_ROWS = 20000


def _ledger_fill_rows(
    conn: sqlite3.Connection,
    *,
    observed_at_utc: str | None = None,
    station_id: str = "",
    event_date: str = "",
) -> list[dict[str, Any]]:
    """Authoritative fills the reconciliation ledger holds for live orders.

    The transition runtime above is the no-live simulator's fill source and
    never ran on a live order: on 2026-09-05 the first five real fills sat in
    ``basket_target_order_reconciliations`` as ``resolved_filled`` while
    every inventory snapshot for their scopes read zero, so the stages that
    top up a chain would have priced the day as if nothing were held.  A
    reconciliation the exchange settled is the live book's inventory fact;
    it is read here beside the transitions, by the same scope, with the same
    as-of rule, and a BUY adds what a SELL removes.
    """

    for table in (
        "basket_target_order_reconciliations",
        "basket_target_order_intents",
        "basket_target_execution_cycles",
    ):
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is None:
            return []
    # Charter 180: ``+r.state`` keeps the planner off the reconciliation
    # state index, which read every filled reconciliation the book has ever
    # had (282 on 2026-09-21) and joined each to its cycle before the scope
    # could filter -- once per scope per quote refresh, 713 calls and 21.8s
    # in the 10:39Z cycle.  Given a station the read now starts from the
    # cycle's (station_id, event_date) index; same rows, same order (65
    # scopes, median 1.54 -> 0.40ms).  Without one that index cannot seek,
    # and the state filter stays the narrowing it was.
    where = [
        "+r.state IN (?,?)" if station_id else "r.state IN (?,?)",
        "r.filled_size>?",
    ]
    params: list[Any] = [*_LEDGER_FILL_STATES, EPS]
    if station_id:
        where.append("c.station_id=?")
        params.append(station_id)
    if event_date:
        where.append("c.event_date=?")
        params.append(event_date)
    rows = conn.execute(
        "SELECT i.order_intent_id,c.station_id,c.event_date,"
        "c.settlement_timezone,i.bucket,i.token_id,i.order_action,r.state,"
        "r.filled_size,r.filled_notional_usdc,r.exchange_order_id,r.updated_at"
        " FROM basket_target_order_reconciliations r"
        " JOIN basket_target_order_intents i"
        " ON i.order_intent_id=r.order_intent_id"
        " JOIN basket_target_execution_cycles c ON c.cycle_id=i.cycle_id"
        " WHERE " + " AND ".join(where)
        + " ORDER BY c.station_id,c.event_date,i.bucket,r.updated_at,"
        "i.order_intent_id LIMIT ?",
        (*params, MAX_LEDGER_FILL_ROWS + 1),
    ).fetchall()
    if len(rows) > MAX_LEDGER_FILL_ROWS:
        raise ContractError("ledger_fill_row_limit_exceeded")
    as_of = None if observed_at_utc is None else _timestamp_utc(
        "observed_at_utc", observed_at_utc
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        updated_at = _timestamp("ledger_fill_updated_at", row[11])
        if as_of is not None and _timestamp_utc(
            "ledger_fill_updated_at", updated_at
        ) > as_of:
            # Same rule as a transition: evidence newer than the quote is
            # not rewound into it, the next quote will carry it.
            raise ContractError("ledger_fill_after_quote_asof")
        action = str(row[6] or "").strip().upper()
        if action not in {"BUY", "SELL"}:
            raise ContractError("ledger_fill_order_action_invalid")
        entry = {
            "order_intent_id": _text("ledger_fill_order_intent_id", row[0]),
            "station_id": _text("ledger_fill_station_id", row[1]),
            "event_date": _text("ledger_fill_event_date", row[2]),
            "settlement_timezone": _text(
                "ledger_fill_settlement_timezone", row[3]
            ),
            "bucket": _text("ledger_fill_bucket", row[4]),
            "token_id": _text("ledger_fill_token_id", row[5]),
            "order_action": action,
            "state": _text("ledger_fill_state", row[7]),
            "filled_size": _number("ledger_fill_size", row[8]),
            "filled_notional_usdc": _number("ledger_fill_notional", row[9]),
            "exchange_order_id": str(row[10] or ""),
            "updated_at": updated_at,
        }
        entry["content_sha256"] = content_sha256({
            "schema": "bayes_basket_ledger_fill_v1",
            **entry,
        })
        result.append(entry)
    return result


def ledger_holdings_by_token(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    intent_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    """What the reconciliation ledger says one scope holds, per token.

    Charter 83 (2026-09-05) made a settled live fill inventory, and the
    resident ingress above folds it into a started day's snapshot.  A
    forward day has no resident ingress: the P0 preselection writes its own
    evidence trio, and it wrote every bucket as held-zero.  So the first
    partial baskets (2026-09-05 10:12Z: LTAC, LFPB, SAEZ, ZGSZ -- one or two
    legs filled, the rest refused by the venue) were re-evaluated as if
    nothing were held, and the kernel judged a fresh three-leg basket at
    the moved prices (HOLD) instead of the legs the basket was missing.

    The same ledger read as ``_ledger_fill_rows``, aggregated per token:
    a BUY adds what a SELL removes.  No as-of clock -- the holdings are
    what the ledger has settled at read time -- and the caller keys the
    evidence identity on them, so the same book held differently is a
    different evidence row.  Fails closed on a token that has sold more
    shares than it bought.

    ``intent_ids`` narrows the read to one strategy's own orders.  The
    ledger is the account's -- every lane's fills on the scope land in it --
    so a caller that answers for one strategy (the basket's forward-day
    preselection, charter 144's sell-back of the basket's mis-bought legs)
    passes that strategy's intents (user ruling 2026-09-24: 「生产是多个策略
    混合，要区分开来」).  None keeps the account-wide reading.
    """

    fills = _ledger_fill_rows(
        conn,
        station_id=_text("ledger_holdings_station_id", station_id),
        event_date=_text("ledger_holdings_event_date", event_date),
    )
    if intent_ids is not None:
        fills = [fill for fill in fills if fill["order_intent_id"] in intent_ids]
    totals: dict[str, dict[str, Any]] = {}
    for fill in fills:
        entry = totals.setdefault(
            fill["token_id"],
            {"shares": 0.0, "cost_usdc": 0.0, "fill_count": 0},
        )
        sign = 1.0 if fill["order_action"] == "BUY" else -1.0
        entry["shares"] += sign * float(fill["filled_size"])
        entry["cost_usdc"] += sign * float(fill["filled_notional_usdc"])
        entry["fill_count"] += 1
    holdings: dict[str, dict[str, Any]] = {}
    for token_id, entry in sorted(totals.items()):
        # Shares cannot go negative -- that is a ledger contradiction.  Cost
        # can: a token sold for more than it was bought has a negative
        # basis, and what remains held is carried at zero.
        if entry["shares"] < -EPS:
            raise ContractError("ledger_holdings_negative:" + token_id)
        if entry["shares"] <= EPS:
            continue
        holdings[token_id] = {
            "shares": float(entry["shares"]),
            "cost_usdc": max(0.0, float(entry["cost_usdc"])),
            "fill_count": int(entry["fill_count"]),
        }
    return {
        "schema": "bayes_basket_ledger_holdings_v1",
        "station_id": station_id,
        "event_date": event_date,
        "holdings": holdings,
        "fill_content_sha256": sorted(
            fill["content_sha256"] for fill in fills
        ),
    }


def has_execution_transition_versions(
    conn: sqlite3.Connection,
    *,
    station_id: str = "",
    event_date: str = "",
) -> bool:
    """Validate and detect transition state for the selected target scope."""

    return bool(_execution_transition_rows(
        conn, station_id=station_id, event_date=event_date
    ))


def build_confirmed_inventory_ingress(
    conn: sqlite3.Connection,
    *,
    manifest_path: Path,
    remaining_budget_usdc: float,
    station_id: str = "",
    event_date: str = "",
    include_listing_days: bool = False,
) -> dict[str, Any]:
    """Return a strict, non-writing inventory ingress packet."""

    manifest = _read_manifest(Path(manifest_path))
    observed_at = _timestamp("observed_at", manifest.get("observed_at"))
    budget = _number("remaining_budget_usdc", remaining_budget_usdc)
    positions = _position_rows(manifest)
    scopes = _registry_scopes(
        conn,
        station_id=station_id,
        event_date=event_date,
        include_listing_days=include_listing_days,
    )
    source_sha = content_sha256(manifest)
    version = "inv_" + content_sha256({
        "schema": SCHEMA,
        "source_sha256": source_sha,
        "observed_at": observed_at,
        "remaining_budget_usdc": budget,
    })[:40]
    markets: list[dict[str, Any]] = []
    for (scope_station, scope_date), buckets in sorted(scopes.items()):
        settlement_timezones = {
            item["settlement_timezone"] for item in buckets
        }
        if len(settlement_timezones) != 1:
            raise ContractError("inventory_registry_settlement_timezone_not_unique")
        if include_listing_days:
            # Resolved, not asserted: a listed day is the opening state.
            event_day_window(
                scope_date,
                as_of_utc=observed_at,
                target_timezone=next(iter(settlement_timezones)),
            )
        else:
            assert_event_day_started(
                scope_date,
                as_of_utc=observed_at,
                target_timezone=next(iter(settlement_timezones)),
            )
        filled_by_bucket: dict[str, float] = {}
        filled_cost_by_bucket: dict[str, float] = {}
        token_ids_by_bucket: dict[str, str] = {}
        position_counts_by_bucket: dict[str, int] = {}
        for item in buckets:
            bucket = item["bucket"]
            token_id = item["yes_token"]
            position = positions.get(token_id, {"size": 0.0, "cost_usdc": 0.0, "position_count": 0})
            filled_by_bucket[bucket] = float(position["size"])
            filled_cost_by_bucket[bucket] = float(position["cost_usdc"])
            token_ids_by_bucket[bucket] = token_id
            position_counts_by_bucket[bucket] = int(position["position_count"])
        snapshot_id = "inv_snapshot_" + content_sha256({
            "inventory_version": version,
            "station_id": scope_station,
            "event_date": scope_date,
        })[:40]
        markets.append({
            "station_id": scope_station,
            "event_date": scope_date,
            "settlement_timezone": next(iter(settlement_timezones)),
            "inventory_snapshot_id": snapshot_id,
            "inventory_version": version,
            "observed_at": observed_at,
            "complete": 1,
            "remaining_budget_usdc": budget,
            "inventory_json": {
                "filled_by_bucket": filled_by_bucket,
                "filled_cost_by_bucket": filled_cost_by_bucket,
                "token_ids_by_bucket": token_ids_by_bucket,
                "position_counts_by_bucket": position_counts_by_bucket,
                "source": "official_polymarket_data_api_complete_positions",
                "source_manifest_sha256": source_sha,
            },
        })
    return {
        "schema": INGRESS_SCHEMA,
        "producer_schema": SCHEMA,
        "source_schema": SOURCE_SCHEMA,
        "source_manifest_sha256": source_sha,
        "observed_at": observed_at,
        "complete": True,
        "markets": markets,
        "places_orders": False,
        "calls_order_gateway": False,
    }


def build_no_live_inventory_ingress(
    conn: sqlite3.Connection,
    *,
    remaining_budget_usdc: float,
    observed_at_utc: str,
    station_id: str = "",
    event_date: str = "",
    include_listing_days: bool = False,
) -> dict[str, Any]:
    """Build no-live inventory from authoritative terminal fill transitions.

    q=0 is valid only when the selected target scope has no execution
    transition versions.  Once a transition exists, every current FULL_FILLED
    or terminal PARTIAL_FILLED version is accumulated by station/event/bucket
    and its content hash is retained in lineage. Unknown, race, or partial
    states with remaining pending quantity remain fail closed.
    """

    observed = _timestamp("observed_at_utc", observed_at_utc)
    budget = _number("remaining_budget_usdc", remaining_budget_usdc)
    scopes = _registry_scopes(
        conn,
        station_id=station_id,
        event_date=event_date,
        include_listing_days=include_listing_days,
    )
    transitions = _execution_transition_rows(
        conn,
        observed_at_utc=observed,
        station_id=station_id,
        event_date=event_date,
    )
    by_scope: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for transition in transitions:
        by_scope.setdefault(
            (transition["station_id"], transition["event_date"]), []
        ).append(transition)
    # Live fills the reconciliation ledger settled.  An intent the transition
    # runtime already carries is not counted twice.
    transition_intents = {
        transition["order_intent_id"] for transition in transitions
    }
    ledger_fills = [
        fill for fill in _ledger_fill_rows(
            conn,
            observed_at_utc=observed,
            station_id=station_id,
            event_date=event_date,
        )
        if fill["order_intent_id"] not in transition_intents
    ]
    fills_by_scope: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for fill in ledger_fills:
        fills_by_scope.setdefault(
            (fill["station_id"], fill["event_date"]), []
        ).append(fill)
    confirmed_cost = sum(
        transition["filled_notional_usdc"]
        for transition in transitions
        if transition["state"] in {"FULL_FILLED", "PARTIAL_FILLED"}
    ) + sum(
        (fill["filled_notional_usdc"] if fill["order_action"] == "BUY"
         else -fill["filled_notional_usdc"])
        for fill in ledger_fills
    )
    if not math.isfinite(confirmed_cost) or confirmed_cost > budget + EPS:
        raise ContractError("execution_transition_remaining_budget_negative")
    remaining_budget = max(0.0, budget - max(0.0, confirmed_cost))
    transition_hashes = sorted(
        transition["content_sha256"] for transition in transitions
    )
    ledger_fill_hashes = sorted(fill["content_sha256"] for fill in ledger_fills)
    version = (
        "invrec_ingress_" if (transitions or ledger_fills) else "inv_no_live_"
    ) + content_sha256({
        "schema": "bayes_basket_no_live_inventory_v1",
        "observed_at_utc": observed,
        "remaining_budget_usdc": budget,
        "confirmed_cost_usdc": confirmed_cost,
        "scopes": sorted(f"{station}|{day}" for station, day in scopes),
        "transition_content_sha256": transition_hashes,
        **(
            {"ledger_fill_content_sha256": ledger_fill_hashes}
            if ledger_fill_hashes else {}
        ),
    })[:40]
    markets: list[dict[str, Any]] = []
    for (scope_station, scope_date), buckets in sorted(scopes.items()):
        settlement_timezones = {
            item["settlement_timezone"] for item in buckets
        }
        if len(settlement_timezones) != 1:
            raise ContractError("inventory_registry_settlement_timezone_not_unique")
        settlement_timezone = next(iter(settlement_timezones))
        if include_listing_days:
            event_day_window(
                scope_date, as_of_utc=observed, target_timezone=settlement_timezone
            )
        else:
            assert_event_day_started(
                scope_date, as_of_utc=observed, target_timezone=settlement_timezone
            )
        bucket_names = [str(item["bucket"]) for item in buckets]
        registry_by_bucket = {str(item["bucket"]): item for item in buckets}
        scope_transitions = by_scope.get((scope_station, scope_date), [])
        for transition in scope_transitions:
            registry_row = registry_by_bucket.get(transition["bucket"])
            if registry_row is None or registry_row["yes_token"] != transition["token_id"]:
                raise ContractError("execution_transition_bucket_token_scope_conflict")
            if registry_row["settlement_timezone"] != transition["settlement_timezone"]:
                raise ContractError("execution_transition_settlement_timezone_mismatch")
        scope_fills = fills_by_scope.get((scope_station, scope_date), [])
        for fill in scope_fills:
            registry_row = registry_by_bucket.get(fill["bucket"])
            if registry_row is None or registry_row["yes_token"] != fill["token_id"]:
                raise ContractError("ledger_fill_bucket_token_scope_conflict")
            if registry_row["settlement_timezone"] != fill["settlement_timezone"]:
                raise ContractError("ledger_fill_settlement_timezone_mismatch")
        filled_by_bucket = {bucket: 0.0 for bucket in bucket_names}
        filled_cost_by_bucket = {bucket: 0.0 for bucket in bucket_names}
        position_counts_by_bucket = {bucket: 0 for bucket in bucket_names}
        lineage: list[dict[str, Any]] = []
        for transition in scope_transitions:
            if transition["state"] not in {
                "FULL_FILLED",
                "PARTIAL_FILLED",
            }:
                continue
            bucket = transition["bucket"]
            filled_by_bucket[bucket] += transition["filled_size"]
            filled_cost_by_bucket[bucket] += transition["filled_notional_usdc"]
            position_counts_by_bucket[bucket] += 1
            lineage.append({
                "source": "target_execution_transition_runtime",
                "order_intent_id": transition["order_intent_id"],
                "transition_seq": transition["transition_seq"],
                "state": transition["state"],
                "filled_size": transition["filled_size"],
                "filled_notional_usdc": transition["filled_notional_usdc"],
                "transition_content_sha256": transition["content_sha256"],
                "inventory_version": transition["inventory_version"],
                "order_state_version": transition["order_state_version"],
                "updated_at": transition["updated_at"],
            })
        for fill in scope_fills:
            bucket = fill["bucket"]
            sign = 1.0 if fill["order_action"] == "BUY" else -1.0
            filled_by_bucket[bucket] += sign * fill["filled_size"]
            filled_cost_by_bucket[bucket] += sign * fill["filled_notional_usdc"]
            position_counts_by_bucket[bucket] += 1
            lineage.append({
                "source": "target_reconciliation_ledger",
                "order_intent_id": fill["order_intent_id"],
                "order_action": fill["order_action"],
                "state": fill["state"],
                "filled_size": fill["filled_size"],
                "filled_notional_usdc": fill["filled_notional_usdc"],
                "exchange_order_id": fill["exchange_order_id"],
                "ledger_fill_content_sha256": fill["content_sha256"],
                "updated_at": fill["updated_at"],
            })
        for bucket in bucket_names:
            if filled_by_bucket[bucket] < -EPS:
                raise ContractError("ledger_fill_negative_inventory")
            filled_by_bucket[bucket] = max(0.0, filled_by_bucket[bucket])
            filled_cost_by_bucket[bucket] = max(0.0, filled_cost_by_bucket[bucket])
        snapshot_id = "inv_snapshot_" + content_sha256({
            "inventory_version": version,
            "station_id": scope_station,
            "event_date": scope_date,
        })[:40]
        markets.append({
            "station_id": scope_station,
            "event_date": scope_date,
            "settlement_timezone": settlement_timezone,
            "inventory_snapshot_id": snapshot_id,
            "inventory_version": version,
            "observed_at": observed,
            "complete": 1,
            "remaining_budget_usdc": remaining_budget,
            "inventory_json": {
                "filled_by_bucket": filled_by_bucket,
                "filled_cost_by_bucket": filled_cost_by_bucket,
                "token_ids_by_bucket": (
                    {bucket: str(item["yes_token"]) for bucket, item in registry_by_bucket.items()}
                    if (scope_transitions or scope_fills) else {}
                ),
                "position_counts_by_bucket": position_counts_by_bucket,
                "source": (
                    "target_execution_transition_runtime_and_ledger_fills"
                    if (scope_transitions and scope_fills)
                    else "target_execution_transition_runtime_no_live"
                    if scope_transitions
                    else "target_reconciliation_ledger_fills"
                    if scope_fills
                    else "target_lane_no_live_q0"
                ),
                "exchange_position_read": False,
                "production_no_live": True,
                "confirmed_fill_cost_usdc": sum(filled_cost_by_bucket.values()),
                "transition_content_sha256": sorted(
                    transition["content_sha256"] for transition in scope_transitions
                ),
                **(
                    {
                        "ledger_fill_content_sha256": sorted(
                            fill["content_sha256"] for fill in scope_fills
                        ),
                    }
                    if scope_fills else {}
                ),
                "lineage": lineage,
            },
        })
    return {
        "schema": INGRESS_SCHEMA,
        "producer_schema": (
            "bayes_basket_execution_transition_inventory_v1"
            if (transitions or ledger_fills)
            else "bayes_basket_no_live_inventory_v1"
        ),
        "source_schema": (
            "target_execution_transition_runtime_no_live"
            if transitions
            else "target_reconciliation_ledger_fills"
            if ledger_fills
            else "target_lane_no_live_q0"
        ),
        "observed_at": observed,
        "complete": True,
        "confirmed_fill_cost_usdc": confirmed_cost,
        "transition_content_sha256": transition_hashes,
        "ledger_fill_content_sha256": ledger_fill_hashes,
        "markets": markets,
        "places_orders": False,
        "calls_order_gateway": False,
    }


__all__ = [
    "INGRESS_SCHEMA",
    "SCHEMA",
    "build_confirmed_inventory_ingress",
    "has_execution_transition_versions",
    "build_no_live_inventory_ingress",
    "ledger_holdings_by_token",
]
