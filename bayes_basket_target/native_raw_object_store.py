"""Provider-neutral content-addressed raw NWP object storage."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Sequence

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


# --------------------------------------------------------------------------
# Charter 195 (三): one download per step object, whatever asks for it.
#
# The range aliases above made ECMWF and GEFS byte ranges re-usable across
# the captures of one run.  Three gaps were left: (1) whole objects -- ICON's
# 36 MB per-lead fields, ECCC's per-lead files, the ECMWF .index and GEFS .idx
# -- had no store at all, so D+0, D+1, D+2, the follow lane and every
# station-set change downloaded them again; (2) two captures of the same run
# running at once both missed the store and both downloaded; (3) nothing
# counted what the store saved.  The same store answers all of it: a whole
# object is the range ``(0, -1)`` of its key, a striped cross-process lock
# makes the second asker wait for the first download instead of repeating it,
# and every answer is counted.
# --------------------------------------------------------------------------

WHOLE_OBJECT_RANGE_START = 0
WHOLE_OBJECT_RANGE_END = -1
LOCK_STRIPES = 256
_STATS_LOCK = threading.Lock()
_STATS: dict[str, dict[str, int]] = {}


def _count(store: Path, *, hit: bool, size: int) -> None:
    name = _store_label(store)
    with _STATS_LOCK:
        row = _STATS.setdefault(
            name,
            {"hits": 0, "hit_bytes": 0, "downloads": 0, "downloaded_bytes": 0},
        )
        if hit:
            row["hits"] += 1
            row["hit_bytes"] += int(size)
        else:
            row["downloads"] += 1
            row["downloaded_bytes"] += int(size)


def _store_label(store: Path) -> str:
    resolved = Path(store).expanduser().resolve()
    # ``.../official_nwp_sources/<provider>/raw`` -> ``<provider>``
    return resolved.parent.name if resolved.name == "raw" else resolved.name


def note_store_answer(store: Path | None, *, hit: bool, size: int) -> None:
    """Count one answer from (or download into) a store, for the receipts."""

    if store is not None:
        _count(Path(store), hit=hit, size=size)


def object_store_stats(reset: bool = False) -> dict[str, dict[str, int]]:
    """What the stores answered in this process, per store."""

    with _STATS_LOCK:
        snapshot = {name: dict(row) for name, row in _STATS.items()}
        if reset:
            _STATS.clear()
    return snapshot


@contextmanager
def fetch_lock(root: Path | None, key: str) -> Iterator[None]:
    """Serialise the download of one object across threads and processes.

    Striped by the key, so two captures of one run meet on the same stripe
    and the second finds the first one's object in the store.  A store that
    cannot hold a lock file is not a reason to fail: the fetch goes unlocked.
    """

    if root is None:
        yield
        return
    handle = None
    try:
        directory = Path(root).expanduser().resolve() / "locks"
        directory.mkdir(parents=True, exist_ok=True)
        stripe = int(hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:4], 16)
        handle = os.open(
            directory / f"{stripe % LOCK_STRIPES:03d}.lock",
            os.O_RDWR | os.O_CREAT,
            0o644,
        )
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError:
        if handle is not None:
            os.close(handle)
        handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                os.close(handle)


def find_whole_object(
    *, object_key: str, roots: Sequence[Path]
) -> tuple[bytes, dict[str, Any]] | None:
    """An immutable whole object already in a store, verified, or None."""

    try:
        found = find_range_addressed_message(
            object_key=object_key,
            range_start=WHOLE_OBJECT_RANGE_START,
            range_end=WHOLE_OBJECT_RANGE_END,
            roots=roots,
        )
        if found is None:
            return None
        path, record = found
        data = path.read_bytes()
    except (OSError, ContractError):
        # Evicted between the look-up and the read, or corrupt: download.
        return None
    if hashlib.sha256(data).hexdigest() != str(record.get("message_sha256")):
        return None
    return data, record


def _drop_corrupt_body(store: Path, digest: str) -> None:
    """A body whose bytes no longer match its name is removed, not trusted."""

    body = store / f"{digest}.grib2"
    try:
        hasher = hashlib.sha256()
        with body.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
    except OSError:
        return
    if hasher.hexdigest() != digest:
        try:
            body.unlink()
        except OSError:
            pass


def cached_whole_object(
    *,
    object_key: str,
    fetch: Callable[[], tuple[bytes, str]],
    root: Path | None,
    after_store: Callable[[Path], None] | None = None,
) -> tuple[bytes, str, bool]:
    """``(bytes, last_modified, from_store)`` for one immutable object.

    ``fetch`` returns the bytes and the server's Last-Modified.  With no store
    this is exactly ``fetch``.  The Last-Modified returned on a store hit is
    the one recorded at the original download, so a receipt built from it is
    byte-identical to the one the download produced.
    """

    if root is None:
        data, last_modified = fetch()
        return data, last_modified, False
    store = Path(root).expanduser().resolve()
    with fetch_lock(store, object_key):
        hit = find_whole_object(object_key=object_key, roots=(store,))
        if hit is not None:
            data, record = hit
            _count(store, hit=True, size=len(data))
            return data, str(record.get("last_modified") or ""), True
        data, last_modified = fetch()
        digest = hashlib.sha256(data).hexdigest()
        _drop_corrupt_body(store, digest)
        store_content_addressed_message(data, root=store, expected_sha256=digest)
        store_range_alias(
            object_key=object_key,
            range_start=WHOLE_OBJECT_RANGE_START,
            range_end=WHOLE_OBJECT_RANGE_END,
            message_sha256=digest,
            root=store,
            last_modified=last_modified,
        )
        _count(store, hit=False, size=len(data))
    if after_store is not None:
        after_store(store)
    return data, last_modified, False


def evict_runs_over_cap(
    root: Path,
    *,
    cap_bytes: int,
    keep_newest_runs: int,
    run_of: Callable[[str], str | None],
    min_age_sec: float = 600.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Drop whole runs from a store, oldest first, until it fits its cap.

    A run is the unit because that is how the objects stop being needed: once
    a newer run of the family has landed and been captured for the days it
    covers, nothing asks for the older run's steps again.  The newest
    ``keep_newest_runs`` runs are never dropped, nor is anything stored in the
    last ``min_age_sec`` (a capture may be about to read it).  A reader racing
    an eviction finds the object gone and downloads it; nothing reads a
    half-deleted body, because the alias goes first and the body is verified
    against its digest on the way out.  The store is a cache, not evidence:
    the receipts in the archives and the SSOT carry the provenance.
    """

    store = Path(root).expanduser().resolve()
    aliases = store / "ranges"
    clock = time.time() if now is None else float(now)
    runs: dict[str, list[tuple[Path, Path, int, float]]] = {}
    total = 0
    try:
        entries = list(aliases.glob("*.json"))
    except OSError:
        entries = []
    for alias in entries:
        try:
            record = json.loads(alias.read_text(encoding="utf-8"))
            digest = raw_sha256(record.get("message_sha256"))
            body = store / f"{digest}.grib2"
            size = body.stat().st_size
            stamp = alias.stat().st_mtime
        except (OSError, ValueError, ContractError):
            continue
        run = run_of(str(record.get("object_key") or ""))
        if run is None:
            continue
        runs.setdefault(run, []).append((alias, body, size, stamp))
        total += size
    ordered = sorted(runs, reverse=True)
    kept = set(ordered[: max(0, int(keep_newest_runs))])
    dropped_runs: list[str] = []
    freed = 0
    for run in reversed(ordered):
        if total <= cap_bytes:
            break
        if run in kept:
            continue
        rows = runs[run]
        if any(clock - stamp < min_age_sec for _a, _b, _s, stamp in rows):
            continue
        for alias, body, size, _stamp in rows:
            for path in (alias, body):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
            total -= size
            freed += size
        dropped_runs.append(run)
    return {
        "store": str(store),
        "bytes_after": total,
        "freed_bytes": freed,
        "dropped_runs": dropped_runs,
        "runs_after": len(ordered) - len(dropped_runs),
    }
