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

