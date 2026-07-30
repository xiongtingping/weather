"""Reject runtime pointers that could escape to an external or symlinked path."""

from __future__ import annotations

from pathlib import Path


class LocalRuntimePointerError(ValueError):
    pass


def resolve_repo_relative_regular_file(base: Path, raw_path: str) -> Path:
    """Resolve a pointer without following symlinks outside the approved root.

    Runtime activation documents may only name ordinary files below ``base``.
    Absolute paths, parent traversal and every symlink component are rejected
    before the pointed-to file is opened.  This prevents an optional market
    branch from turning a slow or missing external volume into an AMSC delay.
    """

    relative = Path(str(raw_path or "").strip())
    if not str(relative) or relative.is_absolute() or ".." in relative.parts:
        raise LocalRuntimePointerError("runtime_pointer_must_be_repo_relative")
    current = base
    for part in relative.parts:
        if part in {"", "."}:
            continue
        current = current / part
        if current.is_symlink():
            raise LocalRuntimePointerError("runtime_pointer_symlink_forbidden")
    if not current.is_file():
        raise LocalRuntimePointerError("runtime_pointer_regular_file_required")
    return current


__all__ = ["LocalRuntimePointerError", "resolve_repo_relative_regular_file"]
