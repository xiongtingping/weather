"""Read-only registry classification for retained historical YES positions.

Balances are retained independently of a market's current trading state.  A
non-zero YES token may require an official CLOB book only while its exact
official registry row is still source-eligible, active, and accepting orders.
Closed or non-accepting positions remain explicitly visible but are never
subscribed or treated as executable.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any


MAX_HELD_YES_TOKENS = 64
REGISTRY_TABLE = "reported_trajectory_market_registry"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _token_ids(values: Sequence[str]) -> list[str]:
    tokens = sorted({_text(value) for value in values if _text(value)})
    if len(tokens) > MAX_HELD_YES_TOKENS:
        raise ValueError("held_yes_registry_scope_exceeded")
    return tokens


def classify_held_yes_tokens(
    conn: sqlite3.Connection,
    *,
    token_ids: Sequence[str],
    official_source_lane: str = "awc",
) -> list[dict[str, Any]]:
    """Classify every non-zero YES token without changing inventory or orders.

    ``ACTIVE_ACCEPTING`` is the only classification that requires an official
    WebSocket book.  All other unambiguous registry states are retained as
    explicit non-tradeable holdings.  A missing or duplicate registry mapping
    is unresolved and must remain visible to acceptance as a fail-closed
    classification error; it never widens the WebSocket subscription.
    """

    tokens = _token_ids(token_ids)
    if not tokens:
        return []
    source_lane = _text(official_source_lane).lower()
    if not source_lane:
        raise ValueError("held_yes_registry_source_lane_required")
    placeholders = ",".join("?" for _ in tokens)
    try:
        rows = conn.execute(
            "SELECT yes_token,station_id,event_date,slug,outcome,source_lane,"
            "source_eligible,market_active,market_accepting_orders,refreshed_at "
            f"FROM {REGISTRY_TABLE} WHERE yes_token IN ({placeholders})",
            tuple(tokens),
        ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError("held_yes_registry_read_failed") from exc

    by_token: dict[str, list[Mapping[str, Any]]] = {token: [] for token in tokens}
    for row in rows:
        mapping = dict(row) if isinstance(row, sqlite3.Row) else {
            "yes_token": row[0],
            "station_id": row[1],
            "event_date": row[2],
            "slug": row[3],
            "outcome": row[4],
            "source_lane": row[5],
            "source_eligible": row[6],
            "market_active": row[7],
            "market_accepting_orders": row[8],
            "refreshed_at": row[9],
        }
        token = _text(mapping.get("yes_token"))
        if token in by_token:
            by_token[token].append(mapping)

    result: list[dict[str, Any]] = []
    for token in tokens:
        matches = by_token[token]
        if not matches:
            result.append({
                "token_id": token,
                "classification": "REGISTRY_UNRESOLVED",
                "registry_resolved": False,
                "quote_required": False,
                "tradable": False,
                "non_tradeable_reasons": ["official_registry_yes_token_missing"],
                "registry": None,
            })
            continue
        if len(matches) != 1:
            result.append({
                "token_id": token,
                "classification": "REGISTRY_AMBIGUOUS",
                "registry_resolved": False,
                "quote_required": False,
                "tradable": False,
                "non_tradeable_reasons": ["official_registry_yes_token_ambiguous"],
                "registry": None,
            })
            continue
        row = matches[0]
        registry = {
            key: (
                bool(row.get(key))
                if key in {"source_eligible", "market_active", "market_accepting_orders"}
                else _text(row.get(key))
            )
            for key in (
                "station_id", "event_date", "slug", "outcome", "source_lane",
                "source_eligible", "market_active", "market_accepting_orders",
                "refreshed_at",
            )
        }
        reasons: list[str] = []
        if registry["source_lane"].lower() != source_lane:
            reasons.append("registry_source_lane_not_official")
        if registry["source_eligible"] is not True:
            reasons.append("registry_source_ineligible")
        if registry["market_active"] is not True:
            reasons.append("registry_market_inactive")
        if registry["market_accepting_orders"] is not True:
            reasons.append("registry_market_not_accepting_orders")
        active_accepting = not reasons
        result.append({
            "token_id": token,
            "classification": (
                "ACTIVE_ACCEPTING" if active_accepting
                else "REGISTRY_NONTRADABLE"
            ),
            "registry_resolved": True,
            "quote_required": active_accepting,
            "tradable": active_accepting,
            "non_tradeable_reasons": reasons,
            "registry": registry,
        })
    return result


def quote_required_token_ids(records: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        _text(record.get("token_id"))
        for record in records
        if record.get("quote_required") is True and _text(record.get("token_id"))
    }


def registry_classification_complete(records: Sequence[Mapping[str, Any]]) -> bool:
    return all(record.get("registry_resolved") is True for record in records)

