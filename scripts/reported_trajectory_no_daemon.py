#!/usr/bin/env python3
"""User-domain resident daemon for country-routed raw-METAR trajectory NO."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import sys
import time
import math
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_runtime_network_env() -> None:
    """Make direct ``python3`` invocations use the same transport as launchd."""

    path = Path(os.environ.get("PM_SHARED_POLY_ENV_PATH") or ROOT / ".env")
    if not path.is_file():
        return
    # launchd binds the target execution boundary.  A shared .env file is a
    # transport/config source, never an authority that may flip the resident
    # back to live submission or disable the explicitly requested target
    # writer.  Preserve values already injected by the service domain.
    launchd_bound = {
        key: os.environ.get(key)
        for key in ("PM_TARGET_EXECUTION_MODE", "PM_TARGET_NO_LIVE", "PM_TARGET_PRODUCTION_WRITER")
        if os.environ.get(key) is not None
    }
    allowed_proxy = {
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
        "GLOBAL_AGENT_HTTP_PROXY",
    }
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not (key.startswith("PM_") or key in allowed_proxy):
            continue
        if key in launchd_bound:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


_load_runtime_network_env()

from reported_trajectory_market_registry import (
    commit_registry_rows,
    discovered_slugs,
    fetch_gamma_event,
    fetch_gamma_events_window,
    load_config,
    materialize_event_registry,
)
from reported_trajectory_no_engine import ReportedTrajectoryNoEngine
from reported_trajectory_no_schema import (
    apply_reported_trajectory_no_schema,
    reported_trajectory_no_schema_status,
)
from reported_trajectory_country_sources import fetch_country_routed_temperature_reports
from reported_trajectory_sources import (
    fetch_amsc_temperature_reports,
    utc_now,
)
from bayes_basket_target.database import DEFAULT_TARGET_DB
from bayes_basket_target.market_registry import (
    export_shared_live_registry,
    load_active_registry_rows,
)
from bayes_basket_target.settlement_time import settlement_day_window
from bayes_basket_target.production_worker import (
    DEFAULT_TARGET_ACTIVATION,
    DEFAULT_TARGET_MARKET_MANIFEST,
    DEFAULT_TARGET_NWP_RUNTIME_ROOT,
    TargetProductionWorker,
    open_target_writer_if_enabled,
)
from bayes_basket_target.resource_bounds import (
    assert_connection_target_capacity,
    load_bounded_json,
)
from bayes_basket_target.taf_rotation_state import (
    MAX_SELECTED_SCOPES as MAX_TAF_SCOPES_PER_POLL,
    build_catalog as build_taf_rotation_catalog,
    build_rotation_state as build_taf_rotation_state,
    load_rotation_state as load_taf_rotation_state,
    persist_rotation_state as persist_taf_rotation_state,
    scope_key as taf_rotation_scope_key,
    select_scopes as select_taf_rotation_scopes,
)
from scripts.capture_bayes_basket_target_awc_taf_revision import (
    capture_forward_awc_taf,
    refresh_forward_taf_inbox,
)
# The unique target worker owns the compile_production_cycle_from_ssot route;
# the resident daemon only schedules that worker and never reimplements it.


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_STATUS = ROOT / "data" / "reported_trajectory_no_status.json"
SCHEMA_VERSION = "reported_trajectory_no_daemon_v1"
SQLITE_IO_RECOVERY_BACKOFF_SEC = 5.0
TARGET_WAL_REOPEN_REASONS = (
    "target_database_wal_checkpoint_failed",
    "target_database_wal_checkpoint_busy",
    "target_database_wal_bytes_exceeded_after_checkpoint",
)
TARGET_TAF_POLL_INTERVAL_SEC = 60.0
TARGET_NWP_POLL_INTERVAL_SEC = 600.0
MAX_ARCHIVE_WAL_BYTES = 512 * 1024 * 1024
MIN_ARCHIVE_FREE_BYTES = 1024 * 1024 * 1024
_SLUG_DATE_RE = re.compile(r"-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$", re.I)


def is_recoverable_sqlite_io_error(exc: BaseException) -> bool:
    """Return whether a lane connection should be rebuilt after a transient I/O fault."""

    return isinstance(exc, sqlite3.OperationalError) and "disk i/o error" in str(exc).lower()


def target_result_has_recoverable_sqlite_io_error(
    result: Mapping[str, Any],
    *,
    forecast: bool = False,
) -> bool:
    """Recognize a serialized target-worker I/O fault without scanning history.

    The production worker normally catches the SQLite exception and returns a
    fail-closed receipt, so the executor future itself does not raise.  Only
    current-cycle reason fields are inspected; durable historical decisions
    embedded for audit must not trigger endless connection rebuilds.
    """

    paths = (
        ("resident_nwp_capture", "reason"),
        ("forward_taf_capture", "reason"),
    ) if forecast else (
        ("reason",),
        ("market_ingress", "reason"),
        ("post_fill_recompute", "reason"),
    )
    for path in paths:
        value: Any = result
        for key in path:
            if not isinstance(value, Mapping):
                value = None
                break
            value = value.get(key)
        reason = str(value or "").lower()
        if (
            "disk i/o error" in reason
            or any(marker in reason for marker in TARGET_WAL_REOPEN_REASONS)
        ):
            return True
    return False


def archive_storage_guard(db_path: Path) -> dict[str, Any]:
    """Fail closed before archive WAL or filesystem exhaustion."""

    database = db_path.expanduser().resolve()
    wal = Path(str(database) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    free_bytes = int(shutil.disk_usage(database.parent).free)
    violations: list[str] = []
    if wal_bytes > MAX_ARCHIVE_WAL_BYTES:
        violations.append("archive_wal_bytes_exceeded")
    if free_bytes < MIN_ARCHIVE_FREE_BYTES:
        violations.append("archive_free_bytes_below_minimum")
    return {
        "ok": not violations,
        "database": str(database),
        "wal_bytes": wal_bytes,
        "max_wal_bytes": MAX_ARCHIVE_WAL_BYTES,
        "free_bytes": free_bytes,
        "min_free_bytes": MIN_ARCHIVE_FREE_BYTES,
        "violations": violations,
        "state": "within_budget" if not violations else "blocked_fail_closed",
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
    last_error: OSError | None = None
    for attempt in range(4):
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_error = exc
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt < 3:
                time.sleep(0.05 * (attempt + 1))
    # Status is telemetry, not an execution authorization boundary.  A
    # transient disk/atomic-replace error must not take down the market/source
    # workers; the next heartbeat retries the write.
    print(f"[reported_trajectory_no] status_write_deferred:{type(last_error).__name__}:{last_error}", file=sys.stderr)


def run_target_forecast_refresh_cycle(
    worker: TargetProductionWorker,
) -> dict[str, Any]:
    """Refresh bounded forecast inboxes outside the decision critical path.

    This remains an internal task of the existing system-domain service; it
    is not a fourth daemon or a legacy model scanner.  Network latency may
    delay this refresh receipt, but can never block P1, settlement truth,
    labels, WS, or no-live decisions.
    """

    now_mono = time.monotonic()
    nwp_state: dict[str, Any] = {
        "state": "not_due",
        "calls_order_gateway": False,
        "places_orders": False,
    }
    previous_nwp = getattr(worker, "_last_official_nwp_state", None)
    if isinstance(previous_nwp, Mapping):
        nwp_state["last_attempt"] = dict(previous_nwp)
    next_nwp = float(
        getattr(worker, "_next_official_nwp_poll_mono", 0.0)
    )
    if bool(getattr(worker, "skip_nwp_refresh", False)):
        nwp_state = {
            "state": "skipped_exact_scope_reuse",
            "reason": "reuse_materialized_probability_requested",
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
    elif now_mono >= next_nwp:
        setattr(
            worker,
            "_next_official_nwp_poll_mono",
            now_mono + TARGET_NWP_POLL_INTERVAL_SEC,
        )
        try:
            refresh = getattr(worker, "refresh_resident_nwp", None)
            nwp_state = (
                refresh(as_of_utc=utc_now())
                if callable(refresh)
                else {
                    "state": "not_configured",
                    "reason": "resident_nwp_refresh_missing",
                    "calls_order_gateway": False,
                    "places_orders": False,
                }
            )
        except Exception as exc:
            nwp_state = {
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
                "calls_order_gateway": False,
                "places_orders": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            }
        setattr(worker, "_last_official_nwp_state", dict(nwp_state))
    next_poll = float(
        getattr(worker, "_next_forward_taf_poll_mono", 0.0)
    )
    capture_state: dict[str, Any] = {
        "state": "not_due",
        "calls_order_gateway": False,
        "places_orders": False,
    }
    previous_taf = getattr(worker, "_last_forward_taf_state", None)
    if isinstance(previous_taf, Mapping):
        capture_state["last_attempt"] = dict(previous_taf)
    if now_mono >= next_poll:
        setattr(
            worker,
            "_next_forward_taf_poll_mono",
            now_mono + TARGET_TAF_POLL_INTERVAL_SEC,
        )
        try:
            taf_scope_conn = (
                worker.commit_conn
                if getattr(worker, "commit_conn", None) is not None
                else getattr(worker, "conn", None)
            )
            if taf_scope_conn is None:
                capture_state = {
                    "state": "not_configured",
                    "calls_order_gateway": False,
                    "places_orders": False,
                }
            else:
                now = utc_now()
                registry_rows = load_active_registry_rows(
                    taf_scope_conn
                )
                catalog = build_taf_rotation_catalog(
                    registry_rows,
                    as_of_utc=now,
                    station_ids=tuple(
                        getattr(worker, "station_allowlist", ()) or ()
                    ),
                )
                rotation_path = Path(
                    getattr(
                        worker,
                        "taf_rotation_state_path",
                        worker.taf_inbox_path.with_name(
                            "taf_rotation_state.json"
                        ),
                    )
                )
                previous_rotation = load_taf_rotation_state(
                    rotation_path
                )
                selected, next_cursor, catalog_sha = (
                    select_taf_rotation_scopes(
                        catalog,
                        previous=previous_rotation,
                        limit=MAX_TAF_SCOPES_PER_POLL,
                    )
                )
                captures: list[dict[str, Any]] = []
                capture_failures: list[dict[str, str]] = []
                outcomes: list[dict[str, str]] = []
                for scope in selected:
                    station = scope["station_id"]
                    event_date = scope["event_date"]
                    settlement_timezone = scope[
                        "settlement_timezone"
                    ]
                    key = taf_rotation_scope_key(scope)
                    try:
                        captures.append(
                            capture_forward_awc_taf(
                                station_id=str(station),
                                event_date=str(event_date),
                                settlement_timezone=str(
                                    settlement_timezone
                                ),
                            )
                        )
                        outcomes.append({
                            "scope_key": key,
                            "state": "captured",
                            "reason": "",
                        })
                    except Exception as exc:
                        failure = {
                            "station_id": str(station),
                            "event_date": str(event_date),
                            "reason": f"{type(exc).__name__}:{exc}",
                        }
                        capture_failures.append(failure)
                        outcomes.append({
                            "scope_key": key,
                            "state": "failed_isolated",
                            "reason": failure["reason"],
                        })
                inbox = (
                    refresh_forward_taf_inbox(
                        captures=captures,
                        inbox_path=worker.taf_inbox_path,
                    )
                    if captures else None
                )
                rotation = build_taf_rotation_state(
                    catalog=catalog,
                    selected=selected,
                    next_cursor=next_cursor,
                    catalog_sha256=catalog_sha,
                    outcomes=outcomes,
                    previous=previous_rotation,
                    updated_at_utc=now,
                )
                rotation_wrote = persist_taf_rotation_state(
                    rotation_path,
                    rotation,
                )
                capture_state = {
                    "state": (
                        "captured_with_scope_failures"
                        if captures and capture_failures
                        else "captured"
                        if captures
                        else "blocked_fail_closed"
                        if capture_failures
                        else "no_active_registry_scope"
                    ),
                    "scope_source": "target_local_active_market_registry",
                    "catalog_scope_count": len(catalog),
                    "selected_scope_count": len(selected),
                    "selected_scope_keys": [
                        taf_rotation_scope_key(scope)
                        for scope in selected
                    ],
                    "capture_count": len(captures),
                    "failure_count": len(capture_failures),
                    "failures": capture_failures,
                    "rotation_state_path": str(rotation_path),
                    "rotation_state_wrote": rotation_wrote,
                    "rotation_content_sha256": str(
                        rotation.get("content_sha256") or ""
                    ),
                    "inbox_content_sha256": str(
                        (inbox or {}).get("content_sha256") or ""
                    ),
                    "calls_order_gateway": False,
                    "places_orders": False,
                }
        except Exception as exc:
            # TAF is optional incremental evidence. Its capture failure must
            # not suppress P0-M, canonical P1, or the disjoint hard-NO lane.
            capture_state = {
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
                "calls_order_gateway": False,
                "places_orders": False,
            }
        setattr(worker, "_last_forward_taf_state", dict(capture_state))
    return {
        "schema": "reported_trajectory_forecast_prefetch_v1",
        "resident_nwp_capture": nwp_state,
        "forward_taf_capture": capture_state,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def resident_nwp_in_flight_receipt(
    worker: TargetProductionWorker,
    *,
    scheduled_at_utc: str,
) -> dict[str, Any]:
    """Publish a real persisted rotation while bounded network work is active.

    A launchd restart must not erase scheduler admission evidence until a
    potentially slow provider request returns.  The rotation was validated
    when ``TargetProductionWorker`` loaded it; this receipt never claims that
    the new capture completed.
    """

    rotation = getattr(worker, "_nwp_rotation_state", None)
    configured = list(getattr(worker, "station_allowlist", ()) or ())
    if not isinstance(rotation, Mapping):
        return {
            "state": "in_flight_no_prior_rotation",
            "scheduled_at_utc": scheduled_at_utc,
            "station_scope_mode": "canonical_allowlist",
            "configured_station_ids": configured,
            "discovered_station_ids": [],
            "scope_count": 0,
            "selected_scope_count": 0,
            "successful_scope_count": 0,
            "candidate_prefetch_attempt_count": 0,
            "candidate_prefetch_scope_limit": 1,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
    catalog_keys = [
        str(value)
        for value in rotation.get("catalog_scope_keys") or ()
    ]
    discovered = sorted({
        value.split("|", 1)[0].strip().upper()
        for value in catalog_keys
        if "|" in value and value.split("|", 1)[0].strip()
    })
    outcomes = [
        value
        for value in rotation.get("latest_outcomes") or ()
        if isinstance(value, Mapping)
    ]
    candidate_attempts = min(
        1,
        sum(bool(value.get("candidate_only")) for value in outcomes),
    )
    successful = sum(
        str(value.get("state") or "").upper()
        not in {"", "BLOCKED_FAIL_CLOSED"}
        for value in outcomes
    )
    return {
        "state": "in_flight_previous_rotation",
        "scheduled_at_utc": scheduled_at_utc,
        "completion_claimed": False,
        "station_scope_mode": "canonical_allowlist",
        "configured_station_ids": configured,
        "discovered_station_ids": discovered,
        "scope_count": int(rotation.get("catalog_scope_count") or 0),
        "scope_catalog_sha256": str(
            rotation.get("catalog_sha256") or ""
        ),
        "selected_scope_count": len(
            rotation.get("latest_selected_scope_keys") or ()
        ),
        "successful_scope_count": successful,
        "candidate_prefetch_attempt_count": candidate_attempts,
        "candidate_prefetch_scope_limit": 1,
        "rotation_state": dict(rotation),
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def run_target_production_cycle(
    worker: TargetProductionWorker,
) -> dict[str, Any]:
    """Consume only durable inbox/SSOT evidence and compile no-live output."""

    result = worker.run()
    connection = getattr(worker, "commit_conn", None)
    if connection is None:
        return result
    try:
        guard = assert_connection_target_capacity(
            connection,
            recover_oversized_wal=True,
        )
    except Exception as exc:
        return {
            **result,
            "ok": False,
            "state": "blocked_fail_closed",
            "reason": f"{type(exc).__name__}:{exc}",
            "post_cycle_resource_guard": {
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
            },
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
    return {
        **result,
        "post_cycle_resource_guard": {
            "state": "PASS",
            "database": None if guard is None else str(guard),
        },
    }


def _open(path: Path, *, ensure_schema: bool = True) -> sqlite3.Connection:
    """Open an archive connection.

    ``ensure_schema=True`` is retained for isolated tools/tests that create a
    fresh database. The resident production path uses ``_open_runtime`` below:
    schema installation belongs to the controlled cutover, never to service
    bootstrap. This is important because DDL against a shared WAL database can
    turn a harmless launchd restart into ``Bootstrap failed: 5``.
    """

    conn = sqlite3.connect(path, timeout=60.0, isolation_level=None, check_same_thread=False)
    # Copy-follow consumes named columns from plan/order rows.  Keep the
    # daemon's shared connection row shape identical to the standalone
    # copy-follow tests and workers.
    conn.row_factory = sqlite3.Row
    # Set the busy handler before any pragma that might touch the database.
    conn.execute("PRAGMA busy_timeout=60000")
    if ensure_schema:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        apply_reported_trajectory_no_schema(conn)
    else:
        # The runtime already uses the cutover-installed journal mode. Do not
        # change it during bootstrap; only read the schema catalog.
        conn.execute("PRAGMA synchronous=FULL")
        status = reported_trajectory_no_schema_status(conn)
        if not status["ok"]:
            missing = ",".join(str(item) for item in status["missing_tables"])
            conn.close()
            raise sqlite3.OperationalError(
                f"reported_trajectory_no_schema_incomplete:{missing}"
            )
    return conn


def _open_runtime(path: Path) -> sqlite3.Connection:
    """Open a production connection without schema DDL or WAL reconfiguration."""

    return _open(path, ensure_schema=False)


def refresh_registry(conn: sqlite3.Connection, config: dict[str, Any]) -> dict[str, Any]:
    policy = config["poll_policy"]
    today = datetime.now(timezone.utc).date() - timedelta(days=1)
    discovered = discovered_slugs(
        conn,
        today=today,
        horizon_days=int(policy["market_date_horizon_days"]) + 1,
    )
    fetched: list[tuple[str, str, dict[str, Any]]] = []
    errors: list[str] = []
    discovered_by_slug = {slug: event_date for slug, event_date in discovered}

    def event_date_for_slug(slug: str, event: Mapping[str, Any]) -> str:
        known = str(discovered_by_slug.get(slug) or "").strip()[:10]
        if known:
            return known
        match = _SLUG_DATE_RE.search(slug)
        if match is not None:
            try:
                return datetime.strptime(
                    f"{match.group('month')} {match.group('day')} {match.group('year')}",
                    "%B %d %Y",
                ).date().isoformat()
            except ValueError:
                pass
        return str(event.get("endDate") or "").strip()[:10]
    end = today + timedelta(days=int(policy["market_date_horizon_days"]) + 1)
    bulk_failed = False
    try:
        try:
            gamma_timeout = float(os.environ.get("PM_GAMMA_TIMEOUT_SEC", "8"))
        except (TypeError, ValueError):
            gamma_timeout = 8.0
        try:
            configured_pages = int(
                os.environ.get(
                    "PM_REPORTED_TRAJECTORY_GAMMA_MAX_PAGES",
                    str(policy.get("market_registry_gamma_max_pages", 20)),
                )
            )
        except (TypeError, ValueError):
            configured_pages = 20
        official_events = fetch_gamma_events_window(
            start_date=today,
            end_date=end,
            timeout=max(1.0, gamma_timeout),
            # Discovery may be empty or stale on a date rollover.  Scan a
            # bounded but sufficiently wide window so newly published cities
            # cannot be hidden behind the first Gamma page.
            max_pages=min(
                4,
                max(1, configured_pages, math.ceil(len(discovered) / 100)),
            ),
        )
    except Exception as exc:
        official_events = []
        bulk_failed = True
        errors.append(f"gamma_bulk:{type(exc).__name__}")

    # Gamma pagination is best-effort.  If it failed completely, recover the
    # already-known slugs individually.  If it partially succeeded, recover
    # only the missing known slugs.  This keeps a single bad page from hiding
    # the new/previously discovered city while leaving the old registry intact
    # when all transports are unavailable.
    official_by_slug = {
        str(event.get("slug") or "").strip(): event
        for event in official_events
        if str(event.get("slug") or "").strip()
    }
    missing_slugs = [slug for slug, _event_date in discovered if slug not in official_by_slug]
    if official_events and missing_slugs or bulk_failed and discovered:
        fallback_targets = missing_slugs if official_events else [slug for slug, _event_date in discovered]

        def fetch_known(item: tuple[str, str]) -> tuple[str, str, dict[str, Any] | None, str | None]:
            slug, known_date = item
            try:
                event = fetch_gamma_event(slug, timeout=max(1.0, gamma_timeout))
                return slug, known_date, event, None
            except Exception as exc:
                return slug, known_date, None, f"{slug}:gamma_slug:{type(exc).__name__}"

        targets = [(slug, discovered_by_slug[slug]) for slug in fallback_targets]
        with ThreadPoolExecutor(max_workers=max(1, min(12, len(targets)))) as fallback_executor:
            fallback_results = list(fallback_executor.map(fetch_known, targets))
        for slug, _known_date, event, error in fallback_results:
            if error:
                errors.append(error)
            elif event is not None:
                official_by_slug[slug] = event

    official_events = [official_by_slug[slug] for slug in sorted(official_by_slug)]
    if not official_events:
        raise RuntimeError(
            "market_registry_official_fetch_empty"
            + (f":{','.join(errors)}" if errors else "")
        )
    for event in official_events:
        slug = str(event.get("slug") or "")
        event_date = event_date_for_slug(slug, event)
        if event_date:
            fetched.append((slug, event_date, event))
    rows: list[dict[str, Any]] = []
    for _slug, event_date, event in fetched:
        try:
            rows.extend(materialize_event_registry(event, event_date=event_date, config=config))
        except Exception as exc:
            errors.append(f"{_slug}:materialize:{type(exc).__name__}")
    if not rows or not any(row.get("source_eligible") for row in rows):
        raise RuntimeError("market_registry_materialization_empty_or_ineligible")
    refreshed_at = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS weather_market_discovery (
                slug TEXT NOT NULL,
                event_date TEXT NOT NULL,
                title TEXT,
                market_open_at TEXT,
                first_seen_at TEXT NOT NULL,
                first_tokenized_at TEXT,
                token_count INTEGER NOT NULL DEFAULT 0,
                status TEXT,
                details_json TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (slug,event_date)
            )"""
        )
        committed = commit_registry_rows(conn, rows, refreshed_at=refreshed_at)
        for slug, event_date, event in fetched:
            market_rows = [row for row in event.get("markets") or [] if isinstance(row, dict)]
            token_count = sum(
                bool(str(row.get("conditionId") or "").strip())
                and bool(row.get("clobTokenIds"))
                for row in market_rows
            )
            conn.execute(
                """INSERT INTO weather_market_discovery(
                    slug,event_date,title,market_open_at,first_seen_at,first_tokenized_at,
                    token_count,status,details_json,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(slug,event_date) DO UPDATE SET
                    title=COALESCE(NULLIF(excluded.title,''),weather_market_discovery.title),
                    market_open_at=COALESCE(excluded.market_open_at,weather_market_discovery.market_open_at),
                    first_tokenized_at=CASE WHEN excluded.token_count>0
                        THEN COALESCE(weather_market_discovery.first_tokenized_at,excluded.first_tokenized_at)
                        ELSE weather_market_discovery.first_tokenized_at END,
                    token_count=MAX(COALESCE(weather_market_discovery.token_count,0),excluded.token_count),
                    status=excluded.status,details_json=excluded.details_json,updated_at=excluded.updated_at
                  WHERE weather_market_discovery.title IS NOT excluded.title
                     OR weather_market_discovery.market_open_at
                        IS NOT excluded.market_open_at
                     OR COALESCE(weather_market_discovery.token_count,0)
                        IS NOT excluded.token_count
                     OR weather_market_discovery.status IS NOT excluded.status
                     OR weather_market_discovery.details_json
                        IS NOT excluded.details_json""",
                (
                    slug,
                    event_date,
                    str(event.get("title") or slug),
                    str(event.get("startDate") or event.get("createdAt") or ""),
                    refreshed_at,
                    refreshed_at if token_count else None,
                    token_count,
                    "tokenized" if token_count else "missing_tokens",
                    json.dumps({"source": "reported_trajectory_no_registry_refresh", "active": event.get("active"), "closed": event.get("closed")}, sort_keys=True),
                    refreshed_at,
                ),
            )
        if "awc" not in config.get("production_source_allowlist", []):
            # AWC rows are retained as historical provenance, but when the
            # lane is disabled they must not remain production-eligible.
            conn.execute(
                """UPDATE reported_trajectory_market_registry
                   SET source_eligible=0,
                       source_block_reason=CASE
                         WHEN COALESCE(source_block_reason,'')='' THEN 'awc_production_disabled'
                         WHEN instr(','||source_block_reason||',', ',awc_production_disabled,')=0
                           THEN source_block_reason||',awc_production_disabled'
                         ELSE source_block_reason
                       END
                   WHERE source_lane='awc' AND source_eligible=1"""
            )
        markets = {(row["slug"], row["event_date"]) for row in rows}
        eligible = {
            (row["slug"], row["event_date"])
            for row in rows
            if row.get("source_eligible")
        }
        semantic_sha256 = hashlib.sha256(
            json.dumps(
                sorted(
                    (
                        row["slug"],
                        row["event_date"],
                        row["outcome"],
                        row["rules_sha256"],
                        row.get("yes_token") or "",
                        row.get("no_token") or "",
                        bool(row.get("market_active")),
                        bool(row.get("market_accepting_orders")),
                        bool(row.get("source_eligible")),
                    )
                    for row in rows
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        conn.execute(
            """INSERT INTO reported_trajectory_market_registry_refresh_state(
                 singleton,last_successful_refresh_at,semantic_sha256,
                 official_market_count,eligible_market_count,registry_row_count
               ) VALUES(1,?,?,?,?,?)
               ON CONFLICT(singleton) DO UPDATE SET
                 last_successful_refresh_at=excluded.last_successful_refresh_at,
                 semantic_sha256=excluded.semantic_sha256,
                 official_market_count=excluded.official_market_count,
                 eligible_market_count=excluded.eligible_market_count,
                 registry_row_count=excluded.registry_row_count""",
            (
                refreshed_at,
                semantic_sha256,
                len(markets),
                len(eligible),
                len(rows),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "refreshed_at": refreshed_at,
        "discovered_market_count": len(discovered),
        "official_market_count": len(markets),
        "eligible_market_count": len(eligible),
        "registry_row_count": len(rows),
        "changed_registry_row_count": committed,
        "semantic_sha256": semantic_sha256,
        "amsc_market_count": len({(row["slug"], row["event_date"]) for row in rows if row.get("source_eligible") and row.get("source_lane") == "amsc"}),
        "awc_market_count": len({(row["slug"], row["event_date"]) for row in rows if row.get("source_eligible") and row.get("source_lane") == "awc"}),
        "blocked_market_count": len(markets - eligible),
        "errors": errors,
    }


def current_registry_summary(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT MAX(refreshed_at),
                  COUNT(DISTINCT slug||'|'||event_date),
                  COUNT(DISTINCT CASE WHEN source_eligible=1 THEN slug||'|'||event_date END),
                  COUNT(DISTINCT CASE WHEN source_eligible=1 AND source_lane='amsc' THEN slug||'|'||event_date END),
                  COUNT(DISTINCT CASE WHEN source_eligible=1 AND source_lane='awc' THEN slug||'|'||event_date END)
           FROM reported_trajectory_market_registry"""
    ).fetchone()
    if row is None or not row[0] or int(row[1] or 0) <= 0:
        return None
    refresh_row = None
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='reported_trajectory_market_registry_refresh_state'"
    ).fetchone():
        refresh_row = conn.execute(
            "SELECT last_successful_refresh_at,semantic_sha256 "
            "FROM reported_trajectory_market_registry_refresh_state "
            "WHERE singleton=1"
        ).fetchone()
    refreshed_at = str(refresh_row[0] if refresh_row else row[0])
    try:
        age_sec = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(refreshed_at.replace("Z", "+00:00"))
        ).total_seconds()
    except ValueError:
        return None
    return {
        "refreshed_at": refreshed_at,
        "registry_age_sec": round(age_sec, 3),
        "official_market_count": int(row[1] or 0),
        "eligible_market_count": int(row[2] or 0),
        "amsc_market_count": int(row[3] or 0),
        "awc_market_count": int(row[4] or 0),
        "semantic_sha256": str(refresh_row[1]) if refresh_row else "",
        "source": (
            "automatic_registry_refresh_state"
            if refresh_row
            else "existing_registry_startup_snapshot"
        ),
    }


def active_stations(conn: sqlite3.Connection, lane: str) -> list[str]:
    now = datetime.now(timezone.utc)
    dates = ((now - timedelta(days=1)).date().isoformat(), (now + timedelta(days=2)).date().isoformat())
    return [
        str(row[0])
        for row in conn.execute(
            """SELECT DISTINCT station_id FROM reported_trajectory_market_registry
               WHERE source_lane=? AND source_eligible=1 AND market_active=1
                 AND market_accepting_orders=1 AND event_date BETWEEN ? AND ?
                 AND COALESCE(station_id,'')<>'' ORDER BY station_id""",
            (lane, *dates),
        )
    ]


def awc_settlement_lookback_hours(
    conn: sqlite3.Connection,
    station_ids: list[str],
    *,
    now_utc: datetime | None = None,
    minimum_hours: float = 2.0,
    margin_hours: float = 1.0,
    maximum_hours: float = 30.0,
) -> float:
    """Cover the active market day without allowing an unbounded AWC query.

    Future market days do not widen the request.  For a market day that has
    started, the request reaches back to its registry-bound settlement-day
    start plus one release margin.  The official AWC response remains bounded
    independently by ``AWC_MAX_RESPONSE_BYTES``.
    """

    stations = sorted({str(value or "").strip().upper() for value in station_ids if str(value or "").strip()})
    minimum = max(2.0, float(minimum_hours))
    maximum = max(minimum, min(48.0, float(maximum_hours)))
    margin = max(0.0, min(2.0, float(margin_hours)))
    if not stations:
        return minimum
    placeholders = ",".join("?" for _ in stations)
    rows = conn.execute(
        f"""SELECT DISTINCT station_id,event_date,settlement_timezone
              FROM reported_trajectory_market_registry
             WHERE source_lane='awc' AND source_eligible=1
               AND market_active=1 AND market_accepting_orders=1
               AND station_id IN ({placeholders})
             ORDER BY station_id,event_date""",
        stations,
    ).fetchall()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    required = minimum
    for row in rows:
        window = settlement_day_window(str(row[1]), str(row[2] or ""))
        start = datetime.fromisoformat(window["target_day_start_utc"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(window["target_day_end_utc"].replace("Z", "+00:00"))
        if now < start or now > end + timedelta(hours=margin):
            continue
        elapsed_hours = (now - start).total_seconds() / 3600.0
        required = max(required, math.ceil(elapsed_hours + margin))
    return float(min(maximum, required))


def amsc_poll_interval(config: dict[str, Any], now: datetime) -> float:
    policy = config["poll_policy"]
    second_in_halfhour = (now.minute % 30) * 60 + now.second + now.microsecond / 1_000_000
    release = float(policy["amsc_regular_release_delay_sec"])
    burst_end = release + float(policy["amsc_burst_window_sec"])
    if release <= second_in_halfhour <= burst_end:
        return float(policy["amsc_burst_retry_sec"])
    seconds_to_release = (
        release - second_in_halfhour
        if second_in_halfhour < release
        else 1800.0 + release - second_in_halfhour
    )
    return max(0.05, min(float(policy["amsc_background_poll_sec"]), seconds_to_release))


def awc_poll_interval(config: dict[str, Any], now: datetime) -> float:
    """Burst at :00/:30 while network calls remain provider-rate-gated."""

    policy = config.get("poll_policy", {})
    phase = (now.minute % 30) * 60 + now.second + now.microsecond / 1_000_000
    burst_window = float(policy.get("awc_release_burst_window_sec", 90.0))
    if phase <= burst_window:
        return max(0.1, float(policy.get("awc_release_burst_retry_sec", 0.25)))
    return max(0.5, float(policy.get("awc_background_poll_sec", 2.0)))


def candidate_prewarm_interval(config: dict[str, Any], now: datetime) -> float:
    policy = config.get("prewarm_policy") or {}
    if policy.get("enabled") is not True:
        return 3600.0
    phase = (now.minute % 30) * 60 + now.second + now.microsecond / 1_000_000
    before = float(policy.get("boundary_before_sec", 60.0))
    after = float(policy.get("boundary_after_sec", 120.0))
    if phase >= 1800.0 - before or phase <= after:
        return max(0.25, float(policy.get("boundary_interval_sec", 1.0)))
    return max(1.0, float(policy.get("background_interval_sec", 15.0)))


def run_once(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    engine: ReportedTrajectoryNoEngine,
    lane: str,
    *,
    stations_override: list[str] | None = None,
) -> dict[str, Any]:
    stations = list(stations_override) if stations_override is not None else active_stations(conn, lane)
    if not stations:
        return {"ok": True, "source_lane": lane, "state": "no_active_stations"}
    request_started_at = utc_now()
    if lane not in set(config.get("production_source_allowlist") or []):
        raise ValueError("production_source_lane_not_allowlisted")
    if lane == "amsc":
        poll = fetch_amsc_temperature_reports(
            stations,
            count=1,
            timeout=12.0,
            max_workers=int(config["poll_policy"]["amsc_max_concurrency"]),
        )
    else:
        batch_results: list[dict[str, Any]] = []

        def commit_provider_batch(batch: dict[str, Any]) -> None:
            completed_at = utc_now()
            result = engine.process_source_poll(
                batch,
                request_started_at=request_started_at,
                response_completed_at=completed_at,
            )
            result["provider_transport"] = batch.get("provider_transport")
            result["provider_completed_after_router_start_ms"] = batch.get(
                "provider_completed_after_router_start_ms"
            )
            batch_results.append(result)

        policy = config.get("poll_policy") or {}
        awc_hours = awc_settlement_lookback_hours(
            conn,
            stations,
            minimum_hours=float(policy.get("awc_settlement_lookback_min_hours", 2.0)),
            margin_hours=float(policy.get("awc_settlement_lookback_margin_hours", 1.0)),
            maximum_hours=float(policy.get("awc_settlement_lookback_max_hours", 30.0)),
        )
        poll = fetch_country_routed_temperature_reports(
            stations,
            timeout=8.0,
            awc_hours=awc_hours,
            on_provider_batch=commit_provider_batch,
        )
        return {
            "schema": SCHEMA_VERSION,
            "ok": not poll.get("station_errors"),
            "source_lane": "awc",
            "transport": poll.get("transport"),
            "router_duration_ms": poll.get("router_duration_ms"),
            "provider_states": poll.get("provider_states") or [],
            "fallback_reasons": poll.get("fallback_reasons") or {},
            "source_deferred": bool(poll.get("source_deferred")),
            "awc_requested_hours": awc_hours,
            "provider_batch_count": len(batch_results),
            "new_observation_count": sum(int(item.get("new_observation_count") or 0) for item in batch_results),
            "decision_results": [
                decision for item in batch_results for decision in item.get("decision_results") or []
            ],
            "submit_results": [
                submit for item in batch_results for submit in item.get("submit_results") or []
            ],
            "batch_results": batch_results,
            "request_started_at": request_started_at,
            "response_completed_at": utc_now(),
        }
    response_completed_at = utc_now()
    result = engine.process_source_poll(
        poll,
        request_started_at=request_started_at,
        response_completed_at=response_completed_at,
    )
    result["request_started_at"] = request_started_at
    result["response_completed_at"] = response_completed_at
    return result


def run_once_isolated(db_path: Path, config: dict[str, Any], lane: str) -> dict[str, Any]:
    """Give each source lane its own connection/engine and failure boundary."""

    conn = _open_runtime(db_path)
    try:
        return run_once(conn, config, ReportedTrajectoryNoEngine(conn, config=config), lane)
    finally:
        conn.close()


class LaneWorker:
    """One durable SQLite connection and engine per disjoint source lane."""

    def __init__(self, db_path: Path, config: dict[str, Any], lane: str) -> None:
        self.db_path = db_path
        self.conn = _open_runtime(db_path)
        self.config = config
        self.lane = lane
        self.engine = ReportedTrajectoryNoEngine(self.conn, config=config)
        self.stations: list[str] = []
        self.next_station_refresh = 0.0

    def poll(self) -> dict[str, Any]:
        now = time.monotonic()
        if now >= self.next_station_refresh or not self.stations:
            self.stations = active_stations(self.conn, self.lane)
            self.next_station_refresh = now + 60.0
        return run_once(
            self.conn, self.config, self.engine, self.lane,
            stations_override=self.stations,
        )

    def close(self) -> None:
        self.conn.close()

    def reopen(self) -> None:
        """Rebuild this lane's connection after a recoverable SQLite I/O error."""

        self.conn.close()
        self.conn = _open_runtime(self.db_path)
        self.engine = ReportedTrajectoryNoEngine(self.conn, config=self.config)
        self.stations = []
        self.next_station_refresh = 0.0


class CandidatePrewarmWorker:
    """Dedicated read-only connection so prewarming never delays source polls."""

    def __init__(self, db_path: Path, config: dict[str, Any], lane: str) -> None:
        self.conn = _open_runtime(db_path)
        self.engine = ReportedTrajectoryNoEngine(self.conn, config=config)
        self.lane = lane

    def run(self) -> dict[str, Any]:
        return self.engine.prewarm_next_candidates(source_lane=self.lane)

    def close(self) -> None:
        self.conn.close()


class RegistryRefreshWorker:
    """Refresh canonical metadata and independently publish target registry.

    Registry publication must not wait for a model release or an NWP capture
    cadence.  This worker only writes the bounded, atomically replaced JSON
    handoff; the sole target production worker remains the only target-SSOT
    writer and ingests the handoff on its next cycle.
    """

    def __init__(
        self,
        db_path: Path,
        config: dict[str, Any],
        *,
        target_registry_path: Path | None = None,
    ) -> None:
        self.db_path = db_path
        self.config = config
        self.conn = _open_runtime(db_path)
        self.target_registry_path = (
            Path(target_registry_path).expanduser().resolve()
            if target_registry_path is not None
            else (
                DEFAULT_TARGET_NWP_RUNTIME_ROOT
                / "market_registry_forward.json"
            ).resolve()
        )

    def run(self) -> dict[str, Any]:
        refreshed = refresh_registry(self.conn, self.config)
        exported_at = utc_now()
        target_export = export_shared_live_registry(
            source_db=self.db_path,
            output_path=self.target_registry_path,
            as_of_utc=exported_at,
            include_not_started_for_p0=True,
            current_catalog_only=True,
        )
        return {
            **refreshed,
            "target_registry_export": target_export,
            "target_registry_export_decoupled_from_nwp": True,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
        }

    def close(self) -> None:
        self.conn.close()

    def reopen(self) -> None:
        self.conn.close()
        self.conn = _open_runtime(self.db_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--once", choices=("registry", "amsc", "awc", "all", "production"), default="")
    args = parser.parse_args()
    config = load_config()
    conn = _open_runtime(args.db)
    engine = ReportedTrajectoryNoEngine(conn, config=config)
    state: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "pid": os.getpid(),
        "started_at": utc_now(),
        "strategy_id": "reported_trajectory_no_v1",
        "production_source_allowlist": list(config.get("production_source_allowlist") or []),
        "maximum_buy_price": float(config["order_policy"]["maximum_buy_price"]),
        "capital_policy": {
            "unified_pool": True,
            "deploy_all_available": False,
            "maximum_order_notional_usdc": float(config["order_policy"]["maximum_order_notional_usdc"]),
        },
        "exit_policy": {"side": "SELL", "limit_price": 0.99, "order_type": "GTC"},
        "taf_requested": True,
        "bayes_consumed": True,
        "hard_no_bayes_consumed": False,
        "weak_evidence_taf_capture_isolated": True,
        "weak_evidence_capture_activation_independent": True,
        "basket_consumes_materialized_probability": True,
        "yes_orders_allowed": False,
        "basket_strategy_id": "weather_basket_plus_hard_no_v2",
        "basket_yes_orders_allowed": False,
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "source_results": {},
    }
    try:
        existing_registry = current_registry_summary(conn)
        # Startup never performs network I/O synchronously.  The resident
        # scheduler below refreshes Gamma in a dedicated bounded worker while
        # all hot-path consumers continue reading the last atomic registry
        # generation.
        registry = (
            refresh_registry(conn, config)
            if args.once == "registry"
            else existing_registry or {
                "state": "materialized_registry_missing_fail_closed",
                "official_market_count": 0,
                "eligible_market_count": 0,
            }
        )
        state["registry"] = registry
        if args.once:
            if args.once == "production":
                commit_error = None
                try:
                    commit_conn = open_target_writer_if_enabled(
                        DEFAULT_TARGET_DB,
                        activation_path=DEFAULT_TARGET_ACTIVATION,
                    )
                except Exception as exc:
                    commit_conn = None
                    commit_error = f"target_writer_open_failed:{type(exc).__name__}:{exc}"
                worker = TargetProductionWorker(
                    DEFAULT_TARGET_DB,
                    commit_conn=commit_conn,
                    commit_error=commit_error,
                    canonical_source_db_path=args.db,
                )
                try:
                    state["production"] = run_target_production_cycle(
                        worker
                    )
                finally:
                    worker.close()
                state["completed_at"] = utc_now()
                _atomic_json(args.status, state)
                print(json.dumps(state, ensure_ascii=False, sort_keys=True))
                return 0 if state["production"].get("ok") else 1
            lanes = tuple(config.get("production_source_allowlist") or []) if args.once == "all" else (args.once,) if args.once in {"amsc", "awc"} else ()
            for lane in lanes:
                try:
                    state["source_results"][lane] = run_once(conn, config, engine, lane)
                except Exception as exc:
                    state["source_results"][lane] = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
            state["completed_at"] = utc_now()
            _atomic_json(args.status, state)
            print(json.dumps(state, ensure_ascii=False, sort_keys=True))
            return 0 if all(item.get("ok") for item in state["source_results"].values()) and not registry.get("errors") else 1

        stopping = False

        def stop(_signum: int, _frame: Any) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        enabled_lanes = tuple(config.get("production_source_allowlist") or [])
        executor = ThreadPoolExecutor(
            max_workers=max(4, len(enabled_lanes) * 2 + 1),
            thread_name_prefix="reported-trajectory-worker",
        )
        # Forecast network I/O is deliberately isolated from the source and
        # decision pool. A slow model publication endpoint must never consume
        # the worker needed to drain durable observations or compile a
        # production_no_live decision.
        forecast_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="reported-trajectory-forecast-prefetch",
        )
        registry_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="reported-trajectory-market-registry",
        )
        lane_workers = {lane: LaneWorker(args.db, config, lane) for lane in enabled_lanes}
        prewarm_workers = {
            lane: CandidatePrewarmWorker(args.db, config, lane) for lane in enabled_lanes
        } if config.get("prewarm_policy", {}).get("enabled") is True else {}
        registry_worker = RegistryRefreshWorker(args.db, config)
        commit_error = None
        try:
            commit_conn = open_target_writer_if_enabled(
                DEFAULT_TARGET_DB,
                activation_path=DEFAULT_TARGET_ACTIVATION,
            )
        except Exception as exc:
            commit_conn = None
            commit_error = f"target_writer_open_failed:{type(exc).__name__}:{exc}"
        production_worker = TargetProductionWorker(
            DEFAULT_TARGET_DB,
            commit_conn=commit_conn,
            commit_error=commit_error,
            canonical_source_db_path=args.db,
        )
        # Forecast prefetch owns network/file work only.  It receives no
        # writable SSOT handle; the production worker above is the sole
        # target database writer and ingests the atomically published
        # registry snapshot on its next cycle.
        forecast_worker = TargetProductionWorker(
            DEFAULT_TARGET_DB,
            commit_conn=None,
            commit_error=None,
            canonical_source_db_path=args.db,
            # Use the worker's frozen canonical production station scope.
            # Forward OOS is observational for production_no_live and must
            # never narrow resident acquisition to a single station.
        )
        lane_futures: dict[str, Any] = {}
        lane_recovery_attempts = {lane: 0 for lane in lane_workers}
        prewarm_futures: dict[str, Any] = {}
        production_future: Any = None
        forecast_future: Any = None
        registry_future: Any = None
        production_recovery_attempts = 0
        forecast_recovery_attempts = 0
        registry_recovery_attempts = 0
        now_mono = time.monotonic()
        registry_refresh_sec = max(
            60.0,
            float(config["poll_policy"]["market_registry_refresh_sec"]),
        )
        registry_retry_sec = max(
            10.0,
            min(
                registry_refresh_sec,
                float(config["poll_policy"]["market_registry_retry_sec"]),
            ),
        )
        materialized_registry_poll_sec = 60.0
        next_materialized_registry_read = now_mono
        next_registry_refresh = now_mono
        next_source = {lane: now_mono for lane in enabled_lanes}
        next_prewarm = {lane: now_mono for lane in prewarm_workers}
        next_reconcile = now_mono
        next_status_write = now_mono
        next_production = now_mono
        next_forecast = now_mono
        next_storage_probe = 0.0
        storage_guard: dict[str, Any] = {
            "ok": False,
            "state": "storage_probe_not_run_fail_closed",
        }
        while not stopping:
            now_mono = time.monotonic()
            if now_mono >= next_storage_probe:
                try:
                    storage_guard = archive_storage_guard(args.db)
                except Exception as exc:
                    storage_guard = {
                        "ok": False,
                        "state": "storage_probe_failed_fail_closed",
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                state["storage_guard"] = storage_guard
                next_storage_probe = now_mono + 5.0
            if now_mono >= next_materialized_registry_read:
                try:
                    materialized_registry = current_registry_summary(conn)
                    if materialized_registry is None:
                        raise RuntimeError("materialized_registry_missing")
                    state["registry"] = materialized_registry
                    state.pop("registry_error", None)
                    registry_healthy = (
                        int(materialized_registry.get("official_market_count") or 0) > 0
                        and int(materialized_registry.get("eligible_market_count") or 0) > 0
                    )
                except Exception as exc:
                    state["registry_error"] = f"{type(exc).__name__}:{exc}"
                    registry_healthy = False
                state["registry_watchdog"] = {
                    "healthy": registry_healthy,
                    "mode": "automatic_official_gamma_registry",
                    "network_refresh_enabled": True,
                    "refresh_in_flight": (
                        registry_future is not None
                        and not registry_future.done()
                    ),
                    "refresh_sec": registry_refresh_sec,
                    "retry_sec": registry_retry_sec,
                    "poll_sec": materialized_registry_poll_sec,
                }
                next_materialized_registry_read = (
                    now_mono + materialized_registry_poll_sec
                )
            for lane, future in list(lane_futures.items()):
                if not future.done():
                    continue
                try:
                    state["source_results"][lane] = future.result()
                    lane_recovery_attempts[lane] = 0
                except Exception as exc:
                    result = {"ok": False, "error": f"{type(exc).__name__}:{exc}", "at": utc_now()}
                    if is_recoverable_sqlite_io_error(exc):
                        attempt = lane_recovery_attempts.get(lane, 0) + 1
                        lane_recovery_attempts[lane] = attempt
                        try:
                            lane_workers[lane].reopen()
                            result["connection_recovery"] = {
                                "ok": True,
                                "attempt": attempt,
                                "backoff_sec": SQLITE_IO_RECOVERY_BACKOFF_SEC,
                                "reason": "sqlite_disk_io_error",
                            }
                            next_source[lane] = time.monotonic() + SQLITE_IO_RECOVERY_BACKOFF_SEC
                        except Exception as recovery_exc:
                            result["connection_recovery"] = {
                                "ok": False,
                                "attempt": attempt,
                                "reason": "sqlite_disk_io_error",
                                "error": f"{type(recovery_exc).__name__}:{recovery_exc}",
                            }
                    state["source_results"][lane] = result
                del lane_futures[lane]
            for lane, future in list(prewarm_futures.items()):
                if not future.done():
                    continue
                try:
                    state.setdefault("candidate_prewarm", {})[lane] = future.result()
                except Exception as exc:
                    state.setdefault("candidate_prewarm", {})[lane] = {
                        "ok": False, "error": f"{type(exc).__name__}:{exc}", "at": utc_now()
                    }
                del prewarm_futures[lane]
            if registry_future is not None and registry_future.done():
                try:
                    registry_result = registry_future.result()
                    registry_recovery_attempts = 0
                    state["registry_refresh"] = {
                        "ok": True,
                        **registry_result,
                        "calls_order_gateway": False,
                        "places_orders": False,
                    }
                    next_registry_refresh = time.monotonic() + registry_refresh_sec
                except Exception as exc:
                    recovery: dict[str, Any] | None = None
                    if is_recoverable_sqlite_io_error(exc):
                        registry_recovery_attempts += 1
                        try:
                            registry_worker.reopen()
                            recovery = {
                                "ok": True,
                                "attempt": registry_recovery_attempts,
                                "reason": "sqlite_disk_io_error",
                            }
                        except Exception as recovery_exc:
                            recovery = {
                                "ok": False,
                                "attempt": registry_recovery_attempts,
                                "reason": "sqlite_disk_io_error",
                                "error": (
                                    f"{type(recovery_exc).__name__}:"
                                    f"{recovery_exc}"
                                ),
                            }
                    state["registry_refresh"] = {
                        "ok": False,
                        "state": "refresh_failed_previous_generation_retained",
                        "error": f"{type(exc).__name__}:{exc}",
                        "retry_sec": registry_retry_sec,
                        "calls_order_gateway": False,
                        "places_orders": False,
                        **({"connection_recovery": recovery} if recovery else {}),
                    }
                    next_registry_refresh = time.monotonic() + registry_retry_sec
                registry_future = None
            if production_future is not None and production_future.done():
                try:
                    production_result = production_future.result()
                    if target_result_has_recoverable_sqlite_io_error(
                        production_result
                    ):
                        production_recovery_attempts += 1
                        try:
                            production_worker.reopen_target_connections()
                            production_result["connection_recovery"] = {
                                "ok": True,
                                "attempt": production_recovery_attempts,
                                "backoff_sec": SQLITE_IO_RECOVERY_BACKOFF_SEC,
                                "reason": "sqlite_disk_io_error",
                            }
                            next_production = (
                                time.monotonic()
                                + SQLITE_IO_RECOVERY_BACKOFF_SEC
                            )
                        except Exception as recovery_exc:
                            production_result["connection_recovery"] = {
                                "ok": False,
                                "attempt": production_recovery_attempts,
                                "reason": "sqlite_disk_io_error",
                                "error": (
                                    f"{type(recovery_exc).__name__}:"
                                    f"{recovery_exc}"
                                ),
                            }
                    else:
                        production_recovery_attempts = 0
                    state["production"] = production_result
                except Exception as exc:
                    state["production"] = {"ok": False, "state": "blocked_fail_closed", "reason": f"{type(exc).__name__}:{exc}", "places_orders": False, "calls_order_gateway": False, "checked_at": utc_now()}
                production_future = None
            if forecast_future is not None and forecast_future.done():
                try:
                    forecast_result = forecast_future.result()
                    if target_result_has_recoverable_sqlite_io_error(
                        forecast_result,
                        forecast=True,
                    ):
                        forecast_recovery_attempts += 1
                        try:
                            forecast_worker.reopen_target_connections()
                            forecast_result["connection_recovery"] = {
                                "ok": True,
                                "attempt": forecast_recovery_attempts,
                                "backoff_sec": SQLITE_IO_RECOVERY_BACKOFF_SEC,
                                "reason": "sqlite_disk_io_error",
                            }
                            next_forecast = (
                                time.monotonic()
                                + SQLITE_IO_RECOVERY_BACKOFF_SEC
                            )
                        except Exception as recovery_exc:
                            forecast_result["connection_recovery"] = {
                                "ok": False,
                                "attempt": forecast_recovery_attempts,
                                "reason": "sqlite_disk_io_error",
                                "error": (
                                    f"{type(recovery_exc).__name__}:"
                                    f"{recovery_exc}"
                                ),
                            }
                    else:
                        forecast_recovery_attempts = 0
                    state["forecast_prefetch"] = forecast_result
                except Exception as exc:
                    state["forecast_prefetch"] = {
                        "schema": "reported_trajectory_forecast_prefetch_v1",
                        "state": "blocked_fail_closed",
                        "reason": f"{type(exc).__name__}:{exc}",
                        "places_orders": False,
                        "calls_order_gateway": False,
                        "live_submit_authorized": False,
                        "orders_submitted": 0,
                        "checked_at": utc_now(),
                    }
                forecast_future = None
            storage_ok = storage_guard.get("ok") is True
            if (
                storage_ok
                and now_mono >= next_registry_refresh
                and registry_future is None
            ):
                registry_future = registry_executor.submit(
                    registry_worker.run
                )
            if storage_ok and "amsc" in lane_workers and now_mono >= next_source.get("amsc", now_mono) and "amsc" not in lane_futures:
                lane_futures["amsc"] = executor.submit(lane_workers["amsc"].poll)
                next_source["amsc"] = time.monotonic() + amsc_poll_interval(config, datetime.now(timezone.utc))
            if storage_ok and "awc" in lane_workers and now_mono >= next_source.get("awc", now_mono) and "awc" not in lane_futures:
                lane_futures["awc"] = executor.submit(lane_workers["awc"].poll)
                next_source["awc"] = time.monotonic() + awc_poll_interval(config, datetime.now(timezone.utc))
            for lane, worker in prewarm_workers.items():
                if not storage_ok or now_mono < next_prewarm[lane] or lane in prewarm_futures:
                    continue
                prewarm_futures[lane] = executor.submit(worker.run)
                next_prewarm[lane] = time.monotonic() + candidate_prewarm_interval(
                    config, datetime.now(timezone.utc)
                )
            if storage_ok and now_mono >= next_production and production_future is None:
                production_future = executor.submit(
                    run_target_production_cycle,
                    production_worker,
                )
                next_production = time.monotonic() + 15.0
            if storage_ok and now_mono >= next_forecast and forecast_future is None:
                state["forecast_prefetch"] = {
                    "schema": "reported_trajectory_forecast_prefetch_v1",
                    "resident_nwp_capture": resident_nwp_in_flight_receipt(
                        forecast_worker,
                        scheduled_at_utc=utc_now(),
                    ),
                    "forward_taf_capture": {
                        "state": "pending_same_bounded_worker",
                        "calls_order_gateway": False,
                        "places_orders": False,
                    },
                    "calls_order_gateway": False,
                    "places_orders": False,
                    "live_submit_authorized": False,
                    "orders_submitted": 0,
                }
                forecast_future = forecast_executor.submit(
                    run_target_forecast_refresh_cycle,
                    forecast_worker,
                )
                next_forecast = time.monotonic() + 15.0
            if storage_ok and now_mono >= next_reconcile:
                state["reconcile"] = engine.reconcile_one()
                next_reconcile = now_mono + (0.1 if state["reconcile"].get("state") != "idle" else 1.0)
            if now_mono >= next_status_write:
                state["heartbeat_at"] = utc_now()
                _atomic_json(args.status, state)
                next_status_write = now_mono + 30.0
            time.sleep(0.01)
        state["stopped_at"] = utc_now()
        _atomic_json(args.status, state)
        return 0
    finally:
        # Cover normal SIGTERM and unexpected exceptions with the same bounded
        # ownership cleanup.  Objects are discovered defensively because
        # --once paths return before resident resources are constructed.
        for executor_name in (
            "executor",
            "forecast_executor",
            "registry_executor",
        ):
            resident_executor = locals().get(executor_name)
            if resident_executor is not None:
                resident_executor.shutdown(
                    wait=True,
                    cancel_futures=False,
                )
        for worker_group_name in ("lane_workers", "prewarm_workers"):
            worker_group = locals().get(worker_group_name)
            if isinstance(worker_group, Mapping):
                for resident_worker in worker_group.values():
                    resident_worker.close()
        for worker_name in (
            "production_worker",
            "forecast_worker",
            "registry_worker",
        ):
            resident_worker = locals().get(worker_name)
            if resident_worker is not None:
                resident_worker.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
