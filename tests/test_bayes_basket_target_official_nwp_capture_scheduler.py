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


def test_candidate_prefetch_is_single_scope_raw_only_and_isolates_provider_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        scheduler,
        "load_market_registry_snapshot",
        lambda _path: {"snapshot_sha256": "d" * 64},
    )
    calls = []

    def provider_runner(**kwargs):
        calls.append(kwargs)
        provider = kwargs["provider_id"]
        if provider == scheduler.MODEL_PROVIDER_IDS["ICON"]:
            partial = kwargs["output_dir"] / "partial.bin"
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"partial")
            raise RuntimeError("provider temporarily unavailable")
        path = kwargs["output_dir"] / "decode.json"
        model = next(
            model_id
            for model_id, provider_id in scheduler.MODEL_PROVIDER_IDS.items()
            if provider_id == provider
        )
        arrival_identity = {
            "schema": "bayes_basket_target_prospective_model_arrival_v1",
            "provider_id": provider,
            "model_id": model,
            "station_id": "RKPK",
            "target_local_date": "2026-07-30",
            "run_initialization_utc": "2026-07-29T00:00:00Z",
            "published_at_utc": "2026-07-29T00:30:00Z",
            "received_at_utc": "2026-07-29T01:00:00Z",
            "raw_payload_sha256s": ["a" * 64],
            "receipt_sha256": "b" * 64,
        }
        write_bounded_json_if_changed(path, {
            "arrival": {
                **arrival_identity,
                "content_sha256": content_sha256(arrival_identity),
            },
        })
        return {
            "blocked_count": 0,
            "records": [{"status": "CAPTURED", "path": str(path)}],
        }

    result = scheduler.prefetch_candidate_raw_training(
        registry_snapshot_path=registry_path,
        scope={
            "event_date": "2026-07-30",
            "settlement_timezone": "Asia/Seoul",
            "station_ids": ["RKPK"],
        },
        as_of_utc="2026-07-29T06:00:00Z",
        run_initialization_utc="2026-07-29T00:00:00Z",
        # Provider ids and names are both accepted; the duplicate ECMWF
        # entry must not cause a second fetch.
        candidate_models=(
            "ECMWF",
            "ecmwf_ifs025_official",
            "GFS",
            "ICON",
            "GEM",
        ),
        output_root=tmp_path / "prefetch",
        provider_runner=provider_runner,
    )

    assert [call["provider_id"] for call in calls] == [
        "ecmwf_ifs025_official",
        "gfs_0p25_official",
        "icon_global_official",
        "gem_gdps_official",
    ]
    assert all(call["station_ids"] == ["RKPK"] for call in calls)
    assert all(
        call["run_initialization_utc"] == "2026-07-29T00:00:00Z"
        for call in calls
    )
    assert result["state"] == "CAPTURED_CANDIDATE"
    assert result["candidate_only"] is True
    assert len(result["content_sha256"]) == 64
    assert result["p0_materialization_authorized"] is False
    assert result["production_no_live"] is True
    assert result["creates_p0"] is False
    assert result["creates_activation"] is False
    assert result["native_arrival_indexed"] is False
    assert result["writes_target_database"] is False
    assert result["calls_order_gateway"] is False
    assert result["successful_models"] == ["ECMWF", "GFS", "GEM"]
    assert result["complete_provider_count"] == 3
    assert len(result["capture_paths"]) == 3
    assert Path(result["receipt_path"]).is_file()
    assert all(
        row["native_arrival_indexed"] is False
        for row in result["providers"]
    )
    assert next(
        row for row in result["providers"] if row["model_id"] == "ICON"
    )["state"] == "BLOCKED_FAIL_CLOSED"
    icon_dir = (
        tmp_path / "prefetch/candidate_raw/2026-07-30"
        / "Asia_Seoul/RKPK/20260729T000000Z/icon_global_official"
    )
    assert not icon_dir.exists()

    replay = scheduler.prefetch_candidate_raw_training(
        registry_snapshot_path=registry_path,
        scope={
            "event_date": "2026-07-30",
            "settlement_timezone": "Asia/Seoul",
            "station_ids": ["RKPK"],
        },
        as_of_utc="2026-07-29T06:01:00Z",
        run_initialization_utc="2026-07-29T00:00:00Z",
        candidate_models=("ECMWF", "GFS", "ICON", "GEM"),
        output_root=tmp_path / "prefetch",
        provider_runner=provider_runner,
    )
    assert [call["provider_id"] for call in calls[4:]] == [
        "icon_global_official"
    ]
    assert replay["content_sha256"] == result["content_sha256"]


def test_candidate_prefetch_rejects_paths_without_arrival_evidence_and_cleans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        scheduler,
        "load_market_registry_snapshot",
        lambda _path: {"snapshot_sha256": "d" * 64},
    )

    def provider_runner(**kwargs):
        path = kwargs["output_dir"] / "decode.json"
        write_bounded_json_if_changed(path, {
            "provider_id": kwargs["provider_id"],
            "forecast": 31.0,
        })
        return {
            "blocked_count": 0,
            "records": [{"status": "CAPTURED", "path": str(path)}],
        }

    result = scheduler.prefetch_candidate_raw_training(
        registry_snapshot_path=tmp_path / "registry.json",
        scope={
            "event_date": "2026-07-30",
            "settlement_timezone": "Asia/Seoul",
            "station_ids": ["KDAL"],
        },
        as_of_utc="2026-07-29T06:00:00Z",
        run_initialization_utc="2026-07-29T00:00:00Z",
        candidate_models=("ECMWF", "GFS"),
        output_root=tmp_path / "prefetch",
        provider_runner=provider_runner,
    )

    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert result["capture_paths"] == []
    assert all(
        "publication_arrival_evidence_missing"
        in str(row.get("reason") or "")
        for row in result["providers"]
    )
    run_root = (
        tmp_path / "prefetch/candidate_raw/2026-07-30"
        / "Asia_Seoul/KDAL/20260729T000000Z"
    )
    assert not (run_root / "ecmwf_ifs025_official").exists()
    assert not (run_root / "gfs_0p25_official").exists()


@pytest.mark.parametrize(
    ("models", "error"),
    [
        (("ECMWF",), "official_nwp_candidate_model_count_below_minimum"),
        (
            ("ECMWF", "GFS", "ICON", "GEM", "CMA", "HRRR"),
            "official_nwp_candidate_model_count_exceeded",
        ),
        (("NOT_A_MODEL", "GFS"), "official_nwp_candidate_model_invalid"),
    ],
)
def test_candidate_prefetch_requires_two_to_five_known_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    models: tuple[str, ...],
    error: str,
) -> None:
    monkeypatch.setattr(
        scheduler,
        "load_market_registry_snapshot",
        lambda _path: {"snapshot_sha256": "e" * 64},
    )
    with pytest.raises(scheduler.ContractError, match=error):
        scheduler.prefetch_candidate_raw_training(
            registry_snapshot_path=tmp_path / "registry.json",
            scope={
                "event_date": "2026-07-30",
                "settlement_timezone": "Asia/Seoul",
                "station_ids": ["RKPK"],
            },
            as_of_utc="2026-07-29T06:00:00Z",
            run_initialization_utc="2026-07-29T00:00:00Z",
            candidate_models=models,
            output_root=tmp_path / "prefetch",
            provider_runner=lambda **_: {},
        )


def test_candidate_prefetch_rejects_multiple_stations_and_future_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        scheduler,
        "load_market_registry_snapshot",
        lambda _path: {"snapshot_sha256": "f" * 64},
    )
    kwargs = {
        "registry_snapshot_path": tmp_path / "registry.json",
        "scope": {
            "event_date": "2026-07-30",
            "settlement_timezone": "Asia/Seoul",
            "station_ids": ["RKPK", "KDAL"],
        },
        "as_of_utc": "2026-07-29T06:00:00Z",
        "run_initialization_utc": "2026-07-29T00:00:00Z",
        "candidate_models": ("ECMWF", "GFS"),
        "output_root": tmp_path / "prefetch",
        "provider_runner": lambda **_: {},
    }
    with pytest.raises(
        scheduler.ContractError,
        match="official_nwp_candidate_scope_must_be_one_station",
    ):
        scheduler.prefetch_candidate_raw_training(**kwargs)

    kwargs["scope"] = {
        "event_date": "2026-07-30",
        "settlement_timezone": "Asia/Seoul",
        "station_ids": ["RKPK"],
    }
    kwargs["as_of_utc"] = "2026-07-28T23:59:00Z"
    with pytest.raises(
        scheduler.ContractError,
        match="official_nwp_candidate_run_after_as_of",
    ):
        scheduler.prefetch_candidate_raw_training(**kwargs)


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


def test_registry_scope_discovery_covers_canonical_multiday_catalog() -> None:
    rows = []
    stations = [f"S{index:03d}" for index in range(45)]
    for day in (
        "2026-07-29",  # closed
        "2026-07-30",  # open
        "2026-07-31",  # not started
        "2026-08-01",  # not started
        "2026-08-03",  # more than 72 hours away
    ):
        for station in stations:
            rows.append({
                "station_id": station,
                "event_date": day,
                "settlement_timezone": "America/New_York",
                "outcome": "mid",
                "source_eligible": 1,
                "market_active": 1,
                "market_accepting_orders": 1,
            })

    scopes = scheduler.registry_capture_scopes(
        {"rows": rows},
        as_of_utc="2026-07-30T16:00:00Z",
    )

    assert scheduler.MAX_DISCOVERED_SCOPES == 135
    assert scheduler.MAX_SCOPES_PER_CYCLE == 4
    assert len(scopes) == 135
    by_day = {
        day: [
            row for row in scopes if row["event_date"] == day
        ]
        for day in {row["event_date"] for row in scopes}
    }
    assert set(by_day) == {
        "2026-07-30",
        "2026-07-31",
        "2026-08-01",
    }
    assert all(len(by_day[day]) == 45 for day in by_day)
    assert {
        row["event_day_state"] for row in by_day["2026-07-30"]
    } == {"open"}
    assert {
        row["event_day_state"] for row in by_day["2026-07-31"]
    } == {"not_started"}
    assert {
        row["event_day_state"] for row in by_day["2026-08-01"]
    } == {"not_started"}


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
