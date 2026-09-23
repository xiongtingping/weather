from __future__ import annotations

import json
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
    selected_provider_by_model: dict[str, str] | None = None,
    aifs_provider_oos_binding: bool = False,
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
    if selected_provider_by_model is not None:
        binding_identity["selected_provider_by_model"] = dict(
            selected_provider_by_model
        )
    if aifs_provider_oos_binding:
        provider_oos_identity = {
            "schema": scheduler.AIFS_PROVIDER_OOS_BINDING_SCHEMA,
            "provider_id": "ecmwf_aifs025_official",
            "model_id": "ECMWF",
            "independence_family_id": "ECMWF",
            "strict_oos_state": "FORMAL_P0_READY",
            "strict_oos_artifact_sha256": "d" * 64,
            "formal_p0_eligible": True,
            "native_arrival_index_eligible": True,
            "provider_lineage": {
                "publication_authority": "official_native",
                "independence_family_id": "ECMWF",
                "same_independence_family_as_ifs": True,
                "counts_as_distinct_independence_family": False,
            },
            "activation_id": activation["activation_id"],
            "activation_sha256": activation["content_sha256"],
            "ensemble_artifact_id": ensemble["ensemble_artifact_id"],
            "ensemble_artifact_sha256": ensemble["content_sha256"],
        }
        binding_identity["provider_strict_oos_binding_by_model"] = {
            "ECMWF": {
                **provider_oos_identity,
                "content_sha256": content_sha256(provider_oos_identity),
            }
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


def test_global_fanout_completion_uses_exact_station_arrivals() -> None:
    paths = [Path("/tmp/arrival-00.json"), Path("/tmp/arrival-01.json")]
    assert scheduler._provider_complete(
        {
            # The source adapter may have skipped unrelated upstream objects.
            # Exact station fanout is the authoritative target-scope result.
            "blocked_count": 7,
            "fanout": {
                "captured_count": 2,
                "expected_arrival_count": 2,
                "blocked_count": 0,
                "blockers": [],
                "outputs": [
                    {"status": "CAPTURED", "path": str(paths[0])},
                    {"status": "REUSED", "path": str(paths[1])},
                ],
            },
        },
        paths,
    ) is True
    assert scheduler._provider_complete(
        {
            "blocked_count": 0,
            "fanout": {
                "captured_count": 1,
                "expected_arrival_count": 2,
                "blocked_count": 0,
                "blockers": [],
                "outputs": [
                    {"status": "CAPTURED", "path": str(paths[0])},
                ],
            },
        },
        paths[:1],
    ) is False


def test_candidate_model_batches_rotate_station_scoped_regional_pool(
    tmp_path: Path,
) -> None:
    pool_path = tmp_path / "regional.json"
    pool_path.write_text(json.dumps({
        "schema": scheduler.REGIONAL_MODEL_POOL_SCHEMA,
        "station_regional_pool": {
            "RKPK": ["JMA-MSM"],
            "KATL": ["HRRR", "NBM", "NAM"],
        },
    }))
    rkpk_batches = [
        scheduler.candidate_model_batch_for_station(
            station_id="RKPK",
            run_initialization_utc=f"2026-07-{day:02d}T12:00:00Z",
            regional_pool_path=pool_path,
        )
        for day in range(1, 7)
    ]
    assert all(
        2 <= len(batch) <= scheduler.MAX_CANDIDATE_MODELS
        for batch in rkpk_batches
    )
    assert set().union(*map(set, rkpk_batches)) == {
        "ECMWF",
        "GFS",
        "ICON",
        "GEM",
        "CMA",
        "JMA-MSM",
    }
    assert "HRRR" not in set().union(*map(set, rkpk_batches))
    assert scheduler.candidate_model_batch_for_station(
        station_id="RKPK",
        run_initialization_utc="2026-07-29T06:00:00Z",
        regional_pool_path=pool_path,
    ) == ["GFS", "ICON"]


def test_candidate_model_batch_rejects_unregistered_regional_model(
    tmp_path: Path,
) -> None:
    pool_path = tmp_path / "regional.json"
    pool_path.write_text(json.dumps({
        "schema": scheduler.REGIONAL_MODEL_POOL_SCHEMA,
        "station_regional_pool": {"RKPK": ["UNREGISTERED"]},
    }))
    with pytest.raises(
        scheduler.ContractError,
        match="station_regional_model_invalid",
    ):
        scheduler.candidate_model_batch_for_station(
            station_id="RKPK",
            run_initialization_utc="2026-07-29T12:00:00Z",
            regional_pool_path=pool_path,
        )


def test_real_station_candidate_pool_eventually_covers_every_configured_model() -> None:
    pool = json.loads(scheduler.REGIONAL_MODEL_POOL_PATH.read_text())
    for station_id, regional_models in pool["station_regional_pool"].items():
        batches = [
            scheduler.candidate_model_batch_for_station(
                station_id=station_id,
                run_initialization_utc=f"2026-07-{day:02d}T12:00:00Z",
            )
            for day in range(1, 15)
        ]
        assert all(
            2 <= len(batch) <= scheduler.MAX_CANDIDATE_MODELS
            for batch in batches
        )
        assert set().union(*map(set, batches)) == {
            "ECMWF",
            "GFS",
            "ICON",
            "GEM",
            "CMA",
            *regional_models,
        }


def test_default_icon_runner_isolates_raw_retention_to_current_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_bayes_basket_target_icon_forward_capture as icon_capture

    calls: list[dict] = []
    monkeypatch.setattr(
        icon_capture,
        "build_plan",
        lambda **kwargs: {"plan": kwargs},
    )
    monkeypatch.setattr(
        icon_capture,
        "execute_plan",
        lambda plan, **kwargs: calls.append(
            {"plan": plan, **kwargs}
        ) or {"blocked_count": 0, "records": []},
    )
    output_dir = tmp_path / "bounded-station-run"

    scheduler._default_provider_runner(
        provider_id="icon_global_official",
        event_date="2026-07-30",
        run_initialization_utc="2026-07-29T18:00:00Z",
        as_of_utc="2026-07-30T08:00:00Z",
        settlement_timezone="America/Mexico_City",
        station_ids=["MMMX"],
        registry_snapshot_sha256="a" * 64,
        contract_path=tmp_path / "model-run-contract.json",
        output_dir=output_dir,
    )

    assert calls == [{
        "plan": {
            "plan": {
                "event_date": "2026-07-30",
                "run_initialization_utc": "2026-07-29T18:00:00Z",
                "as_of_utc": "2026-07-30T08:00:00Z",
                "station_ids": ["MMMX"],
                "target_timezone": "America/Mexico_City",
                "market_registry_snapshot_sha256": "a" * 64,
                "registry_bound": True,
                "source_station_id": "MMMX",
                "due_only": False,
            },
        },
        "source_root": output_dir,
        "output_root": output_dir,
    }]


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

    assert scheduler.MAX_SCOPES_PER_CYCLE == 4
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
    # A station-day expands to one scope per lead, so the catalogue is
    # stations x days x leads.  Station coverage and lead coverage are asserted
    # separately, because collapsing them into a single count would hide either
    # one going missing.
    for day, day_scopes in by_day.items():
        assert {row["station_ids"][0] for row in day_scopes} == set(stations)
        assert {row["lead_days"] for row in day_scopes} == set(
            scheduler.SUPPORTED_LEAD_DAYS
        )
        assert len(day_scopes) == len(stations) * len(
            scheduler.SUPPORTED_LEAD_DAYS
        )
        # Every scope names the run each archive will actually read, so an
        # operator can tell which cycle a capture came from.
        for row in day_scopes:
            assert row["run_init_utc_by_system"] == {
                system_id: run.isoformat().replace("+00:00", "Z")
                for system_id, run in sorted(
                    scheduler.contracted_run_inits(
                        day, lead_days=row["lead_days"]
                    ).items()
                )
            }
    # The bound has to clear what a full canonical catalogue produces, or
    # discovery truncates and the oldest runs are dropped for good.
    assert len(scopes) <= scheduler.MAX_DISCOVERED_SCOPES
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
    # Missing provider-selection evidence pins the ECMWF role to IFS.  An IFS
    # failure moves to the next activated run; it never selects AIFS by
    # availability.
    assert len(calls) == 4
    assert all(provider != "ecmwf_aifs025_official" for _, provider in calls)
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


def test_ecmwf_role_uses_aifs_only_when_own_oos_binding_selects_it(
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
    run = "2026-07-26T00:00:00Z"
    calls: list[str] = []

    def provider_runner(**kwargs):
        provider = kwargs["provider_id"]
        calls.append(provider)
        path = kwargs["output_dir"] / "capture.json"
        write_bounded_json_if_changed(path, {"provider": provider})
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
        activation_bindings_by_run={run: _binding(
            ("ECMWF", "GFS"),
            selected_provider_by_model={
                "ECMWF": "ecmwf_aifs025_official",
                "GFS": "gfs_0p25_official",
            },
            aifs_provider_oos_binding=True,
        )},
        run_initializations=[run],
        provider_runner=provider_runner,
    )

    assert calls == [
        "ecmwf_aifs025_official",
        "gfs_0p25_official",
    ]
    assert result["state"] == "CAPTURED"
    assert result["attempts"][0]["selected_models"] == ["ECMWF", "GFS"]
    assert result["attempts"][0]["selected_provider_by_model"] == {
        "ECMWF": "ecmwf_aifs025_official",
        "GFS": "gfs_0p25_official",
    }
    ecmwf = next(
        row for row in result["attempts"][0]["providers"]
        if row["model_id"] == "ECMWF"
    )
    assert ecmwf["provider_id"] == "ecmwf_aifs025_official"
    assert ecmwf["selected_provider_id"] == "ecmwf_aifs025_official"
    assert [row["provider_id"] for row in ecmwf["attempts"]] == [
        "ecmwf_aifs025_official",
    ]
    assert all("ecmwf_ifs025_official" not in path for path in result["capture_paths"])
    assert any("ecmwf_aifs025_official" in path for path in result["capture_paths"])


def test_ecmwf_aifs_selection_without_own_oos_binding_fails_before_capture(
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
    calls: list[str] = []
    with pytest.raises(
        scheduler.ContractError,
        match="official_nwp_scheduler_aifs_oos_binding_missing",
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
            activation_bindings_by_run={"2026-07-26T00:00:00Z": _binding(
                ("ECMWF", "GFS"),
                selected_provider_by_model={
                    "ECMWF": "ecmwf_aifs025_official",
                    "GFS": "gfs_0p25_official",
                },
            )},
            run_initializations=["2026-07-26T00:00:00Z"],
            provider_runner=lambda **kwargs: calls.append(kwargs["provider_id"]),
        )
    assert calls == []


def test_ecmwf_aifs_adapter_failure_is_explicit_and_fails_closed(
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
    run = "2026-07-26T00:00:00Z"

    def provider_runner(**kwargs):
        provider = kwargs["provider_id"]
        if provider == "ecmwf_aifs025_official":
            raise scheduler.ContractError(
                "official_nwp_scheduler_aifs_adapter_unavailable"
            )
        if provider == "gfs_0p25_official":
            path = kwargs["output_dir"] / "capture.json"
            write_bounded_json_if_changed(path, {"provider": provider})
            return {
                "blocked_count": 0,
                "records": [{"status": "CAPTURED", "path": str(path)}],
            }
        return {"blocked_count": 1, "records": []}

    result = scheduler.capture_scope(
        registry_snapshot_path=registry_path,
        scope={
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
            "station_ids": ["RKPK"],
        },
        as_of_utc="2026-07-26T02:00:00Z",
        output_root=tmp_path / "capture",
        activation_bindings_by_run={run: _binding(
            ("ECMWF", "GFS"),
            selected_provider_by_model={
                "ECMWF": "ecmwf_aifs025_official",
                "GFS": "gfs_0p25_official",
            },
            aifs_provider_oos_binding=True,
        )},
        run_initializations=[run],
        provider_runner=provider_runner,
    )

    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    ecmwf = next(
        row for row in result["attempts"][0]["providers"]
        if row["model_id"] == "ECMWF"
    )
    assert ecmwf["selected_provider_id"] == ""
    assert ecmwf["attempts"][-1]["provider_id"] == "ecmwf_aifs025_official"
    assert "aifs_adapter_unavailable" in ecmwf["attempts"][-1]["reason"]
    assert all("ecmwf_ifs025_official" not in path for path in result["capture_paths"])
    assert all("ecmwf_aifs025_official" not in path for path in result["capture_paths"])


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


def test_every_contracted_lead_resolves_a_run_for_all_four_systems() -> None:
    # The contract holds a list of cycles per lead because a system may read
    # more than one -- ECCC takes both 00Z and 12Z at d2.  Reading that as a
    # single mapping matched nothing, which emptied the run set for every lead
    # and every date and stopped the capture scheduler outright.
    from bayes_basket_target.official_nwp_capture_scheduler import (
        OFFICIAL_ARCHIVE_SCOPE_CONTRACTS,
        SUPPORTED_LEAD_DAYS,
        contracted_run_inits,
    )

    # Charter 204: AIFS ENS is contracted at every cycle as a follow cycle
    # only, so the schedule names no run for it at any lead.
    readiness_systems = {
        system_id
        for system_id, contract in OFFICIAL_ARCHIVE_SCOPE_CONTRACTS.items()
        if any(
            not entry.get("serving_follow")
            for entries in contract["leads"].values()
            for entry in ([entries] if isinstance(entries, dict) else entries)
        )
    }
    assert readiness_systems == set(OFFICIAL_ARCHIVE_SCOPE_CONTRACTS) - {
        "ECMWF_AIFS_ENS_GLOBAL"
    }
    for lead_days in SUPPORTED_LEAD_DAYS:
        runs = contracted_run_inits("2026-08-12", lead_days=lead_days)
        assert set(runs) == readiness_systems


def test_a_multi_cycle_lead_takes_the_latest_contracted_cycle() -> None:
    # ECCC contracts 00Z and 12Z at d2; the later run assimilates more and is
    # also the conservative readiness bound for the lead.
    from bayes_basket_target.official_nwp_capture_scheduler import (
        contracted_run_inits,
    )

    runs = contracted_run_inits("2026-08-12", lead_days=2)

    assert runs["ECCC_GEM_GLOBAL_ENS"].hour == 12
    assert runs["ECCC_GEM_GLOBAL_ENS"].date().isoformat() == "2026-08-10"


def test_a_single_mapping_lead_still_resolves() -> None:
    # An older contract shape must keep working rather than silently drop the
    # system out of the run set.
    import bayes_basket_target.official_nwp_capture_scheduler as scheduler

    original = scheduler.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS
    scheduler.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS = {
        "LEGACY_SYSTEM": {"leads": {"d1": {"run_cycle": "06Z"}}},
    }
    try:
        runs = scheduler.contracted_run_inits("2026-08-12", lead_days=1)
    finally:
        scheduler.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS = original

    assert runs["LEGACY_SYSTEM"].hour == 6


def test_capture_order_serves_the_furthest_day_first_without_starving_today(
) -> None:
    """The entry decision leads, and every day still advances every poll.

    A d0 capture refreshes a book that already has a P0; a d2 capture is the
    only thing that can open one at all, because nothing else produces that
    day's first probability.  The sort used to put the *soonest* local day
    first, which is exactly backwards for a strategy that enters early.

    Measured 2026-09-01 on the live 267-scope catalogue at 20 scopes a poll:
    d0's 120 scopes were served in the first 50 minutes, d1's 135 in the next
    70, and d2's 12 -- the entry-critical ones -- last, at 120 to 130 minutes.
    Reversing into day blocks only moved the starvation the other way (d0 fell
    to 70-130), so the days are interleaved by forward distance instead.
    """

    rows = []
    stations = [f"S{index:03d}" for index in range(40)]
    # d0 is open, d1 and d2 have not started; all three are inside 72 hours.
    for day in ("2026-07-30", "2026-07-31", "2026-08-01"):
        for station in stations:
            rows.append({
                "station_id": station,
                "event_date": day,
                "settlement_timezone": "UTC",
                "outcome": "mid",
                "source_eligible": 1,
                "market_active": 1,
                "market_accepting_orders": 1,
            })
    scopes = scheduler.registry_capture_scopes(
        {"rows": rows},
        as_of_utc="2026-07-30T16:00:00Z",
    )
    ready = [scope for scope in scopes if scope.get("model_ready")]
    assert ready, "the fixture must contain ready scopes to order"

    first_position: dict[str, int] = {}
    last_position: dict[str, int] = {}
    for index, scope in enumerate(ready):
        day = str(scope["event_date"])
        first_position.setdefault(day, index)
        last_position[day] = index
    days = sorted(first_position)
    assert len(days) >= 2, "the fixture must span more than one day"

    # Every day is under way from the very front of the sweep -- none is held
    # back in a block behind another day.
    poll = 20
    for day in days:
        assert first_position[day] < poll, (
            f"{day} does not appear until position {first_position[day]};"
            " a day served in a block starves while another day drains"
        )
    # And the furthest day finishes first, because it is the one that opens.
    furthest, nearest = days[-1], days[0]
    assert last_position[furthest] < last_position[nearest], (
        f"{furthest} finishes at {last_position[furthest]} and {nearest} at"
        f" {last_position[nearest]} -- the entry-critical day must not be last"
    )


def test_an_unready_scope_still_sorts_behind_every_ready_one() -> None:
    """Readiness outranks worth: an unpublished run cannot be captured."""

    rows = []
    for day in ("2026-07-30", "2026-08-01"):
        rows.append({
            "station_id": "S000",
            "event_date": day,
            "settlement_timezone": "UTC",
            "outcome": "mid",
            "source_eligible": 1,
            "market_active": 1,
            "market_accepting_orders": 1,
        })
    scopes = scheduler.registry_capture_scopes(
        {"rows": rows},
        as_of_utc="2026-07-30T16:00:00Z",
    )
    seen_unready = False
    for scope in scopes:
        if not scope.get("model_ready"):
            seen_unready = True
            continue
        assert not seen_unready, (
            "a ready scope was ordered behind an unready one"
        )
