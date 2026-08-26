#!/usr/bin/env python3
"""Plan and safely prune stale one-off dumps under ``output/audits``.

``output/audits`` has no eviction of its own: every investigation, backfill
and manual audit script under ``scripts/`` writes its packet there and
nothing ever removes one.  Measured 2026-08-17: 3.7 GB across 19,140 files,
of which 96% were older than 14 days -- one-off dumps from investigations
long since closed, not evidence any resident path reads.  ``grep`` over
``weather_basket_yes_daemon.py`` and ``production_worker.py`` finds no read
of this directory at runtime; every writer is a ``build_*``/``capture_*``/
``plan_*``/``probe_*``/``audit_*`` script run by hand.

This also indirectly caps the native NWP raw-object stores under
``data/runtime/official_nwp_sources``.  Those stores are swept by
``official_nwp_raw_retention.py --lane native``, which treats a raw body as
garbage only once no reference document under ``output/audits`` names its
hash any more.  A packet that is never pruned keeps naming its body forever,
so the raw stores could grow without bound even with that pass running daily.
Pruning stale packets here is what lets that pass catch up.

Unit of retention is the top-level entry (a file or a directory) directly
under the audits root, not each file inside it: an investigation packet is
written as a single sitting and is judged by the newest file it holds, so a
directory that gained a file yesterday is not stale even if most of its
contents are a month old.

Exactly two entries are protected by name rather than age: the plan and
apply receipts ``official_nwp_raw_retention_daily.sh`` overwrites on every
run.  A substring rule on ``_latest``/``_current`` was tried first and
rejected -- one-off investigation dumps are routinely named that way as a
snapshot label at write time (``bayes_basket_target_production_readiness_
current_v3.json`` is a fixed artifact from 2026-07-25, not a pointer anyone
updates), and the substring match exempted 238 of them, undermining the
policy it was meant to protect. An automated job that rewrites a fixed path
on every run is a different thing from a script that picks a "current"-
sounding name once; only the former earns a name-based exemption, and pure
age already covers it as long as that job runs at least once per window --
this is defence-in-depth for the job stalling longer than that, as it did
2026-08-14 through 2026-08-17 on unreadable references.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Sequence
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.atomic_cutover_receipt import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402

DEFAULT_AUDITS_ROOT = ROOT / "output" / "audits"
DEFAULT_RETENTION_DAYS = 14
PLAN_SCHEMA = "output_audits_retention_plan_v1"
# Exact filenames only -- see the module docstring for why a substring rule
# on "_latest"/"_current" is the wrong tool here.
PROTECTED_EXACT_NAMES = frozenset({
    "official_nwp_native_raw_retention_plan_latest.json",
    "official_nwp_native_raw_retention_apply_latest.json",
})
# Roots scanned for source that names an audits entry, and the bounds that
# keep that scan a scan rather than a walk of the whole workspace.
DEFAULT_REFERENCE_ROOTS = ("scripts", "bayes_basket_target", "config", "tests")
REFERENCE_SUFFIXES = frozenset({".py", ".sh", ".json", ".toml", ".cfg", ".md"})
MAX_REFERENCE_FILES = 20_000
MAX_REFERENCE_FILE_BYTES = 4 * 1024 * 1024


def _entry_newest_mtime(path: Path) -> float:
    if path.is_file():
        return path.stat().st_mtime
    newest = 0.0
    for current, _directories, files in os.walk(path, followlinks=False):
        for name in files:
            candidate = Path(current) / name
            try:
                newest = max(newest, candidate.stat().st_mtime)
            except OSError:
                continue
    return newest


def _entry_bytes_and_count(path: Path) -> tuple[int, int]:
    if path.is_file():
        return path.stat().st_size, 1
    total_bytes = 0
    total_count = 0
    for current, _directories, files in os.walk(path, followlinks=False):
        for name in files:
            candidate = Path(current) / name
            if candidate.is_symlink():
                continue
            try:
                total_bytes += candidate.stat().st_size
            except OSError:
                continue
            total_count += 1
    return total_bytes, total_count


def _is_protected(name: str) -> bool:
    return name in PROTECTED_EXACT_NAMES


def _referencing_sources(
    names: Sequence[str], reference_roots: Sequence[Path]
) -> dict[str, str]:
    """Map each audits entry name to a source file that names it, if any.

    Age is the wrong question for an entry that a script reads on every run.
    ``pr12_awc_observation_sweep.sh`` names one such entry by literal path --
    a sealed authority projection built once, read every ten minutes, and
    never rewritten, so it ages like a dump and is read like a pointer.  On
    2026-08-26 this pass deleted it and the observation-identity lane exited
    78 on the next tick, which is a retention policy stopping production.

    This is the same reference scan the native NWP lane already performs
    before it discards a body, applied to the one thing this pass discards.
    Matching is on the exact entry name, not a substring or a pattern: a name
    that appears verbatim in tracked source is being addressed by something,
    and that is the only claim being made.
    """

    wanted = {str(name) for name in names if str(name)}
    if not wanted:
        return {}
    found: dict[str, str] = {}
    scanned = 0
    for root in reference_roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if scanned >= MAX_REFERENCE_FILES or len(found) == len(wanted):
                break
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix.lower() not in REFERENCE_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_REFERENCE_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                # A reference this pass cannot read cannot prove an entry is
                # unreferenced, so the entry stays: the native lane fails the
                # whole pass closed on this, which is heavier than the one
                # deletion it would prevent here.
                continue
            scanned += 1
            for name in wanted - set(found):
                if name in text:
                    found[name] = str(path)
    return found


def build_retention_plan(
    *,
    audits_root: Path,
    retention_days: int,
    now: datetime | None = None,
    reference_roots: Sequence[Path] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    roots = (
        tuple(reference_roots)
        if reference_roots is not None
        else tuple(ROOT / name for name in DEFAULT_REFERENCE_ROOTS)
    )
    cutoff_epoch = now.timestamp() - max(0, int(retention_days)) * 86400.0
    stale: list[dict[str, Any]] = []
    kept_protected: list[dict[str, Any]] = []
    kept_fresh_bytes = 0
    kept_fresh_count = 0
    if not audits_root.is_dir():
        return {
            "schema": PLAN_SCHEMA,
            "state": "audits_root_missing",
            "audits_root": str(audits_root),
            "retention_days": retention_days,
            "cutoff_utc": datetime.fromtimestamp(
                cutoff_epoch, tz=timezone.utc
            ).isoformat(),
            "stale_entries": [],
            "stale_count": 0,
            "stale_bytes": 0,
            "kept_protected_entries": [],
            "kept_referenced_entries": [],
            "kept_referenced_count": 0,
            "kept_fresh_bytes": 0,
            "kept_fresh_count": 0,
        }
    entries = [
        entry for entry in sorted(audits_root.iterdir())
        if not entry.is_symlink()
    ]
    # Only entries age would otherwise discard are worth scanning for.
    aging = [
        entry.name for entry in entries
        if not _is_protected(entry.name)
        and (_entry_newest_mtime(entry) or 0.0) < cutoff_epoch
    ]
    referenced = _referencing_sources(aging, roots)
    kept_referenced: list[dict[str, Any]] = []
    for entry in entries:
        newest_mtime = _entry_newest_mtime(entry)
        entry_bytes, entry_count = _entry_bytes_and_count(entry)
        if entry.name in referenced:
            kept_referenced.append({
                "path": str(entry),
                "bytes": entry_bytes,
                "file_count": entry_count,
                "referenced_by": referenced[entry.name],
            })
            continue
        if _is_protected(entry.name):
            kept_protected.append({
                "path": str(entry),
                "bytes": entry_bytes,
                "file_count": entry_count,
                "newest_mtime_utc": datetime.fromtimestamp(
                    newest_mtime, tz=timezone.utc
                ).isoformat() if newest_mtime else None,
            })
            continue
        if newest_mtime and newest_mtime < cutoff_epoch:
            stale.append({
                "path": str(entry),
                "is_dir": entry.is_dir(),
                "bytes": entry_bytes,
                "file_count": entry_count,
                "newest_mtime_utc": datetime.fromtimestamp(
                    newest_mtime, tz=timezone.utc
                ).isoformat(),
            })
        else:
            kept_fresh_bytes += entry_bytes
            kept_fresh_count += entry_count
    plan = {
        "schema": PLAN_SCHEMA,
        "state": "retention_plan_ready",
        "generated_at_utc": now.isoformat(),
        "audits_root": str(audits_root),
        "retention_days": retention_days,
        "cutoff_utc": datetime.fromtimestamp(
            cutoff_epoch, tz=timezone.utc
        ).isoformat(),
        "stale_entries": stale,
        "stale_count": len(stale),
        "stale_bytes": sum(item["bytes"] for item in stale),
        "kept_protected_entries": kept_protected,
        "kept_referenced_entries": kept_referenced,
        "kept_referenced_count": len(kept_referenced),
        "kept_fresh_bytes": kept_fresh_bytes,
        "kept_fresh_count": kept_fresh_count,
    }
    return {**plan, "content_sha256": content_sha256(plan)}


def apply_retention_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Delete exactly the entries the plan named, nothing recomputed live.

    Applying against a freshly rebuilt plan would let an entry written
    between planning and apply -- for instance the daily job's own
    ``_latest`` receipt, which is protected by name but not by a live
    directory listing race -- be judged by a different snapshot than the one
    an operator reviewed.  Deleting exactly the planned paths keeps apply a
    replay of a decision already made, not a second decision.
    """

    deleted: list[str] = []
    deleted_bytes = 0
    missing: list[str] = []
    for item in plan.get("stale_entries") or []:
        path = Path(item["path"])
        if not path.exists():
            missing.append(str(path))
            continue
        if path.is_dir() and not path.is_symlink():
            import shutil

            shutil.rmtree(path)
        else:
            path.unlink()
        deleted.append(str(path))
        deleted_bytes += int(item["bytes"])
    return {
        "schema": "output_audits_retention_apply_v1",
        "state": "applied",
        "deleted_count": len(deleted),
        "deleted_bytes": deleted_bytes,
        "deleted_paths": deleted,
        "missing_paths": missing,
        "plan_content_sha256": plan.get("content_sha256"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audits-root", type=Path, default=DEFAULT_AUDITS_ROOT)
    parser.add_argument(
        "--retention-days", type=int, default=DEFAULT_RETENTION_DAYS
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-output", type=Path, default=None)
    parser.add_argument("--result-output", type=Path, default=None)
    args = parser.parse_args(argv)

    plan = build_retention_plan(
        audits_root=args.audits_root.expanduser().resolve(),
        retention_days=args.retention_days,
    )
    if args.plan_output is not None:
        write_bounded_json(args.plan_output, plan)
    if plan["state"] != "retention_plan_ready":
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        return 0

    result: dict[str, Any] = {}
    if args.apply:
        result = apply_retention_plan(plan)
        if args.result_output is not None:
            write_bounded_json(args.result_output, result)

    print(json.dumps({"plan": plan, "apply": result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
