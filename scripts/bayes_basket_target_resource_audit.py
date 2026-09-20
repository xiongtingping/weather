#!/usr/bin/env python3
"""Read-only resource and raw-lineage audit for the isolated target lane.

The audit never deletes, archives, compacts, or opens the resident live DB.
Content-addressed raw bodies are considered protected when any bounded target
receipt references their hash or object path.  Only explicitly unreferenced
objects and identifiable temporary remnants are reported as cleanup
candidates; callers must decide whether a later, separately guarded cleanup
is justified.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    inspect_target_resource_budget,
    write_bounded_json,
)
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.offloaded_paths import assert_present  # noqa: E402


SCHEMA = "bayes_basket_target_resource_audit_v1"
DEFAULT_ROOT = ROOT / "data/research/bayes_basket_target/model_arrivals"
RAW_NAME = re.compile(r"^([0-9a-fA-F]{64})(?:\.[A-Za-z0-9._-]+)?$")
TEMP_NAME = re.compile(r"(?:^\.|\.tmp(?:\.|$)|\.partial(?:\.|$)|\.capture(?:\.|$))")


def _reference_files(root: Path) -> list[Path]:
    """Return bounded target receipts that can retain raw-object lineage.

    Raw ``receipts/`` are not the only durable references. Historical
    reconstruction and calibration audit packets live under
    ``output/audits`` and carry ``raw_object_ref``/hashes for the same raw
    objects. Ignoring those packets makes the audit report false cleanup
    candidates and is unsafe for the retention contract.
    """

    receipt_files = set(root.glob("*/receipts/*.json"))
    files = set(receipt_files)
    project_root = ROOT
    if project_root in root.parents or root == project_root:
        audits_root = project_root / "output" / "audits"
        files.update(
            path
            for path in audits_root.rglob("*.json")
            if any(part.startswith("bayes_basket_target") for part in path.parts)
        )
        research_root = project_root / "data" / "research" / "bayes_basket_target"
        # ``data/research`` lives on the external volume.  Unplugged, its
        # glob returns [] and this audit would report the store as smaller
        # than it is -- the one reading that must never be quietly wrong,
        # because it is what says whether the disk is filling.
        assert_present(research_root.parent, what="data/research")
        files.update(research_root.glob("**/*.json"))
        files = receipt_files | {
            path
            for path in files
            if RAW_ROOT_EXCLUSION not in path.parents
        }
    return sorted(path for path in files if path.is_file())


# NOT resolved.  The paths it is compared against come out of
# ``research_root.glob(...)`` unresolved, and ``data/research`` is a symlink
# to the external volume (charter 167): resolving one side and not the other
# made this exclusion match nothing, so the audit counted the very raw files
# it exists to leave out.  Both sides stay in the repository's own spelling.
RAW_ROOT_EXCLUSION = ROOT / "data" / "research" / "bayes_basket_target" / "model_arrivals"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _receipt_references(root: Path) -> tuple[set[str], set[Path], list[str]]:
    hashes: set[str] = set()
    paths: set[Path] = set()
    errors: list[str] = []
    for receipt in _reference_files(root):
        try:
            payload = load_bounded_json(receipt)
        except Exception as exc:
            errors.append(f"{receipt}:{type(exc).__name__}:{exc}")
            continue
        stack: list[Any] = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, Mapping):
                for key, child in value.items():
                    if key in {"raw_sha256", "raw_payload_sha256"} and isinstance(child, str):
                        candidate = child.strip().lower()
                        if re.fullmatch(r"[0-9a-f]{64}", candidate):
                            hashes.add(candidate)
                    elif key == "raw_payload_sha256s" and isinstance(child, list):
                        for item in child:
                            if not isinstance(item, str):
                                continue
                            candidate = item.strip().lower()
                            if re.fullmatch(r"[0-9a-f]{64}", candidate):
                                hashes.add(candidate)
                    elif key == "raw_object_ref" and isinstance(child, str) and child.strip():
                        try:
                            candidate_path = Path(child).expanduser().resolve()
                        except OSError:
                            continue
                        if _inside(candidate_path, root):
                            paths.add(candidate_path)
                    else:
                        stack.append(child)
            elif isinstance(value, list):
                stack.extend(value)
    return hashes, paths, errors


def audit(*, root: Path, now: float | None = None, temp_grace_sec: float = 3600.0) -> dict[str, Any]:
    resolved = root.expanduser().resolve()
    hashes, paths, errors = _receipt_references(resolved)
    current = float(time.time() if now is None else now)
    unreferenced: list[dict[str, Any]] = []
    temporary: list[dict[str, Any]] = []
    provider_rows: list[dict[str, Any]] = []
    for provider_root in sorted(path for path in resolved.iterdir() if path.is_dir()) if resolved.is_dir() else []:
        raw_root = provider_root / "raw"
        provider_unreferenced: list[dict[str, Any]] = []
        provider_temporary: list[dict[str, Any]] = []
        if raw_root.is_dir():
            for path in sorted(raw_root.iterdir()):
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    size = int(path.stat().st_size)
                    age_sec = max(0.0, current - float(path.stat().st_mtime))
                except OSError as exc:
                    errors.append(f"{path}:{type(exc).__name__}:{exc}")
                    continue
                row = {"path": str(path.resolve()), "size_bytes": size, "age_sec": age_sec}
                if TEMP_NAME.search(path.name):
                    if age_sec >= max(0.0, float(temp_grace_sec)):
                        provider_temporary.append(row)
                        temporary.append({"provider": provider_root.name, **row})
                    continue
                match = RAW_NAME.fullmatch(path.name)
                if not match:
                    continue
                digest = match.group(1).lower()
                if digest not in hashes and path.resolve() not in paths:
                    provider_unreferenced.append({"provider": provider_root.name, **row, "sha256": digest})
                    unreferenced.append({"provider": provider_root.name, **row, "sha256": digest})
        provider_rows.append({
            "provider": provider_root.name,
            "unreferenced_count": len(provider_unreferenced),
            "unreferenced_bytes": sum(int(row["size_bytes"]) for row in provider_unreferenced),
            "stale_temporary_count": len(provider_temporary),
            "stale_temporary_bytes": sum(int(row["size_bytes"]) for row in provider_temporary),
        })
    resource = inspect_target_resource_budget(DEFAULT_TARGET_DB, raw_root=resolved)
    return {
        "schema": SCHEMA,
        "state": "AUDIT_ONLY",
        "root": str(resolved),
        "resource_budget": resource,
        "receipt_reference_count": len(hashes) + len(paths),
        "invalid_receipt_or_stat_errors": errors,
        "unreferenced_raw": unreferenced,
        "stale_temporary_files": temporary,
        "provider_rows": provider_rows,
        "cleanup_performed": False,
        "external_archive_dependency": False,
        "production_dependency": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--temp-grace-sec", type=float, default=3600.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(root=args.root, temp_grace_sec=max(0.0, args.temp_grace_sec))
    if args.output:
        write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["resource_budget"]["ok"] and not result["invalid_receipt_or_stat_errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
