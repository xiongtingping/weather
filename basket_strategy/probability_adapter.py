"""Read-only adapter for the existing P0/P1 -> p(B) probability surfaces.

This module deliberately does not build a second weather model.  It accepts
already committed/materialized posterior projections and chooses P1 when it
is present, otherwise P0.  A missing or malformed market-bucket projection is
an explicit block, never a fallback to a point forecast or a market mean.
"""

from __future__ import annotations

import json
import math
from typing import Any, Mapping, Sequence

from weather_basket_yes_runtime import parse_bucket_interval


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_value(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _bucket_c(label: Any) -> int | None:
    text = _text(label)
    try:
        interval = parse_bucket_interval(text)
    except ValueError:
        # Existing probability SSOTs sometimes use a bare numeric key.
        parsed = _finite(text.replace("°", "").replace("C", "").strip())
        return int(parsed) if parsed is not None and parsed.is_integer() else None
    kind = str(interval.get("kind") or "")
    if kind not in {"single", "lower_tail", "upper_tail"}:
        return None
    boundary = interval.get("lower")
    if boundary is None:
        boundary = interval.get("upper")
    if boundary is not None:
        parsed = _finite(boundary)
        return int(round(parsed)) if parsed is not None else None
    return None


def _distribution_candidate(container: Mapping[str, Any]) -> tuple[Any, str] | None:
    """Find a committed bucket distribution without unbounded traversal.

    Materialized projection objects are normally JSON trees, but callers can
    also pass an already-normalized mapping.  The old recursive walk called
    itself for every missing nested key after converting ``None`` to ``{}``,
    so a projection with a ``posterior_run_id`` but no P1 distribution could
    recurse forever before the P0 fallback was considered.  Walk only real,
    non-empty mappings and keep an identity set so malformed cyclic mappings
    fail closed instead of consuming the process stack.
    """

    direct_keys = (
        "market_bucket_probabilities",
        "posterior_market_bucket_probabilities",
        "bucket_probabilities",
        "probability_by_bucket",
        "p_b",
        "market_bucket_distribution",
        "market_bucket_distribution_json",
        "market_bucket_probabilities_json",
    )
    root = _mapping(container)
    if not root:
        return None
    pending: list[Mapping[str, Any]] = [root]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        for key in direct_keys:
            if key in current:
                return _json_value(current.get(key)), key
        # ``_probability_projection`` in the production adapter is already
        # normalized to ``status/stage/probabilities``.  Accept that exact
        # shape only when its lineage explicitly identifies a posterior
        # stage; a generic ``probabilities`` field is not a market SSOT.
        normalized_stage = _text(
            current.get("stage")
            or current.get("probability_stage")
            or current.get("posterior_stage")
        ).upper()
        if current.get("status") == "available" and normalized_stage in {"P0", "P1"} and "probabilities" in current:
            return _json_value(current.get("probabilities")), "probabilities"

        children: list[Mapping[str, Any]] = []
        for nested_key in ("resolution_bridge", "market_projection", "posterior_market_projection"):
            nested = _mapping(_json_value(current.get(nested_key)))
            if nested:
                children.append(nested)
        checkpoint = _mapping(_json_value(current.get("checkpoint")))
        if checkpoint:
            children.append(checkpoint)
        # Reverse push preserves the former recursive search order while
        # allowing the identity guard above to handle cycles safely.
        pending.extend(reversed(children))
    return None


def _stage_candidates(root: Mapping[str, Any], stage: str) -> list[Mapping[str, Any]]:
    names = (
        ("p1", "P1", "latest_p1", "posterior_p1", "strong_posterior")
        if stage == "P1"
        else ("p0", "P0", "initial_p0", "posterior_p0", "weak_posterior")
    )
    candidates: list[Mapping[str, Any]] = []
    for name in names:
        value = _json_value(root.get(name))
        if isinstance(value, Mapping):
            candidates.append(value)
    # A stage object can identify itself and carry p(B) directly.
    declared_stage = _text(root.get("stage") or root.get("probability_stage") or root.get("posterior_stage")).upper()
    if declared_stage == stage and _distribution_candidate(root) is not None:
        candidates.append(root)
    checkpoint = _mapping(_json_value(root.get("checkpoint")))
    checkpoint_kind = _text(checkpoint.get("checkpoint_kind")).lower()
    if checkpoint and _distribution_candidate(checkpoint) is not None:
        if stage == "P0" and checkpoint_kind in {"initial", "p0", "initial_checkpoint"}:
            candidates.append(checkpoint)
        elif stage == "P1" and checkpoint_kind not in {"initial", "p0", "initial_checkpoint"}:
            candidates.append(checkpoint)
    root_checkpoint_kind = _text(root.get("checkpoint_kind")).lower()
    if root_checkpoint_kind and _distribution_candidate(root) is not None:
        if stage == "P0" and root_checkpoint_kind in {"initial", "p0", "initial_checkpoint"}:
            candidates.append(root)
        elif stage == "P1" and root_checkpoint_kind not in {"initial", "p0", "initial_checkpoint"}:
            candidates.append(root)
    # A materialized RealizationState already names the committed posterior
    # lineage and is therefore an acceptable latest P1 surface when it carries
    # p(B) directly.
    if (
        stage == "P1"
        and declared_stage in {"", "P1"}
        and (_text(root.get("realization_state_id")) or _text(root.get("posterior_run_id")))
        and _distribution_candidate(root) is not None
    ):
        candidates.append(root)
    return candidates


def _parse_distribution(value: Any) -> tuple[dict[str, float], list[dict[str, Any]], list[str]]:
    value = _json_value(value)
    rows: list[tuple[str, float]] = []
    if isinstance(value, Mapping):
        for label, probability in value.items():
            rows.append((_text(label), _finite(probability) if _finite(probability) is not None else float("nan")))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for raw in value:
            row = _mapping(raw)
            label = _text(row.get("bucket") or row.get("outcome") or row.get("label") or row.get("bucket_id"))
            probability = _finite(row.get("probability") if row.get("probability") is not None else row.get("posterior"))
            rows.append((label, probability if probability is not None else float("nan")))
    issues: list[str] = []
    parsed: dict[str, float] = {}
    output_rows: list[dict[str, Any]] = []
    for label, probability in rows:
        if not label or not math.isfinite(probability) or probability < 0.0 or probability > 1.0:
            issues.append("probability_bucket_row_invalid")
            continue
        bucket_c = _bucket_c(label)
        try:
            bucket_kind = str(parse_bucket_interval(label).get("kind") or "")
        except ValueError:
            bucket_kind = "single" if bucket_c is not None else ""
        canonical_key = f"{bucket_c}°C" if bucket_c is not None and bucket_kind == "single" else label
        if canonical_key in parsed:
            issues.append("probability_bucket_duplicate")
            continue
        parsed[canonical_key] = probability
        output_rows.append({
            "bucket": label,
            "canonical_bucket": canonical_key,
            "bucket_c": bucket_c,
            "kind": bucket_kind,
            "probability": probability,
        })
    total = sum(parsed.values())
    if not parsed:
        issues.append("probability_bucket_distribution_missing")
    elif abs(total - 1.0) > 1e-6:
        issues.append("probability_bucket_distribution_not_normalized")
    return parsed, output_rows, sorted(set(issues))


def extract_probability_projection(request: Mapping[str, Any]) -> dict[str, Any]:
    """Extract committed P1/P0 market-bucket probabilities without recomputing them."""

    request = request if isinstance(request, Mapping) else {}
    roots: list[Mapping[str, Any]] = []
    for key in ("probability_projection", "posterior_projection", "posterior", "realization_state"):
        value = _json_value(request.get(key))
        if isinstance(value, Mapping):
            roots.append(value)
    roots.append(request)

    for stage in ("P1", "P0"):
        stage_seen = False
        stage_invalid: list[str] = []
        for root in roots:
            candidates = _stage_candidates(root, stage)
            # A direct root distribution is accepted only when its stage is
            # explicit; this prevents a generic stale market snapshot from
            # being mistaken for a committed posterior.
            if not candidates and _text(root.get("stage") or root.get("probability_stage") or root.get("posterior_stage")).upper() == stage:
                candidates = [root]
            for candidate in candidates:
                stage_seen = True
                found = _distribution_candidate(candidate)
                if found is None:
                    stage_invalid.append(f"{stage.lower()}_market_bucket_projection_missing")
                    continue
                distribution, source_field = found
                probabilities, rows, issues = _parse_distribution(distribution)
                if issues:
                    stage_invalid.extend(f"{stage.lower()}_{issue}" for issue in issues)
                    continue
                return {
                    "status": "available",
                    "stage": stage,
                    "source_field": source_field,
                    "probabilities": probabilities,
                    "rows": rows,
                    "posterior_checkpoint_id": _text(candidate.get("posterior_checkpoint_id")),
                    "posterior_run_id": _text(candidate.get("posterior_run_id")),
                    "realization_state_id": _text(candidate.get("realization_state_id")),
                    "projection_id": _text(candidate.get("market_projection_id") or candidate.get("projection_id")),
                    "reason_codes": [],
                }
        if stage_seen and stage_invalid:
            return {
                "status": "blocked",
                "stage": stage,
                "probabilities": {},
                "rows": [],
                "reason_codes": sorted(set(stage_invalid)),
            }
    return {
        "status": "blocked",
        "stage": "",
        "probabilities": {},
        "rows": [],
        "reason_codes": ["p1_and_p0_market_bucket_projection_missing"],
    }


__all__ = ["extract_probability_projection"]
