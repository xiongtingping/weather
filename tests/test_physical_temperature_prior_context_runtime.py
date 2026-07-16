from __future__ import annotations

import hashlib
import json
from pathlib import Path

from physical_temperature_prior_context_runtime import (
    bind_full_physical_p0_prior_context_for_production,
    build_full_physical_p0_prior_context,
    build_ncei_operational_climate_reference_prior_context,
    build_physical_temperature_prior_context,
)
from physical_latent_state_prior_runtime import (
    build_neutral_physical_latent_state_operational_baseline,
    build_physical_latent_state_prior,
)


def _record(day: str, temperature: float, *, available_at: str | None = None, source_kind: str = "amsc_physical_final") -> dict:
    return {
        "event_date": day,
        "station_id": "ZUUU",
        "available_at": available_at or f"{day}T23:59:00Z",
        "final_physical_high_c": temperature,
        "raw_sha256": hashlib.sha256(f"{day}:{temperature}".encode()).hexdigest(),
        "source_kind": source_kind,
    }


def test_physical_prior_uses_only_asof_physical_x_records_and_is_content_addressed() -> None:
    result = build_physical_temperature_prior_context(
        event_date="2026-07-12",
        decision_as_of="2026-07-12T06:30:00Z",
        temperature_grid_c=[30.0, 31.0, 32.0, 33.0],
        physical_x_records=[
            _record("2025-07-08", 31.0, available_at="2026-07-01T00:00:00Z", source_kind="amsc_historical_complete"),
            _record("2025-07-10", 32.0, available_at="2026-07-01T00:00:00Z"),
            _record("2026-07-11", 33.0, available_at="2026-07-12T06:00:00Z"),
            _record("2026-07-12", 40.0, available_at="2026-07-12T06:00:00Z"),
        ],
        seasonal_window_days=15,
        minimum_samples=3,
        kernel_bandwidth_c=1.0,
    )

    assert result["ok"] is True
    assert result["prior_context_id"].startswith("physicalprior_")
    assert len(result["physical_x_records"]) == 3
    assert abs(sum(row["mass"] for row in result["continuous_distribution"]["grid"]) - 1.0) < 1e-12
    assert result["full_p0_checkpoint_ready"] is False
    assert result["boundary"]["settlement_s_or_market_bucket_not_accepted"] is True


def test_physical_prior_fails_insufficient_instead_of_using_settlement_or_future_x() -> None:
    result = build_physical_temperature_prior_context(
        event_date="2026-07-12",
        decision_as_of="2026-07-12T06:30:00Z",
        temperature_grid_c=[30.0, 31.0],
        physical_x_records=[
            _record("2026-07-11", 31.0, available_at="2026-07-12T07:00:00Z"),
            _record("2025-07-10", 31.0, source_kind="settlement_s"),
        ],
        minimum_samples=1,
    )

    assert result["ok"] is False
    assert result["state"] == "insufficient_history"
    assert result["block_reasons"] == ["physical_prior_minimum_physical_x_samples_not_met"]
    assert result["diagnostics"]["accepted_physical_x_sample_count"] == 0


def test_full_physical_p0_joins_matching_empirical_temperature_and_latent_priors() -> None:
    temperature = build_physical_temperature_prior_context(
        event_date="2026-07-12",
        decision_as_of="2026-07-12T06:30:00Z",
        temperature_grid_c=[30.0, 31.0],
        physical_x_records=[
            _record("2025-07-10", 30.0, available_at="2026-07-01T00:00:00Z"),
            _record("2025-07-11", 31.0, available_at="2026-07-01T00:00:00Z"),
        ],
        minimum_samples=2,
    )
    samples = [
        {"training_sample_id": "a", "quality_grade": "B", "physical_auxiliary_eligible": True, "canonical_sequence_event_ids": ["a"], "labels": {"final_physical_high_c": 30.0}},
        {"training_sample_id": "b", "quality_grade": "B", "physical_auxiliary_eligible": True, "canonical_sequence_event_ids": ["b"], "labels": {"final_physical_high_c": 31.0}},
    ]
    latent = build_physical_latent_state_prior(
        physical_training_samples=samples,
        latent_replay_labels=[
            {"training_sample_id": "a", "training_scope": "physical_x", "z_pre": "warming", "latent_replay_sha256": "a"},
            {"training_sample_id": "b", "training_scope": "physical_x", "z_pre": "temporary_platform", "latent_replay_sha256": "b"},
        ],
        temperature_grid_c=[30.0, 31.0],
        minimum_samples_per_temperature=1,
    )
    full = build_full_physical_p0_prior_context(
        temperature_prior_context=temperature,
        latent_state_prior=latent,
    )

    assert full["ok"] is True

    production = bind_full_physical_p0_prior_context_for_production(full)
    assert production["production_runtime_eligible"] is True
    assert production["shadow_only"] is False
    assert production["artifact_sha256"] == full["artifact_sha256"]
    assert production["continuous_distribution"] == full["continuous_distribution"]
    assert full["prior_context_id"].startswith("physicalp0_")
    assert full["boundary"]["requires_separate_strong_transition_and_bridge_gate"] is True


def test_ncei_climate_reference_is_prior_only_and_can_join_neutral_latent_baseline(tmp_path: Path) -> None:
    manifest = tmp_path / "download_manifest.json"
    manifest.write_text(json.dumps({
        "schema": "ncei_zuuu_ghcnh_research_download_manifest_v1",
        "download_completed_at": "2026-07-10T11:26:42Z",
        "station_identity": {"icao": "ZUUU"},
        "files": [{"sha256": "a" * 64}],
    }), encoding="utf-8")
    labels = tmp_path / "daily_labels.csv"
    labels.write_text(
        "event_date_local,complete_for_daily_max_label,final_daily_max_c\n"
        "2024-07-01,True,34\n"
        "2024-07-10,True,36\n"
        "2025-07-12,True,35\n",
        encoding="utf-8",
    )
    temperature = build_ncei_operational_climate_reference_prior_context(
        event_date="2026-07-14",
        decision_as_of="2026-07-13T12:00:00Z",
        temperature_grid_c=[32.0, 33.0, 34.0, 35.0, 36.0, 37.0],
        daily_labels_path=labels,
        download_manifest_path=manifest,
        minimum_samples=3,
    )
    latent = build_neutral_physical_latent_state_operational_baseline(
        temperature_grid_c=[32.0, 33.0, 34.0, 35.0, 36.0, 37.0]
    )
    full = build_full_physical_p0_prior_context(
        temperature_prior_context=temperature,
        latent_state_prior=latent,
    )

    assert temperature["ok"] is True
    assert temperature["boundary"]["prior_only_not_final_x_training_label"] is True
    assert latent["boundary"]["does_not_change_marginal_p0_x"] is True
    assert full["ok"] is True
