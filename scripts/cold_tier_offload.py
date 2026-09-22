#!/usr/bin/env python3
"""Move the cold end of hot-tier raw directories to the cold tier (charter 196).

User ruling 2026-09-22: 「能否把模型和历史的原始数据移到外置硬盘，但是把校准、
融合、权重、oos、拟合等留在本机？如果可以就执行！」

Charter 167 moved whole directories that nothing live reads.  The raw data
that is left on the internal disk sits in directories production *writes*,
so it cannot move as a directory -- but its old end is read by nobody.  This
moves files, not directories, and only from lanes whose readers were checked
one by one:

``native_capture_archives``
    ``data/runtime/bayes_basket_target_nwp/official_native_capture``.  The
    per-event-date member archives (``{family}_{date}_d{lead}[_{HH}z].json``,
    ``run_{date}_inbox.json``, ...).  The worker and the capture script open
    them only by exact name for the dates they are capturing (horizon: UTC
    today minus one day and later); nothing globs the directory.  The member
    values already live in the SSOT history table (``--commit``), which is
    what calibration, OOS and fitting read -- and that stays.  Cold: the date
    in the name is at least ``--capture-keep-days`` before UTC today.

``madis_hfmetar_raw``
    ``data/raw/madis_hfmetar``.  Content-addressed copies of the hourly MADIS
    objects the observation daemon ingests.  The decoded rows are in the
    database; the retained object is lineage only and is never read back
    (the daemon only fetches ET-today's hours).  Cold: modified at least
    ``--madis-keep-days`` ago.

Deliberately not lanes: ``historical_taf_multianchor`` (the worker globs
``AWC_TAF_*.json`` and re-ingests every archive it finds, so moving files
would change what production reads), the NWP object stores (the retention
pass owns those, with its own cold-tier eviction), and anything in the SSOT
or ``archive.db``.

Each file: verified copy (size + sha256 of the bytes on the volume), manifest
row appended and fsynced on the internal disk, then the hot copy unlinked.
A file some process has open is skipped.  When the cold tier is not usable
(unplugged, TCC-denied under launchd, read-only, on the internal disk, short
of space) the lane does nothing and says why: these files are not re-
obtainable, so there is no discard fallback -- they simply stay hot.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.offloaded_paths import (  # noqa: E402
    DEFAULT_COLD_TIER_MIN_FREE_BYTES,
    cold_tier_root,
    cold_tier_status,
    copy_to_cold_tier,
    evicted_path,
)

MANIFEST = ROOT / "data" / "runtime" / "cold_tier_evictions.jsonl"
CAPTURE_ROOT = ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "official_native_capture"
MADIS_ROOT = ROOT / "data" / "raw" / "madis_hfmetar"
DEFAULT_CAPTURE_KEEP_DAYS = 3
DEFAULT_MADIS_KEEP_DAYS = 7
#: Whatever the name says, a file written this recently may be mid-write.
IN_FLIGHT_FLOOR_SECONDS = 6 * 3600
_DATE_IN_NAME = re.compile(r"_(\d{4}-\d{2}-\d{2})(?=[_.])")
_MADIS_NAME = re.compile(r"^[0-9a-f]{64}\.gz$")


def _candidate_files(root: Path) -> Iterable[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    rows = []
    for entry in os.scandir(root):
        name = entry.name
        if name.startswith(".") or name.endswith((".partial", ".tmp")):
            continue
        if not entry.is_file(follow_symlinks=False):
            continue
        rows.append(Path(entry.path))
    return sorted(rows)


def select_capture_archives(
    root: Path, *, today_utc: datetime, keep_days: int, now_epoch: float
) -> list[Path]:
    """Past-dated capture archives: the first date in the name is at least
    ``keep_days`` before UTC today.  Undated files are never selected."""

    cutoff = (today_utc.date() - timedelta(days=max(1, int(keep_days)))).isoformat()
    selected: list[Path] = []
    for path in _candidate_files(root):
        match = _DATE_IN_NAME.search(path.name)
        if match is None or match.group(1) > cutoff:
            continue
        if now_epoch - path.stat().st_mtime < IN_FLIGHT_FLOOR_SECONDS:
            continue
        selected.append(path)
    return selected


def select_madis_objects(root: Path, *, keep_days: int, now_epoch: float) -> list[Path]:
    floor = max(float(keep_days) * 86400.0, IN_FLIGHT_FLOOR_SECONDS)
    return [
        path
        for path in _candidate_files(root)
        if _MADIS_NAME.fullmatch(path.name) and now_epoch - path.stat().st_mtime >= floor
    ]


def _open_by_any_process(paths: list[Path]) -> set[str]:
    """Paths some process holds open (``lsof``), checked once per lane."""

    if not paths:
        return set()
    directories = sorted({str(path.parent) for path in paths})
    try:
        completed = subprocess.run(
            ["lsof", "-F", "n", "+d", *directories],
            check=False,
            capture_output=True,
            text=True,
            timeout=60.0,
        )
    except (OSError, subprocess.SubprocessError):
        # Unknown is not "nobody": refuse the lane rather than guess.
        return {str(path) for path in paths}
    return {line[1:] for line in completed.stdout.splitlines() if line.startswith("n")}


def _append_manifest(manifest: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def offload(
    lane: str,
    paths: list[Path],
    *,
    cold_status: dict[str, Any],
    manifest: Path,
    apply: bool,
    repo_root: Path = ROOT,
    max_bytes_per_sec: float = 0.0,
    open_paths: Callable[[list[Path]], set[str]] = _open_by_any_process,
    batch_size: int = 200,
) -> dict[str, Any]:
    """Move ``paths`` to the cold tier, verified, manifest before unlink."""

    result: dict[str, Any] = {
        "lane": lane,
        "candidate_count": len(paths),
        "candidate_bytes": sum(path.stat().st_size for path in paths if path.is_file()),
        "cold_tier": cold_status,
        "applied": bool(apply),
        "moved_count": 0,
        "moved_bytes": 0,
        "skipped_open_count": 0,
        "failures": [],
    }
    if not apply:
        return {**result, "state": "dry_run"}
    if cold_status.get("state") != "available":
        # Not re-obtainable: without the cold tier these stay hot.
        return {**result, "state": f"skipped_cold_tier_{cold_status.get('state')}"}
    root = Path(str(cold_status["root"]))
    held = open_paths(paths)
    started = time.monotonic()
    paced = 0
    for start in range(0, len(paths), batch_size):
        batch = paths[start:start + batch_size]
        rows: list[dict[str, Any]] = []
        for path in batch:
            if str(path) in held or str(path.resolve()) in held:
                result["skipped_open_count"] += 1
                continue
            if not path.is_file():
                continue
            try:
                copied = copy_to_cold_tier(path, evicted_path(path, root=root, repo_root=repo_root))
            except Exception as exc:  # noqa: BLE001 - stop the lane, keep the file
                result["failures"].append(f"{path.name}:{type(exc).__name__}:{str(exc)[:160]}")
                break
            rows.append(
                {
                    "schema": "cold_tier_eviction_v1",
                    "lane": lane,
                    "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "path": str(path),
                    "cold_path": copied["cold_path"],
                    "sha256": copied["sha256"],
                    "size_bytes": copied["size_bytes"],
                    "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                    "verified": "size+sha256_on_volume",
                    "production_dependency": False,
                }
            )
            if max_bytes_per_sec > 0:
                paced += int(copied["size_bytes"])
                ahead = paced / max_bytes_per_sec - (time.monotonic() - started)
                if ahead > 0:
                    time.sleep(min(ahead, 5.0))
        _append_manifest(manifest, rows)
        for row in rows:
            Path(row["path"]).unlink(missing_ok=True)
            result["moved_count"] += 1
            result["moved_bytes"] += int(row["size_bytes"])
        if result["failures"]:
            break
    result["state"] = "failed" if result["failures"] else "applied"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lane",
        action="append",
        choices=("native_capture_archives", "madis_hfmetar_raw"),
        help="repeatable; default both",
    )
    parser.add_argument("--cold-tier-root", type=Path, default=None)
    parser.add_argument(
        "--cold-tier-min-free-gib", type=float, default=DEFAULT_COLD_TIER_MIN_FREE_BYTES / 1024**3
    )
    parser.add_argument("--capture-keep-days", type=int, default=DEFAULT_CAPTURE_KEEP_DAYS)
    parser.add_argument("--madis-keep-days", type=int, default=DEFAULT_MADIS_KEEP_DAYS)
    parser.add_argument("--max-mb-per-sec", type=float, default=0.0)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    root = args.cold_tier_root if args.cold_tier_root is not None else cold_tier_root()
    status = cold_tier_status(root, min_free_bytes=int(args.cold_tier_min_free_gib * 1024**3))
    now = datetime.now(timezone.utc)
    lanes = args.lane or ["native_capture_archives", "madis_hfmetar_raw"]
    results = []
    for lane in lanes:
        if lane == "native_capture_archives":
            paths = select_capture_archives(
                CAPTURE_ROOT, today_utc=now, keep_days=args.capture_keep_days,
                now_epoch=now.timestamp(),
            )
        else:
            paths = select_madis_objects(
                MADIS_ROOT, keep_days=args.madis_keep_days, now_epoch=now.timestamp()
            )
        results.append(
            offload(
                lane,
                paths,
                cold_status=status,
                manifest=args.manifest,
                apply=args.apply,
                max_bytes_per_sec=args.max_mb_per_sec * 1024**2,
            )
        )
    receipt = {
        "schema": "cold_tier_offload_receipt_v1",
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "cold_tier": status,
        "lanes": results,
    }
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in results:
        print(
            f"  {row['lane']}: state={row.get('state')} 候选={row['candidate_count']}"
            f" ({row['candidate_bytes'] / 1024**2:.1f} MiB) 已搬={row['moved_count']}"
            f" ({row['moved_bytes'] / 1024**2:.1f} MiB) 占用跳过={row['skipped_open_count']}"
            f" 失败={len(row['failures'])}"
        )
    print(f"  cold_tier: state={status.get('state')} root={status.get('root')}")
    return 1 if any(row.get("state") == "failed" for row in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
