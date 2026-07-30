"""M3-12 probability-version promotion contract.

This module is an offline, no-authority compiler.  It creates a review
candidate for a new probability artifact and deliberately has no concept of
activation, a runtime pointer, a database, or an order gateway.

The contract is intentionally stricter than a scorecard.  A candidate is
bound to all four maturity windows (3/7/15/30 days), four-season evidence,
the exact station x lead x run-cycle x settlement scope, the M3-11 replay
equivalence receipt, strict as-of OOS rows, and one content-addressed parent
artifact.  Missing evidence is represented as ``BLOCKED_EVIDENCE`` inside the
review candidate; it is never silently upgraded to an authority state.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ContractError, canonical_json, content_sha256
from .resource_bounds import (
    MAX_JSON_OUTPUT_BYTES,
    MAX_TARGET_PROCESS_RSS_BYTES,
    assert_process_memory_budget,
    bounded_json_bytes,
)


SCHEMA = "bayes_basket_target_probability_promotion_candidate_v1"
CANDIDATE_ARTIFACT_SCHEMA = "bayes_basket_target_probability_candidate_artifact_v1"
M3_11_SCHEMA = "bayes_basket_target_probability_xsb_bridge_v1"
CANDIDATE_STATE = "CANDIDATE_NO_LIVE_REVIEW"
BLOCKED_EVIDENCE = "BLOCKED_EVIDENCE"
EVIDENCE_COMPLETE = "EVIDENCE_COMPLETE"
WINDOW_DAYS = (3, 7, 15, 30)
MIN_CROSS_SEASONS = 4
DEFAULT_MIN_SAMPLE_N = 4
DEFAULT_MIN_EFFECTIVE_SAMPLE_N = 2.0
DEFAULT_MAX_PARAMETER_DRIFT = 0.25
MAX_SCOPE_KEYS = 4096
MAX_OOS_ROWS = 20_000
MAX_PARAMETERS = 10_000
MAX_BLOCKERS = 256
METRICS = ("brier_score", "nll", "crps")
SCOPE_FIELDS = (
    "station_id",
    "lead_bucket",
    "run_cycle",
    "settlement_definition",
    "settlement_timezone",
)


def _mapping(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_mapping_required")
    return value


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid")
    return result


def _number(name: str, value: Any, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ContractError(f"{name}_invalid")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _leq(left: str, right: str) -> bool:
    return _dt(left) <= _dt(right)


def _lt(left: str, right: str) -> bool:
    return _dt(left) < _dt(right)


def _bounded(value: Any, *, limit: int = MAX_JSON_OUTPUT_BYTES, name: str = "candidate") -> None:
    """Apply a byte bound without writing anything."""

    try:
        bounded_json_bytes(value, limit=limit)
    except Exception as exc:  # resource bound errors are contract failures here
        raise ContractError(f"{name}_resource_bound_exceeded") from exc


def _unique_texts(name: str, value: Any) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ContractError(f"{name}_list_required")
    result = sorted({_text(f"{name}_item", item) for item in value})
    if not result:
        raise ContractError(f"{name}_empty")
    return result


def _single_or_list(raw: Mapping[str, Any], plural: str, singular: str) -> list[str]:
    value = raw.get(plural)
    if value is None:
        value = raw.get(singular)
        if value is None:
            return []
        value = [value]
    return _unique_texts(plural, value)


def _scope_key(raw: Mapping[str, Any], *, timezone_default: str | None = None) -> dict[str, str]:
    value = _mapping("promotion_scope_key", raw)
    result: dict[str, str] = {}
    for field in SCOPE_FIELDS:
        item = value.get(field)
        if field == "settlement_timezone" and item in (None, "") and timezone_default:
            item = timezone_default
        result[field] = _text(f"scope_{field}", item)
    return result


def _scope_key_tuple(value: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(value[field]) for field in SCOPE_FIELDS)


def normalize_scope(scope: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Normalize either explicit scope keys or a bounded Cartesian scope."""

    if isinstance(scope, Sequence) and not isinstance(scope, (str, bytes, bytearray)):
        raw = {"scope_keys": scope}
    else:
        raw = dict(_mapping("promotion_scope", scope))
    _bounded(raw, name="promotion_scope")
    keys_raw = raw.get("scope_keys")
    keys: list[dict[str, str]] = []
    if keys_raw is not None:
        if not isinstance(keys_raw, Sequence) or isinstance(keys_raw, (str, bytes, bytearray)):
            raise ContractError("promotion_scope_keys_list_required")
        for item in keys_raw:
            keys.append(_scope_key(_mapping("promotion_scope_key", item)))
    else:
        stations = _single_or_list(raw, "station_ids", "station_id")
        leads = _single_or_list(raw, "lead_buckets", "lead_bucket")
        cycles = _single_or_list(raw, "run_cycles", "run_cycle")
        definitions = _single_or_list(raw, "settlement_definitions", "settlement_definition")
        timezones_raw = raw.get("settlement_timezones")
        if timezones_raw is None:
            timezone = raw.get("settlement_timezone")
            timezones = [_text("settlement_timezone", timezone)] if timezone else []
        elif isinstance(timezones_raw, Mapping):
            # A definition->timezone map keeps the market registry binding
            # explicit while remaining compact.
            timezones = [_text("settlement_timezone", item) for item in timezones_raw.values()]
        else:
            timezones = _unique_texts("settlement_timezones", timezones_raw)
        if not all((stations, leads, cycles, definitions, timezones)):
            raise ContractError("promotion_scope_dimension_missing")
        if len(timezones) != 1:
            raise ContractError("promotion_scope_timezone_ambiguous")
        combination_count = len(stations) * len(leads) * len(cycles) * len(definitions) * len(timezones)
        if combination_count > MAX_SCOPE_KEYS:
            raise ContractError("promotion_scope_limit_exceeded")
        combinations = itertools.product(stations, leads, cycles, definitions, timezones)
        keys = [
            dict(zip(SCOPE_FIELDS, values))
            for values in combinations
        ]
    if not keys:
        raise ContractError("promotion_scope_empty")
    if len(keys) > MAX_SCOPE_KEYS:
        raise ContractError("promotion_scope_limit_exceeded")
    dedup: dict[tuple[str, ...], dict[str, str]] = {}
    for item in keys:
        normalized = _scope_key(item)
        identity = _scope_key_tuple(normalized)
        previous = dedup.get(identity)
        if previous is not None and previous != normalized:
            raise ContractError("promotion_scope_duplicate_conflict")
        dedup[identity] = normalized
    ordered = [dedup[key] for key in sorted(dedup)]
    return {
        "station_ids": sorted({item["station_id"] for item in ordered}),
        "lead_buckets": sorted({item["lead_bucket"] for item in ordered}),
        "run_cycles": sorted({item["run_cycle"] for item in ordered}),
        "settlement_definitions": sorted({item["settlement_definition"] for item in ordered}),
        "settlement_timezones": sorted({item["settlement_timezone"] for item in ordered}),
        "scope_keys": ordered,
        "scope_key_count": len(ordered),
    }


def _scope_from_parent(parent: Mapping[str, Any], candidate_scope: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = parent.get("scope")
    if isinstance(raw, Mapping) or isinstance(raw, Sequence):
        if isinstance(raw, Mapping) and raw.get("settlement_timezone") in (None, ""):
            zones = candidate_scope.get("settlement_timezones") or []
            if len(zones) == 1:
                raw = {**raw, "settlement_timezone": zones[0]}
        try:
            return normalize_scope(raw)
        except ContractError:
            return None
    fields = {field: parent.get(field) for field in SCOPE_FIELDS}
    if fields["settlement_timezone"] in (None, ""):
        zones = candidate_scope.get("settlement_timezones") or []
        if len(zones) == 1:
            fields["settlement_timezone"] = zones[0]
    if all(value not in (None, "") for value in fields.values()):
        try:
            return normalize_scope([fields])
        except ContractError:
            return None
    return None


def _validate_m311(receipt: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    raw = dict(_mapping("m3_11_equivalence_receipt", receipt))
    if raw.get("schema") != M3_11_SCHEMA:
        raise ContractError("m3_11_equivalence_schema_invalid")
    receipt_sha = _sha("m3_11_equivalence_sha256", raw.get("equivalence_sha256"))
    identity = {key: value for key, value in raw.items() if key != "equivalence_sha256"}
    if content_sha256(identity) != receipt_sha:
        raise ContractError("m3_11_equivalence_hash_mismatch")
    if raw.get("state") != "EQUIVALENT":
        raise ContractError("m3_11_equivalence_not_equivalent")
    checkpoint_count = int(raw.get("checkpoint_count", 0))
    if checkpoint_count <= 0:
        raise ContractError("m3_11_equivalence_checkpoint_missing")
    maximum = _number("m3_11_max_absolute_probability_difference", raw.get("max_absolute_probability_difference"), nonnegative=True)
    tolerance = _number("m3_11_equivalence_tolerance", raw.get("tolerance"), nonnegative=True)
    if maximum > tolerance or tolerance > 1e-9:
        raise ContractError("m3_11_equivalence_tolerance_exceeded")
    boundary = _mapping("m3_11_equivalence_boundary", raw.get("boundary"))
    for field in ("writes_production_db", "places_orders", "calls_order_gateway"):
        if boundary.get(field) is not False:
            raise ContractError(f"m3_11_equivalence_{field}_forbidden")
    return raw, receipt_sha


def _flatten_numbers(value: Any, prefix: str = "") -> dict[str, float]:
    if isinstance(value, Mapping):
        result: dict[str, float] = {}
        for key in sorted(value, key=str):
            result.update(_flatten_numbers(value[key], f"{prefix}.{key}" if prefix else str(key)))
        return result
    if isinstance(value, bool):
        return {}
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return {prefix: float(value)}
    return {}


def _validate_parent(parent: Mapping[str, Any], scope: Mapping[str, Any]) -> tuple[dict[str, Any], str, str]:
    raw = dict(_mapping("parent_artifact", parent))
    artifact_sha = _sha("parent_artifact_sha256", raw.get("content_sha256"))
    artifact_id = _text("parent_artifact_id", raw.get("artifact_id") or raw.get("ensemble_artifact_id") or raw.get("calibration_artifact_id"))
    snapshot_sha = content_sha256(raw)
    parent_scope = _scope_from_parent(raw, scope)
    if parent_scope is None:
        raise ContractError("parent_artifact_scope_missing")
    if parent_scope != scope:
        raise ContractError("parent_artifact_scope_mismatch")
    return raw, artifact_sha, snapshot_sha


def _candidate_artifact_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if key not in {"content_sha256", "promoted", "state"}}


def _build_candidate_artifact(
    *,
    candidate_artifact: Mapping[str, Any] | None,
    candidate_parameters: Mapping[str, Any] | None,
    artifact_id: str,
    parent_sha: str,
    parent_snapshot_sha: str,
    scope: Mapping[str, Any],
    evidence_sha: str,
    windows: Mapping[str, Any],
) -> dict[str, Any]:
    if candidate_artifact is not None:
        _bounded(candidate_artifact, name="candidate_artifact")
        raw = dict(_mapping("candidate_artifact", candidate_artifact))
        supplied = _sha("candidate_artifact_content_sha256", raw.get("content_sha256"))
        if content_sha256(_candidate_artifact_identity(raw)) != supplied:
            raise ContractError("candidate_artifact_hash_mismatch")
        if str(raw.get("parent_artifact_sha256") or "") != parent_sha:
            raise ContractError("candidate_artifact_parent_mismatch")
        if str(raw.get("parent_artifact_snapshot_sha256") or "") != parent_snapshot_sha:
            raise ContractError("candidate_artifact_parent_snapshot_mismatch")
        if normalize_scope(raw.get("scope")) != scope:
            raise ContractError("candidate_artifact_scope_mismatch")
        return raw
    parameters = dict(_mapping("candidate_parameters", candidate_parameters or {}))
    if not parameters:
        raise ContractError("candidate_parameters_missing")
    if len(parameters) > MAX_PARAMETERS:
        raise ContractError("candidate_parameters_limit_exceeded")
    identity = {
        "schema": CANDIDATE_ARTIFACT_SCHEMA,
        "artifact_id": _text("candidate_artifact_id", artifact_id),
        "parent_artifact_sha256": parent_sha,
        "parent_artifact_snapshot_sha256": parent_snapshot_sha,
        "scope": dict(scope),
        "parameters": parameters,
        "source_evidence_sha256": evidence_sha,
        "window_days": list(WINDOW_DAYS),
        "candidate_only": True,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _normalise_rows(rows: Iterable[Mapping[str, Any]], *, as_of: str, scope: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    if isinstance(rows, (str, bytes, bytearray)):
        raise ContractError("oos_rows_invalid")
    by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    blockers: set[str] = set()
    allowed_scope = {_scope_key_tuple(item) for item in scope["scope_keys"]}
    for index, raw_value in enumerate(rows):
        if index >= MAX_OOS_ROWS:
            raise ContractError("oos_rows_limit_exceeded")
        raw = dict(_mapping("oos_row", raw_value))
        row_id = _text("oos_row_id", raw.get("row_id") or raw.get("event_id") or f"row-{index}")
        digest = content_sha256(raw)
        existing = by_id.get(row_id)
        if existing is not None:
            if existing[0] != digest:
                raise ContractError("oos_duplicate_row_conflict")
            continue
        window_raw = raw.get("window_days")
        try:
            window_days = int(window_raw)
        except (TypeError, ValueError):
            blockers.add("window_assignment_missing")
            window_days = 0
        if window_days not in WINDOW_DAYS:
            blockers.add("window_assignment_invalid")
        decision = _timestamp("oos_decision_at_utc", raw.get("decision_at_utc"))
        if not _leq(decision, as_of):
            blockers.add(f"as_of_future_decision:{row_id}")
        feature = raw.get("feature_as_of_utc") or raw.get("features_as_of_utc")
        if feature in (None, ""):
            blockers.add(f"feature_as_of_missing:{row_id}")
        else:
            feature = _timestamp("oos_feature_as_of_utc", feature)
            if not _leq(feature, decision):
                blockers.add(f"time_leakage:{row_id}")
            if not _leq(feature, as_of):
                blockers.add(f"as_of_feature_future:{row_id}")
        for field in ("model_published_at_utc", "model_received_at_utc", "source_received_at_utc"):
            value = raw.get(field)
            if value not in (None, "") and not _leq(_timestamp(f"oos_{field}", value), decision):
                blockers.add(f"time_leakage:{row_id}:{field}")
        for field in ("trained_through_utc", "training_end_utc"):
            value = raw.get(field)
            if value not in (None, "") and not _lt(_timestamp(f"oos_{field}", value), decision):
                blockers.add(f"time_leakage:{row_id}:{field}")
        label_time = raw.get("truth_frozen_at_utc") or raw.get("label_as_of_utc")
        if label_time not in (None, "") and not _leq(_timestamp("oos_label_as_of_utc", label_time), as_of):
            blockers.add(f"oos_label_after_as_of:{row_id}")
        split = str(raw.get("evaluation_split") or raw.get("split") or "").strip().upper()
        if raw.get("oos") is not True and split not in {"OOS", "HOLDOUT", "TEST"}:
            blockers.add(f"oos_marker_missing:{row_id}")
        scope_values = {field: raw.get(field) for field in SCOPE_FIELDS}
        if any(value in (None, "") for value in scope_values.values()):
            blockers.add(f"scope_binding_missing:{row_id}")
        else:
            key = _scope_key(scope_values)
            if _scope_key_tuple(key) not in allowed_scope:
                blockers.add(f"scope_out_of_range:{row_id}")
            raw.update(key)
        raw["row_id"] = row_id
        raw["window_days"] = window_days
        raw["decision_at_utc"] = decision
        by_id[row_id] = (digest, raw)
    normalized = sorted((item[1] for item in by_id.values()), key=lambda row: (row["window_days"], row["decision_at_utc"], row["row_id"]))
    return normalized, sorted(blockers)[:MAX_BLOCKERS]


def _window_input(windows: Mapping[str, Any], days: int) -> Mapping[str, Any] | None:
    value = windows.get(str(days))
    if value is None:
        value = windows.get(days)  # type: ignore[arg-type]
    return value if isinstance(value, Mapping) else None


def _window_metrics(
    raw: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    minimum_sample_n: int = DEFAULT_MIN_SAMPLE_N,
    minimum_effective: float = DEFAULT_MIN_EFFECTIVE_SAMPLE_N,
) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    metrics_raw = raw.get("oos_metrics") or raw.get("metrics")
    if not isinstance(metrics_raw, Mapping):
        metrics_raw = {}
    candidate = metrics_raw.get("candidate") if isinstance(metrics_raw.get("candidate"), Mapping) else metrics_raw
    baseline = metrics_raw.get("baseline") if isinstance(metrics_raw.get("baseline"), Mapping) else {}
    output: dict[str, Any] = {}
    for metric in METRICS:
        value = candidate.get(metric)
        if value is None and rows:
            values = [row.get(metric) for row in rows if row.get(metric) is not None]
            value = sum(_number(f"oos_{metric}", item, nonnegative=True) for item in values) / len(values) if values else None
        if value is None:
            blockers.append(f"oos_metric_missing:{metric}")
        else:
            output[metric] = _number(f"oos_{metric}", value, nonnegative=True)
        baseline_value = baseline.get(metric)
        if baseline_value is None:
            baseline_value = metrics_raw.get(f"baseline_{metric}")
        if baseline_value is not None:
            output[f"baseline_{metric}"] = _number(f"baseline_{metric}", baseline_value, nonnegative=True)
    if not rows and int(raw.get("sample_n", 0) or 0) <= 0:
        blockers.append("sample_missing")
    sample_n = int(raw.get("sample_n", len(rows)) or 0)
    if sample_n < 0 or (rows and sample_n != len(rows)):
        blockers.append("sample_count_mismatch")
    effective = _number("window_effective_sample_n", raw.get("effective_sample_n", sum(float(row.get("effective_sample_n", 1.0)) for row in rows)), nonnegative=True)
    output.update({"sample_n": sample_n, "effective_sample_n": effective})
    if sample_n < minimum_sample_n:
        blockers.append("sample_insufficient")
    if effective < minimum_effective:
        blockers.append("effective_sample_insufficient")
    return output, sorted(set(blockers))


def _drift_evidence(
    *,
    parent: Mapping[str, Any],
    candidate: Mapping[str, Any],
    explicit: Mapping[str, Any] | None,
    maximum: float,
) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    metrics = dict(explicit or {})
    parent_parameters = parent.get("parameters")
    candidate_parameters = candidate.get("parameters")
    if isinstance(parent_parameters, Mapping) and isinstance(candidate_parameters, Mapping):
        left = _flatten_numbers(parent_parameters)
        right = _flatten_numbers(candidate_parameters)
        if len(left) > MAX_PARAMETERS or len(right) > MAX_PARAMETERS:
            raise ContractError("promotion_drift_parameter_limit_exceeded")
        if set(left) != set(right):
            metrics["parameter_key_set_match"] = False
            blockers.append("drift_detected")
            # Keep the receipt finite even when a parameter was added or
            # removed.  The key-set mismatch itself is the drift evidence.
            delta = maximum + 1.0
        else:
            delta = max((abs(right[key] - left[key]) for key in left), default=0.0)
            metrics["parameter_key_set_match"] = True
        metrics["max_parameter_delta"] = delta
        metrics["max_allowed_parameter_delta"] = maximum
    elif "detected" not in metrics and "max_parameter_delta" not in metrics:
        blockers.append("drift_evidence_missing")
    detected = metrics.get("detected") is True
    if detected:
        blockers.append("drift_detected")
    if metrics.get("max_parameter_delta") is not None:
        delta = _number("max_parameter_delta", metrics["max_parameter_delta"], nonnegative=True)
        if delta > maximum:
            blockers.append("drift_detected")
        metrics["max_parameter_delta"] = delta
    metrics["max_allowed_parameter_delta"] = maximum
    metrics["detected"] = bool(blockers and any(item == "drift_detected" for item in blockers))
    return metrics, sorted(set(blockers))


def _candidate_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "schema", "state", "evidence_state", "as_of_utc", "scope",
        "parent_artifact_sha256", "parent_artifact_snapshot_sha256",
        "candidate_artifact", "m3_11_equivalence_sha256", "m3_11_equivalence_receipt",
        "windows", "strict_asof_oos_metrics", "cross_season_evidence", "drift_evidence",
        "version_evidence", "blockers", "checks", "review_boundary",
    )
    return {key: raw[key] for key in keys if key in raw}


def validate_version_sequence(sequence: Sequence[Any]) -> list[dict[str, Any]]:
    """Reject a low→high→low replay in an artifact version history.

    M3-11 protects the materialized stage pointer; M3-12 protects the
    candidate lineage that is submitted for review.  Both representations
    are accepted here because historical receipts use either a numeric
    generation or a P0/P0-M/P1 stage label.
    """

    if isinstance(sequence, (str, bytes, bytearray)) or not isinstance(sequence, Sequence) or not sequence:
        raise ContractError("promotion_version_sequence_missing")
    result: list[dict[str, Any]] = []
    highest = -1.0
    for index, item in enumerate(sequence):
        if isinstance(item, Mapping):
            raw = dict(item)
            if raw.get("generation") is not None:
                value = _number("promotion_generation", raw["generation"], nonnegative=True)
            else:
                stage = str(raw.get("stage") or raw.get("version") or "").strip().upper()
                value = {"P0": 0.0, "P0-M": 1.0, "P1": 2.0}.get(stage)
                if value is None:
                    raise ContractError("promotion_version_sequence_item_invalid")
            result.append({"index": index, "value": value, "stage": raw.get("stage"), "generation": raw.get("generation")})
        else:
            text = str(item).strip().upper()
            value = {"P0": 0.0, "P0-M": 1.0, "P1": 2.0}.get(text)
            if value is None:
                value = _number("promotion_generation", item, nonnegative=True)
            result.append({"index": index, "value": value, "stage": text if text in {"P0", "P0-M", "P1"} else None, "generation": value})
        if value < highest:
            raise ContractError("promotion_version_low_high_low")
        highest = max(highest, value)
    return result


def build_probability_promotion_candidate(
    *,
    parent_artifact: Mapping[str, Any],
    scope: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    m3_11_equivalence_receipt: Mapping[str, Any],
    windows: Mapping[str, Any],
    as_of_utc: str,
    oos_rows: Iterable[Mapping[str, Any]] | None = None,
    candidate_artifact: Mapping[str, Any] | None = None,
    candidate_parameters: Mapping[str, Any] | None = None,
    artifact_id: str = "probability-candidate-m3-12",
    cross_season_evidence: Mapping[str, Any] | None = None,
    drift_evidence: Mapping[str, Any] | None = None,
    version_history: Sequence[Any] | None = None,
    limits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one deterministic M3-12 review candidate.

    ``ContractError`` is reserved for malformed/tampered content-addressed
    inputs.  Evidence gaps return a valid candidate whose ``evidence_state``
    is ``BLOCKED_EVIDENCE``.
    """

    assert_process_memory_budget(limit=MAX_TARGET_PROCESS_RSS_BYTES)
    as_of = _timestamp("promotion_as_of_utc", as_of_utc)
    _bounded(parent_artifact, name="parent_artifact")
    _bounded(m3_11_equivalence_receipt, name="m3_11_equivalence_receipt")
    _bounded(windows or {}, name="promotion_windows")
    bounded_limits = dict(limits or {})
    minimum_sample_n = int(bounded_limits.get("min_sample_n", DEFAULT_MIN_SAMPLE_N))
    minimum_effective = float(bounded_limits.get("min_effective_sample_n", DEFAULT_MIN_EFFECTIVE_SAMPLE_N))
    maximum_drift = float(bounded_limits.get("max_parameter_drift", DEFAULT_MAX_PARAMETER_DRIFT))
    if minimum_sample_n < 1 or minimum_effective <= 0 or maximum_drift < 0:
        raise ContractError("promotion_limits_invalid")
    scope_norm = normalize_scope(scope)
    if windows is None:
        windows_norm: dict[str, Any] = {}
    else:
        windows_norm = dict(_mapping("promotion_windows", windows))
    parent, parent_sha, parent_snapshot_sha = _validate_parent(parent_artifact, scope_norm)
    receipt, receipt_sha = _validate_m311(m3_11_equivalence_receipt)

    candidate = _build_candidate_artifact(
        candidate_artifact=candidate_artifact,
        candidate_parameters=candidate_parameters,
        artifact_id=artifact_id,
        parent_sha=parent_sha,
        parent_snapshot_sha=parent_snapshot_sha,
        scope=scope_norm,
        evidence_sha=receipt_sha,
        windows=windows_norm,
    )
    candidate_parameters_for_drift = candidate.get("parameters") if isinstance(candidate.get("parameters"), Mapping) else {}
    source_rows = list(oos_rows or [])
    # Accept compact per-window packets as well as the canonical flat row
    # stream.  The rows still pass through one deduplicating/as-of validator;
    # no second evidence path is allowed to bypass it.
    for days in WINDOW_DAYS:
        raw_window = _window_input(windows_norm, days)
        if raw_window is None:
            continue
        nested_rows = raw_window.get("rows")
        if isinstance(nested_rows, Sequence) and not isinstance(nested_rows, (str, bytes, bytearray)):
            for item in nested_rows:
                if isinstance(item, Mapping):
                    row = dict(item)
                    row.setdefault("window_days", days)
                    source_rows.append(row)
    normalized_rows, row_blockers = _normalise_rows(source_rows, as_of=as_of, scope=scope_norm)
    blockers: set[str] = set(row_blockers)
    window_output: dict[str, Any] = {}
    all_seasons: set[str] = set()
    seasons_by_scope: dict[tuple[str, ...], set[str]] = defaultdict(set)
    scope_keys = {_scope_key_tuple(item) for item in scope_norm["scope_keys"]}
    for days in WINDOW_DAYS:
        raw_window = _window_input(windows_norm, days)
        if raw_window is None:
            blockers.add(f"window_missing:{days}")
            window_output[str(days)] = {"window_days": days, "state": BLOCKED_EVIDENCE}
            continue
        start_raw = raw_window.get("window_start_utc") or raw_window.get("start_utc")
        end_raw = raw_window.get("window_end_utc") or raw_window.get("end_utc")
        if start_raw in (None, "") or end_raw in (None, ""):
            blockers.add(f"window_boundary_missing:{days}")
            window_output[str(days)] = {"window_days": days, "state": BLOCKED_EVIDENCE}
            continue
        start = _timestamp(f"window_{days}_start_utc", start_raw)
        end = _timestamp(f"window_{days}_end_utc", end_raw)
        if not _lt(start, end):
            blockers.add(f"window_boundary_invalid:{days}")
        metrics_as_of = raw_window.get("metrics_as_of_utc")
        if metrics_as_of in (None, "") and isinstance(raw_window.get("oos_metrics"), Mapping):
            metrics_as_of = raw_window["oos_metrics"].get("as_of_utc")
        if metrics_as_of not in (None, "") and not _leq(_timestamp(f"window_{days}_metrics_as_of_utc", metrics_as_of), as_of):
            blockers.add(f"as_of_metric_future:{days}")
        training_end = raw_window.get("training_end_utc") or raw_window.get("trained_through_utc")
        if training_end not in (None, "") and not _lt(_timestamp(f"window_{days}_training_end_utc", training_end), start):
            blockers.add(f"time_leakage:window_{days}_training_end")
        rows_for_window = [row for row in normalized_rows if row.get("window_days") == days]
        if isinstance(raw_window.get("rows"), Sequence) and not isinstance(raw_window.get("rows"), (str, bytes, bytearray)):
            nested = [dict(item) for item in raw_window["rows"] if isinstance(item, Mapping)]
            if nested:
                nested_ids = {str(item.get("row_id") or item.get("event_id") or "") for item in nested}
                rows_for_window = [row for row in normalized_rows if row["row_id"] in nested_ids]
        for row in rows_for_window:
            if not (_leq(start, row["decision_at_utc"]) and _lt(row["decision_at_utc"], end)):
                blockers.add(f"window_boundary_violation:{days}:{row['row_id']}")
            season = str(row.get("season_bin") or "").strip()
            if season and all(field in row and row.get(field) not in (None, "") for field in SCOPE_FIELDS):
                all_seasons.add(season)
                key = _scope_key_tuple(row)
                if key in scope_keys:
                    seasons_by_scope[key].add(season)
        local_metrics, local_blockers = _window_metrics(
            raw_window,
            rows_for_window,
            minimum_sample_n=minimum_sample_n,
            minimum_effective=minimum_effective,
        )
        # The caller-supplied limits are part of the contract, not a mutable
        # global.  Re-apply them here so a smaller test limit cannot be
        # bypassed by pre-computed sample fields.
        if local_metrics.get("sample_n", 0) < minimum_sample_n:
            local_blockers.append("sample_insufficient")
        if local_metrics.get("effective_sample_n", 0.0) < minimum_effective:
            local_blockers.append("effective_sample_insufficient")
        blockers.update(f"window_{days}:{item}" for item in local_blockers)
        window_output[str(days)] = {
            "window_days": days,
            "window_start_utc": start,
            "window_end_utc": end,
            "metrics": local_metrics,
            "row_count": len(rows_for_window),
            "state": BLOCKED_EVIDENCE if local_blockers else EVIDENCE_COMPLETE,
        }
    explicit_seasons = dict(cross_season_evidence or {})
    declared_seasons = explicit_seasons.get("season_bins")
    if isinstance(declared_seasons, Sequence) and not isinstance(declared_seasons, (str, bytes, bytearray)):
        all_seasons.update(str(item).strip() for item in declared_seasons if str(item).strip())
    if len(all_seasons) < MIN_CROSS_SEASONS:
        blockers.add("cross_season_missing")
    for key in sorted(scope_keys):
        if len(seasons_by_scope.get(key, set())) < MIN_CROSS_SEASONS:
            blockers.add("cross_season_missing:" + "|".join(key))
    covered_scope_keys = {
        _scope_key_tuple(row)
        for row in normalized_rows
        if all(field in row and row.get(field) not in (None, "") for field in SCOPE_FIELDS)
        and _scope_key_tuple(row) in scope_keys
    }
    if covered_scope_keys != scope_keys:
        blockers.add("scope_coverage_missing")
    drift, drift_blockers = _drift_evidence(
        parent=parent,
        candidate=candidate,
        explicit=drift_evidence,
        maximum=maximum_drift,
    )
    blockers.update(drift_blockers)
    version_output: list[dict[str, Any]] = []
    if version_history is not None:
        try:
            version_output = validate_version_sequence(version_history)
        except ContractError as exc:
            blockers.add(str(exc))
    evidence_sha = content_sha256({
        "rows": normalized_rows,
        "windows": window_output,
        "scope": scope_norm,
        "as_of_utc": as_of,
    })
    # If an externally supplied child artifact claims a different evidence
    # binding, it is a tampered candidate rather than a weak candidate.
    if str(candidate.get("source_evidence_sha256") or receipt_sha) not in {receipt_sha, evidence_sha}:
        raise ContractError("candidate_artifact_evidence_binding_mismatch")
    strict_metrics = {
        "window_metrics": window_output,
        "oos_row_count": len(normalized_rows),
        "oos_row_sha256": content_sha256(normalized_rows),
        "as_of_utc": as_of,
        "strict_asof": bool(normalized_rows) and not any(item.startswith(("as_of_", "time_leakage", "oos_label_after_as_of")) for item in blockers),
        "oos_only": bool(normalized_rows) and not any(item.startswith("oos_marker_missing") for item in blockers),
    }
    checks = {
        "windows_3_7_15_30": not any(item.startswith(("window_missing", "window_boundary")) for item in blockers),
        "cross_season": not any(item.startswith("cross_season_missing") for item in blockers),
        "station_lead_run_settlement_scope": "scope_coverage_missing" not in blockers,
        "m3_11_equivalence": True,
        "strict_asof_oos": strict_metrics["strict_asof"] and strict_metrics["oos_only"],
        "parent_artifact_bound": True,
        "drift_clear": not any(item == "drift_detected" for item in blockers),
    }
    review_boundary = {
        "candidate_only": True,
        "writes_database": False,
        "writes_authority": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    identity = {
        "schema": SCHEMA,
        "state": CANDIDATE_STATE,
        "evidence_state": BLOCKED_EVIDENCE if blockers else EVIDENCE_COMPLETE,
        "as_of_utc": as_of,
        "scope": scope_norm,
        "parent_artifact_sha256": parent_sha,
        "parent_artifact_snapshot_sha256": parent_snapshot_sha,
        "candidate_artifact": candidate,
        "m3_11_equivalence_sha256": receipt_sha,
        "m3_11_equivalence_receipt": receipt,
        "windows": window_output,
        "strict_asof_oos_metrics": strict_metrics,
        "cross_season_evidence": {
            **explicit_seasons,
            "season_bins": sorted(all_seasons),
            "season_count": len(all_seasons),
            "min_required": MIN_CROSS_SEASONS,
        },
        "drift_evidence": drift,
        "version_evidence": version_output,
        "blockers": sorted(blockers)[:MAX_BLOCKERS],
        "checks": checks,
        "review_boundary": review_boundary,
    }
    _bounded(identity)
    result = {**identity, "content_sha256": content_sha256(identity)}
    _bounded(result)
    return result


def validate_probability_promotion_candidate(
    raw: Mapping[str, Any],
    *,
    parent_artifact: Mapping[str, Any] | None = None,
    m3_11_equivalence_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the immutable candidate envelope and its optional external parents."""

    value = dict(_mapping("probability_promotion_candidate", raw))
    if value.get("schema") != SCHEMA:
        raise ContractError("probability_promotion_schema_invalid")
    if value.get("state") != CANDIDATE_STATE:
        raise ContractError("probability_promotion_state_invalid")
    if value.get("evidence_state") not in {BLOCKED_EVIDENCE, EVIDENCE_COMPLETE}:
        raise ContractError("probability_promotion_evidence_state_invalid")
    if "activation" in canonical_json(value).lower() or "formal_promoted" in canonical_json(value).lower():
        raise ContractError("probability_promotion_authority_field_forbidden")
    supplied = _sha("probability_promotion_content_sha256", value.get("content_sha256"))
    if content_sha256(_candidate_identity(value)) != supplied:
        raise ContractError("probability_promotion_content_sha256_mismatch")
    scope = normalize_scope(value.get("scope"))
    _sha("probability_promotion_parent_sha256", value.get("parent_artifact_sha256"))
    _sha("probability_promotion_parent_snapshot_sha256", value.get("parent_artifact_snapshot_sha256"))
    candidate = _mapping("probability_promotion_candidate_artifact", value.get("candidate_artifact"))
    candidate_sha = _sha("candidate_artifact_content_sha256", candidate.get("content_sha256"))
    if content_sha256(_candidate_artifact_identity(candidate)) != candidate_sha:
        raise ContractError("candidate_artifact_hash_mismatch")
    if candidate.get("parent_artifact_sha256") != value.get("parent_artifact_sha256"):
        raise ContractError("candidate_parent_sha256_mismatch")
    if normalize_scope(candidate.get("scope")) != scope:
        raise ContractError("candidate_scope_hash_binding_mismatch")
    receipt, receipt_sha = _validate_m311(_mapping("m3_11_equivalence_receipt", value.get("m3_11_equivalence_receipt")))
    if receipt_sha != value.get("m3_11_equivalence_sha256"):
        raise ContractError("candidate_m3_11_receipt_binding_mismatch")
    if m3_11_equivalence_receipt is not None and content_sha256(dict(m3_11_equivalence_receipt)) != content_sha256(receipt):
        raise ContractError("candidate_m3_11_receipt_external_mismatch")
    if parent_artifact is not None:
        parent = dict(_mapping("parent_artifact", parent_artifact))
        if content_sha256(parent) != value.get("parent_artifact_snapshot_sha256"):
            raise ContractError("candidate_parent_snapshot_mismatch")
        if _sha("parent_artifact_content_sha256", parent.get("content_sha256")) != value.get("parent_artifact_sha256"):
            raise ContractError("candidate_parent_content_mismatch")
    boundary = _mapping("probability_promotion_review_boundary", value.get("review_boundary"))
    for field, expected in {
        "candidate_only": True,
        "writes_database": False,
        "writes_authority": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }.items():
        if boundary.get(field) != expected:
            raise ContractError(f"probability_promotion_boundary_{field}_invalid")
    if value.get("checks", {}).get("m3_11_equivalence") is not True:
        raise ContractError("probability_promotion_m3_11_check_invalid")
    _bounded(value)
    return value


def assert_probability_promotion_boundary(raw: Mapping[str, Any]) -> None:
    validate_probability_promotion_candidate(raw)


# Short aliases keep the contract convenient for offline callers while the
# long names remain the documented API.
build_candidate = build_probability_promotion_candidate
validate_candidate = validate_probability_promotion_candidate


__all__ = [
    "BLOCKED_EVIDENCE",
    "CANDIDATE_ARTIFACT_SCHEMA",
    "CANDIDATE_STATE",
    "EVIDENCE_COMPLETE",
    "M3_11_SCHEMA",
    "SCHEMA",
    "WINDOW_DAYS",
    "assert_probability_promotion_boundary",
    "build_candidate",
    "build_probability_promotion_candidate",
    "normalize_scope",
    "validate_version_sequence",
    "validate_candidate",
    "validate_probability_promotion_candidate",
]
