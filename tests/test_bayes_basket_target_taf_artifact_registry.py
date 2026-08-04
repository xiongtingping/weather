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


def test_active_strict_oos_artifact_builds_canonical_registry_v1() -> None:
    source = _active_artifact()

    registry = build_taf_artifact_registry([source])
    parsed = validate_taf_artifact_registry(registry)

    assert registry["schema"] == SCHEMA
    assert registry["artifacts"] == [source]
    assert parsed[0].artifact_id == source["artifact_id"]
    assert parsed[0].content_sha256 == source["content_sha256"]


def test_registry_rejects_duplicate_exact_scope_even_with_distinct_ids() -> None:
    source = _active_artifact()
    duplicate = {
        **source,
        "artifact_id": source["artifact_id"] + "-duplicate",
    }
    duplicate["content_sha256"] = content_sha256({
        key: value for key, value in duplicate.items()
        if key != "content_sha256"
    })

    with pytest.raises(ContractError, match="exact_scope_ambiguous"):
        build_taf_artifact_registry([source, duplicate])


def test_registry_rejects_hash_or_noncanonical_member_order() -> None:
    registry = build_taf_artifact_registry([_active_artifact()])
    tampered_hash = {**registry, "content_sha256": "0" * 64}
    with pytest.raises(ContractError, match="content_sha_mismatch"):
        validate_taf_artifact_registry(tampered_hash)

    second = {
        **_active_artifact(),
        "artifact_id": "taf-zzzz-summer-d2-00z-v1",
        "station_id": "ZZZZ",
    }
    second["content_sha256"] = content_sha256({
        key: value for key, value in second.items()
        if key != "content_sha256"
    })
    canonical_two = build_taf_artifact_registry([second, _active_artifact()])
    noncanonical = {
        **canonical_two,
        "artifacts": list(reversed(canonical_two["artifacts"])),
    }
    noncanonical["content_sha256"] = content_sha256({
        key: value for key, value in noncanonical.items()
        if key != "content_sha256"
    })
    with pytest.raises(ContractError, match="not_canonical"):
        validate_taf_artifact_registry(noncanonical)


def test_registry_rejects_an_empty_active_artifact_set() -> None:
    empty = {"schema": SCHEMA, "artifacts": []}
    empty["content_sha256"] = content_sha256(empty)

    with pytest.raises(ContractError, match="count_invalid"):
        validate_taf_artifact_registry(empty)


def test_registry_cli_builds_from_current_active_artifact(tmp_path: Path) -> None:
    output = tmp_path / "taf-registry.json"
    receipt = tmp_path / "receipt.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_bayes_basket_target_taf_artifact_registry.py"),
            "--artifact",
            str(ACTIVE),
            "--output",
            str(output),
            "--receipt-output",
            str(receipt),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    built = json.loads(output.read_text(encoding="utf-8"))
    report = json.loads(result.stdout)
    assert validate_taf_artifact_registry(built)[0].station_id == "RKPK"
    assert report["state"] == "updated"
    assert report["boundary"]["calls_order_gateway"] is False
    assert json.loads(receipt.read_text(encoding="utf-8")) == report
