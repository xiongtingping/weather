from __future__ import annotations

import json
from pathlib import Path

from joint_weak_baseline_artifact_runtime import build_joint_weak_operational_baseline_artifact
from production_probability_activation_contract import (
    BAYES_FORMULA_ID,
    CHECKPOINT_SCHEMA,
    COMPOSER_SCHEMA,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION as ACTIVATION_SCHEMA,
    STRONG_UPDATE_SCHEMA,
)
from production_probability_p0_runtime import (
    build_physical_only_resolution_contract,
    build_production_p0_inputs,
    build_zuuu_physical_public_slug,
    compile_production_p0_candidate,
)
from production_probability_runtime_manifest import current_runtime_manifest_sha256


EVENT_DATE = "2026-07-14"
AS_OF = "2026-07-13T12:00:00Z"
ROOT = Path(__file__).resolve().parents[1]


def test_physical_public_slug_is_deterministic_without_market_discovery() -> None:
    assert build_zuuu_physical_public_slug("2026-07-15") == "highest-temperature-in-chengdu-on-july-15-2026"


def test_production_resolution_authority_rejects_retired_v2() -> None:
    retired = ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v2.json"
    try:
        build_physical_only_resolution_contract(retired, event_date=EVENT_DATE)
    except ValueError as exc:
        assert "production_resolution_authority_not_accepted_v4" in str(exc)
    else:
        raise AssertionError("retired v2 resolution authority was accepted")


def _files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    ncei = tmp_path / "ncei"
    (ncei / "derived").mkdir(parents=True)
    (ncei / "download_manifest.json").write_text(json.dumps({
        "schema": "ncei_zuuu_ghcnh_research_download_manifest_v1",
        "download_completed_at": "2026-07-10T00:00:00Z",
        "station_identity": {"icao": "ZUUU"},
        "files": [{"sha256": "a" * 64}],
    }), encoding="utf-8")
    rows = ["event_date_local,complete_for_daily_max_label,final_daily_max_c"]
    rows.extend(f"{year}-07-14,True,{34 + year % 4}" for year in range(1990, 2026))
    (ncei / "derived" / "daily_labels.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    weak = tmp_path / "weak.json"
    weak.write_text(json.dumps(build_joint_weak_operational_baseline_artifact()), encoding="utf-8")
    resolution = tmp_path / "resolution.json"
    resolution.write_bytes(
        (ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_bytes()
    )
    curves = tmp_path / "curves"
    curves.mkdir()
    return ncei, weak, resolution, curves


def _activation(artifacts: dict) -> dict:
    return {
        "schema": ACTIVATION_SCHEMA,
        "state": "production_enabled",
        "production_transaction_b_authorized": True,
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "no_order_validation_accepted": True,
        "cutover_approval_id": "pytest-production-p0",
        "activation_effective_at": "2026-07-13T00:00:00Z",
        "prior_model_artifact_sha256": artifacts["prior_model_artifact_sha256"],
        "weak_likelihood_artifact_sha256": artifacts["weak_likelihood_artifact_sha256"],
        "strong_lr_artifact_sha256": artifacts["strong_lr_artifact_sha256"],
        "runtime_manifest_sha256": current_runtime_manifest_sha256(),
        "legacy_writer_retirement_receipt_sha256": "e" * 64,
        "bayes_formula_id": BAYES_FORMULA_ID,
        "composer_schema": COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
        "places_orders": False,
        "authorizes_clob": False,
    }


def test_production_p0_is_rebuilt_from_bound_inputs_and_compiles_under_exact_activation(tmp_path: Path) -> None:
    ncei, weak, resolution, curves = _files(tmp_path)
    inputs = build_production_p0_inputs(
        event_date=EVENT_DATE,
        decision_as_of=AS_OF,
        source_observations=[],
        curve_root=curves,
        weak_artifact_path=weak,
        ncei_root=ncei,
        resolution_authority_path=resolution,
    )

    assert inputs["ok"] is True
    assert inputs["prior_context"]["production_runtime_eligible"] is True
    assert inputs["prior_context"]["shadow_only"] is False
    assert inputs["weak_likelihood"]["joint_weak_likelihood"]["production_runtime_eligible"] is True
    assert inputs["weak_likelihood"]["joint_weak_likelihood"]["shadow_only"] is False
    assert inputs["strong_lr_artifact"]["production_runtime_eligible"] is True
    assert inputs["resolution_contract"]["authority_contract_id"] == "zuuu_amsc_wunderground_daily_high_v4"
    assert inputs["resolution_contract"]["resolution_source_id"] == "wunderground_zuuu_historical_observation_daily_high_final_v3"

    compiled = compile_production_p0_candidate(
        activation=_activation(inputs["runtime_artifacts"]),
        inputs=inputs,
    )
    assert compiled["ok"] is True
    assert compiled["candidate"]["initial_mode"] == "production"
    assert compiled["candidate"]["shadow_only"] is False
    assert compiled["candidate"]["checkpoint"]["consumed_event_ids"] == []


def test_production_p0_fails_closed_when_activation_model_identity_drifts(tmp_path: Path) -> None:
    ncei, weak, resolution, curves = _files(tmp_path)
    inputs = build_production_p0_inputs(
        event_date=EVENT_DATE,
        decision_as_of=AS_OF,
        source_observations=[],
        curve_root=curves,
        weak_artifact_path=weak,
        ncei_root=ncei,
        resolution_authority_path=resolution,
    )
    activation = _activation(inputs["runtime_artifacts"])
    activation["prior_model_artifact_sha256"] = "9" * 64

    compiled = compile_production_p0_candidate(activation=activation, inputs=inputs)

    assert compiled["ok"] is False
    assert compiled["state"] == "blocked"
    assert "activation_prior_artifact_mismatch" in compiled["block_reasons"][0]
