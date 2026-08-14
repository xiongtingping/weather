"""Provider-neutral content-addressed raw NWP object storage."""

from __future__ import annotations

import hashlib
import json
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


def _range_alias_name(
    *, object_key: str, range_start: int, range_end: int
) -> str:
    """Stable file name for one immutable (object, byte range) coordinate."""

    key = f"{str(object_key).strip()}|{int(range_start)}|{int(range_end)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def find_range_addressed_message(
    *,
    object_key: str,
    range_start: int,
    range_end: int,
    roots: Sequence[Path],
) -> tuple[Path, dict[str, Any]] | None:
    """Locate an already-downloaded byte range by its source coordinates.

    Content addressing alone cannot answer "have I fetched this before?": the
    digest is only known *after* the download.  These providers publish
    immutable objects, so ``(object_key, range)`` identifies the same bytes
    forever, and recording that coordinate when the range is first stored makes
    the store answerable from an index row alone.  Without it a capture whose
    forecast steps overlap an earlier one re-fetched every shared field -- two
    event dates served by the same run overlap by roughly half their steps.

    The bytes are still verified against the digest on the way out, so a
    corrupt or mismatched alias degrades to a fresh download rather than to
    wrong data.
    """

    name = _range_alias_name(
        object_key=object_key, range_start=range_start, range_end=range_end
    )
    for root in roots:
        alias = Path(root).expanduser().resolve() / "ranges" / f"{name}.json"
        if not alias.is_file():
            continue
        try:
            record = json.loads(alias.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        try:
            digest = raw_sha256(record.get("message_sha256"))
        except ContractError:
            continue
        try:
            found = find_content_addressed_message(digest, roots)
        except ContractError:
            # A stored object that no longer matches its own digest is a
            # corrupt cache entry, not a reason to fail the capture.
            continue
        if found is not None:
            return found, record
    return None


def store_range_alias(
    *,
    object_key: str,
    range_start: int,
    range_end: int,
    message_sha256: str,
    root: Path,
    last_modified: str = "",
) -> Path:
    """Record where a freshly downloaded byte range landed in the store."""

    digest = raw_sha256(message_sha256)
    directory = Path(root).expanduser().resolve() / "ranges"
    directory.mkdir(parents=True, exist_ok=True)
    name = _range_alias_name(
        object_key=object_key, range_start=range_start, range_end=range_end
    )
    target = directory / f"{name}.json"
    payload = json.dumps(
        {
            "object_key": str(object_key),
            "range_start": int(range_start),
            "range_end": int(range_end),
            "message_sha256": digest,
            "last_modified": str(last_modified or ""),
        },
        sort_keys=True,
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{name}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return target


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
