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
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["OffloadedVolumeMissing", "assert_present", "is_offloaded"]


class OffloadedVolumeMissing(RuntimeError):
    """A path that lives on the external volume, with the volume unplugged."""


def is_offloaded(path: Path | str) -> bool:
    """True when ``path`` is a symlink whose target is not there.

    Deliberately narrow: a real directory answers False, and so does a
    symlink that resolves.  Only the unplugged case is interesting, and only
    the caller's own path is examined -- this never goes looking for mount
    points.
    """

    p = Path(path)
    try:
        return p.is_symlink() and not p.exists()
    except OSError:
        return False


def assert_present(path: Path | str, *, what: str = "") -> Path:
    """Return ``path``, or raise if it is an offloaded directory that is not
    mounted.

    Call this before globbing or testing ``exists()`` on anything under
    ``data/research`` or ``data/reports``.  Raising is the point: the caller
    would otherwise read an empty directory and carry on.
    """

    p = Path(path)
    if is_offloaded(p):
        target = ""
        try:
            target = f" -> {p.readlink()}"
        except OSError:
            pass
        raise OffloadedVolumeMissing(
            f"{what or p} lives on an external volume that is not mounted"
            f" ({p}{target}); plug it in or pass another path"
        )
    return p
