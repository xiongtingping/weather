"""Read-only, as-of-safe samples for ZUUU daily-high trajectory evaluation.

This module is deliberately separate from fact projection.  It binds a
canonical hard-observation sequence available at one cutoff to a post-event
label, without ever letting that label become an observation feature.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from lineage_identity_runtime import build_training_sample_id
from resolution_bridge_runtime import validate_final_resolution_label
from zuuu_daily_high_candidate_runtime import canonical_observation_snapshot


SCHEMA_VERSION = "trajectory_label_sample_v1"
MANIFEST_SCHEMA_VERSION = "trajectory_label_sample_manifest_v1"
POLICY_VERSION = "trajectory_label_sample_shadow_v1"
INCREMENTAL_BAYES_SAMPLE_SCHEMA_VERSION = "temperature_incremental_bayes_training_sample_v2"
INCREMENTAL_BAYES_MANIFEST_SCHEMA_VERSION = "temperature_incremental_bayes_training_manifest_v2"
INCREMENTAL_BAYES_POLICY_VERSION = "canonical_event_asof_training_v2"
_FEATURE_SURFACES = {
    "canonical_new_observation",
    "observation_innovation",
    "trajectory_path_memory",
    "forecast_evidence",
    "forecast_observation_residuals",
    "prior_features",
    "observed_max_lower_bound_c",
    "time_context",
    "quality_missingness_quantization",
}
_FORBIDDEN_FEATURE_TOKENS = {
    "final_physical_high_c",
    "final_daily_max",
    "settlement_scalar_c",
    "settlement_actual",
    "market_settlement_bucket",
    "market_bucket",
    "label_available_at",
    "label_finality",
    "future_observation",
    "future_delta",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    parsed = _number(value)
    return parsed if parsed is not None and math.isfinite(parsed) else None


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


def _feature_label_leaks(value: Any, *, path: str = "") -> list[str]:
    leaks: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = _text(raw_key).lower()
            child_path = f"{path}.{key}" if path else key
            if key in _FORBIDDEN_FEATURE_TOKENS:
                leaks.append(child_path)
            leaks.extend(_feature_label_leaks(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            leaks.extend(_feature_label_leaks(child, path=f"{path}[{index}]"))
    return leaks


def _canonical_event_sequence(
    *,
    canonical_observation_event: Mapping[str, Any],
    canonical_sequence_event_ids: Sequence[str],
    previous_canonical_event_id: str,
) -> tuple[list[str], list[str]]:
    event_id = _text(canonical_observation_event.get("canonical_observation_event_id"))
    sequence = [_text(value) for value in canonical_sequence_event_ids if _text(value)]
    issues: list[str] = []
    if not event_id:
        issues.append("canonical_observation_event_id_missing")
    if not sequence:
        issues.append("canonical_sequence_missing")
    if len(sequence) != len(set(sequence)):
        issues.append("canonical_sequence_contains_duplicate_event")
    if sequence and sequence[-1] != event_id:
        issues.append("canonical_sequence_does_not_end_at_current_event")
    expected_previous = sequence[-2] if len(sequence) >= 2 else ""
    if _text(previous_canonical_event_id) != expected_previous:
        issues.append("previous_canonical_event_id_mismatch")
    return sequence, issues


def _pre_event_trajectory_feature_issues(
    *,
    trajectory_path_memory: Mapping[str, Any],
    canonical_sequence_event_ids: Sequence[str],
    current_event_id: str,
) -> list[str]:
    """Enforce the same O_<t>/O_<=t split used by the runtime LR path."""

    trajectory = _mapping(trajectory_path_memory)
    issues: list[str] = []
    if _text(trajectory.get("temporal_role")) != "pre_event":
        issues.append("trajectory_path_memory_not_pre_event")
    supplied = trajectory.get("canonical_sequence_event_ids")
    if not isinstance(supplied, list):
        issues.append("trajectory_path_memory_sequence_missing")
        return issues
    trajectory_ids = [_text(value) for value in supplied if _text(value)]
    if len(trajectory_ids) != len(supplied) or len(trajectory_ids) != len(set(trajectory_ids)):
        issues.append("trajectory_path_memory_sequence_invalid")
    if current_event_id and current_event_id in trajectory_ids:
        issues.append("trajectory_path_memory_contains_current_event")
    expected_prefix = [_text(value) for value in canonical_sequence_event_ids[:-1] if _text(value)]
    if trajectory_ids != expected_prefix:
        issues.append("trajectory_path_memory_not_canonical_pre_event_prefix")
    return issues


def _source_revisions_as_of(
    source_revisions: Sequence[Mapping[str, Any]],
    *,
    cutoff_as_of: str,
) -> tuple[list[dict[str, str]], list[str]]:
    cutoff = _parse_time(cutoff_as_of)
    rows: list[dict[str, str]] = []
    issues: list[str] = []
    if cutoff is None:
        return [], ["cutoff_as_of_invalid"]
    for raw in source_revisions:
        item = _mapping(raw)
        revision_id = _text(item.get("source_revision_id"))
        source_id = _text(item.get("source_id"))
        available_at = _text(item.get("available_at"))
        available = _parse_time(available_at)
        if not revision_id or not source_id or available is None:
            issues.append("forecast_source_revision_identity_or_available_at_missing")
            continue
        if available > cutoff:
            issues.append("forecast_source_revision_visible_after_cutoff")
            continue
        rows.append(
            {
                "source_revision_id": revision_id,
                "source_id": source_id,
                "available_at": available_at,
            }
        )
    rows.sort(key=lambda item: (item["source_id"], item["source_revision_id"], item["available_at"]))
    if len({row["source_revision_id"] for row in rows}) != len(rows):
        issues.append("forecast_source_revision_duplicate")
    if not rows:
        issues.append("forecast_source_revisions_missing")
    return rows, sorted(set(issues))


def _sample_label(label: Mapping[str, Any] | None, observed_max_c: float | None) -> dict[str, Any]:
    """Normalize labels while retaining their source and censoring semantics."""

    value = _mapping(label)
    label_kind = str(value.get("label_kind") or "").strip()
    resolution_source = str(value.get("resolution_source") or "").strip()
    final_value = _number(value.get("final_daily_max_c", value.get("settlement_actual")))
    interval = _mapping(value.get("final_daily_max_interval_c"))
    lower = _number(interval.get("lower"))
    upper = _number(interval.get("upper"))
    issues: list[str] = []

    if label_kind not in {"formal_settlement", "resolution_source_wu", "amsc_self_max_diagnostic"}:
        issues.append("label_kind_invalid_or_missing")
    if label_kind == "formal_settlement" and final_value is None:
        issues.append("formal_settlement_value_missing")
    if label_kind == "resolution_source_wu":
        source_text = resolution_source.lower()
        if "wunderground" not in source_text and "weatherunderground" not in source_text and not source_text.startswith("wu"):
            issues.append("resolution_source_wu_not_source_matched")
        if final_value is None and (lower is None or upper is None):
            issues.append("resolution_source_wu_value_or_interval_missing")
    if label_kind == "amsc_self_max_diagnostic" and final_value is None:
        issues.append("amsc_self_max_diagnostic_value_missing")
    if lower is not None and upper is not None and lower > upper:
        issues.append("final_daily_max_interval_invalid")
    if final_value is not None and observed_max_c is not None and final_value < observed_max_c:
        issues.append("final_label_below_observed_max")
    if lower is not None and observed_max_c is not None and lower < observed_max_c:
        issues.append("final_label_interval_below_observed_max")

    remaining_increment: float | None = None
    remaining_increment_interval: dict[str, float] | None = None
    if final_value is not None and observed_max_c is not None:
        remaining_increment = final_value - observed_max_c
    elif lower is not None and upper is not None and observed_max_c is not None:
        remaining_increment_interval = {"lower": lower - observed_max_c, "upper": upper - observed_max_c}
    return {
        "label_kind": label_kind,
        "resolution_source": resolution_source,
        "final_daily_max_label": final_value,
        "final_daily_max_interval_c": {"lower": lower, "upper": upper} if lower is not None and upper is not None else None,
        "remaining_increment_t": remaining_increment,
        "remaining_increment_interval_t": remaining_increment_interval,
        "label_version": str(value.get("label_version") or ""),
        "event_run_id": value.get("event_run_id"),
        "label_provenance": {
            key: value.get(key)
            for key in (
                "label_source_observation_ids",
                "label_source_row_count",
                "label_observed_first_at",
                "label_observed_last_at",
                "label_finality",
            )
            if value.get(key) not in (None, "", [], {})
        },
        "issues": sorted(set(issues)),
    }


def build_trajectory_label_sample(
    *,
    station_id: str,
    event_date: str,
    cutoff_as_of: str,
    source_observations: Iterable[Any] | None,
    label: Mapping[str, Any] | None,
    coverage_manifest: Mapping[str, Any] | None = None,
    fact_snapshot_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build one replay sample using only observations available by its cutoff."""

    snapshot = canonical_observation_snapshot(
        source_observations=source_observations,
        as_of=cutoff_as_of,
        event_date=event_date,
    )
    points = list(snapshot.get("selected_points") or [])
    observed_max = max((float(point["temperature_c"]) for point in points), default=None)
    normalized_label = _sample_label(label, observed_max)
    coverage = _mapping(coverage_manifest)
    sample_issues: list[str] = [*normalized_label["issues"]]
    if str(station_id or "").strip().upper() != "ZUUU":
        sample_issues.append("station_id_not_zuuu")
    if not points:
        sample_issues.append("canonical_metar_sequence_missing_at_cutoff")
    if snapshot.get("quality_flags"):
        sample_issues.extend(f"canonical_quality_flag:{flag}" for flag in snapshot["quality_flags"])
    if not coverage:
        sample_issues.append("daily_coverage_manifest_missing")
    elif coverage.get("event_date") != event_date:
        sample_issues.append("daily_coverage_manifest_event_date_mismatch")
    elif coverage.get("station_id") != str(station_id or "").strip().upper():
        sample_issues.append("daily_coverage_manifest_station_mismatch")
    coverage_snapshot_persisted = coverage.get("coverage_snapshot_persisted", True) is True
    if coverage and not coverage_snapshot_persisted:
        sample_issues.append("daily_coverage_manifest_not_persisted")

    label_kind = normalized_label["label_kind"]
    label_trusted = label_kind in {"formal_settlement", "resolution_source_wu"} and not normalized_label["issues"]
    arrivals_trusted = bool(
        coverage_snapshot_persisted
        and _mapping(coverage.get("replay_eligibility")).get("a_class_as_of_replay_eligible")
    )
    source_lineage_complete = bool(points) and all(
        str(point.get("raw_sha256") or "") and str(point.get("raw_object_ref") or "") and str(point.get("first_seen_at") or "")
        for point in points
    )
    if label_trusted and arrivals_trusted and source_lineage_complete and not sample_issues:
        quality_grade = "A"
        quality_reason = "trusted_label_complete_canonical_trajectory_and_first_seen_lineage"
    elif label_trusted and points:
        quality_grade = "B"
        quality_reason = "trusted_label_but_trajectory_or_arrival_coverage_incomplete"
    else:
        quality_grade = "C"
        quality_reason = "diagnostic_or_label_or_time_contract_not_qualified"

    selected_lineage = [
        {
            "source_observation_id": str(point.get("source_observation_id") or ""),
            "raw_sha256": str(point.get("raw_sha256") or ""),
            "raw_object_ref": str(point.get("raw_object_ref") or ""),
            "verified_metar_valid_time": str(point.get("verified_metar_valid_time") or ""),
            "first_seen_at": str(point.get("first_seen_at") or ""),
            "parser_version": str(point.get("parser_version") or ""),
        }
        for point in points
    ]
    feature_fingerprint = {
        "station_id": str(station_id or "").strip().upper(),
        "event_date": str(event_date or ""),
        "cutoff_as_of": str(snapshot.get("as_of") or cutoff_as_of),
        "canonical_metar_lineage": selected_lineage,
        "observed_today_max_c_at_cutoff": observed_max,
        "coverage_sha256": str(coverage.get("coverage_sha256") or ""),
    }
    sample_id = _canonical_hash(
        {
            "station_id": feature_fingerprint["station_id"],
            "event_date": feature_fingerprint["event_date"],
            "cutoff_as_of": feature_fingerprint["cutoff_as_of"],
        }
    )[:32]
    return {
        "schema": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "sample_id": f"trajlbl_{sample_id}",
        "station_id": feature_fingerprint["station_id"],
        "event_date": feature_fingerprint["event_date"],
        "cutoff_as_of": feature_fingerprint["cutoff_as_of"],
        "canonical_metar_sequence": points,
        "canonical_observation_snapshot": snapshot,
        "observed_today_max_c_at_cutoff": observed_max,
        "label": normalized_label,
        "quality_grade": quality_grade,
        "quality_reason": quality_reason,
        "evaluation_eligible": quality_grade == "A",
        "shadow_diagnostic_eligible": quality_grade in {"B", "C"} and bool(points),
        "source_observation_ids": [item["source_observation_id"] for item in selected_lineage if item["source_observation_id"]],
        "raw_sha256_list": [item["raw_sha256"] for item in selected_lineage if item["raw_sha256"]],
        "raw_object_refs": [item["raw_object_ref"] for item in selected_lineage if item["raw_object_ref"]],
        "fact_snapshot_ids": sorted({str(value) for value in (fact_snapshot_ids or []) if str(value)}),
        "parser_versions": sorted({item["parser_version"] for item in selected_lineage if item["parser_version"]}),
        "coverage_manifest_sha256": str(coverage.get("coverage_sha256") or ""),
        "coverage_manifest_persisted": coverage_snapshot_persisted,
        "feature_hash": _canonical_hash(feature_fingerprint),
        "issues": sorted(set(sample_issues)),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
            "historical_backfill": False,
            "final_label_used_as_feature": False,
        },
    }


def build_trajectory_label_sample_manifest(
    *,
    requested_event_dates: Sequence[str],
    samples: Sequence[Mapping[str, Any]],
    excluded: Sequence[Mapping[str, Any]] | None = None,
    parameter_version: str = "no_fitted_parameters_v1",
) -> dict[str, Any]:
    """Produce a deterministic read-only manifest for a batch of samples."""

    rows = [dict(item) for item in samples if isinstance(item, Mapping)]
    rows.sort(key=lambda item: (str(item.get("event_date") or ""), str(item.get("cutoff_as_of") or ""), str(item.get("sample_id") or "")))
    excluded_rows = [dict(item) for item in (excluded or []) if isinstance(item, Mapping)]
    excluded_rows.sort(key=lambda item: (str(item.get("event_date") or ""), str(item.get("reason") or "")))
    quality_counts: dict[str, int] = {}
    for row in rows:
        grade = str(row.get("quality_grade") or "unknown")
        quality_counts[grade] = quality_counts.get(grade, 0) + 1
    core = {
        "schema": MANIFEST_SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "requested_event_dates": sorted({str(value) for value in requested_event_dates if str(value)}),
        "samples": [
            {
                "sample_id": row.get("sample_id"),
                "event_date": row.get("event_date"),
                "cutoff_as_of": row.get("cutoff_as_of"),
                "feature_hash": row.get("feature_hash"),
                "quality_grade": row.get("quality_grade"),
                "label_kind": _mapping(row.get("label")).get("label_kind"),
            }
            for row in rows
        ],
        "excluded": excluded_rows,
        "parameter_version": str(parameter_version or ""),
    }
    return {
        **core,
        "manifest_sha256": _canonical_hash(core),
        "sample_count": len(rows),
        "evaluation_sample_count": sum(1 for row in rows if row.get("evaluation_eligible") is True),
        "shadow_diagnostic_sample_count": sum(1 for row in rows if row.get("shadow_diagnostic_eligible") is True),
        "quality_counts": dict(sorted(quality_counts.items())),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
            "historical_backfill": False,
        },
    }


def build_temperature_incremental_bayes_training_sample(
    *,
    target_identity: str,
    event_date: str,
    canonical_observation_event: Mapping[str, Any],
    previous_canonical_event_id: str,
    canonical_sequence_event_ids: Sequence[str],
    prior_context_id: str,
    forecast_evidence_snapshot_id: str,
    source_revisions: Sequence[Mapping[str, Any]],
    coverage_manifest: Mapping[str, Any],
    feature_payload: Mapping[str, Any],
    resolution_label: Mapping[str, Any],
    feature_builder_version: str,
    label_builder_version: str,
) -> dict[str, Any]:
    """Build one immutable, canonical-event training sample for the v2 filter.

    This is a data-contract builder only.  It rejects a confirmation/nonaccepted
    event, future forecast revision, label surface in features, or sequence
    ambiguity from A-grade settlement evaluation.  B-grade samples may train
    only physical/path auxiliary heads; they never prove settlement promotion.
    """

    target = _text(target_identity)
    day = _text(event_date)[:10]
    event = _mapping(canonical_observation_event)
    features = _mapping(feature_payload)
    label = _mapping(resolution_label)
    coverage = _mapping(coverage_manifest)
    issues: list[str] = []

    if not target:
        issues.append("target_identity_missing")
    if not day:
        issues.append("event_date_missing")
    if _text(event.get("target_identity")) != target:
        issues.append("canonical_event_target_identity_mismatch")
    if _text(event.get("state")) not in {"accepted_primary", "accepted_substitute"}:
        issues.append("canonical_event_not_accepted")
    station_id = _text(event.get("station_id")).upper()
    if not station_id:
        issues.append("canonical_event_station_id_missing")
    valid_time = _text(event.get("valid_time"))
    if not valid_time or valid_time[:10] != day:
        issues.append("canonical_event_valid_time_event_date_mismatch")
    cutoff_as_of = _text(event.get("first_actionable_seen_at"))
    if _parse_time(cutoff_as_of) is None:
        issues.append("canonical_event_first_actionable_seen_at_invalid")
    sequence, sequence_issues = _canonical_event_sequence(
        canonical_observation_event=event,
        canonical_sequence_event_ids=canonical_sequence_event_ids,
        previous_canonical_event_id=previous_canonical_event_id,
    )
    issues.extend(sequence_issues)

    if not _text(prior_context_id):
        issues.append("prior_context_id_missing")
    if not _text(forecast_evidence_snapshot_id):
        issues.append("forecast_evidence_snapshot_id_missing")
    source_revision_rows, revision_issues = _source_revisions_as_of(
        source_revisions,
        cutoff_as_of=cutoff_as_of,
    )
    issues.extend(revision_issues)

    missing_surfaces = sorted(_FEATURE_SURFACES - set(features))
    unknown_surfaces = sorted(set(features) - _FEATURE_SURFACES)
    if missing_surfaces:
        issues.append(f"feature_surface_missing:{','.join(missing_surfaces)}")
    if unknown_surfaces:
        issues.append(f"feature_surface_unknown:{','.join(unknown_surfaces)}")
    leaks = _feature_label_leaks(features)
    if leaks:
        issues.append(f"label_or_future_surface_in_features:{','.join(sorted(set(leaks)))}")
    issues.extend(
        _pre_event_trajectory_feature_issues(
            trajectory_path_memory=_mapping(features.get("trajectory_path_memory")),
            canonical_sequence_event_ids=sequence,
            current_event_id=_text(event.get("canonical_observation_event_id")),
        )
    )

    coverage_sha256 = _text(coverage.get("coverage_manifest_sha256") or coverage.get("coverage_sha256"))
    if _text(coverage.get("event_date")) != day:
        issues.append("coverage_manifest_event_date_mismatch")
    if _text(coverage.get("station_id")).upper() != station_id:
        issues.append("coverage_manifest_station_id_mismatch")
    if not coverage_sha256:
        issues.append("coverage_manifest_sha256_missing")
    coverage_a_class = bool(
        coverage.get("coverage_snapshot_persisted") is True
        and _mapping(coverage.get("replay_eligibility")).get("a_class_as_of_replay_eligible") is True
    )
    if not coverage_a_class:
        issues.append("coverage_manifest_not_a_class_asof_replay_eligible")

    physical_high_c = _finite(label.get("final_physical_high_c"))
    settlement_scalar_c = _finite(label.get("settlement_scalar_c"))
    market_bucket = _text(label.get("market_settlement_bucket"))
    resolution_contract_id = _text(label.get("resolution_contract_id"))
    label_available_at = _text(label.get("label_available_at"))
    label_available = _parse_time(label_available_at)
    cutoff = _parse_time(cutoff_as_of)
    label_finality = _text(label.get("label_finality"))
    resolution_label_validation = validate_final_resolution_label(label)
    if physical_high_c is None:
        issues.append("final_physical_high_label_missing_or_non_finite")
    if settlement_scalar_c is None:
        issues.append("settlement_scalar_label_missing_or_non_finite")
    if not market_bucket:
        issues.append("market_settlement_bucket_label_missing")
    if not resolution_contract_id:
        issues.append("resolution_contract_id_missing")
    if label_available is None:
        issues.append("label_available_at_missing_or_invalid")
    elif cutoff is not None and label_available <= cutoff:
        issues.append("label_available_at_not_after_cutoff")
    if label_finality != "final":
        issues.append("label_not_final")
    if resolution_label_validation.get("ok") is not True:
        for reason in list(resolution_label_validation.get("block_reasons") or []):
            issues.append(f"resolution_label_bridge_invalid:{reason}")

    structural_prefixes = (
        "target_identity",
        "event_date",
        "canonical_event_",
        "canonical_sequence",
        "previous_canonical",
        "prior_context",
        "forecast_evidence",
        "forecast_source_revision",
        "feature_surface",
        "label_or_future_surface",
        "trajectory_path_memory_",
    )
    structural_ok = not any(issue.startswith(structural_prefixes) for issue in issues)
    physical_label_ok = physical_high_c is not None
    settlement_label_ok = (
        physical_label_ok
        and settlement_scalar_c is not None
        and bool(market_bucket)
        and bool(resolution_contract_id)
        and label_available is not None
        and cutoff is not None
        and label_available > cutoff
        and label_finality == "final"
        and resolution_label_validation.get("ok") is True
    )
    if structural_ok and physical_label_ok and settlement_label_ok and coverage_a_class:
        quality_grade = "A"
        quality_reason = "canonical_event_asof_evidence_and_final_resolution_label_closed"
    elif structural_ok and physical_label_ok:
        quality_grade = "B"
        quality_reason = "physical_label_usable_but_settlement_or_arrival_closure_incomplete"
    else:
        quality_grade = "C"
        quality_reason = "canonical_event_or_feature_or_label_contract_blocked"

    canonical_sequence_sha256 = _canonical_hash(sequence)
    identity_payload = {
        "policy_version": INCREMENTAL_BAYES_POLICY_VERSION,
        "target_identity": target,
        "event_date": day,
        "canonical_observation_event_id": _text(event.get("canonical_observation_event_id")),
        "previous_canonical_event_id": _text(previous_canonical_event_id),
        "canonical_sequence_sha256": canonical_sequence_sha256,
        "prior_context_id": _text(prior_context_id),
        "forecast_evidence_snapshot_id": _text(forecast_evidence_snapshot_id),
        "source_revision_ids": source_revision_rows,
        "coverage_manifest_sha256": coverage_sha256,
        "feature_builder_version": _text(feature_builder_version),
        "label_builder_version": _text(label_builder_version),
        "resolution_label": dict(label),
    }
    sample_contract_sha256 = _canonical_hash(identity_payload)
    sample_id = build_training_sample_id(
        target_identity=target,
        canonical_observation_event_id=_text(event.get("canonical_observation_event_id")),
        cutoff_as_of=cutoff_as_of,
        artifact_sha256=sample_contract_sha256,
    )
    labels = {
        "final_physical_high_c": physical_high_c,
        "strict_asof_training_eligible": label.get("strict_asof_training_eligible") is True,
        "settlement_scalar_c": settlement_scalar_c,
        "market_settlement_bucket": market_bucket,
        "resolution_contract_id": resolution_contract_id,
        "label_available_at": label_available_at,
        "label_finality": label_finality,
        "label_provenance": _mapping(label.get("label_provenance")),
    }
    return {
        "schema": INCREMENTAL_BAYES_SAMPLE_SCHEMA_VERSION,
        "policy_version": INCREMENTAL_BAYES_POLICY_VERSION,
        "training_sample_id": sample_id,
        "target_identity": target,
        "station_id": station_id,
        "event_date": day,
        "cutoff_as_of": cutoff_as_of,
        "canonical_observation_event_id": _text(event.get("canonical_observation_event_id")),
        "previous_canonical_event_id": _text(previous_canonical_event_id),
        "canonical_sequence_sha256": canonical_sequence_sha256,
        "canonical_sequence_event_ids": sequence,
        "prior_context_id": _text(prior_context_id),
        "forecast_evidence_snapshot_id": _text(forecast_evidence_snapshot_id),
        "source_revisions": source_revision_rows,
        "coverage_manifest_sha256": coverage_sha256,
        "feature_builder_version": _text(feature_builder_version),
        "label_builder_version": _text(label_builder_version),
        "sample_contract_sha256": sample_contract_sha256,
        "features": dict(features),
        "labels": labels,
        "quality_grade": quality_grade,
        "quality_reason": quality_reason,
        "training_eligible": quality_grade in {"A", "B"},
        "settlement_evaluation_eligible": quality_grade == "A",
        "physical_auxiliary_eligible": quality_grade in {"A", "B"},
        "day_weight": None,
        "issues": sorted(set(issues)),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
            "final_label_used_as_feature": False,
            "future_observation_used_as_feature": False,
            "random_event_split_allowed": False,
        },
    }


def assign_temperature_incremental_event_day_weights(
    samples: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Assign 1/N weights per target/day without mutating source samples."""

    rows = [dict(sample) for sample in samples if isinstance(sample, Mapping)]
    eligible_by_day: dict[tuple[str, str], list[dict[str, Any]]] = {}
    seen_event_keys: set[tuple[str, str, str]] = set()
    for row in rows:
        if not bool(row.get("training_eligible")):
            continue
        key = (_text(row.get("target_identity")), _text(row.get("event_date")))
        event_key = (*key, _text(row.get("canonical_observation_event_id")))
        if not all(event_key):
            raise ValueError("training_sample_weight_identity_missing")
        if event_key in seen_event_keys:
            raise ValueError("training_sample_duplicate_canonical_event")
        seen_event_keys.add(event_key)
        eligible_by_day.setdefault(key, []).append(row)
    output: list[dict[str, Any]] = []
    for row in rows:
        cloned = dict(row)
        group = eligible_by_day.get((_text(row.get("target_identity")), _text(row.get("event_date"))), [])
        cloned["day_weight"] = 1.0 / len(group) if bool(row.get("training_eligible")) and group else None
        output.append(cloned)
    return output


def build_temperature_incremental_expanding_year_folds(
    samples: Sequence[Mapping[str, Any]],
    *,
    eligibility_field: str = "settlement_evaluation_eligible",
) -> dict[str, Any]:
    """Return strict event-day/year expanding folds; never randomizes events."""

    eligible = [
        dict(sample)
        for sample in samples
        if isinstance(sample, Mapping) and bool(sample.get(eligibility_field))
    ]
    by_year: dict[int, list[dict[str, Any]]] = {}
    for sample in eligible:
        event_day = _text(sample.get("event_date"))
        try:
            year = int(event_day[:4])
        except ValueError as exc:
            raise ValueError("training_sample_event_date_invalid") from exc
        by_year.setdefault(year, []).append(sample)
    years = sorted(by_year)
    folds: list[dict[str, Any]] = []
    for test_year in years[1:]:
        train = [sample for year in years if year < test_year for sample in by_year[year]]
        test = list(by_year[test_year])
        train_days = {_text(sample.get("event_date")) for sample in train}
        test_days = {_text(sample.get("event_date")) for sample in test}
        overlap = sorted(train_days & test_days)
        if overlap:
            raise ValueError("training_sample_event_day_split_leakage")
        folds.append(
            {
                "test_year": test_year,
                "train_through_year": test_year - 1,
                "train_event_dates": sorted(train_days),
                "test_event_dates": sorted(test_days),
                "train_training_sample_ids": sorted(_text(sample.get("training_sample_id")) for sample in train),
                "test_training_sample_ids": sorted(_text(sample.get("training_sample_id")) for sample in test),
                "event_day_disjoint": True,
            }
        )
    core = {
        "schema": INCREMENTAL_BAYES_MANIFEST_SCHEMA_VERSION,
        "policy_version": INCREMENTAL_BAYES_POLICY_VERSION,
        "eligibility_field": eligibility_field,
        "years": years,
        "folds": folds,
        "random_split": False,
        "same_event_day_cross_fold": False,
    }
    return {**core, "manifest_sha256": _canonical_hash(core)}


__all__ = [
    "INCREMENTAL_BAYES_MANIFEST_SCHEMA_VERSION",
    "INCREMENTAL_BAYES_POLICY_VERSION",
    "INCREMENTAL_BAYES_SAMPLE_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "POLICY_VERSION",
    "SCHEMA_VERSION",
    "assign_temperature_incremental_event_day_weights",
    "build_temperature_incremental_bayes_training_sample",
    "build_temperature_incremental_expanding_year_folds",
    "build_trajectory_label_sample",
    "build_trajectory_label_sample_manifest",
]
