"""Fail-closed resource bounds for the isolated target lane.

The target lane is a no-live deployment, but its offline packets and SQLite
state are still inputs to a long-running workflow.  Keep parsing bounded and
reject growth before it can consume the host's memory or disk.  The shared
resident database is deliberately rejected; this module is target-only.
"""

from __future__ import annotations

import json
import os
import resource as _resource
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any


class ResourceBoundError(ValueError):
    """Raised when a target input or target store exceeds its hard budget."""


MIB = 1024 * 1024
# Model-member and station fan-out receipts can be materially larger than the
# original 16 MiB envelope.  Keep the parser bounded but allow the 45-station
# historical/native packets to pass without a false resource blocker; node,
# RSS, database, WAL and free-space limits remain independent hard guards.
MAX_JSON_INPUT_BYTES = 64 * MIB
MAX_JSON_OUTPUT_BYTES = 16 * MIB
MAX_JSON_DEPTH = 32
# A canonical observation packet carries a bounded, fixed-width lineage
# record per observation.  The 16 MiB byte cap remains the primary bound, but
# the former 200k node cap rejected a legitimate 14k-row historical packet
# after observation_id/event_date/qc/evidence-role fields were added.  Keep a
# separate structural ceiling that is still comfortably bounded by the
# process-RSS and input/output byte limits.
MAX_JSON_NODES = 500_000
# 45 stations × all available native ensemble products × 28 settled
# station-days requires roughly 1.5-2 GiB in the append-only per-member
# schema.  Keep a hard local bound with ample headroom; WAL and RSS remain
# independently capped so a single transaction or process cannot approach
# this store-level ceiling.
# The append-only canonical migration chain intentionally retains replaced
# tables in the same isolated target database.  V10 crossed the historical
# 4 GiB ceiling while preserving every V9/V8/V7/V6 row, so keep the guard
# bounded but large enough for that audited history plus current facts.
#
# Raised to 12 GiB on 2026-08-12.  The 8 GiB ceiling was reached and stopped
# the whole chain: the store was 8.40 GiB, of which 2.86 GiB was ten
# generations of replaced canonical tables (v2..v13), retained by the
# migration chain and never pruned.  Those generations were exported and
# dropped -- v14/v15 remain for replay -- leaving 5.55 GiB, and the ceiling
# now sits far enough above that a rebuild of the largest table (the member
# archive doubles in place before its old copy is dropped) cannot trip it
# mid-migration, which is how the store crossed the old ceiling in the first
# place.  The guard is not the thing that grew; keep it a real bound.
#
# Raised to 16 GiB on 2026-08-17, for headroom rather than for a migration.
# The store reached 12.006 GiB -- six megabytes past the ceiling -- and every
# writer fail-closed: native ensemble ingress, resident P0 ingress and the
# post-cycle guard all stopped at 07:43Z, and the official capture, whose
# training commit shares this guard, died with it and left the served runs a
# day behind.  Nothing here was a leak; it is ordinary accumulation at roughly
# 250 MiB/day across the envelope, bridge-history and preselection tables.
# Four gigabytes is about a fortnight, which is deliberately short of
# comfortable: this ceiling exists to force a retention decision, not to be
# raised each time it is met.  The daemon now publishes remaining headroom as
# ``target_ssot_capacity`` and warns a gigabyte out, so the next approach is
# visible while there is still room to prune.
#
# Raised again to 18 GiB on 2026-08-20, and this time explicitly as a
# stopgap against this comment's own doctrine: the store crossed 16 GiB at
# 17.19 GB and every writer fail-closed mid-session.  The burn measured
# roughly 1.7 GiB/day since the last raise -- bridge history ~0.4, forward
# preselection plans ~0.13, envelopes and member lineage the rest -- so two
# gigabytes is about a day and a half, bought only to keep production alive
# while the retention decision this ceiling exists to force is actually
# taken.  The candidates are the two append-only JSON tables measured at
# 2.31 GB (basket_probability_bridge_history) and 2.75 GB
# (basket_p0_preselection_plan); retiring their cold rows needs a governance
# ruling because the append-only triggers are the constitution saying so,
# and a VACUUM to realise the space needs more free disk than the machine
# currently has.
MAX_TARGET_DB_BYTES = 18432 * MIB
# The native/P1 target SSOT is append-heavy (45 stations × member lineage).
# Permit a bounded 512 MiB WAL so normal no-live cycles do not self-block;
# SQLite checkpointing, database bytes, RSS and free-space guards remain hard.
MAX_TARGET_WAL_BYTES = 512 * MIB
MAX_TARGET_RESEARCH_RAW_BYTES = 1024 * MIB
MAX_TARGET_RESEARCH_RAW_FILES = 100_000
MIN_TARGET_FREE_BYTES = 512 * MIB
# A full 45-station target cycle may hold the independently bounded SSOT,
# native-member lineage and working decode buffers at once.  Keep the runtime
# hard stop aligned with the 4 GiB target-store budget; every input, WAL and
# free-space guard remains independently bounded.
MAX_TARGET_PROCESS_RSS_BYTES = 4096 * MIB
MAX_INPUT_FILES_PER_BATCH = 4096
MAX_WS_EVIDENCE_CACHE_SCOPES = 512
MAX_WS_EVIDENCE_CACHE_ROW_BYTES = 512 * 1024
# How many days of scopes the evidence cache keeps.
#
# The cache is a working set, not an archive: one row per live scope, upserted
# in place, and the market history it summarises lives in
# ``basket_official_ws_snapshot``.  Nothing ever aged out of it, so it filled
# with 20 event days of scopes -- 2026-07-27 onward -- and hit the 512 ceiling.
# At that point every new scope is refused, which fails the whole ingress
# closed: on 2026-08-19 the resident wrote no quote at all, and the only
# station-days with a book were the ones still early enough to be served by
# the forward pre-selection path.  The Asia-Pacific stations, whose event day
# had already begun, had none -- so P1 could not price a single one of them.
#
# The window is the subscription's own, plus a day either side: the WS
# catalogue carries yesterday through two days ahead, and a scope outside that
# has no reader.  Forty-five stations across five days is 225 rows against a
# 512 ceiling, which leaves the ceiling doing what it is for -- catching a
# runaway -- rather than throttling the steady state.
WS_EVIDENCE_CACHE_RETAIN_DAYS = 3


def _process_rss_bytes() -> int:
    """Return this process' conservative peak RSS in bytes.

    ``ru_maxrss`` is reported in bytes on macOS and KiB on Linux.  Using the
    peak rather than a potentially unavailable instantaneous probe is
    intentional: once a target process has crossed the budget it must fail
    closed instead of continuing to allocate more memory.
    """

    try:
        peak = int(_resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise ResourceBoundError("target_process_memory_probe_unavailable") from exc
    if peak < 0:
        raise ResourceBoundError("target_process_memory_probe_invalid")
    return peak if sys.platform == "darwin" else peak * 1024


def assert_process_memory_budget(
    *,
    projected_bytes: int = 0,
    limit: int = MAX_TARGET_PROCESS_RSS_BYTES,
) -> int:
    """Fail closed before/after a bounded operation can grow process RSS."""

    current = _process_rss_bytes()
    if current + max(0, int(projected_bytes)) > int(limit):
        raise ResourceBoundError("target_process_memory_budget_exceeded")
    return current


def _bounded_bytes(path: Path, *, limit: int) -> bytes:
    path = Path(path).expanduser().resolve()
    try:
        size = int(path.stat().st_size)
    except OSError as exc:
        raise ResourceBoundError("target_input_stat_failed") from exc
    if size > int(limit):
        raise ResourceBoundError("target_input_bytes_exceeded")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ResourceBoundError("target_input_read_failed") from exc
    if len(data) > int(limit):
        raise ResourceBoundError("target_input_bytes_exceeded")
    return data


def _check_json_shape(
    value: Any,
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
    max_nodes: int = MAX_JSON_NODES,
) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > int(max_nodes):
        raise ResourceBoundError("target_json_nodes_exceeded")
    if depth > MAX_JSON_DEPTH:
        raise ResourceBoundError("target_json_depth_exceeded")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ResourceBoundError("target_json_key_invalid")
            _check_json_shape(
                child, depth=depth + 1, nodes=nodes, max_nodes=max_nodes
            )
    elif isinstance(value, list):
        for child in value:
            _check_json_shape(
                child, depth=depth + 1, nodes=nodes, max_nodes=max_nodes
            )


def load_bounded_json(
    path: Path | str,
    *,
    limit: int = MAX_JSON_INPUT_BYTES,
    max_nodes: int = MAX_JSON_NODES,
) -> Any:
    """Load a JSON input only after size and structural limits pass."""

    try:
        raw = _bounded_bytes(Path(path), limit=limit)
        assert_process_memory_budget(projected_bytes=min(int(limit) * 4, 64 * MIB))
        value = json.loads(raw.decode("utf-8"))
    except ResourceBoundError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ResourceBoundError("target_json_invalid") from exc
    _check_json_shape(value, max_nodes=max_nodes)
    assert_process_memory_budget()
    return value


def bounded_json_bytes(
    value: Any,
    *,
    limit: int = MAX_JSON_OUTPUT_BYTES,
    max_nodes: int = MAX_JSON_NODES,
) -> bytes:
    """Serialize JSON and reject an oversized evidence/output packet."""

    try:
        raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ResourceBoundError("target_json_output_invalid") from exc
    if len(raw) > int(limit):
        raise ResourceBoundError("target_json_output_bytes_exceeded")
    _check_json_shape(value, max_nodes=max_nodes)
    assert_process_memory_budget(projected_bytes=len(raw))
    return raw


def write_bounded_json(
    path: Path | str,
    value: Any,
    *,
    limit: int = MAX_JSON_OUTPUT_BYTES,
    max_nodes: int = MAX_JSON_NODES,
) -> None:
    """Atomically write a bounded JSON artifact without retaining duplicates."""

    destination = Path(path).expanduser().resolve()
    raw = bounded_json_bytes(value, limit=limit, max_nodes=max_nodes)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_bounded_json_if_changed(
    path: Path | str,
    value: Any,
    *,
    limit: int = MAX_JSON_OUTPUT_BYTES,
    max_nodes: int = MAX_JSON_NODES,
) -> bool:
    """Atomically write JSON only when its canonical bytes changed.

    Resident producers call this on every bounded poll.  Avoiding an
    ``os.replace`` for identical evidence is part of the zero-write contract:
    unchanged provider data must not churn SSD blocks, mtimes, downstream
    watchers, or lineage.
    """

    destination = Path(path).expanduser().resolve()
    raw = bounded_json_bytes(value, limit=limit, max_nodes=max_nodes)
    if destination.is_file():
        try:
            if int(destination.stat().st_size) == len(raw) and destination.read_bytes() == raw:
                return False
        except OSError as exc:
            raise ResourceBoundError("target_existing_json_read_failed") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _database_path_from_connection(conn: sqlite3.Connection) -> Path | None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[2] or str(row[2]) == ":memory:":
        return None
    return Path(str(row[2])).expanduser().resolve()


def _filesystem_free_bytes(path: Path) -> int:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return int(shutil.disk_usage(candidate).free)


def assert_target_db_capacity(
    db_path: Path | str,
    *,
    projected_bytes: int = 0,
    min_free_bytes: int = MIN_TARGET_FREE_BYTES,
) -> Path:
    """Reject shared-live paths and target DB/WAL growth beyond hard limits."""

    candidate = Path(db_path).expanduser()
    if candidate.name == ":memory:":
        return candidate
    resolved = candidate.resolve()
    # Import lazily to avoid a module cycle with the database boundary.
    from .database import SHARED_LIVE_DB

    if resolved == SHARED_LIVE_DB:
        raise ResourceBoundError("target_resource_guard_shared_live_database")
    size = int(resolved.stat().st_size) if resolved.is_file() else 0
    wal = resolved.with_name(resolved.name + "-wal")
    shm = resolved.with_name(resolved.name + "-shm")
    wal_size = int(wal.stat().st_size) if wal.is_file() else 0
    shm_size = int(shm.stat().st_size) if shm.is_file() else 0
    if size + max(0, int(projected_bytes)) > MAX_TARGET_DB_BYTES:
        raise ResourceBoundError("target_database_bytes_exceeded")
    if wal_size > MAX_TARGET_WAL_BYTES:
        raise ResourceBoundError("target_database_wal_bytes_exceeded")
    if _filesystem_free_bytes(resolved.parent) < max(0, int(min_free_bytes)) + max(0, int(projected_bytes)):
        raise ResourceBoundError("target_database_min_free_space_guard")
    return resolved


def checkpoint_target_wal_once_if_oversized(
    conn: sqlite3.Connection,
    *,
    limit: int = MAX_TARGET_WAL_BYTES,
) -> dict[str, Any]:
    """Boundedly reclaim an oversized isolated-target WAL exactly once.

    This is deliberately a connection preflight rather than a path-level
    cleanup.  SQLite itself must confirm that no reader/writer prevents a
    truncating checkpoint; the WAL is never unlinked or bypassed.  Any busy,
    locked, active-transaction, or still-oversized result remains fail closed.
    """

    path = _database_path_from_connection(conn)
    if path is None:
        return {
            "state": "not_file_backed",
            "attempted": False,
            "wal_bytes_before": 0,
            "wal_bytes_after": 0,
        }
    from .database import SHARED_LIVE_DB

    if path == SHARED_LIVE_DB:
        raise ResourceBoundError("target_resource_guard_shared_live_database")
    wal = path.with_name(path.name + "-wal")
    before = int(wal.stat().st_size) if wal.is_file() else 0
    if before <= int(limit):
        return {
            "state": "within_budget",
            "attempted": False,
            "wal_bytes_before": before,
            "wal_bytes_after": before,
        }
    if conn.in_transaction:
        raise ResourceBoundError("target_database_wal_checkpoint_transaction_active")
    try:
        query_only = int(conn.execute("PRAGMA query_only").fetchone()[0] or 0)
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0] or "").strip().lower()
    except sqlite3.Error as exc:
        raise ResourceBoundError("target_database_wal_checkpoint_probe_failed") from exc
    if query_only != 0:
        raise ResourceBoundError("target_database_wal_bytes_exceeded")
    if journal_mode != "wal":
        raise ResourceBoundError("target_database_wal_checkpoint_mode_invalid")

    previous_busy_timeout = 0
    try:
        previous_busy_timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0] or 0)
        conn.execute("PRAGMA busy_timeout=0")
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error as exc:
        message = str(exc).lower()
        reason = (
            "target_database_wal_checkpoint_busy"
            if "busy" in message or "locked" in message
            else "target_database_wal_checkpoint_failed"
        )
        raise ResourceBoundError(reason) from exc
    finally:
        try:
            conn.execute(f"PRAGMA busy_timeout={max(0, previous_busy_timeout)}")
        except sqlite3.Error:
            pass

    if not row or len(row) < 3:
        raise ResourceBoundError("target_database_wal_checkpoint_result_invalid")
    busy, log_frames, checkpointed_frames = (int(row[0]), int(row[1]), int(row[2]))
    if busy != 0:
        raise ResourceBoundError("target_database_wal_checkpoint_busy")
    after = int(wal.stat().st_size) if wal.is_file() else 0
    if after > int(limit):
        raise ResourceBoundError("target_database_wal_bytes_exceeded_after_checkpoint")
    return {
        "state": "checkpointed",
        "attempted": True,
        "wal_bytes_before": before,
        "wal_bytes_after": after,
        "log_frames": log_frames,
        "checkpointed_frames": checkpointed_frames,
    }


def assert_target_raw_retention_budget(
    output_root: Path | str,
    *,
    bytes_needed: int = 0,
    limit: int = MAX_TARGET_RESEARCH_RAW_BYTES,
    min_free_bytes: int = MIN_TARGET_FREE_BYTES,
) -> int:
    """Reject aggregate raw growth or insufficient free space before writes."""

    root = Path(output_root).expanduser().resolve()
    retained = 0
    retained_files = 0
    retained_dirs = 0
    if root.is_dir():
        for current, directories, files in os.walk(
            root,
            followlinks=False,
        ):
            directories[:] = [
                name
                for name in directories
                if not (Path(current) / name).is_symlink()
            ]
            retained_dirs += len(directories)
            if retained_dirs > MAX_TARGET_RESEARCH_RAW_FILES:
                raise ResourceBoundError(
                    "target_raw_retention_entry_count_exceeded"
                )
            for name in files:
                path = Path(current) / name
                if path.is_symlink():
                    continue
                retained_files += 1
                if retained_files > MAX_TARGET_RESEARCH_RAW_FILES:
                    raise ResourceBoundError(
                        "target_raw_retention_file_count_exceeded"
                    )
                try:
                    retained += int(path.stat().st_size)
                except OSError as exc:
                    raise ResourceBoundError(
                        "target_raw_retention_stat_failed"
                    ) from exc
    if retained + max(0, int(bytes_needed)) > int(limit):
        raise ResourceBoundError("target_raw_aggregate_retention_budget_exceeded")
    if _filesystem_free_bytes(root) < max(0, int(min_free_bytes)) + max(0, int(bytes_needed)):
        raise ResourceBoundError("target_raw_min_free_space_guard")
    return retained


def inspect_target_resource_budget(
    db_path: Path | str,
    *,
    raw_root: Path | str | None = None,
) -> dict[str, Any]:
    """Return a read-only resource snapshot for the isolated target lane.

    This function deliberately does not clean, compact, or mutate anything.
    It gives readiness a single bounded view of the target database, WAL,
    research raw corpus, and filesystem headroom so a report cannot confuse
    target growth with the protected resident production database.
    """

    candidate = Path(db_path).expanduser()
    resolved = candidate.resolve() if candidate.name != ":memory:" else candidate
    violations: list[str] = []
    db_bytes = int(resolved.stat().st_size) if resolved.is_file() else 0
    wal = resolved.with_name(resolved.name + "-wal") if resolved.name != ":memory:" else resolved
    shm = resolved.with_name(resolved.name + "-shm") if resolved.name != ":memory:" else resolved
    wal_bytes = int(wal.stat().st_size) if wal.is_file() else 0
    shm_bytes = int(shm.stat().st_size) if shm.is_file() else 0
    if db_bytes > MAX_TARGET_DB_BYTES:
        violations.append("target_database_bytes_exceeded")
    if wal_bytes > MAX_TARGET_WAL_BYTES:
        violations.append("target_database_wal_bytes_exceeded")

    raw_resolved: Path | None = None
    raw_bytes = 0
    raw_file_count = 0
    if raw_root is not None:
        raw_resolved = Path(raw_root).expanduser().resolve()
        if raw_resolved.exists() and not raw_resolved.is_dir():
            violations.append("target_raw_root_not_directory")
        elif raw_resolved.is_dir():
            for root, _dirs, files in os.walk(raw_resolved, followlinks=False):
                for name in files:
                    path = Path(root) / name
                    if path.is_symlink():
                        continue
                    try:
                        raw_bytes += int(path.stat().st_size)
                        raw_file_count += 1
                    except OSError:
                        violations.append("target_raw_retention_stat_failed")
            if raw_bytes > MAX_TARGET_RESEARCH_RAW_BYTES:
                violations.append("target_aggregate_raw_bytes_exceeded")
            if raw_file_count > MAX_TARGET_RESEARCH_RAW_FILES:
                violations.append("target_raw_file_count_exceeded")

    free_bytes = _filesystem_free_bytes(resolved.parent if resolved.name != ":memory:" else Path.cwd())
    if free_bytes < MIN_TARGET_FREE_BYTES:
        violations.append("target_database_min_free_space_guard")
    try:
        process_rss_bytes = _process_rss_bytes()
        memory_probe_available = True
    except ResourceBoundError:
        process_rss_bytes = None
        memory_probe_available = False
        violations.append("target_process_memory_probe_unavailable")
    if process_rss_bytes is not None and process_rss_bytes > MAX_TARGET_PROCESS_RSS_BYTES:
        violations.append("target_process_memory_budget_exceeded")
    return {
        "state": "within_budget" if not violations else "over_budget",
        "ok": not violations,
        "violations": sorted(set(violations)),
        "database_path": str(resolved),
        "database_bytes": db_bytes,
        "wal_bytes": wal_bytes,
        "shm_bytes": shm_bytes,
        "raw_root": str(raw_resolved) if raw_resolved is not None else None,
        "raw_bytes": raw_bytes,
        "raw_file_count": raw_file_count,
        "free_bytes": free_bytes,
        "process_rss_bytes": process_rss_bytes,
        "memory_probe_available": memory_probe_available,
        "limits": {
            "max_database_bytes": MAX_TARGET_DB_BYTES,
            "max_wal_bytes": MAX_TARGET_WAL_BYTES,
            "max_research_raw_bytes": MAX_TARGET_RESEARCH_RAW_BYTES,
            "max_research_raw_files": MAX_TARGET_RESEARCH_RAW_FILES,
            "min_free_bytes": MIN_TARGET_FREE_BYTES,
            "max_process_rss_bytes": MAX_TARGET_PROCESS_RSS_BYTES,
        },
    }


def assert_connection_target_capacity(
    conn: sqlite3.Connection,
    *,
    recover_oversized_wal: bool = False,
) -> Path | None:
    """Apply target guards; WAL recovery requires an explicit writer context."""

    path = _database_path_from_connection(conn)
    if path is None:
        return None
    if recover_oversized_wal:
        checkpoint_target_wal_once_if_oversized(
            conn,
            limit=MAX_TARGET_WAL_BYTES,
        )
    resolved = assert_target_db_capacity(path)
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0] or 4096)
    max_pages = max(1, MAX_TARGET_DB_BYTES // page_size)
    conn.execute(f"PRAGMA max_page_count={max_pages}")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    return resolved


__all__ = [
    "MAX_INPUT_FILES_PER_BATCH",
    "MAX_JSON_DEPTH",
    "MAX_JSON_INPUT_BYTES",
    "MAX_JSON_NODES",
    "MAX_JSON_OUTPUT_BYTES",
    "MAX_TARGET_DB_BYTES",
    "MAX_TARGET_PROCESS_RSS_BYTES",
    "MAX_TARGET_RESEARCH_RAW_BYTES",
    "MAX_TARGET_RESEARCH_RAW_FILES",
    "MAX_TARGET_WAL_BYTES",
    "MIN_TARGET_FREE_BYTES",
    "MAX_WS_EVIDENCE_CACHE_ROW_BYTES",
    "MAX_WS_EVIDENCE_CACHE_SCOPES",
    "ResourceBoundError",
    "assert_process_memory_budget",
    "checkpoint_target_wal_once_if_oversized",
    "assert_connection_target_capacity",
    "assert_target_db_capacity",
    "assert_target_raw_retention_budget",
    "bounded_json_bytes",
    "inspect_target_resource_budget",
    "load_bounded_json",
    "write_bounded_json",
]
