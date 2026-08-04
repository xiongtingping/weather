from __future__ import annotations

from production_probability_v6_runtime_manifest import (
    FORBIDDEN_PRODUCTION_RUNTIME_FILES,
    RUNTIME_FILES,
    build_v6_runtime_manifest,
    v6_runtime_manifest_sha256,
)


def test_manifest_closes_event_and_weak_revision_production_runtime() -> None:
    manifest = build_v6_runtime_manifest()
    paths = {row["path"] for row in manifest["runtime_files"]}
    assert paths == set(RUNTIME_FILES)
    assert "scripts/weather_basket_yes_daemon.py" in paths
    assert "bayes_basket_target/production_worker.py" in paths
    assert "identifiable_physical_probability_worker.py" not in paths
    assert "production_probability_v6_amsc_archive_ingress.py" not in paths
    assert set(paths).isdisjoint(FORBIDDEN_PRODUCTION_RUNTIME_FILES)
    assert manifest["resident_service"] == "com.weather.weather_basket_yes"
    assert manifest["forbidden_runtime_files_reachable"] is False
    assert manifest["shadow_runtime"] is False
    assert manifest["v5_fallback_forbidden"] is True
    assert manifest["places_orders"] is False
    assert manifest["runtime_manifest_sha256"] == v6_runtime_manifest_sha256()
