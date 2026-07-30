from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.taf_exact_join import build_taf_exact_join


def _taf() -> dict:
    return {
        "schema": "bayes_basket_awc_taf_calibration_dataset_v1",
        "samples": [{
            "sample_id": "taf-1",
            "station_id": "RKPK",
            "event_date": "2026-07-20",
            "decision_anchor_utc": "2026-07-19T16:00:00Z",
            "settlement_timezone": "Asia/Seoul",
            "settlement_window": {
                "start_utc": "2026-07-19T15:00:00Z",
                "end_utc": "2026-07-20T15:00:00Z",
            },
            "taf": {
                "issue_time_utc": "2026-07-19T11:00:00Z",
                "available_time_utc": "2026-07-19T11:05:00Z",
                "tx_c": 31.0,
                "cloud_cover_fraction": 0.25,
                "precipitation_probability": 0.1,
                "wind_speed_mps": 4.0,
                "raw_taf_sha256": "e" * 64,
            },
            "truth": {
                "source_lane": "AWC",
                "tmax_c": 30.0,
                "observation_count": 24,
                "observations": [{"raw_report": "must-not-be-copied"}],
            },
            "receipts": {
                "taf_response_sha256": "f" * 64,
                "metar_response_sha256": "1" * 64,
            },
        }],
    }


def _nwp() -> dict:
    sha = "a" * 64
    return {
        "schema": "bayes_basket_nwp_crossfit_predictions_v1",
        "crossfit_prediction_artifact_sha256": sha,
        "predictions": [{
            "station_id": "RKPK",
            "event_date": "2026-07-20",
            "decision_at_utc": "2026-07-19T10:30:00Z",
            "settlement_timezone": "Asia/Seoul",
            "settlement_definition": "official_daily_tmax_c",
            "season_bin": "summer",
            "lead_bucket": "d2_fallback",
            "run_cycle": "18Z",
            "prediction_oos": True,
            "crossfit_prediction_artifact_sha256": sha,
            "selection_evidence_sha256": "b" * 64,
            "selected_models": ["ECMWF", "GFS"],
            "selected_weights": {"ECMWF": 0.7, "GFS": 0.3},
            "combined_tmax_c": 30.4,
            "model_rows": [
                {
                    "model_id": model,
                    "model_run_id": f"{model}-run",
                    "published_at_utc": "2026-07-19T10:00:00Z",
                    "available_at_utc": "2026-07-19T10:05:00Z",
                    "availability_class": "native_receipt",
                    "source_object_sha256": value * 64,
                    "raw_object_ref": f"/retained/{model}.bin",
                    "raw_object_verified": True,
                }
                for model, value in (("ECMWF", "c"), ("GFS", "d"))
            ],
        }],
    }


def test_exact_join_materializes_conditional_taf_sample() -> None:
    result = build_taf_exact_join(_taf(), _nwp())
    assert result["sample_count"] == 1
    assert result["blockers"] == []
    sample = result["samples"][0]
    assert sample["nwp_combination"]["prediction_oos"] is True
    assert sample["nwp_combination"]["selected_models"] == ["ECMWF", "GFS"]
    assert sample["calibration_scope"]["settlement_definition"] == "official_daily_tmax_c"
    assert "observations" not in sample["truth"]
    assert "raw_taf" not in sample["taf"]
    assert result["boundary"]["calls_order_gateway"] is False


@pytest.mark.parametrize(
    ("mutate", "reason"),
    (
        (lambda row: row.update({"settlement_timezone": "America/New_York"}), "settlement_timezone_mismatch"),
        (lambda row: row.update({"decision_at_utc": "2026-07-19T11:06:00Z"}), "nwp_combination_after_taf_arrival"),
        (lambda row: row.update({"prediction_oos": False}), "nwp_oos_required"),
        (lambda row: row.update({"selected_models": ["ECMWF"], "selected_weights": {"ECMWF": 1.0}, "model_rows": row["model_rows"][:1]}), "minimum_two_models_required"),
        (lambda row: row["model_rows"][0].update({"available_at_utc": "2026-07-19T16:01:00Z"}), "model_available_after_decision"),
        (lambda row: row["model_rows"][0].update({"raw_object_verified": False}), "raw_object_verification_required"),
    ),
)
def test_exact_join_fail_closed_per_event(mutate, reason: str) -> None:
    nwp = deepcopy(_nwp())
    mutate(nwp["predictions"][0])
    result = build_taf_exact_join(_taf(), nwp)
    assert result["sample_count"] == 0
    assert reason in result["blockers"][0]["reason"]


def test_exact_join_accepts_explicit_historical_publication_without_fake_arrival() -> None:
    nwp = _nwp()
    for model_row in nwp["predictions"][0]["model_rows"]:
        model_row["availability_class"] = (
            "official_publication_schedule_historical_replay"
        )
        model_row["available_at_utc"] = None
    result = build_taf_exact_join(_taf(), nwp)
    assert result["sample_count"] == 1
    assert all(
        row["available_at_utc"] is None
        for row in result["samples"][0]["nwp_combination"]["model_rows"]
    )


def test_exact_join_rejects_resource_overflow() -> None:
    taf = _taf()
    taf["samples"] = taf["samples"] * 2
    with pytest.raises(ContractError, match="resource_bound_exceeded"):
        build_taf_exact_join(taf, _nwp(), max_rows=1)
