from __future__ import annotations

from pathlib import Path

import pytest

import bayes_basket_target.official_nwp_capture_scheduler as scheduler
from bayes_basket_target.contracts import content_sha256, model_set_hash
from bayes_basket_target.resource_bounds import write_bounded_json_if_changed


def _binding(
    models: tuple[str, ...],
    *,
    run_cycle: str = "00Z",
    lead_bucket: str = "d2",
) -> dict:
    mask = model_set_hash(models)
    ensemble_identity = {
        "ensemble_artifact_id": f"ens-rkpk-selected-{run_cycle}",
        "station_id": "RKPK",
        "season_bin": "summer",
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "settlement_definition": "official_daily_tmax_c",
        "candidate_models": list(models),
        "supported_masks": {
            mask: {model: 1.0 / len(models) for model in models},
        },
        "residual_covariance": {},
        "correlation_penalty": 0.0,
        "selection_evidence_sha256": "b" * 64,
        "previous_ensemble_artifact_sha256": None,
        "weight_method": "rolling_origin_probability_stack_v4_five_model_pool_min2",
        "effective_sample_n": 20.0,
        "max_weight_delta": 0.2,
        "train_start_date": "2026-06-01",
        "train_end_date": "2026-06-20",
        "recent_start_date": "2026-06-14",
        "recent_end_date": "2026-06-20",
        "holdout_start_date": "2026-06-21",
        "holdout_end_date": "2026-06-30",
        "oos_metrics": {"brier_score": 0.2, "nll": 0.5, "crps": 0.3},
        "valid_from_utc": "2026-07-01T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "promoted": False,
    }
    ensemble = {
        **ensemble_identity,
        "content_sha256": content_sha256(ensemble_identity),
    }
    activation_identity = {
        "activation_id": f"activation-rkpk-selected-{run_cycle}",
        "artifact_kind": "MODEL_ENSEMBLE",
        "artifact_id": ensemble["ensemble_artifact_id"],
        "station_id": "RKPK",
        "model_id_or_star": "*",
        "season_bin": "summer",
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "settlement_definition": "official_daily_tmax_c",
        "available_model_set_hash_or_star": mask,
        "generation": 7,
        "valid_from_utc": "2026-07-01T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "activated_at_utc": "2026-07-01T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    activation = {
        **activation_identity,
        "content_sha256": content_sha256(activation_identity),
    }
    binding_identity = {
        "selected_models": list(models),
        "ensemble_artifact_id": ensemble["ensemble_artifact_id"],
        "ensemble_artifact_sha256": ensemble["content_sha256"],
        "selection_evidence_sha256": ensemble["selection_evidence_sha256"],
        "activation_id": activation["activation_id"],
        "activation_generation": activation["generation"],
        "activation_sha256": activation["content_sha256"],
        "authority_scope": activation["authority_scope"],
    }
    return {
        "activation_binding": {
            **binding_identity,
            "binding_sha256": content_sha256(binding_identity),
        },
        "ensemble_activation": activation,
        "ensemble_artifact": ensemble,
    }


def test_candidate_runs_cap_at_settlement_start_and_fallback_by_cycle() -> None:
    assert scheduler.candidate_run_initializations(
        as_of_utc="2026-07-27T20:12:00Z",
        event_date="2026-07-27",
        settlement_timezone="Asia/Seoul",
    ) == [
        "2026-07-26T12:00:00Z",
        "2026-07-26T06:00:00Z",
        "2026-07-26T00:00:00Z",
        "2026-07-25T18:00:00Z",
    ]


def test_scheduler_exposes_all_five_model_provider_roles() -> None:
    assert {
        key: scheduler.MODEL_PROVIDER_IDS[key]
        for key in ("ECMWF", "GFS", "ICON", "GEM", "CMA")
    } == {
        "ECMWF": "ecmwf_ifs025_official",
        "GFS": "gfs_0p25_official",
        "ICON": "icon_global_official",
        "GEM": "gem_gdps_official",
        "CMA": "cma_grapes_global_open_meteo",
    }
    assert scheduler.MODEL_PROVIDER_IDS["JMA-MSM"] == "jma_msm_open_meteo"
    assert scheduler.MODEL_PROVIDER_IDS["HRRR"] == (
        "ncep_hrrr_conus_open_meteo"
    )


def test_registry_scopes_use_each_market_timezone_and_skip_closed() -> None:
    rows = []
    for station, day, timezone_name in (
        ("RKPK", "2026-07-27", "Asia/Seoul"),
        ("KORD", "2026-07-28", "America/Chicago"),
        ("OLD", "2026-07-25", "America/New_York"),
    ):
        rows.append({
            "station_id": station,
            "event_date": day,
            "settlement_timezone": timezone_name,
            "outcome": "mid",
            "source_eligible": 1,
            "market_active": 1,
            "market_accepting_orders": 1,
        })
    scopes = scheduler.registry_capture_scopes(
        {"rows": rows},
        as_of_utc="2026-07-26T18:00:00Z",
    )
    identities = {
        (row["station_ids"][0], row["event_date"], row["settlement_timezone"])
        for row in scopes
    }
    assert ("RKPK", "2026-07-27", "Asia/Seoul") in identities
    assert ("KORD", "2026-07-28", "America/Chicago") in identities
    assert all(station != "OLD" for station, _, _ in identities)


def test_capture_falls_back_until_exact_selected_mask_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        scheduler,
        "load_market_registry_snapshot",
        lambda _path: {"snapshot_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        scheduler,
        "candidate_run_initializations",
        lambda **_: [
            "2026-07-26T00:00:00Z",
            "2026-07-25T18:00:00Z",
            "2026-07-25T12:00:00Z",
        ],
    )
    calls = []

    def provider_runner(**kwargs):
        calls.append((kwargs["run_initialization_utc"], kwargs["provider_id"]))
        run = kwargs["run_initialization_utc"]
        provider = kwargs["provider_id"]
        complete = (
            (run == "2026-07-26T00:00:00Z" and provider == "gfs_0p25_official")
            or (
                run == "2026-07-25T18:00:00Z"
                and provider in {
                    "ecmwf_ifs025_official",
                    "gfs_0p25_official",
                }
            )
        )
        if not complete:
            return {"blocked_count": 1, "records": []}
        path = kwargs["output_dir"] / "capture.json"
        write_bounded_json_if_changed(
            path,
            {"status": "CAPTURED", "arrival": {"decision_at_utc": "2026-07-26T01:00:00Z"}},
        )
        return {
            "blocked_count": 0,
            "records": [{"status": "CAPTURED", "path": str(path)}],
        }

    result = scheduler.capture_scope(
        registry_snapshot_path=registry_path,
        scope={
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
            "station_ids": ["RKPK"],
        },
        as_of_utc="2026-07-26T02:00:00Z",
        output_root=tmp_path / "capture",
        activation_bindings_by_run={
            "2026-07-26T00:00:00Z": _binding(("ECMWF", "GFS")),
            "2026-07-25T18:00:00Z": _binding(
                ("ECMWF", "GFS"),
                run_cycle="18Z",
                lead_bucket="d2",
            ),
            "2026-07-25T12:00:00Z": _binding(
                ("ECMWF", "GFS"),
                run_cycle="12Z",
                lead_bucket="d2_fallback",
            ),
        },
        provider_runner=provider_runner,
    )

    assert result["state"] == "CAPTURED"
    assert result["selected_run_initialization_utc"] == "2026-07-25T18:00:00Z"
    assert result["attempts"][0]["complete_provider_count"] == 1
    assert result["attempts"][1]["complete_provider_count"] == 2
    assert len(calls) == 4
    assert calls[-1] == (
        "2026-07-25T18:00:00Z",
        "gfs_0p25_official",
    )
    assert {
        provider
        for run, provider in calls
        if run == "2026-07-25T18:00:00Z"
    } == {"ecmwf_ifs025_official", "gfs_0p25_official"}
    assert all(run != "2026-07-25T12:00:00Z" for run, _ in calls)
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0


def test_unselected_provider_is_not_fetched_or_substituted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        scheduler,
        "load_market_registry_snapshot",
        lambda _path: {"snapshot_sha256": "b" * 64},
    )
    runs = ["2026-07-26T00:00:00Z", "2026-07-25T18:00:00Z"]

    calls = []

    def provider_runner(**kwargs):
        run = kwargs["run_initialization_utc"]
        provider = kwargs["provider_id"]
        calls.append((run, provider))
        complete = (
            run == runs[0]
            and provider in {
                "ecmwf_ifs025_official",
                "gfs_0p25_official",
            }
        ) or (
            run == runs[1]
            and provider in {
                "gfs_0p25_official",
                "icon_global_official",
            }
        )
        if not complete:
            return {"blocked_count": 1, "records": []}
        path = kwargs["output_dir"] / "capture.json"
        write_bounded_json_if_changed(
            path,
            {"status": "CAPTURED", "provider": provider},
        )
        return {
            "blocked_count": 0,
            "records": [{"status": "CAPTURED", "path": str(path)}],
        }

    result = scheduler.capture_scope(
        registry_snapshot_path=registry_path,
        scope={
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
            "station_ids": ["RKPK"],
        },
        as_of_utc="2026-07-26T02:00:00Z",
        output_root=tmp_path / "capture",
        activation_bindings_by_run={
            runs[0]: _binding(("GFS", "ICON")),
            runs[1]: _binding(
                ("GFS", "ICON"),
                run_cycle="18Z",
                lead_bucket="d2",
            ),
        },
        run_initializations=runs,
        provider_runner=provider_runner,
    )

    assert result["selected_run_initialization_utc"] == runs[1]
    assert result["attempts"][0]["complete_provider_count"] == 1
    assert result["attempts"][0]["selected_mask_ready"] is False
    assert result["attempts"][1]["selected_mask_ready"] is True
    assert all(
        provider != "ecmwf_ifs025_official"
        for _, provider in calls
    )


def test_activation_generation_or_binding_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        scheduler,
        "load_market_registry_snapshot",
        lambda _path: {"snapshot_sha256": "c" * 64},
    )
    run = "2026-07-26T00:00:00Z"
    bound = _binding(("ECMWF", "GFS"))
    bound["activation_binding"]["activation_generation"] = 8

    with pytest.raises(
        scheduler.ContractError,
        match="official_nwp_scheduler_activation_binding_sha_mismatch",
    ):
        scheduler.capture_scope(
            registry_snapshot_path=registry_path,
            scope={
                "event_date": "2026-07-27",
                "settlement_timezone": "Asia/Seoul",
                "station_ids": ["RKPK"],
            },
            as_of_utc="2026-07-26T02:00:00Z",
            output_root=tmp_path / "capture",
            activation_bindings_by_run={run: bound},
            run_initializations=[run],
            provider_runner=lambda **_: {},
        )
