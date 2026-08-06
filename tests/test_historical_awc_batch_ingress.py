from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MATERIALIZER = _load_script(
    "historical_awc_batch_materializer",
    "scripts/materialize_historical_awc_replay_45station.py",
)
TRUTH_BUILDER = _load_script(
    "historical_awc_batch_truth_builder",
    "scripts/build_historical_awc_truth_contract_45station.py",
)


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _source_manifest(tmp_path: Path) -> tuple[Path, Path]:
    artifact = tmp_path / "locator.json"
    _write_json(artifact, {"schema": "locator", "observations": []})
    pointer = {
        "path": str(artifact),
        "file_sha256": MATERIALIZER._file_sha256(artifact),
    }
    identity = {
        "schema": "bayes_basket_target_historical_awc_replay_45station_manifest_v2",
        "input_artifacts": [pointer],
    }
    manifest = tmp_path / "manifest.json"
    _write_json(manifest, {**identity, "content_sha256": content_sha256(identity)})
    return manifest, artifact


def test_source_manifest_is_the_only_locator_ingress(tmp_path: Path) -> None:
    manifest, artifact = _source_manifest(tmp_path)
    packets, pointers, parent = MATERIALIZER._source_packets(manifest)
    assert packets == [{"schema": "locator", "observations": []}]
    assert pointers == [{
        "path": str(artifact.resolve()),
        "file_sha256": MATERIALIZER._file_sha256(artifact),
    }]
    assert parent["content_sha256"] == json.loads(manifest.read_text())["content_sha256"]


def test_source_manifest_fails_closed_on_leaf_tamper(tmp_path: Path) -> None:
    manifest, artifact = _source_manifest(tmp_path)
    artifact.write_text("{}", encoding="utf-8")
    with pytest.raises(ContractError, match="file_sha256_mismatch"):
        MATERIALIZER._source_packets(manifest)


def test_source_manifest_has_cumulative_resource_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _artifact = _source_manifest(tmp_path)
    monkeypatch.setattr(MATERIALIZER, "MAX_JSON_INPUT_BYTES", 1)
    monkeypatch.setattr(
        MATERIALIZER,
        "load_bounded_json",
        lambda path, limit: json.loads(Path(path).read_text(encoding="utf-8")),
    )
    with pytest.raises(ContractError, match="cumulative_bytes_exceeded"):
        MATERIALIZER._source_packets(manifest)


def test_truth_builder_rejects_legacy_local_timezone_manifest(tmp_path: Path) -> None:
    identity = {
        "schema": "bayes_basket_target_historical_awc_replay_45station_manifest_v2",
        "settlement_timezone": "America/Toronto",
    }
    manifest = tmp_path / "legacy_manifest.json"
    _write_json(manifest, {**identity, "content_sha256": content_sha256(identity)})
    with pytest.raises(ContractError, match="manifest_timezone_invalid"):
        TRUTH_BUILDER._manifest_evidences(manifest)
