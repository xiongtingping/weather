"""Build a raw-bound, shadow-only L0 market-resolution target contract.

This is the missing boundary between a market's immutable rule snapshot and
the later X→S→B label bridge.  It deliberately accepts only rule semantics:
identity, date, source, unit, rounding and bucket boundaries.  It never
accepts or derives a resolved outcome, price, liquidity, position or P&L.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Any, Mapping

from resolution_authority_contract_runtime import validate_resolution_authority_contract
from source_observation_runtime import raw_payload_sha256


SCHEMA_VERSION = "market_resolution_target_contract_v1"
EXTRACTION_SCHEMA_VERSION = "market_rule_target_contract_extraction_v1"
EXTRACTOR_VERSION = "market_rule_target_contract_parser_v1"
FORBIDDEN_SEMANTIC_TOKENS = frozenset(
    {
        "outcome",
        "outcomes",
        "outcomeprice",
        "outcomeprices",
        "price",
        "prices",
        "mid",
        "spread",
        "liquidity",
        "volume",
        "position",
        "positions",
        "pnl",
        "profit",
        "loss",
        "trade",
        "trades",
        "order",
        "orders",
        "token",
        "tokens",
        "wallet",
        "balance",
    }
)


def _fragment_contains_forbidden_market_semantics(value: str) -> bool:
    lowered = _text(value).lower()
    return any(token in lowered for token in ("outcome", "price", "liquidity", "volume", "position", "pnl", "profit", "loss", "order", "trade", "token", "wallet", "balance"))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _normalized_key(value: Any) -> str:
    return "".join(character for character in _text(value).lower() if character.isalnum())


def _forbidden_semantic_paths(value: Any, *, path: str = "") -> list[str]:
    """Return paths whose keys would leak market/execution semantics.

    Raw provider bytes may contain those fields because raw evidence must stay
    complete.  The *parsed rule extraction* must not expose or consume them.
    """

    paths: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            current = f"{path}.{key}" if path else str(key)
            if _normalized_key(key) in FORBIDDEN_SEMANTIC_TOKENS:
                paths.append(current)
            paths.extend(_forbidden_semantic_paths(child, path=current))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            paths.extend(_forbidden_semantic_paths(child, path=f"{path}[{index}]"))
    return paths


def _validate_bucket_contract(value: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    raw = _mapping(value)
    issues: list[str] = []
    rows: list[tuple[str, float, float, dict[str, Any]]] = []
    normalized: dict[str, dict[str, Any]] = {}
    if not raw:
        return {}, ["target_contract_bucket_contract_missing"]
    for label, child in raw.items():
        name = _text(label)
        row = _mapping(child)
        raw_lower = row.get("lower_inclusive")
        raw_upper = row.get("upper_exclusive")
        lower = float("-inf") if raw_lower is None else _finite(raw_lower)
        upper = float("inf") if raw_upper is None else _finite(raw_upper)
        if not name or lower is None or upper is None or lower >= upper or (math.isinf(lower) and math.isinf(upper)):
            issues.append("target_contract_bucket_row_invalid")
            continue
        normalized[name] = {
            "lower_inclusive": None if math.isinf(lower) and lower < 0 else lower,
            "upper_exclusive": None if math.isinf(upper) and upper > 0 else upper,
        }
        rows.append((name, lower, upper, normalized[name]))
    if not rows:
        issues.append("target_contract_bucket_contract_empty")
        return {}, sorted(set(issues))
    ordered = sorted(rows, key=lambda row: (row[1], row[2], row[0]))
    for previous, current in zip(ordered, ordered[1:]):
        if current[1] < previous[2]:
            issues.append("target_contract_bucket_contract_overlap")
    return {name: row for name, _lower, _upper, row in ordered}, sorted(set(issues))


def _raw_text_contains(raw_text: str, value: str) -> bool:
    text = _text(value)
    return bool(text) and (text in raw_text or json.dumps(text, ensure_ascii=True)[1:-1] in raw_text)


def _evidence_fragments_bound(raw_text: str, evidence: Any, *required_fragments: str) -> tuple[bool, list[str]]:
    if not isinstance(evidence, (list, tuple)) or not evidence:
        return False, []
    candidates = [_text(fragment) for fragment in evidence]
    if not all(candidates) or not all(fragment in raw_text for fragment in candidates):
        return False, candidates
    joined = "\n".join(candidates)
    if not all(_raw_text_contains(joined, _text(fragment)) for fragment in required_fragments if _text(fragment)):
        return False, candidates
    return True, candidates


def _evidence_contains(raw_text: str, evidence: Any, *required_fragments: str) -> bool:
    bound, _fragments = _evidence_fragments_bound(raw_text, evidence, *required_fragments)
    return bound


def _evidence_fragments(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_text(fragment) for fragment in value if _text(fragment)]


def build_market_resolution_target_contract_proposal(
    *,
    authority_contract: Mapping[str, Any],
    target_identity: str,
    public_slug: str,
    resolution_local_date: str,
    station_id: str,
    resolution_source: str,
    market_rule_raw_payload: bytes | str,
    market_rule_raw_ref: str,
    market_rule_raw_sha256: str = "",
    market_rule_extraction: Mapping[str, Any],
    supersedes_target_contract_sha256: str = "",
) -> dict[str, Any]:
    """Build one immutable rule-bound target contract proposal without writes.

    ``market_rule_extraction`` is a parser result over the supplied raw rule
    object.  It can carry only target-rule semantics and must retain one
    literal source evidence fragment containing the target identity fields.
    """

    authority = validate_resolution_authority_contract(authority_contract)
    extraction = _mapping(market_rule_extraction)
    raw = market_rule_raw_payload if isinstance(market_rule_raw_payload, (bytes, bytearray, str)) else b""
    raw_text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else _text(raw)
    raw_sha256 = raw_payload_sha256(raw) if raw else ""
    declared_raw_sha256 = _text(market_rule_raw_sha256).lower()
    day = _text(resolution_local_date)[:10]
    blockers: list[str] = []

    if authority.get("ok") is not True:
        blockers.append("target_contract_authority_contract_invalid")
    try:
        date.fromisoformat(day)
    except ValueError:
        blockers.append("target_contract_resolution_local_date_invalid")
    if not _text(target_identity) or not _text(public_slug):
        blockers.append("target_contract_target_identity_or_slug_missing")
    if _text(station_id).upper() != "ZUUU":
        blockers.append("target_contract_station_invalid")
    expected_resolution_source = _text(_mapping(authority_contract.get("settlement_s")).get("source_url_base"))
    if _text(resolution_source).rstrip("/") != expected_resolution_source.rstrip("/"):
        blockers.append("target_contract_resolution_source_not_authority_bound")
    if not _text(market_rule_raw_ref) or not raw:
        blockers.append("target_contract_market_rule_raw_missing")
    if declared_raw_sha256 and declared_raw_sha256 != raw_sha256:
        blockers.append("target_contract_market_rule_raw_sha256_binding_mismatch")
    if _text(extraction.get("schema")) != EXTRACTION_SCHEMA_VERSION:
        blockers.append("target_contract_extraction_schema_invalid")
    if _text(extraction.get("extractor_version")) != EXTRACTOR_VERSION:
        blockers.append("target_contract_extractor_version_invalid")
    if _text(extraction.get("public_slug")) != _text(public_slug):
        blockers.append("target_contract_extraction_slug_mismatch")
    if _text(extraction.get("resolution_local_date"))[:10] != day:
        blockers.append("target_contract_extraction_local_date_mismatch")
    if _text(extraction.get("station_id")).upper() != "ZUUU":
        blockers.append("target_contract_extraction_station_mismatch")
    if _text(extraction.get("resolution_source")).rstrip("/") != expected_resolution_source.rstrip("/"):
        blockers.append("target_contract_extraction_resolution_source_mismatch")
    if _text(extraction.get("temperature_unit")).upper() != "C":
        blockers.append("target_contract_extraction_temperature_unit_invalid")
    if not _text(extraction.get("rounding_rule")):
        blockers.append("target_contract_extraction_rounding_rule_missing")
    forbidden_paths = _forbidden_semantic_paths(extraction)
    if forbidden_paths:
        blockers.append("target_contract_extraction_contains_forbidden_market_semantics")
    buckets, bucket_issues = _validate_bucket_contract(extraction.get("bucket_contract"))
    blockers.extend(bucket_issues)
    evidence = _evidence_fragments(extraction.get("extraction_evidence_fragments"))
    if not _evidence_contains(raw_text, evidence, public_slug, day, expected_resolution_source):
        blockers.append("target_contract_extraction_evidence_not_raw_bound")
    if any(_fragment_contains_forbidden_market_semantics(fragment) for fragment in evidence):
        blockers.append("target_contract_extraction_evidence_contains_forbidden_market_semantics")
    for label in buckets:
        if not _raw_text_contains("\n".join(evidence), label):
            blockers.append("target_contract_bucket_label_not_in_extraction_evidence")
            break
    supersedes = _text(supersedes_target_contract_sha256).lower()
    if supersedes and (len(supersedes) != 64 or any(char not in "0123456789abcdef" for char in supersedes)):
        blockers.append("target_contract_supersedes_sha256_invalid")

    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(blockers)),
            "read_only": True,
            "writes_archive_db": False,
            "places_orders": False,
        }

    core = {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "proposal_ready",
        "target_identity": _text(target_identity),
        "public_slug": _text(public_slug),
        "resolution_local_date": day,
        "target_station_id": "ZUUU",
        "resolution_source_id": _text(_mapping(authority_contract.get("settlement_s")).get("source_id")),
        "resolution_source": expected_resolution_source,
        "temperature_unit": "C",
        "rounding_rule": _text(extraction.get("rounding_rule")),
        "bucket_contract": buckets,
        "authority_contract_id": _text(authority.get("contract_id")),
        "authority_contract_sha256": _text(authority.get("contract_sha256")),
        "market_rule_snapshot": {
            "raw_ref": _text(market_rule_raw_ref),
            "raw_sha256": raw_sha256,
            "extractor_version": EXTRACTOR_VERSION,
            "extraction_evidence_fragments": evidence,
            "extraction_sha256": _sha256(extraction),
        },
        "bridge_closed": False,
        "validated": False,
        "supersedes_target_contract_sha256": supersedes or None,
        "boundary": {
            "does_not_read_market_outcome": True,
            "does_not_read_market_price": True,
            "does_not_read_positions_or_pnl": True,
            "does_not_close_x_s_b_bridge": True,
            "does_not_write_archive_db": True,
            "does_not_place_orders": True,
        },
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }
    contract_sha256 = _sha256(core)
    return {
        **core,
        "target_contract_sha256": contract_sha256,
        "target_contract_id": f"market_resolution_target:{contract_sha256[:24]}",
    }


def validate_market_resolution_target_contract_proposal(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate immutable proposal identity before isolated persistence.

    This deliberately validates only the retained proposal.  The raw evidence
    itself was verified by the builder and remains content-addressed outside
    this registry; the writer must nevertheless reject an in-memory mutation
    of the proposal or its deterministic identity.
    """

    row = dict(_mapping(value))
    issues: list[str] = []
    if _text(row.get("schema")) != SCHEMA_VERSION or row.get("ok") is not True or _text(row.get("state")) != "proposal_ready":
        issues.append("target_contract_proposal_invalid")
    supplied_sha = _text(row.get("target_contract_sha256")).lower()
    unsigned = {key: child for key, child in row.items() if key not in {"target_contract_sha256", "target_contract_id"}}
    expected_sha = _sha256(unsigned) if unsigned else ""
    if not supplied_sha or supplied_sha != expected_sha:
        issues.append("target_contract_sha256_mismatch")
    if _text(row.get("target_contract_id")) != f"market_resolution_target:{expected_sha[:24]}":
        issues.append("target_contract_id_mismatch")
    if row.get("bridge_closed") is not False or row.get("validated") is not False:
        issues.append("target_contract_must_remain_unvalidated_data_accumulation")
    if not _mapping(row.get("bucket_contract")):
        issues.append("target_contract_bucket_contract_missing")
    snapshot = _mapping(row.get("market_rule_snapshot"))
    for field in ("raw_ref", "raw_sha256", "extractor_version", "extraction_sha256"):
        if not _text(snapshot.get(field)):
            issues.append(f"target_contract_market_rule_{field}_missing")
    fragments = snapshot.get("extraction_evidence_fragments")
    if not isinstance(fragments, list) or not fragments or not all(_text(fragment) for fragment in fragments):
        issues.append("target_contract_market_rule_extraction_evidence_fragments_invalid")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not issues,
        "state": "valid" if not issues else "blocked",
        "block_reasons": sorted(set(issues)),
        "read_only": True,
    }


__all__ = [
    "EXTRACTION_SCHEMA_VERSION",
    "EXTRACTOR_VERSION",
    "SCHEMA_VERSION",
    "build_market_resolution_target_contract_proposal",
    "validate_market_resolution_target_contract_proposal",
]
