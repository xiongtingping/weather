"""Assemble and compile the production P0 for the unique probability chain.

This module owns no database and no market side effects. It rebuilds P0 from
the approved physical prior model, the one joint weak model and the approved
strong-LR artifact. A shadow checkpoint is never copied into production.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from joint_weak_live_evidence_runtime import (
    DEFAULT_CURVE_ROOT,
    bind_live_joint_weak_likelihood_for_production,
    build_live_joint_weak_likelihood,
)
from physical_latent_state_prior_runtime import build_neutral_physical_latent_state_operational_baseline
from physical_temperature_prior_context_runtime import (
    bind_full_physical_p0_prior_context_for_production,
    build_full_physical_p0_prior_context,
    build_ncei_operational_climate_reference_prior_context,
)
from physical_x_bucket_contract_runtime import build_physical_x_bucket_contract
from production_initial_posterior_checkpoint import compile_production_initial_posterior_checkpoint
from resolution_authority_contract_runtime import load_resolution_authority_contract
from strong_observation_lr_operational_state_baseline import (
    build_operational_state_aware_strong_lr_baseline,
)
from weak_forecast_evidence_snapshot_runtime import build_weak_forecast_evidence_snapshot_row


SCHEMA_VERSION = "production_probability_p0_runtime_v1"
ROOT = Path(__file__).resolve().parent
DEFAULT_NCEI_ROOT = ROOT / "data" / "research" / "ncei_zuuu_joint_model" / "2021_2025"
DEFAULT_RESOLUTION_AUTHORITY = ROOT / "config" / "resolution_authorities" / "zuuu_amsc_wunderground_daily_high_v4.json"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _blocked(*reasons: str, **context: Any) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted(set(reason for reason in reasons if reason)),
        **context,
        "read_only": True,
        "places_orders": False,
    }


def build_zuuu_physical_public_slug(event_date: str) -> str:
    """Return the stable public key for one ZUUU physical weather day.

    This is an identity formatter, not evidence that a market exists.  Keeping
    it deterministic lets physical-X P0 and AMSC updates run before market
    discovery without changing the public SSOT key later in the day.
    """

    day = date.fromisoformat(_text(event_date)[:10])
    return f"highest-temperature-in-chengdu-on-{day.strftime('%B').lower()}-{day.day}-{day.year}"


def build_physical_only_resolution_contract(
    path: Path | str,
    *,
    event_date: str,
) -> dict[str, Any]:
    source_path = Path(path).expanduser().resolve()
    authority, authority_audit = load_resolution_authority_contract(source_path)
    if authority_audit.get("ok") is not True:
        raise ValueError(
            "production_resolution_authority_not_accepted_v4:"
            + ",".join(str(item) for item in (authority_audit.get("blockers") or []))
        )
    settlement = _mapping(authority.get("settlement_s"))
    core = {
        "authority_contract_id": _text(authority.get("contract_id")),
        "authority_contract_sha256": _text(authority_audit.get("contract_sha256")),
        "authority_validation_schema": _text(authority_audit.get("schema")),
        "resolution_source_id": _text(settlement.get("source_id")),
        "resolution_local_date": _text(event_date)[:10],
        "settlement_unit": "C",
        "bridge_closed": False,
        "validated": False,
        "market_bucket_probabilities_na_reason": "x_to_s_to_b_bridge_data_accumulation",
    }
    return {**core, "resolution_contract_id": f"physicalonly_{_sha(core)[:24]}"}


def build_production_p0_inputs(
    *,
    event_date: str,
    decision_as_of: str,
    source_observations: Iterable[Mapping[str, Any]] | None = None,
    curve_root: Path | str = DEFAULT_CURVE_ROOT,
    weak_artifact_path: Path | str | None = None,
    ncei_root: Path | str = DEFAULT_NCEI_ROOT,
    resolution_authority_path: Path | str = DEFAULT_RESOLUTION_AUTHORITY,
) -> dict[str, Any]:
    """Build production-bound P0 inputs without validating cutover authority."""

    grid_contract = build_physical_x_bucket_contract()
    if grid_contract.get("ok") is not True:
        return _blocked(*(grid_contract.get("block_reasons") or ["physical_x_grid_unavailable"]), temperature_grid_contract=grid_contract)
    grid = list(grid_contract.get("temperature_grid_c") or [])
    root = Path(ncei_root).expanduser().resolve()
    temperature_prior = build_ncei_operational_climate_reference_prior_context(
        event_date=event_date,
        decision_as_of=decision_as_of,
        temperature_grid_c=grid,
        daily_labels_path=root / "derived" / "daily_labels.csv",
        download_manifest_path=root / "download_manifest.json",
    )
    latent_prior = build_neutral_physical_latent_state_operational_baseline(temperature_grid_c=grid)
    prior_unbound = build_full_physical_p0_prior_context(
        temperature_prior_context=temperature_prior,
        latent_state_prior=latent_prior,
    )
    prior = bind_full_physical_p0_prior_context_for_production(prior_unbound)
    weak_unbound = build_live_joint_weak_likelihood(
        temperature_grid_c=grid,
        event_date=event_date,
        decision_as_of=decision_as_of,
        source_observations=source_observations,
        curve_root=curve_root,
        artifact_path=weak_artifact_path,
    )
    weak = bind_live_joint_weak_likelihood_for_production(weak_unbound)
    strong = build_operational_state_aware_strong_lr_baseline()
    blockers: list[str] = []
    if prior.get("ok") is not True:
        blockers.extend(prior.get("block_reasons") or ["production_p0_prior_unavailable"])
    if weak.get("ok") is not True:
        blockers.extend(weak.get("block_reasons") or ["production_p0_weak_unavailable"])
    try:
        resolution = build_physical_only_resolution_contract(resolution_authority_path, event_date=event_date)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        resolution = {}
        blockers.append(f"production_p0_resolution_authority_unavailable:{type(exc).__name__}")
    if blockers:
        return _blocked(
            *blockers,
            temperature_grid_contract=grid_contract,
            temperature_prior=temperature_prior,
            latent_prior=latent_prior,
            prior_context=prior,
            weak_likelihood=weak,
            strong_lr_artifact=strong,
            resolution_contract=resolution,
        )
    runtime_artifacts = {
        "prior_model_artifact_sha256": _text(prior.get("prior_model_artifact_sha256")),
        "prior_context_artifact_sha256": _text(prior.get("artifact_sha256")),
        "weak_likelihood_artifact_sha256": _text(_mapping(weak.get("joint_weak_likelihood")).get("artifact_sha256")),
        "weak_evidence_snapshot_id": _text(_mapping(weak.get("joint_weak_likelihood")).get("forecast_evidence_snapshot_id")),
        "strong_lr_artifact_sha256": _text(strong.get("model_artifact_sha256")),
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "production_p0_inputs_ready",
        "target_identity": f"physical_weather_day_v1:ZUUU:{_text(event_date)[:10]}",
        "event_date": _text(event_date)[:10],
        "decision_as_of": _text(decision_as_of),
        "temperature_grid_contract": grid_contract,
        "temperature_prior": temperature_prior,
        "latent_prior": latent_prior,
        "prior_context": prior,
        "weak_likelihood": weak,
        "strong_lr_artifact": strong,
        "resolution_contract": resolution,
        "runtime_artifacts": runtime_artifacts,
        "read_only": True,
        "places_orders": False,
    }


def compile_production_p0_candidate(
    *,
    activation: Mapping[str, Any],
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile P0 only after the immutable production activation matches."""

    value = _mapping(inputs)
    if value.get("ok") is not True or _text(value.get("state")) != "production_p0_inputs_ready":
        return _blocked("production_p0_inputs_not_ready", inputs=dict(value))
    weak = _mapping(_mapping(value.get("weak_likelihood")).get("joint_weak_likelihood"))
    try:
        candidate = compile_production_initial_posterior_checkpoint(
            activation=activation,
            target_identity=_text(value.get("target_identity")),
            event_date=_text(value.get("event_date")),
            station_id="ZUUU",
            decision_as_of=_text(value.get("decision_as_of")),
            prior_context=_mapping(value.get("prior_context")),
            joint_weak_likelihood=weak,
            active_strong_lr_artifact=_mapping(value.get("strong_lr_artifact")),
            resolution_contract=_mapping(value.get("resolution_contract")),
            source_reliability_initial_state={
                "amsc_zuuu_scheduled_metar_daily_max_v2": {"state": "unknown"},
                "amsc_zuuu_speci_realtime_v1": {"state": "unknown"},
                "awc_zuuu_metar_crosscheck": {"state": "unknown"},
            },
        )
    except (ValueError, RuntimeError) as exc:
        return _blocked(f"production_p0_compile_rejected:{exc}", runtime_artifacts=dict(_mapping(value.get("runtime_artifacts"))))
    weak_snapshot_row = build_weak_forecast_evidence_snapshot_row(
        _mapping(value.get("weak_likelihood")),
        target_identity=_text(value.get("target_identity")),
        event_date=_text(value.get("event_date")),
        decision_as_of=_text(value.get("decision_as_of")),
    )
    candidate = {
        **candidate,
        "weak_forecast_evidence_snapshot_row": weak_snapshot_row,
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": candidate.get("ok") is True,
        "state": "production_p0_candidate_ready" if candidate.get("ok") is True else "blocked",
        "candidate": candidate,
        "runtime_artifacts": dict(_mapping(value.get("runtime_artifacts"))),
        "read_only": True,
        "places_orders": False,
    }


__all__ = [
    "DEFAULT_NCEI_ROOT",
    "DEFAULT_RESOLUTION_AUTHORITY",
    "SCHEMA_VERSION",
    "build_physical_only_resolution_contract",
    "build_production_p0_inputs",
    "build_zuuu_physical_public_slug",
    "compile_production_p0_candidate",
]
