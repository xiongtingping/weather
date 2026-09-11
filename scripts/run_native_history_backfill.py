#!/usr/bin/env python3
"""Foreground, bounded, resumable backfill of native ECMWF PF50 / GEFS history.

Walks event dates *backwards* from ``--from-date`` to ``--to-date``.  For every
event date it captures the runs that give every station both its ``h12_24``
and its ``h24_48`` scope with the fewest global-field downloads: the 00Z and
12Z cycles at lead 1 and the 12Z cycle at lead 2.  The hour bucket is stamped
per station by the collector from the run it actually read, so one run serves
Asia as ``h12_24`` and the Americas as ``h24_48`` at the same time; the 12Z
run is the only one that gives the Americas ``h12_24`` at all, which is why
the lead defaults (d2 18Z, d1/d0 00Z) are not enough.

Walking backwards makes the lead-2 run of event date D the lead-1 run of
event date D-1, so with the scratch raw store kept for a few hours each run
is downloaded once.  Raw objects and packets never touch the production
retention roots: they live under ``--scratch`` and are pruned by age here.

Download and ingest are decoupled.  A capture only writes its packet; a
separate queue ingests packets whenever the SSOT write lock is free.  The
ledger sweep and the resident can hold that lock for many minutes at a
stretch (measured 2026-09-11: sixty consecutive busy seconds), and a download
that waits for a commit before starting the next one would spend most of its
life waiting.  A packet on disk is the capture; it is ingested with short lock
waits, retried on the next pass, never re-downloaded, and never pruned until
its rows are in the SSOT.

A capture is skipped only when every station of the coordinate roster is
already present for its extraction version, or when its packet is already
waiting in the queue.  A partial capture must never be topped up: the row
identity carries the query time, so a second capture of the same
station-day-version collides on the archive's unique key.

Meant to run in a terminal the operator can see; ``--max-event-days`` /
``--max-seconds`` bound one invocation and the loop is idempotent.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
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
from bayes_basket_target.market_registry import (  # noqa: E402
    load_active_registry_rows,
)
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402

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
LOCKED = "database is locked"


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
    # (system, transport, qc, target_local_date); checked with
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


def _run(cmd: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return -1, str(exc.stdout or ""), "capture_timeout"
    return proc.returncode, proc.stdout, proc.stderr


def _summary_line(stdout: str) -> dict:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return {}
            break
    return {}


def _prune(root: Path, *, keep_hours: float, keep: set[Path] | None = None) -> tuple[int, int]:
    cutoff = time.time() - keep_hours * 3600.0
    removed = 0
    freed = 0
    if not root.exists():
        return 0, 0
    for path in root.rglob("*"):
        if not path.is_file() or (keep and path.resolve() in keep):
            continue
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


def _tree_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


class Packet:
    """One capture waiting in the scratch store, identified by its file name."""

    def __init__(self, provider: str, event_date: str, lead: int, cycle: int, path: Path):
        self.provider = provider
        self.event_date = event_date
        self.lead = lead
        self.cycle = cycle
        self.path = path
        self.failures = 0

    @property
    def version(self) -> str:
        return PROVIDERS[self.provider]["extraction_version"](self.lead, self.cycle)

    @staticmethod
    def name(provider: str, event_date: str, lead: int, cycle: int) -> str:
        return f"{provider}_{event_date}_l{lead}_c{cycle:02d}.json"

    @classmethod
    def parse(cls, provider: str, path: Path) -> "Packet | None":
        stem = path.name
        prefix = f"{provider}_"
        if not stem.startswith(prefix) or not stem.endswith(".json"):
            return None
        try:
            body = stem[len(prefix):-5]
            event_date, lead_text, cycle_text = body.split("_")
            date.fromisoformat(event_date)
            return cls(provider, event_date, int(lead_text[1:]), int(cycle_text[1:]), path)
        except (ValueError, IndexError):
            return None

    def valid(self) -> bool:
        try:
            with self.path.open("rb") as fh:
                head = fh.read(4096)
            return (
                f'"event_date": "{self.event_date}"'.encode() in head
                or f'"event_date":"{self.event_date}"'.encode() in head
                or self._full_check()
            )
        except OSError:
            return False

    def _full_check(self) -> bool:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                archive = json.load(fh)
        except (OSError, ValueError):
            return False
        return isinstance(archive, dict) and archive.get("event_date") == self.event_date


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
    parser.add_argument(
        "--ingest-lock-wait-seconds", type=float, default=45.0,
        help="how long one queued ingest waits for the SSOT write lock "
             "before the packet is left for the next pass",
    )
    parser.add_argument(
        "--final-lock-wait-seconds", type=float, default=300.0,
        help="lock wait for the last drain before this invocation exits",
    )
    parser.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH)
    parser.add_argument("--raw-keep-hours", type=float, default=3.0)
    parser.add_argument("--ecmwf-workers", type=int, default=24)
    parser.add_argument("--ecmwf-base-url", default=GCS_REPLICA)
    parser.add_argument(
        "--skip-if-stations-at-least", type=int, default=None,
        help="treat a capture as ingested when this many stations are "
             "present for its extraction version (default: every active "
             "station that has coordinates; the three lead:cycle pairs used "
             "here cover the whole roster)",
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

    def env_for(provider: str, lock_wait: float) -> dict[str, str]:
        spec = PROVIDERS[provider]
        env = dict(os.environ)
        env[spec["raw_env"]] = str(raw_dirs[provider])
        env[spec["packet_env"]] = str(packet_dirs[provider])
        env["NATIVE_HISTORY_LOCK_WAIT_SECONDS"] = str(lock_wait)
        if provider == "ecmwf":
            env["ECMWF_ARCHIVE_TRANSPORT_BASE_URL"] = args.ecmwf_base_url
            env["ECMWF_ARCHIVE_MAX_WORKERS"] = str(args.ecmwf_workers)
            # Reuse only from this lane's own store; the production roots
            # hold nothing older than 30 days anyway.
            env["ECMWF_PF50_RAW_SEARCH_ROOTS"] = ""
        return env

    def log(record: dict) -> None:
        record.setdefault("at", _utc_now())
        with progress.open("a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def present(packet: Packet) -> int:
        spec = PROVIDERS[packet.provider]
        return _stations_present(
            db, system_id=spec["system_id"], transport=spec["transport"],
            event_date=packet.event_date, extraction_version=packet.version,
        )

    # The queue: every well-named packet in the scratch store whose rows are
    # not yet in the SSOT.  Rebuilt from disk at start, so a killed run loses
    # nothing but the capture it was in the middle of.
    queue: dict[Path, Packet] = {}
    for provider in providers:
        for path in sorted(packet_dirs[provider].glob(f"{provider}_*.json")):
            packet = Packet.parse(provider, path)
            if packet is None or not packet.valid():
                continue
            if present(packet) >= done_threshold:
                continue
            queue[path.resolve()] = packet

    t0 = time.monotonic()
    stats = {
        "captures_run": 0, "captures_skipped_present": 0,
        "captures_skipped_queued": 0, "captures_failed": 0,
        "ingests_committed": 0, "ingests_lock_busy": 0, "ingests_failed": 0,
        "rows_written": 0, "bytes_downloaded": 0,
    }

    def out_of_time() -> bool:
        return args.max_seconds is not None and time.monotonic() - t0 > args.max_seconds

    def drain(lock_wait: float) -> None:
        """Ingest queued packets until the lock is busy or the queue is empty."""
        for path in sorted(queue, key=lambda p: p.stat().st_mtime if p.exists() else 0):
            packet = queue[path]
            if not path.exists():
                del queue[path]
                continue
            spec = PROVIDERS[packet.provider]
            cmd = [
                args.python, str(spec["script"]), "--db", str(db),
                "--event-date", packet.event_date, "--lead-days", str(packet.lead),
                "--run-cycle-hour", str(packet.cycle), "--input", str(path), "--commit",
            ]
            started = time.monotonic()
            rc, stdout, stderr = _run(cmd, env_for(packet.provider, lock_wait), lock_wait + 600.0)
            elapsed = time.monotonic() - started
            summary = _summary_line(stdout) if rc == 0 else {}
            ingress = summary.get("ingress") or {}
            wrote = int(ingress.get("member_run_wrote_count") or 0)
            record = {
                "kind": "ingest", "event_date": packet.event_date,
                "provider": packet.provider, "lead_days": packet.lead,
                "run_cycle_hour": packet.cycle, "extraction_version": packet.version,
                "elapsed_seconds": round(elapsed, 1), "returncode": rc,
                "packet": path.name,
            }
            if rc == 0 and ingress.get("state") == "COMMITTED":
                stats["ingests_committed"] += 1
                stats["rows_written"] += wrote
                record.update({"state": "committed", "member_run_wrote_count": wrote})
                del queue[path]
                log(record)
                print(
                    f"   ingest {packet.event_date} {packet.provider:5s} l{packet.lead} "
                    f"{packet.cycle:02d}Z committed rows+{wrote} {elapsed:5.0f}s",
                    flush=True,
                )
                continue
            if LOCKED in (stderr or ""):
                stats["ingests_lock_busy"] += 1
                record["state"] = "lock_busy"
                log(record)
                print(
                    f"   ingest {packet.event_date} {packet.provider:5s} l{packet.lead} "
                    f"{packet.cycle:02d}Z lock busy after {elapsed:.0f}s; "
                    f"{len(queue)} packet(s) queued",
                    flush=True,
                )
                return
            packet.failures += 1
            stats["ingests_failed"] += 1
            record.update({"state": "failed", "stderr_tail": (stderr or "")[-600:]})
            log(record)
            print((stderr or "")[-600:], file=sys.stderr, flush=True)
            if packet.failures >= 3:
                # Three non-lock failures: the packet itself is the problem.
                # Park it where the queue no longer sees it and move on.
                parked = path.with_suffix(".failed.json")
                try:
                    path.rename(parked)
                except OSError:
                    pass
                del queue[path]

    drain(args.ingest_lock_wait_seconds)

    event_days_done = 0
    current = start
    stopped_reason = "to_date_reached"
    while current >= stop:
        if event_days_done >= args.max_event_days:
            stopped_reason = "max_event_days"
            break
        if out_of_time():
            stopped_reason = "max_seconds"
            break
        event_date = current.isoformat()
        for lead, cycle in lead_cycles:
            for provider in providers:
                if out_of_time():
                    stopped_reason = "max_seconds"
                    break
                spec = PROVIDERS[provider]
                path = (packet_dirs[provider] / Packet.name(provider, event_date, lead, cycle)).resolve()
                packet = Packet(provider, event_date, lead, cycle, path)
                record = {
                    "kind": "capture", "event_date": event_date, "provider": provider,
                    "lead_days": lead, "run_cycle_hour": cycle,
                    "extraction_version": packet.version,
                }
                if path in queue:
                    stats["captures_skipped_queued"] += 1
                    record["state"] = "skipped_queued"
                    log(record)
                    continue
                before = present(packet)
                record["stations_present_before"] = before
                if before >= done_threshold:
                    stats["captures_skipped_present"] += 1
                    record["state"] = "skipped_present"
                    log(record)
                    continue
                if before > 0:
                    # A partial capture can never be topped up (row identity
                    # carries the query time); say so loudly and move on.
                    stats["captures_failed"] += 1
                    record["state"] = "refused_partial_present"
                    log(record)
                    print(
                        f"{event_date} {provider:5s} l{lead} {cycle:02d}Z REFUSED: "
                        f"{before} station(s) already present, cannot top up",
                        flush=True,
                    )
                    continue
                cmd = [
                    args.python, str(spec["script"]), "--db", str(db),
                    "--event-date", event_date, "--lead-days", str(lead),
                    "--run-cycle-hour", str(cycle), "--output", str(path),
                ]
                started = time.monotonic()
                rc, stdout, stderr = _run(cmd, env_for(provider, 1.0), args.capture_timeout_seconds)
                elapsed = time.monotonic() - started
                stats["captures_run"] += 1
                summary = _summary_line(stdout) if rc == 0 else {}
                got = int(summary.get("total_response_bytes") or 0)
                stats["bytes_downloaded"] += got
                child_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
                record.update({
                    "returncode": rc, "elapsed_seconds": round(elapsed, 1),
                    "downloaded_fields": summary.get("downloaded_field_receipt_count"),
                    "response_bytes": got, "station_count": summary.get("station_count"),
                    "stations_without_coordinates": summary.get("stations_without_coordinates"),
                    "children_max_rss_mb": round(child_rss / 2**20, 1),
                })
                if rc == 0 and path.exists() and packet.valid():
                    record["state"] = "captured"
                    queue[path] = packet
                else:
                    stats["captures_failed"] += 1
                    record.update({"state": "failed", "stderr_tail": (stderr or "")[-600:]})
                    print((stderr or "")[-600:], file=sys.stderr, flush=True)
                log(record)
                print(
                    f"{event_date} {provider:5s} l{lead} {cycle:02d}Z {record['state']:8s} "
                    f"{elapsed:6.0f}s {got/2**20:7.1f}MB rss{record['children_max_rss_mb']:.0f}MB",
                    flush=True,
                )
                drain(args.ingest_lock_wait_seconds)
            else:
                continue
            break
        if stopped_reason == "max_seconds":
            break
        # Prune: raw objects by age; packets only once their rows are in.
        keep = set(queue)
        removed, freed = 0, 0
        for path in raw_dirs.values():
            r, f = _prune(path, keep_hours=args.raw_keep_hours)
            removed += r
            freed += f
        for path in packet_dirs.values():
            r, f = _prune(path, keep_hours=24.0, keep=keep)
            removed += r
            freed += f
        event_days_done += 1
        current -= timedelta(days=1)
        print(
            f"-- {event_date} done; {len(queue)} packet(s) queued; pruned {removed} files "
            f"/ {freed/2**20:.0f}MB; scratch {_tree_bytes(scratch)/2**20:.0f}MB",
            flush=True,
        )

    if queue:
        drain(args.final_lock_wait_seconds)
    total = time.monotonic() - t0
    summary = {
        "kind": "stopped", "reason": stopped_reason, **stats,
        "event_days_done": event_days_done, "queued_packets": sorted(p.name for p in queue),
        "elapsed_seconds": round(total, 1),
        "next_event_date": current.isoformat() if current >= stop else None,
    }
    log(summary)
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
