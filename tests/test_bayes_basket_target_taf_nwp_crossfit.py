from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from bayes_basket_target.contracts import (
    ContractError,
    FORMAL_ENSEMBLE_METHOD_VERSION,
    content_sha256,
    model_set_hash,
)
from bayes_basket_target.taf_nwp_crossfit import build_nwp_crossfit_predictions


def _inputs(tmp_path: Path) -> tuple[dict, list[Path], Path]:
    raw_root = tmp_path / "raw"
    first = date(2026, 7, 1)
    samples = []
    packets = []
    for model, provider, offset in (
        ("ECMWF", "ecmwf_ifs025_official", -0.3),
        ("GFS", "gfs_0p25_official", 0.4),
    ):
        rows = []
        for index in range(8):
            day = first + timedelta(days=index)
            body = f"{model}-{day}".encode()
            sha = hashlib.sha256(body).hexdigest()
            path = raw_root / provider / "raw" / f"{sha}.grib2"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            rows.append({
                "station_id": "RKPK",
                "event_date": day.isoformat(),
                "provider_id": provider,
                "target_timezone": "Asia/Seoul",
                "daily_curve_complete": True,
                "forecast_daily_max_c": 30.0 + index + offset,
                "published_at_utc": f"{(day - timedelta(days=1)).isoformat()}T00:30:00Z",
                "model_run_initialization_utc": (
                    f"{(day - timedelta(days=1)).isoformat()}T00:00:00Z"
                ),
                "model_run_lineage_sha256": hashlib.sha256(
                    f"run-{model}-{day}".encode()
                ).hexdigest(),
                "component_raw_message_sha256": [sha],
            })
            if model == "ECMWF":
                samples.append({
                    "station_id": "RKPK",
                    "event_date": day.isoformat(),
                    "decision_anchor_utc": f"{(day - timedelta(days=1)).isoformat()}T16:00:00Z",
                    "settlement_timezone": "Asia/Seoul",
                    "truth": {"tmax_c": 30.0 + index},
                })
        packet_path = tmp_path / f"{model}.json"
        packet_path.write_text(json.dumps({"model_rows": rows}), encoding="utf-8")
        packets.append(packet_path)
    return {"samples": samples}, packets, raw_root


def _ensemble(
    *,
    lead_bucket: str = "d2",
    run_cycle: str = "00Z",
) -> dict:
    models = ("ECMWF", "GFS")
    identity = {
        "ensemble_artifact_id": "ens-rkpk-test-v4",
        "station_id": "RKPK",
        "season_bin": "summer",
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "settlement_definition": "official_daily_tmax_c",
        "candidate_models": list(models),
        "supported_masks": {
            model_set_hash(models): {"ECMWF": 0.65, "GFS": 0.35},
        },
        "residual_covariance": {
            "ECMWF": {"ECMWF": 1.0, "GFS": 0.2},
            "GFS": {"ECMWF": 0.2, "GFS": 1.0},
        },
        "correlation_penalty": 0.1,
        "selection_evidence_sha256": "a" * 64,
        "previous_ensemble_artifact_sha256": None,
        "weight_method": FORMAL_ENSEMBLE_METHOD_VERSION,
        "effective_sample_n": 20.0,
        "max_weight_delta": 0.2,
        "train_start_date": "2026-06-01",
        "train_end_date": "2026-06-20",
        "recent_start_date": "2026-06-14",
        "recent_end_date": "2026-06-20",
        "holdout_start_date": "2026-06-21",
        "holdout_end_date": "2026-06-28",
        "oos_metrics": {
            "brier_score": 0.2,
            "nll": 0.4,
            "crps": 0.15,
        },
        "valid_from_utc": "2026-06-29T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "promoted": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_crossfit_uses_only_prior_days_and_minimum_two_models(tmp_path: Path) -> None:
    taf, packets, raw_root = _inputs(tmp_path)
    result = build_nwp_crossfit_predictions(
        taf_dataset=taf,
        evidence_paths=packets,
        raw_root=raw_root,
        lead_bucket="d2",
        run_cycle="00Z",
        ensemble_artifact=_ensemble(),
    )
    assert result["prediction_count"] == 5
    assert result["method_version"] == FORMAL_ENSEMBLE_METHOD_VERSION
    assert result["excluded_burn_in_dates"] == [
        "2026-07-01", "2026-07-02", "2026-07-03"
    ]
    assert result["predictions"][0]["train_end_date"] == "2026-07-03"
    assert result["predictions"][0]["event_date"] == "2026-07-04"
    assert all(
        row["selected_models"] == ["ECMWF", "GFS"]
        and row["prediction_oos"] is True
        for row in result["predictions"]
    )
    assert {
        (row["lead_bucket"], row["run_cycle"])
        for row in result["predictions"]
    } == {("d2", "00Z")}
    for row in result["predictions"]:
        assert sum(row["selected_weights"].values()) == pytest.approx(1.0)
        assert all(weight > 0.0 for weight in row["selected_weights"].values())
    for previous, current in zip(
        result["predictions"],
        result["predictions"][1:],
    ):
        assert max(
            abs(
                current["selected_weights"][model]
                - previous["selected_weights"][model]
            )
            for model in ("ECMWF", "GFS")
        ) <= 0.20 + 1e-12


def test_crossfit_rejects_tampered_retained_raw(tmp_path: Path) -> None:
    taf, packets, raw_root = _inputs(tmp_path)
    target = next(raw_root.rglob("*.grib2"))
    target.write_bytes(b"tampered")
    with pytest.raises(ContractError, match="raw_object_verification_failed"):
        build_nwp_crossfit_predictions(
            taf_dataset=taf,
            evidence_paths=packets,
            raw_root=raw_root,
            lead_bucket="d2",
            run_cycle="00Z",
            ensemble_artifact=_ensemble(),
        )


def test_crossfit_rejects_declared_cycle_drift(tmp_path: Path) -> None:
    taf, packets, raw_root = _inputs(tmp_path)
    with pytest.raises(ContractError, match="model_run_cycle_mismatch"):
        build_nwp_crossfit_predictions(
            taf_dataset=taf,
            evidence_paths=packets,
            raw_root=raw_root,
            lead_bucket="d2_fallback",
            run_cycle="18Z",
            ensemble_artifact=_ensemble(
                lead_bucket="d2_fallback",
                run_cycle="18Z",
            ),
        )


def test_crossfit_rejects_ensemble_hash_and_scope_drift(
    tmp_path: Path,
) -> None:
    taf, packets, raw_root = _inputs(tmp_path)
    tampered = {**_ensemble(), "candidate_models": ["ECMWF", "ICON"]}
    with pytest.raises(
        ContractError,
        match="ensemble_content_sha256_mismatch",
    ):
        build_nwp_crossfit_predictions(
            taf_dataset=taf,
            evidence_paths=packets,
            raw_root=raw_root,
            lead_bucket="d2",
            run_cycle="00Z",
            ensemble_artifact=tampered,
        )
