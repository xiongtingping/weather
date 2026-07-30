"""Project one persisted weak forecast snapshot into read-only fact fields.

The production probability chain already stores the model/TAF inputs in
``weak_forecast_evidence_snapshots``.  This module only copies that immutable
lineage into the public fact surface; it deliberately performs no forecast
calculation, reweighting, or posterior update.
"""

from __future__ import annotations

import json
import math
from typing import Any, Mapping, MutableMapping, Sequence


SCHEMA_VERSION = "production_forecast_snapshot_projection_v1"
_SOURCE_ORDER = ("gfs", "icon", "ecmwf", "taf")
_SOURCE_PREFIX = {
    "gfs": "gfs",
    "icon": "icon",
    "ecmwf": "ecmwf",
    "taf": "taf",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse(value: Any, default: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    try:
        parsed = json.loads(_text(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _compact_revision(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    source_id = _text(raw.get("source_id")).lower()
    if source_id not in _SOURCE_PREFIX:
        return None
    revision_id = _text(raw.get("source_revision_id"))
    peak = _finite(raw.get("forecast_peak_c"))
    if not revision_id or peak is None:
        return None
    row: dict[str, Any] = {
        "source_id": source_id,
        "source_revision_id": revision_id,
        "available_at": _text(raw.get("available_at")),
        "event_date": _text(raw.get("event_date")),
        "forecast_peak_c": peak,
        "forecast_peak_valid_time": _text(raw.get("forecast_peak_valid_time")),
        "forecast_sigma_c": raw.get("forecast_sigma_c"),
        "forecast_weather_regime": _text(raw.get("forecast_weather_regime")) or "unknown",
        "forecast_weather_regime_origin": _text(raw.get("forecast_weather_regime_origin")),
        "source_class": _text(raw.get("source_class")) or "forecast_weak_source",
    }
    return {key: value for key, value in row.items() if value not in (None, "")}


def project_forecast_snapshot_fields(snapshot_row: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a compact, lineage-preserving projection of one weak snapshot."""

    if isinstance(snapshot_row, Mapping):
        snapshot = snapshot_row
    else:
        try:
            snapshot = dict(snapshot_row) if snapshot_row is not None else {}
        except (TypeError, ValueError):
            snapshot = {}
    snapshot_id = _text(snapshot.get("forecast_evidence_snapshot_id"))
    if not snapshot_id:
        return {}
    revisions_raw = _parse(snapshot.get("source_revisions_json"), [])
    revisions: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    if isinstance(revisions_raw, Sequence) and not isinstance(revisions_raw, (str, bytes, bytearray)):
        for raw in revisions_raw:
            if not isinstance(raw, Mapping):
                continue
            row = _compact_revision(raw)
            if row is None or row["source_id"] in seen_sources:
                continue
            seen_sources.add(str(row["source_id"]))
            revisions.append(row)
    revisions.sort(key=lambda row: (_SOURCE_ORDER.index(row["source_id"]), row["source_revision_id"]))

    likelihood_raw = _parse(snapshot.get("likelihood_by_x_json"), {})
    likelihood: dict[str, float] = {}
    if isinstance(likelihood_raw, Mapping):
        for key, value in likelihood_raw.items():
            number = _finite(value)
            temperature = _finite(key)
            if number is None or temperature is None or number < 0.0:
                continue
            likelihood[f"{temperature:g}°C"] = number

    source_revision_ids = _parse(snapshot.get("source_revision_ids_json"), [])
    if not isinstance(source_revision_ids, list):
        source_revision_ids = [row["source_revision_id"] for row in revisions]
    source_revision_ids = [_text(value) for value in source_revision_ids if _text(value)]
    created_at = _text(snapshot.get("created_at"))
    decision_as_of = _text(snapshot.get("decision_as_of"))
    source_set_key = _text(snapshot.get("source_set_key"))

    features: dict[str, Any] = {
        "forecast_evidence_snapshot_id": snapshot_id,
        "forecast_source_set_key": source_set_key,
        "forecast_snapshot_created_at": created_at,
        "forecast_snapshot_decision_as_of": decision_as_of,
        "weather_likelihood_refreshed_at": created_at or decision_as_of,
        "weather_likelihood_feature_quality": "materialized_forecast_weak_snapshot",
        "forecast_weak_snapshot_state": _text(snapshot.get("state")) or "active",
        "forecast_weak_snapshot_source": "archive.db.weak_forecast_evidence_snapshots",
    }
    for row in revisions:
        prefix = _SOURCE_PREFIX[row["source_id"]]
        features[f"{prefix}_forecast_peak_c"] = row["forecast_peak_c"]
        features[f"{prefix}_forecast_peak_value"] = row["forecast_peak_c"]
        if row.get("available_at"):
            features[f"{prefix}_forecast_updated_at"] = row["available_at"]
            features[f"{prefix}_forecast_fetched_at"] = row["available_at"]
            if prefix == "taf":
                features["taf_forecast_peak_issued_at"] = row["available_at"]
        if row.get("forecast_peak_valid_time"):
            features[f"{prefix}_forecast_peak_valid_time"] = row["forecast_peak_valid_time"]
            features[f"{prefix}_forecast_peak_at"] = row["forecast_peak_valid_time"]
            if prefix == "taf":
                features["taf_forecast_peak_valid_start_at"] = row["forecast_peak_valid_time"]

    attribution: dict[str, Any] = {
        "schema": "forecast_weak_attribution_trace_v1",
        "state": "available" if likelihood and revisions else "partial",
        "read_only": True,
        "weak_likelihood_consumed": bool(likelihood),
        "forecast_evidence_snapshot_id": snapshot_id,
        "weak_likelihood_sha256": _text(snapshot.get("weak_likelihood_sha256")),
        "weak_likelihood_artifact_sha256": _text(snapshot.get("weak_likelihood_artifact_sha256")),
        "source_set_key": source_set_key,
        "source_revision_ids": source_revision_ids,
        "source_revisions": revisions,
        "created_at": created_at,
        "decision_as_of": decision_as_of,
        "source": "archive.db.weak_forecast_evidence_snapshots",
        "frontend_recompute_allowed": False,
        "boundaries": {
            "does_not_recompute_or_change_posterior": True,
            "weak_sources_never_hard_lock": True,
            "forecast_only": True,
        },
    }
    return {
        "forecast_weak_likelihood": likelihood,
        "forecast_weak_attribution": attribution,
        "weather_likelihood_features_compact": features,
        "forecast_evidence_snapshot_id": snapshot_id,
    }


def attach_forecast_snapshot_fields(
    public_fact_row: MutableMapping[str, Any],
    snapshot_row: Mapping[str, Any] | None,
) -> MutableMapping[str, Any]:
    """Attach the projection to an existing public fact row without mutating SSOT math."""

    projected = project_forecast_snapshot_fields(snapshot_row)
    if not projected:
        return public_fact_row
    try:
        fact = _parse(public_fact_row.get("fact_json"), {})
    except Exception:
        fact = {}
    if not isinstance(fact, MutableMapping):
        return public_fact_row
    for key, value in projected.items():
        if key == "forecast_evidence_snapshot_id":
            ssot = fact.get("posterior_ssot_fact")
            if isinstance(ssot, MutableMapping):
                ssot.setdefault(key, value)
            continue
        fact[key] = value
    public_fact_row["fact_json"] = json.dumps(fact, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return public_fact_row


__all__ = ["SCHEMA_VERSION", "attach_forecast_snapshot_fields", "project_forecast_snapshot_fields"]
