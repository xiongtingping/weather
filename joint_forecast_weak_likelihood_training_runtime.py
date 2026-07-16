"""Offline trainer for the one source-set aware weak-likelihood model.

The trainer deliberately has no database, network, posterior, market or order
dependency.  It turns only strict-as-of forecast revisions and closed physical
daily-high labels into an immutable *shadow* artifact.  Promotion/approval is a
separate action; training coverage can never block live AMSC fact ingestion.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from statistics import fmean
from typing import Any, Mapping, Sequence

from joint_forecast_weak_likelihood_runtime import ARTIFACT_SCHEMA_VERSION, canonical_artifact_sha256
from joint_weak_bias_calibration_runtime import (
    DEFAULT_EWMA_ALPHA,
    fit_joint_weak_bias_calibration,
    robust_scale,
    select_joint_weak_bias_state,
)
from weak_likelihood_source_set_runtime import SOURCE_UNIVERSE, SURFACE_SCHEMA_VERSION, source_set_key


DATASET_SCHEMA_VERSION = "joint_forecast_weak_likelihood_training_dataset_v1"
TRAINING_REPORT_SCHEMA_VERSION = "joint_forecast_weak_likelihood_training_report_v1"
TRAINING_POLICY = "strict_asof_forecast_to_closed_physical_x_v1"
_CONTEXT_FIELDS = ("season", "weather_regime", "lead_bucket")
_FORBIDDEN_FORECAST_TOKENS = ("market", "price", "order", "position", "pnl", "edge", "lcb", "metar", "observation", "current_")
_PROVENANCE_ONLY_KEYS = frozenset({"boundary", "raw_object_refs", "raw_sha256s", "snapshot_sha256s", "decoder_field_trace"})


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _contains_forbidden_forecast_input(value: Any, *, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = _text(key).lower()
            child_path = f"{path}.{name}" if path else name
            if name in _PROVENANCE_ONLY_KEYS:
                # Provenance is auditable lineage, not a model feature surface.
                # Its explanatory names must not be mistaken for leaked inputs.
                continue
            if any(token in name for token in _FORBIDDEN_FORECAST_TOKENS):
                found.append(child_path)
            found.extend(_contains_forbidden_forecast_input(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_contains_forbidden_forecast_input(child, path=f"{path}[{index}]"))
    return found


def _context_key(context: Mapping[str, Any]) -> str:
    return "|".join(f"{field}={_text(context.get(field))}" for field in _CONTEXT_FIELDS)


def _source_ids(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    seen = {_text(row.get("source_id")) for row in rows}
    return [source for source in SOURCE_UNIVERSE if source in seen]


def _valid_revision(row: Mapping[str, Any], *, as_of: datetime) -> tuple[dict[str, Any] | None, str]:
    value = _mapping(row)
    source_id = _text(value.get("source_id"))
    peak = _finite(value.get("forecast_peak_c"))
    available_at = _time(value.get("available_at") or value.get("first_actionable_seen_at"))
    if source_id not in SOURCE_UNIVERSE:
        return None, "source_id_not_in_unique_weak_universe"
    if _text(value.get("source_class")) != "forecast_weak_source":
        return None, "source_class_not_forecast_weak_source"
    if value.get("strict_asof_tier_a") is not True or value.get("response_headers_observed") is not True:
        return None, "strict_asof_tier_a_transport_evidence_missing"
    if not _text(value.get("raw_sha256")) or not _text(value.get("source_revision_id")):
        return None, "forecast_revision_raw_or_identity_missing"
    if available_at is None or available_at > as_of:
        return None, "forecast_revision_not_visible_at_cutoff"
    if peak is None:
        return None, "forecast_peak_missing_or_invalid"
    if _contains_forbidden_forecast_input(value):
        return None, "forecast_revision_contains_forbidden_observation_or_market_input"
    return {
        "source_id": source_id,
        "source_revision_id": _text(value.get("source_revision_id")),
        "forecast_peak_c": peak,
        "available_at": available_at.isoformat().replace("+00:00", "Z"),
        "raw_sha256": _text(value.get("raw_sha256")),
    }, ""


def build_joint_weak_training_dataset(
    *, events: Sequence[Mapping[str, Any]], source_catalog: Mapping[str, Any]
) -> dict[str, Any]:
    """Construct exact-combination samples; invalid rows are quarantined, never repaired."""

    catalog = _mapping(source_catalog)
    accepted: list[dict[str, Any]] = []
    excluded: dict[str, int] = defaultdict(int)
    lineage: set[str] = set()
    for raw_event in events:
        event = _mapping(raw_event)
        event_id = _text(event.get("event_id"))
        event_date = _text(event.get("event_date"))[:10]
        training_identity = _text(event.get("training_identity")) or _text(event.get("target_identity")) or f"legacy_event:{event_id}"
        context = _mapping(event.get("forecast_context"))
        cutoff = _time(event.get("decision_as_of"))
        label = _mapping(event.get("physical_x_final"))
        target_peak = _finite(label.get("temperature_c"))
        label_ok = (
            label.get("label_kind") == "physical_x_final"
            and label.get("final") is True
            and _text(label.get("raw_sha256"))
            and target_peak is not None
        )
        if not event_id or not event_date or cutoff is None:
            excluded["event_identity_or_cutoff_missing"] += 1
            continue
        if any(not _text(context.get(field)) for field in _CONTEXT_FIELDS):
            excluded["forecast_context_missing"] += 1
            continue
        if not label_ok:
            excluded["closed_physical_x_label_missing_or_invalid"] += 1
            continue
        selected: dict[str, tuple[datetime, dict[str, Any]]] = {}
        invalid_reasons: list[str] = []
        revisions = event.get("source_revisions") if isinstance(event.get("source_revisions"), Sequence) else []
        for raw_revision in revisions:
            revision, reason = _valid_revision(_mapping(raw_revision), as_of=cutoff)
            if revision is None:
                invalid_reasons.append(reason)
                continue
            visible_at = _time(revision["available_at"])
            assert visible_at is not None
            old = selected.get(revision["source_id"])
            if old is None or (visible_at, revision["source_revision_id"]) > (old[0], old[1]["source_revision_id"]):
                selected[revision["source_id"]] = (visible_at, revision)
        rows = [selected[source][1] for source in SOURCE_UNIVERSE if source in selected]
        available = _source_ids(rows)
        missing = [source for source in SOURCE_UNIVERSE if source not in available]
        if not rows:
            excluded["no_strict_asof_forecasts_at_cutoff"] += 1
            continue
        missing_catalog = [source for source in available if not _text(_mapping(catalog.get(source)).get("provider_family"))]
        if missing_catalog:
            excluded["source_catalog_provider_family_missing"] += 1
            continue
        lineage.update(row["raw_sha256"] for row in rows)
        lineage.add(_text(label.get("raw_sha256")))
        accepted.append(
            {
                "event_id": event_id,
                "training_identity": training_identity,
                "identity_kind": _text(event.get("identity_kind")) or "legacy_event",
                "event_date": event_date,
                "decision_as_of": cutoff.isoformat().replace("+00:00", "Z"),
                "context_key": _context_key(context),
                "forecast_context": {field: _text(context.get(field)) for field in _CONTEXT_FIELDS},
                "physical_x_final_c": target_peak,
                "physical_x_label_raw_sha256": _text(label.get("raw_sha256")),
                "available_source_ids": available,
                "missing_source_ids": missing,
                "source_set_key": source_set_key(available),
                "source_revisions": rows,
                "quarantined_revision_counts": dict(sorted({reason: invalid_reasons.count(reason) for reason in set(invalid_reasons)}.items())),
            }
        )
    accepted.sort(key=lambda item: (item["event_date"], item["event_id"], item["decision_as_of"]))
    event_days = {row["event_date"] for row in accepted}
    manifest = {
        "schema": DATASET_SCHEMA_VERSION,
        "training_data_policy": TRAINING_POLICY,
        "sample_count": len(accepted),
        "event_day_count": len(event_days),
        "raw_lineage_count": len(lineage),
        "raw_lineage_sha256": _sha256(sorted(lineage)),
        "excluded_counts": dict(sorted(excluded.items())),
    }
    manifest["dataset_sha256"] = _sha256({"samples": accepted, "manifest": manifest})
    return {"schema": DATASET_SCHEMA_VERSION, "ok": bool(accepted), "state": "available" if accepted else "blocked", "samples": accepted, "manifest": manifest, "source_catalog": catalog, "boundary": {"strict_asof_training_only": True, "uses_closed_physical_x_not_market": True, "writes_posterior": False, "places_orders": False}}


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise ValueError("correlation_pair_insufficient")
    left_mean, right_mean = fmean(left), fmean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    denominator = math.sqrt(sum((a - left_mean) ** 2 for a in left) * sum((b - right_mean) ** 2 for b in right))
    return max(-0.99, min(0.99, numerator / denominator)) if denominator > 0.0 else 0.0


def train_joint_weak_source_set_artifact(
    *, dataset: Mapping[str, Any], minimum_event_days_per_surface: int = 30, minimum_pair_samples: int = 20,
    bias_ewma_alpha: float = DEFAULT_EWMA_ALPHA,
) -> dict[str, Any]:
    """Fit independent exact-combination surfaces from one immutable dataset."""

    value = _mapping(dataset)
    samples = value.get("samples") if isinstance(value.get("samples"), Sequence) else []
    catalog = _mapping(value.get("source_catalog"))
    # This state is trained across exact source sets, because a source's
    # systematic temperature bias remains evidence about that source even when
    # another forecast source is temporarily missing.  Exact source-set
    # calibration/correlation remains separate below.
    bias_correction = fit_joint_weak_bias_calibration(samples=samples, ewma_alpha=bias_ewma_alpha)
    by_surface: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        row = _mapping(sample)
        by_surface[(_text(row.get("context_key")), _text(row.get("source_set_key")))].append(row)
    surfaces_by_context: dict[str, dict[str, Any]] = defaultdict(dict)
    excluded: dict[str, int] = defaultdict(int)
    for (context_key, source_key), rows in sorted(by_surface.items()):
        days = {row["event_date"] for row in rows}
        if len(days) < int(minimum_event_days_per_surface):
            excluded["source_set_event_days_below_minimum"] += len(rows)
            continue
        available = list(rows[0]["available_source_ids"])
        residuals: dict[str, list[float]] = {source: [] for source in available}
        paired: dict[tuple[str, str], tuple[list[float], list[float]]] = {}
        for row in rows:
            target = float(row["physical_x_final_c"])
            context = _mapping(row.get("forecast_context"))
            # Fit the exact source-set surface on the same corrected error
            # live inference produces. Raw residuals would overstate sigma
            # and correlation when bias differs by season or D0 time bin.
            values: dict[str, float] = {}
            for revision in row["source_revisions"]:
                source = _text(revision.get("source_id"))
                raw_residual = float(revision["forecast_peak_c"]) - target
                bias_state = select_joint_weak_bias_state(
                    calibration=bias_correction,
                    source_id=source,
                    season=_text(context.get("season")),
                    weather_regime=_text(context.get("weather_regime")),
                    lead_bucket=_text(context.get("lead_bucket")),
                    decision_as_of=_text(row.get("decision_as_of")),
                )
                values[source] = raw_residual - float(bias_state["bias_c"])
            for source in available:
                residuals[source].append(values[source])
            for index, left in enumerate(available):
                for right in available[index + 1 :]:
                    pair = paired.setdefault((left, right), ([], []))
                    pair[0].append(values[left])
                    pair[1].append(values[right])
        if any(len(pair[0]) < int(minimum_pair_samples) for pair in paired.values()):
            excluded["source_set_pair_samples_below_minimum"] += len(rows)
            continue
        calibration = {}
        errors = {}
        for source, values in residuals.items():
            sigma = robust_scale(values)
            # bias_c is resolved at runtime from the frozen top-level
            # hierarchy using the live forecast-only context.  Keeping this
            # surface field neutral prevents a context/surface mean from
            # silently overriding D0 time-bin selection.
            calibration[source] = {
                "bias_c": 0.0,
                "error_sigma_c": sigma,
                "residual_distribution": {
                    "schema": "conditional_source_residual_distribution_v1",
                    "family": "robust_gaussian_after_hierarchical_bias",
                    "location_c": 0.0,
                    "scale_c": sigma,
                    "learned_from_closed_physical_x": True,
                    "state": "fitted_condition_residual_distribution",
                    "fit_context_key": context_key,
                },
                "sample_count": len({row["event_date"] for row in rows}),
                "bias_mode": "artifact_hierarchical_state",
                "error_residual_definition": "forecast_peak_c_minus_selected_hierarchical_bias_c_minus_physical_x_final_c",
            }
            errors[source] = sigma
        correlation = {source: {other: 1.0 if source == other else 0.0 for other in available} for source in available}
        for (left, right), (left_values, right_values) in paired.items():
            value_correlation = _correlation(left_values, right_values)
            correlation[left][right] = value_correlation
            correlation[right][left] = value_correlation
        inverse_variance = {source: 1.0 / (errors[source] ** 2) for source in available}
        weight_total = sum(inverse_variance.values())
        weights = {source: value / weight_total for source, value in inverse_variance.items()}
        # The scale is fitted from observed residual variance relative to the
        # independent-error pool. It is not a hand-set source availability penalty.
        expected_variance = sum((weights[source] ** 2) * (errors[source] ** 2) for source in available)
        empirical_pool_errors: list[float] = []
        for row in rows:
            context = _mapping(row.get("forecast_context"))
            target = float(row["physical_x_final_c"])
            pooled_error = 0.0
            for revision in row["source_revisions"]:
                source = _text(revision.get("source_id"))
                bias_state = select_joint_weak_bias_state(
                    calibration=bias_correction,
                    source_id=source,
                    season=_text(context.get("season")),
                    weather_regime=_text(context.get("weather_regime")),
                    lead_bucket=_text(context.get("lead_bucket")),
                    decision_as_of=_text(row.get("decision_as_of")),
                )
                corrected_error = float(revision["forecast_peak_c"]) - float(bias_state["bias_c"]) - target
                pooled_error += weights[source] * corrected_error
            empirical_pool_errors.append(pooled_error)
        empirical_variance = fmean([(item - fmean(empirical_pool_errors)) ** 2 for item in empirical_pool_errors]) if len(empirical_pool_errors) > 1 else expected_variance
        information_scale = max(0.05, min(1.0, expected_variance / max(expected_variance, empirical_variance)))
        confidence = min(1.0, len(days) / max(float(minimum_event_days_per_surface) * 3.0, 1.0))
        surfaces_by_context[context_key][source_key] = {"schema": SURFACE_SCHEMA_VERSION, "available_source_ids": available, "missing_source_ids": list(rows[0]["missing_source_ids"]), "source_base_weights": weights, "source_error_calibration": calibration, "source_correlation": correlation, "joint_information_scale": information_scale, "information_confidence": confidence, "mode_formation_policy": "emergent_from_bias_corrected_distance_residual_overlap_and_source_correlation", "force_multimodality": False, "force_unimodality": False, "training_event_day_count": len(days), "training_sample_count": len(rows)}
    # Empty is a mathematical identity surface, not an inferred weather model.
    for context_key in sorted({row[0] for row in by_surface}):
        surfaces_by_context[context_key]["empty"] = {"schema": SURFACE_SCHEMA_VERSION, "available_source_ids": [], "missing_source_ids": list(SOURCE_UNIVERSE), "source_base_weights": {}, "source_error_calibration": {}, "source_correlation": {}, "joint_information_scale": 0.0, "information_confidence": 0.0, "training_event_day_count": 0, "training_sample_count": 0}
    # The artifact identity must bind only samples that actually reached this
    # fit.  Reusing a full-dataset manifest in walk-forward evaluation would
    # otherwise make an identical pre-cutoff candidate appear different merely
    # because post-cutoff rows existed nearby.
    training_samples_sha256 = _sha256(
        {"samples": list(samples), "source_catalog": catalog, "training_data_policy": TRAINING_POLICY}
    )
    artifact = {"schema": ARTIFACT_SCHEMA_VERSION, "approval_state": "shadow_only", "source_data_policy": "forecast_only_asof", "training_data_policy": TRAINING_POLICY, "source_catalog": catalog, "bias_correction": bias_correction, "source_set_surfaces": dict(sorted(surfaces_by_context.items())), "training_samples_sha256": training_samples_sha256, "training_excluded_counts": dict(sorted(excluded.items()))}
    artifact["artifact_sha256"] = canonical_artifact_sha256(artifact)
    blockers = [] if surfaces_by_context else ["no_exact_source_set_surface_reached_training_minimum"]
    return {"schema": TRAINING_REPORT_SCHEMA_VERSION, "state": "trained_shadow_only" if not blockers else "blocked", "blockers": blockers, "artifact": artifact if not blockers else None, "surface_count": sum(len(rows) for rows in surfaces_by_context.values()), "training_excluded_counts": dict(sorted(excluded.items())), "promotion": {"approval_required": True, "strict_asof_oos_required_for_promotion": True, "strict_asof_oos_runtime_gate": False, "live_amsc_ingest_blocked": False}, "boundary": {"writes_runtime_artifact_pointer": False, "writes_posterior": False, "places_orders": False}}


__all__ = ["DATASET_SCHEMA_VERSION", "TRAINING_POLICY", "TRAINING_REPORT_SCHEMA_VERSION", "build_joint_weak_training_dataset", "train_joint_weak_source_set_artifact"]
