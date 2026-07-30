"""Strict, versioned bridge from physical maximum X to settlement S to bucket B.

The bridge never guesses a resolution rule from city names, current weather,
market prices, or bucket labels.  A missing/partial contract is blocked.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "resolution_bridge_v1"
FINAL_LABEL_SCHEMA_VERSION = "resolution_final_label_v1"
PHYSICAL_ONLY_STATE = "physical_only_market_projection_na"
EPSILON = 1e-12


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _first_present(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _is_sha256(value: Any) -> bool:
    text = _text(value).lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _blocked(*reasons: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted(set(reason for reason in reasons if reason)),
        "read_only": True,
        "market_data_consumed": False,
    }


def _physical_distribution(value: Any) -> tuple[list[tuple[float, float]], list[str]]:
    rows: list[tuple[float, float]] = []
    if isinstance(value, Mapping):
        iterator = ({"value_c": key, "probability": probability} for key, probability in value.items())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        iterator = value
    else:
        return [], ["physical_distribution_missing"]
    for item in iterator:
        raw = item if isinstance(item, Mapping) else {}
        physical = _finite(_first_present(raw, "value_c", "temperature_c", "x"))
        probability = _finite(_first_present(raw, "probability", "mass"))
        if physical is None or probability is None or probability < 0.0:
            return [], ["physical_distribution_row_invalid"]
        rows.append((physical, probability))
    total = sum(probability for _physical, probability in rows)
    if not rows or abs(total - 1.0) > 1e-8:
        return [], ["physical_distribution_not_normalized"]
    return rows, []


def _kernel_for_x(contract: Mapping[str, Any], physical_value: float) -> tuple[list[tuple[float, float]], str]:
    kernel = _mapping(contract.get("physical_to_settlement_kernel"))
    candidates = (f"{physical_value:g}", str(physical_value), "default")
    raw_rows: Any = None
    for key in candidates:
        if key in kernel:
            raw_rows = kernel[key]
            break
    if not isinstance(raw_rows, list):
        return [], "physical_to_settlement_kernel_missing_for_x"
    output: list[tuple[float, float]] = []
    for row in raw_rows:
        item = row if isinstance(row, Mapping) else {}
        settlement = _finite(_first_present(item, "settlement_value_c", "s"))
        probability = _finite(item.get("probability"))
        if settlement is None or probability is None or probability < 0.0:
            return [], "physical_to_settlement_kernel_row_invalid"
        output.append((settlement, probability))
    total = sum(probability for _settlement, probability in output)
    if not output or abs(total - 1.0) > 1e-8:
        return [], "physical_to_settlement_kernel_not_normalized"
    return output, ""


def _bucket_contract(contract: Mapping[str, Any]) -> tuple[list[tuple[str, float, float]], str]:
    raw = _mapping(contract.get("bucket_contract"))
    rows: list[tuple[str, float, float]] = []
    for label, bounds in raw.items():
        item = _mapping(bounds)
        raw_lower = item.get("lower_inclusive")
        raw_upper = item.get("upper_exclusive")
        lower = float("-inf") if raw_lower is None else _finite(raw_lower)
        upper = float("inf") if raw_upper is None else _finite(raw_upper)
        if not _text(label) or lower is None or upper is None or upper <= lower or (math.isinf(lower) and math.isinf(upper)):
            return [], "bucket_contract_row_invalid"
        rows.append((_text(label), lower, upper))
    if not rows:
        return [], "bucket_contract_missing"
    rows.sort(key=lambda row: (row[1], row[2], row[0]))
    for previous, current in zip(rows, rows[1:]):
        if current[1] < previous[2] - EPSILON:
            return [], "bucket_contract_overlap"
    return rows, ""


def _bucket_for_settlement(value: float, rows: Sequence[tuple[str, float, float]]) -> str:
    hits = [label for label, lower, upper in rows if value >= lower - EPSILON and value < upper - EPSILON]
    return hits[0] if len(hits) == 1 else ""


def build_physical_only_resolution_bridge(
    *,
    physical_high_distribution: Any,
    resolution_contract: Mapping[str, Any],
    market_bucket_na_reason: str = "x_to_s_to_b_bridge_unavailable",
) -> dict[str, Any]:
    """Bind an X-only posterior to its L0 contract without inventing B.

    An accumulating X/S bridge cannot prevent a physical daily-maximum
    posterior from existing.  This envelope deliberately does not inspect a
    candidate kernel or bucket bounds, because that would create an
    unvalidated market-bucket probability.
    """

    contract = _mapping(resolution_contract)
    contract_id = _text(contract.get("resolution_contract_id"))
    issues: list[str] = []
    if not contract_id:
        issues.append("resolution_contract_id_missing")
    if not _text(contract.get("resolution_source_id")):
        issues.append("resolution_source_id_missing")
    if not _text(contract.get("resolution_local_date")):
        issues.append("resolution_local_date_missing")
    if _text(contract.get("settlement_unit")) != "C":
        issues.append("settlement_unit_not_explicit_celsius")
    physical_rows, physical_errors = _physical_distribution(physical_high_distribution)
    issues.extend(physical_errors)
    if issues:
        return _blocked(*issues)
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": PHYSICAL_ONLY_STATE,
        "read_only": True,
        "market_data_consumed": False,
        "resolution_contract_id": contract_id,
        "resolution_source_id": _text(contract.get("resolution_source_id")),
        "resolution_local_date": _text(contract.get("resolution_local_date")),
        "physical_distribution_sha256": _sha256(physical_rows),
        "settlement_scalar_distribution": [],
        "market_bucket_probabilities": {},
        "market_bucket_probabilities_na_reason": _text(market_bucket_na_reason) or "x_to_s_to_b_bridge_unavailable",
        "settlement_exclusion_certificates": {},
        "bridge_closed": False,
        "contract_sha256": _sha256(contract),
    }


def build_resolution_bridge(
    *,
    physical_high_distribution: Any,
    resolution_contract: Mapping[str, Any],
    hard_exclusion_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Map p(X) through an explicit p(S|X) kernel and explicit bucket contract."""

    contract = _mapping(resolution_contract)
    contract_id = _text(contract.get("resolution_contract_id"))
    if not contract_id:
        return _blocked("resolution_contract_id_missing")
    if _text(contract.get("resolution_source_id")) == "":
        return _blocked("resolution_source_id_missing")
    if _text(contract.get("resolution_local_date")) == "":
        return _blocked("resolution_local_date_missing")
    if _text(contract.get("settlement_unit")) != "C":
        return _blocked("settlement_unit_not_explicit_celsius")
    forbidden = [key for key in contract if any(token in str(key).lower() for token in ("market_price", "orderbook", "position", "pnl", "edge"))]
    if forbidden:
        return _blocked("resolution_contract_contains_forbidden_market_or_trading_input")
    physical_rows, physical_errors = _physical_distribution(physical_high_distribution)
    if physical_errors:
        return _blocked(*physical_errors)
    bucket_rows, bucket_error = _bucket_contract(contract)
    if bucket_error:
        return _blocked(bucket_error)

    settlement_mass: dict[float, float] = {}
    for x_value, x_probability in physical_rows:
        kernel_rows, kernel_error = _kernel_for_x(contract, x_value)
        if kernel_error:
            return _blocked(kernel_error)
        for settlement_value, conditional_probability in kernel_rows:
            settlement_mass[settlement_value] = settlement_mass.get(settlement_value, 0.0) + x_probability * conditional_probability
    settlement_total = sum(settlement_mass.values())
    if abs(settlement_total - 1.0) > 1e-8:
        return _blocked("settlement_distribution_not_normalized")
    bucket_mass = {label: 0.0 for label, _lower, _upper in bucket_rows}
    uncovered: list[float] = []
    for settlement_value, probability in settlement_mass.items():
        label = _bucket_for_settlement(settlement_value, bucket_rows)
        if not label:
            uncovered.append(settlement_value)
            continue
        bucket_mass[label] += probability
    if uncovered:
        return _blocked("settlement_scalar_outside_bucket_contract")
    bucket_total = sum(bucket_mass.values())
    if abs(bucket_total - 1.0) > 1e-8:
        return _blocked("market_bucket_distribution_not_normalized")

    closed = bool(contract.get("bridge_closed") is True and contract.get("validated") is True)
    hard_context = _mapping(hard_exclusion_context)
    hard_certificates_authorized = bool(
        contract.get("hard_exclusion_certificates_authorized") is True
    )
    physical_distribution_sha = _sha256(physical_rows)
    settlement_support = sorted(
        value for value, probability in settlement_mass.items() if probability > EPSILON
    )
    bucket_bounds = {
        label: {"lower_inclusive": lower, "upper_exclusive": upper}
        for label, lower, upper in bucket_rows
    }
    hard_physical_support: list[float] = []
    hard_residual_support: list[float] = []
    for raw in hard_context.get("physical_hard_support_c") or []:
        parsed = _finite(raw)
        if parsed is not None:
            hard_physical_support.append(parsed)
    for raw in hard_context.get("hard_residual_support_c") or []:
        parsed = _finite(raw)
        if parsed is not None:
            hard_residual_support.append(parsed)
    hard_physical_support = sorted(set(hard_physical_support))
    hard_residual_support = sorted(set(hard_residual_support))
    physical_hard_support_sha = _sha256(hard_physical_support)
    hard_authority_sha = _text(
        hard_context.get("hard_residual_support_authority_sha256")
    )
    hard_context_valid = bool(
        hard_certificates_authorized
        and _text(hard_context.get("posterior_checkpoint_id"))
        and _text(hard_context.get("physical_distribution_sha256"))
        == physical_distribution_sha
        and hard_physical_support
        and hard_residual_support
        and _text(hard_context.get("physical_hard_support_sha256"))
        == physical_hard_support_sha
        and _is_sha256(hard_authority_sha)
        and hard_authority_sha
        == _text(contract.get("hard_residual_support_authority_sha256"))
    )
    hard_settlement_support = sorted(
        {x_value + residual for x_value in hard_physical_support for residual in hard_residual_support}
    ) if hard_context_valid else []
    certificates = {}
    for label, _probability in bucket_mass.items():
        bounds = bucket_bounds[label]
        lower = float("-inf") if bounds["lower_inclusive"] is None else float(bounds["lower_inclusive"])
        upper = float("inf") if bounds["upper_exclusive"] is None else float(bounds["upper_exclusive"])
        exact_full_support_exclusion = bool(
            closed
            and hard_context_valid
            and all(not (value >= lower and value < upper) for value in hard_settlement_support)
        )
        core = {
            "state": "valid" if exact_full_support_exclusion else "na",
            "reason": (
                "full_authority_hard_support_excludes_bucket"
                if exact_full_support_exclusion
                else "authority_hard_residual_envelope_not_approved"
                if closed and not hard_context_valid
                else "full_hard_support_intersects_bucket"
                if closed
                else "resolution_bridge_not_closed"
            ),
            "bucket": label,
            "resolution_contract_id": contract_id,
            "bridge_artifact_sha256": _text(contract.get("bridge_artifact_sha256")),
            "target_contract_sha256": _text(contract.get("target_contract_sha256")),
            "physical_distribution_sha256": physical_distribution_sha,
            "posterior_checkpoint_id": _text(hard_context.get("posterior_checkpoint_id")),
            "physical_hard_support_sha256": physical_hard_support_sha if hard_context_valid else "",
            "hard_residual_support_authority_sha256": hard_authority_sha if hard_context_valid else "",
            "physical_hard_support_c": hard_physical_support if hard_context_valid else [],
            "hard_residual_support_c": hard_residual_support if hard_context_valid else [],
            "bucket_bounds": bucket_bounds[label],
            "settlement_support_witness_c": hard_settlement_support if exact_full_support_exclusion else [],
            "full_support_exclusion_verified": exact_full_support_exclusion,
        }
        certificates[label] = {
            **core,
            "certificate_sha256": _sha256(core),
        }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "read_only": True,
        "market_data_consumed": False,
        "resolution_contract_id": contract_id,
        "resolution_source_id": _text(contract.get("resolution_source_id")),
        "resolution_local_date": _text(contract.get("resolution_local_date")),
        "physical_distribution_sha256": physical_distribution_sha,
        "settlement_scalar_distribution": [
            {"settlement_value_c": value, "probability": probability}
            for value, probability in sorted(settlement_mass.items())
        ],
        "market_bucket_probabilities": bucket_mass,
        "settlement_exclusion_certificates": certificates,
        "bridge_closed": closed,
        "hard_exclusion_certificates_authorized": hard_certificates_authorized,
        "contract_sha256": _sha256(contract),
    }


def _source_observation_ids(value: Any) -> tuple[list[str], list[str]]:
    """Normalize immutable source-observation identities without inventing one."""

    row = _mapping(value)
    raw_ids = row.get("source_observation_ids")
    if raw_ids is None:
        raw_ids = [row.get("source_observation_id")]
    if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, (str, bytes, bytearray)):
        return [], ["source_observation_ids_invalid"]
    ids = [_text(item) for item in raw_ids]
    if not ids or any(not item for item in ids):
        return [], ["source_observation_ids_missing"]
    if len(ids) != len(set(ids)):
        return [], ["source_observation_ids_duplicate"]
    return sorted(ids), []


def _final_label_lineage(value: Any, *, role: str) -> tuple[dict[str, Any], list[str]]:
    """Require raw-object and arrival lineage for one final-label surface."""

    row = _mapping(value)
    source_id = _text(row.get("source_id"))
    source_observation_ids, issues = _source_observation_ids(row)
    raw_sha256 = _text(row.get("raw_sha256"))
    raw_object_ref = _text(row.get("raw_object_ref"))
    observed_at = _text(row.get("observed_at"))
    finality = _text(row.get("finality"))
    if not source_id:
        issues.append(f"{role}_source_id_missing")
    if not raw_sha256:
        issues.append(f"{role}_raw_sha256_missing")
    elif not _is_sha256(raw_sha256):
        issues.append(f"{role}_raw_sha256_invalid")
    if not raw_object_ref:
        issues.append(f"{role}_raw_object_ref_missing")
    if _parse_time(observed_at) is None:
        issues.append(f"{role}_observed_at_missing_or_invalid")
    if finality != "final":
        issues.append(f"{role}_finality_not_final")
    return {
        "source_id": source_id,
        "source_observation_ids": source_observation_ids,
        "raw_sha256": raw_sha256,
        "raw_object_ref": raw_object_ref,
        "observed_at": observed_at,
        "finality": finality,
    }, sorted(set(issues))


def _blocked_final_label(*reasons: str) -> dict[str, Any]:
    return {
        "schema": FINAL_LABEL_SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted(set(reason for reason in reasons if reason)),
        "read_only": True,
        "market_data_consumed": False,
        "writes_archive_db": False,
        "places_orders": False,
    }


def build_final_resolution_label(
    *,
    target_identity: str,
    event_date: str,
    physical_high_label: Mapping[str, Any],
    settlement_label: Mapping[str, Any],
    resolution_contract: Mapping[str, Any],
    label_available_at: str,
    asserted_market_bucket: str | None = None,
) -> dict[str, Any]:
    """Build the immutable final X→S→B label consumed by settlement evaluation.

    This deliberately does *not* infer a settlement label from an old scalar,
    a city name, a market price, or an observed METAR.  The caller must provide
    distinct, raw-object-backed provenance for physical truth X and resolution
    truth S.  The function then proves that S is supported by the versioned
    ``p(S|X)`` kernel and derives B from the frozen bucket contract.
    """

    target = _text(target_identity)
    day = _text(event_date)[:10]
    physical = _mapping(physical_high_label)
    settlement = _mapping(settlement_label)
    contract = _mapping(resolution_contract)
    issues: list[str] = []
    if not target:
        issues.append("target_identity_missing")
    if not day:
        issues.append("event_date_missing")
    else:
        try:
            date.fromisoformat(day)
        except ValueError:
            issues.append("event_date_invalid")
    if _text(contract.get("resolution_local_date")) != day:
        issues.append("resolution_contract_local_date_mismatch")
    if contract.get("bridge_closed") is not True or contract.get("validated") is not True:
        issues.append("resolution_contract_not_closed_and_validated")

    physical_high_c = _finite(_first_present(physical, "final_physical_high_c", "physical_high_c"))
    settlement_scalar_c = _finite(_first_present(settlement, "settlement_scalar_c", "settlement_actual"))
    if physical_high_c is None:
        issues.append("final_physical_high_c_missing_or_non_finite")
    if settlement_scalar_c is None:
        issues.append("settlement_scalar_c_missing_or_non_finite")
    physical_lineage, physical_issues = _final_label_lineage(physical, role="physical_high")
    settlement_lineage, settlement_issues = _final_label_lineage(settlement, role="settlement")
    issues.extend(physical_issues)
    issues.extend(settlement_issues)
    if _text(contract.get("resolution_source_id")) != _text(settlement_lineage.get("source_id")):
        issues.append("settlement_source_id_does_not_match_resolution_contract")

    available_at = _text(label_available_at)
    available = _parse_time(available_at)
    if available is None:
        issues.append("label_available_at_missing_or_invalid")
    else:
        for lineage, role in ((physical_lineage, "physical_high"), (settlement_lineage, "settlement")):
            observed = _parse_time(lineage.get("observed_at"))
            if observed is not None and available < observed:
                issues.append(f"label_available_at_before_{role}_observed_at")
    if issues:
        return _blocked_final_label(*issues)

    bridge = build_resolution_bridge(
        physical_high_distribution=[{"value_c": physical_high_c, "probability": 1.0}],
        resolution_contract=contract,
    )
    if bridge.get("ok") is not True or _text(bridge.get("state")) != "available":
        return _blocked_final_label("resolution_bridge_blocked", *list(bridge.get("block_reasons") or []))
    support = [
        row
        for row in list(bridge.get("settlement_scalar_distribution") or [])
        if abs(float(row.get("settlement_value_c")) - float(settlement_scalar_c)) <= EPSILON
        and float(row.get("probability") or 0.0) > EPSILON
    ]
    if not support:
        return _blocked_final_label("settlement_scalar_not_supported_by_physical_to_settlement_kernel")
    bucket_rows, bucket_error = _bucket_contract(contract)
    if bucket_error:
        return _blocked_final_label(bucket_error)
    market_bucket = _bucket_for_settlement(float(settlement_scalar_c), bucket_rows)
    if not market_bucket:
        return _blocked_final_label("settlement_scalar_outside_bucket_contract")
    asserted_bucket = _text(asserted_market_bucket)
    if asserted_bucket and asserted_bucket != market_bucket:
        return _blocked_final_label("asserted_market_bucket_does_not_match_resolution_contract")

    core = {
        "schema": FINAL_LABEL_SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "target_identity": target,
        "event_date": day,
        "final_physical_high_c": physical_high_c,
        "settlement_scalar_c": settlement_scalar_c,
        "market_settlement_bucket": market_bucket,
        "resolution_contract_id": _text(contract.get("resolution_contract_id")),
        "resolution_contract_sha256": _text(bridge.get("contract_sha256")),
        "bridge_closed": True,
        "label_available_at": available_at,
        "label_finality": "final",
        "label_provenance": {
            "physical_high": physical_lineage,
            "settlement": settlement_lineage,
            "resolution_bridge_schema": _text(bridge.get("schema")),
        },
        "read_only": True,
        "market_data_consumed": False,
        "writes_archive_db": False,
        "places_orders": False,
    }
    return {**core, "resolution_label_sha256": _sha256(core)}


def validate_final_resolution_label(value: Any) -> dict[str, Any]:
    """Validate immutable shape/state/hash before a final label is trainable."""

    label = _mapping(value)
    issues: list[str] = []
    if _text(label.get("schema")) != FINAL_LABEL_SCHEMA_VERSION:
        issues.append("resolution_final_label_schema_invalid")
    if label.get("ok") is not True or _text(label.get("state")) != "available":
        issues.append("resolution_final_label_not_available")
    if label.get("bridge_closed") is not True:
        issues.append("resolution_final_label_bridge_not_closed")
    if _text(label.get("label_finality")) != "final":
        issues.append("resolution_final_label_not_final")
    for field in (
        "target_identity",
        "event_date",
        "resolution_contract_id",
        "resolution_contract_sha256",
        "market_settlement_bucket",
        "label_available_at",
    ):
        if not _text(label.get(field)):
            issues.append(f"resolution_final_label_{field}_missing")
    try:
        date.fromisoformat(_text(label.get("event_date")))
    except ValueError:
        issues.append("resolution_final_label_event_date_invalid")
    for field in ("final_physical_high_c", "settlement_scalar_c"):
        if _finite(label.get(field)) is None:
            issues.append(f"resolution_final_label_{field}_missing_or_non_finite")
    provenance = _mapping(label.get("label_provenance"))
    for role in ("physical_high", "settlement"):
        _lineage, lineage_issues = _final_label_lineage(provenance.get(role), role=role)
        issues.extend(lineage_issues)
    supplied_hash = _text(label.get("resolution_label_sha256"))
    unsigned = {key: child for key, child in label.items() if key != "resolution_label_sha256"}
    if not supplied_hash:
        issues.append("resolution_final_label_sha256_missing")
    elif _sha256(unsigned) != supplied_hash:
        issues.append("resolution_final_label_sha256_mismatch")
    return {
        "schema": FINAL_LABEL_SCHEMA_VERSION,
        "ok": not issues,
        "state": "valid" if not issues else "blocked",
        "block_reasons": sorted(set(issues)),
        "read_only": True,
    }


__all__ = [
    "PHYSICAL_ONLY_STATE",
    "build_physical_only_resolution_bridge",
    "FINAL_LABEL_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "build_final_resolution_label",
    "build_resolution_bridge",
    "validate_final_resolution_label",
]
