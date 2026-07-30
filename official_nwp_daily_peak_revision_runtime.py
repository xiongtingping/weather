"""Build one forecast-only daily-peak revision from immutable NWP point snapshots."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from official_nwp_source_catalog_runtime import NORMALIZED_SNAPSHOT_SCHEMA
from official_nwp_physical_regime_runtime import (
    aggregate_official_nwp_physical_regime,
    derive_official_nwp_physical_regime,
)
from weak_likelihood_source_set_runtime import SOURCE_UNIVERSE


SCHEMA_VERSION = "official_nwp_daily_peak_forecast_revision_v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _payload_hashes(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    value = snapshot.get("raw_payload_sha256s")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    rows = tuple(_text(item) for item in value)
    return rows if rows and all(rows) else ()


def _same_provider_payload(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Only collapse a re-fetch when direct provider-body identity agrees."""

    left_hashes, right_hashes = _payload_hashes(left), _payload_hashes(right)
    return bool(left_hashes and left_hashes == right_hashes and _mapping(left.get("normalized_features")) == _mapping(right.get("normalized_features")))


def _safe_normalized_field_superset(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    """Return the preferred side when one capture only adds direct fields.

    1 prefers left, 2 prefers right, and 0 means the two captures conflict.
    Shared normalized values must be identical; a changed forecast value is
    never hidden by a decoder-schema upgrade.
    """

    left_features, right_features = _mapping(left.get("normalized_features")), _mapping(right.get("normalized_features"))
    shared = set(left_features) & set(right_features)
    if not shared or any(left_features[key] != right_features[key] for key in shared):
        return 0
    left_keys, right_keys = set(left_features), set(right_features)
    if left_keys < right_keys:
        return 2
    if right_keys < left_keys:
        return 1
    return 0


def _tier_a_transport(snapshot: Mapping[str, Any]) -> bool:
    requested_at = _time(snapshot.get("requested_at"))
    response_started_at = _time(snapshot.get("response_started_at"))
    response_completed_at = _time(snapshot.get("response_completed_at"))
    return (
        snapshot.get("response_headers_observed") is True
        and requested_at is not None
        and response_started_at is not None
        and response_completed_at is not None
        and requested_at <= response_started_at <= response_completed_at
    )


def build_official_nwp_daily_peak_revision(
    *, provider_id: str, point_snapshots: Sequence[Mapping[str, Any]], target_event_date: str, target_timezone: str, expected_valid_times_utc: Sequence[str]
) -> dict[str, Any]:
    """Require one run and an exact target-day point set; no interpolation or backfill."""

    provider = _text(provider_id)
    try:
        timezone = ZoneInfo(_text(target_timezone))
    except Exception:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["target_timezone_invalid"]}
    if provider not in SOURCE_UNIVERSE or provider not in {"ecmwf", "icon", "gfs", "jma"}:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["official_nwp_provider_not_in_weak_source_universe"]}
    expected = [_text(value) for value in expected_valid_times_utc]
    if not expected or len(set(expected)) != len(expected) or any(_time(value) is None for value in expected):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["expected_valid_times_missing_or_invalid"]}
    selected: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    for raw in point_snapshots:
        snapshot = _mapping(raw)
        if snapshot.get("schema") != NORMALIZED_SNAPSHOT_SCHEMA or _text(snapshot.get("provider_id")) != provider:
            reasons.append("official_nwp_point_snapshot_identity_invalid")
            continue
        valid_at = _time(snapshot.get("valid_time_utc"))
        run_at = _time(snapshot.get("run_at_utc"))
        completed_at = _time(snapshot.get("response_completed_at"))
        temperature = _mapping(snapshot.get("normalized_features")).get("temperature_2m_c")
        try:
            temperature_c = float(temperature)
        except (TypeError, ValueError):
            reasons.append("official_nwp_point_temperature_missing_or_invalid")
            continue
        if valid_at is None or run_at is None or completed_at is None or not _text(snapshot.get("raw_sha256")):
            reasons.append("official_nwp_point_lineage_missing")
            continue
        valid_key = valid_at.isoformat().replace("+00:00", "Z")
        if valid_key not in expected:
            continue
        if valid_at.astimezone(timezone).date().isoformat() != _text(target_event_date)[:10]:
            reasons.append("official_nwp_point_target_local_date_mismatch")
            continue
        old = selected.get(valid_key)
        if old is not None and _text(old.get("snapshot_sha256")) != _text(snapshot.get("snapshot_sha256")):
            old_tier_a, new_tier_a = _tier_a_transport(old), _tier_a_transport(snapshot)
            if old_tier_a != new_tier_a and _mapping(old.get("normalized_features")) == _mapping(snapshot.get("normalized_features")):
                # A later capture can improve transport provenance without
                # changing provider data. It becomes visible only at its own
                # later availability; the older revision remains immutable.
                selected[valid_key] = old if old_tier_a else snapshot
                continue
            if _same_provider_payload(old, snapshot):
                # A later retry of byte-identical official data contains no
                # new forecast information.  Preserve the earliest observed
                # availability, which is the only valid as-of revision.
                old_completed, new_completed = _time(old.get("response_completed_at")), _time(snapshot.get("response_completed_at"))
                if new_completed is not None and (old_completed is None or new_completed < old_completed):
                    selected[valid_key] = snapshot
                continue
            superset = _safe_normalized_field_superset(old, snapshot)
            if superset:
                selected[valid_key] = old if superset == 1 else snapshot
                continue
            reasons.append("official_nwp_point_duplicate_valid_time_conflict")
            continue
        selected[valid_key] = snapshot
    missing = [valid for valid in expected if valid not in selected]
    if missing or reasons:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set([*reasons, "official_nwp_daily_curve_expected_valid_times_missing"])), "missing_valid_times_utc": missing}
    rows = [selected[valid] for valid in expected]
    runs = {_text(row.get("run_at_utc")) for row in rows}
    if len(runs) != 1:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["official_nwp_daily_curve_mixed_model_runs"]}
    temperatures = [float(_mapping(row.get("normalized_features"))["temperature_2m_c"]) for row in rows]
    all_tier_a = all(_tier_a_transport(row) for row in rows)
    raw_hashes = [_text(row.get("raw_sha256")) for row in rows]
    raw_payload_hashes = [_payload_hashes(row) for row in rows]
    snapshot_hashes = [_text(row.get("snapshot_sha256")) for row in rows]
    raw_refs = [_text(row.get("raw_object_ref")) for row in rows]
    completed = max(_time(row.get("response_completed_at")) for row in rows)
    assert completed is not None
    identity = {
        "provider_id": provider,
        "run_at_utc": next(iter(runs)),
        "target_event_date": _text(target_event_date)[:10],
        "expected_valid_times_utc": expected,
        "snapshot_sha256s": snapshot_hashes,
        # Regime enrichment changes the immutable derived-object contract even
        # when the underlying provider snapshots are identical.
        "physical_regime_schema": "official_nwp_daily_physical_regime_v2",
        "temperature_curve_weather_type_inference_forbidden": True,
    }
    point_regimes = [
        derive_official_nwp_physical_regime(_mapping(row.get("normalized_features")))
        for row in rows
    ]
    curve = [
        {
            "valid_time_utc": valid,
            "temperature_c": temperature,
            "snapshot_sha256": snapshot_hash,
            "physical_regime": regime,
            "basin_regime": regime["basin_regime"],
            "basin_regime_confidence": regime["basin_regime_confidence"],
        }
        for valid, temperature, snapshot_hash, regime in zip(
            expected, temperatures, snapshot_hashes, point_regimes
        )
    ]
    daily_regime = aggregate_official_nwp_physical_regime(point_regimes)
    peak_valid_times = [point["valid_time_utc"] for point in curve if point["temperature_c"] == max(temperatures)]
    aggregate_raw_sha256 = _sha256({"raw_sha256s": raw_hashes, "raw_payload_sha256s": raw_payload_hashes, "snapshot_sha256s": snapshot_hashes})
    source_mode = {
        "mode_id": "deterministic_official_curve",
        "mode_probability": 1.0,
        "forecast_peak_c": max(temperatures),
        "forecast_sigma_c": None,
        "forecast_peak_valid_time": min(peak_valid_times),
        "hourly_curve": curve,
        "physical_regime": daily_regime,
        "forecast_weather_regime": daily_regime["forecast_weather_regime"],
        "mode_origin": "official_complete_daily_curve",
    }
    return {
        "schema": SCHEMA_VERSION, "ok": True, "state": "available", "source_id": provider,
        "source_revision_id": f"nwprev_{_sha256(identity)[:32]}", "source_class": "forecast_weak_source",
        "event_date": _text(target_event_date)[:10], "available_at": completed.isoformat().replace("+00:00", "Z"),
        "forecast_peak_c": max(temperatures), "forecast_peak_valid_time": min(peak_valid_times),
        "forecast_sigma_c": None, "forecast_value_kind": "daily_peak_from_complete_nwp_point_curve",
        "run_at_utc": next(iter(runs)), "expected_valid_times_utc": expected, "hourly_curve": curve,
        "source_modes_schema": "forecast_source_modes_v1", "source_modes": [source_mode],
        "physical_regime": daily_regime, "forecast_weather_regime": daily_regime["forecast_weather_regime"],
        "forecast_weather_regime_origin": daily_regime["forecast_weather_regime_origin"],
        "basin_regime": daily_regime["basin_regime"], "basin_regime_confidence": daily_regime["basin_regime_confidence"],
        "basin_regime_source": daily_regime["basin_regime_source"], "raw_sha256": aggregate_raw_sha256,
        "raw_sha256s": raw_hashes, "raw_payload_sha256s": [list(item) for item in raw_payload_hashes],
        "raw_object_refs": raw_refs, "snapshot_sha256s": snapshot_hashes, "strict_asof_tier_a": all_tier_a,
        "response_headers_observed": all_tier_a, "trajectory_eligible": True,
        "tier_a_na_reason": "" if all_tier_a else "one_or_more_point_snapshot_header_arrival_evidence_missing",
        "boundary": {"interpolation_or_backfill_used": False, "mixed_runs_used": False,
                     "market_or_observation_input_consumed": False,
                     "temperature_curve_weather_type_inference_forbidden": True,
                     "physical_regime_uses_direct_physical_fields_only": True,
                     "accumulated_precipitation_point_localization_forbidden": True,
                     "writes_posterior": False, "places_orders": False},
    }


__all__ = ["SCHEMA_VERSION", "build_official_nwp_daily_peak_revision"]
