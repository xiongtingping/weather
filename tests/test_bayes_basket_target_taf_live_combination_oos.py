from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from bayes_basket_target.contracts import (
    ContractError,
    FORMAL_ENSEMBLE_METHOD_VERSION,
    content_sha256,
)
from bayes_basket_target.taf_live_combination_oos import (
    CANDIDATE_SCHEMA,
    EVIDENCE_SCHEMA,
    evaluate_taf_live_combination_oos,
)


ROOT = Path(__file__).resolve().parents[1]


def _inbox() -> dict:
    inbox = json.loads(
        (ROOT / "data" / "bayes_basket_target_p0_inbox.json").read_text()
    )
    # The retained inbox is an audit fixture from the v3 selector. Rebind its
    # synthetic copy to the current formal contract; never mutate or promote
    # the retained historical artifact itself.
    wrapper = inbox["packets"][0]
    packet = wrapper["packet"]
    packet["station_id"] = "RKPK"
    ensemble = packet["ensemble_artifact"]
    ensemble["station_id"] = "RKPK"
    ensemble["season_bin"] = "summer"
    ensemble["lead_bucket"] = "d2_fallback"
    ensemble["run_cycle"] = "18Z"
    ensemble["weight_method"] = FORMAL_ENSEMBLE_METHOD_VERSION
    supported_mask = next(iter(ensemble["supported_masks"]))
    ensemble["supported_masks"] = {
        supported_mask: {"ECMWF": 0.4, "GFS": 0.6}
    }
    for model, calibration in packet["calibration_artifacts"].items():
        calibration["station_id"] = "RKPK"
        calibration["model_id"] = model
        calibration["season_bin"] = "summer"
        calibration["lead_bucket"] = "d2_fallback"
        calibration["run_cycle"] = "18Z"
        calibration["content_sha256"] = content_sha256(
            {
                key: value
                for key, value in calibration.items()
                if key != "content_sha256"
            }
        )
    ensemble["content_sha256"] = content_sha256(
        {
            key: value
            for key, value in ensemble.items()
            if key != "content_sha256"
        }
    )
    wrapper["packet_content_sha256"] = content_sha256(packet)
    inbox["content_sha256"] = content_sha256(
        {
            key: value
            for key, value in inbox.items()
            if key != "content_sha256"
        }
    )
    return inbox


def _observations(n: int = 32) -> list[dict]:
    rows = []
    for index in range(n):
        event_date = (date(2026, 5, 1) + timedelta(days=index)).isoformat()
        prior_date = (date.fromisoformat(event_date) - timedelta(days=2)).isoformat()
        close_date = (date.fromisoformat(event_date) + timedelta(days=1)).isoformat()
        truth = 26.0 + (index % 5) * 0.4
        rows.append({
            "station_id": "RKPK",
            "event_date": event_date,
            "settlement_timezone": "Asia/Seoul",
            "calibration_scope": {"season_bin": "summer", "lead_bucket": "d2_fallback", "run_cycle": "18Z"},
            "settlement_window": {"end_utc": f"{close_date}T15:00:00Z"},
            "decision_anchor_utc": f"{prior_date}T12:00:00Z",
            "taf": {"tx_c": truth + 0.1, "cloud_cover_fraction": 0.3 + (index % 3) * 0.1, "precipitation_probability": 0.1 + (index % 4) * 0.1, "wind_speed_mps": 3.0},
            "truth": {"tmax_c": truth},
            "nwp_combination": {
                "model_forecasts_c": {
                    "ECMWF": truth - 0.2,
                    "GFS": truth + 0.3,
                },
                "historical_model_forecast_lineage_sha256": {
                    "ECMWF": "1" * 64,
                    "GFS": "2" * 64,
                },
                "selected_models": ["ECMWF", "GFS"],
                "selection_evidence_sha256": "3" * 64,
                "prediction_oos": True,
                "decision_at_utc": f"{prior_date}T12:00:00Z",
            },
        })
    return rows


def test_actual_live_lineage_enters_candidate_and_nested_rows() -> None:
    inbox = _inbox()
    report = evaluate_taf_live_combination_oos(observations=_observations(), live_p0_inbox=inbox, station_id="RKPK", season_bin="summer", lead_bucket="d2_fallback", run_cycle="18Z", minimum_sample_n=10)
    assert report["candidate_only"] is True
    assert report["live_training_contract"]["model_mask"] == ["ECMWF", "GFS"]
    assert report["live_training_contract"]["resolved_weights"] == {
        "ECMWF": 0.4,
        "GFS": 0.6,
    }
    assert report["live_training_contract"]["selection_evidence_sha256"]
    assert set(report["live_training_contract"]["calibration_lineage"]) == {"ECMWF", "GFS"}
    for row in report["evaluated_rows"]:
        assert row["nested_train_end_date"] < row["event_date"]
        assert row["nested_weights"] != report["live_training_contract"]["resolved_weights"] or row["nested_train_n"] == 3
    assert report["candidate_artifact"]["schema"] == CANDIDATE_SCHEMA
    assert report["candidate_artifact"]["activated"] is False


def test_final_live_weights_are_not_replayed_on_outer_labels() -> None:
    report = evaluate_taf_live_combination_oos(observations=_observations(), live_p0_inbox=_inbox(), station_id="RKPK", season_bin="summer", lead_bucket="d2_fallback", run_cycle="18Z", minimum_sample_n=10)
    assert report["nested_oos"]["final_live_weights_replayed_on_outer_labels"] is False
    assert report["nested_oos"]["online_weight_update"] is False
    assert all("nested_weights" in row for row in report["evaluated_rows"])


def test_one_model_live_mask_is_rejected() -> None:
    inbox = _inbox()
    wrapper = inbox["packets"][0]
    packet = wrapper["packet"]
    packet["calibration_artifacts"].pop("GFS")
    ensemble = packet["ensemble_artifact"]
    ensemble["candidate_models"] = ["ECMWF"]
    ensemble["supported_masks"] = {}
    wrapper["packet_content_sha256"] = content_sha256(packet)
    inbox["content_sha256"] = content_sha256({key: value for key, value in inbox.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="at_least_two|model_set|ensemble"):
        evaluate_taf_live_combination_oos(observations=_observations(), live_p0_inbox=inbox, station_id="RKPK", season_bin="summer", lead_bucket="d2_fallback", run_cycle="18Z", minimum_sample_n=10)


def test_fingerprint_tampering_is_rejected() -> None:
    inbox = _inbox()
    packet = inbox["packets"][0]["packet"]
    packet["taf_live_combination_fingerprint"] = {"schema": "tampered"}
    inbox["packets"][0]["packet_content_sha256"] = content_sha256(packet)
    inbox["content_sha256"] = content_sha256({key: value for key, value in inbox.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="fingerprint"):
        evaluate_taf_live_combination_oos(observations=_observations(), live_p0_inbox=inbox, station_id="RKPK", season_bin="summer", lead_bucket="d2_fallback", run_cycle="18Z", minimum_sample_n=10)


def test_insufficient_outer_rows_remains_candidate_na() -> None:
    report = evaluate_taf_live_combination_oos(observations=_observations(8), live_p0_inbox=_inbox(), station_id="RKPK", season_bin="summer", lead_bucket="d2_fallback", run_cycle="18Z", minimum_sample_n=24)
    assert report["state"] == "NA"
    assert report["validated"] is False
    assert report["bridge_evidence"] is None
    assert report["candidate_only"] is True


def test_model_publication_after_combination_as_of_is_rejected() -> None:
    rows = _observations()
    rows[0]["nwp_combination"]["model_rows"] = [{
        "model_id": "ECMWF",
        "forecast_daily_max_c": 26.0,
        "published_at_utc": "2026-05-03T00:00:00Z",
    }, {
        "model_id": "GFS",
        "forecast_daily_max_c": 26.0,
        "published_at_utc": "2026-05-03T00:00:00Z",
    }]
    with pytest.raises(ContractError, match="as_of|decision"):
        evaluate_taf_live_combination_oos(observations=rows, live_p0_inbox=_inbox(), station_id="RKPK", season_bin="summer", lead_bucket="d2_fallback", run_cycle="18Z", minimum_sample_n=10)


def test_missing_explicit_oos_or_direct_lineage_is_rejected() -> None:
    rows = _observations()
    rows[0]["nwp_combination"].pop("prediction_oos")
    with pytest.raises(ContractError, match="not_oos"):
        evaluate_taf_live_combination_oos(
            observations=rows,
            live_p0_inbox=_inbox(),
            station_id="RKPK",
            season_bin="summer",
            lead_bucket="d2_fallback",
            run_cycle="18Z",
            minimum_sample_n=10,
        )

    rows = _observations()
    rows[0]["nwp_combination"].pop(
        "historical_model_forecast_lineage_sha256"
    )
    with pytest.raises(ContractError, match="lineage_missing"):
        evaluate_taf_live_combination_oos(
            observations=rows,
            live_p0_inbox=_inbox(),
            station_id="RKPK",
            season_bin="summer",
            lead_bucket="d2_fallback",
            run_cycle="18Z",
            minimum_sample_n=10,
        )
