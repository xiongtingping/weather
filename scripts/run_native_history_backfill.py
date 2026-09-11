#!/usr/bin/env python3
"""Foreground, bounded, resumable backfill of native ECMWF PF50 / GEFS history.

Walks event dates *backwards* from ``--from-date`` to ``--to-date``.  For every
event date it captures the runs that give every station both its ``h12_24``
and its ``h24_48`` scope with the fewest global-field downloads: the 00Z and
12Z cycles at lead 1 and lead 2.  The hour bucket is stamped per station by
the collector from the run it actually read, so one run serves Asia as
``h12_24`` and the Americas as ``h24_48`` at the same time; the 12Z run is
the only one that gives the Americas ``h12_24`` at all, which is why the
lead defaults (d2 18Z, d1/d0 00Z) are not enough.

Walking backwards makes the two lead-2 runs of event date D the two lead-1
runs of event date D-1, so with the scratch raw store kept for a few hours
each run is downloaded once.  Raw objects and packets never touch the
production retention roots: they live under ``--scratch`` and are pruned by
age here.

Captures already in the SSOT (all active stations present for that event
date and extraction version) are skipped, so the loop is idempotent and can
be stopped and restarted at any point.  This process is meant to be run in
a terminal the operator can see; ``--max-event-days`` / ``--max-seconds``
bound one invocation.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import ecmwf_ifs_archive, noaa_gefs_archive  # noqa: E402
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402
from bayes_basket_target.market_registry import (  # noqa: E402
    load_active_registry_rows,
)

GCS_REPLICA = ecmwf_ifs_archive.GCS_OFFICIAL_REPLICA_BASE_URL
DEFAULT_SCRATCH = ROOT / "data" / "runtime" / "native_history_backfill"

PROVIDERS = {
    "ecmwf": {
        "script": ROOT / "scripts" / "backfill_ecmwf_ifs_multistation_archive.py",
        "system_id": ecmwf_ifs_archive.SYSTEM_ID,
        "transport": "ECMWF_OPEN_DATA_S3",
        "extraction_version": ecmwf_ifs_archive.extraction_version_for_lead,
        "raw_env": "ECMWF_PF50_RAW_OBJECT_DIR",
        "packet_env": "ECMWF_PF50_RETAINED_PACKET_DIR",
    },
    "gefs": {
        "script": ROOT / "scripts" / "backfill_noaa_gefs_multistation_archive.py",
        "system_id": noaa_gefs_archive.SYSTEM_ID,
        "transport": "NOAA_GEFS_PDS_S3",
        "extraction_version": noaa_gefs_archive.extraction_version_for_lead,
        "raw_env": "GEFS_RAW_OBJECT_DIR",
        "packet_env": "GEFS_RETAINED_PACKET_DIR",
    },
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _coordinate_roster_count(db: Path) -> int:
    """Active stations the native collectors can decode (have coordinates)."""
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        stations = {
            str(row.get("station_id") or "").strip().upper()
            for row in load_active_registry_rows(conn)
        }
    count = 0
    for station in stations:
        try:
            station_coordinates(station)
        except ContractError:
            continue
        count += 1
    return count


def _stations_present(
    db: Path, *, system_id: str, transport: str, event_date: str,
    extraction_version: str,
) -> int:
    # Seekable on calibration_native_member_skill_idx
    # (system, transport, qc, target_local_date); verified with
    # EXPLAIN QUERY PLAN before this lane was written.
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute(
            "SELECT COUNT(DISTINCT station_id) "
            "FROM calibration_native_ensemble_historical_member_run "
            "WHERE ensemble_system_id=? AND transport_provider_id=? "
            "AND qc_status='ACCEPTED' AND target_local_date=? "
            "AND extraction_version=?",
            (system_id, transport, event_date, extraction_version),
        ).fetchone()
    return int(row[0] or 0)


def _prune(root: Path, *, keep_hours: float) -> tuple[int, int]:
    cutoff = time.time() - keep_hours * 3600.0
    removed = 0
    freed = 0
    if not root.exists():
        return 0, 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff:
                try:
                    path.unlink()
                    removed += 1
                    freed += stat.st_size
                except OSError:
                    pass
    return removed, freed


def _run(cmd: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return -1, str(exc.stdout or ""), "capture_timeout"
    return proc.returncode, proc.stdout, proc.stderr


def _tree_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--from-date", required=True, help="first (newest) event date")
    parser.add_argument("--to-date", required=True, help="last (oldest) event date, inclusive")
    parser.add_argument("--providers", default="gefs,ecmwf")
    # lead:cycle pairs.  (2, 00Z) is deliberately absent: for the Americas the
    # D-2 00Z run ends before the local day does (coverage refuses it), and
    # everywhere else it only repeats the h24_48 that (2, 12Z) already gives.
    parser.add_argument("--lead-cycles", default="1:0,1:12,2:12")
    parser.add_argument("--max-event-days", type=int, default=1)
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--capture-timeout-seconds", type=float, default=3600.0)
    parser.add_argument("--lock-retries", type=int, default=3)
    parser.add_argument("--lock-retry-seconds", type=float, default=20.0)
    parser.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH)
    parser.add_argument("--raw-keep-hours", type=float, default=3.0)
    parser.add_argument("--ecmwf-workers", type=int, default=8)
    parser.add_argument("--ecmwf-base-url", default=GCS_REPLICA)
    parser.add_argument(
        "--skip-if-stations-at-least", type=int, default=None,
        help="treat a capture as done when this many stations are already "
             "in the SSOT for its extraction version (default: every active "
             "station that has coordinates; the three lead:cycle pairs used "
             "here cover the whole roster, and a partial capture must never "
             "be topped up -- row identity carries the query time, so a "
             "second capture of the same station-day-version collides).",
    )
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()

    db = args.db.expanduser().resolve(strict=True)
    providers = [p.strip().lower() for p in args.providers.split(",") if p.strip()]
    unknown = set(providers) - set(PROVIDERS)
    if unknown:
        raise SystemExit(f"unknown providers: {sorted(unknown)}")
    lead_cycles: list[tuple[int, int]] = []
    for item in args.lead_cycles.split(","):
        item = item.strip()
        if not item:
            continue
        lead_text, cycle_text = item.split(":")
        lead_cycles.append((int(lead_text), int(cycle_text)))
    lead_cycles.sort()
    start = date.fromisoformat(args.from_date)
    stop = date.fromisoformat(args.to_date)
    if stop > start:
        raise SystemExit("--to-date must not be after --from-date (walk is backwards)")

    scratch = args.scratch.expanduser().resolve()
    raw_dirs = {p: scratch / f"raw_{p}" for p in providers}
    packet_dirs = {p: scratch / "packets" / p for p in providers}
    for path in list(raw_dirs.values()) + list(packet_dirs.values()):
        path.mkdir(parents=True, exist_ok=True)
    progress = scratch / "progress.jsonl"
    roster = _coordinate_roster_count(db)
    done_threshold = int(args.skip_if_stations_at_least or roster)

    t0 = time.monotonic()
    event_days_done = 0
    captures_run = 0
    captures_skipped = 0
    rows_written = 0
    bytes_downloaded = 0
    current = start
    stopped_reason = "to_date_reached"
    while current >= stop:
        if event_days_done >= args.max_event_days:
            stopped_reason = "max_event_days"
            break
        event_date = current.isoformat()
        # Lead-1 runs first: they were the previous event date's lead-2 runs
        # and are still in the scratch store, so they are cheap.
        for lead, cycle in lead_cycles:
            for provider in providers:
                if (
                    args.max_seconds is not None
                    and time.monotonic() - t0 > args.max_seconds
                ):
                    stopped_reason = "max_seconds"
                    current = stop - timedelta(days=1)
                    break
                spec = PROVIDERS[provider]
                version = spec["extraction_version"](lead, cycle)
                present = _stations_present(
                    db, system_id=spec["system_id"],
                    transport=spec["transport"], event_date=event_date,
                    extraction_version=version,
                )
                record = {
                    "at": _utc_now(), "event_date": event_date,
                    "provider": provider, "lead_days": lead,
                    "run_cycle_hour": cycle, "extraction_version": version,
                    "stations_present_before": present,
                }
                if present >= done_threshold:
                    captures_skipped += 1
                    record["state"] = "skipped_present"
                    with progress.open("a") as fh:
                        fh.write(json.dumps(record, sort_keys=True) + "\n")
                    continue
                env = dict(os.environ)
                env[spec["raw_env"]] = str(raw_dirs[provider])
                env[spec["packet_env"]] = str(packet_dirs[provider])
                if provider == "ecmwf":
                    env["ECMWF_ARCHIVE_TRANSPORT_BASE_URL"] = args.ecmwf_base_url
                    env["ECMWF_ARCHIVE_MAX_WORKERS"] = str(args.ecmwf_workers)
                    # Reuse only from this lane's own store; the production
                    # roots hold nothing older than 30 days anyway.
                    env["ECMWF_PF50_RAW_SEARCH_ROOTS"] = ""
                output = packet_dirs[provider] / (
                    f"{provider}_{event_date}_l{lead}_c{cycle:02d}.json"
                )
                cmd = [
                    args.python, str(spec["script"]),
                    "--db", str(db), "--event-date", event_date,
                    "--lead-days", str(lead), "--run-cycle-hour", str(cycle),
                    "--output", str(output), "--commit",
                ]
                started = time.monotonic()
                rc, stdout, stderr = _run(cmd, env, args.capture_timeout_seconds)
                # A capture whose download succeeded but whose commit lost the
                # SSOT write lock has already written its packet: ingest that
                # packet again instead of downloading the run a second time.
                lock_retries = 0
                while (
                    rc != 0
                    and "database is locked" in (stderr or "")
                    and output.exists()
                    and lock_retries < args.lock_retries
                ):
                    lock_retries += 1
                    time.sleep(args.lock_retry_seconds)
                    retry_cmd = [
                        args.python, str(spec["script"]),
                        "--db", str(db), "--event-date", event_date,
                        "--lead-days", str(lead), "--run-cycle-hour", str(cycle),
                        "--input", str(output), "--commit",
                    ]
                    rc, stdout, stderr = _run(
                        retry_cmd, env, args.capture_timeout_seconds
                    )
                elapsed = time.monotonic() - started
                captures_run += 1
                summary = None
                if rc == 0:
                    for line in reversed((stdout or "").splitlines()):
                        line = line.strip()
                        if line.startswith("{"):
                            try:
                                summary = json.loads(line)
                            except json.JSONDecodeError:
                                summary = None
                            break
                ingress = (summary or {}).get("ingress") or {}
                wrote = int(ingress.get("member_run_wrote_count") or 0)
                got = int((summary or {}).get("total_response_bytes") or 0)
                rows_written += wrote
                bytes_downloaded += got
                child_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
                record.update({
                    "state": "ok" if rc == 0 else "failed",
                    "returncode": rc,
                    "lock_retries": lock_retries,
                    "elapsed_seconds": round(elapsed, 1),
                    "downloaded_fields": (summary or {}).get(
                        "downloaded_field_receipt_count"
                    ),
                    "response_bytes": got,
                    "member_run_wrote_count": wrote,
                    "ingress_state": ingress.get("state"),
                    "station_count": (summary or {}).get("station_count"),
                    "children_max_rss_mb": round(child_rss / 2**20, 1),
                    "stderr_tail": (stderr or "")[-600:] if rc != 0 else "",
                })
                with progress.open("a") as fh:
                    fh.write(json.dumps(record, sort_keys=True) + "\n")
                print(
                    f"{event_date} {provider:5s} l{lead} {cycle:02d}Z "
                    f"{record['state']:7s} {elapsed:6.0f}s "
                    f"{got/2**20:7.1f}MB rows+{wrote} rss{record['children_max_rss_mb']:.0f}MB",
                    flush=True,
                )
                if rc != 0:
                    print((stderr or "")[-600:], file=sys.stderr, flush=True)
            else:
                continue
            break
        if stopped_reason == "max_seconds":
            break
        removed, freed = 0, 0
        for path in raw_dirs.values():
            r, f = _prune(path, keep_hours=args.raw_keep_hours)
            removed += r
            freed += f
        for path in packet_dirs.values():
            r, f = _prune(path, keep_hours=24.0)
            removed += r
            freed += f
        event_days_done += 1
        current -= timedelta(days=1)
        print(
            f"-- {event_date} done; pruned {removed} files / {freed/2**20:.0f}MB; "
            f"scratch now {_tree_bytes(scratch)/2**20:.0f}MB",
            flush=True,
        )
    total = time.monotonic() - t0
    summary = {
        "at": _utc_now(), "state": "stopped", "reason": stopped_reason,
        "event_days_done": event_days_done, "captures_run": captures_run,
        "captures_skipped": captures_skipped, "rows_written": rows_written,
        "bytes_downloaded": bytes_downloaded, "elapsed_seconds": round(total, 1),
        "next_event_date": current.isoformat() if current >= stop else None,
    }
    with progress.open("a") as fh:
        fh.write(json.dumps(summary, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
