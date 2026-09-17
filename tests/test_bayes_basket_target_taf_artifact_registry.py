from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bayes_basket_target import ContractError, content_sha256
from bayes_basket_target.taf_artifact_registry import (
    SCHEMA,
    build_taf_artifact_registry,
    validate_taf_artifact_registry,
)


ROOT = Path(__file__).resolve().parents[1]
ACTIVE = (
    ROOT / "config" / "artifacts"
    / "bayes_basket_target_taf_revision_active.json"
)


def _active_artifact() -> dict:
    return json.loads(ACTIVE.read_text(encoding="utf-8"))


def test_registry_rejects_an_empty_active_artifact_set() -> None:
    empty = {"schema": SCHEMA, "artifacts": []}
    empty["content_sha256"] = content_sha256(empty)

    with pytest.raises(ContractError, match="count_invalid"):
        validate_taf_artifact_registry(empty)

