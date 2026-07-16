"""Build an as-of, physical-X-only station-day climate prior context.

This is deliberately separate from the legacy market-bucket prior.  Its input
labels are only completed AMSC physical daily maxima (forward final-X sources
or complete historical AMSC sequences) available before the decision cutoff.
Settlement S, market buckets, prices, forecasts, and D0 observations are not
accepted as substitutes for physical X.
"""

from __future__ import annotations

import hashlib
import json
import math
import csv
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "physical_temperature_prior_context_v1"
CONTINUOUS_SCHEMA_VERSION = "continuous_temperature_climate_prior_v1"
POLICY_VERSION = "physical_x_station_day_climate_prior_v1"
ALLOWED_SOURCE_KINDS = frozenset({"amsc_physical_final", "amsc_historical_complete"})
NCEI_REFERENCE_POLICY_VERSION = "ncei_ghcnh_hourly_climate_reference_prior_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(_text(value)[:10])
    except ValueError:
        return None


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _distance_day_of_year(left: date, right: date) -> int:
    raw = abs(int(left.strftime("%j")) - int(right.strftime("%j")))
    return min(raw, 366 - raw)


def _blocked(*reasons: str, diagnostics: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted(set(reason for reason in reasons if reason)),
        "diagnostics": dict(diagnostics or {}),
        "read_only": True,
        "shadow_only": True,
        "boundary": {
            "physical_x_only": True,
            "settlement_s_or_market_bucket_not_accepted": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def build_physical_temperature_prior_context(
    *,
    event_date: str,
    decision_as_of: str,
    temperature_grid_c: Sequence[Any],
    physical_x_records: Sequence[Mapping[str, Any]],
    station_id: str = "ZUUU",
    basin_context: str = "sichuan_basin_zuuu",
    seasonal_window_days: int = 15,
    minimum_samples: int = 30,
    kernel_bandwidth_c: float = 1.0,
) -> dict[str, Any]:
    """Return a content-addressed physical-X prior, or explicit insufficiency.

    The caller supplies an explicit physical temperature grid and bandwidth so
    no market bucket boundaries or hidden defaults influence this prior.
    """

    target_day = _day(event_date)
    as_of = _time(decision_as_of)
    station = _text(station_id).upper()
    grid = sorted({_finite(value) for value in temperature_grid_c if _finite(value) is not None})
    bandwidth = _finite(kernel_bandwidth_c)
    issues: list[str] = []
    if target_day is None:
        issues.append("physical_prior_event_date_invalid")
    if as_of is None:
        issues.append("physical_prior_decision_asof_invalid")
    if not station:
        issues.append("physical_prior_station_id_missing")
    if len(grid) < 2:
        issues.append("physical_prior_temperature_grid_invalid")
    if bandwidth is None or bandwidth <= 0.0:
        issues.append("physical_prior_kernel_bandwidth_invalid")
    if int(seasonal_window_days) < 0:
        issues.append("physical_prior_seasonal_window_invalid")
    if int(minimum_samples) < 1:
        issues.append("physical_prior_minimum_samples_invalid")
    if issues:
        return _blocked(*issues)
    assert target_day is not None and as_of is not None and bandwidth is not None

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for raw in physical_x_records:
        row = dict(raw) if isinstance(raw, Mapping) else {}
        row_day = _day(row.get("event_date"))
        available = _time(row.get("available_at"))
        value = _finite(row.get("final_physical_high_c"))
        source_kind = _text(row.get("source_kind"))
        raw_sha = _text(row.get("raw_sha256"))
        reason = ""
        if source_kind not in ALLOWED_SOURCE_KINDS:
            reason = "physical_prior_record_source_kind_invalid"
        elif _text(row.get("station_id")).upper() != station:
            reason = "physical_prior_record_station_mismatch"
        elif row_day is None or row_day >= target_day:
            reason = "physical_prior_record_not_strictly_before_target_day"
        elif available is None or available > as_of:
            reason = "physical_prior_record_not_available_asof"
        elif value is None:
            reason = "physical_prior_record_final_x_invalid"
        elif len(raw_sha) != 64 or any(ch not in "0123456789abcdef" for ch in raw_sha.lower()):
            reason = "physical_prior_record_raw_sha256_invalid"
        elif _distance_day_of_year(row_day, target_day) > int(seasonal_window_days):
            reason = "physical_prior_record_outside_seasonal_window"
        if reason:
            rejected.append({"event_date": _text(row.get("event_date"))[:10], "reason": reason})
            continue
        accepted.append({
            "event_date": row_day.isoformat(),
            "available_at": available.isoformat().replace("+00:00", "Z"),
            "final_physical_high_c": value,
            "raw_sha256": raw_sha,
            "source_kind": source_kind,
        })
    accepted.sort(key=lambda row: (row["event_date"], row["available_at"], row["raw_sha256"]))
    if len(accepted) < int(minimum_samples):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "insufficient_history",
            "block_reasons": ["physical_prior_minimum_physical_x_samples_not_met"],
            "diagnostics": {
                "accepted_physical_x_sample_count": len(accepted),
                "minimum_samples": int(minimum_samples),
                "rejected_counts": {reason: sum(1 for row in rejected if row["reason"] == reason) for reason in sorted({row["reason"] for row in rejected})},
            },
            "read_only": True,
            "shadow_only": True,
            "boundary": {
                "physical_x_only": True,
                "settlement_s_or_market_bucket_not_accepted": True,
                "writes_posterior": False,
                "places_orders": False,
            },
        }

    masses = []
    for temperature in grid:
        score = sum(math.exp(-0.5 * ((temperature - row["final_physical_high_c"]) / bandwidth) ** 2) for row in accepted)
        masses.append(score)
    total = sum(masses)
    if total <= 0.0:
        return _blocked("physical_prior_kernel_mass_invalid")
    continuous_grid = [
        {"temp_c": temperature, "mass": mass / total}
        for temperature, mass in zip(grid, masses)
    ]
    core = {
        "schema": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "event_date": target_day.isoformat(),
        "decision_as_of": as_of.isoformat().replace("+00:00", "Z"),
        "station_id": station,
        "basin_context": _text(basin_context),
        "seasonal_window_days": int(seasonal_window_days),
        "kernel_bandwidth_c": bandwidth,
        "physical_x_records": accepted,
        "continuous_distribution": {
            "schema": CONTINUOUS_SCHEMA_VERSION,
            "grid": continuous_grid,
            "source": "asof_completed_amsc_physical_x_history",
        },
    }
    artifact_sha = _sha(core)
    prior_model_artifact_sha = _sha({
        "schema": "physical_temperature_prior_model_v1",
        "policy_version": POLICY_VERSION,
        "station_id": station,
        "basin_context": _text(basin_context),
        "seasonal_window_days": int(seasonal_window_days),
        "minimum_samples": int(minimum_samples),
        "kernel_bandwidth_c": bandwidth,
        "temperature_grid_c": grid,
        "allowed_source_kinds": sorted(ALLOWED_SOURCE_KINDS),
    })
    return {
        **core,
        "artifact_sha256": artifact_sha,
        "prior_model_artifact_sha256": prior_model_artifact_sha,
        "prior_context_id": f"physicalprior_{artifact_sha[:24]}",
        "ok": True,
        "state": "available_shadow_only",
        "full_p0_checkpoint_ready": False,
        "full_p0_na_reason": "latent_state_prior_not_yet_trained_or_approved",
        "read_only": True,
        "shadow_only": True,
        "boundary": {
            "physical_x_only": True,
            "settlement_s_or_market_bucket_not_accepted": True,
            "d0_observation_not_consumed": True,
            "forecast_not_consumed": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def build_ncei_operational_climate_reference_prior_context(
    *,
    event_date: str,
    decision_as_of: str,
    temperature_grid_c: Sequence[Any],
    daily_labels_path: Path | str,
    download_manifest_path: Path | str,
    station_id: str = "ZUUU",
    basin_context: str = "sichuan_basin_zuuu",
    seasonal_window_days: int = 15,
    minimum_samples: int = 30,
    kernel_bandwidth_c: float = 1.25,
) -> dict[str, Any]:
    """Build a disclosed NCEI climate-reference baseline for P0.

    NCEI hourly maxima are not relabelled as AMSC final-X. They are prior-only
    climate evidence with an inflated kernel; AMSC remains the final-X label
    authority for daily learning and replacement artifacts.
    """

    target_day = _day(event_date)
    as_of = _time(decision_as_of)
    station = _text(station_id).upper()
    grid = sorted({_finite(value) for value in temperature_grid_c if _finite(value) is not None})
    bandwidth = _finite(kernel_bandwidth_c)
    labels_file = Path(daily_labels_path).expanduser().resolve()
    manifest_file = Path(download_manifest_path).expanduser().resolve()
    issues: list[str] = []
    try:
        manifest_bytes = manifest_file.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        manifest_bytes, manifest = b"", {}
        issues.append("ncei_climate_reference_manifest_unreadable")
    completed_at = _time(manifest.get("download_completed_at")) if isinstance(manifest, Mapping) else None
    station_identity = manifest.get("station_identity") if isinstance(manifest, Mapping) and isinstance(manifest.get("station_identity"), Mapping) else {}
    if manifest.get("schema") != "ncei_zuuu_ghcnh_research_download_manifest_v1":
        issues.append("ncei_climate_reference_manifest_schema_invalid")
    if _text(station_identity.get("icao")).upper() != station:
        issues.append("ncei_climate_reference_station_identity_mismatch")
    if as_of is None or completed_at is None or completed_at > as_of:
        issues.append("ncei_climate_reference_not_available_asof")
    if target_day is None or len(grid) < 2 or bandwidth is None or bandwidth <= 0.0:
        issues.append("ncei_climate_reference_prior_configuration_invalid")
    try:
        labels_bytes = labels_file.read_bytes()
    except OSError:
        labels_bytes = b""
        issues.append("ncei_climate_reference_daily_labels_unreadable")
    if issues:
        return _blocked(*issues)
    assert target_day is not None and as_of is not None and completed_at is not None and bandwidth is not None

    accepted: list[dict[str, Any]] = []
    try:
        rows = csv.DictReader(labels_bytes.decode("utf-8").splitlines())
        for row in rows:
            row_day = _day(row.get("event_date_local"))
            value = _finite(row.get("final_daily_max_c"))
            if (
                _text(row.get("complete_for_daily_max_label")).lower() != "true"
                or row_day is None
                or row_day >= target_day
                or value is None
                or _distance_day_of_year(row_day, target_day) > int(seasonal_window_days)
            ):
                continue
            accepted.append({"event_date": row_day.isoformat(), "ncei_hourly_daily_max_c": value})
    except (UnicodeDecodeError, csv.Error):
        return _blocked("ncei_climate_reference_daily_labels_invalid")
    accepted.sort(key=lambda row: row["event_date"])
    if len(accepted) < int(minimum_samples):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "insufficient_history",
            "block_reasons": ["ncei_climate_reference_minimum_samples_not_met"],
            "diagnostics": {"accepted_sample_count": len(accepted), "minimum_samples": int(minimum_samples)},
            "read_only": True,
            "shadow_only": True,
            "boundary": {"writes_posterior": False, "places_orders": False},
        }
    scores = [
        sum(math.exp(-0.5 * ((temperature - row["ncei_hourly_daily_max_c"]) / bandwidth) ** 2) for row in accepted)
        for temperature in grid
    ]
    total = sum(scores)
    if total <= 0.0:
        return _blocked("ncei_climate_reference_kernel_mass_invalid")
    continuous = {
        "schema": CONTINUOUS_SCHEMA_VERSION,
        "grid": [{"temp_c": temperature, "mass": score / total} for temperature, score in zip(grid, scores)],
        "source": "approved_ncei_ghcnh_hourly_climate_reference_not_final_x",
    }
    core = {
        "schema": SCHEMA_VERSION,
        "policy_version": NCEI_REFERENCE_POLICY_VERSION,
        "approval_kind": "operational_climate_reference_baseline",
        "event_date": target_day.isoformat(),
        "decision_as_of": as_of.isoformat().replace("+00:00", "Z"),
        "station_id": station,
        "basin_context": _text(basin_context),
        "seasonal_window_days": int(seasonal_window_days),
        "kernel_bandwidth_c": bandwidth,
        "reference_sample_count": len(accepted),
        "reference_measurement_caveat": "NCEI hourly maximum may miss AMSC half-hour extremes and is prior-only",
        "download_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "daily_labels_sha256": hashlib.sha256(labels_bytes).hexdigest(),
        "source_raw_sha256s": sorted(_text(row.get("sha256")) for row in manifest.get("files") or [] if isinstance(row, Mapping) and _text(row.get("sha256"))),
        "continuous_distribution": continuous,
    }
    artifact_sha = _sha(core)
    prior_model_artifact_sha = _sha({
        "schema": "ncei_temperature_prior_model_v1",
        "policy_version": NCEI_REFERENCE_POLICY_VERSION,
        "station_id": station,
        "basin_context": _text(basin_context),
        "seasonal_window_days": int(seasonal_window_days),
        "kernel_bandwidth_c": bandwidth,
        "temperature_grid_c": grid,
        "source": "ncei_ghcnh_hourly_climate_reference_not_final_x",
    })
    return {
        **core,
        "artifact_sha256": artifact_sha,
        "prior_model_artifact_sha256": prior_model_artifact_sha,
        "prior_context_id": f"nceiclimateprior_{artifact_sha[:24]}",
        "ok": True,
        "state": "available_shadow_only",
        "full_p0_checkpoint_ready": False,
        "full_p0_na_reason": "latent_state_prior_not_yet_joined",
        "read_only": True,
        "shadow_only": True,
        "boundary": {
            "prior_only_not_final_x_training_label": True,
            "amsc_remains_final_x_authority": True,
            "ncei_hourly_domain_caveat_explicit": True,
            "d0_observation_not_consumed": True,
            "forecast_not_consumed": True,
            "settlement_s_or_market_bucket_not_consumed": True,
            "strict_asof_oos_not_live_gate": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def build_full_physical_p0_prior_context(
    *,
    temperature_prior_context: Mapping[str, Any],
    latent_state_prior: Mapping[str, Any],
) -> dict[str, Any]:
    """Join independently fitted physical-X and Z0 priors without refitting.

    This only constructs the P0 prior input.  The initial-checkpoint owner
    still separately verifies the active strong artifact's transition support
    and the closed X->S->B bridge before any checkpoint can be written.
    """

    temperature = dict(temperature_prior_context) if isinstance(temperature_prior_context, Mapping) else {}
    latent = dict(latent_state_prior) if isinstance(latent_state_prior, Mapping) else {}
    reasons: list[str] = []
    if temperature.get("schema") != SCHEMA_VERSION or temperature.get("ok") is not True or temperature.get("state") != "available_shadow_only":
        reasons.append("physical_full_p0_temperature_prior_unavailable")
    if latent.get("schema") != "physical_latent_state_prior_v1" or latent.get("ok") is not True or latent.get("state") != "available_shadow_only":
        reasons.append("physical_full_p0_latent_prior_unavailable")
    continuous = temperature.get("continuous_distribution") if isinstance(temperature.get("continuous_distribution"), Mapping) else {}
    grid_rows = continuous.get("grid") if isinstance(continuous.get("grid"), list) else []
    temperature_grid = [_finite(row.get("temp_c")) for row in grid_rows if isinstance(row, Mapping)]
    latent_grid = [_finite(value) for value in latent.get("temperature_grid_c") or []]
    if any(value is None for value in temperature_grid) or any(value is None for value in latent_grid) or temperature_grid != latent_grid:
        reasons.append("physical_full_p0_temperature_grid_mismatch")
    latent_by_temperature = latent.get("latent_state_prior_by_temperature") if isinstance(latent.get("latent_state_prior_by_temperature"), Mapping) else {}
    expected = {f"{float(value):g}" for value in temperature_grid if value is not None}
    if set(latent_by_temperature) != expected:
        reasons.append("physical_full_p0_latent_state_grid_mismatch")
    if reasons:
        return _blocked(*reasons)
    core = {
        "schema": "full_physical_p0_prior_context_v1",
        "temperature_prior_artifact_sha256": _text(temperature.get("artifact_sha256")),
        "latent_state_prior_artifact_sha256": _text(latent.get("artifact_sha256")),
        "continuous_distribution": dict(continuous),
        "latent_state_prior_by_temperature": dict(latent_by_temperature),
        "event_date": _text(temperature.get("event_date")),
        "decision_as_of": _text(temperature.get("decision_as_of")),
        "station_id": _text(temperature.get("station_id")).upper(),
        "basin_context": _text(temperature.get("basin_context")),
    }
    temperature_model_sha = _text(temperature.get("prior_model_artifact_sha256"))
    latent_model_sha = _text(latent.get("artifact_sha256"))
    if not temperature_model_sha or not latent_model_sha:
        return _blocked("physical_full_p0_prior_model_artifact_identity_missing")
    core["prior_model_artifact_sha256"] = _sha({
        "schema": "full_physical_p0_prior_model_v1",
        "temperature_prior_model_artifact_sha256": temperature_model_sha,
        "latent_state_prior_model_artifact_sha256": latent_model_sha,
    })
    artifact_sha = _sha(core)
    return {
        **core,
        "artifact_sha256": artifact_sha,
        "prior_context_id": f"physicalp0_{artifact_sha[:24]}",
        "ok": True,
        "state": "available_shadow_only",
        "read_only": True,
        "shadow_only": True,
        "boundary": {
            "physical_x_only": True,
            "requires_separate_strong_transition_and_bridge_gate": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def bind_full_physical_p0_prior_context_for_production(
    prior_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a freshly built physical P0 prior to the production runtime.

    The daily context stays content-addressed evidence. This does not refit or
    alter probability mass; it only prevents production from consuming a
    result that still carries the isolated-shadow execution boundary.
    """

    value = dict(prior_context) if isinstance(prior_context, Mapping) else {}
    if (
        value.get("schema") != "full_physical_p0_prior_context_v1"
        or value.get("ok") is not True
        or value.get("state") != "available_shadow_only"
        or value.get("shadow_only") is not True
        or not _text(value.get("artifact_sha256"))
        or not _text(value.get("prior_model_artifact_sha256"))
    ):
        return _blocked("physical_full_p0_prior_not_bindable_for_production")
    boundary = dict(value.get("boundary")) if isinstance(value.get("boundary"), Mapping) else {}
    if boundary.get("physical_x_only") is not True or boundary.get("places_orders") is not False:
        return _blocked("physical_full_p0_prior_production_boundary_invalid")
    return {
        **value,
        "state": "available",
        "production_runtime_eligible": True,
        "shadow_only": False,
        "boundary": {
            **boundary,
            "production_probability_input": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = [
    "ALLOWED_SOURCE_KINDS",
    "CONTINUOUS_SCHEMA_VERSION",
    "NCEI_REFERENCE_POLICY_VERSION",
    "POLICY_VERSION",
    "SCHEMA_VERSION",
    "bind_full_physical_p0_prior_context_for_production",
    "build_full_physical_p0_prior_context",
    "build_ncei_operational_climate_reference_prior_context",
    "build_physical_temperature_prior_context",
]
