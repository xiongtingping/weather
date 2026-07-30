from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from scripts import capture_bayes_basket_target_model_arrival as capture_module


class _Response:
    status = 200

    def __init__(self, chunks: list[bytes], *, content_length: int | None = None) -> None:
        self._chunks = list(chunks)
        self.read_calls = 0
        self.headers = {
            "Last-Modified": "Thu, 23 Jul 2026 02:00:00 GMT",
        }
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        self.read_calls += 1
        return self._chunks.pop(0) if self._chunks else b""


def _capture(tmp_path: Path, **kwargs: object) -> dict[str, object]:
    return capture_module.capture(
        provider_id="icon_global_official",
        urls=["https://opendata.dwd.de/weather/model.grib2.bz2"],
        publication_url=None,
        station_id="ZUUU",
        target_local_date="2026-07-23",
        run_initialization_utc="2026-07-23T00:00:00Z",
        valid_time_utc="2026-07-23T16:00:00Z",
        decision_at_utc="2999-07-23T18:00:00Z",
        target_timezone="America/New_York",
        output_root=tmp_path / "arrivals",
        timeout_sec=10.0,
        max_response_bytes=kwargs.pop("max_response_bytes", 8),
        max_capture_bytes=kwargs.pop("max_capture_bytes", 8),
        min_free_bytes=kwargs.pop("min_free_bytes", 1),
        **kwargs,
    )


def test_cleanup_removes_only_old_dead_pid_temp_shapes(tmp_path: Path) -> None:
    provider = "icon_global_official"
    raw = tmp_path / "arrivals" / provider / "raw"
    receipts = tmp_path / "arrivals" / provider / "receipts"
    raw.mkdir(parents=True)
    receipts.mkdir(parents=True)
    stale_names = (
        "payload.json.tmp.999999999",
        ".payload.json.999999998.tmp",
        ".partial-999999997",
        ".capture-999999996",
        ".capture.999999995.123.0.grib2",
    )
    stale_paths = [raw / name for name in stale_names]
    for path in stale_paths:
        path.write_text("stale\n", encoding="utf-8")
    recent = receipts / "receipt.json.tmp.999999994"
    recent.write_text("recent\n", encoding="utf-8")
    unrecognised = raw / ".unknown.tmp"
    unrecognised.write_text("keep\n", encoding="utf-8")
    old = 1_000.0
    for path in stale_paths:
        path.touch()
        os.utime(path, (old, old))
    recent.touch()
    os.utime(recent, (9_950.0, 9_950.0))

    removed = capture_module._cleanup_stale_capture_temps(
        tmp_path / "arrivals", provider, now=10_000.0, grace_sec=60.0
    )

    assert removed == len(stale_paths)
    assert all(not path.exists() for path in stale_paths)
    assert recent.exists()
    assert unrecognised.exists()


def test_cleanup_retains_current_pid_temp_even_when_old(tmp_path: Path) -> None:
    provider = "icon_global_official"
    raw = tmp_path / "arrivals" / provider / "raw"
    raw.mkdir(parents=True)
    current = raw / f".capture.{os.getpid()}.123.0.grib2"
    current.write_text("active\n", encoding="utf-8")
    old = 1_000.0
    os.utime(current, (old, old))

    assert capture_module._cleanup_stale_capture_temps(
        tmp_path / "arrivals", provider, now=10_000.0, grace_sec=60.0
    ) == 0
    assert current.exists()


def test_prune_unreferenced_raw_keeps_receipt_lineage_and_recent_objects(tmp_path: Path) -> None:
    provider = "icon_global_official"
    provider_root = tmp_path / "arrivals" / provider
    raw = provider_root / "raw"
    receipts = provider_root / "receipts"
    raw.mkdir(parents=True)
    receipts.mkdir(parents=True)
    referenced_hash = "a" * 64
    orphan_hash = "b" * 64
    recent_hash = "c" * 64
    referenced = raw / f"{referenced_hash}.grib2"
    orphan = raw / f"{orphan_hash}.grib2"
    recent = raw / f"{recent_hash}.grib2"
    for path in (referenced, orphan, recent):
        path.write_bytes(b"raw")
    receipts.joinpath("receipt.json").write_text(
        json.dumps({
            "raw_receipts": [{
                "raw_sha256": referenced_hash,
                "raw_object_ref": str(referenced.resolve()),
            }]
        }),
        encoding="utf-8",
    )
    os.utime(referenced, (1_000.0, 1_000.0))
    os.utime(orphan, (1_000.0, 1_000.0))
    os.utime(recent, (9_950.0, 9_950.0))

    result = capture_module._prune_unreferenced_raw(
        tmp_path / "arrivals", provider, now=10_000.0, grace_sec=60.0
    )

    assert result == {"removed_count": 1, "removed_bytes": 3}
    assert referenced.exists()
    assert not orphan.exists()
    assert recent.exists()


def test_content_length_is_rejected_before_body_read(tmp_path: Path, monkeypatch) -> None:
    response = _Response([b"0123456789"], content_length=10)
    monkeypatch.setattr(capture_module, "open_official_https", lambda *_args, **_kwargs: response)

    result = _capture(tmp_path, max_response_bytes=4, max_capture_bytes=4)

    assert result["status"] == "BLOCKED"
    assert result["receipt"]["reason_codes"] == ["model_arrival_response_too_large"]
    assert response.read_calls == 0
    assert not [path for path in (tmp_path / "arrivals").rglob("*") if path.is_file()]


def test_stream_overflow_is_deleted_without_leaving_partial_raw(tmp_path: Path, monkeypatch) -> None:
    response = _Response([b"abc", b"de"])
    monkeypatch.setattr(capture_module, "open_official_https", lambda *_args, **_kwargs: response)

    result = _capture(tmp_path, max_response_bytes=4, max_capture_bytes=4)

    assert result["status"] == "BLOCKED"
    assert result["receipt"]["reason_codes"] == ["model_arrival_response_too_large"]
    assert not [path for path in (tmp_path / "arrivals").rglob("*") if path.is_file()]


def test_successful_capture_hashes_and_persists_stream_without_body_buffer(tmp_path: Path, monkeypatch) -> None:
    body = b"abcde"
    response = _Response([body[:2], body[2:]], content_length=len(body))
    monkeypatch.setattr(capture_module, "open_official_https", lambda *_args, **_kwargs: response)

    result = _capture(tmp_path)

    assert result["status"] == "CAPTURED"
    arrival = result["arrival"]
    expected_sha = hashlib.sha256(body).hexdigest()
    assert arrival["raw_payload_sha256s"] == [expected_sha]
    raw_paths = list((tmp_path / "arrivals").rglob(expected_sha + ".bz2"))
    assert len(raw_paths) == 1
    assert raw_paths[0].read_bytes() == body
    assert result["boundary"]["resource_budget"]["captured_response_bytes"] == len(body)
