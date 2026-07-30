from __future__ import annotations

import copy

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.settlement_time import settlement_day_window
from bayes_basket_target.taf_nwp_transport_oos import (
    build_nested_history_prefix,
)


def _taf_dataset() -> dict:
    samples = []
    for day, truth in (
        ("2026-06-28", 27.0),
        ("2026-06-29", 28.0),
        ("2026-06-30", 29.0),
    ):
        window = settlement_day_window(day, "Asia/Seoul")
        samples.append({
            "station_id": "RKPK",
            "event_date": day,
            "settlement_timezone": "Asia/Seoul",
            "settlement_window": {
                "start_utc": window["target_day_start_utc"],
                "end_utc": window["target_day_end_utc"],
            },
            "truth": {"tmax_c": truth},
        })
    identity = {
        "schema": "bayes_basket_awc_taf_calibration_dataset_v1",
        "source_class": "official_date_addressed_historical_replay",
        "station_id": "RKPK",
        "settlement_timezone": "Asia/Seoul",
        "sample_count": 3,
        "samples": samples,
        "blockers": [],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _evidence(provider: str, values: tuple[float, float, float]) -> dict:
    return {
        "provider_id": provider,
        "model_rows": [
            {
                "provider_id": provider,
                "station_id": "RKPK",
                "event_date": day,
                "target_timezone": "Asia/Seoul",
                "daily_curve_complete": True,
                "forecast_daily_max_c": value,
            }
            for day, value in zip(
                ("2026-06-28", "2026-06-29", "2026-06-30"),
                values,
            )
        ],
    }


def test_history_prefix_is_exact_scoped_and_content_addressed() -> None:
    rows = build_nested_history_prefix(
        taf_dataset=_taf_dataset(),
        historical_evidence=(
            _evidence("ecmwf_ifs025_official", (26.0, 27.0, 28.0)),
            _evidence("gfs_0p25_official", (27.0, 28.0, 29.0)),
        ),
        station_id="RKPK",
        settlement_timezone="Asia/Seoul",
        models=("ECMWF", "GFS"),
    )

    assert [row["event_date"] for row in rows] == [
        "2026-06-28",
        "2026-06-29",
        "2026-06-30",
    ]
    assert rows[0]["historical_model_forecasts_c"] == {
        "ECMWF": 26.0,
        "GFS": 27.0,
    }
    assert len(rows[0]["taf_dataset_content_sha256"]) == 64
    assert set(rows[0]["model_evidence_content_sha256"]) == {
        "ECMWF",
        "GFS",
    }


def test_history_prefix_rejects_registry_timezone_drift() -> None:
    dataset = _taf_dataset()
    identity = {
        key: value
        for key, value in dataset.items()
        if key != "content_sha256"
    }
    identity["settlement_timezone"] = "America/New_York"
    tampered = {
        **identity,
        "content_sha256": content_sha256(identity),
    }
    with pytest.raises(
        ContractError,
        match="history_prefix_taf_scope_invalid",
    ):
        build_nested_history_prefix(
            taf_dataset=tampered,
            historical_evidence=(
                _evidence("ecmwf_ifs025_official", (26.0, 27.0, 28.0)),
                _evidence("gfs_0p25_official", (27.0, 28.0, 29.0)),
            ),
            station_id="RKPK",
            settlement_timezone="Asia/Seoul",
            models=("ECMWF", "GFS"),
        )


def test_history_prefix_rejects_conflicting_duplicate_model_rows() -> None:
    ecmwf = _evidence(
        "ecmwf_ifs025_official",
        (26.0, 27.0, 28.0),
    )
    conflict = copy.deepcopy(ecmwf)
    conflict["model_rows"][0]["forecast_daily_max_c"] = 99.0
    with pytest.raises(
        ContractError,
        match="history_prefix_model_content_conflict",
    ):
        build_nested_history_prefix(
            taf_dataset=_taf_dataset(),
            historical_evidence=(
                ecmwf,
                conflict,
                _evidence("gfs_0p25_official", (27.0, 28.0, 29.0)),
            ),
            station_id="RKPK",
            settlement_timezone="Asia/Seoul",
            models=("ECMWF", "GFS"),
        )
