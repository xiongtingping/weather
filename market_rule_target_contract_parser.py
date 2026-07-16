"""Pure rule-only extractor for an immutable raw market snapshot.

It intentionally reads only the event identity, settlement source and bucket
labels required to freeze a target contract.  Resolved outcomes, prices,
liquidity, orders, positions and P&L are outside its vocabulary and are never
looked up or emitted.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Mapping

from resolution_target_contract_runtime import EXTRACTION_SCHEMA_VERSION, EXTRACTOR_VERSION


SCHEMA_VERSION = "market_rule_target_contract_parser_v1"
_NUMBER = r"(-?\d+(?:\.\d+)?)"
_DEGREE_C = r"(?:°\s*C|C)"
_EXACT = re.compile(rf"^\s*{_NUMBER}\s*{_DEGREE_C}\s*$", re.IGNORECASE)
_BELOW = re.compile(rf"^\s*{_NUMBER}\s*{_DEGREE_C}\s+or\s+below\s*$", re.IGNORECASE)
_HIGHER = re.compile(rf"^\s*{_NUMBER}\s*{_DEGREE_C}\s+or\s+(?:higher|above|more)\s*$", re.IGNORECASE)
_RANGE = re.compile(rf"^\s*{_NUMBER}\s*(?:°\s*C|C)?\s*[-–]\s*{_NUMBER}\s*{_DEGREE_C}\s*$", re.IGNORECASE)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _event_candidates(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    root = _mapping(value)
    candidates: list[Mapping[str, Any]] = [root] if root else []
    for key in ("events", "data"):
        child = root.get(key)
        if isinstance(child, list):
            candidates.extend(item for item in child if isinstance(item, Mapping))
        elif isinstance(child, Mapping):
            candidates.append(child)
            events = child.get("events")
            if isinstance(events, list):
                candidates.extend(item for item in events if isinstance(item, Mapping))
    return candidates


def _parse_bucket_label(label: str) -> dict[str, float | None] | None:
    text = _text(label).replace("–", "-")
    match = _BELOW.match(text)
    if match:
        return {"lower_inclusive": None, "upper_exclusive": float(match.group(1)) + 0.5}
    match = _HIGHER.match(text)
    if match:
        return {"lower_inclusive": float(match.group(1)) - 0.5, "upper_exclusive": None}
    match = _RANGE.match(text)
    if match:
        first, second = sorted((float(match.group(1)), float(match.group(2))))
        return {"lower_inclusive": first - 0.5, "upper_exclusive": second + 0.5}
    match = _EXACT.match(text)
    if match:
        value = float(match.group(1))
        return {"lower_inclusive": value - 0.5, "upper_exclusive": value + 0.5}
    return None


def _station_from_resolution_source(value: str) -> str:
    parts = [item for item in _text(value).rstrip("/").split("/") if item]
    return parts[-1].upper() if parts else ""


def _raw_text_contains(raw_text: str, value: str) -> bool:
    text = _text(value)
    return bool(text) and (text in raw_text or json.dumps(text, ensure_ascii=True)[1:-1] in raw_text)


def _literal_json_string_fragment(raw_text: str, value: str) -> str:
    """Return a minimal literal raw string token, never the full object."""

    for token in (json.dumps(_text(value), ensure_ascii=False), json.dumps(_text(value), ensure_ascii=True)):
        if token in raw_text:
            return token
    return ""


def parse_market_rule_target_contract_extraction(
    raw_payload: bytes | str,
    *,
    public_slug: str,
    resolution_local_date: str,
) -> dict[str, Any]:
    """Extract one rule-only target contract surface from a raw JSON snapshot."""

    day = _text(resolution_local_date)[:10]
    try:
        date.fromisoformat(day)
    except ValueError:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_rule_parser_resolution_local_date_invalid"], "read_only": True}
    raw_text = raw_payload.decode("utf-8", errors="replace") if isinstance(raw_payload, bytes) else _text(raw_payload)
    try:
        decoded = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_rule_parser_raw_json_invalid"], "read_only": True}
    slug = _text(public_slug)
    candidates = [row for row in _event_candidates(decoded) if _text(row.get("slug")) == slug]
    if len(candidates) != 1:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["market_rule_parser_slug_not_unique_in_raw"],
            "read_only": True,
        }
    event = candidates[0]
    markets = [row for row in event.get("markets") or [] if isinstance(row, Mapping)]
    if not markets:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_rule_parser_bucket_markets_missing"], "read_only": True}
    sources = {_text(event.get("resolutionSource"))}
    sources.update(_text(row.get("resolutionSource")) for row in markets)
    sources.discard("")
    if len(sources) != 1:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_rule_parser_resolution_source_not_unique"], "read_only": True}
    resolution_source = next(iter(sources))
    buckets: dict[str, dict[str, float | None]] = {}
    for market in markets:
        label = _text(market.get("groupItemTitle"))
        bounds = _parse_bucket_label(label)
        if not label or bounds is None or label in buckets:
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_rule_parser_bucket_label_invalid_or_duplicate"], "read_only": True}
        buckets[label] = bounds
    # Keep only literal rule-field tokens as L0 evidence.  The complete raw
    # document stays in the raw archive; carrying it here would unnecessarily
    # copy outcome/price fields into a weather-adjacent contract.
    if (
        not _raw_text_contains(raw_text, slug)
        or not _raw_text_contains(raw_text, day)
        or not _raw_text_contains(raw_text, resolution_source)
        or any(not _raw_text_contains(raw_text, label) for label in buckets)
    ):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_rule_parser_raw_evidence_incomplete"], "read_only": True}
    evidence_fragments = [
        _literal_json_string_fragment(raw_text, slug),
        _literal_json_string_fragment(raw_text, day),
        _literal_json_string_fragment(raw_text, resolution_source),
        *(_literal_json_string_fragment(raw_text, label) for label in buckets),
    ]
    if not all(evidence_fragments):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_rule_parser_literal_evidence_fragment_missing"], "read_only": True}
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "rule_extraction_ready",
        "extraction": {
            "schema": EXTRACTION_SCHEMA_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "public_slug": slug,
            "resolution_local_date": day,
            "station_id": _station_from_resolution_source(resolution_source),
            "resolution_source": resolution_source,
            "temperature_unit": "C",
            "rounding_rule": "whole_degree_half_step_v1",
            "bucket_contract": buckets,
            "extraction_evidence_fragments": evidence_fragments,
            "boundary": {
                "does_not_read_market_outcome": True,
                "does_not_read_market_price": True,
                "does_not_read_positions_or_pnl": True,
            },
        },
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "parse_market_rule_target_contract_extraction"]
