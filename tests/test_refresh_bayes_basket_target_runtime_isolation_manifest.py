from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.bayes_basket_target_production_closure_audit import (
    DEFAULT_SOURCE_SCOPE,
    PROTECTED_LEGACY_CONFIG,
    TARGET_ACTIVATION,
    _manifest_digest,
)
from scripts.refresh_bayes_basket_target_runtime_isolation_manifest import (
    ROOT,
    build_refreshed_manifest,
)


def test_refresh_reseals_exact_frozen_scope_without_side_effects() -> None:
    result = build_refreshed_manifest(
        project_root=ROOT,
        allow_scope_upgrade=True,
    )
    expected = set(DEFAULT_SOURCE_SCOPE)
    observed = {
        result["target"]["activation"]["path"],
        *(
            item["path"]
            for item in result["target"]["source_closure"]
        ),
    }
    assert observed == expected
    assert result["manifest_sha256"] == _manifest_digest(result)
    for item in (
        result["target"]["activation"],
        *result["target"]["source_closure"],
        *result["protected_legacy_config"],
    ):
        assert item["sha256"] == hashlib.sha256(
            (ROOT / item["path"]).read_bytes()
        ).hexdigest()


def test_refresh_rejects_manifest_outside_project(tmp_path: Path) -> None:
    outside = tmp_path / "manifest.json"
    outside.write_text("{}")
    with pytest.raises(
        RuntimeError,
        match="runtime_isolation_manifest_outside_root",
    ):
        build_refreshed_manifest(
            project_root=ROOT,
            manifest_path=outside,
        )


def test_refresh_keeps_protected_legacy_resource_separate() -> None:
    result = build_refreshed_manifest(
        project_root=ROOT,
        allow_scope_upgrade=True,
    )
    assert result["protected_legacy_config"][0]["path"] == (
        PROTECTED_LEGACY_CONFIG
    )
    assert result["target"]["activation"]["path"] == TARGET_ACTIVATION


def test_refresh_requires_explicit_scope_upgrade_for_new_runtime_path(
    tmp_path: Path,
) -> None:
    current = build_refreshed_manifest(
        project_root=ROOT,
        allow_scope_upgrade=True,
    )
    missing = {
        **current,
        "target": {
            **current["target"],
            "source_closure": [
                item
                for item in current["target"]["source_closure"]
                if item["path"]
                != "bayes_basket_target/settlement_truth.py"
            ],
        },
    }
    missing["manifest_sha256"] = _manifest_digest(missing)
    for item in (
        current["target"]["activation"],
        *current["target"]["source_closure"],
        *current["protected_legacy_config"],
    ):
        destination = tmp_path / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / item["path"]).read_bytes())
    manifest_path = (
        tmp_path
        / "config/bayes_basket_target/runtime_config_isolation_v1.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(missing, indent=2, sort_keys=True) + "\n"
    )
    with pytest.raises(
        RuntimeError,
        match="runtime_isolation_source_scope_drift",
    ):
        build_refreshed_manifest(
            project_root=tmp_path,
            manifest_path=manifest_path,
        )
    upgraded = build_refreshed_manifest(
        project_root=tmp_path,
        manifest_path=manifest_path,
        allow_scope_upgrade=True,
    )
    assert any(
        item["path"] == "bayes_basket_target/settlement_truth.py"
        for item in upgraded["target"]["source_closure"]
    )
