"""Directories that live on an external volume, and the one thing that must
not happen when it is not plugged in.

User ruling 2026-09-20 (「移动电脑磁盘内容过去（不影响生产的，不依赖外置硬盘
的内容）」).  The store outgrew the machine -- 55 GiB of SSOT on a volume with
four free -- so the cold research artifacts moved to an external APFS volume
and left symlinks behind.  Nothing the resident runs is among them: the
whole set has no reference inside ``bayes_basket_target`` and none of the
hosted sweeps touches it, which is the test a directory had to pass to be
moved at all.

What a dangling symlink does, measured 2026-09-20 rather than assumed:

    mkdir(parents=True, exist_ok=True)   FileExistsError      loud
    open(..., "w")                       FileNotFoundError    loud
    os.listdir()                         FileNotFoundError    loud
    resolve(strict=True)                 FileNotFoundError    loud
    exists() / is_dir()                  False                SILENT
    glob() / rglob()                     []                   SILENT
    os.walk()                            []                   SILENT

The loud four need nothing.  The silent four are the reason this module
exists: a reader that globs an unplugged volume gets an empty list and
reports "no data" -- and the first one to do it would have been the resource
audit, which would have reported the disk as smaller than it is, in the
middle of running out of disk.

Charter 196 (2026-09-22, user ruling 「能否把模型和历史的原始数据移到外置硬盘，
但是把校准、融合、权重、oos、拟合等留在本机？如果可以就执行！」) adds two
things here, both measured before they were written down:

*A plugged-in volume can still be unreadable.*  macOS gates removable
volumes per responsible process (TCC).  Under launchd the daily NWP
retention pass on 2026-09-21 05:20 got ``Operation not permitted`` on the
volume, and then -- exactly like the unplugged case -- ``stat`` answered
(``is_dir()`` True) while ``os.walk`` returned nothing: the pass scanned
``data/research`` as a reference root, silently found zero documents there,
and reported the ``model_arrivals`` source root as holding zero stores.  So
``assert_present`` now also proves the path can be listed/opened, and the
volume being attached is no longer the same thing as it being usable.

*The cold tier.*  Whatever the hot tier evicts (NWP bodies past the 7 GiB
budget, past-dated capture archives, week-old observation objects) goes to
``<cold root>/evicted/<repo-relative path>`` instead of being deleted, when
the cold tier is usable.  When it is not -- unplugged, TCC-denied, read-only,
on the internal disk, or short of space -- the evicting job degrades to what
it did before (discard what is re-obtainable, keep the rest) and records why.
Production never reads the cold tier: the hot readers look only in their own
roots and treat an evicted body as absent, i.e. re-fetch.  Only offline
replay/audit code calls :func:`evicted_copy`, and that call asserts first.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

__all__ = [
    "COLD_TIER_ENV",
    "DEFAULT_COLD_TIER_ROOT",
    "EVICTED_DIR_NAME",
    "OffloadedVolumeMissing",
    "OffloadedVolumeUnreadable",
    "assert_present",
    "cold_tier_root",
    "cold_tier_status",
    "copy_to_cold_tier",
    "evicted_copy",
    "evicted_path",
    "is_offloaded",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
#: The external APFS volume charter 167 moved the cold directories to.
DEFAULT_COLD_TIER_ROOT = Path("/Volumes/weather_data/weather_cold")
#: Overrides the cold root; set it to an empty string to turn the tier off.
COLD_TIER_ENV = "PM_COLD_TIER_ROOT"
EVICTED_DIR_NAME = "evicted"
#: Below this much free space on the volume an eviction discards instead of
#: moving: the cold tier is an archive, and filling it would only move the
#: out-of-disk failure from one volume to the other.
DEFAULT_COLD_TIER_MIN_FREE_BYTES = 50 * 1024**3
_COPY_CHUNK = 4 * 1024 * 1024


class OffloadedVolumeMissing(RuntimeError):
    """A path that lives on the external volume, with the volume unplugged."""


class OffloadedVolumeUnreadable(OffloadedVolumeMissing):
    """The volume is attached but this process may not list or open it.

    A subclass on purpose: every caller that already refuses to read an
    unplugged volume must refuse this too, because the silent operations
    answer the same "nothing is there" in both cases.
    """


def _prefixes(path: Path) -> list[Path]:
    parts = path.parts
    return [Path(*parts[: index + 1]) for index in range(len(parts))]


def is_offloaded(path: Path | str) -> bool:
    """True when ``path`` or one of its ancestors is a symlink whose target
    is not there.

    Deliberately narrow: a real directory answers False, and so does a
    symlink that resolves.  Only the unplugged case is interesting, and only
    the caller's own path is examined -- this never goes looking for mount
    points.  Ancestors count because the readers pass paths *under* the
    offloaded link (``data/research/bayes_basket_target``) as often as the
    link itself, and those answer ``exists() -> False`` just as silently.
    """

    p = Path(path)
    try:
        for prefix in _prefixes(p):
            if prefix.is_symlink() and not prefix.exists():
                return True
    except OSError:
        return False
    return False


def _unreadable_reason(p: Path) -> str | None:
    """Why ``p`` exists but cannot be read, or None when it can."""

    try:
        if p.is_dir():
            with os.scandir(p) as entries:
                next(entries, None)
        elif p.is_file():
            with p.open("rb"):
                pass
    except PermissionError as exc:
        return f"{type(exc).__name__}:{exc.strerror or exc}"
    return None


def assert_present(path: Path | str, *, what: str = "") -> Path:
    """Return ``path``, or raise if it is an offloaded directory that is not
    mounted -- or is mounted but this process may not read it.

    Call this before globbing or testing ``exists()`` on anything under
    ``data/research``, the cold tier, or any other path on the external
    volume.  Raising is the point: the caller would otherwise read an empty
    directory and carry on.
    """

    p = Path(path)
    if is_offloaded(p):
        target = ""
        try:
            target = f" -> {p.readlink()}" if p.is_symlink() else ""
        except OSError:
            pass
        raise OffloadedVolumeMissing(
            f"{what or p} lives on an external volume that is not mounted"
            f" ({p}{target}); plug it in or pass another path"
        )
    reason = _unreadable_reason(p) if p.exists() else None
    if reason is not None:
        raise OffloadedVolumeUnreadable(
            f"{what or p} is attached but not readable by this process ({reason});"
            " under launchd this is macOS removable-volume permission (TCC)"
        )
    return p


def cold_tier_root() -> Path | None:
    """The configured cold root, or None when the tier is switched off."""

    configured = os.environ.get(COLD_TIER_ENV)
    if configured is None:
        return DEFAULT_COLD_TIER_ROOT
    configured = configured.strip()
    return Path(configured).expanduser() if configured else None


def cold_tier_status(
    root: Path | str | None,
    *,
    local_reference: Path | str | None = REPO_ROOT,
    min_free_bytes: int = DEFAULT_COLD_TIER_MIN_FREE_BYTES,
) -> dict[str, Any]:
    """Whether an eviction may move to ``root`` right now, and if not why.

    ``state`` is ``available`` or one of ``disabled`` / ``missing`` /
    ``unreadable`` / ``unwritable`` / ``same_device`` / ``low_space``.
    ``same_device`` is the check that matters most and is cheapest to miss:
    with the volume unplugged, a directory of the same name on the internal
    disk would accept every "eviction" and free nothing.  Tests pass
    ``local_reference=None`` to allow a temporary directory.
    """

    if root is None:
        return {"state": "disabled", "root": "", "free_bytes": None}
    base = Path(root).expanduser()
    row: dict[str, Any] = {"root": str(base), "free_bytes": None}
    if is_offloaded(base) or not base.is_dir():
        return {**row, "state": "missing"}
    reason = _unreadable_reason(base)
    if reason is not None:
        return {**row, "state": "unreadable", "reason": reason}
    if local_reference is not None:
        try:
            if os.stat(base).st_dev == os.stat(local_reference).st_dev:
                return {**row, "state": "same_device"}
        except OSError as exc:
            return {**row, "state": "missing", "reason": str(exc)}
    try:
        descriptor, probe = tempfile.mkstemp(prefix=".cold_tier_probe.", dir=base)
        os.close(descriptor)
        os.unlink(probe)
    except OSError as exc:
        return {**row, "state": "unwritable", "reason": f"{type(exc).__name__}:{exc}"}
    free = shutil.disk_usage(base).free
    row["free_bytes"] = free
    if free < int(min_free_bytes):
        return {**row, "state": "low_space", "min_free_bytes": int(min_free_bytes)}
    return {**row, "state": "available"}


def evicted_path(
    source: Path | str,
    *,
    root: Path | str,
    repo_root: Path | str = REPO_ROOT,
) -> Path:
    """Where ``source`` lives once evicted: ``<root>/evicted/<relative path>``.

    Relative to the repository when it is inside it (both spellings are
    tried, so a resolved and an unresolved path land on the same place);
    relative to the cold root itself when the source already sits on the
    volume (``data/research`` resolves there).
    """

    src = Path(source)
    base = Path(root)
    for anchor in (Path(repo_root), Path(repo_root).resolve()):
        try:
            return base / EVICTED_DIR_NAME / src.relative_to(anchor)
        except ValueError:
            continue
    for anchor in (base, base.resolve()):
        try:
            relative = src.relative_to(anchor)
        except ValueError:
            continue
        if relative.parts and relative.parts[0] == EVICTED_DIR_NAME:
            raise ValueError(f"cold_tier_source_already_evicted:{src}")
        return base / EVICTED_DIR_NAME / relative
    raise ValueError(f"cold_tier_source_outside_repository:{src}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_COPY_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_to_cold_tier(
    source: Path | str,
    destination: Path | str,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Put a verified copy of ``source`` at ``destination``; never unlinks.

    Copy to ``.partial``, fsync, then compare size *and* sha256 of the bytes
    as they now sit on the volume -- a USB write that truncates or flips a
    block must not be the only copy -- and only then rename into place.  An
    identical file already at the destination (content-addressed stores
    re-download the same body) is accepted after the same check.  The caller
    removes the source only after it has recorded this result.
    """

    src = Path(source)
    dst = Path(destination)
    size = src.stat().st_size
    wanted = (expected_sha256 or _sha256_file(src)).lower()
    if dst.is_file() and dst.stat().st_size == size and _sha256_file(dst) == wanted:
        return {"cold_path": str(dst), "sha256": wanted, "size_bytes": size, "copied": False}
    dst.parent.mkdir(parents=True, exist_ok=True)
    staging = dst.with_name(dst.name + ".partial")
    try:
        with src.open("rb") as reader, staging.open("wb") as writer:
            shutil.copyfileobj(reader, writer, _COPY_CHUNK)
            writer.flush()
            os.fsync(writer.fileno())
        if staging.stat().st_size != size:
            raise OSError(f"cold_tier_size_mismatch:{src}:{staging.stat().st_size}!={size}")
        landed = _sha256_file(staging)
        if landed != wanted:
            raise OSError(f"cold_tier_sha256_mismatch:{src}:{landed}!={wanted}")
        os.replace(staging, dst)
    finally:
        if staging.exists():
            staging.unlink()
    return {"cold_path": str(dst), "sha256": wanted, "size_bytes": size, "copied": True}


def evicted_copy(
    source: Path | str,
    *,
    root: Path | str | None = None,
    repo_root: Path | str = REPO_ROOT,
) -> Path:
    """The cold copy of a file the hot tier evicted, for offline readers.

    Asserts the cold root first, so an unplugged or unreadable volume raises
    :class:`OffloadedVolumeMissing` instead of reading as "never existed".
    Production code must not call this -- a body that is not in the hot
    tier is re-fetched there, never read from the volume.
    """

    base = Path(root) if root is not None else cold_tier_root()
    if base is None:
        raise OffloadedVolumeMissing("cold tier is switched off (PM_COLD_TIER_ROOT='')")
    assert_present(base, what="cold tier")
    if not base.is_dir():
        # The cold root *is* the volume: its absence is the unplugged case,
        # not "this file was never evicted".
        raise OffloadedVolumeMissing(f"cold tier {base} is not mounted")
    candidate = evicted_path(source, root=base, repo_root=repo_root)
    if not candidate.is_file():
        raise FileNotFoundError(f"not in the cold tier: {source} (looked at {candidate})")
    return candidate
