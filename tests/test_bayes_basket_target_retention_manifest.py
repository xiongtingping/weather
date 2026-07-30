from __future__ import annotations

import json
from pathlib import Path

import pytest

from bayes_basket_target import retention_manifest as rm


def _entry(manifest: dict, name: str) -> dict:
    return next(row for row in manifest["entries"] if Path(row["path"]).name == name)


def test_authoritative_objects_keep_and_large_archive_requires_maintenance(tmp_path: Path) -> None:
    ssot = tmp_path / "data" / "bayes_basket_target_ssot.db"
    archive = tmp_path / "data" / "archive.db"
    wal = tmp_path / "data" / "archive.db-wal"
    for path in (ssot, archive, wal):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    result = rm.build_retention_manifest(
        [ssot, archive, wal], project_root=tmp_path, max_hash_bytes=0
    )

    assert _entry(result, ssot.name)["category"] == rm.KEEP
    assert _entry(result, archive.name)["category"] == rm.MAINTENANCE_REQUIRED
    assert _entry(result, wal.name)["category"] == rm.MAINTENANCE_REQUIRED
    assert result["cleanup_performed"] is False


def test_large_archive_is_classified_from_metadata_without_reading_body(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "data" / "archive.db"
    archive.parent.mkdir(parents=True)
    with archive.open("wb") as stream:
        stream.truncate(3 * 1024 * 1024 * 1024)

    result = rm.build_retention_manifest(
        [archive],
        project_root=tmp_path,
        max_bytes=0,
        max_hash_bytes=0,
    )

    row = _entry(result, archive.name)
    assert row["category"] == rm.MAINTENANCE_REQUIRED
    assert row["hash_status"] == "not_computed_resource_bound"
    assert result["resource"]["bytes_read"] == 0
    assert result["resource"]["discovered_bytes"] == 3 * 1024 * 1024 * 1024


def test_lineage_raw_truth_model_calibration_doctrine_and_migration_keep(tmp_path: Path) -> None:
    names = (
        "lineage/receipt.json",
        "historical_truth_raw/official.bin",
        "model_payload/ecmwf.bin",
        "calibration/v1.json",
        "doctrine/plan.md",
        "migration/pre_timezone.db",
    )
    paths = []
    for relative in names:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"payload")
        paths.append(path)

    result = rm.build_retention_manifest(paths, project_root=tmp_path)

    assert all(row["category"] == rm.KEEP for row in result["entries"])


def test_only_explicit_unreferenced_temporary_rules_are_candidates(tmp_path: Path) -> None:
    empty_db = tmp_path / "data" / "research" / "bayes_basket_target" / "target.db"
    ui_log = tmp_path / "data" / "ui_server.err.log"
    cache = tmp_path / "output" / "__pycache__" / "module.pyc"
    ds_store = tmp_path / ".DS_Store"
    unknown = tmp_path / "notes.bin"
    for path in (empty_db, ui_log, cache, ds_store, unknown):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"" if path == empty_db else b"x")

    result = rm.build_retention_manifest(
        [empty_db, ui_log, cache, ds_store, unknown], project_root=tmp_path
    )

    assert _entry(result, empty_db.name)["category"] == rm.DELETE_CANDIDATE
    assert _entry(result, ui_log.name)["category"] == rm.DELETE_CANDIDATE
    assert _entry(result, cache.name)["category"] == rm.DELETE_CANDIDATE
    assert _entry(result, ds_store.name)["category"] == rm.DELETE_CANDIDATE
    assert _entry(result, unknown.name)["category"] == rm.KEEP


def test_reference_path_or_hash_overrides_candidate_but_never_authority(tmp_path: Path) -> None:
    candidate = tmp_path / "ui_server.err.log"
    candidate.write_bytes(b"referenced")
    digest = __import__("hashlib").sha256(candidate.read_bytes()).hexdigest()
    result = rm.build_retention_manifest(
        [candidate], project_root=tmp_path, referenced_hashes=[digest]
    )
    row = _entry(result, candidate.name)
    assert row["category"] == rm.KEEP
    assert row["reference_status"] == "referenced"
    assert row["sha256"] == digest


def test_receipt_contains_metadata_only_and_never_file_content(tmp_path: Path) -> None:
    path = tmp_path / "secret.txt"
    secret = "DO_NOT_PUT_THIS_IN_RECEIPT"
    path.write_text(secret, encoding="utf-8")
    result = rm.build_retention_manifest([path], project_root=tmp_path)
    encoded = json.dumps(result, ensure_ascii=False)
    assert secret not in encoded
    row = result["entries"][0]
    assert set(("path", "size_bytes", "mtime_ns", "reason", "reference_status", "sha256")) <= set(row)
    assert "content" not in row and "body" not in row


def test_budget_fails_closed_on_file_count_and_bytes(tmp_path: Path) -> None:
    paths = []
    for index in range(3):
        path = tmp_path / f"file-{index}.bin"
        path.write_bytes(b"1234")
        paths.append(path)

    with pytest.raises(rm.RetentionManifestError, match="max_files"):
        rm.build_retention_manifest(paths, project_root=tmp_path, max_files=2)
    with pytest.raises(rm.RetentionManifestError, match="max_bytes"):
        rm.build_retention_manifest(paths, project_root=tmp_path, max_bytes=11)


def test_disk_probe_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "file.bin"
    path.write_bytes(b"x")

    def broken(_root: Path) -> int:
        raise OSError("probe unavailable")

    monkeypatch.setattr(rm.shutil, "disk_usage", broken)
    with pytest.raises(rm.RetentionManifestError, match="disk_probe_unavailable"):
        rm.build_retention_manifest([path], project_root=tmp_path)


def test_external_drive_is_rejected_without_access(tmp_path: Path) -> None:
    path = Path("/Volumes/External/weather.db")
    with pytest.raises(rm.RetentionManifestError, match="external_path_forbidden"):
        rm.build_retention_manifest([path], project_root=tmp_path)


def test_cli_has_no_delete_switch_and_is_read_only(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "bayes_basket_target_retention_audit.py"
    text = script.read_text(encoding="utf-8")
    assert "--delete" not in text
    path = tmp_path / "file.txt"
    path.write_text("x", encoding="utf-8")
    before = path.stat()
    import subprocess

    completed = subprocess.run(
        ["python3", str(script), "--project-root", str(tmp_path), str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(completed.stdout)["state"] == "AUDIT_ONLY"
    after = path.stat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def test_duplicate_explicit_paths_are_scanned_once(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("x", encoding="utf-8")
    result = rm.build_retention_manifest([path, path], project_root=tmp_path)
    assert result["entry_count"] == 1
