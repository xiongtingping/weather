"""Materialise exact weak-likelihood surfaces for auditable live replacement."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping


SCHEMA_VERSION = "weak_forecast_evidence_snapshot_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def build_weak_forecast_evidence_snapshot_row(
    weak_envelope: Mapping[str, Any],
    *,
    target_identity: str,
    event_date: str,
    decision_as_of: str,
    state: str = "active",
) -> dict[str, Any]:
    """Freeze the exact approved weak surface used by one posterior version."""

    envelope = _mapping(weak_envelope)
    weak = _mapping(envelope.get("joint_weak_likelihood"))
    evidence = _mapping(envelope.get("forecast_evidence"))
    if envelope.get("ok") is not True or weak.get("ok") is not True:
        raise ValueError("weak_forecast_evidence_not_available")
    if state not in {"active", "superseded"}:
        raise ValueError("weak_forecast_evidence_snapshot_state_invalid")
    snapshot_id = _text(weak.get("forecast_evidence_snapshot_id"))
    artifact_sha = _text(weak.get("artifact_sha256"))
    likelihood_sha = _text(weak.get("joint_forecast_weak_likelihood_sha256"))
    source_set_key = _text(_mapping(weak.get("weak_source_set")).get("source_set_key"))
    rows = list(weak.get("joint_forecast_weak_likelihood") or [])
    likelihood_by_x: dict[str, float] = {}
    temperature_grid: list[float] = []
    for raw in rows:
        row = _mapping(raw)
        temperature = float(row.get("temperature_c"))
        likelihood = float(row.get("likelihood"))
        if not math.isfinite(temperature) or not math.isfinite(likelihood) or likelihood <= 0.0:
            raise ValueError("weak_forecast_likelihood_surface_invalid")
        key = f"{temperature:g}"
        if key in likelihood_by_x:
            raise ValueError("weak_forecast_likelihood_temperature_duplicate")
        temperature_grid.append(temperature)
        likelihood_by_x[key] = likelihood
    source_revisions = [dict(row) for row in evidence.get("source_revisions") or [] if isinstance(row, Mapping)]
    source_revision_ids = [_text(row.get("source_revision_id")) for row in source_revisions]
    if (
        not snapshot_id
        or not artifact_sha
        or not likelihood_sha
        or not source_set_key
        or not likelihood_by_x
        or any(not value for value in source_revision_ids)
    ):
        raise ValueError("weak_forecast_evidence_snapshot_identity_missing")
    if _sha([[temperature, likelihood_by_x[f"{temperature:g}"]] for temperature in temperature_grid]) != likelihood_sha:
        raise ValueError("weak_forecast_likelihood_surface_sha_mismatch")
    snapshot = {
        "schema": SCHEMA_VERSION,
        "target_identity": _text(target_identity),
        "event_date": _text(event_date)[:10],
        "decision_as_of": _text(decision_as_of),
        "forecast_evidence_snapshot_id": snapshot_id,
        "weak_likelihood_artifact_sha256": artifact_sha,
        "weak_likelihood_sha256": likelihood_sha,
        "source_set_key": source_set_key,
        "source_revision_ids": source_revision_ids,
        "source_revisions": source_revisions,
        "temperature_grid_c": temperature_grid,
        "likelihood_by_x": likelihood_by_x,
        "forecast_peak_timing_prior": dict(_mapping(weak.get("forecast_peak_timing_prior"))),
        "forecast_background_context": dict(_mapping(weak.get("forecast_background_context"))),
        "source_expert_trace": [
            dict(row) for row in weak.get("source_expert_trace") or [] if isinstance(row, Mapping)
        ],
        "forecast_multimodality_trace": dict(_mapping(weak.get("forecast_multimodality_trace"))),
        "weak_model_diagnostics": {
            key: weak.get(key)
            for key in (
                "formula",
                "context_key",
                "correlation_discount",
                "joint_information_scale",
                "applied_information_scale",
                "weak_information_confidence",
                "weak_evidence_state",
                "mode_formation_policy",
                "force_multimodality",
                "force_unimodality",
                "source_weight_attribution",
            )
            if weak.get(key) not in (None, "", [], {})
        },
        "forecast_only": True,
        "hard_observation_eligible": False,
    }
    if not snapshot["target_identity"] or not snapshot["event_date"] or not snapshot["decision_as_of"]:
        raise ValueError("weak_forecast_evidence_snapshot_target_or_time_missing")
    return {
        "forecast_evidence_snapshot_id": snapshot_id,
        "target_identity": snapshot["target_identity"],
        "event_date": snapshot["event_date"],
        "decision_as_of": snapshot["decision_as_of"],
        "weak_likelihood_artifact_sha256": artifact_sha,
        "weak_likelihood_sha256": likelihood_sha,
        "source_set_key": source_set_key,
        "source_revision_ids_json": _json(source_revision_ids),
        "source_revisions_json": _json(source_revisions),
        "temperature_grid_json": _json(temperature_grid),
        "likelihood_by_x_json": _json(likelihood_by_x),
        "forecast_peak_timing_prior_json": _json(snapshot["forecast_peak_timing_prior"]),
        "forecast_background_context_json": _json(snapshot["forecast_background_context"]),
        "source_expert_trace_json": _json(snapshot["source_expert_trace"]),
        "forecast_multimodality_trace_json": _json(snapshot["forecast_multimodality_trace"]),
        "weak_model_diagnostics_json": _json(snapshot["weak_model_diagnostics"]),
        "snapshot_json": _json(snapshot),
        "snapshot_sha256": _sha(snapshot),
        "state": state,
        "created_at": snapshot["decision_as_of"],
    }


__all__ = ["SCHEMA_VERSION", "build_weak_forecast_evidence_snapshot_row"]
