from __future__ import annotations

import itertools
from pathlib import Path

import pytest

import bayes_basket_target.resident_nwp_producer as producer
from bayes_basket_target import ContractError, content_sha256, model_set_hash
from bayes_basket_target.resource_bounds import (
    MAX_JSON_INPUT_BYTES,
    write_bounded_json_if_changed,
)


MATERIALIZED_1 = "2026-07-27T03:00:00Z"
MATERIALIZED_2 = "2026-07-27T04:00:00Z"
MATERIALIZED_3 = "2026-07-27T05:00:00Z"
SETTLEMENT_CLOSE = "2026-07-27T15:00:00Z"
MODELS = ("ECMWF", "GFS")


def _capture(path: Path, *, decision_at: str, token: str) -> Path:
    write_bounded_json_if_changed(
        path,
        {
            "status": "CAPTURED",
            "token": token,
            "arrival": {"decision_at_utc": decision_at},
        },
    )
    return path


def _decode(
    *,
    capture_paths,
    as_of_utc,
    market_registry_snapshot_path,
):
    assert capture_paths
    assert as_of_utc <= MATERIALIZED_3
    assert market_registry_snapshot_path.name == "registry.json"
    identity = {
        "target_timezone": "Asia/Seoul",
        "decoded_points": [
            {"received_at_utc": "2026-07-27T02:30:00Z"},
            {"received_at_utc": "2026-07-27T02:45:00Z"},
        ],
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


def _ensemble(
    *,
    valid_from: str = "2026-07-27T00:00:00Z",
    valid_until: str = "2026-07-28T00:00:00Z",
) -> dict:
    mask = model_set_hash(MODELS)
    raw = {
        "ensemble_artifact_id": "ensemble-rkpk",
        "station_id": "RKPK",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "candidate_models": list(MODELS),
        "supported_masks": {
            mask: {"ECMWF": 0.6, "GFS": 0.4},
        },
        "residual_covariance": {},
        "correlation_penalty": 0.0,
        "selection_evidence_sha256": "b" * 64,
        "previous_ensemble_artifact_sha256": None,
        "weight_method": "rolling_origin_probability_stack_v4_five_model_pool_min2",
        "effective_sample_n": 30.0,
        "max_weight_delta": 0.2,
        "train_start_date": "2026-05-01",
        "train_end_date": "2026-06-20",
        "recent_start_date": "2026-06-01",
        "recent_end_date": "2026-06-20",
        "holdout_start_date": "2026-06-21",
        "holdout_end_date": "2026-06-30",
        "oos_metrics": {
            "brier_score": 0.2,
            "nll": 0.5,
            "crps": 0.3,
        },
        "valid_from_utc": valid_from,
        "valid_until_utc": valid_until,
        "promoted": False,
    }
    identity = {
        key: raw[key]
        for key in (
            "ensemble_artifact_id",
            "station_id",
            "season_bin",
            "lead_bucket",
            "run_cycle",
            "settlement_definition",
            "candidate_models",
            "supported_masks",
            "residual_covariance",
            "correlation_penalty",
            "selection_evidence_sha256",
            "previous_ensemble_artifact_sha256",
            "weight_method",
            "effective_sample_n",
            "max_weight_delta",
            "train_start_date",
            "train_end_date",
            "recent_start_date",
            "recent_end_date",
            "holdout_start_date",
            "holdout_end_date",
            "oos_metrics",
            "valid_from_utc",
            "valid_until_utc",
            "promoted",
        )
    }
    return {**raw, "content_sha256": content_sha256(identity)}


def _activation(ensemble: dict) -> dict:
    return {
        "activation_id": "activation-rkpk",
        "artifact_kind": "MODEL_ENSEMBLE",
        "artifact_id": ensemble["ensemble_artifact_id"],
        "station_id": "RKPK",
        "model_id_or_star": "*",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "available_model_set_hash_or_star": model_set_hash(MODELS),
        "generation": 1,
        "valid_from_utc": ensemble["valid_from_utc"],
        "valid_until_utc": ensemble["valid_until_utc"],
        "activated_at_utc": ensemble["valid_from_utc"],
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "content_sha256": "c" * 64,
    }


def _packet_builder(
    *,
    run_initialization: str,
    models: tuple[str, ...] = MODELS,
    artifact_valid_until: str = "2026-07-28T00:00:00Z",
):
    def build(**kwargs):
        ensemble = _ensemble(valid_until=artifact_valid_until)
        packet = {
            "stage": "P0",
            "station_id": "RKPK",
            "event_date": kwargs["event_date"],
            "decision_at_utc": kwargs["decision_at_utc"],
            "fresh_until_utc": kwargs["fresh_until_utc"],
            "current_model_runs": [
                {
                    "model_id": model,
                    "run_init_utc": run_initialization,
                    "received_at_utc": "2026-07-27T02:45:00Z",
                }
                for model in models
            ],
            "ensemble_artifact": ensemble,
            "calibration_activations": [_activation(ensemble)],
        }
        row = {
            "source_class": "official_forward_model_bundle",
            "packet_content_sha256": content_sha256(packet),
            "packet": packet,
        }
        identity = {
            "schema": "bayes_basket_target_p0_inbox_v1",
            "generated_at_utc": kwargs["decision_at_utc"],
            "packets": [row],
        }
        inbox = {**identity, "content_sha256": content_sha256(identity)}
        write_bounded_json_if_changed(kwargs["inbox_path"], inbox)
        return {
            "blocked_count": 0,
            "p0_inbox": {
                "state": "updated",
                "content_sha256": inbox["content_sha256"],
                "packet_count": 1,
            },
        }

    return build


def _base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    captures: list[Path] | None = None,
) -> dict:
    registry = tmp_path / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        producer,
        "load_market_registry_snapshot",
        lambda _path: {"snapshot_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        producer,
        "settlement_day_window",
        lambda _day, _tz: {"target_day_end_utc": SETTLEMENT_CLOSE},
    )
    selected = captures or [
        _capture(
            tmp_path / "ecmwf.json",
            decision_at="2026-07-27T02:30:00Z",
            token="ecmwf",
        ),
        _capture(
            tmp_path / "gfs.json",
            decision_at="2026-07-27T02:45:00Z",
            token="gfs",
        ),
    ]
    return {
        "capture_paths": selected,
        "registry_snapshot_path": registry,
        "event_date": "2026-07-27",
        "output_root": tmp_path / "runtime",
        "inbox_path": tmp_path / "p0-inbox.json",
        "db_path": tmp_path / "target.db",
        "decode_builder": _decode,
    }


def test_predecoded_aggregator_or_native_curve_can_join_resident_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {
        "schema": "bayes_basket_target_native_model_arrival_decode_evidence_v1",
        "as_of_utc": MATERIALIZED_1,
        "target_local_date": "2026-07-27",
        "target_timezone": "Asia/Seoul",
        "decoded_points": [{
            "station_id": "RKPK",
            "event_date": "2026-07-27",
            "provider_id": "cma_grapes_global_open_meteo",
            "model_id": "CMA",
            "model_run_initialization_utc": "2026-07-25T12:00:00Z",
            "valid_time_utc": "2026-07-27T00:00:00Z",
            "temperature_2m_c": 30.0,
            "raw_message_sha256": "a" * 64,
            "receipt_sha256": "b" * 64,
            "published_at_utc": "",
            "received_at_utc": "2026-07-26T18:00:00Z",
        }],
        "blocked_captures": [],
        "curves": {"all_complete": True},
        "checks": {},
        "boundary": {
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    artifact = {
        **identity,
        "receipt_sha256": content_sha256(identity),
    }
    artifact_path = tmp_path / "cma-decode.json"
    write_bounded_json_if_changed(artifact_path, artifact)
    monkeypatch.setattr(
        producer,
        "load_market_registry_snapshot",
        lambda _path: {
            "snapshot_sha256": "c" * 64,
            "rows": [{}],
        },
    )
    monkeypatch.setattr(
        producer,
        "registry_settlement_timezone",
        lambda *_args, **_kwargs: "Asia/Seoul",
    )
    monkeypatch.setattr(
        producer,
        "materialize_native_daily_curves",
        lambda points, target_timezone: {
            "all_complete": True,
            "complete_curve_count": 1,
            "eligible_model_rows": list(points),
        },
    )

    result = producer._decode_capture_bundle(
        capture_paths=[artifact_path],
        as_of_utc=MATERIALIZED_1,
        market_registry_snapshot_path=tmp_path / "registry.json",
    )

    assert result["decoded_point_count"] == 1
    assert result["target_timezone"] == "Asia/Seoul"
    assert result["boundary"]["calls_order_gateway"] is False


def test_latest_immature_falls_back_to_previous_mature_two_model_oos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures = [
        _capture(
            tmp_path / f"{index}.json",
            decision_at=f"2026-07-27T02:{index:02d}:00Z",
            token=token,
        )
        for index, token in enumerate(
            ("latest-ecmwf-partial", "latest-icon-partial", "old-ecmwf", "old-gfs")
        )
    ]
    kwargs = _base(tmp_path, monkeypatch, captures=captures)
    result = producer.refresh_resident_p0(
        **kwargs,
        materialized_at_utc=MATERIALIZED_1,
        packet_builder=_packet_builder(
            run_initialization="2026-07-26T00:00:00Z"
        ),
    )

    assert result["state"] == "updated"
    assert result["decision_at_utc"] == MATERIALIZED_1
    assert result["source_bundle_ready_at_utc"] == "2026-07-27T02:45:00Z"
    assert result["fresh_until_utc"] == "2026-07-27T09:00:00Z"
    assert result["selected_bundle_scopes"][0]["model_ids"] == "ECMWF,GFS"
    assert (
        result["selected_bundle_scopes"][0]["run_initialization_utc"]
        == "2026-07-26T00:00:00Z"
    )


def test_default_materialization_clock_uses_capture_cycle_not_wall_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures = [
        _capture(
            tmp_path / f"default-{model}.json",
            decision_at=MATERIALIZED_1,
            token=model,
        )
        for model in MODELS
    ]
    kwargs = _base(tmp_path, monkeypatch, captures=captures)
    result = producer.refresh_resident_p0(
        **kwargs,
        packet_builder=_packet_builder(
            run_initialization="2026-07-26T00:00:00Z"
        ),
    )

    assert result["decision_at_utc"] == MATERIALIZED_1
    assert result["fresh_until_utc"] == "2026-07-27T09:00:00Z"


def test_latest_mature_update_switches_and_same_input_is_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _base(tmp_path, monkeypatch)
    first = producer.refresh_resident_p0(
        **kwargs,
        materialized_at_utc=MATERIALIZED_1,
        packet_builder=_packet_builder(
            run_initialization="2026-07-26T00:00:00Z"
        ),
    )
    state_path = tmp_path / "runtime" / "2026-07-27" / "state.json"
    state_mtime = state_path.stat().st_mtime_ns
    inbox_mtime = (tmp_path / "p0-inbox.json").stat().st_mtime_ns
    same = producer.refresh_resident_p0(
        **kwargs,
        materialized_at_utc=MATERIALIZED_2,
        packet_builder=pytest.fail,
    )
    _capture(
        tmp_path / "icon.json",
        decision_at="2026-07-27T03:30:00Z",
        token="new-mature",
    )
    changed = {
        **kwargs,
        "capture_paths": [*kwargs["capture_paths"], tmp_path / "icon.json"],
    }
    switched = producer.refresh_resident_p0(
        **changed,
        materialized_at_utc=MATERIALIZED_2,
        packet_builder=_packet_builder(
            run_initialization="2026-07-27T00:00:00Z"
        ),
    )

    assert first["state"] == "updated"
    assert same["state"] == "unchanged_zero_write"
    assert state_path.stat().st_mtime_ns > state_mtime
    assert (tmp_path / "p0-inbox.json").stat().st_mtime_ns > inbox_mtime
    assert (
        switched["selected_bundle_scopes"][0]["run_initialization_utc"]
        == "2026-07-27T00:00:00Z"
    )


def test_single_model_and_expired_artifact_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _base(tmp_path, monkeypatch)
    with pytest.raises(ContractError, match="formal_ensemble_minimum"):
        producer.refresh_resident_p0(
            **kwargs,
            materialized_at_utc=MATERIALIZED_1,
            packet_builder=_packet_builder(
                run_initialization="2026-07-26T00:00:00Z",
                models=("ECMWF",),
            ),
        )
    with pytest.raises(
        ContractError,
        match="no_active_ensemble_artifact|ensemble_artifact_not_active",
    ):
        producer.refresh_resident_p0(
            **kwargs,
            materialized_at_utc=MATERIALIZED_1,
            packet_builder=_packet_builder(
                run_initialization="2026-07-26T00:00:00Z",
                artifact_valid_until="2026-07-27T02:59:59Z",
            ),
        )
    assert not (tmp_path / "p0-inbox.json").exists()
    assert not list(
        (tmp_path / "runtime").rglob(".resident-p0-candidate-*.json")
    )


def test_low_high_low_rewind_is_rejected_without_replacing_inbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _base(tmp_path, monkeypatch)
    producer.refresh_resident_p0(
        **kwargs,
        materialized_at_utc=MATERIALIZED_1,
        packet_builder=_packet_builder(
            run_initialization="2026-07-26T00:00:00Z"
        ),
    )
    high_capture = _capture(
        tmp_path / "high.json",
        decision_at="2026-07-27T03:30:00Z",
        token="high",
    )
    high_kwargs = {
        **kwargs,
        "capture_paths": [*kwargs["capture_paths"], high_capture],
    }
    producer.refresh_resident_p0(
        **high_kwargs,
        materialized_at_utc=MATERIALIZED_2,
        packet_builder=_packet_builder(
            run_initialization="2026-07-27T00:00:00Z"
        ),
    )
    before = (tmp_path / "p0-inbox.json").read_bytes()
    low_again = _capture(
        tmp_path / "low-again.json",
        decision_at="2026-07-27T04:30:00Z",
        token="low-again",
    )
    with pytest.raises(ContractError, match="selected_run_rewind"):
        producer.refresh_resident_p0(
            **{
                **kwargs,
                "capture_paths": [*kwargs["capture_paths"], low_again],
            },
            materialized_at_utc=MATERIALIZED_3,
            packet_builder=_packet_builder(
                run_initialization="2026-07-26T00:00:00Z"
            ),
        )
    assert (tmp_path / "p0-inbox.json").read_bytes() == before


def test_capture_count_size_packet_count_and_freshness_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures = [
        _capture(
            tmp_path / f"capture-{index}.json",
            decision_at="2026-07-27T02:00:00Z",
            token=str(index),
        )
        for index in range(3)
    ]
    monkeypatch.setattr(producer, "MAX_INPUT_FILES_PER_BATCH", 2)
    with pytest.raises(ContractError, match="capture_count_invalid"):
        producer.capture_set_identity(
            captures,
            registry_snapshot_sha256="a" * 64,
            event_date="2026-07-27",
        )

    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_JSON_INPUT_BYTES + 1)
    with pytest.raises(ContractError, match="capture_size_invalid"):
        producer.capture_set_identity(
            [oversized],
            registry_snapshot_sha256="a" * 64,
            event_date="2026-07-27",
        )

    monkeypatch.setattr(producer, "MAX_INPUT_FILES_PER_BATCH", 500)
    kwargs = _base(tmp_path, monkeypatch, captures=captures[:2])
    with pytest.raises(ContractError, match="freshness_policy_invalid"):
        producer.refresh_resident_p0(
            **kwargs,
            materialized_at_utc=MATERIALIZED_1,
            freshness_seconds=producer.MAX_P0_FRESHNESS_SECONDS + 1,
            packet_builder=_packet_builder(
                run_initialization="2026-07-26T00:00:00Z"
            ),
        )

    packet = {
        "source_class": "official_forward_model_bundle",
        "packet": {},
    }
    packet["packet_content_sha256"] = content_sha256(packet["packet"])
    identity = {
        "schema": "bayes_basket_target_p0_inbox_v1",
        "generated_at_utc": MATERIALIZED_1,
        "packets": list(
            itertools.repeat(packet, producer.MAX_P0_INBOX_PACKETS + 1)
        ),
    }
    with pytest.raises(ContractError, match="packet_count_invalid"):
        producer._validated_inbox(
            {**identity, "content_sha256": content_sha256(identity)},
            materialized_at_utc=MATERIALIZED_1,
            fresh_until_utc="2026-07-27T09:00:00Z",
        )


def test_materialization_after_settlement_close_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _base(tmp_path, monkeypatch)
    with pytest.raises(
        ContractError,
        match="materialization_after_settlement_close",
    ):
        producer.refresh_resident_p0(
            **kwargs,
            materialized_at_utc=SETTLEMENT_CLOSE,
            packet_builder=_packet_builder(
                run_initialization="2026-07-26T00:00:00Z"
            ),
        )
