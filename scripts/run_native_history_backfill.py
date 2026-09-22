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


# ---------------------------------------------------------------- live yield
#
# Being willing to wait is not the same as being willing to let go.  Ingest
# takes ``BEGIN IMMEDIATE`` for a packet of roughly twelve thousand rows, the
# drain loop starts the next packet the instant the last one commits, and a
# busy lock is retried rather than backed off.  Measured 2026-09-13 against a
# 43.8 GB target at 85% of its cap: the write lock was free on 0 of 32 probes
# over thirty seconds while this lane ran, and 74 of 74 once it stopped.  The
# live capture lanes wait 120 seconds and then raise, so every collision went
# the same way -- jma_msm and dwd_icon_eu had each been attempted once in five
# hours and failed both times with "database is locked", and the resident's
# own decision rate fell from 114 an hour to 33.
#
# So the lane yields on two counts.  It stands aside while a live SSOT sweep
# is overdue by more than one of its own intervals, and it paces itself
# between packets so the lock has an idle window even when nothing is late.
# Neither is a lock wait: waiting longer would not have helped, because there
# was no window to wait for.
LIVE_YIELD_POLL_SECONDS = 15.0


def _hosted_sweep_contract() -> tuple[dict[str, float], Path]:
    """Read the resident's own sweep table rather than restating it here."""

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_native_history_hosted_sweep_contract",
        ROOT / "scripts" / "weather_basket_yes_daemon.py",
    )
    if spec is None or spec.loader is None:
        raise ContractError("hosted_sweep_contract_unreadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    intervals = {
        str(name): float(interval)
        for name, _script, interval, _timeout in module.HOSTED_SWEEPS
        if module.HOSTED_SWEEP_WRITE_DOMAIN.get(str(name)) == "ssot"
    }
    if not intervals:
        raise ContractError("hosted_sweep_contract_has_no_ssot_member")
    return intervals, Path(module.HOSTED_SWEEP_CLOCKS_PATH)


def overdue_ssot_lanes(
    intervals: dict[str, float],
    clocks: dict,
    now_epoch: float,
    *,
    rounds: float = 1.0,
) -> list[tuple[str, float]]:
    """SSOT sweeps overdue by more than ``rounds`` of their own interval.

    A lane with no clock entry has never run under this contract and is not
    evidence of contention, so it is not counted; an unparsable stamp is,
    because a lane whose clock cannot be read is a lane nobody can show is
    being served.
    """

    late: list[tuple[str, float]] = []
    for name, interval in sorted(intervals.items()):
        entry = clocks.get(name)
        if not isinstance(entry, dict):
            continue
        stamp = str(entry.get("attempted_at_utc") or "").strip()
        if not stamp:
            continue
        try:
            attempted = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            late.append((name, float("inf")))
            continue
        if attempted.tzinfo is None:
            attempted = attempted.replace(tzinfo=timezone.utc)
        if interval <= 0:
            continue
        behind = (now_epoch - attempted.timestamp()) / interval
        if behind > rounds:
            late.append((name, behind))
    return late


def stand_aside_for_live_lanes(
    intervals: dict[str, float],
    read_clocks,
    *,
    rounds: float,
    max_seconds: float,
    sleep=time.sleep,
    now=time.time,
    poll_seconds: float = LIVE_YIELD_POLL_SECONDS,
) -> dict:
    """Wait while a live SSOT sweep is overdue; never past ``max_seconds``.

    ``read_clocks`` is re-read every poll, so the wait ends as soon as the
    resident serves the late lane and stamps its clock -- which is the point
    of standing aside.  The result says how long this cost and why it ended.
    """

    waited = 0.0
    first_late: list[tuple[str, float]] = []
    if rounds <= 0:
        return {"waited_seconds": 0.0, "ended": "disabled", "late": []}
    while True:
        try:
            clocks = read_clocks()
        except (OSError, ValueError):
            clocks = {}
        late = overdue_ssot_lanes(
            intervals, clocks if isinstance(clocks, dict) else {}, now(), rounds=rounds,
        )
        if not late:
            return {"waited_seconds": waited,
                    "ended": "clear" if waited else "not_needed",
                    "late": first_late}
        if not first_late:
            first_late = late
        if waited >= max_seconds:
            return {"waited_seconds": waited, "ended": "cap_reached", "late": late}
        sleep(poll_seconds)
        waited += poll_seconds


def pace_after_commit(
    held_seconds: float,
    *,
    ratio: float,
    max_seconds: float,
    sleep=time.sleep,
) -> float:
    """Leave the lock idle for ``ratio`` times as long as it was just held."""

    if ratio <= 0 or held_seconds <= 0:
        return 0.0
    pause = min(held_seconds * ratio, max_seconds)
    if pause > 0:
        sleep(pause)
    return pause


# Charter 193 amendment (the D-3 18Z backfill, 2026-09-22 ruling "要"): the
# production captures share this machine's network and proxy -- the night of
# 2026-09-21 they lost ten hours of P0 to ConnectError/PoolTimeout -- so the
# lane stands aside while one is running and stops on the first sign of a
# transport failure rather than retrying into it.
PRODUCTION_CAPTURE_PATTERN = "capture_official_native_ensemble_forward.py"
TRANSPORT_FAILURE_MARKERS = (
    "transport_failed", "ConnectError", "PoolTimeout", "ReadTimeout",
    "ConnectTimeout", "RemoteProtocolError", "capture_timeout",
)


def production_capture_running(pattern: str = PRODUCTION_CAPTURE_PATTERN) -> bool:
    """Whether a production NWP capture (pass or follow lane) is in flight."""

    try:
        found = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, check=False,
        )
    except OSError:
        return False
    return bool((found.stdout or "").strip())


def stand_aside_for_production_captures(
    *,
    max_seconds: float,
    running=production_capture_running,
    sleep=time.sleep,
    poll_seconds: float = LIVE_YIELD_POLL_SECONDS,
) -> float:
    """Wait while a production capture runs; never past ``max_seconds``."""

    waited = 0.0
    while running() and waited < max_seconds:
        sleep(poll_seconds)
        waited += poll_seconds
    return waited


PRODUCTION_STATUS_PATH = ROOT / "data" / "weather_basket_yes_status.json"
PRODUCTION_TROUBLE_WINDOW_SECONDS = 3600.0


def production_network_trouble(
    status: dict,
    *,
    now_epoch: float,
    window_seconds: float = PRODUCTION_TROUBLE_WINDOW_SECONDS,
) -> list[str]:
    """Event dates whose last production capture hit the network recently.

    Read off the resident's own status (``official_native_capture_by_date``):
    a family held on a transport failure, or a pass that failed on one,
    inside the window.  While production is losing fetches to the network,
    this lane's downloads are the one load that can be taken off it.
    """

    troubled: list[str] = []
    by_date = status.get("official_native_capture_by_date")
    if not isinstance(by_date, dict):
        return troubled
    for event_date, outcome in sorted(by_date.items()):
        if not isinstance(outcome, dict):
            continue
        try:
            completed = datetime.fromisoformat(
                str(outcome.get("completed_at_utc") or "").replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        if now_epoch - completed.timestamp() > window_seconds:
            continue
        held = outcome.get("families_held_on_earlier_run") or {}
        text = " ".join(
            [str(outcome.get("reason") or "")]
            + [str(value) for value in (held.values() if isinstance(held, dict) else [])]
        )
        if transport_failure(text) or "transport_failed" in text:
            troubled.append(str(event_date))
    return troubled


def transport_failure(stderr: str) -> bool:
    text = str(stderr or "")
    return any(marker in text for marker in TRANSPORT_FAILURE_MARKERS)


def _external_volume_root(path: Path) -> Path:
    """``path`` if it is mounted on another device than the repository.

    A cold root that is not mounted would otherwise be created on the internal
    disk by ``mkdir(parents=True)`` and fill it -- the one outcome the cold
    tier exists to prevent.
    """

    from bayes_basket_target.offloaded_paths import assert_present  # noqa: PLC0415

    anchor = path
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    assert_present(anchor, what="backfill raw root")
    if anchor == anchor.parent or os.stat(anchor).st_dev == os.stat(ROOT).st_dev:
        raise SystemExit(
            f"raw root {path} is not on an external volume (mounted?); refusing "
            "to stage raw fields on the internal disk"
        )
    return path


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


def prune_scratch_store(
    raw_dirs, packet_dirs, queue, *, raw_keep_hours: float,
) -> tuple[int, int]:
    """Raw objects by age; packets only once their rows are in (not queued)."""
    keep = set(queue)
    removed, freed = 0, 0
    for path in raw_dirs:
        r, f = _prune(path, keep_hours=raw_keep_hours)
        removed += r
        freed += f
    for path in packet_dirs:
        r, f = _prune(path, keep_hours=24.0, keep=keep)
        removed += r
        freed += f
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
    parser.add_argument(
        "--live-yield-rounds", type=float, default=1.0,
        help="stand aside before each ingest while any SSOT hosted sweep is "
             "overdue by more than this many of its own intervals; 0 disables",
    )
    parser.add_argument(
        "--live-yield-max-seconds", type=float, default=900.0,
        help="longest one stand-aside may last, so a lane that stays late for "
             "reasons of its own cannot stall this walk indefinitely",
    )
    parser.add_argument(
        "--pace-ratio", type=float, default=1.0,
        help="after a committed ingest, stay idle for this multiple of the time "
             "the ingest held the lock (1.0 keeps the lane's duty cycle at or "
             "under half); 0 disables",
    )
    parser.add_argument(
        "--pace-max-seconds", type=float, default=120.0,
        help="cap on one pacing pause",
    )
    parser.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH)
    parser.add_argument(
        "--raw-root", type=Path, default=None,
        help="put the raw GRIB store here instead of under --scratch; it must "
             "be on an external volume, and raw objects there are kept (the "
             "cold tier is an archive), not pruned by age",
    )
    parser.add_argument(
        "--coverable-only", action="store_true",
        help="capture only the stations each run can seal a local day for "
             "(needed for lead 3: D-3 18Z reaches +72h), and count a date done "
             "when all of those are present",
    )
    parser.add_argument(
        "--production-yield-max-seconds", type=float, default=900.0,
        help="before each capture, wait at most this long while a production "
             "NWP capture is running; 0 disables",
    )
    parser.add_argument(
        "--respect-production-network", action="store_true",
        help="do not start a capture while the resident's status shows a "
             "production capture lost to the network within the last hour",
    )
    parser.add_argument(
        "--network-backoff-seconds", type=float, default=300.0,
        help="after a capture fails on the network, pause this long and stop "
             "the invocation (the next one resumes from the raw store)",
    )
    # Reuse only ever reaches back one event date (about 40 minutes at the
    # measured pace); three hours kept ~5 GB of fields on a disk with 23 GiB
    # free.
    parser.add_argument("--raw-keep-hours", type=float, default=1.5)
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
    raw_root = (
        _external_volume_root(args.raw_root.expanduser())
        if args.raw_root is not None
        else scratch
    )
    raw_dirs = {p: raw_root / f"raw_{p}" for p in providers}
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

    thresholds: dict[tuple[str, int, int], int] = {}

    def done_at(packet: Packet) -> int:
        if not args.coverable_only or packet.provider != "ecmwf":
            return done_threshold
        key = (packet.event_date, packet.lead, packet.cycle)
        if key not in thresholds:
            from scripts.backfill_ecmwf_ifs_multistation_archive import (  # noqa: PLC0415
                coverable_station_count,
            )
            thresholds[key] = coverable_station_count(
                db, event_date=packet.event_date,
                lead_days=packet.lead, run_cycle_hour=packet.cycle,
            )
        return thresholds[key]

    # The queue: every well-named packet in the scratch store whose rows are
    # not yet in the SSOT.  Rebuilt from disk at start, so a killed run loses
    # nothing but the capture it was in the middle of.
    queue: dict[Path, Packet] = {}
    for provider in providers:
        for path in sorted(packet_dirs[provider].glob(f"{provider}_*.json")):
            packet = Packet.parse(provider, path)
            if packet is None or not packet.valid():
                continue
            if present(packet) >= done_at(packet):
                continue
            queue[path.resolve()] = packet

    t0 = time.monotonic()
    stats = {
        "captures_run": 0, "captures_skipped_present": 0,
        "captures_skipped_queued": 0, "captures_failed": 0,
        "ingests_committed": 0, "ingests_lock_busy": 0, "ingests_failed": 0,
        "rows_written": 0, "bytes_downloaded": 0,
        "live_yield_seconds": 0.0, "live_yields": 0, "live_yield_expired": 0,
        "pace_seconds": 0.0, "production_yield_seconds": 0.0,
    }

    def out_of_time() -> bool:
        return args.max_seconds is not None and time.monotonic() - t0 > args.max_seconds

    live_intervals: dict[str, float] = {}
    clocks_path: Path | None = None
    if args.live_yield_rounds > 0:
        live_intervals, clocks_path = _hosted_sweep_contract()

    def stand_aside() -> None:
        if clocks_path is None:
            return
        result = stand_aside_for_live_lanes(
            live_intervals,
            lambda: json.loads(clocks_path.read_text(encoding="utf-8")),
            rounds=args.live_yield_rounds,
            max_seconds=args.live_yield_max_seconds,
        )
        if result["ended"] in {"disabled", "not_needed"}:
            return
        stats["live_yields"] += 1
        stats["live_yield_seconds"] += result["waited_seconds"]
        late = [[n, round(r, 2)] for n, r in result["late"]]
        if result["ended"] == "cap_reached":
            stats["live_yield_expired"] += 1
        log({"kind": "live_yield", "ended": result["ended"],
             "waited_seconds": round(result["waited_seconds"], 1), "late": late})
        print(
            f"   stood aside {result['waited_seconds']:.0f}s for {len(late)} late "
            f"live SSOT lane(s) ({result['ended']})",
            flush=True,
        )

    def pace(held_seconds: float) -> None:
        stats["pace_seconds"] += pace_after_commit(
            held_seconds, ratio=args.pace_ratio, max_seconds=args.pace_max_seconds,
        )

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
            if args.coverable_only and packet.provider == "ecmwf":
                cmd.append("--coverable-only")
            stand_aside()
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
                pace(elapsed)
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
                if before >= done_at(packet):
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
                if args.coverable_only and provider == "ecmwf":
                    cmd.append("--coverable-only")
                if args.respect_production_network:
                    try:
                        status = json.loads(PRODUCTION_STATUS_PATH.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        status = {}
                    troubled = production_network_trouble(
                        status if isinstance(status, dict) else {},
                        now_epoch=time.time(),
                    )
                    if troubled:
                        record["state"] = "deferred_production_network_trouble"
                        record["troubled_event_dates"] = troubled
                        log(record)
                        print(f"   production captures are losing fetches to the network "
                              f"({', '.join(troubled)}); not starting a download", flush=True)
                        stopped_reason = "production_network_trouble"
                        break
                if args.production_yield_max_seconds > 0:
                    waited = stand_aside_for_production_captures(
                        max_seconds=args.production_yield_max_seconds,
                    )
                    if waited:
                        stats["production_yield_seconds"] += waited
                        log({"kind": "production_yield", "event_date": event_date,
                             "waited_seconds": waited})
                        print(f"   stood aside {waited:.0f}s for a production capture",
                              flush=True)
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
                    if transport_failure(stderr):
                        record["network_backoff_seconds"] = args.network_backoff_seconds
                        stopped_reason = "network_backoff"
                log(record)
                print(
                    f"{event_date} {provider:5s} l{lead} {cycle:02d}Z {record['state']:8s} "
                    f"{elapsed:6.0f}s {got/2**20:7.1f}MB rss{record['children_max_rss_mb']:.0f}MB",
                    flush=True,
                )
                if stopped_reason == "network_backoff":
                    print(f"   network failure: backing off {args.network_backoff_seconds:.0f}s "
                          "and stopping; the next run resumes from the raw store",
                          flush=True)
                    time.sleep(max(0.0, args.network_backoff_seconds))
                    break
                drain(args.ingest_lock_wait_seconds)
            else:
                continue
            break
        if stopped_reason in {
            "max_seconds", "network_backoff", "production_network_trouble",
        }:
            break
        removed, freed = prune_scratch_store(
            [] if args.raw_root is not None else raw_dirs.values(),
            packet_dirs.values(), queue,
            raw_keep_hours=args.raw_keep_hours,
        )
        event_days_done += 1
        current -= timedelta(days=1)
        print(
            f"-- {event_date} done; {len(queue)} packet(s) queued; pruned {removed} files "
            f"/ {freed/2**20:.0f}MB; scratch {_tree_bytes(scratch)/2**20:.0f}MB",
            flush=True,
        )

    if queue:
        drain(args.final_lock_wait_seconds)
    # Every exit prunes too.  The per-day prune above is skipped when the
    # clock runs out mid-day, and a `--max-seconds` loop always stops that
    # way: measured 2026-09-14, 10.8 GB of raw objects from committed
    # captures had piled up because no invocation ever reached it.
    prune_scratch_store(
        [] if args.raw_root is not None else raw_dirs.values(),
        packet_dirs.values(), queue,
        raw_keep_hours=args.raw_keep_hours,
    )
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
