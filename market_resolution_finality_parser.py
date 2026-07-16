"""Pure parser for Gamma's resolved-state evidence, without settlement outcome.

The parser observes only whether every market in a frozen target reports
``umaResolutionStatus=resolved`` and that the enclosing event is closed.  It
never reads outcome arrays, outcome prices, order books, positions or P&L.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping


SCHEMA_VERSION = "market_resolution_finality_parser_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _event_candidates(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    root = _mapping(value)
    values: list[Mapping[str, Any]] = [root] if root else []
    for key in ("events", "data"):
        child = root.get(key)
        if isinstance(child, list):
            values.extend(item for item in child if isinstance(item, Mapping))
        elif isinstance(child, Mapping):
            values.append(child)
    return values


def _parse_utc(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        return ""
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_market_resolution_finality(
    raw_payload: bytes | str,
    *,
    public_slug: str,
    resolution_local_date: str,
    response_completed_at: str,
) -> dict[str, Any]:
    """Return finality state only when the full frozen event is resolved."""

    observed_at = _parse_utc(response_completed_at)
    if not observed_at:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_finality_parser_response_completed_at_invalid"], "read_only": True}
    raw_text = raw_payload.decode("utf-8", errors="replace") if isinstance(raw_payload, bytes) else _text(raw_payload)
    try:
        decoded = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_finality_parser_raw_json_invalid"], "read_only": True}
    slug = _text(public_slug)
    candidates = [candidate for candidate in _event_candidates(decoded) if _text(candidate.get("slug")) == slug]
    if len(candidates) != 1:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_finality_parser_slug_not_unique_in_raw"], "read_only": True}
    event = candidates[0]
    markets = [candidate for candidate in event.get("markets") or [] if isinstance(candidate, Mapping)]
    if event.get("closed") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_finality_parser_event_not_closed"], "read_only": True}
    if not markets:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_finality_parser_markets_missing"], "read_only": True}
    statuses = {_text(market.get("umaResolutionStatus")).lower() for market in markets}
    if statuses != {"resolved"}:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["market_finality_parser_not_all_markets_resolved"],
            "read_only": True,
        }
    sources = {_text(event.get("resolutionSource"))}
    sources.update(_text(market.get("resolutionSource")) for market in markets)
    sources.discard("")
    if len(sources) != 1:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_finality_parser_resolution_source_not_unique"], "read_only": True}
    day = _text(resolution_local_date)[:10]
    if day not in raw_text:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_finality_parser_target_day_not_literal_in_raw"], "read_only": True}
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "market_resolution_final_observed",
        "public_slug": slug,
        "resolution_local_date": day,
        "resolution_source": next(iter(sources)),
        "market_status": "resolved",
        # This is the timestamp at which the immutable provider response was
        # seen in a fully-resolved state; it is not a guessed on-chain time.
        "resolution_state_observed_at": observed_at,
        "boundary": {
            "does_not_read_market_outcome": True,
            "does_not_read_market_price": True,
            "does_not_read_positions_or_pnl": True,
        },
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "parse_market_resolution_finality"]
