#!/usr/bin/env python3
"""Move cold NWP raw bodies to an external volume, preserving them.

The retention tool's ``--cold-archive-to-budget`` *discards* still-referenced
bodies and is restricted to stores the operator asserts are re-obtainable.
When an external volume is available the preserving move is the better trade:
the bodies stay readable for strict replay whenever the volume is attached,
and the internal disk gets its space back either way.

Charter boundary (AGENTS.md): the external volume is for asynchronous cold
archive of non-critical data only.  Nothing here makes it a production
dependency -- production reads the target SSOT and the canonical archive on
the internal disk, and a missing cold body fails an offline replay closed
rather than affecting AWC/METAR/SPECI, the SSOT or trading.

Safety: every body is copied and its size verified at the destination before
the source is unlinked, so a truncated write on a USB volume cannot destroy
the only copy.  Bodies are content-addressed by sha256 filename, so the
archive stays self-verifying.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def move_one(source: Path, destination: Path) -> int:
    """Copy, verify size, then unlink the source.  Returns bytes moved."""

    size = source.stat().st_size
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".partial")
    try:
        shutil.copyfile(source, staging)
        if staging.stat().st_size != size:
            raise OSError(
                f"cold_archive_size_mismatch:{source}:"
                f"{staging.stat().st_size}!={size}"
            )
        os.replace(staging, destination)
    finally:
        if staging.exists():
            staging.unlink()
    # Only now is a verified second copy on the volume.
    source.unlink()
    return size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths-json", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT / "data" / "runtime")
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    paths = [Path(p) for p in json.loads(args.paths_json.read_text())]
    source_root = args.source_root.resolve()
    archive_root = args.archive_root.expanduser()
    moved_bytes = 0
    moved = 0
    skipped = 0
    failures: list[str] = []
    started = time.time()
    for index, source in enumerate(paths, start=1):
        if not source.is_file():
            skipped += 1
            continue
        try:
            relative = source.resolve().relative_to(source_root)
        except ValueError:
            failures.append(f"outside_source_root:{source}")
            continue
        destination = archive_root / relative
        if not args.apply:
            moved += 1
            moved_bytes += source.stat().st_size
            continue
        try:
            moved_bytes += move_one(source, destination)
            moved += 1
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{type(exc).__name__}:{str(exc)[:160]}")
            if len(failures) > 50:
                break
        if index % 2000 == 0:
            print(
                f"  {index}/{len(paths)} moved={moved} "
                f"{moved_bytes/1e9:.2f}GB elapsed={time.time()-started:.0f}s",
                flush=True,
            )
    receipt = {
        "schema": "nwp_raw_cold_archive_move_v1",
        "applied": bool(args.apply),
        "archive_root": str(archive_root),
        "source_root": str(source_root),
        "candidate_count": len(paths),
        "moved_count": moved,
        "moved_bytes": moved_bytes,
        "skipped_missing_count": skipped,
        "failure_count": len(failures),
        "failures": failures[:50],
        "elapsed_sec": round(time.time() - started, 1),
        "boundary": {
            "external_volume_is_cold_archive_only": True,
            "production_dependency": False,
            "verified_before_unlink": True,
        },
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({k: v for k, v in receipt.items() if k != "failures"},
                     ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
