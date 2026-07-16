import json
from pathlib import Path

from ncei_zuuu_multimodel_v31_runtime import build_v31_multimodel_posterior
from scripts import train_ncei_zuuu_multimodel_allday_bayes_v31 as legacy_trainer


def _legacy_artifact(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": legacy_trainer.MODEL_SCHEMA,
                "version": "v3.1_multimodel_allday",
                "production_ready": True,
                "source_contract": {"core_models": {"gfs": "gfs_seamless", "jma": "jma_gsm"}},
            }
        ),
        encoding="utf-8",
    )
    return path


def _official_bundle() -> dict:
    return {
        "schema": "official_nwp_multimodel_live_bundle_v1",
        "state": "available",
        "catalog_id": "official_nwp_zuuu_v1",
        "open_meteo": False,
        "aggregator_or_republisher": False,
        "response_completed_at": "2026-07-10T16:00:30Z",
        "providers": {},
    }


def test_retired_open_meteo_input_is_not_a_compatibility_path(tmp_path: Path) -> None:
    result = build_v31_multimodel_posterior(
        buckets=["31°C or below", "32°C"],
        observation_inputs={"v31_live_multimodel_snapshot": {"schema": "open_meteo_sichuan_multimodel_live_bundle_v31"}},
        as_of="2026-07-10T16:01:00Z",
        event_date="2026-07-11",
        artifact_path=_legacy_artifact(tmp_path / "legacy.json"),
    )

    assert result["state"] == "unavailable"
    assert result["state_reason"] == "official_nwp_generation_missing"


def test_official_generation_cannot_be_interpreted_by_legacy_coefficients(tmp_path: Path) -> None:
    result = build_v31_multimodel_posterior(
        buckets=["31°C or below", "32°C"],
        observation_inputs={"official_nwp_generation": _official_bundle()},
        as_of="2026-07-10T16:01:00Z",
        event_date="2026-07-11",
        artifact_path=_legacy_artifact(tmp_path / "legacy.json"),
    )

    assert result["state"] == "unavailable"
    assert result["state_reason"] == "official_nwp_artifact_not_approved"
