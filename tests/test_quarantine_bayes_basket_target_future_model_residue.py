from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.quarantine_bayes_basket_target_future_model_residue import build


AS_OF = "2026-07-24T21:20:00Z"


def _write_receipt(root: Path, provider: str, *, target_date: str, capture_started: str, valid_time: str, payload: bytes) -> Path:
    raw_dir = root / provider / "raw"
    receipt_dir = root / provider / "receipts"
    raw_dir.mkdir(parents=True, exist_ok=True)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload).hexdigest()
    raw_path = raw_dir / f"{digest}.bin"
    raw_path.write_bytes(payload)
    receipt = {
        "schema": "bayes_basket_target_native_model_transport_receipt_v1",
        "provider_id": provider,
        "station_id": "TEST",
        "target_local_date": target_date,
        "run_initialization_utc": "2026-07-24T00:00:00Z",
        "valid_time_utc": valid_time,
        "capture_started_at_utc": capture_started,
        "raw_receipts": [{"raw_object_ref": str(raw_path), "raw_sha256": digest}],
    }
    receipt_path = receipt_dir / f"{digest}.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return receipt_path


def test_audit_identifies_only_pre_start_future_residue(tmp_path: Path) -> None:
    provider = "icon_global_official"
    future = _write_receipt(
        tmp_path,
        provider,
        target_date="2026-07-25",
        capture_started="2026-07-24T21:00:00Z",
        valid_time="2026-07-25T10:00:00Z",
        payload=b"future",
    )
    current = _write_receipt(
        tmp_path,
        provider,
        target_date="2026-07-24",
        capture_started="2026-07-24T21:00:00Z",
        valid_time="2026-07-24T22:00:00Z",
        payload=b"current",
    )

    result = build(root=tmp_path, as_of_utc=AS_OF, purge=False)

    assert result["candidate_receipt_count"] == 1
    assert result["safe_raw_object_count"] == 1
    assert future.is_file()
    assert current.is_file()
    assert result["checks"]["no_candidate_raw_shared_with_non_residue"] is True


def test_purge_removes_only_unshared_future_residue(tmp_path: Path) -> None:
    provider = "gfs_0p25_official"
    future = _write_receipt(
        tmp_path,
        provider,
        target_date="2026-07-25",
        capture_started="2026-07-24T21:00:00Z",
        valid_time="2026-07-25T04:00:00Z",
        payload=b"future-only",
    )
    current = _write_receipt(
        tmp_path,
        provider,
        target_date="2026-07-24",
        capture_started="2026-07-24T21:00:00Z",
        valid_time="2026-07-24T22:00:00Z",
        payload=b"keep",
    )
    future_raw = tmp_path / provider / "raw" / f"{hashlib.sha256(b'future-only').hexdigest()}.bin"
    kept_raw = tmp_path / provider / "raw" / f"{hashlib.sha256(b'keep').hexdigest()}.bin"

    result = build(root=tmp_path, as_of_utc=AS_OF, purge=True)

    assert result["checks"]["purged"] is True
    assert not future.is_file()
    assert current.is_file()
    assert not future_raw.is_file()
    assert kept_raw.is_file()
