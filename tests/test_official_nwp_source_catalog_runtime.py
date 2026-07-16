from __future__ import annotations

import json
from pathlib import Path

from official_nwp_source_catalog_runtime import (
    REQUIRED_PROVIDERS,
    load_official_nwp_catalog,
    validate_official_nwp_catalog,
    validate_official_nwp_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json"


def test_official_nwp_catalog_has_exactly_the_four_independent_sources() -> None:
    result = validate_official_nwp_catalog(load_official_nwp_catalog(CATALOG_PATH))

    assert result["ok"] is True, result["issues"]
    assert tuple(result["catalog"]["joint_snapshot_policy"]["provider_universe"]) == REQUIRED_PROVIDERS
    assert result["catalog"]["joint_snapshot_policy"]["missing_provider_policy"] == "propagate_availability_mask_to_unique_weak_likelihood_model"
    assert result["catalog"]["boundaries"]["open_meteo_retired"] is True


def test_snapshot_rejects_aggregator_and_non_official_host() -> None:
    catalog = load_official_nwp_catalog(CATALOG_PATH)
    required = catalog["providers"]["gfs"]["required_normalized_fields"]
    snapshot = {
        "schema": "official_nwp_normalized_snapshot_v1",
        "provider_id": "gfs",
        "source_id": "noaa_nomads_gfs_official",
        "model_id": "gfs_0p25",
        "run_at_utc": "2026-07-12T06:00:00Z",
        "valid_time_utc": "2026-07-12T12:00:00Z",
        "response_completed_at": "2026-07-12T06:20:00Z",
        "raw_object_ref": "/tmp/gfs.grib2.gz",
        "raw_sha256": "abc",
        "raw_request_url": "https://api.open-meteo.com/v1/forecast",
        "decoder_id": "eccodes_subset_v1",
        "decoder_version": "1",
        "normalized_features": {key: 1.0 for key in required},
        "aggregator_or_republisher": True,
        "open_meteo": True,
    }

    result = validate_official_nwp_snapshot(snapshot, provider_id="gfs", catalog=catalog)

    assert result["ok"] is False
    assert "official_nwp_snapshot_raw_request_host_invalid" in result["issues"]
    assert "official_nwp_snapshot_aggregator_not_forbidden" in result["issues"]
    assert "official_nwp_snapshot_open_meteo_retirement_violation" in result["issues"]
