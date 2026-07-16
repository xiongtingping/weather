from __future__ import annotations

from production_probability_v6_runtime_manifest import (
    RUNTIME_FILES,
    build_v6_runtime_manifest,
    v6_runtime_manifest_sha256,
)


def test_manifest_closes_event_and_weak_revision_production_runtime() -> None:
    manifest = build_v6_runtime_manifest()
    paths = {row["path"] for row in manifest["runtime_files"]}
    assert paths == set(RUNTIME_FILES)
    assert "production_probability_v6_event_runtime.py" in paths
    assert "production_probability_v6_canonical_event_drain_runtime.py" in paths
    assert "production_probability_v6_initial_writer.py" in paths
    assert "production_probability_v6_weak_revision_runtime.py" in paths
    assert "production_probability_v6_weak_revision_writer.py" in paths
    assert manifest["shadow_runtime"] is False
    assert manifest["v5_fallback_forbidden"] is True
    assert manifest["places_orders"] is False
    assert manifest["runtime_manifest_sha256"] == v6_runtime_manifest_sha256()
