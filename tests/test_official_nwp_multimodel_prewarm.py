from __future__ import annotations

import json
from pathlib import Path

from official_nwp_multimodel_prewarm import (
    BUNDLE_SCHEMA,
    build_official_nwp_prewarm_plan,
    generation_reference,
    persist_official_nwp_prewarm,
    validate_generation_reference,
)
from official_nwp_source_catalog_runtime import load_official_nwp_catalog


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json"


def _snapshot(provider_id: str, provider: dict) -> dict:
    return {
        "schema": "official_nwp_normalized_snapshot_v1",
        "provider_id": provider_id,
        "source_id": provider["source_id"],
        "model_id": provider["model_id"],
        "run_at_utc": "2026-07-12T06:00:00Z",
        "valid_time_utc": "2026-07-12T12:00:00Z",
        "response_completed_at": "2026-07-12T07:45:05Z",
        "raw_object_ref": f"/tmp/{provider_id}.grib2.gz",
        "raw_sha256": "sha256",
        "raw_request_url": f"https://{provider['allowed_hosts'][0]}/official.grib2",
        "decoder_id": "eccodes_grib2_subset_v1",
        "decoder_version": "2.0",
        "normalized_features": {field: 1.0 for field in provider["required_normalized_fields"]},
        "aggregator_or_republisher": False,
        "open_meteo": False,
    }


def test_official_prewarm_freezes_complete_source_set_and_missingness_mask(tmp_path: Path) -> None:
    catalog = load_official_nwp_catalog(CATALOG)
    source_root = tmp_path / "sources"
    for provider_id, provider in catalog["providers"].items():
        path = source_root / provider_id / "current.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_snapshot(provider_id, provider)), encoding="utf-8")
    cache = tmp_path / "cache" / "current.json"

    plan = build_official_nwp_prewarm_plan(
        catalog_path=CATALOG,
        source_root=source_root,
        cache_path=cache,
        as_of="2026-07-12T07:46:00Z",
    )
    result = persist_official_nwp_prewarm(plan=plan)

    assert plan["ok"] is True, plan["blockers"]
    assert result["ok"] is True
    bundle = json.loads(cache.read_text(encoding="utf-8"))
    assert bundle["schema"] == BUNDLE_SCHEMA
    assert bundle["available_provider_ids"] == ["ecmwf", "icon", "gfs", "jma"]
    assert bundle["missing_provider_ids"] == []
    assert set(bundle["providers"]) == {"ecmwf", "icon", "gfs", "jma"}
    assert bundle["open_meteo"] is False


def test_official_prewarm_keeps_partial_generation_when_one_provider_is_missing(tmp_path: Path) -> None:
    catalog = load_official_nwp_catalog(CATALOG)
    source_root = tmp_path / "sources"
    for provider_id, provider in catalog["providers"].items():
        if provider_id == "jma":
            continue
        path = source_root / provider_id / "current.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_snapshot(provider_id, provider)), encoding="utf-8")

    plan = build_official_nwp_prewarm_plan(
        catalog_path=CATALOG,
        source_root=source_root,
        cache_path=tmp_path / "cache" / "current.json",
        as_of="2026-07-12T07:46:00Z",
    )

    assert plan["ok"] is True, plan["blockers"]
    bundle = plan["bundle"]
    assert bundle["available_provider_ids"] == ["ecmwf", "icon", "gfs"]
    assert bundle["missing_provider_ids"] == ["jma"]
    assert bundle["weak_likelihood_availability"]["nwp_information_state"] == "available_subset"


def test_official_prewarm_emits_empty_nwp_generation_for_other_weak_source_combinations(tmp_path: Path) -> None:
    plan = build_official_nwp_prewarm_plan(
        catalog_path=CATALOG,
        source_root=tmp_path / "sources",
        cache_path=tmp_path / "cache" / "current.json",
        as_of="2026-07-12T07:46:00Z",
    )

    assert plan["ok"] is True, plan["blockers"]
    bundle = plan["bundle"]
    assert bundle["available_provider_ids"] == []
    assert bundle["missing_provider_ids"] == ["ecmwf", "icon", "gfs", "jma"]
    assert bundle["weak_likelihood_availability"]["nwp_information_state"] == "no_nwp_available"


def test_frozen_partial_generation_reference_is_valid_without_four_source_fallback(tmp_path: Path) -> None:
    catalog = load_official_nwp_catalog(CATALOG)
    source_root = tmp_path / "sources"
    for provider_id in ("ecmwf", "icon", "gfs"):
        path = source_root / provider_id / "current.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_snapshot(provider_id, catalog["providers"][provider_id])), encoding="utf-8")
    cache = tmp_path / "cache" / "current.json"
    result = persist_official_nwp_prewarm(
        plan=build_official_nwp_prewarm_plan(
            catalog_path=CATALOG,
            source_root=source_root,
            cache_path=cache,
            as_of="2026-07-12T07:46:00Z",
        )
    )

    assert result["ok"] is True
    validation = validate_generation_reference(generation_reference(json.loads(cache.read_text(encoding="utf-8"))), catalog_path=CATALOG)
    assert validation["ok"] is True, validation["issues"]
    assert validation["bundle"]["available_provider_ids"] == ["ecmwf", "icon", "gfs"]
