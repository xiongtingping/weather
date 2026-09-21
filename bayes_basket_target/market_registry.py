"""Isolated market-registry export and target-local ingress.

The resident live database may be read by an explicit, read-only exporter,
but the target lane never opens it at runtime.  A registry snapshot is a
content-addressed append-only object in the target SSOT.  Consumers select
the newest complete target snapshot and never fall back to the shared-live
table.  Small in-memory unit-test fixtures may still expose the historical
table name; that compatibility path is intentionally limited to SQLite's
``:memory:`` connections and is not available to a file-backed deployment.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .database import SHARED_LIVE_DB
from .settlement_time import settlement_day_state
from .resource_bounds import (
    load_bounded_json,
    load_verified_file_cached,
    write_bounded_json_if_changed,
)
from .resource_bounds import assert_connection_target_capacity


SCHEMA = "bayes_basket_target_market_registry_snapshot_v1"
# Charter 48(e): the target catalog carries each station's nearest listed
# days up to this many beyond the day being settled.  Markets list at D-2,
# so two is the whole listing window (tomorrow and the day just listed);
# the publisher's scope cap (MAX_TARGET_STATIONS * 3) is sized for it.
LISTED_DAYS_PER_STATION = 2
SHARED_EXPORT_SCOPE = "shared_live_registry_export_only"
TARGET_LOCAL_EXPORT_SCOPE = "target_local_registry_scope_export"
EXPORT_SCOPES = frozenset({SHARED_EXPORT_SCOPE, TARGET_LOCAL_EXPORT_SCOPE})
TABLE = "basket_target_market_registry"
SNAPSHOT_TABLE = "basket_target_market_registry_snapshot"
LEGACY_TEST_TABLE = "reported_trajectory_market_registry"
REQUIRED_ROW_KEYS = (
    "slug", "event_date", "outcome", "city_slug", "station_id",
    "settlement_timezone", "settlement_unit", "source_lane",
    "source_eligible", "source_block_reason", "resolution_url",
    "rules_sha256", "yes_token", "no_token", "market_active",
    "market_accepting_orders", "registry_json", "refreshed_at",
)

DDL = f"""
CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} (
  snapshot_sha256 TEXT PRIMARY KEY CHECK(length(snapshot_sha256)=64),
  source_schema TEXT NOT NULL,
  source_scope TEXT NOT NULL CHECK(source_scope='shared_live_registry_export_only'),
  exported_at_utc TEXT NOT NULL,
  source_database_path TEXT NOT NULL,
  row_count INTEGER NOT NULL CHECK(row_count>=0),
  ingested_at_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)
);

CREATE TABLE IF NOT EXISTS {TABLE} (
  snapshot_sha256 TEXT NOT NULL REFERENCES {SNAPSHOT_TABLE}(snapshot_sha256),
  slug TEXT NOT NULL,
  event_date TEXT NOT NULL,
  outcome TEXT NOT NULL,
  city_slug TEXT NOT NULL,
  station_id TEXT,
  settlement_timezone TEXT,
  settlement_unit TEXT,
  source_lane TEXT NOT NULL CHECK(source_lane IN ('amsc','awc','blocked')),
  source_eligible INTEGER NOT NULL CHECK(source_eligible IN (0,1)),
  source_block_reason TEXT,
  resolution_url TEXT,
  rules_sha256 TEXT NOT NULL CHECK(length(rules_sha256)=64),
  yes_token TEXT,
  no_token TEXT,
  market_active INTEGER NOT NULL CHECK(market_active IN (0,1)),
  market_accepting_orders INTEGER NOT NULL CHECK(market_accepting_orders IN (0,1)),
  registry_json TEXT NOT NULL,
  refreshed_at TEXT NOT NULL,
  PRIMARY KEY(snapshot_sha256,slug,event_date,outcome)
);

CREATE INDEX IF NOT EXISTS basket_target_market_registry_latest_idx
  ON {TABLE}(snapshot_sha256,station_id,event_date,source_eligible,market_active,market_accepting_orders);

-- The existing index leads with the snapshot hash, so a read that knows only
-- the scope cannot use it and scanned all 210k rows -- 237ms a station on a
-- path that runs per station every cycle.  Scope-first is the shape the
-- production reads actually have.
CREATE INDEX IF NOT EXISTS basket_target_market_registry_scope_idx
  ON {TABLE}(station_id,event_date);

-- Seven readers ask one scope's settlement timezone -- the native P0
-- compile, the label and truth materializers, the forward training fact
-- validator, the observation store, the training-fact builder -- and each
-- DISTINCTs it off every snapshot row the scope has.  The registry appends a
-- row per scope every refresh, so a scope carries ~880 of them by its event
-- day, each ~1.6KB wide with its registry_json, and the scope index above
-- makes every one a table lookup: 75ms a call warm, measured 2026-09-21, 37%
-- of the native P0 compile's SQL time, once per packet per cycle.  With the
-- timezone in the index the read never touches the table.
CREATE INDEX IF NOT EXISTS basket_target_market_registry_scope_tz_idx
  ON {TABLE}(station_id,event_date,settlement_timezone);

-- Charter 179: the observed-high read asks the same scope question with the
-- source lane as well (``registry_scope_timezone_lanes``).  Charter 178
-- remembers its answer until the registry changes, but every snapshot
-- appends rows to every live scope, so each refresh re-asks all of them
-- through ~880 table lookups apiece: 122-135 calls, 12.6-20.9s in the cycle
-- after each snapshot (2026-09-21).  With the lane in the index too the
-- re-ask never touches the table.
CREATE INDEX IF NOT EXISTS basket_target_market_registry_scope_lane_idx
  ON {TABLE}(station_id,event_date,settlement_timezone,source_lane);

-- The truth-increment probe asks the other question this table gets: which
-- ELIGIBLE station-days since a date have no accepted truth row yet.  Both
-- of its predicates were unseekable against the indexes above -- scope-first
-- leads with the station, and the latest index leads with the snapshot hash
-- -- so it scanned all 429k rows to build a DISTINCT, every cycle.  Measured
-- 2026-09-08: 54.7 seconds in one statement on the live book, inside a stage
-- that runs on the same cycle a decision races its quote lease on.
-- Day-first and partial on the eligible rows: the date becomes a seek and
-- the two columns cover the read.
CREATE INDEX IF NOT EXISTS basket_target_market_registry_eligible_day_idx
  ON {TABLE}(event_date, station_id) WHERE source_eligible=1;

-- The newest snapshot is chosen by ordering the snapshot table and taking
-- one row, and without an index on that order SQLite scanned every snapshot,
-- ran the correlated existence check on each, then sorted the survivors in a
-- temp b-tree.  2.78 seconds standalone and 12.2 under the writer's lock,
-- inside a per-cycle path -- one of the reads that was affordable at one
-- event day and stopped being affordable at two.  Ordered, it walks the
-- index and stops at the first row that qualifies: 0.001 seconds.
CREATE INDEX IF NOT EXISTS basket_target_market_registry_snapshot_recency_idx
  ON {SNAPSHOT_TABLE}(ingested_at_utc DESC, snapshot_sha256 DESC);
-- The board asks this table for one row per station -- the settlement unit
-- each one trades in -- and got it by grouping 398k scope rows through a
-- temp B-tree, 10.7 seconds of every render (2026-08-29).  Two columns,
-- covering, answers it in 0.03.
CREATE INDEX IF NOT EXISTS basket_target_market_registry_unit_idx
  ON {TABLE}(station_id,settlement_unit);
"""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _text(name: str, value: Any, *, allow_empty: bool = False) -> str:
    result = str(value or "").strip()
    if not result and not allow_empty:
        raise ContractError(f"{name}_missing")
    return result


def _iso(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def _flag(name: str, value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if value in (0, 1):
        return int(value)
    raise ContractError(f"{name}_must_be_boolean")


def _row_for_export(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = {key: raw.get(key) for key in REQUIRED_ROW_KEYS}
    row["slug"] = _text("slug", row["slug"])
    row["event_date"] = _text("event_date", row["event_date"])
    row["outcome"] = _text("outcome", row["outcome"])
    row["city_slug"] = _text("city_slug", row["city_slug"], allow_empty=True)
    row["station_id"] = _text("station_id", row["station_id"], allow_empty=True)
    row["settlement_timezone"] = _text("settlement_timezone", row["settlement_timezone"], allow_empty=True)
    row["settlement_unit"] = _text("settlement_unit", row["settlement_unit"], allow_empty=True)
    source_lane = _text("source_lane", row["source_lane"])
    if source_lane not in {"amsc", "awc", "blocked"}:
        raise ContractError("source_lane_invalid")
    row["source_lane"] = source_lane
    row["source_eligible"] = _flag("source_eligible", row["source_eligible"])
    row["market_active"] = _flag("market_active", row["market_active"])
    row["market_accepting_orders"] = _flag("market_accepting_orders", row["market_accepting_orders"])
    for key in ("source_block_reason", "resolution_url", "yes_token", "no_token"):
        row[key] = _text(key, row[key], allow_empty=True)
    row["rules_sha256"] = _text("rules_sha256", row["rules_sha256"])
    if len(row["rules_sha256"]) != 64:
        raise ContractError("rules_sha256_invalid")
    registry = row["registry_json"]
    if isinstance(registry, str):
        try:
            registry = json.loads(registry)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("registry_json_invalid") from exc
    if not isinstance(registry, Mapping):
        raise ContractError("registry_json_mapping_required")
    row["registry_json"] = json.dumps(dict(registry), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    row["refreshed_at"] = _iso("refreshed_at", row["refreshed_at"])
    return row


def _snapshot_core(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "snapshot_sha256"}


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload = load_bounded_json(path)
    except Exception as exc:
        raise ContractError("market_registry_snapshot_json_invalid") from exc
    if not isinstance(payload, Mapping):
        raise ContractError("market_registry_snapshot_mapping_required")
    payload = dict(payload)
    supplied = _text("snapshot_sha256", payload.get("snapshot_sha256"))
    expected = hashlib.sha256(_canonical(_snapshot_core(payload))).hexdigest()
    if supplied != expected:
        raise ContractError("market_registry_snapshot_content_sha_mismatch")
    if payload.get("schema") != SCHEMA:
        raise ContractError("market_registry_snapshot_schema_invalid")
    source_scope = str(payload.get("source_scope") or "")
    if source_scope not in EXPORT_SCOPES:
        raise ContractError("market_registry_snapshot_scope_invalid")
    if source_scope == TARGET_LOCAL_EXPORT_SCOPE:
        boundary = payload.get("boundary")
        required_boundary = {
            "target_only": True,
            "reads_target_database": True,
            "writes_target_database": False,
            "reads_shared_live_database": False,
            "writes_shared_live_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
        }
        if (
            not isinstance(boundary, Mapping)
            or any(boundary.get(key) is not value for key, value in required_boundary.items())
        ):
            raise ContractError("market_registry_target_local_boundary_invalid")
        source_database = Path(
            _text("source_database_path", payload.get("source_database_path"))
        ).expanduser().resolve()
        if source_database == SHARED_LIVE_DB:
            raise ContractError("market_registry_target_local_database_invalid")
        source_snapshot = _text(
            "source_snapshot_sha256",
            payload.get("source_snapshot_sha256"),
        )
        if len(source_snapshot) != 64:
            raise ContractError("market_registry_source_snapshot_sha256_invalid")
    if payload.get("source_schema") != "reported_trajectory_market_registry_v1":
        raise ContractError("market_registry_snapshot_source_schema_invalid")
    _iso("exported_at_utc", payload.get("exported_at_utc"))
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ContractError("market_registry_snapshot_rows_missing")
    normalized = [_row_for_export(item) for item in rows if isinstance(item, Mapping)]
    if len(normalized) != len(rows):
        raise ContractError("market_registry_snapshot_row_invalid")
    if int(payload.get("row_count") or -1) != len(normalized):
        raise ContractError("market_registry_snapshot_row_count_mismatch")
    identities: set[tuple[str, str, str]] = set()
    for row in normalized:
        identity = (row["slug"], row["event_date"], row["outcome"])
        if identity in identities:
            raise ContractError("market_registry_snapshot_duplicate_identity")
        identities.add(identity)
    payload["rows"] = normalized
    return payload


def load_market_registry_snapshot(path: Path) -> dict[str, Any]:
    """Load and content-verify one exported registry snapshot without ingesting it.

    Answered from memory while the file is the same file: the resident NWP
    rotation reads one snapshot several times a call, sixty-odd calls a poll
    (see ``resource_bounds.load_verified_file_cached``).
    """

    return load_verified_file_cached(
        path, _read_payload, namespace="market_registry_snapshot"
    )


def export_shared_live_registry(
    *,
    source_db: Path,
    output_path: Path,
    station_id: str = "",
    event_date: str = "",
    as_of_utc: str | datetime | None = None,
    include_not_started_for_p0: bool = False,
    current_catalog_only: bool = False,
) -> dict[str, Any]:
    """Read only the shared registry and write one independent JSON object.

    The shared/live registry carries markets listed before their settlement
    day starts.  Charter 48 makes a listing tradable at listing, so with
    ``include_not_started_for_p0`` those rows are target scope; without it
    they are filtered before the target snapshot is written.

    ``current_catalog_only`` bounds the catalog to what the resident lanes
    are sized for: no closed days, and per station at most
    ``LISTED_DAYS_PER_STATION`` not-started days, nearest first.  Until
    2026-09-02 that bound was "the first not-started day only", which is
    one day short of the listing window: markets list at D-2, so while a
    station's D+1 is not yet open its D+2 -- the day just listed -- was
    dropped as an "additional future scope" (528 rows, 48 stations, every
    cycle of 2026-09-02) and never reached the target SSOT, so no P0 was
    ever compiled for it and clause 48 had nothing to open.
    """

    source = Path(source_db).expanduser().resolve()
    if not source.is_file():
        raise ContractError("shared_live_registry_source_missing")
    clauses: list[str] = []
    params: list[str] = []
    if station_id:
        clauses.append("station_id=?")
        params.append(station_id)
    if event_date:
        clauses.append("event_date=?")
        params.append(event_date)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='reported_trajectory_market_registry'"
        ).fetchone()
        if table is None:
            raise ContractError("shared_live_registry_table_missing")
        source_rows = [
            _row_for_export(dict(row))
            for row in conn.execute(
                "SELECT slug,event_date,outcome,city_slug,station_id,settlement_timezone,settlement_unit,"
                "source_lane,source_eligible,source_block_reason,resolution_url,rules_sha256,yes_token,no_token,"
                "market_active,market_accepting_orders,registry_json,refreshed_at "
                "FROM reported_trajectory_market_registry" + where +
                " ORDER BY slug,event_date,outcome",
                tuple(params),
            )
        ]
    decision_clock = as_of_utc or datetime.now(timezone.utc)
    classified_rows: list[tuple[dict[str, Any], str]] = []
    future_dates_by_station: dict[str, set[str]] = {}
    unclassifiable_rows: list[dict[str, str]] = []
    for row in source_rows:
        try:
            state = str(settlement_day_state(
                row["event_date"],
                as_of_utc=decision_clock,
                settlement_timezone=str(
                    row.get("settlement_timezone") or ""
                ),
            )["state"])
        except Exception as exc:
            # A row that cannot be placed on a settlement day cannot enter
            # target scope anyway -- most often a market discovered before its
            # station timezone binding caught up.  Aborting the whole export on
            # it froze the published catalog for two days behind 44 such rows,
            # so every downstream capture scope inherited a stale station set
            # while the discovery pipeline was in fact working.
            #
            # Dropping the row is narrower than dropping the catalog, and the
            # count is reported so the binding gap stays visible.
            unclassifiable_rows.append({
                "slug": str(row.get("slug") or ""),
                "event_date": str(row.get("event_date") or ""),
                "station_id": str(row.get("station_id") or ""),
                "settlement_timezone": str(
                    row.get("settlement_timezone") or ""
                ),
                "reason": f"{type(exc).__name__}",
            })
            continue
        classified_rows.append((row, state))
        if state == "not_started":
            station_scope = str(
                row.get("station_id")
                or row.get("city_slug")
                or row.get("slug")
                or ""
            )
            future_dates_by_station.setdefault(station_scope, set()).add(
                str(row["event_date"])
            )
    admitted_future_dates_by_station = {
        station_scope: frozenset(sorted(dates)[:LISTED_DAYS_PER_STATION])
        for station_scope, dates in future_dates_by_station.items()
    }

    rows: list[dict[str, Any]] = []
    excluded_future_rows = 0
    excluded_closed_row_count = 0
    excluded_additional_future_scope_row_count = 0
    for row, state in classified_rows:
        if current_catalog_only and state == "closed":
            excluded_closed_row_count += 1
            continue
        if state == "not_started" and not include_not_started_for_p0:
            excluded_future_rows += 1
            continue
        if current_catalog_only and state == "not_started":
            station_scope = str(
                row.get("station_id")
                or row.get("city_slug")
                or row.get("slug")
                or ""
            )
            if (
                str(row["event_date"])
                not in admitted_future_dates_by_station.get(
                    station_scope, frozenset()
                )
            ):
                excluded_additional_future_scope_row_count += 1
                continue
        rows.append(row)
    destination = Path(output_path).expanduser().resolve()
    stable_scope = {
        "source_database_path": str(source),
        "station_id": station_id,
        "event_date": event_date,
        "include_not_started_for_p0": bool(include_not_started_for_p0),
        "current_catalog_only": bool(current_catalog_only),
        "rows": rows,
    }
    stable_scope_sha256 = content_sha256(stable_scope)
    if destination.is_file():
        try:
            existing = load_bounded_json(destination)
        except Exception:
            existing = None
        if (
            isinstance(existing, Mapping)
            and existing.get("schema") == SCHEMA
            and existing.get("stable_scope_sha256") == stable_scope_sha256
        ):
            return {
                "schema": SCHEMA,
                "state": "UNCHANGED_NO_WRITE",
                "snapshot_sha256": str(existing.get("snapshot_sha256") or ""),
                "row_count": len(rows),
                "source_row_count": len(source_rows),
                "current_catalog_only": bool(current_catalog_only),
                "excluded_future_row_count": excluded_future_rows,
                "unclassifiable_row_count": len(unclassifiable_rows),
                "excluded_closed_row_count": excluded_closed_row_count,
                "excluded_additional_future_scope_row_count": (
                    excluded_additional_future_scope_row_count
                ),
                "as_of_utc": str(
                    (existing.get("filters") or {}).get("as_of_utc") or ""
                ),
                "output": str(destination),
                "source_database": str(source),
                "boundary": {
                    "reads_shared_live_database": True,
                    "writes_shared_live_database": False,
                    "writes_target_database": False,
                    "calls_order_gateway": False,
                    "places_orders": False,
                },
            }
    exported_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    normalized_clock = decision_clock.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if isinstance(decision_clock, datetime) else str(decision_clock)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "source_schema": "reported_trajectory_market_registry_v1",
        "source_scope": "shared_live_registry_export_only",
        "source_database_path": str(source),
        "exported_at_utc": exported_at,
        "source_row_count": len(source_rows),
        "row_count": len(rows),
        "filters": {
            "station_id": station_id,
            "event_date": event_date,
            "as_of_utc": normalized_clock,
            "excluded_future_row_count": excluded_future_rows,
            "unclassifiable_row_count": len(unclassifiable_rows),
            "include_not_started_for_p0": bool(include_not_started_for_p0),
            "current_catalog_only": bool(current_catalog_only),
            "excluded_closed_row_count": excluded_closed_row_count,
            "excluded_additional_future_scope_row_count": (
                excluded_additional_future_scope_row_count
            ),
        },
        "rows": rows,
        "stable_scope_sha256": stable_scope_sha256,
        "writes_shared_live_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "forward_p0_catalog_only": bool(include_not_started_for_p0),
    }
    payload["snapshot_sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
    write_bounded_json_if_changed(destination, payload)
    return {
        "schema": SCHEMA,
        "state": "EXPORTED",
        "snapshot_sha256": payload["snapshot_sha256"],
        "row_count": len(rows),
        "source_row_count": len(source_rows),
        "current_catalog_only": bool(current_catalog_only),
        "excluded_future_row_count": excluded_future_rows,
        "unclassifiable_row_count": len(unclassifiable_rows),
        "excluded_closed_row_count": excluded_closed_row_count,
        "excluded_additional_future_scope_row_count": (
            excluded_additional_future_scope_row_count
        ),
        "as_of_utc": normalized_clock,
        "output": str(destination),
        "source_database": str(source),
        "boundary": {
            "reads_shared_live_database": True,
            "writes_shared_live_database": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }


def apply_target_market_registry_schema(conn: sqlite3.Connection, *, commit: bool = True) -> dict[str, Any]:
    """Install only target-local registry tables on an already-owned DB."""

    statement = ""
    for line in DDL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                conn.execute(statement.strip())
            statement = ""
    if statement.strip():
        raise RuntimeError("target_market_registry_ddl_incomplete")
    if commit:
        conn.commit()
    return {"schema": SCHEMA, "tables": [SNAPSHOT_TABLE, TABLE], "ok": True}


def ingest_target_market_registry(
    conn: sqlite3.Connection,
    *,
    snapshot_path: Path,
    commit: bool = False,
    as_of_utc: str | datetime | None = None,
) -> dict[str, Any]:
    """Validate and append one registry snapshot into the target SSOT."""

    payload = _read_payload(Path(snapshot_path).expanduser().resolve())
    decision_clock = as_of_utc or datetime.now(timezone.utc)
    forward_catalog = (
        payload.get("forward_p0_catalog_only") is True
        and isinstance(payload.get("filters"), Mapping)
        and payload["filters"].get("include_not_started_for_p0") is True
    )
    for row in payload["rows"]:
        try:
            state = settlement_day_state(
                row["event_date"],
                as_of_utc=decision_clock,
                settlement_timezone=str(row.get("settlement_timezone") or ""),
            )["state"]
        except Exception as exc:
            raise ContractError("market_registry_event_date_invalid") from exc
        if state == "not_started" and not forward_catalog:
            raise ContractError("market_registry_future_event_day_not_allowed")
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if {TABLE, SNAPSHOT_TABLE} - tables:
        raise ContractError("target_market_registry_schema_missing")
    snapshot_sha = str(payload["snapshot_sha256"])
    already = conn.execute(
        f"SELECT row_count,content_sha256 FROM {SNAPSHOT_TABLE} WHERE snapshot_sha256=?",
        (snapshot_sha,),
    ).fetchone()
    if already is not None:
        if int(already[0]) != int(payload["row_count"]) or str(already[1]) != snapshot_sha:
            raise ContractError("target_market_registry_snapshot_collision")
        return {
            "schema": SCHEMA,
            "state": "VALIDATED_NO_WRITE" if not commit else "ALREADY_COMMITTED",
            "snapshot_sha256": snapshot_sha,
            "row_count": int(payload["row_count"]),
            "wrote_rows": False,
            "writes_shared_live_database": False,
            "places_orders": False,
        }
    if not commit:
        return {
            "schema": SCHEMA,
            "state": "VALIDATED_NO_WRITE",
            "snapshot_sha256": snapshot_sha,
            "row_count": int(payload["row_count"]),
            "wrote_rows": False,
            "writes_shared_live_database": False,
            "places_orders": False,
        }
    assert_connection_target_capacity(conn)
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            f"INSERT INTO {SNAPSHOT_TABLE}(snapshot_sha256,source_schema,source_scope,exported_at_utc,"
            "source_database_path,row_count,ingested_at_utc,content_sha256) VALUES(?,?,?,?,?,?,?,?)",
            (snapshot_sha, payload["source_schema"], payload["source_scope"], payload["exported_at_utc"],
             str(payload.get("source_database_path") or ""), int(payload["row_count"]), now, snapshot_sha),
        )
        conn.executemany(
            f"INSERT INTO {TABLE}(snapshot_sha256,slug,event_date,outcome,city_slug,station_id,"
            "settlement_timezone,settlement_unit,source_lane,source_eligible,source_block_reason,"
            "resolution_url,rules_sha256,yes_token,no_token,market_active,market_accepting_orders,"
            "registry_json,refreshed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (snapshot_sha, row["slug"], row["event_date"], row["outcome"], row["city_slug"], row["station_id"],
                 row["settlement_timezone"], row["settlement_unit"], row["source_lane"], row["source_eligible"],
                 row["source_block_reason"], row["resolution_url"], row["rules_sha256"], row["yes_token"], row["no_token"],
                 row["market_active"], row["market_accepting_orders"], row["registry_json"], row["refreshed_at"])
                for row in payload["rows"]
            ],
        )
        if own_transaction:
            conn.commit()
    except Exception:
        if own_transaction:
            conn.rollback()
        raise
    return {
        "schema": SCHEMA,
        "state": "COMMITTED" if commit else "VALIDATED_NO_WRITE",
        "snapshot_sha256": snapshot_sha,
        "row_count": int(payload["row_count"]),
        "wrote_rows": bool(commit),
        "writes_shared_live_database": False,
        "places_orders": False,
    }


def _is_memory_connection(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return False
    return not str(row[2] or "")


def _latest_snapshot(
    conn: sqlite3.Connection,
    *,
    station_id: str = "",
    event_date: str = "",
) -> str:
    """Resolve the newest immutable snapshot that actually owns the scope.

    Registry exports may be bounded to one event date.  A newer shard for an
    unrelated date must not hide an older full snapshot that still owns the
    requested station/date.
    """

    params: list[Any] = []
    row_clauses = [
        "r.source_eligible=1",
        "r.market_active=1",
        "r.market_accepting_orders=1",
    ]
    if station_id:
        row_clauses.append("r.station_id=?")
        params.append(station_id)
    if event_date:
        row_clauses.append("r.event_date=?")
        params.append(event_date)
    scoped = bool(station_id or event_date)
    query = (
        f"SELECT s.snapshot_sha256 FROM {SNAPSHOT_TABLE} s "
        + (
            f"WHERE EXISTS (SELECT 1 FROM {TABLE} r "
            "WHERE r.snapshot_sha256=s.snapshot_sha256 AND "
            + " AND ".join(row_clauses)
            + ") "
            if scoped
            else ""
        )
        + "ORDER BY s.ingested_at_utc DESC,s.snapshot_sha256 DESC LIMIT 1"
    )
    row = conn.execute(query, tuple(params)).fetchone()
    if row is None:
        raise ContractError("target_market_registry_snapshot_empty")
    return _text("target_market_registry_snapshot_sha256", row[0])


def load_active_registry_rows(
    conn: sqlite3.Connection,
    *,
    station_id: str = "",
    event_date: str = "",
) -> list[dict[str, Any]]:
    """Read eligible target-local rows; allow only in-memory test fixtures.

    Charter 178: remembered per scope until the table could have changed
    them.  Every probability-envelope load resolves its scope's formal lane
    through here -- 3,817 calls in the first ledger-measured cycle, each one a
    table listing, a newest-snapshot search, the scope's rows and a JSON parse
    per row -- and the answer moves only when a snapshot is ingested or the
    oldest is retired.  Callers get their own copies.
    """
    memo_key = _triples_memo_key(conn)
    if memo_key is not None:
        hit = _ACTIVE_ROWS_MEMO.get((memo_key[0], station_id, event_date))
        if hit is not None and hit[0] == memo_key[1]:
            return copy.deepcopy(hit[1])
    result = _load_active_registry_rows_uncached(
        conn, station_id=station_id, event_date=event_date
    )
    if memo_key is not None:
        _scope_memo_put(
            _ACTIVE_ROWS_MEMO,
            (memo_key[0], station_id, event_date),
            (memo_key[1], copy.deepcopy(result)),
        )
    return result


def _load_active_registry_rows_uncached(
    conn: sqlite3.Connection,
    *,
    station_id: str = "",
    event_date: str = "",
) -> list[dict[str, Any]]:
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    params: list[Any] = []
    clauses = ["source_eligible=1", "market_active=1", "market_accepting_orders=1"]
    if station_id:
        clauses.append("station_id=?")
        params.append(station_id)
    if event_date:
        clauses.append("event_date=?")
        params.append(event_date)
    if {TABLE, SNAPSHOT_TABLE} <= tables:
        snapshot = _latest_snapshot(
            conn,
            station_id=station_id,
            event_date=event_date,
        )
        clauses.insert(0, "snapshot_sha256=?")
        params.insert(0, snapshot)
        rows = conn.execute(
            f"SELECT slug,event_date,station_id,settlement_timezone,settlement_unit,"
            f"outcome,yes_token,registry_json,source_lane "
            f"FROM {TABLE} WHERE " + " AND ".join(clauses) + " ORDER BY station_id,event_date,slug,outcome",
            tuple(params),
        ).fetchall()
    elif _is_memory_connection(conn) and LEGACY_TEST_TABLE in tables:
        legacy_columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({LEGACY_TEST_TABLE})")
        }
        def _field(name: str, default_sql: str = "''") -> str:
            return name if name in legacy_columns else f"{default_sql} AS {name}"

        rows = conn.execute(
            "SELECT " + ",".join([
                _field("slug"), _field("event_date"), _field("station_id"),
                _field("settlement_timezone"), _field("settlement_unit"),
                _field("outcome"), _field("yes_token"), _field("registry_json"),
                # Historical in-memory fixtures predate source routing.  This
                # compatibility default can never run on a file-backed target
                # database and therefore cannot become production authority.
                _field("source_lane", "'awc'"),
            ]) + " FROM reported_trajectory_market_registry WHERE " + " AND ".join(clauses[0:3] + clauses[3:]) +
            " ORDER BY station_id,event_date,slug,outcome",
            tuple(params),
        ).fetchall()
    else:
        raise ContractError("target_market_registry_snapshot_missing")
    result: list[dict[str, Any]] = []
    for row in rows:
        registry_json = str(row[7] or "{}")
        raw = json.loads(registry_json)
        if not isinstance(raw, Mapping):
            raise ContractError("market_registry_json_invalid")
        item = {
            "slug": str(row[0]), "event_date": str(row[1]), "station_id": str(row[2] or ""),
            "settlement_timezone": str(row[3] or ""),
            "settlement_unit": str(row[4] or ""),
            "outcome": str(row[5] or ""),
            "yes_token": str(row[6] or ""),
            "registry_json": registry_json,
            "source_lane": str(row[8] or "").strip().lower(),
            "market_id": str(raw.get("market_id") or ""),
            "lower": raw.get("lower"), "upper": raw.get("upper"),
            # The exchange's own minimum lot, carried raw like lower/upper and
            # validated where an order is actually sized.  Collected since
            # 2026-08-30; a market that will not state it is already blocked
            # upstream by source_block_reasons, so an eligible row reaching
            # here without one can only be a pre-change snapshot.
            "order_min_size": raw.get("order_min_size"),
            # And the exchange's own price increment for this token, carried
            # the same way.  The venue states it per token, not per market,
            # and moves one token to a finer grid when its price sinks into
            # the tail; inferring it from book levels instead is what let a
            # 0.01 rung be built on a 0.001 grid.
            "order_price_min_tick_size": raw.get("order_price_min_tick_size"),
        }
        if (
            not item["station_id"]
            or not item["outcome"]
            or item["source_lane"] not in {"amsc", "awc"}
        ):
            raise ContractError("market_registry_target_identity_missing")
        result.append(item)
    return result


# Per snapshot and per question: the distinct rows one snapshot's registry
# copy gives for that question, for ``_union_over_snapshots``.  Keyed by
# (database file, question, snapshot), so two databases never share an answer;
# in-memory databases are never stored.
_SNAPSHOT_ANSWERS: dict[tuple[str, str, str], frozenset[tuple[Any, ...]]] = {}

_SCOPE_TIMEZONE_QUESTION = (
    "SELECT DISTINCT station_id,event_date,settlement_timezone "
    f"FROM {TABLE} WHERE snapshot_sha256=?"
)
_ELIGIBLE_SCOPE_QUESTION = (
    "SELECT DISTINCT station_id,event_date "
    f"FROM {TABLE} WHERE snapshot_sha256=? AND source_eligible=1"
)


#: The covering index a scope-timezone question is answered from (charter 176).
SCOPE_TIMEZONE_INDEX = "basket_target_market_registry_scope_tz_idx"


def _union_over_snapshots(
    conn: sqlite3.Connection, question: str
) -> set[tuple[Any, ...]]:
    """The union of ``question`` over every snapshot the registry still holds.

    ``question`` is a DISTINCT read of one snapshot (``snapshot_sha256=?``).
    The registry is a union of content-addressed snapshots: a snapshot's rows
    are written in the same transaction as its snapshot row, re-ingesting an
    existing snapshot returns before inserting anything, and retention removes
    a snapshot's rows whole and never brings them back.  So one snapshot's
    answer never changes while it has rows, and the union over the snapshot
    table equals the same DISTINCT over the whole table.  Each snapshot is read
    once and remembered; a remembered snapshot costs one existence seek, and
    one whose rows are gone stops contributing.
    """

    try:
        database = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
    except (sqlite3.Error, TypeError, IndexError):
        database = ""
    remember = bool(database)
    answer: set[tuple[Any, ...]] = set()
    for (sha,) in conn.execute(
        f"SELECT snapshot_sha256 FROM {SNAPSHOT_TABLE}"
    ).fetchall():
        key = (database, question, str(sha))
        known = _SNAPSHOT_ANSWERS.get(key) if remember else None
        if known is None:
            known = frozenset(
                tuple(row) for row in conn.execute(question, (str(sha),))
            )
            if remember:
                _SNAPSHOT_ANSWERS[key] = known
        elif known and conn.execute(
            f"SELECT 1 FROM {TABLE} WHERE snapshot_sha256=? LIMIT 1",
            (str(sha),),
        ).fetchone() is None:
            known = frozenset()
            _SNAPSHOT_ANSWERS[key] = known
        answer |= known
    return answer


def registry_scope_timezone_triples(
    conn: sqlite3.Connection,
) -> set[tuple[Any, Any, Any]]:
    """``SELECT DISTINCT station_id,event_date,settlement_timezone`` over the table.

    Exactly that, NULLs and empty strings included, and read so that it does
    not cost what that statement costs.  No index carries the timezone, so any
    timezone question put to this table reads every matching row back from
    the table -- and the table holds every retained snapshot's full copy of
    the registry.  Measured 2026-09-14 on the live SSOT: the METAR planner's
    whole-table DISTINCT took 19.3s cold for 49 station pairs, and the TAF
    inbox ingest's per-scope DISTINCT took 23.7s for 457 scopes at ~1,500
    rows each; both ran every production cycle.

    The table is a union of content-addressed snapshots.  A snapshot's rows are
    written in the same transaction as its snapshot row, re-ingesting an
    existing snapshot returns before inserting anything, and retention removes
    a snapshot's rows whole and never brings them back.  So a snapshot's
    triples never change while it has rows: each is read once through the
    snapshot index and remembered, a remembered snapshot costs one existence
    seek afterwards, and one whose rows are gone stops contributing.
    """

    if _index_present(conn, SCOPE_TIMEZONE_INDEX):
        # Charter 178: the answer is remembered until the table could have
        # changed it.  Charter 177's index scan is 55ms with its pages warm,
        # but the first cycle measured with the statement ledger (2026-09-21
        # 08:54Z, the machine deep in swap) ran it 6 times for 64.3s -- the
        # pages had been evicted between calls, and the per-snapshot union it
        # replaced kept its answers in process memory, which is what survives
        # a page-cache purge.  The triples change only when a snapshot is
        # ingested (the snapshot table's newest rowid moves, and so does the
        # registry's) or when retention drops the oldest snapshot's rows
        # (retention goes oldest first and whole, so the registry's lowest
        # rowid moves).  Each of the three is one seek at a B-tree end.
        key = _triples_memo_key(conn)
        if key is not None:
            hit = _SCOPE_TIMEZONE_TRIPLES_MEMO.get(key[0])
            if hit is not None and hit[0] == key[1]:
                return set(hit[1])
        # Charter 177: with the timezone in an index the whole-table DISTINCT
        # never touches the table.  Measured 2026-09-21 on the live store:
        # 226ms cold and 55ms warm for 283 triples, against 9,671ms cold and
        # 135ms warm for the union over 2,572 snapshots -- and the cold cost
        # was paid by three callers on every restart.  The same answer by
        # definition (the union was built to equal this statement).  INDEXED
        # BY because the planner, left to itself, answers a whole-table read
        # of this table from the scope index and walks every row's page: the
        # eligible-scope sibling of this statement took 20s that way.
        answer = {
            tuple(row)
            for row in conn.execute(
                "SELECT DISTINCT station_id,event_date,settlement_timezone "
                f"FROM {TABLE} INDEXED BY {SCOPE_TIMEZONE_INDEX}"
            )
        }
        if key is not None:
            _SCOPE_TIMEZONE_TRIPLES_MEMO[key[0]] = (key[1], frozenset(answer))
        return answer
    return _union_over_snapshots(conn, _SCOPE_TIMEZONE_QUESTION)  # type: ignore[return-value]


# (database file) -> (table fingerprint, triples).  Only named databases.
_SCOPE_TIMEZONE_TRIPLES_MEMO: dict[str, tuple[tuple[Any, ...], frozenset]] = {}


def _triples_memo_key(
    conn: sqlite3.Connection,
) -> tuple[str, tuple[Any, ...]] | None:
    """(database, fingerprint of what can change this table), or None.

    Charter 178.  Everything read from this table changes only when a snapshot
    is ingested (the snapshot table's newest rowid moves, and the registry's)
    or when retention drops the oldest snapshot's rows (oldest first and whole,
    so the registry's lowest rowid moves).  None for an unnamed database or a
    store without both tables: callers then read the table as before."""
    try:
        database = _connection_database(conn)
        if not database:
            return None
        # Charter 181: one statement.  Each scalar subquery is a lone MIN or
        # MAX, which SQLite answers from the B-tree's end -- ``SELECT
        # MIN(rowid),MAX(rowid)`` in one select list is the form that scans
        # the table, not this one.  The key is taken on every memoized read
        # (thousands a cycle: 3.8s sampled at 11:04Z as three statements).
        snapshot_top, low, high = conn.execute(
            f"SELECT (SELECT MAX(rowid) FROM {SNAPSHOT_TABLE}),"
            f"(SELECT MIN(rowid) FROM {TABLE}),"
            f"(SELECT MAX(rowid) FROM {TABLE})"
        ).fetchone()
    except (sqlite3.Error, TypeError, IndexError, ValueError):
        return None
    return database, (snapshot_top, low, high)


def registry_fingerprint(
    conn: sqlite3.Connection,
) -> tuple[str, tuple[Any, ...]] | None:
    """(database, fingerprint) that changes whenever this table's rows can
    have; None when there is nothing to key on.  Charter 182: for readers
    outside this module that remember what they read from the registry."""

    return _triples_memo_key(conn)


def _connection_database(conn: sqlite3.Connection) -> str:
    """The file behind this connection's main database ('' if none), asked
    once per connection where the connection can remember it."""

    known = getattr(conn, "_market_registry_database", None)
    if isinstance(known, str):
        return known
    database = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
    try:
        conn._market_registry_database = database  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass  # a plain sqlite3.Connection takes no attributes
    return database


# (database, station, event_date) -> (fingerprint, rows), charter 178.
_ACTIVE_ROWS_MEMO: dict[tuple[str, str, str], tuple[tuple[Any, ...], list]] = {}
_SCOPE_LANES_MEMO: dict[tuple[str, str, str], tuple[tuple[Any, ...], list]] = {}
_SCOPE_MEMO_MAX_KEYS = 4000


def _scope_memo_put(memo: dict, key: tuple, value: tuple) -> None:
    if len(memo) >= _SCOPE_MEMO_MAX_KEYS:
        memo.clear()
    memo[key] = value


def registry_scope_timezone_lanes(
    conn: sqlite3.Connection, *, station_id: str, event_date: str
) -> list[tuple[str, str, str, str]]:
    """``SELECT DISTINCT station_id,event_date,settlement_timezone,source_lane``
    for one scope, remembered until the table could have changed it.

    The observed-running-high read asks this once per decision compile: 145
    times in the first ledger-measured cycle (2026-09-21 08:54Z) for 36.3s,
    each one a walk over the scope's ~880 wide snapshot rows.
    """
    question = (
        "SELECT DISTINCT station_id,event_date,settlement_timezone,source_lane"
        f" FROM {TABLE} WHERE station_id=? AND event_date=?"
    )
    key = _triples_memo_key(conn)
    if key is not None:
        hit = _SCOPE_LANES_MEMO.get((key[0], station_id, event_date))
        if hit is not None and hit[0] == key[1]:
            return list(hit[1])
    rows = [tuple(row) for row in conn.execute(question, (station_id, event_date)).fetchall()]
    if key is not None:
        _scope_memo_put(_SCOPE_LANES_MEMO, (key[0], station_id, event_date), (key[1], list(rows)))
    return rows


def _index_present(conn: sqlite3.Connection, name: str) -> bool:
    """Whether ``name`` exists in this database (a store not yet migrated
    falls back to the path that does not need it)."""
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def registry_eligible_scopes(conn: sqlite3.Connection) -> set[tuple[Any, Any]]:
    """``SELECT DISTINCT station_id,event_date WHERE source_eligible=1`` over the table.

    The truth increment's backlog probe asked this of the whole table every
    production cycle it ran: 23-25s on 2026-09-14, the covering index for the
    question leading with the snapshot, so a whole-table read became a walk
    over every row's table page.  Per snapshot the same index answers it
    without touching the table -- 3.1s cold for all of them, then remembered.
    """

    return _union_over_snapshots(conn, _ELIGIBLE_SCOPE_QUESTION)  # type: ignore[return-value]


def load_started_registry_rows(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str | None = None,
    station_id: str = "",
    event_date: str = "",
) -> list[dict[str, Any]]:
    """Read formal target scopes whose registry settlement day has started.

    The snapshot carries listed-but-not-started days (charter 48 trades a
    listing at listing; ``market_ingress`` asks for them explicitly with
    ``include_listing_days``).  Formal inventory, order-state and readiness
    consumers keep this started-day view, because those reads belong to a
    day that has begun.
    """

    decision_clock = as_of_utc or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result: list[dict[str, Any]] = []
    for row in load_active_registry_rows(conn, station_id=station_id, event_date=event_date):
        try:
            state = settlement_day_state(
                str(row.get("event_date") or ""),
                as_of_utc=decision_clock,
                settlement_timezone=str(row.get("settlement_timezone") or ""),
            )["state"]
        except Exception:
            continue
        if state in {"open", "closed"}:
            result.append(row)
    return result


__all__ = [
    "DDL", "LISTED_DAYS_PER_STATION", "SCHEMA", "SNAPSHOT_TABLE", "TABLE",
    "apply_target_market_registry_schema",
    "export_shared_live_registry", "ingest_target_market_registry",
    "load_active_registry_rows", "load_started_registry_rows",
    "registry_eligible_scopes", "registry_scope_timezone_triples",
]
