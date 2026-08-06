"""Provider-neutral content-addressed raw NWP object storage."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
from typing import Any, Sequence

from .contracts import ContractError


def raw_sha256(value: Any, code: str = "native_raw_sha_invalid") -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(ch not in "0123456789abcdef" for ch in result):
        raise ContractError(code)
    return result


def find_content_addressed_message(
    message_sha256: str,
    roots: Sequence[Path],
) -> Path | None:
    digest = raw_sha256(message_sha256)
    for root in roots:
        candidate = Path(root).expanduser().resolve() / f"{digest}.grib2"
        if not candidate.is_file():
            continue
        hasher = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise ContractError("native_raw_object_sha_mismatch")
        return candidate
    return None


def store_content_addressed_message(
    message: bytes,
    *,
    root: Path,
    expected_sha256: str,
) -> Path:
    digest = hashlib.sha256(message).hexdigest()
    if digest != raw_sha256(expected_sha256, "native_raw_expected_sha_invalid"):
        raise ContractError("native_raw_download_sha_mismatch")
    directory = Path(root).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.grib2"
    existing = find_content_addressed_message(digest, (directory,))
    if existing is not None:
        return existing
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{digest}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(message)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return target
